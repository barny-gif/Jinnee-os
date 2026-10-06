"""Jinnee dashboard – one page. Shows the brain/ files; the approval buttons record the owner's decision through core/approvals.py."""
import os, sys, json, pathlib, time, hmac, hashlib, secrets, ipaddress, asyncio
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "core"))
import env; env.load()  # before the settings below are read
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import approvals, autonomy, heartbeat

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
HERE = pathlib.Path(__file__).resolve().parent
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
TRUST_PEER = os.getenv("DASHBOARD_TRUST_PEER", "") == "1"  # Docker: the peer is the bridge, never loopback
NAME = os.getenv("JINNEE_NAME", "Jinnee")
COOKIE, SESSION_TTL = "jinnee_dash", 30 * 86400
SECRET = secrets.token_bytes(32)  # per process: a restart signs everyone out
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "via", "cf-connecting-ip")
DECISIONS = approvals.DECISIONS
CLOSED_SHOWN = 10

app = FastAPI(title="Jinnee dashboard")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

def rd(name, default):
    p = BRAIN / name
    if not p.exists(): return default
    t = p.read_text(encoding="utf-8")
    return json.loads(t) if name.endswith(".json") else t

def same(a, b):
    return hmac.compare_digest(str(a).encode(), str(b).encode())

def sign(exp):
    return hmac.new(SECRET, str(exp).encode(), hashlib.sha256).hexdigest()

def session_ok(token):
    exp, _, sig = (token or "").partition(".")
    return exp.isdigit() and same(sig, sign(exp)) and int(exp) > time.time()

def is_local(req: Request):
    """No password set: only a direct request to localhost passes, never one relayed by a proxy."""
    if any(h in req.headers for h in PROXY_HEADERS): return False
    if urlsplit("//" + req.headers.get("host", "")).hostname not in ("localhost", "127.0.0.1", "::1"): return False
    if TRUST_PEER: return True
    try: ip = ipaddress.ip_address(req.client.host)
    except (ValueError, AttributeError): return False
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback

def key_ok(req: Request):
    return bool(PASSWORD) and same(req.headers.get("x-dash-key", ""), PASSWORD)

def authed(req: Request):
    if not PASSWORD: return is_local(req)
    return key_ok(req) or session_ok(req.cookies.get(COOKIE))

def same_origin(req: Request):
    src = urlsplit(req.headers.get("origin") or req.headers.get("referer") or "").netloc.lower()
    return bool(src) and src in (req.headers.get("host", "").lower(), req.headers.get("x-forwarded-host", "").lower())

def guard(req: Request):
    if not authed(req):
        if PASSWORD: raise HTTPException(401, "Sign in at /login")
        raise HTTPException(403, "No DASHBOARD_PASSWORD set: the dashboard only answers direct requests to localhost")
    if req.method not in ("GET", "HEAD") and not key_ok(req) and not same_origin(req):
        raise HTTPException(403, "Cross-site request refused")

def page(name):
    return (HERE / "static" / name).read_text(encoding="utf-8").replace("{{NAME}}", NAME)

@app.get("/", response_class=HTMLResponse)
def index(req: Request):
    if PASSWORD and not authed(req): return RedirectResponse("/login", 303)  # also drops any old ?key=
    guard(req)
    return page("index.html")

@app.get("/login", response_class=HTMLResponse)
def login_form(req: Request):
    if not PASSWORD or authed(req): return RedirectResponse("/", 303)
    return page("login.html").replace("{{ERROR}}", "")

@app.post("/login")
async def login(req: Request):
    if not PASSWORD: return RedirectResponse("/", 303)
    if not same_origin(req): raise HTTPException(403, "Cross-site request refused")
    sent = parse_qs((await req.body()).decode("utf-8", "replace")).get("password", [""])[0]
    if not same(sent, PASSWORD):
        await asyncio.sleep(1)
        return HTMLResponse(page("login.html").replace("{{ERROR}}", "Wrong password."), 401)
    exp = int(time.time()) + SESSION_TTL
    https = "https" in (req.url.scheme, req.headers.get("x-forwarded-proto", ""))
    r = RedirectResponse("/", 303)
    r.set_cookie(COOKIE, f"{exp}.{sign(exp)}", max_age=SESSION_TTL, httponly=True, samesite="lax", secure=https, path="/")
    return r

