"""Authentication and authorization of the MCP server: every request is verified, nothing is trusted from arguments."""
import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from mcp_server.auth import tokens
from mcp_server.auth.keys import get_keys
from mcp_server.config import settings
from mcp_server.db.models import AuditLog
from mcp_server.db.session import session_scope
from mcp_server.tools import registry
from tests.harness import new_telegram_id


def forge(claims: dict, key=None, kid=None, alg="RS256") -> str:
    """A token like ours, but with chosen claims / key (for attack tests)."""
    now = int(time.time())
    body = {"iss": settings.issuer, "aud": settings.resource_url, "iat": now, "nbf": now, "exp": now + 600, "jti": f"forged{now}{id(claims)}",
            "sub": "user:1", "uid": 1, "scope": "mcp user", "typ": "session", "client_id": "telegram-bot", **claims}
    keys = get_keys().current
    return jwt.encode(body, key or keys.private_pem, algorithm=alg, headers={"kid": kid or keys.kid})


# ------------------------------------------------------------------ getting tokens
async def test_service_token_needs_the_right_secret(harness):
    ok = await harness.http.post("/auth/token", data={"grant_type": "client_credentials", "client_id": "telegram-bot", "client_secret": "test-bot-secret"})
    assert ok.status_code == 200 and ok.json()["token_type"] == "Bearer" and ok.json()["expires_in"] == settings.service_token_ttl_minutes * 60
    claims = tokens.decode(ok.json()["access_token"])
    assert claims["scope"] == "mcp platform" and claims["aud"] == settings.resource_url and claims["sub"] == "client:telegram-bot"
    assert (await harness.http.post("/auth/token", data={"grant_type": "client_credentials", "client_id": "telegram-bot", "client_secret": "nope"})).status_code == 401
    assert (await harness.http.post("/auth/token", data={"grant_type": "client_credentials", "client_id": "someone-else", "client_secret": "test-bot-secret"})).status_code == 401
    assert (await harness.http.post("/auth/token", data={"grant_type": "password", "client_id": "telegram-bot", "client_secret": "test-bot-secret"})).status_code == 400


