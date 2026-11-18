"""Round 9 2.4: attention rows first on /api/files with > 500 files."""
import shutil, sys
from pathlib import Path
import mrfx.api as api; api.TRUSTED_HOSTS.add("testserver")
from fastapi.testclient import TestClient
from mrfx.config import MrfxConfig
from mrfx.store import Store, FINISHED_NOW
if __name__ == "__main__":
    R = Path(sys.argv[1]); shutil.rmtree(R, ignore_errors=True)
    c = MrfxConfig(inbox_dir=R/"in", processed_dir=R/"p", failed_dir=R/"f", store_dir=R/"store",
                   downloads_dir=R/"dl", entity_map_path=R/"em.yaml"); c.ensure_dirs()
    st = Store(c.store_dir)
    for i in range(600):
        st.upsert_file(f"done{i:04d}.json", status="done", rows_emitted=1, finished_at=FINISHED_NOW)
    st.upsert_file("BIG_needs_ok.json.gz", status="pending_confirmation", size_bytes=9_000_000_000)
    st.upsert_file("queued_one.json", status="queued")
    st.upsert_file("failed_old.json", status="failed", error="x", finished_at="2020-01-01 00:00:00")
    st.upsert_file("held_choice.json", status="needs_choice")
    for i in range(30):
        st.upsert_file(f"skipped{i}.json", status="skipped", finished_at=FINISHED_NOW)
    cl = TestClient(api.create_app(c, st))
    d = cl.get("/api/files").json()
    names = [f["filename"] for f in d["files"]]
    print("total", d["total"], "shown", len(names), "attention", d["attention"], "limit", d["limit"])
    print("first 6:", names[:6])
    for n in ("BIG_needs_ok.json.gz", "queued_one.json", "failed_old.json", "held_choice.json"):
        print(f"  {n:24s} on first page: {n in names} idx {names.index(n) if n in names else None}")
    r = cl.get("/api/files?offset=99999999999999999999"); print("offset overflow ->", r.status_code)
    r = cl.get("/api/files?limit=10000000"); print("limit 1e7 ->", r.status_code, r.json().get("limit"))
