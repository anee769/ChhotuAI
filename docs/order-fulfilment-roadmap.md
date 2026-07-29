# Chhotu.ai Order, Fulfilment, Restocking and Invoice Roadmap

**Implementation status — 29 July 2026:** Phase 1 and Phase 2 are implemented
and tested on the `dev` branch. The existing shop operating system remains on
`main`. Later phases remain planned and are intentionally not silently
simulated by the assistant.

## Release status

| Area | Status | Release |
| --- | --- | --- |
| Existing inventory, CRM, billing, analytics and multilingual voice system | Live | `main` / production |
| Manual customer orders, reservations and fulfilment state machine | Implemented and tested | `dev` |
| Order-taking tools for the Sarvam agent | Implemented and contract-tested | `dev` |
| Dev Vercel preview | Deployed and protected by Vercel authentication | `dev` |
| Isolated Preview configuration and database | Setup pending | Before shared testing |
| Dispatch operations, provider booking and supplier automation | Planned | Phases 3–7 |

The `dev` implementation currently passes 206 backend tests. `main` remains
unchanged until the new order lifecycle has been exercised in an isolated
Preview environment and accepted.

## Product direction

Chhotu.ai should become the operating layer between a shop's conversations and
its physical movement of goods. A customer may place an order by speaking to
Chhotu, but the conversation itself is never the system of record. The backend
stores the order, its items, reservations, delivery status, payments and every
status transition. The voice agent can only read or change that state through
grounded tools.

The design separates four business facts:

1. **An order is a promise.** It reserves available stock but does not change
   physical on-hand stock.
2. **A dispatch is movement.** Reserved goods leave the fulfilment queue.
3. **A delivered order is a sale.** Only then is the existing sale and credit
   ledger written.
4. **A supplier delivery is incoming stock.** A purchase order or supplier
   promise never increases stock; a confirmed goods receipt does.

## Knowledge architecture: global intelligence, private shop learning

Yes: every Chhotu shop/workspace has its own knowledge graph. In the database,
that boundary is the authenticated tenant `user_id`; knowledge entities and
edges cannot reference entities from another tenant. A phrase learned by one
shop—such as `patla sariya → TMT 12mm`—must never silently affect another
shop.

Here, **Chhotu workspace** means a shop/warehouse tenant. It is different from
a Sarvam platform workspace, which is only an agent-deployment container.

The scalable model has two layers:

### 1. Global domain taxonomy

Chhotu ships a shared, versioned base of non-private industry knowledge:

- product families and common attributes;
- standard units, pack sizes and deterministic conversions;
- common multilingual category synonyms;
- safe product-type relationships and validation rules;
- provider capabilities, delivery classes and generic operational concepts.

This layer may suggest candidates, but it does not know a shop's catalogue,
customers, prices, balances, sales, aliases or preferences. A global term can
never prove that a tenant stocks a product.

### 2. Workspace-local knowledge graph

Each tenant graph can learn verified relationships such as:

- spoken term → catalogue SKU;
- product → preferred unit or packaging;
- customer → repeatedly purchased product;
- customer → delivery address or payment preference;
- supplier → supplied SKU, lead-time band or pack convention;
- local term → delivery vehicle/service class;
- correction → rejected and accepted candidate.

Only confirmed actions reinforce these relationships. The graph ranks,
resolves and explains; transactional tables remain authoritative for stock,
money, orders, deliveries, invoices and credit.

### Generalising learning safely

Useful patterns can eventually improve Chhotu for everybody through a
controlled promotion pipeline:

1. Collect a candidate only after repeated confirmations within a tenant.
2. Remove tenant IDs, names, phone numbers, prices and transaction facts.
3. Aggregate the pattern across multiple unrelated tenants.
4. Reject conflicting, overly local or low-support patterns.
5. Human-review the candidate before publishing a new taxonomy version.
6. Roll out the version with provenance, confidence and rollback support.

Raw tenant edges are never copied into another workspace. Customer, supplier,
commercial and behavioral relationships always remain private. Globalisation
is limited to anonymous domain concepts that are broadly reusable.

### Knowledge roadmap

1. **Current foundation:** tenant-scoped product aliases, families and units
   stored in PostgreSQL with confidence and evidence.
2. **Order learning:** verified customer-product and order-language edges from
   confirmed orders.
3. **Supplier learning:** verified supplier-SKU, packaging and lead-time edges
   from purchase orders and goods receipts.
4. **Delivery learning:** address/service/vehicle preferences from completed
   deliveries, with expiry for stale patterns.
5. **Contradiction handling:** decay, supersession and explicit correction of
   outdated edges.
6. **Explainability:** show why a SKU, supplier or delivery option was
   suggested.
7. **Taxonomy promotion:** anonymised cross-tenant candidate aggregation and
   reviewed global releases.

PostgreSQL remains the graph store for now. Its compound tenant keys,
transactions and indexed neighbor queries are sufficient; a dedicated graph
database is warranted only if measured multi-hop workloads exceed it.

