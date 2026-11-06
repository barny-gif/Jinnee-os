"""Searchable memory: a small core in the prompt, everything else found on demand.

  python core/memory.py search "<topic>" [--limit 8] [--in lessons,notes,decisions,catalog]
  python core/memory.py get ID                    # one hit in full, ID as printed by search (e.g. lessons:12)
  python core/memory.py log-tail [N]              # the last N lines of brain/decisions.log.md (default 20)
  python core/memory.py log-search "<topic>" [--limit 10]
  python core/memory.py stats                     # sizes, and how much of each core is in the prompt
  python core/memory.py core lessons|notes        # exactly what the prompt gets from that file
  python core/memory.py compact [--apply yes]     # the owner's, by hand: move old lines to the archives (lists the lessons ones)

Why: the prompt used to carry brain/lessons.md and brain/forge_notes.md whole, on every run. They only grow, so every run
got slower, dearer and noisier, and nothing in them could be searched. Now the prompt gets a CORE of each, with a hard cap
(bytes AND lines): the lines marked [pin] first, then the newest lines that fit. Older lines are not lost: they stay in the
file until `compact` moves them to <name>.archive.md, and search reads the files, the archives, the decision log and an
optional brain/catalog.md (one line per topic, kept by the lead).

Only `compact` writes, and only these four files: lessons.md, forge_notes.md and their .archive.md. It never moves a [pin]
line, a heading or a line dated in the last 14 days (an owner's /undo takes a fresh drop reason back out of lessons.md, so
it must still be there), keeps a copy of the file it rewrites in brain/backups/memory/, and checks afterwards that every
line is either still in the file or in the archive. The agents get the reading commands only; `compact` is denied to them.
The bridge's hourly tidy (core/memory_guard.py) compacts forge_notes.md by itself, but lessons.md holds the owner's own
rules: its old lines move only after the owner said /ok to an approval item that lists them, and only those lines.
The title and the intro under it ("The owner's corrections. Every agent reads this.") are the preamble, never an entry.

Search is BM25 (the ranking search engines use: rare words count more, long entries do not win by length) over entries:
one line plus any indented lines under it. Accents are folded and a word also matches its longer forms (order ~ orders).
Plain Python, nothing to install."""
import argparse, datetime as dt, math, os, pathlib, re, shutil, sys, time, unicodedata
from collections import Counter
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store

BRAIN = pathlib.Path(__file__).resolve().parent.parent / "brain"

# The core: what the prompt carries of each file. Bytes are the cost; lines keep many short lines from becoming noise.
CORES = {
    "lessons": {"file": "lessons.md", "archive": "lessons.archive.md", "env": "JINNEE_MEMORY_LESSONS_BYTES", "bytes": 8000, "lines": 80},
    "notes": {"file": "forge_notes.md", "archive": "forge_notes.archive.md", "env": "JINNEE_MEMORY_NOTES_BYTES", "bytes": 4000, "lines": 40},
}
MIN_BYTES, MIN_LINES = 500, 5
PIN = "[pin]"
KEEP_DAYS = 14        # compact never moves a line dated this recently
TARGET = 0.75         # compact brings the file down to 75% of the cap, so it does not run again at the next new line
BACKUPS_KEPT = 10
NEVER = ("approvals.json", "autonomy_config.json", "alerts_state.json")  # the gates: this module never opens them

# What search reads: (id prefix, file, group for --in)
SOURCES = (("lessons", "lessons.md", "lessons"), ("lessons-archive", "lessons.archive.md", "lessons"),
           ("notes", "forge_notes.md", "notes"), ("notes-archive", "forge_notes.archive.md", "notes"),
           ("decisions", "decisions.log.md", "decisions"), ("catalog", "catalog.md", "catalog"))
GROUPS = ("lessons", "notes", "decisions", "catalog")

K1, B = 1.5, 0.75
PREFIX_FACTOR = 0.6
_TOKEN = re.compile(r"[a-z0-9]{2,}")
_DATE = re.compile(r"^\s*(?:[-*+]\s+)?(\d{4}-\d{2}-\d{2})")
_ITEM = re.compile(r"(?:[-*+]|\d+[.)])\s")  # a list item at the left margin: "- ", "* ", "+ ", "1. ", "1) "


# ---------------------------------------------------------------- reading a memory file

