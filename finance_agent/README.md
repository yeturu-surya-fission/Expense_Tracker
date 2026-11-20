# Finance Agent

A separate service that plays **a company's finance team**. It holds the reimbursement policy, answers employees' policy questions,
checks their expense claims against the policy, and asks the manager (in Telegram) for the final decision.

It shares **no code** with the expense tracker (`app/`, `mcp_server/`) and never reads its database. The two only talk over
**A2A** (the Agent2Agent protocol). A test enforces this. Full flow, setup and examples: [../docs/REIMBURSEMENT.md](../docs/REIMBURSEMENT.md).

## Where to look

```
finance_agent/
├── main.py            the web app: A2A endpoint + agent card + manager bot, started together
├── config.py          every setting (FINANCE_* environment variables)
├── llm.py             the ONLY place a language model is called (CodeVector); the model reads text, it never does maths
├── formatting.py      money formatting and rounding
├── outbox.py          "write it down first": messages for the manager and webhooks for the tracker
│
├── policy/            THE POLICY
│   ├── models.py        Rule (one line of the policy) · Item (something claimed) · Verdict (what the policy says)
│   ├── engine.py        the maths: caps per night/day, limits shared by a trip or a month, city-tier variants, percentages, receipts,
│   │                    pre-approval. Pure functions, no LLM
│   ├── parser.py        policy text → rules (LLM), edits from plain sentences, "what changed" diffs
│   ├── qa.py            "will I get a refund on this room?" answered from the rules; asks follow-ups
│   └── store.py         companies, company codes, policy versions
│
├── claims/            CLAIMS
│   ├── review.py        find each item's rule, let the engine decide
│   ├── service.py       the claim's life: needs_info → awaiting_manager → approved/rejected → paid
│   └── texts.py         the words for the employee and the manager's approval card
│
├── a2a/               THE A2A SIDE (how the expense tracker talks to us)
│   ├── card.py          the public agent card (/.well-known/agent-card.json)
│   ├── skills.py        what we can do: join_company, ask_policy, ask_manager, submit_claim, claim_reply, claim_status
│   ├── executor.py      turns an A2A message into a task and a skill result into a task state
│   ├── auth.py          checks the tracker's signed token against its public keys
│   ├── server.py        mounts the endpoint (tasks are stored in the database)
│   └── notify.py        signed webhooks back to the tracker (decisions, questions), retried until accepted
│
├── manager/           THE MANAGER'S TELEGRAM BOT
│   ├── service.py       everything the manager can do (commands, policy steps, decisions), with no Telegram in it (easy to test)
│   └── bot.py           carries messages in and out; the command menu; delivers notifications
│
└── db/                this service's own database (models, session)
```

## Rules the code keeps

- **Money is computed by code.** `policy/engine.py` is pure Python with exact decimals; the LLM only _reads_ (which rule? how many nights?).
- **The manager decides.** The agent only recommends; nothing is approved without the manager's tap.
- **The manager confirms the policy.** A parsed policy is shown back and saved only after "Save". Every save is a new version, and each claim records
  the version it was checked against.
- **Nothing is lost.** Messages and decisions are written to the database in the same transaction as the change, then delivered by a loop.
- **Each company only sees its own data**, and an employee only their own claims.

## Run it

`docker compose up --build` starts it as the `finance` service (port 8002). Without Docker:
`uv sync --only-group finance` then `python -m finance_agent` (or `uv run python -m finance_agent`; settings: `.env.example`, section "Finance Agent").
