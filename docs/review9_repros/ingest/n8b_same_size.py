"""A corrected file re-dropped under the same name with the SAME byte size
(one rate changed). (a) the loaded twin predates fingerprints; (b) twin has a
fingerprint, file is 20 MB and the change sits between the sampled regions;
(c) follow-up to n8: the lost new file is dropped again."""
import os, time
from h import *
import mrfx.ingest as ing
from mrfx.ingest import scan_inbox
from mrfx.fetch import _win_offsets, _WIN

def doc_bytes(n, changed=None):
    its = []
    for i in range(n):
        rate = 55.0 if i == changed else 40.0
        its.append(item("97110", groups=[grp([str(1000000000 + i)], f"{100000000+i}")], rate=rate))
    return json.dumps({**HDR, "in_network": its}).encode()

def case(label, n, drop_fp):
    cfg = mkcfg(f"n8b_{label}"); st = Store(cfg.store_dir)
    v1 = doc_bytes(n)
    size = len(v1)
    # change ONE rate (same length) at an offset outside the first 15 MB, the
    # last 1 MB and the 8 sampled 64 KB windows
    needle = b'"negotiated_rate": 40.0'
    wins = _win_offsets(size)
    pos = size // 2 if not drop_fp else 0
    while True:
        pos = v1.index(needle, pos + 1)
        if drop_fp or (pos > (15 << 20) and pos + 40 < size - (1 << 20)
                       and not any(o - 40 <= pos < o + _WIN for o in wins)):
            break
    v2 = v1[:pos] + b'"negotiated_rate": 55.0' + v1[pos + len(needle):]
    assert len(v2) == size and v2 != v1
    (cfg.inbox_dir / "acme_2026-05.json").write_bytes(v1)
    print(f"[{label}] {size/1e6:.1f} MB; first load ->", [(x["file"], x["status"], x.get("rows")) for x in scan_inbox(cfg, st)])
    if drop_fp:
        with st.connect() as con:
            con.execute("DELETE FROM content_fingerprints")      # a file loaded by a build before 0.7.0
    (cfg.inbox_dir / "acme_2026-05.json").write_bytes(v2)
    r = scan_inbox(cfg, st)
    print("   corrected file (same size, one rate 40.0 -> 55.0) dropped -> scan result:", [(x["file"], x["status"], x.get("rows")) for x in r])
    print("   rows at 55.0 in the store:", q(st, "SELECT count(*) FROM rates WHERE negotiated_rate = 55.0")[0][0], "(1 expected)")
    print("   inbox:", [x.name for x in cfg.inbox_dir.iterdir()], "| processed copy is the",
          "NEW file" if (cfg.processed_dir / "acme_2026-05.json").read_bytes() == v2 else "old file")

if __name__ == "__main__":
    case("no_fingerprint", 3000, True)
    case("sampled_20MB", 62000, False)
