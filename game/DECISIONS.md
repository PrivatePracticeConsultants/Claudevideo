# DECISIONS.md — LAST LINE

Running log of choices that later phases inherit. Each entry says what was
decided, why, and what would have to be true to revisit it. Append; don't
rewrite history.

Phase status: **P0 complete and audited, then extended well past it** — 3D, HTML5, free
placement, tower tiers, two weapon families, three enemy classes, purchasable
ground, and a six-level campaign. Formally this is P0 plus most of P1/P2's
content; the run layer (P3) is still absent.

---

## P0-1 · Enemy position is derived from one scalar, not stored as x/y

`e_prog` (distance travelled along the path) plus `e_offset` (fixed lateral
offset) is the authoritative state. Screen position is recomputed from it each
tick.

Why: it halves the mutable float state that has to stay bit-identical for
determinism; it makes the **First** targeting priority a single scalar compare
instead of a path-progress lookup; and it lets the renderer interpolate *along*
the path, so enemies round corners instead of cutting across them.

Revisit if: enemies ever need to leave the path (knockback, flying units taking
a direct line). Flying is P1 — it will likely need a second position mode rather
than a change to this one.

## P0-2 · Float64 scalars, never Vector2, inside the sim

Godot's `Vector2` is float32 in standard builds. Doubles are IEEE-754 and
reproducible across platforms; float32 accumulation is not reliably so.

The sim is also restricted to `+ - * /` and `sqrt`. `sqrt` is correctly rounded
by IEEE-754; `sin`/`cos`/`atan2`/`pow` are **not** specified to the last bit and
differ between libm implementations. This is why the path is precomputed into
per-segment unit vectors (no trigonometry to follow it), why perpendicular offset
uses the `(-dy, dx)` identity, and why wave scaling multiplies iteratively
instead of calling `pow`.

Enforced by `tests/cases/test_sim_purity.gd`.

Revisit if: a desync is ever observed despite these rules — at which point the
answer is fixed-point integer math, not more float rules.

## P0-3 · Determinism is enforced by a linter, not by review

Two rules fail silently and are easy to break by accident, especially with a
model generating volume: balance constants in code, and non-determinism in the
sim. `test_sim_purity.gd` greps the simulation sources for banned constructs
(`randf`, `Vector2`, `Time.`, `pow(`, …) and for numeric literals outside
`{0, 1, 2}`.

`rng.gd`, `state_hash.gd` and `database.gd` are exempt from the numeric rule and
the exemption is deliberate, not convenience: the first two hold published
algorithm constants (PCG, FNV-1a), and `database.gd`'s literals are schema floors
used to validate data files rather than values the game plays with.

The linter has a self-test (`test_the_linter_actually_catches_things`) so it
cannot quietly start matching nothing.

## P0-4 · Money and HP are integers

Capital, bounties, damage and HP are all `int`. Float money would add drift to
the state hash for no gameplay benefit. Rounding happens once, at wave start,
when the exponential scaling is applied.

## P0-5 · Pool slots carry a generation stamp

Recycled slots create an ABA hazard: a projectile in flight can land on a
brand-new enemy that inherited its dead target's index. `e_gen` is bumped on
despawn and stamped onto each projectile at fire time; a mismatch discards the
shot.

Shots aimed at something that died are **wasted**, not retargeted. That overkill
is real and the balance sim needs to see it. Covered by
`test_pools.test_a_projectile_cannot_hit_the_enemy_that_inherited_its_target_slot`.

## P0-6 · Ballistic shots home to their target

Straight-line shots at a moving target miss: over the 180-unit range at 900
units/second the flight is ~0.2s, in which a walker moves ~18 units — more than
the hit radius. The options were lead prediction (a quadratic solve, several edge
cases) or homing. P0 takes homing; at these speeds the curvature is invisible.

Revisit in P1: real ballistic lead is the more honest behaviour once projectile
speeds vary by family, and Missile's homing should then be what distinguishes it.

## P0-7 · Waves clear before the next one starts

Wave N+1 begins `inter_wave_delay_ticks` after the board is empty, not after
wave N finishes spawning. Overlapping was tried first: with a ~42s corridor
traversal and ~8s of spawning, everything from every wave ends up alive at once
and the engagement stops being readable.

