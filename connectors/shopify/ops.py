"""Shopify connector, READ-ONLY (scopes: read_orders, read_products, read_inventory). Op names follow connectors/README.md.
CLI: python3 connectors/shopify/ops.py list_orders --days 7"""
import os, sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import requests
from cbase import ConnectorError, need, run_cli

VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-07")

def _get(path, params=None):
    store, token = need("SHOPIFY_STORE", "SHOPIFY_ADMIN_TOKEN")
    store = store.replace("https://", "").replace("http://", "").strip("/")
    if "." not in store: store += ".myshopify.com"
    try:
        r = requests.get(f"https://{store}/admin/api/{VERSION}/{path}", params=params, timeout=20,
                         headers={"X-Shopify-Access-Token": token})
    except requests.RequestException as e:
        raise ConnectorError(f"Shopify did not answer: {type(e).__name__}")
    if r.status_code == 401: raise ConnectorError("Shopify: the token was refused (401) - check SHOPIFY_ADMIN_TOKEN")
    if r.status_code == 403: raise ConnectorError("Shopify: missing scope (403) - the custom app needs read_orders")
    if r.status_code == 429: raise ConnectorError("Shopify: rate limit (429), try again in a minute")
    if not r.ok: raise ConnectorError(f"Shopify answered {r.status_code}")
    return r.json()

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
    return {"name": o.get("name"), "id": o["id"], "currency": o.get("currency"), "taxes_included": o.get("taxes_included", True),
            "gateways": o.get("payment_gateway_names") or [], "email": o.get("email") or (o.get("customer") or {}).get("email") or "",
            "phone": sh.get("phone") or o.get("phone") or "", "billing": b, "shipping": sh,
            "lines": [{"title": i.get("title"), "sku": i.get("sku"), "qty": i.get("quantity", 1), "price": i.get("price"),
                       "discount": i.get("total_discount") or "0"} for i in o.get("line_items") or []],
            "shipping_lines": [{"title": l.get("title"), "price": l.get("price")} for l in o.get("shipping_lines") or []]}

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

OPS = {"list_orders": list_orders, "get_order": get_order, "list_products": list_products, "get_product": get_product}
if __name__ == "__main__": sys.exit(run_cli(OPS))