class Entry:
    """One line that starts at the left margin, with the indented and blank lines that follow it."""
    __slots__ = ("line", "raw")

    def __init__(self, line, raw): self.line, self.raw = line, raw

    @property
    def text(self): return "".join(self.raw).rstrip("\n").rstrip() + "\n"
    @property
    def heading(self): return self.raw[0].lstrip().startswith("#")
    @property
    def pinned(self): return PIN in "".join(self.raw)
    @property
    def date(self):
        m = _DATE.match(self.raw[0])
        try: return dt.date.fromisoformat(m.group(1)) if m else None
        except ValueError: return None
    @property
    def size(self): return len(self.text.encode("utf-8")), self.text.count("\n")


def split(text):
    """→ (preamble lines, entries). Lossless: the preamble plus every entry's raw lines is the text again, byte for byte.
    The preamble is the title: the headings and blank lines before the first real line, and, in a file that is a list,
    the intro under a heading: the undated lines that are not list items, up to the first list item or dated line
    ("The owner's corrections. Every agent reads this."). The preamble is not an entry: never moved, never counted."""
    lines = text.splitlines(keepends=True)
    pre, entries, i = [], [], 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        pre.append(lines[i]); i += 1
    if any(ln.lstrip().startswith("#") for ln in pre) and any(_ITEM.match(ln) for ln in lines[i:]):
        while i < len(lines) and not _ITEM.match(lines[i]) and not _DATE.match(lines[i]):
            pre.append(lines[i]); i += 1
    for n in range(i, len(lines)):
        ln = lines[n]
        if ln.strip() and not ln[0].isspace() or not entries:
            entries.append(Entry(n + 1, [ln]))
        else:
            entries[-1].raw.append(ln)
    return pre, entries


def read(path):
    """The text of a memory file; a missing or unreadable file is empty (search and the prompt go on without it)."""
    try: return pathlib.Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError): return ""


def _int_env(name, default):
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() else default


def cap(name):
    c = CORES[name]
    return max(MIN_BYTES, _int_env(c["env"], c["bytes"])), max(MIN_LINES, c["lines"])


def _size(text): return len(text.encode("utf-8")), text.count("\n") + (1 if text and not text.endswith("\n") else 0)


# ---------------------------------------------------------------- the core in the prompt

def core(name, brain=None):
    """→ (text for the prompt, facts). Never over the cap: the preamble, then the [pin] entries, then the newest entries
    that still fit, printed in the file's own order. What did not fit is named in one notice line at the end (not counted
    in the cap, at most about 400 bytes), with the command that finds it."""
    brain = pathlib.Path(brain or BRAIN)
    fname = CORES[name]["file"]
    max_b, max_l = cap(name)
    pre, entries = split(read(brain / fname))
    used_b = used_l = 0
    head = []
    for ln in pre:  # a title longer than the cap is cut too
        b, l = _size(ln)
        if used_b + b > max_b or used_l + l > max_l: break
        head.append(ln); used_b += b; used_l += l
    keep, pin_cut = set(), 0

    def fits(e):
        b, l = e.size
        return used_b + b <= max_b and used_l + l <= max_l

    for e in entries:
        if not e.pinned or not e.text.strip(): continue
        if fits(e):
            keep.add(e.line); b, l = e.size; used_b += b; used_l += l
        else:
            pin_cut += 1
    for e in reversed(entries):
        if e.pinned or e.line in keep or not e.text.strip(): continue
        if not fits(e): break  # the newest ones, without gaps: an older line never pushes out a newer one
        keep.add(e.line); b, l = e.size; used_b += b; used_l += l
    hidden = [e for e in entries if e.line not in keep and e.text.strip()]
    body = "".join(head) + "".join(e.text for e in entries if e.line in keep)
    facts = {"file": fname, "cap_bytes": max_b, "cap_lines": max_l, "bytes": used_b, "lines": used_l,
             "shown": len(keep), "hidden": len(hidden), "hidden_bytes": sum(e.size[0] for e in hidden), "pin_cut": pin_cut}
    notes = []
    if hidden and len(hidden) > pin_cut:
        notes.append(f"{len(hidden) - pin_cut} older entries of brain/{fname} ({facts['hidden_bytes']} bytes) are not in this "
                     f"prompt: the core is capped at {max_b} bytes / {max_l} lines. Nothing is lost: "
                     f"`{store.PY} core/memory.py search \"<topic>\"` finds them")
    if pin_cut:
        notes.append(f"{pin_cut} {PIN} entries did not fit: the {PIN} lines alone are over the cap, the lead should merge them")
    if notes: body = body.rstrip("\n") + "\n[MEMORY CAP REACHED: " + ". ".join(notes) + ".]\n"
    return body, facts


