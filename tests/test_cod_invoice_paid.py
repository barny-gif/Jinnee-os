"""COD invoices set paid in Billingo only after the carrier settled the same amount for the same order (made-up values)."""
import os, sys, json, pathlib, tempfile, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "core"))
import cod_invoice_paid as cip

ROWS = {"111": {"ref": "#1001", "delivered": "2026-01-05", "amount": 1234.0},
        "222": {"ref": "#1002", "delivered": "2026-01-06", "amount": 5000.0},
        "333": {"ref": "#1004", "delivered": "2026-01-06", "amount": 900.0},
        "334": {"ref": "#1004", "delivered": "2026-01-07", "amount": 900.0}}

def inv(i, number, order, total, status="outstanding", method="cash_on_delivery"):
    return {"id": i, "number": number, "order": order, "total": total, "payment_status": status, "payment_method": method}

INVOICES = [inv("1", "EX-1", "#1001", 1234),            # settled, same amount → ready
            inv("2", "EX-2", "#1002", 4999),            # settled, different amount → mismatch, never marked
            inv("3", "EX-3", "#1003", 700),             # not settled yet → waits
            inv("4", "EX-4", "#1004", 900),             # two settlement rows → unclear
            inv("5", "EX-5", "#1001", 1234, status="paid"),           # already paid → untouched
            inv("6", "EX-6", "#1001", 1234, method="bankcard")]       # paid by card → untouched

class Book:
    def __init__(self): self.items = []
    def add(self, agent, action, title, **kw):
        self.items.append({"agent": agent, "action": action, "title": title, **kw}); return {"id": f"a{len(self.items)}"}, True

class Bill:
    def __init__(self, invoices): self.invoices = invoices; self.marked = []
    def payments(self): return {"invoices": self.invoices}
    def mark_paid(self, id, price, method, date): self.marked.append((id, price, method, date)); return True

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); p = pathlib.Path(self.tmp.name)
        self.state = mock.patch.object(cip, "STATE", p / "state.json"); self.state.start()
        self.ledger = mock.patch.object(cip, "LEDGER", p / "ledger.json"); self.ledger.start()
        (p / "ledger.json").write_text(json.dumps({"cod": ROWS}))
    def tearDown(self):
        self.state.stop(); self.ledger.stop(); self.tmp.cleanup()

class SortTest(Base):
    def test_only_the_settled_matching_unpaid_cod_invoice_is_ready(self):
        s = cip.sort(INVOICES, ROWS)
        self.assertEqual([r["number"] for r in s["ready"]], ["EX-1"])
        self.assertEqual(s["ready"][0]["date"], "2026-01-05")
        self.assertEqual(len(s["mismatch"]), 1); self.assertIn("#1002", s["mismatch"][0])
        self.assertEqual(s["waiting"], ["#1003"])
        self.assertEqual(len(s["unclear"]), 1); self.assertIn("#1004", s["unclear"][0])

class RunTest(Base):
    def test_off_by_default_does_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BILLINGO_AUTO_MARK_PAID", None)
            bill, book = Bill(INVOICES), Book()
            out = cip.run(10_000, act=True, bill=bill, book=book)
        self.assertEqual(out["mode"], "off"); self.assertEqual(bill.marked, []); self.assertEqual(book.items, [])

    def test_queue_adds_one_item_a_day_and_writes_nothing(self):
        bill, book = Bill(INVOICES), Book()
        with mock.patch.dict(os.environ, {"BILLINGO_AUTO_MARK_PAID": "queue"}):
            out = cip.run(10_000, act=True, bill=bill, book=book)
            again = cip.run(10_000 + 2 * cip.CHECK_EVERY, act=True, bill=bill, book=book)
        self.assertEqual(out["queued"], "a1"); self.assertEqual(len(book.items), 1); self.assertIsNone(again["queued"])
        item = book.items[0]
        self.assertEqual(item["action"], "issue_invoice"); self.assertEqual(item["ref"], "1001_cod_fizetett_szamla")
        self.assertIn("apply --invoices 1 --confirm yes", item["text"])
        self.assertEqual(bill.marked, [])

    def test_auto_below_level_2_falls_back_to_queue(self):
        bill, book = Bill(INVOICES), Book()
        with mock.patch.dict(os.environ, {"BILLINGO_AUTO_MARK_PAID": "auto"}):
            out = cip.run(10_000, act=True, bill=bill, book=book, gate=lambda a: 1)
        self.assertIn("below level 2", out["note"]); self.assertEqual(bill.marked, []); self.assertEqual(len(book.items), 1)

    def test_auto_at_level_2_marks_only_the_ready_invoice(self):
        bill = Bill(INVOICES)
        with mock.patch.dict(os.environ, {"BILLINGO_AUTO_MARK_PAID": "auto"}):
            out = cip.run(10_000, act=True, bill=bill, book=Book(), gate=lambda a: 2)
        self.assertEqual(out["marked"], ["EX-1"])
        self.assertEqual(bill.marked, [("1", 1234, "cash_on_delivery", "2026-01-05")])

    def test_dry_round_writes_nothing(self):
        bill, book = Bill(INVOICES), Book()
        with mock.patch.dict(os.environ, {"BILLINGO_AUTO_MARK_PAID": "auto"}):
            out = cip.run(10_000, act=False, bill=bill, book=book, gate=lambda a: 2)
        self.assertEqual(out["ready"], ["EX-1"]); self.assertEqual(bill.marked, []); self.assertEqual(book.items, [])

    def test_no_ledger_marks_nothing(self):
        bill = Bill(INVOICES)
        with mock.patch.dict(os.environ, {"BILLINGO_AUTO_MARK_PAID": "auto"}):
            out = cip.run(10_000, act=True, bill=bill, book=Book(), gate=lambda a: 2, rows=None)
        self.assertEqual(bill.marked, []); self.assertIn("no carrier ledger", out["note"])

class ApplyTest(Base):
    def test_dry_run_then_write(self):
        bill = Bill(INVOICES)
        dry = cip.apply("1", bill=bill)
        self.assertTrue(dry["done"][0]["dry_run"]); self.assertEqual(bill.marked, [])
        done = cip.apply("1", confirm="yes", bill=bill)
        self.assertTrue(done["done"][0]["written"]); self.assertEqual(len(bill.marked), 1)

    def test_refuses_what_changed_since_the_approval(self):
        bill = Bill([inv("1", "EX-1", "#1001", 1234, status="paid"), inv("2", "EX-2", "#1002", 4999)])
        out = cip.apply("1,2,9", confirm="yes", bill=bill)
        self.assertEqual(out["done"], []); self.assertEqual(len(out["refused"]), 3); self.assertEqual(bill.marked, [])

if __name__ == "__main__":
    unittest.main()
