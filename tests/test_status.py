"""/status (step 6): what is running, how many items wait, what is unfinished, the next scheduled job; answered at once,
also while a run holds RUNNING, and without any customer data. Run: python -m unittest discover tests"""
import json, unittest

from test_alerts import Bridge, OWNER, STRANGER, TOKEN

LINE = """\
import asyncio, json, sys, threading, time, types
sys.path.insert(0, "core")
import jinnee

class Message:
    def __init__(self, text): self.text, self.replies = text, []
    async def reply_text(self, text, reply_markup=None): self.replies.append(text)

def update(text):
    return types.SimpleNamespace(effective_user=types.SimpleNamespace(id=4242), message=Message(text))

order, out = [], {}
held, release = threading.Event(), threading.Event()

def busy():  # a scheduled job's run: it holds RUNNING until the test lets it go
    with jinnee.holding("job", "Milo: stock alert"):
        held.set(); release.wait(10)

async def slow(name):  # an ordinary update that takes long (an agent run): it waits for the run above to end
    order.append(name + " start")
    await asyncio.to_thread(release.wait, 10)
    order.append(name + " end")

async def main():
    line = jinnee.OwnerLine(); await line.initialize()
    worker = threading.Thread(target=busy, daemon=True); worker.start(); held.wait(5)
    first = asyncio.create_task(line.process_update(update("first"), slow("first")))
    second = asyncio.create_task(line.process_update(update("second"), slow("second")))
    status = update("/status")
    started = time.monotonic()
    await asyncio.wait_for(line.process_update(status, jinnee.on_command(status, None)), 5)
    out["seconds"] = time.monotonic() - started
    out["status"] = status.message.replies
    out["before_release"] = list(order)
    out["running_while_status"] = jinnee.RUNNING.locked()
    release.set(); await first; await second
    worker.join(5)  # the scheduled run has let go of RUNNING (else the check below races it)
    out["order"] = order
    after = update("/status@testbot"); await jinnee.on_command(after, None)
    out["after"] = after.message.replies

asyncio.run(main())
print("RESULT " + json.dumps(out))
"""


class Status(Bridge):
    def test_idle_answer_is_plain_code_and_counts_only(self):
        self.add("Reply to Anna", "Dear Anna, thank you.")
        got = self.drive([["msg", OWNER, "/status"], ["msg", STRANGER, "/status"]])
        self.assertEqual(got[0]["ran"], [])  # no agent run
        lines = got[0]["replies"][0].split("\n")
        self.assertEqual(lines[0], "Not working on anything right now.")
        self.assertEqual(lines[1], "Waiting for your decision: 1. /pending shows them.")
        self.assertEqual(lines[2], "No unfinished work.")
        self.assertTrue(lines[3].startswith("Next scheduled: "), lines[3])
        self.assertEqual(len(lines), 4)
        self.assertNotIn("Anna", got[0]["replies"][0])  # the item's title (a customer's name) is never shown
        self.assertEqual(got[1]["replies"], [])  # a stranger gets nothing

    def test_unfinished_work_is_counted(self):
        left = self.root / "handoffs" / "forge" / "left.md"
        left.parent.mkdir(parents=True)
        left.write_text("<!-- work: in-progress -->\n# Job\n- [x] one\n- [ ] two\n", encoding="utf-8")
        got = self.drive([["msg", OWNER, "/status"]])
        self.assertIn("Unfinished work: 1. /continue goes on with it.", got[0]["replies"][0])
        self.assertIn("Nothing is waiting for your decision.", got[0]["replies"][0])

    def test_answers_at_once_while_a_run_holds_the_line_and_keeps_the_order_of_the_rest(self):
        self.dotenv(TOKEN)
        (self.root / "line.py").write_text(LINE)
        r = self.run_py("line.py", None)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.split("RESULT ", 1)[1])
        self.assertLess(got["seconds"], 3)
        self.assertTrue(got["running_while_status"])
        (reply,) = got["status"]
        self.assertTrue(reply.startswith("Working now: a scheduled run (Milo: stock alert), for less than a minute (since "), reply)
        self.assertEqual(got["before_release"], ["first start"])  # the second ordinary update waits behind the first
        self.assertEqual(got["order"], ["first start", "first end", "second start", "second end"])
        self.assertTrue(got["after"][0].startswith("Not working on anything right now."))

    def test_status_does_not_cancel_a_change_waiting_for_its_words(self):
        a = self.add()
        got = self.drive([["msg", OWNER, f"/change {a}"], ["msg", OWNER, "/status"], ["msg", OWNER, "shorter, please"]])
        self.assertIn("What should change?", got[0]["replies"][0])
        self.assertIn("Sent back with your note", got[2]["replies"][0]); self.assertEqual(got[2]["ran"], [])
        self.assertEqual(self.book.get(a)["decision"], "edit")

    def test_it_is_a_real_command_in_the_help_and_the_prompt(self):
        self.dotenv(TOKEN)
        (self.root / "p.py").write_text('import json, sys\nsys.path.insert(0, "core")\nimport jinnee\n'
                                        'print("RESULT " + json.dumps([jinnee.system_prompt(), jinnee.owner_command("/nonsense")]))\n')
        r = self.run_py("p.py", None)
        self.assertEqual(r.returncode, 0, r.stderr)
        prompt, help_line = json.loads(r.stdout.split("RESULT ", 1)[1])
        self.assertIn("/status (what is going on right now)", prompt)
        self.assertNotIn("no /forge or /status", prompt)
        self.assertIn("/status answers at once", prompt)
        self.assertIn("· /status ·", help_line)

    def test_the_bridge_starts_with_the_line(self):
        self.dotenv(TOKEN)
        r = self.run_py("core/jinnee.py", None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STUB_CONCURRENT OwnerLine 256", r.stdout)


if __name__ == "__main__":
    unittest.main()
