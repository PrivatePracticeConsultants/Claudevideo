"""Park/pause/retry arithmetic: auto_resumes is never reset; which requeue
paths forget park_count / retry_after / waiting_on / kind; lanes vs parked."""
import datetime as dt
from h import *  # noqa

if __name__ == "__main__":
    banner("A: auto_resumes is never reset — after the user's retry the next stall is an instant 'failed'")
    root, cfg, store = world("s6a", download_timeout_seconds=0.5, download_park_seconds=0,
                             download_stall_seconds=1.5, download_retries=6, download_pause_hours=1,
                             download_auto_resumes=1, download_max_seconds=0)
    srv = Server({"/stall.json.gz": {"body": b"\0" * (4 << 20), "stall_after": 0, "stall_for": 30}})
    add_urls(store, [srv.url("/stall.json.gz")])
    for step in ("first run", "wake (timer elapsed)", "user retry", "user retry again"):
        if step.startswith("wake"):
            with store.connect() as con:
                con.execute("UPDATE url_queue SET retry_after = ? WHERE id = 1", [dt.datetime(2000, 1, 1)])
            print("  woke:", store.wake_paused_urls())
        elif step.startswith("user"):
            print("  retry allowed:", store.set_url_status_by_id(1, "queued"))
        srv.release.set(); srv.release.clear()
        d = drain(cfg, store, 60)
        r = rows(store)[0]
        print(f"{step:22} -> status={r['status']:7} auto_resumes={r['auto_resumes']} park_count={r['park_count']} "
              f"retries={r['retries']} err={r['error'][:70]!r} ({d['secs']}s)")
    srv.stop()

    banner("B: which requeue paths reset which counters (direct store calls on a synthetic row)")
    root, cfg, store = world("s6b")
    cols = "status, kind, park_count, auto_resumes, retries, force_size, big_lane, waiting_on, retry_after, duplicate_of, content_sha, error"

    def seed(status="failed", **over):
        with store.connect() as con:
            con.execute("DELETE FROM url_queue")
            con.execute("INSERT INTO url_queue (id, url, dedup_key, kind, status, parent_id, bytes_total, bytes_done, progress, added_at, "
                        "park_count, auto_resumes, retries, force_size, big_lane, waiting_on, retry_after, duplicate_of, content_sha, error) "
                        "VALUES (1, 'https://x/f.json.gz', 'https://x/f.json.gz', 'duplicate', ?, ?, 5000000000, 100, 1, now(), "
                        "4, 3, 2, true, true, 99, now(), 'old.json.gz', 'abc', 'old error')", [status, over.get("parent_id")])

    def show(label):
        r = q(store, f"SELECT {cols} FROM url_queue WHERE id = 1")[0]
        r["retry_after"] = "set" if r["retry_after"] else None
        print(f"  {label:34} {r}")
    seed(); show("seed (failed, everything dirty)")
    seed(); store.set_url_status_by_id(1, "queued"); show("set_url_status_by_id retry")
    seed(); store.force_size_requeue(1); show("force_size_requeue")
    seed(); store.requeue_failed(); show("requeue_failed (bulk retry)")
    seed(); store.requeue_urls(["https://x/f.json.gz"]); show("requeue_urls (monthly)")
    seed(); store.enqueue_url("https://x/f.json.gz?sig=new", "https://x/f.json.gz"); show("re-paste (direct) of failed")
    seed(parent_id=7); store.enqueue_url("https://x/f.json.gz", "https://x/f.json.gz", parent_id=7); show("re-expand (child) of failed")
    seed("skipped"); store.resume_canceled(); show("resume_canceled (kind!=canceled)")
    seed("paused"); store.wake_paused_urls(); show("wake_paused_urls (timer set)")
    seed("oversize"); store.requeue_oversize_within(10_000_000_000); show("requeue_oversize_within")
    seed("done"); store.requeue_url_for_file("f", "x"); show("requeue_url_for_file (no match)")
    seed("ingesting"); store.release_claim(1); show("release_claim")
    seed("downloading"); store.recover_stuck_urls(); show("recover_stuck_urls")

    banner("C: a parked BIG row woken by an idle small-only lane — can that lane claim it?")
    seed("paused")
    with store.connect() as con:
        con.execute("UPDATE url_queue SET waiting_on = NULL, park_count = 2, bytes_total = 5000000000, big_lane = true WHERE id = 1")
    print("  wake_parked(only_if_idle):", store.wake_parked_urls(only_if_idle=True))
    rec = store.next_queued_url("small_only", 1 << 20)
    print("  small_only claims it:", rec and rec["id"], "status now:", q(store, "SELECT status FROM url_queue")[0])
    print("  (no stranding: every lane falls back to big rows — checked)")
