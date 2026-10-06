"""Autonomy levels: the gate, locks, and files from before locks existed. Run: python -m unittest discover tests"""
import json, pathlib, sys, tempfile, unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import approvals, autonomy

OLD_FLAT = {"send_customer_email": 3, "issue_invoice": 1, "spend_money": 0, "publish_social_post": 2,
            "draft_anything": 3, "read_anything": 3, "update_from_registry": 1, "post_story": 2}


class Brain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.brain = self.root / "brain"
        self.brain.mkdir()
        self.cfg = autonomy.Config(self.brain, ["general", "ecom"])
        self.book = approvals.Book(self.brain)
        self.book.gate = self.cfg

    def write(self, data):
        (self.brain / "autonomy_config.json").write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def file(self):
        return json.loads((self.brain / "autonomy_config.json").read_text(encoding="utf-8"))

    def fresh(self):
        self.write({"_format": autonomy.FORMAT, **self.cfg.defaults()})


class Gate(Brain):
    def test_fresh_install_locks_money_and_customers(self):
        table = self.cfg.defaults()
        for action, level in (("spend_money", 0), ("spend_ad_budget", 0), ("change_product_price", 0), ("issue_refund", 0),
                              ("void_or_edit_invoice", 0), ("issue_invoice", 1), ("send_customer_email", 1),
                              ("publish_social_post", 1), ("update_from_registry", 1), ("raise_autonomy", 1)):
            with self.subTest(action=action):
                self.assertTrue(table[action]["locked"])
                self.assertEqual(table[action]["level"], level)
                self.assertLessEqual(table[action]["max_level"], 2)
                if level == 0: self.assertEqual(table[action]["max_level"], 0)  # forbidden stays forbidden
        self.assertFalse(table["draft_anything"]["locked"])

    def test_levels(self):
        self.fresh()
        self.assertEqual(self.cfg.allowed("spend_money")[:2], (0, True))
        self.assertEqual(self.cfg.allowed("send_customer_email")[:2], (1, True))
        self.assertEqual(self.cfg.allowed("draft_anything")[:2], (3, False))
        level, needs_approval, reason = self.cfg.allowed("spend_money")
        self.assertIn("forbidden", reason)

    def test_unknown_action_needs_approval(self):
        for with_file in (False, True):
            if with_file: self.fresh()
            for action in ("launch_rocket", "", "SPEND_MONEY", "_format"):
                with self.subTest(action=action, with_file=with_file):
                    gate = self.cfg.allowed(action)
                    self.assertEqual((gate.level, gate.needs_approval), (1, True))

    def test_no_file_means_the_pack_defaults(self):
        self.assertEqual(self.cfg.allowed("send_customer_email")[:2], (1, True))
        self.assertEqual(self.cfg.read()[1], "")

    def test_broken_file_falls_back_to_defaults_and_says_so(self):
        for broken in ('{"send_customer_email": 3', "[]", "\x00\x01", '"3"'):
            with self.subTest(broken=broken):
                self.write(broken)
                table, problem = self.cfg.read()
                self.assertIn("autonomy_config.json", problem)
                self.assertEqual(self.cfg.allowed("send_customer_email")[:2], (1, True))
                self.assertEqual(self.cfg.allowed("spend_money").level, 0)
                self.assertIn("WARNING", self.cfg.describe())
                self.assertFalse(self.cfg.migrate())  # left alone for the owner to fix
                self.assertNotEqual(self.cfg.can_change("draft_anything", 2), "")

    def test_nonsense_values_fail_closed(self):
        self.fresh()
        data = self.file()
        data.update(send_customer_email={"level": "3", "locked": True}, issue_invoice={"level": 7}, draft_anything=True,
                    spend_money={"level": None, "max_level": 3, "locked": True}, read_anything={"level": -1})
        self.write(data)
        for action, level in (("send_customer_email", 1), ("issue_invoice", 1), ("draft_anything", 1), ("spend_money", 0),
                              ("read_anything", 1)):
            self.assertEqual(self.cfg.allowed(action).level, level, action)

    def test_a_level_written_above_a_locked_max_does_not_count(self):
        """An agent (or anyone) editing only the level gets nowhere: the lock caps what the gate reports."""
        self.fresh()
        data = self.file()
        data["spend_money"]["level"] = 3
        data["send_customer_email"]["level"] = 3
        data["issue_invoice"] = 3  # a bare number in a current file is a level, not permission to lift the ceiling
        data["raise_autonomy"] = {"level": 3, "max_level": 3, "locked": False, "label": ""}
        self.write(data)
        self.assertEqual(self.cfg.allowed("spend_money")[:2], (0, True))
        self.assertEqual(self.cfg.allowed("send_customer_email")[:2], (2, False))
        self.assertEqual(self.cfg.allowed("issue_invoice")[:2], (2, False))
        self.assertEqual(self.cfg.allowed("raise_autonomy")[:2], (1, True))  # not relaxable from the file at all

    def test_the_owner_can_unlock_by_hand(self):
        self.fresh()
        data = self.file()
        data["send_customer_email"].update(level=3, max_level=3)
        data["spend_money"].update(level=1, locked=False)
        self.write(data)
        self.assertEqual(self.cfg.allowed("send_customer_email")[:2], (3, False))
        self.assertEqual(self.cfg.allowed("spend_money")[:2], (1, True))


