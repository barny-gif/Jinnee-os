"""Carrier ledger from carrier e-mails: made-up spreadsheets and letter texts only, no mailbox. Run: python3 -m unittest discover tests"""
import importlib.util, io, json, os, pathlib, sys, tempfile, unittest, zipfile
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))

def load():
    s = importlib.util.spec_from_file_location("t_gls_ledger", ROOT / "connectors" / "gls_ledger.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

def xlsx(*sheets):
    """Each sheet: list of rows (lists of strings), written as inline strings."""
    b = io.BytesIO(); z = zipfile.ZipFile(b, "w")
    for n, rows in enumerate(sheets, 1):
        body = "".join("<row>" + "".join(f'<c t="inlineStr"><is><t>{v}</t></is></c>' for v in r) + "</row>" for r in rows)
        z.writestr(f"xl/worksheets/sheet{n}.xml", f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>')
    z.close(); return b.getvalue()

COD = xlsx([["Example Carrier Kft."], [], ["Partner: Example Kft."], [],
            ["Jelentés szám", "Csomagszám", "Utánvét hivatkozás", "Kiszállítási dátum", "Utánvét összeg", "", "Levelezési cím"],
            ["900", "1001001", "#1001", "2026-11-02", "1234.00", "HUF", "SECRET STREET 1"],
            ["900", "1001002", "#1002", "2026-11-02", "4321.00", "HUF", "SECRET STREET 2"],
            ["", "", "", "", "5555.00", "HUF", ""]])
SETTLE = xlsx([["Számlaszám / Invoice number", "Számla kiállítás dátuma / Invoice date", "Csomagszám / Parcel number",
                "Ügyfél hivatkozás / Client reference", "VÉGÖSSZEG / Total amount", "Címzett neve / Addressee name"],
               ["HU00000001", "46329.2", "1001001", "#1001", "1000.00", "SECRET NAME"],
               ["HU00000001", "46329.2", "1001002", "#1002", "1300.00", "SECRET NAME"]],
              [["Csomagszám"]],
              [["Csomagszám / Parcel ID", "VÉGÖSSZEG / Total amount"], ["1001002", "100.00"]])
COMP = "Kompenzáció\n1 000 1 000 0 HU00000001 2 921 1 921 2026.11.03\nmás\n4 555 1 921 2 634 HU00000001 1 921 0 2026.11.05"
REMIND = "HU00000009 2026.10.03 GR HUF 1.357  11 1___\nLejárt tételek összege HUF 1.357"

class Ledger(unittest.TestCase):
    def setUp(self):
        self.m = load(); self.tmp = pathlib.Path(tempfile.mkdtemp()); self.m.STATE = self.tmp / "gls_ledger.json"
        self.env = mock.patch.dict(os.environ, {"GLS_INVOICE_VAT": "0.27", "GLS_PAID_INVOICES": ""}); self.env.start(); self.addCleanup(self.env.stop)
    def file(self, name, data):
        p = self.tmp / name; p.write_bytes(data if isinstance(data, bytes) else data.encode()); return p

    def test_cod_detail_keeps_no_address_and_skips_the_total_row(self):
        rows = self.m.parse_cod_detail(COD)
        self.assertEqual([(r["ref"], r["amount"]) for r in rows], [("#1001", 1234.0), ("#1002", 4321.0)])
        self.assertNotIn("SECRET", json.dumps(rows))
    def test_settlement_net_includes_cod_fees_and_no_names(self):
        s = self.m.parse_settlement(SETTLE)
        self.assertEqual((s["invoice"], s["date"], s["net"], s["cod_fees_net"]), ("HU00000001", "2026-11-03", 2400.0, 100.0))
        self.assertNotIn("SECRET", json.dumps(s))
    def test_compensation_and_reminder_texts(self):
        c = self.m.parse_compensation(COMP)
        self.assertEqual([(x["cod"], x["compensated"], x["transferred"], x["remaining"]) for x in c], [(1000, 1000, 0, 1921), (4555, 1921, 2634, 0)])
        self.assertEqual(self.m.parse_reminder(REMIND), [{"invoice": "HU00000009", "date": "2026-10-03", "amount": 1357.0}])

    def build(self):
        d = self.m._read()
        self.m.ingest(d, "Számla és Számlamelléklet (HU00000001)", [self.file("SettlementDocument_HU00000001.xlsx", SETTLE)], "1")
        self.m.ingest(d, "Utanvet reszletezo", [self.file("123_HUF_20261103.xlsx", COD)], "2")
        with mock.patch.object(self.m, "_pdf_text", side_effect=[COMP, REMIND]):
            self.m.ingest(d, "Kompenzációs értesítő", [self.file("comp.pdf", b"%PDF")], "3")
            self.m.ingest(d, "I. sz. Fiz. emlekezteto", [self.file("rem.pdf", b"%PDF")], "4")
        self.m._write(d)

    def test_balance_settled_invoice_and_open_reminded_one(self):
        self.build()
        b = self.m.balance()
        self.assertEqual((b["collected_cod"], b["kept_for_invoices"], b["transferred"]), (5555.0, 2921.0, 2634.0))
        self.assertEqual(b["open_invoices"], [{"invoice": "HU00000009", "date": None, "open": 1357.0, "reminded": True}])
        self.assertEqual(b["we_owe_carrier"], 1357.0)
    def test_an_invoice_the_owner_paid_is_not_open(self):
        self.build()
        b = self.m.balance(paid="HU00000009")
        self.assertEqual((b["open_invoices"], b["verdict"]), ([], "nobody owes anything"))
    def test_owner_paid_invoice_kept_in_state(self):
        self.build()
        self.m.mark_paid_invoice("HU00000009", note="bank transfer")
        b = self.m.balance()
        self.assertEqual((b["open_invoices"], b["must_pay"]), ([], 0))
        with self.assertRaises(self.m.ConnectorError): self.m.mark_paid_invoice("1357")
    def test_cod_on_the_way_decides_whether_to_pay(self):
        self.m.UPLOADED, self.m.GLS_STATUS = self.tmp / "up.json", self.tmp / "st.json"
        self.m.UPLOADED.write_text(json.dumps({"#1003": {"parcel_id": 1, "cod_amount": 5000.0}, "#1004": {"parcel_id": 2, "cod_amount": 0}}))
        self.m.GLS_STATUS.write_text(json.dumps({"#1003": {"state": "delivered", "delivered_at": "2026-11-10T10:00:00+00:00", "parcel_number": "77"},
                                                 "#1004": {"state": "delivered"}}))
        self.build()
        b = self.m.balance()
        self.assertEqual(b["cod_on_the_way"], [{"ref": "#1003", "amount": 5000.0, "delivered": "2026-11-10"}])  # a paid-card parcel is not COD
        self.assertEqual(b["must_pay"], 0)  # the open invoice will be kept from it
        self.m.GLS_STATUS.write_text(json.dumps({}))
        self.assertEqual(self.m.balance()["must_pay"], 1357.0)  # nothing coming: pay it
    def test_invoice_with_no_cod_to_keep_from_is_owed(self):
        d = self.m._read()
        self.m.ingest(d, "Számla és Számlamelléklet (HU00000001)", [self.file("SettlementDocument_x.xlsx", SETTLE)], "1"); self.m._write(d)
        b = self.m.balance()
        self.assertEqual(b["we_owe_carrier"], round(2400 * 1.27))  # nothing collected: the whole invoice has to be paid
        self.assertEqual(b["avg_fee_per_parcel_gross"], round(1150 * 1.27))

if __name__ == "__main__": unittest.main()
