# Architecture

Expense Tracker is a Telegram bot for **personal expenses, budgets, and splitting bills with friends and groups**.
You talk to it in plain language; an LLM agent turns that into records in PostgreSQL. There are **no bank or cash accounts**:
"spent 300 on ice cream" just adds an expense.

This page shows the **structure** (what the parts are). For behaviour step by step see [FLOWS.md](FLOWS.md); for how the AI
agent loops see [STATE_GRAPH.md](STATE_GRAPH.md).

## 1. System overview

```mermaid
flowchart LR
    subgraph TGR["Telegram"]
        PC["👤 Private chats"]
        GC["👥 Group chats<br/>(the bot is added by the owner)"]
    end

    subgraph SVC["Expense Tracker service  (one container, FastAPI)"]
        direction TB
        BOT["Bot layer<br/>private · group · payment buttons · PDF"]
        RUN["AgentRunner<br/>one thread per person per chat<br/>pause and resume"]
        AG["LangGraph agent<br/>agent → tools → respond"]
        TOOLS["Tools<br/>private set  ·  group set"]
        CORE["Core  (no LLM, no Telegram)<br/>expenses · budgets · reports<br/>friends · splits · groups · debts<br/>notices · statements"]
        PARSER["Statement parser<br/>(LLM, structured output)"]
        NOT["Notifier<br/>private messages to other people"]
        REM["Reminder loop<br/>every 30 minutes"]
    end

    LLM["☁️ Gemini / OpenAI /<br/>Anthropic / Groq"]
    DB[("PostgreSQL<br/>expense tables +<br/>agent memory")]

    PC <-->|"messages, buttons, files"| BOT
    GC <-->|"@mentions, buttons"| BOT
    BOT -->|"free text"| RUN --> AG --> TOOLS --> CORE
    BOT -->|"menu buttons, /commands<br/>(no LLM, instant)"| CORE
    BOT -->|"PDF file"| PARSER --> CORE
    AG --> LLM
    PARSER --> LLM
    CORE --> DB
    AG --> DB
    CORE --> NOT
    NOT -->|"bills, reminders, confirmations"| PC
    REM --> CORE
    REM --> NOT
```

## 2. Layers and the one rule that keeps it tidy

Each layer only calls the ones **below** it. The LLM can never touch the database directly: it can only call tools, and tools
only call `core`.

```mermaid
flowchart TB
    L1["<b>bot/</b>  Telegram I/O: private chat, group chat, payment buttons, PDF upload, notifier, reminders"]
    L2["<b>agent/</b>  graph, runner, prompts, guardrails, LLM factory, statement parser"]
    L3["<b>tools/</b>  what the LLM may call (different sets for private and group chats)"]
    L4["<b>core/</b>  all the logic, NO LLM, NO Telegram"]
    L5["<b>db/</b>  SQLAlchemy models and async sessions"]
    L6[("PostgreSQL")]

    L1 --> L2 --> L3 --> L4 --> L5 --> L6
    L1 -.->|"instant buttons skip the agent"| L4
```

| Folder | Responsibility | Key files |
|---|---|---|
| `app/bot/` | Telegram: allow-list, menu, buttons, typing indicator, group events, PDF upload, private notifications, reminder loop | `base.py`, `private.py`, `group.py`, `dues_flow.py`, `statement_flow.py`, `handlers.py` (button router), `notifier.py`, `reminders.py`, `keyboards.py` |
| `app/agent/` | The brain: LangGraph graph, per-person runner (pause/resume), system prompt, scope guardrails, LLM provider factory, statement parser | `graph.py`, `runner.py`, `prompts.py`, `guardrails.py`, `llm.py`, `statement_parser.py` |
| `app/tools/` | Thin LangChain tools: validate input, ask the human when something is missing, delegate to `core` | `expenses.py`, `budgets.py`, `reports.py`, `friends.py`, `splits.py`, `bills.py`, `groups.py`, `conversation.py` |
| `app/core/` | **All the logic.** Money maths, splitting, settling, the pay/confirm lifecycle, every message sent to other people | `expenses.py`, `budgets.py`, `reports.py` + `views.py`, `friends.py`, `splits.py`, `groups.py`, `group_flow.py`, `debts.py`, `notices.py`, `notifier.py`, `statements.py`, `periods.py`, `markers.py` |
| `app/db/` | Models and sessions (`session_scope` = commit or roll back); startup adds new columns to older databases | `models.py`, `session.py` |
| `app/api/` | `/health`, `/telegram/webhook` | `routes.py` |

