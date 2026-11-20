# How it started: five interns and a very ordinary problem

It did not begin with an architecture diagram. It began with a group chat.

We are five Python interns at Fission Labs. Like most teams, we ate together, travelled together and kept a running tab in our heads: who paid for the chai, who covered the cab, who still owed whom from last Friday. The tab lived in memory, and memory is a terrible ledger. Somebody always remembered it differently.

The obvious answer was an expense app. We had all tried one, and we all abandoned it for the same reason: opening an app, picking a category and typing an amount is more work than the expense itself. The one place we all already were, all day, was Telegram. So the first question was almost silly: what if you could just _say_ it there? "Spent 300 on ice cream." Done.

## Version one: just say what you spent

The first prototype was small and a little reckless. A Telegram bot, an LLM that turned a sentence into a record, and a Postgres table. It worked well enough to be dangerous. The first time it misread an amount we learned the lesson that shaped everything after it: **a language model is a great translator and a terrible accountant.** From then on the model could read a sentence and choose a tool, and plain code did the arithmetic.

## Version two: friends, splits and the end of "who owes whom"

Once personal expenses worked, the thing we actually wanted came next. Split the dinner. Remember who paid. Show one total per person instead of a pile of tiny debts, and cancel out the ones that offset each other. Group chats followed, because that is where the dinners are planned.

That growth is where the single-file bot stopped being acceptable. Telegram code, agent code and SQL were tangled together, and every new idea touched all three. So we split the system: an MCP server that owns the data and knows nothing about Telegram, and a bot that owns the conversation. We added a test that fails the build if either side crosses the line. Five people working in one repository need rules the compiler can enforce, not rules we have to remember.

## Version three: the question from the other side of the office

Then a teammate asked the question that changed the project: _"Can it do my work expenses? Will I actually get this hotel back?"_

Reimbursements are not a personal ledger. They have a company policy, a manager and a wait that can last days. We did not want to cram that into the tracker, so we gave it to its own service: a Finance Agent with its own database and its own bot for the manager, talking to the tracker over A2A. A manager pastes the policy, the agent reads it back for confirmation, and from then on every claim is checked against real rules, with the arithmetic done by code and the final decision left to a person.

## What we took from it

We came in as interns who knew Python. We left having shipped a multi-agent system with real auth, real protocols and tests that guard its architecture. More important than any one feature, we learned how to split work across five people without stepping on each other, which turned out to be mostly about drawing the right boundaries early.

It started with ice cream. It grew because every time it worked, someone on the team asked for the next thing.
