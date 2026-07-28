# Samvaad tool audit

Audit date: 2026-07-28  
Agent: `Voice-Assis-9018c9fb-e7c8`  
Committed agent version tested: `4`

## Conclusion

The backend and the committed Samvaad agent have separate contracts.

- Direct requests to the backend pass for all read tools and all safe
  validation paths of write tools.
- Committed version 4 connects with the correct caller and `agent_secret`.
- Version 4 succeeds for tools with no required arguments.
- Version 4 fails for product and customer tools whose result depends on an
  argument, even when its tool card displays the correct proposed JSON.

This pattern means the remaining version-4 failure is in its HTTP Body field
mapping. The tool card is the model's proposed function input; it is not proof
of the HTTP body sent to ChhotuAI. Each Body field must be marked
**agent-filled** in the tool editor. A literal `{{item}}` default is removed by
the backend as an unresolved template and cannot be searched.

## Version evidence

The runtime URL API reports versions 1, 2, 3, and 4. Versions 5–8 return 404.
Therefore all SDK production checks must set:

```bash
SAMVAAD_AGENT_VERSION=4
```

Leaving the version unset tests the unversioned draft and is not equivalent to
version 4.

## Expected and observed results

| Layer | Test | Result |
| --- | --- | --- |
| Backend auth | Real secret and registered caller | Authorized as Gupta Hardware |
| Backend auth | Literal `{{agent_secret}}` | Structured `bad_agent_secret`; no tool runs |
| Backend lookup | `cem_ultratech_ppc` | Resolves `CEM_ULTRATECH_PPC` |
| Backend lookup | `UltraTech PPC` | Unique substring resolves PPC cement |
| Backend lookup | `अल्ट्राटेक पी पी सी सीमेंट` | Resolves PPC cement |
| Backend customer | `Pankaj` | Resolves Pankaj Sharma |
| Version 4 | `list_inventory` | Works |
| Version 4 | daily/low-stock summary | Works with defaults |
| Version 4 | PPC stock | Says no record |
| Version 4 | PPC item details | Says no record |
| Version 4 | Pankaj customer account | Says no customer |

The exact version-4 speech transcript `टेक पीपीसी सीमेंट` was also sent
directly to the backend. It resolved `CEM_ULTRATECH_PPC`, proving that the
speech wording itself is no longer the blocker.

## Backend coverage by tool

| Tool | Backend contract audited |
| --- | --- |
| `shop_profile` | Authorized tenant profile |
| `list_inventory` | All rows include SKU IDs |
| `check_stock` | Item substring, Hindi PPC, exact SKU ID |
| `item_details` | Item substring, Hindi PPC, exact SKU ID |
| `search_items` | Name, brand, fuzzy phrase, exact SKU ID |
| `low_stock` | Default and spoken limit |
| `business_summary` | Day/week/month/dates |
| `top_items` | Spoken days/limit and ordering |
| `list_customers` | Customer rows and balances |
| `customer_account` | Exact ID, phone, exact/substring name |
| `dues` | Spoken `days_before` |
| `recent_activity` | Spoken limit |
| `stock_value` | Counted and uncounted stock |
| `price_quote` | Item substring and exact SKU ID |
| `record_sale` | Substring/SKU resolution, missing-data refusal, idempotency |
| `record_purchase` | Substring/SKU resolution, missing-data refusal, idempotency |
| `stock_take` | Substring/SKU resolution and missing-data refusal |
| `record_payment` | Customer ID/phone/name and missing amount refusal |
| `add_item` | Required cost and duplicate detection |
| `update_shop_profile` | Empty change refusal and field update |
| `update_item` | Substring, case-insensitive SKU ID, ID priority |
| `remove_item` | SKU resolution and referenced-item safety refusal |
| `send_bill` | Customer/item resolution; delivery mocked in tests |
| `send_summary` | Delivery mocked in tests |
| `send_reminders` | Delivery mocked in tests |

## Version 4 tool-editor checklist

For every argument-taking tool:

1. Use its named URL from `docs/samvaad-setup.md`.
2. Keep identity in the URL: `caller`, `shop_key`, and `agent_secret`.
3. Keep the request body flat. Do not create an `args` object.
4. In the Body tab, mark every listed field **agent-filled**.
5. Do not leave a literal product, customer, quantity, or `{{field}}` as the
   default value.
6. Set **What the agent gets back** to `{{facts}}`.
7. Commit the agent and run the SDK with that exact committed version.

For one diagnostic run, add `&debug=1` to a named tool URL. Its response gets
an `_trace.received_args` object showing what crossed the HTTP boundary, with
credentials and request IDs excluded. Remove `debug=1` after verification.

