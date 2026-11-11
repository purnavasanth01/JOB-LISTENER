"""
Source adapters. Each lister returns a list of job dicts (internship-looking jobs only):

  key, company, title, locations[list], url, posted, desc (str|None), detail (tuple|None),
  terms, season, category, degrees, sponsorship   (aggregator extras, may be None)

`detail` tells fetch_detail() how to get the full description later (only for NEW jobs).
"""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import http_client as http
from rules import clean_text, is_internship, workday_req_id

PAUSE = 0.25          # politeness gap between requests to the same host
WD_PAGE = 20
WD_MAX_PAGES = 25     # 500 jobs per query, plenty for intern-filtered queries
FACET_TTL = 24 * 3600
INTERN_FACET = re.compile(r"\bintern|\bco-?op|\bstudent|\btrainee|\bapprentice", re.I)
# only trust "job type"-like facets for the intern filter (location facets can contain "Campus" etc.)
INTERN_PARAM = re.compile(r"worker.?sub.?type|worker.?type|employee.?type|time.?type|job.?type|job.?famil"
                          r"|career.?level|job.?level|experience|employment", re.I)
US_FACET = re.compile(r"^\s*(united states( of america)?|usa|us|u\.s\.)\s*$", re.I)
# a facet-tagged "intern" job whose title looks senior / full-time is a mis-tag, not an internship
NOT_INTERN = re.compile(r"\b(director|principal|lead|vp|avp|vice president|senior|sr\.?|manager|head of|staff"
                        r"|new grad\w*|graduate program|full[- ]time|experienced)\b", re.I)


def _job(key, company, title, locations, url, posted=None, desc=None, detail=None, **extra):
    j = {"key": key, "company": company, "title": (title or "").strip(), "locations": [l for l in locations if l],
         "url": url, "posted": posted, "desc": desc, "detail": detail}
    j.update(extra)
    return j


def _keep(title, hint=False):
    if is_internship(title):
        return True
    return hint and not NOT_INTERN.search(title or "")


# ---------------------------------------------------------------- Workday

def _walk_facets(facets, out):
    for f in facets or []:
        param = f.get("facetParameter")
        for v in f.get("values") or []:
            if "facetParameter" in v:
                _walk_facets([v], out)
            elif param and v.get("id") and v.get("descriptor"):
                out.append((param, v["descriptor"], v["id"], v.get("count") or 0))


def _pick(values, pattern, exclude=None, param_ok=None):
    by_param = {}
    for param, desc, vid, count in values:
        if param_ok and not param_ok.search(param):
            continue
        if pattern.search(desc) and not (exclude and exclude.search(desc)):
            by_param.setdefault(param, []).append((vid, count))
    if not by_param:
        return None
    param, vals = max(by_param.items(), key=lambda kv: sum(c for _, c in kv[1]))
    return {param: [v for v, _ in vals]}