def prompt_block(brain=None):
    """The memory part of the system prompt: company.md whole, the cores of lessons.md and forge_notes.md."""
    brain = pathlib.Path(brain or BRAIN)
    parts = []
    company = read(brain / "company.md")
    if company: parts.append(f"\n\n===== brain/company.md =====\n{company}")
    for name in ("lessons", "notes"):
        if not (brain / CORES[name]["file"]).exists(): continue
        text, f = core(name, brain)
        label = f"brain/{f['file']} (newest {f['shown']} entries" + (f" of {f['shown'] + f['hidden']}" if f["hidden"] else "") + ")"
        parts.append(f"\n\n===== {label} =====\n{text}")
    return "".join(parts)


# ---------------------------------------------------------------- search

def fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def tokens(text): return _TOKEN.findall(fold(text))


class Doc:
    __slots__ = ("id", "group", "file", "line", "text", "date", "order")

    def __init__(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)


def documents(brain=None, groups=GROUPS):
    brain = pathlib.Path(brain or BRAIN)
    docs = []
    for prefix, fname, group in SOURCES:
        if group not in groups: continue
        _, entries = split(read(brain / fname))
        for e in entries:
            if e.heading or not e.text.strip(): continue
            docs.append(Doc(id=f"{prefix}:{e.line}", group=group, file=fname, line=e.line, text=e.text.strip(),
                            date=e.date, order=len(docs)))
    return docs


def rank(query, docs):
    """→ [(doc, score)] best first, without the ones that share no word with the query. Ties: the newer one first."""
    q = tokens(query)
    if not q or not docs: return []
    terms = [Counter(tokens(d.text)) for d in docs]
    lengths = [sum(c.values()) or 1 for c in terms]
    avg = sum(lengths) / len(lengths)
    n, scores = len(docs), [0.0] * len(docs)
    for qt in dict.fromkeys(q):
        tf = []
        for c in terms:
            hit = float(c.get(qt, 0))
            if not hit: hit = sum(cnt for t, cnt in c.items() if len(t) > len(qt) and t.startswith(qt)) * PREFIX_FACTOR
            tf.append(hit)
        df = sum(1 for x in tf if x > 0)
        if not df: continue
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        for i, x in enumerate(tf):
            if x > 0: scores[i] += idf * x * (K1 + 1) / (x + K1 * (1 - B + B * lengths[i] / avg))
    hits = [(docs[i], s) for i, s in enumerate(scores) if s > 0]
    hits.sort(key=lambda h: (-round(h[1], 9), -(h[0].date or dt.date.min).toordinal(), -h[0].order))
    return hits


def short(text, width=200):
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def search(query, brain=None, limit=8, groups=GROUPS):
    return rank(query, documents(brain, groups))[:max(1, limit)]


def get(doc_id, brain=None):
    """The entry an ID points at (the ID's line may be any line inside it), or None."""
    prefix, _, num = doc_id.partition(":")
    files = {p: f for p, f, _ in SOURCES}
    if prefix not in files or not num.isdigit(): return None
    _, entries = split(read(pathlib.Path(brain or BRAIN) / files[prefix]))
    for e in entries:
        if e.line <= int(num) < e.line + len(e.raw): return f"brain/{files[prefix]}:{e.line}", e.text
    return None


def tail(n, brain=None):
    _, entries = split(read(pathlib.Path(brain or BRAIN) / "decisions.log.md"))
    entries = [e for e in entries if not e.heading and e.text.strip()]
    return entries[-n:] if n > 0 else []


# ---------------------------------------------------------------- keeping the core small

def _backup(brain, path):
    folder = brain / "backups" / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    kept = folder / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    k = 1
    while kept.exists():
        kept = folder / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}-{k}.bak"; k += 1
    shutil.copy2(path, kept)
    old = sorted(folder.glob(f"{path.name}.*.bak"), key=lambda p: p.stat().st_mtime)
    for p in old[:-BACKUPS_KEPT]: p.unlink()
    return kept


