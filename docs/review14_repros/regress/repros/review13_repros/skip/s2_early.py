"""S2: the early duplicate check (early_dedup_mb=1 so it runs after 1 MB on
~3-4 MB files). Twin = the same bytes under a second host (localhost vs
127.0.0.1 = two dedup keys). For each case: was the copy skipped early, how
many bytes did the server send for it, and how many files / rates ended up
loaded."""
import logging
import socketserver
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen  # noqa: E402
import h  # noqa: E402

BODIES: dict = {}


def serve(path):
    def fn(hh, head, entry):
        body = BODIES[path]
        rng = h.parse_range(hh, len(body))
        hdr = {"ETag": '"e-%s"' % path.strip("/")}
        enc = ENC.get(path)
        if enc:
            hdr["Content-Encoding"] = enc
        if rng:
            a, b = rng
            hdr["Content-Range"] = f"bytes {a}-{b}/{len(body)}"
            h.send(hh, entry, 206, hdr, body[a:b + 1], head=head)
        else:
            h.send(hh, entry, 200, hdr, body, head=head)
    return fn


ENC: dict = {}


def bytes_for(path_prefix, since):
    return sum(w for m, p, hd, w, s in h.LOG[since:] if p.split("?")[0] == path_prefix)


def run_case(name, first, second, early_mb=1, note=""):
    """first/second: (path, bytes). first served on 127.0.0.1, second on localhost."""
    cfg = h.scratch_cfg("s2_" + name, parallel_downloads=1, parallel_ingests=1, early_dedup_mb=early_mb)
    store = h.open_store(cfg)
    h.ROUTES.clear(); BODIES.clear(); h.LOG.clear()
    # distinct paths (/p1/x, /p2/x): two dedup keys on one server, two bodies
    first = ("/p1" + first[0], first[1])
    second = ("/p2" + second[0], second[1])
    for p, b in (first, second):
        BODIES[p] = b
        h.ROUTES[p] = serve(p)
    httpd, base = h.start_server()
    port = base.rsplit(":", 1)[1]
    from mrfx import fetch
    fetch.add_urls(store, [f"http://127.0.0.1:{port}{first[0]}"])
    h.drain(cfg, store, 120)
    n = len(h.LOG)
    fetch.add_urls(store, [f"http://127.0.0.1:{port}{second[0]}"])
    h.drain(cfg, store, 120)
    rows = h.queue_rows(store, "id, status, kind, error, bytes_done, bytes_total, duplicate_of")
    with store.connect() as con:
        files = con.execute("SELECT filename, status, rows_emitted FROM files ORDER BY filename").fetchall()
        rates = con.execute("SELECT count(*) FROM rates").fetchone()[0]
        vals = con.execute("SELECT list(DISTINCT negotiated_rate ORDER BY negotiated_rate) FROM rates").fetchone()[0]
        fps = con.execute("SELECT filename, magic, trailer_proves, full_sha IS NOT NULL FROM content_fingerprints ORDER BY filename").fetchall()
    sent = bytes_for(second[0], n)
    print(f"\n== {name} {note}")
    print(f"   sizes: first {len(first[1]):,} B, second {len(second[1]):,} B; server sent for the second: {sent:,} B")
    for r in rows:
        print(f"   row {r['id']}: {r['status']}/{r['kind']} dup_of={r['duplicate_of']} err={str(r['error'])[:150]!r}")
    print(f"   files: {files}")
    print(f"   fingerprints (magic, trailer_proves, has_sha): {fps}")
    print(f"   rates loaded: {rates}; distinct rate values: {vals}")
    httpd.shutdown()
    return rows, files, rates, sent


