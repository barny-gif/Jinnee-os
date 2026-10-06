"""What the owner is told without asking. The Telegram bridge calls this once a minute; it is not a service of its own.

  - a new item is waiting for a decision: one message, once per item
  - a decision is ready to be picked up: handed to the lead once, after a short pause in which the owner can still undo
  - a decision was made hours ago and nothing happened (or work started and never reported back): once per item
  - once a day at most: what has been waiting for days, and what is still stuck
  - the autonomy settings changed, or a brain/ file could not be read: once per change

Nothing is sent during quiet hours; it goes out with the first check after they end.
What has been said is kept in brain/alerts_state.json, so a restart does not repeat it.

.env:  QUIET_HOURS=21:00-07:00 (off = never quiet) · REMIND_AFTER_DAYS=2 · STUCK_AFTER_HOURS=6 · UNDO_SECONDS=120
       JINNEE_TZ=Europe/Budapest (empty = this machine's clock; set it in Docker, where the clock is UTC)
"""
import datetime as dt, logging, os, pathlib, re, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store, approvals, autonomy

log = logging.getLogger("jinnee.alerts")
DEFAULT_QUIET = "21:00-07:00"
SPAN = re.compile(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*")


def number(name, default):
    try: v = float(os.getenv(name, "").strip() or default)
    except ValueError: return default
    return v if v >= 0 else default


def zone():
    name = os.getenv("JINNEE_TZ", "").strip()
    if not name: return None
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)
    except Exception:  # unknown name, or no time zone data on this machine
        log.warning("JINNEE_TZ=%s is not a time zone this machine knows; using the machine's clock", name)
        return None


def clock(now):
    """Epoch seconds → the owner's local date and time."""
    return dt.datetime.fromtimestamp(now, zone())


def quiet(now, spec=None):
    """Is `now` inside the quiet hours? 'off', 'none', '' or equal ends mean never. Nonsense means the default."""
    spec = os.getenv("QUIET_HOURS", DEFAULT_QUIET) if spec is None else spec
    if spec.strip().lower() in ("", "off", "none", "no", "0"): return False
    m = SPAN.fullmatch(spec) or SPAN.fullmatch(DEFAULT_QUIET)
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59: h1, m1, h2, m2 = map(int, SPAN.fullmatch(DEFAULT_QUIET).groups())
    start, end, t = h1 * 60 + m1, h2 * 60 + m2, clock(now).hour * 60 + clock(now).minute
    if start == end: return False
    return start <= t < end if start < end else (t >= start or t < end)


def line(book, it):
    label = book.gate.read()[0].get(it["action"], {}).get("label") or it["action"]
    return f"[{it['id']}] {it['title']} ({', '.join(x for x in (it['agent'], label) if x)})"


