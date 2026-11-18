"""Stop pressed while an Explorer 'Export + methodology' ZIP is Compressing
(the long stage the bar shows): the compress loop never checks the stop, the
route then answers 409, and the finished ZIP is never sent and never deleted
(its BackgroundTask belonged to the discarded response). Live server, scratch store."""
import json, secrets, threading, time, urllib.request, urllib.error
from pathlib import Path
BASE = "http://localhost:8791"
EXP = Path(__file__).parent / "live/store/duckdb_tmp/exports"

def get(url, method="GET"):
    req = urllib.request.Request(BASE + url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def listing():
    return sorted((p.name, p.stat().st_size) for p in EXP.glob("*")) if EXP.exists() else []

def main():
    print("exports folder before:", listing())
    tok = secrets.token_hex(12); res = {}
    q = "/api/export.zip?payer=Blue%20Cross%20Blue%20Shield%20of%20Wyoming&grain=npi&view=explorer&full=true&progress=" + tok
    t = threading.Thread(target=lambda: res.update(r=get(q))); t.start()
    seen = []
    for _ in range(600):
        s, b = get(f"/api/progress/{tok}")
        if s == 200:
            j = json.loads(b)
            if not seen or seen[-1] != (j["stage"], j["state"]):
                seen.append((j["stage"], j["state"]))
            if j["stage"] == "Compressing":
                print("DELETE during Compressing:", get(f"/api/progress/{tok}", "DELETE"), "pct was", j["pct"])
                break
        time.sleep(0.01)
    t.join()
    print("stages seen:", seen)
    print("export answered:", res["r"][0], res["r"][1][:80])
    time.sleep(1)
    print("exports folder after:", listing())

if __name__ == "__main__":
    main()
