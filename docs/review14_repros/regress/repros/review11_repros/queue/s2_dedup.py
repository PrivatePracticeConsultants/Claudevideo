"""dedup_key / filename_for: collisions (two different files -> one key) and
misses (same file -> two keys), verified through add_urls counts."""
from h import *  # noqa

if __name__ == "__main__":
    root, cfg, store = world("s2")
    cases = {
        "http vs https": ("http://h.com/a.json", "https://h.com/a.json"),
        "trailing slash": ("https://h.com/a.json", "https://h.com/a.json/"),
        "host case": ("https://H.COM/a.json", "https://h.com/a.json"),
        "path case": ("https://h.com/A.json", "https://h.com/a.json"),
        "param order": ("https://h.com/a?x=1&y=2", "https://h.com/a?y=2&x=1"),
        "pct-encoding": ("https://h.com/a%20b.json", "https://h.com/a b.json"),
        "pct-case": ("https://h.com/a%2Fb.json", "https://h.com/a%2fb.json"),
        "default port": ("https://h.com:443/a.json", "https://h.com/a.json"),
        "empty query": ("https://h.com/a.json?", "https://h.com/a.json"),
        "S3 signed twice": ("https://b.s3.amazonaws.com/k.json.gz?X-Amz-Signature=1&X-Amz-Expires=3&X-Amz-Credential=c",
                            "https://b.s3.amazonaws.com/k.json.gz?X-Amz-Signature=2&X-Amz-Expires=9&X-Amz-Credential=d"),
        "Azure SAS twice": ("https://x.blob.core.windows.net/c/f.json.gz?sv=2020&st=2026-01-01&se=2026-01-02&sr=b&sp=r&sig=AAA",
                            "https://x.blob.core.windows.net/c/f.json.gz?sv=2020&st=2026-02-01&se=2026-02-02&sr=b&sp=r&sig=BBB"),
        "CloudFront": ("https://d1.cloudfront.net/f.json.gz?Expires=1&Signature=a&Key-Pair-Id=K",
                       "https://d1.cloudfront.net/f.json.gz?Expires=2&Signature=b&Key-Pair-Id=K"),
        # COLLISION candidates: two DIFFERENT files whose only difference is a param the key strips
        "COLLISION? token=file-id + sig": ("https://cdn.x.com/dl?token=FILE1&sig=abc", "https://cdn.x.com/dl?token=FILE2&sig=def"),
        "COLLISION? st=state + sig": ("https://cdn.x.com/rates.json?st=NY&sig=abc", "https://cdn.x.com/rates.json?st=CA&sig=def"),
        "COLLISION? se= + sig": ("https://cdn.x.com/dl?se=A&sig=1", "https://cdn.x.com/dl?se=B&sig=2"),
        "COLLISION? policy= always stripped": ("https://cdn.x.com/dl?policy=groupA", "https://cdn.x.com/dl?policy=groupB"),
        "COLLISION? expires= always stripped": ("https://cdn.x.com/dl?expires=2026-01", "https://cdn.x.com/dl?expires=2026-02"),
        "COLLISION? x-amz-* always stripped": ("https://cdn.x.com/dl?x-amz-meta-file=1", "https://cdn.x.com/dl?x-amz-meta-file=2"),
        "no sig: token kept": ("https://cdn.x.com/dl?token=FILE1", "https://cdn.x.com/dl?token=FILE2"),
        "fragment": ("https://h.com/a.json#1", "https://h.com/a.json#2"),
        "userinfo": ("https://u@h.com/a.json", "https://h.com/a.json"),
        "dot segments": ("https://h.com/x/../a.json", "https://h.com/a.json"),
    }
    print(f"{'case':38} {'same key?':9} {'same filename?':15} add_urls")
    for name, (u1, u2) in cases.items():
        k = dedup_key(u1) == dedup_key(u2)
        f = filename_for(u1) == filename_for(u2)
        with store.connect() as con:
            con.execute("DELETE FROM url_queue")
        c = add_urls(store, [u1, u2])
        print(f"{name:38} {str(k):9} {str(f):15} added={c['added']} skipped={c['skipped']}")
    banner("filename_for collisions across DIFFERENT keys (same local file on disk?)")
    import itertools
    us = ["https://a.com/rates.json.gz", "https://b.com/rates.json.gz", "https://a.com/x/rates.json.gz",
          "https://a.com/rates.json.gz?file=1", "https://a.com/rates.json.gz?file=2",
          "https://a.com/" + "r" * 300 + ".json.gz", "https://a.com/" + "r" * 300 + "x.json.gz",
          "https://a.com/dl?name=a.json", "https://a.com/dl?name=b.json", "https://a.com/dl", "https://a.com/dl/"]
    for u1, u2 in itertools.combinations(us, 2):
        if dedup_key(u1) != dedup_key(u2) and filename_for(u1) == filename_for(u2):
            print("  COLLISION:", u1, u2, filename_for(u1))
    print("  filename for a query-only endpoint:", filename_for("https://a.com/dl?name=a.json"))
    print("  filename for /dl/:", filename_for("https://a.com/dl/"))
    print("done")
