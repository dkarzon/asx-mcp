"""
Small thread-safe in-memory TTL cache decorator.
"""
import threading
import time
from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, List

_registry: List[Callable[[], None]] = []


def ttl_cache(seconds: float, maxsize: int = 256) -> Callable[[Callable], Callable]:
    """
    Cache a function's return value per positional-argument tuple for `seconds`.

    Exceptions are not cached. Least-recently-used entries are evicted beyond
    `maxsize`. Cached values are shared, so callers must not mutate them.
    """
    def decorator(func: Callable) -> Callable:
        store: "OrderedDict[tuple, tuple]" = OrderedDict()
        lock = threading.Lock()

        @wraps(func)
        def wrapper(*args) -> Any:
            with lock:
                hit = store.get(args)
                if hit and time.monotonic() - hit[0] < seconds:
                    store.move_to_end(args)
                    return hit[1]
            value = func(*args)
            with lock:
                store[args] = (time.monotonic(), value)
                store.move_to_end(args)
                while len(store) > maxsize:
                    store.popitem(last=False)
            return value

        def cache_clear() -> None:
            with lock:
                store.clear()

        wrapper.cache_clear = cache_clear
        _registry.append(cache_clear)
        return wrapper
    return decorator


def clear_all_caches() -> None:
    """Clear every ttl_cache in the process (used by tests)."""
    for clear in _registry:
        clear()
