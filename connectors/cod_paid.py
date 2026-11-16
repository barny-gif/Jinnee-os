"""Cash-on-delivery orders set to paid in Shopify only once GLS TRANSFERRED the money (owner's rule, 2026-11-16). Three things
must all hold: the parcel is at GLS status 05 (delivered), the COD amount on the label equals the order total, and the carrier
ledger (connectors/gls_ledger.py, read from GLS's own e-mails) lists this parcel in a COD settlement report ("Utánvét
részletező", the statement GLS sends with each COD payout) with the same amount, matched to the order without doubt.
Delivered alone is not enough: the courier has the cash, the shop does not yet.
- SHOPIFY_AUTO_MARK_PAID in .env: off (default) | queue | auto.
  queue: at most one approval item a day (action edit_shopify_order), listing the matching orders, each order queued once
         (brain/cod_paid.json). The owner's /ok is carried out with the `apply` op below, after `approvals.py consume`.
  auto:  writes only when the autonomy table has edit_shopify_order at level 2 or more; otherwise it falls back to queue
         and says so in the result.
- A COD amount that differs from the total (on the label or in the ledger) is never marked: it is reported (order number + amounts).
- The ledger cannot pair the order without doubt (two settlement rows, or the parcel number and the reference point to different
  orders): never marked, reported to the owner. Not in the ledger yet: waiting (normal for a few days); after
  COD_TRANSFER_WARN_DAYS (default 10) days since delivery it is reported as late.
- No carrier ledger set up (no brain/gls_ledger.json): nothing is marked, and the result says why.
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
LEDGER = HERE.parent / "brain" / "gls_ledger.json"      # the carrier ledger's state: "cod" = {parcel number: {ref, amount, ...}}
WARN_DAYS = 10  # COD_TRANSFER_WARN_DAYS: delivered this many days ago and still no settlement row → told to the owner
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

def ledger_cod():
    """The ledger's per-parcel COD settlement rows, or None when there is no ledger at all (not set up, unreadable)."""
    if not LEDGER.exists(): return None
    d = _read(LEDGER)
    return d.get("cod") if isinstance(d.get("cod"), dict) else None

def transfer(name, ship, cod_rows, total):
    """Did GLS transfer this order's COD money? → ("ok", {parcel, amount, delivered}) | ("wait", why) | ("unclear", why) |
    ("mismatch", why). cod_rows: ledger_cod(). The row is found by the COD reference (the order name, as on the label) and by
    the parcel number; both must point to one and the same row."""
    if cod_rows is None: return "wait", "no carrier ledger (GLS_LEDGER_ACCOUNT): a COD transfer cannot be seen"
    parcel = str((ship or {}).get("parcel_number") or "").strip()
    by_ref = {p for p, r in cod_rows.items() if isinstance(r, dict) and str(r.get("ref") or "").strip() == name}
    by_parcel = {parcel} if parcel and isinstance(cod_rows.get(parcel), dict) else set()
    found = by_ref | by_parcel
    if not found: return "wait", f"{name}: no GLS settlement row yet"
    if len(found) > 1:
        return "unclear", f"{name}: the GLS settlement lists more than one parcel for this order ({len(found)}), not marked: please check"
    (p,) = found; row = cod_rows[p]
    ref = str(row.get("ref") or "").strip()
    if ref and ref != name:
        return "unclear", f"{name}: its parcel is settled by GLS under another reference ({ref}), not marked: please check"
    if parcel and p != parcel:
        return "unclear", f"{name}: GLS settled a different parcel under this order's reference, not marked: please check"
    try: diff = abs(Decimal(str(row.get("amount"))) - Decimal(str(total)))
    except (InvalidOperation, ValueError, TypeError): return "unclear", f"{name}: the settled amount is not a number, not marked"
    if diff >= 1: return "mismatch", f"{name}: GLS transferred {forint(row.get('amount'))} Ft, order total {forint(total)} Ft"
    return "ok", {"parcel": p, "amount": forint(row.get("amount")), "delivered": row.get("delivered")}

def check(o, ship, label_entry, cod_rows=None):
    """→ ("ready", transfer row) | ("skip", why) | ("mismatch", why) | ("wait", why) | ("unclear", why). One order, no network."""
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
    verdict, info = transfer(name, ship, cod_rows, total)
    return ("ready", info) if verdict == "ok" else (verdict, info)

def _late(ship, now, days):
    t = str((ship or {}).get("delivered_at") or (ship or {}).get("status_at") or "")
    try: d = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError: return False
    d = d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    return now - d.timestamp() >= days * 86400

def warn_days():
    try: return max(int(os.getenv("COD_TRANSFER_WARN_DAYS", "") or WARN_DAYS), 1)
    except ValueError: return WARN_DAYS

