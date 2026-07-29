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
import json
import re
import os
import secrets
from collections import defaultdict
from datetime import datetime, timedelta

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


def _selling_rate(sku: dict):
    """The shop's own asking price.

    There is no selling_rate column on skus: the schema stores cost
    (opening_cost_per_kg / landed_cost_per_kg) and nothing else about price.
    Rather than migrate, keep it in the attributes jsonb, which upsert_sku
    already persists. Read both so a SKU seeded either way still answers.
    """
    rate = sku.get("selling_rate")
    if rate is None:
        rate = (sku.get("attributes") or {}).get("selling_rate")
    return float(rate) if rate not in (None, "") else None


def _number(value):
    """A quantity or amount as the caller said it.

    Speech gives "ek sau bees" or "एक सौ" far more often than "120". The old
    controller parsed those with nlp.ONES; the agent called float() straight
    on the string and raised, which the caller heard as "kuch gadbad ho gayi".
    Returns None when there is genuinely no number, so the tool can ask.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    import nlp
    total = running = 0.0
    seen = False
    for tok in re.split(r"[\s-]+", text):
        n = nlp.ONES.get(tok)
        if n is None and tok.isdigit():
            n = float(tok)
        if n is None:
            continue
        seen = True
        # "ek sau bees" is 1 x 100 + 20, not 1 + 100 + 20.
        if n >= 100:
            running = (running or 1) * n
            total += running
            running = 0.0
        else:
            running += n
    total += running
    return total if seen and total else None


def _bounded_int(value, default: int, low: int = 1, high: int = 365) -> int:
    """Parse spoken/text counts without allowing pathological query ranges."""
    number = _number(value)
    if number is None:
        return default
    return max(low, min(int(number), high))


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


def _skeleton(word: str) -> str:
    """Consonants only, for acronyms spelled out letter by letter.

    "टीएमटी" is how TMT is said, and it transliterates to "tiemti", which
    matches nothing. Dropping vowels turns both that and "tmt" into "tmt".
    Only ever compared for exact equality, so it cannot loosely match.
    """
    return re.sub(r"[aeiou]", "", (word or "").lower())


def _sounds_like(phrase: str, catalogue: list) -> list:
    """Product names whose WORDS sound close to what was said.

    The matcher scores a phrase against a product's whole text, so a
    one-letter slip on a single word ("siment" for cement, from सीमेंट)
    scores near zero against "UltraTech PPC Cement 50kg". Comparing word to
    word instead catches it. Only ever used to raise a question, never to
    pick, so a loose threshold here cannot ship the wrong bag.
    """
    import difflib
    import translit
    text = (phrase or "").lower()
    if translit.has_devanagari(text):
        text = translit.to_latin(text) or text
    said = [w for w in re.split(r"[\s-]+", text) if len(w) > 3]
    if not said:
        return []
    hits = []
    for sku in catalogue:
        words = set(re.split(r"[\s-]+", (sku.get("canonical") or "").lower()))
        for alias in sku.get("aliases") or []:
            words.update(re.split(r"[\s-]+", str(alias).lower()))
        best = 0.0
        for w in words:
            for q in said:
                if len(q) >= 4 and len(w) >= 3 and _skeleton(q) == _skeleton(w):
                    best = 1.0
                    break
                if len(w) <= 3:
                    continue
                best = max(best, difflib.SequenceMatcher(None, q, w).ratio())
        if best >= 0.66:
            hits.append((best, sku["canonical"]))
    hits.sort(reverse=True)
    return [name for _, name in hits]


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
    if m.get("status") not in ("matched", "disambiguate"):
        # Seeded catalogues carry Devanagari aliases; anything the agent added
        # by voice carries only Latin. "सीमेंट" then matched nothing and got
        # called off-trade. Try the romanised form before giving up.
        import translit
        if translit.has_devanagari(phrase):
            latin = translit.to_latin(phrase)
            if latin and latin != phrase:
                m = M.match(latin, catalogue, repo.load_learning(), "live_sale")
    if m.get("status") == "matched":
        return repo.sku(m.get("sku_id")), None
    if m.get("status") not in ("matched", "disambiguate"):
        near = _sounds_like(phrase, catalogue)
        if near:
            return None, {"said": phrase, "options": near[:3]}
    if m.get("status") == "uncertain":
        # A near miss, not an absent product: "siment" (from सीमेंट) scores
        # close to Cement without clearing the confidence bar. Telling the
        # owner we do not stock it would be a confident wrong answer, so offer
        # the closest names and let them pick.
        ids = [c.get("sku_id") if isinstance(c, dict) else c
               for c in (m.get("candidates") or [])]
        near = [repo.sku(i) for i in ids if i]
        if not near:
            near = [repo.sku(c.get("sku_id") if isinstance(c, dict) else c)
                    for c in M.fuzzy_candidates(phrase, catalogue, limit=3)]
        names = [n["canonical"] for n in near if n]
        if names:
            return None, {"said": phrase, "options": names[:3]}
    if m.get("status") == "disambiguate":
        # The matcher returns options as attribute VALUES ("OPC 53", "PPC") for
        # a variant question and as sku_ids elsewhere. Normalise both.
        by_id = {sku["sku_id"]: sku["canonical"] for sku in catalogue}

        def _name(value):
            """Never let a sku_id reach the caller's ear.

            "Kajaria mein se kaunsa, TILE_KAJARIA_CERAMIC_2X2 ya
            TILE_KAJARIA_VITRIFIED_600" is what the shop actually heard. The
            matcher hands back ids in some shapes and attribute values in
            others, so resolve anything that looks like an id.
            """
            text = str(value or "").strip()
            return by_id.get(text, text)

        names = []
        for o in m.get("options") or []:
            if isinstance(o, dict):
                names.append(_name(o.get("canonical") or o.get("label")
                                   or o.get("value")))
            elif isinstance(o, str):
                names.append(_name(o))
        names = [n for n in names if n and n not in by_id]
        if not names:
            fam = m.get("family") or m.get("candidates_family")
            names = [s["canonical"] for s in catalogue if s.get("family") == fam]
        if names:
            return None, {"said": phrase, "options": names[:4]}
    return None, None


def _item_ref(args: dict) -> str:
    """Prefer a tool-chained SKU id, otherwise use the caller's item words."""
    return str(args.get("sku_id") or args.get("item") or "").strip()


