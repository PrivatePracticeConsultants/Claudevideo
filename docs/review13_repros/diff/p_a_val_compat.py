"""(A) A .val written by 0.7.5 (no "kind") and its .part, resumed by 0.7.6:
Last-Modified-only server, quoted-ETag server, unquoted-ETag server. Expect one
206 and sha == truth each time; plus _val_kind on shapes."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
sys.path.insert(0, "/home/user/Claudevideo/tests/mrfx")  # noqa
from mrfx import fetch  # noqa: E402
from mrfx.config import MrfxConfig  # noqa: E402
from tests.mrfx.test_review11 import Srv, ranged, sha  # noqa: E402


def main():
    from mrfx.fetch import _val_kind
    print("== _val_kind on 0.7.5-shaped records ==")
    for v in ('"abc"', 'W/"abc"', "Tue, 03 Nov 2026 10:00:00 GMT", "5d8c72a5edda8d6a:0",
              "1731000000", "20261103", "abc", ""):
        print(f"  {v!r:40} -> {_val_kind({'validator': v})!r}")
    root = Path(tempfile.mkdtemp(prefix="valcompat_", dir=Path(__file__).parent))
    cfg = MrfxConfig(inbox_dir=root / "inbox", processed_dir=root / "processed",
                     failed_dir=root / "failed", store_dir=root / "store",
                     downloads_dir=root / "downloads", entity_map_path=root / "em.yaml")
    cfg.ensure_dirs()
    cfg.download_retries = 2
    body = os.urandom(700_000)
    lm = "Tue, 03 Nov 2026 10:00:00 GMT"
    cases = {
        "last-modified only": (lambda req: ranged(body, req, lm=lm), lm),
        "quoted etag": (lambda req: ranged(body, req, etag='"v1"'), '"v1"'),
        "unquoted etag": (lambda req: ranged(body, req, etag="5d8c72a5edda8d6a:0"), "5d8c72a5edda8d6a:0"),
        "unquoted etag + last-modified": (
            lambda req: ranged(body, req, etag="5d8c72a5edda8d6a:0", lm=lm), "5d8c72a5edda8d6a:0"),
    }
    bad = 0
    for name, (route, validator) in cases.items():
        s = Srv({"/f.bin": route})
        dest = cfg.downloads_dir / f"{name.replace(' ', '_')}.bin"
        part = Path(str(dest) + ".part")
        part.write_bytes(body[:300_000])
        # exactly what 0.7.5's _write_val wrote: no "kind"
        Path(str(part) + ".val").write_text(json.dumps({"validator": validator, "total": len(body)}))
        got, _ = fetch.download(cfg, s.base + "/f.bin", dest)
        hits = [(h[1], h[2]) for h in s.hits("/f.bin")]
        ok = got == sha(body) and hits == [(206, "bytes=300000-")]
        bad += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] {name}: sha {'==' if got == sha(body) else '!='} truth, requests {hits}")
        s.close()
    print("FAILS:", bad)
    return bad


if __name__ == "__main__":
    sys.exit(main())
