"""
asx-mcp - FastMCP server for Australian Stock Exchange market data.
"""
import logging
import os

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.asx_tools import (
    get_announcement_text,
    get_announcements,
    get_company_profile,
    get_ticker_info,
    search_asx,
)
from app.yahoo_tools import Interval, Period, get_ticker_history, get_ticker_news

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Initialize FastMCP server
mcp = FastMCP("asx-mcp")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness probe for container orchestration."""
    return JSONResponse({"status": "ok"})


@mcp.tool()
def search_asx_ticker(query: str, limit: int = 10) -> dict:
    """
    Search ASX-listed securities (companies and ETFs) by code, company name,
    or GICS industry group (e.g. 'Banks', 'Materials'). Source: ASX.

    Args:
        query: Search string to match against ticker data
        limit: Maximum number of results to return (default: 10, max: 25)

    Returns:
        Dictionary with search results including matched tickers
    """
    return search_asx(query, limit)


@mcp.tool()
def get_asx_ticker_info(symbol: str) -> dict:
    """
    Get quote, valuation and dividend data for an ASX security. Source: ASX,
    prices delayed at least 20 minutes.

    Includes the latest dividend with its franking percentage, ex/record/pay
    dates and declared currency, plus a grossed-up yield (franking credits at
    the 30% company tax rate) and recent annual revenue/net income.

    Args:
        symbol: ASX code (e.g., 'CBA' or 'CBA.AX')

    Returns:
        Dictionary of quote fields. dividend_yield and grossed_up_yield are
        percentages (e.g. 2.95 means 2.95%). dividend.amount_aud is in AUD even
        when the dividend was declared in another currency.
    """
    return get_ticker_info(symbol)


@mcp.tool()
def get_asx_ticker_history(
    symbol: str,
    period: Period = "1mo",
    interval: Interval = "1d",
) -> dict:
    """
    Get historical OHLCV (Open, High, Low, Close, Volume) data for an ASX ticker
    or index. Source: Yahoo Finance; if Yahoo fails, daily/weekly/monthly
    requests fall back to ASX data (about 10 years, unadjusted). The `source`
    field says which was used.

    Args:
        symbol: ASX ticker symbol (e.g., 'CBA' or 'CBA.AX')
        period: Time period (default: 1mo)
        interval: Data interval (default: 1d). Intraday intervals (1m-90m, 1h)
            are only available for recent periods.

    Returns:
        Dictionary containing historical price and volume data
    """
    return get_ticker_history(symbol, period, interval)


@mcp.tool()
def get_asx_ticker_news(symbol: str, limit: int = 10) -> dict:
    """
    Get recent Yahoo Finance news articles for an ASX ticker.

    Args:
        symbol: ASX ticker symbol (e.g., 'CBA' or 'CBA.AX')
        limit: Maximum number of news items to return (default: 10, max: 50)

    Returns:
        Dictionary containing recent news articles with titles, links, and publish dates
    """
    return get_ticker_news(symbol, limit)


@mcp.tool()
def get_asx_announcements(symbol: str, limit: int = 5) -> dict:
    """
    Get the latest official ASX company announcements (market releases) for a
    security, with a price-sensitive flag. ASX only exposes the five most recent.
    Use get_asx_announcement_text with an item's document_key to read it.

    Args:
        symbol: ASX code (e.g., 'CBA' or 'CBA.AX')
        limit: Maximum number of announcements to return (default: 5, max: 5)

    Returns:
        Dictionary with announcements: headline, type, date (UTC), price_sensitive,
        document_key and pdf_url
    """
    return get_announcements(symbol, limit)


@mcp.tool()
def get_asx_announcement_text(document_key: str, offset: int = 0, max_chars: int = 20000) -> dict:
    """
    Read the text of an ASX announcement PDF. Long documents are returned in
    chunks: call again with offset=next_offset until next_offset is null.

    Args:
        document_key: document_key from get_asx_announcements
            (e.g. '2924-03160939-2A1710205')
        offset: Character offset to start from (default: 0)
        max_chars: Maximum characters to return (default: 20000, max: 50000)

    Returns:
        Dictionary with the page count, text chunk, and next_offset
    """
    return get_announcement_text(document_key, offset, max_chars)


@mcp.tool()
def get_asx_company_profile(symbol: str) -> dict:
    """
    Get an ASX security's profile: description, website, directors, registered
    office contact details and share registry. Source: ASX.

    Args:
        symbol: ASX code (e.g., 'CBA' or 'CBA.AX')

    Returns:
        Dictionary of company profile details
    """
    return get_company_profile(symbol)


if __name__ == "__main__":
    # Run with HTTP transport for streamable support.
    # Defaults to localhost; the Dockerfile sets HOST=0.0.0.0 so the
    # server is reachable through the container's published port.
    mcp.run(
        transport="http",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
