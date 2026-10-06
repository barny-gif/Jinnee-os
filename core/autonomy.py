"""Autonomy levels: what the team may do without asking. One gate, used by the prompt, the approval queue and the dashboard.

  0 forbidden (the owner does it personally) · 1 approval required · 2 act, report afterwards · 3 free

brain/autonomy_config.json holds one entry per action: {"level": 1, "max_level": 2, "locked": true, "label": "…"}.
A locked action is never treated as higher than its max_level, and nothing in this program writes a level above it:
not an agent, not the dashboard. Only the owner can, by editing the file by hand.
An action nobody listed needs approval. A file that cannot be read falls back to the pack defaults.

  python core/autonomy.py list
  python core/autonomy.py check ACTION
  python core/autonomy.py propose ACTION LEVEL --why "…" [--agent NAME]   asks the owner; changes nothing by itself
  python core/autonomy.py apply APPROVAL_ID                               after the owner approved the proposal
"""
import os, pathlib, sys, time
from typing import NamedTuple
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store
from pack_loader import resolve

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAMES = {0: "forbidden", 1: "approval required", 2: "act, report afterwards", 3: "free"}
RAISE = "raise_autonomy"
FORMAT = 2  # "_format" in the file; a file without it is the old flat one
# Not a pack's business: changing a level always goes to the owner, and that rule cannot be relaxed from the file.
CORE = {RAISE: {"level": 1, "max_level": 1, "locked": True, "label": "Change an autonomy level"}}


class Gate(NamedTuple):
    level: int
    needs_approval: bool
    reason: str


class Refused(Exception):
    pass


