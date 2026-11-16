"""The key store: API keys and tokens encrypted on disk, read by the programs when they start.

Why: a plain .env is one careless copy away from a leak (a backup zip, a save under a mistyped name, an agent that reads
the folder). Here every value is encrypted with AES-256-GCM; .env keeps only settings that are not secret.

  config/secrets.vault.json   the encrypted values (one entry per key name; the name is bound to its value, so two
                              entries cannot be swapped). Not in git.
  master key                  32 random bytes, base64, in a file of its own (mode 600), found in this order:
                                1. JINNEE_MASTER_KEY_FILE, if set
                                2. /run/jinnee-keys/master.key  (Docker: the host folder in JINNEE_KEY_DIR is mounted there)
                                3. ~/.config/jinnee-os/master.key  (installed directly on a machine)
                              It is never inside the install folder by default, so a copy of that folder carries no key.

What it protects, and what it does not (README, "Your keys"): it protects against copies and leaks of the files (a
backup, a zip, a mistyped save, a commit, an agent or a person reading the folder). It does NOT protect against someone
who can read the master key file too: root on the server, a stolen full disk image, or a program running as the same
user. The running programs hold the keys in memory and in their environment.

Command line (a value is never an argument: it would stay in the shell history):
  python3 core/vault.py status              where the key and the store are, which names are stored (never the values)
  python3 core/vault.py set NAME            asks for the value (hidden), or reads it from stdin
  python3 core/vault.py delete NAME
  python3 core/vault.py import [--yes]      moves the secret lines of .env into the store, then removes them from .env
  python3 core/vault.py doctor              looks for keys left in plain text; exit 1 if it finds any
  python3 core/vault.py init                creates the master key if there is none (the installer does this)
"""
import base64, datetime, errno, getpass, hashlib, json, os, pathlib, re, secrets, stat, sys

try:
    import fcntl
except ImportError:  # Windows: no advisory lock; the writes are still atomic
    fcntl = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORMAT = 1
AAD = b"jinnee-vault/v1/"
DOCKER_KEY = pathlib.Path("/run/jinnee-keys/master.key")
NAME_OK = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Names that hold secrets. Anything ending like a setting (an id, an address, a path) does not, even if it says KEY.
SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|API_KEY|_KEY|PRIVATE|CREDENTIALS?)(_|$)", re.I)
NOT_SECRET = re.compile(r"_(ID|IDS|URL|URI|HOST|PORT|FILE|PATH|DIR|NAME|ENGINE|BASE|ADDRESS|MAILBOX|ACCOUNTS)$", re.I)
SCAN_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}
SCAN_MAX = 1024 * 1024
MIN_LEAK = 12  # shorter values are not searched for in files: too many false alarms


class VaultError(Exception):
    """Something the owner can fix; the message says what, and never contains a value."""


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name)) and not NOT_SECRET.search(name)


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise VaultError("the 'cryptography' package is missing: pip3 install -r requirements.txt") from None
    return AESGCM


# --- where things are
def vault_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("JINNEE_VAULT_FILE") or ROOT / "config" / "secrets.vault.json")


def key_path() -> pathlib.Path:
    if os.environ.get("JINNEE_MASTER_KEY_FILE"):
        return pathlib.Path(os.environ["JINNEE_MASTER_KEY_FILE"]).expanduser()
    if DOCKER_KEY.parent.is_dir():
        return DOCKER_KEY
    base = os.environ.get("XDG_CONFIG_HOME") or (os.environ.get("APPDATA") if os.name == "nt" else "") or pathlib.Path.home() / ".config"
    return pathlib.Path(base) / "jinnee-os" / "master.key"


