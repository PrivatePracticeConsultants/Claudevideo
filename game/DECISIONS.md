# DECISIONS.md — LAST LINE

Running log of choices that later phases inherit. Each entry says what was
decided, why, and what would have to be true to revisit it. Append; don't
rewrite history.

Phase status: **P0 complete and audited, then extended well past it** — 3D, HTML5, free
placement, tower tiers, two weapon families, three enemy classes, purchasable
ground, and a twenty-four-level campaign of boards that grow across three-act
chains. Formally this is P0 plus most of P1/P2's
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

## P0-23 · The starting shoulder narrows in later levels

`starting_ground_reach` is a per-engagement override for how far the free ground
extends from the road.

It exists because of an honest gap in the previous pass: buying ground was a
mechanic nobody needed. With a wide starting band and a deployment limit well
below the number of legal positions, expansion never paid for itself — it was an
optimisation for expert play at best.

Acts IX–XII narrow the band (150 → 125 units against a default of 200), so the
free shoulder no longer holds enough turrets and buying ground becomes part of
clearing the level rather than a flourish. Blackout Grid is authored around it
outright.

## P0-24 · Second audit — findings

The adversarial pass over the systems added since the last audit (ten probes:
degenerate ground reach, splash fractions above 1, over-large deployment limits,
upgrading past the top tier, exact-price purchases, buying road cells, nonsense
blueprint indices, pool ceilings, limit enforcement) found **no defects in the
simulation**. Two process defects, though:

**`tools/render_stress.gd` had silently rotted.** Placement gained a blueprint
argument; the tool was never updated and simply stopped parsing. Nothing noticed
because the dev tools sit outside both the test suite and the shipped web export.
Fixed, and `tests/cases/test_tools_parse.gd` now compiles every script in the
project so the class of failure cannot recur. It is the same shape as the runner
bug in P0-22: not a crash, just something quietly not running.

**Two levels were authored unwinnable.** Terminus could not be cleared even at
55% of its intended HP, and Blackout Grid failed at the multiplier interpolation
suggested. Both were caught — Terminus by the sweep, Blackout by
`test_every_campaign_level_is_winnable_and_losable` — which is precisely the
value of having balance under test rather than under judgement.

## P0-25 · The campaign test samples by default

Playing all twelve levels end to end is 24 full engagements, and the late ones
are hundreds of enemies over sixteen waves — minutes of wall clock for a single
test. A suite slow enough to skip protects nothing.

The default is a five-level spread across the difficulty curve (first, middle,
last, and the two that most depend on buying ground). `LASTLINE_FULL_CAMPAIGN=1`,
or `./game/run_tests.sh --full`, plays every level; that is the pre-release run.

A separate cheap test still loads **all twelve** and steps each for ten seconds,
so a broken map or wave file fails immediately regardless of sampling.

## P0-26 · Performance at the new ceiling, measured

Simulation, worst case the campaign can produce (400 enemies, 36 turrets all at
tier 4, on Terminus):

| enemies | ms per tick | headroom vs the 33.3ms budget |
|---|---|---|
| 50 | 0.81 | 41x |
| 150 | 0.88 | 38x |
| 300 | 0.85 | 39x |
| 400 | 0.70 | 48x |

Flat with entity count, which is the spatial hash doing its job. This is a
desktop-class CPU, not a phone — GDScript on an iPhone 11 should be assumed
several times slower, which still leaves comfortable headroom, but the device
measurement is still owed.

Rendering, same board:

| entities | draw calls |
|---|---|
| 0 | 141 |
| 50 | 162 |
| 200 | 162 |
| 500 | 162 |
| 800 | 162 |

Still flat: the three entity layers cost a fixed +21 no matter how much is on
screen. The 141 baseline is static scenery and now grows with map complexity
(Terminus has 13 segments), which makes merging it the most valuable remaining
optimisation.

## P0-27 · Why it did not look 3D, and what fixed it

The renderer had been 3D geometry from the start, but it read as a flat diagram.
The causes were specific and none of them were the geometry:

1. **Most surfaces were unshaded.** The ground and the corridor floor used
   `SHADING_MODE_UNSHADED`, so no light touched them and nothing had a lit side
   and a dark side.
2. **The camera was orthographic**, which removes every parallax cue at once.
3. **No ambient occlusion**, so nothing sat *in* the scene - objects floated on
   it. Contact shadow at the base of a turret is most of what says "solid".
4. **Flat albedo everywhere** - no metallic or roughness variation, so every
   surface answered the light identically.
5. **A steep 54-degree camera pitch**, which mostly shows you the tops of things.

Fixed by: perspective at a narrow 30-degree field of view (narrow enough that a
turret at the far edge is close to the same size as one near - the readability
argument that originally chose orthographic - while still giving depth), a
40-degree pitch so sides are visible, a key light with real shadows plus a cool
fill, SSAO, glow, filmic tonemapping, distance fog, and PBR metallic/roughness
per material class.

Two things learned the hard way while tuning it:

- **Generated meshes get double-sided materials.** The corridor is a strip mesh
  and getting winding right on every face of every mitred corner is fiddly and
  easy to regress; a back-facing wall renders as a black slot, which is exactly
  what it did. Culling saves nothing on a few hundred triangles.
- **High metallic on a coloured object under a bright key light renders white.**
  The drones were set to 0.5 metallic and stopped reading as coloured at all.
  Painted machines want low metallic.

## P0-28 · Static scenery is now one mesh, not 140

The corridor used to be one `BoxMesh` node per segment per wall, plus a disc at
every corner - about 140 draw calls before a single entity existed, and visible
seams wherever two boxes met. It is now built as quad strips into a single
`ArrayMesh`, with the lateral offset at each waypoint taken perpendicular to the
*average* of the incoming and outgoing segment directions, so corners mitre
instead of overlapping.

That was the largest outstanding optimisation in `post-launch.md`, and it turned
out to also be the fix for how the corridor looked.

Turrets moved to instanced layers at the same time (base, body, barrel), so the
board's draw cost no longer grows with how much you have built.

## P0-29 · Turret facing lives in the simulation

`t_aim_x/t_aim_y` is set when a turret fires. It is genuinely part of what the
turret did, it is deterministic, and it is in the state hash.

The alternative - having the renderer work out what each turret is pointing at -
means scanning every enemy for every turret on every frame to answer a question
the simulation already knew the answer to. The *smoothing* stays in the renderer:
the sim's aim snaps instantly and authoritatively, and how fast the barrel swings
to follow is presentation.

## P0-30 · Levels 13-22 are scaled from a tuned anchor, not hand-swept

Sweeping each new level the way acts I-XII were swept would be hours of
simulation. Instead they are derived from Terminus (act XII), which is tuned:

    limit_n   = limit_(n-1) + 2
    hp_n      = hp_(n-1) x (limit_n / limit_(n-1)) x 1.04
    bounty_n  = hp_n x (terminus bounty / terminus hp)

Player damage scales with the deployment limit, so scaling enemy health with the
limit holds difficulty constant; the 1.04 is the actual escalation. Income has to
track health or the later acts cannot fund the turrets they require.

**The first attempt failed, and instructively.** Reusing Terminus's *wave list*
as well as its multipliers gave every new level an opening wave of 34 Bulwark
Haulers against an empty board. Nine leaks at 12 integrity each ends the level
before wave 3, and all ten failed identically - same leak count, same kill count,
within seconds of each other. That uniformity is what identified it as structural
rather than a balance miss.

The lesson generalises: **a tuned level's wave list is tuned as a whole,
including its ramp.** Terminus survives that opening only because it is the shape
Terminus was tuned around. The new acts generate their own ramp - light early
waves, heavies withheld until wave 3, difficulty carried by the multipliers.

## P0-31 · Acts XIII-XXII hold health constant; escalation comes from elsewhere

The scaling formula in P0-30 was measured and found wrong twice, and the second
failure is the interesting one.

Act XIII wins **untouched at 100 integrity** with `act_hp_multiplier` 5.75, and
loses outright at 7.00. The entire band between "takes no damage at all" and
"unwinnable" is about 20% wide, and there is almost nothing in between — because
a Bulwark Hauler either dies before the exit or it does not, and nine of them
getting through is the whole integrity pool.

Tuning ten levels along a 20%-wide edge, at roughly twenty minutes of simulation
per data point, is not a good use of anything. So health is held at **6.0 across
all ten**, a value verified inside the band at both ends of the run, and
escalation is carried by the levers that are actually controllable:

- wave density +5% per act,
- Bulwark bias +6% per act,
- deployment limit +2 per act,
- and a narrowing starting shoulder (135 → 112 units), so later acts must buy
  ground to have anywhere to put the turrets they are given.

**Known limitation, stated plainly:** this leaves acts XIV-XXII comfortable. Act
XIII finishes at 64 integrity with real pressure; XVII and XXII finish at 100.
They are all winnable and all losable — verified, not assumed — but they are not
tightly tuned. The honest fix is `tools/balance_sim.py` from P3 doing a proper
per-level search; hand-sweeping twenty-two levels at this cost is not the answer.

**A generator bug worth remembering:** the first version of the wave generator
took `level_index` and never used it, so all ten acts shipped byte-identical wave
lists. It showed up as exactly 3819 kills on three different levels with three
different multipliers and three different maps. Identical numbers across
supposedly-different things is the cheapest bug detector available.

## P0-32 · Boards grow across a chain, and keep what you built

Eight boards, each played as three acts. An act reveals only the first N
waypoints of its map's route (`path_waypoints`); the next act reveals more. Every
turret and every cell of bought ground carries forward.

