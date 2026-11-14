"""The analytics fold-in: its own scheduled worker (review 14 item 1.1).

Every screen, report and export answers from the ANALYTICS (the spine and the
practice directory); a file that finished loading is in the raw store at once
but in the analytics only after a fold-in. Until 0.8.0 the fold-in was a side
effect of the download queue going idle: work with no queue credit (a part-way
build, a failed attempt, an inbox file whose refresh was skipped) waited for
an IDLE queue, and one long download starved it for hours; mid-grind the next
update came at 4 x the last one with the header saying "added when the queue
is idle".

Now ONE scheduler per serving process owns every fold-in:

- it runs whenever the analytics are behind (`Store.rollups_stale()`), the
  analytics lock is free and the store's write lock is free at that instant
  (between two files' commits) — whatever the queue is doing;
- cadence: a quiet queue folds in a few seconds after the last file landed;
  a busy one waits max(BUSY_FLOOR_S, BUSY_MULTIPLE x the last fold-in),
  never more than BUSY_CAP_S, counted from the END of the last fold-in, so
  extraction keeps most of the disk and the gap is bounded both ways;
- a failure backs off 2, 4, 8 … 60 min and retries THE SAME update (an
  incremental that ran out of memory is never escalated to a whole-store
  build — only a failed precondition is, and the log says which);
- "Run now" folds in at the next tick whatever the cadence says; "Pause
  analytics" (persisted) is the only thing that holds it — a running build
  stops between two committed parts.

Its state is exposed as it really is (`state()`, `when_text()`): the header's
"next update in about N min", the panel's "why it is waiting" phrase per file.
The CLI paths with no scheduler (`mrfx ingest`, `forget`, a local drain) call
`fold_in()` directly, the same decision tree.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

log = logging.getLogger(__name__)

TICK_S = 2.0
IDLE_SETTLE_S = 3.0
BUSY_FLOOR_S = 60.0
BUSY_MULTIPLE = 3.0
BUSY_CAP_S = 1800.0
STALE_PROBE_S = 10.0
QUEUE_NOTE_STALE_S = 30.0
BACKOFF_BASE_S = 120.0
BACKOFF_CAP_S = 3600.0


def fold_in(store, why: str = "") -> float:
    """ONE analytics update, whatever is needed: resume a part-way plan (a
    names plan as a names plan), a first build, else the payer-scoped
    incremental — and a full build only when the incremental's PRECONDITION
    fails (logged with the precondition). Returns the store method's result
    (ROLLUP_SKIPPED when another update holds the lock). Raises on failure;
    the caller decides about back-off."""
    from .store import ROLLUP_SKIPPED, RollupNeedsFull, RollupPlanPending
    plan = store.rollup_plan()
    if plan is not None and plan.get("kind") == "names":
        # a killed names-only build resumes AS a names build (review 14
        # Group 3: it became a whole-store full build of 287 slices)
        log.info("analytics: finishing the part-way practice-name build")
        r = store.rebuild_rollups(names_only=True, skip_if_busy=True)
        if r == ROLLUP_SKIPPED:
            return r
        plan = None
    if plan is not None:
        log.info("analytics: continuing the part-way build")
        return store.rebuild_rollups(skip_if_busy=True)
    if not store.rollups_ever_built():
        log.info("first analytics build of this store — building them in full%s",
                 f" ({why})" if why else "")
        return store.rebuild_rollups(skip_if_busy=True)
    try:
        return store.update_rollups_incremental(skip_if_busy=True)
    except RollupPlanPending as e:
        return store.rebuild_rollups(names_only=(e.kind == "names"), skip_if_busy=True)
    except RollupNeedsFull as e:
        log.info("the analytics need a full build: %s — running it", e)
        return store.rebuild_rollups(skip_if_busy=True)


def _clock(mono: float | None) -> str:
    """A monotonic instant as local wall-clock HH:MM (for a sentence)."""
    if mono is None:
        return ""
    t = time.time() + (mono - time.monotonic())
    return dt.datetime.fromtimestamp(t).strftime("%H:%M")


def _minutes(s: float) -> str:
    if s < 45:
        return "under a minute"
    m = int(round(s / 60.0))
    if m < 60:
        return f"about {m} min"
    h, mm = divmod(m, 60)
    return f"about {h} h {mm:02d} min" if mm else f"about {h} h"


class FoldIn:
    """The scheduler. One per Store in a serving process (`Store.foldin`)."""

    def __init__(self, store):
        self.store = store
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._mu = threading.Lock()
        self.last_end: float | None = None          # monotonic, end of the last fold-in
        self.last_duration: float | None = None
        self.last_kind: str | None = None
        self.last_poke = time.monotonic()
        self.failures = 0
        self.retry_at = 0.0
        self.run_now = False
        self.running: dict | None = None
        self._stale = (0.0, None)                    # (monotonic checked, bool)
        self._queue = (0.0, 0, {})                   # (monotonic noted, in_flight, counts)
        self.runs = 0

    # -- wiring ---------------------------------------------------------------

    def start(self, stop: threading.Event | None = None) -> bool:
        """Start the worker thread (once). True when THIS call started it."""
        with self._mu:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = stop or threading.Event()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="mrfx-analytics")
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        with self._mu:
            t, st = self._thread, self._stop
        if st is not None:
            st.set()
        self._wake.set()
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive() and not (self._stop and self._stop.is_set())

    def poke(self, reason: str = "") -> None:
        """Something changed (a file landed, a part was removed, a build
        stopped): look again at the next tick."""
        self.last_poke = time.monotonic()
        self._stale = (0.0, None)
        self._wake.set()

    def note_queue(self, in_flight: int, counts: dict | None = None) -> None:
        """The URL queue's coordinator reports how busy it is each loop."""
        self._queue = (time.monotonic(), int(in_flight), dict(counts or {}))

    def request_run_now(self) -> None:
        self.run_now = True
        self.poke("run now")

    def set_paused(self, paused: bool) -> None:
        self.store.set_analytics_paused(paused)
        self.poke("pause" if paused else "resume")

    # -- the decision -----------------------------------------------------------

    def queue_busy(self) -> bool:
        """A download, an index expansion or a parse is in flight (the URL
        queue's last report, or any in-process ingest — an inbox scan)."""
        noted, in_flight, _c = self._queue
        busy = in_flight > 0 and time.monotonic() - noted < QUEUE_NOTE_STALE_S
        if not busy:
            try:
                from .ingest import active_ingest_count
                busy = active_ingest_count() > 0
            except Exception:  # noqa: BLE001
                pass
        return busy

    def gap_s(self, busy: bool | None = None) -> float:
        busy = self.queue_busy() if busy is None else busy
        if not busy:
            return IDLE_SETTLE_S
        last = self.last_duration or 0.0
        return min(BUSY_CAP_S, max(BUSY_FLOOR_S, BUSY_MULTIPLE * last))

    def is_stale(self) -> bool:
        t, v = self._stale
        if v is None or time.monotonic() - t >= STALE_PROBE_S:
            v = bool(self.store.rollups_stale())
            self._stale = (time.monotonic(), v)
        return v

    def decide(self) -> dict:
        """{due, why, code, next_at} — why the scheduler is (not) folding in
        right now, from its real state. Never raises."""
        now = time.monotonic()
        try:
            if self.running is not None:
                return {"due": False, "code": "running", "why": "being added now",
                        "next_at": None}
            if not self.is_stale():
                return {"due": False, "code": "current", "why": "", "next_at": None}
            if self.store.analytics_paused() and not self.run_now:
                return {"due": False, "code": "paused",
                        "why": "analytics paused — press Resume", "next_at": None}
            if self.store.rollup_in_progress():
                job = self.store.rollup_job() or {}
                what = {"names_refresh": "the practice-name refresh",
                        "names": "the practice-name build"}.get(job.get("kind"), "the running update")
                return {"due": False, "code": "lock",
                        "why": f"after {what} finishes", "next_at": None}
            if now < self.retry_at and not self.run_now:
                fail = self.store.rollup_failure() or {}
                sent = str(fail.get("sentence") or "")
                cause = "an out-of-memory stop" if "memory" in sent.lower() else "a failed attempt"
                at = ""
                try:
                    at = dt.datetime.fromisoformat(str(fail.get("at"))).replace(
                        tzinfo=dt.timezone.utc).astimezone().strftime("%H:%M")
                except (TypeError, ValueError):
                    pass
                return {"due": False, "code": "backoff",
                        "why": f"retrying at {_clock(self.retry_at)} after {cause}"
                               + (f" at {at}" if at else ""),
                        "next_at": self.retry_at}
            busy = self.queue_busy()
            gap = self.gap_s(busy)
            if self.run_now or self.last_end is None:
                return {"due": True, "code": "next", "why": "next in line", "next_at": now}
            ref = self.last_end if busy else max(self.last_end, self.last_poke)
            if now - ref >= gap:
                return {"due": True, "code": "next", "why": "next in line", "next_at": now}
            nxt = ref + gap
            if busy:
                return {"due": False, "code": "cadence",
                        "why": f"next update in {_minutes(nxt - now)} (downloads are "
                               "running; updates are spaced so they keep the disk)",
                        "next_at": nxt}
            return {"due": False, "code": "settle", "why": "next in line", "next_at": nxt}
        except Exception as e:  # noqa: BLE001
            log.debug("fold-in decision unavailable (%s)", e)
            return {"due": False, "code": "unknown", "why": "", "next_at": None}

    def when_text(self) -> str:
        """The header's "when": 'in about 4 min (downloads are running)',
        'in a few seconds', 'when you resume' — '' when nothing waits."""
        d = self.decide()
        code = d["code"]
        if code in ("current", "unknown"):
            return ""
        if code == "running":
            return "now"
        if code == "paused":
            return "when you resume the analytics (Files tab)"
        if code == "lock":
            return d["why"]
        if code == "backoff":
            return d["why"].replace("retrying at", "at", 1)
        if code == "cadence" and d.get("next_at"):
            left = max(0.0, d["next_at"] - time.monotonic())
            return f"in {_minutes(left)} (downloads are running)"
        return "in a few seconds"

    def state(self) -> dict:
        d = self.decide()
        nxt = d.get("next_at")
        return {
            "running": self.is_running(), "paused": self.store.analytics_paused(),
            "code": d["code"], "why": d["why"],
            "next_in_s": (max(0.0, nxt - time.monotonic()) if nxt else None),
            "next_at": _clock(nxt) if nxt else "",
            "queue_busy": self.queue_busy(), "gap_s": self.gap_s(),
            "last_duration_s": self.last_duration, "last_kind": self.last_kind,
            "last_end_ago_s": (time.monotonic() - self.last_end) if self.last_end else None,
            "failures": self.failures,
            "retry_at": _clock(self.retry_at) if self.retry_at > time.monotonic() else "",
            "run_now": self.run_now, "runs": self.runs,
        }

    # -- the work ---------------------------------------------------------------

    def run_once(self, why: str = "") -> bool:
        """One fold-in now (the caller decided it is due). True when an
        update ran to completion."""
        from .store import ROLLUP_SKIPPED, RollupPaused
        store = self.store
        if store.rollup_in_progress():
            return False
        # between two files: a commit holding the write lock right now goes
        # first (the update itself never holds that lock for long)
        if not store.write_lock.acquire(timeout=5.0):
            return False
        store.write_lock.release()
        forced = self.run_now
        t0 = time.monotonic()
        self.running = {"started": time.time(), "why": why}
        paused_override = forced and store.analytics_paused()
        if paused_override:
            store._analytics_paused = False      # this one run, by the user's request
        try:
            r = fold_in(store, why)
        except RollupPaused:
            log.info("analytics paused between two parts — it continues on resume")
            self.last_end = time.monotonic()
            return False
        except Exception as e:  # noqa: BLE001 — retried after a back-off
            self.failures += 1
            backoff = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (self.failures - 1)))
            self.retry_at = time.monotonic() + backoff
            self.last_end = time.monotonic()
            try:
                store._note_rollup_failure(e)
            except Exception:  # noqa: BLE001
                pass
            log.exception("analytics update failed (%d in a row) — raw data is safe; "
                          "the committed part is kept and the same update is tried "
                          "again in about %.0f min", self.failures, backoff / 60)
            return False
        finally:
            self.running = None
            self.run_now = False if forced else self.run_now
            if paused_override:
                store._analytics_paused = True
            self._stale = (0.0, None)
        if r == ROLLUP_SKIPPED:
            return False
        took = time.monotonic() - t0
        self.failures = 0
        self.retry_at = 0.0
        self.last_end = time.monotonic()
        if r and r > 0:
            self.last_duration = took
            self.runs += 1
        if took > 60:
            log.info("analytics update took %.0f min — the next one while downloads run "
                     "comes in %s at the earliest", took / 60, _minutes(self.gap_s(True)))
        return True

    def _loop(self) -> None:
        stop = self._stop
        try:
            # the files the analytics hold NOW, before any fold-in: a store
            # opened by 0.8.0 for the first time is seeded here, so the first
            # update's files get their "added" events (found on the 99M store:
            # the first update seeded the set after it, and recorded none)
            self.store._note_fold_ins("an update while the app was stopped")
        except Exception:  # noqa: BLE001 — cosmetic
            pass
        while stop is not None and not stop.is_set():
            self._wake.wait(TICK_S)
            self._wake.clear()
            if stop.is_set():
                break
            try:
                d = self.decide()
                if d["due"]:
                    self.run_once(d["why"])
            except Exception:  # noqa: BLE001 — the scheduler survives everything
                log.exception("analytics scheduler: unexpected error; continuing")
                time.sleep(TICK_S)


def ensure(store) -> FoldIn:
    """The store's scheduler object (created on first use, not started)."""
    f = getattr(store, "foldin", None)
    if f is None:
        f = FoldIn(store)
        store.foldin = f
    return f


def running(store) -> FoldIn | None:
    f = getattr(store, "foldin", None)
    return f if f is not None and f.is_running() else None


def poke(store, reason: str = "") -> bool:
    """Tell a running scheduler that something changed. False when none runs
    (the caller then updates the analytics itself)."""
    f = running(store)
    if f is None:
        return False
    f.poke(reason)
    return True
