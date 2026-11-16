# Work reimbursements (the Finance Agent, over A2A)

The expense tracker stays exactly as it was. On top of it, an employee can get work expenses paid back by their company, and a manager can
set the policy and approve claims, all in Telegram. The company's side is a **separate service, the Finance Agent**, and the two talk
over **A2A** (the Agent2Agent protocol).

## The three parts

| Part | What it is | Who uses it |
|---|---|---|
| **The expense tracker** (`app/`, `mcp_server/`) | Regular expenses and splits, untouched. Its new `mcp_server/reimbursement/` folder keeps *work* expenses apart and is the A2A **client** | the employee |
| **The Finance Agent** (`finance_agent/`) | Holds the company policy, answers questions, checks claims. The A2A **server**. Has its **own database** and its **own Telegram bot** | the manager |
| **A2A in between** | Agent card, signed tokens, tasks | the two services |

## What each person does

**The manager** (in the Finance bot)
1. `/start` → name the company → gets a **company code** (e.g. `FIN-7K2QP`) to share.
2. Pastes the policy (or sends a PDF). The agent shows how it understood it as rules; the manager taps **Save** (or says what to fix).
3. Later, edits by talking: *"raise the hotel cap to 8000"*, *"gym is not reimbursed"*, *"add parking up to 300 a day"*. The bot shows
   exactly what would change; **Save** makes a new policy version (old ones are kept).
4. Each finished claim arrives as one card with the policy's recommendation: **Approve · Reject · Different amount · Ask the employee**.
5. `/paid` marks approved claims as paid.

**The employee** (in the expense bot, private chat)
1. `join company FIN-7K2QP`.
2. `/reimburse` opens the work-reimbursement screen. Work expenses are **separate from personal spending**: they never appear in
   summaries or budgets. *"work expense: cab 450 to the client"*, or *"that cab was for work"* to move one that was already logged.
3. Asks the policy: *"will I get a refund on a hotel room?"*. The answer comes from the real policy; if details are missing the agent asks
   (city? nights? amount?). If the policy says nothing, the bot offers to **ask the manager**, and the manager's answer comes back as a message.
4. *"send my claim"* → confirm → the agent checks each item and asks for what's missing (nights, receipt) → the manager decides → the
   employee gets the result (and later "paid").

## Example

```
Manager  : (pastes policy)
Finance  : Here's how I understood your policy:
           • Hotel stays: up to ₹6,000 per night, receipt needed above ₹500
           • Meals: up to ₹1,000 per day, only during business travel
           • Gym membership: not reimbursed        [✅ Save] [🗑 Discard]

Kiran    : will I get a refund on a hotel room?         (in the expense bot)
Bot      : How many nights?   →  (Kiran) 2 nights, 15000 total
Bot      : ⚠️ Partly. ₹12,000 of ₹15,000: the limit is ₹12,000 (2 × ₹6,000 per night)
           Rule: Hotel stays: up to ₹6,000 per night, receipt needed above ₹500   (Company policy v1)

Kiran    : send my claim
Bot      : Send 2 item(s) totalling ₹15,450 to your manager for approval?  [✅ Yes, send]
Bot      : To check your claim I need a little more:
           • Pune hotel: How many nights?  • Pune hotel: Do you have the receipt? (yes/no)
Kiran    : 2 nights, yes I have it
Bot      : 📨 Claim #3 is with your manager. … The policy suggests ₹12,450 of ₹15,450.

Manager gets a card:  Pune trip · Kiran Rao
           ✅ cab to client: ₹450 → ₹450
           ⚠️ Pune hotel: ₹15,000 → ₹12,000 — …
           [✅ Approve ₹12,450] [❌ Reject] [✏️ Different amount] [💬 Ask Kiran]
Manager  : Approve
Kiran    : ✅ Claim #3 (Pune trip) approved: ₹12,450. ₹3,000 of the ₹15,450 you claimed isn't covered.
```

## How the services talk (A2A)

**Discovery.** The Finance Agent publishes its agent card at `<finance>/.well-known/agent-card.json` (name, skills, endpoint). The tracker
reads it before every call.

**Skills** (one JSON message each; the reply is an A2A *task*):

