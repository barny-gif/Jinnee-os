"""The child process behind the dashboard's "Try it" button (core/keybox.py check). Not for running by hand.

Reads one job on stdin: {"root", "connector", "op", "args", "set": {NAME: value}, "clear": [NAME, ...]}. Imports
connectors/<connector>/ops.py (which loads .env and the key store as usual), then puts the key being tried in place of the
stored one, removes the names in "clear", and calls the one read-only function the manifest names. Prints exactly one line:
{"ok": true|false, "reason": "...", "missing": [...]}. What the function returned is never printed: it may hold the shop's data.
The value arrives on stdin, so it is never on a command line, and it is not written anywhere.
"""
import contextlib, importlib.util, io, json, os, pathlib, re, sys


def main():
    try:
        job = json.loads(sys.stdin.read())
        root = pathlib.Path(job["root"])
        cid, op = str(job["connector"]), str(job["op"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cid) or not re.fullmatch(r"[a-z_][a-z0-9_]*", op) or op.startswith("_"):
            raise ValueError("bad job")
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "reason": "bad job"}
    sys.path.insert(0, str(root / "core"))
    sys.path.insert(0, str(root / "connectors"))
    quiet = io.StringIO()
    try:
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            spec = importlib.util.spec_from_file_location("keycheck_ops", root / "connectors" / cid / "ops.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for n in job.get("clear") or []:
                os.environ.pop(str(n), None)
            for n, v in (job.get("set") or {}).items():
                os.environ[str(n)] = str(v)
            result = getattr(mod, op)(**{str(k): str(v) for k, v in (job.get("args") or {}).items()})
    except Exception as e:  # ConnectorError carries the connector's own words; anything else is named by its type only
        msg = str(e) if type(e).__name__ == "ConnectorError" else type(e).__name__
        return {"ok": False, "reason": msg}
    missing = result.get("missing") if isinstance(result, dict) else None
    if missing:  # e.g. Shopify scopes: the key works, but some rights the team needs are not granted
        return {"ok": True, "reason": "", "missing": [str(m) for m in missing][:20]}
    return {"ok": True, "reason": ""}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False))
