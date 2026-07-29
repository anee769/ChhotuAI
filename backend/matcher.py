"""
matcher.py — resolve a spoken phrase to a SKU (spec Section 8).

Four ordered stages:
  1. Normalize (romanized string arrives from Saaras translit mode)
  2. Exact alias hit (catalogue aliases + learned aliases)  -> conf 1.0
  3. Fuzzy lexical (rapidfuzz token_set_ratio, top 5 > 0.6)
  4. Semantic disambiguation via Sarvam-30B (candidates + recent corrections)

NO embedding models — Stage 4 IS the semantic layer, and it is Sarvam-only.

Also: confidence gates per flow, family->SKU variant disambiguation that asks
only about OPEN dimensions (and skips ones fixed by stock or by priors), unit
resolution, and temporal resolution.
"""
from __future__ import annotations

import re
from typing import Optional

from rapidfuzz import fuzz, process

import sarvam_client

# ---- confidence gates (spec Section 8) ----
GATES = {"delivery": 0.92, "live_sale": 0.80, "recall": 0.88,
         "count": 0.92, "stock_take": 0.92}

FILLERS = {"bhai", "ji", "sahab", "wala", "wali", "ka", "ke", "ki", "de", "do",
           "dede", "chahiye", "please", "aur", "ek", "the", "of", "hai", "mujhe"}

# Hindi / romanized number words -> mm for diameter parsing
NUM_WORDS = {"chhah": 6, "cheh": 6, "chah": 6, "aath": 8, "aat": 8, "das": 10,
             "dus": 10, "barah": 12, "baarah": 12, "bara": 12, "solah": 16,
             "sola": 16, "bees": 20, "bis": 20, "pachees": 25, "pachchis": 25,
             "pacchis": 25, "chhabbis": 25}

BRAND_HINTS = {"tata": "Tata Tiscon", "tiscon": "Tata Tiscon",
               "jindal": "Jindal Panther", "panther": "Jindal Panther",
               "srmb": "SRMB", "ultratech": "UltraTech", "acc": "ACC",
               "ambuja": "Ambuja", "jk": "JK", "asian": "Asian Paints",
               "berger": "Berger"}


# ---------------------------------------------------------------------------
# Stage 1 — normalize
# ---------------------------------------------------------------------------
def normalize(phrase: str) -> str:
    p = phrase.lower().strip()
    p = re.sub(r"[^\w\sऀ-ॿ½¾\"'.]", " ", p)
    toks = [t for t in p.split() if t not in FILLERS]
    return " ".join(toks).strip()


# ---------------------------------------------------------------------------
# Alias index
# ---------------------------------------------------------------------------
def build_alias_index(catalogue: list, learning: dict) -> dict:
    idx: dict = {}
    for s in catalogue:
        for a in s.get("aliases", []):
            key = normalize(str(a))
            if key:
                idx.setdefault(key, []).append(s["sku_id"])
    for la in learning.get("aliases_learned", []):
        key = normalize(str(la.get("phrase") or ""))
        sku_id = la.get("sku_id")
        if key and sku_id:
            idx.setdefault(key, []).append(sku_id)
    return idx


def fuzzy_learned_alias(phrase: str, learning: dict) -> Optional[dict]:
    """Resolve strong, unique ASR variations of a learned shop phrase.

    Voice transcription often changes only the spelling ("sariya" to
    "sariyaa"). We accept that variation for multi-word learned terminology,
    while a generic bare family word still goes through normal disambiguation.
    """
    norm = normalize(phrase)
    if not norm or len(norm.split()) < 2:
        return None

    best_by_sku: dict[str, dict] = {}
    for learned in learning.get("aliases_learned", []):
        sku_id = learned.get("sku_id")
        alias = normalize(str(learned.get("phrase") or ""))
        if not sku_id or len(alias.split()) < 2:
            continue
        score = max(
            fuzz.ratio(norm, alias),
            fuzz.token_set_ratio(norm, alias),
            fuzz.WRatio(norm, alias),
        ) / 100.0
        current = best_by_sku.get(sku_id)
        if current is None or score > current["score"]:
            best_by_sku[sku_id] = {
                "sku_id": sku_id,
                "score": score,
                "alias": alias,
            }

    ranked = sorted(best_by_sku.values(), key=lambda x: x["score"], reverse=True)
    if not ranked or ranked[0]["score"] < 0.90:
        return None
    if len(ranked) > 1 and ranked[1]["score"] > ranked[0]["score"] - 0.08:
        return None
    return {
        "sku_id": ranked[0]["sku_id"],
        "score": round(ranked[0]["score"], 3),
        "alias": ranked[0]["alias"],
    }


