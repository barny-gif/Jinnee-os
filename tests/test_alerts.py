"""Alerts, quiet hours, reminders, the heartbeat, and the bridge's own commands. Run: python -m unittest discover tests
The clock is passed in, so nothing here waits. The bridge parts run core/jinnee.py in a copy of the repo with stand-in
`telegram` modules and a stand-in `claude` on PATH: no network, no real token, no real agent."""
import datetime as dt, json, os, pathlib, sys, tempfile, unittest
from unittest import mock

import test_env as base

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import alerts, approvals, autonomy, heartbeat

HOUR, DAY = 3600, 86400


def at(day, hour, minute=0):
    """A local time on a fixed week, as epoch seconds on this machine's clock (JINNEE_TZ is unset in these tests)."""
    return dt.datetime(2026, 3, day, hour, minute).timestamp()


class Watching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brain = pathlib.Path(self.tmp.name) / "brain"
        self.brain.mkdir()
        patch = mock.patch.dict(os.environ, {"PACKS": "general,ecom"})
        patch.start(); self.addCleanup(patch.stop)
        for k in ("QUIET_HOURS", "REMIND_AFTER_DAYS", "STUCK_AFTER_HOURS", "UNDO_SECONDS", "JINNEE_TZ"): os.environ.pop(k, None)
        self.watch = alerts.Watch(self.brain)
        self.book = self.watch.book

    def add(self, created, title="Reply to Anna", text="Dear Anna, thank you."):
        with mock.patch("time.time", return_value=created):
            return self.book.add("office", "send_customer_email", title, text=text)[0]["id"]

    def decide(self, item_id, when, decision="ok", note=""):
        with mock.patch("time.time", return_value=when):
            self.book.decide(item_id, decision, note)

    def check(self, now, watch=None):
        """One round of what the bridge does every minute: ask, 'send', remember."""
        watch = watch or self.watch
        plan = watch.due(now)
        watch.done(plan)
        return plan["messages"]


class QuietHours(unittest.TestCase):
    def test_default_window_wraps_midnight(self):
        for hour, minute, expected in ((20, 59, False), (21, 0, True), (23, 30, True), (0, 0, True), (6, 59, True), (7, 0, False), (12, 0, False)):
            self.assertEqual(alerts.quiet(at(3, hour, minute), "21:00-07:00"), expected, (hour, minute))

    def test_daytime_window_and_off(self):
        self.assertTrue(alerts.quiet(at(3, 13), "12:00-14:00"))
        self.assertFalse(alerts.quiet(at(3, 14), "12:00-14:00"))
        for off in ("", "off", "OFF", "none", "08:00-08:00"):
            self.assertFalse(alerts.quiet(at(3, 23), off), off)

    def test_nonsense_means_the_default(self):
        for spec in ("evening", "25:00-07:00", "21-7", "21:00"):
            self.assertTrue(alerts.quiet(at(3, 23), spec), spec)
            self.assertFalse(alerts.quiet(at(3, 12), spec), spec)

    def test_from_the_environment(self):
        with mock.patch.dict(os.environ, {"QUIET_HOURS": "13:00-15:00"}):
            self.assertTrue(alerts.quiet(at(3, 14)))
            self.assertFalse(alerts.quiet(at(3, 23)))
        with mock.patch.dict(os.environ):
            os.environ.pop("QUIET_HOURS", None)
            self.assertTrue(alerts.quiet(at(3, 23)))

    def test_time_zone(self):
        noon_utc = dt.datetime(2026, 3, 3, 12, 0, tzinfo=dt.timezone.utc).timestamp()
        try:
            import zoneinfo; zoneinfo.ZoneInfo("Asia/Tokyo")
        except Exception: self.skipTest("no time zone data on this machine")
        with mock.patch.dict(os.environ, {"JINNEE_TZ": "Asia/Tokyo"}):  # 21:00 there
            self.assertTrue(alerts.quiet(noon_utc, "21:00-07:00"))
        with mock.patch.dict(os.environ, {"JINNEE_TZ": "America/New_York"}):  # 07:00 there
            self.assertFalse(alerts.quiet(noon_utc, "21:00-07:00"))
        with mock.patch.dict(os.environ, {"JINNEE_TZ": "Not/AZone"}):
            self.assertIsNone(alerts.zone())  # falls back to the machine's clock, with a line in the log