@app.post("/logout")
def logout(req: Request):
    if not same_origin(req): raise HTTPException(403, "Cross-site request refused")
    r = RedirectResponse("/login", 303)
    r.delete_cookie(COOKIE, path="/")
    return r

def book():
    return approvals.Book(BRAIN)

@app.get("/api/state")
def state(req: Request):
    guard(req)
    now = int(time.time())
    team = [{"agent": a, "ago_min": (now - v.get("ts", 0)) // 60, "note": v.get("note", ""), "state": v.get("state", "ok")}
            for a, v in heartbeat.load(BRAIN).items()]
    log = [l[2:] for l in rd("decisions.log.md", "").splitlines() if l.startswith("- ")][-30:]
    connectors = []
    for c in sorted((ROOT / "connectors").glob("*.manifest.json")):
        d = json.loads(c.read_text(encoding="utf-8"))
        envs = d.get("auth", {}).get("env", [])
        envs = [envs] if isinstance(envs, str) else envs
        ok = all(os.getenv(e) for e in envs) if envs else None
        connectors.append({"id": d["id"], "name": d["name"], "category": d["category"], "ok": ok})
    bk = book()
    items, problem = bk.read()
    closed = sorted((i for i in items if i["state"] == "consumed"), key=lambda i: i["consumed_at"] or 0, reverse=True)[:CLOSED_SHOWN]
    shown = [{**bk.view(i), "status": bk.status(i)} for i in items if i["state"] != "consumed"] + \
            [{**bk.view(i), "status": bk.status(i), "preview": ""} for i in closed]
    cfg = autonomy.Config(BRAIN)
    table, levels_problem = cfg.read()
    levels = [{"action": a, "label": e["label"], "level": autonomy.effective(e, a), "name": autonomy.NAMES[autonomy.effective(e, a)],
               "locked": e["locked"], "max_level": e["max_level"]} for a, e in sorted(table.items())]
    return {"name": NAME, "auth": bool(PASSWORD), "brief": rd("brief_today.md", ""), "approvals": shown,
            "problems": [p for p in (problem, levels_problem) if p], "autonomy": levels,
            "team": team, "connectors": connectors, "log": log[::-1]}

def refuse(e):
    """The queue said no: 404 unknown item, 409 the item has moved on, 400 the request itself is incomplete."""
    code = 404 if isinstance(e, approvals.Missing) else 409 if isinstance(e, approvals.Conflict) else 400
    raise HTTPException(code, str(e)[:1].upper() + str(e)[1:])

@app.post("/api/approve/{item_id}")
async def approve(item_id: str, req: Request):
    guard(req)
    try: body = await req.json()  # {"decision": "ok"|"edit"|"drop", "note": "..."}; a note is required for edit and drop
    except ValueError: body = None
    if not isinstance(body, dict) or body.get("decision") not in DECISIONS:
        raise HTTPException(400, 'Expected {"decision": "ok"|"edit"|"drop", "note": "..."}')
    try: it = book().decide(item_id, body["decision"], str(body.get("note") or ""), by="dashboard")
    except approvals.Refused as e: refuse(e)
    return JSONResponse({"ok": True, "status": approvals.Book.status(it), "sendable": it["sendable"]})

@app.post("/api/undo/{item_id}")
async def undo(item_id: str, req: Request):
    """Back to pending. Works until the team has picked the decision up; after that the answer is 409."""
    guard(req)
    try: book().undo(item_id, by="dashboard")
    except approvals.Refused as e: refuse(e)
    return JSONResponse({"ok": True})

if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST") or ("0.0.0.0" if PASSWORD else "127.0.0.1")
    uvicorn.run(app, host=host, port=int(os.getenv("DASHBOARD_PORT", "8080")))
