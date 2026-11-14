#!/usr/bin/env python3
"""design_lint.py: a local, offline linter for the "AI-made" look in HTML, CSS, TSX and JSX.

It only READS and MARKS. It changes nothing, deletes nothing and never touches the network. Standard library only.
Exit code is always 0: the score is a signal for a person, not a verdict.

Where the rules come from: the shop's own design file, `brain/brand/DESIGN.md` (made from config/DESIGN.example.md by
`core/pack_loader.py --init`, filled in during onboarding; brain/ is never in git).
  - the "Anti-AI-slop rules" table: the AS-xx ids the findings refer to
  - the ```design-lint``` block: colors / colors_legacy / fonts / fonts_legacy / radius / radius_legacy
While the block is still empty (the template), the brand checks (colours, fonts, radius scale) are skipped and the
report says so; the general checks (gradient text, glass, emoji icons, generic copy...) always run.

Usage:
  python3 tools/design_lint.py <file|folder> [...]
      [--design brain/brand/DESIGN.md]   # the rule set (default: this one, when it exists)
      [--md report.md]                   # a markdown report as well
      [--top 10]                         # the N heaviest findings at the end
      [--quiet]                          # file totals only, no finding lines

Scoring: HIGH = 5, MED = 3, LOW = 1 per finding; an aggregated finding counts weight * min(count, 10).
"score / 100 lines" compares files of different sizes. [review] items need a person's look.
"""

import argparse
import math
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DESIGN = os.path.join(ROOT, "brain", "brand", "DESIGN.md")
EXTS = (".html", ".htm", ".css", ".tsx", ".jsx", ".liquid")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build", ".output", ".vinxi"}
WEIGHT = {"HIGH": 5, "MED": 3, "LOW": 1}
CAP = 10

# --------------------------------------------------------------------------- design


class Design:
    """The brand tokens from a DESIGN.md. An empty set means "not filled in": that check is skipped, never guessed."""

    def __init__(self, path=None):
        self.path = path
        self.name = os.path.basename(os.path.dirname(path)) if path else "none"
        self.colors, self.colors_legacy = set(), set()
        self.fonts, self.fonts_legacy = set(), set()
        self.radius, self.radius_legacy = set(), set()
        self.note = ""
        if not path:
            self.note = "no design file: brand checks (colours, fonts, radius) skipped"
            return
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"```design-lint\s*\n(.*?)```", text, re.S)
        if not m:
            self.note = "no ```design-lint``` block in %s: brand checks skipped" % rel(path)
            return
        for line in m.group(1).splitlines():
            line = line.split("#", 1)[0] if not re.search(r"#[0-9A-Fa-f]{3}", line) else line
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip(), val.strip()
            if key == "name":
                self.name = val or self.name
            elif key in ("colors", "colors_legacy"):
                getattr(self, key).update(norm_hex(h) for h in re.findall(r"#[0-9A-Fa-f]{3,8}\b", val))
            elif key in ("fonts", "fonts_legacy"):
                getattr(self, key).update(f.strip().strip("\"'").lower() for f in val.split(",") if f.strip())
            elif key in ("radius", "radius_legacy"):
                getattr(self, key).update(v.strip().lower() for v in val.split())
        empty = [k for k in ("colors", "fonts", "radius") if not getattr(self, k)]
        if empty:
            self.note = "not filled in yet in %s: %s; %s skipped" % (
                rel(path), ", ".join(empty), "those checks are" if len(empty) > 1 else "that check is")


def norm_hex(h):
    h = h.lstrip("#").upper()
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    return "#" + h[:6]


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_hex(r, g, b):
    return "#%02X%02X%02X" % (int(r), int(g), int(b))


