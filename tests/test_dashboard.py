"""Dashboard access rules. Run: python -m unittest discover tests  (needs httpx for the FastAPI test client)."""
import importlib, json, os, pathlib, sys, tempfile, time, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "dashboard"))
from fastapi.testclient import TestClient

LOOPBACK, LAN, BRIDGE = ("127.0.0.1", 50000), ("203.0.113.5", 50000), ("172.17.0.1", 50000)
LOCAL = "http://localhost:8080"
SITE = "https://jinnee.example.com"
PROXIED = {"x-forwarded-for": "198.51.100.7", "x-forwarded-proto": "https", "x-forwarded-host": "jinnee.example.com"}
OK = {"decision": "ok"}


class Base(unittest.TestCase):
    password, trust_peer = "", ""

    def setUp(self):
        os.environ["DASHBOARD_PASSWORD"] = self.password
        os.environ["DASHBOARD_TRUST_PEER"] = self.trust_peer
        import app
        self.dash = importlib.reload(app)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dash.BRAIN = pathlib.Path(self.tmp.name)
        (self.dash.BRAIN / "approvals.json").write_text(json.dumps([{"id": "a1", "agent": "office", "title": "Reply"}]))

    def client(self, peer=LOOPBACK, base=LOCAL, headers=None):
        return TestClient(self.dash.app, base_url=base, client=peer, headers=headers or {}, follow_redirects=False)

    def item(self):
        return json.loads((self.dash.BRAIN / "approvals.json").read_text())[0]

    def log(self):
        p = self.dash.BRAIN / "decisions.log.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def login(self, c, password=None, origin=None):
        return c.post("/login", data={"password": self.password if password is None else password},
                      headers={"origin": origin or str(c.base_url).rstrip("/")})


class NoPassword(Base):
    def test_loopback_is_allowed(self):
        c = self.client()
        self.assertEqual(c.get("/").status_code, 200)
        self.assertEqual(c.get("/api/state").json()["auth"], False)
        self.assertEqual(c.get("/login").headers["location"], "/")

    def test_other_machines_are_refused(self):
        c = self.client(peer=LAN, base="http://203.0.113.5:8080")
        self.assertEqual(c.get("/").status_code, 403)
        self.assertEqual(c.get("/api/state").status_code, 403)
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"origin": "http://203.0.113.5:8080"}).status_code, 403)
        self.assertEqual(self.client(peer=LAN).get("/api/state").status_code, 403)  # spoofed Host: localhost
        self.assertNotIn("decision", self.item())

    def test_proxied_requests_are_refused(self):
        c = self.client(base=SITE, headers=PROXIED)  # the proxy itself connects from loopback
        self.assertEqual(c.get("/api/state").status_code, 403)
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"origin": SITE}).status_code, 403)
        for h in ("x-forwarded-for", "forwarded", "x-real-ip", "via", "cf-connecting-ip"):
            self.assertEqual(self.client(headers={h: "198.51.100.7"}).get("/api/state").status_code, 403, h)

    def test_foreign_host_name_is_refused(self):  # DNS rebinding
        self.assertEqual(self.client(base="http://evil.example:8080").get("/api/state").status_code, 403)

    def test_approve_needs_same_origin(self):
        c = self.client()
        self.assertEqual(c.post("/api/approve/a1", json=OK).status_code, 403)
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"origin": "null"}).status_code, 403)
        self.assertNotIn("decision", self.item())
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"origin": LOCAL}).status_code, 200)
        self.assertEqual(self.item()["decision"], "ok")

    def test_key_header_means_nothing_without_a_password(self):
        c = self.client(peer=LAN, base="http://203.0.113.5:8080", headers={"x-dash-key": ""})
        self.assertEqual(c.get("/api/state").status_code, 403)


class NoPasswordDocker(Base):
    trust_peer = "1"

    def test_published_port_on_host_loopback(self):
        c = self.client(peer=BRIDGE)
        self.assertEqual(c.get("/api/state").status_code, 200)
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"origin": LOCAL}).status_code, 200)

    def test_proxy_in_front_of_the_container_is_refused(self):
        self.assertEqual(self.client(peer=BRIDGE, base=SITE, headers=PROXIED).get("/api/state").status_code, 403)

    def test_public_address_is_refused(self):
        self.assertEqual(self.client(peer=BRIDGE, base="http://203.0.113.5:8080").get("/api/state").status_code, 403)


