"""Billingo v3 connector. Reads invoices; create_draft makes a DRAFT only (never an issued invoice).
Auth: BILLINGO_API_KEY (header X-API-KEY). CLI: python3 connectors/billingo/ops.py list --days 14"""
import os, re, sys, json, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import requests
from cbase import ConnectorError, need, run_cli

BASE = "https://api.billingo.hu/v3"

def _call(method, path, params=None, body=None):
    (key,) = need("BILLINGO_API_KEY")
    try:
        r = requests.request(method, BASE + path, params=params, json=body, timeout=20, headers={"X-API-KEY": key})
    except requests.RequestException as e:
        raise ConnectorError(f"Billingo did not answer: {type(e).__name__}")
    if r.status_code in (401, 403): raise ConnectorError("Billingo: the API key was refused - check BILLINGO_API_KEY")
    if r.status_code == 429: raise ConnectorError("Billingo: rate limit (429), try again in a minute")
    if r.status_code == 422: raise ConnectorError(f"Billingo answered 422: {_validation(r)}")  # field names and rules only: show them all
    if not r.ok: raise ConnectorError(f"Billingo answered {r.status_code}: {r.text[:200]}")
    return r

def _validation(r):
    try: errors = r.json().get("errors") or []
    except ValueError: return r.text[:200]
    return "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errors if isinstance(e, dict))[:800] or r.text[:200]

def _doc(d):
    return {"id": d.get("id"), "number": d.get("invoice_number"), "type": d.get("type"), "date": d.get("invoice_date"),
            "paid": d.get("payment_status"), "total": d.get("gross_total"), "currency": d.get("currency"),
            "comment": d.get("comment") or "", "partner": (d.get("partner") or {}).get("name"),
            "email": d.get("notification_status")}  # what happened to the e-mailed invoice: none / opened / readed / downloaded / closed / failed

def list_invoices(days="14", per_page="100"):
    since = (dt.date.today() - dt.timedelta(days=int(days))).isoformat()
    data = _call("GET", "/documents", {"page": 1, "per_page": min(int(per_page), 100), "start_date": since}).json()
    return [_doc(d) for d in data.get("data", [])]

def status(id):
    return _doc(_call("GET", f"/documents/{id}").json())

def get_pdf(id, out=""):
    pdf = _call("GET", f"/documents/{id}/download").content
    path = pathlib.Path(out or f"handoffs/invoice-{id}.pdf"); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf); return {"saved": str(path), "bytes": len(pdf)}

def create_draft(payload_file):
    """payload_file: JSON in Billingo's DocumentInsert shape. type is forced to 'draft'; nothing is issued.
    Going through the approval queue (issue_invoice) is the caller's job."""
    body = json.loads(pathlib.Path(payload_file).read_text(encoding="utf-8"))
    body["type"] = "draft"
    d = _call("POST", "/documents", body=body).json()
    return {"id": d.get("id"), "type": d.get("type")}

def block_id():
    """BILLINGO_BLOCK_ID if set, else the first document block (the numbering series) of the account."""
    if os.getenv("BILLINGO_BLOCK_ID", "").strip(): return int(os.environ["BILLINGO_BLOCK_ID"])
    data = _call("GET", "/document-blocks").json().get("data", [])
    if not data: raise ConnectorError("Billingo: no document block (számlatömb) found - create one in Billingo first")
    return data[0]["id"]

def create_partner(body):
    """Customer record from order_flow only. Same name+address twice makes a duplicate in Billingo; accepted for now."""
    return _call("POST", "/partners", body=body).json()["id"]

def post_document(body):
    if body.get("type") != "draft": raise ConnectorError("only drafts are created here; issuing needs an approved issue_invoice")
    return _call("POST", "/documents", body=body).json()

def document(id):
    """Raw document, for code only (it carries the customer's data)."""
    return _call("GET", f"/documents/{id}").json()

def partner_email(partner_id):
    emails = _call("GET", f"/partners/{partner_id}").json().get("emails") or []
    return emails[0] if emails else ""

