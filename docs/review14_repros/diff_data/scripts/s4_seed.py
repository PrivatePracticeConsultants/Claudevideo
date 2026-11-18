"""A market store for old-vs-new output diffs: ties, negative gaps, a
placeholder-only practice (NULL-ish median), SSN-pattern TINs, two states,
eval codes 97162/97166/92523, an entity map."""
import random


def seed(store):
    from mrfx.store import FINISHED_NOW
    rnd = random.Random(7)
    codes = ["97110", "97112", "97140", "97162", "97166", "92523", "97530"]
    payers = ["Alpha", "Beta", "Gamma"]
    tins = [f"43{i:07d}" for i in range(40)] + ["078051120", "219099999"]   # SSN patterns
    disc = {"97162": "pt", "97166": "ot", "92523": "slp"}
    for pi, payer in enumerate(payers):
        recs = []
        for ti, tin in enumerate(tins):
            if (ti + pi) % 5 == 0:
                continue
            for code in codes:
                if (ti * 3 + len(code) + pi) % 4 == 0:
                    continue
                for m in ("2026-05", "2026-06"):
                    for k in range(1 + ti % 2):
                        rate = 40.0 + (ti % 6) * 2.5 + pi + rnd.choice([0, 0, 0.005, 0.015])
                        if ti == 7:
                            rate = 0.01                      # placeholder-only practice
                        if ti in (12, 13, 14):
                            rate = 50.0                      # exact ties
                        recs.append(dict(
                            payer=payer, tin_value=tin, tin_type="ein",
                            npi=f"1{ti:05d}{k:04d}", source_file=f"{payer}.json",
                            billing_code=code, billing_code_type="CPT",
                            discipline=disc.get(code, rnd.choice(["pt", "ot", "unspecified"])),
                            is_timed=True, billing_class="professional",
                            negotiated_rate=rate, negotiated_type="negotiated",
                            is_dollar_rate=True,
                            billing_code_modifier=rnd.choice([[], [], ["GP"], ["59"]]),
                            service_code=rnd.choice([["11"], ["11", "22"]]),
                            file_month=m, last_updated_on=m + "-01", expiration_date=None,
                            schema_version="2.0.0", tin_is_really_npi=False,
                            ingested_at="2026-11-01"))
        with store.rates_part_writer(f"{payer}.json") as w:
            w.write_batch(recs)
        store.upsert_file(f"{payer}.json", payer=payer, file_type="in_network", status="done",
                          rows_emitted=len(recs), finished_at=FINISHED_NOW)
    for ti, tin in enumerate(tins):
        for k in range(2):
            st = "MO" if ti % 3 else "KS"
            store.save_npi(f"1{ti:05d}{k:04d}", f"CLINIC {ti % 9}" if k == 0 else f"PERSON {ti}",
                           "261QP2000X" if k == 0 else "225100000X", "x", f"City{ti % 4}", st,
                           entity_type="NPI-2" if k == 0 else "NPI-1", zip_code="64105",
                           address=f"{ti} Main St", phone="5555550100")
    # an entity map joining several TINs (entity grain)
    store.set_entity_map({tins[1]: "BIG GROUP", tins[2]: "BIG GROUP", tins[40]: "BIG GROUP",
                          tins[12]: "TIE CO", tins[13]: "TIE CO"})
