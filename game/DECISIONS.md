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

## P0-72 · The lag I could not reproduce, and the switch that answers it

"Still a little laggy." Everything in P0-70 was real and is still fixed, but it
was all CPU, profiled on the desktop build. The game is played as single-threaded
wasm in a browser, so this time the thing measured was the thing experienced:
frames actually presented in Chromium, on a real board, at 1280x720.

**The honest limit of this investigation: the container has no GPU.** Chromium
here runs on SwiftShader, which is fill-rate bound in a way no real device is, so
its absolute numbers are meaningless and only the DIFFERENCES between
configurations mean anything. Isolated, on one board: scenery is worth about a
third of the frame, the shadow pass about a quarter, and the rest is raw fill.

Two real fixes came out of it, plus one admission.

**1. Shadow casting was on for everything.** `_instanced()` set
SHADOW_CASTING_SETTING_ON for every MultiMesh layer, which meant the health bars,
the tracers and the transparent ground overlay were each rendered a second time
into the shadow map - unshaded decorations casting shadows nobody can see. The
default is OFF now and turrets and drones opt back in.

**2. The web canvas renders at window size TIMES the display's pixel ratio.**
`canvasResizePolicy: 2` means a 1600x900 browser window on a HiDPI screen is a
3200x1800 render target: four times the pixels this scene was designed and
profiled at, every one paying for per-pixel fog, a normal-mapped ground and a
shadow lookup. Invisible from a command line, and it reads to a player as exactly
"a little laggy". `scaling_3d_scale` now holds the 3D pass to a pixel BUDGET
rather than a resolution, so it does the right thing at any window shape and
never scales up. The HUD is canvas_items and stays crisp regardless.

**3. And the admission: I cannot profile the machine this is played on.** So the
expensive things are bundled into three steps on F2, remembered with progress
because nobody wants to find that setting twice. Measured in-browser on one board,
median frame time: HIGH ~450ms, BALANCED ~367ms, FAST ~183ms - a 2.5x spread, and
the ordering is what matters since the absolute numbers are SwiftShader's.
BALANCED gives up the shadow pass and keeps the scenery, because a board without
shadows still reads and a board without scenery is a diagram; FAST gives up both.
None of it touches the simulation, so a replay is unaffected and two players on
different settings are playing the identical game.

**The harness hole this opened, which mattered more than the lag.** A type
inference error - `var budget := ... * QUALITY_PIXEL_SHARE[_quality]`, where
indexing a const Array yields a Variant - stopped `sim_renderer_3d.gd` compiling.
Every renderer test's setup died quietly and the suite printed **"16 tests, 1
assertion, 0 failed"**: a green run over a build whose renderer would not load.
Counting tests is not the same as checking anything, and only the assertion count
knew. The runner now fails a file that runs tests and asserts NOTHING. That is
the third time a guard of this shape has caught something the tests themselves
could not see.

## P0-14 · Deliberately not built in P0

Not oversights — later phases, per §5.7. Anything tempting that came up is in
`post-launch.md`.

- Sell/refund, tier upgrades T2–T4, send-wave-early, targeting priority UI (P2)
- Armour triangle, other four families, status effects (P1)
- Run layer: route map, Corridor Integrity persisting across engagements,
  Salvage, Depots, bosses (P3) — P0's integrity is engagement-scoped
- Modules and the modifier pipeline (P2/P4)
- Save/load, Intel, Commanders, Daily Contract (P5)

## P0-73 · Forward+ on the desktop, Compatibility on the web, and one glow tuned twice

The question was whether a different engine would give better graphics. It would
not, and the project's own config says why: `renderer/rendering_method` was
`gl_compatibility` everywhere, so the desktop build was giving up SSAO, real HDR
bloom and high-quality shadow filtering in order to match a limit that only the
web build actually has. Forward+ does not target WebGL, so that is a browser
ceiling rather than a Godot one — Unity's WebGL export meets the same wall, and
Unreal has had no web target since 4.27.

So the base method is now `forward_plus`, with `.web` and `.mobile` pinned to
`gl_compatibility`. Those two overrides are ENGINE defaults, not ours: a project
that writes neither still reports `gl_compatibility` on those platforms, which
was verified by deleting both lines and asking. They are written out anyway so
the split is visible to whoever opens the file instead of being a property of a
Godot version. Verified from both ends — the exported Linux binary logs
`Vulkan 1.4.318 - Forward+`, and the web build logs
`WebGL 2.0 ... - Compatibility`.

The first Forward+ frame was worse than what it replaced. The whole board washed
out to near-white: the road markings, the treeline and the turrets all
disappeared. The cause was `glow_bloom`, which adds a constant fraction of EVERY
pixel back into the image whether it is bright or not. Compatibility approximates
glow and largely ignores that term; Forward+ runs a real HDR bloom and honours
it. The 0.85/0.1 that reads as a gentle lift on the web build is a full-screen
wash on Forward+.

The fix is not one set of numbers that offends neither renderer. It is `_lit()`,
which prefers a `<key>_hdr` entry from the theme when a RenderingDevice exists
and falls back to the plain key otherwise, so a value stays a single number until
the two renderers actually disagree about it. Only glow has needed a second entry
so far: on Forward+ the constant term goes to zero and `glow_hdr_threshold` does
the work, so only genuinely overbright things — muzzle flashes, tracers, the
emissive bands on menders and jammers — bloom, which is what the glow was for.

Worth recording as a general shape: a renderer swap is not a settings-preserving
operation. Every value tuned by eye was tuned against one renderer's
interpretation of it.

## P0-74 · Ten procedural surfaces, and a quality ladder that has three rungs because the measurement said so

Everything on the board except the ground was a flat colour. That survives at a
distance and fails the moment anything is near the camera: a wall, a turret and a
rock lit identically are three shades of one material, and the eye reads the
whole board as plastic. `render/material_library.gd` now generates, per named
family, an albedo variation, a tangent-space normal map and a roughness map from
one shared noise field, and `data/materials.json` holds the art direction for ten
families — ground, verge, road, wall, turret, enemy, rock, bark, canopy, prop.

Generated, never shipped, for the same reason as the sound effects: no binary
assets, and a surface becomes something reasoned about in a JSON block instead of
opened in an image editor.

