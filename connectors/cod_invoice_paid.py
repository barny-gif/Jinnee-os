"""Cash-on-delivery invoices recorded as paid in Billingo once the carrier SETTLED the money (the carrier ledger,
connectors/gls_ledger.py, lists the parcel in a COD settlement report). The pair must hold without doubt:
- exactly one ledger row carries the order's reference (e.g. "#1001"), and its amount equals the invoice total (under 1 Ft);
- the invoice is issued, not cancelled, payment method cash on delivery, and not paid yet.
Anything else is left alone and listed (order number + amounts), never guessed.
- BILLINGO_AUTO_MARK_PAID in .env: off (default) | queue | auto.
  queue: at most one approval item a day (action issue_invoice), listing the invoices, each invoice queued once
         (brain/cod_invoice_paid.json). After `approvals.py consume` printed GO, the item's `apply` command is run.
  auto:  writes only when the autonomy table has issue_invoice at level 2 or more; otherwise it falls back to queue.
- The payment date written is the delivery date from the ledger (the day the customer paid the courier).
- Billingo is asked only when the ledger holds a settled COD row not handled yet, and at most once an hour.
Customer data is never read: invoice numbers, order numbers and amounts only.
CLI: python3 connectors/cod_invoice_paid.py candidates                                 (read only)
     python3 connectors/cod_invoice_paid.py apply --invoices 123,456 [--confirm yes]   (dry run without --confirm yes)"""
import sys, os, json, pathlib, datetime as dt, importlib.util
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "core"))
from cbase import ConnectorError, run_cli

STATE = HERE.parent / "brain" / "cod_invoice_paid.json"  # {"queued": {number: {...}}, "paid": {number: {...}}, "last_queue_day", "last_check"}
LEDGER = HERE.parent / "brain" / "gls_ledger.json"
MODES = ("off", "queue", "auto")
ACTION = "issue_invoice"
CHECK_EVERY = 3600  # seconds between two Billingo reads from the round

def mode():
    m = os.getenv("BILLINGO_AUTO_MARK_PAID", "off").strip().lower() or "off"
    return m if m in MODES else "off"  # an unknown value is off: fail closed

def _gate(action):
    try:
        import autonomy
        return autonomy.Config().allowed(action).level
    except Exception:
        return 0

def _read(path):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(path)

def ledger_rows():
    d = _read(LEDGER)
    return d.get("cod") if isinstance(d.get("cod"), dict) else None

