"""
The bot's only door to expense data: HTTP requests to the MCP server.

  * a service token (client_credentials) proves who the bot is; a session token per person+chat proves who it acts for
  * both are cached until shortly before they expire, and re-issued when the server says 401
  * every tool call carries its own Authorization header (the protocol is stateless: no session lives on the server)
  * the bot never builds a token itself: the server issues them, the bot can only ask
"""
import asyncio
import contextvars
import logging
import time
from typing import Any

import httpx
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from app.mcp_client.schema import llm_tool
from app.mcp_client.types import Identity, McpError, NoGroup, NotAllowed, ToolResult

log = logging.getLogger(__name__)
PROTOCOL_VERSION = "2026-07-28"
EXPIRY_SKEW = 60  # renew a token this many seconds before it expires
_token: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_bearer", default="")


class _BearerFromContext(httpx2.Auth):
    """Puts the bearer token of THIS call on the request (a ContextVar, so concurrent calls never mix tokens)."""

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {_token.get()}"
        yield request


class McpGateway:
    def __init__(
        self, mcp_url: str, auth_url: str, client_id: str, client_secret: str, *, timeout: float = 30.0, asgi_app: Any = None,
    ):
        self.mcp_url, self.auth_url = mcp_url, auth_url.rstrip("/")
        self.client_id, self.client_secret, self.timeout = client_id, client_secret, timeout
        auth_kwargs: dict[str, Any] = {"transport": httpx.ASGITransport(app=asgi_app)} if asgi_app is not None else {}
        mcp_kwargs: dict[str, Any] = {"transport": httpx2.ASGITransport(app=asgi_app)} if asgi_app is not None else {}
        self._auth_http = httpx.AsyncClient(timeout=timeout, **auth_kwargs)
        self._rejected: set[str] = set()  # bearer tokens the server answered 401 to (the SDK hides the HTTP status)
        self._mcp_http = httpx2.AsyncClient(
            timeout=httpx2.Timeout(timeout), auth=_BearerFromContext(), event_hooks={"response": [self._note_response]}, **mcp_kwargs,
        )
        self._service: tuple[str, float] | None = None
        self._sessions: dict[tuple, tuple[str, float]] = {}
        self._service_lock = asyncio.Lock()

    async def _note_response(self, response) -> None:
        if response.status_code == 401:
            self._rejected.add(response.request.headers.get("authorization", "")[7:])

    async def close(self) -> None:
        await self._auth_http.aclose()
        await self._mcp_http.aclose()

    # ------------------------------------------------------------------ tokens
    async def _service_token(self, force: bool = False) -> str:
        async with self._service_lock:
            if not force and self._service and self._service[1] > time.monotonic():
                return self._service[0]
            try:
                r = await self._auth_http.post(f"{self.auth_url}/auth/token", data={
                    "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret,
                })
            except httpx.HTTPError as e:
                raise McpError(f"cannot reach the MCP server: {type(e).__name__}") from e
            if r.status_code != 200:
                raise McpError(f"the MCP server refused the bot's credentials ({r.status_code}): check MCP_BOT_CLIENT_SECRET")
            body = r.json()
            self._service = (body["access_token"], time.monotonic() + body["expires_in"] - EXPIRY_SKEW)
            return self._service[0]

    async def session_token(self, ident: Identity, force: bool = False) -> str:
        hit = self._sessions.get(ident.key)
        if not force and hit and hit[1] > time.monotonic():
            return hit[0]
        for attempt in (1, 2):
            service = await self._service_token(force=attempt == 2)
            body = {
                "telegram_id": ident.telegram_id, "name": ident.name, "username": ident.username, "chat_type": ident.chat_type,
                "group_chat_id": ident.group_chat_id, "group_title": ident.group_title, "started": ident.started,
                "bot_username": _bot_username(),
            }
            try:
                r = await self._auth_http.post(f"{self.auth_url}/auth/session", json=body, headers={"Authorization": f"Bearer {service}"})
            except httpx.HTTPError as e:
                raise McpError(f"cannot reach the MCP server: {type(e).__name__}") from e
            if r.status_code == 401 and attempt == 1:
                continue  # our service token went stale: get a new one and retry once
            break
        if r.status_code == 403:
            code = (r.json() or {}).get("error")
            raise (NoGroup() if code == "no_group" else NotAllowed())
        if r.status_code != 200:
            raise McpError(f"could not open a session ({r.status_code})")
        out = r.json()
        self._sessions[ident.key] = (out["access_token"], time.monotonic() + out["expires_in"] - EXPIRY_SKEW)
        return out["access_token"]

    def forget_sessions(self, telegram_id: int) -> None:
        for key in [k for k in self._sessions if k[0] == telegram_id]:
            del self._sessions[key]

    # ------------------------------------------------------------------ calls
    async def _invoke(self, bearer: str, tool: str, args: dict) -> ToolResult:
        _token.set(bearer)
        try:
            async with Client(
                streamable_http_client(self.mcp_url, http_client=self._mcp_http, terminate_on_close=False), mode=PROTOCOL_VERSION,
            ) as client:
                res = await asyncio.wait_for(client.call_tool(tool, args), self.timeout)
        except asyncio.TimeoutError as e:
            raise McpError("the MCP server took too long to answer") from e
        except Exception as e:
            if bearer in self._rejected:
                self._rejected.discard(bearer)
                raise _Unauthorized() from e
            raise McpError(f"MCP call failed: {type(e).__name__}: {_leaf(e)}") from e
        data = res.structured_content
        if data is None or "status" not in data:
            raise McpError(f"{tool} returned an unexpected result")
        return ToolResult(
            status=data["status"], text=data.get("text") or "", data=data.get("data") or {},
            input_requests=data.get("input_requests") or [], error=data.get("error"),
        )

    async def call(
        self, ident: Identity, tool: str, args: dict | None = None, *, answers: dict | None = None,
        idempotency_key: str | None = None, member_count: int | None = None,
    ) -> ToolResult:
        """Call a user tool as `ident`. Protocol arguments are added here, never by the LLM."""
        payload = dict(args or {})
        if answers:
            payload["answers"] = answers
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if member_count is not None:
            payload["member_count"] = member_count
        for attempt in (1, 2):
            token = await self.session_token(ident, force=attempt == 2)
            try:
                return await self._invoke(token, tool, payload)
            except _Unauthorized:
                self._sessions.pop(ident.key, None)
                if attempt == 2:
                    raise McpError("the MCP server rejected the session token twice") from None
        raise AssertionError("unreachable")

    async def platform(self, tool: str, args: dict | None = None) -> ToolResult:
        """Call a platform tool with the bot's own service token."""
        for attempt in (1, 2):
            token = await self._service_token(force=attempt == 2)
            try:
                return await self._invoke(token, tool, dict(args or {}))
            except _Unauthorized:
                self._service = None
                if attempt == 2:
                    raise McpError("the MCP server rejected the service token twice") from None
        raise AssertionError("unreachable")

    async def list_tool_definitions(self) -> list[dict]:
        """Tool definitions for the LLM, read from the server (so the server stays the single source of truth)."""
        token = await self._service_token()
        _token.set(token)
        try:
            async with Client(
                streamable_http_client(self.mcp_url, http_client=self._mcp_http, terminate_on_close=False), mode=PROTOCOL_VERSION,
            ) as client:
                tools = (await asyncio.wait_for(client.list_tools(), self.timeout)).tools
        except Exception as e:
            raise McpError(f"could not list the MCP tools: {type(e).__name__}: {e}") from e
        return [llm_tool(t.name, t.description or "", getattr(t, "input_schema", None) or getattr(t, "inputSchema")) for t in tools if not t.name.startswith("platform_")]


class _Unauthorized(Exception):
    pass


def _leaf(exc: BaseException) -> str:
    """The innermost message of an ExceptionGroup (the SDK runs its transport in a task group)."""
    while getattr(exc, "exceptions", None):
        exc = exc.exceptions[0]
    return str(exc)


def _bot_username() -> str | None:
    from app.config import settings  # late: the username is learned from Telegram after start-up
    return settings.bot_username or None
