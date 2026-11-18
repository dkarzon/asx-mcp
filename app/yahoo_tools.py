"""
Yahoo Finance-sourced tools: price history (with an ASX fallback) and news.
"""
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Literal

import pandas as pd
import yfinance as yf
from fastmcp.exceptions import ToolError

from . import asx_client
from .asx_client import AsxNotFoundError, normalise_code
from .rate_limiter import RETRYABLE_EXCEPTIONS, rate_limited_retry

logger = logging.getLogger(__name__)

# yfinance defaults to ~/.cache, which isn't writable for the container's
# unprivileged user. Keep its timezone/cookie caches in CACHE_DIR instead, so
# they persist (fewer Yahoo round-trips and rate limits). Must run before any
# yfinance call.
YFINANCE_CACHE_DIR = Path(os.getenv("CACHE_DIR", "/tmp/asx_cache")) / "yfinance"
try:
    YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))
except OSError as e:
    logger.warning("Could not set yfinance cache location %s: %s", YFINANCE_CACHE_DIR, e)

Period =Literal["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]
Interval = Literal[
    "1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"
]

MAX_NEWS = 50
ASX_TZ = "Australia/Sydney"

# Intervals the ASX fallback can serve (it only has daily bars, which we resample).
ASX_RESAMPLE_RULES = {"1d": None, "1wk": "W-FRI", "1mo": "ME", "3mo": "QE"}
PERIOD_OFFSETS = {
    "1mo": pd.DateOffset(months=1), "3mo": pd.DateOffset(months=3),
    "6mo": pd.DateOffset(months=6), "1y": pd.DateOffset(years=1),
    "2y": pd.DateOffset(years=2), "5y": pd.DateOffset(years=5),
    "10y": pd.DateOffset(years=10),
}
PERIOD_ROWS = {"1d": 1, "5d": 5}


def _yahoo_symbol(symbol: str) -> str:
    """Normalise a ticker to Yahoo's ASX form, e.g. 'cba' -> 'CBA.AX'."""
    return f"{normalise_code(symbol)}.AX"


def _float_or_none(value: Any) -> Any:
    return None if pd.isna(value) else float(value)


def _frame_to_rows(hist: pd.DataFrame) -> list:
    return [
        {
            "date": date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(date, "strftime") else str(date),
            "open": _float_or_none(row["Open"]),
            "high": _float_or_none(row["High"]),
            "low": _float_or_none(row["Low"]),
            "close": _float_or_none(row["Close"]),
            "volume": None if pd.isna(row["Volume"]) else int(row["Volume"]),
        }
        for date, row in hist.iterrows()
    ]


@rate_limited_retry
def _yahoo_history(symbol: str, period: str, interval: str) -> pd.DataFrame:
    hist = yf.Ticker(symbol).history(period=period, interval=interval)
    if hist.empty:
        raise ToolError(
            f"No price history found for {symbol} (period={period}, interval={interval}). "
            "The ticker may be invalid or delisted, or the interval may not be available "
            "for that period (intraday intervals are limited to recent data)."
        )
    return hist


def asx_history(code: str, period: str, interval: str) -> pd.DataFrame:
    """Daily OHLCV from ASX (about 10 years), sliced to `period` and resampled to `interval`."""
    if interval not in ASX_RESAMPLE_RULES:
        raise ToolError(f"ASX history only supports intervals {sorted(ASX_RESAMPLE_RULES)}")
    rows = asx_client.get_daily_chart(code)
    if not rows:
        raise ToolError(f"ASX returned no price history for {code}")

    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df.index = (pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
                .dt.tz_convert(ASX_TZ).dt.normalize().rename(None))

    if period in PERIOD_ROWS:
        df = df.tail(PERIOD_ROWS[period])
    elif period == "ytd":
        df = df[df.index >= pd.Timestamp(datetime.now().year, 1, 1, tz=ASX_TZ)]
    elif period in PERIOD_OFFSETS:
        df = df[df.index >= df.index[-1] - PERIOD_OFFSETS[period]]

    rule = ASX_RESAMPLE_RULES[interval]
    if rule:
        # Label each bar with its first trading day, as Yahoo does.
        df = df.assign(Start=df.index).resample(rule).agg(
            {"Start": "first", "Open": "first", "High": "max", "Low": "min",
             "Close": "last", "Volume": "sum"}
        ).dropna(subset=["Close"]).set_index("Start").rename_axis(None)
    return df


def get_ticker_history(symbol: str, period: Period = "1mo", interval: Interval = "1d") -> Dict[str, Any]:
    """
    Historical OHLCV from Yahoo Finance. If Yahoo fails and the interval is
    daily or coarser, falls back to ASX daily data (about 10 years).
    """
    yahoo_symbol = _yahoo_symbol(symbol)
    source, fallback_reason = "Yahoo Finance", None
    try:
        hist = _yahoo_history(yahoo_symbol, period, interval)
    except (ToolError, *RETRYABLE_EXCEPTIONS) as yahoo_error:
        if interval not in ASX_RESAMPLE_RULES:
            raise
        logger.warning("Yahoo history failed for %s, trying ASX: %s", yahoo_symbol, yahoo_error)
        try:
            hist = asx_history(normalise_code(symbol), period, interval)
        except AsxNotFoundError:
            raise yahoo_error from None  # both sources agree it doesn't exist
        source = ("ASX (fallback; daily closes up to the previous trading day, "
                  "not adjusted for splits or dividends)")
        fallback_reason = str(yahoo_error) or type(yahoo_error).__name__

    data = _frame_to_rows(hist)
    result = {
        "symbol": yahoo_symbol,
        "period": period,
        "interval": interval,
        "count": len(data),
        "data": data,
        "source": source,
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result


@rate_limited_retry
def get_ticker_news(symbol: str, limit: int = 10) -> Dict[str, Any]:
    """
    Get recent Yahoo Finance news for an ASX ticker.

    Args:
        symbol: ASX ticker symbol (e.g., 'CBA' or 'CBA.AX')
        limit: Maximum number of news items (default: 10, max: 50)

    Returns:
        Dictionary containing recent news articles
    """
    symbol = _yahoo_symbol(symbol)
    limit = max(1, min(limit, MAX_NEWS))
    news = yf.Ticker(symbol).get_news(count=limit) or []

    news_items = []
    for item in news:
        content = item.get("content") or {}
        provider = content.get("provider") or {}
        canonical = content.get("canonicalUrl") or {}
        resolutions = (content.get("thumbnail") or {}).get("resolutions") or []

        news_items.append({
            "title": content.get("title"),
            "publisher": provider.get("displayName"),
            "link": canonical.get("url"),
            "published": content.get("pubDate"),
            "summary": content.get("summary"),
            "thumbnail": resolutions[0].get("url") if resolutions else None,
        })

    return {
        "symbol": symbol,
        "count": len(news_items),
        "news": news_items,
        "source": "Yahoo Finance",
    }
