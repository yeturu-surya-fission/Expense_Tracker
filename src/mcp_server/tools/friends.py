from fastmcp import Context
from mcp_server.tools import mcp

from mcp_server.core import friends, notices, views
from mcp_server.core.errors import UserError
from mcp_server.database.session import session_scope
from mcp_server.tools._common import user_id_from


@mcp.tool
async def add_friend(nickname: str, telegram_id: int, ctx: Context) -> str:
    """Add a friend to the user's friend circle with the name the user wants to call them, using their Telegram
    user id (a number such as 123456789). Adding the same id again renames the friend.
    Example: 'add friend Surya 123456789' -> nickname='Surya', telegram_id=123456789."""
    try:
        async with session_scope() as s:
            row, user, created = await friends.add_friend(s, await user_id_from(ctx), telegram_id, nickname)
    except UserError as e:
        return f"❌ {e}"
    head = f"👥 Added {row.nickname} (id {user.telegram_id}) to your friends." if created else f"✏️ Renamed your friend (id {user.telegram_id}) to {row.nickname}."
    if user.started_at:
        return head + "\nThey can receive my messages. You can split bills with them now."
    return head + "\n" + notices.not_started_hint([row.nickname])


@mcp.tool
async def remove_friend(nickname: str, ctx: Context) -> str:
    """Remove a friend from the user's friend circle by the name they use for them."""
    try:
        async with session_scope() as s:
            name = await friends.remove_friend(s, await user_id_from(ctx), nickname)
    except UserError as e:
        return f"❌ {e}"
    return f"🗑️ Removed {name} from your friends."


@mcp.tool
async def list_friends(ctx: Context) -> str:
    """Show the user's friends with their names and Telegram ids."""
    async with session_scope() as s:
        return await views.friends_text(s, await user_id_from(ctx))
