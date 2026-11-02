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
  6. save RESUMES_2/PDF RESUMES/<Stem>.pdf, LATEX/<Stem>.tex, JOB DESCRIPTIONS/<Stem>.md; update TO_APPLY.xlsx (Target / Non-target tabs)

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
MAIN_MD5 = "085c23daee204f3a98892d0f124ac3db"
STATE_RAW = "https://raw.githubusercontent.com/purnavasanth01/JOB-LISTENER/state/"
SUPPORT = os.path.join(HOME, "Library", "Application Support", "job-listener")
LEDGER = os.path.join(SUPPORT, "resume_runner.json")
LOCK = os.path.join(SUPPORT, "resume_runner.lock")
STATE_API = "https://api.github.com/repos/purnavasanth01/JOB-LISTENER/contents/%s?ref=state"   # no CDN cache
MAX_JOBS_PER_RUN = 40         # safety net only; normally every starred job is done in one run
MAX_ATTEMPTS = 2              # first try + one retry with the exact check failure
CLAUDE_PARALLEL = 2           # Claude calls at the same time (Overleaf compiles stay one at a time)
TG_SERVICE = "job-listener-telegram"   # Keychain item holding the bot token (stored by Purna himself)
LIMIT_RE = re.compile(r"usage limit|rate.?limit|limit reached|limit will reset|too many requests|overloaded|quota"
                      r"|hit your .{0,20}limit|\b\d+-hour limit|weekly limit|limit .{0,10}resets", re.I)

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


def fetch(url, raw_api=False):
    h = {"User-Agent": "job-listener-mac", "Cache-Control": "no-cache"}
    if raw_api:
        h["Accept"] = "application/vnd.github.raw"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def state_file(name):
    """Fresh copy from the state branch (API: no 5-minute cache); falls back to the raw CDN."""
    try:
        return fetch(STATE_API % name, raw_api=True)
    except Exception:  # noqa: BLE001 - API rate limit (60/hour) etc.
        return fetch(STATE_RAW + name)


# ---------------------------------------------------------------- problem alerts (Mac + Telegram)

