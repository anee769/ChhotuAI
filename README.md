# ChhotuAI — Voice-First Stock, Sales & Customer Ledger

> *Every inventory system assumes discipline. We assume forgetting.*

ChhotuAI is a focused ledger for Indian hardware and building-material shops.
The owner can record sales and deliveries in Hindi mixed with English product
terms, manage customer credit, record later payments, and review stock and
margin without maintaining a conventional inventory database.

Stock is always derived by replaying an append-only event log; it is never
stored as a mutable `current_stock` value. AI features use Sarvam AI only:
Saaras STT, Bulbul TTS, Sarvam-30B chat/tool calling, and Sarvam Document
Digitization.

## Current features

- Clean reference-style interface with seven focused workspaces: Voice Entry,
  Delivery, Invoice, Inventory, Customers, Today, and Dashboard.
- Stateful voice conversations that retain the complete turn history and ask
  only for information that is still missing.
- Multi-item sale capture from one sentence. All items are kept together in a
  single transaction and a single GST bill.
- A complete preview—customer, phone, payment type, deadline, item quantities,
  rates, line amounts, and grand total—before the transaction is written.
- Customer lookup by contact number for both cash and credit sales. A new
  contact is asked for a name once and then reused.
- Credit balances and payment deadlines per customer, with every later payment
  preserved as a separate receipt.
- Customer name and contact number on generated bills.
- Today view with total sales, margin, cash/credit split, and Total plus Margin
  columns in the “Aaj ki bikri” table.
- Inventory and frozen-capital tables, plus margin and money-position
  dashboards.
- Invoice digitization with landed-cost calculation and a count checklist.
  Parsing an invoice does not silently add stock.

Customer reminder notifications are intentionally not part of the application.
There are no SMS credentials, notification worker, outbox, or reminder API.

## Run locally

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Add the Sarvam key to a `.env` file:

```dotenv
SARVAM_API_KEY=sk_...
```

The application loads `.env` automatically. Start it with:

```bash
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

The ledger, customer, Today, and Dashboard screens can be inspected without a
Sarvam key. A key is required for the conversational Voice Entry and Delivery
flows, live speech-to-text, text-to-speech, semantic product matching, and live
invoice digitization.

### Reset demo data

```bash
.venv/bin/python backend/main.py --demo
```

The same action is available through `POST /api/reset`.

**Warning:** reset replaces the current catalogue, events, customers,
receivables, and payments with the bundled demo data. The seed contains six
products, about six weeks of ledger activity, six customers, frozen capital,
uncounted stock, and sample payment history.

## Tests

Run the focused conversation regressions:

```bash
.venv/bin/python -m unittest backend/test_conversation.py -v
```

These cover multi-item recovery, Hindi quantities and units, customer details,
cash and credit paths, relative credit periods, named deadlines, explicit
confirmation, and preventing ledger or receivable writes before confirmation.

The end-to-end acceptance script needs a running server:

```bash
.venv/bin/python backend/acceptance.py
```

**Warning:** the acceptance script resets the running application to demo data
before executing its integration checks.

## Voice transaction flow

1. The user speaks or types a sale or delivery containing one or more items.
2. Sarvam extracts the structured transaction once. Follow-up answers update
   the same draft instead of sending the whole transaction through repeated
   model calls.
3. Chhotu asks only for unresolved product details, quantity, unit, rate, or
   payment type.
4. For every sale, Chhotu asks for the customer contact number. An existing
   number reuses the saved name; a new number asks for the name.
5. Cash entries proceed to review. Credit entries additionally ask for a
   payment deadline.
6. A confirmation card shows every captured detail and the combined total.
   No sale/delivery event or receivable is written until **Confirm Entry** is
   pressed. A newly identified customer profile may already have been created
   so it can be shown in the preview.
7. One confirmed multi-item sale creates one bill containing all items and the
   customer name and contact number.

Deadlines accept exact dates, `DD/MM/YYYY`, relative periods such as “10 days”,
“two weeks”, or “one month”, Hindi terms such as `kal` and `parso`, and named
dates such as “15 August”.

## Customer credit and payments

Cash and credit sales both remain linked to the selected customer. Credit sales
also create a receivable with the chosen deadline. Recording a later payment
appends a new immutable payment row; it does not overwrite or merge an older
payment from the same customer. The customer screen shows the outstanding
balance, open dues, sale history, and each payment receipt separately.

The application does not contact customers automatically. Deadlines and overdue
balances remain visible in the Customers workspace for the shop owner to act
on.

## Workspace guide

| Workspace | Purpose |
|---|---|
| Voice Entry | Record cash or credit sales by voice or text, review, and confirm |
| Delivery | Record incoming stock through the same stateful conversation flow |
| Invoice | Digitize supplier invoices and review landed costs without creating stock |
| Inventory | View event-derived stock, cost, selling price, margin, and count status |
| Customers | Search customers, review dues, and append individual payments |
| Today | Review total sales, margin, cash, credit, and every sale line |
| Dashboard | Review trends, money position, and frozen capital |

## Sarvam API integration

The API contract was last verified on 2026-07-26. Every Sarvam request uses the
`api-subscription-key` header.

| Capability | Endpoint | Model | Notes |
|---|---|---|---|
| Speech-to-text | `POST /speech-to-text` | `saaras:v3` | Multipart audio; Voice Entry uses code-mixed transcription |
| Text-to-speech | `POST /text-to-speech` | `bulbul:v3` | Used for spoken prompts and the Hindi Today summary |
| Chat and tools | `POST /v1/chat/completions` | `sarvam-30b` | Structured sale/delivery extraction and semantic matching |
| Document parsing | `sarvamai` SDK `document_intelligence` | Sarvam Vision | Asynchronous create, upload, start, wait, and download job |

Implementation notes:

- STT transliteration mode is `translit`, not `transliterate`.
- Chat uses `/v1/chat/completions`; STT and TTS have no `/v1/` prefix.
- Document parsing uses the official `sarvamai` SDK. The old synchronous Vision
  page-parsing endpoint is not used.
- Invoice rows are structured deterministically from the digitized HTML table,
  avoiding a second LLM pass over the entire invoice.
- Voice conversation calls have a bounded timeout and token budget. Local
  follow-up slot handling, deterministic analytics routing, and a multi-item
  recovery guard keep the interaction responsive and prevent omitted items.
- Debug responses can be retained with `DEBUG_SAVE_RESPONSES`.

## Architecture

- `backend/ledger.py` — replays events for stock, cost basis, daily margin,
  reconciliation deltas, and `UNCOUNTED` stock.
- `backend/conversation.py` — stateful voice orchestration, conversation
  history, missing-field questions, customer/payment collection, multi-item
  recovery, deadline parsing, preview, and explicit commit.
- `backend/matcher.py` — normalized aliases, fuzzy matching, learned
  confirmations, semantic reranking, and product disambiguation.
- `backend/crm.py` — customer profiles, receivables, outstanding balances, and
  immutable payment records.
- `backend/repo.py` — narrow JSON repository interface with atomic writes.
- `backend/sarvam_client.py` — Sarvam STT, TTS, chat, and document client.
- `backend/main.py` — FastAPI routes, transaction commit, Today/Dashboard
  summaries, invoices, customer APIs, and bill generation.
- `backend/seed.py` — deterministic catalogue, event, customer, credit, and
  payment demo data.
- `frontend/index.html` — the complete responsive single-page interface.
- `backend/test_conversation.py` — regression tests for the current voice flow.

## Ledger rules

- Stock is derived from purchase, delivery, sale, and stock-count events.
- `UNCOUNTED` is distinct from zero.
- Backdated events recompute the ledger correctly.
- Margin uses last-purchase (replacement) cost.
- Unknown-date or week-precision entries remain auditable and do not pretend to
  be exact Today totals.
- Invoice analysis never creates stock until a separate inventory action is
  confirmed.

## Known limitations

Trade schemes and credit notes, FIFO or weighted-average costing, a hosted
database such as Supabase, and streaming full-duplex voice are outside the
current scope. The current repository uses local JSON files and a
request-response voice conversation.
