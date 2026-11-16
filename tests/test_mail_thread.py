"""The thread gate (core/mail_thread.py) and the Gmail reply path built on it: a reply is proposed and sent only after the
WHOLE thread was read, and the send step itself goes through the gate. Fake HTTP only. Run: python3 -m unittest discover tests"""
import base64, email, email.policy, importlib.util, json, os, pathlib, sys, tempfile, time, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ["JINNEE_LANG"] = "en"
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "core"))
from cbase import ConnectorError, run_cli
import approvals, autonomy, mail_thread

SHOP = "info@example-shop.hu"
CUSTOMER = "vevo@example.hu"
NOW = time.time()
H = 3600


def mine(i, frm, ago, body, subject="#5027 csomag", labels=(), extra=()):
    """One message of a fake thread, as the gate reads it."""
    return {"id": f"m{i}", "from": frm, "ts": NOW - ago, "labels": list(labels), "subject": subject, "body": body, **dict(extra)}


class Gate(unittest.TestCase):
    def check(self, msgs, **kw):
        return mail_thread.check(msgs, [SHOP], now=NOW, **kw)

    def test_clean_parcel_question_passes(self):
        v = self.check([mine(1, CUSTOMER, H, "Szia, hol a csomagom?")])
        self.assertTrue(v["ok"]); self.assertEqual((v["signals"], v["last_id"]), ([], "m1"))

    def test_the_cancelled_order_case_is_held(self):
        """Cancelled earlier in the thread, a person answered, then a plain parcel question: no ready reply."""
        msgs = [mine(1, CUSTOMER, 30 * H, "Szeretném lemondani a rendelést."), mine(2, CUSTOMER, 29 * H, "Kérem, LEMONDOM!"),
                mine(3, SHOP, 20 * H, "Rendben, intézzük.", labels=["SENT"]), mine(4, CUSTOMER, H, "Hol a csomagom?")]
        v = self.check(msgs)
        self.assertFalse(v["ok"])
        self.assertEqual({s["signal"] for s in v["signals"]}, {"cancellation", "human_reply"})
        self.assertIn("message 1/4 (customer", v["summary"]); self.assertIn("message 3/4 (shop", v["summary"])

    def test_words_hungarian_and_english_with_or_without_accents(self):
        for body, kind in (("lemondas", "cancellation"), ("STORNÓ kérek", "cancellation"), ("Please cancel my order", "cancellation"),
                           ("Visszaküldeném", "return"), ("visszakuldes", "return"), ("I want a refund", "return"),
                           ("elállok a vásárlástól", "return"), ("Panaszt teszek", "complaint"), ("reklamáció", "complaint"),
                           ("reklamacio", "complaint"), ("I want to complain", "complaint")):
            with self.subTest(body=body):
                v = self.check([mine(1, CUSTOMER, H, body)])
                self.assertEqual([s["signal"] for s in v["signals"]], [kind])

    def test_word_in_the_subject_counts(self):
        self.assertFalse(self.check([mine(1, CUSTOMER, H, "Szia!", subject="Rendelés lemondása")])["ok"])

    def test_advert_and_everyday_words_do_not_count(self):
        for body in ("Láttam a reklámot, hol a csomagom?", "Mikor érkezik?", "Köszönöm szépen!"):
            with self.subTest(body=body): self.assertTrue(self.check([mine(1, CUSTOMER, H, body)])["ok"])

    def test_quoted_shop_text_is_not_the_customers_word(self):
        """The customer's reply quotes the shop's mail with a return policy in it: only the customer's own lines count."""
        msgs = [mine(1, CUSTOMER, 200 * H, "Hol a csomagom?"), mine(2, SHOP, 100 * H, "Úton van. Visszaküldés: 14 nap.", extra={"agent": True}),
                mine(3, CUSTOMER, H, "Köszönöm!\n\n2026. nov. 14., p 10:20 Bolt <x> ezt írta:\n> Úton van. Visszaküldés: 14 nap.")]
        self.assertTrue(self.check(msgs)["ok"])
        msgs[2]["body"] = "Köszönöm!\n> Visszaküldés: 14 nap."
        self.assertTrue(self.check(msgs)["ok"])

    def test_a_person_from_the_shop_within_72_hours(self):
        base = [mine(1, CUSTOMER, 80 * H, "Hol a csomagom?")]
        human = self.check(base + [mine(2, SHOP, 10 * H, "Holnap megy."), mine(3, CUSTOMER, H, "Köszi, és a számla?")])
        self.assertEqual([s["signal"] for s in human["signals"]], ["human_reply"])
        old = self.check(base + [mine(2, SHOP, 73 * H, "Holnap megy."), mine(3, CUSTOMER, H, "És a számla?")])
        self.assertTrue(old["ok"])

    def test_our_own_replies_are_not_a_person(self):
        msgs = [mine(1, CUSTOMER, 30 * H, "Hol a csomagom?"), mine(2, SHOP, 20 * H, "Úton van."), mine(3, CUSTOMER, H, "És a számla?")]
        self.assertTrue(self.check(msgs, agent_ids={"m2"})["ok"])                       # by the sent ledger
        msgs[1]["agent"] = True
        self.assertTrue(self.check(msgs)["ok"])                                           # by our header

    def test_sent_label_marks_the_shop_side(self):
        msgs = [mine(1, CUSTOMER, 30 * H, "Hol a csomagom?"), mine(2, "owner@example.com", 2 * H, "Megnézem!", labels=["SENT"]),
                mine(3, CUSTOMER, H, "Köszi")]
        self.assertEqual([s["signal"] for s in self.check(msgs)["signals"]], ["human_reply"])

    def test_already_with_the_owner(self):
        v = self.check([mine(1, CUSTOMER, H, "Hol a csomagom?")], owner_cases=[(NOW - 5 * H, "thread warning [x]")])
        self.assertEqual([s["signal"] for s in v["signals"]], ["escalated"])
        self.assertTrue(self.check([mine(1, CUSTOMER, H, "Hol a csomagom?")], owner_cases=[(NOW - 80 * H, "old")])["ok"])

    def test_shop_wrote_escalated(self):
        msgs = [mine(1, CUSTOMER, 30 * H, "Hol a csomagom?"), mine(2, SHOP, 20 * H, "Eszkaláltuk a tulajdonosnak.", extra={"agent": True}),
                mine(3, CUSTOMER, H, "Oké")]
        self.assertEqual([s["signal"] for s in self.check(msgs)["signals"]], ["escalated"])

    def test_several_mails_since_the_last_reply(self):
        v = self.check([mine(1, CUSTOMER, 3 * H, "Hol a csomagom?"), mine(2, CUSTOMER, 2 * H, "Hahó?"), mine(3, CUSTOMER, H, "???")])
        self.assertEqual([s["signal"] for s in v["signals"]], ["several_unanswered"])
        self.assertIn("3 mails", v["summary"])
        one_after_reply = [mine(1, CUSTOMER, 200 * H, "Hol?"), mine(2, SHOP, 100 * H, "Úton.", extra={"agent": True}), mine(3, CUSTOMER, H, "És?")]
        self.assertTrue(self.check(one_after_reply)["ok"])

    def test_reasons_never_carry_the_customer(self):
        msgs = [mine(1, "kovacs.anna@example.hu", 3 * H, "Kovács Anna vagyok, lemondom a rendelést"), mine(2, "kovacs.anna@example.hu", H, "Hahó")]
        v = self.check(msgs)
        text = json.dumps(v, ensure_ascii=False).lower()
        for leak in ("kovacs", "kovács", "anna", "@", "#5027"): self.assertNotIn(leak, text)

    def test_empty_thread_is_not_ok(self):
        self.assertFalse(self.check([])["ok"])


