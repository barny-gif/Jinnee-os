"""Ecom sync v2 with fake HTTP and fake connectors. Run: python3 -m unittest discover tests"""
import importlib.util, json, os, pathlib, sys, tempfile, unittest
from unittest import mock
HERE = pathlib.Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "connectors" / "cbase.py").exists())
sys.path.insert(0, str(ROOT / "connectors"))
from cbase import ConnectorError

TRAFFIC = [{"day": "2026-10-11", "sessions": 10, "visitors": 10}, {"day": "2026-10-12", "sessions": 8, "visitors": 7}]

def load(traffic=True):
    path = next(p for p in (HERE / "ecom_sync.py", ROOT / "connectors" / "ecom_sync.py") if p.exists())
    s = importlib.util.spec_from_file_location("t_ecom_sync2", path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    m.UPLOADED = pathlib.Path(tempfile.mkdtemp()) / "up.json"  # never the real brain/
    if traffic: m._traffic = lambda shop, days: list(TRAFFIC)  # the analytics call has its own tests below
    return m

def gql(body, code=200):
    s = mock.Mock(_shop_host=lambda: "x.myshopify.com", _token=lambda host: "tok", VERSION="2026-07")
    return s, mock.Mock(return_value=Resp(body, code))

class Traffic(unittest.TestCase):
    def setUp(self): self.m = load(traffic=False)
    def test_rows_as_lists_or_dicts(self):
        cols = [{"name": "day"}, {"name": "sessions"}, {"name": "online_store_visitors"}]
        for rows in ([["2026-10-12", "8", "7"]], [{"day": "2026-10-12T00:00:00Z", "sessions": "8", "online_store_visitors": "7"}]):
            s, post = gql({"data": {"shopifyqlQuery": {"tableData": {"columns": cols, "rows": rows}, "parseErrors": []}}})
            with mock.patch.object(self.m.requests, "post", post):
                self.assertEqual(self.m._traffic(s, "7"), [{"day": "2026-10-12", "sessions": 8, "visitors": 7}])
            self.assertIn("SINCE -7d", post.call_args.kwargs["json"]["variables"]["q"])
    def test_missing_scope_is_named(self):
        s, post = gql({"errors": [{"message": "Access denied for shopifyqlQuery field.", "extensions": {"code": "ACCESS_DENIED"}}]})
        with mock.patch.object(self.m.requests, "post", post):
            with self.assertRaisesRegex(ConnectorError, "read_reports"): self.m._traffic(s, "7")
    def test_unreadable_traffic_is_none_not_zero(self):
        self.m._traffic = mock.Mock(side_effect=ConnectorError("the app has no access to analytics: it needs the read_reports scope"))
        out = self.m.build(shop=shop(), bill=bill())
        self.assertIsNone(out["traffic"])
        self.assertFalse(next(a for a in out["automations"] if a["key"] == "shopify_visitors")["ok"])
        self.assertEqual(len(out["orders"]), 1)  # the orders still go

class Resp:
    def __init__(self, data=None, code=200): self.data, self.status_code, self.ok = data or {}, code, code < 400
    def json(self): return self.data

ADDR = {"name": "Titkos Vevo", "address1": "Fo utca 1", "zip": "1111", "city": "Budapest", "country_code": "HU", "phone": "+36301112222",
        "latitude": 47.5, "first_name": "Titkos"}
ORDER = {"id": 6001, "name": "#1024", "created_at": "2026-10-11T10:00:00+02:00", "total_price": "12990.00", "currency": "HUF",
         "financial_status": "paid", "fulfillment_status": None, "cancelled_at": None, "test": False,
         "fulfillments": [{"tracking_numbers": ["T1"], "created_at": "2026-10-12T09:00:00+02:00"}],
         "payment_gateway_names": ["SimplePay"], "shipping_lines": [{"title": "GLS házhoz", "price": "1890.00"}],
         "line_items": [{"title": "Farmer Kalandkönyv", "variant_id": 77, "sku": "FK1", "quantity": 2, "price": "5550.00"}],
         "email": "x@y.hu", "note": "csengo: Kovacs", "shipping_address": ADDR, "billing_address": ADDR}
COD_ORDER = {**ORDER, "id": 6002, "name": "#1025", "financial_status": "pending", "fulfillments": [], "payment_gateway_names": ["Cash on Delivery (COD)"]}
PRODUCT = {"id": 9, "title": "Farmer Kalandkönyv", "status": "active", "variants": [{"id": 77, "title": "Default Title", "sku": "FK1", "price": "5550.00", "inventory_quantity": 4}]}

def shop(orders=(ORDER,), products=(PRODUCT,), fail=None):
    def _get(path, params=None):
        if fail and path.startswith(fail): raise ConnectorError("Shopify did not answer: Timeout")
        return {"orders": list(orders)} if path == "orders.json" else {"products": list(products)}
    return mock.Mock(_get=mock.Mock(side_effect=_get))

def bill(docs=(), fail=False):
    def list_invoices(days):
        if fail: raise ConnectorError("Billingo did not answer")
        return list(docs)
    return mock.Mock(list_invoices=mock.Mock(side_effect=list_invoices))

class Payload(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_seven_days_and_no_note_asked(self):
        s = shop(); out = self.m.build(shop=s, bill=bill())
        params = s._get.call_args_list[0].args[1]
        self.assertEqual(out["window_days"], 7)
        self.assertNotIn("note", params["fields"].split(","))
        self.assertNotIn("Kovacs", json.dumps(out, ensure_ascii=False))
    def test_private_block_is_separate_and_trimmed(self):
        o = self.m.build(shop=shop(), bill=bill())["orders"][0]
        self.assertEqual(o["private"]["customer_name"], "Titkos Vevo")
        self.assertEqual(o["private"]["email"], "x@y.hu")
        self.assertEqual(set(o["private"]["shipping_address"]), {"name", "address1", "zip", "city", "country_code", "phone"})
        public = json.dumps({k: v for k, v in o.items() if k != "private"}, ensure_ascii=False)
        for secret in ("x@y.hu", "Titkos", "+3630", "Fo utca"): self.assertNotIn(secret, public)
    def test_paid_and_cod(self):
        paid, cod = self.m.build(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill())["orders"]
        self.assertEqual((paid["paid"], paid["is_cod"], paid["payment_method"]), ("paid", False, "SimplePay"))
        self.assertEqual((cod["paid"], cod["is_cod"]), ("pending", True))
        self.assertEqual(paid["shipping_method"], "GLS házhoz")
        self.assertEqual(paid["shipped_at"], "2026-10-12T09:00:00+02:00")
        self.assertIsNone(cod["shipped_at"])
    def test_label_old_entry_has_no_cod_amount(self):
        self.m.UPLOADED.write_text(json.dumps({"#1024": 555, "#1025": True}))
        a, b = self.m.build(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill())["orders"]
        self.assertEqual(a["label"], {"state": "uploaded", "parcel_id": "555"})
        self.assertEqual(b["label"], {"state": "uploaded", "parcel_id": None})
    def test_label_new_entry_carries_cod_amount(self):
        self.m.UPLOADED.write_text(json.dumps({"#1024": {"parcel_id": 555, "cod_amount": 0, "at": "2026-10-12T08:00:00+02:00"},
                                               "#1025": {"parcel_id": 556, "cod_amount": 12990, "at": "2026-10-12T08:01:00+02:00"}}))
        a, b = self.m.build(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill())["orders"]
        self.assertEqual(a["label"], {"state": "uploaded", "parcel_id": "555", "cod_amount": 0.0, "sent_at": "2026-10-12T08:00:00+02:00"})
        self.assertEqual(b["label"]["cod_amount"], 12990.0)
    def test_not_uploaded(self):
        self.assertEqual(self.m.build(shop=shop(), bill=bill())["orders"][0]["label"], {"state": "none", "parcel_id": None})
    def test_invoice_number_does_not_match_longer_order(self):
        docs = [{"comment": "Rendelés #10245", "type": "invoice", "number": "2026-9", "paid": True}]
        self.assertEqual(self.m.build(shop=shop(), bill=bill(docs))["orders"][0]["invoice"]["state"], "none")
    def test_billingo_down_is_unknown_and_reported(self):
        out = self.m.build(shop=shop(), bill=bill(fail=True))
        self.assertEqual(out["orders"][0]["invoice"]["state"], "unknown")
        self.assertFalse(next(a for a in out["automations"] if a["key"] == "billingo")["ok"])
    def test_billingo_asked_once_in_five_minutes(self):
        b = bill()
        self.m.build(shop=shop(), bill=b, now=1000); self.m.build(shop=shop(), bill=b, now=1100)
        self.assertEqual(b.list_invoices.call_count, 1)
        self.m.build(shop=shop(), bill=b, now=1400)
        self.assertEqual(b.list_invoices.call_count, 2)
    def test_billingo_failure_is_not_cached(self):
        self.m.build(shop=shop(), bill=bill(fail=True), now=1000)
        ok = bill([{"comment": "#1024", "type": "invoice", "number": "2026-1", "paid": True}])
        self.assertEqual(self.m.build(shop=shop(), bill=ok, now=1010)["orders"][0]["invoice"]["state"], "issued")
    def test_shopify_down_sends_none_not_empty(self):
        out = self.m.build(shop=shop(fail="orders"), bill=bill())
        self.assertIsNone(out["orders"]); self.assertTrue(out["errors"])

class Push(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_dry_run_shows_counts_only_and_sends_nothing(self):
        with mock.patch.object(self.m.requests, "post") as post:
            out = self.m.push(shop=shop(), bill=bill())
        post.assert_not_called()
        self.assertEqual((out["dry_run"], out["orders"], out["products"]), (True, 1, 1))
        for secret in ("x@y.hu", "Titkos", "+3630", "Fo utca"): self.assertNotIn(secret, json.dumps(out, ensure_ascii=False))
    def test_send_result_has_no_customer_data(self):
        env = {"ECOM_SITE_URL": "https://site.example", "ECOM_INGEST_SECRET": "s3"}
        with mock.patch.dict(os.environ, env), mock.patch.object(self.m.requests, "post", return_value=Resp({"ok": True, "new_order_ids": ["6001"]})) as post:
            out = self.m.push("yes", shop=shop(), bill=bill())
        self.assertEqual(post.call_args.args[0], "https://site.example/api/ecom/ingest")
        self.assertEqual(post.call_args.kwargs["headers"], {"x-ecom-secret": "s3"})
        self.assertEqual(out["new_orders"], 1)
        self.assertNotIn("Titkos", json.dumps(out, ensure_ascii=False))
    def test_refuses_plain_http(self):
        with mock.patch.dict(os.environ, {"ECOM_SITE_URL": "http://site.example", "ECOM_INGEST_SECRET": "s3"}), mock.patch.object(self.m.requests, "post") as post:
            with self.assertRaises(ConnectorError): self.m.push("yes", shop=shop(), bill=bill())
        post.assert_not_called()
    def test_wrong_secret_is_named(self):
        with mock.patch.dict(os.environ, {"ECOM_SITE_URL": "https://site.example", "ECOM_INGEST_SECRET": "s3"}), mock.patch.object(self.m.requests, "post", return_value=Resp(code=401)):
            with self.assertRaisesRegex(ConnectorError, "401"): self.m.push("yes", shop=shop(), bill=bill())

if __name__ == "__main__": unittest.main()
