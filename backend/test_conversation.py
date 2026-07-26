"""Focused regression tests for the stateful voice transaction controller."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conversation
import ledger
import main
import nlp


class FakeRepo:
    def __init__(self):
        path = Path(__file__).resolve().parent.parent / "data" / "catalogue.json"
        self.catalogue = json.loads(path.read_text(encoding="utf-8"))
        self.by_id = {row["sku_id"]: row for row in self.catalogue}
        self.saved_customer = None
        self.events = []

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

    def all_events(self):
        return list(self.events)

    def append_event(self, event):
        row = dict(event, event_id=f"evt_{len(self.events) + 1}")
        self.events.append(row)
        return row["event_id"]


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

    def test_explicit_two_item_speech_cannot_be_collapsed_by_model(self):
        spoken = "5 बोरी cement और 1 ton सरिया becha cash"
        state = {"said": [spoken], "history": [{"role": "user", "content": spoken}]}
        collapsed_output = {
            "intent": "sale", "metric": None,
            "items": [{"sku_id": "CEM_ULTRATECH_PPC", "family": "cement",
                       "name": "cement", "in_catalogue": True, "qty": 5,
                       "unit": "tonne", "rate": 500, "payment": "cash"}],
        }
        with patch.object(conversation.sarvam_client, "chat_json",
                          return_value=collapsed_output):
            result = conversation._extract(state, self.repo)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual([row["qty"] for row in result["items"]], [5.0, 1.0])
        self.assertEqual([row["unit"] for row in result["items"]],
                         ["bori", "tonne"])
        self.assertEqual([row["family"] for row in result["items"]],
                         ["cement", "tmt"])

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

    def test_credit_deadline_keeps_every_order_item(self):
        items = [
            dict(self.extracted["items"][0], rate=500, payment="credit"),
            dict(self.extracted["items"][1], rate=65, payment="credit"),
        ]
        state = {"flow": "live_sale", "said": [], "history": [],
                 "locked_intent": "sale", "draft_items": items}
        out = conversation._order_flow(state, "sale", items, self.repo)
        out = conversation._apply_customer_slot(
            out["state"], "customer_phone", "9876543210", self.repo)
        out = conversation._apply_customer_slot(
            out["state"], "customer_name", "Ravi Builder", self.repo)
        out = conversation._apply_customer_slot(
            out["state"], "deadline", "ek mahine baad", self.repo)
        self.assertTrue(out["confirmation_required"])
        self.assertEqual(len(out["confirmation"]["items"]), 2)
        self.assertEqual(out["confirmation"]["payment_deadline"], "2026-08-25")

    def test_native_hindi_followup_values_are_understood(self):
        self.assertEqual(conversation._answer_number("पचास बोरी"), 50)
        self.assertEqual(conversation._detect_payment("नकद"), "cash")
        self.assertEqual(conversation._detect_payment("उधार"), "credit")
        self.assertEqual(conversation.parse_deadline("कल"), "2026-07-27")
        self.assertEqual(conversation.parse_deadline("ek mahine baad"), "2026-08-25")
        self.assertEqual(conversation.parse_deadline("do hafte baad"), "2026-08-09")
        self.assertEqual(conversation.parse_deadline("15 August"), "2026-08-15")

    def test_frozen_capital_question_is_not_forced_into_live_sale(self):
        phrases = [
            "मेरा कौन सा माल 60 दिनों से नहीं बिका है",
            "mera kaun sa maal 60 dinon se nahi bika hai",
            "which stock has not sold in 60 days",
        ]
        for phrase in phrases:
            self.assertEqual(conversation._quick_route(phrase),
                             ("analytics", "frozen"))

        expected = {
            "state": None, "say": "frozen answer", "listen": False, "done": True,
            "summary": {"items": []},
        }
        with patch.object(conversation.sarvam_client, "has_key",
                          return_value=False), \
             patch.object(conversation, "_extract") as extract, \
             patch.object(conversation, "_analytics_answer",
                          return_value=expected) as analytics:
            out = conversation.converse(
                None, phrases[0], "live_sale", self.repo)

        self.assertEqual(out, expected)
        analytics.assert_called_once_with("frozen", self.repo)
        extract.assert_not_called()

    def test_normal_sale_does_not_match_frozen_capital_route(self):
        self.assertIsNone(
            conversation._quick_route("5 bori cement becha cash"))

    def test_outside_product_is_rejected_before_the_llm(self):
        hallucinated = {
            "intent": "sale", "metric": None,
            "items": [{"sku_id": "TMT_12_FE500D_TATA", "family": "tmt",
                       "name": "tiles", "in_catalogue": True,
                       "qty": 5, "unit": "piece", "rate": 100,
                       "payment": "cash"}],
        }
        with patch.object(conversation.sarvam_client, "has_key",
                          return_value=True), \
             patch.object(conversation.sarvam_client, "chat_json",
                          return_value=hallucinated) as chat:
            out = conversation.converse(
                None, "5 tiles bech diya cash", "live_sale", self.repo)

        self.assertIn("hum nahi rakhte", out["say"])
        self.assertNotIn("Kaunsa sariya", out["say"])
        chat.assert_not_called()

    def test_llm_cannot_remap_unknown_product_to_a_stocked_sku(self):
        hallucinated = {
            "intent": "stock_query", "metric": None,
            "items": [{"sku_id": "TMT_12_FE500D_TATA", "family": "tmt",
                       "name": "sariya", "in_catalogue": True,
                       "qty": None, "unit": None, "rate": None,
                       "payment": None}],
        }
        state = {
            "said": ["kya tiles available hai"],
            "history": [{"role": "user", "content": "kya tiles available hai"}],
        }
        with patch.object(conversation.sarvam_client, "chat_json",
                          return_value=hallucinated):
            result = conversation._extract(state, self.repo)

        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["items"][0]["in_catalogue"])
        self.assertIsNone(result["items"][0]["sku_id"])
        self.assertIsNone(result["items"][0]["family"])

    def test_mixed_order_keeps_stocked_item_and_skips_tiles(self):
        model_output = {
            "intent": "sale", "metric": None,
            "items": [
                {"sku_id": "TMT_12_FE500D_TATA", "family": "tmt",
                 "name": "tiles", "in_catalogue": True, "qty": 5,
                 "unit": "piece", "rate": 100, "payment": "cash"},
                {"sku_id": "CEM_ULTRATECH_PPC", "family": "cement",
                 "name": "PPC cement", "in_catalogue": True, "qty": 10,
                 "unit": "bori", "rate": 400, "payment": "cash"},
            ],
        }
        state = {
            "said": ["5 tiles aur 10 bori PPC cement becha cash"],
            "history": [{"role": "user",
                         "content": "5 tiles aur 10 bori PPC cement becha cash"}],
        }
        with patch.object(conversation.sarvam_client, "chat_json",
                          return_value=model_output):
            result = conversation._extract(state, self.repo)

        self.assertFalse(result["items"][0]["in_catalogue"])
        self.assertIsNone(result["items"][0]["sku_id"])
        self.assertEqual(result["items"][1]["sku_id"], "CEM_ULTRATECH_PPC")
        self.assertTrue(result["items"][1]["in_catalogue"])

    def test_mixed_order_announces_and_remembers_unavailable_item(self):
        model_output = {
            "intent": "sale", "metric": None,
            "items": [
                {"sku_id": "TMT_12_FE500D_TATA", "family": "tmt",
                 "name": "tiles", "in_catalogue": True, "qty": 5,
                 "unit": "piece", "rate": 100, "payment": "cash"},
                {"sku_id": "CEM_ULTRATECH_PPC", "family": "cement",
                 "name": "PPC cement", "in_catalogue": True, "qty": 10,
                 "unit": "bori", "rate": 400, "payment": "cash"},
            ],
        }
        phrase = "5 tiles aur 10 bori PPC cement becha cash"
        with patch.object(conversation.sarvam_client, "has_key",
                          return_value=True), \
             patch.object(conversation.sarvam_client, "chat_json",
                          return_value=model_output):
            out = conversation.converse(
                None, phrase, "live_sale", self.repo)
            self.assertIn("inventory mein nahi hai", out["say"])
            self.assertIn("tiles", out["say"])
            self.assertIn("contact number", out["say"])
            self.assertEqual(
                out["state"]["pending_commit"]["skipped"],
                ["tiles"])

            followup = conversation.converse(
                out["state"], "9876543210", "live_sale", self.repo)
        self.assertNotIn("inventory mein nahi hai", followup["say"])
        self.assertIn("naam kya hai", followup["say"])

    def test_explicit_becha_overrides_missing_llm_intent(self):
        model_output = {
            "intent": "unknown", "metric": None,
            "items": [
                {"sku_id": None, "family": "cement",
                 "name": "cement", "in_catalogue": True, "qty": 10,
                 "unit": "bori", "rate": None, "payment": None},
            ],
        }
        with patch.object(conversation.sarvam_client, "has_key",
                          return_value=True), \
             patch.object(conversation.sarvam_client, "chat_json",
                          return_value=model_output):
            out = conversation.converse(
                None, "10 bori cement becha", "auto", self.repo)

        self.assertEqual(out["state"]["locked_intent"], "sale")
        self.assertIn("Kaunsa cement", out["say"])
        self.assertNotIn("becha ya khareeda", out["say"])

    def test_feminine_sale_verbs_are_detected_deterministically(self):
        for phrase in ("50 tiles bechi hai", "50 tiles बेची है",
                       "50 tiles bechee hain"):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    conversation._detect_transaction_intent(phrase), "sale")

    def test_adjacent_quantity_unit_groups_are_separate_items(self):
        rows = nlp.parse_sale_utterance(
            "50 kg cement 10 ton सरिया और 50 tiles बेची है")
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["qty"] for row in rows], [50, 10, 50])
        self.assertEqual([row["unit"] for row in rows], ["kg", "tonne", None])

    def test_exact_multi_item_bechi_order_keeps_context_until_followups(self):
        phrase = "50 kg cement 10 ton सरिया और 50 tiles बेची है"
        model_output = {
            "intent": "unknown", "metric": None,
            "items": [
                {"sku_id": None, "family": "cement", "name": "cement",
                 "in_catalogue": True, "qty": 50, "unit": "kg",
                 "rate": None, "payment": None},
                {"sku_id": None, "family": "tmt", "name": "sariya",
                 "in_catalogue": True, "qty": 10, "unit": "tonne",
                 "rate": None, "payment": None},
                {"sku_id": "TMT_12_FE500D_TATA", "family": "tmt",
                 "name": "tiles", "in_catalogue": True, "qty": 50,
                 "unit": None, "rate": None, "payment": None},
            ],
        }
        with patch.object(conversation.sarvam_client, "has_key",
                          return_value=True), \
             patch.object(conversation.sarvam_client, "chat_json",
                          return_value=model_output) as chat:
            out = conversation.converse(None, phrase, "auto", self.repo)
            self.assertEqual(out["state"]["locked_intent"], "sale")
            self.assertEqual(out["state"]["original_transcript"], phrase)
            self.assertEqual(out["state"]["skipped_items"], ["tiles"])
            self.assertEqual(len(out["state"]["draft_items"]), 2)
            self.assertIn("tiles inventory mein nahi hai", out["say"])
            self.assertIn("Kaunsa cement", out["say"])

            out = conversation.converse(
                out["state"], "PPC", "auto", self.repo)
            self.assertEqual(len(out["state"]["draft_items"]), 2)
            self.assertIn("Kaunsa sariya", out["say"])
            self.assertEqual(out["state"]["original_transcript"], phrase)
            self.assertGreaterEqual(len(out["state"]["history"]), 4)
            self.assertEqual(chat.call_count, 1)

    def test_rate_is_applied_per_quoted_unit(self):
        tmt = self.repo.sku("TMT_16_FE500D_TATA")
        self.assertEqual(
            ledger.line_amount(1, "tonne", 1000, "tonne", tmt), 1000)
        self.assertEqual(ledger.rate_to_base(1000, "tonne", tmt), 1)
        self.assertEqual(
            ledger.line_amount(1, "tonne", 65, "kg", tmt), 65000)

        items = [
            {"sku_id": "CEM_ULTRATECH_OPC53", "qty": 10, "unit": "bori",
             "rate": 100, "rate_unit": "bori", "payment": "cash"},
            {"sku_id": "TMT_16_FE500D_TATA", "qty": 1, "unit": "tonne",
             "rate": 1000, "rate_unit": "tonne", "payment": "cash"},
        ]
        state = {
            "said": [], "history": [],
            "pending_commit": {"flow": "sale", "items": items, "skipped": []},
            "customer": {"customer_id": "cust_test", "name": "Pankaj Sharma",
                         "phone": "+919876543210"},
        }
        out = conversation._prepare_confirmation(state, self.repo)
        self.assertEqual(
            [row["amount"] for row in out["confirmation"]["items"]],
            [1000, 1000])
        self.assertEqual(out["confirmation"]["total"], 2000)

        with patch.object(main, "repo", self.repo):
            committed = main._write_events(
                "sale", [items[1]], "2026-07-26", "exact", "voice_live")
        event = self.repo.events[-1]
        self.assertEqual(event["rate"], 1)
        self.assertEqual(event["quoted_rate"], 1000)
        self.assertEqual(event["rate_unit"], "tonne")
        self.assertEqual(committed["committed"][0]["amount"], 1000)


if __name__ == "__main__":
    unittest.main()
