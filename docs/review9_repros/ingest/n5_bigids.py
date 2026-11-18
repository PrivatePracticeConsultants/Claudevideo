"""provider_group_id values above 2**53 (17-19 digit integers): clean_ref_id
goes through float(), so neighbouring ids become ONE id."""
from h import *
from mrfx.ingest import ingest_file
from mrfx.parser import clean_ref_id

A = [grp(["1111111111"], "111111111")]; B = [grp(["2222222222"], "222222222")]
I1, I2 = 1234567890123456789, 1234567890123456790     # e.g. TIN+NPI glued together

if __name__ == "__main__":
    print("clean_ref_id:", I1, "->", clean_ref_id(I1), "|", I2, "->", clean_ref_id(I2))
    for fast in (True, False):
        cfg = mkcfg(f"n5_{fast}", fast=fast); st = Store(cfg.store_dir)
        d = {**HDR, "provider_references": [{"provider_group_id": I1, "provider_groups": A},
                                            {"provider_group_id": I2, "provider_groups": B}],
             "in_network": [item("97110", refs=[I1], rate=50.0), item("97112", refs=[I2], rate=60.0)]}
        r = ingest_file(cfg, st, put(cfg, "acme_2026-05.json.gz", d, gz=True))
        print(f"fast_scan={fast}:", r)
        print("   stored:", q(st, "SELECT billing_code, negotiated_rate, tin_value, npi FROM rates ORDER BY 1"))
        print("   expected: 97110 -> TIN 111111111, 97112 -> TIN 222222222")
        print("   qa:", {k: v for k, v in json.loads(q(st, "SELECT qa FROM files")[0][0]).items() if k in ("bad_ref_ids", "missing_ref_ids", "rows")})
    cfg = mkcfg("n5_companion"); st = Store(cfg.store_dir)
    ingest_file(cfg, st, put(cfg, "acme_refs_2026-05.json", {**HDR, "provider_references": [
        {"provider_group_id": I1, "provider_groups": A}, {"provider_group_id": I2, "provider_groups": B}]}))
    print("standalone reference file: ids stored:", q(st, "SELECT ref_id, tin_value FROM provider_refs ORDER BY 1, 2"), "(2 distinct ids expected)")
