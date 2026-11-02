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


def tg_send(text, buttons=None, markup=None):
    """Send to the linked chat. buttons = [(label, url)] rows; markup = raw inline_keyboard. Returns message id."""
    if not _tg_chat:
        return False
    msg = {"chat_id": _tg_chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        markup = [[{"text": t, "url": u}] for t, u in buttons]
    if markup:
        msg["reply_markup"] = {"inline_keyboard": markup}
    r = tg_api("sendMessage", msg)
    return (r or {}).get("message_id") or True


HELP = ("<b>Job listener bot</b>\nI message you every new 2027 internship within minutes.\n\n"
        "⭐ Star = interested (a resume will be made). Target companies are starred automatically.\n"
        "❌ Remove = ignore the job.\nButtons are processed on the next check (within ~3 min).\n\n"
        "/status – listener health, today's numbers, starred count\n/help – this message")


def telegram_sync(meta, log, status_text, on_button=None):
    """Called once per run: find your chat, answer commands and ⭐/❌ taps sent since the last run."""
    global _tg_chat
    if not os.environ.get("TELEGRAM_TOKEN"):
        return
    _tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or meta.get("tg_chat")
    try:
        updates = tg_api("getUpdates", {"offset": meta.get("tg_offset", 0), "timeout": 0,
                                        "allowed_updates": ["message", "callback_query"]}) or []
    except Exception as e:  # noqa: BLE001
        log("telegram getUpdates failed: %s" % e)
        return
    for u in updates:
        meta["tg_offset"] = u["update_id"] + 1
        if u.get("callback_query"):
            _button(u["callback_query"], log, on_button)
            continue
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


def _button(cq, log, on_button):
    msg = cq.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(_tg_chat) or not on_button:
        return
    action, _, hid = (cq.get("data") or "").partition(":")
    res = on_button(action, hid)
    try:   # the tap may be minutes old; Telegram can reject a late answer, which is harmless
        tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": {"starred": "⭐ Starred",
               "removed": "❌ Removed"}.get((res or {}).get("status"), "Done")})
    except Exception:  # noqa: BLE001
        pass
    mid = msg.get("message_id")
    if not mid or not res:
        return
    if res.get("delete"):
        try:
            tg_api("deleteMessage", {"chat_id": _tg_chat, "message_id": mid})
            return
        except Exception:  # noqa: BLE001 - older than 48 h: cannot delete, strike it instead
            res = {"text": "❌ <s>%s</s>" % html.escape(msg.get("text", "")[:300]), "markup": []}
    if res.get("text") is not None:
        _safe(log, tg_api, "editMessageText", {"chat_id": _tg_chat, "message_id": mid, "text": res["text"],
                                               "parse_mode": "HTML", "disable_web_page_preview": True,
                                               "reply_markup": {"inline_keyboard": res.get("markup") or []}})
    elif res.get("markup") is not None:
        _safe(log, tg_api, "editMessageReplyMarkup", {"chat_id": _tg_chat, "message_id": mid,
                                                      "reply_markup": {"inline_keyboard": res["markup"]}})