There is **no internal REST API**: tools call `core` functions in the same process, so a tool call and its database changes
commit or roll back together.

## 3. Two chat contexts

The same agent serves a private chat and a Telegram group chat, with different rules:

| | Private chat | Group chat |
|---|---|---|
| Who may use it | the allow-list, friends of a listed person, and anyone in a group the owner added the bot to | **anyone in the group** |
| When the bot reacts | every message, menu button, command | **only** an @mention, a reply to the bot, or a `/command` |
| Tools the AI may call | own expenses, budgets, reports, friends, splits, `split_bills`, dues, cancel split, how to set up a group | `add_group_expense`, `split_bills`, `group_summary`, `settle_group`, `cancel_split` |
| Personal money | yes | never (it would be visible to everyone) |
| Setup | `/start` | **automatic** when an allowed person adds the bot to a group |

A tool called in the wrong chat returns an error; the graph sends it back to the AI once so it can recover.

## 4. Agent internals (LangGraph)

```mermaid
flowchart TD
    S([START]) --> A["<b>agent</b><br/>LLM + system prompt<br/>(date, budget categories,<br/>friends or group members)"]
    A -->|"model called a tool"| T["<b>tools</b><br/>runs ONE tool call per step<br/>(only tools allowed in this chat)"]
    A -->|"model wrote plain text<br/>🛡️ guardrail: dropped"| X["replaced by the<br/>fixed refusal"] --> E([END])
    T -->|"more tool calls pending"| T
    T -->|"last call errored"| A
    T -->|"all calls ok"| R["<b>respond</b><br/>reply built from tool output<br/>+ hidden markers → buttons"]
    R --> E

    T -. "interrupt()" .-> H{{"⏸ paused: asks the user<br/>equally or amounts? settle now?"}}
    H -. "user taps a button / answers" .-> T
```

- **One tool call per step.** Every step is checkpointed, so a paused question never re-runs a write that already happened.
- **`respond` instead of a second LLM call.** The reply text is built from tool output: about twice as fast, and the model cannot add free text.
- **Markers.** Tools may end their output with hidden markers such as `(split #5)` or `(start #123)`. `respond` strips them and passes
  them to the bot as data, which turns them into the **Undo / Cancel split** buttons and the **"press Start"** tag for unreachable people.

## 5. Data model

```mermaid
erDiagram
    USERS ||--o{ FRIENDS : "owner calls friend by a nickname"
    USERS ||--o{ EXPENSES : "spent"
    USERS ||--o{ BUDGETS : sets
    USERS ||--o{ STATEMENT_IMPORTS : uploads
    BUDGETS ||--o{ BUDGET_ALERTS : "fires once per threshold per month"
    STATEMENT_IMPORTS ||--o{ EXPENSES : "produced"
    USERS ||--o{ GROUP_MEMBERS : "is in"
    GROUPS ||--o{ GROUP_MEMBERS : has
    GROUPS ||--o{ SHARED_EXPENSES : "group expenses"
    SHARED_EXPENSES ||--o{ SHARES : "split into"
    SHARES }o--o| EXPENSES : "becomes the person's own expense"
    SHARED_EXPENSES ||--o{ DEBTS : "quick split"
    GROUPS ||--o{ DEBTS : "settle-up"

    USERS {
        int id PK
        bigint telegram_id UK
        string name
        timestamptz started_at "pressed Start: can be messaged"
    }
    FRIENDS {
        int owner_id FK
        int friend_id FK
        string nickname "private to the owner"
    }
    EXPENSES {
        int id PK
        int user_id FK
        numeric amount
        string category
        string source "personal, split, statement"
        timestamptz occurred_at
        timestamptz voided_at "undo"
    }
    GROUPS {
        int id PK
        bigint chat_id UK "the Telegram group"
        string name "the chat title"
        string default_split "equal = stop asking"
    }
    GROUP_MEMBERS {
        int group_id FK
        int user_id FK
        timestamptz left_at "left the chat"
    }
    SHARED_EXPENSES {
        int id PK
        int group_id FK "null for a one-off split"
        int payer_id FK
        numeric amount
        string bundle_id "bills split together"
        timestamptz settled_at
    }
    SHARES {
        int shared_expense_id FK
        int user_id FK
        numeric amount
        int expense_id FK "set once recorded for them"
    }
    DEBTS {
        int id PK
        int debtor_id FK
        int creditor_id FK
        numeric amount
        string kind "quick, group, bundle"
        string detail "the bills behind the amount"
        string status "pending, claimed, settled, cancelled"
    }
    BUDGETS {
        int user_id FK
        string category
        numeric monthly_limit
    }
    BUDGET_ALERTS {
        int budget_id FK
        string period "YYYY-MM"
        int threshold "50, 80, 100"
    }
    STATEMENT_IMPORTS {
        int user_id FK
        string status
        jsonb items
    }
```