# ---------------------------------------------------------------------------
# Spoken attribute extraction
# ---------------------------------------------------------------------------
def extract_attrs(phrase: str) -> dict:
    p = normalize(phrase)
    compact = re.sub(r"\s+", "", p)
    attrs: dict = {}
    # diameter: require the mm unit so a quantity digit ("2 ton") isn't grabbed.
    m = re.search(r"\b(\d{1,2})\s*m\s?m\b", p)
    if m:
        attrs["diameter_mm"] = int(m.group(1))
    else:
        for w, mm in NUM_WORDS.items():
            if re.search(rf"\b{w}\b", p):
                attrs["diameter_mm"] = mm
                break
    # grade — capture any Fe### the owner says, even one we don't stock (Fe550).
    # Require the "fe" prefix or an explicit "500d" so a bare quantity/price
    # digit is never mistaken for a grade.
    mg = re.search(r"fe\s?(\d{3})\s*(d)?", p)
    if mg:
        attrs["grade"] = "Fe" + mg.group(1) + ("D" if mg.group(2) else "")
    elif re.search(r"\b500\s*d\b|500d", p):
        attrs["grade"] = "Fe500D"
    # brand
    for hint, brand in {
        **BRAND_HINTS,
        "अल्ट्राटेक": "UltraTech",
    }.items():
        if re.search(rf"\b{hint}\b", p):
            attrs["brand"] = brand
            break
    # cement type
    if re.search(r"opc\s*53|53\s*grade", p):
        attrs["type"] = "OPC 53"
    elif re.search(r"opc\s*43|43\s*grade", p):
        attrs["type"] = "OPC 43"
    elif re.search(r"\bppc\b", p) or "पीपीसी" in compact:
        attrs["type"] = "PPC"
    # pipe class
    mc = re.search(r"class\s*(\d)", p)
    if mc:
        attrs["class"] = f"Class {mc.group(1)}"
    # pipe size
    m = re.search(r'(\d(?:\.\d)?)\s*(?:inch|")', p)
    if m:
        attrs["size_inch"] = float(m.group(1))
    if "½" in phrase or "aadha" in p or "half" in p:
        attrs["size_inch"] = 0.5
    if "¾" in phrase or "pauna" in p:
        attrs["size_inch"] = 0.75
    return attrs


# ---------------------------------------------------------------------------
# Fuzzy
# ---------------------------------------------------------------------------
def fuzzy_candidates(phrase: str, catalogue: list, limit: int = 5) -> list:
    norm = normalize(phrase)
    choices = {}
    for s in catalogue:
        blob = s["canonical"].lower() + " " + " ".join(s.get("aliases", []))
        choices[s["sku_id"]] = blob
    scored = process.extract(norm, choices, scorer=fuzz.token_set_ratio, limit=limit)
    out = []
    for _blob, score, sku_id in scored:
        if score / 100.0 >= 0.6:
            out.append({"sku_id": sku_id, "score": round(score / 100.0, 3)})
    return out


# ---------------------------------------------------------------------------
# Stage 4 — Sarvam-30B semantic rerank
# ---------------------------------------------------------------------------
def semantic_pick(phrase: str, candidates: list, catalogue_by_id: dict,
                  learning: dict) -> Optional[dict]:
    if not sarvam_client.has_key() or not candidates:
        return None
    cand_lines = []
    for c in candidates:
        s = catalogue_by_id.get(c["sku_id"])
        if s:
            cand_lines.append(f'{c["sku_id"]}: {s["canonical"]}')
    fewshot = ""
    for corr in learning.get("corrections", [])[-20:]:
        fewshot += f'\n  "{corr["spoken"]}" -> {corr["chosen_sku"]}'
    sys = (
        "You match a shopkeeper's spoken Hindi/English product phrase to exactly "
        "one SKU id from the candidate list, or UNCERTAIN if none clearly fit. "
        "Reply ONLY as JSON: {\"sku_id\": \"<id or UNCERTAIN>\", \"confidence\": 0.0-1.0}."
    )
    user = (f'Phrase: "{phrase}"\nCandidates:\n' + "\n".join(cand_lines) +
            (f"\nPast corrections:{fewshot}" if fewshot else ""))
    try:
        out = sarvam_client.chat_json(
            [{"role": "system", "content": sys},
             {"role": "user", "content": user}])
    except Exception:
        return None
    sku = out.get("sku_id")
    if sku and sku != "UNCERTAIN" and sku in catalogue_by_id:
        return {"sku_id": sku, "confidence": float(out.get("confidence", 0.85))}
    return None


