# LAST LINE — agent handoff

You are picking up a Godot 4.5 / GDScript roguelite tower defense that is
finished enough to play end to end and is published to a live URL. This file is
what you need to be productive in the first hour without breaking something the
tests will not catch. It is vendor-neutral — nothing here assumes any particular
assistant.

**Read this file in full, then `README.md` (what the game is), then skim
`DECISIONS.md` (why everything is the way it is).** `DECISIONS.md` is 83 numbered
entries and it is the single most valuable document here: each one records a
decision, the measurement behind it, and in many cases the wrong answer that was
tried first. Before you "fix" something that looks odd, search that file for it.
A surprising amount of what looks like an oddity is a scar.

`game/` shares nothing with the Python product in the rest of this repository.
Ignore `CLAUDE.md`, `docs/AI_HANDOFF.md`, `mrfx/`, `src/`, `run.py` entirely.

---

## 1. Do this first

```bash
# From the REPOSITORY ROOT, not from game/.
GODOT=/path/to/godot ./game/run_tests.sh
```

Expect **403 tests, 62,309 assertions, 0 failed**, in roughly 4–6 minutes. If
you get that, your environment is good. If you get parse errors about
identifiers not being declared, read §2.1 — you almost certainly ran Godot
directly instead of through the script.

Needs Godot 4.5 (`4.5.stable`). No plugins, no addons, no package manager, no
build step. The whole game is GDScript and JSON in this directory.

---

## 2. Environment traps that will cost you an hour

These are not style preferences. Each one has bitten someone.

### 2.1 `class_name` resolution needs an import pass

Godot resolves `class_name` globals from `.godot/global_script_class_cache.cfg`,
a build artifact that is **not committed**. On a fresh checkout, every file that
references `Sim`, `MaterialLibrary`, `TestCase` etc. fails to parse with
"Identifier not declared".

`run_tests.sh` runs `--import` first, which is why you must go through it.
If you invoke the test runner directly, do the import yourself first:

```bash
godot --headless --path game --import
```

Symptom when you forget: a wall of `Parse Error: Could not find type "X"`, often
followed by a test summary that still says `0 failed` — see §2.4.

### 2.2 Always run from the repository root with `--path game`

```bash
godot --path game …          # correct
cd game && godot --path game # "Invalid project path specified: game, aborting."
```

A shell whose working directory persists into `game/` will break every
subsequent command in a confusing way. Pass `--path game` from the root.

### 2.3 Running one test file

```bash
GODOT=/path/to/godot ./game/run_tests.sh -- --case test_materials
```

The `--` is required: everything after it is forwarded to the runner as user
args. Expect a few hundred milliseconds. If it takes minutes, you are running
the whole suite and the filter was dropped — that was a real bug in
`run_tests.sh` until P0-77, so make sure you have that commit.

### 2.4 A green suite is not proof, unless the assertion count moved

A GDScript file that fails to compile contributes **nothing** — no tests, no
failures. The suite once reported `16 tests, 1 assertion, 0 failed` while the
entire renderer was failing to compile.

`run_tests.gd` now FAILS any case file that reports tests but adds no
assertions, which closes most of that hole. It does not close all of it. **Check
that the assertion count is in the expected range (~62k), not just that failures
are zero.**

### 2.5 The simulation purity linter is a substring matcher

`tests/cases/test_sim_purity.gd` mechanically enforces the core invariants. It
strips comments, then substring-matches the remaining live code — so it is blunt
in ways that will surprise you.

**Banned tokens**, over `core/sim.gd`, `core/spatial_hash.gd`, `core/rng.gd` and
`core/database.gd`:

- `Vector2` — which means **`Vector2i` is also refused**, because it is a
  substring match. Use two scalars, or an index into a flat array. `Vector3` is
  banned in these files too: the sim is 2D and float64.
- `randf`, `randi`, `randfn`, `randomize`, `RandomNumberGenerator` — use
  `core/rng.gd`.
- `sin(`, `cos(`, `atan`, `pow(` — not bit-reproducible across libm.
- `Time.`, `OS.get_ticks`, `Engine.get_frames`, `get_process_delta` — the tick
  is the only clock inside the simulation.
