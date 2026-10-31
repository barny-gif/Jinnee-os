"""Shopify connector. Reads orders, products, the app's scopes and the store's sessions. Writes, each a dry run unless --confirm yes:
`fulfill` (mark an order fulfilled with the GLS tracking number, never e-mailing the customer), `set_house_number` and `mark_paid`
(the owner's two narrow order edits, 2026-10-29; never price, product or customer). Op names follow connectors/README.md.
CLI: python3 connectors/shopify/ops.py list_orders --days 7
     python3 connectors/shopify/ops.py scopes                 (which scopes the token has, and which REQUIRED ones are missing)
     python3 connectors/shopify/ops.py sessions --days 7      (sessions and online store visitors per day; totals only)
     python3 connectors/shopify/ops.py open_count [--since 1792044000]   (counts only: open unfulfilled, new since)
     python3 connectors/shopify/ops.py fulfill --order-id 123 --tracking 1234567890 [--confirm yes]
     python3 connectors/shopify/ops.py set_house_number --order-id 123 --house-number 12/B [--confirm yes]   (GO: edit_shopify_order)
     python3 connectors/shopify/ops.py mark_paid --order-id 123 [--confirm yes]   (COD after the GLS transfer; GO: edit_shopify_order)"""
import os, re, sys, json, time, pathlib, importlib.util, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import requests
from cbase import ConnectorError, need, run_cli

VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-07")
# What the team's work needs (owner, 2026-10-15). write_products is deliberately NOT here: the owner dropped it.
REQUIRED_SCOPES = ("read_orders", "read_products", "read_inventory", "read_all_orders", "read_reports",
                   "read_merchant_managed_fulfillment_orders", "write_merchant_managed_fulfillment_orders",
                   "write_orders")  # write_orders: set_house_number and mark_paid (2026-10-30)

_TOKEN = {"value": "", "until": 0.0}

def _shop_host():
    (store,) = need("SHOPIFY_STORE")
    store = store.replace("https://", "").replace("http://", "").strip("/")
    return store if "." in store else store + ".myshopify.com"

def _token(host):
    """SHOPIFY_ADMIN_TOKEN if set (old custom app). Otherwise the client credentials grant of a Dev Dashboard app:
    SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET are exchanged for a token that lasts 24 h; it is kept in memory and renewed."""
    fixed = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
    if fixed: return fixed
    if _TOKEN["value"] and time.time() < _TOKEN["until"]: return _TOKEN["value"]
    cid, secret = need("SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET")
    try:
        r = requests.post(f"https://{host}/admin/oauth/access_token", timeout=20,
                          data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret})
    except requests.RequestException as e:
        raise ConnectorError(f"Shopify did not answer the token request: {type(e).__name__}")
    if not r.ok: raise ConnectorError(f"Shopify refused the client id/secret ({r.status_code}): is the app installed on this store, and SHOPIFY_STORE correct?")
    d = r.json()
    if not d.get("access_token"): raise ConnectorError("Shopify returned no access token")
    _TOKEN.update(value=d["access_token"], until=time.time() + max(int(d.get("expires_in", 86400)) - 300, 60))
    return _TOKEN["value"]

def _check(r, what):
    if r.status_code == 401:
        _TOKEN.update(value="", until=0.0)
        raise ConnectorError(f"[{what}] Shopify: the token was refused (401) - check the Shopify credentials in .env")
    if r.status_code == 403: raise ConnectorError(f"[{what}] Shopify: missing scope (403) - run the scopes op to see which one")
    if r.status_code == 429: raise ConnectorError(f"[{what}] Shopify: rate limit (429), try again in a minute")
    if r.status_code == 422:
        try: errs = r.json().get("errors")
        except ValueError: errs = None
        raise ConnectorError(f"[{what}] Shopify refused it (422): {json.dumps(errs, ensure_ascii=False)[:300]}")
    if not r.ok: raise ConnectorError(f"[{what}] Shopify answered {r.status_code}")
    return r.json()