# ---------------------------------------------------------------------------
# Variant disambiguation (spec Section 8 — the "sariya" case)
# ---------------------------------------------------------------------------
_DIMS = {
    "tmt": [("diameter_mm", "size"), ("grade", "grade"), ("brand", "brand")],
    "cement": [("brand", "brand"), ("type", "type")],
    "pipe": [("size_inch", "size"), ("class", "class")],
}
_LABELS = {"diameter_mm": "Kaunsa size", "grade": "Kaunsa grade",
           "brand": "Kaunsa brand", "type": "Kaunsa type",
           "size_inch": "Kaunsa size", "class": "Kaunsa class"}


def _prior_value(learning: dict, family: str, attribute: str) -> Optional[str]:
    best = None
    for p in learning.get("attribute_priors", []):
        if p["family"] == family and p["attribute"] == attribute and p["count"] >= 10:
            if best is None or p["count"] > best["count"]:
                best = p
    return best["value"] if best else None


def resolve_variant(candidate_ids: list, catalogue_by_id: dict, spoken_attrs: dict,
                    learning: dict) -> dict:
    """
    Narrow a family to one SKU. Ask ONLY about open dimensions, in order, and
    skip any dimension that is fixed (one value stocked) or resolved by a strong
    prior. Returns matched | disambiguate.
    """
    cands = [catalogue_by_id[i] for i in candidate_ids if i in catalogue_by_id]
    if not cands:
        return {"status": "uncertain"}
    family = cands[0]["family"]
    dims = _DIMS.get(family, [])
    assumed = {}

    # Global not-stocked pre-check: if the owner named an attribute value that
    # exists in NO candidate at all (e.g. grade Fe550), say so — never ask about
    # other dimensions and never substitute (spec test #14).
    for attr, _kind in dims:
        if attr in spoken_attrs:
            all_values = {c["attributes"].get(attr) for c in cands}
            if spoken_attrs[attr] not in all_values:
                return {"status": "not_stocked", "attribute": attr,
                        "value": spoken_attrs[attr], "family": family,
                        "available": [_fmt_opt(attr, v) for v in sorted(
                            (v for v in all_values if v is not None),
                            key=lambda x: str(x))]}

    for attr, _kind in dims:
        values = sorted({c["attributes"].get(attr) for c in cands
                         if c["attributes"].get(attr) is not None},
                        key=lambda x: (str(type(x)), x))
        # spoken a value we do NOT stock -> say so, never substitute (test #14)
        if attr in spoken_attrs and values and spoken_attrs[attr] not in values:
            return {"status": "not_stocked", "attribute": attr,
                    "value": spoken_attrs[attr], "family": family,
                    "available": [_fmt_opt(attr, v) for v in values]}
        if len(values) <= 1:
            continue  # fixed — never ask
        # spoken?
        if attr in spoken_attrs and spoken_attrs[attr] in values:
            cands = [c for c in cands if c["attributes"].get(attr) == spoken_attrs[attr]]
            continue
        # strong prior?
        pv = _prior_value(learning, family, attr)
        if pv in values:
            cands = [c for c in cands if c["attributes"].get(attr) == pv]
            assumed[attr] = pv
            continue
        # must ask — ONE question, the open options only
        return {
            "status": "disambiguate",
            "attribute": attr,
            "question": _LABELS.get(attr, "Kaunsa"),
            "options": [{"value": v,
                         "label": _fmt_opt(attr, v),
                         "sku_ids": [c["sku_id"] for c in cands
                                     if c["attributes"].get(attr) == v]}
                        for v in values],
            "assumed": assumed,
        }
        # (loop breaks via return)

    if len(cands) == 1:
        return {"status": "matched", "sku_id": cands[0]["sku_id"],
                "confidence": 0.95, "assumed": assumed}
    # still several -> disambiguate on the first differing attr as fallback
    return {"status": "disambiguate", "attribute": "sku",
            "question": "Kaunsa exact",
            "options": [{"value": c["sku_id"], "label": c["canonical"],
                         "sku_ids": [c["sku_id"]]} for c in cands],
            "assumed": assumed}


