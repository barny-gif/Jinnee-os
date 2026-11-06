"""Interrupted work is kept (core/workfile.py, core/scheduler.py, core/jinnee.py): the work-file mark, what the owner is told
after a run stopped (with and without a work file), /continue, the scheduled job that said it was done but left nothing
(problem), and the quiet hours. No network and no real claude: the scheduler's runner is a fake, the bridge runs a stand-in
`claude` script on PATH. Run: python3 -m unittest discover tests"""
import json, os, pathlib, sys, tempfile, time, unittest
from unittest import mock

import test_env as base
from test_scheduler import Base, Fake, at
from test_alerts import Bridge, OWNER

REPO = pathlib.Path(__file__).resolve().parent.parent
os.environ["JINNEE_LANG"] = "en"  # the owner's texts are checked in English here, whatever this machine is set to (test_texts checks hu)
sys.path.insert(0, str(REPO / "core"))
import alerts, heartbeat, scheduler, workfile

WORK = """<!-- work: in-progress -->
# Competitor round
Status: in progress
- [x] prices at the first shop
- [x] call back +36 30 123 4567 about anna.kovacs@example.com
- [ ] promotions at the second shop
Prices: the first shop raised two items.
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class Mark(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_status_reads_the_mark_in_any_language_and_only_at_the_top(self):
        h = self.root / "handoffs" / "x"
        self.assertEqual(workfile.status(write(h / "a.md", WORK)), "in-progress")
        self.assertEqual(workfile.status(write(h / "b.md", "﻿<!--work:DONE-->\n# Kész\nÁllapot: kész\n")), "done")
        self.assertEqual(workfile.status(write(h / "c.md", "# Title\n\nStatus: folyamatban\n<!-- work: in-progress -->\n")), "in-progress")
        self.assertEqual(workfile.status(write(h / "d.md", "# a\nb\nc\nd\n<!-- work: in-progress -->\n")), "")  # too far down
        self.assertEqual(workfile.status(write(h / "e.md", "# A plain handoff\n")), "")
        self.assertEqual(workfile.status(h / "missing.md"), "")

    def test_touched_finds_this_runs_files_and_never_the_owners_inbox(self):
        old = write(self.root / "handoffs" / "ledger" / "old.md", WORK)
        os.utime(old, (time.time() - 600, time.time() - 600))
        start = time.time()
        write(self.root / "handoffs" / "scout" / "2026-11-06-round.md", WORK)
        write(self.root / "handoffs" / "scout" / "2026-11-06-done.md", "<!-- work: done -->\n# Done\n")
        write(self.root / "handoffs" / "scout" / "plain.md", "# no mark\n")
        write(self.root / "handoffs" / "inbox" / "owner.md", WORK)
        self.assertEqual(workfile.touched(self.root, start), {"open": ["handoffs/scout/2026-11-06-round.md"],
                                                              "done": ["handoffs/scout/2026-11-06-done.md"]})
        self.assertEqual(workfile.touched(self.root / "nowhere", start), {"open": [], "done": []})

    def test_the_summary_shows_steps_not_results_and_masks_personal_data(self):
        write(self.root / "handoffs" / "scout" / "r.md", WORK)
        text = workfile.summary(self.root, ["handoffs/scout/r.md"])
        self.assertIn("handoffs/scout/r.md: 2 of 3 steps done", text)
        self.assertIn("Next: promotions at the second shop", text)
        self.assertNotIn("anna.kovacs", text); self.assertNotIn("123 4567", text)
        self.assertIn("[e-mail]", text); self.assertIn("[number]", text)
        self.assertNotIn("raised two items", text)  # a result is never shown
        write(self.root / "handoffs" / "scout" / "s.md", "<!-- work: in-progress -->\n# Notes\n")
        self.assertIn("started, no step list in it", workfile.summary(self.root, ["handoffs/scout/s.md"]))
        long = "<!-- work: in-progress -->\n" + "".join(f"- [x] step {i} " + "x" * 100 + "\n" for i in range(9))
        write(self.root / "handoffs" / "scout" / "l.md", long)
        text = workfile.summary(self.root, ["handoffs/scout/l.md"])
        self.assertIn("9 of 9 steps done (the last 5)", text); self.assertNotIn("step 3 ", text)
        self.assertTrue(all(len(n) <= workfile.STEP_CHARS for n in text.split(": ", 2)[2].split("; ")))

    def test_still_open_and_content(self):
        a = write(self.root / "handoffs" / "a.md", WORK)
        write(self.root / "handoffs" / "b.md", "<!-- work: done -->\n# B\nAll there.\n")
        self.assertEqual(workfile.still_open(self.root, ["handoffs/a.md", "handoffs/b.md", "handoffs/gone.md"]), ["handoffs/a.md"])
        self.assertTrue(workfile.has_content(a))
        self.assertFalse(workfile.has_content(write(self.root / "handoffs" / "c.md", "<!-- work: done -->\n# Title only\n\n")))

    def test_the_continue_prompt(self):
        p = workfile.resume_prompt(["handoffs/scout/r.md"], "Look at the three competitors", 10, 2)
        for part in ("CONTINUE", "stopped after 10 minutes", "continuation 2", "handoffs/scout/r.md", "do not redo it",
                     "never send, queue or consume anything a second time", "<!-- work: done -->", "Look at the three competitors"):
            self.assertIn(part, p)


class Scheduled(Base):
    """watcher.weekly_round (Thursday 10:00, promises handoffs/scout/*weekly-round*.md) and the brief, in a temporary install."""
    def setUp(self):
        super().setUp()
        write(self.brain / "watch" / "competitors.md", "First shop\nSecond shop\n")
        self.s = scheduler.Scheduler(self.brain, ["general"], root=self.root)
        self.s.due(at(2026, 10, 22, 9))  # first seen Thursday 09:00

    def round(self, now, runner):
        due = [(j, t, k) for j, t, k in self.s.due(now) if j.id == "watcher.weekly_round"]
        self.assertEqual(len(due), 1)
        return self.s.run(*due[0], runner, now=now)

    def leaves(self, text, name="2026-10-22-weekly-round.md", reply="Two price changes."):
        def run(job, prompt):
            write(self.root / "handoffs" / "scout" / name, text)
            return True, reply, "replied"
        return run

    def stops(self, text=WORK, name="2026-10-22-weekly-round.md"):
        def run(job, prompt):
            if text: write(self.root / "handoffs" / "scout" / name, text)
            return False, "I worked on this for 10 minutes without finishing, so I stopped.", "timed out after 10 minutes"
        return run

    # --- do not believe the run
    def test_a_run_that_left_its_file_is_ok(self):
        out = self.round(at(2026, 10, 22, 10), Fake(self.leaves("<!-- work: done -->\n# Round\n- [x] First shop: two prices up\n")))
        self.assertEqual(out.result, "ok"); self.assertEqual(heartbeat.load(self.brain)["watcher"]["state"], "ok")

    def test_said_done_but_no_file_is_a_problem(self):
        out = self.round(at(2026, 10, 22, 10), Fake((True, "Done, all fine.", "replied")))
        self.assertEqual(out.result, "problem")
        self.assertIn("handoffs/scout/*weekly-round*.md was not written in this run", out.note)
        self.assertIn("Done, all fine.", out.message)  # what it did say still reaches the owner
        self.assertEqual(self.rec("watcher.weekly_round")["result"], "problem")
        self.assertNotIn("ok_start", self.rec("watcher.weekly_round"))  # the next run looks back as far as after a failure
        self.assertEqual(heartbeat.load(self.brain)["watcher"]["state"], "problem")
        self.assertEqual(heartbeat.trouble(self.brain, at(2026, 10, 22, 10, 1)),
                         ["watcher: last run left no usable result (scheduled watcher.weekly_round: the run said it was done, but "
                          "handoffs/scout/*weekly-round*.md was not written in this run)"])
        probs = self.s.problems()
        self.assertEqual(len(probs), 1); self.assertIn("did not leave what it should", probs[0])
        self.assertIn("problem", self.s.summary(at(2026, 10, 22, 10, 1)))

    def test_an_old_file_does_not_count(self):
        old = write(self.root / "handoffs" / "scout" / "2026-10-15-weekly-round.md", "<!-- work: done -->\n# Last week\nAll the facts.\n")
        os.utime(old, (time.time() - 7 * 86400, time.time() - 7 * 86400))
        self.assertEqual(self.round(at(2026, 10, 22, 10), Fake((True, "Same as last week.", "replied"))).result, "problem")

    def test_an_empty_file_or_one_left_in_progress_is_a_problem(self):
        out = self.round(at(2026, 10, 22, 10), Fake(self.leaves("<!-- work: done -->\n# Round\n\n")))
        self.assertEqual(out.result, "problem"); self.assertIn("is empty", out.note)
        out = self.round(at(2026, 10, 29, 10), Fake(self.leaves(WORK, name="2026-10-29-weekly-round.md")))
        self.assertEqual(out.result, "problem"); self.assertIn("still marked in progress", out.note)

    def test_an_empty_reply_is_a_problem_even_without_a_promised_file(self):
        out = self.s.run(self.job("jinnee.evening"), at(2026, 10, 22, 20), "2026-10-22 20:00", Fake((True, "…", "replied")),
                         now=at(2026, 10, 22, 20))
        self.assertEqual((out.result, out.message), ("problem", "")); self.assertIn("without a word", out.note)

    def test_silent_excuses_the_file_only_when_the_job_says_so(self):
        out = self.round(at(2026, 10, 22, 10), Fake((True, "SILENT", "replied")))
        self.assertEqual(out.result, "problem")  # the weekly round always writes its summary
        job = self.job("watcher.weekly_round", self.s)._replace(silent_ok=True)
        out = self.s.run(job, at(2026, 10, 29, 10), "2026-10-29 10:00", Fake((True, "SILENT", "replied")), now=at(2026, 10, 29, 10))
        self.assertEqual((out.result, out.message), ("silent", ""))
        out = self.s.run(self.job("jinnee.evening"), at(2026, 10, 22, 20), "2026-10-22 20:00", Fake((True, "SILENT", "replied")),
                         now=at(2026, 10, 22, 20))
        self.assertEqual(out.result, "silent")  # nothing promised: SILENT is a full answer

    def test_the_brief_file_is_checked_in_brain(self):
        out = self.s.run(self.job("jinnee.brief", self.s), at(2026, 10, 23, 8), "2026-10-23 08:00", Fake((True, "1. Quiet.", "r")),
                         now=at(2026, 10, 23, 8))
        self.assertEqual(out.result, "ok")  # Fake leaves the brief's file, as a real run does
        Fake.brain = None
        for f in self.brain.glob("brief_today.md"): os.utime(f, (time.time() - 86400, time.time() - 86400))  # yesterday's
        out = self.s.run(self.job("jinnee.brief", self.s), at(2026, 10, 24, 8), "2026-10-24 08:00", Fake((True, "1. Quiet.", "r")),
                         now=at(2026, 10, 24, 8))
        self.assertEqual((out.result, out.message), ("problem", "1. Quiet."))
        self.assertTrue(any("morning brief" in p for p in self.s.problems()))  # told although a lead run

    def test_the_problem_is_told_once_and_never_in_the_quiet_hours(self):
        self.round(at(2026, 10, 22, 10), Fake((True, "Done.", "replied")))
        jobs = self.s.jobs()
        watch = alerts.Watch(self.brain)
        with mock.patch.object(scheduler.Scheduler, "jobs", lambda self_: jobs), mock.patch.dict(os.environ, PACKS="general"):
            quiet = watch.due(at(2026, 10, 22, 22)); watch.done(quiet)
            self.assertEqual(quiet["messages"], [])
            morning = watch.due(at(2026, 10, 23, 7, 1)); watch.done(morning)
            self.assertEqual(sum("did not leave what it should" in m for m in morning["messages"]), 1)
            again = watch.due(at(2026, 10, 23, 7, 2)); watch.done(again)
            self.assertFalse(any("did not leave" in m for m in again["messages"]))

    # --- a run that stopped
    def test_a_stopped_run_keeps_its_work_and_the_next_run_continues_it(self):
        out = self.round(at(2026, 10, 22, 10), Fake(self.stops()))
        self.assertEqual((out.result, out.message), ("failed", ""))  # an agent job: told through the alerts, not straight
        rec = self.rec("watcher.weekly_round")
        self.assertEqual(rec["work"], ["handoffs/scout/2026-10-22-weekly-round.md"])
        self.assertIn("work so far is kept in handoffs/scout/2026-10-22-weekly-round.md", rec["note"])
        probs = self.s.problems()
        self.assertEqual(len(probs), 1)
        for part in ("stopped before it finished", "2 of 3 steps done", "/continue watcher.weekly_round", "The next run continues it"):
            self.assertIn(part, probs[0])
        self.assertNotIn("anna.kovacs", probs[0])
        self.assertEqual([j.id for j, _, _ in self.s.interrupted()], ["watcher.weekly_round"])
        seen = []
        def finish(job, prompt):
            seen.append(prompt)
            write(self.root / "handoffs" / "scout" / "2026-10-22-weekly-round.md", WORK.replace("in-progress", "done").replace("[ ]", "[x]"))
            write(self.root / "handoffs" / "scout" / "2026-10-29-weekly-round.md", "<!-- work: done -->\n# Round\n- [x] all five lines here\n")
            return True, "Round done.", "replied"
        out = self.round(at(2026, 10, 29, 10), Fake(finish))
        self.assertEqual(out.result, "ok")
        self.assertIn("An earlier run of this job stopped before it finished", seen[0])
        self.assertIn("handoffs/scout/2026-10-22-weekly-round.md", seen[0])
        self.assertNotIn("work", self.rec("watcher.weekly_round")); self.assertEqual(self.s.problems(), [])

    def test_a_stopped_run_without_a_work_file_is_a_plain_failure(self):
        out = self.round(at(2026, 10, 22, 10), Fake(self.stops(text="")))
        self.assertEqual(out.result, "failed")
        self.assertNotIn("work", self.rec("watcher.weekly_round"))
        self.assertEqual(self.s.interrupted(), [])
        probs = self.s.problems()
        self.assertEqual(len(probs), 1); self.assertIn("failed: timed out", probs[0]); self.assertNotIn("/continue", probs[0])

    def test_a_stopped_lead_run_tells_the_owner_directly_with_continue(self):
        def stop(job, prompt):
            write(self.root / "handoffs" / "jinnee" / "2026-10-23-brief.md", WORK)
            return False, "I worked on this for 10 minutes without finishing, so I stopped.", "timed out after 10 minutes"
        out = self.s.run(self.job("jinnee.brief", self.s), at(2026, 10, 23, 8), "2026-10-23 08:00", Fake(stop), now=at(2026, 10, 23, 8))
        self.assertEqual(out.result, "failed")
        for part in ("The work so far is kept", "handoffs/jinnee/2026-10-23-brief.md: 2 of 3 steps done", "/continue jinnee.brief"):
            self.assertIn(part, out.message)
        self.assertFalse(any("morning brief" in p for p in self.s.problems()))  # not a second time through the alerts

    def test_continue_now_runs_unattended_through_the_runner_and_counts(self):
        self.round(at(2026, 10, 22, 10), Fake(self.stops()))
        again = self.s.resume("watcher.weekly_round", Fake(self.stops()), now=at(2026, 10, 22, 11))
        self.assertEqual(again.result, "failed"); self.assertIn("stopped again", again.message)
        self.assertEqual(self.rec("watcher.weekly_round")["attempt"], 1)
        prompts = []
        def finish(job, prompt):
            prompts.append(prompt)
            write(self.root / "handoffs" / "scout" / "2026-10-22-weekly-round.md",
                  "<!-- work: done -->\n# Round\n- [x] prices\n- [x] promotions\nAll five lines are here now.\n")
            return True, "Round done.", "replied"
        out = self.s.resume("watcher.weekly_round", Fake(finish), now=at(2026, 10, 22, 11, 30))
        self.assertEqual(out.result, "ok"); self.assertIn("Round done.", out.message)
        self.assertIn("continuation 2", prompts[0]); self.assertIn("Nothing goes out from it", prompts[0])
        self.assertEqual(self.rec("watcher.weekly_round")["slot_t"], at(2026, 10, 22, 10))  # no slot taken
        self.assertNotIn("work", self.rec("watcher.weekly_round"))
        nothing = self.s.resume("watcher.weekly_round", Fake())
        self.assertEqual(nothing.result, "skipped"); self.assertIn("no unfinished work", nothing.message)
        self.assertEqual(self.s.resume("nope", Fake()).result, "skipped")

    def test_continue_waits_while_another_run_holds_the_line(self):
        self.round(at(2026, 10, 22, 10), Fake(self.stops()))
        runner = Fake(busy=True)
        out = self.s.resume("watcher.weekly_round", runner)
        self.assertEqual(out.result, "busy"); self.assertEqual(runner.calls, [])

    def test_a_job_with_work_carried_runs_even_when_its_precheck_finds_nothing_new(self):
        os.environ.update(GMAIL_CLIENT_ID="cid", GMAIL_CLIENT_SECRET="sec", GMAIL_ACCOUNTS="info")
        with mock.patch.dict(scheduler.PRECHECKS, new_mail=lambda since, root: (False, "no new mail")):
            s = scheduler.Scheduler(self.brain, ["general"], root=self.root)
            s.due(at(2026, 10, 23, 10))
            job = self.job("office.inbox", s)
            write(self.root / "handoffs" / "office" / "triage.md", WORK)
            s._carry(job, ["handoffs/office/triage.md"])
            runner = Fake((True, "Finished the triage.", "replied"))
            out = s.run(job, at(2026, 10, 23, 10, 5), "slot", runner, now=at(2026, 10, 23, 10, 5))
            self.assertEqual(len(runner.calls), 1); self.assertIn("handoffs/office/triage.md", runner.calls[0][1])
            self.assertEqual(out.result, "ok")
            s._carry(job, [])
            out = s.run(job, at(2026, 10, 23, 11, 5), "slot2", runner, now=at(2026, 10, 23, 11, 5))
            self.assertEqual(out.result, "nothing")


STOP = """printf "%s\\n" "$@" > args.txt
mkdir -p handoffs/jinnee
printf '%s\\n' '<!-- work: in-progress -->' '# Supplier comparison' 'Status: in progress' \\
  '- [x] prices from the first supplier' '- [x] delivery times for anna@example.com' '- [ ] the summary' > handoffs/jinnee/2026-11-06-suppliers.md
sleep 30
"""
FINISH = """printf "%s\\n" "$@" > args.txt
F=$(ls handoffs/jinnee/*.md handoffs/scout/*.md 2>/dev/null | head -1)
[ -n "$F" ] && printf '%s\\n' '<!-- work: done -->' '# Done' '- [x] all of it' 'The full comparison is here.' > "$F"
echo "All done: the comparison is in the file."
"""


class BridgeKeepsWork(Bridge):
    """core/jinnee.py with a stand-in claude: a run that writes a work file and is stopped by the time limit, /continue."""
    def read(self, name):
        return (self.root / name).read_text(encoding="utf-8")

    def test_a_timed_out_run_with_a_work_file_says_how_far_it_got_and_continue_goes_on(self):
        got = self.drive([["msg", OWNER, "Compare our two suppliers"]], agent="real", extra=self.fake_claude(STOP))
        reply = got[0]["replies"][-1]
        for part in ("stopped before finishing", "The work so far is kept, not lost", "handoffs/jinnee/2026-11-06-suppliers.md",
                     "2 of 3 steps done", "Next: the summary", "/continue"):
            self.assertIn(part, reply)
        self.assertNotIn("anna@example.com", reply)
        rec = json.loads((self.brain / "interrupted.json").read_text())
        self.assertEqual((rec["task"], rec["files"], rec["attempt"]),
                         ("Compare our two suppliers", ["handoffs/jinnee/2026-11-06-suppliers.md"], 1))
        beat = json.loads((self.brain / "heartbeat.json").read_text())["jinnee"]
        self.assertEqual(beat["state"], "failed"); self.assertIn("work kept in handoffs/jinnee/2026-11-06-suppliers.md", beat["note"])

        got = self.drive([["msg", OWNER, "/continue"]], agent="real", extra=self.fake_claude(FINISH))
        self.assertIn("Going on from handoffs/jinnee/2026-11-06-suppliers.md", got[0]["replies"][0])
        self.assertEqual(got[0]["replies"][-1], "All done: the comparison is in the file.")
        args = self.read("args.txt")
        for part in ("CONTINUE.", "handoffs/jinnee/2026-11-06-suppliers.md", "The original task:\nCompare our two suppliers",
                     "--permission-mode\nacceptEdits\n", "WORK IN PROGRESS"):
            self.assertIn(part, args)
        self.assertNotIn("Edit(brain/company.md)", args)  # the owner's own run may still edit it, as before
        self.assertFalse((self.brain / "interrupted.json").exists())  # finished: nothing left to continue
        got = self.drive([["msg", OWNER, "/continue"]], agent="real", extra=self.fake_claude(FINISH))
        self.assertEqual(got[0]["replies"], ["Nothing was cut off or left unfinished, so there is nothing to continue."])

    def test_a_bare_continue_word_continues_only_when_something_is_cut_off(self):
        got = self.drive([["msg", OWNER, "folytasd"]])
        self.assertEqual(got[0]["ran"], ["folytasd"])  # nothing cut off: an ordinary message to the lead
        write(self.root / "handoffs" / "jinnee" / "x.md", WORK)
        (self.brain / "interrupted.json").write_text(json.dumps({"task": "Do the round", "files": ["handoffs/jinnee/x.md"],
                                                                 "at": int(time.time()), "attempt": 1}))
        got = self.drive([["msg", OWNER, "Continue."]], agent="real", extra=self.fake_claude(FINISH))
        self.assertIn("Going on from handoffs/jinnee/x.md", got[0]["replies"][0])
        args = self.read("args.txt")
        self.assertIn("CONTINUE.", args); self.assertIn("Do the round", args)

    def test_a_timed_out_run_without_a_work_file_keeps_the_old_answer(self):
        got = self.drive([["msg", OWNER, "do everything"]], agent="real", extra=self.fake_claude("sleep 30\n"))
        self.assertIn("without finishing, so I stopped", got[0]["replies"][0])
        self.assertNotIn("/continue", got[0]["replies"][0])
        self.assertFalse((self.brain / "interrupted.json").exists())

    def test_continue_a_scheduled_job_runs_it_unattended(self):
        write(self.brain / "watch" / "competitors.md", "First shop\n")
        write(self.root / "handoffs" / "scout" / "2026-11-05-weekly-round.md", WORK)
        (self.brain / "schedule_state.json").write_text(json.dumps({"watcher.weekly_round": {
            "first_seen": 1, "slot_t": 2, "result": "failed", "ended": int(time.time()), "note": "timed out",
            "work": ["handoffs/scout/2026-11-05-weekly-round.md"]}}))
        got = self.drive([["msg", OWNER, "/continue watcher.weekly_round"]], agent="real", extra=self.fake_claude(FINISH))
        self.assertIn("unattended as scheduled", got[0]["replies"][0])
        self.assertIn("All done", got[0]["replies"][-1])
        args = self.read("args.txt")
        self.assertIn("--permission-mode\ndontAsk\n", args); self.assertIn("--no-session-persistence", args)
        self.assertIn("handoffs/scout/2026-11-05-weekly-round.md", args)
        denied = args.split("--disallowedTools\n", 1)[1].split("--append-system-prompt\n", 1)[0].splitlines()
        self.assertIn("Edit(brain/company.md)", denied)  # unattended: the owner's fact sheet is read only
        state = json.loads((self.brain / "schedule_state.json").read_text())["watcher.weekly_round"]
        self.assertNotIn("work", state)

    def test_the_agents_get_the_rule_and_may_not_write_the_record(self):
        self.dotenv("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_OWNER_ID=1\n")
        (self.root / "d.py").write_text('import json, sys\nsys.path.insert(0, "core")\nimport jinnee\n'
                                        'print("RESULT " + json.dumps({"g": jinnee.GUARDED, "s": jinnee.SCHEDULED_DENY, "p": jinnee.system_prompt()}))\n')
        r = self.run_py("d.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.split("RESULT ", 1)[1])
        self.assertIn("Edit(brain/interrupted.json)", got["g"]); self.assertIn("Edit(brain/interrupted.json)", got["s"])
        self.assertIn("WORK IN PROGRESS. A run is stopped after 10 minutes", got["p"])
        # the owner's own statements: a scheduled run may not change them, the owner's own run is as before
        self.assertIn("Edit(brain/company.md)", got["s"]); self.assertNotIn("Edit(brain/company.md)", got["g"])
        self.assertIn("<!-- work: in-progress -->", got["p"])


if __name__ == "__main__":
    unittest.main()
