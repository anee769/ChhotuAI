

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