def _num(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def sort(invoices, rows):
    """invoices: Billingo invoice rows (id, number, order, total, payment_status, payment_method); rows: the ledger's COD rows.
    → {"ready": [{id, number, order, total, date}], "mismatch": [text], "unclear": [text], "waiting": [order]}"""
    by_ref = {}
    for parcel, r in (rows or {}).items():
        ref = str(r.get("ref") or "").strip()
        if ref: by_ref.setdefault(ref, []).append(r)
    out = {"ready": [], "mismatch": [], "unclear": [], "waiting": []}
    for inv in invoices or []:
        if inv.get("payment_method") != "cash_on_delivery" or inv.get("payment_status") == "paid" or not inv.get("order"): continue
        order, total = inv["order"], _num(inv.get("total"))
        hits = by_ref.get(order, [])
        if not hits: out["waiting"].append(order); continue
        if len(hits) > 1: out["unclear"].append(f"{order}: {len(hits)} settlement rows"); continue
        amount = _num(hits[0].get("amount"))
        if total is None or amount is None or abs(total - amount) >= 1:
            out["mismatch"].append(f"{order} ({inv.get('number')}): invoice {total} Ft, settled {amount} Ft"); continue
        out["ready"].append({"id": str(inv["id"]), "number": inv.get("number"), "order": order, "total": total,
                             "date": hits[0].get("delivered") or dt.date.today().isoformat()})
    return out

def _money(n): return f"{int(round(n)):,}".replace(",", " ") + " Ft"

def item_text(rows):
    lines = ["Utánvétes számlák fizetettre állítása a Billingóban. A GLS kézbesítette a csomagot, és az utánvétet elszámolta "
             "(benne van a GLS utánvét-részletezőjében), az összeg egyezik a számlával:", ""]
    lines += [f"{r['number']} ({r['order']}) – {_money(r['total'])}, fizetés dátuma {r['date']}, mód: utánvét" for r in rows]
    lines += ["", "Végrehajtás a GO után, írás előtt minden számlát újra ellenőriz:",
              "python3 connectors/cod_invoice_paid.py apply --invoices " + ",".join(r["id"] for r in rows) + " --confirm yes"]
    return "\n".join(lines)

def _billingo():
    spec = importlib.util.spec_from_file_location("cip_billingo", HERE / "billingo" / "ops.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def _book():
    import approvals
    return approvals.Book(HERE.parent / "brain", HERE.parent)

def run(now, act=False, bill=None, book=None, gate=None, rows=False):
    """Called from the ecom_sync round. act=False: nothing is queued or written; the result only lists."""
    m = mode()
    out = {"mode": m, "ready": [], "queued": None, "marked": [], "mismatch": [], "unclear": [], "note": None, "errors": []}
    if m == "off": return out
    rows = ledger_rows() if rows is False else rows
    if not rows: out["note"] = "no carrier ledger: nothing is set to paid"; return out
    state = _read(STATE)
    queued, paid = state.setdefault("queued", {}), state.setdefault("paid", {})
    open_refs = {str(r.get("ref")) for r in rows.values()} - {v.get("order") for v in list(paid.values()) + list(queued.values())}
    if not open_refs or now - float(state.get("last_check") or 0) < CHECK_EVERY: return out
    bill = bill or _billingo()
    try: invoices = bill.payments()["invoices"]
    except Exception as e: out["errors"].append(f"[COD invoice paid: read Billingo] {type(e).__name__}: {e}"); return out
    s = sort(invoices, rows)
    ready = [r for r in s["ready"] if r["number"] not in paid]
    out.update(ready=[r["number"] for r in ready], mismatch=s["mismatch"], unclear=s["unclear"])
    if m == "auto" and (gate or _gate)(ACTION) < 2:
        m = "queue"; out["note"] = f"auto mode, but {ACTION} is below level 2: queued for approval instead"
    if not act: return out
    state["last_check"] = now
    stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds")
    if m == "auto":
        for r in ready:
            try: bill.mark_paid(r["id"], r["total"], "cash_on_delivery", r["date"])
            except Exception as e: out["errors"].append(f"[COD invoice paid {r['number']}] {e}"); continue
            paid[r["number"]] = {"id": r["id"], "order": r["order"], "total": r["total"], "date": r["date"], "at": stamp, "by": "auto"}
            out["marked"].append(r["number"])
        _write(STATE, state); return out
    new = [r for r in ready if r["number"] not in queued]
    today = stamp[:10]
    if not new or state.get("last_queue_day") == today: _write(STATE, state); return out
    try:
        it, _ = (book or _book()).add("ledger", ACTION, f"{len(new)} utánvétes számla fizetettre (a GLS elszámolta)",
                                      summary=", ".join(f"{r['number']} {r['order']} {_money(r['total'])}" for r in new),
                                      text=item_text(new), payload={"invoices": new},
                                      ref="_".join(r["order"].lstrip("#") for r in new[:3]) + "_cod_fizetett_szamla")
    except Exception as e:
        out["errors"].append(f"[COD invoice paid queue] {type(e).__name__}: {e}"); _write(STATE, state); return out
    for r in new: queued[r["number"]] = {"id": r["id"], "order": r["order"], "total": r["total"], "approval_id": it["id"], "at": stamp}
    state["last_queue_day"] = today
    _write(STATE, state)
    out["queued"] = it["id"]
    return out

def candidates(bill=None):
    """Read only: which COD invoices would be set paid, which differ, which wait for the settlement."""
    bill = bill or _billingo()
    rows = ledger_rows()
    return {"mode": mode(), "ledger": rows is not None, **sort(bill.payments()["invoices"], rows)}

def apply(invoices, confirm="no", bill=None):
    """Only after `approvals.py consume` printed GO. Each invoice is read again right before writing: still unpaid, cash on
    delivery, and the ledger still lists its order once with the same amount. Any change → that invoice is not written."""
    ids = [x.strip() for x in str(invoices).split(",") if x.strip()]
    if not ids or not all(x.isdigit() for x in ids): raise ConnectorError("[COD invoice paid] --invoices: Billingo document ids (digits), comma separated")
    bill = bill or _billingo()
    rows = ledger_rows()
    current = {str(i["id"]): i for i in bill.payments()["invoices"]}
    state = _read(STATE); paid = state.setdefault("paid", {})
    done, refused = [], []
    for i in ids:
        inv = current.get(i)
        if inv is None: refused.append(f"{i}: not found among the issued invoices"); continue
        s = sort([inv], rows)
        if not s["ready"]:
            why = (s["mismatch"] or s["unclear"] or [f"{inv.get('order')}: not settled yet" if s["waiting"] else f"{inv.get('number')}: already paid or not cash on delivery"])[0]
            refused.append(why); continue
        r = s["ready"][0]
        if confirm != "yes": done.append({"invoice": r["number"], "order": r["order"], "total": r["total"], "date": r["date"], "dry_run": True}); continue
        try: bill.mark_paid(r["id"], r["total"], "cash_on_delivery", r["date"])
        except Exception as e: refused.append(f"{r['number']}: {e}"); continue
        paid[r["number"]] = {"id": r["id"], "order": r["order"], "total": r["total"], "date": r["date"], "by": "approval",
                             "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        done.append({"invoice": r["number"], "order": r["order"], "total": r["total"], "date": r["date"], "written": True})
    if confirm == "yes" and done: _write(STATE, state)
    out = {"done": done, "refused": refused}
    if confirm != "yes": out["next"] = "run again with --confirm yes, only after approvals.py consume printed GO"
    return out

OPS = {"candidates": candidates, "apply": apply}
if __name__ == "__main__": sys.exit(run_cli(OPS))
