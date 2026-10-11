"""Billingo v3 connector. Reads invoices; create_draft makes a DRAFT only (never an issued invoice).
Auth: BILLINGO_API_KEY (header X-API-KEY). CLI: python3 connectors/billingo/ops.py list --days 14"""
import os, sys, json, pathlib, datetime as dt
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
    if not r.ok: raise ConnectorError(f"Billingo answered {r.status_code}: {r.text[:200]}")
    return r

def _doc(d):
    return {"id": d.get("id"), "number": d.get("invoice_number"), "type": d.get("type"), "date": d.get("invoice_date"),
            "paid": d.get("payment_status"), "total": d.get("gross_total"), "currency": d.get("currency"),
            "comment": d.get("comment") or "", "partner": (d.get("partner") or {}).get("name")}

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

OPS = {"list": list_invoices, "status": status, "get_pdf": get_pdf, "create_draft": create_draft}
if __name__ == "__main__": sys.exit(run_cli(OPS))