class Migration(Brain):
    def test_old_flat_file_keeps_the_owners_levels(self):
        self.write(OLD_FLAT)
        before = {a: self.cfg.allowed(a).level for a in OLD_FLAT}
        self.assertEqual(before, OLD_FLAT)  # readable as it is, before anything is rewritten
        self.assertTrue(self.cfg.migrate())
        data = self.file()
        self.assertEqual(data["_format"], autonomy.FORMAT)
        self.assertEqual({a: data[a]["level"] for a in OLD_FLAT}, OLD_FLAT)
        self.assertEqual(data["send_customer_email"], {"level": 3, "max_level": 3, "locked": True, "label": "Send an email to a customer"})
        self.assertEqual((data["publish_social_post"]["max_level"], data["publish_social_post"]["locked"]), (2, True))
        self.assertEqual((data["spend_money"]["max_level"], data["spend_money"]["locked"]), (0, True))
        self.assertEqual(data["post_story"], {"level": 2, "max_level": 3, "locked": False, "label": ""})  # the owner's own action
        self.assertEqual(data["issue_refund"]["level"], 0)  # arrived with the pack since
        self.assertEqual(data["raise_autonomy"], autonomy.CORE["raise_autonomy"])
        self.assertEqual(json.loads((self.brain / "autonomy_config.json.v1.bak").read_text()), OLD_FLAT)
        self.assertEqual({a: self.cfg.allowed(a).level for a in OLD_FLAT}, OLD_FLAT)
        self.assertFalse(self.cfg.migrate())  # once

    def test_old_pack_shape_still_loads(self):
        self.assertEqual(autonomy.entry(1), {"level": 1, "max_level": 3, "locked": False, "label": ""})
        self.assertEqual(autonomy.entry({"level": 1, "locked": True}), {"level": 1, "max_level": 1, "locked": True, "label": ""})

    def test_init_writes_the_new_shape_and_migrates_an_old_file(self):
        import shutil, subprocess, os
        for d in ("core", "packs"): shutil.copytree(REPO / d, self.root / d, ignore=shutil.ignore_patterns("__pycache__"))
        run = lambda: subprocess.run([sys.executable, "core/pack_loader.py", "--init"], cwd=self.root, capture_output=True, text=True,
                                     env={**os.environ, "PACKS": "general,ecom", "PYTHONDONTWRITEBYTECODE": "1"}, timeout=60)
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.file()
        self.assertEqual((data["_format"], data["spend_ad_budget"]["locked"], data["raise_autonomy"]["level"]), (autonomy.FORMAT, True, 1))
        self.assertNotIn("updated to the lockable format", r.stdout)
        self.write(OLD_FLAT)
        r = run()
        self.assertIn("updated to the lockable format", r.stdout)
        self.assertEqual(self.file()["send_customer_email"]["level"], 3)


