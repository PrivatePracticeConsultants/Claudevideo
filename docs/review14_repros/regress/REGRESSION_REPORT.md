# Regression audit of MRF Explorer 0.7.7: did every fix from rounds 11–13 hold?

Repository `/home/user/Claudevideo`, branch `claude/gracious-mccarthy-ao7vmo`, head `9617c59` (`mrfx 0.7.7`).
Read-only: nothing under the repository was edited (`git status --short` is empty before and after).
All scratch work is in `audit14/regress/` (the `scratchpad/` prefix is left out below):

- `repros/`: copies of `docs/review1{1,2,3}_repros`. Their `/tmp/mrfx_review11/...` paths now point to `audit14/regress/r11tmp/`, and the perf checkers point to my own real store. The repository's copies are untouched.
- `logs/`: every run's output on 0.7.7. `logs076/`: the comparison runs on 0.7.6.
- `v076/`: `git archive c8b80fc mrfx config`, which is 0.7.6. Some scripts were run on it for paired comparisons.
- `det/`: the determinism run.
- `cfgsim/` and `upd/`: the config-continuity and updater simulations.

Two more sets of records were used for comparison. Round 11's raw "after the fix" logs are in `scratchpad/r11exec/results/after_*.log`. Rounds 12 and 13 left their own after-fix runs in `scratchpad/r12exec/results/after/*.log` and `scratchpad/r13exec/repros13_out/*.out`. My 0.7.7 outputs were diffed against these as well as against the status blocks.

The box was shared with the lead's full-suite run and with my own parallel batches, so timings are upper bounds.

## Verdict in one paragraph

**Held 58, explained 5, regressed 1.** These are the scripts with a recorded "after the fix" output, or a recorded hold. Another 19 scripts had no recorded after-output: they were run and are consistent with their round's FINDINGS. Where their output changed against the round's "before" output, the change is a later round's deliberate fix (counted in the explained). About 25 helper, live-network, 99M-store and throughput scripts were not run, each with its reason in the tables.

- Every after-fix output recorded in the status blocks of rounds 11, 12 and 13 reproduces on 0.7.7. That includes the three kill harnesses: s5_crash 20/20 with nothing stuck or mismatched, c10_kill 12/12 OK, and s4_kill 25/25 correct with 0/25 workers alive after 45 s.
- The accuracy bar is met exactly: TX 734 checks and WY 202, **0 differences**. The visit-value "All payers" row reads **p26 · Q2**, and the entity grain is 2/2.
- Determinism: 200/200 output calls are byte-identical twice, and identical with and without a progress token, once the documented `Generated …` stamps are stripped.
- A 0.7.6 config with every key set to a non-default value is carried by the updater byte for byte and read by 0.7.7 to the same values.

**One regression (P1):** a link whose ingest fails leaves its round-13 "same content" claim (`Store._content_claims`) held for the rest of the process. Two things follow:

1. A byte-identical twin link that arrives after the failure is parked as `skipped/duplicate` "will retry automatically" and never retries until a restart. The content's rows are missing.
2. A copy of the same file dropped in the inbox **waits forever** (scan_inbox did not return in 30 s; by code it loops for as long as the claim is held).

Round 11/13's pinned behaviour "a failed first loader hands the load to the twin" (`skip/s4_claims.py` C, SKIP_REVIEW §2) is broken. Round 13's own after-fix run already printed it (`r13exec/repros13_out/s4_claims.out`: `rates: 0`), but its status block does not list s4_claims, so it shipped unnoticed.

There are also several stale doc statements (P2/P3).

## 1. Inventory: script, round, recorded output, 0.7.7 output, verdict

Verdicts:
- **HELD**: same as recorded.
- **EXPLAINED**: different, and the difference is a later round's deliberate change (cited).
- **REGRESSED**: a fixed behaviour is broken again.
- **n/r**: no "after the fix" output was recorded; run and checked for consistency with the round's FINDINGS.

### Round 11 — download (`docs/review11_repros/download/`)

