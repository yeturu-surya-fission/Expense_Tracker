"""The same conversation over real sockets (uvicorn on localhost), not the in-process shortcut: Host/Origin checks, headers, JSON bodies."""
import asyncio
import socket

import pytest
import uvicorn

from app.mcp_client.gateway import McpGateway
from app.mcp_client.types import Identity, McpError


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live_server():
    from mcp_server.server import create_app

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    yield port
    server.should_exit = True
    await task


async def test_gateway_over_real_http(live_server):
    port = live_server
    gw = McpGateway(f"http://127.0.0.1:{port}/mcp", f"http://127.0.0.1:{port}", "telegram-bot", "test-bot-secret")
    me = Identity(telegram_id=7_999_000_001, name="Kiran", started=True)
    try:
        defs = await gw.list_tool_definitions()
        assert any(t["name"] == "add_expenses" for t in defs)
        res = await gw.call(me, "add_expenses", {"items": [{"amount": 42, "category": "food", "description": "real http tea"}]})
        assert res.ok and "₹42" in res.text
        listed = await gw.call(me, "get_expenses")
        assert "real http tea" in listed.text
        ask = await gw.call(me, "split_expense", {"amount": 100, "description": "x", "with_friends": ["nobody"]})
        assert ask.status == "rejected"  # unknown friend: a normal refusal travels over HTTP too
        platform = await gw.platform("platform_outbox_claim", {"limit": 1})
        assert platform.ok
        await asyncio.gather(*(gw.call(me, "list_friends") for _ in range(10)))  # concurrent calls on one connection pool
    finally:
        await gw.close()


async def test_real_http_rejects_a_wrong_secret_and_a_missing_server(live_server):
    port = live_server
    bad = McpGateway(f"http://127.0.0.1:{port}/mcp", f"http://127.0.0.1:{port}", "telegram-bot", "wrong")
    with pytest.raises(McpError):
        await bad.call(Identity(telegram_id=1), "list_friends")
    await bad.close()
    gone = McpGateway("http://127.0.0.1:9/mcp", "http://127.0.0.1:9", "telegram-bot", "x", timeout=2)
    with pytest.raises(McpError):
        await gone.call(Identity(telegram_id=1), "list_friends")
    await gone.close()
