# LAST LINE

Roguelite tower defense. Godot 4.x / GDScript. **3D, and it runs in a browser.**

**Status: P0 complete and audited three times, then extended well past it.**
Forty-eight levels — twelve boards played as four acts each, where every act
runs the same road and keeps what you built on the last one, and the escalation
is in what walks it — four weapon families with four tiers each, five targeting
orders apiece and support links between them, eight drone classes, three damage
types against three armour classes, named per-act affixes, a module drafted
between acts, free placement on purchasable ground, saved progress, synthesised
sound, and an end-of-act debrief, all on a deterministic 30Hz simulation drawn
with real lighting, shadows and depth cueing. The last board has two roads. The design document is the master plan
(v2); the phase ladder is §5.7. Formally this is P0 plus much of P1/P2's content;
the run layer (P3) is the next real milestone.

This tree is self-contained and shares nothing with MRF Explorer, the Python
product that occupies the rest of this repository.

> **Picking this up cold, or handing it to another agent?** Read
> **`AGENTS.md`** first. It is the operational handoff: the environment traps
> that will otherwise cost you an hour, the invariants that are mechanically
> enforced, the working method this codebase expects, and what is currently
> open. This file explains what the game *is*; `AGENTS.md` explains how to work
> on it, and `DECISIONS.md` explains why it is the way it is.

## Run it

Needs Godot 4.x (built and verified against 4.5).

```bash
godot --path game                            # play
GODOT=/path/to/godot ./game/run_tests.sh     # the suite, headless
GODOT=/path/to/godot ./game/run_tests.sh --full   # ...playing every level end to end
GODOT=/path/to/godot ./game/run_tests.sh -- --case test_engagement   # one file
GODOT=/path/to/godot ./game/build_web.sh     # HTML5 build -> build/web/
python3 game/tools/verify_web.py             # boot the web build in a real browser

# Desktop build. Needs the matching export templates installed.
godot --headless --path game --export-release "Linux" ../build/linux/lastline.x86_64
godot --headless --path game --export-release "Windows" ../build/windows/lastline.exe
```

**The desktop build looks better than the web build, and it is not close.**
Desktop runs the Forward+ renderer, which has SSAO, high-quality shadow
filtering and a real HDR bloom; the web build runs Compatibility, which has
none of them, because Forward+ does not target WebGL. That is a browser limit
rather than a Godot one — the same wall stops Unity's WebGL export, and Unreal
has had no web target at all since 4.27. `project.godot` sets
`rendering_method=forward_plus` with a `.web` override back to
`gl_compatibility`, so one project serves both and nothing in `render/`
hard-codes which one it got.

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

| Drone | Armour | Health | Speed | Leak cost | Pays | |
|---|---|---|---|---|---|---|
| Skitter Drone | Light | 22 | 140 | 1 | $7 | fast, fragile, numerous |
| Sentry Walker | Light | 55 | 90 | 4 | $16 | the baseline |
| Bulwark Hauler | Plated | 260 | 55 | 12 | $58 | slow and very tough |
| **Vanguard Lance** | Light | **620** | **168** | 11 | $150 | fastest *and* tough |
| **Siege Breaker** | Plated | **1400** | 78 | 16 | $300 | shrugs off 80% of slows |
| **Brood Carrier** | Plated | 150 | 95 | 5 | $40 | armoured; breaks into 3 Skitters |
| **Field Mender** | Shielded | 90 | 104 | 3 | $30 | heals everything around it |
| **Static Jammer** | Shielded | 340 | 122 | 7 | $110 | silences turrets it passes |
| **Breach Borer** | Plated | **900** | 62 | 0 | $210 | tunnels; opens a new road |

The first three trade speed against health. The **Lance** does not — it is the
fastest thing on the board *and* tougher than anything that is not slower than
it, so a turret gets less time on a target that needs more damage. A thin line of
fire that held everything else lets Lances through.

The **Breaker** exists because the Arc Suppressor does: once slowing everything
was possible, slowing everything was the answer to everything.