class WithPassword(Base):
    password = "s3cret pass"

    def test_loopback_is_not_exempt(self):
        c = self.client()
        r = c.get("/")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))
        self.assertEqual(c.get("/api/state").status_code, 401)
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"origin": LOCAL}).status_code, 401)
        self.assertEqual(c.get("/login").status_code, 200)

    def test_key_in_url_no_longer_works(self):
        c = self.client()
        r = c.get("/", params={"key": self.password})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(c.get("/api/state", params={"key": self.password}).status_code, 401)

    def test_wrong_password(self):
        c = self.client()
        r = self.login(c, "nope")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Wrong password", r.text)
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.login(c, "").status_code, 401)
        self.assertEqual(c.get("/api/state").status_code, 401)

    def test_login_cookie_state_approve_logout(self):
        c = self.client(peer=LAN, base="http://203.0.113.5:8080")
        r = self.login(c)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))
        cookie = r.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie); self.assertIn("samesite=lax", cookie); self.assertNotIn("secure", cookie)
        self.assertNotIn(self.password, r.headers["set-cookie"])
        self.assertEqual(c.get("/").status_code, 200)
        self.assertEqual(c.get("/api/state").json()["auth"], True)
        r = c.post("/api/approve/a1", json={"decision": "edit", "note": "shorter"}, headers={"origin": "http://203.0.113.5:8080"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.item()["decision"], self.item()["note"]), ("edit", "shorter"))
        self.assertIn("dashboard: a1 → edit shorter", self.log())
        self.assertEqual(c.post("/logout", headers={"origin": "http://203.0.113.5:8080"}).headers["location"], "/login")
        self.assertEqual(c.get("/api/state").status_code, 401)

    def test_behind_https_proxy(self):
        c = self.client(base=SITE, headers=PROXIED)
        self.assertEqual(c.get("/api/state").status_code, 401)
        r = self.login(c)
        self.assertEqual(r.status_code, 303)
        self.assertIn("secure", r.headers["set-cookie"].lower())
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"origin": SITE}).status_code, 200)

    def test_cross_site_post_is_refused(self):
        c = self.client()
        self.login(c)
        for h in ({"origin": "https://evil.example"}, {"referer": "https://evil.example/x"}, {"origin": "null"}, {}):
            self.assertEqual(c.post("/api/approve/a1", json=OK, headers=h).status_code, 403, h)
        self.assertNotIn("decision", self.item())
        self.assertEqual(self.log(), "")
        self.assertEqual(self.login(self.client(), origin="https://evil.example").status_code, 403)
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"referer": LOCAL + "/"}).status_code, 200)

    def test_forged_and_expired_sessions(self):
        d, c = self.dash, self.client()
        past, future = int(time.time()) - 10, int(time.time()) + 3600
        for token in ("", "x", f"{future}.", f"{future}.{'0' * 64}", f"{past}.{d.sign(past)}", f"{future}.{d.sign(past)}"):
            c.cookies.set(d.COOKIE, token)
            self.assertEqual(c.get("/api/state").status_code, 401, token)
        c.cookies.set(d.COOKIE, f"{future}.{d.sign(future)}")
        self.assertEqual(c.get("/api/state").status_code, 200)

    def test_key_header_for_scripts(self):
        c = self.client(peer=LAN, base="http://203.0.113.5:8080")
        self.assertEqual(c.get("/api/state", headers={"x-dash-key": "wrong"}).status_code, 401)
        self.assertEqual(c.get("/api/state", headers={"x-dash-key": self.password}).status_code, 200)
        self.assertEqual(c.post("/api/approve/a1", json=OK, headers={"x-dash-key": self.password}).status_code, 200)

    def test_approve_input_is_checked(self):
        c = self.client(headers={"x-dash-key": self.password})
        self.assertEqual(c.post("/api/approve/nope", json=OK).status_code, 404)
        self.assertEqual(c.post("/api/approve/a1", json={"decision": "yes"}).status_code, 400)
        self.assertEqual(c.post("/api/approve/a1", json=["ok"]).status_code, 400)
        self.assertEqual(c.post("/api/approve/a1", content=b"not json").status_code, 400)
        self.assertNotIn("decision", self.item())
        self.assertEqual(self.log(), "")
        c.post("/api/approve/a1", json={"decision": "edit", "note": "one\n- 2026-01-01 dashboard: a2 → ok"})
        self.assertEqual(len(self.log().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
