"""The dashboard's "Your keys" box (core/keybox.py, core/keycheck.py, dashboard/app.py /api/keys*): the name a typed label
becomes, the service list, the key log, the write limit, the HTTPS rule, delete, and "Try it". Only made-up keys, in a temp
folder; "Try it" runs against a made-up connector that never leaves the machine.
Run: cd tests && python -m unittest test_keybox"""
import json, os, pathlib, shutil, sys, unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(REPO / "tests"))
os.environ.setdefault("JINNEE_LANG", "en")
import keybox, texts, vault
from test_vault import Base, FAKE_TG, FAKE_SHOP, FAKE_PW

GOOD = "good-made-up-key-0001"
BAD = "wrong-made-up-key-9999"
FAKE_OPS = '''import os
class ConnectorError(Exception):
    pass
def ping():
    key = os.getenv("FAKESVC_API_KEY", "")
    if key != "%s":
        raise ConnectorError("FakeSvc refused the key " + key)
    return {"secret_data": key, "orders": ["made-up"]}
def scopes():
    return {"ok": False, "missing": ["read_made_up"]}
''' % GOOD
FAKE_MANIFEST = {"id": "fakesvc", "name": "FakeSvc", "category": "shop", "auth": {"type": "api_key", "env": "FAKESVC_API_KEY"},
                 "check": {"op": "ping"}}


class Names(unittest.TestCase):
    def test_typed_name_becomes_a_technical_name(self):
        for typed, want in (("Saját CRM api", "SAJAT_CRM_API_KEY"),
                            ("saját crm API kulcs", "SAJAT_CRM_API_KEY"),
                            ("  Árvíztűrő tükörfúrógép ", "ARVIZTURO_TUKORFUROGEP_API_KEY"),
                            ("Partner token", "PARTNER_TOKEN"),
                            ("Futár jelszó", "FUTAR_PASSWORD"),
                            ("hírlevél-szolgáltató / v2", "HIRLEVEL_SZOLGALTATO_V2_API_KEY"),
                            ("123 shop", "KEY_123_SHOP_API_KEY"),
                            ("Raktár api", "RAKTAR_API_KEY")):
            got = keybox.tech_name(typed)
            self.assertEqual(got, want, typed)
            self.assertTrue(vault.NAME_OK.fullmatch(got) and vault.is_secret_name(got), got)

    def test_nothing_usable_is_refused(self):
        for typed in ("", "   ", "!!!", "–", "kulcs"):
            with self.assertRaises(ValueError, msg=typed):
                keybox.tech_name(typed)

    def test_a_long_name_stays_usable(self):
        got = keybox.tech_name("a" * 300)
        self.assertLessEqual(len(got), keybox.NAME_MAX)
        self.assertTrue(got.endswith("_API_KEY"))


class Catalog(unittest.TestCase):
    def test_services_with_human_labels_and_only_existing_checks(self):
        with mock.patch.dict(os.environ, {"JINNEE_LANG": "hu"}):
            cat = {s["id"]: s for s in keybox.catalog(REPO)}
        shop = {k["name"]: k for k in cat["shopify"]["keys"]}
        self.assertEqual(sorted(shop), ["SHOPIFY_ADMIN_TOKEN", "SHOPIFY_CLIENT_SECRET"])  # settings (store, client id) stay in .env
        self.assertEqual(shop["SHOPIFY_ADMIN_TOKEN"]["label"], "Admin hozzáférési token")
        self.assertEqual([k["name"] for k in cat["gls"]["keys"]], ["GLS_PASSWORD"])
        self.assertEqual(cat["jinnee"]["name"], "Jinnee maga")
        checks = {s["id"]: any(k["check"] for k in s["keys"]) for s in cat.values()}
        self.assertEqual(checks, {"billingo": True, "gls": True, "shopify": True, "gmail": False, "szamlazz": False, "jinnee": False})

    def test_every_check_names_a_function_that_exists(self):
        for d in keybox.manifests(REPO):
            if "check" in d:
                self.assertIsNotNone(keybox.check_spec(d, REPO), d["id"])

    def test_a_check_with_no_function_behind_it_gives_no_button(self):
        self.assertIsNone(keybox.check_spec({"id": "shopify", "check": {"op": "no_such_op"}}, REPO))
        self.assertIsNone(keybox.check_spec({"id": "szamlazz", "check": {"op": "anything"}}, REPO))

    def test_label_falls_back_to_the_name(self):
        self.assertEqual(keybox.label("GMAIL_SHOP_REFRESH_TOKEN", "gmail"), "Shop refresh token")
        cat = keybox.catalog(REPO)
        self.assertEqual(keybox.owner_of("GMAIL_SHOP_REFRESH_TOKEN", cat), ("gmail", "Gmail"))
        self.assertEqual(keybox.owner_of("SAJAT_CRM_API_KEY", cat), ("", ""))


