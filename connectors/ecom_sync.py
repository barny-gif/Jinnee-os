"""Shop -> the admin site (jinnee-os.com). Sends the last days' orders (plus older ones up to OPEN_DAYS while still open) with their payment, invoice and parcel state, and the
product list, the store's visitors and the Shopify app's scopes. It reads Shopify, Billingo, MyGLS tracking (each parcel at most
hourly, state in brain/gls_status.json) and brain/gls_uploaded.json. Its one write: when GLS has picked a parcel up, the order is
marked fulfilled in Shopify with the parcel number, without e-mailing the customer (owner, 2026-10-15), once per order
(brain/shopify_fulfilled.json); only when really sending, never in a dry run. OFF unless SHOPIFY_AUTO_FULFILL=yes, and once on
it only fulfills parcels picked up after it was switched on (brain/shopify_fulfill_start.json; SHOPIFY_FULFILL_SINCE back-fills).
The site never calls this machine.
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
import order_checks
import order_auto

def _load(name):
    spec = importlib.util.spec_from_file_location(f"sync_{name}", HERE / name / "ops.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

ORDER_FIELDS = ("id,name,created_at,total_price,currency,financial_status,fulfillment_status,cancelled_at,line_items,fulfillments,"
                "payment_gateway_names,shipping_lines,test,email,phone,shipping_address,billing_address")
PRODUCT_FIELDS = "id,title,status,variants"
ADDRESS_KEYS = ("name", "company", "address1", "address2", "zip", "city", "province", "country_code", "phone")
COD = ("cash on delivery", "cod", "utánvét", "utanvet")  # the same words as order_flow.COD
DAYS = "7"   # the owner asked for the last 7 days only; Shopify gives 60 without read_all_orders
OPEN_DAYS = "30"  # an older order still goes while it is open (not delivered, no invoice, or not paid): a COD parcel often takes longer than 7 days (#1024, 2026-10-20)
PAGE = 250   # Shopify's largest page; more than this in the window is reported, not silently cut
BILL_TTL = 300  # Billingo is asked every 5 minutes, not every round
UPLOADED = HERE.parent / "brain" / "gls_uploaded.json"  # order name -> parcel id, or {"parcel_id", "cod_amount", "at"}
ACTIONS = False  # the site keeps its step buttons switched off until the command runner says otherwise
GLS_STATUS = HERE.parent / "brain" / "gls_status.json"  # order name -> parcel id, parcel number, GLS state, when last asked
GLS_TTL = 3600       # a parcel is asked from GLS at most once an hour
GLS_RETRY = 600      # after a failed GLS call, wait 10 minutes before asking again
GLS_FINAL = ("delivered", "returned")  # never asked again
FULFILLED = HERE.parent / "brain" / "shopify_fulfilled.json"  # order name -> {order_id, tracking, at, fulfillment_id} or {..., error, tried}
FULFILL_RETRY = 1800  # a failed Shopify fulfillment is tried again after 30 minutes, not every round
PICKED_UP = ("handed_to_gls", "in_transit", "out_for_delivery", "delivered", "returned", "problem")  # GLS had the parcel
# When automatic fulfillment was first switched on: only parcels GLS picked up at or after this moment are fulfilled, so turning
# it on never back-fills older orders. SHOPIFY_FULFILL_SINCE (ISO date/time) in .env is the explicit way to ask for older ones.
FULFILL_START = HERE.parent / "brain" / "shopify_fulfill_start.json"
PAY_TTL = 3600  # the Payments tab (bank accounts, paid / open / overdue invoices) is asked from Billingo once an hour
_bill_cache = {"at": 0.0, "days": None, "docs": None}
_pay_cache = {"at": 0.0, "data": None}
_gls_fail = {"at": 0.0, "error": None}

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

def _billing(bill, now):
    """The Payments tab, at most PAY_TTL old. None = Billingo did not answer: the site keeps what it has (never zero).
    Holds no partner data: invoice and order numbers, dates, totals, and the bank account's name and last 4 characters."""
    c = _pay_cache
    if c["data"] is not None and now - c["at"] < PAY_TTL: return c["data"], None
    data, err = _try("Billingo payments", bill.payments)
    if data is None: return None, err
    c.update(at=now, data={**data, "checked_at": _iso(now)})
    return c["data"], None

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

