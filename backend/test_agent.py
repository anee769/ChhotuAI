"""Every voice-agent tool, exercised against an in-memory shop.

These run without a database on purpose. The point of the tool layer is that
the agent never has to guess, so what is asserted here is mostly refusals:
ambiguous item -> a question, credit with no customer -> a question, payment
larger than the balance -> refused. A tool that quietly does the wrong thing is
far worse over a phone line than one that asks.
"""
from __future__ import annotations

import json
import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import os
os.environ.setdefault("CHHOTU_TODAY", "2026-07-26")
os.environ.setdefault("CHHOTU_SECRET", "test-secret")
os.environ.setdefault("SAMVAAD_WEBHOOK_SECRET", "test-agent-secret")

import agent
import clock
import main

USER = {"user_id": "u_test", "phone": "+919999999999", "name": "Test Owner",
        "shop_name": "Test Traders"}


class FakeRepo:
    def __init__(self):
        path = Path(__file__).resolve().parent.parent / "data" / "catalogue.json"
        self.catalogue = json.loads(path.read_text(encoding="utf-8"))
        self.by_id = {r["sku_id"]: r for r in self.catalogue}
        self.events, self._customers, self._recv, self._pay = [], [], [], []

    # --- catalogue -------------------------------------------------------
    def load_catalogue(self):
        return self.catalogue

    def sku(self, sku_id):
        return self.by_id.get(sku_id)

    def upsert_sku(self, sku):
        self.by_id[sku["sku_id"]] = sku
        self.catalogue = [s for s in self.catalogue if s["sku_id"] != sku["sku_id"]]
        self.catalogue.append(sku)

    def load_learning(self):
        return {"aliases_learned": [], "attribute_priors": [],
                "unit_priors": [], "corrections": []}

    def load_config(self):
        return {"shop_name": "Test Traders", "gstin": "27ABCDE1234F1Z5"}

    def gst_rate_for(self, sku):
        return float(sku.get("gst_rate") or 18)

    # --- events ----------------------------------------------------------
    def all_events(self):
        return list(self.events)

    def events_for_sku(self, sku_id):
        return [e for e in self.events if e["sku_id"] == sku_id]

    def append_event(self, event):
        eid = f"evt_{len(self.events) + 1:04d}"
        self.events.append(dict(event, event_id=eid))
        return eid

    # --- customers and credit -------------------------------------------
    @staticmethod
    def normalize_phone(phone):
        return "+91" + "".join(c for c in str(phone) if c.isdigit())[-10:]

    def customers(self):
        return list(self._customers)

    def customer(self, cid):
        return next((c for c in self._customers if c["customer_id"] == cid), None)

    def upsert_customer(self, phone, name=None):
        row = {"customer_id": f"cust_{len(self._customers) + 1}",
               "phone": self.normalize_phone(phone), "name": name}
        self._customers.append(row)
        return row

    def receivables(self):
        return list(self._recv)

    def payments(self):
        return list(self._pay)

    def add_receivable(self, customer_id, amount, deadline, sale_event_ids):
        row = {"receivable_id": f"recv_{len(self._recv) + 1}",
               "customer_id": customer_id, "amount": round(float(amount), 2),
               "deadline": deadline, "sale_event_ids": list(sale_event_ids),
               "created_at": "2026-07-26T10:00:00", "status": "open"}
        self._recv.append(row)
        return row

    def add_payment(self, customer_id, amount, paid_on, note=""):
        row = {"payment_id": f"pay_{len(self._pay) + 1}",
               "customer_id": customer_id, "amount": round(float(amount), 2),
               "paid_on": paid_on, "note": note, "created_at": paid_on}
        self._pay.append(row)
        return row


CEMENT_PPC = "CEM_ULTRATECH_PPC"
TMT_12 = "TMT_12_FE500D_TATA"