def list_workday(spec, st, company):
    host, tenant, site = spec["host"], spec["tenant"], spec["site"]
    api = "https://%s/wday/cxs/%s/%s" % (host, tenant, site)
    web = spec.get("web") or "https://%s/%s" % (host, site)
    fc = st.get("wd_facets")
    if not fc or time.time() - fc.get("at", 0) > FACET_TTL:
        d = http.post_json(api + "/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        vals = []
        _walk_facets(d.get("facets"), vals)
        fc = {"at": time.time(),
              "intern": _pick(vals, INTERN_FACET, re.compile(r"internal|international", re.I), INTERN_PARAM),
              "us": _pick(vals, US_FACET)}
        st["wd_facets"] = fc
        time.sleep(PAUSE)
    us = fc.get("us") or {}
    if spec.get("all"):
        queries = [(dict(us), "", WD_MAX_PAGES, False)]
    else:
        queries = []
        if fc.get("intern"):
            a = dict(us)
            a.update(fc["intern"])
            queries.append((a, "", WD_MAX_PAGES, True))
        queries.append((dict(us), "intern", 2 if fc.get("intern") else 10, False))
        queries.append((dict(us), "co-op", 1, False))
    jobs = {}
    for applied, text, max_pages, hint in queries:
        offset, total = 0, None
        for _ in range(max_pages):
            d = http.post_json(api + "/jobs", {"appliedFacets": applied, "limit": WD_PAGE, "offset": offset,
                                               "searchText": text})
            time.sleep(PAUSE)
            if total is None:
                total = d.get("total") or 0
                if hint and total == 0:
                    st.pop("wd_facets", None)   # facet ids may have changed; rediscover next run
            posts = d.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath") or ""
                rid = workday_req_id(path) or ((p.get("bulletFields") or [None])[0])
                if not rid or not _keep(p.get("title"), hint):
                    continue
                key = "wd:%s:%s" % (tenant.lower(), str(rid).lower())
                if key not in jobs:
                    jobs[key] = _job(key, company, p.get("title"), [p.get("locationsText")], web + path,
                                     posted=p.get("postedOn"), detail=("workday", api + path))
            offset += WD_PAGE
            if not posts or offset >= total:
                break
    return list(jobs.values())


def detail_workday(url):
    d = http.get_json(url)
    ji = d.get("jobPostingInfo") or {}
    locs = [ji.get("location")] + list(ji.get("additionalLocations") or [])
    return clean_text(ji.get("jobDescription")), [l for l in locs if l]


# ---------------------------------------------------------------- Greenhouse / Lever / Ashby / SmartRecruiters

def list_greenhouse(spec, st, company):
    slug = spec["slug"]
    d = http.get_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug)
    out = []
    for j in d.get("jobs") or []:
        meta = " ".join(str(m.get("value")) for m in (j.get("metadata") or []) if m.get("value"))
        if not _keep(j.get("title"), bool(re.search(r"\bintern", meta, re.I))):
            continue
        out.append(_job("gh:%s" % j["id"], company, j.get("title"), [(j.get("location") or {}).get("name")],
                        j.get("absolute_url"), posted=j.get("first_published") or j.get("updated_at"),
                        detail=("greenhouse", slug, j["id"])))
    return out


def detail_greenhouse(slug, jid):
    d = http.get_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s" % (slug, jid))
    locs = [(d.get("location") or {}).get("name")] + [o.get("name") for o in d.get("offices") or []]
    return clean_text(d.get("content")), [l for l in locs if l]


