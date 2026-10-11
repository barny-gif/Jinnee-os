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
