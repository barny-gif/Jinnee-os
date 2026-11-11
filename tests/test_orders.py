"""Connectors + orders panel, with fake HTTP. Run: python -m unittest discover tests"""
import importlib.util, json, os, pathlib, sys, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT / "tests")); import fake_business; fake_business.use()  # a made-up business (config/business.yaml)
import orders
from cbase import ConnectorError

def load(n):
    s = importlib.util.spec_from_file_location("t_" + n, ROOT / "connectors" / n / "ops.py"); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

class Resp:
    def __init__(self, data, code=200): self.data, self.status_code, self.ok, self.text, self.content = data, code, code < 400, "", b"%PDF"
    def json(self): return self.data

ORDER = {"id": 1, "name": "#5042", "created_at": "2026-10-10T10:00:00Z", "total_price": "12600", "currency": "HUF", "financial_status": "paid",
         "fulfillment_status": None, "line_items": [{"quantity": 2}], "fulfillments": [], "email": "x@y.hu", "shipping_address": {"country_code": "HU", "name": "Secret"}}

class Shop(unittest.TestCase):
    def setUp(self): os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_ADMIN_TOKEN="t"); self.m = load("shopify")
    def test_order_has_no_personal_data(self):
        with mock.patch("requests.get", return_value=Resp({"orders": [ORDER]})):
            out = self.m.list_orders()
        self.assertEqual(out[0]["name"], "#5042"); self.assertNotIn("Secret", json.dumps(out)); self.assertNotIn("x@y.hu", json.dumps(out))
    def test_bad_token(self):
        with mock.patch("requests.get", return_value=Resp({}, 401)):
            with self.assertRaises(ConnectorError): self.m.list_orders()
    def test_missing_env(self):
        os.environ["SHOPIFY_ADMIN_TOKEN"] = ""
        with self.assertRaises(ConnectorError): self.m.list_orders()

class Bill(unittest.TestCase):
    def setUp(self): os.environ["BILLINGO_API_KEY"] = "k"; self.m = load("billingo")
    def test_draft_forced(self):
        p = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "bp.json"; p.write_text('{"type":"invoice","partner_id":1}')
        with mock.patch("requests.request", return_value=Resp({"id": 5, "type": "draft"})) as r:
            self.m.create_draft(str(p))
        self.assertEqual(r.call_args.kwargs["json"]["type"], "draft")
    def test_status_shows_what_happened_to_the_email(self):
        with mock.patch("requests.request", return_value=Resp({"id": 5, "type": "invoice", "invoice_number": "EX-2026-1", "notification_status": "opened"})):
            self.assertEqual(self.m.status("5")["email"], "opened")
    DRAFT = {"id": 9, "type": "draft", "block_id": 3, "partner": {"id": 5, "name": "Secret"}, "fulfillment_date": "2026-10-13", "due_date": "2020-01-01",
             "payment_method": "bankcard", "language": "hu", "currency": "HUF", "conversion_rate": 1, "electronic": False, "comment": "Shopify #5042",
             "gross_total": 13900, "items": [{"name": "Fa kirakó", "quantity": 2, "unit": "db", "vat": "AAM", "gross_amount": 12400, "net_amount": 12400},
                                             {"name": "Szállítás", "quantity": 1, "unit": "db", "vat": "AAM", "gross_amount": 1500}]}
    def test_issue_from_draft_puts_the_whole_document(self):
        with mock.patch("requests.request", side_effect=[Resp(self.DRAFT), Resp({"id": 9, "invoice_number": "EX-2026-1"})]) as r:
            out = self.m.create_from_draft(9)
        self.assertEqual(out["invoice_number"], "EX-2026-1"); self.assertEqual(r.call_count, 2)
        put = r.call_args_list[1]; body = put.kwargs["json"]
        self.assertEqual(put.args[:2], ("PUT", self.m.BASE + "/documents/9")); self.assertEqual(body["type"], "invoice")
        self.assertEqual((body["partner_id"], body["block_id"], body["payment_method"], body["comment"]), (5, 3, "bankcard", "Shopify #5042"))
        self.assertEqual(body["items"][0], {"name": "Fa kirakó", "unit_price": 6200.0, "unit_price_type": "gross", "quantity": 2.0, "unit": "db", "vat": "AAM"})
        self.assertGreaterEqual(body["due_date"], __import__("datetime").date.today().isoformat()); self.assertNotIn("Secret", json.dumps(body))
    def test_issue_leaves_out_empty_settings(self):
        draft = {**self.DRAFT, "settings": {"place_id": None, "dont_send_to_nav_reason": "", "round": "one", "without_financial_fulfillment": False}}
        with mock.patch("requests.request", side_effect=[Resp(draft), Resp({"id": 9, "invoice_number": "EX-2026-1"})]) as r:
            self.m.create_from_draft(9)
        self.assertEqual(r.call_args_list[1].kwargs["json"]["settings"], {"round": "one", "without_financial_fulfillment": False})
        with mock.patch("requests.request", side_effect=[Resp({**self.DRAFT, "settings": {"place_id": None}}), Resp({"id": 9})]) as r:
            self.m.create_from_draft(9)
        self.assertNotIn("settings", r.call_args_list[1].kwargs["json"])
    def test_validation_error_names_every_field(self):
        errors = [{"field": "settings.place_id", "message": "must be an integer"}, {"field": "settings.dont_send_to_nav_reason", "message": "is invalid " + "x" * 300}]
        with mock.patch("requests.request", side_effect=[Resp(self.DRAFT), Resp({"message": "Validation Failed", "errors": errors}, 422)]):
            with self.assertRaises(ConnectorError) as e: self.m.create_from_draft(9)
        self.assertIn("settings.place_id", str(e.exception)); self.assertIn("settings.dont_send_to_nav_reason", str(e.exception))
    def test_issue_refused_when_lines_do_not_add_up(self):
        with mock.patch("requests.request", side_effect=[Resp({**self.DRAFT, "gross_total": 20000})]) as r:
            with self.assertRaises(ConnectorError): self.m.create_from_draft(9)
        self.assertEqual(r.call_count, 1)  # only the read, nothing issued
    def test_issue_refused_when_not_a_draft(self):
        with mock.patch("requests.request", side_effect=[Resp({**self.DRAFT, "type": "invoice"})]) as r:
            with self.assertRaises(ConnectorError): self.m.create_from_draft(9)
        self.assertEqual(r.call_count, 1)

