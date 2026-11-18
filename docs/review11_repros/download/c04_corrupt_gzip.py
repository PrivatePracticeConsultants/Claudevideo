"""Case 4 — a CDN serves a bad object with a consistent Content-Length:
  crc_bad     last 4 bytes of the gzip (the CRC) flipped — the deflate stream is fine
  mid_bad     16 bytes flipped in the middle of the deflate stream
  truncated   the last 20 KB missing (no trailer at all), Content-Length = short size
  zip_trunc   a .zip missing its central directory
For each: the download verdict, trailer_proves recorded, the queue row's final
state and message, rows that landed in the store, and whether the bad download
is kept (a retry would fetch the same bad object again)."""
import io
import json
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, doc, gz, mk_cfg, quiet_logs, row, q, dl_files  # noqa: E402

from mrfx.fetch import add_urls, filename_for, run_queue, DOWNLOAD_IDENTITY  # noqa: E402


def run(name, fname, body):
    cfg, store = mk_cfg("c04_" + name)
    srv = Srv({"/" + fname: body}, {"/" + fname: {}})
    url = srv.base + "/" + fname
    t0 = time.time()
    try:
        add_urls(store, [url])
        run_queue(cfg, store, drain=True)
    finally:
        srv.close()
    r = row(store, url)
    files = q(store, "SELECT filename, status, rows_emitted FROM files")
    nrates = q(store, "SELECT count(*) FROM rates")[0][0]
    ident = q(store, "SELECT filename, trailer_proves FROM content_identity") if q(
        store, "SELECT count(*) FROM information_schema.tables WHERE table_name='content_identity'")[0][0] else "n/a"
    print(f"[{name}] {time.time() - t0:.1f}s row status={r['status']} kind={r['kind']} rows_emitted={r['rows_emitted']} "
          f"err={(r['error'] or '')[:170]!r}")
    print(f"    files table: {files} | rates rows: {nrates} | identity: {ident}")
    print(f"    requests: {len(srv.hits('/' + fname))} | downloads dir: {dl_files(cfg)} | "
          f"DOWNLOAD_IDENTITY leak: {[k for k in DOWNLOAD_IDENTITY if name in k]}")


if __name__ == "__main__":
    quiet_logs()
    good = gz(doc(n=300, pad_mb=1.5))
    bad_crc = good[:-8] + bytes(b ^ 0xFF for b in good[-8:-4]) + good[-4:]
    mid = len(good) // 2
    bad_mid = good[:mid] + bytes(b ^ 0x55 for b in good[mid:mid + 16]) + good[mid + 16:]
    trunc = good[:-(20 << 10)]
    run("crc_bad", "rates.json.gz", bad_crc)
    run("mid_bad", "rates.json.gz", bad_mid)
    run("truncated", "rates.json.gz", trunc)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("rates.json", doc(n=300, pad_mb=1.5))
    zbytes = buf.getvalue()
    run("zip_good", "rates.zip", zbytes)
    run("zip_trunc", "rates.zip", zbytes[:-200])
