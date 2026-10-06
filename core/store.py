"""File plumbing for brain/: one lock for every writer, atomic JSON writes, and reads that survive a broken file.
The bridge and the dashboard are separate processes writing the same files, so nothing writes brain/*.json except through here."""
import contextlib, json, os, pathlib, tempfile, threading, time

try:
    import fcntl
    def _lock(f): fcntl.flock(f, fcntl.LOCK_EX)
    def _unlock(f): fcntl.flock(f, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt
    def _lock(f):
        while True:  # LK_LOCK gives up after about 10 seconds; keep waiting
            try: f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1); return
            except OSError: time.sleep(0.05)
    def _unlock(f): f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

_held = threading.local()


class Unreadable(Exception):
    """The file exists but is not what it should be (cut off, hand-edited into invalid JSON, wrong shape)."""


@contextlib.contextmanager
def locked(brain):
    """Exclusive across processes and threads; the same thread may nest. One lock file per brain/ keeps the order simple."""
    key = str(pathlib.Path(brain).resolve())
    depth = getattr(_held, "depth", None)
    if depth is None: depth = _held.depth = {}
    if depth.get(key):
        depth[key] += 1
        try: yield
        finally: depth[key] -= 1
        return
    pathlib.Path(brain).mkdir(parents=True, exist_ok=True)
    with open(pathlib.Path(brain) / ".lock", "a+b") as f:
        _lock(f); depth[key] = 1
        try: yield
        finally:
            depth[key] = 0; _unlock(f)


def read_json(path, kind):
    """A missing or empty file is an empty `kind` (list or dict). Anything unparseable or of the wrong type raises Unreadable."""
    try: text = pathlib.Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError: return kind()
    except (OSError, UnicodeDecodeError) as e: raise Unreadable(f"{pathlib.Path(path).name}: {e}")
    if not text.strip(): return kind()
    try: data = json.loads(text)
    except ValueError as e: raise Unreadable(f"{pathlib.Path(path).name} is not valid JSON ({e})")
    if not isinstance(data, kind): raise Unreadable(f"{pathlib.Path(path).name} should hold a {kind.__name__}")
    return data


def write_text(path, text):
    """Temp file in the same folder, then rename: a reader sees the old file or the new one, never half of either."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        for attempt in range(20):
            try: os.replace(tmp, path); return
            except PermissionError:  # Windows: a reader has the file open for a moment
                if attempt == 19: raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def set_aside(path):
    """Keep a broken file under a new name so the owner can look at it, and return that name."""
    path = pathlib.Path(path)
    kept = path.with_name(f"{path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
    os.replace(path, kept)
    return kept.name


def append_line(path, line):
    """One line at the end of a text file the owner and the agents also edit by hand."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(" ".join(str(line).split()) + "\n")
