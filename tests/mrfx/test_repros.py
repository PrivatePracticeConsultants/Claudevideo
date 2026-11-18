"""Review 14 Group 4: the evidence checks itself.

Round 13's executor ran its repro scripts and recorded their output, but one
output (`rates: 0`) showed a regression nobody read. Here every repro script
of rounds 11-14 that finishes in well under a minute is RUN against the code
under test, in a scratch copy, and each recorded "after the fix" line is
asserted (CASES: script -> substrings that must appear, and ones that must
not). Slow tier (never in `-m quick`).

The scripts live under docs/review14_repros/ (round 14's own, plus the
regression sweep's copies of rounds 11-13 with their scratch paths made
relative); this test rewrites the one absolute scratch prefix they still
carry to the test's own temp folder. A script that needs something only the
original audit box had (the real payer files, the 99M store, Chromium for a
live UI walk) is not in the table."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "docs" / "review14_repros"
OLD_R11 = ("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/"
           "scratchpad/audit14/regress/r11tmp")

R11 = "regress/review11_repros"
R12 = "regress/review12_repros"
R13 = "regress/review13_repros"

# (script, [must appear], [must NOT appear])
CASES: list[tuple[str, list[str], list[str]]] = [
    # ---- round 11 (status block 11; regression sweep 14 "held")
    (f"{R11}/download/c01_truncate_resume.py", ["sha_match=True"], ["sha_match=False"]),
    (f"{R11}/download/c02_range_odd.py", ["sha==v2 True"], ["sha==v2 False"]),
    (f"{R11}/download/c02b_json_splice.py", ["('97110', 55.0, 10)"], []),
    (f"{R11}/download/c05_http_errors.py", ["this link redirects in a loop", "the file is empty (0 bytes)"], []),
    (f"{R11}/download/c05b_bad_encoding.py", ["the server sent a compressed body that cannot be read"], []),
    (f"{R11}/download/c08b_wrong_sidecar_json.py", ["sha==truth True", "rates rows: 300"], []),
    (f"{R11}/download/c09_early_dedup.py", ["checksum-equal to"], []),
    (f"{R11}/queue/s3_toc.py", ["files still missing: []"], []),
    (f"{R11}/queue/s11_oversize_reexpand.py", ["GETs on big so far: 1"], []),
    (f"{R11}/queue/s4k_twin_kill.py", ["[B] rates: [{'source_file':", "'n': 6}]"], []),
    (f"{R11}/queue/s12_waiting_drain.py", ["left 'paused': 0 of 6"], []),
    (f"{R11}/ingest/s2_samename.py", ["contract lines held twice: []"], []),
    (f"{R11}/ingest/s2b_keep_stuck.py", ["0/30 confirmed files ended with a status other than 'done'"], []),
    (f"{R11}/ingest/s3_claims.py", ["inflight rows left: []"], []),
    (f"{R11}/ingest/s1_containers.py", ["the file is empty (0 bytes)", "saved as UTF-16"], []),
    (f"{R11}/ingest/s9b_opens.py", ["2.01x the file, 7 opens"], []),
    (f"{R11}/ingest/s7_pool.py", ["done rows=25"], []),
    (f"{R11}/seams/stale_reuse.py", ["2026-06"], []),
    (f"{R11}/seams/refresh_static_children.py", ["'2026-06', 'done')] (80,)"], []),
    (f"{R11}/seams/dest_no_sidecar.py", ["requests: []", "bytes served: 0 of 22632"], []),
    (f"{R11}/seams/content_encoding.py", ["identical: True"], []),
    # ---- round 12
    (f"{R12}/agent_2_5/npi_drawer_latest.py", ["explorer npi grain latest rows: [('97110', 'GP', '2026-06', 42.0)]"], []),
    (f"{R12}/agent_diff/s1_unquoted_etag.py", ["OK sha"], []),
    (f"{R12}/agent_diff/s4_refresh_old_url.py", ["'2026-06', 80)]", "'2026-05', 50)]"], []),
    (f"{R12}/agent_diff/s5_crash_inbox_claim.py", ["recover_stuck_urls: 1", "did not finish"], []),
    (f"{R12}/agent_diff/s7_clear_during_recheck.py", ["'error': 'cleared from the queue'", "file months: [('2026-05', 6)]"], []),
    (f"{R12}/agent_diff/s8_stale_splices.py", ["OK True"], []),
    # ---- round 13
    (f"{R13}/skip/s1_dedupkey.py", ["DIFFERS from the stated expectation: 0"], []),
    (f"{R13}/skip/s10_known.py", ["user-pasted non-catalog index after the tick: [{'id': 4, 'status': 'queued'"], []),
    (f"{R13}/skip/s4_claims.py", [], ["rates 0 "]),
    (f"{R13}/diff/p_a_val_compat.py", ["FAILS: 0"], []),
    (f"{R13}/diff/p_e_live_twin.py", ["RESULT: held"], []),
    (f"{R13}/diff/p_g_loaded_size_unknown.py", ["file months now: [('2026-06',)]"], []),
    # ---- round 14 (this round's own repros, after the fixes)
    ("diff_data/scripts/s10_killwindow_twin.py", ["rate rows=60"], ["rate rows=120"]),
    ("diff_data/scripts/s11_failed_link_claim.py",
     ["content claims held = {}", "rate rows: 60", "scan_inbox returned within 20 s: True"], []),
    ("diff_data/scripts/s13_recheck_new_children.py", ["1 new file listed — not downloaded"],
     ["'/2026-12-01_twin_in-network-rates.json.gz', 200"]),
    ("diff_data/scripts/s2_midplan.py", ["spine identical to fresh build: True"],
     ["spine identical to fresh build: False"]),
    ("diff_data/scripts/s3_names_plan_and_header.py", ["S3b header: Analytics cover 3 of 4 payers"],
     ["the files loaded since are not in the analytics yet"]),
    ("diff_data/scripts/s5_rule_sentence.py", ["200 /api/leads.csv                         rule printed: True",
                                               "200 /api/report/market                     rule printed: True",
                                               "200 /api/report/dossier                    rule printed: True"],
     ["200 /api/report/ratecard                   rule printed: False"]),
    ("diff_data/scripts/s7_month_twin.py", [], ['"skipped"']),
    ("diff_data/scripts/s8_legacy_twin.py", ["backfilled: 1", "rate rows: 60"], ["rate rows: 120"]),
    ("diff_data/scripts/s9_reread_codes.py", ["spine 97110 rows: [(10,)] stale: False"], []),
    ("diff_data/scripts/s4_old_vs_new.py", ["IDENTICAL overview TX"], ["DIFFERENT"]),
    ("diff_ui/r01_digest_stop_poisons_cache.py", ["store._digest_cache rows with error: 0"], []),
    ("diff_ui/r02_size_premium_stop_cached.py", ["next plain request: 200 subject.position = 0.0"], []),
    ("diff_ui/r03_backup_lists_parts_before_lock.py", ["ROWS LOST IN THE BACKUP: 0"], []),
    # (the outreach zip differs only by its documented "Generated:" stamp)
    ("diff_ui/r09_bytes_with_without_bar.py", ["SAME 200/200 POST /api/report/market",
                                               "SAME 200/200 GET /api/export.csv"], ["DIFF 200/200 POST"]),
    ("diff_ui/r13_cache_after_forget.py", ["freshness          before=['Beta Health', 'Alpha Health']"], []),
    ("diff_ui/r14_poll_under_saturation.py", ["the stopped run answered: 409"], []),
    ("diff_ui/r15_query_token_in_outputs.py", ["token inside: False"], ["token inside: True"]),
]


@pytest.fixture(scope="module")
def scratch(tmp_path_factory) -> Path:
    dst = tmp_path_factory.mktemp("repros")
    shutil.copytree(SRC / "regress" / "repros", dst / "regress")
    shutil.copytree(SRC / "diff_data", dst / "diff_data")
    shutil.copytree(SRC / "diff_ui", dst / "diff_ui")
    r11 = dst / "r11tmp"
    r11.mkdir()
    for p in dst.rglob("*.py"):
        t = p.read_text()
        if OLD_R11 in t:
            p.write_text(t.replace(OLD_R11, str(r11)))
    # round 14's data scripts compare against 0.7.6 (c8b80fc) where a copy
    # of it is present (diff_data/old); without git history they run alone
    old = dst / "diff_data" / "old"
    if not old.exists():
        try:
            old.mkdir()
            a = subprocess.run(["git", "-C", str(REPO), "archive", "c8b80fc", "mrfx"],
                               capture_output=True, check=True, timeout=60).stdout
            subprocess.run(["tar", "-x", "-C", str(old)], input=a, check=True, timeout=60)
        except Exception:  # noqa: BLE001 — a zip install has no history
            shutil.rmtree(old, ignore_errors=True)
    return dst


def _run(scratch: Path, rel: str, timeout: float = 240) -> str:
    p = scratch / rel
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    r = subprocess.run([sys.executable, str(p)], cwd=str(p.parent), env=env,
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout + "\n" + r.stderr


@pytest.mark.slow
@pytest.mark.parametrize("rel,must,must_not", CASES, ids=[c[0] for c in CASES])
def test_repro_holds(scratch, rel, must, must_not):
    if "diff_data/scripts/s8_legacy_twin" in rel or "s7_month_twin" in rel:
        if not (scratch / "diff_data" / "old").exists():
            pytest.skip("needs the 0.7.6 code from git history (c8b80fc)")
    out = _run(scratch, rel)
    missing = [m for m in must if m not in out]
    present = [m for m in must_not if m in out]
    assert not missing and not present, (
        f"{rel}: missing {missing}, unexpected {present}\n--- output tail ---\n"
        + "\n".join(out.splitlines()[-30:]))
