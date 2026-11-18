"""Bases for E/F/G/I: real payer files read by the real ingest (in-process),
analytics NOT built (base8_unbuilt) and 7-of-8 built (base7_built), plus the
uninterrupted reference build of all 8 (ref8)."""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

S = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
FILES = [S / "real" / n for n in (
    "bcbsms_2026-10_320_33B0_in-network-rates_01_of_10.json.gz",
    "bcbsms_2026-10_460_70J0_in-network-rates_03_of_13.json.gz",
    "moda_ModaTXPriceTrans_20261101.json.gz",
    "tufts_2026-11-01_TuftsHealthPublicPlans_DirectSubsidizedIndividual_in-network-rates.zip",
    "wy_2026-10-27_000005300_in-network-rates_05_of_09.json.gz",
)] + [S / "real2" / n for n in (
    "wv_hmhs_2026-10-27_000000647_in-network-rates_01_of_02.json.gz",
    "security_networx_PRG000049.zip",
)]
LATE = S / "real" / "uhc_2026-11-01_HEALTHSCOPE-BENEFITS_Third-Party-Administrator_SHO_U1_3342_in-network-rates.json.gz"


def ingest_into(d: Path, port: int, files, build: bool):
    from mrfx.config import load_mrfx_config
    from mrfx.ingest import ingest_file
    from mrfx.store import Store
    cfgp = h.write_cfg(d, port)
    cfg = load_mrfx_config(cfgp)
    st = Store(cfg.store_dir, cfg.duckdb_memory_gb)
    out = []
    for f in files:
        dst = cfg.inbox_dir / f.name
        shutil.copy(f, dst)
        t = time.time()
        r = ingest_file(cfg, st, dst, rebuild_rollups=False)
        out.append({"f": f.name, "status": r.get("status"), "rows": r.get("rows_emitted"),
                    "s": round(time.time() - t, 1)})
        print(out[-1], flush=True)
    if build:
        t = time.time()
        st.rebuild_rollups()
        out.append({"build_s": round(time.time() - t, 1)})
    st.close() if hasattr(st, "close") else None
    return out


def main():
    root = h.ROOT / "bases"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    log = {}
    log["base8_unbuilt"] = ingest_into(root / "base8_unbuilt", 8480, FILES + [LATE], build=False)
    # the reference: the SAME 8 parts, one uninterrupted build
    shutil.copytree(root / "base8_unbuilt", root / "ref8")
    h.write_cfg(root / "ref8", 8481)
    from mrfx.store import Store
    st = Store(root / "ref8" / "store", 2)
    t = time.time()
    st.rebuild_rollups()
    log["ref8_build_s"] = round(time.time() - t, 1)
    del st
    log["base7_built"] = ingest_into(root / "base7_built", 8482, FILES, build=True)
    h.save("bases.json", log)
    print(json.dumps(log, indent=1))


if __name__ == "__main__":
    main()
