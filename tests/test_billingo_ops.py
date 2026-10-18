"""Billingo ops `payments` (the site's Payments tab), with fake HTTP only: no test reaches Billingo. Run: python3 -m unittest discover tests"""
import datetime as dt, importlib.util, json, os, pathlib, sys, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))

def load():
    s = importlib.util.spec_from_file_location("t_billingo_ops", ROOT / "connectors" / "billingo" / "ops.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

class Resp:
    def __init__(self, data=None, code=200): self.data, self.status_code, self.ok = data or {}, code, code < 400
    def json(self): return self.data

TODAY = dt.date.today().isoformat()
PAST = (dt.date.today() - dt.timedelta(days=5)).isoformat()
FUTURE = (dt.date.today() + dt.timedelta(days=5)).isoformat()
PARTNER = {"name": "Titkos Vevo", "emails": ["x@y.hu"], "address": {"address": "Fo utca 1"}}

def doc(i, **kw):
    d = {"id": i, "invoice_number": f"MV-2026-{i}", "type": "invoice", "cancelled": False, "comment": f"Shopify #10{i:02d}",
         "invoice_date": PAST, "due_date": FUTURE, "paid_date": None, "gross_total": 1000.0 * i, "currency": "HUF",
         "payment_status": "outstanding", "payment_method": "wire_transfer", "partner": PARTNER, "document_partner": PARTNER}
    d.update(kw); return d

ACCOUNTS = {"data": [{"id": 11, "name": "Revolut HUF", "account_number": "12345678-12345678-87654321", "account_number_iban": "HU42 1234 5678 1234 5678 8765 4321",
                      "currency": "HUF", "swift": "REVOLT21", "need_qr": False}], "total": 1}

class Payments(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"BILLINGO_API_KEY": "k"}); self.env.start()
        self.m = load()
    def tearDown(self): self.env.stop()

    def run_with(self, pages):
        def fake(method, url, params=None, json=None, timeout=None, headers=None):
            if url.endswith("/bank-accounts"): return Resp(ACCOUNTS)
            return Resp(pages[params["page"] - 1])
        with mock.patch.object(self.m.requests, "request", side_effect=fake) as req:
            return self.m.payments(), req

    def test_accounts_last4_only_and_no_partner_data(self):
        docs = [doc(1, payment_status="paid", paid_date=PAST, payment_method="bankcard")]
        out, _ = self.run_with([{"data": docs, "last_page": 1}])
        self.assertEqual(out["bank_accounts"], [{"id": "11", "name": "Revolut HUF", "currency": "HUF", "last4": "4321"}])
        text = json.dumps(out, ensure_ascii=False)
        for secret in ("Titkos", "x@y.hu", "Fo utca", "12345678", "REVOLT21"): self.assertNotIn(secret, text)
        inv = out["invoices"][0]
        self.assertEqual((inv["number"], inv["order"], inv["paid_date"], inv["payment_method"], inv["overdue"]),
                         ("MV-2026-1", "#1001", PAST, "bankcard", False))

    def test_only_issued_not_cancelled_invoices(self):
        docs = [doc(1), doc(2, type="draft"), doc(3, type="proforma"), doc(4, cancelled=True), doc(5, type="cancellation")]
        out, _ = self.run_with([{"data": docs, "last_page": 1}])
        self.assertEqual([i["number"] for i in out["invoices"]], ["MV-2026-1"])

    def test_overdue_is_unpaid_and_past_due(self):
        docs = [doc(1, due_date=PAST), doc(2, due_date=FUTURE), doc(3, due_date=PAST, payment_status="paid", paid_date=PAST), doc(4, due_date=TODAY)]
        out, _ = self.run_with([{"data": docs, "last_page": 1}])
        self.assertEqual([i["overdue"] for i in out["invoices"]], [True, False, False, False])

    def test_all_pages_are_read_and_marked_complete(self):
        out, req = self.run_with([{"data": [doc(1)], "last_page": 2}, {"data": [doc(2)], "last_page": 2}])
        self.assertEqual(len(out["invoices"]), 2); self.assertTrue(out["complete"])
        self.assertEqual(req.call_count, 3)  # accounts + 2 pages

    def test_page_limit_is_reported_as_incomplete(self):
        with mock.patch.object(self.m.requests, "request", side_effect=lambda m, u, params=None, **k:
                               Resp(ACCOUNTS) if u.endswith("/bank-accounts") else Resp({"data": [doc(params["page"])], "last_page": 99})):
            out = self.m.payments(pages="2")
        self.assertFalse(out["complete"]); self.assertEqual(len(out["invoices"]), 2)

    def test_no_order_number_in_comment(self):
        out, _ = self.run_with([{"data": [doc(1, comment="")], "last_page": 1}])
        self.assertIsNone(out["invoices"][0]["order"])

if __name__ == "__main__": unittest.main()
