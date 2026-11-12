"""s4 case C with the content-claim calls traced (which names claim / release what)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h, s4_claims

if __name__ == "__main__":
    import mrfx.ingest as ing
    from mrfx import fetch
    from mrfx.store import Store
    for meth in ("claim_content_ingest", "claim_inbox_content", "release_content_claim", "release_inbox_claim"):
        orig = getattr(Store, meth)
        def wrap(self, *a, _o=orig, _m=meth, **k):
            r = _o(self, *a, **k)
            print(f"   TRACE {_m}{tuple(str(x)[:12] for x in a)} -> {r!r:.60} | claims now {[(c[:8], n) for c, n in self._content_claims.items()]}", flush=True)
            return r
        setattr(Store, meth, wrap)
    real = ing.run_in_network_parse
    st = {"n": 0}
    def flaky(*a, **k):
        st["n"] += 1
        if st["n"] == 1:
            raise RuntimeError("injected parser failure on the first loader")
        return real(*a, **k)
    ing.run_in_network_parse = flaky
    cfg, store, httpd, port = s4_claims.setup("C_trace")
    fetch.add_urls(store, [f"http://127.0.0.1:{port}/tpa.json.gz", f"http://localhost:{port}/tpa.json.gz"])
    h.drain(cfg, store, 30)
    s4_claims.report(store, "C traced")
    httpd.shutdown()
