# Work reimbursements (the Finance Agent, over A2A)

The expense tracker stays exactly as it was. On top of it, an employee can get work expenses paid back by their company, and a manager can
set the policy and approve claims, all in Telegram. The company's side is a **separate service, the Finance Agent**, and the two talk
over **A2A** (the Agent2Agent protocol).

## The three parts

| Part                                            | What it is                                                                                                                               | Who uses it      |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| **The expense tracker** (`app/`, `mcp_server/`) | Regular expenses and splits, untouched. Its new `mcp_server/reimbursement/` folder keeps _work_ expenses apart and is the A2A **client** | the employee     |
| **The Finance Agent** (`finance_agent/`)        | Holds the company policy, answers questions, checks claims. The A2A **server**. Has its **own database** and its **own Telegram bot**    | the manager      |
| **A2A in between**                              | Agent card, signed tokens, tasks                                                                                                         | the two services |

## What each person does

**The manager** (in the Finance bot)

1. `/start` → name the company → gets a **company code** (e.g. `FIN-7K2QP`) to share.
2. `/setpolicy`, then pastes the policy (or sends a PDF / text file). The agent shows how it understood it as rules; the manager taps **Save** (or says what to fix).
   A long policy that Telegram splits into several messages is collected: send all the parts, then `/done`.
3. Later, `/editpolicy` (or just say it): _"raise the hotel cap to 8000"_, _"gym is not reimbursed"_, _"add parking up to 300 a day"_. The bot shows
   exactly what would change; **Save** makes a new policy version (old ones are kept).
4. Each finished claim arrives as one card with the policy's recommendation: **Approve · Reject · Different amount · Ask the employee**.
   **Different amount** goes through the expenses one by one (policy amount / full / nothing / a typed amount), shows a summary, and the employee
   is told for each expense what was paid and what was not, e.g. `❌ late cab: not refunded (₹2,000)`.
5. `/paid` marks approved claims as paid.