async def test_client_credentials_via_basic_auth(harness):
    basic = base64.b64encode(b"telegram-bot:test-bot-secret").decode()
    r = await harness.http.post("/auth/token", data={"grant_type": "client_credentials"}, headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200


async def test_session_token_is_short_lived_user_scoped_and_audience_bound(harness):
    p = await harness.person("Kiran")
    claims = tokens.decode(p.token)
    assert claims["sub"] == f"user:{p.user_id}" and claims["uid"] == p.user_id
    assert claims["scope"] == "mcp user" and claims["act"] == {"sub": "client:telegram-bot"}
    assert claims["aud"] == settings.resource_url and claims["exp"] - claims["iat"] == settings.session_token_ttl_minutes * 60
    assert claims["chat_type"] == "private"


async def test_only_a_service_token_can_open_sessions(harness):
    user = await harness.person()
    for bad in (user.token, "garbage", ""):
        r = await harness.session_response(new_telegram_id(), service=bad)
        assert r.status_code in (401, 403), r.text
    assert (await harness.session_response(new_telegram_id(), service=user.token)).status_code == 403  # a user token is not a service token


async def test_session_request_is_validated(harness):
    r = await harness.http.post("/auth/session", json={"telegram_id": "abc"}, headers={"Authorization": f"Bearer {harness.service}"})
    assert r.status_code == 400
    r = await harness.http.post("/auth/session", json={"telegram_id": 5, "chat_type": "group"}, headers={"Authorization": f"Bearer {harness.service}"})
    assert r.status_code == 400  # a group session needs the group chat id


# ------------------------------------------------------------------ the MCP endpoint rejects bad tokens
async def test_no_token_is_401_with_discovery_hint(harness):
    r = await harness.raw_mcp(None)
    assert r.status_code == 401
    assert "bearer" in r.headers.get("www-authenticate", "").lower()


@pytest.mark.parametrize("make", ["tampered", "expired", "wrong_audience", "wrong_issuer", "foreign_key", "alg_none", "unknown_kid", "no_exp"])
async def test_forged_or_invalid_tokens_are_rejected(harness, make):
    good = (await harness.person()).token
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    foreign_pem = foreign.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    now = int(time.time())
    token = {
        "tampered": good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB"),
        "expired": forge({"iat": now - 4000, "nbf": now - 4000, "exp": now - 3000}),
        "wrong_audience": forge({"aud": "https://other.example/mcp"}),
        "wrong_issuer": forge({"iss": "https://evil.example"}),
        "foreign_key": forge({}, key=foreign_pem),  # right kid, signed by someone else
        "alg_none": base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=") + "." + good.split(".")[1] + ".",
        "unknown_kid": forge({}, kid="not-a-real-key"),
        "no_exp": jwt.encode({"iss": settings.issuer, "aud": settings.resource_url, "sub": "user:1", "jti": "x", "iat": now}, get_keys().current.private_pem, algorithm="RS256", headers={"kid": get_keys().current.kid}),
    }[make]
    r = await harness.raw_mcp(token)
    assert r.status_code == 401, (make, r.status_code, r.text[:200])


async def test_revoked_token_stops_working_immediately(harness):
    p = await harness.person()
    assert (await p.call("list_friends"))["status"] == "ok"
    r = await harness.http.post("/auth/revoke", json={"token": p.token}, headers={"Authorization": f"Bearer {harness.service}"})
    assert r.status_code == 200
    assert (await harness.raw_mcp(p.token)).status_code == 401


async def test_a_token_that_was_never_valid_cannot_be_used_to_call_tools(harness):
    r = await harness.raw_mcp(forge({"uid": 99999999, "sub": "user:99999999"}, kid="zzz"), "tools/call", {"name": "list_friends", "arguments": {}})
    assert r.status_code == 401


# ------------------------------------------------------------------ scopes and chat context
async def test_service_token_cannot_use_user_tools(harness):
    r = await harness.call(harness.service, "add_expenses", items=[{"amount": 1, "category": "food", "description": "x"}])
    assert r["status"] == "error" and r["error"]["code"] == "insufficient_scope"


async def test_user_token_cannot_use_platform_tools(harness, alice):
    r = await alice.call("platform_outbox_claim")
    assert r["status"] == "error" and r["error"]["code"] == "insufficient_scope"
    r = await alice.call("platform_group_event", event="title_changed", chat_id=1, title="x")
    assert r["error"]["code"] == "insufficient_scope"


async def test_private_and_group_tools_are_kept_apart(harness):
    private = await harness.person()
    group = await harness.person(group_chat_id=-100_000 - new_telegram_id() % 10**6, group_title="Trip")
    r = await group.call("add_expenses", items=[{"amount": 5, "category": "food", "description": "x"}])
    assert r["status"] == "error" and r["error"]["code"] == "not_available_here"  # personal money never in a group
    r = await private.call("add_group_expense", amount=5, description="x")
    assert r["status"] == "error" and r["error"]["code"] == "not_available_here"


async def test_tools_never_take_an_identity_argument(harness):
    """Who is calling comes from the verified token only: no tool lets a caller name another user."""
    forbidden = {"user_id", "uid", "telegram_id_of_caller", "chat_type", "group_chat_id", "subject"}
    for t in await harness.list_tools():
        props = set((t.input_schema if hasattr(t, "input_schema") else t.inputSchema).get("properties", {}))
        if t.name.startswith("platform_"):
            continue
        assert not (props & forbidden), (t.name, props & forbidden)


async def test_user_tools_work_on_the_callers_own_data_only(harness):
    a, b = await harness.person("A"), await harness.person("B")
    await a.ok("add_expenses", items=[{"amount": 111, "category": "food", "description": "a-lunch"}])
    assert "a-lunch" in (await a.ok("get_expenses"))["text"]
    assert "a-lunch" not in (await b.ok("get_expenses"))["text"]


# ------------------------------------------------------------------ who may use the system
async def test_allow_list_is_enforced_by_the_server(harness, monkeypatch):
    owner, stranger = new_telegram_id(), new_telegram_id()
    monkeypatch.setattr(settings, "allowed_telegram_ids", str(owner))
    assert (await harness.session_response(owner)).status_code == 200
    r = await harness.session_response(stranger)
    assert r.status_code == 403 and r.json()["error"] == "not_allowed"
    # once the owner adds the stranger as a friend, they may use it
    me = await harness.person(telegram_id=owner)
    await me.ok("add_friend", nickname="Stranger", telegram_id=stranger)
    assert (await harness.session_response(stranger)).status_code == 200


async def test_only_owners_can_set_up_a_group(harness, monkeypatch):
    owner, stranger = new_telegram_id(), new_telegram_id()
    chat = -200_000 - new_telegram_id() % 10**6
    monkeypatch.setattr(settings, "allowed_telegram_ids", str(owner))
    r = await harness.session_response(stranger, chat_type="group", group_chat_id=chat, group_title="Nope")
    assert r.status_code == 403 and r.json()["error"] == "no_group"
    assert (await harness.session_response(owner, chat_type="group", group_chat_id=chat, group_title="Trip")).status_code == 200
    # the stranger can talk in a group the owner set up (everyone in the group may use the bot)
    assert (await harness.session_response(stranger, chat_type="group", group_chat_id=chat)).status_code == 200


# ------------------------------------------------------------------ transport
async def test_jwks_publishes_the_signing_key(harness):
    jwks = (await harness.http.get("/.well-known/jwks.json")).json()
    kid = jwt.get_unverified_header((await harness.person()).token)["kid"]
    assert any(k["kid"] == kid and k["kty"] == "RSA" and "d" not in k for k in jwks["keys"])


async def test_browser_origin_is_validated(harness):
    token = (await harness.person()).token
    assert (await harness.raw_mcp(token, headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await harness.raw_mcp(token)).status_code != 403  # no Origin header (a server-to-server client) is fine


async def test_wrong_host_header_is_rejected(harness):
    token = (await harness.person()).token
    r = await harness.raw_mcp(token, headers={"Host": "evil.example"})
    assert r.status_code in (400, 421)


async def test_rate_limit_per_caller(harness, monkeypatch):
    p = await harness.person()
    registry.reset_rate_limits()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    results = [(await p.call("list_friends"))["status"] for _ in range(5)]
    assert results[:3] == ["ok"] * 3 and results[3] == "error"
    assert (await p.call("list_friends"))["error"]["code"] == "rate_limited"
    registry.reset_rate_limits()


async def test_audit_log_records_calls_without_secrets(harness):
    p = await harness.person()
    await p.ok("list_friends")
    async with session_scope() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.subject == f"user:{p.user_id}"))).scalars().all()
    actions = {r.action for r in rows}
    assert {"auth.session", "list_friends"} <= actions
    blob = json.dumps([(r.subject, r.actor, r.action, r.outcome, r.detail) for r in rows])
    assert p.token not in blob and "eyJ" not in blob


async def test_tools_list_shows_each_caller_only_what_it_can_call(harness):
    everything = {t.name for t in await harness.list_tools(harness.service)}
    assert "platform_outbox_claim" in everything and "add_expenses" in everything and "settle_group" in everything
    private = {t.name for t in await harness.list_tools((await harness.person()).token)}
    assert "add_expenses" in private and "split_expense" in private
    assert not {n for n in private if n.startswith("platform_")} and "settle_group" not in private  # group-only
    group = {t.name for t in await harness.list_tools((await harness.person(group_chat_id=-300_000 - new_telegram_id() % 10**6, group_title="G")).token)}
    assert "settle_group" in group and "add_expenses" not in group and not {n for n in group if n.startswith("platform_")}
