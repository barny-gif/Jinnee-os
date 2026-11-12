"""Cash-on-delivery orders set to paid in Shopify once GLS delivered them (owner's rule): the parcel is at GLS status 05
(delivered) and the COD amount on the label equals the order total, rounded to whole forints. Nothing else counts.
- SHOPIFY_AUTO_MARK_PAID in .env: off (default) | queue | auto.
  queue: at most one approval item a day (action edit_shopify_order), listing the matching orders, each order queued once
         (brain/cod_paid.json). The owner's /ok is carried out with the `apply` op below, after `approvals.py consume`.
  auto:  writes only when the autonomy table has edit_shopify_order at level 2 or more; otherwise it falls back to queue
         and says so in the result.
- A COD amount that differs from the total is never marked: it is reported (order number + the two amounts).
- Cancelled, Shopify test, 0 Ft and tagged (barter / influencer / test / gift) orders are never touched.
Customer data is never read here beyond what the order rows hold, and never printed: order numbers, ids and amounts only.
CLI: python3 connectors/cod_paid.py candidates                       (read only)
     python3 connectors/cod_paid.py apply --orders 123,456 [--confirm yes]   (dry run without --confirm yes)"""
import sys, os, json, pathlib, datetime as dt, importlib.util
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "core"))
from cbase import ConnectorError, run_cli

STATE = HERE.parent / "brain" / "cod_paid.json"        # {"queued": {name: {...}}, "paid": {name: {...}}, "last_queue_day": "YYYY-MM-DD"}
UPLOADED = HERE.parent / "brain" / "gls_uploaded.json"
GLS_STATUS = HERE.parent / "brain" / "gls_status.json"
MODES = ("off", "queue", "auto")
ACTION = "edit_shopify_order"
COD = ("cash on delivery", "cod", "utánvét", "utanvet")
MANUAL = ("", "manual")  # no gateway: an order made by hand from a draft is collected by the courier (team rule)
NOT_SALE = ("barter", "influencer", "teszt", "test", "ajándék", "ajandek", "gift")

def mode():
    m = os.getenv("SHOPIFY_AUTO_MARK_PAID", "off").strip().lower() or "off"
    return m if m in MODES else "off"  # an unknown value is off: fail closed

def _gate(action):
    try:
        import autonomy
        return autonomy.Config().allowed(action).level
    except Exception:
        return 0

def _read(path):
    try: d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
    return d if isinstance(d, dict) else {}

def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(path)