def main():
    logging.basicConfig(level=logging.ERROR)
    socketserver.BaseServer.handle_error = lambda *a: None
    raw = gen.doc(n_items=1500, filler=2000)
    big = gen.gz0(raw)
    head = 1 << 20
    print(f"raw {len(raw):,} B, stored-gzip {len(big):,} B; sampled windows for that size: "
          f"{gen.windows(len(big), head)}")

    # A: a true twin (same bytes, second host) — expected: skipped after ~1 MB
    run_case("A_twin_gz", ("/a.json.gz", big), ("/a.json.gz", big), note="true twin, single-member gz")

    # B: single-member gz, one rate changed in the middle, SAME size (stored blocks)
    raw2, at = gen.change_rate_once(raw, 1_600_000)
    big2 = gen.gz0(raw2)
    assert len(big2) == len(big)
    d = gen.diff_ranges(big, big2)
    print(f"\nB: byte ranges that differ: {d}; overlap with sampled ranges: "
          f"{gen.overlaps(d, gen.windows(len(big), head))}")
    run_case("B_same_size_mid_change", ("/b.json.gz", big), ("/b.json.gz", big2),
             note="same size, one rate changed at %d; the trailer CRC is in the tail" % at)

    # C: MULTI-member gz (bgzip-like): the change is in member 1, whose trailer is mid-file
    wins = gen.windows(len(big), head)[2:]
    # pick a split just after the change, outside every window
    split = None
    for cand in range(at + 20_000, len(raw) - 200_000, 7919):
        if all(not (lo - 64 <= cand + 18 <= hi + 64) for lo, hi in wins):
            split = cand
            break
    m1, m2 = raw[:split], raw[split:]
    multi = gen.gz0(m1) + gen.gz0(m2)
    multi2 = gen.gz0(raw2[:split]) + gen.gz0(m2)
    assert len(multi) == len(multi2)
    d = gen.diff_ranges(multi, multi2)
    print(f"\nC: multi-member; split at {split}; differing ranges {d}; overlap with sampled: "
          f"{gen.overlaps(d, gen.windows(len(multi), head))}")
    run_case("C_multimember", ("/c.json.gz", multi), ("/c.json.gz", multi2),
             note="two-member gz; copy differs in member 1 only (same size, same tail)")
    run_case("C2_multimember_true_twin", ("/c2.json.gz", multi), ("/c2.json.gz", multi),
             note="two-member gz; TRUE twin — is it still skipped early, or downloaded in full?")

    # D: plain .json, differs only in the middle (equal size, head, tail, windows)
    d = gen.diff_ranges(raw, raw2)
    print(f"\nD: plain json differing ranges {d}; overlap with sampled: {gen.overlaps(d, gen.windows(len(raw), head))}")
    run_case("D_plain_json", ("/d.json", raw), ("/d.json", raw2), note="plain json: must never skip early")
    run_case("D2_plain_json_true_twin", ("/d2.json", raw), ("/d2.json", raw), note="plain json true twin: full download then sha")

    # E: Content-Encoding: gzip on a .json name (raw gz bytes kept) vs the same bytes as .json.gz
    ENC["/p1/e.json"] = "gzip"
    run_case("E_content_encoding_gzip", ("/e.json", big), ("/e.json.gz", big),
             note="first served as Content-Encoding: gzip under a .json name")
    ENC.clear()
    # F: a .json.gz the server transparently DECOMPRESSES (plain body, no header) vs the raw gz
    run_case("F_transparent_decode", ("/f.json.gz", big), ("/f.json.gz", raw),
             note="second host serves the SAME content inflated (plain JSON body) — different bytes")

    # G: zip with two members (manifest first, rates second) and its true twin
    z = gen.zip_of({"manifest.json": b'{"files": 1}', "rates.json": raw})
    run_case("G_zip_two_members", ("/g.zip", z), ("/g.zip", z), note="zip, 2 members, true twin")
    z2 = gen.zip_of({"manifest.json": b'{"files": 1}', "rates.json": raw2})
    print(f"\nG2: zip sizes {len(z)} vs {len(z2)}; diff ranges {gen.diff_ranges(z, z2)[:3]}…")
    run_case("G2_zip_mid_change", ("/g2.zip", z), ("/g2.zip", z2), note="zip, one rate changed")


if __name__ == "__main__":
    main()
