"""Forge's git helper against a local 'remote': own branch only, tests must pass, never main, the token never shows."""
import importlib, os, pathlib, subprocess, sys, tempfile, unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


class ForgeGit(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        seed, self.remote = self.tmp / "seed", self.tmp / "remote.git"
        (seed / "tests").mkdir(parents=True)
        (seed / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test(self): self.assertTrue(True)\n")
        sh("git", "init", "-q", "-b", "main", str(seed))
        sh("git", "-C", str(seed), "add", "-A")
        sh("git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
        sh("git", "clone", "-q", "--bare", str(seed), str(self.remote))
        env = {"FORGE_WORK_DIR": str(self.tmp / "work"), "FORGE_REMOTE_URL": str(self.remote), "GITHUB_TOKEN": "ghp_secretvalue"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start(); self.addCleanup(patcher.stop)
        import forge_git
        self.fg = importlib.reload(forge_git)
        self.work = self.tmp / "work"

    def test_start_makes_a_forge_branch_from_main(self):
        self.fg.start("ecom-tab")
        self.assertEqual(self.fg.branch(), "forge/ecom-tab")

    def test_bad_names_and_main_are_refused(self):
        with self.assertRaises(SystemExit): self.fg.start("Main")
        with self.assertRaises(SystemExit): self.fg.start("../x")
        self.fg.start("ok-name")
        sh("git", "checkout", "-q", "main", cwd=self.work)
        (self.work / "a.txt").write_text("x")
        with self.assertRaises(SystemExit) as cm: self.fg.commit("on main")
        self.assertIn("forge/", str(cm.exception))

    def test_failing_tests_block_the_commit(self):
        self.fg.start("broken")
        (self.work / "tests" / "test_bad.py").write_text("import unittest\nclass B(unittest.TestCase):\n    def test(self): self.fail('no')\n")
        with self.assertRaises(SystemExit) as cm: self.fg.commit("bad")
        self.assertIn("tests do not pass", str(cm.exception))
        self.assertTrue(self.fg.git("status", "--porcelain"))  # nothing was committed

    def test_secret_looking_file_is_refused(self):
        self.fg.start("secrets")
        (self.work / ".env").write_text("A=1\n")
        with self.assertRaises(SystemExit) as cm: self.fg.commit("oops")
        self.assertIn("secret", str(cm.exception))

    def test_commit_then_pr_pushes_only_the_branch(self):
        self.fg.start("feature")
        (self.work / "new.txt").write_text("hello")
        self.assertIn("tests green", self.fg.commit("add file"))
        with mock.patch.object(self.fg, "api", return_value={"html_url": "https://github.com/x/y/pull/9"}) as api:
            url = self.fg.pr("Title", "Body")
        self.assertEqual(url, "https://github.com/x/y/pull/9")
        body = api.call_args.args[2]
        self.assertEqual((body["head"], body["base"]), ("forge/feature", "main"))
        heads = subprocess.run(["git", "-C", str(self.remote), "branch", "--list"], capture_output=True, text=True).stdout
        self.assertIn("forge/feature", heads)
        main_log = subprocess.run(["git", "-C", str(self.remote), "log", "--oneline", "main"], capture_output=True, text=True).stdout
        self.assertEqual(len(main_log.strip().splitlines()), 1)  # main untouched

    def test_pr_without_news_is_refused(self):
        self.fg.start("empty")
        with self.assertRaises(SystemExit): self.fg.pr("T", "B")

    def test_token_never_in_an_error(self):
        self.fg.start("tt")
        with mock.patch.dict(os.environ, {"FORGE_REMOTE_URL": "https://example.invalid/none.git"}):
            fg = importlib.reload(self.fg)
            fg.git("remote", "set-url", "origin", "https://example.invalid/none.git")
            with self.assertRaises(SystemExit) as cm: fg.git("fetch", "origin", "main")
        self.assertNotIn("ghp_secretvalue", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