def _fmt_opt(attr: str, v) -> str:
    if attr == "diameter_mm":
        return f"{v}mm"
    if attr == "size_inch":
        return f'{v}"'
    return str(v)


# ---------------------------------------------------------------------------
# Top-level match
# ---------------------------------------------------------------------------
def match(phrase: str, catalogue: list, learning: dict, flow: str = "live_sale") -> dict:
    """
    Returns one of:
      {status:"matched", sku_id, confidence, assumed}
      {status:"disambiguate", attribute, question, options, ...}
      {status:"confirm", candidates:[...]}  (below gate — surface top 3 to tap)
      {status:"uncertain", candidates:[...]}
    """
    by_id = {s["sku_id"]: s for s in catalogue}
    norm = normalize(phrase)
    spoken_attrs = extract_attrs(phrase)
    alias_idx = build_alias_index(catalogue, learning)
    gate = GATES.get(flow, 0.8)

    # Stage 1b — deterministic identifiers and canonical-name substrings.
    # Administrative tools often send a SKU id copied from list_inventory,
    # while the voice model commonly shortens a canonical name ("UltraTech
    # PPC" for "UltraTech PPC Cement 50kg"). Neither should need fuzzy or LLM
    # matching. Only accept a substring when it identifies exactly one SKU;
    # generic fragments such as "cement" must still be disambiguated.
    raw = str(phrase or "").strip().casefold()
    id_hits = [sku_id for sku_id in by_id if sku_id.casefold() == raw]
    if len(id_hits) == 1:
        return {"status": "matched", "sku_id": id_hits[0],
                "confidence": 1.0, "assumed": {}, "stage": "sku_id"}

    if norm:
        substring_hits = []
        padded_query = f" {norm} "
        for sku in catalogue:
            names = [sku.get("canonical") or "", *(sku.get("aliases") or [])]
            candidate_norms = {normalize(str(name)) for name in names if name}
            if any(
                candidate
                and (f" {norm} " in f" {candidate} "
                     or f" {candidate} " in padded_query)
                for candidate in candidate_norms
            ):
                substring_hits.append(sku["sku_id"])
        substring_hits = list(dict.fromkeys(substring_hits))
        if len(substring_hits) == 1:
            return {"status": "matched", "sku_id": substring_hits[0],
                    "confidence": 1.0, "assumed": {},
                    "stage": "unique_substring"}

    # Stage 2 — exact alias: whole phrase, then multiword-substring (learned
    # phrases like "chhota patla rod"). Before generic single-token aliases,
    # tolerate a safe ASR spelling variation of a learned multi-word phrase.
    hit_ids = None
    if norm in alias_idx:
        hit_ids = alias_idx[norm]
    if hit_ids is None:
        padded = f" {norm} "
        multiword = sorted((a for a in alias_idx if " " in a), key=len, reverse=True)
        for a in multiword:
            if f" {a} " in padded:
                hit_ids = alias_idx[a]
                break
    if hit_ids is None:
        learned = fuzzy_learned_alias(phrase, learning)
        if learned and learned["sku_id"] in by_id:
            return {
                "status": "matched",
                "sku_id": learned["sku_id"],
                "confidence": learned["score"],
                "assumed": {},
                "stage": "learned_alias_fuzzy",
                "learned_alias": learned["alias"],
            }
    if hit_ids is None:
        for tok in sorted(norm.split(), key=len, reverse=True):
            if tok in alias_idx:
                hit_ids = alias_idx[tok]
                break

    if hit_ids:
        uniq = list(dict.fromkeys(hit_ids))
        if len(uniq) == 1:
            return {"status": "matched", "sku_id": uniq[0], "confidence": 1.0,
                    "assumed": {}, "stage": "alias"}
        # family-level alias -> disambiguate
        res = resolve_variant(uniq, by_id, spoken_attrs, learning)
        res["stage"] = "alias_family"
        return res

    # Stage 2b — attribute-based family inference (diameter -> tmt, size -> pipe).
    # Lets bare spoken attributes ("barah mm", "Fe500D wala") reach the right
    # family even with no family word or learned alias present.
    fam_from_attrs = None
    if "diameter_mm" in spoken_attrs or "grade" in spoken_attrs:
        fam_from_attrs = "tmt"  # diameters & Fe-grades are steel in this catalogue
    elif "size_inch" in spoken_attrs:
        fam_from_attrs = "pipe"
    if fam_from_attrs:
        cand_ids = [s["sku_id"] for s in catalogue if s["family"] == fam_from_attrs]
        res = resolve_variant(cand_ids, by_id, spoken_attrs, learning)
        if res.get("status") in ("matched", "disambiguate", "not_stocked"):
            res["stage"] = "attr_family"
            return res

    # Stage 3 — fuzzy
    cands = fuzzy_candidates(phrase, catalogue)
    if not cands:
        return {"status": "uncertain", "candidates": [], "stage": "fuzzy"}

    # if the top candidates share a family and the phrase is generic, disambiguate
    top = cands[0]
    if top["score"] >= 0.92 and (len(cands) == 1 or cands[1]["score"] < top["score"] - 0.15):
        return {"status": "matched", "sku_id": top["sku_id"],
                "confidence": top["score"], "assumed": {}, "stage": "fuzzy"}

    # Stage 4 — semantic rerank
    pick = semantic_pick(phrase, cands, by_id, learning)
    if pick:
        conf = pick["confidence"]
        if conf >= gate:
            return {"status": "matched", "sku_id": pick["sku_id"],
                    "confidence": conf, "assumed": {}, "stage": "semantic"}

    # below gate — never guess. Surface top 3 for a tap.
    return {
        "status": "confirm",
        "candidates": [{"sku_id": c["sku_id"],
                        "canonical": by_id[c["sku_id"]]["canonical"],
                        "score": c["score"]} for c in cands[:3]],
        "gate": gate,
        "stage": "below_gate",
    }


