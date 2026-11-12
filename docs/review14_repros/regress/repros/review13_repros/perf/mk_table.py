"""timings.csv -> markdown table rows for the report."""
import csv
import sys
rows = list(csv.DictReader(open(sys.argv[1])))
print("| # | feature (route) | view | payload | cold ms | warm ms | 3rd call (variant) ms | bytes | status |")
print("|---|---|---|---|---:|---:|---:|---:|---|")
for i, r in enumerate(rows, 1):
    pl = (r["payload"] or r["url"].split("?", 1)[-1] if "?" in r["url"] else r["payload"]) or "—"
    pl = pl.replace("|", "\\|")
    if len(pl) > 90:
        pl = pl[:87] + "…"
    var = f'{r["variant_ms"]} ({r["variant_status"]})' if r["variant_ms"] else "—"
    print(f'| {i} | `{r["method"]} {r["url"].split("?")[0]}` | {r["view"]} | {pl} | {r["cold_ms"]} | {r["warm_ms"]} | {var} | {r["bytes"]} | {r["status"]} |')