class NewItems(Watching):
    def test_told_once_per_item(self):
        a = self.add(at(3, 10))
        first = self.check(at(3, 10, 1))
        self.assertEqual(len(first), 1)
        self.assertIn(f"[{a}] Reply to Anna (office, Send an email to a customer)", first[0])
        self.assertIn(f"/ok {a}", first[0])
        for minute in range(2, 30):
            self.assertEqual(self.check(at(3, 10, minute)), [])
        b = self.add(at(3, 11), "Second")
        again = self.check(at(3, 11, 1))
        self.assertEqual(len(again), 1)
        self.assertIn(b, again[0]); self.assertNotIn(a, again[0])

    def test_several_new_items_are_one_message(self):
        ids = [self.add(at(3, 10), f"Item {n}") for n in range(4)]
        messages = self.check(at(3, 10, 1))
        self.assertEqual(len(messages), 1)
        for i in ids: self.assertIn(f"[{i}]", messages[0])

    def test_an_item_that_cannot_go_out_says_so(self):
        self.add(at(3, 10), text="DRAFT")
        self.assertIn("not sendable as it is: the text starts with a draft marker", self.check(at(3, 10, 1))[0])

    def test_held_through_quiet_hours_then_sent_once(self):
        a = self.add(at(3, 22))
        for hour in (22, 23):
            self.assertEqual(self.check(at(3, hour, 30)), [])
        self.assertEqual(self.check(at(4, 6, 59)), [])
        morning = self.check(at(4, 7, 0))
        self.assertEqual(len(morning), 1); self.assertIn(a, morning[0])
        self.assertEqual(self.check(at(4, 7, 1)), [])

    def test_decided_before_anyone_was_told_means_no_message(self):
        a = self.add(at(3, 22))
        self.decide(a, at(3, 22, 5))  # the owner saw it on the dashboard
        self.book.consume(a); self.book.result(a, "sent")
        self.assertEqual(self.check(at(4, 7, 0)), [])

    def test_restart_does_not_repeat(self):
        self.add(at(3, 10))
        self.assertEqual(len(self.check(at(3, 10, 1))), 1)
        again = alerts.Watch(self.brain)  # a new process: only the file remembers
        self.assertEqual(self.check(at(3, 10, 2), again), [])
        self.assertIn("asked", json.loads((self.brain / "alerts_state.json").read_text()))

    def test_a_message_that_was_not_sent_is_tried_again(self):
        self.add(at(3, 10))
        self.assertEqual(len(self.watch.due(at(3, 10, 1))["messages"]), 1)  # sending failed: done() was never called
        self.assertEqual(len(self.check(at(3, 10, 2))), 1)
        self.assertEqual(self.check(at(3, 10, 3)), [])

    def test_lost_state_file_repeats_at_most_once(self):
        self.add(at(3, 10))
        self.check(at(3, 10, 1))
        (self.brain / "alerts_state.json").write_text("{half")
        self.assertEqual(len(self.check(at(3, 10, 2))), 1)
        self.assertEqual(self.check(at(3, 10, 3)), [])


class Reminders(Watching):
    def test_waiting_for_days(self):
        a = self.add(at(3, 10))
        self.check(at(3, 10, 1))
        self.assertEqual(self.check(at(5, 9, 59)), [])  # not two days yet
        late = self.check(at(5, 10, 0))
        self.assertEqual(len(late), 1)
        self.assertIn("Still open", late[0]); self.assertIn(f"[{a}]", late[0]); self.assertIn("waiting for you for 2 days", late[0])

    def test_at_most_one_reminder_a_day(self):
        self.add(at(3, 10), "One"); self.add(at(3, 10), "Two")
        self.check(at(3, 10, 1))
        sent = [m for hour in range(7, 21) for m in self.check(at(6, hour))]
        self.assertEqual(len(sent), 1)
        self.assertIn("One", sent[0]); self.assertIn("Two", sent[0])
        self.assertEqual(len([m for hour in range(7, 21) for m in self.check(at(7, hour))]), 1)  # and one the next day

    def test_threshold_from_the_environment(self):
        self.add(at(3, 10))
        self.check(at(3, 10, 1))
        with mock.patch.dict(os.environ, {"REMIND_AFTER_DAYS": "5"}):
            self.assertEqual(self.check(at(7, 12)), [])
            self.assertEqual(len(self.check(at(8, 10, 1))), 1)

    def test_reminder_waits_for_the_morning(self):
        self.add(at(3, 23))
        self.assertEqual(self.check(at(5, 23, 30)), [])
        morning = self.check(at(6, 7, 0))  # first told about it, and it is not also "still open" in the same breath
        self.assertEqual(len(morning), 1)
        self.assertIn("Waiting for your decision", morning[0])

    def test_nothing_open_means_silence(self):
        a = self.add(at(3, 10)); self.check(at(3, 10, 1))
        self.decide(a, at(3, 11), "drop", "no")
        self.book.consume(a)
        self.assertEqual([m for day in (5, 6, 7) for m in self.check(at(day, 12))], [])