class Panel(unittest.TestCase):
    def test_match_and_gaps(self):
        os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_ADMIN_TOKEN="t", BILLINGO_API_KEY="k")
        shop = mock.Mock(list_orders=lambda days: [dict(load("shopify")._order(ORDER)), dict(load("shopify")._order({**ORDER, "id": 2, "name": "#5043"}))])
        inv = mock.Mock(list_invoices=lambda days: [{"number": "A-1", "paid": "paid", "comment": "Shopify #5042", "total": 1, "currency": "HUF", "id": 1, "partner": "", "date": "", "type": "invoice"}])
        out = orders.build(shop, inv)
        self.assertEqual(out["orders"][0]["invoice"]["number"], "A-1"); self.assertIs(out["orders"][1]["invoice"], False)
        self.assertEqual(out["summary"]["no_invoice"], 1)
    def test_connector_down_is_not_guessed(self):
        def boom(days): raise ConnectorError("Billingo did not answer")
        shop = mock.Mock(list_orders=lambda days: [load("shopify")._order(ORDER)])
        out = orders.build(shop, mock.Mock(list_invoices=boom))
        self.assertIsNone(out["summary"]["no_invoice"]); self.assertTrue(out["errors"])

if __name__ == "__main__": unittest.main()


class Flow(unittest.TestCase):
    def order(self, **kw):
        o = {"name": "#5042", "id": 1, "currency": "HUF", "taxes_included": True, "gateways": ["Bank deposit"], "email": "a@b.hu", "phone": "",
             "billing": {"name": "Teszt Elek", "zip": "9999", "city": "Mintaváros", "address1": "Fő u. 1", "country_code": "HU"}, "shipping": {},
             "lines": [{"title": "Fa puzzle", "sku": "P1", "qty": 2, "price": "5000", "discount": "1000"}], "shipping_lines": [{"title": "GLS", "price": "1200"}]}
        o.update(kw); return o

    def run_flow(self, order, existing=()):
        import order_flow
        shop = mock.Mock(order_private=lambda i: order)
        bill = mock.Mock(list_invoices=lambda days: list(existing), create_partner=mock.Mock(return_value=7), block_id=lambda: 3, post_document=mock.Mock(return_value={"id": 99, "type": "draft"}))
        return order_flow.invoice_draft("1", shop, bill), bill

    def test_draft_body(self):
        out, bill = self.run_flow(self.order())
        body = bill.post_document.call_args.args[0]
        self.assertEqual(out["draft_id"], 99); self.assertEqual(body["type"], "draft"); self.assertEqual(body["comment"], "Shopify #5042")
        self.assertEqual(body["items"][0]["unit_price"], 4500.0); self.assertEqual(body["items"][1]["unit_price"], 1200.0)
        self.assertNotIn("Teszt", json.dumps(out)); self.assertEqual(body["payment_method"], "wire_transfer")
    def test_cod(self):
        _, bill = self.run_flow(self.order(gateways=["Cash on Delivery (COD)"]))
        self.assertEqual(bill.post_document.call_args.args[0]["payment_method"], "cash_on_delivery")
    def test_no_second_invoice(self):
        out, bill = self.run_flow(self.order(), existing=[{"number": "A-1", "id": 1, "comment": "Shopify #5042"}])
        self.assertIn("skipped", out); bill.post_document.assert_not_called()
    def test_incomplete_address(self):
        with self.assertRaises(ConnectorError): self.run_flow(self.order(billing={"name": "X"}))
    def test_only_drafts(self):
        with self.assertRaises(ConnectorError): load("billingo").post_document({"type": "invoice"})


