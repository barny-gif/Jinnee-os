"""Product costs tab data in the ecom sync (2026-10-23): unit cost, order money fields, daily sales totals. Fake HTTP only.
Run: python3 -m unittest discover tests"""
import json, os, unittest
from unittest import mock
from test_ecom_sync import load, bill, Resp, ORDER, COD_ORDER, PRODUCT, ALL_SCOPES, TRAFFIC
from cbase import ConnectorError

NOW = 1792756800  # 2026-10-23 12:00 UTC = 14:00 Budapest
PII = ("x@y.hu", "Titkos", "+3630", "Fo utca", "csengo")
SALES_KEYS = {"day", "orders", "units", "revenue_gross", "shipping_paid", "discounts", "cod_orders"}
PRODUCT_KEYS = {"day", "variant_id", "units", "revenue_gross"}

def order(i, day="2026-10-20T10:00:00+02:00", **kw):
    o = json.loads(json.dumps(ORDER)); o.update(id=7000 + i, name=f"#{2000 + i}", created_at=day, **kw)
    for n, l in enumerate(o["line_items"]): l.setdefault("id", (7000 + i) * 10 + n)
    return o

def fake_shop(orders=(), products=(PRODUCT,), items=None, fail=None, page=None):
    """orders.json honours since_id (ascending ids) when given, like Shopify; page = page size override for paging tests."""
    calls = []
    def _get(path, params=None):
        calls.append((path, dict(params or {})))
        if fail and path.startswith(fail): raise ConnectorError("Shopify did not answer: Timeout")
        if path == "orders.json":
            rows = list(orders)
            if "since_id" in (params or {}):
                rows = sorted((o for o in rows if o["id"] > int(params["since_id"])), key=lambda o: o["id"])
            return {"orders": rows[: (page or params.get("limit", 250))]}
        if path == "inventory_items.json":
            ids = params["ids"].split(",")
            return {"inventory_items": [{"id": int(i), "cost": c} for i, c in (items or {}).items() if i in ids]}
        return {"products": list(products)}
    s = mock.Mock(_get=mock.Mock(side_effect=_get), scopes=mock.Mock(return_value=dict(ALL_SCOPES)),
                  sessions=mock.Mock(return_value=list(TRAFFIC)), fulfill=mock.Mock())
    s.calls = calls
    return s

