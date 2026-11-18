"""
HTTP client for the ASX website's data endpoints.

These are the undocumented endpoints that power asx.com.au company pages
(hosted by Markit Digital). They are not a supported public API and may
change without notice, so every ASX URL lives in this module.
"""
import logging
import re
from typing import Any, Dict, Optional

import requests
from fastmcp.exceptions import ToolError

from .rate_limiter import asx_rate_limited_retry

logger = logging.getLogger(__name__)

BASE_URL = "https://asx.api.markitdigital.com/asx-research/1.0"
CHART_URL = "https://www.asx.com.au/asx/1/chart/highcharts"
TIMEOUT_SECONDS = 20
MAX_FILE_BYTES = 25 * 1024 * 1024

_session = requests.Session()
_session.headers.update({
    # The endpoints serve a browser front end; a browser-like UA avoids blocks.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/csv, application/pdf, */*",
})

_CODE_RE = re.compile(r"^[A-Z0-9]{1,6}$")


class AsxNotFoundError(ToolError):
    """The ASX endpoint reported that the requested security or file does not exist."""


def normalise_code(symbol: str) -> str:
    """Normalise a ticker to a bare ASX code, e.g. ' cba.ax ' -> 'CBA'."""
    code = symbol.strip().upper()
    if code.endswith(".AX"):
        code = code[:-3]
    if not code:
        raise ToolError("Ticker symbol must not be empty")
    if not _CODE_RE.match(code):
        raise ToolError(f"'{symbol}' is not a valid ASX code (expected 1-6 letters/digits, e.g. 'CBA')")
    return code


@asx_rate_limited_retry
def _get(url: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    """
    GET a URL with rate limiting and retries.

    400/404 are mapped to AsxNotFoundError (not retried); other HTTP errors
    raise requests.HTTPError, which is retried.
    """
    logger.debug("ASX GET %s %s", url, params or "")
    resp = _session.get(url, params=params, timeout=TIMEOUT_SECONDS)
    if resp.status_code in (400, 404):
        raise AsxNotFoundError(f"ASX returned {resp.status_code} for {url}")
    resp.raise_for_status()
    return resp


def get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET a Markit ASX research endpoint and return its `data` object."""
    payload = _get(f"{BASE_URL}{path}", params).json()
    return payload.get("data") or {}


def get_directory_csv() -> str:
    """Download the full ASX listed-company directory as CSV text."""
    resp = _get(f"{BASE_URL}/companies/directory/file")
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def get_daily_chart(code: str) -> list:
    """
    Daily [timestamp_ms, open, high, low, close, volume] rows (about 10 years),
    oldest first. Works for indices too (e.g. 'XJO').
    """
    return _get(CHART_URL, {"asx_code": code, "complete": "true"}).json()


def get_file(document_key: str) -> bytes:
    """Download an announcement document (PDF) by its documentKey."""
    resp = _get(f"{BASE_URL}/file/{document_key}")
    if len(resp.content) > MAX_FILE_BYTES:
        raise ToolError(f"Document {document_key} is larger than {MAX_FILE_BYTES // 1_000_000}MB")
    return resp.content
