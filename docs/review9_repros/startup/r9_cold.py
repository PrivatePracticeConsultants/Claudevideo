"""Cold vs warm cost of the migration's one scan, and of cheaper alternatives."""
import glob, os, sys, time
import duckdb
D = sys.argv[1]
fs = sorted(glob.glob(D + "/rates/*.parquet"))
def drop():
    os.sync()
    try:
        open("/proc/sys/vm/drop_caches", "w").write("3\n"); return True
    except OSError as e:
        return False
def run(label, sql, params=None, cold=True):
    ok = drop() if cold else None
    con = duckdb.connect()
    t = time.monotonic(); r = con.execute(sql, params or []).fetchall()
    print(f"{label:55s} cold={ok!s:5} {time.monotonic()-t:7.2f} s  rows={len(r)}")
    con.close()
if __name__ == "__main__":
    print(len(fs), "parts,", sum(os.path.getsize(f) for f in fs) / 1e9, "GB")
    q = "SELECT DISTINCT filename, negotiated_type, is_dollar_rate FROM read_parquet(?, filename=true, union_by_name=true)"
    run("one scan (as shipped in 0.7.2)", q, [fs])
    run("one scan, warm", q, [fs], cold=False)
    run("one scan without union_by_name", q.replace(", union_by_name=true", ""), [fs])
    run("types only (no is_dollar)", "SELECT DISTINCT filename, negotiated_type FROM read_parquet(?, filename=true)", [fs])
    run("parquet_metadata stats of negotiated_type",
        "SELECT file_name, stats_min_value, stats_max_value, count(*) FROM parquet_metadata(?) WHERE path_in_schema = 'negotiated_type' GROUP BY ALL", [fs])
    run("parquet_metadata, warm",
        "SELECT file_name, stats_min_value, stats_max_value, count(*) FROM parquet_metadata(?) WHERE path_in_schema = 'negotiated_type' GROUP BY ALL", [fs], cold=False)
