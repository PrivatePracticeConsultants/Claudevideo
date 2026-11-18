"""New shapes through the five parse paths (classic, fast, two-pass with the
classic / fast skim, single-read big); rows + QA must be identical. Also the
expected rows where the rule is clear."""
import io, json, contextlib, tempfile, sys
from h import *
from mrfx.parser import (InNetworkParser, run_big_file_fast, skim_needed_ref_ids,
                         skim_needed_ref_ids_fast)
import mrfx.fastscan as fs

cfg = mkcfg("n4")

def rows_of(batches, res):
    out = []
    for b in batches:
        for r in b:
            out.append((r["billing_code"], r["tin_value"], r["npi"], r["negotiated_rate"]))
    return sorted(out, key=repr), {k: v for k, v in res.qa.to_dict().items() if v}

def mk(sink_store, ext=None, keep=None):
    return InNetworkParser(cfg, "x_2026-05.json", external_refs=ext, keep_ref_ids=keep,
                           sink=lambda b: sink_store.append(list(b)),
                           header_defaults={"payer": None, "schema_version": None, "last_updated_on": None})

def run(data: bytes, ext=None):
    res = {}
    for name in ("classic", "fast", "two_pass_classic", "two_pass_fastskim", "big_fast"):
        store_ = []
        try:
            if name == "classic":
                p = mk(store_, ext); r = p.parse(io.BytesIO(data))
            elif name == "fast":
                p = mk(store_, ext)
                try:
                    r = p.parse_fast(io.BytesIO(data))
                except fs.FastScanUnsupported as e:
                    res[name] = ("DECLINED", str(e)[:60], {}); continue
            elif name.startswith("two_pass"):
                skim = skim_needed_ref_ids if name.endswith("classic") else skim_needed_ref_ids_fast
                try:
                    keep, n = skim(cfg, io.BytesIO(data))
                except fs.FastScanUnsupported as e:
                    res[name] = ("DECLINED", str(e)[:60], {}); continue
                p = mk(store_, ext, keep=keep); r = p.parse(io.BytesIO(data))
            else:
                p = mk(store_, ext)
                sp = Path(tempfile.mkdtemp(dir=str(BASE))) / ".s.1.spool.tmp"
                try:
                    r, n = run_big_file_fast(cfg, p, lambda: contextlib.nullcontext(io.BytesIO(data)), sp)
                except fs.FastScanUnsupported as e:
                    res[name] = ("DECLINED", str(e)[:60], {}); continue
            rows, qa = rows_of(store_, r)
            res[name] = (len(rows), rows, dict(qa, skipped=r.ref_groups_skipped) if r.ref_groups_skipped else qa)
        except Exception as e:
            res[name] = ("ERR", type(e).__name__ + ": " + str(e)[:80], {})
    return res

A = [grp(["1111111111"], "111111111")]; B = [grp(["2222222222"], "222222222")]
C = [grp(["3333333333"], "333333333")]; D = [grp(["4444444444"], "444444444")]
EXT = {1: [("999999999", "ein", ("9999999999",))], 2: [("888888888", "ein", ("8888888888",))],
       7: [("777777777", "ein", ("7777777777",))]}
R = lambda i, g: {"provider_group_id": i, "provider_groups": g}

def raw_dupkeys(parts):
    """A top-level object with repeated keys, in order."""
    return ("{" + ",".join(f"{json.dumps(k)}:{json.dumps(v)}" for k, v in parts) + "}").encode()

