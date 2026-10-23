"""The schedule: when each agent works without being asked. The Telegram bridge (core/jinnee.py) asks it once a minute
what is due and runs that; this file decides, keeps the record and never sends anything itself.

Who sets what
  core (this file)                   the lead's own rhythm: morning brief, evening close, Friday report
  packs/<pack>/pack.json "schedule"  the rhythm the agent files promise ("Owner's inbox triage (hourly)"), one entry per job:
      "office.inbox": {"agent": "office", "name": "Milo: the owner's inbox", "when": "hourly :05", "needs": ["email"],
                       "precheck": "new_mail", "task": "..."}
  config/business.yaml "schedule"    the owner: another time or off per job, the time zone, the quiet hours, all off

  when:  hourly [:MM] [workdays] · daily HH:MM · workdays HH:MM · weekly DAY[,DAY] HH:MM · off
         DAY: mon tue wed thu fri sat sun (the full English or Hungarian name works too: friday, péntek)
  needs: email · invoicing · shop · shipping (a connector of that kind with its code and its .env settings) ·
         competitors (brain/watch/competitors.md lists them). A job whose need is missing does not run; it shows as off and why.
  after_any: [job ids]: the job only sums up or builds on those; it is off when none of them runs.
  precheck: new_mail · open_orders · marketing_plan. Plain code asks first whether there is anything to do (counts only, no
         personal data); when there is not, no agent run is started and the owner's subscription is not spent on it.

Rules
  - Times are the owner's (alerts.zone(): JINNEE_TZ, else schedule.timezone, else the machine's clock).
  - Daylight saving: an hourly job runs once in every real hour (so twice in the hour the clocks go back, and the hour that
    does not exist in spring is skipped); a daily or weekly job runs once on its day, also on the days the clocks change
    (02:30 on the spring day runs at 03:30).
  - Nothing starts in the quiet hours (alerts.quiet()). A fixed time inside them never runs, and `plan` says so.
  - Missed (machine off, a long run): an hourly job is caught up within 45 minutes, a daily or weekly one within 6 hours.
    A job seen for the first time does not catch up on what was due before it existed (an update does not set off a flood).
  - Once per slot: the slot is taken in brain/schedule_state.json before the run starts, so a restart never runs it twice.
    A run cut off by a restart or a crash is recorded as failed when the bridge starts again.
  - One at a time: brain/.schedule.lock across programs; the bridge never makes the owner wait behind a scheduled run.
  - The run itself is unattended (JINNEE_UNATTENDED=1): nothing goes out from it (connectors refuse --confirm yes and
    --send yes), decisions are not carried out (approvals.py consume refuses), only brain/ and handoffs/ can be written.
    What should go out is queued for the owner. If a program file changes during the run anyway, the run counts as failed.
  - A failed run: the heartbeat says failed and the owner is told once, through core/alerts.py.

CLI (none of these starts a run):
  python3 core/scheduler.py plan [--days 7]   what would run when, and what is off and why
  python3 core/scheduler.py status            each job: next run, last run, result
  python3 core/scheduler.py check             the owner's schedule settings: ok (exit 0), or what is wrong (exit 1)
"""
import contextlib, datetime as dt, hashlib, importlib.util, json, os, pathlib, re, sys, time
from typing import NamedTuple
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store, alerts, heartbeat
from pack_loader import resolve

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONNECTORS = ROOT / "connectors"
STATE = "schedule_state.json"
OUTBOX = "schedule_outbox.json"
LOCK = ".schedule.lock"
SILENT = "SILENT"
GRACE = {"hourly": 45 * 60, "daily": 6 * 3600, "weekly": 6 * 3600}
LOOKBACK = 8 * 86400  # far enough back to find the last slot of a weekly job
DAY = 86400
WEEK = tuple(range(7))
WORKDAYS = (0, 1, 2, 3, 4)
DAY_NAMES = {}
for _i, _names in enumerate((("mon", "monday", "hétfő", "hetfo"), ("tue", "tuesday", "kedd"), ("wed", "wednesday", "szerda"),
                             ("thu", "thursday", "csütörtök", "csutortok"), ("fri", "friday", "péntek", "pentek"),
                             ("sat", "saturday", "szombat"), ("sun", "sunday", "vasárnap", "vasarnap"))):
    for _n in _names: DAY_NAMES[_n] = _i
