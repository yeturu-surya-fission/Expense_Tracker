"""
The boundary that makes the MCP server the single owner of expense data:
the Telegram bot (app/) reaches it ONLY through the MCP client, and the server (mcp_server/) knows nothing about Telegram or LLMs.
"""

import ast
from pathlib import Path
import tomllib

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
        assert not (ROOT / "app" / legacy).exists(), (
            f"app/{legacy} should have moved to mcp_server"
        )
    for file, mods in imports_of("app").items():
        assert not [
            m for m in mods if m.startswith(("app.core", "app.db", "app.tools"))
        ], file


def test_only_the_mcp_client_package_speaks_mcp():
    for file, mods in imports_of("app").items():
        if file.startswith("app/mcp_client/"):
            continue
        assert "mcp" not in top(mods) and not [
            m for m in mods if m.startswith("mcp_types")
        ], f"{file} imports the MCP SDK directly"


def test_the_bot_opens_no_business_database():
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "create_async_engine" not in text and "session_scope" not in text, (
            path.name
        )
    cfg = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "bot_database_url" in cfg and "database_url: str" not in cfg.replace(
        "bot_database_url: str", ""
    )  # only its own conversation store


def test_the_server_knows_nothing_about_telegram_or_the_ai():
    forbidden = {
        "telegram",
        "langchain",
        "langchain_core",
        "langgraph",
        "langchain_google_genai",
        "langchain_openai",
        "langchain_anthropic",
        "langchain_groq",
        "app",
    }
    for file, mods in imports_of("mcp_server").items():
        assert not (top(mods) & forbidden), f"{file} imports {top(mods) & forbidden}"


def test_only_the_gateway_knows_the_auth_endpoints_and_builds_tokens():
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name != "gateway.py":
            assert "/auth/" not in text, path.name
        assert "jwt.encode" not in text and "import jwt" not in text, (
            f"{path.name} must never mint tokens"
        )


def test_both_services_import_cleanly():
    """The entry points of each service load (catches a broken import before a container does)."""
    import importlib

    for module in (
        "app.main",
        "app.bot.app",
        "app.bot.handlers",
        "mcp_server.main",
        "mcp_server.server",
        "mcp_server.jobs",
    ):
        importlib.import_module(module)


def _dependencies_of(group: str) -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    groups = pyproject.get("dependency-groups", {})
    return " ".join(groups.get(group, [])).lower()


def test_each_service_depends_only_on_its_own_requirements():
    mcp, bot = _dependencies_of("mcp"), _dependencies_of("bot")
    assert "telegram" not in mcp and "langchain" not in mcp and "langgraph" not in mcp
    assert (
        "sqlalchemy" not in bot
        and "asyncpg" not in bot
        and "pypdf" not in bot
        and "pyjwt" not in bot
    )


# ------------------------------------------------------------------ the Finance Agent is a third, separate service
def test_the_finance_agent_shares_no_code_with_the_other_two():
    for file, mods in imports_of("finance_agent").items():
        assert not [m for m in mods if m.split(".")[0] in ("app", "mcp_server")], (
            f"{file} imports the expense tracker's code"
        )


def test_the_expense_tracker_never_imports_the_finance_agents_code():
    for folder in ("app", "mcp_server"):
        for file, mods in imports_of(folder).items():
            assert "finance_agent" not in top(mods), (
                f"{file} imports the Finance Agent: they may only talk over A2A"
            )


def test_only_the_reimbursement_package_speaks_a2a_on_the_tracker_side():
    for file, mods in imports_of("mcp_server").items():
        if not file.startswith("mcp_server/reimbursement/"):
            assert "a2a" not in top(mods), f"{file} imports the A2A SDK directly"
    for file, mods in imports_of("app").items():
        assert "a2a" not in top(mods), f"{file}: the bot never speaks A2A"


def test_the_finance_agent_has_its_own_requirements_and_image():
    finance = _dependencies_of("finance")
    assert (
        "a2a-sdk" in finance
        and "langchain" not in finance
        and "langgraph" not in finance
    )
    assert (ROOT / "Dockerfile.finance").exists()
    assert "finance_agent" in (ROOT / "Dockerfile.finance").read_text(encoding="utf-8")
    bot = _dependencies_of("bot")
    assert "a2a" not in bot


def test_the_finance_agent_loads_cleanly():
    import importlib

    for module in (
        "finance_agent.main",
        "finance_agent.manager.bot",
        "finance_agent.a2a.notify",
    ):
        importlib.import_module(module)
