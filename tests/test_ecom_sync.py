"""Ecom sync v2 with fake HTTP and fake connectors. Run: python3 -m unittest discover tests"""
import importlib.util, json, os, pathlib, sys, tempfile, unittest
from unittest import mock
HERE = pathlib.Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "connectors" / "cbase.py").exists())
sys.path.insert(0, str(ROOT / "connectors"))
from cbase import ConnectorError

TRAFFIC = [{"day": "2026-10-11", "sessions": 10, "visitors": 10}, {"day": "2026-10-12", "sessions": 8, "visitors": 7}]

def load(traffic=True):
    path = next(p for p in (HERE / "ecom_sync.py", ROOT / "connectors" / "ecom_sync.py") if p.exists())
    s = importlib.util.spec_from_file_location("t_ecom_sync2", path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    tmp = pathlib.Path(tempfile.mkdtemp())
    m.UPLOADED, m.GLS_STATUS, m.FULFILLED = tmp / "up.json", tmp / "gls_status.json", tmp / "fulfilled.json"  # never the real brain/
    m.FULFILL_START = tmp / "fulfill_start.json"
    m.order_auto.STATE, m.order_auto.START = tmp / "order_auto.json", tmp / "order_auto_start.json"
    m.order_auto._gate = lambda action: 1  # the automatic chain has its own tests (test_order_auto.py); off here
    m._gls_fail.update(at=0.0, error=None)
    real = m._load
    m._load = lambda name: fake_gls() if name == "gls" else real(name)  # no test ever reaches MyGLS
    if traffic: m._traffic = lambda shop, days: list(TRAFFIC)  # the analytics call has its own tests below
    return m

def gls_module():
    s = importlib.util.spec_from_file_location("t_gls_ops", ROOT / "connectors" / "gls" / "ops.py"); g = importlib.util.module_from_spec(s); s.loader.exec_module(g)
    return g

def fake_gls(numbers=None, statuses=None, fail=None):
    """numbers: parcel id -> parcel number; statuses: parcel number -> statuses() list (newest first)."""
    g = gls_module()
    def parcel_numbers(days):
        if fail == "list": raise ConnectorError("[GLS parcel list] MyGLS did not answer: Timeout")
        return dict(numbers or {})
    def st(n):
        if fail == "status": raise ConnectorError("[GLS status] MyGLS did not answer: Timeout")
        return list((statuses or {}).get(n, []))
    return mock.Mock(parcel_numbers=mock.Mock(side_effect=parcel_numbers), statuses=mock.Mock(side_effect=st), classify=g.classify)

def S(text, at): return {"status": text, "at": at, "code": None}
DELIVERED = [S("Kiszállítva", "2026-10-14T07:30:38+00:00"), S("Csomag kiszállítás alatt", "2026-10-14T04:53:05+00:00"),
             S("Lehívás - felvett csomag", "2026-10-13T08:24:26+00:00"), S("Csomag rögzítve", "2026-10-12T11:40:30+00:00")]
HELD = [S("Depó tárolás", "2026-10-15T04:07:03+00:00"), S("Csomag a kiszállító depóban", "2026-10-14T03:44:42+00:00"),
        S("Lehívás - felvett csomag", "2026-10-13T08:24:28+00:00"), S("Utánvét adat rögzítve", "2026-10-12T08:56:03+00:00")]

class Traffic(unittest.TestCase):
    def setUp(self): self.m = load(traffic=False)
    def test_uses_the_sessions_op(self):
        s = shop(); self.m._traffic(s, "7"); s.sessions.assert_called_once_with("7")
    def test_unreadable_traffic_is_none_not_zero(self):
        self.m._traffic = mock.Mock(side_effect=ConnectorError("the app has no access to analytics: it needs the read_reports scope"))
        out = self.m.build(shop=shop(), bill=bill())
        self.assertIsNone(out["traffic"]); self.assertIsNone(out["visitors"])
        self.assertFalse(next(a for a in out["automations"] if a["key"] == "shopify_visitors")["ok"])
        self.assertEqual(len(out["orders"]), 1)  # the orders still go
    def test_visitors_today_and_last_7_days(self):
        days = [{"day": f"2026-10-{d:02d}", "sessions": d, "visitors": d - 1} for d in range(7, 16)]  # 9 days, 10-15 is today
        now = 1792044000  # 2026-10-15 08:00 Budapest
        with mock.patch.dict(os.environ, {"JINNEE_TZ": "Europe/Budapest"}):
            v = self.m._visitors(days, now)
        self.assertEqual(v["today"], {"sessions": 15, "visitors": 14})
        self.assertEqual(v["last_7_days"], {"sessions": sum(range(9, 16)), "visitors": sum(range(8, 15))})
        self.assertEqual([d["day"] for d in v["days"]][0], "2026-10-09"); self.assertEqual(len(v["days"]), 7)
        self.assertEqual(self.m._visitors([], now)["today"], {"sessions": 0, "visitors": 0})

class Resp:
    def __init__(self, data=None, code=200): self.data, self.status_code, self.ok = data or {}, code, code < 400
    def json(self): return self.data

ADDR = {"name": "Titkos Vevo", "address1": "Fo utca 1", "zip": "1111", "city": "Budapest", "country_code": "HU", "phone": "+36301112222",
        "latitude": 47.5, "first_name": "Titkos"}
ORDER = {"id": 6001, "name": "#1024", "created_at": "2026-10-11T10:00:00+02:00", "total_price": "12990.00", "currency": "HUF",
         "financial_status": "paid", "fulfillment_status": None, "cancelled_at": None, "test": False,
         "fulfillments": [{"tracking_numbers": ["T1"], "created_at": "2026-10-12T09:00:00+02:00"}],
         "payment_gateway_names": ["SimplePay"], "shipping_lines": [{"title": "GLS házhoz", "price": "1890.00"}],
         "line_items": [{"title": "Farmer Kalandkönyv", "variant_id": 77, "sku": "FK1", "quantity": 2, "price": "5550.00"}],
         "email": "x@y.hu", "note": "csengo: Kovacs", "shipping_address": ADDR, "billing_address": ADDR}
COD_ORDER = {**ORDER, "id": 6002, "name": "#1025", "financial_status": "pending", "fulfillments": [], "payment_gateway_names": ["Cash on Delivery (COD)"]}
PRODUCT = {"id": 9, "title": "Farmer Kalandkönyv", "status": "active", "variants": [{"id": 77, "title": "Default Title", "sku": "FK1", "price": "5550.00", "inventory_quantity": 4}]}

ALL_SCOPES = {"ok": True, "missing": [], "required": [], "scopes": ["read_orders"]}

def shop(orders=(ORDER,), products=(PRODUCT,), fail=None, scopes=ALL_SCOPES, fulfill=None):
    def _get(path, params=None):
        if fail and path.startswith(fail): raise ConnectorError("Shopify did not answer: Timeout")
        return {"orders": list(orders)} if path == "orders.json" else {"products": list(products)}
    def _scopes():
        if fail == "scopes": raise ConnectorError("[access_scopes] Shopify answered 500")
        return dict(scopes)
    def _fulfill(order_id, tracking, company="GLS", confirm="no"):
        if fulfill == "fail": raise ConnectorError("[fulfillments] Shopify answered 500")
        return {"order_id": order_id, "already": fulfill == "already", "created": fulfill != "already", "fulfillment_id": "F" + order_id, "tracking": tracking}
    return mock.Mock(_get=mock.Mock(side_effect=_get), scopes=mock.Mock(side_effect=_scopes), fulfill=mock.Mock(side_effect=_fulfill),
                     sessions=mock.Mock(return_value=list(TRAFFIC)))

def bill(docs=(), fail=False):
    def list_invoices(days):
        if fail: raise ConnectorError("Billingo did not answer")
        return list(docs)
    return mock.Mock(list_invoices=mock.Mock(side_effect=list_invoices))

class Payload(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_seven_days_and_no_note_asked(self):
        s = shop(); out = self.m.build(shop=s, bill=bill())
        params = s._get.call_args_list[0].args[1]
        self.assertEqual(out["window_days"], 7)
        self.assertNotIn("note", params["fields"].split(","))
        self.assertNotIn("Kovacs", json.dumps(out, ensure_ascii=False))
    def test_private_block_is_separate_and_trimmed(self):
        o = self.m.build(shop=shop(), bill=bill())["orders"][0]
        self.assertEqual(o["private"]["customer_name"], "Titkos Vevo")
        self.assertEqual(o["private"]["email"], "x@y.hu")
        self.assertEqual(set(o["private"]["shipping_address"]), {"name", "address1", "zip", "city", "country_code", "phone"})
        public = json.dumps({k: v for k, v in o.items() if k != "private"}, ensure_ascii=False)
        for secret in ("x@y.hu", "Titkos", "+3630", "Fo utca"): self.assertNotIn(secret, public)
    def test_paid_and_cod(self):
        paid, cod = self.m.build(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill())["orders"]
        self.assertEqual((paid["paid"], paid["is_cod"], paid["payment_method"]), ("paid", False, "SimplePay"))
        self.assertEqual((cod["paid"], cod["is_cod"]), ("pending", True))
        self.assertEqual(paid["shipping_method"], "GLS házhoz")
        self.assertEqual(paid["shipped_at"], "2026-10-12T09:00:00+02:00")
        self.assertIsNone(cod["shipped_at"])
    def test_label_old_entry_has_no_cod_amount(self):
        self.m.UPLOADED.write_text(json.dumps({"#1024": 555, "#1025": True}))
        a, b = self.m.build(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill())["orders"]
        self.assertEqual(a["label"], {"state": "uploaded", "parcel_id": "555"})
        self.assertEqual(b["label"], {"state": "uploaded", "parcel_id": None})
    def test_label_new_entry_carries_cod_amount(self):
        self.m.UPLOADED.write_text(json.dumps({"#1024": {"parcel_id": 555, "cod_amount": 0, "at": "2026-10-12T08:00:00+02:00"},
                                               "#1025": {"parcel_id": 556, "cod_amount": 12990, "at": "2026-10-12T08:01:00+02:00"}}))
        a, b = self.m.build(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill())["orders"]
        self.assertEqual(a["label"], {"state": "uploaded", "parcel_id": "555", "cod_amount": 0.0, "sent_at": "2026-10-12T08:00:00+02:00"})
        self.assertEqual(b["label"]["cod_amount"], 12990.0)
    def test_not_uploaded(self):
        self.assertEqual(self.m.build(shop=shop(), bill=bill())["orders"][0]["label"], {"state": "none", "parcel_id": None})
    def test_invoice_number_does_not_match_longer_order(self):
        docs = [{"comment": "Rendelés #10245", "type": "invoice", "number": "2026-9", "paid": True}]
        self.assertEqual(self.m.build(shop=shop(), bill=bill(docs))["orders"][0]["invoice"]["state"], "none")
    def test_billingo_down_is_unknown_and_reported(self):
        out = self.m.build(shop=shop(), bill=bill(fail=True))
        self.assertEqual(out["orders"][0]["invoice"]["state"], "unknown")
        self.assertFalse(next(a for a in out["automations"] if a["key"] == "billingo")["ok"])
    def test_billingo_asked_once_in_five_minutes(self):
        b = bill()
        self.m.build(shop=shop(), bill=b, now=1000); self.m.build(shop=shop(), bill=b, now=1100)
        self.assertEqual(b.list_invoices.call_count, 1)
        self.m.build(shop=shop(), bill=b, now=1400)
        self.assertEqual(b.list_invoices.call_count, 2)
    def test_billingo_failure_is_not_cached(self):
        self.m.build(shop=shop(), bill=bill(fail=True), now=1000)
        ok = bill([{"comment": "#1024", "type": "invoice", "number": "2026-1", "paid": True}])
        self.assertEqual(self.m.build(shop=shop(), bill=ok, now=1010)["orders"][0]["invoice"]["state"], "issued")
    def test_shopify_down_sends_none_not_empty(self):
        out = self.m.build(shop=shop(fail="orders"), bill=bill())
        self.assertIsNone(out["orders"]); self.assertTrue(out["errors"])

class GlsClassify(unittest.TestCase):
    def setUp(self): self.g = gls_module()
    def test_states_from_live_texts(self):
        c = self.g.classify
        self.assertEqual(c(DELIVERED)["state"], "delivered"); self.assertEqual(c(DELIVERED)["delivered_at"], "2026-10-14T07:30:38+00:00")
        self.assertEqual(c(DELIVERED)["handed_at"], "2026-10-13T08:24:26+00:00")
        self.assertEqual(c(DELIVERED[1:])["state"], "out_for_delivery")
        self.assertEqual(c(DELIVERED[2:])["state"], "handed_to_gls")
        self.assertEqual(c(DELIVERED[3:])["state"], "uploaded"); self.assertIsNone(c(DELIVERED[3:])["handed_at"])
        self.assertEqual(c(HELD)["state"], "problem"); self.assertIsNone(c(HELD)["delivered_at"])
        self.assertEqual(c(HELD[1:])["state"], "in_transit")
        self.assertEqual(c([S("Visszaküldve a feladónak", "x")] + DELIVERED[1:])["state"], "returned")
        self.assertEqual(c([])["state"], "uploaded")
    def test_net_date(self):
        self.assertEqual(self.g._date("/Date(1791963038000+0200)/"), "2026-10-14T07:30:38+00:00"); self.assertIsNone(self.g._date(None))
    def test_parcel_list_keeps_only_ids_and_numbers(self):
        body = {"GetParcelListErrors": [], "PrintDataInfoList": [{"ParcelId": 549107707, "ParcelNumber": 3424008826, "ClientReference": "#1025",
                                                                  "Parcel": {"DeliveryAddress": {"Name": "Titkos Vevo"}}}, {"ParcelId": 1, "ParcelNumber": None}]}
        with mock.patch.dict(os.environ, {"GLS_CLIENT_NUMBER": "1", "GLS_USERNAME": "u", "GLS_PASSWORD": "p"}), \
             mock.patch.object(self.g.requests, "post", return_value=Resp(body)) as post:
            self.assertEqual(self.g.parcel_numbers("30", now=1_000_000), {"549107707": "3424008826"})
        sent = post.call_args.kwargs["json"]
        self.assertTrue(post.call_args.args[0].endswith("/GetParcelList"))
        self.assertEqual((sent["PrintDateFrom"], sent["PrintDateTo"]), ("/Date(-1592000000)/", "/Date(1000000000)/"))
    def test_status_error_names_the_step(self):
        with mock.patch.dict(os.environ, {"GLS_CLIENT_NUMBER": "1", "GLS_USERNAME": "u", "GLS_PASSWORD": "p"}), \
             mock.patch.object(self.g.requests, "post", return_value=Resp({"GetParcelStatusErrors": [{"ErrorCode": 26, "ErrorDescription": "Parcel not found"}]})):
            with self.assertRaisesRegex(ConnectorError, r"\[GLS status\].*26"): self.g.statuses("1")

class GlsTracking(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.m.UPLOADED.write_text(json.dumps({"#1024": {"parcel_id": 548860860, "cod_amount": 12990}, "#1025": 549107707}))
        self.gls = fake_gls({"548860860": "3423946814", "549107707": "3424008826"}, {"3423946814": HELD, "3424008826": DELIVERED})
    def run_build(self, now=10_000, gls=None, docs=()):
        return self.m.build(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill(docs), now=now, gls=gls or self.gls)
    def test_state_goes_to_site_and_brain(self):
        paid, cod = self.run_build()["orders"]  # ORDER is #1024 (here held at the depot), COD_ORDER is #1025 (delivered)
        self.assertEqual(paid["shipping"]["state"], "problem"); self.assertEqual(paid["delivered"], {"state": "no", "at": None})
        self.assertEqual(cod["delivered"], {"state": "yes", "at": "2026-10-14T07:30:38+00:00"})
        self.assertEqual(cod["shipping"]["parcel_number"], "3424008826")
        stored = json.loads(self.m.GLS_STATUS.read_text())
        self.assertEqual(stored["#1025"]["state"], "delivered"); self.assertEqual(stored["#1024"]["parcel_id"], "548860860")
    def test_cod_delivered_without_invoice_is_flagged_only(self):
        paid, cod = self.run_build()["orders"]
        self.assertTrue(cod["cod_invoice_due"]); self.assertFalse(paid["cod_invoice_due"])
        _, cod = self.m.build(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill([{"comment": "#1025", "type": "invoice", "number": "MV-2026-9"}]),
                              now=10_000 + 3600, gls=self.gls)["orders"]
        self.assertFalse(cod["cod_invoice_due"])
    def test_cod_held_at_depot_code_07_is_picked_up_but_no_invoice(self):
        """Live 2026-10-15: #1024 COD, newest status 07 'Depó tárolás' after 86 'Lehívás - felvett csomag'."""
        held = [{**S("Depó tárolás", "2026-10-15T04:07:03+00:00"), "code": "07"}, {**S("Meghat. időpontra történő kisz", "2026-10-14T05:34:03+00:00"), "code": "09"},
                {**S("Lehívás - felvett csomag", "2026-10-13T08:24:28+00:00"), "code": "86"}, {**S("Utánvét adat rögzítve", "2026-10-12T10:56:03+00:00"), "code": "52"}]
        self.m.UPLOADED.write_text(json.dumps({"#1025": 549107707}))
        g = fake_gls({"549107707": "3423946814"}, {"3423946814": held})
        cod = self.m.build(shop=shop(orders=(COD_ORDER,)), bill=bill(), now=10_000, gls=g)["orders"][0]
        self.assertEqual((cod["shipping"]["state"], cod["shipping"]["code"]), ("problem", "07"))
        self.assertEqual(cod["shipping"]["handed_at"], "2026-10-13T08:24:28+00:00")  # GLS picked it up from us
        self.assertEqual(cod["delivered"], {"state": "no", "at": None}); self.assertFalse(cod["cod_invoice_due"])
    def test_hourly_and_never_again_once_delivered(self):
        self.run_build(now=10_000); self.assertEqual(self.gls.statuses.call_count, 2); self.assertEqual(self.gls.parcel_numbers.call_count, 1)
        self.run_build(now=10_000 + 1800); self.assertEqual(self.gls.statuses.call_count, 2)  # within the hour: nothing asked
        self.run_build(now=10_000 + 3600); self.assertEqual(self.gls.statuses.call_count, 3)  # only the undelivered one
        self.assertEqual(self.gls.parcel_numbers.call_count, 1)  # the parcel number is resolved once
    def test_replaced_label_starts_over(self):
        self.run_build(now=10_000)
        self.m.UPLOADED.write_text(json.dumps({"#1025": 777}))
        g = fake_gls({"777": "999"}, {"999": DELIVERED[2:]})
        _, cod = self.run_build(now=10_100, gls=g)["orders"]
        self.assertEqual((cod["shipping"]["parcel_number"], cod["shipping"]["state"]), ("999", "handed_to_gls"))
    def test_not_printed_yet_is_uploaded(self):
        g = fake_gls({}, {})
        paid, _ = self.run_build(gls=g)["orders"]
        self.assertEqual(paid["shipping"]["state"], "uploaded"); self.assertIsNone(paid["shipping"]["parcel_number"])
        g.statuses.assert_not_called()
    def test_gls_down_keeps_what_is_known_and_backs_off(self):
        self.run_build(now=10_000)
        down = fake_gls(fail="status")
        out = self.run_build(now=10_000 + 3600, gls=down)
        self.assertFalse(next(a for a in out["automations"] if a["key"] == "gls_tracking")["ok"])
        self.assertEqual(out["orders"][1]["delivered"]["state"], "yes")  # stored state still goes
        self.assertEqual(out["orders"][0]["shipping"]["state"], "problem")
        self.run_build(now=10_000 + 3700, gls=down); self.assertEqual(down.statuses.call_count, 1)  # no new call within GLS_RETRY
    def test_no_label_no_shipping_keys(self):
        self.m.UPLOADED.write_text("{}")
        o = self.run_build()["orders"][0]
        self.assertNotIn("delivered", o); self.assertNotIn("shipping", o)
    def test_dry_run_counts(self):
        out = self.m.push(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill(), gls=self.gls)
        self.assertEqual((out["gls_tracked"], out["delivered"], out["cod_invoice_due"]), (2, 1, ["#1025"]))

class Fulfillment(unittest.TestCase):
    """#1024 (ORDER) is held at the depot after pickup, #1025 (COD_ORDER) delivered: both were picked up by GLS."""
    def setUp(self):
        self.m = load()
        self.m.UPLOADED.write_text(json.dumps({"#1024": {"parcel_id": 548860860}, "#1025": 549107707}))
        self.gls = fake_gls({"548860860": "3423946814", "549107707": "3424008826"}, {"3423946814": HELD, "3424008826": DELIVERED})
        # switched on long before these pickups (now=10_000 is 1970), unless a test says otherwise
        env = {k: v for k, v in os.environ.items() if k not in ("SHOPIFY_AUTO_FULFILL", "SHOPIFY_FULFILL_SINCE")}
        self.env = mock.patch.dict(os.environ, {**env, "SHOPIFY_AUTO_FULFILL": "yes"}, clear=True); self.env.start()
    def tearDown(self): self.env.stop()
    def build(self, s, now=10_000, act=True, gls=None):
        return self.m.build(shop=s, bill=bill(), now=now, gls=gls or self.gls, act=act)
    def test_picked_up_orders_are_fulfilled_once_with_the_parcel_number(self):
        s = shop(orders=(ORDER, COD_ORDER))
        out = self.build(s)
        self.assertEqual(sorted(c.args[:2] for c in s.fulfill.call_args_list), [("6001", "3423946814"), ("6002", "3424008826")])
        for c in s.fulfill.call_args_list: self.assertEqual(c.kwargs, {"confirm": "yes"})
        self.assertEqual(sorted(out["fulfilled_now"]), ["#1024", "#1025"])
        self.assertEqual(out["orders"][1]["shopify_fulfillment"]["state"], "fulfilled")
        self.assertEqual(out["orders"][1]["shopify_fulfillment"]["tracking"], "3424008826")
        self.build(s, now=10_060); self.assertEqual(s.fulfill.call_count, 2)  # never twice
        stored = json.loads(self.m.FULFILLED.read_text()); self.assertEqual(stored["#1025"]["tracking"], "3424008826")
        self.assertNotIn("Titkos", self.m.FULFILLED.read_text())
    def test_dry_run_never_fulfills(self):
        s = shop(orders=(ORDER, COD_ORDER))
        out = self.build(s, act=False)
        s.fulfill.assert_not_called(); self.assertFalse(self.m.FULFILLED.exists())
        self.assertEqual(sorted(out["fulfilled_now"]), ["#1024", "#1025"])
        self.assertEqual(out["orders"][0]["shopify_fulfillment"]["state"], "pending")
        p = self.m.push(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill(), gls=self.gls)  # real clock: switched on now, older pickups wait
        self.assertEqual(p["would_fulfill"], []); self.assertNotIn("fulfilled_now", p)
        self.m.FULFILL_START.write_text(json.dumps({"since": "2026-10-01"}))
        p = self.m.push(shop=shop(orders=(ORDER, COD_ORDER)), bill=bill(), gls=self.gls)
        self.assertEqual(sorted(p["would_fulfill"]), ["#1024", "#1025"])
    def test_not_picked_up_not_fulfilled(self):
        g = fake_gls({"548860860": "3423946814", "549107707": "3424008826"}, {"3423946814": DELIVERED[3:], "3424008826": []})
        s = shop(orders=(ORDER, COD_ORDER)); out = self.build(s, gls=g)
        s.fulfill.assert_not_called(); self.assertEqual(out["orders"][0]["shopify_fulfillment"]["state"], "pending")
    def test_no_label_cancelled_or_already_fulfilled_skipped(self):
        self.m.UPLOADED.write_text("{}")
        s = shop(orders=(ORDER,)); self.build(s); s.fulfill.assert_not_called()
        self.m.UPLOADED.write_text(json.dumps({"#1024": {"parcel_id": 548860860}, "#1025": 549107707}))
        s = shop(orders=({**ORDER, "cancelled_at": "2026-10-12T00:00:00Z"}, {**COD_ORDER, "fulfillment_status": "fulfilled",
                                                                               "fulfillments": [{"tracking_numbers": ["3424008826"]}]}))
        out = self.build(s, now=20_000); s.fulfill.assert_not_called()
        self.assertEqual(out["orders"][1]["shopify_fulfillment"], {"state": "fulfilled", "tracking": "3424008826", "at": None, "by": "shopify", "error": None})
    def test_already_fulfilled_in_shopify_is_recorded_not_reported_new(self):
        s = shop(orders=(COD_ORDER,), fulfill="already"); out = self.build(s)
        self.assertEqual(out["fulfilled_now"], []); self.assertTrue(json.loads(self.m.FULFILLED.read_text())["#1025"]["already"])
    def test_failure_is_reported_and_retried_later(self):
        s = shop(orders=(COD_ORDER,), fulfill="fail"); out = self.build(s)
        self.assertEqual(out["orders"][0]["shopify_fulfillment"]["state"], "failed")
        self.assertFalse(next(a for a in out["automations"] if a["key"] == "shopify_fulfillment")["ok"])
        self.build(s, now=10_600); self.assertEqual(s.fulfill.call_count, 1)  # backs off
        ok = shop(orders=(COD_ORDER,)); out = self.build(ok, now=10_000 + self.m.FULFILL_RETRY)
        self.assertEqual(ok.fulfill.call_count, 1); self.assertEqual(out["orders"][0]["shopify_fulfillment"]["state"], "fulfilled")
    def test_off_by_default_and_only_explicit_yes_turns_it_on(self):
        for value in (None, "", "no", "true", "1", "on", "YES please"):
            s = shop(orders=(ORDER, COD_ORDER))
            os.environ.pop("SHOPIFY_AUTO_FULFILL", None)
            if value is not None: os.environ["SHOPIFY_AUTO_FULFILL"] = value
            out = self.build(s)
            s.fulfill.assert_not_called(); self.assertFalse(self.m.FULFILLED.exists()); self.assertFalse(self.m.FULFILL_START.exists())
            self.assertEqual(out["fulfilled_now"], [])
            self.assertEqual(next(a for a in out["automations"] if a["key"] == "shopify_fulfillment")["label"], "Shopify fulfillment (off)")
    def test_switching_on_does_not_back_fill_earlier_pickups(self):
        on_at = 1792044000  # 2026-10-15 06:00 UTC, after both pickups (2026-10-13)
        s = shop(orders=(ORDER, COD_ORDER)); out = self.build(s, now=on_at)
        s.fulfill.assert_not_called(); self.assertEqual(out["fulfilled_now"], [])
        self.assertEqual(json.loads(self.m.FULFILL_START.read_text())["since"], "2026-10-15T06:00:00+00:00")
        s = shop(orders=(ORDER, COD_ORDER)); self.build(s, now=on_at + 7200); s.fulfill.assert_not_called()  # the start stays
    def test_pickup_after_switch_on_is_fulfilled(self):
        self.m.FULFILL_START.write_text(json.dumps({"since": "2026-10-13T08:24:27+00:00"}))  # between the two pickups
        s = shop(orders=(ORDER, COD_ORDER)); out = self.build(s, now=1792044000)
        self.assertEqual(out["fulfilled_now"], ["#1024"])  # picked up 08:24:28; #1025 at 08:24:26 is older
    def test_explicit_back_fill(self):
        self.m.FULFILL_START.write_text(json.dumps({"since": "2026-10-15T06:00:00+00:00"}))
        os.environ["SHOPIFY_FULFILL_SINCE"] = "2026-10-13"
        s = shop(orders=(ORDER, COD_ORDER)); out = self.build(s, now=1792044000)
        self.assertEqual(sorted(out["fulfilled_now"]), ["#1024", "#1025"])
        os.environ["SHOPIFY_FULFILL_SINCE"] = "soon"
        self.m.FULFILLED.unlink(); s = shop(orders=(ORDER, COD_ORDER)); out = self.build(s, now=1792044000)
        s.fulfill.assert_not_called(); self.assertIn("SHOPIFY_FULFILL_SINCE", " ".join(out["errors"]))
    def test_dry_run_writes_no_start(self):
        self.build(shop(orders=(ORDER, COD_ORDER)), act=False); self.assertFalse(self.m.FULFILL_START.exists())
    def test_unknown_pickup_time_is_not_fulfilled(self):
        self.m.GLS_STATUS.write_text(json.dumps({"#1025": {"parcel_id": "549107707", "parcel_number": "3424008826", "state": "delivered",
                                                           "handed_at": None, "checked": 10_000, "checked_at": "x"}}))
        self.m.UPLOADED.write_text(json.dumps({"#1025": 549107707}))
        s = shop(orders=(COD_ORDER,)); self.build(s); s.fulfill.assert_not_called()

class Scopes(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_scopes_go_to_the_site(self):
        out = self.m.build(shop=shop(), bill=bill(), now=10_000)
        self.assertTrue(out["shopify_scopes"]["ok"]); self.assertIn("checked_at", out["shopify_scopes"])
    def test_missing_scope_is_an_error(self):
        out = self.m.build(shop=shop(scopes={"ok": False, "missing": ["read_reports"], "required": [], "scopes": []}), bill=bill())
        self.assertIn("[Shopify scopes] missing: read_reports", out["errors"])
        self.assertFalse(next(a for a in out["automations"] if a["key"] == "shopify_scopes")["ok"])
    def test_unreadable_scopes_is_none(self):
        out = self.m.build(shop=shop(fail="scopes"), bill=bill())
        self.assertIsNone(out["shopify_scopes"]); self.assertEqual(len(out["orders"]), 1)

class Push(unittest.TestCase):
    def setUp(self): self.m = load()
    def test_dry_run_shows_counts_only_and_sends_nothing(self):
        with mock.patch.object(self.m.requests, "post") as post:
            out = self.m.push(shop=shop(), bill=bill())
        post.assert_not_called()
        self.assertEqual((out["dry_run"], out["orders"], out["products"]), (True, 1, 1))
        for secret in ("x@y.hu", "Titkos", "+3630", "Fo utca"): self.assertNotIn(secret, json.dumps(out, ensure_ascii=False))
    def test_send_result_has_no_customer_data(self):
        env = {"ECOM_SITE_URL": "https://site.example", "ECOM_INGEST_SECRET": "s3"}
        with mock.patch.dict(os.environ, env), mock.patch.object(self.m.requests, "post", return_value=Resp({"ok": True, "new_order_ids": ["6001"]})) as post:
            out = self.m.push("yes", shop=shop(), bill=bill())
        self.assertEqual(post.call_args.args[0], "https://site.example/api/ecom/ingest")
        self.assertEqual(post.call_args.kwargs["headers"], {"x-ecom-secret": "s3"})
        self.assertEqual(out["new_orders"], 1)
        self.assertNotIn("Titkos", json.dumps(out, ensure_ascii=False))
    def test_refuses_plain_http(self):
        with mock.patch.dict(os.environ, {"ECOM_SITE_URL": "http://site.example", "ECOM_INGEST_SECRET": "s3"}), mock.patch.object(self.m.requests, "post") as post:
            with self.assertRaises(ConnectorError): self.m.push("yes", shop=shop(), bill=bill())
        post.assert_not_called()
    def test_wrong_secret_is_named(self):
        with mock.patch.dict(os.environ, {"ECOM_SITE_URL": "https://site.example", "ECOM_INGEST_SECRET": "s3"}), mock.patch.object(self.m.requests, "post", return_value=Resp(code=401)):
            with self.assertRaisesRegex(ConnectorError, "401"): self.m.push("yes", shop=shop(), bill=bill())

if __name__ == "__main__": unittest.main()