def _parcel_id(entry):
    pid = entry.get("parcel_id") if isinstance(entry, dict) else entry
    return None if pid in (None, True, "") else str(pid)

def _shipping(uploaded, gls, now):
    """GLS state per uploaded order, kept in brain/gls_status.json. Each parcel is asked at most once an hour, never again once
    delivered or returned. A failed call keeps the stored state (unknown stays unknown) and is retried after GLS_RETRY.
    A replaced label (new parcel id) starts over."""
    try: state = json.loads(GLS_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError): state = {}
    if now - _gls_fail["at"] < GLS_RETRY: return state, _gls_fail["error"]
    numbers, changed = None, False
    try:
        for name, entry in uploaded.items():
            pid = _parcel_id(entry)
            if not pid: continue
            s = state.get(name) if (state.get(name) or {}).get("parcel_id") == pid else {"parcel_id": pid}
            if s.get("state") in GLS_FINAL or now - s.get("checked", 0) < GLS_TTL: state[name] = s; continue
            if not s.get("parcel_number"):
                if numbers is None: numbers = gls.parcel_numbers("30")
                s["parcel_number"] = numbers.get(pid)
            s.update(gls.classify(gls.statuses(s["parcel_number"])) if s["parcel_number"] else gls.classify([]))
            s["checked"] = int(now); s["checked_at"] = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds")
            state[name] = s; changed = True
    except ConnectorError as e:
        _gls_fail.update(at=now, error=f"[GLS tracking] {e}")
        return state, _gls_fail["error"]
    finally:
        if changed:
            GLS_STATUS.parent.mkdir(parents=True, exist_ok=True)
            tmp = GLS_STATUS.with_suffix(".tmp"); tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(GLS_STATUS)
    _gls_fail.update(at=0.0, error=None)
    return state, None

def _ship_fields(s, is_cod, invoice):
    """What the site gets. 'delivered' only when GLS was asked: absent means the site keeps what it has."""
    if not s or not s.get("checked_at"): return {}
    out = {"shipping": {k: s.get(k) for k in ("state", "code", "text", "status_at", "handed_at", "delivered_at", "checked_at", "parcel_number")},
           "delivered": {"state": "yes" if s.get("state") == "delivered" else "no", "at": s.get("delivered_at")}}
    # COD: the invoice is due once the customer took the parcel (lessons 2026-10-13). A flag only: nothing is issued from here.
    out["cod_invoice_due"] = bool(is_cod and s.get("state") == "delivered" and invoice.get("state") == "none")
    return out

def _auto_fulfill():
    """OFF by default (owner, 2026-10-15): only SHOPIFY_AUTO_FULFILL=yes in .env switches the automatic Shopify fulfillment on."""
    return os.getenv("SHOPIFY_AUTO_FULFILL", "").strip().lower() == "yes"

