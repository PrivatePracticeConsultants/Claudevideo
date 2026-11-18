"""Hand-made store with the hard shapes for the sliced-spine identity check."""
import random

PAYERS = ["Alpha", "Beta", "O'Neil Health", "Zeta"]
CODES = ["0001U", "92507", "97110", "97112", "97140", "97530", "G0283", "abc"]


def rows_for(payer_i, payer, file_tag, months, seed):
    rnd = random.Random(seed)
    recs = []
    for t in range(14):
        # TINs 0..5 are SHARED by every payer (same tin_value), 6.. are per payer
        tin = f"55500{t:04d}" if t < 6 else f"6{payer_i}{t:07d}"
        tin_npi = False
        ttype = "ein"
        if t == 3:            # a 10-digit NPI published in the TIN slot (untyped)
            tin, tin_npi, ttype = "1234567893", True, "npi"
        if t == 4:            # 9 digits typed 'npi' colliding with an EIN string
            tin, tin_npi, ttype = "555000005", True, "npi"
        if t == 5:            # SSN-pattern TIN
            tin = "078051120"
        for code in CODES:
            if payer == "Zeta" and code != "97110":
                continue          # Zeta: ONE huge code -> hash halves
            for m in months:
                if t % 4 == 0 and m == months[-1]:
                    continue      # contract absent from the newest month
                for k in range(1 + (t % 3) + (4 if payer == "Zeta" else 0)):
                    npi = f"1{(t * 7 + k) % 9}{t:04d}{k:04d}"
                    rate = round(30 + payer_i * 3 + rnd.choice([0, 0, 1.5, 7.25]) + k * 0.5, 2)
                    if t == 7 and k == 0:
                        rate = 0.01     # placeholder
                    recs.append(dict(
                        payer=payer, tin_value=tin, tin_type=ttype, npi=npi,
                        source_file=file_tag, billing_code=code,
                        billing_code_type="CPT", discipline=rnd.choice(["pt", "ot", "slp"]),
                        is_timed=True, billing_class=rnd.choice(["professional", "institutional"]),
                        negotiated_rate=rate, negotiated_type="negotiated",
                        is_dollar_rate=True,
                        billing_code_modifier=rnd.choice([["GP"], [], ["GP", "59"]]),
                        service_code=rnd.choice([["11"], ["11", "22"], []]),
                        file_month=m, last_updated_on=m + "-01", expiration_date=None,
                        schema_version="2.0.0", tin_is_really_npi=tin_npi,
                        ingested_at="2026-11-01"))
    return recs


def seed(store, extra_months=None):
    from mrfx.store import FINISHED_NOW
    files = []
    for pi, payer in enumerate(PAYERS):
        # each payer split over TWO files (months split over files)
        for fi, months in enumerate((["2026-05", "2026-06"], ["2026-07"])):
            fn = f"p{pi}_f{fi}.json"
            recs = rows_for(pi, payer, fn, months, seed=pi * 10 + fi)
            with store.rates_part_writer(fn) as w:
                w.write_batch(recs)
            store.upsert_file(fn, payer=payer, file_type="in_network", status="done",
                              rows_emitted=len(recs), finished_at=FINISHED_NOW)
            files.append(fn)
    # names for some NPIs (directory)
    for t in range(14):
        for k in range(3):
            npi = f"1{(t * 7 + k) % 9}{t:04d}{k:04d}"
            if (t + k) % 2 == 0:
                store.save_npi(npi, f"CLINIC {t % 5}", "261QP2000X" if k == 0 else "225100000X",
                               "x", f"City{t % 3}", ["MO", "KS"][t % 2],
                               entity_type="NPI-2" if k == 0 else "NPI-1", zip_code="64105")
    return files


def dump(store):
    with store.connect() as con:
        spine = con.execute("SELECT * FROM rates_by_tin_tbl ORDER BY ALL").fetchall()
        d = con.execute("SELECT * FROM tin_directory_tbl ORDER BY ALL").fetchall()
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='rates_by_tin_tbl' ORDER BY ordinal_position").fetchall()]
    return spine, d, cols
