"""
conversation.py — voice dialogue for "Hey Chhotu".

Design: the LLM performs the rich extraction once; deterministic guards verify
transaction verbs, item boundaries, catalogue membership, units, and amounts.
A thin voice controller then retains that structured draft until the bill is
confirmed, so nothing commits half-filled.

  • The first turn sends the conversation history to Sarvam and receives a
    structured understanding (intent + items, each with sku_id/family, qty,
    rate, and payment). Deterministic parsing protects explicit words such as
    bechi/बेची and prevents absent products from being remapped to stocked SKUs.
  • Follow-up answers update the same structured draft locally. The original
    transcript, history, accepted lines, and rejected lines stay in state through
    product/rate/customer questions, confirmation, commit, and bill creation.
  • A small controller then looks at that understanding and asks — by voice — for
    the ONE thing still missing (which product / kitna / rate / cash-or-udhaar),
    or commits when everything is there. This guarantees Chhotu always asks the
    price and never records an incomplete entry.

Keeping the LLM job to "extract", not "run the whole dialogue policy", keeps its
reasoning short — important because sarvam-30b is a reasoning model capped at
4096 tokens on this tier, and a heavier task overruns that budget.

Exact figures (stock on hand, margin, udhaar) are computed by the backend and
spoken by Chhotu — never invented by the model.

State is a plain dict passed between client and server each turn and includes
`history`, `original_transcript`, `draft_items`, `skipped_items`, and the current
awaiting slot.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import date, timedelta

import clock
import matcher as M
import translit
import nlp
import sarvam_client

_FAMILIES = ("tmt", "cement", "pipe", "fitting", "fastener", "paint")
_HINDI_NUMBERS = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5,
    "das": 10, "barah": 12, "pandrah": 15, "solah": 16, "bees": 20,
    "pachees": 25, "tees": 30, "pachaas": 50, "sau": 100,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "दस": 10,
    "बारह": 12, "पंद्रह": 15, "सोलह": 16, "बीस": 20,
    "पच्चीस": 25, "तीस": 30, "पचास": 50, "सौ": 100,
}

# 'low' = fast (~8s/turn), great when Saaras returns numbers as digits (usual).
# 'medium' = slower (~30s/turn) but firmer on spelled-out Hindi number WORDS.
_EFFORT = os.environ.get("CHHOTU_REASONING", "low")

# Dynamic language: Chhotu's default voice is Hindi/Hinglish, but the owner
# may speak (and expect replies) in plain English. Detected once from the
# FIRST utterance and locked into state — like locked_intent — so a later
# short answer ("12mm", a phone number) can't flip the language mid-order.
_HINDI_MARK_RE = re.compile(
    r"[ऀ-ॿ]|"  # Devanagari script
    r"\b(hai|hain|kya|kitna|kitne|bacha|bika|becha|bech|khareed|kharida|"
    r"udhaar|udhar|mein|nahi|haan|aur|sariya|saria|paisa|rupaye|rupaya|"
    r"bataiye|dabaiye|maal|kaunsa|wala|theek|thik|gaya|gayi|diya|hafte|"
    r"mahine|bori|"
    # Everyday words that carry no English collision. Without these a plain
    # romanized question like "aaj ka hisaab batao" was answered in English.
    r"aaj|kal|abhi|hisaab|hisab|batao|bata|bataye|karo|kiya|naam|chahiye|"
    r"saamaan|samaan|thoda|zyada|poora|pura|kaisa|kaisi|kaise|raha|rahi|"
    r"mera|meri|mere|apna|apne|humne|hamne|maine|saptah|paise|nagad|"
    r"jaldi|dena|lena|gin|ginti|baaki|phir|yeh|kuch)\b",
    re.I,
)


# Devanagari spells the same word more than one way. The nukta (U+093C) is
# optional in ordinary typing and STT output — "हफ़्ते" and "हफ्ते" are the
# same word but different codepoints, so a pattern written one way silently
# misses the other. Every deterministic matcher below folds the text first
# (NFD to split precomposed nukta letters like क़, drop the nukta, recompose).
# Matching only — never use this on text we display or store.
def _fold(text: str) -> str:
    t = unicodedata.normalize("NFD", text or "")
    t = t.replace("़", "")
    return unicodedata.normalize("NFC", t).lower()


def _config(repo) -> dict:
    """Settings, read defensively — a repo stub in a test may not have any."""
    try:
        return repo.load_config() or {}
    except Exception:
        return {}


def _detect_lang(text: str):
    t = _fold(text).strip()
    if not t:
        return None
    if _HINDI_MARK_RE.search(t):
        return "hi"
    if re.search(r"[A-Za-z]", t):
        return "en"
    return None


def _L(state, hi: str, en: str) -> str:
    """Pick the reply string for the conversation's detected language."""
    return en if (state or {}).get("lang") == "en" else hi


def parse_phone(text: str):
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def _transliterate_to_latin(text: str) -> str:
    """Best-effort: a Devanagari name -> its normal English spelling, so
    customer names are always saved in Latin script regardless of which
    language the owner spoke in. A no-op (and no API call) when there's no
    Devanagari to convert."""
    if not re.search(r"[ऀ-ॿ]", text or ""):
        return text
    try:
        # sarvam-30b spends tokens on reasoning_content BEFORE the answer even
        # for a trivial ask — max_tokens=30 cut it off mid-reasoning every
        # time (finish_reason=length, content=None), the same failure mode
        # fixed earlier for the main extractor. Give it real room, and read
        # reasoning_content as a last-resort fallback like chat_json does.
        resp = sarvam_client.chat(
            [{"role": "system", "content":
              "Transliterate this Indian person's name into English/Latin "
              "script, exactly as it is normally spelled in English (not a "
              "strict letter-by-letter transliteration). Reply with ONLY "
              "the name — no quotes, no explanation."},
             {"role": "user", "content": text}],
            temperature=0.1, reasoning_effort=_EFFORT, max_tokens=4096, timeout=75)
        msg = sarvam_client.chat_message(resp)
        out = (msg.get("content") or "").strip().strip('"')
        # The model sometimes echoes the Devanagari back, and on a serverless
        # host this call can time out entirely. Either way the name must not
        # stay in Devanagari, so fall through to the deterministic mapping.
        if out and not translit.has_devanagari(out):
            return out
    except Exception:
        pass
    return translit.to_latin_name(text)


