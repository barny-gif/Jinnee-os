"""Golden test: moving the business values into config/business.yaml changed nothing for the shop the code was written for.

The code before the move (commit BEFORE, read from git history into a temp folder) had one shop's values written into it.
This test reads those values out of the OLD code at run time, writes them into a business.yaml in a temp folder with the
installer's own writer (core/business.py render), and then runs old and new code side by side: the parcel label note, the
whole MyGLS parcel, the manual (non-Shopify) parcel, the Billingo invoice draft body, the Payments invoice list and the
not-revenue check must come out identical. The shop's values themselves never appear in this repo. Nothing is sent:
every HTTP call is faked. Skipped when the git history is not available (e.g. a shallow clone).
Run: python3 -m unittest discover tests"""
import ast, importlib.util, io, json, os, pathlib, re, shutil, subprocess, sys, tarfile, tempfile, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core")); sys.path.insert(0, str(ROOT / "connectors"))
import business

BEFORE = "a8aaed607953d8f7962d23e371f38148faaefd10"  # main just before the business values left the code (PR #34 merged)


def _have_history():
    try:
        return subprocess.run(["git", "cat-file", "-e", BEFORE + "^{commit}"], cwd=ROOT, capture_output=True).returncode == 0
    except OSError:
        return False


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class Resp:
    def __init__(self, data=None, code=200): self.data, self.status_code, self.ok, self.text = data or {}, code, code < 400, ""
    def json(self): return self.data


GOODS = [[], [{"title": "Fa kirakó", "qty": 1, "price": "8490", "discount": "0"}],
         [{"title": "Fa kirakó", "qty": 1, "price": "8490", "discount": "873.62"}, {"title": "Építőkocka", "qty": 2, "price": "3000", "discount": "0"}],
         [{"title": "", "qty": 1, "price": "100", "discount": "0"}, {"title": "Plüss maci", "qty": 0, "price": "2990", "discount": "0"}],
         [{"title": "Nagyon hosszú nevű fa játék " * 4, "qty": 3, "price": "1990", "discount": "0"}] * 3]