def hue_sat(rgb):
    r, g, b = [c / 255.0 for c in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0, 0.0, mx
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    s = d / mx if mx else 0
    return h * 60, s, mx


def is_purple(rgb):
    h, s, v = hue_sat(rgb)
    return 255 <= h <= 300 and s >= 0.35 and v >= 0.35


def is_blue(rgb):
    h, s, v = hue_sat(rgb)
    return 185 <= h < 245 and s >= 0.35 and v >= 0.35


def nearest(h, pool):
    best, bd = None, 1e9
    a = hex_rgb(h)
    for p in pool:
        b = hex_rgb(p)
        d = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
        if d < bd:
            best, bd = p, d
    return best, bd


# --------------------------------------------------------------------------- patterns

HEX_RE = re.compile(r"(?<![\w&])#([0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3,4})(?![\w-])")
RGB_RE = re.compile(r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})")
EMOJI_CHARS = (
    "\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF"
    "\U0001FA70-\U0001FAFF\U0001F1E6-\U0001F1FF☀-⛿✀-➿⭐⭕⏰-⏺"
)
# typographic marks, not emoji icons
EMOJI_ALLOW = set("✓✔✕✖✗✘★☆•✱")
EMOJI_RE = re.compile("[" + EMOJI_CHARS + "]️?")
EMOJI_LEAD_RE = re.compile(r">\s*(?:<[^>]+>\s*)*([" + EMOJI_CHARS + r"])")

BANNED_COPY = [
    "unlock the power", "unlock your", "unleash", "supercharge", "seamless", "seamlessly",
    "next-level", "next level", "elevate your", "revolutionize", "revolutionise", "game-changer",
    "game changer", "game-changing", "all-in-one", "effortless", "effortlessly", "built for the future",
    "cutting-edge", "cutting edge", "harness the", "empower", "transform your", "lightning-fast",
    "lightning fast", "10x your", "take it to the next", "at your fingertips", "like never before",
    "world-class", "state-of-the-art", "powered by ai", "ai-powered", "magic",
]
BANNED_COPY_RE = [(p, re.compile(r"(?<!\w)" + re.escape(p) + r"(?!\w)", re.I)) for p in BANNED_COPY]
FAKE_STAT_RE = [
    ("N+ users/customers", re.compile(
        r"\b\d[\d,.]*\s*(?:k|m|\+|k\+|m\+)\s+(?:users|customers|teams|businesses|subscribers|members|companies|buyers|orders)\b",
        re.I)),
    ("trusted by", re.compile(r"\btrusted by\b", re.I)),
    ("join thousands", re.compile(r"\bjoin (?:thousands|millions|hundreds)\b", re.I)),
    ("99.x% claim", re.compile(r"\b99(?:\.\d+)?\s?%")),
    ("as seen on / loved by", re.compile(r"\b(?:as seen (?:on|in)|loved by)\b", re.I)),
]

GRAD_TEXT_CSS = re.compile(r"\s*(?:-webkit-)?background-clip\s*:\s*text", re.I)
GRAD_TEXT_CLS = re.compile(r"\b(?:bg-clip-text|shimmer-text|text-gradient[\w-]*)\b")
SPARKLE_RE = re.compile(r"✨|\bSparkles?\b|\bsparkle[\w-]*|\bWandSparkles\b|\bmagic-wand\b")
BLUR_CLS = re.compile(r"\bbackdrop-blur(?:-[a-z0-9]+)?\b")
ROUNDED_CLS = re.compile(r"\brounded(?:-(?:none|sm|md|lg|xl|2xl|3xl|4xl|full|\[[^\]]+\]))?(?=[\s\"'`])")
GRID3_CSS = re.compile(r"grid-template-columns\s*:\s*(?:repeat\(\s*3\s*,\s*(?:1fr|minmax\(\s*0\s*,\s*1fr\s*\))\s*\)|1fr\s+1fr\s+1fr)\s*(?:;|$|})", re.I)
GRID3_CLS = re.compile(r"\b(?:[a-z]+:)?grid-cols-3\b")
GRAD_FILL_CLS = re.compile(r"\b(?:bg-gradient-to-[a-z]+|bg-linear-to-[a-z]+|bg-radial[\w-]*|gradient-primary|bg-gradient-brand)\b")
ANIM_CLS = re.compile(r"\banimate-(?:pulse|spin|ping|bounce|breathe|orbit|shimmer|marquee[\w-]*)\b")

