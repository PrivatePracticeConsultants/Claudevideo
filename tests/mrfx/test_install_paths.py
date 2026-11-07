"""The program must start and update from wherever its folder lives (round 5,
P0.1). The typed `mrfx` command is a pip console script that imports the
package from the folder it was INSTALLED in; the user renamed the folder
(E:\\mrfx_project -> E:\\mrfx_project 11.5.26) and "Start MRF Explorer" died
with `ModuleNotFoundError: No module named 'mrfx'`. The launchers now run the
folder's own environment with `python -m mrfx`, which imports from the folder
it is started in, and the updater re-links the typed command without needing
the internet. Windows itself is not available here; these pin the parts that
decide it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BATS = ("Start MRF Explorer (auto-restart).bat", "Update MRF Explorer.bat")


def _commands(bat: str) -> list[str]:
    return [ln.strip() for ln in (ROOT / bat).read_text().splitlines()
            if ln.strip() and not ln.strip().upper().startswith(("REM", "ECHO", "@ECHO"))]


def test_launchers_run_the_folders_own_python_never_the_installed_command():
    for bat in BATS:
        cmds = _commands(bat)
        runs = [c for c in cmds if "-m mrfx" in c]
        assert runs, bat
        assert all(c.startswith('".venv\\Scripts\\python.exe" -m mrfx') for c in runs), runs
        # no bare `mrfx …` (the console script) and no activate.bat, whose
        # VIRTUAL_ENV is an absolute path that a renamed folder also breaks
        assert not [c for c in cmds if re.match(r"^\(?\s*mrfx\s", c)], bat
        assert not [c for c in cmds if "activate" in c.lower()], bat
        assert "\r\n" in (ROOT / bat).read_bytes().decode(), f"{bat}: cmd.exe wants CRLF"


def test_python_m_mrfx_runs_the_copy_it_is_started_in(tmp_path):
    """A copy of the program in another folder, with the current environment
    (whose editable install points at the ORIGINAL folder): `python -m mrfx`
    started in the copy imports the copy."""
    moved = tmp_path / "renamed folder 11.5.26"
    shutil.copytree(ROOT / "mrfx", moved / "mrfx",
                    ignore=shutil.ignore_patterns("__pycache__"))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-m", "mrfx", "--help"], cwd=moved, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "serve" in r.stdout, r.stderr[-500:]
    r = subprocess.run([sys.executable, "-c", "import mrfx; print(mrfx.__file__)"],
                       cwd=moved, env=env, capture_output=True, text=True, timeout=60)
    assert Path(r.stdout.strip()).resolve().parent == (moved / "mrfx").resolve()


def test_update_relinks_without_build_isolation_and_checks_the_link(tmp_path, monkeypatch):
    import zipfile

    import mrfx.updater as up
    assert any("--no-build-isolation" in step for step in up.PIP_STEPS)
    assert "setuptools==" in (ROOT / "requirements.txt").read_text()
    root = tmp_path / "Claudevideo"
    (root / "mrfx").mkdir(parents=True)
    (root / "mrfx" / "__init__.py").write_text('__version__ = "0.0.1"\n')
    build = tmp_path / "new.zip"
    with zipfile.ZipFile(build, "w") as z:
        z.writestr("Claudevideo/mrfx/__init__.py", '__version__ = "0.0.2"\n')
        z.writestr("Claudevideo/requirements.txt", "duckdb==1.5.5\n")
    calls = []
    monkeypatch.setattr(up.subprocess, "run",
                        lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0))
    # the link still points elsewhere after pip: reported, never silent
    monkeypatch.setattr(up, "linked_folder", lambda py: Path("/somewhere/else/mrfx"))
    logs = []
    res = up.apply_update(build, root, log=logs.append)
    # round 7 P1.19: the NEW build's requirements first (from the zip, before
    # any file is written), then the editable relink only
    assert calls[0][1:5] == ["-m", "pip", "install", "-q"] and calls[0][5] == "-r"
    assert calls[0][6].endswith("requirements.txt")
    assert [c[1:] for c in calls[1:]] == [list(s) for s in up.PIP_STEPS[1:]]
    assert not res["pip_ok"] and any("still points at" in m for m in logs)
    # and the right link is accepted
    monkeypatch.setattr(up, "linked_folder", lambda py: (root / "mrfx").resolve())
    (root / "mrfx" / "__init__.py").write_text('__version__ = "0.0.1"\n')
    assert up.apply_update(build, root, log=logs.append)["pip_ok"]


def test_linked_folder_reports_this_environments_install():
    from mrfx.updater import linked_folder
    assert linked_folder(sys.executable) == (ROOT / "mrfx").resolve()


def test_serve_says_so_when_started_in_another_copys_folder(tmp_path, monkeypatch):
    from mrfx.cli import _warn_if_running_another_folders_code
    monkeypatch.chdir(ROOT)
    assert _warn_if_running_another_folders_code() is None
    other = tmp_path / "Claudevideo"
    (other / "mrfx").mkdir(parents=True)
    (other / "mrfx" / "__init__.py").write_text("")
    monkeypatch.chdir(other)
    msg = _warn_if_running_another_folders_code()
    assert msg and str(other) in msg and "Start MRF Explorer" in msg