class Stuck(Watching):
    def test_approved_and_not_carried_out(self):
        a = self.add(at(3, 8)); self.check(at(3, 8, 1))
        self.decide(a, at(3, 9))
        self.assertEqual(self.check(at(3, 14, 59)), [])
        stuck = self.check(at(3, 15, 0))
        self.assertEqual(len(stuck), 1)
        self.assertIn("Stuck", stuck[0]); self.assertIn(f"[{a}]", stuck[0]); self.assertIn("approved, not picked up yet, for 6 h", stuck[0])
        self.assertIn("/undo", stuck[0])  # says what the owner can do
        self.assertEqual(self.check(at(3, 16)), [])  # once
        self.assertEqual(self.check(at(3, 20)), [])  # and not again as a reminder on the same day
        tomorrow = self.check(at(4, 9))
        self.assertEqual(len(tomorrow), 1); self.assertIn("Still open", tomorrow[0]); self.assertIn(a, tomorrow[0])

    def test_started_and_never_reported_back(self):
        a = self.add(at(3, 8)); self.check(at(3, 8, 1))
        self.decide(a, at(3, 9))
        with mock.patch("time.time", return_value=at(3, 9, 5)):
            self.assertEqual(self.book.consume(a).word, "GO")
        self.assertEqual(self.check(at(3, 15, 0)), [])
        stuck = self.check(at(3, 15, 5))
        self.assertEqual(len(stuck), 1); self.assertIn("being carried out, for 6 h", stuck[0])
        self.book.result(a, "sent")
        self.assertEqual(self.check(at(4, 9)), [])

    def test_approved_but_not_sendable_is_stuck_with_its_reason(self):
        a = self.add(at(3, 8), text=""); self.check(at(3, 8, 1))
        self.decide(a, at(3, 9))
        self.assertIn("approved, but not sendable: the text is empty", self.check(at(3, 15))[0])

    def test_threshold_from_the_environment_and_quiet_hours(self):
        a = self.add(at(3, 8)); self.check(at(3, 8, 1))
        self.decide(a, at(3, 18))
        with mock.patch.dict(os.environ, {"STUCK_AFTER_HOURS": "4"}):
            self.assertEqual(self.check(at(3, 21, 59)), [])
            self.assertEqual(self.check(at(3, 22, 0)), [])  # due, but it is night
            self.assertEqual(len(self.check(at(4, 7, 0))), 1)


class HandOver(Watching):
    def test_a_decision_goes_to_the_lead_once_after_the_undo_pause(self):
        a = self.add(at(3, 10)); self.check(at(3, 10, 1))
        self.decide(a, at(3, 11, 0))
        self.assertEqual(self.watch.due(at(3, 11, 1))["hand_over"], [])  # the owner can still undo
        plan = self.watch.due(at(3, 11, 2))
        self.assertEqual([i["id"] for i in plan["hand_over"]], [a])
        self.watch.done(plan)
        self.assertEqual(self.watch.due(at(3, 11, 3))["hand_over"], [])
        self.assertEqual(alerts.Watch(self.brain).due(at(3, 11, 4))["hand_over"], [])  # nor after a restart
        text = alerts.hand_over_message(self.book, plan["hand_over"])
        self.assertIn(f"[{a}]", text); self.assertIn("consume", text)

    def test_undone_in_time_means_nothing_is_handed_over(self):
        a = self.add(at(3, 10)); self.check(at(3, 10, 1))
        self.decide(a, at(3, 11, 0))
        self.book.undo(a)
        self.assertEqual(self.watch.due(at(3, 11, 5))["hand_over"], [])
        self.decide(a, at(3, 12, 0), "edit", "shorter")  # decided afresh: handed over as a new decision
        self.assertEqual(self.watch.due(at(3, 12, 1))["hand_over"], [])

    def test_decisions_are_handed_over_at_night_too(self):
        a = self.add(at(3, 10)); self.check(at(3, 10, 1))
        self.decide(a, at(3, 22, 0))
        plan = self.watch.due(at(3, 22, 3))
        self.assertEqual(([i["id"] for i in plan["hand_over"]], plan["messages"]), ([a], []))


