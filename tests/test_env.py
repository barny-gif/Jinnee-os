""".env loading for native installs. Run: python -m unittest discover tests
The end-to-end tests start the entry points the way install.sh does, from a copy of the repo in a temp directory."""
import http.client, json, os, pathlib, shutil, socket, subprocess, sys, tempfile, time, unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import env

SAMPLE = """\
# --- a comment line
JINNEE_NAME=Jinnee
JINNEE_LANG=hu             # language Jinnee speaks with the owner
TELEGRAM_BOT_TOKEN=
DASHBOARD_PASSWORD=        # empty, with a comment
  export SPACED = padded value
HASH_INSIDE=abc#123
HASH_FIRST=#abc
DOUBLE="two words # not a comment"  # comment
SINGLE='it is $literal \\n'
ESCAPED="line1\\nline2 \\"q\\""
UNCLOSED="half
URL=https://example.com/a?b=c#frag
not a variable line
=novalue
"""


class Parse(unittest.TestCase):
    def test_plain_empty_and_comments(self):
        got = env.parse(SAMPLE)
        self.assertEqual(got["JINNEE_NAME"], "Jinnee")
        self.assertEqual(got["JINNEE_LANG"], "hu")
        self.assertEqual(got["TELEGRAM_BOT_TOKEN"], "")
        self.assertEqual(got["DASHBOARD_PASSWORD"], "")
        self.assertEqual(got["SPACED"], "padded value")
        self.assertEqual(got["HASH_INSIDE"], "abc#123")
        self.assertEqual(got["HASH_FIRST"], "#abc")
        self.assertEqual(got["URL"], "https://example.com/a?b=c#frag")
        self.assertEqual(len(got), 12)  # the comment, the prose line and "=novalue" are skipped

    def test_quotes(self):
        got = env.parse(SAMPLE)
        self.assertEqual(got["DOUBLE"], "two words # not a comment")
        self.assertEqual(got["SINGLE"], "it is $literal \\n")
        self.assertEqual(got["ESCAPED"], 'line1\nline2 "q"')
        self.assertEqual(got["UNCLOSED"], '"half')

    def test_windows_line_ends_and_bom(self):
        self.assertEqual(env.parse("A=1\r\nB=two  # c\r\n"), {"A": "1", "B": "two"})
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / ".env"
            p.write_bytes(b"\xef\xbb\xbfJOS_T_BOM=yes\r\nJOS_T_BAD=\xff\xfe\r\n")
            with mock.patch.dict(os.environ):
                self.assertEqual(env.load(p), ["JOS_T_BOM", "JOS_T_BAD"])
                self.assertEqual(os.environ["JOS_T_BOM"], "yes")

    def test_env_example_parses(self):
        got = env.parse((REPO / ".env.example").read_text(encoding="utf-8"))
        self.assertEqual((got["JINNEE_LANG"], got["PACKS"], got["DASHBOARD_PASSWORD"]), ("hu", "general", ""))


