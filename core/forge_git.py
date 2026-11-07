"""Forge's only road to the code: its own branch, tests green, a pull request. Never main, never a merge: the owner merges.

  python3 core/forge_git.py start NAME           fresh clone/branch forge/NAME from the newest main in forge-work/
  python3 core/forge_git.py commit "message"     runs the guard and the tests, then commits everything in forge-work/
  python3 core/forge_git.py pr "title" "body"    guards title, body and the branch's commits, pushes the branch, opens the PR
  python3 core/forge_git.py status               branch and changed files

Forge edits files under forge-work/ (a clone kept on the host, so it survives a rebuild). The token (GITHUB_TOKEN, a fine-grained
token for this one repo: contents + pull requests) is read from .env, handed to git through the environment and never printed.
Business data never leaves through here: tools/guard.py checks the added lines, the commit message, the PR title and body, with
the generic patterns plus the owner's own words (config/private_patterns.txt, gitignored, here and in forge-work/config/)."""
import os, re, sys, subprocess, pathlib, json, base64, shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "tools"))
import env; env.load()
import guard

WORK = pathlib.Path(os.getenv("FORGE_WORK_DIR") or ROOT / "forge-work")
REPO = os.getenv("FORGE_REPO", "barny-gif/Jinnee-os")
REMOTE = os.getenv("FORGE_REMOTE_URL") or f"https://github.com/{REPO}.git"
PREFIX = "forge/"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")


class Refused(SystemExit):
    pass


def refuse(text):
    raise Refused(f"REFUSED: {text}")


def git(*args, check=True):
    token = os.getenv("GITHUB_TOKEN", "").strip()
    e = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token and REMOTE.startswith("https://"):  # through the environment, so it appears in no command line or file
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        e.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraheader", GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    r = subprocess.run(["git", *args], cwd=WORK if WORK.exists() else ROOT, env=e, capture_output=True, text=True)
    if check and r.returncode:
        text = (r.stderr or r.stdout).strip()[:300]
        refuse(f"git {args[0]} failed: {text.replace(token, '***') if token else text}")
    return r.stdout.strip()


def branch():
    return git("rev-parse", "--abbrev-ref", "HEAD") if (WORK / ".git").exists() else ""


def on_forge_branch():
    b = branch()
    if not b.startswith(PREFIX): refuse(f"you are on '{b or 'no branch'}': work only on a forge/ branch (start NAME first)")
    return b


def start(name):
    if not NAME.fullmatch(name or ""): refuse("the name is lowercase letters, digits and dashes, 2 to 41 characters")
    if not (WORK / ".git").exists():
        WORK.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", REMOTE, str(WORK)], check=False, capture_output=True, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        if not (WORK / ".git").exists():  # a private repo needs the token
            WORK.mkdir(parents=True, exist_ok=True)
            git("init", "--quiet"); git("remote", "add", "origin", REMOTE)
    if git("status", "--porcelain"): refuse("forge-work has changes that were not committed: commit or ask the owner before starting a new branch")
    git("fetch", "--quiet", "origin", "main")
    git("checkout", "--quiet", "-B", PREFIX + name, "origin/main")
    git("config", "core.hooksPath", ".githooks")  # the guard's pre-commit and commit-msg hooks, in this clone too
    own, theirs = ROOT / "config" / "private_patterns.txt", WORK / "config" / "private_patterns.txt"
    ignored = subprocess.run(["git", "check-ignore", "-q", "config/private_patterns.txt"], cwd=WORK, capture_output=True).returncode == 0
    if ignored and own.is_file() and own.resolve() != theirs.resolve() and (not theirs.is_file() or theirs.read_bytes() != own.read_bytes()):
        theirs.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(own, theirs)  # only where git ignores it
    return f"branch {PREFIX}{name} from the newest main, in {WORK}"


def private_words():
    """The owner's own words from config/private_patterns.txt, in the running copy and in forge-work (both gitignored)."""
    out, seen = [], set()
    for p in (ROOT / "config" / "private_patterns.txt", WORK / "config" / "private_patterns.txt"):
        if p.is_file() and p.resolve() not in seen:
            seen.add(p.resolve()); out += guard.private_patterns(p)
    return out


def guarded(problems, what):
    if problems:
        refuse(f"business data in {what} (the repo is public and may be sold): " + "; ".join(problems[:10])
               + ". Use made-up values (example-shop.hu, #1001, 1 234 Ft) and describe the case without its real numbers.")


def text_problems(text, where, private):
    return [f"{where}: {what} ({shown})" for what, shown in guard.findings(text, message=True, private=private)]


def run_tests():
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "tests"], cwd=WORK, capture_output=True, text=True)
    tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
    return r.returncode == 0, "\n".join(tail)


def commit(message):
    on_forge_branch()
    if not message.strip(): refuse("a commit needs a message")
    if not git("status", "--porcelain"): refuse("nothing changed")
    if any(re.search(r"(^|/)\.env($|\.)|secret|token|\.pem$", f, re.I) and not f.endswith(".example")
           for f in (line[3:] for line in git("status", "--porcelain").splitlines())):
        refuse("a file that looks like a secret is among the changes: remove it first")
    private = private_words()
    git("add", "-A")
    try: added = guard.added_lines(git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff"))
    finally: git("reset", "--quiet")
    guarded(guard.check_lines(added, private) + text_problems(message, "commit message", private), "the commit")
    ok, tail = run_tests()
    if not ok: refuse(f"the tests do not pass, nothing was committed:\n{tail}")
    git("add", "-A")
    git("-c", "user.name=Forge", "-c", "user.email=forge@jinnee-os.local", "commit", "--quiet", "-m", message)
    return f"committed, tests green ({tail.splitlines()[-1] if tail else ''})"


def api(method, path, body):
    import requests
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token: refuse("GITHUB_TOKEN is not set in .env")
    r = requests.request(method, f"https://api.github.com{path}", json=body, timeout=30,
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    if r.status_code >= 300: refuse(f"GitHub answered {r.status_code}: {r.text[:200].replace(token, '***')}")
    return r.json()


def pr(title, body):
    b = on_forge_branch()
    if git("status", "--porcelain"): refuse("there are uncommitted changes: commit first")
    if git("rev-list", "--count", "origin/main..HEAD", check=False) in ("", "0"): refuse("this branch has nothing new compared with main")
    private = private_words()
    probs = text_problems(title, "PR title", private) + text_problems(body, "PR body", private)
    for sha in git("rev-list", "origin/main..HEAD").split():
        probs += text_problems(git("log", "-1", "--format=%B", sha), f"commit {sha[:8]} message", private)
    probs += guard.check_lines(guard.added_lines(git("diff", "-U0", "--no-color", "--no-ext-diff", "origin/main...HEAD")), private)
    guarded(probs, "the pull request")  # nothing is pushed
    git("push", "--quiet", "origin", f"refs/heads/{b}:refs/heads/{b}")  # only this branch, never main
    out = api("POST", f"/repos/{REPO}/pulls", {"title": title, "head": b, "base": "main", "body": body + "\n\n(Forge, via core/forge_git.py)"})
    return out["html_url"]


def status():
    return f"{branch() or 'no clone yet'}\n{git('status', '--short') if (WORK / '.git').exists() else ''}".strip()


COMMANDS = {"start": (start, 1), "commit": (commit, 1), "pr": (pr, 2), "status": (status, 0)}

if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd not in COMMANDS or len(args) != COMMANDS[cmd][1]:
        raise SystemExit(__doc__)
    print(COMMANDS[cmd][0](*args))
