"""Sending the three documents and the reminder over WhatsApp.

Each function does the same three steps: build the PDF, park it behind a
short-lived tokenised URL, then send a one-line covering message with the PDF
attached. Twilio fetches media from a public URL and cannot authenticate,
which is why the link is a random token rather than a session-guarded path.

Nothing here decides WHEN to send. The owner asks for a bill by voice, the
summary asks for confirmation first, and reminders run from the scheduler —
all of that lives at the call sites, so a message is never a surprise.
"""
from __future__ import annotations

from datetime import date, timedelta

import clock
import crm
import documents
import ledger as L
import messages
import pdfs
import whatsapp


def _identity(repo, user: dict) -> dict:
    cfg = repo.load_config()
    return {
        "shop": cfg.get("shop_name") or user.get("shop_name") or "My Shop",
        "owner": user.get("name") or "",
        "gstin": cfg.get("gstin") or "",
        "phone": user.get("phone") or "",
        "address": cfg.get("address") or "",
    }


# ---------------------------------------------------------------------------
def send_bill(repo, user: dict, customer: dict, lines: list, *, payment: str,
              due_on: str = None, bill_no: str = None, on: date = None) -> dict:
    """lines: [{sku_id, qty, unit, rate}] — amounts are recomputed here so the
    bill can never disagree with the ledger."""
    if not (customer or {}).get("phone"):
        raise ValueError("This customer has no phone number saved.")
    who = _identity(repo, user)
    on = on or clock.today()
    rows, subtotal, gst_total = [], 0.0, 0.0
    for it in lines:
        sku = repo.sku(it["sku_id"]) or {}
        qty, unit = float(it["qty"]), it.get("unit") or sku.get("default_unit")
        rate = float(it.get("rate") or 0)
        amount = L.line_amount(qty, unit, rate, it.get("rate_unit") or unit, sku)
        gst = amount * float(repo.gst_rate_for(sku)) / 100.0
        subtotal += amount
        gst_total += gst
        rows.append({"name": sku.get("canonical", it["sku_id"]), "qty": qty,
                     "unit": unit, "rate": rate, "amount": amount})
    total = subtotal + gst_total
    bill_no = bill_no or f"{on:%Y%m%d}-{int(total) % 10000:04d}"

    pdf = pdfs.bill_pdf(shop=who["shop"], owner=who["owner"], customer=customer,
                        lines=rows, subtotal=subtotal, gst=gst_total,
                        total=total, bill_no=bill_no, on=on, payment=payment,
                        due_on=due_on, gstin=who["gstin"], phone=who["phone"],
                        address=who["address"])
    doc = documents.store(user["user_id"], "bill", f"bill-{bill_no}.pdf", pdf)
    body = messages.bill_note(shop=who["shop"], customer=customer.get("name", ""),
                              total=total, payment=payment, due_on=due_on)
    whatsapp.send_whatsapp(customer["phone"], body, media_url=doc["url"])
    return {"sent_to": customer["phone"], "total": round(total, 2),
            "bill_no": bill_no, "url": doc["url"]}


