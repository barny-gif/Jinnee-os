"""Connectors + orders panel, with fake HTTP. Run: python -m unittest discover tests"""
import importlib.util, json, os, pathlib, sys, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "dashboard"))
import orders
from cbase import ConnectorError

def load(n):
    s = importlib.util.spec_from_file_location("t_" + n, ROOT / "connectors" / n / "ops.py"); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

class Resp:
    def __init__(self, data, code=200): self.data, self.status_code, self.ok, self.text, self.content = data, code, code < 400, "", b"%PDF"
    def json(self): return self.data

ORDER = {"id": 1, "name": "#1042", "created_at": "2026-10-10T10:00:00Z", "total_price": "12990", "currency": "HUF", "financial_status": "paid",
         "fulfillment_status": None, "line_items": [{"quantity": 2}], "fulfillments": [], "email": "x@y.hu", "shipping_address": {"country_code": "HU", "name": "Secret"}}

class Shop(unittest.TestCase):
    def setUp(self): os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_ADMIN_TOKEN="t"); self.m = load("shopify")
    def test_order_has_no_personal_data(self):
        with mock.patch("requests.get", return_value=Resp({"orders": [ORDER]})):
            out = self.m.list_orders()
        self.assertEqual(out[0]["name"], "#1042"); self.assertNotIn("Secret", json.dumps(out)); self.assertNotIn("x@y.hu", json.dumps(out))
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
        with mock.patch("requests.request", return_value=Resp({"id": 5, "type": "invoice", "invoice_number": "MV-2026-1", "notification_status": "opened"})):
            self.assertEqual(self.m.status("5")["email"], "opened")
    DRAFT = {"id": 9, "type": "draft", "block_id": 3, "partner": {"id": 5, "name": "Secret"}, "fulfillment_date": "2026-10-13", "due_date": "2020-01-01",
             "payment_method": "bankcard", "language": "hu", "currency": "HUF", "conversion_rate": 1, "electronic": False, "comment": "Shopify #1042",
             "gross_total": 15282, "items": [{"name": "Kalandkönyv", "quantity": 2, "unit": "db", "vat": "AAM", "gross_amount": 13392, "net_amount": 13392},
                                             {"name": "Szállítás", "quantity": 1, "unit": "db", "vat": "AAM", "gross_amount": 1890}]}
    def test_issue_from_draft_puts_the_whole_document(self):
        with mock.patch("requests.request", side_effect=[Resp(self.DRAFT), Resp({"id": 9, "invoice_number": "MV-2026-1"})]) as r:
            out = self.m.create_from_draft(9)
        self.assertEqual(out["invoice_number"], "MV-2026-1"); self.assertEqual(r.call_count, 2)
        put = r.call_args_list[1]; body = put.kwargs["json"]
        self.assertEqual(put.args[:2], ("PUT", self.m.BASE + "/documents/9")); self.assertEqual(body["type"], "invoice")
        self.assertEqual((body["partner_id"], body["block_id"], body["payment_method"], body["comment"]), (5, 3, "bankcard", "Shopify #1042"))
        self.assertEqual(body["items"][0], {"name": "Kalandkönyv", "unit_price": 6696.0, "unit_price_type": "gross", "quantity": 2.0, "unit": "db", "vat": "AAM"})
        self.assertGreaterEqual(body["due_date"], __import__("datetime").date.today().isoformat()); self.assertNotIn("Secret", json.dumps(body))
    def test_issue_leaves_out_empty_settings(self):
        draft = {**self.DRAFT, "settings": {"place_id": None, "dont_send_to_nav_reason": "", "round": "one", "without_financial_fulfillment": False}}
        with mock.patch("requests.request", side_effect=[Resp(draft), Resp({"id": 9, "invoice_number": "MV-2026-1"})]) as r:
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
        shop = mock.Mock(list_orders=lambda days: [dict(load("shopify")._order(ORDER)), dict(load("shopify")._order({**ORDER, "id": 2, "name": "#1043"}))])
        inv = mock.Mock(list_invoices=lambda days: [{"number": "A-1", "paid": "paid", "comment": "Shopify #1042", "total": 1, "currency": "HUF", "id": 1, "partner": "", "date": "", "type": "invoice"}])
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
        o = {"name": "#1042", "id": 1, "currency": "HUF", "taxes_included": True, "gateways": ["Bank deposit"], "email": "a@b.hu", "phone": "",
             "billing": {"name": "Teszt Elek", "zip": "9970", "city": "Szentgotthárd", "address1": "Fő u. 1", "country_code": "HU"}, "shipping": {},
             "lines": [{"title": "Fa puzzle", "sku": "P1", "qty": 2, "price": "5000", "discount": "1000"}], "shipping_lines": [{"title": "GLS", "price": "1490"}]}
        o.update(kw); return o

    def run_flow(self, order, existing=()):
        import order_flow
        shop = mock.Mock(order_private=lambda i: order)
        bill = mock.Mock(list_invoices=lambda days: list(existing), create_partner=mock.Mock(return_value=7), block_id=lambda: 3, post_document=mock.Mock(return_value={"id": 99, "type": "draft"}))
        return order_flow.invoice_draft("1", shop, bill), bill

    def test_draft_body(self):
        out, bill = self.run_flow(self.order())
        body = bill.post_document.call_args.args[0]
        self.assertEqual(out["draft_id"], 99); self.assertEqual(body["type"], "draft"); self.assertEqual(body["comment"], "Shopify #1042")
        self.assertEqual(body["items"][0]["unit_price"], 4500.0); self.assertEqual(body["items"][1]["unit_price"], 1490.0)
        self.assertNotIn("Teszt", json.dumps(out)); self.assertEqual(body["payment_method"], "wire_transfer")
    def test_cod(self):
        _, bill = self.run_flow(self.order(gateways=["Cash on Delivery (COD)"]))
        self.assertEqual(bill.post_document.call_args.args[0]["payment_method"], "cash_on_delivery")
    def test_no_second_invoice(self):
        out, bill = self.run_flow(self.order(), existing=[{"number": "A-1", "id": 1, "comment": "Shopify #1042"}])
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
        os.environ.update(GLS_CLIENT_NUMBER="123", GLS_USERNAME="u", GLS_PASSWORD="p", GLS_PICKUP_NAME="Monti", GLS_PICKUP_STREET="Fő u.",
                          GLS_PICKUP_HOUSE="1", GLS_PICKUP_CITY="Szentgotthárd", GLS_PICKUP_ZIP="9970", GLS_PICKUP_PHONE="06301234567")
    def order(self, **kw):
        o = Flow().order(); o["shipping"] = {"name": "Teszt Elek", "zip": "1011", "city": "Budapest", "address1": "Kossuth Lajos utca 12/B", "country_code": "HU"}
        o["phone"] = "06 20 123 4567"; o.update(kw); return o
    def test_street_split_and_phone(self):
        import order_flow
        self.assertEqual(order_flow.split_street("Kossuth Lajos utca 12/B"), ("Kossuth Lajos utca", "12/B"))
        self.assertEqual(order_flow.split_street("Fő u. 1"), ("Fő u.", "1"))
        self.assertEqual(order_flow.phone("06 20 123 4567"), "+36201234567")
        self.assertEqual(order_flow.phone("+43 660 1234"), "+436601234")
    def test_dry_run_sends_nothing_and_cod(self):
        import order_flow
        gls = mock.Mock(client_number=lambda: 123, prepare_labels=mock.Mock(return_value=[{"parcel_id": 1}]))
        shop = mock.Mock(order_private=lambda i: self.order(gateways=["Cash on Delivery (COD)"]))
        out = order_flow.parcel_prepare("1", "no", shop=shop, gls=gls)
        self.assertTrue(out["dry_run"]); gls.prepare_labels.assert_not_called(); self.assertEqual(out["cod_amount"], 9000 + 1490)  # 2 x 5000 - 1000 discount + shipping
        self.assertNotIn("Teszt", json.dumps(out))
        out = order_flow.parcel_prepare("1", "yes", shop=shop, gls=gls)
        sent = gls.prepare_labels.call_args.args[0][0]
        self.assertEqual(sent["DeliveryAddress"]["HouseNumber"], "12/B"); self.assertEqual(sent["ClientReference"], "#1042"); self.assertGreater(sent["CODAmount"], 0)
        self.assertNotIn("Teszt", json.dumps(out))
    def test_label_note_lists_the_products(self):
        import order_flow
        lines = [{"title": "Farmer Kalandkönyv", "qty": 1, "price": "8490", "discount": "0"}, {"title": "Horgász játék", "qty": 2, "price": "3000", "discount": "0"}]
        o = self.order(lines=lines)
        self.assertEqual(order_flow.gls_parcel(o, mock.Mock(client_number=lambda: 1))["Content"], "#1042: 1× Farmer Kalandkönyv, 2× Horgász játék")
        shop = mock.Mock(order_private=lambda i: o)
        self.assertEqual(order_flow.parcel_prepare("1", "no", shop=shop, gls=mock.Mock(client_number=lambda: 1))["content"], "#1042: 1× Farmer Kalandkönyv, 2× Horgász játék")
        long = order_flow.gls_content(self.order(lines=[{"title": "X" * 300, "qty": 1}]))
        self.assertEqual(len(long), order_flow.CONTENT_MAX); self.assertTrue(long.endswith("…"))
        self.assertEqual(order_flow.gls_content(self.order(lines=[])), "Monti Világ #1042")
    def test_gls_errors_surface(self):
        m = load("gls")
        with mock.patch("requests.post", return_value=Resp({"PrepareLabelsError": [{"ErrorCode": 9, "ErrorDescription": "bad zip"}]})):
            with self.assertRaises(ConnectorError): m.prepare_labels([{}])
    def test_hash_password(self):
        import hashlib; m = load("gls")
        self.assertEqual(m._auth()["Password"], list(hashlib.sha512(b"p").digest()))


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
        order_flow.UPLOADED.write_text(json.dumps({"#1042": 555}))
        gls = mock.Mock(client_number=lambda: 1, delete_labels=mock.Mock(return_value=True), prepare_labels=mock.Mock(return_value=[{"parcel_id": 556}]))
        shop = mock.Mock(order_private=lambda i: g.order(shipping={"name": "T E", "zip": "1011", "city": "Budapest", "address1": "Molnár utca", "country_code": "HU"}))
        skipped = order_flow.parcel_prepare("1", "yes", shop=shop, gls=gls); self.assertIn("skipped", skipped); gls.delete_labels.assert_not_called()
        out = order_flow.parcel_prepare("1", "yes", "13", "3. emelet 16. ajtó", "yes", shop, gls)
        gls.delete_labels.assert_called_once_with([555])
        sent = gls.prepare_labels.call_args.args[0][0]["DeliveryAddress"]
        self.assertEqual((sent["HouseNumber"], sent["HouseNumberInfo"]), ("13", "3. emelet 16. ajtó"))
        rec = json.loads(order_flow.UPLOADED.read_text())["#1042"]
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
        self.assertEqual(json.loads(order_flow.UPLOADED.read_text())["#1042"]["cod_amount"], 9000 + 1490)
    def test_failed_delete_uploads_nothing(self):
        import order_flow, tempfile
        g = Gls(); g.setUp()
        order_flow.UPLOADED = pathlib.Path(tempfile.mkdtemp()) / "up.json"; order_flow.UPLOADED.write_text(json.dumps({"#1042": 555}))
        gls = mock.Mock(client_number=lambda: 1, delete_labels=mock.Mock(side_effect=ConnectorError("printed")), prepare_labels=mock.Mock())
        with self.assertRaises(ConnectorError): order_flow.parcel_prepare("1", "yes", "9", "", "yes", mock.Mock(order_private=lambda i: g.order()), gls)
        gls.prepare_labels.assert_not_called()


