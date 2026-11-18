# Audit 8 — status and what remains (version 0.7.1)

Audit 8 reviewed downloads, ingest, every output and the dashboard/API/docs
after round 7 and the early-duplicate / size-lane work. Every finding below
was reproduced by a script before it was fixed, and each fix has a test
(`tests/mrfx/test_early_dedup.py`, `test_audit8_ingest.py`,
`test_audit8_outputs.py`, `test_audit8_ui.py`). The rules a change must keep
are in `docs/AI_HANDOFF.md` → "Audit 8 additions".

## Done

### Downloads (commit 1e922fc)
- F1 a copy waiting on an in-flight twin never reconnects (was 25 requests /
  35 MB for a 3.2 MB file); waiters whose twin turns out different are freed.
- F3 an early skip needs size + first N MB + last 1 MB + 8 windows AND a
  gz/zip container (whose trailer checksums the whole content); a plain .json
  is never skipped early (two plain files differing only in the middle were).
- Range checks use the download's own browser identity / TLS override; the
  loaded fingerprint is written only by a successful ingest; `duplicate_of`
  is recorded and rewritten by forget; lanes keep the slots busy (slot 0
  small-only, other small slots fall back to big files).

### Ingest (commit 37cd705)
- P0-1 provider references are file-local on all four parse paths and the
  pool worker; ambiguous same-month companions are not guessed between.
- P0-2 a later run of a file supersedes its earlier parked / doomed part.
- P1-1 row rules 3: stored "fee_schedule" rows migrate to dollars; files
  trimmed by the old arrangement rule are flagged "re-ingest suggested".
- P1-2 a > 1 MB standalone reference file is stored as references (batched).
- P3-1 one rule for malformed values, with a plain-language failure.

### Outputs and dashboard (this commit)
- P0 trajectory on a matched panel (a payer adding a code read as -20 %).
- P1-2 one subject rule for Benchmark / Negotiate / rate card.
- P1-3 worst payer and leaderboard on the lead finder's code set.
- P1-4 per-visit value on a common basket. P1-5 size premium mid-rank.
- P1-6 outreach merge fields on the house basis + mid-rank + definitions.
- P1-7 sparkline ends at the as-of month; labelled a contract trend.
- P1-8 inflation source per year.
- P2-9 `half_up` = DuckDB's round(); no Python round() on printed numbers.
- P2-10 digest cache follows entity-map edits. P2-11 hospital per-hospital
  medians. P2-12 market report states as-of / basis / sources.
- P1-B same-name inbox drop / upload never silently ignored.
- P1-C Files poll paged, no preflight blobs, no stacked polls.
- P1-D downloads settings shown on the Files tab (read-only).
- P2-E waiting copies counted and badged on their own; stall timer reset.
- P2-F refusals read `detail` or `error`. P2-G export names sanitised,
  temp files removed on every failure, swept while the server runs.
- P2-H docs: HOW_TO_RUN parked/waiting/lanes text, starter list generated
  from the catalog (109 links, pinned by a test), README browser note.
- P3: URL / page input validation, localhost hand-off ignores proxies,
  "…" for a not-yet-read kind, market.py labels, AI_HANDOFF percentile text.

## Not done — and why

1. **A settings screen.** The Files tab now SHOWS the download settings and
   where to change them; editing `config/mrfx.yaml` from the dashboard (while
   keeping the user's comments, and with an update never overwriting it) is a
   feature of its own. Suggested: `/api/settings` limited to a whitelist of
   keys, written through a comment-preserving YAML round-trip, with a test
   that an update leaves the file untouched.
2. **Sparkline at the printed grain.** The line stays a fixed panel of
   (payer, TIN) contracts and is labelled as such: re-pooling per practice
   would let a payer joining move the line again (round 7 P0.3). If a client
   needs "your rate" over time, add a second, separately labelled series.
3. **Memory of deferred groups.** A file with no reference table of its own
   now defers its target-code groups to the end of the file (it cannot know
   earlier that no table follows). This is the same memory class as a file
   whose table comes after in_network, bounded by the target-code subset; a
   spill-to-disk for the deferred list (like run_big_file_fast's spool) would
   remove even that.
4. **Classic reader on malformed non-target values.** The fast reader (the
   default) skips them; the classic one (no numpy, or a declined file) must
   tokenize everything and fails such a file — now in plain words. A
   tolerant tokenizer is the only way to close it; not worth it so far.
5. **Re-time outreach.zip on the 99M store.** It now reads the materialised
   medians on the house basis instead of pooling every month; the audit's
   131 s / 51 s was measured on a loaded machine. Measure on a quiet one.
6. **Same-name detection for files loaded before audit 8** compares size
   only (no fingerprint was recorded then). A same-size different file under
   an old name is treated as the same file — rare; `--force` re-ingests it.