def _iso(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")

def _ts(value):
    """ISO date or date/time -> epoch seconds (a date or a time without zone counts as UTC); None if unreadable."""
    try: d = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError: return None
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()

def _fulfill_since(now, write=True):
    """→ (epoch seconds, error). SHOPIFY_FULFILL_SINCE wins (explicit back-fill); otherwise the moment recorded in
    brain/shopify_fulfill_start.json, written the first time this really runs with fulfillment switched on (a dry run
    only assumes 'now' and writes nothing)."""
    env = os.getenv("SHOPIFY_FULFILL_SINCE", "").strip()
    if env:
        t = _ts(env)
        return (t, None) if t is not None else (None, "[Shopify fulfillment] SHOPIFY_FULFILL_SINCE is not a date: nothing fulfilled")
    try:
        t = _ts(json.loads(FULFILL_START.read_text(encoding="utf-8"))["since"])
        if t is not None: return t, None
    except (OSError, ValueError, KeyError, TypeError): pass
    if not write: return now, None
    FULFILL_START.parent.mkdir(parents=True, exist_ok=True)
    FULFILL_START.write_text(json.dumps({"since": _iso(now), "note": "auto fulfillment switched on; older pickups are not back-filled"}), encoding="utf-8")
    return now, None

def _fulfilled():
    try: return json.loads(FULFILLED.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

def _fulfillments(raw, shipping, shop, now, act=False):
    """Shopify fulfillment once GLS has picked the parcel up, with its parcel number, never e-mailing the customer.
    Never twice: an order already fulfilled in Shopify, or recorded in brain/shopify_fulfilled.json, is not sent again, and
    shop.fulfill only uses fulfillment orders that are still open. A failure is recorded and retried after FULFILL_RETRY.
    The site's Barter / Influencer / Teszt / Ajándék mark is not visible from here: such orders are not skipped, only noted
    (they normally have no GLS label, so they never reach this point). act=False (dry run): nothing is sent or written, the
    names are only listed. → (record, errors, names fulfilled (or, in a dry run, to be fulfilled) in this round)"""
    done, errors, new, changed = _fulfilled(), [], [], False
    if raw is None or not _auto_fulfill(): return done, errors, new  # off by default: nothing is read, written or sent
    since, err = _fulfill_since(now, write=act)
    if err: return done, [err], new
    try:
        for o in raw:
            name = o.get("name") or ""
            s = (shipping or {}).get(name) or {}
            rec = done.get(name) or {}
            if rec.get("at") or o.get("cancelled_at"): continue
            if (o.get("fulfillment_status") or "") == "fulfilled": continue  # done in Shopify by hand: nothing to do
            if not (s.get("handed_at") or s.get("state") in PICKED_UP) or not s.get("parcel_number"): continue
            # Picked up before fulfillment was switched on (or pickup time unknown): never back-filled unless SHOPIFY_FULFILL_SINCE asks
            handed = _ts(s["handed_at"]) if s.get("handed_at") else None
            if handed is None or handed < since: continue
            if rec.get("error") and now - rec.get("tried", 0) < FULFILL_RETRY: errors.append(rec["error"]); continue
            if not act: new.append(name); continue
            kind = any(c.get("field") == "kind" for c in order_checks.check(o))
            if kind: print(f"ecom sync: {name} may be barter / influencer / gift / test (0 Ft or test order); fulfilled anyway, the site mark is not visible here", flush=True)
            try:
                out = shop.fulfill(str(o["id"]), s["parcel_number"], confirm="yes")
            except ConnectorError as e:
                done[name] = {"order_id": str(o["id"]), "tracking": s["parcel_number"], "error": f"[Shopify fulfillment {name}] {e}", "tried": int(now)}
                errors.append(done[name]["error"]); changed = True; continue
            done[name] = {"order_id": str(o["id"]), "tracking": s["parcel_number"], "fulfillment_id": out.get("fulfillment_id") or None,
                          "already": bool(out.get("already")), "at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds")}
            changed = True
            if not out.get("already"): new.append(name)
    finally:
        if changed:
            FULFILLED.parent.mkdir(parents=True, exist_ok=True)
            tmp = FULFILLED.with_suffix(".tmp"); tmp.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(FULFILLED)
    return done, errors, new

def _fulfill_fields(o, rec):
    """What the site shows on the 'Shopify: teljesítve' step: fulfilled when Shopify says so or this machine did it."""
    if (o.get("fulfillment_status") or "") == "fulfilled":
        tr = [t for f in o.get("fulfillments") or [] for t in (f.get("tracking_numbers") or [])]
        return {"state": "fulfilled", "tracking": (rec or {}).get("tracking") or (tr[0] if tr else None), "at": (rec or {}).get("at"), "by": "shopify" if not (rec or {}).get("at") else "auto", "error": None}
    if (rec or {}).get("at"): return {"state": "fulfilled", "tracking": rec.get("tracking"), "at": rec["at"], "by": "auto", "error": None}
    if (rec or {}).get("error"): return {"state": "failed", "tracking": rec.get("tracking"), "at": None, "by": None, "error": rec["error"]}
    return {"state": "pending", "tracking": None, "at": None, "by": None, "error": None}

def _address(a):
    return {k: a.get(k) for k in ADDRESS_KEYS if a.get(k)} if isinstance(a, dict) and a else None

def _private(o):
    sh, b = o.get("shipping_address") or {}, o.get("billing_address") or {}
    return {"customer_name": sh.get("name") or b.get("name") or None, "email": o.get("email") or None,
            "phone": sh.get("phone") or o.get("phone") or b.get("phone") or None,
            "shipping_address": _address(sh), "billing_address": _address(b)}

def _order(o, invoices, uploaded, shipping=None, fulfilled=None):
    name = o.get("name") or ""
    gateways = [g for g in o.get("payment_gateway_names") or [] if g]
    ships = o.get("fulfillments") or []
    is_cod = any(c in " ".join(gateways).lower() for c in COD); invoice = _invoice(name, invoices)
    return {**_ship_fields((shipping or {}).get(name), is_cod, invoice), "id": str(o["id"]), "name": name, "created_at": o.get("created_at"), "total": float(o.get("total_price") or 0),
            "currency": o.get("currency"), "paid": o.get("financial_status"), "shipped": o.get("fulfillment_status") or "unfulfilled",
            "cancelled": bool(o.get("cancelled_at")),
            "payment_method": ", ".join(gateways) or None,
            "shipping_method": ", ".join(l.get("title") for l in o.get("shipping_lines") or [] if l.get("title")) or None,
            "is_cod": is_cod, "shopify_test": bool(o.get("test")),
            "shipped_at": min((f.get("created_at") for f in ships if f.get("created_at")), default=None),
            "tracking": [t for f in ships for t in (f.get("tracking_numbers") or [])],
            "invoice": invoice, "label": _label(uploaded.get(name)),
            "shopify_fulfillment": _fulfill_fields(o, (fulfilled or {}).get(name)),
            "checks": order_checks.check(o),  # what is missing before the draft / the label: field names only, never the data
            "lines": [{"title": l.get("title"), "variant_id": str(l["variant_id"]) if l.get("variant_id") else None, "sku": l.get("sku") or None,
                       "qty": l.get("quantity", 1), "price": float(l.get("price") or 0)} for l in o.get("line_items") or []],
            "private": _private(o)}

def _closed(row):
    """Nothing left to happen: cancelled, or the parcel's journey is over (delivered / returned) and its invoice is issued and paid.
    Unknown (Billingo or GLS did not answer) counts as open, so the order keeps going to the site."""
    if row["cancelled"]: return True
    inv = row["invoice"]
    return (row.get("shipping") or {}).get("state") in GLS_FINAL and inv["state"] == "issued" and inv["paid"] == "paid"

def _in_window(o, since):
    return (_ts(o.get("created_at")) or 0) >= since

def _variants(p):
    return [{"variant_id": str(v["id"]), "product_id": str(p["id"]), "title": p.get("title"),
             "variant_title": None if (v.get("title") or "") == "Default Title" else v.get("title"), "sku": v.get("sku") or None,
             "price": float(v.get("price") or 0), "stock": v.get("inventory_quantity"), "status": p.get("status")} for v in p.get("variants") or []]

def _traffic(shop, days):
    """Store sessions and online store visitors per day (shopify ops `sessions`, read_reports). Totals only."""
    return shop.sessions(str(days))

def _visitors(traffic, now):
    """The Overview tile's numbers: today and the last 7 days (today included), from the daily rows. None when unreadable."""
    if traffic is None: return None
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).astimezone(_tz()).date()
    week = {(today - dt.timedelta(days=i)).isoformat() for i in range(7)}
    t = next((r for r in traffic if r["day"] == today.isoformat()), {"sessions": 0, "visitors": 0})
    w = [r for r in traffic if r["day"] in week]
    return {"today": {"sessions": t["sessions"], "visitors": t["visitors"]},
            "last_7_days": {"sessions": sum(r["sessions"] for r in w), "visitors": sum(r["visitors"] for r in w)},
            "days": [r for r in traffic if r["day"] in week], "day": today.isoformat()}

