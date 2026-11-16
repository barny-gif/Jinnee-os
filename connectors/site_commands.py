"""Commands the owner gives on the site, carried out on this machine. Only one kind for now: the exchange label.

The site writes a row to ecom_commands; this reads the open rows (GET /api/ecom/commands), carries out the ones it knows and
writes the result back (POST, the row is consumed then). A command it does not know is left open and untouched.

Exchange label: action "label" with params {"type": "exchange", "send_items": "...", "back_items": "...", "again": ""} and the
Shopify order id in order_id. It runs connectors/exchange.py (carrier XS service, no COD, refuses without a house number),
uploaded at once: the owner asked for it on the site, and a parcel label upload is the team's level-2 action (report after).
OFF unless ECOM_SITE_COMMANDS=on. In a scheduled run (JINNEE_UNATTENDED=1) nothing is carried out.
The result written back holds the order name and parcel ids or the refusal reason: never the customer's name or address.
CLI: python3 connectors/site_commands.py run            (dry: lists what would run)
     python3 connectors/site_commands.py run --send yes"""
import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import requests
from cbase import ConnectorError, need, run_cli, unattended

SWITCH = "ECOM_SITE_COMMANDS"


def _on():
    return os.getenv(SWITCH, "").strip().lower() in ("on", "yes", "1", "true")


def _site():
    site, secret = need("ECOM_SITE_URL", "ECOM_INGEST_SECRET")
    if not site.lower().startswith("https://"): raise ConnectorError("[site commands] ECOM_SITE_URL must start with https://")
    return site.rstrip("/") + "/api/ecom/commands", {"x-ecom-secret": secret}


def fetch():
    url, headers = _site()
    try: r = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException as e: raise ConnectorError(f"[site commands] the site did not answer: {type(e).__name__}")
    if r.status_code == 401: raise ConnectorError("[site commands] the site refused the secret (401)")
    if not r.ok: raise ConnectorError(f"[site commands] the site answered {r.status_code}")
    try: return (r.json() or {}).get("commands") or []
    except ValueError: raise ConnectorError("[site commands] the answer was not JSON")


def report(results):
    if not results: return 0
    url, headers = _site()
    try: r = requests.post(url, json={"results": results}, headers=headers, timeout=20)
    except requests.RequestException as e: raise ConnectorError(f"[site commands] writing the results back failed: {type(e).__name__}")
    if not r.ok: raise ConnectorError(f"[site commands] writing the results back: the site answered {r.status_code}")
    try: return (r.json() or {}).get("consumed", 0)
    except ValueError: return 0


def is_exchange(c):
    p = c.get("params") if isinstance(c, dict) else None
    return c.get("action") == "label" and isinstance(p, dict) and p.get("type") == "exchange"


def _exchange(c, prepare):
    p = c["params"]
    out = prepare(str(c.get("order_id") or ""), str(p.get("send_items") or ""), str(p.get("back_items") or ""),
                  send="yes", again=str(p.get("again") or ""))
    if out.get("skipped"): return True, f"{out.get('order', '')}: {out['skipped']}"
    ids = ", ".join(str(u.get("parcel_id")) for u in out.get("uploaded") or [])
    warn = "; ".join(out.get("warnings") or [])
    return True, f"{out.get('order', '')}: exchange label uploaded ({ids})" + (f"; warning: {warn}" if warn else "")


def run(send="no", prepare=None):
    """Carries out the open exchange commands. send=no: only lists them. Other commands are left open."""
    todo = [c for c in fetch() if is_exchange(c)]
    if send != "yes" or not _on() or unattended():
        why = ("dry run" if send != "yes" else f"{SWITCH} is off" if not _on() else "scheduled run: nothing is carried out")
        return {"would_run": [{"id": c.get("id"), "order": c.get("order_name")} for c in todo], "note": why}
    if prepare is None:
        import exchange
        prepare = exchange.prepare
    results = []
    for c in todo:
        try: ok, msg = _exchange(c, prepare)
        except ConnectorError as e: ok, msg = False, str(e)
        except Exception as e: ok, msg = False, f"[exchange] failed: {type(e).__name__}"
        results.append({"id": c.get("id"), "ok": ok, "result": msg[:500]})
    return {"done": results, "consumed": report(results)}


OPS = {"run": run}

if __name__ == "__main__":
    sys.exit(run_cli(OPS))
