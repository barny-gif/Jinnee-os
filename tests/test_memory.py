"""Searchable memory (core/memory.py): the capped core in the prompt, search, and moving old lines to the archives.
Run: python -m unittest discover tests"""
import datetime as dt, json, os, pathlib, random, sys, tempfile, unittest
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

import test_env as base

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import memory, store, approvals, autonomy

TODAY = dt.date(2026, 11, 6)
WORDS = ("parcel courier invoice refund tone newsletter supplier price label return stock photo caption weekend "
         "discount formal informal greeting delivery").split()


def lessons(n, start=dt.date(2026, 1, 1), pin=("never use exclamation marks in customer mail [pin]",), seed=7):
    rnd = random.Random(seed)
    out = ["# lessons – the owner's corrections\n"] + [f"- {p}\n" for p in pin]
    for i in range(n):
        out.append(f"- {start + dt.timedelta(days=i // 2)} dropped \"Reply {i}\" (echo, send_customer_email): "
                   f"{' '.join(rnd.choices(WORDS, k=10))} [approval a{i}]\n")
    return "".join(out)


def nonblank(text):
    return Counter(l.rstrip("\n") for l in text.splitlines() if l.strip())


class Brain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brain = pathlib.Path(self.tmp.name) / "brain"
        self.brain.mkdir()
        env = mock.patch.dict(os.environ)
        env.start(); self.addCleanup(env.stop)
        for c in memory.CORES.values(): os.environ.pop(c["env"], None)

    def write(self, name, text):
        (self.brain / name).write_text(text, encoding="utf-8")

    def text(self, name):
        return (self.brain / name).read_text(encoding="utf-8")

    def cli(self, *argv):
        out = StringIO()
        with redirect_stdout(out): code = memory.main(list(argv), self.brain)
        return code, out.getvalue()


class Split(unittest.TestCase):
    def test_lossless_on_odd_files(self):
        samples = ["", "\n\n", "# only a title\n", "no newline at the end", "﻿- with a bom\n",
                   "# t\n\n- a\n  continued\n\n- b\n# mid heading\nplain prose\n\tindented\n- c",
                   "   starts indented\n- next\n", lessons(50)]
        for s in samples:
            with self.subTest(s=s[:30]):
                pre, entries = memory.split(s)
                self.assertEqual("".join(pre) + "".join("".join(e.raw) for e in entries), s)

    def test_continuation_lines_belong_to_their_entry(self):
        _, entries = memory.split("# t\n- 2026-10-01 first\n  more about it\n- second\n")
        self.assertEqual([e.line for e in entries], [2, 4])
        self.assertEqual(entries[0].text, "- 2026-10-01 first\n  more about it\n")
        self.assertEqual(entries[0].date, dt.date(2026, 10, 1))
        self.assertIsNone(entries[1].date)

    def test_the_intro_under_the_title_is_preamble_not_an_entry(self):
        text = ("# lessons\nThe owner's corrections. Everyone reads this.\n  (kept short)\n\n"
                "- 2026-01-05 first rule\n  with a second line\n- 2026-01-06 second rule\n")
        pre, entries = memory.split(text)
        self.assertEqual("".join(pre), "# lessons\nThe owner's corrections. Everyone reads this.\n  (kept short)\n\n")
        self.assertEqual([e.text for e in entries], ["- 2026-01-05 first rule\n  with a second line\n", "- 2026-01-06 second rule\n"])
        self.assertEqual("".join(pre) + "".join("".join(e.raw) for e in entries), text)

    def test_the_intro_ends_at_the_first_list_item_or_dated_line(self):
        for first in ("- a rule\n", "* a rule\n", "1. a rule\n", "2026-01-05 a dated rule\n"):
            with self.subTest(first=first):
                pre, entries = memory.split("# t\nintro\n## sub title\n" + first + "- next\n")
                self.assertEqual(pre, ["# t\n", "intro\n", "## sub title\n"])
                self.assertEqual(entries[0].text, first)

    def test_a_file_without_a_title_or_a_list_keeps_every_line_an_entry(self):
        for text in ("# catalog\nstripe: payments, see the runbook\nshop: the catalogue\n",  # one line per topic, no list
                     "an undated first line\n- then a list\n"):                                 # no title above it
            with self.subTest(text=text[:20]):
                pre, entries = memory.split(text)
                self.assertFalse([ln for ln in pre if ln.strip() and not ln.startswith("#")])
                self.assertEqual(len(entries), 2)


