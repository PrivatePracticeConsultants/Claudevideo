import ast, sys, pathlib
if __name__ == "__main__":
    root = pathlib.Path(sys.argv[1])
    for f in ["benchmark.py","schedule.py","market.py","economics.py","api.py","leads.py","quality.py","packets.py","dossier.py","monitor.py","overview.py","territory.py","leverage.py","contracts.py","visit.py","spark.py","outreach.py","clients.py","utilization.py","hospital.py","floors.py","inflation.py","engagements.py","orgprofile.py","medicare.py","demographics.py"]:
        p = root / f
        if not p.exists(): continue
        t = ast.parse(p.read_text())
        hits = [n.lineno for n in ast.walk(t) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "round"]
        if hits: print(f, hits)
