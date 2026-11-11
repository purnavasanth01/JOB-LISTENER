#!/usr/bin/env python3
"""
JOB-LISTENER: alerts for new Spring/Summer/Fall 2027 internships.

  Listener 1  own career sites   Amazon, Google, Microsoft, Netflix, Oracle-Cloud sites (Oracle, Goldman,
                                 Uber, Dell, JPMorgan, Amex), AMD, Rivian
  Listener 2  job systems        Workday, Greenhouse, Lever, Ashby, SmartRecruiters (targets.json)
  Listener 3  checker            Simplify + vanshb03 + Jobright GitHub lists. Catches anything 1/2 missed,
                                 covers companies that block scripts (Meta, Apple, Tesla, TikTok, ...).

One run = fetch everything, alert only on jobs never seen before, save state. GitHub Actions runs it every 3 min.

  python main.py --state-dir state             normal run
  python main.py --state-dir /tmp/s --no-notify  print alerts instead of sending
  python main.py --dry-run --only workday:nvidia   test one source without saving anything
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import notify
import rules
import sources

ROOT = os.path.dirname(os.path.abspath(__file__))
OWN_TYPES = {"amazon", "google", "microsoft", "eightfold", "orc", "phenom"}          # Listener 1
L3_SPECS = [{"type": "simplify"}, {"type": "vansh"}, {"type": "jobright"}]          # Listener 3
DETAIL_BUDGET = 150         # description fetches per run; the rest wait for the next run
DETAIL_LANES = 8            # websites read in parallel; each website still gets one request at a time
RUN_BUDGET = 200            # seconds; after this, remaining descriptions wait for the next run
BROKEN_AFTER = 3600         # alert when a source has failed for 1 hour...
BROKEN_AFTER_SLOW = 86400   # ...or 1 day for naturally flaky sources (Microsoft 429s, Google HTML)
REKEY_MIN = 50              # this many "new" jobs at once from one source = probably the site changed ids
DUP_WINDOW_DAYS = 45        # same company + near-identical title alerted within this window = duplicate
HEARTBEAT_HOUR = 9          # daily "listener alive" message after 9 AM Central
MAX_CSV_ROWS = 20000
CSV_FIELDS = ["found_at", "company", "rank", "title", "role", "location", "tags", "layer", "source", "posted", "url",
              "reason", "evidence"]
VAGUE_MIN_TECH_WORDS = 4    # vague title is kept when its description has this many distinct tech words
STAR_KEEP_DAYS = 60
SOURCE_NAMES = {"workday": "Workday", "greenhouse": "Greenhouse", "ashby": "Ashby", "lever": "Lever",
                "smartrecruiters": "SmartRecruiters", "orc": "Oracle Cloud", "amazon": "Amazon site",
                "google": "Google site", "microsoft": "Microsoft site", "eightfold": "Netflix site",
                "phenom": "careers site", "simplify": "Simplify list", "vansh": "vanshb03 list",
                "jobright": "Jobright list"}
PAY_RE = __import__("re").compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kK]?(?:\s?(?:-|–|to)\s?\$?\s?\d[\d,]*(?:\.\d+)?\s?[kK]?)?"
                                  r"(?:\s?(?:/|per)\s?(?:hour|hr|year|yr|month|week))?")
TERM_RE = __import__("re").compile(r"\b(spring|summer|fall|autumn|winter)\s*(?:20)?27\b", __import__("re").I)
# drops worth a second look by Claude during the resume run (judgment calls, not obvious drops)
BORDERLINE = ("requires", "ITAR", "F-1", "sponsorship", "H-1B", "PhD", "undergrad", "vague title")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def central_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago"))
    except Exception:  # noqa: BLE001
        return utcnow() - timedelta(hours=5)


# ---------------------------------------------------------------- state

class State:
    FILES = {"seen": {}, "health": {}, "meta": {}, "pending": [], "unsent": [], "stars": {}}

    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)
        for name, default in self.FILES.items():
            setattr(self, name, self._load(name, default))

    def _load(self, name, default):
        p = os.path.join(self.path, name + ".json")
        if not os.path.exists(p):
            return type(default)()
        with open(p) as f:
            return json.load(f)   # a corrupt state file must stop the run, never silently re-seed

    def save(self):
        for name in self.FILES:
            p = os.path.join(self.path, name + ".json")
            with open(p + ".tmp", "w") as f:
                json.dump(getattr(self, name), f, separators=(",", ":"), sort_keys=True)
            os.replace(p + ".tmp", p)

    def append_csv(self, name, rows):
        if not rows:
            return
        p = os.path.join(self.path, name)
        new = not os.path.exists(p)
        if not new:
            with open(p, newline="") as f:
                header = next(csv.reader(f), [])
            if header != CSV_FIELDS:              # columns were added: rewrite once with the new header
                with open(p, newline="") as f:
                    old = list(csv.DictReader(f))
                with open(p, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(old)
        with open(p, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(rows)
        with open(p) as f:
            lines = f.readlines()
        if len(lines) > MAX_CSV_ROWS + 1:
            with open(p, "w") as f:
                f.writelines([lines[0]] + lines[-MAX_CSV_ROWS:])


# ---------------------------------------------------------------- matching index

class Index:
    """Finds a previously seen job by URL/ID keys or by company + near-identical title."""

    def __init__(self):
        self.by_key = {}
        self.by_company = {}
        self.outcome = {}

    def add(self, key, entry):
        self.outcome[key] = entry.get("o") or ""
        for k in entry.get("k") or []:
            self.by_key.setdefault(k, key)
        self.by_key.setdefault(key, key)
        self.by_company.setdefault(entry.get("c"), []).append(
            (rules.title_tokens(entry.get("ti")), key, entry.get("t"), entry.get("o"), entry.get("l")))

    def match(self, cid, tokens, ukeys, only_alerted_since=None):
        """only_alerted_since set = alert-time check: the same URL/ID already alerted or in the backlog blocks
        forever; a near-identical title blocks only if it was alerted after that date. Dropped entries never
        block (a list may drop a job its own company site shows with better details)."""
        for k in ukeys:
            hit = self.by_key.get(k)
            if hit and not (only_alerted_since is not None and "drop" in self.outcome.get(hit, "")):
                return hit
        for toks, key, t, outcome, layer in self.by_company.get(cid, []):
            if only_alerted_since is not None:
                if outcome != "alert" or (t or "") < only_alerted_since:
                    continue
            if rules.similar_titles(tokens, toks):
                return key
        return None


# ---------------------------------------------------------------- the run

class Run:
    def __init__(self, args):
        self.args = args
        self.t0 = time.time()
        self.now = utcnow()
        self.now_s = iso(self.now)
        self.logs = []
        self.st = State(args.state_dir)
        self.index = Index()
        for k, e in self.st.seen.items():
            self.index.add(k, e)
        self.targets, self.alias = self._load_targets()
        self.status = []                  # status lines (broken / recovered / seeded) sent once per run
        self.rows = {"alerts.csv": [], "dropped.csv": [], "missed.csv": [], "backlog.csv": [], "borderline.csv": []}
        self.backlog_fps = set()
        self.alerts = []
        self.groups = {}
        self.source_ok = {}
        self.details_used = 0
        self.jd_files = {}             # hid -> JD markdown (written to state/jds/ at the end of the run)

    # ---------------- helpers
    def log(self, msg):
        line = "%s %s" % (utcnow().strftime("%H:%M:%S"), msg)
        self.logs.append(line)
        print(line, flush=True)

    def _load_targets(self):
        with open(os.path.join(ROOT, "targets.json")) as f:
            comps = json.load(f)["companies"]
        alias = {}
        for t in comps:
            t["cid"] = rules.norm_company(t["name"])
            for n in [t["name"]] + list(t.get("aliases") or []):
                alias[rules.norm_company(n)] = t
        return comps, alias

    def resolve(self, name):
        t = self.alias.get(rules.norm_company(name))
        return (t["cid"], t) if t else (rules.norm_company(name), None)

    def keys_of(self, job):
        return sorted(rules.url_keys(job.get("url")) | {job["key"]})

    def record(self, job, outcome, reason=None):
        e = {"t": self.now_s, "c": job["cid"], "ti": (job.get("title") or "")[:140], "k": self.keys_of(job),
             "o": outcome, "l": job["layer"], "s": job["source"]}
        if reason:
            e["r"] = reason[:60]
        self.st.seen[job["key"]] = e
        self.index.add(job["key"], e)
        self.st.meta.get("detail_fails", {}).pop(job["key"], None)

    def row(self, job, role="", tags=(), reason="", location=None, evidence=""):
        t = job.get("target")
        return {"found_at": central_now().strftime("%Y-%m-%d %H:%M"), "company": job["company"],
                "rank": t["rank"] if t else "", "title": job.get("title"), "role": role,
                "location": location if location is not None else "; ".join(job.get("locations") or [])[:120],
                "tags": ", ".join(tags), "layer": job["layer"], "source": job["source"],
                "posted": job.get("posted") or "", "url": job.get("url") or "", "reason": reason,
                "evidence": evidence}

    def drop(self, job, reason, role="", evidence=""):
        self.record(job, "drop", reason)
        row = self.row(job, role, reason=reason, evidence=evidence)
        self.rows["dropped.csv"].append(row)
        if any(b in reason for b in BORDERLINE):
            self.rows["borderline.csv"].append(row)

    @staticmethod
    def listener_label(job):
        kind = job["source"].split(":")[0]
        return "Listener %s (%s)" % (job["layer"][-1], SOURCE_NAMES.get(kind, kind))

    # ---------------- filters
    def prefilter(self, job, allow_vague=False):
        """Title / list-level checks. -> (role, None) or (None, reason).
        allow_vague: a title that is merely unclear ("not a software role") is kept for the description check."""
        title = job.get("title") or ""
        r = rules.category_excluded(job.get("category"), title)
        if r:
            return None, r
        role, r = rules.classify_role(title, job.get("category"))
        if r == "not a software role" and allow_vague:
            job["vague"], role, r = True, "Vague title", None
        if r:
            return None, r
        r = (rules.term_check(title, job.get("terms"), job.get("season"))
             or rules.location_check(job.get("locations"))
             or rules.hard_drop(title, None, job.get("degrees"), job.get("sponsorship")))
        if r:
            return None, r
        return role, None

    # ---------------- fetching
    def fetch_all(self):
        specs = []
        only = self.args.only
        for t in self.targets:
            for spec in t.get("sources") or []:
                specs.append((spec, t))
        for spec in L3_SPECS:
            specs.append((spec, None))
        if only:
            specs = [(s, t) for s, t in specs if sources.source_id(s).startswith(only)]

        def one(spec, target):
            sid = sources.source_id(spec)
            h = self.st.health.setdefault(sid, {})
            started = time.time()
            try:
                jobs = sources.LISTERS[spec["type"]](spec, h, target["name"] if target else None)
                return sid, spec, target, jobs, None, time.time() - started
            except Exception as e:  # noqa: BLE001 - one broken source must never stop the others
                return sid, spec, target, None, e, time.time() - started

        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(lambda st: one(*st), specs))
        for sid, spec, target, jobs, err, secs in results:
            self.update_health(sid, spec, jobs, err)
            self.log("%-46s %s (%.1fs)" % (sid, ("%d jobs" % len(jobs)) if jobs is not None and not err else
                                           ("unchanged" if not err else "ERROR %s" % str(err)[:90]), secs))
        return results

    def update_health(self, sid, spec, jobs, err):
        h = self.st.health.setdefault(sid, {})
        if err is None:
            if h.get("broken_alerted"):
                self.status.append("✅ %s is working again" % sid)
            h.update(ok_at=self.now_s, fails=0, broken_alerted=False)
            h.pop("first_fail", None)
            h.pop("last_err", None)
            if jobs is not None:
                h["count"] = len(jobs)
                if jobs:
                    h.pop("zero_since", None)
                else:
                    h.setdefault("zero_since", self.now_s)
            self.source_ok[sid] = True
            return
        self.source_ok[sid] = False
        h["fails"] = h.get("fails", 0) + 1
        h.setdefault("first_fail", self.now_s)
        h["last_err"] = ("%s: %s" % (type(err).__name__, err))[:200]
        limit = BROKEN_AFTER_SLOW if spec["type"] in sources.SLOW_ALARM else BROKEN_AFTER
        first = parse_iso(h["first_fail"]) or self.now
        if not h.get("broken_alerted") and (self.now - first).total_seconds() >= limit:
            h["broken_alerted"] = True
            self.status.append("❌ %s failing since %s: %s" % (sid, h["first_fail"], h["last_err"][:120]))

    # ---------------- seeding (first time a source is seen: no alerts, everything to backlog.csv)
    def seed(self, jobs):
        n = 0
        for j in jobs:
            role, reason = self.prefilter(j)
            self.record(j, "seed" if not reason else "seed-drop", reason)
            if reason:
                continue
            fp = (j["cid"], rules.title_tokens(j.get("title")))
            if fp in self.backlog_fps:
                continue
            self.backlog_fps.add(fp)
            tags = ["desc not checked"] + (["⚠ " + j["target"]["flag"]] if j.get("target") and j["target"].get("flag") else [])
            self.rows["backlog.csv"].append(self.row(j, role, tags))
            n += 1
        return n

    # ---------------- main processing
    def process(self, results):
        candidates = []
        seeded_now = []
        l3 = []
        for sid, spec, target, jobs, err, _ in results:
            if err is not None or jobs is None:
                continue
            if target is None:
                l3.append((sid, jobs))
                continue
            layer = "L1" if spec["type"] in OWN_TYPES else "L2"
            for j in jobs:
                j.update(layer=layer, source=sid, cid=target["cid"], company=target["name"], target=target)
            h = self.st.health[sid]
            new = [j for j in jobs if j["key"] not in self.st.seen]
            if not h.get("seeded"):
                seeded_now.append((sid, self.seed(new)))
                h["seeded"] = True
                continue
            if len(new) >= REKEY_MIN:
                known = sum(1 for j in new if self.index.match(j["cid"], rules.title_tokens(j["title"]), self.keys_of(j)))
                if known >= 0.8 * len(new):
                    for j in new:
                        self.record(j, "rekey")
                    self.status.append("ℹ️ %s: %d jobs got new ids (site change?) — treated as already seen" % (sid, len(new)))
                    continue
            candidates.extend(new)
        # register this run's L1/L2 candidates so Listener 3 rows for the same job are recognised
        run_index_keys = {}
        for j in candidates:
            for k in self.keys_of(j):
                run_index_keys.setdefault(k, j["key"])
        # Listener 3
        for sid, jobs in l3:
            h = self.st.health[sid]
            new = []
            for j in jobs:
                if j["key"] in self.st.seen:
                    continue
                cid, target = self.resolve(j["company"])
                j.update(layer="L3", source=sid, cid=cid, target=target,
                         company=target["name"] if target else j["company"])
                new.append(j)
            if not h.get("seeded"):
                seeded_now.append((sid, self.seed(new)))
                h["seeded"] = True
                continue
            for j in new:
                toks = rules.title_tokens(j["title"])
                keys = self.keys_of(j)
                hit = next((run_index_keys[k] for k in keys if k in run_index_keys), None)
                if not hit:
                    hit = next((c["key"] for c in candidates if c["layer"] != "L3" and c["cid"] == j["cid"]
                                and rules.similar_titles(toks, rules.title_tokens(c["title"]))), None)
                hit = hit or self.index.match(j["cid"], toks, keys)
                if hit:
                    self.record(j, "dup:" + hit)
                    continue
                t = j.get("target")
                if t and t.get("sources"):
                    direct = [sources.source_id(s) for s in t["sources"]]
                    healthy = all(self.source_ok.get(d) and self.st.health.get(d, {}).get("seeded") for d in direct)
                    j["l3_tag"] = "MISSED by L1/L2" if healthy else "via L3 (direct source down)"
                else:
                    j["l3_tag"] = "via L3"
                candidates.append(j)
                for k in keys:
                    run_index_keys.setdefault(k, j["key"])
        # retry jobs that waited for a description last run
        known = {c["key"] for c in candidates}
        for p in self.st.pending:
            if p["key"] not in self.st.seen and p["key"] not in known:
                cid, target = self.resolve(p["company"]) if p["layer"] == "L3" else (p["cid"], self._target_by_cid(p["cid"]))
                p["target"] = target
                candidates.append(p)
                known.add(p["key"])
        self.st.pending = []
        if seeded_now:
            total = sum(n for _, n in seeded_now)
            self.status.append("🟢 First look at %d source(s): %d relevant jobs saved to backlog.csv (no alerts for "
                               "these)." % (len(seeded_now), total))
        self.evaluate(candidates)

    def _target_by_cid(self, cid):
        return next((t for t in self.targets if t["cid"] == cid), None)

    def evaluate(self, candidates):
        since = iso(self.now - timedelta(days=DUP_WINDOW_DAYS))
        # targets first (by rank), then everything else
        candidates.sort(key=lambda j: ((j.get("target") or {}).get("rank", 999), j["layer"]))
        ready = []
        for j in candidates:                              # 1) cheap title / list checks
            if j["key"] in self.st.seen or any(r[0]["key"] == j["key"] for r in ready):
                continue
            role, reason = self.prefilter(j, allow_vague=True)
            if reason:
                self.drop(j, reason)
                continue
            ready.append((j, role))
        fetched = self.fetch_descriptions([j for j, _ in ready])   # 2) descriptions, in parallel per website
        for j, role in ready:                             # 3) the same checks as before, one job at a time
            desc, locs = j.get("desc"), None
            if desc is None and (j.get("detail") or j.get("aggregator")):
                got = fetched.get(j["key"])
                if got is None:                           # over budget / out of time: next run
                    self.defer(j)
                    continue
                desc, locs, err = got
                if err:
                    fails = self.st.meta.setdefault("detail_fails", {})
                    fails[j["key"]] = fails.get(j["key"], 0) + 1
                    self.log("description failed (%d) %s: %s" % (fails[j["key"]], j["key"], err))
                    if fails[j["key"]] < 3:
                        self.defer(j)
                        continue
                    desc = None   # alert anyway, tagged "desc not checked"
            flags = []
            if j.get("vague"):                            # second pass: unclear title, judge by the description
                n = rules.tech_word_count(desc)
                if n < VAGUE_MIN_TECH_WORDS:
                    reason = "not a software role (vague title, %d tech words in description)" % n
                    self.drop(j, reason if n >= 2 else "not a software role")
                    continue
                flags.append("🔍 vague title, %d tech words in description" % n)
            title = j.get("title") or ""
            reason = ((rules.location_check(locs) if locs else None)
                      or rules.term_check(title, j.get("terms"), j.get("season"), desc))
            if reason:
                self.drop(j, reason, role)
                continue
            elig = rules.check_eligibility(title, desc, j.get("degrees"), j.get("sponsorship"))
            if elig["drop"]:
                self.drop(j, elig["drop"], role, elig["evidence"])
                continue
            flags += elig["flags"]
            if not desc:
                flags.append("desc not checked")
            t = j.get("target")
            if t and t.get("flag"):
                flags.append("⚠ " + t["flag"])
            if j.get("l3_tag") and j["l3_tag"] != "via L3":
                flags.append(j["l3_tag"])
            toks = rules.title_tokens(j["title"])
            gkey = (j["cid"], toks)
            if gkey in self.groups:                      # same title, another location -> one alert
                self.groups[gkey]["count"] += 1
                self.record(j, "alert")
                continue
            dup = self.index.match(j["cid"], toks, self.keys_of(j), only_alerted_since=since)
            if dup:
                self.record(j, "dup:" + dup)
                continue
            loc = "; ".join(list(dict.fromkeys(l.strip() for l in (locs or j.get("locations") or []) if l))[:3])
            hid = hashlib.sha1(j["key"].encode()).hexdigest()[:10]
            a = {"key": j["key"], "hid": hid, "company": j["company"], "rank": t["rank"] if t else None,
                 "target": bool(t), "autostar": bool(t and t.get("autostar")),
                 "title": j["title"], "role": role, "location": loc, "tags": flags,
                 "line": elig["line"], "url": j.get("url"), "layer": j["layer"], "listener": self.listener_label(j),
                 "count": 1}
            self.groups[gkey] = a
            self.alerts.append(a)
            self.record(j, "alert")
            self.st.stars[hid] = {"key": j["key"], "company": j["company"], "title": j["title"], "url": j.get("url"),
                                  "location": loc, "rank": a["rank"], "listener": a["listener"], "role": role,
                                  "status": "starred" if a["autostar"] else "new", "alerted": self.now_s,
                                  "starred": self.now_s if a["autostar"] else None}
            self.jd_files[hid] = self.render_jd(j, a, desc)
            row = self.row(j, role, flags, location=loc, evidence=elig["line"] or "")
            self.rows["alerts.csv"].append(row)
            if j.get("l3_tag") == "MISSED by L1/L2":
                self.rows["missed.csv"].append(row)

    @staticmethod
    def _site(j):
        d = j.get("detail")
        if d and d[0] in ("greenhouse", "smartrecruiters"):
            return d[0]
        if d and d[0] in ("orc", "eightfold"):
            return d[1]
        url = d[1] if d and d[0] in ("workday", "html") else (j.get("url") or "")
        return urlparse(url).netloc or "other"

    def fetch_descriptions(self, jobs):
        """-> {key: (desc, locs, error)} for jobs that need a description. Websites run in parallel, but each
        website's jobs go one after another (never more than one request at a time to the same site)."""
        need = [j for j in jobs if j.get("desc") is None and (j.get("detail") or j.get("aggregator"))]
        need = need[:DETAIL_BUDGET]
        lanes = {}
        for j in need:
            lanes.setdefault(self._site(j), []).append(j)
        deadline = self.t0 + RUN_BUDGET
        out = {}

        def lane(js):
            for j in js:
                if time.time() > deadline:
                    return
                try:
                    desc, locs = sources.fetch_detail(j)
                    out[j["key"]] = (desc, locs, None)
                except Exception as e:  # noqa: BLE001
                    out[j["key"]] = (None, None, e)
                time.sleep(sources.PAUSE)
        if lanes:
            with ThreadPoolExecutor(max_workers=DETAIL_LANES) as ex:
                list(ex.map(lane, lanes.values()))
        self.details_used += len(out)
        return out

    def render_jd(self, j, a, desc):
        """The script-made part of the JD file (Step 4): header bullets + verbatim description + ATS keywords.
        The fit section (Meets / Gaps / Angle / Rating) is added later on the Mac by Claude."""
        t = j.get("target")
        text = desc or ""
        pay = sorted({m.group(0).strip() for m in PAY_RE.finditer(text)})[:3]
        terms = sorted({m.group(0).title() for m in TERM_RE.finditer(text + " " + j["title"])})
        kws = sorted({m.group(1) for m in rules.TECH_WORDS.finditer(text)}, key=str.lower)
        req = j["key"].split(":")[-1]
        lines = ["# %s — %s" % (j["company"], j["title"]), "",
                 "- **Job key:** `%s` (req/ID: %s)" % (j["key"], req),
                 "- **Employer:** %s%s" % (j["company"], " — TARGET #%s (H-1B sponsor list)" % t["rank"] if t else ""),
                 "- **Role family:** %s" % a["role"],
                 "- **Location:** %s" % (a["location"] or "not stated"),
                 "- **Pay:** %s" % (", ".join(pay) or "not stated"),
                 "- **Term:** %s" % (", ".join(terms) or "not stated (confirm Summer 2027)"),
                 "- **Posted:** %s · **Found:** %s CT" % (j.get("posted") or "?", central_now().strftime("%Y-%m-%d %H:%M")),
                 "- **Link:** %s" % (j.get("url") or ""),
                 "- **Found by:** %s" % a["listener"],
                 "- **Sponsorship wording:** %s" % (('"%s"' % a["line"]) if a.get("line") else "none found"),
                 "- **Flags:** %s" % (", ".join(a["tags"]) or "none"), "",
                 "## Job description (verbatim)", "", text.strip() or "_Description could not be read; open the link._",
                 "", "## ATS keywords", "", ", ".join(kws) or "none detected", ""]
        return "\n".join(lines)

    def write_jds(self):
        d = os.path.join(self.st.path, "jds")
        os.makedirs(d, exist_ok=True)
        for hid, md in self.jd_files.items():
            with open(os.path.join(d, hid + ".md"), "w") as f:
                f.write(md)
        keep = {h for h, e in self.st.stars.items() if e.get("status") != "removed"}
        for name in os.listdir(d):
            if name.endswith(".md") and name[:-3] not in keep:
                os.remove(os.path.join(d, name))

    def defer(self, j):
        p = {k: v for k, v in j.items() if k != "target"}
        self.st.pending.append(p)

    # ---------------- notifications
    def status_text(self):
        today = central_now().strftime("%Y-%m-%d")
        d = self.st.meta.get("days", {}).get(today, {})
        bad = sorted(s for s, v in self.source_ok.items() if not v)
        starred = sum(1 for e in self.st.stars.values() if e.get("status") == "starred")
        return ("<b>Listener status</b>\nLast check: %s CT\nSources OK: %d/%d%s\nToday so far: %d alerts, %d dropped, "
                "%d checks\n⭐ Starred jobs: %d\nJobs known: %d\nBacklog/alerts files: state branch on GitHub" % (
                    central_now().strftime("%b %d %H:%M"), len(self.source_ok) - len(bad), len(self.source_ok),
                    (" (down: " + ", ".join(bad[:5]) + ")") if bad else "", d.get("alerts", 0), d.get("dropped", 0),
                    d.get("runs", 0), starred, len(self.st.seen)))

    def on_button(self, action, hid):
        """⭐ = interested (one-way: never un-stars). ❌ = remove.
        Returns {"status", and how to redraw the message: "text"+"markup" | "markup" | "delete"} or None."""
        e = self.st.stars.get(hid)
        if not e:
            return None
        if action == "x":
            e["status"], e["removed"] = "removed", self.now_s
        elif action == "s" and e.get("status") != "removed":
            if e.get("status") != "starred":
                e["status"], e["starred"] = "starred", self.now_s
        self.log("button %s on %s (%s — %s) -> %s" % (action, hid, e["company"], e["title"][:40], e["status"]))
        mid = e.get("msg_id")
        group = sorted(((h, x) for h, x in self.st.stars.items() if mid and x.get("msg_id") == mid and "block" in x),
                       key=lambda hx: hx[1]["idx"])
        if not group:                                   # message from before grouping: one job per message
            if e["status"] == "removed":
                return {"status": e["status"], "delete": True}
            return {"status": e["status"], "markup": notify._keyboard(e.get("url"), hid, True)}
        if all(x.get("status") == "removed" for _, x in group):
            return {"status": e["status"], "delete": True}
        return {"status": e["status"], "text": notify.group_text(e["header"], group),
                "markup": notify.group_markup(group)}

    def prune_stars(self):
        cutoff = iso(self.now - timedelta(days=STAR_KEEP_DAYS))
        for hid in [h for h, e in self.st.stars.items() if (e.get("alerted") or "") < cutoff]:
            self.st.stars.pop(hid)

    def deliver(self):
        if not (self.args.dry_run or self.args.no_notify):
            notify.telegram_sync(self.st.meta, self.log, self.status_text, self.on_button)
        batch = self.st.unsent + self.alerts
        if not batch:
            return
        channels = notify.configured()
        if self.args.dry_run or self.args.no_notify or not any(channels.values()):
            why = "dry run" if self.args.dry_run else "--no-notify" if self.args.no_notify else "no NTFY_TOPIC/SMTP set"
            self.log("ALERTS (%s, not sent): %d" % (why, len(batch)))
            for a in batch:
                self.log("  • %s #%s | %s | %s | %s | %s | %s | 💬 %s" % (
                    a["company"], a.get("rank") or "-", a["title"], a.get("listener", a["layer"]), a.get("location"),
                    ", ".join(a["tags"]), a["url"], a.get("line") or "-"))
            # secrets not set yet: keep the alerts queued so they are delivered once they are
            self.st.unsent = [] if (self.args.dry_run or self.args.no_notify) else batch[-200:]
            return
        if notify.send_jobs(batch, self.log):
            self.log("sent %d alerts" % len(batch))
            for a in batch:
                if a.get("msg_id") and a.get("hid") in self.st.stars:
                    self.st.stars[a["hid"]].update({k: a[k] for k in ("msg_id", "idx", "block", "header") if k in a})
            self.st.unsent = []
        else:
            self.log("alert delivery FAILED; will retry next run")
            self.st.unsent = batch[-200:]

    def heartbeat(self):
        ct = central_now()
        today = ct.strftime("%Y-%m-%d")
        m = self.st.meta
        days = m.setdefault("days", {})
        d = days.setdefault(today, {"runs": 0, "alerts": 0, "dropped": 0, "missed": 0})
        d["runs"] += 1
        d["alerts"] += len(self.alerts)
        d["dropped"] += len(self.rows["dropped.csv"])
        d["missed"] += len(self.rows["missed.csv"])
        last = parse_iso(m.get("last_run"))
        if last and (self.now - last).total_seconds() > 1800:
            d["gaps"] = d.get("gaps", 0) + 1
            self.log("note: %d min since previous run" % ((self.now - last).total_seconds() // 60))
        m["last_run"] = self.now_s
        for k in sorted(days)[:-8]:
            days.pop(k)
        if m.get("heartbeat") == today or ct.hour < HEARTBEAT_HOUR or self.args.dry_run:
            return
        yday = (ct - timedelta(days=1)).strftime("%Y-%m-%d")
        y = days.get(yday, {})
        broken = [s for s, h in self.st.health.items() if h.get("broken_alerted")]
        zero = [s for s, h in self.st.health.items()
                if h.get("zero_since") and (self.now - (parse_iso(h["zero_since"]) or self.now)).days >= 1]
        body = ("Yesterday: %d runs, %d alerts, %d dropped, %d caught only by Listener 3, %d run gaps >30 min.\n"
                "Sources: %d OK, %d broken%s.%s" % (
                    y.get("runs", 0), y.get("alerts", 0), y.get("dropped", 0), y.get("missed", 0), y.get("gaps", 0),
                    sum(1 for v in self.source_ok.values() if v), len(broken),
                    (" (" + ", ".join(broken[:6]) + ")") if broken else "",
                    ("\nNo jobs for 24h+: " + ", ".join(zero[:8])) if zero else ""))
        if notify.send_status("✅ Job listener alive", body, self.log, priority="low") or not any(notify.configured().values()):
            m["heartbeat"] = today

    def finish(self):
        if self.status:
            msg = "\n".join(self.status)
            self.log("STATUS: " + msg.replace("\n", " | "))
            if not (self.args.dry_run or self.args.no_notify):
                notify.send_status("Job listener status", msg, self.log)
        if self.args.dry_run:
            self.log("dry run: state not saved")
            return
        for name, rows in self.rows.items():
            self.st.append_csv(name, rows)
        self.write_jds()
        self.st.save()
        with open(os.path.join(self.st.path, "last_run.log"), "w") as f:
            f.write("\n".join(self.logs) + "\n")

    def go(self):
        results = self.fetch_all()
        self.process(results)
        self.deliver()
        self.heartbeat()
        self.prune_stars()
        self.log("done in %.1fs: %d alerts, %d dropped, %d deferred, %d descriptions fetched, %d jobs known" % (
            time.time() - self.t0, len(self.alerts), len(self.rows["dropped.csv"]), len(self.st.pending),
            self.details_used, len(self.st.seen)))
        self.finish()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "state"))
    ap.add_argument("--dry-run", action="store_true", help="fetch + filter + print; send nothing, save nothing")
    ap.add_argument("--no-notify", action="store_true", help="save state but print alerts instead of sending")
    ap.add_argument("--only", help="only sources whose id starts with this, e.g. workday:nvidia or greenhouse")
    args = ap.parse_args()
    try:
        Run(args).go()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            notify.send_status("❌ Job listener crashed", traceback.format_exc()[-1500:], print, priority="high")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
