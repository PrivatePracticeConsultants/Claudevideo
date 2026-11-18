# timeline — variant `wave2` overrides={} keep_warm=True reuse_store=default_warm http_root=http_wave2 stale_s=None

files: 14 (339 MB gz); processed=14; wall=80.6s; rollups=1 (41.1s total); url_progress UPDATEs=28 (0.61s); workers=3 downloads=6

| file | MB gz | dl start | dl s | wait fetched s | ing start | ing s | preflight s | fp s | finish_ok s | finalize s | rows |
|---|---|---|---|---|---|---|---|---|---|---|---|
| w2_plan10_2026-10_in-network-rates_04_of_14.json | 8.1 | 0.1 | 0.22 | 0.1 | 0.4 | 3.3 | 0.02 | 0.06 | 0.03 | 0.02 | 352570 |
| w2_plan10_2026-10_in-network-rates_11_of_14.json | 37.7 | 0.1 | 1.10 | 2.5 | 3.7 | 11.5 | 0.01 | 0.10 | 0.03 | 0.02 | 1664521 |
| w2_plan11_2026-10_in-network-rates_05_of_14.json | 10.3 | 0.1 | 0.31 | 0.1 | 0.5 | 3.9 | 0.01 | 0.06 | 0.02 | 0.01 | 446152 |
| w2_plan11_2026-10_in-network-rates_12_of_14.json | 44.5 | 0.1 | 1.01 | 3.3 | 4.4 | 13.2 | 0.01 | 0.13 | 0.03 | 0.02 | 1946792 |
| w2_plan12_2026-10_in-network-rates_06_of_14.json | 13.4 | 0.1 | 0.39 | 0.2 | 0.7 | 4.6 | 0.02 | 0.13 | 0.03 | 0.01 | 599415 |
| w2_plan12_2026-10_in-network-rates_13_of_14.json | 52.0 | 0.1 | 1.22 | 4.0 | 5.4 | 15.5 | 0.01 | 0.07 | 0.03 | 0.03 | 2308890 |
| w2_plan13_2026-10_in-network-rates_07_of_14.json | 16.9 | 0.4 | 0.57 | 14.3 | 15.2 | 5.1 | 0.01 | 0.10 | 0.03 | 0.02 | 748772 |
| w2_plan13_2026-10_in-network-rates_14_of_14.json | 60.2 | 0.5 | 0.99 | 16.2 | 17.7 | 18.2 | 0.01 | 0.04 | 0.03 | 0.01 | 2694926 |
| w2_plan7_2026-10_in-network-rates_01_of_14.json. | 5.1 | 0.7 | 0.23 | 19.4 | 20.4 | 1.7 | 0.01 | 0.04 | 0.02 | 0.02 | 229774 |
| w2_plan7_2026-10_in-network-rates_08_of_14.json. | 21.1 | 1.1 | 0.40 | 19.5 | 20.9 | 6.4 | 0.01 | 0.05 | 0.03 | 0.02 | 918914 |
| w2_plan8_2026-10_in-network-rates_02_of_14.json. | 5.4 | 1.2 | 0.19 | 20.8 | 22.1 | 1.8 | 0.01 | 0.06 | 0.02 | 0.02 | 238513 |
| w2_plan8_2026-10_in-network-rates_09_of_14.json. | 26.0 | 17.7 | 0.30 | 6.0 | 24.0 | 7.9 | 0.01 | 0.06 | 0.03 | 0.01 | 1152380 |
| w2_plan9_2026-10_in-network-rates_03_of_14.json. | 6.6 | 17.7 | 0.19 | 9.5 | 27.3 | 2.0 | 0.01 | 0.05 | 0.02 | 0.02 | 299232 |
| w2_plan9_2026-10_in-network-rates_10_of_14.json. | 31.7 | 17.7 | 0.36 | 11.4 | 29.4 | 9.5 | 0.01 | 0.03 | 0.03 | 0.02 | 1403623 |

sum download 7.5s, sum wait-in-fetched 127.2s, sum ingest 104.7s

- rollup update_rollups_incremental at t=80.6s took 41.1s -> 41.104724914000144

write_lock: held 3.01s of 80.6s wall (3.7%); 501 acquisitions; held while a rollup was active 0.07s
  hold by thread: mrfx-proc-1 0.59s, mrfx-proc-0 0.38s, mrfx-prefetch-0 0.38s, mrfx-prefetch-3 0.37s, mrfx-prefetch-1 0.32s, mrfx-proc-2 0.25s, mrfx-prefetch-2 0.24s, mrfx-prefetch-4 0.23s, mrfx-prefetch-5 0.18s, MainThread 0.09s
  waits >2ms: 306 totalling 5.10s; by thread: mrfx-prefetch-3 0.83s, mrfx-prefetch-1 0.81s, mrfx-prefetch-0 0.76s, mrfx-prefetch-5 0.70s, mrfx-prefetch-2 0.66s, mrfx-prefetch-4 0.63s, mrfx-proc-1 0.32s, mrfx-proc-0 0.19s, mrfx-proc-2 0.18s
  longest holds: mrfx-proc-1@31.4s 173ms, mrfx-prefetch-1@17.8s 56ms, mrfx-prefetch-3@0.8s 40ms, mrfx-proc-1@0.7s 35ms, mrfx-prefetch-5@1.2s 30ms, mrfx-prefetch-0@0.9s 29ms
  longest waits: mrfx-prefetch-5@17.8s 79ms, mrfx-prefetch-3@17.7s 65ms, mrfx-prefetch-2@29.4s 49ms, mrfx-prefetch-3@0.6s 48ms, mrfx-prefetch-3@29.4s 47ms, mrfx-prefetch-4@0.1s 46ms

parsers starved (no fetched file, a parser slot idle, downloads/queue still pending): ~0.5s
downloaders idle with queue pending (prefetch cap reached): ~16.0s
mean CPU: workers 1.43 cores, main 1.96 cores (box has 4)
main process io: read_bytes 3 MB, write_bytes 516 MB, rchar 4675 MB, wchar 514 MB
peak RSS (VmHWM): main 3136 MB; workers 548, 704, 655 MB
worker io (last snapshot): pid 12279: rchar 159 MB wchar 31 MB read_bytes 1 MB write_bytes 31 MB; pid 12283: rchar 183 MB wchar 36 MB read_bytes 0 MB write_bytes 36 MB; pid 12298: rchar 198 MB wchar 39 MB read_bytes 0 MB write_bytes 40 MB
max .wal size seen: 0.5 MB (sampled every 0.5s)

queue end states: {'done': 41}