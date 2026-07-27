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
        self.known_customers = []
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

    def customers(self):
        return list(self.known_customers)

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
            for answer in ("450", "65", "cash", "Ravi Builder", "9876543210"):
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
        self.assertEqual(chat.call_args.kwargs["timeout"], 75)
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
        analytics.assert_called_once()
        metric, repo, state = analytics.call_args.args
        self.assertEqual(metric, "frozen")
        self.assertIs(repo, self.repo)
        self.assertEqual(state["lang"], "hi")
        extract.assert_not_called()

    def test_normal_sale_does_not_match_frozen_capital_route(self):
        self.assertIsNone(
            conversation._quick_route("5 bori cement becha cash"))

    def test_outside_product_is_offered_as_new_stock_not_remapped(self):
        """An unstocked product must never be silently remapped to a stocked
        SKU. It is offered as a new catalogue entry instead — and that decision
        is reached without spending an LLM call."""
        hallucinated = {
            "intent": "sale", "metric": None,
            "items": [{"sku_id": "TMT_12_FE500D_TATA", "family": "tmt",
                       "name": "wire", "in_catalogue": True,
                       "qty": 5, "unit": "piece", "rate": 100,
                       "payment": "cash"}],
        }
        with patch.object(conversation.sarvam_client, "has_key",
                          return_value=True), \
             patch.object(conversation.sarvam_client, "chat_json",
                          return_value=hallucinated) as chat:
            out = conversation.converse(
                None, "5 wire bech diya cash", "live_sale", self.repo)

        self.assertEqual(out["state"]["awaiting_order"]["slot"], "confirm_add")
        self.assertIn("wire", out["say"])
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
            "said": ["kya wire available hai"],
            "history": [{"role": "user", "content": "kya wire available hai"}],
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
                 "name": "wire", "in_catalogue": True, "qty": 5,
                 "unit": "piece", "rate": 100, "payment": "cash"},
                {"sku_id": "CEM_ULTRATECH_PPC", "family": "cement",
                 "name": "PPC cement", "in_catalogue": True, "qty": 10,
                 "unit": "bori", "rate": 400, "payment": "cash"},
            ],
        }
        state = {
            "said": ["5 wire aur 10 bori PPC cement becha cash"],
            "history": [{"role": "user",
                         "content": "5 wire aur 10 bori PPC cement becha cash"}],
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
                 "name": "wire", "in_catalogue": True, "qty": 5,
                 "unit": "piece", "rate": 100, "payment": "cash"},
                {"sku_id": "CEM_ULTRATECH_PPC", "family": "cement",
                 "name": "PPC cement", "in_catalogue": True, "qty": 10,
                 "unit": "bori", "rate": 400, "payment": "cash"},
            ],
        }
        phrase = "5 wire aur 10 bori PPC cement becha cash"
        with patch.object(conversation.sarvam_client, "has_key",
                          return_value=True), \
             patch.object(conversation.sarvam_client, "chat_json",
                          return_value=model_output):
            out = conversation.converse(
                None, phrase, "live_sale", self.repo)
            self.assertIn("inventory mein nahi hai", out["say"])
            self.assertIn("wire", out["say"])
            # wire isn't stocked — Chhotu offers to add it rather than
            # dropping the line on the floor
            self.assertIn("add kar doon", out["say"])
            out = conversation.converse(out["state"], "nahi", "live_sale", self.repo)
            self.assertIn("naam kya hai", out["say"])
            self.assertEqual(
                out["state"]["pending_commit"]["skipped"],
                ["wire"])

            followup = conversation.converse(
                out["state"], "Ravi Builder", "live_sale", self.repo)
        # the unavailable-item notice is announced once, not on every turn
        self.assertNotIn("inventory mein nahi hai", followup["say"])
        self.assertIn("number bataiye", followup["say"])

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
        phrase = "50 kg cement 10 ton सरिया और 50 wire बेची है"
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
                 "name": "wire", "in_catalogue": True, "qty": 50,
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
            # wire is unstocked -> offered, declined, then remembered as skipped
            self.assertIn("add kar doon", out["say"])
            out = conversation.converse(out["state"], "nahi", "auto", self.repo)
            self.assertEqual(out["state"]["skipped_items"], ["wire"])
            # declined on purpose — don't lecture the owner about it afterwards
            self.assertNotIn("wire inventory mein nahi hai", out["say"])
            self.assertEqual(len(out["state"]["draft_items"]), 2)
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


class CustomerByNameTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.items = [{"sku_id": "CEM_ULTRATECH_PPC", "family": "cement",
                       "name": "PPC cement", "in_catalogue": True, "qty": 10,
                       "unit": "bori", "rate": 400, "payment": "cash"}]
        self.state = {"flow": "live_sale", "said": [], "history": [],
                      "locked_intent": "sale", "draft_items": self.items}

    def _start(self):
        return conversation._order_flow(self.state, "sale", self.items, self.repo)

    def test_name_is_asked_before_the_phone_number(self):
        out = self._start()
        self.assertEqual(out["state"]["awaiting"], "customer_name")
        self.assertIn("naam", out["say"])

    def test_known_name_resolves_without_asking_for_a_number(self):
        self.repo.known_customers = [
            {"customer_id": "c1", "name": "Ravi Builder", "phone": "+919876543210"}]
        out = self._start()
        out = conversation.converse(out["state"], "Ravi Builder", "live_sale", self.repo)
        self.assertTrue(out["confirmation_required"])
        self.assertEqual(out["confirmation"]["customer"]["customer_id"], "c1")

    def test_duplicate_names_are_offered_as_numbered_options(self):
        self.repo.known_customers = [
            {"customer_id": "c1", "name": "Ramesh Kumar", "phone": "+919000000001"},
            {"customer_id": "c2", "name": "Ramesh Traders", "phone": "+919000000002"},
        ]
        out = self._start()
        out = conversation.converse(out["state"], "Ramesh", "live_sale", self.repo)
        self.assertEqual(out["state"]["awaiting"], "customer_choice")
        self.assertEqual([c["customer_id"] for c in out["customer_options"]],
                         ["c1", "c2"])
        out = conversation.converse(out["state"], "doosra", "live_sale", self.repo)
        self.assertTrue(out["confirmation_required"])
        self.assertEqual(out["confirmation"]["customer"]["customer_id"], "c2")

    def test_an_exact_name_wins_over_a_partial_one(self):
        self.repo.known_customers = [
            {"customer_id": "c1", "name": "Ramesh", "phone": "+919000000001"},
            {"customer_id": "c2", "name": "Ramesh Traders", "phone": "+919000000002"},
        ]
        self.assertEqual(
            [c["customer_id"]
             for c in conversation._find_customers_by_name(self.repo, "Ramesh")],
            ["c1"])

    def test_unknown_name_asks_for_a_number_and_opens_the_account(self):
        out = self._start()
        out = conversation.converse(out["state"], "Naya Seth", "live_sale", self.repo)
        self.assertEqual(out["state"]["awaiting"], "customer_phone")
        self.assertIn("number", out["say"])
        out = conversation.converse(out["state"], "9812345678", "live_sale", self.repo)
        self.assertTrue(out["confirmation_required"])
        self.assertEqual(out["confirmation"]["customer"]["name"], "Naya Seth")
        self.assertEqual(self.repo.saved_customer["phone"], "+919812345678")


class NewItemConfirmationTests(unittest.TestCase):
    """A delivery of something unstocked must be confirmed before it becomes a
    permanent SKU — and something outside every known hardware category has to
    be confirmed as genuinely new rather than misheard."""

    def setUp(self):
        self.repo = FakeRepo()
        self.created = []
        self.repo.upsert_sku = self._upsert

    def _upsert(self, sku):
        self.created.append(sku)
        self.repo.catalogue.append(sku)
        self.repo.by_id[sku["sku_id"]] = sku

    def _deliver(self, name, family=None):
        items = [{"sku_id": None, "family": family, "name": name,
                  "in_catalogue": True, "qty": 10, "unit": "piece",
                  "rate": 100, "payment": None}]
        state = {"flow": "delivery", "said": [], "history": [],
                 "locked_intent": "delivery", "draft_items": items}
        return conversation._order_flow(state, "delivery", items, self.repo)

    def test_known_category_asks_before_adding(self):
        out = self._deliver("Havells copper wire")
        self.assertEqual(out["state"]["awaiting_order"]["slot"], "confirm_add")
        self.assertIn("add kar doon", out["say"])
        self.assertEqual(self.created, [])

    def test_declining_just_acknowledges_and_resets(self):
        """Saying no is an answer, not an error. Chhotu accepts it, writes
        nothing, and releases the screen — no repeating 'we don't stock that'
        back at the owner who just declined to stock it."""
        out = self._deliver("Havells copper wire")
        out = conversation._apply_order_slot(out["state"], "nahi", self.repo)
        self.assertEqual(self.created, [])
        self.assertNotIn("nahi rakhte", out["say"])
        self.assertIn("Theek hai", out["say"])
        self.assertTrue(out["done"])
        self.assertFalse(out["listen"])
        self.assertTrue(out["reset"])

    def test_accepting_provisions_the_sku(self):
        out = self._deliver("Havells copper wire")
        out = conversation._apply_order_slot(out["state"], "haan add karo", self.repo)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(out["state"]["draft_items"][0]["sku_id"],
                         self.created[0]["sku_id"])

    def test_unrecognised_word_is_double_checked_as_new_or_a_mistake(self):
        out = self._deliver("blorptron")
        self.assertEqual(out["state"]["awaiting_order"]["slot"], "verify_new")
        self.assertIn("galti se", out["say"])
        out = conversation._apply_order_slot(out["state"], "galti se bol diya", self.repo)
        self.assertEqual(self.created, [])

    def test_already_stocked_size_is_never_offered_as_a_new_item(self):
        items = [{"sku_id": None, "family": "tmt", "name": "barah mm sariya",
                  "in_catalogue": True, "qty": 2, "unit": "tonne",
                  "rate": 57000, "payment": None}]
        state = {"flow": "delivery", "said": [], "history": [],
                 "locked_intent": "delivery", "draft_items": items}
        out = conversation._order_flow(state, "delivery", items, self.repo)
        self.assertEqual(self.created, [])
        self.assertEqual(items[0]["sku_id"], "TMT_12_FE500D_TATA")
        self.assertTrue(out["confirmation_required"])


