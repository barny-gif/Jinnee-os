"""COD orders set to paid only after GLS 05 delivery AND GLS's transfer of the money (carrier ledger), when the label COD amount
and the transferred amount equal the order total. Made-up shop data only."""
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
    m.STATE, m.UPLOADED, m.GLS_STATUS, m.LEDGER = d / "cod_paid.json", d / "gls_uploaded.json", d / "gls_status.json", d / "gls_ledger.json"
    return m

def order(**kw):
    o = {"id": 1001, "name": "#1001", "total_price": "1234.00", "financial_status": "pending", "cancelled_at": None,
         "payment_gateway_names": ["Cash on Delivery (COD)"], "test": False, "tags": ""}
    o.update(kw); return o

DELIVERED = {"#1001": {"state": "delivered", "code": "05", "parcel_number": "90001001", "delivered_at": "2026-12-20T10:00:00+00:00"}}
# the carrier ledger's settlement rows (GLS "Utánvét részletező"): parcel number -> COD reference, amount
ROWS = {"90001001": {"ref": "#1001", "amount": 1234.0, "delivered": "2026-12-20", "report_uid": "7"}}
LABEL = {"#1001": {"parcel_id": "9", "cod_amount": 1234.0}}
NOW = 1_800_000_000

class Book:
    def __init__(self): self.items = []
    def add(self, agent, action, title, summary="", text=None, payload=None, **kw):
        it = {"id": f"a{len(self.items) + 1}", "action": action, "text": text, "payload": payload}
        self.items.append(it); return it, ""

class Select(unittest.TestCase):
    def setUp(self): self.m = load()
    def verdict(self, o, ship=DELIVERED, label=LABEL, rows=ROWS): return self.m.check(o, ship.get(o["name"]), label.get(o["name"]), rows)[0]

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
        ready, mism = self.m.select([order()], DELIVERED, {"#1001": {"parcel_id": "9", "cod_amount": 1500}}, ROWS)
        self.assertEqual(ready, []); self.assertIn("1500", mism[0]); self.assertIn("1234", mism[0])
    def test_unknown_label_amount_is_reported(self):
        self.assertEqual(self.verdict(order(), DELIVERED, {"#1001": "9"}), "mismatch")
    def test_half_forint_rounds_up_and_matches(self):
        self.assertEqual(self.verdict(order(total_price="1234.50"), DELIVERED, {"#1001": {"cod_amount": 1235}},
                                      {"90001001": {"ref": "#1001", "amount": 1235}}), "ready")
    def test_half_forint_rounded_down_on_the_label_matches_too(self):
        # shape of a real case: a ,50 total after a percentage discount; the label rounded half to even, down by 0,50 Ft
        self.assertEqual(self.verdict(order(total_price="1234.50"), DELIVERED, {"#1001": {"cod_amount": 1234}}), "ready")
    def test_one_forint_or_more_apart_is_a_mismatch(self):
        self.assertEqual(self.verdict(order(total_price="1234.50"), DELIVERED, {"#1001": {"cod_amount": 1233}}), "mismatch")
        self.assertEqual(self.verdict(order(total_price="1234.00"), DELIVERED, {"#1001": {"cod_amount": 1235}}), "mismatch")

class Transfer(unittest.TestCase):
    """Owner's rule (2026-11-16): delivered is not enough, GLS must have transferred the COD money (the carrier ledger)."""
    def setUp(self): self.m = load()
    def verdict(self, rows, ship=DELIVERED, o=None):
        o = o or order(); return self.m.check(o, ship.get(o["name"]), LABEL.get(o["name"]), rows)

    def test_delivered_but_not_yet_transferred_waits(self):
        self.assertEqual(self.verdict({})[0], "wait")
    def test_no_ledger_at_all_waits_and_never_marks(self):
        self.assertEqual(self.verdict(None)[0], "wait")
    def test_transferred_with_the_same_amount_is_ready(self):
        v, info = self.verdict(ROWS); self.assertEqual(v, "ready"); self.assertEqual(info["parcel"], "90001001")
    def test_found_by_parcel_number_only_is_ready(self):
        self.assertEqual(self.verdict({"90001001": {"ref": "", "amount": 1234}})[0], "ready")
    def test_found_by_reference_only_when_parcel_unknown_is_ready(self):
        ship = {"#1001": {"state": "delivered", "code": "05"}}
        self.assertEqual(self.verdict({"90009999": {"ref": "#1001", "amount": 1234}}, ship)[0], "ready")
    def test_transferred_amount_differs_is_a_mismatch(self):
        v, why = self.verdict({"90001001": {"ref": "#1001", "amount": 1200}}); self.assertEqual(v, "mismatch")
        self.assertIn("1200", why); self.assertIn("1234", why)
    def test_two_rows_for_one_order_is_unclear(self):
        rows = {"90001001": {"ref": "#1001", "amount": 1234}, "90001002": {"ref": "#1001", "amount": 1234}}
        self.assertEqual(self.verdict(rows)[0], "unclear")
    def test_parcel_settled_under_another_reference_is_unclear(self):
        self.assertEqual(self.verdict({"90001001": {"ref": "#1009", "amount": 1234}})[0], "unclear")
    def test_reference_on_another_parcel_is_unclear(self):
        self.assertEqual(self.verdict({"90007777": {"ref": "#1001", "amount": 1234}})[0], "unclear")
    def test_a_longer_order_number_is_not_this_order(self):
        self.assertEqual(self.verdict({"90007777": {"ref": "#10011", "amount": 1234}})[0], "wait")
    def test_unclear_and_mismatch_are_reported_not_marked(self):
        ready, notes = self.m.select([order()], DELIVERED, LABEL, {"90001001": {"ref": "#1009", "amount": 1234}})
        self.assertEqual(ready, []); self.assertEqual(len(notes), 1)
    def test_late_transfer_is_reported_after_the_warning_days(self):
        late = self.m.sort([order()], DELIVERED, LABEL, {}, NOW)["late"]  # NOW is well past the delivery date
        self.assertEqual(len(late), 1); self.assertIn("#1001", late[0])
        fresh = {"#1001": {**DELIVERED["#1001"], "delivered_at": "2027-01-14T10:00:00+00:00"}}  # a day before NOW
        self.assertEqual(self.m.sort([order()], fresh, LABEL, {}, NOW)["late"], [])
        self.assertEqual(self.m.sort([order()], DELIVERED, LABEL, None, NOW)["late"], [])  # no ledger: a setup note, not a late list

