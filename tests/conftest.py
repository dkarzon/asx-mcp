import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import asx_client, asx_directory, rate_limiter, yahoo_tools  # noqa: E402
from app.ttl_cache import clear_all_caches  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point caches at a temp dir, reset singletons/caches, and remove delays."""
    monkeypatch.setattr(asx_directory, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(asx_directory, "_directory", None)
    for limiter in (rate_limiter.yahoo_limiter, rate_limiter.asx_limiter):
        monkeypatch.setattr(limiter, "min_interval", 0.0)
    for fn in (asx_client._get, yahoo_tools._yahoo_history, yahoo_tools.get_ticker_news):
        monkeypatch.setattr(fn.retry, "wait", wait_none())
    clear_all_caches()
    yield
    clear_all_caches()


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", content=b""):
        self.status_code = status_code
        self._json = json_body
        self.text = text
        self.content = content
        self.encoding = "utf-8"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeAsx:
    """
    Stand-in for the ASX HTTP session. `routes` maps a URL path (e.g.
    '/asx-research/1.0/companies/CBA/header') to a FakeResponse or a list of
    them (served in order, last one repeated).
    """

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, path, *responses):
        self.routes[path] = list(responses)

    def get(self, url, params=None, timeout=None):
        path = urlparse(url).path
        self.calls.append(path)
        if path not in self.routes:
            return FakeResponse(404, {"error": {"code": 404}})
        queue = self.routes[path]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def count(self, path):
        return self.calls.count(path)


@pytest.fixture
def fake_asx(monkeypatch):
    fake = FakeAsx()
    monkeypatch.setattr(asx_client._session, "get", fake.get)
    return fake