def _get(path, params=None, base=None):
    host = _shop_host()
    try:
        r = requests.get(f"https://{host}{base or f'/admin/api/{VERSION}'}/{path}", params=params, timeout=20,
                         headers={"X-Shopify-Access-Token": _token(host)})
    except requests.RequestException as e:
        raise ConnectorError(f"Shopify did not answer: {type(e).__name__}")
    return _check(r, path.split(".json")[0])

def _post(path, body):
    host = _shop_host()
    try:
        r = requests.post(f"https://{host}/admin/api/{VERSION}/{path}", json=body, timeout=30,
                          headers={"X-Shopify-Access-Token": _token(host)})
    except requests.RequestException as e:
        raise ConnectorError(f"[{path.split('.json')[0]}] Shopify did not answer: {type(e).__name__}")
    return _check(r, path.split(".json")[0])

def _order(o):
    """Only what the team needs. No customer name, address or email: order number and country only."""
    tracking = [t for f in o.get("fulfillments") or [] for t in (f.get("tracking_numbers") or [])]
    return {"id": o["id"], "name": o.get("name"), "created_at": o.get("created_at"),
            "total": o.get("total_price"), "currency": o.get("currency"),
            "paid": o.get("financial_status"), "shipped": o.get("fulfillment_status") or "unfulfilled",
            "cancelled": bool(o.get("cancelled_at")), "country": (o.get("shipping_address") or {}).get("country_code"),
            "items": sum(i.get("quantity", 0) for i in o.get("line_items") or []),
            "tracking": tracking, "note": o.get("note") or ""}

def order_private(order_id):
    """FOR CODE ONLY, not in OPS and not in the CLI: the customer's contact data, needed to issue the invoice and the parcel.
    It must never be printed, logged or sent to an agent or Telegram. Only order_flow.py calls this."""
    o = _get(f"orders/{order_id}.json")["order"]
    b = o.get("billing_address") or o.get("shipping_address") or {}
    sh = o.get("shipping_address") or b
    # An automatic or order-level discount is only in discount_allocations; total_discount stays 0 for it (seen live 2026-10-21)
    def off(x): return sum(float(a.get("amount") or 0) for a in x.get("discount_allocations") or []) or float(x.get("total_discount") or 0)
    return {"name": o.get("name"), "id": o["id"], "currency": o.get("currency"), "taxes_included": o.get("taxes_included", True),
            "total": o.get("total_price"),
            "gateways": o.get("payment_gateway_names") or [], "email": o.get("email") or (o.get("customer") or {}).get("email") or "",
            "phone": sh.get("phone") or o.get("phone") or "", "billing": b, "shipping": sh,
            "lines": [{"title": i.get("title"), "sku": i.get("sku"), "qty": i.get("quantity", 1), "price": i.get("price"),
                       "discount": str(off(i))} for i in o.get("line_items") or []],
            "shipping_lines": [{"title": l.get("title"), "price": str(max(float(l.get("price") or 0) - off(l), 0))} for l in o.get("shipping_lines") or []]}

def list_orders(days="7", limit="100"):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).isoformat(timespec="seconds")
    data = _get("orders.json", {"status": "any", "limit": min(int(limit), 250), "created_at_min": since, "order": "created_at desc"})
    return [_order(o) for o in data.get("orders", [])]

def get_order(order_id):
    return _order(_get(f"orders/{order_id}.json")["order"])

def list_products(limit="50"):
    ps = _get("products.json", {"limit": min(int(limit), 250), "fields": "id,title,status,variants"}).get("products", [])
    return [{"id": p["id"], "title": p["title"], "status": p["status"],
             "stock": sum(v.get("inventory_quantity") or 0 for v in p.get("variants", []))} for p in ps]

def get_product(product_id):
    p = _get(f"products/{product_id}.json")["product"]
    return {"id": p["id"], "title": p["title"], "status": p["status"],
            "variants": [{"sku": v.get("sku"), "price": v.get("price"), "stock": v.get("inventory_quantity")} for v in p.get("variants", [])]}