- `get_overlapping` — use the `SpatialHash` broadphase, not the physics server.

**Magic numbers** are banned over a narrower list, `core/sim.gd` and
`core/spatial_hash.gd`. A bare `1 << 15`, or a constant like
`CARRY_TIER_CAP := 99`, will be rejected. Every number that changes how the game
plays belongs in `data/*.json`.

Because comments are stripped first, a doc comment may freely name `Vector2` or
`pow(` to explain why they are avoided — and several do.

`render/`, `ui/`, `audio/`, `tools/` and `tests/` are **not** linted at all. Use
`Vector3`, trigonometry and literals freely there.

### 2.6 Generated assets — with one deliberate exception

Almost everything is generated: every **surface** texture by
`render/material_library.gd`, every **sound** by `audio/sfx.gd`, and all board
geometry from Godot primitives welded with `SurfaceTool`. If you need a new
surface, add a family to `data/materials.json` — do not add an image.

**The exception is entity art.** `assets/art/` holds sixteen authored sprites —
five turret families and eleven drone classes — and they are what the game
draws for turrets and drones. They are sliced from a single authored sheet by
`tools/slice_sprites.py`, which is committed so that re-exporting the sheet is
one command rather than an archaeology exercise.

Do **not** "restore" procedural entity meshes over them. The generated meshes
still exist and are still the fallback when a sprite is missing, which is what
keeps a checkout with no `assets/` running — but the sprites are the intended
look, and they measured *cheaper* than the meshes they replaced (P0-79).

### 2.7 Two renderers, and they disagree

- **Desktop** runs Forward+ (`rendering_method=forward_plus`): SSAO, real HDR
  bloom, high-quality shadows.
- **Web** runs Compatibility (`rendering_method.web=gl_compatibility`), because
  Forward+ does not target WebGL. This is a browser limit, not a Godot one.

A value tuned by eye against one renderer is **not** valid on the other. Glow is
the case that already bit: the same settings that read as a gentle lift on the
web build washed the entire board to white on Forward+. `_lit()` in the renderer
prefers a `<key>_hdr` entry from `data/theme.json` when a RenderingDevice
exists, so a value stays one number until the two genuinely disagree.

If you have no GPU (most containers), you can still verify Forward+:
`apt-get install mesa-vulkan-drivers` gives you lavapipe, then
`--rendering-driver vulkan`.

---

## 3. The invariants

Four are structural and mechanically enforced; the rest are conventions the
codebase will fight you on.

1. **The simulation is deterministic.** Same data + same seed + same command log
   → bit-identical end state. Replays, the headless balance probe and every
   screenshot comparison depend on it. Never introduce wall-clock time, engine
   time, unordered iteration, or unseeded randomness into `core/`.
2. **No balance constants in code.** If a number changes how the game plays it
   lives in `data/*.json`.
3. **No allocations in the per-tick path.** Entities come from preallocated
   struct-of-arrays pools with free lists and generation stamps.
4. **Entities render through `MultiMesh`**, never one node per entity. Draw
   calls must stay flat as entity count climbs — verified at 22 draw calls from
   0 to 400 entities by `tools/render_stress.gd`.

Plus, learned the hard way and stated because they are not obvious:

5. **The simulation never learns it is being drawn.** `core/` has no `Node`, no
   scene tree, no `delta`. The sim world is flat `(x, y)`; the renderer maps it
   to `(x, 0, y)`. Moving from 2D to 3D changed no game logic at all.
6. **A cached value that can go stale independently of what it describes is a
   silent wrong answer.** This caused three separate bugs in one session. The
   fix is always structural — own the value where it is derived — never "be
   careful". See `DECISIONS.md` P0-70.
7. **Cosmetic code must never be able to fail real work.** A missing or
   malformed `data/materials.json` costs textures, not the game. Every *other*
   data file is load-bearing and must stop the program with a readable message.

---

## 4. Commands

All from the repository root.

```bash
# Play
godot --path game

# Tests
GODOT=/path/to/godot ./game/run_tests.sh                          # all of it
GODOT=/path/to/godot ./game/run_tests.sh --full                   # + every level end to end
GODOT=/path/to/godot ./game/run_tests.sh -- --case test_materials # one file

# Web build → build/web/, then publish by copying into docs/play/
GODOT=/path/to/godot ./game/build_web.sh
python3 game/tools/verify_web.py        # boots it in real Chromium, reports page errors

# Desktop build (needs export templates installed)
godot --headless --path game --export-release "Linux" ../build/linux/lastline.x86_64
```

