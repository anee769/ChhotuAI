"""
nlp.py — deterministic fallback for parsing a spoken sale/delivery utterance
into line items, including mid-utterance self-correction ("do ton... nahi teen").

The LLM tool-call in main.py is primary; this runs when no API key is set or
the tool-call fails, so the demo never dies on a flaky network.
"""
from __future__ import annotations

import re

ONES = {"ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5,
        "panch": 5, "chhah": 6, "cheh": 6, "chah": 6, "saat": 7, "aath": 8,
        "aat": 8, "nau": 9, "das": 10, "dus": 10, "gyarah": 11, "barah": 12,
        "terah": 13, "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17,
        "atharah": 18, "unnees": 19, "bees": 20, "bis": 20, "pachees": 25,
        "tees": 30, "chalis": 40, "pachaas": 50, "pachas": 50, "saath": 60,
        "sattar": 70, "assi": 80, "nabbe": 90, "sau": 100,
        "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "छह": 6,
        "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "बारह": 12,
        "सोलह": 16, "बीस": 20, "पच्चीस": 25, "तीस": 30,
        "चालीस": 40, "पचास": 50, "साठ": 60, "सौ": 100}

UNIT_WORDS = ["kg", "kilo", "ton", "tonne", "tan", "bori", "bora", "bag",
              "piece", "pcs", "nag", "adad", "bundle", "litre", "liter",
              "bucket", "box", "feet", "metre", "meter", "किलो", "टन",
              "बोरी", "बोरा", "बैग", "पीस", "नग"]

_NEG = re.compile(r"\bnah?in?\b|\bnahi+\b|\bnahin\b")


def _to_num(tok: str):
    if tok.isdigit():
        return float(tok)
    return ONES.get(tok)


def _find_qty(segment: str):
    """Return the quantity, honoring self-correction (number AFTER 'nahi')."""
    toks = segment.split()
    nums = [(i, _to_num(t)) for i, t in enumerate(toks)]
    nums = [(i, n) for i, n in nums if n is not None]
    if not nums:
        return None, False
    neg_pos = None
    for m in _NEG.finditer(segment):
        # index into token stream ~ count spaces before match
        neg_pos = segment[:m.start()].count(" ")
    corrected = False
    if neg_pos is not None:
        after = [(i, n) for i, n in nums if i > neg_pos]
        if after:
            corrected = True
            return after[0][1], corrected
    return nums[0][1], corrected


def _find_unit(segment: str):
    for w in UNIT_WORDS:
        found = (w in segment) if re.search(r"[\u0900-\u097f]", w) else \
            bool(re.search(rf"\b{re.escape(w)}\b", segment))
        if found:
            return {"kilo": "kg", "ton": "tonne", "tan": "tonne", "bora": "bori",
                    "bag": "bori", "pcs": "piece", "nag": "piece", "adad": "piece",
                    "liter": "litre", "meter": "metre", "किलो": "kg",
                    "टन": "tonne", "बोरी": "bori", "बोरा": "bori",
                    "बैग": "bori", "पीस": "piece", "नग": "piece"}.get(w, w)
    return None


def _split_item_segments(text: str) -> list:
    """Split explicit separators and adjacent quantity+unit item starts.

    STT often drops an "aur", producing text such as
    "50 kg cement 10 ton sariya". Both quantity+unit groups are still strong,
    deterministic item boundaries and must not become one product phrase.
    """
    chunks = re.split(r"\baur\b|और|,|\+", text)
    qty_words = sorted(ONES, key=len, reverse=True)
    unit_words = sorted(UNIT_WORDS, key=len, reverse=True)
    qty_pat = r"(?:\d+(?:\.\d+)?|" + "|".join(map(re.escape, qty_words)) + r")"
    unit_pat = r"(?:" + "|".join(map(re.escape, unit_words)) + r")"
    start_re = re.compile(rf"(?<!\S){qty_pat}\s+{unit_pat}(?=\s|$)")
    out = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        starts = [m.start() for m in start_re.finditer(chunk)]
        if len(starts) <= 1:
            out.append(chunk)
            continue
        boundaries = [starts[0]]
        for start in starts[1:]:
            # A second quantity after "nahi" is normally a correction:
            # "do ton ... nahi, teen ton ...", not another item.
            if _NEG.search(chunk[boundaries[-1] : start]):
                continue
            boundaries.append(start)
        boundaries.append(len(chunk))
        for i, start in enumerate(boundaries[:-1]):
            part = chunk[start : boundaries[i + 1]].strip()
            if part:
                out.append(part)
    return out


def parse_sale_utterance(transcript: str) -> list:
    """
    Split on 'aur' into items; parse qty (with self-correction), unit, payment.
    Product phrase is left for the matcher.
    """
    t = transcript.lower().strip()
    payment = "credit" if re.search(r"\budhaar|udhar|credit\b", t) else \
        ("cash" if re.search(r"\bcash|nagad\b", t) else None)
    segments = _split_item_segments(t)
    items = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        qty, corrected = _find_qty(seg)
        if qty is None:
            continue
        unit = _find_unit(seg)
        # product phrase = segment minus leading number/unit noise (matcher is robust)
        items.append({
            "product_phrase": seg,
            "qty": qty,
            "unit": unit,
            "payment": payment,
            "self_corrected": corrected,
        })
    return items
