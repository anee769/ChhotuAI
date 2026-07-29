"""Customer credit ledger derived from receivables and payment events."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

import ledger as L


def _sale_amount(event: dict, sku: dict) -> float:
    quoted = event.get("quoted_rate")
    rate = quoted if quoted is not None else event.get("rate")
    if rate is None:
        return 0.0
    return round(L.line_amount(
        float(event.get("qty") or 0),
        event.get("unit") or sku.get("default_unit"),
        float(rate),
        event.get("rate_unit") if quoted is not None else None,
        sku,
    ), 2)


def orders_by_customer(repo) -> dict:
    """Group sale lines into customer orders using the durable request/bill id."""
    catalogue = {sku["sku_id"]: sku for sku in repo.load_catalogue()}
    receivable_order = {}
    for receivable in repo.receivables():
        for event_id in receivable.get("sale_event_ids") or []:
            receivable_order[event_id] = receivable["receivable_id"]
    grouped = {}
    for event in repo.all_events():
        customer_id = event.get("customer_id")
        sku = catalogue.get(event.get("sku_id"))
        if event.get("type") != "sale" or not customer_id or not sku:
            continue
        request_id = (event.get("evidence") or {}).get("request_id")
        order_id = (request_id or receivable_order.get(event.get("event_id"))
                    or event.get("event_id"))
        key = (customer_id, order_id)
        order = grouped.setdefault(key, {
            "order_id": order_id,
            "occurred_on": str(event.get("occurred_on") or "")[:10],
            "payment": event.get("payment") or "cash",
            "items": [],
            "total": 0.0,
        })
        amount = _sale_amount(event, sku)
        order["items"].append({
            "event_id": event.get("event_id"),
            "sku_id": sku["sku_id"],
            "canonical": sku["canonical"],
            "qty": float(event.get("qty") or 0),
            "unit": event.get("unit") or sku.get("default_unit"),
            "amount": amount,
        })
        order["total"] = round(order["total"] + amount, 2)
    out = defaultdict(list)
    for (customer_id, _), order in grouped.items():
        out[customer_id].append(order)
    for rows in out.values():
        rows.sort(key=lambda row: (row["occurred_on"], row["order_id"]),
                  reverse=True)
    return dict(out)


def accounts(repo) -> list:
    """Allocate payments FIFO and return customer balances with open dues."""
    customers = {c["customer_id"]: c for c in repo.customers()}
    recs_by = {}
    pays_by = {}
    for r in repo.receivables():
        recs_by.setdefault(r["customer_id"], []).append(dict(r))
    for p in repo.payments():
        pays_by.setdefault(p["customer_id"], []).append(dict(p))

    customer_orders = orders_by_customer(repo)
    workflow_orders = {}
    if hasattr(repo, "orders"):
        for order in repo.orders():
            workflow_orders.setdefault(order.get("customer_id"), []).append(order)
        for rows in workflow_orders.values():
            rows.sort(
                key=lambda row: (row.get("created_at") or "", row["order_id"]),
                reverse=True)
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
        orders = customer_orders.get(cid, [])
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
            "orders": orders,
            "fulfilment_orders": workflow_orders.get(cid, []),
            "order_count": len(orders),
            "total_sales": round(sum(order["total"] for order in orders), 2),
            "last_order_on": orders[0]["occurred_on"] if orders else None,
            "repeat_buyer": len(orders) >= 2,
        })
    return sorted(out, key=lambda x: (-x["outstanding"], x.get("name", "")))


def account(repo, customer_id: str):
    return next((a for a in accounts(repo) if a["customer_id"] == customer_id), None)


def analytics(repo, as_of: date, days: int = 30) -> dict:
    """Acquisition, retention, customer value, and best-product signals."""
    rows = accounts(repo)
    start = as_of - timedelta(days=days - 1)
    previous_start = start - timedelta(days=days)
    previous_end = start - timedelta(days=1)

    def created_on(customer):
        raw = str(customer.get("created_at") or "")[:10]
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    new_customers = sum(1 for row in rows
                        if created_on(row) and start <= created_on(row) <= as_of)
    previous_new = sum(
        1 for row in rows
        if created_on(row)
        and previous_start <= created_on(row) <= previous_end
    )
    buyers = [row for row in rows if row["order_count"]]
    repeat = [row for row in buyers if row["repeat_buyer"]]
    active = [row for row in buyers
              if row.get("last_order_on")
              and start <= date.fromisoformat(row["last_order_on"]) <= as_of]

    catalogue = {sku["sku_id"]: sku for sku in repo.load_catalogue()}
    products = defaultdict(lambda: {"revenue": 0.0, "orders": set(), "qty": 0.0})
    for event in repo.all_events():
        sku = catalogue.get(event.get("sku_id"))
        occurred = str(event.get("occurred_on") or "")[:10]
        if event.get("type") != "sale" or not sku:
            continue
        try:
            event_date = date.fromisoformat(occurred)
        except ValueError:
            continue
        if not start <= event_date <= as_of:
            continue
        bucket = products[sku["sku_id"]]
        bucket["revenue"] += _sale_amount(event, sku)
        bucket["qty"] += float(event.get("qty") or 0)
        bucket["orders"].add(
            (event.get("evidence") or {}).get("request_id")
            or event.get("event_id"))

    best_products = sorted(({
        "sku_id": sku_id,
        "canonical": catalogue[sku_id]["canonical"],
        "revenue": round(values["revenue"], 2),
        "order_count": len(values["orders"]),
    } for sku_id, values in products.items()),
        key=lambda row: (-row["revenue"], -row["order_count"]))[:5]
    top_customers = sorted(({
        "customer_id": row["customer_id"],
        "name": row.get("name") or row.get("phone"),
        "total_sales": row["total_sales"],
        "order_count": row["order_count"],
    } for row in buyers), key=lambda row: -row["total_sales"])[:5]
    growth = None
    if previous_new:
        growth = round((new_customers - previous_new) / previous_new * 100, 1)
    elif new_customers:
        growth = 100.0
    acquisition_trend = []
    for offset in range(7, -1, -1):
        bucket_end = as_of - timedelta(days=offset * 7)
        bucket_start = bucket_end - timedelta(days=6)
        acquisition_trend.append({
            "start": bucket_start.isoformat(),
            "end": bucket_end.isoformat(),
            "new_customers": sum(
                1 for row in rows
                if created_on(row)
                and bucket_start <= created_on(row) <= bucket_end
            ),
        })
    outstanding_credit = round(
        sum(float(row.get("outstanding") or 0) for row in rows), 2)
    return {
        "period_days": days,
        "new_customers": new_customers,
        "previous_new_customers": previous_new,
        "acquisition_growth_pct": growth,
        "active_buyers": len(active),
        "repeat_buyers": len(repeat),
        "repeat_rate_pct": round(len(repeat) / len(buyers) * 100, 1) if buyers else 0,
        "average_order_value": round(
            sum(row["total_sales"] for row in buyers)
            / max(1, sum(row["order_count"] for row in buyers)), 2),
        "outstanding_credit": outstanding_credit,
        "open_credit_accounts": sum(
            1 for row in rows if float(row.get("outstanding") or 0) > 0),
        "acquisition_trend": acquisition_trend,
        "top_customers": top_customers,
        "best_products": best_products,
    }


def due_receivables(repo, as_of: date, days_before: int = 2) -> list:
    rows = []
    for acc in accounts(repo):
        for due in acc["open_dues"]:
            delta = (date.fromisoformat(due["deadline"]) - as_of).days
            if delta <= days_before:
                rows.append({**due, "days_until_deadline": delta, "customer": acc})
    return rows
