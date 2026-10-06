"""The approval queue. This module is the only writer of brain/approvals.json; nobody edits that file by hand.

An item lives once:  pending → decided (ok / edit / drop: who, when, note) → consumed (when, outcome, result)
  - Only the owner decides, on the dashboard or with a Telegram command. There is no command for it here.
  - Until an item is consumed the owner can undo the decision. After that it is closed for good.
  - `consume` works once per item. It is the last step before acting: exit 0 and the word GO mean "do it now".
    A second consume, an undecided item, changed content or anything else is REFUSED, and then nothing is done.
  - What the owner approved is pinned: if the text or the file changes afterwards, the approval no longer counts.
  - An approved item whose outgoing text is empty or still marked as a draft is "approved, but not sendable".
  - `edit` sends the note back; the corrected version is a new item (`add --replaces ID`) and needs a new decision.
  - `drop` needs a reason. The reason goes to brain/lessons.md, which every agent reads.

  python core/approvals.py add --agent NAME --action ACTION --title "…" [--summary "…"] [--text "…" | --file handoffs/…] [--reference] [--replaces ID]
  python core/approvals.py list [--all]          show ID
  python core/approvals.py consume ID            GO (exit 0) · CHANGE / DROPPED (exit 3) · REFUSED (exit 1)
  python core/approvals.py result ID "what happened" [--failed]
  python core/approvals.py withdraw ID "why"
"""
import contextlib, hashlib, pathlib, re, secrets, sys, time
from typing import NamedTuple
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store, autonomy

ROOT = pathlib.Path(__file__).resolve().parent.parent
DECISIONS = ("ok", "edit", "drop")
CONTENT_DIRS = ("handoffs", "brain")   # --file must point inside one of these
KEEP_DAYS = 30                         # closed items stay this long, then leave the file (the decision log keeps the line)
PREVIEW = 6000                         # characters of the content shown on the dashboard
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
MARK = re.compile(r"(?:DRAFT|TODO|TBD|PLACEHOLDER|FIXME)\b"
                  r"|(?i:draft|todo|tbd|placeholder|vázlat|piszkozat|entwurf)\s*(?:[:\])\-–—]|$)"
                  r"|(?i:lorem ipsum)")
FIELDS = {"id": "", "agent": "", "action": "", "title": "", "summary": "", "text": None, "file": None, "outbound": False,
          "payload": None, "replaces": None, "created_at": 0, "state": "pending",
          "decision": None, "note": "", "decided_by": "", "decided_at": None, "sendable": None, "blocked": "", "sha": None,
          "consumed_at": None, "outcome": None, "result": "", "finished_at": None}
OLD = "decided before one-time approvals existed; closed without acting. Ask again if it is still wanted"


class Refused(Exception):
    """The request makes no sense in the item's current state. Nothing was changed."""

class Missing(Refused): pass
class Conflict(Refused): pass
class NotNeeded(Exception): pass


class Verdict(NamedTuple):
    word: str   # GO | CHANGE | DROPPED | REFUSED
    text: str
    item: dict


def one_line(s):
    return " ".join(str(s or "").split())


def unsendable(text):
    """Why this text must not go out as it is, or ''."""
    body = (text or "").strip()
    if not body: return "the text is empty"
    m = MARK.match(body.splitlines()[0].lstrip("#*_>[(<`~-–— \t"))
    return f'the text starts with a draft marker ("{m.group(0).strip()}")' if m else ""


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