# --------------------------------------------------------------------------- file splitting


def split_regions(path, text):
    """→ css_chunks [(line, css)], text_lines [(line, visible_text)], code_lines [(line, raw)]"""
    lines = text.splitlines()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".css":
        return [(1, text)], [], []
    css, vis, code = [], [], []
    if ext in (".tsx", ".jsx"):
        for i, ln in enumerate(lines, 1):
            s = ln.strip()
            if s.startswith(("import ", "//", "*", "/*")):
                continue
            code.append((i, ln))
            # JSX text: text between tags and string literals
            t = re.sub(r"<[^>]*>", " ", ln)
            t = re.sub(r"className=\{?[\"'`][^\"'`]*[\"'`]\}?", " ", t)
            vis.append((i, t))
        return css, vis, code
    # HTML (and Liquid, which is HTML with tags in it)
    state = "markup"
    buf, buf_start = [], 0
    for i, ln in enumerate(lines, 1):
        rest = ln
        while True:
            if state == "markup":
                m = re.search(r"<(style|script)\b[^>]*>", rest, re.I)
                if not m:
                    seg = rest
                    if seg.strip():
                        vis.append((i, strip_tags(seg)))
                        code.append((i, seg))
                        for sm in re.finditer(r"style\s*=\s*\"([^\"]*)\"|style\s*=\s*'([^']*)'", seg):
                            css.append((i, sm.group(1) or sm.group(2)))
                    break
                pre = rest[:m.start()]
                if pre.strip():
                    vis.append((i, strip_tags(pre)))
                    code.append((i, pre))
                state = m.group(1).lower()
                buf, buf_start = [], i
                rest = rest[m.end():]
            else:
                m = re.search(r"</%s\s*>" % state, rest, re.I)
                if not m:
                    buf.append(rest)
                    break
                buf.append(rest[:m.start()])
                chunk = "\n".join(buf)
                if state == "style":
                    css.append((buf_start, chunk))
                else:
                    for k, cl in enumerate(chunk.split("\n")):
                        code.append((buf_start + k, cl))
                        # an inline style in a JS template string counts too
                        for sm in re.finditer(r"style\s*=\s*\\?[\"']([^\"'\\]*)", cl):
                            css.append((buf_start + k, sm.group(1)))
                state = "markup"
                rest = rest[m.end():]
    return css, vis, code


def strip_tags(s):
    s = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", s)  # Liquid output and tags
    s = re.sub(r"<[^>]*>", " ", s)
    s = re.sub(r"^[^<]*>", " ", s)  # the end of a tag from the line before
    s = re.sub(r"<[^>]*$", " ", s)  # the start of a tag that goes on in the next line
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s)
    return s


def iter_decls(start_line, css):
    """CSS declarations (line, 'prop: value') without comments, multi-line values included."""
    css = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)
    line = start_line
    cur, cur_line = [], start_line
    started = False  # whether the current declaration has had a non-space character yet
    for ch in css:
        if ch in ";{}":
            d = "".join(cur).strip()
            if ":" in d and not d.startswith("@"):
                yield cur_line, " ".join(d.split())
            cur, started = [], False
            cur_line = line
            continue
        if ch == "\n":
            line += 1
            if not started:
                cur_line = line
            cur.append(" ")
            continue
        if not started and not ch.isspace():
            started = True
            cur_line = line
        cur.append(ch)
    d = "".join(cur).strip()
    if ":" in d:
        yield cur_line, " ".join(d.split())


