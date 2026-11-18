"""The SAME file uploaded twice 0.2 s apart (a double click), 0.69 MB so the
head_bytes defect of n1 is not involved."""
import time
from h import *
from fastapi.testclient import TestClient
from mrfx.api import create_app
from n9_upload import doc, settle

if __name__ == "__main__":
    cfg = mkcfg("n9d"); st = Store(cfg.store_dir)
    c = TestClient(create_app(cfg, st))
    body = doc(2000)
    ra = c.post("/api/upload", files={"file": ("acme_2026-05.json", body)}).json()
    time.sleep(0.2)
    rb = c.post("/api/upload", files={"file": ("acme_2026-05.json", body)}).json()
    time.sleep(2); settle(st, 2)
    print("first ->", ra["status"], ra["filename"], "| second ->", rb["status"], rb["filename"], "|", rb.get("note", "")[:90])
    print("files:", q(st, "SELECT filename, status, rows_emitted FROM files ORDER BY 1"))
    print("rates rows:", q(st, "SELECT count(*), count(DISTINCT source_file) FROM rates")[0], "(2000 from one file expected)")
    st.rebuild_rollups()
    print("rates_by_tin source_count for one TIN:", q(st, "SELECT source_count FROM rates_by_tin WHERE tin_value='100000001'"))
