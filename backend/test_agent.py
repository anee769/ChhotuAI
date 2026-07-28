"""Every voice-agent tool, exercised against an in-memory shop.

These run without a database on purpose. The point of the tool layer is that
the agent never has to guess, so what is asserted here is mostly refusals:
ambiguous item -> a question, credit with no customer -> a question, payment
larger than the balance -> refused. A tool that quietly does the wrong thing is
far worse over a phone line than one that asks.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_search_falls_back_to_fuzzy(self):
        self.assertGreater(self.call("search_items", query="tiscon")["count"], 0)

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

    def test_new_item_requires_a_cost_price(self):
        out = self.call("add_item", name="Asian Paints Apcolite 20L")
        self.assertFalse(out["added"])
        self.assertEqual(out["needs"]["field"], "cost_price")

    def test_new_item_is_added_and_then_findable(self):
        out = self.call("add_item", name="Asian Paints Apcolite 20L",
                        cost_price=3200, selling_rate=3600, unit="bucket")
        self.assertTrue(out["added"])
        self.assertTrue(self.call("check_stock", item="Apcolite")["found"])

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

    def test_every_tool_is_documented_for_the_console(self):
        """A tool with no worked example ships as an empty args box, which the
        agent then fills in by guesswork."""
        import samvaad_config
        self.assertEqual(set(samvaad_config.EXAMPLES), set(agent.TOOLS))
        self.assertEqual(set(samvaad_config.PARAMS), set(agent.TOOLS))

    def test_generated_bodies_are_templates_not_fixed_values(self):
        """A literal in the body freezes the tool: check_stock would look up
        the same item forever."""
        import samvaad_config as SC
        for name, params in SC.PARAMS.items():
            args = json.loads(SC.body(name))["args"]
            self.assertEqual(set(args), set(params), name)
            for k, v in args.items():
                self.assertEqual(v, "{{%s}}" % k, name)

    def test_generated_curls_carry_no_secret(self):
        import samvaad_config as SC
        for name in SC.PARAMS:
            self.assertNotIn("X-Agent-Secret", SC.curl(name))

    def test_manifest_describes_every_tool(self):
        names = {t["name"] for t in agent.manifest()}
        self.assertEqual(names, set(agent.TOOLS))
        self.assertTrue(all(t["description"] for t in agent.manifest()))


if __name__ == "__main__":
    unittest.main()
