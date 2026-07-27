# LAST LINE

Roguelite tower defense. Godot 4.x / GDScript. **3D, and it runs in a browser.**

**Status: phase P0 complete and audited.** One map, one enemy, one platform, ten
waves, win/lose — on a deterministic 30Hz simulation with the architecture the
later phases need already in place. The design document is the master plan (v2);
the phase ladder is §5.7 of it.

This tree is self-contained and shares nothing with MRF Explorer, the Python
product that occupies the rest of this repository.

## Run it

Needs Godot 4.x (built and verified against 4.5).

```bash
godot --path game                            # play
GODOT=/path/to/godot ./game/run_tests.sh     # the suite, headless
GODOT=/path/to/godot ./game/build_web.sh     # HTML5 build -> build/web/
python3 game/tools/verify_web.py             # boot the web build in a real browser
```

The web build is single-threaded on purpose, so it needs no COOP/COEP headers and
will run from any static host. It will **not** run from `file://` — browsers
block wasm there. Serve it:

```bash
python3 -m http.server 8000 --directory build/web   # then open localhost:8000
```

### Playable link (GitHub Pages)

A built copy is committed to `docs/play/` and served by GitHub Pages, so the game
can be opened in a browser with no toolchain at all:

**https://privatepracticeconsultants.github.io/Claudevideo/play/**

Pages is configured to deploy from the `claude/roguelite-tower-defense-plan-vmprb4`
branch, `/docs` folder. Two things to know:

- `docs/play/` is a **build artifact checked into git** (~37 MB, almost all of it
  the Godot wasm runtime). It is the price of a click-to-play link on a repo with
  no CI. Refresh it with `./game/build_web.sh && cp build/web/* docs/play/`, and
  avoid re-committing it casually — each rebuild adds another copy to history.
  If this repo ever gets CI, building it there and publishing to a `gh-pages`
  branch is the better arrangement.
- If that branch is deleted after merging, the link dies. Repoint Pages at
  whichever branch then holds `docs/`.

`docs/.nojekyll` stops Pages running Jekyll over the build.

In game: click a pad to build · `1`/`2`/`3` speed · `space` pause · `F3` debug
overlay · `R` restart · `esc` quit.

## What P0 actually guarantees

The acceptance gate for this phase was: *play a ten-wave engagement start to
finish; same seed + inputs produce an identical end state, test-proven.* Both
halves are covered by `tests/cases/test_engagement.gd` and
`tests/cases/test_determinism.gd`.

The current suite is 84 tests / ~35,400 assertions, all green — including
`test_audit.gd`, the regression tests for seven defects a deliberate
break-it pass found after P0 was first written. Every one of them was a *silent*
failure rather than a crash; `DECISIONS.md` (P0-17) has the table.

## Layout

```
core/       deterministic simulation — no Node, no scene tree, no engine time
  sim.gd            the tick; entity pools; wave director; targeting; combat
  database.gd       loads and validates every balance value from data/
  rng.gd            the only sanctioned source of randomness (PCG-XSH-RR)
  spatial_hash.gd   allocation-free broadphase for targeting queries
  state_hash.gd     bit-exact fingerprint of simulation state
data/       every balance value in the game, as JSON
render/     3D drawing (MultiMesh + interpolation), camera, debug overlay
ui/         HUD
tools/      headless dev utilities (screenshot capture, render stress, web verify)
tests/      the suite; run_tests.gd is the headless entry point
```

## The rules this code is built to

Four constraints shape almost every decision in `core/`. They are cheap to hold
now and extremely expensive to retrofit, which is why they are in place before
there is much of a game.

1. **The simulation is deterministic.** Same data + same seed + same command log
   produces a bit-identical end state. Replays, seeded Daily Contracts, and the
   headless balance sim that keeps the content tuned all reduce to this.
2. **No balance constants in code.** If a number changes how the game plays, it
   is in `data/*.json`.
3. **No allocations in the per-tick path.** Everything spawned comes from a
   preallocated pool.
4. **Entities render through MultiMesh**, never one node per entity.

The renderer is 3D and the simulation is not — the sim's world is flat `(x, y)`
and the renderer maps it to `(x, 0, y)`. Moving from 2D to 3D changed **no game
logic at all**, which is the clearest evidence available that rule 1 is earning
its keep.

Rules 1 and 2 are enforced mechanically by `tests/cases/test_sim_purity.gd`,
which fails the build on a stray `randf()`, `Vector2`, `pow()` or magic number in
simulation code. Rule 4 is guarded structurally by `tests/cases/test_renderer.gd`
and measured directly by `tools/render_stress.gd`.

If you are picking this up: read `DECISIONS.md` first. It explains why each of
these is the way it is, and what would justify changing it.

## Dev tools

```bash
# Drive the game to an arbitrary state and save a PNG — no monitor required.
xvfb-run -a godot --path game --rendering-driver opengl3 \
    --script res://tools/capture_screenshot.gd -- --wave 6 --overlay --out shot.png

# Verify draw calls stay flat as entity count climbs.
xvfb-run -a godot --path game --rendering-driver opengl3 \
    --script res://tools/render_stress.gd
```

Scope discipline: ideas beyond the current phase go in `post-launch.md`, not into
the code.
