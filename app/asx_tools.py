"""
ASX-sourced tools: search, quotes with franking, announcements, company profile.

Prices from ASX are delayed by at least 20 minutes.
"""
import io
import logging
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastmcp.exceptions import ToolError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from . import asx_client, asx_directory
from .asx_client import AsxNotFoundError, normalise_code
from .ttl_cache import ttl_cache

logger = logging.getLogger(__name__)

SOURCE = "ASX (delayed at least 20 minutes)"
COMPANY_TAX_RATE = 0.30
DIVIDEND_TYPES = {"F": "Final", "I": "Interim"}
MAX_ANNOUNCEMENTS = 5  # the ASX endpoint only returns the latest five
MAX_TEXT_CHARS = 50_000
_DOCUMENT_KEY_RE = re.compile(r"^[A-Za-z0-9-]{5,64}$")


def _not_found(code: str) -> ToolError:
    return ToolError(f"ASX has no security with code {code}. Use search_asx_ticker to find the right code.")


# --- cached fetches ------------------------------------------------------------

@ttl_cache(seconds=15 * 60)
def _header(code: str) -> Dict[str, Any]:
    return asx_client.get_json(f"/companies/{code}/header")


@ttl_cache(seconds=15 * 60)
def _key_statistics(code: str) -> Dict[str, Any]:
    return asx_client.get_json(f"/companies/{code}/key-statistics")


@ttl_cache(seconds=5 * 60)
def _announcements(code: str) -> Dict[str, Any]:
    return asx_client.get_json(f"/companies/{code}/announcements")


@ttl_cache(seconds=7 * 24 * 60 * 60)
def _about(code: str) -> Dict[str, Any]:
    return asx_client.get_json(f"/companies/{code}/about")


@ttl_cache(seconds=30 * 24 * 60 * 60, maxsize=64)
def _document_pages(document_key: str) -> List[str]:
    """Extract text per page. Announcements never change, so cache for long."""
    try:
        reader = PdfReader(io.BytesIO(asx_client.get_file(document_key)))
        return [(page.extract_text() or "").strip() for page in reader.pages]
    except PdfReadError as e:
        raise ToolError(f"Document {document_key} is not a readable PDF: {e}") from e


# --- helpers ---------------------------------------------------------------------

