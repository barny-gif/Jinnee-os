"""Keeps a business's own data out of this public repo: phone numbers, e-mail addresses, real Shopify store handles and
access tokens in what a commit adds, plus order numbers and money amounts in commit messages.

Two layers:
  1. Generic patterns (below, in this file): run by the git hooks AND by GitHub Actions on every push and pull request.
     They name no business, so they can be public.
  2. Your own words: config/private_patterns.txt (in .gitignore, never committed): your shop name, domain, product names,
     invoice series, your own name... Only the local git hooks read it. One entry per line; `#` starts a comment;
     a plain line matches as text (any case); a line starting with `re:` is a regular expression.

Usage:
  python3 tools/guard.py install-hooks          once per clone: git uses .githooks/ (pre-commit + commit-msg)
  python3 tools/guard.py staged                 what `git commit` is about to add (the pre-commit hook)
  python3 tools/guard.py msg FILE               a commit message (the commit-msg hook)
  python3 tools/guard.py range BASE HEAD        every line added and every commit message between two commits (CI)
  python3 tools/guard.py scan [PATH ...]        whole files, for a look at what is already there (report only)
Exit code 1 and a list of `file:line: what` when something is found. The found text itself is shown masked."""
import os, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "config" / "private_patterns.txt"

# Obviously made-up numbers the tests use (the last 7 digits). Anything else that looks like a Hungarian phone number is refused.
FAKE_SUBSCRIBERS = {"1234567", "1112222", "0000000", "1111111", "7654321", "9999999"}
EXAMPLE_DOMAIN = re.compile(r"(^|\.)(example(-[a-z0-9-]+)?\.[a-z.]+|[a-z0-9-]+\.(example|test|invalid|local|localhost)|localhost|"
                            r"users\.noreply\.github\.com|jinnee-os\.com|[a-z]\.[a-z]{2,3})$", re.I)
EXAMPLE_HANDLES = {"demo", "example", "example-shop", "your-store", "your-shop", "yourstore", "test", "shop", "store", "my-store"}
NOREPLY = {"noreply@anthropic.com"}

PHONE = re.compile(r"(?<![\w+])(?:\+36|06)[ /-]?(?:1|[2-9]\d)[ /-]?\d{3}[ -]?\d{3,4}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
SHOPIFY = re.compile(r"\b([a-z0-9][a-z0-9-]*)\.myshopify\.com", re.I)
TOKENS = [("a Shopify access token", re.compile(r"\bshp(?:at|ss|ca|pa)_[0-9a-fA-F]{32}\b")),
          ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,})\b")),
          ("a Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
          ("an Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
          ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"))]
# Commit messages only: code and tests may hold made-up order numbers and amounts, a message should not hold real ones.
ORDER_NO = re.compile(r"(?<![\w/])#1\d{3}(?!\d)|\b(?:order|orders|rendelés|rendeles)\s+(?:no\.?\s*)?#?\d{4,}\b", re.I)  # Shopify numbers orders from #1001
AMOUNT = re.compile(r"\b\d{1,3}(?:[ .,]\d{3})+\s?(?:Ft|HUF)\b|\b\d{4,}\s?(?:Ft|HUF)\b", re.I)


def mask(s: str) -> str:
    s = s.strip()
    return s if len(s) <= 4 else s[:2] + "*" * (len(s) - 4) + s[-2:]


def private_patterns(path=None):
    """config/private_patterns.txt → [(label, compiled)]. Missing file → []."""
    p = pathlib.Path(path) if path else PRIVATE
    try: lines = p.read_text(encoding="utf-8-sig").splitlines()
    except OSError: return []
    out = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"): continue
        try:
            rx = re.compile(line[3:], re.I) if line.startswith("re:") else re.compile(re.escape(line), re.I)
        except re.error as e:
            raise SystemExit(f"{p}:{n}: not a valid regular expression ({e})")
        out.append((f"a private word (config/private_patterns.txt line {n})", rx))
    return out


def findings(text: str, message=False, private=()):
    """Every problem in one text: [(what, masked match)]."""
    out = []
    for m in PHONE.finditer(text):
        if re.sub(r"\D", "", m.group(0))[-7:] not in FAKE_SUBSCRIBERS: out.append(("a phone number", mask(m.group(0))))
    for m in EMAIL.finditer(text):
        if m.group(0).lower() not in NOREPLY and not EXAMPLE_DOMAIN.search(m.group(1)): out.append(("an e-mail address", mask(m.group(0))))
    for m in SHOPIFY.finditer(text):
        if m.group(1).lower() not in EXAMPLE_HANDLES: out.append(("a real Shopify store handle", mask(m.group(0))))
    for what, rx in TOKENS:
        out += [(what, mask(m.group(0))) for m in rx.finditer(text)]
    if message:
        out += [("an order number", m.group(0)) for m in ORDER_NO.finditer(text)]
        out += [("a money amount", mask(m.group(0))) for m in AMOUNT.finditer(text)]
    for what, rx in private:
        out += [(what, mask(m.group(0))) for m in rx.finditer(text)]
    return out


def added_lines(diff: str):
    """`git diff -U0` → [(file, line number, text)] for the added lines only."""
    out, file, n = [], None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line); n = int(m.group(1)) if m else 0
        elif line.startswith("+") and file:
            out.append((file, n, line[1:])); n += 1
    return out


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def check_lines(lines, private=()):
    return [f"{f}:{n}: {what} ({shown})" for f, n, text in lines for what, shown in findings(text, private=private)]


def check_message(text, where="commit message", private=()):
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))  # git's own comment lines
    return [f"{where}: {what} ({shown})" for what, shown in findings(body, message=True, private=private)]


def report(problems, hint) -> int:
    if not problems: return 0
    print("guard: this would make business data public:", file=sys.stderr)
    for p in problems: print("  " + p, file=sys.stderr)
    print(hint, file=sys.stderr)
    return 1


HINT_LOCAL = ("Replace it with a made-up value (example-shop.hu, #5001, an 06 30 123 4567-style test number), or write the "
              "commit message without it. Nothing was committed.")
HINT_CI = "Replace it with a made-up value and push again (README: \"Keeping your business out of the repo\")."


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    op = argv[0] if argv else ""
    if op == "install-hooks":
        git("config", "core.hooksPath", ".githooks")
        print("git now uses .githooks/ (pre-commit, commit-msg). Your own words go into config/private_patterns.txt.")
        return 0
    if op == "staged":
        return report(check_lines(added_lines(git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff")), private_patterns()), HINT_LOCAL)
    if op == "msg" and len(argv) == 2:
        return report(check_message(pathlib.Path(argv[1]).read_text(encoding="utf-8", errors="replace"), private=private_patterns()), HINT_LOCAL)
    if op == "range" and len(argv) == 3:
        base, head = argv[1], argv[2]
        probs = check_lines(added_lines(git("diff", "-U0", "--no-color", "--no-ext-diff", f"{base}...{head}")))
        for sha in git("rev-list", f"{base}..{head}").split():
            probs += check_message(git("log", "-1", "--format=%B", sha), f"commit {sha[:8]} message")
        return report(probs, HINT_CI)
    if op == "scan":
        files = argv[1:] or git("ls-files").split()
        probs, private = [], private_patterns()
        for f in files:
            try: text = (ROOT / f).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError): continue
            probs += check_lines([(f, i, l) for i, l in enumerate(text.splitlines(), 1)], private)
        return report(probs, "(scan: report only)")
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