def _number_in_text(text: str):
    m = re.search(r"\d+(?:\.\d+)?", text or "")
    if m:
        return float(m.group(0))
    t = (text or "").lower()
    for word, value in _HINDI_NUMBERS.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", t):
            return float(value)
    return None


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
            return date(int(dm.group(3) or clock.today().year), int(dm.group(2)),
                        int(dm.group(1))).isoformat()
        except ValueError:
            return None
    if re.search(r"agle hafte|next week|ek hafta|अगले हफ्ते|एक हफ्ता", t):
        return (clock.today() + timedelta(days=7)).isoformat()
    if re.search(r"agle mahine|next month|ek mahina|एक महीना|अगले महीने", t):
        return (clock.today() + timedelta(days=30)).isoformat()
    if re.search(r"kal|tomorrow|कल", t):
        return (clock.today() + timedelta(days=1)).isoformat()
    if re.search(r"parso|परसों|परसो|day after tomorrow", t):
        return (clock.today() + timedelta(days=2)).isoformat()
    relative = re.search(
        r"([\w\u0900-\u097f.]+)\s*(din|days?|दिन|hafte|weeks?|हफ्ते|"
        r"mahine|months?|महीने|महीना)", t)
    if relative:
        number = _number_in_text(relative.group(1))
        if number is not None:
            unit = relative.group(2)
            multiplier = 30 if re.search(r"mahine|month|मही", unit) else \
                (7 if re.search(r"hafte|week|हफ्ते", unit) else 1)
            return (clock.today() + timedelta(days=int(number * multiplier))).isoformat()
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3,
        "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
        "aug": 8, "september": 9, "october": 10, "november": 11,
        "december": 12,
    }
    named = re.search(r"\b(\d{1,2})\s+([a-z]+)\b", t)
    if named and named.group(2) in months:
        try:
            return date(clock.today().year, months[named.group(2)],
                        int(named.group(1))).isoformat()
        except ValueError:
            return None
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
        f"Today: {clock.today().isoformat()}.\n"
        "The shop stocks ONLY these products:\n" + _catalogue_lines(repo) + "\n\n"
        "Hindi numbers: ek=1 do=2 teen=3 char=4 paanch=5 chhe=6 saat=7 aath=8 "
        "nau=9 das=10 barah=12 solah=16 bees=20 pachees=25 tees=30 chalees=40 "
        "pachaas=50 pachpan=55 saath=60 chausath=64 assi=80 sau=100 hazaar=1000 "
        "lakh=100000; dhai=2.5 saade=+0.5 sava=+0.25 paune=-0.25. Digits stay as-is.\n\n"
        "Return JSON:\n"
        '{"intent":"sale|delivery|count|stock_query|analytics_query|chitchat|unknown",'
        '"metric":"margin|cash|udhaar|frozen|inventory|day_summary|week_summary|null",'
        '"reply":"<ONLY for intent=chitchat: a short, warm, natural reply in the '
        'SAME language/script the owner used — a real greeting/thanks/small-talk '
        'reply, not a canned line. null for every other intent>",'
        '"items":[{"sku_id":"<exact sku_id, or null if not pinned to ONE>",'
        '"family":"tmt|cement|tiles|null","name":"<what he called it>",'
        '"in_catalogue":true|false,"qty":<number or null>,"unit":"<unit or empty>",'
        '"rate":<number or null>,"rate_unit":"kg|tonne|bori|piece|null",'
        '"payment":"cash|credit|null"}]}\n\n'
        "RULES:\n"
        "- Combine EVERYTHING said across the whole text (later lines answer earlier "
        "questions). Keep the owner's self-corrections ('do nahi teen' -> 3).\n"
        "- sku_id: set it ONLY when the words pin down exactly ONE product. Just "
        "'sariya'/'saria/सरिया'/'bar' -> family tmt, sku_id null (size unknown). Just "
        "'cement'/'सीमेंट' -> family cement, sku_id null (type unknown). Just "
        "'tiles'/'टाइल' -> family tiles, sku_id null (type unknown). 'barah mm "
        "sariya'/'tata 12' -> TMT_12_FE500D_TATA. 'solah mm' -> TMT_16_FE500D_TATA. "
        "'ultratech opc'/'53 wala' -> CEM_ULTRATECH_OPC53. 'ppc' -> CEM_ULTRATECH_PPC. "
        "'ceramic'/'floor tile' -> TILE_KAJARIA_CERAMIC_2X2. 'vitrified' -> "
        "TILE_KAJARIA_VITRIFIED_600.\n"
        "- Anything the shop does NOT stock (wire, sand, bricks, pipe, paint, "
        "fittings, plywood, marble): in_catalogue=false, sku_id null, family null. "
        "NEVER substitute.\n"
        "- qty = number of units (ton/bori/piece). A size like '12mm'/'barah mm' is "
        "NOT a quantity.\n"
        "- rate: only if a price was actually spoken (rupaye/rs/@). rate_unit is "
        "the unit the price applies to ('₹65 per kg' -> kg, '₹1000 per tonne' -> "
        "tonne). If no separate price unit was spoken, use the item's quantity "
        "unit. payment: only if spoken (cash/nagad->cash; "
        "udhaar/credit/baaki->credit). Else null.\n"
        "- stock question ('kitna bacha/stock hai') -> intent stock_query. "
        "Margin/cash/udhaar/frozen/inventory question -> analytics_query + metric. "
        "A whole-day wrap-up ('aaj ka hisaab/summary', 'how did today go') -> "
        "metric day_summary; a whole-week one ('hafte ka hisaab', 'weekly "
        "report') -> metric week_summary.\n"
        "- Greetings, small talk, thanks, or anything not about the shop's sale/"
        "purchase/stock/money -> intent=chitchat, with a real warm reply (never "
        "the literal words 'chitchat' or a business rejection). Only fall back "
        "to intent=unknown when the owner is CLEARLY trying to transact but you "
        "genuinely cannot tell what.\n"
        "- intent=delivery when the owner RECEIVED/BOUGHT stock: "
        "'kharida/khareeda/khareedi/खरीदा/खरीदी', 'hamne liya', 'mangwaya', 'aaya', "
        "'delivery aayi', OR in English 'bought', 'buy', 'purchased', 'received', "
        "'got', 'arrived', 'stocked in'. intent=sale when the owner "
        "SOLD/GAVE stock to a customer: "
        "'becha/bechi/bech diya/बेचा/बेची', 'diya', 'bika', OR in English 'sold', "
        "'sell', 'gave'. Do not confuse the two — 'kharida'/'bought' is ALWAYS "
        "delivery, never sale.\n"
        "- intent MUST be exactly one of: sale, delivery, count, stock_query, "
        "analytics_query, chitchat, unknown. Never invent another word.\n"
        "- Think briefly, then output the JSON."
    )


_VALID_INTENTS = {"sale", "delivery", "count", "stock_query", "analytics_query",
                  "chitchat", "unknown"}

# cash/udhaar is a fixed two-word vocabulary, unlike product/qty/price — the
# LLM has been observed to miss an explicit "udhaar mein becha" and ask "cash
# ya udhaar?" again. Detect it deterministically from the transcript instead
# of trusting the extractor's json field, same reasoning as _quick_route().
_CREDIT_RE = re.compile(r"udhaar|udhar|credit|baaki|khaate|उधार|बाकी|खाते")
_CASH_RE = re.compile(r"\bcash\b|nagad|कैश|नकद")


def _detect_payment(text: str):
    t = _fold(text)
    if _CREDIT_RE.search(t):
        return "credit"
    if _CASH_RE.search(t):
        return "cash"
    return None


_DELIVERY_INTENT_RE = re.compile(
    r"\b(?:kharid(?:a|i|e)?|khareed(?:a|i|e)?|mangwaya|delivery|aaya|liya|"
    r"bought|buy|buying|purchase[ds]?|received?|receiving|got|arrived|"
    r"stock(?:ed)?\s*in|came\s*in)\b"
    r"|खरीद[ाीे]?|मंगवाया|आया|लिया",
    re.I,
)
_SALE_INTENT_RE = re.compile(
    r"\b(?:bech(?:a|i|ee|e|na)?|bik(?:a|i|ee|e)?|sold|sale|sell(?:ing)?|diya|"
    r"gave|given)\b"
    r"|बेच[ाीे]?|बिक[ाीे]?|दिया",
    re.I,
)
_COUNT_INTENT_RE = re.compile(
    r"\b(?:count|ginti|gina|stock|opening)\b|गिनती|गिना|स्टॉक",
    re.I,
)


def _detect_transaction_intent(text: str) -> str:
    """Detect explicit transaction verbs before asking the model."""
    text = _fold(text)
    if _DELIVERY_INTENT_RE.search(text):
        return "delivery"
    if _SALE_INTENT_RE.search(text):
        return "sale"
    if _COUNT_INTENT_RE.search(text):
        return "count"
    return "unknown"


def _clean_unavailable_name(text: str) -> str:
    """Reduce a rejected utterance fragment to a useful product label."""
    original = (text or "").strip()
    cleaned = _DELIVERY_INTENT_RE.sub(" ", original.lower())
    cleaned = _SALE_INTENT_RE.sub(" ", cleaned)
    cleaned = re.sub(
        r"\b(?:hai|hain|tha|thi|cash|nagad|udhaar|credit|aur|mein|ke|liye|"
        r"laga|aaya|liya|mila|per|kitne|rupaye|aaj|kal|humne|hamne|maine|"
        r"hum|main|apne|today|yesterday|"
        r"i|we|you|the|a|an|of|for|in|and|rupees?|rs)\b"
        r"|है|हैं|था|थी|नकद|उधार|और|में|रुपये|आज|कल|हमने|हमन|मैंने|मैने|हम|अपने",
        " ",
        cleaned,
        flags=re.I,
    )
    number_words = set(nlp.ONES)
    unit_words = set(nlp.UNIT_WORDS)
    words = []
    # \w excludes Devanagari vowel signs and the nukta (Unicode category Mn),
    # so a bare \w+ silently truncates "लकड़ी" to "लकड" and "हमने" to "हमन".
    # Spell the Devanagari block out alongside it.
    for word in re.findall(r"[\w.ऀ-ॿ]+", cleaned, flags=re.UNICODE):
        if re.fullmatch(r"\d+(?:\.\d+)?", word):
            continue
        if word in number_words or word in unit_words:
            continue
        words.append(word)
    return " ".join(words).strip() or original


def _extract(state, repo) -> dict:
    joined = " . ".join(s for s in state["said"] if s)
    segmented = _fallback_extract(joined, repo)

    # A fully out-of-catalogue order needs no model call. The catalogue is an
    # allow-list, so "5 tiles" can be rejected immediately and can never be
    # semantically remapped to the nearest stocked SKU.
    if segmented["items"] and all(
            not _catalogue_evidence(row.get("name", ""), repo)["in_catalogue"]
            for row in segmented["items"]):
        return segmented

    history = state.get("history") or [{"role": "user", "content": joined}]
    messages = [{"role": "system", "content": _extract_prompt(repo)}, *history]
    out = {}
    try:
        # sarvam-30b is a reasoning model: on this prompt it routinely spends
        # 3000+ tokens on internal reasoning_content before writing the JSON
        # answer. max_tokens=1200/timeout=18 (a prior latency attempt) cut it
        # off on EVERY call — finish_reason="length", content=None — silently
        # falling back to the much cruder regex parser below on every single
        # turn. 4096 is this tier's actual cap; give it the room it needs.
        out = sarvam_client.chat_json(messages, temperature=0.1,
                                      reasoning_effort=_EFFORT, max_tokens=4096,
                                      timeout=75)
    except Exception:
        out = {}
    if not isinstance(out, dict) or "items" not in out:
        return _fallback_extract(joined, repo)
    if not isinstance(out, dict):
        out = {}
    payment = _detect_payment(joined)
    items = []
    model_rows = out.get("items", []) or []
    for idx, r in enumerate(model_rows):
        if not isinstance(r, dict):
            continue
        fam = r.get("family") if r.get("family") in _FAMILIES else None
        unit = (r.get("unit") or "").strip().lower() or None
        if unit:
            unit = M.UNIT_WORDS.get(unit, unit)
        # Positional reconciliation is safe only when both extractors found
        # the same number of spoken items. Otherwise an omitted separator can
        # shift every later product onto the wrong model row.
        aligned = len(model_rows) == len(segmented["items"])
        local = segmented["items"][idx] if aligned and idx < len(segmented["items"]) else None
        source_phrase = ((local or {}).get("name") or
                         (joined if len(model_rows) == 1 else r.get("name")) or "")
        evidence = _catalogue_evidence(source_phrase, repo)
        name = source_phrase
        name_l = name.lower()
        stocked_family = ("cement" if re.search(r"cement|सीमेंट", name_l)
                          else "tmt" if re.search(r"sari?ya|sariya|सरिया|tmt", name_l)
                          else None)
        candidate_ids = evidence["candidate_ids"]
        proposed_sid = r.get("sku_id") if _valid_sku(r.get("sku_id"), repo) else None
        # A generic stocked family is ambiguous, not a licence for the model to
        # choose a variant. Only a single deterministic candidate may auto-pin.
        sid = candidate_ids[0] if len(candidate_ids) == 1 else None
        if proposed_sid in candidate_ids and len(candidate_ids) == 1:
            sid = proposed_sid
        fam = evidence.get("family") or stocked_family or fam
        if not evidence["in_catalogue"]:
            sid = None
            fam = None
        items.append({
            "sku_id": sid,
            "family": fam, "name": name,
            "in_catalogue": evidence["in_catalogue"],
            "qty": (r.get("qty") if isinstance(r.get("qty"), (int, float))
                    else (local or {}).get("qty")),
            "unit": unit or (local or {}).get("unit"),
            "rate": r.get("rate") if isinstance(r.get("rate"), (int, float)) else None,
            "rate_unit": M.UNIT_WORDS.get(
                str(r.get("rate_unit") or "").strip().lower(),
                str(r.get("rate_unit") or "").strip().lower()) or None,
            "payment": payment or (r.get("payment")
                                   if r.get("payment") in ("cash", "credit") else None),
        })
    raw_intent = out.get("intent")
    intent = raw_intent if raw_intent in _VALID_INTENTS else "unknown"
    # Explicit transaction words in the owner's sentence are authoritative.
    # Do not ask "becha ya khareeda?" merely because the model omitted an
    # intent that the deterministic parser already found.
    if segmented.get("intent") in ("sale", "delivery", "count"):
        intent = segmented["intent"]
    # Never let the model collapse an explicit "X aur Y" order into one item.
    # The local segment parser is authoritative for item count; the LLM remains
    # authoritative for richer attributes when it found every spoken segment.
    if len(segmented["items"]) > len(items):
        items = segmented["items"]
    reply = out.get("reply")
    return {"intent": intent, "metric": out.get("metric"),
            "reply": reply if isinstance(reply, str) else None, "items": items}


