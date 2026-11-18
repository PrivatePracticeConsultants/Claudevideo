"""User actions in every status (synthetic rows + store calls), the list_urls
window, 'clear queue' during an index expansion, skip during the disk-full
sleep and during a park."""
import datetime as dt
import threading
import types
from h import *  # noqa

STATES = {
    "queued": dict(status="queued"),
    "downloading": dict(status="downloading"),
    "fetched": dict(status="fetched"),
    "expanding": dict(status="expanding", kind="toc"),
    "ingesting": dict(status="ingesting", kind="in_network"),
    "paused/parked": dict(status="paused", park_count=2, retry_after="future"),
    "paused/long": dict(status="paused", park_count=0, auto_resumes=1, retry_after="future"),
    "paused/waiting": dict(status="paused", park_count=1, waiting_on=2, retry_after="future"),
    "failed": dict(status="failed", kind="in_network"),
    "skipped/user": dict(status="skipped", kind="unknown"),
    "skipped/duplicate": dict(status="skipped", kind="duplicate", content_sha="abc"),
    "skipped/canceled": dict(status="skipped", kind="canceled"),
    "skipped/forgotten": dict(status="skipped", kind="in_network", filename="f.json.gz", content_sha="abc"),
    "oversize": dict(status="oversize", bytes_total=6_000_000_000),
    "done": dict(status="done", kind="in_network", filename="f.json.gz", content_sha="abc"),
}
ACTIONS = {
    "retry": lambda s: s.set_url_status_by_id(1, "queued"),
    "skip": lambda s: s.set_url_status_by_id(1, "skipped"),
    "force_size": lambda s: s.force_size_requeue(1),
    "retry_all": lambda s: s.requeue_failed(),
    "clear_queue": lambda s: s.clear_queued(),
    "stop_downloads": lambda s: s.cancel_all_downloading(),
    "resume_canceled": lambda s: s.resume_canceled(),
    "wake_paused": lambda s: s.wake_paused_urls(),
    "wake_parked(idle)": lambda s: s.wake_parked_urls(only_if_idle=True),
    "recover_stuck": lambda s: s.recover_stuck_urls(),
    "re-paste": lambda s: s.enqueue_url("https://x/f.json.gz", "https://x/f.json.gz"),
    "re-expand(child)": lambda s: s.enqueue_url("https://x/f.json.gz", "https://x/f.json.gz", parent_id=2),
    "monthly_requeue": lambda s: s.requeue_urls(["https://x/f.json.gz"]),
}


def seed(store, spec):
    spec = dict(spec)
    ra = spec.pop("retry_after", None)
    with store.connect() as con:
        con.execute("DELETE FROM url_queue")
        con.execute("INSERT INTO url_queue (id, url, dedup_key, kind, status, bytes_total, bytes_done, progress, added_at) "
                    "VALUES (1, 'https://x/f.json.gz', 'https://x/f.json.gz', 'unknown', 'queued', 0, 0, 0, now())")
        con.execute("INSERT INTO url_queue (id, url, dedup_key, kind, status, bytes_total, bytes_done, progress, added_at) "
                    "VALUES (2, 'https://x/twin.json.gz', 'https://x/twin.json.gz', 'in_network', 'ingesting', 0, 0, 0, now())")
        for k, v in spec.items():
            con.execute(f"UPDATE url_queue SET {k} = ? WHERE id = 1", [v])
        if ra:
            con.execute("UPDATE url_queue SET retry_after = ? WHERE id = 1", [dt.datetime(2099, 1, 1)])


