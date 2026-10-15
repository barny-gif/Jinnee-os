"""Shopify ops scopes / sessions / fulfill, with fake HTTP only: no test reaches Shopify. Run: python3 -m unittest discover tests"""
import importlib.util, os, pathlib, sys, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
from cbase import ConnectorError

def load():
    s = importlib.util.spec_from_file_location("t_shopify_ops", ROOT / "connectors" / "shopify" / "ops.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

class Resp:
    def __init__(self, data=None, code=200): self.data, self.status_code, self.ok = data or {}, code, code < 400
    def json(self): return self.data

LIVE = ["read_all_orders", "read_customer_events", "read_customers", "read_discounts", "read_draft_orders", "read_inventory", "read_locations",
        "read_marketing_events", "read_merchant_managed_fulfillment_orders", "read_orders", "read_pixels", "read_products", "read_reports",
        "read_returns", "read_shipping", "write_merchant_managed_fulfillment_orders", "write_orders", "write_pixels"]  # the token on 2026-10-15

class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SHOPIFY_STORE": "demo", "SHOPIFY_ADMIN_TOKEN": "t"}); self.env.start()
        self.m = load()
    def tearDown(self): self.env.stop()

class Scopes(Base):
    def test_live_list_is_complete_without_write_products(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp({"access_scopes": [{"handle": h} for h in LIVE]})) as get:
            out = self.m.scopes()
        self.assertEqual(get.call_args.args[0], "https://demo.myshopify.com/admin/oauth/access_scopes.json")
        self.assertTrue(out["ok"]); self.assertEqual(out["missing"], []); self.assertNotIn("write_products", out["required"])
    def test_missing_are_listed_in_order(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp({"access_scopes": [{"handle": "read_orders"}, {"handle": "write_products"}]})):
            out = self.m.scopes()
        self.assertFalse(out["ok"]); self.assertEqual(out["missing"][0], "read_products"); self.assertIn("read_reports", out["missing"])
        self.assertIn("write_products", out["scopes"])
    def test_refused_token(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(code=401)):
            with self.assertRaisesRegex(ConnectorError, "401"): self.m.scopes()

class Sessions(Base):
    COLS = [{"name": "day"}, {"name": "sessions"}, {"name": "online_store_visitors"}]
    def test_rows_as_lists_or_dicts_sorted(self):
        for rows in ([["2026-10-13", "9", "8"], ["2026-10-12", "8", "7"]],
                     [{"day": "2026-10-13T00:00:00Z", "sessions": "9", "online_store_visitors": "8"}, {"day": "2026-10-12", "sessions": 8, "online_store_visitors": 7}]):
            with mock.patch.object(self.m.requests, "post", return_value=Resp({"data": {"shopifyqlQuery": {"tableData": {"columns": self.COLS, "rows": rows}, "parseErrors": []}}})) as post:
                out = self.m.sessions("7")
            self.assertEqual(out, [{"day": "2026-10-12", "sessions": 8, "visitors": 7}, {"day": "2026-10-13", "sessions": 9, "visitors": 8}])
            q = post.call_args.kwargs["json"]["variables"]["q"]
            self.assertIn("FROM sessions SHOW sessions, online_store_visitors TIMESERIES day SINCE -7d", q)
            for word in ("customer", "email", "name", "GROUP BY"): self.assertNotIn(word, q)
            self.assertTrue(post.call_args.args[0].endswith("/graphql.json"))
    def test_missing_scope_is_named(self):
        body = {"errors": [{"message": "Access denied for shopifyqlQuery field.", "extensions": {"code": "ACCESS_DENIED"}}]}
        with mock.patch.object(self.m.requests, "post", return_value=Resp(body)):
            with self.assertRaisesRegex(ConnectorError, "read_reports"): self.m.sessions("7")
    def test_parse_error_is_named(self):
        with mock.patch.object(self.m.requests, "post", return_value=Resp({"data": {"shopifyqlQuery": {"tableData": None, "parseErrors": ["bad"]}}})):
            with self.assertRaisesRegex(ConnectorError, "could not read"): self.m.sessions("7")

FOS = {"fulfillment_orders": [{"id": 11, "status": "open", "supported_actions": ["create_fulfillment", "hold"]},
                              {"id": 12, "status": "closed", "supported_actions": []},
                              {"id": 13, "status": "in_progress", "supported_actions": ["create_fulfillment"]}]}

class Fulfill(Base):
    def test_dry_run_reads_only(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(FOS)) as get, mock.patch.object(self.m.requests, "post") as post:
            out = self.m.fulfill("6001", "3424008826")
        post.assert_not_called()
        self.assertTrue(get.call_args.args[0].endswith("/orders/6001/fulfillment_orders.json"))
        self.assertEqual((out["dry_run"], out["fulfillment_orders"], out["notify_customer"]), (True, 2, False))
    def test_confirm_sends_open_orders_with_gls_tracking_and_no_customer_mail(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(FOS)), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"fulfillment": {"id": 99, "status": "success"}}, 201)) as post:
            out = self.m.fulfill("6001", "3424008826", confirm="yes")
        self.assertTrue(post.call_args.args[0].endswith("/admin/api/2026-07/fulfillments.json"))
        self.assertEqual(post.call_args.kwargs["json"], {"fulfillment": {
            "line_items_by_fulfillment_order": [{"fulfillment_order_id": 11}, {"fulfillment_order_id": 13}],
            "tracking_info": {"number": "3424008826", "company": "GLS"}, "notify_customer": False}})
        self.assertEqual((out["created"], out["fulfillment_id"], out["notify_customer"]), (True, "99", False))
    def test_nothing_open_means_already_and_no_post(self):
        closed = {"fulfillment_orders": [{"id": 12, "status": "closed", "supported_actions": []}]}
        with mock.patch.object(self.m.requests, "get", return_value=Resp(closed)), mock.patch.object(self.m.requests, "post") as post:
            out = self.m.fulfill("6001", "3424008826", confirm="yes")
        post.assert_not_called(); self.assertTrue(out["already"]); self.assertFalse(out["created"])
    def test_no_tracking_no_call(self):
        with mock.patch.object(self.m.requests, "get") as get:
            with self.assertRaisesRegex(ConnectorError, "tracking"): self.m.fulfill("6001", "")
            with self.assertRaisesRegex(ConnectorError, "digits"): self.m.fulfill("#1025", "1")
        get.assert_not_called()
    def test_422_shows_shopify_errors(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(FOS)), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"errors": {"base": ["Fulfillment order is not open"]}}, 422)):
            with self.assertRaisesRegex(ConnectorError, r"\[fulfillments\].*422.*not open"): self.m.fulfill("6001", "1", confirm="yes")

if __name__ == "__main__": unittest.main()
