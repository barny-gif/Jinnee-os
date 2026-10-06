"""Load and merge packs. `PACKS=general,ecom python core/pack_loader.py --init`"""
import json, os, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKS_DIR = ROOT / "packs"
BRAIN = ROOT / "brain"

def load_pack(pack_id: str) -> dict:
    p = PACKS_DIR / pack_id / "pack.json"
    if not p.exists():
        raise SystemExit(f"No such pack: {pack_id}")
    data = json.loads(p.read_text(encoding="utf-8"))
    data["_dir"] = str(PACKS_DIR / pack_id)
    return data

def order(pack_ids):
    return ["general"] + [p for p in pack_ids if p and p != "general"]

def resolve(pack_ids):
    """general always comes first; vertical packs inherit and override."""
    merged = {"agents": ["jinnee"], "connectors": {"required": [], "optional": [], "defaults": {}},
              "autonomy_defaults": {}, "onboarding": []}
    for pid in order(pack_ids):
        pk = load_pack(pid)
        for a in pk.get("agents", []):
            if a not in merged["agents"]: merged["agents"].append(a)
        c = pk.get("connectors", {})
        for k in ("required", "optional"):
            for x in c.get(k, []):
                if x not in merged["connectors"][k]: merged["connectors"][k].append(x)
        merged["connectors"]["defaults"].update(c.get("defaults", {}))
        merged["autonomy_defaults"].update(pk.get("autonomy_defaults", {}))
        if pk.get("onboarding"): merged["onboarding"].append(f"{pk['_dir']}/{pk['onboarding']}")
    return merged

def agent_files(pack_ids):
    files = [ROOT / "core" / "jinnee.md"]
    for pid in order(pack_ids):
        d = PACKS_DIR / pid / "agents"
        if d.exists(): files += sorted(d.glob("*.md"))
    return files

if __name__ == "__main__":
    import env; env.load()  # PACKS given on the command line still wins
    packs = [p.strip() for p in os.getenv("PACKS", "general").split(",")]
    m = resolve(packs)
    if "--init" in sys.argv:
        BRAIN.mkdir(exist_ok=True)
        defaults = {"autonomy_config.json": json.dumps(m["autonomy_defaults"], indent=2, ensure_ascii=False),
                    "approvals.json": "[]", "heartbeat.json": "{}",
                    "lessons.md": "# lessons – the owner's corrections\n",
                    "decisions.log.md": "# decisions\n"}
        for name, content in defaults.items():
            f = BRAIN / name
            if not f.exists(): f.write_text(content, encoding="utf-8")
        print("brain/ ready. Agents:", ", ".join(m["agents"]))
    else:
        print(json.dumps(m, indent=2, ensure_ascii=False))
