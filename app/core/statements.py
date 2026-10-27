"""
Bank-statement import (everything except the LLM, which lives in app/agent/statement_parser.py).

    PDF bytes --extract_pdf_pages--> text --(LLM)--> raw rows --build_plan--> items (new / duplicate / maybe / skipped)
        --save_import--> StatementImport(pending) --user taps confirm--> apply_import --> Expense rows
                                                                      |--> undo_import voids them all
Only money going OUT becomes an expense. Credits and ATM withdrawals are skipped (a withdrawal is not spending: what
you buy with the cash is what you log). All amounts are validated here in code: the LLM output is never trusted blindly.
"""
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import budgets
from app.core.errors import UserError
from app.core.expenses import add_expense, normalize_category
from app.core.formatting import cat_icon, money
from app.core.periods import now_local, to_local, tz
from app.db.models import Expense, StatementImport

MAX_PAGES = 80
MAX_ROWS = 1500
CATEGORIES = [
    "food", "groceries", "transport", "fuel", "shopping", "clothes", "bills", "rent", "emi", "entertainment",
    "health", "education", "travel", "insurance", "investment", "cash_withdrawal", "salary", "interest",
    "refund", "other",
]
CASH_WITHDRAWAL = "cash_withdrawal"


class StatementError(UserError):
    """User-facing problem with the uploaded statement."""


class PasswordRequired(StatementError):
    pass


class WrongPassword(StatementError):
    pass


# ------------------------------------------------------------------ PDF -> text
def extract_pdf_pages(data: bytes, password: str | None = None) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        raise StatementError("I couldn't open that file - is it a valid PDF?") from None
    if reader.is_encrypted:
        if not password:
            raise PasswordRequired("This PDF is password protected.")
        try:
            ok = reader.decrypt(password)
        except Exception:
            ok = 0
        if not ok:
            raise WrongPassword("That password didn't work.")
    if len(reader.pages) > MAX_PAGES:
        raise StatementError(f"That statement has {len(reader.pages)} pages; I handle up to {MAX_PAGES}. Send it in parts.")
    try:
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception:
        raise StatementError("I couldn't read the text in that PDF.") from None
    if not "".join(pages).strip():
        raise StatementError("That PDF has no readable text (probably a scan/photo). Please download the digital statement from your bank.")
    return pages


# ------------------------------------------------------------------ raw rows -> plan
def _norm_desc(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _clean_row(raw: dict) -> dict | None:
    """Validate one LLM row. Returns None if it can't be trusted."""
    try:
        d = datetime.strptime(str(raw.get("date", "")).strip()[:10], "%Y-%m-%d").date()
        amount = Decimal(str(raw.get("amount"))).copy_abs().quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation, TypeError):
        return None
    kind = str(raw.get("type", "")).strip().lower()
    if kind not in ("debit", "credit") or amount <= 0 or d > now_local().date() + timedelta(days=1) or d.year < 2000:
        return None
    category = normalize_category(raw.get("category"))
    desc = re.sub(r"\s+", " ", str(raw.get("description") or "")).strip()[:120] or category
    return {"date": d.isoformat(), "description": desc, "amount": str(amount), "type": kind, "category": category}


async def build_plan(session: AsyncSession, user_id: int, raw_rows: list[dict]) -> tuple[list[dict], dict]:
    rows = [r for r in (_clean_row(x) for x in raw_rows) if r]
    dropped = len(raw_rows) - len(rows)
    if not rows:
        raise StatementError("I couldn't find any transactions in that PDF. I can only import bank/card statements.")
    if len(rows) > MAX_ROWS:
        raise StatementError(f"That statement has {len(rows)} rows; the limit is {MAX_ROWS}. Send it in parts.")

    seen: dict[str, int] = {}
    for r in rows:  # identical rows on the same day get distinct keys (two coffees of 100 are two coffees)
        base = f"{user_id}|{r['date']}|{r['type']}|{r['amount']}|{_norm_desc(r['description'])}"
        n = seen[base] = seen.get(base, 0) + 1
        r["key"] = hashlib.sha1(f"{base}|{n}".encode()).hexdigest()[:40]

    spendable = [r for r in rows if r["type"] == "debit" and r["category"] != CASH_WITHDRAWAL]
    already = set((await session.execute(
        select(Expense.external_key).where(
            Expense.user_id == user_id, Expense.voided_at.is_(None),
            Expense.external_key.in_([r["key"] for r in spendable] or [""]),
        )
    )).scalars().all())

    manual: list[Expense] = []
    if spendable:
        lo = date.fromisoformat(min(r["date"] for r in spendable)) - timedelta(days=1)
        hi = date.fromisoformat(max(r["date"] for r in spendable)) + timedelta(days=2)
        manual = list((await session.execute(
            select(Expense).where(
                Expense.user_id == user_id, Expense.voided_at.is_(None), Expense.source == "personal",
                Expense.occurred_at >= datetime.combine(lo, time.min, tzinfo=tz()),
                Expense.occurred_at < datetime.combine(hi, time.min, tzinfo=tz()),
            )
        )).scalars().all())

    for r in rows:
        if r["type"] == "credit":
            r["status"] = "skip_credit"
        elif r["category"] == CASH_WITHDRAWAL:
            r["status"] = "skip_cash"
        elif r["key"] in already:
            r["status"] = "duplicate"
        else:
            r["status"] = "new"
            d, amount = date.fromisoformat(r["date"]), Decimal(r["amount"])
            for m in manual:  # did the user already log this by hand? (same amount, +-1 day)
                if m.amount == amount and abs((to_local(m.occurred_at).date() - d).days) <= 1:
                    manual.remove(m)
                    r["status"] = "maybe"
                    break

    total = lambda status: str(sum((Decimal(r["amount"]) for r in rows if r["status"] == status), Decimal(0)))
    meta = {
        "count": len(rows), "dropped": dropped,
        "start": min(r["date"] for r in rows), "end": max(r["date"] for r in rows),
        "credits_total": total("skip_credit"), "cash_total": total("skip_cash"),
    }
    return rows, meta