| Skill | Does | Task states used |
|---|---|---|
| `join_company` | employee joins with the code | completed / failed |
| `ask_policy` | policy question; follow-ups continue the **same task** | **input-required** while it needs details, then completed |
| `ask_manager` | passes a question to the manager | completed |
| `submit_claim` | checks the claim, hands it to the manager | **input-required** if details are missing, else completed |
| `claim_reply` | gives the missing details, or answers the manager's question | input-required / completed |
| `claim_status` | where a claim stands | completed |

**The manager's decision** is not part of an open task (it can take days). It travels back as a **signed webhook**:
`POST <tracker>/a2a/webhook` with `X-Signature: sha256=HMAC(body, shared secret)`. Events: `claim_update` (approved / rejected / paid),
`claim_question` (the manager asked), `manager_answer`. The Finance Agent keeps each event in its database until the tracker accepts it
(retries with back-off), and the tracker ignores a repeated one. Each becomes a normal Telegram message through the existing outbox.

**Trust, both directions.**
- *Tracker → Finance:* every call carries a short-lived (2 minute) RS256 token the tracker signs with its own key. The Finance Agent
  checks the signature against the tracker's published keys (`/.well-known/jwks.json`), the issuer, that it was **addressed to it**
  (`aud`), the expiry and the scope. The token names the employee, so the tracker, not the employee, vouches for who they are.
- *Finance → Tracker:* the HMAC signature above.
- The tracker's own tools keep the usual gate (token → scope → context → rate limit → idempotency → audit).

## Setup

1. A **second bot** from @BotFather for the manager: `FINANCE_TELEGRAM_BOT_TOKEN`.
2. `FINANCE_WEBHOOK_SECRET`: a long random string (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
3. `CODEVECTOR_API_KEY` (already set for the expense bot; the Finance Agent uses it to read policies).
4. Optional: `FINANCE_ALLOWED_MANAGER_IDS` (comma list of Telegram ids that may become managers; empty = anyone who messages the manager bot).
5. `docker compose up --build` → six containers: `mcp`, `bot`, `finance` and a Postgres for each.

`MCP_FINANCE_AGENT_URL` (tracker) and `FINANCE_PUBLIC_URL` (finance) must be the **same address**: tokens are addressed to it. Compose sets both
to `http://finance:8002`. Running without Docker, see `.env.example`.

## Rules the system keeps

- **The model never touches an amount.** It reads text (which rule? how many nights?); `finance_agent/policy/engine.py` does every sum with exact decimals.
- **The manager always decides.** The agent only recommends.
- **The manager confirms what the agent understood.** A parsed or edited policy is saved only after **Save**. Each save is a new version (old ones are kept),
  and every claim records which version it was checked against. (A claim that is re-checked after the employee gives more details is checked
  against the policy as it is then.)
- **Work expenses never mix with personal ones.** Separate tables; not in summaries or budgets. Moving one removes it from personal spending.
- **A failed call loses nothing.** If the Finance Agent is down when a claim is sent, the items go back to the waiting list.
- **Companies are isolated.** A manager sees only their company's claims; an employee only their own.

## Limits (today)

- **Receipts are a yes/no answer**, not an uploaded photo. (Reading photos needs a vision-capable model; not built.)
- Policy edits and Q&A depend on the language model reading text correctly. That is why the manager confirms every change, and why amounts
  are computed by code. A very messy policy PDF may need a few rounds of corrections.
- An employee belongs to **one** company; a manager runs **one** company.
- The Finance Agent's database is created automatically (`create_all`) rather than by migrations (the policy tables are new and small).
- The manager's decision comes back by webhook, not by streaming a long-running task; both services must be able to reach each other.
- Claims are not paid by this system: "paid" is the manager marking it after paying outside.

## Where the code is

| Folder | |
|---|---|
| `finance_agent/` | the whole Finance Agent (see its [README](../finance_agent/README.md) for a file-by-file map) |
| `mcp_server/reimbursement/` | the tracker's side: `models.py`, `domain.py`, `tools.py` (what the AI agent calls), `a2a_client.py`, `webhook.py` |
| `app/bot/reimburse_flow.py` | the `/reimburse` screen and its buttons |
| `tests/finance/` | engine, policy, claims, manager, and `test_with_tracker.py` (both services running together) |
