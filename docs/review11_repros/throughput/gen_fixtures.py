"""Build the review-11 throughput queue: N synthetic in-network .json.gz files
of a chosen compressed size (target CPT items with inline provider_groups,
mixed with non-target items so the bulk skip has work), plus real payer
files copied read-only. Streams through gzip; nothing raw lands on disk.

usage: gen_fixtures.py <http_root> [n_synth=24] [min_mb=5] [max_mb=60]
"""
import gzip
import json
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.config import MrfxConfig  # noqa: E402

REAL_DIR = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/real")
REAL = [
    "bcbsms_2026-10_320_33B0_in-network-rates_01_of_10.json.gz",      # 25 MB gz / 0.48 GB
    "uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-New-York_Third-Party-Administrator_Empire_CSP-377-A351_in-network-rates.json.gz",  # 35 MB / 0.62 GB
    "wy_2026-11_710_26B0_in-network-rates.json.gz",                   # 119 MB / 1.11 GB
]
NON_TARGET = ["99213", "99214", "27447", "29881", "70450", "80053", "36415",
              "93000", "71046", "45378", "99283", "20610", "97597", "64483"]


def item(rng: random.Random, code: str, n_groups: int) -> bytes:
    groups = []
    for _ in range(n_groups):
        tin = f"{rng.randint(10, 99)}-{rng.randint(1000000, 9999999)}"
        npis = [1000000000 + rng.randint(0, 999999999) for _ in range(rng.randint(1, 6))]
        prices = [{"negotiated_type": "negotiated",
                   "negotiated_rate": round(rng.uniform(18, 160), 2),
                   "expiration_date": "9999-12-31",
                   "service_code": ["11"] if rng.random() < 0.7 else ["11", "12"],
                   "billing_class": "professional"}
                  for _ in range(rng.randint(1, 4))]
        if rng.random() < 0.3:
            prices[0]["billing_code_modifier"] = [rng.choice(["GP", "CQ", "KX", "59"])]
        groups.append({"provider_groups": [{"npi": npis, "tin": {"type": "ein", "value": tin}}],
                       "negotiated_prices": prices})
    return json.dumps({
        "negotiation_arrangement": "ffs", "name": f"svc {code}",
        "billing_code_type": "CPT", "billing_code_type_version": "2026",
        "billing_code": code, "description": f"service {code} description text",
        "negotiated_rates": groups,
    }).encode()


def build(out: Path, target_gz_bytes: int, seed: int, codes: list[str]) -> None:
    rng = random.Random(seed)
    head = (b'{"reporting_entity_name": "Synthetic Health Plan %d", '
            b'"reporting_entity_type": "health insurance issuer", '
            b'"last_updated_on": "2026-10-01", "version": "1.0.0", "in_network": [' % (seed % 7 + 7 * (seed // 1000)))
    with open(out, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as gz:
        gz.write(head)
        first = True
        while raw.tell() < target_gz_bytes:
            for _ in range(200):
                if rng.random() < 0.3:
                    code, n = rng.choice(codes), rng.randint(8, 40)
                else:
                    code, n = rng.choice(NON_TARGET), rng.randint(8, 40)
                if not first:
                    gz.write(b",\n")
                first = False
                gz.write(item(rng, code, n))
        gz.write(b"]}\n")


if __name__ == "__main__":
    root = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    lo = float(sys.argv[3]) if len(sys.argv) > 3 else 5
    hi = float(sys.argv[4]) if len(sys.argv) > 4 else 60
    prefix = sys.argv[5] if len(sys.argv) > 5 else "synth"
    seed_base = int(sys.argv[6]) if len(sys.argv) > 6 else 100
    plan_base = int(sys.argv[7]) if len(sys.argv) > 7 else 0
    with_real = len(sys.argv) <= 5
    root.mkdir(parents=True, exist_ok=True)
    codes = sorted(MrfxConfig().code_set)
    rng = random.Random(11)
    total = 0
    for i in range(n):
        mb = lo + (hi - lo) * (i / max(1, n - 1)) ** 2   # skewed small, like a real index
        out = root / f"{prefix}_plan{plan_base + i % 7}_2026-10_in-network-rates_{i + 1:02d}_of_{n:02d}.json.gz"
        if out.exists() and abs(out.stat().st_size - mb * 1e6) < 2e6:
            total += out.stat().st_size
            continue
        build(out, int(mb * 1e6), seed=seed_base + i, codes=codes)
        total += out.stat().st_size
        print(f"{out.name}: {out.stat().st_size / 1e6:.1f} MB", flush=True)
    for name in (REAL if with_real else []):
        dst = root / name
        if not dst.exists():
            shutil.copyfile(REAL_DIR / name, dst)
            dst.chmod(0o444)
        total += dst.stat().st_size
        print(f"{name[:60]}: {dst.stat().st_size / 1e6:.1f} MB (real, read-only copy)")
    print(f"total {total / 1e6:.0f} MB in {root}")