| script | recorded after the fix (status block 11) | 0.7.7 output | verdict |
|---|---|---|---|
| c01_truncate_resume | cr_start_wrong 911,726 B sha ok; cr_absent_short sha ok | cr_start_wrong: "the server sent 1,173,870 bytes for a 911,726-byte file — starting again", dest 911,726 B sha_match=True; cr_absent_short dest 911,726 B sha_match=True (and the other 4 variants OK) | HELD |
| c02_range_odd | every variant sha == v2; gz queue variant done 300 | all 5 download variants sha==v2 True; `changed_no_validator_same_size_queue` done in_network rows=300 | HELD |
| c02b_json_splice | rows 55.0 ×20 = v2, sha == v2 | `[('97110', 55.0, 10), ('97140', 55.0, 10)]`, content_sha==v2 True | HELD |
| c04_corrupt_gzip | no sidecar left | downloads dir `[]` in all 5 variants (no `.fetchmeta`); 0 rates for every bad file | HELD (the raw "CRC check failed 0x…" row text is unchanged from 0.7.6, run there too, so it is not a regression; see P3-1) |
| c05_http_errors | redirect loop in words, 429 Retry-After honoured | redirect_loop "this link redirects in a loop — …"; r429 done 760,264 B; zero_byte "the file is empty (0 bytes)…" | HELD |
| c05b_bad_encoding | plain sentence | "the server sent a compressed body that cannot be read — press retry later; …", 1 request | HELD |
| c07_segments | seg_fails / start_only / holey_restart_200 sha ok; start_only no doubling; holey_restart_200 retries=1 | seg_fails 367.0 MB sha ok; start_only 73.4 MB sha ok; holey_restart_200 4 requests sha ok; reserve_race 200/200. Identical to `r11exec/results/after_c07.log` | HELD |
| c08b_wrong_sidecar_json | true sha, 300 rows (was 000…, 600) | A sha==truth True, 0 GETs; B skipped/duplicate; rates 300 | HELD |
| c10_kill (+c10_driver) | seed 39616 ×12: 12/12 OK, resume from .part, 0.13 MB re-downloaded, sha == truth, 600 rates | `[kill] seed=39616 trials=12` → **12/12 trials OK** (8 × 0.13 MB re-downloaded, 4 × 0.00 MB), rates 600, sha_ok | HELD |
| c03_chunked | n/r | plain OK; chunked twin early=True (FINDINGS P2-5 fixed); clean_short accepted (FINDINGS "not a finding") | n/r — consistent |
| c06_slow_drip | n/r | clocks fire as in FINDINGS; cancel_during_backoff 1.0 s (P2-1 fixed) | n/r — consistent |
| c08_cancel_sidecar | n/r | cancel in back-off released after 1.0 s; wrong_sidecar sha==truth True, 300 rows | n/r — consistent |
| c09_early_dedup | (3.8: fixed by the wording) | constructed CRC collision still skipped, now "checksum-equal to …" (round-11 decision) | HELD |
| c11_efficiency | n/r (efficiency measurement) | not run (throughput class) | — |

### Round 11 — queue (`docs/review11_repros/queue/`)

| script | recorded | 0.7.7 | verdict |
|---|---|---|---|
| s3_toc | retry on the capped index True → files missing [] | "retry allowed on the DONE index row … True"; B "files still missing: []"; idx child_count 6 | HELD |
| s11b_oversize_raise | sized: re-queued → done; chunked keeps 0 ("download anyway" per row) | sized: oversize → after raise+restart done rows=3 (re-queued True); chunked: stays oversize, bytes_total 0, "download anyway" works | HELD |
| s11_oversize_reexpand | 1 GET over 3 re-expansions | sized and chunked: "GETs on big so far: 1" after 3 re-expansions and a restart | HELD |
| s9_actions | B rows 1–5 listed; C cleared index stays skipped (rates {}) | B rows 1–3 failed + 4 oversize + 5 parked present; C `[('toc.json','skipped')] rates: {}` | HELD |
| s6_counters | A user retry → paused again; B every requeue path resets | A "user retry → status=paused"; B retry/force_size/requeue_failed/monthly/re-paste/re-expand/oversize all reset park/auto/force/waiting/retry_after | HELD |
| s2_dedup | token/st/policy/x-amz-meta distinct, signed shapes dedup | token=file-id, st=, policy=, x-amz-* → two rows; S3/Azure/CloudFront signed twice → one | HELD |
| s7_lanes | small files claimed at 12–17 s (were 24–31 s) | run 1 (loaded box): sized 14.8–18.0 s, chunked 19.7–29.0 s; paired re-run below | HELD (see §1a) |
| s5_crash (+s5_driver) | seed 4242 ×20: no row in flight, every file its rows, files == parts 9/9 in 19 (trial 2 before add_urls), downloads left none 20/20; "PROBLEM" = `.verified_parts.json` | seed 4242 ×20: **stuck=[] and rows_mismatch={} in 20/20, downloads_left=[] in 20/20**; files/parts 9/9 in 18. Trial 2 (kill 0.6 s, before add_urls): 0/0. Trial 8 (kill 1.8 s): killed *inside* add_urls after the TOC row was inserted (queue holds only toc.json + its 6 children) → 6/6. Every "PROBLEM" tag is the `.verified_parts.json` artifact | HELD (trial 8 is the same harness artifact as trial 2, a kill during the paste; see P3-3) |
| s4k_twin_kill | A done, B skipped/duplicate, 6 rows once, downloads cleaned | [A] and [B]: row 1 done, row 2 skipped/duplicate, rates 6 from one file, downloads [] | HELD |
| s0_smoke, s1_transitions, s8_processor, s10_monthly, s10c_dead_payer, s12_waiting_drain | n/r | ran, rc 0; s8 C2 "5 files one at a time → 4 rebuilds" (round-11 4.3, still open by decision); s12 0 of 6 left paused | n/r — consistent |

### Round 11 — ingest (`docs/review11_repros/ingest/`)

