"""Pure logic of the server: money maths, periods, statements. No network."""
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pypdf import PdfWriter

from mcp_server.domain import statements
from mcp_server.domain.errors import SplitError
from mcp_server.domain.periods import PeriodError, month_key, resolve_period, shift_month, to_local, tz
from mcp_server.domain.splits import compute_transfers, equal_shares, exact_shares

D = Decimal


# ------------------------------------------------------------------ splitting to the paisa
def test_equal_shares_add_up_to_the_total():
    shares = equal_shares(D("1000"), [1, 2, 3])
    assert sum(shares.values()) == D("1000") and sorted(shares.values()) == [D("333.33"), D("333.33"), D("333.34")]
    assert shares[1] == D("333.34")  # the first person (the payer) takes the odd paisa


def test_odd_paise_rotate_towards_whoever_has_paid_least_so_far():
    first = equal_shares(D("10"), [1, 2, 3])
    second = equal_shares(D("10"), [1, 2, 3], already=first)
    third = equal_shares(D("10"), [1, 2, 3], already={u: first[u] + second[u] for u in first})
    totals = {u: first[u] + second[u] + third[u] for u in first}
    assert max(totals.values()) - min(totals.values()) <= D("0.01")


def test_exact_shares_must_add_up_and_be_positive():
    assert exact_shares(D("100"), {1: D("40"), 2: D("60")}) == {1: D("40"), 2: D("60")}
    with pytest.raises(SplitError):
        exact_shares(D("100"), {1: D("40"), 2: D("50")})
    with pytest.raises(SplitError):
        exact_shares(D("100"), {1: D("100"), 2: D("0")})


def test_nobody_to_split_with():
    with pytest.raises(SplitError):
        equal_shares(D("10"), [])


def test_transfers_are_the_fewest_and_clear_every_balance():
    net = {1: D("300"), 2: D("-1600"), 3: D("1300")}  # the party-night example: user 2 owes everyone
    transfers = compute_transfers(net)
    assert len(transfers) == 2 and sum(a for _, _, a in transfers) == D("1600")
    balance = dict(net)
    for debtor, creditor, amount in transfers:
        balance[debtor] += amount
        balance[creditor] -= amount
    assert all(v == 0 for v in balance.values())
    assert compute_transfers({1: D("0"), 2: D("0")}) == []


# ------------------------------------------------------------------ periods
def test_month_arithmetic_crosses_years():
    assert shift_month("2026-01", -1) == "2025-12" and shift_month("2025-12", 1) == "2026-01" and shift_month("2026-03", -15) == "2024-12"


@pytest.mark.parametrize("text", ["today", "yesterday", "this_week", "last_7_days", "last_30_days", "this_month", "last_month", "this_year", "all_time",
                                  "2026-09", "2026-09-15", "2026-10-01..2026-10-15", "this month", "THIS_MONTH"])
def test_known_periods_parse(text):
    assert resolve_period(text).label


def test_reversed_range_is_swapped_and_nonsense_is_refused():
    assert resolve_period("2026-10-15..2026-10-01").label == resolve_period("2026-10-01..2026-10-15").label
    for bad in ("2026-13", "2026-02-30", "whenever", "2026-10-01..2026-13-01"):
        with pytest.raises(PeriodError):
            resolve_period(bad)


def test_this_month_period_starts_on_the_first():
    p = resolve_period("this_month")
    assert p.start is not None and to_local(p.start).day == 1 and month_key()[5:] in p.start.astimezone(tz()).strftime("%m")


# ------------------------------------------------------------------ statements
def blank_pdf(password: str | None = None) -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    if password:
        w.encrypt(password)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_pdf_needs_the_password_and_rejects_a_wrong_one():
    locked = blank_pdf("secret")
    with pytest.raises(statements.PasswordRequired):
        statements.extract_pdf_pages(locked)
    with pytest.raises(statements.WrongPassword):
        statements.extract_pdf_pages(locked, "nope")


def test_a_pdf_without_text_is_explained():
    with pytest.raises(statements.StatementError, match="no readable text"):
        statements.extract_pdf_pages(blank_pdf())