def colors_in(s):
    out = []
    for m in HEX_RE.finditer(s):
        pre = s[max(0, m.start() - 6):m.start()].lower()
        if "href=" in pre or "url(" in pre:
            continue
        out.append(norm_hex(m.group(0)))
    for m in RGB_RE.finditer(s):
        r, g, b = (min(255, int(x)) for x in m.groups())
        out.append(rgb_hex(r, g, b))
    return out


# --------------------------------------------------------------------------- linting


def prefixed_once(decls, rx):
    """The -webkit- and the plain variant count as one finding (within 2 lines)."""
    std = [ln for ln, d in decls if rx.match(d) and not d.lower().startswith("-webkit-")]
    pre = [ln for ln, d in decls if rx.match(d) and d.lower().startswith("-webkit-")]
    out = sorted(set(std))
    for ln in pre:
        if not any(abs(ln - s) <= 2 for s in std):
            out.append(ln)
    return sorted(set(out))


class Report:
    def __init__(self, path, design, nlines):
        self.path, self.design, self.nlines = path, design, nlines
        self.findings = []  # (line, sev, rule, msg, score)

    def add(self, line, sev, rule, msg, count=1):
        if any(f[0] == line and f[2] == rule and f[3] == msg for f in self.findings):
            return  # same line + rule + message (e.g. the -webkit- and the plain variant on one line)
        score = WEIGHT[sev] * min(count, CAP)
        self.findings.append((line, sev, rule, msg, score))

    @property
    def score(self):
        return sum(f[4] for f in self.findings)