class Other(Watching):
    def test_changed_levels_are_reported_once(self):
        cfg = autonomy.Config(self.brain)
        cfg.write(cfg.defaults())
        self.assertEqual(self.check(at(3, 10)), [])  # the first look is the baseline
        table = cfg.read()[0]; table["send_customer_email"].update(level=2); table["spend_money"].update(locked=False, level=1)
        cfg.write(table)
        changed = self.check(at(3, 10, 1))
        self.assertEqual(len(changed), 1)
        self.assertIn("send_customer_email: approval required, locked at max 2 → act, report afterwards, locked at max 2", changed[0])
        self.assertIn("spend_money: forbidden, locked at max 0 → approval required, not locked", changed[0])
        self.assertEqual(self.check(at(3, 10, 2)), [])

    def test_unreadable_files_are_reported_once(self):
        (self.brain / "autonomy_config.json").write_text("{")
        first = self.check(at(3, 10))
        self.assertEqual(len(first), 1); self.assertIn("autonomy_config.json", first[0])
        self.assertEqual(self.check(at(3, 10, 1)), [])
        (self.brain / "approvals.json").write_text("[{")
        self.assertIn("approvals.json", self.check(at(3, 10, 2))[0])
        self.book.tidy()  # the next writer sets the broken file aside
        aside = self.check(at(3, 10, 3))
        self.assertEqual(len(aside), 1); self.assertIn("set aside as approvals.json.broken-", aside[0])
        self.assertEqual(self.check(at(3, 10, 4)), [])


class Heartbeat(Watching):
    def test_only_real_trouble_is_flagged(self):
        now = at(3, 12)
        with mock.patch("time.time", return_value=now - 5 * HOUR):
            heartbeat.beat("office", "invoices checked", brain=self.brain)       # finished long ago: fine
            heartbeat.beat("ops", "order watch", "working", brain=self.brain)    # started and never finished
            heartbeat.beat("watcher", "timed out", "failed", brain=self.brain)   # failed
        with mock.patch("time.time", return_value=now - 600):
            heartbeat.beat("jinnee", "working", "working", brain=self.brain)     # still inside a normal run
        got = heartbeat.trouble(self.brain, now)
        self.assertEqual(got, ["ops: started 5 h ago and never finished", "watcher: last run failed (timed out)"])
        heartbeat.beat("watcher", "weekly round done", brain=self.brain)
        self.assertEqual(heartbeat.trouble(self.brain, now), ["ops: started 5 h ago and never finished"])

    def test_an_agent_that_never_checked_in_is_not_flagged(self):
        self.assertEqual(heartbeat.trouble(self.brain), [])  # the old check flagged every agent of every pack here
        heartbeat.beat("jinnee", "replied", brain=self.brain)
        self.assertEqual(heartbeat.trouble(self.brain, at(9, 12)), [])  # nor one that is idle for days

    def test_broken_file_and_old_entries(self):
        (self.brain / "heartbeat.json").write_text("{half")
        self.assertEqual(heartbeat.trouble(self.brain), [])
        (self.brain / "heartbeat.json").write_text(json.dumps({"office": {"ts": 1, "note": "from before states existed"}, "x": 5}))
        self.assertEqual(heartbeat.trouble(self.brain), [])
        heartbeat.beat("office", "ok", brain=self.brain)
        self.assertEqual(sorted(json.loads((self.brain / "heartbeat.json").read_text())), ["office"])