def scopes():
    """Read-only: the scopes the token really has (GET /admin/oauth/access_scopes.json) and which REQUIRED ones are missing.
    Extra scopes are listed but never count as missing."""
    have = sorted(s.get("handle") for s in _get("access_scopes.json", base="/admin/oauth").get("access_scopes", []) if s.get("handle"))
    missing = [s for s in REQUIRED_SCOPES if s not in have]
    return {"ok": not missing, "missing": missing, "required": list(REQUIRED_SCOPES), "scopes": have}

SESSIONS_QUERY = "FROM sessions SHOW sessions, online_store_visitors TIMESERIES day SINCE -{days}d UNTIL today"
GRAPHQL = "query($q: String!) { shopifyqlQuery(query: $q) { tableData { columns { name } rows } parseErrors } }"

def sessions(days="7"):
    """Store sessions and online store visitors per day, oldest first, today included (GraphQL Admin API shopifyqlQuery; needs
    read_reports). Totals only: the query never selects or groups by anything about a customer."""
    host = _shop_host()
    try:
        r = requests.post(f"https://{host}/admin/api/{VERSION}/graphql.json", timeout=20, headers={"X-Shopify-Access-Token": _token(host)},
                          json={"query": GRAPHQL, "variables": {"q": SESSIONS_QUERY.format(days=int(days))}})
    except requests.RequestException as e:
        raise ConnectorError(f"[sessions] Shopify did not answer: {type(e).__name__}")
    body = _check(r, "sessions")
    if body.get("errors"):
        text = json.dumps(body["errors"], ensure_ascii=False)
        if "ACCESS_DENIED" in text or "access" in text.lower(): raise ConnectorError("[sessions] the app has no access to analytics: it needs the read_reports scope")
        raise ConnectorError(f"[sessions] Shopify refused the analytics query: {text[:160]}")
    q = (body.get("data") or {}).get("shopifyqlQuery") or {}
    if q.get("parseErrors"): raise ConnectorError(f"[sessions] Shopify could not read the analytics query: {str(q['parseErrors'])[:160]}")
    table = q.get("tableData") or {}
    names = [c.get("name") for c in table.get("columns") or []]
    out = []
    for row in table.get("rows") or []:
        d = row if isinstance(row, dict) else dict(zip(names, row))
        if d.get("day"): out.append({"day": str(d["day"])[:10], "sessions": int(float(d.get("sessions") or 0)),
                                     "visitors": int(float(d.get("online_store_visitors") or 0))})
    return sorted(out, key=lambda x: x["day"])

FULFILLABLE = ("open", "in_progress")

def open_count(since=""):
    """Counts only, no order data: open orders that are not (fully) fulfilled yet and, when `since` is given (epoch seconds
    or an ISO time), orders created after it. The scheduler asks this before the hourly order watch."""
    out = {"open_unfulfilled": int(_get("orders/count.json", {"status": "open", "fulfillment_status": "unfulfilled"}).get("count") or 0)}
    if str(since).strip():
        try: when = dt.datetime.fromtimestamp(float(since), dt.timezone.utc)
        except (TypeError, ValueError):
            try: when = dt.datetime.fromisoformat(str(since).strip().replace("Z", "+00:00"))
            except ValueError: raise ConnectorError("--since must be epoch seconds or an ISO date/time")
            if not when.tzinfo: when = when.replace(tzinfo=dt.timezone.utc)
        out["new_since"] = int(_get("orders/count.json", {"status": "any", "created_at_min": when.isoformat(timespec="seconds")}).get("count") or 0)
    return out

