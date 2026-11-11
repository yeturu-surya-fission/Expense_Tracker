"""uvicorn mcp_server.main:app  (use `python -m mcp_server` on Windows: psycopg-free, but keeps one entry point)."""
import logging

from mcp_server.config import settings
from mcp_server.server import create_app

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