The **Carrier** is the only drone that is not a point on the health ladder. It is
Plated *and* wears flat armour, so cheap fast kinetic guns lose twice over to it — and it breaks into
three Skitters where it dies, which are faster than it was and want exactly the
cheap fast guns that could not hurt the carrier. The weapon that kills it well is
the wrong weapon for what it leaves. Because it splits on death and not on exit,
letting one through costs 5 and no Skitters: the one drone in the game where a
leak can be the right call.

The **Mender** and the **Jammer** are the two support classes, and neither is a
point on the ladder. The Mender puts health back into everything around it and
never into itself — it is small, quick and cheap, so *First* and *Toughest* walk
straight past it. It is the drone the targeting orders exist for. The Jammer is
the only thing in the game that attacks *your board*: it silences turrets it
passes, and a jammed turret's cooldown does not advance either, so the silence
costs exactly the uptime it looks like it costs.

All five arrive partway into the campaign (levels 13, 18, 25, 26 and 33), never
open a wave, and stay a small share of any level's head-count — they are elites,
not populations.

**The Breach Borer.** The only drone that costs you the **map** instead of
Integrity. It walks two thirds of its road, goes under, and opens a dormant
**breach road** that carries traffic for the rest of the act. It never reaches
the exit, so it takes no Integrity — what it takes is a whole new stretch of
road to hold.

The breach road is drawn and buildable from the first tick of every act on that
board, so nothing ever appears under a line you already built, and you can spend
slots covering a road that may never open. That is the decision: pay now for a
road that might stay shut, or back yourself to kill the Borer before it tunnels.
It is 900 health, Plated and armoured, so the answer is a prepared explosive line
rather than a reaction — the first drone where killing something *fast enough*
matters rather than just killing it. Armed on act IV of the last six boards; the
HUD says **BREACH ARMED** when one is coming.

**Champions.** Every 20th drone of a large wave group arrives as a champion:
about 4.5× the health, 5× the bounty, drawn half again as large and white-hot.
They put spikes inside a stream — a steady stream is answered by a steady line,
a spike is answered by a targeting order (*Toughest* finds them) or by an
overcharge. Deterministic, like everything else: the Nth spawn is the Nth spawn
in every run.

- **Click owned ground** (green) to build the selected weapon.
- **Click a turret** to upgrade it a tier. Hovering shows its DPS and next cost.
- **Click dim blue ground** to buy that cell, expanding where you can build.
  Ground can only be bought next to ground you already hold, and each purchase
  costs more than the last.
- **Right-click a turret** to sell it back for 65% of everything spent on it.
- **`T`** re-tasks the turret under the cursor: First (the default — whatever is
  furthest along the road), Last, Nearest, Toughest, Weakest. None is strictly
  better. *Last* holds a leaker back for the guns behind it and wastes a front
  line's uptime; *Nearest* keeps a Suppressor's slow on what is closest to it
  rather than to the exit; *Toughest* puts a Railgun on the Breaker and lets forty
  Skitters past; *Weakest* is how a Cannon line clears chaff so the heavy guns are
  never distracted. Orders, tiers, doctrines and veteran ranks all carry between acts.
- **`O`** **overcharges** the turret under the cursor for $120: a few seconds of
  much faster, harder fire, then a long per-turret cooldown. The only active
  ability in the game — everything else is placement. It exists to answer a
  **champion** in the wave rather than before it, and it is refused while the
  turret is jammed: the counter to a Jammer is killing the Jammer.
- **`E`** calls the next wave in early for a bounty — the gap between waves is
  when Capital accumulates, so it trades preparation for money.
- **Scroll to zoom** on whatever the cursor is over, **middle-drag to pan**,
  **`Z`** to reset the view. Boards run to 16,000 units, so framing one end to
  end makes a turret a few pixels wide.
