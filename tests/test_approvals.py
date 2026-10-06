"""The approval queue: one life per item. Run: python -m unittest discover tests"""
import json, os, pathlib, shutil, subprocess, sys, tempfile, threading, time, unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import approvals, autonomy, store

MAIL = "Dear Anna, your invoice is attached."


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.brain = self.root / "brain"
        (self.root / "handoffs" / "office").mkdir(parents=True)
        self.brain.mkdir()
        self.mail = self.root / "handoffs" / "office" / "mail.md"
        self.mail.write_text(MAIL, encoding="utf-8")
        self.book = approvals.Book(self.brain)
        self.book.gate = autonomy.Config(self.brain, ["general", "ecom"])

    def add(self, **kw):
        args = {"agent": "office", "action": "send_customer_email", "title": "Reply to Anna", "file": "handoffs/office/mail.md", **kw}
        return self.book.add(**args)[0]["id"]

    def file(self):
        return json.loads((self.brain / "approvals.json").read_text(encoding="utf-8"))

    def text(self, name):
        p = self.brain / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def state(self, item_id):
        it = self.book.get(item_id)
        return it["state"], it["decision"], it["outcome"]


class Lifecycle(Queue):
    def test_pending_decided_consumed(self):
        a = self.add(summary="invoice\nreminder")
        it = self.book.get(a)
        self.assertEqual((it["state"], it["agent"], it["action"], it["summary"], it["outbound"]),
                         ("pending", "office", "send_customer_email", "invoice reminder", True))
        self.assertEqual(self.book.consume(a).word, "REFUSED")  # nothing to act on yet
        self.assertEqual(self.book.check(a)[0], False)
        self.book.decide(a, "ok", by="dashboard")
        it = self.book.get(a)
        self.assertEqual((it["state"], it["decision"], it["decided_by"], it["sendable"]), ("decided", "ok", "dashboard", True))
        self.assertIsNotNone(it["decided_at"])
        self.assertTrue(self.book.check(a)[0])
        v = self.book.consume(a)
        self.assertEqual(v.word, "GO")
        self.assertEqual(self.state(a), ("consumed", "ok", "executing"))
        self.book.result(a, "sent,\nmessage id 77")
        it = self.book.get(a)
        self.assertEqual((it["outcome"], it["result"]), ("done", "sent, message id 77"))
        self.assertIsNotNone(it["consumed_at"]); self.assertIsNotNone(it["finished_at"])
        log = self.text("decisions.log.md")
        for line in (f"dashboard: {a} → ok", f"office: {a} → taken, carrying it out", f"office: {a} → done: sent, message id 77"):
            self.assertIn(line, log)

    def test_consume_works_once(self):
        a = self.add()
        self.book.decide(a, "ok")
        self.assertEqual([self.book.consume(a).word for _ in range(3)], ["GO", "REFUSED", "REFUSED"])
        self.assertIn("works once", self.book.consume(a).text)
        self.book.result(a, "sent")
        self.assertEqual(self.book.consume(a).word, "REFUSED")  # and not after it is done either
        with self.assertRaises(approvals.Conflict): self.book.result(a, "sent again")
        self.assertEqual(self.book.consume("nope").word, "REFUSED")

    def test_a_decided_item_cannot_be_decided_again(self):
        a = self.add()
        self.book.decide(a, "ok")
        for again in ("ok", "edit", "drop"):
            with self.assertRaises(approvals.Conflict): self.book.decide(a, again, "changed my mind")
        self.assertEqual(self.state(a), ("decided", "ok", None))
        self.book.consume(a)
        with self.assertRaises(approvals.Conflict): self.book.decide(a, "drop", "too late")
        self.assertEqual(self.state(a), ("consumed", "ok", "executing"))
        self.assertEqual(self.text("decisions.log.md").count(f"{a} → "), 2)  # the ok and the pick-up, no refused attempt

    def test_undo_until_consumed(self):
        a = self.add()
        with self.assertRaises(approvals.Conflict): self.book.undo(a)  # nothing to undo
        self.book.decide(a, "ok")
        self.book.undo(a, by="telegram")
        it = self.book.get(a)
        self.assertEqual((it["state"], it["decision"], it["decided_at"], it["sha"], it["sendable"]), ("pending", None, None, None, None))
        self.assertEqual(self.book.consume(a).word, "REFUSED")  # the undone approval is gone
        self.assertIn(f"telegram: {a} → undo", self.text("decisions.log.md"))
        self.book.decide(a, "edit", "shorter")  # and it can be decided afresh
        self.assertEqual(self.book.consume(a).word, "CHANGE")
        with self.assertRaises(approvals.Conflict) as e: self.book.undo(a)
        self.assertIn("can no longer be undone", str(e.exception))
        b = self.add()
        self.book.decide(b, "ok"); self.book.consume(b)
        with self.assertRaises(approvals.Conflict): self.book.undo(b)
        self.assertEqual(self.state(b), ("consumed", "ok", "executing"))

    def test_a_failed_attempt_is_closed_too(self):
        a = self.add()
        self.book.decide(a, "ok"); self.book.consume(a)
        self.book.result(a, "connector did not answer", failed=True)
        self.assertEqual(self.state(a), ("consumed", "ok", "failed"))
        self.assertEqual(self.book.consume(a).word, "REFUSED")  # trying again needs a new approval

    def test_edit_returns_the_note_and_the_new_version_needs_a_new_decision(self):
        a = self.add()
        with self.assertRaises(approvals.Refused): self.book.decide(a, "edit", "  ")
        self.book.decide(a, "edit", "address her formally")
        v = self.book.consume(a)
        self.assertEqual(v.word, "CHANGE"); self.assertIn("address her formally", v.text)
        self.assertEqual(self.state(a), ("consumed", "edit", "returned"))
        self.mail.write_text("Dear Ms Kovacs, your invoice is attached.", encoding="utf-8")
        b = self.add(replaces=a)
        it = self.book.get(b)
        self.assertEqual((it["state"], it["decision"], it["replaces"]), ("pending", None, a))
        self.assertEqual(self.book.consume(b).word, "REFUSED")

    def test_replacing_an_approved_item_drops_the_approval(self):
        a = self.add()
        self.book.decide(a, "ok")
        b = self.add(replaces=a)
        self.assertEqual(self.state(a), ("consumed", "ok", "superseded"))
        self.assertEqual(self.book.get(a)["result"], f"replaced by {b}")
        self.assertEqual(self.book.consume(a).word, "REFUSED")
        self.assertEqual(self.book.consume(b).word, "REFUSED")
        with self.assertRaises(approvals.Missing): self.add(replaces="nope")

    def test_changed_content_voids_the_approval(self):
        a = self.add()
        self.book.decide(a, "ok")
        self.mail.write_text(MAIL + " PS: 50% off everything.", encoding="utf-8")
        v = self.book.consume(a)
        self.assertEqual(v.word, "REFUSED"); self.assertIn("changed after the owner approved", v.text)
        self.assertEqual(self.state(a), ("decided", "ok", None))
        self.mail.write_text(MAIL, encoding="utf-8")  # exactly what was approved again
        self.assertEqual(self.book.consume(a).word, "GO")

    def test_drop_needs_a_reason_and_the_team_learns_it(self):
        a = self.add()
        for empty in ("", "   ", "\n"):
            with self.assertRaises(approvals.Refused): self.book.decide(a, "drop", empty)
        self.assertEqual(self.state(a)[0], "pending")
        (self.brain / "lessons.md").write_text("# lessons – the owner's corrections\n- no exclamation marks\n", encoding="utf-8")
        self.book.decide(a, "drop", "we never chase\ninvoices under 10 EUR")
        lessons = self.text("lessons.md")
        self.assertIn(f'dropped "Reply to Anna" (office, send_customer_email): we never chase invoices under 10 EUR [approval {a}]', lessons)
        self.assertIn(f"dashboard: {a} → drop we never chase invoices under 10 EUR", self.text("decisions.log.md"))
        self.book.undo(a)  # a slip: the reason comes back out, the rest of the file is untouched
        self.assertEqual(self.text("lessons.md"), "# lessons – the owner's corrections\n- no exclamation marks\n")
        self.book.decide(a, "drop", "not this one")
        v = self.book.consume(a)
        self.assertEqual((v.word, self.state(a)), ("DROPPED", ("consumed", "drop", "dropped")))
        self.assertIn("not this one", self.text("lessons.md"))

    def test_withdraw(self):
        a = self.add()
        self.book.withdraw(a, "order was cancelled")
        self.assertEqual(self.state(a), ("consumed", None, "withdrawn"))
        with self.assertRaises(approvals.Conflict): self.book.decide(a, "ok")
        with self.assertRaises(approvals.Conflict): self.book.withdraw(a)

    def test_closed_items_leave_the_file_after_a_month(self):
        a, b, c = self.add(), self.add(), self.add()
        self.book.decide(a, "ok"); self.book.consume(a); self.book.result(a, "sent")
        self.book.decide(b, "ok"); self.book.consume(b)  # still being carried out
        items = self.file()
        for it in items: it["created_at"] = 1; it["consumed_at"] = it["consumed_at"] and 2; it["finished_at"] = it["finished_at"] and 3
        (self.brain / "approvals.json").write_text(json.dumps(items))
        self.book.tidy()
        self.assertEqual([it["id"] for it in self.file()], [b, c])