def plan(name, text, today=None, allowed=None):
    """→ (entries to move, the text that stays). Moves the oldest movable entries until the file is within TARGET of the
    cap. Movable: not a heading, not [pin], not dated in the last KEEP_DAYS days, and, when `allowed` is given (the
    owner's approved list for lessons.md), one of those texts exactly. Empty list = nothing to do."""
    max_b, max_l = cap(name)
    b, l = _size(text)
    if b <= max_b and l <= max_l: return [], text
    today = today or dt.date.today()
    goal_b, goal_l = int(max_b * TARGET), int(max_l * TARGET)
    pre, entries = split(text)
    move = set()
    for e in entries:
        if b <= goal_b and l <= goal_l: break
        if e.heading or e.pinned or not e.text.strip(): continue
        if e.date and (today - e.date).days < KEEP_DAYS: continue
        if allowed is not None and e.text.strip() not in allowed: continue
        eb, el = _size("".join(e.raw))
        move.add(e.line); b -= eb; l -= el
    moved = [e for e in entries if e.line in move]
    return moved, "".join(pre) + "".join("".join(e.raw) for e in entries if e.line not in move)


def compact(brain=None, apply=False, today=None, names=("lessons", "notes"), only=None):
    """→ one report per file: {name, file, archive, moved, bytes_before, bytes_after, backup}. Dry run unless apply.
    only: {name: entry texts}: for that file, nothing outside the list moves (core/memory_guard.py passes the lines the
    owner approved for lessons.md). The bridge never calls this for lessons.md without it.
    Under the brain/ lock (approvals.py appends drop reasons to lessons.md under the same lock), so no line is written in
    between. Order: copy of the file, archive written whole (atomically) with the moved lines appended, the file
    rewritten (atomically) without them, then both read back and compared. A crash between the two writes leaves a line in
    both places, never in neither; a failed check puts the copy back."""
    brain = pathlib.Path(brain or BRAIN)
    today = today or dt.date.today()
    out = []
    with store.locked(brain):
        for name in names:
            c = CORES[name]
            src, arc = brain / c["file"], brain / c["archive"]
            assert src.name not in NEVER and arc.name not in NEVER and src.parent == brain == arc.parent
            if not src.exists(): continue
            text = read(src)
            moved, rest = plan(name, text, today, (only or {}).get(name))
            rep = {"name": name, "file": src.name, "archive": arc.name, "moved": [e.text.strip() for e in moved],
                   "bytes_before": _size(text)[0], "bytes_after": _size(rest)[0], "backup": ""}
            out.append(rep)
            if not moved or not apply: continue
            before = read(arc) if arc.exists() else f"# {c['file']} archive: older lines moved out of the core, searchable with core/memory.py\n"
            block = f"\n## moved {today.isoformat()} from {c['file']} ({len(moved)} entries)\n" + "".join(e.text for e in moved)
            backup = _backup(brain, src)
            rep["backup"] = str(backup.relative_to(brain))
            store.write_text(arc, before.rstrip("\n") + "\n" + block)
            store.write_text(src, rest)
            ok_src = read(src) == rest
            ok_arc = read(arc).endswith(block) and all(e.text in read(arc) for e in moved)
            if not (ok_src and ok_arc):
                shutil.copy2(backup, src)
                raise RuntimeError(f"compact {src.name}: the check after moving failed; the file is back from {backup.name}")
    return out


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NUMBER = re.compile(r"#\s?\d+|\+?\d[\d ()/.-]{3,}\d")


def excerpt(entries, limit=10, width=90):
    """Short lines for a message to the owner: the date and the start of each entry. E-mail addresses and longer numbers
    (order numbers, phone numbers, amounts) are left out, so a message never carries a customer's data."""
    entries = [Entry(0, e.splitlines(keepends=True) or [""]) if isinstance(e, str) else e for e in entries]
    rows = []
    for e in entries[:limit]:
        first = e.text.strip().splitlines()[0]
        body = _DATE.sub("", first, count=1) if e.date else first
        body = body.lstrip("-*+ \t").lstrip(":–— ")
        body = _NUMBER.sub("[number]", _EMAIL.sub("[email]", body))
        rows.append(short(f"{e.date}: {body}" if e.date else body, width))
    if len(entries) > limit: rows.append(f"… and {len(entries) - limit} more")
    return rows


