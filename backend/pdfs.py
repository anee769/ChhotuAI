"""Printable documents: the GST bill and the day/week summary.

These are what actually get sent on WhatsApp — the chat message is a one-line
covering note and the PDF is the document. A bill has to survive being shown
to a contractor's accountant, so it carries the letterhead, GSTIN, a CGST/SGST
split, the amount in words and a signature/seal block.

Owner and shop name come from the user record captured at registration, never
hardcoded: this is a multi-tenant app and every shop signs its own bills.
"""
from __future__ import annotations

import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

INK = colors.HexColor("#20242a")
MUTED = colors.HexColor("#6c7179")
ACCENT = colors.HexColor("#006c49")
LINE = colors.HexColor("#c9ced5")

_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
         "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
         "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
         "Eighty", "Ninety")


def _under_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def amount_in_words(amount) -> str:
    """Indian numbering — a bill reads 'One Lakh Sixty Thousand', not
    'One Hundred Sixty Thousand'."""
    n = int(round(float(amount or 0)))
    if n == 0:
        return "Zero Rupees Only"
    parts = []
    for divisor, label in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand"),
                           (100, "Hundred")):
        if n >= divisor:
            parts.append(f"{_under_hundred(n // divisor)} {label}")
            n %= divisor
    if n:
        parts.append(_under_hundred(n))
    return " ".join(parts) + " Rupees Only"


def money(n) -> str:
    """Indian digit grouping: 1,60,185."""
    n = float(n or 0)
    neg, n = n < 0, abs(n)
    whole, frac = divmod(round(n * 100), 100)
    s = str(int(whole))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if neg else "") + s + f".{int(frac):02d}"


