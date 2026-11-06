"""Work that is left half-done although the run ended normally, and nothing running in the background (core/jinnee.py,
core/workfile.py). The case behind it: the lead answered "the builder is on it", the builder had been started in the
background, the `claude -p` run ended with the answer and took it along, and /continue said there was nothing to continue,
because only a stopped run (time limit, error) was ever recorded. No network and no real claude: a stand-in `claude`
script on PATH. Run: python3 -m unittest discover tests"""
import json, os, pathlib, re, sys, tempfile, time, unittest

from test_alerts import Bridge, OWNER
from test_workfile import write

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import workfile

LEFT = """<!-- work: in-progress -->
# Export button on the report page
Status: started
- [ ] read the report page
- [ ] add the button
- [ ] tests
"""

# The run ends normally: the work file is opened, the answer claims the work is under way, nothing else happens.
ANNOUNCED = """printf "%s\\n" "$@" > args.txt
mkdir -p handoffs/forge
cat > handoffs/forge/2026-11-06-export-button.md <<'EOF'
""" + LEFT + """EOF
echo "The builder is working on it."
"""
FINISH_ANY = """printf "%s\\n" "$@" > args.txt
F=$(grep -l 'work: in-progress' $(echo "$@" | grep -o 'handoffs/[A-Za-z0-9_./-]*\\.md' | head -1) 2>/dev/null | head -1)
[ -n "$F" ] && printf '%s\\n' '<!-- work: done -->' '# Done' '- [x] all of it' 'Finished and tested.' > "$F"
echo "Done: the change is finished and tested."
"""
SAY = 'printf "%s\\n" "$@" > args.txt\necho "A plain answer."\n'


def aged(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))
    return path


class LeftOpen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_newest_first_only_in_progress_never_the_inbox_and_not_older_than_a_week(self):
        h = self.root / "handoffs"
        aged(write(h / "forge" / "a.md", LEFT), 2)
        aged(write(h / "scout" / "b.md", LEFT), 1)
        aged(write(h / "office" / "old.md", LEFT), workfile.MAX_AGE_DAYS + 1)
        write(h / "forge" / "done.md", "<!-- work: done -->\n# Done\n")
        write(h / "forge" / "plain.md", "# a plain handoff\n")
        write(h / "inbox" / "owner.md", LEFT)
        self.assertEqual([p for p, _ in workfile.left_open(self.root)], ["handoffs/scout/b.md", "handoffs/forge/a.md"])
        self.assertEqual(workfile.MAX_AGE_DAYS, 7)
        self.assertIn("handoffs/office/old.md", [p for p, _ in workfile.left_open(self.root, max_age_days=30)])
        self.assertEqual(workfile.left_open(self.root / "nowhere"), [])

    def test_the_prompt_for_a_file_without_a_recorded_task(self):
        text = workfile.resume_prompt(["handoffs/forge/a.md"], None, 10, stopped=False)
        self.assertIn("An earlier run ended and left this work unfinished.", text)
        self.assertIn("handoffs/forge/a.md", text); self.assertIn("the step list in the file say what it is", text)
        self.assertNotIn("stopped after", text)
        self.assertIn("stopped after 10 minutes", workfile.resume_prompt(["x.md"], "task", 10))  # unchanged for a stopped run