def learning_context(repo, limit: int = 100) -> dict:
    """Compact, grounded product memory for the voice agent.

    Tools always resolve aliases deterministically. Returning the same mapping
    from ``shop_profile`` also prevents a premature LLM clarification before a
    tool is called.
    """
    learning = repo.load_learning()
    catalogue = {sku["sku_id"]: sku for sku in repo.load_catalogue()}
    aliases = []
    for row in learning.get("aliases_learned", []):
        sku = catalogue.get(row.get("sku_id"))
        phrase = str(row.get("phrase") or "").strip()
        if not sku or not phrase:
            continue
        aliases.append({
            "phrase": phrase,
            "sku_id": sku["sku_id"],
            "product": sku.get("canonical"),
        })
        if len(aliases) >= limit:
            break
    priors = [
        {
            "family": row.get("family"),
            "attribute": row.get("attribute"),
            "value": row.get("value"),
            "evidence_count": row.get("count", 0),
        }
        for row in learning.get("attribute_priors", [])
        if row.get("family") and row.get("attribute")
    ][:50]
    return {
        "state": repo.learning_state(),
        "product_aliases": aliases,
        "attribute_priors": priors,
    }


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


def _no_item_named() -> dict:
    return {"found": False, "needs": {"field": "item"},
            "speak": "Kaunsa saamaan? Naam bataiye."}


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


def _scrub(value):
    """Drop any argument the console left as an unfilled {{template}}.

    Tool bodies are templates: {"item": "{{item}}"}. When the agent has nothing
    to put in a slot, the literal placeholder arrives instead. Treating that as
    a real value means searching the catalogue for "{{item}}" and confidently
    reporting it out of stock, so strip it and let the tool ask.
    """
    if isinstance(value, str):
        return None if ("{{" in value and "}}" in value) else value
    if isinstance(value, dict):
        return {k: v for k, v in ((k, _scrub(v)) for k, v in value.items())
                if v is not None}
    if isinstance(value, list):
        return [v for v in (_scrub(v) for v in value) if v not in (None, {}, [])]
    return value


def _when(args: dict) -> str:
    """The day an entry belongs to, not the day it was spoken.

    "Kal becha tha" is ordinary: shopkeepers catch up on the ledger after the
    shop is shut. Recording it as today quietly moves money between days and
    breaks every summary that follows.
    """
    raw = str(args.get("occurred_on") or args.get("when") or "").strip().lower()
    today = clock.today()
    if not raw:
        return today.isoformat()
    if raw in ("aaj", "today"):
        return today.isoformat()
    if raw in ("kal", "yesterday", "kal ka", "beeta kal"):
        return (today - timedelta(days=1)).isoformat()
    if raw in ("parso", "day before yesterday"):
        return (today - timedelta(days=2)).isoformat()
    try:
        return L._d(raw).isoformat()
    except Exception:
        return today.isoformat()


def _lines(args: dict) -> list:
    """Item lines, however the console managed to express them.

    A nested array of objects is painful to template in the Body tab, so a
    single-line call may arrive flattened as item/qty/unit/rate. Most phone
    orders are one or two lines, and refusing the easy shape would push people
    into hand-editing JSON.
    """
    rows = args.get("items")
    if isinstance(rows, dict):
        rows = [rows]
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except (ValueError, TypeError):
            rows = [{"item": rows}]
    if not rows and (args.get("item") or args.get("sku_id")):
        rows = [{"item": args.get("item"), "sku_id": args.get("sku_id"),
                 "qty": args.get("qty"),
                 "unit": args.get("unit"), "rate": args.get("rate")}]
    return [r for r in (rows or [])
            if isinstance(r, dict) and (r.get("item") or r.get("sku_id"))]


def _ask_which(question: dict) -> dict:
    return {"needs": question,
            "speak": f"{question['said']} mein se kaunsa, "
                     f"{' ya '.join(question['options'][:3])}?"}


def _customer_by_name(repo, name: str):
    """Resolve an exact id/phone, exact name, or unique name substring."""
    want = str(name or "").strip().casefold()
    if not want:
        return None, []
    accounts = crm.accounts(repo)
    exact_id = [
        a for a in accounts
        if str(a.get("customer_id") or "").casefold() == want
    ]
    if len(exact_id) == 1:
        return exact_id[0], []
    digits = "".join(c for c in want if c.isdigit())
    if len(digits) >= 7:
        exact_phone = [
            a for a in accounts
            if "".join(c for c in str(a.get("phone") or "") if c.isdigit())
            .endswith(digits[-10:])
        ]
        if len(exact_phone) == 1:
            return exact_phone[0], []
    exact = [a for a in accounts
             if str(a.get("name") or "").casefold() == want]
    if len(exact) == 1:
        return exact[0], []
    part = [a for a in accounts if want in (a.get("name") or "").lower()]
    if len(part) == 1:
        return part[0], []
    return None, [{"name": a.get("name"), "phone": a.get("phone"),
                   "outstanding": a["outstanding"]} for a in (exact or part)[:5]]


def _customer_ref(args: dict) -> str:
    return str(args.get("customer_id") or args.get("customer_phone")
               or args.get("customer") or args.get("name") or "").strip()


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
        return end - timedelta(
            days=_bounded_int(args["days"], 1, high=3650) - 1), end
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
    memory = learning_context(repo)
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
        "learning_state": repo.learning_state(),
        "learning_counts": repo.learning_counts(),
        "learned_product_names": memory["product_aliases"],
        "learned_product_defaults": memory["attribute_priors"],
        "product_resolution_policy": (
            "Pass the caller's exact local item phrase to the relevant tool "
            "before asking size, brand, or type. Ask only when that tool "
            "returns needs. If learned_product_names contains the phrase, use "
            "its sku_id and product directly."
        ),
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
                      "selling_rate": _selling_rate(sku),
                      "cost_price": sku.get("opening_cost_per_kg")})
    items.sort(key=lambda i: i["name"])
    names = ", ".join(i["name"] for i in items[:6])
    return {"count": len(items), "items": items,
            "speak": f"{len(items)} item hain: {names}"
                     + (" aur bhi." if len(items) > 6 else ".")}


