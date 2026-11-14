"""The bar never changes a result byte: each output route with and without a
token (dates normalised; ZIP members compared, not the ZIP's timestamps)."""
import io, re, secrets, tempfile, zipfile, hashlib
from pathlib import Path
import h
from fastapi.testclient import TestClient

def norm(b, ctype):
    if b[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(b))
        return b"|".join(n.encode() + b"=" + hashlib.sha1(re.sub(rb"\d{4}-\d{2}-\d{2}[ T_]\d{2}[:\d]*", b"D", z.read(n))).digest()
                         for n in sorted(re.sub(r"\d{4}-\d{2}-\d{2}_\d{4}", "D", n) and n for n in z.namelist())), True
    return re.sub(rb"(Generated|generated|as of|Built)[^<\n]*", b"", re.sub(rb"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", b"D", b)), False

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t09_"))
    cfg = h.mkcfg(root); store = h.Store(cfg.store_dir)
    from tests.mrfx.test_review13 import _market
    T, _ = _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80])
    _market(cfg, store, "Beta Health", "71000", [42, 52, 62, 72, 82])
    _market(cfg, store, "Alpha Health", "71000", [41, 51, 61, 71, 81], day="2026-07-01")
    store.rebuild_rollups()
    c = TestClient(h._api.create_app(cfg, store))
    M = {"month": "latest", "state": "MO", "payers": []}
    S = {"subject": T[0], "market": M}
    calls = [
        ("GET", "/api/rates?grain=tin&limit=50&payer=Alpha%20Health", None),
        ("GET", "/api/summary?grain=tin&payer=Alpha%20Health", None),
        ("GET", "/api/overview", None),
        ("GET", "/api/code/97110?grain=tin", None),
        ("GET", f"/api/entity/tin/{T[0]}", None),
        ("GET", "/api/export.csv?grain=tin&payer=Alpha%20Health&cpt=97110", None),
        ("GET", "/api/export/outreach.zip?grain=tin&payer=Alpha%20Health&cpt=97110", None),
        ("POST", "/api/report/org-bundle.zip", S),
        ("POST", "/api/schedule/fee.csv", S),
        ("POST", "/api/roster.csv", {"payer": "Alpha Health", "market": M}),
        ("POST", "/api/report/pitch", S),
        ("POST", "/api/report/negotiation", S),
        ("POST", "/api/report/dossier", S),
        ("POST", "/api/report/market", {"state": "MO", "code": "97110"}),
        ("POST", "/api/benchmark/market", S),
        ("POST", "/api/contract-gaps", S),
        ("GET", "/api/clients/digest", None),
        ("POST", "/api/leaderboard", {"market": M}),
        ("POST", "/api/trajectory", {"market": M}),
    ]
    for m, url, body in calls:
        outs = []
        for tok in (None, secrets.token_hex(12)):
            hdr = {"X-Mrfx-Progress": tok} if tok else {}
            r = c.request(m, url, json=body, headers=hdr)
            outs.append((r.status_code, *norm(r.content, r.headers.get("content-type"))))
        same = outs[0] == outs[1]
        print(f"{'SAME' if same else 'DIFF'} {outs[0][0]}/{outs[1][0]} {m} {url[:60]}")
        if not same:
            a, b = outs[0][1], outs[1][1]
            i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), None)
            print("    first diff at", i, a[max(0,(i or 0)-60):(i or 0)+60], "\n vs", b[max(0,(i or 0)-60):(i or 0)+60])

if __name__ == "__main__":
    main()
