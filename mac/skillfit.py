"""
Fill each skills line to the margin in the script, so Claude is asked only once per resume.

Printed width comes from Computer Modern's own metrics (cm10_metrics.json = CTAN amsfonts cmr10/cmbx10 AFMs:
glyph widths + kerning pairs). Skills lines are {\\small} = 10pt CMR10 items after a CMBX10 label; widths are PDF
points, like pdfcheck. A line whose natural width fits never wraps (TeX can only shrink spaces further).
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 10 * 72 / 72.27                       # 10pt TeX -> PDF points
SPACE = {"rm": 333.333, "bf": 383.333}       # font space (1/1000 em)
EXTRA = {"rm": 111.111, "bf": 127.778}       # extra space after ':' '.' '?' '!' (space factor >= 2000)
LIGATURES = ("ffi", "ffl", "ff", "fi", "fl", "---", "--", "``", "''")
LIG_CHAR = {"---": "—", "--": "–", "``": "“", "''": "”"}
UNKNOWN = 778                                # unknown glyph: as wide as 'M' (errs towards no wrap)
LINE_PT = 531.76     # label start (50.72) -> right margin (582.48), measured on the 64 Overleaf resumes of Nov 1-3
SAFETY_PT = 1.5      # predicted vs printed width was within -1.3 / +0.5pt on all 256 skills lines

_M = json.load(open(os.path.join(HERE, "cm10_metrics.json"), encoding="utf-8"))
TEX = [(r"\&", "&"), (r"\#", "#"), (r"\%", "%"), (r"\$", "$"), (r"\_", "_"), ("~", " "), (r"\,", " "),
       (r"$\sim$", "∼"), (r"\textsuperscript{2}", "2")]


def _plain(tex):
    for a, b in TEX:
        tex = tex.replace(a, b)
    tex = re.sub(r"\\[A-Za-z]+\*?", "", tex)                  # any other macro: drop the name, keep its text
    return tex.replace("{", "").replace("}", "").replace("$", "")


def _run(font, text):
    """Width (1/1000 em) of a word with no spaces: TeX ligatures, then glyph widths + kerns."""
    w, k = _M[font]["w"], _M[font]["kern"]
    glyphs, i = [], 0
    while i < len(text):
        for lig in LIGATURES:
            if text.startswith(lig, i):
                glyphs.append(LIG_CHAR.get(lig, lig))
                i += len(lig)
                break
        else:
            glyphs.append(text[i])
            i += 1
    total = sum(w.get(g, UNKNOWN) for g in glyphs)
    return total + sum(k.get(a + "|" + b, 0) for a, b in zip(glyphs, glyphs[1:]))


def _text(font, text):
    total, prev = 0.0, ""
    for n, word in enumerate(text.split(" ")):
        if n:
            total += SPACE["rm" if font == "rm" else "bf"] + (EXTRA[font] if prev[-1:] in ":.?!" else 0)
        total += _run(font, word)
        prev = word or prev
    return total


def width(label, items):
    """Natural printed width (pt) of '\\textbf{label:} items' (both in LaTeX)."""
    lab = _plain(label).strip() + ":"
    em = _text("bf", lab) + SPACE["rm"] + EXTRA["rm"] + _text("rm", _plain(items).strip())
    return em * SIZE / 1000


def split_items(items):
    """Top-level comma split: 'AWS (EC2, S3), Docker' -> ['AWS (EC2, S3)', 'Docker']."""
    out, depth, cur = [], 0, ""
    for c in items:
        depth += (c in "([{") - (c in ")]}")
        if c == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += c
    return [x for x in out + [cur.strip()] if x]


def _key(item):
    return re.sub(r"[^a-z0-9+#]", "", _plain(item).lower())


def fill(skills, extras):
    """skills = [[label, items]], extras = [[ranked true skills per line]] ->
    (new skills, dropped JD items). A line that would wrap loses its last items (Claude lists required skills
    first); a line with room takes the next extras that fit. A skill already on the page is never added twice."""
    limit = LINE_PT - SAFETY_PT
    lines = [[lab, split_items(val)] for lab, val in skills]
    used = {_key(x) for _, items in lines for x in items}
    dropped = []
    for i, (lab, items) in enumerate(lines):
        while len(items) > 1 and width(lab, ", ".join(items)) > limit:
            dropped.append(items.pop())
        for x in (extras[i] if i < len(extras) else []):
            if _key(x) not in used and width(lab, ", ".join(items + [x])) <= limit:
                items.append(x)
                used.add(_key(x))
    return [[lab, ", ".join(items)] for lab, items in lines], dropped


def spare(label, items):
    return round(LINE_PT - width(label, items), 1)
