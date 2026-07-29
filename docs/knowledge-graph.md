# Chhotu knowledge graph

The knowledge graph is a tenant-isolated learning index for shop vocabulary
and preferences. It is not a second ledger.

## Source of truth

- Stock and margin come from the immutable event ledger.
- Customer balances come from receivables and payments.
- Product and customer identity come from their transactional tables.
- The graph may rank a match or suggest a clarification, but it cannot record
  a sale, change stock, calculate a bill, or prove that money was received.

## Initial graph

Every entity and relationship belongs to one `user_id`.

Entities:

- `term`: a phrase used by this shop, such as `laal wali cement`
- `product`: an inventory SKU
- `product_family`: cement, TMT, tile, pipe, and other catalogue families
- `unit`: bori, tonne, box, piece, and shop-specific units

Relationships:

- `term -> alias_for -> product`
- `product -> belongs_to -> product_family`
- `product -> uses_unit -> unit`

Each edge stores:

- confidence, capped below absolute certainty
- evidence count
- the latest 20 evidence records
- first-seen and last-confirmed timestamps
- active/inactive status

## Learning flow

1. The matcher resolves a spoken phrase using deterministic catalogue and
   existing learning rules.
2. The user confirms the selected product.
3. Existing alias/prior memory is updated.
4. The same confirmation is mirrored into graph relationships.
5. Repeated confirmations strengthen the same edge instead of creating
   duplicates.

Graph persistence failures are deliberately non-blocking during the rollout.
They must never prevent an already-confirmed ledger operation.

## Rollout

1. Foundation: persist and reinforce product vocabulary relationships.
2. Read path: use strong graph aliases as matcher candidates, with the same
   confirmation thresholds used today.
3. Preferences: add customer-product and customer-payment-pattern edges from
   verified orders, never from agent guesses.
4. Operations: add expiry and contradiction handling so old preferences lose
   influence.
5. Explainability: include the evidence behind a suggested match in internal
   diagnostics.

Dedicated graph infrastructure is unnecessary at this stage. PostgreSQL
tables provide tenant isolation, transactions, backups and adequate indexed
neighbor lookups without another operational dependency.