PIN_ADVICE = (f"A rule that must always apply: put {PIN} at the end of its line in brain/lessons.md; {PIN} lines are always "
              "in the prompt and never moved. Room for more: raise JINNEE_MEMORY_LESSONS_BYTES in .env")


# ---------------------------------------------------------------- command line

def _groups(raw):
    if not raw: return GROUPS
    got = tuple(g.strip() for g in raw.split(",") if g.strip())
    bad = [g for g in got if g not in GROUPS]
    if bad: raise SystemExit(f"--in: unknown {', '.join(bad)} (choose from {', '.join(GROUPS)})")
    return got


def main(argv=None, brain=None):
    brain = pathlib.Path(brain or BRAIN)
    try: sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError): pass
    p = argparse.ArgumentParser(prog="core/memory.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query", nargs="+"); s.add_argument("--limit", type=int, default=8); s.add_argument("--in", dest="groups")
    g = sub.add_parser("get"); g.add_argument("id")
    t = sub.add_parser("log-tail"); t.add_argument("n", nargs="?", type=int, default=20); t.add_argument("--full", action="store_true")
    ls = sub.add_parser("log-search"); ls.add_argument("query", nargs="+"); ls.add_argument("--limit", type=int, default=10)
    sub.add_parser("stats")
    c = sub.add_parser("core"); c.add_argument("name", choices=sorted(CORES))
    k = sub.add_parser("compact"); k.add_argument("--apply", choices=("yes",))
    a = p.parse_args(argv)

    if a.cmd in ("search", "log-search"):
        groups = ("decisions",) if a.cmd == "log-search" else _groups(a.groups)
        hits = search(" ".join(a.query), brain, a.limit, groups)
        if not hits:
            print("nothing found (try another word; the files searched: " + ", ".join(f"brain/{f}" for _, f, gr in SOURCES if gr in groups) + ")")
            return 1
        for d, score in hits: print(f"{d.id:<20} {short(d.text)}")
        return 0
    if a.cmd == "get":
        got = get(a.id, brain)
        if not got:
            print(f"no entry '{a.id}' (IDs look like lessons:12, as search prints them)"); return 1
        print(got[0]); print(got[1].rstrip("\n")); return 0
    if a.cmd == "log-tail":
        rows = tail(a.n, brain)
        if not rows: print("brain/decisions.log.md is empty or missing"); return 0
        for e in rows: print(f"decisions:{e.line:<10} " + (e.text.strip() if a.full else short(e.text)))
        return 0
    if a.cmd == "stats":
        for prefix, fname, _ in SOURCES:
            path = brain / fname
            if not path.exists(): print(f"brain/{fname:<24} missing"); continue
            _, entries = split(read(path))
            print(f"brain/{fname:<24} {path.stat().st_size:>8} bytes  {sum(1 for e in entries if e.text.strip()):>5} entries")
        advice = False
        for name in CORES:
            _, f = core(name, brain)
            if not f["hidden"]: state = "all shown"
            elif name == "lessons":
                state = f"{f['hidden']} older entries not shown; searchable, and moved to the archive only with the owner's /ok"
                advice = True
            else: state = f"{f['hidden']} entries not shown; searchable, the bridge moves them to the archive"
            print(f"core {name:<8} {f['bytes']}/{f['cap_bytes']} bytes, {f['lines']}/{f['cap_lines']} lines in the prompt ({state})")
        if advice: print(PIN_ADVICE + f" (now {cap('lessons')[0]} bytes).")
        return 0
    if a.cmd == "core":
        print(core(a.name, brain)[0], end=""); return 0
    if a.cmd == "compact":
        for rep in compact(brain, apply=a.apply == "yes"):
            if not rep["moved"]: print(f"brain/{rep['file']}: within the cap, nothing to move"); continue
            verb = "moved" if a.apply == "yes" else "would move"
            print(f"brain/{rep['file']}: {verb} {len(rep['moved'])} entries to brain/{rep['archive']} "
                  f"({rep['bytes_before']} → {rep['bytes_after']} bytes)" + (f", copy in brain/{rep['backup']}" if rep["backup"] else ""))
            if rep["name"] == "lessons":  # the owner's own rules: show which ones, and how to keep one
                for row in excerpt(rep["moved"]): print(f"  {row}")
                print("  " + PIN_ADVICE + f" (now {cap('lessons')[0]} bytes).")
        if a.apply != "yes": print("dry run: nothing was written. Add --apply yes to move.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
