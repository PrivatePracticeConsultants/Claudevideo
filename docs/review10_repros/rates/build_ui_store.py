"""Build the c2 fixture as a servable store under /tmp/mrfx_review10/rates/ui (port 8711)."""
import sys, shutil
from pathlib import Path
sys.path.insert(0, "/tmp/mrfx_review10/rates/repros")
import fx, c2_hypo

def main():
    root = Path("/tmp/mrfx_review10/rates/ui")
    shutil.rmtree(root, ignore_errors=True)
    orig = fx.mk
    c2_hypo.mk = lambda tmp=None: orig(root)
    cfg, store = c2_hypo.build()
    (root / "mrfx.yaml").write_text(
        f"inbox_dir: {root}/inbox\nprocessed_dir: {root}/processed\nfailed_dir: {root}/failed\n"
        f"store_dir: {root}/store\ndownloads_dir: {root}/downloads\nentity_map_path: {root}/entity_map.yaml\n"
        "port: 8711\nenrichment:\n  mode: \"off\"\n")
    print("built", root)

if __name__ == "__main__":
    main()
