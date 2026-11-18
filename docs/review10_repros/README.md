# Review 10 reproductions

Evidence for `docs/NEXT_STEPS_PROMPT_10.md`; each item there names its script
as `<area>/<script>`. Reviewer-written; the paths at the top of each script
point at `/tmp/mrfx_review10/<area>/` — change them to your scratch folder.
Trials live outside the repository and never touch the user's `data/`.

- `search/` — search latency: `ui_search.py` (Chromium typing at 120 / 400
  ms per key, every request timed), `abandoned.py`, `explain_search.py`,
  `exp_units.py`, `exp_in.py`, the prototype diffs `protoA_*.diff` (abort +
  server-side interrupt, one debounce, `/api/boot`) and `protoB_*.diff`
  (practice list per generation, late name join), result tables
  (`req_table.md`, `ui_requests_*.md`, `ab_bodies_scale.md`) and `FINDINGS.md`.
- `rates/` — Overview, Clients, Explorer, Code comparison, Markets, Rate
  changes, Sources: fixture builders (`fx.py`, `fxm.py`, `c2_hypo.py`,
  `c3_filters.py`), real-store scripts (`r_*.py`, need a served store),
  in-process ones (`p1_entity_explain.py`, `p2_entity_proto.py`, `q1_db.py`,
  server stopped), the Chromium pass `ui_rates.py`. Its findings are written
  out in full in the prompt (Groups 1.7–1.9, 2.2–2.5, 3, 4).
- `consulting/` — Consulting, Growth and every client-facing document, with
  `FINDINGS.md`; `fxf.py` adds a Medicare anchor, utilization, floors, a
  hospital file and ACS to the fixture.
- `SNAPSHOT_0.7.2_vs_0.7.3.md` — every endpoint's body under both builds.