class Core(Brain):
    def test_small_file_goes_in_whole_without_a_notice(self):
        self.write("lessons.md", lessons(5))
        text, f = memory.core("lessons", self.brain)
        self.assertEqual(text, lessons(5))
        self.assertEqual(f["hidden"], 0)
        self.assertNotIn("MEMORY CAP", text)

    def test_cap_holds_in_bytes_and_lines(self):
        self.write("lessons.md", lessons(400))
        for cap_b in ("500", "2000", "8000", "50000"):
            with self.subTest(cap=cap_b):
                os.environ["JINNEE_MEMORY_LESSONS_BYTES"] = cap_b
                text, f = memory.core("lessons", self.brain)
                body = text.split("[MEMORY CAP REACHED")[0]
                self.assertLessEqual(len(body.encode("utf-8")), int(cap_b))
                self.assertLessEqual(body.count("\n"), memory.CORES["lessons"]["lines"])
                self.assertLessEqual(f["bytes"], f["cap_bytes"])
                notice = text[len(body):]
                self.assertLess(len(notice.encode("utf-8")), 400)
                self.assertIn("core/memory.py search", notice)
                self.assertIn(f"{f['hidden']} older entries", notice)

    def test_pin_first_then_the_newest_in_file_order(self):
        self.write("lessons.md", lessons(300))
        text, f = memory.core("lessons", self.brain)
        lines = text.splitlines()
        self.assertEqual(lines[0], "# lessons – the owner's corrections")
        self.assertEqual(lines[1], "- never use exclamation marks in customer mail [pin]")
        self.assertIn("[approval a299]", lines[-2])  # the newest is in, last before the notice
        shown = [int(l.split("[approval a")[1].rstrip("]")) for l in lines if "[approval a" in l]
        self.assertEqual(shown, list(range(300 - len(shown), 300)))  # contiguous, newest, in order
        self.assertEqual(f["shown"], len(shown) + 1)

    def test_pins_over_the_cap_are_cut_and_said(self):
        os.environ["JINNEE_MEMORY_LESSONS_BYTES"] = "500"
        self.write("lessons.md", lessons(3, pin=[f"rule {i} " + "x" * 80 + " [pin]" for i in range(10)]))
        text, f = memory.core("lessons", self.brain)
        self.assertGreater(f["pin_cut"], 0)
        self.assertIn("[pin] lines alone are over the cap", text)
        self.assertLessEqual(len(text.split("[MEMORY CAP")[0].encode("utf-8")), 500)

    def test_bad_env_values_fall_back(self):
        for raw in ("", "abc", "-5", "1e9"):
            os.environ["JINNEE_MEMORY_NOTES_BYTES"] = raw
            self.assertEqual(memory.cap("notes"), (4000, 40))
        os.environ["JINNEE_MEMORY_NOTES_BYTES"] = "10"
        self.assertEqual(memory.cap("notes")[0], memory.MIN_BYTES)

    def test_missing_files_and_missing_brain(self):
        self.assertEqual(memory.prompt_block(self.brain), "")
        self.assertEqual(memory.prompt_block(self.brain / "nowhere"), "")
        self.assertEqual(memory.core("notes", self.brain / "nowhere")[1]["shown"], 0)
        self.write("company.md", "# company\nWe sell candles.\n")
        block = memory.prompt_block(self.brain)
        self.assertIn("===== brain/company.md =====\n# company\nWe sell candles.", block)
        self.assertNotIn("lessons", block)

    def test_company_whole_and_the_cores_labelled(self):
        big_company = "# company\n" + "Fact line about the business.\n" * 600
        self.write("company.md", big_company)
        self.write("lessons.md", lessons(400))
        self.write("forge_notes.md", "# notes\n" + "".join(f"- 2026-05-{1 + i % 28:02d} connector note {i}\n" for i in range(200)))
        block = memory.prompt_block(self.brain)
        self.assertIn(big_company, block)  # company.md is never cut
        self.assertRegex(block, r"===== brain/lessons\.md \(newest \d+ entries of 401\) =====")
        self.assertRegex(block, r"===== brain/forge_notes\.md \(newest \d+ entries of 200\) =====")
        self.assertIn("connector note 199", block)
        self.assertNotIn("connector note 0\n", block)


