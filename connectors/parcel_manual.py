"""A parcel for an order that is NOT in Shopify (e.g. eMAG), uploaded to our own MyGLS. Temporary: until it is in the repo (PR).
Dry run by default; --send yes uploads (prepared, not printed). The recipient data only passes through: output shows the reference,
the COD amount, the label note and warnings. One reference, one parcel (brain/gls_uploaded.json, key "<channel>-<ref>").
Label note: the same template as Shopify orders (config/business.yaml labels.*), with the channel's reference as the order:
"<shop> <ref>, <goods>, COD <amount> Ft" or "..., PAID".
CLI: python3 connectors/parcel_manual.py prepare --channel eMAG --ref 1000000001 --goods "1× Wooden puzzle" --cod 9890
     --name … --zip … --city … --street … --house … --phone … --email … [--send yes]
A barter / influencer / gift / test parcel: --cod 0 --kind barter, and the note ends with BARTER instead of PAID."""
import sys, json, pathlib, importlib.util, datetime as dt
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cbase import ConnectorError, run_cli, setting
import business

def _flow():
    spec = importlib.util.spec_from_file_location("pm_flow", HERE / "order_flow.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

KINDS = ("barter", "influencer", "gift", "test")  # not-revenue parcels: no COD, the kind stands where PAID would

def note(ref, goods, cod, kind=""):
    out = setting(business.label_note, str(ref), goods, cod)
    if kind:
        paid = setting(business.labels)["paid"]
        if not out.endswith(paid): raise ConnectorError(f"[label note] the payment part '{paid}' is not at the end of the template: cannot put {kind.upper()} there")
        out = out[:len(out) - len(paid)] + kind.upper()
    return out

def prepare(channel, ref, goods, cod, name, zip, city, street, house, phone, email="", info="", send="no", kind=""):
    kind = kind.strip().lower()
    if kind and kind not in KINDS: raise ConnectorError(f"--kind must be one of {', '.join(KINDS)}")
    if kind and float(cod or 0): raise ConnectorError(f"a {kind} parcel has no cash on delivery: --cod must be 0")
    flow = _flow(); gls = flow._load("gls")
    if not goods.strip(): raise ConnectorError("what is in the parcel (--goods) is missing: it goes on the label note")
    cod = float(cod or 0)
    missing = [k for k, v in (("name", name), ("zip", zip), ("city", city), ("street", street), ("house", house), ("phone", phone)) if not str(v).strip()]
    if missing: raise ConnectorError(f"{channel} {ref}: missing {', '.join(missing)}")
    key = f"{channel}-{ref}"
    done = flow._uploaded().get(key)
    if done: return {"order": key, "skipped": "already uploaded to MyGLS", "parcel_id": done}
    o = {"name": key, "shipping": {"name": name, "address1": f"{street} {house}", "zip": str(zip), "city": city, "country_code": "HU"},
         "billing": {}, "gateways": ["cash on delivery"] if cod else ["paid"], "lines": [], "shipping_lines": [],
         "phone": str(phone), "email": email}
    parcel = flow.gls_parcel(o, gls, house=str(house), info=info)
    parcel.update({"ClientReference": f"{channel} {ref}", "CODAmount": cod, "CODReference": str(ref) if cod else "",
                   "Content": note(ref, goods, cod, kind)})
    if send != "yes":
        return {"order": key, "dry_run": True, "cod_amount": cod, "content": parcel["Content"], "next": "run again with --send yes"}
    out = flow.step("upload to MyGLS", gls.prepare_labels, [parcel])
    rec = {"parcel_id": out[0]["parcel_id"] if out else None, "cod_amount": cod, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           # for the site's packing queue (ecom_sync): kind and contents only, never the address
           "manual": {"kind": kind or ("emag" if channel.lower() == "emag" else "barter"), "items": goods.strip()}}
    flow.UPLOADED.write_text(json.dumps({**flow._uploaded(), key: rec}), encoding="utf-8")
    return {"order": key, "uploaded": out, "cod_amount": cod, "content": parcel["Content"]}

OPS = {"prepare": prepare}
if __name__ == "__main__": sys.exit(run_cli(OPS))
