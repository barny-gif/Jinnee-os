"""The key store (core/vault.py) and how core/env.py reads it. Only made-up keys, in a temp folder.
Run: python -m unittest tests.test_vault"""
import base64, json, os, pathlib, stat, subprocess, sys, tempfile, unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import env, vault

FAKE_TG = "1234567890:" + "A" * 35            # shaped like a bot token, not one
FAKE_SHOP = "shpat_" + "0" * 32
FAKE_PW = "made-up-password-for-tests"

ENV_TEXT = f"""\
# --- Jinnee core
JINNEE_NAME=Jinnee
TELEGRAM_BOT_TOKEN={FAKE_TG}
TELEGRAM_OWNER_ID=123456789
SHOPIFY_STORE=example-shop
SHOPIFY_ADMIN_TOKEN='{FAKE_SHOP}'
GMAIL_CLIENT_ID=client-id-is-not-secret
BILLINGO_API_KEY=
export DASHBOARD_PASSWORD="{FAKE_PW}"   # comment
GLS_PICKUP_ADDRESS=Main street 1
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.root = self.tmp / "install"
        (self.root / "config").mkdir(parents=True)
        self.keyfile = self.tmp / "keys" / "master.key"
        self.envfile = self.root / ".env"
        self.envfile.write_text(ENV_TEXT, encoding="utf-8")
        os.chmod(self.envfile, 0o644)
        names = ("TELEGRAM_BOT_TOKEN", "SHOPIFY_ADMIN_TOKEN", "DASHBOARD_PASSWORD", "BILLINGO_API_KEY", "JINNEE_NAME",
                 "JINNEE_KEY_DIR_HOST", vault.MARK, "A_TOKEN", "B_TOKEN", "C_TOKEN", "X_TOKEN")
        self.env = mock.patch.dict(os.environ, {"JINNEE_VAULT_FILE": str(self.root / "config" / "secrets.vault.json"),
                                                "JINNEE_MASTER_KEY_FILE": str(self.keyfile)})
        self.env.start()
        for n in names:
            os.environ.pop(n, None)
        vault._loaded.clear()

    def tearDown(self):
        self.env.stop()
        vault._loaded.clear()
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def blob(self):
        return (self.root / "config" / "secrets.vault.json").read_text(encoding="utf-8")


class Names(unittest.TestCase):
    def test_which_names_are_secret(self):
        for n in ("TELEGRAM_BOT_TOKEN", "SHOPIFY_ADMIN_TOKEN", "SHOPIFY_CLIENT_SECRET", "GLS_PASSWORD", "BILLINGO_API_KEY",
                  "SZAMLAZZ_AGENT_KEY", "GMAIL_INFO_REFRESH_TOKEN", "DASHBOARD_PASSWORD", "GITHUB_TOKEN"):
            self.assertTrue(vault.is_secret_name(n), n)
        for n in ("TELEGRAM_OWNER_ID", "SHOPIFY_STORE", "GMAIL_CLIENT_ID", "GLS_USERNAME", "JINNEE_MASTER_KEY_FILE",
                  "GLS_PICKUP_EMAIL", "JINNEE_NAME", "REGISTRY_URL", "JINNEE_KEY_DIR", "GMAIL_INFO_MAILBOX"):
            self.assertFalse(vault.is_secret_name(n), n)


class Store(Base):
    def test_round_trip_and_nothing_readable_on_disk(self):
        vault.put("SHOPIFY_ADMIN_TOKEN", FAKE_SHOP)
        self.assertEqual(vault.get("SHOPIFY_ADMIN_TOKEN"), FAKE_SHOP)
        self.assertNotIn(FAKE_SHOP, self.blob())
        self.assertNotIn(base64.b64encode(FAKE_SHOP.encode()).decode(), self.blob())
        self.assertEqual(vault.names(), ["SHOPIFY_ADMIN_TOKEN"])

    def test_files_are_owner_only(self):
        vault.put("X_TOKEN", "abc")
        for p in (self.keyfile, pathlib.Path(os.environ["JINNEE_VAULT_FILE"])):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, p)

    def test_replace_reports_and_keeps_one_entry(self):
        self.assertFalse(vault.put("X_TOKEN", "one"))
        self.assertTrue(vault.put("X_TOKEN", "two"))
        self.assertEqual(vault.get_all(), {"X_TOKEN": "two"})

    def test_wrong_key_is_named_not_garbage(self):
        vault.put("X_TOKEN", "abc")
        self.keyfile.write_text(base64.b64encode(os.urandom(32)).decode())
        with self.assertRaisesRegex(vault.VaultError, "not the one"):
            vault.get_all()

    def test_missing_key_with_a_store_is_an_error_not_a_new_key(self):
        vault.put("X_TOKEN", "abc")
        self.keyfile.unlink()
        with self.assertRaisesRegex(vault.VaultError, "no master key"):
            vault.get_all()
        with self.assertRaises(vault.VaultError):
            vault.put("Y_TOKEN", "def")  # a new key would lock the old entries out for good
        self.assertFalse(self.keyfile.exists())

    def test_swapped_entries_do_not_open(self):
        vault.set_many({"A_TOKEN": "aaa", "B_TOKEN": "bbb"})
        p = pathlib.Path(os.environ["JINNEE_VAULT_FILE"])
        d = json.loads(p.read_text())
        d["entries"]["A_TOKEN"], d["entries"]["B_TOKEN"] = d["entries"]["B_TOKEN"], d["entries"]["A_TOKEN"]
        p.write_text(json.dumps(d))
        with self.assertRaisesRegex(vault.VaultError, "cannot be opened"):
            vault.get_all()

    def test_tampered_value_does_not_open(self):
        vault.put("A_TOKEN", "aaa")
        p = pathlib.Path(os.environ["JINNEE_VAULT_FILE"])
        d = json.loads(p.read_text())
        ct = bytearray(base64.b64decode(d["entries"]["A_TOKEN"]["ct"])); ct[0] ^= 1
        d["entries"]["A_TOKEN"]["ct"] = base64.b64encode(bytes(ct)).decode()
        p.write_text(json.dumps(d))
        with self.assertRaises(vault.VaultError):
            vault.get_all()

    def test_bad_names_and_empty_values_refused(self):
        for n, v in (("1BAD", "x"), ("A-B", "x"), ("OK_TOKEN", "")):
            with self.assertRaises(vault.VaultError):
                vault.put(n, v)

    def test_delete(self):
        vault.put("A_TOKEN", "aaa")
        self.assertTrue(vault.delete("A_TOKEN"))
        self.assertFalse(vault.delete("A_TOKEN"))
        self.assertEqual(vault.names(), [])


class Import(Base):
    def test_moves_only_secrets_and_leaves_settings(self):
        r = vault.import_env(self.envfile)
        self.assertEqual(r["moved"], ["DASHBOARD_PASSWORD", "SHOPIFY_ADMIN_TOKEN", "TELEGRAM_BOT_TOKEN"])
        text = self.envfile.read_text()
        for v in (FAKE_TG, FAKE_SHOP, FAKE_PW):
            self.assertNotIn(v, text)
        for keep in ("JINNEE_NAME=Jinnee", "TELEGRAM_OWNER_ID=123456789", "SHOPIFY_STORE=example-shop",
                     "GMAIL_CLIENT_ID=client-id-is-not-secret", "BILLINGO_API_KEY=", "GLS_PICKUP_ADDRESS=Main street 1"):
            self.assertIn(keep, text)
        self.assertIn("# TELEGRAM_BOT_TOKEN: in the key store", text)
        self.assertEqual(vault.get("SHOPIFY_ADMIN_TOKEN"), FAKE_SHOP)  # quotes and export handled like core/env.py
        self.assertEqual(vault.get("DASHBOARD_PASSWORD"), FAKE_PW)
        self.assertEqual(stat.S_IMODE(self.envfile.stat().st_mode), 0o600)

    def test_no_plain_copy_left_behind(self):
        vault.import_env(self.envfile)
        for f in self.root.rglob("*"):
            if f.is_file():
                t = f.read_text(errors="ignore")
                self.assertNotIn(FAKE_TG, t, f)

    def test_second_import_is_a_no_op(self):
        vault.import_env(self.envfile)
        self.assertEqual(vault.import_env(self.envfile), {"moved": [], "replaced": []})

    def test_hand_edited_env_wins_over_older_stored_value(self):
        vault.put("SHOPIFY_ADMIN_TOKEN", "older-value-123456")
        r = vault.import_env(self.envfile)
        self.assertIn("SHOPIFY_ADMIN_TOKEN", r["replaced"])
        self.assertEqual(vault.get("SHOPIFY_ADMIN_TOKEN"), FAKE_SHOP)

    def test_cli_needs_confirmation(self):
        with mock.patch.object(vault, "env_file", return_value=self.envfile), \
             mock.patch("sys.stdin") as stdin, mock.patch("builtins.print"):
            stdin.isatty.return_value = False
            self.assertEqual(vault._cli(["import"]), 1)
        self.assertIn(FAKE_TG, self.envfile.read_text())  # nothing moved without the yes
        with mock.patch.object(vault, "env_file", return_value=self.envfile), mock.patch("builtins.print") as pr:
            self.assertEqual(vault._cli(["import", "--yes"]), 0)
        self.assertNotIn(FAKE_TG, self.envfile.read_text())
        self.assertNotIn(FAKE_TG, repr(pr.call_args_list))


class EnvLoad(Base):
    def test_env_load_reads_the_store(self):
        vault.import_env(self.envfile)
        names = env.load(self.envfile)
        self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], FAKE_TG)
        self.assertIn("TELEGRAM_BOT_TOKEN", names)
        self.assertEqual(os.environ["JINNEE_NAME"], "Jinnee")

    def test_a_set_variable_still_wins_but_an_empty_one_does_not(self):
        vault.set_many({"TELEGRAM_BOT_TOKEN": FAKE_TG, "BILLINGO_API_KEY": "stored-billingo-key"})
        os.environ["TELEGRAM_BOT_TOKEN"] = "from-docker"
        os.environ["BILLINGO_API_KEY"] = ""      # Docker env_file with an empty BILLINGO_API_KEY= line
        env.load_vault()
        self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "from-docker")
        self.assertEqual(os.environ["BILLINGO_API_KEY"], "stored-billingo-key")

    def test_a_replaced_key_reaches_a_child_run_but_a_docker_value_is_kept(self):
        vault.set_many({"A_TOKEN": "first-value-123", "B_TOKEN": "bbb-value-123", "C_TOKEN": "ccc"})
        os.environ["C_TOKEN"] = "set-by-docker"
        env.load_vault()
        self.assertEqual(os.environ["A_TOKEN"], "first-value-123")
        vault.put("A_TOKEN", "second-value-456")   # the owner replaced it on the dashboard
        vault.delete("B_TOKEN")
        vault.put("C_TOKEN", "stored-c")
        env.load_vault()                            # what the next run (a child process) does
        self.assertEqual(os.environ["A_TOKEN"], "second-value-456")
        self.assertNotIn("B_TOKEN", os.environ)
        self.assertEqual(os.environ["C_TOKEN"], "set-by-docker")
        for n in ("A_TOKEN", "C_TOKEN", vault.MARK):
            os.environ.pop(n, None)

    def test_unopenable_store_warns_once_without_values_and_does_not_crash(self):
        vault.put("A_TOKEN", "secret-value-123456")
        self.keyfile.unlink()
        env._warned.clear()
        with mock.patch("sys.stderr") as err:
            self.assertEqual(env.load_vault(), [])
            self.assertEqual(env.load_vault(), [])
        out = "".join(str(c) for c in err.write.call_args_list)
        self.assertEqual(out.count("no master key"), 1)
        self.assertNotIn("secret-value-123456", out)

    def test_no_store_no_change(self):
        self.assertEqual(env.load_vault(), [])


class Doctor(Base):
    def problems(self, **kw):
        return [m for lv, m in vault.doctor(self.root, **kw) if lv == "problem"]

    def test_plain_env_is_reported_by_name_only(self):
        ps = self.problems()
        joined = " ".join(ps)
        self.assertIn("TELEGRAM_BOT_TOKEN", joined)
        self.assertIn("mode 644", joined)
        for v in (FAKE_TG, FAKE_SHOP, FAKE_PW):
            self.assertNotIn(v, joined)

    def test_clean_after_import(self):
        vault.import_env(self.envfile)
        self.assertEqual(self.problems(), [])

    def test_mistyped_copy_and_a_value_in_a_note_are_found(self):
        vault.import_env(self.envfile)
        (self.root / ".envű").write_text("x=1\n")
        (self.root / "brain").mkdir()
        (self.root / "brain" / "notes.md").write_text("the shop token is %s\n" % FAKE_SHOP)
        joined = " ".join(self.problems())
        self.assertIn(".envű", joined)
        self.assertIn("brain/notes.md", joined)
        self.assertIn("SHOPIFY_ADMIN_TOKEN", joined)
        self.assertNotIn(FAKE_SHOP, joined)

    def test_example_file_is_fine(self):
        vault.import_env(self.envfile)
        (self.root / ".env.example").write_text("TELEGRAM_BOT_TOKEN=\n")
        self.assertEqual(self.problems(), [])

    def test_key_inside_install_folder_is_a_note(self):
        os.environ["JINNEE_MASTER_KEY_FILE"] = str(self.root / "keys" / "master.key")
        vault.import_env(self.envfile)
        notes = [m for lv, m in vault.doctor(self.root) if lv == "note"]
        self.assertTrue(any("inside the install folder" in n for n in notes))

    def test_docker_relative_key_dir_is_a_note(self):
        os.environ["JINNEE_KEY_DIR_HOST"] = "./keys"
        notes = [m for lv, m in vault.doctor(self.root, deep=False) if lv == "note"]
        self.assertTrue(any("inside the install folder" in n for n in notes))

    def test_unopenable_store_is_a_problem(self):
        vault.import_env(self.envfile)
        self.keyfile.write_text(base64.b64encode(os.urandom(32)).decode())
        self.assertTrue(any("cannot be opened" in p for p in self.problems(deep=False)))


try:
    import fastapi, httpx  # noqa: F401
    HAS_WEB = True
except ImportError:
    HAS_WEB = False


@unittest.skipUnless(HAS_WEB, "needs fastapi and httpx")
class DashboardKeys(Base):
    def setUp(self):
        super().setUp()
        os.environ["DASHBOARD_PASSWORD"] = ""
        os.environ["JINNEE_LANG"] = "en"
        sys.path.insert(0, str(REPO / "dashboard"))
        import importlib, app
        self.dash = importlib.reload(app)
        self.dash.ROOT = self.root
        (self.root / "connectors").mkdir()
        import shutil
        shutil.copy(REPO / "connectors" / "shopify.manifest.json", self.root / "connectors")
        self.p = mock.patch.object(vault, "env_file", return_value=self.envfile)
        self.p.start()
        from fastapi.testclient import TestClient
        self.c = TestClient(self.dash.app, base_url="http://localhost:8080", client=("127.0.0.1", 50000))
        self.h = {"origin": "http://localhost:8080"}

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def test_lists_names_and_where_never_values(self):
        r = self.c.get("/api/keys")
        self.assertEqual(r.status_code, 200)
        rows = {x["name"]: x for x in r.json()["keys"]}
        self.assertEqual(rows["TELEGRAM_BOT_TOKEN"]["where"], "plain")
        self.assertEqual(rows["SHOPIFY_ADMIN_TOKEN"]["used_by"], "Shopify")
        self.assertIn("TELEGRAM_BOT_TOKEN", r.json()["plain"])
        for v in (FAKE_TG, FAKE_SHOP, FAKE_PW):
            self.assertNotIn(v, r.text)

    def test_set_stores_encrypted_and_does_not_echo(self):
        r = self.c.post("/api/keys", json={"name": "BILLINGO_API_KEY", "value": "billingo-made-up-123"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("billingo-made-up-123", r.text)
        self.assertEqual(vault.get("BILLINGO_API_KEY"), "billingo-made-up-123")
        self.assertNotIn("billingo-made-up-123", self.blob())
        rows = {x["name"]: x for x in self.c.get("/api/keys").json()["keys"]}
        self.assertEqual(rows["BILLINGO_API_KEY"]["where"], "stored")

    def test_bad_input_and_cross_site_are_refused(self):
        self.assertEqual(self.c.post("/api/keys", json={"name": "bad name", "value": "x"}, headers=self.h).status_code, 400)
        self.assertEqual(self.c.post("/api/keys", json={"name": "A_TOKEN", "value": ""}, headers=self.h).status_code, 400)
        r = self.c.post("/api/keys", json={"name": "A_TOKEN", "value": "x"}, headers={"origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(vault.names(), [])

    def test_import_button_moves_env(self):
        r = self.c.post("/api/keys/import", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["moved"], ["DASHBOARD_PASSWORD", "SHOPIFY_ADMIN_TOKEN", "TELEGRAM_BOT_TOKEN"])
        self.assertNotIn(FAKE_TG, self.envfile.read_text())
        self.assertEqual(self.c.get("/api/keys").json()["plain"], [])

    def test_page_has_the_box_in_both_languages(self):
        self.assertIn('id="keyform"', self.c.get("/").text)
        for lang in ("en", "hu"):
            d = json.loads((REPO / "core" / "locale" / f"{lang}.json").read_text(encoding="utf-8"))
            for k in ("keys", "key_name", "key_value", "key_save", "key_hint", "where_stored", "where_plain", "where_unset",
                      "keys_plain", "key_import", "key_import_confirm", "key_imported", "key_bad"):
                self.assertIn("dash." + k, d, lang)


class Repo(unittest.TestCase):
    def test_store_and_key_never_go_to_git_or_the_image(self):
        gi = (REPO / ".gitignore").read_text()
        di = (REPO / ".dockerignore").read_text()
        for line in ("config/secrets.vault.json*", "keys/"):
            self.assertIn(line, gi.splitlines())
            self.assertIn(line, di.splitlines())

    def test_compose_mounts_the_key_folder_and_env(self):
        dc = (REPO / "docker-compose.yml").read_text()
        self.assertIn("${JINNEE_KEY_DIR:-./keys}:/run/jinnee-keys", dc)
        self.assertIn("./.env:/app/.env", dc)
        self.assertIn("cryptography", (REPO / "requirements.txt").read_text())


if __name__ == "__main__":
    unittest.main()
