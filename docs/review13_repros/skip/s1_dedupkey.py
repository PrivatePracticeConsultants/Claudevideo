"""S1: dedup_key / _normalize_url / filename_for — which URL pairs the queue
treats as ONE row (same key) and which as two. 'ONE' for two DIFFERENT files
is a false skip (the second is never queued); 'TWO' for the same file is a
false load candidate (the second row downloads; the content checks decide
later). Pure functions: no store, no server."""
import sys

sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.fetch import _normalize_url, dedup_key, filename_for  # noqa: E402

B = "https://cdn.payer.com/mrf"
PAIRS = [
    # (label, url_a, url_b, want_same_key, note)
    # ---- false-skip side: distinct files that must stay two rows
    ("identity param beside a signature", f"{B}/dl?file=a.json&sig=X1", f"{B}/dl?file=b.json&sig=Y2", False, "round 11 2.10"),
    ("short ?token=<file-id> beside sig", f"{B}/dl?token=FILE1&sig=aaaa", f"{B}/dl?token=FILE2&sig=bbbb", False, "round 11 2.10"),
    ("LONG ?token=<file-id> (>=20 chars) beside sig", f"{B}/dl?token=in-network-rates-01-of-10&sig=aaaa", f"{B}/dl?token=in-network-rates-02-of-10&sig=bbbb", False, "token value >= 20 chars reads as a signature part"),
    ("?st=NY vs ?st=CA beside sig", f"{B}/dl?st=NY&sig=a", f"{B}/dl?st=CA&sig=b", False, ""),
    ("?st=<date> vs other date beside sig (real SAS start)", f"{B}/dl?st=2026-01-01T00%3A00Z&sig=a", f"{B}/dl?st=2026-02-01T00%3A00Z&sig=b", True, "SAS start is volatile: SAME file"),
    ("?policy=A vs B unsigned", f"{B}/dl?policy=A", f"{B}/dl?policy=B", False, ""),
    ("?x-amz-meta-file=1 vs 2", f"{B}/dl?x-amz-meta-file=1&X-Amz-Signature=s", f"{B}/dl?x-amz-meta-file=2&X-Amz-Signature=t", False, "round 11 2.10"),
    ("path case differs (S3 keys are case-sensitive)", f"{B}/Rates.json.gz", f"{B}/rates.json.gz", False, "two keys by design"),
    ("?expires=<file-id>? unsigned 'expires' is ALWAYS dropped", f"{B}/dl?expires=1&id=a", f"{B}/dl?expires=2&id=a", True, "same file re-signed: correct"),
    ("different files told apart ONLY by an always-dropped name", f"{B}/dl?expires=fileA", f"{B}/dl?expires=fileB", True, "would collapse — a payer naming files in ?expires= is not a real shape"),
    ("different files told apart only by ?Signature= (no other param)", f"{B}/dl?Signature=fileA", f"{B}/dl?Signature=fileB", True, "would collapse — not a real shape"),
    ("query param ORDER", f"{B}/dl?a=1&b=2", f"{B}/dl?b=2&a=1", True, ""),
    ("repeated param a=1&a=2 vs a=2&a=1", f"{B}/dl?a=1&a=2", f"{B}/dl?a=2&a=1", True, ""),
    ("repeated param a=1&a=2 vs a=1", f"{B}/dl?a=1&a=2", f"{B}/dl?a=1", False, ""),
    # ---- false-load side: the same file under two spellings
    ("http vs https", f"http://cdn.payer.com/mrf/r.json.gz", f"https://cdn.payer.com/mrf/r.json.gz", False, "two rows; the content checks decide"),
    ("HOST case", f"https://CDN.Payer.com/mrf/r.json.gz", f"https://cdn.payer.com/mrf/r.json.gz", True, ""),
    ("default port :443", f"https://cdn.payer.com:443/mrf/r.json.gz", f"https://cdn.payer.com/mrf/r.json.gz", False, "two rows"),
    ("trailing slash on a directory-style URL", f"{B}/dl/", f"{B}/dl", False, "two rows"),
    ("percent-encoded vs literal space", f"{B}/carefirst%20ppo_index.json", f"{B}/carefirst ppo_index.json", False, "two rows"),
    ("percent-encoded vs literal safe char (%2D vs -)", f"{B}/in%2Dnetwork.json.gz", f"{B}/in-network.json.gz", False, "two rows"),
    ("upper vs lower-case percent escape", f"{B}/a%2fb.json", f"{B}/a%2Fb.json", False, "two rows"),
    ("empty query ? vs none", f"{B}/r.json.gz?", f"{B}/r.json.gz", True, ""),
    ("fragment #x", f"{B}/r.json.gz#x", f"{B}/r.json.gz", True, ""),
    ("S3 presigned re-signed next day", f"{B}/r.json.gz?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=A%2F20260101&X-Amz-Date=20260101T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=aaa",
     f"{B}/r.json.gz?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=A%2F20260102&X-Amz-Date=20260102T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=bbb", True, ""),
    ("Azure SAS re-signed next day", f"{B}/r.json.gz?sv=2021-06-08&st=2026-01-01T00%3A00Z&se=2026-01-02T00%3A00Z&sr=b&sp=r&sig=aaa",
     f"{B}/r.json.gz?sv=2021-06-08&st=2026-01-02T00%3A00Z&se=2026-01-03T00%3A00Z&sr=b&sp=r&sig=bbb", True, ""),
    ("CloudFront signed re-signed", f"{B}/r.json.gz?Expires=1&Signature=a&Key-Pair-Id=K", f"{B}/r.json.gz?Expires=2&Signature=b&Key-Pair-Id=K", True, ""),
    ("GOOGLE CLOUD STORAGE V4 signed, re-signed next day", f"{B}/r.json.gz?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=svc%2F20260101%2Fauto%2Fstorage%2Fgoog4_request&X-Goog-Date=20260101T000000Z&X-Goog-Expires=3600&X-Goog-SignedHeaders=host&X-Goog-Signature=aaa",
     f"{B}/r.json.gz?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=svc%2F20260102%2Fauto%2Fstorage%2Fgoog4_request&X-Goog-Date=20260102T000000Z&X-Goog-Expires=3600&X-Goog-SignedHeaders=host&X-Goog-Signature=bbb", True, "x-goog-* is in no list"),
    ("Akamai / generic ?token=<JWT>", f"{B}/r.json.gz?token=eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.sig1", f"{B}/r.json.gz?token=eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjJ9.sig2", True, "a JWT token with no sig param: is it stripped?"),
    ("Akamai __token__", f"{B}/r.json.gz?__token__=exp%3D1~hmac%3Da", f"{B}/r.json.gz?__token__=exp%3D2~hmac%3Db", True, ""),
    ("CDN host alias (two hosts, same file)", "https://cdn1.payer.com/mrf/r.json.gz", "https://cdn2.payer.com/mrf/r.json.gz", False, "two rows; content checks decide"),
    ("response-content-disposition differs", f"{B}/r.json.gz?response-content-disposition=attachment", f"{B}/r.json.gz", False, "two rows"),
]