class Limit(unittest.TestCase):
    def test_ten_a_minute_per_client(self):
        now = [1000.0]
        lim = keybox.Limiter(10, 60, clock=lambda: now[0])
        self.assertEqual([lim.allow("a") for _ in range(11)], [True] * 10 + [False])
        self.assertTrue(lim.allow("b"))
        now[0] += 60
        self.assertTrue(lim.allow("a"))


class Log(Base):
    def test_lines_have_no_value_and_the_file_is_private(self):
        self.assertNotIn("value", keybox.record.__code__.co_varnames)
        keybox.record("A_TOKEN", "add", "127.0.0.1")
        keybox.record("A_TOKEN", "replace", "127.0.0.1")
        keybox.record("B_TOKEN", "add", "203.0.113.7", "Saját B")
        keybox.record("B_TOKEN", "delete", "203.0.113.7")
        h = keybox.history()
        self.assertEqual(h["A_TOKEN"]["action"], "replace")
        self.assertEqual(h["B_TOKEN"]["action"], "delete")
        lines = [json.loads(l) for l in keybox.audit_path().read_text().splitlines()]
        self.assertEqual(len(lines), 4)
        for l in lines:
            self.assertEqual(set(l) - {"label"}, {"at", "name", "action", "ip", "via"})
        if os.name != "nt":
            self.assertEqual(keybox.audit_path().stat().st_mode & 0o777, 0o600)

    def test_a_broken_line_is_skipped(self):
        keybox.record("A_TOKEN", "add", "127.0.0.1")
        with open(keybox.audit_path(), "a") as fh:
            fh.write("not json\n")
        self.assertEqual(list(keybox.history()), ["A_TOKEN"])

    def test_unknown_action_refused(self):
        with self.assertRaises(ValueError):
            keybox.record("A_TOKEN", "show", "127.0.0.1")


try:
    import fastapi, httpx  # noqa: F401
    HAS_WEB = True
except ImportError:
    HAS_WEB = False


