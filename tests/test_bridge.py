"""Who the Telegram bridge obeys. Run: python -m unittest discover tests
core/jinnee.py runs in a copy of the repo with stand-in `telegram` modules: no real token, no network, and `claude` is never called."""
import json, unittest

import test_env as base

TOKEN = "TELEGRAM_BOT_TOKEN=123456:TEST-not-a-real-token\n"
OWNER, STRANGER = 4242, 9999

DRIVER = """\
import asyncio, json, sys, types
sys.path.insert(0, "core")
import jinnee

ran = []
jinnee.run_agent = lambda message: (ran.append(message), "reply")[1]

class Message:
    def __init__(self, text): self.text, self.replies = text, []
    async def reply_text(self, text): self.replies.append(text)

def send(handler, user_id, text):
    user = None if user_id is None else types.SimpleNamespace(id=user_id)
    update = types.SimpleNamespace(effective_user=user, message=Message(text))
    asyncio.run(handler(update, None))
    return update.message.replies

out = {"owner": jinnee.OWNER, "steps": []}
for handler, user_id, text in json.loads(sys.argv[1]):
    jinnee.FRESH["next"] = False
    before = len(ran)
    replies = send(getattr(jinnee, handler), user_id, text)
    out["steps"].append({"ran": ran[before:], "replies": replies, "fresh": jinnee.FRESH["next"]})
print("RESULT " + json.dumps(out))
"""


class Bridge(base.RepoCopy):
    def start(self, dotenv):
        self.dotenv(dotenv)
        return self.run_py("core/jinnee.py")

    def drive(self, dotenv, steps):
        """Calls the real handlers with made-up updates; run_agent is replaced by a recorder."""
        self.dotenv(dotenv)
        (self.root / "driver.py").write_text(DRIVER)
        r = self.run_py("driver.py", None, json.dumps(steps))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.split("RESULT ", 1)[1])

    def assertRefused(self, r):
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TELEGRAM_OWNER_ID is missing or not a number", r.stderr)
        self.assertIn("@userinfobot", r.stderr)  # says where the number comes from
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("STUB_TOKEN", r.stdout)  # stopped before the bot was even built
        self.assertNotIn("is running", r.stdout)

    def test_does_not_start_without_an_owner(self):
        for owner_line in ("", "TELEGRAM_OWNER_ID=\n", "TELEGRAM_OWNER_ID=0\n", "TELEGRAM_OWNER_ID=   # todo\n"):
            with self.subTest(owner_line=owner_line):
                self.assertRefused(self.start(TOKEN + owner_line))

    def test_does_not_start_with_an_owner_that_is_not_a_number(self):
        for value in ("@myname", "12 34", "-4242", "4242abc", "٤٢"):
            with self.subTest(value=value):
                self.assertRefused(self.start(TOKEN + f"TELEGRAM_OWNER_ID='{value}'\n"))

    def test_does_not_start_without_a_token(self):
        r = self.start(f"TELEGRAM_BOT_TOKEN=\nTELEGRAM_OWNER_ID={OWNER}\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TELEGRAM_BOT_TOKEN is not set", r.stderr)
        self.assertNotIn("STUB_TOKEN", r.stdout)

    def test_starts_with_an_owner_and_schedules_the_brief(self):
        r = self.start(TOKEN + f"TELEGRAM_OWNER_ID={OWNER}\nJINNEE_NAME=Testbot\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('STUB_TOKEN "123456:TEST-not-a-real-token"', r.stdout)
        self.assertIn("STUB_DAILY morning_brief 08:00:00", r.stdout)
        self.assertIn("Testbot is running", r.stdout)

    def test_only_the_owner_reaches_the_agent(self):
        got = self.drive(TOKEN + f"TELEGRAM_OWNER_ID={OWNER}\n", [
            ["on_message", STRANGER, "delete everything"],
            ["on_message", None, "a channel post has no user"],
            ["cmd_new", STRANGER, "/new"],
            ["on_message", OWNER, "hello"],
            ["cmd_new", OWNER, "/new"],
        ])
        self.assertEqual(got["owner"], OWNER)
        stranger, nobody, stranger_new, owner, owner_new = got["steps"]
        for dropped in (stranger, nobody, stranger_new):
            self.assertEqual(dropped, {"ran": [], "replies": [], "fresh": False})  # no agent run, no reply, no reset
        self.assertEqual(owner, {"ran": ["hello"], "replies": ["reply"], "fresh": False})
        self.assertEqual((owner_new["ran"], len(owner_new["replies"]), owner_new["fresh"]), ([], 1, True))

    def test_handlers_drop_everyone_when_no_owner_is_set(self):
        """main() already refuses to start; the handlers must not be open either if they are reached another way."""
        for owner_line in ("", "TELEGRAM_OWNER_ID=0\n"):
            with self.subTest(owner_line=owner_line):
                got = self.drive(TOKEN + owner_line, [["on_message", STRANGER, "hi"], ["on_message", 0, "hi"],
                                                      ["cmd_new", STRANGER, "/new"]])
                self.assertEqual(got["owner"], 0)
                for step in got["steps"]:
                    self.assertEqual(step, {"ran": [], "replies": [], "fresh": False})


if __name__ == "__main__":
    unittest.main()
