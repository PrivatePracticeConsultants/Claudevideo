# Agent instructions

**This repository holds two unrelated projects. Work out which one you are here
for before you read anything else — they share no code, no data, no
dependencies, and no conventions.**

| You were asked about… | Work in | Start with |
|---|---|---|
| negotiated healthcare rates, MRFs, payers, DuckDB, the dashboard | `mrfx/` | `CLAUDE.md`, then `docs/AI_HANDOFF.md` |
| LAST LINE, the tower defense game, Godot, GDScript, the playable link | `game/` | **`game/AGENTS.md`** |

Nothing written for one applies to the other. In particular, the invariants in
`CLAUDE.md` (streaming parsers, single DB writer, the honesty contract) are
about the Python product and say nothing about the game; the game's own
invariants are in `game/AGENTS.md`.

`src/` + `run.py` are an earlier, single-purpose BCBS-Missouri extractor kept
for reference. **Do not extend them.** All new Python work is in `mrfx/`.

---

## MRF Explorer (`mrfx/`) — the Python product

Read in this order:

1. **`CLAUDE.md`** (repo root) — the 60-second version: what it is, build/run/test
   commands, the six invariants you must not break, and the ground rules.
2. **`docs/AI_HANDOFF.md`** — the authoritative deep-dive. Read it IN FULL
   before any substantive change: module map, invariants in detail,
   URL-pipeline states, measured performance envelope, the recipe for adding a
   payer source, how to test, and known limitations.

User-facing setup is `GETTING_STARTED.md`; the product overview is `README.md`.

## LAST LINE (`game/`) — the Godot game

Read **`game/AGENTS.md`** first. It is written for an agent picking this up
cold, from any vendor, and covers the environment quirks that will otherwise
cost you an hour, the invariants that are mechanically enforced, the working
method the codebase expects, and what is currently open.

Then `game/README.md` (what the game is and how it is built) and
`game/DECISIONS.md` (why every non-obvious thing is the way it is — 84 entries,
each one a decision with the measurement behind it).

---

Everything in these files is kept verified against the code at HEAD — commands,
config defaults, function names, counts. If you change behaviour, update the doc
in the same commit.