class AnnouncedButNotDone(Bridge):
    """The case from 2026-11-06, reproduced, and what the owner sees now."""
    def read(self, name):
        return (self.root / name).read_text(encoding="utf-8")

    def test_a_normal_run_that_leaves_a_work_file_says_so_and_continue_finds_it(self):
        got = self.drive([["msg", OWNER, "Add an export button to the report page"]], agent="real", extra=self.fake_claude(ANNOUNCED))
        reply = got[0]["replies"][-1]
        self.assertTrue(reply.startswith("The builder is working on it."))
        self.assertTrue(reply.endswith("(Unfinished work was left in handoffs/forge/2026-11-06-export-button.md. "
                                       "Send /continue to go on with it.)"), reply)
        self.assertFalse((self.brain / "interrupted.json").exists())  # nothing was stopped: no record, the file is enough

        got = self.drive([["msg", OWNER, "/continue"]], agent="real", extra=self.fake_claude(FINISH_ANY))
        self.assertEqual(got[0]["replies"][0], "Going on from handoffs/forge/2026-11-06-export-button.md (left unfinished).")
        self.assertEqual(got[0]["replies"][-1], "Done: the change is finished and tested.")  # finished: no extra line
        args = self.read("args.txt")
        for part in ("CONTINUE. An earlier run ended and left this work unfinished.", "handoffs/forge/2026-11-06-export-button.md",
                     "--permission-mode\nacceptEdits\n"):
            self.assertIn(part, args)
        self.assertEqual(workfile.status(self.root / "handoffs/forge/2026-11-06-export-button.md"), "done")
        got = self.drive([["msg", OWNER, "/continue"]])
        self.assertEqual(got[0]["replies"], ["Nothing was cut off or left unfinished, so there is nothing to continue."])

    def test_a_bare_continue_word_finds_a_file_left_unfinished(self):
        write(self.root / "handoffs" / "forge" / "2026-11-06-export-button.md", LEFT)
        got = self.drive([["msg", OWNER, "folytasd"]], agent="real", extra=self.fake_claude(FINISH_ANY))
        self.assertEqual(got[0]["replies"][0], "Going on from handoffs/forge/2026-11-06-export-button.md (left unfinished).")
        self.assertIn("CONTINUE.", self.read("args.txt"))

    def test_a_plain_answer_without_a_work_file_has_no_extra_line(self):
        got = self.drive([["msg", OWNER, "What is on today?"]], agent="real", extra=self.fake_claude(SAY))
        self.assertEqual(got[0]["replies"], ["A plain answer."])

    def test_an_older_unfinished_file_is_not_announced_as_left_by_this_run(self):
        aged(write(self.root / "handoffs" / "forge" / "2026-11-05-other.md", LEFT), 1)
        got = self.drive([["msg", OWNER, "What is on today?"]], agent="real", extra=self.fake_claude(SAY))
        self.assertEqual(got[0]["replies"], ["A plain answer."])

    def test_several_unfinished_files_are_listed_and_one_is_chosen_by_number(self):
        h = self.root / "handoffs"
        aged(write(h / "forge" / "2026-11-04-export-button.md", LEFT.replace("- [ ] read", "- [x] read")), 2)
        aged(write(h / "scout" / "2026-11-05-price-round.md", LEFT), 1)
        got = self.drive([["msg", OWNER, "/continue"]], agent="real", extra=self.fake_claude(FINISH_ANY))
        reply = got[0]["replies"][0]
        self.assertEqual(len(got[0]["replies"]), 1)  # only the list: nothing is run before the owner chooses
        self.assertTrue(reply.startswith("Unfinished work, newest first:\n1. handoffs/scout/2026-11-05-price-round.md"), reply)
        self.assertIn("\n2. handoffs/forge/2026-11-04-export-button.md: left unfinished", reply)
        self.assertIn("1 of 3 steps done", reply); self.assertIn("/continue_1, /continue_2", reply)
        self.assertFalse((self.root / "args.txt").exists())

        got = self.drive([["msg", OWNER, "/continue_2"]], agent="real", extra=self.fake_claude(FINISH_ANY))
        self.assertEqual(got[0]["replies"][0], "Going on from handoffs/forge/2026-11-04-export-button.md (left unfinished).")
        self.assertIn("handoffs/forge/2026-11-04-export-button.md", self.read("args.txt"))
        self.assertEqual(workfile.status(h / "scout" / "2026-11-05-price-round.md"), "in-progress")  # the other one untouched
        got = self.drive([["msg", OWNER, "/continue 9"]])
        self.assertEqual(got[0]["replies"], ["There is no number 9. Send /continue on its own to see what is unfinished."])

    def test_a_file_older_than_a_week_is_not_offered(self):
        aged(write(self.root / "handoffs" / "forge" / "2026-10-20-old-idea.md", LEFT), 8)
        got = self.drive([["msg", OWNER, "/continue"]])
        self.assertEqual(got[0]["replies"], ["Nothing was cut off or left unfinished, so there is nothing to continue."])
        got = self.drive([["msg", OWNER, "folytasd"]])
        self.assertEqual(got[0]["ran"], ["folytasd"])  # an ordinary message then

    def test_a_stopped_run_and_a_left_file_are_both_offered(self):
        h = self.root / "handoffs"
        write(h / "jinnee" / "stopped.md", LEFT)
        (self.brain / "interrupted.json").write_text(json.dumps({"task": "Do the round", "files": ["handoffs/jinnee/stopped.md"],
                                                                 "at": int(time.time()) - 3600, "attempt": 1}))
        write(h / "forge" / "left.md", LEFT)
        got = self.drive([["msg", OWNER, "/continue"]])
        reply = got[0]["replies"][0]
        self.assertIn("1. handoffs/forge/left.md: left unfinished", reply)
        self.assertIn("2. handoffs/jinnee/stopped.md: stopped", reply)
        self.assertEqual(reply.count("handoffs/jinnee/stopped.md"), 1)  # the stopped run's file is not listed twice
        got = self.drive([["msg", OWNER, "/continue_2"]], agent="real", extra=self.fake_claude(FINISH_ANY))
        self.assertEqual(got[0]["replies"][0], "Going on from handoffs/jinnee/stopped.md.")
        self.assertIn("The original task:\nDo the round", self.read("args.txt"))