def _fallback_extract(text: str, repo) -> dict:
    """Fast local fallback when the model times out or returns malformed JSON."""
    t = (text or "").lower()
    intent = _detect_transaction_intent(t)
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
    return {"intent": intent, "metric": None, "reply": None, "items": items}


def _valid_sku(sid, repo) -> bool:
    return bool(sid) and repo.sku(sid) is not None


_KNOWN_OUTSIDE_PRODUCTS_RE = re.compile(
    r"\b(?:wire|sand|balu|bricks?|eent|pipes?|paints?|fittings?|"
    r"plywood|marble|granite|stones?)\b|"
    r"वायर|तार|रेत|बालू|ईंट|पाइप|पेंट|फिटिंग|प्लाईवुड|मार्बल|ग्रेनाइट|पत्थर"
)
_FAMILY_HINTS = {
    "tmt": re.compile(r"\bsari?ya\b|\bsaria\b|\btmt\b|\brods?\b|\bbars?\b|\bsteel\b|सरिया|स्टील"),
    "cement": re.compile(r"\bcement\b|सीमेंट"),
    "tiles": re.compile(r"\btiles?\b|\bvitrified\b|\bceramic\b|\bkajaria\b|टाइल"),
}


def _catalogue_evidence(phrase: str, repo) -> dict:
    """Deterministic catalogue allow-list built from the actual SKU aliases.

    The LLM may extract quantities and intent, but it may not invent catalogue
    membership. Known outside products win even if a unit such as "bori" could
    otherwise resemble a cement alias (for example "5 bori tiles").
    """
    raw = (phrase or "").lower()
    if _KNOWN_OUTSIDE_PRODUCTS_RE.search(raw):
        return {"in_catalogue": False, "candidate_ids": [], "family": None}

    catalogue = repo.load_catalogue()
    alias_idx = M.build_alias_index(catalogue, repo.load_learning())
    norm = M.normalize(raw)
    padded = f" {norm} "
    matched_sets = []
    for alias, ids in alias_idx.items():
        a = M.normalize(alias)
        if a and f" {a} " in padded:
            matched_sets.append(set(ids))

    candidates = set()
    if matched_sets:
        candidates = set.intersection(*matched_sets)
        if not candidates:
            candidates = set.union(*matched_sets)

    hinted_families = {family for family, pat in _FAMILY_HINTS.items()
                       if pat.search(raw)}
    if not candidates and hinted_families:
        candidates = {s["sku_id"] for s in catalogue
                      if s.get("family") in hinted_families}

    attrs = M.extract_attrs(raw)
    if not candidates and ("diameter_mm" in attrs or "grade" in attrs):
        candidates = {s["sku_id"] for s in catalogue if s.get("family") == "tmt"}
    for key, value in attrs.items():
        narrowed = {sid for sid in candidates
                    if (repo.sku(sid) or {}).get("attributes", {}).get(key) == value}
        if narrowed:
            candidates = narrowed

    families = {(repo.sku(sid) or {}).get("family") for sid in candidates}
    families.discard(None)
    return {
        "in_catalogue": bool(candidates),
        "candidate_ids": sorted(candidates),
        "family": next(iter(families)) if len(families) == 1 else None,
    }


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
# "Which period" and "is this a summary at all" are two independent questions,
# and pairing them into one regex is what made "saptah ka summary" fall through
# to the DAY branch: the week alternative only fired when a summary word sat
# right next to the week word. Match them separately instead — any summary
# request is weekly the moment a week word appears anywhere in the sentence.
_SUMMARY_RE = re.compile(
    r"\b(?:summary|sumary|summry|report|recap|overview|hisaab|hisab|"
    r"hisab\s*kitab|band\s*karo|kaisa\s*raha|kaisi\s*rahi|kaisa\s*gaya|"
    r"how\s*(?:did|was)|business\s*kaisa|kya\s*hua)\b"
    r"|सारांश|समरी|रिपोर्ट|हिसाब|कैसा\s*रहा|कैसी\s*रही|ब्यौरा|लेखा",
    re.I,
)
_WEEK_RE = re.compile(
    r"\b(?:haft[ae]|hafte|hafta|saptah|week(?:ly)?|saat\s*din|7\s*din|"
    r"seven\s*days?|last\s*7)\b|हफ्त[ेाो]|सप्ताह|साप्ताहिक|सात\s*दिन",
    re.I,
)


def _summary_metric(text: str) -> str:
    return "week_summary" if _WEEK_RE.search(_fold(text)) else "day_summary"


