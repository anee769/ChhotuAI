"""WhatsApp message bodies for bills, summaries and credit reminders.

WhatsApp has no tables. It has *bold*, _italic_ and a monospace block, and
only inside monospace do columns stay aligned — everywhere else the font is
proportional and any hand-spaced column collapses. So every figure that needs
to line up lives in a ``` block, and everything else is normal text.

Width matters: a monospace block wraps at roughly 32 characters on a phone,
and a wrapped bill line looks broken. WIDTH below is the budget, and the
column helpers truncate names rather than let a row spill.

The letterhead and the seal belong on the PDF, not here — a chat message that
tries to imitate a rubber stamp reads as a fake. The message carries the
figures and the PDF carries the document.
"""
from __future__ import annotations

from datetime import date, datetime

WIDTH = 32
RULE = "─" * 26


def _money(n) -> str:
    """Indian grouping: 1,60,185 rather than 160,185."""
    n = int(round(float(n or 0)))
    sign, s = ("-", str(abs(n))) if n < 0 else ("", str(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


def _qty(n) -> str:
    f = float(n or 0)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _row(left: str, right: str, width: int = WIDTH) -> str:
    """One monospace row, right-aligned figure, name truncated to fit."""
    right = str(right)
    room = width - len(right) - 1
    left = str(left)
    if len(left) > room:
        left = left[:max(0, room - 1)] + "…"
    return left.ljust(room) + " " + right


def _short_date(value) -> str:
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return value.strftime("%d %b %Y")


# ---------------------------------------------------------------------------
# 1. Bill
# ---------------------------------------------------------------------------
def bill(*, shop: str, owner: str, customer: str, lines: list, subtotal: float,
         gst: float, total: float, bill_no: str, on: date, payment: str,
         gstin: str = "", due_on: str = None, phone: str = "") -> str:
    """lines: [{name, qty, unit, amount}]"""
    head = [f"*{shop.upper()}*"]
    if owner:
        head.append(f"{owner} · Proprietor")
    if gstin:
        head.append(f"GSTIN {gstin}")
    if phone:
        head.append(phone)

    body = [f"Bill No : {bill_no}",
            f"Date    : {_short_date(on)}",
            f"Customer: {customer}"]

    table = [_row("ITEM", "AMOUNT"), "─" * WIDTH]
    for it in lines:
        table.append(_row(it["name"], _money(it["amount"])))
        # qty/rate on their own indented line: squeezing four columns into 32
        # characters truncates the product name to uselessness
        table.append(f"  {_qty(it['qty'])} {it.get('unit', '')}"
                     f"{' @ ' + _money(it['rate']) if it.get('rate') else ''}")
    table.append("─" * WIDTH)
    table.append(_row("Subtotal", _money(subtotal)))
    if gst:
        table.append(_row("GST", _money(gst)))
    table.append(_row("TOTAL", "Rs " + _money(total)))

    tail = [f"Payment : {payment.title()}"]
    if payment == "credit" and due_on:
        tail.append(f"Due on  : {_short_date(due_on)}")
    tail.append("")
    tail.append("Dhanyavaad! 🙏")

    return ("\n".join(head) + "\n" + RULE + "\n" + "\n".join(body)
            + "\n```" + "\n".join(table) + "```\n" + "\n".join(tail))


# ---------------------------------------------------------------------------
# 2. Day / week summary (to the owner)
# ---------------------------------------------------------------------------
def summary(*, shop: str, period: str, on, sale: float, margin: float,
            cash: float, credit: float, outstanding: float,
            top_item: str = "", frozen: list = None,
            inventory_value: float = None) -> str:
    weekly = period == "week"
    title = "Hafte ka hisaab" if weekly else "Aaj ka hisaab"
    when = (f"{_short_date(on[0])} – {_short_date(on[1])}" if weekly
            else _short_date(on))

    table = [_row("Sale", _money(sale)),
             _row("Gross profit", _money(margin)),
             _row("Cash aaya", _money(cash)),
             _row("Udhaar gaya", _money(credit)),
             "─" * WIDTH,
             _row("Total baaki", _money(outstanding))]
    if weekly and inventory_value is not None:
        table.append(_row("Stock value", _money(inventory_value)))

    out = [f"*{shop.upper()}*", f"_{title} · {when}_", "```" + "\n".join(table) + "```"]
    if top_item:
        out.append(f"Sabse zyada bika: *{top_item}*")
    if weekly:
        # Frozen capital is a weekly concern, not a daily one — a shopkeeper
        # can't act on 60-day-old stock before breakfast.
        if frozen:
            out.append("")
            out.append("*60 din se nahi bika:*")
            for f in frozen[:4]:
                out.append(f"• {f['canonical']} — Rs {_money(f['value'])}")
        else:
            out.append("")
            out.append("Koi maal 60 din se phasa nahi hai. 👍")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 3. Credit reminder (2 days before the deadline)
# ---------------------------------------------------------------------------
def reminder(*, shop: str, owner: str, customer: str, amount: float,
             due_on, days_left: int, phone: str = "") -> str:
    when = ("kal" if days_left == 1 else
            "aaj" if days_left == 0 else
            f"{days_left} din baad" if days_left > 1 else
            f"{abs(days_left)} din pehle")
    lead = (f"Namaste {customer} ji," if customer else "Namaste,")
    body = [
        lead,
        "",
        f"{shop} se aapka udhaar *Rs {_money(amount)}* "
        f"{when} ({_short_date(due_on)}) due hai.",
        "",
        "Agar aapne pehle hi de diya hai to is message ko "
        "nazarandaaz kar dijiye. 🙏",
    ]
    sign = [""]
    if owner:
        sign.append(f"— {owner}")
    sign.append(shop + (f" · {phone}" if phone else ""))
    return "\n".join(body + sign)


# ---------------------------------------------------------------------------
# Covering notes for the PDF sends. The document carries the detail; these
# just say what arrived, so the message is readable in a notification preview.
# ---------------------------------------------------------------------------
def bill_note(*, shop: str, customer: str, total: float, payment: str,
              due_on: str = None) -> str:
    lead = f"Namaste {customer} ji," if customer else "Namaste,"
    tail = (f"Udhaar — {_short_date(due_on)} tak jama kar dijiye."
            if payment == "credit" and due_on else "Payment received. Dhanyavaad! 🙏")
    return (f"{lead}\n\n{shop} se aapka bill *Rs {_money(total)}* ka hai.\n"
            f"Poora bill neeche PDF mein hai.\n\n{tail}")


def summary_note(*, shop: str, period: str, start, end, sale: float,
                 margin: float) -> str:
    if period == "week":
        when = f"{_short_date(start)} – {_short_date(end)}"
        title = "Hafte ka hisaab"
    else:
        when, title = _short_date(end), "Aaj ka hisaab"
    return (f"*{shop}*\n_{title} · {when}_\n\n"
            f"Sale *Rs {_money(sale)}*, gross profit *Rs {_money(margin)}*.\n"
            "Poori report PDF mein hai. 📄")
