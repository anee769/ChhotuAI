# Chhotu.ai

### Awaaz se hisaab

Chhotu.ai is a voice-first **warehouse and shop operating system** for Indian
hardware, building-material, and inventory-led businesses.

It brings daily operations into one calm workspace: catalogue, stock movement,
sales, purchases, physical counts, customer credit, payments, invoices,
billing, reminders, margin, low stock, and frozen capital. Its multilingual
voice experience supports every language available through Sarvam AI Agents,
including natural code-mixed speech. The owner can speak naturally in a
supported language and does not need to learn conventional ERP screens.

> Most inventory systems assume perfect data entry. Chhotu.ai is designed for
> the way a real shop operates: people speak, forget, correct themselves, sell
> on credit, receive partial payments, and count stock later.

## Product principles

- **Voice is the operating interface.** The assistant can answer questions and
  perform approved actions through grounded backend tools.
- **Multilingual by design.** Chhotu supports all languages provided by Sarvam
  AI Agents, understands code-mixed conversations, and automatically replies
  in the language used by the owner.
- **Self-learning for every shop.** Confirmed product matches and corrections
  teach Chhotu that shop's aliases, preferred units, and attribute patterns, so
  repeated work needs fewer clarification questions over time.
- **The ledger is the source of truth.** Current stock is derived by replaying
  immutable events; there is no editable `current_stock` number.
- **AI interprets; deterministic code calculates.** Product resolution,
  quantities, dates, money, GST, stock, margin, and credit are validated by the
  backend rather than trusted to model memory.
- **No silent writes.** Transactions and outbound messages require confirmation.
- **Every shop is isolated.** Operational rows are tenant-scoped in PostgreSQL.
- **The interface stays focused.** It exposes shop work, not generic CRM or ERP
  features that do not help the owner run the day.

## Current capabilities

### Voice operations

The primary assistant is a multilingual Sarvam Samvaad v6 voice-to-voice
agent. It maintains context for the active job, supports interruptions,
supports the complete language set available to Sarvam AI Agents—including
mixed-language conversations—and calls 27 grounded HTTP tools for:

- shop profile and catalogue queries;
- stock checks, product search, item details, low-stock alerts, and stock value;
- sales, incoming purchases, and physical stock counts;
- customer accounts, outstanding dues, and payment recording;
- daily, weekly, monthly, and custom-period business summaries;
- top-selling, slow-moving, and margin-ranked products;
- product creation, editing, and safe deletion;
- bill and summary previews inside the app;
- WhatsApp bills, summaries, and credit reminders.

The assistant does not retain old chat threads in the UI. Ending a conversation
releases the microphone, refreshes operational data, and returns Voice Entry to
its clean starting state.

### Self-learning shop intelligence

- Every confirmed product match can become a shop-specific spoken alias.
- Manual corrections preserve the chosen SKU and rejected alternatives for
  better future resolution.
- Confirmed units and product attributes build per-shop priors that reduce
  repeated questions as usage grows.
- Learning is tenant-scoped: one shop's vocabulary never affects another shop.
- Learning is continuous; there is no Day 1/Day 60 mode or manual reset toggle.
- Confirmed aliases are also preserved as explainable relationships in the
  tenant's knowledge graph.
- Learning improves interpretation and matching only. Stock, rates, GST,
  margin, credit, and other business calculations remain deterministic.

### Inventory and catalogue

- Event-derived stock across sales, deliveries, purchases, opening balances,
  and stock takes.
- Product search by name, SKU, brand, aliases, normalized words, and learned
  shop terminology.
- Units and conversions for bags, kilograms, tonnes, boxes, pieces, and
  product-specific packaging.
- Cost, selling rate, GST, count precision, and stock status.
- Low-stock reporting when counted stock falls below the configured operating
  threshold.
- Frozen-capital reporting for counted inventory with no recent movement.
- Catalogue administration from Voice Entry and the Inventory workspace.
- Safe deletion: a product can be removed only when no ledger event references
  it, preserving historical integrity.

### Sales, purchases, and billing

- Multi-item voice capture, including self-corrections and mixed Hindi/English
  product phrases.
