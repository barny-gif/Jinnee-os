"""The order chain's news and the cash-on-delivery invoice at delivery (owner's rule, 2026-11-16). Made-up shop data only:
order numbers, amounts and parcel numbers are invented. Run: python3 -m unittest discover tests"""
import datetime as dt, importlib.util, json, os, pathlib, sys, tempfile, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "core")); sys.path.insert(0, str(ROOT / "tests"))
import fake_business; fake_business.use()
from cbase import ConnectorError
import notices

NOW = 1_800_000_000  # 2027-01-15 08:00 UTC
def iso(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")

def load():
    spec = importlib.util.spec_from_file_location("order_events_t", ROOT / "connectors" / "order_events.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    tmp = pathlib.Path(tempfile.mkdtemp())
    m.BRAIN = tmp
    m.order_auto.STATE = tmp / "order_auto.json"
    return m, tmp

def order(**kw):
    o = {"id": 1001, "name": "#1001", "created_at": iso(NOW + 60), "total_price": "12345.00", "financial_status": "pending",
         "cancelled_at": None, "payment_gateway_names": ["Cash on Delivery (COD)"], "test": False, "tags": ""}
    o.update(kw); return o

def card(**kw): return order(payment_gateway_names=["shopify_payments"], financial_status="paid", **kw)

DELIVERED = {"state": "delivered", "code": "05", "parcel_number": "90001001", "handed_at": iso(NOW + 3600), "delivered_at": iso(NOW + 86400)}
DRAFT = [{"id": 501, "type": "draft", "comment": "Shopify #1001", "number": None}]
AUTO_ON = {"enabled": {"label": True, "draft": True}, "held": {}}

class Book:
    def __init__(self): self.items = []
    def add(self, agent, action, title, summary="", text=None, payload=None, ref=None, **kw):
        it = {"id": ref or f"a{len(self.items) + 1}", "agent": agent, "action": action, "title": title, "text": text, "payload": payload}
        self.items.append(it); return it, ""


class Base(unittest.TestCase):
    def setUp(self):
        self.m, self.tmp = load()
        env = mock.patch.dict(os.environ, {"JINNEE_LANG": "en"}); env.start(); self.addCleanup(env.stop)
        for k in ("ORDER_NOTICES", "COD_INVOICE_ON_DELIVERY", "COD_INVOICE_SINCE"): os.environ.pop(k, None)
        self.book, self.flow = Book(), mock.Mock()
        self.flow.invoice_issue.return_value = {"invoice": "EX-2027-1", "sent": True}
        self.m.owner_name = lambda: ""
        self.start()

    def start(self):
        """The first real round, before any order: it records the switch-on moment."""
        self.round([], now=NOW)

    def round(self, raw, shipping=None, invoices=DRAFT, auto=None, cod=None, now=NOW + 2 * 86400, level=1, act=True, uploaded=None):
        return self.m.run(raw, shipping or {}, uploaded or {}, auto or {"enabled": {"label": False, "draft": False}, "held": {}},
                          cod or {}, invoices, now, act=act, book=self.book, flow=self.flow, gate=lambda a: level)

    def news(self): return [n["text"] for n in notices.recent(self.tmp, 50)][::-1]
    def keys(self): return [n["key"] for n in notices.recent(self.tmp, 50)][::-1]


class NewOrder(Base):
    def test_cod_order_with_draft_and_label_ready(self):
        self.m.order_auto.STATE.write_text(json.dumps({"#1001": {"label": {"at": iso(NOW)}, "draft": {"at": iso(NOW), "draft_id": 501}}}))
        self.round([order()], auto=AUTO_ON, now=NOW + 120)
        (text,) = self.news()
        self.assertEqual(text, "New order: #1001, cash on delivery, 12 345 Ft. The invoice draft and the GLS label are ready. "
                               "The invoice is issued only once the customer has taken the parcel.")

    def test_the_owner_is_named_when_the_business_file_has_a_name(self):
        self.m.owner_name = lambda: "Anna"
        self.round([card()], now=NOW + 120)
        self.assertTrue(self.news()[0].startswith("Anna, a new order came in: #1001, paid in advance, 12 345 Ft."))

    def test_hungarian(self):
        os.environ["JINNEE_LANG"] = "hu"; self.m.owner_name = lambda: "Anna"
        self.m.order_auto.STATE.write_text(json.dumps({"#1001": {"label": {"at": iso(NOW)}, "draft": {"at": iso(NOW)}}}))
        self.round([order()], auto=AUTO_ON, now=NOW + 120)
        self.assertEqual(self.news()[0], "Anna, új megrendelés jött: #1001, utánvét, 12 345 Ft. A számla-piszkozat és a GLS-címke kész. "
                                         "A számlát csak akkor állítom ki, ha a vevő átvette a csomagot.")

    def test_a_held_step_names_the_field_never_the_data(self):
        self.m.order_auto.STATE.write_text(json.dumps({"#1001": {"draft": {"at": iso(NOW)}}}))
        self.round([order()], auto={**AUTO_ON, "held": {"#1001": ["label: house number"]}}, now=NOW + 120)
        self.assertIn("The invoice draft is ready. The GLS label was not made, missing: house number.", self.news()[0])

    def test_told_once(self):
        for n in range(3): self.round([card()], now=NOW + 120 + n * 60)
        self.assertEqual(len(self.news()), 1)

    def test_not_until_the_automatic_steps_ran(self):
        self.round([order()], auto=AUTO_ON, now=NOW + 120)
        self.assertEqual(self.news(), [])  # label and draft are switched on but not done yet: next round
        self.round([order()], auto=AUTO_ON, now=NOW + 2 * 3600)  # an hour later it is told anyway
        self.assertEqual(len(self.news()), 1)

    def test_orders_from_before_the_switch_on_are_not_told(self):
        self.round([card(created_at=iso(NOW - 86400))])
        self.assertEqual(self.news(), [])

    def test_dry_run_writes_nothing(self):
        out = self.round([card()], act=False)
        self.assertEqual(out["news"], []); self.assertEqual(self.news(), [])

    def test_switched_off(self):
        os.environ["ORDER_NOTICES"] = "off"
        self.round([card()]); self.assertEqual(self.news(), [])

    def test_cancelled_test_and_tagged_orders_are_silent(self):
        self.round([card(cancelled_at=iso(NOW)), card(id=2, name="#1002", test=True), card(id=3, name="#1003", tags="gift")])
        self.assertEqual(self.news(), [])


class PickedUp(Base):
    def test_told_once_when_gls_has_it(self):
        ship = {"#1001": {"state": "handed_to_gls", "code": "86", "handed_at": iso(NOW + 3600)}}
        self.round([card()], ship); self.round([card()], ship)
        self.assertEqual([k for k in self.keys() if k.startswith("picked_up")], ["picked_up:#1001"])
        self.assertIn("GLS picked up the parcel of #1001 (paid in advance, 12 345 Ft)", self.news()[-1])

    def test_not_before(self):
        self.round([card()], {"#1001": {"state": "uploaded"}})
        self.assertNotIn("picked_up:#1001", self.keys())


class CodInvoiceOnDelivery(Base):
    def test_not_delivered_nothing_is_asked_or_issued(self):
        for state in ({"state": "uploaded"}, {"state": "in_transit", "code": "02", "handed_at": iso(NOW + 3600)},
                      {"state": "delivered", "code": "07"}):
            out = self.round([order()], {"#1001": state}, level=2)
            self.assertEqual(out["invoice"], {"queued": [], "issued": [], "no_draft": [], "errors": []})
        self.assertEqual(self.book.items, []); self.flow.invoice_issue.assert_not_called()

    def test_delivered_level_1_asks_the_owner_once(self):
        out = self.round([order()], {"#1001": DELIVERED})
        self.assertEqual(out["invoice"]["queued"], ["#1001"]); self.flow.invoice_issue.assert_not_called()
        (it,) = self.book.items
        self.assertEqual((it["agent"], it["action"], it["id"]), ("ledger", "issue_invoice", "1001_cod_atvette"))
        self.assertEqual(it["title"], "#1001: the customer took the parcel (cash on delivery, 12 345 Ft). May I issue and send the invoice?")
        self.assertIn("python3 connectors/order_flow.py invoice_issue --draft_id 501 --confirm yes --send yes", it["text"])
        self.assertEqual(it["payload"]["draft_id"], "501")
        self.round([order()], {"#1001": DELIVERED}); self.round([order()], {"#1001": DELIVERED}, now=NOW + 9 * 86400)
        self.assertEqual(len(self.book.items), 1)

    def test_the_draft_id_from_the_automatic_step_wins(self):
        self.m.order_auto.STATE.write_text(json.dumps({"#1001": {"draft": {"at": iso(NOW), "draft_id": 777}}}))
        self.round([order()], {"#1001": DELIVERED})
        self.assertIn("--draft_id 777 ", self.book.items[0]["text"])

    def test_delivered_level_2_issues_and_sends_then_tells(self):
        out = self.round([order()], {"#1001": DELIVERED}, level=2)
        self.assertEqual(out["invoice"]["issued"], ["#1001"]); self.assertEqual(self.book.items, [])
        self.flow.invoice_issue.assert_called_once_with("501", confirm="yes", send="yes")
        self.assertIn("#1001: the customer took the parcel; I issued the invoice (EX-2027-1) and sent it to them.", self.news())
        self.round([order()], {"#1001": DELIVERED}, level=2)
        self.assertEqual(self.flow.invoice_issue.call_count, 1)

    def test_a_failed_issue_is_told_once_and_tried_again_later(self):
        self.flow.invoice_issue.side_effect = ConnectorError("Billingo answered 500")
        out = self.round([order()], {"#1001": DELIVERED}, level=2)
        self.assertEqual(len(out["invoice"]["errors"]), 1)
        self.round([order()], {"#1001": DELIVERED}, level=2, now=NOW + 2 * 86400 + 60)  # within 30 minutes: not again
        self.assertEqual(self.flow.invoice_issue.call_count, 1)
        self.flow.invoice_issue.side_effect = None
        self.round([order()], {"#1001": DELIVERED}, level=2, now=NOW + 2 * 86400 + 3600)
        self.assertEqual(self.flow.invoice_issue.call_count, 2)
        self.assertEqual(sum(1 for k in self.keys() if k.startswith("invoice_failed")), 1)

    def test_paid_order_keeps_its_old_path(self):
        out = self.round([card()], {"#1001": DELIVERED}, level=2)
        self.assertEqual(out["invoice"]["queued"] + out["invoice"]["issued"], []); self.assertEqual(self.book.items, [])

    def test_already_issued_is_left_alone(self):
        issued = [{"id": 9, "type": "invoice", "comment": "Shopify #1001", "number": "EX-2027-0"}]
        self.round([order()], {"#1001": DELIVERED}, invoices=issued, level=2)
        self.assertEqual(self.book.items, []); self.flow.invoice_issue.assert_not_called()

    def test_another_orders_draft_is_not_used(self):
        other = [{"id": 502, "type": "draft", "comment": "Shopify #10011"}]
        out = self.round([order()], {"#1001": DELIVERED}, invoices=other)
        self.assertEqual(out["invoice"]["no_draft"], ["#1001"]); self.assertEqual(self.book.items, [])
        self.assertIn("I find no invoice draft for it in Billingo", self.news()[-1])

    def test_billingo_unknown_waits(self):
        out = self.round([order()], {"#1001": DELIVERED}, invoices=None)
        self.assertEqual(self.book.items, []); self.assertEqual(out["invoice"]["no_draft"], [])
        self.round([order()], {"#1001": DELIVERED})
        self.assertEqual(len(self.book.items), 1)

    def test_switched_off(self):
        os.environ["COD_INVOICE_ON_DELIVERY"] = "off"
        self.round([order()], {"#1001": DELIVERED}, level=2)
        self.assertEqual(self.book.items, []); self.flow.invoice_issue.assert_not_called()

    def test_delivered_before_the_switch_on_is_not_asked(self):
        old = order(created_at=iso(NOW - 9 * 86400))
        self.round([old], {"#1001": {**DELIVERED, "delivered_at": iso(NOW - 86400)}})
        self.assertEqual(self.book.items, [])
        os.environ["COD_INVOICE_SINCE"] = iso(NOW - 2 * 86400)  # asked for on purpose
        self.round([old], {"#1001": {**DELIVERED, "delivered_at": iso(NOW - 86400)}})
        self.assertEqual(len(self.book.items), 1)

    def test_returned_parcel_keeps_the_draft(self):
        self.round([order()], {"#1001": {"state": "returned", "code": "23", "status_at": iso(NOW + 86400)}}, level=2)
        self.flow.invoice_issue.assert_not_called(); self.assertEqual(self.book.items, [])
        self.assertIn("#1001: the parcel went back", self.news()[-1])


class EndToEnd(Base):
    """The real order_flow behind the delivery step: its own gate is the last word."""
    def setUp(self):
        super().setUp()
        import order_flow
        self.of = order_flow
        old = (order_flow.ISSUED, order_flow.GLS_STATUS)
        self.addCleanup(lambda: setattr(order_flow, "ISSUED", old[0]) or setattr(order_flow, "GLS_STATUS", old[1]))
        order_flow.ISSUED, order_flow.GLS_STATUS = self.tmp / "issued.json", self.tmp / "gls_status.json"
        self.bill = mock.Mock(document=lambda i: {"type": "draft", "comment": "Shopify #1001", "gross_total": 12345,
                                                  "payment_method": "cash_on_delivery", "partner": {"id": 5}},
                              partner_email=lambda p: "customer@example.com",
                              create_from_draft=mock.Mock(return_value={"id": 77, "invoice_number": "EX-2027-1"}),
                              send_by_email=mock.Mock(return_value=True))
        self.flow = mock.Mock(invoice_issue=lambda d, **kw: order_flow.invoice_issue(d, bill=self.bill, **kw))

    def test_delivered_on_file_is_issued(self):
        self.of.GLS_STATUS.write_text(json.dumps({"#1001": DELIVERED}))
        out = self.round([order()], {"#1001": DELIVERED}, level=2)
        self.assertEqual(out["invoice"]["issued"], ["#1001"]); self.bill.send_by_email.assert_called_once()

    def test_the_gate_refuses_when_the_status_file_disagrees(self):
        self.of.GLS_STATUS.write_text(json.dumps({"#1001": {"state": "in_transit", "code": "02"}}))
        out = self.round([order()], {"#1001": DELIVERED}, level=2)
        self.assertEqual(out["invoice"]["issued"], []); self.bill.create_from_draft.assert_not_called()
        self.assertIn("GLS has not delivered it yet", out["invoice"]["errors"][0])


class CodPaidNews(Base):
    def test_marked_paid_is_told(self):
        self.round([], cod={"marked_rows": [{"name": "#1001", "total": 12345}]})
        self.assertEqual(self.news(), ["GLS transferred the cash on delivery of #1001 (12 345 Ft); I set it paid in Shopify."])

    def test_issues_are_told_once_each(self):
        cod = {"issues": [{"order": "#1001", "kind": "mismatch"}, {"order": "#1002", "kind": "unclear"}, {"order": "#1003", "kind": "late"}]}
        self.round([], cod=cod); self.round([], cod=cod)
        self.assertEqual(self.keys(), ["cod_mismatch:#1001", "cod_unclear:#1002", "cod_late:#1003"])
        self.assertIn("more than 10 days ago", self.news()[2])


if __name__ == "__main__": unittest.main()