Revisit if: engagement pacing feels slack in playtesting. Overlap is the standard
lever, and it is a wave-director change, not a data change.

## P0-8 · Own test harness instead of GUT

GUT is the right long-term choice and `tests/framework.gd` deliberately mirrors
its assertion names (`assert_eq`, `assert_true`, `assert_almost_eq`) so swapping
it in is mechanical. It could not be vendored in the environment P0 was built in
— the GitHub archive and API endpoints needed to fetch it are blocked — and
shipping untested code to avoid a hundred lines of harness was the worse trade.

Action for whoever has network access: vendor GUT into `addons/`, port the cases,
delete `framework.gd`. Nothing else depends on it.

## P0-9 · `run_tests.sh` always runs `--import` first

Godot resolves `class_name` globals from `.godot/global_script_class_cache.cfg`,
which is a build artifact and is not committed. Without a scan pass, every test
file fails to parse with "Identifier not declared" on a fresh checkout. This bites
in CI specifically.

## P0-10 · The game lives in `game/`, and `.gitignore` had to be fixed for it

This repo's primary product is MRF Explorer (Python). The game is a separate tree
under `game/` and shares nothing with it.

The root `.gitignore` had an unanchored `data/` rule, which also matched
`game/data/` — the directory holding every balance constant in the project. Those
files would have been silently untracked. The rule is now anchored to `/data/`,
which is what MRF Explorer actually meant (`config/mrfx.yaml` resolves its store
paths from the repo root).

## P0-11 · The debug overlay reports allocation *proxies*, and says so

Godot exposes no per-frame allocation counter. The overlay reports live object
count and static memory, plus the per-second delta of each, and labels them as
proxies. Both should sit flat mid-wave; a number that climbs while entity counts
are steady is the signal that something in the tick path is allocating.

Claiming a measurement the engine is not making would be worse than not having
one.

## P0-12 · Balance is a starting point, and it is measured, not asserted

The shipped engagement was tuned by sweeping `base_hp` against two scripted
policies and reading the outcomes:

| base_hp | fill-every-pad | build nothing |
|---|---|---|
| 40 | win, 100 integrity, 0 leaks | loss, wave 4 |
| **55** | **win, 72 integrity, 7 leaks, still 100 entering wave 8** | **loss, wave 4** |
| 70 | loss at wave 10 | loss, wave 4 |

55 was chosen because a naive fill wins but takes all its damage in the last
three waves — the authored spike the plan asks for in §4.2 — while 70 makes even
a full board lose, so placement *order* is a real skill axis.

This is not final balance. It is a starting point for `tools/balance_sim.py`
(P3), which the two policies in `tests/sim_fixture.gd` are the direct ancestor of.

## P0-13 · Rendering was verified, not assumed

`tools/render_stress.gd` measures the P0 rendering constraint directly. Result:

| entities (enemies + projectiles) | draw calls, 2D | draw calls, 3D |
|---|---|---|
| 0 | 18 | 124 |
| 50 | 22 | 141 |
| 200 | 22 | 141 |
| 500 | 22 | 141 |

Draw calls are flat from 25 to 250 enemies in both renderers — that is the
MultiMesh constraint holding. The three entity layers cost a fixed +4 (2D) or
+17 (3D) regardless of how many entities they draw.

The 3D baseline of 124 is static scenery, not entities: the corridor is a mesh
instance per segment per wall and every pad is its own cylinder, doubled by the
shadow pass. Fine at this stage, worth merging into one mesh before the entity
budget gets tight — logged in `post-launch.md`.

`tests/cases/test_renderer.gd` guards the structure that produces this in the
headless suite.

Frame times from that tool are **llvmpipe software rasterisation** on a headless
box and are not a device measurement. The 250-enemies-at-60fps-on-an-iPhone-11
target has to be measured on the device, and that is P1's acceptance gate.

## P0-15 · The renderer is 3D; the simulation is not

The game renders in 3D with an orthographic 3/4 camera. **The simulation did not
change by one line to support it.** Its world is flat `(x, y)` with no concept of
height, and the renderer maps that to `(x, 0, y)` — the third dimension is
presentation only, defined in exactly one place (`SimRenderer3D.to_world`).

