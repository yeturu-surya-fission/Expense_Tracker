"""
The Finance Agent tells us what the manager decided: POST /a2a/webhook, signed with HMAC-SHA256 over the body using a secret
only the two services share. Each event becomes a message for the employee through the normal outbox.
"""
import hmac
import hashlib
import json
import logging
from decimal import Decimal

from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_server.config import settings
from mcp_server.db.models import User
from mcp_server.db.session import session_scope
from mcp_server.domain import outbox
from mcp_server.reimbursement.models import WorkClaim
from mcp_server.tools.registry import audit, rate_limited

log = logging.getLogger(__name__)
MAX_BODY = 64 * 1024


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(settings.finance_webhook_secret.encode(), body, hashlib.sha256).hexdigest()


async def finance_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    if not settings.finance_webhook_secret or len(body) > MAX_BODY or not hmac.compare_digest(request.headers.get("x-signature", ""), sign(body)):
        await audit("client:finance-agent", None, "a2a.webhook", "denied", "bad signature")
        return JSONResponse({"error": "invalid_signature"}, status_code=401)
    if rate_limited("webhook:finance", 600):
        return JSONResponse({"error": "slow_down"}, status_code=429)
    try:
        payload = json.loads(body)
        uid = int(payload["employee_ref"])
        event = str(payload["event"])
        text = str(payload["text"])[:3500]
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    async with session_scope() as s:
        user = await s.get(User, uid)
        if user is None:
            return JSONResponse({"error": "unknown_employee"}, status_code=404)
        if event in ("claim_update", "claim_question"):
            claim = (await s.execute(
                select(WorkClaim).where(WorkClaim.user_id == uid, WorkClaim.remote_claim_id == int(payload["claim_id"])).with_for_update()
            )).scalar_one_or_none()
            if claim is None:
                return JSONResponse({"error": "unknown_claim"}, status_code=404)
            status = "info_requested" if event == "claim_question" else str(payload.get("status", claim.status))
            approved = Decimal(str(payload["approved_amount"])) if payload.get("approved_amount") is not None else claim.approved_amount
            if event == "claim_update" and claim.status == status and claim.approved_amount == approved:
                return JSONResponse({"ok": True, "duplicate": True})  # the same news twice (a retry): tell the person once
            claim.status, claim.approved_amount = status, approved
            if payload.get("note"):
                claim.note = str(payload["note"])[:500]
            claim.questions = None
        elif event != "manager_answer":
            return JSONResponse({"error": "unknown_event"}, status_code=400)
        await outbox.enqueue(s, user.telegram_id, text)
    await audit("client:finance-agent", None, f"a2a.{event}", "ok")
    return JSONResponse({"ok": True})


def register_reimbursement_routes(mcp) -> None:
    mcp.custom_route("/a2a/webhook", methods=["POST"])(finance_webhook)