The load-bearing property is that **the revealed prefix never moves**. Acts share
a route by taking prefixes of the same waypoint list, so the road an act-1 turret
was covering is in exactly the same place in act 3 — which is the only reason
inheriting that turret makes sense. Route extensions are authored to head into
fresh ground rather than double back over where you have already built, so a
carried turret is not orphaned by the corridor being rerouted through it.
`adopt()` still drops and counts anything that no longer fits, because "authored
not to happen" is not the same as "cannot happen".

What carries and what does not:

- **Turrets and owned ground carry.** They were paid for in the act that built
  them, and charging again would make continuing a chain strictly worse than
  starting one.
- **Capital does not.** Engagement-scoped Capital is what makes each act's
  spending a fresh decision; carrying it would compound into a runaway.
- **Carried turrets count against the new act's deployment limit.** The limit
  rises by 4 per act, so what a new act buys you is room to *extend* coverage
  into the new stretch — not a clean slate on top of everything already standing.
  That is the whole strategic point: your act-1 decisions are still on the board
  in act 3, good ones and bad ones alike.

Carry-over is applied at construction, before any command runs, so it is part of
the initial state a replay starts from. Without that, every chained act would
desync — covered by `test_chain.test_a_carried_board_still_replays_identically`.

Restarting an act restores the same inheritance rather than a clean board:
retrying act 3 must not quietly delete what acts 1 and 2 built.

## P0-33 · Drone speeds rose with route length

Routes roughly doubled (full routes are now 10,500–15,400 units against
4,500–9,500 before). At the old speeds a single drone took minutes to walk one,
which made under-defended waves drag and made the headless suite unworkably slow.

Speeds went up ~1.6x (Skitter 86→140, Walker 55→90, Bulwark 34→55) so traversal
time per act stayed comparable to what was already tuned. This is a case where a
number that looks like pure balance is really about pacing and test throughput.

## P0-34 · The 24-act curve is resampled from the proven one, corrected once

The first attempt at authoring 24 chained acts generated the whole curve from a
formula. It produced a campaign whose *opening* level threw 2,223 drones at a
14-turret board with $400 — fifteen times the head-count of the level it
replaced, at four times the health multiplier. Every act was end-game soup and
the first two thirds of the difficulty curve had stopped existing.

The fix was to stop inventing. The 22-level campaign that preceded the chain
rework had been tuned level by level against a scripted-competent policy, so it
is the only measured data in the project. The 24 acts resample it: head-count
(148 → 5,591), starting Capital, bounty, and — this was the part the formula got
most wrong — the **per-class spawn intervals, shares and entry points**, lifted
from the old wave files rather than authored. Spawn *interval* turned out to
dominate head-count: pacing 148 walkers at 13 ticks instead of the proven 24
turned a winnable level into 9 kills and 25 leaks, with the same drones.

### The one correction

Exactly one change of this rework invalidates that data, and it invalidates all
of it by the same factor. P0-33 raised drone speeds 1.64× so a doubled route did
not mean a five-minute walk. **A drone moving 1.64× faster spends 1.64× less time
inside a turret's range, so the same turret does 1.64× less damage to it before
it is past.** Arrival rate is unchanged (spawn intervals are in ticks) and so is
income (bounty per drone, and the number of drones). Health is the only quantity
that has to move.

Measured before the correction, with the old health numbers carried over intact:
the first fourteen levels were unwinnable and the last nine were untouchable —
`highway_act1` finished 9 kills / 25 leaks / 5 turrets built, while
`spillway_act1` finished 100 integrity, zero leaks, 44 turrets all at tier 4.
That is what a 1.64× error looks like from both ends at once.

### Where the correction stops applying

Time-on-target is the binding constraint only while the board is DPS-starved,
which is the whole early campaign. Late, the deployment limit and the inherited
board leave a surplus, and the correction over-corrects: level 15 was measured
winning *without taking a scratch* at health ×6.0, well past the corrected
ceiling of ×3.66. So the corrected curve is trusted up to the point it saturates
and health keeps climbing from there to a top found by running it, not argued
for.

A rejected alternative: solving health and economy from a target
capacity-to-demand ratio. It is a defensible model — capacity is slots × the DPS
of the tier you are meant to reach, demand is peak health arriving per second,
and the probe does discriminate on that ratio (0.26 loses by nine leaks, 0.36
wins untouched). But head-count grows superlinearly against a linear capacity, so
the solved health curve fell *below* 1.0 mid-campaign and jumped at every tier
boundary. Resampling something that was measured beat modelling something that
was not.

The generator is a throwaway script, not a checked-in tool: the data files are
the source of truth and are meant to be hand-editable afterwards.

## P0-37 · An inherited board arrives refitted, or the chain is a cutscene

Carrying a board forward was supposed to mean "your act-1 decisions are still on
the board in act 3". What it actually meant, once measured, was that **every
carrying act in the campaign could be won by building nothing at all** — the
board inherited from the previous act cleared the next one unattended. Sixteen of
sixteen.

The cause is the tier ladder, not the carry. An act ends with its turrets at tier
4; a tier-4 ballistic is 210 DPS against the 20 it grew from. Inheriting that
intact hands the next act ten times the board it is authored against, and no
plausible difficulty step closes a 10× gap — raising the next act's health by
half did not move a single one of the sixteen.

So a carried turret arrives **one tier down, and never above tier 2**
(`Sim.CARRY_TIER_CAP`). Measured effect, on the same campaign:

| | acts won by an idle run |
|---|---|
| carried intact | 16 of 16 |
| carried one tier down | 2 of 16 |
| ...and capped at tier 2 | **0 of 16** |

The step-down does most of the work; the cap is what makes it structural rather
than tuned, because it bounds what an inheritance can be worth *however*
comfortably the previous act was won. That matters: the flawless margin at the
end of an act turned out to be well over 2×, not the 10–15% assumed.

What survives is what the chain is for — placements, weapon choices, and the
ground bought to reach them. What comes back is the decision the inheritance had
removed: what to re-invest in, now that the road is longer than the board that
held it. Thematically it is a refit; mechanically it is the reason act 2 is a
level.

## P0-35 · The balance gate plays chains, not levels

`test_every_campaign_level_is_winnable_and_losable` became wrong the moment
boards carried forward. It opened each level from a standing start — but act 3 is
authored against the turrets act 2 leaves behind, and its own Capital is smaller
*because* of them. Judged alone it looked unwinnable; judged in sequence it is
the level it was designed as.

So the unit of testing is now a whole chain (`test_every_campaign_chain_is_winnable_and_losable`),
and the losable half got stronger with it: an act must be losable **from the
board it inherits**, not from an empty one. An act that an inherited board clears
unattended is not an act, it is a cutscene.

The geometric premise the whole mechanic rests on — that an extension heads into
fresh ground rather than rerouting through what you already built — is measured
rather than asserted. Sampling every legal build spot in each act's band and
re-checking it against the next act's longer corridor, **exactly 2 spots per
board are lost, 0.4–1.2% of the band, and never to a segment that already
existed**. Those two are the pair either side of the old end-of-route, which is
inherent to a road that now continues. `test_chain` fails above 4%.

## P0-36 · Two things the longer boards broke quietly

Both found by measuring the new maps rather than by playing them, and both are
now guarded so they cannot come back.

**A route that crowded itself.** Every waypoint list is legal JSON and every
segment has length, so nothing rejected `lastlight_01` — but two of its stretches
passed within 100 units of each other. The no-build radius is 58 per side, so the
strip between them was buildable from neither, and the corridor walls (54 units
of half-width each) overlapped into one fused blob. Fixed by moving the offending
leg out to 350, and `Database` now rejects any map whose non-adjacent segments
come within twice `min_distance_from_path`. The measured clearances are 200–800
units; the check fails below 116.

**Shadows that stopped halfway across the board.** `directional_shadow_max_distance`
was a fixed 9,000 — fine for the boards it was written against, not for a 14,900-unit
route framed end to end. The far third of the last act rendered unshadowed, which
reads as two different scenes joined down the middle. The range is now refitted
from the camera framing every time the camera is fitted, which also means an
early act — which frames a third of the board — gets its depth resolution spent
on the part you can actually see.

## P0-38 · SSAO is asked for only where it exists

`tools/render_stress.gd` turned up a warning that had been printing on every
level load: *"Screen-space ambient occlusion (SSAO) can only be enabled when
using the Forward+ renderer."* The web build runs Compatibility (WebGL 2 →
OpenGL ES 3.0), which is how most people will actually play this — so the
ambient occlusion the renderer notes described as "the single biggest
contributor to a scene reading as solid" was off in the one place it mattered
most, and the only evidence was a line in a console nobody opens.

Now guarded on `RenderingServer.get_rendering_device() != null`, with the ambient
term lifted 1.35× where SSAO is unavailable. That is not a substitute — nothing
in Compatibility is — but it stops unlit faces crushing to flat colour without an
occlusion pass to give them shape.

The general lesson is the one P0-17 already recorded in a different costume: the
failure was silent. It cost nothing to find once something actually ran the
renderer and read what it printed, and it would have cost nothing forever if
nothing had.

## P0-39 · The Vanguard Lance breaks the trade every other drone makes

Skitters are fast and fragile; Bulwarks are slow and tough. Every class traded
speed against health, which means one defensive answer — enough damage per
second, placed anywhere — covered all of them. The Lance is the fastest thing on
the board *and* the toughest, and that is the entire design: a turret gets less
time on a target that needs more damage, so a thin line of fire that held
everything else lets Lances through. It is answered by spreading coverage down
the road or concentrating burst, not by adding one more turret.

Three numbers moved during tuning, all by measurement:

- **Share of head-count: 4.5% → 1.5%.** At 4.5% it was contributing a third of
  the closing wave's health from 4.5% of its bodies, and every level from the
  fourteenth on lost to exactly six leaks.