## Target workflow

```text
Customer/owner/supplier
        ↓
Sarvam voice agent or manual workspace
        ↓
Grounded Chhotu tools
        ↓
Orders ── Reservations ── Deliveries ── Sale/credit ledger
        ↓
Restock suggestions ── Purchase orders ── Goods receipts
        ↓
Supplier invoices ── PO/receipt/invoice reconciliation
```

## Order state machine

Primary statuses:

```text
draft
  → awaiting_confirmation
  → confirmed
  → stock_allocated
  → ready_for_dispatch
  → out_for_delivery
  → delivered
```

Controlled side paths:

```text
draft / awaiting_confirmation / confirmed → cancelled
awaiting_confirmation → partially_available
partially_available → confirmed or cancelled
out_for_delivery → delivery_failed
delivery_failed → ready_for_dispatch or cancelled
```

Every transition is validated by the backend and appended to an immutable
status history. The agent is not allowed to invent or skip a transition.

## Inventory reservation rule

The existing event ledger remains the source of truth for physical stock.
Orders add a separate reservation projection:

```text
available stock = physical on-hand stock - active order reservations
```

Active reservations belong to confirmed, allocated, ready, out-for-delivery
and delivery-failed orders. Cancelling or delivering an order releases its
reservation. Delivering creates the existing sale events exactly once.

## Delivery-provider boundary

Chhotu owns:

- Delivery address and contact
- Requested and scheduled window
- Internal status and audit trail
- Customer-visible status
- Provider booking reference and tracking URL
- Proof/failure notes

A provider adapter owns:

- Quote
- Vehicle/service selection
- Booking
- Driver assignment
- Live tracking
- Cancellation
- Webhook authentication and provider-status mapping

The delivery record therefore supports `manual`, `own_fleet`, `porter` and
future providers without putting provider-specific fields in the order.

### Porter recommendation

Porter's public site says its API integration provides order creation,
webhooks and tracking, but currently lists API vehicle availability as
two-wheelers. That is useful for small hardware parcels, not cement bags,
tiles or TMT loads. Chhotu should therefore start with manual/own-fleet
delivery tracking and a provider-neutral adapter. Porter should be enabled
only for eligible orders after enterprise credentials, city coverage,
payload limits and commercial terms are confirmed.

Until then, Chhotu can prepare the pickup/drop/contact/goods details and let
the owner mark the external booking reference manually.

### How a Porter integration would work

Chhotu should not call Porter directly from the voice model. The backend owns
the provider adapter and performs the following controlled flow:

1. A confirmed order reaches `ready_for_dispatch`.
2. Deterministic eligibility checks inspect pickup/drop city, package
   dimensions, weight, item category and required vehicle.
3. If Porter is eligible, the backend requests a quote and stores the returned
   service/quote reference. If it is not eligible, the order remains in the
   manual/own-fleet queue.
4. The owner reviews the fee and explicitly approves booking.
5. The backend creates the provider booking with an idempotency key and stores
   the provider order ID and tracking URL.
6. Authenticated webhooks are recorded once, mapped to Chhotu's internal
   statuses, and shown to both the owner and customer.
7. Cancellation, failure and reattempts go through the same adapter and audit
   trail. A provider response never writes stock or money directly.

The adapter contract should expose `quote`, `book`, `cancel`, `status` and
`verify_webhook`. This keeps Chhotu independent of one logistics company and
allows different providers by city, weight class or vehicle type.

Official references:

- [Porter API integrations](https://porter.in/api-integrations)
- [Porter business services](https://porter.in/)

## Phased delivery

### Phase 1 — Manual order and fulfilment foundation

**Status: implemented on `dev`.**

Goal: make orders and delivery status reliable without depending on an LLM or
external logistics provider.

Deliverables:

- Tenant-scoped orders, order items and status history
- Tenant-scoped deliveries linked one-to-one with an order
- Deterministic totals and GST snapshots
- Physical versus reserved versus available stock
- Validated status transitions
- Idempotent conversion of a delivered order into sale/credit events
- Manual order workspace with filters and status actions
- Customer order history includes open and completed orders
- API and domain tests for tenancy, transitions, reservations, cancellation,
  partial availability and exactly-once fulfilment

Exit criteria:

- Two concurrent orders cannot reserve the same stock
- Cancelling releases the reservation
- Delivering writes one sale even if the request is retried
- Invalid transitions return a clear error and make no changes

### Phase 2 — Customer orders through Chhotu

**Status: implemented and contract-tested on `dev`; live-agent acceptance in
the isolated Preview environment remains before release.**

Goal: let the existing Sarvam assistant safely create and manage the same
orders through tools.

Deliverables:

- `create_order_draft`
- `show_order`
- `confirm_order`
- `get_order_status`
- `list_orders`
- `update_order_status`
- `cancel_order`
- Product matching through the existing catalogue, aliases and knowledge graph
- Customer matching by English/Latin name, ID or phone
- One order for all items in a conversation
- Explicit confirmation before reservation
- Tool replies containing structured facts and a short speakable response
- Samvaad setup instructions and smoke/contract tests

Exit criteria:

- The assistant never converts an unknown product into a known SKU
- It reports every unavailable or ambiguous line
- It cannot confirm, cancel or advance a missing order
- Repeated tool calls with the same request ID do not create duplicates

### Phase 3 — Fulfilment operations

- Delivery queue and mobile dispatch view
- Driver/vehicle assignment
- Delivery windows and customer notifications
- Failed-delivery and reattempt workflow
- Proof of delivery
- On-time delivery, ageing and failure analytics
- Workspace-graph reinforcement only after a delivery is completed

### Phase 4 — Inbound customer calls

- Dedicated number or agent assignment identifies the shop tenant
- Caller phone identifies the customer within that shop
- Multilingual order capture and status lookup
- Human handoff
- Structured call outcome and consent-aware QA metadata

The caller number must never identify the tenant by itself: the same person
can buy from multiple Chhotu shops.

### Phase 5 — Restocking and supplier calls

- Suppliers and product-supplier mappings
- Deterministic reorder suggestions based on available stock, reservations,
  velocity, safety stock and lead time
- Purchase orders and approvals
- Outbound supplier call tasks
- Supplier confirmation, rate, freight and promised date
- Partial goods receipts
- Tenant-local supplier terminology, pack-size and lead-time learning

### Phase 6 — Invoice retrieval and reconciliation

- Durable supplier invoice metadata and file storage
- Search by supplier, invoice number, purchase order and date
- Existing Sarvam document digitisation pipeline
- Three-way comparison: purchase order vs goods receipt vs invoice
- Owner confirmation before committing discrepancies

### Phase 7 — Provider automation and analytics

- Provider adapter interface and webhook inbox
- Porter or another carrier after commercial onboarding
- Idempotent booking and webhook processing
- Order conversion, fill rate, average fulfilment time, on-time delivery,
  cancellations, supplier lead time and invoice variance

## Safety and operating rules

- Read before write.
- Ask for explicit confirmation before money, stock or delivery changes.
- Every write has a request/idempotency key.
- Backend calculations decide totals, availability and valid transitions.
- Agent language may be natural; stored customer names remain Latin script.
- Tool responses distinguish `not_found`, `ambiguous`, `insufficient_stock`,
  `invalid_transition` and `already_completed`.
- External webhooks are authenticated, stored and processed idempotently.
- No provider failure is allowed to corrupt the internal order state.

## Rollout strategy

1. Keep production on `main`; develop and validate the roadmap on `dev`.
2. Configure Vercel Preview with copied non-data settings and a separate Neon
   database branch. Never point routine dev testing at production data.
3. Point a separate Sarvam agent version at the Preview tool URLs and retain
   Twilio sandbox restrictions.
4. Run the complete order lifecycle with seeded demo orders: draft,
   confirmation, reservation, cancellation, dispatch and exactly-once
   delivery-to-sale conversion.
5. Run live multilingual agent acceptance for every Phase 2 order tool,
   including ambiguous, unavailable and multi-item orders.
6. Merge `dev` into `main` only after automated tests, mobile UX, tenant
   isolation, Preview smoke tests and rollback checks pass.
7. Enable inbound customer calls only after tool-contract, interruption,
   consent and tenant-resolution tests pass.
8. Add one delivery provider through the adapter; retain manual/own-fleet
   fallback.
9. Add supplier and invoice automation only after order and delivery telemetry
   is stable.

## Dev environment checklist

- Vercel Preview deployment is generated from `dev` and requires team login.
- Copy non-database Production variables into the Preview scope.
- Create a Neon branch/clone and set Preview `DATABASE_URL` to that database.
- Set `CHHOTU_URL` and `CHHOTU_PUBLIC_URL` to the Preview origin.
- Use a separate `CHHOTU_SECRET`/agent secret for Preview.
- Configure a separate Samvaad agent version whose tools target Preview.
- Keep Twilio on sandbox/test recipients and prevent scheduled reminders from
  contacting production customers.
- Seed synthetic shops, customers, inventory, learning edges and orders.
- Verify health, authentication, tenant isolation, tool contracts, PDFs,
  presentation expiry and mobile layouts.
- Document promotion/rollback steps before merging to `main`.

## Merge gates for production

- All automated tests pass from a clean checkout.
- Preview health and authenticated signup/onboarding pass.
- No Preview request reads or writes the production database.
- All order transitions and reservations remain deterministic and idempotent.
- Agent tools report missing, ambiguous and unavailable items without
  hallucinating replacements.
- Every customer-visible message requires the expected confirmation.
- Tenant knowledge remains isolated under cross-tenant tests.
- Mobile order, customer and dispatch screens work without clipped controls.
- Existing inventory, CRM, billing, invoice, dashboard and Voice Entry flows
  pass regression smoke tests.
