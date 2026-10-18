"""Automatic MyGLS label + invoice draft for new orders, with fakes only: no test reaches Shopify, MyGLS or Billingo,
and none touches the real brain/. Run: python3 -m unittest discover tests"""
import importlib.util, json, pathlib, sys, tempfile, unittest
from unittest import mock
HERE = pathlib.Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "connectors" / "cbase.py").exists())
sys.path.insert(0, str(ROOT / "connectors"))
from cbase import ConnectorError

def load():
    s = importlib.util.spec_from_file_location("t_order_auto", ROOT / "connectors" / "order_auto.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    tmp = pathlib.Path(tempfile.mkdtemp())
    m.STATE, m.START = tmp / "order_auto.json", tmp / "order_auto_start.json"
    return m

ADDR = {"name": "Titkos Vevo", "address1": "Fő utca 12/B", "zip": "1111", "city": "Budapest", "country_code": "HU", "phone": "06 30 111 2222"}
NOW = 1792400000.0  # 2026-10-19
def order(name="#1028", created="2026-10-19T10:00:00+02:00", **kw):
    o = {"id": 7001, "name": name, "created_at": created, "total_price": "10380.00", "financial_status": "paid",
         "payment_gateway_names": ["SimplePay"], "cancelled_at": None, "test": False, "email": "x@y.hu", "phone": None,
         "shipping_address": ADDR, "billing_address": ADDR, "shipping_lines": [{"title": "GLS házhoz"}]}
    o.update(kw); return o

def flow(fail=None):
    def parcel_prepare(order_id, send="no"):
        if fail == "label": raise ConnectorError("[upload to MyGLS] MyGLS did not answer")
        assert send == "yes"; return {"order": "#", "uploaded": [{"parcel_id": 555}], "warnings": []}
    def invoice_draft(order_id):
        if fail == "draft": raise ConnectorError("[create draft] Billingo answered 500")
        return {"order": "#", "draft_id": 99, "type": "draft"}
    return mock.Mock(parcel_prepare=mock.Mock(side_effect=parcel_prepare), invoice_draft=mock.Mock(side_effect=invoice_draft))

ON = lambda action: 2
OFF_LABEL = lambda action: 1 if action == "upload_parcel_label" else 3

class OrderAuto(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.m._write(self.m.START, {"since": "2026-10-19T00:00:00+00:00"})

    def test_complete_new_order_gets_label_and_draft(self):
        f = flow(); out = self.m.run([order()], NOW, act=True, flow=f, gate=ON)
        self.assertEqual((out["labels"], out["drafts"], out["held"]), (["#1028"], ["#1028"], {}))
        f.parcel_prepare.assert_called_once_with("7001", send="yes"); f.invoice_draft.assert_called_once_with("7001")
        st = json.loads(self.m.STATE.read_text())["#1028"]
        self.assertEqual((st["label"]["parcel_id"], st["draft"]["draft_id"]), (555, 99))
        self.assertNotIn("Titkos", json.dumps(out) + self.m.STATE.read_text())

    def test_never_twice(self):
        f = flow(); self.m.run([order()], NOW, act=True, flow=f, gate=ON); self.m.run([order()], NOW + 60, act=True, flow=f, gate=ON)
        self.assertEqual((f.parcel_prepare.call_count, f.invoice_draft.call_count), (1, 1))
        f2 = flow(); self.m.run([order(name="#1029")], NOW, act=True, uploaded={"#1029": {"parcel_id": 1}}, flow=f2, gate=ON)
        f2.parcel_prepare.assert_not_called()  # already in gls_uploaded.json (uploaded by hand)

    def test_missing_house_number_holds_the_label_but_not_the_draft(self):
        f = flow(); o = order(shipping_address={**ADDR, "address1": "Fő utca"})
        out = self.m.run([o], NOW, act=True, flow=f, gate=ON)
        f.parcel_prepare.assert_not_called(); f.invoice_draft.assert_called_once()
        self.assertEqual(out["held"], {"#1028": ["label: házszám"]})

    def test_missing_phone_holds_the_label(self):
        none = {**ADDR, "phone": None}
        out = self.m.run([order(shipping_address=none, billing_address=none)], NOW, act=True, flow=flow(), gate=ON)
        self.assertEqual(out["labels"], []); self.assertIn("label: telefonszám", out["held"]["#1028"])

    def test_billing_incomplete_holds_the_draft(self):
        f = flow(); out = self.m.run([order(billing_address={**ADDR, "address1": ""})], NOW, act=True, flow=f, gate=ON)
        f.invoice_draft.assert_not_called(); self.assertEqual(out["labels"], ["#1028"])
        self.assertEqual(out["held"], {"#1028": ["invoice: számlázási cím"]})

    def test_cancelled_test_zero_and_pickup_point_are_not_touched(self):
        f = flow()
        orders = [order(name="#1", cancelled_at="2026-10-19T11:00:00Z"), order(name="#2", test=True), order(name="#3", total_price="0.00"),
                  order(name="#4", shipping_lines=[{"title": "GLS csomagpont"}])]
        out = self.m.run(orders, NOW, act=True, flow=f, gate=ON)
        f.parcel_prepare.assert_not_called(); self.assertEqual(out["labels"], [])
        self.assertIn("#4", out["held"])

    def test_label_off_by_default_level(self):
        f = flow(); out = self.m.run([order()], NOW, act=True, flow=f, gate=OFF_LABEL)
        f.parcel_prepare.assert_not_called(); self.assertEqual(out["drafts"], ["#1028"]); self.assertFalse(out["enabled"]["label"])
        out = self.m.run([order(name="#1029")], NOW, act=True, flow=flow(), gate=lambda a: 1)
        self.assertEqual(out["enabled"], {"label": False, "draft": False}); self.assertEqual(out["drafts"], [])

    def test_no_back_fill_when_first_switched_on(self):
        self.m.START.unlink()
        f = flow(); out = self.m.run([order(created="2026-10-18T23:08:32+02:00")], NOW, act=True, flow=f, gate=ON)
        f.parcel_prepare.assert_not_called(); f.invoice_draft.assert_not_called(); self.assertEqual(out["labels"], [])
        self.assertTrue(self.m.START.exists())
        with mock.patch.dict("os.environ", {"ORDER_AUTO_SINCE": "2026-10-18"}):  # back-fill only when asked
            self.assertEqual(self.m.run([order(created="2026-10-18T23:08:32+02:00")], NOW, act=True, flow=flow(), gate=ON)["labels"], ["#1028"])

    def test_dry_run_does_nothing(self):
        self.m.START.unlink()
        f = flow(); out = self.m.run([order(created="2026-10-19T20:00:00+02:00")], NOW, act=False, flow=f, gate=ON)
        f.parcel_prepare.assert_not_called(); f.invoice_draft.assert_not_called()
        self.assertFalse(self.m.START.exists()); self.assertFalse(self.m.STATE.exists())

    def test_failure_is_recorded_and_retried_later_not_every_round(self):
        out = self.m.run([order()], NOW, act=True, flow=flow(fail="label"), gate=ON)
        self.assertTrue(any("MyGLS did not answer" in e for e in out["errors"])); self.assertEqual(out["drafts"], ["#1028"])
        f = flow(); self.m.run([order()], NOW + 60, act=True, flow=f, gate=ON); f.parcel_prepare.assert_not_called()
        self.m.run([order()], NOW + self.m.RETRY + 1, act=True, flow=f, gate=ON); f.parcel_prepare.assert_called_once()

    def test_unreadable_autonomy_fails_closed(self):
        with mock.patch.dict(sys.modules, {"autonomy": None}):
            self.assertEqual(self.m._gate("upload_parcel_label"), 0)

if __name__ == "__main__": unittest.main()
