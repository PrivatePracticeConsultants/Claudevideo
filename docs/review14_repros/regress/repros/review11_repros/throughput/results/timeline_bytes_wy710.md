# timeline — variant `bytes_wy710` overrides={'parallel_ingests': 2, 'parallel_downloads': 1} keep_warm=True reuse_store=None http_root=http_root stale_s=None

files: 1 (119 MB gz); processed=1; wall=17.5s; rollups=2 (2.5s total); url_progress UPDATEs=4 (0.29s); workers=2 downloads=1

| file | MB gz | dl start | dl s | wait fetched s | ing start | ing s | preflight s | fp s | finish_ok s | finalize s | rows |
|---|---|---|---|---|---|---|---|---|---|---|---|
| wy_2026-11_710_26B0_in-network-rates.json.gz | 118.7 | 0.1 | 4.75 | 0.1 | 4.9 | 9.8 | 0.03 | 0.06 | 0.05 | 0.03 | 1757643 |

sum download 4.8s, sum wait-in-fetched 0.1s, sum ingest 9.8s

- rollup update_rollups_incremental at t=14.8s took 0.0s -> RAISED RuntimeError: rollup tables were built by an older version (or never built)
- rollup rebuild_rollups at t=17.3s took 2.5s -> 2.4548544179999965

write_lock: held 0.81s of 17.5s wall (4.6%); 44 acquisitions; held while a rollup was active 0.05s
  hold by thread: mrfx-prefetch-0 0.55s, mrfx-proc-1 0.13s, MainThread 0.08s, mrfx-proc-0 0.05s
  waits >2ms: 13 totalling 0.12s; by thread: mrfx-prefetch-0 0.05s, mrfx-proc-0 0.04s, mrfx-proc-1 0.03s
  longest holds: mrfx-prefetch-0@0.1s 249ms, mrfx-prefetch-0@1.0s 208ms, mrfx-prefetch-0@1.8s 34ms, mrfx-proc-0@4.8s 29ms, mrfx-proc-1@14.6s 25ms, MainThread@14.8s 22ms (rollup)
  longest waits: mrfx-prefetch-0@4.8s 24ms, mrfx-proc-1@0.1s 12ms, mrfx-proc-0@4.8s 12ms, mrfx-proc-0@4.9s 11ms, mrfx-proc-0@14.7s 10ms, mrfx-prefetch-0@4.9s 8ms

parsers starved (no fetched file, a parser slot idle, downloads/queue still pending): ~4.5s
downloaders idle with queue pending (prefetch cap reached): ~0.0s
mean CPU: workers 0.77 cores, main 0.55 cores (box has 4)
main process io: read_bytes 27 MB, write_bytes 143 MB, rchar 260 MB, wchar 142 MB
peak RSS (VmHWM): main 1927 MB; workers 759 MB
worker io (last snapshot): pid 463: rchar 184 MB wchar 16 MB read_bytes 15 MB write_bytes 16 MB
max .wal size seen: 1.4 MB (sampled every 0.5s)

queue end states: {'done': 1}