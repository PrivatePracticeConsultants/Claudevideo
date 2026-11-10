"""Write every client-facing document / JSON for one subject+market into a folder (for grep and Chromium)."""
import io, json, os, re, zipfile

def dump(c, outdir, subject, market, payer, volumes=None, state=None):
    os.makedirs(outdir, exist_ok=True)
    S = {"subject": subject, "market": market}
    vol = volumes or {"97110": 3000, "97140": 1500}
    reqs = {
        "pitch.html": ("/api/report/pitch", {**S, "volumes": vol}),
        "negotiation.html": ("/api/report/negotiation", {**S, "volumes": vol}),
        "payer_compare.html": ("/api/report/payer-compare", {**S, "payer": payer}),
        "proposal.html": ("/api/report/proposal", {**S, "payer": payer, "volumes": vol, "target": {"kind": "p50"}}),
        "ratecard.html": ("/api/report/ratecard", S),
        "fee.csv": ("/api/schedule/fee.csv", S),
        "dossier.html": ("/api/report/dossier", S),
        "market.html": ("/api/report/market", {"market": market, "state": state or market.get("state")}),
        "benchmark.json": ("/api/benchmark/market", S),
        "opportunity.json": ("/api/benchmark/opportunity", {**S, "volumes": vol}),
        "compare.json": ("/api/negotiate/compare", {**S, "payer": payer}),
        "proposal.json": ("/api/negotiate/proposal", {**S, "payer": payer, "volumes": vol, "target": {"kind": "p50"}}),
        "fee.json": ("/api/schedule/fee", S),
        "dossier.json": ("/api/report/dossier.json", S),
        "gaps.json": ("/api/contract-gaps", S),
        "size.json": ("/api/quality/size", S),
        "subject_types.json": ("/api/quality/subject-types", S),
        "types.json": ("/api/quality/types", {"market": market}),
        "posture.json": ("/api/quality/posture", {"market": market}),
        "leverage.json": ("/api/leverage", {**S, "payer": payer}),
        "leverage_summary.json": ("/api/leverage/summary", S),
        "visit.json": ("/api/economics/visit", S),
        "weighted.json": ("/api/economics/weighted-position", {**S, "volumes": vol}),
        "leads.json": ("/api/leads", {"market": market, "threshold_percentile": 99, "min_codes": 1}),
        "leads.csv": ("/api/leads.csv", {"market": market, "threshold_percentile": 99, "min_codes": 1}),
        "leaderboard.json": ("/api/leaderboard", {"market": market, "min_codes": 1}),
        "roster.json": ("/api/roster", {"market": market, "payer": payer}),
        "roster.csv": ("/api/roster.csv", {"market": market, "payer": payer}),
        "trajectory.json": ("/api/trajectory", {"market": market}),
        "renewals.json": ("/api/contracts/renewals", {"market": market, "subjects": [subject]}),
        "new_to_network.json": ("/api/contracts/new-to-network", {"market": market}),
        "changes.json": ("/api/changes", S),
        "hospital_parity.json": ("/api/hospital/parity", {**S, "payer": payer, "state": market.get("state")}),
        "floors.json": ("/api/floors/compare", {**S, "payer": payer, "state": market.get("state")}),
        "medicare_org.json": ("/api/medicare/org", {"subject": subject}),
    }
    codes = {}
    for fn, (url, body) in reqs.items():
        r = c.post(url, json=body)
        codes[fn] = r.status_code
        open(os.path.join(outdir, fn), "w").write(r.text)
    r = c.post("/api/report/org-bundle.zip", json=S)
    codes["org-bundle.zip"] = r.status_code
    if r.status_code == 200:
        open(os.path.join(outdir, "org-bundle.zip"), "wb").write(r.content)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        for n in z.namelist():
            p = os.path.join(outdir, "bundle", n); os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(z.read(n))
    else:
        open(os.path.join(outdir, "org-bundle.err"), "w").write(r.text)
    q = "&".join(f"{k}={v}" for k, v in (("grain", "tin"), ("dollar_only", 1), ("state", market.get("state") or ""), ("month", market.get("month") if market.get("month") not in (None, "latest") else "")) if v != "")
    r = c.get("/api/export/outreach.csv?" + q); codes["outreach.csv"] = r.status_code
    open(os.path.join(outdir, "outreach.csv"), "w").write(r.text)
    r = c.get("/api/export/outreach.zip?" + q); codes["outreach.zip"] = r.status_code
    if r.status_code == 200:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        for n in z.namelist():
            p = os.path.join(outdir, "outreach_zip", n); os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(z.read(n))
    return codes

def text(path):
    h = open(path, encoding="utf-8", errors="replace").read()
    h = re.sub(r"<style.*?</style>", " ", h, flags=re.S)
    h = re.sub(r"<svg.*?</svg>", " [svg] ", h, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))