class Gatekeeping(Queue):
    def test_forbidden_and_free_actions_are_not_queued(self):
        with self.assertRaises(approvals.Refused) as e: self.add(action="spend_money", file=None)
        self.assertIn("forbidden", str(e.exception))
        with self.assertRaises(approvals.NotNeeded): self.add(action="draft_anything", file=None)
        self.assertEqual(self.book.read()[0], [])

    def test_unknown_action_is_queued_for_approval(self):
        a = self.add(action="order_new_stock", file=None)
        self.assertEqual(self.state(a)[0], "pending")

    def test_an_approval_does_not_outlive_the_action_being_forbidden(self):
        a = self.add()
        self.book.decide(a, "ok")
        table = self.book.gate.defaults(); table["send_customer_email"].update(level=0, max_level=0)
        self.book.gate.write(table)
        v = self.book.consume(a)
        self.assertEqual(v.word, "REFUSED"); self.assertIn("forbidden", v.text)

    def test_there_is_no_way_to_decide_from_the_command_line(self):
        r = subprocess.run([sys.executable, str(REPO / "core" / "approvals.py"), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        for word in ("decide", "approve", "undo"):
            self.assertNotIn(word, r.stdout.split("positional arguments:")[1].split("options:")[0])

    def test_file_must_be_inside_handoffs_or_brain(self):
        (self.root / ".env").write_text("TELEGRAM_BOT_TOKEN=secret\n")
        for path in (".env", "../outside.md", "/etc/passwd", "handoffs/../.env", "handoffs/office/missing.md"):
            with self.subTest(path=path):
                with self.assertRaises(approvals.Refused): self.add(file=path)
        with self.assertRaises(approvals.Refused): self.add(text="both")
        self.assertEqual(self.book.read()[0], [])


class Sendable(Queue):
    def test_marker_rules(self):
        for text in ("", "   \n\t", "DRAFT", "DRAFT – approve me", "# DRAFT\nHello", "**Draft:** Hello", "[draft] hello", "TODO write this",
                     "draft\nHello", "Lorem ipsum dolor", "Vázlat: szia", "PLACEHOLDER", "> TBD"):
            self.assertNotEqual(approvals.unsendable(text), "", repr(text))
        for text in ("Hello Anna", "Draft beer is back on tap!", "Todo lists made easy", "Our drafting service", "Re: your draft"):
            self.assertEqual(approvals.unsendable(text), "", repr(text))

    def test_empty_or_draft_text_is_approved_but_not_sendable(self):
        for body, why in (("", "empty"), ("  \n", "empty"), ("DRAFT: Dear Anna…", "draft marker")):
            with self.subTest(body=body):
                a = self.add(file=None, text=body)
                self.assertIn(why, self.book.view(self.book.get(a))["warning"])  # the owner sees it before deciding
                it = self.book.decide(a, "ok")
                self.assertEqual((it["state"], it["decision"], it["sendable"]), ("decided", "ok", False))
                self.assertIn(why, it["blocked"])
                self.assertIn("approved, but not sendable", self.book.status(it))
                v = self.book.consume(a)
                self.assertEqual(v.word, "REFUSED"); self.assertIn("not sendable", v.text)
                self.assertEqual(self.state(a), ("decided", "ok", None))  # nothing went out, nothing was used up

    def test_a_file_that_was_emptied_or_removed(self):
        a = self.add()
        self.mail.write_text("")
        self.assertFalse(self.book.decide(a, "ok")["sendable"])
        b = self.add(file=None, text="x")
        c = self.add()
        self.mail.unlink()
        it = self.book.decide(c, "ok")
        self.assertFalse(it["sendable"]); self.assertIn("cannot be read", it["blocked"])

    def test_fixing_it_means_asking_again(self):
        a = self.add(file=None, text="TODO")
        self.book.decide(a, "ok")
        b = self.add(file=None, text="Hello Anna", replaces=a)
        self.assertEqual(self.state(a)[2], "superseded")
        self.book.decide(b, "ok")
        self.assertEqual(self.book.consume(b).word, "GO")

    def test_reference_material_and_items_without_text_are_not_checked(self):
        a = self.add(file=None, text="TODO list for the theme change", reference=True, action="change_live_shop")
        b = self.add(file=None)
        for i in (a, b):
            self.assertTrue(self.book.decide(i, "ok")["sendable"])
            self.assertEqual(self.book.consume(i).word, "GO")


class Files(Queue):
    def test_missing_and_empty_file(self):
        self.assertEqual(self.book.read(), ([], ""))
        (self.brain / "approvals.json").write_text("")
        self.assertEqual(self.book.read(), ([], ""))
        self.assertEqual(self.state(self.add())[0], "pending")

    def test_broken_file_is_set_aside_not_trusted(self):
        for broken in ('[{"id": "a1", "decision": "ok", "state": "decided"', '{"id": "a1"}', "\x00garbage", '"[]"'):
            with self.subTest(broken=broken):
                for f in self.brain.glob("approvals.json*"): f.unlink()
                (self.brain / "approvals.json").write_text(broken)
                items, problem = self.book.read()
                self.assertEqual(items, []); self.assertIn("approvals.json", problem)
                self.assertEqual(self.book.consume("a1").word, "REFUSED")  # nothing in a broken file can be acted on
                kept = [f for f in self.brain.glob("approvals.json.broken-*")]
                self.assertEqual(len(kept), 1)
                self.assertEqual(kept[0].read_text(), broken)  # still there for the owner to look at
                self.assertEqual(self.file(), [])
                self.assertIn("could not be read", self.text("decisions.log.md"))
                self.assertEqual(self.book.read()[1], "")
                self.assertEqual(self.state(self.add())[0], "pending")  # and the queue works again

    def test_items_from_before_the_lifecycle(self):
        old = [{"id": "a1", "agent": "office", "title": "Reply", "summary": "s"},
               {"id": "a2", "agent": "office", "title": "Sent last week", "decision": "ok", "note": "", "decided_at": 1759000000},
               {"id": "a3", "agent": "social", "title": "Post", "decision": "drop", "note": "no"},
               "not an item", {"id": "a1", "title": "same id twice"}, {"title": "no id"}]
        (self.brain / "approvals.json").write_text(json.dumps(old))
        items = {it["title"]: it for it in self.book.read()[0]}
        self.assertEqual(len(items), 5)
        self.assertEqual((items["Reply"]["state"], items["Reply"]["action"]), ("pending", ""))
        self.assertEqual((items["Sent last week"]["state"], items["Sent last week"]["outcome"]), ("consumed", "closed"))
        self.assertEqual(self.book.consume("a2").word, "REFUSED")  # was it carried out back then? nobody knows: never twice
        self.assertEqual(self.book.consume("a3").word, "REFUSED")
        self.assertEqual(len({it["id"] for it in items.values()}), 5)
        self.book.decide("a1", "ok")  # an old pending item has no action: it is treated as needing approval, and can get it
        self.assertEqual(self.book.consume("a1").word, "GO")
        self.assertTrue(all(set(approvals.FIELDS) <= set(it) for it in self.file()))

    def test_no_temp_files_left_behind(self):
        self.add(); self.add()
        self.assertEqual(sorted(p.name for p in self.brain.iterdir()), [".lock", "approvals.json"])


WORKER = """\
import sys
sys.path.insert(0, sys.argv[1])
import approvals, autonomy
book = approvals.Book(sys.argv[2]); book.gate = autonomy.Config(sys.argv[2], ["general"])
mode, n = sys.argv[3], int(sys.argv[4])
if mode == "add":
    for i in range(n): book.add("office", "send_customer_email", f"{sys.argv[5]}-{i}", text="hello")
elif mode == "consume":
    print(book.consume(sys.argv[5]).word)
elif mode == "decide":
    for item_id in sys.argv[5:]:
        try: book.decide(item_id, "ok", by="dashboard")
        except approvals.Refused: pass
"""


class Parallel(Queue):
    def worker(self, *args):
        return subprocess.Popen([sys.executable, "-c", WORKER, str(REPO / "core"), str(self.brain), *map(str, args)],
                                stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_processes_adding_and_deciding_at_once_lose_nothing(self):
        first = [self.add(file=None, text="hi") for _ in range(10)]
        procs = [self.worker("add", 15, f"w{i}") for i in range(4)] + [self.worker("decide", 0, *first)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
            p.stdout.close()
        items = self.file()  # still valid JSON, and complete
        self.assertEqual(len(items), 70)
        self.assertEqual(len({it["id"] for it in items}), 70)
        self.assertEqual(sorted(it["title"] for it in items if it["title"].startswith("w")),
                         sorted(f"w{i}-{j}" for i in range(4) for j in range(15)))
        self.assertTrue(all(it["state"] == "decided" for it in items if it["id"] in first))
        self.assertEqual(self.text("decisions.log.md").count("→ ok"), 10)

    def test_only_one_process_gets_the_go(self):
        a = self.add()
        self.book.decide(a, "ok")
        procs = [self.worker("consume", 0, a) for _ in range(8)]
        words = sorted(p.communicate(timeout=120)[0].strip() for p in procs)
        self.assertEqual(words, ["GO"] + ["REFUSED"] * 7)

    def test_only_one_thread_gets_the_go(self):
        a = self.add()
        self.book.decide(a, "ok")
        words, start = [], threading.Barrier(8)
        def take():
            start.wait(); words.append(approvals.Book(self.brain).consume(a).word)
        threads = [threading.Thread(target=take) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(words), ["GO"] + ["REFUSED"] * 7)

    def test_a_reader_never_sees_half_a_file(self):
        stop, bad = threading.Event(), []
        def read():
            while not stop.is_set():
                try: json.loads((self.brain / "approvals.json").read_text(encoding="utf-8") or "[]")
                except FileNotFoundError: pass
                except ValueError as e: bad.append(e)
        t = threading.Thread(target=read); t.start()
        try:
            for i in range(60): self.add(file=None, text="x" * 2000)
        finally:
            stop.set(); t.join()
        self.assertEqual(bad, [])

    def test_lock_can_be_taken_again_by_the_same_thread(self):
        with store.locked(self.brain):
            with store.locked(self.brain):
                self.add()
        self.assertEqual(len(self.file()), 1)


class CommandLine(Queue):
    def setUp(self):
        super().setUp()
        for d in ("core", "packs"): shutil.copytree(REPO / d, self.root / d, ignore=shutil.ignore_patterns("__pycache__"))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "core/approvals.py", *args], cwd=self.root, capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PACKS": "general,ecom", "PYTHONDONTWRITEBYTECODE": "1"})

    def test_the_agents_path(self):
        r = self.run_cli("add", "--agent", "office", "--action", "send_customer_email", "--title", "Reply to Anna",
                         "--file", "handoffs/office/mail.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = r.stdout.split()[1].rstrip(":")
        self.assertIn(f"[{a}] Reply to Anna", self.run_cli("list").stdout)
        r = self.run_cli("consume", a)
        self.assertEqual((r.returncode, r.stdout.split()[0]), (1, "REFUSED"))
        approvals.Book(self.brain).decide(a, "ok")
        self.assertIn("DECIDED, take it now", self.run_cli("list").stdout)
        r = self.run_cli("consume", a)
        self.assertEqual((r.returncode, r.stdout.split()[0]), (0, "GO"))
        r = self.run_cli("consume", a)
        self.assertEqual((r.returncode, r.stdout.split()[0]), (1, "REFUSED"))
        self.assertIn("TAKEN, result missing", self.run_cli("list").stdout)
        self.assertEqual(self.run_cli("result", a, "sent").returncode, 0)
        self.assertEqual(self.run_cli("list").stdout.strip(), "Nothing is open.")
        self.assertIn("status: done", self.run_cli("show", a).stdout)

    def test_exit_codes_other_than_go(self):
        codes = {}
        for decision in ("edit", "drop"):
            a = self.add()
            approvals.Book(self.brain).decide(a, decision, "because")
            r = self.run_cli("consume", a)
            codes[r.stdout.split()[0]] = r.returncode
        codes["free"] = self.run_cli("add", "--agent", "o", "--action", "draft_anything", "--title", "t").returncode
        codes["forbidden"] = self.run_cli("add", "--agent", "o", "--action", "spend_money", "--title", "t").returncode
        codes["missing"] = self.run_cli("show", "nope").returncode
        self.assertEqual(codes, {"CHANGE": 3, "DROPPED": 3, "free": 2, "forbidden": 1, "missing": 1})  # 0 is GO and nothing else

    def test_draft_warning_on_add(self):
        r = self.run_cli("add", "--agent", "social", "--action", "publish_social_post", "--title", "Post", "--text", "DRAFT")
        self.assertEqual(r.returncode, 0)
        self.assertIn("WARNING", r.stdout)


if __name__ == "__main__":
    unittest.main()
