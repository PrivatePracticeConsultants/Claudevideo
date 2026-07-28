# LAST LINE

Roguelite tower defense. Godot 4.x / GDScript. **3D, and it runs in a browser.**

**Status: P0 complete and audited three times, then extended well past it.**
Forty-eight levels — twelve boards played as four acts each, where every act
opens more of the same road and keeps what you built on the last one — four
weapon families with four tiers each, five drone classes, free placement on
purchasable ground, saved progress, all on a deterministic 30Hz simulation drawn
with real lighting, shadows and depth cueing. The design document is the master plan
(v2); the phase ladder is §5.7. Formally this is P0 plus much of P1/P2's content;
the run layer (P3) is the next real milestone.

This tree is self-contained and shares nothing with MRF Explorer, the Python
product that occupies the rest of this repository.

## Run it

Needs Godot 4.x (built and verified against 4.5).

```bash
godot --path game                            # play
GODOT=/path/to/godot ./game/run_tests.sh     # the suite, headless
GODOT=/path/to/godot ./game/run_tests.sh --full   # ...playing every level end to end
GODOT=/path/to/godot ./game/run_tests.sh -- --case test_engagement   # one file
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

## How to play

Drones walk the corridor end to end. Anything that reaches the far end costs
Corridor Integrity. Hit zero and the level is lost.

| Drone | Health | Speed | Leak cost | Pays | |
|---|---|---|---|---|---|
| Skitter Drone | 22 | 140 | 1 | $7 | fast, fragile, numerous |
| Sentry Walker | 55 | 90 | 4 | $16 | the baseline |
| Bulwark Hauler | 260 | 55 | 12 | $58 | slow and very tough |
| **Vanguard Lance** | **620** | **168** | 11 | $150 | fastest *and* tough |
| **Siege Breaker** | **1400** | 78 | 16 | $300 | shrugs off 80% of slows |

The first three trade speed against health. The **Lance** does not — it is the
fastest thing on the board *and* tougher than anything that is not slower than
it, so a turret gets less time on a target that needs more damage. A thin line of
fire that held everything else lets Lances through.

The **Breaker** exists because the Arc Suppressor does: once slowing everything
was possible, slowing everything was the answer to everything. Both arrive
partway into the campaign, never open a wave, and stay a small share of any
level's head-count — they are elites, not populations.

- **Click owned ground** (green) to build the selected weapon.
- **Click a turret** to upgrade it a tier. Hovering shows its DPS and next cost.
- **Click dim blue ground** to buy that cell, expanding where you can build.
  Ground can only be bought next to ground you already hold, and each purchase
  costs more than the last.
- **Right-click a turret** to sell it back for 65% of everything spent on it.
- **`E`** calls the next wave in early for a bounty — the gap between waves is
  when Capital accumulates, so it trades preparation for money.
- **Scroll to zoom** on whatever the cursor is over, **middle-drag to pan**,
  **`Z`** to reset the view. Boards run to 16,000 units, so framing one end to
  end makes a turret a few pixels wide.
- **`Q`** cycles weapon · **`1`–`4`** speed · **`space`** pause · **`[`**/**`]`**
  move between unlocked boards · **`F3`** debug · **`R`** restart · **`N`** next
  level after a win.

Progress is saved per board, so the campaign resumes where you left it.

Four families answering different problems:

| | Ballistic | Cannon | Arc Suppressor | Railgun |
|---|---|---|---|---|
| Damage | Single target | Area, falls off to the edge | Very low | Enormous, very slow |
| Does | Kills things | Kills crowds | Slows the blast radius | Pierces a whole lane |
| Best against | Bulwark Haulers | Skitter swarms | Vanguard Lances | Siege Breakers, columns |
| Tier 1 cost | $100 | $140 | $130 | $260 |

The Suppressor barely damages anything. It is a force multiplier: a slowed drone
spends longer inside everyone else's range, so a Suppressor makes the turrets
around it worth more. Slows refresh rather than stack, so massing them does not
pin a wave in place.

The Railgun is the opposite trade: one very slow, very long-ranged shot that
tears down a whole lane at full damage. Worth several turrets against a column on
a straight, and close to worthless against a scattered swarm.

You are capped at a **deployment limit** per level — 24 turrets at the first,
doubling every ten levels up to the pool ceiling of 144. It is also capped by how
much road the act has actually revealed, so act I of a board is a smaller board
than act III. Once your allowance is placed the only way to grow is to upgrade,
which is what makes *where* you put them matter.

### Boards that grow

Each board is four acts. Act I runs the first stretch of the road; act II opens
more of it; act III runs the whole thing; act IV runs it again under far heavier
pressure. **The road already revealed never moves**, so every turret and every
cell of ground you bought is still there, still covering what it covered, when
the corridor extends. Act I of a new board always starts clean.

**Every turret you built carries forward.** Two things do not:

- **Capital.** Each act's spending is its own decision.
- **Tiers.** Turrets arrive **refitted**, back to tier 1. You keep every
  placement, every weapon choice and all your ground; you re-earn the depth.
  Without it an act that ended tier-4 across the board simply wins the next one
  for you — measured, *every* carrying act in the campaign could be cleared by
  building nothing at all.

The price is deliberately paid in tiers rather than in emplacements. An earlier
version capped how many turrets could carry and deleted the rest; it balanced
fine and was the wrong trade.

Retrying an act (`R`) restores the same inheritance, not an empty board.

### Crossing to a new board

Boards are different maps, so turrets genuinely cannot follow — a coordinate on
Highway means nothing on Port. What follows is what they were worth: a finished
board **salvages into the opening Capital** of the next one, capped at 40% of
that act's own budget so it scales with the campaign rather than swamping it. A
board finished strongly opens the next one richer than one scraped through.

## Adding a weapon family

Weapons are data. A new family is a new key in `data/blueprints/blueprints.json`
and, if it needs a behaviour the sim does not already have, a field the sim reads.
Nothing about the campaign, the HUD, the renderer or the tests needs to know it
exists by name.

A family is a `tiers` array — one entry per upgrade step — and every entry needs:

```json
"laser": {
  "display_name": "Pulse Laser",
  "tiers": [
    { "name": "Mk I", "cost": 120, "damage": 8,
      "range_units": 210.0, "fire_interval_seconds": 0.25,
      "projectile_speed_units_per_second": 900.0,
      "projectile_hit_radius_units": 6.0,
      "projectile_lifetime_seconds": 1.5 }
  ]
}
```

`Database._validate_blueprints` enforces every one of those, so a typo is a
plain-language load error rather than a turret that quietly does nothing. Two
optional fields turn it into an area weapon: `splash_radius_units` and
`splash_min_fraction` (the share of full damage at the blast edge). That is all
the Cannon family is — it adds no code.

Once the key exists it is immediately buildable: `Q` cycles every family the data
defines, the HUD reads its name and cost from the same place, and the scripted
test policy will start using it. Turrets currently share one silhouette across
families and are tinted by tier (`SimRenderer3D._build_turret_layers`); a family
that plays differently enough to need its own shape wants the same treatment
`_enemy_mesh` gives drone classes.

**If the behaviour is genuinely new** — slowing, chaining, damage over time — it
needs sim support, and that means: state on the enemy pool (preallocated, never
grown), the new field read in `Database`, the state folded into `state_hash()` so
replays still prove out, and a test that the effect expires. Balance values stay
in JSON; `tests/cases/test_sim_purity.gd` fails the build on a number in code.

## What P0 actually guarantees

The acceptance gate for this phase was: *play a ten-wave engagement start to
finish; same seed + inputs produce an identical end state, test-proven.* Both
halves are covered by `tests/cases/test_engagement.gd` and
`tests/cases/test_determinism.gd`.

The suite includes `test_audit.gd` — regression tests for seven defects a
deliberate break-it pass found after P0 was first written. Every one was a
*silent* failure rather than a crash; `DECISIONS.md` (P0-17) has the table.

Campaign content is covered by a test that every chain is winnable by a scripted
competent policy and losable by an idle one, so a balance change that makes a
level impossible — or trivial — fails the build instead of being discovered in
play. It caught two unwinnable levels during authoring, and later caught a
generated curve whose *opening* level threw 2,223 drones at a 14-turret board.

It plays whole chains rather than single levels, because an act in the middle of
a chain is entered carrying the previous act's board — judging it from a standing
start would measure a game nobody plays. The losable half is correspondingly
strict: an act must still be losable **from the board it inherits**.

That test plays three of the eight chains by default, because playing all of them
is 24 full engagements — **over ten minutes of CPU**, more than some sandboxes
allow in one command. `--full` plays every one and is the pre-release run; give it
room, and use `-- --case test_engagement` to run that gate on its own. A separate
cheap test loads all twenty-four regardless, so a broken map or wave file fails
immediately.

`tools/balance_probe.gd` is the same measurement in a form you can watch: it plays
the whole campaign chained and prints a table, and `--sweep <level>` replays one
level across a range of health multipliers. That sweep is how the campaign's two
anchors were set, and it is the only sane way to tune a game whose "won, but it
cost something" band turned out to be about 16% wide.

## How it is drawn

Each weapon family has its own mount, housing and muzzle — a Ballistic is a boxy
receiver with a slim barrel, a Cannon a tapered mount with a short bore angled up
to lob, a Suppressor a smooth drum with a coil ring and no barrel at all, a
Railgun a low sled with a very long flat rail. Colour is a second cue, not the
only one.

The ground is a mottled grid rather than a flat quad, with a graded verge either
side of the road, a centre line, and scattered debris for scale. It rolls in the
distance and is dead flat anywhere a turret can stand: the simulation is 2D and
every placement rule is a distance in the ground plane, so relief under the
playable band would put turrets on slopes the rules know nothing about. All of it
is hashed from position rather than randomised, so a board looks the same every
time you open it.


Perspective at a narrow field of view, a key light with shadows plus a cool fill,
glow and filmic tonemapping — plus screen-space ambient occlusion **on Forward+
only**. SSAO does not exist on the Compatibility renderer, which is what the web
build runs on (WebGL 2), so it is asked for only where it is real and the ambient
term is lifted where it is not. Godot's own response to asking anyway was a
console warning and no pixels.

The narrow FOV is deliberate: wide enough for real depth, narrow enough that a
turret at the far edge of the board is close to the same size as one near the
camera, which is what keeps range and coverage judgeable.

Everything that scales with entity count is instanced — one `MultiMesh` layer per
enemy class (so class reads from silhouette, not only colour), plus health bars,
projectiles, buildable cells, and the three parts of every turret. The corridor
is a single generated mesh rather than a box per segment. Draw calls do not grow
with how much is happening.

## Layout

```
core/       deterministic simulation — no Node, no scene tree, no engine time
  sim.gd            the tick; entity pools; wave director; targeting; combat
  database.gd       loads and validates every balance value from data/
  rng.gd            the only sanctioned source of randomness (PCG-XSH-RR)
  spatial_hash.gd   allocation-free broadphase for targeting queries
  state_hash.gd     bit-exact fingerprint of simulation state
data/       every balance value in the game, as JSON
  building.json     placement rules and the purchasable-ground grid
  levels.json       campaign order
  blueprints/       weapon families and their tiers
  enemies/          drone classes
  maps/ waves/      one file per map, one per engagement
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

# Play the whole campaign chained, the way a player would, and print the table.
# Reports rather than asserts - this is the tuning loop, not a gate.
godot --headless --path game --script res://tools/balance_probe.gd
```

Scope discipline: ideas beyond the current phase go in `post-launch.md`, not into
the code.
