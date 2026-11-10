"""Drop ONE store's files from the OS page cache (posix_fadvise DONTNEED) — reproduces the
first search after the PC was switched on, without touching anyone else's cache.
usage: evict.py <store_dir>"""
import os
import sys

if __name__ == "__main__":
    n = b = 0
    for root, _d, files in os.walk(sys.argv[1]):
        for f in files:
            p = os.path.join(root, f)
            try:
                fd = os.open(p, os.O_RDONLY)
                os.fsync(fd)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                os.close(fd)
                n += 1; b += os.path.getsize(p)
            except OSError:
                pass
    print(f"evicted {n} files, {b/1e9:.2f} GB")