TOKEN = "TELEGRAM_BOT_TOKEN=123456:TEST-not-a-real-token\nTELEGRAM_OWNER_ID=4242\nPACKS=general,ecom\nQUIET_HOURS=off\nUNDO_SECONDS=0\n"
DRIVER = """\
import asyncio, json, sys, types
sys.path.insert(0, "core")
import jinnee

ran, sent, tasks = [], [], []
if sys.argv[2] == "record": jinnee.run_agent = lambda message: (ran.append(message), "agent reply")[1]
jinnee.TIMEOUT = 2

class Message:
    def __init__(self, text): self.text, self.replies = text, []
    async def reply_text(self, text): self.replies.append(text)

class Bot:
    async def send_message(self, chat_id, text): sent.append([chat_id, text])

ctx = types.SimpleNamespace(bot=Bot(), application=types.SimpleNamespace(create_task=tasks.append))

async def step(kind, user_id, text):
    user = types.SimpleNamespace(id=user_id)
    if kind == "tick":
        await jinnee.alert_tick(ctx)
        for t in tasks: await t
        tasks.clear(); return []
    if kind == "brief":
        await jinnee.morning_brief(ctx); return []
    if kind == "edited":  # Telegram delivers an edit as an update whose .message is None
        update = types.SimpleNamespace(effective_user=user, message=None, edited_message=Message(text))
        for handler in (jinnee.on_message, jinnee.on_command, jinnee.cmd_new): await handler(update, ctx)
        return update.edited_message.replies
    update = types.SimpleNamespace(effective_user=user, message=Message(text))
    await (jinnee.on_command if text.startswith("/") else jinnee.on_message)(update, ctx)
    return update.message.replies

out = []
for kind, user_id, text in json.loads(sys.argv[1]):
    a, b = len(ran), len(sent)
    replies = asyncio.run(step(kind, user_id, text))
    out.append({"replies": replies, "ran": ran[a:], "sent": sent[b:]})
print("RESULT " + json.dumps(out))
"""
OWNER, STRANGER = 4242, 9999


class Bridge(base.RepoCopy):
    def setUp(self):
        super().setUp()
        self.brain = self.root / "brain"
        self.brain.mkdir()
        (self.root / "handoffs").mkdir()
        self.book = approvals.Book(self.brain)
        self.book.gate = autonomy.Config(self.brain, ["general", "ecom"])

    def add(self, title="Reply to Anna", text="Dear Anna, thank you."):
        return self.book.add("office", "send_customer_email", title, text=text)[0]["id"]

    def drive(self, steps, agent="record", extra=None):
        self.dotenv(TOKEN)
        (self.root / "driver.py").write_text(DRIVER)
        r = self.run_py("driver.py", extra, json.dumps(steps), agent)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.split("RESULT ", 1)[1])

    def fake_claude(self, script):
        bin_dir = self.root / "bin"; bin_dir.mkdir(exist_ok=True)
        (bin_dir / "claude").write_text("#!/bin/sh\n" + script); (bin_dir / "claude").chmod(0o755)
        return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def state(self, item_id):
        it = self.book.get(item_id)
        return it["state"], it["decision"], it["decided_by"]