SHORT = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
GRAMMAR = "hourly [:MM] [workdays] · daily HH:MM · workdays HH:MM · weekly DAY HH:MM · off"
# What a scheduled run may not change: the program itself. Hashed before and after every run.
PROGRAM_DIRS = ("core", "connectors", "packs", "dashboard", "tools", "config", "registry", ".githooks")
PROGRAM_FILES = ("run.sh", "install.sh", "install.ps1", "Dockerfile", "docker-compose.yml", "requirements.txt", ".env")

BRIEF = ("Write the 5-line morning brief from brain/ and handoffs/, and also save it to brain/brief_today.md. "
         "Include what is waiting for the owner's decision (see 'approvals right now').")
EVENING = ("Evening close. Look at what happened since this morning's brief (brain/decisions.log.md, handoffs/, the approvals "
           "right now) and what is due tomorrow. If something is worth the owner's attention tonight, say it in at most 4 lines. "
           f"If nothing is, reply with exactly {SILENT} and nothing else.")
WEEKLY = ("The Friday weekly report for the owner, from this week's handoffs: Ledger's weekly finance line, Muse's weekly "
          "measurement (if the shop pack is installed), Scout's 5-line watch summary, and what is still waiting for the owner. "
          "At most 10 lines. Numbers exactly as they are in the handoffs; a part that is missing is named as missing, never "
          "estimated. Save it to handoffs/jinnee/<date>-weekly-report.md too.")
CORE_JOBS = {
    "jinnee.brief": {"agent": "jinnee", "name": "Jinnee: morning brief", "when": "daily 08:00", "lead": True, "task": BRIEF},
    "jinnee.evening": {"agent": "jinnee", "name": "Jinnee: evening close", "when": "daily 20:00", "lead": True, "task": EVENING},
    "jinnee.weekly_report": {"agent": "jinnee", "name": "Jinnee: Friday report", "when": "weekly fri 15:30", "lead": True, "task": WEEKLY,
                             "after_any": ["ledger.weekly_finance", "marketing.weekly_measurement", "watcher.weekly_round"]},
}
UNATTENDED_RULES = (
    "This run was started by the schedule, and the owner is not watching it. Nothing goes out from it: no e-mail, post, invoice, "
    "parcel label or shop change, whatever the autonomy level says (the connectors refuse --confirm yes and --send yes here). "
    "What should go out is queued for the owner with core/approvals.py add: finished, exactly as it would go out. Do not consume "
    "approvals: decided items reach the lead on their own. Write only under brain/ and handoffs/. The schedule records the "
    "heartbeat for this run.")
REPLY = ("Your reply goes to the owner on Telegram as it is: at most 5 short lines, no personal data (order numbers and first "
         f"names at most). If nothing needs the owner, reply with exactly {SILENT} and nothing else.")


class SpecError(ValueError):
    """A `when` that cannot be read. The message says what is wrong in words."""


class Rule(NamedTuple):
    kind: str      # hourly | daily | weekly
    hour: int      # -1 for hourly
    minute: int
    days: tuple    # weekdays it runs on, 0 = Monday

    @property
    def text(self):
        if self.kind == "hourly": return f"hourly :{self.minute:02d}" + (" workdays" if self.days == WORKDAYS else "")
        at = f"{self.hour:02d}:{self.minute:02d}"
        if self.days == WEEK: return f"daily {at}"
        if self.days == WORKDAYS: return f"workdays {at}"
        return f"weekly {','.join(SHORT[d] for d in self.days)} {at}"

    @property
    def every(self):
        """Seconds between runs on average: the heartbeat's expected interval."""
        return 3600 if self.kind == "hourly" else 7 * DAY // len(self.days)


def _clock(word):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", word)
    if not m: raise SpecError(f"'{word}' is not a time; write it as HH:MM, like 08:00")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59: raise SpecError(f"'{word}' is not a time of day")
    return h, mi


