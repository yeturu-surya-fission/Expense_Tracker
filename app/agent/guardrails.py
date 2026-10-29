"""
Scope guardrails. The LLM never writes free-form answers: for anything that is not an expense / split action it
must call `respond_to_user`, and the wording of those replies comes from the constants below, not from the model.
"""

SCOPE_MESSAGE = (
    "🙅 That's outside my job - I only handle your expenses:\n"
    "spending, budgets, summaries, splitting bills with friends, trip groups and bank-statement PDFs.\n\n"
    "Try: spent 200 on lunch"
)

GREETING = "👋 Hi! Tell me what you spent, e.g. \"spent 200 on lunch\", split a bill with friends, or send a bank-statement PDF."

HELP = (
    "👋 I'm your expense tracker. Just talk to me:\n\n"
    "🔴 spent 300 on ice cream\n"
    "🎯 set a limit of 3k for food (I'll warn you at 50, 80 and 100%)\n"
    "📊 how much did I spend this month?\n"
    "↩️ undo last\n"
    "📄 send a bank-statement PDF to import it\n\n"
    "👥 Friends and splitting:\n"
    "• add friend Surya 123456789  (or /invite to get a link)\n"
    "• dinner 3000, split equally with Surya and Ravi\n"
    "• /dues shows who owes whom\n\n"
    "🧳 Trips: create a Telegram group, add your friends and add me. Anyone there can mention me ('@me I paid 1000 for petrol') and say '@me split the bill'.\n\n"
    "Use the menu below for instant answers."
)

MAX_CLARIFY_CHARS = 200
MAX_USER_MESSAGE_CHARS = 1000


def render_reply(kind: str, message: str | None) -> str:
    kind = (kind or "").strip().lower()
    if kind == "greeting":
        return GREETING
    if kind == "help":
        return HELP
    if kind == "clarify" and message:
        return "❓ " + message.strip()[:MAX_CLARIFY_CHARS]
    return SCOPE_MESSAGE
