"""Offline tests for the ASX tools. ASX HTTP and yfinance are mocked throughout."""
import asyncio
import os
import time
from types import SimpleNamespace

import pandas as pd
import pytest
from fastmcp.exceptions import ToolError
from yfinance.exceptions import YFRateLimitError

from app import asx_directory, asx_tools, yahoo_tools
from conftest import FakeResponse

API = "/asx-research/1.0"
CHART = "/asx/1/chart/highcharts"

DIRECTORY_CSV = (
    '"ASX code","Company name","GICs industry group","Listing date","Market Cap"\n'
    '"BOQ","BANK OF QUEENSLAND LIMITED.","Banks","10/12/1997",4604770442\n'
    '"CBA","COMMONWEALTH BANK OF AUSTRALIA.","Banks","12/09/1991",285224924298\n'
    '"BHP","BHP GROUP LIMITED","Materials","13/08/1885",200000000000\n'
    '"CBO","COBRAM ESTATE OLIVES LIMITED","Food, Beverage & Tobacco","25/05/2022",900000000\n'
)

CBA_HEADER = {"data": {
    "dateListed": "1991-09-12", "displayName": "COMMONWEALTH BANK OF AUSTRALIA.",
    "priceAsk": 170.08, "priceBid": 170.07, "priceChange": -0.36, "priceChangePercent": -0.211,
    "priceLast": 170.08, "sector": "Financials", "industryGroup": "Banks", "symbol": "CBA",
    "volume": 598907, "marketCap": 285224924298,
}}
CBA_STATS = {"data": {
    "isin": "AU000000CBA7", "priceClose": 170.44, "priceDayHigh": 170.66, "priceDayLow": 167.56,
    "priceFiftyTwoWeekHigh": 185.59, "priceFiftyTwoWeekLow": 146.98, "volumeAverage": 2067606.2,
    "dateExDate": "2026-08-19", "datePayDate": "2026-09-29", "dateRecordDate": "2026-08-20",
    "dividend": 2.7, "dividendCurrency": "AUD", "dividendType": "F", "frankingPercent": 100,
    "foreignCurrencyAmount": 0, "earningsPerShare": 6.51792, "priceEarningsRatio": 26.25684,
    "currency": "AUD", "shareDescription": "Ordinary Fully Paid", "numOfShares": 1673462358,
    "yieldAnnual": 2.9629195024642105,
    "incomeStatement": [{"revenue": 30474000000, "netIncome": 10866000000, "period": "2026A",
                         "fPeriodEndDate": 46203, "curCode": "AUD"}],
}}
NOT_FOUND = FakeResponse(400, {"error": {"code": 400, "message": "Bad Request: Symbol not found"}})


def ok(body):
    return FakeResponse(200, body)


@pytest.fixture
def directory(fake_asx):
    fake_asx.add(f"{API}/companies/directory/file", FakeResponse(200, text=DIRECTORY_CSV))
    fake_asx.add(f"{API}/search/predictive", ok({"data": {"items": []}}))
    return fake_asx


# --- search -----------------------------------------------------------------

def test_search_ranks_exact_code_then_name_then_market_cap(directory):
    result = asx_tools.search_asx("cba")
    assert [t["symbol"] for t in result["tickers"]] == ["CBA"]
    assert result["tickers"][0] == {
        "symbol": "CBA", "name": "COMMONWEALTH BANK OF AUSTRALIA.", "type": None,
        "industry_group": "Banks", "market_cap": 285224924298, "listing_date": "1991-09-12",
    }
    # name prefix ('BANK OF QUEENSLAND') outranks a name word match ('...WEALTH BANK')
    assert [t["symbol"] for t in asx_tools.search_asx("bank")["tickers"]] == ["BOQ", "CBA"]
    # equal scores (industry-group match only) are ordered by market cap
    assert [t["symbol"] for t in asx_tools.search_asx("banks")["tickers"]] == ["CBA", "BOQ"]


def test_search_merges_predictive_results_for_etfs(directory):
    directory.add(f"{API}/search/predictive", ok({"data": {"items": [
        {"symbol": "BANK", "displayName": "GLOBAL X AUSTRALIAN BANK CREDIT ETF", "issueType": "ETF",
         "marketCap": 189306855},
        {"symbol": "CBA", "displayName": "COMMONWEALTH BANK OF AUSTRALIA.", "issueType": "CS"},
    ]}}))
    tickers = asx_tools.search_asx("bank")["tickers"]
    assert tickers[0]["symbol"] == "BANK" and tickers[0]["type"] == "ETF"  # exact code match
    assert next(t for t in tickers if t["symbol"] == "CBA")["type"] == "Equity"


