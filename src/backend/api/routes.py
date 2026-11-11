import hmac

from fastapi import APIRouter, Header, HTTPException, Request
from telegram import Update

from backend.config import settings

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    """Used only when TELEGRAM_MODE=webhook. Telegram signs calls with our secret token."""
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="bad secret")
    tg = request.app.state.telegram
    await tg.update_queue.put(Update.de_json(await request.json(), tg.bot))
    return {"ok": True}
