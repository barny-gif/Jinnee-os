"""config/business.yaml (core/business.py): reading, checking, the label note, writing it from the installer's answers.
Made-up businesses only. Run: python3 -m unittest discover tests"""
import json, os, pathlib, sys, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core")); sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "tests"))
import business, fake_business
from cbase import ConnectorError


def using(text):
    return mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": str(fake_business.write(text))})


class Reading(unittest.TestCase):
    def test_example_file_is_complete_and_made_up(self):
        with mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": str(ROOT / "config" / "business.example.yaml")}):
            self.assertEqual(business.check(["general", "ecom"]), [])
            self.assertEqual(business.label_note("#1001", "1× Wooden puzzle", 9890), "example-shop.hu #1001, 1× Wooden puzzle, COD 9890 Ft")

    def test_missing_file_names_the_path_and_the_fix(self):
        with mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": "/nonexistent/business.yaml"}):
            with self.assertRaisesRegex(business.BusinessConfigError, "does not exist.*Nothing was sent.*business.py setup"):
                business.label_note("#1", "x")

    def test_each_missing_or_empty_key_is_named(self):
        for key, old, new in (("shop.label_name", '  label_name: "example-shop.hu"\n', ""),
                              ("shop.label_name", '"example-shop.hu"', '""'),
                              ("labels.note", '  note: "{shop} {order}, {goods}, {payment}"\n', ""),
                              ("labels.cod", '  cod: "COD {amount} Ft"\n', "")):
            with self.subTest(key=key, new=new), using(fake_business.YAML.replace(old, new)):
                with self.assertRaisesRegex(business.BusinessConfigError, f"'{key}' is (missing|empty)"):
                    business.label_note("#1", "x", 100)

    def test_templates_are_checked(self):
        for old, new, msg in (("{goods}", "{customer_name}", "unknown placeholder"), ("{order}", "", "must contain \\{order\\}"),
                              ("COD {amount} Ft", "COD Ft", "must contain \\{amount\\}"), ("{payment}", "{payment", "not a valid template")):
            with self.subTest(new=new), using(fake_business.YAML.replace(old, new, 1)):
                with self.assertRaisesRegex(business.BusinessConfigError, msg): business.label_note("#1", "x")

    def test_not_yaml_or_wrong_types(self):
        for text, msg in (("business: [unclosed", "cannot be read as YAML"), ("", "empty"),
                          (fake_business.YAML.replace("not_revenue_tags: [", "not_revenue_tags: {x: ").replace("']", "'}"), "must be a list")):
            with self.subTest(text=text[:20]), using(text):
                with self.assertRaisesRegex(business.BusinessConfigError, msg):
                    business.label_note("#1", "x"); business.not_revenue_tags()

    def test_an_edit_is_seen_without_a_restart(self):
        p = fake_business.write()
        with mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": str(p)}):
            self.assertTrue(business.label_note("#1", "").startswith("example-shop.hu"))
            p.write_text(fake_business.YAML.replace("example-shop.hu", "other-shop.hu") + "\n# edited\n", encoding="utf-8")
            self.assertTrue(business.label_note("#1", "").startswith("other-shop.hu"))