async def save_import(session: AsyncSession, user_id: int, filename: str | None, items: list[dict], meta: dict) -> StatementImport:
    imp = StatementImport(user_id=user_id, filename=(filename or "statement.pdf")[:256], items=items, meta=meta)
    session.add(imp)
    await session.flush()
    return imp


# ------------------------------------------------------------------ preview
def counts(items: list[dict]) -> dict[str, int]:
    return {s: sum(1 for i in items if i["status"] == s) for s in ("new", "maybe", "duplicate", "skip_credit", "skip_cash")}


def preview_text(imp: StatementImport) -> str:
    items, meta = imp.items, imp.meta
    c = counts(items)
    fresh = [i for i in items if i["status"] in ("new", "maybe")]
    spent = sum((Decimal(i["amount"]) for i in fresh), Decimal(0))
    fmt = lambda s: date.fromisoformat(s).strftime("%d %b %Y")
    lines = [
        f"📄 {imp.filename}",
        f"🗓 {fmt(meta['start'])} → {fmt(meta['end'])} · {meta['count']} rows read\n",
        f"🔴 Spending to add  {money(spent)}\n",
    ]
    by_cat: dict[str, Decimal] = {}
    for i in fresh:
        by_cat[i["category"]] = by_cat.get(i["category"], Decimal(0)) + Decimal(i["amount"])
    if by_cat:
        lines.append("By category")
        total = sum(by_cat.values()) or Decimal(1)
        for cat, amt in sorted(by_cat.items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"{cat_icon(cat)} {cat:<15} {money(amt)} ({int(amt * 100 / total)}%)")
        lines.append("")
    if c["skip_credit"]:
        lines.append(f"↪️ {c['skip_credit']} money-in rows skipped ({money(Decimal(meta['credits_total']))}): this tracks spending only")
    if c["skip_cash"]:
        lines.append(f"🏧 {c['skip_cash']} ATM withdrawals skipped ({money(Decimal(meta['cash_total']))}): log what you spend the cash on")
    if c["duplicate"]:
        lines.append(f"♻️ {c['duplicate']} already imported before - skipped")
    if c["maybe"]:
        lines.append(f"❓ {c['maybe']} look like expenses you added by hand (same amount, ±1 day) - skipped unless you include them")
    if meta.get("dropped"):
        lines.append(f"⚠️ {meta['dropped']} unreadable rows ignored")
    lines.append(f"\n✅ Ready to import {c['new']} new expenses.")
    return "\n".join(lines)


# ------------------------------------------------------------------ apply / undo
@dataclass
class ImportResult:
    applied: int
    text: str


def _occurred(d: date) -> datetime | None:
    return None if d == now_local().date() else datetime.combine(d, time(12, 0), tzinfo=tz())


async def _load(session: AsyncSession, user_id: int, import_id: int, want: str) -> StatementImport:
    imp = (await session.execute(
        select(StatementImport).where(StatementImport.id == import_id, StatementImport.user_id == user_id).with_for_update()
    )).scalar_one_or_none()
    if imp is None:
        raise StatementError("I can't find that import.")
    if imp.status != want:
        raise StatementError({"done": "That statement was already imported.", "cancelled": "That import was cancelled.",
                              "undone": "That import was already undone.", "pending": "That import isn't done yet."}[imp.status])
    return imp


async def apply_import(session: AsyncSession, user_id: int, import_id: int, include_maybes: bool = False) -> ImportResult:
    imp = await _load(session, user_id, import_id, "pending")  # row lock + status flip = tap-twice safe
    wanted = {"new", "maybe"} if include_maybes else {"new"}
    todo = sorted((i for i in imp.items if i["status"] in wanted), key=lambda i: i["date"])
    created = []
    for i in todo:
        created.append(await add_expense(
            session, user_id, Decimal(i["amount"]), i["category"], i["description"], _occurred(date.fromisoformat(i["date"])),
            source="statement", external_key=i["key"], import_id=imp.id,
        ))
    imp.status = "done"
    imp.meta = {**imp.meta, "applied": len(todo)}
    this_month = now_local().strftime("%Y-%m")
    alerts = await budgets.alerts_for(session, user_id, [e for e in created if to_local(e.occurred_at).strftime("%Y-%m") == this_month])
    total = sum((e.amount for e in created), Decimal(0))
    text = "\n".join([f"✅ Added {len(created)} expenses ({money(total)}) from {imp.filename}", *alerts])
    return ImportResult(len(todo), text)


async def cancel_import(session: AsyncSession, user_id: int, import_id: int) -> None:
    imp = await _load(session, user_id, import_id, "pending")
    imp.status = "cancelled"


async def undo_import(session: AsyncSession, user_id: int, import_id: int) -> str:
    imp = await _load(session, user_id, import_id, "done")
    rows = (await session.execute(select(Expense).where(Expense.import_id == imp.id, Expense.voided_at.is_(None)))).scalars().all()
    now = datetime.now(timezone.utc)
    for e in rows:
        e.voided_at = now
    imp.status = "undone"
    return f"↩️ Import undone - {len(rows)} expenses removed."
