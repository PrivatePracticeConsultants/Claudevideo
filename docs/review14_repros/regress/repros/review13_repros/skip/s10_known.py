"""S10: `mrfx add --known` and the catalog: month-dated vs static URLs, the
same URL twice, placeholder lines pasted by hand, and what the monthly
refresh (catalog-scoped) does for a user-pasted static index that is NOT in
the catalog."""
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402


def main():
    from mrfx import fetch
    from mrfx.known_sources import load_known_sources
    logging.basicConfig(level=logging.ERROR)
    cfg = h.scratch_cfg("s10", parallel_downloads=1, parallel_ingests=1, known_sources_path=Path("/home/user/Claudevideo/config/known_sources.yaml"))
    store = h.open_store(cfg)
    srcs = load_known_sources(cfg.known_sources_path)
    urls = [s["url"] for s in srcs]
    tpls = [s.get("url_template") or s["url"] for s in srcs]
    print(f"catalog: {len(srcs)} sources, {len(set(urls))} distinct expanded URLs, "
          f"{sum('{FIRST_OF_MONTH}' in t for t in tpls)} month-dated, {sum(bool(s.get('queueable')) for s in srcs)} queueable")
    dup = [u for u in set(urls) if urls.count(u) > 1]
    print("  the same URL under two catalog entries:", dup or "none")
    # a month-dated template queued twice in one month, and in two months
    tpl = next(t for t in tpls if "{FIRST_OF_MONTH}" in t)
    today = dt.date.today()
    print("  template:", tpl)
    r1 = fetch.add_urls(store, [tpl]); r2 = fetch.add_urls(store, [tpl])
    print("  pasted twice this month:", r1, r2)
    from mrfx.known_sources import expand_placeholders
    nxt = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    r3 = fetch.add_urls(store, [expand_placeholders(tpl, nxt)])
    print("  next month's expansion:", r3, "-> rows:", len(h.queue_rows(store, "id")))
    # a static index pasted twice
    stat = next(s["url"] for s in srcs if "{FIRST_OF_MONTH}" not in (s.get("url_template") or s["url"]))
    print("  static index pasted twice:", fetch.add_urls(store, [stat]), fetch.add_urls(store, [stat]))
    # monthly_refresh_tick scope: a user-pasted static index NOT in the catalog
    fetch.add_urls(store, ["https://payer.example.com/mrf/index.json"])
    with store.write_lock, store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'done', kind = 'toc'")
    out = fetch.monthly_refresh_tick(cfg, store)
    rows = h.queue_rows(store, "id, status, kind, url")
    print("  monthly_refresh_tick with every top row 'done':", out)
    print("  user-pasted non-catalog index after the tick:", [r for r in rows if "payer.example.com" in r["url"]])
    print("  catalog static index after the tick:", [(r["status"], r["url"][:60]) for r in rows if r["url"] == stat])


if __name__ == "__main__":
    main()
