"""MyGLS (Hungary) connector. prepare_labels UPLOADS parcel data to MyGLS without printing, so the label is only a print click away.
Auth: GLS_CLIENT_NUMBER, GLS_USERNAME, GLS_PASSWORD (the password is sent as a SHA-512 hash, as the MyGLS API requires).
GLS_API_BASE defaults to https://api.mygls.hu (test system: https://api.test.mygls.hu, with its own test login).
CLI: python3 connectors/gls/ops.py track --parcel_number 123456789
     python3 connectors/gls/ops.py shipping          (read only: GLS state of every uploaded order, order numbers only)"""
import os, sys, re, json, time, hashlib, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
UPLOADED = pathlib.Path(__file__).resolve().parent.parent.parent / "brain" / "gls_uploaded.json"
import requests
from cbase import ConnectorError, need, run_cli

def _base():
    return os.getenv("GLS_API_BASE", "https://api.mygls.hu").rstrip("/") + "/ParcelService.svc/json/"

def _auth():
    _, user, pw = need("GLS_CLIENT_NUMBER", "GLS_USERNAME", "GLS_PASSWORD")
    return {"Username": user, "Password": list(hashlib.sha512(pw.encode("utf-8")).digest())}

def _post(method, body):
    try:
        r = requests.post(_base() + method, json={**_auth(), **body}, timeout=30)
    except requests.RequestException as e:
        raise ConnectorError(f"MyGLS did not answer: {type(e).__name__}")
    if r.status_code in (401, 403): raise ConnectorError("MyGLS refused the login - check GLS_USERNAME / GLS_PASSWORD")
    if not r.ok: raise ConnectorError(f"MyGLS answered {r.status_code}: {r.text[:200]}")
    return r.json()

def client_number():
    (n,) = need("GLS_CLIENT_NUMBER"); return int(n)

def prepare_labels(parcels):
    """parcels: list of MyGLS 'Parcel' dicts (built by order_flow). → [{"parcel_id", "reference"}]; errors raise with GLS's own words."""
    data = _post("PrepareLabels", {"WebshopEngine": os.getenv("GLS_WEBSHOP_ENGINE", "Shopify"), "ParcelList": parcels})
    errs = data.get("PrepareLabelsError") or []
    if errs: raise ConnectorError("MyGLS: " + "; ".join(f"{e.get('ErrorCode')}: {e.get('ErrorDescription')}" for e in errs))
    return [{"parcel_id": p.get("ParcelId"), "reference": p.get("ClientReference")} for p in data.get("ParcelInfoList") or []]

def delete_labels(parcel_ids):
    """Deletes PREPARED parcels (not yet printed). Used to correct an upload: delete, then prepare again."""
    data = _post("DeleteLabels", {"ParcelIdList": [int(i) for i in parcel_ids]})
    errs = data.get("DeleteLabelsErrorList") or []
    if errs: raise ConnectorError("MyGLS: " + "; ".join(f"{e.get('ErrorCode')}: {e.get('ErrorDescription')}" for e in errs))
    return True

def _date(v):
    """MyGLS dates are .NET JSON dates ('/Date(1791963038000+0200)/'): the number is UTC milliseconds. → ISO 8601 UTC, or None."""
    m = re.search(r"/Date\((-?\d+)", v or "")
    return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, dt.timezone.utc).isoformat(timespec="seconds") if m else None

def _json_date(ts):
    return f"/Date({int(ts * 1000)})/"

def statuses(parcel_number):
    """Newest first, as MyGLS gives it. Status text in Hungarian; the code is passed on as GLS sends it."""
    data = _post("GetParcelStatuses", {"ParcelNumber": int(parcel_number), "ReturnPOD": False, "LanguageIsoCode": "HU"})
    errs = data.get("GetParcelStatusErrors") or []
    if errs: raise ConnectorError("[GLS status] MyGLS: " + "; ".join(f"{e.get('ErrorCode')}: {e.get('ErrorDescription')}" for e in errs))
    return [{"date": s.get("StatusDate"), "at": _date(s.get("StatusDate")), "code": None if s.get("StatusCode") is None else str(s.get("StatusCode")),
             "status": s.get("StatusDescription"), "depot": s.get("DepotCity")} for s in data.get("ParcelStatusList") or []]

