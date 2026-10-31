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
            out = self.m.fulfill("6001", "9000000003")
        post.assert_not_called()
        self.assertTrue(get.call_args.args[0].endswith("/orders/6001/fulfillment_orders.json"))
        self.assertEqual((out["dry_run"], out["fulfillment_orders"], out["notify_customer"]), (True, 2, False))
    def test_confirm_sends_open_orders_with_gls_tracking_and_no_customer_mail(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(FOS)), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"fulfillment": {"id": 99, "status": "success"}}, 201)) as post:
            out = self.m.fulfill("6001", "9000000003", confirm="yes")
        self.assertTrue(post.call_args.args[0].endswith("/admin/api/2026-07/fulfillments.json"))
        self.assertEqual(post.call_args.kwargs["json"], {"fulfillment": {
            "line_items_by_fulfillment_order": [{"fulfillment_order_id": 11}, {"fulfillment_order_id": 13}],
            "tracking_info": {"number": "9000000003", "company": "GLS"}, "notify_customer": False}})
        self.assertEqual((out["created"], out["fulfillment_id"], out["notify_customer"]), (True, "99", False))
    def test_nothing_open_means_already_and_no_post(self):
        closed = {"fulfillment_orders": [{"id": 12, "status": "closed", "supported_actions": []}]}
        with mock.patch.object(self.m.requests, "get", return_value=Resp(closed)), mock.patch.object(self.m.requests, "post") as post:
            out = self.m.fulfill("6001", "9000000003", confirm="yes")
        post.assert_not_called(); self.assertTrue(out["already"]); self.assertFalse(out["created"])
    def test_no_tracking_no_call(self):
        with mock.patch.object(self.m.requests, "get") as get:
            with self.assertRaisesRegex(ConnectorError, "tracking"): self.m.fulfill("6001", "")
            with self.assertRaisesRegex(ConnectorError, "digits"): self.m.fulfill("#5025", "1")
        get.assert_not_called()
    def test_422_shows_shopify_errors(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(FOS)), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"errors": {"base": ["Fulfillment order is not open"]}}, 422)):
            with self.assertRaisesRegex(ConnectorError, r"\[fulfillments\].*422.*not open"): self.m.fulfill("6001", "1", confirm="yes")

# Order 1036 (2026-10-28): the street name itself has a number ("48as utca" shape), the house number was missing in Shopify and was
# only added on the GLS label by hand, so the check kept saying "label: házszám" after the parcel was already uploaded.
SHIP = {"first_name": "Teszt", "last_name": "Elek", "address1": "Negyvennyolcas 48as utca", "address2": "", "city": "Budapest",
        "zip": "1111", "country_code": "HU", "phone": "+36301234567", "province_code": None}
def order(**kw):
    o = {"id": 7001, "name": "#5036", "cancelled_at": None, "financial_status": "pending", "total_price": "21650.00", "currency": "HUF",
         "payment_gateway_names": ["Cash on Delivery (COD)"], "shipping_address": dict(SHIP)}
    o.update(kw); return {"order": o}

