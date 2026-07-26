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
    if re.search(r"agle hafte|next week|ek hafta", t):
        return (TODAY + timedelta(days=7)).isoformat()
    if re.search(r"kal|tomorrow", t):
        return (TODAY + timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s*(din|day)", t)
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


def _extract(state, repo) -> dict:
    joined = " . ".join(s for s in state["said"] if s)
    messages = [{"role": "system", "content": _extract_prompt(repo)},
                {"role": "user", "content": joined}]
    out = {}
    for _ in range(2):
        try:
            out = sarvam_client.chat_json(messages, temperature=0.1,
                                          reasoning_effort=_EFFORT, max_tokens=4096)
        except Exception:
            out = {}
        if isinstance(out, dict) and "items" in out:
            break
    if not isinstance(out, dict):
        out = {}
    items = []
    for r in out.get("items", []) or []:
        if not isinstance(r, dict):
            continue
        fam = r.get("family") if r.get("family") in _FAMILIES else None
        unit = (r.get("unit") or "").strip().lower() or None
        if unit:
            unit = M.UNIT_WORDS.get(unit, unit)
        items.append({
            "sku_id": r.get("sku_id") if _valid_sku(r.get("sku_id"), repo) else None,
            "family": fam, "name": r.get("name") or "",
            "in_catalogue": r.get("in_catalogue", True),
            "qty": r.get("qty") if isinstance(r.get("qty"), (int, float)) else None,
            "unit": unit,
            "rate": r.get("rate") if isinstance(r.get("rate"), (int, float)) else None,
            "payment": r.get("payment") if r.get("payment") in ("cash", "credit") else None,
        })
    raw_intent = out.get("intent")
    intent = raw_intent if raw_intent in _VALID_INTENTS else "unknown"
    return {"intent": intent, "metric": out.get("metric"), "items": items}


def _valid_sku(sid, repo) -> bool:
    return bool(sid) and repo.sku(sid) is not None


# ---------------------------------------------------------------------------
# Main entry — controller
# ---------------------------------------------------------------------------
def converse(state, user_text, flow, repo):
    if not sarvam_client.has_key():
        return _reply("Voice ke liye Sarvam API key chahiye.", listen=False, done=True)

    if not state or "said" not in state:
        state = {"flow": flow, "said": []}

    # Customer phone/name/deadline are deterministic data-entry slots (udhaar
    # sales only) — handled directly, not via the LLM extractor, and kept out
    # of state["said"] so they don't confuse product understanding.
    aw = state.get("awaiting")
    if aw in ("customer_phone", "customer_name", "deadline"):
        return _apply_customer_slot(state, aw, user_text, repo)

    state["said"].append(user_text or "")

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


def _order_flow(state, intent, items, repo):
    if intent not in ("sale", "delivery", "count"):
        # Never guess bought-vs-sold when the extractor couldn't classify it —
        # writing the wrong direction silently corrupts stock.
        return _say(state, "Ye maal aapne becha ya khareeda?", listen=True, done=False)
    flow = intent  # sale | delivery | count
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

    for it in kept:
        # 1) which product
        if not it.get("sku_id"):
            return _say(state, _ask_product(it), listen=True, done=False)
        sku = repo.sku(it["sku_id"])
        # 2) quantity
        if it.get("qty") in (None, ""):
            return _say(state, f"{sku['canonical']} — kitna?", listen=True, done=False)
        # 3) unit sanity
        if not it.get("unit") or it["unit"] not in sku.get("units", {}):
            it["unit"] = sku.get("default_unit")
        # 4) rate (sale AND delivery)
        if flow in ("sale", "delivery") and it.get("rate") is None:
            per = it["unit"]
            q = (f"{sku['canonical']} kitne mein aaya, per {per}?" if flow == "delivery"
                 else f"{sku['canonical']} ka rate kya laga, per {per}?")
            return _say(state, q, listen=True, done=False)
        # 5) payment (sale only)
        if flow == "sale" and not it.get("payment"):
            return _say(state, f"{sku['canonical']} — cash ya udhaar?",
                        listen=True, done=False)

    if flow == "sale" and any(it.get("payment") == "credit" for it in kept):
        state["pending_commit"] = {"flow": flow, "items": kept, "skipped": skipped}
        return _resume_customer_capture(state, repo)

    return _commit(state, flow, kept, skipped, repo)


# ---------------------------------------------------------------------------
# Udhaar customer capture (deterministic data entry, gates the commit)
# ---------------------------------------------------------------------------
def _apply_customer_slot(state, aw, user_text, repo):
    if aw == "customer_phone":
        phone = parse_phone(user_text)
        if not phone:
            return _say(state, "Customer ka 10 digit number bataiye udhaar ke liye.",
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
        return _say(state, "Customer ka 10 digit number bataiye udhaar ke liye.",
                    listen=True, done=False)
    if not customer.get("customer_id"):
        state["awaiting"] = "customer_name"
        return _say(state, "Naya customer hai — naam kya hai?", listen=True, done=False)
    if not state.get("payment_deadline"):
        state["awaiting"] = "deadline"
        return _say(state, f"{customer['name']} — payment kab tak? Date ya kitne din "
                    "baad bataiye.", listen=True, done=False)
    pc = state.pop("pending_commit")
    return _commit(state, pc["flow"], pc["items"], pc["skipped"], repo)


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
                         "customer_id": customer.get("customer_id") if payment == "credit" else None,
                         "payment_deadline": deadline if payment == "credit" else None})
        rows.append({"phrase": it.get("name") or sku["canonical"],
                     "sku_id": sku["sku_id"], "qty": float(it["qty"]),
                     "unit": it["unit"], "rate": it.get("rate"),
                     "payment": payment, "confirmed": True})
        parts.append(f"{_num(it['qty'])} {it['unit']} {sku['canonical']}")
    result = main._write_events(etype, ev_items, TODAY.isoformat(), "exact", "voice_live")
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
    if customer.get("name") and deadline:
        say += f" {customer['name']} ke udhaar mein, {deadline} tak."
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
        say = (f"Frozen capital {ft} rupaye ka phasa hua hai — 60 din se hila nahi."
               if ft else "Abhi koi capital phasa hua nahi hai, accha hai.")
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
    return {"state": state, "say": text, "listen": listen, "done": done,
            "summary": {"items": []}}


def _reply(say, listen=False, done=True, state=None, summary=None):
    return {"state": state, "say": say, "listen": listen, "done": done,
            "summary": summary or {"items": []}}


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