The one field that does most of the work is `lattice_x` against `lattice_y`.
Equal counts give isotropic grit — soil, stone, concrete. Unequal counts stretch
the noise along an axis, and that single asymmetry is the entire difference
between asphalt dragged along the road, brushed metal on a turret housing, and
the vertical grain of bark. Three families, one generator, no resemblance.
Grooves (`panel_x`/`panel_y`) do the other half: noise alone reads as rock
however it is tuned, because nothing in nature repeats on a grid, so manufactured
things get seams and natural ones are asserted not to have any.

Three things this got wrong first, all caught by measuring rather than by
looking:

- **Panel grooves on a welded cylinder read as corrugation, not machining.**
  Horizontal seams wrap a turret housing and come out looking like flexible hose.
  Vertical seams run along it and read as panel joins. `panel_y` is now 0 on the
  turret family.
- **The library was being rebuilt on every level load.** `main.gd` constructs a
  fresh `SimRenderer3D` per act and frees everything else, so a renderer-owned
  library regenerated all ten families each time — 270 ms measured, on 48 acts.
  It is now held for the session and handed in, exactly as `_sfx` already was for
  the same reason and with the same comment. The second pass over all ten
  families measures 0.10 ms, which is what a cache is supposed to look like.
- **The tier switch configured nodes and then threw them away.** Surface maps are
  baked into materials, and a material already handed to a MeshInstance does not
  re-read its textures, so changing tier has to rebuild every node that owns one
  — and it has to do that BEFORE the shadow and visibility flags are applied, not
  after.

Then the cost, measured in a browser rather than guessed, on one board:

| tier | maps | frame time | vs before the art pass |
|---|---|---|---|
| HIGH | albedo + normal + roughness | 667 ms | +48% |
| BALANCED | albedo + roughness | 383 ms | +4% |
| FAST | none | 183 ms | unchanged |

That table is why the ladder has three rungs instead of a switch. All three maps
cost roughly what the shadow pass costs, and nearly all of it is the normal map —
the one that has to be decoded and re-based per pixel where the others are plain
fetches. BALANCED drops it and keeps the albedo and roughness variation, which is
most of what stops a surface reading as plastic, for 4%. FAST was already
unchanged because it had stopped paying before the art pass existed.

Same caveat as P0-72 and it has not stopped being true: this container has no
GPU, so the absolutes are SwiftShader numbers and only the ORDERING and the
RATIOS mean anything. Software rasterisation penalises texture sampling harder
than real hardware does, so 48% and 4% are more likely to be ceilings than floors
on a machine with a GPU.

`tools/material_probe.gd` reports the per-family generation cost and proves the
cache is one, because none of the above is visible from a screenshot.

## P0-75 · "Missing frames": what the measurement actually said, and the one real bug it found

Reported as dropped frames rather than a low frame rate, which is a different
claim and needs a different measurement — an average cannot show a stutter,
because a steady 40 ms and an alternating 20/60 ms have the same one and feel
nothing alike. `tools/frame_jitter.py` records every rAF delta over a long
window, after a warm-up so level load and shader compilation are not counted as
stutter, and reports the shape.

The answer, on this machine, was that there is no stutter to find:

| tier | median | p99 | max | frames over 2x median | median frame-to-frame change |
|---|---|---|---|---|---|
| BALANCED | 649.9 ms | 733.2 | 733.2 | 0 (0.0%) | 25.1 ms (3.9%) |
| FAST | 283.3 ms | 333.3 | 333.3 | 0 (0.0%) | 0.0 ms (0.0%) |
| HIGH | 1066.6 ms | 1200.0 | 1200.0 | 0 (0.0%) | 0.0 ms (0.0%) |

Frame delivery is perfectly uniform. Every value is also an exact multiple of
16.67 ms, which is the tell: rAF is locked to the display, so a frame that costs
283 ms is presented on one refresh out of seventeen and the other sixteen show
the previous image again. That IS "missing frames" in the literal sense, and it
is a consequence of being slower than the display rather than a scheduling fault
— nothing here is going to be fixed by smoothing.

So the rest of the pass was spent eliminating the hardware-independent causes
rather than guessing at hardware I do not have. Checked and cleared: the tick
loop is a correct fixed-timestep accumulator; enemy motion is interpolated on
distance-along-path, so it rounds corners rather than cutting them; the HUD is
signature-gated and rebuilds no text unless something changed; sound plays from
a pre-allocated, throttled voice pool with no per-shot allocation; draw calls
stay flat at 22 from 0 to 400 entities, so the per-family materials did not cost
the MultiMesh invariant. Steady-state CPU is 1.3 ms of `update_visuals` plus
0.73 ms per simulation tick.

One real bug, and it was in the accumulator:

```gdscript
if steps >= MAX_STEPS_PER_FRAME:
    _accumulator = 0.0        # drops the backlog AND the sub-tick phase
```

The phase is exactly what `alpha` is. Zeroing it meant every capped frame
rendered at `alpha` 0, so on a machine slow enough to hit the cap, interpolation
stopped happening precisely when it was most needed and everything on the board
snapped from tick position to tick position. `fmod(_accumulator, _tick_period)`
drops the whole ticks that cannot be afforded and keeps the fraction. Dropping
the backlog was always right; dropping the phase never was.

And one real hitch, which is the thing most likely to be perceived as a skip
because it lands on a click. Rebuilding the ground overlay swept all 2,960 cells
asking the simulation three questions about each — `cell_is_unlocked`,
`cell_is_offerable`, `cell_premium` — each of which made more calls of its own.
Around twenty thousand cross-object dispatches per ground purchase, measured at
8.9 ms: half a frame at 60 Hz spent on dispatch rather than on work. The flags
are now read as arrays and rejected on directly, and 15.2 ms → 12.4 ms for a
ground purchase, 8.9 ms → 6.6 ms for the sweep itself.

Deliberately NOT done: re-deriving the adjacency rule in the renderer. The cheap
array reads reject most of the grid, and whether a cell can actually be offered
is still asked through `cell_is_offerable()`, which owns that rule. An overlay
free to disagree with what the player can really buy would be a worse bug than
any frame it saved.

`F3` now reports the worst frame in the last second and a count of frames that
took over twice the typical one, because the average it reported before could
not have shown any of this — and because the next report of this kind should
arrive as numbers from the machine that has the problem.

## P0-76 · The game now notices it is running badly, and says what it is running on

The F3 readout came back from the machine that has the problem:

```
fps 11.5 (frame 86.68 ms)   worst frame 93.2 ms   long frames 0/s
sim steps/frame 3   platforms 2   enemies 12   draw calls 24
```

