# ChhotuAI — Voice-First Stock & Margin Ledger

> *Every inventory system assumes discipline. We assume forgetting.*

A voice-first stock and margin ledger for an Indian hardware / building-material
shop. The owner speaks Hindi code-mixed with English product terms; entries can
be made live, at closing, or three days later, in any order, with vague dates —
and the ledger stays correct because **stock is derived from an append-only
event log, never stored**.

All AI capability is **Sarvam AI only** (Saaras STT, Bulbul TTS, Sarvam-30B
chat + tool calling, Sarvam Document Digitization). No other models, no
embeddings.

---

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export SARVAM_API_KEY=sk_...          # optional — app runs without it (see below)
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000

Reset to a clean demo state at any time:

```bash
.venv/bin/python backend/main.py --demo      # or POST /api/reset
```

Run the acceptance test (server must be running):

```bash
.venv/bin/python backend/acceptance.py        # 23 checks, all green
```

### Customer credit and SMS reminders

Every sale conversation asks for the customer's phone number. Existing numbers
reuse the saved name; new numbers ask for a name. Cash sales close immediately.
Credit sales create a customer receivable with a payment deadline, and later
payments are appended to a separate payment ledger.

Deadline reminders are checked hourly and two days before the due date. Without
an SMS gateway, reminders remain safely visible in the local notification
outbox. To send them automatically, install the open-source
[SMS Gateway for Android](https://github.com/capcom6/android-sms-gateway) on a
shop Android phone, enable its Local Server, and configure:

```bash
export SMSGATE_ENDPOINT=http://PHONE_IP:8080/message
export SMSGATE_USERNAME=the_username_shown_in_the_app
export SMSGATE_PASSWORD=the_password_shown_in_the_app
```

The gateway software has no registration fee in local mode. Messages use the
phone's SIM, so the mobile operator's normal SMS plan may apply.

### Running without a Sarvam API key

The demo is fully operable with **no key**: type phrases in the text box instead
of speaking, and the invoice beat uses a deterministic hard-invoice fixture.
With a key, the mic (audio upload), spoken Hindi Today-summary, live invoice
OCR, and the Sarvam-30B semantic matcher stage all activate. **No beat depends
on the key** — this is deliberate (never let the best beat depend on the
riskiest integration).

---

## Section 1 — verified Sarvam API contract (2026-07-26)

Auth header `api-subscription-key` on every endpoint.

| Capability | Endpoint | Model | Notes |
|---|---|---|---|
| Speech-to-text | `POST /speech-to-text` | `saaras:v3` | multipart; modes `transcribe/translate/verbatim/translit/codemix` |
| Text-to-speech | `POST /text-to-speech` | `bulbul:v3` | v3 controls `pace`+`temperature` only (pitch/loudness are v2) |
| Chat + tools | `POST /v1/chat/completions` | `sarvam-30b` | OpenAI-compatible tool calling; **note the `/v1/` prefix** |
| Document parse | `sarvamai` SDK `document_intelligence` | Sarvam Vision | **batch job** (create→upload→start→wait→download zip); no sync endpoint |

**Deviations from the original spec, per §14 ("follow the API and tell me"):**

- STT transliterate mode is **`translit`**, not `transliterate`.
- Chat path is **`/v1/chat/completions`** (STT/TTS have no version prefix).
- The synchronous "Sarvam Vision parse a page" endpoint **has been removed**.
  Document parsing is now the async **Document Digitization batch job** via the
  official `sarvamai` SDK. We use the SDK for that one capability and raw HTTP
  (with retries / 20s timeout / `DEBUG_SAVE_RESPONSES`) for STT/TTS/chat.
- pip package is **`sarvamai`**, not `sarvam-ai`.
- **`sarvam-30b` is a reasoning model** — responses carry `reasoning_content`
  and only then `content`; a small token budget truncates it mid-reasoning
  (`finish_reason=length`, `content=null`). The **starter tier caps
  `max_tokens` at 4096**, which is too small for it to reason over a full
  invoice *and* emit JSON. So invoice structuring parses the Document
  Digitization **HTML table deterministically** (no LLM) — reliable and fast;
  the LLM tool-call is still used for sale utterances, where it responds
  quickly. `chat_json` also falls back to `reasoning_content`.

**Live paths verified with a real key:** TTS↔STT round-trip, Sarvam-30B
tool-call sale parsing, and Document Digitization on the hard invoice all
confirmed working end-to-end. The key lives in `.env` (gitignored); the app
loads it automatically.

---

## Architecture

- **`backend/ledger.py`** — `stock_at()` replays the event log; returns a number
  or `UNCOUNTED` (never 0). Confidence, last-purchase cost basis, daily margin,
  reconciliation delta. **No `current_stock` field anywhere.**
- **`backend/repo.py`** — `JsonRepo` behind a narrow `Repo` interface, flushed
  atomically on every write (survives a server kill mid-demo).
- **`backend/matcher.py`** — 4 stages: normalize → exact alias (incl. multiword
  learned) → rapidfuzz → Sarvam-30B semantic rerank. Confidence gates per flow,
  family→SKU disambiguation that asks only about open dimensions.
- **`backend/sarvam_client.py`** — one wrapper over all Sarvam capability.
- **`backend/seed.py`** — 53 SKUs (variant explosion), ~6 weeks of events with a
  steel price rise, frozen capital, UNCOUNTED SKUs, a stock-take mismatch.
- **`frontend/index.html`** — single page, six tabs, projector-friendly.

---

## Demo script (the beats)

1. **Invoice** → *Use sample hard invoice* → landed cost = (taxable + prorated
   freight) ÷ qty, GST stripped; UNCERTAIN row flagged; handwritten 2→3
   surfaced; **no stock created**; top-N count checklist.
2. **Sale**, Day 1 → *"sariya do ton"* → one "Kaunsa size?" question → tap 12mm
   → tap the Tata Fe500D → confirm. Cash → **Bill** button (GST PDF).
3. **Sale** → *"do ton barah mm nahi nahi teen ton…"* → qty shows 2→3.
4. **Sale** → *"parso ek ton barah mm gaya tha"* → day chips → confirm →
   **stock recomputes**, prior number flashes to the new value.
5. **Sale** → *"pichle hafte kuch cement gaya tha"* → narrowing question →
   *Pata nahi* → week precision, excluded from today's margin.
6. Sell an **UNCOUNTED** SKU → margin still computed, stock cell stays
   *"abhi tak gina nahi"*. Then **Stock & Count** → voice count → resolves to a
   number. The seeded stock-take shows the reconciliation delta.
7. **Today** → margin / cash / udhaar split, "abhi tak ka", *Bolo (Hindi)*.
8. **Day 60** toggle → replay *"sariya do ton"* → resolves with **zero
   questions**; counters jump.
9. **Dashboard** → margin trend (steel rise marked), money position, frozen
   capital.

## Known gaps (deliberate)

Trade-scheme / credit-note modelling, FIFO/weighted-average costing, Supabase
swap, and real-time conversation mode are out of scope per the spec. Cost basis
uses **last-purchase (replacement) cost**, stated deliberately.
