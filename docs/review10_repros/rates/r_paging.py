"""Explorer paging / totals on the 32M copy: pages are disjoint and complete under tie-heavy sorts,
the summary strip's row count equals the table's total, the export equals the screen's order."""
import csv, io, json, urllib.request, urllib.parse, hashlib, sys
B = "http://localhost:8710"
def get(u): return json.load(urllib.request.urlopen(B + u))
def raw(u): return urllib.request.urlopen(B + u).read()
PAY = urllib.parse.quote("Blue Cross and Blue Shield of Alabama")
IDENT = ("unit_id", "payer", "billing_code", "modifier_set", "billing_class", "service_code_set", "file_month", "is_dollar_rate", "tin_value", "negotiated_rate")

def main():
    views = [("tin latest AL 97110", "grain=tin&latest=1&state=AL&cpt=97110"),
             ("tin all months, state WY", "grain=tin&state=WY&cpt=97110,97530"),
             ("tin 97110 outliers hidden", "grain=tin&cpt=97110&hide_outliers=1"),
             ("entity latest payer+code", f"grain=entity&latest=1&cpt=97110&payer={PAY}"),
             ("npi latest payer+code AL", f"grain=npi&latest=1&cpt=97110&state=AL&payer={PAY}"),
             ("tin therapy only base prof 97530", "grain=tin&latest=1&therapy_only=1&modifier=base&billing_class=professional&cpt=97530")]
    for lab, q in views:
        for sort in ("payer", "negotiated_rate", "display_name", "npi_count"):
            seen, order, total = set(), [], None
            for page in range(1, 5):
                d = get(f"/api/rates?{q}&dollar_only=1&sort={sort}&dir=asc&page={page}&page_size=1000")
                total = d["total"]
                for r in d["rows"]:
                    k = tuple(str(r[c]) for c in IDENT)
                    order.append(k)
                    seen.add(k)
                if len(d["rows"]) < 1000: break
            s = get(f"/api/summary?{q}&dollar_only=1")
            dup = len(order) - len(seen)
            print(f"{lab:36s} sort {sort:15s} total {total:8d} summary n {s['n']:8d} {'OK ' if total == s['n'] else 'MISMATCH'} fetched {len(order):5d} duplicates across pages {dup}")
    # export order == screen order (first 1000 rows), TIN grain
    q = "grain=tin&latest=1&state=AL&cpt=97110&dollar_only=1&sort=negotiated_rate&dir=desc"
    scr = get(f"/api/rates?{q}&page=1&page_size=1000")["rows"]
    ex = list(csv.DictReader(io.StringIO(raw(f"/api/export.csv?{q}").decode("utf-8-sig"))))
    same = all(a["unit_id"] == b["unit_id"] and a["payer"] == b["payer"] and float(a["negotiated_rate"]) == float(b["negotiated_rate"])
               and a["modifier_set"].replace("|", ";") == b["modifiers"] for a, b in zip(scr, ex))
    print("export rows", len(ex), "screen total", get(f"/api/rates?{q}&page=1&page_size=1")["total"], "| first 1000 in the same order:", same)

if __name__ == "__main__":
    main()