class Run(unittest.TestCase):
    def setUp(self): self.m = load(); self.book = Book(); self.shop = mock.Mock()
    def run_(self, mode, act=True, level=1, now=NOW, raw=None, rows=ROWS):
        with mock.patch.dict(os.environ, {"SHOPIFY_AUTO_MARK_PAID": mode}):
            return self.m.run(raw or [order()], DELIVERED, LABEL, now, act=act, shop=self.shop, book=self.book, gate=lambda a: level, cod_rows=rows)

    def test_off_by_default_does_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHOPIFY_AUTO_MARK_PAID", None)
            out = self.m.run([order()], DELIVERED, LABEL, NOW, act=True, shop=self.shop, book=self.book, cod_rows=ROWS)
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
        self.run_("queue", now=NOW + 60, raw=[order(), order(id=1002, name="#1002")],  # same day, a new order: waits for tomorrow
                  rows={**ROWS, "90001002": {"ref": "#1002", "amount": 1234}})
        self.assertEqual(len(self.book.items), 1)

    def test_auto_below_level_2_falls_back_to_queue(self):
        out = self.run_("auto", level=1)
        self.assertIn("below level 2", out["note"]); self.assertEqual(len(self.book.items), 1); self.shop.mark_paid.assert_not_called()

    def test_auto_at_level_2_marks(self):
        self.shop.mark_paid.return_value = {"written": True}
        out = self.run_("auto", level=2)
        self.assertEqual(out["marked"], ["#1001"]); self.shop.mark_paid.assert_called_once_with("1001", confirm="yes")
        self.run_("auto", level=2); self.assertEqual(self.shop.mark_paid.call_count, 1)  # never twice

    def test_delivered_without_transfer_is_neither_queued_nor_marked(self):
        self.shop.mark_paid.return_value = {"written": True}
        for mode, level in (("queue", 1), ("auto", 2)):
            out = self.run_(mode, level=level, rows={})
            self.assertEqual(out["ready"], []); self.assertEqual(out["waiting"], ["#1001"])
        self.assertEqual(self.book.items, []); self.shop.mark_paid.assert_not_called()
    def test_no_ledger_says_so(self):
        out = self.run_("auto", level=2, rows=None)
        self.assertIn("ledger", out["note"]); self.shop.mark_paid.assert_not_called()
    def test_queue_text_says_gls_transferred(self):
        self.run_("queue"); self.assertIn("át is utalta", self.book.items[0]["text"])

    def test_unknown_mode_is_off(self):
        self.assertEqual(self.run_("yes")["mode"], "off"); self.assertEqual(self.book.items, [])

class Apply(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.m.GLS_STATUS.write_text(json.dumps(DELIVERED)); self.m.UPLOADED.write_text(json.dumps(LABEL))
        self.m.LEDGER.write_text(json.dumps({"cod": ROWS}))
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
    def test_transfer_gone_from_the_ledger_is_refused(self):
        for ledger in ({"cod": {}}, {"cod": {"90001001": {"ref": "#1001", "amount": 999}}}, None):
            if ledger is None: self.m.LEDGER.unlink()
            else: self.m.LEDGER.write_text(json.dumps(ledger))
            s = self.shop(order()); out = self.m.apply("1001", confirm="yes", shop=s)
            s.mark_paid.assert_not_called(); self.assertEqual(out["done"], []); self.assertEqual(len(out["refused"]), 1)
    def test_bad_ids(self):
        with self.assertRaises(ConnectorError): self.m.apply("#1001", shop=mock.Mock())

if __name__ == "__main__": unittest.main()