def ago(seconds):
    hours = int(seconds // 3600)
    return f"{hours // 24} days" if hours >= 48 else f"{hours} h" if hours >= 1 else f"{int(seconds // 60)} min"


class Watch:
    def __init__(self, brain=None):
        self.book = approvals.Book(brain)
        self.brain = self.book.brain
        self.cfg = self.book.gate
        self.path = self.brain / "alerts_state.json"

    def state(self):
        try: s = store.read_json(self.path, dict)
        except store.Unreadable: s = {}  # worst case something is said twice
        return {"asked": {}, "handed": {}, "stuck": {}, "digest_day": "", "levels": None, "problems": [], **s}

    def problems(self):
        out = [p for p in (self.book.read()[1], self.cfg.read()[1]) if p]
        for kept in sorted(self.brain.glob("approvals.json.broken-*")):
            out.append(f"brain/approvals.json could not be read and was set aside as {kept.name}. Anything that was waiting "
                       f"in it has to be asked again. Delete that file once you have looked at it.")
        return out

    def due(self, now=None):
        """→ {"messages": [...], "hand_over": [items], "state": {...}}. Writes nothing: call done() once the messages are out."""
        now = now or time.time()
        s, messages = self.state(), []
        pending, decided, running = self.book.open_items()
        alive = {i["id"] for i in pending + decided + running}
        for key in ("asked", "handed", "stuck"):
            s[key] = {i: t for i, t in s[key].items() if i in alive}

        # decisions go to the lead once the owner's time to undo has passed, quiet hours or not: the owner just decided
        pause = number("UNDO_SECONDS", 120)
        hand_over = [i for i in decided if i["id"] not in s["handed"] and now - (i["decided_at"] or 0) >= pause]
        for i in hand_over: s["handed"][i["id"]] = int(now)

        if quiet(now):
            return {"messages": [], "hand_over": hand_over, "state": s}

        new = [i for i in pending if i["id"] not in s["asked"]]
        if new:
            rows = []
            for i in new:
                rows.append("• " + line(self.book, i))
                warning = self.book.view(i)["warning"]
                if warning: rows.append(f"   not sendable as it is: {warning}")
                s["asked"][i["id"]] = int(now)
            one = new[0]["id"]
            messages.append("Waiting for your decision:\n" + "\n".join(rows) +
                            f"\n\n/ok {one} · /change {one} what to change · /drop {one} why\nOr use the dashboard.")

        limit = number("STUCK_AFTER_HOURS", 6) * 3600
        late = [(i, now - (i["decided_at"] or now)) for i in decided] + [(i, now - (i["consumed_at"] or now)) for i in running]
        late = [(i, age) for i, age in late if age >= limit]
        fresh = [(i, age) for i, age in late if i["id"] not in s["stuck"]]
        if fresh:
            rows = [f"• {line(self.book, i)}: {self.book.status(i)}, for {ago(age)}" for i, age in fresh]
            for i, _ in fresh: s["stuck"][i["id"]] = int(now)
            messages.append("Stuck: you decided, and nothing came of it yet.\n" + "\n".join(rows) +
                            "\n\nWhat you can do: tell me to carry it out, or /undo <id> to take the decision back "
                            "(if it was not started). If I do not answer at all, check that the machine is on.")

        today = clock(now).strftime("%Y-%m-%d")
        if s["digest_day"] != today:
            days = number("REMIND_AFTER_DAYS", 2)
            old = [i for i in pending if i not in new and now - i["created_at"] >= days * 86400]
            day = lambda ts: clock(ts).strftime("%Y-%m-%d")  # stuck items come back from the day after their own message
            still = [(i, age) for i, age in late if day(s["stuck"][i["id"]]) != today]
            if old or still:
                rows = [f"• {line(self.book, i)}: waiting for you for {ago(now - i['created_at'])}" for i in old]
                rows += [f"• {line(self.book, i)}: {self.book.status(i)}, for {ago(age)}" for i, age in still]
                messages.append("Still open:\n" + "\n".join(rows) + "\n\n/pending shows everything that is waiting.")
                s["digest_day"] = today

        levels = {a: [autonomy.effective(e, a), e["max_level"], e["locked"]] for a, e in self.cfg.read()[0].items()}
        if s["levels"] is not None and levels != s["levels"] and not self.cfg.read()[1]:
            rows = []
            for a in sorted(set(levels) | set(s["levels"])):
                was, now_ = s["levels"].get(a), levels.get(a)
                if was == now_: continue
                describe = lambda v: "not listed" if v is None else autonomy.NAMES[v[0]] + (f", locked at max {v[1]}" if v[2] else ", not locked")
                rows.append(f"• {a}: {describe(was)} → {describe(now_)}")
            messages.append("The autonomy settings changed:\n" + "\n".join(rows) +
                            "\n\nIf that was you or a request you approved, all is well. If not, look at brain/autonomy_config.json.")
        if not self.cfg.read()[1]: s["levels"] = levels

        problems = self.problems()
        for p in problems:
            if p not in s["problems"]: messages.append("Something needs your attention: " + p)
        s["problems"] = problems
        return {"messages": messages, "hand_over": hand_over, "state": s}

    def done(self, plan):
        with store.locked(self.brain):
            store.write_json(self.path, plan["state"])


def hand_over_message(book, items):
    """What the lead is told when decisions are ready. The lead still has to take each one with consume."""
    rows = [f"- {line(book, i)}: {book.status(i)}" + (f" – note: {i['note']}" if i["note"] else "") for i in items]
    return ("The owner has decided the following. Handle each one now, in this order: run `python3 core/approvals.py consume <id>`; "
            "act only if it prints GO; then record what happened with `result`. For CHANGE, fix it and ask again with "
            "`add … --replaces <id>`. For a level change use `python3 core/autonomy.py apply <id>` instead of consume. "
            "Then tell the owner in a few lines what was done.\n" + "\n".join(rows))
