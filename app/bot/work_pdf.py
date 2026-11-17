"""The /workpdf report: a PDF of the person's work expenses and what the company paid. Pure drawing, no Telegram and no network."""
from datetime import date
from decimal import Decimal

from fpdf import FPDF


def _txt(value) -> str:
    """The built-in PDF fonts only know Latin-1: keep letters, replace the rupee sign and anything else unsupported."""
    return str(value if value is not None else "").replace("₹", "Rs ").encode("latin-1", "replace").decode("latin-1")


def _rs(value) -> str:
    return "Rs " + f"{Decimal(str(value)):,.2f}".rstrip("0").rstrip(".") if value is not None else "-"


def _details(r: dict) -> str:
    bits = []
    if r.get("nights"):
        bits.append(f"{r['nights']} night(s)")
    if r.get("days"):
        bits.append(f"{r['days']} day(s)")
    if r.get("has_receipt") is not None:
        bits.append("receipt: " + ("yes" if r["has_receipt"] else "no"))
    if r.get("purpose"):
        bits.append(r["purpose"])
    return ", ".join(bits)


def build_pdf(name: str, company: str | None, rows: list[dict]) -> bytes:
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, _txt("Work expense report"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _txt(f"{name}" + (f" - {company}" if company else "") + f" - generated {date.today().isoformat()}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    cols = [("Date", 22), ("Expense", 62), ("Details", 52), ("Claimed", 24), ("Claim", 38), ("Status", 32), ("Paid", 24), ("Note", 23)]
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(230, 230, 230)
    for title, w in cols:
        pdf.cell(w, 7, title, border=1, fill=True)
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    claimed = paid = Decimal(0)
    for r in rows:
        amount = Decimal(r["amount"])
        claimed += amount
        if r.get("approved") is not None:
            got = Decimal(r["approved"])
        elif r.get("claim_approved") is not None and r.get("status") in ("approved", "paid"):
            got = None  # approved as a total, with no figure per expense
        else:
            got = None
        if got is not None:
            paid += got
        note = ("not refunded" if got == 0 else "partly paid") if got is not None and got < amount else ""
        cells = [
            r["date"], r["description"], _details(r), _rs(amount),
            f"#{r['claim_no']} {r['claim_title']}" if r.get("claim_no") else "-", r["status"], _rs(got) if got is not None else "-", note,
        ]
        reason = r.get("reason") if note else None
        height = 6
        for (title, w), text in zip(cols, cells):
            pdf.cell(w, height, _txt(text)[: int(w / 1.9)], border=1)
        pdf.ln()
        if reason:  # why it was cut, under its row
            pdf.set_font("Helvetica", "I", 7)
            pdf.cell(0, 4.5, _txt("   Reason: " + reason)[:190], new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 8)
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, _txt(f"Total claimed: {_rs(claimed)}   Paid or approved per expense: {_rs(paid)}"), new_x="LMARGIN", new_y="NEXT")
    if not rows:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, "No work expenses yet.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
