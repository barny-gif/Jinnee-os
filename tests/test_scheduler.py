"""The schedule (core/scheduler.py): times, daylight saving, quiet hours, once per slot across restarts, one at a time,
connectors that are missing, the owner's settings, and what a run may do. No network, no claude: the runner is a fake.
Run: python3 -m unittest discover tests"""
import datetime as dt, json, os, pathlib, subprocess, sys, tempfile, textwrap, time, unittest, zoneinfo
from unittest import mock
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import alerts, heartbeat, scheduler
from scheduler import parse, slots, Rule, SpecError

BUD = zoneinfo.ZoneInfo("Europe/Budapest")
UTC = dt.timezone.utc
CLEAN = ("JINNEE_TZ", "QUIET_HOURS", "JINNEE_BUSINESS_FILE", "PACKS", "JINNEE_UNATTENDED", "BILLINGO_API_KEY", "SZAMLAZZ_AGENT_KEY",
         "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_ACCOUNTS", "SHOPIFY_STORE", "SHOPIFY_ADMIN_TOKEN", "SHOPIFY_CLIENT_ID",
         "SHOPIFY_CLIENT_SECRET", "GLS_CLIENT_NUMBER", "GLS_USERNAME", "GLS_PASSWORD")


def at(y, m, d, h, mi=0, fold=0, tz=BUD):
    return dt.datetime(y, m, d, h, mi, fold=fold, tzinfo=tz).timestamp()


def local(t):
    return dt.datetime.fromtimestamp(t, BUD)


class Parse(unittest.TestCase):
    def test_what_can_be_written(self):
        cases = {"hourly": "hourly :00", "hourly :05": "hourly :05", "Hourly :35 workdays": "hourly :35 workdays",
                 "daily 8:00": "daily 08:00", "workdays 07:30": "workdays 07:30", "weekdays 07:30": "workdays 07:30",
                 "weekly fri 14:00": "weekly fri 14:00", "weekly Friday 14:00": "weekly fri 14:00", "weekly péntek 14:00": "weekly fri 14:00",
                 "weekly csutortok 10:00": "weekly thu 10:00", "weekly mon,thu 09:00": "weekly mon,thu 09:00"}
        for spec, text in cases.items():
            with self.subTest(spec=spec): self.assertEqual(parse(spec).text, text)
        for off in ("off", "OFF", " off ", "none", "never", False):
            with self.subTest(off=off): self.assertIsNone(parse(off))

    def test_what_is_refused_with_a_reason(self):
        for bad in ("", "hourly 5", "hourly :75", "daily 25:00", "daily 8", "daily", "weekly fun 10:00", "weekly fri", "every day",
                    "daily 08:00 extra", True, 8, None, ["daily 08:00"]):
            with self.subTest(bad=bad), self.assertRaises(SpecError) as e: parse(bad)
            self.assertTrue(str(e.exception))

    def test_interval_for_the_heartbeat(self):
        self.assertEqual(parse("hourly :05").every, 3600)
        self.assertEqual(parse("daily 08:00").every, 86400)
        self.assertEqual(parse("weekly fri 14:00").every, 7 * 86400)