def main():
    bad = []
    print(f"{'verdict':8} {'want':5} {'got':5} label")
    for label, a, b, want_same, note in PAIRS:
        same = dedup_key(a) == dedup_key(b)
        ok = same == want_same
        tag = "ok" if ok else "DIFFERS"
        print(f"{tag:8} {'ONE' if want_same else 'TWO':5} {'ONE' if same else 'TWO':5} {label}" + (f"  [{note}]" if note else ""))
        if not ok:
            bad.append((label, a, b, dedup_key(a), dedup_key(b)))
        # filename collision across distinct keys?
        if not same and filename_for(a) == filename_for(b):
            print(f"   !! filename_for collision: {filename_for(a)}")
    print("\n-- _normalize_url")
    for u in ["cdn.payer.com/mrf/r.json.gz", "//cdn.payer.com/x", "HTTPS://CDN.payer.com/x", "rates.json", "http://", "https:///x", " https://a.com/x"]:
        print(f"  {u!r:40} -> {_normalize_url(u)!r}")
    print("\n-- keys the user may not expect")
    for u in [f"{B}/r.json.gz?token=eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.sig1",
              f"{B}/dl?token=in-network-rates-01-of-10&sig=aaaa",
              f"{B}/r.json.gz?X-Goog-Signature=aaa&X-Goog-Date=20260101T000000Z"]:
        print(f"  {u}\n    -> {dedup_key(u)}")
    print("\nDIFFERS from the stated expectation:", len(bad))
    for label, a, b, ka, kb in bad:
        print(f"  {label}\n    {ka}\n    {kb}")


if __name__ == "__main__":
    main()
