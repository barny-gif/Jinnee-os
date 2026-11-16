"""The bank statement report travels to the site as written, known keys only; anything shaped like an account number stops it."""
import json, sys, pathlib, tempfile, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
import ecom_sync

REPORT = {"updated": "2026-11-16", "from": "2026-10-01", "to": "2026-11-16", "currency": "HUF",
          "weeks": [{"week": "2026-W45", "from": "2026-11-02", "to": "2026-11-08", "in": 1234, "out": 567, "own_in": 0}],
          "categories_out": {"ads": 567}, "items": [{"date": "2026-11-02", "dir": "in", "amount": 1234, "label": "Carrier COD transfer"}],
          "split": {"partner_share": 0.5}}


class BankStatement(unittest.TestCase):
    def write(self, data):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = pathlib.Path(d.name) / "bank_statement.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return p

    def test_report_goes_as_written(self):
        self.assertEqual(ecom_sync._bank_statement(self.write(REPORT)), REPORT)

    def test_unknown_keys_are_left_out(self):
        out = ecom_sync._bank_statement(self.write({**REPORT, "raw_pdf_text": "x"}))
        self.assertNotIn("raw_pdf_text", out)

    def test_no_report_is_none(self):
        self.assertIsNone(ecom_sync._bank_statement(pathlib.Path("/nonexistent/bank_statement.json")))
        self.assertIsNone(ecom_sync._bank_statement(self.write("not json")))
        self.assertIsNone(ecom_sync._bank_statement(self.write({"from": "2026-10-01"})))  # no weeks: not a report

    def test_account_number_stops_it(self):
        for acc in ("XX00EXAMPLE0000000000", "11111111-22222222-33333333", "123456789012345678"):
            with self.subTest(acc=acc):
                bad = {**REPORT, "items": [{"date": "2026-11-02", "label": "from " + acc, "amount": 1}]}
                self.assertIsNone(ecom_sync._bank_statement(self.write(bad)))


if __name__ == "__main__":
    unittest.main()
