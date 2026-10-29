# Flows

Step-by-step behaviour of every important path. For the static structure see [ARCHITECTURE.md](ARCHITECTURE.md); for how the AI agent
loops see [STATE_GRAPH.md](STATE_GRAPH.md).

Contents: [1. Message routing](#1-message-routing) · [2. Recording an expense](#2-recording-an-expense) ·
[3. Splitting a bill with friends](#3-splitting-a-bill-with-friends) · [4. The payment lifecycle](#4-the-payment-lifecycle-i-paid--received) ·
[5. Several bills, different payers](#5-several-bills-different-payers-a-party-night) · [6. Group chats: setup](#6-group-chats-setup-and-who-is-a-member) ·
[7. Group chats: split the bill](#7-group-chats-split-the-bill) · [8. Bank-statement PDF](#8-bank-statement-pdf-import) ·
[9. Budgets and alerts](#9-budgets-and-alerts) · [10. Scope guardrail](#10-scope-guardrail) · [11. Reminders](#11-reminders) ·
[12. Reports](#12-reports-and-periods)

## 1. Message routing

Everything you send is routed first. Menu buttons and commands never touch the LLM, which keeps them instant.

```mermaid
flowchart TD
    IN(["Telegram update"]) --> WHERE{"where?"}

    WHERE -->|"private chat"| PALLOW{"allowed?<br/>allow-list, a friend of a listed person,<br/>or a member of a group"}
    PALLOW -->|no| DENY["🔒 private bot"]
    PALLOW -->|yes| PKIND{"what is it?"}
    PKIND -->|"menu button / /command"| DIRECT["answer from the database<br/>(no LLM, instant)"]
    PKIND -->|"PDF file"| PDF["statement import (flow 8)"]
    PKIND -->|"text"| LEN{"over 1000 chars?"}
    LEN -->|yes| TOOLONG["✂️ too long"]
    LEN -->|no| AGENT["AgentRunner → LangGraph<br/>(private tools)"]
    PKIND -->|"button tap"| CB["route by the button:<br/>answer a question · I paid · Received · Undo · month ◀ ▶"]

    WHERE -->|"group chat"| GKIND{"what is it?"}
    GKIND -->|"bot added / removed"| SETUP["set up the group (flow 6)<br/>or leave if a stranger added it"]
    GKIND -->|"someone joined / left / renamed / upgraded"| MEMB["update members and name (flow 6)"]
    GKIND -->|"/group, /settle"| GCMD["answer without the AI"]
    GKIND -->|"ordinary message"| ADDR{"@mention, or a reply<br/>to the bot?"}
    ADDR -->|no| IGN["ignored: no reply, no AI call"]
    ADDR -->|yes| GAGENT["AgentRunner → LangGraph<br/>(group tools; the speaker becomes a member)"]

    AGENT --> TO{"answered within 60 s?"}
    GAGENT --> TO
    TO -->|no| T1["⏱️ took too long"]
    TO -->|"rate limit"| T2["⏳ AI busy"]
    TO -->|yes| SEND["send the reply + buttons<br/>(Undo, Cancel split, Equally / amounts questions)"]
    SEND --> TAG{"some people could not<br/>be messaged privately?"}
    TAG -->|yes| START["tag them in the group<br/>with a Start button"]
```

## 2. Recording an expense

No account questions: it just records.

```mermaid
sequenceDiagram
    autonumber
    actor U as You
    participant B as Bot
    participant G as LangGraph + LLM
    participant C as core
    participant D as Postgres

    U->>B: "spent 300 on ice cream"
    B->>G: run the graph
    G->>G: agent: LLM picks add_expenses(300, food)
    G->>C: tools: add_expense
    C->>D: insert the expense
    C->>C: budget check (50 / 80 / 100%)
    C-->>G: "Spent ₹300 on ice cream" + any alert
    G-->>B: respond builds the reply (+ Undo button)
    B-->>U: reply
```

## 3. Splitting a bill with friends

"went to dining, paid 3k, split with Surya" — if you didn't say *how*, the bot asks.

```mermaid
sequenceDiagram
    autonumber
    actor U as You
    participant B as Bot
    participant G as Graph
    participant C as core
    actor F as Surya (private chat)

    U->>B: "dining 3k, split with Surya"
    B->>G: run
    G->>G: LLM: split_expense(3000, with Surya) — no split method said
    Note over G: the tool pauses: interrupt() saved in Postgres
    G-->>B: question
    B-->>U: "How should I split ₹3,000?"  [Equally] [I'll tell the amounts] [Cancel]
    U->>B: taps Equally
    B->>G: resume("equal")
    G->>C: create the split: ₹1,500 each, you included
    C->>C: your ₹1,500 goes into your expenses now
    C->>C: a debt: Surya owes you ₹1,500
    C-->>F: "Kiran paid for dining. Your share: ₹1,500."  [✅ I paid]
    G-->>B: reply + Cancel-split button
    B-->>U: "Surya owes ₹1,500 ✓ notified"
```

Other questions the bot may ask: *one-off or create a group?* (4 or more people), *equally, always equally in this group, or amounts?* (group
expenses), *settle up now?* (before settling a group). A typed answer works too ("equally"), and an unrelated reply simply cancels the
question and handles your new message.

## 4. The payment lifecycle ("I paid" → "Received")

```mermaid
stateDiagram-v2
    [*] --> pending : the debt is created
    pending --> claimed : debtor taps I paid, their share is saved to THEIR expenses
    claimed --> settled : the person owed taps Received
    claimed --> pending : the person owed taps Not yet, the expense is removed again
    pending --> cancelled : the split is cancelled before anyone paid
    settled --> [*]
    cancelled --> [*]
```

- The **bill message disappears** from the debtor's chat when they tap **I paid**, and a short confirmation replaces it.
- If a person owes **two people** (see flow 5), their whole share is saved **once**, on their first payment. If one of those
  payments is rejected but the other still stands, the expense stays; it is removed only when none stands.
- Tapping twice, or the wrong person tapping, is refused safely.

## 5. Several bills, different payers (a party night)

*"I paid 2500 for food, Surya paid 600 for ice cream, Komal paid 3500 for the bar. Who pays whom?"*
One `split_bills` call nets everything instead of creating one debt per bill.

```mermaid
flowchart TD
    M["one message with 3 bills"] --> ONE["ONE split_bills call<br/>(all bills, who paid each)"]
    ONE --> SH["each bill shared equally by everyone<br/>total ₹6,600 → ₹2,200 each"]
    SH --> NET["net = paid − share, per person<br/>Kiran 2500−2200 = +300<br/>Surya 600−2200 = −1600<br/>Komal 3500−2200 = +1300"]
    NET --> TR["fewest payments:<br/>Surya → Komal ₹1,300<br/>Surya → Kiran ₹300"]
    TR --> OUT["reply: WHO PAYS WHOM first,<br/>then each person's maths with the bills in brackets"]
    TR --> DM["private bills with the items<br/>and an I paid button"]
    NET --> OWE0["people who owe nothing:<br/>their share is saved to their expenses now"]
```

The reply looks like this (every number can be checked: *owes = share − paid*):

```
💸 Who pays whom
• surya → Komal Kumar  ₹1,300
• surya → You  ₹300

📋 Each person  (owes = share − paid)
• surya owes ₹1,600
   (share ₹2,200: party food ₹833.33, ice cream ₹200, bar ₹1,166.67 · paid ₹600: ice cream ₹600)
• Komal Kumar gets back ₹1,300 …
• You get back ₹300 …
```

## 6. Group chats: setup and who is a member

There is **no `/creategroup` and no `/join`**. The owner creates the Telegram group, adds friends, and adds the bot.

```mermaid
flowchart TD
    ADD["owner adds the bot to a Telegram group"] --> WHO{"is the person who added it<br/>on the allow-list?"}
    WHO -->|no| LEAVE["bot says sorry and leaves"]
    WHO -->|yes| MAKE["create the expense group<br/>(named after the chat)"]
    MAKE --> M1["member: the person who added the bot"]
    MAKE --> M2["members: the adder's friends who are in the chat<br/>(asked from Telegram, best effort: reliable if the bot is an admin)"]
    MAKE --> WEL["post a welcome: how to mention me,<br/>who I know, 'mention me once if I don't know you'"]

    subgraph LATER["Afterwards the member list keeps itself up to date"]
        J["'X joined' notice → add X"]
        L["'X left' notice → leave X out of future splits"]
        SP["anyone @mentions the bot → add them"]
        RN["chat renamed → rename the group"]
        UP["group upgraded to a supergroup → keep the same group, new chat id"]
    end
```

**Why members are learned instead of listed:** a bot cannot list a group's members, so people who never speak and aren't the owner's friends
can't be seen. Before a split the bot compares what it knows with Telegram's member count and warns: *"I only know 3 of the 5 people, the
others should mention me once."*

**Access:** everyone in the group can use the bot there, by **@mentioning it** (or replying to it, or `/group`, `/settle`). Ordinary chatter is
never read or answered. Group members can also use the bot privately (for their own bills), but cannot add it to other groups.

## 7. Group chats: split the bill

```mermaid
sequenceDiagram
    autonumber
    actor A as Kiran (in the group)
    actor S as Surya (in the group)
    participant B as Bot
    participant C as core
    actor K as Komal (has not pressed Start)

    A->>B: "@bot I paid 60 for coffee"
    B->>C: add_group_expense, shared by all members
    B-->>A: "K Gopal paid ₹60 · Split equally among 3 (₹20 each)"
    S->>B: "@bot I paid 100 for tiffins, I ate dosa 40, Kiran and Komal idly 30 each"
    B-->>S: "Split: Surya ₹40, Kiran ₹30, Komal ₹30"
    A->>B: "@bot split the bill"   (or /settle)
    B-->>A: "Settle up Goa trip now?  [Yes, settle up]"
    A->>B: taps Yes
    B->>C: settle: net per person, fewest payments
    B-->>A: group message: who pays whom, then each person's maths
    C-->>S: private bill with the items + [I paid]  (if Surya owes)
    B-->>K: tags Komal in the group: "press Start" [Start the bot]
    K->>B: presses Start
    B-->>K: her bill arrives immediately (with the items)
```

- If the split isn't stated for an expense the bot asks (*equally / always equally / amounts*); "always equally" stops the question for this group.
- "Surya paid 600" is credited to **Surya**, not to whoever typed it.
- People who owe nothing are told what they're owed, and everyone's share is saved to their own expenses (immediately, or when they pay).

Example with real numbers (coffee 60 by Gopal, tiffins 100 by Surya, cool drinks 80 by Komal; shares as typed):

| | Share | Paid | Share − paid |
|---|---|---|---|
| K Gopal | 20 + 30 + 20 = 70 | 60 | **owes 10** |
| Surya | 20 + 40 + 40 = 100 | 100 | even |
| Komal Kumar | 20 + 30 + 20 = 70 | 80 | **gets back 10** |

→ one payment: **K Gopal → Komal Kumar ₹10**.

## 8. Bank-statement PDF import

```mermaid
flowchart TD
    P["you send a PDF<br/>(caption 'password 1234' if protected)"] --> T["extract the text (pypdf)"]
    T -->|"password needed / wrong"| PW["🔒 resend with the password"]
    T -->|"scanned, no text"| SC["❌ needs a digital statement"]
    T --> LLM["LLM reads the rows in chunks (2 at a time)<br/>date · amount · debit/credit · category"]
    LLM -->|"any chunk fails"| FAIL["abort: nothing imported<br/>(a missing page would be wrong)"]
    LLM --> V["code validates every date and amount"]
    V --> CL{"each row"}
    CL -->|"money in"| S1["skipped (this tracks spending only)"]
    CL -->|"ATM withdrawal"| S2["skipped (log what you spend the cash on)"]
    CL -->|"already imported"| S3["duplicate: skipped"]
    CL -->|"same amount ±1 day as a hand entry"| S4["maybe: skipped unless you include it"]
    CL -->|"a debit"| NEW["new expense"]
    NEW --> PREV["preview with totals + buttons<br/>[Import N new] [Include maybes] [Cancel]"]
    PREV --> APPLY["on confirm: expenses saved,<br/>budget alerts checked, [Undo this import]"]
```

## 9. Budgets and alerts

```mermaid
flowchart TD
    A["'set a limit of 3k for food'"] --> SB["set_budget(food, 3000)<br/>monthly; old alerts cleared"]
    E["any expense in category food<br/>(your own, or your share of a split once recorded)"] --> SUM["sum this month's food expenses<br/>(undone ones excluded)"]
    SUM --> PCT["percent = spent ÷ limit"]
    PCT --> TH{"crossed 50 / 80 / 100%<br/>not announced yet<br/>this month?"}
    TH -->|no| QUIET["no alert"]
    TH -->|yes| SAVE["record ALL crossed thresholds"]
    SAVE --> WHICH{"highest crossed"}
    WHICH -->|"50 or 80"| W["⚠️ 50% used, ₹1,300 left, spend wisely!"]
    WHICH -->|"100, not over"| F["🚨 budget fully used"]
    WHICH -->|"over the limit"| X["🚨 EXCEEDED, over by ₹…"]
```

Each threshold fires **once per budget per month**. When a friend taps **I paid**, the share is checked against **their** budgets.

## 10. Scope guardrail

The model never writes the text you read. Anything that is not about your expenses, friends or shared bills is refused with fixed wording.

```mermaid
flowchart TD
    M(["message"]) --> LLM["LLM with a strict prompt<br/>must answer through a tool"]
    LLM --> WHAT{"what did the model do?"}
    WHAT -->|"an expense / split / group tool"| TOOL["run it"]
    WHAT -->|"respond_to_user('off_topic')<br/>coding, general questions,<br/>'ignore your rules', 'print your prompt'"| REF["🙅 fixed refusal"]
    WHAT -->|"respond_to_user('greeting' / 'help')"| FIX["fixed greeting or help"]
    WHAT -->|"respond_to_user('clarify', short question)"| CLA["❓ cut to 200 characters"]
    WHAT -->|"plain text (broke the rules)"| LEAK["🛡️ dropped, replaced by the refusal"]
```

## 11. Reminders

```mermaid
flowchart LR
    LOOP["background loop<br/>every 30 min"] --> Q["unpaid debts older than<br/>REMINDER_EVERY_HOURS (48)"]
    Q --> D["debtor: delete the old bill message,<br/>send a fresh one with the items + [I paid]"]
    LOOP --> Q2["'says paid' debts nobody confirmed"]
    Q2 --> C["person owed: 'Did you receive it?'<br/>[Received] [Not yet]"]
    D --> STOP["stops when they tap I paid"]
    C --> STOP2["stops when they confirm"]
```

## 12. Reports and periods

Everything is stored with a full timestamp and category; months and days are computed in your timezone (a 23:30 expense on the 31st stays in that month).

```mermaid
flowchart TD
    Q(["question"]) --> HOW{"how asked?"}
    HOW -->|"menu / buttons"| BTN["no LLM"]
    HOW -->|"free text"| LLM["LLM maps the phrase to a tool + period<br/>(it knows today's date)"]
    LLM --> MAP{"phrase"}
    MAP -->|"'in september'"| P1["spending_summary(period = 2026-09)"]
    MAP -->|"'1st to 15th october'"| P2["spending_summary(2026-10-01..2026-10-15)"]
    MAP -->|"'month wise'"| P3["monthly_trend"]
    MAP -->|"'day wise for august'"| P4["daily_breakdown(2026-08)"]
    BTN --> NAV["📊 Summary: ◀ month ▶ · 📅 Daily · 📈 Trend<br/>edits the same message in place"]
```

Accepted periods: `today`, `yesterday`, `this_week`, `last_7_days`, `last_30_days`, `this_month`, `last_month`, `this_year`, `all_time`,
a month `YYYY-MM`, a day `YYYY-MM-DD`, or a range `YYYY-MM-DD..YYYY-MM-DD`. Anything else gives a clear error.