def check_stock(repo, user, args):
    ref = _item_ref(args)
    if not ref:
        return _no_item_named()
    sku, question = _find_sku(repo, ref)
    if question:
        return _ask_which(question)
    if not sku:
        return _not_stocked(repo, user, ref)
    st = _stock_of(repo, sku)
    return {"found": True, "sku_id": sku["sku_id"], "name": sku["canonical"],
            **st, "selling_rate": _selling_rate(sku),
            "speak": f"{sku['canonical']} ka stock {st['text']} hai."}


def item_details(repo, user, args):
    ref = _item_ref(args)
    if not ref:
        return _no_item_named()
    sku, question = _find_sku(repo, ref)
    if question:
        return _ask_which(question)
    if not sku:
        return _not_stocked(repo, user, ref)
    events = repo.events_for_sku(sku["sku_id"])
    cost = L.landed_cost_as_of(sku, events, clock.today())
    sales = sorted((e for e in events if e["type"] == "sale"),
                   key=lambda e: e.get("occurred_on", ""))
    st = _stock_of(repo, sku)
    return {"found": True, "sku_id": sku["sku_id"], "name": sku["canonical"],
            "brand": sku.get("brand"), "family": sku.get("family"),
            "unit": sku.get("default_unit"), "attributes": sku.get("attributes"),
            "selling_rate": _selling_rate(sku), "landed_cost": cost,
            "gst_rate": repo.gst_rate_for(sku), "stock": st,
            "last_sold_on": sales[-1]["occurred_on"] if sales else None,
            "speak": f"{sku['canonical']}: stock {st['text']}, cost "
                     f"{_say_number(cost or 0)} rupaye."}


def search_items(repo, user, args):
    q = (args.get("query") or args.get("item") or "").strip().lower()
    if not q:
        return _no_item_named()
    catalogue = repo.load_catalogue()

    def _words(text: str) -> list:
        return [w for w in re.split(r"[\s,./-]+", text) if w]

    import translit
    forms = [q]
    if translit.has_devanagari(q):
        # "टाटा टिस्को टीएमटी" found nothing at all: every stage here works on
        # Latin text, so a caller who spells the brand in Devanagari got
        # "kuch nahi mila" about a product sitting on the shelf.
        latin = (translit.to_latin(q) or "").strip().lower()
        if latin and latin != q:
            forms.append(latin)

    hits, seen = [], set()

    def _add(sku):
        if sku and sku["sku_id"] not in seen:
            seen.add(sku["sku_id"])
            hits.append(sku)

    # Searching must use the same Day-60 memory as write tools. Otherwise the
    # agent sees several TMT variants from search and asks "which size?" even
    # though record_sale itself already knows the local phrase.
    learned_match = M.match(q, catalogue, repo.load_learning(), "live_sale")
    if learned_match.get("status") == "matched":
        _add(repo.sku(learned_match.get("sku_id")))

    for form in forms:
        for sku in catalogue:
            blob = " ".join([sku.get("sku_id") or "",
                             sku.get("canonical") or "", sku.get("brand") or "",
                             sku.get("family") or "",
                             " ".join(str(a) for a in (sku.get("aliases") or []))
                             ]).lower()
            # Every word the caller said, in any order: "16 mm tata" should
            # find "Tata Tiscon TMT Bar 16mm" even though neither is a
            # substring of the other.
            if form in blob or all(w in blob.replace(" ", "") or w in blob
                                   for w in _words(form)):
                _add(sku)
    if not hits:
        for form in forms:
            ids = [c.get("sku_id") if isinstance(c, dict) else c
                   for c in M.fuzzy_candidates(form, catalogue, limit=5)]
            for i in ids:
                _add(repo.sku(i))
    if not hits:
        # Last resort, word by word: catches "Tisco" for "Tiscon".
        names = _sounds_like(q, catalogue)
        for name in names:
            _add(next((s for s in catalogue if s["canonical"] == name), None))
    events = repo.all_events()
    rows = [{"sku_id": s["sku_id"], "name": s["canonical"],
             "stock": _stock_of(repo, s, events)["text"],
             "selling_rate": _selling_rate(s)} for s in hits]
    return {"count": len(rows), "items": rows,
            "speak": ", ".join(r["name"] for r in rows[:5]) or "Kuch nahi mila."}


def low_stock(repo, user, args):
    """Uses the same velocity model as the dashboard, so the voice answer and
    the screen never disagree about what is running out."""
    import main
    rows = main._low_stock_items(
        repo, limit=_bounded_int(args.get("limit"), 8, high=100))
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
    frozen, frozen_total = [], 0.0
    if (end - start).days >= 6:
        # Only worth saying on a week or longer. "Nothing moved today" is
        # noise; "sixty days and nothing moved" is a decision.
        events = repo.all_events()
        for sku in repo.load_catalogue():
            det = L._stock_detail(sku, events, end)
            if det["qty"] == L.UNCOUNTED or not (det.get("base") or 0) > 0:
                continue
            moved = any(e["sku_id"] == sku["sku_id"]
                        and e["type"] in ("sale", "delivery")
                        and 0 <= (end - L._d(e["occurred_on"])).days <= 60
                        for e in events)
            if not moved:
                value = (L.landed_cost_as_of(sku, events, end) or 0) * det["base"]
                frozen.append({"sku_id": sku["sku_id"],
                               "name": sku["canonical"],
                               "value": round(value, 2)})
                frozen_total += value
        frozen.sort(key=lambda f: -f["value"])
    alerts = main._low_stock_items(repo) if single_day else []
    if alerts:
        speak += " Stock alert: " + ", ".join(
            f"{r['canonical']} khatam ho gaya" if r.get("out_of_stock")
            else f"{r['canonical']} sirf {r['stock']} bacha hai"
            for r in alerts[:3]) + "."
    if frozen:
        speak += (f" {_say_number(frozen_total)} rupaye ka maal do mahine se "
                  f"nahi bika, jaise {frozen[0]['name']}.")
    return {"start": start.isoformat(), "end": end.isoformat(), **t,
            "low_stock": alerts, "frozen_capital": frozen[:8],
            "frozen_total": round(frozen_total, 2), "speak": speak}


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
    rows = []
    for k, v in qty.items():
        cost = L.landed_cost_as_of(by[k], repo.events_for_sku(k), end) or 0
        rows.append({"sku_id": k, "name": by[k]["canonical"],
                     "qty_sold": round(v, 2), "unit": L.base_unit(by[k]),
                     "revenue": round(revenue[k], 2),
                     "margin": round(revenue[k] - cost * v, 2)})
    order = (args.get("order") or "top").lower()
    slow = order == "slow"
    # "sabse zyada kamai kis item se" is a margin question, not a revenue one.
    rows.sort(key=lambda r: r["margin" if order == "margin" else "revenue"],
              reverse=not slow)
    rows = rows[:_bounded_int(args.get("limit"), 5, high=100)]
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


