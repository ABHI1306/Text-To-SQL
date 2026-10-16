"""Shared connection pools for warehouse adapters, one per database address.

Adapters are built for every request, so pooling has to live outside them. ``psycopg_pool`` is
optional: when it is not installed (or the pool size is 0) callers connect directly, as before.
"""

from __future__ import annotations

import atexit
from threading import Lock
from typing import Any

_pools: dict[str, Any] = {}
_lock = Lock()


def get_pool(dsn: str, max_size: int) -> Any | None:
    """The shared pool for ``dsn``, or None when pooling is off or unavailable."""
    if max_size <= 0:
        return None
    try:
        from psycopg_pool import ConnectionPool
    except ImportError:
        return None
    with _lock:
        pool = _pools.get(dsn)
        if pool is None:
            # Connections are checked, recycled and never held across requests.
            pool = ConnectionPool(
                dsn, min_size=0, max_size=max_size, max_idle=300, open=True,
                check=ConnectionPool.check_connection,
            )
            _pools[dsn] = pool
        return pool


def close_pools() -> None:
    with _lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()


# Pool worker threads are not daemons; close them at exit so scripts and the worker stop at once.
atexit.register(close_pools)