def parse(spec):
    """→ Rule, or None for off. YAML reads a bare `off` as false, so False is off too."""
    if spec is False: return None
    if not isinstance(spec, str): raise SpecError(f"expected a text like 'daily 08:00' ({GRAMMAR})")
    words = spec.strip().lower().split()
    if not words: raise SpecError(f"empty; write one of: {GRAMMAR}")
    head, rest = words[0], words[1:]
    if head in ("off", "none", "never", "no") and not rest: return None
    if head == "hourly":
        minute, days = 0, WEEK
        for w in rest:
            if re.fullmatch(r":\d{2}", w) and int(w[1:]) <= 59: minute = int(w[1:])
            elif w in ("workdays", "weekdays"): days = WORKDAYS
            else: raise SpecError(f"'{w}' is not understood after hourly; write e.g. 'hourly :05' or 'hourly :05 workdays'")
        return Rule("hourly", -1, minute, days)
    if head in ("daily", "workdays", "weekdays"):
        if len(rest) != 1: raise SpecError(f"write '{head} HH:MM', like '{head} 08:00'")
        h, mi = _clock(rest[0])
        return Rule("daily", h, mi, WEEK if head == "daily" else WORKDAYS)
    if head == "weekly":
        if len(rest) != 2: raise SpecError("write 'weekly DAY HH:MM', like 'weekly fri 14:00'")
        days = set()
        for d in rest[0].split(","):
            if d not in DAY_NAMES: raise SpecError(f"'{d}' is not a day; use mon, tue, wed, thu, fri, sat or sun")
            days.add(DAY_NAMES[d])
        h, mi = _clock(rest[1])
        return Rule("weekly", h, mi, tuple(sorted(days)))
    raise SpecError(f"'{spec.strip()}' is not understood; write one of: {GRAMMAR}")


# --- time: the owner's wall clock ↔ real instants (epoch seconds)

def _local(t, tz):
    return dt.datetime.fromtimestamp(t, tz)  # tz None: this machine's clock


def _instant(naive, tz):
    """The real moment of a wall-clock time. In the hour that does not exist (spring) it is pushed on by the gap; in the
    hour that happens twice (autumn) it is the first one."""
    return (naive.replace(tzinfo=tz) if tz is not None else naive).timestamp()