def _excel_serial_to_iso(serial: Any) -> Optional[str]:
    """ASX reports financial period end dates as Excel serial day numbers."""
    try:
        return (date(1899, 12, 30) + timedelta(days=int(serial))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def grossed_up_yield(cash_yield: Optional[float], franking_percent: Optional[float]) -> Optional[float]:
    """
    Gross a cash yield (%) up for franking credits at the 30% company tax rate.

    e.g. 2.96% fully franked -> 4.23%. Base-rate entities frank at 25%, for
    which this slightly overstates the credit.
    """
    if not cash_yield or franking_percent is None:
        return None
    rate = COMPANY_TAX_RATE
    return round(cash_yield * (1 + (franking_percent / 100) * rate / (1 - rate)), 4)


def _dividend(stats: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if stats.get("dividend") is None:
        return None
    declared_currency = stats.get("dividendCurrency") or "AUD"
    foreign_amount = stats.get("foreignCurrencyAmount")
    return {
        "amount_aud": stats.get("dividend"),
        "type": DIVIDEND_TYPES.get(stats.get("dividendType"), stats.get("dividendType")),
        "franking_percent": stats.get("frankingPercent"),
        "declared_currency": declared_currency,
        "declared_amount": foreign_amount if declared_currency != "AUD" and foreign_amount else stats.get("dividend"),
        "ex_date": stats.get("dateExDate"),
        "record_date": stats.get("dateRecordDate"),
        "pay_date": stats.get("datePayDate"),
    }


# --- tools ------------------------------------------------------------------------

def search_asx(query: str, limit: int = 10) -> Dict[str, Any]:
    """Search ASX securities by code, company name or GICS industry group."""
    if not query.strip():
        raise ToolError("Search query must not be empty")
    results = asx_directory.search(query, limit)
    return {"query": query, "count": len(results), "tickers": results}


def get_ticker_info(symbol: str) -> Dict[str, Any]:
    """Quote, valuation, dividend/franking and income data for an ASX security."""
    code = normalise_code(symbol)
    try:
        header = _header(code)
    except AsxNotFoundError:
        raise _not_found(code) from None
    if not header:
        raise _not_found(code)

    try:
        stats = _key_statistics(code)
    except AsxNotFoundError:
        stats = {}  # some security types have a header but no statistics

    last = header.get("priceLast")
    change = header.get("priceChange")
    previous_close = round(last - change, 4) if last is not None and change is not None \
        else stats.get("priceClose")
    dividend = _dividend(stats)
    cash_yield = stats.get("yieldAnnual")
    franking = dividend["franking_percent"] if dividend else None

    return {
        "symbol": code,
        "name": header.get("displayName") or code,
        "current_price": last,
        "change": change,
        "change_percent": header.get("priceChangePercent"),
        "previous_close": previous_close,
        "bid": header.get("priceBid"),
        "ask": header.get("priceAsk"),
        "day_high": stats.get("priceDayHigh"),
        "day_low": stats.get("priceDayLow"),
        "volume": header.get("volume"),
        "average_volume": stats.get("volumeAverage"),
        "market_cap": header.get("marketCap"),
        "shares_on_issue": stats.get("numOfShares"),
        "pe_ratio": stats.get("priceEarningsRatio"),
        "eps": stats.get("earningsPerShare"),
        "dividend_yield": cash_yield,
        "grossed_up_yield": grossed_up_yield(cash_yield, franking),
        "dividend": dividend,
        "52_week_high": stats.get("priceFiftyTwoWeekHigh"),
        "52_week_low": stats.get("priceFiftyTwoWeekLow"),
        "currency": stats.get("currency") or "AUD",
        "sector": header.get("sector"),
        "industry_group": header.get("industryGroup"),
        "isin": stats.get("isin"),
        "security_description": stats.get("shareDescription"),
        "listed_date": header.get("dateListed"),
        "annual_income": [
            {
                "period": row.get("period"),
                "period_end": _excel_serial_to_iso(row.get("fPeriodEndDate")),
                "revenue": row.get("revenue"),
                "net_income": row.get("netIncome"),
                "currency": row.get("curCode"),
            }
            for row in stats.get("incomeStatement") or []
        ],
        "source": SOURCE,
    }


def get_announcements(symbol: str, limit: int = MAX_ANNOUNCEMENTS) -> Dict[str, Any]:
    """Latest ASX company announcements (up to five)."""
    code = normalise_code(symbol)
    try:
        data = _announcements(code)
    except AsxNotFoundError:
        raise _not_found(code) from None

    limit = max(1, min(limit, MAX_ANNOUNCEMENTS))
    items = [
        {
            "headline": item.get("headline"),
            "type": item.get("announcementType"),
            "date": item.get("date"),
            "price_sensitive": bool(item.get("isPriceSensitive")),
            "document_key": item.get("documentKey"),
            "file_size": item.get("fileSize"),
            "pdf_url": f"{asx_client.BASE_URL}/file/{item['documentKey']}" if item.get("documentKey") else None,
        }
        for item in (data.get("items") or [])[:limit]
    ]
    return {
        "symbol": code,
        "name": data.get("displayName") or code,
        "count": len(items),
        "announcements": items,
        "source": "ASX company announcements",
    }


def get_announcement_text(document_key: str, offset: int = 0, max_chars: int = 20_000) -> Dict[str, Any]:
    """Extract the text of an ASX announcement PDF, paginated by character offset."""
    key = document_key.strip()
    if not _DOCUMENT_KEY_RE.match(key):
        raise ToolError(f"'{document_key}' is not a valid ASX document key (e.g. '2924-03160939-2A1710205')")
    offset = max(0, offset)
    max_chars = max(1, min(max_chars, MAX_TEXT_CHARS))

    try:
        pages = _document_pages(key)
    except AsxNotFoundError:
        raise ToolError(f"No ASX announcement found with document key {key}") from None

    full_text = "\n\n".join(f"--- Page {i} ---\n{text}" for i, text in enumerate(pages, 1))
    chunk = full_text[offset:offset + max_chars]
    next_offset = offset + len(chunk)
    has_more = next_offset < len(full_text)
    return {
        "document_key": key,
        "pages": len(pages),
        "total_chars": len(full_text),
        "offset": offset,
        "next_offset": next_offset if has_more else None,
        "text": chunk,
        "note": None if any(pages) else "No extractable text; the PDF may be a scanned image.",
    }


def get_company_profile(symbol: str) -> Dict[str, Any]:
    """Company description, directors, contact details and share registry."""
    code = normalise_code(symbol)
    try:
        about = _about(code)
    except AsxNotFoundError:
        raise _not_found(code) from None

    contact = about.get("addressContact") or {}
    registry = about.get("addressShareRegistry") or {}
    return {
        "symbol": code,
        "name": about.get("displayName") or code,
        "type": asx_directory.ISSUE_TYPES.get(about.get("issueType"), about.get("issueType")),
        "description": about.get("description"),
        "website": about.get("websiteUrl"),
        "directors": [
            {"name": d.get("name"), "title": d.get("title")} for d in about.get("directors") or []
        ],
        "contact": {
            "address": contact.get("address"),
            "phone": contact.get("phone"),
            "fax": contact.get("fax"),
        },
        "share_registry": {
            "name": registry.get("attention"),
            "address": registry.get("address"),
            "phone": registry.get("phone"),
        },
        "source": "ASX",
    }
