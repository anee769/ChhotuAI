"""Exercise every voice-agent tool against a live deployment.

test_agent.py proves the tool logic against an in-memory shop. This proves the
deployed thing: real HTTP, real Postgres, real matcher, real multi-tenancy. The
two catch different faults, and most of today's bugs were the second kind, so
both are worth having.

Point it at a THROWAWAY shop. It records sales and payments, which cannot be
undone.

    python3 backend/scenarios_agent.py +919000000001
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("CHHOTU_URL", "https://chhotuai.vercel.app")
SECRET = os.environ.get("SAMVAAD_WEBHOOK_SECRET", "")
CALLER = sys.argv[1] if len(sys.argv) > 1 else "+919000000001"

PASS = FAIL = 0
failures: list[str] = []


def call(tool: str, **args) -> dict:
    body = json.dumps({"tool": tool, "caller": CALLER, "secret": SECRET,
                       "args": args}).encode()
    req = urllib.request.Request(f"{BASE}/api/agent/tool", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def check(label: str, condition, detail="") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        failures.append(label)
        print(f"  FAIL {label}  {detail}")


def section(name: str) -> None:
    print(f"\n=== {name}")


def main() -> None:
    section("empty shop: reads must not invent anything")
    p = call("shop_profile")
    check("shop_profile returns the shop", p.get("ok") and p.get("shop"), p)
    check("shop_profile carries facts", "facts" in p)
    check("inventory starts empty", call("list_inventory")["count"] == 0)
    check("no customers yet", call("list_customers")["count"] == 0)
    check("no dues yet", call("dues")["count"] == 0)
    check("summary of an empty day is zero, not missing",
          call("business_summary", period="day")["sale"] == 0)
    check("stock value of nothing is zero",
          call("stock_value")["at_cost"] == 0)
    miss = call("check_stock", item="laptop")
    check("off-trade miss names the trade",
          miss.get("stocks_this_kind") is False and "dukaan" in miss["speak"])
    check("no item named asks rather than answers",
          call("check_stock", item="")["needs"]["field"] == "item")

    section("add_item")
    check("cost price is required",
          call("add_item", name="Test Cement")["needs"]["field"] == "cost_price")
    a = call("add_item", name="Test Cement 50kg", cost_price=380,
             selling_rate=420, unit="bori", brand="TestCo")
    check("adds with cost and price", a.get("added"), a)
    check("asks for the opening count next",
          a.get("next_step", {}).get("tool") == "stock_take")
    check("duplicate is refused",
          call("add_item", name="Test Cement 50kg", cost_price=380)
          .get("added") is False)
    d = call("item_details", item="Test Cement")
    check("cost survived the round trip", d.get("landed_cost") == 380, d)
    check("price survived the round trip", d.get("selling_rate") == 420, d)
    check("new item is uncounted until counted",
          call("check_stock", item="Test Cement")["counted"] is False)

    section("stock_take and record_purchase")
    check("stock take sets the count",
          call("stock_take", item="Test Cement", qty=100, unit="bori")
          .get("recorded"))
    check("stock reads back", call("check_stock", item="Test Cement")["qty"] == 100)
    check("purchase adds to it",
          call("record_purchase", item="Test Cement", qty=50, unit="bori",
               rate=375).get("recorded"))
    check("stock is 150 after delivery",
          call("check_stock", item="Test Cement")["qty"] == 150)

    section("selling")
    check("unknown item asks before recording",
          call("record_sale", item="Asian Paints Apcolite", qty=2)
          .get("needs", {}).get("field") == "confirm_add")
    s = call("record_sale", item="Test Cement", qty=10, unit="bori", rate=420,
             payment="cash", request_id="sc-1")
    check("cash sale records", s.get("recorded"), s)
    check("cash sale totals correctly", s.get("total") == 4200, s)
    check("stock fell by ten", call("check_stock", item="Test Cement")["qty"] == 140)
    check("retry with the same id does not double",
          call("record_sale", item="Test Cement", qty=10, unit="bori", rate=420,
               payment="cash", request_id="sc-1").get("duplicate"))
    check("stock still 140 after the retry",
          call("check_stock", item="Test Cement")["qty"] == 140)
    check("credit with no customer is refused",
          call("record_sale", item="Test Cement", qty=5, payment="credit")
          .get("needs", {}).get("field") == "customer")

    section("credit and customers")
    c = call("record_sale", item="Test Cement", qty=20, unit="bori", rate=420,
             payment="credit", customer="Suresh Patil",
             customer_phone="9812345678", payment_deadline="2026-08-20",
             request_id="sc-2")
    check("credit sale records", c.get("recorded"), c)
    check("credit opens a receivable", (c.get("receivable") or {}).get("amount") == 8400, c)
    acc = call("customer_account", name="Suresh")
    check("customer is found by partial name", acc.get("found"), acc)
    check("outstanding matches the sale", acc.get("outstanding") == 8400, acc)
    check("due appears in the dues list",
          any(r["name"] == "Suresh Patil" for r in call("dues", days_before=60)["dues"]))
    check("overpayment is refused",
          call("record_payment", customer="Suresh", amount=99999)
          .get("recorded") is False)
    pay = call("record_payment", customer="Suresh", amount=3400, request_id="sc-3")
    check("payment records", pay.get("recorded"), pay)
    check("balance drops to 5000", pay.get("outstanding") == 5000, pay)
    check("repeat payment id is ignored",
          call("record_payment", customer="Suresh", amount=3400,
               request_id="sc-3").get("duplicate"))
    check("payment history is visible",
          len(call("customer_account", name="Suresh")["recent_payments"]) == 1)

    section("reporting")
    b = call("business_summary", period="day")
    check("day sale is 4200 cash plus 8400 credit", b["sale"] == 12600, b)
    check("cash and credit split correctly",
          b["cash"] == 4200 and b["credit"] == 8400, b)
    check("week includes today", call("business_summary", period="week")["sale"] == 12600)
    check("explicit range works",
          call("business_summary", start="2026-01-01", end="2026-01-02")["sale"] == 0)
    t = call("top_items", days=30)
    check("top items lists the cement", t["items"] and t["items"][0]["qty_sold"] == 30, t)
    check("top items carries margin", "margin" in (t["items"] or [{}])[0])
    check("margin ordering is accepted", call("top_items", order="margin")["ok"])
    check("recent activity lists events", call("recent_activity", limit=5)["count"] > 0)
    check("stock value is now positive", call("stock_value")["at_cost"] > 0)
    q = call("price_quote", item="Test Cement", qty=4)
    check("quote uses the shop's own price", q["lines"][0]["rate"] == 420, q)
    check("quote adds GST", q["gst"] > 0 and q["total"] > q["subtotal"])
    check("search finds by brand", call("search_items", query="TestCo")["count"] >= 1)

    section("editing and removing")
    check("update needs something to change",
          call("update_item", item="Test Cement").get("updated") is False)
    check("price can be corrected",
          call("update_item", item="Test Cement", selling_rate=450).get("updated"))
    check("corrected price is used",
          call("price_quote", item="Test Cement", qty=1)["lines"][0]["rate"] == 450)
    check("an item with history cannot be removed",
          call("remove_item", item="Test Cement").get("removed") is False)
    call("add_item", name="Mistake Item", cost_price=10, unit="piece")
    check("an unused item can be removed",
          call("remove_item", item="Mistake Item").get("removed"))

    section("isolation and refusals")
    other = json.dumps({"tool": "list_inventory", "caller": "+919999999999",
                        "secret": SECRET, "args": {}}).encode()
    req = urllib.request.Request(f"{BASE}/api/agent/tool", data=other,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        unknown = json.loads(r.read())
    check("an unregistered caller gets nothing", unknown.get("authorised") is False)
    check("a refusal says failure in every field",
          not any(unknown.get(k) for k in ("ok", "recorded", "added", "sent")))
    check("an unknown tool does not list the catalogue",
          "known_tools" not in call("no_such_tool"))

    print(f"\n{PASS} passed, {FAIL} failed")
    if failures:
        print("failed:", *failures, sep="\n  ")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