- **Leak value: 18 → 14 → 11**, which is *below* a Bulwark's 12. That looks
  backwards. A Bulwark's price is set for something you can reliably stop; the
  Lance's is set for something you often cannot, so its expected cost per drone
  spawned is already the highest in the game. At 14 it was the only thing that
  ever decided a level — the final act killed 5,578 of 5,590 drones and lost.
- **It never opens a wave and never appears before the campaign is well under
  way.** A class this punishing arriving before the player has a board is not
  difficulty, it is a wall.

A rejected approach: renormalising health so the Lance held total demand
constant, on the theory that it should change the *shape* of the threat and not
its mass. It sounds principled and it gutted the roster — a 3% Lance share cut
every other drone's health by a third to make room for itself. A new tier is
supposed to make the late campaign harder. The health ramp stayed where
measurement put it and the closing levels were re-measured instead.

## P0-40 · The deployment limit doubles every ten levels

Sixteen turrets on a board this size was reported as simply not enough to work
with. The scripted policy winning anyway says more about the policy than about
the game: it places optimally and instantly, and a person does neither, so
"the greedy clears it" is a ceiling and not a description of play.

The limit is now `24 × 2^(level/10)` — 24 at the first level, 48 by the
eleventh, 118 at the twenty-fourth, against a pool ceiling raised from 64 to 144.
Roughly twice the turrets is roughly twice the damage, so health and Capital
follow the same ratio, with a deliberate 40% of the increase kept as slack rather
than handed straight back: the point of the change was that the game had too
little room, not that it was mistuned.

Two things this broke, both caught by the suite rather than by play:

- **Shots in flight when an engagement resolves were never resolved.** `step()`
  stops after the result is set, so they sat in the pool forever and the "every
  shot fired was resolved" invariant was quietly false. With a couple of dozen
  turrets the odds of a shot being mid-flight on the exact resolving tick are
  low and the test passed by luck; with 118 it happens nearly every time.
  `_clear_projectiles()` now runs on resolution.
- **A test that measured the spacing rule while claiming to measure
  affordability.** Candidate build sites are sampled closer together than
  `min_platform_spacing`, so consecutive ones reject each other once the first is
  built. That never showed until the opening level could afford enough turrets to
  reach its own neighbours.

The within-chain health step also had to steepen (×1.22/×1.48 → ×1.70/×2.10):
with 24 turrets carried onto a board whose next act adds two slots, the old step
left the second act of the opening chain winnable by building nothing.

## P0-41 · The Arc Suppressor, and why slows refresh instead of stacking

The third weapon family is the first that is not primarily about damage. It does
roughly a fifth of a Ballistic's DPS and instead drags everything in a small
blast radius down to a fraction of its speed. That makes it a force multiplier —
every other turret on the board gets more time on target — and the direct answer
to the Vanguard Lance, whose whole threat is crossing a firing arc too fast to be
killed in it.

**Slows refresh; they never stack.** The strongest slow currently on a drone wins
and re-arms its timer. Stacking multiplicatively is the obvious implementation
and it is a trap: a cluster of Suppressors would pin a wave in place
indefinitely, which turns one turret into the answer to every question and is
exactly the degenerate state the deployment limit exists to prevent.

Suppression is sim state, so it plays by the sim's rules: preallocated arrays on
the enemy pool, durations converted to ticks at load (seconds would drift with
tick rate), cleared when a pool slot is recycled — the same hazard the generation
stamp exists for, applied to status effects — and folded into `state_hash()`, or
a desync in *who is slowed* would be invisible to the determinism test.

One knock-on worth recording: the renderer picked a turret's family colour by
asking "does it splash". The Suppressor splashes too, so every Suppressor
rendered as a Cannon until the test was reordered to ask about slowing first.

### It made the balance gate fail, correctly

Suppression makes an **inherited** board stronger, because slowed drones give
every turret already standing more time on target. The second act of the opening
chain immediately became winnable by building nothing, and the gate caught it.

Chasing it revealed the actual defect, which was not the Suppressor. Slack from
the deployment-limit change (P0-40) had been set at 40% of the capacity increase,
which left the opening acts roughly **four times** over-provisioned. That is
harmless on its own and poisonous in a chain: the board an act hands forward then
wins the next act unattended. Cutting the slack to 15% and steepening the
within-chain step (×1.85/×2.35) fixed it at the source. The player's headroom
comes from having far more turret slots, not from every act being loose.

## P0-42 · Thirty-six levels, and the two things that broke at that scale

Twelve boards, three acts each. Two structural defects only appeared once the
campaign was this long, and both were caught by measurement rather than play.

**A limit is only real if there is somewhere to put the turrets.** The
deployment limit doubles on absolute level index (P0-40) and hit 144 well before
the campaign ended — but the first act of a board reveals only a third of its
road. `terminus_act1` was handed 144 slots for 5,380 units of corridor, fielded
63, and lost a level authored for the full allowance. The limit is now also
capped by revealed road length (`ROAD_PER_TURRET`), which makes act 1 and act 3
of a board differ naturally instead of by accident.

**Stretching a curve is not the same as extending it.** Head-count, health and
economy are resampled from a 22-level measured campaign onto N points. Going from
24 points to 36 stretched the threat curve while the limit kept doubling on
absolute index, so the middle of the campaign got far more turrets against barely
more drones — **eleven acts became winnable by building nothing at all**. Fixed
by curving the board ramp (`BOARD_RAMP_CURVE`) and scaling head-count with the
slot count, so more turrets means a busier board rather than only a safer one.

Both pool ceilings moved with it: `max_enemies` 1,280 → 2,048 (heaviest authored
wave is 1,621 drones) and `max_projectiles` 2,560 → 8,192. `Database` refuses to
load a wave that would breach either, which is how the first one surfaced — as a
clear load error naming the number, not as spawns silently going missing.

## P0-43 · An inheritance is bounded in size as well as in quality

P0-37 capped what a carried board could be worth per turret. At 78 slots that was
not enough: eight mid-campaign acts went back to being idle-winnable, because
seventy-eight tier-2 turrets arriving free is a board regardless of how modest
each one is.

An inheritance may now occupy at most `carry_limit_share` (0.55) of the new act's
deployment limit; the rest are **stood down** and counted separately from turrets
lost to the extended corridor, so the HUD can say which happened. The share is
below 1 for a reason beyond balance: it guarantees there is always room to build
past what you inherited, which is the difference between continuing a board and
being handed one.

## P0-44 · The fourth family, the fifth class, and why each answers the last

The roster is now a chain of answers rather than a ladder of numbers:

- **Arc Suppressor** slows things → answers the **Vanguard Lance**, whose threat
  is crossing a firing arc too fast to be killed in it.
- **Siege Breaker** shrugs off 80% of any suppression → answers the Suppressor,
  because once slowing everything was possible, slowing everything was the answer
  to everything.
- **Railgun** fires a piercing round down a lane at full damage the whole way →
  answers the Breaker, and a column on a straight, and is close to worthless
  against a scattered swarm.

Priced deliberately *worse* per dollar than Ballistic (0.138 dps/$ against 0.162
at tier 4): the Railgun pays a premium for range and pierce, and a family that
was strictly better per dollar would end the arsenal rather than extend it.

## P0-45 · Playability: what a 36-level campaign needs that a demo does not

Five additions, in the order they mattered:

1. **Progress is saved, per board.** Restarting a 36-level campaign from level 1
   every session makes the back half unreachable. Progress is per *board*, not
   per level, because later acts are entered carrying earlier ones — dropping a
   player into act 3 of a board they have never played hands them a level
   authored against turrets they do not have. `[` and `]` move between unlocked
   boards. A missing or corrupt save is a new campaign, never a crash.
2. **Sell, at a partial refund.** Free placement with no undo is punishing in a
   way nothing in the design intends. The refund is 65% of everything spent
   including upgrades, so relocating stays a real cost.
3. **Next-wave preview.** With five drone classes, what is coming is the
   difference between planning a board and guessing at one — and the wave file
   already knows. Withholding it is not difficulty.
4. **Call the next wave early, for a pro-rata bounty.** The gap between waves is
   when Capital accumulates and turrets get built, so calling one in trades
   preparation for money. That is a decision, not a convenience.
5. **4× speed.** Sixteen-wave acts on a 16,000-unit road are long.

Sell and call-early are simulation state and go through the command log like
everything else. That immediately mattered: the replay helper understood only
`PLACE` and `UPGRADE` and fell through to "upgrade" for anything else, so a log
containing a sell would have replayed as a different game. Caught in the audit,
not by a test — the tests could not see it because the scripted policy never
emits those commands.

## P0-46 · Deleting the player's turrets was the wrong price

P0-43 capped how much of a new act's deployment limit an inheritance could
occupy, at 55%. It worked — it was the thing that finally stopped eight
mid-campaign acts winning themselves — and it was wrong, because the way it
worked was by **deleting turrets the player had built and paid for**. It was
reported as a bug the first time anyone played it, which is the correct reaction:
nothing in the design says a level can take your board away.

The cap is gone (`carry_limit_share` is 1.0). The same job is now done by
refitting a carried turret all the way back to tier 1 rather than to tier 2 —
the price is paid in tiers, which the game already has an economy for, instead of
in emplacements, which it does not.

Measured on the same campaign, acts winnable by an idle run:

| | idle-winnable acts |
|---|---|
| carried intact | all of them |
| refit to tier 2, capped at 55% of the limit | 0, but turrets vanished |
| **refit to tier 1, everything carries** | **0** |

Two things this exposed that are worth keeping in mind:

- **On the opening boards a refit costs nothing**, because the board being
  carried is already all tier 1. Those chains are held up by the threat step
  alone, which is why `ACT_STEP` had to rise to 2.50 for act II.
- **Easing health globally makes carrying acts *more* idle-winnable, not less.**
  An inherited board's strength comes from the deployment limit and its tier;
  neither scales with health. So every time the top of the curve came down to fix
  the final act, the second act of the opening chain went back to winning itself.
  `BOARD_OPENING_HP` is now a fixed anchor for exactly this reason. I reasoned
  the opposite of this out loud and the probe corrected me.