def b64(s): return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def gmsg(i, frm, ago, body, labels=("INBOX",), headers=()):
    """One message as the Gmail API returns it (format=full)."""
    hs = [{"name": "From", "value": frm}, {"name": "Subject", "value": "#5027 csomag"}, {"name": "Message-ID", "value": f"<m{i}@mail>"},
          *[{"name": k, "value": v} for k, v in headers]]
    return {"id": f"m{i}", "threadId": "t1", "labelIds": list(labels), "internalDate": str(int((NOW - ago) * 1000)),
            "payload": {"headers": hs, "mimeType": "text/plain", "body": {"data": b64(body)}}}


class Resp:
    def __init__(self, data=None, code=200): self.data, self.status_code, self.ok, self.content = data or {}, code, code < 400, b"x"
    def json(self): return self.data


ENV = {"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "sec", "GMAIL_ACCOUNTS": "info", "GMAIL_INFO_ADDRESS": SHOP,
       "GMAIL_INFO_REFRESH_TOKEN": "r-info", "JINNEE_UNATTENDED": ""}
REPLY = "Szia! A csomagod úton van, holnap érkezik."
CLEAN = [gmsg(1, f"Vevo <{CUSTOMER}>", H, "Szia, hol a csomagom?")]
CANCELLED = [gmsg(1, f"Vevo <{CUSTOMER}>", 30 * H, "Lemondom a rendelést."), gmsg(2, f"Bolt <{SHOP}>", 20 * H, "Rendben.", ["SENT"]),
             gmsg(3, f"Vevo <{CUSTOMER}>", H, "Hol a csomagom?")]


