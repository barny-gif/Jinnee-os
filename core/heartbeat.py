"""Agent liveness in brain/heartbeat.json: who started, who finished, who failed.

  python core/heartbeat.py beat AGENT "what was done" [--working | --failed]

Who is in trouble? An agent that said something and then went wrong: it started and never finished, or its last run
failed. And, since the schedule exists (core/scheduler.py), an agent that should have run and did not: the scheduler gives
each scheduled agent its expected rhythm and the last time it was due (`expected`); no beat since then means it did not
run. An agent without a schedule that has never checked in, or that finished its last job and has had nothing to do
since, is not flagged: "quiet for an hour" says nothing about an agent nobody asked."""
import pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store

BRAIN = pathlib.Path(__file__).resolve().parent.parent / "brain"
STALE = 60 * 60  # a run is cut off after 10 minutes, so "working" for an hour means it died on the way
STATES = ("working", "ok", "failed")


def load(brain=None):
    try: data = store.read_json(pathlib.Path(brain or BRAIN) / "heartbeat.json", dict)
    except store.Unreadable: return {}
    return {a: v for a, v in data.items() if isinstance(v, dict)}


def beat(agent: str, note: str = "", state: str = "ok", brain=None):
    brain = pathlib.Path(brain or BRAIN)
    with store.locked(brain):
        data = load(brain)
        data[agent] = {"ts": int(time.time()), "note": " ".join(str(note).split()), "state": state if state in STATES else "ok"}
        store.write_json(brain / "heartbeat.json", data)


def trouble(brain=None, now=None, expected=None):
    """→ ["ops: started 3 h ago and never finished", "jinnee: last run failed (timed out)",
          "office: should have run at 10:05 (hourly :05) and did not"]
    expected: {agent: {"since": epoch, "at": "Fri 10:05", "when": "hourly :05", "every": seconds}} from core/scheduler.py."""
    now, out, beats = now or time.time(), [], load(brain)
    for agent, v in sorted(beats.items()):
        age = now - v.get("ts", 0)
        if v.get("state") == "failed":
            out.append(f"{agent}: last run failed" + (f" ({v['note']})" if v.get("note") else ""))
        elif v.get("state") == "working" and age > STALE:
            out.append(f"{agent}: started {int(age // 3600)} h ago and never finished")
    for agent, e in sorted((expected or {}).items()):
        v = beats.get(agent) or {}
        if v.get("state") == "failed" or (v.get("state") == "working" and now - v.get("ts", 0) > STALE): continue  # said above
        if v.get("ts", 0) < e.get("since", 0):
            at = e.get("at") or time.strftime("%a %H:%M", time.localtime(e["since"]))  # the scheduler names it in the owner's time
            out.append(f"{agent}: should have run at {at} ({e.get('when', 'on its schedule')}) and did not")
    return out


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(prog="heartbeat.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("beat"); c.add_argument("agent"); c.add_argument("note", nargs="?", default="")
    c.add_argument("--working", action="store_true"); c.add_argument("--failed", action="store_true")
    sub.add_parser("trouble")
    a = p.parse_args()
    if a.cmd == "beat": beat(a.agent, a.note, "failed" if a.failed else "working" if a.working else "ok")
    else:
        import env; env.load()
        try:
            import scheduler; expected = scheduler.Scheduler().expected()
        except Exception: expected = None  # the old answer is still right, just without the rhythm
        print("\n".join(trouble(expected=expected)) or "Nobody is stuck.")