## P0-47 · Forty-eight levels on the same twelve boards

A fourth act per board. Act III already opens the whole route, so **act IV adds
no road** — it is the only act in a chain whose corridor is identical to the one
before it, and it escalates through the deployment limit, head-count and drone
mix instead. That is the honest shape of "more levels without more map", and it
is worth naming rather than dressing up.

Act IV sits only ~4% above act III in health. Steeper simply lost: at ×3.40 both
`coldstore_act4` and `highline_act4` died on seven leaks, because act III of the
later boards already finishes in the sixties and there is nowhere above that to
go. The escalation that still had room was slots and bodies.

## P0-48 · Zoom and pan, because the board outgrew the frame

Boards run to 16,000 units. Framed end to end, a turret is a few pixels and the
range preview that makes placement legible is meaningless.

Zoom is a **multiplier on the fitted extent** and pan is an **offset from the
fitted centre**, rather than an absolute camera transform. That matters because
`_fit_camera` reruns on every viewport resize and every time a chain extends the
corridor: expressed absolutely, the view the player chose would be thrown away by
any window resize. Expressed relatively, the auto-fit keeps owning the base
framing and the player's choice rides on top of it.

Both are clamped — zoom to `[0.18, 1.0]`, pan to the framed extent scaled by how
far in you are, so there is no panning at all when the whole board is visible.
Unbounded versions of either are how you end up inside the ground plane or
looking at a board you cannot find again.

Zoom is applied about the ground point under the cursor, so the thing you are
looking at stays roughly where you are looking. None of it touches the
simulation; `test_renderer` guards the clamps and the survives-a-refit property.

## P0-49 · A turret per family, and terrain that reads as terrain

Two complaints, both fair: weapons did not look like weapons, and terrain did not
look like terrain. They had the same root cause — everything was drawn from the
cheapest primitive that would do, and colour was carrying all the meaning.

**Weapons.** All four families shared one silhouette (cylinder mount, cylinder
housing, box barrel) and differed only in tint, so the arsenal read as one weapon
in four colours. Each family now has its own mount, housing and muzzle:

| | mount | housing | muzzle |
|---|---|---|---|
| Ballistic | hexagonal turntable | blocky receiver | one long slim barrel |
| Cannon | wide, eight-sided | tapered, wide at the base | short fat bore, angled up |
| Suppressor | squat, round | smooth drum | a coil ring — no barrel at all |
| Railgun | low four-sided sled | narrow, long front-to-back | a very long thin rail, dead flat |

Elevation is baked into the muzzle basis rather than applied as a separate
rotation, so a mortar visibly lobs and a railgun visibly does not. Housings now
turn with the barrel — a boxy receiver that never faces its target reads as a
crate someone left there.

This costs three MultiMesh layers per family instead of three in total. Draw
calls went 23 → 29 and, measured, **stay flat from 25 to 400 entities**, which is
the invariant that actually matters. One node per turret would not have been.

**Terrain.** The ground was a single flat quad in one colour — under one
directional light that is a slab, and no amount of tonemapping fixes it. It is
now a mottled grid whose vertex colours vary from a hash of position, plus a
graded verge either side of the road, a centre line down it, and scattered debris
off it for scale.

Three things worth recording:

- **Hashed, not random.** The renderer has no business touching the simulation's
  seeded RNG, and a board that looked different every time you restarted would be
  its own kind of wrong.
- **Relief only away from the road.** The ground rolls, but is dead flat
  everywhere a turret could stand. The simulation is 2D and every placement rule
  is a distance in the ground plane; relief under the playable band would put
  turrets on slopes the rules know nothing about.
- **The first pass made it worse.** Ground at `#12161d` with props scattered to
  the horizon read as debris floating in a void — the props revealed how empty
  the ground had always been. Lightening the ground, tightening props to within
  620 units of the road, and dropping the buildable overlay's alpha from 0.55 to
  0.34 fixed it. Screenshots each time; none of this is arguable from the code.

`_ground_height` also needed an early-out: it called `distance_to_path` for all
~9,400 vertices, which walks every path segment, and that is a visible hitch on a
level load and worse on a phone.

## P0-50 · Crossing to a new board carries money, because it cannot carry turrets

Reported while playing: *"why does it delete my existing weapons when I enter
into a new level?"* Measured first rather than assumed, and the answer split in
two:

- **Within a board, nothing was being deleted.** Acts I→II→III→IV carry every
  turret — 24 → 26 → 28 → 30 built, with 24, 26 and 28 carried, and zero dropped
  or stood down. That half was working.
- **Crossing to a new board, everything went.** Level 4 is a different *map*;
  a coordinate on Highway means nothing on Port, and there is no honest way to
  move an emplacement between them.

So the turrets could not follow, but what they were worth could. A finished board
now salvages into the opening Capital of the next one, and the end-of-board
banner says so instead of leaving it to be discovered.

**It has to be bounded, and the numbers say why.** At 50% of everything spent, a
finished board is worth **8,790 Capital arriving at an act budgeted for 1,300**,
and **22,330 at one budgeted for 2,350**. Unbounded, "continuity" is just
deleting the economy from the fifth level onward. Salvage is therefore capped at
40% of the receiving act's own opening budget — proportional, so it stays
meaningful late instead of being decisive early and irrelevant by the end, and
still tied to how well the last board was actually finished.

The condition is the exact inverse of the carry rule, which is the neat part: an
act that continues a chain takes turrets and refuses salvage; an act that opens
one takes salvage and refuses turrets. Both are applied at construction, so both
are part of the state a replay starts from.

## P0-51 · The banner was quoting a number the next act would not pay

Found by re-reading the salvage path rather than by a test, which is the
uncomfortable part. `board_salvage()` returns what the board you just finished is
worth; the cap is applied by `grant_salvage()` on the act you are about to open.
The end-of-board banner called the first of those. Measured across a chained
campaign run:

| boundary | board worth | next act's ceiling | actually paid |
| --- | --- | --- | --- |
| highway → port | $8,790 | $520 | $520 |
| port → capital | $22,330 | $940 | $940 |
| capital → refinery | $58,820 | $1,740 | $1,740 |
| refinery → railyard | $96,445 | $2,300 | $2,300 |

So the banner promised **$8,790 and the next screen credited $520** — a 17×
overstatement, and the honesty rule broken by the one line that exists to reassure
the player nothing was thrown away. The ceiling now travels with the quote:
`Sim.salvage_ceiling_of(db)` is static so `main.gd` can read the *receiving*
act's ceiling while the outgoing act is still on screen, and the HUD quotes
`min(worth, ceiling)`.

**What the same table says about the mechanic.** Every realistic finish is worth
17–42× the ceiling, so the cap binds every single time: salvage is in practice a
flat 40% top-up on the new board's budget, not a reward that tracks how well the
last board was held. `test_a_weaker_finish_salvages_less` passes only because a
one-turret board falls under the ceiling. Making it responsive is one data value —
`board_salvage_fraction` around 0.02 of spend puts the first two boundaries under
their ceilings and leaves the last two capped — but it moves opening Capital on
board-opening acts, which are the anchored-easy point of every board, so it is a
measured change and not a free one. Recorded here rather than done.

## P0-52 · Twelve boards of four acts, not fewer boards of longer chains

Asked directly, after salvage shipped: should boards become longer chains so
turrets persist across more levels? No, and the evidence is from this session:

- Turrets already persist across four consecutive levels with **zero dropped and
  zero stood down**. That is the continuity that was asked for, and it is
  verified, not assumed.
- Salvage closes the boundary in the only currency that can cross a map.
- Longer chains means *fewer distinct boards*. Map variety has been asked to grow
  repeatedly; halving it to buy two more levels of persistence trades the thing
  that was wanted often for the thing that was wanted once.
- Every restructure this session invalidated the balance curve and cost six to
  ten probe runs plus multiple full-suite runs, and each one introduced acts an
  inherited board could clear unattended that then had to be hunted down. The
  campaign is currently measured; that is worth something.

If more persistence is wanted, the cheap dials are `CARRY_TIER_CAP` (how deeply a
carried turret is refitted) and `board_salvage_cap_share` — single values, one
probe run each, no structural risk.

## P0-53 · Targeting priority: the cheapest real decision a tower defence has

Every turret shot whatever was furthest along the road. That is the right default
and it is still the default — so nothing in the measured campaign moved — but it
meant a line of forty turrets was one decision repeated forty times.

Five orders: First, Last, Nearest, Toughest, Weakest. None is strictly better than
another, which is the test a mechanic like this has to pass:

- **Last** holds a leaker back for the guns behind it, and wastes a front line's
  uptime doing it.
- **Nearest** keeps a Suppressor's slow on whatever is closest to *it* rather than
  closest to the exit.
- **Toughest** puts a Railgun on the Siege Breaker and lets forty Skitters walk
  past it.
- **Weakest** is how a Cannon line clears chaff so the heavy guns are never
  distracted.

It is **simulation state, not a display preference** — it decides what gets shot —
so it goes through the command log as `CMD_SET_PRIORITY`, is part of the state
hash, and is proved to replay bit-identically. It also survives selling (the pool
compaction moves it with the emplacement) and carries between acts, because tiers
being refitted is a price and re-issuing thirty standing orders is just tedium.

First keeps its own loop. It can reject a candidate on one float compare before
touching its position, which the general path cannot — Nearest needs the distance
it would be skipping. At 144 turrets against 2,048 drones that early-out is the
difference between the profile that was measured and a slower one.

## P0-54 · Armour and the Brood Carrier: the first drones that are not just bigger

Five classes in, the enemy roster was an HP-and-payout ladder — every drone was
the last one but more so. Two properties change that:

**Armour comes off the hit, not off the health.** Flat, so it scales with how many
rounds you need rather than how much damage you deal: armour is nearly nothing to
a Railgun landing 640 and half of a tier-1 Autocannon's 10. It never reduces a hit
below 1, because a weapon that literally cannot scratch something reads as broken
rather than as a counter.

It also **scales with the wave, and is capped as a share of the round**. Both
halves are needed and the numbers say why. Health grows exponentially — by the
last act of Terminus a drone carries 40× its base — so flat armour is a texture
that exists for four levels and then evaporates. But uncapped scaling is worse: at
the same point it would be 40, and a tier-4 Autocannon landing 42 would do 2.
`armour_max_bite` (0.5) says armour may never take more than half a round, so it
stays a reason to bring a bigger gun at every scale and never becomes a reason the
small ones stop working.

**The Brood Carrier splits.** 150 health plus three 22-health Skitters is 216
effective against a Bulwark's 260, and it pays 61 against the Bulwark's 58 — so it
is not a difficulty increase, it is a different shape at the same price. The
design is the contradiction inside it: the weapon that kills the carrier well
(one big round, past the armour) is the wrong weapon for what it leaves (three
fast, fragile things), so a line that answers it has to be two things at once.
And because splitting is on death and not on exit, letting one leak costs 5 and no
Skitters — the only drone in the game where letting something through can be
correct.

**Splits are resolved at the end of the tick, not where the drone died.** Not
style. Area damage and piercing walk the spatial hash, which was built at the top
of the tick and still lists slots whose occupants have since been killed; hatching
immediately can hand a child one of those slots, and the same blast then finds it
alive and hits it too. Deterministic, but decided by the free list, which is no way
to decide anything.

**Seeded at a flat health budget.** Every carrier added to a wave removed ten
Skitters (216 effective against 220), across the last four boards only — Lance
arrives at level 18, Breaker at 26, Carrier at 33, which keeps the cadence the
campaign already had. Measured after: all sixteen levels still win, and none of the
four boards' act 4 can be cleared by its inherited board with no input (idle runs
lose by 9, 9, 9 and 10 leaks).

The pool validator counts `count × (1 + split_count)`. Counting only what the wave
file lists would let a wave overrun `max_enemies` at the exact moment the last
carrier dies, which is the worst possible time to start silently dropping drones.

## P0-55 · Feedback: what the board was missing was not information, it was impact

Muzzle flashes, impact sparks, blast rings, wrecks, and a camera shake when the
corridor takes a hit. All of it decoration, and all of it derived by **diffing the
simulation between ticks** rather than by the simulation reporting events: a shot
fired is a cooldown that went up, an impact is a projectile slot that was alive and
is not, a wreck is a drone slot that was alive and is not.

That is the whole design. The sim never grows a render-facing event channel it
would then have to hash, and an effect can never desync anything because there is
nothing for it to desync. It also means the effects layer is optional — every
headless test runs with none attached and nothing notices.

Driven per TICK and not per frame, from main's step loop. At 4x a frame spans
several ticks, and sampling only the one a frame lands on makes a firing line look
like it is misfiring.

One pooled MultiMesh of 1,024 billboards, so the project's rendering invariant
holds: a hundred wrecks in one tick is still one draw call and not one node.

**Two things only a screenshot could have said.** The first pass drew flat white
squares — stickers on the board rather than light coming off it — and was fixed
with a generated radial falloff texture (squared, because linear still shows a disc
edge). The second was that a 26-unit flash on a 5,000-unit board is a few pixels,
so `tools/capture_screenshot.gd` grew a `--zoom` flag; verifying this layer at
full-board framing verifies nothing.

Only a leak shakes the camera. If everything shakes the screen then nothing does,
and a leak is the only event in the game that costs something you cannot get back.
Shake is applied as an offset from a stored resting position rather than
accumulated onto the live one, because a camera left displaced would silently
break every screen-to-world pick after it — which is how you build in the wrong
place.

## P0-56 · Sound, synthesised in code

There are no audio files in this repository and there is not going to be one. Every
sample is generated at startup from filtered noise and swept sine tones, in about
thirty lines of arithmetic. That buys three things worth more than fidelity: the
web build stays the size it was, a family's sound is tuned by editing a number in
`theme.json` the same way its colour is, and the game stops being silent.

**The hard part is not synthesis, it is the throttle.** A hundred and forty-four
turrets firing three times a second is four hundred shots a second; played
faithfully that is not a firing line, it is white noise. Each kind of sound gets one
voice every few tens of milliseconds and the rest are dropped — and the throttle is
armed on the *decision*, not on a voice being found, or a sound dropped for want of
a voice would be re-offered on the very next tick, defeating it exactly when the
board is busiest. A leak is never throttled, because every one of them matters.

Sound rides along with the same tick-diff the visual layer uses rather than working
it out a second time, and the whole thing is muted with M.

## P0-57 · A debrief, because winning without knowing why is not learning

A tower defence tells you almost nothing about why you won. The board is a blur at
4x and then it is a banner, and the next act's build decisions get made on a hunch.
The debrief answers the one question those decisions actually turn on: which of
your weapon families was doing the work.

Damage is tracked per **family** and not per emplacement, and that is a correctness
decision rather than a UI one. Selling compacts the platform pool by moving the last
turret into the freed slot, so an emplacement index is only meaningful within a
tick and a shot already in flight would credit whoever inherited the slot. A
blueprint index never moves.

**Overkill is not counted.** A Railgun round doing 640 to a drone with 12 health
left is credited with 12. Claiming 640 would make the whole breakdown a fiction the
moment anything died, and this file has a rule about numbers that cannot be
explained. Families that never fired are omitted rather than printed as zeroes; a
line of noughts is not information.

## P0-58 · Interest, so that holding Capital is a thing you can do

Capital not spent the instant it arrived was Capital wasted, which made "build
now" beat "build better in two waves" unconditionally. 5% of what is in hand when
a wave begins, capped at $90, nothing on the opening wave.

Both bounds matter. Uncapped, a percentage of an unbounded pile is an unbounded
pile, and the correct play late in a long act becomes building nothing and
banking - the exact opposite of the decision it was added to create. Paying on
wave one would just be a bigger starting purse.

## P0-59 · Support links: a family reaches other families only

Placement decided coverage and nothing else. Every weapon family now projects a
bonus onto turrets **of other families** within its radius: Autocannon lends
reach, Mortar and Railgun lend damage, Suppressor lends rate of fire.

The other-families rule is the whole mechanic. Without it this is a flat damage
bonus with extra steps; with it, four Autocannons in a row get nothing from each
other and a mixed line is worth more than the sum of its parts.

**Nothing is projected at tier 1, and that is a balance rule wearing flavour's
clothes.** A board carried into the next act arrives refitted to tier 1, so an
inheritance projects nothing until it is re-invested in. Measured with links live
at tier 1: twenty-four inherited turrets cleared the whole of Highway act II with
no input at all — the act became a cutscene. Gating it fixed that, and gave the
player the first reason in the game to upgrade a turret that is not their best
one.

Measured in real play, which is the check that matters for a mechanic that could
easily have existed only in a unit test:

| level | turrets | linked | links | best | mean rate |
| --- | --- | --- | --- | --- | --- |
| highway_act1 | 24 | 13 | 22 | +14% range | — |
| capital_act1 | 40 | 40 | 139 | +23% rate, +26% dmg | +12.0% |
| railyard_act1 | 61 | 61 | 291 | at the caps | +17.8% |
| reactor_act3 | 144 | 144 | 651 | at the caps | +17.0% |

Capped by **total** rather than by number of sources, because "the first three
contributors in index order" is deterministic and arbitrary, and a rule nobody
can predict is not a rule anyone can play around.

**The recompute had to become incremental.** It is O(turrets²) and runs when the
board changes; at 208 emplacements that is 43,264 compares, the scripted policy
changes the board a few thousand times an act, and the suite went from under
eight minutes to over ten on that alone. Placing or upgrading turret k changes
only what k projects and what k receives, so the recompute now touches k and
whatever lies within the widest support radius of it. Selling still does a full
pass, because it compacts the pool and indices move.

## P0-60 · Two drones with jobs

Every drone before these was answered by pointing more guns at the road.

**Field Mender** puts health back into everything around it and never into
itself — a drone that outheals your line while also being the toughest thing in
it is a wall, not a puzzle. It is small, quick and cheap, so First and Toughest
walk straight past it. It is the first thing in the game that makes the targeting
orders worth having.

Its bounty sits on the ladder where its 90 health says it belongs, not where its
importance does. Priced above the Brood Carrier it broke the roster's one hard
rule — the tougher drone pays better — and the suite caught it. Paying over the
odds for a Mender would also make the drone you most want dead the one you most
want to farm.

**Static Jammer** is the only thing in the game that attacks the *board*. It
silences turrets it passes, and a jammed turret's cooldown does not advance
either, so the silence costs exactly the uptime it looks like it costs. It makes
"where is my line thinnest" a question whose answer changes while you watch.

Both seeded as elites at a flat threat budget: a Mender priced at four Sentry
Walkers rather than its own two, a Jammer at two Bulwarks. Deliberate
over-estimates — adding difficulty by accident is far harder to notice than
removing it.

## P0-61 · Integrity that carries, and a module draft

Integrity now persists between the acts of one board. Before it, a sloppy act I
cost nothing in act IV and a chain was four fresh starts wearing a chain's
clothes. It never crosses to a new board: a board is a contract, and a bad run
three boards ago following you forever with no way to recover it is a punishment
rather than a decision.

**Honest about the measurement.** The scripted policy almost never leaks, so
persistence is nearly invisible to the probe — Highline still runs
100/100/100/100. It is a real change for a human player, who does leak, and the
probe cannot tell you that. What the probe can say is that it breaks nothing.

