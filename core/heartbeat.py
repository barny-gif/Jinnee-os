"""Agent liveness in brain/heartbeat.json: who started, who finished, who failed.

  python core/heartbeat.py beat AGENT "what was done" [--working | --failed]

Who is in trouble? Only an agent that said something and then went wrong: it started and never finished, or its last
run failed. An agent that has never checked in, or that finished its last job and has had nothing to do since, is not
flagged. Nothing here schedules the agents yet, so "quiet for an hour" says nothing about them, and flagging it was a
false alarm every morning. When a pack gives an agent a fixed rhythm, its expected interval belongs here too."""
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


def trouble(brain=None, now=None):
    """→ ["ops: started 3 h ago and never finished", "jinnee: last run failed (timed out)"]"""
    now, out = now or time.time(), []
    for agent, v in sorted(load(brain).items()):
        age = now - v.get("ts", 0)
        if v.get("state") == "failed":
            out.append(f"{agent}: last run failed" + (f" ({v['note']})" if v.get("note") else ""))
        elif v.get("state") == "working" and age > STALE:
            out.append(f"{agent}: started {int(age // 3600)} h ago and never finished")
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
    else: print("\n".join(trouble()) or "Nobody is stuck.")