- Cash and credit sales linked to a customer.
- One combined bill for multiple items.
- Rates, unit-aware line amounts, GST, subtotal, and total calculated by code.
- Customer name and contact number included in bill data.
- In-app bill preview through `show_bill`; previewing does not send anything.
- Explicit WhatsApp permission followed by a separate `send_bill` action.
- GST invoice PDF generation and short-lived, token-protected document links.
- Incoming purchase and supplier-invoice flows that update catalogue cost and
  add confirmed delivered quantities to the event ledger.

### Customers, credit, and collection

- Customer search by name or phone number.
- Customer profiles reused across both cash and credit sales.
- Receivables with payment deadlines.
- FIFO allocation of later payments against open credit.
- Every payment kept as a separate immutable receipt.
- Outstanding balance, next deadline, open dues, and payment history.
- A **Send reminder** button on each customer with outstanding credit.
- Scheduled reminders for credit due within two days.
- Voice-triggered reminder sending through the agent.

WhatsApp is implemented with Twilio. The sandbox is suitable for demos, but a
recipient must join the sandbox before Twilio can deliver to that number.
Delivery status is checked before the UI or agent claims success.

### Invoice intelligence

- Supplier invoice upload as image or PDF.
- Sarvam document digitization with deterministic table extraction and an LLM
  fallback for difficult layouts.
- Product matching against the shop catalogue.
- Landed-cost calculation with freight allocation.
- Provisioning of genuinely new products.
- Confirmed invoice quantities create delivery events; uncertain or illegible
  quantities are held until the owner resolves them.

### Operational reporting

- Today: total sale, gross margin, cash, credit, and each sale line.
- Dashboard: sales trend, money position, outstanding credit, inventory value,
  and frozen capital.
- Low-stock products included in daily summaries.
- Voice and in-app summaries for day, yesterday, week, month, a number of days,
  or an exact date range.
- `show_summary` displays the latest summary in Voice Entry without sending it.
- Only the latest bill or summary preview is visible, and it dismisses
  automatically after 20 seconds.

### Accounts and tenancy

- Mobile-number and password signup/sign-in.
- Owner identity collected during signup.
- Company name, GSTIN, and address collected during onboarding/settings.
- Passwords stored as salted PBKDF2 hashes.
- Session tokens stored as hashes and scoped to a single user.
- Every SKU, event, customer, receivable, payment, learned memory, document,
  configuration, and voice presentation belongs to one tenant.
- Agent calls resolve the tenant through a verified caller number or a signed
  per-shop key. The backend never guesses a shop.

## Workspaces

| Workspace | Operational purpose |
| --- | --- |
| Voice Entry | Talk to Chhotu, record work, ask questions, and review the latest bill or summary |
| Invoice | Digitize supplier invoices, resolve matches, calculate landed cost, and add confirmed stock |
| Inventory | Search stock, review count/status/cost, and manage the catalogue |
| Customers | Search accounts, review dues and receipts, record payments, and send reminders |
| Today | Review the day’s sale, margin, cash/credit split, and individual sale lines |
| Dashboard | Review trends, inventory value, outstanding money, low stock, and frozen capital |
| Settings | Maintain company and billing details |

## How the system fits together

```mermaid
flowchart LR
    Owner["Owner: browser or phone"] --> Voice["Samvaad v6 voice runtime"]
    Voice --> Tools["Grounded agent tools"]
    Owner --> UI["Chhotu.ai web app"]
    UI --> API["FastAPI application"]
    Tools --> API
    API --> Ledger["Deterministic ledger, CRM, billing, analytics"]
    Ledger --> DB[("PostgreSQL / Neon")]
    API --> Sarvam["Sarvam STT, TTS, chat, documents"]
    API --> Twilio["Twilio WhatsApp"]
    Tools --> Preview["Short-lived bill and summary presentation"]
    Preview --> UI
```

### Voice session sequence

1. The authenticated browser requests `GET /api/voice/session`.
2. The backend returns non-secret Samvaad configuration and a shop-specific key.
3. The browser SDK requests a signed connection through the authenticated
   `/api/voice/samvaad/.../url` proxy.
4. The proxy uses the server-only `SAMVAAD_API_KEY`; the long-lived key never
   enters browser JavaScript.
