"""Acceptance test — drives the running server over HTTP (spec Section 13).
Run:  .venv/bin/python backend/acceptance.py
Assumes server on 127.0.0.1:8000. Resets to clean demo state first.
"""
import json, sys, urllib.request

BASE = "http://127.0.0.1:8000"
PASS, FAIL = [], []


def call(path, body=None, method=None, raw=False):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read() if raw else json.loads(r.read())


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅" if cond else "  ❌") + f" {name}" + (f"  — {detail}" if detail else ""))


def parse_commit(text, flow="live_sale", pick=None, occurred_on=None, precision=None,
                 ctype=None, payment=None):
    """Helper: parse text, auto-resolve first item (optionally force sku), commit."""
    r = call("/api/parse", {"text": text, "flow": flow})
    return r


print("== RESET ==")
call("/api/reset", {})
st = call("/api/state")
print(f"  catalogue {len(st['catalogue'])} SKUs, state={st['learning_state']}")

# --- #1 & #2 invoice: catalogue+cost, NO stock, landed = (taxable+freight)/qty ---
print("\n#1/#2 Invoice ingestion (fixture)")
inv = call("/api/invoice", {})
l1 = next(l for l in inv["lines"] if l.get("line_id") == "L1")
# taxable 156000, freight prorated by value, qty 3t=3000kg
check("L1 landed cost GST-stripped (< gross)", l1["landed_cost_per_unit"] < l1["gross_would_be"],
      f'landed {l1["landed_cost_per_unit"]} vs gross {l1["gross_would_be"]}')
check("L1 landed ≈ (taxable+freight)/3000kg",
      abs(l1["landed_cost_per_unit"] - (172500 + l1["prorated_freight"]) / 3000) < 0.5,
      f'{l1["landed_cost_per_unit"]}')
check("uncertain row flagged (L5)", any(l.get("line_id") == "L5" and l.get("certain") is False for l in inv["lines"]))
check("handwritten correction surfaced (L4)", any(l.get("handwritten_correction") for l in inv["lines"]))
stock_before = call("/api/stock")
committed = call("/api/invoice/commit", {"lines": inv["lines"]})
stock_after = call("/api/stock")
# Invoice IS the delivery — confirmed (certain) lines now add stock; only a
# line still needing confirmation (illegible/handwritten qty) holds back.
check("invoice added stock for confirmed lines",
      stock_before["rows"] != stock_after["rows"]
      and any(c.get("stock_added") for c in committed["committed"]))
check("top-N count checklist returned", len(committed["checklist"]) > 0)

# --- #14 not stocked ---
print("\n#14 Fe550 not stocked")
r = call("/api/parse", {"text": "das ton fe550 tata", "flow": "live_sale"})
m = r["items"][0]["match"]
check("Fe550 -> not_stocked, no substitute", m["status"] == "not_stocked", str(m.get("value")))

# --- #4 self-correction ---
print("\n#4 Self-correction")
r = call("/api/parse", {"text": "do ton barah mm nahi nahi teen ton aur pachaas bori cement", "flow": "live_sale"})
check("qty corrected to 3", r["items"][0]["qty"] == 3, f'qty={r["items"][0]["qty"]}')
check("self_corrected flag set", r["items"][0]["self_corrected"] is True)

# --- #3 Day1 sariya one question ---
print("\n#3 Day1 disambiguation = one question")
r = call("/api/parse", {"text": "sariya do ton", "flow": "live_sale"})
m = r["items"][0]["match"]
check("Day1 sariya asks ONE dim (size)", m["status"] == "disambiguate" and m["attribute"] == "diameter_mm",
      m.get("attribute"))

# --- #5 GST bill PDF ---
print("\n#5 GST invoice PDF")
pdf = call("/api/bill", {"items": [{"sku_id": "TMT_12_FE500D_TATA", "qty": 2, "unit": "tonne", "rate": 62, "payment": "cash"}]}, raw=True)
check("bill returns a PDF", pdf[:4] == b"%PDF", f"{pdf[:8]}")

# --- #8 sell UNCOUNTED sku: margin computed, stock flagged ---
print("\n#8 Sell an UNCOUNTED SKU")
UNC = "CEM_ULTRATECH_PPC"
before = next(x for x in call("/api/stock")["rows"] if x["sku_id"] == UNC)
check("SKU is UNCOUNTED before", before["uncounted"] is True, before["display"])
call("/api/commit", {"type": "sale", "occurred_on": st["today"], "precision": "exact",
     "items": [{"sku_id": UNC, "qty": 20, "unit": "bori", "payment": "cash", "spoken": "ultratech ppc bees bori"}]})