That is the payoff of keeping `core/` free of engine types, and it is worth
stating plainly because it is the strongest available evidence that the
architecture is right: swapping the entire presentation layer touched no game
logic, and the determinism, economy, pathing and wave tests all stayed green
without modification.

Orthographic rather than perspective is a readability decision, not an
aesthetic one. Under perspective, two identical turrets at the near and far edge
of the board render at different sizes, which makes range and coverage harder to
judge — in a genre where judging coverage *is* the game.

The camera fits itself to the map's playable extent at startup and on every
viewport resize, rather than using hand-tuned framing numbers. That was needed
anyway for the web build, where the canvas is whatever size the browser window
is, and it means the 12 maps in P4 will not each need camera tuning.

Cost, measured: static scenery is ~124 draw calls, because the corridor is one
mesh instance per segment per wall and each pad is its own cylinder. That is
fine now and worth merging into a single mesh before the entity budget matters —
logged in `post-launch.md`.

## P0-16 · HTML5 export, single-threaded on purpose

`variant/thread_support=false` selects Godot's "nothreads" web template. With
threads on, the build requires `SharedArrayBuffer`, which browsers only grant to
pages served with COOP/COEP cross-origin-isolation headers — ruling out itch.io,
GitHub Pages and most plain static hosting. Single-threaded costs some
performance and buys the ability to put a playtest link anywhere, which at this
stage is worth far more.

`html/export_icon` must stay `false` until the project defines an icon: with it
`true` and no icon, Godot fails the entire export with only "configuration
errors" and no further detail. That cost real time to diagnose and is the kind of
thing nobody rediscovers cheaply.

Verified by `tools/verify_web.py`, which serves the build from a **deliberately
plain** static server with no special headers and drives it in real Chromium. A
web export can build cleanly and still fail to run — wrong renderer, missing
cross-origin isolation, a 404 on the wasm — and all of those look like success to
the exporter and like a black rectangle to a playtester. Confirmed running:
WebGL 2.0, single-threaded, `crossOriginIsolated: false`, no page errors.

## P0-17 · Audit findings

A deliberate attempt to break P0 found seven defects. All are fixed, and each has
a regression test in `tests/cases/test_audit.gd`. Recorded here because the
pattern in them is worth remembering: **every one was a silent failure**, not a
crash — which is exactly the class this project's honesty rules exist to catch.

| # | Defect | Why it mattered |
|---|---|---|
| 1 | Despawning an already-dead entity wrote **past the end of the free list** | Pool corruption. The worst of the seven, and the only one that was memory-unsafe. |
| 2 | A repeated map waypoint made a zero-length segment, dividing by zero and writing **NaN** into the direction table | Whether the NaN reached an enemy depended on where a binary search landed — real, and intermittent. Now rejected at load. |
| 3 | `_fire()` **silently dropped** shots when the projectile pool was full | A platform that appears to fire and deals no damage, with nothing recording it. Enemy spawns already had an overflow counter; projectiles did not. |
| 4 | Commands appended **out of tick order** were applied at the wrong tick | The replay cursor only moves forward, so a live run and its replay could diverge — precisely what the determinism work exists to prevent. Now inserted in order. |
| 5 | An unknown enemy id **silently resolved to enemy type 0** | A typo in a wave file would quietly spawn a different enemy than the one written. Now returns -1 and the group is skipped and counted. |
| 6 | Projectiles spent their last tick of life **without moving** | Off-by-one; lifetime N gave N-1 ticks of travel. |
| 7 | The state hash ignored **queued-but-unapplied commands** | Two runs with identical boards and different pending input hashed equal, which would let a desync through. |

Two non-defect cleanups from the same pass: the renderer was parsing colours from
strings *every frame* (a String allocation in the one place the project promises
not to allocate — now resolved once at setup), and the HUD rebuilt its label
string every frame regardless of whether anything changed.

Balance was re-verified after the fixes and is unchanged: a naive fill still wins
at 72/100 integrity with all damage in the last three waves.

## P0-18 · Fixed pads removed, and what had to replace them

Placement is free-form: anywhere in the buildable band beside the road. That was
a direct request, and it quietly broke the economy.

With pads, positions were scarce, so once the good ones were taken the only way
to spend Capital was to upgrade. With free placement there are ~150 legal
positions and no scarcity at all — and the raw numbers say spreading always wins:

| purchase | added DPS | cost | DPS per $ |
|---|---|---|---|
| new T1 | 20 | 100 | **0.200** |
| T1 → T2 | +24 | 240 | 0.100 |
| T2 → T3 | +54 | 560 | 0.096 |
| T3 → T4 | +112 | 1300 | 0.086 |

A new turret is always the better buy, so nothing would ever be upgraded and the
whole tier ladder would be dead content. This was not a theory — the first sweep
after free placement produced runs that were 100% tier 1.

The fix is a per-engagement **deployment limit**. Once your allowance is placed,
upgrading is the only way to spend, and the decision becomes *which* positions
you commit to and how hard you invest in them. Fixed pads had been providing
that scarcity for free; removing them meant putting it back deliberately.

Revisit if: a future family makes coverage genuinely more valuable than density
(Support auras would). The limit is per-engagement data, not a constant.

## P0-19 · Buildable ground is a purchasable grid

Cells beside the road start owned; the rest can be bought with Capital, but only
orthogonally adjacent to ground already held, at a price that climbs with each
purchase.

Orthogonal-only is the load-bearing detail: with diagonals, a purchase can slip
past the corner of the road and claim ground on the far side, which defeats the
point of expansion being a commitment to a direction.

Buildability is judged by the **cell centre**, not the click point. That is a
visible simplification — a click 110 units from the road can be refused because
its cell centre is 132 — but the alternative is a buildable region whose edge is
invisible, and the grid is drawn on the ground precisely so the rule is legible.

`BUILD_TOO_FAR` was deleted when this landed: distant ground is no longer
forbidden, it is unowned, and a reason code nothing can return is a trap for the
next person who checks for it.

## P0-20 · Cannon: area damage, and the determinism it threatens

The Cannon family lobs shells that damage everything within a radius, falling off
linearly to `splash_min_fraction` at the edge, with a floor of 1 damage so a
shell that visibly reaches something never does literally nothing.

Area damage is the first mechanic that resolves against *several* entities from a
single event, which makes it the first place iteration order can leak into the
simulation. `_detonate` walks the spatial hash in cell order and, within a cell,
in slot order — so the sequence of kills, and therefore the order bounties are
paid and pool slots are recycled, is identical everywhere. Resolving a blast in
arbitrary order would be a determinism hole that only surfaces when something
explodes near a pool boundary, which is to say months later.

Cannon is deliberately worse than Ballistic at single targets — lower DPS, slower
rate of fire, shorter range. It is paid for in area, and against one Bulwark
Hauler it is the wrong tool.

## P0-21 · Three enemy classes, as an HP-and-payout ladder

Skitter (fast, fragile, numerous), Sentry Walker (baseline), Bulwark Hauler
(slow, very tough, 12 integrity per leak and a large bounty).

This is explicitly **not** the armour triangle from plan section 3.5 — there are
no damage-type resistances yet. Swarms punish an arsenal with no area damage and
heavies punish one with no concentrated damage, which gives Cannon a reason to
exist without inventing a counter system P1 will design properly.

The alphabetical ordering of enemy ids is a live hazard: `enemy_ids()` sorts, so
adding "heavy" silently moved index 0 from walker to heavy. Several tests changed
meaning without failing. `enemy_index("walker")` now exists so nothing addresses
an enemy by a number that shifts when content is added.

## P0-22 · The test runner used to hide broken files

A test file that failed to parse was logged and skipped, and the suite still
exited 0 — the total quietly dropped from 89 to 83 and nothing went red. Fixed:
an unloadable or empty test file is now a FAIL.

Worth recording because it is the same failure class as everything in the audit —
not a crash, just a number that silently got smaller.

## P0-14 · Deliberately not built in P0

Not oversights — later phases, per §5.7. Anything tempting that came up is in
`post-launch.md`.

- Sell/refund, tier upgrades T2–T4, send-wave-early, targeting priority UI (P2)
- Armour triangle, other four families, status effects (P1)
- Run layer: route map, Corridor Integrity persisting across engagements,
  Salvage, Depots, bosses (P3) — P0's integrity is engagement-scoped
- Modules and the modifier pipeline (P2/P4)
- Save/load, Intel, Commanders, Daily Contract (P5)
