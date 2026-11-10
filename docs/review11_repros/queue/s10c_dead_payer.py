"""Dead payer: this month's AND last month's dated index 404. Does the daily
grace retry revive last month's failed row too (2 GETs/day), and does it end?"""
import datetime as dt
from h import *  # noqa
from mrfx.fetch import monthly_refresh_tick

if __name__ == "__main__":
    root, cfg, store = world("s10c")
    today = dt.date.today()
    first = today.replace(day=1)
    prev_m = (first - dt.timedelta(days=1)).replace(day=1).isoformat()
    prev2 = ((first - dt.timedelta(days=1)).replace(day=1) - dt.timedelta(days=1)).replace(day=1).isoformat()
    srv = Server({f"/dead/{prev2}_index.json": toc_bytes([]) , "/x.json.gz": rate_file_bytes("x")})
    srv.routes[f"/dead/{prev2}_index.json"] = toc_bytes([srv.url("/x.json.gz")])
    ks = root / "ks.yaml"
    ks.write_text(f"sources:\n  - name: Dead\n    states: [MO]\n    url: {srv.base}/dead/{{FIRST_OF_MONTH}}_index.json\n    kind: toc\n    queueable: true\n")
    cfg.known_sources_path = ks
    add_urls(store, [f"{srv.base}/dead/{prev2}_index.json"]); drain(cfg, store, 60)
    import collections
    for day in (1, 2, 3, 4, 10, 11):
        with store.connect() as con:
            con.execute("UPDATE url_queue SET finished_at = finished_at - INTERVAL 25 HOUR WHERE status = 'failed'")
        out = monthly_refresh_tick(cfg, store, today=today.replace(day=day)); drain(cfg, store, 60)
        c = collections.Counter(p for p, _, _ in srv.log)
        print(f"day {day:2}: tick queued={out.get('queued')} retried={out.get('retried')} done={out.get('done')} | rows:",
              [(r['id'], r['url'][:21], r['status']) for r in rows(store)], "| GETs:", {k.split('/')[-1][:10]: v for k, v in c.items()})
    srv.stop(); shutil.rmtree(root, ignore_errors=True)
