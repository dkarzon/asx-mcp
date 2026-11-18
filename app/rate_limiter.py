"""
Rate limiting and retry utilities for API calls.
"""
import logging
import threading
import time
from functools import wraps
from typing import Callable, Any

import requests
from curl_cffi.curl import CurlError
from tenacity import (
    before_sleep_log,
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from yfinance.exceptions import YFRateLimitError

logger = logging.getLogger(__name__)

# Exceptions worth retrying. yfinance makes its HTTP calls through curl_cffi
# (whose exceptions derive from CurlError, not from requests), and raises
# YFRateLimitError when Yahoo throttles us.
RETRYABLE_EXCEPTIONS = (
    YFRateLimitError,
    CurlError,
    requests.exceptions.RequestException,
    ConnectionError,
    TimeoutError,
)


class RateLimiter:
    """Thread-safe rate limiter to prevent overwhelming the API."""

    def __init__(self, calls_per_second: float = 2.0):
        """
        Initialize rate limiter.

        Args:
            calls_per_second: Maximum number of calls allowed per second
        """
        self.calls_per_second = calls_per_second
        self.min_interval = 1.0 / calls_per_second
        self.last_call_time = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Wait if necessary to maintain rate limit."""
        # Hold the lock while sleeping so concurrent callers are serialised
        # rather than all waking up together.
        with self._lock:
            elapsed = time.monotonic() - self.last_call_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last_call_time = time.monotonic()


# One limiter per upstream provider so a burst against one doesn't slow the other.
yahoo_limiter = RateLimiter(calls_per_second=2.0)
asx_limiter = RateLimiter(calls_per_second=4.0)


def with_rate_limit(limiter: RateLimiter) -> Callable[[Callable], Callable]:
    """Decorator factory applying the given rate limiter to a function."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            limiter.wait()
            return func(*args, **kwargs)
        return wrapper
    return decorator


def with_retry(func: Callable) -> Callable:
    """
    Decorator to add retry logic with exponential backoff.
    Retries on network errors, HTTP errors and Yahoo rate limit errors.
    """
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def rate_limited_retry(func: Callable) -> Callable:
    """
    Decorator combining Yahoo rate limiting and retry logic.

    Rate limiting is applied inside the retry loop so that every attempt,
    including retries, is throttled.
    """
    return with_retry(with_rate_limit(yahoo_limiter)(func))


def asx_rate_limited_retry(func: Callable) -> Callable:
    """Decorator combining ASX rate limiting and retry logic."""
    return with_retry(with_rate_limit(asx_limiter)(func))
