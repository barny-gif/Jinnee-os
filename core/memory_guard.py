"""The bridge's hourly memory tidy, with the owner's lessons behind an approval.

brain/forge_notes.md is a technical notebook: its old lines move to the archive by themselves (core/memory.py compact).
brain/lessons.md holds the owner's corrections. Moving them out silently once took the owner's ground rules out of the
file, so here they never move without the owner's word:

  1. lessons.md is over its cap → one approval item (action archive_lessons, agent "memory") that lists the lines that
     would move. The owner hears of it the usual way (core/alerts.py: quiet hours, tap buttons, the dashboard), with a
     short excerpt (dates and the start of each line; e-mail addresses and longer numbers left out) and the choices:
     [pin] the lines that must stay, raise JINNEE_MEMORY_LESSONS_BYTES, or /ok.
  2. Nothing moves while it waits. /ok → after the undo pause the bridge carries it out itself (consume, then only the
     approved lines that are still there, unchanged, unpinned and still needed to get under the cap; then result).
     A line the owner pinned in the meantime no longer matches, so it stays.
  3. /drop or /change → nothing moves, and the owner is not asked again for ASK_AGAIN_DAYS days.
  4. Back under the cap (the owner raised it, or pinned) → a waiting item is withdrawn by itself.

archive_lessons is a core action (core/autonomy.py): approval required, locked at most there. Only the owner can change
that, by hand in brain/autonomy_config.json; level 0 means nothing ever moves from lessons.md.
"""
import datetime as dt, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import approvals, memory, store, texts

ACTION = "archive_lessons"
AGENT = "memory"
KIND = "lessons_archive"
ASK_AGAIN_DAYS = 7


def ours(item):
    """An item this module asked for (an agent cannot add a payload from the command line)."""
    pay = item.get("payload")
    return item.get("action") == ACTION and item.get("agent") == AGENT and isinstance(pay, dict) and pay.get("kind") == KIND


def _lessons(brain):
    return memory.read(brain / memory.CORES["lessons"]["file"])


def notice(moved, size_before):
    """The lines under the item in the owner's Telegram message."""
    cap_b = memory.cap("lessons")[0]
    return ([texts.t("memory.notice_size", size=size_before, cap=cap_b, n=len(moved))]
            + [f"· {row}" for row in memory.excerpt(moved)]
            + [texts.t("memory.notice_pin", pin=memory.PIN), texts.t("memory.notice_raise", cap=cap_b),
               texts.t("memory.notice_ok", days=ASK_AGAIN_DAYS), texts.t("memory.notice_dashboard")])


def tidy(brain, book=None, today=None, now=None):
    """Once an hour from the bridge. → log lines. Writes forge_notes.md and its archive; for lessons.md only asks."""
    brain = pathlib.Path(brain)
    book = book or approvals.Book(brain)
    today, now = today or dt.date.today(), now or time.time()
    out = []
    for rep in memory.compact(brain, apply=True, today=today, names=("notes",)):
        if rep["moved"]: out.append(f"memory: {len(rep['moved'])} old entries of {rep['file']} moved to {rep['archive']}")
    if not (brain / memory.CORES["lessons"]["file"]).exists(): return out
    with store.locked(brain):  # nobody adds a lesson or decides between the look and the ask
        text = _lessons(brain)
        moved, _ = memory.plan("lessons", text, today)
        mine = [i for i in book.read()[0] if ours(i)]
        waiting = [i for i in mine if i["state"] == "pending"]
        if not moved:
            for i in waiting:
                book.withdraw(i["id"], "no longer needed: brain/lessons.md is within its cap now")
                out.append(f"memory: {i['id']} withdrawn, lessons.md is within its cap")
            return out
        if [i for i in mine if i["state"] in ("pending", "decided") or i["outcome"] == "executing"]:
            return out  # one question at a time; the owner has not answered (or it is being carried out)
        recent_no = [i for i in mine if i["outcome"] in ("dropped", "returned")
                     and now - (i["finished_at"] or i["consumed_at"] or 0) < ASK_AGAIN_DAYS * 86400]
        if recent_no: return out
        gate = book.gate.allowed(ACTION)
        if gate.level == 0:
            return out + [f"memory: lessons.md is over its cap, and {ACTION} is forbidden: nothing moves"]
        if not gate.needs_approval:  # the owner raised it by hand: act, and say so in the decision log
            rep = memory.compact(brain, apply=True, today=today, names=("lessons",))[0]
            book.log(AGENT, f"{len(rep['moved'])} old lines of lessons.md moved to lessons.archive.md ({gate.reason})")
            return out + [f"memory: {len(rep['moved'])} old entries of lessons.md moved ({gate.reason})"]
        lines = [e.text.strip() for e in moved]
        item, _ = book.add(AGENT, ACTION, texts.t("memory.title", n=len(moved)), summary=texts.t("memory.summary"),
                           text="\n".join(lines) + "\n", reference=True,
                           payload={"kind": KIND, "entries": lines, "notice": notice(moved, memory._size(text)[0])})
        out.append(f"memory: lessons.md over its cap, asked the owner ({item['id']}, {len(moved)} lines)")
    return out


def carry_out(brain, item_id, book=None, today=None):
    """The owner decided on one of our items and the undo pause is over. → the message for the owner."""
    brain = pathlib.Path(brain)
    book = book or approvals.Book(brain)
    item = book.get(item_id)
    if item is None or not ours(item): return ""
    verdict = book.consume(item_id)
    if verdict.word == "CHANGE":
        return texts.t("memory.changed", id=item_id, days=ASK_AGAIN_DAYS, pin=memory.PIN)
    if verdict.word != "GO": return ""  # dropped (closed by the bridge already) or refused: nothing to do, nothing moved
    approved = set(item["payload"].get("entries") or [])
    try:
        reps = memory.compact(brain, apply=True, today=today, names=("lessons",), only={"lessons": approved})
    except Exception as e:
        book.result(item_id, f"nothing moved: {type(e).__name__}: {e}", failed=True)
        return texts.t("memory.failed", id=item_id)
    moved = reps[0]["moved"] if reps else []
    book.result(item_id, f"moved {len(moved)} of {len(approved)} approved lines to brain/lessons.archive.md")
    kept = len(approved) - len(moved)
    return texts.t("memory.done", id=item_id, n=len(moved)) + (texts.t("memory.done_kept", kept=kept) if kept else "")
