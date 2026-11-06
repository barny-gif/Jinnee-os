"""The owner's own conversation (step 6): a session id of its own in brain/conversation.json, started with --session-id,
resumed with --resume, renewed by /new, and replaced by a clean one when the CLI no longer finds it.
Run: python -m unittest discover tests. `claude` is a fake shell script here; nothing reaches the real CLI."""
import json, unittest, uuid

from test_alerts import Bridge, OWNER

CALLS = """printf "%s\\n" "$@" >> calls.txt; echo "----END----" >> calls.txt
case " $* " in *" --resume "*) if [ -n "$GONE" ]; then echo "No conversation found with session ID: x" >&2; exit 1; fi;; esac
if [ -n "$BROKEN" ]; then echo "Invalid API key" >&2; exit 1; fi
echo "$REPLY"
"""


class OwnConversation(Bridge):
    def claude(self, reply="fine", **flags):
        env = self.fake_claude(CALLS)
        env.update(REPLY=reply, GONE=flags.get("gone", ""), BROKEN=flags.get("broken", ""))
        return env

    def calls(self):
        path = self.root / "calls.txt"
        if not path.exists(): return []
        return [c.strip("\n").split("\n") for c in path.read_text(encoding="utf-8").split("----END----\n") if c.strip()]

    @staticmethod
    def session(args):
        """→ (flag, id) of the owner's conversation in one call, or None."""
        for flag in ("--session-id", "--resume"):
            if flag in args: return flag, args[args.index(flag) + 1]
        return None

    def record(self):
        return json.loads((self.brain / "conversation.json").read_text(encoding="utf-8"))

    def test_the_first_message_starts_it_and_the_next_one_continues_it_even_after_a_restart(self):
        self.drive([["msg", OWNER, "hello"]], agent="real", extra=self.claude())
        self.drive([["msg", OWNER, "and again"]], agent="real", extra=self.claude())  # a new process: the bridge restarted
        first, second = self.calls()
        flag, sid = self.session(first)
        self.assertEqual(flag, "--session-id"); uuid.UUID(sid)
        self.assertEqual(self.session(second), ("--resume", sid))
        for call in (first, second):
            self.assertNotIn("--continue", call); self.assertNotIn("--no-session-persistence", call)
        self.assertEqual(self.record()["id"], sid); self.assertTrue(self.record()["started"])

    def test_new_starts_another_one(self):
        got = self.drive([["msg", OWNER, "hello"], ["msg", OWNER, "/new"], ["msg", OWNER, "fresh topic"], ["msg", OWNER, "more"]],
                         agent="real", extra=self.claude())
        self.assertEqual(got[1]["replies"], ["New day, clean slate. What are we doing?"])
        one, two, three = self.calls()  # /new itself runs no agent
        self.assertEqual(self.session(one)[0], "--session-id")
        self.assertEqual(self.session(two)[0], "--session-id"); self.assertNotEqual(self.session(two)[1], self.session(one)[1])
        self.assertEqual(self.session(three), ("--resume", self.session(two)[1]))

    def test_a_lost_conversation_falls_back_to_a_clean_one_and_says_so_in_one_line(self):
        self.drive([["msg", OWNER, "hello"]], agent="real", extra=self.claude())
        old = self.record()["id"]
        got = self.drive([["msg", OWNER, "still there?"]], agent="real", extra=self.claude(reply="Yes.", gone="1"))
        reply = got[0]["replies"][0]
        self.assertTrue(reply.startswith("Yes.\n\n(The earlier conversation could not be found"), reply)
        self.assertEqual(reply.count("\n"), 2)  # the answer, an empty line, the one line
        _, failed, retried = self.calls()
        self.assertEqual(self.session(failed), ("--resume", old))
        flag, new = self.session(retried)
        self.assertEqual(flag, "--session-id"); self.assertNotEqual(new, old)
        self.assertEqual(self.record()["id"], new); self.assertTrue(self.record()["started"])
        got = self.drive([["msg", OWNER, "next"]], agent="real", extra=self.claude(reply="ok"))
        self.assertEqual(self.session(self.calls()[-1]), ("--resume", new)); self.assertEqual(got[0]["replies"], ["ok"])

    def test_another_error_is_not_mistaken_for_a_lost_conversation(self):
        self.drive([["msg", OWNER, "hello"]], agent="real", extra=self.claude())
        old = self.record()["id"]
        got = self.drive([["msg", OWNER, "again"]], agent="real", extra=self.claude(broken="1"))
        self.assertIn("Invalid API key", got[0]["replies"][0])
        self.assertEqual(len(self.calls()), 2)  # no second try with a new conversation
        self.assertEqual(self.record()["id"], old)  # kept: the next message resumes it once the error is gone

    def test_a_scheduled_job_does_not_touch_it_and_the_brief_joins_it(self):
        self.drive([["msg", OWNER, "hello"]], agent="real", extra=self.claude())
        before = self.record()
        self.drive([["job", 0, "ops.stock_alert"], ["job", 0, "jinnee.brief"], ["msg", OWNER, "thanks"]], agent="real",
                   extra=self.claude(reply="Stock is fine."))
        _, job, brief, thanks = self.calls()
        self.assertIsNone(self.session(job)); self.assertIn("--no-session-persistence", job); self.assertNotIn("--continue", job)
        self.assertEqual(self.session(brief), ("--resume", before["id"]))  # on purpose: the owner can answer the brief
        self.assertEqual(self.session(thanks), ("--resume", before["id"]))
        self.assertEqual(self.record(), before)

    def test_the_file_is_guarded(self):
        self.dotenv("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_OWNER_ID=1\n")
        (self.root / "g.py").write_text('import json, sys\nsys.path.insert(0, "core")\nimport jinnee\n'
                                        'print("RESULT " + json.dumps([jinnee.GUARDED, jinnee.SCHEDULED_DENY]))\n')
        r = self.run_py("g.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        guarded, denied = json.loads(r.stdout.split("RESULT ", 1)[1])
        self.assertIn("Edit(brain/conversation.json)", guarded); self.assertIn("Edit(brain/conversation.json)", denied)

    def test_an_unusable_file_means_a_new_conversation(self):
        (self.brain / "conversation.json").write_text('{"id": "not-a-uuid", "started": true}')
        self.drive([["msg", OWNER, "hello"]], agent="real", extra=self.claude())
        flag, sid = self.session(self.calls()[0])
        self.assertEqual(flag, "--session-id"); self.assertEqual(self.record()["id"], sid)


if __name__ == "__main__":
    unittest.main()
