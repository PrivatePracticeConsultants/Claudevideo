"""Timestamps on a PC whose clock is not UTC (round 3, P0.7).

DuckDB stores an AWARE datetime in a TIMESTAMP column as LOCAL wall-clock time
(06:30Z -> 01:30 under New York) and a naive one as-is. The rollup coverage
marker and the names marker are compared against those stamps, so on the
night clocks fall back (2026-11-01, 02:00 EDT -> 01:00 EST) a file finishing
30 real minutes after the marker got an EARLIER stamp, never entered the
incremental delta, and its rows were missing from the analytics for good
(rollups_stale() False). Names saved by the API path (aware) landed 5 h
"before" bulk saves (naive UTC) and were never folded into the directory.

These run in a subprocess with TZ=America/New_York (this box runs UTC, so an
in-process test could never see the bug) and drive the clock across the
fall-back hour.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

DRIVER = textwrap.dedent('''
    import datetime as dt, json, sys, types
    from pathlib import Path

    if __name__ == "__main__":
        import mrfx.store as ms
        from mrfx.config import MrfxConfig
        from mrfx.ingest import ingest_file

        clock = {"t": dt.datetime(2026, 11, 1, 5, 50, tzinfo=dt.timezone.utc)}  # 01:50 EDT

        class FakeDT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                t = clock["t"]
                return t.astimezone(tz) if tz is not None else t.astimezone().replace(tzinfo=None)

        fake = types.SimpleNamespace(datetime=FakeDT, timezone=dt.timezone,
                                     date=dt.date, timedelta=dt.timedelta)
        ms.dt = fake
        import mrfx.ingest as mi
        mi.dt = fake

        d = Path(sys.argv[1])
        cfg = MrfxConfig(inbox_dir=d / "in", processed_dir=d / "done", failed_dir=d / "bad",
                         store_dir=d / "store", downloads_dir=d / "dl",
                         entity_map_path=d / "em.yaml")
        cfg.ensure_dirs()
        st = ms.Store(cfg.store_dir)

        def mrf(payer, tin, npi, rate):
            return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
                    "last_updated_on": "2026-11-01", "version": "2.0.0",
                    "provider_references": [], "in_network": [{
                        "negotiation_arrangement": "ffs", "name": "x",
                        "billing_code_type": "CPT", "billing_code_type_version": "2026",
                        "billing_code": "97110", "description": "x",
                        "negotiated_rates": [{
                            "provider_groups": [{"npi": [npi], "tin": {"type": "ein", "value": tin}}],
                            "negotiated_prices": [{"negotiated_type": "negotiated",
                                                   "negotiated_rate": rate,
                                                   "billing_class": "professional",
                                                   "service_code": ["11"],
                                                   "expiration_date": "9999-12-31"}]}]}]}

        a = cfg.inbox_dir / "pa_2026-11_in-network.json"
        a.write_text(json.dumps(mrf("Payer A", "811100001", "1811100001", 40.0)))
        ingest_file(cfg, st, a)                      # rollup runs: marker = A's stamp
        st.save_npis_bulk([{"npi": "1811100001", "org_name": "FIRST CLINIC",
                            "entity_type": "NPI-2", "state": "MO"}])
        st.rebuild_rollups()                         # names marker = the bulk stamp

        clock["t"] = dt.datetime(2026, 11, 1, 6, 20, tzinfo=dt.timezone.utc)  # 01:20 EST
        b = cfg.inbox_dir / "pb_2026-11_in-network.json"
        b.write_text(json.dumps(mrf("Payer B", "811100002", "1811100002", 50.0)))
        ingest_file(cfg, st, b, rebuild_rollups=False)
        stale = st.rollups_stale()
        st.update_rollups_incremental()
        with st.connect() as con:
            payers = sorted(r[0] for r in con.execute(
                "SELECT DISTINCT payer FROM rates_by_tin").fetchall())

        # an API-path name save 30 min after the bulk save (save_npi)
        clock["t"] = dt.datetime(2026, 11, 1, 6, 50, tzinfo=dt.timezone.utc)
        st.save_npi("1811100002", "SECOND CLINIC", "225100000X", "PT", "Town", "MO",
                    entity_type="NPI-2")
        st.refresh_directory_incremental()
        with st.connect() as con:
            row = con.execute("SELECT display_name FROM tin_directory "
                              "WHERE tin_value = '811100002'").fetchone()
            name = row[0] if row else None
        print(json.dumps({"stale": stale, "payers": payers, "name": name}))
''')


def test_rollup_and_names_markers_survive_the_fall_back_hour(tmp_path):
    drv = tmp_path / "drive.py"
    drv.write_text(DRIVER)
    env = {**os.environ, "TZ": "America/New_York", "MRFX_NO_AUTO_DOWNLOAD": "1"}
    r = subprocess.run([sys.executable, str(drv), str(tmp_path)], env=env,
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    import json
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["stale"] is True, "file B (finished 30 min after the marker) was invisible"
    assert out["payers"] == ["Payer A", "Payer B"]
    assert out["name"] == "SECOND CLINIC"


CONVERT = textwrap.dedent('''
    import json, sys
    from pathlib import Path

    if __name__ == "__main__":
        from mrfx.store import Store
        d = Path(sys.argv[1])
        st = Store(d / "store")
        with st.connect() as con:          # an OLDER store: local-time stamps, no flag
            con.execute("DELETE FROM meta WHERE key = 'timestamps_utc'")
            con.execute("INSERT INTO files (filename, status, finished_at) VALUES "
                        "('a.json', 'done', TIMESTAMP '2026-07-01 10:00:00'), "
                        "('b.json', 'done', TIMESTAMP '2026-12-01 10:00:00.5')")
            con.execute("INSERT OR REPLACE INTO meta VALUES "
                        "('rollup_covered_through', '2026-12-01 10:00:00.5'), "
                        "('directory_names_through', '2026-12-01 10:00:00')")
        st.close()
        st = Store(d / "store")            # reopen -> one-time conversion
        with st.connect() as con:
            got = con.execute("SELECT filename, finished_at::VARCHAR FROM files "
                              "ORDER BY 1").fetchall()
        out = {"files": got, "cov": st.meta_get("rollup_covered_through"),
               "names": st.meta_get("directory_names_through"),
               "flag": st.meta_get("timestamps_utc")}
        st.close()
        st = Store(d / "store")            # third open: nothing converts twice
        with st.connect() as con:
            out["again"] = con.execute("SELECT finished_at::VARCHAR FROM files "
                                       "ORDER BY filename").fetchall()
        print(json.dumps(out))
''')


def test_an_older_stores_local_times_are_converted_once(tmp_path):
    drv = tmp_path / "convert.py"
    drv.write_text(CONVERT)
    env = {**os.environ, "TZ": "America/Chicago", "MRFX_NO_AUTO_DOWNLOAD": "1"}
    r = subprocess.run([sys.executable, str(drv), str(tmp_path)], env=env,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]
    import json
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # Chicago is UTC-5 in July (CDT) and UTC-6 in December (CST)
    assert out["files"] == [["a.json", "2026-07-01 15:00:00"], ["b.json", "2026-12-01 16:00:00.5"]]
    assert out["cov"] == "2026-12-01 16:00:00.5"          # marker moved with its column
    assert out["names"] == "2026-11-30 20:00:00"          # mixed column: back 14 h
    assert out["flag"] == "1"
    assert out["again"] == [["2026-07-01 15:00:00"], ["2026-12-01 16:00:00.5"]]
