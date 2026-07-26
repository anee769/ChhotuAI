"""Focused regression tests for the stateful voice transaction controller."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conversation


class FakeRepo:
    def __init__(self):
        path = Path(__file__).resolve().parent.parent / "data" / "catalogue.json"
        self.catalogue = json.loads(path.read_text(encoding="utf-8"))
        self.by_id = {row["sku_id"]: row for row in self.catalogue}
        self.saved_customer = None

    def load_catalogue(self):
        return self.catalogue

    def load_learning(self):
        return {"aliases_learned": [], "attribute_priors": [],
                "unit_priors": [], "corrections": []}

    def sku(self, sku_id):
        return self.by_id.get(sku_id)

    @staticmethod
    def normalize_phone(phone):
        digits = "".join(ch for ch in str(phone) if ch.isdigit())[-10:]
        return "+91" + digits

    def customer_by_phone(self, phone):
        if self.saved_customer and self.saved_customer["phone"] == self.normalize_phone(phone):
            return self.saved_customer
        return None

    def upsert_customer(self, phone, name):
        self.saved_customer = {
            "customer_id": "cust_test", "phone": self.normalize_phone(phone),
            "name": name,
        }
        return self.saved_customer


class ConversationStateTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.extracted = {
            "intent": "sale", "metric": None,
            "items": [
                {"sku_id": "CEM_ULTRATECH_PPC", "family": "cement",
                 "name": "50 bori PPC cement", "in_catalogue": True,
                 "qty": 50, "unit": "bori", "rate": None, "payment": None},
                {"sku_id": "TMT_12_FE500D_TATA", "family": "tmt",
                 "name": "12 kg barah mm sariya", "in_catalogue": True,
                 "qty": 12, "unit": "kg", "rate": None, "payment": None},
            ],
        }

    def test_followups_reuse_one_extraction_and_keep_all_items(self):
        committed = {}

        def fake_commit(state, flow, items, skipped, repo):
            committed.update(flow=flow, items=items, customer=state.get("customer"))
            return conversation._say(state, "saved", listen=False, done=True)

        with patch.object(conversation.sarvam_client, "has_key", return_value=True), \
             patch.object(conversation, "_extract", return_value=self.extracted) as extract, \
             patch.object(conversation, "_commit", side_effect=fake_commit):
            out = conversation.converse(
                None, "50 bori PPC cement aur 12 kg barah mm sariya becha",
                "live_sale", self.repo,
            )
            self.assertEqual(out["state"]["awaiting_order"]["slot"], "rate")
            for answer in ("450", "65", "cash", "9876543210", "Ravi Builder"):
                out = conversation.converse(out["state"], answer, "live_sale", self.repo)
            self.assertTrue(out["confirmation_required"])
            self.assertEqual(len(out["confirmation"]["items"]), 2)
            self.assertEqual(committed, {}, "nothing may be written before confirmation")
            self.assertEqual(out["confirmation"]["customer"]["name"], "Ravi Builder")
            out = conversation.converse(out["state"], "confirm", "live_sale", self.repo)

        self.assertTrue(out["done"])
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(len(committed["items"]), 2)
        self.assertEqual([row["rate"] for row in committed["items"]], [450.0, 65.0])
        self.assertTrue(any(row["role"] == "assistant"
                            for row in out["state"]["history"]))
        self.assertEqual(committed["customer"]["name"], "Ravi Builder")

    def test_extractor_receives_complete_role_history_once(self):
        state = {
            "said": ["50 bori cement", "PPC"],
            "history": [
                {"role": "user", "content": "50 bori cement"},
                {"role": "assistant", "content": "Kaunsa cement — OPC 53 ya PPC?"},
                {"role": "user", "content": "PPC"},
            ],
        }
        model_output = {
            "intent": "sale", "metric": None,
            "items": [{"sku_id": "CEM_ULTRATECH_PPC", "family": "cement",
                       "name": "PPC cement", "in_catalogue": True,
                       "qty": 50, "unit": "bori", "rate": 450,
                       "payment": "cash"}],
        }
        with patch.object(conversation.sarvam_client, "chat_json",
                          return_value=model_output) as chat:
            result = conversation._extract(state, self.repo)
        messages = chat.call_args.args[0]
        self.assertEqual([m["role"] for m in messages[-3:]],
                         ["user", "assistant", "user"])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(chat.call_args.kwargs["timeout"], 18)
        self.assertEqual(result["items"][0]["sku_id"], "CEM_ULTRATECH_PPC")

    def test_credit_asks_deadline_but_cash_does_not(self):
        credit = [dict(self.extracted["items"][0], rate=450, payment="credit")]
        cash = [dict(self.extracted["items"][0], rate=450, payment="cash")]
        state_credit = {"flow": "live_sale", "said": [], "history": [],
                        "locked_intent": "sale", "draft_items": credit}
        state_cash = {"flow": "live_sale", "said": [], "history": [],
                      "locked_intent": "sale", "draft_items": cash}

        with patch.object(conversation.sarvam_client, "has_key", return_value=True), \
             patch.object(conversation, "_commit",
                          return_value={"done": True, "listen": False}):
            out_credit = conversation._order_flow(
                state_credit, "sale", credit, self.repo)
            out_credit = conversation._apply_customer_slot(
                out_credit["state"], "customer_phone", "9876543210", self.repo)
            out_credit = conversation._apply_customer_slot(
                out_credit["state"], "customer_name", "Ravi Builder", self.repo)
            self.assertEqual(out_credit["state"]["awaiting"], "deadline")
            out_credit = conversation._apply_customer_slot(
                out_credit["state"], "deadline", "kal", self.repo)
            self.assertTrue(out_credit["confirmation_required"])

            out_cash = conversation._order_flow(state_cash, "sale", cash, self.repo)
            out_cash = conversation._apply_customer_slot(
                out_cash["state"], "customer_phone", "9123456780", self.repo)
            out_cash = conversation._apply_customer_slot(
                out_cash["state"], "customer_name", "Cash Customer", self.repo)
            self.assertTrue(out_cash["confirmation_required"])
            out_cash = conversation.converse(
                out_cash["state"], "confirm", "live_sale", self.repo)
            self.assertTrue(out_cash["done"])

    def test_native_hindi_followup_values_are_understood(self):
        self.assertEqual(conversation._answer_number("पचास बोरी"), 50)
        self.assertEqual(conversation._detect_payment("नकद"), "cash")
        self.assertEqual(conversation._detect_payment("उधार"), "credit")
        self.assertEqual(conversation.parse_deadline("कल"), "2026-07-27")


if __name__ == "__main__":
    unittest.main()