def add_customer(repo, user, args):
    """Create a customer without inventing or silently overwriting identity."""
    name = str(args.get("name") or args.get("customer") or "").strip()
    phone = str(args.get("customer_phone") or args.get("phone") or "").strip()
    normalised_phone = auth.normalize_phone(phone)
    if len(name) < 2:
        return {"added": False, "needs": {"field": "name"},
                "speak": "Customer ka poora naam batayein."}
    if not re.fullmatch(r"\+91\d{10}", normalised_phone):
        return {"added": False, "needs": {"field": "customer_phone"},
                "speak": "Customer ka das digit mobile number batayein."}
    existing = next(
        (row for row in repo.customers()
         if auth.normalize_phone(row.get("phone")) == normalised_phone),
        None,
    )
    if existing:
        return {
            "added": False,
            "existing": True,
            "customer_id": existing["customer_id"],
            "name": existing.get("name"),
            "phone": existing.get("phone"),
            "speak": f"{existing.get('name') or name} pehle se customer list mein hai.",
        }
    customer = repo.upsert_customer(normalised_phone, name)
    return {
        "added": True,
        "customer_id": customer["customer_id"],
        "name": customer.get("name"),
        "phone": customer.get("phone"),
        "speak": f"{customer.get('name') or name} ko customer list mein add kar diya.",
    }


def customer_account(repo, user, args):
    ref = _customer_ref(args)
    acc, options = _customer_by_name(repo, ref)
    if not acc:
        if options:
            return _ask_which_customer(options)
        return {"found": False,
                "speak": f"{ref} naam ka koi customer nahi mila."}
    return {"found": True, "customer_id": acc["customer_id"], "name": acc.get("name"),
            "phone": acc.get("phone"), "outstanding": acc["outstanding"],
            "total_credit": acc["total_credit"], "total_paid": acc["total_paid"],
            "next_deadline": acc.get("next_deadline"),
            "open_dues": [{"amount": d["amount"], "remaining": d["remaining"],
                           "deadline": d["deadline"]} for d in acc["open_dues"]],
            "recent_payments": [{"amount": p["amount"], "paid_on": p.get("paid_on")}
                                for p in (acc.get("payments") or [])[:5]],
            "overdue": any(L._d(d["deadline"]) < clock.today()
                           for d in acc["open_dues"]),
            "speak": (f"{acc.get('name')} ka {_say_number(acc['outstanding'])} "
                      "rupaye baaki hai." if acc["outstanding"] > 0
                      else f"{acc.get('name')} ka koi udhaar baaki nahi.")}


def dues(repo, user, args):
    rows = crm.due_receivables(
        repo, clock.today(),
        days_before=_bounded_int(args.get("days_before"), 7, low=0, high=365))
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
    n = _bounded_int(args.get("limit"), 8, high=100)
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


def stock_value(repo, user, args):
    """What the shelves are worth, at cost and at selling price.

    Owners ask this as "maal kitne ka pada hai". It needs both numbers: cost is
    the money tied up, selling price is what it should come back as.
    """
    events = repo.all_events()
    at_cost = at_sale = 0.0
    rows, uncounted = [], []
    for sku in repo.load_catalogue():
        st = _stock_of(repo, sku, events)
        if not st["counted"] or not st["qty"] or st["qty"] < 0:
            if not st["counted"]:
                uncounted.append(sku["canonical"])
            continue
        base = L.to_base(st["qty"], st["unit"], sku)
        cost = L.landed_cost_as_of(sku, repo.events_for_sku(sku["sku_id"]),
                                   clock.today()) or 0
        rate = _selling_rate(sku)
        rate = L.rate_to_base(float(rate), sku.get("default_unit"), sku) \
            if rate else cost * 1.10
        at_cost += base * cost
        at_sale += base * rate
        rows.append({"name": sku["canonical"], "qty": st["qty"],
                     "unit": st["unit"], "value_at_cost": round(base * cost, 2)})
    rows.sort(key=lambda r: r["value_at_cost"], reverse=True)
    return {"at_cost": round(at_cost, 2), "at_selling_price": round(at_sale, 2),
            "potential_margin": round(at_sale - at_cost, 2),
            "items": rows, "uncounted": uncounted,
            "speak": f"Stock ki value {_say_number(at_cost)} rupaye cost par, "
                     f"aur bikne par {_say_number(at_sale)} rupaye."}


def price_quote(repo, user, args):
    """What a basket costs, GST included — before anything is recorded."""
    lines, unknown = [], []
    subtotal = gst_total = 0.0
    for row in _lines(args):
        ref = _item_ref(row)
        sku, question = _find_sku(repo, ref)
        if question:
            return _ask_which(question)
        if not sku:
            unknown.append(ref)
            continue
        qty = _number(row.get("qty")) or 0
        unit = row.get("unit") or sku.get("default_unit")
        rate = _number(row.get("rate"))
        if rate is None:
            rate = _selling_rate(sku)
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
def _already_written(repo, request_id: str, retry_window_seconds: int = 8) -> list:
    """Very recent events from a retry carrying this exact request_id.

    Samvaad is instructed to create a fresh id per confirmed action, but an
    agent version can accidentally reuse an old id. Treating that id as unique
    forever silently drops real sales. Retry protection therefore only covers
    the short interval in which an HTTP/tool retry can occur; an old reused id
    is a new business entry.
    """
    if not request_id:
        return []
    out = []
    for event in repo.all_events():
        if (event.get("evidence") or {}).get("request_id") != request_id:
            continue
        try:
            recorded = datetime.fromisoformat(str(event.get("recorded_at") or ""))
        except ValueError:
            continue
        now = datetime.now(recorded.tzinfo) if recorded.tzinfo else datetime.now()
        if 0 <= (now - recorded).total_seconds() <= retry_window_seconds:
            out.append(event)
    return out


