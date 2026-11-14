# Seam checks (the author, between the four passes). Scripts run standalone: `.venv/bin/python docs/review11_repros/seams/<script>.py`; scratch under /tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/mine/.

# My own seam checks (round 11), baseline 1064203

## M-1 (P1 when `delete_raw_after_ingest: false`) a republished file under the same URL is never fetched again
- `fetch.py` `fetch_url_record` reuse path ("NEVER re-download here" when dest + .fetchmeta exist).
- `seams/stale_reuse.py`: first load May (50 rows, file_month 2026-05); server replaced with June
  bytes; `store.requeue_urls([url])` → **0 HTTP requests**, row `done`, still 50 rows / 2026-05.
- With the default (delete_raw True) `_cleanup_raw` removes dest+sidecar after a done ingest, so
  the refresh re-downloads. Users who keep raw files, and any twin/duplicate row that kept its
  bytes, hit the stale path. Fix: on reuse, validate against the server with one conditional
  HEAD (ETag / Last-Modified / Content-Length vs the sidecar; `_write_meta` must record them);
  on mismatch download fresh. Never reuse without a validator when the row was requeued by the
  refresh (`requeue_urls`) or a user retry of a `done` row.

## M-2 (P2) the wall-clock cap (`download_max_seconds`) re-claims the slot it just freed
- `fetch_url_record` set-aside branch requeues with `status='queued'` and no `retries` bump;
  `_claim_next` orders by `coalesce(retries,0), id`, so the lowest-id file is claimed again at once.
- `seams/setaside_order.py` (1 slot, cap 3 s, two 1.5 MB drips at 300 KB/s): claims
  `(0.1, a) (4.2, a) (6.2, b) (10.5, b)` — b waited for a to finish; the cap only added a
  reconnect (`Range: bytes=917504-`). Message says "so other files get a turn": false in this
  config, and with N slots it is still the same slot churning. Fix: bump `retries` (the fair
  tier) or set `retry_after = now + a few seconds` on set-aside.

## M-4 (P1) a static index re-expanded by the monthly refresh never refreshes a child whose URL is stable
- `store._enqueue_one`: an existing child row in `done` returns None ("already queued / in
  flight / done") whatever its parent's re-expansion found; `requeue_urls` only touches the
  top-level index. `seams/refresh_static_children.py` (DEFAULT config): first load May/50 rows;
  child + index republished in place; `requeue_urls([index])` → requests `[GET /index.json]`
  only; child still `done`, file_month 2026-05, 50 rows. The June vintage never loads.
- Which payers publish stable child URLs is unknown (the real files on this box are all
  month-dated); the refresh is opt-in (`monthly_refresh`). Fix together with M-1: record the
  server's ETag / Last-Modified / Content-Length per loaded URL (in the sidecar AND in
  `url_queue` / identity), and when a parent is re-expanded, HEAD each `done` child once;
  requeue those whose validator changed. One request per child per month, no re-download
  when nothing changed.

## M-3 (P3) a kill between `part.replace(dest)` and `_write_meta` re-downloads a complete file
- `seams/dest_no_sidecar.py`: complete dest, no sidecar → one full GET, 22,632 of 22,632 bytes.
- Window is microseconds; cheap fix: if dest exists without a sidecar and its size equals the
  server's Content-Length (HEAD), hash it locally instead of re-downloading.

## Checked, correct
- `Content-Encoding: deflate` body with encoded Content-Length (`seams/content_encoding.py`):
  decoded correctly, `done > total` passes, bytes identical. (Only `done < total` is checked.)
- `parallel_downloads: 1` is honoured (one thread, sequential claims).
- The full rollup rebuild holds `write_lock` only for `_register_views` and the swap; the
  heavy build runs unlocked (`_rebuild_rollups_locked`) — the `url_progress` docstring saying
  "a multi-minute rollup rebuild holds write_lock the whole time" is stale.
- `_reserve_disk_or_raise` / mid-stream disk messages both contain "free disk space", so
  `fetch_url_record`'s disk-full branch (queued + 60 s pause) matches them.
- `cmd_add` hands links to a running dashboard over HTTP; 20 other CLI commands refuse to open
  the store when the port answers (`_something_owns_the_port`).