**Modules** are the between-acts choice: three offered after a win that continues
a chain, one kept for the rest of the board. Ten in the pool, each deliberately
touching a different system, so a draft is a question about what this board needs
rather than a comparison of three numbers on one axis.

The offer is seeded by the level and not by the clock, so the same act always
offers the same three — that is what makes a draft something you can plan around
instead of a slot machine you reload. Everything is applied at construction, like
an inherited board and like salvage, so nothing in the tick has a special case
for modules: a module is a different number in a table the tick was already
reading, which is why adding one is a data change.

Link radius is grown by the **square** of the fraction, because the table it lands
in is squared — applying the raw 30% there would have been a quietly much smaller
buff than the module claims.

## P0-62 · Forks: N complete routes, not a graph

Terminus is the first board with two roads. They are modelled as **two complete
routes from the same gate to the same exit**, not as a branch node in a graph,
and that is the whole reason it was affordable: a drone's position stays one
scalar distance along one polyline, the placement rules stay "distance to the
nearest segment", and nothing in the tick learned what a junction is.

What it buys is that coverage has to be **divided**. One road rewards one long
line; two roads mean every turret chooses which one it watches, and the
deployment limit means it cannot watch both. The fork is 10,477 units against the
main road's 15,950, so it is not a second copy of the same problem — it is a
shorter deadline, and `route_weights` sends three drones the long way for every
one that takes it.

**A fork opens only at full reveal.** The alternative was revealing a proportional
prefix of each, and it does not work: a fork is authored to leave the gate and
rejoin at the exit, so a prefix of one ends in the middle of nowhere and
everything walking it leaks there. Acts I and II are one road; III and IV are two,
which is also a better escalation — the board you have learned to hold suddenly
has a second way in.

**Two things the measurement forced.**

The scripted policy only knew about road 0, so it built a perfect line beside a
road carrying two thirds of the traffic and lost act III by eighty leaks. That is
a fixture bug, not a balance result — a policy that cannot see half the board
measures a level nobody would play that way. Fixed, the same act lost by sixteen.

Sixteen leaks was a real result, and the answer was not to make the fork weaker.
The deployment limit has always been bounded by how much road there is to stand
beside, and a forked act carries 26,427 units against a single road's 15,950 —
so `max_platforms` went from 144 to 208 and the two forked acts got a limit to
match. They then won at full Integrity.

## P0-63 · A landscape, and the horizon that cannot be in it

Asked for trees, terrain, "an environment that actually looks more realistic".
The board was a road and a dark green sheet with some debris on it.

What went in: a graded terrain palette (bare soil beside the road, grass beyond
it, rock on anything that has climbed, blended by position rather than painted
on), a clustered treeline, scattered boulders, a gradient sky, and fog retuned to
the colour of what it fades into. All of it instanced — 2,777 trees on Highway
are two MultiMesh layers, not 2,777 nodes.

**The thing worth recording is what was cut.** A ring of hills went in first and
came straight back out. The camera sits at −38° with a 26° field of view, so the
*top* of the frame still points 25° below horizontal: the horizon is not
off-screen at this framing, it is geometrically unreachable, and four hundred
vertices of mountain range were being drawn for nobody. Putting it in view would
mean pitching the camera to roughly −13°, which is nearly side-on and not a tower
defence board any more. Everything that stayed is on the ground plane, because the
ground plane is the entire frame.

**Three things only a screenshot could say**, in the order they were found:

1. Fog the colour of the old background (near-black) turned every distant thing
   into a silhouette of nothing the moment there *was* something distant. Retuned
   to the sky's horizon colour, then dropped from 0.00022 to 0.000008 because the
   first correction made the near ground milky.
2. Trees placed "not on the road" stood in the foreground with the board visible
   through the gaps. Correct placement, unplayable frame. The rule is now measured
   along the camera's own view axis — and *behind the board's centre*, not behind
   its nearest corner, which was the obvious answer and still left room for a
   conifer in the bottom-left of a four-thousand-unit board.
3. The buildable-ground overlay was authored dark for a dark board. On lit terrain
   the same colours read as holes cut in the grass, and owned ground and ground
   you can buy stopped being distinguishable. Both went light and thin, then were
   re-separated by hue rather than by darkness.

**Cost, measured on a software rasteriser** — a worst case, not a GPU: 57ms a
frame before, 91ms with the landscape, 84ms with tree shadows off. Draw calls stay
flat at 35 from 25 entities to 400, which is the invariant that actually matters.
`tree_shadows` is the first switch to reach for if a real device struggles and
`tree_limit` is the second.

**A note on testing scenery.** The rule that nothing tall stands in front of the
board, or on ground a turret could use, is exactly the kind of thing that should
be a test — and a MultiMesh's instance buffer lives in the RenderingServer and
reads back as zeroes in a headless run, so the obvious test passed while proving
nothing. The renderer now keeps the placement list it built, and the tests check
that.

## P0-64 · The board stops moving; the waves get worse

Reported: "I don't like that it pushes back the levels of my game more each new
round. Could we have it be that the enemies just get more difficult so that I'm
forced to build more?"

That was three mechanics being felt as one. An act was harder than the act before
it because (a) it revealed a longer prefix of the map's road, (b) it raised the
deployment limit, and (c) it sent more and tougher drones. Only (c) is
escalation. (a) and (b) together mean the board keeps growing out from under a
line that is already built, so holding it is *stretching* rather than
reinforcing — which is exactly what was reported.

**The road, the deployment limit and the owned ground are now act IV's from act I
onward.** Four acts, one board. `path_waypoints` is gone from every wave file; the
key still works, so a future board may still use it, but nothing does.

**What was deliberately NOT done: rewrite the difficulty curve.** The first
attempt replaced every act's measured health multiplier and head-count with an
invented ramp scaled off act IV (`0.52/0.74/0.93/1.0`). Highway act I — the
gentlest level in the game — went to 29 leaks and a loss, and all four Highway
acts lost. Reverted. The change that shipped only removes the reveal and unifies
the limit; every hp multiplier and every drone count stays exactly as it was
measured. **A lift is applied FROM the measured baseline, not instead of it.**

Three things the measurement then found, each fixed with one number:

1. **Highway act II became idle-winnable.** It now inherits a full-size board on
   the full road, and 397 drones at ×4.09 could not get past it untouched.
   Lifted to 457 at ×4.70 — from the measured figure, ×1.15 on both — and it
   loses to a do-nothing run again. Nothing else in the campaign idle-survived.
2. **Terminus act III collapsed: 68 leaks, Integrity 0.** The fork used to open
   "once the corridor is fully revealed", which was act III. With a uniform
   limit, act III arrives already holding 208 of its 208 turrets, so the second
   road opened with nothing left to answer it. A fork is now a property of the
   board (`forks_open`), true in all four Terminus acts. The board you learn is
   the board you get.
3. **Every act's budget was tuned against its own old limit.** With one limit the
   rule "an inheriting act starts poorer than the act that opened the board"
   stopped falling out of the data, so acts II–IV are clamped to their own
   board's act IV budget — the leanest figure that board was ever measured with,
   always below its opener's. Highway act I is the one opener funded up (700 →
   875, the ratio its limit moved by): every other opener was measured reaching
   its full limit *at tier 4*, so its budget was never the constraint, while
   Highway's reached the limit at tier 1, and the competent run was down to 96
   Integrity by wave 8 — in the opening act of the game, the one act that must
   not.

Measured after: 48 of 48 levels won by the scripted competent run, 0 leaks on
every one. No act idle-survives — checked act by act across the first six
boards, and on the last act of every chain by the suite. 287 tests, 0 failed.

**A test that encodes a removed mechanic is worse than no test.** `test_chain`
asserted that each act extends the corridor and reveals more waypoints, and
`test_routes` asserted that a fork opens only at full reveal. Both were true, both
were now the opposite of the design, and both had to be rewritten rather than
deleted: `test_chain` now asserts the road, limit and reveal are *identical*
across a chain, and — the assertion that was missing all along — that every
consecutive pair of acts in the campaign brings more drones or tougher ones. The
escalation is the whole mechanic now, so it is asserted rather than assumed.

## P0-65 · Two systems that make composition a decision

Asked for more complexity, more difficulty and more strategy, with the choice of
what left to me.

The honest diagnosis first: **the campaign had four weapon families and one
shopping list.** Nothing in the game asked what you brought, only how much of it,
so the deployment limit went on whatever had the best numbers and "which family"
was never a question with a wave-dependent answer. Support links pushed weakly
towards mixing; nothing pushed towards mixing *this* way for *this* wave.

**1. A damage type against an armour class.** Three types (Kinetic, Explosive,
Energy), three classes (Light, Plated, Shielded), one 3x3 matrix in `damage.json`,
one multiply in `_damage_enemy`. 1.35 / 1.0 / 0.65 — a favourable matchup is worth
about two-for-one, enough to feel and not enough to kill a mono-family board.

Three decisions inside it worth keeping:

- **Three types for four guns.** Ballistic and Railgun are both Kinetic, and the
  axis that separates them already existed: flat armour comes off each HIT, so a
  wall of cheap fast rounds is punished by it and one big slow round is not. A
  fourth damage type would have bought nothing armour was not already saying.
- **The matrix scales the shot BEFORE armour bites.** The other order punishes a
  bad matchup twice, and the two systems are meant to ask different questions —
  the matrix asks what you brought, armour asks how big it is. The order is
  pinned by a test, not by this paragraph.
- **The floor is 1, not 0.** A round that rounded to nothing would make a family
  you have already paid for unusable rather than unwise.
- **Both support classes are Shielded.** That is what turns "kill the Menders
  first" from a hope into a plan with a price: bring an Energy line, re-task it.

