"""Single source of "now" for the whole app.

Every date-dependent number — today's margin, the 60-day frozen-capital window,
an udhaar deadline — used to be computed against a hardcoded 2026-07-26. That
is fine for a frozen demo and wrong for anything that runs on a timer: a
reminder scheduler asking "which dues fall two days from now" against a date
that never advances would fire on the same rows forever, or never fire at all.

`today()` is a FUNCTION, not a constant, on purpose. A module-level
`TODAY = date.today()` is evaluated once at import, so a server left running
overnight would keep insisting it is still yesterday — the same class of bug,
just harder to notice.

CHHOTU_TODAY pins the date (ISO yyyy-mm-dd) for the seeded demo, the
acceptance run and the tests. It is read on every call so it can be changed
without restarting the process.
"""
from __future__ import annotations

import os
from datetime import date

ENV_VAR = "CHHOTU_TODAY"


def today() -> date:
    pinned = (os.environ.get(ENV_VAR) or "").strip()
    if pinned:
        try:
            return date.fromisoformat(pinned)
        except ValueError:
            # A malformed pin must not silently freeze the clock at import
            # time — fall through to the real date.
            pass
    return date.today()


def today_iso() -> str:
    return today().isoformat()
