"""
Page checks for a compiled resume PDF, matching resume_tool.py `check` (which needs pdflatex + poppler).

Measures with pdfminer (pure Python) on the PDF Overleaf produced, and re-uses resume_tool's own rules:
1 page, text bottom <= MAIN's, identical left/top, right edge, only the 2 known LaTeX errors, overfull <= 0.6pt,
no wrapped skills line / 3-line bullet / orphan / stray gap, title on one line, diff vs MAIN limited to allowed lines.
Coordinates are calibrated against MAIN.tex compiled on the same Overleaf, so engine differences cancel out.
"""
import io
import re

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTTextContainer, LTTextLineHorizontal

PAGE_H = 792.0
NEVER_CLAIM = ["GCP", "Google Cloud", "Snowflake", "Databricks", "Spark", "PySpark", "Airflow", "dbt", "Alteryx",
               "Hadoop", "Kubernetes", "Kafka", "Flink", "PostgreSQL", "MySQL", "SQL Server", "PyTorch",
               "TensorFlow", "JAX", "OpenCV", "scikit-learn", "Prophet", "SciPy", "LangChain", "LangGraph",
               "CrewAI", "LlamaIndex", "Tableau", "QuickSight", "MicroStrategy", "PowerShell", "Rust", "C#",
               ".NET", "React Native", "Flutter", "Kotlin", "Swift", "ROS", "Salesforce", "97% accuracy"]


def _lines(pdf_bytes):
    """pdftotext-like lines: dict(x0, y0, x1, y1 [top-left origin], w=[words])."""
    pages = list(extract_pages(io.BytesIO(pdf_bytes), laparams=LAParams(line_margin=0.2, char_margin=2.0)))
    out = []
    for el in pages[0] if pages else []:
        if not isinstance(el, LTTextContainer):
            continue
        for ln in el:
            if not isinstance(ln, LTTextLineHorizontal):
                continue
            words = ln.get_text().split()
            if not words:
                continue
            out.append(dict(x0=ln.x0, y0=PAGE_H - ln.y1, x1=ln.x1, y1=PAGE_H - ln.y0, w=words))
    return len(pages), out


def measure(pdf_bytes):
    n, L = _lines(pdf_bytes)
    return dict(pages=n, left=min(d["x0"] for d in L), right=max(d["x1"] for d in L),
                top=min(d["y0"] for d in L), bottom=max(d["y1"] for d in L)), L


def check(rt, main_tex, tex, pdf_bytes, log, ref, bank=None):
    """rt = imported resume_tool module, ref = measure() of MAIN compiled the same way."""
    m, L = measure(pdf_bytes)
    errs = re.findall(r"^! (.*)$", log, re.M)
    overfull = [float(x) for x in re.findall(r"Overfull \\hbox \(([\d.]+)pt too wide\)", log)]
    # same visual-line merge + layout rules as resume_tool.check
    L.sort(key=lambda d: (round(d["y0"], 1), d["x0"]))
    merged = []
    for d in L:
        if merged and d["y0"] < merged[-1]["y1"] - 1 and d["y1"] > merged[-1]["y0"] + 1:
            mm = merged[-1]
            mm["w"] = (d["w"] + mm["w"]) if d["x0"] < mm["x0"] else (mm["w"] + d["w"])
            mm["x0"] = min(mm["x0"], d["x0"]); mm["x1"] = max(mm["x1"], d["x1"]); mm["y1"] = max(mm["y1"], d["y1"])
        else:
            merged.append(dict(d))
    L = merged
    problems, sec, groups = [], None, []
    head = []
    for d in L:
        t = " ".join(d["w"])
        if t in ("Education", "Experience", "Projects", "Technical Skills"):
            sec = t
            continue
        if sec is None:
            head.append(t)
            continue
        if d["w"][0] == "•":
            groups.append([sec, [d]])
        elif groups and d["x0"] > ref["left"] + 11 and groups[-1][0] == sec:
            groups[-1][1].append(d)
        else:
            if sec == "Technical Skills":
                problems.append("skills line wraps: " + t[:60])
            groups.append([None, []])
    if len(head) > 3:
        problems.append("title wraps to 2 lines (keep it ~105 characters)")
    nb = 0
    for s, ls in groups:
        if s == "Technical Skills" and len(ls) > 1:
            problems.append("skills line wraps: " + " ".join(ls[1]["w"])[:60])
        if not ls or s not in ("Experience", "Projects"):
            continue
        nb += 1
        txt = " ".join(" ".join(x["w"]) for x in ls)
        if len(ls) >= 3:
            problems.append("3-line bullet: " + txt[:70])
        if len(ls) >= 2 and (len(ls[-1]["w"]) <= 2 or ls[-1]["x1"] - ls[-1]["x0"] < 40):
            problems.append("orphan last line: " + txt[:70])
    gaps = [(L[i]["y0"] - L[i - 1]["y0"], " ".join(L[i]["w"])[:50]) for i in range(1, len(L))
            if L[i]["w"] and L[i]["w"][0] == "•" and L[i - 1]["x0"] > ref["left"] + 11]
    if gaps:
        normal = sorted(g for g, _ in gaps)[len(gaps) // 2]
        problems += ["stray gap %.1fpt (normal %.1f) before: %s" % (g, normal, t) for g, t in gaps if g > normal + 4]
    claimed = []
    changed = [c.replace("\\#", "#").replace("\\%", "%") for c in
               re.findall(r"^\s*(?:\\resumeItem\{.*\}|\\item \\textbf\{.*|\\small \\textit\{.*)$", tex, re.M)]
    for term in NEVER_CLAIM:
        pat = r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])"
        if any(re.search(pat, c) for c in changed):
            claimed.append(term)
    bad_diff = rt.diff_violations(main_tex, tex)
    new = []
    if bank is not None:
        new = [b for b in re.findall(r"^\s*\\resumeItem\{(.*)\}\s*$", tex, re.M)
               if "Relevant Coursework" not in b and b not in bank]
    # translate to the MAIN.pdf reference numbers (742.73 / 28.8 / 40.2) so limits read like the rulebook
    bottom = rt.MAIN_BOTTOM + (m["bottom"] - ref["bottom"])
    known = ref.get("errors", rt.KNOWN_ERRORS)
    extra = [e for e in errs if e not in known]
    res = dict(pages=m["pages"], text_bottom=round(bottom, 2), spare_to_MAIN=round(rt.MAIN_BOTTOM - bottom, 2),
               left_shift=round(m["left"] - ref["left"], 2), top_shift=round(m["top"] - ref["top"], 2),
               right_over=round(m["right"] - ref["right"], 2), latex_errors=len(errs), unexpected_errors=extra,
               overfull_pt=overfull, bullets=nb, layout_problems=problems, diff_violations=bad_diff,
               never_claim_hits=claimed, bullets_not_in_existing_files=new)
    res["PASS"] = (m["pages"] == 1 and bottom <= rt.MAIN_BOTTOM + 0.01 and abs(res["left_shift"]) < 0.05
                   and abs(res["top_shift"]) < 0.1 and res["right_over"] <= 0.01 and not extra
                   and len(errs) == len(known) and not problems and not bad_diff
                   and not claimed and all(o <= 0.6 for o in overfull))
    res["FILL_OK"] = bottom >= 730
    return res
