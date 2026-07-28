"""Tool surface for the Samvaad voice agent.

Chhotu's own dialogue engine (conversation.py) is a hand-written controller:
every question it can answer had to be anticipated and coded, which is why it
sounds canned the moment a caller phrases something new. That ceiling is why
this module exists. Samvaad brings the general intelligence and the audio loop
— speech in, speech out, barge-in, sub-500ms turns — and calls back here
whenever the conversation needs to touch the ledger. The split is deliberate:
streaming audio cannot run on a serverless function, and re-implementing
turn-taking to reuse /api/converse would be slower and worse than what Samvaad
already does.

The design rule for everything below: **return facts, not sentences.** Each
tool answers with structured data plus a short `speak` line as a fallback. The
agent is free to ignore `speak` and phrase the answer itself, in whatever
language and register the caller used — that is the whole point of moving off
the hardcoded controller. What we must never do is let the agent invent a
number, so a tool that doesn't know something says so explicitly rather than
returning an empty result the model will happily fill in.

Identifying the shop is the security question. A call carries no session
cookie, so identity arrives one of two ways:

  * ``caller``   — the number the call came from, which must already belong to
                   a registered owner. Used for telephony.
  * ``shop_key`` — an unguessable per-shop key derived from CHHOTU_SECRET,
                   handed to the logged-in browser and injected into the
                   Samvaad session as a variable. Used for in-app voice, where
                   the caller ID is whatever the browser claims and therefore
                   worthless.

Neither falls back to "some" shop. An unrecognised identity gets a refusal,
never someone else's books.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections import defaultdict
from datetime import timedelta

import auth
import clock
import crm
import ledger as L
import matcher as M
import sqlrepo

LOW_STOCK_THRESHOLD = 5.0


class AgentError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def verify_secret(supplied: str) -> bool:
    expected = (os.environ.get("SAMVAAD_WEBHOOK_SECRET") or "").strip()
    if not expected:
        raise AgentError("SAMVAAD_WEBHOOK_SECRET is not configured.")
    return secrets.compare_digest(supplied or "", expected)


def shop_key(user_id: str) -> str:
    """A per-shop capability token for the in-app voice session.

    Derived rather than stored, so there is nothing extra to migrate or leak,
    and tied to CHHOTU_SECRET so rotating that invalidates every issued key.
    """
    secret = os.environ.get("CHHOTU_SECRET") or ""
    if not secret:
        raise AgentError("CHHOTU_SECRET is not configured.")
    return hmac.new(secret.encode(), f"agent:{user_id}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def _resolved(value: str) -> str:
    """Drop a template variable the channel never filled in.

    A telephony session has no shop_key, so the console sends the literal
    "{{shop_key}}". That is a non-empty string, and treating it as a real
    credential would shadow the caller's number and lock out every phone call.
    """
    value = (value or "").strip()
    return "" if ("{{" in value or "}}" in value) else value


def shop_for_caller(phone: str = "", key: str = ""):
    """Resolve the shop from either identity. Never guesses.

    Both are credentials that must match exactly, so trying one and then the
    other is no weaker than trying only one — and it means a session that
    carries both, or a channel that fills in only one, still works.
    """
    key = _resolved(key)
    norm = auth.normalize_phone(_resolved(phone))
    if not key and not norm:
        # Nothing to match on. Bail before touching the database, so a stream
        # of junk caller IDs cannot turn into a stream of queries.
        return None, None
    for u in auth.all_users():
        if key and secrets.compare_digest(shop_key(u["user_id"]), key):
            return u, sqlrepo.SqlRepo(u["user_id"])
        if norm and u["phone"] == norm:
            return u, sqlrepo.SqlRepo(u["user_id"])
    return None, None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
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


def _g(n) -> str:
    return f"{float(n or 0):g}"


def _stock_of(repo, sku: dict, events=None) -> dict:
    det = L._stock_detail(sku, events if events is not None else repo.all_events())
    unit = det.get("unit") or sku.get("default_unit")
    if det["qty"] == L.UNCOUNTED:
        return {"counted": False, "qty": None, "unit": unit,
                "text": "abhi tak gina nahi gaya"}
    qty = det["qty"]
    return {"counted": True, "qty": round(qty, 3), "unit": unit,
            "estimated": bool(det.get("estimated")),
            "oversold": qty < 0,
            "low": 0 <= qty <= LOW_STOCK_THRESHOLD,
            "text": f"{'~' if det.get('estimated') else ''}{_g(qty)} {unit}"}


def _find_sku(repo, phrase: str):
    """Returns (sku, question) — exactly one is set, both None if no match.

    A voice agent that guesses between two cements ships the wrong bag, so an
    ambiguous phrase comes back as a question for the caller, not a pick.
    """
    phrase = (phrase or "").strip()
    if not phrase:
        return None, None
    catalogue = repo.load_catalogue()
    m = M.match(phrase, catalogue, repo.load_learning(), "live_sale")
    if m.get("status") == "matched":
        return repo.sku(m.get("sku_id")), None
    if m.get("status") == "disambiguate":
        # The matcher returns options as attribute VALUES ("OPC 53", "PPC") for
        # a variant question and as sku_ids elsewhere. Normalise both.
        names = []
        for o in m.get("options") or []:
            if isinstance(o, dict):
                names.append(o.get("canonical") or o.get("value") or o.get("label"))
            elif isinstance(o, str):
                s = repo.sku(o)
                names.append(s["canonical"] if s else o)
        names = [str(n) for n in names if n]
        if not names:
            fam = m.get("family") or m.get("candidates_family")
            names = [s["canonical"] for s in catalogue if s.get("family") == fam]
        if names:
            return None, {"said": phrase, "options": names[:4]}
    return None, None


# What kind of shop this is, in the words a shopkeeper would use. Inferred from
# the registered name so it works without extra setup, and overridable in
# config for the shop whose name gives nothing away ("Sharma & Sons").
_SHOP_KINDS = (
    ("hardware", "hardware"),
    ("building", "building material"),
    ("construction", "construction material"),
    ("builder", "building material"),
    ("timber", "timber aur plywood"),
    ("plywood", "timber aur plywood"),
    ("sanitary", "sanitary aur plumbing"),
    ("electric", "electrical"),
    ("paint", "paint aur hardware"),
    ("cement", "building material"),
    ("steel", "building material"),
    ("traders", "hardware aur building material"),
)
_DEFAULT_SHOP_KIND = "hardware aur building material"


def _shop_kind(repo, user) -> str:
    cfg = repo.load_config()
    if cfg.get("shop_type"):
        return str(cfg["shop_type"])
    name = (cfg.get("shop_name") or user.get("shop_name") or "").lower()
    for keyword, label in _SHOP_KINDS:
        if keyword in name:
            return label
    return _DEFAULT_SHOP_KIND


def _not_stocked(repo, user, phrase: str) -> dict:
    """A miss, with enough context for the agent to answer like a shopkeeper.

    "Nahi mila" is a database answer. A shopkeeper distinguishes two very
    different misses: hardware they simply don't carry ("pipe abhi nahi
    rakhte"), and something outside their trade entirely ("hum hardware ki
    dukaan hain"). The difference is whether the word matches a known hardware
    category, so return that judgement and let the agent phrase it.

    The trade is named as a trade, not as a list of the shelves. Answering
    "hum cement, tiles, tmt ki dukaan hain" describes an inventory table; a
    shopkeeper says what line of business they are in.
    """
    import conversation as C
    kind = _shop_kind(repo, user)
    category = C._match_hardware_category(phrase or "")
    if category:
        speak = f"{phrase} hum abhi nahi rakhte."
    else:
        speak = f"Hum {kind} ki dukaan hain, {phrase} hum nahi rakhte."
    return {"found": False, "item": phrase, "shop_kind": kind,
            "shop_sells": sorted({s.get("family") for s in repo.load_catalogue()
                                  if s.get("family")}),
            "known_hardware_category": category,
            "stocks_this_kind": bool(category), "speak": speak}


def _ask_which(question: dict) -> dict:
    return {"needs": question,
            "speak": f"{question['said']} mein se kaunsa, "
                     f"{' ya '.join(question['options'][:3])}?"}


def _customer_by_name(repo, name: str):
    """Returns (account, options). Owners speak names, never customer ids."""
    want = (name or "").strip().lower()
    if not want:
        return None, []
    accounts = crm.accounts(repo)
    exact = [a for a in accounts if (a.get("name") or "").lower() == want]
    if len(exact) == 1:
        return exact[0], []
    part = [a for a in accounts if want in (a.get("name") or "").lower()]
    if len(part) == 1:
        return part[0], []
    return None, [{"name": a.get("name"), "phone": a.get("phone"),
                   "outstanding": a["outstanding"]} for a in (exact or part)[:5]]


def _ask_which_customer(options: list) -> dict:
    return {"needs": {"options": options},
            "speak": "Kaunse, " + " ya ".join(o["name"] for o in options[:3]) + "?"}


def _period_range(args: dict):
    """day | yesterday | week | month | {days: N} | {start, end}.

    Resolved here rather than in the agent's prompt so the model can pass
    through roughly whatever the caller said without doing date arithmetic —
    which is exactly the kind of thing an LLM gets quietly wrong.
    """
    end = clock.today()
    if args.get("start") and args.get("end"):
        return L._d(args["start"]), L._d(args["end"])
    if args.get("days"):
        return end - timedelta(days=max(int(args["days"]), 1) - 1), end
    period = (args.get("period") or "day").lower()
    if period in ("week", "weekly", "hafta", "hafte"):
        return end - timedelta(days=6), end
    if period in ("month", "monthly", "mahina", "mahine"):
        return end - timedelta(days=29), end
    if period in ("yesterday", "kal"):
        return end - timedelta(days=1), end - timedelta(days=1)
    return end, end


def _totals(repo, start, end) -> dict:
    by = {s["sku_id"]: s for s in repo.load_catalogue()}
    events = repo.all_events()
    out = {"sale": 0.0, "margin": 0.0, "cash": 0.0, "credit": 0.0}
    for i in range((end - start).days + 1):
        m = L.margin_for_day(by, events, start + timedelta(days=i))
        out["sale"] += m["total"]
        out["margin"] += m["margin"]
        out["cash"] += m["cash"]
        out["credit"] += m["credit"]
    return {k: round(v, 2) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
def shop_profile(repo, user, args):
    cfg = repo.load_config()
    catalogue = repo.load_catalogue()
    accounts = crm.accounts(repo)
    outstanding = round(sum(a["outstanding"] for a in accounts), 2)
    return {
        "shop": cfg.get("shop_name") or user.get("shop_name") or "",
        "owner": user.get("name") or "",
        "phone": user.get("phone") or "",
        "gstin": cfg.get("gstin") or "",
        "address": cfg.get("address") or "",
        "shop_kind": _shop_kind(repo, user),
        "today": clock.today().isoformat(),
        "item_count": len(catalogue),
        "customer_count": len(accounts),
        "total_outstanding": outstanding,
        "speak": f"{cfg.get('shop_name') or 'Dukaan'} mein {len(catalogue)} item, "
                 f"{len(accounts)} customer, {_say_number(outstanding)} rupaye "
                 "udhaar baaki.",
    }


def list_inventory(repo, user, args):
    events = repo.all_events()
    items = []
    for sku in repo.load_catalogue():
        st = _stock_of(repo, sku, events)
        items.append({"sku_id": sku["sku_id"], "name": sku["canonical"],
                      "family": sku.get("family"), "brand": sku.get("brand"),
                      "unit": st["unit"], "stock": st["qty"],
                      "counted": st["counted"], "low": st.get("low", False),
                      "selling_rate": sku.get("selling_rate"),
                      "cost_price": sku.get("cost_price")})
    items.sort(key=lambda i: i["name"])
    names = ", ".join(i["name"] for i in items[:6])
    return {"count": len(items), "items": items,
            "speak": f"{len(items)} item hain: {names}"
                     + (" aur bhi." if len(items) > 6 else ".")}


def check_stock(repo, user, args):
    sku, question = _find_sku(repo, args.get("item"))
    if question:
        return _ask_which(question)
    if not sku:
        return _not_stocked(repo, user, args.get("item"))
    st = _stock_of(repo, sku)
    return {"found": True, "sku_id": sku["sku_id"], "name": sku["canonical"],
            **st, "selling_rate": sku.get("selling_rate"),
            "speak": f"{sku['canonical']} ka stock {st['text']} hai."}


def item_details(repo, user, args):
    sku, question = _find_sku(repo, args.get("item"))
    if question:
        return _ask_which(question)
    if not sku:
        return _not_stocked(repo, user, args.get("item"))
    events = repo.events_for_sku(sku["sku_id"])
    cost = L.landed_cost_as_of(sku, events, clock.today())
    sales = sorted((e for e in events if e["type"] == "sale"),
                   key=lambda e: e.get("occurred_on", ""))
    st = _stock_of(repo, sku)
    return {"found": True, "sku_id": sku["sku_id"], "name": sku["canonical"],
            "brand": sku.get("brand"), "family": sku.get("family"),
            "unit": sku.get("default_unit"), "attributes": sku.get("attributes"),
            "selling_rate": sku.get("selling_rate"), "landed_cost": cost,
            "gst_rate": repo.gst_rate_for(sku), "stock": st,
            "last_sold_on": sales[-1]["occurred_on"] if sales else None,
            "speak": f"{sku['canonical']}: stock {st['text']}, cost "
                     f"{_say_number(cost or 0)} rupaye."}


def search_items(repo, user, args):
    q = (args.get("query") or args.get("item") or "").strip().lower()
    catalogue = repo.load_catalogue()
    hits = [s for s in catalogue
            if q and (q in (s["canonical"] or "").lower()
                      or q in (s.get("brand") or "").lower()
                      or q in (s.get("family") or "").lower())]
    if not hits and q:
        # Fall back to the same fuzzy ranking the matcher uses, so a caller's
        # approximate word still lands somewhere useful.
        ids = [c.get("sku_id") if isinstance(c, dict) else c
               for c in M.fuzzy_candidates(q, catalogue, limit=5)]
        hits = [s for s in (repo.sku(i) for i in ids if i) if s]
    events = repo.all_events()
    rows = [{"sku_id": s["sku_id"], "name": s["canonical"],
             "stock": _stock_of(repo, s, events)["text"],
             "selling_rate": s.get("selling_rate")} for s in hits]
    return {"count": len(rows), "items": rows,
            "speak": ", ".join(r["name"] for r in rows[:5]) or "Kuch nahi mila."}


def low_stock(repo, user, args):
    """Uses the same velocity model as the dashboard, so the voice answer and
    the screen never disagree about what is running out."""
    import main
    rows = main._low_stock_items(repo, limit=int(args.get("limit") or 8))
    if not rows:
        return {"count": 0, "items": [], "speak": "Koi item kam nahi hai."}
    said = ", ".join(f"{r['canonical']} khatam ho gaya" if r.get("out_of_stock")
                     else f"{r['canonical']} sirf {r['stock']} bacha hai"
                     for r in rows[:4])
    return {"count": len(rows), "items": rows,
            "speak": f"{len(rows)} item kam hain: {said}."}


def business_summary(repo, user, args):
    import main
    start, end = _period_range(args)
    t = _totals(repo, start, end)
    single_day = start == end
    label = "Aaj" if single_day and end == clock.today() else \
            (start.isoformat() if single_day
             else f"{start.isoformat()} se {end.isoformat()} tak")
    speak = (f"{label} sale {_say_number(t['sale'])} rupaye, gross profit "
             f"{_say_number(t['margin'])} rupaye, cash {_say_number(t['cash'])} "
             f"aur udhaar {_say_number(t['credit'])} rupaye.")
    alerts = main._low_stock_items(repo) if single_day else []
    if alerts:
        speak += " Stock alert: " + ", ".join(
            f"{r['canonical']} khatam ho gaya" if r.get("out_of_stock")
            else f"{r['canonical']} sirf {r['stock']} bacha hai"
            for r in alerts[:3]) + "."
    return {"start": start.isoformat(), "end": end.isoformat(), **t,
            "low_stock": alerts, "speak": speak}


def top_items(repo, user, args):
    start, end = _period_range({"days": args.get("days") or 30})
    by = {s["sku_id"]: s for s in repo.load_catalogue()}
    qty = defaultdict(float)
    revenue = defaultdict(float)
    for e in repo.all_events():
        if e["type"] != "sale":
            continue
        sku = by.get(e["sku_id"])
        if not sku or not (start <= L._d(e["occurred_on"]) <= end):
            continue
        base = L.to_base(float(e.get("qty") or 0),
                         e.get("unit") or L.base_unit(sku), sku)
        qty[e["sku_id"]] += base
        revenue[e["sku_id"]] += base * float(e.get("rate") or 0)
    rows = [{"sku_id": k, "name": by[k]["canonical"], "qty_sold": round(v, 2),
             "unit": L.base_unit(by[k]), "revenue": round(revenue[k], 2)}
            for k, v in qty.items()]
    slow = (args.get("order") or "top") == "slow"
    rows.sort(key=lambda r: r["revenue"], reverse=not slow)
    rows = rows[:int(args.get("limit") or 5)]
    return {"from": start.isoformat(), "to": end.isoformat(), "items": rows,
            "speak": ", ".join(f"{r['name']} {_say_number(r['revenue'])} rupaye"
                               for r in rows) or "Is period mein koi sale nahi."}


def list_customers(repo, user, args):
    rows = [{"customer_id": a["customer_id"], "name": a.get("name"),
             "phone": a.get("phone"), "outstanding": a["outstanding"],
             "total_credit": a["total_credit"],
             "next_deadline": a.get("next_deadline")} for a in crm.accounts(repo)]
    owing = [r for r in rows if r["outstanding"] > 0]
    return {"count": len(rows), "customers": rows, "owing_count": len(owing),
            "speak": f"{len(rows)} customer hain, {len(owing)} ka udhaar baaki hai."}


def customer_account(repo, user, args):
    acc, options = _customer_by_name(repo, args.get("name") or args.get("customer"))
    if not acc:
        if options:
            return _ask_which_customer(options)
        return {"found": False,
                "speak": f"{args.get('name')} naam ka koi customer nahi mila."}
    return {"found": True, "customer_id": acc["customer_id"], "name": acc.get("name"),
            "phone": acc.get("phone"), "outstanding": acc["outstanding"],
            "total_credit": acc["total_credit"], "total_paid": acc["total_paid"],
            "next_deadline": acc.get("next_deadline"),
            "open_dues": [{"amount": d["amount"], "remaining": d["remaining"],
                           "deadline": d["deadline"]} for d in acc["open_dues"]],
            "speak": (f"{acc.get('name')} ka {_say_number(acc['outstanding'])} "
                      "rupaye baaki hai." if acc["outstanding"] > 0
                      else f"{acc.get('name')} ka koi udhaar baaki nahi.")}


def dues(repo, user, args):
    rows = crm.due_receivables(repo, clock.today(),
                               days_before=int(args.get("days_before", 7)))
    if not rows:
        return {"count": 0, "dues": [], "speak": "Abhi koi udhaar due nahi hai."}
    out = [{"name": r["customer"].get("name"), "phone": r["customer"].get("phone"),
            "remaining": r["remaining"], "deadline": r["deadline"],
            "days_until_deadline": r["days_until_deadline"]} for r in rows]
    said = ", ".join(f"{r['name']} ka {_say_number(r['remaining'])} rupaye"
                     for r in out[:4])
    return {"count": len(out), "dues": out,
            "speak": f"{len(out)} udhaar due hain: {said}."}


def recent_activity(repo, user, args):
    n = int(args.get("limit") or 8)
    by = {s["sku_id"]: s for s in repo.load_catalogue()}
    events = sorted(repo.all_events(),
                    key=lambda e: (e.get("occurred_on") or "",
                                   e.get("event_id") or ""))
    rows = []
    for e in reversed(events[-n:]):
        sku = by.get(e["sku_id"])
        rows.append({"date": e.get("occurred_on"), "type": e["type"],
                     "item": sku["canonical"] if sku else e["sku_id"],
                     "qty": e.get("qty"), "unit": e.get("unit"),
                     "rate": e.get("quoted_rate") or e.get("rate"),
                     "payment": e.get("payment")})
    return {"count": len(rows), "events": rows,
            "speak": "; ".join(f"{r['date']} {r['type']} {_g(r['qty'])} "
                               f"{r['unit']} {r['item']}" for r in rows[:4])
                     or "Abhi koi entry nahi."}


def price_quote(repo, user, args):
    """What a basket costs, GST included — before anything is recorded."""
    lines, unknown = [], []
    subtotal = gst_total = 0.0
    for row in args.get("items") or []:
        sku, question = _find_sku(repo, row.get("item"))
        if question:
            return _ask_which(question)
        if not sku:
            unknown.append(row.get("item"))
            continue
        qty = float(row.get("qty") or 0)
        unit = row.get("unit") or sku.get("default_unit")
        rate = row.get("rate")
        if rate is None:
            rate = sku.get("selling_rate")
        if rate is None:
            cost = L.landed_cost_as_of(sku, repo.events_for_sku(sku["sku_id"]),
                                       clock.today())
            rate = round((cost or 0) * 1.10, 1)
        amount = L.line_amount(qty, unit, float(rate), unit, sku)
        gst = amount * float(repo.gst_rate_for(sku)) / 100.0
        subtotal += amount
        gst_total += gst
        lines.append({"sku_id": sku["sku_id"], "name": sku["canonical"],
                      "qty": qty, "unit": unit, "rate": float(rate),
                      "amount": round(amount, 2), "gst": round(gst, 2)})
    total = round(subtotal + gst_total, 2)
    return {"lines": lines, "subtotal": round(subtotal, 2),
            "gst": round(gst_total, 2), "total": total, "unavailable": unknown,
            "speak": f"Total {_say_number(total)} rupaye, GST milaakar."}


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------
def _already_written(repo, request_id: str) -> list:
    """Events from an earlier attempt carrying this exact request_id.

    Deliberately keyed on an id the agent supplies rather than on "an identical
    sale in the last two minutes" — two customers really can buy ten bags of
    the same cement minutes apart, and silently dropping the second sale would
    be worse than the duplicate it was meant to prevent.
    """
    if not request_id:
        return []
    return [e for e in repo.all_events()
            if (e.get("evidence") or {}).get("request_id") == request_id]


def _commit(repo, user, etype: str, args: dict, source: str = "voice_agent") -> dict:
    """Resolve spoken items to SKUs and append events. Asks before guessing."""
    import main
    request_id = str(args.get("request_id") or "").strip()[:64]
    prior = _already_written(repo, request_id)
    if prior:
        return {"recorded": True, "duplicate": True, "unavailable": [],
                "_items": [{"sku_id": e["sku_id"], "qty": e.get("qty"),
                            "unit": e.get("unit")} for e in prior],
                "_result": {"committed": [{"event_id": e.get("event_id"),
                                           "sku_id": e["sku_id"], "amount": 0}
                                          for e in prior],
                            "affected_stock": {}}}
    items, unknown = [], []
    for row in args.get("items") or []:
        sku, question = _find_sku(repo, row.get("item"))
        if question:
            return {"recorded": False, **_ask_which(question)}
        if not sku:
            unknown.append(row.get("item"))
            continue
        if not row.get("qty"):
            return {"recorded": False,
                    "needs": {"said": row.get("item"), "field": "qty"},
                    "speak": f"{sku['canonical']} kitna?"}
        unit = row.get("unit") or sku.get("default_unit")
        items.append({"sku_id": sku["sku_id"], "qty": float(row["qty"]),
                      "unit": unit, "rate": row.get("rate"),
                      "rate_unit": unit if row.get("rate") is not None else None,
                      "payment": args.get("payment"),
                      "customer_id": (args.get("customer_id")
                                      if etype == "sale" else None),
                      "payment_deadline": args.get("payment_deadline"),
                      "spoken": row.get("item", "")})
    if not items:
        return {"recorded": False, "unavailable": unknown,
                "speak": "Saamaan samajh nahi aaya. Naam aur quantity dobara bataiye."}
    result = main._write_events(etype, items,
                                args.get("occurred_on") or clock.today().isoformat(),
                                args.get("precision", "exact"), source,
                                request_id=request_id)
    return {"recorded": True, "unavailable": unknown,
            "_items": items, "_result": result}


def _said(repo, items) -> str:
    return ", ".join(f"{_g(i['qty'])} {i['unit']} "
                     f"{(repo.sku(i['sku_id']) or {}).get('canonical', '')}"
                     for i in items)


def record_sale(repo, user, args):
    """A sale, cash or credit. Credit needs a named customer and a deadline."""
    payment = (args.get("payment") or "cash").lower()
    customer = None
    name = args.get("customer") or args.get("customer_name")
    if name:
        acc, options = _customer_by_name(repo, name)
        if not acc and options:
            return {"recorded": False, **_ask_which_customer(options)}
        customer = acc
        if not customer and args.get("customer_phone"):
            customer = repo.upsert_customer(args["customer_phone"], name)
    if payment == "credit" and not customer:
        # Udhaar with nobody's name on it is unrecoverable money.
        return {"recorded": False, "needs": {"field": "customer"},
                "speak": "Udhaar kiske naam par likhun? Naam aur number bataiye."}

    out = _commit(repo, user, "sale",
                  {**args, "payment": payment,
                   "customer_id": (customer or {}).get("customer_id")})
    if not out.get("recorded"):
        return out
    res, items = out.pop("_result"), out.pop("_items")
    if out.get("duplicate"):
        # Already written, receivable included. Re-running either would double
        # the customer's debt, which is the expensive half of this mistake.
        return {**out, "lines": res["committed"],
                "speak": "Ye entry pehle hi ho chuki hai."}
    total = round(sum(c["amount"] for c in res["committed"]), 2)
    receivable = None
    if payment == "credit" and customer:
        deadline = args.get("payment_deadline") or \
            (clock.today() + timedelta(days=30)).isoformat()
        receivable = repo.add_receivable(customer["customer_id"], total, deadline,
                                         [c["event_id"] for c in res["committed"]])
    return {**out, "total": total, "payment": payment,
            "customer": (customer or {}).get("name"),
            "lines": res["committed"], "stock_after": res["affected_stock"],
            "receivable": receivable,
            "speak": f"{_said(repo, items)} likh liya, "
                     f"{_say_number(total)} rupaye, {payment}."}


def record_purchase(repo, user, args):
    """Stock coming in from a supplier. `rate` here is the cost price."""
    out = _commit(repo, user, "delivery", args)
    if not out.get("recorded"):
        return out
    res, items = out.pop("_result"), out.pop("_items")
    return {**out, "lines": res["committed"],
            "stock_after": res["affected_stock"],
            "speak": f"{_said(repo, items)} stock mein add kar diya."}


def stock_take(repo, user, args):
    """A physical count. Overwrites the derived figure for that item."""
    out = _commit(repo, user, "stock_take", args)
    if not out.get("recorded"):
        return out
    res, items = out.pop("_result"), out.pop("_items")
    return {**out, "stock_after": res["affected_stock"],
            "speak": f"Ginti update kar di: {_said(repo, items)}."}


def record_payment(repo, user, args):
    """Cash received against an outstanding udhaar."""
    acc, options = _customer_by_name(repo, args.get("customer") or args.get("name"))
    if not acc:
        if options:
            return {"recorded": False, **_ask_which_customer(options)}
        return {"recorded": False, "speak": "Ye customer nahi mila."}
    try:
        amount = float(args.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return {"recorded": False, "needs": {"field": "amount"},
                "speak": "Kitne rupaye mile?"}
    if amount > acc["outstanding"] + 0.01:
        # Refuse rather than create a negative balance the owner has to unpick.
        return {"recorded": False, "outstanding": acc["outstanding"],
                "speak": f"{acc.get('name')} ka sirf "
                         f"{_say_number(acc['outstanding'])} rupaye baaki hai."}
    request_id = str(args.get("request_id") or "").strip()[:64]
    note = args.get("note") or "voice agent"
    if request_id:
        note = f"{note} [{request_id}]"
        if any(f"[{request_id}]" in (p.get("note") or "")
               for p in repo.payments()):
            return {"recorded": True, "duplicate": True,
                    "customer": acc.get("name"), "outstanding": acc["outstanding"],
                    "speak": "Ye payment pehle hi jama ho chuki hai."}
    repo.add_payment(acc["customer_id"], amount,
                     args.get("paid_on") or clock.today().isoformat(), note)
    after = crm.account(repo, acc["customer_id"])
    return {"recorded": True, "amount": amount, "customer": acc.get("name"),
            "outstanding": after["outstanding"],
            "speak": f"{_say_number(amount)} rupaye jama kar diye. "
                     f"{acc.get('name')} ka ab "
                     f"{_say_number(after['outstanding'])} rupaye baaki hai."}


def update_shop_profile(repo, user, args):
    """Shop name, owner, GSTIN, address — the letterhead on every bill.

    The owner's name lives on the users row rather than in config, because auth
    reads it too, so this writes both sides and returns the merged result.
    """
    fields = {k: str(args[k]).strip()
              for k in ("shop_name", "shop_type", "gstin", "address")
              if args.get(k) not in (None, "")}
    owner = str(args.get("owner") or args.get("name") or "").strip()
    if not fields and not owner:
        return {"updated": False,
                "speak": "Kya badalna hai: dukaan ka naam, GSTIN ya address?"}
    if fields:
        repo.save_config(fields)
    if owner or fields.get("shop_name"):
        auth.complete_onboarding(user["user_id"], name=owner,
                                 shop_name=fields.get("shop_name", ""))
        if owner:
            user["name"] = owner
    changed = sorted(list(fields) + (["owner"] if owner else []))
    return {"updated": True, "changed": changed,
            "profile": {**fields, "owner": owner or user.get("name") or ""},
            "speak": f"{', '.join(changed)} update kar diya."}


def add_item(repo, user, args):
    """A new SKU. Cost price is required — margin maths is useless without it."""
    name = (args.get("name") or args.get("item") or "").strip()
    if not name:
        return {"added": False, "speak": "Naye item ka naam bataiye."}
    if args.get("cost_price") in (None, ""):
        return {"added": False, "needs": {"field": "cost_price"},
                "speak": f"{name} ka cost price kya hai?"}
    existing, _ = _find_sku(repo, name)
    if existing:
        return {"added": False, "sku_id": existing["sku_id"],
                "speak": f"{existing['canonical']} pehle se list mein hai."}
    unit = args.get("unit") or "piece"
    sku_id = "sku_" + hashlib.sha1(name.lower().encode()).hexdigest()[:8]
    repo.upsert_sku({
        "sku_id": sku_id, "canonical": name,
        "brand": args.get("brand") or "",
        "family": args.get("family") or name,
        "default_unit": unit, "units": {unit: 1},
        "cost_price": float(args["cost_price"]),
        "selling_rate": (float(args["selling_rate"])
                         if args.get("selling_rate") else None),
        "attributes": args.get("attributes") or {},
    })
    return {"added": True, "sku_id": sku_id, "name": name, "unit": unit,
            "speak": f"{name} list mein add kar diya."}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send_bill(repo, user, args):
    import notify
    acc, options = _customer_by_name(repo, args.get("customer") or args.get("name"))
    if not acc:
        if options:
            return {"sent": False, **_ask_which_customer(options)}
        return {"sent": False, "speak": "Ye customer nahi mila."}
    customer = repo.customer(acc["customer_id"]) or acc
    if not args.get("items"):
        return {"sent": False, "needs": {"field": "items"},
                "speak": "Bill mein kya-kya daalna hai?"}
    lines = []
    for row in args["items"]:
        if row.get("sku_id"):
            lines.append(row)
            continue
        sku, question = _find_sku(repo, row.get("item"))
        if question:
            return {"sent": False, **_ask_which(question)}
        if not sku:
            return {"sent": False,
                    "speak": f"{row.get('item')} nahi mila, bill nahi bhej saka."}
        lines.append({"sku_id": sku["sku_id"], "qty": row.get("qty"),
                      "unit": row.get("unit") or sku.get("default_unit"),
                      "rate": row.get("rate") or sku.get("selling_rate") or 0})
    try:
        out = notify.send_bill(repo, user, customer, lines,
                               payment=args.get("payment") or "cash",
                               due_on=args.get("payment_deadline"))
    except Exception as e:
        return {"sent": False, "error": str(e)[:200],
                "speak": "Bill nahi bhej saka."}
    return {"sent": True, **out,
            "speak": f"{customer.get('name')} ko bill bhej diya, "
                     f"{_say_number(out['total'])} rupaye."}


def send_summary(repo, user, args):
    import notify
    period = "week" if (args.get("period") or "day") == "week" else "day"
    try:
        out = notify.send_summary(repo, user, period=period)
    except Exception as e:
        return {"sent": False, "error": str(e)[:200],
                "speak": "Summary nahi bhej saka."}
    return {"sent": True, **out,
            "speak": f"{'Hafte' if period == 'week' else 'Aaj'} ki summary "
                     "WhatsApp par bhej di."}


def send_reminders(repo, user, args):
    import notify
    try:
        out = notify.send_due_reminders(repo, user,
                                        days_before=int(args.get("days_before", 2)))
    except Exception as e:
        return {"sent": False, "error": str(e)[:200],
                "speak": "Reminder nahi bhej saka."}
    sent = out.get("sent")
    n = sent if isinstance(sent, int) else len(sent or [])
    return {"sent": True, **out,
            "speak": f"{n} customer ko reminder bhej diya."}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# `description` is what the Samvaad console shows the agent — it is how the
# model decides which tool to call, so these are written for the model, not for
# a developer reading the code.
TOOLS = {
    "shop_profile": (shop_profile,
                     "Dukaan ka naam, owner, GSTIN, aaj ki date, kitne item aur "
                     "customer hain, kul udhaar. Call ke shuru mein context ke liye."),
    "list_inventory": (list_inventory,
                       "Poori inventory: har item ka naam, stock, unit, rate. "
                       "'kya kya hai', 'stock list' jaise sawaal ke liye."),
    "check_stock": (check_stock,
                    "Ek item ka current stock. args: item (jo caller ne bola, "
                    "Hindi, English ya mix, kuch bhi chalega)."),
    "item_details": (item_details,
                     "Ek item ki poori detail: cost price, selling rate, GST, "
                     "stock, aakhri sale kab hui. args: item."),
    "search_items": (search_items,
                     "Naam ya brand se item dhoondo jab caller ka shabd exact "
                     "na ho. args: query."),
    "low_stock": (low_stock,
                  "Woh item jo khatam ho gaye ya khatam hone waale hain. "
                  "args: limit (optional)."),
    "business_summary": (business_summary,
                         "Sale, gross profit, cash aur udhaar kisi bhi period "
                         "ka. args: period (day/yesterday/week/month) ya days, "
                         "ya start aur end date."),
    "top_items": (top_items,
                  "Sabse zyada ya sabse kam bikne waale item. args: days, "
                  "limit, order (top ya slow)."),
    "list_customers": (list_customers,
                       "Saare customer aur unka outstanding udhaar."),
    "customer_account": (customer_account,
                         "Ek customer ka hisaab: kitna udhaar baaki, kab tak. "
                         "args: name."),
    "dues": (dues, "Jo udhaar due ho rahe hain. args: days_before."),
    "recent_activity": (recent_activity,
                        "Pichhli entries: kya bika, kya aaya. args: limit."),
    "price_quote": (price_quote,
                    "Kisi saamaan ka bhaav aur GST ke saath total, bina kuch "
                    "record kiye. args: items[{item, qty, unit}]."),

    "record_sale": (record_sale,
                    "Sale record karo. args: items[{item, qty, unit, rate}], "
                    "payment (cash ya credit), customer (naam, credit ke liye "
                    "zaroori), customer_phone, payment_deadline."),
    "record_purchase": (record_purchase,
                        "Supplier se aaya stock record karo. args: items[{item, "
                        "qty, unit, rate}] jahan rate cost price hai."),
    "stock_take": (stock_take,
                   "Ginti ke baad stock theek karo. args: items[{item, qty, unit}]."),
    "record_payment": (record_payment,
                       "Customer se udhaar ka paisa mila. args: customer (naam), "
                       "amount."),
    "update_shop_profile": (update_shop_profile,
                            "Dukaan ki details badlo: shop_name, owner, gstin, "
                            "address, shop_type (jaise 'hardware' ya 'building "
                            "material'). Yehi bill ke letterhead par chhapta hai."),
    "add_item": (add_item,
                 "Nayi item list mein daalo. args: name, cost_price (zaroori), "
                 "selling_rate, unit, brand."),

    "send_bill": (send_bill,
                  "Customer ko WhatsApp par bill PDF bhejo. args: customer "
                  "(naam), items[{item, qty, rate}], payment."),
    "send_summary": (send_summary,
                     "Owner ko day ya week ki summary PDF WhatsApp par bhejo. "
                     "args: period (day ya week). Bhejne se pehle poochho."),
    "send_reminders": (send_reminders,
                       "Jinka udhaar due hai unhe reminder bhejo. args: days_before."),
}

# Older Samvaad console entries pointed at these names; keep them working, as
# module attributes too, since callers reach for agent.day_summary directly.
_ALIASES = {"day_summary": "business_summary", "record_order": "record_sale"}
day_summary = business_summary
record_order = record_sale


def manifest() -> list:
    """Everything the console needs in order to add these as HTTP tools."""
    return [{"name": name, "description": desc}
            for name, (_, desc) in sorted(TOOLS.items())]


def handle(tool: str, caller: str, args: dict, key: str = "") -> dict:
    user, repo = shop_for_caller(caller, key)
    if not user:
        # Never fall back to "some" shop: a wrong guess reads another
        # business's books aloud to a stranger.
        return {"speak": "Yeh number humare system mein registered nahi hai.",
                "authorised": False}
    # Bind the shop so main._write_events — and anything else that reaches for
    # the request-scoped `repo` proxy — sees this user instead of raising 401.
    import main
    main.bind_user(user)
    repo = main.repo

    name = _ALIASES.get(tool, tool)
    entry = TOOLS.get(name)
    if not entry:
        # Deliberately no tool listing here. The agent already has its own
        # catalogue, so echoing ours back adds nothing but a string of internal
        # names that a curious caller could talk it into reading out.
        return {"speak": "Yeh kaam abhi nahi kar sakta.", "error": "unknown_tool",
                "authorised": True}
    try:
        out = entry[0](repo, user, args or {})
    except Exception as e:
        # A tool crash must not become a confident wrong answer on the call.
        # The exception type is enough to debug from the logs; the message can
        # carry SQL, paths or column names, and anything returned here is text
        # the agent may decide to speak.
        print(f"[agent] {name} failed for {user['user_id']}: "
              f"{type(e).__name__}: {e}", flush=True)
        return {"speak": "Isme kuch gadbad ho gayi, dobara boliye.",
                "error": type(e).__name__, "authorised": True}
    return {**out, "tool": name, "authorised": True,
            "shop": user.get("shop_name") or ""}