class Slots(unittest.TestCase):
    def test_autumn_hourly_runs_in_both_real_hours(self):
        """2026-10-25: at 03:00 summer time the clocks go back to 02:00. 02:05 happens twice, an hour apart: two runs."""
        got = slots(parse("hourly :05"), at(2026, 10, 25, 0), at(2026, 10, 25, 5), BUD)
        self.assertEqual([local(t).strftime("%H:%M") for t, _ in got], ["00:05", "01:05", "02:05", "02:05", "03:05", "04:05"])
        twice = [t for t, _ in got if local(t).hour == 2]
        self.assertEqual(twice[1] - twice[0], 3600)
        self.assertEqual(len({k for _, k in got}), 6)  # every real hour has its own record key

    def test_autumn_daily_runs_once(self):
        got = slots(parse("daily 02:30"), at(2026, 10, 24, 12), at(2026, 10, 26, 12), BUD)
        self.assertEqual(len([1 for t, _ in got if local(t).date() == dt.date(2026, 10, 25)]), 1)
        t = [t for t, _ in got if local(t).date() == dt.date(2026, 10, 25)][0]
        self.assertEqual(dt.datetime.fromtimestamp(t, UTC).strftime("%H:%M"), "00:30")  # the first 02:30 (summer time)

    def test_spring_the_missing_hour(self):
        """2027-03-28: 02:00 does not exist (clocks jump to 03:00). Hourly skips it; a daily 02:30 runs once, at 03:30."""
        got = slots(parse("hourly :05"), at(2027, 3, 28, 0), at(2027, 3, 28, 5), BUD)
        self.assertEqual([local(t).strftime("%H:%M") for t, _ in got], ["00:05", "01:05", "03:05", "04:05"])
        daily = [t for t, _ in slots(parse("daily 02:30"), at(2027, 3, 27, 12), at(2027, 3, 29, 12), BUD)]
        day = [t for t in daily if local(t).date() == dt.date(2027, 3, 28)]
        self.assertEqual(len(day), 1); self.assertEqual(local(day[0]).strftime("%H:%M"), "03:30")

    def test_the_brief_stays_at_eight_across_the_change(self):
        got = [t for t, _ in slots(parse("daily 08:00"), at(2026, 10, 24, 0), at(2026, 10, 27, 0), BUD)]
        self.assertEqual([local(t).strftime("%m-%d %H:%M") for t in got], ["10-24 08:00", "10-25 08:00", "10-26 08:00"])
        self.assertEqual(got[1] - got[0], 25 * 3600)  # the night between is an hour longer

    def test_weekdays(self):
        week = (at(2026, 10, 26, 0), at(2026, 11, 2, 0))  # Monday to Monday
        self.assertEqual([local(t).strftime("%a") for t, _ in slots(parse("weekly fri 14:00"), *week, BUD)], ["Fri"])
        self.assertEqual(len(slots(parse("workdays 07:30"), *week, BUD)), 5)
        hourly = slots(parse("hourly :05 workdays"), *week, BUD)
        self.assertEqual({local(t).strftime("%a") for t, _ in hourly}, {"Mon", "Tue", "Wed", "Thu", "Fri"})
        self.assertEqual(len(hourly), 5 * 24)

    def test_a_zone_with_a_half_hour_offset(self):
        india = zoneinfo.ZoneInfo("Asia/Kolkata")
        got = slots(parse("hourly :05"), at(2026, 10, 23, 0, tz=india), at(2026, 10, 23, 3, tz=india), india)
        self.assertEqual([dt.datetime.fromtimestamp(t, india).strftime("%H:%M") for t, _ in got], ["00:05", "01:05", "02:05"])


class Fake:
    """Stands in for the bridge: busy or not, and what the agent run answers."""
    def __init__(self, answer=(True, "Two things for you.", "replied"), busy=False):
        self.answer, self._busy, self.calls = answer, busy, []
    def busy(self): return self._busy
    def run(self, job, prompt):
        self.calls.append((job.id, prompt))
        if isinstance(self.answer, Exception): raise self.answer
        return self.answer(job, prompt) if callable(self.answer) else self.answer


class Base(unittest.TestCase):
    packs = ["general"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.brain = self.root / "brain"; self.brain.mkdir()
        self.yaml = self.root / "business.yaml"
        env = {k: "" for k in CLEAN}
        env.update(JINNEE_TZ="Europe/Budapest", JINNEE_BUSINESS_FILE=str(self.yaml))
        self.env = mock.patch.dict(os.environ, env); self.env.start(); self.addCleanup(self.env.stop)
        for k in CLEAN:
            if os.environ.get(k) == "" and k not in ("JINNEE_TZ", "JINNEE_BUSINESS_FILE"): os.environ.pop(k)
        self.s = self.sched()

    def sched(self):
        return scheduler.Scheduler(self.brain, self.packs, root=REPO)

    def owner(self, text):
        self.yaml.write_text(textwrap.dedent(text), encoding="utf-8")
        st = self.yaml.stat(); os.utime(self.yaml, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))  # a new mtime: read again

    def job(self, jid, s=None):
        return next(j for j in (s or self.s).jobs() if j.id == jid)

    def due_ids(self, now, s=None):
        return [j.id for j, _, _ in (s or self.s).due(now)]

    def run_due(self, now, runner, s=None):
        s = s or self.s
        return [s.run(j, t, k, runner, now=now) for j, t, k in s.due(now)]

    def rec(self, jid):
        return json.loads((self.brain / "schedule_state.json").read_text())[jid]


