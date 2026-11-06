"""`mrfx update <new build .zip>` — install a newer version over this one while
keeping everything the user has: the data/ folder (rates, names, the link
queue, backups), the .venv/ environment, and their settings files.

Drag the zip onto "Update MRF Explorer.bat", or run the command. Steps:
1. Check the zip is an MRF Explorer build (mrfx/ package + requirements.txt)
   and has no unsafe paths.
2. Zip up the program files it is about to replace into
   data/update_backups/ (so an update can be undone by hand).
3. Write every changed file from the new build — except data/, .venv/ and the
   user's settings (PROTECTED); a protected file the new build changed is
   saved next to it as <name>.new for reference.
4. Re-sync the environment (pip install -r requirements.txt, then pip install
   --no-build-isolation -e . — works offline) and check where the typed `mrfx`
   command now points.

It never deletes anything."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

# never written by an update: the user's data and environment
SKIP_DIRS = ("data/", ".venv/", "venv/", ".git/")
# the user's settings: kept as they are; the new build's copy -> <name>.new
PROTECTED = ("config/mrfx.yaml", "config/entity_map.yaml", "config/registry_overrides.yaml")


class UpdateError(Exception):
    """The update was refused before anything was changed — say why."""


def _members(z: zipfile.ZipFile) -> tuple[str, dict[str, zipfile.ZipInfo]]:
    """(top-level prefix, {relative path: info}) for the build in the zip."""
    names = [i for i in z.infolist() if not i.is_dir()]
    inits = [i.filename for i in names if i.filename.endswith("mrfx/__init__.py")]
    if not inits:
        raise UpdateError("this zip doesn't contain MRF Explorer (no mrfx/__init__.py) — "
                          "check you picked the right file")
    prefix = min(inits, key=len)[: -len("mrfx/__init__.py")]
    out = {}
    for i in names:
        if not i.filename.startswith(prefix):
            continue
        rel = i.filename[len(prefix):]
        p = PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts or ":" in rel or "\\" in rel:
            raise UpdateError(f"refusing an unsafe path inside the zip: {i.filename!r}")
        out[rel] = i
    if "requirements.txt" not in out:
        raise UpdateError("this zip is missing requirements.txt — not a complete build")
    return prefix, out


def _version_in(text: str) -> str | None:
    import re
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)', text)
    return m.group(1) if m else None


def apply_update(zip_path: Path, root: Path, run_pip: bool = True, log=print) -> dict:
    zip_path, root = Path(zip_path), Path(root)
    if not zip_path.is_file():
        raise UpdateError(f"file not found: {zip_path}")
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise UpdateError(f"{zip_path.name} isn't a readable zip ({e}) — download it again") from e
    with z:
        _, members = _members(z)
        old_v = None
        try:
            old_v = _version_in((root / "mrfx" / "__init__.py").read_text(encoding="utf-8"))
        except OSError:
            pass
        new_v = _version_in(z.read(members["mrfx/__init__.py"]).decode("utf-8", "replace"))

        plan = []                  # (rel, info, mode) mode: write | new
        for rel, info in sorted(members.items()):
            if rel.startswith(SKIP_DIRS):
                continue
            target = root / rel
            data = z.read(info)
            try:
                same = target.read_bytes() == data
            except OSError:
                same = False
            if same:
                continue
            if rel in PROTECTED and target.exists():
                try:   # the same .new from an earlier run: nothing to do
                    if target.with_name(target.name + ".new").read_bytes() == data:
                        continue
                except OSError:
                    pass
                plan.append((rel, data, "new"))
            else:
                plan.append((rel, data, "write"))

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = root / "data" / "update_backups" / f"before_update_{stamp}.zip"
        replaced = [rel for rel, _, mode in plan if mode == "write" and (root / rel).exists()]
        if replaced:
            backup.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as bz:
                for rel in replaced:
                    bz.write(root / rel, rel)
            log(f"saved the {len(replaced)} program file(s) being replaced to {backup}")

        written, new_copies = 0, []
        # the updater's own launcher LAST: cmd.exe re-reads a running .bat
        plan.sort(key=lambda t: t[0].lower().endswith(".bat"))
        for rel, data, mode in plan:
            target = root / rel
            if mode == "new":
                target = target.with_name(target.name + ".new")
                new_copies.append(str(target.relative_to(root)))
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".updating")
            tmp.write_bytes(data)
            tmp.replace(target)
            written += 1
    log(f"updated {written} file(s) (version {old_v or '?'} -> {new_v or '?'}); "
        "your data folder and settings were not touched")
    for c in new_copies:
        log(f"  your settings were kept; the new version's copy is saved as {c} "
            "(only needed if you want to see new options)")
    pip_ok = True
    linked = None
    if run_pip:
        log("re-syncing the program's components (pip) — usually a few seconds…")
        for args in PIP_STEPS:
            r = subprocess.run([sys.executable, *args], cwd=root)
            if r.returncode != 0:
                pip_ok = False
                log("  pip reported a problem (see above). The update itself is in place, "
                    "and \"Start MRF Explorer\" runs this folder's program regardless. "
                    "To finish the re-sync, run these two lines in the program folder:\n"
                    "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
                    "    .venv\\Scripts\\python.exe -m pip install --no-build-isolation -e .")
                break
        if pip_ok:
            linked = linked_folder(sys.executable)
            if linked is not None and linked != (root / "mrfx").resolve():
                pip_ok = False
                where = ("is not set up in this environment" if linked == NOT_IMPORTABLE
                         else f"still points at {linked.parent}, not this folder")
                log(f"  the typed `mrfx` command {where}. \"Start MRF Explorer\" and "
                    "\"Update MRF Explorer\" are not affected (they run this folder's "
                    "program directly).")
    return {"written": written, "kept_settings": new_copies, "backup": str(backup) if replaced else None,
            "old_version": old_v, "new_version": new_v, "pip_ok": pip_ok, "linked": linked}


# The re-sync after an update. `-e .` re-points the typed `mrfx` command at THIS
# folder (it remembers an absolute path, so a renamed or moved folder broke it:
# "No module named 'mrfx'"). --no-build-isolation builds with the setuptools
# pinned in requirements.txt instead of downloading one: with isolation, every
# update needed PyPI, and offline the step failed and left the old path.
PIP_STEPS = (["-m", "pip", "install", "-q", "-r", "requirements.txt"],
             ["-m", "pip", "install", "-q", "--no-build-isolation", "--no-deps", "-e", "."])


NOT_IMPORTABLE = Path("<not importable>")


def linked_folder(python: str) -> Path | None:
    """The mrfx package folder the environment's `mrfx` command imports —
    asked from OUTSIDE the program folder, where the current directory cannot
    mask a stale install path. None when it cannot be told."""
    import tempfile
    try:
        r = subprocess.run([python, "-c", "import mrfx, pathlib; "
                            "print(pathlib.Path(mrfx.__file__).resolve().parent)"],
                           cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return NOT_IMPORTABLE
    return Path(r.stdout.strip().splitlines()[-1])