def sort(raw, shipping, uploaded, cod_rows, now=None):
    """→ {"ready": [{name, id, total, parcel}], "mismatch": [text], "unclear": [text], "waiting": [names], "late": [text]}."""
    out = {"ready": [], "mismatch": [], "unclear": [], "waiting": [], "late": []}
    for o in raw or []:
        name = o.get("name") or ""
        ship = (shipping or {}).get(name)
        verdict, why = check(o, ship, (uploaded or {}).get(name), cod_rows)
        if verdict == "ready":
            out["ready"].append({"name": name, "id": str(o["id"]), "total": forint(o.get("total_price")), "parcel": why["parcel"]})
        elif verdict in ("mismatch", "unclear"): out[verdict].append(why)
        elif verdict == "wait":
            out["waiting"].append(name)
            if cod_rows is not None and now and _late(ship, now, warn_days()):
                out["late"].append(f"{name}: delivered more than {warn_days()} days ago, GLS has not settled its COD yet")
    return out

def select(raw, shipping, uploaded, cod_rows=None):
    """→ (ready [{name, id, total, parcel}], mismatches and unclear pairings [text])."""
    s = sort(raw, shipping, uploaded, cod_rows)
    return s["ready"], s["mismatch"] + s["unclear"]

def _money(n): return f"{n:,}".replace(",", " ") + " Ft"

def item_text(rows):
    lines = ["Utánvétes rendelések fizetettre állítása a Shopifyban. A GLS kézbesítette őket, és az utánvétet már át is utalta "
             "(benne van a GLS utánvét-részletezőjében), az összeg egyezik a végösszeggel:", ""]
    lines += [f"{r['name']} – {_money(r['total'])}" for r in rows]
    lines += ["", "Végrehajtás a GO után, írás előtt minden rendelést újra ellenőriz:",
              "python3 connectors/cod_paid.py apply --orders " + ",".join(r["id"] for r in rows) + " --confirm yes"]
    return "\n".join(lines)

def _book():
    import approvals
    root = HERE.parent
    return approvals.Book(root / "brain", root)

def run(raw, shipping, uploaded, now, act=False, shop=None, book=None, gate=None, cod_rows=False):
    """Called from the ecom_sync round. act=False: nothing is queued, written or marked; the result only lists.
    cod_rows: the ledger's settlement rows (default: read brain/gls_ledger.json).
    → {"mode", "ready": [names], "queued": approval id or None, "marked": [names], "mismatch": [text], "unclear": [text],
       "waiting": [names], "late": [text], "note", "errors"}"""
    m = mode()
    out = {"mode": m, "ready": [], "queued": None, "marked": [], "mismatch": [], "unclear": [], "waiting": [], "late": [],
           "marked_rows": [], "note": None, "errors": []}
    if m == "off" or raw is None: return out
    cod_rows = ledger_cod() if cod_rows is False else cod_rows
    state = _read(STATE)
    queued, paid = state.setdefault("queued", {}), state.setdefault("paid", {})
    sorted_ = sort(raw, shipping, uploaded, cod_rows, now)
    out.update(mismatch=sorted_["mismatch"], unclear=sorted_["unclear"], waiting=sorted_["waiting"], late=sorted_["late"])
    if cod_rows is None: out["note"] = "no carrier ledger (GLS_LEDGER_ACCOUNT): no COD order is set to paid until GLS's transfer can be seen"
    ready = [r for r in sorted_["ready"] if r["name"] not in paid]
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
            out["marked"].append(r["name"]); out["marked_rows"].append({"name": r["name"], "total": r["total"]})
        _write(STATE, state)
        return out
    new = [r for r in ready if r["name"] not in queued]
    today = stamp[:10]
    if not new or state.get("last_queue_day") == today: return out
    try:
        it, _ = (book or _book()).add("milo", ACTION, f"{len(new)} utánvétes rendelés fizetettre állítása (a GLS átutalta)",
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
    rows = ledger_cod()
    s = sort(raw, _read(GLS_STATUS), _read(UPLOADED), rows, dt.datetime.now(dt.timezone.utc).timestamp())
    return {"mode": mode(), "ledger": rows is not None, **s}

def apply(orders, confirm="no", shop=None):
    """Only after `approvals.py consume` printed GO. Every order is read again right before writing: still open, pending,
    cash on delivery, GLS 05, the label COD amount still equals the total, and the carrier ledger still shows GLS's transfer of
    the same amount for this parcel. Any change → that order is not written."""
    ids = [x.strip() for x in str(orders).split(",") if x.strip()]
    if not ids or not all(x.isdigit() for x in ids): raise ConnectorError("[COD paid] --orders: Shopify order ids (digits), comma separated")
    shop = shop or _shopify()
    shipping, uploaded, rows = _read(GLS_STATUS), _read(UPLOADED), ledger_cod()
    state = _read(STATE); paid = state.setdefault("paid", {})
    done, refused = [], []
    for oid in ids:
        o = shop._get(f"orders/{int(oid)}.json")["order"]
        name = o.get("name") or oid
        verdict, why = check(o, shipping.get(name), uploaded.get(name), rows)
        if verdict != "ready": refused.append(why if str(why).startswith(name) else f"{name}: {why}"); continue
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