class OwnerCommands(Bridge):
    def test_ok_is_recorded_by_the_bridge_not_the_agent(self):
        a = self.add()
        got = self.drive([["msg", OWNER, f"/ok {a}"], ["msg", OWNER, f"/ok {a}"]])
        self.assertEqual(got[0]["ran"], [])  # no agent run: the decision is plain code
        self.assertIn("Approved: ", got[0]["replies"][0]); self.assertIn(f"/undo_{a}", got[0]["replies"][0])
        self.assertEqual(self.state(a), ("decided", "ok", "telegram"))
        self.assertIn("already approved", got[1]["replies"][0])  # a second /ok changes nothing
        self.assertEqual((self.brain / "decisions.log.md").read_text(encoding="utf-8").count(f"telegram: {a} → ok"), 1)

    def test_tappable_form_and_single_item_shortcut(self):
        a = self.add()
        got = self.drive([["msg", OWNER, f"/ok_{a}"], ["msg", OWNER, f"/undo_{a}"], ["msg", OWNER, "/ok"], ["msg", OWNER, "/undo"]])
        self.assertIn("Approved", got[0]["replies"][0]); self.assertIn("Undone", got[1]["replies"][0])
        self.assertIn("Approved", got[2]["replies"][0]); self.assertIn("Undone", got[3]["replies"][0])
        self.assertEqual(self.state(a)[0], "pending")

    def test_several_items_need_an_id(self):
        a, b = self.add("One"), self.add("Two")
        got = self.drive([["msg", OWNER, "/ok"], ["msg", OWNER, "/ok nope"], ["msg", OWNER, "/drop too pushy"], ["msg", OWNER, "/pending"]])
        self.assertIn("Which one?", got[0]["replies"][0]); self.assertIn(f"[{a}]", got[0]["replies"][0])
        self.assertIn("no item 'nope'", got[1]["replies"][0])
        self.assertIn("Which one?", got[2]["replies"][0])
        self.assertIn(f"[{b}] Two", got[3]["replies"][0])
        self.assertEqual((self.state(a)[0], self.state(b)[0]), ("pending", "pending"))

    def test_a_mistyped_id_never_approves_something_else(self):
        a = self.add()
        got = self.drive([["msg", OWNER, "/ok zzzz"]])
        self.assertIn("no item 'zzzz'", got[0]["replies"][0])
        self.assertEqual(self.state(a)[0], "pending")

    def test_change_and_drop_take_their_words_inline_or_as_the_next_message(self):
        a, b, c = self.add("One"), self.add("Two"), self.add("Three")
        got = self.drive([["msg", OWNER, f"/change {a} address her formally"],
                          ["msg", OWNER, f"/drop_{b}"], ["msg", OWNER, "we never chase small invoices"],
                          ["msg", OWNER, f"/drop_{c}"], ["msg", OWNER, "/pending"], ["msg", OWNER, "how are sales?"]])
        self.assertEqual((self.book.get(a)["decision"], self.book.get(a)["note"]), ("edit", "address her formally"))
        self.assertIn("Why are you dropping it?", got[1]["replies"][0])
        self.assertEqual(got[2]["ran"], [])  # the next message was the reason, not a request for the agent
        self.assertEqual((self.book.get(b)["decision"], self.book.get(b)["note"]), ("drop", "we never chase small invoices"))
        self.assertIn(f"we never chase small invoices [approval {b}]", (self.brain / "lessons.md").read_text(encoding="utf-8"))
        self.assertEqual(self.state(c)[0], "pending")  # another command in between cancels the question
        self.assertEqual(got[5]["ran"], ["how are sales?"])

    def test_only_the_owner(self):
        a = self.add()
        got = self.drive([["msg", STRANGER, f"/ok {a}"], ["msg", STRANGER, "/pending"], ["msg", STRANGER, f"/drop {a} spam"]])
        self.assertEqual([s["replies"] for s in got], [[], [], []])
        self.assertEqual(self.state(a)[0], "pending")

    def test_undo_after_pick_up_is_refused(self):
        a = self.add()
        self.book.decide(a, "ok"); self.book.consume(a)
        got = self.drive([["msg", OWNER, f"/undo {a}"], ["msg", OWNER, "/unknown"]])
        self.assertIn("can no longer be undone", got[0]["replies"][0])
        self.assertIn("Commands:", got[1]["replies"][0])

    def test_edited_message_is_ignored(self):
        a = self.add()
        got = self.drive([["edited", OWNER, "hello again"], ["edited", OWNER, f"/ok {a}"], ["msg", OWNER, "hello"]])
        self.assertEqual(got[0], {"replies": [], "ran": [], "sent": []})  # and no exception
        self.assertEqual(self.state(a)[0], "pending")
        self.assertEqual(got[2]["ran"], ["hello"])


class Ticking(Bridge):
    def test_alert_then_hand_over_then_silence(self):
        a = self.add()
        got = self.drive([["tick", 0, ""], ["tick", 0, ""], ["msg", OWNER, f"/ok {a}"], ["tick", 0, ""], ["tick", 0, ""]])
        self.assertEqual(len(got[0]["sent"]), 1)
        self.assertEqual(got[0]["sent"][0][0], OWNER); self.assertIn("Waiting for your decision", got[0]["sent"][0][1])
        self.assertEqual(got[1], {"replies": [], "ran": [], "sent": []})
        self.assertEqual(len(got[3]["ran"]), 1)  # the decision reaches the lead without the owner writing again
        self.assertIn(f"[{a}]", got[3]["ran"][0]); self.assertIn("consume", got[3]["ran"][0])
        self.assertEqual(got[3]["sent"], [[OWNER, "agent reply"]])
        self.assertEqual(got[4], {"replies": [], "ran": [], "sent": []})

    def test_a_new_process_does_not_send_again(self):
        self.add()
        self.assertEqual(len(self.drive([["tick", 0, ""]])[0]["sent"]), 1)
        self.assertEqual(self.drive([["tick", 0, ""], ["tick", 0, ""]])[0]["sent"], [])

    def test_quiet_hours_from_dotenv(self):
        self.add()
        self.dotenv(TOKEN)
        (self.root / "driver.py").write_text(DRIVER)
        with (self.root / ".env").open("a") as f: f.write("QUIET_HOURS=00:00-23:59\n")  # the later line wins in core/env.py
        r = self.run_py("driver.py", None, json.dumps([["tick", 0, ""]]), "record")
        self.assertEqual(json.loads(r.stdout.split("RESULT ", 1)[1])[0]["sent"], [], r.stderr)

    def test_startup_schedules_the_check_and_migrates(self):
        (self.brain / "autonomy_config.json").write_text(json.dumps({"send_customer_email": 2, "spend_money": 0}))
        (self.brain / "approvals.json").write_text(json.dumps([{"id": "a1", "title": "old", "decision": "ok"}]))
        self.dotenv(TOKEN)
        r = self.run_py("core/jinnee.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STUB_REPEAT alert_tick 60", r.stdout)
        self.assertIn("STUB_DAILY morning_brief 08:00:00", r.stdout)
        levels = json.loads((self.brain / "autonomy_config.json").read_text())
        self.assertEqual((levels["_format"], levels["send_customer_email"]["level"], levels["spend_money"]["locked"]), (2, 2, True))
        self.assertEqual(self.book.get("a1")["state"], "consumed")


