"""Phase-10 drift guard: mcp_server/core must stay identical to backend/core.

The copies exist so each deploy unit stays self-contained; this test is the tripwire that
makes drift loud the moment someone edits one side without the other.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
BACKEND_CORE = SRC / "backend" / "core"
MCP_CORE = SRC / "mcp_server" / "core"


def _py_files(root: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8-sig") for p in sorted(root.rglob("*.py"))}  # -sig: BOMs are noise


def test_core_file_lists_match():
    backend, mcp = _py_files(BACKEND_CORE), _py_files(MCP_CORE)
    assert set(backend) == set(mcp), (
        f"core trees diverged: only in backend/core: {sorted(set(backend) - set(mcp))}; "
        f"only in mcp_server/core: {sorted(set(mcp) - set(backend))}")


def _normalized(root: Path) -> dict[str, str]:
    """Files with known-equivalent differences neutralized:
    - both package prefixes (self-imports), and
    - mcp_server.database.session (the server's own session wrapper) == backend.db.session."""
    return {name: text.replace("from backend.", "from PKG.")
                      .replace("from mcp_server.", "from PKG.")
                      .replace("from PKG.database.session", "from PKG.db.session")
            for name, text in _py_files(root).items()}


def test_core_contents_match():
    backend, mcp = _normalized(BACKEND_CORE), _normalized(MCP_CORE)
    drifted = [name for name in backend if backend[name] != mcp[name]]
    assert not drifted, f"these core modules drifted between the copies: {drifted}"