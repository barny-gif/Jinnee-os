"""Agent liveness. At the end of every agent run: beat('social-media-manager', 'posts drafted')."""
import json, time, pathlib
HB = pathlib.Path(__file__).resolve().parent.parent / "brain" / "heartbeat.json"
STALE = 60 * 60  # one silent hour is suspicious

def _load():
    return json.loads(HB.read_text(encoding="utf-8")) if HB.exists() else {}

def beat(agent: str, note: str = ""):
    data = _load(); data[agent] = {"ts": int(time.time()), "note": note}
    HB.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def silent(expected):
    data = _load(); now = int(time.time())
    return [a for a in expected if now - data.get(a, {}).get("ts", 0) > STALE]