def _same_retry(prior: list, etype: str, items: list, args: dict) -> bool:
    """A reused id is a retry only when the recently written payload matches."""
    if len(prior) != len(items) or not prior:
        return False
    remaining = list(prior)
    for item in items:
        match = next((
            event for event in remaining
            if event.get("type") == etype
            and event.get("sku_id") == item.get("sku_id")
            and abs(float(event.get("qty") or 0) - float(item.get("qty") or 0)) < 1e-9
            and str(event.get("unit") or "") == str(item.get("unit") or "")
            and (etype != "sale"
                 or (event.get("payment") or "cash") == (item.get("payment") or "cash"))
            and (etype != "sale"
                 or (event.get("customer_id") or "") == (item.get("customer_id") or ""))
        ), None)
        if not match:
            return False
        requested_rate = item.get("rate")
        if requested_rate is not None:
            written_rate = match.get("quoted_rate")
            if written_rate is None:
                written_rate = match.get("rate")
            if written_rate is None or abs(float(written_rate) - float(requested_rate)) > 1e-9:
                return False
        remaining.remove(match)
    return all(str(event.get("occurred_on") or "")[:10] == _when(args)
               for event in prior)


def _commit(repo, user, etype: str, args: dict, source: str = "voice_agent") -> dict:
    """Resolve spoken items to SKUs and append events. Asks before guessing."""
    import main
    request_id = str(args.get("request_id") or "").strip()[:64]
    items, unknown = [], []
    for row in _lines(args):
        ref = _item_ref(row)
        sku, question = _find_sku(repo, ref)
        if question:
            return {"recorded": False, **_ask_which(question)}
        if not sku:
            unknown.append(ref)
            continue
        if _number(row.get("qty")) is None:
            return {"recorded": False,
                    "needs": {"said": ref, "field": "qty"},
                    "speak": f"{sku['canonical']} kitna?"}
        unit = row.get("unit") or sku.get("default_unit")
        items.append({"sku_id": sku["sku_id"], "qty": _number(row["qty"]),
                      "unit": unit, "rate": _number(row.get("rate")),
                      "rate_unit": unit if _number(row.get("rate")) is not None
                                   else None,
                      "payment": args.get("payment"),
                      "customer_id": (args.get("customer_id")
                                      if etype == "sale" else None),
                      "payment_deadline": args.get("payment_deadline"),
                      "spoken": row.get("item") or ref})
    if unknown and not args.get("add_unknown"):
        # Recording the rest and quietly dropping this one leaves the owner
        # with a sale that does not match what went out of the door.
        first = unknown[0]
        miss = _not_stocked(repo, user, first)
        if miss["stocks_this_kind"]:
            ask = (f"{first} inventory mein nahi hai. Naya item add kar doon?")
        else:
            # Far more often a misheard word than a real new product.
            ask = (f"{first} na inventory mein hai na humare line mein. Ye "
                   "sach mein naya item hai ya galti se bol diya?")
        return {"recorded": False, "unavailable": unknown,
                "needs": {"field": "confirm_add", "said": first,
                          "stocks_this_kind": miss["stocks_this_kind"],
                          "shop_kind": miss["shop_kind"]},
                "speak": ask}
    if not items:
        return {"recorded": False, "unavailable": unknown,
                "speak": "Saamaan samajh nahi aaya. Naam aur quantity dobara bataiye."}
    prior = _already_written(repo, request_id)
    if _same_retry(prior, etype, items, args):
        return {"recorded": True, "duplicate": True, "unavailable": [],
                "_items": [{"sku_id": e["sku_id"], "qty": e.get("qty"),
                            "unit": e.get("unit")} for e in prior],
                "_result": {"committed": [{"event_id": e.get("event_id"),
                                           "sku_id": e["sku_id"], "amount": 0}
                                          for e in prior],
                            "affected_stock": {}}}
    result = main._write_events(etype, items, _when(args),
                                args.get("precision", "exact"), source,
                                request_id=request_id)
    # The tap flow learns from every confirmation (main.commit does this); the
    # agent did not, so a shop that says "mota sariya" on ten calls taught the
    # matcher nothing. Learn the same way, from what the caller actually said.
    import learning as LEARN
    for it in items:
        if it.get("spoken") and it.get("sku_id"):
            try:
                LEARN.record_confirmation(repo, it["spoken"], it["sku_id"],
                                          unit=it.get("unit"))
            except Exception:
                # Learning is an optimisation. Losing it must never lose a sale.
                pass
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
                "speak": "Retry mila tha; entry safe hai aur dobara nahi likhi."}
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
    if out.get("duplicate"):
        return {**out, "lines": res["committed"],
                "stock_after": res["affected_stock"],
                "speak": "Retry mila tha; stock entry safe hai aur dobara nahi likhi."}
    return {**out, "lines": res["committed"],
            "stock_after": res["affected_stock"],
            "speak": f"{_said(repo, items)} stock mein add kar diya."}


def stock_take(repo, user, args):
    """A physical count. Overwrites the derived figure for that item.

    Also reports how far the books were out. The count is the easy half; the
    gap between counted and recorded is the half that tells the owner whether
    something is walking out of the shop.
    """
    before = {}
    for row in _lines(args):
        sku, _ = _find_sku(repo, _item_ref(row))
        if sku:
            before[sku["sku_id"]] = _stock_of(repo, sku)
    out = _commit(repo, user, "stock_take", args)
    if not out.get("recorded"):
        return out
    res, items = out.pop("_result"), out.pop("_items")
    deltas = []
    for it in items:
        was = before.get(it["sku_id"], {})
        if was.get("counted") and was.get("qty") is not None:
            gap = round(float(it["qty"]) - float(was["qty"]), 3)
            if gap:
                sku = repo.sku(it["sku_id"]) or {}
                deltas.append({"sku_id": it["sku_id"],
                               "name": sku.get("canonical"),
                               "recorded": was["qty"], "counted": it["qty"],
                               "difference": gap, "unit": it["unit"]})
    speak = f"Ginti update kar di: {_said(repo, items)}."
    if deltas:
        d = deltas[0]
        direction = "kam" if d["difference"] < 0 else "zyada"
        speak += (f" Hisaab se {_g(abs(d['difference']))} {d['unit']} "
                  f"{direction} nikla.")
    return {**out, "stock_after": res["affected_stock"],
            "differences": deltas, "speak": speak}