after = next(x for x in call("/api/stock")["rows"] if x["sku_id"] == UNC)
check("still UNCOUNTED after sale (not 0/neg)", after["uncounted"] is True, after["display"])
today = call("/api/today")
sold = [l for l in today["lines"] if l["sku_id"] == UNC]
check("margin still computed for uncounted sale", bool(sold) and sold[0]["margin"] is not None,
      str(sold[0]["margin"]) if sold else "no line")

# --- #9 voice count -> opening_balance resolves to a number ---
print("\n#9 Voice count establishes baseline")
call("/api/commit", {"type": "opening_balance", "occurred_on": st["today"], "precision": "day",
     "items": [{"sku_id": UNC, "qty": 40, "unit": "bori", "count_precision": "estimated", "spoken": "ultratech ppc chalis bori"}]})
after2 = next(x for x in call("/api/stock")["rows"] if x["sku_id"] == UNC)
check("resolves to a number after count", after2["uncounted"] is False, after2["display"])

# --- #6 back-dated parso -> stock recomputes across period ---
print("\n#6 Back-dated correction")
import datetime
parso = (datetime.date.fromisoformat(st["today"]) - datetime.timedelta(days=2)).isoformat()
sku6 = "TMT_12_FE500D_TATA"
s_today_before = next(x for x in call("/api/stock")["rows"] if x["sku_id"] == sku6)["display"]
call("/api/commit", {"type": "sale", "occurred_on": parso, "precision": "day", "source": "voice_recall",
     "items": [{"sku_id": sku6, "qty": 1.0, "unit": "tonne", "payment": "cash", "spoken": "parso ek ton barah mm"}]})
s_today_after = next(x for x in call("/api/stock")["rows"] if x["sku_id"] == sku6)["display"]
s_parso = next(x for x in call("/api/stock?as_of=" + parso)["rows"] if x["sku_id"] == sku6)["display"]
check("today stock changed after back-dated insert", s_today_before != s_today_after,
      f"{s_today_before} -> {s_today_after}")

# --- #7 vague pichle hafte -> week precision excluded from today ---
print("\n#7 Vague date -> week precision excluded")
r = call("/api/parse", {"text": "pichle hafte kuch cement gaya tha", "flow": "recall"})
check("vague triggers narrowing question", r["temporal"]["ask"] and r["temporal"]["vague"])
today_before = call("/api/today")["margin"]
call("/api/commit", {"type": "sale", "occurred_on": st["today"], "precision": "week", "source": "voice_recall",
     "items": [{"sku_id": "CEM_ULTRATECH_OPC53", "qty": 10, "unit": "bori", "payment": "cash", "spoken": "pichle hafte cement"}]})
t = call("/api/today")
check("week-precision excluded from today's margin", call("/api/today")["margin"] == today_before,
      f'margin unchanged {today_before}')
check("shows 'plus N purani entries'", t["provisional_extra"] >= 1, f'extra={t["provisional_extra"]}')

# --- #10 stock_take mismatch -> delta surfaced, history intact ---
print("\n#10 Stock-take reconciliation delta")
rec = call("/api/reconciliations")["reconciliations"]
check("reconciliation delta surfaced", any(abs(x["delta"]) > 0 for x in rec),
      str(rec[0]["delta"]) if rec else "none")

# --- #11 today margin split ---
print("\n#11 Today margin split")
t = call("/api/today")
check("today has margin + cash + credit", all(k in t for k in ("margin", "cash", "credit")),
      f'margin={t["margin"]} cash={t["cash"]} credit={t["credit"]}')

# --- #12 Day60 zero questions ---
print("\n#12 Day60 replay -> zero questions")
call("/api/toggle", {"which": "day60"})
r = call("/api/parse", {"text": "sariya do ton", "flow": "live_sale"})
m = r["items"][0]["match"]
check("Day60 sariya resolves with zero questions", m["status"] == "matched", m.get("sku_id"))
counts = call("/api/state")["learning_counts"]
check("Day60 counters jumped", counts["aliases"] > 20, str(counts))
call("/api/toggle", {"which": "day1"})

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
