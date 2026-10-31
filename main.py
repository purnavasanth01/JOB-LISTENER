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
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import notify
import rules
import sources

ROOT = os.path.dirname(os.path.abspath(__file__))
OWN_TYPES = {"amazon", "google", "microsoft", "eightfold", "orc", "phenom"}          # Listener 1
L3_SPECS = [{"type": "simplify"}, {"type": "vansh"}, {"type": "jobright"}]          # Listener 3
DETAIL_BUDGET = 80          # description fetches per run; the rest wait for the next run
RUN_BUDGET = 200            # seconds; after this, remaining descriptions wait for the next run
BROKEN_AFTER = 3600         # alert when a source has failed for 1 hour...
BROKEN_AFTER_SLOW = 86400   # ...or 1 day for naturally flaky sources (Microsoft 429s, Google HTML)
REKEY_MIN = 50              # this many "new" jobs at once from one source = probably the site changed ids
DUP_WINDOW_DAYS = 45        # same company + near-identical title alerted within this window = duplicate
HEARTBEAT_HOUR = 9          # daily "listener alive" message after 9 AM Central
MAX_CSV_ROWS = 20000
CSV_FIELDS = ["found_at", "company", "rank", "title", "role", "location", "tags", "layer", "source", "posted", "url",
              "reason"]


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
    FILES = {"seen": {}, "health": {}, "meta": {}, "pending": [], "unsent": []}

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
        self.rows = {"alerts.csv": [], "dropped.csv": [], "missed.csv": [], "backlog.csv": []}
        self.backlog_fps = set()
        self.alerts = []
        self.groups = {}
        self.source_ok = {}
        self.details_used = 0

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

    def row(self, job, role="", tags=(), reason="", location=None):
        t = job.get("target")
        return {"found_at": central_now().strftime("%Y-%m-%d %H:%M"), "company": job["company"],
                "rank": t["rank"] if t else "", "title": job.get("title"), "role": role,
                "location": location if location is not None else "; ".join(job.get("locations") or [])[:120],
                "tags": ", ".join(tags), "layer": job["layer"], "source": job["source"],
                "posted": job.get("posted") or "", "url": job.get("url") or "", "reason": reason}

    # ---------------- filters
    def prefilter(self, job):
        """Title / list-level checks. -> (role, None) or (None, reason)."""
        title = job.get("title") or ""
        r = rules.category_excluded(job.get("category"), title)
        if r:
            return None, r
        role, r = rules.classify_role(title, job.get("category"))
        if r:
            return None, r
        r = (rules.term_check(title, job.get("terms"), job.get("season"))
             or rules.location_check(job.get("locations"))
             or rules.hard_drop(title, None, job.get("degrees"), job.get("sponsorship")))
        if r:
            return None, r
        return role, None

    def postfilter(self, job, desc, locs):
        title = job.get("title") or ""
        return ((rules.location_check(locs) if locs else None)
                or rules.term_check(title, job.get("terms"), job.get("season"), desc)
                or rules.hard_drop(title, desc, job.get("degrees"), job.get("sponsorship")))

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
        for j in candidates:
            if j["key"] in self.st.seen:
                continue
            role, reason = self.prefilter(j)
            if reason:
                self.record(j, "drop", reason)
                self.rows["dropped.csv"].append(self.row(j, reason=reason))
                continue
            desc, locs = j.get("desc"), None
            if desc is None and (j.get("detail") or j.get("aggregator")):
                if self.details_used >= DETAIL_BUDGET or time.time() - self.t0 > RUN_BUDGET:
                    self.defer(j)
                    continue
                try:
                    self.details_used += 1
                    desc, locs = sources.fetch_detail(j)
                except Exception as e:  # noqa: BLE001
                    fails = self.st.meta.setdefault("detail_fails", {})
                    fails[j["key"]] = fails.get(j["key"], 0) + 1
                    self.log("description failed (%d) %s: %s" % (fails[j["key"]], j["key"], e))
                    if fails[j["key"]] < 3:
                        self.defer(j)
                        continue
                    desc = None   # alert anyway, tagged "desc not checked"
            reason = self.postfilter(j, desc, locs)
            if reason:
                self.record(j, "drop", reason)
                self.rows["dropped.csv"].append(self.row(j, role, reason=reason))
                continue
            tags = rules.soft_tags(desc, j.get("sponsorship"))
            if not desc:
                tags.append("desc not checked")
            t = j.get("target")
            if t and t.get("flag"):
                tags.append("⚠ " + t["flag"])
            if j.get("l3_tag"):
                tags.append(j["l3_tag"])
            toks = rules.title_tokens(j["title"])
            gkey = (j["cid"], toks)
            if gkey in self.groups:                      # same title, another location -> one alert line
                g = self.groups[gkey]
                g["count"] += 1
                self.record(j, "alert")
                continue
            dup = self.index.match(j["cid"], toks, self.keys_of(j), only_alerted_since=since)
            if dup:
                self.record(j, "dup:" + dup)
                continue
            loc = "; ".join((locs or j.get("locations") or [])[:3])
            a = {"key": j["key"], "company": j["company"], "rank": t["rank"] if t else None, "title": j["title"],
                 "role": role, "location": loc, "tags": tags, "url": j.get("url"), "layer": j["layer"], "count": 1}
            self.groups[gkey] = a
            self.alerts.append(a)
            self.record(j, "alert")
            row = self.row(j, role, tags, location=loc)
            self.rows["alerts.csv"].append(row)
            if j.get("l3_tag") == "MISSED by L1/L2":
                self.rows["missed.csv"].append(row)

    def defer(self, j):
        p = {k: v for k, v in j.items() if k != "target"}
        self.st.pending.append(p)

    # ---------------- notifications
    def deliver(self):
        batch = self.st.unsent + self.alerts
        if not batch:
            return
        channels = notify.configured()
        if self.args.dry_run or self.args.no_notify or not any(channels.values()):
            why = "dry run" if self.args.dry_run else "--no-notify" if self.args.no_notify else "no NTFY_TOPIC/SMTP set"
            self.log("ALERTS (%s, not sent): %d" % (why, len(batch)))
            for a in batch:
                self.log("  • %s #%s | %s | %s | %s | %s | %s" % (a["company"], a.get("rank") or "-", a["title"],
                                                                  a["role"], a.get("location"), ", ".join(a["tags"]), a["url"]))
            # secrets not set yet: keep the alerts queued so they are delivered once they are
            self.st.unsent = [] if (self.args.dry_run or self.args.no_notify) else batch[-200:]
            return
        if notify.send_jobs(batch, self.log):
            self.log("sent %d alerts" % len(batch))
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
        self.st.save()
        with open(os.path.join(self.st.path, "last_run.log"), "w") as f:
            f.write("\n".join(self.logs) + "\n")

    def go(self):
        results = self.fetch_all()
        self.process(results)
        self.deliver()
        self.heartbeat()
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