@unittest.skipUnless(HAS_WEB, "needs fastapi and httpx")
class Box(Base):
    def setUp(self):
        super().setUp()
        os.environ["DASHBOARD_PASSWORD"] = ""
        os.environ["JINNEE_LANG"] = "en"
        for n in ("SAJAT_CRM_API_KEY", "FAKESVC_API_KEY"):
            os.environ.pop(n, None)
        sys.path.insert(0, str(REPO / "dashboard"))
        import importlib, app
        self.dash = importlib.reload(app)
        self.dash.ROOT = self.root
        (self.root / "connectors" / "fakesvc").mkdir(parents=True)
        for m in (REPO / "connectors").glob("*.manifest.json"):
            shutil.copy(m, self.root / "connectors")
        (self.root / "connectors" / "fakesvc.manifest.json").write_text(json.dumps(FAKE_MANIFEST))
        (self.root / "connectors" / "fakesvc" / "ops.py").write_text(FAKE_OPS)
        self.p = mock.patch.object(vault, "env_file", return_value=self.envfile)
        self.p.start()
        from fastapi.testclient import TestClient
        self.TestClient = TestClient
        self.c = TestClient(self.dash.app, base_url="http://localhost:8080", client=("127.0.0.1", 50000))
        self.h = {"origin": "http://localhost:8080"}

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def everything_said(self):
        """Every answer the box gives, plus the key log, as one text: a value must be in none of it."""
        out = self.c.get("/api/keys").text + self.c.get("/").text
        a = keybox.audit_path()
        return out + (a.read_text() if a.exists() else "")

    def rows(self):
        return {x["name"]: x for x in self.c.get("/api/keys").json()["keys"]}

    def test_other_name_is_made_by_the_server_and_shown_first(self):
        r = self.c.get("/api/keys/name", params={"label": "Saját CRM api"})
        self.assertEqual(r.json(), {"name": "SAJAT_CRM_API_KEY", "exists": False, "used_by": ""})
        r = self.c.post("/api/keys", json={"label": "Saját CRM api", "value": "crm-made-up-value-123"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "SAJAT_CRM_API_KEY")
        self.assertEqual(vault.get("SAJAT_CRM_API_KEY"), "crm-made-up-value-123")
        row = self.rows()["SAJAT_CRM_API_KEY"]
        self.assertEqual((row["label"], row["service"], row["where"], row["action"]), ("Saját CRM api", "own", "stored", "add"))
        self.assertTrue(row["at"])
        self.assertTrue(self.c.get("/api/keys/name", params={"label": "saját crm api kulcs"}).json()["exists"])
        self.assertEqual(self.c.get("/api/keys/name", params={"label": "!!!"}).status_code, 400)
        self.assertEqual(self.c.post("/api/keys", json={"label": "???", "value": "x"}, headers=self.h).status_code, 400)

    def test_a_value_is_never_in_an_answer_or_the_log(self):
        vals = ["billingo-made-up-123", "billingo-made-up-456", "crm-made-up-789", GOOD, BAD]
        said = [self.c.post("/api/keys", json={"name": "BILLINGO_API_KEY", "value": vals[0]}, headers=self.h).text,
                self.c.post("/api/keys", json={"name": "BILLINGO_API_KEY", "value": vals[1]}, headers=self.h).text,
                self.c.post("/api/keys", json={"label": "My CRM", "value": vals[2]}, headers=self.h).text,
                self.c.post("/api/keys/test", json={"name": "FAKESVC_API_KEY", "value": GOOD}, headers=self.h).text,
                self.c.post("/api/keys/test", json={"name": "FAKESVC_API_KEY", "value": BAD}, headers=self.h).text,
                self.c.delete("/api/keys/MY_CRM_API_KEY", headers=self.h).text]
        text = "\n".join(said) + self.everything_said()
        for v in vals + [FAKE_TG, FAKE_SHOP, FAKE_PW, vals[1][-4:] + '"']:
            self.assertNotIn(v, text)
        log = [json.loads(l) for l in keybox.audit_path().read_text().splitlines()]
        self.assertEqual([(l["name"], l["action"]) for l in log],
                         [("BILLINGO_API_KEY", "add"), ("BILLINGO_API_KEY", "replace"), ("MY_CRM_API_KEY", "add"), ("MY_CRM_API_KEY", "delete")])
        self.assertTrue(all(l["ip"] == "127.0.0.1" and "value" not in l for l in log))
        self.assertEqual(self.rows()["BILLINGO_API_KEY"]["action"], "replace")

    def test_delete_asks_nothing_here_but_needs_same_site_and_a_stored_key(self):
        self.c.post("/api/keys", json={"name": "BILLINGO_API_KEY", "value": "billingo-made-up-123"}, headers=self.h)
        self.assertEqual(self.c.delete("/api/keys/BILLINGO_API_KEY", headers={"origin": "https://evil.example.com"}).status_code, 403)
        self.assertEqual(vault.names(), ["BILLINGO_API_KEY"])
        r = self.c.delete("/api/keys/BILLINGO_API_KEY", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(vault.names(), [])
        self.assertNotIn("BILLINGO_API_KEY", os.environ)
        self.assertEqual(self.rows()["BILLINGO_API_KEY"]["where"], "unset")
        self.assertEqual(self.c.delete("/api/keys/BILLINGO_API_KEY", headers=self.h).status_code, 404)
        self.assertEqual(self.c.delete("/api/keys/bad%20name", headers=self.h).status_code, 400)
        self.assertEqual(keybox.history()["BILLINGO_API_KEY"]["action"], "delete")

    def test_ten_writes_a_minute(self):
        codes = [self.c.post("/api/keys", json={"name": "A_TOKEN", "value": "made-up-%d" % i}, headers=self.h).status_code
                 for i in range(11)]
        self.assertEqual(codes, [200] * 10 + [429])
        self.assertEqual(self.c.delete("/api/keys/A_TOKEN", headers=self.h).status_code, 429)
        self.assertEqual(self.c.post("/api/keys/test", json={"name": "FAKESVC_API_KEY", "value": GOOD}, headers=self.h).status_code, 429)
        self.assertEqual(vault.get("A_TOKEN"), "made-up-9")
        self.assertEqual(self.c.get("/api/keys").status_code, 200)  # reading is not limited

    def client(self, base, peer, **headers):
        self.dash.PASSWORD = "made-up-dashboard-pw"
        return self.TestClient(self.dash.app, base_url=base, client=(peer, 50000)), {"x-dash-key": "made-up-dashboard-pw", **headers}

    def test_plain_http_off_this_machine_is_refused(self):
        c, h = self.client("http://jinnee.example.com", "203.0.113.5")
        k = c.get("/api/keys", headers=h).json()
        self.assertFalse(k["secure"])
        self.assertIn("HTTPS", k["insecure"])
        for r in (c.post("/api/keys", json={"name": "A_TOKEN", "value": "made-up-1"}, headers=h),
                  c.post("/api/keys/test", json={"name": "FAKESVC_API_KEY", "value": GOOD}, headers=h),
                  c.delete("/api/keys/A_TOKEN", headers=h), c.post("/api/keys/import", headers=h)):
            self.assertEqual(r.status_code, 403)
            self.assertIn("HTTPS", r.json()["detail"])
        self.assertEqual(vault.names(), [])
        # a forged X-Forwarded-Proto from outside does not count: only our own proxy may say "https"
        c, h = self.client("http://jinnee.example.com", "203.0.113.5", **{"x-forwarded-proto": "https"})
        self.assertEqual(c.post("/api/keys", json={"name": "A_TOKEN", "value": "made-up-1"}, headers=h).status_code, 403)

    def test_https_direct_or_through_caddy_is_allowed(self):
        c, h = self.client("https://jinnee.example.com", "203.0.113.5")
        self.assertTrue(c.get("/api/keys", headers=h).json()["secure"])
        self.assertEqual(c.post("/api/keys", json={"name": "A_TOKEN", "value": "made-up-1"}, headers=h).status_code, 200)
        c, h = self.client("http://jinnee.example.com", "127.0.0.1", **{"x-forwarded-proto": "https", "x-forwarded-for": "198.51.100.23"})
        self.assertEqual(c.post("/api/keys", json={"name": "B_TOKEN", "value": "made-up-2"}, headers=h).status_code, 200)
        self.assertEqual(keybox.history()["B_TOKEN"]["ip"], "198.51.100.23")
        self.assertEqual(sorted(vault.names()), ["A_TOKEN", "B_TOKEN"])

    def test_try_it_says_only_works_or_not(self):
        rows = self.rows()
        self.assertTrue(rows["FAKESVC_API_KEY"]["check"])
        self.assertFalse(rows["SZAMLAZZ_AGENT_KEY"]["check"])
        r = self.c.post("/api/keys/test", json={"name": "FAKESVC_API_KEY", "value": GOOD}, headers=self.h)
        self.assertEqual(r.json(), {"ok": True, "reason": ""})
        r = self.c.post("/api/keys/test", json={"name": "FAKESVC_API_KEY", "value": BAD}, headers=self.h)
        self.assertFalse(r.json()["ok"])
        self.assertIn("FakeSvc refused the key", r.json()["reason"])
        self.assertNotIn(BAD, r.text)  # the connector put the value into its error: it is cut out
        self.assertEqual(vault.names(), [])  # trying stores nothing
        self.assertNotIn("FAKESVC_API_KEY", os.environ)
        self.assertEqual(self.c.post("/api/keys/test", json={"name": "SZAMLAZZ_AGENT_KEY", "value": "x"}, headers=self.h).status_code, 404)

    def test_try_it_with_no_value_uses_the_stored_key(self):
        self.c.post("/api/keys", json={"name": "FAKESVC_API_KEY", "value": GOOD}, headers=self.h)
        r = self.c.post("/api/keys/test", json={"name": "FAKESVC_API_KEY"}, headers=self.h)
        self.assertEqual(r.json(), {"ok": True, "reason": ""})

    def test_works_but_rights_missing(self):
        m = dict(FAKE_MANIFEST, check={"op": "scopes"})
        (self.root / "connectors" / "fakesvc.manifest.json").write_text(json.dumps(m))
        r = self.c.post("/api/keys/test", json={"name": "FAKESVC_API_KEY", "value": GOOD}, headers=self.h).json()
        self.assertTrue(r["ok"])
        self.assertIn("read_made_up", r["reason"])

    def test_page_has_the_friendly_form(self):
        page = self.c.get("/").text
        for bit in ('id="keysvc"', 'id="keywhich"', 'id="keyown"', 'id="keyval" type="password"', 'autocomplete="new-password"',
                    'id="keyshow"', 'id="keytest" hidden', 'id="keyinsecure"', "API name", "API key"):
            self.assertIn(bit, page)
        self.assertNotIn('list="keynames"', page)  # no more typing the technical name


if __name__ == "__main__":
    unittest.main()
