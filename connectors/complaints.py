"""Customer complaints -> the admin site's Complaints page (owner, 2026-10-27). Echo records each complaint in
brain/complaints.json with this CLI; ecom_sync sends the list with every round.
What goes: complaint id, order number, date, products, a summary with no customer name, the status, the approval id and the
reply's subject. The reply text and the customer's e-mail go only once the owner approved the reply (Telegram /ok), and only
the text exactly as approved (the file's fingerprint must match the approval's); they land in the site's protected table.
The e-mail is the order's, from the orders this round already read: nothing extra is asked from Shopify. A complaint whose
order is not among them gets no e-mail key, so the site keeps what it has.
Status: the one Echo recorded (reply_sent, exchange_in_progress, closed) wins; otherwise it follows the approval:
pending -> reply_waiting_approval, approved -> reply_ready, sent back or dropped -> new. No approval -> new.
The CLI never prints the reply text or an e-mail address.
CLI: python3 connectors/complaints.py record --id 2026-10-27-1032 --order "#1032" --received_at 2026-10-26T18:00:00+01:00
                 --products "1× Zsiráf Kalandkönyv, 1× Farmer Kalandkönyv" --summary "…" --subject "…"
                 --reply_file handoffs/echo/….md --approval surp [--status reply_sent]
     python3 connectors/complaints.py show"""
import sys, json, hashlib, pathlib, datetime as dt
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / "core"))
from cbase import ConnectorError, run_cli

STATE = ROOT / "brain" / "complaints.json"
STATUSES = ("new", "reply_waiting_approval", "reply_ready", "reply_sent", "exchange_in_progress", "closed")
SET_BY_ECHO = ("reply_sent", "exchange_in_progress", "closed")
VISIBLE = ("reply_ready", "reply_sent", "exchange_in_progress", "closed")  # the site shows the reply only in these
FIELDS = {"order": "order_name", "received_at": "received_at", "products": "products", "summary": "summary",
          "subject": "reply_subject", "reply_file": "reply_file", "approval": "approval_id", "status": "status"}

def _read():
    try: d = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
    return d if isinstance(d, dict) else {}

def _write(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp"); tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(STATE)

def _book():
    import approvals
    return approvals.Book(ROOT / "brain", ROOT)

def _reply_path(rel):
    p = (ROOT / str(rel)).resolve()
    if not p.is_relative_to((ROOT / "handoffs").resolve()): raise ConnectorError(f"[complaint] the reply file must be under handoffs/: {rel}")
    return p

def record(id, **kw):
    """Creates or updates one complaint. Only the keys given change; --status '' clears Echo's own status."""
    if not id.strip(): raise ConnectorError("[complaint] --id is required")
    unknown = sorted(set(kw) - set(FIELDS))
    if unknown: raise ConnectorError(f"[complaint] unknown option: {', '.join(unknown)}")
    if kw.get("status") and kw["status"] not in SET_BY_ECHO: raise ConnectorError(f"[complaint] --status is one of {', '.join(SET_BY_ECHO)} (the rest follows the approval)")
    if kw.get("received_at"):
        try: dt.datetime.fromisoformat(kw["received_at"])
        except ValueError: raise ConnectorError("[complaint] --received_at must be an ISO date/time with offset")
    if kw.get("reply_file"): _reply_path(kw["reply_file"])
    d = _read(); row = d.setdefault(id, {})
    for k, v in kw.items():
        if v == "" and k == "status": row.pop("status", None)
        else: row[FIELDS[k]] = v
    _write(d)
    return show(id)

def _status(row, item):
    if row.get("status") in SET_BY_ECHO: return row["status"]
    if not row.get("approval_id"): return "new"
    if item is None: return None  # the approval left the queue (30 days): the site keeps what it has
    if item["state"] == "pending": return "reply_waiting_approval"
    return "reply_ready" if item["decision"] == "ok" else "new"

def _approved_text(row, item):
    """The reply exactly as the owner approved it, or None (not approved, file changed since, or unreadable)."""
    if not item or item["decision"] != "ok" or not row.get("reply_file") or item.get("file") != row["reply_file"]: return None
    try: text = _reply_path(row["reply_file"]).read_text(encoding="utf-8")
    except (OSError, ConnectorError): return None
    return text if hashlib.sha256(text.encode("utf-8")).hexdigest() == item.get("sha") else None

def payload(orders=None, book=None):
    """The 'complaints' list for the site. orders: the raw Shopify orders of this round (for the e-mail). NOT a CLI op:
    it can hold the reply text and an e-mail address, which only go to the site."""
    rows = _read()
    if not rows: return []
    book = book or _book()
    emails = {o.get("name"): o.get("email") for o in orders or [] if o.get("name") and o.get("email")}
    out = []
    for cid, row in rows.items():
        item = book.get(row["approval_id"]) if row.get("approval_id") else None
        c = {"complaint_id": cid}
        for k in ("order_name", "received_at", "products", "summary", "approval_id", "reply_subject"):
            if k in row: c[k] = row[k] or None
        status = _status(row, item)
        if status: c["status"] = status
        if status in VISIBLE:
            text = _approved_text(row, item)
            if text is not None: c["reply_body"] = text
            if row.get("order_name") in emails: c["reply_to_email"] = emails[row["order_name"]]
        elif status is not None:
            c["reply_body"] = None  # not approved (or no longer): the site must not show an older text
        out.append(c)
    return out

def show(id=""):
    """What would go, without the reply text and the e-mail: whether they would go is shown as yes / no."""
    out = []
    for c in payload():
        if id and c["complaint_id"] != id: continue
        out.append({**{k: v for k, v in c.items() if k not in ("reply_body", "reply_to_email")},
                    "reply_text_goes": bool(c.get("reply_body")), "email_from_order": c.get("status") in VISIBLE})
    return out

OPS = {"record": record, "show": show}
if __name__ == "__main__": sys.exit(run_cli(OPS))
