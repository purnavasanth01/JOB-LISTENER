"""Tiny stdlib HTTP client: gzip, JSON, retries on transient errors, no retry on 429."""
import gzip
import json
import socket
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; job-listener/1.0; +https://github.com/purnavasanth01/JOB-LISTENER)"
TIMEOUT = 25


class HttpError(Exception):
    def __init__(self, code, url):
        super().__init__("HTTP %s for %s" % (code, url[:120]))
        self.code = code


class RateLimited(HttpError):
    pass


class NotModified(Exception):
    pass


def request(url, data=None, headers=None, retries=2, etag=None, want_headers=False):
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8", "Accept-Encoding": "gzip"}
    body = None
    if data is not None:
        h["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    if etag:
        h["If-None-Match"] = etag
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "replace")
                return (text, dict(resp.headers)) if want_headers else text
        except urllib.error.HTTPError as e:
            if e.code == 304:
                raise NotModified()
            if e.code == 429:
                raise RateLimited(429, url)
            if e.code < 500:
                raise HttpError(e.code, url)
            last = HttpError(e.code, url)
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
            last = e
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last


def get_json(url, **kw):
    return json.loads(request(url, **kw))


def post_json(url, data, **kw):
    return json.loads(request(url, data=data, **kw))
