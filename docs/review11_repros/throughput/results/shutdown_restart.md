# shutdown / restart cost (Linux, scratch store, paced 3 MB/s local server, 27-file queue)

## case A: SIGINT
- ready after 3.0s; busy (downloading>=1 and ingesting>=1) after 0.0s; queue at kill: {'done': 4, 'ingesting': 3, 'downloading': 2, 'queued': 1, 'fetched': 5}
- signal -> exit: **0.36s**, exit code 0; tracebacks in output: 2; spawn workers still alive 1s later: 0
- leftovers after exit: {'parquet.tmp': 0, 'progress': 0, 'spool': 0, 'parts': 4, 'downloads': 20, 'parts_': 2}
  ```
    File "/home/user/Claudevideo/mrfx/ingest.py", line 754, in dispatch_and_wait
      fut = pool.submit(
          _parse_worker, cfg, str(path), name, header_defaults, external_refs,
          pf.est_uncompressed_bytes or 0, pf.compressed_bytes, str(tmp_out), progress_path,
      )
    File "/home/user/Claudevideo/mrfx/ingest.py", line 447, in submit
      return self._pool.submit(fn, *args)
             ~~~~~~~~~~~~~~~~~^^^^^^^^^^^
    File "/usr/local/lib/python3.13/concurrent/futures/process.py", line 825, in submit
      raise RuntimeError('cannot schedule new futures after '
                         'interpreter shutdown')
  RuntimeError: cannot schedule new futures after interpreter shutdown
  ```

## case B: SIGTERM
- ready after 1.0s; busy (downloading>=1 and ingesting>=1) after 1.5s; queue at kill: {'fetched': 6, 'ingesting': 3, 'downloading': 3, 'done': 3}
- signal -> exit: **0.33s**, exit code -15; tracebacks in output: 0; spawn workers still alive 1s later: 3
- leftovers after exit: {'parquet.tmp': 3, 'progress': 3, 'spool': 0, 'parts': 3, 'downloads': 24, 'parts_': 3}
  ```
  19:35:34 INFO    mrfx.fetch: download lanes: 3 slot(s) for files up to 100 MB, 3 for bigger files
  19:35:34 INFO    mrfx.fetch: parallel downloads: 6 fetcher thread(s)
  19:35:34 INFO    mrfx.fetch: parallel ingest: 3 parser worker process(es)
  
    MRF Explorer  →  http://localhost:8912
    store: /tmp/mrfx_review11/throughput/shutdown/B/store  (0 file(s) ingested)
    inbox: /tmp/mrfx_review11/throughput/shutdown/B/inbox  (drop .json / .json.gz / .zip here)
    or paste MRF/TOC URLs on the Files tab — downloads run automatically
    if the app ever freezes: open /tmp/mrfx_review11/throughput/shutdown/B/store/diagnostics/stacks_latest.txt
    in Notepad and send its contents (auto-refreshed every 30s)
  
  19:35:34 INFO    mrfx.cli: the dashboard is ready — open http://localhost:8912
  ```

## case C: SIGKILL
- ready after 0.8s; busy (downloading>=1 and ingesting>=1) after 1.6s; queue at kill: {'done': 3, 'fetched': 6, 'ingesting': 3, 'downloading': 3}
- signal -> exit: **0.03s**, exit code -9; tracebacks in output: 0; spawn workers still alive 1s later: 6
- leftovers after exit: {'parquet.tmp': 3, 'progress': 3, 'spool': 0, 'parts': 3, 'downloads': 24, 'parts_': 3}
  ```
  19:35:46 INFO    mrfx.fetch: download lanes: 3 slot(s) for files up to 100 MB, 3 for bigger files
  19:35:46 INFO    mrfx.fetch: parallel downloads: 6 fetcher thread(s)
  19:35:46 INFO    mrfx.fetch: parallel ingest: 3 parser worker process(es)
  
    MRF Explorer  →  http://localhost:8912
    store: /tmp/mrfx_review11/throughput/shutdown/C/store  (0 file(s) ingested)
    inbox: /tmp/mrfx_review11/throughput/shutdown/C/inbox  (drop .json / .json.gz / .zip here)
    or paste MRF/TOC URLs on the Files tab — downloads run automatically
    if the app ever freezes: open /tmp/mrfx_review11/throughput/shutdown/C/store/diagnostics/stacks_latest.txt
    in Notepad and send its contents (auto-refreshed every 30s)
  
  19:35:47 INFO    mrfx.cli: the dashboard is ready — open http://localhost:8912
  ```
