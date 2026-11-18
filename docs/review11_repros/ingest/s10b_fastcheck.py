"""mrfx fastcheck on 3 real files (fast vs classic parser: identical rows/QA, timings)."""
import subprocess, shutil
from h import *
REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
picks = [REAL / "real2/nd_hmhs_2026-10-27_000005200_in-network-rates_01_of_10.json.gz",
         REAL / "real/uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-New-York_Third-Party-Administrator_Empire_CSP-377-A351_in-network-rates.json.gz",
         REAL / "real/bcbsms_2026-10_320_33B0_in-network-rates_01_of_10.json.gz"]
if __name__ == "__main__":
    tmp = BASE / "s10_real"; shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
    for p in picks: shutil.copy(p, tmp / p.name)
    cfgy = tmp / "mrfx.yaml"
    cfgy.write_text(f"inbox_dir: {tmp}/inbox\nprocessed_dir: {tmp}/processed\nfailed_dir: {tmp}/failed\nstore_dir: {tmp}/store\n"
                    f"downloads_dir: {tmp}/downloads\nentity_map_path: {tmp}/entity_map.yaml\nenrichment:\n  mode: \"off\"\n")
    r = subprocess.run(["/home/user/Claudevideo/.venv/bin/mrfx", "--config", str(cfgy), "fastcheck", *[str(tmp / p.name) for p in picks]],
                       capture_output=True, text=True, timeout=1800)
    print(r.stdout[-4000:]); print("stderr tail:", r.stderr[-600:])
    shutil.rmtree(tmp, ignore_errors=True)
