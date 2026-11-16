"""Exchange parcel for an existing Shopify order: the right item goes out with the carrier's exchange service (GLS "XS": the
courier hands over the new parcel and takes the wrong item back at the same visit), no cash on delivery. The carrier answers
with two parcel ids (out and back); both are kept in brain/gls_uploaded.json as "Csere-<order>" and "Csere-<order>-vissza",
kind "exchange", so the tracking and the site's packing list follow them like any other parcel.
Dry run by default; send=yes uploads (prepared, not printed; upload_parcel_label). One exchange per order unless again=yes.
Customer data only passes through (the order's own shipping address and the first valid phone); the output shows the order
number, the label note and the parcel ids. Tell the customer beforehand: the parcel must not be opened before the courier
has taken the returned item (lessons).
CLI: python3 connectors/exchange.py prepare --order_id <Shopify id> --send_items "1× Puzzle" --back_items "1× Puzzle" [--send yes]"""
import sys, json, pathlib, importlib.util, datetime as dt
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cbase import ConnectorError, run_cli
import parcel_manual

SERVICE = "XS"

def _flow():
    spec = importlib.util.spec_from_file_location("ex_flow", HERE / "order_flow.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def _key(order_name, again=""):
    ref = str(order_name).lstrip("#")
    return f"Csere-{ref}" + (f"-{again}" if again else "")

def prepare(order_id, send_items, back_items="", send="no", again="", shop=None, gls=None):
    if not str(order_id).strip().isdigit(): raise ConnectorError("[exchange] --order_id: the Shopify order id (digits)")
    if not send_items.strip(): raise ConnectorError("[exchange] --send_items: what goes out (e.g. '1× Puzzle')")
    flow = _flow()
    shop = shop or flow._load("shopify"); gls = gls or flow._load("gls")
    o = shop.order_private(order_id)
    key = _key(o["name"], again.strip())
    uploaded = flow._uploaded()
    if key in uploaded: return {"order": o["name"], "skipped": f"already uploaded as {key} (again=<n> makes a second exchange)"}
    sh = o["shipping"]
    phone = flow.best_phone([o.get("phone"), sh.get("phone"), (o.get("billing") or {}).get("phone")], sh.get("country_code") or "HU")
    ref = o["name"].lstrip("#")
    goods = send_items.strip() + (f", csere: {back_items.strip()} vissza" if back_items.strip() else ", csere")
    parcel = flow.gls_parcel({**o, "name": key, "gateways": ["paid"], "phone": phone}, gls)
    parcel.update({"ClientReference": f"Csere {ref}", "CODAmount": 0, "CODReference": "",
                   "Content": parcel_manual.note(ref, goods, 0), "ServiceList": [{"Code": SERVICE}]})
    d = parcel["DeliveryAddress"]
    warn = [w for w, bad in (("house number not found in the address", not d["HouseNumber"]),
                             ("no valid phone number: the courier cannot call", not flow.valid_phone(phone))) if bad]
    out = {"order": o["name"], "key": key, "content": parcel["Content"], "service": SERVICE, "warnings": warn}
    if send != "yes": return {**out, "dry_run": True, "next": "send=yes uploads it (prepared, not printed)"}
    if not d["HouseNumber"]: raise ConnectorError(f"[exchange] {o['name']}: no house number in the shipping address: nothing uploaded")
    ids = flow.step("upload to MyGLS", gls.prepare_labels, [parcel])
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    recs = {key: {"parcel_id": ids[0]["parcel_id"] if ids else None, "cod_amount": 0, "at": at, "services": [SERVICE],
                  "manual": {"kind": "exchange", "items": goods}}}
    if len(ids) > 1:  # the carrier creates the return parcel too
        recs[key + "-vissza"] = {"parcel_id": ids[1]["parcel_id"], "cod_amount": 0, "at": at, "services": [SERVICE],
                                 "manual": {"kind": "exchange", "items": (back_items.strip() or "csere") + " vissza"}}
    flow.UPLOADED.write_text(json.dumps({**flow._uploaded(), **recs}, ensure_ascii=False), encoding="utf-8")
    return {**out, "uploaded": [{"key": k, "parcel_id": v["parcel_id"]} for k, v in recs.items()]}

OPS = {"prepare": prepare}
if __name__ == "__main__": sys.exit(run_cli(OPS))