5. Samvaad streams microphone and speaker audio and calls
   `/api/agent/tool/{tool_name}` when it needs shop data or an action.
6. The backend authenticates the tool, resolves the exact tenant, validates the
   arguments, and returns structured facts.
7. `show_bill` and `show_summary` create short-lived presentation rows that the
   active browser polls and displays. Send tools remain separate.
8. Ending the interaction clears the presentation and conversation UI.

If live Samvaad is unavailable, the web app falls back to the earlier
record/transcribe/converse flow so basic Voice Entry remains usable.

## Ledger and accounting rules

- Stock is replayed from append-only events.
- `UNCOUNTED` is different from zero.
- Backdated events recompute later stock and reporting.
- Margin uses the most recent landed/replacement cost, not FIFO or weighted
  average costing.
- Unit-aware pricing prevents tonne, kilogram, bag, and piece rates from being
  multiplied twice.
- Unknown-date and week-precision entries remain auditable and do not pretend
  to be exact Today totals.
- Credit balances are derived from receivables and immutable payment receipts.
- The LLM never supplies authoritative stock, margin, outstanding balance, GST,
  or totals without a backend tool.

## Data model

PostgreSQL is the operational database. A Neon PostgreSQL URL works directly.

| Table | Purpose |
| --- | --- |
| `users`, `sessions` | Authentication, onboarding, and hashed sessions |
| `skus` | Tenant catalogue, aliases, units, GST, and costs |
| `events` | Append-only inventory and sale ledger |
| `customers` | Customer identity and contact details |
| `receivables` | Credit created by sales |
| `payments` | Immutable customer payment receipts |
| `learning` | Per-shop aliases, priors, and corrections |
| `knowledge_entities`, `knowledge_edges` | Explainable tenant-specific aliases and product relationships |
| `user_config` | Shop, GSTIN, address, and operating settings |
| `documents` | Expiring generated PDFs used by WhatsApp |
| `voice_presentations` | Expiring in-app bill and summary previews |

The legacy JSON repository remains for migration and isolated tests; the live
multi-tenant application uses `SqlRepo`.

## Local setup

Requirements:

- Python 3.10 or newer;
- PostgreSQL or Neon;
- Node.js only when rebuilding the checked-in browser SDK bundle.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create `.env` in the repository root:

```dotenv
# Core application
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
CHHOTU_SECRET=replace-with-a-long-random-value
CHHOTU_PUBLIC_URL=http://127.0.0.1:8000

# Sarvam APIs and Samvaad v6
SARVAM_API_KEY=...
SAMVAAD_API_KEY=...
SAMVAAD_WEBHOOK_SECRET=replace-with-another-long-random-value
SAMVAAD_ORG_ID=019f9945-ebf7-77f9-b60b-dc1963284e44
SAMVAAD_WORKSPACE_ID=019f9945-ebfb-76ac-9855-2f2c5985abbb
SAMVAAD_APP_ID=Voice-Assis-9018c9fb-e7c8
SAMVAAD_AGENT_VERSION=6

# Optional WhatsApp delivery
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=+14155238886
CHHOTU_TEST_RECIPIENT=+91...

# Optional scheduled reminders
CRON_SECRET=replace-with-a-random-cron-secret
```

Generate application secrets with:

```bash
openssl rand -hex 32
```

For a fresh database, create the schema:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0, 'backend'); import db; db.init_schema()"
```

To import the bundled legacy demo ledger into a tenant instead:

```bash
.venv/bin/python backend/migrate.py \
  --phone 9876543210 \
  --shop "Gupta Hardware"
```

Start the application:

```bash
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

### Rebuild the browser voice bundle

The repository checks in a browser-safe bundle built from the pinned JavaScript
SDK:

```bash
npm install --omit=optional
npm run build:samvaad
```

The Python SDK in `requirements.txt` is used by the agent probe scripts. The
browser bundle uses the version pinned in `package.json`.

## Samvaad agent configuration

The current application pins committed agent version **6**.

The complete prompt, tool descriptions, field sources, cURL definitions, and
response mapping are generated from the backend registry:

