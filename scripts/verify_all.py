"""Item 4 verification: tools registration, schemas, lifespan, and the full auth chain.

Run: & .venv\\Scripts\\python.exe scripts\\verify_all.py
"""
import asyncio
import os
import sys

ROOT = r"C:\Users\FL_LPT-657\Projects\Expense_Tracker"
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402

for key, value in dotenv_values(os.path.join(ROOT, ".env")).items():
    if key and value and key.startswith("MCP_"):
        os.environ.setdefault(key, value)


async def main() -> int:
    failures: list[str] = []

    # ---- 1. all 21 tools registered, schemas exclude ctx
    import mcp_server.server  # noqa: F401  (registers tools, builds Starlette app)
    from fastmcp import Client
    from mcp_server.tools import mcp

    async with Client(mcp) as client:
        tools = await client.list_tools()
    expected = {
        "respond_to_user", "add_expenses", "undo_last_expense", "set_budget", "remove_budget",
        "get_budgets", "get_expenses", "spending_summary", "monthly_trend", "daily_breakdown",
        "add_friend", "remove_friend", "list_friends", "split_expense", "my_dues", "cancel_split",
        "create_group", "add_group_expense", "group_summary", "settle_group", "split_bills",
    }
    got = {t.name for t in tools}
    if got != expected:
        failures.append(f"tool set mismatch: missing={expected - got} extra={got - expected}")
    for t in tools:
        schema = getattr(t, "input_schema", None) or t.inputSchema
        if "ctx" in schema.get("properties", {}):
            failures.append(f"{t.name}: ctx leaked into schema")
    print(f"tools: {len(tools)} registered, ctx excluded from all schemas")

    # ---- 2. lifespan: Starlette app builds and inits the DB (already exercised by Client(mcp) above)
    from mcp_server.server import app as starlette_app
    print(f"lifespan: starlette app built, routes={len(starlette_app.routes)}")

    # ---- 3. auth chain roundtrip (phases 3 -> 5)
    from backend.auth.issuer import issue_mcp_token, TOKEN_AUDIENCE, TOKEN_ISSUER
    from mcp_server.auth.verifier import MCPVerifier

    verifier = MCPVerifier()
    token = issue_mcp_token(user_id=42, chat_type="group", group_chat_id=-100123)
    at = await verifier.verify_token(token)
    if at is None:
        failures.append("valid token was rejected by the verifier")
    else:
        claims = at.claims
        ok = (claims.get("user_id") == 42 and claims.get("chat_type") == "group"
              and claims.get("group_chat_id") == -100123)
        print(f"auth chain: valid token accepted, claims={claims}")
        if not ok:
            failures.append(f"claims coerced wrongly: {claims}")

    private_token = issue_mcp_token(user_id=7, chat_type="private")
    at_private = await verifier.verify_token(private_token)
    if at_private is None:
        failures.append("valid private-chat token was rejected")
    elif "group_chat_id" in (at_private.claims or {}):
        failures.append("private token must not carry group_chat_id")
    else:
        print(f"auth chain: private token accepted, claims={at_private.claims}")

    # tampered payload must be rejected
    h, p, s = token.split(".")
    if await verifier.verify_token(f"{h}.{p[:-1]}{chr(ord(p[-1]) ^ 1)}.{s}") is not None:
        failures.append("tampered token was accepted")
    else:
        print("auth chain: tampered token rejected")

    # wrong audience must be rejected
    import jwt
    from mcp_server.auth.keys import get_public_key, TOKEN_ALG
    from backend.auth.keys import get_private_key
    wrong_aud = jwt.encode({"iss": TOKEN_ISSUER, "aud": "someone-else", "sub": "1",
                            "iat": 0, "nbf": 0, "exp": 4102444800, "chat_type": "private"},
                           get_private_key(), algorithm=TOKEN_ALG)
    if await verifier.verify_token(wrong_aud) is not None:
        failures.append("wrong-audience token was accepted")
    else:
        print("auth chain: wrong-audience token rejected")

    # expired token must be rejected
    expired = jwt.encode({"iss": TOKEN_ISSUER, "aud": TOKEN_AUDIENCE, "sub": "1",
                          "iat": 0, "nbf": 0, "exp": 0, "chat_type": "private"},
                         get_private_key(), algorithm=TOKEN_ALG)
    if await verifier.verify_token(expired) is not None:
        failures.append("expired token was accepted")
    else:
        print("auth chain: expired token rejected")

    # missing required claim must be rejected
    missing = jwt.encode({"iss": TOKEN_ISSUER, "aud": TOKEN_AUDIENCE, "sub": "1",
                          "iat": 0, "nbf": 0, "exp": 4102444800},
                         get_private_key(), algorithm=TOKEN_ALG)
    if await verifier.verify_token(missing) is not None:
        failures.append("token missing chat_type was accepted")
    else:
        print("auth chain: missing-required-claim token rejected")

    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


raise SystemExit(asyncio.run(main()))
