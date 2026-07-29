"""Durable customer orders, reservations and fulfilment.

Orders deliberately sit beside the event ledger rather than inside it. An
order is a promise and reserves availability; a delivered order is the moment
the existing sale/credit ledger is written. All totals and transitions are
deterministic here so a manual button and a voice-agent tool enforce exactly
the same rules.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Optional

import clock
import ledger as L


STATUSES = (
    "draft", "awaiting_confirmation", "partially_available", "confirmed",
    "stock_allocated", "ready_for_dispatch", "out_for_delivery",
    "delivery_failed", "delivered", "cancelled",
)

RESERVING_STATUSES = {
    "confirmed", "stock_allocated", "ready_for_dispatch",
    "out_for_delivery", "delivery_failed",
}

TRANSITIONS = {
    "draft": {"awaiting_confirmation", "confirmed", "cancelled"},
    "awaiting_confirmation": {
        "confirmed", "partially_available", "cancelled",
    },
    "partially_available": {"confirmed", "cancelled"},
    "confirmed": {"stock_allocated", "cancelled"},
    "stock_allocated": {"ready_for_dispatch", "cancelled"},
    "ready_for_dispatch": {"out_for_delivery", "cancelled"},
    "out_for_delivery": {"delivered", "delivery_failed"},
    "delivery_failed": {"ready_for_dispatch", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}

DELIVERY_STATUS = {
    "ready_for_dispatch": "ready",
    "out_for_delivery": "out_for_delivery",
    "delivery_failed": "failed",
    "delivered": "delivered",
    "cancelled": "cancelled",
}


class OrderError(ValueError):
    def __init__(self, message: str, code: str = "invalid_order", **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_rate(repo, sku: dict) -> tuple[float, str]:
    """Return a deterministic rate together with the unit it prices.

    Shop-configured selling rates are stored per default selling unit (for
    example, per bori). A cost-derived fallback is per ledger base unit (for
    example, per kg). Keeping the unit beside the number prevents a ₹/kg rate
    from accidentally being interpreted as ₹/bori.
    """
    rate = sku.get("selling_rate")
    if rate is None:
        rate = (sku.get("attributes") or {}).get("selling_rate")
    if rate not in (None, ""):
        return float(rate), str(
            sku.get("default_unit") or L.base_unit(sku))
    cost = L.landed_cost_as_of(
        sku, repo.events_for_sku(sku["sku_id"]), clock.today())
    return round(float(cost or 0) * 1.10, 2), L.base_unit(sku)


def _physical_base(repo, sku: dict) -> Optional[float]:
    detail = L._stock_detail(sku, repo.all_events(), clock.today())
    if detail["qty"] == L.UNCOUNTED:
        return None
    return max(0.0, float(detail.get("base") or 0))


def reserved_base(repo, sku_id: str, exclude_order_id: str = "") -> float:
    total = 0.0
    for order in repo.orders():
        if order.get("order_id") == exclude_order_id:
            continue
        if order.get("status") not in RESERVING_STATUSES:
            continue
        for item in order.get("items") or []:
            if item.get("sku_id") == sku_id:
                total += float(item.get("reserved_base") or 0)
    return round(total, 6)


def stock_availability(repo, sku_id: str, exclude_order_id: str = "") -> dict:
    sku = repo.sku(sku_id)
    if not sku:
        raise OrderError("Product not found.", "item_not_found", sku_id=sku_id)
    physical = _physical_base(repo, sku)
    reserved = reserved_base(repo, sku_id, exclude_order_id)
    available = None if physical is None else max(0.0, physical - reserved)
    return {
        "sku_id": sku_id,
        "physical_base": physical,
        "reserved_base": reserved,
        "available_base": available,
        "base_unit": L.base_unit(sku),
    }


def _customer(repo, payload: dict) -> dict:
    customer = None
    if payload.get("customer_id"):
        customer = repo.customer(str(payload["customer_id"]))
    if not customer and payload.get("customer_phone"):
        customer = repo.customer_by_phone(payload["customer_phone"])
        if not customer and payload.get("customer_name"):
            customer = repo.upsert_customer(
                payload["customer_phone"], payload["customer_name"])
    if not customer:
        raise OrderError(
            "Select an existing customer or provide name and phone.",
            "customer_required",
        )
    return customer


def _normalise_items(repo, raw_items: list, order_id: str = "") -> list:
    if not isinstance(raw_items, list) or not raw_items:
        raise OrderError("Add at least one order item.", "items_required")
    out = []
    for index, raw in enumerate(raw_items, 1):
        sku = repo.sku(str(raw.get("sku_id") or ""))
        if not sku:
            raise OrderError(
                f"Line {index}: product not found.",
                "item_not_found", line=index, sku_id=raw.get("sku_id"))
        try:
            qty = float(raw.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            raise OrderError(
                f"Line {index}: quantity must be positive.",
                "invalid_quantity", line=index)
        unit = str(raw.get("unit") or sku.get("default_unit") or "").strip()
        try:
            requested_base = float(L.to_base(qty, unit, sku))
        except Exception:
            raise OrderError(
                f"Line {index}: {unit} is not valid for {sku['canonical']}.",
                "invalid_unit", line=index, unit=unit)
        rate = raw.get("rate")
        rate_unit = str(raw.get("rate_unit") or "").strip()
        try:
            if rate not in (None, ""):
                rate = float(rate)
                rate_unit = rate_unit or unit
            else:
                rate, rate_unit = _default_rate(repo, sku)
        except (TypeError, ValueError):
            raise OrderError(
                f"Line {index}: rate is invalid.", "invalid_rate", line=index)
        if rate < 0:
            raise OrderError(
                f"Line {index}: rate cannot be negative.",
                "invalid_rate", line=index)
        try:
            subtotal = float(L.line_amount(qty, unit, rate, rate_unit, sku))
        except Exception:
            raise OrderError(
                f"Line {index}: rate unit is invalid.",
                "invalid_rate_unit", line=index, rate_unit=rate_unit)
        gst_rate = float(repo.gst_rate_for(sku))
        gst_amount = subtotal * gst_rate / 100.0
        availability = stock_availability(repo, sku["sku_id"], order_id)
        available = availability["available_base"]
        status = (
            "uncounted" if available is None
            else "available" if available + 1e-9 >= requested_base
            else "backordered"
        )
        out.append({
            "line_id": f"line_{index:03d}",
            "sku_id": sku["sku_id"],
            "canonical": sku["canonical"],
            "qty": qty,
            "unit": unit,
            "rate": round(rate, 2),
            "rate_unit": rate_unit,
            "gst_rate": round(gst_rate, 2),
            "subtotal": round(subtotal, 2),
            "gst_amount": round(gst_amount, 2),
            "total": round(subtotal + gst_amount, 2),
            "requested_base": round(requested_base, 6),
            "reserved_base": 0.0,
            "availability_status": status,
        })
    return out


def create(repo, payload: dict, *, source: str = "manual") -> dict:
    customer = _customer(repo, payload)
    payment = str(payload.get("payment") or "cash").lower()
    if payment not in ("cash", "credit"):
        raise OrderError("Payment must be cash or credit.", "invalid_payment")
    deadline = str(payload.get("payment_deadline") or "").strip() or None
    if payment == "credit" and not deadline:
        raise OrderError(
            "Credit order needs a payment deadline.", "deadline_required")
    fulfilment = str(payload.get("fulfilment_method") or "delivery").lower()
    if fulfilment not in ("delivery", "pickup"):
        raise OrderError(
            "Fulfilment must be delivery or pickup.", "invalid_fulfilment")
    address = str(payload.get("delivery_address") or "").strip()
    if fulfilment == "delivery" and not address:
        raise OrderError(
            "Delivery order needs an address.", "delivery_address_required")
    items = _normalise_items(repo, payload.get("items") or [])
    subtotal = round(sum(item["subtotal"] for item in items), 2)
    gst_total = round(sum(item["gst_amount"] for item in items), 2)
    now = _now()
    row = {
        "customer_id": customer["customer_id"],
        "status": "draft",
        "source": source,
        "payment": payment,
        "payment_deadline": deadline,
        "fulfilment_method": fulfilment,
        "delivery_address": address,
        "requested_delivery_on": (
            str(payload.get("requested_delivery_on") or "").strip() or None),
        "notes": str(payload.get("notes") or "").strip(),
        "subtotal": subtotal,
        "gst_total": gst_total,
        "total": round(subtotal + gst_total, 2),
        "request_id": str(payload.get("request_id") or "").strip() or None,
        "sale_event_ids": [],
        "created_at": now,
        "updated_at": now,
        "items": items,
        "history": [{
            "from_status": None, "to_status": "draft", "note": "Order created",
            "source": source, "changed_at": now,
        }],
    }
    created = repo.create_order(row)
    if fulfilment == "delivery":
        repo.upsert_delivery(created["order_id"], {
            "status": "unscheduled",
            "address": address,
            "scheduled_for": row["requested_delivery_on"],
        })
    return get(repo, created["order_id"])


def get(repo, order_id: str) -> dict:
    order = repo.order(order_id)
    if not order:
        raise OrderError("Order not found.", "order_not_found", order_id=order_id)
    customer = repo.customer(order.get("customer_id"))
    order = dict(order)
    order["customer"] = customer
    if "delivery" not in order:
        order["delivery"] = repo.delivery_for_order(order_id)
    return order


def list_orders(repo, *, status: str = "", customer_id: str = "") -> list:
    rows = []
    for order in repo.orders():
        if status and order.get("status") != status:
            continue
        if customer_id and order.get("customer_id") != customer_id:
            continue
        rows.append(get(repo, order["order_id"]))
    return sorted(
        rows, key=lambda row: (row.get("created_at") or "", row["order_id"]),
        reverse=True)


def confirm(repo, order_id: str, *, allow_backorder: bool = False,
            source: str = "manual") -> dict:
    def _confirm(order):
        current = order.get("status")
        refreshing_backorder = (
            current == "confirmed"
            and any(item.get("availability_status") != "available"
                    for item in order.get("items") or [])
        )
        if current not in ("draft", "awaiting_confirmation",
                           "partially_available") and not refreshing_backorder:
            raise OrderError(
                f"Order is already {current}.", "invalid_transition",
                current=current, requested="confirmed")
        shortages = []
        for item in order.get("items") or []:
            availability = stock_availability(
                repo, item["sku_id"], exclude_order_id=order_id)
            available = availability["available_base"]
            requested = float(item.get("requested_base") or 0)
            if available is None:
                item["reserved_base"] = 0.0
                item["availability_status"] = "uncounted"
                shortages.append({
                    "sku_id": item["sku_id"], "name": item["canonical"],
                    "reason": "uncounted",
                })
            elif available + 1e-9 < requested:
                item["reserved_base"] = (
                    round(max(0.0, available), 6) if allow_backorder else 0.0)
                item["availability_status"] = "backordered"
                shortages.append({
                    "sku_id": item["sku_id"], "name": item["canonical"],
                    "requested_base": requested,
                    "available_base": round(available, 6),
                    "base_unit": availability["base_unit"],
                })
            else:
                item["reserved_base"] = round(requested, 6)
                item["availability_status"] = "available"
        target = "confirmed" if not shortages or allow_backorder \
            else "partially_available"
        order["status"] = target
        order["updated_at"] = _now()
        order.setdefault("history", []).append({
            "from_status": current, "to_status": target,
            "note": (
                "Confirmed with backorder" if shortages and allow_backorder
                else "Stock shortage found" if shortages
                else "Order confirmed and stock reserved"),
            "source": source, "changed_at": order["updated_at"],
        })

    guard = getattr(repo, "reservation_guard", None)
    with guard() if guard else nullcontext():
        updated = repo.update_order(order_id, _confirm)
    shortages = [{
        "sku_id": item["sku_id"],
        "name": item["canonical"],
        "reason": item.get("availability_status"),
    } for item in updated.get("items") or []
        if item.get("availability_status") != "available"]
    result = get(repo, order_id)
    result["shortages"] = shortages
    return result


def _sale_events_for_order(repo, order_id: str) -> list:
    request_id = f"order:{order_id}"
    return [
        event for event in repo.all_events()
        if (event.get("evidence") or {}).get("request_id") == request_id
        and event.get("type") == "sale"
    ]


def _write_sale(repo, order: dict) -> list:
    prior = _sale_events_for_order(repo, order["order_id"])
    if prior:
        return [event["event_id"] for event in prior]
    import main
    items = [{
        "sku_id": item["sku_id"],
        "qty": item["qty"],
        "unit": item["unit"],
        "rate": item["rate"],
        "rate_unit": item["rate_unit"],
        "payment": order["payment"],
        "customer_id": order["customer_id"],
        "payment_deadline": order.get("payment_deadline"),
        "spoken": f"Order {order['order_id']}",
    } for item in order.get("items") or []]
    result = main._write_events(
        "sale", items, clock.today().isoformat(), "exact",
        "order_fulfilment", request_id=f"order:{order['order_id']}")
    event_ids = [row["event_id"] for row in result["committed"]]
    if order.get("payment") == "credit":
        existing = next((
            receivable for receivable in repo.receivables()
            if set(receivable.get("sale_event_ids") or []) == set(event_ids)
        ), None)
        if not existing:
            deadline = order.get("payment_deadline") or (
                clock.today() + timedelta(days=30)).isoformat()
            repo.add_receivable(
                order["customer_id"], order["total"], deadline, event_ids)
    return event_ids


def transition(repo, order_id: str, to_status: str, *, note: str = "",
               source: str = "manual") -> dict:
    to_status = str(to_status or "").strip().lower()
    if to_status not in STATUSES:
        raise OrderError("Unknown order status.", "invalid_status",
                         requested=to_status)
    if to_status == "confirmed":
        return confirm(repo, order_id, source=source)

    def _transition(order):
        current = order.get("status")
        if to_status not in TRANSITIONS.get(current, set()):
            raise OrderError(
                f"Order cannot move from {current} to {to_status}.",
                "invalid_transition", current=current, requested=to_status)
        if to_status == "stock_allocated" and any(
                item.get("availability_status") != "available"
                for item in order.get("items") or []):
            raise OrderError(
                "Backordered items must be resolved before allocation.",
                "stock_not_allocated")
        sale_event_ids = (
            _write_sale(repo, order) if to_status == "delivered" else None)
        order["status"] = to_status
        order["updated_at"] = _now()
        if sale_event_ids is not None:
            order["sale_event_ids"] = sale_event_ids
        if to_status in ("delivered", "cancelled"):
            for item in order.get("items") or []:
                item["reserved_base"] = 0.0
        order.setdefault("history", []).append({
            "from_status": current, "to_status": to_status,
            "note": note, "source": source, "changed_at": order["updated_at"],
        })

    repo.update_order(order_id, _transition)
    if to_status in DELIVERY_STATUS:
        repo.upsert_delivery(order_id, {
            "status": DELIVERY_STATUS[to_status],
            **({"proof_note": note} if to_status in
               ("delivered", "delivery_failed") else {}),
        })
    return get(repo, order_id)


def update_delivery(repo, order_id: str, patch: dict) -> dict:
    order = get(repo, order_id)
    if order.get("fulfilment_method") != "delivery":
        raise OrderError("Pickup order has no delivery.", "not_a_delivery")
    allowed = {
        "provider", "provider_order_id", "tracking_url", "scheduled_for",
        "address", "driver_name", "driver_phone", "vehicle", "proof_note",
    }
    clean = {key: value for key, value in patch.items() if key in allowed}
    if not clean:
        raise OrderError("No delivery fields supplied.", "nothing_to_update")
    return repo.upsert_delivery(order_id, clean)


def stock_rows(repo) -> dict:
    """Reservation projection keyed by SKU for inventory and APIs."""
    out = {}
    for sku in repo.load_catalogue():
        availability = stock_availability(repo, sku["sku_id"])
        physical = availability["physical_base"]
        unit = sku.get("default_unit") or availability["base_unit"]
        reserved_display = L.from_base(
            availability["reserved_base"], unit, sku)
        available_base = (
            None if physical is None
            else max(0.0, physical - availability["reserved_base"]))
        out[sku["sku_id"]] = {
            **availability,
            "available_base": available_base,
            "reserved_display": round(reserved_display, 3),
            "available_display": (
                None if available_base is None
                else round(L.from_base(available_base, unit, sku), 3)),
            "display_unit": unit,
        }
    return out
