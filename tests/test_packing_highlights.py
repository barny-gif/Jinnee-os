"""Packing queue and Overview highlights in the ecom sync (2026-10-30). Run: python3 -m unittest discover tests"""
import json, unittest
from test_ecom_sync import load

UPLOADED = {
    "#1036": {"parcel_id": 557996458, "cod_amount": 21650.0, "at": "2026-10-29T13:44:49+00:00"},
    "eMAG-1136816029": {"parcel_id": 553988366, "cod_amount": 9890.0, "at": "2026-10-21T08:52:38+00:00"},
    "Barter-Influencer-1": {"parcel_id": 555753167, "cod_amount": 0.0, "at": "2026-10-26T08:06:28+00:00"},
    "Influencer-anna": {"parcel_id": 1, "cod_amount": 0.0, "at": "2026-10-30T08:00:00+00:00",
                        "manual": {"kind": "influencer", "items": "1× Montessori Zsiráf Kalandkönyv"}},
    "Test-1": {"parcel_id": 2, "cod_amount": 0.0, "at": "2026-10-30T08:00:00+00:00", "manual": {"kind": "test", "items": "1× próba"}},
    "eMAG-old": 553000000,  # the oldest records hold the parcel id only
}
SHIPPING = {"eMAG-1136816029": {"state": "delivered", "handed_at": "2026-10-21T10:22:09+00:00", "delivered_at": "2026-10-22T12:00:50+00:00"}}

class ManualParcels(unittest.TestCase):
    def setUp(self): self.m = load()

    def rows(self): return {p["ref"]: p for p in self.m._manual_parcels(UPLOADED, SHIPPING)}

    def test_shopify_orders_are_not_manual_parcels(self):
        self.assertNotIn("#1036", self.rows())

    def test_emag_parcel_carries_gls_state_and_cod(self):
        p = self.rows()["eMAG-1136816029"]
        self.assertEqual((p["channel"], p["kind"], p["cod_amount"], p["gls_state"]), ("emag", "emag", 9890.0, "delivered"))
        self.assertEqual(p["handed_at"], "2026-10-21T10:22:09+00:00")

    def test_old_barter_record_gets_its_kind_from_the_reference(self):
        p = self.rows()["Barter-Influencer-1"]
        self.assertEqual((p["channel"], p["kind"], p["items"], p["handed_at"]), ("manual", "barter", "", None))

    def test_new_record_carries_kind_and_contents(self):
        p = self.rows()["Influencer-anna"]
        self.assertEqual((p["kind"], p["items"]), ("influencer", "1× Montessori Zsiráf Kalandkönyv"))

    def test_test_parcel_is_sent_marked_test_so_the_site_leaves_it_out(self):
        self.assertEqual(self.rows()["Test-1"]["kind"], "test")

    def test_bare_parcel_id_record(self):
        p = self.rows()["eMAG-old"]
        self.assertEqual((p["parcel_id"], p["kind"], p["cod_amount"]), (553000000, "emag", 0.0))

    def test_no_personal_fields(self):
        keys = {k for p in self.rows().values() for k in p}
        self.assertFalse(keys & {"name", "address", "street", "phone", "email", "zip", "city"})

class Highlights(unittest.TestCase):
    def setUp(self): self.m = load()

    def test_missing_file_is_an_empty_list(self):
        self.assertEqual(self.m._highlights(), [])

    def test_first_highlight_goes_with_order_number_only(self):
        self.m.HIGHLIGHTS.write_text(json.dumps({"2026-10-30-1032": {
            "order_name": "#1032", "created": "2026-10-30", "title": "Panaszos vevő (első panasz)",
            "note": "Amint megérkezik az új, varrott Farmer Kalandkönyv: beszéljünk vele, és cseréljük ki a mostanit.",
            "trigger": "új Farmer-szállítmány", "complaint_id": "2026-10-27-1032", "done": False}}), encoding="utf-8")
        [h] = self.m._highlights()
        self.assertEqual((h["id"], h["order_name"], h["complaint_id"], h["created_on"], h["done"]),
                         ("2026-10-30-1032", "#1032", "2026-10-27-1032", "2026-10-30", False))

    def test_broken_file_is_reported_not_sent_as_empty(self):
        self.m.HIGHLIGHTS.write_text("[]", encoding="utf-8")
        value, err = self.m._try("highlights", self.m._highlights)
        self.assertIsNone(value)
        self.assertIn("highlights", err)

if __name__ == "__main__": unittest.main()