class Book:
    def __init__(self, brain=None, root=None):
        self.brain = pathlib.Path(brain or ROOT / "brain")
        self.root = pathlib.Path(root or self.brain.parent)
        self.path = self.brain / "approvals.json"
        self.gate = autonomy.Config(self.brain)

    # --- reading
    def _normal(self, raw):
        """Whatever is in the file → items with every field. Items from before the lifecycle that already carry a
        decision are closed: nobody knows whether they were carried out, and acting twice is the worse mistake."""
        now, out, seen = int(time.time()), [], set()
        for r in raw:
            if not isinstance(r, dict): continue
            it = {**FIELDS, **r}
            it["id"] = str(it["id"] or self._new_id(seen))
            while it["id"] in seen: it["id"] += "-2"
            seen.add(it["id"])
            if r.get("state") not in ("pending", "decided", "consumed"):
                if r.get("decision") in DECISIONS:
                    it.update(state="consumed", outcome="closed", result=OLD, consumed_at=r.get("decided_at") or now)
                else:
                    it.update(state="pending", decision=None)
            if not isinstance(r.get("outbound"), bool):  # not said: content is taken to be what goes out
                it["outbound"] = isinstance(it["text"], str) or bool(it["file"])
            if not it["created_at"]: it["created_at"] = now
            out.append(it)
        return out

    def read(self):
        """→ (items, problem), without writing. A broken file reads as empty: nothing in it can be acted on."""
        try: return self._normal(store.read_json(self.path, list)), ""
        except store.Unreadable as e:
            return [], f"{e}. Nothing can be approved or carried out until it is fixed or replaced."

    def get(self, item_id):
        return next((it for it in self.read()[0] if it["id"] == item_id), None)

    def content(self, item):
        """The text an item is about → (text or None, problem)."""
        if isinstance(item.get("text"), str): return item["text"], ""
        if not item.get("file"): return None, ""
        p = (self.root / str(item["file"])).resolve()
        if not any(p.is_relative_to((self.root / d).resolve()) for d in CONTENT_DIRS):
            return None, f"{item['file']} is not inside {' or '.join(d + '/' for d in CONTENT_DIRS)}"
        try: return p.read_text(encoding="utf-8", errors="replace"), ""
        except OSError: return None, f"the file {item['file']} cannot be read"

    def _pin(self, item):
        """→ (fingerprint of the content, why it cannot go out or '')."""
        text, problem = self.content(item)
        if problem: return None, problem
        if text is None: return None, ""
        return hashlib.sha256(text.encode("utf-8")).hexdigest(), (unsendable(text) if item["outbound"] else "")

    def view(self, item):
        """An item as the owner should see it: with a preview of the content and, if there is one, the warning."""
        text, problem = self.content(item)
        warning = item["blocked"] if item["decision"] == "ok" else ""
        if item["state"] == "pending": warning = problem or (unsendable(text) if item["outbound"] and text is not None else "")
        return {**item, "text": None, "preview": (text or "")[:PREVIEW], "cut": len(text or "") > PREVIEW, "warning": warning,
                "label": self.gate.read()[0].get(item["action"], {}).get("label", "")}

    def open_items(self):
        """What still needs someone: (pending, decided but not picked up, picked up but no result yet)."""
        items = self.read()[0]
        return ([i for i in items if i["state"] == "pending"], [i for i in items if i["state"] == "decided"],
                [i for i in items if i["state"] == "consumed" and i["outcome"] == "executing"])

    # --- writing
    @contextlib.contextmanager
    def _open(self):
        with store.locked(self.brain):
            try: raw = store.read_json(self.path, list)
            except store.Unreadable as e:
                raw, kept = None, store.set_aside(self.path)
                self.log("system", f"approvals.json could not be read ({e}); kept as {kept}. Open approvals have to be asked again")
            items = self._normal(raw or [])
            yield items
            old = time.time() - KEEP_DAYS * 86400
            items[:] = [i for i in items if not (i["state"] == "consumed" and i["outcome"] != "executing"
                                                 and (i["finished_at"] or i["consumed_at"] or 0) < old)]
            if items != raw: store.write_json(self.path, items)

    def tidy(self):
        """Bring the file to the current shape (old items, closed ones past their date)."""
        with self._open(): pass

    def log(self, who, line):
        store.append_line(self.brain / "decisions.log.md", f"- {time.strftime('%Y-%m-%d')} {who}: {line}")

    def _new_id(self, taken):
        while True:
            new = "".join(secrets.choice(ALPHABET) for _ in range(4))
            if new not in taken: return new

    @staticmethod
    def _find(items, item_id):
        it = next((i for i in items if i["id"] == item_id), None)
        if it is None: raise Missing(f"no approval item '{item_id}'")
        return it

    def add(self, agent, action, title, summary="", text=None, file=None, reference=False, payload=None, replaces=None):
        """Ask the owner. → (item, warning). Refused when the action is forbidden; NotNeeded when no approval is required."""
        gate = self.gate.allowed(action)
        if gate.level == 0: raise Refused(f"{gate.reason}. Tell the owner in words instead of queueing it")
        if not gate.needs_approval: raise NotNeeded(f"{gate.reason}. Go ahead and report afterwards")
        if not one_line(title): raise Refused("a title is required: one line the owner understands")
        if text is not None and file: raise Refused("give --text or --file, not both")
        with self._open() as items:
            if replaces:
                old = self._find(items, replaces)
                if old["state"] != "consumed":  # the earlier decision does not carry over to the new version
                    old.update(state="consumed", outcome="superseded", consumed_at=int(time.time()), finished_at=int(time.time()))
            it = {**FIELDS, "id": self._new_id({i["id"] for i in items}), "agent": one_line(agent), "action": action,
                  "title": one_line(title), "summary": one_line(summary), "text": text, "file": file or None,
                  "outbound": (text is not None or bool(file)) and not reference, "payload": payload,
                  "replaces": replaces or None, "created_at": int(time.time())}
            _, warning = self._pin(it)
            if it["file"] and self.content(it)[1]: raise Refused(warning)
            if replaces: old["result"] = f"replaced by {it['id']}"
            items.append(it)
            return dict(it), warning

    def decide(self, item_id, decision, note="", by="dashboard"):
        """The owner's decision. Only a pending item can be decided; a decided one has to be undone first."""
        note = one_line(note)
        if decision not in DECISIONS: raise Refused("the decision is ok, edit or drop")
        with self._open() as items:
            it = self._find(items, item_id)
            if it["state"] != "pending":
                raise Conflict(f"'{item_id}' is already {self.status(it)}")
            if decision == "drop" and not note:
                raise Refused("dropping needs a reason: one sentence the team can learn from")
            if decision == "edit" and not note:
                raise Refused("say what should change")
            it.update(state="decided", decision=decision, note=note, decided_by=by, decided_at=int(time.time()))
            if decision == "ok":
                sha, blocked = self._pin(it)
                it.update(sha=sha, sendable=not blocked, blocked=blocked)
            self.log(by, f"{item_id} → {decision} {note}".rstrip())
            if decision == "drop":
                store.append_line(self.brain / "lessons.md",
                                  f"- {time.strftime('%Y-%m-%d')} dropped \"{it['title']}\" ({it['agent']}, {it['action']}): {note} {self._tag(item_id)}")
            return dict(it)

    @staticmethod
    def _tag(item_id):
        return f"[approval {item_id}]"

    def undo(self, item_id, by="dashboard"):
        """Back to pending, as long as nobody has acted on the decision."""
        with self._open() as items:
            it = self._find(items, item_id)
            if it["state"] == "pending": raise Conflict(f"'{item_id}' has no decision to undo")
            if it["state"] == "consumed": raise Conflict(f"'{item_id}' is already {self.status(it)}; it can no longer be undone")
            if it["decision"] == "drop":  # the reason was a slip: take it back out of what the team learns from
                lessons = self.brain / "lessons.md"
                if lessons.exists():
                    lines = lessons.read_text(encoding="utf-8").splitlines(keepends=True)
                    kept = [l for l in lines if not l.rstrip().endswith(self._tag(item_id))]
                    if kept != lines: store.write_text(lessons, "".join(kept))
            it.update(state="pending", decision=None, note="", decided_by="", decided_at=None, sendable=None, blocked="", sha=None)
            self.log(by, f"{item_id} → undo")
            return dict(it)

    def consume(self, item_id):
        """Take the decision, exactly once. Only GO means act."""
        with self._open() as items:
            try: it = self._find(items, item_id)
            except Missing as e: return Verdict("REFUSED", str(e), None)
            no = self._blocked(it)
            if no: return Verdict("REFUSED", no, dict(it))
            now = int(time.time())
            if it["decision"] == "ok":
                it.update(state="consumed", consumed_at=now, outcome="executing")
                self.log(it["agent"] or "agent", f"{item_id} → taken, carrying it out")
                return Verdict("GO", f"approved by the owner ({it['decided_by']}, {when(it['decided_at'])}). Carry it out now, exactly as "
                                     f"approved, then report: {store.PY} core/approvals.py result {item_id} \"what happened\"", dict(it))
            outcome, word = ("returned", "CHANGE") if it["decision"] == "edit" else ("dropped", "DROPPED")
            it.update(state="consumed", consumed_at=now, finished_at=now, outcome=outcome)
            if word == "CHANGE":
                return Verdict(word, f"the owner wants a change: {it['note']} | Fix it, then ask again with: add … --replaces {item_id}", dict(it))
            return Verdict(word, f"the owner dropped it: {it['note']} | Do not do it. The reason is in brain/lessons.md", dict(it))

    def _blocked(self, it):
        """Why this item cannot be taken now, or ''."""
        if it["state"] == "pending": return "not decided yet. Wait for the owner; do not act"
        if it["state"] == "consumed":
            return (f"already used on {when(it['consumed_at'])} ({it['outcome']}). An approval works once; "
                    f"if it is needed again, ask again with add")
        if it["decision"] != "ok": return ""
        gate = self.gate.allowed(it["action"])
        if gate.level == 0: return gate.reason
        again = f"Fix it and ask again with: add … --replaces {it['id']}"
        if not it["sendable"]: return f"approved, but not sendable: {it['blocked']}. {again}"
        if self._pin(it)[0] != it["sha"]:
            return f"the content changed after the owner approved it, so the approval no longer covers it. {again}"
        return ""

    def check(self, item_id):
        """Would consume say GO? → (bool, reason). Changes nothing."""
        it = self.get(item_id)
        if it is None: return False, f"no approval item '{item_id}'"
        no = self._blocked(it)
        if no: return False, no
        return it["decision"] == "ok", self.status(it)

    def result(self, item_id, text, failed=False):
        """What happened after GO. A failed attempt is closed too: trying again needs a new approval."""
        with self._open() as items:
            it = self._find(items, item_id)
            if it["state"] != "consumed" or it["outcome"] != "executing":
                raise Conflict(f"'{item_id}' is {self.status(it)}; a result follows a GO, once")
            it.update(outcome="failed" if failed else "done", result=one_line(text), finished_at=int(time.time()))
            self.log(it["agent"] or "agent", f"{item_id} → {it['outcome']}: {it['result']}".rstrip(": "))
            return dict(it)

    def withdraw(self, item_id, why=""):
        """The team takes its own request back (no longer relevant, replaced by something else)."""
        with self._open() as items:
            it = self._find(items, item_id)
            if it["state"] == "consumed": raise Conflict(f"'{item_id}' is already {self.status(it)}")
            now = int(time.time())
            it.update(state="consumed", consumed_at=now, finished_at=now, outcome="withdrawn", result=one_line(why))
            self.log(it["agent"] or "agent", f"{item_id} → withdrawn {it['result']}".rstrip())
            return dict(it)

    @staticmethod
    def status(it):
        """One phrase for people: where the item stands."""
        if it["state"] == "pending": return "waiting for the owner"
        if it["state"] == "decided":
            if it["decision"] == "ok" and not it["sendable"]: return f"approved, but not sendable: {it['blocked']}"
            return {"ok": "approved", "edit": "sent back for a change", "drop": "dropped"}[it["decision"]] + ", not picked up yet"
        return {"executing": "being carried out", "done": "done", "failed": "failed", "returned": "sent back for a change",
                "dropped": "dropped", "superseded": "replaced by a newer version", "withdrawn": "withdrawn",
                "closed": "closed"}.get(it["outcome"], "closed")

    def brief(self):
        """For the lead's prompt: what is waiting for the team, and what is waiting for the owner."""
        pending, decided, running = self.open_items()
        problem = self.read()[1]
        lines = [f"WARNING: {problem}"] if problem else []
        for it in decided:
            lines.append(f"DECIDED, take it now with `consume {it['id']}`: [{it['id']}] {it['title']} ({it['agent']}, {it['action']}) – {self.status(it)}")
        for it in running:
            lines.append(f"TAKEN, result missing, report with `result {it['id']} \"…\"`: [{it['id']}] {it['title']} ({it['agent']})")
        for it in pending:
            lines.append(f"waiting for the owner since {when(it['created_at'])}: [{it['id']}] {it['title']} ({it['agent']}, {it['action']})")
        return "\n".join(lines) or "Nothing is open."


