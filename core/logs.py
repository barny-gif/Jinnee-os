"""Logging for the two long-running programs. With JINNEE_LOG_DIR set (run.sh sets it) each program writes
<dir>/<name>.log and rotates it at 1 MB, keeping three old files, so the folder stays under 8 MB for good.
Without it the lines go to stderr, where Docker collects them (docker-compose.yml caps those too)."""
import logging, logging.handlers, os, pathlib

def setup(name):
    folder = os.getenv("JINNEE_LOG_DIR", "").strip()
    if folder:
        pathlib.Path(folder).mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(pathlib.Path(folder) / f"{name}.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines carry the request URL, and the bot token is in it
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return bool(folder)