def record_payment(repo, user, args):
    """Cash received against an outstanding udhaar."""
    acc, options = _customer_by_name(repo, _customer_ref(args))
    if not acc:
        if options:
            return {"recorded": False, **_ask_which_customer(options)}
        return {"recorded": False, "speak": "Ye customer nahi mila."}
    try:
        amount = _number(args.get("amount")) or 0
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
    cost = _number(args["cost_price"])
    rate = _number(args.get("selling_rate"))
    if cost is None:
        return {"added": False, "needs": {"field": "cost_price"},
                "speak": f"{name} ka cost price kya hai?"}
    attrs = dict(args.get("attributes") or {})
    if args.get("brand"):
        attrs["brand"] = args["brand"]
    if rate is not None:
        # No selling_rate column exists, so it rides in attributes rather than
        # being silently dropped, which is what happened before: every item the
        # agent added came back with no cost and no price at all.
        attrs["selling_rate"] = rate
    sku_id = "sku_" + hashlib.sha1(name.lower().encode()).hexdigest()[:8]
    repo.upsert_sku({
        "sku_id": sku_id, "canonical": name,
        "family": args.get("family") or name,
        "default_unit": unit, "units": {unit: 1},
        "opening_cost_per_kg": cost,     # the ledger's cost-per-base-unit field
        "gst_rate": args.get("gst_rate"),
        "attributes": attrs,
        "aliases": [name.lower()] + ([args["brand"].lower()]
                                     if args.get("brand") else []),
    })
    # A new SKU has no stock until something counts it, and an uncounted item
    # answers "abhi tak gina nahi gaya" to every later question. Asking now is
    # the difference between a usable item and a dead row.
    return {"added": True, "sku_id": sku_id, "name": name, "unit": unit,
            "cost_price": cost, "selling_rate": rate,
            "next_step": {"tool": "stock_take", "why": "opening count",
                          "item": name, "unit": unit},
            "speak": f"{name} list mein add kar diya. Abhi kitna stock hai, "
                     f"{unit} mein bata dijiye?"}


def update_item(repo, user, args):
    """Fix an existing product: its name, cost, price or unit.

    Needed because a SKU with stock against it cannot simply be deleted and
    re-added, and because onboarding can leave a product with no name at all,
    which the agent then reads out as silence.
    """
    requested_id = str(args.get("sku_id") or "").strip()
    sku = None
    if requested_id:
        # sku_id is an identifier, not a fuzzy product description. Resolve it
        # first and case-insensitively; when both item and sku_id arrive, the
        # explicit id must win.
        sku = repo.sku(requested_id)
        if not sku:
            sku = next(
                (row for row in repo.load_catalogue()
                 if str(row.get("sku_id") or "").casefold()
                 == requested_id.casefold()),
                None)
    lookup = args.get("item") or (requested_id if not sku else "")
    question = None
    if not sku:
        sku, question = _find_sku(repo, lookup)
    print(
        "[agent] update_item lookup "
        f"item={str(args.get('item') or '')[:100]!r} "
        f"sku_id={requested_id[:100]!r} "
        f"matched={(sku or {}).get('sku_id')!r} "
        f"ambiguous={bool(question)}",
        flush=True,
    )
    if question and not sku:
        return {"updated": False, **_ask_which(question)}
    if not sku:
        return {"updated": False,
                "speak": f"{args.get('item')} list mein mila hi nahi."}
    patch = dict(sku)
    changed = []
    if args.get("name"):
        patch["canonical"] = str(args["name"]).strip()
        changed.append("naam")
    if args.get("unit"):
        patch["default_unit"] = args["unit"]
        patch["units"] = {**(sku.get("units") or {}), args["unit"]: 1}
        changed.append("unit")
    if args.get("cost_price") not in (None, ""):
        patch["opening_cost_per_kg"] = _number(args["cost_price"])
        changed.append("cost")
    if args.get("selling_rate") not in (None, ""):
        patch["attributes"] = {**(sku.get("attributes") or {}),
                               "selling_rate": _number(args["selling_rate"])}
        changed.append("rate")
    if args.get("brand") not in (None, ""):
        patch["attributes"] = {**(patch.get("attributes") or {}),
                               "brand": str(args["brand"]).strip()}
        changed.append("brand")
    if args.get("type") not in (None, ""):
        patch["attributes"] = {**(patch.get("attributes") or {}),
                               "type": str(args["type"]).strip()}
        changed.append("type")
    if args.get("family") not in (None, ""):
        patch["family"] = str(args["family"]).strip().lower()
        changed.append("family")
    if args.get("gst_rate") not in (None, ""):
        patch["gst_rate"] = _number(args["gst_rate"])
        changed.append("GST")
    if not changed:
        return {"updated": False, "needs": {"field": "what"},
                "speak": "Kya badalna hai: naam, unit, cost ya rate?"}
    repo.upsert_sku(patch)
    print(
        f"[agent] update_item saved sku_id={sku['sku_id']!r} "
        f"changed={changed!r}",
        flush=True,
    )
    return {"updated": True, "sku_id": sku["sku_id"],
            "name": patch.get("canonical"), "changed": changed,
            "speak": f"{patch.get('canonical')} ka {', '.join(changed)} "
                     "update kar diya."}


