"""Drain a realistic queue through the REAL run_queue(drain=True) against a
local http.server (separate process) and record a per-file timeline:
download start/end, wait in 'fetched', ingest start/end, rollup rebuilds,
url_progress UPDATE count, CPU use of main + parser workers, idle gaps.

Nothing under mrfx/ is modified: the hooks below wrap module attributes in
THIS process only (parser workers are spawned fresh and see the real code).

usage: queue_timeline.py <variant> [key=value ...]     (MrfxConfig overrides)
  e.g. queue_timeline.py default
       queue_timeline.py dl2 parallel_downloads=2
       queue_timeline.py ing1 parallel_ingests=1
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
import mrfx.fetch as F  # noqa: E402
import mrfx.ingest as I  # noqa: E402
import mrfx.sniff as SN  # noqa: E402
from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.store import Store  # noqa: E402

SCRATCH = Path("/tmp/mrfx_review11/throughput")
HTTP_ROOT = SCRATCH / "http_root"
OUT_DIR = Path("/home/user/Claudevideo/docs/review11_repros/throughput/results")
PORT = 8911
CLK = os.sysconf("SC_CLK_TCK")


def _coerce(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def proc_cpu(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / CLK
    except (OSError, IndexError):
        return None


def proc_rss_hwm(pid: int) -> tuple[float, float]:
    rss = hwm = 0.0
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) / 1024
                elif line.startswith("VmHWM:"):
                    hwm = int(line.split()[1]) / 1024
    except OSError:
        pass
    return rss, hwm


def proc_io(pid: int) -> dict:
    out = {}
    try:
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                k, v = line.split(":")
                out[k.strip()] = int(v)
    except OSError:
        pass
    return out


def main() -> int:
    variant = sys.argv[1]
    overrides = {k: _coerce(v) for k, v in (a.split("=", 1) for a in sys.argv[2:])}
    # harness-only knobs (not MrfxConfig fields)
    keep_warm = bool(overrides.pop("keep_warm", True))        # serve opens the store keep_warm=True
    reuse_store = overrides.pop("reuse_store", None)          # start from another variant's finished store
    http_root = SCRATCH / str(overrides.pop("http_root", "http_root"))
    stale_s = overrides.pop("rollup_max_stale_seconds", None)  # cadence experiment only
    only = overrides.pop("only", None)                        # serve only files whose name contains this
    run_dir = SCRATCH / "runs" / variant
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if reuse_store:
        shutil.copytree(SCRATCH / "runs" / str(reuse_store) / "store", run_dir / "store")
    if stale_s is not None:
        F.ROLLUP_MAX_STALE_SECONDS = float(stale_s)

    cfg = MrfxConfig(
        inbox_dir=run_dir / "inbox", processed_dir=run_dir / "processed",
        failed_dir=run_dir / "failed", store_dir=run_dir / "store",
        downloads_dir=run_dir / "downloads", entity_map_path=run_dir / "entity_map.yaml",
        enrichment={"mode": "off"}, **overrides)
    cfg.ensure_dirs()
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, keep_warm=keep_warm, temp_dir=cfg.duckdb_temp_dir)
    import logging
    logging.basicConfig(level=logging.INFO, filename=str(run_dir / "mrfx.log"),
                        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")

    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(PORT), "--bind", "127.0.0.1",
                            "--directory", str(http_root)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    names = sorted(p.name for p in http_root.iterdir() if p.suffix in (".gz", ".zip", ".json")
                   and (not only or str(only) in p.name))
    sizes = {n: (http_root / n).stat().st_size for n in names}
    # filename_for() truncates the basename to 120 chars: map a local name back to the served one
    def served(local: str) -> str:
        base = local.split("_", 1)[1]
        return next((n for n in names if n == base or n.startswith(base[:60])), base)
    urls = [f"http://127.0.0.1:{PORT}/{n}" for n in names]

    T0 = time.monotonic()
    ev: list[dict] = []
    lock = threading.Lock()

    def mark(kind, name, **kw):
        with lock:
            ev.append({"t": round(time.monotonic() - T0, 3), "kind": kind, "name": name, **kw})

    # ---- write_lock instrumentation: wait + hold per thread, rollup-attributed ----
    class LockProxy:
        def __init__(self, real):
            self._l = real
            self.waits: list[tuple[str, float, float]] = []   # (thread, t, wait_s)
            self.holds: list[tuple[str, float, float, bool]] = []  # (thread, t, hold_s, rollup_active)
            self._since: dict[int, float] = {}

        def acquire(self, blocking=True, timeout=-1):
            t = time.monotonic()
            ok = self._l.acquire(blocking, timeout)
            w = time.monotonic() - t
            name = threading.current_thread().name
            if w > 0.002:
                with lock:
                    self.waits.append((name, round(t - T0, 3), w))
            if ok:
                self._since[threading.get_ident()] = time.monotonic()
            return ok

        def release(self):
            t0 = self._since.pop(threading.get_ident(), None)
            self._l.release()
            if t0 is not None:
                h = time.monotonic() - t0
                with lock:
                    self.holds.append((threading.current_thread().name, round(t0 - T0, 3), h,
                                       bool(getattr(store, "_rollup_active", False))))

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *a):
            self.release()

        def locked(self):
            return self._l.locked()

    lp = LockProxy(store.write_lock)
    store.write_lock = lp

    # ---- hooks (this process only) ----
    real_download = F.download

    def download_hook(cfg_, url, dest, *a, **k):
        n = served(dest.name)
        mark("dl_start", n)
        try:
            return real_download(cfg_, url, dest, *a, **k)
        finally:
            mark("dl_end", n)
    F.download = download_hook

    real_ingest = I.ingest_file

    def ingest_hook(cfg_, store_, path, *a, **k):
        n = served(path.name)
        mark("ing_start", n)
        try:
            r = real_ingest(cfg_, store_, path, *a, **k)
            mark("ing_end", n, status=r.get("status"), rows=r.get("rows"))
            return r
        except BaseException as e:
            mark("ing_end", n, status=f"raised {type(e).__name__}")
            raise
    I.ingest_file = ingest_hook
    F.ingest_file = ingest_hook   # fetch imported the name directly

    def timed(mod, attr, label):
        real = getattr(mod, attr)

        def w(*a, **k):
            t = time.monotonic()
            try:
                return real(*a, **k)
            finally:
                p_ = a[0] if a and isinstance(a[0], Path) else (a[2] if len(a) > 2 and isinstance(a[2], Path) else None)
                mark(label, served(p_.name) if p_ is not None else "", dur=round(time.monotonic() - t, 3))
        setattr(mod, attr, w)
    timed(SN, "preflight", "preflight")
    F.preflight = SN.preflight
    timed(F, "_record_download_fingerprint", "dl_fingerprint")
    timed(I, "_finish_ok", "finish_ok")

    prog_calls = {"n": 0, "t": 0.0}
    real_prog = store.url_progress

    def prog_hook(url_id, done, total):
        t = time.monotonic()
        real_prog(url_id, done, total)
        prog_calls["n"] += 1
        prog_calls["t"] += time.monotonic() - t
    store.url_progress = prog_hook

    real_fin = store.finalize_rates_part

    def fin_hook(name, tmp, rows):
        t = time.monotonic()
        real_fin(name, tmp, rows)
        mark("finalize", served(name), dur=round(time.monotonic() - t, 3), rows=rows)
    store.finalize_rates_part = fin_hook

    for attr in ("update_rollups_incremental", "rebuild_rollups"):
        real_r = getattr(store, attr)

        def r_hook(*a, _real=real_r, _attr=attr, **k):
            t = time.monotonic()
            mark("rollup_start", _attr)
            try:
                r = _real(*a, **k)
                mark("rollup_end", _attr, dur=round(time.monotonic() - t, 3), result=str(r))
                return r
            except BaseException as e:
                mark("rollup_end", _attr, dur=round(time.monotonic() - t, 3),
                     result=f"RAISED {type(e).__name__}: {str(e)[:200]}")
                raise
        setattr(store, attr, r_hook)

    # ---- sampler: queue state counts + CPU of main and workers ----
    samples: list[dict] = []
    stop = threading.Event()
    seen_pids: set[int] = set()
    hwm: dict[int, float] = {}
    last_io: dict[int, dict] = {}      # last /proc/<pid>/io snapshot per process (workers die at pool shutdown)
    wal_max = [0]
    wal_p = cfg.store_dir / "mrfx.duckdb.wal"

    def sampler():
        last_cpu = {}
        last_t = time.monotonic()
        while not stop.is_set():
            try:
                with store.connect() as con:
                    rows = con.execute("SELECT status, count(*) FROM url_queue GROUP BY 1").fetchall()
                counts = {s: n for s, n in rows}
            except Exception:  # noqa: BLE001
                counts = {}
            pids = [os.getpid()]
            pool = I.get_parse_pool()
            if pool is not None:
                pids += [p.pid for p in (getattr(pool._pool, "_processes", None) or {}).values()]
            now = time.monotonic()
            busy = {}
            for pid in pids:
                c = proc_cpu(pid)
                if c is None:
                    continue
                seen_pids.add(pid)
                if pid in last_cpu:
                    busy[pid] = (c - last_cpu[pid]) / max(1e-6, now - last_t)
                last_cpu[pid] = c
                hwm[pid] = max(hwm.get(pid, 0.0), proc_rss_hwm(pid)[1])
                io_ = proc_io(pid)
                if io_:
                    last_io[pid] = io_
            try:
                wal_max[0] = max(wal_max[0], wal_p.stat().st_size)
            except OSError:
                pass
            last_t = now
            samples.append({"t": round(now - T0, 2), "counts": counts,
                            "cpu_main": round(busy.get(os.getpid(), 0.0), 2),
                            "cpu_workers": round(sum(v for p, v in busy.items() if p != os.getpid()), 2)})
            stop.wait(0.5)
    threading.Thread(target=sampler, daemon=True).start()

    from mrfx.fetch import add_urls, run_queue
    io0 = proc_io(os.getpid())
    print(add_urls(store, urls), flush=True)
    t_run = time.monotonic()
    n = run_queue(cfg, store, drain=True)
    wall = time.monotonic() - t_run
    io1 = proc_io(os.getpid())
    stop.set()
    time.sleep(0.6)
    srv.terminate()

    with store.connect() as con:
        qrows = con.execute("SELECT url, status, rows_emitted, error FROM url_queue ORDER BY id").fetchall()
        frows = con.execute("SELECT filename, status, rows_emitted FROM files").fetchall()
    rows_by_name = {served(f[0]): f[2] for f in frows}

    # ---- per-file table ----
    per: dict[str, dict] = {nm: {"size_mb": sizes[nm] / 1e6} for nm in names}
    for e in ev:
        d = per.get(e["name"])
        if d is None:
            continue
        if e["kind"] in ("dl_start", "dl_end", "ing_start", "ing_end"):
            d[e["kind"]] = e["t"]
        if e["kind"] == "ing_end":
            d["ing_status"] = e.get("status")
        if e["kind"] in ("preflight", "dl_fingerprint", "finish_ok", "finalize"):
            d[e["kind"] + "_s"] = d.get(e["kind"] + "_s", 0.0) + e["dur"]
    rollups = [e for e in ev if e["kind"] == "rollup_end"]
    lines = [f"# timeline — variant `{variant}` overrides={overrides} keep_warm={keep_warm} "
             f"reuse_store={reuse_store} http_root={http_root.name} stale_s={stale_s}", "",
             f"files: {len(names)} ({sum(sizes.values()) / 1e6:.0f} MB gz); processed={n}; "
             f"wall={wall:.1f}s; rollups={len(rollups)} "
             f"({sum(r['dur'] for r in rollups):.1f}s total); "
             f"url_progress UPDATEs={prog_calls['n']} ({prog_calls['t']:.2f}s); "
             f"workers={F.resolve_worker_count(cfg)} downloads={F.resolve_download_count(cfg)}", "",
             "| file | MB gz | dl start | dl s | wait fetched s | ing start | ing s | preflight s | fp s | finish_ok s | finalize s | rows |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tot = {"dl": 0.0, "wait": 0.0, "ing": 0.0}
    for nm in sorted(names, key=lambda x: per[x].get("dl_start", 1e9)):
        d = per[nm]
        dl = d.get("dl_end", 0) - d.get("dl_start", 0)
        wait = d.get("ing_start", 0) - d.get("dl_end", 0) if "ing_start" in d else float("nan")
        ing = d.get("ing_end", 0) - d.get("ing_start", 0) if "ing_end" in d else float("nan")
        tot["dl"] += dl
        tot["wait"] += 0 if wait != wait else wait
        tot["ing"] += 0 if ing != ing else ing
        lines.append(f"| {nm[:48]} | {d['size_mb']:.1f} | {d.get('dl_start', 0):.1f} | {dl:.2f} | {wait:.1f} | "
                     f"{d.get('ing_start', 0):.1f} | {ing:.1f} | {d.get('preflight_s', 0):.2f} | "
                     f"{d.get('dl_fingerprint_s', 0):.2f} | {d.get('finish_ok_s', 0):.2f} | "
                     f"{d.get('finalize_s', 0):.2f} | {rows_by_name.get(nm, '')} |")
    lines += ["", f"sum download {tot['dl']:.1f}s, sum wait-in-fetched {tot['wait']:.1f}s, sum ingest {tot['ing']:.1f}s", ""]
    for r in rollups:
        lines.append(f"- rollup {r['name']} at t={r['t']:.1f}s took {r['dur']:.1f}s -> {r.get('result', '')}")
    # ---- write_lock accounting ----
    hold_total = sum(h[2] for h in lp.holds)
    hold_rollup = sum(h[2] for h in lp.holds if h[3])
    by_thread_hold: dict[str, float] = {}
    for th, _, h, _ in lp.holds:
        by_thread_hold[th] = by_thread_hold.get(th, 0.0) + h
    by_thread_wait: dict[str, float] = {}
    for th, _, w in lp.waits:
        by_thread_wait[th] = by_thread_wait.get(th, 0.0) + w
    longest_holds = sorted(lp.holds, key=lambda h: -h[2])[:6]
    longest_waits = sorted(lp.waits, key=lambda w: -w[2])[:6]
    lines += ["", f"write_lock: held {hold_total:.2f}s of {wall:.1f}s wall ({100 * hold_total / wall:.1f}%); "
              f"{len(lp.holds)} acquisitions; held while a rollup was active {hold_rollup:.2f}s",
              "  hold by thread: " + ", ".join(f"{k} {v:.2f}s" for k, v in sorted(by_thread_hold.items(), key=lambda kv: -kv[1])),
              f"  waits >2ms: {len(lp.waits)} totalling {sum(w[2] for w in lp.waits):.2f}s; by thread: "
              + ", ".join(f"{k} {v:.2f}s" for k, v in sorted(by_thread_wait.items(), key=lambda kv: -kv[1])),
              "  longest holds: " + ", ".join(f"{h[0]}@{h[1]:.1f}s {h[2] * 1000:.0f}ms{' (rollup)' if h[3] else ''}" for h in longest_holds),
              "  longest waits: " + ", ".join(f"{w[0]}@{w[1]:.1f}s {w[2] * 1000:.0f}ms" for w in longest_waits)]
    # idle-gap analysis from the sampler
    workers = F.resolve_worker_count(cfg)
    starved = sum(0.5 for s in samples if s["counts"].get("fetched", 0) == 0
                  and s["counts"].get("ingesting", 0) < workers
                  and (s["counts"].get("downloading", 0) or s["counts"].get("queued", 0)))
    capped = sum(0.5 for s in samples if s["counts"].get("queued", 0) > 0
                 and s["counts"].get("downloading", 0) == 0)
    cpu_w = [s["cpu_workers"] for s in samples[2:]]
    cpu_m = [s["cpu_main"] for s in samples[2:]]
    lines += ["", f"parsers starved (no fetched file, a parser slot idle, downloads/queue still pending): ~{starved:.1f}s",
              f"downloaders idle with queue pending (prefetch cap reached): ~{capped:.1f}s",
              f"mean CPU: workers {sum(cpu_w) / max(1, len(cpu_w)):.2f} cores, main {sum(cpu_m) / max(1, len(cpu_m)):.2f} cores "
              f"(box has {os.cpu_count()})",
              f"main process io: read_bytes {(io1.get('read_bytes', 0) - io0.get('read_bytes', 0)) / 1e6:.0f} MB, "
              f"write_bytes {(io1.get('write_bytes', 0) - io0.get('write_bytes', 0)) / 1e6:.0f} MB, "
              f"rchar {(io1.get('rchar', 0) - io0.get('rchar', 0)) / 1e6:.0f} MB, wchar {(io1.get('wchar', 0) - io0.get('wchar', 0)) / 1e6:.0f} MB",
              f"peak RSS (VmHWM): main {hwm.get(os.getpid(), 0):.0f} MB; workers "
              + ", ".join(f"{hwm[p]:.0f}" for p in sorted(hwm) if p != os.getpid()) + " MB",
              "worker io (last snapshot): " + "; ".join(
                  f"pid {p}: rchar {v.get('rchar', 0) / 1e6:.0f} MB wchar {v.get('wchar', 0) / 1e6:.0f} MB "
                  f"read_bytes {v.get('read_bytes', 0) / 1e6:.0f} MB write_bytes {v.get('write_bytes', 0) / 1e6:.0f} MB"
                  for p, v in last_io.items() if p != os.getpid()),
              f"max .wal size seen: {wal_max[0] / 1e6:.1f} MB (sampled every 0.5s)", ""]
    bad = [q for q in qrows if q[1] != "done"]
    lines.append(f"queue end states: {dict((s, sum(1 for q in qrows if q[1] == s)) for s in set(q[1] for q in qrows))}")
    for q in bad[:10]:
        lines.append(f"- {q[0].rsplit('/', 1)[-1][:60]}: {q[1]} {str(q[3])[:140]}")
    md = "\n".join(lines)
    (OUT_DIR / f"timeline_{variant}.md").write_text(md)
    (OUT_DIR / f"timeline_{variant}.json").write_text(json.dumps(
        {"variant": variant, "overrides": overrides, "wall": wall, "events": ev, "samples": samples,
         "per_file": per, "prog_calls": prog_calls, "hwm": hwm,
         "lock_holds": lp.holds, "lock_waits": lp.waits}, default=str))
    print(md)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
