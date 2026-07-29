from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crm


class FakeCRMRepo:
    def __init__(self):
        self._catalogue = [{
            "sku_id": "CEM_PPC", "canonical": "PPC Cement",
            "default_unit": "bori", "units": {"bori": 1},
        }]
        self._customers = [
            {"customer_id": "c1", "name": "Repeat Buyer",
             "phone": "+919000000001", "created_at": "2026-07-20T10:00:00"},
            {"customer_id": "c2", "name": "New Buyer",
             "phone": "+919000000002", "created_at": "2026-07-25T10:00:00"},
        ]
        self._events = [
            {"event_id": "e1", "type": "sale", "sku_id": "CEM_PPC",
             "customer_id": "c1", "qty": 2, "unit": "bori", "rate": 100,
             "payment": "cash", "occurred_on": "2026-07-22",
             "evidence": {"request_id": "order-1"}},
            {"event_id": "e2", "type": "sale", "sku_id": "CEM_PPC",
             "customer_id": "c1", "qty": 3, "unit": "bori", "rate": 100,
             "payment": "credit", "occurred_on": "2026-07-26",
             "evidence": {"request_id": "order-2"}},
            {"event_id": "e3", "type": "sale", "sku_id": "CEM_PPC",
             "customer_id": "c2", "qty": 1, "unit": "bori", "rate": 100,
             "payment": "cash", "occurred_on": "2026-07-27",
             "evidence": {"request_id": "order-3"}},
        ]

    def load_catalogue(self):
        return self._catalogue

    def customers(self):
        return self._customers

    def all_events(self):
        return self._events

    def receivables(self):
        return []

    def payments(self):
        return []


class CRMAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeCRMRepo()

    def test_orders_are_grouped_per_customer_and_request(self):
        accounts = {row["customer_id"]: row
                    for row in crm.accounts(self.repo)}

        self.assertEqual(accounts["c1"]["order_count"], 2)
        self.assertEqual(accounts["c1"]["total_sales"], 500)
        self.assertTrue(accounts["c1"]["repeat_buyer"])
        self.assertEqual(accounts["c2"]["order_count"], 1)

    def test_acquisition_retention_and_best_products(self):
        out = crm.analytics(self.repo, date(2026, 7, 29), days=30)

        self.assertEqual(out["new_customers"], 2)
        self.assertEqual(out["repeat_buyers"], 1)
        self.assertEqual(out["repeat_rate_pct"], 50)
        self.assertEqual(out["average_order_value"], 200)
        self.assertEqual(out["best_products"][0]["revenue"], 600)
        self.assertEqual(out["top_customers"][0]["name"], "Repeat Buyer")
        self.assertEqual(len(out["acquisition_trend"]), 8)
        self.assertEqual(out["acquisition_trend"][-1]["new_customers"], 1)
        self.assertEqual(out["outstanding_credit"], 0)
        self.assertEqual(out["open_credit_accounts"], 0)


if __name__ == "__main__":
    unittest.main()