def _safe(log, fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        log("telegram send failed: %s" % e)
        return False


def _url_ok(url):
    return bool(url) and url.startswith(("http://", "https://"))


def _keyboard(url, hid, starred):
    """Single-job keyboard (messages sent before grouping)."""
    rows = []
    if _url_ok(url):
        rows.append([{"text": "🔗 Apply", "url": url}])
    if hid:
        rows.append([{"text": "⭐ Starred ✓" if starred else "⭐ Star", "callback_data": "s:" + hid},
                     {"text": "❌ Remove", "callback_data": "x:" + hid}])
    return rows


GROUP_SIZE = 10
TARGET_HEADER = "🔥🔥 <b>TARGET COMPANIES · H-1B SPONSORS</b> 🔥🔥\n<i>auto-starred: resumes are made unless you tap ❌</i>"
OTHER_HEADER = "🔔 <b>New internships</b>\n<i>tap ⭐ for a resume</i>"


def _job_block(a, n):
    e = html.escape
    loc = e((a.get("location") or "?")[:80]) + (" (+%d more)" % (a["count"] - 1) if a.get("count", 1) > 1 else "")
    listener = e(a.get("listener") or a.get("layer") or "")
    if a.get("target"):
        lines = ["<b>%d. 🏢 %s</b> · #%s" % (n, e(a["company"].upper()), a.get("rank")),
                 "💼 <b>%s</b>" % e(a["title"])]
    else:
        lines = ["<b>%d. %s</b> — %s" % (n, e(a["company"]), e(a["title"]))]
    lines.append("📍 %s · 📡 %s" % (loc, listener))
    if a.get("line"):
        lines.append("💬 <i>\"%s\"</i>" % e(a["line"]))
    lines += [e(t) for t in a.get("tags") or []]
    return "\n".join(lines)


def group_text(header, items):
    """items: [(hid, entry)] with entry keys idx, block, status, company, title."""
    parts = [header]
    for _, x in items:
        if x.get("status") == "removed":
            parts.append("<s>%d. %s — %s</s> ❌" % (x["idx"], html.escape(x.get("company", "")),
                                                    html.escape(x.get("title", ""))))
        else:
            parts.append(x["block"])
    return "\n\n".join(parts)[:4090]


def group_markup(items):
    rows = []
    for hid, x in items:
        if x.get("status") == "removed" or not hid:
            continue
        n, row = x["idx"], []
        if _url_ok(x.get("url")):
            row.append({"text": "🔗 %d" % n, "url": x["url"]})
        row.append({"text": ("⭐ %d ✓" if x.get("status") == "starred" else "⭐ %d") % n, "callback_data": "s:" + hid})
        row.append({"text": "❌ %d" % n, "callback_data": "x:" + hid})
        rows.append(row)
    return rows


def telegram_jobs(alerts):
    """Up to 10 jobs per message (targets in their own loud messages), one 🔗/⭐/❌ row per job.
    Sets msg_id / idx / block / header on each alert so taps can re-render the message later."""
    if not _tg_chat:
        return False
    sent = 0
    for loud, subset in ((True, [a for a in alerts if a.get("target")]), (False, [a for a in alerts if not a.get("target")])):
        chunks, cur, size = [], [], 0
        for a in subset:                       # max 10 per message, and stay under Telegram's 4096 characters
            blk = len(_job_block(a, 10)) + 2
            if cur and (len(cur) >= GROUP_SIZE or size + blk > TG_MAX - 200):
                chunks.append(cur)
                cur, size = [], 0
            cur.append(a)
            size += blk
        if cur:
            chunks.append(cur)
        for ci, chunk in enumerate(chunks):
            header = (TARGET_HEADER if loud else OTHER_HEADER) + (" (%d/%d)" % (ci + 1, len(chunks)) if len(chunks) > 1 else "")
            items = []
            for n, a in enumerate(chunk, 1):
                a["idx"], a["block"], a["header"] = n, _job_block(a, n), header
                items.append((a.get("hid"), {"idx": n, "block": a["block"], "url": a.get("url"),
                                             "status": "starred" if a.get("target") else "new",
                                             "company": a["company"], "title": a["title"]}))
            if sent:
                time.sleep(1.05)               # Telegram allows ~1 message/second per chat
            mid = tg_send(group_text(header, items), markup=group_markup(items))
            sent += 1
            for a in chunk:
                a["msg_id"] = mid
    return True


def _line(a):
    who = a["company"] + (" #%d" % a["rank"] if a.get("rank") else "")
    extra = " ×%d locations" % a["count"] if a.get("count", 1) > 1 else ""
    tags = (" [" + ", ".join(a["tags"]) + "]") if a.get("tags") else ""
    line = (' | 💬 "%s"' % a["line"]) if a.get("line") else ""
    return "%s — %s | %s | %s%s%s%s" % (who, a["title"], a.get("listener") or a["role"], (a.get("location") or "?")[:40],
                                       extra, line, tags)


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
        "<tr{bg}><td><b>{c}</b>{r}</td><td><a href='{u}'>{t}</a></td><td>{loc}</td><td>{layer}</td><td><i>{line}</i></td>"
        "<td>{tags}</td></tr>"
        .format(bg=" style='background:#fff3cd'" if a.get("target") else "",
                c=("🔥 " if a.get("target") else "") + html.escape(a["company"]),
                r=(" #%d" % a["rank"]) if a.get("rank") else "", u=html.escape(a["url"] or ""),
                t=html.escape(a["title"]),
                loc=html.escape((a.get("location") or "?")[:60]) + (" ×%d" % a["count"] if a.get("count", 1) > 1 else ""),
                layer=html.escape(a.get("listener") or a.get("layer", "")), line=html.escape(a.get("line") or ""),
                tags=html.escape(", ".join(a.get("tags") or [])))
        for a in alerts)
    page = ("<p>{n} new internship{s}. 🔥 = target company (auto-starred). Apply today.</p><table border=1 cellpadding=5 "
            "cellspacing=0 style='border-collapse:collapse;font-family:sans-serif;font-size:13px'><tr><th>Company</th>"
            "<th>Title (apply link)</th><th>Location</th><th>Found by</th><th>Sponsorship line</th><th>Notes</th></tr>"
            "{rows}</table>"
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
