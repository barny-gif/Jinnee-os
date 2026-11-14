#!/usr/bin/env python3
"""visual_check.py: a visual test for a page, in a headless browser (Playwright + Chromium).

In plain words: it opens an HTML file or a web address in an invisible browser at three screen sizes (phone, tablet,
desktop), takes a full-page screenshot of each, and watches for:
  - console errors (JavaScript errors the browser reports)
  - failed network requests (a missing image, a font that does not load, a 404)
  - horizontal overflow (the page is wider than the screen, so on a phone you would have to scroll sideways)
With --impeccable it also runs the Impeccable design detector on the same file.
The screenshots are for looking at: the numbers do not see everything.

Setup (the installer does it): pip install -r requirements.txt, then python3 -m playwright install chromium
(on a bare Linux server also the browser's system libraries: sudo python3 -m playwright install-deps chromium).

Usage:
  python3 tools/visual_check.py <file|URL> [...]
      [--sizes 390x844,768x1024,1440x900]  # screen sizes (default: these three)
      [--wait 800]                          # extra wait in ms before the screenshot (animations)
      [--dark | --light]                    # prefers-color-scheme
      [--impeccable]                        # + Impeccable detect, summarised by rule
      [--out handoffs/visual]               # where the folders go (default: this)
  python3 tools/visual_check.py --self-check   # can the browser start here? prints its path

Output: handoffs/visual/<page>_<YYYYMMDD_HHMMSS>/
  <page>_mobile_390x844.png, ..._tablet_768x1024.png, ..._desktop_1440x900.png
  report.md (the same summary that is printed)

Exit code: 0 = no console error and no overflow on any page,
           1 = a console error or an overflow (fix it before the owner sees the page),
           2 = a usage or loading problem (no such file, the browser cannot start).
Failed requests and Impeccable findings are in the report but do not change the exit code by themselves. Note: Chromium
also logs a 404 resource as a console error ("Failed to load resource"), so a missing image or font usually shows as a
console error too, and gives exit 1.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse, unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SIZES = "390x844,768x1024,1440x900"
IMPECCABLE = os.path.join(ROOT, ".claude", "skills", "impeccable", "scripts", "impeccable")
NO_TELEMETRY = {"DO_NOT_TRACK": "1", "IMPECCABLE_NO_TELEMETRY": "1"}
PWTimeout = TimeoutError  # replaced by Playwright's own TimeoutError in load_playwright()
SETUP = "pip install -r requirements.txt, then: python3 -m playwright install chromium"


# Playwright starts Chromium without its sandbox unless asked (chromium_sandbox=False), so it runs as root in Docker too.
# /dev/shm is tiny in a container; sRGB keeps the screenshot colours equal to the brand hex values.
CHROMIUM_ARGS = ["--disable-dev-shm-usage", "--force-color-profile=srgb"]

# JS: measures the overflow and lists the elements that stick out
OVERFLOW_JS = r"""
() => {
  const vw = window.innerWidth;
  const de = document.documentElement;
  const docW = Math.max(de.scrollWidth, document.body ? document.body.scrollWidth : 0);
  const offenders = [];
  if (docW > vw + 1) {
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.right > vw + 1) {
        // only the outermost element that sticks out (skipped when its parent sticks out too)
        const p = el.parentElement;
        if (p && p !== document.body && p.getBoundingClientRect().right > vw + 1) continue;
        let sel = el.tagName.toLowerCase();
        if (el.id) sel += '#' + el.id;
        if (el.classList.length) sel += '.' + Array.from(el.classList).slice(0, 2).join('.');
        offenders.push({sel, right: Math.round(r.right), width: Math.round(r.width)});
        if (offenders.length >= 8) break;
      }
    }
  }
  return {vw, docW, offenders};
}
"""

# JS: is there any visible text at all (a quick sign of an empty page or invisible letters)
TEXT_JS = r"""
() => {
  const t = (document.body && document.body.innerText || '').trim();
  return t.length;
}
"""


def parse_sizes(s):
    out = []
    for part in s.split(","):
        part = part.strip().lower()
        m = re.fullmatch(r"(\d+)x(\d+)", part)
        if not m:
            raise argparse.ArgumentTypeError(f"not a size: {part!r} (e.g. 390x844)")
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def size_label(w):
    if w < 600:
        return "mobile"
    if w < 1100:
        return "tablet"
    return "desktop"


def to_url(target):
    """→ (url, local file or None, page name)."""
    p = urlparse(target)
    if p.scheme in ("http", "https"):
        slug = (p.netloc + p.path).strip("/") or p.netloc
        return target, None, slug
    if p.scheme == "file":
        path = unquote(p.path)
    else:
        path = os.path.abspath(target)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    name = os.path.splitext(os.path.basename(path))[0]
    return "file://" + path, path, name


def rel(p):
    """Relative to the repo when inside it; absolute otherwise."""
    p = os.path.abspath(p)
    return os.path.relpath(p, ROOT) if p.startswith(ROOT + os.sep) else p


def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")
    return s[:60] or "page"


def run_impeccable(target):
    env = {**os.environ, **NO_TELEMETRY}
    try:
        r = subprocess.run([IMPECCABLE, "detect", "--json", target], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        return None, f"Impeccable did not run: {e}"
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None, f"Impeccable output is not JSON (exit {r.returncode}): {r.stderr.strip()[:300]}"
    return data, None


def check_page(browser, url, sizes, args, outdir, name):
    res = {"sizes": [], "console": [], "failed": []}
    seen_console, seen_failed = set(), set()
    for (w, h) in sizes:
        ctx_kwargs = {"viewport": {"width": w, "height": h}}
        if args.dark:
            ctx_kwargs["color_scheme"] = "dark"
        elif args.light:
            ctx_kwargs["color_scheme"] = "light"
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        tag = f"{w}x{h}"

        def on_console(msg, tag=tag):
            if msg.type == "error":
                loc = msg.location or {}
                where = loc.get("url", "")
                if loc.get("lineNumber") is not None and where:
                    where += f":{loc.get('lineNumber')}"
                key = (msg.text, where)
                if key not in seen_console:
                    seen_console.add(key)
                    res["console"].append({"size": tag, "text": msg.text, "where": where})

        def on_pageerror(err, tag=tag):
            key = ("pageerror", str(err))
            if key not in seen_console:
                seen_console.add(key)
                res["console"].append({"size": tag, "text": f"Uncaught: {err}", "where": ""})

        def on_failed(req, tag=tag):
            key = (req.url, "failed")
            if key not in seen_failed:
                seen_failed.add(key)
                res["failed"].append({"size": tag, "url": req.url,
                                      "why": req.failure or "failed"})

        def on_response(resp, tag=tag):
            if resp.status >= 400:
                key = (resp.url, resp.status)
                if key not in seen_failed:
                    seen_failed.add(key)
                    res["failed"].append({"size": tag, "url": resp.url,
                                          "why": f"HTTP {resp.status}"})

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("requestfailed", on_failed)
        page.on("response", on_response)

        load_note = ""
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
        except PWTimeout:
            load_note = "the network did not go idle in 30 s; waited for 'load' instead"
            try:
                page.wait_for_load_state("load", timeout=15000)
            except PWTimeout:
                load_note = "the page did not finish loading in 45 s"
        try:
            page.evaluate("document.fonts && document.fonts.ready")
        except Exception:  # noqa: BLE001
            pass
        if args.wait:
            page.wait_for_timeout(args.wait)

        ov = page.evaluate(OVERFLOW_JS)
        text_len = page.evaluate(TEXT_JS)
        png = os.path.join(outdir, f"{name}_{size_label(w)}_{tag}.png")
        page.screenshot(path=png, full_page=True)
        res["sizes"].append({"tag": tag, "label": size_label(w), "png": png,
                             "overflow": ov["docW"] > ov["vw"] + 1, "docW": ov["docW"],
                             "vw": ov["vw"], "offenders": ov["offenders"],
                             "text_len": text_len, "note": load_note})
        ctx.close()
    return res


def build_report(target, url, res, imp, imp_err, args):
    L = []
    over = [s for s in res["sizes"] if s["overflow"]]
    ok = not res["console"] and not over
    L.append(f"# Visual check: {os.path.basename(target) or target}")
    L.append("")
    L.append(f"- Source: `{url}`")
    L.append(f"- Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    scheme = "dark" if args.dark else "light" if args.light else "default"
    L.append(f"- Colour scheme: {scheme}; extra wait: {args.wait} ms")
    L.append(f"- **Result: {'OK' if ok else 'PROBLEM'}** "
             f"(console errors: {len(res['console'])}, overflow: at {len(over)} sizes, "
             f"failed requests: {len(res['failed'])})")
    L.append("")
    L.append("## Screen sizes")
    for s in res["sizes"]:
        line = f"- {s['label']} {s['tag']}: `{rel(s['png'])}`"
        if s["overflow"]:
            line += f": OVERFLOW, the page is {s['docW']} px wide on a {s['vw']} px screen"
        else:
            line += ": no horizontal overflow"
        if s["text_len"] == 0:
            line += "; WARNING: no visible text in the page"
        if s["note"]:
            line += f" ({s['note']})"
        L.append(line)
        for o in s["offenders"]:
            L.append(f"    - sticks out: `{o['sel']}` (right edge {o['right']} px, width {o['width']} px)")
    L.append("")
    L.append("## Console errors")
    if res["console"]:
        for c in res["console"]:
            L.append(f"- [{c['size']}] {c['text']}" + (f"  ({c['where']})" if c["where"] else ""))
    else:
        L.append("- none")
    L.append("")
    L.append("## Failed network requests")
    if res["failed"]:
        for f in res["failed"]:
            L.append(f"- [{f['size']}] {f['why']}: {f['url']}")
    else:
        L.append("- none")
    if args.impeccable:
        L.append("")
        L.append("## Impeccable detect")
        if imp_err:
            L.append(f"- {imp_err}")
        else:
            cnt = Counter(i.get("antipattern", "?") for i in imp)
            adv = sum(1 for i in imp if i.get("advisory"))
            summary = ", ".join(f"{k} {v}" for k, v in cnt.most_common()) or "no findings"
            L.append(f"- {len(imp)} findings in total ({adv} advisory): {summary}")
            shown = Counter()
            for i in imp:
                k = i.get("antipattern", "?")
                shown[k] += 1
                if shown[k] <= 5:
                    L.append(f"    - [{i.get('severity')}] {k}: {i.get('snippet', '')}")
                elif shown[k] == 6:
                    L.append(f"    - ... {k}: {cnt[k] - 5} more like it (full list: "
                             f"impeccable detect --json)")
    return "\n".join(L) + "\n", ok


def human_summary(name, res, imp, imp_err, args, outdir):
    over = [s for s in res["sizes"] if s["overflow"]]
    parts = [f"{name}: {len(res['sizes'])} screenshots -> {rel(outdir)}/"]
    parts.append(f"  Console errors: {len(res['console'])}"
                 + ("" if not res["console"] else f" (e.g. {res['console'][0]['text'][:100]})"))
    if over:
        parts.append("  Overflow: " + "; ".join(
            f"{s['label']} {s['tag']} ({s['docW']} px > {s['vw']} px)" for s in over))
    else:
        parts.append("  Overflow: none, no size needs sideways scrolling")
    parts.append(f"  Failed requests: {len(res['failed'])}")
    empty = [s["tag"] for s in res["sizes"] if s["text_len"] == 0]
    if empty:
        parts.append(f"  WARNING: no visible text at: {', '.join(empty)}")
    if args.impeccable:
        if imp_err:
            parts.append(f"  Impeccable: {imp_err}")
        else:
            cnt = Counter(i.get("antipattern", "?") for i in imp)
            parts.append(f"  Impeccable: {len(imp)} findings"
                         + (": " + ", ".join(f"{k} {v}" for k, v in cnt.most_common()) if cnt else ""))
    parts.append("  Look at the screenshots too (phone and desktop): the numbers do not see everything.")
    return "\n".join(parts)


def load_playwright():
    """→ sync_playwright, or a SystemExit(2) that says how to install it. Imported here, not at the top, so that the
    rest of this file (and its tests) works without Playwright, and a missing install gets a plain answer."""
    global PWTimeout
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as timeout_error
    except ImportError:
        print(f"ERROR: Playwright is not installed. {SETUP}", file=sys.stderr)
        raise SystemExit(2)
    PWTimeout = timeout_error
    return sync_playwright


def launch(pw):
    """The browser, or SystemExit(2) with the reason and the fix (a missing browser or missing system libraries)."""
    try:
        return pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
    except Exception as e:  # noqa: BLE001
        first = " ".join(str(e).split())[:400]
        print(f"ERROR: the browser does not start: {first}\n"
              f"Fix: python3 -m playwright install chromium (on a bare Linux server also: "
              f"sudo python3 -m playwright install-deps chromium)", file=sys.stderr)
        raise SystemExit(2)


def self_check():
    """Starts the browser once and opens a blank page. → 0 and the browser's path, or 2 with the fix."""
    sync_playwright = load_playwright()
    with sync_playwright() as pw:
        browser = launch(pw)
        page = browser.new_page()
        page.set_content("<p>ok</p>")
        ok = page.inner_text("p") == "ok"
        print(f"ok {pw.chromium.executable_path}" if ok else "ERROR: the browser started but did not render")
        browser.close()
    return 0 if ok else 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--self-check"]:
        return self_check()
    ap = argparse.ArgumentParser(description="visual test of a page (screenshots + console, network, overflow).")
    ap.add_argument("targets", nargs="+", help="HTML file(s) or http/https/file URL(s)")
    ap.add_argument("--sizes", type=parse_sizes, default=parse_sizes(DEFAULT_SIZES),
                    help=f"screen sizes, comma separated (default: {DEFAULT_SIZES})")
    ap.add_argument("--wait", type=int, default=0, help="extra wait in ms before the screenshot")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dark", action="store_true", help="prefers-color-scheme: dark")
    g.add_argument("--light", action="store_true", help="prefers-color-scheme: light")
    ap.add_argument("--impeccable", action="store_true", help="run Impeccable detect too")
    ap.add_argument("--out", default=os.path.join(ROOT, "handoffs", "visual"), help="the output folder")
    args = ap.parse_args(argv)

    sync_playwright = load_playwright()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    any_fail = False
    usage_err = False
    with sync_playwright() as pw:
        browser = launch(pw)
        for target in args.targets:
            try:
                url, local, name = to_url(target)
            except FileNotFoundError as e:
                print(f"ERROR: no such file: {e}", file=sys.stderr)
                usage_err = True
                continue
            name = safe_name(name)
            outdir = os.path.join(args.out, f"{name}_{stamp}")
            os.makedirs(outdir, exist_ok=True)
            try:
                res = check_page(browser, url, args.sizes, args, outdir, name)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR: {target} could not be loaded: {e}", file=sys.stderr)
                usage_err = True
                continue
            imp, imp_err = (None, None)
            if args.impeccable:
                imp, imp_err = run_impeccable(local or url)
            report, ok = build_report(target, url, res, imp, imp_err, args)
            with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as fh:
                fh.write(report)
            print(human_summary(name, res, imp, imp_err, args, outdir))
            print(f"  Report: {rel(os.path.join(outdir, 'report.md'))}")
            print()
            any_fail |= not ok
        browser.close()
    if any_fail:
        return 1
    return 2 if usage_err else 0


if __name__ == "__main__":
    sys.exit(main())