_QUICK_PATTERNS = [
    # Summaries are checked before the single-metric patterns below, or
    # "hafte ka margin" would answer with just today's margin line.
    ("_summary", _SUMMARY_RE),
    ("frozen", re.compile(r"nahi\s*bika|bika\s*nahi|not\s+sold|unsold|frozen|"
                          r"phasa|dead\s*stock|purana\s*maal|move\w*\s*nahi|"
                          r"kaun\s*sa\s*(?:saamaan|samaan|maal)|"
                          r"नहीं\s*बिका|बिका\s*नहीं|फ्रोजन|फ[ँं]?सा|"
                          r"पुराना\s*माल|कौन\s*सा\s*(?:सामान|माल)")),
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
    t = _fold(text)
    for metric, pat in _QUICK_PATTERNS:
        if pat.search(t):
            if metric == "_summary":
                return ("analytics", _summary_metric(t))
            # margin and cash are the two day-scoped numbers, so "pichhle
            # hafte ka margin" wants the weekly roll-up, not today's line.
            # frozen/inventory/udhaar are point-in-time balances — a week
            # word doesn't change what they mean.
            if metric in ("margin", "cash") and _WEEK_RE.search(t):
                return ("analytics", "week_summary")
            return ("analytics", metric)
    if _STOCK_QUERY_RE.search(t):
        return ("stock", None)
    return None


def converse(state, user_text, flow, repo):
    if not state or "said" not in state:
        state = {"flow": flow, "said": [], "history": []}

    state.setdefault("history", [])
    # A forced reply language from Settings overrides detection outright —
    # an owner who picked "always Hindi" means it even when they type a name
    # or a phone number that looks like plain English.
    forced = _config(repo).get("reply_language")
    if forced in ("hi", "en"):
        state["lang"] = forced
    elif not state.get("lang"):
        detected = _detect_lang(user_text)
        if detected:
            state["lang"] = detected
    if not state.get("original_transcript") and user_text:
        # Keep the exact first sentence alongside the structured draft through
        # product/rate/customer follow-ups, confirmation, and bill creation.
        state["original_transcript"] = user_text

    # Follow-up slots are applied to the structured draft already extracted on
    # the first turn. This preserves the complete order and avoids another
    # 30-second model call for simple answers such as "12 mm", "50", or "cash".
    aw = state.get("awaiting")
    if aw in ("customer_phone", "customer_name", "customer_choice", "deadline"):
        state["history"].append({"role": "user", "content": user_text or ""})
        return _apply_customer_slot(state, aw, user_text, repo)
    if aw == "confirmation":
        state["history"].append({"role": "user", "content": user_text or ""})
        if re.search(r"confirm|haan|ha|yes|theek|ठीक|हाँ", _fold(user_text)):
            state["awaiting"] = None
            pc = state.pop("pending_commit")
            return _commit(state, pc["flow"], pc["items"], pc["skipped"], repo)
        return _prepare_confirmation(state, repo)
    if state.get("awaiting_order"):
        state["history"].append({"role": "user", "content": user_text or ""})
        return _apply_order_slot(state, user_text, repo)

    # Voice Entry is a sale-capable screen, but it is also where the owner asks
    # stock and business questions. Route an unambiguous first-turn query before
    # the screen's `live_sale` hint can force it into an order. Analytics are
    # fully ledger-derived and do not need an LLM or API key.
    if not state.get("locked_intent"):
        quick = _quick_route(user_text)
        if quick and quick[0] == "analytics":
            return _analytics_answer(quick[1], repo, state)
        if quick == ("stock", None) and sarvam_client.has_key():
            state["said"].append(user_text or "")
            state["history"].append({"role": "user", "content": user_text or ""})
            return _stock_flow(state, _extract(state, repo)["items"], repo)

    if not sarvam_client.has_key():
        return _reply(_L(state, "Voice ke liye Sarvam API key chahiye.",
                         "A Sarvam API key is needed for voice."),
                      listen=False, done=True)

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

    if intent == "chitchat":
        # The model writes its own natural reply here — small talk has no
        # fixed vocabulary to pattern-match, unlike payment/analytics
        # keywords, so this is the one case genuinely left to the LLM.
        return _reply(ext.get("reply") or _L(state, "Namaste!", "Hi there!"),
                      listen=True, done=False, state=state)
    if intent == "analytics_query":
        metric = ext.get("metric")
        # The extractor has no reliable sense of period, and its fallback for a
        # summary-shaped question is "margin" — which answers with today's
        # numbers. An explicit week word in the transcript overrules it.
        if _WEEK_RE.search(_fold(user_text)) and metric in (
                None, "margin", "day_summary", "week_summary"):
            metric = "week_summary"
        elif metric == "day_summary" or (metric in (None, "margin")
                                         and _SUMMARY_RE.search(_fold(user_text))):
            metric = "day_summary"
        return _analytics_answer(metric, repo, state)
    if intent == "stock_query":
        return _stock_flow(state, ext["items"], repo)
    if intent == "unknown" and not ext["items"]:
        return _say(state, _L(state,
                              "Ye samajh nahi aaya — sariya, cement, ya tiles mein se "
                              "kya chahiye?",
                              "Didn't quite catch that — do you need TMT bars, "
                              "cement, or tiles?"),
                    listen=True, done=False)
    state["draft_items"] = ext["items"]
    return _order_flow(state, intent, ext["items"], repo)


# ---------------------------------------------------------------------------
# Sale / delivery / count
# ---------------------------------------------------------------------------
def _provision_new_sku(item, family, repo, create=True):
    """A delivery/count of a sariya size or cement type the shop doesn't
    stock yet EXTENDS the catalogue instead of being rejected or stuck asking
    a disambiguation that only ever offers the old sizes/types. We can never
    sell what isn't in the catalogue, so this only ever runs for
    delivery/count — never for a sale. Returns the sku_id, or None if the
    name didn't carry enough (e.g. no diameter/type spoken at all)."""
    name = item.get("name") or ""
    attrs = M.extract_attrs(name)
    catalogue = repo.load_catalogue()

    if family == "tmt":
        dia = attrs.get("diameter_mm")
        if not dia:
            return None
        existing = next((s for s in catalogue if s.get("family") == "tmt"
                         and s.get("attributes", {}).get("diameter_mm") == dia), None)
        if existing:
            return existing["sku_id"]
        if not create:
            return None
        grade = attrs.get("grade") or "Fe500D"
        brand = attrs.get("brand") or "Tata Tiscon"
        sku_id = f"TMT_{dia}_{grade.upper()}_{brand.split()[0].upper()}"
        tmt_costs = [s["opening_cost_per_kg"] for s in catalogue if s.get("family") == "tmt"]
        cost = round(sum(tmt_costs) / len(tmt_costs), 1) if tmt_costs else 55.0
        repo.upsert_sku({
            "sku_id": sku_id,
            "canonical": f"{brand} TMT Bar {dia}mm {grade}",
            "family": "tmt",
            "attributes": {"diameter_mm": dia, "grade": grade, "brand": brand},
            "default_unit": "tonne",
            "units": {"kg": 1, "tonne": 1000, "piece": round((dia * dia) / 162.0 * 12.0, 2)},
            "gst_rate": 18,
            "opening_cost_per_kg": cost,
            "aliases": ["saria", "sariya", "सरिया", "tmt bar", "rod", "tmt",
                        f"{dia}mm", f"{dia} mm", brand.split()[0].lower()],
        })
        return sku_id

    if family == "cement":
        typ = attrs.get("type")
        if not typ:
            m = re.search(r"\b(opc\s*\d{2}|ppc|psc|src|pcc)\b", name.lower())
            typ = m.group(1).upper() if m else None
        if not typ:
            return None
        existing = next((s for s in catalogue if s.get("family") == "cement"
                         and (s.get("attributes", {}).get("type") or "").upper()
                         == typ.upper()), None)
        if existing:
            return existing["sku_id"]
        if not create:
            return None
        brand = attrs.get("brand") or "UltraTech"
        sku_id = f"CEM_{brand.split()[0].upper()}_{typ.upper().replace(' ', '')}"
        cement_costs = [s["opening_cost_per_kg"] for s in catalogue if s.get("family") == "cement"]
        cost = round(sum(cement_costs) / len(cement_costs), 1) if cement_costs else 400.0
        repo.upsert_sku({
            "sku_id": sku_id,
            "canonical": f"{brand} {typ} Cement 50kg",
            "family": "cement",
            "attributes": {"brand": brand, "type": typ, "weight_kg": 50},
            "default_unit": "bori",
            "units": {"bori": 1, "tonne": 20, "kg": 0.02},
            "gst_rate": 28,
            "opening_cost_per_kg": cost,
            "aliases": ["cement", "सीमेंट", "bori", "bag", brand.lower(), typ.lower()],
        })
        return sku_id

    return None


# A reference list of common hardware/construction categories the shop
# doesn't currently stock any variant of. When one of these is bought, we
# know enough about the category to ask a SENSIBLE follow-up (brand + type)
# instead of silently creating a vague catalogue entry from whatever bare
# word was said ("pipe" -> a real "OTHER_PIPE" with no brand/size on file).
_HARDWARE_TAXONOMY = {
    "pipe": re.compile(r"\bpipes?\b|पाइप"),
    "paint": re.compile(r"\bpaints?\b|पेंट"),
    "wire": re.compile(r"\bwires?\b|\bcables?\b|तार"),
    "brick": re.compile(r"\bbricks?\b|ईंट"),
    "sand": re.compile(r"\bsand\b|\bbalu\b|रेत|बालू"),
    "plywood": re.compile(r"\bplywood\b|प्लाईवुड"),
    "marble/granite": re.compile(r"\bmarble\b|\bgranite\b|मार्बल|ग्रेनाइट"),
    "fastener": re.compile(r"\bnuts?\b|\bbolts?\b|\bscrews?\b|\bwashers?\b"),
    "sanitaryware": re.compile(r"\btoilet\b|\bwashbasin\b|\bcommode\b|\bsanitary"),
    "lock": re.compile(r"\blocks?\b|ताला"),
    "switch/socket": re.compile(r"\bswitch(?:es)?\b|\bsockets?\b"),
    "adhesive": re.compile(r"\badhesive\b|\bfevicol\b|tile.?fix"),
    "tool": re.compile(r"\bhammer\b|\bdrill\b|\bwrench\b|\bscrewdriver\b"),
    "roofing sheet": re.compile(r"\broofing\b|\btin sheet\b|\basbestos\b"),
    "fitting": re.compile(r"\bfittings?\b|फिटिंग"),
}


def _match_hardware_category(name: str):
    n = (name or "").lower()
    for cat, pat in _HARDWARE_TAXONOMY.items():
        if pat.search(n):
            return cat
    return None


def _has_brand_or_type(name: str) -> bool:
    """Rough heuristic: more than the bare category word(s) were said, e.g.
    'Havells 2.5mm copper wire' vs just 'wire'. `name` may be the whole
    utterance ("I bought 20 pipes"), not just the product phrase, so clean
    out transaction verbs/fillers/qty/units first — otherwise ordinary
    sentence words ("I", "bought") get counted as if they were brand/type."""
    cleaned = _clean_unavailable_name(name)
    words = re.findall(r"[a-zA-Zऀ-ॿ]+", cleaned)
    return len(words) >= 2


def _provision_generic_sku(item, repo):
    """A delivery/count of a product OUTSIDE tmt/cement — tiles, pipe, paint,
    whatever the owner actually brought in — still extends the catalogue.
    The owner decides what the shop carries by what they buy; a hardcoded
    category list shouldn't silently refuse to record it. Only ever runs for
    delivery/count, never sale (nothing to sell that was never bought)."""
    label = _clean_unavailable_name(item.get("name") or "").strip()
    if not label or len(label) < 2:
        return None
    catalogue = repo.load_catalogue()
    existing = next((s for s in catalogue
                     if s.get("canonical", "").lower() == label.lower()), None)
    if existing:
        return existing["sku_id"]
    slug = re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_") or "ITEM"
    sku_id, n = f"OTHER_{slug}"[:60], 1
    while repo.sku(sku_id):
        n += 1
        sku_id = f"OTHER_{slug}_{n}"[:60]
    unit = item.get("unit") or "unit"
    repo.upsert_sku({
        "sku_id": sku_id,
        "canonical": label.title(),
        "family": "other",
        "attributes": {},
        "default_unit": unit,
        "units": {unit: 1},
        "gst_rate": 18,
        "opening_cost_per_kg": 0,
        "aliases": [label.lower()],
    })
    return sku_id


def _names_a_new_variant(item, family) -> bool:
    """Did the owner actually name a NEW variant of a family we stock ('25mm
    sariya', 'PSC cement'), or just say the family word ('cement')? Only the
    former is a candidate for a new catalogue entry; the latter needs the
    ordinary which-one question."""
    name = item.get("name") or ""
    attrs = M.extract_attrs(name)
    if family == "tmt":
        return bool(attrs.get("diameter_mm"))
    if family == "cement":
        return bool(attrs.get("type")
                    or re.search(r"\b(opc\s*\d{2}|ppc|psc|src|pcc)\b", name.lower()))
    if family == "tiles":
        return bool(re.search(r"\b(ceramic|vitrified|marble|granite|wall|floor|"
                              r"\d{3,4}\s*x\s*\d{3,4}|\dx\d)\b", name.lower()))
    return True


def _sku_from_learned_alias(name, repo):
    """Resolve a phrase the shop has TAUGHT us — 'mota sariya', 'patla rod',
    'PPC cement' — straight to its SKU, so a shop 60 days in stops being asked
    "kaunsa sariya?" for a phrase it has already confirmed dozens of times and
    moves on to rate/quantity. Only whole-phrase and multiword alias hits count:
    the single-token scan in matcher.match() is what turns a bare 'cement' into
    a coin flip between OPC and PPC, and that genuinely does need the question."""
    norm = M.normalize(name or "")
    if not norm:
        return None
    idx = M.build_alias_index(repo.load_catalogue(), repo.load_learning())
    ids = idx.get(norm)
    if ids is None:
        padded = f" {norm} "
        for alias in sorted((a for a in idx if " " in a), key=len, reverse=True):
            if f" {alias} " in padded:
                ids = idx[alias]
                break
    uniq = list(dict.fromkeys(ids or []))
    return uniq[0] if len(uniq) == 1 and repo.sku(uniq[0]) else None


def _ask_product(item, state=None):
    fam = item.get("family")
    if fam == "tmt":
        return _L(state, "Kaunsa sariya — barah mm ya solah mm?",
                  "Which size TMT bar — 12mm or 16mm?")
    if fam == "cement":
        return _L(state, "Kaunsa cement — OPC 53 ya PPC?",
                  "Which cement — OPC 53 or PPC?")
    if fam == "tiles":
        return _L(state, "Kaunsi tile — ceramic floor ya vitrified?",
                  "Which tile — ceramic floor or vitrified?")
    return _L(state, "Kaunsa maal — sariya, cement, ya tiles? Thoda detail se boliye.",
              "Which product — TMT bar, cement, or tiles? A bit more detail please.")


def _ask_order_slot(state, item_index, slot, text):
    state["awaiting_order"] = {"item_index": item_index, "slot": slot}
    return _say(state, text, listen=True, done=False)


def _answer_number(text):
    value, _ = nlp._find_qty((text or "").lower())
    if value is not None:
        return float(value)
    return _number_in_text(text)


def _sku_from_answer(text, family, repo):
    t = _fold(text)
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
    if family == "tiles" or re.search(r"tiles?|टाइल", t):
        if re.search(r"vitrified|600", t):
            direct = "TILE_KAJARIA_VITRIFIED_600"
        elif re.search(r"ceramic|floor|2x2", t):
            direct = "TILE_KAJARIA_CERAMIC_2X2"
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
        return _say(state, _L(state,
                              "Order ka context kho gaya — poori entry ek baar phir boliye.",
                              "Lost track of the order — please say the whole entry again."),
                    listen=True, done=False)
    item = items[idx]
    if slot == "product":
        sid = _sku_from_answer(user_text, item.get("family"), repo)
        if not sid:
            return _ask_order_slot(state, idx, slot, _ask_product(item, state))
        item["sku_id"] = sid
        item["family"] = repo.sku(sid).get("family")
    elif slot == "qty":
        qty = _answer_number(user_text)
        if qty is None or qty <= 0:
            return _ask_order_slot(state, idx, slot,
                                   _L(state, "Quantity samajh nahi aayi — kitna?",
                                      "Didn't catch the quantity — how much?"))
        item["qty"] = qty
        unit = nlp._find_unit((user_text or "").lower())
        if unit:
            item["unit"] = unit
    elif slot == "rate":
        rate = _answer_number(user_text)
        if rate is None or rate <= 0:
            return _ask_order_slot(state, idx, slot,
                                   _L(state, "Rate number mein bataiye.",
                                      "Please give the rate as a number."))
        item["rate"] = rate
        # The question names the item's unit ("per tonne", "per bori", etc.).
        # Respect a different unit only when the owner explicitly says one.
        item["rate_unit"] = (
            nlp._find_unit((user_text or "").lower())
            or item.get("unit")
            or (repo.sku(item.get("sku_id")) or {}).get("default_unit")
        )
    elif slot == "payment":
        payment = _detect_payment(user_text)
        if not payment:
            return _ask_order_slot(state, idx, slot,
                                   _L(state, "Payment cash hai ya udhaar?",
                                      "Is the payment cash or credit?"))
        for row in items:
            if not row.get("payment"):
                row["payment"] = payment
    elif slot == "intent":
        detected = _detect_transaction_intent(user_text)
        if detected in ("sale", "delivery", "count"):
            state["locked_intent"] = detected
        else:
            return _ask_order_slot(state, idx, slot,
                                   _L(state, "Ye maal becha tha ya khareeda?",
                                      "Was this sold or bought?"))
    elif slot in ("confirm_add", "verify_new"):
        answer = _yes_no(user_text)
        if answer is None:
            return _ask_order_slot(
                state, idx, slot,
                _L(state, "Haan ya na mein bataiye — add karoon?",
                   "Just yes or no — should I add it?"))
        if answer:
            item["_confirmed_add"] = True
        else:
            # Declined: leave the line out of the catalogue entirely. Marking
            # it routes the line through the existing "skipped" machinery,
            # which tells the owner it wasn't added.
            item["_declined"] = True
            item["in_catalogue"] = False
            item["sku_id"] = None
    elif slot == "brand_type":
        extra = (user_text or "").strip()
        if not extra:
            return _ask_order_slot(state, idx, slot,
                                   _L(state, "Brand aur type bataiye.",
                                      "Please tell me the brand and type."))
        # Fold the answer into the item's name so provisioning (retried by
        # falling back into _order_flow below) picks up the real brand/type
        # instead of a bare category placeholder.
        item["name"] = f"{item.get('name', '')} {extra}".strip()
    state["draft_items"] = items
    return _order_flow(state, state.get("locked_intent") or state.get("flow"),
                       items, repo)


def _order_flow(state, intent, items, repo):
    if intent not in ("sale", "delivery", "count"):
        # Never guess bought-vs-sold when the extractor couldn't classify it —
        # writing the wrong direction silently corrupts stock.
        state["draft_items"] = items
        return _ask_order_slot(state, 0, "intent",
                               _L(state, "Ye maal aapne becha ya khareeda?",
                                  "Was this sold or bought?"))
    flow = intent  # sale | delivery | count
    state["locked_intent"] = flow
    # Anything the shop doesn't carry yet gets offered as a new catalogue
    # entry right here instead of being rejected outright with "hum nahi
    # rakhte". This runs for sales too: the owner selling something is just as
    # good evidence that the shop stocks it as a delivery is, and refusing the
    # line loses the sale entirely. The resulting SKU simply starts uncounted,
    # which the ledger already reports honestly until a stock count lands.
    newly_added = []
    if flow in ("sale", "delivery", "count"):
        stocked_families = {s.get("family") for s in repo.load_catalogue()}
        for idx, it in enumerate(items):
            if it.get("sku_id") or it.get("_declined"):
                continue
            fam = it.get("family")
            name_l = (it.get("name") or "").lower()
            if not fam:
                fam = ("cement" if re.search(r"cement|सीमेंट", name_l)
                       else "tmt" if re.search(r"sari?ya|सरिया|\btmt\b|\bbars?\b", name_l)
                       else "tiles" if re.search(r"tiles?|टाइल", name_l)
                       else None)
            # An existing SKU is not a new item — resolve it silently and never
            # ask "should I add this?" for stock the shop already carries.
            existing = (_provision_new_sku(it, fam, repo, create=False)
                        if fam in ("tmt", "cement") else None)
            existing = existing or _sku_from_learned_alias(it.get("name"), repo)
            if existing:
                it["sku_id"] = existing
                it["family"] = repo.sku(existing).get("family")
                it["in_catalogue"] = True
                continue

            # A stocked family named without anything to tell its variants
            # apart ("cement", "sariya") is an AMBIGUOUS reference, not a new
            # product. Leave it for the disambiguation question below — asking
            # "should I add cement?" when two cements are on the shelf is
            # nonsense.
            if fam in stocked_families and not _names_a_new_variant(it, fam):
                continue

            label = _clean_unavailable_name(it.get("name")) or (it.get("name") or "")
            category = _match_hardware_category(it.get("name"))
            known_kind = category or fam in ("tmt", "cement", "tiles")
            ask_first = _config(repo).get("confirm_new_items", True)
            if ask_first and not it.get("_confirmed_add"):
                state["draft_items"] = items
                if known_kind:
                    # Recognised kind of goods, just not stocked yet — a plain
                    # "add it?" is enough.
                    return _ask_order_slot(
                        state, idx, "confirm_add",
                        _L(state,
                           f"{label} inventory mein nahi hai — naya item add kar doon?",
                           f"{label} isn't in inventory — should I add it as a "
                           "new item?"))
                # Not in the catalogue AND not a hardware category we know of.
                # That is far more often a misheard word than a real new brand,
                # so make the owner say so before it becomes a permanent SKU.
                return _ask_order_slot(
                    state, idx, "verify_new",
                    _L(state,
                       f"{label} na inventory mein hai na kisi jaani-pehchaani "
                       "category mein — ye sach mein naya brand ya item hai, ya "
                       "galti se bol diya?",
                       f"{label} is neither in inventory nor a hardware category "
                       "I know — is that genuinely a new brand or item, or was it "
                       "said by mistake?"))
            if category and not _has_brand_or_type(it.get("name")):
                state["draft_items"] = items
                return _ask_order_slot(
                    state, idx, "brand_type",
                    _L(state, f"{category} ke liye kaunsa brand aur type?",
                       f"What brand and type of {category}?"))
            sid = (_provision_new_sku(it, fam, repo) if fam in ("tmt", "cement")
                   else None)
            # Family-specific provisioning needs a recognizable diameter/type;
            # fall back to generic rather than dropping the line.
            sid = sid or _provision_generic_sku(it, repo)
            if sid:
                it["sku_id"] = sid
                it["family"] = fam or repo.sku(sid).get("family")
                it["in_catalogue"] = True
                it["_freshly_provisioned"] = True
                newly_added.append(repo.sku(sid)["canonical"])

    # drop products the shop does not stock, but tell the owner once
    kept = []
    skipped = []
    declined = []
    for it in items:
        if it.get("in_catalogue") is False and not it.get("sku_id"):
            name = _clean_unavailable_name(it.get("name")) or "wo cheez"
            skipped.append(name)
            if it.get("_declined"):
                declined.append(name)
        else:
            kept.append(it)
    if not kept:
        # The owner was ASKED whether to add it and said no. They already know
        # it isn't stocked — repeating that back at them is just argumentative.
        # Acknowledge, write nothing, and end the turn so the screen returns to
        # a ready mic.
        if declined:
            out = _say(state, _L(state, "Theek hai, kuch nahi joda.",
                                 "Alright, nothing was added."),
                       listen=False, done=True)
            out["reset"] = True
            return out
        nm = skipped[0] if skipped else _L(state, "wo cheez", "that item")
        return _say(state, _L(state,
                              f"{nm} hum nahi rakhte — sariya, cement, ya tiles hai. Kuch aur?",
                              f"We don't stock {nm} — only TMT bars, cement, or tiles. "
                              "Anything else?"),
                    listen=True, done=False)

    # Keep rejected product names across all follow-up turns. Previously this
    # local list disappeared as soon as we asked the first rate/product
    # question, so a mixed order silently skipped the unavailable line.
    remembered_skipped = state.setdefault("skipped_items", [])
    newly_skipped = []
    for name in skipped:
        if name not in remembered_skipped:
            remembered_skipped.append(name)
            # Only announce lines the owner never got a say on. One they just
            # declined needs no explanation back to them.
            if name not in declined:
                newly_skipped.append(name)
    notice = ""
    if newly_added:
        notice += _L(state, f"{_oxford(newly_added)} inventory mein naya add ho gaya. ",
                     f"{_oxford(newly_added, state)} added to inventory as a new item. ")
    if newly_skipped:
        notice += _L(state,
                     f"{_oxford(newly_skipped)} inventory mein nahi hai, isliye add nahi kiya. ",
                     f"{_oxford(newly_skipped, state)} isn't in inventory, so it wasn't added. ")
    if notice:
        state["pending_notice"] = notice

    state["draft_items"] = kept
    # Resolve product identity and quantity for every line first. A delivery/
    # count of a size/type still unresolved here (ambiguous, not merely
    # missing) gets one more provisioning attempt before falling back to
    # asking — otherwise a genuinely new size (e.g. 25mm) loops forever on a
    # disambiguation question that only ever offers the OLD sizes.
    for idx, it in enumerate(kept):
        if not it.get("sku_id"):
            # A phrase the shop has already taught us ("mota sariya", "PPC
            # cement") resolves without another disambiguation question.
            sid = _sku_from_learned_alias(it.get("name"), repo)
            if sid:
                it["sku_id"] = sid
                it["family"] = repo.sku(sid).get("family")
        if not it.get("sku_id") and flow in ("delivery", "count") and it.get("family"):
            sid = _provision_new_sku(it, it["family"], repo)
            if sid:
                it["sku_id"] = sid
        if not it.get("sku_id"):
            return _ask_order_slot(state, idx, "product", _ask_product(it, state))
        sku = repo.sku(it["sku_id"])
        if it.get("qty") in (None, ""):
            return _ask_order_slot(state, idx, "qty",
                                   _L(state, f"{sku['canonical']} — kitna?",
                                      f"{sku['canonical']} — how much?"))
        if not it.get("unit") or it["unit"] not in sku.get("units", {}):
            it["unit"] = sku.get("default_unit")

    # Then collect each line's rate. Payment belongs to the whole sale and is
    # deliberately asked only once after all line items are complete.
    for idx, it in enumerate(kept):
        sku = repo.sku(it["sku_id"])
        if flow in ("sale", "delivery") and it.get("rate") is None:
            per = it["unit"]
            q = _L(state,
                   f"{sku['canonical']} kitne mein aaya, per {per}?" if flow == "delivery"
                   else f"{sku['canonical']} ka rate kya laga, per {per}?",
                   f"What was the rate for {sku['canonical']}, per {per}?" if flow == "delivery"
                   else f"What did {sku['canonical']} sell for, per {per}?")
            return _ask_order_slot(state, idx, "rate", q)
        if flow in ("sale", "delivery") and it.get("rate") is not None:
            it["rate_unit"] = it.get("rate_unit") or it["unit"]

    if flow == "sale" and any(not it.get("payment") for it in kept):
        first_missing = next(i for i, it in enumerate(kept) if not it.get("payment"))
        return _ask_order_slot(state, first_missing, "payment",
                               _L(state, "Is poori sale ka payment cash hai ya udhaar?",
                                  "Is this whole sale cash or credit?"))

    if flow == "sale":
        state["pending_commit"] = {
            "flow": flow, "items": kept,
            "skipped": list(state.get("skipped_items", [])),
        }
        return _resume_customer_capture(state, repo)

    state["pending_commit"] = {
        "flow": flow, "items": kept,
        "skipped": list(state.get("skipped_items", [])),
    }
    return _prepare_confirmation(state, repo)


# ---------------------------------------------------------------------------
# Udhaar customer capture (deterministic data entry, gates the commit)
# ---------------------------------------------------------------------------
def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ ]+", " ", (name or "").lower()).strip()