def fulfill(order_id, tracking, company="GLS", confirm="no"):
    """Mark an order fulfilled in Shopify with the parcel's tracking number. notify_customer is always false: the customer gets no
    e-mail from this (that would need its own approval). Idempotent: only fulfillment orders that are still open (and allow
    create_fulfillment) are sent; when none is left, nothing is created and 'already' is true. Dry run unless confirm == 'yes'."""
    if not str(order_id).strip().isdigit(): raise ConnectorError("[fulfill] order_id must be the Shopify order id (digits)")
    tracking = str(tracking or "").strip()
    if not tracking: raise ConnectorError("[fulfill] no tracking number: an order is only fulfilled with its GLS parcel number")
    fos = _get(f"orders/{int(order_id)}/fulfillment_orders.json").get("fulfillment_orders", [])
    open_ids = [f["id"] for f in fos if f.get("status") in FULFILLABLE and "create_fulfillment" in (f.get("supported_actions") or [])]
    if not open_ids: return {"order_id": str(order_id), "already": True, "created": False, "tracking": tracking,
                             "statuses": sorted({f.get("status") for f in fos if f.get("status")})}
    body = {"fulfillment": {"line_items_by_fulfillment_order": [{"fulfillment_order_id": i} for i in open_ids],
                            "tracking_info": {"number": tracking, "company": company}, "notify_customer": False}}
    if confirm != "yes":
        return {"order_id": str(order_id), "dry_run": True, "already": False, "fulfillment_orders": len(open_ids), "tracking": tracking,
                "notify_customer": False, "next": "run again with --confirm yes to fulfill it in Shopify"}
    f = _post("fulfillments.json", body).get("fulfillment") or {}
    return {"order_id": str(order_id), "already": False, "created": True, "fulfillment_id": str(f.get("id") or ""), "status": f.get("status"),
            "tracking": tracking, "notify_customer": False}

def _graphql(query, variables, what):
    host = _shop_host()
    try:
        r = requests.post(f"https://{host}/admin/api/{VERSION}/graphql.json", timeout=30, headers={"X-Shopify-Access-Token": _token(host)},
                          json={"query": query, "variables": variables})
    except requests.RequestException as e:
        raise ConnectorError(f"[{what}] Shopify did not answer: {type(e).__name__}")
    body = _check(r, what)
    if body.get("errors"): raise ConnectorError(f"[{what}] Shopify refused it: {json.dumps(body['errors'], ensure_ascii=False)[:300]}")
    return body.get("data") or {}

def _user_errors(payload, what):
    errs = (payload or {}).get("userErrors") or []
    if errs: raise ConnectorError(f"[{what}] Shopify refused it: " + "; ".join(str(e.get("message")) for e in errs)[:300])

