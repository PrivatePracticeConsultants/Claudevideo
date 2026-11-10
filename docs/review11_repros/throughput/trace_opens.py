"""Summarize an `strace -f -e trace=openat,...` log: how many times each
pipeline file was opened (= full or partial passes), by pid, plus renames,
unlinks and fsyncs.  usage: trace_opens.py <trace.txt> <name-substring>"""
import re
import sys
from collections import Counter

trace, key = sys.argv[1], sys.argv[2]
opens = Counter()
other = Counter()
for line in open(trace, errors="replace"):
    m = re.match(r"(\d+)\s+(\w+)\((.*)\)\s*=\s*(-?\d+)", line)
    if not m:
        continue
    pid, call, args, ret = m.groups()
    if call == "openat":
        if key in args and int(ret) >= 0:
            path = re.search(r'"([^"]*)"', args).group(1).rsplit("/", 1)[-1]
            mode = "W" if ("O_WRONLY" in args or "O_RDWR" in args) else "R"
            opens[(pid, path[-70:], mode)] += 1
    elif call in ("rename", "renameat", "renameat2", "unlink", "unlinkat", "fsync", "fdatasync"):
        if key in args or call in ("fsync", "fdatasync"):
            other[(call, re.sub(r'"[^"]*/', '"', args)[:120])] += 1
print(f"opens of files matching {key!r} (pid, file, R/W):")
for (pid, path, mode), n in sorted(opens.items()):
    print(f"  pid {pid}  {mode}  x{n}  {path}")
print("renames/unlinks/fsyncs:")
for (call, args), n in sorted(other.items()):
    print(f"  x{n} {call}({args})")
