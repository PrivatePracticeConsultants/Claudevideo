"""Case 11 — per-download overhead on a real payer file served from localhost:
HTTP requests, bytes transferred vs size, full-file hash passes (ContentHasher
bytes, _sha_range bytes, file_fingerprint / hash_file calls), progress
callbacks (count + DuckDB cost per call), wall-clock of the download stage;
then the ingest stage's extra read passes on a smaller real file."""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, mk_cfg, quiet_logs, row  # noqa: E402

import mrfx.fetch as F  # noqa: E402
import mrfx.identity as I  # noqa: E402
from mrfx.fetch import add_urls, fetch_url_record, filename_for, process_url_record  # noqa: E402

REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/real")
BIG = REAL / "wy_2026-11_710_26B0_in-network-rates.json.gz"        # 118 MB
SMALL = REAL / "wy_2026-11_430_65B0_in-network-rates_2_of_4.json.gz"  # 24 MB

counters = {"hasher_bytes": 0, "sha_range_bytes": 0, "sha_range_calls": 0,
            "file_fingerprint_calls": 0, "hash_file_calls": 0, "progress_calls": 0,
            "progress_time": 0.0, "gz_members_calls": 0}


class CountingHasher(I.ContentHasher):
    def update(self, chunk):
        counters["hasher_bytes"] += len(chunk)
        super().update(chunk)

    def gz_members(self, path):
        counters["gz_members_calls"] += 1
        return super().gz_members(path)


def instrument():
    F.ContentHasher = CountingHasher
    real_sr = F._sha_range

    def sr(path, start, n):
        counters["sha_range_calls"] += 1
        counters["sha_range_bytes"] += n
        return real_sr(path, start, n)
    F._sha_range = sr
    real_ff = F.file_fingerprint

    def ff(path, head_mb):
        counters["file_fingerprint_calls"] += 1
        return real_ff(path, head_mb)
    F.file_fingerprint = ff
    real_hf = I.hash_file

    def hf(path):
        counters["hash_file_calls"] += 1
        return real_hf(path)
    I.hash_file = hf


def wrap_progress(store):
    real = store.url_progress

    def up(url_id, d, t):
        t0 = time.perf_counter()
        try:
            return real(url_id, d, t)
        finally:
            counters["progress_calls"] += 1
            counters["progress_time"] += time.perf_counter() - t0
    store.url_progress = up


def stage(label, cfg, store, srv, path, fn):
    for k in counters:
        counters[k] = 0 if not isinstance(counters[k], float) else 0.0
    n0 = len(srv.log)
    t0 = time.perf_counter()
    ok = fn()
    dt = time.perf_counter() - t0
    hits = srv.log[n0:]
    size = len(srv.files[path])
    print(f"[{label}] {dt:.1f}s ok={ok} | requests {len(hits)}: {[(x['status'], x['range'] or '-', x['sent']) for x in hits][:4]} "
          f"| bytes sent {sum(x['sent'] for x in hits) / 1e6:.1f} MB for {size / 1e6:.1f} MB")
    print(f"    hash passes: hasher {counters['hasher_bytes'] / 1e6:.1f} MB ({counters['hasher_bytes'] / max(size, 1):.2f}x size), "
          f"_sha_range {counters['sha_range_calls']} calls / {counters['sha_range_bytes'] / 1e6:.1f} MB, "
          f"file_fingerprint {counters['file_fingerprint_calls']}, hash_file {counters['hash_file_calls']}, "
          f"gz_members {counters['gz_members_calls']}")
    print(f"    progress callbacks: {counters['progress_calls']} ({counters['progress_time'] * 1e3:.1f} ms in DuckDB total, "
          f"{counters['progress_time'] * 1e3 / max(counters['progress_calls'], 1):.2f} ms each)")


if __name__ == "__main__":
    quiet_logs(level=60)
    instrument()
    cfg, store = mk_cfg("c11", early_dedup_mb=15, parallel_ingests=1)
    wrap_progress(store)
    srv = Srv({"/big.json.gz": BIG.read_bytes(), "/small.json.gz": SMALL.read_bytes()}, {})
    ub, us = srv.base + "/big.json.gz", srv.base + "/small.json.gz"
    try:
        add_urls(store, [ub, us])
        rb = store.next_queued_url()
        stage("download stage, 118 MB real file (fetch_url_record)", cfg, store, srv, "/big.json.gz",
              lambda: fetch_url_record(cfg, store, rb))
        dest = cfg.downloads_dir / filename_for(ub)
        print(f"    on disk after fetch: {[(p.name, p.stat().st_size) for p in cfg.downloads_dir.iterdir()]}")
        dest.unlink(missing_ok=True); F._meta_path(dest).unlink(missing_ok=True)   # keep the disk free
        store.update_url(rb["id"], status="skipped")   # its bytes are gone: keep it out of the ingest stage
        # throttled: progress callbacks per unit time on a slow link (1 MB/s for 24 MB)
        srv.rules["/small.json.gz"] = {"delay": 0.06}
        rs = store.next_queued_url()
        stage("download stage, 24 MB real file at ~1 MB/s", cfg, store, srv, "/small.json.gz",
              lambda: fetch_url_record(cfg, store, rs))
        # ingest stage of the 24 MB file (already fetched): extra read passes
        rs2 = store.next_fetched_url()
        stage("ingest stage, 24 MB real file (process_url_record)", cfg, store, srv, "/small.json.gz",
              lambda: process_url_record(cfg, store, rs2))
        print("    row:", {k: row(store, us)[k] for k in ("status", "kind", "rows_emitted")})
    finally:
        srv.close()
        shutil.rmtree(cfg.store_dir.parent, ignore_errors=True)
