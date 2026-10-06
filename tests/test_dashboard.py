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


class Approvals(Base):
    """What the buttons do to an item. Access is covered above; here the request comes from the machine itself."""
    ORIGIN = {"origin": LOCAL}

    def setUp(self):
        super().setUp()
        self.brain = self.dash.BRAIN
        self.c = self.client(headers=self.ORIGIN)
        items = [{"id": "a1", "agent": "office", "action": "send_customer_email", "title": "Reply", "text": "Dear Anna, thank you."},
                 {"id": "a2", "agent": "social", "action": "publish_social_post", "title": "Post", "text": "DRAFT: write me"},
                 {"id": "a3", "agent": "social", "action": "publish_social_post", "title": "Empty post", "text": " "}]
        (self.brain / "approvals.json").write_text(json.dumps(items))

    def post(self, item_id, decision, note=None):
        return self.c.post(f"/api/approve/{item_id}", json={"decision": decision, "note": note})

    def shown(self):
        return {a["id"]: a for a in self.c.get("/api/state").json()["approvals"]}

    def test_state_shows_the_three_stages_and_the_text(self):
        self.post("a1", "ok")
        from approvals import Book
        self.assertEqual(Book(self.brain).consume("a1").word, "GO")
        self.post("a2", "edit", "finish it first")
        got = self.shown()
        self.assertEqual({i: a["state"] for i, a in got.items()}, {"a1": "consumed", "a2": "decided", "a3": "pending"})
        self.assertEqual(got["a1"]["status"], "being carried out")
        self.assertIn("sent back for a change", got["a2"]["status"])
        self.assertEqual((got["a3"]["status"], got["a3"]["preview"]), ("waiting for the owner", " "))
        self.assertIn("empty", got["a3"]["warning"])  # visible before the owner decides
        self.assertEqual(got["a1"]["label"], "Send an email to a customer")
        self.assertIsNone(got["a2"]["text"])  # the page gets the preview, cut to size

    def test_decide_once_then_undo(self):
        self.assertEqual(self.post("a1", "ok").json(), {"ok": True, "status": "approved, not picked up yet", "sendable": True})
        for again in ("ok", "drop", "edit"):
            r = self.post("a1", again, "changed my mind")
            self.assertEqual(r.status_code, 409, again)
            self.assertIn("already approved", r.json()["detail"])
        self.assertEqual(self.c.post("/api/undo/a1").status_code, 200)
        self.assertEqual(self.shown()["a1"]["state"], "pending")
        self.assertEqual(self.c.post("/api/undo/a1").status_code, 409)  # nothing left to undo
        self.assertEqual(self.post("a1", "drop", "not needed").status_code, 200)
        self.assertEqual(self.c.post("/api/undo/nope").status_code, 404)
        self.assertEqual(self.log().count("a1 → "), 3)  # ok, undo, drop

    def test_undo_is_over_once_the_team_picked_it_up(self):
        from approvals import Book
        self.post("a1", "ok")
        self.assertEqual(Book(self.brain).consume("a1").word, "GO")
        r = self.c.post("/api/undo/a1")
        self.assertEqual(r.status_code, 409)
        self.assertIn("can no longer be undone", r.json()["detail"])
        self.assertEqual(self.post("a1", "drop", "stop").status_code, 409)
        self.assertEqual(self.shown()["a1"]["state"], "consumed")

    def test_undo_follows_the_same_access_rules(self):
        self.post("a1", "ok")
        self.assertEqual(self.client().post("/api/undo/a1").status_code, 403)  # no origin: cross-site
        self.assertEqual(self.client(peer=LAN, base="http://203.0.113.5:8080").post("/api/undo/a1", headers={"origin": "http://203.0.113.5:8080"}).status_code, 403)
        self.assertEqual(self.shown()["a1"]["state"], "decided")

    def test_drop_and_change_need_words(self):
        for decision in ("drop", "edit"):
            for note in (None, "", "   "):
                r = self.post("a1", decision, note)
                self.assertEqual(r.status_code, 400, (decision, note))
        self.assertIn("reason", self.post("a1", "drop").json()["detail"])
        self.assertEqual(self.shown()["a1"]["state"], "pending")
        self.assertEqual(self.post("a1", "drop", "we do not email on Sundays").status_code, 200)
        self.assertIn("we do not email on Sundays [approval a1]", (self.brain / "lessons.md").read_text(encoding="utf-8"))

    def test_empty_or_draft_text_is_approved_but_not_sendable(self):
        for item_id, why in (("a2", "draft marker"), ("a3", "empty")):
            r = self.post(item_id, "ok")
            self.assertEqual((r.status_code, r.json()["sendable"]), (200, False))
            self.assertIn("approved, but not sendable", r.json()["status"])
            self.assertIn(why, self.shown()[item_id]["warning"])  # and the owner keeps seeing why

    def test_levels_are_shown_and_cannot_be_written(self):
        got = {a["action"]: a for a in self.c.get("/api/state").json()["autonomy"]}
        self.assertEqual((got["spend_money"]["level"], got["spend_money"]["locked"], got["spend_money"]["name"]), (0, True, "forbidden"))
        self.assertEqual((got["raise_autonomy"]["level"], got["raise_autonomy"]["locked"]), (1, True))
        for method, path in (("post", "/api/autonomy"), ("put", "/api/autonomy/spend_money"), ("post", "/api/state"),
                             ("patch", "/api/autonomy/spend_money")):
            self.assertIn(getattr(self.c, method)(path, json={"level": 3}).status_code, (404, 405), path)
        self.assertFalse((self.brain / "autonomy_config.json").exists())

    def test_approving_a_forged_raise_changes_no_level(self):
        """The only thing the dashboard can do about levels is approve a request; the lock is checked when it is applied."""
        import autonomy
        (self.brain / "approvals.json").write_text(json.dumps([{"id": "r1", "agent": "ops", "action": "raise_autonomy", "title": "spend",
                                                                "payload": {"target": "spend_money", "to": 3}}]))
        self.assertEqual(self.post("r1", "ok").status_code, 200)
        cfg = autonomy.Config(self.brain)
        with self.assertRaises(autonomy.Refused): cfg.apply("r1")
        self.assertEqual(cfg.allowed("spend_money")[:2], (0, True))

    def test_broken_files_are_reported_not_fatal(self):
        (self.brain / "approvals.json").write_text('[{"id": "a1"')
        (self.brain / "autonomy_config.json").write_text("{")
        (self.brain / "heartbeat.json").write_text("nope")
        got = self.c.get("/api/state").json()
        self.assertEqual((got["approvals"], got["team"], len(got["problems"])), ([], [], 2))
        self.assertEqual(self.post("a1", "ok").status_code, 404)


if __name__ == "__main__":
    unittest.main()
