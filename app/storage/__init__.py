"""
app/storage/__init__.py
=======================
Public API for the storage layer.

``get_store(config)`` is the single factory used by the rest of the
application to obtain a fully-initialised store instance.
"""

from __future__ import annotations

from app.config import Settings
from app.storage.base import BaseStore
from app.storage.sqlite_store import SQLiteStore


def get_store(config: Settings) -> BaseStore:
    """
    Return a fully-initialised storage backend for the given configuration.

    Currently the only supported backend is SQLite.  ``create_tables()``
    is called automatically so callers never need to do it themselves.

    Parameters
    ----------
    config:
        The application ``Settings`` instance (typically the module-level
        ``settings`` singleton from ``app.config``).

    Returns
    -------
    BaseStore
        A concrete store implementation ready for use.
    """
    store = SQLiteStore(db_path=config.sqlite_db_path)
    store.create_tables()
    return store


__all__ = ["BaseStore", "SQLiteStore", "get_store"]
