"""Order -> invoice DRAFT in Billingo. Customer contact data passes through this code only: it is never printed or logged
(the CLI prints order number, draft id and warnings). Issuing and sending are separate steps behind the approval queue.
CLI: python3 connectors/order_flow.py invoice_draft --order_id 123456789"""
import sys, os, re, json, time, pathlib, datetime as dt, importlib.util
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cbase import ConnectorError, run_cli

def _load(name):
    spec = importlib.util.spec_from_file_location(f"flow_{name}", HERE / name / "ops.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

VAT = os.getenv("BILLINGO_VAT", "27%")
ELECTRONIC = os.getenv("BILLINGO_ELECTRONIC", "0").strip().lower() in ("1", "true", "yes")  # e-számla is a separate Billingo feature
COD = ("cash on delivery", "cod", "utánvét", "utanvet")

def payment_method(gateways):
    g = " ".join(gateways).lower()
    return "cash_on_delivery" if any(c in g for c in COD) else "wire_transfer" if any(w in g for w in ("bank deposit", "átutal", "atutal", "wire", "bank transfer")) else "bankcard"

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

def step(label, fn, *a):
    """Say which step failed: 'create partner', 'create draft'..."""
    try: return fn(*a)
    except ConnectorError as e: raise ConnectorError(f"[{label}] {e}")

def invoice_draft(order_id, shop=None, bill=None):
    shop = shop or _load("shopify"); bill = bill or _load("billingo")
    o = shop.order_private(order_id)
    existing = [d for d in step("list invoices", bill.list_invoices, "60") if o["name"] in (d["comment"] or "")]
    if existing:  # one order, one invoice: never a second one
        return {"order": o["name"], "skipped": "an invoice or draft already mentions this order", "invoice": existing[0]["number"] or existing[0]["id"]}
    partner = step("create partner", bill.create_partner, partner_body(o))
    today = dt.date.today()
    body = {"partner_id": partner, "block_id": step("find numbering block", bill.block_id), "type": "draft", "fulfillment_date": today.isoformat(),
            "due_date": (today + dt.timedelta(days=8)).isoformat(), "payment_method": payment_method(o["gateways"]),
            "language": "hu", "currency": o["currency"] or "HUF", "electronic": ELECTRONIC, "items": items(o),
            "comment": f"Shopify {o['name']}"}
    d = step("create draft", bill.post_document, body)
    return {"order": o["name"], "draft_id": d.get("id"), "type": d.get("type")}

ISSUED = HERE.parent / "brain" / "invoices_issued.json"  # draft id -> {"number", "sent"}
UPLOADED = HERE.parent / "brain" / "gls_uploaded.json"  # order name -> parcel id; brain/ is kept across rebuilds

def _uploaded():
    try: return json.loads(UPLOADED.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

def _issued():
    try: return json.loads(ISSUED.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

def invoice_issue(draft_id, confirm="no", send="no", paid="no", bill=None):
    """Draft -> real invoice -> (optionally) e-mailed to the customer. Dry run unless confirm=yes.
    paid=yes records the payment on the invoice (use it only when the Shopify order is PAID, e.g. card or already transferred).
    Only run it for an item that has a GO from `core/approvals.py consume` (action issue_invoice / send_customer_email)."""
    bill = bill or _load("billingo")
    done = _issued().get(str(draft_id))
    if done and (send != "yes" or done.get("sent")) and (paid != "yes" or done.get("paid")):
        return {"skipped": "already issued" + (" and sent" if done.get("sent") else ""), "invoice": done.get("number")}
    doc = bill.document(draft_id)
    if not done and doc.get("type") != "draft":
        raise ConnectorError(f"document {draft_id} is not a draft (type {doc.get('type')}): nothing to issue")
    mail = bill.partner_email((doc.get("partner") or {}).get("id")) if send == "yes" else ""
    if send == "yes" and not mail: raise ConnectorError("the customer has no e-mail address in Billingo: issue without sending, or add one")
    if confirm != "yes":
        return {"dry_run": True, "comment": doc.get("comment"), "total": doc.get("gross_total"), "would_send": send == "yes", "would_mark_paid": paid == "yes",
                "next": "--confirm yes issues the invoice (cannot be undone); add --send yes to e-mail it"}
    if done: inv = {"id": done.get("id"), "invoice_number": done.get("number")}
    else:
        inv = step("issue invoice", bill.create_from_draft, draft_id)
        rec = {"id": inv.get("id"), "number": inv.get("invoice_number"), "sent": False, "paid": False}
        ISSUED.parent.mkdir(parents=True, exist_ok=True)
        ISSUED.write_text(json.dumps({**_issued(), str(draft_id): rec}), encoding="utf-8")  # recorded BEFORE sending
    out = {"invoice": inv.get("invoice_number")}
    def remember(**more):
        rec = {**_issued().get(str(draft_id), {"id": inv.get("id"), "number": inv.get("invoice_number")}), **more}
        ISSUED.write_text(json.dumps({**_issued(), str(draft_id): rec}), encoding="utf-8")
    if paid == "yes" and not (done or {}).get("paid"):
        step("mark paid", bill.mark_paid, inv.get("id"), doc.get("gross_total"), doc.get("payment_method"))
        remember(paid=True); out["paid"] = True
    if send == "yes":
        step("send e-mail", bill.send_by_email, inv.get("id"), [mail])
        remember(sent=True); out["sent"] = True
    return out

def split_street(address):
    """'Fő u. 12/B' -> ('Fő u.', '12/B'). GLS wants street and house number apart. No number found -> ('whole text', '')."""
    m = re.match(r"^(.*?)[\s,]+(\d[\w/.\-]*(?:\s*(?:em|fsz|ép|lh)\.?\s*\S*)?)$", address.strip(), re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else (address.strip(), "")

def phone(p, country="HU"):
    d = re.sub(r"[^\d+]", "", p or "")
    if d.startswith("00"): d = "+" + d[2:]
    if d.startswith("06"): d = "+36" + d[2:]
    if d and not d.startswith("+") and country == "HU": d = "+36" + d.lstrip("0")
    return d

def next_workday(today=None):
    d = (today or dt.date.today()) + dt.timedelta(days=1)
    while d.weekday() >= 5: d += dt.timedelta(days=1)
    return d

CONTENT_MAX = 200  # MyGLS Content: the note printed on the label; kept short so it is not cut on the label (exact GLS limit not confirmed)

def gls_content(o, cod_amount=0):
    """'montivilag.hu #1027, 1× Farmer Kalandkönyv, COD 10380 Ft' or '..., PAID' (owner, 2026-10-21; products since 2026-10-19). No personal data."""
    goods = ", ".join(f"{max(int(l['qty']), 1)}× {l['title']}" for l in o["lines"] if l.get("title"))
    pay = f"COD {int(cod_amount)} Ft" if cod_amount else "PAID"
    text = ", ".join(x for x in (f"montivilag.hu {o['name']}", goods, pay) if x)
    return text if len(text) <= CONTENT_MAX else text[:CONTENT_MAX - 1] + "…"

def gls_parcel(o, gls, house="", info=""):
    """The MyGLS Parcel for an order. Pickup data (your address) comes from GLS_PICKUP_* in .env."""
    sh = o["shipping"] or o["billing"]
    street, found = split_street(" ".join(x for x in (sh.get("address1"), sh.get("address2")) if x))
    house = house or found  # a house number given by hand wins
    missing = [k for k, v in (("name", sh.get("name")), ("zip", sh.get("zip")), ("city", sh.get("city")), ("street", street)) if not v]
    if missing: raise ConnectorError(f"{o['name']}: shipping address incomplete ({', '.join(missing)})")
    pick = {k: os.getenv("GLS_PICKUP_" + k.upper(), "").strip() for k in ("name", "street", "house", "city", "zip", "phone", "email")}
    gone = [k for k in ("name", "street", "city", "zip") if not pick[k]]
    if gone: raise ConnectorError("Missing in .env: " + ", ".join("GLS_PICKUP_" + k.upper() for k in gone))
    cod = payment_method(o["gateways"]) == "cash_on_delivery"
    total = round(sum(float(i["unit_price"]) * i["quantity"] for i in items(o)), 0)
    if cod and o.get("total") is not None and abs(total - float(o["total"])) >= 1:  # the courier must collect exactly what the customer owes
        raise ConnectorError(f"{o['name']}: COD amount {int(total)} Ft does not match the order total {int(float(o['total']))} Ft")
    ms = int(time.mktime(next_workday().timetuple()) * 1000)
    return {"ClientNumber": gls.client_number(), "ClientReference": o["name"], "CODAmount": total if cod else 0,
            "CODReference": o["name"] if cod else "", "Content": gls_content(o, total if cod else 0), "Count": 1,
            "PickupDate": f"/Date({ms})/",
            "PickupAddress": {"Name": pick["name"], "Street": pick["street"], "HouseNumber": pick["house"], "City": pick["city"],
                              "ZipCode": pick["zip"], "CountryIsoCode": "HU", "ContactName": pick["name"], "ContactPhone": phone(pick["phone"]),
                              "ContactEmail": pick["email"]},
            "DeliveryAddress": {"Name": sh["name"], "Street": street, "HouseNumber": house, "HouseNumberInfo": info, "City": sh["city"], "ZipCode": sh["zip"],
                                "CountryIsoCode": sh.get("country_code") or "HU", "ContactName": sh["name"],
                                "ContactPhone": phone(o["phone"] or sh.get("phone"), sh.get("country_code") or "HU"), "ContactEmail": o["email"]}}

def parcel_prepare(order_id, send="no", house="", info="", replace="no", shop=None, gls=None):
    """Dry run by default: checks the data and says what WOULD be uploaded. send=yes uploads it to MyGLS (prepared, not printed)."""
    shop = shop or _load("shopify"); gls = gls or _load("gls")
    o = shop.order_private(order_id)
    done = _uploaded().get(o["name"])
    if done and replace != "yes":  # one order, one parcel: never a second upload
        return {"order": o["name"], "skipped": "already uploaded to MyGLS (replace=yes deletes the prepared parcel and uploads it again)", "parcel_id": done}
    parcel = gls_parcel(o, gls, house, info)
    d = parcel["DeliveryAddress"]
    warn = [w for w, bad in (("house number not found in the address: GLS may refuse or deliver badly", not d["HouseNumber"]),
                             ("no phone number: the courier cannot call", not d["ContactPhone"])) if bad]
    if send != "yes":
        return {"order": o["name"], "dry_run": True, "replaces": done or None, "cod_amount": parcel["CODAmount"], "content": parcel["Content"], "pickup": parcel["PickupDate"], "warnings": warn,
                "next": "run again with --send yes to upload to MyGLS"}
    old = done.get("parcel_id") if isinstance(done, dict) else done
    if old and old is not True:  # correction: the old prepared parcel goes first; if that fails nothing is uploaded
        step("delete the old parcel", gls.delete_labels, [old])
    out = step("upload to MyGLS", gls.prepare_labels, [parcel])
    try:
        UPLOADED.parent.mkdir(parents=True, exist_ok=True)
        # the COD amount is recorded too, so the site shows "SENT · NO COD" / "SENT · COD …" instead of "check payment mode"
        rec = {"parcel_id": out[0]["parcel_id"] if out else None, "cod_amount": parcel["CODAmount"],
               "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        UPLOADED.write_text(json.dumps({**_uploaded(), o["name"]: rec}), encoding="utf-8")
    except OSError: warn.append("could not record the upload: do not run this order again")
    return {"order": o["name"], "uploaded": out, "warnings": warn}

OPS = {"invoice_draft": invoice_draft, "invoice_issue": invoice_issue, "parcel_prepare": parcel_prepare}
if __name__ == "__main__": sys.exit(run_cli(OPS))
