"""A scheduled run has nobody watching it (JINNEE_UNATTENDED=1): nothing goes out from it, whatever the autonomy level says.
Also the two count-only connector ops the scheduler asks before an hourly run. Fake HTTP only. Run: python3 -m unittest discover tests"""
import importlib.util, io, json, os, pathlib, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "core"))
import cbase, approvals, autonomy
from cbase import ConnectorError

OFF = {"JINNEE_UNATTENDED": ""}
ON = {"JINNEE_UNATTENDED": "1"}


def load(path, name):
    s = importlib.util.spec_from_file_location(name, ROOT / "connectors" / path)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


class Resp:
    def __init__(self, data=None, code=200): self.data, self.status_code, self.ok, self.content = data or {}, code, code < 400, b"x"
    def json(self): return self.data


def cli(ops, argv):
    out = io.StringIO()
    with redirect_stdout(out): code = cbase.run_cli(ops, argv)
    return code, out.getvalue()


class ConnectorGuard(unittest.TestCase):
    """cbase.run_cli is the door every connector CLI goes through."""
    def setUp(self):
        self.calls = []
        self.ops = {"send": lambda **k: self.calls.append(("send", k)) or {"sent": k.get("confirm") == "yes"},
                    "parcel_prepare": lambda **k: self.calls.append(("parcel", k)) or {"dry_run": k.get("send") != "yes"},
                    "loop": lambda **k: self.calls.append(("loop", k)) or {}}

    def test_outgoing_steps_are_refused_in_a_scheduled_run(self):
        with mock.patch.dict(os.environ, ON):
            for argv in (["send", "--draft_id", "d1", "--confirm", "yes"], ["send", "--confirm", "YES"],
                         ["parcel_prepare", "--order_id", "1", "--send", "yes"], ["parcel_prepare", "--send", " yes "], ["loop"]):
                with self.subTest(argv=argv):
                    code, out = cli(self.ops, argv)
                    self.assertEqual(code, 1); self.assertIn("REFUSED", json.loads(out)["error"])
        self.assertEqual(self.calls, [])  # the op itself never ran

    def test_dry_runs_still_work_in_a_scheduled_run(self):
        with mock.patch.dict(os.environ, ON):
            self.assertEqual(cli(self.ops, ["send", "--draft_id", "d1"])[0], 0)
            self.assertEqual(cli(self.ops, ["parcel_prepare", "--order_id", "1", "--send", "no"])[0], 0)
        self.assertEqual([c[0] for c in self.calls], ["send", "parcel"])

    def test_by_hand_nothing_changes(self):
        with mock.patch.dict(os.environ, OFF):
            code, out = cli(self.ops, ["send", "--draft_id", "d1", "--confirm", "yes"])
        self.assertEqual(code, 0); self.assertEqual(json.loads(out), {"sent": True})

    def test_a_real_connector_refuses_from_its_own_command_line(self):
        env = {**os.environ, "JINNEE_UNATTENDED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        for argv in (["gmail/ops.py", "send", "--account", "info", "--draft_id", "d1", "--confirm", "yes"],
                     ["order_flow.py", "invoice_issue", "--draft_id", "1", "--confirm", "yes"],
                     ["order_flow.py", "parcel_prepare", "--order_id", "1", "--send", "yes"],
                     ["parcel_manual.py", "prepare", "--send", "yes"],
                     ["shopify/ops.py", "fulfill", "--order_id", "1", "--tracking", "1", "--confirm", "yes"],
                     ["ecom_sync.py", "push", "--send", "yes"], ["ecom_sync.py", "loop"]):
            with self.subTest(argv=argv):
                r = subprocess.run([sys.executable, str(ROOT / "connectors" / argv[0]), *argv[1:]], capture_output=True, text=True,
                                   env=env, timeout=60, cwd=tempfile.gettempdir())
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("REFUSED", json.loads(r.stdout)["error"])


class ApprovalGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.brain = pathlib.Path(self.tmp.name) / "brain"; self.brain.mkdir()
        self.book = approvals.Book(self.brain)
        self.book.gate = autonomy.Config(self.brain, ["general", "ecom"])

    def test_a_scheduled_run_cannot_take_a_decision(self):
        a = self.book.add("echo", "send_customer_email", "Reply", text="Finished reply.")[0]["id"]
        self.book.decide(a, "ok")
        with mock.patch.dict(os.environ, ON):
            v = self.book.consume(a)
        self.assertEqual(v.word, "REFUSED"); self.assertIn("scheduled run", v.text)
        self.assertEqual(self.book.get(a)["state"], "decided")  # untouched: the bridge still hands it over
        with mock.patch.dict(os.environ, OFF):
            self.assertEqual(self.book.consume(a).word, "GO")

    def test_a_scheduled_run_cannot_apply_a_level_change(self):
        cfg = autonomy.Config(self.brain, ["general", "ecom"])
        item = cfg.propose("send_customer_email", 2, "twenty replies approved unchanged")
        self.book.decide(item["id"], "ok")
        with mock.patch.dict(os.environ, ON), mock.patch.object(approvals.autonomy.Config, "defaults", cfg.defaults):
            with self.assertRaises(autonomy.Refused): cfg.apply(item["id"])
        self.assertEqual(cfg.allowed("send_customer_email").level, 1)

    def test_queueing_for_the_owner_still_works(self):
        with mock.patch.dict(os.environ, ON):
            it, _ = self.book.add("echo", "send_customer_email", "Reply", text="Finished reply.")
        self.assertEqual(it["state"], "pending")


class CountOnly(unittest.TestCase):
    def test_gmail_new_since_counts_inbox_mail_after_the_time(self):
        env = {"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "sec", "GMAIL_ACCOUNTS": "info",
               "GMAIL_INFO_ADDRESS": "info@example-shop.hu", "GMAIL_INFO_REFRESH_TOKEN": "r-info"}
        with mock.patch.dict(os.environ, env):
            m = load("gmail/ops.py", "t_unattended_gmail")
            seen = []
            def req(method, url, params=None, json=None, timeout=None, headers=None):
                seen.append((method, url.replace(m.API, ""), params)); return Resp({"messages": [{"id": "a"}, {"id": "b"}]})
            with mock.patch.object(m.requests, "post", return_value=Resp({"access_token": "at", "expires_in": 3600})), \
                 mock.patch.object(m.requests, "request", side_effect=req):
                out = m.new_since("info", "1792044000.7")
                self.assertEqual(out, {"account": "info", "new": 2})
                self.assertEqual(seen, [("GET", "/messages", {"q": "in:inbox after:1792044000", "maxResults": 50})])  # ids only: no message is opened
                with self.assertRaises(ConnectorError): m.new_since("info", "yesterday")
        self.assertIn("new_since", m.OPS)

    def test_shopify_open_count(self):
        with mock.patch.dict(os.environ, {"SHOPIFY_STORE": "demo", "SHOPIFY_ADMIN_TOKEN": "t"}):
            m = load("shopify/ops.py", "t_unattended_shopify")
            with mock.patch.object(m.requests, "get", side_effect=[Resp({"count": 3}), Resp({"count": 1})]) as get:
                out = m.open_count(since="1792044000")
            self.assertEqual(out, {"open_unfulfilled": 3, "new_since": 1})
            first, second = get.call_args_list
            self.assertTrue(first.args[0].endswith("/orders/count.json"))
            self.assertEqual(first.kwargs["params"], {"status": "open", "fulfillment_status": "unfulfilled"})
            self.assertEqual(second.kwargs["params"], {"status": "any", "created_at_min": "2026-10-15T06:00:00+00:00"})
            with mock.patch.object(m.requests, "get", return_value=Resp({"count": 0})) as get:
                self.assertEqual(m.open_count(), {"open_unfulfilled": 0}); self.assertEqual(get.call_count, 1)
            with self.assertRaises(ConnectorError): m.open_count(since="last week")
        self.assertIn("open_count", m.OPS)


if __name__ == "__main__":
    unittest.main()
