"""
conversation.py — voice-first slot-filling dialogue for a sale/delivery/count.

Chhotu asks follow-up questions BY VOICE (never taps): product disambiguation,
"ye wala na?" learning confirmation, price ("N rupaye per bori na?"), payment
(cash/udhaar), unit, and date. The user answers by voice; Saaras transcribes,
this module interprets the answer in the context of what was asked, and advances.

Deterministic state machine (not free-form LLM dialogue) so it is reliable on
stage. All product understanding still flows through matcher.py (Sarvam-backed).

State is a plain dict passed between client and server each turn — the server
stays stateless.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import matcher as M
import nlp
import learning as LEARN

TODAY = date(2026, 7, 26)

_YES = re.compile(r"\b(haan|haa+|ha|ji|yes|theek|thik|sahi|bilkul|ok|okay|done|kar do|likh do|correct|sad?i)\b", re.I)
_NO = re.compile(r"\b(nah?in?|nahi+|no|galat|nope|mat|alag)\b", re.I)
_CASH = re.compile(r"\b(cash|nagad|nakad)\b", re.I)
_CREDIT = re.compile(r"\b(udhaar|udhar|credit|baaki|khaata|khata)\b", re.I)

_ONES = dict(nlp.ONES)  # ek..sau
_MULT = {"sau": 100, "so": 100, "hazaar": 1000, "hazar": 1000, "hajar": 1000,
         "hajaar": 1000, "lakh": 100000, "lac": 100000}


def parse_yes_no(text: str):
    t = (text or "").lower()
    if _NO.search(t):
        return False
    if _YES.search(t):
        return True
    return None


def parse_payment(text: str):
    t = (text or "").lower()
    if _CREDIT.search(t):
        return "credit"
    if _CASH.search(t):
        return "cash"
    return None


def parse_number(text: str):
    """Digits first (Saaras usually returns '350'); else Hindi words incl. hundreds."""
    t = (text or "").lower().replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", t)
    if m:
        return float(m.group())
    toks = re.findall(r"[a-zऀ-ॿ]+", t)
    total = 0.0
    cur = 0.0
    found = False
    for w in toks:
        if w in _MULT:
            m = _MULT[w]
            if m == 100:
                cur = (cur or 1) * 100
            else:  # hazaar / lakh close the current group
                total += (cur or 1) * m
                cur = 0
            found = True
        elif w in _ONES:
            cur += _ONES[w]
            found = True
    total += cur
    return total if found else None


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
    n = parse_number(t)
    if n and re.search(r"din|day", t):
        return (TODAY + timedelta(days=int(n))).isoformat()
    if re.search(r"agle hafte|next week|ek hafta", t):
        return (TODAY + timedelta(days=7)).isoformat()
    if re.search(r"kal|tomorrow", t):
        return (TODAY + timedelta(days=1)).isoformat()
    return None


# ---------------------------------------------------------------------------
# Ingest first utterance
# ---------------------------------------------------------------------------
# Devanagari normalization (units / "aur" / digits) — product names are left
# for the matcher, which has Devanagari aliases (सरिया, सीमेंट, पाइप).
_DEVA_DIGITS = {'०': '0', '१': '1', '२': '2', '३': '3', '४': '4',
                '५': '5', '६': '6', '७': '7', '८': '8', '९': '9'}
_DEVA_WORDS = {'और': ' aur ', 'बोरी': ' bori ', 'बोरा': ' bora ', 'बोरे': ' bori ',
               'टन': ' ton ', 'किलो': ' kilo ', 'पीस': ' piece ', 'नग': ' nag ',
               'बंडल': ' bundle ', 'फ़ीट': ' feet ', 'फीट': ' feet ',
               'लीटर': ' litre ', 'मीटर': ' metre '}


def _norm_deva(t: str) -> str:
    t = t or ""
    for d, r in _DEVA_DIGITS.items():
        t = t.replace(d, r)
    for d, r in _DEVA_WORDS.items():
        t = t.replace(d, r)
    return t


def _segment(text: str, repo) -> list:
    """
    Split an utterance into per-product item texts. The LLM handles products NOT
    joined by 'aur' ("3 bori cement 2 ton sariya"); falls back to aur/comma split.
    Each returned span keeps its own quantity + unit for deterministic parsing.
    """
    parts = _llm_segment(text)
    if parts:
        return parts
    segs = re.split(r"\baur\b|,|\+|;", text)
    return [s.strip() for s in segs if s.strip()]


def _llm_segment(text: str):
    import sarvam_client
    if not sarvam_client.has_key():
        return None
    try:
        out = sarvam_client.chat_json([
            {"role": "system", "content":
             "Split a hardware-shop owner's utterance into separate product "
             "line-items. Return ONLY JSON: {\"items\":[{\"text\":\"...\"}]}. "
             "Each text must keep that product's quantity, unit and name together. "
             "Do NOT invent items, do NOT add products not said. Input may be "
             "romanized Hindi (e.g. '3 bori cement 2 ton sariya aur 3 bori tile')."},
            {"role": "user", "content": text}])
        parts = [i.get("text", "").strip() for i in out.get("items", [])
                 if i.get("text", "").strip()]
        return parts or None
    except Exception:
        return None


def _ingest(text: str, flow: str, repo) -> dict:
    # Normalize Devanagari units/aur/digits, segment into products (LLM), then
    # parse qty/unit per item DETERMINISTICALLY; product id via the matcher.
    norm = _norm_deva(text)
    # Sale payment is confirmed once for the whole sale after customer identity.
    spoken_pay = parse_payment(norm) if flow != "live_sale" else None
    items = []
    for seg in _segment(norm, repo):
        qty, corrected = nlp._find_qty(seg)
        unit = nlp._find_unit(seg)
        if unit:
            unit = M.UNIT_WORDS.get(str(unit).lower(), unit)
        items.append({
            "phrase": seg, "qty": qty, "unit": unit, "rate": None,
            "payment": spoken_pay, "self_corrected": corrected,
            "sku_id": None, "confirmed": False, "secondary": None,
        })
    temporal = M.resolve_temporal(norm, TODAY)
    temporal["date"] = temporal["date"].isoformat()
    temporal["confirmed"] = not temporal["ask"]
    return {"flow": flow, "items": items, "temporal": temporal, "mode": "entry",
            "cursor": 0, "awaiting": None, "ctx": {}, "transcript": text}


# ---------------------------------------------------------------------------
# Helpers for the current item
# ---------------------------------------------------------------------------
def _cur(state):
    i = state["cursor"]
    items = state["items"]
    return items[i] if 0 <= i < len(items) else None


def _resolve_product(it, repo):
    """Run the matcher and stamp the result on the item."""
    cat = repo.load_catalogue()
    learn = repo.load_learning()
    phrase = it["phrase"] if not it.get("secondary") else it["phrase"]
    m = M.match(phrase, cat, learn, "live_sale")
    it["match"] = m
    return m


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def converse(state, user_text, flow, repo):
    """
    Returns dict: {state, say, listen, done, summary, committed?}
    - state None -> start a new turn. flow='auto' routes the intent
      (sale / delivery / count / stock_query / analytics_query).
    - else apply user_text as the answer to state['awaiting'], then advance.
    """
    if not state:
        if flow == "auto":
            return _route_and_start(user_text, repo)
        state = _ingest(user_text, flow, repo)
    else:
        if state.get("await_intent"):
            return _route_and_start(user_text, repo)
        _apply_answer(state, user_text, repo)
    return _advance(state, repo)


# ---------------------------------------------------------------------------
# Intent routing (central "Hey Chhotu" assistant)
# ---------------------------------------------------------------------------
def _route_and_start(text, repo):
    r = _intent(_norm_deva(text))
    intent = r.get("intent")
    if intent == "stock_query":
        return _start_stock_query(r.get("product") or text, repo)
    if intent == "analytics_query":
        return _analytics_answer(r.get("metric"), repo)
    if intent in ("sale", "delivery", "count"):
        flow = {"sale": "live_sale", "delivery": "delivery",
                "count": "opening_balance"}[intent]
        return _advance(_ingest(text, flow, repo), repo)
    return _reply("Kya karna hai — sale, delivery, stock ki ginti, ya hisaab?",
                  listen=True, done=False,
                  state={"await_intent": True, "items": [], "cursor": 0})


def _intent(t: str) -> dict:
    """Keyword intent routing — fast, deterministic, no API dependency.
    Order matters: analytics/stock QUESTIONS first, then delivery/count/sale
    ACTIONS. Payment words (cash/udhaar) inside a sale must NOT trigger analytics.
    """
    t = " " + t.lower() + " "
    kitna = bool(re.search(r"kitna|kitne|kitni", t))
    # 1) analytics questions — real metric words, or "kitna udhaar/margin"
    if re.search(r"margin|hisaab|hisab|kamaya|kamai|profit|frozen|phasa|phase|atka|inventory", t) \
            or (kitna and re.search(r"udhaar|udhar|margin|kamaya|hisaab", t)):
        metric = "margin"
        if re.search(r"udhaar|udhar|baaki|baki|lena", t):
            metric = "udhaar"
        elif re.search(r"frozen|phasa|phase|atka", t):
            metric = "frozen"
        elif re.search(r"inventory|maal ki value|stock value", t):
            metric = "inventory"
        elif re.search(r"\bcash\b|nagad", t):
            metric = "cash"
        return {"intent": "analytics_query", "metric": metric}
    # 2) stock question — "kitna ... hai/stock/bacha", no sale/delivery verb
    if (kitna or re.search(r"\bbacha|bache|balance\b", t)) \
            and re.search(r"stock|bacha|bache|hai|maal|godown|balance|available|pada", t):
        prod = re.sub(r"\b(kitna|kitne|kitni|stock|bacha|bache|hai|kya|balance|ka|ki|abhi|available|godown|mein|me|pada|hua|baaki)\b",
                      " ", t)
        return {"intent": "stock_query", "product": re.sub(r"\s+", " ", prod).strip()}
    # 3) delivery action
    if re.search(r"\baaya|aayi|aaye|delivery|receive|khareed|mangwaya\b", t):
        return {"intent": "delivery"}
    # 4) count action
    if re.search(r"\bgina|ginti|opening|stock take|stock-take|count kar\b", t):
        return {"intent": "count"}
    # 5) default: sale
    return {"intent": "sale"}


def _reply(say, listen=False, done=True, state=None, summary=None):
    return {"state": state, "say": say, "listen": listen, "done": done,
            "summary": summary or {"items": []}}


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
        say = f"Frozen capital {int(d['frozen_total'])} rupaye ka phasa hua hai — 60 din se hila nahi."
    elif metric == "inventory":
        say = f"Inventory ki value {int(mp['inventory_value'])} rupaye hai, landed cost pe."
    else:
        say = (f"Aaj ka margin abhi tak {int(t['margin'])} rupaye. "
               f"Cash {int(t['cash'])}, udhaar {int(t['credit'])} rupaye.")
    return _reply(say, done=True, summary={"items": [], "answer": say})


def _start_stock_query(product_text, repo):
    st = {"flow": "live_sale", "mode": "stock_query",
          "items": [{"phrase": product_text, "qty": None, "unit": None,
                     "rate": None, "payment": None, "sku_id": None,
                     "confirmed": False}],
          "temporal": {"confirmed": True, "ask": False},
          "cursor": 0, "awaiting": None, "ctx": {}}
    return _advance(st, repo)


def _stock_answer_for(sku, repo):
    import main
    import ledger
    det = ledger._stock_detail(sku, repo.all_events())
    view = main._stock_view(sku, det)
    if view.get("uncounted"):
        say = f"{sku['canonical']} abhi tak gina nahi gaya — gin lo to ledger mein aa jayega."
    elif view.get("oversold"):
        say = f"{sku['canonical']} recorded se zyada bik gaya lagta hai — ek baar gin lo."
    else:
        say = f"{sku['canonical']} ka stock {view['display']} hai."
    return _reply(say, done=True, summary={"items": [
        {"phrase": sku["canonical"], "sku_id": sku["sku_id"], "qty": None,
         "unit": view.get("unit"), "confirmed": True}], "answer": say})


def _apply_answer(state, text, repo):
    aw = state.get("awaiting")
    it = _cur(state)
    if not aw:
        return
    if aw in {"disambiguate", "confirm_product", "pick_candidate", "qty",
              "rate", "confirm_rate", "unit"} and it is None:
        return
    ctx = state.get("ctx", {})

    if aw == "disambiguate":
        # record the spoken attribute value; _advance will ask the next open one
        opts = ctx.get("options", [])
        attr = ctx.get("attribute")
        attrs = it.setdefault("attrs", {})
        spoken = M.extract_attrs(text)
        val = None
        if attr in spoken:
            val = spoken[attr]
        else:
            low = text.lower()
            for o in opts:
                if str(o["value"]).lower() in low or o["label"].lower() in low:
                    val = o["value"]
                    break
        if val is not None:
            attrs[attr] = val
            it["_fail"] = 0
        else:
            it["_fail"] = it.get("_fail", 0) + 1
        state["awaiting"] = None

    elif aw == "confirm_product":
        yn = parse_yes_no(text)
        if yn is True:
            it["confirmed"] = True
            _learn(state, it, repo, was_tap=False)
        elif yn is False:
            # rejected -> re-open with alternatives
            it["sku_id"] = None
            it["_reject"] = (it.get("_reject") or []) + ([ctx.get("sku_id")] if ctx.get("sku_id") else [])
        state["awaiting"] = None

    elif aw == "pick_candidate":
        low = text.lower()
        cands = ctx.get("candidates", [])
        chosen = None
        for c in cands:
            canon = (repo.sku(c["sku_id"]) or {}).get("canonical", "").lower()
            if c["sku_id"].lower() in low or any(w in canon for w in low.split() if len(w) > 2):
                chosen = c
                break
        if chosen:
            it["sku_id"] = chosen["sku_id"]
            it["confirmed"] = True
            _learn(state, it, repo, was_tap=True)
        state["awaiting"] = None

    elif aw == "qty":
        n = parse_number(text)
        if n:
            it["qty"] = n
        state["awaiting"] = None

    elif aw == "rate":
        n = parse_number(text)
        if n:
            state["ctx"]["proposed_rate"] = n
            # move to confirm the rate
        state["awaiting"] = None
        state["_pending_confirm_rate"] = bool(n)

    elif aw == "confirm_rate":
        yn = parse_yes_no(text)
        if yn is True:
            it["rate"] = ctx.get("proposed_rate")
        elif yn is False:
            n = parse_number(text)  # maybe they restated the number
            if n:
                it["rate"] = n
            else:
                state["ctx"]["proposed_rate"] = None  # ask again
        state["awaiting"] = None

    elif aw == "payment":
        p = parse_payment(text)
        state["payment"] = p or "cash"
        for row in state.get("items", []):
            row["payment"] = state["payment"]
        state["awaiting"] = None

    elif aw == "customer_phone":
        phone = parse_phone(text)
        if phone:
            customer = repo.customer_by_phone(phone)
            state["customer"] = {
                "phone": repo.normalize_phone(phone),
                "customer_id": customer.get("customer_id") if customer else None,
                "name": customer.get("name") if customer else None,
            }
        state["awaiting"] = None

    elif aw == "customer_name":
        name = (text or "").strip()
        if name:
            customer = repo.upsert_customer(state["customer"]["phone"], name)
            state["customer"] = {
                "phone": customer["phone"], "customer_id": customer["customer_id"],
                "name": customer["name"],
            }
        state["awaiting"] = None

    elif aw == "deadline":
        deadline = parse_deadline(text)
        if deadline:
            state["payment_deadline"] = deadline
        state["awaiting"] = None

    elif aw == "unit":
        # accept a spoken unit, else confirm the proposed one
        u = _spoken_unit(text, repo.sku(it["sku_id"]))
        if u:
            it["unit"] = u
            it["unit_assumed"] = False
        else:
            yn = parse_yes_no(text)
            if yn is False and ctx.get("alt"):
                it["unit"] = ctx["alt"]
            it["unit_assumed"] = False  # confirmed either way
        state["awaiting"] = None

    elif aw == "date":
        yn = parse_yes_no(text)
        T = state["temporal"]
        if yn is False:
            T["date"] = TODAY.isoformat()
            T["precision"] = "day"
        elif ctx.get("vague"):
            T["precision"] = "week"
        T["confirmed"] = True
        state["awaiting"] = None


def _spoken_unit(text, sku):
    if not sku:
        return None
    t = (text or "").lower()
    for w, u in M.UNIT_WORDS.items():
        if re.search(rf"\b{w}\b", t) and u in sku.get("units", {}):
            return u
    return None


def _learn(state, it, repo, was_tap):
    if it.get("sku_id") and it.get("phrase"):
        LEARN.record_confirmation(repo, it["phrase"], it["sku_id"],
                                  rejected=it.get("_reject", []),
                                  unit=it.get("unit"), was_tap=was_tap)


# ---------------------------------------------------------------------------
# Advance — find the next thing to ask, or commit
# ---------------------------------------------------------------------------
def _advance(state, repo):
    flow = state["flow"]
    items = state["items"]
    if not items:
        return _say(state, "Maaf kijiye, kuch samajh nahi aaya. Dobara boliye.",
                    listen=True, done=False)

    # rate confirm pending?
    if state.pop("_pending_confirm_rate", False):
        it = _cur(state)
        pr = state["ctx"].get("proposed_rate")
        if pr:
            unit = it.get("unit") or "unit"
            state["awaiting"] = "confirm_rate"
            return _say(state, f"{int(pr)} rupaye per {unit}, theek hai na?",
                        listen=True, done=False)

    while state["cursor"] < len(items):
        it = items[state["cursor"]]

        # 1) product — accumulate attributes, ask ONE short question per turn
        if not it.get("sku_id"):
            cat = repo.load_catalogue()
            learn = repo.load_learning()
            by = {s["sku_id"]: s for s in cat}
            attrs = it.get("attrs", {})
            if it.get("fam"):  # in an attribute-narrowing dialogue
                cand_ids = [s["sku_id"] for s in cat if s["family"] == it["fam"]]
                m = M.resolve_variant(cand_ids, by, attrs, learn)
            else:
                m = M.match(it["phrase"], cat, learn, "live_sale")
            st = m.get("status")
            if st == "matched":
                it["sku_id"] = m["sku_id"]
                if it.get("fam"):
                    # user chose every attribute by voice -> that IS confirmation
                    it["confirmed"] = True
                    _learn(state, it, repo, was_tap=True)
                elif m.get("stage") == "alias":
                    it["confirmed"] = True  # exact learned/catalogue alias
                else:
                    # system guessed (fuzzy/semantic/prior) -> confirm first
                    state["ctx"] = {"sku_id": m["sku_id"]}
                    state["awaiting"] = "confirm_product"
                    return _say(state, f"{_speak_name(by.get(m['sku_id']))} — ye wala na?",
                                listen=True, done=False)
            elif st == "disambiguate":
                if not it.get("fam") and m.get("options"):
                    it["fam"] = by[m["options"][0]["sku_ids"][0]]["family"]
                # after 2 mishears, read the actual product names instead
                if it.get("_fail", 0) >= 2:
                    ids = [sid for o in m["options"] for sid in o["sku_ids"]]
                    state["ctx"] = {"candidates": [{"sku_id": i} for i in ids]}
                    state["awaiting"] = "pick_candidate"
                    it["_fail"] = 0
                    names = _oxford([by[i]["canonical"] for i in ids][:6])
                    return _say(state, f"Naam se batao — {names}, inme se kaunsa?",
                                listen=True, done=False)
                state["ctx"] = {"attribute": m["attribute"], "options": m["options"]}
                state["awaiting"] = "disambiguate"
                opts = " ya ".join(o["label"] for o in m["options"][:6])
                return _say(state, f"{m['question']} — {opts}?", listen=True, done=False)
            elif st == "not_stocked":
                avail = " ya ".join(m.get("available", []))
                state["cursor"] += 1  # skip this item
                return _say(state, f"{m['value']} to stock mein nahi hai. "
                            f"Humare paas {avail} hai. Agla batao.",
                            listen=True, done=False)
            else:  # confirm / uncertain -> read the few candidates
                cands = m.get("candidates", [])
                if not cands:
                    state["cursor"] += 1
                    return _say(state, "Ye samajh nahi aaya, chhod diya. Aage boliye.",
                                listen=True, done=False)
                state["ctx"] = {"candidates": cands}
                state["awaiting"] = "pick_candidate"
                names = _oxford([(by.get(c["sku_id"]) or {}).get("canonical", "")
                                 for c in cands])
                return _say(state, f"{names} — inme se kaunsa?", listen=True, done=False)

        if not it.get("confirmed"):
            # matched non-exact but confirm slot not yet asked (safety)
            sku = repo.sku(it["sku_id"])
            state["ctx"] = {"sku_id": it["sku_id"]}
            state["awaiting"] = "confirm_product"
            return _say(state, f"{_speak_name(sku)} — ye wala na?", listen=True, done=False)

        sku = repo.sku(it["sku_id"])

        # stock query -> answer now, no further slots
        if state.get("mode") == "stock_query":
            return _stock_answer_for(sku, repo)

        # 1b) quantity (a segment may have no number)
        if it.get("qty") is None:
            state["awaiting"] = "qty"
            return _say(state, f"{_speak_name(sku)} — kitna?", listen=True, done=False)

        # 2) unit (confirm if missing/assumed/unknown to this SKU)
        if (not it.get("unit") or it.get("unit_assumed")
                or it.get("unit") not in sku.get("units", {})):
            u = it.get("unit")
            if not u or u not in sku.get("units", {}):
                res = M.resolve_unit(it["phrase"], sku, repo.load_learning())
                u = res["unit"]
                it["unit"] = u
            state["ctx"] = {"alt": _other_unit(sku, u)}
            state["awaiting"] = "unit"
            return _say(state, f"{u} mein na?", listen=True, done=False)

        # 3) rate (sale only)
        if flow == "live_sale" and not it.get("rate"):
            state["awaiting"] = "rate"
            return _say(state, f"Rate kya laga, per {it['unit']}?", listen=True, done=False)

        state["cursor"] += 1  # item complete

    # 5) date (once, after items)
    T = state["temporal"]
    if not T.get("confirmed"):
        state["awaiting"] = "date"
        if T.get("vague"):
            return _say(state, f"{T['marker']} — pakka nahi, is hafte maan lein?",
                        listen=True, done=False)
        friendly = _friendly_date(T["date"])
        return _say(state, f"{T.get('marker','')} — {friendly}, theek?", listen=True, done=False)

    # Customer identity and payment are transaction-level slots.
    if flow == "live_sale":
        customer = state.get("customer") or {}
        if not customer.get("phone"):
            state["awaiting"] = "customer_phone"
            return _say(state, "Customer ka 10 digit contact number bataiye.",
                        listen=True, done=False)
        if not customer.get("customer_id"):
            known = repo.customer_by_phone(customer["phone"])
            if known:
                state["customer"] = {
                    "phone": known["phone"], "customer_id": known["customer_id"],
                    "name": known["name"],
                }
            else:
                state["awaiting"] = "customer_name"
                return _say(state, "Naya customer hai. Naam kya hai?",
                            listen=True, done=False)
        if not state.get("payment"):
            state["awaiting"] = "payment"
            return _say(state, f"{state['customer']['name']} — cash ya udhaar?",
                        listen=True, done=False)
        if state["payment"] == "credit" and not state.get("payment_deadline"):
            state["awaiting"] = "deadline"
            return _say(state, "Payment ki deadline kab hai? Date ya kitne din baad bataiye.",
                        listen=True, done=False)

    # DONE -> commit
    return _commit(state, repo)


def _commit(state, repo):
    import main
    T = state["temporal"]
    occurred_on = T["date"]
    precision = "week" if T.get("precision") == "week" else \
        ("exact" if not T.get("ask") else "day")
    etype = "sale" if state["flow"] == "live_sale" else state["flow"]
    customer = state.get("customer") or {}
    payment = state.get("payment", "cash")
    items = [{"sku_id": it["sku_id"], "qty": it["qty"], "unit": it["unit"],
              "rate": it.get("rate"), "payment": payment,
              "customer_id": customer.get("customer_id"),
              "payment_deadline": state.get("payment_deadline"),
              "spoken": it["phrase"], "was_tap": False}
             for it in state["items"] if it.get("sku_id")]
    source = "voice_live" if occurred_on == TODAY.isoformat() else "voice_recall"
    result = main._write_events(etype, items, occurred_on, precision, source)
    if etype == "sale" and payment == "credit" and customer.get("customer_id"):
        total = sum(float(c.get("amount") or 0) for c in result["committed"])
        result["receivable"] = repo.add_receivable(
            customer["customer_id"], total, state["payment_deadline"],
            [c["event_id"] for c in result["committed"]],
        )
    # spoken summary
    parts = []
    for it in state["items"]:
        if not it.get("sku_id"):
            continue
        sku = repo.sku(it["sku_id"])
        parts.append(f"{_num(it['qty'])} {it['unit']} {_speak_name(sku)}")
    summ = ", ".join(parts)
    if payment == "credit":
        say = (f"Theek hai. {summ} {customer.get('name')} ke udhaar mein likh diya. "
               f"Deadline {state.get('payment_deadline')} hai.")
    else:
        say = f"Theek hai. {summ} cash mein likh diya. Aaj ka margin update ho gaya."
    out = _say(state, say, listen=False, done=True)
    out["committed"] = result
    return out


# ---------------------------------------------------------------------------
# Speech helpers
# ---------------------------------------------------------------------------
def _say(state, text, listen, done):
    return {"state": state, "say": text, "listen": listen, "done": done,
            "summary": _summary(state)}


def _summary(state):
    rows = []
    for idx, it in enumerate(state["items"]):
        rows.append({
            "phrase": it["phrase"], "qty": it.get("qty"), "unit": it.get("unit"),
            "rate": it.get("rate"), "payment": it.get("payment"),
            "sku_id": it.get("sku_id"), "confirmed": it.get("confirmed"),
            "self_corrected": it.get("self_corrected"),
            "active": idx == state["cursor"],
        })
    return {"items": rows, "temporal": state.get("temporal"),
            "awaiting": state.get("awaiting"), "customer": state.get("customer"),
            "payment": state.get("payment"),
            "payment_deadline": state.get("payment_deadline")}


def _speak_name(sku):
    return sku.get("canonical", "") if sku else ""


def _other_unit(sku, u):
    for k in (sku or {}).get("units", {}):
        if k != u:
            return k
    return None


def _oxford(names):
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " ya " + names[-1]


def _num(n):
    try:
        f = float(n)
        return int(f) if f == int(f) else f
    except Exception:
        return n


def _friendly_date(iso):
    d = date.fromisoformat(iso)
    diff = (TODAY - d).days
    return {0: "aaj", 1: "kal", 2: "parso"}.get(diff, iso)
