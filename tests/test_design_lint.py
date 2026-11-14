"""tools/design_lint.py and the shop's design file: the general checks always run, the brand checks only once
brain/brand/DESIGN.md has tokens, and --init puts the template there. Run: python3 -m unittest discover tests"""
import io, os, pathlib, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import design_lint as dl

PAGE = """<!doctype html><html><head><style>
h1 { background: linear-gradient(90deg, #7C3AED, #2563EB); -webkit-background-clip: text; background-clip: text; }
.card { color: #123456; border-radius: 13px; font-family: Comic Neue, sans-serif; }
</style></head><body>
<h1>Unlock the power of wooden toys</h1>
<button><span class="icon">🚀</span> Buy</button>
<p>Trusted by parents</p>
</body></html>
"""
FILLED = """# DESIGN.md
```design-lint
name: example-shop
colors: #FFFFFF #1A1A1A #2F6B3A
fonts: Fraunces, Work Sans
radius: 4px 8px 999px
```
"""


class Lint(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.d)
        (self.d / "page.html").write_text(PAGE, encoding="utf-8")

    def run_lint(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf): code = dl.main([str(self.d / "page.html"), *args])
        self.assertEqual(code, 0)  # always 0: a signal, not a verdict
        return buf.getvalue()

    def rules(self, design):
        return {f[2] for f in dl.lint_file(str(self.d / "page.html"), design).findings}

    def test_template_runs_the_general_checks_only(self):
        design = dl.Design(str(ROOT / "config" / "DESIGN.example.md"))
        self.assertEqual((design.colors, design.fonts, design.radius), (set(), set(), set()))
        self.assertIn("not filled in yet", design.note)
        found = self.rules(design)
        self.assertTrue({"AS-01", "AS-02", "AS-05", "AS-08", "AS-09"} <= found, found)
        self.assertFalse({"AS-17", "LEGACY"} & found)  # no token, no off-token guess
        self.assertNotIn("font outside the tokens", self.run_lint("--design", str(ROOT / "config" / "DESIGN.example.md")))

    def test_filled_tokens_add_the_brand_checks(self):
        (self.d / "DESIGN.md").write_text(FILLED, encoding="utf-8")
        design = dl.Design(str(self.d / "DESIGN.md"))
        self.assertEqual(design.note, "")
        out = self.run_lint("--design", str(self.d / "DESIGN.md"))
        self.assertIn("colour outside the tokens #123456", out)
        self.assertIn("font outside the tokens: comic neue", out)
        self.assertIn("radius off the scale 13px", out)

    def test_without_any_design_file(self):
        out = self.run_lint("--design", str(self.d / "missing.md"))
        self.assertIn("no such design file", out)
        found = self.rules(dl.Design(None))
        self.assertIn("AS-02", found)
        self.assertNotIn("AS-17", found)

    def test_default_is_the_shops_own_file_and_names_no_brand(self):
        self.assertEqual(pathlib.Path(dl.DEFAULT_DESIGN), ROOT / "brain" / "brand" / "DESIGN.md")
        src = (ROOT / "tools" / "design_lint.py").read_text(encoding="utf-8").lower()
        for word in ("esportsight", "jinnee-os/design", "bettor"):
            self.assertNotIn(word, src)


class Init(unittest.TestCase):
    def test_init_puts_the_template_in_brain_brand_once(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root)
        for d in ("core", "packs"): shutil.copytree(ROOT / d, root / d, ignore=shutil.ignore_patterns("__pycache__"))
        (root / "config").mkdir(); shutil.copy(ROOT / "config" / "DESIGN.example.md", root / "config" / "DESIGN.example.md")
        run = lambda: subprocess.run([sys.executable, "core/pack_loader.py", "--init"], cwd=root, capture_output=True, text=True,
                                     env={**os.environ, "PACKS": "general,ecom", "PYTHONDONTWRITEBYTECODE": "1"}, timeout=60)
        r = run(); self.assertEqual(r.returncode, 0, r.stderr)
        brand = root / "brain" / "brand" / "DESIGN.md"
        self.assertEqual(brand.read_text(), (ROOT / "config" / "DESIGN.example.md").read_text())
        brand.write_text("the owner's own\n")
        r = run(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(brand.read_text(), "the owner's own\n")  # never overwritten


if __name__ == "__main__":
    unittest.main()


class AgentFiles(unittest.TestCase):
    """What the agent files tell the team to run exists in this repo, and the skills they name are installed here."""

    def test_forge_runs_the_three_checks_that_exist(self):
        forge = (ROOT / "packs" / "ecom" / "agents" / "forge.md").read_text(encoding="utf-8")
        for cmd, path in (("python3 tools/design_lint.py", "tools/design_lint.py"),
                          ("python3 tools/visual_check.py", "tools/visual_check.py"),
                          (".claude/skills/impeccable/scripts/impeccable detect", ".claude/skills/impeccable/scripts/impeccable")):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, forge)
                self.assertTrue((ROOT / path).is_file())
        self.assertIn("Owner decides", forge)
        self.assertIn("brain/brand/DESIGN.md", forge)

    def test_email_writers_name_the_emails_skill_and_the_gate(self):
        for f in ("packs/ecom/agents/marketing-manager.md", "packs/general/agents/echo.md"):
            with self.subTest(f=f):
                text = (ROOT / f).read_text(encoding="utf-8")
                self.assertIn("`emails` skill", text)
                self.assertIn("send_customer_email", text)
        self.assertTrue((ROOT / ".claude" / "skills" / "emails" / "SKILL.md").is_file())
        pack = (ROOT / "packs" / "ecom" / "pack.json").read_text(encoding="utf-8")
        self.assertIn('"send_customer_email": { "level": 1, "max_level": 2, "locked": true', pack)

    def test_skills_the_agents_start_themselves_are_not_by_name_only(self):
        for name in ("impeccable", "emails", "awesome-design-md"):
            with self.subTest(name=name):
                head = (ROOT / ".claude" / "skills" / name / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
                self.assertIn(f"name: {name}", head)
                self.assertNotIn("disable-model-invocation: true", head)