def _write_private(path: pathlib.Path, data: bytes):
    """Atomic write, readable by the owner only (0600) from the first byte: never a moment with wider rights."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.replace(tmp, path)
        except OSError as e:
            # Docker: .env is a single file mounted from the host, which cannot be replaced, only rewritten in place
            if e.errno not in (errno.EBUSY, errno.EXDEV, errno.EPERM) or not path.exists():
                raise
            with open(path, "r+b") as fh:
                fh.seek(0)
                fh.write(data)
                fh.truncate()
            os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


# --- the master key
def _kid(key: bytes) -> str:
    """A short fingerprint of the key, kept in the store so a wrong key is named as such. Reveals nothing of the key."""
    return hashlib.sha256(b"jinnee-vault-kid/" + key).hexdigest()[:16]


def load_key(create: bool = False) -> bytes:
    p = key_path()
    if not p.exists():
        if not create:
            raise VaultError("no master key at %s. In Docker check JINNEE_KEY_DIR in .env; directly on a machine: "
                             "python3 core/vault.py init" % p)
        if p.parent.exists() is False:
            p.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _write_private(p, base64.b64encode(secrets.token_bytes(32)) + b"\n")
    try:
        key = base64.b64decode(p.read_bytes().strip(), validate=True)
    except (ValueError, OSError):
        raise VaultError("the master key at %s cannot be read" % p) from None
    if len(key) != 32:
        raise VaultError("the master key at %s is not a 32-byte key" % p)
    return key


# --- the store
class _Lock:
    def __init__(self, path):
        self.path = path.with_name(path.name + ".lock")
        self.fh = None

    def __enter__(self):
        if fcntl:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "a")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def _read_raw() -> dict:
    p = vault_path()
    if not p.exists():
        return {"format": FORMAT, "kid": None, "entries": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise VaultError("%s cannot be read (damaged?)" % p) from None
    if not isinstance(d, dict) or d.get("format") != FORMAT or not isinstance(d.get("entries"), dict):
        raise VaultError("%s is not a key store this version knows" % p)
    return d


def names() -> list:
    """The stored names. Needs no key: the names are not secret, the values are."""
    return sorted(_read_raw()["entries"])


def _check_kid(d, key):
    if d.get("kid") and d["kid"] != _kid(key):
        raise VaultError("the master key at %s is not the one this store was locked with" % key_path())


def get_all() -> dict:
    d = _read_raw()
    if not d["entries"]:
        return {}
    key = load_key()
    _check_kid(d, key)
    aes = _aesgcm()(key)
    out = {}
    for name, e in d["entries"].items():
        try:
            out[name] = aes.decrypt(base64.b64decode(e["nonce"]), base64.b64decode(e["ct"]), AAD + name.encode()).decode("utf-8")
        except Exception:
            raise VaultError("the stored value of %s cannot be opened (damaged, or moved from another entry)" % name) from None
    return out


def get(name: str):
    return get_all().get(name)


def set_many(values: dict) -> list:
    """Stores several values in one write. Returns the names that replaced an earlier value."""
    for n, v in values.items():
        if not NAME_OK.fullmatch(n):
            raise VaultError("%r is not a usable name (letters, digits and _ only)" % n)
        if not isinstance(v, str) or v == "":
            raise VaultError("%s: an empty value is not stored (use delete)" % n)
    with _Lock(vault_path()):
        d = _read_raw()
        key = load_key(create=not d["entries"])
        _check_kid(d, key)
        aes = _aesgcm()(key)
        replaced = []
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        for n, v in values.items():
            nonce = secrets.token_bytes(12)
            ct = aes.encrypt(nonce, v.encode("utf-8"), AAD + n.encode())
            if n in d["entries"]:
                replaced.append(n)
            d["entries"][n] = {"nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(ct).decode(), "updated": now}
        d["kid"] = _kid(key)
        _write_private(vault_path(), (json.dumps(d, indent=1, sort_keys=True) + "\n").encode())
    return replaced


def put(name: str, value: str) -> bool:
    """Stores one value. True if it replaced an earlier one."""
    return bool(set_many({name: value}))


def delete(name: str) -> bool:
    with _Lock(vault_path()):
        d = _read_raw()
        if name not in d["entries"]:
            return False
        del d["entries"][name]
        _write_private(vault_path(), (json.dumps(d, indent=1, sort_keys=True) + "\n").encode())
    return True


# --- at startup (core/env.py)
_loaded = {}


def load_into_environ() -> list:
    """Puts the stored values into os.environ. A name already set to something wins (Docker, export, a test);
    a name set to empty does not: an empty KEY= line left in .env must not hide the stored key."""
    p = vault_path()
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return []
    if _loaded.get("mtime") == mtime:
        new = {k: v for k, v in _loaded["values"].items() if not os.environ.get(k)}
    else:
        values = get_all()
        _loaded.update(mtime=mtime, values=values)
        new = {k: v for k, v in values.items() if not os.environ.get(k)}
    os.environ.update(new)
    return list(new)


# --- moving .env into the store
LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")


def env_file() -> pathlib.Path:
    return ROOT / ".env"


def plain_secrets(path=None) -> dict:
    """{name: value} of the secret lines with a value in a .env. Only for import; never printed."""
    import env as _env  # core/env.py: the same parser the programs use
    p = pathlib.Path(path or env_file())
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    return {k: v for k, v in _env.parse(text).items() if v and is_secret_name(k)}


def import_env(path=None) -> dict:
    """Encrypts the secret lines of .env into the store, checks each one reads back the same, and only then rewrites
    .env without them (a comment line says where each went). No plain copy of .env is left behind: the store is the copy.
    Returns {"moved": [...], "replaced": [...]}."""
    p = pathlib.Path(path or env_file())
    found = plain_secrets(p)
    if not found:
        return {"moved": [], "replaced": []}
    replaced = set_many(found)
    _loaded.clear()
    back = get_all()
    bad = [n for n, v in found.items() if back.get(n) != v]
    if bad:
        raise VaultError("these did not read back the same, .env was left as it was: " + ", ".join(sorted(bad)))
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    out = []
    for line in text.splitlines():
        m = None if line.lstrip().startswith("#") else LINE.fullmatch(line)
        if m and m.group(1) in found:
            out.append("# %s: in the key store (python3 core/vault.py status)" % m.group(1))
        else:
            out.append(line)
    _write_private(p, ("\n".join(out) + "\n").encode("utf-8"))  # 0600: what is left is settings, still nobody else's
    return {"moved": sorted(found), "replaced": sorted(replaced)}


# --- doctor: is anything left in plain text?
ENV_LIKE = re.compile(r"(^\.env)|(\.env$)|(^env\.bak)", re.I)
ENV_OK = {".env", ".env.example"}


def _inside(child: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def doctor(root=None, deep=True) -> list:
    """[(level, message)]; level is "problem" or "note". The messages name files and keys, never a value."""
    root = pathlib.Path(root or ROOT)
    out = []
    kp, vp = key_path(), vault_path()
    values = {}
    if vp.exists():
        try:
            values = get_all()
        except VaultError as e:
            out.append(("problem", "the key store cannot be opened: %s" % e))
    left = sorted(plain_secrets(root / ".env"))
    if left:
        out.append(("problem", ".env still holds keys in plain text: %s. Move them: python3 core/vault.py import"
                    % ", ".join(left)))
    for p, what in ((kp, "the master key"), (vp, "the key store"), (root / ".env", ".env")):
        try:
            mode = stat.S_IMODE(p.stat().st_mode)
        except OSError:
            continue
        if mode & 0o077 and os.name != "nt":
            out.append(("problem", "%s (%s) can be read by other users of this machine (mode %o): chmod 600 %s" % (what, p, mode, p)))
    host_dir = os.environ.get("JINNEE_KEY_DIR_HOST", "")  # Docker: the host folder behind /run/jinnee-keys
    if host_dir and not os.path.isabs(os.path.expanduser(host_dir)):
        out.append(("note", "the master key folder is %s, inside the install folder: a copy of the folder carries the key with "
                    "the store. Set JINNEE_KEY_DIR in .env to a folder outside it (README, \"Your keys\")." % host_dir))
    elif kp.exists() and _inside(kp, root):
        out.append(("note", "the master key is inside the install folder (%s): a copy of the folder carries the key with the "
                    "store, so the encryption protects nothing against that copy. Move it out (README, \"Your keys\")." % kp))
    if deep:
        leaks = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP]
            for fn in filenames:
                f = pathlib.Path(dirpath) / fn
                if f in (vp, kp) or f.name.startswith(vp.name):
                    continue
                rel = f.relative_to(root).as_posix()
                if ENV_LIKE.search(fn) and rel not in ENV_OK and not fn.endswith((".example", ".sample", ".template")):
                    leaks.setdefault(rel, []).append("a .env-like file")
                try:
                    if f.is_symlink() or f.stat().st_size > SCAN_MAX:
                        continue
                    raw = f.read_bytes()
                except OSError:
                    continue
                if b"\0" in raw[:4096]:
                    continue
                text = raw.decode("utf-8", "ignore")
                hit = sorted(n for n, v in values.items() if len(v) >= MIN_LEAK and v in text)
                if hit:
                    leaks.setdefault(rel, []).append("the value of " + ", ".join(hit))
        for rel, why in sorted(leaks.items()):
            out.append(("problem", "%s: %s in plain text" % (rel, "; ".join(why))))
    return out


# --- command line
def _cli(argv):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "status":
            kp, vp = key_path(), vault_path()
            print("master key: %s (%s)" % (kp, "present" if kp.exists() else "missing"))
            print("key store:  %s (%s)" % (vp, "present" if vp.exists() else "not created yet"))
            ns = names()
            print("stored:     %s" % (", ".join(ns) if ns else "nothing yet"))
            if kp.exists() and vp.exists() and os.stat(kp).st_dev == os.stat(vp).st_dev:
                print("note:       the key and the store are on the same disk: this guards against copies of the files, "
                      "not against someone who can read the whole disk (README, \"Your keys\").")
            return 0
        if cmd == "init":
            existed = key_path().exists()
            load_key(create=True)
            print("master key: %s (%s)" % (key_path(), "already there" if existed else "created"))
            return 0
        if cmd == "set" and len(argv) == 2:
            name = argv[1]
            value = getpass.getpass("Value for %s (not shown): " % name) if sys.stdin.isatty() else sys.stdin.read().strip()
            replaced = put(name, value)
            print("%s: %s" % (name, "replaced" if replaced else "stored") + ". Restart for it to take effect.")
            return 0
        if cmd == "delete" and len(argv) == 2:
            print("%s: %s" % (argv[1], "deleted" if delete(argv[1]) else "was not stored"))
            return 0
        if cmd == "import":
            found = sorted(plain_secrets())
            if not found:
                print("Nothing to move: .env holds no key in plain text.")
                return 0
            print("These keys move from .env into the encrypted store, and their lines are removed from .env:")
            print("  " + ", ".join(found))
            if "--yes" not in argv:
                if not sys.stdin.isatty():
                    print("Not moved: confirm with --yes, or run it in a terminal.")
                    return 1
                if input("Move them? [y/N] ").strip().lower() not in ("y", "yes", "i", "igen"):
                    print("Nothing changed.")
                    return 1
            r = import_env()
            print("Moved: %d. .env no longer holds them. Restart for the programs to read the store "
                  "(Docker: docker compose up -d)." % len(r["moved"]))
            return 0
        if cmd == "doctor":
            found = doctor()
            for level, msg in found:
                print("%s: %s" % (level.upper(), msg))
            problems = [m for lv, m in found if lv == "problem"]
            if not found:
                print("OK: no key in plain text, the store opens, the rights are right.")
            return 1 if problems else 0
    except VaultError as e:
        print("Key store: %s" % e, file=sys.stderr)
        return 2
    print(__doc__.split("Command line", 1)[1].split("\n", 1)[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
