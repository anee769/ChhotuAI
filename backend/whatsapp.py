"""Outbound messaging over Twilio — WhatsApp first, SMS as a fallback.

Twilio's WhatsApp Sandbox is what makes this usable today: it needs no Meta
template approval and no verified business number, so OTP and all three
business messages work on trial credit. The tradeoff is that a recipient must
opt in once by WhatsApp-ing the join code to the sandbox number, and the
sandbox only talks to numbers that have done so.

Production later means swapping TWILIO_WHATSAPP_FROM for an approved sender
and registering templates; the call sites below do not change.

SMS is kept as a fallback for OTP only. It costs more than WhatsApp in India
and, unlike WhatsApp, real Indian A2P SMS needs DLT registration — so it is
off unless TWILIO_SMS_FROM is set.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

API_ROOT = "https://api.twilio.com/2010-04-01"
TIMEOUT = 15


def _sid() -> str:
    return (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()


def _token() -> str:
    return (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()


def _wa_from() -> str:
    # Twilio's shared sandbox number, unless a real sender is configured.
    raw = (os.environ.get("TWILIO_WHATSAPP_FROM") or "+14155238886").strip()
    return raw if raw.startswith("whatsapp:") else f"whatsapp:{raw}"


def _sms_from() -> str:
    return (os.environ.get("TWILIO_SMS_FROM") or "").strip()


def is_configured() -> bool:
    return bool(_sid() and _token())


class MessagingError(RuntimeError):
    pass


def test_recipient() -> str:
    """Every outbound message is redirected here, when set.

    While the customer list holds real phone numbers and the Twilio account is
    a trial, a reminder run must not be able to reach a stranger. This is the
    single choke point — every send goes through _post, so no call site can
    route around it — and the intended recipient is kept in the body so the
    redirect is visible rather than silent.
    """
    return (os.environ.get("CHHOTU_TEST_RECIPIENT") or "").strip()


def _post(to: str, body: str, from_: str, media_url: Optional[str] = None) -> dict:
    if not is_configured():
        raise MessagingError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
    redirect = test_recipient()
    if redirect:
        intended = to
        prefix = "whatsapp:" if to.startswith("whatsapp:") else ""
        target = redirect if redirect.startswith("+") else "+" + redirect
        to = f"{prefix}{target}"
        if to != intended:
            body = (f"[TEST — would have gone to "
                    f"{intended.removeprefix('whatsapp:')}]\n\n{body}")
    data = {"To": to, "From": from_, "Body": body}
    if media_url:
        data["MediaUrl"] = media_url
    resp = requests.post(
        f"{API_ROOT}/Accounts/{_sid()}/Messages.json",
        data=data, auth=(_sid(), _token()), timeout=TIMEOUT)
    if resp.status_code >= 300:
        # Twilio's message is genuinely useful (63007 = recipient never joined
        # the sandbox, 21608 = number not verified on a trial account), so
        # surface it rather than a bare status code.
        raise MessagingError(f"{resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _wa(phone: str) -> str:
    p = phone if phone.startswith("+") else "+" + phone.lstrip("+")
    return f"whatsapp:{p}"


def send_whatsapp(phone: str, body: str, media_url: str = None) -> dict:
    return _post(_wa(phone), body, _wa_from(), media_url)


def send_sms(phone: str, body: str) -> dict:
    if not _sms_from():
        raise MessagingError("TWILIO_SMS_FROM not set")
    return _post(phone, body, _sms_from())


# ---------------------------------------------------------------------------
# The four messages the app actually sends
# ---------------------------------------------------------------------------
def send_otp(phone: str, code: str) -> dict:
    body = (f"{code} is your Chhotu.ai login code. It expires in 10 minutes.\n"
            "Do not share this code with anyone.")
    try:
        return send_whatsapp(phone, body)
    except MessagingError:
        # WhatsApp needs a one-time sandbox opt-in; SMS does not. Fall back
        # only if an SMS sender is configured.
        if _sms_from():
            return send_sms(phone, body)
        raise


def send_bill(phone: str, customer_name: str, total: float, shop: str,
              media_url: str = None) -> dict:
    body = (f"Namaste {customer_name}, your bill from {shop} is "
            f"Rs {total:,.0f}. Thank you for your business.")
    return send_whatsapp(phone, body, media_url)


def send_summary(phone: str, text: str) -> dict:
    return send_whatsapp(phone, text)


def send_reminder(phone: str, customer_name: str, amount: float, due_on: str,
                  shop: str) -> dict:
    body = (f"Namaste {customer_name}, a reminder from {shop}: "
            f"Rs {amount:,.0f} is due on {due_on}. "
            "Please ignore this message if you have already paid.")
    return send_whatsapp(phone, body)


# ---------------------------------------------------------------------------
# Delivery confirmation
# ---------------------------------------------------------------------------
TERMINAL = {"delivered", "read", "sent", "failed", "undelivered"}


def message_status(sid: str) -> dict:
    resp = requests.get(f"{API_ROOT}/Accounts/{_sid()}/Messages/{sid}.json",
                        auth=(_sid(), _token()), timeout=TIMEOUT)
    if resp.status_code >= 300:
        raise MessagingError(f"{resp.status_code}: {resp.text[:200]}")
    m = resp.json()
    return {"sid": sid, "status": m.get("status"),
            "error_code": m.get("error_code"), "error": m.get("error_message")}


def confirm(sid: str, wait_seconds: float = 6.0) -> dict:
    """Wait briefly for a terminal status.

    Twilio's create call returns `queued` and then fails ASYNCHRONOUSLY — an
    unjoined sandbox recipient (63015) or an unverified trial number (21608)
    both look like success at the moment of sending. Reporting a reminder as
    sent when it never arrived is worse than reporting the failure: the shop
    stops chasing money it is still owed.
    """
    import time
    deadline = time.time() + wait_seconds
    last = {"sid": sid, "status": "queued", "error_code": None}
    while time.time() < deadline:
        try:
            last = message_status(sid)
        except MessagingError:
            break
        if last["status"] in TERMINAL:
            break
        time.sleep(1.0)
    last["ok"] = last.get("status") in ("delivered", "read", "sent")
    return last