class NothingInTheBackground(Bridge):
    def facts(self, extra=None):
        self.dotenv("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_OWNER_ID=1\nPACKS=general,ecom\n")
        (self.root / "d.py").write_text(
            'import json, re, sys\nsys.path.insert(0, "core")\nimport jinnee\n'
            'keys = ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_AUTO_BACKGROUND_TASKS")\n'
            'envs = {k: {n: e.get(n) for n in keys} for k, e in (("owner", jinnee.command("x")[2]), ("scheduled", jinnee.command("x", unattended=True)[2]))}\n'
            'handled = re.search(r"\\(([a-z|]+)\\)", jinnee.COMMAND.pattern).group(1).split("|")\n'
            'print("RESULT " + json.dumps({"env": envs, "p": jinnee.system_prompt(), "help": jinnee.owner_command("/nonsense"),\n'
            '      "cmds": [c for c, _ in jinnee.OWNER_COMMANDS], "handled": handled}))\n')
        r = self.run_py("d.py", extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.split("RESULT ", 1)[1])

    def test_every_run_has_background_tasks_switched_off(self):
        got = self.facts({"CLAUDE_AUTO_BACKGROUND_TASKS": "1"})  # even when the machine asks for the opposite
        for kind in ("owner", "scheduled"):
            self.assertEqual(got["env"][kind], {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "CLAUDE_AUTO_BACKGROUND_TASKS": None})

    def test_the_rule_is_in_the_prompt_and_the_commands_are_the_real_ones(self):
        got = self.facts()
        p = got["p"]
        for part in ("FINISH IN THIS RUN.", "Never start a subagent (Forge or any other agent) or a long command in the background",
                     "wait for its result in this same run", "Tell the owner only about work that is done",
                     "the pull request address", "no others exist"):
            self.assertIn(part, p)
        # one list: the bridge's own commands, the help line and the prompt agree
        self.assertEqual(set(got["cmds"]), {"/" + w for w in got["handled"]} | {"/continue", "/new"})
        self.assertEqual(got["help"], "Commands: " + " · ".join(got["cmds"]))
        rule = p.split("FINISH IN THIS RUN.", 1)[1].split("WORK IN PROGRESS.", 1)[0]
        for c in got["cmds"]: self.assertIn(c + " (", rule)
        # no agent file offers a command that does not exist (it was "/forge status" and friends)
        # (saying that it does not exist is allowed: "no /forge", "no `/forge`")
        self.assertEqual(re.findall(r"(?<![\w./])(?<!no )(?<!no `)/forge(?![\w/.-])", p), [])
        self.assertIn("there is no /forge", rule)

    def test_the_agent_files_say_it_too(self):
        lead = (REPO / "core" / "jinnee.md").read_text(encoding="utf-8")
        forge = (REPO / "packs" / "ecom" / "agents" / "forge.md").read_text(encoding="utf-8")
        for text in (lead, forge):
            self.assertIn("foreground", text); self.assertIn("in the background", text)
            self.assertIn("/continue", text); self.assertIn("pull request address", text)
        self.assertNotIn("`/forge ", forge)  # the old "/forge status", "/forge connector", "/forge rollback" are gone


if __name__ == "__main__":
    unittest.main()