def _opening(repo, sku_id, qty, unit, rate):
    repo.append_event({"type": "opening_balance", "sku_id": sku_id, "qty": qty,
                       "unit": unit, "rate": rate, "quoted_rate": rate,
                       "rate_unit": unit, "occurred_on": "2026-07-01",
                       "precision": "exact", "confidence": 1.0,
                       "payment": None, "customer_id": None})


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        _opening(self.repo, CEMENT_PPC, 200, "bori", 385)
        _opening(self.repo, TMT_12, 3, "tonne", 55.5)
        # Bind the fake into the request-scoped proxy the write path uses, so
        # main._write_events writes here and not into a real shop's ledger.
        main._CURRENT.set(self.repo)
        main._CURRENT_USER.set(USER)

    def call(self, tool, **args):
        fn = agent.TOOLS[agent._ALIASES.get(tool, tool)][0]
        return fn(self.repo, USER, args)

    def test_every_registered_tool_handles_a_safe_smoke_request(self):
        """Exercise the function behind every manifest entry at least once.

        Detailed assertions below cover the transactional tools. This catches
        newly registered reporting/sending tools that reference a missing
        repository method or return a non-JSON value before they ever reach
        Samvaad.
        """
        import notify
        import presentations
        with patch.object(notify, "send_summary",
                          return_value={"sent_to": "+910000000000"}), \
                patch.object(notify, "send_due_reminders",
                             return_value={"sent": [], "skipped": [],
                                           "as_of": "2026-07-26"}), \
                patch.object(presentations, "store",
                             return_value={"presentation_id": "vp_test"}):
            for name, (fn, _description) in agent.TOOLS.items():
                with self.subTest(tool=name):
                    out = fn(self.repo, USER, {})
                    self.assertIsInstance(out, dict)
                    json.dumps(out)

    # --- reads -----------------------------------------------------------
    def test_profile_counts_the_shop(self):
        out = self.call("shop_profile")
        self.assertEqual(out["shop"], "Test Traders")
        self.assertGreater(out["item_count"], 0)

    def test_inventory_lists_every_item_with_stock(self):
        out = self.call("list_inventory")
        self.assertEqual(out["count"], len(self.repo.catalogue))
        row = next(i for i in out["items"] if i["sku_id"] == CEMENT_PPC)
        self.assertEqual(row["stock"], 200)

    def test_check_stock_answers_a_specific_item(self):
        out = self.call("check_stock", item="ppc cement")
        self.assertTrue(out["found"])
        self.assertEqual(out["qty"], 200)

    def test_check_stock_asks_instead_of_guessing_between_variants(self):
        out = self.call("check_stock", item="cement")
        self.assertIn("needs", out)
        self.assertGreater(len(out["needs"]["options"]), 1)

    def test_unknown_item_is_reported_not_invented(self):
        out = self.call("check_stock", item="hydraulic press")
        self.assertFalse(out["found"])

    def test_a_miss_names_the_trade_not_the_shelves(self):
        out = self.call("check_stock", item="laptop")
        self.assertFalse(out["stocks_this_kind"])
        self.assertIn("dukaan hain", out["speak"])
        # "hum cement, tiles, tmt ki dukaan hain" describes an inventory
        # table. A shopkeeper names a line of business.
        for family in ("cement", "tiles", "tmt"):
            self.assertNotIn(family, out["speak"])

    def test_shop_kind_comes_from_the_name_and_can_be_overridden(self):
        self.repo.load_config = lambda: {"shop_name": "MM Hardware"}
        self.assertEqual(agent._shop_kind(self.repo, USER), "hardware")
        self.repo.load_config = lambda: {"shop_name": "Sharma & Sons"}
        self.assertEqual(agent._shop_kind(self.repo, USER),
                         agent._DEFAULT_SHOP_KIND)
        self.repo.load_config = lambda: {"shop_name": "Sharma & Sons",
                                         "shop_type": "sanitary aur plumbing"}
        self.assertEqual(agent._shop_kind(self.repo, USER),
                         "sanitary aur plumbing")

    def test_hardware_we_do_not_carry_reads_differently_from_off_trade(self):
        """"Pipe" is our trade and simply unstocked; "laptop" is not. A
        shopkeeper would not answer those two the same way."""
        pipe = self.call("check_stock", item="pipe")
        laptop = self.call("check_stock", item="laptop")
        self.assertEqual(pipe["known_hardware_category"], "pipe")
        self.assertIsNone(laptop["known_hardware_category"])
        self.assertNotEqual(pipe["speak"], laptop["speak"])

    def test_item_details_include_cost_and_gst(self):
        out = self.call("item_details", item="ppc cement")
        self.assertEqual(out["gst_rate"], 28)
        self.assertIsNotNone(out["landed_cost"])

    def test_every_product_tool_accepts_substring_and_sku_id(self):
        """A SKU returned by one tool must be valid input to every next tool."""
        import notify

        cases = {
            "check_stock": lambda out: out.get("found")
            and out.get("sku_id") == CEMENT_PPC,
            "item_details": lambda out: out.get("found")
            and out.get("sku_id") == CEMENT_PPC,
            "price_quote": lambda out: bool(out.get("lines"))
            and out["lines"][0]["sku_id"] == CEMENT_PPC,
            "record_sale": lambda out: out.get("recorded"),
            "record_purchase": lambda out: out.get("recorded"),
            "stock_take": lambda out: out.get("recorded"),
            "update_item": lambda out: out.get("updated")
            and out.get("sku_id") == CEMENT_PPC,
            # Opening stock deliberately prevents deletion; returning this id
            # proves resolution reached the safety rule rather than "not found".
            "remove_item": lambda out: out.get("removed") is False
            and out.get("sku_id") == CEMENT_PPC,
            "send_bill": lambda out: out.get("sent"),
            "show_bill": lambda out: out.get("shown"),
        }

        for reference_field, reference in (
                ("item", "UltraTech PPC"),
                ("sku_id", CEMENT_PPC.lower())):
            for tool, assertion in cases.items():
                with self.subTest(reference=reference_field, tool=tool):
                    repo = FakeRepo()
                    _opening(repo, CEMENT_PPC, 200, "bori", 385)
                    main._CURRENT.set(repo)
                    main._CURRENT_USER.set(USER)
                    args = {
                        reference_field: reference,
                        "qty": 1,
                        "unit": "bori",
                        "rate": 420,
                        "payment": "cash",
                        "request_id": f"audit-{reference_field}-{tool}",
                    }
                    if tool in ("send_bill", "show_bill"):
                        repo.upsert_customer("9876543210", "Audit Customer")
                        args["customer"] = "Audit Customer"
                    fn = agent.TOOLS[tool][0]
                    import presentations
                    with patch.object(notify, "send_bill",
                                      return_value={"total": 420,
                                                    "sent_to": "+919876543210"}), \
                            patch.object(
                                presentations, "store",
                                return_value={"presentation_id": "vp_test"}):
                        out = fn(repo, USER, args)
                    self.assertTrue(assertion(out), (tool, reference, out))

    def test_search_items_accepts_an_exact_sku_id(self):
        out = self.call("search_items", query=CEMENT_PPC.lower())
        self.assertTrue(any(row["sku_id"] == CEMENT_PPC
                            for row in out["items"]), out)

    def test_search_falls_back_to_fuzzy(self):
        self.assertGreater(self.call("search_items", query="tiscon")["count"], 0)

    def test_search_finds_products_however_the_caller_says_it(self):
        """A caller said "Tata Tisco 16 mm" and was told the shop had no such
        thing, about three tonnes sitting on the shelf."""
        for query in ("Tata Tisco 16 mm", "16 mm tata", "टाटा टिस्को टीएमटी",
                      "सीमेंट", "tiscon 16mm"):
            self.assertGreater(self.call("search_items", query=query)["count"],
                               0, query)

    def test_a_question_never_reads_out_a_database_id(self):
        """The shop actually heard "Kajaria mein se kaunsa,
        TILE_KAJARIA_CERAMIC_2X2 ya TILE_KAJARIA_VITRIFIED_600"."""
        out = self.call("check_stock", item="kajaria")
        for option in out["needs"]["options"]:
            self.assertNotIn("_", option, option)
            self.assertFalse(option.isupper(), option)

    def test_an_acronym_spelled_out_in_devanagari_matches(self):
        """"टीएमटी" is how TMT is said aloud; it transliterates to "tiemti"
        and matched nothing at all."""
        self.assertGreater(self.call("search_items", query="टीएमटी")["count"], 0)
        out = self.call("check_stock", item="टीएमटी")
        self.assertTrue(out.get("found") or out.get("needs"), out)

    def test_search_still_finds_nothing_for_nothing(self):
        for query in ("biryani", "laptop"):
            self.assertEqual(self.call("search_items", query=query)["count"],
                             0, query)

    def test_summary_period_words_resolve_to_ranges(self):
        day = self.call("business_summary", period="day")
        week = self.call("business_summary", period="week")
        self.assertEqual(day["start"], day["end"])
        self.assertNotEqual(week["start"], week["end"])
        self.assertEqual(week["end"], clock.today().isoformat())

    def test_explicit_dates_win_over_period(self):
        out = self.call("business_summary", start="2026-07-01", end="2026-07-05")
        self.assertEqual((out["start"], out["end"]), ("2026-07-01", "2026-07-05"))

    def test_price_quote_adds_gst_without_recording_anything(self):
        before = len(self.repo.events)
        out = self.call("price_quote", items=[{"item": "ppc cement", "qty": 10}])
        self.assertEqual(len(self.repo.events), before)
        self.assertGreater(out["gst"], 0)
        self.assertAlmostEqual(out["total"], out["subtotal"] + out["gst"], places=2)

    # --- writes ----------------------------------------------------------
    def test_cash_sale_records_and_reduces_stock(self):
        out = self.call("record_sale", items=[{"item": "ppc cement", "qty": 10,
                                               "rate": 420}], payment="cash")
        self.assertTrue(out["recorded"])
        self.assertEqual(out["total"], 4200)
        self.assertEqual(self.call("check_stock", item="ppc cement")["qty"], 190)

    def test_credit_sale_without_a_customer_is_refused(self):
        out = self.call("record_sale", items=[{"item": "ppc cement", "qty": 5}],
                        payment="credit")
        self.assertFalse(out["recorded"])
        self.assertEqual(out["needs"]["field"], "customer")
        self.assertEqual(self.repo.events, self.repo.events[:2])

    def test_credit_sale_opens_a_receivable(self):
        self.repo.upsert_customer("9876543210", "Ramesh")
        out = self.call("record_sale", items=[{"item": "ppc cement", "qty": 10,
                                               "rate": 420}],
                        payment="credit", customer="Ramesh",
                        payment_deadline="2026-08-10")
        self.assertTrue(out["recorded"])
        self.assertEqual(out["receivable"]["amount"], 4200)
        self.assertEqual(self.call("customer_account", name="Ramesh")["outstanding"],
                         4200)

    def test_ambiguous_item_blocks_the_whole_sale(self):
        out = self.call("record_sale", items=[{"item": "cement", "qty": 5}])
        self.assertFalse(out["recorded"])
        self.assertIn("needs", out)
        self.assertEqual(len(self.repo.events), 2)

    def test_sale_without_a_quantity_asks_for_it(self):
        out = self.call("record_sale", items=[{"item": "ppc cement"}])
        self.assertFalse(out["recorded"])
        self.assertEqual(out["needs"]["field"], "qty")

    def test_a_retried_sale_is_not_written_twice(self):
        args = dict(items=[{"item": "ppc cement", "qty": 10, "rate": 420}],
                    payment="cash", request_id="req-abc")
        first = self.call("record_sale", **args)
        again = self.call("record_sale", **args)
        self.assertTrue(first["recorded"])
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(self.call("check_stock", item="ppc cement")["qty"], 190)

    def test_a_retried_credit_sale_does_not_double_the_debt(self):
        self.repo.upsert_customer("9876543210", "Ramesh")
        args = dict(items=[{"item": "ppc cement", "qty": 10, "rate": 420}],
                    payment="credit", customer="Ramesh", request_id="req-xyz")
        self.call("record_sale", **args)
        self.call("record_sale", **args)
        self.assertEqual(len(self.repo.receivables()), 1)
        self.assertEqual(self.call("customer_account", name="Ramesh")["outstanding"],
                         4200)

    def test_two_genuine_identical_sales_both_land(self):
        """Without a request_id, an identical basket is a second customer —
        not a retry. Dropping it would be silent data loss."""
        args = dict(items=[{"item": "ppc cement", "qty": 10, "rate": 420}],
                    payment="cash")
        self.call("record_sale", **args)
        self.call("record_sale", **args)
        self.assertEqual(self.call("check_stock", item="ppc cement")["qty"], 180)

    def test_a_retried_payment_is_not_credited_twice(self):
        self.repo.upsert_customer("9876543210", "Ramesh")
        self.call("record_sale", items=[{"item": "ppc cement", "qty": 10,
                                         "rate": 420}],
                  payment="credit", customer="Ramesh")
        args = dict(customer="Ramesh", amount=1200, request_id="pay-1")
        self.call("record_payment", **args)
        again = self.call("record_payment", **args)
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(len(self.repo.payments()), 1)

    def test_unfilled_placeholders_are_dropped_not_searched(self):
        """An agent that leaves {{item}} unfilled must get a question, not a
        confident report that {{item}} is out of stock."""
        out = agent.TOOLS["check_stock"][0](
            self.repo, USER, agent._scrub({"item": "{{item}}"}))
        self.assertNotIn("{{", json.dumps(out))
        self.assertFalse(out.get("found"))
        # and it must ask for a name, not describe the absence of one
        self.assertEqual(out["needs"]["field"], "item")
        self.assertNotIn("None", out["speak"])

    def test_a_single_line_may_arrive_flattened(self):
        """Nested arrays are painful to template in the console Body tab."""
        out = self.call("record_sale", item="ppc cement", qty=10, rate=420,
                        payment="cash")
        self.assertTrue(out["recorded"])
        self.assertEqual(out["total"], 4200)

    def test_string_numbers_from_the_template_still_work(self):
        out = self.call("record_sale", item="ppc cement", qty="10", rate="420",
                        payment="cash")
        self.assertEqual(out["total"], 4200)

    def test_stock_value_reports_cost_and_selling_price(self):
        out = self.call("stock_value")
        self.assertGreater(out["at_cost"], 0)
        self.assertGreaterEqual(out["at_selling_price"], out["at_cost"])

    def test_top_items_can_rank_by_margin(self):
        self.call("record_sale", items=[{"item": "ppc cement", "qty": 10,
                                         "rate": 420}], payment="cash")
        out = self.call("top_items", order="margin")
        self.assertIn("margin", out["items"][0])

    def test_adding_an_item_asks_for_its_opening_count(self):
        """A new SKU is uncounted, so every later question answers "gina nahi
        gaya" until somebody counts it."""
        out = self.call("add_item", name="Asian Paints Apcolite 20L",
                        cost_price=3200, unit="bucket")
        self.assertEqual(out["next_step"]["tool"], "stock_take")
        self.assertIn("kitna stock", out["speak"])

    def test_selling_something_unstocked_asks_before_recording(self):
        out = self.call("record_sale", item="Asian Paints Apcolite",
                        qty=2, payment="cash")
        self.assertFalse(out["recorded"])
        self.assertEqual(out["needs"]["field"], "confirm_add")
        self.assertTrue(out["needs"]["stocks_this_kind"])
        self.assertEqual(self.repo.events, self.repo.events[:2])

    def test_obvious_trade_goods_are_not_interrogated(self):
        """A live call put "Birla White Putty" in the same bucket as biryani."""
        for word in ("Birla White Putty", "wall primer", "gitti", "water tank"):
            out = self.call("record_sale", item=word, qty=1, payment="cash")
            self.assertTrue(out["needs"]["stocks_this_kind"], word)

    def test_something_outside_the_trade_is_questioned_harder(self):
        out = self.call("record_sale", item="biryani", qty=2, payment="cash")
        self.assertFalse(out["needs"]["stocks_this_kind"])
        self.assertIn("galti se", out["speak"])

    def test_a_known_item_alongside_an_unknown_one_still_asks(self):
        """Recording the rest and dropping one leaves a sale that does not
        match what left the shop."""
        out = self.call("record_sale", items=[
            {"item": "ppc cement", "qty": 10, "rate": 420},
            {"item": "Apcolite paint", "qty": 2}], payment="cash")
        self.assertFalse(out["recorded"])
        self.assertEqual(len(self.repo.events), 2)

    def test_add_unknown_lets_the_sale_through_once_confirmed(self):
        out = self.call("record_sale", items=[{"item": "ppc cement", "qty": 10,
                                               "rate": 420},
                                              {"item": "Apcolite paint",
                                               "qty": 2}],
                        payment="cash", add_unknown=True)
        self.assertTrue(out["recorded"])
        self.assertEqual(out["unavailable"], ["Apcolite paint"])

    def test_yesterdays_sale_is_dated_yesterday(self):
        """Shopkeepers catch up on the ledger after closing. Recording "kal
        becha" as today moves money between days."""
        self.call("record_sale", item="ppc cement", qty=5, rate=420,
                  payment="cash", occurred_on="kal")
        sale = [e for e in self.repo.events if e["type"] == "sale"][-1]
        self.assertEqual(sale["occurred_on"],
                         (clock.today() - timedelta(days=1)).isoformat())
        self.assertEqual(self.call("business_summary", period="day")["sale"], 0)
        self.assertGreater(
            self.call("business_summary", period="yesterday")["sale"], 0)

    def test_an_explicit_date_is_honoured(self):
        self.call("record_sale", item="ppc cement", qty=1, rate=420,
                  payment="cash", occurred_on="2026-07-20")
        self.assertEqual(
            [e for e in self.repo.events if e["type"] == "sale"][-1]["occurred_on"],
            "2026-07-20")

    def test_a_count_reports_how_far_the_books_were_out(self):
        self.call("stock_take", item="ppc cement", qty=200)
        out = self.call("stock_take", item="ppc cement", qty=173)
        self.assertEqual(out["differences"][0]["difference"], -27)
        self.assertIn("kam nikla", out["speak"])

    def test_a_matching_count_reports_no_discrepancy(self):
        out = self.call("stock_take", item="ppc cement", qty=200)
        self.assertEqual(out["differences"], [])

    def test_the_agent_learns_the_words_the_caller_used(self):
        """The tap flow learns from every confirmation; the agent did not, so
        a shop saying "mota sariya" on ten calls taught the matcher nothing."""
        learned = []
        import learning
        with patch.object(learning, "record_confirmation",
                          side_effect=lambda r, spoken, sku, **k:
                          learned.append((spoken, sku))):
            self.call("record_sale", item="ppc cement", qty=1, rate=420,
                      payment="cash")
        self.assertEqual(learned[0][0], "ppc cement")
        self.assertEqual(learned[0][1], CEMENT_PPC)

    def test_spoken_numbers_are_understood(self):
        """Speech gives "ek sau bees", not "120". float() raised on those and
        the caller heard "kuch gadbad ho gayi"."""
        for said, expect in [("100", 100), (100, 100), ("ek sau", 100),
                             ("ek sau bees", 120), ("do sau", 200),
                             ("एक सौ", 100), ("das", 10), ("1,200", 1200)]:
            self.assertEqual(agent._number(said), float(expect), said)
        for empty in ("", None, "kuch nahi"):
            self.assertIsNone(agent._number(empty), empty)

    def test_a_count_spoken_in_words_records(self):
        out = self.call("stock_take", item="ppc cement", qty="ek sau bees",
                        unit="bori")
        self.assertTrue(out["recorded"], out)
        self.assertEqual(self.call("check_stock", item="ppc cement")["qty"], 120)

    def test_a_sale_spoken_in_words_records(self):
        out = self.call("record_sale", item="ppc cement", qty="das",
                        rate="char sau bees", payment="cash")
        self.assertTrue(out["recorded"], out)
        self.assertEqual(out["total"], 4200)

    def test_a_devanagari_item_name_still_matches(self):
        """Seeded catalogues carry Devanagari aliases; anything added by voice
        carries only Latin, and the miss was then called off-trade."""
        self.call("add_item", name="Asian Paints Apcolite", cost_price=3200,
                  unit="bucket")
        out = self.call("check_stock", item="एशियन पेंट्स एपकोलाइट")
        self.assertTrue(out.get("found") or out.get("needs"), out)

    def test_a_near_miss_asks_instead_of_denying(self):
        """"siment" (from सीमेंट) scores close to Cement without clearing the
        matcher's bar. Saying we don't stock it is a confident wrong answer."""
        out = self.call("check_stock", item="siment")
        self.assertIn("needs", out)
        self.assertTrue(any("Cement" in o for o in out["needs"]["options"]), out)

    def test_a_genuine_miss_is_still_a_miss(self):
        """The phonetic fallback must not turn every unknown word into a
        question about cement."""
        for word in ("laptop", "biryani", "hydraulic press"):
            out = self.call("check_stock", item=word)
            self.assertNotIn("needs", out, word)
            self.assertFalse(out["found"], word)

    def test_purchase_increases_stock(self):
        self.call("record_purchase", items=[{"item": "ppc cement", "qty": 50,
                                             "rate": 390}])
        self.assertEqual(self.call("check_stock", item="ppc cement")["qty"], 250)

    def test_stock_take_overwrites_the_derived_figure(self):
        self.call("stock_take", items=[{"item": "ppc cement", "qty": 173}])
        self.assertEqual(self.call("check_stock", item="ppc cement")["qty"], 173)

    def test_payment_reduces_the_balance(self):
        self.repo.upsert_customer("9876543210", "Ramesh")
        self.call("record_sale", items=[{"item": "ppc cement", "qty": 10,
                                         "rate": 420}],
                  payment="credit", customer="Ramesh")
        out = self.call("record_payment", customer="Ramesh", amount=1200)
        self.assertTrue(out["recorded"])
        self.assertEqual(out["outstanding"], 3000)

    def test_customer_tools_accept_id_phone_and_unique_name_substring(self):
        customer = self.repo.upsert_customer("9876543210", "Ramesh Kumar")
        self.call("record_sale", item="ppc cement", qty=10, rate=420,
                  payment="credit", customer="Ramesh Kumar")
        for args in (
                {"customer_id": customer["customer_id"]},
                {"customer_phone": "9876543210"},
                {"name": "Ramesh"}):
            with self.subTest(args=args):
                out = self.call("customer_account", **args)
                self.assertTrue(out["found"], out)
                self.assertEqual(out["customer_id"], customer["customer_id"])

    def test_overpayment_is_refused(self):
        self.repo.upsert_customer("9876543210", "Ramesh")
        self.call("record_sale", items=[{"item": "ppc cement", "qty": 1,
                                         "rate": 420}],
                  payment="credit", customer="Ramesh")
        out = self.call("record_payment", customer="Ramesh", amount=99999)
        self.assertFalse(out["recorded"])
        self.assertEqual(self.repo.payments(), [])

    def test_ambiguous_customer_name_asks_which(self):
        self.repo.upsert_customer("9876543210", "Ramesh Kumar")
        self.repo.upsert_customer("9876543211", "Ramesh Gupta")
        out = self.call("record_payment", customer="Ramesh", amount=100)
        self.assertFalse(out["recorded"])
        self.assertEqual(len(out["needs"]["options"]), 2)

    def test_show_bill_previews_without_sending_whatsapp(self):
        import notify
        import presentations
        self.repo.upsert_customer("9876543210", "Ramesh Kumar")
        with patch.object(presentations, "store",
                          return_value={"presentation_id": "vp_bill"}) as store, \
                patch.object(notify, "send_bill") as send:
            out = self.call("show_bill", customer="Ramesh",
                            items=json.dumps([
                                {"item": "ppc cement", "qty": 2,
                                 "unit": "bori", "rate": 420},
                                {"sku_id": TMT_12, "qty": 1,
                                 "unit": "tonne", "rate": 55000},
                            ]), payment="cash")
        self.assertTrue(out["shown"])
        self.assertEqual(out["line_count"], 2)
        self.assertEqual(store.call_args.args[1], "bill")
        self.assertEqual(len(store.call_args.args[2]["items"]), 2)
        send.assert_not_called()

    def test_show_summary_previews_without_sending_whatsapp(self):
        import notify
        import presentations
        with patch.object(presentations, "store",
                          return_value={"presentation_id": "vp_summary"}) as store, \
                patch.object(notify, "send_summary") as send:
            out = self.call("show_summary", period="day")
        self.assertTrue(out["shown"])
        self.assertIn("sale", store.call_args.args[2])
        send.assert_not_called()

    def test_new_item_requires_a_cost_price(self):
        out = self.call("add_item", name="Asian Paints Apcolite 20L")
        self.assertFalse(out["added"])
        self.assertEqual(out["needs"]["field"], "cost_price")

    def test_new_item_is_added_and_then_findable(self):
        out = self.call("add_item", name="Asian Paints Apcolite 20L",
                        cost_price=3200, selling_rate=3600, unit="bucket")
        self.assertTrue(out["added"])
        self.assertTrue(self.call("check_stock", item="Apcolite")["found"])

    def test_a_new_item_keeps_its_cost_and_price(self):
        """upsert_sku has no cost_price or selling_rate column, so writing
        those keys dropped both silently: every item the agent added came back
        with no cost, which breaks margin and quotes."""
        self.call("add_item", name="Asian Paints Apcolite 20L",
                  cost_price=3200, selling_rate=3600, unit="bucket",
                  brand="Asian Paints")
        detail = self.call("item_details", item="Apcolite")
        self.assertEqual(detail["landed_cost"], 3200)
        self.assertEqual(detail["selling_rate"], 3600)

    def test_a_new_item_quotes_at_its_own_price(self):
        self.call("add_item", name="Asian Paints Apcolite 20L",
                  cost_price=3200, selling_rate=3600, unit="bucket")
        out = self.call("price_quote", item="Apcolite", qty=2)
        self.assertEqual(out["lines"][0]["rate"], 3600)
        self.assertGreater(out["total"], 7200)

    def test_shop_profile_update_writes_config_and_owner(self):
        saved = {}
        self.repo.save_config = saved.update
        with patch.object(agent.auth, "complete_onboarding") as onboard:
            out = self.call("update_shop_profile", gstin="27ABCDE1234F1Z5",
                            address="Main Road, Pune", owner="Ramesh Sharma")
        self.assertTrue(out["updated"])
        self.assertEqual(saved["gstin"], "27ABCDE1234F1Z5")
        self.assertEqual(onboard.call_args.kwargs["name"], "Ramesh Sharma")

    def test_empty_profile_update_changes_nothing(self):
        self.repo.save_config = lambda patch: self.fail("should not write")
        out = self.call("update_shop_profile")
        self.assertFalse(out["updated"])

    def test_update_item_matches_a_unique_canonical_substring(self):
        out = self.call("update_item", item="UltraTech PPC",
                        selling_rate=450)
        self.assertTrue(out["updated"])
        self.assertEqual(out["sku_id"], CEMENT_PPC)
        self.assertEqual(
            self.repo.sku(CEMENT_PPC)["attributes"]["selling_rate"], 450)

    def test_update_item_understands_hindi_ppc_acronym_and_brand(self):
        out = self.call("update_item", item="अल्ट्राटेक पी पी सी सीमेंट",
                        unit="bori")
        self.assertTrue(out["updated"])
        self.assertEqual(out["sku_id"], CEMENT_PPC)

    def test_update_item_matches_sku_id_passed_as_item(self):
        out = self.call("update_item", item="cem_ultratech_ppc",
                        selling_rate=451)
        self.assertTrue(out["updated"])
        self.assertEqual(out["sku_id"], CEMENT_PPC)

    def test_update_item_explicit_sku_id_wins_over_conflicting_item(self):
        out = self.call("update_item", item="UltraTech OPC 53",
                        sku_id="cem_ultratech_ppc", selling_rate=452)
        self.assertTrue(out["updated"])
        self.assertEqual(out["sku_id"], CEMENT_PPC)
        self.assertEqual(
            self.repo.sku(CEMENT_PPC)["attributes"]["selling_rate"], 452)
        self.assertNotEqual(
            self.repo.sku("CEM_ULTRATECH_OPC53")
            .get("attributes", {}).get("selling_rate"), 452)

    def test_update_item_does_not_guess_an_ambiguous_substring(self):
        out = self.call("update_item", item="UltraTech", selling_rate=450)
        self.assertFalse(out["updated"])
        self.assertIn("needs", out)
        self.assertGreaterEqual(len(out["needs"]["options"]), 2)

    def test_duplicate_item_is_not_added_twice(self):
        out = self.call("add_item", name="ppc cement", cost_price=400)
        self.assertFalse(out["added"])


