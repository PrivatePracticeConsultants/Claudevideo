"""monthly_refresh_tick + _maybe_queue_prev_month across simulated days."""
import datetime as dt
from h import *  # noqa
from mrfx.fetch import monthly_refresh_tick

if __name__ == "__main__":
    root, cfg, store = world("s10")
    logs = capture_logs()
    today = dt.date.today()
    this_m = today.replace(day=1).isoformat()
    prev_m = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1).isoformat()
    srv = Server({})
    base = srv.base
    srv.routes[f"/dated/{prev_m}_index.json"] = toc_bytes([base + "/f1.json.gz"])
    srv.routes["/f1.json.gz"] = rate_file_bytes("f1")
    srv.routes["/static_index.json"] = toc_bytes([base + "/f1.json.gz"])
    srv.routes["/dead/" + prev_m + "_index.json"] = toc_bytes([base + "/f1.json.gz"])
    ks = root / "known_sources.yaml"
    ks.write_text(f"""sources:
  - name: Dated Payer
    states: [MO]
    url: {base}/dated/{{FIRST_OF_MONTH}}_index.json
    kind: toc
    queueable: true
  - name: Static Payer
    states: [MO]
    url: {base}/static_index.json
    kind: toc
    queueable: true
  - name: Dead Payer
    states: [MO]
    url: {base}/dead/{{FIRST_OF_MONTH}}_index.json
    kind: toc
    queueable: true
""")
    cfg.known_sources_path = ks
    # last month the user loaded all three
    add_urls(store, [f"{base}/dated/{prev_m}_index.json", f"{base}/static_index.json", f"{base}/dead/{prev_m}_index.json"])
    drain(cfg, store, 60)
    print("loaded last month:", [(r["url"], r["status"]) for r in rows(store)])
    # the dead payer's server now 404s everything; the dated payer has not posted this month yet
    del srv.routes["/dead/" + prev_m + "_index.json"]
    gets = lambda: {p.split("?")[0]: n for p, n in __import__("collections").Counter(p for p, _, _ in srv.log).items()}
    for day in (1, 2, 3, 10, 11, 12):
        d = today.replace(day=day)
        # make yesterday's failures old enough for the daily retry
        with store.connect() as con:
            con.execute("UPDATE url_queue SET finished_at = finished_at - INTERVAL 25 HOUR WHERE status = 'failed'")
        out = monthly_refresh_tick(cfg, store, today=d)
        dr = drain(cfg, store, 60)
        print(f"day {day:2}: tick={out} drained={dr['finished']} rows=", [(r['id'], r['url'][:28], r['status']) for r in rows(store)])
    print("GET counts:", gets())
    print("meta:", store.meta_get("monthly_refresh_last"), store.meta_get("monthly_refresh_static"))
    print("prev-month fallback messages:", [r["error"] for r in rows(store) if r["error"] and "last month" in r["error"]][:3])
    srv.stop()