class MoneyFields(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_automatic_discount_from_allocations_order_1031(self):
        o = order(1, total_price="14880.00", total_discounts="0.00",
                  line_items=[{"id": 1, "title": "Játék", "variant_id": 77, "quantity": 1, "price": "14480.00", "total_discount": "0.00",
                               "discount_allocations": [{"amount": "1490.00"}]}],
                  shipping_lines=[{"title": "GLS", "price": "1890.00", "discount_allocations": []}])
        self.assertEqual(self.m.discounts(o), 1490.0)
        self.assertEqual(self.m.shipping_paid(o), 1890.0)
    def test_free_shipping_discount_lowers_shipping_paid(self):
        o = order(2, shipping_lines=[{"title": "GLS", "price": "1890.00", "discount_allocations": [{"amount": "1890.00"}]}])
        self.assertEqual(self.m.shipping_paid(o), 0.0)
        self.assertEqual(self.m.discounts(o), 1890.0)
    def test_old_style_line_discount_and_order_total_fallback(self):
        self.assertEqual(self.m.discounts(order(3, line_items=[{"id": 1, "quantity": 1, "price": "100", "total_discount": "10"}])), 10.0)
        self.assertEqual(self.m.discounts(order(4, total_discounts="500.00")), 500.0)
    def test_payment_type(self):
        pt = self.m.payment_type
        self.assertEqual(pt(COD_ORDER), "cod")
        self.assertEqual(pt(ORDER), "card")
        self.assertEqual(pt({**ORDER, "financial_status": "pending", "payment_gateway_names": ["Bank Deposit"]}), "bank_transfer")
        self.assertEqual(pt({**ORDER, "financial_status": "pending", "payment_gateway_names": []}), "cod")  # team rule
        self.assertIsNone(pt({**ORDER, "payment_gateway_names": []}))
    def test_orders_carry_the_new_fields_within_the_site_contract(self):
        out = self.m.build(shop=fake_shop(orders=(ORDER, COD_ORDER)), bill=bill(), now=NOW)
        a, b = out["orders"]
        self.assertEqual((a["shipping_paid"], a["discounts"], a["payment_type"]), (1890.0, 0.0, "card"))
        self.assertEqual(b["payment_type"], "cod")
        for o in out["orders"]:
            self.assertIn(o["payment_type"], ("card", "transfer", "bank_transfer", "cod", None))
            self.assertGreaterEqual(o["shipping_paid"], 0); self.assertGreaterEqual(o["discounts"], 0)
    def test_order_fetch_asks_no_new_customer_field(self):
        s = fake_shop(orders=(ORDER,)); self.m.build(shop=s, bill=bill(), now=NOW)
        fields = s.calls[0][1]["fields"].split(",")
        for f in ("tags", "refunds", "total_discounts"): self.assertIn(f, fields)
        self.assertNotIn("customer", fields); self.assertNotIn("note", fields)

class Exclusions(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_reasons(self):
        nr = self.m.not_revenue
        self.assertIsNone(nr(ORDER))
        self.assertEqual(nr({**ORDER, "cancelled_at": "2026-10-20T10:00:00Z"}), "cancelled")
        self.assertEqual(nr({**ORDER, "test": True}), "test")
        self.assertEqual(nr({**ORDER, "financial_status": "refunded"}), "refunded")
        self.assertEqual(nr({**ORDER, "total_price": "0.00"}), "zero")
        for tag in ("Barter", "influencer", "Teszt", "Ajándék", "VIP, ajandek"):
            self.assertEqual(nr({**ORDER, "tags": tag}), "tagged", tag)
        self.assertIsNone(nr({**ORDER, "tags": "VIP, visszatérő"}))
        self.assertIsNone(nr({**ORDER, "financial_status": "partially_refunded"}))

class Aggregate(unittest.TestCase):
    def setUp(self):
        self.m = load(); self.env = mock.patch.dict(os.environ, {"JINNEE_TZ": "Europe/Budapest"}); self.env.start()
    def tearDown(self): self.env.stop()
    def agg(self, orders, first="2026-10-19", last="2026-10-21"):
        import datetime as dt
        return self.m.aggregate(orders, dt.date.fromisoformat(first), dt.date.fromisoformat(last))
    def test_totals_zero_days_and_shop_day(self):
        late = order(2, day="2026-10-19T22:30:00Z")  # 00:30 on 10-20 in Budapest
        cod = {**order(3), **{k: COD_ORDER[k] for k in ("financial_status", "payment_gateway_names")}}
        sales, prods, ex = self.agg([order(1), late, cod])
        by = {r["day"]: r for r in sales}
        self.assertEqual(sorted(by), ["2026-10-19", "2026-10-20", "2026-10-21"])
        self.assertEqual(by["2026-10-19"]["orders"], 0); self.assertEqual(by["2026-10-21"], {**by["2026-10-21"], "orders": 0, "revenue_gross": 0.0})
        d = by["2026-10-20"]
        self.assertEqual((d["orders"], d["units"], d["revenue_gross"], d["shipping_paid"], d["cod_orders"]), (3, 6, 3 * 12990.0, 3 * 1890.0, 1))
        self.assertEqual(prods, [{"day": "2026-10-20", "variant_id": "77", "units": 6, "revenue_gross": 6 * 5550.0}])
        self.assertEqual(ex, {})
    def test_excluded_orders_leave_zero_rows_and_are_counted(self):
        bad = [order(1, cancelled_at="2026-10-20T12:00:00Z"), order(2, test=True), order(3, financial_status="refunded"),
               order(4, total_price="0.00"), order(5, tags="Influencer")]
        sales, prods, ex = self.agg(bad)
        self.assertEqual(sum(r["orders"] for r in sales), 0); self.assertEqual(sum(r["revenue_gross"] for r in sales), 0)
        self.assertEqual(prods, [{"day": "2026-10-20", "variant_id": "77", "units": 0, "revenue_gross": 0.0}])  # clears an old count
        self.assertEqual(ex, {"cancelled": 1, "test": 1, "refunded": 1, "zero": 1, "tagged": 1})
    def test_partial_refund_takes_money_and_units_off(self):
        o = order(1, financial_status="partially_refunded")
        lid = o["line_items"][0]["id"]
        o["refunds"] = [{"transactions": [{"kind": "refund", "status": "success", "amount": "5550.00"},
                                          {"kind": "refund", "status": "failure", "amount": "999.00"}],
                         "refund_line_items": [{"line_item_id": lid, "quantity": 1}]}]
        sales, prods, _ = self.agg([o])
        d = next(r for r in sales if r["day"] == "2026-10-20")
        self.assertEqual((d["orders"], d["units"], d["revenue_gross"]), (1, 1, 12990.0 - 5550.0))
        self.assertEqual((prods[0]["units"], prods[0]["revenue_gross"]), (1, 5550.0))
    def test_refund_without_transaction_uses_the_discounted_line_price(self):
        o = order(1, financial_status="partially_refunded")
        o["line_items"][0]["discount_allocations"] = [{"amount": "1100.00"}]  # 2 x 5550 - 1100 = 10000 -> 5000 per unit
        o["refunds"] = [{"transactions": [], "refund_line_items": [{"line_item_id": o["line_items"][0]["id"], "quantity": 1}]}]
        sales, prods, _ = self.agg([o])
        d = next(r for r in sales if r["day"] == "2026-10-20")
        self.assertEqual((d["revenue_gross"], d["discounts"]), (12990.0 - 5000.0, 1100.0))
        self.assertEqual(prods[0]["revenue_gross"], 5000.0)
    def test_amounts_never_negative(self):
        o = order(1, financial_status="partially_refunded", refunds=[{"transactions": [{"kind": "refund", "status": "success", "amount": "99999"}]}])
        sales, _, _ = self.agg([o])
        self.assertTrue(all(r[k] >= 0 for r in sales for k in SALES_KEYS - {"day"}))

class History(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_pages_by_since_id_and_asks_no_customer_field(self):
        many = [order(i, day="2026-10-20T10:00:00+02:00") for i in range(1, 8)]
        s = fake_shop(orders=many, page=3)
        self.m.PAGE = 3
        import datetime as dt
        got = self.m._history_orders(s, dt.date(2026, 5, 1))
        self.assertEqual(sorted(o["id"] for o in got), [o["id"] for o in many])
        for path, p in s.calls:
            fields = p["fields"].split(",")
            for bad in ("email", "phone", "shipping_address", "billing_address", "customer", "note", "name"): self.assertNotIn(bad, fields)
    def test_cycle_sends_last_days_only_and_no_history_call(self):
        s = fake_shop(orders=(order(1),))
        out = self.m.build(shop=s, bill=bill(), now=NOW)  # dry run, history auto
        self.assertEqual(len(out["sales_daily"]), 7); self.assertFalse(out["sales_window"]["full"])
        self.assertFalse(any("since_id" in p for _, p in s.calls))
    def test_full_history_once_a_day_and_marked_only_after_the_site_took_it(self):
        s = fake_shop(orders=(order(1),))
        with mock.patch.dict(os.environ, {"ECOM_SITE_URL": "https://site.example", "ECOM_INGEST_SECRET": "x", "ECOM_HISTORY_DAYS": "90"}), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"ok": True}, 500)):
            with self.assertRaises(ConnectorError): self.m.push("yes", shop=s, bill=bill())
        self.assertFalse(self.m.HISTORY.exists())
        self.m._hist_try["at"] = 0.0
        with mock.patch.dict(os.environ, {"ECOM_SITE_URL": "https://site.example", "ECOM_INGEST_SECRET": "x", "ECOM_HISTORY_DAYS": "90"}), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"ok": True, "new_order_ids": []})) as post:
            out = self.m.push("yes", shop=s, bill=bill())
            sent = post.call_args.kwargs["json"]
            self.assertTrue(out["sales"]["full_history"]); self.assertEqual(len(sent["sales_daily"]), 90)
            self.assertTrue(self.m.HISTORY.exists())
            self.m._hist_try["at"] = 0.0
            again = self.m.push("yes", shop=s, bill=bill())
            self.assertFalse(again["sales"]["full_history"])
    def test_dry_run_with_history_yes_prints_totals_only(self):
        s = fake_shop(orders=(ORDER, COD_ORDER))
        out = self.m.push("no", shop=s, bill=bill(), history="yes")
        self.assertTrue(out["dry_run"]); self.assertTrue(out["sales"]["full_history"])
        self.assertFalse(self.m.HISTORY.exists())
        text = json.dumps(out["sales"], ensure_ascii=False)
        for secret in PII + ("#5024", "#5025"): self.assertNotIn(secret, text)
    def test_history_failure_falls_back_to_last_days(self):
        s = fake_shop(orders=(order(1),))
        real = s._get.side_effect
        def _get(path, params=None):
            if "since_id" in (params or {}): raise ConnectorError("Shopify did not answer: Timeout")
            return real(path, params)
        s._get.side_effect = _get
        out = self.m.build(shop=s, bill=bill(), now=NOW, history="yes")
        self.assertFalse(out["sales_window"]["full"]); self.assertEqual(len(out["sales_daily"]), 7)
        self.assertTrue(any("Shopify did not answer" in e for e in out["errors"]))
    def test_cut_order_list_sends_no_totals(self):
        s = fake_shop(orders=(order(1),)); self.m.PAGE = 1
        self.assertIsNone(self.m.build(shop=s, bill=bill(), now=NOW)["sales_daily"])

