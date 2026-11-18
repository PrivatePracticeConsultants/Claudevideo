"""Per scenario: when the request(s) for the FINAL text left the browser and how long each took,
across runs. usage: req_in_ui.py <json> [...]"""
import json
import sys

if __name__ == "__main__":
    runs = [json.load(open(p)) for p in sys.argv[1:]]
    print("| scenario | " + " | ".join(r["label"] for r in runs) + " |")
    print("|---|" + "---|" * len(runs))
    for i, sc in enumerate(runs[0]["scenarios"]):
        cells = []
        for r in runs:
            s = r["scenarios"][i]
            fin = {}
            for q in s["requests"]:
                kind = q["url"].split("?")[0].replace("/api/", "")
                if kind in ("rates", "summary", "benchmark/subjects", "benchmark/market") or kind.startswith(("code/", "trend", "entity/")):
                    fin[kind.split("/")[0] if not kind.startswith("benchmark") else kind] = q   # the last one of each kind
            wait = round(min((q["start"] for q in fin.values()), default=0) - s["typing_ms"]) if fin else None
            parts = ", ".join(f"{k} {q['dur']} ms{'' if q['status'] == 200 else ' [' + str(q['status']) + ']'}" for k, q in fin.items())
            bad = sum(1 for q in s["requests"] if q["status"] and q["status"] >= 400)
            cells.append(f"sent {wait} ms after last key; {parts}; total {s['first_action_to_last_render_ms']} ms; {s['n_requests']} req{', ' + str(bad) + ' refused' if bad else ''}; load {s['load'].split(',')[0]}")
        print(f"| {sc['name']} | " + " | ".join(cells) + " |")
