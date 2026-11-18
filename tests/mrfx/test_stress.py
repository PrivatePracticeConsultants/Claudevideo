"""Review 14 Group 4: the two race tests (round 13's 5.2.7 and this round's
2.3) run N times beside a CPU hog — the conditions under which 1 run in 30
of the index test ended with its rate file 'failed' on the 0.7.7 zip.

    MRFX_STRESS_N=50 pytest tests/mrfx/test_stress.py -m stress

Slow tier (never in `-m quick`); N defaults to 3 so the full suite stays
bearable — the release check runs it with 50."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RACES = ["tests/mrfx/test_fetch.py::test_toc_url_expands_and_children_ingest",
         "tests/mrfx/test_fetch.py::test_parallel_ingest_matches_serial"]


def _hogs(n: int) -> list[subprocess.Popen]:
    return [subprocess.Popen([sys.executable, "-c", "while True: pass"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(n)]


@pytest.mark.stress
@pytest.mark.parametrize("nodeid", RACES)
def test_race_test_holds_under_a_cpu_hog(nodeid):
    n = int(os.environ.get("MRFX_STRESS_N", "3"))
    hogs = _hogs(max(1, (os.cpu_count() or 2) // 2))
    failures = []
    try:
        for i in range(n):
            r = subprocess.run([sys.executable, "-m", "pytest", nodeid, "-q", "-x",
                                "-p", "no:cacheprovider"], cwd=str(REPO),
                               capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                failures.append((i, r.stdout[-1500:]))
    finally:
        for h in hogs:
            h.kill()
            h.wait(10)
    assert failures == [], f"{len(failures)} of {n} runs failed: {failures[:2]}"
