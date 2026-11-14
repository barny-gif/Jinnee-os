"""tools/skills_fetch.py: a skill is installed only at its pinned commit, gets the by-name-only line, and a failed fetch
leaves the old copy alone. The "upstream" is a local git repository, so nothing is downloaded.
Run: python3 -m unittest discover tests"""
import os, pathlib, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import skills_fetch as sf

SKILL = "---\nname: demo-skill\ndescription: a demo\n---\n\n# Demo\n"


def run(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


@unittest.skipUnless(shutil.which("git"), "needs git")
class Fetch(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp)
        self.up, skills = tmp / "upstream", tmp / "skills"
        (self.up / "skills" / "demo").mkdir(parents=True)
        (self.up / "skills" / "demo" / "SKILL.md").write_text(SKILL)
        (self.up / "skills" / "demo" / "assets").mkdir()
        (self.up / "skills" / "demo" / "assets" / "a.txt").write_text("asset")
        (self.up / "LICENSE").write_text("MIT License\n")
        run("init", "-q", cwd=self.up); run("add", "-A", cwd=self.up); run("commit", "-q", "-m", "one", cwd=self.up)
        self.first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.up, capture_output=True, text=True).stdout.strip()
        # fetching a commit by its hash from a local repository: allowed like GitHub allows it
        run("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.up)
        self.spec = {"repo": str(self.up), "commit": self.first, "path": "skills/demo", "license": "LICENSE", "invoke_by_name": True}
        p = mock.patch.object(sf, "SKILLS", skills); p.start(); self.addCleanup(p.stop)
        self.skills = skills

    def test_installs_at_the_pinned_commit_by_name_only(self):
        self.assertIn("installed", sf.fetch("demo-skill", self.spec))
        d = self.skills / "demo-skill"
        text = (d / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: demo-skill\ndisable-model-invocation: true\ndescription: a demo\n---\n"))
        self.assertEqual((d / "LICENSE").read_text(), "MIT License\n")
        self.assertEqual((d / "assets" / "a.txt").read_text(), "asset")
        self.assertEqual(sf.current("demo-skill"), self.first)
        self.assertIn("up to date", sf.fetch("demo-skill", self.spec))  # the second run does nothing
        self.assertEqual([p.name for p in self.skills.iterdir()], ["demo-skill"])  # no work folder left behind

    def test_a_newer_upstream_is_not_taken(self):
        sf.fetch("demo-skill", self.spec)
        (self.up / "skills" / "demo" / "SKILL.md").write_text(SKILL + "changed upstream\n")
        run("commit", "-q", "-am", "two", cwd=self.up)
        self.assertIn("up to date", sf.fetch("demo-skill", self.spec))
        self.assertNotIn("changed upstream", (self.skills / "demo-skill" / "SKILL.md").read_text())

    def test_a_failed_fetch_keeps_the_old_copy(self):
        sf.fetch("demo-skill", self.spec)
        bad = {**self.spec, "commit": "0" * 40}
        with self.assertRaises(sf.FetchError): sf.fetch("demo-skill", bad)
        self.assertEqual(sf.current("demo-skill"), self.first)
        with self.assertRaises(sf.FetchError): sf.fetch("other", {**self.spec, "path": "nowhere"})  # no SKILL.md there
        self.assertEqual([p.name for p in self.skills.iterdir()], ["demo-skill"])

    def test_by_name_only_replaces_an_existing_flag(self):
        f = self.skills / "x.md"; self.skills.mkdir(parents=True)
        f.write_text("---\nname: x\ndisable-model-invocation: false\n---\nbody\n")
        sf.by_name_only(f)
        self.assertEqual(f.read_text(), "---\nname: x\ndisable-model-invocation: true\n---\nbody\n")
        f.write_text("no front matter\n")
        with self.assertRaises(sf.FetchError): sf.by_name_only(f)


class Pinned(unittest.TestCase):
    def test_every_fetched_skill_is_pinned_to_a_full_commit_and_listed(self):
        third = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for name, spec in sf.FETCHED.items():
            with self.subTest(name=name):
                self.assertRegex(spec["commit"], r"^[0-9a-f]{40}$")
                self.assertTrue(spec["repo"].startswith("https://github.com/"))
                self.assertIn(spec["commit"], third)
                self.assertIn(f".claude/skills/{name}/", ignored)

    def test_vendored_skills_carry_their_licence(self):
        for name in ("impeccable", "awesome-design-md", "emails"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / ".claude" / "skills" / name / "LICENSE").is_file())
                self.assertIn(name, (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / ".claude" / "skills" / "impeccable" / "NOTICE.md").is_file())  # Apache-2.0
        self.assertFalse((ROOT / ".claude" / "skills" / "impeccable" / "scripts" / "bin").exists() and
                         subprocess.run(["git", "ls-files", ".claude/skills/impeccable/scripts/bin"], cwd=ROOT,
                                        capture_output=True, text=True).stdout.strip())  # the engine binary is never committed


if __name__ == "__main__":
    unittest.main()
