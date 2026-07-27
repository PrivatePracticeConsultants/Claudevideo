# LAST LINE

Roguelite tower defense. Godot 4.x / GDScript.

**Status: phase P0 complete.** One map, one enemy, one platform, ten waves,
win/lose — on a deterministic 30Hz simulation with the architecture the later
phases need already in place. The design document is the master plan (v2); the
phase ladder is §5.7 of it.

This tree is self-contained and shares nothing with MRF Explorer, the Python
product that occupies the rest of this repository.

## Run it

Needs Godot 4.x (built and verified against 4.5).

```bash
godot --path game                   # play
GODOT=/path/to/godot ./game/run_tests.sh    # the suite, headless
```

In game: click a pad to build · `1`/`2`/`3` speed · `space` pause · `F3` debug
overlay · `R` restart · `esc` quit.

## What P0 actually guarantees

The acceptance gate for this phase was: *play a ten-wave engagement start to
finish; same seed + inputs produce an identical end state, test-proven.* Both
halves are covered by `tests/cases/test_engagement.gd` and
`tests/cases/test_determinism.gd`.

The current suite is 69 tests / ~35,000 assertions, all green.

## Layout

```
core/       deterministic simulation — no Node, no scene tree, no engine time
  sim.gd            the tick; entity pools; wave director; targeting; combat
  database.gd       loads and validates every balance value from data/
  rng.gd            the only sanctioned source of randomness (PCG-XSH-RR)
  spatial_hash.gd   allocation-free broadphase for targeting queries
  state_hash.gd     bit-exact fingerprint of simulation state
data/       every balance value in the game, as JSON
render/     MultiMesh drawing + interpolation; debug overlay
ui/         HUD
tools/      headless dev utilities (screenshot capture, render stress)
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
