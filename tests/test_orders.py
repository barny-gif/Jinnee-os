"""Connectors + orders panel, with fake HTTP. Run: python -m unittest discover tests"""
import importlib.util, json, os, pathlib, sys, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "dashboard"))
import orders
from cbase import ConnectorError

def load(n):
    s = importlib.util.spec_from_file_location("t_" + n, ROOT / "connectors" / n / "ops.py"); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

class Resp:
    def __init__(self, data, code=200): self.data, self.status_code, self.ok, self.text, self.content = data, code, code < 400, "", b"%PDF"
    def json(self): return self.data

ORDER = {"id": 1, "name": "#1042", "created_at": "2026-10-10T10:00:00Z", "total_price": "12990", "currency": "HUF", "financial_status": "paid",
         "fulfillment_status": None, "line_items": [{"quantity": 2}], "fulfillments": [], "email": "x@y.hu", "shipping_address": {"country_code": "HU", "name": "Secret"}}

class Shop(unittest.TestCase):
    def setUp(self): os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_ADMIN_TOKEN="t"); self.m = load("shopify")
    def test_order_has_no_personal_data(self):
        with mock.patch("requests.get", return_value=Resp({"orders": [ORDER]})):
            out = self.m.list_orders()
        self.assertEqual(out[0]["name"], "#1042"); self.assertNotIn("Secret", json.dumps(out)); self.assertNotIn("x@y.hu", json.dumps(out))
    def test_bad_token(self):
        with mock.patch("requests.get", return_value=Resp({}, 401)):
            with self.assertRaises(ConnectorError): self.m.list_orders()
    def test_missing_env(self):
        os.environ["SHOPIFY_ADMIN_TOKEN"] = ""
        with self.assertRaises(ConnectorError): self.m.list_orders()

class Bill(unittest.TestCase):
    def setUp(self): os.environ["BILLINGO_API_KEY"] = "k"; self.m = load("billingo")
    def test_draft_forced(self):
        p = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "bp.json"; p.write_text('{"type":"invoice","partner_id":1}')
        with mock.patch("requests.request", return_value=Resp({"id": 5, "type": "draft"})) as r:
            self.m.create_draft(str(p))
        self.assertEqual(r.call_args.kwargs["json"]["type"], "draft")

class Panel(unittest.TestCase):
    def test_match_and_gaps(self):
        os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_ADMIN_TOKEN="t", BILLINGO_API_KEY="k")
        shop = mock.Mock(list_orders=lambda days: [dict(load("shopify")._order(ORDER)), dict(load("shopify")._order({**ORDER, "id": 2, "name": "#1043"}))])
        inv = mock.Mock(list_invoices=lambda days: [{"number": "A-1", "paid": "paid", "comment": "Shopify #1042", "total": 1, "currency": "HUF", "id": 1, "partner": "", "date": "", "type": "invoice"}])
        out = orders.build(shop, inv)
        self.assertEqual(out["orders"][0]["invoice"]["number"], "A-1"); self.assertIs(out["orders"][1]["invoice"], False)
        self.assertEqual(out["summary"]["no_invoice"], 1)
    def test_connector_down_is_not_guessed(self):
        def boom(days): raise ConnectorError("Billingo did not answer")
        shop = mock.Mock(list_orders=lambda days: [load("shopify")._order(ORDER)])
        out = orders.build(shop, mock.Mock(list_invoices=boom))
        self.assertIsNone(out["summary"]["no_invoice"]); self.assertTrue(out["errors"])

if __name__ == "__main__": unittest.main()


class Flow(unittest.TestCase):
    def order(self, **kw):
        o = {"name": "#1042", "id": 1, "currency": "HUF", "taxes_included": True, "gateways": ["Bank deposit"], "email": "a@b.hu", "phone": "",
             "billing": {"name": "Teszt Elek", "zip": "9970", "city": "Szentgotthárd", "address1": "Fő u. 1", "country_code": "HU"}, "shipping": {},
             "lines": [{"title": "Fa puzzle", "sku": "P1", "qty": 2, "price": "5000", "discount": "1000"}], "shipping_lines": [{"title": "GLS", "price": "1490"}]}
        o.update(kw); return o

    def run_flow(self, order, existing=()):
        import order_flow
        shop = mock.Mock(order_private=lambda i: order)
        bill = mock.Mock(list_invoices=lambda days: list(existing), create_partner=mock.Mock(return_value=7), block_id=lambda: 3, post_document=mock.Mock(return_value={"id": 99, "type": "draft"}))
        return order_flow.invoice_draft("1", shop, bill), bill

    def test_draft_body(self):
        out, bill = self.run_flow(self.order())
        body = bill.post_document.call_args.args[0]
        self.assertEqual(out["draft_id"], 99); self.assertEqual(body["type"], "draft"); self.assertEqual(body["comment"], "Shopify #1042")
        self.assertEqual(body["items"][0]["unit_price"], 4500.0); self.assertEqual(body["items"][1]["unit_price"], 1490.0)
        self.assertNotIn("Teszt", json.dumps(out)); self.assertEqual(body["payment_method"], "wire_transfer")
    def test_cod(self):
        _, bill = self.run_flow(self.order(gateways=["Cash on Delivery (COD)"]))
        self.assertEqual(bill.post_document.call_args.args[0]["payment_method"], "cash_on_delivery")
    def test_no_second_invoice(self):
        out, bill = self.run_flow(self.order(), existing=[{"number": "A-1", "id": 1, "comment": "Shopify #1042"}])
        self.assertIn("skipped", out); bill.post_document.assert_not_called()
    def test_incomplete_address(self):
        with self.assertRaises(ConnectorError): self.run_flow(self.order(billing={"name": "X"}))
    def test_only_drafts(self):
        with self.assertRaises(ConnectorError): load("billingo").post_document({"type": "invoice"})


