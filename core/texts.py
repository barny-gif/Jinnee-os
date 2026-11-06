"""What the system itself says to the owner, in the owner's language: Telegram messages, alerts, the dashboard.

One dictionary per language in core/locale/<lang>.json: a key → the text, with {name} fields filled in by str.format.
en.json is complete and is the fallback: a key missing from another language, an unknown language, or a translated text
whose fields do not fit, all come out in English instead of failing (the last one is logged). A new language is a new
file next to these two; tests/test_texts.py checks that every language has every key, with the same fields.

The language is read on every call (business.language(): JINNEE_LANG in .env wins, then business.language in
config/business.yaml, else en), so a change in the file needs no restart. "hu-HU" and "HU" count as "hu".

Not here, on purpose: the commands (/ok, /pending…), the machine's marks (<!-- work: in-progress -->, [pin], SILENT,
the approval ids, file names), the log lines, and the agents' prompts. Those stay the same in every language.

  python3 core/texts.py check      every language against en.json: missing keys, extra keys, fields that differ
"""
import json, logging, pathlib, string, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import business

log = logging.getLogger("jinnee.texts")
LOCALE = pathlib.Path(__file__).resolve().parent / "locale"
BASE = "en"
_cache = {}


def code(lang) -> str:
    """'hu-HU', 'HU', ' hu ' → 'hu'. Anything without letters → the base language."""
    c = str(lang or "").strip().lower().replace("_", "-").split("-")[0]
    return c if c.isalpha() else BASE


def language() -> str:
    return code(business.language(BASE))


def table(lang) -> dict:
    """The dictionary of one language; {} when there is no file for it, or it cannot be read (then English is used)."""
    lang = code(lang)
    p = LOCALE / f"{lang}.json"
    try: st = p.stat(); key = (st.st_mtime_ns, st.st_size)
    except OSError: return {}
    hit = _cache.get(lang)
    if hit and hit[0] == key: return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict): raise ValueError("not a key → text list")
        data = {k: v for k, v in data.items() if isinstance(v, str) and not k.startswith("_")}
    except (OSError, ValueError) as e:
        log.warning("locale/%s.json cannot be read (%s); English is used", lang, type(e).__name__)
        data = {}
    _cache[lang] = (key, data)
    return data


def fields(text) -> set:
    """The {names} a text expects."""
    return {f for _, f, _, _ in string.Formatter().parse(text) if f is not None and f != ""}


def t(key, lang=None, **kw) -> str:
    """The text for `key` in `lang` (default: the owner's language), with the fields filled in.
    A key that is not even in English is a bug in the program: KeyError, so a test finds it."""
    base = table(BASE)
    if key not in base: raise KeyError(f"no text '{key}' in locale/{BASE}.json")
    lang = code(lang) if lang else language()
    text = table(lang).get(key, base[key]) if lang != BASE else base[key]
    try: return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        if text is base[key]: raise
        log.warning("locale/%s.json: '%s' does not fit its fields; the English text is used", lang, key)
        return base[key].format(**kw)


def bundle(prefix, lang=None) -> dict:
    """Every text whose key starts with `prefix` (e.g. "dash."), in `lang`, English where it is missing. For the dashboard."""
    lang = code(lang) if lang else language()
    own = table(lang)
    return {k: own.get(k, v) for k, v in table(BASE).items() if k.startswith(prefix)}


def problems() -> list:
    """Every language file against en.json → readable lines ([] = all good)."""
    base, out = table(BASE), []
    if not base: return [f"locale/{BASE}.json is missing or cannot be read"]
    for p in sorted(LOCALE.glob("*.json")):
        lang = p.stem
        if lang == BASE: continue
        own = table(lang)
        if not own: out.append(f"locale/{lang}.json is empty or cannot be read"); continue
        out += [f"locale/{lang}.json: '{k}' is missing (English is shown)" for k in base if k not in own]
        out += [f"locale/{lang}.json: '{k}' is not a key in {BASE}.json" for k in own if k not in base]
        for k in base:
            if k in own:
                try: a, b = fields(base[k]), fields(own[k])
                except ValueError: out.append(f"locale/{lang}.json: '{k}' has a broken {{field}}"); continue
                if a != b: out.append(f"locale/{lang}.json: '{k}' has the fields {sorted(b)}, English has {sorted(a)}")
    return out


if __name__ == "__main__":
    if sys.argv[1:] != ["check"]: sys.exit(__doc__)
    probs = problems()
    print("\n".join(probs) if probs else "texts: ok")
    sys.exit(1 if probs else 0)
