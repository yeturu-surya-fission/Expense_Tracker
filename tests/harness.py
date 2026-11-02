"""
Test harness: runs the real MCP server in-process (ASGI, no sockets) and talks to it only through the official MCP client
over HTTP, exactly like the Telegram bot does. Tokens are minted through the real /auth endpoints.
"""
import asyncio
import contextvars
import itertools
import time
from dataclasses import dataclass, field

import httpx
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

BASE = "http://testserver"
_ids = itertools.count(7_000_000_000 + (int(time.time()) % 1_000_000) * 100)
_current = contextvars.ContextVar("test_token", default="")


class _CtxAuth(httpx2.Auth):
    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {_current.get()}"
        yield request


def new_telegram_id() -> int:
    return next(_ids)


@dataclass
class Person:
    """One person with an open session in one chat context."""
    h: "Harness"
    telegram_id: int
    token: str
    user_id: int
    name: str
    group_chat_id: int | None = None
    answers: dict = field(default_factory=dict)

    async def call(self, tool: str, **args) -> dict:
        return await self.h.call(self.token, tool, **args)

    async def ok(self, tool: str, **args) -> dict:
        r = await self.call(tool, **args)
        assert r["status"] == "ok", r
        return r


class Harness:
    def __init__(self, app):
        self.app = app
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
        self._h2 = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), auth=_CtxAuth(), base_url=BASE)
        self.client: Client | None = None
        self.service = ""
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._error: BaseException | None = None

    async def _run(self) -> None:
        # the app's lifespan and the MCP client both use task groups, which must be entered and left by the SAME task
        try:
            async with self.app.router.lifespan_context(self.app):
                self.service = await self.service_token()
                _current.set(self.service)
                async with Client(streamable_http_client(f"{BASE}/mcp", http_client=self._h2)) as client:
                    self.client = client
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as e:  # surfaced by start()/stop()
            self._error = e
            self._ready.set()

    async def start(self, secret: str) -> None:
        self.secret = secret
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._error:
            raise self._error

    async def stop(self) -> None:
        self._stop.set()
        await self._task
        await self._h2.aclose()
        await self.http.aclose()

    # ---- auth
    async def service_token(self) -> str:
        r = await self.http.post("/auth/token", data={"grant_type": "client_credentials", "client_id": "telegram-bot", "client_secret": self.secret})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    async def session_response(self, telegram_id: int, *, name: str = "Test", chat_type="private", group_chat_id=None,
                               group_title=None, started=True, service: str | None = None) -> httpx.Response:
        body = {"telegram_id": telegram_id, "name": name, "chat_type": chat_type, "started": started, "bot_username": "expense_test_bot"}
        if group_chat_id is not None:
            body["group_chat_id"] = group_chat_id
        if group_title:
            body["group_title"] = group_title
        return await self.http.post("/auth/session", json=body, headers={"Authorization": f"Bearer {self.service if service is None else service}"})

    async def person(self, name: str = "Test", *, telegram_id: int | None = None, group_chat_id: int | None = None,
                     group_title: str | None = None, started: bool = True) -> Person:
        tg = telegram_id or new_telegram_id()
        r = await self.session_response(
            tg, name=name, chat_type="group" if group_chat_id is not None else "private",
            group_chat_id=group_chat_id, group_title=group_title, started=started,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        return Person(self, tg, body["access_token"], body["user_id"], name, group_chat_id)

    # ---- MCP calls
    async def call(self, token: str, tool: str, **args) -> dict:
        _current.set(token)
        res = await self.client.call_tool(tool, args)
        assert res.structured_content is not None, res
        return res.structured_content

    async def list_tools(self, token: str | None = None):
        _current.set(token or self.service)
        return (await self.client.list_tools()).tools

    async def platform(self, tool: str, **args) -> dict:
        return await self.call(self.service, tool, **args)

    # ---- raw MCP (for negative auth tests: no SDK niceties)
    async def raw_mcp(self, token: str | None, method: str = "tools/list", params: dict | None = None, headers: dict | None = None):
        hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
        if token:
            hdr["Authorization"] = f"Bearer {token}"
        hdr.update(headers or {})
        return await self.http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=hdr)