def test_garbage_rows_are_dropped():
    assert statements._clean_row({"date": "garbage", "description": "x", "amount": 5, "type": "debit", "category": "food"}) is None
    assert statements._clean_row({"date": "2026-10-01", "description": "x", "amount": 5, "type": "sideways", "category": "food"}) is None
    far = (date.today() + timedelta(days=30)).isoformat()
    assert statements._clean_row({"date": far, "description": "x", "amount": 5, "type": "debit", "category": "food"}) is None
    ok = statements._clean_row({"date": "2026-10-01", "description": "  SWIGGY   ORDER ", "amount": "450.5", "type": "DEBIT", "category": "Food"})
    assert ok == {"date": "2026-10-01", "description": "SWIGGY ORDER", "amount": "450.50", "type": "debit", "category": "food"}


def d(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def row(days_ago, desc, amount, kind, category):
    return {"date": d(days_ago), "description": desc, "amount": amount, "type": kind, "category": category}


STATEMENT = [
    row(5, "ACME LTD SALARY", 50000, "credit", "salary"),
    row(4, "SWIGGY ORDER", 450.5, "debit", "food"),
    row(3, "ATM WDL", 2000, "debit", "cash_withdrawal"),
    row(2, "UBER TRIP", 300, "debit", "transport"),
    {"date": "garbage", "description": "bad", "amount": 5, "type": "debit", "category": "x"},
]


async def test_statement_import_preview_apply_duplicates_and_undo(harness, alice):
    prev = await alice.ok("statement_preview", filename="oct.pdf", rows=STATEMENT)
    assert prev["data"]["new"] == 2 and prev["data"]["maybe"] == 0  # only the two spending rows; salary and ATM are skipped
    assert "skipped" in prev["text"].lower()
    imp = prev["data"]["import_id"]
    assert (await alice.call("statement_apply", import_id=imp + 99999))["status"] == "rejected"  # not yours / not found
    done = await alice.ok("statement_apply", import_id=imp)
    assert "Added 2 expenses" in done["text"] and "750.50" in done["text"].replace(",", "")
    assert (await alice.call("statement_apply", import_id=imp))["status"] == "rejected"  # tap twice: nothing doubled
    again = await alice.ok("statement_preview", filename="oct.pdf", rows=STATEMENT)  # the same statement again
    assert again["data"]["new"] == 0  # every row is already imported
    undone = await alice.ok("statement_undo", import_id=imp)
    assert "2 expenses removed" in undone["text"]
    assert (await alice.call("statement_undo", import_id=imp))["status"] == "rejected"


async def test_a_possible_duplicate_of_a_hand_entered_expense_is_flagged(harness, alice):
    await alice.ok("add_expenses", items=[{"amount": 300, "category": "transport", "description": "cab", "date": d(2)}])
    prev = await alice.ok("statement_preview", filename="x.pdf", rows=[row(2, "UBER TRIP", 300, "debit", "transport")])
    assert prev["data"]["maybe"] == 1 and prev["data"]["new"] == 0
    done = await alice.ok("statement_apply", import_id=prev["data"]["import_id"], include_maybes=True)
    assert "Added 1 expenses" in done["text"]


async def test_cancelling_an_import_adds_nothing(harness, alice):
    prev = await alice.ok("statement_preview", filename="x.pdf", rows=[row(1, "COFFEE", 90, "debit", "food")])
    await alice.ok("statement_cancel", import_id=prev["data"]["import_id"])
    assert (await alice.call("statement_apply", import_id=prev["data"]["import_id"]))["status"] == "rejected"
    assert "COFFEE" not in (await alice.ok("get_expenses"))["text"]


async def test_another_user_cannot_touch_my_import(harness, alice, bob):
    prev = await alice.ok("statement_preview", filename="x.pdf", rows=[row(1, "COFFEE", 90, "debit", "food")])
    assert (await bob.call("statement_apply", import_id=prev["data"]["import_id"]))["status"] == "rejected"
    assert (await bob.call("statement_cancel", import_id=prev["data"]["import_id"]))["status"] == "rejected"


async def test_pdf_reading_tool_reports_password_state_and_never_leaks_pages_of_others(harness, alice):
    import base64
    r = await alice.ok("statement_read_pdf", pdf_base64=base64.b64encode(blank_pdf("pw")).decode())
    assert r["data"]["password"] == "required"
    r = await alice.ok("statement_read_pdf", pdf_base64=base64.b64encode(blank_pdf("pw")).decode(), password="bad")
    assert r["data"]["password"] == "wrong"
    assert (await alice.call("statement_read_pdf", pdf_base64="@@@not base64@@@"))["status"] == "rejected"
    assert (await alice.call("statement_read_pdf", pdf_base64=base64.b64encode(blank_pdf()).decode()))["status"] == "rejected"