class Load(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = pathlib.Path(self.tmp.name) / ".env"
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("JOS_T_NEW", "JOS_T_SET", "JOS_T_EMPTY"): os.environ.pop(k, None)

    def test_sets_what_is_missing(self):
        self.file.write_text("JOS_T_NEW=from file  # c\n")
        self.assertEqual(env.load(self.file), ["JOS_T_NEW"])
        self.assertEqual(os.environ["JOS_T_NEW"], "from file")

    def test_existing_variable_is_not_overwritten(self):
        os.environ.update(JOS_T_SET="from shell", JOS_T_EMPTY="")
        self.file.write_text("JOS_T_SET=from file\nJOS_T_EMPTY=from file\nJOS_T_NEW=x\n")
        self.assertEqual(env.load(self.file), ["JOS_T_NEW"])
        self.assertEqual(os.environ["JOS_T_SET"], "from shell")
        self.assertEqual(os.environ["JOS_T_EMPTY"], "")  # set to empty counts as set (Docker env_file does this)

    def test_missing_file_is_fine(self):
        self.assertEqual(env.load(self.file), [])

    def test_default_path_is_the_repo_root(self):
        self.assertEqual(env.ROOT, REPO)


STUBS = {  # stand-ins so jinnee.py starts without the Telegram library, the network or a real token
    "telegram/__init__.py": "class Update: pass\n",
    "telegram/ext.py": (
        "import json\n"
        "class _Any:\n"
        "    def __init__(self, *a, **k): pass\n"
        "    def __getattr__(self, n): return _Any()\n"
        "    def __and__(self, o): return self\n"
        "    def __invert__(self): return self\n"
        "MessageHandler = CommandHandler = _Any\n"
        "filters = _Any()\n"
        "class ContextTypes: DEFAULT_TYPE = object\n"
        "class _Jobs:\n"
        "    def run_daily(self, callback, time): print('STUB_DAILY ' + callback.__name__ + ' ' + str(time))\n"
        "class _App:\n"
        "    job_queue = _Jobs()\n"
        "    def add_handler(self, h): pass\n"
        "    def run_polling(self): pass\n"
        "class ApplicationBuilder:\n"
        "    def token(self, t): print('STUB_TOKEN ' + json.dumps(t)); return self\n"
        "    def build(self): return _App()\n"),
    "requests.py": "def get(*a, **k): raise RuntimeError('no network in tests')\n",
}
OURS = ("JINNEE_NAME", "JINNEE_LANG", "PACKS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "REGISTRY_URL",
        "DASHBOARD_PASSWORD", "DASHBOARD_HOST", "DASHBOARD_PORT", "DASHBOARD_TRUST_PEER")


class RepoCopy(unittest.TestCase):
    """A copy of the repo with its own .env, started with none of our variables in the environment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "jinnee-os"
        for d in ("core", "dashboard", "packs", "connectors"):
            shutil.copytree(REPO / d, self.root / d, ignore=shutil.ignore_patterns("__pycache__"))
        stubs = pathlib.Path(self.tmp.name) / "stubs"
        for name, src in STUBS.items():
            (stubs / name).parent.mkdir(parents=True, exist_ok=True)
            (stubs / name).write_text(src)
        self.env = {k: v for k, v in os.environ.items() if k not in OURS}
        self.env.update(PYTHONPATH=os.pathsep.join([str(stubs)] + [p for p in sys.path if p]), PYTHONDONTWRITEBYTECODE="1")

    def dotenv(self, text):
        (self.root / ".env").write_text(text, encoding="utf-8")

    def run_py(self, script, extra=None, *args):
        return subprocess.run([sys.executable, script, *args], cwd=self.root, env={**self.env, **(extra or {})},
                              capture_output=True, text=True, timeout=60)


class Installed(RepoCopy):
    def start_dashboard(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
        with (self.root / ".env").open("a", encoding="utf-8") as f:
            f.write(f"\nDASHBOARD_HOST=127.0.0.1\nDASHBOARD_PORT={port}   # picked by the test\n")
        proc = subprocess.Popen([sys.executable, "dashboard/app.py"], cwd=self.root, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))
        for _ in range(100):
            if proc.poll() is not None: self.fail("dashboard exited:\n" + proc.stdout.read())
            try: socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); return port
            except OSError: time.sleep(0.1)
        self.fail("dashboard did not start listening")

    def get(self, port, path, headers=None):
        c = http.client.HTTPConnection("localhost", port, timeout=10)
        self.addCleanup(c.close)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.getheader("location"), r.read().decode()

    def test_dashboard_password_from_dotenv(self):
        self.dotenv('JINNEE_NAME=Testbot\nDASHBOARD_PASSWORD="s3cret pass"   # set by the owner\n')
        port = self.start_dashboard()
        self.assertEqual(self.get(port, "/")[:2], (303, "/login"))
        self.assertEqual(self.get(port, "/api/state")[0], 401)
        status, _, body = self.get(port, "/api/state", {"x-dash-key": "s3cret pass"})
        self.assertEqual(status, 200)
        self.assertEqual((json.loads(body)["auth"], json.loads(body)["name"]), (True, "Testbot"))

    def test_dashboard_without_password_stays_local_and_open(self):
        self.dotenv("JINNEE_NAME=Testbot\nDASHBOARD_PASSWORD=\n")
        port = self.start_dashboard()
        status, _, body = self.get(port, "/api/state")
        self.assertEqual((status, json.loads(body)["auth"]), (200, False))

    def test_bridge_gets_token_name_and_packs_from_dotenv(self):
        self.dotenv("TELEGRAM_BOT_TOKEN=123456:TEST-not-a-real-token\nTELEGRAM_OWNER_ID=4242\n"
                    "JINNEE_NAME=Testbot\nPACKS=general,ecom   # two packs\n")
        r = self.run_py("core/jinnee.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('STUB_TOKEN "123456:TEST-not-a-real-token"', r.stdout)
        self.assertIn("Testbot is running. Packs: general, ecom", r.stdout)

    def test_bridge_without_dotenv_has_no_token(self):  # what a native install did before
        r = self.run_py("core/jinnee.py")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TELEGRAM_BOT_TOKEN is not set", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_pack_loader_init(self):
        self.dotenv("PACKS=general,ecom\n")
        from_file = self.run_py("core/pack_loader.py")
        self.assertEqual(from_file.returncode, 0, from_file.stderr)
        both = json.loads(from_file.stdout)["agents"]
        r = self.run_py("core/pack_loader.py", {"PACKS": "general"})  # the installer's `PACKS=… python3 …` form wins
        only_general = json.loads(r.stdout)["agents"]
        self.assertGreater(len(both), len(only_general))
        r = subprocess.run([sys.executable, "core/pack_loader.py", "--init"], cwd=self.root, env={**self.env, "PACKS": "general"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("brain/ ready. Agents: " + ", ".join(only_general), r.stdout)
        self.assertTrue((self.root / "brain" / "approvals.json").exists())


if __name__ == "__main__":
    unittest.main()
