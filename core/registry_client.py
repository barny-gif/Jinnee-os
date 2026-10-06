"""Update check against the jinnee-os.com registry. Never installs on its own – only reports."""
import json, os, pathlib, requests
ROOT = pathlib.Path(__file__).resolve().parent.parent

def installed_versions():
    out = {}
    for p in (ROOT / "packs").glob("*/pack.json"):
        d = json.loads(p.read_text(encoding="utf-8")); out[f"pack:{d['id']}"] = d["version"]
    for c in (ROOT / "connectors").glob("*.manifest.json"):
        d = json.loads(c.read_text(encoding="utf-8")); out[f"connector:{d['id']}"] = d["version"]
    return out

def check():
    url = os.getenv("REGISTRY_URL", "")
    if not url: return []
    try:
        idx = requests.get(url, timeout=10).json()
    except Exception:
        return []
    have = installed_versions(); updates = []
    for item in idx.get("items", []):
        key = f"{item['kind']}:{item['id']}"
        if key in have and item["version"] != have[key]:
            updates.append({"key": key, "from": have[key], "to": item["version"],
                            "changelog": item.get("changelog", ""), "url": item["url"]})
    return updates

if __name__ == "__main__":
    import env; env.load()
    for u in check(): print(f"{u['key']}: {u['from']} → {u['to']} – {u['changelog']}")