class Issue(unittest.TestCase):
    def setUp(self):
        import order_flow, tempfile
        order_flow.ISSUED = pathlib.Path(tempfile.mkdtemp()) / "inv.json"
        self.bill = mock.Mock(document=lambda i: {"type": "draft", "comment": "Shopify #1042", "gross_total": 15282, "partner": {"id": 5}},
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
        self.bill.document = lambda i: {"type": "draft", "comment": "Shopify #1042", "gross_total": 15282, "payment_method": "bankcard", "partner": {"id": 5}}
        out = order_flow.invoice_issue(1, "yes", "yes", "yes", bill=self.bill)
        self.assertEqual(out, {"invoice": "A-2026-9", "paid": True, "sent": True})
        self.bill.mark_paid.assert_called_once_with(77, 15282, "bankcard")
        self.assertIn("skipped", order_flow.invoice_issue(1, "yes", "yes", "yes", bill=self.bill)); self.assertEqual(self.bill.mark_paid.call_count, 1)
    def test_unpaid_order_is_not_marked_paid(self):
        import order_flow
        order_flow.invoice_issue(1, "yes", "yes", bill=self.bill); self.bill.mark_paid.assert_not_called()
    def test_not_a_draft(self):
        import order_flow
        self.bill.document = lambda i: {"type": "invoice", "partner": {"id": 5}}
        with self.assertRaises(ConnectorError): order_flow.invoice_issue(1, "yes", bill=self.bill)