def slots(rule, start, end, tz):
    """Every moment the rule names with start <= t < end → [(t, key)], in order. The key names the slot for the record."""
    out = {}
    if rule.kind == "hourly":
        h = int(start // 3600) * 3600 - 3600
        while h < end + 3600:
            c = h + ((rule.minute - _local(h, tz).minute) % 60) * 60  # the zone's offset may not be whole hours
            lc = _local(c, tz)
            if start <= c < end and lc.minute == rule.minute and lc.weekday() in rule.days:
                out[c] = "hour " + dt.datetime.fromtimestamp(c, dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            h += 3600
    else:
        d, last = _local(start, tz).date() - dt.timedelta(days=1), _local(end, tz).date() + dt.timedelta(days=1)
        while d <= last:
            if d.weekday() in rule.days:
                t = _instant(dt.datetime.combine(d, dt.time(rule.hour, rule.minute)), tz)
                if start <= t < end: out[t] = f"{d.isoformat()} {rule.hour:02d}:{rule.minute:02d}"
            d += dt.timedelta(days=1)
    return sorted(out.items())


def when_text(t, now=None, tz=None):
    """A moment for people: 'today 08:00', 'tomorrow 08:00', 'Fri 14:00', '2026-11-02 09:30'."""
    if not t: return "never"
    lt, ln = _local(t, tz), _local(now or time.time(), tz)
    days = (lt.date() - ln.date()).days
    hm = lt.strftime("%H:%M")
    if days == 0: return f"today {hm}"
    if days == 1: return f"tomorrow {hm}"
    if days == -1: return f"yesterday {hm}"
    if -6 <= days <= 6: return f"{SHORT[lt.weekday()].capitalize()} {hm}"
    return lt.strftime("%Y-%m-%d %H:%M")


# --- what a job needs

def _loaded(rel, cache={}):
    """A connector module, loaded once (its own login cache is kept between runs)."""
    if rel not in cache:
        spec = importlib.util.spec_from_file_location("sched_" + re.sub(r"\W", "_", rel), CONNECTORS / rel)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); cache[rel] = m
    return cache[rel]


def connector_ready(category):
    """→ (ready, which connector or why not). Ready = a connector of this kind has its code (connectors/<id>/ops.py) and
    every .env setting its manifest names. The same rule as the dashboard's Connections box, plus the code."""
    why = []
    for mf in sorted(CONNECTORS.glob("*.manifest.json")):
        try: d = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        if d.get("category") != category: continue
        cid = d.get("id") or mf.name.split(".")[0]
        auth = d.get("auth") or {}
        envs = auth.get("env") or []
        envs = [envs] if isinstance(envs, str) else list(envs)
        missing = [e for e in envs if not os.getenv(e, "").strip()]
        groups = auth.get("env_any") or []
        if groups and not any(all(os.getenv(e, "").strip() for e in g) for g in groups):
            missing.append(" or ".join("+".join(g) for g in groups))
        code = (CONNECTORS / cid / "ops.py").exists()
        if code and not missing: return True, cid
        why.append(f"{d.get('name') or cid}: " + ("no connector code yet" if not code else "set " + ", ".join(missing) + " in .env"))
    return False, "; ".join(why) or f"no {category} connector is installed"


def competitors_ready(brain):
    p = pathlib.Path(brain) / "watch" / "competitors.md"
    try: lines = p.read_text(encoding="utf-8").splitlines()
    except OSError: return False, "the owner has not named competitors yet (brain/watch/competitors.md, from the onboarding)"
    names = [l for l in lines if l.strip() and not l.lstrip().startswith("#")]
    return (True, f"{len(names)} named") if names else (False, "brain/watch/competitors.md lists no competitor yet")


NEEDS = ("email", "invoicing", "shop", "shipping", "competitors")


def need_ready(need, brain):
    if need == "competitors": return competitors_ready(brain)
    if need in NEEDS: return connector_ready(need)
    return False, f"'{need}' is not a need this version knows"


def precheck_new_mail(since, root):
    m = _loaded("gmail/ops.py")
    total = sum(int(m.new_since(a["name"], since)["new"]) for a in m.accounts())
    return total > 0, f"{total} new e-mail(s)" if total else "no new mail"


def precheck_open_orders(since, root):
    out = _loaded("shopify/ops.py").open_count(since=str(int(since)))
    n, new = int(out.get("open_unfulfilled") or 0), int(out.get("new_since") or 0)
    return bool(n or new), f"{n} open unfulfilled, {new} new" if n or new else "no open or new orders"


def precheck_marketing_plan(since, root):
    fresh = [p for p in (pathlib.Path(root) / "handoffs" / "marketing").glob("*plan*.md") if p.stat().st_mtime >= time.time() - 3 * DAY]
    return bool(fresh), "this week's marketing plan is there" if fresh else "no marketing plan from the last 3 days"


PRECHECKS = {"new_mail": precheck_new_mail, "open_orders": precheck_open_orders, "marketing_plan": precheck_marketing_plan}


class Job(NamedTuple):
    id: str
    agent: str
    name: str
    when: str      # as set (pack, core or owner)
    rule: object   # Rule, or None when off
    needs: tuple
    precheck: str
    task: str
    lead: bool     # the lead's own run, in the owner's conversation (brief, evening, report); else a separate session
    pack: str
    source: str    # core | pack | owner (where `when` came from)
    off: str       # why it does not run; '' = it runs


class Outcome(NamedTuple):
    result: str    # busy | skipped | nothing | silent | ok | failed
    message: str   # for the owner ('' = nothing to send)
    note: str


class Scheduler:
    def __init__(self, brain=None, packs=None, root=None):
        self.root = pathlib.Path(root or ROOT)
        self.brain = pathlib.Path(brain or self.root / "brain")
        self.packs = packs
        self.path = self.brain / STATE
        self.extra = {}  # job id → callable returning text added to that job's task (the brief: agents in trouble, updates)

    # --- settings
    def _packs(self):
        return self.packs or [p.strip() for p in os.getenv("PACKS", "general").split(",") if p.strip()]

    def owner(self):
        """→ (the owner's schedule section as a dict, [problems]). Never raises."""
        v = alerts.setting("schedule")
        if v is None: return {}, []
        if not isinstance(v, dict): return {}, ["config/business.yaml: 'schedule' should be a list of settings (see config/business.example.yaml)"]
        probs = [f"config/business.yaml schedule: '{k}' is not a schedule setting (enabled, timezone, quiet_hours, jobs)"
                 for k in v if k not in ("enabled", "timezone", "quiet_hours", "jobs")]
        if "jobs" in v and v["jobs"] is not None and not isinstance(v["jobs"], dict):
            probs.append("config/business.yaml schedule.jobs should be job: time pairs, like  office.inbox: \"hourly :10\"")
        if "enabled" in v and not isinstance(v["enabled"], bool):
            probs.append("config/business.yaml schedule.enabled should be true or false")
        return v, probs

    def declared(self):
        """Every job the core and the installed packs declare → {id: entry}. A pack that cannot be read adds nothing."""
        out = {k: {**v, "pack": "core"} for k, v in CORE_JOBS.items()}
        try: merged = resolve(self._packs())
        except (SystemExit, OSError, ValueError): merged = {"schedule": {}, "agents": ["jinnee"]}
        out.update(merged.get("schedule", {}))
        return out, set(merged.get("agents", [])) | {"jinnee"}

    def jobs(self):
        owner, _ = self.owner()
        mine = owner.get("jobs") if isinstance(owner.get("jobs"), dict) else {}
        everything_off = owner.get("enabled") is False
        declared, agents = self.declared()
        out = []
        for jid, d in declared.items():
            when, source = d.get("when", ""), "core" if d.get("pack") == "core" else "pack"
            if jid in mine and mine[jid] is not True:  # True (`on`) keeps the default
                when, source = mine[jid], "owner"
            off, rule = "", None
            try: rule = parse(when)
            except SpecError as e:
                off = (f"your setting in config/business.yaml is not understood ({e})" if source == "owner"
                       else f"the pack's time is not understood ({e})")
            if rule is None and not off:
                off = "switched off in config/business.yaml" if source == "owner" else "switched off"
            needs = tuple(d.get("needs") or ())
            if not off and everything_off: off = "all scheduled runs are switched off (schedule.enabled: false in config/business.yaml)"
            if not off and d.get("agent") not in agents: off = f"its agent '{d.get('agent')}' is not installed"
            if not off and d.get("precheck") and d["precheck"] not in PRECHECKS: off = f"unknown precheck '{d['precheck']}'"
            if not off:
                for n in needs:
                    ok, why = need_ready(n, self.brain)
                    if not ok: off = f"needs {n}: {why}"; break
            out.append(Job(jid, d.get("agent", ""), d.get("name") or jid, when if isinstance(when, str) else ("off" if when is False else str(when)),
                           rule, needs, d.get("precheck") or "", d.get("task", ""), bool(d.get("lead")), d.get("pack", ""), source, off))
        on = {j.id for j in out if not j.off}
        for i, j in enumerate(out):  # a job that only sums up others: off when none of them runs
            sources = declared[j.id].get("after_any") or []
            if sources and not j.off and not on.intersection(sources):
                out[i] = j._replace(off="none of the jobs it sums up runs (" + ", ".join(sources) + ")")
        return out

    def check(self):
        """What is wrong with the owner's schedule settings ([] = all good)."""
        owner, probs = self.owner()
        mine = owner.get("jobs") if isinstance(owner.get("jobs"), dict) else {}
        declared, _ = self.declared()
        for jid, v in mine.items():
            if jid not in declared:
                probs.append(f"config/business.yaml schedule.jobs: there is no job '{jid}' (python3 core/scheduler.py plan lists them)")
                continue
            if v is True: continue
            try: parse(v)
            except SpecError as e: probs.append(f"config/business.yaml schedule.jobs.{jid}: {e}. That job does not run until it is fixed")
        tz = owner.get("timezone")
        if tz is not None:
            if not isinstance(tz, str) or not tz.strip(): probs.append("config/business.yaml schedule.timezone should be a name like Europe/Budapest")
            else:
                try:
                    import zoneinfo; zoneinfo.ZoneInfo(tz.strip())
                except Exception: probs.append(f"config/business.yaml schedule.timezone '{tz}' is not a time zone this machine knows")
                env = os.getenv("JINNEE_TZ", "").strip()
                if env and env != tz.strip():
                    probs.append(f"JINNEE_TZ={env} in .env differs from schedule.timezone ({tz.strip()}); .env wins. Delete one of them")
        q = owner.get("quiet_hours")
        if q is not None and q is not False and not (isinstance(q, str) and (q.strip().lower() in ("off", "none", "no", "0", "")
                                                                               or alerts.SPAN.fullmatch(q))):
            probs.append(f"config/business.yaml schedule.quiet_hours '{q}' is not like 21:00-07:00 (or off); the default is used")
        if "QUIET_HOURS" in os.environ and q is not None:
            probs.append("QUIET_HOURS in .env and schedule.quiet_hours are both set; .env wins. Delete one of them")
        return probs

    # --- the record
    def state(self):
        try: s = store.read_json(self.path, dict)
        except store.Unreadable: s = {}  # worst case a slot is taken again; the slot record is rebuilt from here
        return {k: v for k, v in s.items() if isinstance(v, dict)}

    def _edit(self, fn):
        with store.locked(self.brain):
            s = self.state()
            out = fn(s)
            store.write_json(self.path, s)
            return out

    @contextlib.contextmanager
    def exclusive(self):
        """One scheduled run at a time, across programs (two bridges on one brain/ would otherwise both run it).
        Yields False when another program holds it."""
        self.brain.mkdir(parents=True, exist_ok=True)
        f = open(self.brain / LOCK, "a+b")
        try:
            try:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); got = True
            except ImportError:  # Windows
                import msvcrt
                try: f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1); got = True
                except OSError: got = False
            except OSError: got = False
            yield got
        finally:
            f.close()  # closing releases the lock

    # --- deciding
    def _first_seen(self, jobs, now):
        """A job the record has never seen starts from now: what was due before it existed is not caught up."""
        s = self.state()
        new = [j.id for j in jobs if j.id not in s]
        if new:
            def mark(st):
                for jid in new: st.setdefault(jid, {"first_seen": int(now)})
            self._edit(mark)
            s = self.state()
        return s

    def current(self, job, now, tz, s):
        """The slot this job should be running for at `now`: (t, key) or None."""
        if job.off or job.rule is None: return None
        rec = s.get(job.id, {})
        start = max(rec.get("first_seen", now), now - GRACE[job.rule.kind])
        for t, key in reversed(slots(job.rule, start, now + 1, tz)):
            if t <= rec.get("slot_t", 0): return None   # taken already
            if alerts.quiet(t): return None              # a quiet-hour slot never runs
            return t, key
        return None

    def due(self, now=None):
        """→ [(job, t, key)] that should start now, the earliest slot first. Nothing in the quiet hours."""
        now = now or time.time()
        jobs = self.jobs()
        s = self._first_seen(jobs, now)
        if alerts.quiet(now): return []
        tz = alerts.zone()
        out = [(j, *c) for j in jobs for c in [self.current(j, now, tz, s)] if c]
        return sorted(out, key=lambda x: x[1])

    def next_runs(self, job, now, count=1, days=8, tz=None):
        """The next moments it will really run (quiet-hour slots left out)."""
        if job.off or job.rule is None: return []
        return [t for t, _ in slots(job.rule, now, now + days * DAY, tz) if not alerts.quiet(t)][:count]

    def since(self, job, slot_t, s=None):
        """From when the run should look for new things: the start of the last good run, else one interval back."""
        rec = (s or self.state()).get(job.id, {})
        return rec.get("ok_start") or slot_t - (job.rule.every if job.rule else 3600)

    # --- running
    def claim(self, job, t, key, now):
        def take(s):
            rec = s.setdefault(job.id, {"first_seen": int(now)})
            if t <= rec.get("slot_t", 0): return False
            rec.update(slot_t=t, slot=key, started=int(now), ended=None, result="running", note="")
            return True
        return self._edit(take)

    def finish(self, job, result, note, now, started=None):
        def done(s):
            rec = s.setdefault(job.id, {})
            rec.update(ended=int(now), result=result, note=" ".join(str(note).split())[:300])
            if result in ("ok", "silent", "nothing"): rec["ok_start"] = int(started or rec.get("started") or now)
        self._edit(done)

    def recover(self, now=None):
        """At start: a run the record says is still going cannot be (this program just started). Recorded as failed,
        unless another program holds the schedule lock and may really be running it."""
        now = now or time.time()
        with self.exclusive() as got:
            if not got: return []
            hit = []
            def fix(s):
                for jid, rec in s.items():
                    if rec.get("result") == "running":
                        rec.update(result="failed", ended=int(now), note="the program stopped during this run (restart or crash)")
                        hit.append(jid)
            self._edit(fix)
        for jid in hit:
            agent = next((j.agent for j in self.jobs() if j.id == jid), "")
            if agent: heartbeat.beat(agent, f"scheduled {jid}: the program stopped during the run", "failed", brain=self.brain)
        return hit

    def fingerprint(self):
        """sha256 over the program files: a scheduled run must not change them."""
        h = hashlib.sha256()
        paths = [self.root / f for f in PROGRAM_FILES]
        for d in PROGRAM_DIRS:
            base = self.root / d
            if base.is_dir(): paths += [p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
        for p in sorted(set(paths)):
            if not p.is_file(): continue
            h.update(str(p.relative_to(self.root)).encode()); h.update(b"\0")
            try: h.update(hashlib.sha256(p.read_bytes()).digest())
            except OSError: h.update(b"unreadable")
        return h.hexdigest()

    def prompt(self, job, t, since, tz=None):
        at = _local(t, tz).strftime("%a %Y-%m-%d %H:%M")
        zone = alerts.zone_name() or "this machine's clock"
        task = job.task + (self.extra[job.id]() if job.id in self.extra else "")
        if job.lead:
            return f"{task}\n\n(Scheduled: {job.name}, {at}, {zone}.) {UNATTENDED_RULES}"
        where = f"packs/{job.pack}/agents/{job.agent}.md" if job.pack not in ("", "core") else f"{job.agent}.md"
        look = _local(since, tz).strftime("%a %Y-%m-%d %H:%M")
        return (f"Scheduled run '{job.id}' for {at} ({zone}). Work as {job.name}, by the agent file {where}.\n"
                f"Task: {task}\nLook at what is new since {look}.\n\n{UNATTENDED_RULES}\n{REPLY}")

    def run(self, job, t, key, runner, now=None):
        """One due slot, start to finish. runner.busy() → bool; runner.run(job, prompt) → (worked, text, note).
        → Outcome. Never raises for a failed run: the failure is the outcome, and it is in the record."""
        with self.exclusive() as got:
            if not got: return Outcome("busy", "", "another program is running a scheduled job")
            if runner.busy(): return Outcome("busy", "", "the owner's conversation is running; tried again in a minute")
            now = now or time.time()
            if not self.claim(job, t, key, now): return Outcome("skipped", "", "this slot is taken already")
            heartbeat.beat(job.agent, f"scheduled {job.id}: started", "working", brain=self.brain)
            tz = alerts.zone()
            since = self.since(job, t)
            if job.precheck:
                try: work, note = PRECHECKS[job.precheck](since, self.root)
                except Exception as e:  # the connector said why; it never carries personal data
                    return self._end(job, "failed", "", f"the pre-check could not ask: {str(e)[:200] or type(e).__name__}", now)
                if not work: return self._end(job, "nothing", "", note, now)
            before = self.fingerprint()
            try: worked, text, note = runner.run(job, self.prompt(job, t, since, tz))
            except Exception as e: worked, text, note = False, "", f"the run could not start ({type(e).__name__})"
            if self.fingerprint() != before:
                return self._end(job, "failed", "", "a program file changed during the run; nothing was rolled back, "
                                                     "look at `git status` in the install folder", now)
            text = (text or "").strip()
            if not worked:
                return self._end(job, "failed", text if job.lead else "", note, now)
            if text.strip(" .").upper() == SILENT: return self._end(job, "silent", "", "nothing for the owner", now)
            return self._end(job, "ok", text if job.lead else f"{job.name} (scheduled):\n{text}", note or "done", now)

    def _end(self, job, result, message, note, started):
        self.finish(job, result, note, time.time(), started)
        heartbeat.beat(job.agent, f"scheduled {job.id}: {note}", "failed" if result == "failed" else "ok", brain=self.brain)
        return Outcome(result, message, note)

    # --- what finished in the quiet hours waits here (brain/schedule_outbox.json) for the first minute after them
    def later(self, text):
        with store.locked(self.brain):
            try: box = store.read_json(self.brain / OUTBOX, list)
            except store.Unreadable: box = []
            store.write_json(self.brain / OUTBOX, (box + [str(text)])[-20:])

    def outbox(self, now=None):
        """→ the held messages, once, when it is no longer quiet; [] otherwise."""
        if alerts.quiet(now or time.time()) or not (self.brain / OUTBOX).exists(): return []
        with store.locked(self.brain):
            try: box = store.read_json(self.brain / OUTBOX, list)
            except store.Unreadable: box = []
            if box: store.write_json(self.brain / OUTBOX, [])
        return [str(x) for x in box]

    # --- telling
    def rows(self, now=None):
        """Every job for people: what, when, next, last, result."""
        now = now or time.time()
        tz, s = alerts.zone(), self.state()
        out = []
        for j in self.jobs():
            rec = s.get(j.id, {})
            nxt = self.next_runs(j, now, tz=tz)
            off = j.off or ("" if nxt else f"its time is inside the quiet hours ({alerts.quiet_spec()}), so it never runs")
            last = rec.get("started")
            result = rec.get("result") or ("" if last else "never ran")
            if result != "running":  # a slot whose time has passed without a run
                cur = [x for x in slots(j.rule, now - LOOKBACK, now + 1, tz) if not alerts.quiet(x[0])] if j.rule and not j.off else []
                if cur and cur[-1][0] > rec.get("slot_t", 0) and cur[-1][0] >= rec.get("first_seen", now) and now - cur[-1][0] > GRACE[j.rule.kind]:
                    result = f"missed {when_text(cur[-1][0], now, tz)}"
            out.append({"id": j.id, "agent": j.agent, "name": j.name, "when": j.rule.text if j.rule and not j.off else j.when,
                        "source": j.source, "on": not off, "off": off, "next": nxt[0] if nxt else None,
                        "next_text": when_text(nxt[0], now, tz) if nxt else "", "last": last,
                        "last_text": when_text(last, now, tz) if last else "", "result": result, "note": rec.get("note", "")})
        return out

    def summary(self, now=None):
        """The /schedule answer on Telegram."""
        now = now or time.time()
        head = f"Schedule ({alerts.zone_name() or 'machine clock'}, quiet hours {alerts.quiet_spec()}):"
        lines = [head]
        for r in self.rows(now):
            if r["on"]:
                last = f"last {r['last_text']}: {r['result']}" + (f" ({r['note']})" if r["note"] else "") if r["last"] else r["result"]
                lines.append(f"• {r['name']}: {r['when']}. Next {r['next_text']}; {last}")
            else:
                lines.append(f"• {r['name']}: off, {r['off']}")
        probs = self.check()
        if probs: lines += ["", "Settings to fix:"] + [f"• {p}" for p in probs]
        lines += ["", "Change a time or switch one off in config/business.yaml (schedule.jobs); see config/business.example.yaml."]
        return "\n".join(lines)

    def plan(self, now=None, days=7):
        """What would run when; starts nothing."""
        now = now or time.time()
        tz = alerts.zone()
        lines = [f"Time zone: {alerts.zone_name() or 'this machine clock (set schedule.timezone in config/business.yaml)'}",
                 f"Quiet hours (nothing starts): {alerts.quiet_spec()}", f"Packs: {', '.join(self._packs())}", ""]
        for j in self.jobs():
            if j.off:
                lines.append(f"OFF  {j.id:<28} {j.name}: {j.off}"); continue
            runs = self.next_runs(j, now, count=10 ** 6, days=days, tz=tz)
            skipped = len(slots(j.rule, now, now + days * DAY, tz)) - len(runs)
            pre = f", asks '{j.precheck}' first" if j.precheck else ""
            lines.append(f"ON   {j.id:<28} {j.name}: {j.rule.text} ({j.source}{pre}); {len(runs)} run(s) in {days} days"
                         + (f", {skipped} slot(s) inside the quiet hours" if skipped else ""))
            for t in runs[:3]: lines.append(f"       {_local(t, tz).strftime('%a %Y-%m-%d %H:%M')}")
            if len(runs) > 3: lines.append(f"       … and {len(runs) - 3} more")
            if not runs:
                later = self.next_runs(j, now, tz=tz)
                lines.append(f"       next: {_local(later[0], tz).strftime('%a %Y-%m-%d %H:%M')}" if later
                             else "       never: its time is inside the quiet hours")
        probs = self.check()
        if probs: lines += ["", "Settings to fix:"] + [f"- {p}" for p in probs]
        return "\n".join(lines)

    def expected(self, now=None):
        """For the heartbeat: {agent: {"since", "when", "every"}}: the last slot each agent should have finished by now
        (its catch-up time is over). An agent with no beat since then did not run when it should have."""
        now = now or time.time()
        tz, s, out = alerts.zone(), self.state(), {}
        for j in self.jobs():
            if j.off or j.rule is None: continue
            rec = s.get(j.id, {})
            past = [t for t, _ in slots(j.rule, now - LOOKBACK, now - GRACE[j.rule.kind], tz)
                    if not alerts.quiet(t) and t >= rec.get("first_seen", now)]
            if past and past[-1] > out.get(j.agent, {}).get("since", 0):
                out[j.agent] = {"since": past[-1], "at": _local(past[-1], tz).strftime("%a %H:%M"), "when": j.rule.text,
                                "every": j.rule.every, "job": j.id}
        return out

    def problems(self, now=None):
        """For core/alerts.py, said once each: a scheduled agent run that failed (until a later run of it works), and the
        owner's settings that cannot be read. The lead's own runs (brief, evening) tell the owner themselves."""
        s, out = self.state(), []
        for j in self.jobs():
            rec = s.get(j.id, {})
            if rec.get("result") == "failed" and not j.lead:
                out.append(f"The scheduled run '{j.name}' failed: {rec.get('note') or 'no reason given'}. It tries again at its "
                           "next time; /schedule shows the details.")
        return out + [p for p in self.check() if "is not understood" in p or "there is no job" in p or "not a time" in p]


def main(argv=None):
    import argparse, env
    env.load()
    p = argparse.ArgumentParser(prog="scheduler.py", description="The schedule. These commands start nothing.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("plan", help="what would run when"); c.add_argument("--days", type=int, default=7)
    sub.add_parser("status", help="next and last run of each job")
    sub.add_parser("check", help="the owner's schedule settings")
    a = p.parse_args(argv)
    sched = Scheduler()
    if a.cmd == "plan": print(sched.plan(days=max(1, min(a.days, 31))))
    elif a.cmd == "status": print(sched.summary())
    else:
        probs = sched.check()
        print("\n".join(probs) if probs else "schedule settings: ok")
        return 1 if probs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