def _level(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3 else None


def entry(value, base=None, lift=False):
    """Any accepted shape (a bare level, or a dict) → a full entry. What makes no sense fails closed.
    base: the pack default for the same action; it supplies whatever the value does not say.
    lift: reading a file from before locks existed, where a bare level is the owner's own setting: it is kept,
    and becomes the ceiling if it is above the pack's."""
    base = base or {}
    given = value if isinstance(value, dict) else {"level": value}
    level = _level(given.get("level"))
    if level is None: level = min(base.get("level", 1), 1)
    locked = given["locked"] if isinstance(given.get("locked"), bool) else bool(base.get("locked", False))
    cap = _level(given.get("max_level"))
    if not locked: cap = 3
    elif cap is None:
        cap = base.get("max_level", level)
        if lift: cap = max(cap, level)
    label = given.get("label") if isinstance(given.get("label"), str) else base.get("label", "")
    return {"level": level, "max_level": cap, "locked": locked, "label": label}


def effective(e, action=""):
    level = min(e["level"], e["max_level"]) if e["locked"] else e["level"]
    return min(level, CORE[RAISE]["max_level"]) if action == RAISE else level


class Config:
    def __init__(self, brain=None, packs=None):
        self.brain = pathlib.Path(brain or ROOT / "brain")
        self.path = self.brain / "autonomy_config.json"
        self.packs = packs

    def defaults(self):
        packs = self.packs or [p.strip() for p in os.getenv("PACKS", "general").split(",")]
        try: raw = resolve(packs)["autonomy_defaults"]
        except (SystemExit, OSError, ValueError): raw = {}  # a pack that is missing or broken: core rules only
        out = {a: entry(v) for a, v in raw.items()}
        out.update({a: dict(v) for a, v in CORE.items()})
        return out

    def read(self):
        """→ (table, problem). Never raises: a broken file means the defaults, and `problem` says so."""
        table = self.defaults()
        try: raw = store.read_json(self.path, dict)
        except store.Unreadable as e:
            return table, f"{e}. The fresh-install levels are in use until the file is fixed."
        old = raw.get("_format") != FORMAT
        for action, value in raw.items():
            if not action.startswith("_"): table[action] = entry(value, table.get(action), lift=old)
        table[RAISE] = dict(CORE[RAISE])
        return table, ""

    def write(self, table):
        store.write_json(self.path, {"_format": FORMAT, **table})

    def migrate(self):
        """A file from before locks existed ({"action": level}) → entries, keeping every level the owner set.
        Actions that arrived with a pack since are added. The old file is kept once as autonomy_config.json.v1.bak.
        A missing or broken file is left alone."""
        with store.locked(self.brain):
            try: raw = store.read_json(self.path, dict)
            except store.Unreadable: return False
            if not self.path.exists(): return False
            table, _ = self.read()
            if raw == {"_format": FORMAT, **table}: return False
            backup = self.path.with_name(self.path.name + ".v1.bak")
            if raw.get("_format") != FORMAT and not backup.exists():
                backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
            self.write(table)
            return True

    def allowed(self, action):
        """The gate. → Gate(level, needs_approval, reason)."""
        e = self.read()[0].get(action)
        if e is None:
            return Gate(1, True, f"'{action}' is not a listed action, so it needs the owner's approval")
        level = effective(e, action)
        if level == 0:
            return Gate(0, True, f"'{action}' is forbidden for the team (level 0): the owner does it personally")
        if level == 1:
            return Gate(1, True, f"'{action}' needs the owner's approval (level 1)")
        return Gate(level, False, f"'{action}' is level {level} ({NAMES[level]})")

    def describe(self):
        """The table as text, for the prompt and `list`."""
        table, problem = self.read()
        lines = [f"WARNING: {problem}"] if problem else []
        for action in sorted(table):
            e = table[action]; level = effective(e, action)
            lock = f", locked at max {e['max_level']}" if e["locked"] else ""
            lines.append(f"{action}: level {level} ({NAMES[level]}{lock})" + (f" – {e['label']}" if e["label"] else ""))
        return "\n".join(lines)

    def can_change(self, action, level):
        """Why a change is impossible, or ''. Lowering is always possible; raising stops at a locked action's max_level."""
        table, problem = self.read()
        if problem: return problem
        if _level(level) is None: return "the level must be 0, 1, 2 or 3"
        e = table.get(action)
        if action == RAISE: return "changing levels always needs the owner's approval; that rule itself cannot be changed"
        if e is None: return f"'{action}' is not a listed action"
        if e["locked"] and level > e["max_level"]:
            return (f"'{action}' is locked at max level {e['max_level']}. Only the owner can change that, "
                    f"by editing brain/autonomy_config.json by hand")
        return ""

    def propose(self, action, level, why, agent="jinnee"):
        """A level change is only ever a request to the owner. → the approval item."""
        import approvals
        no = self.can_change(action, level)
        if no: raise Refused(no)
        now = effective(self.read()[0][action], action)
        if level == now: raise Refused(f"'{action}' is already level {level}")
        return approvals.Book(self.brain).add(
            agent=agent, action=RAISE, title=f"{action}: level {now} → {level}",
            summary=" ".join(str(why).split()), payload={"target": action, "to": level, "from": now})[0]

    def apply(self, approval_id):
        """Write the level of an approved proposal. The approval is used up here, and the lock is checked again."""
        import approvals
        book = approvals.Book(self.brain)
        with store.locked(self.brain):
            item = book.get(approval_id)
            if item is None: raise Refused(f"no approval item '{approval_id}'")
            pay = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item.get("action") != RAISE or "target" not in pay:
                raise Refused(f"'{approval_id}' is not a level change")
            no = self.can_change(pay["target"], pay.get("to"))
            if no:
                if item["state"] != "consumed": book.withdraw(approval_id, f"not applied: {no}")
                raise Refused(no)
            verdict = book.consume(approval_id)
            if verdict.word != "GO": raise Refused(verdict.text)
            table, _ = self.read()
            table[pay["target"]]["level"] = pay["to"]
            self.write(table)
            book.result(approval_id, f"{pay['target']} is now level {pay['to']}")
            return pay["target"], pay["to"]


def main(argv):
    import argparse, approvals, env
    env.load()
    p = argparse.ArgumentParser(prog="autonomy.py", description="Autonomy levels.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("check"); c.add_argument("action")
    c = sub.add_parser("propose"); c.add_argument("action"); c.add_argument("level", type=int)
    c.add_argument("--why", required=True); c.add_argument("--agent", default="jinnee")
    c = sub.add_parser("apply"); c.add_argument("approval_id")
    a = p.parse_args(argv)
    cfg = Config()
    try:
        if a.cmd == "list": print(cfg.describe())
        elif a.cmd == "check":
            g = cfg.allowed(a.action)
            print(f"level {g.level} | {'ASK FIRST' if g.needs_approval else 'NO APPROVAL NEEDED'} | {g.reason}")
        elif a.cmd == "propose":
            item = cfg.propose(a.action, a.level, a.why, a.agent)
            print(f"ASKED {item['id']}: {item['title']}. Nothing changes until the owner approves and you run: "
                  f"{store.PY} core/autonomy.py apply {item['id']}")
        elif a.cmd == "apply":
            action, level = cfg.apply(a.approval_id)
            print(f"DONE {action} is now level {level} ({NAMES[level]})")
    except (Refused, approvals.Refused) as e:
        print(f"REFUSED {e}"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
