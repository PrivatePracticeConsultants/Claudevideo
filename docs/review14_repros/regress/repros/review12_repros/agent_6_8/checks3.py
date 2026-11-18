if __name__ == "__main__":
    from mrfx.modifiers import normalize
    import inspect; print(inspect.signature(normalize))
    for m, pos in ((["GP","59","GP"], None), (["11"], None), (["59","GP"], ["11"]), (["GP","11"], None), (["21","31","22","24"], None), (["CQ"], None), (None, None), ("GP", None), (["gp"], ["1","01"])):
        try: print("mod", m, pos, "->", normalize(m, pos))
        except Exception as e: print("mod", m, "EXC", type(e).__name__, e)
    from mrfx.benchmark import is_latest, normalize_market
    print("is_latest LATEST:", is_latest(normalize_market({"month": "LATEST"})), is_latest({"month": "Latest "}))