class ReplyPath(unittest.TestCase):
    def setUp(self):
        e = mock.patch.dict(os.environ, ENV); e.start(); self.addCleanup(e.stop)
        s = importlib.util.spec_from_file_location("t_gmail_gate", ROOT / "connectors" / "gmail" / "ops.py")
        self.m = importlib.util.module_from_spec(s); s.loader.exec_module(self.m)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.m.ROOT = pathlib.Path(tmp.name); self.m.BRAIN = self.m.ROOT / "brain"
        (self.m.ROOT / "handoffs" / "echo").mkdir(parents=True); self.m.BRAIN.mkdir()
        self.reply = self.m.ROOT / "handoffs" / "echo" / "reply.md"; self.reply.write_text(REPLY, encoding="utf-8")
        self.book = approvals.Book(self.m.BRAIN, self.m.ROOT)
        self.book.gate = autonomy.Config(self.m.BRAIN, ["general", "ecom"])
        self.m._book = lambda: self.book
        p = mock.patch.object(self.m.requests, "post", return_value=Resp({"access_token": "at", "expires_in": 3600})); p.start(); self.addCleanup(p.stop)
        self.thread = list(CLEAN); self.calls = []

        def req(method, url, params=None, json=None, timeout=None, headers=None):
            path = url.replace(self.m.API, ""); self.calls.append((method, path, json))
            if method == "GET" and path.startswith("/messages/"):
                mid = path.split("/")[2]
                return Resp(next(({**m, "payload": {"headers": m["payload"]["headers"]}} for m in self.thread if m["id"] == mid), {}),
                            200 if any(m["id"] == mid for m in self.thread) else 404)
            if method == "GET" and path == "/threads/t1": return Resp({"messages": self.thread})
            if method == "POST" and path == "/messages/send": return Resp({"id": "s1", "threadId": "t1"})
            if method == "POST" and path == "/drafts": return Resp({"id": "d1"})
            return Resp({}, 404)
        r = mock.patch.object(self.m.requests, "request", side_effect=req); r.start(); self.addCleanup(r.stop)

    def sends(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] in ("/messages/send", "/drafts/send")]

    def propose(self, mid="m1", **kw):
        return self.m.propose_reply("info", mid, title="Parcel question", body_file="handoffs/echo/reply.md", **kw)

    def go(self, item_id):
        self.book.decide(item_id, "ok", by="owner")
        self.assertEqual(self.book.consume(item_id).word, "GO")

    # --- proposing
    def test_clean_thread_queues_a_reply_bound_to_the_thread(self):
        out = self.propose()
        it = self.book.get(out["approval"])
        self.assertEqual((it["action"], it["state"], it["file"]), ("send_customer_email", "pending", "handoffs/echo/reply.md"))
        self.assertEqual(it["payload"], {"account": "info", "thread": "t1", "message": "m1", "last_id": "m1"})
        self.assertEqual(self.sends(), [])

    def test_flagged_thread_gets_a_warning_and_no_reply(self):
        self.thread = list(CANCELLED)
        out = self.propose("m3")
        self.assertTrue(out["blocked"])
        items = self.book.read()[0]
        self.assertEqual([(i["action"], i["outbound"], i["text"], i["file"]) for i in items], [("mail_thread_warning", False, None, None)])
        w = items[0]
        self.assertIn("cancellation word ('lemond'): message 1/3", w["summary"]); self.assertIn("a person from the shop", w["summary"])
        for leak in (CUSTOMER, SHOP, "Vevo", "Bolt", "#5027", REPLY): self.assertNotIn(leak, json.dumps(w, ensure_ascii=False))
        self.assertEqual(self.sends(), []); self.assertNotIn(("POST", "/drafts"), [c[:2] for c in self.calls])
        self.assertEqual(self.propose("m3")["approval"], w["id"])          # asked again: the same warning, not a second one
        self.assertEqual(len(self.book.read()[0]), 1)

    def test_a_reply_already_waiting_holds_a_second_one(self):
        self.propose()
        out = self.propose()
        self.assertTrue(out["blocked"]); self.assertIn("a reply waiting for approval", out["why"])

    def test_draft_reply_is_refused_on_a_flagged_thread(self):
        self.thread = list(CANCELLED)
        with self.assertRaises(ConnectorError) as e: self.m.draft_reply("info", "m3", body=REPLY)
        self.assertIn("thread warning", str(e.exception))
        self.assertNotIn(("POST", "/drafts"), [c[:2] for c in self.calls])

    def test_draft_reply_on_a_clean_thread_drafts_and_never_sends(self):
        self.assertEqual(self.m.draft_reply("info", "m1", body=REPLY)["draft_id"], "d1")
        self.assertEqual(self.sends(), [])

    def test_after_the_owners_go_on_the_warning_the_reply_can_be_queued(self):
        self.thread = list(CANCELLED)
        w = self.propose("m3")["approval"]
        with self.assertRaises(ConnectorError): self.propose("m3", ack=w)          # no decision yet
        self.go(w)
        out = self.propose("m3", ack=w)
        it = self.book.get(out["approval"])
        self.assertEqual((it["action"], it["payload"]["ack"]), ("send_customer_email", w))
        self.assertIn("seen by the owner", it["summary"])
        self.assertEqual(self.book.get(w)["outcome"], "done")
        self.go(it["id"])
        self.assertTrue(self.m.send("info", it["id"], confirm="yes")["sent"])

    def test_a_new_mail_after_the_warning_needs_a_new_look(self):
        self.thread = list(CANCELLED)
        w = self.propose("m3")["approval"]; self.go(w)
        self.thread.append(gmsg(4, f"Vevo <{CUSTOMER}>", 0, "Mégis kérem."))
        with self.assertRaises(ConnectorError): self.propose("m3", ack=w)
        self.assertEqual(self.book.get(w)["outcome"], "failed")

    # --- sending: the send step goes through the gate
    def test_send_needs_a_go(self):
        a = self.propose()["approval"]
        with self.assertRaises(ConnectorError) as e: self.m.send("info", a, confirm="yes")
        self.assertIn("no GO", str(e.exception))
        self.book.decide(a, "ok", by="owner")                               # decided, but not taken with consume
        with self.assertRaises(ConnectorError): self.m.send("info", a, confirm="yes")
        self.assertEqual(self.sends(), [])

    def test_approved_reply_goes_out_exactly_as_approved_and_is_ours(self):
        a = self.propose()["approval"]; self.go(a)
        self.assertTrue(self.m.send("info", a)["dry_run"]); self.assertEqual(self.sends(), [])
        out = self.m.send("info", a, confirm="yes")
        self.assertEqual((out["sent"], out["message_id"]), (True, "s1"))
        (_, _, body), = self.sends()
        raw = base64.urlsafe_b64decode(body["raw"]).decode()
        self.assertEqual(body["threadId"], "t1")
        self.assertIn("X-Jinnee-Agent: echo", raw); self.assertIn("In-Reply-To: <m1@mail>", raw)
        sent = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=email.policy.default)
        self.assertEqual((sent.get_content().strip(), sent["To"]), (REPLY, CUSTOMER))
        self.assertEqual(self.book.get(a)["outcome"], "done")
        self.assertEqual(json.loads((self.m.BRAIN / "mail_sent.json").read_text()), ["s1"])
        with self.assertRaises(ConnectorError): self.m.send("info", a, confirm="yes")   # an approval works once
        self.assertEqual(len(self.sends()), 1)

    def test_a_mail_that_came_in_after_the_proposal_stops_the_send(self):
        a = self.propose()["approval"]; self.go(a)
        self.thread.append(gmsg(2, f"Vevo <{CUSTOMER}>", 0, "Bocsi, inkább lemondom."))
        with self.assertRaises(ConnectorError) as e: self.m.send("info", a, confirm="yes")
        self.assertIn("nothing was sent", str(e.exception))
        self.assertEqual(self.sends(), []); self.assertEqual(self.book.get(a)["outcome"], "failed")

    def test_send_runs_the_gate_again_and_obeys_it(self):
        """Even with a GO, a gate that says no at send time stops the mail: the send path goes through the gate."""
        a = self.propose()["approval"]; self.go(a)
        held = {"ok": False, "signals": [{"signal": "complaint", "message": 1, "detail": "complaint word"}], "summary": "complaint word",
                "messages": 1, "last_id": "m1"}
        with mock.patch.object(self.m.mail_thread, "check", return_value=held) as gate:
            with self.assertRaises(ConnectorError): self.m.send("info", a, confirm="yes")
        gate.assert_called_once()
        self.assertEqual(self.sends(), [])

    def test_text_changed_after_approval_is_not_sent(self):
        a = self.propose()["approval"]; self.go(a)
        self.reply.write_text(REPLY + " Ps: 50% kedvezmény!", encoding="utf-8")
        with self.assertRaises(ConnectorError): self.m.send("info", a, confirm="yes")
        self.assertEqual(self.sends(), [])

    def test_an_item_queued_around_the_gate_cannot_be_sent(self):
        """A send_customer_email item added straight with core/approvals.py add (no thread check) is refused even with a GO."""
        a = self.book.add("echo", "send_customer_email", "Reply", text=REPLY)[0]["id"]; self.go(a)
        with self.assertRaises(ConnectorError) as e: self.m.send("info", a, confirm="yes")
        self.assertIn("propose_reply", str(e.exception))
        w = self.book.add("echo", "mail_thread_warning", "Warning", payload={"account": "info", "thread": "t1", "message": "m1"})[0]["id"]
        self.go(w)
        with self.assertRaises(ConnectorError): self.m.send("info", w, confirm="yes")
        self.assertEqual(self.sends(), [])

    def test_a_scheduled_run_never_sends(self):
        a = self.propose()["approval"]; self.go(a)
        with mock.patch.dict(os.environ, {"JINNEE_UNATTENDED": "1"}):
            self.assertEqual(run_cli(self.m.OPS, ["send", "--account", "info", "--approval", a, "--confirm", "yes"]), 1)
        self.assertEqual(self.sends(), [])

    def test_thread_check_reports_without_the_customer(self):
        self.thread = list(CANCELLED)
        out = self.m.thread_check("info", "m3")
        self.assertEqual((out["ok"], out["signals"]), (False, ["cancellation", "human_reply"]))
        self.assertNotIn("@", json.dumps(out))


class Wiring(unittest.TestCase):
    def test_echo_and_the_hourly_task_use_propose_reply(self):
        echo = (ROOT / "packs" / "general" / "agents" / "echo.md").read_text(encoding="utf-8")
        self.assertIn("propose_reply", echo)
        task = json.loads((ROOT / "packs" / "general" / "pack.json").read_text(encoding="utf-8"))["schedule"]["echo.customer_mail"]["task"]
        self.assertIn("propose_reply", task)

    def test_the_warning_is_locked_at_asking_the_owner(self):
        for pack in ("general", "ecom"):
            d = json.loads((ROOT / "packs" / pack / "pack.json").read_text(encoding="utf-8"))["autonomy_defaults"]
            if "mail_thread_warning" in d or pack == "general":
                self.assertEqual({k: d["mail_thread_warning"][k] for k in ("level", "max_level", "locked")}, {"level": 1, "max_level": 1, "locked": True})


if __name__ == "__main__":
    unittest.main()