# ---------------------------------------------------------------------------
# Unit resolution
# ---------------------------------------------------------------------------
UNIT_WORDS = {
    "kg": "kg", "kilo": "kg", "kilogram": "kg",
    "ton": "tonne", "tonne": "tonne", "tan": "tonne",
    "bori": "bori", "bora": "bori", "bag": "bori", "bag": "bori",
    "piece": "piece", "pcs": "piece", "nag": "piece", "adad": "piece",
    "feet": "feet", "foot": "feet", "metre": "metre", "meter": "metre",
    "bundle": "bundle", "litre": "litre", "liter": "litre", "bucket": "bucket",
    "box": "box",
}


def resolve_unit(phrase: str, sku: dict, learning: dict) -> dict:
    p = normalize(phrase)
    for w, u in UNIT_WORDS.items():
        if re.search(rf"\b{w}\b", p) and u in sku.get("units", {}):
            return {"unit": u, "assumed": False}
    # unit prior
    for up in learning.get("unit_priors", []):
        if up["sku_id"] == sku["sku_id"] and up["unit"] in sku.get("units", {}):
            return {"unit": up["unit"], "assumed": True}
    return {"unit": sku.get("default_unit"), "assumed": True}


# ---------------------------------------------------------------------------
# Temporal resolution
# ---------------------------------------------------------------------------
def resolve_temporal(phrase: str, today) -> dict:
    """
    Returns {date, precision, ask, vague, marker}.
    Default to today, silent. ANY explicit past marker (kal/parso/pichle hafte)
    surfaces day chips to confirm; a vague one (pichle hafte) adds "Pata nahi"
    -> week precision, excluded from a single day's margin.
    """
    from datetime import timedelta
    p = normalize(phrase)
    if re.search(r"\babhi\b", p):
        return {"date": today, "precision": "exact", "ask": False, "vague": False, "marker": "abhi"}
    if re.search(r"pichl[ae]\s+haft|is\s+haft|last\s+week", p):
        return {"date": today, "precision": "week", "ask": True, "vague": True, "marker": "pichle hafte"}
    if re.search(r"\bparso|parson|tarso", p):
        return {"date": today - timedelta(days=2), "precision": "day", "ask": True, "vague": False, "marker": "parso"}
    if re.search(r"\bkal\b|yesterday", p):
        return {"date": today - timedelta(days=1), "precision": "day", "ask": True, "vague": False, "marker": "kal"}
    if re.search(r"\baaj\b|today", p):
        return {"date": today, "precision": "day", "ask": False, "vague": False, "marker": "aaj"}
    return {"date": today, "precision": "exact", "ask": False, "vague": False, "marker": None}
