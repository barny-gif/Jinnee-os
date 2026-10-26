"""Manual (non-Shopify) parcel: a barter / influencer / gift / test parcel has no COD, and its kind stands on the label
where PAID would. Only the label note is built here: nothing is sent, no GLS client is loaded."""
import pathlib, sys, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core")); sys.path.insert(0, str(ROOT / "tests")); sys.path.insert(0, str(ROOT / "connectors"))
import fake_business
fake_business.use()
import parcel_manual as pm
from cbase import ConnectorError

SHOP = fake_business.SHOP


class Note(unittest.TestCase):
    def test_paid_and_cod_unchanged(self):
        self.assertEqual(pm.note("1000000001", "1× Fa kirakó", 0), f"{SHOP} 1000000001, 1× Fa kirakó, PAID")
        self.assertEqual(pm.note("1000000001", "1× Fa kirakó", 9890), f"{SHOP} 1000000001, 1× Fa kirakó, COD 9890 Ft")

    def test_kind_replaces_paid(self):
        self.assertEqual(pm.note("Influencer-1", "1× Fa kirakó", 0, "barter"), f"{SHOP} Influencer-1, 1× Fa kirakó, BARTER")
        self.assertEqual(pm.note("A-2", "1× Fa kirakó", 0, "gift"), f"{SHOP} A-2, 1× Fa kirakó, GIFT")


class Prepare(unittest.TestCase):
    ARGS = dict(channel="Barter", ref="Influencer-1", goods="1× Fa kirakó", name="Teszt Elek", zip="1000", city="Budapest",
                street="Fő utca", house="1", phone="06301234567")

    def test_kind_with_cod_refused(self):
        with self.assertRaises(ConnectorError):
            pm.prepare(cod="5000", kind="barter", **self.ARGS)

    def test_unknown_kind_refused(self):
        with self.assertRaises(ConnectorError):
            pm.prepare(cod="0", kind="discount", **self.ARGS)


if __name__ == "__main__":
    unittest.main()