class ClientCredentials(unittest.TestCase):
    def test_token_exchange_and_cache(self):
        for k in ("SHOPIFY_ADMIN_TOKEN",): os.environ[k] = ""
        os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_CLIENT_ID="id", SHOPIFY_CLIENT_SECRET="sec")
        m = load("shopify")
        with mock.patch("requests.post", return_value=Resp({"access_token": "tok", "expires_in": 86399})) as post, \
             mock.patch("requests.get", return_value=Resp({"orders": []})) as get:
            m.list_orders(); m.list_orders()
        self.assertEqual(post.call_count, 1)  # cached for the second call
        self.assertEqual(get.call_args.kwargs["headers"]["X-Shopify-Access-Token"], "tok")
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "client_credentials")
    def test_refused_secret(self):
        os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_ADMIN_TOKEN="", SHOPIFY_CLIENT_ID="id", SHOPIFY_CLIENT_SECRET="bad")
        with mock.patch("requests.post", return_value=Resp({}, 400)):
            with self.assertRaises(ConnectorError): load("shopify").list_orders()


class Gls(unittest.TestCase):
    def setUp(self):
        import order_flow, tempfile
        order_flow.UPLOADED = pathlib.Path(tempfile.mkdtemp()) / "up.json"  # never touch the real brain/
        os.environ.update(GLS_CLIENT_NUMBER="123", GLS_USERNAME="u", GLS_PASSWORD="p", GLS_PICKUP_NAME="Example Shop", GLS_PICKUP_STREET="Fő u.",
                          GLS_PICKUP_HOUSE="1", GLS_PICKUP_CITY="Mintaváros", GLS_PICKUP_ZIP="9999", GLS_PICKUP_PHONE="06301234567")
    def order(self, **kw):
        o = Flow().order(); o["shipping"] = {"name": "Teszt Elek", "zip": "1011", "city": "Budapest", "address1": "Kossuth Lajos utca 12/B", "country_code": "HU"}
        o["phone"] = "06 20 123 4567"; o.update(kw); return o
    def test_hand_made_pending_order_is_cod(self):
        # The owner re-made a COD order from a draft; no gateway, financial_status pending. It printed 'PAID' and COD 0.
        import order_flow
        g = mock.Mock(client_number=lambda: 1)
        for gw in ([], [""], ["manual"]):
            o = self.order(gateways=gw, financial_status="pending", total="10200.00")
            p = order_flow.gls_parcel(o, g)
            self.assertEqual(p["CODAmount"], 10200, gw); self.assertIn("COD", p["Content"]); self.assertNotIn("PAID", p["Content"])
        # the right answer is 'not COD': paid with no gateway, or a pending card order
        self.assertEqual(order_flow.gls_parcel(self.order(gateways=[], financial_status="paid"), g)["CODAmount"], 0)
        self.assertEqual(order_flow.payment_method(["shopify_payments"], "pending"), "bankcard")
        self.assertEqual(order_flow.payment_method([], None), "bankcard")  # status unknown: unchanged behaviour
        self.assertEqual(order_flow.payment_method(["Cash on Delivery (COD)"], "pending"), "cash_on_delivery")
    def test_street_split_and_phone(self):
        import order_flow
        self.assertEqual(order_flow.split_street("Kossuth Lajos utca 12/B"), ("Kossuth Lajos utca", "12/B"))
        self.assertEqual(order_flow.split_street("Fő u. 1"), ("Fő u.", "1"))
        # shape of a real case: no space after the street type's dot, the number ends with a dot too
        self.assertEqual(order_flow.split_street("Példa Kálmán u.9."), ("Példa Kálmán u.", "9"))  # the lone trailing dot goes (#68 kept it)
        self.assertEqual(order_flow.split_street("Fő u.12/B"), ("Fő u.", "12/B"))
        self.assertEqual(order_flow.split_street("Fő utca"), ("Fő utca", ""))  # still no number = no number
        self.assertEqual(order_flow.split_street("Fő tér 3. 2. em."), ("Fő tér", "3. 2. em."))  # was ('Fő tér 3.', '2. em.'): the 3 was read as part of the street
        self.assertEqual(order_flow.phone("06 20 123 4567"), "+36201234567")
    def test_placeholder_address2_keeps_house_number(self):
        # address1 'Utca neve 5', address2 '-' -> the number was not found and the label waited
        import order_flow
        g = mock.Mock(client_number=lambda: 1)
        for filler in ("-", " - ", ".", "/"):
            o = self.order(); o["shipping"].update(address1="Kossuth Lajos utca 5", address2=filler)
            d = order_flow.gls_parcel(o, g)["DeliveryAddress"]
            self.assertEqual((d["Street"], d["HouseNumber"]), ("Kossuth Lajos utca", "5"), filler)
        o = self.order(); o["shipping"].update(address1="Kossuth Lajos utca", address2="-")  # really no number: still flagged
        self.assertEqual(order_flow.gls_parcel(o, g)["DeliveryAddress"]["HouseNumber"], "")
        o = self.order(); o["shipping"].update(address1="Kossuth Lajos utca", address2="5")  # a real address2 still counts
        self.assertEqual(order_flow.gls_parcel(o, g)["DeliveryAddress"]["HouseNumber"], "5")
        self.assertEqual(order_flow.phone("+43 660 1234"), "+436601234")
    def test_dry_run_sends_nothing_and_cod(self):
        import order_flow
        gls = mock.Mock(client_number=lambda: 123, prepare_labels=mock.Mock(return_value=[{"parcel_id": 1}]))
        shop = mock.Mock(order_private=lambda i: self.order(gateways=["Cash on Delivery (COD)"]))
        out = order_flow.parcel_prepare("1", "no", shop=shop, gls=gls)
        self.assertTrue(out["dry_run"]); gls.prepare_labels.assert_not_called(); self.assertEqual(out["cod_amount"], 9000 + 1200)  # 2 x 5000 - 1000 discount + shipping
        self.assertNotIn("Teszt", json.dumps(out))
        out = order_flow.parcel_prepare("1", "yes", shop=shop, gls=gls)
        sent = gls.prepare_labels.call_args.args[0][0]
        self.assertEqual(sent["DeliveryAddress"]["HouseNumber"], "12/B"); self.assertEqual(sent["ClientReference"], "#5042"); self.assertGreater(sent["CODAmount"], 0)
        self.assertNotIn("Teszt", json.dumps(out))
    def test_cod_must_match_the_order_total(self):
        import order_flow
        g = mock.Mock(client_number=lambda: 1)
        ok = self.order(gateways=["Cash on Delivery (COD)"], total="10200.00")
        self.assertEqual(order_flow.gls_parcel(ok, g)["CODAmount"], 10200)
        with self.assertRaises(ConnectorError):  # a live case: the label said more than the customer owed
            order_flow.gls_parcel(self.order(gateways=["Cash on Delivery (COD)"], total="9000.00"), g)
        self.assertEqual(order_flow.gls_parcel(self.order(total="1.00"), g)["CODAmount"], 0)  # PAID: nothing to collect, no check
    def test_automatic_discount_is_taken_off(self):
        # Shopify puts an automatic discount only in discount_allocations; total_discount stays 0 (seen live 2026-10-21)
        raw = {"order": {"name": "#5031", "id": 5, "total_price": "10800.00", "payment_gateway_names": ["Vásárlás Utánvétellel"],
                         "line_items": [{"title": "Fa vonat", "quantity": 1, "price": "4000.00", "total_discount": "0.00", "discount_allocations": [{"amount": "400.00"}]},
                                        {"title": "Fa kirakó", "quantity": 1, "price": "6500.00", "total_discount": "0.00", "discount_allocations": [{"amount": "800.00"}]}],
                         "shipping_lines": [{"title": "GLS", "price": "1500.00", "discount_allocations": []}]}}
        os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_ADMIN_TOKEN="tok")
        m = load("shopify")
        with mock.patch.object(m, "_get", return_value=raw): o = m.order_private("5")
        o["shipping"] = self.order()["shipping"]
        import order_flow
        self.assertEqual(order_flow.gls_parcel(o, mock.Mock(client_number=lambda: 1))["CODAmount"], 10800)
    def test_label_note_lists_the_products(self):
        import order_flow
        lines = [{"title": "Fa kirakó", "qty": 1, "price": "6500", "discount": "0"}, {"title": "Építőkocka", "qty": 2, "price": "3000", "discount": "0"}]
        o = self.order(lines=lines)
        paid = "example-shop.hu #5042, 1× Fa kirakó, 2× Építőkocka, PAID"
        self.assertEqual(order_flow.gls_parcel(o, mock.Mock(client_number=lambda: 1))["Content"], paid)
        shop = mock.Mock(order_private=lambda i: o)
        self.assertEqual(order_flow.parcel_prepare("1", "no", shop=shop, gls=mock.Mock(client_number=lambda: 1))["content"], paid)
        cod = self.order(lines=lines, gateways=["Cash on Delivery (COD)"])
        self.assertEqual(order_flow.gls_parcel(cod, mock.Mock(client_number=lambda: 1))["Content"],
                         "example-shop.hu #5042, 1× Fa kirakó, 2× Építőkocka, COD 13700 Ft")  # 6500 + 2 x 3000 + 1200 shipping
        long = order_flow.gls_content(self.order(lines=[{"title": "X" * 300, "qty": 1}]))
        self.assertEqual(len(long), order_flow.CONTENT_MAX); self.assertTrue(long.endswith("…"))
        self.assertEqual(order_flow.gls_content(self.order(lines=[])), "example-shop.hu #5042, PAID")
    def test_manual_parcel_for_other_channels(self):
        import tempfile, parcel_manual
        gls = mock.Mock(client_number=lambda: 1, prepare_labels=mock.Mock(return_value=[{"parcel_id": 7}]))
        data = dict(channel="eMAG", ref="113", goods="1× Plüss maci", cod="7350", name="Secret Name", zip="9998", city="Mintafalu",
                    street="Fő tér", house="1", phone="06701234567", email="s@x.hu")
        with tempfile.TemporaryDirectory() as d, mock.patch("os.environ", {**os.environ, "GLS_PICKUP_NAME": "Shop", "GLS_PICKUP_STREET": "Fő u.",
                                                                           "GLS_PICKUP_CITY": "Bp", "GLS_PICKUP_ZIP": "1011"}):
            flow = parcel_manual._flow(); flow.UPLOADED = pathlib.Path(d) / "up.json"; flow._load = lambda n: gls
            with mock.patch.object(parcel_manual, "_flow", lambda: flow):
                dry = parcel_manual.prepare(**data)
                self.assertTrue(dry["dry_run"]); gls.prepare_labels.assert_not_called()
                self.assertEqual(dry["content"], "example-shop.hu 113, 1× Plüss maci, COD 7350 Ft"); self.assertNotIn("Secret", json.dumps(dry))
                out = parcel_manual.prepare(**data, send="yes")
                sent = gls.prepare_labels.call_args.args[0][0]
                self.assertEqual(sent["CODAmount"], 7350); self.assertEqual(sent["DeliveryAddress"]["HouseNumber"], "1"); self.assertNotIn("Secret", json.dumps(out))
                self.assertIn("skipped", parcel_manual.prepare(**data, send="yes"))  # one reference, one parcel
                with self.assertRaises(ConnectorError): parcel_manual.prepare(**{**data, "house": ""})
                self.assertEqual(parcel_manual.prepare(**{**data, "ref": "114", "cod": "0"})["content"], "example-shop.hu 114, 1× Plüss maci, PAID")
    def test_gls_errors_surface(self):
        m = load("gls")
        with mock.patch("requests.post", return_value=Resp({"PrepareLabelsError": [{"ErrorCode": 9, "ErrorDescription": "bad zip"}]})):
            with self.assertRaises(ConnectorError): m.prepare_labels([{}])
    def test_hash_password(self):
        import hashlib; m = load("gls")
        self.assertEqual(m._auth()["Password"], list(hashlib.sha512(b"p").digest()))


