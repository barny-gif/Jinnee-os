"""Work files: how a long job survives being cut off.

A run is stopped after JINNEE_TIMEOUT seconds (10 minutes by default), without warning. What the agent only had in its head
is then gone. So for anything longer than a quick answer the agent writes as it goes, into a work file:

    handoffs/<agent>/<YYYY-MM-DD>-<topic>.md

    <!-- work: in-progress -->          first line, exactly; `done` when everything asked is in it
    # The topic
    Status: in progress, 14:10          a line for people, in the owner's language
    - [x] first step                    the step list: ticked as each step is done
    - [ ] second step
    ...the results, written in as soon as each one is there...

The first line is the machine's mark, the same in every language. The bridge (core/jinnee.py) and the schedule
(core/scheduler.py) read it after a run stopped: an in-progress file that changed during the run is the work so far.
The owner is told where it is and how far it got (the step names and counts only, never the results), and can send
/continue to go on from that file. handoffs/inbox/ (the owner's own uploads) is never looked at.

Why handoffs/ and not brain/: brain/ is the memory that goes into every prompt and is tidied by the bridge; a half-done
job is not memory. handoffs/<agent>/ is where the team already works (core/jinnee.md, Delegation), both the owner's runs
and the scheduled runs may write there, and in Docker it is a volume that survives a rebuild.
"""
import pathlib, re, time

IN_PROGRESS, DONE = "in-progress", "done"
MARK = re.compile(r"<!--\s*work:\s*(in-progress|done)\s*-->", re.I)
HEAD_LINES = 3          # the mark must be in the first non-empty lines
HEAD_BYTES = 2048
STEP = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+?)\s*$")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
LONG_NUMBER = re.compile(r"\+?\d(?:[\d \-/().]{5,}\d)")  # phone numbers, account numbers: 7+ digits with separators
SHOWN_STEPS = 5
STEP_CHARS = 70
SKIP = ("inbox",)        # handoffs/inbox/: the owner's files, not the team's work

RULE = """WORK IN PROGRESS. A run is stopped after {MINUTES} minutes without warning, and what is only in your head is lost. For anything longer than a quick answer:
- Before the first real step, create the work file `handoffs/<agent>/<YYYY-MM-DD>-<topic>.md`. Its first line is exactly `<!-- work: in-progress -->`; then a title, one status line in the owner's language, and the step list (`- [ ] step`).
- Write each result into the file as soon as you have it and tick its step (`- [x] step`). Never keep it all for the end; never rewrite the whole file to add a part.
- When everything asked is in it, change the first line to `<!-- work: done -->`. Never mark it done when it is not.
- Step names say what the work is, not who it is about: no customer names, e-mail addresses or phone numbers in them (the owner sees them in a summary).
- Told to continue a work file: read it first; what is ticked is done, so do not redo it and never send or queue it again; go on from the first open step in the same file."""


def _head(path):
    try:
        with open(path, "rb") as f: raw = f.read(HEAD_BYTES)
    except OSError: return ""
    return raw.decode("utf-8", "replace").lstrip("﻿")


def status(path):
    """→ 'in-progress', 'done', or '' (not a work file / cannot be read)."""
    lines = [l for l in _head(path).splitlines() if l.strip()][:HEAD_LINES]
    for l in lines:
        m = MARK.search(l)
        if m: return m.group(1).lower()
    return ""


def _files(root):
    base = pathlib.Path(root) / "handoffs"
    if not base.is_dir(): return []
    out = []
    for p in base.rglob("*.md"):
        rel = p.relative_to(base).parts
        if rel and rel[0] in SKIP: continue
        if p.is_file(): out.append(p)
    return out


def touched(root, since):
    """Work files changed at or after `since` (epoch seconds) → {"open": [rel paths], "done": [rel paths]}, newest first.
    One second of slack for file systems that keep whole seconds."""
    root, found = pathlib.Path(root), []
    for p in _files(root):
        try: m = p.stat().st_mtime
        except OSError: continue
        if m >= since - 1: found.append((m, p))
    out = {"open": [], "done": []}
    for _, p in sorted(found, key=lambda x: x[0], reverse=True):
        st = status(p)
        if st == IN_PROGRESS: out["open"].append(str(p.relative_to(root)).replace("\\", "/"))
        elif st == DONE: out["done"].append(str(p.relative_to(root)).replace("\\", "/"))
    return out


