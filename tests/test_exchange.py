"""Exchange parcel (carrier's exchange service): made-up order, fake carrier; nothing reaches GLS or Shopify."""
import importlib.util, json, os, pathlib, sys, tempfile, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "tests"))
import fake_business; fake_business.use()
from cbase import ConnectorError
import order_flow

def load():
    s = importlib.util.spec_from_file_location("t_exchange", ROOT / "connectors" / "exchange.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

ORDER = {"name": "#1001", "id": 1, "currency": "HUF", "taxes_included": True, "total": "1234.00", "financial_status": "paid",
         "gateways": ["shopify_payments"], "email": "vevo@example.com", "phone": "+36 06 30 111 2222",
         "billing": {}, "shipping": {"name": "Teszt Elek", "zip": "1011", "city": "Budapest", "address1": "Példa utca 5",
                                      "country_code": "HU", "phone": "06 30 111 2222"},
         "lines": [{"title": "Fa kirakó", "sku": "X", "qty": 1, "price": "1234", "discount": "0"}], "shipping_lines": []}

class Exchange(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        order_flow.UPLOADED = self.tmp / "up.json"  # never the real brain/
        os.environ.update(GLS_CLIENT_NUMBER="123", GLS_PICKUP_NAME="Example Shop", GLS_PICKUP_STREET="Fő u.", GLS_PICKUP_HOUSE="1",
                          GLS_PICKUP_CITY="Mintaváros", GLS_PICKUP_ZIP="9999", GLS_PICKUP_PHONE="06301234567")
        self.m = load(); self.m._flow = lambda: order_flow
        self.shop = mock.Mock(order_private=lambda i: json.loads(json.dumps(ORDER)))
        self.gls = mock.Mock(client_number=lambda: 123, prepare_labels=mock.Mock(return_value=[{"parcel_id": 11}, {"parcel_id": 12}]))

    def run_(self, **kw):
        return self.m.prepare("1", "1× Fa kirakó", back_items="1× Fa kirakó", shop=self.shop, gls=self.gls, **kw)

    def test_dry_run_uploads_nothing_and_shows_no_customer_data(self):
        out = self.run_()
        self.assertTrue(out["dry_run"]); self.gls.prepare_labels.assert_not_called()
        self.assertIn("csere: 1× Fa kirakó vissza", out["content"]); self.assertEqual(out["service"], "XS")
        self.assertNotIn("Teszt", json.dumps(out)); self.assertNotIn("Példa", json.dumps(out))

    def test_upload_sends_xs_without_cod_and_keeps_both_parcels(self):
        out = self.run_(send="yes")
        parcel = self.gls.prepare_labels.call_args.args[0][0]
        self.assertEqual((parcel["ServiceList"], parcel["CODAmount"]), ([{"Code": "XS"}], 0))
        self.assertEqual(parcel["DeliveryAddress"]["ContactPhone"], "+36301112222")  # the malformed +36 06 form is repaired
        up = json.loads(order_flow.UPLOADED.read_text(encoding="utf-8"))
        self.assertEqual(up["Csere-1001"]["parcel_id"], 11); self.assertEqual(up["Csere-1001-vissza"]["parcel_id"], 12)
        self.assertEqual(up["Csere-1001"]["manual"]["kind"], "exchange")
        self.assertEqual([u["key"] for u in out["uploaded"]], ["Csere-1001", "Csere-1001-vissza"])

    def test_once_per_order_unless_again(self):
        self.run_(send="yes")
        self.assertIn("skipped", self.run_(send="yes")); self.assertEqual(self.gls.prepare_labels.call_count, 1)
        self.m.prepare("1", "1× Fa kirakó", again="2", send="yes", shop=self.shop, gls=self.gls)
        self.assertIn("Csere-1001-2", json.loads(order_flow.UPLOADED.read_text(encoding="utf-8")))

    def test_the_right_answer_is_no(self):
        with self.assertRaises(ConnectorError): self.m.prepare("#1001", "1× X", shop=self.shop, gls=self.gls)
        with self.assertRaises(ConnectorError): self.m.prepare("1", " ", shop=self.shop, gls=self.gls)
        no_number = json.loads(json.dumps(ORDER)); no_number["shipping"]["address1"] = "Példa utca"
        self.shop.order_private = lambda i: no_number
        with self.assertRaises(ConnectorError): self.m.prepare("1", "1× X", send="yes", shop=self.shop, gls=self.gls)
        self.gls.prepare_labels.assert_not_called()

if __name__ == "__main__": unittest.main()
