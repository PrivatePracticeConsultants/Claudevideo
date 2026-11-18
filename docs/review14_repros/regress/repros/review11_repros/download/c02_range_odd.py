"""Case 2 — resume against odd servers: 416 on resume; content CHANGED between
attempts (same size / different size; with and without a validator); 206 with
a wrong total. For every variant: is a mixed-origin file ever marked fetched,
and what is the sha on disk vs the two truths?"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, doc, gz, mk_cfg, quiet_logs, sha, sha_file, row, rows  # noqa: E402

from mrfx.fetch import DownloadError, add_urls, download, run_queue  # noqa: E402


def run(name, body_v1, body_v2, rules, swap_after_first=True, via_queue=False, **cfgkw):
    """Serve v1 for the first (cut) request, then v2 for every later one."""
    cfg, store = mk_cfg("c02_" + name, **cfgkw)
    srv = Srv({"/f.json.gz": body_v1}, {"/f.json.gz": rules})
    state = {"swapped": False}
    orig_rules = dict(rules)

    def swap(h, body, rng, nth):
        if swap_after_first and nth >= 2 and not state["swapped"]:
            srv.files["/f.json.gz"] = body_v2
            if "etag2" in orig_rules:
                srv.rules["/f.json.gz"]["etag"] = orig_rules["etag2"]
            state["swapped"] = True
        return False
    srv.behave["/f.json.gz"] = swap
    url = srv.base + "/f.json.gz"
    dest = cfg.downloads_dir / ("f.json.gz" if not via_queue else "x")
    t0 = time.time()
    try:
        if via_queue:
            add_urls(store, [url])
            run_queue(cfg, store, drain=True)
            r = row(store, url)
            out = f"row status={r['status']} kind={r['kind']} rows={r['rows_emitted']} err={(r['error'] or '')[:120]!r}"
            from mrfx.fetch import filename_for
            dest = cfg.downloads_dir / filename_for(url)
        else:
            got = download(cfg, url, dest)
            out = f"download OK sha==v1 {got[0] == sha(body_v1)} sha==v2 {got[0] == sha(body_v2)}"
    except DownloadError as e:
        out = f"DownloadError retryable={e.retryable}: {str(e)[:140]}"
    except Exception as e:  # noqa: BLE001
        out = f"UNCAUGHT {type(e).__name__}: {str(e)[:140]}"
    finally:
        srv.close()
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists():
        s = sha_file(dest)
        on_disk = f"dest {dest.stat().st_size} B sha==v1 {s == sha(body_v1)} sha==v2 {s == sha(body_v2)}"
    elif part.exists():
        on_disk = f"part {part.stat().st_size} B"
    else:
        on_disk = "nothing on disk"
    print(f"[{name}] {time.time() - t0:5.1f}s v1={len(body_v1)} B v2={len(body_v2)} B | {out}")
    print(f"    requests: {srv.describe('/f.json.gz')}")
    print(f"    on disk: {on_disk}")
    return cfg, store


if __name__ == "__main__":
    quiet_logs()
    v1 = gz(doc(n=300, pad_mb=1.2, rate_mid=40.0), level=0)      # stored blocks: same size
    v2_same = gz(doc(n=300, pad_mb=1.2, rate_mid=47.0), level=0)   # one rate differs
    print("same size v1/v2:", len(v1) == len(v2_same), len(v1), len(v2_same))
    v2_diff = gz(doc(n=300, pad_mb=1.5, rate_mid=47.0), level=0)
    cut = 400 << 10

    # 416 on resume: server shrinks the file so the .part offset is past the end
    tiny = gz(doc(n=50))
    run("416_on_resume", v1, tiny, {"cut_at": [cut]})

    # content changed, STRONG ETag: If-Range must make the server send 200 (restart)
    # our server ignores If-Range (like many) -> what does the client do?
    run("changed_strong_etag_server_ignores_ifrange", v1, v2_same,
        {"cut_at": [cut], "etag": '"v1"', "etag2": '"v2"'})

    # content changed, no validator at all, same size -> splice undetectable?
    run("changed_no_validator_same_size", v1, v2_same, {"cut_at": [cut]})
    # ... different size: total changes between attempts
    run("changed_no_validator_diff_size", v1, v2_diff, {"cut_at": [cut]})

    # an RFC-correct server: If-Range mismatch -> 200 full body of v2
    def ifrange_server(h, body, rng, nth):
        if nth >= 2:
            h.server  # noqa: B018
        return False
    cfg, store = mk_cfg("c02_ifrange_rfc")
    srv = Srv({"/f.json.gz": v1}, {"/f.json.gz": {"cut_at": [cut], "etag": '"v1"'}})

    def rfc(h, body, rng, nth):
        if nth == 1:
            return False
        srv.files["/f.json.gz"] = v2_same
        if h.headers.get("If-Range") and h.headers.get("If-Range") != '"v2"':
            # validator mismatch: full 200 of the CURRENT content
            h.send_response(200)
            h.send_header("ETag", '"v2"')
            h.send_header("Content-Length", str(len(v2_same)))
            h.end_headers()
            h.wfile.write(v2_same)
            h._rec("/f.json.gz", "GET", rng, 200, len(v2_same))
            return True
        return False
    srv.behave["/f.json.gz"] = rfc
    url = srv.base + "/f.json.gz"
    dest = cfg.downloads_dir / "f.json.gz"
    try:
        got = download(cfg, url, dest)
        print(f"[ifrange_rfc] OK sha==v2 {got[0] == sha(v2_same)}  requests {srv.describe('/f.json.gz')}")
    finally:
        srv.close()

    # the queue path for the same-size splice: does the row end 'done' with wrong rows?
    run("changed_no_validator_same_size_queue", v1, v2_same, {"cut_at": [cut]}, via_queue=True)
