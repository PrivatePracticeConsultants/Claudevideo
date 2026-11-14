"""Is the NPI-grain page stable while another heavy request runs? (run1 of the timing saw one differing body)"""
import hashlib, json, threading, time, urllib.request, urllib.error
B = "http://localhost:8710"
U = "/api/rates?dollar_only=1&sort=negotiated_rate&dir=desc&page=1&page_size=100&grain=npi&latest=1&state=AL&cpt=97110"
HEAVY = "/api/rates?dollar_only=1&grain=npi&latest=1&page_size=5"

def fetch(u):
    try:
        return urllib.request.urlopen(B + u, timeout=300).read()
    except urllib.error.HTTPError as e:
        return b"HTTP %d " % e.code + e.read()

def main():
    base = fetch(U); ref = json.loads(base)
    print("quiet: total", ref["total"], hashlib.sha256(base).hexdigest()[:10])
    for rnd in range(3):
        t = threading.Thread(target=fetch, args=(HEAVY,)); t.start(); time.sleep(0.5)
        for i in range(4):
            b = fetch(U)
            if b != base:
                try:
                    d = json.loads(b)
                    diffs = [(j, k, x[k], y[k]) for j, (x, y) in enumerate(zip(ref["rows"], d["rows"])) for k in x if x[k] != y[k]]
                    print(f"round {rnd} call {i}: DIFFERENT body; total {d.get('total')} vs {ref['total']}; row diffs {len(diffs)} {diffs[:4]}")
                except Exception:
                    print(f"round {rnd} call {i}: DIFFERENT:", b[:300])
            else:
                print(f"round {rnd} call {i}: same")
        t.join()

if __name__ == "__main__":
    main()
