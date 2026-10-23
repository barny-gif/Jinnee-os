"""tools/guard.py and the git hooks: business data is refused, made-up data passes. The "bad" samples are built from pieces
so this file itself never holds one. Run: python3 -m unittest discover tests"""
import os, pathlib, shutil, subprocess, sys, tempfile, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import guard

AT = "@"
PHONE = "+36 30 " + "987 6543"
MAIL = "info" + AT + "real-shop.hu"
HANDLE = "abcd" + "-ef" + ".myshopify.com"
ORDER = "#" + "1031"
MONEY = "16 370" + " Ft"


class Patterns(unittest.TestCase):
    def kinds(self, text, **kw):
        return [what for what, _ in guard.findings(text, **kw)]

    def test_refused(self):
        for text, what in ((f"call {PHONE}", "a phone number"), ("06-20-" + "555-1234", "a phone number"),
                           (f"write to {MAIL}", "an e-mail address"), (f"store {HANDLE}", "a real Shopify store handle"),
                           ("shpat_" + "a1" * 16, "a Shopify access token"), ("ghp_" + "A" * 36, "a GitHub token"),
                           ("123456789:" + "A" * 35, "a Telegram bot token")):
            with self.subTest(text=text): self.assertIn(what, self.kinds(text))

    def test_made_up_values_pass(self):
        for text in ("06 30 123 4567", "+36301112222", "06 30 111 2222", "x" + AT + "y.hu", "vevo" + AT + "example.hu",
                     "info" + AT + "example-shop.hu", "forge" + AT + "jinnee-os.local", "demo.myshopify.com", "ghp_secretvalue",
                     'store + ".myshopify.com"', "noreply" + AT + "anthropic.com"):
            with self.subTest(text=text): self.assertEqual(self.kinds(text), [])

    def test_order_numbers_and_amounts_only_in_messages(self):
        text = f"Order {ORDER[1:]}: the label said {MONEY}, {ORDER} (COD)"
        self.assertEqual(self.kinds(text), [])  # code and tests may hold made-up ones
        self.assertEqual(sorted(set(self.kinds(text, message=True))), ["a money amount", "an order number"])
        self.assertEqual(self.kinds("Merge pull request #34 from owner/forge/x; 12 tests", message=True), [])

    def test_found_text_is_masked(self):
        (what, shown), = guard.findings(MAIL)
        self.assertNotIn(MAIL, shown); self.assertTrue(shown.startswith("in") and shown.endswith("hu"))

    def test_private_patterns_text_and_regex(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        (d / "p.txt").write_text("# my words\nKis Bolt\nre:\\bKB-\\d+\n\n", encoding="utf-8")
        priv = guard.private_patterns(d / "p.txt")
        self.assertEqual(len(priv), 2)
        self.assertEqual(len(guard.findings("a kis bolt label, invoice KB-2026", private=priv)), 2)
        self.assertEqual(guard.private_patterns(d / "missing.txt"), [])
        (d / "bad.txt").write_text("re:(unclosed\n", encoding="utf-8")
        with self.assertRaises(SystemExit): guard.private_patterns(d / "bad.txt")

    def test_added_lines_only(self):
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old " + PHONE + "\n+new line\n"
                "@@ -10 +10,2 @@\n+mail " + MAIL + "\n+ok\n")
        self.assertEqual(guard.added_lines(diff), [("x.py", 1, "new line"), ("x.py", 10, "mail " + MAIL), ("x.py", 11, "ok")])
        self.assertEqual(guard.check_lines(guard.added_lines(diff)), ["x.py:10: an e-mail address (" + guard.mask(MAIL) + ")"])

    def test_the_repo_itself_is_clean(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        if not files: self.skipTest("not a git checkout")
        probs = []
        for f in files:
            try: text = (ROOT / f).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError): continue
            probs += guard.check_lines([(f, i, l) for i, l in enumerate(text.splitlines(), 1)])
        self.assertEqual(probs, [])


@unittest.skipUnless(shutil.which("git") and os.name == "posix", "needs git")
class Hooks(unittest.TestCase):
    """The real hooks in a throw-away repo."""
    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.repo)
        (self.repo / "tools").mkdir(); (self.repo / "config").mkdir()
        shutil.copy(ROOT / "tools" / "guard.py", self.repo / "tools" / "guard.py")
        shutil.copytree(ROOT / ".githooks", self.repo / ".githooks")
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t" + AT + "example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t" + AT + "example.com", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "-q"); self.git("config", "commit.gpgsign", "false")
        subprocess.run([sys.executable, "tools/guard.py", "install-hooks"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "config" / "private_patterns.txt").write_text("Kis Bolt\n", encoding="utf-8")

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, env=self.env, capture_output=True, text=True)

    def commit(self, name, text, msg):
        (self.repo / name).write_text(text, encoding="utf-8"); self.git("add", name)
        return self.git("commit", "-q", "-m", msg)

    def test_clean_commit_goes_through(self):
        r = self.commit("a.txt", "example-shop.hu #5001\n", "Label note from the settings")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_private_word_in_a_file_is_refused(self):
        r = self.commit("a.txt", "label: Kis Bolt\n", "Label note")
        self.assertNotEqual(r.returncode, 0); self.assertIn("private_patterns.txt line 1", r.stderr)
        self.assertEqual(self.git("rev-list", "--all").stdout, "")  # nothing was committed

    def test_order_number_in_the_message_is_refused(self):
        r = self.commit("a.txt", "fine\n", f"Fix the label of {ORDER}")
        self.assertNotEqual(r.returncode, 0); self.assertIn("an order number", r.stderr)

    def test_contact_data_is_refused_without_private_file(self):
        (self.repo / "config" / "private_patterns.txt").unlink()
        r = self.commit("a.txt", f"ring {PHONE}\n", "Add a note")
        self.assertNotEqual(r.returncode, 0); self.assertIn("a phone number", r.stderr)

    def test_range_checks_lines_and_messages(self):
        self.assertEqual(self.commit("a.txt", "one\n", "First").returncode, 0)
        base = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("config", "core.hooksPath", "/dev/null")  # as if someone skipped the hooks
        self.commit("b.txt", f"mail {MAIL}\n", f"Second, {MONEY}")
        r = subprocess.run([sys.executable, "tools/guard.py", "range", base, "HEAD"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("b.txt:1: an e-mail address", r.stderr); self.assertIn("message: a money amount", r.stderr)


if __name__ == "__main__":
    unittest.main()
