"""
conversation.py — voice dialogue for "Hey Chhotu".

Design: the LLM does ALL the understanding, every turn; a thin voice controller
only decides which follow-up to ask so nothing commits half-filled.

  • Each turn the Sarvam chat model re-reads the WHOLE conversation so far and
    returns a single structured understanding of it (intent + items, each with an
    exact sku_id when it can be pinned down, qty, rate, payment). Follow-up
    answers are understood by the model too — there is no deterministic parsing of
    what the owner says.
  • A small controller then looks at that understanding and asks — by voice — for
    the ONE thing still missing (which product / kitna / rate / cash-or-udhaar),
    or commits when everything is there. This guarantees Chhotu always asks the
    price and never records an incomplete entry.

Keeping the LLM job to "extract", not "run the whole dialogue policy", keeps its
reasoning short — important because sarvam-30b is a reasoning model capped at
4096 tokens on this tier, and a heavier task overruns that budget.

Exact figures (stock on hand, margin, udhaar) are computed by the backend and
spoken by Chhotu — never invented by the model.

State is a plain dict passed between client and server each turn:
  {"flow": ..., "said": ["<owner utterance 1>", "<owner utterance 2>", ...]}
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta

import matcher as M
import nlp
import sarvam_client

TODAY = date(2026, 7, 26)
_FAMILIES = ("tmt", "cement", "pipe", "fitting", "fastener", "paint")

# 'low' = fast (~8s/turn), great when Saaras returns numbers as digits (usual).
# 'medium' = slower (~30s/turn) but firmer on spelled-out Hindi number WORDS.
_EFFORT = os.environ.get("CHHOTU_REASONING", "low")


def parse_phone(text: str):
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def parse_deadline(text: str):
    """Accept YYYY-MM-DD, DD/MM[/YYYY], or a relative number of days."""
    t = (text or "").lower().strip()
    iso = re.search(r"\b(20\d{2}-\d{1,2}-\d{1,2})\b", t)
    if iso:
        try:
            return date.fromisoformat(iso.group(1)).isoformat()
        except ValueError:
            return None
    dm = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\b", t)
    if dm:
        try:
            return date(int(dm.group(3) or TODAY.year), int(dm.group(2)),
                        int(dm.group(1))).isoformat()
        except ValueError:
            return None
    if re.search(r"agle hafte|next week|ek hafta|अगले हफ्ते|एक हफ्ता", t):
        return (TODAY + timedelta(days=7)).isoformat()
    if re.search(r"kal|tomorrow|कल", t):
        return (TODAY + timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s*(din|day|दिन)", t)
    if m:
        return (TODAY + timedelta(days=int(m.group(1)))).isoformat()
    return None


# ---------------------------------------------------------------------------
# Extractor prompt (deliberately compact -> short reasoning -> fits 4096)
# ---------------------------------------------------------------------------
def _catalogue_lines(repo) -> str:
    out = []
    for s in repo.load_catalogue():
        out.append(f'    {s["sku_id"]} = "{s["canonical"]}" (family {s["family"]}, '
                   f'default unit {s.get("default_unit")})')
    return "\n".join(out)


def _extract_prompt(repo) -> str:
    return (
        "You read an Indian hardware-shop owner speaking (Hindi/English/Devanagari "
        "mix) and EXTRACT what he means as JSON. Output ONLY the JSON object.\n\n"
        f"Today: {TODAY.isoformat()}.\n"
        "The shop stocks ONLY these products:\n" + _catalogue_lines(repo) + "\n\n"
        "Hindi numbers: ek=1 do=2 teen=3 char=4 paanch=5 chhe=6 saat=7 aath=8 "
        "nau=9 das=10 barah=12 solah=16 bees=20 pachees=25 tees=30 chalees=40 "
        "pachaas=50 pachpan=55 saath=60 chausath=64 assi=80 sau=100 hazaar=1000 "
        "lakh=100000; dhai=2.5 saade=+0.5 sava=+0.25 paune=-0.25. Digits stay as-is.\n\n"
        "Return JSON:\n"
        '{"intent":"sale|delivery|count|stock_query|analytics_query|unknown",'
        '"metric":"margin|cash|udhaar|frozen|inventory|null",'
        '"items":[{"sku_id":"<exact sku_id, or null if not pinned to ONE>",'
        '"family":"tmt|cement|null","name":"<what he called it>",'
        '"in_catalogue":true|false,"qty":<number or null>,"unit":"<unit or empty>",'
        '"rate":<number or null>,"payment":"cash|credit|null"}]}\n\n'
        "RULES:\n"
        "- Combine EVERYTHING said across the whole text (later lines answer earlier "
        "questions). Keep the owner's self-corrections ('do nahi teen' -> 3).\n"
        "- sku_id: set it ONLY when the words pin down exactly ONE product. Just "
        "'sariya'/'saria/सरिया' -> family tmt, sku_id null (size unknown). Just "
        "'cement'/'सीमेंट' -> family cement, sku_id null (type unknown). 'barah mm "
        "sariya'/'tata 12' -> TMT_12_FE500D_TATA. 'solah mm' -> TMT_16_FE500D_TATA. "
        "'ultratech opc'/'53 wala' -> CEM_ULTRATECH_OPC53. 'ppc' -> CEM_ULTRATECH_PPC.\n"
        "- Anything the shop does NOT stock (tiles, wire, sand, bricks, pipe, paint, "
        "fittings): in_catalogue=false, sku_id null, family null. NEVER substitute.\n"
        "- qty = number of units (ton/bori/piece). A size like '12mm'/'barah mm' is "
        "NOT a quantity.\n"
        "- rate: only if a price was actually spoken (rupaye/rs/@). payment: only if "
        "spoken (cash/nagad->cash; udhaar/credit/baaki->credit). Else null.\n"
        "- stock question ('kitna bacha/stock hai') -> intent stock_query. "
        "Margin/cash/udhaar/frozen/inventory question -> analytics_query + metric.\n"
        "- intent=delivery when the owner RECEIVED/BOUGHT stock: 'kharida/khareeda', "
        "'hamne liya', 'mangwaya', 'aaya', 'delivery aayi'. intent=sale when the owner "
        "SOLD/GAVE stock to a customer: 'becha/bech diya', 'diya', 'bika'. Do not "
        "confuse the two — 'kharida' is ALWAYS delivery, never sale.\n"
        "- intent MUST be exactly one of: sale, delivery, count, stock_query, "
        "analytics_query, unknown. Never invent another word.\n"
        "- Think briefly, then output the JSON."
    )


_VALID_INTENTS = {"sale", "delivery", "count", "stock_query", "analytics_query", "unknown"}

# cash/udhaar is a fixed two-word vocabulary, unlike product/qty/price — the
# LLM has been observed to miss an explicit "udhaar mein becha" and ask "cash
# ya udhaar?" again. Detect it deterministically from the transcript instead
# of trusting the extractor's json field, same reasoning as _quick_route().
_CREDIT_RE = re.compile(r"udhaar|udhar|credit|baaki|khaate|उधार|बाकी|खाते")
_CASH_RE = re.compile(r"\bcash\b|nagad|कैश|नकद")


def _detect_payment(text: str):
    t = (text or "").lower()
    if _CREDIT_RE.search(t):
        return "credit"
    if _CASH_RE.search(t):
        return "cash"
    return None


def _extract(state, repo) -> dict:
    joined = " . ".join(s for s in state["said"] if s)
    history = state.get("history") or [{"role": "user", "content": joined}]
    messages = [{"role": "system", "content": _extract_prompt(repo)}, *history]
    out = {}
    try:
        out = sarvam_client.chat_json(messages, temperature=0.1,
                                      reasoning_effort=_EFFORT, max_tokens=1200,
                                      timeout=18)
    except Exception:
        out = {}
    if not isinstance(out, dict) or "items" not in out:
        return _fallback_extract(joined, repo)
    if not isinstance(out, dict):
        out = {}
    payment = _detect_payment(joined)
    items = []
    for r in out.get("items", []) or []:
        if not isinstance(r, dict):
            continue
        fam = r.get("family") if r.get("family") in _FAMILIES else None
        unit = (r.get("unit") or "").strip().lower() or None
        if unit:
            unit = M.UNIT_WORDS.get(unit, unit)
        name = r.get("name") or ""
        name_l = name.lower()
        stocked_family = ("cement" if re.search(r"cement|सीमेंट", name_l)
                          else "tmt" if re.search(r"sari?ya|sariya|सरिया|tmt", name_l)
                          else None)
        # A generic stocked family is ambiguous, not "out of catalogue".
        # This prevents "50 bori cement" being rejected before we can ask OPC/PPC.
        if stocked_family:
            fam = fam or stocked_family
        items.append({
            "sku_id": r.get("sku_id") if _valid_sku(r.get("sku_id"), repo) else None,
            "family": fam, "name": name,
            "in_catalogue": True if stocked_family else r.get("in_catalogue", True),
            "qty": r.get("qty") if isinstance(r.get("qty"), (int, float)) else None,
            "unit": unit,
            "rate": r.get("rate") if isinstance(r.get("rate"), (int, float)) else None,
            "payment": payment or (r.get("payment")
                                   if r.get("payment") in ("cash", "credit") else None),
        })
    raw_intent = out.get("intent")
    intent = raw_intent if raw_intent in _VALID_INTENTS else "unknown"
    return {"intent": intent, "metric": out.get("metric"), "items": items}


def _fallback_extract(text: str, repo) -> dict:
    """Fast local fallback when the model times out or returns malformed JSON."""
    t = (text or "").lower()
    if re.search(r"kharida|khareeda|mangwaya|delivery|aaya|liya|खरीदा|मंगवाया|आया|लिया", t):
        intent = "delivery"
    elif re.search(r"becha|bech|bika|diya|sale|बेचा|बिका|दिया", t):
        intent = "sale"
    elif re.search(r"gina|count|opening|गिना", t):
        intent = "count"
    else:
        intent = "unknown"
    payment = _detect_payment(t)
    items = []
    for row in nlp.parse_sale_utterance(t):
        phrase = row.get("product_phrase") or ""
        match = M.match(phrase, repo.load_catalogue(), repo.load_learning(),
                        "live_sale" if intent == "sale" else intent)
        sid = match.get("sku_id") if match.get("status") == "matched" else None
        sku = repo.sku(sid) if sid else None
        family = (sku or {}).get("family")
        if not family:
            family = "cement" if re.search(r"cement|सीमेंट", phrase) else \
                ("tmt" if re.search(r"sari?ya|sariya|सरिया|tmt", phrase) else None)
        items.append({
            "sku_id": sid, "family": family, "name": phrase,
            "in_catalogue": bool(family or sid), "qty": row.get("qty"),
            "unit": row.get("unit"), "rate": None, "payment": payment,
        })
    return {"intent": intent, "metric": None, "items": items}


def _valid_sku(sid, repo) -> bool:
    return bool(sid) and repo.sku(sid) is not None


# ---------------------------------------------------------------------------
# Main entry — controller
# ---------------------------------------------------------------------------
# Analytics/stock questions use a small, fixed vocabulary ("kitna udhaar",
# "nahi bika", "stock hai"...) regardless of how they're phrased around it —
# there's no real ambiguity to resolve, so route them by keyword match
# instead of paying for (and risking) an LLM classification. This is what
# actually answers "kaun sa saamaan 60 din mein nahi bika" reliably: a plain
# sale/delivery utterance never matches these patterns, so it can't misfire
# on an order. The LLM is reserved for what genuinely needs it — pinning
# down which product/qty/price was said.
_QUICK_PATTERNS = [
    ("frozen", re.compile(r"nahi\s*bika|bika\s*nahi|frozen|phasa|dead\s*stock|"
                          r"purana\s*maal|move\w*\s*nahi|kaun\s*sa\s*saamaan")),
    ("udhaar", re.compile(r"kitna\s*udhaar|udhaar\s*kitna|total\s*udhaar|udhaar\s*baaki|"
                          r"baaki\s*kitna")),
    ("cash", re.compile(r"aaj\s*(ka\s*)?cash|cash\s*kitna|kitna\s*cash")),
    ("inventory", re.compile(r"inventory\s*(ki\s*)?value|maal\s*ki\s*value|"
                             r"stock\s*ki\s*value")),
    ("margin", re.compile(r"margin|munafa|profit")),
]
_STOCK_QUERY_RE = re.compile(r"kitna\s*bacha|stock\s*hai|kitna\s*stock|kitna\s*hai\b")


def _quick_route(text: str):
    """Deterministic fast-path for analytics/stock questions. Returns
    ("analytics", metric), ("stock", None), or None (fall through to the LLM)."""
    t = (text or "").lower()
    for metric, pat in _QUICK_PATTERNS:
        if pat.search(t):
            return ("analytics", metric)
    if _STOCK_QUERY_RE.search(t):
        return ("stock", None)
    return None


def converse(state, user_text, flow, repo):
    if not sarvam_client.has_key():
        return _reply("Voice ke liye Sarvam API key chahiye.", listen=False, done=True)

    if not state or "said" not in state:
        state = {"flow": flow, "said": [], "history": []}

    state.setdefault("history", [])

    # Follow-up slots are applied to the structured draft already extracted on
    # the first turn. This preserves the complete order and avoids another
    # 30-second model call for simple answers such as "12 mm", "50", or "cash".
    aw = state.get("awaiting")
    if aw in ("customer_phone", "customer_name", "deadline"):
        state["history"].append({"role": "user", "content": user_text or ""})
        return _apply_customer_slot(state, aw, user_text, repo)
    if aw == "confirmation":
        state["history"].append({"role": "user", "content": user_text or ""})
        if re.search(r"confirm|haan|ha|yes|theek|ठीक|हाँ", (user_text or "").lower()):
            state["awaiting"] = None
            pc = state.pop("pending_commit")
            return _commit(state, pc["flow"], pc["items"], pc["skipped"], repo)
        return _prepare_confirmation(state, repo)
    if state.get("awaiting_order"):
        state["history"].append({"role": "user", "content": user_text or ""})
        return _apply_order_slot(state, user_text, repo)

    if (not flow or flow == "auto") and not state.get("locked_intent"):
        quick = _quick_route(user_text)
        if quick == ("stock", None):
            state["said"].append(user_text or "")
            state["history"].append({"role": "user", "content": user_text or ""})
            return _stock_flow(state, _extract(state, repo)["items"], repo)
        if quick and quick[0] == "analytics":
            return _analytics_answer(quick[1], repo)

    state["said"].append(user_text or "")
    state["history"].append({"role": "user", "content": user_text or ""})

    ext = _extract(state, repo)
    intent = ext["intent"]
    if flow and flow != "auto":
        intent = {"live_sale": "sale", "delivery": "delivery",
                  "opening_balance": "count", "count": "count"}.get(flow, intent)
    elif intent in ("sale", "delivery", "count"):
        # Lock the transaction type (bought vs sold) the first time it's
        # resolved, so a noisy re-classification on a later follow-up turn
        # (e.g. after the owner just says "cash") can't silently flip a
        # purchase into a sale mid-conversation.
        intent = state.setdefault("locked_intent", intent)
    elif state.get("locked_intent"):
        intent = state["locked_intent"]

    if intent == "analytics_query":
        return _analytics_answer(ext.get("metric"), repo)
    if intent == "stock_query":
        return _stock_flow(state, ext["items"], repo)
    if intent == "unknown" and not ext["items"]:
        return _say(state, "Ye samajh nahi aaya — sirf sariya aur cement rakhte hain. "
                    "Kya chahiye?", listen=True, done=False)
    state["draft_items"] = ext["items"]
    return _order_flow(state, intent, ext["items"], repo)


# ---------------------------------------------------------------------------
# Sale / delivery / count
# ---------------------------------------------------------------------------
def _ask_product(item):
    fam = item.get("family")
    if fam == "tmt":
        return "Kaunsa sariya — barah mm ya solah mm?"
    if fam == "cement":
        return "Kaunsa cement — OPC 53 ya PPC?"
    return "Kaunsa maal — sariya ya cement? Thoda detail se boliye."


def _ask_order_slot(state, item_index, slot, text):
    state["awaiting_order"] = {"item_index": item_index, "slot": slot}
    return _say(state, text, listen=True, done=False)


def _answer_number(text):
    value, _ = nlp._find_qty((text or "").lower())
    if value is not None:
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    if m:
        return float(m.group(0))
    hindi = {
        "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "छह": 6,
        "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "बारह": 12,
        "सोलह": 16, "बीस": 20, "पच्चीस": 25, "तीस": 30,
        "चालीस": 40, "पचास": 50, "साठ": 60, "सौ": 100,
    }
    for word, number in hindi.items():
        if word in (text or ""):
            return float(number)
    return None


def _sku_from_answer(text, family, repo):
    t = (text or "").lower()
    direct = None
    if family == "tmt" or re.search(r"sari?ya|sariya|tmt|सरिया", t):
        if re.search(r"\b12\b|barah|बारह", t):
            direct = "TMT_12_FE500D_TATA"
        elif re.search(r"\b16\b|solah|सोलह", t):
            direct = "TMT_16_FE500D_TATA"
        elif re.search(r"\b20\b|bees|बीस", t):
            direct = "TMT_20_FE500D_TATA"
    if family == "cement" or re.search(r"cement|सीमेंट", t):
        if re.search(r"opc|53|ओपीसी", t):
            direct = "CEM_ULTRATECH_OPC53"
        elif re.search(r"\bppc\b|पीपीसी", t):
            direct = "CEM_ULTRATECH_PPC"
        elif re.search(r"\bpsc\b|पीएससी", t):
            direct = "CEM_ULTRATECH_PSC"
    if direct and repo.sku(direct):
        return direct
    match = M.match(text or "", repo.load_catalogue(), repo.load_learning(), "live_sale")
    return match.get("sku_id") if match.get("status") == "matched" else None


def _apply_order_slot(state, user_text, repo):
    waiting = state.pop("awaiting_order")
    items = state.get("draft_items") or []
    idx = int(waiting.get("item_index", 0))
    slot = waiting.get("slot")
    if idx >= len(items):
        return _say(state, "Order ka context kho gaya — poori entry ek baar phir boliye.",
                    listen=True, done=False)
    item = items[idx]
    if slot == "product":
        sid = _sku_from_answer(user_text, item.get("family"), repo)
        if not sid:
            return _ask_order_slot(state, idx, slot, _ask_product(item))
        item["sku_id"] = sid
        item["family"] = repo.sku(sid).get("family")
    elif slot == "qty":
        qty = _answer_number(user_text)
        if qty is None or qty <= 0:
            return _ask_order_slot(state, idx, slot, "Quantity samajh nahi aayi — kitna?")
        item["qty"] = qty
        unit = nlp._find_unit((user_text or "").lower())
        if unit:
            item["unit"] = unit
    elif slot == "rate":
        rate = _answer_number(user_text)
        if rate is None or rate <= 0:
            return _ask_order_slot(state, idx, slot, "Rate number mein bataiye.")
        item["rate"] = rate
    elif slot == "payment":
        payment = _detect_payment(user_text)
        if not payment:
            return _ask_order_slot(state, idx, slot, "Payment cash hai ya udhaar?")
        for row in items:
            if not row.get("payment"):
                row["payment"] = payment
    elif slot == "intent":
        t = (user_text or "").lower()
        if re.search(r"kharida|khareeda|liya|aaya|delivery|खरीदा|लिया|आया", t):
            state["locked_intent"] = "delivery"
        elif re.search(r"becha|bech|bika|diya|sale|बेचा|बिका|दिया", t):
            state["locked_intent"] = "sale"
        else:
            return _ask_order_slot(state, idx, slot, "Ye maal becha tha ya khareeda?")
    state["draft_items"] = items
    return _order_flow(state, state.get("locked_intent") or state.get("flow"),
                       items, repo)


def _order_flow(state, intent, items, repo):
    if intent not in ("sale", "delivery", "count"):
        # Never guess bought-vs-sold when the extractor couldn't classify it —
        # writing the wrong direction silently corrupts stock.
        state["draft_items"] = items
        return _ask_order_slot(state, 0, "intent",
                               "Ye maal aapne becha ya khareeda?")
    flow = intent  # sale | delivery | count
    state["locked_intent"] = flow
    # drop products the shop does not stock, but tell the owner once
    kept = []
    skipped = []
    for it in items:
        if it.get("in_catalogue") is False and not it.get("sku_id"):
            skipped.append(it.get("name") or "wo cheez")
        else:
            kept.append(it)
    if not kept:
        nm = skipped[0] if skipped else "wo cheez"
        return _say(state, f"{nm} hum nahi rakhte — sirf sariya aur cement hai. "
                    "Kuch aur?", listen=True, done=False)

    state["draft_items"] = kept
    # Resolve product identity and quantity for every line first.
    for idx, it in enumerate(kept):
        if not it.get("sku_id"):
            return _ask_order_slot(state, idx, "product", _ask_product(it))
        sku = repo.sku(it["sku_id"])
        if it.get("qty") in (None, ""):
            return _ask_order_slot(state, idx, "qty",
                                   f"{sku['canonical']} — kitna?")
        if not it.get("unit") or it["unit"] not in sku.get("units", {}):
            it["unit"] = sku.get("default_unit")

    # Then collect each line's rate. Payment belongs to the whole sale and is
    # deliberately asked only once after all line items are complete.
    for idx, it in enumerate(kept):
        sku = repo.sku(it["sku_id"])
        if flow in ("sale", "delivery") and it.get("rate") is None:
            per = it["unit"]
            q = (f"{sku['canonical']} kitne mein aaya, per {per}?" if flow == "delivery"
                 else f"{sku['canonical']} ka rate kya laga, per {per}?")
            return _ask_order_slot(state, idx, "rate", q)

    if flow == "sale" and any(not it.get("payment") for it in kept):
        first_missing = next(i for i, it in enumerate(kept) if not it.get("payment"))
        return _ask_order_slot(state, first_missing, "payment",
                               "Is poori sale ka payment cash hai ya udhaar?")

    if flow == "sale":
        state["pending_commit"] = {"flow": flow, "items": kept, "skipped": skipped}
        return _resume_customer_capture(state, repo)

    state["pending_commit"] = {"flow": flow, "items": kept, "skipped": skipped}
    return _prepare_confirmation(state, repo)


# ---------------------------------------------------------------------------
# Udhaar customer capture (deterministic data entry, gates the commit)
# ---------------------------------------------------------------------------
def _apply_customer_slot(state, aw, user_text, repo):
    if aw == "customer_phone":
        phone = parse_phone(user_text)
        if not phone:
            return _say(state, "Customer ka 10 digit contact number bataiye.",
                        listen=True, done=False)
        known = repo.customer_by_phone(phone)
        state["customer"] = {"phone": repo.normalize_phone(phone),
                             "customer_id": known.get("customer_id") if known else None,
                             "name": known.get("name") if known else None}
        state["awaiting"] = None
        return _resume_customer_capture(state, repo)

    if aw == "customer_name":
        name = (user_text or "").strip()
        if not name:
            return _say(state, "Naam bataiye.", listen=True, done=False)
        customer = repo.upsert_customer(state["customer"]["phone"], name)
        state["customer"] = {"phone": customer["phone"],
                             "customer_id": customer["customer_id"],
                             "name": customer["name"]}
        state["awaiting"] = None
        return _resume_customer_capture(state, repo)

    if aw == "deadline":
        deadline = parse_deadline(user_text)
        if not deadline:
            return _say(state, "Date samajh nahi aayi — jaise 'kal', 'agle hafte', "
                        "ya '5 din baad' bataiye.", listen=True, done=False)
        state["payment_deadline"] = deadline
        state["awaiting"] = None
        return _resume_customer_capture(state, repo)


def _resume_customer_capture(state, repo):
    customer = state.get("customer") or {}
    if not customer.get("phone"):
        state["awaiting"] = "customer_phone"
        return _say(state, "Customer ka 10 digit contact number bataiye.",
                    listen=True, done=False)
    if not customer.get("customer_id"):
        state["awaiting"] = "customer_name"
        return _say(state, "Naya customer hai — naam kya hai?", listen=True, done=False)
    pending = state.get("pending_commit") or {}
    needs_deadline = any(it.get("payment") == "credit"
                         for it in pending.get("items", []))
    if needs_deadline and not state.get("payment_deadline"):
        state["awaiting"] = "deadline"
        return _say(state, f"{customer['name']} — payment kab tak? Date ya kitne din "
                    "baad bataiye.", listen=True, done=False)
    return _prepare_confirmation(state, repo)


def _prepare_confirmation(state, repo):
    """Return a complete, read-only transaction preview. Nothing is written
    until the explicit Confirm Entry button sends the confirmation turn."""
    import ledger
    pending = state.get("pending_commit") or {}
    flow = pending.get("flow")
    items = pending.get("items") or []
    customer = state.get("customer") or {}
    preview_items = []
    total = 0.0
    for item in items:
        sku = repo.sku(item["sku_id"])
        qty = float(item["qty"])
        rate = float(item.get("rate") or 0)
        base_qty = ledger.to_base(qty, item.get("unit") or sku["default_unit"], sku)
        amount = round(rate * base_qty, 2) if flow in ("sale", "delivery") else None
        if amount is not None:
            total += amount
        preview_items.append({
            "sku_id": sku["sku_id"], "canonical": sku["canonical"],
            "qty": qty, "unit": item.get("unit") or sku["default_unit"],
            "rate": item.get("rate"), "payment": item.get("payment"),
            "amount": amount,
        })
    state["awaiting"] = "confirmation"
    state["draft_items"] = items
    preview = {
        "flow": flow, "items": preview_items, "total": round(total, 2),
        "customer": {
            "customer_id": customer.get("customer_id"),
            "name": customer.get("name"), "phone": customer.get("phone"),
        } if customer else None,
        "payment_deadline": state.get("payment_deadline"),
    }
    out = _say(state, "Saari details taiyaar hain. Check karke Confirm Entry dabaiye.",
               listen=False, done=False)
    out["confirmation_required"] = True
    out["confirmation"] = preview
    return out


def _commit(state, flow, items, skipped, repo):
    import main
    etype = {"sale": "sale", "delivery": "delivery",
             "count": "opening_balance"}.get(flow, flow)
    customer = state.get("customer") or {}
    deadline = state.get("payment_deadline")
    ev_items, rows, parts = [], [], []
    for it in items:
        sku = repo.sku(it["sku_id"])
        payment = it.get("payment") or "cash"
        ev_items.append({"sku_id": sku["sku_id"], "qty": float(it["qty"]),
                         "unit": it["unit"], "rate": it.get("rate"),
                         "payment": payment, "spoken": it.get("name", ""),
                         "was_tap": False,
                         "customer_id": customer.get("customer_id"),
                         "payment_deadline": deadline if payment == "credit" else None})
        rows.append({"phrase": it.get("name") or sku["canonical"],
                     "sku_id": sku["sku_id"], "qty": float(it["qty"]),
                     "unit": it["unit"], "rate": it.get("rate"),
                     "payment": payment, "confirmed": True})
        parts.append(f"{_num(it['qty'])} {it['unit']} {sku['canonical']}")
    result = main._write_events(etype, ev_items, TODAY.isoformat(), "exact", "voice_live")
    result["customer"] = {
        "customer_id": customer.get("customer_id"),
        "name": customer.get("name"), "phone": customer.get("phone"),
    } if customer else None
    if flow == "sale" and customer.get("customer_id") and deadline:
        credit_total = sum(c.get("amount") or 0 for i, c in enumerate(result["committed"])
                           if ev_items[i]["payment"] == "credit")
        if credit_total:
            result["receivable"] = repo.add_receivable(
                customer["customer_id"], credit_total, deadline,
                [c["event_id"] for i, c in enumerate(result["committed"])
                 if ev_items[i]["payment"] == "credit"])
    verb = {"sale": "bik gaya", "delivery": "aa gaya",
            "count": "gin liya"}.get(flow, "likh diya")
    say = f"Theek hai — {_oxford(parts)} {verb}, likh diya."
    if customer.get("name"):
        say += f" Customer: {customer['name']}."
    if deadline:
        say += f" Udhaar {deadline} tak."
    if skipped:
        say += f" {_oxford(skipped)} chhod diya, wo stock mein nahi."
    out = _say(state, say, listen=False, done=True)
    out["summary"] = {"items": rows}
    out["committed"] = result
    return out


# ---------------------------------------------------------------------------
# Stock question
# ---------------------------------------------------------------------------
def _stock_flow(state, items, repo):
    it = items[0] if items else None
    if not it or (it.get("in_catalogue") is False and not it.get("sku_id")):
        nm = (it or {}).get("name") or "Wo cheez"
        return _say(state, f"{nm} hum nahi rakhte — sirf sariya aur cement hai.",
                    listen=True, done=False)
    if not it.get("sku_id"):
        return _say(state, _ask_product(it), listen=True, done=False)
    return _answer_stock(repo.sku(it["sku_id"]), repo)


def _answer_stock(sku, repo):
    import main
    import ledger
    det = ledger._stock_detail(sku, repo.all_events())
    view = main._stock_view(sku, det)
    if view.get("uncounted"):
        say = f"{sku['canonical']} abhi tak gina nahi gaya — ek baar gin lo to ledger mein aa jayega."
    elif view.get("oversold"):
        say = f"{sku['canonical']} recorded se zyada bik gaya lagta hai — ek baar gin lo."
    else:
        say = f"{sku['canonical']} ka stock {view['display']} hai."
    return _reply(say, listen=False, done=True,
                  summary={"items": [{"phrase": sku["canonical"],
                                      "sku_id": sku["sku_id"],
                                      "unit": view.get("unit"), "confirmed": True}],
                           "answer": say})


# ---------------------------------------------------------------------------
# Analytics question (exact numbers from the ledger)
# ---------------------------------------------------------------------------
def _analytics_answer(metric, repo):
    import main
    t = main._today_summary()
    d = main.dashboard()
    mp = d["money_position"]
    if metric == "cash":
        say = f"Aaj cash {int(t['cash'])} rupaye aaya."
    elif metric == "udhaar":
        say = (f"Total udhaar {int(mp['outstanding_credit'])} rupaye baaki hai. "
               f"Aaj {int(t['credit'])} rupaye udhaar gaya.")
    elif metric == "frozen":
        ft = int(d["frozen_total"])
        items = d.get("frozen_capital") or []
        if not items:
            say = "Abhi koi maal 60 din se pada nahi hai, sab move ho raha hai."
        else:
            names = _oxford([f"{f['canonical']} ({f['stock']})" for f in items])
            say = f"{names} — 60 din se bika nahi, {ft} rupaye phasa hua hai."
    elif metric == "inventory":
        say = f"Inventory ki value {int(mp['inventory_value'])} rupaye hai, landed cost pe."
    else:
        say = (f"Aaj ka margin abhi tak {int(t['margin'])} rupaye. "
               f"Cash {int(t['cash'])}, udhaar {int(t['credit'])} rupaye.")
    return _reply(say, listen=False, done=True, summary={"items": [], "answer": say})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _say(state, text, listen, done):
    state.setdefault("history", []).append({"role": "assistant", "content": text})
    return {"state": state, "say": text, "listen": listen, "done": done,
            "summary": {"items": _draft_summary(state)}}


def _reply(say, listen=False, done=True, state=None, summary=None):
    return {"state": state, "say": say, "listen": listen, "done": done,
            "summary": summary or {"items": []}}


def _draft_summary(state):
    rows = []
    for item in state.get("draft_items") or []:
        rows.append({
            "phrase": item.get("name") or "",
            "sku_id": item.get("sku_id"),
            "qty": item.get("qty"), "unit": item.get("unit"),
            "rate": item.get("rate"), "payment": item.get("payment"),
            "confirmed": bool(item.get("sku_id") and item.get("qty") is not None),
        })
    return rows


def _oxford(names):
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " aur " + names[-1]


def _num(n):
    try:
        f = float(n)
        return int(f) if f == int(f) else f
    except Exception:
        return n
