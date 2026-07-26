"""Deadline reminders with an SMSGate transport and a safe local outbox."""
from __future__ import annotations

import os
from datetime import date

import requests

import crm


def _message(customer: dict, due: dict) -> str:
    deadline = due["deadline"]
    amount = due["remaining"]
    return (f"Namaste {customer.get('name') or 'ji'}, Sharma Building Materials par "
            f"aapka Rs {amount:,.0f} udhaar {deadline} tak dena hai. "
            "Payment ho gaya ho to is message ko ignore karein.")


def _send_sms(phone: str, text: str) -> dict:
    endpoint = os.environ.get("SMSGATE_ENDPOINT", "").strip()
    username = os.environ.get("SMSGATE_USERNAME", "").strip()
    password = os.environ.get("SMSGATE_PASSWORD", "").strip()
    if not endpoint:
        return {"status": "queued", "provider": "outbox",
                "detail": "SMSGATE_ENDPOINT not configured"}
    try:
        r = requests.post(
            endpoint,
            auth=(username, password) if username or password else None,
            json={"textMessage": {"text": text}, "phoneNumbers": [phone]},
            timeout=15,
        )
        r.raise_for_status()
        return {"status": "sent", "provider": "smsgate",
                "provider_response": r.json() if r.content else {}}
    except Exception as e:
        return {"status": "failed", "provider": "smsgate", "detail": str(e)[:300]}


def run_due_reminders(repo, as_of: date) -> dict:
    existing = {(n.get("receivable_id"), n.get("kind"))
                for n in repo.notifications()
                if n.get("status") in ("queued", "sent")}
    created = []
    for due in crm.due_receivables(repo, as_of, days_before=2):
        key = (due["receivable_id"], "deadline_2_day")
        if key in existing:
            continue
        customer = due["customer"]
        text = _message(customer, due)
        delivery = _send_sms(customer["phone"], text)
        created.append(repo.add_notification({
            "kind": "deadline_2_day",
            "receivable_id": due["receivable_id"],
            "customer_id": customer["customer_id"],
            "phone": customer["phone"],
            "message": text,
            "scheduled_for": as_of.isoformat(),
            **delivery,
        }))
    return {"created": created, "due_count": len(created)}
