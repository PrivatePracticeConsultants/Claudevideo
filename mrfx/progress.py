"""A 0–100 % progress bar for every output (review 13 Group 2; the design is
docs/review13_repros/progress/DESIGN.md).

Every output keeps the request it uses today. A page that wants a bar mints a
token per run and sends it as `X-Mrfx-Progress: <token>` (or `?progress=` on
the iframe downloads); the route's decorator (api._progress_route) makes a
`Progress` for it and puts it in a contextvar for the route's thread. Modules
reach it with `progress.current()` — a no-op `_NOOP` when nothing set it, so
the CLI, the tests and a token-less request run exactly as before (no SET on
any connection, no thread, no registry entry).

What the percentage is (never a clock):
  weights of the finished stages + the current stage's weight x its own
  fraction, over the plan's total weight. The fraction inside a stage is the
  module's `tick(frac)` when it has one (a per-payer loop), else DuckDB's own
  `query_progress()` of the connection running the statement (sampled from a
  side thread every 150 ms; measured free and monotonic on duckdb 1.5.5),
  else unknown ("at least N %"). Capped at 99 until the route finishes; the
  browser shows 100 only with the body in hand. Never backwards.

Stop: `cancel()` interrupts every connection the run opened
(`Store.connect()` attaches each new one to the current Progress) and makes
the next `Store.connect()` raise `Stopped` — a `duckdb.InterruptException`
subclass, so fault-isolated loops (dossier sections, packets) keep working
and simply stop opening connections. The route answers 409 and never
delivers a partial document.

Invariant 3: every public method swallows its own errors — a broken progress
channel never fails, changes or delays a result. Invariant 7: it only reads.
"""
from __future__ import annotations

import contextvars
import logging
import re
import sys
import threading
import time

import duckdb

log = logging.getLogger(__name__)

_current: contextvars.ContextVar = contextvars.ContextVar("mrfx_progress", default=None)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SAMPLE_SECONDS = 0.15
CAP = 99


class Stopped(duckdb.InterruptException):
    """The run was stopped by the user (or superseded). An Exception — never a
    BaseException — so `except Exception` loops and the detached runners'
    handlers see it and the job state is never wedged at 'running'."""


def _safe(fn):
    def wrapper(self, *a, **k):
        try:
            return fn(self, *a, **k)
        except Exception as e:  # noqa: BLE001 — cosmetic tier, by contract
            log.debug("progress: %s failed (%s)", fn.__name__, e)
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class _Group:
    __slots__ = ("weight", "parent_done", "parent_stage")

    def __init__(self, weight, parent_done, parent_stage):
        self.weight = weight
        self.parent_done = parent_done
        self.parent_stage = parent_stage