class DispatchTests(unittest.TestCase):
    """handle() is the only entry point, so identity is enforced there."""

    def test_unknown_identity_gets_no_data(self):
        with patch.object(agent.auth, "all_users", return_value=[
                {"user_id": "u_other", "phone": "+918888888888"}]):
            out = agent.handle("shop_profile", "+910000000000", {})
        self.assertFalse(out["authorised"])
        self.assertNotIn("item_count", out)

    def test_a_wrong_shop_key_opens_nobody(self):
        with patch.object(agent.auth, "all_users", return_value=[
                {"user_id": "u_other", "phone": "+918888888888"}]):
            out = agent.handle("shop_profile", "", {}, key="0" * 32)
        self.assertFalse(out["authorised"])

    def test_a_refusal_says_failure_in_every_field(self):
        """authorised=false alone was narrated back as "likh diya hai" for a
        write that never happened."""
        with patch.object(agent.auth, "all_users", return_value=[]):
            out = agent.handle("add_item", "917006322772", {"name": "x"})
        for key in ("ok", "authorised", "recorded", "added", "updated", "sent"):
            self.assertFalse(out[key], key)
        self.assertEqual(json.loads(out["facts"])["error"], "not_authorised")

    def test_blank_identity_is_not_a_wildcard(self):
        self.assertFalse(agent.handle("shop_profile", "", {})["authorised"])

    def test_unresolved_template_does_not_shadow_the_caller(self):
        """A phone call has no shop_key, so the console sends "{{shop_key}}"
        literally. That must not be mistaken for a credential."""
        shop = {"user_id": "u_1", "phone": "+917006322772", "shop_name": "S"}
        with patch.object(agent.auth, "all_users", return_value=[shop]), \
                patch.object(agent.sqlrepo, "SqlRepo", return_value=object()):
            user, repo = agent.shop_for_caller("917006322772", "{{shop_key}}")
        self.assertEqual(user, shop)

    def test_an_unresolved_template_alone_authorises_nobody(self):
        shop = {"user_id": "u_1", "phone": "+917006322772"}
        with patch.object(agent.auth, "all_users", return_value=[shop]):
            self.assertEqual(agent.shop_for_caller("{{caller}}", "{{shop_key}}"),
                             (None, None))

    def test_shop_key_is_stable_and_per_shop(self):
        self.assertEqual(agent.shop_key("u_1"), agent.shop_key("u_1"))
        self.assertNotEqual(agent.shop_key("u_1"), agent.shop_key("u_2"))

    def test_secret_comparison_rejects_a_wrong_value(self):
        self.assertTrue(agent.verify_secret("test-agent-secret"))
        self.assertFalse(agent.verify_secret("nope"))

    def test_an_unknown_tool_does_not_leak_the_catalogue(self):
        shop = {"user_id": "u_1", "phone": "+917006322772", "shop_name": "S"}
        with patch.object(agent.auth, "all_users", return_value=[shop]), \
                patch.object(main, "bind_user"):
            out = agent.handle("list_all_secrets", "917006322772", {})
        self.assertEqual(out["error"], "unknown_tool")
        self.assertNotIn("known_tools", out)
        self.assertNotIn("record_sale", json.dumps(out))

    def test_a_crash_returns_the_type_not_the_message(self):
        shop = {"user_id": "u_1", "phone": "+917006322772", "shop_name": "S"}
        boom = lambda *a: (_ for _ in ()).throw(
            RuntimeError("password=hunter2 at /var/task/db.py"))
        with patch.object(agent.auth, "all_users", return_value=[shop]), \
                patch.object(main, "bind_user"), \
                patch.dict(agent.TOOLS, {"dues": (boom, "")}):
            out = agent.handle("dues", "917006322772", {})
        self.assertEqual(out["error"], "RuntimeError")
        self.assertNotIn("hunter2", json.dumps(out))

    def test_nothing_the_agent_reads_contains_an_em_dash(self):
        """A voice model echoes the punctuation it is fed. This caught a stale
        hand-written example that survived the original sweep."""
        import samvaad_config as SC
        speakable = [SC.INSTRUCTIONS, json.dumps(SC.NEEDS_EXAMPLE),
                     json.dumps(SC.MISS_EXAMPLE)]
        speakable += [d for _, d in agent.TOOLS.values()]
        speakable += [json.dumps(v, ensure_ascii=False)
                      for v in SC.EXAMPLES.values()]
        for text in speakable:
            self.assertNotIn("\u2014", text, text[:80])

    def test_customer_names_are_sent_to_tools_in_latin_english_script(self):
        import samvaad_config as SC
        self.assertIn("HAR CUSTOMER TOOL CALL SE PEHLE CHECK KARO",
                      SC.INSTRUCTIONS)
        self.assertIn('GALAT: {"customer": "पंकज शर्मा"}', SC.INSTRUCTIONS)
        self.assertIn('SAHI:  {"customer": "Pankaj Sharma"}', SC.INSTRUCTIONS)
        self.assertIn("TOOL MAT CHALAO", SC.INSTRUCTIONS)
        self.assertIn("Pankaj Sharma", SC.INSTRUCTIONS)
        self.assertIn("Never send Devanagari", SC.PARAM_DOCS["name"][1])
        self.assertIn("Never send Devanagari", SC.PARAM_DOCS["customer"][1])
        for tool in ("customer_account", "record_sale", "record_payment",
                     "show_bill", "send_bill"):
            self.assertIn("Latin script", agent.TOOLS[tool][1])

    def test_a_description_only_names_arguments_the_body_carries(self):
        """The description is the model's only spec for the arguments. Every
        one that said items[{...}] made the model compose an array the body
        template had no slot for, so nothing substituted and the call arrived
        as raw {{placeholders}}."""
        import re
        import samvaad_config as SC
        known = set(SC.PARAM_DOCS) | {"items"}
        for name, (_, desc) in agent.TOOLS.items():
            if "args:" not in desc:
                continue
            named = set(re.findall(r"[a-z_]{3,}", desc.split("args:", 1)[1]))
            self.assertEqual(named & known - set(SC.PARAMS.get(name, ())),
                             set(), f"{name} describes arguments it cannot take")

    def test_every_tool_is_documented_for_the_console(self):
        """A tool with no worked example ships as an empty args box, which the
        agent then fills in by guesswork."""
        import samvaad_config
        self.assertEqual(set(samvaad_config.EXAMPLES), set(agent.TOOLS))
        self.assertEqual(set(samvaad_config.PARAMS), set(agent.TOOLS))
        for name, (example_args, _reply) in samvaad_config.EXAMPLES.items():
            self.assertEqual(
                set(example_args) - set(samvaad_config.PARAMS[name]), set(),
                f"{name} example uses fields absent from its console body")

    def test_checked_in_setup_guide_matches_the_generator(self):
        import samvaad_config
        rendered = io.StringIO()
        with redirect_stdout(rendered):
            samvaad_config.main()
        guide = (Path(__file__).resolve().parent.parent
                 / "docs" / "samvaad-setup.md")
        self.assertEqual(guide.read_text(encoding="utf-8"), rendered.getvalue())

    def test_generated_bodies_are_flat_agent_filled_templates(self):
        """Nested args are not substituted by Samvaad during live calls."""
        import samvaad_config as SC
        for name, params in SC.PARAMS.items():
            args = json.loads(SC.body(name))
            self.assertEqual(set(args), set(params), name)
            self.assertNotIn("args", args, name)
            for k, v in args.items():
                self.assertEqual(v, "{{%s}}" % k, name)

    def test_generated_curls_reference_the_secret_never_contain_it(self):
        """The header is there so the console can prefill Auth, but it must
        carry the stored secret's NAME, never a real key."""
        import samvaad_config as SC
        for name in SC.PARAMS:
            text = SC.curl(name)
            self.assertIn(f"/api/agent/tool/{name}?", text)
            self.assertIn("caller={{caller_number}}", text)
            self.assertIn("shop_key={{shop_key}}", text)
            self.assertIn("secret={{agent_secret}}", text)
            self.assertIn("X-Agent-Secret: {{SECRET_KEY}}", text)
            self.assertNotIn(os.environ["SAMVAAD_WEBHOOK_SECRET"], text)

    def test_named_http_route_delivers_direct_arguments_for_every_tool(self):
        """All 25 console tools must reach handle with the model's own args.

        This is the transport regression that the in-memory function tests
        cannot catch: the model composed correct fields, but the old nested
        body delivered literal {{placeholders}} to production.
        """
        import samvaad_config as SC
        client = TestClient(main.app)
        received = []

        def capture(tool, caller, args, key=""):
            received.append((tool, caller, args, key))
            return {"ok": True, "tool": tool, "received": args}

        with patch.object(agent, "verify_secret", return_value=True), \
                patch.object(agent, "handle", side_effect=capture):
            for name, params in SC.PARAMS.items():
                payload = {field: f"value-for-{field}" for field in params}
                response = client.post(
                    f"/api/agent/tool/{name}",
                    params={"caller": "917006322772",
                            "shop_key": "browser-shop-key",
                            "secret": "test-agent-secret"},
                    json=payload)
                self.assertEqual(response.status_code, 200, name)
                self.assertEqual(response.json()["received"], payload, name)

        self.assertEqual([row[0] for row in received], list(SC.PARAMS))
        for (name, caller, args, key), params in zip(
                received, SC.PARAMS.values()):
            self.assertEqual(caller, "917006322772", name)
            self.assertEqual(key, "browser-shop-key", name)
            self.assertEqual(set(args), set(params), name)

    def test_bad_agent_secret_is_a_structured_refusal_not_transport_error(self):
        client = TestClient(main.app)
        with patch.object(agent, "verify_secret", return_value=False), \
                patch.object(agent, "handle") as handle:
            response = client.post(
                "/api/agent/tool/update_item",
                params={"caller": "917006322772",
                        "secret": "{{agent_secret}}"},
                json={"item": "UltraTech PPC Cement 50kg",
                      "selling_rate": 50})
        self.assertEqual(response.status_code, 200)
        out = response.json()
        self.assertFalse(out["ok"])
        self.assertFalse(out["authorised"])
        self.assertFalse(out["updated"])
        self.assertEqual(out["error"], "bad_agent_secret")
        self.assertEqual(
            json.loads(out["facts"])["error"], "bad_agent_secret")
        handle.assert_not_called()

    def test_named_route_debug_trace_shows_the_actual_http_arguments(self):
        client = TestClient(main.app)
        result = {
            "ok": True, "updated": True, "tool": "update_item",
            "authorised": True, "facts": "{}",
        }
        with patch.object(agent, "verify_secret", return_value=True), \
                patch.object(agent, "handle", return_value=result):
            response = client.post(
                "/api/agent/tool/update_item",
                params={"caller": "917006322772", "secret": "valid",
                        "debug": "1"},
                json={"item": "UltraTech PPC", "selling_rate": 50,
                      "sku_id": "{{sku_id}}"})
        self.assertEqual(response.status_code, 200)
        trace = response.json()["_trace"]
        self.assertEqual(
            trace["received_args"],
            {"item": "UltraTech PPC", "selling_rate": 50})
        self.assertTrue(trace["caller_present"])
        self.assertNotIn("secret", json.dumps(trace))
        self.assertEqual(
            json.loads(response.json()["facts"])["_trace"], trace)

    def test_every_reply_carries_the_whole_payload_as_facts(self):
        """The console templates named fields out of the reply, so one field
        must hold everything, or configuring 23 tools means naming every key
        of every one and losing whatever was forgotten."""
        shop = {"user_id": "u_1", "phone": "+917006322772", "shop_name": "S"}
        with patch.object(agent.auth, "all_users", return_value=[shop]), \
                patch.object(main, "bind_user"), \
                patch.dict(agent.TOOLS,
                           {"dues": (lambda r, u, a: {"count": 2, "speak": "do"},
                                     "")}):
            out = agent.handle("dues", "917006322772", {})
        facts = json.loads(out["facts"])
        self.assertEqual(facts["count"], 2)
        self.assertEqual(facts["speak"], "do")
        self.assertNotIn("facts", facts)

    def test_facts_stays_parseable_when_the_list_is_long(self):
        rows = [{"name": f"item {i}", "qty": i} for i in range(400)]
        text = agent._facts({"count": 400, "items": rows, "speak": "bahut"})
        self.assertLessEqual(len(text), agent.FACTS_LIMIT)
        parsed = json.loads(text)
        self.assertEqual(parsed["items_truncated_from"], 400)
        self.assertEqual(parsed["count"], 400)

    def test_manifest_describes_every_tool(self):
        names = {t["name"] for t in agent.manifest()}
        self.assertEqual(names, set(agent.TOOLS))
        self.assertTrue(all(t["description"] for t in agent.manifest()))


if __name__ == "__main__":
    unittest.main()