H = list(HDR.items())
SHAPES = {
 # name: (bytes, ext, expected [(tin, npi)] or None)
 "own_first+companion_only_id": (json.dumps({**HDR, "provider_references": [R(1, A)], "in_network": [item("97110", refs=[1, 2])]}).encode(), EXT,
                                 [("111111111", "1111111111"), ("888888888", "8888888888")]),
 "own_after+companion_only_id": (json.dumps({**HDR, "in_network": [item("97110", refs=[1, 2])], "provider_references": [R(1, A)]}).encode(), EXT,
                                 [("111111111", "1111111111"), ("888888888", "8888888888")]),
 "split_table_around_in_network": (raw_dupkeys(H + [("provider_references", [R(1, A)]), ("in_network", [item("97110", refs=[1, 2])]), ("provider_references", [R(2, B)])]), EXT,
                                 [("111111111", "1111111111"), ("222222222", "2222222222")]),
 "split_table_no_ext": (raw_dupkeys(H + [("provider_references", [R(1, A)]), ("in_network", [item("97110", refs=[1, 2])]), ("provider_references", [R(2, B)])]), None,
                                 [("111111111", "1111111111"), ("222222222", "2222222222")]),
 "two_in_network_arrays": (raw_dupkeys(H + [("provider_references", [R(1, A)]), ("in_network", [item("97110", refs=[1])]), ("in_network", [item("97112", refs=[1])])]), None, None),
 "dup_id_own_after_ext": (json.dumps({**HDR, "in_network": [item("97110", refs=[1])], "provider_references": [R(1, A), R(1, B)]}).encode(), EXT, None),
 "ids_str_float": (json.dumps({**HDR, "provider_references": [R("1", A), R(2.0, B), R("3.0", C), R(" 4 ", D)], "in_network": [item("97110", refs=["1", 2.0, 3, "4"])]}).encode(), EXT,
                   [("111111111", "1111111111"), ("222222222", "2222222222"), ("333333333", "3333333333"), ("444444444", "4444444444")]),
 "cite_bad_ids": (json.dumps({**HDR, "provider_references": [R(1, A)], "in_network": [item("97110", refs=[1, "2.5", "abc", None, True, [1], {"a": 1}])]}).encode(), EXT, None),
 "ids_above_2^53": (json.dumps({**HDR, "provider_references": [R(9007199254740992, A), R(9007199254740993, B)],
                                "in_network": [item("97110", refs=[9007199254740992]), item("97112", refs=[9007199254740993])]}).encode(), None,
                   None),
 "ids_19_digits": (json.dumps({**HDR, "provider_references": [R(1234567890123456789, A), R(1234567890123456790, B)],
                               "in_network": [item("97110", refs=[1234567890123456789]), item("97112", refs=[1234567890123456790])]}).encode(), None, None),
 "empty_in_network+table": (json.dumps({**HDR, "provider_references": [R(1, A)], "in_network": []}).encode(), EXT, []),
 "table_only_small": (json.dumps({**HDR, "provider_references": [R(1, A)]}).encode(), EXT, []),
 "own_location_after+ext": (json.dumps({**HDR, "in_network": [item("97110", refs=[1, 7])], "provider_references": [{"provider_group_id": 1, "location": "https://x/1.json"}]}).encode(), EXT,
                            [("777777777", "7777777777")]),
 "own_empty_groups_first+ext": (json.dumps({**HDR, "provider_references": [R(1, [])], "in_network": [item("97110", refs=[1])]}).encode(), EXT, []),
 "ref_entry_not_dict": (json.dumps({**HDR, "provider_references": [5, "x", None, R(1, A)], "in_network": [item("97110", refs=[1])]}).encode(), None, [("111111111", "1111111111")]),
 "ref_table_is_object": (json.dumps({**HDR, "provider_references": {"1": R(1, A)}, "in_network": [item("97110", refs=[1])]}).encode(), EXT, None),
 "in_network_null": (json.dumps({**HDR, "provider_references": [R(1, A)], "in_network": None}).encode(), EXT, None),
 "ambiguous_ext_and_own": (json.dumps({**HDR, "provider_references": [R(1, A)], "in_network": [item("97110", refs=[1, 2])]}).encode(), {1: None, 2: None}, [("111111111", "1111111111")]),
 "many_items_refs_after": (json.dumps({**HDR, "in_network": [item("97110", refs=[i % 5 + 1], rate=10 + i) for i in range(50)] + [item("99213", refs=[9])],
                                       "provider_references": [R(i, [grp([str(1000000000 + i)], f"{100000000+i}")]) for i in range(1, 6)]}).encode(), EXT, None),
 "ws_and_bom": (b"\xef\xbb\xbf \n" + json.dumps({**HDR, "provider_references": [R(1, A)], "in_network": [item("97110", refs=[1])]}, indent=3).encode(), None, [("111111111", "1111111111")]),
 "escaped_key": (b'{"reporting_entity_name":"Acme Health","in_\\u006eetwork":[' + json.dumps(item("97110", groups=A)).encode() + b']}', None, [("111111111", "1111111111")]),
 "escaped_code": (b'{"reporting_entity_name":"Acme Health","in_network":[' + json.dumps(item("97110", groups=A)).replace('"97110"', '"9711\\u0030"').encode() + b']}', None, [("111111111", "1111111111")]),
 "code_key_escaped": (b'{"reporting_entity_name":"Acme Health","in_network":[' + json.dumps(item("97110", groups=A)).replace('"billing_code"', '"billing\\u005fcode"').encode() + b']}', None, [("111111111", "1111111111")]),
 "nested_billing_code_decoy": (json.dumps({**HDR, "in_network": [{"bundled_codes": [{"billing_code": "99213", "billing_code_type": "CPT"}], **item("97110", groups=A)}]}).encode(), None, [("111111111", "1111111111")]),
 "decoy_in_string": (json.dumps({**HDR, "in_network": [{"description": "\"billing_code\": \"99213\", }]", **item("97110", groups=A)}]}).encode(), None, [("111111111", "1111111111")]),
}

if __name__ == "__main__":
    bad = 0
    for name, (data, ext, expect) in SHAPES.items():
        r = run(data, ext)
        vals = {k: (v[0], v[1], v[2]) for k, v in r.items()}
        base = vals["classic"]
        diff = [k for k, v in vals.items() if v != base and v[0] != "DECLINED"]
        got = None if base[0] in ("ERR", "DECLINED") else sorted({(t, n) for _c, t, n, _r in base[1]})
        wrong = expect is not None and got != sorted(expect)
        summary = {k: (v[0] if v[0] not in ("ERR", "DECLINED") else f"{v[0]}:{v[1]}") for k, v in r.items()}
        flag = ("DIFF " if diff else "same ") + ("WRONG " if wrong else "      ")
        print(flag + f"{name:30s}", summary)
        if diff or wrong or "-v" in sys.argv:
            bad += 1
            for k in ["classic"] + [d for d in diff]:
                v = r[k]
                print("      ", f"{k:18s}", v[1] if v[0] != "ERR" else v[1], v[2])
            if wrong:
                print("       expected (tin, npi):", sorted(expect))
    print("shapes with a difference or a wrong answer:", bad)
