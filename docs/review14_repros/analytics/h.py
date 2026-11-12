"""Shared harness for the round-14 analytics audit (scratch only).

Drives the REAL MRF Explorer 0.7.7 (`mrfx serve` subprocess, or run_queue /
scan_inbox in-process), and checks the analytics spine from the outside.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/analytics")
REPO = Path("/home/user/Claudevideo")
PY = str(REPO / ".venv/bin/python")
CODES = ("97110", "97140", "97530", "92507")


# --------------------------------------------------------------- fixtures
def npi_from(n: int) -> str:
    base = f"1{n:08d}"[:9]
    digits = "80840" + base
    tot = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        tot += d
    return base + str((10 - tot % 10) % 10)


def gen_mrf(path: Path, payer: str, tin_start: int, n_tins: int, codes=CODES,
            month: str = "2026-10-01", rate_base: float = 80.0, seed: int = 0) -> int:
    """A small in-network file: n_tins EINs x codes, one NPI each. Returns the
    number of (tin, code) price lines written."""
    rnd = random.Random(seed)
    items = []
    for c_i, code in enumerate(codes):
        nr = []
        for t in range(tin_start, tin_start + n_tins):
            tin = f"43-{t:07d}"
            nr.append({
                "provider_groups": [{"npi": [int(npi_from(t))],
                                     "tin": {"type": "ein", "value": tin}}],
                "negotiated_prices": [{
                    "negotiated_type": "negotiated",
                    "negotiated_rate": round(rate_base + c_i * 5 + rnd.random() * 20, 2),
                    "service_code": ["11"], "billing_class": "professional",
                    "expiration_date": "9999-12-31"}]})
        items.append({"negotiation_arrangement": "ffs", "name": f"code {code}",
                      "billing_code_type": "CPT", "billing_code_type_version": "2026",
                      "billing_code": code, "description": f"code {code}",
                      "negotiated_rates": nr})
    doc = {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
           "last_updated_on": month, "version": "1.0.0",
           "provider_references": [], "in_network": items}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)
    return n_tins * len(codes)


# --------------------------------------------------------------- config
def write_cfg(d: Path, port: int, **extra) -> Path:
    d = Path(d)
    for sub in ("inbox", "processed", "failed", "store", "downloads", "config"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    cfg = {
        "inbox_dir": str(d / "inbox"), "processed_dir": str(d / "processed"),
        "failed_dir": str(d / "failed"), "store_dir": str(d / "store"),
        "downloads_dir": str(d / "downloads"),
        "entity_map_path": str(d / "config" / "entity_map.yaml"),
        "port": port,
        "enrichment": {"mode": "off"},
        "monthly_recheck": False,
        "parallel_ingests": 2,
        "duckdb_memory_gb": 2,
        "render_js": False,
    }
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    import yaml
    p = d / "config" / "mrfx.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


# --------------------------------------------------------------- servers
class FileServer:
    """A local static HTTP server (its own process) over `directory`."""

    def __init__(self, directory: Path, port: int):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.p = subprocess.Popen(
            [sys.executable, "-I", "-m", "http.server", str(port), "--bind", "127.0.0.1",
             "--directory", str(directory)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                httpx.get(self.base + "/", timeout=1, trust_env=False)
                return
            except httpx.HTTPError:
                time.sleep(0.1)

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"

    def close(self):
        self.p.terminate()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()


class Serve:
    """`mrfx --config <ABS> serve` as a subprocess (its own Popen handle)."""

    def __init__(self, cfg_path: Path, port: int, log: Path, python: str = PY,
                 cwd: Path | None = None, env_extra: dict | None = None,
                 args: list[str] | None = None):
        self.port = port
        self.log = log
        self.t0 = time.time()
        env = dict(os.environ)
        env.pop("HTTPS_PROXY", None) if False else None
        env["PYTHONUNBUFFERED"] = "1"
        if env_extra:
            env.update(env_extra)
        self.fh = open(log, "ab")
        cmd = [python, "-m", "mrfx", "--config", str(Path(cfg_path).resolve()), "serve"]
        if args:
            cmd = [python, *args]
        self.p = subprocess.Popen(cmd, cwd=str(cwd or Path(cfg_path).resolve().parent.parent),
                                  stdout=self.fh, stderr=subprocess.STDOUT, env=env)

    def wait_ready(self, timeout=180) -> float:
        t = time.time()
        while time.time() - t < timeout:
            if self.p.poll() is not None:
                raise RuntimeError(f"serve exited {self.p.returncode}; see {self.log}")
            try:
                r = httpx.get(f"http://127.0.0.1:{self.port}/api/boot", timeout=5, trust_env=False)
                if r.status_code == 200:
                    return time.time() - t
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise TimeoutError("serve never answered")

    def stop(self, timeout=120):
        """Graceful: SIGTERM is treated as Ctrl-C by serve."""
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(30)
        self.fh.close()

    def kill(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait(30)
        self.fh.close()

    def rss_mb(self) -> float:
        """RSS of the serve process + its children (parser workers)."""
        tot = 0
        pids = [self.p.pid]
        try:
            kids = Path(f"/proc/{self.p.pid}/task/{self.p.pid}/children").read_text().split()
            pids += [int(k) for k in kids]
        except OSError:
            pass
        for pid in pids:
            try:
                for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if ln.startswith("VmRSS:"):
                        tot += int(ln.split()[1])
            except OSError:
                pass
        return tot / 1024

    def main_rss_mb(self) -> float:
        try:
            for ln in Path(f"/proc/{self.p.pid}/status").read_text().splitlines():
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) / 1024
        except OSError:
            pass
        return 0.0


def api(port: int, path: str, timeout=120, method="GET", **kw):
    url = f"http://127.0.0.1:{port}{path}"
    r = httpx.request(method, url, timeout=timeout, trust_env=False, **kw)
    try:
        return r.json()
    except ValueError:
        return {"_status": r.status_code, "_text": r.text[:300]}


def add_links(port: int, urls: list[str]) -> dict:
    return api(port, "/api/urls", method="POST", json={"urls": urls})


def snap(port: int, payers_of_interest=(), code: str = "97110") -> dict:
    """What the screens say right now."""
    t = time.time()
    out = {"t": t}
    try:
        st = api(port, "/api/stats")
        out["analytics"] = st.get("analytics")
        out["data_generation"] = st.get("data_generation")
    except Exception as e:  # noqa: BLE001
        out["err_stats"] = repr(e)
    try:
        pp = api(port, "/api/payers")
        out["payers"] = pp.get("payers")
        out["covered"] = pp.get("covered")
    except Exception as e:  # noqa: BLE001
        out["err_payers"] = repr(e)
    try:
        r = api(port, "/api/rates?grain=tin&page_size=1")
        out["rates_total"] = r.get("total")
    except Exception as e:  # noqa: BLE001
        out["err_rates"] = repr(e)
    for p in payers_of_interest:
        try:
            r = api(port, f"/api/rates?grain=tin&page_size=1&payer={httpx.QueryParams({'p': p})['p']}")
            out.setdefault("rates_by_payer", {})[p] = r.get("total")
        except Exception as e:  # noqa: BLE001
            out.setdefault("rates_by_payer", {})[p] = repr(e)
    try:
        c = api(port, f"/api/code/{code}")
        out["code_payer_rank"] = {r.get("payer"): r.get("median_rate") for r in (c.get("payer_rank") or [])}
        out["code_hist_n"] = sum(int(b.get("n") or 0) for b in (c.get("histogram") or []))
    except Exception as e:  # noqa: BLE001
        out["err_code"] = repr(e)
    out["took"] = round(time.time() - t, 2)
    return out


class Poller(threading.Thread):
    """Records /api/stats.analytics (+ payers) every `every` s into a list."""

    def __init__(self, port: int, every: float = 1.0, light: bool = True):
        super().__init__(daemon=True)
        self.port, self.every, self.light = port, every, light
        self.rows: list[dict] = []
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            t = time.time()
            row = {"t": t}
            try:
                st = api(self.port, "/api/stats", timeout=60)
                row["a"] = st.get("analytics")
                row["gen"] = st.get("data_generation")
                if not self.light:
                    pp = api(self.port, "/api/payers", timeout=60)
                    row["covered"] = pp.get("covered")
                    row["payers"] = pp.get("payers")
            except Exception as e:  # noqa: BLE001
                row["err"] = repr(e)[:200]
            self.rows.append(row)
            self.halt.wait(max(0.0, self.every - (time.time() - t)))

    def stop(self):
        self.halt.set()
        self.join(30)


def states_timeline(rows, t0=0.0):
    """Compress poller rows into state transitions."""
    out = []
    last = None
    for r in rows:
        a = r.get("a") or {}
        key = (a.get("state"), a.get("covered_files"), a.get("done_files"),
               a.get("covered_payers"), a.get("loaded_payers"))
        if key != last:
            out.append({"dt": round(r["t"] - t0, 1), "state": a.get("state"),
                        "files": f"{a.get('covered_files')}/{a.get('done_files')}",
                        "payers": f"{a.get('covered_payers')}/{a.get('loaded_payers')}",
                        "sentence": a.get("sentence"), "err": r.get("err")})
            last = key
    return out


# --------------------------------------------------------------- truth (offline)
GRAIN = ("payer", "tin_value", "billing_code", "modifier_set", "billing_class",
         "service_code_set", "file_month", "is_dollar_rate", "tin_is_really_npi")


def truth(store_dir: Path, hash_rows: bool = True) -> dict:
    """With the server STOPPED: the spine vs the spine rebuilt from the raw
    parts (ground truth), per done file. Read-only on the DB."""
    import duckdb
    sys.path.insert(0, str(REPO))
    from mrfx.store import SPINE_QUERY
    db = Path(store_dir) / "mrfx.duckdb"
    con = duckdb.connect(str(db), read_only=True)
    con.execute("SET enable_progress_bar=false")
    out = {}
    files = con.execute(
        "SELECT filename, payer, rows_emitted, finished_at::VARCHAR, coalesce(file_type,'') "
        "FROM files WHERE status='done' ORDER BY filename").fetchall()
    out["done_files"] = len(files)
    has = con.execute("SELECT count(*) FROM information_schema.tables "
                      "WHERE table_name='rates_by_tin_tbl'").fetchone()[0]
    out["spine_exists"] = bool(has)
    glob = str(Path(store_dir) / "rates" / "*.parquet")
    parts = list((Path(store_dir) / "rates").glob("*.parquet"))
    if not parts:
        out["raw_rows"] = 0
        return out
    con.execute(f"CREATE TEMP VIEW raw AS SELECT * FROM read_parquet('{glob}')")
    out["raw_rows"] = con.execute("SELECT count(*) FROM raw").fetchone()[0]
    per_src = dict(con.execute("SELECT source_file, count(*) FROM raw GROUP BY 1").fetchall())
    meta = dict(con.execute("SELECT key, value FROM meta WHERE key LIKE 'rollup%' "
                            "OR key IN ('data_generation')").fetchall())
    out["meta"] = {k: (v[:120] if isinstance(v, str) else v) for k, v in meta.items()}
    if not has:
        out["files"] = [{"f": f, "rows_emitted": r, "raw": per_src.get(f, 0)} for f, _p, r, _t, _ft in files]
        return out
    cols = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='rates_by_tin_tbl' "
        "ORDER BY ordinal_position").fetchall()]
    cl = ", ".join(cols)
    exp_sql = SPINE_QUERY.format(part="TRUE").replace("FROM rates\n", "FROM raw\n")
    assert "FROM raw" in exp_sql
    con.execute(f"CREATE TEMP TABLE expected AS SELECT {cl} FROM ({exp_sql})")
    out["spine_rows"] = con.execute("SELECT count(*) FROM rates_by_tin_tbl").fetchone()[0]
    out["expected_rows"] = con.execute("SELECT count(*) FROM expected").fetchone()[0]
    out["missing"] = con.execute(
        f"SELECT count(*) FROM (SELECT {cl} FROM expected EXCEPT ALL SELECT {cl} FROM rates_by_tin_tbl)").fetchone()[0]
    out["extra"] = con.execute(
        f"SELECT count(*) FROM (SELECT {cl} FROM rates_by_tin_tbl EXCEPT ALL SELECT {cl} FROM expected)").fetchone()[0]
    out["spine_payers"] = sorted(r[0] for r in con.execute(
        "SELECT DISTINCT payer FROM rates_by_tin_tbl").fetchall())
    out["raw_payers"] = sorted(r[0] for r in con.execute("SELECT DISTINCT payer FROM raw").fetchall())
    # per file: its grain keys missing from the spine
    on = " AND ".join(f"s.{c} IS NOT DISTINCT FROM k.{c}" for c in GRAIN)
    keys_sql = (
        "SELECT DISTINCT source_file, payer, tin_value, billing_code, "
        "coalesce(array_to_string(billing_code_modifier, '|'), '') AS modifier_set, billing_class, "
        "coalesce(array_to_string(service_code, '|'), '') AS service_code_set, file_month, "
        "is_dollar_rate, tin_is_really_npi FROM raw "
        "WHERE negotiated_rate IS NULL OR isfinite(negotiated_rate)")
    miss = dict(con.execute(
        f"SELECT k.source_file, count(*) FILTER (WHERE s.payer IS NULL) FROM ({keys_sql}) k "
        f"LEFT JOIN (SELECT DISTINCT {', '.join(GRAIN)} FROM rates_by_tin_tbl) s ON {on} "
        "GROUP BY 1").fetchall())
    nkeys = dict(con.execute(f"SELECT source_file, count(*) FROM ({keys_sql}) GROUP BY 1").fetchall())
    out["files"] = [{"f": f, "payer": p, "rows_emitted": r, "raw": per_src.get(f, 0),
                     "keys": nkeys.get(f, 0), "keys_missing": miss.get(f, 0), "type": ft}
                    for f, p, r, _t, ft in files]
    out["files_fully_in_spine"] = sum(1 for x in out["files"]
                                      if x["type"] == "provider_reference"
                                      or (x["keys"] > 0 and x["keys_missing"] == 0)
                                      or (x["rows_emitted"] == 0))
    out["files_rowcount_mismatch"] = [x["f"] for x in out["files"]
                                      if x["type"] != "provider_reference" and x["rows_emitted"] != x["raw"]]
    if hash_rows:
        out["hash_spine"] = table_hash(con, "rates_by_tin_tbl")
        out["hash_dir"] = table_hash(con, "tin_directory_tbl")
    con.close()
    return out


def table_hash(con, tbl: str) -> str:
    """md5 over the table's rows sorted by every column (small stores) — the
    'sorted export' hash."""
    import hashlib
    n = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
    if n > 3_000_000:
        return set_hash(con, tbl)
    h = hashlib.md5()
    cur = con.execute(f"SELECT * FROM {tbl} ORDER BY ALL")
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            h.update(repr(r).encode())
            h.update(b"\n")
    return f"{n}:{h.hexdigest()}"


def set_hash(con, tbl: str) -> str:
    """Order-independent multiset hash inside DuckDB (big tables)."""
    r = con.execute(
        f"SELECT count(*), sum(h::HUGEINT)::VARCHAR, bit_xor(h) FROM "
        f"(SELECT hash(COLUMNS(*)) AS h FROM {tbl})").fetchone()
    return f"set:{r[0]}:{r[1]}:{r[2]}"


def run_truth_subprocess(store_dir: Path, hash_rows=True) -> dict:
    """truth() in a fresh process (no DuckDB instance shared with anything)."""
    code = ("import json,sys; sys.path.insert(0, %r); import h; "
            "print(json.dumps(h.truth(%r, %r), default=str))") % (str(ROOT), str(store_dir), hash_rows)
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode:
        raise RuntimeError(r.stderr[-2000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def short_truth(t: dict) -> dict:
    keep = ("done_files", "raw_rows", "spine_rows", "expected_rows", "missing", "extra",
            "files_fully_in_spine", "files_rowcount_mismatch", "spine_payers", "raw_payers",
            "hash_spine", "hash_dir")
    return {k: t.get(k) for k in keep}


def save(name: str, obj) -> Path:
    p = ROOT / "out" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, default=str))
    return p


def grep_log(log: Path, *pats) -> list[str]:
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [ln for ln in lines if any(p in ln for p in pats)]
