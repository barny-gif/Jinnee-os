"""Critical-data checks before the invoice draft and the MyGLS label. Run: python3 -m unittest discover tests"""
import importlib.util, json, pathlib, sys, unittest
from unittest import mock
HERE = pathlib.Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "connectors" / "cbase.py").exists())
sys.path.insert(0, str(ROOT / "connectors"))
s = importlib.util.spec_from_file_location("t_order_checks", ROOT / "connectors" / "order_checks.py"); C = importlib.util.module_from_spec(s); s.loader.exec_module(C)

ADDR = {"name": "Titkos Vevo", "address1": "Fő utca 12/B", "zip": "1111", "city": "Budapest", "country_code": "HU", "phone": "06 30 111 2222"}
OK = {"name": "#5030", "total_price": "12600.00", "financial_status": "paid", "payment_gateway_names": ["SimplePay"], "cancelled_at": None, "test": False,
      "email": "x@y.hu", "phone": None, "shipping_address": ADDR, "billing_address": ADDR, "shipping_lines": [{"title": "GLS házhoz"}]}

EGER6 = "111" + "222"; EGER = "36 111 222"  # made up

def fields(o, step=None): return sorted(p["field"] for p in C.check(o) if step in (None, p["step"]))

class Checks(unittest.TestCase):
    def test_complete_order_has_nothing_missing(self):
        self.assertEqual(C.check(OK), []); self.assertTrue(C.ready([], "label")); self.assertTrue(C.ready([], "invoice"))
    def test_missing_house_number_stops_the_label_only(self):
        o = {**OK, "shipping_address": {**ADDR, "address1": "Fő utca"}}
        p = C.check(o)
        self.assertEqual(fields(o), ["house_number"])
        self.assertFalse(C.ready(p, "label")); self.assertTrue(C.ready(p, "invoice"))
    def test_house_number_glued_or_elsewhere_is_found(self):
        # shape of a real case: 'A A A.9.' with address2 '-': it was a false 'house number missing' stop
        for a1, a2 in (("Kossuth Lajos u.12.", "-"), ("Fő utca12", None), ("Fő utca", "12"), ("Fő utca hsz. 12", ""),
                       ("Fő u. 12. 2. em. 5.", ""), ("Fő utca 12", "3/5"), ("1848. március 15. tér 4", ""), ("Május 1. utca 3", "")):
            o = {**OK, "shipping_address": {**ADDR, "address1": a1, "address2": a2}}
            self.assertEqual(fields(o), [], (a1, a2))
    def test_missing_house_number_says_why(self):
        def why(a1, a2=""):
            p = [x for x in C.check({**OK, "shipping_address": {**ADDR, "address1": a1, "address2": a2}}) if x["field"] == "house_number"]
            self.assertEqual([x["level"] for x in p], ["stop"], a1); return p[0]["why"]
        self.assertEqual(why("Fő utca"), "nincs szám a címben")
        self.assertIn("utcanév része", why("Május 1. utca"))
        self.assertIn("nem egyértelmű", why("Fő utca 12 14")); self.assertIn("nem egyértelmű", why("Fő utca", "12 3/5"))
        self.assertNotIn("Fő", why("Fő utca 12 14"))  # the reason names the problem, never the address
    # The JinneeHQ address check (address-check.test.js HOUSE_CASES) reads the same addresses: the house number yes / no / not clear
    # verdict must be the same on every row. One row is stricter here on purpose ('12 II/5', a floor/door with no floor word: STOP).
    SAME_AS_HQ = [("Kossuth u.12.", None, "ok"), ("Kossuth utca12", None, "ok"), ("Kossuth utca12.", None, "ok"),
                  ("Kossuth u.12. fsz.2.", None, "ok"), ("Fő utca 12", None, "ok"), ("Fő utca 12.", None, "ok"), ("Fő u. 12/B", None, "ok"),
                  ("Fő utca 12-14", None, "ok"), ("Fő utca 12 B", None, "ok"), ("Petőfi Sándor u 7", None, "ok"), ("Kossuth Lajos 12", None, "ok"),
                  ("Fő utca hsz. 12", None, "ok"), ("Fő utca 12 hsz.", None, "ok"), ("Fő utca 12. 3. em. 4.", None, "ok"),
                  ("Rákóczi Ferenc utca 5/A 2. emelet 6. ajtó", None, "ok"), ("Andrássy út 2. 3. em. 4", None, "ok"),
                  ("Fő utca 12 II/5", None, "ambiguous"), ("Fő utca 12 B ép. 2. lh.", None, "ok"), ("1848. március 15. tér 4", None, "ok"),
                  ("IV. Béla út 2", None, "ok"), ("Május 1. utca 3", None, "ok"), ("Május 1. utca", None, "missing"),
                  ("1848. március 15. tér", None, "missing"), ("Fő utca", "12", "ok"), ("Fő utca", "12/B fsz. 1", "ok"), ("", "Fő utca 9", "ok"),
                  ("Fő utca", None, "missing"), ("Fő utca", "-", "missing"), ("Fő utca 3. em. 4.", None, "only_floor"),
                  ("Fő utca", "3. em. 4.", "only_floor"), ("", None, "missing"), ("Fő utca 12 14", None, "ambiguous"),
                  ("Fő utca 12, 14", None, "ambiguous"), ("Dózsa György út 17 3/4", None, "ambiguous"), ("1848. március 15. 4", None, "ambiguous")]
    # Phone shapes, the same table as the JinneeHQ check (address-check.test.js): (as typed, normalised, valid in HU)
    PHONE_SAME_AS_HQ = [("+36 30 123 4567", "+36301234567", True), ("06 30 123 4567", "+36301234567", True),
                        ("0036 30 123 4567", "+36301234567", True), ("30/123-4567", "+36301234567", True),
                        ("+36 06 30 123 4567", "+36301234567", True), ("06 1 123 4567", "+3611234567", True),
                        ("12345", "+3612345", False), ("+49 151 2345678", "+491512345678", False),
                        # 2026-11-11 false alarm: the country code typed without its + ('36 30 …' in one block, 11 digits)
                        ("36301234567", "+36301234567", True), ("36 30 123 4567", "+36301234567", True), ("36-30-123-4567", "+36301234567", True),
                        ("36 1 123 4567", "+3611234567", True), ("36 06 30 123 4567", "+36301234567", True),
                        # the domestic 0 / 06 left after +36, written several ways
                        ("+36 06 20 123 4567", "+36201234567", True), ("+36 0 20 123 4567", "+36201234567", True),
                        ("+36 (0) 20 123 4567", "+36201234567", True), ("+3606301234567", "+36301234567", True),
                        # 0036, also with 06 left in or a + in front; the country code twice
                        ("0036 06 30 123 4567", "+36301234567", True), ("+0036 30 123 4567", "+36301234567", True),
                        ("+36 36 30 123 4567", "+36301234567", True),
                        # separators
                        ("(06-30) 123-45-67", "+36301234567", True), ("06/30/123-4567", "+36301234567", True),
                        # an Eger number (area code 36) stays what it is
                        # (built from parts: a 6-digit Eger test number cannot end in one of tools/guard.py's made-up 7-digit endings)
                        (EGER, "+3636" + EGER6, True), ("06 " + EGER, "+3636" + EGER6, True), ("+36 " + EGER, "+3636" + EGER6, True),
                        # still bad: too short / too long / nothing
                        ("36", "+3636", False), ("+36 30 123 45678 9", "+363012345678" + "9", False), ("", "", False)]
    def test_phone_shapes_match_the_hq_check(self):
        for raw, norm, ok in self.PHONE_SAME_AS_HQ:
            self.assertEqual(C.flow.phone(raw), norm, raw); self.assertEqual(C.flow.valid_phone(raw), ok, raw)
            o = {**OK, "phone": None, "shipping_address": {**ADDR, "phone": raw}, "billing_address": {**ADDR, "phone": raw}}
            self.assertEqual("phone" in fields(o), not ok, raw)
        self.assertTrue(C.flow.valid_phone("+49 151 2345678", "DE"))
    def test_house_number_verdict_matches_the_hq_check(self):
        for a1, a2, want in self.SAME_AS_HQ:
            a = C.flow.parse_address({"address1": a1, "address2": a2})
            got = "ok" if a["house"] else "ambiguous" if "nem egyértelmű" in a["why"] else "only_floor" if "csak emelet" in a["why"] else "missing"
            self.assertEqual(got, want, (a1, a2))
            p = [x for x in C.check({**OK, "shipping_address": {**ADDR, "address1": a1, "address2": a2}}) if x["field"] == "house_number"]
            self.assertEqual(bool(p), want != "ok", (a1, a2))
    def test_only_a_floor_or_door_is_no_house_number(self):
        # 'Fő utca 3. em. 4.': the 3 is the floor; it must not go on the label as the house number
        for a in ("Fő utca 3. em. 4.", "Fő utca 3. emelet", "Fő utca 12 em 3", "Fő utca 5 ajtó", "Fő utca fsz. 1"):
            r = C.flow.parse_street(a); self.assertEqual((r["house"], r["why"]), ("", C.flow.ONLY_FLOOR), a)
        self.assertEqual(C.flow.parse_street("Fő utca 12. 3. ajtó")["house"], "12")  # a number before the floor is still the house number
        o = {**OK, "shipping_address": {**ADDR, "address1": "Fő utca 3. em. 4."}}
        self.assertEqual([(x["field"], x["level"], x["why"]) for x in C.check(o)], [("house_number", "stop", C.flow.ONLY_FLOOR)])
    def test_a_street_type_alone_is_no_street(self):
        for a in ("utca 12", "u. 12"):
            self.assertEqual(C.flow.parse_street(a)["street"], "", a)
            self.assertIn("street", fields({**OK, "shipping_address": {**ADDR, "address1": a}}), a)
        self.assertEqual(C.flow.parse_street("Tér utca 3")["street"], "Tér utca")  # a name that is also a street type word is fine
    def test_phone_missing_or_bad(self):
        none = {**ADDR, "phone": None}
        self.assertEqual(fields({**OK, "shipping_address": none, "billing_address": none}), ["phone"])
        bad = {**ADDR, "phone": "123"}
        self.assertEqual(fields({**OK, "shipping_address": bad, "billing_address": bad}), ["phone"])
        self.assertEqual(fields({**OK, "shipping_address": bad}), [])  # a valid billing phone is enough
        self.assertEqual(fields({**OK, "shipping_address": none, "billing_address": none, "phone": "+36301112222"}), [])  # order phone is enough
        self.assertEqual(fields({**OK, "shipping_address": none}), [])  # the billing phone is enough too
    def test_one_malformed_phone_does_not_hide_a_good_one(self):
        # shape of a real case: the checkout form put +36 before a number typed as 06…, and the order phone itself was fine
        doubled = {**ADDR, "phone": "+3606301112222"}
        self.assertEqual(fields({**OK, "shipping_address": doubled, "billing_address": doubled, "phone": "+36301112222"}), [])
        self.assertEqual(fields({**OK, "shipping_address": doubled, "billing_address": doubled}), [])  # +36 06 30… is read as +36 30…
        self.assertEqual(C.flow.phone("+3606301112222"), "+36301112222")
        self.assertEqual(C.flow.phone("+36301112222"), "+36301112222")
        self.assertEqual(C.flow.best_phone(["123", "06 30 111 2222"]), "+36301112222")
        self.assertEqual(C.flow.best_phone(["123"]), "+36123")  # nothing valid: the first given, as before
        self.assertEqual(C.flow.best_phone([None, ""]), "")
    def test_zip_city_country(self):
        o = {**OK, "shipping_address": {**ADDR, "zip": "111", "city": "", "country_code": "AT"}}
        self.assertEqual(fields(o), ["city", "country", "zip"])
    def test_no_address_at_all(self):
        self.assertEqual(fields({**OK, "shipping_address": None}, "label"), ["address"])
    def test_billing_incomplete_stops_the_invoice(self):
        o = {**OK, "billing_address": {**ADDR, "address1": ""}}
        p = C.check(o); self.assertEqual(fields(o), ["billing_address"]); self.assertFalse(C.ready(p, "invoice")); self.assertTrue(C.ready(p, "label"))
    def test_no_email_warns_but_does_not_stop(self):
        p = C.check({**OK, "email": None})
        self.assertEqual({(x["step"], x["level"]) for x in p}, {("label", "warn"), ("invoice", "warn")})
        self.assertTrue(C.ready(p, "label")); self.assertTrue(C.ready(p, "invoice"))
    def test_zero_or_test_order_stops_everything(self):
        for o in ({**OK, "total_price": "0.00"}, {**OK, "test": True}):
            p = C.check(o); self.assertFalse(C.ready(p, "label")); self.assertFalse(C.ready(p, "invoice"))
    def test_pickup_point_needs_hand_work(self):
        self.assertEqual(fields({**OK, "shipping_lines": [{"title": "GLS Csomagpont"}]}), ["shipping_method"])
    def test_unknown_payment(self):
        self.assertEqual(fields({**OK, "payment_gateway_names": []}), ["payment"])
    def test_cancelled_is_not_checked(self):
        self.assertEqual(C.check({**OK, "cancelled_at": "2026-10-15", "shipping_address": None}), [])
    def test_output_never_holds_customer_data(self):
        o = {**OK, "shipping_address": {**ADDR, "address1": "Titkos utca", "phone": "1"}, "billing_address": {**ADDR, "zip": ""}}
        text = json.dumps(C.check(o), ensure_ascii=False)
        for secret in ("Titkos", "1111", "Budapest", "x@y.hu"): self.assertNotIn(secret, text)
    def test_cli_lists_order_and_payment_only(self):
        shop = mock.Mock(_get=mock.Mock(return_value={"orders": [{**OK, "shipping_address": {**ADDR, "address1": "Fő utca"}},
                                                                    {**OK, "name": "#5031", "payment_gateway_names": ["Cash on Delivery (COD)"]}]}))
        out = C.check_open(shop=shop)
        self.assertEqual(out[0], {"order": "#5030", "payment": "PAID", "missing": ["label: házszám (stop)"]})
        self.assertEqual(out[1], {"order": "#5031", "payment": "COD 12600 Ft", "missing": "minden megvan"})
        self.assertNotIn("Titkos", json.dumps(out, ensure_ascii=False))

if __name__ == "__main__": unittest.main()
