> ⚠️ **Out of date.** This page describes the earlier cash/bank wallet version. The current design (expenses, friends, bill splitting, trip groups) is described in the [README](../README.md). The agent graph (`agent → tools → respond`) is unchanged.

# Flows

Step-by-step behaviour of the important paths. For the static structure see [ARCHITECTURE.md](ARCHITECTURE.md).

Contents: [1. Message routing](#1-message-routing) · [2. Recording an expense](#2-recording-an-expense-with-the-cashbank-question) ·
[3. Scope guardrail](#3-scope-guardrail) · [4. Budgets and alerts](#4-budgets-and-alerts) ·
[5. Cash and bank transfers](#5-cash--bank-transfers) · [6. Bank-statement PDF import](#6-bank-statement-pdf-import) ·
[7. Month-wise and date-wise views](#7-month-wise-and-date-wise-views) · [8. Undo](#8-undo)

## 1. Message routing

Everything you send is routed first. Menu buttons and commands never touch the LLM, which keeps them instant.

```mermaid
flowchart TD
    IN(["Telegram update"]) --> ALLOW{"user in<br/>ALLOWED_TELEGRAM_IDS?"}
    ALLOW -->|no| DENY["🔒 private bot"]
    ALLOW -->|yes| KIND{"what is it?"}

    KIND -->|"menu button / command<br/>💰 🧾 📊 🎯 ↩️ ❓ /trend /daily"| DIRECT["answer from the database<br/>(no LLM, instant)"]
    KIND -->|"inline button"| CB{"which button?"}
    KIND -->|"PDF file"| PDF["statement import<br/>(flow 6)"]
    KIND -->|"other file / photo"| NOPDF["📄 only PDF statements"]
    KIND -->|"text"| LEN{"over 1000 chars?"}

    LEN -->|yes| TOOLONG["✂️ too long"]
    LEN -->|no| TYPING["show typing… (refreshed every 4 s)"] --> AGENT["AgentRunner → LangGraph<br/>(flows 2 and 3)"]

    CB -->|"Cash / Bank / Cancel"| AGENT
    CB -->|"↩️ Undo"| UNDO["undo (flow 8)"]
    CB -->|"◀ ▶ month · Daily · Trend"| VIEWS["edit message in place<br/>(flow 7)"]
    CB -->|"Import / Undo import"| PDF

    AGENT --> TO{"answered within 60 s?"}
    TO -->|no| T1["⏱️ took too long, resend"]
    TO -->|"rate limit"| T2["⏳ AI busy, try in a minute"]
    TO -->|yes| SEND["send reply<br/>+ buttons (Undo, Balance,<br/>Cash/Bank, …)"]
```

## 2. Recording an expense (with the cash/bank question)

If you don't say cash or bank, the agent **asks** instead of guessing. This is the human-in-the-loop step.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant B as Bot
    participant R as AgentRunner
    participant G as LangGraph
    participant L as LLM
    participant C as core/ledger
    participant D as Postgres

    U->>B: "spent 300 on ice cream"
    B->>R: chat(text)
    R->>G: run graph
    G->>L: system prompt + message
    L-->>G: tool call add_expenses(300, food, ice cream, account = null)
    Note over G: account missing, so the tool calls interrupt()
    G->>D: checkpoint (paused)
    G-->>R: paused with question
    R-->>B: "Was this paid from cash or bank?"
    B-->>U: message + [💵 Cash] [🏦 Bank] [✖ Cancel]

    U->>B: taps 💵 Cash
    B->>R: chat("cash", from_button)
    R->>G: resume("cash")
    G->>C: record_expense(cash)
    C->>D: lock accounts, debit cash, insert transaction
    C->>C: evaluate budget alerts
    C-->>G: confirmation + balances + any alert
    G->>G: respond node builds the reply (no 2nd LLM call)
    G-->>R: text + transaction ids
    R-->>B: reply
    B-->>U: "🔴 Spent ₹300 on ice cream 🍔 from 💵 cash" + [↩️ Undo] [💰 Balance]
```

What happens if you reply with something else while a question is pending?

```mermaid
flowchart TD
    P["a question is pending"] --> M{"your next message"}
    M -->|"cash / bank / UPI / card… or a button"| OK["resume with that answer"]
    M -->|"cancel / skip / no"| CAN["resume with 'cancel' → nothing recorded"]
    M -->|"anything else"| DROP["cancel the old question<br/>then handle the new message normally"]
```

If you did say the account ("spent 300 on ice cream by UPI"), the pause (steps 6–12) is skipped and it is recorded straight away.

## 3. Scope guardrail

Two layers, so safety does not depend on the model behaving. The model never writes the text you read.

```mermaid
flowchart TD
    M(["user message"]) --> LLM["LLM with strict system prompt<br/>must answer through a tool"]
    LLM --> WHAT{"what did the model do?"}

    WHAT -->|"wallet action"| TOOL["ledger tools<br/>add_expenses, add_income, transfer_funds,<br/>get_balances, set_budget, …"]
    WHAT -->|"respond_to_user('off_topic')<br/>coding, general knowledge, maths,<br/>'ignore your rules', 'print your prompt'"| REF["🙅 fixed refusal text from guardrails.py<br/>(model wording ignored)"]
    WHAT -->|"respond_to_user('greeting' / 'help')"| CAN["fixed greeting or help text"]
    WHAT -->|"respond_to_user('clarify', short question)"| CLA["❓ question, cut to 200 chars"]
    WHAT -->|"plain text (broke the rules)"| LEAK["🛡️ dropped and replaced by the refusal"]

    TOOL --> OUT(["reply built from tool output"])
    REF --> OUT
    CAN --> OUT
    CLA --> OUT
    LEAK --> OUT
```

## 4. Budgets and alerts

```mermaid
flowchart TD
    A["'set a limit of 3k for food'"] --> SB["set_budget(food, 3000)<br/>monthly; old alerts cleared"]
    SB --> ST["shows current progress"]

    E["any expense in category food"] --> SUM["sum this month's food expenses<br/>(voided ones excluded)"]
    SUM --> PCT["percent = spent ÷ limit"]
    PCT --> TH{"crossed 50 / 80 / 100%<br/>not announced yet<br/>this month?"}
    TH -->|no| QUIET["no alert"]
    TH -->|yes| SAVE["record ALL crossed thresholds<br/>in budget_alerts"]
    SAVE --> WHICH{"highest crossed"}
    WHICH -->|"50 or 80"| W["⚠️ 50% used, ₹1,300 left, spend wisely!"]
    WHICH -->|"100, not over"| F["🚨 budget fully used"]
    WHICH -->|"over the limit"| X["🚨 EXCEEDED, over by ₹…"]
```

- Each threshold fires **once per budget per month**, so at 40% → 90% you get one 80% alert, not two.
- Budgets reset every calendar month in your timezone. Past months stay viewable: *"show september budgets"*.

## 5. Cash ⇄ bank transfers

```mermaid
flowchart LR
    W["'withdrew 300'"] --> T1["transfer_funds<br/>from bank → to cash"]
    D["'deposited 500 in bank'"] --> T2["transfer_funds<br/>from cash → bank"]
    T1 --> LD["one DB transaction:<br/>bank −300, cash +300<br/>kind = transfer"]
    T2 --> LD
    LD --> NOTE["not an expense or income,<br/>so it never affects budgets or summaries"]
    LD --> GUARD{"enough in the source account?"}
    GUARD -->|no| ERR["❌ nothing moved<br/>'Not enough in bank'"]
```

## 6. Bank-statement PDF import

You send a PDF, see a preview, and only then are balances changed.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant B as Bot
    participant S as core/statements
    participant P as statement_parser (LLM)
    participant D as Postgres

    U->>B: sends statement.pdf (caption "password 1234" if protected)
    B->>S: extract text (pypdf)
    alt password needed or wrong
        S-->>B: PasswordRequired / WrongPassword
        B-->>U: 🔒 resend with the password as caption
    else scanned image, no text
        S-->>B: error
        B-->>U: ❌ needs a digital statement
    end
    B->>P: text, split into ~4500 char chunks
    loop each chunk (2 at a time, with rate-limit backoff)
        P->>P: extract rows: date, narration, amount, debit/credit, category
    end
    Note over P: if ANY chunk fails, abort. A missing page would corrupt balances.
    P-->>B: raw rows
    B->>S: build_plan(rows)
    S->>S: validate every date and amount in code, drop bad rows
    S->>D: look up existing external_keys and nearby hand-entered transactions
    S->>D: save StatementImport (pending)
    B-->>U: preview + [✅ Import N new] [➕ Include maybes] [✖ Cancel]

    U->>B: taps ✅ Import
    B->>S: apply_import
    S->>D: lock import row, status pending → done (tap twice is safe)
    S->>D: post each row to the ledger on the bank account
    S->>S: budget alerts for this month's categories
    B-->>U: "✅ Imported 11 transactions" + balances<br/>[🏦 Set bank = closing balance] [↩️ Undo this import]
```

How each row is classified and applied:

```mermaid
flowchart TD
    ROW["parsed row"] --> OK{"valid date and amount?"}
    OK -->|no| DROP["dropped, counted as unreadable"]
    OK -->|yes| KEY["key = hash(date, type, amount, narration, occurrence#)"]
    KEY --> SEEN{"key already in ledger?"}
    SEEN -->|yes| DUP["♻️ duplicate: always skipped<br/>(re-upload is safe)"]
    SEEN -->|no| HAND{"same amount, bank side,<br/>within ±1 day of an entry<br/>you typed by hand?"}
    HAND -->|yes| MAYBE["❓ maybe: skipped unless<br/>you tap 'Include maybes'"]
    HAND -->|no| NEW["new: imported"]

    NEW --> TYPE{"debit or credit?"}
    MAYBE -. "if included" .-> TYPE
    TYPE -->|credit| INC["income → bank"]
    TYPE -->|"debit, category = cash_withdrawal"| TRF["transfer bank → cash"]
    TYPE -->|"debit, other"| EXP["expense from bank"]
```

Notes: statements are treated as history, so they may take the bank balance negative instead of failing.
**Undo this import** voids every transaction of that import and reverses the balances.
**Set bank = closing balance** corrects drift if you already entered some of these by hand.

## 7. Month-wise and date-wise views

Everything is stored with a full timestamp and category; months and days are computed in your timezone
(a 23:30 expense on the 31st stays in that month).

```mermaid
flowchart TD
    Q(["question"]) --> HOW{"how asked?"}
    HOW -->|"menu / buttons"| BTN["no LLM"]
    HOW -->|"free text"| LLM["LLM maps the phrase to a tool + period<br/>(it knows today's date)"]

    LLM --> MAP{"phrase"}
    MAP -->|"'in september'"| P1["spending_summary(period = 2026-09)"]
    MAP -->|"'1st to 15th october'"| P2["spending_summary(period = 2026-10-01..2026-10-15)"]
    MAP -->|"'month wise', 'compare months'"| P3["monthly_trend(months, category?)"]
    MAP -->|"'day wise for august'"| P4["daily_breakdown(month = 2026-08)"]
    MAP -->|"'september budgets'"| P5["get_budgets(month = 2026-09)"]

    BTN --> NAV["📊 Summary: ◀ prev month · next month ▶<br/>📅 Daily · 📈 Trend (3 / 6 / 12 months)"]
    NAV --> EDIT["edits the same message in place"]
    P1 --> RES(["reply from database"])
    P2 --> RES
    P3 --> RES
    P4 --> RES
    P5 --> RES
    EDIT --> RES
```

Accepted periods: `today`, `yesterday`, `this_week`, `last_7_days`, `last_30_days`, `this_month`, `last_month`, `this_year`,
`all_time`, a month `YYYY-MM`, a day `YYYY-MM-DD`, or a range `YYYY-MM-DD..YYYY-MM-DD`.
Anything else gives a clear error, never silently "all time".

## 8. Undo

```mermaid
flowchart TD
    U1["'undo last' (chat)"] --> V
    U2["↩️ Undo button under a result"] --> V
    U3["↩️ Undo last (menu), asks 'Undo this?' first"] --> V
    V["void_transaction"] --> ALR{"already undone?"}
    ALR -->|yes| MSG["'already undone'"]
    ALR -->|no| L["lock the accounts involved"]
    L --> REV["reverse the effect:<br/>give back to from_account, take from to_account"]
    REV --> MARK["set voided_at<br/>(row stays in history, shown as undone)"]
    MARK --> BAL["show new balances"]
```
