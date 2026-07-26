"""Customer credit ledger derived from receivables and payment events."""
from __future__ import annotations

from datetime import date


def accounts(repo) -> list:
    """Allocate payments FIFO and return customer balances with open dues."""
    customers = {c["customer_id"]: c for c in repo.customers()}
    recs_by = {}
    pays_by = {}
    for r in repo.receivables():
        recs_by.setdefault(r["customer_id"], []).append(dict(r))
    for p in repo.payments():
        pays_by.setdefault(p["customer_id"], []).append(dict(p))

    out = []
    for cid, customer in customers.items():
        recs = sorted(recs_by.get(cid, []), key=lambda r: (r["deadline"], r["created_at"]))
        customer_payments = sorted(
            pays_by.get(cid, []),
            key=lambda p: (p.get("paid_on", ""), p.get("created_at", "")),
            reverse=True,
        )
        paid_pool = sum(float(p["amount"]) for p in customer_payments)
        open_dues = []
        total_credit = 0.0
        for r in recs:
            amount = float(r["amount"])
            total_credit += amount
            applied = min(amount, paid_pool)
            paid_pool -= applied
            remaining = round(amount - applied, 2)
            if remaining > 0:
                r["remaining"] = remaining
                open_dues.append(r)
        outstanding = round(sum(r["remaining"] for r in open_dues), 2)
        out.append({
            **customer,
            "total_credit": round(total_credit, 2),
            "total_paid": round(sum(float(p["amount"]) for p in pays_by.get(cid, [])), 2),
            "outstanding": outstanding,
            "next_deadline": open_dues[0]["deadline"] if open_dues else None,
            "open_dues": open_dues,
            "payment_count": len(customer_payments),
            # Keep every receipt as its own immutable row. Totals remain derived.
            "payments": customer_payments,
        })
    return sorted(out, key=lambda x: (-x["outstanding"], x.get("name", "")))


def account(repo, customer_id: str):
    return next((a for a in accounts(repo) if a["customer_id"] == customer_id), None)


def due_receivables(repo, as_of: date, days_before: int = 2) -> list:
    rows = []
    for acc in accounts(repo):
        for due in acc["open_dues"]:
            delta = (date.fromisoformat(due["deadline"]) - as_of).days
            if delta <= days_before:
                rows.append({**due, "days_until_deadline": delta, "customer": acc})
    return rows
