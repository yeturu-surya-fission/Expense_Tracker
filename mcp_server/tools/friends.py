from mcp_server.db.session import session_scope
from mcp_server.domain import friends, notices, views
from mcp_server.tools.registry import tool


@tool(contexts=("private",), write=True)
async def add_friend(call, nickname: str, telegram_id: int) -> str:
    """Add a friend to the user's friend circle with the name the user wants to call them, using their Telegram
    user id (a number such as 123456789). Adding the same id again renames the friend.
    Example: 'add friend Surya 123456789' -> nickname='Surya', telegram_id=123456789."""
    async with session_scope() as s:
        row, user, created = await friends.add_friend(s, call.user_id, telegram_id, nickname)
        started = user.started_at is not None
    head = f"👥 Added {row.nickname} (id {user.telegram_id}) to your friends." if created else f"✏️ Renamed your friend (id {user.telegram_id}) to {row.nickname}."
    if started:
        return head + "\nThey can receive my messages. You can split bills with them now."
    return head + "\n" + notices.not_started_hint([row.nickname])


@tool(contexts=("private",), write=True)
async def remove_friend(call, nickname: str) -> str:
    """Remove a friend from the user's friend circle by the name they use for them."""
    async with session_scope() as s:
        name = await friends.remove_friend(s, call.user_id, nickname)
    return f"🗑️ Removed {name} from your friends."


@tool(contexts=("private",))
async def list_friends(call) -> str:
    """Show the user's friends with their names and Telegram ids."""
    async with session_scope() as s:
        return await views.friends_text(s, call.user_id)