class HouseNumber(Base):
    def test_dry_run_reads_only_and_never_prints_the_address(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(order())), mock.patch.object(self.m.requests, "post") as post:
            out = self.m.set_house_number("7001", "12/B")
        post.assert_not_called()
        self.assertEqual((out["dry_run"], out["order"], out["house_number"], out["billing_changed"]), (True, "#5036", "12/B", False))
        self.assertNotIn("utca", str(out)); self.assertNotIn("Elek", str(out))
    def test_confirm_sends_the_whole_address_with_the_number_appended(self):
        after = order(shipping_address={**SHIP, "address1": "Negyvennyolcas 48as utca 12/B"})
        with mock.patch.object(self.m.requests, "get", side_effect=[Resp(order()), Resp(after)]), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"data": {"orderUpdate": {"order": {"id": "x"}, "userErrors": []}}})) as post:
            out = self.m.set_house_number("7001", "12/B", confirm="yes")
        self.assertTrue(post.call_args.args[0].endswith("/admin/api/2026-07/graphql.json"))
        inp = post.call_args.kwargs["json"]["variables"]["input"]
        self.assertEqual(inp["id"], "gid://shopify/Order/7001")
        self.assertEqual(inp["shippingAddress"], {"firstName": "Teszt", "lastName": "Elek", "address1": "Negyvennyolcas 48as utca 12/B",
                                                  "city": "Budapest", "zip": "1111", "countryCode": "HU", "phone": "+36301234567"})
        self.assertTrue(out["written"])
    def test_already_has_a_number_writes_nothing(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(order(shipping_address={**SHIP, "address1": "Fő u. 3"}))), \
             mock.patch.object(self.m.requests, "post") as post:
            out = self.m.set_house_number("7001", "12", confirm="yes")
        post.assert_not_called(); self.assertTrue(out["already"]); self.assertFalse(out["written"])
    def test_cancelled_or_bad_number_writes_nothing(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(order(cancelled_at="2026-10-30T10:00:00Z"))), \
             mock.patch.object(self.m.requests, "post") as post:
            with self.assertRaisesRegex(ConnectorError, "cancelled"): self.m.set_house_number("7001", "12", confirm="yes")
            for bad in ("", "B", "utca 12", "1" * 21):
                with self.assertRaisesRegex(ConnectorError, "house number only"): self.m.set_house_number("7001", bad, confirm="yes")
        post.assert_not_called()
    def test_shopify_user_error_is_named(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(order())), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"data": {"orderUpdate": {"order": None, "userErrors": [{"field": ["zip"], "message": "Zip is invalid"}]}}})):
            with self.assertRaisesRegex(ConnectorError, r"\[house number\].*Zip is invalid"): self.m.set_house_number("7001", "12", confirm="yes")
    def test_read_back_without_the_number_is_an_error(self):
        with mock.patch.object(self.m.requests, "get", side_effect=[Resp(order()), Resp(order())]), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"data": {"orderUpdate": {"order": {"id": "x"}, "userErrors": []}}})):
            with self.assertRaisesRegex(ConnectorError, "reading it back"): self.m.set_house_number("7001", "12", confirm="yes")

class MarkPaid(Base):
    def test_dry_run_reads_only(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(order())), mock.patch.object(self.m.requests, "post") as post:
            out = self.m.mark_paid("7001")
        post.assert_not_called(); self.assertEqual((out["dry_run"], out["total"], out["status"]), (True, "21650.00", "pending"))
    def test_confirm_marks_paid(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(order())), \
             mock.patch.object(self.m.requests, "post", return_value=Resp({"data": {"orderMarkAsPaid": {"order": {"id": "x", "displayFinancialStatus": "PAID"}, "userErrors": []}}})) as post:
            out = self.m.mark_paid("7001", confirm="yes")
        self.assertEqual(post.call_args.kwargs["json"]["variables"], {"input": {"id": "gid://shopify/Order/7001"}})
        self.assertEqual((out["written"], out["status"]), (True, "paid"))
    def test_the_right_answer_is_no(self):
        cases = [(order(cancelled_at="2026-10-30"), "cancelled"), (order(payment_gateway_names=["SimplePay"]), "not cash on delivery"),
                 (order(financial_status="refunded"), "not pending")]
        for o, why in cases:
            with mock.patch.object(self.m.requests, "get", return_value=Resp(o)), mock.patch.object(self.m.requests, "post") as post:
                with self.assertRaisesRegex(ConnectorError, why): self.m.mark_paid("7001", confirm="yes")
            post.assert_not_called()
    def test_already_paid_writes_nothing(self):
        with mock.patch.object(self.m.requests, "get", return_value=Resp(order(financial_status="paid"))), mock.patch.object(self.m.requests, "post") as post:
            out = self.m.mark_paid("7001", confirm="yes")
        post.assert_not_called(); self.assertTrue(out["already"])

if __name__ == "__main__": unittest.main()
