"""Shutdown and restart cost of `mrfx serve` with downloads AND parses in
flight. Spawns the real server via Popen on a scratch config (port 8912),
feeds it a queue from a PACED local server (so downloads are mid-stream),
then: (A) SIGINT, (B) SIGTERM, (C) SIGKILL then restart and time the
catch-up, (D) `serve --supervise` with SIGINT to the whole process group
(the closest Linux analogue of Ctrl-C in the .bat window).
Writes results/shutdown_restart.md.  usage: shutdown_restart.py [A B C D]
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
PY = "/home/user/Claudevideo/.venv/bin/python"
H = Path("/home/user/Claudevideo/docs/review11_repros/throughput")
SCRATCH = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/throughput/shutdown")
HTTP_ROOT = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/throughput/http_root")
PORT, FPORT = 8912, 8913
OUT = H / "results" / "shutdown_restart.md"


def api(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


def counts():
    try:
        return api("/api/urls")["counts"]
    except Exception:  # noqa: BLE001
        return None


def fresh(case: str, prebuilt: Path | None = None) -> Path:
    d = SCRATCH / case
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "mrfx.yaml").write_text(
        f"inbox_dir: {d}/inbox\nprocessed_dir: {d}/processed\nfailed_dir: {d}/failed\n"
        f"store_dir: {d}/store\ndownloads_dir: {d}/downloads\nentity_map_path: {d}/entity_map.yaml\n"
        f"port: {PORT}\nenrichment:\n  mode: \"off\"\n")
    from mrfx.config import load_mrfx_config
    from mrfx.fetch import add_urls
    from mrfx.store import Store
    cfg = load_mrfx_config(d / "mrfx.yaml")
    cfg.ensure_dirs()
    if prebuilt:
        shutil.rmtree(cfg.store_dir, ignore_errors=True)
        shutil.copytree(prebuilt, cfg.store_dir)
    st = Store(cfg.store_dir)
    names = sorted(p.name for p in HTTP_ROOT.iterdir() if p.suffix == ".gz")
    add_urls(st, [f"http://127.0.0.1:{FPORT}/{n}" for n in names])
    st.close()
    return d


def start_serve(d: Path, supervise: bool = False):
    log = open(d / "serve.out", "ab")
    args = [PY, "-m", "mrfx", "--config", str(d / "mrfx.yaml"), "serve"] + (["--supervise"] if supervise else [])
    p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd="/home/user/Claudevideo",
                         start_new_session=True)
    return p, log


def wait_busy(timeout=120.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        c = counts()
        if c and c.get("downloading", 0) >= 1 and c.get("ingesting", 0) >= 1:
            return c, time.monotonic() - t0
        time.sleep(0.5)
    return counts(), time.monotonic() - t0


def wait_ready(timeout=180.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            api("/api/stats")
            return time.monotonic() - t0
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return None


def stop_and_time(p, sig, group=False, label=""):
    t0 = time.monotonic()
    if group:
        os.killpg(p.pid, sig)
    else:
        p.send_signal(sig)
    try:
        code = p.wait(timeout=180)
    except subprocess.TimeoutExpired:
        p.kill()
        code = "TIMEOUT(180s)->killed"
    return time.monotonic() - t0, code


def leftovers(d: Path) -> dict:
    rates = d / "store" / "rates"
    return {"parquet.tmp": len(list(rates.glob(".*.parquet.tmp"))),
            "progress": len(list(rates.glob(".*.progress*"))),
            "spool": len(list(rates.glob(".*.spool.tmp"))),
            "parts": len(list(rates.glob("*.parquet"))),
            "downloads": len(list((d / "downloads").glob("*"))),
            "parts_": len(list((d / "downloads").glob("*.part")))}


def orphan_workers() -> int:
    out = subprocess.run(["pgrep", "-fc", "multiprocessing.spawn"], capture_output=True, text=True).stdout
    return int(out.strip() or 0)


def main():
    cases = sys.argv[1:] or ["A", "B", "C", "D"]
    SCRATCH.mkdir(parents=True, exist_ok=True)
    fsrv = subprocess.Popen([PY, str(H / "throttle_server.py"), str(FPORT), str(HTTP_ROOT), "3000000"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    lines = ["# shutdown / restart cost (Linux, scratch store, paced 3 MB/s local server, 27-file queue)", ""]
    try:
        for case in cases:
            d = fresh(case)
            sup = case == "D"
            p, log = start_serve(d, supervise=sup)
            t_ready = wait_ready()
            c, t_busy = wait_busy()
            time.sleep(8)        # let parses be mid-file
            c = counts()
            if case == "A":
                dt, code = stop_and_time(p, signal.SIGINT, label=case)
            elif case == "B":
                dt, code = stop_and_time(p, signal.SIGTERM)
            elif case == "C":
                dt, code = stop_and_time(p, signal.SIGKILL)
            else:
                dt, code = stop_and_time(p, signal.SIGINT, group=True)
            time.sleep(1.0)
            orphans = orphan_workers()
            log.close()
            text = (d / "serve.out").read_text(errors="replace")
            tb = text.count("Traceback")
            lines.append(f"## case {case}: {'serve --supervise, SIGINT to the process group' if sup else {'A': 'SIGINT', 'B': 'SIGTERM', 'C': 'SIGKILL'}[case]}")
            lines.append(f"- ready after {t_ready:.1f}s; busy (downloading>=1 and ingesting>=1) after {t_busy:.1f}s; queue at kill: {c}")
            lines.append(f"- signal -> exit: **{dt:.2f}s**, exit code {code}; tracebacks in output: {tb}; "
                         f"spawn workers still alive 1s later: {orphans}")
            lines.append(f"- leftovers after exit: {leftovers(d)}")
            tail = [ln for ln in text.splitlines()[-12:]]
            lines += ["  ```", *("  " + t[:160] for t in tail), "  ```"]
            if case == "C":
                # restart and time the catch-up
                p2, log2 = start_serve(d)
                t0 = time.monotonic()
                t_ready2 = wait_ready()
                # wait until the queue drains and the final rollup lands
                deadline = time.monotonic() + 900
                drained_at = None
                while time.monotonic() < deadline:
                    c2 = counts()
                    if c2 and not any(c2.get(k) for k in ("queued", "downloading", "fetched", "ingesting", "expanding")):
                        drained_at = time.monotonic() - t0
                        break
                    time.sleep(1.0)
                time.sleep(2)
                # watch the log for the rollup lines
                t_roll = None
                for _ in range(300):
                    txt = (d / "serve.out").read_text(errors="replace")
                    part2 = txt[txt.rfind("opening the store"):]
                    if re.search(r"rollup rebuild finished in|analytics update finished in", part2):
                        t_roll = time.monotonic() - t0
                        break
                    time.sleep(1)
                dt2, code2 = stop_and_time(p2, signal.SIGINT)
                log2.close()
                txt = (d / "serve.out").read_text(errors="replace")
                part2 = txt[txt.rfind("opening the store"):]
                keys = [ln for ln in part2.splitlines() if re.search(
                    r"recover|resumed|catch|rollup|analytics|orphan|swept|reclaimed|left mid|ready", ln, re.I)]
                lines.append(f"### restart after SIGKILL")
                lines.append(f"- dashboard ready after {t_ready2:.1f}s; queue drained {drained_at and f'{drained_at:.1f}s'} after start; "
                             f"last rollup finished {t_roll and f'{t_roll:.1f}s'} after start; "
                             f"final state {counts() or c2}; leftovers now {leftovers(d)}; SIGINT exit {dt2:.2f}s")
                lines += ["  ```", *("  " + k[:200] for k in keys[:30]), "  ```"]
            lines.append("")
    finally:
        fsrv.terminate()
    OUT.write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
