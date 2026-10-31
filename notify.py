"""Alerts: Telegram + email (+ optional ntfy) once per run. Config comes from environment variables (GitHub Secrets).

Telegram: TELEGRAM_TOKEN from @BotFather. The chat is detected automatically the first time you message the bot
(only that private chat is ever used); TELEGRAM_CHAT_ID can pin it explicitly.
"""
import html
import json
import os
import re
import smtplib
import ssl
import time
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

NTFY_MAX = 3800   # ntfy message limit is 4096 bytes
TG_MAX = 3800     # Telegram message limit is 4096 characters
_tg_chat = None   # set by telegram_sync()


def configured():
    return {"telegram": bool(os.environ.get("TELEGRAM_TOKEN") and _tg_chat),
            "push": bool(os.environ.get("NTFY_TOPIC")),
            "email": bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))}


# ---------------------------------------------------------------- Telegram

def tg_api(method, payload):
    token = re.sub(r"\s+", "", os.environ.get("TELEGRAM_TOKEN", ""))   # pasted tokens sometimes contain spaces
    base = os.environ.get("TELEGRAM_API", "https://api.telegram.org").rstrip("/")   # override only for tests
    try:
        req = urllib.request.Request("%s/bot%s/%s" % (base, token, method), data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - the token is in the URL: never let it reach a (public) log
        msg = str(e)
        for part in {token, token.split(":")[-1]} - {""}:
            msg = msg.replace(part, "<token>")
        raise RuntimeError("telegram %s failed: %s" % (method, msg[:150])) from None
    if not d.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, d.get("description")))
    return d.get("result")


def tg_send(text, buttons=None):
    if not _tg_chat:
        return False
    msg = {"chat_id": _tg_chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        msg["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]}
    tg_api("sendMessage", msg)
    return True


HELP = ("<b>Job listener bot</b>\nI message you every new 2027 internship within minutes.\n\n"
        "/status – listener health and today's numbers\n/help – this message")


def telegram_sync(meta, log, status_text):
    """Called once per run: find your chat, answer commands sent since the last run."""
    global _tg_chat
    if not os.environ.get("TELEGRAM_TOKEN"):
        return
    _tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or meta.get("tg_chat")
    try:
        updates = tg_api("getUpdates", {"offset": meta.get("tg_offset", 0), "timeout": 0,
                                        "allowed_updates": ["message"]}) or []
    except Exception as e:  # noqa: BLE001
        log("telegram getUpdates failed: %s" % e)
        return
    for u in updates:
        meta["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        text = (m.get("text") or "").strip().lower()
        if not _tg_chat and chat.get("type") == "private":
            _tg_chat = chat["id"]
            meta["tg_chat"] = _tg_chat
            log("telegram: connected to chat %s" % _tg_chat)
            _safe(log, tg_send, "✅ Connected. Job alerts will arrive here.\n\n" + HELP)
            continue
        if str(chat.get("id")) != str(_tg_chat):
            continue          # strangers who find the bot are ignored
        if text.startswith("/status"):
            _safe(log, tg_send, status_text())
        elif text.startswith("/help") or text.startswith("/start"):
            _safe(log, tg_send, HELP)


def _safe(log, fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        log("telegram send failed: %s" % e)
        return False


def _tg_job(a):
    e = html.escape
    who = "<b>%s</b>%s" % (e(a["company"]), " #%d" % a["rank"] if a.get("rank") else "")
    loc = e((a.get("location") or "?")[:50]) + (" ×%d" % a["count"] if a.get("count", 1) > 1 else "")
    tags = (" · " + e(", ".join(a["tags"]))) if a.get("tags") else ""
    return "%s — <a href=\"%s\">%s</a>\n%s · %s%s" % (who, e(a.get("url") or "", quote=True), e(a["title"]),
                                                      e(a["role"]), loc, tags)


def telegram_jobs(alerts):
    """One message per ~15 jobs; a single job also gets an Apply button."""
    if not _tg_chat:
        return False
    n = len(alerts)
    chunks, cur = [], "🔔 <b>%d new internship%s</b>\n" % (n, "" if n == 1 else "s")
    for a in alerts:
        block = "\n" + _tg_job(a) + "\n"
        if len(cur) + len(block) > TG_MAX:
            chunks.append(cur)
            cur = ""
        cur += block
    chunks.append(cur)
    for i, c in enumerate(chunks):
        buttons = [("Apply: " + alerts[0]["company"][:30], alerts[0]["url"])] if n == 1 and alerts[0].get("url") else None
        tg_send(c, buttons)
        if i < len(chunks) - 1:
            time.sleep(1.1)       # Telegram allows ~1 message/second per chat
    return True


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
        ok |= telegram_jobs(alerts)
    except Exception as e:  # noqa: BLE001
        log("telegram failed: %s" % e)
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
        ok |= tg_send("<b>%s</b>\n%s" % (html.escape(title), html.escape(body)))
    except Exception as e:  # noqa: BLE001
        log("status telegram failed: %s" % e)
    try:
        ok |= push(title, body, priority=priority, tags="robot")
    except Exception as e:  # noqa: BLE001
        log("status push failed: %s" % e)
    try:
        ok |= email(title, body)
    except Exception as e:  # noqa: BLE001
        log("status email failed: %s" % e)
    return ok