def test_search_survives_predictive_failure(directory):
    directory.add(f"{API}/search/predictive", FakeResponse(503))
    assert asx_tools.search_asx("bhp")["tickers"][0]["symbol"] == "BHP"


def test_directory_is_downloaded_once_and_cached_to_disk(directory):
    asx_tools.search_asx("cba")
    asx_tools.search_asx("bhp")
    asx_directory._directory = None  # simulate a restart: reload from disk, not ASX
    asx_tools.search_asx("boq")
    assert directory.count(f"{API}/companies/directory/file") == 1
    assert (asx_directory.CACHE_DIR / "asx_directory.csv").exists()


def test_stale_directory_is_used_when_refresh_fails(fake_asx):
    path = asx_directory.CACHE_DIR / "asx_directory.csv"
    path.write_text(DIRECTORY_CSV, encoding="utf-8")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(path, (old, old))
    fake_asx.add(f"{API}/companies/directory/file", FakeResponse(503))
    assert asx_tools.search_asx("bhp")["tickers"][0]["symbol"] == "BHP"


def test_search_rejects_empty_query():
    with pytest.raises(ToolError):
        asx_tools.search_asx("   ")


# --- info -------------------------------------------------------------------

def test_info_returns_quote_with_franking(fake_asx):
    fake_asx.add(f"{API}/companies/CBA/header", ok(CBA_HEADER))
    fake_asx.add(f"{API}/companies/CBA/key-statistics", ok(CBA_STATS))
    info = asx_tools.get_ticker_info(" cba.ax ")
    assert info["symbol"] == "CBA"
    assert info["current_price"] == 170.08
    assert info["previous_close"] == 170.44
    assert info["dividend"] == {
        "amount_aud": 2.7, "type": "Final", "franking_percent": 100,
        "declared_currency": "AUD", "declared_amount": 2.7,
        "ex_date": "2026-08-19", "record_date": "2026-08-20", "pay_date": "2026-09-29",
    }
    assert info["grossed_up_yield"] == pytest.approx(4.2327, abs=1e-4)
    assert info["annual_income"][0]["period_end"] == "2026-06-30"


def test_info_reports_foreign_declared_dividend(fake_asx):
    stats = {"data": {**CBA_STATS["data"], "dividend": 1.3799, "dividendCurrency": "USD",
                      "foreignCurrencyAmount": 0.99}}
    fake_asx.add(f"{API}/companies/BHP/header", ok(CBA_HEADER))
    fake_asx.add(f"{API}/companies/BHP/key-statistics", ok(stats))
    dividend = asx_tools.get_ticker_info("BHP")["dividend"]
    assert dividend["amount_aud"] == 1.3799
    assert (dividend["declared_currency"], dividend["declared_amount"]) == ("USD", 0.99)


def test_info_without_dividend(fake_asx):
    fake_asx.add(f"{API}/companies/ZIP/header", ok(CBA_HEADER))
    fake_asx.add(f"{API}/companies/ZIP/key-statistics", ok({"data": {"yieldAnnual": 0}}))
    info = asx_tools.get_ticker_info("ZIP")
    assert info["dividend"] is None and info["grossed_up_yield"] is None


def test_grossed_up_yield_partial_franking():
    assert asx_tools.grossed_up_yield(4.0, 30) == pytest.approx(4.0 * (1 + 0.3 * 0.3 / 0.7), abs=1e-4)
    assert asx_tools.grossed_up_yield(4.0, 0) == 4.0
    assert asx_tools.grossed_up_yield(0, 100) is None


def test_info_unknown_code_is_not_retried(fake_asx):
    fake_asx.add(f"{API}/companies/XYZQ/header", NOT_FOUND)
    with pytest.raises(ToolError, match="no security with code XYZQ"):
        asx_tools.get_ticker_info("XYZQ")
    assert fake_asx.count(f"{API}/companies/XYZQ/header") == 1


def test_info_retries_server_errors(fake_asx):
    fake_asx.add(f"{API}/companies/CBA/header", FakeResponse(503), ok(CBA_HEADER))
    fake_asx.add(f"{API}/companies/CBA/key-statistics", ok(CBA_STATS))
    assert asx_tools.get_ticker_info("CBA")["current_price"] == 170.08
    assert fake_asx.count(f"{API}/companies/CBA/header") == 2


def test_info_is_cached(fake_asx):
    fake_asx.add(f"{API}/companies/CBA/header", ok(CBA_HEADER))
    fake_asx.add(f"{API}/companies/CBA/key-statistics", ok(CBA_STATS))
    asx_tools.get_ticker_info("CBA")
    asx_tools.get_ticker_info("cba")
    assert fake_asx.count(f"{API}/companies/CBA/header") == 1


