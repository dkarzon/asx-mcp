"""
ASX listed-company directory and ticker search.

The full directory (~1,800 companies) is downloaded from ASX once a day,
saved to CACHE_DIR, and searched locally. ASX's predictive search is used
alongside it because the directory excludes ETFs and other non-company
products.
"""
import csv
import io
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp.exceptions import ToolError

from . import asx_client
from .ttl_cache import ttl_cache

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.getenv("CACHE_DIR", "/tmp/asx_cache"))
DIRECTORY_FILE_NAME = "asx_directory.csv"
DIRECTORY_MAX_AGE_SECONDS = 24 * 60 * 60
MAX_RESULTS = 25

ISSUE_TYPES = {"CS": "Equity", "ETF": "ETF"}


def _parse_date(value: str) -> Optional[str]:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def _parse_int(value: str) -> Optional[int]:
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_directory(text: str) -> List[Dict[str, Any]]:
    """Parse the ASX directory CSV into result-shaped dicts."""
    entries = []
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("ASX code") or "").strip().upper()
        if not code:
            continue
        entries.append({
            "symbol": code,
            "name": (row.get("Company name") or "").strip(),
            "type": None,  # the directory doesn't say; filled in from predictive search
            "industry_group": (row.get("GICs industry group") or "").strip() or None,
            "market_cap": _parse_int(row.get("Market Cap") or ""),
            "listing_date": _parse_date(row.get("Listing date") or ""),
        })
    return entries


class AsxDirectory:
    """Thread-safe, daily-refreshed copy of the ASX company directory."""

    def __init__(self, cache_dir: Path):
        self._file = cache_dir / DIRECTORY_FILE_NAME
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []
        self._loaded_at = 0.0

    def _file_age(self) -> Optional[float]:
        try:
            return time.time() - self._file.stat().st_mtime
        except OSError:
            return None

    def _download(self) -> None:
        text = asx_client.get_directory_csv()
        entries = parse_directory(text)
        if not entries:
            raise ToolError("ASX company directory download was empty")
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._file)
        logger.info("Downloaded ASX directory: %d companies", len(entries))

    def entries(self) -> List[Dict[str, Any]]:
        """Return directory entries, refreshing from ASX if older than a day."""
        with self._lock:
            if self._entries and time.time() - self._loaded_at < DIRECTORY_MAX_AGE_SECONDS:
                return self._entries

            age = self._file_age()
            if age is None or age >= DIRECTORY_MAX_AGE_SECONDS:
                try:
                    self._download()
                except Exception as e:  # keep serving a stale copy if we have one
                    if age is None:
                        raise ToolError(f"Could not download the ASX company directory: {e}") from e
                    logger.warning("Directory refresh failed, using stale copy: %s", e)

            self._entries = parse_directory(self._file.read_text(encoding="utf-8"))
            self._loaded_at = time.time()
            return self._entries


def _score(entry: Dict[str, Any], query: str, tokens: List[str]) -> int:
    code = entry["symbol"].lower()
    name = entry["name"].lower()
    if code == query:
        return 100
    if code.startswith(query):
        return 70
    if name.startswith(query):
        return 60
    if all(re.search(r"\b" + re.escape(t), name) for t in tokens):
        return 50
    if query in name:
        return 40
    if query in (entry.get("industry_group") or "").lower():
        return 20
    return 0


@ttl_cache(seconds=24 * 60 * 60, maxsize=512)
def _predictive_search(query: str) -> List[Dict[str, Any]]:
    items = asx_client.get_json("/search/predictive", {"searchText": query}).get("items") or []
    return [{
        "symbol": (item.get("symbol") or "").upper(),
        "name": item.get("displayName") or "",
        "type": ISSUE_TYPES.get(item.get("issueType"), item.get("issueType")),
        "industry_group": item.get("industryGroup"),
        "market_cap": item.get("marketCap"),
        "listing_date": None,
    } for item in items if item.get("symbol")]


_directory: Optional[AsxDirectory] = None
_directory_lock = threading.Lock()


def get_directory() -> AsxDirectory:
    global _directory
    with _directory_lock:
        if _directory is None:
            _directory = AsxDirectory(CACHE_DIR)
        return _directory


def search(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search ASX securities by code, name or GICS industry group."""
    q = query.strip().lower()
    tokens = q.split()
    limit = max(1, min(limit, MAX_RESULTS))

    scored: Dict[str, tuple] = {}
    for entry in get_directory().entries():
        score = _score(entry, q, tokens)
        if score:
            scored[entry["symbol"]] = (score, entry)

    try:
        remote = _predictive_search(q)
    except Exception as e:  # predictive search is a supplement; degrade gracefully
        logger.warning("ASX predictive search failed for %r: %s", q, e)
        remote = []

    for item in remote:
        existing = scored.get(item["symbol"])
        if existing:
            score, entry = existing
            scored[item["symbol"]] = (score, {**entry, "type": entry["type"] or item["type"]})
        else:
            score = _score(item, q, tokens) or 45  # ASX thought it relevant
            scored[item["symbol"]] = (score, item)

    ranked = sorted(scored.values(), key=lambda se: (-se[0], -(se[1]["market_cap"] or 0)))
    return [entry for _, entry in ranked[:limit]]
