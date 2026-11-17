"""The dashboard's "Your keys" box, in plain words: which services can take a key, the technical name a typed name becomes,
the local log of what was added, replaced or deleted (never a value), a write limit, and the "Try it" check.

The store itself is core/vault.py (AES-256-GCM); this module never holds a value longer than one call and never writes one.

  catalog()            the services from connectors/*.manifest.json (plus Jinnee itself), each key with a human label
  tech_name(label)     "Saját CRM api" -> "SAJAT_CRM_API_KEY": accents off, upper case, _ between words, a key-like ending
  record(...)          one line in config/secrets.audit.jsonl: name, action, time, client IP. No value, ever.
  history()            {name: the last line about it}; the dashboard shows the date from it
  Limiter              at most N writes per minute per client
  check(name, value)   one cheap, read-only call of the connector that uses the key, run as a child process
                       (core/keycheck.py); only "works / does not work + why" comes back
"""
import collections, datetime, json, os, pathlib, re, subprocess, sys, threading, time, unicodedata

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import texts, vault

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ("TELEGRAM_BOT_TOKEN", "DASHBOARD_PASSWORD", "GITHUB_TOKEN")  # Jinnee's own keys, not a connector's
CORE_ID, OWN_ID = "jinnee", "own"
NAME_MAX = 60
CHECK_TIMEOUT = 25  # seconds; a service that does not answer by then counts as "does not work"
AUDIT_READ_MAX = 2 * 1024 * 1024
ACTIONS = ("add", "replace", "delete", "import")


