"""
The boundary that makes the MCP server the single owner of expense data:
the Telegram bot (app/) reaches it ONLY through the MCP client, and the server (mcp_server/) knows nothing about Telegram or LLMs.
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
        out[str(path.relative_to(ROOT)).replace("\\", "/")] = mods
    return out


def top(mods: set[str]) -> set[str]:
    return {m.split(".")[0] for m in mods}


def test_the_bot_never_imports_the_server_or_a_database_driver():
    forbidden = {"mcp_server", "sqlalchemy", "asyncpg", "pypdf", "alembic"}
    for file, mods in imports_of("app").items():
        assert not (top(mods) & forbidden), f"{file} imports {top(mods) & forbidden}"


def test_the_bot_has_no_leftover_business_modules():
    for legacy in ("core", "db", "tools"):
        assert not (ROOT / "app" / legacy).exists(), f"app/{legacy} should have moved to mcp_server"
    for file, mods in imports_of("app").items():
        assert not [m for m in mods if m.startswith(("app.core", "app.db", "app.tools"))], file


def test_only_the_mcp_client_package_speaks_mcp():
    for file, mods in imports_of("app").items():
        if file.startswith("app/mcp_client/"):
            continue
        assert "mcp" not in top(mods) and not [m for m in mods if m.startswith("mcp_types")], f"{file} imports the MCP SDK directly"


def test_the_bot_opens_no_business_database():
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "create_async_engine" not in text and "session_scope" not in text, path.name
    cfg = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "bot_database_url" in cfg and "database_url: str" not in cfg.replace("bot_database_url: str", "")  # only its own conversation store


def test_the_server_knows_nothing_about_telegram_or_the_ai():
    forbidden = {"telegram", "langchain", "langchain_core", "langgraph", "langchain_google_genai", "langchain_openai", "langchain_anthropic",
                 "langchain_groq", "app"}
    for file, mods in imports_of("mcp_server").items():
        assert not (top(mods) & forbidden), f"{file} imports {top(mods) & forbidden}"


def test_only_the_gateway_knows_the_auth_endpoints_and_builds_tokens():
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name != "gateway.py":
            assert "/auth/" not in text, path.name
        assert "jwt.encode" not in text and "import jwt" not in text, f"{path.name} must never mint tokens"


def test_both_services_import_cleanly():
    """The entry points of each service load (catches a broken import before a container does)."""
    import importlib
    for module in ("app.main", "app.bot.app", "app.bot.handlers", "mcp_server.main", "mcp_server.server", "mcp_server.jobs"):
        importlib.import_module(module)


def test_each_service_depends_only_on_its_own_requirements():
    def packages(name: str) -> str:
        lines = (ROOT / name).read_text(encoding="utf-8").lower().splitlines()
        return " ".join(line for line in lines if line.strip() and not line.startswith("#"))

    mcp, bot = packages("requirements-mcp.txt"), packages("requirements-bot.txt")
    assert "telegram" not in mcp and "langchain" not in mcp and "langgraph" not in mcp
    assert "sqlalchemy" not in bot and "asyncpg" not in bot and "pypdf" not in bot and "pyjwt" not in bot