def remove_item(repo, user, args):
    """Delete a product that should never have been added.

    Refused once anything has been bought, sold or counted against it: stock
    is replayed from the event log, so removing a referenced SKU would rewrite
    history rather than tidy it.
    """
    ref = _item_ref(args) or str(args.get("name") or "").strip()
    sku, question = _find_sku(repo, ref)
    if question:
        return {"removed": False, **_ask_which(question)}
    if not sku:
        return {"removed": False,
                "speak": f"{ref} list mein mila hi nahi."}
    if repo.events_for_sku(sku["sku_id"]):
        return {"removed": False, "sku_id": sku["sku_id"],
                "speak": f"{sku['canonical']} ka hisaab pehle se chal raha hai, "
                         "isliye hata nahi sakta."}
    repo.delete_sku(sku["sku_id"])
    return {"removed": True, "sku_id": sku["sku_id"], "name": sku["canonical"],
            "speak": f"{sku['canonical']} list se hata diya."}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send_bill(repo, user, args):
    import notify
    acc, options = _customer_by_name(repo, _customer_ref(args))
    if not acc:
        if options:
            return {"sent": False, **_ask_which_customer(options)}
        return {"sent": False, "speak": "Ye customer nahi mila."}
    customer = repo.customer(acc["customer_id"]) or acc
    rows = _lines(args)
    if not rows:
        return {"sent": False, "needs": {"field": "items"},
                "speak": "Bill mein kya-kya daalna hai?"}
    lines = []
    for row in rows:
        ref = _item_ref(row)
        sku, question = _find_sku(repo, ref)
        if question:
            return {"sent": False, **_ask_which(question)}
        if not sku:
            return {"sent": False,
                    "speak": f"{ref} nahi mila, bill nahi bhej saka."}
        lines.append({"sku_id": sku["sku_id"], "qty": row.get("qty"),
                      "unit": row.get("unit") or sku.get("default_unit"),
                      "rate": row.get("rate") or _selling_rate(sku) or 0})
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


def show_bill(repo, user, args):
    """Put a non-sending bill preview in the owner's active browser."""
    import presentations
    acc, options = _customer_by_name(repo, _customer_ref(args))
    if not acc:
        if options:
            return {"shown": False, **_ask_which_customer(options)}
        return {"shown": False, "speak": "Ye customer nahi mila."}
    customer = repo.customer(acc["customer_id"]) or acc
    rows = _lines(args)
    if not rows:
        return {"shown": False, "needs": {"field": "items"},
                "speak": "Bill mein kya-kya daalna hai?"}
    lines, subtotal, gst_total = [], 0.0, 0.0
    for row in rows:
        ref = _item_ref(row)
        sku, question = _find_sku(repo, ref)
        if question:
            return {"shown": False, **_ask_which(question)}
        if not sku:
            return {"shown": False,
                    "speak": f"{ref} nahi mila, bill nahi dikha saka."}
        qty = _number(row.get("qty"))
        rate = _number(row.get("rate"))
        unit = row.get("unit") or sku.get("default_unit")
        if qty is None:
            return {"shown": False, "needs": {"field": "qty", "item": ref},
                    "speak": f"{sku['canonical']} kitna hai?"}
        if rate is None:
            rate = _selling_rate(sku) or 0
        amount = L.line_amount(qty, unit, rate,
                               row.get("rate_unit") or unit, sku)
        gst = amount * float(repo.gst_rate_for(sku)) / 100.0
        subtotal += amount
        gst_total += gst
        lines.append({"sku_id": sku["sku_id"], "name": sku["canonical"],
                      "qty": qty, "unit": unit, "rate": rate,
                      "amount": round(amount, 2)})
    total = subtotal + gst_total
    cfg = repo.load_config()
    payload = {
        "shop": cfg.get("shop_name") or user.get("shop_name") or "My Shop",
        "customer": {"customer_id": customer.get("customer_id"),
                     "name": customer.get("name") or "",
                     "phone": customer.get("phone") or ""},
        "items": lines,
        "subtotal": round(subtotal, 2),
        "gst": round(gst_total, 2),
        "total": round(total, 2),
        "payment": args.get("payment") or "cash",
        "payment_deadline": args.get("payment_deadline") or "",
        "date": clock.today().isoformat(),
    }
    shown = presentations.store(user["user_id"], "bill", payload)
    return {"shown": True, "presentation_id": shown["presentation_id"],
            "customer": customer.get("name"), "total": round(total, 2),
            "line_count": len(lines),
            "speak": "Bill screen par dikha diya hai. WhatsApp par bheju?"}


