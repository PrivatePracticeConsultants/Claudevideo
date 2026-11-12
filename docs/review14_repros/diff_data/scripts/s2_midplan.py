"""S2: files landing / forgotten while a sliced full build is part-way.
After the plan + whatever the queue does next (incremental while stale),
the spine must equal a fresh one-shot build of the final raw store."""
import json, shutil, sys
from pathlib import Path

D = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(D / "scripts"))


class Killed(Exception):
    pass


def kill_after(store, n):
    real = store._run_slice
    c = {"n": 0}
    def f(con, plan, idx, log):
        c["n"] += 1
        if c["n"] > n:
            raise Killed()
        return real(con, plan, idx, log)
    store._run_slice = f
    return real


def reference(store_dir, work, tag):
    from mrfx.store import Store
    import shapes
    ref_dir = work / f"ref_{tag}"
    shutil.rmtree(ref_dir, ignore_errors=True)
    shutil.copytree(store_dir, ref_dir)
    r = Store(ref_dir)
    with r.connect() as con:
        con.execute("DELETE FROM meta WHERE key='rollup_plan'")
        r._build_rollup_tables(con, split=1, threads=1)
    return shapes.dump(r)[:2]


def queue_like_catch_up(s):
    """what fetch.rebuild_now does while rollups_stale(): incremental, else full"""
    n = 0
    while s.rollups_stale() and n < 5:
        n += 1
        try:
            s.update_rollups_incremental()
            print("   catch-up: incremental")
        except Exception as e:
            print("   catch-up: incremental refused (%s) -> full" % e)
            s.rebuild_rollups()


def cov(s, label):
    c = s.analytics_coverage(max_age_seconds=0)
    print(f"   [{label}] state={c['state']} payers {c['covered_payers']}/{c['loaded_payers']} "
          f"files {c['covered_files']}/{c['done_files']} rows {c['covered_rows']}/{c['loaded_rows']} "
          f"covered={c['covered']}")


def new_file(s, fn, payer, month, seed):
    import shapes
    from mrfx.store import FINISHED_NOW
    pi = shapes.PAYERS.index(payer)
    recs = shapes.rows_for(pi, payer, fn, [month], seed)
    with s.rates_part_writer(fn) as w:
        w.write_batch(recs)
    s.upsert_file(fn, payer=payer, file_type="in_network", status="done",
                  rows_emitted=len(recs), finished_at=FINISHED_NOW)


def scenario(name, mutate, kill_n=9, needs_full=False):
    import shapes
    from mrfx.store import Store
    work = D / "w_s2" / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    s = Store(work / "store")
    shapes.seed(s)
    s.rebuild_rollups()                         # a prior complete build
    if needs_full:
        with s.connect() as con:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_needs_full','1')")
    s._rollup_partition_rows = lambda split=1, threads=1: 60
    real = kill_after(s, kill_n)
    try:
        s.rebuild_rollups()
    except Killed:
        pass
    plan = s.rollup_plan()
    done = {sl.get("payer") for sl in plan["slices"][:plan["next"]] if sl["t"] == "spine"}
    print(f"== {name}: plan part-way {plan['next']}/{len(plan['slices'])}, payers touched {sorted(done, key=str)}")
    mutate(s)
    cov(s, "after mutation, plan part-way")
    s._run_slice = real
    s.rebuild_rollups()                         # resume
    cov(s, "plan finished")
    print("   stale after plan:", s.rollups_stale(), "| needs_full:",
          s._meta_get(s.connect().__enter__(), "rollup_needs_full"))
    queue_like_catch_up(s)
    cov(s, "after catch-up")
    got = shapes.dump(s)[:2]
    ref = reference(work / "store", work, name)
    sp_ok, d_ok = got[0] == ref[0], got[1] == ref[1]
    print(f"   RESULT spine identical to fresh build: {sp_ok} ({len(got[0])} vs {len(ref[0])}); "
          f"directory identical: {d_ok}")
    if not sp_ok:
        a, b = set(got[0]), set(ref[0])
        print("   extra rows in spine:", len(a - b), "missing:", len(b - a),
              "payers of extras:", sorted({r[0] for r in a - b}))


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    scenario("a_file_lands_for_done_payer",
             lambda s: new_file(s, "p0_f2.json", "Alpha", "2026-08", 99))
    scenario("b_file_lands_for_pending_payer",
             lambda s: new_file(s, "p3_f2.json", "Zeta", "2026-08", 98))
    scenario("c_forget_done_payer_file", lambda s: s.forget_file("p0_f1.json"))

    def unreadable_forget(s):
        s._part_payers_for_removal = lambda p: None   # part held open: scope unknown
        s.forget_file("p0_f1.json")
        del s._part_payers_for_removal
    scenario("d_unscopable_forget_while_needs_full_plan", unreadable_forget, needs_full=True)
    scenario("e_unscopable_forget_no_prior_flag", unreadable_forget, needs_full=False)
