"""Print one or more scenarios of a ui_*.json in detail. usage: show.py <json> <substr> [...]"""
import json
import sys

if __name__ == "__main__":
    d = json.load(open(sys.argv[1]))
    for sc in d["scenarios"]:
        if not any(s.lower() in sc["name"].lower() for s in sys.argv[2:]):
            continue
        print(f"== {sc['name']}  load {sc['load']}  typing {sc['typing_ms']}  first->render {sc['first_action_to_last_render_ms']}  last-key->render {sc['last_key_to_last_render_ms']}")
        for r in sc["requests"]:
            print(f"   {r['start']:>6} -> {str(r['end']):>6} ({str(r['dur']):>5} ms) {r['status']} {str(r['bytes']):>7}b {'ABORT ' if r['aborted'] else ''}{r['m']} {r['url'][:150]}")
        for k, v in sc["render"].items():
            print("   render", k, json.dumps(v)[:400])
        if sc["longtasks"]:
            print("   longtasks", sc["longtasks"])
