"""Automatic NPPES file: the app keeps CMS's monthly provider file on hand by
itself (enrichment.auto_download) so practice names resolve in one local pass
instead of crawling the NPPES API — and a new month refreshes providers whose
details changed. A local HTTP server stands in for download.cms.gov."""

from __future__ import annotations

import csv
import http.server
import io
import threading
import zipfile

import pytest

import mrfx.enrich as E
import mrfx.nppes_auto as A
from tests.mrfx.test_hardening import _payer_part


def _nppes_zip(rows) -> bytes:
    hdr = [E._BULK_COLS[k] for k in
           ("npi", "org", "first", "last", "city", "state", "tax1", "entity", "address", "zip", "phone")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(hdr)
    w.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("npidata_pfile_20261101-20261109_fileheader.csv", b"header only\n")
        zf.writestr("npidata_pfile_20261101-20261109.csv", buf.getvalue().encode())
    return out.getvalue()


NOV = "NPPES_Data_Dissemination_November_2026_V2.zip"
DEC = "NPPES_Data_Dissemination_December_2026_V2.zip"
ROW_A = ["1000000001", "Cornerstone PT", "", "", "Columbus", "OH", "225100000X", "2",
         "1 Main St", "43004", "6145551212"]
ROW_B = ["1000000002", "", "Jane", "Doe", "Dayton", "OH", "225100000X", "1",
         "2 Elm St", "45402", "9375550000"]


@pytest.fixture
def cms(monkeypatch):
    """A fake download.cms.gov: page + files, with a request log."""
    files = {NOV: _nppes_zip([ROW_A, ROW_B])}
    hits = []
    state = {"page": [NOV]}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.lstrip("/").split("?")[0]
            hits.append(name)
            if name == "NPI_Files.html":
                links = "".join(f'<a href="./{n}">{n}</a>' for n in state["page"])
                body = (f"<html>{links}<a href='./NPPES_Deactivated_NPI_Report_110226_V2.zip'>d</a>"
                        "<a href='./NPPES_Data_Dissemination_102626_110126_Weekly_V2.zip'>w</a>"
                        "</html>").encode()
            elif name in files:
                body = files[name]
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(A, "NPPES_FILES_PAGE", f"http://127.0.0.1:{srv.server_address[1]}/NPI_Files.html")
    monkeypatch.delenv("MRFX_NO_AUTO_DOWNLOAD", raising=False)
    monkeypatch.setattr(E, "_bulk_sig", None)
    yield {"files": files, "hits": hits, "state": state}
    srv.shutdown()


def test_newest_local_picks_by_month_in_the_name(tmp_path):
    for n in ("NPPES_Data_Dissemination_December_2025.zip", NOV,
              "NPPES_Data_Dissemination_October_2026_V2.zip",
              "NPPES_Data_Dissemination_102626_110126_Weekly_V2.zip",   # weekly: never "full"
              "NPPES_Deactivated_NPI_Report_110226_V2.zip", NOV.replace(".zip", ".zip.part")):
        (tmp_path / n).write_bytes(b"x")
    assert A.newest_local(tmp_path).name == NOV


def test_bulk_source_resolution(cfg, tmp_path):
    folder = tmp_path / "nppes"
    folder.mkdir()
    (folder / NOV).write_bytes(b"x")
    cfg.enrichment.nppes_dir = folder
    assert A.bulk_source(cfg) == folder / NOV                 # unset -> nppes_dir's newest
    explicit = tmp_path / "mine.zip"
    cfg.enrichment.bulk_csv_path = explicit
    assert A.bulk_source(cfg) == explicit                     # an explicit file always wins
    cfg.enrichment.bulk_csv_path = folder
    assert A.bulk_source(cfg) == folder / NOV                 # a folder -> its newest


def test_auto_download_then_local_lookups_then_monthly_refresh(cfg, store, cms, tmp_path):
    cfg.enrichment.nppes_dir = tmp_path / "nppes"
    cfg.enrichment.mode = "api"                               # the shipped default
    _payer_part(store, "r.json", "P", [("430000001", "1000000001", "97110", "2026-06", 50.0),
                                       ("430000001", "1000000002", "97110", "2026-06", 52.0)])
    store.upsert_file("r.json", file_type="in_network", status="done", rows_emitted=2)

    # 1) fresh install, api mode, no file: run_enrichment fetches the file itself
    #    and resolves locally — no NPPES API request at all
    api_calls = []
    import mrfx.enrich as enrich_mod
    real_api = enrich_mod.enrich_via_api
    enrich_mod.enrich_via_api = lambda *a, **k: api_calls.append(1) or real_api(*a, **k)
    try:
        assert E.run_enrichment(cfg, store) == 2
    finally:
        enrich_mod.enrich_via_api = real_api
    assert api_calls == []
    assert (cfg.enrichment.nppes_dir / NOV).exists()
    with store.connect() as con:
        assert con.execute("SELECT org_name FROM npi_directory WHERE npi='1000000001'"
                           ).fetchone()[0] == "Cornerstone PT"

    # 2) within a day it doesn't even look at CMS again
    n_page = cms["hits"].count("NPI_Files.html")
    assert A.ensure_current(cfg) == cfg.enrichment.nppes_dir / NOV
    assert cms["hits"].count("NPI_Files.html") == n_page

    # 3) CMS publishes December; Cornerstone renamed. The next check downloads
    #    it, and the next pass refreshes the already-identified provider.
    renamed = ["1000000001", "Cornerstone Physical Therapy LLC"] + ROW_A[2:]
    cms["files"][DEC] = _nppes_zip([renamed, ROW_B])
    cms["state"]["page"] = [DEC]
    (cfg.enrichment.nppes_dir / "NPPES_Data_Dissemination_September_2026_V2.zip").write_bytes(b"old")
    assert A.ensure_current(cfg, force=True) == cfg.enrichment.nppes_dir / DEC
    kept = sorted(p.name for p in cfg.enrichment.nppes_dir.glob("NPPES_*.zip"))
    assert kept == [DEC, NOV]                                 # newest two kept, older pruned
    E.enrich_via_bulk(cfg, store)
    with store.connect() as con:
        assert con.execute("SELECT org_name FROM npi_directory WHERE npi='1000000001'"
                           ).fetchone()[0] == "Cornerstone Physical Therapy LLC"
        assert con.execute("SELECT org_name FROM npi_directory WHERE npi='1000000002'"
                           ).fetchone()[0] == "Jane Doe"     # unchanged provider untouched


def test_offline_never_raises_and_retries_within_the_hour(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("MRFX_NO_AUTO_DOWNLOAD", raising=False)
    monkeypatch.setattr(A, "NPPES_FILES_PAGE", "http://127.0.0.1:9/NPI_Files.html")  # nothing listens
    cfg.enrichment.nppes_dir = tmp_path / "nppes"
    assert A.ensure_current(cfg) is None
    age = __import__("time").time() - (cfg.enrichment.nppes_dir / A._STAMP).stat().st_mtime
    assert A.CHECK_INTERVAL_S - age <= A.RETRY_AFTER_FAILURE_S + 5


def test_explicit_file_or_switched_off_means_no_network(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("MRFX_NO_AUTO_DOWNLOAD", raising=False)
    monkeypatch.setattr(A, "latest_remote", lambda cfg: pytest.fail("must not check CMS"))
    cfg.enrichment.bulk_csv_path = tmp_path / "my_own_nppes.zip"
    assert A.ensure_current(cfg) is None
    cfg.enrichment.bulk_csv_path = None
    cfg.enrichment.auto_download = False
    assert A.ensure_current(cfg) is None
    cfg.enrichment.auto_download = True
    cfg.enrichment.mode = "off"
    assert A.ensure_current(cfg) is None


def test_a_broken_download_is_discarded(cfg, store, cms, tmp_path):
    cms["files"][NOV] = b"<html>maintenance page</html>"
    cfg.enrichment.nppes_dir = tmp_path / "nppes"
    assert A.ensure_current(cfg) is None
    assert not (cfg.enrichment.nppes_dir / NOV).exists()


def test_only_one_process_downloads_the_monthly_file(cfg, cms, tmp_path):
    """Round 3 P1.4: serve's enrichment loop and a CLI command both saw a
    stale stamp and appended to the same .part. A lock owned by a LIVE
    process makes the second one stand back; a dead owner's lock is taken
    over (a crashed download must not block the feature forever)."""
    import os
    folder = cfg.enrichment.nppes_dir = tmp_path / "nppes"   # never the repo's data/
    folder.mkdir(parents=True, exist_ok=True)
    A._LAST_CHECK.clear()
    (folder / A._LOCK).write_text(str(os.getpid()))          # someone alive holds it
    assert A.ensure_current(cfg, force=True) is None
    assert NOV not in cms["hits"]                              # page checked, file NOT fetched
    (folder / A._LOCK).write_text("999999")                   # a dead process's lock
    got = A.ensure_current(cfg, force=True)
    assert got is not None and got.name == NOV and NOV in cms["hits"]
    assert not (folder / A._LOCK).exists()                    # released afterwards


def test_an_unwritable_stamp_does_not_mean_asking_cms_all_day(cfg, cms, monkeypatch, tmp_path):
    """If the stamp file can't be written (OneDrive lock / read-only folder),
    the check used to repeat every enrichment tick (~20 s). The in-memory
    record now spaces checks like the file would."""
    cfg.enrichment.nppes_dir = tmp_path / "nppes"            # never the repo's data/
    A._LAST_CHECK.clear()
    real = A.Path.write_text

    def no_stamp(self, *a, **k):
        if self.name == A._STAMP:
            raise PermissionError("locked")
        return real(self, *a, **k)
    monkeypatch.setattr(A.Path, "write_text", no_stamp)
    A.ensure_current(cfg)
    pages = cms["hits"].count("NPI_Files.html")
    for _ in range(3):
        A.ensure_current(cfg)
    assert cms["hits"].count("NPI_Files.html") == pages == 1


def _edge_csv(path, extra_bad=False):
    hdr = [E._BULK_COLS[k] for k in E._BULK_COLS] + E._BULK_TAX_SLOTS[1:4]
    rows = []
    base = dict.fromkeys(hdr, "")

    def row(**kw):
        r = dict(base)
        for k, v in kw.items():
            r[E._BULK_COLS.get(k, k)] = v
        rows.append([r[h] for h in hdr])
    row(npi="1000000001", org="Org LLC", entity="2", tax1="261QP2000X",
        **{E._BULK_TAX_SLOTS[1]: " 225100000X ", E._BULK_TAX_SLOTS[2]: ""})
    row(npi="1000000002", first="Jane", last="Doe", entity=" 1 ", city="Dayton", state="OH")
    row(npi="1000000003", first="Solo", entity="1")                 # first only
    row(npi="1000000004", last="Lastonly", entity="9")              # last only, odd entity
    row(npi="1000000005", org=" ", first="X", last="Y")            # a blank-looking org
    row(npi="", org="no npi -> skipped")
    row(npi="1000000006", **{E._BULK_TAX_SLOTS[3]: "235Z00000X"},  # tax only in slot 4
        enumerated="05/01/2026", deactivated="", reactivated="")
    import csv as _csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows)
    if extra_bad:
        with open(path, "ab") as f:
            f.write(b"1000000007,Bad \xff byte" + b"," * (len(hdr) - 2) + b"\n")


def test_fast_and_tolerant_nppes_readers_agree(tmp_path):
    """Round 3 P1.4: the cache builder now streams with pyarrow (61 s / 0.39 GB
    on the real 9.9M-row file vs 164 s) — it must produce exactly what the
    tolerant Python reader produces, field by field, on the awkward cases."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    src = tmp_path / "npidata_pfile_x.csv"
    _edge_csv(src)
    schema = pa.schema([(c, pa.string()) for c in E._NPPES_CACHE_COLS])
    fast, slow = tmp_path / "fast.parquet", tmp_path / "slow.parquet"
    n1 = E._write_nppes_parquet_arrow(src, fast, schema, None)
    n2 = E._write_nppes_parquet_python(src, tmp_path / "s.tmp", slow, schema, None)
    assert n1 == n2 == 6
    a, b = pq.read_table(fast).to_pylist(), pq.read_table(slow).to_pylist()
    assert a == b
    by = {r["npi"]: r for r in a}
    assert by["1000000002"]["org_name"] == "Jane Doe" and by["1000000002"]["entity_type"] == "NPI-1"
    assert by["1000000003"]["org_name"] == "Solo" and by["1000000004"]["entity_type"] is None
    assert by["1000000001"]["taxonomy_codes"] == "261QP2000X|225100000X"
    assert by["1000000006"]["taxonomy_codes"] == "235Z00000X"


def test_an_odd_nppes_file_falls_back_to_the_tolerant_reader(tmp_path, caplog):
    src = tmp_path / "npidata_pfile_x.csv"
    _edge_csv(src, extra_bad=True)                  # invalid UTF-8 the fast reader refuses
    with caplog.at_level("INFO"):
        n = E._write_nppes_parquet(None, None, tmp_path / "cache.parquet", None, src=src)
    assert n == 7 and "tolerant reader" in caplog.text
