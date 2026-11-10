"""Scenario 2b: (1) 'Keep both' chosen, then a restart before the load thread ran:
is the held file ever loaded, and can the user get out? (2) the /confirm route's
upsert('queued') lands AFTER the ingest thread it just started: can 'done' be
overwritten by 'queued' for good?"""
import threading
from h import *
from fastapi.testclient import TestClient
from mrfx.ingest import scan_inbox, resolve_choice, NEEDS_CHOICE
import mrfx.api as api
import mrfx.ingest as ing


def app_client(cfg, store):
    app = api.create_app(cfg, store) if hasattr(api, "create_app") else api.make_app(cfg, store)
    return TestClient(app)


if __name__ == "__main__":
    cfg = mkcfg("s2b"); store = Store(cfg.store_dir)
    put(cfg, "acme_2026-05.json.gz", doc(n=3), gz=True); scan_inbox(cfg, store)
    put(cfg, "acme_2026-05.json.gz", doc(n=5, seed=9), gz=True); scan_inbox(cfg, store)
    held = [f for f, s, *_ in files_rows(store) if s == NEEDS_CHOICE][0]
    # the user clicks Keep both; the server dies before the ingest thread runs (resolve only)
    r = resolve_choice(cfg, store, held, "keep")
    print("after keep + restart: row", store.file_status(held)["status"], "| file at", where_is(cfg, held))
    store2 = Store(cfg.store_dir); store2.recover_stuck_files()
    for i in range(3):
        print("  scan", i, "->", scan_inbox(cfg, store2))
    print("  row:", store2.file_status(held)["status"], "| rates:", rate_counts(store2))
    c = app_client(cfg, store2)
    print("  DELETE /api/files/{held}:", c.delete(f"/api/files/{held}").status_code, c.delete(f"/api/files/{held}").json())
    print("  POST choose keep again:", c.post(f"/api/files/{held}/choose", json={"action": "keep"}).status_code,
          c.post(f"/api/files/{held}/choose", json={"action": "keep"}).json())
    print("  POST confirm:", c.post(f"/api/files/{held}/confirm").status_code)
    print("  -> the user has no way out: file in .held, row 'queued' forever (forget refuses 'queued')")

    print("\n== confirm route ordering: thread first, upsert('queued') second")
    cfg2 = mkcfg("s2b_confirm", confirm_over_gb=0.000001); store3 = Store(cfg2.store_dir)
    stuck = 0
    N = 30
    for i in range(N):
        name = f"big_{i}.json.gz"
        put(cfg2, name, doc(n=2, seed=i), gz=True)
        scan_inbox(cfg2, store3)
        assert store3.file_status(name)["status"] == "pending_confirmation", store3.file_status(name)["status"]
        c2 = app_client(cfg2, store3)
        c2.post(f"/api/files/{name}/confirm")
        # wait for the background ingest to finish (claim released)
        t0 = time.time()
        while (ing.ingest_in_progress(name) or store3.file_status(name)["status"] == "processing") and time.time() - t0 < 30:
            time.sleep(0.05)
        time.sleep(0.2)
        st = store3.file_status(name)
        if st["status"] != "done":
            stuck += 1
            print(f"  {name}: final status {st['status']!r} rows_emitted={st['rows_emitted']} where={where_is(cfg2, name)} "
                  f"rates={rate_counts(store3).get(name)}")
    print(f"  {stuck}/{N} confirmed files ended with a status other than 'done' after their ingest finished")