### restart after SIGKILL
- dashboard ready after 2.4s; queue drained 50.6s after start; last rollup finished 65.6s after start; final state {'done': 15}; leftovers now {'parquet.tmp': 0, 'progress': 0, 'spool': 0, 'parts': 15, 'downloads': 0, 'parts_': 0}; SIGINT exit 0.41s
  ```
  19:35:58 INFO    mrfx.store: analytics memory budget: 6 GB (5.6 GiB, as DuckDB reports it) — chosen automatically from this machine's RAM; raise duckdb_memory_gb in config/mrfx.yaml if rebuilds run ou
  19:35:58 INFO    mrfx.cli: recovered 3 file(s) left mid-parse by a previous run
  19:35:58 INFO    mrfx.cli: analytics are behind the ingested files — catching up in the background. Reason: 3 newly ingested file(s) not yet rolled up (incremental)
  19:35:58 INFO    mrfx.cli: incremental analytics catch-up unavailable (rollup tables were built by an older version (or never built)) — running a full rebuild
  19:35:58 INFO    mrfx.fetch: resumed 12 URL(s) left mid-flight by a previous run
  19:35:58 INFO    mrfx.store: full analytics rebuild: the slow whole-store pass (one-time after an upgrade, or when a shortcut wasn't safe). Extraction continues but shares the disk; once this complete
  19:35:58 INFO    mrfx.store: rollup: preparing (counting source rows)…
  19:35:59 INFO    mrfx.cli: the dashboard is ready — open http://localhost:8912
  19:36:47 INFO    mrfx.fetch: updating analytics rollups (12 newly ingested file(s))...
  19:36:48 INFO    mrfx.store: analytics update: 12 new file(s), 0 queued slice(s) — recomputing 8 payer slice(s) instead of the whole store
  19:36:48 INFO    mrfx.store: analytics update rates_by_tin_tbl: partition 1/3
  19:36:51 INFO    mrfx.store: analytics update rates_by_tin_tbl: partition 2/3
  19:36:55 INFO    mrfx.store: analytics update rates_by_tin_tbl: partition 3/3
  19:36:58 INFO    mrfx.store: analytics update tin_directory_tbl: partition 1/3
  19:37:00 INFO    mrfx.store: analytics update tin_directory_tbl: partition 2/3
  19:37:01 INFO    mrfx.store: analytics update tin_directory_tbl: partition 3/3
  19:37:02 INFO    mrfx.store: analytics update finished in 15s (payer slice, not a full rebuild)
  ```

## case D: serve --supervise, SIGINT to the process group
- ready after 1.0s; busy (downloading>=1 and ingesting>=1) after 1.6s; queue at kill: {'done': 6, 'fetched': 3, 'ingesting': 3, 'downloading': 3}
- signal -> exit: **0.51s**, exit code 0; tracebacks in output: 0; spawn workers still alive 1s later: 7
- leftovers after exit: {'parquet.tmp': 2, 'progress': 0, 'spool': 0, 'parts': 6, 'downloads': 18, 'parts_': 3}
  ```
  19:37:05 INFO    mrfx.fetch: download lanes: 3 slot(s) for files up to 100 MB, 3 for bigger files
  19:37:05 INFO    mrfx.fetch: parallel downloads: 6 fetcher thread(s)
  
    MRF Explorer  →  http://localhost:8912
    store: /tmp/mrfx_review11/throughput/shutdown/D/store  (0 file(s) ingested)
    inbox: /tmp/mrfx_review11/throughput/shutdown/D/inbox  (drop .json / .json.gz / .zip here)
    or paste MRF/TOC URLs on the Files tab — downloads run automatically
    if the app ever freezes: open /tmp/mrfx_review11/throughput/shutdown/D/store/diagnostics/stacks_latest.txt
    in Notepad and send its contents (auto-refreshed every 30s)
  
  19:37:05 INFO    mrfx.fetch: parallel ingest: 3 parser worker process(es)
  19:37:05 INFO    mrfx.cli: the dashboard is ready — open http://localhost:8912
  ```