def create_from_draft(id):
    """Turns a draft into a real, numbered invoice. IRREVERSIBLE (a tax document: only a storno can undo it).
    Billingo has no "issue this draft" call: PUT /documents/{id} takes the whole document again (DocumentInsert) with type=invoice,
    so the body is rebuilt from the stored draft. If the rebuilt lines do not add up to the draft's total, nothing is issued."""
    d = document(id)
    if d.get("type") != "draft": raise ConnectorError(f"document {id} is not a draft (type {d.get('type')}): nothing to issue")
    items = []
    for i in d.get("items") or []:
        q = float(i.get("quantity") or 0)
        if q <= 0: raise ConnectorError(f"draft {id}: a line has no quantity, issue it in Billingo by hand")
        line = {"name": i.get("name"), "unit_price": round(float(i.get("gross_amount") or 0) / q, 2), "unit_price_type": "gross",
                "quantity": q, "unit": i.get("unit"), "vat": i.get("vat")}
        items.append({**line, **{k: i[k] for k in ("comment", "entitlement") if i.get(k)}})
    total = round(sum(l["unit_price"] * l["quantity"] for l in items), 2)
    if not items or abs(total - float(d.get("gross_total") or 0)) > 1:
        raise ConnectorError(f"draft {id}: the lines add up to {total}, the draft says {d.get('gross_total')}; not issued, check it in Billingo")
    today = dt.date.today().isoformat()
    body = {"partner_id": (d.get("partner") or {}).get("id"), "block_id": d.get("block_id"), "type": "invoice",
            "fulfillment_date": d.get("fulfillment_date"), "due_date": max(d.get("due_date") or today, today),  # a due date in the past is refused
            "payment_method": d.get("payment_method"), "language": d.get("language"), "currency": d.get("currency"),
            "conversion_rate": d.get("conversion_rate") or 1, "electronic": bool(d.get("electronic")), "items": items, "comment": d.get("comment") or ""}
    settings = {k: v for k, v in (d.get("settings") or {}).items() if v not in (None, "")}  # a draft comes back with empty settings (place_id: null), which the PUT refuses (422)
    if settings: body["settings"] = settings
    missing = [k for k in ("partner_id", "block_id", "fulfillment_date", "payment_method", "language", "currency") if not body.get(k)]
    if missing: raise ConnectorError(f"draft {id}: {', '.join(missing)} missing, not issued")
    return _call("PUT", f"/documents/{id}", body=body).json()

def mark_paid(id, price, payment_method, date=""):
    """Records the payment on an issued invoice, so it shows as paid (not as outstanding). Replaces the payment history."""
    day = date or dt.date.today().isoformat()
    _call("PUT", f"/documents/{id}/payments", body=[{"date": day, "price": price, "payment_method": payment_method}])
    return True

def send_by_email(id, emails):
    _call("POST", f"/documents/{id}/send", body={"emails": list(emails)})
    return True

def _last4(number):
    digits = "".join(c for c in str(number or "") if c.isalnum())
    return digits[-4:] or None

def payments(days="400", pages="10"):
    """The site's Payments tab: the owner's bank accounts (name, currency, last 4 characters only) and every issued invoice
    of the window with its due and paid date. Since the Revolut bank sync (2026-10-18) Billingo marks invoices paid on its own;
    the API gives no balance and no bank transactions (/bank-transactions is 404), so neither is here. No partner data."""
    accounts = [{"id": str(a.get("id")), "name": a.get("name") or None, "currency": a.get("currency"),
                 "last4": _last4(a.get("account_number_iban") or a.get("account_number"))}
                for a in _call("GET", "/bank-accounts", {"page": 1, "per_page": 100}).json().get("data", [])]
    since, today = (dt.date.today() - dt.timedelta(days=int(days))).isoformat(), dt.date.today().isoformat()
    docs, page = [], 1
    while True:
        j = _call("GET", "/documents", {"page": page, "per_page": 100, "start_date": since}).json()
        docs += j.get("data", [])
        complete = page >= int(j.get("last_page") or 1)
        if complete or page >= int(pages): break
        page += 1
    invoices = []
    for d in docs:
        if d.get("type") != "invoice" or d.get("cancelled"): continue
        order = re.search(r"#\d+", d.get("comment") or "")
        paid = d.get("payment_status") == "paid"
        invoices.append({"id": str(d.get("id")), "number": d.get("invoice_number"), "order": order.group(0) if order else None,
                         "date": d.get("invoice_date"), "due_date": d.get("due_date"), "paid_date": d.get("paid_date") or None,
                         "total": d.get("gross_total"), "currency": d.get("currency"), "payment_status": d.get("payment_status"),
                         "payment_method": d.get("payment_method"),
                         "overdue": bool(not paid and d.get("due_date") and d["due_date"] < today)})
    return {"bank_accounts": accounts, "invoices": invoices, "since": since, "complete": complete}

OPS = {"list": list_invoices, "status": status, "get_pdf": get_pdf, "create_draft": create_draft, "payments": payments}
if __name__ == "__main__": sys.exit(run_cli(OPS))
