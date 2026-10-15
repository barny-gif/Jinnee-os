"""Critical-data checks before the invoice draft and the MyGLS label. Run: python3 -m unittest discover tests"""
import importlib.util, json, pathlib, sys, unittest
from unittest import mock
HERE = pathlib.Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "connectors" / "cbase.py").exists())
sys.path.insert(0, str(ROOT / "connectors"))
s = importlib.util.spec_from_file_location("t_order_checks", ROOT / "connectors" / "order_checks.py"); C = importlib.util.module_from_spec(s); s.loader.exec_module(C)

ADDR = {"name": "Titkos Vevo", "address1": "Fő utca 12/B", "zip": "1111", "city": "Budapest", "country_code": "HU", "phone": "06 30 111 2222"}
OK = {"name": "#1030", "total_price": "12990.00", "financial_status": "paid", "payment_gateway_names": ["SimplePay"], "cancelled_at": None, "test": False,
      "email": "x@y.hu", "phone": None, "shipping_address": ADDR, "billing_address": ADDR, "shipping_lines": [{"title": "GLS házhoz"}]}

def fields(o, step=None): return sorted(p["field"] for p in C.check(o) if step in (None, p["step"]))

class Checks(unittest.TestCase):
    def test_complete_order_has_nothing_missing(self):
        self.assertEqual(C.check(OK), []); self.assertTrue(C.ready([], "label")); self.assertTrue(C.ready([], "invoice"))
    def test_missing_house_number_stops_the_label_only(self):
        o = {**OK, "shipping_address": {**ADDR, "address1": "Fő utca"}}
        p = C.check(o)
        self.assertEqual(fields(o), ["house_number"])
        self.assertFalse(C.ready(p, "label")); self.assertTrue(C.ready(p, "invoice"))
    def test_phone_missing_or_bad(self):
        none = {**ADDR, "phone": None}
        self.assertEqual(fields({**OK, "shipping_address": none, "billing_address": none}), ["phone"])
        self.assertEqual(fields({**OK, "shipping_address": {**ADDR, "phone": "123"}}), ["phone"])
        self.assertEqual(fields({**OK, "shipping_address": none, "billing_address": none, "phone": "+36301112222"}), [])  # order phone is enough
        self.assertEqual(fields({**OK, "shipping_address": none}), [])  # the billing phone is enough too
    def test_zip_city_country(self):
        o = {**OK, "shipping_address": {**ADDR, "zip": "111", "city": "", "country_code": "AT"}}
        self.assertEqual(fields(o), ["city", "country", "zip"])
    def test_no_address_at_all(self):
        self.assertEqual(fields({**OK, "shipping_address": None}, "label"), ["address"])
    def test_billing_incomplete_stops_the_invoice(self):
        o = {**OK, "billing_address": {**ADDR, "address1": ""}}
        p = C.check(o); self.assertEqual(fields(o), ["billing_address"]); self.assertFalse(C.ready(p, "invoice")); self.assertTrue(C.ready(p, "label"))
    def test_no_email_warns_but_does_not_stop(self):
        p = C.check({**OK, "email": None})
        self.assertEqual({(x["step"], x["level"]) for x in p}, {("label", "warn"), ("invoice", "warn")})
        self.assertTrue(C.ready(p, "label")); self.assertTrue(C.ready(p, "invoice"))
    def test_zero_or_test_order_stops_everything(self):
        for o in ({**OK, "total_price": "0.00"}, {**OK, "test": True}):
            p = C.check(o); self.assertFalse(C.ready(p, "label")); self.assertFalse(C.ready(p, "invoice"))
    def test_pickup_point_needs_hand_work(self):
        self.assertEqual(fields({**OK, "shipping_lines": [{"title": "GLS Csomagpont"}]}), ["shipping_method"])
    def test_unknown_payment(self):
        self.assertEqual(fields({**OK, "payment_gateway_names": []}), ["payment"])
    def test_cancelled_is_not_checked(self):
        self.assertEqual(C.check({**OK, "cancelled_at": "2026-10-15", "shipping_address": None}), [])
    def test_output_never_holds_customer_data(self):
        o = {**OK, "shipping_address": {**ADDR, "address1": "Titkos utca", "phone": "1"}, "billing_address": {**ADDR, "zip": ""}}
        text = json.dumps(C.check(o), ensure_ascii=False)
        for secret in ("Titkos", "1111", "Budapest", "x@y.hu"): self.assertNotIn(secret, text)
    def test_cli_lists_order_and_payment_only(self):
        shop = mock.Mock(_get=mock.Mock(return_value={"orders": [{**OK, "shipping_address": {**ADDR, "address1": "Fő utca"}},
                                                                    {**OK, "name": "#1031", "payment_gateway_names": ["Cash on Delivery (COD)"]}]}))
        out = C.check_open(shop=shop)
        self.assertEqual(out[0], {"order": "#1030", "payment": "PAID", "missing": ["label: házszám (stop)"]})
        self.assertEqual(out[1], {"order": "#1031", "payment": "COD 12990 Ft", "missing": "minden megvan"})
        self.assertNotIn("Titkos", json.dumps(out, ensure_ascii=False))

if __name__ == "__main__": unittest.main()