- **`F2`** cycles graphics quality: **High** / **Balanced** / **Fast**.
  Each rung gives up one thing at a time. High has everything. Balanced drops
  the shadow pass and the surface normal maps, keeping the albedo and roughness
  variation that stops surfaces reading as plastic. Fast drops the scenery and
  the generated surfaces entirely. All three cut the 3D render resolution to a
  pixel budget. Measured in a browser on one board: **667 ms / 383 ms / 183 ms**
  per frame — Fast is about 3.6x the frame rate of High. The setting is
  remembered, and it never touches the simulation — two players on different
  settings are playing the identical game.

  Those numbers come from a machine with no GPU, so only their ordering and
  their ratios mean anything. **If the game feels slow, press `F2` until it says
  FAST.**

  You should not normally have to. If the frame rate stays below 24 fps for
  five seconds the game steps the tier down by itself and says so, and it
  remembers. It only ever steps *down*, and pressing `F2` once turns the
  automatic behaviour off for the session — an explicit choice wins, including
  the choice to run HIGH on a machine that struggles with it.
- **`F3`** shows the debug readout, including the **worst frame** in the last
  second, how many frames took over twice the typical one, the current quality
  tier and 3D scale, and the **GPU** actually being used. Those are the numbers
  worth reporting if it runs badly — an average frame time cannot tell slowness
  from stutter, and if the `gpu` line says `llvmpipe`, `SwiftShader` or
  `Software`, the browser is not using the graphics card at all and no setting
  in the game will fix that. (In Chrome: check `chrome://gpu`, and make sure
  "Use graphics acceleration when available" is on in Settings → System.)
