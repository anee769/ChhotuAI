"""Order, reservation and fulfilment invariants."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("CHHOTU_TODAY", "2026-07-29")
os.environ["SAMVAAD_WEBHOOK_SECRET"] = "test-agent-secret"
os.environ.setdefault("CHHOTU_SECRET", "test-secret")

import main
import orderbook
from repo import JsonRepo


USER = {
    "user_id": "u_order_test", "phone": "+919999999999",
    "name": "Owner", "shop_name": "Test Hardware",
}


class OrderbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        sku = {
            "sku_id": "CEMENT_PPC", "canonical": "PPC Cement 50kg",
            "family": "cement", "attributes": {"selling_rate": 420},
            "default_unit": "bori", "units": {"kg": 1, "bori": 50},
            "gst_rate": 28, "opening_cost_per_kg": 7.5, "aliases": [],
        }
        for name, value in {
            "catalogue.json": [sku], "events.json": [], "customers.json": [],
            "receivables.json": [], "payments.json": [], "orders.json": [],
            "deliveries.json": [], "config.json": {"gst_default": 18},
        }.items():
            (self.data / name).write_text(
                json.dumps(value), encoding="utf-8")
        self.repo = JsonRepo(self.data)
        self.customer = self.repo.upsert_customer(
            "9876543210", "Pankaj Sharma")
        self.repo.append_event({
            "type": "opening_balance", "sku_id": "CEMENT_PPC",
            "qty": 20, "unit": "bori", "rate": 7.5,
            "occurred_on": "2026-07-01", "precision": "exact",
            "confidence": 1, "source": "test", "evidence": {},
        })
        main._CURRENT.set(self.repo)
        main._CURRENT_USER.set(USER)

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, qty=5, request_id="req-1", payment="cash"):
        return {
            "customer_id": self.customer["customer_id"],
            "payment": payment,
            "payment_deadline": (
                "2026-08-15" if payment == "credit" else None),
            "fulfilment_method": "delivery",
            "delivery_address": "Site 4, Pune",
            "request_id": request_id,
            "items": [{"sku_id": "CEMENT_PPC", "qty": qty, "unit": "bori"}],
        }

    def test_draft_does_not_move_or_reserve_stock(self):
        order = orderbook.create(self.repo, self.payload())

        self.assertEqual(order["status"], "draft")
        self.assertEqual(order["items"][0]["reserved_base"], 0)
        self.assertEqual(order["items"][0]["rate"], 420)
        self.assertEqual(order["items"][0]["rate_unit"], "bori")
        self.assertEqual(order["subtotal"], 2100)
        availability = orderbook.stock_availability(
            self.repo, "CEMENT_PPC")
        self.assertEqual(availability["physical_base"], 1000)
        self.assertEqual(availability["available_base"], 1000)
        self.assertEqual(len(self.repo.all_events()), 1)

    def test_confirm_reserves_and_cancel_releases(self):
        order = orderbook.create(self.repo, self.payload())
        confirmed = orderbook.confirm(self.repo, order["order_id"])

        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["items"][0]["reserved_base"], 250)
        self.assertEqual(
            orderbook.stock_availability(self.repo, "CEMENT_PPC")
            ["available_base"], 750)

        cancelled = orderbook.transition(
            self.repo, order["order_id"], "cancelled")
        self.assertEqual(cancelled["items"][0]["reserved_base"], 0)
        self.assertEqual(
            orderbook.stock_availability(self.repo, "CEMENT_PPC")
            ["available_base"], 1000)

    def test_existing_reservation_prevents_overbooking(self):
        first = orderbook.create(self.repo, self.payload(qty=15))
        orderbook.confirm(self.repo, first["order_id"])
        second = orderbook.create(
            self.repo, self.payload(qty=10, request_id="req-2"))

        checked = orderbook.confirm(self.repo, second["order_id"])

        self.assertEqual(checked["status"], "partially_available")
        self.assertEqual(checked["items"][0]["reserved_base"], 0)
        self.assertEqual(
            checked["items"][0]["availability_status"], "backordered")

    def test_simultaneous_confirmations_cannot_overbook(self):
        first = orderbook.create(self.repo, self.payload(qty=15))
        second = orderbook.create(
            self.repo, self.payload(qty=15, request_id="req-2"))
        barrier = threading.Barrier(2)
        outcomes = []

        def run(order_id):
            barrier.wait()
            outcomes.append(orderbook.confirm(self.repo, order_id)["status"])

        threads = [
            threading.Thread(target=run, args=(first["order_id"],)),
            threading.Thread(target=run, args=(second["order_id"],)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(outcomes, ["confirmed", "partially_available"])
        reserved = sum(
            float(item.get("reserved_base") or 0)
            for order in self.repo.orders()
            for item in order.get("items") or [])
        self.assertEqual(reserved, 750)

    def test_backorder_can_be_rechecked_after_stock_arrives(self):
        first = orderbook.create(self.repo, self.payload(qty=20))
        orderbook.confirm(self.repo, first["order_id"])
        second = orderbook.create(
            self.repo, self.payload(qty=2, request_id="req-2"))
        backordered = orderbook.confirm(
            self.repo, second["order_id"], allow_backorder=True)
        self.assertEqual(backordered["status"], "confirmed")
        self.assertEqual(
            backordered["items"][0]["availability_status"], "backordered")

        orderbook.transition(self.repo, first["order_id"], "cancelled")
        refreshed = orderbook.confirm(self.repo, second["order_id"])
        self.assertEqual(
            refreshed["items"][0]["availability_status"], "available")
        self.assertEqual(refreshed["items"][0]["reserved_base"], 100)

    def test_cost_fallback_is_priced_per_base_unit(self):
        sku = self.repo.sku("CEMENT_PPC")
        sku["attributes"].pop("selling_rate")
        self.repo.upsert_sku(sku)

        order = orderbook.create(self.repo, self.payload(qty=1))

        self.assertEqual(order["items"][0]["rate_unit"], "kg")
        self.assertEqual(order["items"][0]["subtotal"], 412.5)

    def test_request_id_is_idempotent(self):
        first = orderbook.create(self.repo, self.payload())
        second = orderbook.create(self.repo, self.payload())

        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(len(self.repo.orders()), 1)

    def test_invalid_transition_changes_nothing(self):
        order = orderbook.create(self.repo, self.payload())
        with self.assertRaises(orderbook.OrderError) as caught:
            orderbook.transition(
                self.repo, order["order_id"], "out_for_delivery")

        self.assertEqual(caught.exception.code, "invalid_transition")
        self.assertEqual(
            self.repo.order(order["order_id"])["status"], "draft")

    def test_delivery_writes_sale_and_credit_exactly_once(self):
        order = orderbook.create(
            self.repo, self.payload(payment="credit"))
        oid = order["order_id"]
        orderbook.confirm(self.repo, oid)
        for status in ("stock_allocated", "ready_for_dispatch",
                       "out_for_delivery", "delivered"):
            delivered = orderbook.transition(self.repo, oid, status)

        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(len(delivered["sale_event_ids"]), 1)
        sales = [event for event in self.repo.all_events()
                 if event["type"] == "sale"]
        self.assertEqual(len(sales), 1)
        self.assertEqual(len(self.repo.receivables()), 1)
        self.assertEqual(delivered["items"][0]["reserved_base"], 0)

        with self.assertRaises(orderbook.OrderError):
            orderbook.transition(self.repo, oid, "delivered")
        self.assertEqual(
            len([event for event in self.repo.all_events()
                 if event["type"] == "sale"]), 1)
        self.assertEqual(len(self.repo.receivables()), 1)

    def test_delivery_metadata_stays_separate_from_order_status(self):
        order = orderbook.create(self.repo, self.payload())
        delivery = orderbook.update_delivery(self.repo, order["order_id"], {
            "driver_name": "Amit", "vehicle": "Tata Ace MH01",
            "provider": "own_fleet",
        })

        self.assertEqual(delivery["driver_name"], "Amit")
        self.assertEqual(delivery["provider"], "own_fleet")
        self.assertEqual(
            self.repo.order(order["order_id"])["status"], "draft")

    def test_authenticated_http_workflow_uses_the_same_order_rules(self):
        client = TestClient(main.app)

        def bind(_user):
            main._CURRENT_USER.set(USER)
            main._CURRENT.set(self.repo)

        with patch.object(main.auth, "user_for_token", return_value=USER), \
                patch.object(main, "bind_user", side_effect=bind):
            created = client.post(
                "/api/orders", headers={"Authorization": "Bearer test"},
                json=self.payload()).json()
            order_id = created["order_id"]
            self.assertEqual(created["status"], "draft")

            invalid = client.post(
                f"/api/orders/{order_id}/transition",
                headers={"Authorization": "Bearer test"},
                json={"status": "out_for_delivery"})
            self.assertEqual(invalid.status_code, 409)

            confirmed = client.post(
                f"/api/orders/{order_id}/confirm",
                headers={"Authorization": "Bearer test"},
                json={"allow_backorder": False}).json()
            self.assertEqual(confirmed["status"], "confirmed")

            delivery = client.patch(
                f"/api/orders/{order_id}/delivery",
                headers={"Authorization": "Bearer test"},
                json={"driver_name": "Amit", "vehicle": "Tata Ace"}).json()
            self.assertEqual(delivery["driver_name"], "Amit")

            listing = client.get(
                "/api/orders?status=confirmed",
                headers={"Authorization": "Bearer test"}).json()
            self.assertEqual(
                [row["order_id"] for row in listing["orders"]], [order_id])


if __name__ == "__main__":
    unittest.main()