class AgentFailures(Bridge):
    def beat(self):
        return json.loads((self.brain / "heartbeat.json").read_text())["jinnee"]

    def test_a_run_that_fails_tells_the_owner_what_to_do(self):
        env = self.fake_claude('echo "Invalid API key · Please run /login" >&2\nexit 1\n')
        got = self.drive([["msg", OWNER, "hello"]], agent="real", extra=env)
        reply = got[0]["replies"][0]
        self.assertIn("could not finish", reply); self.assertIn("What you can do", reply); self.assertIn("claude login", reply)
        self.assertIn("Invalid API key", reply)
        self.assertNotIn("Traceback", reply)
        self.assertEqual((self.beat()["state"], self.beat()["note"]), ("failed", "stopped with an error (exit 1)"))
        self.assertEqual(heartbeat.trouble(self.brain), ["jinnee: last run failed (stopped with an error (exit 1))"])

    def test_a_run_that_takes_too_long(self):
        env = self.fake_claude("sleep 30\n")
        got = self.drive([["msg", OWNER, "do everything"]], agent="real", extra=env)
        self.assertIn("without finishing, so I stopped", got[0]["replies"][0])
        self.assertIn("What you can do", got[0]["replies"][0])
        self.assertEqual((self.beat()["state"], self.beat()["note"]), ("failed", "timed out after 10 minutes"))

    def test_claude_not_installed(self):
        got = self.drive([["msg", OWNER, "hello"]], agent="real", extra={"PATH": str(self.root / "nowhere")})
        self.assertIn("not installed", got[0]["replies"][0])
        self.assertEqual(self.beat()["state"], "failed")

    def test_a_failed_morning_brief_reaches_the_owner(self):
        env = self.fake_claude("exit 3\n")
        got = self.drive([["brief", 0, ""]], agent="real", extra=env)
        self.assertEqual(got[0]["sent"][0][0], OWNER)
        self.assertIn("could not finish", got[0]["sent"][0][1])

    def test_a_good_run_clears_the_failure_and_gets_the_guard_rails(self):
        env = self.fake_claude('printf "%s\\n" "$@" > args.txt\necho fine\n')
        a = self.add()
        self.book.decide(a, "ok")
        got = self.drive([["msg", OWNER, "hello"]], agent="real", extra=env)
        self.assertEqual(got[0]["replies"], ["fine"])
        self.assertEqual((self.beat()["state"], self.beat()["note"]), ("ok", "replied"))
        args = (self.root / "args.txt").read_text(encoding="utf-8")
        for needed in ("Bash(python3 core/approvals.py *)", "Bash(python3 core/autonomy.py *)", "Edit(brain/approvals.json)",
                       "Edit(brain/autonomy_config.json)", "--disallowedTools", "acceptEdits"):
            self.assertIn(needed, args)
        self.assertIn(f"DECIDED, take it now with `consume {a}`", args)  # the prompt carries what is open
        self.assertIn("spend_money: level 0 (forbidden, locked at max 0)", args)
        self.assertIn("Never write brain/approvals.json", args)
        self.assertNotIn("bypassPermissions", args)


if __name__ == "__main__":
    unittest.main()
