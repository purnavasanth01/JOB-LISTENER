#!/usr/bin/env python3
"""
Mac side of JOB-LISTENER: turn ⭐ starred jobs into tailored one-page resumes, ready when you open the laptop.

launchd runs this at login, after wake, and every 5 minutes while awake. For each starred job not done yet:
  1. download its JD file (written by the cloud listener on the `state` branch)
  2. ask Claude Code (`claude -p`, your logged-in subscription) for a SPEC that follows
     INTERNSHIP/knowledge_base/RESUME_RULES_AND_FACTS.md: lane, title, bullets, skills, Airkrit yes/no, fit
  3. build the .tex with your own resume_tool.build (exact line replacement on MAIN.tex: margins can't change)
  4. compile on Overleaf, measure the page (1 page, MAIN's margins, bottom 730-742.73pt, no wraps/orphans,
     diff vs MAIN limited to title/bullets/skills/Airkrit, never-claim list)
  5. one retry with the exact failure if a check fails
  6. save <Stem>.tex, <Stem>.pdf and <Stem>.md (JD + fit) to INTERNSHIP/RESUMES_2/<date>/ and update TO_APPLY.html/csv

  mac/.venv/bin/python mac/resume_runner.py                 normal run (what launchd does)
  mac/.venv/bin/python mac/resume_runner.py --job <hid>     force one starred job (even if done before)
  mac/.venv/bin/python mac/resume_runner.py --jd FILE.md    run the pipeline on a local JD file (testing)
"""
import argparse
import csv
import datetime as dt
import fcntl
import glob
import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
INTERN = os.path.join(HOME, "Desktop", "INTERNSHIP")
KB = os.path.join(INTERN, "knowledge_base")
RULES = os.path.join(KB, "RESUME_RULES_AND_FACTS.md")
TOOL = os.path.join(KB, "resume_tool.py")
LATEX_FILES = os.path.join(INTERN, "LATEX FILES")
OUT = os.path.join(INTERN, "RESUMES_2")
MAIN = os.path.join(HOME, "Desktop", "RESUMES", "MAIN RESUME", "MAIN.tex")
MAIN_MD5 = "5c1a9b80d8da19a2eea599777cba6ed7"
STATE_RAW = "https://raw.githubusercontent.com/purnavasanth01/JOB-LISTENER/state/"
SUPPORT = os.path.join(HOME, "Library", "Application Support", "job-listener")
LEDGER = os.path.join(SUPPORT, "resume_runner.json")
LOCK = os.path.join(SUPPORT, "resume_runner.lock")
MAX_JOBS_PER_RUN = 6          # limits how much Claude usage one run can spend
MAX_ATTEMPTS = 2              # first try + one retry with the exact check failure

sys.path.insert(0, HERE)


