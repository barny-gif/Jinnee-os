"""Jinnee dashboard – one page. Shows the brain/ files; the approval buttons record the owner's decision through core/approvals.py."""
import os, re, sys, json, html, pathlib, time, hmac, hashlib, secrets, ipaddress, asyncio
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "core"))
import env; env.load()  # before the settings below are read
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import approvals, autonomy, heartbeat, keybox, logs, notices, scheduler, texts, vault
import orders

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
        if PASSWORD: raise HTTPException(401, texts.t("dash.err_sign_in"))
        raise HTTPException(403, texts.t("dash.err_local_only"))
    if req.method not in ("GET", "HEAD") and not key_ok(req) and not same_origin(req):
        raise HTTPException(403, texts.t("dash.err_cross_site"))

# The page's words, in the owner's language, are put into the HTML here, when it is served: {{t:key}} becomes the text
# (HTML-escaped) and {{TEXTS}} the dash.* texts as JSON for the script. Why not a separate request from the browser: one
# request, no English flash before the words arrive, and nothing extra that can fail on its own. A language change in
# config/business.yaml shows at the next page load.
SLOT = re.compile(r"\{\{t:([a-z0-9_.]+)\}\}")

def page(name):
    words = texts.bundle("dash.")
    out = (HERE / "static" / name).read_text(encoding="utf-8").replace("{{NAME}}", html.escape(NAME))
    out = SLOT.sub(lambda m: html.escape(words.get("dash." + m.group(1), m.group(1))), out)
    return out.replace("{{TEXTS}}", json.dumps(words, ensure_ascii=False).replace("</", "<\\/"))

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
    if not same_origin(req): raise HTTPException(403, texts.t("dash.err_cross_site"))
    sent = parse_qs((await req.body()).decode("utf-8", "replace")).get("password", [""])[0]
    if not same(sent, PASSWORD):
        await asyncio.sleep(1)
        return HTMLResponse(page("login.html").replace("{{ERROR}}", html.escape(texts.t("dash.wrong_password"))), 401)
    exp = int(time.time()) + SESSION_TTL
    https = "https" in (req.url.scheme, req.headers.get("x-forwarded-proto", ""))
    r = RedirectResponse("/", 303)
    r.set_cookie(COOKIE, f"{exp}.{sign(exp)}", max_age=SESSION_TTL, httponly=True, samesite="lax", secure=https, path="/")
    return r

@app.post("/logout")
def logout(req: Request):
    if not same_origin(req): raise HTTPException(403, texts.t("dash.err_cross_site"))
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
        groups = d.get("auth", {}).get("env_any")  # alternatives: any one complete group is enough
        if groups: ok = bool(ok is not False) and any(all(os.getenv(e) for e in g) for g in groups)
        connectors.append({"id": d["id"], "name": d["name"], "category": d["category"], "ok": ok})
    bk = book()
    items, problem = bk.read()
    closed = sorted((i for i in items if i["state"] == "consumed"), key=lambda i: i["consumed_at"] or 0, reverse=True)[:CLOSED_SHOWN]
    shown = [{**bk.view(i), "status": bk.status(i)} for i in items if i["state"] != "consumed"] + \
            [{**bk.view(i), "status": bk.status(i), "preview": ""} for i in closed]
    cfg = autonomy.Config(BRAIN)
    table, levels_problem = cfg.read()
    levels = [{"action": a, "label": autonomy.label(a, e), "level": autonomy.effective(e, a), "name": autonomy.name(autonomy.effective(e, a)),
               "max_name": autonomy.name(e["max_level"]), "locked": e["locked"], "max_level": e["max_level"]} for a, e in sorted(table.items())]
    try: schedule = [{k: r[k] for k in ("id", "name", "when", "on", "off", "next_text", "last_text", "result", "result_text", "note")}
                     for r in scheduler.Scheduler(BRAIN).rows(now)]
    except Exception: schedule = None  # the box says it could not be read; the rest of the page still works
    return {"name": NAME, "auth": bool(PASSWORD), "brief": rd("brief_today.md", ""), "approvals": shown,
            "problems": [p for p in (problem, levels_problem) if p], "autonomy": levels,
            "team": team, "connectors": connectors, "schedule": schedule, "log": log[::-1],
            # the order chain's plain news (core/notices.py): order numbers, payment type, amounts and states only
            "news": [{"text": n["text"], "at": n["at"]} for n in notices.recent(BRAIN, 20)]}