### Dev tools — use these instead of guessing

Every one of these exists because a question came up that could not be answered
by reading code.

```bash
# Drive the game to any state and save a PNG. No monitor needed.
xvfb-run -a godot --path game --rendering-driver opengl3 \
    --script res://tools/capture_screenshot.gd -- --wave 9 --enemies 6 --zoom 10 --out shot.png

# Where a frame goes, stage by stage, on a real board.
xvfb-run -a godot --path game --rendering-driver opengl3 \
    --script res://tools/frame_profile.gd -- --level 46 --wave 10

# Draw calls vs entity count — the MultiMesh invariant.
xvfb-run -a godot --path game --rendering-driver opengl3 --script res://tools/render_stress.gd

# Play the whole 48-level campaign headless and print the table. Reports, does not assert.
godot --headless --path game --script res://tools/balance_probe.gd
godot --headless --path game --script res://tools/balance_probe.gd -- --from 32 --to 35 --idle

# Cost of the generated surface maps, per family.
godot --headless --path game --script res://tools/material_probe.gd

# Frame-time DISTRIBUTION in a real browser (p99, worst, long-frame count).
python3 game/tools/frame_jitter.py
```

---

## 5. Layout

```
core/       deterministic simulation — no Node, no scene tree, no engine time
  sim.gd            the tick; entity pools; wave director; targeting; combat (~3,000 lines)
  database.gd       loads and validates every balance value from data/
  rng.gd            the only sanctioned randomness (PCG-XSH-RR)
  spatial_hash.gd   allocation-free broadphase, and the bounding box of its contents
  state_hash.gd     bit-exact fingerprint of simulation state (FNV-1a)
data/       every balance value and every look decision, as JSON
  sim.json building.json economy.json scaling.json damage.json levels.json
  theme.json        colours, lighting, camera, quality thresholds
  materials.json    procedural surface families (art direction)
  blueprints/ enemies/ modules/ affixes/ maps/ waves/
render/
  sim_renderer_3d.gd  everything drawn: board, entities, effects, camera, quality tiers
  material_library.gd generated albedo / normal / roughness maps, per family
  debug_overlay.gd    F3: fps, worst frame, long frames, quality, GPU, pools
audio/      sfx.gd — every sound, synthesised at startup, played from a voice pool
ui/         hud.gd — stats, wave preview, banner, debrief, transient notices
tools/      headless dev utilities (see §4)
tests/      run_tests.gd is the entry point; 35 case files under tests/cases/
main.gd     entry point: owns the Sim, drives it at fixed 30Hz, routes input
```

### The one structural idea worth internalising

`main.gd` accumulates real time and spends it in **whole fixed 30 Hz ticks**,
handing the leftover fraction to the renderer as `alpha` for interpolation. The
simulation knows nothing about frames. Everything good downstream — replays,
4× speed for free, the headless balance probe, determinism across machines —
falls out of that one split. Do not put game logic in `_process`.

---

## 6. How this codebase expects you to work

The conventions below are visible in every commit and every comment. Matching
them matters more than usual here, because the comments are load-bearing
documentation.

- **Measure, don't extrapolate.** "Should be faster" is not a result. Every
  performance claim in `DECISIONS.md` has a number and a tool that produced it.
  If a tool does not exist for your question, write one in `tools/` — that is
  where five of the nine came from.
- **Comments explain WHY, and record what was tried and rejected.** The house
  style is a doc comment that states the reason a thing exists and the failure
  it prevents, not a restatement of the code. Read `render/material_library.gd`
  for the register.
- **Commit messages are what + why + verified-with-numbers.** Big claims need a
  live run.
- **When a test finds a failure class, fix the class and leave a regression
  test.** Do not fix the instance.
- **Tests assert the thing that would cost a player something.** Look at
  `tests/cases/test_auto_quality.gd`: it pins the boundary (25 fps against a
  24 fps floor must never trip) rather than the happy path.