| Manager command                |                                                                                                                 |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `/setpolicy`                   | Give the policy (or replace it: you see what changes before it's saved). `/setpolicy <text>` works too          |
| `/editpolicy`                  | Change part of it (`/editpolicy raise the hotel cap to 8000`)                                                   |
| `/policy` · `/history`         | The current rules · earlier versions with what changed                                                          |
| `/claims` · `/paid`            | Claims waiting for you · approved claims to mark as paid                                                        |
| `/employees` · `/code`         | Who joined · your company code                                                                                  |
| _(any other message)_          | Never changes the policy. The policy changes **only** through `/setpolicy` or `/editpolicy` (and then **Save**) |
| `/done` · `/retry` · `/cancel` | Finish a policy sent in several messages · try the last step again · stop what the bot is waiting for           |

If the model is slow or unreachable, your text is **kept** and the bot offers **Try again** (or `/retry`): you never paste it twice.

**The employee** (in the expense bot, private chat)

1. `/join FIN-7K2QP` (or say "join company FIN-7K2QP").
2. `/work cab 450 to the client` records a **work** expense and `/personal lunch 250` a **personal** one, so the two never get mixed
   (plain messages like "work expense: cab 450" or "lunch 250" still work). `/work` alone opens the reimbursement screen (`/reimburse` too). Work expenses are **separate from personal spending**: they never appear in
   summaries or budgets. _"work expense: cab 450 to the client"_, or _"that cab was for work"_ to move one that was already logged.
3. Asks the policy with `/policy will I get a refund on a hotel room?` (or just says it). `/claims` lists their claims. The answer comes from the real policy; if details are missing the agent asks
   (city? nights? amount?). If the policy says nothing, the bot offers to **ask the manager**, and the manager's answer comes back as a message.
4. _"send my claim"_ → confirm → the agent checks each item and asks for what's missing (nights, receipt) → the manager decides → the
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

## Asked up front, receipts included

When an employee adds a work expense (and has joined a company), the Finance Agent looks at it **straight away** (A2A skill `check_expense`)
and the bot asks for what the policy needs for _that_ expense: the receipt, nights, days, whether it was approved first, the city tier.

- _"work expense: flight 3000"_ → if the policy wants a receipt above ₹500: **"Send me a photo or PDF of it, or say “no receipt”."**
- The employee sends the photo or PDF in the chat. It is kept with that expense (up to 5 MB; JPG, PNG, WEBP or PDF). Typing "I have it" is not enough.
- Answers like _"2 nights"_, _"yes it was approved"_, _"Tier-1"_ go to the right expense with no numbers needed. _"no receipt"_ is accepted too:
  the claim then shows that expense as not eligible and the manager decides.
- _"send my claim"_ is refused while an expense still lacks what the policy asked, and says what is missing. When it is sent, the **receipts
  travel with the claim** and the manager's bot sends each file right after the claim card, labelled with its expense, to check.
- If the finance agent is down when an expense is added, nothing is asked then: the checks at claim time (below) still catch everything.
- A PDF sent while an expense waits for its receipt is the receipt. Otherwise a PDF is still a bank statement.

## The manager just chatting

A message that is not a command (and not an answer the bot is waiting for) gets a **short answer about this company's policy, claims or
employees** (max 3 lines), or a one-line refusal if it is about something else. It never changes the policy: a request to change a rule
is pointed to `/editpolicy`. Guardrails: messages over 400 characters are declined, 12 questions per 10 minutes per manager, only the
company's own facts reach the model, and if the model is down the manager gets the list of commands.

## When a claim needs more details

After "send my claim" the finance agent may ask things the policy needs (how many days / nights, do you have the receipt, was it approved
first, which city tier). The claim waits **with the employee**, and the manager sees nothing until it is complete.

- The employee just answers in their own words: _"1 day for all"_, _"2 nights and yes I have the receipt"_. No claim or item numbers are needed;
  the answer is applied to every open question of that kind. Different answers per item work too ("lunch 2 days, dinner 1 day").
- `/claims` (or "what do you need for my claim?") lists exactly what each claim is waiting for, and says so if the **manager** asked something.
- "Send my claim" again does not fail: it says the expenses are already in claim #N and what that claim needs.
- The employee always sees **their own** claim number. The manager's cards use the Finance Agent's number (so the two can differ).

**Matching an expense to a rule** uses what the expense _is_ (its description). A single word in the category alone (the chat assistant might
label sweets for an event as "food") is only a hint: the finance agent's model is asked to confirm, and if no rule clearly covers the item
it goes to the manager as "no rule covers this". Conditions that only a person can check ("only during approved business travel") are shown
on the line as `(check: ...)`.

## What a policy can say

A policy is turned into **rules**. Each rule has a limit and a unit, and the unit decides how it is applied:

| The policy says                                                      | Rule                                                                                                                                    |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| "Hotels: Tier-1 cities ₹4,000 a night, Tier-2 ₹3,000, others ₹2,000" | one rule with **variants**; a city in the claim (Pune, Jaipur...) picks the tier, otherwise the employee is asked "Which applies: ...?" |
| "Lunch: max ₹350 a day"                                              | `per_day`: 2 days of lunches are limited to ₹700                                                                                        |
| "All ground transport combined must not exceed ₹5,000 per trip"      | `per_claim`: **one limit shared** by every item of that kind in the claim (a ₹3,000 cab plus a ₹3,500 train pays ₹5,000, not ₹6,500)    |
| "Internet and mobile: ₹1,000 a month"                                | `per_month`: shared by everything this person claims in the month, **across claims**                                                    |
| "Purchases above ₹5,000 need prior approval"                         | `preapproval_over`: the employee is asked whether it was approved, only above that amount                                               |
| "Receipts must be submitted"                                         | `receipt_over` (0 = always)                                                                                                             |
| "50% of the phone bill"                                              | `percent`                                                                                                                               |
| "Alcohol, fines, personal shopping are not reimbursed"               | rules with `covered: false`. These **win** over a paying rule (beer at dinner is alcohol, not dinner)                                   |

Every amount is computed by `finance_agent/policy/engine.py`. The model only reads the text.

## How the services talk (A2A)

**Discovery.** The Finance Agent publishes its agent card at `<finance>/.well-known/agent-card.json` (name, skills, endpoint). The tracker
reads it before every call.

**Skills** (one JSON message each; the reply is an A2A _task_):

| Skill          | Does                                                         | Task states used                                          |
| -------------- | ------------------------------------------------------------ | --------------------------------------------------------- |
| `join_company` | employee joins with the code                                 | completed / failed                                        |
| `ask_policy`   | policy question; follow-ups continue the **same task**       | **input-required** while it needs details, then completed |
| `ask_manager`  | passes a question to the manager                             | completed                                                 |
| `submit_claim` | checks the claim, hands it to the manager                    | **input-required** if details are missing, else completed |
| `claim_reply`  | gives the missing details, or answers the manager's question | input-required / completed                                |
| `claim_status` | where a claim stands                                         | completed                                                 |

**The manager's decision** is not part of an open task (it can take days). It travels back as a **signed webhook**:
`POST <tracker>/a2a/webhook` with `X-Signature: sha256=HMAC(body, shared secret)`. Events: `claim_update` (approved / rejected / paid),
`claim_question` (the manager asked), `manager_answer`. The Finance Agent keeps each event in its database until the tracker accepts it
(retries with back-off), and the tracker ignores a repeated one. Each becomes a normal Telegram message through the existing outbox.

**Trust, both directions.**

- _Tracker → Finance:_ every call carries a short-lived (2 minute) RS256 token the tracker signs with its own key. The Finance Agent
  checks the signature against the tracker's published keys (`/.well-known/jwks.json`), the issuer, that it was **addressed to it**
  (`aud`), the expiry and the scope. The token names the employee, so the tracker, not the employee, vouches for who they are.
- _Finance → Tracker:_ the HMAC signature above.
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

- **Not enforced yet, though many policies say it:** who must approve at which amount (reporting manager / department head / finance manager),
  the 30- and 90-day claim deadlines, who is eligible (full-time vs contractors), processing timelines, and "economy class only" (kept as a note
  on the rule for the manager to see). The manager is the only approver, and decides these by hand on the card.
- Reading a long policy asks the model to write a lot of structured text, so it can take a minute (`FINANCE_POLICY_TIMEOUT`, default 150 seconds).
  It still can misread a clause: that is why you see what it understood, and why you can fix it by talking before saving.

- **Receipts are a yes/no answer**, not an uploaded photo. (Reading photos needs a vision-capable model; not built.)
- Policy edits and Q&A depend on the language model reading text correctly. That is why the manager confirms every change, and why amounts
  are computed by code. A very messy policy PDF may need a few rounds of corrections.
- An employee belongs to **one** company; a manager runs **one** company.
- The Finance Agent's database is created automatically (`create_all`) rather than by migrations (the policy tables are new and small).
- The manager's decision comes back by webhook, not by streaming a long-running task; both services must be able to reach each other.
- Claims are not paid by this system: "paid" is the manager marking it after paying outside.

## Where the code is

| Folder                      |                                                                                                                   |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `finance_agent/`            | the whole Finance Agent (see its [README](../finance_agent/README.md) for a file-by-file map)                     |
| `mcp_server/reimbursement/` | the tracker's side: `models.py`, `domain.py`, `tools.py` (what the AI agent calls), `a2a_client.py`, `webhook.py` |
| `app/bot/reimburse_flow.py` | the `/reimburse` screen and its buttons                                                                           |
| `tests/finance/`            | engine, policy, claims, manager, and `test_with_tracker.py` (both services running together)                      |