| script | recorded | 0.7.7 | verdict |
|---|---|---|---|
| s2_samename | A/A2/G 0 lines held twice; C Keep both after a restart loads | A/A2 "contract lines held twice: []"; C keep → restart → done 5 rows; G rollups fine | HELD |
| s2b_keep_stuck | scan → done 5 rows | "scan 0 → done rows 5"; 0/30 confirmed files not done | HELD |
| s3_claims | A/B2 0 duplicate lines; C/C2/D unchanged-correct | A dup 0; B2 dup 0; C recover 1/1 → 300 once; C2 300 once; D inflight rows [] | HELD |
| s1_containers | blob listing quarantined with the Links sentence; 0-byte / UTF-16 named | u_blob_listing "…paste its web address on the Links tab…"; i_zero_byte "the file is empty (0 bytes)…"; l_utf16 "saved as UTF-16…"; truncated/trailing-garbage gz "damaged or incomplete" | HELD. Differences from `r11exec after_s1_containers.log` are EXPLAINED: b/c/e/g/h/j/o/s/t now "the same content as a_plain.json" (round 13 5.2.1, compressed-differently twins); f/z "gone" (round 12: a second copy is deleted from the inbox) |
| s10_honesty | fastcheck 3/3 identical | 0-row file stays 0 across 3 scans; fast == classic; **3 of 3 files identical** (ND HMHS 0 rows, UHC-NY 20,741, BCBS-MS 2,660,356; the same counts as round 11's own log) | HELD |
| s6_companion | B' row says "drop this file in again to pick up …" | B' "its payer's provider list has now arrived — drop this file in again to…" | HELD |
| s7_pool (shim copied from `/tmp/mrfx_review11/ingest/site/sitecustomize.py`) | exit → "ended by the system twice …"; hang → wedge sentence without "RuntimeError:"; retry loads 20 rows | exit: files row "the parser process for this file was ended by the system twice …"; hang: "parser worker wedged (no progress for 60 min) — worker killed; press retry…"; "A retried from failed/ → done rows=20"; healthy B 25 rows on the same pool | HELD |
| s9b_opens | 71.3 MB = 2.01×, 7 opens | `total 71.3 MB = 2.01x the file, 7 opens` | HELD |
| s4_kill (orphan `os.kill` replaced by a report, as rounds 12–13 did) | 25/25 correct, 0/25 workers alive 45 s after the kill | 25/25 correct, 0/25 alive at 45 s (seed 72365, §1b) | HELD |
| s5_reingest | the raw DuckDB loop still meets the transient error (by design) | reader saw only {400, 500}, never 0 or 2×; 1 transient "Parquet file is likely corrupted" in the raw loop; forget/re-drop/pooled refusals correct | HELD |
| s8_oversize | n/r | pending → restart → confirm → done 5; forget-decline; trailer lie quarantined | n/r — consistent |
| s4b_orphans, s6g_mem, s9_eff, s10b_fastcheck | n/r | not run (4-min orphan watch on a 118 MB file / memory ladder / strace; s10b duplicates s10's fastcheck) | — |

### Round 11 — seams (`docs/review11_repros/seams/`)

| script | recorded | 0.7.7 | verdict |
|---|---|---|---|
| stale_reuse | 2026-06 / 80 rows (was 0 requests, 2026-05) | requests [GET, GET]; after refresh done (80, 7), month 2026-06 | HELD |
| refresh_static_children | child 2026-06 | after refresh `('…in-network-rates.json.gz', '2026-06', 'done') (80,)` | HELD |
| dest_no_sidecar | 0 requests (was a full GET) | "fetched: True requests: [] bytes served: 0 of 22632" | HELD |
| setaside_order | a, b, a, b | claims `[(0.1,1,a),(4.3,2,b),(8.5,1,a),(10.7,2,b)]` | HELD |
| content_encoding | n/r | OK 2710 vs raw 2710, identical True | n/r — consistent |

Throughput (`throughput/*`): skipped as instructed (each run is over 5 minutes).

### Round 12 (`docs/review12_repros/`)

Every round-12 script below is also identical, line for line, to round 12's own after-fix log in `r12exec/results/after/`. The only differences are hashed filenames and line truncation.

| script | recorded (status block 12) | 0.7.7 | verdict |
|---|---|---|---|
| agent_2_5/npi_drawer_latest | NPI chart 42.0 (TIN drawer / Explorer / Code page 42.0) | npi drawer 42.0, tin drawer 42.0, explorer npi/tin `('97110','GP','2026-06',42.0)`, code page 42.0 | HELD |
| agent_diff/s1_unquoted_etag | OK, 5 requests, 4.1 s | "OK sha … in 4.1s, requests: 5" | HELD |
| agent_diff/s2_cli_ingest_wait | done in 0.5 s | "scan returned: True after 0.7s … done 30" | HELD |
| agent_diff/s4_refresh_old_url | A [index, sig=S2 206, sig=S2 200] 2026-06/80; B unchanged-correct; C one 1-byte probe, nothing downloaded | A exactly that, 2026-06/80; B 2026-06/80; C `[index 200, child?sig=S1 206]`, stays 2026-05/50 with the "unchanged by the same size only" note | HELD |
| agent_diff/s5_crash_inbox_claim | recover_stuck_urls 1; B queued "the inbox load of A.json.gz … did not finish" | `recover_stuck_urls: 1`; B queued with that sentence | HELD |
| agent_diff/s7_clear_during_recheck | stays skipped/canceled, children 2026-05 | index "skipped … cleared from the queue", 12 s later still skipped; months [('2026-05', 6)] | HELD |
| agent_diff/s8_stale_splices | 5 → 3 requests | 3 requests, OK | HELD |
| agent_6_8/mpfs_probe (from the repo root, scratch dir arg) | upload 422 with the line; summary/code/benchmark 200 | upload 422 "line 2 (code 97110): the rate 'nan' is not a dollar amount…"; summary 200, code 200, bench 200 | HELD |
| agent_diff/s3_probe_bytes, s6_probe_escape | unchanged (probed-and-held) | byte-identical result lines to the same scripts on 0.7.6 (`MRFX_SRC=v076`) | HELD |
| agent_2_5/check_rules, agent_6_8/checks2, checks3, roundscan | unchanged; 0 Python round() | outputs identical to 0.7.6's; roundscan prints nothing | HELD |
| agent_6_8/checks1 | stops at its own SQL | same `InvalidInputException … parameters: 2`, identical to 0.7.6 | HELD |
| agent_9_10/ae_check (on `build_store.py … 2000`) | one practice under AE, never shown | "AE listed: False" | HELD |
| agent_9_10/entity_ab, files_page | n/r (round 9/10 digests) | two-pass == one-pass totals and first rows; attention rows at index 0–3, offset overflow 422 | n/r — consistent |
| mine/stale_reuse_hdrs | n/r | republished bytes load: 2026-06 / 80 | n/r — consistent |
| mine/fresh_serve, fresh_serve2, ui_walk, live_link, update_path; agent_2_5/idle_cpu; agent_9_10/latency, startup_2000 | — | not run: they are bound to audit12's extracted 0.7.5 install, the live payer network, or Chromium. The update path was re-done for 0.7.6 → 0.7.7 in §5 | — |

### Round 13 (`docs/review13_repros/`)

Every round-13 script is also identical to round 13's own after-fix run (`r13exec/repros13_out/*.out`), apart from the ordering of hashed filenames and p_c's race order.

| script | recorded (status block 13) | 0.7.7 | verdict |
|---|---|---|---|
| skip/s1_dedupkey | 4 keys differing → 0 | "DIFFERS from the stated expectation: 0" | HELD |
| skip/s3_identity | A1–A4: 1 done + 1 "the same content as …" / 300; case C still loads twice (by design) | A1–A4 each 1 done + 1 skipped "the same content as …", rates 300; C 3,000 (two copies), held copy needs_choice | HELD |
| skip/s10_known | pasted index after the monthly tick: done → queued | `user-pasted non-catalog index after the tick: [{… 'status': 'queued' …}]` | HELD |
| skip/s7_revive | C re-paste of an unchanged done link costs 0 requests | "C: … requests since: 0"; A re-paste of skipped/duplicate: 0 bytes, "pasted again: still a copy of …" | HELD |
| diff/p_g_loaded_size_unknown | re-downloaded, 2026-06 | "file months now: [('2026-06',)] VERDICT: re-downloaded" | HELD |
| mine/stale_pickers | `analytics` in /api/stats and /api/boot; /api/payers {3 payers, covered 1} | `['analytics']`; payers 3, covered ['Moda'] | HELD |
| diff/p_c_clear_window | unchanged by design (the confirm text says it) | cleared [2] instead of [3]: which child a downloader had claimed at the clear instant is a race; the documented semantics hold | HELD (timing variant) |
| diff/vm1_handworked | its four quartile expectations encode the OLD rule (4.1) | 61 ok, exactly the 4 quartile FAILs (v=99.99/119.99/139.99, "p25 == median") | EXPLAINED (round 13 4.1) |
| **skip/s4_claims** | not in the status block; SKIP_REVIEW §2 "a failed first loader (link or inbox) hands the load to the twin (`s4.out`)" | **C: row 1 failed; row 2 skipped/duplicate "…being loaded right now — will retry automatically…"; rates 0 (one copy = 200); parse calls 1** (c8b80fc: twin done, 200). A, B1, B2, C2, D unchanged | **REGRESSED (P1-1)** |
| skip/s5_samename | n/r | D: corrected same-size old-mtime file now held as needs_choice (was silently "same") | EXPLAINED (round 13 Group 5, P1-4 fixed) |
| skip/s6_refresh | n/r | A/C "re-downloaded" 2026-06/80; D "no longer listed in its index (checked …)" | EXPLAINED (round 13 P0-2 / P2-6 fixed) |
| skip/s8_sniff | n/r | blobs listing now queues "Hallowed"/"Allowed-Benefits"/"NDC-Corp" names (2, 3, 6) | EXPLAINED (round 13 P2-5 fixed) |
| skip/s9_zero | n/r | rows as before + the "checked …: unchanged by the same version tag and the same size" note | EXPLAINED (round 13 wording item) |
| skip/s2_early, s3b_shacost, diff/p_a_val_compat, p_e_live_twin, p_f_removed_child | n/r | identical to c8b80fc's and to round 13's after outputs (apart from hashes, order and timings) | n/r — consistent |
| perf/check_real + check_entity | TX 734 / WY 202, 0 differences, All payers p26 · Q2; entity 2/2 | identical (§2) | HELD |
| perf/spot99*, driver, prof, proto, inspect_store; progress/*; diff/pooled_cost | — | not run: they need the 99M store under `scale4n` (owned by another reviewer), or they are DuckDB-only experiments | — |

### 1a. s7_lanes (timing, the box is loaded)

Recorded (round 11): small files claimed at 12–17 s of the drain (they were 24–31 s on 1064203).

| run | sized: small files claimed at | chunked |
|---|---|---|
| 0.7.7 run 1 (box with suite + my batches) | 14.8, 14.8, 17.9, 18.0, 18.0 s (drain 66.8 s) | 19.7–29.0 s (drain 65.4 s) |
| 0.7.6, back to back with run 2 (heavier load: s4_kill + suite) | 30.6, 30.7, 31.4, 38.6, 38.9 s (drain 72.5 s) | 24.6–32.3 s (drain 78.7 s) |
| 0.7.7 run 2 (right after the 0.7.6 run) | 36.4, 36.5, 36.6, 45.7, 45.8 s (drain 96.5 s, box at its busiest) | 37.1–46.2 s (drain 83.8 s) |

**Load-independent measures:**
- The lanes still work: `big_lane flags set: 47` in every 0.7.7 run.
- The small files are claimed in the first half of the drain. Run 1: 22–27 % of the drain. Run 2: 38–47 %. 0.7.6 under the same load: 42–54 %.
- By position in the request order, the small files are requests 62–71 (run 1) and 66–75 (run 2) of 107. On 0.7.6 they are 68–77 of 109. On 1064203, before the fix, they were 50–54 of 55, at 81 % of the drain (FINDINGS F4).

The absolute seconds track the box load, not the build. **HELD** (no worse than 0.7.6 under paired load).

### 1b. ingest s4_kill (20 random + 5 window kills, seed logged)

Recorded in rounds 11–13: **25/25 trials correct, 0/25 pool workers left alive 45 s after the kill** (round 13: seed 65474).

0.7.7 (seed 72365; clean run 21.1 s, BCBS-MS 2,660,356 + UHC-NY 20,741 rows): **`25/25 trials correct (rows/state/temps/rollups); 0/25 left pool workers alive 45 s after the kill`**.

- That covers 20 random kills (1.2–19.8 s) plus the 5 window kills: before_finalize, after_finalize, before_commit, after_commit, before_finish_ok.
- After the restart, each file is done exactly once with its clean count. There are no temp files, the repeated-line count is 416,826 as in the clean store, and the rollups are not stale.
- 2 workers were still alive 1 s after the kill in 15 trials, and all were gone by 5 s.
- The two 5.2.6 window trials (after_commit, before_finish_ok) pass. Both were 'needs_choice' before round 13's fix.

**HELD.**

## 2. Accuracy: fresh three-file REAL store

The store was built with 0.7.7 at `audit14/regress/realstore`, with absolute paths in its own config. It held the three files from `scratchpad/real` (WY 05_of_09, Moda TX, UHC HealthScope). Enrichment ran in bulk mode from `nppes_live/…/NPPES_Data_Dissemination_November_2026_V2.zip` with `nppes_dir` in scratch and `auto_download: false`. The command was `mrfx --config <abs> ingest`, which took 1 min 45 s.

- Rows per file: WY **699,174**, Moda **430,768**, UHC **252**. That gives 3,511 TINs and 12,929 NPIs named, the same numbers round 13 recorded.
- Outputs were fetched with `perf/real_fetch.py` through a real `mrfx serve` on port 8497 (Popen; it stops the server itself). TX ran with subject 760513962 / Moda, and WY with 208741495 / BCBS WY.
  - **58/58 TX and 58/58 WY bodies were byte-identical** on the second call.
  - 57 of the 58 calls returned 200. `changes_latest` returned 422, and so did WY's three visit routes, which were 422 in round 13 too (the status codes match round 13's `_timings.json` route by route).
- `perf/check_real.py` (S retargeted to my store):
  - TX: `734 OK, 0 DIFF, PROBLEMS: 0`. The visit-value All payers row reads `{'value': 105.44, 'percentile': 26, 'ranked_among': 1668, 'quartile': 'Q2', …}`, so **p26 · Q2**.
  - WY: `202 OK, 0 DIFF, PROBLEMS: 0`.
- `perf/check_entity.py`: "multi-TIN entity rows checked 2 mismatches 0".
- Compared with round 13's c8b80fc bodies (`audit13/perf/realout_tx|wy`), 45 TX and 48 WY bodies are byte-identical. All the others differ only by these, and no number moved:
  - the version and timestamp lines;
  - the `analytics` block in `/api/stats` (1.1);
  - the quartile Q3 → Q2 on the tied visit row (4.1);
  - the 4.2 rule sentence (pitch, negotiation, payer-compare);
  - the 6.2 "Not included, and why — Medicaid floor … Hospital parity …" lines (dossier, negotiation, payer-compare).

The store was deleted at the end.

## 3. Determinism

Script: `repros/review13_repros/perf/det.py`. Its routes are round 13's `perf/calls.py` (all 135 output features), adapted to the real store as `calls_real.py`: TX subject 760513962, payers Moda / BCBS WY / HealthScope, WY as the variant state.

- Each route and each variant was called three times on a fresh `mrfx serve`: A and B without a token, then C with `X-Mrfx-Progress: det<24 hex>`. That is **200 routes** (135 main + 65 variants). `packets_build` was excluded because it writes files.
- Statuses were identical across A/B/C for all 200: 185 × 200, 13 × 422, 2 × 404.
- **Raw bytes: A == B in 193/200 and A == C in 193/200.** The 7 that differ in both comparisons are:
  - the six export/outreach ZIPs, where only the methodology member's `Generated: 2026-11-12T08:25:32.625351+00:00` line differs (plus the zip entry time);
  - `report_market`, where the difference is its `Generated …` line.
- **After stripping only the documented `Generated …` stamps: 200/200 identical A vs B, and 200/200 identical with vs without a token.**

## 4. Docs that describe old behaviour

Searched GETTING_STARTED.md, README.md, HOW_TO_RUN.md, docs/AI_HANDOFF.md and CLAUDE.md. Release-note "What changed" sections are history and are not counted.

| file:line | statement | why it is false on 0.7.7 | sev |
|---|---|---|---|
| docs/AI_HANDOFF.md:318 | "Rebuilds give up after 3 failures (raw data is safe) and never run per-file during queue grinds." | Round 13 1.3 removed the give-up. `fetch.py:3717-3730` backs off `min(3600, 120·2^(n-1))` and retries for ever. AI_HANDOFF:815 itself says "it never gives up for the run". The file contradicts itself | P2 |
| docs/AI_HANDOFF.md:1159 | "the suite (670 tests) runs real end-to-end drains…" | `pytest --collect-only`: **1247** tests (470 quick) | P2 |
| docs/AI_HANDOFF.md:1152 | "(497 of 866 tests, 2 min 48 s on Nov 6 2026 …)" | dated, but read as the current quick tier; it is now 470 of 1247, ~4.3 min. CLAUDE.md lines 31–32 have the right 1247 / 470 | P3 |
| GETTING_STARTED.md:176-177 | "about 2 GB each after the database's 8 GB: 2 on a 16 GB machine, up to 7 on 32 GB" | The automatic pool is no longer 8 GB. `store.auto_pool_gb` gives 6 GB on 16 GB (2 GB on 8 GB, 12 GB on 32 GB), and `fetch._ram_worker_cap` gives 3 workers on 16 GB (4 on 16 GiB). The same guide's line 609-610 says "a 16 GB PC **3**", so it contradicts itself. Stale since 0.7.6 (item 2.6); round 13 4.3 fixed only the `config/mrfx.yaml` comment | P2 |
| GETTING_STARTED.md:~1285 ("Plain sentences instead of program errors … a damaged or cut-off download … now each say what happened") | true for the inbox | through a **link**, a corrupt gz still fails with `CRC check failed 0x36bafc05 != 0xc94503fa` / `Compressed file ended before the end-of-stream marker was reached` (c04, the same on 0.7.6) | P3 |
| mrfx/config.py:127 (code comment) | "a payer that republished nothing costs one HEAD per file" | it is one 1-byte ranged GET per loaded file (AI_HANDOFF rule 7) | P3 |
| mrfx/cli.py:128 and fetch.py:1986 (docstrings) | "OPT-IN (monthly_refresh) …"; "costs one download per file" | the loop now also runs for `monthly_recheck` (default ON), and an unchanged file costs a 1-byte probe | P3 |

Checked and **still true**:
- "one GET per file": no user doc says it.
- "re-add the link to pick up new codes": GETTING_STARTED:194-197, 239 and 708-709 now describe the Re-read button / re-paste, and :1152 is history.
- `monthly_refresh` "Off by default … your choice" (config/mrfx.yaml:279-282, matching `monthly_refresh: bool = False`).
- `monthly_recheck` "on by default" (config/mrfx.yaml:284-288, AI_HANDOFF:879, GETTING_STARTED:1147).
- The Explorer default is the newest file per contract (HOW_TO_RUN:255); AI_HANDOFF:1199 only mentions the "then-pooled default" as history.
- The pooled same-month rule (AI_HANDOFF:699-711).
- The CLAUDE.md test counts (1247 / 470 = `--collect-only`).
- "re-adding a file you already have costs nothing" (GETTING_STARTED:693, true since round 13 P2-3; s7_revive prints 0 bytes).

## 5. Upgrade continuity (0.7.6 → 0.7.7)

**`git diff c8b80fc 9617c59 -- config/mrfx.yaml`.** No key was renamed or removed, and no existing default changed. The diff only adds:
- the memory comment rewritten (round 13 4.3);
- `# monthly_recheck: true` (new; default ON);
- `# backup_dir:`, `# backup_every_days: 7`, `# backup_keep: 4` (new; backup is OFF until `backup_dir` is set).

`mrfx/config.py`: 48 fields in 0.7.6 and 52 in 0.7.7. The 4 new ones are those above. `backup_dir` was added to `_ANCHORED_FIELDS`, and an empty `backup_dir` reads as None. `MrfxConfig()` defaults are identical for all 48 shared keys. `mrfx/updater.py` is unchanged; it still has `PROTECTED = config/mrfx.yaml, entity_map.yaml, registry_overrides.yaml`.

**Simulation 1 (`cfgsim/mk.py`).** A 0.7.6 `config/mrfx.yaml` with **all 48 0.7.6 keys set to non-default values**. That includes the nested keys:
- `codes.cpt_codes`;
- `enrichment.mode/bulk_csv_path/auto_download/nppes_dir/api_concurrency`;
- `report_branding.name/logo_path`;
- `payer_name_map`, every path, every download clock;
- `monthly_refresh: true`, `duckdb_memory_gb: 9`, `parallel_ingests: 3`, `user_agent`.

Results:
- Loaded by 0.7.6's `load_mrfx_config`: 48 fields, no warning.
- Loaded by 0.7.7's: 52 fields, no warning. **0 keys read differently.**
- The 4 new keys take their defaults: monthly_recheck True, backup off, 7 days, keep 4.

**Simulation 2 (`upd/`): the real updater path.**
1. Build a 0.7.6 install: `git archive c8b80fc`, plus that config (paths rewritten into the install), plus a `data/` marker.
2. Run 0.7.6's own `mrfx.updater.apply_update(<0.7.7 release zip scratchpad/mrfx_project.zip>, root, run_pip=False)`. pip was skipped so the repository's `.venv` is not re-pointed.

Results:
- "updated 110 file(s) (version 0.7.6 -> 0.7.7)". **config/mrfx.yaml sha256 unchanged (`sha256sum -c` OK).**
- `config/mrfx.yaml.new` is byte-identical to the 0.7.7 shipped config.
- The `data/` marker is intact, and the tree reads `__version__ = "0.7.7"`.
- Loading the kept config with the updated tree's code: no warning, **0 of the 48 keys read differently**, and the 4 new keys at their defaults.

**Intended behaviour for the kept config.**
- `monthly_refresh: true` → `monthly_refresh_tick(dated_sources=True, recheck=True)`: both new dated months and the re-check, the same as 0.7.6 did.
- A 0.7.6 user who never set `monthly_refresh` now gets the monthly **re-check** (one 1-byte request per loaded file per month), because `monthly_recheck` defaults to ON. This is the deliberate round-13 5.2.5 decision and is announced in GETTING_STARTED's "What changed".
- A store where 0.7.6 already recorded `monthly_refresh_last = 2026-11` re-checks user-pasted indexes from December, not now. That is harmless.

**Verdict: continuity holds.**

## 6. Findings by severity

### P0
None. No wrong number, no doubled or dropped rows in a pinned path: accuracy is 0 differences, fastcheck 3/3 is identical, every kill harness ended with correct rows, and determinism is 200/200.

### P1-1 (REGRESSION of round 11 1.1 / round 13 SKIP_REVIEW §2) — a failed link ingest leaves its "same content" claim held: the twin never loads and an inbox copy hangs

**Where.** `Store.claim_content_ingest` (`store.py:3471-3478`) registers `self._content_claims[content_sha2] = filename` for a link (round 13 5.2.1). It is released only on success: `ingest._finish_ok` → `release_content_claim` (`ingest.py:417-422`, comment "a link's download releases its content claim here"). On a failed link ingest nothing releases it. A trace of every claim and release call (`skip/c_trace.py`) shows:

```
claim_content_ingest(sha, '1', …) -> None              claims now [('5542b6dc', 'eaaf49b5_tpa.json.gz')]
   (A's parse fails; release_content_claim is never called)
claim_content_ingest(sha, '2', …) -> ('eaaf49b5_tpa.json.gz', 'ingesting')
``` By code reading, the `stopped`, `requeued` and `ingest crashed` branches of `process_url_record` (`fetch.py:3431-3480`) have no release either. The inbox path does release in `ingest_file`'s `finally` (`ingest.py:1299`).

**Repro 1: `skip/s4_claims.py` case C.** Two links with the same bytes; the first parse raises once.
- 0.7.7: row 1 failed; row 2 `skipped/duplicate` "the same content as …, which is being loaded right now — will retry automatically from the kept download if that one fails"; **rates 0 (one copy = 200)**; parse calls 1; the kept download is left in `downloads/`.
- c8b80fc (`skip/s4.out`): row 2 done, 200 rates, parse calls 2.

**Sequence (`c_long.py`, 40 s of queue).** A failed at 1.5 s. B was fetched at 1.5 s and skipped at 1.8 s against the stale claim. `_revive_twins` had already run at A's failure, so nothing re-queues B.

**Restart (`c_restart.py`).** After 20 s more in the same process the claim is still held. After a restart (`recover_stuck_urls → 1`) B loads its 200 rows. So the rows are missing until the next start, and the row's "will retry automatically" promise is false meanwhile.

**Repro 2: `c_inbox_wait.py`.** One link fails at ingest, then the same bytes are dropped in the inbox in the same process, which is what `mrfx serve`'s watcher does. `scan_inbox` **did not return in 30 s** (`claim_inbox_content` returns `(claimant, 'ingesting')` and `_content_claim` loops `sleep(2)` until stop or shutdown). In `serve` this stalls the inbox watcher for every later file. It is the same class as round 12 item 1.3 (`s2_cli_ingest_wait`, "scan never returned").

**Why the suite misses it.** `test_fetch.py:175-205` / `:708-730` and `test_review11.py:218` call `revive_skipped_duplicates` directly while A is still `ingesting`. None of them has the twin claim after A's failure, and none drops an inbox copy after a failed link.

**Fix direction (not applied, read-only round).** Release `content_sha2` in `process_url_record` on every non-done outcome, or in `ingest_file`'s `finally` for link paths too. Have `_same_content_locked` ignore a claimant whose url row or file row is no longer in flight. Pin it with s4_claims C and the inbox-after-failed-link case.

### P2
- **P2-1:** AI_HANDOFF.md:318, "Rebuilds give up after 3 failures" (false since round 13 1.3; contradicts :815).
- **P2-2:** GETTING_STARTED.md:176-177 states the 0.7.5 memory rule ("the database's 8 GB: 2 on a 16 GB machine"). The code gives 6 GB and 3–4 workers, and line 609-610 of the same guide says 3.
- **P2-3:** AI_HANDOFF.md:1159 "the suite (670 tests)"; it is 1247.

### P3
- **P3-1:** A damaged gz downloaded by a **link** still reads as the raw `CRC check failed 0x… != 0x…` / `Compressed file ended before the end-of-stream marker was reached` (c04; identical on 0.7.6, so it is not a regression). GETTING_STARTED's "Plain sentences instead of program errors … a damaged or cut-off download" holds for the inbox only.
  - Also: `DOWNLOAD_IDENTITY` keeps an entry for a failed zip row (round-11 FINDINGS P3, "bytes, not a bug").
- **P3-2:** AI_HANDOFF.md:1152's dated quick-tier count (497 of 866). Code comments: config.py:127 "one HEAD per file"; cli.py:128 / fetch.py:1986 "OPT-IN … costs one download per file".
- **P3-3:** `add_urls` over a pasted list is not atomic. In s5_crash trial 8, a kill 1.8 s in, during `add_urls`, left only the first of 6 pasted links queued (the TOC; it expanded to its 6 children after the restart). The other 5 links were silently absent, with nothing stuck and no wrong rows. This is the same harness window as rounds 11–13's trial 2, which is killed before `add_urls`. In the dashboard the paste's POST would fail visibly.
- Cosmetic log: on the fresh real store, `enrich` logged "scoped name refresh unavailable (4,007 of 3,511 TINs affected …)". The affected count exceeds the TIN count; I did not chase it.

## 7. Commands (evidence)

- Copies: `cp -r docs/review1{1,2,3}_repros audit14/regress/repros/`, then `sed s#/tmp/mrfx_review11#…/r11tmp#`.
- Batches: `logs/{dl_,q_,ing_,seam_,r12_,r13_}*.log`. Each was run with `/home/user/Claudevideo/.venv/bin/python <script>` from its own copied folder. `mpfs_probe` was run from the repository root with a scratch argument.
- 0.7.6 comparisons: `PYTHONPATH=v076` or `MRFX_SRC=v076`, or harness copies in `q076/` and `d076/`. Outputs are in `logs076/`.
- Kill harnesses:
  - `download/c10_kill.py 12 39616` → `logs/kill_c10.log`;
  - `queue/s5_crash.py 4242 20` → `logs/kill_s5_crash.log`;
  - `ingest/s4_kill.py 20` → `logs/kill_s4.log`. Its `os.kill` of late orphans was replaced by a print; only Popen handles were killed.
- Accuracy:
  - `mrfx --config realstore/config/mrfx.yaml ingest` → `realstore/ingest.log`;
  - `perf/real_fetch.py <cfg> 8497 760513962 TX Moda` and `… 208741495 WY "Blue Cross Blue Shield of Wyoming"` (`REALOUT=realout_tx|realout_wy`);
  - `perf/check_real.py realout_tx 760513962 TX Moda`, `… realout_wy …`, and `perf/check_entity.py realout_tx` → `logs/check_real_{tx,wy}.log`, `logs/check_entity_tx.log`.
- Determinism: `perf/det.py <cfg> 8497 det/` → `logs/det.log`, `det/det.json`.
- New repros for P1-1: `repros/review13_repros/skip/c_long.py`, `c_restart.py`, `c_inbox_wait.py`, `c_trace.py`.
- Config: `cfgsim/mk.py write|load` (`load076.json`, `load077.json`, `def_*.json`). The updater was run as `apply_update(…, run_pip=False)` on `upd/old/Claudevideo`, which was deleted afterwards.
