"""tools/visual_check.py: the parts that need no browser always; one real run when Chromium can start on this machine
(skipped otherwise, with the reason). Run: python3 -m unittest discover tests"""
import argparse, io, os, pathlib, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stderr
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import visual_check as vc

PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{margin:0} .wide{width:1200px;height:20px;background:#2F6B3A}</style></head>
<body><p>Hand-made puzzles.</p><div class="wide"></div></body></html>"""


def browser_starts():
    try:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "visual_check.py"), "--self-check"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


class Parts(unittest.TestCase):
    def test_sizes_and_labels(self):
        self.assertEqual(vc.parse_sizes("390x844, 1440X900"), [(390, 844), (1440, 900)])
        with self.assertRaises(argparse.ArgumentTypeError): vc.parse_sizes("wide")
        self.assertEqual([vc.size_label(w) for w in (390, 768, 1440)], ["mobile", "tablet", "desktop"])

    def test_targets(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        (d / "landing page.html").write_text(PAGE)
        url, local, name = vc.to_url(str(d / "landing page.html"))
        self.assertEqual((url, local, name), ("file://" + str(d / "landing page.html"), str(d / "landing page.html"), "landing page"))
        self.assertEqual(vc.safe_name(name), "landing_page")
        self.assertEqual(vc.to_url("https://example-shop.hu/pages/sale")[2], "example-shop.hu/pages/sale")
        with self.assertRaises(FileNotFoundError): vc.to_url(str(d / "missing.html"))

    def test_report_marks_overflow_and_console_errors(self):
        args = argparse.Namespace(dark=False, light=False, wait=0, impeccable=True)
        res = {"sizes": [{"tag": "390x844", "label": "mobile", "png": "/tmp/x.png", "overflow": True, "docW": 1200, "vw": 390,
                          "offenders": [{"sel": "div.wide", "right": 1200, "width": 1200}], "text_len": 10, "note": ""}],
               "console": [{"size": "390x844", "text": "Uncaught: boom", "where": ""}], "failed": []}
        text, ok = vc.build_report("p.html", "file:///p.html", res, [{"antipattern": "gradient-text", "severity": "warning"}], None, args)
        self.assertFalse(ok)
        for part in ("Result: PROBLEM", "OVERFLOW, the page is 1200 px wide on a 390 px screen", "`div.wide`",
                     "Uncaught: boom", "gradient-text 1"):
            self.assertIn(part, text)
        res["sizes"][0]["overflow"], res["console"] = False, []
        self.assertTrue(vc.build_report("p.html", "u", res, [], None, args)[1])

    def test_no_playwright_says_how_to_install(self):
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}), redirect_stderr(err):
            with self.assertRaises(SystemExit) as e: vc.main(["x.html"])
        self.assertEqual(e.exception.code, 2)
        self.assertIn("python3 -m playwright install chromium", err.getvalue())

    def test_impeccable_runs_with_telemetry_off(self):
        with mock.patch.object(vc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "[]", "")) as run:
            self.assertEqual(vc.run_impeccable("p.html"), ([], None))
        env = run.call_args.kwargs["env"]
        self.assertEqual((env["DO_NOT_TRACK"], env["IMPECCABLE_NO_TELEMETRY"]), ("1", "1"))
        self.assertEqual(run.call_args.args[0][:2], [vc.IMPECCABLE, "detect"])

    def test_default_output_is_in_handoffs(self):
        self.assertIn('default=os.path.join(ROOT, "handoffs", "visual")', pathlib.Path(vc.__file__).read_text())  # gitignored


@unittest.skipUnless(browser_starts(), "Chromium does not start here (python3 tools/visual_check.py --self-check says why)")
class RealBrowser(unittest.TestCase):
    def test_three_screenshots_and_the_overflow(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        (d / "page.html").write_text(PAGE)
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "visual_check.py"), str(d / "page.html"), "--out", str(d / "out")],
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # the 1200 px element overflows on the phone and the tablet
        (run,) = list((d / "out").iterdir())
        self.assertEqual(sorted(p.name for p in run.iterdir()),
                         ["page_desktop_1440x900.png", "page_mobile_390x844.png", "page_tablet_768x1024.png", "report.md"])
        report = (run / "report.md").read_text()
        self.assertIn("mobile 390x844", report); self.assertIn("desktop 1440x900", report)
        self.assertIn("1200 px wide on a 390 px screen", report)


if __name__ == "__main__":
    unittest.main()