def _find_customers_by_name(repo, name: str) -> list:
    """Look a spoken customer name up in the CRM. Exact (normalized) hits win
    outright; only if there are none do we widen to token-prefix matches, so
    'Ramesh' finding both 'Ramesh Kumar' and 'Ramesh Traders' asks the owner
    which one instead of guessing."""
    target = _norm_name(name)
    if not target:
        return []
    rows = list(repo.customers())
    exact = [c for c in rows if _norm_name(c.get("name")) == target]
    if exact:
        return exact
    words = target.split()
    partial = []
    for c in rows:
        other = _norm_name(c.get("name"))
        if not other:
            continue
        if all(any(w == o or o.startswith(w) for o in other.split()) for w in words):
            partial.append(c)
    return partial


# "pehla / doosra / teesra" is a closed three-word list, so pick the option
# deterministically rather than paying for an LLM call that could land on the
# wrong customer's ledger.
_ORDINALS = [
    re.compile(r"\b(?:1|pehl[ae]|pahl[ae]|first|ek)\b|पहल[ाे]"),
    re.compile(r"\b(?:2|doosr[ae]|dusr[ae]|second|do)\b|दूसर[ाे]"),
    re.compile(r"\b(?:3|teesr[ae]|tisr[ae]|third|teen)\b|तीसर[ाे]"),
    re.compile(r"\b(?:4|chauth[ae]|fourth|char)\b|चौथ[ाे]"),
]
_YES_RE = re.compile(r"\b(?:haan|han|ha|ji|yes|yeah|yep|ok|okay|theek|thik|sahi|"
                     r"karo|kar\s*do|add|naya|new|bilkul|sure)\b|हाँ|हां|ठीक|सही|नया")
