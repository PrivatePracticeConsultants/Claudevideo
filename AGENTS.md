# Agent instructions

This repo's agent orientation lives in two files — read them in this order:

1. **`CLAUDE.md`** (repo root) — the 60-second version: what this project is,
   which of the two codebases to work in (`mrfx/` — never extend `src/`),
   build/run/test commands, the six invariants you must not break, and the
   ground rules.
2. **`docs/AI_HANDOFF.md`** — the authoritative deep-dive. Read it IN FULL
   before making any substantive change: module map, invariants in detail,
   URL-pipeline states, measured performance envelope, the recipe for adding
   a new payer source, how to test, and known limitations.

Everything in both files is verified against the code at HEAD (commands,
config defaults, function names, counts). User-facing setup lives in
`GETTING_STARTED.md`; the product overview is `README.md`.
