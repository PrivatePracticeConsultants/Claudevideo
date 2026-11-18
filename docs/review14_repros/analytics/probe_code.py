import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent)); import h
if __name__ == "__main__":
    d = h.ROOT / "abcd"
    s = h.Serve(d / "config/mrfx.yaml", 8471, d / "probe.log"); s.wait_ready()
    try:
        c = h.api(8471, "/api/code/97110")
        print(list(c.keys()))
        for k, v in c.items():
            if not isinstance(v, list): print(k, json.dumps(v, default=str)[:400])
            else: print(k, 'list', len(v), json.dumps(v[:1], default=str)[:400])
        f = h.api(8471, "/api/files"); print(type(f), list(f.keys()) if isinstance(f, dict) else None)
        rows = f.get('files') or []
        print(json.dumps(rows[:1], default=str)[:1500])
        st = h.api(8471, "/api/stats"); print(list(st.keys()))
    finally:
        s.stop()