def show_summary(repo, user, args):
    """Put a non-sending business summary in the owner's active browser."""
    import presentations
    summary = business_summary(repo, user, args)
    shown = presentations.store(user["user_id"], "summary", summary)
    return {**summary, "shown": True,
            "presentation_id": shown["presentation_id"],
            "speak": "Summary screen par dikha di hai."}


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
        out = notify.send_due_reminders(
            repo, user,
            days_before=_bounded_int(args.get("days_before"), 2,
                                     low=0, high=365))
    except Exception as e:
        return {"sent": False, "error": str(e)[:200],
                "speak": "Reminder nahi bhej saka."}
    # send_due_reminders returns its own "sent" key holding the LIST of
    # deliveries. Splatting it over ours turned the boolean every other tool
    # returns into an array, so name the two things differently.
    delivered = out.get("sent") or []
    n = delivered if isinstance(delivered, int) else len(delivered)
    return {"sent": True, "count": n, "delivered": delivered,
            "skipped": out.get("skipped") or [], "as_of": out.get("as_of"),
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
                     "customer hain, kul udhaar, Day 1/Day 60 state aur grounded "
                     "local-name-to-SKU mappings. Call ke shuru mein zaroor chalao."),
    "list_inventory": (list_inventory,
                       "Poori inventory: har item ka naam, stock, unit, rate. "
                       "'kya kya hai', 'stock list' jaise sawaal ke liye."),
    "check_stock": (check_stock,
                    "Ek item ka current stock. args: item (jo caller ne bola, "
                    "Hindi, English ya mix) ya exact sku_id."),
    "item_details": (item_details,
                     "Ek item ki poori detail: cost price, selling rate, GST, "
                     "stock, aakhri sale kab hui. args: item ya exact sku_id."),
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
                  "Sabse zyada ya sabse kam bikne waale item, aur har ek ka "
                  "revenue aur margin. args: days, limit, order (top, slow "
                  "ya margin)."),
    "list_customers": (list_customers,
                       "Saare customer aur unka outstanding udhaar."),
    "add_customer": (add_customer,
                     "Naya customer list mein add karo. Confirm karne ke baad "
                     "args: name English Latin script mein aur "
                     "customer_phone das digit mobile number."),
    "customer_account": (customer_account,
                         "Ek customer ka hisaab: kitna udhaar baaki, kab tak. "
                         "args: name English Latin script mein, ya exact "
                         "customer_id/customer_phone. Devanagari naam ko tool "
                         "call se pehle transliterate karo."),
    "dues": (dues, "Jo udhaar due ho rahe hain. args: days_before."),
    "recent_activity": (recent_activity,
                        "Pichhli entries: kya bika, kya aaya. args: limit."),
    "stock_value": (stock_value,
                    "Poore stock ki value: cost par kitna paisa phansa hai aur "
                    "bikne par kitna aayega. 'maal kitne ka pada hai' ke liye."),
    "price_quote": (price_quote,
                    "Kisi saamaan ka bhaav aur GST ke saath total, bina kuch "
                    "record kiye. args: item ya sku_id, qty, unit."),

    "record_sale": (record_sale,
                    "Sale record karo. Ek item ek call mein. Caller ka local "
                    "naam pehle isi tool ko exact item phrase mein bhejo; "
                    "size/brand khud tab tak mat poochho jab tak tool needs na de. "
                    "args: item ya "
                    "sku_id, qty, "
                    "unit, rate, payment (cash ya credit), customer (naam, "
                    "English Latin script mein; credit ke liye zaroori), "
                    "customer_phone, "
                    "payment_deadline, occurred_on, request_id."),
    "record_purchase": (record_purchase,
                        "Supplier se aaya stock record karo. Ek item ek call mein. "
                        "Caller ka local naam exact item phrase mein bhejo; "
                        "tool needs de tabhi size/brand poochho. "
                        "args: item ya sku_id, qty, unit, rate (cost price), "
                        "occurred_on, request_id."),
    "stock_take": (stock_take,
                   "Ginti ke baad stock theek karo. Ek item ek call mein. "
                   "Caller ka local naam exact item phrase mein bhejo; "
                   "tool needs de tabhi size/brand poochho. "
                   "args: item ya sku_id, qty, unit, occurred_on, request_id."),
    "record_payment": (record_payment,
                       "Customer se udhaar ka paisa mila. args: customer (naam "
                       "English Latin script mein) ya customer_id/"
                       "customer_phone, amount, request_id."),
    "update_shop_profile": (update_shop_profile,
                            "Dukaan ki details badlo: shop_name, owner, gstin, "
                            "address, shop_type (jaise 'hardware' ya 'building "
                            "material'). Yehi bill ke letterhead par chhapta hai."),
    "add_item": (add_item,
                 "Nayi item list mein daalo. args: name, cost_price (zaroori), "
                 "selling_rate, unit, family, brand, type, gst_rate. Add hone ke baad stock_take se "
                 "opening ginti likhwana zaroori hai."),

    "update_item": (update_item,
                    "Mojooda item theek karo: naam, family, brand, type, unit, "
                    "cost_price, selling_rate ya gst_rate. args: item (ya "
                    "exact sku_id) aur jo badalna hai."),
    "remove_item": (remove_item,
                    "Galti se added item ko list se hatao. args: item ya sku_id. Jispe "
                    "koi sale ya stock chal raha ho, wo nahi hatega."),
    "show_bill": (show_bill,
                  "Bill app ki screen par preview karo, WhatsApp par mat bhejo. "
                  "Send karne se hamesha pehle ye tool chalao. args: customer "
                  "(naam English Latin script mein) ya customer_id/"
                  "customer_phone, item ya sku_id, qty, unit, rate, payment, "
                  "payment_deadline, items."),
    "send_bill": (send_bill,
                  "Customer ko WhatsApp par bill PDF bhejo. args: customer "
                  "(naam English Latin script mein) ya customer_id/"
                  "customer_phone, item ya sku_id, qty, unit, rate, payment, "
                  "payment_deadline, items."),
    "show_summary": (show_summary,
                     "Business summary app ki screen par dikhao, WhatsApp par "
                     "mat bhejo. args: period, days, start, end."),
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
        # Spell the failure out in every field an agent might look at. A
        # refusal that only says authorised=false got narrated back to the
        # owner as "likh diya hai" for a write that never happened, which is
        # the worst failure this system has: a confident lie about the ledger.
        refusal = {"ok": False, "authorised": False, "error": "not_authorised",
                   "recorded": False, "added": False, "updated": False,
                   "sent": False, "found": False,
                   "speak": "Yeh number humare system mein registered nahi "
                            "hai, isliye koi entry nahi hui."}
        return {**refusal, "facts": _facts(refusal)}
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
        out = entry[0](repo, user, _scrub(args or {}))
    except Exception as e:
        # A tool crash must not become a confident wrong answer on the call.
        # The exception type is enough to debug from the logs; the message can
        # carry SQL, paths or column names, and anything returned here is text
        # the agent may decide to speak.
        print(f"[agent] {name} failed for {user['user_id']}: "
              f"{type(e).__name__}: {e}", flush=True)
        return {"speak": "Isme kuch gadbad ho gayi, dobara boliye.",
                "error": type(e).__name__, "authorised": True}
    out = {"ok": True, **out, "tool": name, "authorised": True,
           "shop": user.get("shop_name") or ""}
    return {**out, "facts": _facts(out)}


# The console's "What the agent gets back" step templates named fields out of
# the reply. Anything it does not name is at best uncertain to arrive, and a
# per-tool sentence written there would put the canned answers straight back.
# So every reply also carries `facts`: the whole payload as one compact string.
# One placeholder, {{facts}}, works for all 23 tools and loses nothing.
FACTS_LIMIT = 3000


def _facts(payload: dict) -> str:
    body = {k: v for k, v in payload.items()
            if k not in ("facts", "authorised", "shop")}
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= FACTS_LIMIT:
        return text
    # Long lists are the only thing that gets near the limit. Trim the rows
    # rather than truncating the JSON into something unparseable.
    for key in ("items", "customers", "events", "dues", "lines"):
        rows = body.get(key)
        if isinstance(rows, list) and len(rows) > 12:
            body[key] = rows[:12]
            body[f"{key}_truncated_from"] = len(rows)
            text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            if len(text) <= FACTS_LIMIT:
                break
    return text[:FACTS_LIMIT]
