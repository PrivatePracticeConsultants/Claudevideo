# timeline — variant `wave2_cadence20` overrides={} keep_warm=True reuse_store=default_warm http_root=http_wave2 stale_s=20

files: 14 (339 MB gz); processed=14; wall=76.7s; rollups=2 (54.8s total); url_progress UPDATEs=28 (0.59s); workers=3 downloads=6

| file | MB gz | dl start | dl s | wait fetched s | ing start | ing s | preflight s | fp s | finish_ok s | finalize s | rows |
|---|---|---|---|---|---|---|---|---|---|---|---|
| w2_plan10_2026-10_in-network-rates_04_of_14.json | 8.1 | 0.1 | 0.27 | 0.1 | 0.4 | 3.1 | 0.03 | 0.07 | 0.02 | 0.02 | 352570 |
| w2_plan10_2026-10_in-network-rates_11_of_14.json | 37.7 | 0.1 | 0.95 | 2.6 | 3.6 | 11.2 | 0.01 | 0.11 | 0.03 | 0.02 | 1664521 |
| w2_plan11_2026-10_in-network-rates_05_of_14.json | 10.3 | 0.1 | 0.31 | 0.1 | 0.5 | 3.9 | 0.02 | 0.06 | 0.03 | 0.01 | 446152 |
| w2_plan11_2026-10_in-network-rates_12_of_14.json | 44.5 | 0.1 | 1.08 | 3.2 | 4.4 | 13.4 | 0.01 | 0.10 | 0.03 | 0.02 | 1946792 |
| w2_plan12_2026-10_in-network-rates_06_of_14.json | 13.4 | 0.1 | 0.39 | 0.1 | 0.6 | 4.7 | 0.02 | 0.09 | 0.03 | 0.02 | 599415 |
| w2_plan12_2026-10_in-network-rates_13_of_14.json | 52.0 | 0.1 | 1.10 | 4.2 | 5.4 | 16.0 | 0.01 | 0.13 | 0.06 | 0.04 | 2308890 |
| w2_plan13_2026-10_in-network-rates_07_of_14.json | 16.9 | 0.5 | 0.47 | 13.9 | 14.9 | 5.3 | 0.01 | 0.11 | 0.03 | 0.01 | 748772 |
| w2_plan13_2026-10_in-network-rates_14_of_14.json | 60.2 | 0.5 | 0.96 | 16.3 | 17.8 | 28.8 | 0.01 | 0.04 | 0.05 | 0.03 | 2694926 |
| w2_plan7_2026-10_in-network-rates_01_of_14.json. | 5.1 | 0.7 | 0.14 | 19.4 | 20.2 | 3.2 | 0.01 | 0.03 | 0.02 | 0.04 | 229774 |
| w2_plan7_2026-10_in-network-rates_08_of_14.json. | 21.1 | 0.9 | 0.52 | 20.1 | 21.5 | 10.5 | 0.02 | 0.07 | 0.05 | 0.04 | 918914 |
| w2_plan8_2026-10_in-network-rates_02_of_14.json. | 5.4 | 1.2 | 0.19 | 22.2 | 23.5 | 3.2 | 0.02 | 0.06 | 0.04 | 0.03 | 238513 |
| w2_plan8_2026-10_in-network-rates_09_of_14.json. | 26.0 | 1.2 | 0.31 | 25.4 | 26.9 | 14.1 | 0.01 | 0.05 | 0.06 | 0.03 | 1152380 |
| w2_plan9_2026-10_in-network-rates_03_of_14.json. | 6.6 | 20.2 | 0.16 | 11.8 | 32.1 | 3.8 | 0.02 | 0.08 | 0.03 | 0.03 | 299232 |
| w2_plan9_2026-10_in-network-rates_10_of_14.json. | 31.7 | 20.2 | 0.35 | 15.5 | 36.0 | 13.0 | 0.02 | 0.05 | 0.03 | 0.02 | 1403623 |

sum download 7.2s, sum wait-in-fetched 154.9s, sum ingest 134.3s

- rollup update_rollups_incremental at t=47.8s took 27.4s -> 27.436891227999695
- rollup update_rollups_incremental at t=76.6s took 27.4s -> 27.356073164999998

write_lock: held 3.43s of 76.7s wall (4.5%); 510 acquisitions; held while a rollup was active 1.85s
  hold by thread: mrfx-proc-1 0.81s, mrfx-proc-2 0.52s, mrfx-prefetch-2 0.42s, mrfx-prefetch-1 0.38s, mrfx-prefetch-0 0.27s, mrfx-prefetch-4 0.26s, mrfx-proc-0 0.25s, mrfx-prefetch-5 0.24s, mrfx-prefetch-3 0.16s, MainThread 0.11s
  waits >2ms: 337 totalling 7.70s; by thread: mrfx-prefetch-1 1.39s, mrfx-prefetch-0 1.23s, mrfx-prefetch-2 1.13s, mrfx-prefetch-3 1.05s, mrfx-prefetch-4 0.98s, mrfx-prefetch-5 0.94s, mrfx-proc-2 0.39s, mrfx-proc-1 0.37s, mrfx-proc-0 0.20s, MainThread 0.02s
  longest holds: mrfx-proc-1@26.4s 274ms (rollup), mrfx-prefetch-5@20.4s 44ms (rollup), mrfx-proc-2@31.9s 40ms (rollup), mrfx-proc-2@21.3s 40ms (rollup), mrfx-proc-1@23.4s 39ms (rollup), mrfx-prefetch-2@36.1s 39ms (rollup)
  longest waits: mrfx-prefetch-3@20.2s 75ms, mrfx-prefetch-1@36.1s 63ms, mrfx-proc-2@36.0s 61ms, mrfx-prefetch-0@36.1s 58ms, mrfx-prefetch-0@20.2s 56ms, mrfx-prefetch-1@20.2s 53ms

parsers starved (no fetched file, a parser slot idle, downloads/queue still pending): ~0.5s
downloaders idle with queue pending (prefetch cap reached): ~18.5s
mean CPU: workers 1.52 cores, main 2.11 cores (box has 4)
main process io: read_bytes 134 MB, write_bytes 526 MB, rchar 4499 MB, wchar 524 MB
peak RSS (VmHWM): main 4426 MB; workers 560, 671, 724 MB
worker io (last snapshot): pid 12450: rchar 159 MB wchar 31 MB read_bytes 12 MB write_bytes 31 MB; pid 12455: rchar 198 MB wchar 39 MB read_bytes 17 MB write_bytes 40 MB; pid 12456: rchar 183 MB wchar 36 MB read_bytes 32 MB write_bytes 36 MB
max .wal size seen: 4.2 MB (sampled every 0.5s)

queue end states: {'done': 41}