class NoPersonalData(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_aggregate_rows_have_only_the_contract_keys(self):
        out = self.m.build(shop=fake_shop(orders=(ORDER, COD_ORDER)), bill=bill(), now=NOW, history="yes")
        for r in out["sales_daily"]: self.assertEqual(set(r), SALES_KEYS)
        for r in out["product_sales_daily"]: self.assertEqual(set(r), PRODUCT_KEYS)
        text = json.dumps([out["sales_daily"], out["product_sales_daily"], out["sales_window"]], ensure_ascii=False)
        for secret in PII + ("#5024", "#5025"): self.assertNotIn(secret, text)

class UnitCost(unittest.TestCase):
    def setUp(self): self.m = load()
    PROD = {"id": 9, "title": "Farmer", "status": "active", "variants": [
        {"id": 77, "title": "Default Title", "sku": "FK1", "price": "5550.00", "inventory_quantity": 4, "inventory_item_id": 501},
        {"id": 78, "title": "Nagy", "sku": "FK2", "price": "6550.00", "inventory_quantity": 1, "inventory_item_id": 502},
        {"id": 79, "title": "Új", "sku": "", "price": "7550.00", "inventory_quantity": 0, "inventory_item_id": 503}]}
    def test_cost_per_item_and_sku(self):
        s = fake_shop(products=(self.PROD,), items={"501": "2100.50", "502": None})
        a, b, c = self.m.build(shop=s, bill=bill(), now=NOW)["products"]
        self.assertEqual((a["unit_cost"], a["sku"]), (2100.5, "FK1"))
        self.assertIsNone(b["unit_cost"])          # not filled in Shopify: null
        self.assertNotIn("unit_cost", c)            # not returned: the site keeps what it has
        self.assertIsNone(c["sku"])
        inv = [p for path, p in s.calls if path == "inventory_items.json"]
        self.assertEqual(inv, [{"ids": "501,502,503", "limit": 100}])
    def test_asked_hourly(self):
        s = fake_shop(products=(self.PROD,), items={"501": "1"})
        self.m.build(shop=s, bill=bill(), now=NOW); self.m.build(shop=s, bill=bill(), now=NOW + 60)
        self.assertEqual(sum(1 for p, _ in s.calls if p == "inventory_items.json"), 1)
        self.m.build(shop=s, bill=bill(), now=NOW + 3600)
        self.assertEqual(sum(1 for p, _ in s.calls if p == "inventory_items.json"), 2)
    def test_failure_keeps_products_without_unit_cost(self):
        out = self.m.build(shop=fake_shop(products=(self.PROD,), fail="inventory_items"), bill=bill(), now=NOW)
        self.assertEqual(len(out["products"]), 3)
        self.assertTrue(all("unit_cost" not in p for p in out["products"]))
        self.assertFalse(next(a for a in out["automations"] if a["key"] == "shopify_unit_cost")["ok"])
    def test_negative_or_bad_cost_is_null(self):
        self.assertIsNone(self.m._cost("-5")); self.assertIsNone(self.m._cost("abc")); self.assertEqual(self.m._cost("12"), 12.0)

if __name__ == "__main__": unittest.main()
