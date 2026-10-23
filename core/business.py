"""The owner's own business settings: config/business.yaml. Kept out of git (.gitignore); the repo only ships
config/business.example.yaml with made-up values.

What lives where:
  .env                    HOW the system connects: keys, tokens, the Shopify store handle, the GLS pickup address, VAT.
  config/business.yaml    WHO the business is and what goes on its documents: name, language, tone of voice, intro,
                          the parcel label note, the invoice series to show, the order tags that are not revenue.

Nothing here has a built-in fallback for another business. A value that a step needs and that is missing stops that
step with an error that says which key to set (the label or invoice is not made), instead of quietly using a default.

CLI:
  python3 core/business.py check                     every key the installed packs need: ok, or what is missing
  python3 core/business.py label --order "#1001" --goods "1x Wooden puzzle" --cod 9890
                                                     prints the parcel label note exactly as it would go to GLS (offline)
  python3 core/business.py setup                     asks the questions in the terminal and writes config/business.yaml
  python3 core/business.py init [--force yes]        writes config/business.yaml from variables (no questions):
      BUSINESS_NAME, BUSINESS_LANGUAGE (default hu), BUSINESS_VOICE, BUSINESS_INTRO,
      SHOP_LABEL_NAME, SHOP_NOT_REVENUE_TAGS (comma separated), BILLING_INVOICE_PREFIX
  JINNEE_BUSINESS_FILE=<path> points every command (and the programs) at another file."""
import json, os, pathlib, string, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config" / "business.yaml"
EXAMPLE = "config/business.example.yaml"
LABEL_MAX = 200  # MyGLS Content: the note printed on the label; kept short so it is not cut on the label
LABEL_FIELDS = {"shop", "order", "goods", "payment"}
PAYMENT_FIELDS = {"amount"}
# Generic label wording; every business can change it in its own file. No business name in here.
DEFAULT_LABELS = {"note": "{shop} {order}, {goods}, {payment}", "cod": "COD {amount} Ft", "paid": "PAID"}


class BusinessConfigError(Exception):
    """config/business.yaml is missing, unreadable, or lacks a value a step needs. The step does not run."""


def path() -> pathlib.Path:
    raw = os.getenv("JINNEE_BUSINESS_FILE", "").strip()
    return pathlib.Path(raw) if raw else DEFAULT_PATH


def _fail(what: str) -> BusinessConfigError:
    return BusinessConfigError(f"{path()}: {what}. Nothing was sent. Fix it in that file (see {EXAMPLE}) "
                               f"or run `python3 core/business.py setup`, then `python3 core/business.py check`.")


_cache = {"key": None, "data": None}


def load() -> dict:
    """The parsed file. Read again whenever it changes on disk (an edit needs no restart); otherwise kept in memory."""
    p = path()
    try: st = p.stat(); key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError: key = None
    if key and _cache["key"] == key: return _cache["data"]
    data = _read(p)
    _cache.update(key=key, data=data)
    return data


def _read(p) -> dict:
    if not p.exists():
        raise _fail("the file does not exist (the business settings were never set up on this machine)")
    try:
        import yaml
    except ImportError:
        raise BusinessConfigError("PyYAML is not installed, so config/business.yaml cannot be read. "
                                  "Run `pip install -r requirements.txt` (Docker: `docker compose up -d --build`). Nothing was sent.")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as e:
        raise _fail(f"cannot be read as YAML ({type(e).__name__}: {str(e).splitlines()[0][:160]})")
    if not isinstance(data, dict):
        raise _fail("is empty or not a list of settings")
    return data


def _get(data: dict, key: str):
    node = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise _fail(f"'{key}' is missing")
        node = node[part]
    return node


def text(key: str, allow_empty=False, data=None) -> str:
    """A text value. allow_empty: the key must be there, but "" is a valid answer (e.g. 'every invoice series')."""
    v = _get(load() if data is None else data, key)
    if v is None: v = ""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise _fail(f"'{key}' must be a text, not {type(v).__name__}")
    v = str(v).strip()
    if not v and not allow_empty:
        raise _fail(f"'{key}' is empty")
    return v


def words(key: str, data=None) -> list:
    """A list of words (may be empty: [] ), lower-cased."""
    v = _get(load() if data is None else data, key)
    if v is None: return []
    if not isinstance(v, list) or not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in v):
        raise _fail(f"'{key}' must be a list, like [\"test\", \"gift\"] (or [] for none)")
    return [str(x).strip().lower() for x in v if str(x).strip()]


