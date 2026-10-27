from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base

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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all never alters existing tables: add columns introduced after a database was first created
        for ddl in (
            "ALTER TABLE shared_expenses ADD COLUMN IF NOT EXISTS bundle_id VARCHAR(32)",
            "ALTER TABLE debts ADD COLUMN IF NOT EXISTS bundle_id VARCHAR(32)",
            "ALTER TABLE group_members ADD COLUMN IF NOT EXISTS left_at TIMESTAMPTZ",
            "ALTER TABLE debts ADD COLUMN IF NOT EXISTS detail VARCHAR(500)",
            "CREATE INDEX IF NOT EXISTS ix_shared_expenses_bundle_id ON shared_expenses (bundle_id)",
            "CREATE INDEX IF NOT EXISTS ix_debts_bundle_id ON debts (bundle_id)",
        ):
            await conn.execute(text(ddl))
