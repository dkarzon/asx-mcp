# asx-mcp

A FastMCP server for Australian Stock Exchange (ASX) market data, combining data from the ASX website with Yahoo Finance.

> **Credit:** This project started from [jacklatrobe/ASX_MCP](https://github.com/jacklatrobe/ASX_MCP), a yfinance-based FastMCP server, and has since been heavily modified: it now sources search, quotes, franking, announcements and company profiles from ASX, adds new tools, and keeps Yahoo Finance only for price history and news.

## Overview

asx-mcp is a Model Context Protocol (MCP) server that gives AI assistants access to ASX market data over streamable HTTP. It uses two sources:

| Data | Source |
|---|---|
| Ticker search, quotes, dividends & franking, announcements, company profiles | **ASX** (the data endpoints behind asx.com.au) |
| Price history (incl. intraday), news | **Yahoo Finance**, with ASX as a fallback for daily/weekly/monthly history |

> **Note:** The ASX endpoints are undocumented, not a supported public API, and may change without notice. ASX's [terms of use](https://www.asx.com.au/legals/terms-of-use) restrict automated access and allow personal, non-commercial use only. This project is intended for personal use. ASX prices are delayed by at least 20 minutes.

## Features

- 🔍 **Ticker Search** - Local search of the full ASX company directory, plus ETFs via ASX search
- 📊 **Quotes with Franking** - Price, valuation, latest dividend with franking %, grossed-up yield
- 📢 **ASX Announcements** - Latest official company announcements, with PDF text extraction
- 🏢 **Company Profiles** - Directors, share registry, contact details
- 📈 **Historical Data** - OHLCV data with flexible periods and intervals
- 📰 **Market News** - Recent Yahoo Finance news for tickers
- ⚡ **Rate Limiting** - Per-provider rate limits (ASX 4 req/s, Yahoo 2 req/s)
- 🔄 **Auto-Retry** - Exponential backoff retry logic for resilience
- 💾 **Caching** - Daily directory refresh on disk; in-memory TTL caches per endpoint

## Tools

### 1. `search_asx_ticker`

Search ASX-listed securities (companies and ETFs) by code, company name, or GICS industry group.

**Parameters:**
- `query` (string): Search string, e.g. `"bank"`, `"CBA"`, `"Materials"`
- `limit` (int, optional): Max results to return (default: 10, max: 25)

Results are ranked: exact code, code prefix, name prefix, name words, then industry group; ties are broken by market cap.

### 2. `get_asx_ticker_info`

Quote, valuation and dividend data for an ASX security.

**Parameters:**
- `symbol` (string): ASX code (e.g. `'CBA'` or `'CBA.AX'`)

**Returns:** current price, change, previous close, bid/ask, day and 52-week range, volume, market cap, P/E, EPS, ISIN, sector, GICS industry group, listing date, recent annual revenue/net income, and:

```json
"dividend_yield": 2.96,
"grossed_up_yield": 4.23,
"dividend": {
  "amount_aud": 2.7,
  "type": "Final",
  "franking_percent": 100,
  "declared_currency": "AUD",
  "declared_amount": 2.7,
  "ex_date": "2026-08-19",
  "record_date": "2026-08-20",
  "pay_date": "2026-09-29"
}
```

- Yields are percentages. `grossed_up_yield` adds franking credits at the 30% company tax rate (base-rate entities frank at 25%, so it slightly overstates theirs).
- `dividend` is the **most recent** dividend only; ASX doesn't expose dividend history. It is `null` for non-payers.
- `amount_aud` is in AUD; for dividends declared in another currency (e.g. BHP, CSL in USD), `declared_amount` is the original amount.

### 3. `get_asx_ticker_history`

Historical OHLCV (Open, High, Low, Close, Volume) data for a ticker or index.

**Parameters:**
- `symbol` (string): ASX code
- `period` (string, optional): `1d`, `5d`, `1mo`, `3mo`, `6mo`, `1y`, `2y`, `5y`, `10y`, `ytd`, `max` (default: `1mo`)
- `interval` (string, optional): `1m`, `2m`, `5m`, `15m`, `30m`, `60m`, `90m`, `1h`, `1d`, `5d`, `1wk`, `1mo`, `3mo` (default: `1d`)

Served from Yahoo Finance. If Yahoo fails and the interval is `1d`, `1wk`, `1mo` or `3mo`, falls back to ASX daily data (about 10 years, up to the previous trading day, not adjusted for splits or dividends). The `source` field says which was used.

### 4. `get_asx_ticker_news`

Recent Yahoo Finance news articles for a ticker.

**Parameters:**
- `symbol` (string): ASX code
- `limit` (int, optional): Max news items to return (default: 10, max: 50)

### 5. `get_asx_announcements`

Latest official ASX company announcements, with a price-sensitive flag.

**Parameters:**
- `symbol` (string): ASX code
- `limit` (int, optional): Max announcements (default: 5, max: 5 — ASX only exposes the five most recent)

Each item includes `headline`, `type`, `date` (UTC), `price_sensitive`, `document_key` and `pdf_url`.

### 6. `get_asx_announcement_text`

Extract the text of an announcement PDF.

**Parameters:**
- `document_key` (string): from `get_asx_announcements`
- `offset` (int, optional): Character offset (default: 0)
- `max_chars` (int, optional): Characters to return (default: 20000, max: 50000)

Long documents are chunked: call again with `offset=next_offset` until `next_offset` is `null`. Scanned (image-only) PDFs have no extractable text.

### 7. `get_asx_company_profile`

Company description, website, directors, registered office contact details and share registry.

**Parameters:**
- `symbol` (string): ASX code

## Installation

### Using pip

```bash
pip install -r requirements.txt
```

### Using Docker

```bash
docker build -t asx-mcp .
docker run -p 8000:8000 asx-mcp
```

## Usage

### Running Locally

```bash
python server.py
```

The server will start on HTTP transport at `http://127.0.0.1:8000/mcp`. A liveness endpoint is available at `/health`.

### Running with Docker

```bash
docker run -p 8000:8000 -v $(pwd)/cache:/app/cache asx-mcp
```

Mount a volume to keep the ASX company directory between container restarts.

### Environment Variables

- `CACHE_DIR` - Directory for the ASX company directory cache (default: `/tmp/asx_cache`; `/app/cache` in Docker)
- `HOST` - Bind address (default: `127.0.0.1`; `0.0.0.0` in Docker)
- `PORT` - Port (default: `8000`)
- `LOG_LEVEL` - Logging level (default: `INFO`)

### Running Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock ASX and Yahoo, so they run offline. To check that the live ASX endpoints still behave as expected:

```bash
ASX_LIVE_TESTS=1 pytest tests/test_live.py
```

## Architecture

```
asx-mcp/
├── app/
│   ├── asx_client.py        # ASX HTTP client: all ASX URLs, error mapping
│   ├── asx_directory.py     # Company directory (daily, on disk) + search
│   ├── asx_tools.py         # Info/franking, announcements, PDF text, profile
│   ├── yahoo_tools.py       # History (with ASX fallback) and news
│   ├── rate_limiter.py      # Per-provider rate limiting & retry logic
│   └── ttl_cache.py         # In-memory TTL cache decorator
├── tests/                   # Offline pytest suite + opt-in live smoke tests
├── server.py                # FastMCP server with HTTP transport
├── requirements.txt         # Python dependencies
├── requirements-dev.txt     # Test dependencies
└── Dockerfile               # Container configuration
```

## Technical Details

### Caching

| Data | Cache |
|---|---|
| ASX company directory | 24 hours, on disk in `CACHE_DIR`; a stale copy is used if a refresh fails |
| ASX predictive search | 24 hours, in memory |
| Quote header & key statistics | 15 minutes |
| Announcements list | 5 minutes |
| Company profile | 7 days |
| Announcement PDF text | 30 days (announcements don't change) |

### Rate Limiting and Retries

- Separate thread-safe rate limiters for ASX (4 req/s) and Yahoo (2 req/s), applied to every attempt including retries
- Up to 3 attempts with exponential backoff on network errors, HTTP 5xx/429, and Yahoo rate limiting
- ASX "not found" responses (400/404) are not retried and become clear tool errors

## Dependencies

- **fastmcp** (4.x) - MCP server framework
- **yfinance** (1.x) - Yahoo Finance API
- **pandas** (≥2.2) - History data handling
- **requests** (≥2.31.0) - ASX HTTP client
- **pypdf** (6.x–7.x) - Announcement PDF text extraction
- **tenacity** (9.x) - Retry logic

## License

MIT License - see LICENSE file for details. The original copyright notice from [jacklatrobe/ASX_MCP](https://github.com/jacklatrobe/ASX_MCP) is retained, as the MIT License requires.

## Contributing

Contributions welcome! Please feel free to submit a Pull Request.
