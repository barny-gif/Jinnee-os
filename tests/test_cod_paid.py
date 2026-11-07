"""COD orders set to paid after GLS 05 delivery, when the label COD amount equals the order total. Made-up shop data only."""
import os, sys, json, pathlib, tempfile, unittest, importlib.util
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = pathlib.Path(os.getenv("JINNEE_APP", ROOT))
sys.path.insert(0, str(APP / "connectors")); sys.path.insert(0, str(APP / "core"))
from cbase import ConnectorError

def load():
    spec = importlib.util.spec_from_file_location("cod_paid_t", ROOT / "connectors" / "cod_paid.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    d = pathlib.Path(tempfile.mkdtemp())
    m.STATE, m.UPLOADED, m.GLS_STATUS = d / "cod_paid.json", d / "gls_uploaded.json", d / "gls_status.json"
    return m

def order(**kw):
    o = {"id": 1001, "name": "#1001", "total_price": "1234.00", "financial_status": "pending", "cancelled_at": None,
         "payment_gateway_names": ["Cash on Delivery (COD)"], "test": False, "tags": ""}
    o.update(kw); return o

DELIVERED = {"#1001": {"state": "delivered", "code": "05"}}
LABEL = {"#1001": {"parcel_id": "9", "cod_amount": 1234.0}}
NOW = 1_800_000_000

class Book:
    def __init__(self): self.items = []
    def add(self, agent, action, title, summary="", text=None, payload=None, **kw):
        it = {"id": f"a{len(self.items) + 1}", "action": action, "text": text, "payload": payload}
        self.items.append(it); return it, ""

class Select(unittest.TestCase):
    def setUp(self): self.m = load()
    def verdict(self, o, ship=DELIVERED, label=LABEL): return self.m.check(o, ship.get(o["name"]), label.get(o["name"]))[0]

    def test_delivered_matching_is_ready(self): self.assertEqual(self.verdict(order()), "ready")
    def test_manual_gateway_pending_is_cod(self): self.assertEqual(self.verdict(order(payment_gateway_names=[])), "ready")
    def test_picked_up_only_is_not(self): self.assertEqual(self.verdict(order(), {"#1001": {"state": "handed_to_gls", "code": "86"}}), "skip")
    def test_held_at_depot_is_not(self): self.assertEqual(self.verdict(order(), {"#1001": {"state": "delivered", "code": "07"}}), "skip")
    def test_cancelled(self): self.assertEqual(self.verdict(order(cancelled_at="2026-01-01")), "skip")
    def test_already_paid(self): self.assertEqual(self.verdict(order(financial_status="paid")), "skip")
    def test_card(self): self.assertEqual(self.verdict(order(payment_gateway_names=["shopify_payments"], financial_status="authorized")), "skip")
    def test_tagged_barter_and_test(self):
        self.assertEqual(self.verdict(order(tags="Barter")), "skip")
        self.assertEqual(self.verdict(order(test=True)), "skip")
    def test_amount_differs_is_reported_not_marked(self):
        ready, mism = self.m.select([order()], DELIVERED, {"#1001": {"parcel_id": "9", "cod_amount": 1500}})
        self.assertEqual(ready, []); self.assertIn("1500", mism[0]); self.assertIn("1234", mism[0])
    def test_unknown_label_amount_is_reported(self):
        self.assertEqual(self.verdict(order(), DELIVERED, {"#1001": "9"}), "mismatch")
    def test_half_forint_rounds_up_and_matches(self):
        self.assertEqual(self.verdict(order(total_price="1234.50"), DELIVERED, {"#1001": {"cod_amount": 1235}}), "ready")

class Run(unittest.TestCase):
    def setUp(self): self.m = load(); self.book = Book(); self.shop = mock.Mock()
    def run_(self, mode, act=True, level=1, now=NOW, raw=None):
        with mock.patch.dict(os.environ, {"SHOPIFY_AUTO_MARK_PAID": mode}):
            return self.m.run(raw or [order()], DELIVERED, LABEL, now, act=act, shop=self.shop, book=self.book, gate=lambda a: level)

    def test_off_by_default_does_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHOPIFY_AUTO_MARK_PAID", None)
            out = self.m.run([order()], DELIVERED, LABEL, NOW, act=True, shop=self.shop, book=self.book)
        self.assertEqual(out["mode"], "off"); self.assertEqual(self.book.items, []); self.shop.mark_paid.assert_not_called()

    def test_queue_adds_one_finished_item(self):
        out = self.run_("queue")
        self.assertEqual(out["queued"], "a1"); it = self.book.items[0]
        self.assertEqual(it["action"], "edit_shopify_order")
        self.assertIn("#1001 – 1 234 Ft", it["text"]); self.assertIn("apply --orders 1001 --confirm yes", it["text"])
        self.shop.mark_paid.assert_not_called()

    def test_dry_round_queues_nothing(self):
        out = self.run_("queue", act=False)
        self.assertEqual(out["ready"], ["#1001"]); self.assertEqual(self.book.items, []); self.assertFalse(self.m.STATE.exists())

    def test_not_queued_twice_and_once_a_day(self):
        self.run_("queue")
        self.run_("queue", now=NOW + 3 * 86400)  # same order, later day: already queued
        self.assertEqual(len(self.book.items), 1)
        self.run_("queue", now=NOW + 60, raw=[order(), order(id=1002, name="#1002")])  # same day, a new order: waits for tomorrow
        self.assertEqual(len(self.book.items), 1)

    def test_auto_below_level_2_falls_back_to_queue(self):
        out = self.run_("auto", level=1)
        self.assertIn("below level 2", out["note"]); self.assertEqual(len(self.book.items), 1); self.shop.mark_paid.assert_not_called()

    def test_auto_at_level_2_marks(self):
        self.shop.mark_paid.return_value = {"written": True}
        out = self.run_("auto", level=2)
        self.assertEqual(out["marked"], ["#1001"]); self.shop.mark_paid.assert_called_once_with("1001", confirm="yes")
        self.run_("auto", level=2); self.assertEqual(self.shop.mark_paid.call_count, 1)  # never twice

    def test_unknown_mode_is_off(self):
        self.assertEqual(self.run_("yes")["mode"], "off"); self.assertEqual(self.book.items, [])

class Apply(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.m.GLS_STATUS.write_text(json.dumps(DELIVERED)); self.m.UPLOADED.write_text(json.dumps(LABEL))
    def shop(self, o):
        s = mock.Mock(); s._get.return_value = {"order": o}; s.mark_paid.return_value = {"written": True, "status": "paid"}; return s

    def test_go_marks_paid(self):
        s = self.shop(order()); out = self.m.apply("1001", confirm="yes", shop=s)
        s.mark_paid.assert_called_once_with("1001", confirm="yes"); self.assertTrue(out["done"][0]["written"])
    def test_without_confirm_is_dry(self):
        s = self.shop(order()); out = self.m.apply("1001", shop=s)
        s.mark_paid.assert_not_called(); self.assertTrue(out["done"][0]["dry_run"])
    def test_changed_since_queue_is_refused(self):
        for changed in (order(total_price="1500"), order(financial_status="paid"), order(cancelled_at="2026-01-01")):
            s = self.shop(changed); out = self.m.apply("1001", confirm="yes", shop=s)
            s.mark_paid.assert_not_called(); self.assertEqual(out["done"], []); self.assertEqual(len(out["refused"]), 1)
    def test_bad_ids(self):
        with self.assertRaises(ConnectorError): self.m.apply("#1001", shop=mock.Mock())

if __name__ == "__main__": unittest.main()