class Progress:
    """One run's progress. See the module docstring for the rules."""

    def __init__(self, token: str | None = None, plan: list | None = None,
                 label: str = "", on_update=None):
        self.token = token
        self.label = label
        self.on_update = on_update
        self._lock = threading.Lock()
        self.plan = [(str(n), float(w)) for n, w in (plan or [])]
        self.cancelled = False
        self.cancel_reason = ""
        self.state = "running"
        self.started = time.monotonic()
        self.ended: float | None = None
        self.message = ""
        self._stage_i = -1               # index into self.plan of the current stage
        self._stage_name = ""
        self._stage_frac: float | None = None
        self._ticked = False
        self._waiting: str | None = None
        self._last_pct = 0
        self._cons: list = []
        self._sampler: threading.Thread | None = None
        self._groups: list[_Group] = []
        self._extra: list = []           # stages met that were not planned
        self._q_last = -1.0              # the sampler's last DuckDB reading
        self._q_max = 0.0                # running max within the current stage

    # -- stages ------------------------------------------------------------

    def _total(self) -> float:
        return sum(w for _n, w in self.plan) or 1.0

    def _median_weight(self) -> float:
        ws = sorted(w for _n, w in self.plan) or [1.0]
        return ws[len(ws) // 2]

    @_safe
    def stage(self, name: str, weight: float | None = None, *, indeterminate: bool = False) -> None:
        """Enter a stage. A planned name moves to its slot (never backwards);
        an unplanned one is appended with the plan's median weight. Inside a
        group() the stages run within the group's slot."""
        with self._lock:
            if self._groups:
                # nested stages only change the words; the group's slot moves
                # by the nested run's own ticks / DuckDB readings. The words
                # keep the outer step in front (review 14 Group 3: "Finding
                # this practice" flashed alone inside the dossier)
                g = self._groups[0]
                outer = (self.plan[g.parent_stage][0]
                         if 0 <= getattr(g, "parent_stage", -1) < len(self.plan) else "")
                self._stage_name = (f"{outer}: {name[:1].lower()}{name[1:]}"
                                    if outer and outer != name else name)
                self._q_max = 0.0
                self._q_last = -1.0
                self._emit_locked()
                return
            names = [n for n, _w in self.plan]
            idx = -1
            for j in range(max(0, self._stage_i), len(names)):
                if names[j] == name:
                    idx = j
                    break
            if idx < 0:
                self.plan.insert(max(0, self._stage_i + 1),
                                 (name, float(weight) if weight else self._median_weight()))
                idx = max(0, self._stage_i + 1)
            self._stage_i = idx
            self._stage_name = name
            self._stage_frac = None
            self._ticked = False
            self._q_max = 0.0
            self._q_last = -1.0
            self._emit_locked()

    def plan_from(self, name: str) -> None:
        """Use the stage table PLANS[name] when no plan is set yet."""
        try:
            from .progress_plans import PLANS
            with self._lock:
                if not self.plan and name in PLANS:
                    self.plan = [(str(n), float(w)) for n, w in PLANS[name]]
        except Exception:  # noqa: BLE001
            pass

    @_safe
    def tick(self, frac: float) -> None:
        """The current stage is `frac` (0..1) done. Clamped; never backwards."""
        f = max(0.0, min(1.0, float(frac)))
        with self._lock:
            if self._stage_frac is None or f > self._stage_frac:
                self._stage_frac = f
            self._ticked = True
            self._emit_locked()

    def group(self, weight: float = 1.0):
        """`with p.group():` — a nested feature (a benchmark inside the pitch
        report) runs inside the current stage's slot instead of resetting the
        bar. Its stage() calls change the words only."""
        p = self

        class _Ctx:
            def __enter__(self_inner):
                try:
                    with p._lock:
                        p._groups.append(_Group(weight, p._stage_frac, p._stage_i))
                except Exception:  # noqa: BLE001
                    pass
                return p

            def __exit__(self_inner, *exc):
                try:
                    with p._lock:
                        if p._groups:
                            p._groups.pop()
                except Exception:  # noqa: BLE001
                    pass
                return False
        return _Ctx()

    def waiting(self, what: str):
        """`with p.waiting("Waiting for an earlier summary"):` — the state reads
        'waiting' (a clock is shown, never a percentage) while inside."""
        p = self

        class _Ctx:
            def __enter__(self_inner):
                try:
                    with p._lock:
                        p._waiting = what
                        p._emit_locked()
                except Exception:  # noqa: BLE001
                    pass
                return p

            def __exit__(self_inner, *exc):
                try:
                    with p._lock:
                        p._waiting = None
                        p._emit_locked()
                except Exception:  # noqa: BLE001
                    pass
                return False
        return _Ctx()

    # -- DuckDB connections -------------------------------------------------

    @_safe
    def attach(self, con) -> None:
        """Called by Store.connect() for every connection the run opens: DuckDB
        progress is switched on for THIS session only (never globally) and
        the connection is remembered so cancel() can interrupt it."""
        con.execute("SET enable_progress_bar = true")
        con.execute("SET enable_progress_bar_print = false")
        con.execute("SET progress_bar_time = 0")
        with self._lock:
            self._cons.append(con)
            if len(self._cons) > 64:
                self._cons = self._cons[-64:]
            if self._sampler is None and self.state == "running":
                self._sampler = threading.Thread(target=self._sample_safe, daemon=True,
                                                 name="mrfx-progress")
                self._sampler.start()

    def _sample_safe(self) -> None:
        try:
            self._sample()
        except Exception:  # noqa: BLE001 — a dead sampler only stops the movement
            pass

    def _sample(self) -> None:
        while self.state == "running" and self.ended is None:
            try:
                with self._lock:
                    con = self._cons[-1] if self._cons else None
                q = con.query_progress() if con is not None else -1.0
            except Exception:  # noqa: BLE001 — a closed handle: clean exception
                q = -1.0
            try:
                with self._lock:
                    if q is not None and q >= 0:
                        if self._q_last >= 0 and q + 1e-9 < self._q_last:
                            pass       # a new statement started: keep the stage max
                        self._q_last = q
                        self._q_max = max(self._q_max, q / 100.0)
                        if not self._ticked:
                            if self._stage_frac is None or self._q_max > self._stage_frac:
                                self._stage_frac = self._q_max
                        self._emit_locked()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(SAMPLE_SECONDS)

    @_safe
    def cancel(self, reason: str = "stopped") -> None:
        """Stop the run: interrupt every attached connection (~0.1 s to take
        effect) and refuse to open another."""
        with self._lock:
            self.cancelled = True
            self.cancel_reason = reason
            cons = list(self._cons)
        for con in cons:
            try:
                con.interrupt()
            except Exception:  # noqa: BLE001 — closed already
                pass

    # -- reading ------------------------------------------------------------

    def _pct_locked(self) -> int:
        total = self._total()
        done = sum(w for _n, w in self.plan[:max(0, self._stage_i)])
        cur = self.plan[self._stage_i][1] if 0 <= self._stage_i < len(self.plan) else 0.0
        frac = self._stage_frac or 0.0
        pct = int(100 * (done + cur * frac) / total) if self._stage_i >= 0 else 0
        pct = max(self._last_pct, min(CAP, pct))
        self._last_pct = pct
        return pct

    def _emit_locked(self) -> None:
        if self.on_update is not None:
            try:
                self.on_update(self._snapshot_locked())
            except Exception:  # noqa: BLE001
                pass

    def _snapshot_locked(self) -> dict:
        pct = 100 if self.state == "done" else self._pct_locked()
        state = self.state
        if state == "running" and self._waiting:
            state = "waiting"
        n = len(self.plan)
        return {
            "token": self.token, "label": self.label, "state": state, "pct": pct,
            "stage": self._waiting or self._stage_name,
            "stage_index": (self._stage_i + 1) if self._stage_i >= 0 else 0,
            "stage_count": n,
            "stage_pct": None if self._stage_frac is None else int(100 * self._stage_frac),
            "indeterminate": bool(self._waiting) or (self._stage_frac is None and self._stage_i >= 0),
            "elapsed_ms": int(1000 * ((self.ended or time.monotonic()) - self.started)),
            "message": self.message or self.cancel_reason,
        }

    def snapshot(self) -> dict:
        try:
            with self._lock:
                return self._snapshot_locked()
        except Exception:  # noqa: BLE001
            return {"token": self.token, "state": self.state, "pct": 0, "stage": "",
                    "stage_index": 0, "stage_count": 0, "stage_pct": None,
                    "indeterminate": True, "elapsed_ms": 0, "message": ""}

    @_safe
    def finish(self, state: str = "done", message: str = "") -> None:
        with self._lock:
            if self.cancelled and state == "done":
                state = "stopped"
            self.state = state
            self.message = message or self.message
            self.ended = time.monotonic()
            if state == "done":
                self._stage_i = len(self.plan) - 1
                self._stage_frac = 1.0
            self._cons = []
            self._emit_locked()


class _Noop:
    """What progress.current() returns when no run set one: every method is a
    no-op, so modules call it unconditionally at no cost."""
    token = None
    cancelled = False
    state = "running"

    def stage(self, *a, **k):
        pass

    def tick(self, *a, **k):
        pass

    def plan_from(self, *a, **k):
        pass

    def attach(self, *a, **k):
        pass

    def cancel(self, *a, **k):
        pass

    def finish(self, *a, **k):
        pass

    def snapshot(self):
        return {}

    def group(self, weight: float = 1.0):
        return _NullCtx()

    def waiting(self, what: str):
        return _NullCtx()


class _NullCtx:
    def __enter__(self):
        return _NOOP

    def __exit__(self, *exc):
        return False


_NOOP = _Noop()


def current():
    """The run's Progress, or the shared no-op."""
    try:
        return _current.get() or _NOOP
    except Exception:  # noqa: BLE001
        return _NOOP


def set_current(p) -> contextvars.Token:
    return _current.set(p)


def reset_current(tok) -> None:
    try:
        _current.reset(tok)
    except Exception:  # noqa: BLE001
        _current.set(None)


def check_cancelled() -> None:
    """Raise Stopped when the current run was stopped (between units of work)."""
    p = current()
    if getattr(p, "cancelled", False):
        raise Stopped(getattr(p, "cancel_reason", "") or "stopped")


class Registry:
    """The running and recently finished Progress objects, by token. Read by
    the async poll endpoint under a lock held for microseconds."""

    def __init__(self, cap: int = 256, keep_s: float = 60.0):
        self._lock = threading.Lock()
        self._by_token: dict[str, Progress] = {}
        self.cap = cap
        self.keep_s = keep_s

    def add(self, p: Progress) -> None:
        try:
            with self._lock:
                self._sweep_locked()
                self._by_token[p.token] = p
        except Exception:  # noqa: BLE001
            pass

    def get(self, token: str) -> Progress | None:
        try:
            with self._lock:
                return self._by_token.get(token)
        except Exception:  # noqa: BLE001
            return None

    def _sweep_locked(self) -> None:
        now = time.monotonic()
        for t, p in list(self._by_token.items()):
            if p.ended is not None and now - p.ended > self.keep_s:
                del self._by_token[t]
        if len(self._by_token) >= self.cap:
            done = sorted((p.ended, t) for t, p in self._by_token.items() if p.ended is not None)
            for _e, t in done[: len(self._by_token) - self.cap + 1]:
                del self._by_token[t]


class TextBar(Progress):
    """The CLI's bar: the same stages and weights as the web, on one stderr
    line (`[#####.....]  48 %  Comparing with peers (3 of 5)`); plain lines
    when stderr is not a terminal (only when the stage changes)."""

    def __init__(self, plan_name: str, stream=None, label: str = ""):
        from .progress_plans import PLANS
        super().__init__(token=None, plan=PLANS.get(plan_name, []), label=label,
                         on_update=self._draw)
        self.stream = stream or sys.stderr
        self._last_line = ""
        self._last_stage = None
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.stages_seen: list[str] = []

    def _draw(self, snap: dict) -> None:
        try:
            stage = snap.get("stage") or ""
            if stage and (not self.stages_seen or self.stages_seen[-1] != stage):
                self.stages_seen.append(stage)
            pct = snap.get("pct") or 0
            n = int(pct / 5)
            where = (f" ({snap['stage_index']} of {snap['stage_count']})"
                     if snap.get("stage_count") else "")
            line = f"[{'#' * n}{'.' * (20 - n)}] {pct:3d} %  {stage}{where}"
            if self._tty:
                if line != self._last_line:
                    self.stream.write("\r" + line.ljust(90))
                    self.stream.flush()
            elif (snap.get("stage_index"), snap.get("stage_count")) != self._last_stage \
                    or snap.get("state") == "done":
                # redirected output: one line per OUTER step (review 14 Group
                # 3, P3 — a line per nested word: ~10 per client in packets)
                self.stream.write(line + "\n")
                self.stream.flush()
            self._last_line = line
            self._last_stage = (snap.get("stage_index"), snap.get("stage_count"))
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        self._tok = set_current(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.finish("done" if exc_type is None else "failed")
            if self._tty:
                self.stream.write("\n")
                self.stream.flush()
        finally:
            reset_current(self._tok)
        return False


def text_bar(plan_name: str, stream=None, label: str = "") -> TextBar:
    return TextBar(plan_name, stream=stream, label=label)
