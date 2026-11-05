"""`mrfx update <zip>`: a newer build goes in, the user's data, environment
and settings stay exactly as they were."""

from __future__ import annotations

import io
import zipfile

import pytest

from mrfx.updater import UpdateError, apply_update


def _install(root):
    files = {
        "mrfx/__init__.py": '__version__ = "0.2.0"\n',
        "mrfx/old.py": "OLD = 1\n",
        "config/mrfx.yaml": "port: 9999   # the user's own edit\n",
        "config/known_sources.yaml": "sources: []  # old list\n",
        "config/entity_map.yaml": "mine: true\n",
        "data/mrfx_store/rates/abc.parquet": "PARQUET-BYTES",
        "data/mrfx_store/mrfx.duckdb": "DB-BYTES",
        "data/inbox/payer.json": "{}",
        ".venv/pyvenv.cfg": "home = x\n",
        "requirements.txt": "duckdb\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return files


def _build_zip(path, files, prefix="Claudevideo/"):
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in files.items():
            z.writestr(prefix + rel, text)
    return path


NEW = {
    "mrfx/__init__.py": '__version__ = "0.3.0"\n',
    "mrfx/old.py": "OLD = 2  # changed\n",
    "mrfx/brand_new.py": "NEW = 1\n",
    "config/mrfx.yaml": "port: 8377   # shipped default\n",
    "config/known_sources.yaml": "sources: [new]\n",
    "requirements.txt": "duckdb\n",
    "data/mrfx_store/mrfx.duckdb": "MUST-NOT-OVERWRITE",
    ".venv/pyvenv.cfg": "MUST-NOT-OVERWRITE",
    "Update MRF Explorer.bat": "@echo off\r\n",
}


def test_update_replaces_program_and_keeps_data_and_settings(tmp_path):
    root = tmp_path / "install"
    before = _install(root)
    z = _build_zip(tmp_path / "mrfx_project.zip", NEW)
    res = apply_update(z, root, run_pip=False, log=lambda *a: None)

    assert (root / "mrfx/old.py").read_text() == "OLD = 2  # changed\n"
    assert (root / "mrfx/brand_new.py").exists()
    assert (root / "config/known_sources.yaml").read_text() == "sources: [new]\n"   # new payer list
    assert (root / "Update MRF Explorer.bat").exists()
    # the user's settings are kept; the new version's copy sits beside them
    assert (root / "config/mrfx.yaml").read_text() == before["config/mrfx.yaml"]
    assert (root / "config/mrfx.yaml.new").read_text() == NEW["config/mrfx.yaml"]
    assert (root / "config/entity_map.yaml").read_text() == "mine: true\n"
    # data and the environment are never written, even when the zip has them
    for rel in ("data/mrfx_store/rates/abc.parquet", "data/mrfx_store/mrfx.duckdb",
                "data/inbox/payer.json", ".venv/pyvenv.cfg"):
        assert (root / rel).read_text() == before[rel], rel
    assert (res["old_version"], res["new_version"]) == ("0.2.0", "0.3.0")
    # the replaced program files are backed up (and only those)
    with zipfile.ZipFile(res["backup"]) as b:
        assert sorted(b.namelist()) == ["config/known_sources.yaml", "mrfx/__init__.py", "mrfx/old.py"]
        assert b.read("mrfx/old.py") == b"OLD = 1\n"

    again = apply_update(z, root, run_pip=False, log=lambda *a: None)
    assert again["written"] == 0 and again["backup"] is None       # idempotent


@pytest.mark.parametrize("bad, why", [
    ({"README.md": "x"}, "doesn't contain MRF Explorer"),
    ({"mrfx/__init__.py": "x", "../evil.py": "x", "requirements.txt": ""}, "unsafe path"),
    ({"mrfx/__init__.py": "x"}, "missing requirements.txt"),
])
def test_refused_zips_change_nothing(tmp_path, bad, why):
    root = tmp_path / "install"
    before = _install(root)
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as zz:
        for rel, text in bad.items():
            zz.writestr("Claudevideo/" + rel, text)   # incl. Claudevideo/../evil.py
    with pytest.raises(UpdateError, match=why):
        apply_update(z, root, run_pip=False, log=lambda *a: None)
    for rel, text in before.items():
        assert (root / rel).read_text() == text
    assert not (root / "data/update_backups").exists()


def test_not_a_zip_is_refused(tmp_path):
    p = tmp_path / "mrfx_project.zip"
    p.write_bytes(b"<html>download page</html>")
    with pytest.raises(UpdateError, match="isn't a readable zip"):
        apply_update(p, tmp_path, run_pip=False, log=lambda *a: None)


def test_update_command_refuses_while_the_program_runs(cfg, monkeypatch, capsys, tmp_path):
    import mrfx.cli as cli
    monkeypatch.setattr(cli, "_something_owns_the_port", lambda cfg, action: True)
    args = type("A", (), {"zip": str(tmp_path / "x.zip"), "no_pip": True})()
    assert cli.cmd_update(cfg, args) == 1
    assert "Close the MRF Explorer window" in capsys.readouterr().out