if __name__ == "__main__":
    root, cfg, store = world("s9")
    banner("A: action x state matrix — resulting status of row 1 ('.' = unchanged)")
    print(f"{'state':20}" + "".join(f"{a:>18}" for a in ACTIONS))
    no_action = []
    for sname, spec in STATES.items():
        line = f"{sname:20}"
        changed_by = []
        for aname, fn in ACTIONS.items():
            seed(store, spec)
            try:
                fn(store)
            except Exception as e:  # noqa: BLE001
                line += f"{'ERR':>18}"; continue
            r = q(store, "SELECT status, kind FROM url_queue WHERE id = 1")[0]
            cell = "." if r["status"] == spec["status"] else r["status"]
            if cell != ".":
                changed_by.append(aname)
            line += f"{cell:>18}"
        print(line)
        if not any(a in changed_by for a in ("retry", "skip", "force_size", "retry_all", "clear_queue", "stop_downloads", "resume_canceled")):
            no_action.append(sname)
    print("\nstates with NO user action at all:", no_action)

    banner("B: list_urls window — 600 pasted links, the oldest one failed: reachable?")
    with store.connect() as con:
        con.execute("DELETE FROM url_queue")
        for i in range(1, 601):
            con.execute("INSERT INTO url_queue (id, url, dedup_key, kind, status, parent_id, bytes_total, bytes_done, progress, added_at) "
                        "VALUES (?, ?, ?, 'unknown', ?, NULL, 0, 0, 0, now())",
                        [i, f"https://x/p{i}.json.gz", f"https://x/p{i}.json.gz", "failed" if i <= 3 else "done"])
    listed = {r["id"] for r in store.list_urls()}
    print(f"  listed {len(listed)} rows; failed pasted rows 1-3 present? {[i in listed for i in (1, 2, 3)]}; counts={store.url_queue_counts()}")
    with store.connect() as con:
        con.execute("UPDATE url_queue SET status = 'oversize' WHERE id = 4")
        con.execute("UPDATE url_queue SET status = 'paused', park_count = 1 WHERE id = 5")
    listed = {r["id"] for r in store.list_urls()}
    print(f"  oversize pasted row 4 present? {4 in listed}; parked pasted row 5 present? {5 in listed}")

    banner("C: 'Clear queue' pressed while an index is EXPANDING (expansion takes a while)")
    root, cfg, store = world("s9c")
    import mrfx.fetch as mf
    srv = Server({f"/f{i}.json.gz": rate_file_bytes(f"f{i}") for i in range(4)})
    srv.routes["/toc.json"] = toc_bytes([srv.url(f"/f{i}.json.gz") for i in range(4)])
    real_expand = mf.expand_toc_with_plans
    gate = threading.Event()
    def slow_expand(*a, **k):
        gate.set(); time.sleep(2.0)
        return real_expand(*a, **k)
    mf.expand_toc_with_plans = slow_expand
    add_urls(store, [srv.url("/toc.json")])
    stop, th = serve(cfg, store)
    gate.wait(20)
    print("  during expansion:", {r["status"] for r in rows(store)}, "-> clear_queued() =", store.clear_queued(),
          "; toc row now:", status_of(store, "toc.json")["status"])
    time.sleep(3.0)
    print("  3 s later:", [(r["url"], r["status"], r["kind"]) for r in rows(store)])
    wait_for(lambda: all(r["status"] in ("done", "skipped", "failed") for r in rows(store)), 30)
    stop.set(); th.join(30)
    mf.expand_toc_with_plans = real_expand
    print("  final:", [(r["url"], r["status"]) for r in rows(store)], " rates:", rates_per_file(store))

    banner("D: skip / retry during the 60 s disk-full sleep")
    root, cfg, store = world("s9d")
    srv.routes["/g.json.gz"] = rate_file_bytes("g")
    real_reserve = mf._reserve_disk_or_raise
    hit = {"n": 0}
    def full_once(*a, **k):
        hit["n"] += 1
        if hit["n"] == 1:
            raise mf.DownloadError("not enough free disk space for this file: it needs ~0.0 GB", retryable=False)
        return real_reserve(*a, **k)
    mf._reserve_disk_or_raise = full_once
    slept = {}
    real_sleep = time.sleep
    mf.time = types.SimpleNamespace(**{k: getattr(time, k) for k in dir(time) if not k.startswith("_")})
    mf.time.sleep = lambda s: (slept.setdefault("s", s), real_sleep(min(s, 3.0)))
    add_urls(store, [srv.url("/g.json.gz")])
    stop, th = serve(cfg, store)
    wait_for(lambda: slept.get("s"), 20)
    r = status_of(store, "g.json.gz")
    print(f"  downloader sleeping {slept.get('s')}s; row status during the sleep: {r['status']} error={r['error'][:50]!r}")
    print("  user presses skip:", store.set_url_status_by_id(r["id"], "skipped"))
    time.sleep(4.0)
    print("  after the sleep ends:", status_of(store, "g.json.gz")["status"])
    print("  user presses retry:", store.set_url_status_by_id(r["id"], "queued"))
    wait_for(lambda: status_of(store, "g.json.gz")["status"] in ("done", "failed"), 30)
    print("  final:", status_of(store, "g.json.gz")["status"])
    stop.set(); th.join(30)
    mf._reserve_disk_or_raise = real_reserve; mf.time = time

    banner("E: skip during a park, then wake")
    root, cfg, store = world("s9e", download_timeout_seconds=0.5, download_park_seconds=1.5, download_stall_seconds=600,
                             download_park_minutes=10, download_park_limit=6, download_retries=6)
    srv.routes["/stall.json.gz"] = {"body": b"\0" * (4 << 20), "stall_after": 0, "stall_for": 30}
    add_urls(store, [srv.url("/stall.json.gz")])
    stop, th = serve(cfg, store)
    wait_for(lambda: status_of(store, "stall")["status"] == "paused", 30)
    r = status_of(store, "stall"); print("  parked:", r["status"], r["error"][:50])
    print("  skip while parked:", store.set_url_status_by_id(r["id"], "skipped"))
    time.sleep(2)
    print("  wake_parked / wake_paused after skip:", store.wake_parked_urls(), store.wake_paused_urls(), "->", status_of(store, "stall")["status"])
    stop.set(); srv.release.set(); th.join(30)
    srv.stop()