Three things fall out of that, and together they close the question P0-75 left
open.

**It is not a stutter.** Worst frame 93.2 ms against an 86.7 ms average, zero
long frames. That is uniform slowness, which is exactly the shape P0-75 measured
here and could not reproduce as anything else. Nothing is hitching; the frames
are all just expensive.

**It is not the board.** 11.5 fps on wave 1 of level 5 with TWO platforms and
twelve drones. `tools/render_stress.gd` already said the same thing from the
other direction - 130 ms/frame with zero enemies on screen - so entity count is
close to irrelevant and the cost is static fill. Every optimisation aimed at
entities, targeting or the tick loop was therefore never going to move this
number, which is worth knowing before spending another day on one.

**It is very probably not a GPU at all.** Eleven frames a second on an almost
empty scene at roughly 920,000 pixels is not what any hardware accelerator does.
It is what a software rasteriser does. So the overlay now reports
`RenderingServer.get_video_adapter_name()` and its vendor, because "llvmpipe",
"SwiftShader" or "Software" in that line means the browser is not using the GPU
and nothing tunable in this repo will fix it - and reading it takes two seconds
where guessing at it took two rounds. It also reports the current tier and the
3D scale, since neither was visible and both change what every other number in
the readout means.

The fix for the general case is that the game no longer waits to be told. F2
only ever helped a player who knew F2 existed. `_govern_quality()` watches the
real frame time and steps the tier down after sustained slowness, saving the
result so it survives a reload.

Three rules, all of them about not fighting the player:

- **It only steps DOWN.** Stepping back up on a quiet moment gives a game that
  oscillates between tiers, and a stutter caused by the fix is worse than the
  one it fixed.
- **F2 ends it for the session.** An explicit choice is an explicit choice,
  including the choice to run HIGH on a machine that cannot hold it.
- **It waits for SUSTAINED slowness** - five seconds by default, and any single
  good frame resets the count. A level load, a shader compile and a backgrounded
  tab all produce enormous single frames, and none of them mean the player
  should silently lose their graphics.

Both thresholds are in `theme.json` like every other tuneable, and the tests pin
the boundary rather than the happy path: 25 fps against a 24 fps floor must never
trip, 60 fps must never trip, one four-second frame among good ones must be
forgiven, and a run of slow frames broken by a single good one must start over.

Verified in the real game rather than only in the harness - this container is
slow enough to be its own test case. Starting from a cleared progress file and
running for 45 seconds, `progress.json` came back `{"boards_unlocked":1,
"quality":2}`: it stepped BALANCED to FAST by itself and persisted it.

## P0-77 · A vendor-neutral handoff, and the documented test filter that never worked

The game had to become something another agent — from any vendor — could pick up
and run with. Two problems stood in the way.

**The repository routed newcomers to the wrong project.** The root `AGENTS.md`,
which is the file most agent tooling reads first, described only MRF Explorer
and never mentioned `game/` at all. Anyone landing here for the game would have
been sent to `CLAUDE.md` and `docs/AI_HANDOFF.md` and ended up in a Python
transparency-in-coverage pipeline. It is now a router: two unrelated projects, a
table saying which is which, and the explicit warning that the invariants of one
say nothing about the other.

**`game/AGENTS.md` is new** and is the operational document: the environment
traps, the enforced invariants, every command, the layout, the working method,
current state, and the one open investigation. It deliberately does not repeat
`README.md` (what the game is) or this file (why it is that way) — it covers the
things that are true but written down nowhere, which is where an hour goes.

Writing it turned up a real bug, which is the argument for writing these by
running every command rather than by remembering them.

`run_tests.sh` ended with:

```bash
exec "$GODOT" --headless --path "$HERE" --script res://tests/run_tests.gd
```

No `"$@"`. So the invocation advertised in both `README.md` and the runner's own
doc comment —

```bash
./game/run_tests.sh -- --case test_materials
```

— silently dropped the filter and ran the entire suite. Six minutes instead of
five seconds, with nothing to indicate the argument had been ignored; it just
looked like the suite was slow. The runner has understood `--case` since it was
written. Nothing was ever handing it one. Fixed by forwarding the remaining
arguments, with `--full` shifted off first since it is an environment switch.
Verified: `--case test_auto_quality` now runs 7 tests in 369 ms.

That failure has the shape this project keeps rediscovering, in a new place: a
thing that reported success while doing nothing. The green suite over a
non-compiling renderer (P0-72) and the frame counter that could not tell
slowness from stutter (P0-75) are the same bug wearing different clothes. A
documented command nobody had checked against its own implementation is simply
the documentation-shaped version of it.

## P0-78 · Ares Station: a base-defense mode that is a route generator, not a second simulation

Prompted by Ares Outpost (francisstudio.itch.io): a base in the middle, waves
converging from all sides, bosses on a cadence. The decision that made it
affordable is that the mode is a different way of PRODUCING routes, not a
different simulation. An outpost map declares a `base`, a ring of authored
`gates`, and three radii; `_build_outpost_path()` emits one two-point route per
gate, `_commit_routes()` — extracted from the corridor path builder so there is
exactly one implementation of what a route is — turns them into the same flat
segment tables every corridor board uses. Enemies still carry one scalar
distance along one polyline; leaking at the end of the route IS reaching the
station. Sampling, targeting, broods, the spatial hash, the state hash and
replay are untouched, and the replay test on the siege passes without any
outpost-specific work.

The gates are authored positions, not points on a circle — computing them would
need trigonometry the sim may not use, and hand placement lets a board have a
cheap side and a dear side, which is the whole opening decision. Ground is a
ring: `cell_buildable` rejects inside `core_radius` (the station) and outside
`build_radius`; the free start is `start_radius`. "Endless" waves are expanded
into a concrete list at LOAD by `_expand_endless()` — archetypes cycle by index
so wave 12 is learnable, bosses land every fifth wave on top of their wave, and
growth compounds by repeated multiplication, not pow(). The tick never learns
the mode exists; a replay of a siege is a replay of a fixed list.

What the measurement said, tuning it:

- The greedy probe sampled sites gate-first and placed 4 turrets of a 34 limit:
  on this board the unlocked ground is the ring at the END of every route.
  Sampling now runs base-outward on outpost boards. 4 built → 34.
