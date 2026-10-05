"""Jinnee dashboard – one page. Shows the brain/ files; the approval buttons write approvals.json."""
import os, json, pathlib, time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
HERE = pathlib.Path(__file__).resolve().parent
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
NAME = os.getenv("JINNEE_NAME", "Jinnee")

app = FastAPI(title="Jinnee dashboard")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

def rd(name, default):
    p = BRAIN / name
    if not p.exists(): return default
    t = p.read_text(encoding="utf-8")
    return json.loads(t) if name.endswith(".json") else t

def guard(req: Request):
    if PASSWORD and req.headers.get("x-dash-key") != PASSWORD and req.query_params.get("key") != PASSWORD:
        raise HTTPException(401, "Password required (?key=…)")

@app.get("/", response_class=HTMLResponse)
def index(req: Request):
    guard(req)
    return (HERE / "static" / "index.html").read_text(encoding="utf-8").replace("{{NAME}}", NAME)

@app.get("/api/state")
def state(req: Request):
    guard(req)
    hb = rd("heartbeat.json", {}); now = int(time.time())
    team = [{"agent": a, "ago_min": (now - v.get("ts", 0)) // 60, "note": v.get("note", "")} for a, v in hb.items()]
    log = [l[2:] for l in rd("decisions.log.md", "").splitlines() if l.startswith("- ")][-30:]
    connectors = []
    for c in sorted((ROOT / "connectors").glob("*.manifest.json")):
        d = json.loads(c.read_text(encoding="utf-8"))
        envs = d.get("auth", {}).get("env", [])
        envs = [envs] if isinstance(envs, str) else envs
        ok = all(os.getenv(e) for e in envs) if envs else None
        connectors.append({"id": d["id"], "name": d["name"], "category": d["category"], "ok": ok})
    return {"name": NAME, "brief": rd("brief_today.md", ""), "approvals": rd("approvals.json", []),
            "team": team, "connectors": connectors, "log": log[::-1]}

@app.post("/api/approve/{item_id}")
async def approve(item_id: str, req: Request):
    guard(req)
    body = await req.json()  # {"decision": "ok"|"edit"|"drop", "note": "..."}
    items = rd("approvals.json", [])
    for it in items:
        if it.get("id") == item_id:
            it.update(decision=body.get("decision"), note=body.get("note", ""), decided_at=int(time.time()))
    (BRAIN / "approvals.json").write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    with (BRAIN / "decisions.log.md").open("a", encoding="utf-8") as f:
        f.write(f"- {time.strftime('%Y-%m-%d')} dashboard: {item_id} → {body.get('decision')} {body.get('note','')}\n")
    return JSONResponse({"ok": True})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("DASHBOARD_PORT", "8080")))