class StreetSplit(unittest.TestCase):
    """How customers really type the address (shapes only, made-up streets). The house number is the number after the street's
    words; a number inside the street's name stays in the name; when it cannot be told apart, there is no number and a reason."""
    def setUp(self):
        import order_flow; self.f = order_flow
    def split(self, a): p = self.f.parse_street(a); return (p["street"], p["house"], p["info"])
    def test_number_glued_to_the_street(self):
        cases = {"Kossuth Lajos u.12.": ("Kossuth Lajos u.", "12", ""),   # #1047 shape: 'A A A.9.', address2 '-'
                 "Fő utca12": ("Fő utca", "12", ""), "Fő út 3/A": ("Fő út", "3/A", ""), "Teréz krt.5": ("Teréz krt.", "5", ""),
                 "Fő tér 7.": ("Fő tér", "7", ""), "Kossuth L.u.12": ("Kossuth L.u.", "12", ""), "Fő u.12/B": ("Fő u.", "12/B", ""),
                 "Petőfi S. u 3": ("Petőfi S. u", "3", ""), "Rákóczi út 12-14": ("Rákóczi út", "12-14", ""),
                 "Fő utca 12 B": ("Fő utca", "12 B", ""), "Fő utca 12, ": ("Fő utca", "12", ""),
                 "Fő u.3. -": ("Fő u.", "3", "")}  # address2 "-" joined on by hand (shopify ops)
        for a, want in cases.items(): self.assertEqual(self.split(a), want, a)
    def test_floor_and_door_after_the_number(self):
        cases = {"Fő u. 12. 2. em. 5.": ("Fő u.", "12", "2. em. 5."), "Fő u. 12 fsz. 1": ("Fő u.", "12", "fsz. 1"),
                 "Fő tér 3. 2. em.": ("Fő tér", "3", "2. em."), "Fő utca 12, 2. emelet 5. ajtó": ("Fő utca", "12", "2. emelet 5. ajtó"),
                 "Fő utca 12 III. em 5": ("Fő utca", "12", "III. em 5"), "Fő utca 7 B ép. 2. lh": ("Fő utca", "7 B", "ép. 2. lh"),
                 "Fő u. 12.2.em.5.": ("Fő u.", "12", "2.em. 5.")}
        for a, want in cases.items(): self.assertEqual(self.split(a), want, a)
        self.assertEqual(self.f.split_street("Fő u. 12. 2. em. 5."), ("Fő u.", "12. 2. em. 5."))  # split_street keeps it as written
    def test_a_number_in_the_street_name_stays_in_the_name(self):
        cases = {"1848. március 15. tér 4": ("1848. március 15. tér", "4", ""), "IV. Béla út 2": ("IV. Béla út", "2", ""),
                 "Május 1. utca 3": ("Május 1. utca", "3", ""), "Május 1. utca 3/A 1. em 2": ("Május 1. utca", "3/A", "1. em 2"),
                 "Negyvennyolcas 48as utca 12/B": ("Negyvennyolcas 48as utca", "12/B", ""),
                 "1111 Budapest, Fő utca 12": ("1111 Budapest, Fő utca", "12", "")}
        for a, want in cases.items(): self.assertEqual(self.split(a), want, a)
    def test_the_word_for_house_number(self):
        for a in ("Fő utca hsz. 12", "Fő utca hsz 12", "Fő utca házszám: 12", "Fő utca 12. sz.", "Fő utca 12 szám", "Fő utca szám 12"):
            self.assertEqual(self.split(a), ("Fő utca", "12", ""), a)
    def test_no_house_number_says_why(self):
        p = self.f.parse_street
        self.assertEqual((p("Fő utca")["house"], p("Fő utca")["why"]), ("", "nincs szám a címben"))
        self.assertIn("utcanév része", p("Május 1. utca")["why"]); self.assertIn("utcanév része", p("Negyvennyolcas 48as utca")["why"])
        self.assertEqual(p("Május 1. utca")["street"], "Május 1. utca")
        for a in ("Fő utca 12 14", "Fő u. 12. 3/5", "Fő utca 12/B 3/5"):
            self.assertEqual(p(a)["house"], "", a); self.assertIn("nem egyértelmű", p(a)["why"], a)
        self.assertIn("utcanév nincs", p("12")["why"]); self.assertEqual(p("")["why"], "üres cím")
        self.assertEqual(self.f.split_street("Fő utca 12 14"), ("Fő utca 12 14", ""))
        self.assertIn("nem egyértelmű", p("Fő utca 12 II/5")["why"])  # a floor/door with no floor word: STOP, not a guess
    def test_address2(self):
        pa = lambda a1, a2: (lambda r: (r["street"], r["house"], r["info"]))(self.f.parse_address({"address1": a1, "address2": a2}))
        self.assertEqual(pa("Kossuth Lajos u.12.", "-"), ("Kossuth Lajos u.", "12", ""))         # #1047: placeholder address2
        self.assertEqual(pa("Fő utca", "12"), ("Fő utca", "12", ""))                             # house number typed into address2
        self.assertEqual(pa("Fő utca", "12. 2. em. 5."), ("Fő utca", "12", "2. em. 5."))
        self.assertEqual(pa("Fő utca", "hsz. 12"), ("Fő utca", "12", ""))
        self.assertEqual(pa("Fő utca 12", "2. em. 5."), ("Fő utca", "12", "2. em. 5."))          # floor / door in address2
        self.assertEqual(pa("Fő utca 12", "3/5"), ("Fő utca", "12", "3/5"))                      # in address2 '3/5' is the flat, not a 2nd number
        self.assertEqual(pa("Fő utca 12.", "fsz 1"), ("Fő utca", "12", "fsz 1"))
        self.assertEqual(pa("Fő utca", "-"), ("Fő utca", "", ""))
        self.assertEqual(pa("Fő utca", None), ("Fő utca", "", ""))
        self.assertIn("nem egyértelmű", self.f.parse_address({"address1": "Fő utca", "address2": "12 3/5"})["why"])
    def test_label_gets_number_and_floor_apart(self):
        g = mock.Mock(client_number=lambda: 1)
        o = Gls().order(); o["shipping"].update(address1="Kossuth Lajos u.12.", address2="-")
        d = self.f.gls_parcel(o, g)["DeliveryAddress"]
        self.assertEqual((d["Street"], d["HouseNumber"], d["HouseNumberInfo"]), ("Kossuth Lajos u.", "12", ""))
        o = Gls().order(); o["shipping"].update(address1="Fő u. 12. 2. em. 5.", address2="")
        d = self.f.gls_parcel(o, g)["DeliveryAddress"]
        self.assertEqual((d["Street"], d["HouseNumber"], d["HouseNumberInfo"]), ("Fő u.", "12", "2. em. 5."))
        d = self.f.gls_parcel(o, g, house="14", info="")["DeliveryAddress"]  # a number given by hand wins, and the found floor is not mixed in
        self.assertEqual((d["HouseNumber"], d["HouseNumberInfo"]), ("14", ""))
        d = self.f.gls_parcel(o, g, info="3. ajtó")["DeliveryAddress"]       # info given by hand wins over the found one
        self.assertEqual((d["HouseNumber"], d["HouseNumberInfo"]), ("12", "3. ajtó"))


