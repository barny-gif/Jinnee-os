"""Order -> invoice DRAFT in Billingo. Customer contact data passes through this code only: it is never printed or logged
(the CLI prints order number, draft id and warnings). Issuing and sending are separate steps behind the approval queue.
CLI: python3 connectors/order_flow.py invoice_draft --order_id 123456789"""
import sys, os, pathlib, datetime as dt, importlib.util
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cbase import ConnectorError, run_cli

def _load(name):
    spec = importlib.util.spec_from_file_location(f"flow_{name}", HERE / name / "ops.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

VAT = os.getenv("BILLINGO_VAT", "27%")
COD = ("cash on delivery", "cod", "utánvét", "utanvet")

def payment_method(gateways):
    g = " ".join(gateways).lower()
    return "cash_on_delivery" if any(c in g for c in COD) else "bankcard"

def items(o):
    """Shopify prices are gross (VAT included) in a Hungarian shop. A line discount is spread into the unit price."""
    out = []
    for l in o["lines"]:
        q = max(int(l["qty"]), 1)
        unit = round((float(l["price"]) * q - float(l["discount"])) / q, 2)
        out.append({"name": l["title"], "unit_price": unit, "unit_price_type": "gross", "quantity": q, "unit": "db", "vat": VAT})
    for s in o["shipping_lines"]:
        if float(s["price"] or 0) > 0:
            out.append({"name": s["title"] or "Szállítás", "unit_price": float(s["price"]), "unit_price_type": "gross", "quantity": 1, "unit": "db", "vat": VAT})
    return out

def partner_body(o):
    b = o["billing"]
    name = (b.get("company") or b.get("name") or "").strip()
    if not name or not b.get("zip") or not b.get("city") or not b.get("address1"):
        raise ConnectorError(f"{o['name']}: the billing name/address is incomplete (or Shopify withheld it: the custom app may need protected customer data access)")
    return {"name": name, "emails": [o["email"]] if o["email"] else [], "tax_type": "NO_TAX_NUMBER",
            "address": {"country_code": b.get("country_code") or "HU", "post_code": b["zip"], "city": b["city"],
                        "address": " ".join(x for x in (b.get("address1"), b.get("address2")) if x)}}

def invoice_draft(order_id, shop=None, bill=None):
    shop = shop or _load("shopify"); bill = bill or _load("billingo")
    o = shop.order_private(order_id)
    existing = [d for d in bill.list_invoices(days="60") if o["name"] in (d["comment"] or "")]
    if existing:  # one order, one invoice: never a second one
        return {"order": o["name"], "skipped": "an invoice or draft already mentions this order", "invoice": existing[0]["number"] or existing[0]["id"]}
    partner = bill.create_partner(partner_body(o))
    today = dt.date.today()
    body = {"partner_id": partner, "block_id": bill.block_id(), "type": "draft", "fulfillment_date": today.isoformat(),
            "due_date": (today + dt.timedelta(days=8)).isoformat(), "payment_method": payment_method(o["gateways"]),
            "language": "hu", "currency": o["currency"] or "HUF", "electronic": True, "items": items(o),
            "comment": f"Shopify {o['name']}"}
    d = bill.post_document(body)
    return {"order": o["name"], "draft_id": d.get("id"), "type": d.get("type")}

OPS = {"invoice_draft": invoice_draft}
if __name__ == "__main__": sys.exit(run_cli(OPS))
