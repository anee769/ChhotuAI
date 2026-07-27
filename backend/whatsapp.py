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


def _post(to: str, body: str, from_: str, media_url: Optional[str] = None) -> dict:
    if not is_configured():
        raise MessagingError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
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
