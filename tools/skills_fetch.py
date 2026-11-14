"""The skills that are not kept in this repo, fetched at a pinned upstream commit, and the Impeccable engine warmed up.

    python3 tools/skills_fetch.py sync     fetch what is missing or out of date, then download the Impeccable engine
    python3 tools/skills_fetch.py check    what is there and what is missing (exit 1 if something is missing)

Why not simply in git: the taste skill carries about 27 MB of photographs under their own licences (THIRD_PARTY.md).
The pinned commit is the integrity check: git verifies every object against the commit hash, and the fetched HEAD must
equal the pinned hash, or nothing is installed. An update is a new hash here, in a pull request; never "latest".

Every step that needs the network can fail without harm: the bridge works without these skills, and `sync` can be run
again later. Exit code 0 when everything is in place, 1 when something is still missing.
Standard library only; needs git (the installer already requires it)."""
import os, pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
STAMP = ".fetched"  # inside the skill folder: the commit it was fetched at

# name → where it comes from. "invoke_by_name": the SKILL.md gets `disable-model-invocation: true`, so the agents never
# start it on their own (the taste skill bans purple and deals a random palette: it would fight the shop's own brand).
FETCHED = {
    "design-taste-frontend": {
        "repo": "https://github.com/Leonxlnx/taste-skill",
        "commit": "f6b76575f949707b4e3c37cd86cb44b0b98643e8",
        "path": "skills/taste-skill",
        "license": "LICENSE",
        "invoke_by_name": True,
    },
}
IMPECCABLE = SKILLS / "impeccable" / "scripts" / "impeccable"
NO_TELEMETRY = {"DO_NOT_TRACK": "1", "IMPECCABLE_NO_TELEMETRY": "1"}


class FetchError(Exception):
    pass


def git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        raise FetchError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout.strip()


def by_name_only(skill_md: pathlib.Path):
    """Adds `disable-model-invocation: true` to the SKILL.md front matter, right after its name line (once)."""
    lines = skill_md.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0].strip() != "---":
        raise FetchError(f"{skill_md}: no front matter")
    try: end = lines.index("---", 1)
    except ValueError: raise FetchError(f"{skill_md}: the front matter does not end")
    head = lines[1:end]
    if any(l.startswith("disable-model-invocation:") for l in head):
        head = [("disable-model-invocation: true" if l.startswith("disable-model-invocation:") else l) for l in head]
    else:
        at = next((i for i, l in enumerate(head) if l.startswith("name:")), None)
        if at is None: raise FetchError(f"{skill_md}: no name in the front matter")
        head.insert(at + 1, "disable-model-invocation: true")
    skill_md.write_text("\n".join(["---", *head, *lines[end:]]), encoding="utf-8")


def current(name) -> str:
    """The commit the installed copy was fetched at; "" when it is missing or incomplete."""
    d = SKILLS / name
    try: stamp = (d / STAMP).read_text(encoding="utf-8").strip()
    except OSError: return ""
    return stamp if (d / "SKILL.md").is_file() else ""


def fetch(name, spec=None) -> str:
    """Installs one skill at its pinned commit. → what happened, in one line. Raises FetchError; the old copy stays then."""
    spec = spec or FETCHED[name]
    if current(name) == spec["commit"]: return f"{name}: up to date ({spec['commit'][:12]})"
    SKILLS.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix=f".fetch-{name}-", dir=SKILLS))  # same disk as the target: the swap is a rename
    try:
        repo = work / "repo"; repo.mkdir()
        git("init", "-q", cwd=repo)
        git("fetch", "-q", "--depth", "1", spec["repo"], spec["commit"], cwd=repo)
        got = git("rev-parse", "FETCH_HEAD", cwd=repo)
        if got != spec["commit"]:
            raise FetchError(f"{name}: upstream gave {got[:12]}, the pinned commit is {spec['commit'][:12]}; nothing installed")
        git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=repo)
        src = repo / spec["path"]
        if not (src / "SKILL.md").is_file():
            raise FetchError(f"{name}: no SKILL.md at {spec['path']} in {spec['commit'][:12]}")
        new = work / name
        shutil.copytree(src, new, ignore=shutil.ignore_patterns(".git"))
        if spec.get("license"): shutil.copy(repo / spec["license"], new / "LICENSE")
        if spec.get("invoke_by_name"): by_name_only(new / "SKILL.md")
        (new / STAMP).write_text(spec["commit"] + "\n", encoding="utf-8")
        target = SKILLS / name
        if target.exists(): target.rename(work / "old")
        new.rename(target)
        return f"{name}: installed at {spec['commit'][:12]} from {spec['repo']}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def engine(launcher=IMPECCABLE) -> str:
    """Downloads the Impeccable engine through its own launcher (sha256 against engine-checksums.txt), telemetry off.
    → the engine's version line. Raises FetchError."""
    if not pathlib.Path(launcher).is_file(): raise FetchError("impeccable: the skill is not in this copy")
    try:
        r = subprocess.run(["sh", str(launcher), "engine-probe"], capture_output=True, text=True, timeout=600,
                           env={**os.environ, **NO_TELEMETRY}, cwd=ROOT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FetchError(f"impeccable engine: {e}")
    if r.returncode != 0 or not r.stdout.startswith("impeccable-engine"):
        raise FetchError(f"impeccable engine: not available ({(r.stderr or r.stdout).strip()[-300:]})")
    return r.stdout.strip()


def check() -> list:
    """→ [(what, ok, detail)] for everything this file looks after."""
    out = []
    for name, spec in FETCHED.items():
        have = current(name)
        out.append((name, have == spec["commit"], f"at {have[:12]}" if have else "missing"))
    out.append(("impeccable", (SKILLS / "impeccable" / "SKILL.md").is_file(), "in the repo"))
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    op = argv[0] if argv else ""
    if op == "sync":
        ok = True
        for name in FETCHED:
            try: print(fetch(name))
            except (FetchError, OSError) as e: ok = False; print(f"{name}: NOT installed: {e}", file=sys.stderr)
        try: print(engine())
        except FetchError as e: ok = False; print(f"{e}. It is downloaded on first use instead.", file=sys.stderr)
        return 0 if ok else 1
    if op == "check":
        rows = check()
        for what, ok, detail in rows: print(f"{'ok' if ok else 'MISSING'}  {what} ({detail})")
        return 0 if all(ok for _, ok, _ in rows) else 1
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
