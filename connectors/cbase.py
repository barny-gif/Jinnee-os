"""Shared bits for the connectors: env check, one error type, JSON printing for the CLI. Read-only helpers only."""
import json, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "core"))
import env; env.load()

class ConnectorError(Exception):
    """Anything the agent or dashboard should report as 'the connector did not answer', never guess around."""

def need(*names):
    missing = [n for n in names if not os.getenv(n, "").strip()]
    if missing: raise ConnectorError("Missing in .env: " + ", ".join(missing))
    return [os.environ[n].strip() for n in names]

def run_cli(ops: dict, argv=None):
    """python3 connectors/<id>/ops.py <op> [--key value ...] → JSON on stdout. Errors → {"error": ...}, exit 1."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ops:
        print(json.dumps({"error": "usage: <op> [--key value]", "ops": sorted(ops)})); return 2
    kwargs, it = {}, iter(argv[1:])
    for a in it:
        if a.startswith("--"): kwargs[a[2:].replace("-", "_")] = next(it, "")
    try:
        print(json.dumps(ops[argv[0]](**kwargs), ensure_ascii=False, indent=2)); return 0
    except (ConnectorError, TypeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False)); return 1
