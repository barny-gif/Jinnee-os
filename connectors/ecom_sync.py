"""Shop -> the admin site (jinnee-os.com). Sends the last days' orders with their payment, invoice and parcel state, and the
product list. It only reads Shopify, Billingo and brain/gls_uploaded.json. The site never calls this machine.
Customer contact data (name, e-mail, phone, addresses) is sent too, in a separate "private" block per order: the owner decided
on 2026-10-12 that it may live in the site's protected table. It goes nowhere else: this file never prints or logs it, and the
CLI only ever shows counts. The order note is not fetched.
Dry run by default.
CLI: python3 connectors/ecom_sync.py push              (says what WOULD be sent: counts only)
     python3 connectors/ecom_sync.py push --send yes
     python3 connectors/ecom_sync.py loop              (push every ECOM_SYNC_SECONDS, default 60)"""
import sys, os, re, json, time, pathlib, datetime as dt, importlib.util
import requests
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cbase import ConnectorError, need, run_cli

def _load(name):
    spec = importlib.util.spec_from_file_location(f"sync_{name}", HERE / name / "ops.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

ORDER_FIELDS = ("id,name,created_at,total_price,currency,financial_status,fulfillment_status,cancelled_at,line_items,fulfillments,"
                "payment_gateway_names,shipping_lines,test,email,phone,shipping_address,billing_address")
PRODUCT_FIELDS = "id,title,status,variants"
ADDRESS_KEYS = ("name", "company", "address1", "address2", "zip", "city", "province", "country_code", "phone")
COD = ("cash on delivery", "cod", "utánvét", "utanvet")  # the same words as order_flow.COD
DAYS = "7"   # the owner asked for the last 7 days only; Shopify gives 60 without read_all_orders
PAGE = 250   # Shopify's largest page; more than this in the window is reported, not silently cut
BILL_TTL = 300  # Billingo is asked every 5 minutes, not every round
UPLOADED = HERE.parent / "brain" / "gls_uploaded.json"  # order name -> parcel id, or {"parcel_id", "cod_amount", "at"}
ACTIONS = False  # the site keeps its step buttons switched off until the command runner says otherwise
_bill_cache = {"at": 0.0, "days": None, "docs": None}

def _try(label, fn):
    try: return fn(), None
    except ConnectorError as e: return None, f"[{label}] {e}"

def _uploaded():
    try: return json.loads(UPLOADED.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

def _invoices(bill, days, now):
    """Billingo's list, at most BILL_TTL old. A failed call is never cached: unknown stays unknown until it answers."""
    c = _bill_cache
    if c["docs"] is not None and c["days"] == days and now - c["at"] < BILL_TTL: return c["docs"], None
    docs, err = _try("Billingo invoices", lambda: bill.list_invoices(days=str(int(days) + 7)))
    if docs is not None: c.update(at=now, days=days, docs=docs)
    return docs, err

def _invoice(name, invoices):
    """unknown = Billingo did not answer (never shown as 'no invoice'). '#1024' must not match a comment about '#10245'."""
    if invoices is None: return {"state": "unknown", "number": None, "paid": None}
    inv = next((i for i in invoices if name and re.search(re.escape(name) + r"(?!\d)", i.get("comment") or "")), None)
    if not inv: return {"state": "none", "number": None, "paid": None}
    return {"state": "draft" if inv.get("type") == "draft" else "issued", "number": inv.get("number") or None, "paid": inv.get("paid")}

def _label(entry):
    """What went to MyGLS. Old entries hold only the parcel id (or True): the COD amount of those is not known, so it is not sent,
    and the site shows 'check payment mode' instead of guessing."""
    if not entry: return {"state": "none", "parcel_id": None}
    if isinstance(entry, dict):
        out = {"state": "uploaded", "parcel_id": str(entry["parcel_id"]) if entry.get("parcel_id") not in (None, True) else None}
        if isinstance(entry.get("cod_amount"), (int, float)): out["cod_amount"] = float(entry["cod_amount"])
        if entry.get("at"): out["sent_at"] = entry["at"]
        return out
    return {"state": "uploaded", "parcel_id": None if entry is True else str(entry)}

def _address(a):
    return {k: a.get(k) for k in ADDRESS_KEYS if a.get(k)} if isinstance(a, dict) and a else None

def _private(o):
    sh, b = o.get("shipping_address") or {}, o.get("billing_address") or {}
    return {"customer_name": sh.get("name") or b.get("name") or None, "email": o.get("email") or None,
            "phone": sh.get("phone") or o.get("phone") or b.get("phone") or None,
            "shipping_address": _address(sh), "billing_address": _address(b)}

def _order(o, invoices, uploaded):
    name = o.get("name") or ""
    gateways = [g for g in o.get("payment_gateway_names") or [] if g]
    ships = o.get("fulfillments") or []
    return {"id": str(o["id"]), "name": name, "created_at": o.get("created_at"), "total": float(o.get("total_price") or 0),
            "currency": o.get("currency"), "paid": o.get("financial_status"), "shipped": o.get("fulfillment_status") or "unfulfilled",
            "cancelled": bool(o.get("cancelled_at")),
            "payment_method": ", ".join(gateways) or None,
            "shipping_method": ", ".join(l.get("title") for l in o.get("shipping_lines") or [] if l.get("title")) or None,
            "is_cod": any(c in " ".join(gateways).lower() for c in COD), "shopify_test": bool(o.get("test")),
            "shipped_at": min((f.get("created_at") for f in ships if f.get("created_at")), default=None),
            "tracking": [t for f in ships for t in (f.get("tracking_numbers") or [])],
            "invoice": _invoice(name, invoices), "label": _label(uploaded.get(name)),
            "lines": [{"title": l.get("title"), "variant_id": str(l["variant_id"]) if l.get("variant_id") else None, "sku": l.get("sku") or None,
                       "qty": l.get("quantity", 1), "price": float(l.get("price") or 0)} for l in o.get("line_items") or []],
            "private": _private(o)}

def _variants(p):
    return [{"variant_id": str(v["id"]), "product_id": str(p["id"]), "title": p.get("title"),
             "variant_title": None if (v.get("title") or "") == "Default Title" else v.get("title"), "sku": v.get("sku") or None,
             "price": float(v.get("price") or 0), "stock": v.get("inventory_quantity"), "status": p.get("status")} for v in p.get("variants") or []]

TRAFFIC_QUERY = "FROM sessions SHOW sessions, online_store_visitors TIMESERIES day SINCE -{days}d UNTIL today"
GRAPHQL = "query($q: String!) { shopifyqlQuery(query: $q) { tableData { columns { name } rows } parseErrors } }"

def _traffic(shop, days):
    """Store sessions and visitors per day, from Shopify analytics (GraphQL Admin API, shopifyqlQuery; needs the read_reports scope).
    Totals only: nothing here identifies a visitor."""
    host = shop._shop_host()
    try:
        r = requests.post(f"https://{host}/admin/api/{shop.VERSION}/graphql.json", timeout=20, headers={"X-Shopify-Access-Token": shop._token(host)},
                          json={"query": GRAPHQL, "variables": {"q": TRAFFIC_QUERY.format(days=int(days))}})
    except requests.RequestException as e:
        raise ConnectorError(f"Shopify did not answer: {type(e).__name__}")
    if not r.ok: raise ConnectorError(f"Shopify answered {r.status_code}")
    body = r.json()
    if body.get("errors"):
        text = json.dumps(body["errors"], ensure_ascii=False)
        if "ACCESS_DENIED" in text or "access" in text.lower(): raise ConnectorError("the app has no access to analytics: it needs the read_reports scope")
        raise ConnectorError(f"Shopify refused the analytics query: {text[:160]}")
    q = (body.get("data") or {}).get("shopifyqlQuery") or {}
    if q.get("parseErrors"): raise ConnectorError(f"Shopify could not read the analytics query: {str(q['parseErrors'])[:160]}")
    table = q.get("tableData") or {}
    names = [c.get("name") for c in table.get("columns") or []]
    out = []
    for row in table.get("rows") or []:
        d = row if isinstance(row, dict) else dict(zip(names, row))
        if d.get("day"): out.append({"day": str(d["day"])[:10], "sessions": int(d.get("sessions") or 0), "visitors": int(d.get("online_store_visitors") or 0)})
    return out

def build(days=DAYS, shop=None, bill=None, now=None):
    """The whole payload. A list that could not be read is None, never an empty list: the site then keeps what it has.
    NOT a CLI op: the result holds customer data and must only go to the site."""
    shop = shop or _load("shopify"); bill = bill or _load("billingo")
    now = now or time.time()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).isoformat(timespec="seconds")
    raw, shop_err = _try("Shopify orders", lambda: shop._get("orders.json", {"status": "any", "limit": PAGE, "created_at_min": since,
                                                                               "order": "created_at desc", "fields": ORDER_FIELDS}).get("orders", []))
    invoices, bill_err = _invoices(bill, days, now)
    prods, prod_err = _try("Shopify products", lambda: shop._get("products.json", {"limit": PAGE, "fields": PRODUCT_FIELDS}).get("products", []))
    traffic, traffic_err = _try("Shopify visitors", lambda: _traffic(shop, days))
    errors = [e for e in (shop_err, bill_err, prod_err, traffic_err) if e]
    if raw is not None and len(raw) >= PAGE: errors.append(f"[Shopify orders] more than {PAGE} orders in {days} days: only the newest {PAGE} are shown")
    if prods is not None and len(prods) >= PAGE: errors.append(f"[Shopify products] more than {PAGE} products: only the first {PAGE} are shown")
    uploaded = _uploaded()
    return {"source": "jinnee-ecom", "sent_at": int(now), "window_days": int(days), "actions_enabled": ACTIONS, "errors": errors,
            "automations": [{"key": "shopify_orders", "label": "Shopify orders", "ok": shop_err is None, "error": shop_err},
                            {"key": "billingo", "label": "Billingo invoices", "ok": bill_err is None, "error": bill_err},
                            {"key": "shopify_products", "label": "Shopify products", "ok": prod_err is None, "error": prod_err},
                            {"key": "shopify_visitors", "label": "Shopify visitors", "ok": traffic_err is None, "error": traffic_err}],
            "traffic": traffic,  # None = could not be read: the site keeps what it has and says so, never shows zero
            "orders": None if raw is None else [_order(o, invoices, uploaded) for o in raw],
            "products": None if prods is None else [v for p in prods for v in _variants(p)]}

def push(send="no", days=DAYS, shop=None, bill=None):
    payload = build(days, shop, bill)
    count = {k: None if payload[k] is None else len(payload[k]) for k in ("orders", "products")}
    count["traffic_days"] = None if payload["traffic"] is None else len(payload["traffic"])
    count["visitors"] = None if payload["traffic"] is None else sum(t["visitors"] for t in payload["traffic"])
    private = [o["private"] for o in payload["orders"] or []]  # how many orders came with contact data: numbers only
    count.update(with_name=sum(1 for p in private if p["customer_name"]), with_address=sum(1 for p in private if p["shipping_address"]),
                 cod=sum(1 for o in payload["orders"] or [] if o["is_cod"]), paid=sum(1 for o in payload["orders"] or [] if o["paid"] == "paid"))
    if send != "yes":  # counts only: the payload itself holds customer data and is never printed
        return {"dry_run": True, **count, "errors": payload["errors"], "next": "run again with --send yes to send it to the site"}
    site, secret = need("ECOM_SITE_URL", "ECOM_INGEST_SECRET")
    if not site.lower().startswith("https://"): raise ConnectorError("[send to site] ECOM_SITE_URL must start with https://: customer data is not sent unencrypted")
    try:
        r = requests.post(site.rstrip("/") + "/api/ecom/ingest", json=payload, timeout=30, headers={"x-ecom-secret": secret})
    except requests.RequestException as e:
        raise ConnectorError(f"[send to site] the site did not answer: {type(e).__name__}")
    if r.status_code == 401: raise ConnectorError("[send to site] the site refused the secret (401): ECOM_INGEST_SECRET differs on the two sides")
    if r.status_code == 503: raise ConnectorError("[send to site] the site has no ECOM_INGEST_SECRET set (503)")
    if not r.ok: raise ConnectorError(f"[send to site] the site answered {r.status_code}")
    try: new = len(r.json().get("new_order_ids") or [])
    except ValueError: new = None
    return {"sent": True, **count, "new_orders": new, "errors": payload["errors"]}

def loop(seconds=""):
    """Runs until stopped. A failed round is printed once per distinct reason and tried again at the next round."""
    wait, last = max(int(seconds or os.getenv("ECOM_SYNC_SECONDS", "60")), 30), None
    while True:
        try: out = push("yes"); note = "; ".join(out["errors"]) or "ok"
        except ConnectorError as e: note = str(e)
        if note != last: print(f"ecom sync: {note}", flush=True); last = note
        time.sleep(wait)

def status():
    """Which settings this machine has for the site: names only, the secret itself is never shown."""
    site = os.getenv("ECOM_SITE_URL", "").strip()
    return {"ECOM_SITE_URL": site or "missing", "ECOM_INGEST_SECRET": "set" if os.getenv("ECOM_INGEST_SECRET", "").strip() else "missing",
            "ECOM_SYNC_SECONDS": os.getenv("ECOM_SYNC_SECONDS", "").strip() or "not set (60)"}

OPS = {"push": push, "loop": loop, "status": status}
if __name__ == "__main__": sys.exit(run_cli(OPS))
