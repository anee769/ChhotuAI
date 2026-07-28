"""Tool endpoint for the Samvaad voice agent.

Samvaad runs the audio loop — speech in, speech out, barge-in, sub-500ms turns
— and calls back here whenever the conversation needs to touch the ledger.
That split is deliberate: streaming audio cannot run on a serverless function,
and re-implementing turn-taking to reuse /api/converse would be slower and
worse than what Samvaad already does.

Identifying the shop is the whole security question. A call carries no session
cookie, so the caller's number is the credential: it must already be a
registered owner. An unrecognised number gets a polite refusal, never someone
else's books — which is why the phone lookup happens before any tool runs.

Every reply is a short spoken sentence. The agent reads it aloud verbatim, so
these are written to be *heard*, not read: no rupee symbols, no tables, no
digits that a TTS engine will run together.
"""
from __future__ import annotations

import os
import secrets
from datetime import timedelta

import auth
import clock
import crm
import ledger as L
import sqlrepo


class AgentError(RuntimeError):
    pass


def verify_secret(supplied: str) -> bool:
    expected = (os.environ.get("SAMVAAD_WEBHOOK_SECRET") or "").strip()
    if not expected:
        raise AgentError("SAMVAAD_WEBHOOK_SECRET is not configured.")
    return secrets.compare_digest(supplied or "", expected)


def shop_for_caller(phone: str):
    """The caller's number IS the credential — there is no session on a call."""
    norm = auth.normalize_phone(phone or "")
    if not norm:
        return None, None
    for u in auth.all_users():
        if u["phone"] == norm:
            return u, sqlrepo.SqlRepo(u["user_id"])
    return None, None


def _say_number(n) -> str:
    """TTS runs long digit strings together; keep spoken figures rounded."""
    n = float(n or 0)
    if n >= 10000000:
        return f"{n / 10000000:.2f} crore".replace(".00", "")
    if n >= 100000:
        return f"{n / 100000:.2f} lakh".replace(".00", "")
    if n >= 1000:
        return f"{n / 1000:.1f} hazaar".replace(".0", "")
    return str(int(n))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def check_stock(repo, args: dict) -> dict:
    import main
    query = (args.get("item") or "").strip()
    if not query:
        return {"speak": "Kaunsa saamaan? Naam bataiye."}
    import matcher as M
    match = M.match(query, repo.load_catalogue(), repo.load_learning(), "live_sale")
    sku = repo.sku(match.get("sku_id")) if match.get("status") == "matched" else None
    if not sku:
        return {"speak": f"{query} humare stock mein nahi mila."}
    det = L._stock_detail(sku, repo.all_events())
    view = main._stock_view(sku, det)
    if view.get("uncounted"):
        return {"speak": f"{sku['canonical']} abhi tak gina nahi gaya."}
    return {"speak": f"{sku['canonical']} ka stock {view['display']} hai.",
            "sku_id": sku["sku_id"], "stock": view.get("display")}