- Money alone made it worse (kills fund the economy; gentler waves paid less).
- At 40 waves the probe hit the deployment limit — 48/48 built — and still
  drowned. Per-lane duty cycle means a turret here does roughly a sixth of a
  corridor turret's work, while wave mass compounds. The verifiable length is
  14 waves: WIN, 1,369 of 2,600 hull left, 33 built. Leaky and held, which is
  what a siege should feel like. Longer sieges wait on teaching the probe to
  buy ground (post-launch).
- Two existing gates caught the first wave authoring: Lances landed on wave 2
  and reached 8% of head count. The data changed, not the tests.
- One name-proxy died: "opens a chain" was tested as "engagement ends in act1",
  and the siege is a single-act board. The test now derives it from being the
  first level on its map.

New content riding along: `titan` and `harbinger` boss classes — slow, plated,
enormous (radius 74/96 against the Bulwark's 24) and worth a fortune, because a
boss that is merely a big number is a spike, and slow-plus-bounty makes it a
siege that funds the answer to the next one. The station mesh is welded
primitives like everything else, low and wide so it never occludes the lanes
converging on it.

## P0-79 · Authored art replaces the generated meshes, and it made the game faster

Sixteen sprites arrived as one 4x4 sheet - five turret families and eleven drone
classes, top-down, facing up. They are now what the game draws.

**This repeals the no-binary-assets rule for entities.** That rule earned its
keep for a long time and is still right for surfaces (`material_library.gd` is
untouched, and the ground, road, walls and scenery are still generated). But it
was a constraint adopted because there was no art, not a principle - and holding
it in front of art this much better than anything a noise field will produce
would have been dogma. `game/AGENTS.md` is amended in the same commit, because a
handoff document that still says "there are no images in this repository" would
send the next person to regenerate over the assets.

**The slicer is committed, not run by hand.** `tools/slice_sprites.py` cuts the
sheet and keys it. Two things in it are less obvious than they look:

- The backdrop is not one colour - it samples 107 to 124 - so a global colour key
  either leaves a halo or eats the art's own greys, and the Breaker is almost
  entirely mid-grey. Background is instead found by flood filling inward from
  each cell's border: a pixel is background only if it is grey-ish *and*
  connected to the edge, so the Breaker's grey is protected by its own outline.
- That leaves pockets. The Mender's glow arcs loop round and seal backdrop
  inside them, which shipped as opaque grey holes. Anything within ALPHA_LOW of
  the backdrop is indistinguishable from it and can be cleared regardless of
  connectivity - measured, only 2.2% of the Breaker's body sits that close - but
  bounded by region size, because a lone near-grey pixel is a highlight and
  clearing those would punch speckle.

Edge alpha ramps with distance-from-backdrop and the colour is then solved back
out of the blend (F = (C - (1-a)B) / a), which is what stops a grey fringe
reading as a dirty outline on a dark board.

**In the renderer** each layer simply gets a textured quad instead of a welded
mesh. Turret families collapse from three layers to one: the sprite is the whole
emplacement, so the mount and muzzle are inside the picture and the thing that
turns to track a target is the entire assembly. Sprites are UNSHADED - the art
has its light painted in, and lighting it a second time with the board's sun
turned the mid-tones muddy - and cut out with ALPHA_SCISSOR rather than blended,
because blended sprites must be depth-sorted and pay for every overlapping
pixel.

The direction a drone faces needed the segment's unit vector, which
`sample_for_render` already had in hand and was throwing away. It is published
as `out_dx`/`out_dy` rather than recomputed, and the ANGLE is taken in the
renderer: `atan2` is not bit-reproducible across libm and has no business in the
simulation.

**It is cheaper than what it replaced**, which was the hope rather than the
expectation. Same tool, same scene, `tools/render_stress.gd`:

| | draw calls | ms/frame at 0 entities | at 400 entities |
|---|---|---|---|
| welded meshes | 16 → 22 | 130.14 | 141.15 |
| authored sprites | 12 → 16 | **84.05** | **88.98** |

Two triangles per entity instead of a welded assembly, and four fewer layers.
Browser frame times after the swap, with zero long frames at every tier: FAST
333ms, BALANCED 633ms, HIGH 1133ms (software rasteriser - ordering and ratios
only).

**One bug found, and it was mine from the previous phase.** The jitter probe
reported FAST *slower* than HIGH, which is impossible. The auto-quality governor
was stepping the tier down during the probe's warm-up and the probe was
labelling whatever it landed on. `tools/frame_jitter.py` now presses F2 before
warm-up - taking the tier away from the governor while it is still the saved
default - so the labels are deterministic again. Worth recording as a shape: a
feature that adapts at runtime silently invalidates every measurement tool that
assumed it did not.

## P0-80 · The sprites were pancakes, and it was the renderer's fault

Reported as "because these are 2D while the map is 3D, it appears like
pancakes", which was exactly right and worth taking apart, because the instinct
was to fix it in the art and the art was not the problem.

A sprite lying flat on the ground, seen by a camera pitched at -40 degrees, sits
50 degrees off square-on: a circle renders 64% as tall as it is wide, and with
no thickness at all the result reads as a decal painted on the floor rather than
an object standing on it.

The fix is to lean each sprite back toward the camera. At 28 degrees against a
40 degree camera the sprite is 22 degrees off square-on instead of 50, so it
keeps its proportions and gains the read of something upright. It costs nothing
- same two triangles, a different basis - and it needs no new art.

Two details that are not obvious:

- **The lean is applied OUTSIDE the facing rotation**, `_lean * Basis(UP,
  facing)`. The other order swings the lean around with the barrel, so a turret
  appears to wobble as it tracks.
- **The axis is the camera's own right-hand vector**, derived from its yaw. It
  is fixed, because this camera never rotates, so it is computed once at setup
  rather than per sprite per frame.

The first attempt leaned them the wrong way - `-tilt` instead of `+tilt` - which
took them from 50 degrees off square-on to 78 and rendered the board as a field
of edge-on slivers. Captured, and that screenshot is the entire reason it cost
one attempt instead of an afternoon of arguing about basis conventions. Worth
recording as a habit rather than a bug: for anything geometric, render it and
look before reasoning about whether it is right.

Leaning pivots about the sprite's centre, so each one is also raised by
`0.5 * sin(lean)` of its own span to keep its lower edge out of the ground.

The remaining half of the problem IS art, and it is a re-generation rather than
a fix: the assets are drawn at a true 90-degree overhead, which shows no sides.
Art drawn from roughly 60-65 degrees of elevation shows some of each unit's
flank and reads with volume even before the lean. That is a prompt change for
the next sheet, not something the renderer can recover.

## P0-81 · The sprites looked pixelated, and two of the three causes were silent

Three things were degrading the authored art, and only the third was visible as
a decision anyone had made.

**Mipmaps were never generated.** `_sprite_material()` asks for
`TEXTURE_FILTER_LINEAR_WITH_MIPMAPS`, and Godot's default PNG import sets
`mipmaps/generate=false`. Asking for a filter that needs mipmaps against a
texture that has none does not warn - it silently falls back to plain linear, so
every minified sprite aliased and crawled. That is the exact failure mode this
project keeps meeting: a request that reports success while doing nothing.

**Godot was about to VRAM-compress them behind our backs.** The imports carried
`detect_3d/compress_to=1`, which re-imports a 2D-imported texture with block
compression the first time it is used in 3D. Nothing had triggered it yet, so
the art in the repo was lossless and the art on a colleague's next checkout
would not have been - a difference that appears at import time, not in any diff.
Set to 0.

**The 3D pass was upscaled at every tier.** One `render_scale_floor` of 0.6 meant
that on any window larger than 720p the whole 3D pass rendered at 60% and was
bilinearly upscaled - including HIGH. The floor is now per tier
(`render_scale_floors`, 0.9 / 0.75 / 0.6), because the tier is already where the
player says what they will pay, and the resolution they are shown belongs to it.
Below 720p none of them bind, which is why this was invisible in every
screenshot taken here and obvious to someone playing on a real monitor.

Enabling mipmaps made it FASTER, which was not the expectation - minified
sampling gets its texture cache back. Same tool, same scene:

| | ms/frame at 0 entities | at 400 |
|---|---|---|
| sprites, no mipmaps | 84.05 | 88.98 |
| sprites, mipmapped | **72.51** | **81.79** |

Browser frame times at 1920x1080, where the new floors actually bind, zero long
frames at every tier: FAST 567ms, BALANCED 950ms, HIGH 1517ms. Software
rasteriser, so ordering and ratios only.

The honest trade: raising the floors costs frames on a large screen, and it is
the auto governor (P0-76) that keeps that safe - a machine that cannot hold HIGH
at 0.9 is stepped down to a tier whose floor it can. Clarity and frame rate are
the same dial, and the player already has it.

What is left is source resolution: the sheet is 1024x1024 for sixteen assets, so
each is about 250 pixels square. That is ample at normal zoom and soft when
zoomed right in, and no import setting recovers it - it wants a larger sheet.

## P0-82 · The second art drop, and two things the first sheet had hidden

Sixteen entities re-authored at 2048x2048 and 60-65 degrees of elevation, plus a
projectile sheet and an impact burst. Each sprite is now ~500px where the first
sheet gave ~250, and the units read with real volume instead of as flat plates.

**Grid divider lines broke the keying, and only one sprite survived.** The new
sheets draw visible lines between cells. Cropping on the exact cell boundary put
those lines on the new image's border - which is precisely where backdrop_of()
samples - so the backdrop was read as the LINE colour, every real backdrop pixel
then sat further than FILL_TOLERANCE from it, the flood fill spread nowhere, and
fifteen of sixteen sprites came out with an opaque rectangle of sky around them.
`CELL_INSET` trims 3% off each side before anything looks at the cell. Caught by
compositing the sliced output over a dark checker, which is now the standard
check: sprites keyed against their own backdrop look perfect in isolation and
obviously wrong the moment they are over something dark.

**The art's own orientation is per asset, and it is not "up".** This sheet draws
turret barrels up-and-RIGHT at 35-43 degrees and the rig's crane up-LEFT.
Measured rather than guessed - the opaque pixel furthest from each sprite's
centre of mass, against straight up - and stored in theme.json as
`sprite_forward_degrees`. Without it every turret would have fired about 40
degrees wide of its target while still hitting, because aiming is simulation and
the barrel is decoration; the bug would have looked like bad art rather than bad
data.

**The drones are front-facing characters, not top-down machines.** A standing
mech rotated to walk east lies on its side. `sprite_rotate_drones` is false for
this sheet: drones keep a fixed upright facing and only turrets track. Turrets
are drawn from above and their barrels are the whole point, so they still turn.
If a future sheet draws drones from overhead, one flag puts the tracking back.

Cost, same tool and scene as P0-81, which had ~250px sprites:

| | draw calls | ms/frame at 0 | at 400 |
|---|---|---|---|
| 250px sprites | 12 → 16 | 72.51 | 81.79 |
| 500px sprites | 12 → 16 | 76.75 | 87.61 |

Deliberately NOT optimised away. 33.6 MB of texture before mipmaps is nothing to
a real GPU, and this box has no GPU - SwiftShader has no texture units and
over-charges for sampling far beyond what hardware does. Optimising a 6% figure
measured on a software rasteriser is the exact mistake this project has been
careful about all the way through. Recorded so the trade is visible if a real
device ever disagrees.

Worth knowing for sizing: a turret spans 74 world units, which is 154 screen
pixels at the closest zoom the camera allows - so 500px is oversampled about
3x. The Titan is the exception at ~700 screen pixels, and it is the reason the
sheet is not simply downscaled.

## P0-83 · Tracer art per family, hits that spark, and shots that leave the barrel

"The turrets have projectiles coming out of the back" - reported from play, and
the cause was one line: the tracer quad's long axis is scaled about its CENTRE,
and the simulation spawns a projectile at the turret's centre. On the frame a
shot fires, half the quad therefore pokes out of the back of the mount. The old
24-unit tracer buried the artefact inside the old small turret art; the new
500px sprites made it plain.

Fixed in the renderer only: the drawn centre is advanced along the velocity
(`projectile_muzzle_advance`) so the art's tail clears the turret's picture and
the shot reads as leaving the barrel. The simulation's position - and therefore
what a shot actually hits - is untouched, which is the standing rule: the sim
never bends for how something looks.

The authored tracers went in per FAMILY, not per damage type - the Suppressor
and the Railgun are both Energy and look nothing alike in flight. `p_family`
already carried the firing blueprint on every projectile (it was added for
veterancy), so routing each round to its family's art layer cost one lookup.
Facing was measured off the art like the turrets before it, with one twist: the
explosive shell measures BACKWARDS, because its brightest pixels are its
exhaust flame and the bright-centroid method finds the tail. Its stored offset
is the measurement plus 180 - worth writing down, because re-measuring it will
"correct" it the wrong way.

Hits wear the authored spark burst on a second effect layer sharing the one
pool: slots carry a kind, the draw sweep deals each live slot to its layer, one
extra draw call. The burst also earns its keep as a mask - the muzzle advance
means a tracer's tip can overshoot the target by a frame at impact, and the
burst is what the eye sees instead.

The suite caught a stale accessor rather than a bug: `drawn_effect_count()`
returned only the glow layer, so "a shot landing leaves an impact" failed with
the impact drawn plainly on screen. The TEST was right. The accessor now sums
both layers. Same lesson as the frame counter and the test filter before it: a
counter that no longer counts the whole population is a lie with a green light
on it.

Audit: 403 tests, 62,309 assertions, 0 failed. Draw calls flat at 16 with 400
entities and tracers live. Browser frame distribution, zero long frames at
every tier: FAST 300ms / BALANCED 550ms / HIGH 1042ms (SwiftShader - ordering
and ratios only).

## P0-84 · Pinned bases, rotating heads: the real fix for firing out of the back

The muzzle-advance fix (P0-83) treated a symptom. The disease was that the WHOLE
turret sprite rotated to track its target: three-quarter-view art turned
in-plane draws upside down the moment a turret aims down-screen - base in the
air, gun underneath - so shots read as leaving the back of the mount no matter
where the tracer started. The player's suggested structure was the correct and
classic one: pin the base, spin only the gun.

The art is one image per turret, but the composition is consistent - gun
assembly above, round drum below - so the slicer now cuts each turret at a
per-family split line into `<id>_base.png` and `<id>_head.png`. The one idea
that makes the renderer side trivial: both halves are RE-CENTRED so the head's
pivot (the centre of the drum's top face) sits at the canvas centre. A quad
rotates about its centre, so baking the pivot into the image turns "rotate the
head about its mount" into a plain basis rotation - no per-frame offset
arithmetic, no pivot maths at draw time. The halves land on a larger canvas as a
result, and the span ratio is read off the textures rather than stored anywhere
it could go stale.

The base layer never turns; the head layer carries the measured barrel offset
and the per-frame tracking. Split mode is all-or-nothing across families and
falls back to whole-sprite rotation if any half is missing, which keeps old
checkouts and partial art drops working.

A head rotated in-plane is still three-quarter art - a gun aiming down-screen is
seen slightly from below - but a compact gun assembly reads fine that way, which
is exactly why every classic tower defense draws its turret heads as separable
pieces. What does NOT survive in-plane rotation is a whole turret with a
grounded base, and now nothing asks it to.

Audit: 403 tests, 62,309 assertions, 0 failed. Draw calls flat at 17 across 0 to
400 entities - the five base layers cost exactly one call. Browser frame
distribution, zero long frames at every tier: FAST 183ms / BALANCED 383ms /
HIGH 667ms - every tier measurably faster than the previous run, though on
SwiftShader only the ordering and the zero-long-frame result are trustworthy.

## P0-85 · Firing out of the back, third time: the bug was never in the art

The player reported it twice and I fixed the wrong thing twice. P0-83 pushed the
tracer forward along its velocity so it stopped poking out of the mount. P0-84
pinned the base and rotated only the gun, because a whole rotated turret drew
upside down when it aimed down-screen. Both were real bugs. Neither was THE bug,
and after each one the guns still pointed somewhere other than at the target.

The third look started from the plane's own vertex data instead of from the art:

    PlaneMesh(FACE_Y): the texture's top edge (v=0) is at local -Z.
    Basis(Vector3.UP, a) sends -Z to (-sin a, 0, -cos a).
    facing is atan2(aim_x, aim_y); to_world maps sim (x, y) to world (X, Z).

So rotating by `facing` alone points the art's top edge at `(-aim_x, 0, -aim_y)`:
exactly backwards, for every sprite, since the day sprites were introduced. The
renderer needed half a turn it never had.

**Why it survived two fixes and three releases.** The correction lived entirely
in data, as `sprite_forward_degrees`. Each sheet's offsets were measured by
eye against art that was already drawing backwards, so each family absorbed a
different share of the same 180 degrees - and no two were wrong by the same
amount. That is why it never looked like one bug. The Railgun's offset happened
to land on a value that is correct mod 360, so the Railgun looked right; the
Cannon was about 100 degrees off; the Rig was off by something else again. A
board of turrets each wrong by a different amount reads as sloppy art, not as a
single missing constant, and it is not a thing you can spot by staring at a
screenshot - which is exactly what I did, twice.

**The fix is a split of responsibility, not a new number.** `theme.json` now
holds `sprite_barrel_degrees`: where the business end points IN ITS OWN PICTURE,
clockwise from the top of the frame. Nothing about Godot is folded in - every
number is checkable by opening the PNG. The engine's half turn is
`SPRITE_HALF_TURN` in the renderer, stated once, next to the derivation above.
Data that mixes a measurement with an engine convention cannot be verified
against either.

`tests/cases/test_sprite_facing.gd` asserts the whole chain from the plane's
vertex data outward, for every id and every direction on the compass, and
includes a test that half a turn from correct FAILS - the check that the check
works. Confirmed by reverting `SPRITE_HALF_TURN` to zero: 4 of 7 tests fail with
40 assertions naming the exact angles.

**One thing the half turn broke on the way in.** Drones do not rotate on this
sheet (`sprite_rotate_drones` is false, P0-82), but the offset was still being
added to their zero heading - which had been harmless while the offset was zero
and became half a turn the moment it was not. Every drone on the board would have
drawn upside down. A correction only means "turn the art's forward to where it is
going"; on a sprite nobody turns, there is nothing to correct.

**A note for the next person who tries to test this by reading transforms.**
Under `--headless` the dummy rendering server does not keep the instance buffer:
`set_instance_transform` is accepted and `get_instance_transform` returns the
identity, always, for every layer. A test written that way passes whatever the
renderer does. Both facing tests therefore ask the renderer what rotation it
WILL apply (`sprite_facing`, `drone_facing` - the single place each convention is
expressed) rather than reading back what it did.

## P0-86 · The turret sheet, detached - and the import settings that kept reverting

Cutting a pinned base out of a one-piece turret picture (P0-84) was always going
to be approximate: any horizontal split line runs through the gun's own shadow
and its mounting yoke, so the base kept a slice of barrel and the head lost the
collar it should pivot on. The third art drop is the parts separately - bare
drums and detached guns - and the slicer mounts them.

That sheet is not a grid: nine bare bases in a 3x3 block, one gun in the corner
of it, and four more down a ragged right-hand column whose cells are four
different heights. So `TURRET_PAIR_CELLS` is pixel rectangles measured off the
delivered image, scaled if it is ever re-exported at another size. Inventing a
grid that is not there is how P0-82 shipped fifteen sprites in opaque rectangles.

Three numbers per family, all read off a 10% grid drawn over the trimmed art
rather than guessed: `hub` (the centre of the socket in the drum's top face, well
above the middle, because the drum is painted from sixty degrees up and most of
the picture is the near wall), `pivot` (the centre of the gun's turntable collar)
and `span` (the gun's width as a share of the drum's - the one taste number).

**One canvas for all five families, not one each.** The canvas is what the
renderer draws at a single span, so a family with a roomier canvas would quietly
draw a smaller drum than its neighbour: five turrets at five sizes, from a
constant that reads as if it set one. The Ballistic's gatling reaches furthest
past its collar and therefore sizes the canvas for everybody - 512px holding
1.76 base widths, so the drum fills 57% of it. `turret_sprite_span` went 74 ->
112 to leave the drum the same 64 world units it was before. With base and head
sharing one canvas and one anchor, the per-family span ratio the renderer used to
compute from texture widths is gone: it is 1.0 by construction now.

**The import settings that kept coming back.** `mipmaps/generate=false` and
`detect_3d/compress_to=1` are both wrong for art that lives minified on a flat
quad, and both fail silently - the first makes the renderer's LINEAR_WITH_MIPMAPS
degrade to plain linear with no warning, the second lets Godot rewrite its own
sidecar to VRAM compression the first time the texture is used in 3D. P0-81 fixed
them. They were wrong again.

The reason is dull and was the whole problem: `.import` was in `.gitignore`. Those
sidecars are source, not build output, and ignoring them meant every fresh clone
re-imported with the defaults. P0-81's fix reached the sprites the entity sheet
produced; the five tracers and the ten split halves were generated afterwards,
got fresh default sidecars, and shipped that way. Fixed three ways so it stays
fixed: `[importer_defaults]` in project.godot so a first import is already
correct (verified by deleting a sidecar and re-importing), the sidecars committed,
and `tests/cases/test_art_import.gd` failing if either setting drifts on any
drawn sprite.

Audit: 413 tests, 62,562 assertions, 0 failed. Draw calls flat at 17 across 0 to
400 entities (18 before; one fewer layer). Cost measured as a matched A/B in one
session, two runs of `render_stress.gd` each, because a single run right after a
browser test read 25% high and would have been reported as a regression:

| entities | before | after |
|---|---|---|
| 0 | 40.29, 41.36 | 43.09, 46.19 |
| 100 | 44.73, 43.34 | 42.91, 42.38 |
| 400 | 51.41, 57.05 | 45.22, 47.25 |

Faster where it matters - a busy board - and about 3ms slower on an empty one,
which is the mipmap chains becoming resident. Browser frame distribution, zero
long frames at every tier: FAST 200ms / BALANCED 417ms / HIGH 717ms. Those
medians are NOT comparable with the previous release's; that was a different
container on a different day, and only a same-session A/B says anything about a
change. The zero-long-frames result and the tier ordering are what transfer.

## P0-87 · Audit: what the shipped build was actually carrying

A full audit, and the largest finding was not in the code.

**Every player was downloading the authoring sheets.** `assets/art/_source_*.png`
are the four sheets the sprites were cut from. They live next to their output so
a sheet can be re-sliced without hunting for it; nothing in the game ever loads
them. But `export_presets.cfg` only excluded `tests/*, tools/*`, so Godot
imported all four and packed them. **index.pck 20.1 MB -> 9.3 MB**, more than
half the download, for art nobody sees.

That is the kind of saving that is worthless if it quietly drops a real sprite,
and a missing sprite does not error - the renderer falls back to the generated
mesh. Checking the pck's own file table would have been the obvious proof and
the parser read zero files against pack format 3, which is exactly the sort of
"tool reports nothing, nothing looks wrong" answer this project keeps getting
caught by. So it was proved from the outside instead: drive the real browser
build, place turrets, zoom in, photograph one. The painted Ballistic renders.

**Documented counts had drifted.** README and AGENTS.md both said 48 levels /
12 boards / four weapon families / eight drone classes. HEAD is 49 levels across
13 boards (outpost mode added one), five turret families, and eleven drone
classes - the README's drone table did not list the Titan or the Harbinger at
all. All corrected against the data files rather than against memory.

**Dead config, and a document pointing straight at it.** theme.json still set
`render_scale_floor`, singular, long after the code moved to a per-tier
`render_scale_floors` array - and AGENTS.md named that dead key as the first
lever to reach for when the game is slow. Seven more orphans went with it
(`pad_radius`, `platform_metallic`, `platform_roughness`, four `ground_detail_*`
left behind when material_library stopped reading its `world` argument).

A knob that does nothing is worse than no knob, so it is now asserted:
test_art_import.gd fails on any `world` key that no reader file mentions. It
understands the one dynamic case - `_lit()` builds `<key>_hdr` at runtime for
the Forward+ overrides - and it was verified by planting a dead key and watching
it fail. The reverse also turned up: `band_premium` and `platform_rig` were read
from the theme but never present in it, so the renderer's inline defaults were
the real values. Added at those exact values, so nothing changes on screen.

**Not fixed, on purpose.** `proj_impact.png` is 1857x1857 and is drawn at 15 to
60 world units - about thirty times oversampled, and still the largest texture
in the game. It no longer costs download size now the pck has shrunk, and
resizing it changes shipped art, so it is a decision to make rather than a thing
to quietly do. Likewise the 38 inline `_world.get(key, default)` defaults that
disagree with theme.json: harmless while the file wins, a silent revert to a
stale look if a key is ever deleted, and a pattern spanning the whole renderer
rather than a bug to unpick mid-audit.

**And the thing only the deep run could see: the siege could not be lost.**
`run_tests.sh --full` plays all thirteen chains instead of the sampled two, and
it failed on `XLIX - Ares Station: Siege must still be losable from a clean
start`. Measured: an idle run - not one turret placed - finished the siege with
**1196 of 2600 hull left**. The level was free.

The cause is a half-finished edit. The siege was authored at 40 waves, the
balance probe could not win it, and it was cut to 14 (P0-84's note records
exactly that). The hull was not rescaled with it. The stale note in the wave
file even quoted the tell without anyone reading it as one: a competent run
finishing with "1369 of 2600 hull left" is almost the same number as the idle
run's 1196 - 37 turrets were buying 173 hull.

Re-measured properly across five seeds before touching anything:

| run | drain | turrets |
|---|---|---|
| idle | 1404, every seed | 0 |
| competent | 926 - 991 | 37 of 48 |

Idle drain is seed-independent, which makes sense: everything reaches the core,
so the total is just what the waves are worth. That puts the safe window at
992-1404, and `starting_integrity` is now **1200** - mid-window, about 17%
margin either side. Verified: idle LOSES on all five seeds, competent WINS with
209-274 hull left.

The narrow spread is left as a design smell rather than papered over. Thirty-seven
turrets buying back only 29% of the leak means this level reads as a
hull-endurance check more than a defence-building one; giving turrets more
leverage on a per-lane map is the open question, and it is recorded in the wave
file next to the number rather than in someone's head.

Two guards added to `test_outpost.gd`, at sampled-suite cost, so this is caught
on every run rather than in a pre-release one: the siege must fall to an idle
run, and must hold for a competent one. Verified by putting 2600 back and
watching the first one fail.

Other probes, all clean, and two rows that look alarming and are not - both now
documented where they are printed, so the next reader does not chase them:

- `board_probe` prints `idle -> SURVIVED (trivial)` for highway_act4. That idle
  run starts from the INHERITED board, which test_engagement deliberately
  stopped asserting when turrets began carrying their tiers.
- `link_probe` shows reactor_act3 with 104 turrets and zero links. A lender
  needs `tier_scaling * t_tier > 0` and t_tier is zero-based, so links begin at
  tier 2; the probe plays mid-chain acts from a CLEAN start, where the greedy
  policy builds wide rather than tall. Nothing meets that act that way in play.

`balance_probe` over all 49 levels: no losses. `frame_profile` at the worst
board (level 46, wave 10, 208 turrets): `sim.step` 0.485 ms and
`update_visuals` 0.473 ms, both far inside budget; `refresh_board` is 4.1 ms on
a placement and 8.1 ms on a ground purchase, split 3.95 ms `_refresh_cells` /
3.46 ms `_refresh_link_lines` - the cells half is now the larger one, and
AGENTS.md had been blaming the link lines alone at ~5 ms.

Audit: 415 tests, 62,565 assertions, 0 failed on the sampled suite; the deep
run that found the siege - `run_tests.sh --full`, all thirteen chains - re-run
after the fix at 417 tests, 62,716 assertions, 0 failed in 17 minutes.

Worth saying plainly, because it happened twice in one session: both of the
findings above came from checks that do not run by default. The siege needed
`--full`, which is a pre-release run; the import settings needed somebody to
notice that `.import` was in `.gitignore`. The sampled suite was green
throughout, and would have stayed green through a release. If `--full` is not
made a release gate, it should at least be run whenever a level's wave file
changes - that single edit is what left the siege free.

## P0-87 · The station, the flashes, and an icon — plus a corner the generator signs

Three assets that were still generated in code, and one keying bug that only
showed up because two of them were single-subject images.

**The signature.** Every sheet the generator produces carries a small
four-pointed white sparkle in a corner. It is not art, and keying leaves it fully
opaque. On a grid sheet that is invisible - it lands in a gutter. On a
single-subject image it is not cosmetic at all: `trim_square` takes the bounding
box of everything opaque, so a speck in the corner drags the box out and the
subject ends up small and off-centre in its own canvas.

The first attempt removed opaque islands below a share of the largest one, and
got it wrong on **both** ends: it deleted the Kinetic flash's detached spark dots
(real art, ~300px each) and kept the sparkle (~3,400px, comfortably over any
threshold that spared the sparks). Size was never the signal. Measured, the stamp
lands in the same place every time - bbox (1760,1760)-(1855,1855) on a 2048
sheet, 9.4% in from the right and bottom - so it is removed by POSITION, with a
corner square scrubbed to the local backdrop before anything else looks at the
sheet. A square and not a margin strip, because the station's artwork reaches
11.2% in from the right edge but only high up; requiring both axes is what keeps
it. Islands are still dropped at 16px and below, which is dust and cannot be art.

The second attempt then painted a solid orange square over the Explosive flash
and a white one over the Arc, because it sampled the fill colour by walking
INWARD from each corner - straight into the artwork. Sampling the extreme corner
is right: the margin every sheet is authored with guarantees it is backdrop.

**The icon is not keyed at all.** Its greys and blues sit within a few values of
the backdrop, so the flood fill chews holes in the shield and the dust pass
shakes the fragments off; it came out speckled along one edge. An app icon does
not want transparency anyway - a solid field reads better in a browser tab than a
floating emblem. So `--icon` finds the subject by keying, throws the key away,
and keeps only its bounding box to centre an opaque crop. The project has had
`html/export_icon=false` with an apology attached since the web export existed,
because Godot fails the whole export if it is true and no icon is set. Both are
fixed.

**The muzzle flashes are per family and they turn.** Hits already wore painted
debris while shots wore a generated soft glow, and side by side the asymmetry
read as the gun firing a light bulb. Unlike the impact burst these are not
billboards: a flash is drawn pointing up its own picture and has to be turned to
match the gun, so it lies on the ground plane, leans with everything else, and
goes through the same `sprite_facing` correction the barrel does. Their
`sprite_barrel_degrees` are all 0.0 - after P0-85, "drawn pointing up" is the
cheap case. `drawn_effect_count()` now sums all three kinds; it has already been
wrong once this way, reporting zero while the screen was full.

**The station is the largest sprite on the board by an order of magnitude** -
about nine turret drums across - and it exposed something the small sprites never
did. A leaning quad that big has real depth spread: its lower edge sat at ground
level, so the road quads at corridor height won the depth test and eight roads
drew straight over the building they converge on. Lifting it 14 units - 1.7% of
its own span, invisible - puts its base above the roads and they pass under it.
It does not cast a shadow: a leaning cutout casts a shadow shaped like a leaning
cutout. `_station_mesh()` is kept and is not dead code; it is what a checkout
with no `assets/` draws.