def lint_file(path, design):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        print("ERROR: %s: %s" % (path, e))
        return None
    lines = text.splitlines()
    if path.lower().endswith((".html", ".htm", ".liquid")):
        # HTML comments are not visible and do not count (line numbers are kept)
        text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    css_chunks, vis, code = split_regions(path, text)

    decls = []
    for start, chunk in css_chunks:
        decls.extend(iter_decls(start, chunk))

    all_colors = []
    for ln, d in decls:
        for c in colors_in(d):
            all_colors.append((ln, c, d))
    for ln, raw in code:
        for c in colors_in(raw):
            # the ones in a style attribute are already among the declarations
            if "style" in raw and any(ln == l2 and c == c2 for l2, c2, _ in all_colors):
                continue
            if re.search(r"(?:bg|text|border|from|to|via|fill|stroke|ring|shadow)-\[#", raw) or "'" + c.lower() in raw.lower() \
                    or '"' + c.lower() in raw.lower() or ": " in raw or ":#" in raw.replace(" ", ""):
                all_colors.append((ln, c, raw.strip()))

    rep = Report(path, design, len(lines))

    # AS-02 gradient text
    for ln in prefixed_once(decls, GRAD_TEXT_CSS):
        rep.add(ln, "HIGH", "AS-02", "gradient text (background-clip: text)")
    for ln, raw in code:
        m = GRAD_TEXT_CLS.search(raw)
        if m and not GRAD_TEXT_CSS.search(raw):
            rep.add(ln, "HIGH", "AS-02", "gradient/shimmer text class: %s" % m.group(0))

    # AS-03 glass / backdrop-blur
    blur_re = re.compile(r"(?:-webkit-)?backdrop-filter\s*:\s*(?!none)", re.I)
    dmap = {ln: d for ln, d in decls}
    blur_hits = [(ln, dmap.get(ln, "backdrop-filter")) for ln in prefixed_once(decls, blur_re)]
    for ln, raw in code:
        for m in BLUR_CLS.finditer(raw):
            blur_hits.append((ln, m.group(0)))
    for k, (ln, d) in enumerate(blur_hits):
        sev = "LOW" if k == 0 else "MED"
        rep.add(ln, sev, "AS-03", "backdrop blur #%d/%d%s: %s" % (
            k + 1, len(blur_hits), " (the first one: a sticky header may be fine)" if k == 0 else "", d[:60]))

    # AS-05 emoji as icon, AS-06 sparkle, AS-08 copy, AS-09 fake stat, AS-16 em-dash
    emdash_lines = []
    lead_by_line, iconcls_lines = defaultdict(list), set()
    for ln, raw in code:
        for m in EMOJI_LEAD_RE.finditer(raw):
            ch = m.group(1)
            if ch in EMOJI_ALLOW or ch == "✨":
                continue
            lead_by_line[ln].append(ch)
        if re.search(r"class(?:Name)?=[\"'][^\"']*\b(?:icon|ico|emoji)\b[^\"']*[\"'][^>]*>\s*[" + EMOJI_CHARS + "]", raw):
            iconcls_lines.add(ln)
    for ln in sorted(lead_by_line):
        # one finding per line (a line may hold several emoji icons, e.g. a list)
        rep.add(ln, "MED", "AS-05", "emoji as an icon%s: %s" % (
            " (in an icon-class element)" if ln in iconcls_lines else " (at the start of an element)",
            " ".join(dict.fromkeys(lead_by_line[ln]))))
    for ln, raw in code:
        if SPARKLE_RE.search(raw):
            rep.add(ln, "HIGH", "AS-06", "sparkle / AI star: %s" % SPARKLE_RE.search(raw).group(0))
    for ln, t in vis:
        for p, rx in BANNED_COPY_RE:
            if rx.search(t):
                rep.add(ln, "MED", "AS-08", "generic copy: \"%s\"" % p)
        for label, rx in FAKE_STAT_RE:
            if rx.search(t):
                rep.add(ln, "MED", "AS-09", "social proof / stat to verify (%s): %s" % (label, t.strip()[:60]))
        if "—" in t:
            emdash_lines.append(ln)
    if emdash_lines:
        rep.add(emdash_lines[0], "LOW", "AS-16", "em-dash in the visible text: %d (lines: %s)" % (
            len(emdash_lines), ",".join(map(str, emdash_lines[:8]))), count=len(emdash_lines))

    # colours: AS-17 off-token, legacy drift (only with brand colours filled in); AS-01 purple-blue gradient + purple glow
    tok = design.colors
    leg = design.colors_legacy
    if tok:
        off, legacy_used, near = defaultdict(list), defaultdict(list), {}
        for ln, c, _ in all_colors:
            if c in tok:
                continue
            if c in leg:
                legacy_used[c].append(ln)
                continue
            n, dist = nearest(c, tok | leg)
            off[c].append(ln)
            near[c] = (n, dist)
        for c, lns in sorted(off.items(), key=lambda kv: -len(kv[1])):
            n, dist = near[c]
            if dist <= 12:
                rep.add(lns[0], "LOW", "AS-17", "almost a token colour %s (~%s), %d places" % (c, n, len(lns)))
            else:
                rep.add(lns[0], "LOW", "AS-17", "colour outside the tokens %s, %d places (lines: %s)" % (
                    c, len(lns), ",".join(map(str, sorted(set(lns))[:6]))))
        far = [c for c in off if near[c][1] > 12]
        if len(far) >= 5:
            rep.add(0, "MED", "AS-17", "palette drift: %d different colours outside the tokens" % len(far),
                    count=len(far) - 4)
        if legacy_used:
            first = min(l for v in legacy_used.values() for l in v)
            rep.add(first, "LOW", "LEGACY", "legacy (not current) palette: %d colours, %d places (%s)" % (
                len(legacy_used), sum(len(v) for v in legacy_used.values()),
                " ".join(sorted(legacy_used)[:8])), count=min(len(legacy_used), 5))

    glow_count = 0
    for ln, d in decls:
        prop = d.split(":", 1)[0].strip().lower()
        val = d.split(":", 1)[1] if ":" in d else ""
        if "gradient(" in val:
            cs = [hex_rgb(c) for c in colors_in(val)]
            if any(is_purple(c) for c in cs) and any(is_blue(c) for c in cs):
                rep.add(ln, "HIGH", "AS-01", "purple-blue gradient: %s" % val.strip()[:70])
            elif "radial-gradient(" in val and any(is_purple(c) for c in cs):
                rep.add(ln, "MED", "AS-01", "purple radial glow: %s" % val.strip()[:70])
        if prop in ("box-shadow", "text-shadow", "filter") or prop.startswith("--shadow") or prop.startswith("--glow"):
            cs = [hex_rgb(c) for c in colors_in(val)]
            colored = [c for c in cs if hue_sat(c)[1] >= 0.35]
            big = [int(x) for x in re.findall(r"(\d+)px", val)]
            if colored or (big and max(big) >= 60):
                glow_count += 1
    if glow_count > 3:
        rep.add(0, "MED", "AS-12", "coloured glow / large shadow: %d declarations (limit: 3)" % glow_count,
                count=glow_count - 3)

    for ln, raw in code:
        m = GRAD_FILL_CLS.search(raw)
        if m and not GRAD_TEXT_CLS.search(raw):
            rep.add(ln, "MED", "AS-01", "gradient fill class: %s (button/hero? review)" % m.group(0))

    # AS-04 radius
    radii = []
    for ln, d in decls:
        if re.match(r"border-radius\s*:", d, re.I) or re.match(r"--radius[\w-]*\s*:", d):
            v = d.split(":", 1)[1].strip().split()[0].lower() if d.split(":", 1)[1].strip() else ""
            if v and not v.startswith(("var(", "calc(", "inherit", "0")):
                radii.append((ln, v))
    for ln, raw in code:
        for m in ROUNDED_CLS.finditer(raw):
            radii.append((ln, m.group(0)))
    if radii:
        pill = {"999px", "9999px", "50%", "100%", "rounded-full", "100px"}
        nonpill = [v for _, v in radii if v not in pill]
        if len(nonpill) >= 8:
            v, n = Counter(nonpill).most_common(1)[0]
            share = n / len(nonpill)
            if share >= 0.6:
                rep.add(0, "MED", "AS-04", "one radius everywhere: %s is %d%% of the non-pill radii (%d/%d)" % (
                    v, round(share * 100), n, len(nonpill)))
        allowed = design.radius | design.radius_legacy
        if design.radius:
            offr = Counter(v for _, v in radii if not v.startswith("rounded") and v not in allowed
                           and re.match(r"^[\d.]+(px|rem|%)$", v))
            for v, n in offr.most_common():
                ln = next(l for l, x in radii if x == v)
                rep.add(ln, "LOW", "AS-04", "radius off the scale %s (%dx)" % (v, n))

    # AS-07 three equal columns
    for ln, d in decls:
        if GRID3_CSS.search(d + ";"):
            rep.add(ln, "LOW", "AS-07", "3 equal columns (review: a feature-card row?)")
    for ln, raw in code:
        if GRID3_CLS.search(raw):
            rep.add(ln, "LOW", "AS-07", "grid-cols-3 (review: a feature-card row?)")

    # AS-15 endless decorative animation
    inf = [ln for ln, d in decls if re.match(r"(?:animation|--animate[\w-]*)\s*:", d) and "infinite" in d]
    inf += [ln for ln, raw in code if ANIM_CLS.search(raw)]
    if inf:
        rep.add(inf[0], "LOW", "AS-15", "endless animation: %d (review: does it show a live state?)" % len(inf),
                count=len(inf))

    # AS-10 fonts
    fams = []
    for ln, d in decls:
        if re.match(r"(?:font-family|--font[\w-]*)\s*:", d, re.I):
            first = d.split(":", 1)[1].split(",")[0].strip().strip("\"'").lower()
            if first and not first.startswith("var(") and first != "inherit":
                fams.append((ln, first))
    if fams:
        if design.fonts:
            bad = Counter(f for _, f in fams if f not in design.fonts and f not in design.fonts_legacy)
            for f, n in bad.most_common():
                ln = next(l for l, x in fams if x == f)
                rep.add(ln, "MED", "AS-10", "font outside the tokens: %s (%dx)" % (f, n))
            legf = Counter(f for _, f in fams if f in design.fonts_legacy and f not in design.fonts)
            if legf:
                ln = next(l for l, x in fams if x in legf)
                rep.add(ln, "LOW", "LEGACY", "legacy font stack (%s), not %s" % (
                    ", ".join(legf), " / ".join(sorted(design.fonts))))
        uniq = {f for _, f in fams}
        if uniq == {"inter"} and len(fams) >= 2:
            rep.add(fams[0][0], "MED", "AS-10", "Inter only, no display font")

    rep.findings.sort(key=lambda f: (f[0], -WEIGHT[f[1]]))
    return rep