def _template(key: str, allowed: set, data: dict, must=()) -> str:
    t = text(key, data=data)
    try:
        used = {f for _, f, _, _ in string.Formatter().parse(t) if f is not None}
    except ValueError as e:
        raise _fail(f"'{key}' is not a valid template ({e}); write placeholders like {{order}}")
    bad = sorted(used - allowed)
    if bad: raise _fail(f"'{key}' uses unknown placeholder(s) {', '.join('{' + b + '}' for b in bad)}; allowed: "
                        + ", ".join('{' + a + '}' for a in sorted(allowed)))
    gone = [m for m in must if m not in used]
    if gone: raise _fail(f"'{key}' must contain {', '.join('{' + m + '}' for m in gone)}")
    return t


def labels(data=None) -> dict:
    """The label settings, checked: shop name and the three templates."""
    data = load() if data is None else data
    return {"shop": text("shop.label_name", data=data),
            "note": _template("labels.note", LABEL_FIELDS, data, must=("order",)),
            "cod": _template("labels.cod", PAYMENT_FIELDS, data, must=("amount",)),
            "paid": _template("labels.paid", set(), data)}


def label_note(order: str, goods: str, cod_amount=0, data=None) -> str:
    """The note printed on the parcel label. The template is split at ', '; a part that comes out empty (no goods
    listed) is left out with its comma. Never holds personal data: only what the template names."""
    cfg = labels(data)
    pay = cfg["cod"].format(amount=int(cod_amount)) if cod_amount else cfg["paid"]
    fields = {"shop": cfg["shop"], "order": order, "goods": goods, "payment": pay}
    parts = [p.format(**fields).strip() for p in cfg["note"].split(", ")]
    out = ", ".join(p for p in parts if p)
    return out if len(out) <= LABEL_MAX else out[:LABEL_MAX - 1] + "…"


def invoice_prefix(data=None) -> str:
    """Only invoices whose number starts with this are shown (Payments). "" = every series."""
    data = load() if data is None else data
    prefix = text("billing.invoice_prefix", allow_empty=True, data=data)
    old = os.environ.get("BILLINGO_PAYMENTS_PREFIX")
    if old is not None and old.strip() != prefix:  # the old place: refuse to guess which one is meant
        raise BusinessConfigError(f"BILLINGO_PAYMENTS_PREFIX={old.strip()!r} is still set in .env, but the invoice series now comes from "
                                  f"{path()} billing.invoice_prefix ({prefix!r}). Put the right value in the YAML file and delete the "
                                  ".env line. Nothing was sent.")
    return prefix


def not_revenue_tags(data=None) -> list:
    return words("shop.not_revenue_tags", data=data)


def prompt_block() -> str:
    """For the agents' system prompt: who the business is. When the file is not usable, says so plainly instead."""
    try:
        data = load()
        lines = [f"Business name: {text('business.name', data=data)}",
                 f"Language with the owner and the customers: {text('business.language', data=data)}",
                 f"Tone of voice for anything customer-facing: {text('business.voice', data=data)}"]
        intro = text("business.intro", allow_empty=True, data=data) if "intro" in (data.get("business") or {}) else ""
        if intro: lines.append(f"About the business (the owner's words): {intro}")
        return "\n".join(lines)
    except BusinessConfigError as e:
        return (f"SETUP MISSING: {e}\nTell the owner this in one line. Until it is fixed, do not invent the business's name, "
                "language or tone of voice, and do not prepare parcel labels or invoices.")


def language(default="en") -> str:
    """JINNEE_LANG in .env still wins (older installs); otherwise the business file; otherwise the default."""
    env = os.getenv("JINNEE_LANG", "").strip()
    if env: return env
    try: return text("business.language")
    except BusinessConfigError: return default


def check(packs=None) -> list:
    """Every problem with the file for the installed packs ([] = all good)."""
    packs = packs if packs is not None else [p.strip() for p in os.getenv("PACKS", "general").split(",") if p.strip()]
    try: data = load()
    except BusinessConfigError as e: return [str(e)]
    probs = []
    steps = [lambda: text("business.name", data=data), lambda: text("business.language", data=data),
             lambda: text("business.voice", data=data)]
    if "ecom" in packs:
        steps += [lambda: labels(data), lambda: invoice_prefix(data), lambda: not_revenue_tags(data)]
    for s in steps:
        try: s()
        except BusinessConfigError as e: probs.append(str(e))
    return probs


# --- writing the file (no PyYAML needed: a JSON string is a valid YAML string)

def _q(v) -> str:
    return json.dumps(str(v), ensure_ascii=False)


