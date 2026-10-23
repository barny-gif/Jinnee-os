"""A made-up business for the tests (never a real one): writes config-like YAML to a temp file and points
JINNEE_BUSINESS_FILE at it. use() is called at import by the test modules that make labels, invoices or sales figures."""
import atexit, os, pathlib, shutil, tempfile

SHOP = "example-shop.hu"
PREFIX = "EX-"
TAGS = ["barter", "influencer", "teszt", "ajándék", "ajandek"]  # generic Hungarian/English words, also tested as marks
YAML = f"""business:
  name: "Example Kft."
  language: "hu"
  voice: "Warm and informal, short sentences."
  intro: "A small shop selling wooden toys."
shop:
  label_name: "{SHOP}"
  not_revenue_tags: {TAGS!r}
labels:
  note: "{{shop}} {{order}}, {{goods}}, {{payment}}"
  cod: "COD {{amount}} Ft"
  paid: "PAID"
billing:
  invoice_prefix: "{PREFIX}"
"""
_dir = None


def write(text=YAML) -> pathlib.Path:
    global _dir
    if _dir is None:
        _dir = tempfile.mkdtemp(prefix="jinnee-business-")
        atexit.register(shutil.rmtree, _dir, True)
    p = pathlib.Path(tempfile.mkstemp(suffix=".yaml", dir=_dir)[1])
    p.write_text(text, encoding="utf-8")
    return p


def use() -> pathlib.Path:
    """Sets the fake business for this test process (unless a test already did)."""
    p = write()
    os.environ["JINNEE_BUSINESS_FILE"] = str(p)
    os.environ.pop("BILLINGO_PAYMENTS_PREFIX", None)
    return p
