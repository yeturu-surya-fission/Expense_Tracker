# The MCP server

`mcp_server/` is a standalone service that owns **everything about expenses**: the tools, the business logic, the database and the
authentication. The Telegram bot is just its first client. Anything that speaks MCP (Streamable HTTP, spec revision 2026-07-28) and holds a
valid token can use the same tools.

For where it sits in the whole system see [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Endpoints

| Endpoint | Auth | What |
|---|---|---|
| `POST /mcp` | `Authorization: Bearer <token>` on **every** request | the MCP endpoint: `tools/list`, `tools/call`, `server/discover` (stateless: no session is kept on the server) |
| `POST /auth/token` | client id + secret | `client_credentials`: a first-party client gets a **service token** |
| `POST /auth/session` | service token | the client vouches for one Telegram person in one chat and gets that person's **session token** |
| `POST /auth/revoke` | service token | end a token before it expires |
| `GET /.well-known/jwks.json` | none | public keys that verify our tokens |
| `GET /.well-known/oauth-protected-resource/mcp` | none | RFC 9728 metadata: which authorization server protects this resource (where outside OAuth clients start) |
| `GET /healthz` | none | liveness |

A request without a valid token gets `401` and a `WWW-Authenticate: Bearer … resource_metadata="…"` header.

## 2. Tokens

RS256 JWTs. The server signs with its private key (`MCP_JWT_PRIVATE_KEY`; a throw-away key is generated in dev) and publishes the public
keys at the JWKS endpoint, so a gateway or another service can verify tokens without a shared secret. Rotation: set the new key as the
private key and put the old *public* key in `MCP_JWT_EXTRA_PUBLIC_KEYS` until old tokens have expired (≤ 30 minutes).

| | Service token | Session token |
|---|---|---|
| Who | a first-party client (the bot) | one person, in one chat context |
| `sub` | `client:telegram-bot` | `user:<id>` |
| `scope` | `mcp platform` | `mcp user` |
| Lifetime | 10 minutes | 30 minutes |
| Extra claims | `client_id` | `uid`, `chat_type` (`private` / `group`), `group_chat_id`, `act` (the client acting for the user) |
| Used for | platform tools, listing tools, opening sessions | every user tool |

**Checked on every request:** signature and `kid`, `alg` (only RS256), issuer, **audience** (`<MCP_PUBLIC_URL>/mcp`), expiry (`exp`),
required claims, **revocation** (`jti` table), then the tool's scope, then the chat context, then a rate limit. A token minted for another
server, an expired one, one signed by someone else, `alg: none` and a revoked token are all rejected (`tests/server/test_auth.py`).

**Who may get a session** is decided by the server: the allow-list (`ALLOWED_TELEGRAM_IDS`), friends of anybody, and members of a group the
owner added the bot to. A group session for an unknown chat is only opened for someone allowed to set the group up.

**Identity is never an argument.** No tool takes a user id: the person and the chat come from the token. A client cannot act for someone
unless the server issued it a session for that person, which only a service-token holder can ask for.

## 3. The result of every tool

```json
{ "status": "ok | input_required | rejected | error",
  "text": "ready-to-show message",
  "data": { "expense_ids": [12], "split_ids": [5], "start_users": [{"telegram_id": 123, "name": "Komal"}] },
  "input_requests": [],
  "error": null }
```

| `status` | Meaning |
|---|---|
| `ok` | done; `data` has structured facts (ids for Undo buttons, people who must press Start, …) |
| `input_required` | a human must answer first; **nothing has been written** (see below) |
| `rejected` | a normal "no": unknown friend, amounts that don't add up, already settled… show `text` |
| `error` | the call could not be processed (`error.code`: `insufficient_scope`, `not_available_here`, `rate_limited`, `internal`…) |

## 4. Asking the human (human-in-the-loop)

A tool that needs an answer stops **before writing** and returns the question. The client asks the human and calls the **same tool again
with the same arguments plus `answers`**. Nothing is committed until every question has an answer.

```text
→ split_expense {amount: 900, description: "dinner", with_friends: ["Bob"]}
← {status: "input_required", text: "How should I split ₹900 between 2 people?",
   input_requests: [{key: "split_mode", type: "choice",
                     options: [{id: "equal", label: "Equally"}, {id: "exact", label: "I'll tell the amounts"}]}]}
   … the human taps "Equally" …
→ split_expense {…same arguments…, answers: {split_mode: "equal"}, idempotency_key: "k1"}
← {status: "ok", text: "Split #7 · dinner ₹900 …", data: {split_ids: [7], start_users: []}}
```

* A tool may ask more than one question in a row (`kind`, then `split_mode`): call again after each answer.
* An answer that is not one of the options asks again; `"cancel"` ends it with "Cancelled - nothing was recorded".
* It is **stateless on the server**: no pending-question table, so a restart or a second server instance changes nothing.

Every user tool also accepts three optional protocol arguments (AI clients should hide them from the model and supply them themselves):

| Argument | Meaning |
|---|---|
| `answers` | `{question key: option id}` from the human, after an `input_required` result |
| `idempotency_key` | repeat a *successful* write with the same key and you get the first result back, nothing is written twice |
| `member_count` | advisory: how many people the chat platform says are in the group chat (used for the "I only know 3 of 5 people" warning) |

## 5. Tools

`tools/list` shows each caller only the tools it can call: a session token sees the user tools of its chat context; a service token sees all.

**User tools** (session token). `private` / `group` = the chat context they work in.

| Tool | Context | Kind | What |
|---|---|---|---|
| `add_expenses`, `undo_last_expense`, `undo_expenses` | private | write | record / undo own expenses |
| `set_budget`, `remove_budget`, `get_budgets` | private | write / read | monthly budgets with 50/80/100% alerts |
| `get_expenses`, `spending_summary`, `monthly_trend`, `daily_breakdown` | private | read | reports for any period |
| `add_friend`, `remove_friend`, `list_friends` | private | write / read | the friend circle |
| `split_expense` | private | write | split one bill with friends (asks how) |
| `split_bills` | private, group | write | several bills by different payers, netted to the fewest payments |
| `my_dues`, `debt_action` | private | read / write | who owes whom (one total per person; opposite debts are netted first); **I paid / Received / Not yet** for one bill |
| `settle_with` | private | write | the dues buttons, one per **person**: `pay` (everything I owe them), `received`, `notyet` |
| `pay_friend` | private | write | "I paid Surya 300": takes that amount off what the user owes that friend (all of it when no amount); the friend confirms |
| `cancel_split` | private, group | write | cancel a split nobody has paid yet |
| `create_group` | private, group | read | how to set up a trip group |
| `add_group_expense`, `group_summary`, `settle_group` | group | write / read | the group's expenses and settle-up (asks to confirm) |
| `join_group_via_start` | private | write | a person pressed a group's "Start the bot" button |
| `statement_read_pdf`, `statement_preview`, `statement_apply`, `statement_cancel`, `statement_undo` | private | read / write | bank-statement import (debits only, with a confirmation step) |
| `ui_view`, `last_expense` | private | read | screens for a chat client's commands and buttons (summary, history, budgets, friends, dues, daily, trend, invite) |
| `agent_context` | private, group | read | what an AI agent should know before choosing tools |
| `respond_to_user` | private, group | read | fixed greeting / help / refusal wording |

**Platform tools** (service token only):

| Tool | What |
|---|---|
| `platform_outbox_claim` / `platform_outbox_ack` | take the next messages for people, report delivered / blocked / failed |
| `platform_group_event` | the platform reports: bot added, members joined/left, title changed, chat migrated |
| `platform_accept_invite` | someone opened a friend's invite link |
| `platform_friend_ids` | Telegram ids of a person's friends (to see which are in a chat) |

## 6. Messages for other people: the outbox

The server never talks to Telegram. A tool that must tell someone ("you owe 750", a reminder, "that split was cancelled") writes an
**outbox row in the same transaction as the data**. A platform client claims rows (`platform_outbox_claim`, leased for 60 s), delivers
them, and acknowledges each:

* delivered → the platform's message id is stored (so the message can be deleted later);
* the person never pressed Start / blocked the bot → `blocked`: remembered, and their bills are queued again the moment they open a session as
  "started";
* a network problem → retried with growing delay, five attempts.

Payment reminders are produced by a background job inside the server (every 30 minutes), also through the outbox.

## 7. Running it

```bash
# docker (both services, each with its own database)
cp .env.example .env     # fill MCP_BOT_CLIENT_SECRET, TELEGRAM_BOT_TOKEN, CODEVECTOR_API_KEY
docker compose up --build

# locally, server only
export MCP_DATABASE_URL=postgresql+asyncpg://expense:expense@localhost:5432/expense_mcp
python -m mcp_server                       # http://localhost:8001/mcp  (dev: creates tables, throw-away signing key)
```

Production checklist: `MCP_ENV=production` (refuses to start without `MCP_JWT_PRIVATE_KEY`), a real `MCP_PUBLIC_URL` behind HTTPS,
`MCP_AUTO_CREATE_TABLES=false` with `alembic upgrade head` (the Dockerfile does this), a long random `MCP_BOT_CLIENT_SECRET`, and
`ALLOWED_TELEGRAM_IDS` set. Create a signing key with `python -m mcp_server.auth.genkey`.

Migrations: `alembic revision --autogenerate -m "what changed"` then `alembic upgrade head`. A test checks that the migrations produce
exactly the schema the models describe.

## 8. Not done yet: sign-in for people outside Telegram

Today every session is opened by a trusted first-party client that vouches for a Telegram identity. The server already publishes the
OAuth 2.1 resource-server metadata (RFC 9728) and checks audience-bound tokens, which is what an outside AI app needs to discover how to
log in. The remaining piece is an authorization server on this service ("Sign in with Telegram" → authorization code + PKCE → the same
session tokens), so that someone using a desktop AI app and someone using the Telegram bot are the same account. Until then, other
clients need a service credential issued by the operator.
