"""Build the tiny UI store at outputs/ui_tiny (+ mrfx.yaml on port 8651) and dump a url-queue snapshot
with one row per status (from a second scratch store) to urls_snapshot.json."""
import sys, json, shutil
from pathlib import Path
import fx
OUT = Path("/tmp/mrfx_review9/outputs")

if __name__ == "__main__":
    d = OUT / "ui_tiny"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir()
    _mk = fx.mk
    fx.mk = lambda tmp=None: _mk(d)
    import fxm
    from fxm import doc, NPIS, B, C, D, E
    fxm.mk = fx.mk
    z04 = {"97110": {B: 50, C: 60}, "97140": {B: 30, C: 40}}
    z06 = {"97110": {B: 45, C: 54}, "97140": {B: 27, C: 36}}
    e04 = {"97110": {B: 40, C: 40}}
    e06 = {"97110": {D: 20, E: 20}}
    cfg, store = fxm.build([("zeta_2026-04.json", doc("Zeta Health", "2026-04-01", z04, NPIS)),
                            ("zeta_2026-06.json", doc("Zeta Health", "2026-06-01", z06, NPIS)),
                            ("eps_2026-04.json", doc("Epsilon Health", "2026-04-01", e04, NPIS)),
                            ("eps_2026-06.json", doc("Epsilon Health", "2026-06-01", e06, NPIS))])
    store.close()
    (d / "mrfx.yaml").write_text(
        f"inbox_dir: {d}/inbox\nprocessed_dir: {d}/processed\nfailed_dir: {d}/failed\nstore_dir: {d}/store\n"
        f"downloads_dir: {d}/downloads\nentity_map_path: {d}/entity_map.yaml\nport: 8651\nenrichment:\n  mode: \"off\"\n")
    print("built", d)
    # ---- queue snapshot from a separate scratch store
    cfg2, st = _mk()
    ids = {}
    for k in ("queued", "downloading", "fetched", "ingesting", "done", "failed", "skipped", "oversize", "paused", "parked", "waiting", "expanding", "biglane"):
        ids[k] = st.enqueue_url(f"https://files.example.org/mrf/{k}_2026-06_in-network-rates.json.gz", k)
    st.update_url(ids["downloading"], status="downloading", bytes_done=40_000_000, bytes_total=120_000_000)
    st.update_url(ids["fetched"], status="fetched", bytes_done=5_000_000, bytes_total=5_000_000, filename="fetched_2026-06.json.gz")
    st.update_url(ids["ingesting"], status="ingesting", filename="ingesting_2026-06.json.gz")
    st.update_url(ids["done"], status="done", filename="done_2026-06.json.gz", kind="in_network")
    st.update_url(ids["failed"], status="failed", error="the server answered 403 (forbidden) — the link has expired; get a fresh one from the payer's page")
    st.update_url(ids["skipped"], status="skipped", error="allowed-amounts file: not negotiated rates", kind="allowed_amounts")
    st.update_url(ids["oversize"], status="oversize", bytes_total=30_000_000_000, error="30.0 GB is over the 5 GB confirm limit")
    st.update_url(ids["expanding"], status="expanding", kind="toc")
    try:
        st.pause_url(ids["paused"], 2.0, "stalled: no data for 20 minutes")
    except Exception as e:
        print("pause_url", repr(e)); st.update_url(ids["paused"], status="paused", error="stalled")
    try:
        st.update_url(ids["parked"], status="downloading")
        st.park_url(ids["parked"], 10, "slow server: set aside for 10 minutes")
    except Exception as e:
        print("park_url", repr(e))
    st.park_waiting(ids["waiting"], ids["downloading"], 10, "waiting: an identical-looking copy is already downloading")
    try:
        st.update_url(ids["biglane"], big_lane=True)
    except Exception as e:
        print("big_lane", repr(e))
    import mrfx.api as api
    from fastapi.testclient import TestClient
    c = TestClient(api.create_app(cfg2, st))
    snap = c.get("/api/urls").json()
    (OUT / "urls_snapshot.json").write_text(json.dumps(snap, default=str))
    print("counts", snap["counts"], "settings", snap["settings"])
    for u in snap["urls"]:
        print("  ", u["id"], u["status"], u.get("kind"), {k: u[k] for k in ("park_count", "waiting_on", "big_lane", "paused_until", "error") if k in u and u[k]})