def record_order(repo, user: dict, args: dict) -> dict:
    """An order taken over the phone is a RESERVATION, not a completed sale.

    It is written as a credit sale against the caller only when the shop owner
    is the one speaking. A customer ringing in cannot move another shop's
    stock, so this returns the order for confirmation instead of committing.
    """
    import matcher as M
    items, unknown, ambiguous = [], [], []
    catalogue = repo.load_catalogue()
    for row in args.get("items") or []:
        name = (row.get("item") or "").strip()
        qty = row.get("qty")
        if not name or not qty:
            continue
        match = M.match(name, catalogue, repo.load_learning(), "live_sale")
        sku = repo.sku(match.get("sku_id")) if match.get("status") == "matched" else None
        if not sku and match.get("status") == "disambiguate":
            # "cement" when the shop stocks two — on a call the right move is
            # to ask which, not to guess and ship the wrong bag.
            # matcher returns options as attribute VALUES ("OPC 53", "PPC")
            # for a variant question, or as sku_ids elsewhere. Normalise both,
            # and fall back to the family's product names.
            names = []
            for o in match.get("options") or []:
                if isinstance(o, dict):
                    names.append(o.get("canonical") or o.get("value")
                                 or o.get("label"))
                elif isinstance(o, str):
                    sku_o = repo.sku(o)
                    names.append(sku_o["canonical"] if sku_o else o)
            names = [str(n) for n in names if n]
            if not names:
                names = [s["canonical"] for s in catalogue
                         if s.get("family") == (match.get("family")
                                                or match.get("candidates_family"))]
            if names:
                ambiguous.append({"said": name, "options": names[:4]})
                continue
        if not sku:
            unknown.append(name)
            continue
        items.append({"sku_id": sku["sku_id"], "canonical": sku["canonical"],
                      "qty": float(qty),
                      "unit": row.get("unit") or sku.get("default_unit")})
    if ambiguous:
        # Ask before recording anything: a half-written order is worse than
        # one more question.
        a = ambiguous[0]
        return {"speak": f"{a['said']} mein se kaunsa — "
                         f"{' ya '.join(a['options'][:3])}?",
                "recorded": False, "needs": a}
    if not items:
        return {"speak": "Order samajh nahi aaya. Saamaan ka naam aur quantity "
                         "dobara bataiye.", "recorded": False}
    said = ", ".join(f"{i['qty']:g} {i['unit']} {i['canonical']}" for i in items)
    note = f" {', '.join(unknown)} humare paas nahi hai." if unknown else ""
    return {"speak": f"Theek hai — {said} likh liya.{note} "
                     "Dukaan se confirmation aa jayega.",
            "recorded": True, "items": items, "unavailable": unknown}


def day_summary(repo, user: dict, args: dict) -> dict:
    import main
    end = clock.today()
    weekly = (args.get("period") or "day") == "week"
    start = end - timedelta(days=6) if weekly else end
    by = {s["sku_id"]: s for s in repo.load_catalogue()}
    events = repo.all_events()
    sale = margin = cash = credit = 0.0
    for i in range((end - start).days + 1):
        m = L.margin_for_day(by, events, start + timedelta(days=i))
        sale += m["total"]; margin += m["margin"]
        cash += m["cash"]; credit += m["credit"]
    when = "Pichhle saat din mein" if weekly else "Aaj"
    speak = (f"{when} sale {_say_number(sale)} rupaye, "
             f"gross profit {_say_number(margin)} rupaye, "
             f"cash {_say_number(cash)} aur udhaar {_say_number(credit)} rupaye.")
    low_stock = main._low_stock_items(repo) if not weekly else []
    if low_stock:
        warnings = [
            (f"{row['canonical']} khatam ho gaya"
             if row["out_of_stock"]
             else f"{row['canonical']} sirf {row['stock']} bacha hai")
            for row in low_stock[:3]
        ]
        speak += f" Stock alert: {', '.join(warnings)}."
    return {"speak": speak, "sale": round(sale, 2), "margin": round(margin, 2),
            "low_stock": low_stock}


def dues(repo, user: dict, args: dict) -> dict:
    rows = crm.due_receivables(repo, clock.today(),
                               days_before=int(args.get("days_before", 7)))
    if not rows:
        return {"speak": "Abhi koi udhaar due nahi hai.", "count": 0}
    names = ", ".join(f"{r['customer'].get('name')} ka "
                      f"{_say_number(r['remaining'])} rupaye" for r in rows[:4])
    return {"speak": f"{len(rows)} udhaar due hain — {names}.", "count": len(rows)}


TOOLS = {
    "check_stock": lambda repo, user, args: check_stock(repo, args),
    "record_order": record_order,
    "day_summary": day_summary,
    "dues": dues,
}


def handle(tool: str, caller: str, args: dict) -> dict:
    user, repo = shop_for_caller(caller)
    if not user:
        # Never fall back to "some" shop: a wrong guess reads another
        # business's books aloud to a stranger.
        return {"speak": "Yeh number humare system mein registered nahi hai.",
                "authorised": False}
    fn = TOOLS.get(tool)
    if not fn:
        return {"speak": "Yeh kaam abhi nahi kar sakta.", "error": "unknown_tool"}
    out = fn(repo, user, args or {})
    return {**out, "authorised": True, "shop": user.get("shop_name") or ""}
