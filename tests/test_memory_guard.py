"""The hourly memory tidy (core/memory_guard.py): forge_notes.md moves by itself, lessons.md only with the owner's /ok.
Run: python -m unittest discover tests
The rules below are made up for the tests; the shape is the one that went wrong: a title, an intro line under it, and
old undated-in-the-text rules over the cap."""
import datetime as dt, json, os, pathlib, sys, tempfile, unittest
from unittest import mock

import test_env as base
import test_alerts

REPO = pathlib.Path(__file__).resolve().parent.parent
os.environ["JINNEE_LANG"] = "en"  # the owner's texts are checked in English here, whatever this machine is set to (test_texts checks hu)
sys.path.insert(0, str(REPO / "core"))
import memory, memory_guard, approvals, autonomy, alerts

TODAY = dt.date(2026, 11, 6)
NOW = dt.datetime(2026, 11, 6, 12).timestamp()
DAY = 86400
TITLE = "# Lessons: the owner's corrections\n"
INTRO = "The owner's corrections. Every agent reads this.\n"
RULES = ["never promise a delivery date the carrier has not confirmed", "write to customers in a calm, plain tone",
         "a refund question always goes to the owner first", "no health claims about any product",
         "photos only with the written consent of the people in them", "keep customer addresses in the shop system only",
         "every order is checked against stock before it is confirmed", "a complaint gets an answer within one day"]


def old_rules(n=17, start=dt.date(2026, 10, 6)):
    """Old rules, each long enough that a few of them push the file over a small cap."""
    out = []
    for i in range(n):
        day = start if i < 9 else start + dt.timedelta(days=6)
        out.append(f"- {day} {RULES[i % len(RULES)]} (rule {i}); " + "background " * 12 + "\n")
    return out