class GlsDedupe(unittest.TestCase):
    def test_engine_sent_and_no_second_upload(self):
        import order_flow, tempfile
        os.environ.update(GLS_CLIENT_NUMBER="1", GLS_USERNAME="u", GLS_PASSWORD="p")
        with mock.patch("requests.post", return_value=Resp({"ParcelInfoList": [{"ParcelId": 7, "ClientReference": "#1"}]})) as post:
            load("gls").prepare_labels([{}])
        self.assertEqual(post.call_args.kwargs["json"]["WebshopEngine"], "Shopify")
        order_flow.UPLOADED = pathlib.Path(tempfile.mkdtemp()) / "up.json"
        g = Gls(); g.setUp()
        gls = mock.Mock(client_number=lambda: 1, prepare_labels=mock.Mock(return_value=[{"parcel_id": 7}]))
        shop = mock.Mock(order_private=lambda i: g.order())
        order_flow.parcel_prepare("1", "yes", shop=shop, gls=gls); second = order_flow.parcel_prepare("1", "yes", shop=shop, gls=gls)
        self.assertEqual(gls.prepare_labels.call_count, 1); self.assertIn("skipped", second)


class GlsReplace(unittest.TestCase):
    def test_correction_deletes_then_uploads(self):
        import order_flow, tempfile
        g = Gls(); g.setUp()
        order_flow.UPLOADED = pathlib.Path(tempfile.mkdtemp()) / "up.json"
        order_flow.UPLOADED.write_text(json.dumps({"#5042": 555}))
        gls = mock.Mock(client_number=lambda: 1, delete_labels=mock.Mock(return_value=True), prepare_labels=mock.Mock(return_value=[{"parcel_id": 556}]))
        shop = mock.Mock(order_private=lambda i: g.order(shipping={"name": "T E", "zip": "1011", "city": "Budapest", "address1": "Molnár utca", "country_code": "HU"}))
        skipped = order_flow.parcel_prepare("1", "yes", shop=shop, gls=gls); self.assertIn("skipped", skipped); gls.delete_labels.assert_not_called()
        out = order_flow.parcel_prepare("1", "yes", "13", "3. emelet 16. ajtó", "yes", shop, gls)
        gls.delete_labels.assert_called_once_with([555])
        sent = gls.prepare_labels.call_args.args[0][0]["DeliveryAddress"]
        self.assertEqual((sent["HouseNumber"], sent["HouseNumberInfo"]), ("13", "3. emelet 16. ajtó"))
        rec = json.loads(order_flow.UPLOADED.read_text())["#5042"]
        self.assertEqual((rec["parcel_id"], rec["cod_amount"]), (556, 0)); self.assertTrue(rec["at"]); self.assertNotIn("Molnár", json.dumps(out))
        # a later correction of a parcel recorded in the new format deletes the right id
        gls.delete_labels.reset_mock(); gls.prepare_labels.return_value = [{"parcel_id": 557}]
        order_flow.parcel_prepare("1", "yes", "13", "", "yes", shop, gls)
        gls.delete_labels.assert_called_once_with([556])
    def test_cod_amount_recorded_for_the_site(self):
        import order_flow, tempfile
        g = Gls(); g.setUp()
        order_flow.UPLOADED = pathlib.Path(tempfile.mkdtemp()) / "up.json"
        gls = mock.Mock(client_number=lambda: 1, prepare_labels=mock.Mock(return_value=[{"parcel_id": 9}]))
        shop = mock.Mock(order_private=lambda i: g.order(gateways=["Cash on Delivery (COD)"]))
        order_flow.parcel_prepare("1", "yes", shop=shop, gls=gls)
        self.assertEqual(json.loads(order_flow.UPLOADED.read_text())["#5042"]["cod_amount"], 9000 + 1200)
    def test_failed_delete_uploads_nothing(self):
        import order_flow, tempfile
        g = Gls(); g.setUp()
        order_flow.UPLOADED = pathlib.Path(tempfile.mkdtemp()) / "up.json"; order_flow.UPLOADED.write_text(json.dumps({"#5042": 555}))
        gls = mock.Mock(client_number=lambda: 1, delete_labels=mock.Mock(side_effect=ConnectorError("printed")), prepare_labels=mock.Mock())
        with self.assertRaises(ConnectorError): order_flow.parcel_prepare("1", "yes", "9", "", "yes", mock.Mock(order_private=lambda i: g.order()), gls)
        gls.prepare_labels.assert_not_called()


