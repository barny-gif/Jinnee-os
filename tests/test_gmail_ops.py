"""Gmail connector with several accounts, fake HTTP only: no test reaches Google. Run: python3 -m unittest discover tests"""
import base64, importlib.util, json, os, pathlib, sys, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
from cbase import ConnectorError

def load(name="ops"):
    s = importlib.util.spec_from_file_location(f"t_gmail_{name}", ROOT / "connectors" / "gmail" / f"{name}.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

class Resp:
    def __init__(self, data=None, code=200): self.data, self.status_code, self.ok, self.content = data or {}, code, code < 400, b"x"
    def json(self): return self.data

TWO = {"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "sec", "GMAIL_ACCOUNTS": "info,owner",
       "GMAIL_INFO_ADDRESS": "info@example-shop.hu", "GMAIL_INFO_REFRESH_TOKEN": "r-info",
       "GMAIL_OWNER_ADDRESS": "owner@example.com", "GMAIL_OWNER_REFRESH_TOKEN": "r-owner"}
ALIAS = {**TWO, "GMAIL_INFO_MAILBOX": "owner", "GMAIL_INFO_REFRESH_TOKEN": ""}

def b64(s): return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
MSG = {"id": "m1", "threadId": "t1", "labelIds": ["UNREAD", "INBOX"], "snippet": "Hol a csomagom?",
       "payload": {"headers": [{"name": "From", "value": "Vevo <vevo@example.hu>"}, {"name": "Subject", "value": "#5027 csomag"},
                               {"name": "Message-ID", "value": "<abc@mail>"}, {"name": "To", "value": "info@example-shop.hu"}],
                   "mimeType": "multipart/alternative",
                   "parts": [{"mimeType": "text/html", "body": {"data": b64("<p>Szia, <b>hol</b> a csomagom?</p>")}},
                             {"mimeType": "text/plain", "body": {"data": b64("Szia, hol a csomagom?")}}]}}

class Base(unittest.TestCase):
    envs = TWO
    def setUp(self):
        self.env = mock.patch.dict(os.environ, self.envs, clear=False); self.env.start()
        for k in [k for k in os.environ if k.startswith("GMAIL_") and k not in self.envs]: os.environ.pop(k)
        self.m = load(); self.posts = []
        def post(url, data=None, timeout=None):
            self.posts.append(data); return Resp({"access_token": "at-" + data["refresh_token"], "expires_in": 3600})
        self.p_post = mock.patch.object(self.m.requests, "post", side_effect=post); self.p_post.start()
    def tearDown(self): self.p_post.stop(); self.env.stop()

    def fake(self, routes):
        """routes: (method, path prefix) -> data. Records (auth header, method, path, params, body)."""
        self.calls = []
        def req(method, url, params=None, json=None, timeout=None, headers=None):
            path = url.replace(self.m.API, "")
            self.calls.append((headers["Authorization"], method, path, params, json))
            for (mth, pre), data in routes.items():
                if mth == method and path.startswith(pre): return data if isinstance(data, Resp) else Resp(data)
            return Resp({}, 404)
        return mock.patch.object(self.m.requests, "request", side_effect=req)

class Accounts(Base):
    def test_lists_accounts_without_tokens(self):
        out = self.m.accounts()
        self.assertEqual([(a["name"], a["mailbox"], a["token"]) for a in out], [("info", "info", "set"), ("owner", "owner", "set")])
        self.assertNotIn("r-info", json.dumps(out))
    def test_unknown_account_is_refused(self):
        with self.assertRaises(ConnectorError): self.m.search("shop")

    def test_each_account_uses_its_own_token(self):
        with self.fake({("GET", "/messages"): {"messages": []}}):
            self.m.search("info"); self.m.search("owner")
        self.assertEqual([c[0] for c in self.calls], ["Bearer at-r-info", "Bearer at-r-owner"])
        self.assertEqual(sorted(p["refresh_token"] for p in self.posts), ["r-info", "r-owner"])

    def test_token_is_cached(self):
        with self.fake({("GET", "/messages"): {"messages": []}}):
            self.m.search("info"); self.m.search("info")
        self.assertEqual(len(self.posts), 1)

    def test_expired_testing_token_says_why(self):
        self.p_post.stop()
        with mock.patch.object(self.m.requests, "post", return_value=Resp({"error": "invalid_grant"}, 400)):
            with self.assertRaises(ConnectorError) as e: self.m.check("info")
        self.assertIn("7 days", str(e.exception)); self.assertNotIn("r-info", str(e.exception))
        self.p_post.start()

class Reading(Base):
    def test_search_tags_the_account_and_reads_headers(self):
        with self.fake({("GET", "/messages/m1"): MSG, ("GET", "/messages"): {"messages": [{"id": "m1"}]}}):
            out = self.m.search("info", "is:unread", "5")
        self.assertEqual((out[0]["account"], out[0]["subject"], out[0]["unread"]), ("info", "#5027 csomag", True))
        self.assertEqual(self.calls[0][3]["maxResults"], 5)
    def test_read_prefers_plain_text(self):
        with self.fake({("GET", "/messages/m1"): MSG}):
            self.assertEqual(self.m.read("info", "m1")["body"], "Szia, hol a csomagom?")
    def test_html_only_is_stripped(self):
        only = {**MSG, "payload": {**MSG["payload"], "parts": [MSG["payload"]["parts"][0]]}}
        with self.fake({("GET", "/messages/m1"): only}):
            self.assertNotIn("<b>", self.m.read("info", "m1")["body"])

class SharedMailbox(Base):
    """info@ is only an alias inside the owner mailbox: one token, two slices that never mix."""
    envs = ALIAS
    def test_alias_uses_the_mailbox_token_and_sees_only_its_address(self):
        with self.fake({("GET", "/messages"): {"messages": []}}):
            self.m.search("info", "is:unread"); self.m.search("owner", "is:unread")
        (h1, _, _, p1, _), (h2, _, _, p2, _) = self.calls
        self.assertEqual((h1, h2), ("Bearer at-r-owner", "Bearer at-r-owner"))
        self.assertIn("deliveredto:info@example-shop.hu", p1["q"]); self.assertTrue(p1["q"].startswith("is:unread"))
        self.assertIn("-deliveredto:info@example-shop.hu", p2["q"])  # the owner's own slice leaves the shop's mail out
        self.assertEqual(self.m.accounts()[0]["token"], "set")
    def test_alias_of_alias_is_refused(self):
        with mock.patch.dict(os.environ, {"GMAIL_ACCOUNTS": "info,owner,x", "GMAIL_X_MAILBOX": "info"}):
            with self.assertRaises(ConnectorError): self.m.search("x")

class Drafts(Base):
    def test_reply_draft_in_thread_from_the_account_address(self):
        with self.fake({("GET", "/messages/m1"): MSG, ("POST", "/drafts"): {"id": "d1"}}):
            out = self.m.draft_reply("info", "m1", body="Szia! A csomagod úton van.")
        body = self.calls[-1][4]
        raw = base64.urlsafe_b64decode(body["message"]["raw"]).decode()
        self.assertEqual((out["draft_id"], body["message"]["threadId"]), ("d1", "t1"))
        self.assertIn("From: info@example-shop.hu", raw); self.assertIn("To: vevo@example.hu", raw)
        self.assertIn("Subject: Re: #5027 csomag", raw); self.assertIn("In-Reply-To: <abc@mail>", raw)
        self.assertNotIn("/send", " ".join(c[2] for c in self.calls))
    def test_empty_reply_refused(self):
        with self.assertRaises(ConnectorError): self.m.draft_reply("info", "m1", body="  ")
    def test_send_is_dry_unless_confirmed(self):
        d = {"id": "d1", "message": {"payload": {"headers": [{"name": "Subject", "value": "Re: #5027"}]}}}
        with self.fake({("GET", "/drafts/d1"): d, ("POST", "/drafts/send"): {"id": "s1", "threadId": "t1"}}):
            dry = self.m.send("info", "d1")
            self.assertTrue(dry["dry_run"]); self.assertEqual([c[1] for c in self.calls], ["GET"])
            sent = self.m.send("info", "d1", confirm="yes")
        self.assertEqual(sent["message_id"], "s1"); self.assertEqual(self.calls[-1][4], {"id": "d1"})
    def test_forbidden_names_the_likely_cause(self):
        with self.fake({("GET", "/profile"): Resp({}, 403)}):
            with self.assertRaises(ConnectorError) as e: self.m.check("info")
        self.assertIn("Gmail API", str(e.exception))

class Auth(unittest.TestCase):
    def setUp(self): self.a = load("auth")
    def test_link_has_offline_consent_and_pkce(self):
        url = self.a.link("cid", "v" * 64, "info@example-shop.hu")
        for part in ("access_type=offline", "prompt=consent", "code_challenge_method=S256", "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2F",
                     "gmail.compose", "login_hint=info%40example-shop.hu"):
            self.assertIn(part, url)
    def test_code_from_pasted_address_or_bare_code(self):
        self.assertEqual(self.a.code_from("http://127.0.0.1:8765/?code=4/abc&scope=x"), "4/abc")
        self.assertEqual(self.a.code_from("4/abc"), "4/abc")
        with self.assertRaises(ConnectorError): self.a.code_from("http://127.0.0.1:8765/?error=access_denied")
    def test_exchange_needs_a_refresh_token(self):
        with mock.patch.object(self.a.requests, "post", return_value=Resp({"access_token": "a"})):
            with self.assertRaises(ConnectorError): self.a.exchange("c", "s", "code", "v")
        with mock.patch.object(self.a.requests, "post", return_value=Resp({"refresh_token": "r1"})) as p:
            self.assertEqual(self.a.exchange("c", "s", "code", "v"), "r1")
        self.assertEqual(p.call_args.kwargs["data"]["code_verifier"], "v")

if __name__ == "__main__": unittest.main()