def list_lever(spec, st, company):
    slug = spec["slug"]
    d = http.get_json("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    out = []
    for j in d or []:
        cats = j.get("categories") or {}
        if not _keep(j.get("text"), bool(re.search(r"intern", str(cats.get("commitment")), re.I))):
            continue
        desc = " ".join([j.get("descriptionPlain") or ""] +
                        [clean_text(l.get("content")) for l in j.get("lists") or []] +
                        [j.get("additionalPlain") or ""])
        locs = [cats.get("location")] + list(cats.get("allLocations") or [])
        out.append(_job("lever:%s" % j["id"], company, j.get("text"), locs, j.get("hostedUrl"),
                        posted=j.get("createdAt"), desc=clean_text(desc)))
    return out


def list_ashby(spec, st, company):
    slug = spec["slug"]
    d = http.get_json("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    out = []
    for j in d.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        if not _keep(j.get("title"), str(j.get("employmentType")).lower() == "intern"):
            continue
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        out.append(_job("ashby:%s" % j["id"], company, j.get("title"), locs, j.get("jobUrl"),
                        posted=j.get("publishedAt"), desc=clean_text(j.get("descriptionPlain") or j.get("descriptionHtml"))))
    return out


def list_smartrecruiters(spec, st, company):
    co = spec["company"]
    base = "https://api.smartrecruiters.com/v1/companies/%s/postings" % co
    seen, out = set(), []
    for url in (base + "?limit=100&offset=0", base + "?limit=100&offset=100", base + "?q=intern&limit=100"):
        d = http.get_json(url)
        time.sleep(PAUSE)
        for j in d.get("content") or []:
            hint = "intern" in str((j.get("typeOfEmployment") or {}).get("label", "")).lower()
            if j["id"] in seen or not _keep(j.get("name"), hint):
                continue
            seen.add(j["id"])
            loc = (j.get("location") or {}).get("fullLocation")
            out.append(_job("sr:%s" % j["id"], company, j.get("name"), [loc],
                            "https://jobs.smartrecruiters.com/%s/%s" % (co, j["id"]),
                            posted=j.get("releasedDate"), detail=("smartrecruiters", co, j["id"])))
    return out


def detail_smartrecruiters(co, jid):
    d = http.get_json("https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % (co, jid))
    secs = ((d.get("jobAd") or {}).get("sections") or {})
    return clean_text(" ".join((s or {}).get("text", "") for s in secs.values())), None


# ---------------------------------------------------------------- Oracle Recruiting Cloud (Oracle, Goldman, Uber, Dell, JPMC, Amex)

def list_orc(spec, st, company):
    host, site = spec["host"], spec["site"]
    base = "https://%s/hcmRestApi/resources/latest/recruitingCEJobRequisitions" % host
    out, seen = [], set()
    finders = ["findReqs;siteNumber=%s,limit=25,offset=%d,sortBy=POSTING_DATES_DESC" % (site, o) for o in (0, 25, 50, 75, 100, 125)]
    finders.append("findReqs;siteNumber=%s,keyword=intern,limit=25,sortBy=POSTING_DATES_DESC" % site)
    for f in finders:
        d = http.get_json(base + "?onlyData=true&expand=requisitionList.secondaryLocations&finder=" + quote(f, safe=";=,"))
        time.sleep(PAUSE)
        items = (d.get("items") or [{}])[0].get("requisitionList") or []
        for r in items:
            rid = r.get("Id")
            if not rid or rid in seen or not _keep(r.get("Title"), False):
                continue
            seen.add(rid)
            locs = [r.get("PrimaryLocation")] + [s.get("Name") for s in r.get("secondaryLocations") or []]
            desc = clean_text(" ".join(filter(None, [r.get("ShortDescriptionStr"), r.get("ExternalResponsibilitiesStr"),
                                                     r.get("ExternalQualificationsStr")])))
            out.append(_job("orc:%s:%s" % (host, rid), company, r.get("Title"), locs,
                            "https://%s/hcmUI/CandidateExperience/en/sites/%s/job/%s" % (host, site, rid),
                            posted=r.get("PostedDate"), desc=None, detail=("orc", host, site, rid, desc)))
    return out


def detail_orc(host, site, rid, short=""):
    url = ("https://%s/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
           "&finder=ById;Id=%%22%s%%22,siteNumber=%s" % (host, rid, site))
    d = http.get_json(url)
    it = (d.get("items") or [{}])[0]
    text = " ".join(filter(None, [it.get("ExternalDescriptionStr"), it.get("ExternalResponsibilitiesStr"),
                                  it.get("ExternalQualificationsStr"), short]))
    return clean_text(text), None


# ---------------------------------------------------------------- Big-tech own systems

def list_amazon(spec, st, company):
    out, seen = [], set()
    for offset in (0, 100):
        d = http.get_json("https://www.amazon.jobs/en/search.json?base_query=intern&country=USA&sort=recent"
                          "&result_limit=100&offset=%d" % offset)
        time.sleep(PAUSE)
        for j in d.get("jobs") or []:
            jid = j.get("id_icims") or j.get("id")
            if not jid or jid in seen or not _keep(j.get("title"), bool(j.get("is_intern"))):
                continue
            seen.add(jid)
            locs = [j.get("normalized_location") or j.get("location")]
            for l in j.get("locations") or []:
                try:
                    locs.append(json.loads(l).get("normalizedLocation"))
                except (ValueError, AttributeError, TypeError):
                    pass
            desc = clean_text(" ".join(filter(None, [j.get("description"), j.get("basic_qualifications"),
                                                     j.get("preferred_qualifications")])))
            out.append(_job("amazon:%s" % jid, company, j.get("title"), locs,
                            "https://www.amazon.jobs" + (j.get("job_path") or "/en/jobs/%s" % jid),
                            posted=j.get("posted_date"), desc=desc))
    return out


def list_microsoft(spec, st, company):
    # Eightfold PCSX. Rate-limits hard (HTTP 429): one call per run, no retries.
    d = http.get_json("https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com&query=intern"
                      "&location=United%20States&start=0&sort_by=timestamp", retries=0)
    data = d.get("data") or d
    out = []
    for p in data.get("positions") or []:
        pid = p.get("id")
        if not pid or not _keep(p.get("name"), False):
            continue
        url = p.get("positionUrl") or "/careers/job/%s" % pid
        if url.startswith("/"):
            url = "https://apply.careers.microsoft.com" + url
        locs = p.get("standardizedLocations") or p.get("locations") or [p.get("location")]
        out.append(_job("ms:%s" % pid, company, p.get("name"), [str(l) for l in locs if l], url,
                        posted=p.get("postedTs"), detail=("pcsx", pid)))
    if not out and not data.get("positions"):
        raise ValueError("unexpected Microsoft response shape: %s" % list(d.keys())[:6])
    return out


def detail_pcsx(pid):
    # search results carry no description; position_details does (one call per NEW job only)
    d = http.get_json("https://apply.careers.microsoft.com/api/pcsx/position_details?position_id=%s"
                      "&domain=microsoft.com&hl=en" % pid, retries=0)
    p = d.get("data") or d
    return clean_text(p.get("jobDescription")), p.get("standardizedLocations") or p.get("locations")


def list_google(spec, st, company):
    out, seen = [], set()
    for page in (1, 2, 3):
        h = http.request("https://www.google.com/about/careers/applications/jobs/results/?target_level=INTERN_AND_APPRENTICE"
                         "&location=United%%20States&sort_by=date&page=%d" % page, headers={"Accept": "text/html"})
        time.sleep(PAUSE)
        found = re.findall(r"jobs/results/(\d{6,})-([a-z0-9-]+)", h)
        for jid, slug in found:
            if jid in seen:
                continue
            seen.add(jid)
            title = slug.replace("-", " ").title().replace("Bsms", "BS/MS").replace("Phd", "PhD").replace("Ux", "UX")
            if not _keep(title):
                continue
            url = "https://www.google.com/about/careers/applications/jobs/results/%s-%s" % (jid, slug)
            out.append(_job("google:%s" % jid, company, title, ["United States"], url, detail=("html", url)))
        if not found:
            break
    if not seen:
        raise ValueError("Google page had no job links (layout changed?)")
    return out


def list_eightfold(spec, st, company):
    host, dom = spec["host"], spec["domain"]
    out, seen = [], set()
    for start in (0, 50):
        d = http.get_json("https://%s/api/apply/v2/jobs?domain=%s&query=intern&sort_by=new&num=50&start=%d"
                          % (host, dom, start))
        time.sleep(PAUSE)
        for p in d.get("positions") or []:
            pid = p.get("id")
            if not pid or pid in seen or not _keep(p.get("name"), False):
                continue
            seen.add(pid)
            out.append(_job("nfx:%s" % pid, company, p.get("name"), list(p.get("locations") or [p.get("location")]),
                            p.get("canonicalPositionUrl") or "https://%s/careers/job/%s" % (host, pid),
                            posted=p.get("t_create"), detail=("eightfold", host, dom, pid)))
    return out


def detail_eightfold(host, dom, pid):
    d = http.get_json("https://%s/api/apply/v2/jobs/%s?domain=%s" % (host, pid, dom))
    return clean_text(d.get("job_description")), d.get("locations")


def list_phenom(spec, st, company):
    host = spec["host"]
    out, seen = [], set()
    for page in (1, 2):
        d = http.get_json("https://%s/api/jobs?keywords=intern&page=%d&limit=100&sortBy=posted_date&descending=true"
                          % (host, page))
        time.sleep(PAUSE)
        jobs = d.get("jobs") or []
        for j in jobs:
            x = j.get("data") or {}
            rid = x.get("req_id") or x.get("slug")
            if not rid or rid in seen or not _keep(x.get("title"), False):
                continue
            seen.add(rid)
            desc = clean_text(" ".join(filter(None, [x.get("description"), x.get("responsibilities"),
                                                     x.get("qualifications")])))
            out.append(_job("phenom:%s:%s" % (host, rid), company, x.get("title"),
                            [x.get("full_location") or x.get("location_name")],
                            "https://%s/careers-home/jobs/%s" % (host, rid), posted=x.get("posted_date"), desc=desc))
        if len(jobs) < 100:
            break
    return out


# ---------------------------------------------------------------- Listener 3: aggregator lists (GitHub)

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
VANSH_URL = "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json"
JOBRIGHT_REPOS = ["2026-Software-Engineer-Internship", "2026-Data-Analysis-Internship", "2026-Design-Internship",
                  "2026-Product-Management-Internship"]


def _etag_get(url, st, slot):
    """Returns body, or None when unchanged since last run (HTTP 304)."""
    try:
        text, headers = http.request(url, etag=(st.get("etag") or {}).get(slot), want_headers=True)
    except http.NotModified:
        return None
    et = headers.get("ETag") or headers.get("Etag")
    if et:
        st.setdefault("etag", {})[slot] = et
    return text


def _listings_json(url, st, src_name):
    text = _etag_get(url, st, "main")
    if text is None:
        return None
    rows = json.loads(text)
    if not isinstance(rows, list) or len(rows) < 50:
        raise ValueError("%s listings look broken (%s rows)" % (src_name, len(rows) if isinstance(rows, list) else "?"))
    out = []
    for x in rows:
        if not x.get("active") or x.get("is_visible") is False:
            continue
        posted = x.get("date_posted")
        if isinstance(posted, (int, float)):
            posted = datetime.fromtimestamp(posted, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # keep the time (app shows "posted 2h ago")
        season = x.get("season")
        terms = x.get("terms")
        out.append(_job("%s:%s" % (src_name, x.get("id")), x.get("company_name") or "", x.get("title"),
                        list(x.get("locations") or []), x.get("url"), posted=posted, terms=terms, season=season,
                        category=x.get("category"), degrees=x.get("degrees"), sponsorship=x.get("sponsorship"),
                        aggregator=True))
    return out


def list_simplify(spec, st, company=None):
    return _listings_json(SIMPLIFY_URL, st, "simplify")


def list_vansh(spec, st, company=None):
    return _listings_json(VANSH_URL, st, "vansh")


ROW_RE = re.compile(r"^\|\s*(.*?)\s*\|\s*\*\*\[(.*?)\]\((.*?)\)\*\*\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")
LINK_TEXT = re.compile(r"\[(.*?)\]\(")


def list_jobright(spec, st, company=None):
    out, changed = [], False
    for repo in JOBRIGHT_REPOS:
        text = _etag_get("https://raw.githubusercontent.com/jobright-ai/%s/master/README.md" % repo, st, repo)
        time.sleep(PAUSE)
        if text is None:
            continue
        changed = True
        prev = ""
        for line in text.splitlines():
            m = ROW_RE.match(line)
            if not m:
                continue
            comp_cell, title, link, loc, _model, posted = m.groups()
            if comp_cell.strip() in ("↳", "&#8627;"):
                comp = prev
            else:
                lt = LINK_TEXT.search(comp_cell)
                comp = (lt.group(1) if lt else comp_cell).strip("* ")
                prev = comp
            jid = re.search(r"jobs/info/([0-9a-f]+)", link)
            if not jid:
                continue
            out.append(_job("jobright:%s" % jid.group(1), comp, title, [loc], link.split("?")[0],
                            posted=posted, aggregator=True))
    if not changed:
        return None
    return out


# ---------------------------------------------------------------- description fetch

_ashby_cache = {}


def detail_from_url(url):
    """Best-effort description for aggregator rows that link straight to an ATS."""
    if not url:
        return None, None
    p = urlparse(url)
    host, path = p.netloc.lower(), p.path
    m = re.match(r"/([^/]+)/jobs/(\d+)", path)
    if "greenhouse.io" in host and m:
        return detail_greenhouse(m.group(1), m.group(2))
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        parts = [x for x in path.split("/") if x]
        if "myworkdaysite.com" in host and len(parts) > 3 and parts[0] == "recruiting":
            tenant, rest = parts[1], parts[2:]
        else:
            tenant, rest = host.split(".")[0], parts
        if rest and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", rest[0]):
            rest = rest[1:]
        if len(rest) >= 2 and "job" in rest:
            site = rest[0]
            return detail_workday("https://%s/wday/cxs/%s/%s/%s" % (host, tenant, site, "/".join(rest[1:])))
    m = re.match(r"/([^/]+)/([0-9a-f-]{36})", path)
    if host == "jobs.lever.co" and m:
        j = http.get_json("https://api.lever.co/v0/postings/%s/%s" % (m.group(1), m.group(2)))
        desc = " ".join([j.get("descriptionPlain") or ""] + [clean_text(l.get("content")) for l in j.get("lists") or []])
        return clean_text(desc), None
    if host == "jobs.ashbyhq.com" and m:
        slug = m.group(1)
        if slug not in _ashby_cache:
            _ashby_cache[slug] = {j["id"]: j for j in http.get_json(
                "https://api.ashbyhq.com/posting-api/job-board/%s" % slug).get("jobs") or []}
        j = _ashby_cache[slug].get(m.group(2))
        return (clean_text(j.get("descriptionPlain")), None) if j else (None, None)
    m = re.match(r"/([^/]+)/(\d{9,})", path)
    if "smartrecruiters.com" in host and m:
        return detail_smartrecruiters(m.group(1), m.group(2))
    return detail_html(url), None


JSON_DESC = re.compile(r'"(description|jobDescription|job_description|qualifications|responsibilities|'
                       r'basicQualifications|requirements)"\s*:\s*"((?:[^"\\]|\\.){80,})"')


def detail_html(url):
    """Any other job page (Jobright, iCIMS/Jibe, company sites): use the job JSON embedded in the page
    (Next.js data, schema.org JobPosting, Jibe) when present, else the visible text."""
    h = http.request(url, headers={"Accept": "text/html"})
    parts = []
    for m in JSON_DESC.finditer(h):
        try:
            parts.append(clean_text(json.loads('"%s"' % m.group(2))))
        except ValueError:
            continue
    parts = sorted(set(parts), key=len, reverse=True)[:4]
    if sum(len(p) for p in parts) > 300:
        return " ".join(parts)[:40000]
    text = clean_text(re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", h))
    return text[:40000] if len(text) > 300 else None


def fetch_detail(job):
    """-> (desc or None, locations or None). Raises on network errors (caller retries next run)."""
    d = job.get("detail")
    if d:
        kind = d[0]
        if kind == "workday":
            return detail_workday(d[1])
        if kind == "greenhouse":
            return detail_greenhouse(d[1], d[2])
        if kind == "smartrecruiters":
            return detail_smartrecruiters(d[1], d[2])
        if kind == "orc":
            return detail_orc(d[1], d[2], d[3], d[4])
        if kind == "eightfold":
            return detail_eightfold(d[1], d[2], d[3])
        if kind == "pcsx":
            return detail_pcsx(d[1])
        if kind == "html":
            return detail_html(d[1]), None
    if job.get("aggregator"):
        return detail_from_url(job.get("url"))
    return None, None


LISTERS = {
    "workday": list_workday, "greenhouse": list_greenhouse, "lever": list_lever, "ashby": list_ashby,
    "smartrecruiters": list_smartrecruiters, "orc": list_orc, "amazon": list_amazon, "microsoft": list_microsoft,
    "google": list_google, "eightfold": list_eightfold, "phenom": list_phenom,
    "simplify": list_simplify, "vansh": list_vansh, "jobright": list_jobright,
}

# Sources that are flaky by nature: only report them broken after a full day of failures.
SLOW_ALARM = {"microsoft", "google"}


def source_id(spec):
    t = spec["type"]
    ident = spec.get("tenant") or spec.get("slug") or spec.get("company") or spec.get("host") or ""
    if t == "workday" and spec.get("site"):
        ident = "%s/%s" % (ident, spec["site"])
    if t == "orc":
        ident = "%s/%s" % (spec["host"].split(".")[0], spec["site"])
    return "%s:%s" % (t, ident) if ident else t