MAX_AGE_DAYS = 7  # see left_open()


def left_open(root, max_age_days=MAX_AGE_DAYS, now=None):
    """Work files still marked in progress that nobody is continuing → [(rel path, mtime)], newest first.
    For /continue when no stopped run was recorded: a run that ended normally (or whose subagent never got going) can
    still leave its file half-done. A file untouched for longer than `max_age_days` is left alone: work files are written
    at every step, so a week without a change means abandoned, and the slowest scheduled rhythm is weekly, so a job's own
    next run has taken over by then. The file stays on disk; it is only no longer offered."""
    root = pathlib.Path(root)
    limit = (time.time() if now is None else now) - max_age_days * 86400
    found = []
    for p in _files(root):
        try: m = p.stat().st_mtime
        except OSError: continue
        if m >= limit and status(p) == IN_PROGRESS:
            found.append((str(p.relative_to(root)).replace("\\", "/"), m))
    return sorted(found, key=lambda x: x[1], reverse=True)


def still_open(root, paths):
    """The ones of `paths` that are still marked in progress (a file deleted or marked done since drops out)."""
    return [p for p in paths if status(pathlib.Path(root) / p) == IN_PROGRESS]


def clean(text):
    """A step name as the owner may see it: one line, cut short, e-mail addresses and long numbers masked."""
    text = EMAIL.sub("[e-mail]", " ".join(str(text).split()))
    text = LONG_NUMBER.sub("[number]", text)
    return text if len(text) <= STEP_CHARS else text[:STEP_CHARS - 1].rstrip() + "…"


def steps(path):
    """→ (done names, open names) from the step list."""
    done, todo = [], []
    try: text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError: return done, todo
    for line in text.splitlines():
        m = STEP.match(line)
        if m: (done if m.group(1).lower() == "x" else todo).append(m.group(2))
    return done, todo


def has_content(path, least=20):
    """A file that says something: at least `least` characters besides the mark, headings and blank lines."""
    try: text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError: return False
    body = "".join(l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#") and not MARK.search(l))
    return len(body) >= least


def summary(root, paths):
    """For the owner: where the work is and how far it got. Step names and counts only; never the results themselves."""
    rows = []
    for rel in paths:
        done, todo = steps(pathlib.Path(root) / rel)
        if done or todo:
            row = f"• {rel}: {len(done)} of {len(done) + len(todo)} steps done"
            if done:
                names = [clean(n) for n in done[-SHOWN_STEPS:]]
                row += ("" if len(done) <= SHOWN_STEPS else f" (the last {SHOWN_STEPS})") + ": " + "; ".join(names)
            if todo: row += f". Next: {clean(todo[0])}"
        else:
            row = f"• {rel}: started, no step list in it"
        rows.append(row)
    return "\n".join(rows)


def resume_prompt(paths, task, minutes, attempt=1, stopped=True):
    """The message that continues cut-off work from its file(s). stopped=False: a file found still in progress after a run
    that ended normally (no record of the task; the file's title and steps say what it is)."""
    files = ", ".join(paths)
    why = (f"An earlier run on this task was stopped after {minutes} minutes before it finished (this is continuation {attempt})."
           if stopped else "An earlier run ended and left this work unfinished.")
    if not stopped and not task: task = "(not kept: the title and the step list in the file say what it is)"
    return (f"CONTINUE. {why} The work so far is in: {files} (marked <!-- work: in-progress -->).\n"
            "Read that file first. What is ticked is done: do not redo it, and never send, queue or consume anything a second "
            "time (an approval already used will be refused anyway). Go on from the first open step, writing into the same file "
            "as you go, and set its first line to <!-- work: done --> when everything asked is in it. Then answer as the "
            "original task asks.\n\nThe original task:\n" + str(task or "(not kept)")[:4000])