def newer_rules(n=10):
    return [f"- {TODAY - dt.timedelta(days=2)} a fresh correction {i}, kept because it is recent\n" for i in range(n)]


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brain = pathlib.Path(self.tmp.name) / "brain"
        self.brain.mkdir()
        env = mock.patch.dict(os.environ, {"PACKS": "general,ecom", "JINNEE_MEMORY_LESSONS_BYTES": "2500"})
        env.start(); self.addCleanup(env.stop)
        for k in ("QUIET_HOURS", "UNDO_SECONDS", "JINNEE_TZ", "JINNEE_UNATTENDED", "JINNEE_MEMORY_NOTES_BYTES"): os.environ.pop(k, None)
        self.book = approvals.Book(self.brain)
        self.book.gate = autonomy.Config(self.brain, ["general", "ecom"])
        self.rules = old_rules()
        self.before = TITLE + INTRO + "".join(self.rules) + "".join(newer_rules())
        self.write("lessons.md", self.before)
        self.assertGreater(len(self.before.encode()), 2500)  # over the cap, as on the live machine

    def write(self, name, text): (self.brain / name).write_text(text, encoding="utf-8")
    def text(self, name): return (self.brain / name).read_text(encoding="utf-8")

    def tidy(self, now=NOW):
        today = dt.datetime.fromtimestamp(now).date()
        with mock.patch("time.time", return_value=now):
            return memory_guard.tidy(self.brain, self.book, today=today, now=now)

    def mine(self):
        return [i for i in self.book.read()[0] if memory_guard.ours(i)]

    def decide(self, item_id, decision, note="", now=NOW):
        with mock.patch("time.time", return_value=now):
            self.book.decide(item_id, decision, note, by="telegram")

    def carry_out(self, item_id, now=NOW):
        with mock.patch("time.time", return_value=now):
            return memory_guard.carry_out(self.brain, item_id, self.book, today=TODAY)

    # --- the incident
    def test_nothing_moves_from_lessons_without_the_owner(self):
        log = self.tidy()
        self.assertEqual(self.text("lessons.md"), self.before)  # byte for byte
        self.assertFalse((self.brain / "lessons.archive.md").exists())
        self.assertFalse((self.brain / "backups").exists())
        items = self.mine()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it["state"], it["action"], it["agent"], it["outbound"]), ("pending", "archive_lessons", "memory", False))
        self.assertIn("asked the owner", log[-1])
        entries = it["payload"]["entries"]
        self.assertTrue(entries)
        self.assertTrue(all(e.startswith("- 2026-10-") for e in entries))  # the oldest rules, never the fresh ones
        self.assertFalse([e for e in entries if "Every agent reads this" in e])  # the intro is not a line to move
        # asked once: the next hours wait for the answer
        for h in range(1, 5): self.tidy(NOW + h * 3600)
        self.assertEqual(len(self.mine()), 1)
        self.assertEqual(self.text("lessons.md"), self.before)

    def test_the_owner_is_told_what_would_move_and_what_to_do(self):
        self.tidy()
        watch = alerts.Watch(self.brain)
        watch.book.gate = self.book.gate
        os.environ["QUIET_HOURS"] = "off"
        msg = watch.due(NOW)["messages"][0]
        self.assertIn("Waiting for your decision", msg)
        self.assertIn("Move old lessons to the archive", msg)  # the label of the core action
        self.assertIn("rule 0", msg)                          # an excerpt of what would move
        self.assertIn("[pin]", msg)
        self.assertIn("JINNEE_MEMORY_LESSONS_BYTES", msg)
        self.assertIn("/ok", msg)
        os.environ["QUIET_HOURS"] = "00:00-23:59"            # quiet hours: the item waits, nothing is said
        self.assertEqual(alerts.Watch(self.brain).due(NOW)["messages"], [])

    def test_the_excerpt_leaves_out_customer_details(self):
        self.write("lessons.md", TITLE + INTRO + "- 2026-09-01 the customer at someone@example.com with order #48213 "
                   "and phone +36 30 123 4567 gets a call back " + "x " * 40 + "\n" + "".join(self.rules))
        self.tidy()
        said = "\n".join(self.mine()[0]["payload"]["notice"])
        for leak in ("someone@example.com", "48213", "123 4567"): self.assertNotIn(leak, said)
        self.assertIn("the customer at [email]", said)

    # --- the owner says yes
    def test_after_ok_only_the_approved_lines_move_and_the_intro_stays(self):
        self.tidy()
        it = self.mine()[0]
        self.decide(it["id"], "ok")
        said = self.carry_out(it["id"])
        self.assertIn("Done", said)
        after, archive = self.text("lessons.md"), self.text("lessons.archive.md")
        self.assertTrue(after.startswith(TITLE + INTRO))  # the preamble never moves
        moved = it["payload"]["entries"]
        for e in moved:
            self.assertNotIn(e, after); self.assertIn(e, archive)
        for line in newer_rules(): self.assertIn(line, after)
        self.assertNotIn("Every agent reads this", archive)
        self.assertLessEqual(len(after.encode()), 2500)
        done = self.book.get(it["id"])
        self.assertEqual((done["state"], done["outcome"]), ("consumed", "done"))
        self.assertIn(f"moved {len(moved)} of {len(moved)}", done["result"])
        self.assertEqual(self.tidy(NOW + 3600), [])  # within the cap: no new question
        self.assertEqual(len(self.mine()), 1)
        self.assertEqual(self.carry_out(it["id"]), "")  # an approval works once

    def test_a_line_pinned_before_the_ok_stays(self):
        self.tidy()
        it = self.mine()[0]
        first = it["payload"]["entries"][0]
        self.write("lessons.md", self.text("lessons.md").replace(first, first + " [pin]"))
        self.decide(it["id"], "ok")
        said = self.carry_out(it["id"])
        after = self.text("lessons.md")
        self.assertIn(first + " [pin]", after)
        self.assertNotIn(first, self.text("lessons.archive.md"))
        self.assertIn("stayed", said)
        self.assertTrue(after.startswith(TITLE + INTRO))

    def test_a_line_added_after_the_question_is_not_moved_by_its_ok(self):
        self.tidy()
        it = self.mine()[0]
        extra = "- 2026-09-30 an even older rule written in later " + "y " * 30 + "\n"
        self.write("lessons.md", TITLE + INTRO + extra + self.text("lessons.md")[len(TITLE + INTRO):])
        self.decide(it["id"], "ok")
        self.carry_out(it["id"])
        self.assertIn(extra, self.text("lessons.md"))  # not on the list the owner saw

    # --- the owner says no, or changes the cap
    def test_drop_moves_nothing_and_asks_again_only_a_week_later(self):
        self.tidy()
        it = self.mine()[0]
        self.decide(it["id"], "drop", "keep them for now")
        with mock.patch("time.time", return_value=NOW):
            self.assertEqual(self.book.consume(it["id"]).word, "DROPPED")  # what the bridge does with a drop
        self.assertFalse((self.brain / "lessons.archive.md").exists())
        self.assertIn(self.before, self.text("lessons.md"))  # untouched; the drop reason is appended below, as for any drop
        self.tidy(NOW + 3 * DAY)
        self.assertEqual(len(self.mine()), 1)
        self.tidy(NOW + 8 * DAY)
        self.assertEqual(len(self.mine()), 2)
        self.assertFalse((self.brain / "lessons.archive.md").exists())

    def test_change_is_taken_as_not_now(self):
        self.tidy()
        it = self.mine()[0]
        self.decide(it["id"], "edit", "only the oldest three")
        said = self.carry_out(it["id"])
        self.assertIn("Nothing moved", said); self.assertIn("[pin]", said)
        self.assertEqual(self.text("lessons.md"), self.before)
        self.assertEqual(self.book.get(it["id"])["outcome"], "returned")
        self.tidy(NOW + DAY)
        self.assertEqual(len(self.mine()), 1)

    def test_a_raised_cap_withdraws_the_question(self):
        self.tidy()
        os.environ["JINNEE_MEMORY_LESSONS_BYTES"] = "12000"
        log = self.tidy(NOW + 3600)
        it = self.book.get(self.mine()[0]["id"])
        self.assertEqual((it["state"], it["outcome"]), ("consumed", "withdrawn"))
        self.assertIn("withdrawn", log[0])
        self.assertEqual(self.text("lessons.md"), self.before)

    # --- the gate
    def test_the_action_is_locked_at_approval_required(self):
        gate = self.book.gate
        self.assertTrue(gate.allowed("archive_lessons").needs_approval)
        with self.assertRaises(autonomy.Refused): gate.propose("archive_lessons", 2, "no more questions")
        self.write("autonomy_config.json", json.dumps({"_format": 2, "archive_lessons": {"level": 3}}))
        self.assertTrue(gate.allowed("archive_lessons").needs_approval)  # locked: the file alone does not raise it

    def test_forbidden_means_never_and_a_hand_raised_level_acts_and_logs(self):
        self.write("autonomy_config.json", json.dumps({"_format": 2, "archive_lessons": {"level": 0}}))
        self.tidy()
        self.assertEqual((self.mine(), self.text("lessons.md")), ([], self.before))
        self.write("autonomy_config.json", json.dumps({"_format": 2, "archive_lessons": {"level": 2, "locked": False}}))
        self.tidy()
        self.assertEqual(self.mine(), [])
        self.assertTrue(self.text("lessons.md").startswith(TITLE + INTRO))
        self.assertIn("rule 0", self.text("lessons.archive.md"))
        self.assertIn("memory: ", self.text("decisions.log.md"))

    # --- the technical notebook stays automatic
    def test_forge_notes_still_move_by_themselves(self):
        self.write("forge_notes.md", "# notes\nTechnical notes.\n" + "".join(
            f"- 2026-0{1 + i % 6}-01 connector note {i} " + "z" * 60 + "\n" for i in range(200)))
        log = self.tidy()
        self.assertTrue((self.brain / "forge_notes.archive.md").exists())
        self.assertTrue(self.text("forge_notes.md").startswith("# notes\nTechnical notes.\n"))
        self.assertIn("forge_notes.md moved", log[0])
        self.assertEqual(self.text("lessons.md"), self.before)

    def test_an_agent_item_with_the_same_action_is_not_carried_out_here(self):
        item = self.book.add("jinnee", "archive_lessons", "please archive", text="x", reference=True)[0]
        self.assertFalse(memory_guard.ours(item))
        self.decide(item["id"], "ok")
        self.assertEqual(self.carry_out(item["id"]), "")
        self.assertEqual(self.book.get(item["id"])["state"], "decided")
        self.assertEqual(self.text("lessons.md"), self.before)