def render(name, language, voice, intro="", label_name="", tags=(), invoice_prefix=""):
    tags_yaml = "[" + ", ".join(_q(t) for t in tags) + "]"
    return f"""# Your business settings for Jinnee OS. This file stays on your machine: it is in .gitignore and never goes to GitHub.
# Edit it any time; the programs read it on every use. Check it with: python3 core/business.py check
# The template with every key explained: {EXAMPLE}

business:
  name: {_q(name)}            # as your customers know you
  language: {_q(language)}    # the language the team speaks with you and your customers (hu, en, de...)
  voice: {_q(voice)}          # how the team sounds to customers
  intro: {_q(intro)}          # a sentence or two about the business, in your words

shop:
  label_name: {_q(label_name)}   # first thing on the parcel label note, e.g. your shop's web address
  not_revenue_tags: {tags_yaml}  # orders with one of these Shopify tags are left out of sales figures ([] = none)

labels:                          # the parcel label note; placeholders: {{shop}} {{order}} {{goods}} {{payment}}
  note: {_q(DEFAULT_LABELS["note"])}
  cod: {_q(DEFAULT_LABELS["cod"])}       # {{amount}} = the cash on delivery amount
  paid: {_q(DEFAULT_LABELS["paid"])}

billing:
  invoice_prefix: {_q(invoice_prefix)}   # Payments shows only invoice numbers starting with this ("" = every series)
"""


def write(content: str, force=False) -> pathlib.Path:
    p = path()
    if p.exists() and not force:
        raise BusinessConfigError(f"{p} already exists; it was not changed. Edit it by hand, or add --force yes to replace it.")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)
    return p


def _tags(raw: str) -> list:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def init(force="no"):
    """From variables, for installs without a terminal. BUSINESS_NAME and BUSINESS_VOICE are required, plus
    SHOP_LABEL_NAME when the ecom pack is on."""
    env = lambda k, d="": os.getenv(k, d).strip()
    packs = [p.strip() for p in os.getenv("PACKS", "general").split(",")]
    need = ["BUSINESS_NAME", "BUSINESS_VOICE"] + (["SHOP_LABEL_NAME"] if "ecom" in packs else [])
    missing = [k for k in need if not env(k)]
    if missing: raise BusinessConfigError("Not set: " + ", ".join(missing) + ". config/business.yaml was not written.")
    p = write(render(env("BUSINESS_NAME"), env("BUSINESS_LANGUAGE") or "hu", env("BUSINESS_VOICE"), env("BUSINESS_INTRO"),
                     env("SHOP_LABEL_NAME"), _tags(env("SHOP_NOT_REVENUE_TAGS")), env("BILLING_INVOICE_PREFIX")), force == "yes")
    return {"written": str(p)}


def setup(ask=input, force="no"):
    """The same questions as the installer, in the terminal."""
    packs = [p.strip() for p in os.getenv("PACKS", "general").split(",")]
    def q(question, default="", required=True):
        while True:
            a = ask(question + (f" [{default}] " if default else " ")).strip() or default
            if a or not required: return a
            print("  An answer is needed here.")
    name = q("Your business name, as customers know it:")
    lang = q("Language for you and your customers (hu, en, de...):", "hu")
    voice = q("How should the team sound to customers? (e.g. warm, informal, short sentences):")
    intro = q("One or two sentences about the business (Enter to skip):", required=False)
    label, tags, prefix = "", [], ""
    if "ecom" in packs:
        label = q("Shop name for the parcel label note (e.g. your-shop.com):")
        tags = _tags(q("Order tags that are not revenue, comma separated (Enter = none):", required=False))
        prefix = q("Show only invoice numbers starting with (Enter = every series):", required=False)
    p = write(render(name, lang, voice, intro, label, tags, prefix), force == "yes")
    return {"written": str(p)}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv: print(__doc__); return 2
    op, it, kw = argv[0], iter(argv[1:]), {}
    for a in it:
        if a.startswith("--"): kw[a[2:]] = next(it, "")
    try:
        if op == "check":
            probs = check()
            print(f"{path()}: ok" if not probs else "\n".join(probs)); return 0 if not probs else 1
        if op == "label":
            print(label_note(kw.get("order", "#1001"), kw.get("goods", ""), float(kw.get("cod") or 0))); return 0
        if op == "init": print(json.dumps(init(kw.get("force", "no")))); return 0
        if op == "setup": print(json.dumps(setup(force=kw.get("force", "no")))); return 0
    except BusinessConfigError as e:
        print(f"error: {e}", file=sys.stderr); return 1
    print(__doc__); return 2


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "core"))
    import env; env.load()
    sys.exit(main())
