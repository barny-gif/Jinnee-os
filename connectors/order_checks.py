"""Critical-data checks before each step of an order (owner's rule, 2026-10-15): what is missing for the invoice draft and for the
MyGLS label. Works on a raw Shopify order. The result names the step and the field only, never the customer's data, so it can go
to the site, the log and Telegram. A step with a 'stop' problem is not done automatically; a 'warn' is shown but does not stop it.
CLI: python3 connectors/order_checks.py open        (the open orders of the last 7 days: order number + missing fields)"""
import sys, re, pathlib, importlib.util
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cbase import run_cli

def _load_flow():
    spec = importlib.util.spec_from_file_location("checks_order_flow", HERE / "order_flow.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

flow = _load_flow()
PICKUP_POINT = ("csomagpont", "automata", "parcel shop", "parcelshop", "parcel locker", "átvevőpont", "atvevopont", "box")
# the field names the site shows (Hungarian, short)
NAMES = {"name": "név", "zip": "irányítószám", "city": "város", "street": "utca", "house_number": "házszám", "phone": "telefonszám",
         "country": "ország", "email": "e-mail", "billing_address": "számlázási cím", "payment": "fizetési mód", "total": "végösszeg",
         "address": "szállítási cím", "kind": "rendelés típusa", "shipping_method": "szállítási mód"}

def _p(step, field, level, why):
    return {"step": step, "field": field, "label": NAMES.get(field, field), "level": level, "why": why}

def _valid_phone(p, country):
    d = flow.phone(p, country)
    return bool(re.fullmatch(r"\+36\d{8,9}", d)) if country == "HU" else len(re.sub(r"\D", "", d)) >= 8

def check(o):
    """→ list of problems. Cancelled orders are not checked: nothing is done for them."""
    if o.get("cancelled_at"): return []
    out = []
    total = float(o.get("total_price") or 0)
    gateways = " ".join(g for g in o.get("payment_gateway_names") or [] if g)
    sh = o.get("shipping_address") or {}
    b = o.get("billing_address") or {}
    # Which kind of order: a 0 Ft or test order may be barter / influencer / gift, which gets no invoice and no label (lessons 2026-10-12)
    if total <= 0 or o.get("test"):
        out.append(_p("order", "kind", "stop", "0 Ft-os vagy teszt rendelés: barter, influencer, ajándék? Számla és címke előtt kérdezz"))
    # MyGLS label
    if not sh:
        out.append(_p("label", "address", "stop", "nincs szállítási cím"))
    else:
        street, house = flow.split_street(" ".join(x for x in (sh.get("address1"), sh.get("address2")) if x))
        for field, ok in (("name", sh.get("name")), ("zip", re.fullmatch(r"\d{4}", (sh.get("zip") or "").strip())),
                          ("city", sh.get("city")), ("street", street), ("house_number", house)):
            if not ok: out.append(_p("label", field, "stop", "hiányzik vagy hibás"))
        country = (sh.get("country_code") or "HU").upper()
        if country != "HU": out.append(_p("label", "country", "stop", "csak Magyarországra szállítunk"))
        ph = sh.get("phone") or o.get("phone") or b.get("phone")
        if not ph: out.append(_p("label", "phone", "stop", "nincs telefonszám: a futár nem tud hívni"))
        elif not _valid_phone(ph, country): out.append(_p("label", "phone", "stop", "a telefonszám formátuma hibás"))
    titles = " ".join(l.get("title") or "" for l in o.get("shipping_lines") or []).lower()
    if any(w in titles for w in PICKUP_POINT):
        out.append(_p("label", "shipping_method", "stop", "csomagpontos szállítás: a házhoz címke nem jó, kézzel kell"))
    if not o.get("email"): out.append(_p("label", "email", "warn", "nincs e-mail: a GLS nem tud értesítést küldeni"))
    # Invoice draft
    if not gateways: out.append(_p("invoice", "payment", "stop", "ismeretlen fizetési mód"))
    bill_name = (b.get("company") or b.get("name") or "").strip()
    if not (bill_name and b.get("zip") and b.get("city") and b.get("address1")):
        out.append(_p("invoice", "billing_address", "stop", "a számlázási név vagy cím hiányos"))
    if not o.get("email"): out.append(_p("invoice", "email", "warn", "nincs e-mail: a számla nem jut el a vevőhöz"))
    return out

def ready(problems, step):
    """May this step run automatically? Only with no 'stop' problem for it (or for the order as a whole)."""
    return not any(p["level"] == "stop" and p["step"] in (step, "order") for p in problems)

def check_open(days="7", shop=None):
    """Read only. Order number, payment and what is missing: no customer data."""
    if shop is None:
        spec = importlib.util.spec_from_file_location("checks_shopify", HERE / "shopify" / "ops.py")
        shop = importlib.util.module_from_spec(spec); spec.loader.exec_module(shop)
    import datetime as dt
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).isoformat(timespec="seconds")
    orders = shop._get("orders.json", {"status": "open", "limit": 250, "created_at_min": since,
                                       "fields": "name,total_price,financial_status,payment_gateway_names,cancelled_at,test,email,phone,"
                                                 "shipping_address,billing_address,shipping_lines"}).get("orders", [])
    out = []
    for o in orders:
        cod = any(c in " ".join(o.get("payment_gateway_names") or []).lower() for c in flow.COD)
        out.append({"order": o.get("name"), "payment": f"COD {float(o.get('total_price') or 0):.0f} Ft" if cod else (o.get("financial_status") or "").upper(),
                    "missing": [f"{p['step']}: {p['label']} ({p['level']})" for p in check(o)] or "minden megvan"})
    return out

OPS = {"open": check_open}
if __name__ == "__main__": sys.exit(run_cli(OPS))
