# Chhotu.ai Order, Fulfilment, Restocking and Invoice Roadmap

**Implementation status — 29 July 2026:** Phase 1 and Phase 2 are implemented
in the current codebase. Later phases remain planned and are intentionally not
silently simulated by the assistant.

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

1. Ship Phase 1 behind manual controls.
2. Run the complete order lifecycle with seeded and real demo orders.
3. Expose those same domain operations as Phase 2 tools.
4. Enable customer calls only after tool-contract and interruption tests pass.
5. Add one delivery provider through the adapter; retain manual fallback.
