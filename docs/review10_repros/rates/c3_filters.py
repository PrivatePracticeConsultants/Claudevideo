"""Explorer filters on the messy fixture (fxm), hand-checked; plus the market report's '-0.0%'."""
import sys, re, json
sys.path.insert(0, "/tmp/mrfx_review10/rates/repros")
from fxm import *  # noqa
import fx

def rows(c, q):
    r = c.get("/api/rates?page_size=500&" + q)
    assert r.status_code == 200, (q, r.status_code, r.text[:200])
    return r.json()

def show(label, d, expect):
    got = sorted((x["display_name"], x["payer"][:5], x["modifier_set"], x["billing_class"][:4], x["file_month"], x["negotiated_rate"]) for x in d["rows"])
    print(f"{label}: total {d['total']}  expected {expect}")
    return got

def main():
    cfg, store = build()
    c = client(cfg, store)
    S = "q=" + S1 + "&cpt=97110&grain=tin"
    g = show("S1 all rows, dollar only, all months", rows(c, S + "&dollar_only=1"), "7 = 04: A40 B42; 06: A40 A34(CQ) A41(KX) A100(inst) B42 [pct row and the 0.01 are not separate rows]")
    for x in g: print("    ", x)
    g = show("S1 dollar_only=0", rows(c, S + "&dollar_only=0"), "8 (+ the 80% row)")
    show("S1 latest", rows(c, S + "&dollar_only=1&latest=1"), "5")
    show("S1 latest base", rows(c, S + "&latest=1&modifier=base"), "3 = A40 A100(inst) B42")
    show("S1 latest base professional", rows(c, S + "&latest=1&modifier=base&billing_class=professional"), "2")
    show("S1 latest assistant", rows(c, S + "&latest=1&modifier=assistant"), "1")
    show("S1 latest KX", rows(c, S + "&latest=1&modifier=KX"), "1")
    show("S1 latest mod_not CQ,KX", rows(c, S + "&latest=1&mod_not=CQ,KX"), "3")
    show("S1 latest rate 41..42", rows(c, S + "&latest=1&rate_min=41&rate_max=42"), "2 = A41(KX) B42")
    show("S1 latest hide outliers", rows(c, S + "&latest=1&hide_outliers=1"), "5 (100 < 5 x median)")
    d = rows(c, "cpt=97110&grain=tin&latest=1&rate_max=41&payer=Alpha%20Health&q=" + C)
    show("C Alpha latest rate_max=41 (C was 40 in 04, 50 in 05/06)", d, "0 — the superseded April $40 must not come back")
    d = rows(c, "cpt=97110&grain=tin&month=2026-04&rate_max=41&payer=Alpha%20Health&q=" + C)
    show("C Alpha month 2026-04 rate_max=41", d, "1")
    show("NPI search 10 digits at tin grain (NPI of S1)", rows(c, "grain=tin&q=1" + S1), "0 at TIN grain (an NPI is not a TIN)")
    show("NPI search at npi grain", rows(c, "grain=npi&latest=1&cpt=97110&q=1" + S1), "5")
    show("name search 'acme' entity grain latest 97110", rows(c, "grain=entity&latest=1&cpt=97110&q=acme"), "8 = MO 5 + KS 3")
    show("state KS entity grain latest 97110", rows(c, "grain=entity&latest=1&cpt=97110&state=KS"), "ACME KS 3 + K1 2 (Alpha, Gamma) + K2 + K3 + TWO STATE PT 1 = 8")
    show("state ks lower case", rows(c, "grain=tin&latest=1&cpt=97110&state=ks"), "8")
    show("city town", rows(c, "grain=tin&latest=1&cpt=97110&city=town&payer=Gamma%20Health"), "3")
    show("discipline PT", rows(c, "grain=tin&latest=1&cpt=97110&discipline=PT&payer=Gamma%20Health"), "3")
    show("pos 11 + payer Delta (latest)", rows(c, "grain=tin&latest=1&pos=11&payer=Delta%20Health"), "2")
    show("wildcard: modifier=% ", rows(c, S + "&latest=1&modifier=%25"), "0 (no modifier is literally '%')")
    show("wildcard: q=_ (underscore)", rows(c, "grain=tin&latest=1&cpt=97530&q=_"), "0 (no name contains an underscore)")
    for sub in ("ACME REHAB", "ACME REHAB · KS", "BIG GROUP", "acme"):
        r = c.post("/api/clients", json={"subject": sub}); 
    dg = c.get("/api/clients/digest").json()
    for r in dg["clients"]:
        print("   digest:", r["subject"], "->", r.get("display_name"), "| changes", (r.get("changes") or {}).get("n"), (r.get("changes") or {}).get("span"), "| gaps", r.get("gaps"), "| error", r.get("error"))

    # ---- '-0.0%' in the market report's city table
    cfg2, store2 = fx.mk()
    A = [f"61000000{i}" for i in range(1, 6)]; Bt = [f"62000000{i}" for i in range(1, 6)]
    npis = {t: ["1" + t] for t in A + Bt}
    prices = {"97110": {**{t: 47.99 for t in A}, **{t: 48.01 for t in Bt}}}
    ingest(cfg2, store2, [("alpha_2026-06.json", doc("Alpha Health", "2026-06-01", prices, npis))])
    for t in A: name(store2, "1" + t, "A" + t[-1] + " REHAB", "MO", city="ALPHAVILLE")
    for t in Bt: name(store2, "1" + t, "B" + t[-1] + " REHAB", "MO", city="BETATOWN")
    store2.rebuild_rollups()
    c2 = client(cfg2, store2)
    h = c2.post("/api/report/market", json={"market": {"month": "latest"}, "state": "MO"}).text
    print("market report city rows:", re.findall(r"<td>(Alphaville|Betatown)</td><td class='num'>([^<]*)</td><td class='num'>([^<]*)</td>", h))
    print("   hand: state median = (47.99+48.01)/2 = 48.00; Alphaville 47.99 = -0.02% -> '0.0%' (never '-0.0%'); Betatown +0.02% -> '+0.0%'")
    print("   footer has the market in words:", "practices in MO" in h, "| 'Market definition' present:", "Market definition" in h)
    print("   ", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))[-900:])

if __name__ == "__main__":
    main()