@unittest.skipUnless(_have_history(), "needs the git history with commit " + BEFORE[:8])
class OldAndNewAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="jinnee-golden-"))
        tar = subprocess.run(["git", "archive", BEFORE, "connectors", "core"], cwd=ROOT, capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as t: t.extractall(cls.tmp, filter="data")
        old = cls.tmp / "connectors"
        # The shop's values, read out of the old code (they are not written anywhere in this repo)
        flow_src = (old / "order_flow.py").read_text(encoding="utf-8")
        cls.shop = re.search(r"""\(f"(\S+) \{o\['name'\]\}", goods, pay\)""", flow_src).group(1)  # inside the old gls_content
        assert re.search(r'f"' + re.escape(cls.shop) + r' \{ref\}, ', (old / "parcel_manual.py").read_text(encoding="utf-8"))
        cls.prefix = re.search(r'os\.getenv\("BILLINGO_PAYMENTS_PREFIX", "([^"]*)"\)', (old / "billingo" / "ops.py").read_text(encoding="utf-8")).group(1)
        cls.tags = ast.literal_eval(re.search(r"^NOT_REVENUE = (\(.*?\))", (old / "ecom_sync.py").read_text(encoding="utf-8"), re.M).group(1))
        assert cls.shop and cls.prefix and cls.tags
        # ... into a business.yaml, written the way the installer writes it
        cls.yaml = cls.tmp / "business.yaml"
        cls.yaml.write_text(business.render("Golden test", "hu", "as before", label_name=cls.shop, tags=cls.tags, invoice_prefix=cls.prefix),
                            encoding="utf-8")
        cls.env = mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": str(cls.yaml), "GLS_CLIENT_NUMBER": "123", "GLS_USERNAME": "u",
                                               "GLS_PASSWORD": "p", "GLS_PICKUP_NAME": "Example Shop", "GLS_PICKUP_STREET": "Fő u.",
                                               "GLS_PICKUP_HOUSE": "1", "GLS_PICKUP_CITY": "Mintaváros", "GLS_PICKUP_ZIP": "9999",
                                               "GLS_PICKUP_PHONE": "06301234567", "GLS_PICKUP_EMAIL": "", "BILLINGO_API_KEY": "k",
                                               "BILLINGO_PAYMENTS_PREFIX": ""})
        cls.env.start(); os.environ.pop("BILLINGO_PAYMENTS_PREFIX")
        saved_modules, saved_path = dict(sys.modules), list(sys.path)
        try:
            cls.old = {"flow": _load("golden_old_flow", old / "order_flow.py"), "manual": _load("golden_old_manual", old / "parcel_manual.py"),
                       "billingo": _load("golden_old_billingo", old / "billingo" / "ops.py"), "sync": _load("golden_old_sync", old / "ecom_sync.py")}
        finally:  # the old code must not replace any module the other tests use
            for k in set(sys.modules) - set(saved_modules): sys.modules.pop(k, None)
            sys.modules.update(saved_modules); sys.path[:] = saved_path
        new = ROOT / "connectors"
        cls.new = {"flow": _load("golden_new_flow", new / "order_flow.py"), "manual": _load("golden_new_manual", new / "parcel_manual.py"),
                   "billingo": _load("golden_new_billingo", new / "billingo" / "ops.py"), "sync": _load("golden_new_sync", new / "ecom_sync.py")}

    @classmethod
    def tearDownClass(cls):
        cls.env.stop(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def order(self, lines, cod, ship="1490"):
        total = sum(float(l["price"]) * max(int(l["qty"]), 1) - float(l["discount"]) for l in lines) + float(ship)
        return {"name": "#5042", "id": 7, "currency": "HUF", "email": "s@x.hu", "phone": "06 20 123 4567", "taxes_included": True,
                "gateways": ["Cash on Delivery (COD)"] if cod else ["SimplePay"],
                "billing": {"name": "Teszt Elek", "zip": "9999", "city": "Mintaváros", "address1": "Fő u. 1", "country_code": "HU"},
                "shipping": {"name": "Teszt Elek", "zip": "1011", "city": "Budapest", "address1": "Kossuth Lajos utca 12/B", "country_code": "HU"},
                "lines": lines, "shipping_lines": [{"title": "GLS", "price": ship}], "total": f"{round(total)}"}

    def test_label_note(self):
        n = 0
        for lines in GOODS:
            for cod in (0, 10380, 9890.0, 15980.4):
                o = self.order(lines, bool(cod))
                with self.subTest(lines=len(lines), cod=cod):
                    self.assertEqual(self.new["flow"].gls_content(o, cod), self.old["flow"].gls_content(o, cod)); n += 1
        self.assertEqual(n, 20)

    def test_whole_mygls_parcel(self):
        gls = mock.Mock(client_number=lambda: 123)
        for lines in GOODS[1:]:
            for cod in (False, True):
                o = self.order(lines, cod)
                with self.subTest(lines=len(lines), cod=cod):
                    self.assertEqual(self.new["flow"].gls_parcel(o, gls), self.old["flow"].gls_parcel(o, gls))

    def test_manual_parcel_dry_run(self):
        data = dict(channel="eMAG", ref="1000000001", name="Secret Name", zip="9998", city="Mintafalu", street="Fő tér", house="1",
                    phone="06701234567", email="s@x.hu")
        for goods in ("1× Plüss maci", "2× Fa kirakó, 1× Építőkocka", "1× " + "Hosszú név " * 30):
            for cod in ("9890", "0", ""):
                with self.subTest(goods=goods[:12], cod=cod):
                    args = {**data, "goods": goods, "cod": cod}
                    self.assertEqual(self.new["manual"].prepare(**args), self.old["manual"].prepare(**args))

    def test_invoice_draft_body(self):
        bodies = {}
        for side in ("old", "new"):
            for lines in GOODS[1:]:
                for cod in (False, True):
                    o = self.order(lines, cod)
                    shop = mock.Mock(order_private=lambda i, o=o: o)
                    bill = mock.Mock(list_invoices=lambda days: [], create_partner=lambda body: 5, block_id=lambda: 3,
                                     post_document=mock.Mock(return_value={"id": 99, "type": "draft"}))
                    out = getattr(self, side)["flow"].invoice_draft("7", shop=shop, bill=bill)
                    bodies.setdefault(side, []).append((out, bill.post_document.call_args.args[0]))
        self.assertEqual(bodies["new"], bodies["old"])
        self.assertEqual(len(bodies["new"]), 8)

    def test_payments_invoice_list(self):
        docs = [{"id": i, "invoice_number": num, "type": "invoice", "cancelled": False, "comment": f"Shopify #50{i:02d}",
                 "invoice_date": "2026-10-01", "due_date": "2026-10-09", "gross_total": 1000 + i, "currency": "HUF",
                 "payment_status": "paid" if i % 2 else "outstanding", "payment_method": "bankcard"}
                for i, num in enumerate([f"{self.prefix}2026-1", f"{self.prefix}2026-2", "OTHER-2025-14", None, f"x{self.prefix}1",
                                         f"{self.prefix.lower()}2026-3", f"{self.prefix}2026-4", f"{self.prefix[:1]}Z-2026-9", self.prefix[:-1]], 1)]
        accounts = {"data": [{"id": 11, "name": "Bank HUF", "currency": "HUF", "account_number": "12345678-87654321"}]}
        def fake(method, url, params=None, **kw):
            return Resp(accounts) if url.endswith("/bank-accounts") else Resp({"data": docs, "last_page": 1})
        out = {}
        for side in ("old", "new"):
            m = getattr(self, side)["billingo"]
            with mock.patch.object(m.requests, "request", side_effect=fake):
                out[side] = m.payments()
        self.assertEqual(out["new"], out["old"])
        self.assertEqual(len(out["new"]["invoices"]), 3)  # the shop's own series only, as before

    def test_not_revenue(self):
        base = {"total_price": "1000", "financial_status": "paid", "cancelled_at": None, "test": False}
        cases = ["", "VIP", ", ,", *self.tags, *(t.upper() for t in self.tags), *(f"VIP, {t}-2026" for t in self.tags), "barterless", "gift"]
        for tags in cases:
            with self.subTest(tags=tags):
                o = {**base, "tags": tags}
                self.assertEqual(self.new["sync"].not_revenue(o), self.old["sync"].not_revenue(o))
        for extra in ({"cancelled_at": "x"}, {"test": True}, {"total_price": "0"}, {"financial_status": "refunded"}):
            o = {**base, "tags": "", **extra}
            self.assertEqual(self.new["sync"].not_revenue(o), self.old["sync"].not_revenue(o))


if __name__ == "__main__":
    unittest.main()
