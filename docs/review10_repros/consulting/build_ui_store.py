"""Persistent hand-workable store for the Chromium pass (fxf + multi-state ACME + referrals + roster + a client).
Writes /tmp/mrfx_review10/consulting/ui.yaml (port 8721)."""
from fxf import *  # noqa
import shutil, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    NPIS[S2] = ["1" + S2, "1610000092", "1610000093"]
    root = Path("/tmp/mrfx_review10/consulting/uistore")
    shutil.rmtree(root, ignore_errors=True)
    import fx
    _mk = fx.mk
    cfg, store = build_full.__globals__["build"].__globals__["mk"](root) if False else (None, None)
    # build() calls mk() with a temp dir; build there, then note the path in the yaml
    cfg, store = build_full()
    for n in NPIS[S2]:
        name(store, n, "ACME REHAB", "KS", zipc="67202", city="Wichita")
    for t in (K1, K2, K3):
        name(store, "1" + t, LABEL[t], "KS", zipc="66101", city="Kansas City")
    DOCS = ["1901234561", "1811223340", "1722334459"]
    tmp = Path(cfg.store_dir).parent
    p = tmp / "OrderReferring_2026-07-17.csv"
    p.write_text("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n" + "".join(f"{d},DOC{i},ANNA,Y,N,N,N,N\n" for i, d in enumerate(DOCS)) + f"1{S1},ACME,REHAB,Y,N,N,N,N\n")
    from mrfx.medicare import import_orf_roster, import_shared_patients
    import_orf_roster(store, p)
    p = tmp / "docgraph_2022.csv"
    p.write_text("from_npi,to_npi,patient_count,transaction_count,average_day_wait,std_day_wait\n"
                 + "".join(f"{d},1{S1},{70 + i},{500 + i},12.5,3.1\n" for i, d in enumerate(DOCS))
                 + f"{DOCS[0]},1{B},55,200,10,2\n" + f"{DOCS[0]},1610000092,90,300,10,2\n")
    import_shared_patients(store, p)
    store.rebuild_rollups()
    d = tmp
    Path("/tmp/mrfx_review10/consulting/ui.yaml").write_text(
        f"inbox_dir: {d}/inbox\nprocessed_dir: {d}/processed\nfailed_dir: {d}/failed\nstore_dir: {d}/store\n"
        f"downloads_dir: {d}/downloads\nentity_map_path: {d}/entity_map.yaml\nport: 8721\nenrichment:\n  mode: \"off\"\n")
    print("store at", d)
