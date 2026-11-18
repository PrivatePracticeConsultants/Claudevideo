# timeline — variant `mem_big` overrides={'parallel_ingests': 3, 'parallel_downloads': 3} keep_warm=True reuse_store=None http_root=http_big stale_s=None

files: 3 (471 MB gz); processed=3; wall=55.9s; rollups=2 (11.4s total); url_progress UPDATEs=14 (1.40s); workers=3 downloads=3

| file | MB gz | dl start | dl s | wait fetched s | ing start | ing s | preflight s | fp s | finish_ok s | finalize s | rows |
|---|---|---|---|---|---|---|---|---|---|---|---|
| uhc_2026-11-01_UnitedHealthcare-Insurance-Compan | 261.7 | 0.0 | 10.43 | 0.1 | 10.6 | 33.1 | 0.04 | 0.09 | 0.03 | 0.03 | 2008635 |
| wy_2026-11_590_10P0_in-network-rates_2_of_2.json | 90.5 | 0.0 | 4.64 | 0.2 | 4.9 | 9.7 | 0.01 | 0.20 | 0.03 | 0.04 | 11551 |
| wy_2026-11_710_26B0_in-network-rates.json.gz | 118.7 | 0.1 | 1.10 | 0.7 | 1.8 | 15.5 | 0.03 | 0.48 | 0.03 | 0.03 | 1757643 |

sum download 16.2s, sum wait-in-fetched 0.9s, sum ingest 58.3s

- rollup update_rollups_incremental at t=44.3s took 0.0s -> RAISED RuntimeError: rollup tables were built by an older version (or never built)
- rollup rebuild_rollups at t=55.7s took 11.4s -> 11.408245069999992

write_lock: held 3.48s of 55.9s wall (6.2%); 180 acquisitions; held while a rollup was active 0.07s
  hold by thread: mrfx-proc-0 0.92s, mrfx-prefetch-2 0.87s, mrfx-proc-1 0.55s, mrfx-prefetch-1 0.54s, mrfx-prefetch-0 0.36s, mrfx-proc-2 0.16s, MainThread 0.09s
  waits >2ms: 92 totalling 3.46s; by thread: mrfx-prefetch-0 0.95s, mrfx-prefetch-2 0.94s, mrfx-proc-1 0.56s, mrfx-proc-2 0.53s, mrfx-prefetch-1 0.29s, mrfx-proc-0 0.19s
  longest holds: mrfx-prefetch-2@0.1s 417ms, mrfx-proc-0@1.3s 251ms, mrfx-proc-1@9.0s 201ms, mrfx-prefetch-2@1.2s 179ms, mrfx-prefetch-1@3.6s 179ms, mrfx-proc-0@10.2s 168ms
  longest waits: mrfx-prefetch-0@1.3s 279ms, mrfx-prefetch-2@1.4s 268ms, mrfx-proc-1@1.3s 255ms, mrfx-proc-2@1.3s 250ms, mrfx-prefetch-0@0.3s 234ms, mrfx-proc-1@1.6s 187ms

parsers starved (no fetched file, a parser slot idle, downloads/queue still pending): ~10.0s
downloaders idle with queue pending (prefetch cap reached): ~0.0s
mean CPU: workers 1.10 cores, main 0.39 cores (box has 4)
main process io: read_bytes 0 MB, write_bytes 536 MB, rchar 837 MB, wchar 534 MB
peak RSS (VmHWM): main 1977 MB; workers 748, 240, 617 MB
worker io (last snapshot): pid 611: rchar 184 MB wchar 16 MB read_bytes 0 MB write_bytes 16 MB; pid 664: rchar 110 MB wchar 6 MB read_bytes 0 MB write_bytes 7 MB; pid 812: rchar 303 MB wchar 28 MB read_bytes 0 MB write_bytes 28 MB
max .wal size seen: 1.8 MB (sampled every 0.5s)

queue end states: {'done': 3}