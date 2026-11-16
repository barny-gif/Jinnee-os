"""marveen.io community agent API, READ ONLY. Feed, search, highlights, one post, mentions. No write endpoint exists here.
Auth: MARVEEN_IO_API_KEY (header Authorization: Bearer mrv_...). Docs: https://api.marveen.io/agent/v1/docs
CLI: python3 connectors/marveen/ops.py search --q webshop
The first successful call may carry a one-time "attest_grant" (a skill-uploader key). It is a secret and we do not upload
skills, so it is dropped and never printed; only the fact that one was offered is reported (a new one can be requested)."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import requests
from cbase import ConnectorError, need, run_cli

BASE = "https://api.marveen.io/agent/v1"

def _get(path, params=None):
    (key,) = need("MARVEEN_IO_API_KEY")
    try:
        r = requests.get(BASE + path, params={k: v for k, v in (params or {}).items() if v not in (None, "")},
                         timeout=20, headers={"Authorization": "Bearer " + key})
    except requests.RequestException as e:
        raise ConnectorError(f"marveen.io did not answer: {type(e).__name__}")
    if r.status_code in (401, 403): raise ConnectorError("marveen.io: the API key was refused - check MARVEEN_IO_API_KEY")
    if r.status_code == 429: raise ConnectorError(f"marveen.io: rate limit (429), retry after {r.headers.get('Retry-After', '?')} s")
    if not r.ok: raise ConnectorError(f"marveen.io answered {r.status_code}: {r.text[:200]}")
    try: data = r.json()
    except ValueError: raise ConnectorError("marveen.io: the answer was not JSON")
    if isinstance(data, dict) and "attest_grant" in data:
        grant = data.pop("attest_grant") or {}
        data["attest_key_offered"] = bool(isinstance(grant, dict) and grant.get("attest_key"))
    return data

def _limit(limit):
    n = int(limit)
    if not 1 <= n <= 50: raise ConnectorError("limit must be 1-50")
    return n

def feed(limit="20", before=""):
    return _get("/feed", {"limit": _limit(limit), "before": before})

def search(q, limit="20", before=""):
    q = (q or "").strip()
    if not 1 <= len(q) <= 200: raise ConnectorError("q must be 1-200 characters")
    return _get("/search", {"q": q, "limit": _limit(limit), "before": before})

def highlights():
    return _get("/feed-highlights")

def post(id, comments_after=""):
    if not str(id).strip(): raise ConnectorError("id is required")
    return _get(f"/feed/posts/{str(id).strip()}", {"comments_after": comments_after})

def mentions():
    return _get("/mentions")

OPS = {"feed": feed, "search": search, "highlights": highlights, "post": post, "mentions": mentions}

if __name__ == "__main__":
    sys.exit(run_cli(OPS))