def _split_street(address):
    """The same street / house number split the label and the checks use (order_flow.split_street), loaded lazily."""
    spec = importlib.util.spec_from_file_location("shopify_order_flow", pathlib.Path(__file__).resolve().parent.parent / "order_flow.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m.split_street(address)

HOUSE = re.compile(r"\d[\w/.\-]*(?:\s*(?:em|fsz|ép|lh)\.?\s*\S*)?", re.I)
ORDER_UPDATE = "mutation($input: OrderInput!) { orderUpdate(input: $input) { order { id } userErrors { field message } } }"
# MailingAddressInput (Admin GraphQL): shippingAddress REPLACES the address, so every field that is there now is sent back
ADDRESS_FIELDS = (("first_name", "firstName"), ("last_name", "lastName"), ("company", "company"), ("address1", "address1"),
                  ("address2", "address2"), ("city", "city"), ("zip", "zip"), ("country_code", "countryCode"),
                  ("province_code", "provinceCode"), ("phone", "phone"))

def set_house_number(order_id, house_number, confirm="no"):
    """Owner's rule (lessons 2026-10-29): add the missing house number to the order's SHIPPING address in Shopify, only with the number
    the owner gave, only after a GO. The street stays as it is; the number goes at the end of address1. Refused when the order is
    cancelled, has no shipping address, or already has a house number. The billing address cannot be changed through the API
    (OrderInput has no billingAddress). The output names the order and the number only, never the address. Dry run unless --confirm yes."""
    if not str(order_id).strip().isdigit(): raise ConnectorError("[house number] order_id must be the Shopify order id (digits)")
    house = re.sub(r"\s+", " ", str(house_number or "")).strip()
    if not HOUSE.fullmatch(house) or len(house) > 20:
        raise ConnectorError("[house number] give the house number only, starting with a digit (e.g. 12, 12/B, 3. 2. em)")
    o = _get(f"orders/{int(order_id)}.json")["order"]
    name = o.get("name")
    if o.get("cancelled_at"): raise ConnectorError(f"[house number] {name} is cancelled: nothing is written")
    sh = o.get("shipping_address")
    if not sh: raise ConnectorError(f"[house number] {name} has no shipping address in Shopify")
    a1, a2 = (sh.get("address1") or "").strip(), (sh.get("address2") or "").strip()
    if _split_street(" ".join(x for x in (a1, a2) if x))[1]:
        return {"order": name, "already": True, "written": False, "next": "the shipping address already has a house number: nothing to do"}
    new_a1 = f"{a1} {house}".strip()
    street, got = _split_street(" ".join(x for x in (new_a1, a2) if x))
    if got != house or not street:
        raise ConnectorError(f"[house number] {name}: after adding it the address would still not split into street + '{house}'; fix it in Shopify by hand")
    out = {"order": name, "house_number": house, "field": "shipping address1", "billing_changed": False}
    if confirm != "yes": return {**out, "dry_run": True, "next": "run again with --confirm yes to write it to Shopify"}
    addr = {gq: sh.get(rest) for rest, gq in ADDRESS_FIELDS if sh.get(rest) not in (None, "")}
    addr["address1"] = new_a1
    data = _graphql(ORDER_UPDATE, {"input": {"id": f"gid://shopify/Order/{int(order_id)}", "shippingAddress": addr}}, "house number")
    _user_errors(data.get("orderUpdate"), "house number")
    back = (_get(f"orders/{int(order_id)}.json")["order"].get("shipping_address") or {})
    ok = _split_street(" ".join(x for x in ((back.get("address1") or "").strip(), (back.get("address2") or "").strip()) if x))[1] == house
    if not ok: raise ConnectorError(f"[house number] {name}: Shopify accepted it, but reading it back shows no house number; check the order by hand")
    return {**out, "written": True}

MARK_PAID = "mutation($input: OrderMarkAsPaidInput!) { orderMarkAsPaid(input: $input) { order { id displayFinancialStatus } userErrors { field message } } }"
COD_GATEWAYS = ("cash on delivery", "cod", "utánvét", "utanvet")

def mark_paid(order_id, confirm="no"):
    """Owner's rule (lessons 2026-10-29): a cash-on-delivery order is set to paid in Shopify when GLS has transferred the money,
    not at delivery, only after a GO. Refused unless the order is open, cash on delivery and still 'pending'. Dry run unless --confirm yes."""
    if not str(order_id).strip().isdigit(): raise ConnectorError("[mark paid] order_id must be the Shopify order id (digits)")
    o = _get(f"orders/{int(order_id)}.json")["order"]
    name, status = o.get("name"), o.get("financial_status")
    if o.get("cancelled_at"): raise ConnectorError(f"[mark paid] {name} is cancelled: nothing is written")
    if not any(c in " ".join(o.get("payment_gateway_names") or []).lower() for c in COD_GATEWAYS):
        raise ConnectorError(f"[mark paid] {name} is not cash on delivery: only COD orders are set to paid from here")
    if status == "paid": return {"order": name, "already": True, "written": False, "status": status}
    if status != "pending": raise ConnectorError(f"[mark paid] {name} is '{status}', not pending: nothing is written")
    out = {"order": name, "total": o.get("total_price"), "currency": o.get("currency"), "status": status}
    if confirm != "yes": return {**out, "dry_run": True, "next": "run again with --confirm yes to mark it paid in Shopify"}
    data = _graphql(MARK_PAID, {"input": {"id": f"gid://shopify/Order/{int(order_id)}"}}, "mark paid")
    p = data.get("orderMarkAsPaid") or {}
    _user_errors(p, "mark paid")
    return {**out, "written": True, "status": ((p.get("order") or {}).get("displayFinancialStatus") or "").lower()}

OPS = {"list_orders": list_orders, "get_order": get_order, "list_products": list_products, "get_product": get_product,
       "scopes": scopes, "sessions": sessions, "open_count": open_count, "fulfill": fulfill,
       "set_house_number": set_house_number, "mark_paid": mark_paid}
if __name__ == "__main__": sys.exit(run_cli(OPS))
