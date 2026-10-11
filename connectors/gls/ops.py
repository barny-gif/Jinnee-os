"""MyGLS (Hungary) connector. prepare_labels UPLOADS parcel data to MyGLS without printing, so the label is only a print click away.
Auth: GLS_CLIENT_NUMBER, GLS_USERNAME, GLS_PASSWORD (the password is sent as a SHA-512 hash, as the MyGLS API requires).
GLS_API_BASE defaults to https://api.mygls.hu (test system: https://api.test.mygls.hu, with its own test login).
CLI: python3 connectors/gls/ops.py track --parcel_number 123456789"""
import os, sys, hashlib, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
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

def track(parcel_number):
    data = _post("GetParcelStatuses", {"ParcelNumber": int(parcel_number), "ReturnPOD": False, "LanguageIsoCode": "HU"})
    errs = data.get("GetParcelStatusErrors") or []
    if errs: raise ConnectorError("MyGLS: " + "; ".join(f"{e.get('ErrorCode')}: {e.get('ErrorDescription')}" for e in errs))
    return [{"date": s.get("StatusDate"), "status": s.get("StatusDescription"), "depot": s.get("DepotCity")} for s in data.get("ParcelStatusList") or []]

OPS = {"track": track}
if __name__ == "__main__": sys.exit(run_cli(OPS))