```bash
.venv/bin/python backend/samvaad_config.py > docs/samvaad-setup.md
```

Use [docs/samvaad-setup.md](docs/samvaad-setup.md) when creating or updating
tools in the Samvaad console. Important rules:

- argument body fields must use **Let the agent decide**;
- `caller_number`, `shop_key`, and `agent_secret` are agent variables;
- the `X-Agent-Secret` value must match `SAMVAAD_WEBHOOK_SECRET`;
- every tool returns the full structured result in `{{facts}}`;
- commit the dashboard version and update `SAMVAAD_AGENT_VERSION` together.

## WhatsApp safety

For demo environments, set `CHHOTU_TEST_RECIPIENT`. Every outbound WhatsApp
message is redirected to that number and prefixed with the intended recipient,
preventing seeded customer numbers from receiving test messages.

Remove `CHHOTU_TEST_RECIPIENT` only when the deployment is intentionally ready
to contact actual customers. The Twilio Sandbox still requires each recipient
to opt in.

Bills and summaries use expiring public PDF URLs because Twilio must fetch the
media without a Chhotu.ai login. The random document token and expiry protect
the file.

## Tests

Run the complete backend suite:

```bash
.venv/bin/python -m unittest discover -s backend -p 'test_*.py'
```

The suite covers conversation state, multi-item extraction, quantities and
units, deterministic calculations, customer/credit/payment flows, reminders,
tool authentication, tenant resolution, product substring/SKU matching,
Samvaad configuration, document generation, storage, and safety refusals.

Check the frontend JavaScript syntax:

```bash
sed -n '/^<script>$/,/^<\/script>$/p' frontend/index.html \
  | sed '1d;$d' > /tmp/chhotu-inline.js
node --check /tmp/chhotu-inline.js
```

## Deployment

Vercel discovers the FastAPI application through the root `app.py` entry point.
`vercel.json` configures the function duration and daily reminder cron without
rewriting the browser's request path. Before production deployment:

1. provision PostgreSQL/Neon and run the schema migration;
2. configure all core and Samvaad environment variables;
3. set `SAMVAAD_AGENT_VERSION=6`;
4. configure Twilio only if outbound messaging is required;
5. keep `CHHOTU_TEST_RECIPIENT` set during demos;
6. set `CHHOTU_PUBLIC_URL` to the production HTTPS origin;
7. run the full test suite.

## Repository map

| Path | Responsibility |
| --- | --- |
| `backend/main.py` | FastAPI routes, authenticated tenant binding, transaction APIs, dashboards, invoices, and PDFs |
| `backend/agent.py` | Grounded Samvaad tool registry and deterministic tool handlers |
| `backend/samvaad_runtime.py` | Secure signed-URL proxy and committed agent-version pin |
| `backend/samvaad_config.py` | Generated prompt and console tool configuration |
| `backend/ledger.py` | Event replay, units, landed cost, stock, margin, and reconciliation |
| `backend/sqlrepo.py` | Tenant-scoped PostgreSQL repository |
| `backend/db.py` | Relational schema and short-lived connections |
| `backend/conversation.py` | Legacy/fallback conversational transaction controller |
| `backend/matcher.py` | Alias, substring, SKU, fuzzy, and learned product resolution |
| `backend/crm.py` | Customer accounts, FIFO credit allocation, and payment history |
| `backend/notify.py` | Bills, summaries, reminders, PDFs, and delivery verification |
| `backend/sarvam_client.py` | Sarvam STT, TTS, chat, and document intelligence |
| `frontend/index.html` | Complete responsive single-page operating interface |
| `docs/samvaad-setup.md` | Generated Samvaad v6 prompt and tool setup |

## Current limitations

- Twilio Sandbox recipients must opt in; production WhatsApp requires an
  approved sender and templates.
- Costing is last-purchase/replacement-cost based, not FIFO or weighted average.
- The current tenancy model is owner-centric; staff roles and granular
  permissions are not implemented.
- Warehouse bins, barcode scanning, supplier accounts, purchase orders,
  returns/credit notes, and offline synchronization are not yet implemented.
- Samvaad tool configuration is committed manually in its console and must stay
  synchronized with the generated setup guide.