- **`Q`** cycles weapon · **`1`–`4`** speed · **`space`** pause · **`[`**/**`]`**
  move between unlocked boards · **`M`** mute · **`F3`** debug · **`R`** restart ·
  **`N`** next level after a win. On a won act, **`1`**–**`3`** fit a module
  instead of setting speed.

When an act ends, the **debrief** under the banner says which of your weapon
families actually did the work — damage landed and drones killed, per family, with
overkill excluded. It is the one number the next act's build decisions turn on.

Progress is saved per board, so the campaign resumes where you left it.

Four families answering different problems:

| | Ballistic | Cannon | Arc Suppressor | Railgun |
|---|---|---|---|---|
| Fires | Kinetic | Explosive | Energy | Energy |
| Damage | Single target | Area, falls off to the edge | Very low | Enormous, very slow |
| Does | Kills things | Kills crowds | Slows the blast radius | Pierces a whole lane |
| Best against | Skitter swarms | Bulwark Haulers, Breakers | Menders, Jammers | Menders, Jammers, columns |
| Tier 1 cost | $100 | $140 | $130 | $260 |

### What a gun is good against

Every gun fires one **damage type** and every drone wears one **armour class**.
Where they meet is worth 1.35x, 1.0x or 0.65x — a favourable matchup is roughly
two-for-one against an unfavourable one.

| | Light | Plated | Shielded |
|---|---|---|---|
| **Kinetic** (Ballistic) | **1.35** | 0.65 | 1.0 |
| **Explosive** (Cannon) | 1.0 | **1.35** | 0.65 |
| **Energy** (Arc Suppressor, Railgun) | 0.65 | 1.0 | **1.35** |

Three types for four guns, on purpose. The Arc Suppressor and the Railgun share
Energy and are nothing like each other — a four-damage slowing field and a
ninety-three-damage lance through a whole lane. Ballistic and Cannon are pulled
apart by the *other* axis instead: flat armour comes off each **hit**, so a wall
of cheap fast rounds is punished by it and one big slow round is barely troubled.

The floor is deliberately not zero. A bad matchup is a bad answer, not no answer
— no wave should teach you that a family you have already paid for does nothing.

Both support classes are Shielded, which is what makes "put a Railgun on the
Menders and set it to *Weakest*" a plan rather than a hope. The wave preview above
the board groups the next wave **by armour class**, so the decision is readable
before the money is spent.

### Act affixes

An act can carry named modifiers, announced beside the level name before the
first wave walks:

| | |
|---|---|
| **Hardened** | +3 armour off every hit |
| **Swift** | everything moves 22% faster |
| **Resilient** | +25% health across the board |
| **Relentless** | the gap between waves is cut to 55% |
| **Screened** | slowing fields bite far less |
| **Massed** | 30% more drones, and each pays 15% less |
| **Austere** | bounties are cut to 75% |

Authored per act, never rolled. A modifier you cannot see coming is a surprise,
not a decision — this game is deterministic on purpose, so an act can be learned,
lost to, and beaten. Each board has one **signature** affix that stays the same
across its acts, so a player learns "Refinery is the Screened board" rather than
re-reading a list every act. They arrive gradually: acts I–III of the first three
boards carry none at all, and only the last six boards' act IVs carry two.

The Suppressor barely damages anything. It is a force multiplier: a slowed drone
spends longer inside everyone else's range, so a Suppressor makes the turrets
around it worth more. Slows refresh rather than stack, so massing them does not
pin a wave in place.

The Railgun is the opposite trade: one very slow, very long-ranged shot that
tears down a whole lane at full damage. Worth several turrets against a column on
a straight, and close to worthless against a scattered swarm.

### The strategy layer

Four systems, one purpose: making "carpet the road and upgrade everything" stop
being the answer.

- **Salvage Rig** (`Q` cycles to it) — occupies a deployment slot, fires
  nothing, pays Capital at the top of every wave (not the first). Every rig is
  a gun you didn't build. A jammed rig pays nothing, so an economy board fears
  Jammers more than a gun line does.
- **Doctrines** — at tier 4, `G`/`H` picks one of two permanent specializations
  per turret: Ballistic *Shredder* (rate) or *AP Core* (rounds ignore flat
  armour); Cannon *Siege Shells* (blast) or *Core Breaker* (payload);
  Suppressor *Stasis Web* (deeper slow) or *Overload Coil* (damage); Railgun
  *Long Lance* (reach) or *Cyclotron* (rate). Chosen once, kept for the board.
- **Premium ground** — gold cells, a handful per map: High Ground (+25% range)
  and Power Taps (+30% fire rate). Who gets the hill is the opening decision,
  and at the fork the good tiles don't split evenly.
- **Veterancy** — a turret's kills earn ranks (40/150/400), each +5% damage.
  Ranks carry between acts and die with a sell: specific guns become worth
  protecting.

### Support links

Every family projects a bonus onto turrets **of other families** nearby —
Autocannon lends reach, Mortar and Railgun lend damage, Suppressor lends rate of
fire. Only other families, which is the point: four Autocannons in a row get
nothing from each other, and a mixed line is worth considerably more than the sum
of its parts. The lines drawn between your turrets are the links; hover one to see
what it is receiving.

Nothing is projected at tier 1. Upgrading a Suppressor makes the four guns around
it better as well as itself, which is the first reason in the game to spend on a
turret that is not your best one.

### Money

Capital left in hand when a wave begins earns 5% back, up to $90. It is not much
— it is enough that "hold two waves and buy the better thing" is a real
alternative to spending everything the moment it arrives.

You are capped at a **deployment limit** per board — 30 turrets on the first,
rising board by board to the pool ceiling of 208 (118–144 on the later
single-road boards; the forked board gets more because there is more road to
stand beside). It is the same limit in every act of a board, so the ceiling is
something you learn once. Once your allowance is placed the only way to grow is
to upgrade, which is what makes *where* you put them matter.

### Boards that hold

Each board is four acts, and all four are the **same board** — same road, same
deployment limit, same ground you own. Every turret and every cell you bought is
still there, still covering exactly what it covered. Act I of a new board always
starts clean.

What escalates between acts is **what walks the road**: more drones, and tougher
ones. Terminus act I sends 4,890 drones at ×1.25 health; act IV sends 10,555 at
×9.57 — and that health figure is against a board that arrives with every tier
you bought still on it. Acts used to escalate partly by revealing more road and raising the
limit, and that was the wrong feeling — the board kept moving out from under a
line that was already built, so holding meant stretching rather than
reinforcing.

**Everything you built carries forward — placements, weapons, tiers, targeting
orders, ground.** Only Capital resets, so each act's spending is its own
decision.

Turrets used to arrive refitted back to tier 1, and that rule died on contact
with a player: "every time I go to a new level it resets the levels of my
weapons." A tier is the most expensive thing you buy, and buying it knowing it
expires at the act boundary is a worse decision than not buying it. The waves
absorbed the price instead — acts II–IV are sized for a board that arrives
intact, which is why an act IV health multiplier reads 10× where an act I reads
1.5×.

Corridor Integrity carries between the acts of a board too, so a sloppy act I
costs something in act IV. It resets when the campaign moves to a new board.

Retrying an act (`R`) restores the same inheritance, not an empty board.

### The module draft

Win an act that continues a chain and three **modules** are offered — press `1`,
`2` or `3` to fit one for the rest of the board. Ten in the pool, each touching a
different system: fire rate, damage, reach, the support-link radius, opening
Capital, the interest ceiling, sell refunds, Integrity, jam resistance, upgrade
cost. The offer is seeded by the level, so the same act always offers the same
three — a draft is something you plan a board around, not a slot machine. They
reset with the board.

### Two roads

The last board forks. Both roads leave the same gate and reach the same exit, and
three drones take the long way for every one that takes the short one — so the
fork is not a second copy of the problem, it is a shorter deadline. Coverage has
to be divided, and the deployment limit means a turret cannot watch both. Both
roads are open from act I: a fork is a fact about the board, not a surprise
sprung on an act. It used to open partway through the chain, and that stopped
working the moment every act ran the whole road — the act inheriting a full
board was already at its deployment limit, so the second road arrived with no
turrets left to answer it. Measured: 68 leaks and Integrity 0 on an act that had
been winnable.

### Ares Station (level XLIX)

The last board is not a corridor. The station sits in the middle, eight lanes
converge on it from authored gates, and building happens in a ring around the
core — start inside the free ring, buy outward. Integrity is a hull, not a leak
counter: early leaks are the cost of conceding an approach, which is the whole
opening decision on a board you cannot fully cover. Waves are generated from
five cycling archetypes with a boss every fifth wave — the Titan first, then
the Harbinger — arriving on top of their waves, not instead of them. Fourteen
waves, probe-verified winnable and leaky.

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

The board sits in a landscape. The ground is a graded terrain rather than a flat
quad — bare soil beside the road where everything has been driven over, grass
beyond it, rock on anything that has climbed — with a clustered treeline, boulders
and scattered debris around it. It rolls in the distance and is dead flat anywhere
a turret can stand: the simulation is 2D and every placement rule is a distance in
the ground plane, so relief under the playable band would put turrets on slopes
the rules know nothing about. All of it is hashed from position rather than
randomised, so a board looks the same every time you open it.

### Surfaces

Every surface family — ground, verge, road, wall, turret, drone, rock, bark,
canopy, prop — carries a generated albedo variation, normal map and roughness
map, built from one noise field per family by `render/material_library.gd` and
directed by `data/materials.json`. Nothing is shipped as an image; the same rule
as the sound effects.

The field that does most of the work is `lattice_x` against `lattice_y`. Equal
counts give isotropic grit, which is soil, stone and concrete. Unequal counts
stretch the noise along one axis, and that single asymmetry is the whole
difference between asphalt dragged along the road, brushed metal on a turret
housing and the vertical grain of bark. Panel grooves do the rest: noise alone
reads as rock however it is tuned, because nothing in nature repeats on a grid,
so manufactured things get seams and natural ones are tested not to have any.

The maps are generated once for the **session**, not per level — a level load
that rebuilt them would freeze for about a quarter of a second on each of the 48
acts. `tools/material_probe.gd` reports what they cost.

Nothing tall stands in front of the board or on ground a turret could use. The
simulation has no idea a tree is there, so a tree must never be able to hide
anything the rules care about — that is a tested property, not a convention.

There is no horizon, and that is not an oversight: at −38° with a 26° field of
view the top of the frame still points 25° below horizontal, so the sky is
geometrically unreachable at this framing. Everything you can see is on the
ground plane.


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
projectiles, buildable cells, the three parts of every turret, and the combat
feedback layer. The corridor is a single generated mesh rather than a box per
segment. Draw calls do not grow with how much is happening.

Muzzle flashes, impact sparks, blast rings and wrecks are worked out by **diffing
the simulation between ticks** — a shot fired is a cooldown that went up, a wreck
is a drone slot that was alive and is not. The simulation never grows a
render-facing event channel, and the effects layer is entirely optional: every
headless test runs with none attached. A leak, and only a leak, shakes the camera.

## How it sounds

Synthesised in code. There are no audio files in this repository — every sample is
generated at startup from filtered noise and swept sine tones, so the web build
stays the size it was and a family's sound is tuned by editing a number in
`theme.json` the same way its colour is.

The interesting part is the throttle. A hundred and forty-four turrets firing three
times a second is four hundred shots a second, which played faithfully is white
noise rather than a firing line; each kind of sound gets one voice every few tens of
milliseconds and the rest are dropped. A leak is never throttled. **`M`** mutes.

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
  theme.json        colours, lighting, camera, quality thresholds
  materials.json    procedural surface families (the art direction)
  blueprints/       weapon families, their tiers, and their support links
  enemies/          drone classes
  modules/          the between-acts draft pool
  maps/ waves/      one file per map, one per engagement
render/
  sim_renderer_3d.gd  3D drawing (MultiMesh + interpolation), camera, effects,
                      the three quality tiers
  material_library.gd generated albedo / normal / roughness maps, per family
  debug_overlay.gd    F3: fps, worst frame, long frames, quality, GPU, pools
assets/art/ the authored entity sprites — five turret families, eleven drone
            classes — sliced from one sheet by tools/slice_sprites.py. The only
            binary assets in the project; surfaces and sound are still generated
audio/      sfx.gd — every sound in the game, synthesised at startup
ui/         HUD, wave preview, end-of-act debrief, transient notices
tools/      headless dev utilities — screenshot capture, render stress, frame
            profile, material probe, link probe, balance_probe for the whole
            campaign, board_probe for one board, and two Python tools that
            drive a real browser (verify_web, frame_jitter)
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

# ...and zoomed in, which is the only framing that shows anything a few
# pixels wide - which is most of the feedback layer.
xvfb-run -a godot --path game --rendering-driver opengl3 \
    --script res://tools/capture_screenshot.gd -- --wave 9 --enemies 6 --zoom 8 --out shot.png

# Where a frame actually goes, stage by stage, on a real board. Times the CPU
# side - the per-tick diff, the per-frame instance fill, the board rebuild -
# separately, so the answer is WHICH stage rather than how much.
xvfb-run -a godot --path game --rendering-driver opengl3 \
    --script res://tools/frame_profile.gd -- --level 46 --wave 10

# Frame-time DISTRIBUTION in a browser: p99, worst, and how many frames took
# over twice the typical one. An average cannot show a stutter - a steady 40ms
# and an alternating 20/60ms have the same one and feel nothing alike.
python3 game/tools/frame_jitter.py

# What the generated surface maps cost, per family, and proof the cache is one.
godot --headless --path game --script res://tools/material_probe.gd

# Verify draw calls stay flat as entity count climbs.
xvfb-run -a godot --path game --rendering-driver opengl3 \
    --script res://tools/render_stress.gd

# Play the whole campaign chained, the way a player would, and print the table.
# Reports rather than asserts - this is the tuning loop, not a gate.
godot --headless --path game --script res://tools/balance_probe.gd
godot --headless --path game --script res://tools/balance_probe.gd -- --from 32 --to 35 --idle

# One board's four acts, idle-testing the last of them - the act that inherits
# three acts of building is the one that can turn out to need no input at all.
godot --headless --path game --script res://tools/board_probe.gd -- --from 44

# What the support links are actually worth in a real game, rather than in a
# unit test - how many turrets ended up linked, and to what effect.
godot --headless --path game --script res://tools/link_probe.gd
```

Scope discipline: ideas beyond the current phase go in `post-launch.md`, not into
the code.