class Issue(unittest.TestCase):
    def setUp(self):
        import order_flow, tempfile
        order_flow.ISSUED = pathlib.Path(tempfile.mkdtemp()) / "inv.json"
        self.bill = mock.Mock(document=lambda i: {"type": "draft", "comment": "Shopify #5042", "gross_total": 13900, "partner": {"id": 5}},
                              partner_email=lambda p: "a@b.hu", create_from_draft=mock.Mock(return_value={"id": 77, "invoice_number": "A-2026-9"}),
                              send_by_email=mock.Mock(return_value=True))
    def test_dry_run_changes_nothing(self):
        import order_flow
        out = order_flow.invoice_issue(1, bill=self.bill); self.assertTrue(out["dry_run"]); self.bill.create_from_draft.assert_not_called()
        self.assertNotIn("a@b.hu", json.dumps(out))
    def test_issue_and_send_once(self):
        import order_flow
        out = order_flow.invoice_issue(1, "yes", "yes", bill=self.bill)
        self.assertEqual(out, {"invoice": "A-2026-9", "sent": True}); self.bill.send_by_email.assert_called_once_with(77, ["a@b.hu"])
        again = order_flow.invoice_issue(1, "yes", "yes", bill=self.bill)
        self.assertIn("skipped", again); self.assertEqual(self.bill.create_from_draft.call_count, 1); self.assertEqual(self.bill.send_by_email.call_count, 1)
    def test_issue_then_send_later_does_not_issue_twice(self):
        import order_flow
        order_flow.invoice_issue(1, "yes", "no", bill=self.bill)
        order_flow.invoice_issue(1, "yes", "yes", bill=self.bill)
        self.assertEqual(self.bill.create_from_draft.call_count, 1); self.assertEqual(self.bill.send_by_email.call_count, 1)
    def test_paid_order_records_payment_once(self):
        import order_flow
        self.bill.document = lambda i: {"type": "draft", "comment": "Shopify #5042", "gross_total": 13900, "payment_method": "bankcard", "partner": {"id": 5}}
        out = order_flow.invoice_issue(1, "yes", "yes", "yes", bill=self.bill)
        self.assertEqual(out, {"invoice": "A-2026-9", "paid": True, "sent": True})
        self.bill.mark_paid.assert_called_once_with(77, 13900, "bankcard")
        self.assertIn("skipped", order_flow.invoice_issue(1, "yes", "yes", "yes", bill=self.bill)); self.assertEqual(self.bill.mark_paid.call_count, 1)
    def test_unpaid_order_is_not_marked_paid(self):
        import order_flow
        order_flow.invoice_issue(1, "yes", "yes", bill=self.bill); self.bill.mark_paid.assert_not_called()
    def test_not_a_draft(self):
        import order_flow
        self.bill.document = lambda i: {"type": "invoice", "partner": {"id": 5}}
        with self.assertRaises(ConnectorError): order_flow.invoice_issue(1, "yes", bill=self.bill)
