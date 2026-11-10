# Review 9 reproductions

One script per finding of `docs/NEXT_STEPS_PROMPT_9.md`; each item there names
its script as `<folder>/<script>`. They are evidence, not tests: run one, see
the wrong result, fix the code, see the right result, then write the
regression test in `tests/mrfx/`.

Run from the script's own folder (they import their neighbours `h.py`,
`dl.py`, `fx.py`):

```
cd docs/review9_repros/ingest
PYTHONPATH=/home/user/Claudevideo /home/user/Claudevideo/.venv/bin/python n3_vintage.py
```

- Everything they create goes under `/tmp/mrfx_review9/` (stores, inboxes,
  logs) — never inside the repository, never the user's `data/`. Delete that
  folder when done; some scripts leave a few hundred MB.
- Local HTTP servers only (127.0.0.1, ports 8600–8699). No internet.
- `startup/` — start-up, the stored-row update, the Files page
  (`r9_steps.py <store_dir> pin|nopin <rules|keep>` times each start-up step
  and counts its disk reads; `mig_scale.py 2000` builds a 2,000-part store;
  `seed_old.py` / `age_store.py` belong to the update-path check and expect
  an extracted older build as their first argument).
- `ingest/` — downloads (`d*.py`) and ingest (`n*.py`). `n6_mem.py old …`
  needs commit 556cbf4 extracted to `/tmp/mrfx_review9/ingest/old556`
  (`git archive 556cbf4 | tar -x -C /tmp/mrfx_review9/ingest/old556`).
- `outputs/` — numbers (`t/s*.py`, and `t/t1…t14` from round 8), the
  endpoint timer (`timing.py`, `cmp_timing.py`: edit the store config path
  and port at the top), and the Chromium drivers (`ui_*.py`: they expect a
  server already running on the port named at the top of each; Chromium is
  at `/opt/pw-browsers/chromium` — never run `playwright install`).
- `outputs/snapshot.py <store-parent-dir> <out-dir>` captures the body of
  every dashboard endpoint; `outputs/diff_snap.py <a> <b>` compares two
  captures byte for byte (the "numbers did not move" proof of rounds 4–8).
- A few scripts carry the absolute path `/home/user/Claudevideo`; adjust if
  the repository lives elsewhere.
