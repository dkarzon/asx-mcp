"""
Live smoke tests against the real ASX endpoints. Skipped unless ASX_LIVE_TESTS=1.

The ASX endpoints are undocumented and can change without notice; run these
to find out quickly when they do:  ASX_LIVE_TESTS=1 pytest tests/test_live.py
"""
import os

import pytest

from app import asx_tools, yahoo_tools

pytestmark = pytest.mark.skipif(os.getenv("ASX_LIVE_TESTS") != "1", reason="set ASX_LIVE_TESTS=1")


def test_live_search():
    symbols = [t["symbol"] for t in asx_tools.search_asx("commonwealth bank")["tickers"]]
    assert "CBA" in symbols


def test_live_info_has_franking():
    info = asx_tools.get_ticker_info("CBA")
    assert info["current_price"] and info["dividend"]["franking_percent"] is not None


def test_live_announcements_and_text():
    items = asx_tools.get_announcements("CBA")["announcements"]
    assert items
    assert asx_tools.get_announcement_text(items[0]["document_key"], 0, 500)["pages"] >= 1


def test_live_profile():
    assert asx_tools.get_company_profile("CBA")["directors"]


def test_live_asx_history():
    df = yahoo_tools.asx_history("CBA", "1mo", "1d")
    assert len(df) > 10 and df["Close"].notna().all()
