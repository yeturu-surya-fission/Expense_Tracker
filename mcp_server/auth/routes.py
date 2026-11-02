"""
HTTP endpoints next to the MCP endpoint (not MCP calls):

  POST /auth/token      client_credentials: a first-party client trades its id+secret for a short-lived SERVICE token
  POST /auth/session    (service token) the client vouches for a Telegram person and gets that person's SESSION token
  POST /auth/revoke     (service token) end a session before it expires
  GET  /.well-known/jwks.json   public keys that verify our tokens
  GET  /healthz
"""
import base64
import hmac
import logging
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_server.auth import tokens
from mcp_server.auth.keys import get_keys
from mcp_server.auth.verifier import JwtVerifier
from mcp_server.config import settings
from mcp_server.db.session import session_scope
from mcp_server.domain import access, friends, groups, notices
from mcp_server.tools.registry import audit, rate_limited

log = logging.getLogger(__name__)
_verifier = JwtVerifier()


def _json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _oauth_error(code: str, description: str, status: int) -> JSONResponse:
    return _json({"error": code, "error_description": description}, status)


async def _form_or_json(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}
    return dict(parse_qsl((await request.body()).decode("utf-8", "replace")))  # application/x-www-form-urlencoded


def _client_secrets() -> dict[str, str]:
    return {settings.bot_client_id: settings.bot_client_secret} if settings.bot_client_secret else {}


async def _service_caller(request: Request) -> dict | JSONResponse:
    """The claims of a valid service token in the Authorization header, or the error response to send."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return _oauth_error("invalid_token", "Bearer token required", 401)
    access_token = await _verifier.verify_token(header[7:].strip())
    if access_token is None:
        return _oauth_error("invalid_token", "The token is invalid, expired or revoked", 401)
    if "platform" not in access_token.scopes or (access_token.claims or {}).get("typ") != "service":
        return _oauth_error("insufficient_scope", "A service token with scope 'platform' is required", 403)
    return access_token.claims or {}


async def token_endpoint(request: Request) -> JSONResponse:
    form = await _form_or_json(request)
    if form.get("grant_type") != "client_credentials":
        return _oauth_error("unsupported_grant_type", "Only client_credentials is supported", 400)
    client_id, secret = str(form.get("client_id", "")), str(form.get("client_secret", ""))
    auth = request.headers.get("authorization", "")
    if not client_id and auth.lower().startswith("basic "):
        try:
            client_id, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            pass
    if rate_limited(f"auth:{request.client.host if request.client else '?'}", 60):
        await audit(f"client:{client_id}", None, "auth.token", "rate_limited")
        return _oauth_error("slow_down", "Too many attempts", 429)
    expected = _client_secrets().get(client_id)
    if expected is None or not hmac.compare_digest(expected.encode(), secret.encode()):
        await audit(f"client:{client_id}", None, "auth.token", "denied", "bad client credentials")
        return _oauth_error("invalid_client", "Unknown client or wrong secret", 401)
    token, ttl = tokens.issue_service_token(client_id)
    await audit(f"client:{client_id}", None, "auth.token", "ok")
    return _json({"access_token": token, "token_type": "Bearer", "expires_in": ttl, "scope": "mcp platform"})


async def session_endpoint(request: Request) -> JSONResponse:
    caller = await _service_caller(request)
    if isinstance(caller, JSONResponse):
        return caller
    client_id = caller["client_id"]
    body = await _form_or_json(request)
    try:
        telegram_id = int(body["telegram_id"])
        chat_type = str(body.get("chat_type", "private"))
        group_chat_id = int(body["group_chat_id"]) if body.get("group_chat_id") is not None else None
    except (KeyError, TypeError, ValueError):
        return _oauth_error("invalid_request", "telegram_id (and group_chat_id for group chats) must be numbers", 400)
    if chat_type not in ("private", "group") or (chat_type == "group" and group_chat_id is None):
        return _oauth_error("invalid_request", "chat_type must be 'private', or 'group' with a group_chat_id", 400)
    if rate_limited(f"session:{client_id}", settings.rate_limit_per_minute * 10):
        return _oauth_error("slow_down", "Too many sessions requested", 429)
    if body.get("bot_username"):
        settings.bot_username = str(body["bot_username"])[:64]

    async with session_scope() as s:
        started = bool(body.get("started")) and chat_type == "private"
        before = await friends.upsert_user(s, telegram_id)
        was_started = before.started_at is not None
        user = await friends.upsert_user(s, telegram_id, body.get("name"), body.get("username"), started=started)
        if chat_type == "group":
            group = await groups.group_for_chat(s, group_chat_id)
            if group is None:
                if not access.can_add_bot(telegram_id):
                    await audit(f"user:{user.id}", client_id, "auth.session", "denied", "no group")
                    return _oauth_error("no_group", "This chat is not set up and this person may not set it up", 403)
                group, _ = await groups.ensure_group(s, group_chat_id, body.get("group_title"), user.id)
            await groups.join_group(s, group.id, user.id)  # speaking in a group makes you a member
        elif not await access.allowed(s, telegram_id):
            await audit(f"user:{user.id}", client_id, "auth.session", "denied", "not allowed")
            return _oauth_error("not_allowed", "This person may not use the system", 403)
        if started and not was_started:
            await notices.deliver_pending(s, user.id)  # everything that was waiting for them
        user_id = user.id
    token, ttl, _claims = tokens.issue_session_token(user_id, client_id, chat_type, group_chat_id)
    await audit(f"user:{user_id}", client_id, "auth.session", "ok", chat_type)
    return _json({"access_token": token, "token_type": "Bearer", "expires_in": ttl, "scope": "mcp user", "user_id": user_id})


async def revoke_endpoint(request: Request) -> JSONResponse:
    caller = await _service_caller(request)
    if isinstance(caller, JSONResponse):
        return caller
    body = await _form_or_json(request)
    try:
        claims = tokens.decode(str(body.get("token", "")))
    except tokens.TokenError:
        return _json({})  # RFC 7009: revoking something unknown or already invalid is not an error
    await tokens.revoke(claims["jti"], int(claims["exp"]))
    await audit(str(claims["sub"]), caller.get("client_id"), "auth.revoke", "ok")
    return _json({})


async def jwks_endpoint(request: Request) -> JSONResponse:
    return JSONResponse(get_keys().jwks(), headers={"Cache-Control": "public, max-age=300"})


async def health_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def register_auth_routes(mcp) -> None:
    mcp.custom_route("/auth/token", methods=["POST"])(token_endpoint)
    mcp.custom_route("/auth/session", methods=["POST"])(session_endpoint)
    mcp.custom_route("/auth/revoke", methods=["POST"])(revoke_endpoint)
    mcp.custom_route("/.well-known/jwks.json", methods=["GET"])(jwks_endpoint)
    mcp.custom_route("/healthz", methods=["GET"])(health_endpoint)