def _qty(n) -> str:
    f = float(n or 0)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _letterhead(c, w, y, shop, owner, gstin, phone, address, title):
    c.setFillColor(ACCENT)
    c.rect(0, y + 6 * mm, w, 3, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(18 * mm, y - 4 * mm, (shop or "").upper())
    c.setFont("Helvetica", 8.5)
    c.setFillColor(MUTED)
    line = y - 9.5 * mm
    if owner:
        c.drawString(18 * mm, line, f"{owner} · Proprietor")
        line -= 4.2 * mm
    for extra in (address, " · ".join(x for x in (gstin and f"GSTIN {gstin}", phone) if x)):
        if extra:
            c.drawString(18 * mm, line, extra)
            line -= 4.2 * mm
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(w - 18 * mm, y - 4 * mm, title)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(18 * mm, line - 1 * mm, w - 18 * mm, line - 1 * mm)
    return line - 8 * mm


def _seal(c, x, y, shop, owner):
    """Signature and seal block. A real rubber stamp is a scan the shop
    supplies; until then this is the space it occupies, which is what makes
    the sheet read as a proper bill rather than a receipt."""
    c.setStrokeColor(LINE)
    c.setDash(2, 2)
    c.rect(x, y, 46 * mm, 26 * mm, stroke=1, fill=0)
    c.setDash()
    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawCentredString(x + 23 * mm, y + 20 * mm, "Seal")
    c.setFillColor(INK)
    c.setFont("Helvetica", 8)
    c.line(x + 4 * mm, y + 8 * mm, x + 42 * mm, y + 8 * mm)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(x + 23 * mm, y + 4 * mm, owner or shop or "")
    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawCentredString(x + 23 * mm, y + 0.5 * mm, "Authorised Signatory")


def bill_pdf(*, shop: str, owner: str, customer: dict, lines: list,
             subtotal: float, gst: float, total: float, bill_no: str,
             on: date, payment: str, due_on: str = None, gstin: str = "",
             phone: str = "", address: str = "") -> bytes:
    """lines: [{name, qty, unit, rate, amount, gst_rate}]"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = _letterhead(c, w, h - 18 * mm, shop, owner, gstin, phone, address,
                    "TAX INVOICE")

    # --- parties ---
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(MUTED)
    c.drawString(18 * mm, y, "BILL TO")
    c.drawRightString(w - 18 * mm, y, "INVOICE DETAILS")
    y -= 5 * mm
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, customer.get("name") or "Cash Customer")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 18 * mm, y, f"No.  {bill_no}")
    y -= 4.6 * mm
    c.setFont("Helvetica", 9)
    if customer.get("phone"):
        c.drawString(18 * mm, y, customer["phone"])
    c.drawRightString(w - 18 * mm, y,
                      f"Date  {on.strftime('%d %b %Y') if hasattr(on, 'strftime') else on}")
    y -= 10 * mm

    # --- table ---
    cols = (18 * mm, 108 * mm, 133 * mm, w - 18 * mm)   # item, qty, rate, amount
    c.setFillColor(colors.HexColor("#f3f5f7"))
    c.rect(18 * mm, y - 2 * mm, w - 36 * mm, 7 * mm, stroke=0, fill=1)
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(cols[0] + 2 * mm, y, "ITEM")
    c.drawRightString(cols[1], y, "QTY")
    c.drawRightString(cols[2], y, "RATE")
    c.drawRightString(cols[3] - 2 * mm, y, "AMOUNT")
    y -= 8 * mm

    c.setFillColor(INK)
    for it in lines:
        c.setFont("Helvetica", 9)
        name = it["name"]
        while c.stringWidth(name, "Helvetica", 9) > 84 * mm and len(name) > 4:
            name = name[:-2]
        if name != it["name"]:
            name += "…"
        c.drawString(cols[0] + 2 * mm, y, name)
        c.drawRightString(cols[1], y, f"{_qty(it['qty'])} {it.get('unit', '')}")
        c.drawRightString(cols[2], y, money(it.get("rate")) if it.get("rate") else "—")
        c.drawRightString(cols[3] - 2 * mm, y, money(it["amount"]))
        y -= 6.4 * mm
        if y < 70 * mm:            # leave room for totals + seal
            c.showPage()
            y = h - 30 * mm

    c.setStrokeColor(LINE)
    c.line(18 * mm, y + 2 * mm, w - 18 * mm, y + 2 * mm)
    y -= 4 * mm

    # --- totals, right aligned ---
    def total_row(label, value, bold=False, size=9):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawRightString(w - 48 * mm, y, label)
        c.drawRightString(w - 18 * mm, y, value)
        y -= 5.4 * mm

    total_row("Taxable value", money(subtotal))
    # Intra-state sale: GST is shown split, which is what a GST-registered
    # buyer's accountant expects to see on the face of the bill.
    if gst:
        total_row("CGST", money(gst / 2))
        total_row("SGST", money(gst / 2))
    y -= 1 * mm
    c.setStrokeColor(LINE)
    c.line(w - 78 * mm, y + 3 * mm, w - 18 * mm, y + 3 * mm)
    y -= 2 * mm
    c.setFillColor(ACCENT)
    total_row("TOTAL", "Rs " + money(total), bold=True, size=11)
    c.setFillColor(INK)

    y -= 2 * mm
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(MUTED)
    c.drawString(18 * mm, y, "Amount in words: " + amount_in_words(total))
    y -= 5 * mm
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9)
    label = "PAID — CASH" if payment == "cash" else "ON CREDIT (UDHAAR)"
    c.drawString(18 * mm, y, label)
    if payment == "credit" and due_on:
        c.setFont("Helvetica", 9)
        c.drawString(18 * mm + 42 * mm, y, f"Payment due: {due_on}")

    _seal(c, w - 64 * mm, 26 * mm, shop, owner)
    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawString(18 * mm, 18 * mm,
                 "Goods once sold will not be taken back. "
                 "Subject to local jurisdiction.")
    c.drawString(18 * mm, 14 * mm, "Generated by Chhotu.ai")
    c.showPage()
    c.save()
    return buf.getvalue()


def _by_item(lines: list) -> list:
    """Collapse a period's sale lines to one row per product.

    A week of trading is a dozen separate sales of the same TMT bar; listing
    each is a transaction log, not a summary. Grouped by (product, unit) —
    tonnes and kilos of the same bar can't be added.
    """
    grouped: dict = {}
    for l in lines:
        key = (l.get("canonical") or l.get("sku_id"), l.get("unit"))
        row = grouped.setdefault(key, {"canonical": key[0], "unit": key[1],
                                       "qty": 0.0, "amount": 0.0})
        row["qty"] += float(l.get("qty") or 0)
        row["amount"] += float(l.get("amount") or 0)
    return sorted(grouped.values(), key=lambda r: -r["amount"])


def summary_pdf(*, shop: str, owner: str, period: str, start, end,
                sale: float, margin: float, cash: float, credit: float,
                outstanding: float, lines: list = None, frozen: list = None,
                inventory_value: float = None, gstin: str = "",
                phone: str = "") -> bytes:
    weekly = period == "week"
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    title = "WEEKLY SUMMARY" if weekly else "DAILY SUMMARY"
    y = _letterhead(c, w, h - 18 * mm, shop, owner, gstin, phone, "", title)

    span = (f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}" if weekly
            else end.strftime("%d %b %Y"))
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(INK)
    c.drawString(18 * mm, y, span)
    y -= 12 * mm

    # --- headline figures as cards ---
    cards = [("SALE", money(sale)), ("GROSS PROFIT", money(margin)),
             ("CASH IN", money(cash)), ("CREDIT GIVEN", money(credit))]
    cw = (w - 36 * mm - 9 * mm) / 4
    for i, (label, value) in enumerate(cards):
        x = 18 * mm + i * (cw + 3 * mm)
        c.setStrokeColor(LINE)
        c.setFillColor(colors.HexColor("#fafbfc"))
        c.roundRect(x, y - 16 * mm, cw, 18 * mm, 2 * mm, stroke=1, fill=1)
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(x + 3 * mm, y - 4.5 * mm, label)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x + 3 * mm, y - 12 * mm, money(value.replace(",", "")) if False else value)
    y -= 26 * mm

    def section(label):
        nonlocal y
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(18 * mm, y, label.upper())
        c.setStrokeColor(LINE)
        c.line(18 * mm, y - 2 * mm, w - 18 * mm, y - 2 * mm)
        c.setFillColor(INK)
        y -= 8 * mm

    if lines:
        section("Items sold")
        c.setFont("Helvetica", 9)
        for l in _by_item(lines)[:14]:
            c.drawString(20 * mm, y, l["canonical"][:52])
            c.drawRightString(w - 60 * mm, y, f"{_qty(l['qty'])} {l.get('unit','')}")
            c.drawRightString(w - 18 * mm, y, money(l["amount"]))
            y -= 5.6 * mm
        y -= 4 * mm

    section("Money position")
    c.setFont("Helvetica", 9)
    rows = [("Total outstanding credit (udhaar)", money(outstanding))]
    if inventory_value is not None:
        rows.append(("Inventory value at landed cost", money(inventory_value)))
    for label, value in rows:
        c.drawString(20 * mm, y, label)
        c.drawRightString(w - 18 * mm, y, value)
        y -= 5.6 * mm
    y -= 4 * mm

    if weekly:
        section("Frozen capital · no movement in 60 days")
        c.setFont("Helvetica", 9)
        if frozen:
            for f in frozen[:10]:
                c.drawString(20 * mm, y, f["canonical"][:52])
                c.drawRightString(w - 18 * mm, y, money(f["value"]))
                y -= 5.6 * mm
        else:
            c.setFillColor(MUTED)
            c.drawString(20 * mm, y, "Nothing has been sitting unsold for 60 days.")
            c.setFillColor(INK)
            y -= 5.6 * mm

    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawString(18 * mm, 14 * mm, "Generated by Chhotu.ai · figures derived from the event ledger")
    c.showPage()
    c.save()
    return buf.getvalue()