Nothing is ever deleted: **undo** sets `voided_at`. LangGraph keeps its own checkpoint tables in the same database
(conversation memory and paused questions).

### How money moves

| Situation | What is stored | When it becomes someone's own expense |
|---|---|---|
| "spent 300 on ice cream" | one `expenses` row | immediately |
| Quick split with friends | a `shared_expense` + a `share` per person + a `debt` per friend | the payer's share now; a friend's share when they tap **I paid** |
| Several bills, different payers (a party) | several `shared_expenses` with one `bundle_id`, shares, and only the **netted** `debts` | people who owe nothing now; a debtor when they tap **I paid** (once, even if they owe two people) |
| Group chat expense | a `shared_expense` + shares under the group | at settle-up: people who owe nothing now; a debtor on **I paid** |
| Statement PDF | `expenses` rows (debits only) | on confirmation |

Shares are exact to the paisa, and the odd paise rotate between people so nobody drifts ahead.

## 6. Configuration and deployment

```mermaid
flowchart LR
    ENV[".env"] --> CFG["config.py<br/>(strips quotes and inline comments)"]
    CFG --> KEY{"LLM_API_KEY prefix"}
    KEY -->|"AIza… / AQ."| G["Gemini"]
    KEY -->|"sk-ant-"| AN["Anthropic"]
    KEY -->|"gsk_"| GR["Groq"]
    KEY -->|"sk-"| O["OpenAI"]

    subgraph DC["docker compose"]
        W["app<br/>(FastAPI + bot)"] --> PG[("postgres:16<br/>database expense_tracker<br/>volume pgdata_expense")]
    end
    CFG --> W
```

| Setting | Meaning |
|---|---|
| `TELEGRAM_MODE` | `polling` for local/dev, `webhook` for production (needs `PUBLIC_URL`) |
| `ALLOWED_TELEGRAM_IDS` | the owner side: who may use the bot and add it to groups; empty means everyone |
| `LLM_API_KEY` / `LLM_PROVIDER` / `LLM_MODEL` | the brain; provider is auto-detected from the key |
| `REMINDER_EVERY_HOURS` | how often an unpaid share is re-sent (default 48) |
| `ASK_GROUP_MIN_PEOPLE` | a one-off split this big first asks "one-off or create a group?" (default 4) |
| `BUDGET_ALERT_THRESHOLDS` | default `50,80,100` |
| `TIMEZONE` | decides what "today" and "this month" mean |

## 7. Safety and reliability

| Concern | How it is handled |
|---|---|
| LLM answers non-expense questions | Prompt rules **plus** code: the model may only act through tools, and non-expense replies are fixed text. Free text from the model is replaced by the refusal. |
| Wrong money maths | The LLM never calculates; `core` does, in one DB transaction with row locks. Shares are exact to the paisa. |
| Strangers using the bot | Allow-list for private use; the bot **leaves** a group if a non-listed person adds it. Group members can use it only inside that group and for their own bills. |
| Reading a group's private chat | It only acts on @mentions, replies and commands; with BotFather privacy mode ON Telegram does not even deliver other messages. |
| Double write after a pause | One tool call per graph step plus checkpoints. |
| Double tap on I paid / Import | Rows are locked and their status flips once. |
| Duplicate statement rows | Stable key per row; look-alikes of hand-entered expenses are flagged. |
| People the bot cannot message | They are tagged in the group with a Start button; the bill is delivered the moment they press Start. |
| Hanging or slow LLM | 25 s per request, 60 s per message, a friendly rate-limit message. |
| Prompt injection inside a PDF | The parser LLM has no tools; every date and amount is re-validated in code. |
| Secrets | `.env` is git-ignored; httpx logging is silenced (its URLs contain the bot token). |
