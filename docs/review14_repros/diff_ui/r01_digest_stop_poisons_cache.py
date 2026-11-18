"""Stop on the Clients digest: does a stopped run leave a poisoned cache that
the NEXT (un-stopped) digest — and the monthly packets — serve?"""
import tempfile, threading, time, secrets
from pathlib import Path
import h
from fastapi.testclient import TestClient

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t01_"))
    cfg, store, A = h.small_store(root, n=20)
    from mrfx.clients import add_client
    add_client(store, A[0]); add_client(store, A[1]); add_client(store, A[2])
    import mrfx.benchmark as bm, mrfx.clients as cl
    orig = bm.resolve_subject_tins
    def slow(store_, subject, *a, **k):
        time.sleep(0.8)          # stands in for a slow statement on a big store
        return orig(store_, subject, *a, **k)
    cl.resolve_subject_tins = slow
    app = h._api.create_app(cfg, store)
    c = TestClient(app)
    tok = secrets.token_hex(12)
    res = {}
    def run():
        r = c.get("/api/clients/digest", headers={"X-Mrfx-Progress": tok})
        res["code"], res["body"] = r.status_code, r.text[:200]
    t = threading.Thread(target=run); t.start()
    time.sleep(0.4)
    print("DELETE ->", c.delete(f"/api/progress/{tok}").json())
    t.join()
    print("stopped run answered:", res["code"], res["body"])
    cl.resolve_subject_tins = orig
    r = c.get("/api/clients/digest")   # a plain, un-stopped request
    print("next plain digest:", r.status_code)
    for row in r.json()["clients"]:
        print("  ", row["subject"], "error=", row.get("error"), "gaps=", row.get("gaps"))
    print("cache key unchanged; store._digest_cache rows with error:",
          sum(1 for x in store._digest_cache[1]["clients"] if x.get("error")))
    print("monthly packets built after the stop:")
    packets_after(cfg, store)


def packets_after(cfg, store):
    from mrfx.packets import build_all_packets
    out = build_all_packets(cfg, store, out_root=Path(cfg.store_dir).parent / "packets")
    import glob
    for f in sorted(glob.glob(out["dir"] + "/**/*.html", recursive=True)):
        txt = Path(f).read_text()
        if "stopped" in txt:
            i = txt.find("stopped")
            print("  packet file mentions 'stopped':", Path(f).relative_to(out["dir"]),
                  "...", txt[max(0, i-120):i+20].replace("\n", " "))

if __name__ == "__main__":
    main()