**2. Act affixes.** Named modifiers on everything an act sends — Hardened, Swift,
Resilient, Relentless, Screened, Massed, Austere — announced beside the level name
before the first wave walks. Every one is a multiplier or a flat bonus on a value
the simulation already had, applied once at load, so **nothing an affix does
happens in a tick**: no runtime cost and no way to desync a replay.

Authored per act, never rolled. A modifier you cannot see coming is a surprise,
not a decision, and this game is deterministic so an act can be learned, lost to,
and beaten. Each board has one *signature* affix that stays the same across its
acts — "Refinery is the Screened board" is something you learn once.

**What the measurement changed, in order:**

1. **Two affixes on early act IVs is one system too many.** The first assignment
   gave every board a signature at act III and two at act IV. Highway IV and Port
   IV both LOST. The first three boards are where the triangle is being learned;
   stacking a modifier on that is two new things at once. The ramp is now: boards
   1–3 one affix and only at act IV, boards 4–6 a signature from act III, boards
   7–12 from act II with a second at act IV.
2. **The triangle made the early acts EASIER, not harder.** Highway's opening
   waves are all Light and the scripted policy leads with Ballistic, so Kinetic's
   1.35 handed act II back to a do-nothing run. Lifted x1.18 on both axes, then
   acts III and IV lifted above it (x1.25/x1.35 health, x1.20/x1.30 head-count) to
   keep a board's curve monotone — which `test_chain` now asserts, so it could not
   have been left broken quietly.
3. **The finale was over-tuned at Relentless + Resilient**: 7 leaks, Integrity 0,
   with all 208 turrets at tier 4 — unbeatable by the scripted run, carried board
   and all. Swapped to **Relentless + Austere**, which squeezes the economy rather
   than adding another raw-power multiplier, and it now wins at 20 Integrity with
   5 leaks. The closest act in the game, which is what a finale should be.

Measured after: 48 of 48 won by the scripted competent run. The campaign now has
five genuinely close acts instead of none — Port IV at 40 Integrity, Coldstore IV
at 52, Highline IV at 84, Highway IV at 88, Terminus IV at 20. No act
idle-survives. 317 tests, 0 failed.

**The audit pass found a hole the feature's own tests were blind to.** The rule
"both support classes are Shielded, so bring the right gun" was checked by
`test_the_support_drones_are_answerable_by_a_gun`, which asked whether the matrix
had a number above 1.0 against Shielded. It did — the Arc Suppressor's — and the
Arc Suppressor does **four damage a shot**. A Static Jammer has 340 health. The
sentence had no gun behind it: the matrix said yes and the arithmetic said no.

The fix is that the **Railgun is Energy**, not Kinetic. It is now the precision
anti-support gun — ninety-three damage a shot into the class both support drones
wear — which is what "pick the Mender out and kill it" was always supposed to
mean. It also fixes the thing that was wrong on the other side: a Railgun
described as the answer to Siege Breakers was 0.65 into them while it was Kinetic.
Cannon is the anti-plate answer now, which is what artillery is for.

The test that could not see it has been replaced by one that can, and it needs no
magic threshold: **for every armour class, something favoured into it must hit
harder than the feeblest gun in the arsenal.** The comparison is against the
arsenal's own floor, so it stays true whatever the numbers become.

The re-measurement after that swap moved the campaign around and is the reason
Terminus IV ended up with three affixes: Relentless + Austere had been tuned
against Kinetic Railguns and read as a 100-Integrity walkover once they were
Energy. Relentless + Resilient + Austere brings it to **4 Integrity and 6 leaks**
— the hardest act in the game, which is what a finale is for. The middle boards'
finales gained Austere too; it thins bounties without adding raw power, and the
measurable effect is exactly the intended one — Refinery IV still holds, with 52
of its 68 turrets at tier 4 instead of all 68. You can no longer afford
everything.

**A screenshot caught what no test could.** The wave preview groups the incoming
wave by armour class, and it shipped for one build reading `LIGHT 371 ()` — a
`PackedStringArray` held inside a `Dictionary` is a VALUE in GDScript, so
appending to a local copy of it drops the append on the floor, silently. Every
assertion about the preview would have passed; the string was simply empty. The
buckets are plain `Array`s now. The same capture found the first version of the
line running off the right edge of a 1280-wide window and losing the Shielded
drones at the end — the ones that most needed reading — which is why enemies grew
a `short_name` and the preview groups by class instead of tagging every group.

## P0-66 · Tiers carry, and the waves absorb the price

Reported: "Every time I go to a new level, it resets the levels of my weapons.
Let's not do that."

That reset was P0-51's deliberate answer to the cutscene problem - an act that
inherits a finished tier-4 board is won by that board with no input at all - and
the reasoning still holds. The experience does not. A tier is the most expensive
thing a player buys; buying one knowing it expires at the act boundary is a worse
decision than not buying it, which turns the game's main money sink into a trap.
`CARRY_TIER_CAP` was deleted outright - the purity linter rightly refused an
unbounded sentinel pretending to be a balance value - and the price moved off the
player and onto the waves: acts II-IV are now authored against a board that
arrives intact.

**The gate had to change with it, honestly.** "Losable by building nothing from
the inheritance" stopped having a yes: the last act of a chain arrives holding a
full-limit, full-tier board, and there is nothing a competent run can add that an
idle one lacks. Demanding that gate is demanding the inheritance be broken, which
is the thing that was just reported as a bug. The gate is now: winnable by the
competent run, and losable from a CLEAN start - the claim that the waves demand
the board the chain builds.

**What the re-measurement taught, in order:**

1. The lift needed is act-position-graded, not flat: inheriting an act I board
   (mostly tier 1-2) is worth little; inheriting an act III board (all tier 4) is
   worth the whole tier ladder, which is 8-10x DPS family by family. Acts II got
   x1.4 health, acts III x2.4, acts IV x3.4 on the measured baseline - then
   per-board corrections downward where the probe lost.
2. `max_enemies` 2048 -> 4096. The load-time pool validator caught three late
   acts whose lifted peak head-count would have silently dropped drones at the
   worst moment - exactly the failure it was built to catch.
3. **Chained integrity is a real difficulty channel now.** Terminus IV kept
   losing by exactly 4 leaks at wildly different health values, and the reason
   was upstream: act III was ending at 52 Integrity, and the finale inherits
   that. Softening the finale could not fix arriving wounded. Act III was eased
   until it ends at 100, and the finale was tuned from there - it now wins at 20
   Integrity with 5 leaks, still the closest act in the game.
4. The escalation test had to learn what an affix is: Highway IV reads x12.89
   raw against act III's x14.58, but it is Swift - as played it is the harder
   act. The test now compares effective values (hp and count with affix
   multipliers folded in) instead of raw file numbers.

Measured after: 48/48 won by the scripted run playing each chain with its real
inheritance; the late-campaign spread is Lastlight IV at 25 Integrity, Terminus
IV at 20, Gantry IV and Coldstore IV at 52, Highline IV at 52. Every opening act
still loses to a do-nothing run.

## P0-67 · Detail is bought with triangles, not draw calls

Asked for cooler weapons and more realistic enemies. The constraint that shaped
the answer is the project's oldest render invariant: draw calls stay flat no
matter how much is on screen, because everything that scales goes through a
MultiMesh. So the quality lever is not more objects - it is making the ONE mesh
each layer instances richer. `_merged()` welds a handful of primitives into a
single ArrayMesh with SurfaceTool, and every turret part and drone class went
from one primitive to a small assembly:

- Ballistic: receiver + optics block + slung ammunition drum, twin barrels with
  a muzzle brake. Cannon: the recoil taper + collar + bracing haunches, a real
  forward-pointing bore with a muzzle ring. Suppressor: the drum wound with
  three coil rings, the muzzle ring gaining a floating focus hub. Railgun: sled
  + twin capacitor banks + heat-sink stack, twin rails with spacers where the
  bare unit box used to be. Mounts all stand on a foundation slab with a seat
  collar now - the difference between "a shape on the grass" and "installed".
- Drones: the Skitter gets a swept tail fin, the Walker a sensor head and
  shoulder plates, the Bulwark a sloped glacis and layered top plates (it wears
  Plated in the matrix; it should look like it), the Lance swept side blades,
  the Mender its tool halo, the Jammer the dish it jams with, the Brood a
  visible cargo belly, the Breaker a ram and spine plate.
- Turrets got their own material - machined metal at 0.55 metallic - instead of
  borrowing the drones' matte skin. Both live in theme.json.

Measured: draw calls flat at 38 from 0 to 400 enemies, which is the invariant;
~110ms a frame on the software rasteriser, within noise of the pre-change 109.

**The screenshot bug of the day:** fifty-one freshly assembled turrets rendered
as NOTHING - `ArrayMesh` has no `material` property, unlike every PrimitiveMesh
the file had ever assigned one to, so the assignment failed and the layers drew
uncoloured nothing. Every mesh now goes through `_skin()`, which does it per
surface. The tests all passed while the board was invisible; the screenshot did
not.

## P0-68 · Spikes in the stream, and the one active ability

Asked for more dynamic play and more strategy, choice of features mine. The gap
both fill: between waves the game is all decisions, and during a wave it is
none - you watch the line you built be right or wrong. Nothing asked for a
decision at combat speed.

**Champions** put the spike in the stream: every 20th spawn of a big wave group
arrives at 4.5x health, 5x bounty, half again as large and white-hot. A steady
stream is answered by a steady line; a spike is answered by a targeting order or
by fire worth holding. Split children never champion - the spike is the carrier,
not its litter - and small elite groups never reach a 20th spawn, so Breakers do
not silently double. Deterministic by construction: the counter is per-group.

