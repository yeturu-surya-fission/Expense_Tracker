# Presentation diagrams

Three simple diagrams. They render on GitHub and in VS Code (Mermaid preview). Paste any block into https://mermaid.live to export a PNG/SVG for slides.

---

## 1. Architecture: two agents, three services, three databases

```mermaid
flowchart LR
    EMP([👤 Employee<br/>Telegram]):::person
    MGR([👔 Manager<br/>Telegram]):::person
    LLM[["🧠 LLM<br/>reads text only"]]:::llm

    subgraph TRACKER["💳 EXPENSE TRACKER AGENT"]
        direction TB
        subgraph BOT["Bot service :8000 (the brain)"]
            direction TB
            H["Handlers<br/>commands · files · buttons"]
            G["LangGraph<br/>agent → tools → respond"]
            H --> G
        end
        subgraph MCP["MCP server :8001 (the hands)"]
            direction TB
            T["Tools<br/>expenses · splits · budgets · work claims"]
            A["JWT auth · audit<br/>idempotency · outbox"]
        end
        G -- "calls tools<br/>(MCP, user's JWT)" --> T
        DB1[("Bot DB<br/>chat memory")]:::db
        DB2[("Tracker DB<br/>expenses · claims · receipts")]:::db
        G --- DB1
        T --- DB2
    end

    subgraph FIN["🏢 FINANCE AGENT :8002"]
        direction TB
        P["Policy engine<br/>exact money maths"]
        C["Claims + manager bot"]
        DB3[("Finance DB<br/>policies · claims · receipts")]:::db
        P --- DB3
        C --- DB3
    end

    EMP <--> H
    G <-.-> LLM
    P <-.-> LLM
    TRACKER == "A2A (short-lived JWT)<br/>check_expense · submit_claim · ask_policy" ==> FIN
    FIN == "signed webhook (HMAC)<br/>decision · question" ==> TRACKER
    C <--> MGR

    classDef person fill:#fff3cd,stroke:#b58900
    classDef llm fill:#e8defa,stroke:#6f42c1
    classDef db fill:#d1ecf1,stroke:#0c5460
```

**Read it in one breath:** the **Expense Tracker Agent** is the bot (brain: chat + LangGraph) plus the MCP server (hands: tools + data). The employee chats with the bot; its AI picks a tool; the tool runs on the MCP server. Work expenses and claims go over **A2A** from the tracker agent to the **Finance Agent**, which applies the company policy and asks the **manager** in their own Telegram bot. The decision comes back by **signed webhook**.

| Rule that makes it safe | How |
|---|---|
| AI never does money maths | the policy engine uses exact `Decimal`; the LLM only reads text |
| Manager always decides | nothing is paid or saved without a button press |
| No lost messages | outbox tables in both services, retried until delivered |
| Each service owns its data | links between services are plain ids, not shared tables |

---

## 2. The state graph: how one message moves

The LangGraph has three nodes. Everything around it decides whether a message reaches it at all.

```mermaid
flowchart TD
    MSG([Telegram message]):::start --> KIND{What is it?}

    KIND -- "button tap" --> CB["Resume a paused question<br/>or run a button action"]
    KIND -- "/command<br/>(/work /personal /workpdf /join ...)" --> CMD["Handler runs the tool directly<br/>or adds a WORK/PERSONAL prefix"]
    KIND -- "PDF or photo" --> FILE{"Waiting for<br/>a receipt?"}
    KIND -- "plain text" --> GRAPH

    FILE -- yes --> REC["Attach receipt to that expense"]
    FILE -- no --> STMT["Import as bank statement<br/>(preview → confirm)"]
    CMD --> GRAPH

    subgraph GRAPH["LangGraph state graph"]
        direction TB
        AG["agent<br/>LLM picks a tool"]:::node
        TL["tools<br/>one call per step, via MCP"]:::node
        RS["respond<br/>builds reply from tool output"]:::node
        AG -- "asked for tools" --> TL
        TL -- "more calls waiting" --> TL
        TL -- "a tool failed" --> AG
        TL -- "all OK" --> RS
        AG -- "free text only" --> REFUSE["Fixed refusal<br/>(off-topic is never answered)"]
    end

    TL -. "server needs an answer<br/>(input_required)" .-> PAUSE["⏸ interrupt():<br/>ask user, nothing written yet"]
    PAUSE -. "user replies" .-> TL

    RS --> OUT([Reply to user]):::end_
    REFUSE --> OUT
    REC --> OUT
    STMT --> OUT
    CB --> OUT

    classDef start fill:#fff3cd,stroke:#b58900
    classDef end_ fill:#d4edda,stroke:#28a745
    classDef node fill:#f2f0ff,stroke:#6f42c1
```

### Every case this covers

| Case | What happens |
|---|---|
| Normal expense ("coffee 120") | agent → tools (add_expense) → respond |
| Several actions in one message | tools loops, one tool per step, each checkpointed |
| Missing detail (which friend? which category?) | server returns `input_required` → graph pauses → user picks → same tool re-called, so no double write |
| Tool error | goes back to agent once to fix arguments |
| Off-topic ("write me code") | LLM free text is dropped, fixed refusal sent |
| Tool not allowed in this chat (group vs private) | error result, never executed |
| Same request sent twice | idempotency key makes the second a no-op |
| Cancel in the middle | answer "cancel" stops the tool, nothing saved |
| Bank statement PDF | handler parses, shows preview, imports only after confirm; undoable as a group |
| Receipt photo/PDF while a work expense waits | stored with that expense, not imported as a statement |

---

## 3. Work expense and claim: the full lifecycle

This is the second state machine. It runs across tracker and finance agent.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Added : /work flight 3000

    state "Up-front check (A2A check_expense)" as Check {
        direction TB
        [*] --> Ask
        Ask : Policy needs receipt / nights / city?
        Ask --> Waiting : yes, ask employee
        Waiting --> Ask : employee answers or sends receipt
        Ask --> Ready : nothing missing
    }

    Added --> Check
    Check --> Draft : expense saved with its receipt

    Draft --> sending : "send my claim"<br/>(blocked if anything unfinished)
    sending --> failed : finance agent unreachable
    sending --> needs_info : policy still needs details
    needs_info --> awaiting_manager : employee answers
    sending --> awaiting_manager : complete

    awaiting_manager --> info_requested : manager asks a question
    info_requested --> awaiting_manager : employee replies
    awaiting_manager --> approved : manager approves<br/>(policy amount, full, or per expense)
    awaiting_manager --> rejected : manager rejects with reason
    approved --> paid : manager marks paid
    rejected --> [*]
    paid --> [*]
    failed --> Draft : retry
```

### Who does what at each step

| Step | Employee side (tracker + bot) | Finance side |
|---|---|---|
| Add expense | saves `work_expenses` row | `check_expense` reads policy, returns missing items; saves nothing |
| Send claim | `work_claims` row + receipts | stores claim, runs policy engine per item, saves `claims` + receipts |
| Needs info | asks employee in the bot | task state `input-required` |
| Manager review | waits | manager card with receipts and policy verdict per item |
| Different amount | employee told which expenses were cut | manager decides expense by expense |
| Decision | updated via signed webhook, employee notified | `webhook_events` row retried until accepted |
| Policy changes | `/policy` Q&A | manager uploads PDF/text → draft → **Save** → new version |
