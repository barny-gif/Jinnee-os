"""Upload one file to a Lovable presigned URL (from mcp__claude_ai_Lovable__get_file_upload_url), so an approved build
prompt can carry its attachments. The agent has no curl; this is the one road, and it is narrow:
- only a file inside handoffs/ (what the owner sent or the team prepared), never .env, brain/ or code;
- only an https URL; nothing is printed of the URL beyond its host (presigned URLs carry a signature);
- dry run by default: --send yes uploads. A scheduled run refuses --send yes (cbase.run_cli);
- the URL is signed together with the headers get_file_upload_url returns (x-goog-content-length-range,
  x-goog-meta-user_id): without them storage answers 400. Pass that object as --headers '<json>'; only x-goog-* keys.

python3 connectors/lovable_upload.py put --url "<upload_url>" --headers '<headers json>' --file handoffs/inbox/x.jpg --content-type image/jpeg [--send yes]
"""
import json, pathlib, sys
from urllib.parse import urlparse
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cbase
from cbase import ConnectorError

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOWED_DIR = ROOT / "handoffs"
MAX_BYTES = 20 * 1024 * 1024


def _file(path: str) -> pathlib.Path:
    if not path: raise ConnectorError("[check file] --file is missing")
    p = (ROOT / path).resolve() if not pathlib.Path(path).is_absolute() else pathlib.Path(path).resolve()
    if ALLOWED_DIR.resolve() not in p.parents: raise ConnectorError("[check file] only files inside handoffs/ may be uploaded")
    if not p.is_file(): raise ConnectorError(f"[check file] not found: {p.relative_to(ROOT) if ROOT in p.parents else p.name}")
    if p.stat().st_size > MAX_BYTES: raise ConnectorError("[check file] larger than 20 MB")
    return p


def _host(url: str) -> str:
    u = urlparse(url or "")
    if u.scheme != "https" or not u.netloc: raise ConnectorError("[check url] an https upload URL is required")
    return u.netloc


def _headers(raw) -> dict:
    if not raw: return {}
    try:
        h = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        raise ConnectorError("[check headers] --headers must be the JSON object from get_file_upload_url") from None
    if not isinstance(h, dict): raise ConnectorError("[check headers] --headers must be a JSON object")
    for k, v in h.items():
        if not str(k).lower().startswith("x-goog-"): raise ConnectorError(f"[check headers] only x-goog-* headers allowed, not {k}")
        if not isinstance(v, str) or "\n" in v or "\r" in v: raise ConnectorError(f"[check headers] {k} must be one line of text")
    return h


def put(url="", file="", content_type="application/octet-stream", headers="", send="no", _http=None):
    p, host, extra = _file(file), _host(url), _headers(headers)
    out = {"file": str(p.relative_to(ROOT)), "bytes": p.stat().st_size, "host": host, "headers": sorted(extra)}
    if str(send).strip().lower() != "yes":
        return {**out, "dry_run": True}
    if _http is None:
        import requests
        _http = requests
    try:
        r = _http.put(url, data=p.read_bytes(), headers={**extra, "Content-Type": content_type}, timeout=120)
    except Exception as e:
        raise ConnectorError(f"[upload] {host} did not answer: {type(e).__name__}") from None
    if not 200 <= r.status_code < 300:
        raise ConnectorError(f"[upload] {host} answered {r.status_code}")
    return {**out, "uploaded": True}


OPS = {"put": put}

if __name__ == "__main__":
    sys.exit(cbase.run_cli(OPS))