# ---------------------------------------------------------------------------
def send_summary(repo, user: dict, period: str = "day", on: date = None) -> dict:
    """Send the owner their own day/week summary as a PDF."""
    who = _identity(repo, user)
    end = on or clock.today()
    weekly = period == "week"
    start = end - timedelta(days=6) if weekly else end

    catalogue = repo.load_catalogue()
    by = {s["sku_id"]: s for s in catalogue}
    events = repo.all_events()
    tot = {"total": 0.0, "margin": 0.0, "cash": 0.0, "credit": 0.0}
    lines = []
    for i in range((end - start).days + 1):
        m = L.margin_for_day(by, events, start + timedelta(days=i))
        for k in tot:
            tot[k] += m[k]
        lines += m["lines"]

    outstanding = sum(a["outstanding"] for a in crm.accounts(repo))
    frozen, inventory_value = [], 0.0
    if weekly:
        for sku in catalogue:
            det = L._stock_detail(sku, events, end)
            if det["qty"] == L.UNCOUNTED:
                continue
            cost = L.landed_cost_as_of(sku, events, end) or 0
            base = det.get("base") or 0
            inventory_value += cost * base
            moved = [e for e in events if e["sku_id"] == sku["sku_id"]
                     and e["type"] in ("sale", "delivery")
                     and 0 <= (end - L._d(e["occurred_on"])).days <= 60]
            if not moved and base > 0:
                frozen.append({"canonical": sku["canonical"],
                               "value": cost * base})
        frozen.sort(key=lambda f: -f["value"])

    pdf = pdfs.summary_pdf(
        shop=who["shop"], owner=who["owner"], period=period, start=start,
        end=end, sale=tot["total"], margin=tot["margin"], cash=tot["cash"],
        credit=tot["credit"], outstanding=outstanding, lines=lines,
        frozen=frozen, inventory_value=inventory_value if weekly else None,
        gstin=who["gstin"], phone=who["phone"])
    name = f"{'week' if weekly else 'day'}-{end.isoformat()}.pdf"
    doc = documents.store(user["user_id"], "summary", name, pdf)
    body = messages.summary_note(shop=who["shop"], period=period,
                                 start=start, end=end, sale=tot["total"],
                                 margin=tot["margin"])
    whatsapp.send_whatsapp(who["phone"], body, media_url=doc["url"])
    return {"sent_to": who["phone"], "period": period, "url": doc["url"],
            **{k: round(v, 2) for k, v in tot.items()}}


# ---------------------------------------------------------------------------
def send_customer_reminder(repo, user: dict, customer_id: str,
                           on: date = None) -> dict:
    """Send the selected customer's next open credit reminder on demand."""
    who = _identity(repo, user)
    account = crm.account(repo, customer_id)
    if not account:
        raise ValueError("Customer not found.")
    if not account.get("phone"):
        raise ValueError("This customer has no phone number saved.")
    open_dues = account.get("open_dues") or []
    if not open_dues:
        raise ValueError("This customer has no outstanding credit.")
    on = on or clock.today()
    due = open_dues[0]
    due_on = date.fromisoformat(due["deadline"])
    days_left = (due_on - on).days
    body = messages.reminder(
        shop=who["shop"], owner=who["owner"],
        customer=account.get("name") or "", amount=due["remaining"],
        due_on=due["deadline"], days_left=days_left, phone=who["phone"])
    resp = whatsapp.send_whatsapp(account["phone"], body)
    result = whatsapp.confirm(resp.get("sid"))
    if not result.get("ok"):
        status = result.get("status") or "failed"
        code = result.get("error_code")
        raise RuntimeError(f"WhatsApp {status}" + (f" ({code})" if code else ""))
    return {"sent": True, "customer_id": customer_id,
            "customer": account.get("name") or "",
            "phone": account["phone"], "amount": round(due["remaining"], 2),
            "due": due["deadline"], "status": result.get("status")}


# ---------------------------------------------------------------------------
def send_due_reminders(repo, user: dict, *, days_before: int = 2,
                       on: date = None) -> dict:
    """Text-only reminders for credit falling due. No PDF: a nudge about money
    owed should be readable at a glance, not an attachment to open."""
    who = _identity(repo, user)
    on = on or clock.today()
    sent, skipped = [], []
    for due in crm.due_receivables(repo, on, days_before=days_before):
        account = due["customer"]
        if not account.get("phone"):
            skipped.append({"customer": account.get("name"), "why": "no phone"})
            continue
        body = messages.reminder(
            shop=who["shop"], owner=who["owner"],
            customer=account.get("name") or "", amount=due["remaining"],
            due_on=due["deadline"], days_left=due["days_until_deadline"],
            phone=who["phone"])
        try:
            resp = whatsapp.send_whatsapp(account["phone"], body)
            # Twilio accepts and then fails asynchronously, so the create call
            # returning 201 proves nothing. Confirm before claiming delivery.
            result = whatsapp.confirm(resp.get("sid"))
            row = {"customer": account.get("name"), "amount": due["remaining"],
                   "due": due["deadline"], "status": result.get("status")}
            if result.get("ok"):
                sent.append(row)
            else:
                skipped.append({**row, "why": f"{result.get('status')}"
                                              f" ({result.get('error_code')})"})
        except Exception as e:
            skipped.append({"customer": account.get("name"), "why": str(e)[:120]})
    return {"sent": sent, "skipped": skipped, "as_of": on.isoformat()}