@pytest.mark.parametrize("bad", ["", "  ", "BAD$", "TOOLONGCODE"])
def test_invalid_codes_rejected_without_network(fake_asx, bad):
    with pytest.raises(ToolError):
        asx_tools.get_ticker_info(bad)
    assert fake_asx.calls == []


# --- announcements ----------------------------------------------------------

ANNOUNCEMENTS = {"data": {"displayName": "COMMONWEALTH BANK OF AUSTRALIA.", "items": [
    {"announcementType": "COMPANY ADMINISTRATION", "date": "2026-11-11T23:00:46.000Z",
     "documentKey": f"2924-0316093{i}-2A1710205", "fileSize": "162KB",
     "headline": f"Headline {i}", "isPriceSensitive": i == 0, "url": ""}
    for i in range(5)
]}}


def test_announcements_mapping_and_limit(fake_asx):
    fake_asx.add(f"{API}/companies/CBA/announcements", ok(ANNOUNCEMENTS))
    result = asx_tools.get_announcements("CBA", limit=50)
    assert result["count"] == 5
    first = result["announcements"][0]
    assert first["price_sensitive"] is True
    assert first["pdf_url"].endswith("/file/2924-03160930-2A1710205")
    assert asx_tools.get_announcements("CBA", limit=2)["count"] == 2


def test_announcements_unknown_code(fake_asx):
    fake_asx.add(f"{API}/companies/XYZQ/announcements", NOT_FOUND)
    with pytest.raises(ToolError, match="XYZQ"):
        asx_tools.get_announcements("XYZQ")


class FakePdfReader:
    def __init__(self, stream):
        self.pages = [SimpleNamespace(extract_text=lambda t=t: t) for t in ("Page one text", "Page two")]


def test_announcement_text_paginates(fake_asx, monkeypatch):
    monkeypatch.setattr(asx_tools, "PdfReader", FakePdfReader)
    fake_asx.add(f"{API}/file/2924-03160939-2A1710205", FakeResponse(200, content=b"%PDF"))
    first = asx_tools.get_announcement_text("2924-03160939-2A1710205", 0, 20)
    assert first["pages"] == 2 and first["text"] == "--- Page 1 ---\nPage "
    rest = asx_tools.get_announcement_text("2924-03160939-2A1710205", first["next_offset"], 1000)
    assert first["text"] + rest["text"] == "--- Page 1 ---\nPage one text\n\n--- Page 2 ---\nPage two"
    assert rest["text"].endswith("Page two") and rest["next_offset"] is None
    assert fake_asx.count(f"{API}/file/2924-03160939-2A1710205") == 1  # text is cached


def test_announcement_text_rejects_bad_key_and_missing_file(fake_asx):
    with pytest.raises(ToolError, match="not a valid"):
        asx_tools.get_announcement_text("../../etc/passwd")
    with pytest.raises(ToolError, match="No ASX announcement"):
        asx_tools.get_announcement_text("2924-00000000-XXXXXXX")


# --- profile ----------------------------------------------------------------

def test_company_profile(fake_asx):
    fake_asx.add(f"{API}/companies/VAS/about", ok({"data": {
        "displayName": "VANGUARD AUSTRALIAN SHARES INDEX ETF", "issueType": "ETF",
        "description": "Tracks the S&P/ASX 300.", "websiteUrl": "http://www.vanguard.com.au",
        "addressContact": {"address": "Melbourne", "phone": "(03) 8888 3888"},
        "addressShareRegistry": {"attention": "COMPUTERSHARE", "address": "Brisbane"},
    }}))
    profile = asx_tools.get_company_profile("vas")
    assert profile["type"] == "ETF"
    assert profile["directors"] == []
    assert profile["share_registry"]["name"] == "COMPUTERSHARE"


# --- history ----------------------------------------------------------------

def fake_ticker(monkeypatch, **attrs):
    monkeypatch.setattr(yahoo_tools.yf, "Ticker", lambda symbol: SimpleNamespace(**attrs))


def raise_(exc):
    raise exc


def ms(day):
    """Epoch ms for a date at midnight UTC, as the ASX chart endpoint returns."""
    return int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)


ASX_CHART = [
    [ms("2026-11-10"), 1.0, 2.0, 0.5, 1.5, 100],
    [ms("2026-11-11"), 1.5, 3.0, 1.0, 2.5, 200],
    [ms("2026-11-16"), 2.5, 2.6, 2.0, 2.2, 300],
    [ms("2026-11-17"), 2.2, 2.4, 2.1, 2.3, 400],
]


