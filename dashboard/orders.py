"""Orders panel data: Shopify orders (last 14 days) matched with Billingo invoices. Read-only; cached 60 s.
An order counts as invoiced when an invoice's comment mentions its number (e.g. '#1042'). If a connector does not answer,
the panel says so; it never fills in a guess."""
import sys, time, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors")); sys.path.insert(0, str(ROOT / "connectors" / "shopify")); sys.path.insert(0, str(ROOT / "connectors" / "billingo"))
from cbase import ConnectorError
import importlib.util

def _load(name):
    spec = importlib.util.spec_from_file_location(f"conn_{name}", ROOT / "connectors" / name / "ops.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

CACHE = {"ts": 0, "data": None}
TTL = 60
STALE_HOURS = 2  # same threshold as agents/ops.md

def _try(fn):
    try: return fn(), None
    except ConnectorError as e: return None, str(e)

def build(shop=None, invoicing=None, now=None):
    shop = shop or _load("shopify"); invoicing = invoicing or _load("billingo")
    now = now or time.time()
    orders, shop_err = _try(lambda: shop.list_orders(days="14"))
    invoices, inv_err = _try(lambda: invoicing.list_invoices(days="21"))
    rows = []
    for o in orders or []:
        inv = None
        if invoices is not None:
            inv = next((i for i in invoices if o["name"] and o["name"] in (i["comment"] or "")), None)
        rows.append({**o, "invoice": None if inv_err else ({"number": inv["number"], "paid": inv["paid"]} if inv else False),
                     "label": bool(o["tracking"])})
    paid = [r for r in rows if not r["cancelled"]]
    summary = {"orders": len(paid), "revenue": round(sum(float(r["total"] or 0) for r in paid), 2),
               "currency": paid[0]["currency"] if paid else "",
               "no_invoice": None if inv_err else sum(1 for r in paid if r["invoice"] is False),
               "no_label": sum(1 for r in paid if not r["label"] and r["shipped"] == "unfulfilled")}
    return {"orders": rows[:50], "summary": summary, "errors": [e for e in (shop_err, inv_err) if e], "at": int(now)}

def get():
    if CACHE["data"] is None or time.time() - CACHE["ts"] > TTL:
        CACHE.update(ts=time.time(), data=build())
    return CACHE["data"]
