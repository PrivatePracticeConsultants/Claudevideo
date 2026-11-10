"""Side-by-side table of ui_*.json runs. usage: table.py <json> [<json> ...]
Prints, per scenario: requests fired, and first-action->last-render ms per run (with load)."""
import json
import sys

if __name__ == "__main__":
    runs = [json.load(open(p)) for p in sys.argv[1:]]
    names = [s["name"] for s in runs[0]["scenarios"]]
    print("| scenario | " + " | ".join(r["label"] for r in runs) + " |")
    print("|---|" + "---|" * len(runs))
    for i, n in enumerate(names):
        cells = []
        for r in runs:
            s = r["scenarios"][i]
            t = s["first_action_to_last_render_ms"]
            lk = s["last_key_to_last_render_ms"]
            bad = sum(1 for q in s["requests"] if q["status"] and q["status"] >= 400)
            cells.append(f"{t} ms (after last key {lk}); {s['n_requests']} req{', ' + str(bad) + ' err' if bad else ''}; load {s['load'].split(',')[0]}"
                         + ("" if s["ok"] else " NOT SETTLED"))
        print(f"| {n} | " + " | ".join(cells) + " |")