def log(msg):
    print("%s %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def notify_mac(title, text):
    subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s" sound name "Glass"'
                    % (text.replace('"', "'"), title.replace('"', "'"))], capture_output=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "job-listener-mac", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def resume_tool():
    spec = importlib.util.spec_from_file_location("resume_tool", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def claude_bin():
    found = sorted(glob.glob(os.path.join(HOME, ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")))
    for c in found[::-1] + [shutil.which("claude") or "", os.path.join(HOME, ".claude/local/claude"),
                            os.path.join(HOME, ".local/bin/claude")]:
        if c and os.access(c, os.X_OK):
            return c
    raise RuntimeError("Claude Code CLI not found")


# ---------------------------------------------------------------- naming

def camel(s, limit):
    words = re.findall(r"[A-Za-z0-9]+", s)
    return "".join(w if w.isupper() and len(w) <= 4 else w[:1].upper() + w[1:] for w in words)[:limit]


def stem_for(company, title, key):
    t = re.sub(r"\((?:[^)]*?)\)|\b(summer|fall|spring|winter)\s*(20)?2\d\b|\b20\d\d\b|[-–—|,].*$", " ", title, flags=re.I)
    req = re.sub(r"[^A-Za-z0-9]", "", key.split(":")[-1])[:12]
    return "%s_%s_%s" % (camel(company, 24) or "Company", camel(t, 40) or "Role", req or "ID")


# ---------------------------------------------------------------- Claude

SPEC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["lane", "title", "tamu", "vz", "pep", "cal", "airkrit", "skills", "fit", "new_wordings", "notes"],
    "properties": {
        "lane": {"type": "string"},
        "title": {"type": "string"},
        "tamu": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "vz": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "pep": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "cal": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "airkrit": {"anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "string"}}]},
        "skills": {"type": "array", "minItems": 4, "maxItems": 5,
                   "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2}},
        "fit": {"type": "object", "additionalProperties": False,
                "required": ["what_the_job_is", "meets", "gaps", "angle", "rating", "reason"],
                "properties": {k: {"type": "string"} for k in
                               ("what_the_job_is", "meets", "gaps", "angle", "rating", "reason")}},
        "new_wordings": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}

INSTRUCTIONS = r"""You are the RESUME GENERATOR, running UNATTENDED from a script on Purna's Mac.
Follow the rulebook below exactly (Parts 2-10, 15-16). Do NOT run the Part 1 start-up checklist and do not ask
questions: the script already verified MAIN.tex (md5 unchanged) and will build, compile and check the page itself.

Your only output is the JSON spec (the script passes it to resume_tool.py build, which replaces ONLY the title,
the \resumeItem lines, the skills \item lines and the optional Airkrit block in MAIN.tex):
- lane: the Part 10 lane you used.
- title: the one-line title text exactly as LaTeX (e.g. "Software Engineer — Backend \& Full-Stack Development, CI/CD"),
  <= ~105 characters, never a title Purna didn't hold, Amazon SDE roles use "Software Development Engineer — ...".
- tamu / vz / pep / cal: the \resumeItem contents (WITHOUT the \resumeItem{ } wrapper), most relevant first. Copy
  fact-bank bullets (Part 8) CHARACTER-FOR-CHARACTER including \textbf{}, $\sim$, \%, ``'' and R\textsuperscript{2}.
  Never use one fact twice. Any wording not in Part 8 must be a faithful rewording and must be listed in new_wordings.
- airkrit: null, or exactly the two Part 6 bullets (AK_PY or AK_PY_ACT, AK_XL; AK_XL first when Excel/BI is the hard
  requirement). Decide per Part 6: YES only for data analyst/scientist/engineer/BI/data-governance roles AND only if
  the page still fits (then use about 9 other bullets, one of them 1-line).
- skills: 4 lines (5 only if bullets were cut), each [label, items] in LaTeX (use \& for &). EVERY line must fit on
  one printed line: keep label+items under ~95 characters. JD keywords first, company cloud rule, never-claim list,
  avoid "(" or "/" on the first skills line when the page is full (0.56pt trap).
- Plan the page with the Part 7 arithmetic: MAIN (12 two-line bullets + 4 one-line skills, no Airkrit) is exactly
  full (742.73pt). The page must be FILLED (730-742.73pt), never over.
- CalTrack and every other number: use exactly what MAIN.tex says (R\textsuperscript{2} = 0.97, 4.5 kcal MAE).
- fit: what_the_job_is (3-5 plain lines), meets (requirements truly satisfied, with evidence), gaps (missing,
  including never-claim items the JD asks for), angle (which true experiences to lead with), rating (STRONG,
  MEDIUM-STRONG, MEDIUM, MEDIUM-WEAK, WEAK or DROP recommended) and reason (one line).
- notes: anything Purna must do before submitting (duplicate IDs, eligibility to confirm, documents, deadlines,
  Power BI/AWS reminders).
"""


def ask_claude(rules, main_tex, jd, previous=None, feedback=None):
    parts = [INSTRUCTIONS, "\n===== RULEBOOK (RESUME_RULES_AND_FACTS.md) =====\n", rules,
             "\n===== MAIN.tex (template; source of truth for facts and numbers) =====\n", main_tex,
             "\n===== JOB DESCRIPTION FILE =====\n", jd]
    if previous is not None:
        parts += ["\n===== YOUR PREVIOUS SPEC FAILED THE PAGE CHECK =====\n", json.dumps(previous, indent=1),
                  "\n===== CHECK RESULT (fix exactly these problems; text_bottom must end 730-742.73) =====\n",
                  json.dumps(feedback, indent=1),
                  "\nGuide: one 2-line bullet ~25.9pt, one 1-line bullet ~13.95pt, a wrapped skills line or a title "
                  "wrap ~12-13pt. Over 742.73 -> cut or shorten; under 730 -> add true bullets from the fact bank."]
    cmd = [claude_bin(), "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--json-schema", json.dumps(SPEC_SCHEMA)]
    r = subprocess.run(cmd, input="".join(parts), capture_output=True, text=True, timeout=900)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError("claude -p failed: %s %s" % (r.returncode, (r.stderr or r.stdout)[-400:]))
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError("claude -p returned no spec: %s" % str(out.get("result"))[:400])
    u = out.get("usage") or {}
    log("  claude: %s in (+%s cached) / %s out tokens" % (u.get("input_tokens"), u.get("cache_read_input_tokens", 0)
                                                          + u.get("cache_creation_input_tokens", 0),
                                                          u.get("output_tokens")))
    return out["structured_output"]


# ---------------------------------------------------------------- one job

def fit_section(spec):
    f = spec["fit"]
    lines = ["", "## Fit (Claude, against MAIN.tex facts)", "",
             "- **Rating:** %s — %s" % (f["rating"], f["reason"]),
             "- **What this job actually is:** %s" % f["what_the_job_is"],
             "- **Meets:** %s" % f["meets"], "- **Gaps (not claimed):** %s" % f["gaps"],
             "- **Angle:** %s" % f["angle"], "- **Lane / title:** %s / %s" % (spec["lane"], spec["title"]),
             "- **Airkrit:** %s" % ("yes" if spec.get("airkrit") else "no")]
    if spec.get("new_wordings"):
        lines.append("- **NEW wording — confirm:** " + " | ".join(spec["new_wordings"]))
    if spec.get("notes"):
        lines.append("- **Before submitting:** " + spec["notes"])
    return "\n".join(lines) + "\n"


def make_resume(ctx, job, jd):
    """-> dict(status, stem, files, check, spec)"""
    rt, main_tex, rules, ol, ref, bank = ctx["rt"], ctx["main"], ctx["rules"], ctx["ol"], ctx["ref"], ctx["bank"]
    import pdfcheck
    spec, res, tex, pdf, prev, feedback = None, None, None, None, None, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        spec = ask_claude(rules, main_tex, jd, prev, feedback)
        spec_rt = dict(spec, skills=[tuple(x) for x in spec["skills"]], airkrit=spec.get("airkrit") or None)
        try:
            tex = rt.build(main_tex, spec_rt)
        except Exception as e:  # noqa: BLE001
            res = {"PASS": False, "FILL_OK": False, "build_error": str(e)}
        else:
            pdf, latex_log, status = ol.compile_tex(tex, "resume")
            res = (pdfcheck.check(rt, main_tex, tex, pdf, latex_log, ref, bank) if pdf
                   else {"PASS": False, "FILL_OK": False, "compile_status": status})
        log("  attempt %d: %s, bottom %s, problems %s" % (attempt, "PASS" if res.get("PASS") else "FAIL",
                                                         res.get("text_bottom"), res.get("layout_problems")
                                                         or res.get("diff_violations") or res.get("never_claim_hits")
                                                         or res.get("build_error") or ""))
        if res.get("PASS") and res.get("FILL_OK"):
            break
        prev, feedback = spec, {k: v for k, v in res.items() if k != "bullets_not_in_existing_files"}
    ok = bool(res.get("PASS"))
    return dict(status="ready" if ok else "needs you", tex=tex, pdf=pdf if ok else None, check=res, spec=spec,
                attempts=attempt, fill_ok=bool(res.get("FILL_OK")))


def save_outputs(job, jd, out, stem):
    day = dt.date.today().isoformat()
    d = os.path.join(OUT, day)
    os.makedirs(d, exist_ok=True)
    suffix = "" if out["status"] == "ready" else "_NEEDS_REVIEW"
    files = {}
    if out.get("tex"):
        files["tex"] = os.path.join(d, stem + suffix + ".tex")
        with open(files["tex"], "w", encoding="utf-8") as f:
            f.write(out["tex"])
    if out.get("pdf"):
        files["pdf"] = os.path.join(d, stem + ".pdf")
        with open(files["pdf"], "wb") as f:
            f.write(out["pdf"])
    files["jd"] = os.path.join(d, stem + ".md")
    with open(files["jd"], "w", encoding="utf-8") as f:
        f.write(jd.rstrip() + "\n" + (fit_section(out["spec"]) if out.get("spec") else ""))
    return {k: os.path.relpath(v, OUT) for k, v in files.items()}


# ---------------------------------------------------------------- table

TABLE_FIELDS = ["date", "status", "applied", "rank", "company", "title", "fit", "apply_link", "pdf", "tex", "jd",
                "notes", "hid"]


def write_table(ledger):
    csv_path = os.path.join(OUT, "TO_APPLY.csv")
    applied = {}
    if os.path.exists(csv_path):                       # keep whatever you typed in the "applied" column
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("hid"):
                    applied[r["hid"]] = r.get("applied", "")
    rows = []
    for hid, e in ledger["jobs"].items():
        if e.get("status") not in ("ready", "needs you"):
            continue
        rows.append({"date": e.get("date", ""), "status": e["status"], "applied": applied.get(hid, ""),
                     "rank": e.get("rank") or "", "company": e["company"], "title": e["title"],
                     "fit": e.get("fit", ""), "apply_link": e.get("url", ""),
                     "pdf": e.get("files", {}).get("pdf", ""), "tex": e.get("files", {}).get("tex", ""),
                     "jd": e.get("files", {}).get("jd", ""), "notes": e.get("notes", ""), "hid": hid})
    rows.sort(key=lambda r: (r["applied"] != "", r["status"] != "ready", r["rank"] == "", str(r["rank"]).zfill(3),
                             r["date"]), reverse=False)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TABLE_FIELDS)
        w.writeheader()
        w.writerows(rows)
    e = html.escape
    trs = []
    for r in rows:
        link = lambda p, label: ('<a href="%s">%s</a>' % (e(p), label)) if p else ""
        trs.append("<tr class='%s'><td>%s</td><td>%s</td><td>%s</td><td><b>%s</b>%s</td><td>%s</td><td>%s</td>"
                   "<td><a href='%s' target='_blank'>Apply ↗</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                   % ("done" if r["applied"] else ("bad" if r["status"] != "ready" else ""), e(r["date"]),
                      "✅" if r["applied"] else ("📄" if r["status"] == "ready" else "⚠️ needs you"),
                      e(str(r["rank"])), e(r["company"]), " 🔥" if r["rank"] else "", e(r["title"]), e(r["fit"]),
                      e(r["apply_link"]), link(r["pdf"], "PDF"), link(r["tex"], ".tex"), link(r["jd"], "JD"),
                      e(r["notes"])[:300]))
    page = ("<!doctype html><meta charset='utf-8'><title>To apply</title><style>body{font:14px -apple-system,sans-serif;"
            "margin:24px}table{border-collapse:collapse;width:100%%}td,th{border:1px solid #ccc;padding:6px;"
            "vertical-align:top}th{background:#f3f3f3;text-align:left}tr.done{opacity:.45}tr.bad{background:#fff4e5}"
            "</style><h2>Resumes ready to apply (%d)</h2><p>Mark a job applied by typing anything in the "
            "<b>applied</b> column of TO_APPLY.csv. Updated %s.</p><table><tr><th>Date</th><th></th><th>Rank</th>"
            "<th>Company</th><th>Title</th><th>Fit</th><th>Link</th><th>Resume</th><th>LaTeX</th><th>JD</th>"
            "<th>Notes</th></tr>%s</table>" % (sum(1 for r in rows if r["status"] == "ready" and not r["applied"]),
                                              dt.datetime.now().strftime("%b %d %H:%M"), "".join(trs)))
    with open(os.path.join(OUT, "TO_APPLY.html"), "w", encoding="utf-8") as f:
        f.write(page)


# ---------------------------------------------------------------- run

def reference(ol, rt, main_tex, ledger):
    """MAIN.tex compiled on Overleaf, measured once and cached (calibration for the page checks)."""
    import pdfcheck
    ref = ledger.get("ref")
    if ref and ref.get("md5") == MAIN_MD5:
        return ref
    pdf, latex_log, status = ol.compile_tex(main_tex, "main-reference")
    if not pdf:
        raise RuntimeError("could not compile MAIN.tex on Overleaf (%s)" % status)
    m, _ = pdfcheck.measure(pdf)
    ref = dict(m, md5=MAIN_MD5, errors=re.findall(r"^! (.*)$", latex_log, re.M))
    ledger["ref"] = ref
    log("reference MAIN.tex on Overleaf: %s" % json.dumps({k: (round(v, 2) if isinstance(v, float) else v)
                                                          for k, v in ref.items()}))
    return ref


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", help="force this starred job id")
    ap.add_argument("--jd", help="local JD .md file to process (test)")
    ap.add_argument("--headed", action="store_true", help="show the Overleaf browser")
    args = ap.parse_args()

    os.makedirs(SUPPORT, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another run is active; exiting")
        return
    if not os.path.isdir(OUT):
        log("RESUMES_2 folder missing: %s" % OUT)
        return
    with open(MAIN, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    if md5 != MAIN_MD5:
        notify_mac("Resume runner stopped", "MAIN.tex changed (md5). Update the runner after checking it.")
        log("MAIN.tex md5 %s != %s: stopping (rulebook says tell Purna)" % (md5, MAIN_MD5))
        return
    ledger = load_json(LEDGER, {"jobs": {}})

    # ---- which jobs
    todo = []
    if args.jd:
        jd = open(args.jd, encoding="utf-8").read()
        m = re.search(r"^# (.+?) — (.+)$", jd, re.M)
        km = re.search(r"\*\*Job key:\*\* `([^`]+)`", jd)
        um = re.search(r"\*\*Link:\*\* (\S+)", jd)
        rm = re.search(r"TARGET #(\d+)", jd)
        key = km.group(1) if km else os.path.basename(args.jd)
        hid = hashlib.sha1(key.encode()).hexdigest()[:10]
        todo.append((hid, {"company": m.group(1), "title": m.group(2), "key": key,
                           "rank": int(rm.group(1)) if rm else None, "url": um.group(1) if um else ""}, jd))
    else:
        try:
            stars = json.loads(fetch(STATE_RAW + "stars.json"))
        except Exception as e:  # noqa: BLE001
            log("could not read stars.json: %s" % e)
            return
        for hid, e in sorted(stars.items(), key=lambda kv: kv[1].get("starred") or ""):
            if e.get("status") != "starred":
                continue
            if args.job and hid != args.job:
                continue
            if not args.job and hid in ledger["jobs"]:
                continue
            todo.append((hid, e, None))
    if not todo:
        log("nothing to do")
        write_table(ledger)
        return
    todo = todo[:MAX_JOBS_PER_RUN]
    log("%d starred job(s) to process" % len(todo))

    import overleaf
    rt = resume_tool()
    main_tex = open(MAIN, encoding="utf-8").read()
    rules = open(RULES, encoding="utf-8").read()
    bank = rt.bank_from(LATEX_FILES)
    made = []
    with overleaf.Overleaf(headless=not args.headed) as ol:
        ctx = dict(rt=rt, main=main_tex, rules=rules, ol=ol, bank=bank, ref=reference(ol, rt, main_tex, ledger))
        save_json(LEDGER, ledger)
        for hid, e, jd in todo:
            log("job %s: %s — %s" % (hid, e["company"], e["title"]))
            try:
                if jd is None:
                    jd = fetch(STATE_RAW + "jds/%s.md" % hid)
                out = make_resume(ctx, e, jd)
                stem = stem_for(e["company"], e["title"], e.get("key", hid))
                files = save_outputs(e, jd, out, stem)
                fit = out["spec"]["fit"] if out.get("spec") else {}
                note = out["spec"].get("notes", "") if out.get("spec") else ""
                if out["status"] == "ready" and not out["fill_ok"]:
                    note = ("page not fully filled (bottom %s pt); " % out["check"].get("text_bottom")) + note
                if out["status"] != "ready":
                    c = out["check"]
                    note = "check failed: %s; %s" % ("; ".join(str(x) for x in (c.get("layout_problems") or [])
                                                     + (c.get("diff_violations") or []) + (c.get("never_claim_hits") or [])
                                                     + ([c["build_error"]] if c.get("build_error") else [])
                                                     + (["bottom %s" % c.get("text_bottom")] if c.get("text_bottom") else []))
                                                     [:4], note)
                ledger["jobs"][hid] = {"status": out["status"], "company": e["company"], "title": e["title"],
                                       "rank": e.get("rank"), "url": e.get("url"), "date": dt.date.today().isoformat(),
                                       "fit": fit.get("rating", ""), "files": files, "notes": note[:400],
                                       "attempts": out["attempts"], "check": out["check"]}
                made.append((out["status"], e["company"]))
                log("  -> %s %s" % (out["status"], files))
            except Exception as ex:  # noqa: BLE001 - one bad job must not stop the rest
                log("  ERROR: %s" % ex)
                errors = ledger.setdefault("errors", {})
                errors[hid] = errors.get(hid, 0) + 1
                if errors[hid] >= 3:                       # transient errors retry on later runs, then give up
                    ledger["jobs"][hid] = {"status": "needs you", "company": e["company"], "title": e["title"],
                                           "rank": e.get("rank"), "url": e.get("url"),
                                           "date": dt.date.today().isoformat(), "notes": "error: %s" % str(ex)[:300]}
            save_json(LEDGER, ledger)
    write_table(ledger)
    ready = sum(1 for s, _ in made if s == "ready")
    if made:
        notify_mac("Resumes ready: %d" % ready, ", ".join(c for _, c in made)[:120] +
                   ((" · %d need you" % (len(made) - ready)) if len(made) > ready else ""))


if __name__ == "__main__":
    main()