class Raising(Brain):
    def setUp(self):
        super().setUp()
        self.fresh()

    def test_a_raise_is_only_a_request(self):
        item = self.cfg.propose("send_customer_email", 2, "20 replies\napproved unchanged", agent="jinnee")
        self.assertEqual((item["action"], item["state"], item["title"]), ("raise_autonomy", "pending", "send_customer_email: level 1 → 2"))
        self.assertEqual(self.cfg.allowed("send_customer_email").level, 1)
        with self.assertRaises(autonomy.Refused): self.cfg.apply(item["id"])  # not approved yet
        self.assertEqual(self.cfg.allowed("send_customer_email").level, 1)
        self.book.decide(item["id"], "ok", by="dashboard")
        self.assertEqual(self.cfg.apply(item["id"]), ("send_customer_email", 2))
        self.assertEqual(self.cfg.allowed("send_customer_email")[:2], (2, False))
        self.assertEqual(self.book.get(item["id"])["outcome"], "done")
        with self.assertRaises(autonomy.Refused): self.cfg.apply(item["id"])  # one approval, one change

    def test_agent_cannot_propose_past_a_lock(self):
        for action, level in (("spend_money", 1), ("send_customer_email", 3), ("change_product_price", 2), ("raise_autonomy", 2),
                              ("update_from_registry", 2), ("launch_rocket", 2), ("draft_anything", 9)):
            with self.subTest(action=action):
                with self.assertRaises(autonomy.Refused): self.cfg.propose(action, level, "please")
        self.assertEqual(self.book.read()[0], [])
        with self.assertRaises(autonomy.Refused) as e: self.cfg.propose("spend_money", 1, "please")
        self.assertIn("Only the owner", str(e.exception))

    def test_lowering_and_unlocked_actions_go_through_approval_too(self):
        item = self.cfg.propose("draft_anything", 1, "be careful for a while")
        self.book.decide(item["id"], "ok")
        self.cfg.apply(item["id"])
        self.assertEqual(self.cfg.allowed("draft_anything")[:2], (1, True))

    def test_dashboard_approval_cannot_lift_a_lock(self):
        """Even an approved item gets nowhere if it asks for more than the lock allows: a hand-made item, or a lock added since."""
        item = self.cfg.propose("send_customer_email", 2, "ok?")
        data = self.file(); data["send_customer_email"]["max_level"] = 1; self.write(data)  # the owner tightened it meanwhile
        self.book.decide(item["id"], "ok")
        with self.assertRaises(autonomy.Refused): self.cfg.apply(item["id"])
        self.assertEqual(self.cfg.allowed("send_customer_email").level, 1)
        self.assertEqual(self.book.get(item["id"])["outcome"], "withdrawn")
        forged = [{"id": "f1", "agent": "ops", "action": "raise_autonomy", "title": "spend", "state": "pending",
                   "payload": {"target": "spend_money", "to": 3}},
                  {"id": "f2", "agent": "ops", "action": "raise_autonomy", "title": "itself", "state": "pending",
                   "payload": {"target": "raise_autonomy", "to": 3}}]
        (self.brain / "approvals.json").write_text(json.dumps(forged))
        for fid in ("f1", "f2"):
            self.book.decide(fid, "ok")
            with self.assertRaises(autonomy.Refused): self.cfg.apply(fid)
        self.assertEqual(self.cfg.allowed("spend_money").level, 0)
        self.assertEqual(self.cfg.allowed("raise_autonomy").level, 1)

    def test_a_plain_approval_is_not_a_level_change(self):
        item, _ = self.book.add("office", "raise_autonomy", "make me free")  # the queue's own CLI cannot attach a target
        self.book.decide(item["id"], "ok")
        before = self.file()
        with self.assertRaises(autonomy.Refused): self.cfg.apply(item["id"])
        self.assertEqual(self.file(), before)

    def test_command_line(self):
        import subprocess, os, shutil
        for d in ("core", "packs"): shutil.copytree(REPO / d, self.root / d, ignore=shutil.ignore_patterns("__pycache__"))
        def run(*args):
            return subprocess.run([sys.executable, "core/autonomy.py", *args], cwd=self.root, capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PACKS": "general,ecom", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertIn("spend_money: level 0 (forbidden, locked at max 0)", run("list").stdout)
        self.assertIn("ASK FIRST", run("check", "anything_new").stdout)
        r = run("propose", "spend_money", "2", "--why", "trust me")
        self.assertEqual(r.returncode, 1); self.assertIn("REFUSED", r.stdout); self.assertNotIn("Traceback", r.stderr)
        r = run("propose", "issue_invoice", "2", "--why", "a month without a correction")
        self.assertEqual(r.returncode, 0, r.stderr)
        item_id = r.stdout.split()[1].rstrip(":")
        r = run("apply", item_id)
        self.assertEqual(r.returncode, 1); self.assertIn("not decided yet", r.stdout)
        self.book.decide(item_id, "ok")
        r = run("apply", item_id)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.file()["issue_invoice"]["level"], 2)


if __name__ == "__main__":
    unittest.main()