def _tz():
    """The shop's day boundary (Shopify analytics days are in the shop's time zone): JINNEE_TZ, else Europe/Budapest."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("JINNEE_TZ", "").strip() or "Europe/Budapest")
    except Exception:
        return dt.timezone.utc

def build(days=DAYS, shop=None, bill=None, now=None, gls=None, act=False):
    """The whole payload. A list that could not be read is None, never an empty list: the site then keeps what it has.
    act=True also fulfills picked-up orders in Shopify (only when the payload is really sent).
    NOT a CLI op: the result holds customer data and must only go to the site."""
    shop = shop or _load("shopify"); bill = bill or _load("billingo"); gls = gls or _load("gls")
    now = now or time.time()
    reach = str(max(int(days), int(OPEN_DAYS)))  # read back far enough to keep sending the orders that are still open
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(reach))).isoformat(timespec="seconds")
    every, shop_err = _try("Shopify orders", lambda: shop._get("orders.json", {"status": "any", "limit": PAGE, "created_at_min": since,
                                                                                 "order": "created_at desc", "fields": ORDER_FIELDS}).get("orders", []))
    recent_since = now - int(days) * 86400
    raw = None if every is None else [o for o in every if _in_window(o, recent_since)]  # the new-order chain (label + draft) sees the last days only
    invoices, bill_err = _invoices(bill, reach, now)
    billing, pay_err = _billing(bill, now)
    prods, prod_err = _try("Shopify products", lambda: shop._get("products.json", {"limit": PAGE, "fields": PRODUCT_FIELDS}).get("products", []))
    traffic, traffic_err = _try("Shopify visitors", lambda: _traffic(shop, days))
    scopes, scopes_err = _try("Shopify scopes", shop.scopes)
    # New orders: MyGLS label and invoice draft at once, only when the autonomy table allows it and no required field is missing
    auto = order_auto.run(raw, now, act=act, uploaded=_uploaded())
    auto_err = "; ".join(dict.fromkeys(auto["errors"])) or None
    uploaded = _uploaded()  # read again: the automatic upload may have just added an order
    shipping, gls_err = _shipping(uploaded, gls, now)
    fulfilled, fulfill_errs, fulfilled_now = _fulfillments(every, shipping, shop, now, act)  # an older open order may still be picked up; the switch-on moment still limits it
    fulfill_err = "; ".join(dict.fromkeys(fulfill_errs)) or None
    errors = [e for e in (shop_err, bill_err, pay_err, prod_err, traffic_err, gls_err, scopes_err, fulfill_err, auto_err) if e]
    if billing is not None and not billing["complete"]: errors.append("[Billingo payments] more than 1000 invoices in the window: only the newest are shown")
    if scopes and scopes["missing"]: errors.append(f"[Shopify scopes] missing: {', '.join(scopes['missing'])}")
    if every is not None and len(every) >= PAGE: errors.append(f"[Shopify orders] more than {PAGE} orders in {reach} days: only the newest {PAGE} are shown")
    if prods is not None and len(prods) >= PAGE: errors.append(f"[Shopify products] more than {PAGE} products: only the first {PAGE} are shown")
    return {"source": "jinnee-ecom", "sent_at": int(now), "window_days": int(days), "actions_enabled": ACTIONS, "errors": errors,
            "automations": [{"key": "shopify_orders", "label": "Shopify orders", "ok": shop_err is None, "error": shop_err},
                            {"key": "billingo", "label": "Billingo invoices", "ok": bill_err is None, "error": bill_err},
                            {"key": "billingo_payments", "label": "Billingo payments (bank sync)", "ok": pay_err is None, "error": pay_err},
                            {"key": "shopify_products", "label": "Shopify products", "ok": prod_err is None, "error": prod_err},
                            {"key": "shopify_visitors", "label": "Shopify visitors", "ok": traffic_err is None, "error": traffic_err},
                            {"key": "gls_tracking", "label": "GLS tracking", "ok": gls_err is None, "error": gls_err},
                            {"key": "shopify_fulfillment", "label": "Shopify fulfillment" if _auto_fulfill() else "Shopify fulfillment (off)",
                             "ok": fulfill_err is None, "error": fulfill_err},
                            {"key": "order_auto", "label": "Auto label + draft" + ("" if auto["enabled"]["label"] else " (label off)"),
                             "ok": auto_err is None, "error": auto_err},
                            {"key": "shopify_scopes", "label": "Shopify scopes", "ok": bool(scopes and scopes["ok"]),
                             "error": scopes_err or (f"missing: {', '.join(scopes['missing'])}" if scopes and scopes["missing"] else None)}],
            "traffic": traffic,  # None = could not be read: the site keeps what it has and says so, never shows zero
            "visitors": _visitors(traffic, now),  # the Overview tile: today and last 7 days; None = unreadable, never zero
            # None = could not be read (the site keeps the last known state and says when it was checked)
            "shopify_scopes": None if scopes is None else {**scopes, "checked_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds")},
            "fulfilled_now": fulfilled_now,
            "order_auto": auto,  # order numbers and field names only: what got a label / draft this round and what is held
            "billing": billing,  # the Payments tab; None = Billingo did not answer, the site keeps what it has
            # the last days' orders, plus any older one (up to OPEN_DAYS) that is still open; a closed older order keeps its last state on the site
            "orders": None if every is None else [r for o in every for r in [_order(o, invoices, uploaded, shipping, fulfilled)]
                                                  if _in_window(o, recent_since) or not _closed(r)],
            "products": None if prods is None else [v for p in prods for v in _variants(p)]}

def push(send="no", days=DAYS, shop=None, bill=None, gls=None):
    payload = build(days, shop, bill, gls=gls, act=send == "yes")
    count = {k: None if payload[k] is None else len(payload[k]) for k in ("orders", "products")}
    count["traffic_days"] = None if payload["traffic"] is None else len(payload["traffic"])
    count["visitors"] = None if payload["traffic"] is None else sum(t["visitors"] for t in payload["traffic"])
    count["visitors_today"] = None if payload["visitors"] is None else payload["visitors"]["today"]
    count["visitors_7d"] = None if payload["visitors"] is None else payload["visitors"]["last_7_days"]
    count["scopes_missing"] = None if payload["shopify_scopes"] is None else payload["shopify_scopes"]["missing"]
    count["fulfilled_now" if send == "yes" else "would_fulfill"] = payload["fulfilled_now"]
    a = payload["order_auto"]
    count["order_auto"] = {"enabled": a["enabled"], ("labels" if send == "yes" else "would_label"): a["labels"],
                           ("drafts" if send == "yes" else "would_draft"): a["drafts"], "held": a["held"]}
    b = payload["billing"]
    count["billing"] = None if b is None else {"bank_accounts": len(b["bank_accounts"]), "invoices": len(b["invoices"]),
                                               "paid": sum(1 for i in b["invoices"] if i["payment_status"] == "paid"),
                                               "overdue": [i["number"] for i in b["invoices"] if i["overdue"]]}
    count["shopify_fulfilled"] = sum(1 for o in payload["orders"] or [] if o["shopify_fulfillment"]["state"] == "fulfilled")
    private = [o["private"] for o in payload["orders"] or []]  # how many orders came with contact data: numbers only
    count.update(with_name=sum(1 for p in private if p["customer_name"]), with_address=sum(1 for p in private if p["shipping_address"]),
                 cod=sum(1 for o in payload["orders"] or [] if o["is_cod"]), paid=sum(1 for o in payload["orders"] or [] if o["paid"] == "paid"),
                 gls_tracked=sum(1 for o in payload["orders"] or [] if o.get("shipping")),
                 delivered=sum(1 for o in payload["orders"] or [] if (o.get("shipping") or {}).get("state") == "delivered"),
                 cod_invoice_due=[o["name"] for o in payload["orders"] or [] if o.get("cod_invoice_due")],
                 missing_data={o["name"]: [f"{p['step']}: {p['label']}" for p in o["checks"] if p["level"] == "stop"]
                               for o in payload["orders"] or [] if any(p["level"] == "stop" for p in o["checks"])})
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
        try:
            out = push("yes"); note = "; ".join(out["errors"]) or "ok"
            if out.get("fulfilled_now"): print(f"ecom sync: fulfilled in Shopify (GLS picked up, no customer e-mail): {', '.join(out['fulfilled_now'])}", flush=True)
            a = out.get("order_auto") or {}
            if a.get("labels"): print(f"ecom sync: MyGLS label uploaded: {', '.join(a['labels'])}", flush=True)
            if a.get("drafts"): print(f"ecom sync: Billingo invoice draft made: {', '.join(a['drafts'])}", flush=True)
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
