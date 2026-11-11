"""Order -> invoice DRAFT in Billingo. Customer contact data passes through this code only: it is never printed or logged
(the CLI prints order number, draft id and warnings). Issuing and sending are separate steps behind the approval queue.
CLI: python3 connectors/order_flow.py invoice_draft --order_id 123456789"""
import sys, os, re, json, time, pathlib, datetime as dt, importlib.util
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cbase import ConnectorError, run_cli, setting
import business

def _load(name):
    spec = importlib.util.spec_from_file_location(f"flow_{name}", HERE / name / "ops.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

VAT = os.getenv("BILLINGO_VAT", "27%")
ELECTRONIC = os.getenv("BILLINGO_ELECTRONIC", "0").strip().lower() in ("1", "true", "yes")  # e-számla is a separate Billingo feature
COD = ("cash on delivery", "cod", "utánvét", "utanvet")

MANUAL = ("", "manual")  # no gateway: an order the owner made by hand from a draft

def payment_method(gateways, financial_status=None):
    g = " ".join(gateways).lower()
    if any(c in g for c in COD): return "cash_on_delivery"
    if any(w in g for w in ("bank deposit", "átutal", "atutal", "wire", "bank transfer")): return "wire_transfer"
    # Unpaid with no real gateway: the courier collects it. Reading it as a card payment printed 'PAID' and COD 0 on the label.
    if financial_status == "pending" and all(x.strip().lower() in MANUAL for x in gateways or [""]): return "cash_on_delivery"
    return "bankcard"

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
            "due_date": (today + dt.timedelta(days=8)).isoformat(), "payment_method": payment_method(o["gateways"], o.get("financial_status")),
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

L = r"[^\W\d_]"  # one letter (any alphabet), for boundaries
# floor / door / building words that may follow the house number ('2. em. 5.', 'fsz. 1', 'B ép. 3. lh')
FLOOR_WORD = re.compile(rf"(?<!{L})(?:fszt|fsz|földszint|félemelet|emelet|em|ajtó|ajto|lph|lh|lépcsőház|épület|ép|tetőtér|tt)\.?(?!{L})", re.I)
FLOOR_REST = re.compile(r"(?:\d+|[IVX]+)(?:/\d+)?\.?|[\s,./:\-]+")
# a house number token: 12, 12., 12/B, 12B, 12 B, 3/A, 12-14, 12/3 (a following letter run like '48as' is part of a name, not a number)
HOUSE_TOKEN = re.compile(rf"(?<![\w/\-])(\d+(?:\s*[-/]\s*\d+)?(?:\s*/?\s*[A-Za-z](?!{L}))?)(\.?)(?=$|[\s,])")
# a number followed straight by one of these was a floor or a door, not the house number ('Fő utca 3. em. 4.')
FLOOR_AFTER = re.compile(rf"\s*(?:félemelet|emelet|em|ajtó|ajto)\.?(?!{L})", re.I)
# street-type words: one alone is no street name ('utca 12'). Same list as the JinneeHQ address check (address-check.ts)
STREET_TYPES = {"utcája", "utca", "u", "útja", "út", "sugárút", "körút", "krt", "tere", "tér", "köz", "köze", "sor", "sora", "sétány",
                "stny", "dűlő", "dülő", "lakótelep", "ltp", "park", "parkja", "rakpart", "rkp", "fasor", "liget", "telep", "udvar", "lejtő",
                "ösvény", "határút", "árok", "kert", "sétaút", "lépcső", "tanya", "major", "puszta", "zug", "kapu", "pincesor", "üdülőtelep"}
ONLY_FLOOR = "csak emelet vagy ajtó szerepel, maga a házszám hiányzik"
AMBIGUOUS = "több szám van egymás után, nem egyértelmű, melyik a házszám"
NUMBER_WORD = re.compile(rf"(?<!{L})(?:hsz|házszám|hászám|szám|sz)\.?:?(?!{L})", re.I)

def _floor_info(text):
    """Is this only floor / door / building detail ('2. em. 5.', 'fsz 1', 'III. em')? Needs at least one such word: bare numbers are not."""
    t = text.strip(" ,")
    return bool(FLOOR_WORD.search(t)) and not FLOOR_REST.sub("", FLOOR_WORD.sub(" ", t)).strip()

def parse_street(address):
    """One address line -> {'street', 'house', 'info', 'full', 'why'}. house is the number alone ('12', '12/B', '12-14'), info the
    floor/door detail after it, full the house number as written with that detail ('12. 2. em. 5.'). No house number -> house ''
    and why says what is wrong: 'nincs szám', 'a szám az utcanév része', 'több szám, nem egyértelmű'.
    The house number is the first number after the street's words that is followed by nothing or by floor/door detail only, so a
    street whose name holds a number ('1848. március 15. tér 4', 'Május 1. utca 3', 'IV. Béla út 2') keeps it."""
    text = re.sub(r"\s+", " ", (address or "").strip()).strip(" ,")
    text = re.sub(r"(?:[\s,]+[^\w\s]+)+$", "", text)  # a placeholder joined at the end ('Fő u. 3 -', from address2 '-') is not part of it
    out = {"street": text, "house": "", "info": "", "full": "", "why": ""}
    if not text: return {**out, "why": "üres cím"}
    # a number glued to the street ('u.12.', 'utca12', 'krt.5') gets its space; 'hsz.', 'szám' next to the number is dropped
    text = re.sub(rf"(?:(?<=\.)|(?<={L}))(?=\d)", " ", text)
    text = re.sub(rf"(?:(?<=\s)|^){NUMBER_WORD.pattern}\s*(?=\d)", "", text, flags=re.I)
    text = re.sub(rf"(?<=\d)(\.?)\s*{NUMBER_WORD.pattern}(?=$|[\s,])", r"\1", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    out["street"] = text
    if not re.search(L, text): return {**out, "why": "csak szám van a címben, utcanév nincs"}
    if not re.search(r"\d", text): return {**out, "why": "nincs szám a címben"}
    ambiguous = False
    for m in HOUSE_TOKEN.finditer(text):
        street, tail = text[:m.start()].rstrip(" ,"), text[m.end():]
        if not re.search(L, street): continue  # nothing but numbers before it: a zip code or a bare number, not the street's number
        # '3. em. 4.' straight after the street, or 'fsz. 1': the number is a floor / door, the house number is not there.
        # Never sent as the house number (a '3' on the label would be a wrong address).
        if FLOOR_AFTER.match(tail) or FLOOR_WORD.fullmatch(street.rsplit(" ", 1)[-1]):
            return {**out, "why": ONLY_FLOOR}
        if tail.strip(" ,") and not _floor_info(tail):
            # only numbers follow ('12 14', '12 II/5' with no floor word): not clear which is the house number
            if not FLOOR_REST.sub("", tail).strip(): ambiguous = True
            continue  # else more of the street's name follows: the number belongs to the name
        if re.search(r"(?:^|[\s,])\d[\w/\-]*\.?$", street): ambiguous = True; break  # '12 14', '12. 3/5': which one is it?
        if street.lower().rstrip(".") in STREET_TYPES:  # 'utca 12': a street type with no name in front is no street
            return {**out, "street": "", "why": "nincs utcanév, csak az utca típusa"}
        house = re.sub(r"\s+", "", m.group(1)) if re.fullmatch(r"\d+\s*[-/]\s*\d+", m.group(1)) else re.sub(r"\s+", " ", m.group(1))
        info = tail.strip(" ,")
        full = (m.group(1) + m.group(2) + tail).strip(" ,") if info else m.group(1)
        return {"street": street, "house": house, "info": info, "full": full, "why": ""}
    if ambiguous: return {**out, "why": AMBIGUOUS}
    return {**out, "why": "a címben lévő szám az utcanév része, házszám nincs utána"}

def split_street(address):
    """'Fő u. 12/B' -> ('Fő u.', '12/B'). GLS wants street and house number apart. No number found -> ('whole text', '').
    The house part keeps floor/door detail as written ('Fő tér 3. 2. em.' -> ('Fő tér', '3. 2. em.')); a lone trailing dot goes."""
    p = parse_street(address)
    return (p["street"], p["full"])

def parse_address(addr):
    """A shipping address (address1 + address2) -> parse_street's dict. address2 is the 'flat, floor' field: when address1 already
    has the house number, address2 is the floor/door detail ('3/5', '2. em. 5.'); when it has none, the two are read as one line
    (the house number written into address2: '12', '12. 2. em')."""
    a1 = (addr.get("address1") or "").strip()
    a2 = (addr.get("address2") or "").strip() if re.search(r"\w", addr.get("address2") or "") else ""
    first = parse_street(a1)
    if first["house"] and a2:
        if re.search(rf"{L}{{3,}}", FLOOR_WORD.sub(" ", a2)):  # address2 has words of its own (a street?): read the two together
            return parse_street(f"{a1} {a2}")
        info = ", ".join(x for x in (first["info"], a2) if x)
        return {**first, "info": info, "full": f"{first['full']} {a2}"}
    return first if not a2 else parse_street(f"{a1} {a2}")

def street_line(addr):
    """address1 + address2 as one line. An address2 with no letter or digit ('-', '.', '/') is a placeholder and is left out:
    joined, it hid the house number at the end."""
    return " ".join(x for x in (addr.get("address1"), addr.get("address2")) if x and re.search(r"\w", x))

def phone(p, country="HU"):
    d = re.sub(r"[^\d+]", "", p or "")
    if d.startswith("00"): d = "+" + d[2:]
    if d.startswith("06"): d = "+36" + d[2:]
    if d and not d.startswith("+") and country == "HU": d = "+36" + d.lstrip("0")
    # a checkout form that puts +36 in front of a number typed with its domestic 06 prefix: '+36 06 30…' is '+36 30…'
    if country == "HU" and re.fullmatch(r"\+3606\d{8,9}", d): d = "+36" + d[5:]
    return d

def valid_phone(p, country="HU"):
    d = phone(p, country)
    return bool(re.fullmatch(r"\+36\d{8,9}", d)) if country == "HU" else len(re.sub(r"\D", "", d)) >= 8

def best_phone(candidates, country="HU"):
    """The first number that is valid once normalised; else the first given one (normalised); else ''. An order can carry the
    phone in several places (order, shipping address, billing address) and one of them may be malformed."""
    given = [c for c in candidates if c and str(c).strip()]
    return next((phone(c, country) for c in given if valid_phone(c, country)), phone(given[0], country) if given else "")

def next_workday(today=None):
    d = (today or dt.date.today()) + dt.timedelta(days=1)
    while d.weekday() >= 5: d += dt.timedelta(days=1)
    return d

CONTENT_MAX = business.LABEL_MAX  # MyGLS Content: the note printed on the label; kept short so it is not cut (exact GLS limit not confirmed)

def gls_content(o, cod_amount=0):
    """The label note from config/business.yaml (shop.label_name + labels.*), e.g. 'example-shop.hu #1001, 1× Wooden puzzle,
    COD 7350 Ft' or '..., PAID'. No personal data. A missing setting stops the label (ConnectorError)."""
    goods = ", ".join(f"{max(int(l['qty']), 1)}× {l['title']}" for l in o["lines"] if l.get("title"))
    return setting(business.label_note, o["name"], goods, cod_amount)

def gls_parcel(o, gls, house="", info=""):
    """The MyGLS Parcel for an order. Pickup data (your address) comes from GLS_PICKUP_* in .env."""
    sh = o["shipping"] or o["billing"]
    a = parse_address(sh)
    street = a["street"]
    if not house: house, info = a["house"], info or a["info"]  # a house number given by hand wins (with its own info)
    missing = [k for k, v in (("name", sh.get("name")), ("zip", sh.get("zip")), ("city", sh.get("city")), ("street", street)) if not v]
    if missing: raise ConnectorError(f"{o['name']}: shipping address incomplete ({', '.join(missing)})")
    pick = {k: os.getenv("GLS_PICKUP_" + k.upper(), "").strip() for k in ("name", "street", "house", "city", "zip", "phone", "email")}
    gone = [k for k in ("name", "street", "city", "zip") if not pick[k]]
    if gone: raise ConnectorError("Missing in .env: " + ", ".join("GLS_PICKUP_" + k.upper() for k in gone))
    cod = payment_method(o["gateways"], o.get("financial_status")) == "cash_on_delivery"
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
                                "ContactPhone": best_phone([o["phone"], sh.get("phone")], sh.get("country_code") or "HU"), "ContactEmail": o["email"]}}

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