class LearnedAliasTests(unittest.TestCase):
    """After 60 days of confirmations the shop's own words resolve straight to
    a SKU instead of triggering another 'kaunsa sariya?' question."""

    def setUp(self):
        self.repo = FakeRepo()
        self.repo.load_learning = lambda: {
            "aliases_learned": [
                {"phrase": "mota sariya", "sku_id": "TMT_16_FE500D_TATA"},
                {"phrase": "patla sariya", "sku_id": "TMT_12_FE500D_TATA"},
                {"phrase": "ppc cement", "sku_id": "CEM_ULTRATECH_PPC"},
            ],
            "attribute_priors": [], "unit_priors": [], "corrections": [],
        }

    def test_learned_phrases_resolve_to_one_sku(self):
        for phrase, sku in (("2 ton mota sariya", "TMT_16_FE500D_TATA"),
                            ("patla sariya", "TMT_12_FE500D_TATA"),
                            ("50 bori PPC cement", "CEM_ULTRATECH_PPC")):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    conversation._sku_from_learned_alias(phrase, self.repo), sku)

    def test_a_bare_family_word_still_asks(self):
        for phrase in ("sariya", "cement", "10 bori cement"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(
                    conversation._sku_from_learned_alias(phrase, self.repo))

    def test_sale_of_a_learned_phrase_skips_the_product_question(self):
        items = [{"sku_id": None, "family": "tmt", "name": "mota sariya",
                  "in_catalogue": True, "qty": 2, "unit": "tonne",
                  "rate": None, "payment": "cash"}]
        state = {"flow": "live_sale", "said": [], "history": [],
                 "locked_intent": "sale", "draft_items": items}
        out = conversation._order_flow(state, "sale", items, self.repo)
        self.assertEqual(out["state"]["awaiting_order"]["slot"], "rate")
        self.assertIn("16mm", out["say"])


class SummaryRoutingTests(unittest.TestCase):
    def test_day_and_week_summaries_route_deterministically(self):
        for phrase in ("aaj ka hisaab batao", "aaj ka summary",
                       "today's summary", "poora hisaab batao",
                       "summary batao", "आज का सारांश", "business kaisa raha"):
            with self.subTest(phrase=phrase):
                self.assertEqual(conversation._quick_route(phrase),
                                 ("analytics", "day_summary"))
        for phrase in ("hafte ka hisaab batao", "weekly summary",
                       "is hafte ka business kaisa raha", "saptah ka summary",
                       "हफ्ते का सारांश बताओ", "हफ्ते की रिपोर्ट",
                       "saat din ka hisaab", "weekly report do"):
            with self.subTest(phrase=phrase):
                self.assertEqual(conversation._quick_route(phrase),
                                 ("analytics", "week_summary"))

    def test_a_week_word_upgrades_the_day_scoped_metrics(self):
        # margin/cash are per-day numbers, so a week word means the roll-up…
        for phrase in ("pichhle hafte ka margin", "hafte ka cash kitna aaya"):
            with self.subTest(phrase=phrase):
                self.assertEqual(conversation._quick_route(phrase),
                                 ("analytics", "week_summary"))
        # …but a balance is point-in-time and a week word changes nothing.
        self.assertEqual(conversation._quick_route("hafte ka total udhaar kitna"),
                         ("analytics", "udhaar"))
        self.assertEqual(conversation._quick_route("aaj ka margin"),
                         ("analytics", "margin"))

    def test_optional_nukta_spellings_match_the_same_way(self):
        """The nukta (U+093C) is optional in ordinary Devanagari typing and in
        STT output, so "हफ़्ते" and "हफ्ते" are the same word on different
        codepoints. Every deterministic matcher must see them as equal."""
        for with_nukta, without in (("हफ़्ते का हिसाब", "हफ्ते का हिसाब"),
                                    ("साप्ताहिक रिपोर्ट", "साप्ताहिक रिपोर्ट")):
            with self.subTest(phrase=with_nukta):
                self.assertEqual(conversation._quick_route(with_nukta),
                                 conversation._quick_route(without))
                self.assertEqual(conversation._quick_route(with_nukta),
                                 ("analytics", "week_summary"))
        self.assertEqual(conversation._detect_payment("नक़द"), "cash")
        self.assertEqual(conversation._detect_payment("नकद"), "cash")
        self.assertEqual(
            conversation._detect_transaction_intent("10 बोरी सीमेंट ख़रीदा"),
            "delivery")
        self.assertEqual(conversation._detect_lang("हफ़्ते का हिसाब"), "hi")

    def test_a_plain_sale_is_not_mistaken_for_a_summary(self):
        self.assertIsNone(conversation._quick_route("5 bori cement becha cash"))
        self.assertIsNone(conversation._quick_route("aaj 10 bori cement becha"))


if __name__ == "__main__":
    unittest.main()