def test_history_converts_rows_and_nans(monkeypatch):
    df = pd.DataFrame(
        {"Open": [1.0, float("nan")], "High": [2.0, 2.5], "Low": [0.5, 0.6],
         "Close": [1.5, 1.6], "Volume": [100, float("nan")]},
        index=pd.to_datetime(["2026-11-17", "2026-11-18"]),
    )
    fake_ticker(monkeypatch, history=lambda period, interval: df)
    result = yahoo_tools.get_ticker_history("CBA", "5d", "1d")
    assert result["count"] == 2 and result["source"] == "Yahoo Finance"
    assert result["data"][0] == {"date": "2026-11-17 00:00:00", "open": 1.0, "high": 2.0,
                                 "low": 0.5, "close": 1.5, "volume": 100}
    assert result["data"][1]["open"] is None and result["data"][1]["volume"] is None


def test_history_falls_back_to_asx_when_yahoo_fails(monkeypatch, fake_asx):
    fake_ticker(monkeypatch, history=lambda period, interval: raise_(YFRateLimitError()))
    fake_asx.add(CHART, ok(ASX_CHART))
    result = yahoo_tools.get_ticker_history("CBA", "max", "1wk")
    assert result["source"].startswith("ASX")
    assert "fallback_reason" in result
    # weeks are labelled by their first trading day, like Yahoo
    assert [(r["date"], r["open"], r["high"], r["close"], r["volume"]) for r in result["data"]] == [
        ("2026-11-10 00:00:00", 1.0, 3.0, 2.5, 300),
        ("2026-11-16 00:00:00", 2.5, 2.6, 2.3, 700),
    ]


def test_history_asx_fallback_slices_period(monkeypatch, fake_asx):
    fake_ticker(monkeypatch, history=lambda period, interval: pd.DataFrame())
    fake_asx.add(CHART, ok(ASX_CHART))
    data = yahoo_tools.get_ticker_history("CBA", "5d", "1d")["data"]
    assert len(data) == 4
    assert [r["date"][:10] for r in yahoo_tools.get_ticker_history("CBA", "1d", "1d")["data"]] == ["2026-11-17"]


def test_history_intraday_has_no_fallback(monkeypatch, fake_asx):
    fake_ticker(monkeypatch, history=lambda period, interval: pd.DataFrame())
    with pytest.raises(ToolError, match="No price history"):
        yahoo_tools.get_ticker_history("CBA", "1d", "5m")
    assert fake_asx.calls == []


def test_history_unknown_everywhere_raises_yahoo_error(monkeypatch, fake_asx):
    fake_ticker(monkeypatch, history=lambda period, interval: pd.DataFrame())
    fake_asx.add(CHART, FakeResponse(404, {"error_code": "id-or-code-invalid"}))
    with pytest.raises(ToolError, match="No price history found for ZZZZQ.AX"):
        yahoo_tools.get_ticker_history("ZZZZQ")


# --- news -------------------------------------------------------------------

def test_news_reads_nested_provider(monkeypatch):
    item = {"id": "1", "content": {
        "title": "Headline", "pubDate": "2026-11-18T01:37:02Z", "summary": "S",
        "provider": {"displayName": "Investing.com"},
        "canonicalUrl": {"url": "https://example.com/a"},
        "thumbnail": {"resolutions": [{"url": "https://example.com/t.jpg"}]},
    }}
    fake_ticker(monkeypatch, get_news=lambda count: [item])
    news = yahoo_tools.get_ticker_news("BHP")["news"][0]
    assert news == {"title": "Headline", "publisher": "Investing.com",
                    "link": "https://example.com/a", "published": "2026-11-18T01:37:02Z",
                    "summary": "S", "thumbnail": "https://example.com/t.jpg"}


def test_news_tolerates_missing_fields(monkeypatch):
    fake_ticker(monkeypatch, get_news=lambda count: [{"id": "1", "content": {"title": "T"}}])
    news = yahoo_tools.get_ticker_news("BHP")["news"][0]
    assert news["title"] == "T" and news["publisher"] is None and news["link"] is None


# --- server -----------------------------------------------------------------

def test_server_exposes_tools_with_enum_schemas():
    import server

    listed = asyncio.run(server.mcp.list_tools())
    by_name = {t.name: t for t in listed}
    assert set(by_name) == {
        "search_asx_ticker", "get_asx_ticker_info", "get_asx_ticker_history",
        "get_asx_ticker_news", "get_asx_announcements", "get_asx_announcement_text",
        "get_asx_company_profile",
    }
    props = by_name["get_asx_ticker_history"].parameters["properties"]
    assert "1mo" in props["period"]["enum"]
    assert "1wk" in props["interval"]["enum"]
