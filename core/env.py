"""Read the repo's .env into os.environ at startup. Docker does this with env_file; a native install has nothing else.
A variable that is already set (Docker, `export`, `PACKS=… python …`) always wins, even when it is set to empty."""
import os, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")
ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}

def parse_value(raw: str) -> str:
    v = raw.strip()
    if v[:1] == "'" and "'" in v[1:]:  # single quotes: literal
        return v[1:v.index("'", 1)]
    if v[:1] == '"':  # double quotes: \n \t \" \\ are unescaped
        m = re.match(r'"((?:[^"\\]|\\.)*)"', v, re.S)
        if m: return re.sub(r"\\(.)", lambda e: ESCAPES.get(e.group(1), e.group(0)), m.group(1))
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()  # unquoted: " #" starts a comment, "a#b" and "#a" do not

def parse(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = None if line.lstrip().startswith("#") else LINE.fullmatch(line)
        if m: out[m.group(1)] = parse_value(m.group(2))
    return out

def load(path=None) -> list:
    """Returns the names it set. A missing or unreadable file is not an error: the defaults apply."""
    try:
        text = pathlib.Path(path or ROOT / ".env").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    new = {k: v for k, v in parse(text).items() if k not in os.environ}
    os.environ.update(new)
    return list(new)