_NO_RE = re.compile(r"\b(?:nahi|nahin|na|no|nope|mat|galti|galat|mistake|wrong|"
                    r"rehne|chhodo|cancel|skip)\b|नहीं|नही|मत|गलत|गलती")


def _yes_no(text: str):
    """Return True/False for a clear yes/no, else None. 'no' is checked first —
    'nahi karo' must never read as the 'karo' yes."""
    t = _fold(text)
    if _NO_RE.search(t):
        return False
    if _YES_RE.search(t):
        return True
    return None


def _clean_spoken_name(text: str) -> str:
    # STT auto-punctuates a spoken utterance ("Ramesh." as a full sentence) —
    # strip trailing punctuation or it doubles up later ("Customer: Ramesh.."
    # from name "Ramesh." + our own period).
    return re.sub(r"[.!?,।]+$", "", (text or "").strip()).strip()


def _apply_customer_slot(state, aw, user_text, repo):
    if aw == "customer_phone":
        phone = parse_phone(user_text)
        if not phone:
            return _say(state, _L(state, "Customer ka 10 digit contact number bataiye.",
                                  "Please give the customer's 10-digit phone number."),
                        listen=True, done=False)
        known = repo.customer_by_phone(phone)
        if known:
            state["customer"] = {"phone": known["phone"],
                                 "customer_id": known.get("customer_id"),
                                 "name": known.get("name")}
        else:
            # The name was already collected before the number was asked for,
            # so a brand-new customer is complete the moment the phone lands.
            pending = state.pop("pending_customer_name", None)
            if pending:
                customer = repo.upsert_customer(phone, pending)
                state["customer"] = {"phone": customer["phone"],
                                     "customer_id": customer["customer_id"],
                                     "name": customer["name"]}
            else:
                state["customer"] = {"phone": repo.normalize_phone(phone),
                                     "customer_id": None, "name": None}
        state["awaiting"] = None
        return _resume_customer_capture(state, repo)

    if aw == "customer_name":
        name = _clean_spoken_name(user_text)
        if not name:
            return _say(state, _L(state, "Naam bataiye.", "Please tell me the name."),
                        listen=True, done=False)
        # Customer names are always saved in English/Latin script — bills,
        # receipts and the CRM table read consistently regardless of which
        # language the owner spoke the name in.
        name = _transliterate_to_latin(name)
        state["awaiting"] = None

        # A phone was captured first (older flow, or the owner volunteered a
        # number) — that number already identifies the customer, so just name
        # them rather than searching the CRM for a different match.
        existing_phone = (state.get("customer") or {}).get("phone")
        if existing_phone:
            customer = repo.upsert_customer(existing_phone, name)
            state["customer"] = {"phone": customer["phone"],
                                 "customer_id": customer["customer_id"],
                                 "name": customer["name"]}
            return _resume_customer_capture(state, repo)

        matches = _find_customers_by_name(repo, name)
        if len(matches) == 1:
            c = matches[0]
            state["customer"] = {"phone": c.get("phone"),
                                 "customer_id": c.get("customer_id"),
                                 "name": c.get("name")}
            return _resume_customer_capture(state, repo)
        if len(matches) > 1:
            return _ask_customer_choice(state, matches)
        # Nobody by that name — collect a number and open a new account.
        state["pending_customer_name"] = name
        state["awaiting"] = "customer_phone"
        return _say(state, _L(state,
                              f"{name} naam se koi customer nahi mila — naya "
                              "customer hai? Unka 10 digit number bataiye.",
                              f"No customer named {name} — a new customer? Please "
                              "give their 10-digit phone number."),
                    listen=True, done=False)

    if aw == "customer_choice":
        options = state.get("customer_options") or []
        pick = None
        text = _fold(user_text)
        for i, pat in enumerate(_ORDINALS[:len(options)]):
            if pat.search(text):
                pick = options[i]
                break
        if pick is None:
            named = [c for c in options
                     if _norm_name(c.get("name")) == _norm_name(_clean_spoken_name(user_text))]
            if len(named) == 1:
                pick = named[0]
        if pick is None:
            # A number is unambiguous where a repeated name isn't.
            phone = parse_phone(user_text)
            pick = next((c for c in options
                         if repo.normalize_phone(c.get("phone") or "")
                         == repo.normalize_phone(phone)), None) if phone else None
        if pick is None:
            return _ask_customer_choice(state, options, repeat=True)
        state.pop("customer_options", None)
        state["awaiting"] = None
        state["customer"] = {"phone": pick.get("phone"),
                             "customer_id": pick.get("customer_id"),
                             "name": pick.get("name")}
        return _resume_customer_capture(state, repo)

    if aw == "deadline":
        deadline = parse_deadline(user_text)
        if not deadline:
            return _say(state, _L(state,
                                  "Date samajh nahi aayi — jaise 'kal', 'agle hafte', "
                                  "ya '5 din baad' bataiye.",
                                  "Didn't catch the date — try 'tomorrow', 'next week', "
                                  "or 'in 5 days'."),
                        listen=True, done=False)
        state["payment_deadline"] = deadline
        state["awaiting"] = None
        return _resume_customer_capture(state, repo)


