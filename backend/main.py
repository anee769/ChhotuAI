"""
main.py — ChhotuAI FastAPI backend (spec Sections 9 & 10).

One page, four tabs. Voice-first stock & margin ledger. All AI = Sarvam only.
Storage behind Repo. Stock is always derived from the event log — never stored.

Run:  uvicorn backend.main:app --reload --port 8000   (from repo root)
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Body, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sarvam_client
import matcher as M
import ledger as L
import learning as LEARN
import nlp
import crm
from repo import JsonRepo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TODAY = date(2026, 7, 26)  # fixed "today" so the seeded demo is stable

app = FastAPI(title="ChhotuAI")
repo = JsonRepo()

# uvicorn loads this file as the package module "backend.main", but it also
# inserts backend/ onto sys.path (above) so that bare imports like
# `import matcher` work everywhere in this codebase. That same sys.path entry
# means a bare `import main` (used throughout conversation.py, lazily, inside
# request handlers) does NOT resolve to this already-loaded module — it
# re-executes this entire file as a SECOND, independent top-level module,
# with its OWN `repo = JsonRepo()`. Every commit made via conversation.py's
# `import main; main._write_events(...)` was silently writing into that
# shadow copy's repo instead of this one: correctly persisted to disk (both
# instances point at the same files), but invisible to /api/dashboard and any
# other read on THIS running app until the process was restarted and both
# happened to reload from the same now-consistent files. Registering this
# module under the bare name up front makes any later `import main` resolve
# to this exact object instead of creating a shadow one.
sys.modules.setdefault("main", sys.modules[__name__])
app.mount("/data", StaticFiles(directory=str(DATA)), name="data")


def by_id() -> dict:
    return {s["sku_id"]: s for s in repo.load_catalogue()}


# ---------------------------------------------------------------------------
# Static page
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# State / learning toggle
# ---------------------------------------------------------------------------
@app.get("/api/state")
def state():
    return {
        "shop": "Sharma Building Materials",
        "today": TODAY.isoformat(),
        "catalogue": repo.load_catalogue(),
        "learning_state": repo.learning_state(),
        "learning_counts": repo.learning_counts(),
    }


@app.get("/api/config")
def get_config():
    return repo.load_config()


@app.post("/api/config")
def set_config(payload: dict = Body(...)):
    repo.save_config(payload)
    return repo.load_config()


@app.post("/api/toggle")
def toggle(payload: dict = Body(...)):
    which = payload.get("which", "day1")
    repo.set_learning_state("day60" if which == "day60" else "day1")
    return {"learning_state": repo.learning_state(),
            "learning_counts": repo.learning_counts()}


# ---------------------------------------------------------------------------
# Transcription (Saaras) — both modes
# ---------------------------------------------------------------------------
@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    audio = await file.read()
    if not sarvam_client.has_key():
        raise HTTPException(400, "SARVAM_API_KEY not set — type the phrase instead (text box).")
    out = sarvam_client.transcribe_both(audio, file.filename or "audio.wav")
    return out


# ---------------------------------------------------------------------------
# Sale / delivery / count parsing  (LLM tool-call, nlp fallback)
# ---------------------------------------------------------------------------
_RECORD_SALE_TOOL = [{
    "type": "function",
    "function": {
        "name": "record_sale",
        "description": "Extract line items from a shopkeeper's spoken sale/delivery. "
                       "Resolve any mid-sentence self-correction (e.g. 'do ton nahi teen ton' -> 3).",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_phrase": {"type": "string"},
                            "qty": {"type": "number"},
                            "unit": {"type": "string"},
                            "rate": {"type": "number"},
                            "payment": {"type": "string", "enum": ["cash", "credit"]},
                        },
                        "required": ["product_phrase", "qty"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}]


def _llm_items(transcript: str):
    if not sarvam_client.has_key():
        return None
    try:
        resp = sarvam_client.chat(
            [{"role": "system", "content": "You extract sale line items from Hindi/English "
              "code-mixed speech for a hardware shop. Always call record_sale."},
             {"role": "user", "content": transcript}],
            tools=_RECORD_SALE_TOOL, tool_choice="auto")
        msg = sarvam_client.chat_message(resp)
        for tc in msg.get("tool_calls", []) or []:
            if tc["function"]["name"] == "record_sale":
                args = json.loads(tc["function"]["arguments"])
                return args.get("items", [])
    except Exception:
        return None
    return None


def _resolve_items(items: list, flow: str, learning: dict, catalogue: list) -> list:
    out = []
    for it in items:
        phrase = it.get("product_phrase", "")
        m = M.match(phrase, catalogue, learning, flow)
        row = {"phrase": phrase, "qty": it.get("qty"), "unit": it.get("unit"),
               "rate": it.get("rate"), "payment": it.get("payment"),
               "self_corrected": it.get("self_corrected", False), "match": m}
        if m["status"] == "matched":
            sku = repo.sku(m["sku_id"])
            if not it.get("unit"):
                u = M.resolve_unit(phrase, sku, learning)
                row["unit"] = u["unit"]
                row["unit_assumed"] = u["assumed"]
        out.append(row)
    return out


@app.post("/api/parse")
async def parse(payload: dict = Body(...)):
    """Body: {text?, flow}. Returns transcript, resolved items, temporal."""
    flow = payload.get("flow", "live_sale")
    text = payload.get("text", "").strip()
    catalogue = repo.load_catalogue()
    learning = repo.load_learning()

    # Self-correction ("...nahi teen ton") is handled deterministically by the
    # nlp parser (the LLM tends to emit both the struck and corrected value as
    # separate items). For everything else, Sarvam-30B tool-calling is primary.
    import re as _re
    has_negation = bool(_re.search(r"\bnah?in?\b|nahi+", text.lower()))
    if has_negation:
        items = nlp.parse_sale_utterance(text)
    else:
        items = _llm_items(text) or nlp.parse_sale_utterance(text)
    resolved = _resolve_items(items, flow, learning, catalogue)
    temporal = M.resolve_temporal(text, TODAY)
    temporal["date"] = temporal["date"].isoformat()
    return {"transcript": text, "items": resolved, "temporal": temporal}


@app.post("/api/converse")
def converse(payload: dict = Body(...)):
    """
    Voice conversation turn (spec: all follow-ups by voice).
    Body: {state?, text, flow}. state=None starts a new dialogue from `text`;
    otherwise `text` is the answer to the last question. Returns the next thing
    Chhotu should SAY (text + Bulbul audio), whether to keep listening, the
    running summary, and — when done — the committed result.
    """
    import conversation
    state = payload.get("state")
    text = payload.get("text", "")
    flow = payload.get("flow", "live_sale")
    out = conversation.converse(state, text, flow, repo)
    # speak the question/confirmation
    out["say_audio_b64"] = None
    if out.get("say") and sarvam_client.has_key():
        try:
            out["say_audio_b64"] = sarvam_client.text_to_speech(out["say"], "hi-IN")
        except Exception as e:
            out["tts_error"] = str(e)
    out["learning_counts"] = repo.learning_counts()
    return out


@app.post("/api/say")
def say(payload: dict = Body(...)):
    """TTS a short line (used for the 'Haan, boliye' wake prompt)."""
    text = payload.get("text", "")
    if not text or not sarvam_client.has_key():
        return {"audio_b64": None}
    try:
        return {"audio_b64": sarvam_client.text_to_speech(text, "hi-IN")}
    except Exception as e:
        return {"audio_b64": None, "error": str(e)}


@app.post("/api/resolve")
def resolve(payload: dict = Body(...)):
    """Re-run the matcher for a refined phrase (after a disambiguation tap)."""
    phrase = payload.get("phrase", "")
    flow = payload.get("flow", "live_sale")
    m = M.match(phrase, repo.load_catalogue(), repo.load_learning(), flow)
    return m


# ---------------------------------------------------------------------------
# Commit events  (sale / delivery / opening_balance / stock_take)
# ---------------------------------------------------------------------------
@app.post("/api/commit")
def commit(payload: dict = Body(...)):
    """
    Body: {type, items:[{sku_id, qty, unit, rate?, payment?, spoken?, rejected?,
    was_tap?}], occurred_on?, precision?}
    Appends events (never mutates stock), records learning, returns affected
    stock recomputed from the log (so back-dated inserts show recompute).
    """
    etype = payload["type"]
    occurred_on = payload.get("occurred_on") or TODAY.isoformat()
    precision = payload.get("precision", "exact")
    result = _write_events(etype, payload.get("items", []), occurred_on, precision,
                           payload.get("source", "voice_live"))
    # learn from the confirmation/tap (tap flow only; conversation learns inline)
    for it in payload.get("items", []):
        if it.get("spoken") and it.get("sku_id"):
            LEARN.record_confirmation(repo, it["spoken"], it["sku_id"],
                                      rejected=it.get("rejected", []),
                                      unit=it.get("unit"),
                                      was_tap=it.get("was_tap", False))
    result["learning_counts"] = repo.learning_counts()
    return result


def _write_events(etype: str, items: list, occurred_on: str, precision: str,
                  source: str) -> dict:
    """Append events (never mutates stock) and return recomputed affected stock."""
    catalogue_by = by_id()
    committed = []
    for it in items:
        sku_id = it.get("sku_id")
        sku = catalogue_by.get(sku_id)
        if not sku:
            continue
        quoted_rate = it.get("rate")
        rate_unit = it.get("rate_unit")
        rate_assumed = False
        if etype == "sale" and quoted_rate is None:
            cost = L.landed_cost_as_of(sku, repo.all_events(), L._d(occurred_on))
            quoted_rate = round((cost or 0) * 1.10, 1)
            rate_unit = L.base_unit(sku)
            rate_assumed = True
        base_rate = (L.rate_to_base(float(quoted_rate), rate_unit, sku)
                     if quoted_rate is not None and rate_unit
                     else quoted_rate)
        conf = L.compute_confidence(0.9, occurred_on,
                                    datetime.now().isoformat(timespec="seconds"),
                                    precision)
        ev = {
            "type": etype, "sku_id": sku_id,
            "qty": float(it["qty"]), "unit": it.get("unit", sku["default_unit"]),
            # `rate` remains per base unit for all ledger math. Preserve the
            # owner's quoted rate/unit separately for bills and audit display.
            "rate": base_rate,
            "quoted_rate": quoted_rate if rate_unit else None,
            "rate_unit": rate_unit,
            "payment": it.get("payment") if etype == "sale" else None,
            "customer_id": it.get("customer_id") if etype == "sale" else None,
            "payment_deadline": it.get("payment_deadline") if etype == "sale" else None,
            "occurred_on": occurred_on, "precision": precision,
            "count_precision": it.get("count_precision"),
            "occurred_at": None, "confidence": conf, "source": source,
            "evidence": {"transcript": it.get("spoken", "")},
        }
        eid = repo.append_event(ev)
        amount = (L.line_amount(
            float(it["qty"]), it.get("unit", sku["default_unit"]),
            float(quoted_rate), rate_unit, sku)
            if quoted_rate is not None else 0)
        committed.append({
            "event_id": eid, "sku_id": sku_id,
            "rate": quoted_rate, "rate_unit": rate_unit,
            "base_rate": base_rate, "rate_assumed": rate_assumed,
            "amount": round(amount, 2),
        })
    events = repo.all_events()
    affected = {}
    for c in committed:
        sku = catalogue_by[c["sku_id"]]
        affected[c["sku_id"]] = _stock_view(sku, L._stock_detail(sku, events))
    return {"committed": committed, "affected_stock": affected}


# ---------------------------------------------------------------------------
# Customers / credit / payments
# ---------------------------------------------------------------------------
@app.get("/api/customers")
def customers():
    return {"customers": crm.accounts(repo)}


@app.post("/api/customers")
def save_customer(payload: dict = Body(...)):
    try:
        return repo.upsert_customer(payload.get("phone", ""), payload.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/customers/{customer_id}/payments")
def record_payment(customer_id: str, payload: dict = Body(...)):
    customer = repo.customer(customer_id)
    if not customer:
        raise HTTPException(404, "customer not found")
    try:
        amount = float(payload.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    account = crm.account(repo, customer_id)
    if amount <= 0:
        raise HTTPException(400, "payment amount must be positive")
    if not account or amount > account["outstanding"] + 0.01:
        raise HTTPException(400, "payment exceeds outstanding credit")
    row = repo.add_payment(
        customer_id, amount, payload.get("paid_on") or TODAY.isoformat(),
        payload.get("note", ""),
    )
    return {"payment": row, "account": crm.account(repo, customer_id)}


def _stock_view(sku: dict, det: dict) -> dict:
    if det["qty"] == L.UNCOUNTED:
        return {"display": "abhi tak gina nahi", "uncounted": True,
                "unit": sku.get("default_unit")}
    qty = det["qty"]
    prefix = "~" if det.get("estimated") else ""
    unit = det.get("unit", "")
    qs = f"{qty:g}"  # 8.0 -> "8", 4.2 -> "4.2"
    if qty < 0:
        return {"display": f"0 {unit} — recorded se {prefix}{abs(qty):g} zyada bika, gin lo",
                "qty": qty, "uncounted": False, "oversold": True, "unit": unit}
    return {"display": f'{prefix}{qs} {unit}',
            "qty": qty, "uncounted": False, "estimated": det.get("estimated"),
            "unit": unit}


# ---------------------------------------------------------------------------
# Stock listing + reconciliation
# ---------------------------------------------------------------------------
@app.get("/api/stock")
def stock(as_of: str = None):
    events = repo.all_events()
    ao = L._d(as_of) if as_of else None
    rows = []
    for sku in repo.load_catalogue():
        det = L._stock_detail(sku, events, ao)
        rows.append({"sku_id": sku["sku_id"], "canonical": sku["canonical"],
                     "family": sku["family"], **_stock_view(sku, det)})
    return {"as_of": (as_of or TODAY.isoformat()), "rows": rows}


@app.get("/api/reconciliations")
def reconciliations():
    events = repo.all_events()
    catalogue_by = by_id()
    out = []
    for e in events:
        if e["type"] != "stock_take":
            continue
        sku = catalogue_by.get(e["sku_id"])
        if not sku:
            continue
        r = L.reconciliation_delta(sku, events, e)
        if r.get("delta"):
            out.append({"sku_id": e["sku_id"], "canonical": sku["canonical"],
                        "occurred_on": e["occurred_on"], **r})
    return {"reconciliations": out}


# ---------------------------------------------------------------------------
# Today — margin + cash/credit split + provisional + TTS
# ---------------------------------------------------------------------------
def _today_summary() -> dict:
    events = repo.all_events()
    m = L.margin_for_day(by_id(), events, TODAY)
    # week-precision sales anywhere in the current week -> "plus N purani entries"
    week_extra = 0
    for e in events:
        if e["type"] == "sale" and e.get("precision") == "week":
            d = L._d(e["occurred_on"])
            if 0 <= (TODAY - d).days <= 7:
                week_extra += 1
    m["provisional_extra"] = max(m["provisional_extra"], week_extra)
    return m


@app.get("/api/today")
def today():
    return _today_summary()


@app.get("/api/today/tts")
def today_tts():
    m = _today_summary()
    text = (f"Aaj ki total sale {int(m['total'])} rupaye. "
            f"Margin abhi tak {int(m['margin'])} rupaye. "
            f"Cash aaya {int(m['cash'])} rupaye. "
            f"Udhaar gaya {int(m['credit'])} rupaye.")
    if m["provisional_extra"]:
        text += f" Plus {m['provisional_extra']} purani entries baaki hain."
    if not sarvam_client.has_key():
        return {"text": text, "audio_b64": None}
    try:
        b64 = sarvam_client.text_to_speech(text, language_code="hi-IN")
        return {"text": text, "audio_b64": b64}
    except Exception as e:
        return {"text": text, "audio_b64": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Dashboard  (spec 9.6 — build last)
# ---------------------------------------------------------------------------
@app.get("/api/dashboard")
def dashboard():
    events = repo.all_events()
    catalogue = repo.load_catalogue()
    catalogue_by = {s["sku_id"]: s for s in catalogue}
    from datetime import timedelta

    # margin trend, last 30 days
    trend = []
    for i in range(29, -1, -1):
        d = TODAY - timedelta(days=i)
        mm = L.margin_for_day(catalogue_by, events, d)
        trend.append({"date": d.isoformat(), "margin": mm["margin"]})

    # steel price rise marker (first day a delivery rate jumps)
    rise_date = None
    for sid in ["TMT_12_FE500D_TATA"]:
        deliveries = sorted([e for e in events if e["sku_id"] == sid
                             and e["type"] == "delivery" and e.get("rate")],
                            key=lambda e: e["occurred_on"])
        for a, b in zip(deliveries, deliveries[1:]):
            if b["rate"] > a["rate"] * 1.03:
                rise_date = b["occurred_on"]
                break

    # money position — outstanding credit + inventory value at landed cost
    outstanding = sum((float(e["rate"]) *
                       L.to_base(float(e["qty"]), e.get("unit"), catalogue_by[e["sku_id"]]))
                      for e in events if e["type"] == "sale"
                      and e.get("payment") == "credit" and e.get("rate")
                      and e["sku_id"] in catalogue_by)
    inv_value = 0.0
    excluded = 0
    frozen = []
    for sku in catalogue:
        det = L._stock_detail(sku, events, TODAY)
        if det["qty"] == L.UNCOUNTED:
            excluded += 1
            continue
        cost = L.landed_cost_as_of(sku, events, TODAY) or 0
        base_qty = det.get("base") or 0
        val = cost * base_qty
        inv_value += val
        # frozen capital: counted, zero movement in 60 days
        sales = [e for e in events if e["sku_id"] == sku["sku_id"]
                 and e["type"] in ("sale", "delivery")
                 and 0 <= (TODAY - L._d(e["occurred_on"])).days <= 60]
        if not sales and base_qty > 0:
            frozen.append({"sku_id": sku["sku_id"], "canonical": sku["canonical"],
                           "value": round(val, 0),
                           "stock": _stock_view(sku, det)["display"]})
    frozen.sort(key=lambda x: -x["value"])

    return {
        "margin_trend": trend, "steel_rise_date": rise_date,
        "money_position": {"outstanding_credit": round(outstanding, 0),
                           "inventory_value": round(inv_value, 0),
                           "uncounted_excluded": excluded},
        "frozen_capital": frozen[:12],
        "frozen_total": round(sum(f["value"] for f in frozen), 0),
    }


# ---------------------------------------------------------------------------
# Invoice ingestion (spec 9.1) — catalogue + landed cost ONLY, no stock
# ---------------------------------------------------------------------------
def _compute_landed(lines: list) -> list:
    """Strip GST (use taxable value), prorate freight by value, /qty (Section 4)."""
    goods = [l for l in lines if l.get("taxable_value") and l.get("qty")
             and l.get("certain") and not l.get("is_freight")]
    total_taxable = sum(l["taxable_value"] for l in goods)
    freight_total = sum(l.get("freight_value", 0) for l in lines if l.get("is_freight"))
    catalogue = repo.load_catalogue()
    learning = repo.load_learning()
    out = []
    for l in lines:
        row = dict(l)
        if l.get("is_freight"):
            row["display"] = f"Freight ₹{l['freight_value']:.0f} (prorated by value)"
            out.append(row)
            continue
        # match to catalogue
        if l.get("product_phrase"):
            m = M.match(l["product_phrase"], catalogue, learning, "delivery")
            row["match"] = m
        if l.get("certain") and l.get("qty") and l.get("taxable_value"):
            sku = None
            mm = row.get("match", {})
            if mm.get("status") == "matched":
                sku = repo.sku(mm["sku_id"])
            base_qty = L.to_base(l["qty"], l.get("unit", "unit"), sku) if sku else l["qty"]
            prorated = freight_total * (l["taxable_value"] / total_taxable) if total_taxable else 0
            landed = (l["taxable_value"] + prorated) / base_qty if base_qty else None
            row["prorated_freight"] = round(prorated, 1)
            row["landed_cost_per_unit"] = round(landed, 2) if landed else None
            gross = l["taxable_value"] * (1 + l.get("gst_rate", 0) / 100)
            row["gross_would_be"] = round(gross / base_qty, 2) if base_qty else None
        out.append(row)
    return out


@app.post("/api/invoice")
async def invoice(file: UploadFile = File(None)):
    """
    Parse an invoice photo -> line items + landed cost. NO stock events created.
    Uses Sarvam Document Digitization when a key is present; otherwise the
    deterministic fixture so the beat is rock-solid on stage.
    """
    used_fixture = True
    lines = None
    if file is not None and sarvam_client.has_key():
        try:
            data = await file.read()
            suffix = Path(file.filename or "invoice.png").suffix or ".png"
            tmp = ROOT / "debug" / f"uploaded_invoice{suffix}"
            tmp.write_bytes(data)
            parsed = sarvam_client.parse_document(str(tmp))
            lines = _lines_from_markdown(parsed.get("markdown", ""))
            used_fixture = not bool(lines)
        except Exception as e:
            print("invoice live parse failed, using fixture:", repr(e)[:200])
            used_fixture = True
    if lines is None or used_fixture:
        fx = json.loads((DATA / "invoice_fixture.json").read_text(encoding="utf-8"))
        lines = fx["lines"]
        meta = {k: fx[k] for k in ("supplier", "invoice_no", "invoice_date", "image")}
    else:
        meta = {"supplier": "Parsed invoice", "invoice_no": "-",
                "invoice_date": TODAY.isoformat(), "image": None}

    priced = _compute_landed(lines)
    return {"used_fixture": used_fixture, "meta": meta, "lines": priced,
            "has_key": sarvam_client.has_key()}


_UNIT_MAP = {"mt": "tonne", "ton": "tonne", "tonne": "tonne", "bags": "bori",
             "bag": "bori", "bori": "bori", "pcs": "piece", "nos": "piece",
             "piece": "piece", "ltr": "litre", "litre": "litre"}


def _num(s: str):
    s = re.sub(r"[^\d.]", "", s or "")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _lines_from_table(md: str) -> list:
    """
    Deterministically parse the Document Digitization HTML table into line items.
    Reliable and fast — no LLM reasoning-token limits. GST stripping / freight
    proration happen later in _compute_landed (Section 4).
    """
    import re as _re
    rows = _re.findall(r"<tr>(.*?)</tr>", md, _re.S | _re.I)
    out, idx = [], 0
    for row in rows:
        cells = [_re.sub(r"<[^>]+>", "", c).strip()
                 for c in _re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, _re.S | _re.I)]
        cells = [c for c in cells if c != ""]
        if not cells:
            continue
        joined = " ".join(cells).lower()
        if "description" in joined or joined.startswith(("qty", "rate", "amount")):
            continue  # header
        if any(k in joined for k in ("sub total", "grand total", "cgst", "sgst",
                                     "igst", "total")):
            continue  # totals
        desc = cells[0]
        amount = _num(cells[-1]) if len(cells) >= 2 else None
        y0 = 0.27 + idx * 0.065
        bbox = [0.06, round(y0, 3), 0.94, round(y0 + 0.055, 3)]
        idx += 1
        # freight / transport
        if "freight" in joined or "transport" in joined:
            out.append({"line_id": f"L{idx}", "raw_text": " | ".join(cells),
                        "is_freight": True, "freight_value": amount or 0,
                        "certain": True, "bbox": bbox})
            continue
        # illegible / smudged
        alpha = sum(ch.isalpha() for ch in desc)
        if "smudg" in joined or "~" in desc or alpha < 3:
            out.append({"line_id": f"L{idx}", "raw_text": " | ".join(cells),
                        "product_phrase": None, "qty": None, "unit": None,
                        "taxable_value": None, "gst_rate": None, "certain": False,
                        "uncertain_reason": "row illegible — crop shown, not guessed",
                        "bbox": bbox})
            continue
        # qty + unit
        qty_cell = cells[1] if len(cells) >= 3 else ""
        qty = _num(qty_cell)
        um = _re.search(r"[a-zA-Z]+", qty_cell)
        unit = _UNIT_MAP.get(um.group(0).lower(), None) if um else None
        handwritten = bool(_re.search(r"@|/|↑|\bcorr", qty_cell)) or qty is None
        gst = 28 if "cement" in desc.lower() else 18
        line = {"line_id": f"L{idx}", "raw_text": " | ".join(cells),
                "product_phrase": desc, "qty": None if handwritten else qty,
                "unit": unit or "tonne", "taxable_value": amount,
                "gst_rate": gst, "certain": not handwritten, "bbox": bbox}
        if handwritten:
            line["uncertain_reason"] = f"handwritten/ambiguous qty: '{qty_cell}' — confirm"
            line["handwritten_note"] = qty_cell
        out.append(line)
    return out


def _lines_from_markdown(md: str) -> list:
    """
    Structure parsed invoice text into line items. Deterministic HTML-table
    parse first (Document Digitization returns clean tables); Sarvam-30B only if
    no table is present.
    """
    tbl = _lines_from_table(md)
    if tbl:
        return tbl
    if not md.strip() or not sarvam_client.has_key():
        return []
    sys_p = ("You convert an invoice into JSON. Do NOT explain or analyse — output "
             "ONLY the JSON object, nothing else. Schema: "
             '{"lines":[{"product_phrase":str,"qty":number|null,"unit":str,'
             '"taxable_value":number,"gst_rate":number,"certain":bool,'
             '"is_freight":bool,"freight_value":number}]}. '
             "Use the Amount column as taxable_value (pre-GST). Illegible/smudged "
             "rows: certain=false. Transport/Freight rows: is_freight=true with "
             "freight_value. Keep any handwritten/struck value in product_phrase "
             "and set certain=false. Be terse.")
    try:
        out = sarvam_client.chat_json(
            [{"role": "system", "content": sys_p},
             {"role": "user", "content": md[:6000]}])
        return out.get("lines", [])
    except Exception:
        return []


@app.post("/api/invoice/commit")
def invoice_commit(payload: dict = Body(...)):
    """
    Commit chosen invoice lines -> catalogue rows + landed cost ONLY.
    Explicitly does NOT create delivery/stock events (spec 9.1 CRITICAL).
    Returns the top-30-by-value count checklist.
    """
    lines = payload.get("lines", [])
    catalogue_by = by_id()
    committed = []
    for l in lines:
        m = l.get("match", {})
        sku_id = m.get("sku_id") if m.get("status") == "matched" else l.get("sku_id")
        landed = l.get("landed_cost_per_unit")
        if sku_id and sku_id in catalogue_by and landed:
            # update cost basis only
            repo.upsert_sku({"sku_id": sku_id, "landed_cost_per_kg": landed,
                             "opening_cost_per_kg": landed})
            committed.append({"sku_id": sku_id, "landed_cost": landed,
                              "value": l.get("taxable_value")})
    # top-30 checklist ranked by purchase value; flag which need counting
    events = repo.all_events()
    checklist = []
    ranked = sorted([c for c in committed if c.get("value")],
                    key=lambda c: -c["value"])
    for c in ranked[:30]:
        sku = repo.sku(c["sku_id"])
        det = L._stock_detail(sku, events, TODAY)
        checklist.append({"sku_id": c["sku_id"], "canonical": sku["canonical"],
                          "purchase_value": c["value"],
                          "needs_count": det["qty"] == L.UNCOUNTED,
                          "stock": _stock_view(sku, det)["display"]})
    return {"committed": committed, "checklist": checklist,
            "note": "Catalogue + landed cost updated. No stock quantities created."}


# ---------------------------------------------------------------------------
# GST invoice PDF (bill trigger)
# ---------------------------------------------------------------------------
@app.post("/api/bill")
def bill(payload: dict = Body(...)):
    from reportlab.lib.pagesizes import A5
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm

    items = payload.get("items", [])  # [{sku_id, qty, unit, rate, payment}]
    customer = payload.get("customer") or {}
    if customer.get("customer_id") and not customer.get("name"):
        customer = repo.customer(customer["customer_id"]) or customer
    catalogue_by = by_id()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A5)
    w, h = A5
    y = h - 20 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(15 * mm, y, "Sharma Building Materials")
    c.setFont("Helvetica", 8)
    y -= 6 * mm
    c.drawString(15 * mm, y, "Tax Invoice (GST)")
    c.drawRightString(w - 15 * mm, y, f"Date: {TODAY.isoformat()}")
    if customer.get("name") or customer.get("phone"):
        y -= 5 * mm
        c.setFont("Helvetica-Bold", 8)
        c.drawString(15 * mm, y, f"Customer: {customer.get('name') or 'Unnamed'}")
        c.setFont("Helvetica", 8)
        c.drawRightString(w - 15 * mm, y,
                          f"Contact: {customer.get('phone') or '-'}")
    y -= 4 * mm
    c.line(15 * mm, y, w - 15 * mm, y)
    y -= 6 * mm
    c.setFont("Helvetica-Bold", 8)
    c.drawString(15 * mm, y, "Item")
    c.drawString(90 * mm, y, "Qty")
    c.drawString(105 * mm, y, "Rate")
    c.drawRightString(w - 15 * mm, y, "Amount")
    y -= 2 * mm
    c.line(15 * mm, y, w - 15 * mm, y)
    y -= 6 * mm
    c.setFont("Helvetica", 8)
    subtotal = gst_total = 0.0
    for it in items:
        sku = catalogue_by.get(it["sku_id"], {})
        amount = (L.line_amount(
            float(it["qty"]), it.get("unit", "unit"), float(it["rate"]),
            it.get("rate_unit"), sku)
            if sku else float(it["rate"]) * float(it["qty"]))
        gst = amount * repo.gst_rate_for(sku) / 100
        subtotal += amount
        gst_total += gst
        c.drawString(15 * mm, y, (sku.get("canonical", it["sku_id"]))[:42])
        c.drawString(90 * mm, y, f'{it["qty"]}{it.get("unit","")}')
        rate_label = f'{it["rate"]}'
        if it.get("rate_unit"):
            rate_label += f'/{it["rate_unit"]}'
        c.drawString(105 * mm, y, rate_label)
        c.drawRightString(w - 15 * mm, y, f"{amount:,.0f}")
        y -= 5 * mm
    y -= 2 * mm
    c.line(15 * mm, y, w - 15 * mm, y)
    y -= 6 * mm
    c.drawRightString(w - 15 * mm, y, f"Subtotal: {subtotal:,.0f}")
    y -= 5 * mm
    c.drawRightString(w - 15 * mm, y, f"GST: {gst_total:,.0f}")
    y -= 5 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(w - 15 * mm, y, f"Total: {subtotal + gst_total:,.0f}")
    c.showPage()
    c.save()
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": "inline; filename=invoice.pdf"})


# ---------------------------------------------------------------------------
# Demo reset
# ---------------------------------------------------------------------------
@app.post("/api/reset")
def reset():
    import seed
    seed.main()
    global repo
    repo = JsonRepo()
    return {"ok": True, "message": "Reset to clean demo state."}


if __name__ == "__main__":
    if "--demo" in sys.argv:
        import seed
        seed.main()
        print("Demo state reset.")
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)
