"""
Compile a .tex on Overleaf (the user's familiar compiler) and return the PDF + log.

Uses a dedicated, persistent Chromium profile so the bot account stays logged in. The password is read from the
macOS Keychain (service "job-listener-overleaf"); it is never written to disk by this code.
Each compile uploads a throw-away project, compiles it with pdfLaTeX, downloads output.pdf / output.log, and
deletes the project again so the account stays clean.
"""
import io
import json
import os
import subprocess
import time
import zipfile

from playwright.sync_api import sync_playwright

BASE = "https://www.overleaf.com"
KEYCHAIN_SERVICE = "job-listener-overleaf"
PROFILE = os.path.expanduser("~/Library/Application Support/job-listener/overleaf-profile")


class LoginNeeded(RuntimeError):
    """Overleaf wants a human (CAPTCHA / email check). Run `python overleaf.py --login` once."""


def keychain_login():
    out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-g"],
                         capture_output=True, text=True)
    acct = next((l.split('"')[3] for l in out.stdout.splitlines() if l.strip().startswith('"acct"')), None)
    pw = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                        capture_output=True, text=True).stdout.strip()
    if not (acct and pw):
        raise RuntimeError("Overleaf login not found in Keychain (service %s)" % KEYCHAIN_SERVICE)
    return acct, pw


class Overleaf:
    def __init__(self, headless=True):
        self.headless = headless

    def __enter__(self):
        os.makedirs(PROFILE, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(PROFILE, headless=self.headless,
                                                               viewport={"width": 1280, "height": 900})
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.ensure_login()
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    # ---------------------------------------------------------------- session
    def ensure_login(self, wait_for_human=0):
        p = self.page
        p.goto(BASE + "/project", wait_until="domcontentloaded")
        if "/login" not in p.url:
            return
        email, pw = keychain_login()
        p.fill('input[name="email"]', email)
        p.fill('input[name="password"]', pw)
        p.click('button[type="submit"]')
        try:
            p.wait_for_url("**/project**", timeout=30000 + wait_for_human * 1000)
        except Exception as e:  # CAPTCHA, email confirmation, wrong password ...
            raise LoginNeeded("Overleaf login did not complete (%s). Run: mac/.venv/bin/python mac/overleaf.py "
                              "--login" % (p.url,)) from e

    def _csrf(self):
        tok = self.page.locator('meta[name="ol-csrfToken"]').get_attribute("content")
        if not tok:
            self.page.goto(BASE + "/project", wait_until="domcontentloaded")
            tok = self.page.locator('meta[name="ol-csrfToken"]').get_attribute("content")
        return tok

    # ---------------------------------------------------------------- compile
    def compile_tex(self, tex, name="resume-check"):
        """-> (pdf_bytes or None, log_text, status)"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("main.tex", tex)
        csrf = self._csrf()
        r = self.page.request.post(BASE + "/project/new/upload", headers={"X-Csrf-Token": csrf},
                                   multipart={"name": "%s-%d" % (name, int(time.time())),
                                              "qqfile": {"name": "resume.zip", "mimeType": "application/zip",
                                                         "buffer": buf.getvalue()}})
        if not r.ok:
            raise RuntimeError("Overleaf upload failed: HTTP %s %s" % (r.status, r.text()[:200]))
        pid = r.json().get("project_id")
        try:
            body = {"rootDoc_id": None, "draft": False, "check": "silent", "incrementalCompilesEnabled": False,
                    "stopOnFirstError": False}
            r = self.page.request.post(BASE + "/project/%s/compile" % pid, data=json.dumps(body),
                                       headers={"X-Csrf-Token": csrf, "Content-Type": "application/json"})
            if not r.ok:
                raise RuntimeError("Overleaf compile failed: HTTP %s %s" % (r.status, r.text()[:200]))
            j = r.json()
            files = {f["path"]: f for f in j.get("outputFiles") or []}
            dom = j.get("pdfDownloadDomain") or BASE
            q = []
            if j.get("clsiServerId"):
                q.append("clsiserverid=" + j["clsiServerId"])
            if j.get("compileGroup"):
                q.append("compileGroup=" + j["compileGroup"])
            qs = ("?" + "&".join(q)) if q else ""

            def fetch(path):
                f = files.get(path)
                if not f:
                    return None
                resp = self.page.request.get(dom + f["url"] + qs)
                return resp.body() if resp.ok else None
            pdf = fetch("output.pdf")
            log = (fetch("output.log") or b"").decode("latin-1")
            return pdf, log, j.get("status")
        finally:
            self.page.request.delete(BASE + "/project/%s" % pid, headers={"X-Csrf-Token": csrf})


if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:          # one-time manual login in a visible window (CAPTCHA etc.)
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(PROFILE, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(BASE + "/login")
            print("Log in in the browser window (if needed). Waiting up to 30 minutes...")
            try:
                email, pwd = keychain_login()
                page.fill('input[name="email"]', email)
                page.fill('input[name="password"]', pwd)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_url("**/project**", timeout=1800000)
            print("Logged in. The session is saved; the runner will reuse it.")
            ctx.close()
    elif len(sys.argv) > 1:
        tex = open(sys.argv[1], encoding="utf-8").read()
        with Overleaf() as ol:
            pdf, log, status = ol.compile_tex(tex)
        print("status:", status, "| pdf bytes:", len(pdf or b""), "| log bytes:", len(log))
        if pdf:
            out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/overleaf-test.pdf"
            open(out, "wb").write(pdf)
            print("saved", out)