class Search(Brain):
    def setUp(self):
        super().setUp()
        self.write("lessons.md", "# lessons\n- 2026-10-01 dropped \"Reply\": we never offer a discount on the first order\n"
                                 "- 2026-10-02 dropped \"Post\": the photo was too dark\n")
        self.write("lessons.archive.md", "# archive\n- 2026-03-01 parcels to the islands go with the second courier only\n")
        self.write("forge_notes.md", "# notes\n- 2026-09-01 shop connector: the token expires after 90 days\n"
                                     "  renew it in the admin, then restart\n")
        self.write("decisions.log.md", "# decisions\n- 2026-10-03 telegram: a1 → ok\n- 2026-10-04 telegram: a2 → drop discount too high\n")
        self.write("catalog.md", "# catalog\n- szállítás és futár: lásd lessons archívum\n")

    def ids(self, query, **kw):
        return [d.id for d, _ in memory.search(query, self.brain, **kw)]

    def test_finds_the_right_entry_first(self):
        self.assertEqual(self.ids("discount first order")[0], "lessons:2")
        self.assertEqual(self.ids("token expires")[0], "notes:2")
        self.assertEqual(self.ids("courier islands")[0], "lessons-archive:2")

    def test_continuation_lines_are_searched_and_shown(self):
        self.assertEqual(self.ids("renew admin")[0], "notes:2")
        self.assertEqual(memory.get("notes:3", self.brain)[1], "- 2026-09-01 shop connector: the token expires after 90 days\n"
                                                               "  renew it in the admin, then restart\n")

    def test_accents_and_longer_forms(self):
        self.assertEqual(self.ids("szallitas")[0], "catalog:2")
        self.assertEqual(self.ids("FUTÁR")[0], "catalog:2")
        self.assertIn("lessons-archive:2", self.ids("parcel"))  # parcel ~ parcels

    def test_groups(self):
        self.assertEqual(self.ids("discount", groups=("decisions",)), ["decisions:3"])
        self.assertEqual(set(self.ids("discount")), {"lessons:2", "decisions:3"})
        with self.assertRaises(SystemExit): memory._groups("lessons,nonsense")

    def test_nothing_found_and_empty_query(self):
        self.assertEqual(self.ids("zzzz"), [])
        self.assertEqual(self.ids("!!"), [])
        code, out = self.cli("search", "zzzz")
        self.assertEqual(code, 1)
        self.assertIn("nothing found", out)

    def test_cli_commands(self):
        code, out = self.cli("search", "token", "expires")
        self.assertEqual(code, 0); self.assertTrue(out.startswith("notes:2"))
        code, out = self.cli("get", "lessons:3")
        self.assertEqual((code, out.splitlines()[0]), (0, "brain/lessons.md:3"))
        self.assertEqual(self.cli("get", "lessons:99")[0], 1)
        self.assertEqual(self.cli("get", "approvals:1")[0], 1)  # only the memory files can be opened
        code, out = self.cli("log-tail", "1")
        self.assertEqual(out.strip(), "decisions:3          - 2026-10-04 telegram: a2 → drop discount too high")
        self.assertEqual(len(self.cli("log-tail")[1].splitlines()), 2)
        code, out = self.cli("log-search", "discount")
        self.assertEqual(out.split()[0], "decisions:3")
        code, out = self.cli("stats")
        self.assertIn("core lessons", out)
        self.assertEqual(self.cli("core", "lessons")[1], self.text("lessons.md"))

    def test_nothing_there_does_not_crash(self):
        for name in ("lessons.md", "lessons.archive.md", "forge_notes.md", "decisions.log.md", "catalog.md"):
            (self.brain / name).unlink()
        self.assertEqual(self.ids("discount"), [])
        self.assertEqual(self.cli("log-tail", "5"), (0, "brain/decisions.log.md is empty or missing\n"))
        self.assertEqual(self.cli("stats")[0], 0)
        self.assertEqual(self.cli("compact", "--apply", "yes")[0], 0)
        (self.brain / "lessons.md").write_bytes(b"\xff\xfe broken \x80")  # unreadable = empty, not a crash
        self.assertEqual(self.ids("broken"), [])