def track(parcel_number):
    return [{k: s[k] for k in ("date", "status", "depot")} for s in statuses(parcel_number)]

def parcel_numbers(days="30", now=None):
    """MyGLS parcel id (what PrepareLabels gave) -> parcel (tracking) number, for labels printed in the last `days` days.
    GetParcelList by print date (field names as in webapix/mygls-sdk; confirmed on the live account 2026-10-15). A label that is
    uploaded but not printed has no parcel number yet. Only ids and numbers leave this function: the 'Parcel' block holds the recipient."""
    now = now or time.time()
    data = _post("GetParcelList", {"PrintDateFrom": _json_date(now - int(days) * 86400), "PrintDateTo": _json_date(now)})
    errs = data.get("GetParcelListErrors") or []
    if errs: raise ConnectorError("[GLS parcel list] MyGLS: " + "; ".join(f"{e.get('ErrorCode')}: {e.get('ErrorDescription')}" for e in errs))
    return {str(i["ParcelId"]): str(i["ParcelNumber"]) for i in data.get("PrintDataInfoList") or [] if i.get("ParcelId") and i.get("ParcelNumber")}

# Status text (HU, lower case) -> simple state. Checked in this order; the newest status decides. Seen on the live account:
# Csomag rögzítve, Utánvét adat rögzítve, Lehívás - felvett csomag, Depó tranzit, GLS központban regisztrálva,
# Csomag a kiszállító depóban, Rollkarte check, Meghat. időpontra történő kisz, Csomag kiszállítás alatt, Kiszállítva, Depó tárolás.
STATES = (("returned", ("visszaküld", "feladónak", "visszaszállít")),
          ("delivered", ("kiszállítva", "kézbesítve", "átvette")),
          ("out_for_delivery", ("kiszállítás alatt",)),
          ("problem", ("tárolás", "sikertelen", "nem kézbesít", "sérül", "megtagad", "elutasít", "hibás cím", "nem található")),
          ("uploaded", ("rögzítve",)))

def classify(items):
    """items: statuses() output (newest first). → state, latest status, when GLS first had the parcel in hand, when it was delivered."""
    if not items: return {"state": "uploaded", "code": None, "text": None, "status_at": None, "handed_at": None, "delivered_at": None}
    def state(s):
        t = (s.get("status") or "").lower()
        return next((st for st, words in STATES if any(w in t for w in words)), "in_transit")
    last = items[0]; st = state(last)
    physical = [s for s in items if state(s) != "uploaded" and s.get("at")]
    if st == "in_transit" and len(physical) == 1: st = "handed_to_gls"  # the first scan: GLS has just picked it up
    return {"state": st, "code": last.get("code"), "text": last.get("status"), "status_at": last.get("at"),
            "handed_at": min((s["at"] for s in physical), default=None),
            "delivered_at": last.get("at") if st == "delivered" else None}

def shipping(days="30"):
    """Read-only: every order in brain/gls_uploaded.json with its parcel number and GLS state. Order numbers only."""
    try: uploaded = json.loads(UPLOADED.read_text(encoding="utf-8"))
    except (OSError, ValueError): uploaded = {}
    numbers = parcel_numbers(days); out = []
    for name, entry in uploaded.items():
        pid = entry.get("parcel_id") if isinstance(entry, dict) else entry
        number = numbers.get(str(pid))
        out.append({"order": name, "parcel_number": number, **(classify(statuses(number)) if number else classify([]))})
    return out

OPS = {"track": track, "shipping": shipping}
if __name__ == "__main__": sys.exit(run_cli(OPS))
