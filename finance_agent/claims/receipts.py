"""Receipt files that come with a claim: checked, stored with the claim, and handed to the manager with its card."""
import base64
import binascii

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_agent.db.models import ClaimReceipt

MAX_BYTES = 5 * 1024 * 1024
MAX_FILES = 30
TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


class ReceiptError(ValueError):
    pass


def decode(raw: list[dict], valid_refs: set[int]) -> list[dict]:
    """The files in a claim message -> [{item_ref, filename, content_type, data}], refusing anything that isn't a small image or PDF."""
    if len(raw) > MAX_FILES:
        raise ReceiptError(f"Please attach at most {MAX_FILES} receipts to one claim.")
    out = []
    for r in raw:
        kind = str(r.get("content_type") or "").lower()
        try:
            ref, data = int(float(r["ref"])), base64.b64decode(str(r.get("data") or ""), validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise ReceiptError("A receipt file wasn't readable.") from None
        if ref not in valid_refs:
            raise ReceiptError("A receipt belongs to an item that isn't in this claim.")
        if kind not in TYPES or not data:
            raise ReceiptError("Receipts must be a photo (JPG, PNG, WEBP) or a PDF.")
        if len(data) > MAX_BYTES:
            raise ReceiptError("A receipt is bigger than 5 MB.")
        out.append({"item_ref": ref, "filename": (str(r.get("filename") or "receipt"))[:120], "content_type": kind, "data": data})
    return out


async def save(session: AsyncSession, claim_id: int, files: list[dict]) -> None:
    for f in files:
        session.add(ClaimReceipt(claim_id=claim_id, **f))
    await session.flush()


async def of(session: AsyncSession, claim_id: int) -> list[ClaimReceipt]:
    return list((await session.execute(select(ClaimReceipt).where(ClaimReceipt.claim_id == claim_id).order_by(ClaimReceipt.id))).scalars().all())