class Compact(Brain):
    GATES = {"approvals.json": "[]\n", "autonomy_config.json": "{\"x\": 1}\n", "alerts_state.json": "{}\n"}

    def setUp(self):
        super().setUp()
        for name, body in self.GATES.items(): self.write(name, body)
        self.before = lessons(400, start=dt.date(2026, 4, 1))  # all older than KEEP_DAYS before TODAY
        self.write("lessons.md", self.before)

    def test_dry_run_writes_nothing(self):
        rep = memory.compact(self.brain, apply=False, today=TODAY)
        self.assertTrue(rep[0]["moved"])
        self.assertEqual(self.text("lessons.md"), self.before)
        self.assertFalse((self.brain / "lessons.archive.md").exists())
        self.assertFalse((self.brain / "backups").exists())

    def test_no_line_is_lost_and_the_file_fits_again(self):
        memory.compact(self.brain, apply=True, today=TODAY)
        after, archive = self.text("lessons.md"), self.text("lessons.archive.md")
        everything = nonblank(after) + nonblank(archive)
        for line, n in nonblank(self.before).items():
            self.assertGreaterEqual(everything[line], n, line)
        extra = everything - nonblank(self.before)  # only the archive's own title and one "moved" heading
        self.assertEqual(sum(extra.values()), 2)
        self.assertTrue(all(l.startswith("#") for l in extra))
        b, l = memory._size(after)
        self.assertLessEqual(b, int(memory.cap("lessons")[0] * memory.TARGET))
        self.assertLessEqual(l, memory.cap("lessons")[1])
        _, f = memory.core("lessons", self.brain)
        self.assertEqual(f["hidden"], 0)  # after tidying, the whole file is the core again
        self.assertEqual(memory.compact(self.brain, apply=True, today=TODAY)[0]["moved"], [])  # nothing twice

    def test_pins_headings_and_fresh_lines_stay(self):
        text = "# lessons\n- old rule kept forever [pin]\n## a heading in the middle\n" + lessons(300, start=TODAY - dt.timedelta(days=200)).split("\n", 2)[2]
        text += "".join(f"- {TODAY - dt.timedelta(days=3)} fresh drop {i} " + "y" * 120 + f" [approval f{i}]\n" for i in range(70))
        self.write("lessons.md", text)
        memory.compact(self.brain, apply=True, today=TODAY)
        after = self.text("lessons.md")
        self.assertIn("- old rule kept forever [pin]", after)
        self.assertIn("## a heading in the middle", after)
        for i in range(70): self.assertIn(f"[approval f{i}]", after)  # fresh: an /undo must still find it
        self.assertNotIn("fresh drop", self.text("lessons.archive.md"))
        # still over the cap because of the fresh lines: the prompt holds the cap anyway and says so
        text, f = memory.core("lessons", self.brain)
        self.assertLessEqual(f["bytes"], f["cap_bytes"])
        self.assertIn("MEMORY CAP REACHED", text)

    def test_gates_are_never_touched(self):
        stamps = {n: (self.brain / n).stat().st_mtime_ns for n in self.GATES}
        memory.compact(self.brain, apply=True, today=TODAY)
        for name, body in self.GATES.items():
            self.assertEqual(self.text(name), body)
            self.assertEqual((self.brain / name).stat().st_mtime_ns, stamps[name])
        written = {p.name for p in self.brain.iterdir() if p.is_file()} - set(self.GATES)
        self.assertEqual(written, {"lessons.md", "lessons.archive.md", ".lock"})

    def test_backup_is_kept_and_limited(self):
        memory.compact(self.brain, apply=True, today=TODAY)
        copies = list((self.brain / "backups" / "memory").glob("lessons.md.*.bak"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8"), self.before)
        for i in range(memory.BACKUPS_KEPT + 3):
            self.write("lessons.md", lessons(400, start=dt.date(2026, 4, 1), seed=i))
            memory.compact(self.brain, apply=True, today=TODAY)
        self.assertEqual(len(list((self.brain / "backups" / "memory").glob("lessons.md.*.bak"))), memory.BACKUPS_KEPT)

    def test_a_failed_check_puts_the_file_back(self):
        real = store.write_text
        def bad(path, text):
            real(path, text + ("- garbage\n" if pathlib.Path(path).name == "lessons.md" else ""))
        with mock.patch.object(store, "write_text", bad):
            with self.assertRaises(RuntimeError): memory.compact(self.brain, apply=True, today=TODAY)
        self.assertEqual(self.text("lessons.md"), self.before)

    def test_an_existing_archive_is_kept(self):
        self.write("lessons.archive.md", "# archive\n- 2025-01-01 something from last year\n")
        memory.compact(self.brain, apply=True, today=TODAY)
        archive = self.text("lessons.archive.md")
        self.assertTrue(archive.startswith("# archive\n- 2025-01-01 something from last year\n"))
        self.assertIn(f"## moved {TODAY} from lessons.md", archive)
        self.assertEqual(memory.search("something from last year", self.brain)[0][0].id, "lessons-archive:2")

    def test_notes_too_and_cli(self):
        self.write("forge_notes.md", "# notes\n" + "".join(f"- 2026-0{1 + i % 6}-01 connector note {i} " + "z" * 60 + "\n" for i in range(200)))
        code, out = self.cli("compact")
        self.assertIn("would move", out); self.assertIn("dry run", out)
        code, out = self.cli("compact", "--apply", "yes")
        self.assertIn("brain/forge_notes.md: moved", out)
        self.assertTrue((self.brain / "forge_notes.archive.md").exists())
        self.assertEqual(memory.search("note 0", self.brain, groups=("notes",))[0][0].group, "notes")

    def test_undo_still_finds_a_fresh_drop_after_tidying(self):
        book = approvals.Book(self.brain)
        book.gate = autonomy.Config(self.brain, ["general", "ecom"])
        (self.brain / "approvals.json").unlink()
        item = book.add(agent="office", action="send_customer_email", title="Reply to Anna", text="Dear Anna, done.")[0]
        book.decide(item["id"], "drop", "we never chase invoices under the minimum")
        self.assertIn(f"[approval {item['id']}]", self.text("lessons.md"))
        memory.compact(self.brain, apply=True)  # the real date: the drop is from today
        self.assertIn(f"[approval {item['id']}]", self.text("lessons.md"))
        book.undo(item["id"])
        self.assertNotIn(f"[approval {item['id']}]", self.text("lessons.md"))


class Bridge(base.RepoCopy):
    """jinnee.py itself: the prompt it builds and the tools it allows."""
    DRIVER = """\
import json, sys
sys.path.insert(0, "core")
import jinnee
print("RESULT " + json.dumps({"tools": jinnee.TOOLS, "scheduled": jinnee.SCHEDULED_TOOLS, "guarded": jinnee.GUARDED,
                              "scheduled_deny": jinnee.SCHEDULED_DENY, "prompt": jinnee.system_prompt()}))
"""

    def test_prompt_and_tools(self):
        brain = self.root / "brain"
        brain.mkdir()
        (brain / "company.md").write_text("# company\nWe sell candles.\n", encoding="utf-8")
        (brain / "lessons.md").write_text(lessons(400), encoding="utf-8")
        self.dotenv("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_OWNER_ID=1\n")
        (self.root / "driver.py").write_text(self.DRIVER)
        r = self.run_py("driver.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.split("RESULT ", 1)[1])
        prompt = got["prompt"]
        self.assertIn("We sell candles.", prompt)
        self.assertIn("never use exclamation marks in customer mail [pin]", prompt)
        self.assertIn("[approval a399]", prompt)
        self.assertNotIn("[approval a0]", prompt)
        self.assertIn("MEMORY CAP REACHED", prompt)
        self.assertIn('core/memory.py search "<topic>"', prompt)  # the rule: search before work
        self.assertLess(len(prompt.encode("utf-8")), len(lessons(400).encode("utf-8")))
        for py in ("python3", "python"):
            for cmd in ("search *", "get *", "log-tail *", "log-search *", "stats"):
                self.assertIn(f"Bash({py} core/memory.py {cmd})", got["tools"])
                self.assertIn(f"Bash({py} core/memory.py {cmd})", got["scheduled"])
            self.assertIn(f"Bash({py} core/memory.py compact *)", got["guarded"])
            self.assertIn(f"Bash({py} core/memory.py compact *)", got["scheduled_deny"])
        self.assertFalse([t for t in got["tools"] + got["scheduled"] if "compact" in t or t.endswith("memory.py *)")])


if __name__ == "__main__":
    unittest.main()
