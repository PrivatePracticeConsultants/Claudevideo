"""A long report printed as PDF: (1) Stop is not honoured while Chromium
prints (cancel() only interrupts DuckDB connections); a second PDF waits on
pdf._LOCK behind it; (2) a report big enough to pass Playwright's 30 s
set_content timeout answers a bare 500, not the sentence."""
import secrets, tempfile, threading, time
from pathlib import Path
import h
from fastapi.testclient import TestClient

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t08_"))
    cfg = h.mkcfg(root); store = h.Store(cfg.store_dir)
    from tests.mrfx.test_review13 import _market
    T, _ = _market(cfg, store, "Alpha Health", "41000", [80, 90, 100, 110, 120])
    store.rebuild_rollups()
    import mrfx.api as api
    N = {"rows": 40000}
    def big_report(cfg_, store_, bench, opp):
        rows = "".join(f"<tr><td>{i}</td><td>CLINIC {i} REHAB</td><td>${i*1.01:,.2f}</td></tr>"
                       for i in range(N["rows"]))
        return f"<html><body><table>{rows}</table></body></html>"
    api.render_pitch_report = big_report        # stands in for a state-wide report
    c = TestClient(api.create_app(cfg, store), raise_server_exceptions=False)
    body = {"subject": T[0], "market": {"month": "latest", "state": "MO"}}
    tok = secrets.token_hex(12); res = {}
    def run():
        t = time.time()
        r = c.post("/api/report/pitch?format=pdf", json=body, headers={"X-Mrfx-Progress": tok})
        res["a"] = (r.status_code, round(time.time() - t, 1), r.headers.get("content-type"))
    def second():
        t = time.time()
        r = c.post("/api/report/pitch?format=pdf", json={**body})
        res["b"] = (r.status_code, round(time.time() - t, 1))
    ta = threading.Thread(target=run); ta.start()
    for _ in range(100):
        s = c.get(f"/api/progress/{tok}")
        if s.status_code == 200 and s.json()["stage"] == "Printing the PDF":
            break
        time.sleep(0.1)
    tb = threading.Thread(target=second); tb.start()
    t_stop = time.time()
    print("Stop pressed while 'Printing the PDF':", c.delete(f"/api/progress/{tok}").json())
    ta.join()
    print("stopped PDF answered (code, seconds since its start, type):", res["a"],
          "| seconds after Stop:", round(time.time() - t_stop, 1))
    tb.join()
    print("a second PDF requested at the same moment answered (code, s):", res["b"])
    # (2) bigger than the 30 s set_content budget
    N["rows"] = 150000
    t = time.time()
    r = c.post("/api/report/pitch?format=pdf", json=body)
    print(f"150k-row report as PDF: HTTP {r.status_code} after {time.time()-t:.0f} s:", r.text[:160])

if __name__ == "__main__":
    main()
