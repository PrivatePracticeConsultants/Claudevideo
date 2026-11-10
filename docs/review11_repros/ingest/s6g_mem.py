"""Scenario 6G: peak RSS of an ingest (fresh subprocess, VmHWM) for a rate file whose groups all
cite a 1,000-id companion and which has NO own table (every group deferred until EOF), for
~100 / 200 MB raw on the small single-pass path, fast and classic. The fixture is streamed to
disk so the measuring process never holds it. usage: s6g_mem.py [child <run> <file> <fast>]"""
import subprocess, sys, os
from h import *
from mrfx.ingest import ingest_file


def write_fixture(path: Path, groups: int, codes=("97110", "97112", "97140", "97530", "97535")):
    per = groups // len(codes)
    with gzip.open(path, "wb", compresslevel=1) as f:
        f.write(json.dumps({**HDR})[:-1].encode() + b', "in_network": [')
        for ci, c in enumerate(codes):
            if ci: f.write(b",")
            f.write(b'{"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": "%s", "negotiated_rates": [' % c.encode())
            for i in range(per):
                if i: f.write(b",")
                f.write(b'{"provider_references": [%d], "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": %.1f, '
                        b'"billing_class": "professional", "service_code": ["11"]}]}' % (i % 1000, 10.0 + (i % 50)))
            f.write(b"]}")
        f.write(b"]}")


def child(run, fname, fast):
    cfg = mkcfg(run, fast=fast == "1", fresh=False); st = Store(cfg.store_dir)
    p = cfg.inbox_dir / fname
    t0 = time.time(); r = ingest_file(cfg, st, p); dt_ = time.time() - t0
    print(json.dumps({"status": r["status"], "rows": r.get("rows"), "secs": round(dt_, 1), "peak_mb": round(peak_mb()), "rss_after": round(rss_mb())}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        child(*sys.argv[2:5]); sys.exit(0)
    for groups in (750_000, 1_500_000):
        for fast in ("1", "0"):
            run = f"s6g_{groups}_{fast}"
            cfg = mkcfg(run, fast=fast == "1"); st = Store(cfg.store_dir)
            ref = {**HDR, "provider_references": [{"provider_group_id": i, "provider_groups": [grp([str(1000000000 + i)], f"{100000000 + i}")]} for i in range(1000)]}
            ingest_file(cfg, st, put(cfg, "acme_refs_2026-05.json", ref))
            fx = cfg.inbox_dir / "acme_rates_2026-05.json.gz"
            write_fixture(fx, groups); gz_mb = fx.stat().st_size / 1e6
            raw_mb = groups * 133 / 1e6
            st.close()
            out = subprocess.run([sys.executable, __file__, "child", run, fx.name, fast], capture_output=True, text=True, timeout=1200)
            print(f"groups={groups:,} (~{raw_mb:.0f} MB raw, {gz_mb:.1f} MB gz) fast={fast}: {out.stdout.strip()} {out.stderr[-200:] if out.returncode else ''}")
            shutil.rmtree(BASE / run, ignore_errors=True)
