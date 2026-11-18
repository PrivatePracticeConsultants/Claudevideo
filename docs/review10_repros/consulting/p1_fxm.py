from fxm import *  # noqa
from surf import surfaces
import sys, json
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    for subj, pay in (("ACME REHAB", "Alpha Health"), ("BIG GROUP", "Alpha Health"), ("B REHAB", "Alpha Health")):
        print("=====", subj)
        o = surfaces(c, subj, MO, pay)
        print("fee:", json.dumps(o.get("fee"))[:1800])
    print(cfg.store_dir)