def forint(v):
    """Whole forints, half up (1 234,50 → 1 235). None when it is not a number."""
    try: return int(Decimal(str(v)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError): return None

def is_cod(o):
    g = [x for x in o.get("payment_gateway_names") or []]
    if any(c in " ".join(x for x in g if x).lower() for c in COD): return True
    return o.get("financial_status") == "pending" and all(str(x or "").strip().lower() in MANUAL for x in g or [""])

def _tagged(o):
    tags = [t.strip().lower() for t in str(o.get("tags") or "").split(",")]
    return any(t in NOT_SALE for t in tags)

def label_cod(entry):
    return entry.get("cod_amount") if isinstance(entry, dict) and isinstance(entry.get("cod_amount"), (int, float)) else None

def check(o, ship, label_entry):
    """→ ("ready", None) | ("skip", why) | ("mismatch", why). One order, no network."""
    name = o.get("name") or ""
    if o.get("cancelled_at"): return "skip", "cancelled"
    if o.get("test") or _tagged(o) or (forint(o.get("total_price")) or 0) <= 0: return "skip", "not a sale (test / tagged / 0 Ft)"
    if o.get("financial_status") != "pending": return "skip", f"financial status {o.get('financial_status')}"
    if not is_cod(o): return "skip", "not cash on delivery"
    if (ship or {}).get("state") != "delivered" or str((ship or {}).get("code") or "05") != "05": return "skip", "not delivered (GLS 05)"
    cod, total = label_cod(label_entry), o.get("total_price")
    if cod is None: return "mismatch", f"{name}: the COD amount on the label is not known, order total {forint(total)} Ft"
    # whole forints on the label: a total ending in ,50 may be rounded either way (the label rounds half to even), so less than
    # 1 Ft apart is the same amount; anything more is a real difference and is never marked
    try: diff = abs(Decimal(str(cod)) - Decimal(str(total)))
    except (InvalidOperation, ValueError, TypeError): return "mismatch", f"{name}: the order total is not a number"
    if diff >= 1: return "mismatch", f"{name}: label COD {forint(cod)} Ft, order total {forint(total)} Ft"
    return "ready", None

def select(raw, shipping, uploaded):
    """→ (ready [{name, id, total}], mismatches [text])."""
    ready, mism = [], []
    for o in raw or []:
        name = o.get("name") or ""
        verdict, why = check(o, (shipping or {}).get(name), (uploaded or {}).get(name))
        if verdict == "ready": ready.append({"name": name, "id": str(o["id"]), "total": forint(o.get("total_price"))})
        elif verdict == "mismatch": mism.append(why)
    return ready, mism

def _money(n): return f"{n:,}".replace(",", " ") + " Ft"

def item_text(rows):
    lines = ["Utánvétes rendelések fizetettre állítása a Shopifyban (GLS 05 kézbesítve, a címke COD-összege egyezik a végösszeggel):", ""]
    lines += [f"{r['name']} – {_money(r['total'])}" for r in rows]
    lines += ["", "Végrehajtás a GO után, írás előtt minden rendelést újra ellenőriz:",
              "python3 connectors/cod_paid.py apply --orders " + ",".join(r["id"] for r in rows) + " --confirm yes"]
    return "\n".join(lines)

def _book():
    import approvals
    root = HERE.parent
    return approvals.Book(root / "brain", root)

def run(raw, shipping, uploaded, now, act=False, shop=None, book=None, gate=None):
    """Called from the ecom_sync round. act=False: nothing is queued, written or marked; the result only lists.
    → {"mode", "ready": [names], "queued": approval id or None, "marked": [names], "mismatch": [text], "note", "errors"}"""
    m = mode()
    out = {"mode": m, "ready": [], "queued": None, "marked": [], "mismatch": [], "note": None, "errors": []}
    if m == "off" or raw is None: return out
    state = _read(STATE)
    queued, paid = state.setdefault("queued", {}), state.setdefault("paid", {})
    ready, out["mismatch"] = select(raw, shipping, uploaded)
    ready = [r for r in ready if r["name"] not in paid]
    out["ready"] = [r["name"] for r in ready]
    if m == "auto" and (gate or _gate)(ACTION) < 2:
        m = "queue"; out["note"] = f"auto mode, but {ACTION} is below level 2: queued for approval instead"
    if not act or not ready: return out
    stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds")
    if m == "auto":
        if shop is None: raise ConnectorError("[COD paid] no Shopify connector")
        for r in ready:
            try: res = shop.mark_paid(r["id"], confirm="yes")
            except ConnectorError as e: out["errors"].append(f"[COD paid {r['name']}] {e}"); continue
            paid[r["name"]] = {"order_id": r["id"], "total": r["total"], "at": stamp, "by": "auto", "already": bool(res.get("already"))}
            out["marked"].append(r["name"])
        _write(STATE, state)
        return out
    new = [r for r in ready if r["name"] not in queued]
    today = stamp[:10]
    if not new or state.get("last_queue_day") == today: return out
    try:
        it, _ = (book or _book()).add("milo", ACTION, f"{len(new)} utánvétes rendelés fizetettre állítása (GLS 05)",
                                      summary=", ".join(f"{r['name']} {_money(r['total'])}" for r in new), text=item_text(new),
                                      payload={"orders": new},
                                      # readable /ok id: the order number(s), COD, what /ok does (set paid)
                                      ref="_".join(r["name"].lstrip("#") for r in new[:3]) + "_cod_fizetett")
    except Exception as e:
        out["errors"].append(f"[COD paid queue] {type(e).__name__}: {e}"); return out
    for r in new: queued[r["name"]] = {"order_id": r["id"], "total": r["total"], "approval_id": it["id"], "at": stamp}
    state["last_queue_day"] = today
    _write(STATE, state)
    out["queued"] = it["id"]
    return out

def _shopify():
    spec = importlib.util.spec_from_file_location("cod_shopify", HERE / "shopify" / "ops.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def candidates(shop=None):
    """Read only: which delivered COD orders would be marked, and which differ. Order numbers and amounts only."""
    shop = shop or _shopify()
    raw = shop._get("orders.json", {"status": "open", "financial_status": "pending", "limit": 250,
                                    "fields": "id,name,total_price,financial_status,cancelled_at,payment_gateway_names,test,tags"}).get("orders", [])
    ready, mism = select(raw, _read(GLS_STATUS), _read(UPLOADED))
    return {"mode": mode(), "ready": ready, "mismatch": mism}

def apply(orders, confirm="no", shop=None):
    """Only after `approvals.py consume` printed GO. Every order is read again right before writing: still open, pending,
    cash on delivery, GLS 05, and the label COD amount still equals the total. Any change → that order is not written."""
    ids = [x.strip() for x in str(orders).split(",") if x.strip()]
    if not ids or not all(x.isdigit() for x in ids): raise ConnectorError("[COD paid] --orders: Shopify order ids (digits), comma separated")
    shop = shop or _shopify()
    shipping, uploaded = _read(GLS_STATUS), _read(UPLOADED)
    state = _read(STATE); paid = state.setdefault("paid", {})
    done, refused = [], []
    for oid in ids:
        o = shop._get(f"orders/{int(oid)}.json")["order"]
        name = o.get("name") or oid
        verdict, why = check(o, shipping.get(name), uploaded.get(name))
        if verdict != "ready": refused.append(f"{name}: {why}"); continue
        if confirm != "yes": done.append({"order": name, "total": forint(o.get("total_price")), "dry_run": True}); continue
        try: res = shop.mark_paid(oid, confirm="yes")
        except ConnectorError as e: refused.append(f"{name}: {e}"); continue
        paid[name] = {"order_id": oid, "total": forint(o.get("total_price")), "by": "approval",
                      "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "already": bool(res.get("already"))}
        done.append({"order": name, "total": forint(o.get("total_price")), "written": True, "status": res.get("status")})
    if confirm == "yes" and done: _write(STATE, state)
    out = {"done": done, "refused": refused}
    if confirm != "yes": out["next"] = "run again with --confirm yes, only after approvals.py consume printed GO"
    return out

OPS = {"candidates": candidates, "apply": apply}
if __name__ == "__main__": sys.exit(run_cli(OPS))