**Overcharge** is the answer at combat speed: O on a turret, $120, a few seconds
at 2.2x rate and 1.6x damage, then that turret is spent for 24s. It is a
command like any other - tick-addressed, logged, replayed, hashed - and it is
refused while jammed, because the counter to a Jammer is killing the Jammer,
not paying to ignore it. The surge keeps burning while jammed, which is exactly
the play a Jammer wants to make against a surging turret.

Champions are also a tax the measurement had to collect: ~5% of a big group's
head-count arrives 4.5x tougher, and at champion_hp_mult 7.0 (the first guess)
Port IV and Capital IV both LOSS'd and Capital III fell to 8 Integrity. The
knob came down to 4.5 and four acts got small per-act corrections; 48/48 wins
again, with the campaign's late spread now Lastlight IV at 8 Integrity, Capital
IV at 40, Terminus IV at 73.

Enemies also got their identity said in colour, not just silhouette: one colour
per class (warm yellows/oranges for Light, greys and bronzes for Plated), cold
glows on the two Shielded support drones - the ones the player is told to pick
out of a crowd at 4x speed. Champions render 1.5x and tint white-hot; an
overcharged turret glows the same heat the whole time its surge runs.

## P0-69 · The strategy layer: whether, which, where, and worth protecting

"It feels very simple" - and the measurement agreed: the scripted player, which
never retargets, never overcharges and never reads the matrix, was winning all
48 levels. Every system so far made the game richer without ever making the
dumb plan FAIL. Strategy only exists where "carpet the road, upgrade
everything, any order" loses. Four systems, one per missing dimension:

- **Salvage Rig** - WHETHER to build a gun. A blueprint whose tiers carry
  `income_per_wave` instead of ballistics: it earns at the top of each wave
  (never the first - it must be risked before it pays), stands against the same
  deployment limit, and earns nothing while jammed, so the Jammer threatens an
  economy board exactly as much as a gun line. The validator refuses a tier
  with both damage and income - a gun that prints money answers every trade at
  once.
- **Doctrines** - WHICH gun it becomes. At top tier, one of two permanent
  specializations per family, free (it is the identity half of the tier-4
  purchase, not another purchase), refused on second thoughts - an either/or
  you can undo is a menu. AP Core is the interesting one: rounds that ignore
  flat armour, the one way a wall of small rounds ever answers plate, bought by
  giving up Shredder's rate.
- **Premium ground** - WHERE it stands. Three or four authored gold cells per
  map (+25% range or +30% rate), placed by a script that walks the road and
  offsets into the buildable band. Scarce on purpose; a fork's good tiles do
  not split evenly.
- **Veterancy** - which guns are WORTH PROTECTING. Kills earn ranks, ranks earn
  +5% damage each, ranks die with a sell and carry between acts. Kill credit
  rides the projectile's source slot; after a sell compacts the pool an
  in-flight round can credit the swapped-in turret - deterministic, rare, and
  bounded to one kill count, which is recorded here rather than hidden.

None of it moved the balance gate: the scripted player builds no rigs, chooses
no doctrines, and only stands on premium ground by accident, so 48/48 stands
measured and every new lever is pure upside a human can reach for. All four are
commands or data through the same pipeline - logged, replayed, hashed - and
test_strategy holds the contract: 15 tests, including bit-exact replay of rig
placement and doctrine choice.

## P0-70 · The lag was looking for nothing, 20,000 times a tick

Reported: "the game feels a little laggy." Profiled rather than guessed, with a
new tool (`tools/frame_profile.gd`) that times each stage separately, because
"feels laggy" is not a number and the existing render-stress tool only counts
draw calls. On Terminus act III with 208 turrets and 30 drones alive:

    renderer.note_tick (per tick)   1.82ms      x4 at 4x speed
    renderer.update_visuals         1.94ms
    renderer.refresh_board         16.28ms      on every click
    sim.step                        5.50ms      x4 at 4x speed

A 60fps frame is 16.6ms. At 4x speed the sim alone wanted 22ms, so the game
could not keep up and `MAX_STEPS_PER_FRAME` silently clamped it - the lag was
real and the cause was almost entirely **sweeping empty pool slots**.

The pools are 4,096 enemies and 8,192 projectiles. Every tick swept all of both,
plus the renderer's three diff loops - roughly 20,000 iterations a tick to find
thirty live things, four times a frame. Three fixes, all cost-only:

1. **Live high-water bounds.** The free lists hand out the LOWEST free index
   first, so live slots cluster at the bottom and a high-water mark bounds every
   sweep tightly. It resets exactly when a pool empties. note_tick 1.82ms ->
   0.07ms; `_advance_enemies` 0.06ms.
2. **An exact reject before the hash walk.** 208 turrets each scanned a 7x7
   neighbourhood of hash cells whether or not anything was near them. The
   spatial hash now reports the bounding box of everything in it - measured in
   the pass already reading every position - and a turret whose reach does not
   touch that box provably has no target. `_update_platforms` 4.29ms -> 0.14ms,
   and `sim.step` 5.50ms -> 0.49ms.
3. **Splitting the board rebuild by trigger, and bucketing the links.**
   `rebuild_link_pairs` was the obvious double loop: 43,264 iterations at 208
   turrets, 8.4ms, on every placement AND every upgrade. Only turrets that
   actually lend are collected, and they are bucketed on the build grid that
   already exists. The ground overlay - 6.9ms sweeping every cell - now redraws
   only when ground is BOUGHT, which is the only thing that can change it.
   A turret click went 16.3ms -> 4.6ms.

Steady state at 4x is now ~3ms a frame against a 16.6ms budget, and the test
suite came along for the ride: test_engagement 220s -> 73s, test_brood 128s ->
20s.

**Three attempts at this were wrong, in the same way each time: a bound or a
cached value that could go stale independently of the thing it described.** All
three were caught by the audit, and the fixes are structural rather than careful:

- The enemy box started as a sim field measured beside the hash rebuild. Any test
  or future code that moved a drone without stepping got a stale box and turrets
  that would not fire - which is exactly how `test_a_jammed_turret_does_not_fire`
  failed. The box now lives ON the hash, computed inside `rebuild()`, so it is
  exactly as fresh as the hash is, always.
- The high-water mark was raised inside `_fire()` rather than at the claim, so a
  test that took a projectile slot off the free list the same way `_fire()` does
  was invisible to the renderer. Claiming a slot is now one function that raises
  the mark, and nothing else pops those lists.
- The renderer's diff loops first used "this tick's bound or last tick's", which
  is correct only if `note_tick` runs every single tick. A test calling it twice
  caught it. They now keep a bound that only grows and drops to zero after a
  sweep that found the pool already drained - correct at any cadence.

**Proof it is cost-only, not behaviour:** the same probe run against the stashed
pre-optimisation tree returns identical results, act for act, kill for kill -
Port IV at 88 integrity and 1,436 kills both before and after. 350 tests, 0
failed. And a "skip cells that are not buildable" reject in the ground overlay
is recorded as REJECTED: it measured slower, because most of the band beside the
road is buildable and it added a call per cell to reject almost nothing.

## P0-71 · A drone that costs you the map

Asked for more strategy, and for something like "a tunneling enemy that starts
making a new part of the map". The architecture took it almost as-is: routes are
already N complete gate-to-exit polylines, so a new road is a route the wave
director is not yet using.

**The Breach Borer** walks two thirds of its road, goes under, and opens a
dormant breach route for the rest of the act. It never reaches the exit, so it
costs no Integrity - it costs the MAP. Killing it before its tunnel point is the
whole counterplay, which makes it the first drone where killing something FAST
ENOUGH matters rather than just killing it.

**The road is a fact about the board, not about the act.** Breach geometry is
present from the first tick of every act on an armed board, for two reasons that
both had to be learned the hard way:

1. Armed on act IV only, act IV dropped SIX inherited turrets on load - the road
   materialised where they stood. That is exactly what P0-64 forbids, and the
   probe's `carry 138` was the only sign of it.
2. The dormant road has to be inside the buildable band before it opens, or the
   player cannot prepare, and an unpreparable threat is not a decision.

**Where the road runs is a balance value.** The first breaches bulged through the
middle of the board and stole 3-8% of the main road's build sites - enough that
Lastlight act III LOST with nine leaks, on an act with no Borer in it at all.
Re-routed along whichever lane sits furthest from every existing road, the loss
is under 1.1%, all of it at the shared gate and exit, which is unavoidable.

**And the fixture had to learn what a dormant road is.** With the geometry
present, the scripted policy spread its whole allowance across three roads when
only two carried traffic - thinner everywhere that mattered, and Lastlight III
still lost. `candidate_sites` now samples OPEN roads only. What a human does
about a dormant road is a decision; what the policy does is defend the traffic.

Measured after: 48/48 still won. The breach is felt where it should be - Reactor
IV drops from 100 Integrity to 67, Lastlight IV to 35, Gantry IV to 57 - because
the scripted bot never kills the Borer in time and the road opens on it. A human
who reads BREACH ARMED and puts a Cannon line on the approach keeps it shut.

**The parse error that nearly shipped.** A triple-quote inside a patch ate the
closing quote of a HUD string. `hud.gd` stopped parsing, which means the GAME
would not have started - and the only reason it was caught is that the test
runner treats "this file defines no tests" as a FAILURE rather than as nothing
to run. That rule was written after a whole file of tests once vanished
silently; this is the second time it has paid for itself.

## P0-14 · Deliberately not built in P0

Not oversights — later phases, per §5.7. Anything tempting that came up is in
`post-launch.md`.

- Sell/refund, tier upgrades T2–T4, send-wave-early, targeting priority UI (P2)
- Armour triangle, other four families, status effects (P1)
- Run layer: route map, Corridor Integrity persisting across engagements,
  Salvage, Depots, bosses (P3) — P0's integrity is engagement-scoped
- Modules and the modifier pipeline (P2/P4)
- Save/load, Intel, Commanders, Daily Contract (P5)
