"""Pre-load the dashboard's slow screens at launch (0.8.8).

Every whole-store summary (Market overview, the Explorer's strip, Rate
changes, a code's trend …) is cached in memory per data generation, so after
each start the first visit to each recomputed it from the whole store — up
to a minute or more on a 1.5-billion-rate store, while the user waited.

`serve` now asks those questions itself, as soon as it is up and again after
every analytics update, through the app's own routes (so the answer lands in
exactly the cache the screen reads). Which questions:

  * the screens a fresh dashboard opens (DEFAULT_URLS), and
  * every GET the user waited on for more than LEARN_MIN_S seconds, with its
    exact filters ("Tennessee", "PT", a code) — remembered in
    store/warm_urls.json, newest first, at most MAX_LEARNED.

Downloads (CSV / ZIP / PDF / HTML reports) are never replayed: they are not
cached, so replaying one would only spend the time again. Every failure here
is logged and skipped — pre-loading never stands in the way of the app.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

WARM_HEADER = "x-mrfx-warm"
LEARN_MIN_S = 3.0
MAX_LEARNED = 30
REQUEST_TIMEOUT_S = 1800.0
FILE_NAME = "warm_urls.json"

# what the dashboard requests when it opens (captured from a real browser
# session: the Explorer is the landing screen; Overview is one click away)
DEFAULT_URLS = (
    "/api/stats",
    "/api/overview",
    "/api/summary?grain=tin&latest=1&dollar_only=1",
    "/api/rates?grain=tin&latest=1&dollar_only=1&sort=negotiated_rate&dir=desc&page=1&page_size=100",
    "/api/payers",
    "/api/months",
    "/api/states",
    "/api/benchmark/subjects",
)

# never learned / replayed: downloads and reports (uncached), live polls,
# anything that changes or streams state
_SKIP_SUFFIXES = (".csv", ".zip", ".pdf", ".html", ".xlsx", ".json.zip")
_SKIP_PARTS = ("/download", "/export", "/report", "/progress", "/jobs", "/packets",
               "/files", "/urls", "/analytics", "/boot", "/stop", "/events")

_lock = threading.Lock()


def learnable(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    low = path.lower()
    return not (low.endswith(_SKIP_SUFFIXES) or any(p in low for p in _SKIP_PARTS))


def _file(store_dir) -> Path:
    return Path(store_dir) / FILE_NAME


def learned(store_dir) -> list[str]:
    try:
        got = json.loads(_file(store_dir).read_text(encoding="utf-8"))
        return [str(e["url"]) for e in got if isinstance(e, dict) and e.get("url")]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def remember(store_dir, url: str, seconds: float) -> None:
    """Note a slow dashboard GET (newest first, de-duplicated). Never raises."""
    try:
        path = urlsplit(url).path
        if seconds < LEARN_MIN_S or not learnable(path):
            return
        with _lock:
            f = _file(store_dir)
            try:
                rows = json.loads(f.read_text(encoding="utf-8"))
                rows = [r for r in rows if isinstance(r, dict) and r.get("url") != url]
            except (OSError, ValueError, TypeError):
                rows = []
            rows.insert(0, {"url": url, "seconds": round(seconds, 1),
                            "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rows[:MAX_LEARNED], indent=1), encoding="utf-8")
            tmp.replace(f)
    except Exception:  # noqa: BLE001 — remembering is a convenience
        log.debug("could not remember a slow request", exc_info=True)


def plan(store_dir) -> list[str]:
    """The defaults, then what the user waited on (newest first), once each."""
    out, seen = [], set()
    for u in (*DEFAULT_URLS, *learned(store_dir)):
        if u not in seen and learnable(urlsplit(u).path):
            seen.add(u)
            out.append(u)
    return out


def warm(base_url: str, urls: list[str], stop: threading.Event | None = None,
         client=None) -> dict:
    """GET each URL in turn (one at a time — a pre-load must not crowd out
    the user). Returns {url: seconds | "error …"}."""
    import httpx
    own = client is None
    client = client or httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_S,
                                    trust_env=False, headers={WARM_HEADER: "1"})
    out: dict[str, float | str] = {}
    try:
        for u in urls:
            if stop is not None and stop.is_set():
                break
            t0 = time.monotonic()
            try:
                r = client.get(u, headers={WARM_HEADER: "1"})
                out[u] = (round(time.monotonic() - t0, 2) if r.status_code < 400
                          else f"error {r.status_code}")
            except Exception as e:  # noqa: BLE001 — skip it, try the next
                out[u] = f"error {type(e).__name__}"
    finally:
        if own:
            client.close()
    return out


class SlowRequestMiddleware:
    """Remember every dashboard GET that took LEARN_MIN_S or longer, so the
    next launch pre-loads it (pre-load requests themselves are not counted)."""

    def __init__(self, app, store_dir=None):
        self.app = app
        self.store_dir = store_dir

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "GET" or self.store_dir is None
                or not learnable(scope.get("path", ""))):
            return await self.app(scope, receive, send)
        if any(k.lower() == WARM_HEADER.encode() for k, _v in scope.get("headers") or []):
            return await self.app(scope, receive, send)
        t0 = time.monotonic()
        status = {"code": 0}

        async def tracked(message):
            if message["type"] == "http.response.start":
                status["code"] = message.get("status", 0)
            await send(message)
        await self.app(scope, receive, tracked)
        took = time.monotonic() - t0
        if status["code"] == 200 and took >= LEARN_MIN_S:
            from urllib.parse import parse_qsl, urlencode
            # per-request tokens (a progress bar id, a cache-buster) would
            # make the replayed question a different one
            qs = urlencode([(k, v) for k, v in parse_qsl(scope.get("query_string", b"").decode("latin-1"),
                                                        keep_blank_values=True)
                            if k not in ("progress", "_", "t", "ts")])
            url = scope["path"] + (f"?{qs}" if qs else "")
            import asyncio
            await asyncio.to_thread(remember, self.store_dir, url, took)