def notify_telegram(text):
    raw = subprocess.run(["security", "find-generic-password", "-s", TG_SERVICE, "-w"], capture_output=True,
                         text=True).stdout.strip()
    if raw and re.fullmatch(r"[0-9a-fA-F]+", raw) and len(raw) % 2 == 0:    # Keychain hex-encodes odd pastes
        try:
            raw = bytes.fromhex(raw).decode("utf-8", "replace")
        except ValueError:
            pass
    token = raw.split("\x1b")[0].replace(" ", "").strip()                   # drop paste junk: spaces, arrow keys
    if not token:
        return False
    try:
        chat = json.loads(state_file("meta.json")).get("tg_chat")
        if not chat:
            return False
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token,
                                     data=json.dumps({"chat_id": chat, "text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception as e:  # noqa: BLE001 - never print the URL: it contains the token
        log("telegram alert failed: %s" % type(e).__name__)
        return False


def alert_once(ledger, key, title, text):
    """Mac notification + Telegram message, at most once per day per problem."""
    today = dt.date.today().isoformat()
    sent = ledger.setdefault("alerts", {})
    if sent.get(key) == today:
        return
    notify_mac(title, text)
    notify_telegram("💻 %s\n%s" % (title, text))
    sent[key] = today
    save_json(LEDGER, ledger)


class UsageLimit(RuntimeError):
    """Claude usage limit reached: stop for now, the jobs stay queued for a later run."""


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
    """Company_Role_ReqID, e.g. Stripe_SoftwareEngineerIntern_7123456 (term, year and location parts dropped)."""
    t = re.sub(r"\([^)]*\)", " ", title)
    t = re.sub(r"\b(summer|fall|spring|winter|autumn)\s*(20)?2\d\b|\b20\d\d\b", " ", t, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s[-–—|:]\s|,|\s[-–—]$|^[-–—]\s", t) if re.search(r"[A-Za-z]{2}", p)]
    role = parts[0] if parts else "Role"
    if re.fullmatch(r"(?i)\s*(summer\s+)?(intern(ship)?|co-?op)s?\s*", role) and len(parts) > 1:
        role = parts[1] + " Intern"                     # "Intern - GM OneOps Deploy Engineer"
    if not re.search(r"intern|co-?op", role, re.I):                 # keep "Intern" when it sits in a later part
        extra = next((p for p in parts[1:] if re.fullmatch(r"(?i)(summer\s+)?(intern(ship)?|co-?op)s?", p)), None)
        role += " " + extra if extra else ""
    req = re.sub(r"[^A-Za-z0-9]", "", key.split(":")[-1])[:12]
    return "%s_%s_%s" % (camel(company, 24) or "Company", camel(role, 40) or "Role", req or "ID")


# ---------------------------------------------------------------- Claude

SPEC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["lane", "title", "tamu", "vz", "pep", "cal", "airkrit", "skills", "fit", "new_wordings", "notes", "key_note"],
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
        "key_note": {"type": "string"},
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
- skills: 4 lines (5 only if bullets were cut), each [label, items] in LaTeX (use \& for &). PURNA'S RULE
  (2026-11-02, his explicit decision, overrides the Part 9 true-skills / never-claim lists FOR THE SKILLS LINES
  ONLY): list EVERY technical skill the JD names (languages, frameworks, tools, platforms; required first, then
  nice-to-have), using the JD's exact wording, whether or not it is in Part 9. Not skills: degrees, clearances,
  soft skills, years of experience. Then fill each line to the margin with the next most relevant Part 9 skills.
  If the JD's skills don't all fit, keep the required ones. Bullets and the title must still state only true facts. A printed line holds ~103-113 characters (label + items, as printed: "\&" counts as 1), so aim
  for 100-106 and never more than ~108 (a wrapped line fails). Never list a skill twice (e.g. Python on two lines). Company cloud rule, avoid "(" or
  "/" on the first skills line when the page is full (0.56pt trap).
- Plan the page with the Part 7 arithmetic: MAIN (12 two-line bullets + 4 one-line skills, no Airkrit) is exactly
  full (742.73pt). The page must be FILLED (730-742.73pt), never over.
- CalTrack and every other number: use exactly what MAIN.tex says (R\textsuperscript{2} = 0.97, 4.5 kcal MAE).
- fit: what_the_job_is (3-5 plain lines), meets (requirements truly satisfied, with evidence), gaps (missing,
  including never-claim items the JD asks for), angle (which true experiences to lead with), rating (STRONG,
  MEDIUM-STRONG, MEDIUM, MEDIUM-WEAK, WEAK or DROP recommended) and reason (one line).
- notes: start with "Listed from the JD (prepare basics): ..." naming every skills-line item that is NOT in Part 9
  (none -> omit). Then anything Purna must do before submitting (duplicate IDs, eligibility to confirm, documents, deadlines,
  Power BI/AWS reminders).
- key_note: the ONE thing Purna must know before applying, plain text, at most 12 words (e.g. "Apply once:
  duplicate of the Workday posting JR105180." or "Confirm Power BI use before submitting."). Shown in his sheet;
  the full notes stay in the JD file.
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
                  "wrap ~12-13pt. Over 742.73 -> cut or shorten; under 730 -> add true bullets from the fact bank. "
                  "skills_space: a line with more than %dpt empty has room left -> add the next most relevant true "
                  "skills (about free_chars characters, ~4.9pt each); a line that wraps -> shorten it."
                  % pdfcheck_full_pt()]
    cmd = [claude_bin(), "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--json-schema", json.dumps(SPEC_SCHEMA)]
    r = subprocess.run(cmd, input="".join(parts), capture_output=True, text=True, timeout=900)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        msg = (r.stderr or r.stdout)[-400:]
        raise (UsageLimit if LIMIT_RE.search(msg) else RuntimeError)("claude -p failed: %s %s" % (r.returncode, msg))
    if out.get("is_error") or not out.get("structured_output"):
        msg = str(out.get("result"))[:400]
        raise (UsageLimit if LIMIT_RE.search(msg) else RuntimeError)("claude -p returned no spec: %s" % msg)
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


def check_spec(ctx, spec):
    """Build from MAIN.tex with resume_tool, compile on Overleaf, check the page. -> (res, tex, pdf)"""
    import pdfcheck
    rt, main_tex = ctx["rt"], ctx["main"]
    spec_rt = dict(spec, skills=[tuple(x) for x in spec["skills"]], airkrit=spec.get("airkrit") or None)
    try:
        tex = rt.build(main_tex, spec_rt)
    except Exception as e:  # noqa: BLE001
        return {"PASS": False, "FILL_OK": False, "build_error": str(e)}, None, None
    pdf, latex_log, status = ctx["ol"].compile_tex(tex, "resume")
    if not pdf:
        return {"PASS": False, "FILL_OK": False, "compile_status": status}, tex, None
    return pdfcheck.check(rt, main_tex, tex, pdf, latex_log, ctx["ref"], ctx["bank"]), tex, pdf


def pdfcheck_full_pt():
    import pdfcheck
    return pdfcheck.SKILLS_FULL_PT


def quality(res):
    """Rank passing attempts: page filled, then skills lines full, then least empty skills space."""
    return (bool(res.get("FILL_OK")), bool(res.get("SKILLS_FULL")),
            -sum(x["empty_pt"] for x in res.get("skills_space") or []))


def run_jobs(ctx, todo, on_done, on_error):
    """Claude calls run CLAUDE_PARALLEL at a time in threads; building, Overleaf and checks run here (one at a
    time). Each job: attempt 1, and one retry with the exact failure. The best passing attempt is kept.
    -> usage-limit message if Claude's limit was hit (the remaining jobs simply stay queued)."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    jobs = {hid: dict(e=e, jd=jd, attempt=0, prev=None, feedback=None, best=None, last=None)
            for hid, e, jd in todo}
    queue, pending, limit = list(jobs), {}, None
    with ThreadPoolExecutor(max_workers=CLAUDE_PARALLEL) as pool:
        while queue or pending:
            while queue and not limit and len(pending) < CLAUDE_PARALLEL:
                hid = queue.pop(0)
                j = jobs[hid]
                j["attempt"] += 1
                log("job %s: %s — %s (attempt %d)" % (hid, j["e"]["company"], j["e"]["title"], j["attempt"]))
                pending[pool.submit(ask_claude, ctx["rules"], ctx["main"], j["jd"], j["prev"], j["feedback"])] = hid
            if not pending:
                break
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for fut in done:
                hid = pending.pop(fut)
                j = jobs[hid]
                try:
                    spec = fut.result()
                except UsageLimit as ex:
                    limit = str(ex)
                    continue
                except Exception as ex:  # noqa: BLE001
                    on_error(hid, j["e"], ex)
                    continue
                try:
                    res, tex, pdf = check_spec(ctx, spec)
                except Exception as ex:  # noqa: BLE001 - Overleaf hiccup etc.: retried on a later run
                    on_error(hid, j["e"], ex)
                    continue
                log("  %s attempt %d: %s, bottom %s, problems %s" % (
                    j["e"]["company"], j["attempt"], "PASS" if res.get("PASS") else "FAIL", res.get("text_bottom"),
                    res.get("layout_problems") or res.get("diff_violations") or res.get("never_claim_hits")
                    or res.get("build_error") or res.get("compile_status") or ""))
                j["last"] = (res, tex, pdf, spec)
                if res.get("PASS") and (j["best"] is None or quality(res) >= quality(j["best"][0])):
                    j["best"] = j["last"]
                if (res.get("PASS") and res.get("FILL_OK") and res.get("SKILLS_FULL", True)) \
                        or j["attempt"] >= MAX_ATTEMPTS:
                    res, tex, pdf, spec = j["best"] or j["last"]
                    ok = bool(res.get("PASS"))
                    on_done(hid, j["e"], j["jd"], dict(status="ready" if ok else "needs you", tex=tex,
                                                       pdf=pdf if ok else None, check=res, spec=spec,
                                                       attempts=j["attempt"], fill_ok=bool(res.get("FILL_OK"))))
                else:
                    j["prev"] = spec
                    j["feedback"] = {k: v for k, v in res.items() if k != "bullets_not_in_existing_files"}
                    queue.insert(0, hid)                 # retry next, while the other call keeps running
    return limit


FOLDERS = {"pdf": "PDF RESUMES", "tex": "LATEX", "jd": "JOB DESCRIPTIONS"}


def save_outputs(job, jd, out, stem):
    """PDFs, .tex and JD .md files each go into their own folder inside RESUMES_2."""
    dirs = {k: os.path.join(OUT, v) for k, v in FOLDERS.items()}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    suffix = "" if out["status"] == "ready" else "_NEEDS_REVIEW"
    files = {}
    if out.get("tex"):
        files["tex"] = os.path.join(dirs["tex"], stem + suffix + ".tex")
        with open(files["tex"], "w", encoding="utf-8") as f:
            f.write(out["tex"])
    if out.get("pdf"):
        files["pdf"] = os.path.join(dirs["pdf"], stem + ".pdf")
        with open(files["pdf"], "wb") as f:
            f.write(out["pdf"])
    files["jd"] = os.path.join(dirs["jd"], stem + ".md")
    with open(files["jd"], "w", encoding="utf-8") as f:
        f.write(jd.rstrip() + "\n" + (fit_section(out["spec"]) if out.get("spec") else ""))
    return {k: os.path.relpath(v, OUT) for k, v in files.items()}


# ---------------------------------------------------------------- table

COLUMNS = [("applied", "Applied", 9), ("status", "Status", 16), ("rank", "Rank", 6), ("company", "Company", 22),
           ("title", "Title (apply link)", 44), ("location", "Location", 24), ("sponsorship", "Sponsorship line", 60),
           ("pay", "Pay", 18), ("fit", "Fit", 15), ("pdf", "Resume PDF", 11), ("tex", "LaTeX", 8),
           ("jd", "Job description", 14), ("notes", "Notes", 45), ("mine", "My notes", 40), ("date", "Found", 11),
           ("hid", "id", 11)]
KEEP_COLUMNS = {"applied": "Applied", "mine": "My notes"}      # typed by Purna: survive every rebuild
STATES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
          "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
          "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
          "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
          "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
          "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
          "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
          "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
          "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
          "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY"}
US_WORDS = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}
REMOTE_WORDS = {"remote", "telecommuter", "telecommute", "virtual", "work from home", "anywhere"}


def short_location(raw):
    """'Bellevue, Washington; Vancouver, Canada' -> 'Bellevue, WA; Vancouver, Canada'. City kept, rest abbreviated."""
    if not raw:
        return ""
    if re.search(r"canary wharf|churchill place", raw, re.I):
        raw = re.sub(r"canary wharf[^;]*", "London, UK", raw, flags=re.I)
    out, seen = [], set()
    remote = False
    for part in re.split(r"\s*;\s*", raw):
        m = re.match(r"^([A-Za-z .]+?)\s+-\s+(.+)$", part)                 # "Florida - Deerfield Beach"
        if m and m.group(1).strip().lower() in STATES:
            part = "%s, %s" % (m.group(2), m.group(1))
        bits = [b.strip() for b in part.split(",") if b.strip()]
        bits = [b for b in bits if b.lower() not in US_WORDS]
        words = part.lower().split()
        if words and words[0] in REMOTE_WORDS:                              # "Telecommuter MA", "Remote"
            remote = True
            rest = [STATES.get(w, w.upper() if len(w) == 2 else w.title()) for w in words[1:] if w not in US_WORDS]
            if rest:
                out.append("Remote, " + " ".join(rest))
                remote = False
            continue
        bits = [STATES.get(b.lower(), b) if i or len(bits) == 1 else b for i, b in enumerate(bits)]  # city stays
        if len(bits) > 2:
            bits = bits[:2]
        if not bits:
            continue
        loc = ", ".join(bits)
        city = bits[0].lower()
        if city in seen:
            continue
        seen.add(city)
        out.append(loc)
    if remote:
        out.insert(0, "Remote, US")
    if not out:
        return "US" if re.search(r"united states|\bus\b|usa", raw, re.I) else raw
    return "; ".join(out[:4]) + (" +%d more" % (len(out) - 4) if len(out) > 4 else "")


def short_note(text, words=12):
    """One short line for the sheet (the full notes live in the JD file)."""
    t = re.sub(r"\*\*|`", "", text or "").strip()
    t = re.sub(r"^\(?\d+[.)]\s*", "", t)                                     # leading "1)" / "1."
    first = re.split(r"(?<=[.!?])\s|\s\d+[.)]\s|\n", t, maxsplit=1)[0].strip()
    w = first.split()
    short = " ".join(w[:words])
    if len(short) > 90:
        short = short[:88].rsplit(" ", 1)[0]
    return short + ("…" if short != first else "")
STATUS_LABEL = {"ready": "📄 ready", "needs you": "⚠️ needs you", "queued": "⏳ making resume"}


def jd_fields(jd):
    """Header bullets the cloud listener wrote into the JD file."""
    def get(label):
        m = re.search(r"^- \*\*%s:\*\* (.*)$" % re.escape(label), jd or "", re.M)
        return m.group(1).strip() if m else ""
    spons = get("Sponsorship wording")
    return {"location": get("Location"), "pay": get("Pay"),
            "sponsorship": spons.strip('"') if spons and spons != "none found" else "none found in posting"}


def _previous_typed():
    """Keep whatever Purna typed in the Applied / My notes columns (xlsx edited in Excel, or the csv)."""
    typed = {k: {} for k in KEEP_COLUMNS}
    xlsx = os.path.join(OUT, "TO_APPLY.xlsx")
    if os.path.exists(xlsx):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(xlsx, read_only=True)
            for ws in wb.worksheets:
                rows = list(ws.iter_rows(values_only=True))
                if not rows:
                    continue
                hdr = list(rows[0])
                if "id" not in hdr:
                    continue
                for k, label in KEEP_COLUMNS.items():
                    if label in hdr:
                        for r in rows[1:]:
                            if r[hdr.index(label)] not in (None, ""):
                                typed[k][r[hdr.index("id")]] = str(r[hdr.index(label)])
        except Exception as e:  # noqa: BLE001 - a half-saved file must not stop the run
            log("could not read TO_APPLY.xlsx typed columns: %s" % e)
    csv_path = os.path.join(OUT, "TO_APPLY.csv")
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                for k, label in KEEP_COLUMNS.items():
                    if r.get("id") and r.get(label) and r["id"] not in typed[k]:
                        typed[k][r["id"]] = r[label]
    return typed


NUMBERS_FILE = "TO_APPLY.numbers"
NUMBERS_COLS = [("rank", "Rank", 50), ("title", "Title (apply link)", 330), ("applied", "Applied", 70),
                ("location", "Location", 170), ("sponsorship", "Sponsorship line", 230), ("fit", "Fit", 120),
                ("pay", "Pay", 130)]
NUMBERS_HASH = os.path.join(SUPPORT, "numbers_sheet.md5")


def _numbers_doc(path=None):
    import warnings
    warnings.filterwarnings("ignore", message="unsupported version")   # newer Numbers versions read/write fine
    from numbers_parser import Document
    return Document(path) if path else Document()


def _numbers_applied():
    """What Purna typed in the Applied column of TO_APPLY.numbers -> {url or 'Company — Title': value}.
    A row present in the file wins even when its Applied cell is empty (he may have cleared it)."""
    path = os.path.join(OUT, NUMBERS_FILE)
    marks = {}
    if not os.path.exists(path):
        return marks
    try:
        doc = _numbers_doc(path)
        for sheet in doc.sheets:
            for t in sheet.tables:
                rows = t.rows()
                if not rows:
                    continue
                hdr = [str(c.value or "") for c in rows[0]]
                if "Applied" not in hdr or not any(h.startswith("Title") for h in hdr):
                    continue
                ti, ai = [i for i, h in enumerate(hdr) if h.startswith("Title")][0], hdr.index("Applied")
                for r in rows[1:]:
                    cell = r[ti]
                    text = str(cell.value or "").strip()
                    if not text:
                        continue
                    m = re.search(r'HYPERLINK\("([^"]+)"', cell.formula or "") if cell.formula else None
                    links = getattr(cell, "hyperlinks", None) or []
                    url = m.group(1) if m else (links[0][1] if links else "")
                    v = r[ai].value
                    v = "" if v in (None, False) else ("Done" if v is True else str(v).strip())
                    marks[text] = v
                    if url:
                        marks[url] = v
    except Exception as e:  # noqa: BLE001 - a half-saved file must not stop the run
        log("could not read %s: %s" % (NUMBERS_FILE, e))
    return marks


def short_sponsorship(text):
    """Sponsorship sentence -> a few words for the sheet (the full sentence stays in the JD file)."""
    t = (text or "").strip()
    if not t or t.startswith("none found"):
        return "none found"
    m = re.search(r"track record of offering H1B sponsorships, with (\d+) in (\d{4})", t)
    if m:
        return "H-1B history: %s in %s" % (m.group(1), m.group(2))
    if re.search(r"student visa", t, re.I) and re.search(r"unable|not|cannot|can't", t, re.I):
        return "❌ No student visas"
    if re.search(r"not (be )?able to (provide|offer)|not available|unable to|does not sponsor|do not sponsor|cannot "
                 r"sponsor|will not sponsor|no (visa )?sponsorship|not provide (visa )?sponsorship", t, re.I):
        return "❌ No sponsorship"
    if re.search(r"will (provide|offer|sponsor)|accept applications from candidates who require|sponsorship is "
                 r"available", t, re.I):
        return "✅ Sponsors" + (" (UK visa)" if re.search(r"\bUK\b", t) else "")
    if re.search(r"select yes|require .{0,40}sponsor", t, re.I):
        return "Asks if you need sponsorship"
    if re.search(r"authori[sz]ed to work", t, re.I):
        return "US work authorization required"
    return short_note(t, 8)


def _numbers_title(r):
    return ("%s — %s" % (r["company"], r["title"])).replace('"', "'")


def write_numbers(rows):
    """TO_APPLY.numbers (the file Purna opens): tabs Target / Non-target, 7 columns, title = apply link.
    Rewritten only when its content changes, so an open copy in Numbers isn't disturbed every 5 minutes."""
    from numbers_parser import RGB, Style
    data = [[r["rank"], _numbers_title(r), r["url"], r["applied"], r["location"], r["sponsorship_short"],
             r["fit_cell"], r["pay"]] for r in rows]
    digest = hashlib.md5(json.dumps(data, ensure_ascii=False, default=str).encode()).hexdigest()
    path = os.path.join(OUT, NUMBERS_FILE)
    stamp = lambda: "%s %s" % (digest, os.path.getmtime(path))       # content + the exact file we wrote
    if os.path.exists(path) and os.path.exists(NUMBERS_HASH) and open(NUMBERS_HASH).read() == stamp():
        return                                              # unchanged and not re-saved by Numbers since
    doc = _numbers_doc()
    head = Style(bold=True, bg_color=RGB(230, 230, 230))
    done = Style(font_color=RGB(158, 158, 158), strikethrough=True)
    for n, (name, subset) in enumerate((("Target", [r for r in rows if r["rank"]]),
                                        ("Non-target", [r for r in rows if not r["rank"]]))):
        if n == 0:
            sheet = doc.sheets[0]
            sheet.name = name
        else:
            doc.add_sheet(name)
            sheet = doc.sheets[n]
        t = sheet.tables[0]
        for c, (_, label, width) in enumerate(NUMBERS_COLS):
            t.write(0, c, label, style=head)
            t.col_width(c, width)
        for i, r in enumerate(subset, 1):
            # Rank as text: the library stores the number 25 as 25.000000000000004
            vals = {"rank": str(int(r["rank"])) if r["rank"] else "", "title": _numbers_title(r), "applied": r["applied"],
                    "location": r["location"], "sponsorship": r["sponsorship_short"], "fit": r["fit_cell"],
                    "pay": r["pay"]}
            for c, (k, _, _) in enumerate(NUMBERS_COLS):
                t.write(i, c, vals[k])
                if r["applied"]:
                    t.set_cell_style(i, c, done)
            if r["url"]:
                t.cell(i, 1).formula = 'HYPERLINK("%s","%s")' % (r["url"].replace('"', "%22"), vals["title"])
        if t.num_cols > len(NUMBERS_COLS):                      # new tables start with spare empty columns/rows
            t.delete_column(num_cols=t.num_cols - len(NUMBERS_COLS), start_col=len(NUMBERS_COLS))
        if t.num_rows > len(subset) + 1:
            t.delete_row(num_rows=t.num_rows - len(subset) - 1, start_row=len(subset) + 1)
    tmp = os.path.join(OUT, ".TO_APPLY.tmp.numbers")
    doc.save(tmp)
    os.replace(tmp, path)
    with open(NUMBERS_HASH, "w") as f:
        f.write(stamp())


def write_table(ledger):
    """TO_APPLY.xlsx (opens in Numbers/Excel): tabs Target and Non-target. Also TO_APPLY.csv."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    typed = _previous_typed()
    marks = _numbers_applied()
    rows = []
    for hid, e in list(ledger.get("queue", {}).items()) + list(ledger["jobs"].items()):
        if e.get("status") not in STATUS_LABEL:
            continue
        files = e.get("files", {})
        url, ntitle = e.get("url") or "", _numbers_title({"company": e.get("company", ""), "title": e.get("title", "")})
        applied = marks[url] if url and url in marks else marks.get(ntitle, typed["applied"].get(hid, ""))
        spons = e.get("sponsorship", "")
        rows.append({"applied": applied, "mine": typed["mine"].get(hid, ""),
                     "sponsorship_short": short_sponsorship(spons),
                     "fit_cell": (e.get("fit") if e["status"] == "ready" else "") or STATUS_LABEL[e["status"]],
                     "status": STATUS_LABEL[e["status"]], "rank": e.get("rank") or "",
                     "company": e.get("company", ""), "title": e.get("title", ""),
                     "location": short_location(e.get("location", "")),
                     "sponsorship": e.get("sponsorship", ""), "pay": e.get("pay", ""), "fit": e.get("fit", ""),
                     "url": e.get("url", ""), "pdf": files.get("pdf", ""), "tex": files.get("tex", ""),
                     "jd": files.get("jd", ""), "notes": e.get("key_note") or short_note(e.get("notes", "")), "date": e.get("date", ""), "hid": hid})
    order = {"📄 ready": 0, "⏳ making resume": 1, "⚠️ needs you": 2}
    rows.sort(key=lambda r: (r["applied"] != "", order.get(r["status"], 3), int(r["rank"] or 999), r["date"]))
    with open(os.path.join(OUT, "TO_APPLY.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([h for _, h, _ in COLUMNS] + ["Apply link"])          # csv has no hyperlinks: keep the URL
        w.writerows([[r[k] for k, _, _ in COLUMNS] + [r["url"]] for r in rows])
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    fills = {"📄 ready": "E8F5E9", "⏳ making resume": "FFF8E1", "⚠️ needs you": "FFEBEE"}
    for name, subset in (("Target", [r for r in rows if r["rank"]]), ("Non-target", [r for r in rows if not r["rank"]])):
        ws = wb.create_sheet(name)
        ws.append([h for _, h, _ in COLUMNS])
        for c in ws[1]:
            c.font = Font(bold=True)
        for i, (_, _, width) in enumerate(COLUMNS):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i + 1)].width = width
        ws.freeze_panes = "D2"
        for r in subset:
            ws.append([r[k] for k, _, _ in COLUMNS])
            row = ws.max_row
            for k, label in (("title", r["title"]), ("pdf", "PDF"), ("tex", ".tex"), ("jd", "JD")):
                col = [c for c, _, _ in COLUMNS].index(k) + 1
                target = r["url"] if k == "title" else (("file://" + os.path.join(OUT, r[k]).replace(" ", "%20")) if r[k] else "")
                if target:
                    cell = ws.cell(row=row, column=col, value=label)
                    cell.hyperlink = target
                    cell.font = Font(color="0563C1", underline="single")
            ws.cell(row=row, column=2).fill = PatternFill("solid", fgColor=fills.get(r["status"], "FFFFFF"))
            if r["applied"]:
                for c in ws[row]:
                    c.font = Font(color="9E9E9E", strike=True)
    try:
        write_numbers(rows)
    except Exception as e:  # noqa: BLE001 - the xlsx backup is still written
        log("could not write %s: %s" % (NUMBERS_FILE, e))
    tmp = os.path.join(OUT, ".TO_APPLY.tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, os.path.join(OUT, "TO_APPLY.xlsx"))
    old_html = os.path.join(OUT, "TO_APPLY.html")
    if os.path.exists(old_html):
        os.remove(old_html)


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


class _Session:
    """Closes an already-entered Overleaf session at the end of the with-block."""
    def __init__(self, ol):
        self.ol = ol

    def __enter__(self):
        return self.ol

    def __exit__(self, *exc):
        self.ol.__exit__(*exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", help="force this starred job id")
    ap.add_argument("--jd", nargs="+", help="local JD .md file(s) to process (test)")
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
    ledger = load_json(LEDGER, {"jobs": {}})
    if md5 != MAIN_MD5:
        alert_once(ledger, "main_md5", "Resume runner stopped", "MAIN.tex changed (md5). Resumes paused until the "
                   "runner is updated for the new MAIN.tex.")
        log("MAIN.tex md5 %s != %s: stopping (rulebook says tell Purna)" % (md5, MAIN_MD5))
        return

    # ---- which jobs
    todo = []
    for path in args.jd or []:
        jd = open(path, encoding="utf-8").read()
        m = re.search(r"^# (.+?) — (.+)$", jd, re.M)
        km = re.search(r"\*\*Job key:\*\* `([^`]+)`", jd)
        um = re.search(r"\*\*Link:\*\* (\S+)", jd)
        rm = re.search(r"TARGET #(\d+)", jd)
        key = km.group(1) if km else os.path.basename(path)
        hid = hashlib.sha1(key.encode()).hexdigest()[:10]
        todo.append((hid, {"company": m.group(1), "title": m.group(2), "key": key,
                           "rank": int(rm.group(1)) if rm else None, "url": um.group(1) if um else ""}, jd))
    if not args.jd:
        try:
            stars = json.loads(state_file("stars.json"))
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
        ledger["queue"] = {}
        write_table(ledger)
        return
    # every starred job shows up in the sheet right away ("making resume"), with location / sponsorship / pay
    queue = {}
    for i, (hid, e, jd) in enumerate(todo):
        if jd is None:
            try:
                jd = fetch(STATE_RAW + "jds/%s.md" % hid)
            except Exception as ex:  # noqa: BLE001 - the job is retried on a later run
                log("JD file for %s not available yet: %s" % (hid, ex))
        todo[i] = (hid, e, jd)
        queue[hid] = dict(jd_fields(jd), status="queued", company=e["company"], title=e["title"], rank=e.get("rank"),
                          url=e.get("url"), date=dt.date.today().isoformat())
    ledger["queue"] = queue
    write_table(ledger)
    todo = [t for t in todo if t[2]][:MAX_JOBS_PER_RUN]
    log("%d starred job(s) to process" % len(todo))

    import overleaf
    rt = resume_tool()
    main_tex = open(MAIN, encoding="utf-8").read()
    rules = open(RULES, encoding="utf-8").read()
    bank = rt.bank_from(LATEX_FILES)
    made = []
    try:
        ol_session = overleaf.Overleaf(headless=not args.headed).__enter__()
    except overleaf.LoginNeeded as ex:
        alert_once(ledger, "overleaf_login", "Overleaf login needed",
                   "Resumes are paused. Double-click 'Overleaf login.command' in JOB-LISTENER/mac and solve the "
                   "image check once.")
        log("Overleaf session expired: %s" % ex)
        return

    def on_done(hid, e, jd, out):
        stem = stem_for(e["company"], e["title"], e.get("key", hid))
        files = save_outputs(e, jd, out, stem)
        fit = out["spec"]["fit"] if out.get("spec") else {}
        note = out["spec"].get("notes", "") if out.get("spec") else ""
        key = short_note(out["spec"].get("key_note") or note) if out.get("spec") else ""
        if out["status"] == "ready" and not out["fill_ok"]:
            note = ("page not fully filled (bottom %s pt); " % out["check"].get("text_bottom")) + note
            key = "Page not fully filled. " + key
        if out["status"] != "ready":
            c = out["check"]
            why = ((c.get("layout_problems") or []) + (c.get("diff_violations") or []) + (c.get("never_claim_hits") or [])
                   + ([c["build_error"]] if c.get("build_error") else [])
                   + (["bottom %s" % c.get("text_bottom")] if c.get("text_bottom") else []))
            note = "check failed: %s; %s" % ("; ".join(str(x) for x in why[:4]), note)
            key = short_note("Check failed: " + str(why[0]) if why else "Check failed.")
        ledger["jobs"][hid] = dict(jd_fields(jd), status=out["status"], company=e["company"], title=e["title"],
                                   rank=e.get("rank"), url=e.get("url"), date=dt.date.today().isoformat(),
                                   fit=fit.get("rating", ""), files=files, notes=note[:400], key_note=key,
                                   attempts=out["attempts"],
                                   check=out["check"])
        made.append((out["status"], e["company"]))
        log("  -> %s %s" % (out["status"], files))
        ledger["queue"].pop(hid, None)
        save_json(LEDGER, ledger)
        write_table(ledger)                             # the sheet updates after every finished resume

    def on_error(hid, e, ex):
        log("  ERROR %s: %s" % (hid, ex))
        errors = ledger.setdefault("errors", {})
        errors[hid] = errors.get(hid, 0) + 1
        if errors[hid] >= 3:                            # transient errors retry on later runs, then give up
            ledger["jobs"][hid] = {"status": "needs you", "company": e["company"], "title": e["title"],
                                   "rank": e.get("rank"), "url": e.get("url"), "date": dt.date.today().isoformat(),
                                   "notes": "error: %s" % str(ex)[:300]}
            ledger["queue"].pop(hid, None)
        save_json(LEDGER, ledger)
        write_table(ledger)

    with _Session(ol_session) as ol:
        ctx = dict(rt=rt, main=main_tex, rules=rules, ol=ol, bank=bank, ref=reference(ol, rt, main_tex, ledger))
        save_json(LEDGER, ledger)
        limit = run_jobs(ctx, todo, on_done, on_error)
    if limit:
        alert_once(ledger, "claude_limit", "Claude usage limit reached",
                   "Resumes paused; %d starred job(s) still queued. They continue automatically when the limit "
                   "resets." % len(ledger.get("queue", {})))
        log("Claude usage limit: %s" % limit)
    ready = sum(1 for st, _ in made if st == "ready")
    if made:
        summary = ", ".join(c for _, c in made)[:150] + ((" · %d need you" % (len(made) - ready))
                                                          if len(made) > ready else "")
        notify_mac("Resumes ready: %d" % ready, summary)
        notify_telegram("📄 Resumes ready: %d — %s\nOpen INTERNSHIP/RESUMES_2/TO_APPLY.xlsx" % (ready, summary))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001 - tell Purna instead of failing silently every 5 minutes
        import traceback
        traceback.print_exc()
        alert_once(load_json(LEDGER, {"jobs": {}}), "crash", "Resume runner error", str(ex)[:200])
        raise