class Stats(unittest.TestCase):
    def test_stats_and_compact_advise_pin(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JINNEE_MEMORY_LESSONS_BYTES": "2500"}):
            brain = pathlib.Path(tmp)
            (brain / "lessons.md").write_text(TITLE + INTRO + "".join(old_rules(40)), encoding="utf-8")
            from contextlib import redirect_stdout
            from io import StringIO
            said = {}
            for argv in (["stats"], ["compact"]):
                out = StringIO()
                with redirect_stdout(out): memory.main(argv, brain)
                said[argv[0]] = out.getvalue()
                self.assertIn("[pin]", said[argv[0]], argv)
                self.assertIn("JINNEE_MEMORY_LESSONS_BYTES", said[argv[0]], argv)
            self.assertIn("with the owner's /ok", said["stats"])
            self.assertIn("rule 0", said["compact"])  # the dry run names the lines it would move
            self.assertFalse((brain / "lessons.archive.md").exists())


class BridgeEndToEnd(test_alerts.Bridge):
    """core/jinnee.py with stand-in telegram: the hourly tidy asks, the minute check tells, /ok, and the bridge moves."""
    DRIVER = test_alerts.DRIVER.replace('    if kind == "tick":', '    if kind == "tidy":\n        jinnee.memory_tidy(); return []\n    if kind == "tick":')

    def drive(self, steps, agent="record", extra=None):
        self.dotenv(test_alerts.TOKEN + "JINNEE_MEMORY_LESSONS_BYTES=2500\n")
        (self.root / "driver.py").write_text(self.DRIVER)
        r = self.run_py("driver.py", extra, json.dumps(steps), agent)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.split("RESULT ", 1)[1])

    def test_ask_tell_ok_move(self):
        before = TITLE + INTRO + "".join(old_rules(17, start=dt.date(2026, 1, 6)))
        (self.brain / "lessons.md").write_text(before, encoding="utf-8")
        got = self.drive([["tidy", 0, ""], ["tick", 0, ""]])
        self.assertEqual((self.brain / "lessons.md").read_text(encoding="utf-8"), before)
        item = next(i for i in self.book.read()[0] if memory_guard.ours(i))
        sent = got[1]["sent"][0][1]
        self.assertIn(f"[{item['id']}]", sent); self.assertIn("[pin]", sent); self.assertIn("JINNEE_MEMORY_LESSONS_BYTES", sent)
        got = self.drive([["msg", test_alerts.OWNER, f"/ok {item['id']}"], ["tick", 0, ""], ["tick", 0, ""]])
        self.assertEqual(got[1]["ran"], [])  # no agent run: the bridge carries it out itself
        self.assertIn("Done", got[1]["sent"][0][1])
        self.assertEqual(got[2], {"replies": [], "ran": [], "sent": []})
        after = (self.brain / "lessons.md").read_text(encoding="utf-8")
        self.assertTrue(after.startswith(TITLE + INTRO))
        self.assertLess(len(after), len(before))
        self.assertEqual(self.book.get(item["id"])["outcome"], "done")


if __name__ == "__main__":
    unittest.main()