def main(argv):
    import argparse, env
    env.load()
    p = argparse.ArgumentParser(prog="approvals.py", description="The approval queue. Only GO from `consume` means act.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("add", help="ask the owner")
    c.add_argument("--agent", required=True); c.add_argument("--action", required=True, help="the autonomy key, e.g. send_customer_email")
    c.add_argument("--title", required=True); c.add_argument("--summary", default="")
    c.add_argument("--text", help="the exact text that will go out"); c.add_argument("--file", help="or the file that holds it (handoffs/… or brain/…)")
    c.add_argument("--reference", action="store_true", help="the text or file is background, not something that goes out")
    c.add_argument("--replaces", metavar="ID", help="the item this corrected version replaces")
    c = sub.add_parser("list", help="what is open"); c.add_argument("--all", action="store_true")
    for name in ("show", "consume"):
        sub.add_parser(name).add_argument("id")
    c = sub.add_parser("result"); c.add_argument("id"); c.add_argument("text"); c.add_argument("--failed", action="store_true")
    c = sub.add_parser("withdraw"); c.add_argument("id"); c.add_argument("why", nargs="?", default="")
    a = p.parse_args(argv)
    book = Book()
    try:
        if a.cmd == "add":
            item, warning = book.add(a.agent, a.action, a.title, a.summary, a.text, a.file, a.reference, replaces=a.replaces)
            print(f"ASKED {item['id']}: waiting for the owner. Do nothing until `consume {item['id']}` says GO.")
            if warning: print(f"WARNING {warning}. The owner will see it as not sendable.")
        elif a.cmd == "list":
            if a.all:
                for it in book.read()[0]: print(f"[{it['id']}] {it['title']} ({it['agent']}, {it['action']}) – {book.status(it)}")
            else: print(book.brief())
        elif a.cmd == "show":
            it = book.get(a.id)
            if it is None: raise Missing(f"no approval item '{a.id}'")
            for k, v in book.view(it).items():
                if v not in (None, "", False) or k == "state": print(f"{k}: {v}")
            print(f"status: {book.status(it)}")
        elif a.cmd == "consume":
            v = book.consume(a.id)
            print(f"{v.word} {a.id}: {v.text}")
            return {"GO": 0, "REFUSED": 1}.get(v.word, 3)
        elif a.cmd == "result":
            it = book.result(a.id, a.text, a.failed)
            print(f"RECORDED {a.id}: {it['outcome']}")
        elif a.cmd == "withdraw":
            book.withdraw(a.id, a.why); print(f"WITHDRAWN {a.id}")
    except NotNeeded as e:
        print(f"NO APPROVAL NEEDED {e}"); return 2
    except Refused as e:
        print(f"REFUSED {e}"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
