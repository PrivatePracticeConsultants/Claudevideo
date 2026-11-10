"""Does a file that needs the user (waiting for 'ingest anyway', queued, processing)
appear on the Files tab's first page once the store holds more than 500 files?"""
import shutil, sys
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")
import mrfx.api as api; api.TRUSTED_HOSTS.add("testserver")
from fastapi.testclient import TestClient
from mrfx.config import MrfxConfig
from mrfx.store import Store, FINISHED_NOW
R = Path("/tmp/mrfx_review9/startup/f1")
if __name__ == "__main__":
    shutil.rmtree(R, ignore_errors=True)
    c = MrfxConfig(inbox_dir=R/"in", processed_dir=R/"p", failed_dir=R/"f", store_dir=R/"store",
                   downloads_dir=R/"dl", entity_map_path=R/"em.yaml"); c.ensure_dirs()
    st = Store(c.store_dir)
    for i in range(600):
        st.upsert_file(f"done{i:04d}.json", status="done", rows_emitted=1, finished_at=FINISHED_NOW)
    st.upsert_file("BIG_needs_ok.json.gz", status="pending_confirmation", size_bytes=9_000_000_000)
    st.upsert_file("queued_one.json", status="queued")
    st.upsert_file("failed_old.json", status="failed", error="x", finished_at="2020-01-01 00:00:00")
    d = TestClient(api.create_app(c, st)).get("/api/files").json()
    names = [f["filename"] for f in d["files"]]
    print("total", d["total"], "shown", len(names))
    for n in ("BIG_needs_ok.json.gz", "queued_one.json", "failed_old.json"):
        print(f"  {n:24s} on first page: {n in names}")