class OncePerSlot(Base):
    def test_a_new_job_does_not_catch_up_on_what_was_before_it(self):
        self.assertEqual(self.due_ids(at(2026, 10, 23, 10)), [])          # installed at 10:00: today's 08:00 brief is not owed
        self.assertEqual(self.due_ids(at(2026, 10, 24, 7, 59)), [])
        self.assertEqual(self.due_ids(at(2026, 10, 24, 8, 0)), ["jinnee.brief"])

    def test_runs_once_and_a_restart_does_not_repeat_it(self):
        self.s.due(at(2026, 10, 23, 7))
        runner = Fake()
        out = self.run_due(at(2026, 10, 23, 8, 0), runner)
        self.assertEqual([o.result for o in out], ["ok"]); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.due_ids(at(2026, 10, 23, 8, 1)), [])
        restarted = self.sched()  # a new program reads the record from brain/
        self.assertEqual(self.due_ids(at(2026, 10, 23, 8, 2), restarted), [])
        self.assertEqual(self.due_ids(at(2026, 10, 23, 20, 0), restarted), ["jinnee.evening"])
        self.assertEqual(self.rec("jinnee.brief")["slot"], "2026-10-23 08:00")

    def test_a_second_claim_of_the_same_slot_is_refused(self):
        self.s.due(at(2026, 10, 23, 7))
        j, t, k = self.s.due(at(2026, 10, 23, 8))[0]
        self.assertTrue(self.s.claim(j, t, k, at(2026, 10, 23, 8)))
        self.assertFalse(self.sched().claim(j, t, k, at(2026, 10, 23, 8)))
        self.assertEqual(self.s.run(j, t, k, Fake(), now=at(2026, 10, 23, 8, 1)).result, "skipped")

    def test_autumn_night_with_no_quiet_hours(self):
        """The whole night of 2026-10-25 with quiet hours off: hourly twice in the repeated hour, daily once."""
        os.environ["QUIET_HOURS"] = "off"
        self.owner('schedule:\n  jobs:\n    jinnee.brief: "hourly :05"\n    jinnee.evening: "daily 02:30"\n')
        s = self.sched()
        s.due(at(2026, 10, 24, 23, 0))
        runner, t = Fake(), at(2026, 10, 24, 23, 0)
        while t < at(2026, 10, 25, 5, 0):
            for o in self.run_due(t, runner, s): self.assertEqual(o.result, "ok")
            t += 60
        hours = [p for jid, p in runner.calls if jid == "jinnee.brief"]
        self.assertEqual(len(hours), 7)  # 23:05, 00:05, 01:05, 02:05 summer, 02:05 winter, 03:05, 04:05 (6 h 55 min of real time)
        self.assertEqual(len([1 for jid, _ in runner.calls if jid == "jinnee.evening"]), 1)

    def test_spring_night_with_no_quiet_hours(self):
        os.environ["QUIET_HOURS"] = "off"
        self.owner('schedule:\n  jobs:\n    jinnee.evening: "daily 02:30"\n')
        s = self.sched(); s.due(at(2027, 3, 27, 23))
        runner, t, ran = Fake(), at(2027, 3, 27, 23), []
        while t < at(2027, 3, 28, 6):
            for o in self.run_due(t, runner, s): ran.append(t)
            t += 60
        evening = [t for t in ran if any(jid == "jinnee.evening" for jid, _ in runner.calls)]
        self.assertEqual(len([1 for jid, _ in runner.calls if jid == "jinnee.evening"]), 1)
        self.assertEqual(local(ran[-1]).strftime("%H:%M"), "03:30")


class CatchUpAndQuiet(Base):
    def test_a_morning_the_machine_was_off_is_caught_up_within_six_hours(self):
        self.s.due(at(2026, 10, 22, 10))
        self.assertEqual(self.due_ids(at(2026, 10, 23, 13, 59)), ["jinnee.brief"])  # back on at 13:59: still owed
        self.assertEqual(self.due_ids(at(2026, 10, 23, 14, 1)), [])                 # 14:01: missed, not run late
        row = next(r for r in self.s.rows(at(2026, 10, 23, 15)) if r["id"] == "jinnee.brief")
        self.assertEqual(row["result"], "missed today 08:00")

    def test_hourly_is_caught_up_within_45_minutes_only(self):
        self.owner('schedule:\n  jobs:\n    jinnee.brief: "hourly :05"\n')
        s = self.sched(); s.due(at(2026, 10, 23, 9))
        self.assertEqual(self.due_ids(at(2026, 10, 23, 10, 50), s), ["jinnee.brief"])
        self.assertEqual(self.due_ids(at(2026, 10, 23, 10, 51), s), [])

    def test_nothing_starts_in_the_quiet_hours(self):
        self.s.due(at(2026, 10, 22, 10))
        self.assertEqual(self.due_ids(at(2026, 10, 23, 20, 0)), ["jinnee.evening"])
        self.assertEqual(self.due_ids(at(2026, 10, 23, 21, 30)), [])  # 20:00 still owed, but now it is quiet: not started
        self.assertEqual(self.due_ids(at(2026, 10, 24, 7, 0)), [])    # and not at 07:00 either: that was yesterday's slot

    def test_a_time_inside_the_quiet_hours_never_runs_and_says_so(self):
        self.owner('schedule:\n  quiet_hours: "22:00-07:00"\n  jobs:\n    jinnee.evening: "daily 23:00"\n')
        s = self.sched(); s.due(at(2026, 10, 22, 10))
        self.assertEqual(self.due_ids(at(2026, 10, 23, 23, 0), s), [])
        row = next(r for r in s.rows(at(2026, 10, 23, 12)) if r["id"] == "jinnee.evening")
        self.assertFalse(row["on"]); self.assertIn("inside the quiet hours (22:00-07:00)", row["off"])
        self.assertIn("never: its time is inside the quiet hours", s.plan(at(2026, 10, 23, 12)))

    def test_quiet_hours_from_the_business_file_and_from_env(self):
        self.owner('schedule:\n  quiet_hours: "19:00-07:00"\n')
        s = self.sched(); s.due(at(2026, 10, 22, 10))
        self.assertEqual(self.due_ids(at(2026, 10, 23, 20, 0), s), [])  # 20:00 is quiet now
        self.owner('schedule:\n  quiet_hours: off\n')                     # YAML reads this as false: never quiet
        self.assertFalse(alerts.quiet(at(2026, 10, 23, 3, 0)))
        os.environ["QUIET_HOURS"] = "21:00-07:00"                         # .env wins over the file
        self.assertTrue(alerts.quiet(at(2026, 10, 23, 3, 0)))
        self.assertIn("both set", " ".join(self.sched().check()))


