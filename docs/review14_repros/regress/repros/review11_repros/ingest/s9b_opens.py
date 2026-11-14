"""Scenario 9b: which call sites open the source file during one inbox ingest, and how many
bytes each handle reads (builtins.open wrapper; in-process path, fast scan, a real 35 MB file)."""
import builtins, traceback, io
from h import *
from mrfx.ingest import ingest_file

REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
SRC = REAL / "real/uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-New-York_Third-Party-Administrator_Empire_CSP-377-A351_in-network-rates.json.gz"
_open = builtins.open
sites = []


class Counting(io.FileIO):
    def __init__(self, *a, site, **k):
        super().__init__(*a, **k); self.n = 0; self.site = site; sites.append(self)
    def read(self, size=-1):
        b = super().read(size); self.n += len(b); return b
    def readinto(self, buf):
        n = super().readinto(buf); self.n += n or 0; return n


def spy(file, mode="r", *a, **k):
    if isinstance(file, (str, Path)) and str(file).endswith(SRC.name) and "r" in mode and "b" in mode:
        frames = [f for f in traceback.extract_stack()[:-1] if "/mrfx/" in f.filename]
        site = " <- ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames[-3:])
        raw = Counting(file, "rb", site=site)
        return io.BufferedReader(raw) if "b" in mode else raw
    return _open(file, mode, *a, **k)


if __name__ == "__main__":
    cfg = mkcfg("s9b"); st = Store(cfg.store_dir)
    shutil.copy(SRC, cfg.inbox_dir / SRC.name); size = SRC.stat().st_size
    builtins.open = spy
    try:
        r = ingest_file(cfg, st, cfg.inbox_dir / SRC.name)
    finally:
        builtins.open = _open
    print("result:", r["status"], r.get("rows"), "| file %.1f MB" % (size / 1e6))
    tot = 0
    for s in sites:
        print(f"  {s.n/1e6:7.1f} MB  {s.site}")
        tot += s.n
    print(f"  total {tot/1e6:.1f} MB = {tot/size:.2f}x the file, {len(sites)} opens")
    shutil.rmtree(BASE / "s9b", ignore_errors=True)
