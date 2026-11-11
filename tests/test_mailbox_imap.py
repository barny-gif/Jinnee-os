"""IMAP mailbox with an app password, fake server only: no test reaches a real mailbox. Run: python3 -m unittest discover tests"""
import importlib.util, os, pathlib, sys, tempfile, unittest
from email.message import EmailMessage
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
from cbase import ConnectorError

def load():
    s = importlib.util.spec_from_file_location("t_imap", ROOT / "connectors" / "mailbox_imap.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

def mail(subject="COD detail", fname="report 1.xlsx"):
    e = EmailMessage(); e["From"] = "cod@carrier.example"; e["Subject"] = subject; e["Date"] = "Wed, 11 Nov 2026 08:02:05 +0100"
    e.set_content("see attached"); e.add_attachment(b"PK-data", maintype="application", subtype="octet-stream", filename=fname)
    return e.as_bytes()

class Fake:
    """A read-only IMAP server stand-in that records what it was asked."""
    def __init__(self, msgs, login_ok=True): self.msgs, self.login_ok, self.calls = msgs, login_ok, []
    def login(self, u, p):
        self.calls.append(("login", u, p))
        if not self.login_ok: raise self.error("bad")
    def select(self, box, readonly=False): self.calls.append(("select", box, readonly)); return "OK", [b"1"]
    def uid(self, cmd, *args):
        self.calls.append((cmd,) + args)
        if cmd == "SEARCH": return "OK", [b" ".join(k.encode() for k in self.msgs)]
        if cmd == "FETCH": return "OK", [(b"x", self.msgs[args[0] if isinstance(args[0], str) else args[0].decode()])]
        raise AssertionError(f"write command {cmd} must never be used")
    def logout(self): self.calls.append(("logout",))

ENV = {"IMAP_ACCOUNTS": "private", "IMAP_PRIVATE_USER": "owner@example.com", "IMAP_PRIVATE_PASSWORD": "abcd efgh ijkl mnop"}

class Imap(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)
        self.m = load(); self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.m.ROOT = self.tmp; self.m.INBOX = self.tmp / "handoffs" / "inbox" / "mail"
    def use(self, fake):
        fake.error = self.m.imaplib.IMAP4.error
        p = mock.patch.object(self.m.imaplib, "IMAP4_SSL", return_value=fake); p.start(); self.addCleanup(p.stop); return fake

    def test_accounts_never_show_the_password(self):
        out = self.m.accounts()
        self.assertEqual(out, [{"name": "private", "host": "imap.gmail.com", "login": "set"}])
        self.assertNotIn("abcd", str(out))
    def test_search_reads_only_and_lists_attachments(self):
        f = self.use(Fake({"7": mail(), "9": mail("COD detail 2", "b.xlsx")}))
        out = self.m.search("private", sender="carrier.example", since="2026-11-01")
        self.assertEqual([x["uid"] for x in out["messages"]], ["9", "7"])  # newest first
        self.assertEqual(out["messages"][1]["attachments"], ["report 1.xlsx"])
        self.assertIn(("select", '"[Gmail]/All Mail"', True), f.calls)  # read-only: not even "seen" changes
        self.assertIn(("login", "owner@example.com", "abcdefghijklmnop"), f.calls)  # spaces in the app password are dropped
        search = next(c for c in f.calls if c[0] == "SEARCH")
        self.assertEqual(search[2:], ("FROM", '"carrier.example"', "SINCE", "01-Nov-2026"))
    def test_save_attachments_only_under_the_inbox_once(self):
        self.use(Fake({"7": mail(fname="../../evil.xlsx")}))
        out = self.m.save_attachments("private", "7", folder="../carrier", match=".xlsx")
        p = (self.tmp / out["saved"][0]).resolve()
        self.assertTrue(str(p).startswith(str(self.m.INBOX.resolve())))
        self.assertEqual(p.read_bytes(), b"PK-data")
        p.write_bytes(b"kept"); self.m.save_attachments("private", "7", folder="../carrier", match=".xlsx")
        self.assertEqual(p.read_bytes(), b"kept")  # an existing file is not overwritten
    def test_wrong_password_says_what_to_do_without_showing_it(self):
        self.use(Fake({}, login_ok=False))
        with self.assertRaises(ConnectorError) as e: self.m.search("private")
        self.assertIn("app password", str(e.exception)); self.assertNotIn("abcd", str(e.exception))
    def test_unknown_account_and_bad_input(self):
        self.use(Fake({}))  # never the real server, even if a check came too late
        with self.assertRaises(ConnectorError): self.m.search("shop")
        with self.assertRaises(ConnectorError): self.m.save_attachments("private", "x1")
        with self.assertRaises(ConnectorError): self.m.search("private", since="11/01")

if __name__ == "__main__": unittest.main()
