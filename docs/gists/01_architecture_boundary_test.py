"""Gist 1: make the architecture fail CI instead of living in a README.

The bot (app/) may never import the database or the server; the server (mcp_server/) may never import Telegram or LangChain.
Excerpt from tests/test_architecture.py.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def imports_of(folder: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in (ROOT / folder).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module)
        out[str(path.relative_to(ROOT))] = mods
    return out


def top(mods: set[str]) -> set[str]:
    return {m.split(".")[0] for m in mods}


def test_the_bot_never_imports_the_server_or_a_database_driver():
    forbidden = {"mcp_server", "sqlalchemy", "asyncpg", "alembic"}
    for file, mods in imports_of("app").items():
        assert not (top(mods) & forbidden), f"{file} imports {top(mods) & forbidden}"
