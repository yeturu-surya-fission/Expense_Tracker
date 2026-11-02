"""The Alembic migrations create exactly the schema the models describe (what production runs instead of create_all)."""
import asyncio
import os
import uuid

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from mcp_server.config import settings

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _admin_dsn(url) -> dict:
    return dict(host=url.host, port=url.port or 5432, user=url.username, password=url.password, database="postgres")


async def test_migrations_build_the_same_schema_as_the_models(monkeypatch):
    base = make_url(settings.database_url)
    name = f"alembic_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(**_admin_dsn(base))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        monkeypatch.setattr(settings, "database_url", base.set(database=name).render_as_string(hide_password=False))
        cfg = Config(os.path.join(ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(ROOT, "mcp_server", "migrations"))
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.check, cfg)  # raises if the models and the migrations disagree
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