class OneAtATime(Base):
    def test_a_job_does_not_start_while_the_owners_run_is_going(self):
        self.s.due(at(2026, 10, 22, 10))
        busy = Fake(busy=True)
        self.assertEqual([o.result for o in self.run_due(at(2026, 10, 23, 8), busy)], ["busy"])
        self.assertEqual(busy.calls, [])
        self.assertEqual(self.due_ids(at(2026, 10, 23, 8, 1)), ["jinnee.brief"])  # nothing was taken: next minute again
        self.assertEqual([o.result for o in self.run_due(at(2026, 10, 23, 8, 1), Fake())], ["ok"])

    def test_another_program_holding_the_lock(self):
        self.s.due(at(2026, 10, 22, 10))
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, sys, time
            f = open({str(self.brain / '.schedule.lock')!r}, 'a+b'); fcntl.flock(f, fcntl.LOCK_EX)
            print('held', flush=True); time.sleep(30)""")], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        runner = Fake()
        self.assertEqual([o.result for o in self.run_due(at(2026, 10, 23, 8), runner)], ["busy"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.s.recover(at(2026, 10, 23, 8)), [])  # it may be running there: not marked failed
        holder.kill(); holder.wait()
        self.assertEqual([o.result for o in self.run_due(at(2026, 10, 23, 8, 1), runner)], ["ok"])

    def test_a_run_cut_off_by_a_restart_is_recorded_as_failed(self):
        self.s.due(at(2026, 10, 22, 10))
        j, t, k = self.s.due(at(2026, 10, 23, 8))[0]
        self.s.claim(j, t, k, at(2026, 10, 23, 8))  # ... and the machine went down here
        restarted = self.sched()
        self.assertEqual(restarted.recover(at(2026, 10, 23, 8, 5)), ["jinnee.brief"])
        self.assertEqual(self.rec("jinnee.brief")["result"], "failed")
        self.assertIn("stopped during this run", self.rec("jinnee.brief")["note"])
        self.assertEqual(heartbeat.load(self.brain)["jinnee"]["state"], "failed")
        self.assertEqual(self.due_ids(at(2026, 10, 23, 8, 6), restarted), [])  # and it is not run a second time


class Results(Base):
    def setUp(self):
        super().setUp()
        os.environ.update(GMAIL_CLIENT_ID="cid", GMAIL_CLIENT_SECRET="sec", GMAIL_ACCOUNTS="info")
        self.mail = mock.patch.dict(scheduler.PRECHECKS, new_mail=lambda since, root: (False, "no new mail"))
        self.mail.start(); self.addCleanup(self.mail.stop)  # no test reaches Gmail
        self.s = self.sched()
        self.s.due(at(2026, 10, 23, 10))  # first seen at 10:00: the 10:05 inbox slot is the first one owed

    def by_job(self, now, runner):
        return {j.id: self.s.run(j, t, k, runner, now=now) for j, t, k in self.s.due(now)}

    def test_silent_sends_nothing(self):
        out = self.by_job(at(2026, 10, 23, 20, 0), Fake((True, "SILENT", "replied")))
        self.assertEqual((out["jinnee.evening"].result, out["jinnee.evening"].message), ("silent", ""))
        self.assertEqual(heartbeat.load(self.brain)["jinnee"]["state"], "ok")

    def test_an_agent_run_is_labelled_and_a_lead_run_is_not(self):
        scheduler.PRECHECKS["new_mail"] = lambda since, root: (True, "1 new e-mail(s)")
        out = self.by_job(at(2026, 10, 23, 10, 5), Fake((True, "Supplier invoice in, draft ready.", "replied")))
        self.assertEqual(list(out), ["office.inbox"])
        self.assertEqual(out["office.inbox"].message, "Milo: the owner's inbox (scheduled):\nSupplier invoice in, draft ready.")
        brief = self.by_job(at(2026, 10, 24, 8, 0), Fake((True, "1. Two orders.", "replied")))
        self.assertEqual(brief["jinnee.brief"].message, "1. Two orders.")

    def test_nothing_new_means_no_agent_run(self):
        seen = []
        def none(since, root): seen.append(since); return False, "no new mail"
        scheduler.PRECHECKS["new_mail"] = none
        runner = Fake()
        out = self.by_job(at(2026, 10, 23, 10, 5), runner)
        self.assertEqual({k: o.result for k, o in out.items()}, {"office.inbox": "nothing"}); self.assertEqual(runner.calls, [])
        self.assertEqual(seen, [at(2026, 10, 23, 9, 5)])  # first time: one interval back
        self.assertEqual((heartbeat.load(self.brain)["office"]["state"], self.rec("office.inbox")["note"]), ("ok", "no new mail"))
        self.by_job(at(2026, 10, 23, 11, 5), runner)
        self.assertEqual(seen[-1], at(2026, 10, 23, 10, 5))  # then: since the last good run started

    def test_a_failed_precheck_is_a_failed_run_told_once(self):
        def broken(since, root): raise RuntimeError("[info login] Google refused the refresh token (invalid_grant)")
        scheduler.PRECHECKS["new_mail"] = broken
        runner = Fake()
        out = self.by_job(at(2026, 10, 23, 10, 5), runner)
        self.assertEqual(out["office.inbox"].result, "failed"); self.assertEqual(runner.calls, [])
        self.assertEqual(heartbeat.load(self.brain)["office"]["state"], "failed")
        probs = self.s.problems()
        self.assertEqual(len(probs), 1); self.assertIn("invalid_grant", probs[0])
        jobs = self.s.jobs()
        watch = alerts.Watch(self.brain)
        with mock.patch.object(scheduler.Scheduler, "jobs", lambda self_: jobs), mock.patch.dict(os.environ, PACKS="general"):
            plan = watch.due(at(2026, 10, 23, 10, 6)); watch.done(plan)
            self.assertEqual(sum("invalid_grant" in m for m in plan["messages"]), 1)
            again = watch.due(at(2026, 10, 23, 10, 7)); watch.done(again)
            self.assertFalse(any("invalid_grant" in m for m in again["messages"]))  # the same failure is not repeated
        scheduler.PRECHECKS["new_mail"] = lambda since, root: (False, "no new mail")
        self.by_job(at(2026, 10, 23, 11, 5), runner)
        self.assertEqual(self.s.problems(), [])  # a later good run clears it

    def test_a_failed_agent_run_goes_to_the_alerts_not_straight_to_the_owner(self):
        scheduler.PRECHECKS["new_mail"] = lambda since, root: (True, "2 new")
        out = self.by_job(at(2026, 10, 23, 10, 5), Fake((False, "I worked on this for 10 minutes…", "timed out after 10 minutes")))
        self.assertEqual((out["office.inbox"].result, out["office.inbox"].message), ("failed", ""))
        self.assertEqual(heartbeat.trouble(self.brain, at(2026, 10, 23, 10, 6)),
                         ["office: last run failed (scheduled office.inbox: timed out after 10 minutes)"])

    def test_a_failed_lead_run_reaches_the_owner_directly(self):
        out = self.by_job(at(2026, 10, 24, 8, 0), Fake((False, "I could not finish this: …", "stopped with an error (exit 3)")))
        self.assertEqual((out["jinnee.brief"].result, out["jinnee.brief"].message), ("failed", "I could not finish this: …"))
        self.assertFalse(any("morning brief" in p for p in self.s.problems()))  # not a second time through the alerts

    def test_a_runner_that_blows_up(self):
        out = self.by_job(at(2026, 10, 24, 8, 0), Fake(OSError("no")))
        self.assertEqual(out["jinnee.brief"].result, "failed"); self.assertIn("could not start", self.rec("jinnee.brief")["note"])


class Needs(Base):
    packs = ["general", "ecom"]

    def test_without_connectors_only_the_lead_runs(self):
        jobs = {j.id: j for j in self.s.jobs()}
        self.assertEqual(sorted(j for j, x in jobs.items() if not x.off), ["jinnee.brief", "jinnee.evening"])
        self.assertIn("GMAIL_CLIENT_ID", jobs["office.inbox"].off)
        self.assertIn("BILLINGO_API_KEY", jobs["ledger.invoice_watch"].off)
        self.assertIn("Számlázz.hu: no connector code yet", jobs["ledger.invoice_watch"].off)
        self.assertIn("SHOPIFY_STORE", jobs["ops.order_watch"].off)
        self.assertIn("competitors", jobs["watcher.weekly_round"].off)
        self.assertIn("none of the jobs it sums up runs", jobs["jinnee.weekly_report"].off)
        self.assertIn("none of the jobs it sums up runs", jobs["social.weekly_calendar"].off)

    def test_a_szamlazz_key_alone_is_not_enough(self):
        os.environ["SZAMLAZZ_AGENT_KEY"] = "k"
        self.assertIn("no connector code yet", self.job("ledger.invoice_watch").off)

    def test_connected_jobs_run(self):
        os.environ.update(SHOPIFY_STORE="demo", SHOPIFY_CLIENT_ID="i", SHOPIFY_CLIENT_SECRET="s", BILLINGO_API_KEY="k",
                          GMAIL_CLIENT_ID="cid", GMAIL_CLIENT_SECRET="sec", GMAIL_ACCOUNTS="info")
        (self.brain / "watch").mkdir(); (self.brain / "watch" / "competitors.md").write_text("# competitors\n- example-rival.hu\n")
        off = {j.id: j.off for j in self.s.jobs() if j.off}
        self.assertEqual(off, {})
        os.environ.pop("SHOPIFY_CLIENT_SECRET")  # one of the two Shopify logins half set: not ready
        self.assertIn("SHOPIFY_ADMIN_TOKEN or SHOPIFY_CLIENT_ID+SHOPIFY_CLIENT_SECRET", self.job("ops.order_watch").off)

    def test_an_empty_competitor_list(self):
        (self.brain / "watch").mkdir(); (self.brain / "watch" / "competitors.md").write_text("# competitors\n\n")
        self.assertIn("lists no competitor", self.job("watcher.weekly_round").off)

    def test_every_promised_rhythm_is_declared(self):
        """The rhythms the agent files promise (hourly, daily, Monday, Thursday, Friday) all have a job."""
        declared, _ = self.s.declared()
        promised = {"office.inbox": ("office", "hourly"), "echo.customer_mail": ("echo", "hourly"), "ledger.invoice_watch": ("ledger", "daily"),
                    "ledger.weekly_finance": ("ledger", "weekly fri"), "watcher.weekly_round": ("watcher", "weekly thu"),
                    "ops.order_watch": ("ops", "hourly"), "ops.stock_alert": ("ops", "daily"), "marketing.weekly_plan": ("marketing-manager", "weekly mon"),
                    "marketing.weekly_measurement": ("marketing-manager", "weekly fri"), "social.weekly_calendar": ("social-media-manager", "weekly"),
                    "jinnee.brief": ("jinnee", "daily 08:00"), "jinnee.evening": ("jinnee", "daily 20:00"), "jinnee.weekly_report": ("jinnee", "weekly fri")}
        for jid, (agent, when) in promised.items():
            with self.subTest(job=jid):
                self.assertEqual(declared[jid]["agent"], agent); self.assertTrue(declared[jid]["when"].startswith(when))
                parse(declared[jid]["when"])
                if agent != "jinnee":
                    pack = declared[jid]["pack"]
                    self.assertTrue((REPO / "packs" / pack / "agents" / f"{agent}.md").exists())
        self.assertEqual(set(declared), set(promised))
        self.assertIn("Owner's inbox triage** (hourly)", (REPO / "packs/general/agents/office.md").read_text())
        self.assertIn("Weekly round** (Thursday)", (REPO / "packs/general/agents/watcher.md").read_text())

    def test_hourly_jobs_do_not_start_at_the_same_minute(self):
        hourly = [self.job(j).rule.minute for j in ("office.inbox", "echo.customer_mail", "ops.order_watch")]
        self.assertEqual(len(set(hourly)), 3)


class OwnerSettings(Base):
    def test_off_another_time_and_on(self):
        self.owner("""\
            schedule:
              jobs:
                jinnee.evening: off
                jinnee.brief: "daily 07:15"
            """)
        s = self.sched()
        self.assertEqual(self.job("jinnee.evening", s).off, "switched off in config/business.yaml")
        b = self.job("jinnee.brief", s)
        self.assertEqual((b.rule.text, b.source, b.off), ("daily 07:15", "owner", ""))
        self.owner("schedule:\n  jobs:\n    jinnee.evening: on\n")
        self.assertEqual(self.job("jinnee.evening", self.sched()).rule.text, "daily 20:00")

    def test_a_bad_setting_stops_that_job_and_is_reported(self):
        self.owner('schedule:\n  jobs:\n    jinnee.brief: "every morning"\n    jinne.brief: "daily 08:00"\n  colour: blue\n')
        s = self.sched()
        self.assertIn("not understood", self.job("jinnee.brief", s).off)
        probs = s.check()
        self.assertTrue(any("jinnee.brief" in p and "not understood" in p for p in probs))
        self.assertTrue(any("no job 'jinne.brief'" in p for p in probs))
        self.assertTrue(any("'colour' is not a schedule setting" in p for p in probs))
        self.assertTrue(s.problems())  # told to the owner once, through the alerts

    def test_everything_off(self):
        self.owner("schedule:\n  enabled: false\n")
        self.assertTrue(all("schedule.enabled: false" in j.off for j in self.sched().jobs()))
        self.assertEqual(self.due_ids(at(2026, 10, 24, 8), self.sched()), [])

    def test_time_zone_from_the_business_file(self):
        os.environ["JINNEE_TZ"] = ""
        self.owner('schedule:\n  timezone: "Europe/Lisbon"\n')
        self.assertEqual(alerts.zone_name(), "Europe/Lisbon")
        self.assertEqual(str(alerts.zone()), "Europe/Lisbon")
        s = self.sched(); s.due(at(2026, 10, 22, 10))
        lisbon = zoneinfo.ZoneInfo("Europe/Lisbon")
        self.assertEqual(self.due_ids(at(2026, 10, 23, 8, 0, tz=lisbon), s), ["jinnee.brief"])  # 08:00 there, 09:00 in Budapest
        os.environ["JINNEE_TZ"] = "Europe/Budapest"  # .env wins, and check says the two differ
        self.assertEqual(alerts.zone_name(), "Europe/Budapest")
        self.assertTrue(any("differs" in p for p in self.sched().check()))
        self.owner('schedule:\n  timezone: "Mars/Olympus"\n')
        self.assertTrue(any("not a time zone" in p for p in self.sched().check()))

    def test_a_zone_that_cannot_be_loaded_is_said_loudly(self):
        os.environ["JINNEE_TZ"] = "Europe/Nowhere"
        s = self.sched()
        self.assertTrue(any("cannot be loaded" in p and "tzdata" in p for p in s.check()))
        self.assertIn("NOT LOADED", s.plan(at(2026, 10, 23, 12)))
        self.assertTrue(any("cannot be loaded" in p for p in s.problems()))  # the owner is told, once

    def test_no_file_no_problem(self):
        self.assertEqual(self.s.check(), [])
        self.assertIn("jinnee.brief", self.s.plan(at(2026, 10, 23, 12)))

    def test_example_file_settings_are_valid(self):
        """config/business.example.yaml documents the schedule; every job id and time in it must be real."""
        os.environ["JINNEE_BUSINESS_FILE"] = str(REPO / "config" / "business.example.yaml")
        s = scheduler.Scheduler(self.brain, ["general", "ecom"], root=REPO)
        self.assertEqual(s.check(), [])
        self.assertIsInstance(alerts.setting("schedule.jobs"), dict)


class WhatARunMaySee(Base):
    def test_the_prompt_says_nothing_goes_out(self):
        os.environ.update(GMAIL_CLIENT_ID="cid", GMAIL_CLIENT_SECRET="sec", GMAIL_ACCOUNTS="info")
        s = self.sched()
        p = s.prompt(self.job("office.inbox", s), at(2026, 10, 23, 10, 5), at(2026, 10, 23, 9, 5), BUD)
        for needed in ("Fri 2026-10-23 10:05", "packs/general/agents/office.md", "since Fri 2026-10-23 09:05", "Nothing goes out",
                       "core/approvals.py add", "Do not consume", "Write only under brain/ and handoffs/", "SILENT"):
            self.assertIn(needed, p)
        lead = s.prompt(self.job("jinnee.brief", s), at(2026, 10, 23, 8), at(2026, 10, 22, 8), BUD)
        self.assertTrue(lead.startswith(scheduler.BRIEF)); self.assertIn("Nothing goes out", lead)

    def test_a_program_file_changed_during_the_run_fails_it(self):
        root = self.root / "install"
        for d in ("core", "connectors", "packs"):
            import shutil; shutil.copytree(REPO / d, root / d, ignore=shutil.ignore_patterns("__pycache__"))
        s = scheduler.Scheduler(self.brain, ["general"], root=root)
        s.due(at(2026, 10, 22, 10))
        def tamper(job, prompt):
            (root / "connectors" / "cbase.py").write_text("# gone\n"); return True, "done", "replied"
        out = [s.run(j, t, k, Fake(tamper), now=at(2026, 10, 23, 8)) for j, t, k in s.due(at(2026, 10, 23, 8))]
        self.assertEqual(out[0].result, "failed"); self.assertIn("program file changed", self.rec("jinnee.brief")["note"])
        before = s.fingerprint()
        (self.brain / "notes.md").write_text("brain/ is allowed")
        (root / "handoffs").mkdir(); (root / "handoffs" / "x.md").write_text("so is handoffs/")
        self.assertEqual(s.fingerprint(), before)


class QuietOutbox(Base):
    def test_a_message_that_finished_in_the_quiet_hours_waits_for_the_morning(self):
        self.s.later("Milo: order watch (scheduled):\n#1001 has no label yet.")
        self.assertEqual(self.s.outbox(at(2026, 10, 23, 23, 0)), [])      # still quiet: kept
        self.assertEqual(self.s.outbox(at(2026, 10, 24, 7, 1)), ["Milo: order watch (scheduled):\n#1001 has no label yet."])
        self.assertEqual(self.s.outbox(at(2026, 10, 24, 7, 2)), [])       # delivered once

    def test_a_broken_outbox_file_is_not_fatal(self):
        (self.brain / scheduler.OUTBOX).write_text("{half")
        self.assertEqual(self.s.outbox(at(2026, 10, 24, 9)), [])
        self.s.later("x"); self.assertEqual(self.s.outbox(at(2026, 10, 24, 9)), ["x"])


class Heartbeat(Base):
    def test_an_agent_that_should_have_run_and_did_not(self):
        os.environ.update(GMAIL_CLIENT_ID="cid", GMAIL_CLIENT_SECRET="sec", GMAIL_ACCOUNTS="info")
        s = self.sched(); s.due(at(2026, 10, 23, 9))
        exp = s.expected(at(2026, 10, 23, 11, 0))
        self.assertEqual((exp["office"]["at"], exp["office"]["when"], exp["office"]["every"]), ("Fri 10:05", "hourly :05", 3600))
        self.assertEqual(heartbeat.trouble(self.brain, at(2026, 10, 23, 11, 0), exp),
                         ["echo: should have run at Fri 09:20 (hourly :20) and did not",
                          "office: should have run at Fri 10:05 (hourly :05) and did not"])
        with mock.patch("time.time", return_value=at(2026, 10, 23, 10, 6)):
            heartbeat.beat("office", "scheduled office.inbox: no new mail", brain=self.brain)
        self.assertEqual(heartbeat.trouble(self.brain, at(2026, 10, 23, 11, 0), exp), ["echo: should have run at Fri 09:20 (hourly :20) and did not"])
        self.assertEqual(heartbeat.trouble(self.brain, at(2026, 10, 23, 11, 0)), [])  # without the schedule: the old answer

    def test_nothing_is_expected_inside_the_quiet_hours_or_before_it_existed(self):
        s = self.sched(); s.due(at(2026, 10, 23, 9))
        self.assertNotIn("jinnee", s.expected(at(2026, 10, 23, 13)))       # first seen after this morning's 08:00
        self.assertEqual(s.expected(at(2026, 10, 24, 15))["jinnee"]["at"], "Sat 08:00")
        self.assertEqual(s.expected(at(2026, 10, 25, 3))["jinnee"]["at"], "Sat 20:00")  # the night adds nothing


class CommandLine(Base):
    def test_plan_status_and_check_start_nothing(self):
        self.repo_state_before = (REPO / "brain" / "schedule_state.json").exists()
        env = {**os.environ, "PACKS": "general,ecom", "PYTHONDONTWRITEBYTECODE": "1"}
        for cmd in (["plan"], ["plan", "--days", "2"], ["status"], ["check"]):
            with self.subTest(cmd=cmd):
                r = subprocess.run([sys.executable, str(REPO / "core" / "scheduler.py"), *cmd], capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, str(REPO / "core" / "scheduler.py"), "plan"], capture_output=True, text=True, env=env, timeout=60)
        self.assertIn("ON   jinnee.brief", r.stdout); self.assertIn("OFF  ops.order_watch", r.stdout)
        self.assertFalse((REPO / "brain" / "schedule_state.json").exists() and not self.repo_state_before)  # nothing written
        self.owner('schedule:\n  jobs:\n    jinnee.brief: "at dawn"\n')
        r = subprocess.run([sys.executable, str(REPO / "core" / "scheduler.py"), "check"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 1); self.assertIn("not understood", r.stdout)


if __name__ == "__main__":
    unittest.main()