@app.get("/api/orders")
def api_orders(req: Request):
    guard(req)
    return orders.get()

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
        raise HTTPException(400, texts.t("dash.err_bad_body"))
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

# --- Keys: the owner pastes an API key here instead of editing .env. It goes straight into the encrypted store
# (core/vault.py). No value is ever sent back to the browser, not even a part of one: only names, labels, dates, and where
# each one is. core/keybox.py has the service list, the name rules, the local key log and the "Try it" check.
KEY_WRITES = keybox.Limiter(10, 60)   # add, replace, delete, import, try: per client, per minute

def peer_trusted(req: Request):
    """Is the direct peer our own reverse proxy (Caddy on this machine, or the Docker bridge with the port on 127.0.0.1)?"""
    if TRUST_PEER: return True
    try: ip = ipaddress.ip_address(req.client.host)
    except (ValueError, AttributeError): return False
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback

def client_ip(req: Request):
    """For the key log: the browser's address. Behind our own proxy that is the last X-Forwarded-For hop Caddy added."""
    peer = getattr(req.client, "host", "") or "?"
    hops = [h.strip() for h in req.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    if hops and peer_trusted(req):
        try: return str(ipaddress.ip_address(hops[-1]))
        except ValueError: pass
    return peer

def key_transport(req: Request):
    """'' when a key may travel to this page, else why not. Fine: a direct request to localhost (the machine itself, or an
    SSH tunnel), or HTTPS, either here or ended by our own proxy (Caddy sends X-Forwarded-Proto: https, see Caddyfile).
    Plain http over the network: refused, the key would cross the wire readable."""
    if is_local(req): return ""
    if req.url.scheme == "https": return ""
    proto = req.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if proto == "https" and peer_trusted(req): return ""
    return texts.t("dash.key_insecure")

def key_write(req: Request):
    """Every key write: signed in and same-site (guard), over a safe line, and within the limit."""
    guard(req)
    why = key_transport(req)
    if why: raise HTTPException(403, why)
    if not KEY_WRITES.allow(client_ip(req)): raise HTTPException(429, texts.t("dash.key_rate"))

def known_keys():
    """{name: connector name} for every secret a connector or the core can use, from the manifests."""
    return {k["name"]: s["name"] for s in keybox.catalog(ROOT) for k in s["keys"]}

async def body_of(req: Request):
    try: body = await req.json()
    except ValueError: body = None
    return body if isinstance(body, dict) else {}

@app.get("/api/keys")
def keys(req: Request):
    guard(req)
    try: stored, err = vault.names(), ""
    except vault.VaultError as e: stored, err = [], str(e)
    plain = sorted(vault.plain_secrets())
    try: found = vault.doctor(ROOT, check_env=False)
    except Exception as e: found = [("problem", type(e).__name__)]
    cat = keybox.catalog(ROOT)
    info = {k["name"]: (s, k) for s in cat for k in s["keys"]}
    hist, dates = keybox.history(), keybox.stored_dates()
    rows = []
    for n in sorted(set(info) | set(stored) | set(plain)):
        where = "plain" if n in plain else "stored" if n in stored else "unset"
        sid, sname = keybox.owner_of(n, cat)
        h = hist.get(n, {})
        last = h if h and h.get("action") != "delete" else {}
        own = h.get("label") or ""
        lab = info[n][1]["label"] if n in info else (keybox.label(n, sid) if sid else own or n)
        rows.append({"name": n, "used_by": sname, "service": sid or keybox.OWN_ID, "where": where,
                     "label": lab, "check": bool(n in info and info[n][1]["check"]),
                     "at": (last.get("at") or dates.get(n, "")) if where == "stored" else "",
                     "action": last.get("action", "") if where == "stored" else ""})
    why = key_transport(req)
    return {"keys": rows, "plain": plain, "error": err, "services": cat,
            "secure": not why, "insecure": why,
            "problems": [m for lv, m in found if lv == "problem"], "notes": [m for lv, m in found if lv == "note"]}

@app.get("/api/keys/name")
def key_name(req: Request, label: str = ""):
    """What a typed name ("Saját CRM api") is stored as, before anything is stored. Reads nothing secret."""
    guard(req)
    try: name = keybox.tech_name(label[:keybox.NAME_MAX * 2])
    except ValueError: raise HTTPException(400, texts.t("dash.key_bad_label"))
    try: exists = name in vault.names()
    except vault.VaultError: exists = False
    return {"name": name, "exists": exists, "used_by": keybox.owner_of(name, keybox.catalog(ROOT))[1]}

@app.post("/api/keys")
async def key_set(req: Request):
    """{"name": "SHOPIFY_ADMIN_TOKEN", "value": "..."} for a known key, or {"label": "Saját CRM api", "value": "..."} for one
    of the owner's own: then the server makes the name (keybox.tech_name) and says it in the answer."""
    key_write(req)
    body = await body_of(req)
    value = str(body.get("value") or "").strip()
    own = str(body.get("label") or "").strip()[:keybox.NAME_MAX]
    if own and not body.get("name"):
        try: name = keybox.tech_name(own)
        except ValueError: raise HTTPException(400, texts.t("dash.key_bad_label"))
    else:
        name, own = str(body.get("name") or "").strip(), ""
    if not vault.NAME_OK.fullmatch(name) or not value:
        raise HTTPException(400, texts.t("dash.key_bad"))
    try: replaced = vault.put(name, value)
    except vault.VaultError as e: raise HTTPException(409, str(e))
    os.environ[name] = value  # this process; the team's runs read the store when they start
    keybox.record(name, "replace" if replaced else "add", client_ip(req), own)
    return JSONResponse({"ok": True, "replaced": replaced, "name": name})

@app.delete("/api/keys/{name}")
def key_delete(name: str, req: Request):
    """Takes one key out of the store. The browser asks the owner first. A key still in .env is not touched here."""
    key_write(req)
    if not vault.NAME_OK.fullmatch(name): raise HTTPException(400, texts.t("dash.key_bad"))
    try: gone = vault.delete(name)
    except vault.VaultError as e: raise HTTPException(409, str(e))
    if not gone: raise HTTPException(404, texts.t("dash.key_not_stored", name=name))
    if name not in vault.plain_secrets(): os.environ.pop(name, None)
    keybox.record(name, "delete", client_ip(req))
    return JSONResponse({"ok": True, "name": name})

@app.post("/api/keys/test")
async def key_test(req: Request):
    """{"name": ..., "value": ...}: one read-only call of the connector with this key (no value: the stored one).
    Nothing is stored. The answer is only {"ok": true|false, "reason": "..."}."""
    key_write(req)
    body = await body_of(req)
    name, value = str(body.get("name") or "").strip(), str(body.get("value") or "").strip()
    if not vault.NAME_OK.fullmatch(name): raise HTTPException(400, texts.t("dash.key_bad"))
    try: r = await asyncio.to_thread(keybox.check, name, value or None, ROOT)
    except LookupError: raise HTTPException(404, texts.t("dash.key_no_test"))
    return JSONResponse({"ok": r["ok"], "reason": r["reason"]})

@app.post("/api/keys/import")
def key_import(req: Request):
    """Moves the keys left in .env into the store. The browser asks the owner first (the button's confirm)."""
    key_write(req)
    try: r = vault.import_env()
    except (vault.VaultError, OSError) as e: raise HTTPException(409, str(e))
    for n in r["moved"]: keybox.record(n, "import", client_ip(req))
    return JSONResponse({"ok": True, "moved": r["moved"]})

if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST") or ("0.0.0.0" if PASSWORD else "127.0.0.1")
    to_file = logs.setup("dashboard")  # then uvicorn's own lines go through the same rotating file
    uvicorn.run(app, host=host, port=int(os.getenv("DASHBOARD_PORT", "8080")), **({"log_config": None} if to_file else {}))