# --------------------------------------------------------------------------- cli


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for f in sorted(files):
                    if f.lower().endswith(EXTS):
                        out.append(os.path.join(root, f))
        elif os.path.isfile(p):
            out.append(p)
        else:
            print("ERROR: no such file or folder: %s" % p)
    return out


def rel(p):
    try:
        r = os.path.relpath(p, ROOT)
    except ValueError:
        return p
    return p if r.startswith("..") else r


def main(argv=None):
    ap = argparse.ArgumentParser(description="design linter for the AI-made look (reads only)")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--design", help="the DESIGN.md with the brand tokens (default: brain/brand/DESIGN.md)")
    ap.add_argument("--md", help="write a markdown report here as well")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    dpath = a.design or (DEFAULT_DESIGN if os.path.exists(DEFAULT_DESIGN) else None)
    if a.design and not os.path.isfile(a.design):
        print("ERROR: no such design file: %s" % a.design)
        return 0
    design = Design(dpath)

    reps = [r for r in (lint_file(f, design) for f in collect(a.paths)) if r]
    out = []
    w = out.append
    w("=" * 72)
    w("DESIGN LINT  |  rules: %s" % ("%s (%s)" % (design.name, rel(dpath)) if dpath else "general only"))
    if design.note:
        w("NOTE: " + design.note)
    w("=" * 72)
    for r in sorted(reps, key=lambda r: -r.score):
        per100 = r.score * 100.0 / max(r.nlines, 1)
        w("\n%s  score: %d  (%.1f / 100 lines, %d lines)" % (rel(r.path), r.score, per100, r.nlines))
        if not a.quiet:
            for ln, sev, rule, msg, sc in r.findings:
                w("  %s:%-5s %-4s %-6s %s" % (os.path.basename(r.path), ln if ln else "-", sev, rule, msg))
            if not r.findings:
                w("  (no findings)")

    total = sum(r.score for r in reps)
    by_rule = Counter()
    by_rule_score = Counter()
    for r in reps:
        for f in r.findings:
            by_rule[f[2]] += 1
            by_rule_score[f[2]] += f[4]
    w("\n" + "=" * 72)
    w("TOTAL: %d files, %d findings, score: %d" % (len(reps), sum(len(r.findings) for r in reps), total))
    w("By rule (score / findings): " + (", ".join(
        "%s %d/%d" % (k, by_rule_score[k], by_rule[k]) for k, _ in by_rule_score.most_common()) or "none"))
    allf = [(f[4], WEIGHT[f[1]], r.path, f) for r in reps for f in r.findings]
    allf.sort(key=lambda x: (-x[0], -x[1]))
    if allf:
        w("\nTOP %d findings (by score):" % a.top)
        for sc, _, p, (ln, sev, rule, msg, _) in allf[:a.top]:
            w("  [%2d] %s:%s %s %s %s" % (sc, rel(p), ln if ln else "-", sev, rule, msg))
    w("Exit 0. The score is a signal, not a verdict; [review] items need a person's look.")
    print("\n".join(out))

    if a.md:
        with open(a.md, "w", encoding="utf-8") as fh:
            fh.write("```\n" + "\n".join(out) + "\n```\n")
        print("\nReport saved: %s" % a.md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
