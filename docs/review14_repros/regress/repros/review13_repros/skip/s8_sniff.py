"""S8: what is skipped by KIND, and which decisions read the NAME vs the
CONTENT. preflight() on constructed files; expand_blobs_listing /
extract_links_from_text / expand_toc on constructed listings."""
import gzip
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/user/Claudevideo")
import gen  # noqa: E402
from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.fetch import expand_blobs_listing, expand_toc, extract_links_from_text  # noqa: E402
from mrfx.sniff import preflight  # noqa: E402


def pf_of(cfg, tmp, name, data):
    p = tmp / name
    p.write_bytes(data)
    pf = preflight(p, cfg, None)
    return pf.file_type, pf.verdict, [m[:90] for m in pf.messages]


def main():
    logging.basicConfig(level=logging.ERROR)
    tmp = Path(tempfile.mkdtemp(prefix="s8_", dir=str(Path(__file__).parent / "scratch")))
    cfg = MrfxConfig(inbox_dir=tmp / "i", processed_dir=tmp / "p", failed_dir=tmp / "f", store_dir=tmp / "s",
                     downloads_dir=tmp / "d", entity_map_path=tmp / "em.yaml")
    raw = gen.doc(n_items=20, filler=10)
    d = json.loads(raw)
    print("== preflight: CONTENT decides (file name is irrelevant)")
    print("  in_network named 'allowed-amounts.json' ->", pf_of(cfg, tmp, "acme_allowed-amounts.json", raw))
    print("  in_network named 'prescription-drug.json.gz' ->", pf_of(cfg, tmp, "x_prescription-drug_ndc.json.gz", gzip.compress(raw)))
    print("  in_network with a UTF-8 BOM ->", pf_of(cfg, tmp, "bom.json", b"\xef\xbb\xbf" + raw))
    print("  in_network with a BOM inside gzip ->", pf_of(cfg, tmp, "bom.json.gz", gzip.compress(b"\xef\xbb\xbf" + raw)))
    big_refs = {"reporting_entity_name": "Acme", "last_updated_on": "2026-05-01", "version": "2.0.0",
                "provider_references": [{"provider_group_id": i, "provider_groups": [{"npi": [1000000000 + i], "tin": {"type": "ein", "value": "430000001"}}]} for i in range(20000)],
                "in_network": d["in_network"]}
    print("  provider_references (>1 MB) BEFORE in_network ->", pf_of(cfg, tmp, "refs_first.json", json.dumps(big_refs).encode()))
    small_refs = dict(big_refs); small_refs["provider_references"] = big_refs["provider_references"][:3]
    print("  small provider_references before in_network ->", pf_of(cfg, tmp, "refs_small.json", json.dumps(small_refs).encode()))
    only_refs = {k: v for k, v in small_refs.items() if k != "in_network"}
    print("  provider_references only (no in_network) ->", pf_of(cfg, tmp, "refs_only.json", json.dumps(only_refs).encode()))
    print("  JSON ARRAY index ->", pf_of(cfg, tmp, "array.json", json.dumps([{"location": "https://x/a.json.gz"}]).encode()))
    print("  JSON array of in_network items (no object wrapper) ->", pf_of(cfg, tmp, "array2.json", json.dumps(d["in_network"]).encode()))
    print("  out_of_network content ->", pf_of(cfg, tmp, "oon.json", json.dumps({"reporting_entity_name": "A", "out_of_network": []}).encode()))
    print("  empty file ->", pf_of(cfg, tmp, "empty.json", b""))
    print("  UTF-16 ->", pf_of(cfg, tmp, "u16.json", raw.decode().encode("utf-16")))
    print("  zip: manifest FIRST, rates second ->", pf_of(cfg, tmp, "z.zip", gen.zip_of({"00_manifest.json": b'{"files": 1}', "rates.json": raw})))
    print("  zip: rates named .txt only ->", pf_of(cfg, tmp, "z2.zip", gen.zip_of({"rates.txt": raw})))
    print("  zip: two rate members, the SMALLER is the target payer ->", pf_of(cfg, tmp, "z3.zip", gen.zip_of({"a_big_other.json": gen.doc(n_items=50, filler=100, payer="Other"), "b_small.json": raw})), "(largest member is read; the other is IGNORED, log only)")
    print("  TOC content ->", pf_of(cfg, tmp, "toc.json", json.dumps({"reporting_entity_name": "A", "reporting_structure": [{"in_network_files": [{"location": "https://x/a.json.gz"}]}]}).encode()))
    print("  blobs content ->", pf_of(cfg, tmp, "blobs.json", json.dumps({"blobs": [{"name": "a", "downloadUrl": "https://x/a.json.gz"}]}).encode()))

    print("\n== expand_blobs_listing: the NAME decides (UHC/Optum listings)")
    listing = {"blobs": [
        {"name": "2026-11-01_Acme_in-network-rates.json.gz", "downloadUrl": "https://x/1.json.gz"},
        {"name": "2026-11-01_Hallowed-Health_in-network-rates.json.gz", "downloadUrl": "https://x/2.json.gz"},
        {"name": "2026-11-01_Allowed-Benefits-TPA_in-network-rates.json.gz", "downloadUrl": "https://x/3.json.gz"},
        {"name": "2026-11-01_Acme_allowed-amounts.json.gz", "downloadUrl": "https://x/4.json.gz"},
        {"name": "2026-11-01_Acme-NDC_in-network-rates.json.gz", "downloadUrl": "https://x/5.json.gz"},
        {"name": "2026-11-01_NDC-Corp_in-network-rates.json.gz", "downloadUrl": "https://x/6.json.gz"},
        {"name": "2026-11-01_Acme_prescription-drugs.json.gz", "downloadUrl": "https://x/7.json.gz"},
        {"name": "2026-11-01_Acme_index.json", "downloadUrl": "https://x/8.json"},
        {"name": "2026-11-01_Acme_in-network-rates.json.gz", "downloadUrl": "https://x/1.json.gz?sig=again"},
    ]}
    p = tmp / "listing.json"; p.write_bytes(json.dumps(listing).encode())
    urls, trunc = expand_blobs_listing(p, 100)
    print("  queued:", urls)
    print("  NOT queued (silently, log only): ", sorted({f"https://x/{i}.json.gz" for i in range(1, 8)} - set(urls)))

    print("\n== expand_toc: no name filter at all (content of the child decides later)")
    toc = {"reporting_entity_name": "A", "reporting_structure": [{"in_network_files": [
        {"location": "https://x/allowed-amounts.json.gz"}, {"location": "https://x/ndc_in-network.json.gz"},
        {"location": "/relative/r.json.gz"}, {"location": "ftp://x/r.json.gz"}, {"location": ""},
        {"location": "https://x/r.json.gz?sig=1"}, {"location": "https://x/r.json.gz?sig=2"}]}]}
    p = tmp / "toc.json"; p.write_bytes(json.dumps(toc).encode())
    print("  ", expand_toc(p, 100, base_url="https://x/toc.json"))

    print("\n== extract_links_from_text: name sets PRIORITY only (dropped only past the cap)")
    html = " ".join(f'<a href="https://x/{n}">{n}</a>' for n in
                    ["allowed-amounts.json.gz", "in-network-rates.json.gz", "rx_formulary.json.gz", "other.json.gz", "drug-free-workplace_in-network.json.gz"])
    print("  cap 100:", extract_links_from_text(html, "https://x/", 100))
    print("  cap 2  :", extract_links_from_text(html, "https://x/", 2))


if __name__ == "__main__":
    main()