# --- which services can take a key
def manifests(root=None):
    out = []
    for p in sorted(pathlib.Path(root or ROOT).joinpath("connectors").glob("*.manifest.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # a broken manifest hides one service, not the box
    return out


def _auth_names(d):
    a = d.get("auth") or {}
    envs = a.get("env") or []
    envs = [envs] if isinstance(envs, str) else list(envs)
    return envs + [n for g in a.get("env_any") or [] for n in g if n not in envs]


def _prefix(service_id):
    return re.sub(r"[^A-Z0-9]+", "_", service_id.upper()) + "_"


def label(name, service_id=""):
    """The human name of one key in the owner's language: locale dash.kl_<name> if there is one, else made from the name."""
    k = "dash.kl_" + name.lower()
    if k in texts.table(texts.BASE):
        return texts.t(k)
    rest = name[len(_prefix(service_id)):] if service_id and name.startswith(_prefix(service_id)) else name
    rest = rest.replace("_", " ").strip().lower()
    return (rest[:1].upper() + rest[1:]) or name


def catalog(root=None):
    """[{id, name, keys: [{name, label, check}]}]: every connector with a secret in its manifest, then Jinnee itself.
    Only secret names (core/vault.py is_secret_name): settings such as a store handle or a user name stay in .env."""
    out = []
    for d in manifests(root):
        if not d.get("id") or not d.get("name"):
            continue
        keys = [n for n in _auth_names(d) if vault.is_secret_name(n)]
        if keys:
            can = bool(check_spec(d, root))
            out.append({"id": d["id"], "name": d["name"],
                        "keys": [{"name": n, "label": label(n, d["id"]), "check": can} for n in keys]})
    out.sort(key=lambda s: s["name"].lower())
    out.append({"id": CORE_ID, "name": texts.t("dash.key_core"),
                "keys": [{"name": n, "label": label(n), "check": False} for n in CORE]})
    return out


def owner_of(name, cat):
    """(service id, service name) of a key: the catalog first, then a name that starts like a service (GMAIL_SHOP_REFRESH_TOKEN
    belongs to Gmail). ('', '') when nothing uses it: one of the owner's own keys."""
    for s in cat:
        if any(k["name"] == name for k in s["keys"]):
            return s["id"], s["name"]
    for s in cat:
        if s["id"] != CORE_ID and name.startswith(_prefix(s["id"])):
            return s["id"], s["name"]
    return "", ""


# --- a typed name -> a technical name
_TAIL = {"KULCS", "KULCSA", "KULCSOM", "KULCSUNK", "KEY"}  # "api kulcs" and "api key" both end up as ..._API_KEY


def tech_name(text) -> str:
    """'Saját CRM api' -> 'SAJAT_CRM_API_KEY'. Accents removed, upper case, every run of other characters one _, and an
    ending that marks it as a key (so the store's leak check covers it). ValueError if nothing usable is left."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    words = [w for w in re.split(r"[^A-Za-z0-9]+", s.upper()) if w]
    while words and words[-1] in _TAIL:
        words.pop()
    if words and words[-1] == "JELSZO":
        words[-1] = "PASSWORD"
    if not words:
        raise ValueError("no letters or digits")
    name = "_".join(words)[:NAME_MAX - 8].strip("_")
    if name[0].isdigit():
        name = "KEY_" + name
    if not vault.is_secret_name(name):
        name += "_KEY" if name.endswith("_API") or name == "API" else "_API_KEY"
    if not vault.NAME_OK.fullmatch(name):  # cannot happen with the rules above; the store would refuse it anyway
        raise ValueError("not a usable name")
    return name


# --- the local log
def audit_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("JINNEE_KEY_AUDIT_FILE") or vault.vault_path().with_name("secrets.audit.jsonl"))


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def record(name, action, ip, label_text=""):
    """One line: {"at", "name", "action", "ip", "via"} (+ "label" for the owner's own keys). There is no value parameter on
    purpose: nothing here can write one. The file is the owner's only (0600) and only ever appended to."""
    if action not in ACTIONS:
        raise ValueError(action)
    line = {"at": now_iso(), "name": str(name), "action": action, "ip": str(ip or "?")[:64], "via": "dashboard"}
    if label_text:
        line["label"] = str(label_text)[:NAME_MAX]
    p = audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return line


def history() -> dict:
    """{name: the newest log line about it}. A missing or partly broken file is not an error: those lines are skipped."""
    p = audit_path()
    try:
        with open(p, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - AUDIT_READ_MAX))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return {}
    out = {}
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("name") and d.get("action") in ACTIONS:
            prev = out.get(d["name"], {})
            out[d["name"]] = {**d, "label": d.get("label") or prev.get("label", "")} if d["action"] != "delete" else d
    return out


def stored_dates() -> dict:
    """{name: when the store last wrote it}, for keys set before the log existed or from the command line."""
    try:
        return {n: e.get("updated", "") for n, e in vault._read_raw()["entries"].items()}
    except vault.VaultError:
        return {}


# --- write limit
class Limiter:
    """At most `limit` calls per `window` seconds per client. In memory: a restart clears it, which is fine for a limit."""

    def __init__(self, limit=10, window=60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, who) -> bool:
        with self.lock:
            now = self.clock()
            q = self.hits[who]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self.hits) > 10000:  # many clients: forget the quiet ones
                for k in [k for k, v in self.hits.items() if not v]:
                    del self.hits[k]
            return True


# --- "Try it"
def check_spec(manifest, root=None):
    """The manifest's "check": {"op": "...", "args": {...}, "clear": [...]}, if its connector code has that function."""
    c = manifest.get("check")
    if not isinstance(c, dict) or not isinstance(c.get("op"), str):
        return None
    ops = pathlib.Path(root or ROOT) / "connectors" / manifest.get("id", "") / "ops.py"
    try:
        if not re.search(r"^def %s\(" % re.escape(c["op"]), ops.read_text(encoding="utf-8"), re.M):
            return None
    except OSError:
        return None
    return c


def _scrub(text, value):
    text = str(text or "")
    if value and len(value) >= 4:
        text = text.replace(value, "***")
    return text[:240]


def check(name, value=None, root=None):
    """One read-only call of the connector that uses `name`, with `value` in place of the stored key (None: the stored one).
    {"ok": bool, "reason": str}; never the connector's answer itself. The value goes to the child on stdin, not on its
    command line (which other users of the machine can see)."""
    for d in manifests(root):
        if name not in _auth_names(d):
            continue
        spec = check_spec(d, root)
        if not spec:
            break
        # a key of one login group is tried on its own: SHOPIFY_CLIENT_SECRET must not be hidden by a SHOPIFY_ADMIN_TOKEN
        groups = (d.get("auth") or {}).get("env_any") or []
        mine = next((g for g in groups if name in g), [])
        clear = sorted({n for g in groups for n in g if mine and n not in mine} | set(spec.get("clear") or []))
        job = {"connector": d["id"], "op": spec["op"], "args": spec.get("args") or {}, "clear": clear,
               "set": {name: value} if value else {}, "root": str(pathlib.Path(root or ROOT))}
        try:
            r = subprocess.run([sys.executable, str(ROOT / "core" / "keycheck.py")], input=json.dumps(job), text=True,
                               capture_output=True, timeout=CHECK_TIMEOUT, cwd=str(pathlib.Path(root or ROOT)))
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": texts.t("dash.key_test_timeout", s=CHECK_TIMEOUT)}
        lines = [l for l in (r.stdout or "").splitlines() if l.startswith("{")]
        try:
            out = json.loads(lines[-1])
        except (IndexError, ValueError):
            return {"ok": False, "reason": texts.t("dash.key_test_broken")}
        if out.get("ok") and out.get("missing"):
            return {"ok": True, "reason": _scrub(texts.t("dash.key_test_missing", names=", ".join(map(str, out["missing"]))), value)}
        return {"ok": bool(out.get("ok")), "reason": _scrub(out.get("reason"), value)}
    raise LookupError(name)