- **Trials and scratch work live OUTSIDE the repository**, in a scratch
  directory, with their own config. Guard any driver script with
  `if __name__ == "__main__"`.
- **Never weaken TLS.** Do not disable certificate verification or unset a proxy
  to make a download work.

### Publishing

`docs/play/` is a committed copy of the web build, served by GitHub Pages at
<https://privatepracticeconsultants.github.io/Claudevideo/play/>. To publish:
build, copy `build/web/*` into `docs/play/`, commit, push, then **confirm the
live site actually updated** by comparing hashes rather than assuming:

```bash
sha256sum docs/play/index.pck
curl -sL https://privatepracticeconsultants.github.io/Claudevideo/play/index.pck | sha256sum
```

Pages takes 30–90 seconds. Do not report a link as updated before those match.

---

## 7. Where things stand

**Complete and audited:** 48 levels across 12 boards played as 4 acts each; four
weapon families with four tiers, five targeting orders and support links; eight
drone classes including champions, menders, jammers, a splitter and a Breach
Borer that opens new road mid-act; three damage types against three armour
classes; per-act affixes; a module drafted between acts; purchasable ground with
premium tiles; overcharge, doctrines, rigs and veterancy; saved progress;
synthesised sound; an end-of-act debrief; procedural surfaces on ten material
families; a three-rung quality ladder that steps itself down on slow hardware.

Formally this is P0 plus most of P1/P2 content. **The run layer (P3) is the next
real milestone** — seeded runs, Daily Contracts, meta-progression. `post-launch.md`
is the backlog of deferred ideas, each tagged with the earliest phase it could
land; it is explicitly not a commitment list.

### Open thread — performance on real hardware

This is the only live investigation. Read `DECISIONS.md` P0-72, P0-75 and P0-76
before touching it; a lot of ground has already been covered and eliminated.

What is established:

- The container this was developed in has **no GPU** (llvmpipe / SwiftShader).
  Absolute frame-time numbers from it are meaningless; only ordering and ratios
  between configurations transfer. Never quote them as if they were hardware.
- The user's machine reported **11.5 fps with 2 platforms and 12 drones on
  wave 1**, worst frame 93.2 ms against an 86.7 ms average, zero long frames.
  That is uniform slowness, not stutter, and it is nearly independent of what is
  on the board — `render_stress` shows ~130 ms/frame with zero enemies. **The
  cost is static fill.** Optimising entities, targeting or the tick loop will
  not move it.
- Already eliminated as causes: the tick loop (correct fixed-timestep
  accumulator), interpolation (enemies interpolate on distance-along-path), the
  HUD (signature-gated, no per-frame string churn), audio (pre-allocated
  throttled voice pool), and draw calls (flat at 22).
- Strong hypothesis, **not yet confirmed**: the browser is rendering in
  software. 11.5 fps on an almost-empty scene at ~920k pixels is not hardware
  behaviour. F3 now reports the video adapter for exactly this reason. The next
  data point needed is that `gpu` line from the affected machine. If it names a
  real GPU and it is still ~11 fps, that is a genuinely different problem.

If you continue this: the remaining levers are all fill-rate — `render_pixel_budget`
and `render_scale_floor` in `theme.json`, the transparent ground overlay, the
scenery layer, and the shadow pass. Measure with `tools/frame_jitter.py` (shape)
and `tools/frame_profile.gd` (CPU stages), not with the frame counter.

### Known rough edges

- `tests/framework.gd` is a hand-rolled test harness because GUT could not be
  fetched. Porting to GUT is a clean, self-contained job if you want it —
  nothing else depends on `framework.gd`.
- `_refresh_link_lines` is ~5 ms on a 208-turret board and fires on every turret
  placement. Reduced but not eliminated; the remaining cost is inside
  `rebuild_link_pairs`.
- The quality ladder's numbers in `README.md` come from a GPU-less machine and
  are labelled as such. Re-measure on real hardware before trusting them.

---

## 8. Git

Develop and push only to the branch named in your task instructions. Do not open
a pull request unless you are explicitly asked to. Do not commit `build/` (it is
ignored); **do** commit `docs/play/` when publishing, and the `.uid` files Godot
generates next to new scripts.
