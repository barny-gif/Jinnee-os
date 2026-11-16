"""Plain news for the owner that needs no decision: "a new order came in", "GLS picked the parcel up", "GLS transferred the COD,
I set the order paid". The order chain writes them (connectors/order_events.py); the Telegram bridge's minute check
(core/alerts.py) sends the unsent ones, outside the quiet hours, and the dashboard shows the latest.

Each notice has a key (for example 'new_order:#1001'): one key is said once, ever, so a restart or a repeated round never
repeats it. Only order numbers, payment type, amounts and states go in here: never a customer's name, address, phone or e-mail.
State: brain/notices.json {"items": [{"key", "text", "kind", "at", "sent_at"}]}, the newest KEEP kept."""
import pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store

FILE = "notices.json"
KEEP = 500        # keys remembered (a said key never comes back while it is among them)
PER_ROUND = 10    # at most this many notices in one minute check; the rest go a minute later


def _path(brain): return pathlib.Path(brain) / FILE


def _read(brain):
    try: d = store.read_json(_path(brain), dict)
    except store.Unreadable: d = {}  # a broken file: start again; worst case a notice is said twice
    items = d.get("items") if isinstance(d.get("items"), list) else []
    return {"items": [i for i in items if isinstance(i, dict) and i.get("key")]}


def add(brain, key, text, kind="info", now=None):
    """Queue one notice. → True when it is new, False when this key was already queued or said."""
    key, text = str(key).strip(), str(text or "").strip()
    if not key or not text: return False
    with store.locked(brain):
        d = _read(brain)
        if any(i["key"] == key for i in d["items"]): return False
        d["items"].append({"key": key, "text": text, "kind": kind, "at": int(now or time.time()), "sent_at": None})
        d["items"] = d["items"][-KEEP:]
        store.write_json(_path(brain), d)
    return True


def known(brain, key):
    return any(i["key"] == key for i in _read(brain)["items"])


def pending(brain, limit=PER_ROUND):
    """The unsent notices, oldest first."""
    return [i for i in _read(brain)["items"] if not i.get("sent_at")][:limit]


def mark_sent(brain, keys, now=None):
    keys = set(keys or [])
    if not keys: return
    with store.locked(brain):
        d = _read(brain)
        for i in d["items"]:
            if i["key"] in keys and not i.get("sent_at"): i["sent_at"] = int(now or time.time())
        store.write_json(_path(brain), d)


def recent(brain, n=20):
    """The newest n, newest first: for the dashboard."""
    return [{k: i.get(k) for k in ("key", "text", "kind", "at", "sent_at")} for i in _read(brain)["items"][-n:]][::-1]