class ClientCredentials(unittest.TestCase):
    def test_token_exchange_and_cache(self):
        for k in ("SHOPIFY_ADMIN_TOKEN",): os.environ[k] = ""
        os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_CLIENT_ID="id", SHOPIFY_CLIENT_SECRET="sec")
        m = load("shopify")
        with mock.patch("requests.post", return_value=Resp({"access_token": "tok", "expires_in": 86399})) as post, \
             mock.patch("requests.get", return_value=Resp({"orders": []})) as get:
            m.list_orders(); m.list_orders()
        self.assertEqual(post.call_count, 1)  # cached for the second call
        self.assertEqual(get.call_args.kwargs["headers"]["X-Shopify-Access-Token"], "tok")
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "client_credentials")
    def test_refused_secret(self):
        os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_ADMIN_TOKEN="", SHOPIFY_CLIENT_ID="id", SHOPIFY_CLIENT_SECRET="bad")
        with mock.patch("requests.post", return_value=Resp({}, 400)):
            with self.assertRaises(ConnectorError): load("shopify").list_orders()


class Gls(unittest.TestCase):
    def setUp(self):
        os.environ.update(GLS_CLIENT_NUMBER="123", GLS_USERNAME="u", GLS_PASSWORD="p", GLS_PICKUP_NAME="Monti", GLS_PICKUP_STREET="Fő u.",
                          GLS_PICKUP_HOUSE="1", GLS_PICKUP_CITY="Szentgotthárd", GLS_PICKUP_ZIP="9970", GLS_PICKUP_PHONE="06301234567")
    def order(self, **kw):
        o = Flow().order(); o["shipping"] = {"name": "Teszt Elek", "zip": "1011", "city": "Budapest", "address1": "Kossuth Lajos utca 12/B", "country_code": "HU"}
        o["phone"] = "06 20 123 4567"; o.update(kw); return o
    def test_street_split_and_phone(self):
        import order_flow
        self.assertEqual(order_flow.split_street("Kossuth Lajos utca 12/B"), ("Kossuth Lajos utca", "12/B"))
        self.assertEqual(order_flow.split_street("Fő u. 1"), ("Fő u.", "1"))
        self.assertEqual(order_flow.phone("06 20 123 4567"), "+36201234567")
        self.assertEqual(order_flow.phone("+43 660 1234"), "+436601234")
    def test_dry_run_sends_nothing_and_cod(self):
        import order_flow
        gls = mock.Mock(client_number=lambda: 123, prepare_labels=mock.Mock(return_value=[{"parcel_id": 1}]))
        shop = mock.Mock(order_private=lambda i: self.order(gateways=["Cash on Delivery (COD)"]))
        out = order_flow.parcel_prepare("1", "no", shop, gls)
        self.assertTrue(out["dry_run"]); gls.prepare_labels.assert_not_called(); self.assertEqual(out["cod_amount"], 9000 + 1490)  # 2 x 5000 - 1000 discount + shipping
        self.assertNotIn("Teszt", json.dumps(out))
        out = order_flow.parcel_prepare("1", "yes", shop, gls)
        sent = gls.prepare_labels.call_args.args[0][0]
        self.assertEqual(sent["DeliveryAddress"]["HouseNumber"], "12/B"); self.assertEqual(sent["ClientReference"], "#1042"); self.assertGreater(sent["CODAmount"], 0)
        self.assertNotIn("Teszt", json.dumps(out))
    def test_gls_errors_surface(self):
        m = load("gls")
        with mock.patch("requests.post", return_value=Resp({"PrepareLabelsError": [{"ErrorCode": 9, "ErrorDescription": "bad zip"}]})):
            with self.assertRaises(ConnectorError): m.prepare_labels([{}])
    def test_hash_password(self):
        import hashlib; m = load("gls")
        self.assertEqual(m._auth()["Password"], list(hashlib.sha512(b"p").digest()))
