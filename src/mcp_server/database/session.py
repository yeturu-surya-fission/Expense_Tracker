"""Database session for MCP server.

Provides lazy access to the shared database session. The engine is not
created at import time to avoid requiring a live database connection
during module loading.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import AsyncContextManager


@asynccontextmanager
async def session_scope() -> AsyncIterator:
    """Delegated async context manager around app.db.session.session_scope."""
    from backend.db.session import session_scope as _session_scope
    async with _session_scope() as s:
        yield s


def get_session_scope():
    from backend.db.session import session_scope
    return session_scope


def get_engine():
    from backend.db.session import engine
    return engine


def get_init_db():
    from backend.db.session import init_db
    return init_db


__all__ = ["session_scope", "get_session_scope", "get_engine", "get_init_db"]
