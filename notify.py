"""Alerts: one phone push (ntfy) + one email per run. Config comes from environment variables (GitHub Secrets)."""
import html
import json
import os
import smtplib
import ssl
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

NTFY_MAX = 3800   # ntfy message limit is 4096 bytes


def configured():
    return {"push": bool(os.environ.get("NTFY_TOPIC")),
            "email": bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))}


def _line(a):
    who = a["company"] + (" #%d" % a["rank"] if a.get("rank") else "")
    extra = " ×%d locations" % a["count"] if a.get("count", 1) > 1 else ""
    tags = (" [" + ", ".join(a["tags"]) + "]") if a.get("tags") else ""
    return "%s — %s | %s | %s%s%s" % (who, a["title"], a["role"], (a.get("location") or "?")[:40], extra, tags)


def push(title, body, click=None, actions=None, priority="high", tags="rotating_light"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    # JSON publishing keeps emoji / non-ASCII company names intact (headers would not)
    msg = {"topic": topic, "title": title, "message": body.encode("utf-8")[:NTFY_MAX].decode("utf-8", "ignore"),
           "priority": {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(priority, 3),
           "tags": [tags]}
    if click:
        msg["click"] = click
    if actions:
        msg["actions"] = [{"action": "view", "label": label, "url": url} for label, url in actions[:3]]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(server, data=json.dumps(msg).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return 200 <= r.status < 300


def email(subject, text, html_body=None):
    user, pw = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_PASS", "").strip()
    if not (user and pw):
        return False
    to = os.environ.get("EMAIL_TO", "").strip() or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465")), context=ssl.create_default_context(),
                          timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
    return True


def send_jobs(alerts, log):
    """alerts: list of dicts (company, rank, title, role, location, tags, url, layer, count).
    Returns True if at least one channel delivered (otherwise the caller retries next run)."""
    n = len(alerts)
    companies = sorted({a["company"] for a in alerts})
    title = "%d new internship%s: %s" % (n, "" if n == 1 else "s", ", ".join(companies[:3]) +
                                         (" +%d" % (len(companies) - 3) if len(companies) > 3 else ""))
    # push body: compact, best first
    lines, used = [], 0
    for i, a in enumerate(alerts):
        chunk = "• %s\n%s\n" % (_line(a), a["url"])
        if used + len(chunk.encode("utf-8")) > NTFY_MAX - 60:
            lines.append("…+%d more (see email)" % (n - i))
            break
        lines.append(chunk)
        used += len(chunk.encode("utf-8"))
    actions = [("Apply %d" % (i + 1), a["url"]) for i, a in enumerate(alerts[:3]) if a.get("url")]
    ok = False
    try:
        ok |= push(title, "".join(lines), click=alerts[0].get("url"), actions=actions)
    except Exception as e:  # noqa: BLE001 - never let alerting crash the run
        log("push failed: %s" % e)
    text = "\n\n".join("%s\n%s" % (_line(a), a["url"]) for a in alerts)
    rows = "".join(
        "<tr><td><b>{c}</b>{r}</td><td><a href='{u}'>{t}</a></td><td>{role}</td><td>{loc}</td><td>{tags}</td><td>{layer}</td></tr>"
        .format(c=html.escape(a["company"]), r=(" #%d" % a["rank"]) if a.get("rank") else "", u=html.escape(a["url"] or ""),
                t=html.escape(a["title"]), role=html.escape(a["role"]),
                loc=html.escape((a.get("location") or "?")[:60]) + (" ×%d" % a["count"] if a.get("count", 1) > 1 else ""),
                tags=html.escape(", ".join(a.get("tags") or [])), layer=a.get("layer", ""))
        for a in alerts)
    page = ("<p>{n} new internship{s}. Apply today.</p><table border=1 cellpadding=5 cellspacing=0 "
            "style='border-collapse:collapse;font-family:sans-serif;font-size:13px'><tr><th>Company</th><th>Title (apply link)"
            "</th><th>Role</th><th>Location</th><th>Tags</th><th>Found by</th></tr>{rows}</table>"
            ).format(n=n, s="" if n == 1 else "s", rows=rows)
    try:
        ok |= email("🔔 " + title, text, page)
    except Exception as e:  # noqa: BLE001
        log("email failed: %s" % e)
    return ok


def send_status(title, body, log, priority="default"):
    ok = False
    try:
        ok |= push(title, body, priority=priority, tags="robot")
    except Exception as e:  # noqa: BLE001
        log("status push failed: %s" % e)
    try:
        ok |= email(title, body)
    except Exception as e:  # noqa: BLE001
        log("status email failed: %s" % e)
    return ok