class LabelNote(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": str(fake_business.write())}); p.start(); self.addCleanup(p.stop)

    def test_paid_cod_and_no_goods(self):
        self.assertEqual(business.label_note("#1001", "2× Fa kirakó", 0), "example-shop.hu #1001, 2× Fa kirakó, PAID")
        self.assertEqual(business.label_note("#1001", "2× Fa kirakó", 10380.0), "example-shop.hu #1001, 2× Fa kirakó, COD 10380 Ft")
        self.assertEqual(business.label_note("#1001", "", 0), "example-shop.hu #1001, PAID")  # the empty part goes with its comma

    def test_long_note_is_cut_to_the_label_limit(self):
        out = business.label_note("#1001", "1× " + "Hosszú termék " * 30, 9890)
        self.assertEqual(len(out), business.LABEL_MAX); self.assertTrue(out.endswith("…"))

    def test_own_wording(self):
        text = fake_business.YAML.replace('note: "{shop} {order}, {goods}, {payment}"', 'note: "{order} / {shop}, {payment}"') \
                                 .replace('paid: "PAID"', 'paid: "Fizetve"')
        with using(text):
            self.assertEqual(business.label_note("#7", "ignored", 0), "#7 / example-shop.hu, Fizetve")

    def test_a_brace_in_a_product_name_is_only_text(self):
        self.assertEqual(business.label_note("#1", "1× Kocka {xl}", 0), "example-shop.hu #1, 1× Kocka {xl}, PAID")


class NothingGoesOutWithoutIt(unittest.TestCase):
    def test_label_upload_stops_before_mygls(self):
        import importlib.util
        s = importlib.util.spec_from_file_location("t_biz_flow", ROOT / "connectors" / "order_flow.py")
        flow = importlib.util.module_from_spec(s); s.loader.exec_module(flow)
        order = {"name": "#1001", "id": 1, "currency": "HUF", "gateways": ["Cash on Delivery (COD)"], "email": "", "phone": "06 30 111 2222",
                 "billing": {}, "shipping": {"name": "Teszt Elek", "zip": "9999", "city": "Mintaváros", "address1": "Fő u. 1", "country_code": "HU"},
                 "lines": [{"title": "Fa kirakó", "qty": 1, "price": "5000", "discount": "0"}], "shipping_lines": [], "total": "5000"}
        shop, gls = mock.Mock(order_private=lambda i: order), mock.Mock(client_number=lambda: 1)
        env = dict(GLS_PICKUP_NAME="Example Shop", GLS_PICKUP_STREET="Fő u.", GLS_PICKUP_CITY="Mintaváros", GLS_PICKUP_ZIP="9999",
                   JINNEE_BUSINESS_FILE="/nonexistent/business.yaml")
        with mock.patch.dict(os.environ, env), mock.patch.object(flow, "UPLOADED", pathlib.Path(fake_business.write("")).with_suffix(".json")):
            with self.assertRaisesRegex(ConnectorError, "business.yaml"):
                flow.parcel_prepare("1", send="yes", shop=shop, gls=gls)
        gls.prepare_labels.assert_not_called()


class Prompt(unittest.TestCase):
    def test_business_block_and_missing_setup(self):
        with using(fake_business.YAML):
            block = business.prompt_block()
        for part in ("Business name: Example Kft.", "language with the owner and the customers: hu", "Warm and informal", "wooden toys"):
            self.assertIn(part.lower(), block.lower())
        with mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": "/nonexistent/business.yaml"}):
            self.assertTrue(business.prompt_block().startswith("SETUP MISSING"))

    def test_language_env_override_then_file_then_default(self):
        with using(fake_business.YAML.replace('language: "hu"', 'language: "de"')):
            with mock.patch.dict(os.environ, {"JINNEE_LANG": "en"}): self.assertEqual(business.language(), "en")
            with mock.patch.dict(os.environ, {"JINNEE_LANG": ""}): self.assertEqual(business.language(), "de")
        with mock.patch.dict(os.environ, {"JINNEE_LANG": "", "JINNEE_BUSINESS_FILE": "/nonexistent/business.yaml"}):
            self.assertEqual(business.language(), "en")

    def test_check_asks_for_shop_keys_only_with_ecom(self):
        general_only = 'business:\n  name: "A"\n  language: "hu"\n  voice: "calm"\n'
        with using(general_only):
            self.assertEqual(business.check(["general"]), [])
            probs = business.check(["general", "ecom"])
        self.assertTrue(any("shop.label_name" in p for p in probs) and any("invoice_prefix" in p for p in probs), probs)


class Writing(unittest.TestCase):
    def setUp(self):
        self.target = pathlib.Path(fake_business.write("")).with_name("written.yaml")
        self.target.unlink(missing_ok=True)
        p = mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": str(self.target)}); p.start(); self.addCleanup(p.stop)

    ANSWERS = {"BUSINESS_NAME": 'Kis "Bolt" Bt. #1: \\ a: b', "BUSINESS_LANGUAGE": "hu", "BUSINESS_VOICE": "Tegező, rövid mondatok, felkiáltójel nélkül",
               "BUSINESS_INTRO": "Fa játékok - kézzel\nkészítve", "SHOP_LABEL_NAME": "kis-bolt.hu", "SHOP_NOT_REVENUE_TAGS": "teszt, ajándék,,",
               "BILLING_INVOICE_PREFIX": "KB-", "PACKS": "general,ecom"}

    def test_init_round_trips_any_text(self):
        with mock.patch.dict(os.environ, self.ANSWERS):
            business.init()
            self.assertEqual(business.check(["general", "ecom"]), [])
            data = business.load()
            self.assertEqual(data["business"]["name"], self.ANSWERS["BUSINESS_NAME"])
            self.assertEqual(data["business"]["intro"], self.ANSWERS["BUSINESS_INTRO"])
            self.assertEqual(business.not_revenue_tags(), ["teszt", "ajándék"])
            self.assertEqual(business.invoice_prefix(), "KB-")
            self.assertEqual(business.label_note("#1001", "1× Fa vonat", 4990), "kis-bolt.hu #1001, 1× Fa vonat, COD 4990 Ft")

    def test_init_writes_the_schedule_block(self):
        sys.path.insert(0, str(ROOT / "core"))
        import alerts, scheduler
        for given, zone in (("", "Europe/Budapest"), ("Europe/Vienna", "Europe/Vienna")):
            with self.subTest(given=given), mock.patch.dict(os.environ, {**self.ANSWERS, "BUSINESS_TIMEZONE": given, "JINNEE_TZ": ""}):
                os.environ.pop("QUIET_HOURS", None)
                business.init(force="yes")
                data = business.load()
                self.assertEqual((data["schedule"]["timezone"], data["schedule"]["jobs"]), (zone, {}))
                self.assertNotIn("quiet_hours", data["schedule"])  # only as a comment: QUIET_HOURS in .env is the older place
                self.assertEqual(alerts.zone_name(), zone)
                self.assertEqual(scheduler.Scheduler(pathlib.Path(self.target).parent / "brain-x", ["general", "ecom"]).check(), [])

    def test_init_refuses_missing_answers_and_overwriting(self):
        with mock.patch.dict(os.environ, {**self.ANSWERS, "SHOP_LABEL_NAME": ""}):
            with self.assertRaisesRegex(business.BusinessConfigError, "SHOP_LABEL_NAME"): business.init()
        self.assertFalse(self.target.exists())
        with mock.patch.dict(os.environ, {**self.ANSWERS, "SHOP_LABEL_NAME": "", "PACKS": "general"}):
            business.init()  # without the ecom pack the shop name is not needed
            with self.assertRaisesRegex(business.BusinessConfigError, "already exists"): business.init()
            business.init(force="yes")

    def test_setup_asks_and_writes(self):
        answers = iter(["", "Minta Kft.", "", "barátságos", "", "", "minta.hu", "teszt", ""])  # first empty name is asked again; Enter = suggested zone
        with mock.patch.dict(os.environ, {"PACKS": "general,ecom"}), mock.patch("builtins.print"):
            business.setup(ask=lambda q: next(answers))
            self.assertEqual(business.check(["general", "ecom"]), [])
            self.assertEqual((business.text("business.name"), business.text("business.language")), ("Minta Kft.", "hu"))
            self.assertEqual(business.invoice_prefix(), "")

    def test_cli_check_and_label(self):
        import io, contextlib
        with mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": str(fake_business.write()), "PACKS": "general,ecom"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(business.main(["check"]), 0)
                self.assertEqual(business.main(["label", "--order", "#1001", "--goods", "1× Fa kirakó", "--cod", "9890"]), 0)
            self.assertEqual(out.getvalue().splitlines()[-1], "example-shop.hu #1001, 1× Fa kirakó, COD 9890 Ft")
        err = io.StringIO()
        with contextlib.redirect_stderr(err): self.assertEqual(business.main(["label"]), 1)
        self.assertIn("does not exist", err.getvalue())


if __name__ == "__main__":
    unittest.main()
