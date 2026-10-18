"""The automatic start of the order chain (owner, 2026-10-19): a new order gets its MyGLS label uploaded at once, the right way
(PAID: no COD, COD: with the order total), and its Billingo invoice DRAFT made, unless a required field is missing (house number,
phone, zip, city, name...): then nothing is done for that step and the site / Telegram show which field (never the data).
- The label goes only when `upload_parcel_label` is level 2 or more in the autonomy table (default 1: off). The draft goes only
  when `draft_anything` is level 2 or more. Issuing and e-mailing the invoice are NOT here: they stay behind the approval queue.
- No back-fill: the first time the label switch is found on (in a real run), the moment is written to brain/order_auto_start.json;
  only orders created after it are handled. ORDER_AUTO_SINCE (ISO date/time) in .env asks for older ones on purpose.
- Once per order and step (brain/order_auto.json); a failure is recorded and retried after RETRY seconds, never in a loop.
- Cancelled, Shopify test and 0 Ft orders (possible barter / influencer / gift) are never touched.
- act=False (dry run): nothing is uploaded, created or written; the names are only listed.
Customer data passes through order_flow only and is never printed: the result holds order numbers, ids and field names."""
import sys, os, json, pathlib, datetime as dt, importlib.util
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "core"))
from cbase import ConnectorError
import order_checks

STATE = HERE.parent / "brain" / "order_auto.json"        # order name -> {"label": {...}, "draft": {...}}
START = HERE.parent / "brain" / "order_auto_start.json"  # {"since": iso}: when the automatic label was first switched on
RETRY = 1800  # a failed step is tried again after 30 minutes

def _flow():
    spec = importlib.util.spec_from_file_location("auto_order_flow", HERE / "order_flow.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def _gate(action):
    """→ level from the autonomy table (0 when it cannot be read: fail closed)."""
    try:
        import autonomy
        return autonomy.Config().allowed(action).level
    except Exception:
        return 0

def _iso(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")

def _ts(value):
    try: d = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError: return None
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()

def _read(path):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(path)

def _since(now, write):
    env = os.getenv("ORDER_AUTO_SINCE", "").strip()
    if env:
        t = _ts(env)
        return (t, None) if t is not None else (None, "[order auto] ORDER_AUTO_SINCE is not a date: nothing done")
    t = _ts(_read(START).get("since", ""))
    if t is not None: return t, None
    if write: _write(START, {"since": _iso(now), "note": "automatic MyGLS label / invoice draft switched on; older orders are not back-filled"})
    return now, None

def run(raw, now, act=False, uploaded=None, flow=None, gate=None):
    """raw: Shopify orders (raw, as ecom_sync reads them). uploaded: brain/gls_uploaded.json content.
    → {"enabled": {...}, "labels": [names], "drafts": [names], "held": {name: [step: field]}, "errors": [...]}"""
    gate = gate or _gate
    out = {"enabled": {"label": gate("upload_parcel_label") >= 2, "draft": gate("draft_anything") >= 2},
           "labels": [], "drafts": [], "held": {}, "errors": []}
    if raw is None or not (out["enabled"]["label"] or out["enabled"]["draft"]): return out
    since, err = _since(now, write=act)
    if err: out["errors"].append(err); return out
    state, changed, uploaded = _read(STATE), False, uploaded or {}
    try:
        for o in raw:
            name = o.get("name") or ""
            created = _ts(o.get("created_at") or "")
            if not name or created is None or created < since: continue
            if o.get("cancelled_at") or o.get("test") or float(o.get("total_price") or 0) <= 0: continue
            problems = order_checks.check(o)
            rec = state.get(name) or {}
            for step, key, enabled, done_already in (("label", "label", out["enabled"]["label"], name in uploaded),
                                                     ("invoice", "draft", out["enabled"]["draft"], False)):
                if not enabled or done_already or (rec.get(key) or {}).get("at"): continue
                if not order_checks.ready(problems, step):
                    out["held"].setdefault(name, []).extend(f"{p['step']}: {p['label']}" for p in problems
                                                            if p["level"] == "stop" and p["step"] in (step, "order"))
                    continue
                last = rec.get(key) or {}
                if last.get("error") and now - last.get("tried", 0) < RETRY: out["errors"].append(last["error"]); continue
                if not act: out["labels" if key == "label" else "drafts"].append(name); continue
                flow = flow or _flow()
                try:
                    if key == "label":
                        r = flow.parcel_prepare(str(o["id"]), send="yes")
                        done = {"at": _iso(now), "parcel_id": ((r.get("uploaded") or [{}])[0] or {}).get("parcel_id") or r.get("parcel_id"),
                                "skipped": r.get("skipped")}
                    else:
                        r = flow.invoice_draft(str(o["id"]))
                        done = {"at": _iso(now), "draft_id": r.get("draft_id"), "skipped": r.get("skipped")}
                except ConnectorError as e:
                    rec[key] = {"error": f"[order auto {key} {name}] {e}", "tried": int(now)}
                    out["errors"].append(rec[key]["error"])
                else:
                    rec[key] = done
                    if not done.get("skipped"): out["labels" if key == "label" else "drafts"].append(name)
                state[name] = rec; changed = True
    finally:
        if changed: _write(STATE, state)
    for k in out["held"]: out["held"][k] = list(dict.fromkeys(out["held"][k]))
    return out
