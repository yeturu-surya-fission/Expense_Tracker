from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finance_agent.config import settings
from finance_agent.db.models import Base

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, roll back on any exception."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    if settings.auto_create_tables:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # create_all never alters existing tables: add columns introduced after a database was first created
            await conn.execute(text("ALTER TABLE claims ADD COLUMN IF NOT EXISTS external_no INTEGER"))
            await conn.execute(text("ALTER TABLE claims ADD COLUMN IF NOT EXISTS decided_items JSONB"))
            await conn.execute(text("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS claim_id INTEGER"))