def _ask_customer_choice(state, options, repeat=False):
    """Several customers share the spoken name — put them on screen and let the
    owner say 'pehla'/'doosra' instead of reciting a phone number."""
    options = options[:4]
    state["customer_options"] = options
    state["awaiting"] = "customer_choice"
    labels = [f"{i + 1}. {c.get('name')}" + (f" ({c.get('phone')})" if c.get("phone") else "")
              for i, c in enumerate(options)]
    listing = "; ".join(labels)
    if repeat:
        say = _L(state, f"Samajh nahi aaya — {listing}. Pehla ya doosra?",
                 f"Didn't catch that — {listing}. The first or the second?")
    else:
        say = _L(state, f"Is naam ke {len(options)} customer hain — {listing}. Kaun sa?",
                 f"There are {len(options)} customers with that name — {listing}. Which one?")
    out = _say(state, say, listen=True, done=False)
    out["customer_options"] = options
    return out


def _resume_customer_capture(state, repo):
    customer = state.get("customer") or {}
    if not customer.get("customer_id"):
        # Name first: the owner knows who they just sold to by name, not by
        # phone number. A number is only asked for once the name turns out to
        # be nobody already on the books.
        if not customer.get("name"):
            state["awaiting"] = "customer_name"
            return _say(state, _L(state, "Customer ka naam kya hai?",
                                  "What's the customer's name?"),
                        listen=True, done=False)
        if not customer.get("phone"):
            state["awaiting"] = "customer_phone"
            return _say(state, _L(state, "Customer ka 10 digit contact number bataiye.",
                                  "Please give the customer's 10-digit phone number."),
                        listen=True, done=False)
    pending = state.get("pending_commit") or {}
    needs_deadline = any(it.get("payment") == "credit"
                         for it in pending.get("items", []))
    if needs_deadline and not state.get("payment_deadline"):
        state["awaiting"] = "deadline"
        return _say(state, _L(state,
                              f"{customer['name']} — payment kab tak? Date ya kitne din "
                              "baad bataiye.",
                              f"{customer['name']} — when will they pay? A date or "
                              "'in N days'."),
                    listen=True, done=False)
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
        unit = item.get("unit") or sku["default_unit"]
        rate_unit = item.get("rate_unit") or unit
        amount = (round(ledger.line_amount(qty, unit, rate, rate_unit, sku), 2)
                  if flow in ("sale", "delivery") else None)
        if amount is not None:
            total += amount
        preview_items.append({
            "sku_id": sku["sku_id"], "canonical": sku["canonical"],
            "qty": qty, "unit": unit,
            "rate": item.get("rate"), "rate_unit": rate_unit,
            "payment": item.get("payment"),
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
    out = _say(state, _L(state,
                         "Saari details taiyaar hain. Check karke Confirm Entry dabaiye.",
                         "Everything's ready. Please check and press Confirm Entry."),
               listen=False, done=False)
    out["confirmation_required"] = True
    out["confirmation"] = preview
    return out


def _commit(state, flow, items, skipped, repo):
    import main
    customer = state.get("customer") or {}
    deadline = state.get("payment_deadline")

    def _rows_for(its):
        ev, rw = [], []
        for it in its:
            sku = repo.sku(it["sku_id"])
            payment = it.get("payment") or "cash"
            ev.append({"sku_id": sku["sku_id"], "qty": float(it["qty"]),
                       "unit": it["unit"], "rate": it.get("rate"),
                       "rate_unit": it.get("rate_unit") or it["unit"],
                       "payment": payment, "spoken": it.get("name", ""),
                       "was_tap": False,
                       "customer_id": customer.get("customer_id"),
                       "payment_deadline": deadline if payment == "credit" else None})
            rw.append({"phrase": it.get("name") or sku["canonical"],
                       "sku_id": sku["sku_id"], "qty": float(it["qty"]),
                       "unit": it["unit"], "rate": it.get("rate"),
                       "rate_unit": it.get("rate_unit") or it["unit"],
                       "payment": payment, "confirmed": True})
        return ev, rw

    base_etype = {"sale": "sale", "delivery": "delivery",
                  "count": "opening_balance"}.get(flow, flow)
    parts = [f"{_num(it['qty'])} {it['unit']} {repo.sku(it['sku_id'])['canonical']}"
            for it in items]

    # A delivery of a product provisioned THIS turn has no prior stock to
    # reconcile against — the delivered quantity directly becomes the known
    # count (an opening_balance), not a plain delivery layered onto nothing.
    # A delivery of a product that already existed stays uncounted, even if
    # it happened to be uncounted before — there could be untracked stock
    # already on the shelf, so a real physical count is still needed.
    fresh = [it for it in items if flow == "delivery" and it.get("_freshly_provisioned")]
    normal = [it for it in items if it not in fresh]

    result = {"committed": [], "affected_stock": {}}
    rows = []
    if normal:
        ev, rw = _rows_for(normal)
        r = main._write_events(base_etype, ev, clock.today().isoformat(), "exact", "voice_live")
        result["committed"] += r["committed"]
        result["affected_stock"].update(r["affected_stock"])
        rows += rw
    needs_count = [repo.sku(it["sku_id"])["canonical"] for it in normal
                  if flow == "delivery"
                  and result["affected_stock"].get(it["sku_id"], {}).get("uncounted")]
    if fresh:
        ev, rw = _rows_for(fresh)
        r = main._write_events("opening_balance", ev, clock.today().isoformat(), "exact", "voice_live")
        result["committed"] += r["committed"]
        result["affected_stock"].update(r["affected_stock"])
        rows += rw
        # lock in the actual paid rate as the new SKU's reference cost, since
        # it's no longer just an estimate from other sizes/types.
        for it in fresh:
            if it.get("rate"):
                repo.upsert_sku({"sku_id": it["sku_id"], "opening_cost_per_kg": it["rate"]})
    result["flow"] = flow  # sale|delivery|count — a bill is only ever ours to GIVE on a sale
    result["customer"] = {
        "customer_id": customer.get("customer_id"),
        "name": customer.get("name"), "phone": customer.get("phone"),
    } if customer else None
    if flow == "sale" and customer.get("customer_id") and deadline:
        # flow == "sale" never provisions new items, so `items` lines up
        # 1:1 with result["committed"] in the same order.
        credit_total = sum(c.get("amount") or 0 for i, c in enumerate(result["committed"])
                           if (items[i].get("payment") or "cash") == "credit")
        if credit_total:
            result["receivable"] = repo.add_receivable(
                customer["customer_id"], credit_total, deadline,
                [c["event_id"] for i, c in enumerate(result["committed"])
                 if (items[i].get("payment") or "cash") == "credit"])
    verb = _L(state,
             {"sale": "bik gaya", "delivery": "aa gaya",
              "count": "gin liya"}.get(flow, "likh diya"),
             {"sale": "sold", "delivery": "received",
              "count": "counted"}.get(flow, "recorded"))
    # One flowing sentence, comma-joined, exactly one trailing period — not a
    # stack of separately-punctuated fragments (which doubled up whenever a
    # captured name already carried STT's own trailing punctuation).
    bits = [f"{_oxford(parts, state)} {verb}"]
    if customer.get("name"):
        bits.append(_L(state, f"{customer['name']} ke naam", f"for {customer['name']}"))
    if deadline:
        bits.append(_L(state, f"udhaar {deadline} tak", f"on credit, due {deadline}"))
    say = _L(state, "Theek hai — ", "Done — ") + ", ".join(bits) + "."
    if skipped:
        say += _L(state, f" {_oxford(skipped)} chhod diya, wo stock mein nahi.",
                  f" Skipped {_oxford(skipped, state)} — not in stock.")
    if needs_count:
        say += _L(state,
                  f" {_oxford(needs_count)} pehle se stock mein ho sakta hai — ek baar "
                  "gin lo, jab chaho voice se bata dena.",
                  f" {_oxford(needs_count, state)} may already have stock on the shelf — do a "
                  "count when you can, you can tell me the number by voice anytime.")
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
        nm = (it or {}).get("name") or _L(state, "Wo cheez", "that item")
        return _say(state, _L(state,
                              f"{nm} hum nahi rakhte — sariya, cement, ya tiles hai.",
                              f"We don't stock {nm} — only TMT bars, cement, or tiles."),
                    listen=True, done=False)
    if not it.get("sku_id"):
        return _say(state, _ask_product(it, state), listen=True, done=False)
    return _answer_stock(repo.sku(it["sku_id"]), repo, state)


def _answer_stock(sku, repo, state=None):
    import main
    import ledger
    det = ledger._stock_detail(sku, repo.all_events())
    view = main._stock_view(sku, det)
    if view.get("uncounted"):
        say = _L(state,
                 f"{sku['canonical']} abhi tak gina nahi gaya — ek baar gin lo to "
                 "ledger mein aa jayega.",
                 f"{sku['canonical']} hasn't been counted yet — do a stock count "
                 "and it'll show up in the ledger.")
    elif view.get("oversold"):
        say = _L(state,
                 f"{sku['canonical']} recorded se zyada bik gaya lagta hai — ek baar gin lo.",
                 f"{sku['canonical']} looks oversold versus what's recorded — please "
                 "do a stock count.")
    else:
        say = _L(state, f"{sku['canonical']} ka stock {view['display']} hai.",
                 f"{sku['canonical']} stock is {view['display']}.")
    return _reply(say, listen=False, done=True,
                  summary={"items": [{"phrase": sku["canonical"],
                                      "sku_id": sku["sku_id"],
                                      "unit": view.get("unit"), "confirmed": True}],
                           "answer": say})


# ---------------------------------------------------------------------------
# Analytics question (exact numbers from the ledger)
# ---------------------------------------------------------------------------
def _business_summary(metric, repo, state):
    """The one question a shopkeeper actually asks at closing time: how did
    today go. Everything here is derived from the same ledger the Today and
    Dashboard screens read, so the spoken number can never drift from the
    screen. The weekly version additionally reports frozen capital — a
    60-day-old problem is a weekly-review concern, not a daily one."""
    import main
    import ledger as L
    from datetime import timedelta

    t = main._today_summary()
    d = main.dashboard()
    mp = d["money_position"]
    weekly = metric == "week_summary"

    if weekly:
        catalogue_by = main.by_id()
        events = repo.all_events()
        total = margin = cash = credit = 0.0
        for i in range(7):
            m = L.margin_for_day(catalogue_by, events, clock.today() - timedelta(days=i))
            total += m["total"]
            margin += m["margin"]
            cash += m["cash"]
            credit += m["credit"]
        head = _L(state, "Pichhle saat din mein", "Over the last seven days")
    else:
        total, margin = t["total"], t["margin"]
        cash, credit = t["cash"], t["credit"]
        head = _L(state, "Aaj", "Today")

    bits = [
        _L(state, f"sale {int(total)} rupaye", f"sales were ₹{int(total)}"),
        _L(state, f"gross profit {int(margin)} rupaye", f"gross profit ₹{int(margin)}"),
        _L(state, f"cash {int(cash)} rupaye", f"cash ₹{int(cash)}"),
        _L(state, f"udhaar {int(credit)} rupaye", f"credit ₹{int(credit)}"),
    ]
    say = (f"{head} {', '.join(bits[:-1])} " + _L(state, "aur ", "and ") + f"{bits[-1]}.")
    say += _L(state,
              f" Total bakaya udhaar {int(mp['outstanding_credit'])} rupaye hai.",
              f" Total outstanding credit is ₹{int(mp['outstanding_credit'])}.")

    # Best-selling line of the period gives the number some texture.
    top = None
    lines = t.get("lines") or []
    if lines and not weekly:
        top = max(lines, key=lambda l: l.get("amount") or 0)
    if top and top.get("canonical"):
        say += _L(state, f" Sabse zyada {top['canonical']} gaya.",
                  f" {top['canonical']} moved the most.")

    if weekly:
        frozen = d.get("frozen_capital") or []
        if frozen:
            names = _oxford([f["canonical"] for f in frozen[:3]], state)
            say += _L(state,
                      f" Frozen maal: {names} — 60 din se nahi bika, "
                      f"{int(d['frozen_total'])} rupaye phasa hua hai.",
                      f" Frozen stock: {names} — unsold for 60 days, with "
                      f"₹{int(d['frozen_total'])} locked up.")
        else:
            say += _L(state, " Koi maal 60 din se phasa nahi hai.",
                      " No stock has been sitting frozen for 60 days.")
        say += _L(state,
                  f" Inventory ki value {int(mp['inventory_value'])} rupaye hai.",
                  f" Inventory is worth ₹{int(mp['inventory_value'])}.")
    return _reply(say, listen=False, done=True, summary={"items": [], "answer": say})


def _analytics_answer(metric, repo, state=None):
    import main
    if metric in ("day_summary", "week_summary"):
        return _business_summary(metric, repo, state)
    t = main._today_summary()
    d = main.dashboard()
    mp = d["money_position"]
    if metric == "cash":
        say = _L(state, f"Aaj cash {int(t['cash'])} rupaye aaya.",
                 f"₹{int(t['cash'])} cash came in today.")
    elif metric == "udhaar":
        say = _L(state,
                 f"Total udhaar {int(mp['outstanding_credit'])} rupaye baaki hai. "
                 f"Aaj {int(t['credit'])} rupaye udhaar gaya.",
                 f"Total outstanding credit is ₹{int(mp['outstanding_credit'])}. "
                 f"₹{int(t['credit'])} went out on credit today.")
    elif metric == "frozen":
        ft = int(d["frozen_total"])
        items = d.get("frozen_capital") or []
        if not items:
            say = _L(state, "Abhi koi maal 60 din se pada nahi hai, sab move ho raha hai.",
                     "Nothing has been sitting unsold for 60 days — everything's moving.")
        else:
            names = _oxford([f"{f['canonical']} ({f['stock']})" for f in items], state)
            say = _L(state,
                     f"{names} — 60 din se bika nahi, {ft} rupaye phasa hua hai.",
                     f"{names} — unsold for 60 days, ₹{ft} locked up in it.")
    elif metric == "inventory":
        say = _L(state,
                 f"Inventory ki value {int(mp['inventory_value'])} rupaye hai, landed cost pe.",
                 f"Inventory value is ₹{int(mp['inventory_value'])}, at landed cost.")
    else:
        say = _L(state,
                 f"Aaj ka margin abhi tak {int(t['margin'])} rupaye. "
                 f"Cash {int(t['cash'])}, udhaar {int(t['credit'])} rupaye.",
                 f"Today's margin so far is ₹{int(t['margin'])}. "
                 f"Cash ₹{int(t['cash'])}, credit ₹{int(t['credit'])}.")
    return _reply(say, listen=False, done=True, summary={"items": [], "answer": say})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _say(state, text, listen, done):
    notice = state.pop("pending_notice", None)
    if notice:
        text = notice + text
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


def _oxford(names, state=None):
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + _L(state, " aur ", " and ") + names[-1]


def _num(n):
    try:
        f = float(n)
        return int(f) if f == int(f) else f
    except Exception:
        return n
