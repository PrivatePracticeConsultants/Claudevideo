class_name Sim
extends RefCounted

## The deterministic engagement simulation.
##
## Contract, in order of how much damage breaking it does:
##
## 1. DETERMINISM. Same data + same seed + same command log => bit-identical end
##    state, on every platform. This is what buys replays, seeded Daily
##    Contracts, and the headless balance sim that keeps 45 modules tuned by one
##    person. Everything below is in service of it.
##      - No Vector2 anywhere in this file. Godot's Vector2 is float32 in
##        standard builds; float64 arithmetic is IEEE-754 and reproducible.
##      - Only +, -, *, / and sqrt(). sqrt is correctly rounded by IEEE-754;
##        sin/cos/atan2/pow are not specified to the last bit and differ between
##        libm implementations. The path tables below exist so that following a
##        path needs no trigonometry at all.
##      - No engine time, no frame delta, no global RNG. The tick is the clock.
##      - No iteration over Dictionary keys: insertion order would leak into the
##        sim. All lookups go through the index tables built in _build_*.
##
## 2. NO BALANCE CONSTANTS. Every number that changes how the game plays is read
##    from Database. tests/test_sim_purity.gd fails the build on a stray literal.
##
## 3. NO ALLOCATIONS PER TICK. All storage is preallocated to the ceilings in
##    sim.json and recycled through free lists. step() must not create an Array,
##    Dictionary, String or Object.
##
## This class is pure logic - it is a RefCounted, not a Node, and never touches
## the scene tree. That is what lets tests and the balance sim run it headless at
## thousands of ticks a second.

enum { RESULT_RUNNING, RESULT_WIN, RESULT_LOSS }
enum { PHASE_WAITING, PHASE_SPAWNING, PHASE_CLEARING, PHASE_DONE }
enum { CMD_PLACE, CMD_UPGRADE, CMD_BUY_CELL, CMD_SELL, CMD_SEND_WAVE, CMD_SET_PRIORITY }

## What a turret shoots at when more than one thing is in range.
##
## FIRST is the default and is what every turret did before this existed, so
## nothing in the campaign's measured balance moves unless the player asks for it.
## The others are not strictly-better options; each is the wrong answer somewhere:
## LAST holds a leaker back for the turrets behind it and wastes a front line's
## uptime; NEAREST keeps a suppressor's slow on whatever is closest to it rather
## than whatever is closest to the exit; TOUGHEST puts a railgun on the Breaker and
## lets forty Skitters walk past it; WEAKEST is how a cannon line clears chaff so
## the heavy guns are never distracted.
enum { TARGET_FIRST, TARGET_LAST, TARGET_NEAREST, TARGET_TOUGHEST, TARGET_WEAKEST }
const TARGET_MODE_NAMES := ["First", "Last", "Nearest", "Toughest", "Weakest"]

## How many there are. A function and not a const because GDScript will not accept
## `TARGET_MODE_NAMES.size()` in a constant expression, and writing the number out
## would put a literal in a file whose whole point is that it has none.
static func target_mode_count() -> int:
	return TARGET_MODE_NAMES.size()

## Why a placement was refused. Returned rather than a bare bool so the build
## cursor can tell the player which rule they are breaking instead of just
## refusing to light up.
enum {
	BUILD_OK,
	BUILD_ON_PATH,
	BUILD_OVERLAPS,
	BUILD_OUT_OF_BOUNDS,
	BUILD_NO_CAPITAL,
	BUILD_AT_LIMIT,
	BUILD_LOCKED,
}

# --- immutable tables, built once from Database ------------------------------

var _db: Database
var _tick_rate: int
var _max_enemies: int
var _max_projectiles: int
var _max_platforms: int

# Path geometry. Precomputing per-segment unit vectors and cumulative lengths
# turns "walk along the path" into one multiply-add and removes every call to a
# trigonometric function from the hot loop.
var _wp_x: PackedFloat64Array = PackedFloat64Array()
var _wp_y: PackedFloat64Array = PackedFloat64Array()
var _seg_dx: PackedFloat64Array = PackedFloat64Array()
var _seg_dy: PackedFloat64Array = PackedFloat64Array()
var _seg_cum: PackedFloat64Array = PackedFloat64Array()
## Per-segment length, stored rather than derived from consecutive _seg_cum
## entries. Cumulative distance restarts at zero for each route, so the difference
## across a route boundary is meaningless - and the placement rules walk every
## segment in the board without caring which route it belongs to.
var _seg_len: PackedFloat64Array = PackedFloat64Array()
## Each segment's start point, so the placement scan can walk every segment on the
## board in one flat loop without mapping indices back to routes.
var _seg_ax: PackedFloat64Array = PackedFloat64Array()
var _seg_ay: PackedFloat64Array = PackedFloat64Array()
var _seg_count: int = 0
var _path_length: float = 0.0

## A board may carry more than one complete route from the gate to the exit.
##
## Modelled as N complete routes rather than as a graph with branch nodes, and
## that is the whole reason this was affordable: a drone's position stays one
## scalar distance along one polyline, the placement rules stay "distance to the
## nearest segment", and nothing in the tick learned what a junction is. What it
## costs is that the routes are authored whole and share only their endpoints -
## which is exactly what a fork that rejoins looks like anyway.
##
## The point of it is that coverage has to be DIVIDED. A single road rewards one
## long line; two roads mean every turret is choosing which one it watches, and
## the deployment limit means it cannot watch both.
var _route_first_wp: PackedInt32Array = PackedInt32Array()
var _route_wp_count: PackedInt32Array = PackedInt32Array()
var _route_first_seg: PackedInt32Array = PackedInt32Array()
var _route_seg_count: PackedInt32Array = PackedInt32Array()
var _route_length: PackedFloat64Array = PackedFloat64Array()
var _route_count: int = 1
## Flat lookup of route-per-spawn, one entry per unit of weight, so a board can
## send two drones down the long way for every one down the short way without any
## arithmetic at spawn time.
var _route_pick: PackedInt32Array = PackedInt32Array()
var _spawn_cursor: int = 0

# Placement is free-form: anywhere in a band alongside the corridor, rather than
# on a fixed set of pads. These are the rules that define that band.
var _build_min_dist: float = 0.0
var _build_max_dist: float = 0.0
var _build_min_spacing: float = 0.0
## Deployment limit: how many platforms may exist at once in this engagement.
##
## This exists because free placement removed the natural scarcity that fixed
## pads provided. With unlimited positions, a new tier-1 turret is always better
## Capital-for-damage than upgrading an existing one (0.20 dps/$ against 0.13 and
## falling), so nothing would ever be upgraded and the tier ladder would be dead
## content. Capping deployments makes "which positions do I commit to, and how
## hard do I invest in them" the actual decision.
var _platform_limit: int = 0

## Buildable ground is a grid of cells, not a continuous band.
##
## Cells near the corridor start unlocked; the rest can be bought with Capital,
## but only next to ground you already hold, so expansion spreads outward from
## the road instead of letting you claim an unrelated corner of the map. That
## turns "where can I build" from a fixed constraint into something you invest
## in, and gives Capital a third use alongside placing and upgrading.
var _cell_size: float = 0.0
var _grid_cols: int = 0
var _grid_rows: int = 0
var _cell_unlocked: PackedByteArray = PackedByteArray()
var _cell_buildable: PackedByteArray = PackedByteArray()
var _cells_bought: int = 0
var _cell_base_cost: int = 0
var _cell_cost_step: int = 0
var _bounds_width: float = 0.0
var _bounds_height: float = 0.0

var _type_ids: PackedStringArray = PackedStringArray()
var _type_display: PackedStringArray = PackedStringArray()
var _type_base_hp: PackedInt64Array = PackedInt64Array()
var _type_speed: PackedFloat64Array = PackedFloat64Array()
var _type_leak: PackedInt32Array = PackedInt32Array()
var _type_base_bounty: PackedInt64Array = PackedInt64Array()
var _type_radius: PackedFloat64Array = PackedFloat64Array()
var _type_jitter: PackedFloat64Array = PackedFloat64Array()
## How much of a suppression effect a drone shrugs off, 0 (none) to 1 (immune).
## Absent means 0, so every drone written before suppression existed reads right.
var _type_slow_resist: PackedFloat64Array = PackedFloat64Array()
## Flat damage subtracted from every hit this drone takes, before its health.
##
## Flat and not a percentage because that is what makes it a shape and not a
## multiplier: armour 3 is nothing to a Railgun landing 640 and most of the round
## to a tier-1 Autocannon landing 5. It is the answer to "one weapon does
## everything" that a percentage resistance would not be, and it never reduces a
## hit below 1 - a weapon that literally cannot scratch something reads as broken
## rather than as a counter.
## It scales with the wave the way health does, and is capped as a share of the
## hit it is subtracting from.
##
## Both halves are needed. Flat armour in a game where health grows exponentially
## is a texture that exists for four levels and then evaporates - by the last act
## of Terminus a drone carries 40x its base health and 3 armour is nothing. But
## armour that scales without a cap is worse: at the same point it would be 40,
## and a tier-4 Autocannon landing 42 would do 2. The cap says armour may never
## take more than `armour_max_bite` of a round, so it stays a reason to bring a
## bigger gun at every scale and never becomes a reason the small ones stop
## working.
var _type_armour: PackedInt32Array = PackedInt32Array()
var _type_armour_now: PackedInt32Array = PackedInt32Array()
var _armour_max_bite: float = 0.0
## What this drone leaves behind when it dies, and how many. -1 for the drones
## that simply die. Validated one hop deep: a splitting type may only split into a
## non-splitting one, which makes a cycle impossible to write.
var _type_split_into: PackedInt32Array = PackedInt32Array()
var _type_split_count: PackedInt32Array = PackedInt32Array()
## Health this drone puts back into the OTHER drones around it, per tick, and how
## far that reaches.
##
## The first enemy property that makes the targeting orders added alongside it
## worth having. Everything before this could be answered by pointing more guns at
## the road; a mender has to be picked out and killed, and it is small and cheap,
## so First and Toughest walk straight past it. It never heals itself - a drone
## that outheals your line while also being the toughest thing in it is not a
## puzzle, it is a wall.
var _type_repair: PackedInt32Array = PackedInt32Array()
var _type_repair_now: PackedInt32Array = PackedInt32Array()
var _type_repair_interval: PackedInt32Array = PackedInt32Array()
var _type_repair_radius_sq: PackedFloat64Array = PackedFloat64Array()
## Ticks of silence this drone imposes on turrets it passes, and how far.
##
## The first drone that attacks the BOARD rather than walking past it. Everything
## else in the game is a health bar moving along a road; a jammer makes the
## question "where is my line thinnest" have an answer that changes while you
## watch it.
var _type_jam_ticks: PackedInt32Array = PackedInt32Array()
var _type_jam_radius_sq: PackedFloat64Array = PackedFloat64Array()
var _type_jam_interval: PackedInt32Array = PackedInt32Array()
## False for every engagement whose drone roster has neither, which is most of the
## campaign - and it skips a 2,048-slot scan per tick for all of them.
var _has_support_drones: bool = false

## What a weapon family projects onto the turrets around it.
##
## The rule that makes this a decision rather than a bonus: a family's link only
## reaches turrets of a DIFFERENT family. Four Autocannons in a row get nothing
## from each other; an Autocannon flanked by a Mortar and a Suppressor is worth
## considerably more than the sum of its parts. Placement already decided
## coverage; this makes it decide composition too, on a board where the
## deployment limit means every emplacement is a choice you cannot take back
## cheaply.
##
## The bonus scales with the GRANTING turret's tier, so upgrading a Suppressor
## makes the four guns around it better as well as itself - which is the first
## reason in the game to upgrade something that is not your best gun.
var _bp_support_radius_sq: PackedFloat64Array = PackedFloat64Array()
var _bp_support_rate: PackedFloat64Array = PackedFloat64Array()
var _bp_support_damage: PackedFloat64Array = PackedFloat64Array()
var _bp_support_range: PackedFloat64Array = PackedFloat64Array()
var _bp_support_tier_scale: PackedFloat64Array = PackedFloat64Array()
## Ceilings on what one turret can receive, summed over every source reaching it.
## Capped by TOTAL rather than by source count, because "the first three
## contributors in index order" is deterministic and arbitrary, and a rule nobody
## can predict is not a rule anyone can play around.
var _support_cap_rate: float = 0.0
var _support_cap_damage: float = 0.0
var _support_cap_range: float = 0.0

# Blueprint tiers are flattened to [tier_slot]; a blueprint's tier t lives at
# _bp_tier_offset[b] + t. P0 only ever places tier 0, but the table already has
# room for T2-T4 so P2 is a data change and not a storage change.
var _bp_ids: PackedStringArray = PackedStringArray()
var _bp_tier_offset: PackedInt32Array = PackedInt32Array()
var _bp_tier_count: PackedInt32Array = PackedInt32Array()
var _tier_cost: PackedInt64Array = PackedInt64Array()
var _tier_damage: PackedInt64Array = PackedInt64Array()
var _tier_range_sq: PackedFloat64Array = PackedFloat64Array()
var _tier_interval: PackedInt32Array = PackedInt32Array()
var _tier_proj_speed: PackedFloat64Array = PackedFloat64Array()
var _tier_hit_radius: PackedFloat64Array = PackedFloat64Array()
var _tier_proj_life: PackedInt32Array = PackedInt32Array()
# Splash radius of 0 means a single-target hit; anything above it damages
# everything inside the radius, falling off linearly to splash_min_fraction at
# the edge.
var _tier_splash_radius: PackedFloat64Array = PackedFloat64Array()
var _tier_splash_min: PackedFloat64Array = PackedFloat64Array()
## Suppression: how far a hit drags a drone's speed down, and for how long.
## A factor of 1.0 means "does not slow", which is what every weapon written
## before suppression existed reads as.
var _tier_slow_factor: PackedFloat64Array = PackedFloat64Array()
var _tier_slow_ticks: PackedInt32Array = PackedInt32Array()
## Half-width of the lane a piercing shot damages along its flight path. Zero
## means the shot stops at whatever it hit, which is every weapon written before
## the Railgun existed.
var _tier_pierce: PackedFloat64Array = PackedFloat64Array()

var _hp_growth: float = 1.0
var _bounty_growth: float = 1.0
var _act_hp_mult: float = 1.0
var _act_bounty_mult: float = 1.0
var _inter_wave_delay: int = 0
var _wave_count: int = 0

# --- mutable simulation state ------------------------------------------------

var _rng: Rng
var _seed: int = 0
var _tick: int = 0
var _result: int = RESULT_RUNNING
var _capital: int = 0
var _integrity: int = 0
var _kills: int = 0
var _leaks: int = 0
var _spawn_overflow: int = 0
var _projectile_overflow: int = 0
var _unknown_enemy_groups: int = 0
var _rejected_commands: int = 0
## Turrets sold and waves called early. Both are hashed: they change Capital, so
## a desync in either is a desync in what the player could afford.
var _sold: int = 0
var _early_calls: int = 0
var _sell_refund: float = 0.0
var _early_wave_bonus: int = 0
## Turrets carried from the previous act that no longer fit - almost always
## because the extended corridor now runs where they stood.
var _carry_dropped: int = 0
## Turrets that fit fine but exceeded the share of the new limit an inheritance
## may occupy.
var _carry_stood_down: int = 0
## Capital carried in from the previous board. Hashed because it changes what
## could be afforded, which changes everything downstream of it.
var _salvage_granted: int = 0
## Whether this act opened on the integrity the last one ended with rather than
## on a full bar. Hashed, because it changes how much room the board has.
var _integrity_inherited: bool = false
## Modules drafted earlier in this chain, and what they add up to. All applied at
## construction; nothing here changes once the act has started.
var _module_ids: PackedStringArray = PackedStringArray()
var _module_rate: float = 0.0
var _module_damage: float = 0.0
var _module_range: float = 0.0
var _upgrade_discount: float = 0.0
var _jam_resist: float = 0.0
var _integrity_max: int = 0
var _salvage_fraction: float = 0.0
var _salvage_cap_share: float = 0.0
## Interest paid on Capital still in hand when a wave begins.
##
## The point is not the money, it is the decision. Without it, every Capital not
## spent the instant it arrives is Capital wasted, so "build now" beats "build
## better in two waves" unconditionally and there is no reason to ever hold
## anything. Capped, because a percentage of an unbounded pile is an unbounded
## pile: uncapped, the correct play late in a long act becomes building nothing
## and banking, which is the opposite of the decision it was added to create.
var _interest_rate: float = 0.0
var _interest_cap: int = 0
var _interest_paid: int = 0

var _phase: int = PHASE_WAITING
var _wave_index: int = -1
var _phase_timer: int = 0

## Enemy storage, struct-of-arrays. Read-only outside core/.
##
## Position is derived, not stored: `e_prog` (distance travelled along the path)
## plus `e_offset` (fixed lateral offset) is the authoritative state, and x/y are
## recomputed from it each tick. That halves the mutable float state, makes
## "furthest along the path" - the First targeting priority - a single scalar
## compare, and lets the renderer interpolate along the path instead of through
## it, so enemies round corners instead of cutting them.
var e_alive: PackedByteArray = PackedByteArray()
var e_gen: PackedInt32Array = PackedInt32Array()
var e_hp: PackedInt64Array = PackedInt64Array()
var e_hp_max: PackedInt64Array = PackedInt64Array()
var e_prog: PackedFloat64Array = PackedFloat64Array()
var e_prev_prog: PackedFloat64Array = PackedFloat64Array()
var e_offset: PackedFloat64Array = PackedFloat64Array()
var e_speed: PackedFloat64Array = PackedFloat64Array()
var e_type: PackedInt32Array = PackedInt32Array()
var e_bounty: PackedInt64Array = PackedInt64Array()
var e_leak: PackedInt32Array = PackedInt32Array()
## Suppression currently on this drone: ticks remaining, and the multiplier
## applied to its speed while they last. Refreshed rather than stacked - the
## strongest slow wins and re-arms the timer - because multiplying slows together
## spirals to a standstill and makes one turret worth more than the ten around it.
var e_slow_ticks: PackedInt32Array = PackedInt32Array()
var e_slow_factor: PackedFloat64Array = PackedFloat64Array()
## Which of the board's routes this drone is walking. Assigned at spawn and
## never changed: the routes are complete roads, not a graph, so there is no
## junction at which changing it would mean anything.
var e_route: PackedInt32Array = PackedInt32Array()
var e_x: PackedFloat64Array = PackedFloat64Array()
var e_y: PackedFloat64Array = PackedFloat64Array()
var e_live_count: int = 0
var _e_free: PackedInt32Array = PackedInt32Array()
var _e_free_top: int = 0

## Drones that died this tick and owe children, resolved at the end of the tick
## rather than where they died.
##
## Not a stylistic choice. Area damage and piercing walk the spatial hash, which
## was built at the top of the tick and still lists slots whose occupants have
## since been killed. Spawning a child immediately can hand it one of those stale
## slots, and the very blast that killed its parent would then find it alive and
## hit it too - deterministic, but arbitrary, and dependent on which slot the free
## list happened to return. Deferring to the end of the tick means a brood always
## emerges into the next tick's hash, whatever killed it.
var _split_type: PackedInt32Array = PackedInt32Array()
var _split_prog: PackedFloat64Array = PackedFloat64Array()
var _split_route: PackedInt32Array = PackedInt32Array()
var _split_pending: int = 0

## Projectile storage. `p_target_gen` is not redundant with `p_target`: slots are
## recycled, so without a generation stamp a projectile in flight can land on a
## brand-new enemy that happens to have been given the dead one's slot.
var p_alive: PackedByteArray = PackedByteArray()
var p_x: PackedFloat64Array = PackedFloat64Array()
var p_y: PackedFloat64Array = PackedFloat64Array()
var p_prev_x: PackedFloat64Array = PackedFloat64Array()
var p_prev_y: PackedFloat64Array = PackedFloat64Array()
var p_target: PackedInt32Array = PackedInt32Array()
var p_target_gen: PackedInt32Array = PackedInt32Array()
var p_damage: PackedInt64Array = PackedInt64Array()
var p_speed: PackedFloat64Array = PackedFloat64Array()
var p_hit_radius: PackedFloat64Array = PackedFloat64Array()
var p_life: PackedInt32Array = PackedInt32Array()
var p_splash: PackedFloat64Array = PackedFloat64Array()
var p_splash_min: PackedFloat64Array = PackedFloat64Array()
var p_slow_factor: PackedFloat64Array = PackedFloat64Array()
var p_slow_ticks: PackedInt32Array = PackedInt32Array()
var p_pierce: PackedFloat64Array = PackedFloat64Array()
## Where the shot was fired from. A piercing round damages the whole lane between
## there and the point it went off, so it has to remember its own origin.
var p_from_x: PackedFloat64Array = PackedFloat64Array()
var p_from_y: PackedFloat64Array = PackedFloat64Array()
## Which weapon family fired it, for the damage tally. The blueprint index and not
## the platform index: see _family_damage.
var p_family: PackedInt32Array = PackedInt32Array()
var p_live_count: int = 0
var _p_free: PackedInt32Array = PackedInt32Array()
var _p_free_top: int = 0

## Platform storage.
var t_used: PackedByteArray = PackedByteArray()
var t_blueprint: PackedInt32Array = PackedInt32Array()
var t_tier: PackedInt32Array = PackedInt32Array()
var t_tier_slot: PackedInt32Array = PackedInt32Array()
var t_cooldown: PackedInt32Array = PackedInt32Array()
var t_x: PackedFloat64Array = PackedFloat64Array()
var t_y: PackedFloat64Array = PackedFloat64Array()
## Direction the turret last fired in, as a unit vector. Kept in the simulation
## rather than recomputed by the renderer because it is a pure function of what
## the turret did, and deriving it in the renderer would mean scanning every
## enemy for every turret on every frame to answer a question the sim already
## knew the answer to.
var t_aim_x: PackedFloat64Array = PackedFloat64Array()
var t_aim_y: PackedFloat64Array = PackedFloat64Array()
## Which enemy this turret picks when several are in range. Simulation state, not
## a display preference: it decides what gets shot, so it is logged as a command,
## hashed, and carried between acts along with the emplacement it belongs to.
var t_priority: PackedInt32Array = PackedInt32Array()
## Ticks this turret is jammed for. It does not fire and its cooldown does not
## advance, so a jamming pass costs exactly the uptime it looks like it costs.
var t_disabled: PackedInt32Array = PackedInt32Array()
## What the turrets around this one are worth to it, as multipliers on its own
## tier stats. All 1.0 for a turret standing on its own. Derived state, recomputed
## when the board changes rather than per tick - it can only change when something
## is placed, upgraded or sold.
var t_rate_mult: PackedFloat64Array = PackedFloat64Array()
var t_damage_mult: PackedFloat64Array = PackedFloat64Array()
var t_range_mult: PackedFloat64Array = PackedFloat64Array()
var t_count: int = 0

## Which turrets are linked to which, flat [a, b, a, b, ...], for the renderer to
## draw. Derived, so it is not hashed - the multipliers it produces are, which is
## what actually decides anything.
var _link_pairs: PackedInt32Array = PackedInt32Array()
## Set when the board changes. The recompute is O(turrets squared), which at 144
## is twenty thousand compares - nothing once in a while, and far too much every
## tick.
var _links_dirty: bool = true
## Which turret changed, or -1 for "recompute everything". Two changes before a
## recompute fall back to -1, which is correct and costs nothing: commands are
## applied once per tick and the recompute runs on the same tick.
var _links_touched: int = -1
## The furthest any family's link reaches, so a partial recompute knows how wide
## its neighbourhood is.
var _max_support_radius: float = 0.0

## Damage actually landed, per weapon family, and drones killed per family.
##
## Per FAMILY rather than per emplacement on purpose: selling compacts the
## platform pool by moving the last turret into the freed slot, so an emplacement
## index is only meaningful within a tick and a shot already in flight would credit
## whoever inherited the slot. A blueprint index never moves. Overkill is not
## counted - a shot that does 640 to a drone with 12 left is credited with 12,
## because the debrief exists to tell the truth about where the damage went.
var _family_damage: PackedInt64Array = PackedInt64Array()
var _family_kills: PackedInt32Array = PackedInt32Array()

# Active-wave spawn cursors, sized to the widest wave in the data.
var _g_enemy_type: PackedInt32Array = PackedInt32Array()
var _g_remaining: PackedInt32Array = PackedInt32Array()
var _g_interval: PackedInt32Array = PackedInt32Array()
var _g_next_tick: PackedInt32Array = PackedInt32Array()
var _g_count: int = 0

# Per-wave scaled values, recomputed when a wave begins.
var _type_hp_now: PackedInt64Array = PackedInt64Array()
var _type_bounty_now: PackedInt64Array = PackedInt64Array()

# Command log. Parallel packed arrays rather than an Array[Dictionary] so that
# replaying commands costs nothing and the log itself can be hashed.
var _cmd_tick: PackedInt32Array = PackedInt32Array()
var _cmd_type: PackedInt32Array = PackedInt32Array()
var _cmd_a: PackedInt32Array = PackedInt32Array()
var _cmd_b: PackedInt32Array = PackedInt32Array()
var _cmd_c: PackedInt32Array = PackedInt32Array()
var _cmd_cursor: int = 0

var _hash: SpatialHash

# Scratch scalars used instead of returning a Vector2/Array from the path
# sampler, so sampling a position allocates nothing.
var _out_x: float = 0.0
var _out_y: float = 0.0

func _init(db: Database, seed_value: int) -> void:
	_db = db
	_seed = seed_value
	_rng = Rng.new(seed_value)
	_tick_rate = int(db.sim["tick_rate_hz"])
	_max_enemies = int(db.sim["max_enemies"])
	_max_projectiles = int(db.sim["max_projectiles"])
	_max_platforms = int(db.sim["max_platforms"])
	_sell_refund = float(db.economy.get("sell_refund_fraction", 0.0))
	_carry_limit_share = float(db.economy.get("carry_limit_share", 1.0))
	_salvage_fraction = float(db.economy.get("board_salvage_fraction", 0.0))
	_salvage_cap_share = float(db.economy.get("board_salvage_cap_share", 0.0))
	_early_wave_bonus = int(db.economy.get("early_wave_bonus", 0))
	_hp_growth = float(db.scaling["hp_growth_per_wave"])
	_bounty_growth = float(db.scaling["bounty_growth_per_wave"])
	_armour_max_bite = clampf(float(db.scaling["armour_max_bite"]), 0.0, 1.0)
	_interest_rate = maxf(0.0, float(db.economy.get("interest_per_wave", 0.0)))
	_interest_cap = maxi(0, int(db.economy.get("interest_cap", 0)))
	_support_cap_rate = maxf(0.0, float(db.economy.get("support_cap_fire_rate", 0.0)))
	_support_cap_damage = maxf(0.0, float(db.economy.get("support_cap_damage", 0.0)))
	_support_cap_range = maxf(0.0, float(db.economy.get("support_cap_range", 0.0)))
	# The engagement may override the engagement-scoped economy; master plan
	# section 4.1 scales starting Capital by act so a tier-4 platform is
	# reachable in a single fight by act three.
	_capital = int(db.engagement.get("starting_capital", db.economy["starting_capital"]))
	_act_hp_mult = float(db.engagement.get("act_hp_multiplier", 1.0))
	_act_bounty_mult = float(db.engagement.get("act_bounty_multiplier", 1.0))
	_integrity = int(db.economy["starting_integrity"])
	_integrity_max = _integrity
	_inter_wave_delay = int(db.engagement["inter_wave_delay_ticks"])
	_wave_count = (db.engagement["waves"] as Array).size()
	_build_path()
	_load_build_rules()
	_build_types()
	_build_blueprints()
	_build_pools()
	_phase = PHASE_WAITING
	_phase_timer = _inter_wave_delay

# --- construction ------------------------------------------------------------

## The corridor, optionally only partly revealed.
##
## A map file holds the full route; an engagement may use only the first N
## waypoints of it. That is what lets a chain of levels share one board and
## extend it: the prefix is byte-identical between acts, so everything already
## built beside it stays exactly where it was and stays useful.
func _build_path() -> void:
	var full: Array = _db.map["path"]
	var revealed := int(_db.engagement.get("path_waypoints", full.size()))
	revealed = clampi(revealed, 2, full.size())
	# Route 0 is the road the board was authored around; anything in
	# "alternate_paths" is a fork that leaves the gate with it and rejoins at the
	# exit. Absent means one route, which is what every board written before forks
	# existed has, and it costs those boards nothing.
	var routes := [full.slice(0, revealed)]
	# A fork opens only once the corridor is fully revealed - acts I and II of a
	# board are one road, acts III and IV are two.
	#
	# The alternative was revealing a proportional prefix of each fork, and it does
	# not work: a fork is authored to leave the gate and rejoin at the exit, so a
	# prefix of one ends in the middle of nowhere and everything walking it leaks
	# there. Opening at full reveal is both simpler and a better escalation - the
	# board you have learned to hold suddenly has a second way in.
	if revealed >= full.size():
		for extra: Array in (_db.map.get("alternate_paths", []) as Array):
			routes.append(extra)

	_route_count = routes.size()
	_route_first_wp.resize(_route_count)
	_route_wp_count.resize(_route_count)
	_route_first_seg.resize(_route_count)
	_route_seg_count.resize(_route_count)
	_route_length.resize(_route_count)

	var total_wp := 0
	var total_seg := 0
	for route: Array in routes:
		total_wp += route.size()
		total_seg += route.size() - 1
	_wp_x.resize(total_wp)
	_wp_y.resize(total_wp)
	_seg_dx.resize(total_seg)
	_seg_dy.resize(total_seg)
	_seg_cum.resize(total_seg)
	_seg_len.resize(total_seg)
	_seg_ax.resize(total_seg)
	_seg_ay.resize(total_seg)

	var wp := 0
	var seg := 0
	for r in _route_count:
		var route: Array = routes[r]
		_route_first_wp[r] = wp
		_route_wp_count[r] = route.size()
		_route_first_seg[r] = seg
		_route_seg_count[r] = route.size() - 1
		for i in route.size():
			var p: Dictionary = route[i]
			_wp_x[wp + i] = float(p["x"])
			_wp_y[wp + i] = float(p["y"])
		var cum := 0.0
		for i in route.size() - 1:
			var dx := _wp_x[wp + i + 1] - _wp_x[wp + i]
			var dy := _wp_y[wp + i + 1] - _wp_y[wp + i]
			var length := sqrt(dx * dx + dy * dy)
			_seg_dx[seg + i] = dx / length
			_seg_dy[seg + i] = dy / length
			_seg_cum[seg + i] = cum
			_seg_len[seg + i] = length
			_seg_ax[seg + i] = _wp_x[wp + i]
			_seg_ay[seg + i] = _wp_y[wp + i]
			cum += length
		_route_length[r] = cum
		wp += route.size()
		seg += route.size() - 1
	_seg_count = total_seg
	# The longest route is what "how far is it to the exit" means for anything that
	# needs one number - the camera fit, and the deployment limit's road budget.
	_path_length = 0.0
	for r in _route_count:
		_path_length = maxf(_path_length, _route_length[r])

	# Spawn shares, expanded to a flat table so picking one is an index rather than
	# a search. A route with no declared weight carries one share.
	_route_pick = PackedInt32Array()
	var weights: Array = _db.map.get("route_weights", [])
	for r in _route_count:
		var weight := 1
		if r < weights.size():
			weight = maxi(1, int(weights[r]))
		for _w in weight:
			_route_pick.append(r)

func _load_build_rules() -> void:
	_build_min_dist = float(_db.building["min_distance_from_path"])
	# How far the free starting ground reaches. Overridable per engagement so a
	# late level can hand you a narrow shoulder and make buying ground a real
	# decision rather than an optimisation you never need.
	_build_max_dist = float(_db.engagement.get("starting_ground_reach",
		_db.building["max_distance_from_path"]))
	_build_min_spacing = float(_db.building["min_platform_spacing"])
	# The engagement may set its own deployment limit; the pool ceiling in
	# sim.json is the hard upper bound regardless.
	_platform_limit = mini(int(_db.engagement.get("platform_limit", _max_platforms)), _max_platforms)
	var bounds: Dictionary = _db.map["bounds"]
	_bounds_width = float(bounds["width"])
	_bounds_height = float(bounds["height"])
	_cell_size = float(_db.building["cell_size"])
	_cell_base_cost = int(_db.building["cell_base_cost"])
	_cell_cost_step = int(_db.building["cell_cost_step"])
	_build_grid()

## A cell is *buildable* if its centre clears the road; it is *unlocked* if the
## player owns it. Both are fixed-size and computed once - the buildable test
## involves a distance-to-path query that has no business running per click.
func _build_grid() -> void:
	_grid_cols = maxi(1, int(ceil(_bounds_width / _cell_size)))
	_grid_rows = maxi(1, int(ceil(_bounds_height / _cell_size)))
	_cell_unlocked.resize(_grid_cols * _grid_rows)
	_cell_buildable.resize(_grid_cols * _grid_rows)
	for cy in _grid_rows:
		for cx in _grid_cols:
			var index := cy * _grid_cols + cx
			var to_path := distance_to_path(cell_centre_x(cx), cell_centre_y(cy))
			_cell_buildable[index] = 1 if to_path >= _build_min_dist else 0
			# Everything within the starting reach comes free, so a new level is
			# immediately playable without spending on ground.
			_cell_unlocked[index] = 1 if (_cell_buildable[index] == 1 and to_path <= _build_max_dist) else 0

func cell_size() -> float: return _cell_size
func grid_cols() -> int: return _grid_cols
func grid_rows() -> int: return _grid_rows
## Written with a division rather than a 0.5 literal so the "no bare numbers in
## gameplay code" linter stays strict; half a cell is geometry, but relaxing the
## rule to admit it would also admit every balance value someone writes as 0.5.
func cell_centre_x(cx: int) -> float: return float(cx) * _cell_size + _cell_size / 2.0
func cell_centre_y(cy: int) -> float: return float(cy) * _cell_size + _cell_size / 2.0
func cell_x_of(x: float) -> int: return int(floor(x / _cell_size))
func cell_y_of(y: float) -> int: return int(floor(y / _cell_size))
func cell_in_bounds(cx: int, cy: int) -> bool:
	return cx >= 0 and cy >= 0 and cx < _grid_cols and cy < _grid_rows
func cell_is_unlocked(cx: int, cy: int) -> bool:
	return cell_in_bounds(cx, cy) and _cell_unlocked[cy * _grid_cols + cx] == 1
func cell_is_buildable(cx: int, cy: int) -> bool:
	return cell_in_bounds(cx, cy) and _cell_buildable[cy * _grid_cols + cx] == 1
func cells_bought() -> int: return _cells_bought

## Price of the next cell. Climbs with each purchase so expanding is a real
## trade against turrets, rather than something you do reflexively.
func next_cell_cost() -> int:
	return _cell_base_cost + _cell_cost_step * _cells_bought

## A cell can be bought if it is legal ground, not already owned, next to ground
## you already own, and affordable.
func can_buy_cell(cx: int, cy: int) -> bool:
	if not cell_in_bounds(cx, cy):
		return false
	if _cell_unlocked[cy * _grid_cols + cx] == 1:
		return false
	if _cell_buildable[cy * _grid_cols + cx] == 0:
		return false
	if _capital < next_cell_cost():
		return false
	return _has_unlocked_neighbour(cx, cy)

## Orthogonal neighbours only. Allowing diagonals would let a purchase squeeze
## past the corner of the road and claim ground on the far side.
func _has_unlocked_neighbour(cx: int, cy: int) -> bool:
	return cell_is_unlocked(cx - 1, cy) or cell_is_unlocked(cx + 1, cy) \
		or cell_is_unlocked(cx, cy - 1) or cell_is_unlocked(cx, cy + 1)

## True when the cell is adjacent to owned ground and legal, regardless of
## whether it can be paid for - what the renderer needs to show the frontier.
func cell_is_offerable(cx: int, cy: int) -> bool:
	if not cell_in_bounds(cx, cy):
		return false
	if _cell_unlocked[cy * _grid_cols + cx] == 1:
		return false
	if _cell_buildable[cy * _grid_cols + cx] == 0:
		return false
	return _has_unlocked_neighbour(cx, cy)

func _try_buy_cell(cx: int, cy: int) -> bool:
	if not can_buy_cell(cx, cy):
		return false
	_capital -= next_cell_cost()
	_cell_unlocked[cy * _grid_cols + cx] = 1
	_cells_bought += 1
	return true

func _build_types() -> void:
	_type_ids = _db.enemy_ids()
	var n := _type_ids.size()
	_type_base_hp.resize(n)
	_type_speed.resize(n)
	_type_leak.resize(n)
	_type_base_bounty.resize(n)
	_type_radius.resize(n)
	_type_jitter.resize(n)
	_type_slow_resist.resize(n)
	_type_armour.resize(n)
	_type_armour_now.resize(n)
	_type_split_into.resize(n)
	_type_split_count.resize(n)
	_type_repair.resize(n)
	_type_repair_now.resize(n)
	_type_repair_interval.resize(n)
	_type_repair_radius_sq.resize(n)
	_type_jam_ticks.resize(n)
	_type_jam_radius_sq.resize(n)
	_type_jam_interval.resize(n)
	_type_display.resize(n)
	_type_hp_now.resize(n)
	_type_bounty_now.resize(n)
	for i in n:
		var e: Dictionary = _db.enemies[_type_ids[i]]
		_type_base_hp[i] = int(e["base_hp"])
		_type_speed[i] = float(e["speed_units_per_second"]) / float(_tick_rate)
		_type_leak[i] = int(e["leak_value"])
		_type_base_bounty[i] = int(e["base_bounty"])
		_type_radius[i] = float(e["radius"])
		_type_jitter[i] = float(e["spawn_jitter_units"])
		_type_slow_resist[i] = clampf(float(e.get("slow_resistance", 0.0)), 0.0, 1.0)
		_type_armour[i] = maxi(0, int(e.get("armour", 0)))
		# Seconds and units in the data file; ticks and squared units in here,
		# because the tick is the only clock and a square root is the only thing a
		# distance compare would otherwise need.
		#
		# Repair is a PULSE and not a per-tick trickle: at 30 ticks a second, any
		# healing rate a person would write reads as zero once divided down and
		# rounded to an integer, and integer health is not negotiable.
		_type_repair[i] = maxi(0, int(e.get("repair_per_pulse", 0)))
		_type_repair_interval[i] = maxi(1, int(round(
			float(e.get("repair_interval_seconds", 1.0)) * float(_tick_rate))))
		var repair_radius := float(e.get("repair_radius_units", 0.0))
		_type_repair_radius_sq[i] = repair_radius * repair_radius
		_type_jam_ticks[i] = maxi(0, int(round(
			float(e.get("jam_seconds", 0.0)) * float(_tick_rate))))
		var jam_radius := float(e.get("jam_radius_units", 0.0))
		_type_jam_radius_sq[i] = jam_radius * jam_radius
		_type_jam_interval[i] = maxi(1, int(round(
			float(e.get("jam_interval_seconds", 1.0)) * float(_tick_rate))))
		_type_display[i] = str(e.get("display_name", _type_ids[i]))
	# Second pass: a split target is named by id, and every id has to exist before
	# any of them can be resolved.
	for i in n:
		var e: Dictionary = _db.enemies[_type_ids[i]]
		var child := str(e.get("splits_into", ""))
		_type_split_into[i] = -1 if child.is_empty() else _type_index(child)
		_type_split_count[i] = 0 if _type_split_into[i] < 0 else maxi(0, int(e.get("split_count", 0)))
		if _type_split_count[i] <= 0:
			_type_split_into[i] = -1
	# Third pass: does THIS engagement field anything with a support role?
	#
	# Asked of the engagement's own wave list rather than of the roster, because
	# the roster always contains a Mender and the question being answered is
	# whether this level pays for a per-tick scan of the whole enemy pool. Most of
	# the campaign fields neither, and most of the campaign should not pay for
	# them. Split children count: a carrier that hatched menders would still need
	# the scan, even though nothing spawned one directly.
	_has_support_drones = false
	for wave: Dictionary in (_db.engagement["waves"] as Array):
		for group: Dictionary in (wave["groups"] as Array):
			var t := _type_index(str(group["enemy"]))
			if t < 0:
				continue
			if _has_role(t) or (_type_split_into[t] >= 0 and _has_role(_type_split_into[t])):
				_has_support_drones = true
				return

func _has_role(type_index: int) -> bool:
	return _type_repair[type_index] > 0 or _type_jam_ticks[type_index] > 0

func _build_blueprints() -> void:
	_bp_ids = _db.blueprint_ids()
	var n := _bp_ids.size()
	_bp_tier_offset.resize(n)
	_bp_tier_count.resize(n)
	_bp_support_radius_sq.resize(n)
	_bp_support_rate.resize(n)
	_bp_support_damage.resize(n)
	_bp_support_range.resize(n)
	_bp_support_tier_scale.resize(n)
	for b in n:
		# Absent means the family projects nothing, so a weapon written before
		# links existed reads exactly right.
		var support: Dictionary = (_db.blueprints[_bp_ids[b]] as Dictionary).get("support", {})
		var radius := float(support.get("radius_units", 0.0))
		_bp_support_radius_sq[b] = radius * radius
		_bp_support_rate[b] = float(support.get("fire_rate_bonus", 0.0))
		_bp_support_damage[b] = float(support.get("damage_bonus", 0.0))
		_bp_support_range[b] = float(support.get("range_bonus", 0.0))
		_bp_support_tier_scale[b] = float(support.get("tier_scaling", 0.0))
		_max_support_radius = maxf(_max_support_radius, radius)
	var slot := 0
	for b in n:
		var tiers: Array = (_db.blueprints[_bp_ids[b]] as Dictionary)["tiers"]
		_bp_tier_offset[b] = slot
		_bp_tier_count[b] = tiers.size()
		slot += tiers.size()
	_tier_cost.resize(slot)
	_tier_damage.resize(slot)
	_tier_range_sq.resize(slot)
	_tier_interval.resize(slot)
	_tier_proj_speed.resize(slot)
	_tier_hit_radius.resize(slot)
	_tier_proj_life.resize(slot)
	_tier_splash_radius.resize(slot)
	_tier_splash_min.resize(slot)
	_tier_slow_factor.resize(slot)
	_tier_slow_ticks.resize(slot)
	_tier_pierce.resize(slot)
	for b in n:
		var tiers: Array = (_db.blueprints[_bp_ids[b]] as Dictionary)["tiers"]
		for t in tiers.size():
			var td: Dictionary = tiers[t]
			var s := _bp_tier_offset[b] + t
			_tier_cost[s] = int(td["cost"])
			_tier_damage[s] = int(td["damage"])
			var r := float(td["range_units"])
			_tier_range_sq[s] = r * r
			# A fire interval must be at least one tick: the sim cannot fire
			# twice in the same tick, and rounding to zero would mean a turret
			# that never advances its cooldown.
			_tier_interval[s] = maxi(1, int(round(float(td["fire_interval_seconds"]) * float(_tick_rate))))
			_tier_proj_speed[s] = float(td["projectile_speed_units_per_second"]) / float(_tick_rate)
			_tier_hit_radius[s] = float(td["projectile_hit_radius_units"])
			_tier_proj_life[s] = maxi(1, int(round(float(td["projectile_lifetime_seconds"]) * float(_tick_rate))))
			_tier_splash_radius[s] = float(td.get("splash_radius_units", 0.0))
			_tier_splash_min[s] = float(td.get("splash_min_fraction", 1.0))
			_tier_pierce[s] = float(td.get("pierce_width_units", 0.0))
			_tier_slow_factor[s] = float(td.get("slow_factor", 1.0))
			# Seconds in the data file, ticks in the sim - the tick is the only
			# clock in here, and a duration in seconds would drift with tick rate.
			_tier_slow_ticks[s] = int(round(
				float(td.get("slow_duration_seconds", 0.0)) * float(_tick_rate)))

func _build_pools() -> void:
	e_alive.resize(_max_enemies)
	e_gen.resize(_max_enemies)
	e_hp.resize(_max_enemies)
	e_hp_max.resize(_max_enemies)
	e_prog.resize(_max_enemies)
	e_prev_prog.resize(_max_enemies)
	e_offset.resize(_max_enemies)
	e_speed.resize(_max_enemies)
	e_type.resize(_max_enemies)
	e_bounty.resize(_max_enemies)
	e_leak.resize(_max_enemies)
	e_slow_ticks.resize(_max_enemies)
	e_slow_factor.resize(_max_enemies)
	e_route.resize(_max_enemies)
	e_x.resize(_max_enemies)
	e_y.resize(_max_enemies)
	_e_free.resize(_max_enemies)
	# One entry per drone that could die this tick, which cannot exceed the pool.
	_split_type.resize(_max_enemies)
	_split_prog.resize(_max_enemies)
	_split_route.resize(_max_enemies)
	for i in _max_enemies:
		# Free list is filled in reverse so slot 0 is handed out first; makes
		# test expectations and debug output readable.
		_e_free[i] = _max_enemies - 1 - i
	_e_free_top = _max_enemies

	p_alive.resize(_max_projectiles)
	p_x.resize(_max_projectiles)
	p_y.resize(_max_projectiles)
	p_prev_x.resize(_max_projectiles)
	p_prev_y.resize(_max_projectiles)
	p_target.resize(_max_projectiles)
	p_target_gen.resize(_max_projectiles)
	p_damage.resize(_max_projectiles)
	p_speed.resize(_max_projectiles)
	p_hit_radius.resize(_max_projectiles)
	p_life.resize(_max_projectiles)
	p_splash.resize(_max_projectiles)
	p_splash_min.resize(_max_projectiles)
	p_slow_factor.resize(_max_projectiles)
	p_slow_ticks.resize(_max_projectiles)
	p_pierce.resize(_max_projectiles)
	p_from_x.resize(_max_projectiles)
	p_from_y.resize(_max_projectiles)
	p_family.resize(_max_projectiles)
	_p_free.resize(_max_projectiles)
	for i in _max_projectiles:
		_p_free[i] = _max_projectiles - 1 - i
	_p_free_top = _max_projectiles

	t_used.resize(_max_platforms)
	t_blueprint.resize(_max_platforms)
	t_tier.resize(_max_platforms)
	t_aim_x.resize(_max_platforms)
	t_aim_y.resize(_max_platforms)
	t_tier_slot.resize(_max_platforms)
	t_cooldown.resize(_max_platforms)
	t_x.resize(_max_platforms)
	t_y.resize(_max_platforms)
	t_priority.resize(_max_platforms)
	t_disabled.resize(_max_platforms)
	t_rate_mult.resize(_max_platforms)
	t_damage_mult.resize(_max_platforms)
	t_range_mult.resize(_max_platforms)
	t_rate_mult.fill(1.0)
	t_damage_mult.fill(1.0)
	t_range_mult.fill(1.0)
	_family_damage.resize(_bp_ids.size())
	_family_kills.resize(_bp_ids.size())

	var widest := 0
	for wave in (_db.engagement["waves"] as Array):
		widest = maxi(widest, ((wave as Dictionary)["groups"] as Array).size())
	_g_enemy_type.resize(widest)
	_g_remaining.resize(widest)
	_g_interval.resize(widest)
	_g_next_tick.resize(widest)

	var bounds: Dictionary = _db.map["bounds"]
	var cell := float(_db.sim["spatial_hash_cell_size"])
	# The path starts and ends off-screen, so pad the grid past the map bounds
	# rather than assuming enemies stay in frame.
	var grid_margin := cell * float(_db.sim["spatial_hash_margin_cells"])
	_hash = SpatialHash.new(
		-grid_margin, -grid_margin,
		float(bounds["width"]) + grid_margin * 2.0,
		float(bounds["height"]) + grid_margin * 2.0,
		cell, _max_enemies)

# --- command log -------------------------------------------------------------

## Queue a placement. Commands are addressed by tick, never by wall clock, so a
## recorded log replays identically at 1x, 3x or headless.
func queue_place(at_tick: int, x_units: int, y_units: int, blueprint_index: int) -> void:
	_queue(at_tick, CMD_PLACE, x_units, y_units, blueprint_index)

## Upgrade an existing platform one tier. Addressed by platform index, which is
## stable because platforms are only ever appended.
func queue_upgrade(at_tick: int, platform_index: int) -> void:
	_queue(at_tick, CMD_UPGRADE, platform_index, 0, 0)

func queue_sell(at_tick: int, platform_index: int) -> void:
	_queue(at_tick, CMD_SELL, platform_index, 0, 0)

func queue_send_wave(at_tick: int) -> void:
	_queue(at_tick, CMD_SEND_WAVE, 0, 0, 0)

## Re-task a turret. Integer-valued and tick-addressed like every other command,
## so a run that retargets mid-wave replays exactly.
func queue_priority(at_tick: int, platform_index: int, mode: int) -> void:
	_queue(at_tick, CMD_SET_PRIORITY, platform_index, mode, 0)

## Re-create a board carried forward from the previous level in a chain.
##
## Turrets and owned ground persist between acts on the same map; Capital does
## not, because engagement-scoped Capital is what makes each act's spending a
## fresh decision. Carried turrets DO count against the new act's deployment
## limit, so a bigger limit is what buys you room to extend rather than a clean
## slate.
##
## They all arrive, and they all arrive REFITTED - back to tier 1. That is not
## a tax for its own sake - it is the difference between a chain and a cutscene.
## An act that inherits a finished tier-4 board is won by that board with no input
## at all: measured, every single carrying act in the campaign was cleared by an
## idle run, and raising the next act's health by half did not touch it, because
## a tier-4 turret is an order of magnitude past the tier-1 one it grew from.
## Stepping down one tier fixed fourteen of sixteen. The remaining two were
## chased for a while with a cap on how MANY turrets could carry, which worked
## and was wrong: it deleted turrets the player had paid for, and that was
## reported as a bug the first time anyone played it. Refitting to tier 1 does
## the same job by costing tiers instead of emplacements, which is a price paid
## in the currency the game already has.
##
## What survives is what the chain is actually for - your placements, your weapon
## choices, the ground you bought. What comes back is the decision the
## inheritance had removed: what to re-invest in, now that the road is longer than
## the board that held it.
const CARRY_TIER_CAP := 0

## And no more than a share of the new act's deployment limit comes back, read
## from economy.json.
##
## The tier cap bounds how GOOD an inheritance can be; this bounds how BIG. Both
## are needed, and the second only became obvious once the limit started doubling
## every ten levels: at 78 slots, a board carried forward at tier 2 is nearly
## three thousand DPS arriving for free, and eight acts in the middle of the
## campaign went back to being winnable by building nothing. Capping the count
## keeps the inheritance meaningful at any board size instead of at the one size
## it was tuned against, and it always leaves room to rebuild past what you kept.
var _carry_limit_share: float = 0.0
##
## Applied at construction, before any command runs, so it is part of the initial
## state a replay starts from. Anything that no longer fits - a turret whose spot
## the extended corridor now runs through - is dropped and counted rather than
## silently relocated.
func adopt(platforms: Array, owned_cells: PackedInt32Array) -> void:
	for i in range(0, owned_cells.size(), 2):
		var cx := owned_cells[i]
		var cy := owned_cells[i + 1]
		if cell_in_bounds(cx, cy) and cell_is_buildable(cx, cy):
			_cell_unlocked[cy * _grid_cols + cx] = 1
	var ceiling := maxi(1, int(floor(float(_platform_limit) * _carry_limit_share)))
	for entry in platforms:
		if t_count >= ceiling:
			# Everything past the ceiling is stood down, not lost to the corridor -
			# counted separately so the HUD can say which happened.
			_carry_stood_down += 1
			continue
		var record: Dictionary = entry
		var x := float(record["x"])
		var y := float(record["y"])
		var blueprint := int(record["blueprint"])
		# Free: it was paid for in the act it was built in.
		var verdict := _place_without_charge(x, y, blueprint)
		if verdict != BUILD_OK:
			# Two different things, and the player deserves to know which. A turret
			# the extended corridor now runs through is gone; one that simply did
			# not fit under the new act's deployment limit is stood down.
			if verdict == BUILD_AT_LIMIT:
				_carry_stood_down += 1
			else:
				_carry_dropped += 1
			continue
		var index := t_count - 1
		var tier := clampi(mini(int(record["tier"]) - 1, CARRY_TIER_CAP),
			0, _bp_tier_count[blueprint] - 1)
		t_tier[index] = tier
		t_tier_slot[index] = _bp_tier_offset[blueprint] + tier
		# Orders carry even though tiers do not. Refitting a gun is a cost; making
		# the player re-issue every standing order is just tedium.
		t_priority[index] = clampi(int(record.get("priority", TARGET_FIRST)),
			0, target_mode_count() - 1)
	_mark_links_dirty(-1)

## Placement that skips the price but honours every other rule. Only used by
## adopt(); a turret carried forward was already paid for.
func _place_without_charge(x: float, y: float, blueprint_index: int) -> int:
	var held := _capital
	_capital = _tier_cost[_bp_tier_offset[blueprint_index]]
	var verdict := _try_place(x, y, blueprint_index)
	_capital = held
	return verdict

## What a finished board is worth in Capital.
##
## Turrets cannot cross to another board - a coordinate on Highway means nothing
## on Port - but the work that went into them can. This is what the emplacements
## get stripped down to when a chain ends and a new one begins, so the next board
## is opened with what the last one earned rather than from nothing.
func board_salvage() -> int:
	var total := 0
	for i in t_count:
		var blueprint := t_blueprint[i]
		for tier in t_tier[i] + 1:
			total += _tier_cost[_bp_tier_offset[blueprint] + tier]
	return int(floor(float(total) * _salvage_fraction))

## The most salvage this engagement will accept.
##
## A share of its own opening budget, so it scales with the campaign instead of
## being a flat number that is decisive early and irrelevant late. Bounded at all
## because it has to be: measured, a finished board is worth 8,790 Capital
## arriving at an act budgeted for 1,300, and 22,330 at one budgeted for 2,350.
## Unbounded, "continuity" would simply delete the economy from the fifth level
## onward.
## Static because the ceiling that matters at the end of a board belongs to the
## act you are about to open, not the one still on screen. The HUD has to quote
## it there, and quoting a different number than the next act will honour is
## exactly the kind of fiction the honesty rule exists to prevent.
static func salvage_ceiling_of(db: Database) -> int:
	return int(floor(float(db.engagement.get("starting_capital",
		db.economy["starting_capital"]))
		* float(db.economy.get("board_salvage_cap_share", 0.0))))

func salvage_ceiling() -> int:
	return salvage_ceiling_of(_db)

## Fit the modules drafted earlier in this chain.
##
## Applied at construction, before any command runs, like adopt() and
## grant_salvage() - so a module is part of the state a replay starts from rather
## than an event partway through it. Unknown ids are ignored rather than rejected:
## the offer is made by the layer above, and a module removed from the data file
## between two runs must not make a saved chain unplayable.
##
## Effects are read by name and applied to the tables the simulation already had.
## Nothing here is a special case in the tick - a module is a different number in
## a table the tick was already reading, which is why adding one is a data change.
func apply_modules(ids: PackedStringArray) -> void:
	for id in ids:
		if not _db.modules.has(id):
			continue
		var m: Dictionary = _db.modules[id]
		_module_ids.append(id)
		_module_rate += float(m.get("fire_rate", 0.0))
		_module_damage += float(m.get("damage", 0.0))
		_module_range += float(m.get("range", 0.0))
		_capital += int(m.get("capital", 0))
		_interest_cap += int(m.get("interest_cap", 0))
		_sell_refund += float(m.get("sell_refund", 0.0))
		_integrity_max += int(m.get("integrity", 0))
		_integrity += int(m.get("integrity", 0))
		# Clamped at 1.0 rather than at some arbitrary share: upgrade_cost floors at
		# 1 anyway, so a full discount is cheap and not free.
		_upgrade_discount = clampf(_upgrade_discount + float(m.get("upgrade_discount", 0.0)), 0.0, 1.0)
		_jam_resist = clampf(_jam_resist + float(m.get("jam_resist", 0.0)), 0.0, 1.0)
		var reach := float(m.get("support_radius", 0.0))
		if reach > 0.0:
			# Squared, because the table is squared - a 30% longer radius is a 69%
			# larger radius_sq, and applying the raw fraction here would quietly be
			# a much smaller buff than the module claims.
			var growth := (1.0 + reach) * (1.0 + reach)
			for b in _bp_support_radius_sq.size():
				_bp_support_radius_sq[b] *= growth
			_max_support_radius *= (1.0 + reach)
	_mark_links_dirty(-1)

func modules() -> PackedStringArray: return _module_ids
func module_rate_bonus() -> float: return _module_rate
func module_damage_bonus() -> float: return _module_damage
func module_range_bonus() -> float: return _module_range

## Open an engagement on the integrity the last act ended with.
##
## Applied at construction like adopt() and grant_salvage(), so it is part of the
## state a replay starts from. Only ever within a chain: a new board is a new
## contract and starts whole, or a bad run three boards ago would follow you
## forever with no way to recover it.
func inherit_integrity(value: int) -> void:
	if value <= 0:
		return
	_integrity = mini(value, _integrity_max)
	_integrity_inherited = true

func integrity_inherited() -> bool: return _integrity_inherited

## Open an engagement with salvage from the board before it. Applied at
## construction like adopt(), so it is part of the state a replay starts from.
func grant_salvage(amount: int) -> void:
	if amount <= 0:
		return
	_salvage_granted = mini(amount, salvage_ceiling())
	_capital += _salvage_granted

func salvage_granted() -> int: return _salvage_granted

## What to hand to the next act in this chain.
func board_snapshot() -> Dictionary:
	var platforms := []
	for i in t_count:
		platforms.append({"x": t_x[i], "y": t_y[i],
			"blueprint": t_blueprint[i], "tier": t_tier[i],
			"priority": t_priority[i]})
	var cells := PackedInt32Array()
	for cy in _grid_rows:
		for cx in _grid_cols:
			if _cell_unlocked[cy * _grid_cols + cx] == 1:
				cells.append(cx)
				cells.append(cy)
	return {"platforms": platforms, "cells": cells, "salvage": board_salvage(),
		"integrity": _integrity, "modules": _module_ids}

func carry_dropped() -> int: return _carry_dropped
func carry_stood_down() -> int: return _carry_stood_down
## Most turrets an inheritance may put on the board this act.
func carry_ceiling() -> int:
	return maxi(1, int(floor(float(_platform_limit) * _carry_limit_share)))
func interest_paid() -> int: return _interest_paid
func interest_rate() -> float: return _interest_rate
func interest_cap() -> int: return _interest_cap
func sold() -> int: return _sold
func early_calls() -> int: return _early_calls
## Whether a wave can be called early right now, for the HUD.
func can_send_wave() -> bool: return _phase == PHASE_WAITING and _phase_timer > 0
## What calling the next wave in right now would pay.
func send_wave_bonus() -> int:
	if not can_send_wave():
		return 0
	return int(floor(float(_early_wave_bonus) * float(_phase_timer)
		/ float(maxi(_inter_wave_delay, 1))))

## What the next wave is made of, as [[display_name, count], ...] in the order
## the groups arrive.
##
## Pure query. With five drone classes on the board, "what is coming" is the
## difference between planning a board and guessing at one, and it is information
## the wave file already has - withholding it is not difficulty.
func next_wave_preview() -> Array:
	var index := _wave_index + 1 if _phase != PHASE_SPAWNING else _wave_index
	var waves: Array = _db.engagement["waves"]
	if index < 0 or index >= waves.size():
		return []
	var out := []
	for group in ((waves[index] as Dictionary)["groups"] as Array):
		var g: Dictionary = group
		var type_index := _type_index(str(g["enemy"]))
		if type_index < 0:
			continue
		out.append([_type_display[type_index], int(g["count"])])
	return out

## Which wave the preview describes, 1-based.
func next_wave_number() -> int:
	var index := _wave_index + 1 if _phase != PHASE_SPAWNING else _wave_index
	return mini(index + 1, _wave_count)

## Buy one grid cell of buildable ground.
func queue_buy_cell(at_tick: int, cell_x: int, cell_y: int) -> void:
	_queue(at_tick, CMD_BUY_CELL, cell_x, cell_y, 0)

func _queue(at_tick: int, kind: int, a: int, b: int, c: int) -> void:
	# Inserted in tick order rather than appended. The cursor that replays this
	# log only moves forward, so an out-of-order append used to be applied at the
	# wrong tick - silently, and differently between a live run and its replay,
	# which is precisely the failure the determinism work exists to prevent.
	# A command for a tick already past lands on the next one instead of being
	# skipped; that is what a click during the current tick means anyway.
	var at := maxi(at_tick, _tick)
	var position := _cmd_tick.size()
	while position > 0 and _cmd_tick[position - 1] > at:
		position -= 1
	_cmd_tick.insert(position, at)
	_cmd_type.insert(position, kind)
	_cmd_a.insert(position, a)
	_cmd_b.insert(position, b)
	_cmd_c.insert(position, c)

func command_count() -> int:
	return _cmd_tick.size()

func _apply_commands() -> void:
	while _cmd_cursor < _cmd_tick.size() and _cmd_tick[_cmd_cursor] <= _tick:
		var ok := false
		if _cmd_type[_cmd_cursor] == CMD_PLACE:
			ok = _try_place(float(_cmd_a[_cmd_cursor]), float(_cmd_b[_cmd_cursor]), _cmd_c[_cmd_cursor]) == BUILD_OK
		elif _cmd_type[_cmd_cursor] == CMD_UPGRADE:
			ok = _try_upgrade(_cmd_a[_cmd_cursor])
		elif _cmd_type[_cmd_cursor] == CMD_BUY_CELL:
			ok = _try_buy_cell(_cmd_a[_cmd_cursor], _cmd_b[_cmd_cursor])
		elif _cmd_type[_cmd_cursor] == CMD_SELL:
			ok = _try_sell(_cmd_a[_cmd_cursor])
		elif _cmd_type[_cmd_cursor] == CMD_SEND_WAVE:
			ok = _try_send_wave()
		elif _cmd_type[_cmd_cursor] == CMD_SET_PRIORITY:
			ok = _try_set_priority(_cmd_a[_cmd_cursor], _cmd_b[_cmd_cursor])
		if not ok:
			_rejected_commands += 1
		_cmd_cursor += 1

## Shortest distance from a point to the corridor centre line.
##
## Projects onto each segment and clamps, so corners are handled correctly rather
## than by measuring to the nearest waypoint. Uses sqrt only - no trigonometry -
## so it is safe to call from inside the simulation.
func distance_to_path(x: float, y: float) -> float:
	var best := INF
	# Every segment of every route. A board with two roads has to be buildable
	# beside both, and "how close is the nearest road" is the same question
	# whichever road answers it.
	for i in _seg_count:
		var ax := _seg_ax[i]
		var ay := _seg_ay[i]
		var dx := _seg_dx[i]
		var dy := _seg_dy[i]
		var seg_length := _seg_len[i]
		# Projection of (point - a) onto the unit segment direction, clamped to
		# the segment so the nearest point is never past either end.
		var t := (x - ax) * dx + (y - ay) * dy
		if t < 0.0:
			t = 0.0
		elif t > seg_length:
			t = seg_length
		var px := ax + dx * t
		var py := ay + dy * t
		var ox := x - px
		var oy := y - py
		var distance := sqrt(ox * ox + oy * oy)
		if distance < best:
			best = distance
	return best

## Whether a platform may be built at this spot, and if not, which rule stops it.
## Pure query - changes nothing - so the build cursor can call it every frame.
func can_build_at(x: float, y: float, blueprint_index: int) -> int:
	if blueprint_index < 0 or blueprint_index >= _bp_ids.size():
		return BUILD_OUT_OF_BOUNDS
	if x < 0.0 or y < 0.0 or x > _bounds_width or y > _bounds_height:
		return BUILD_OUT_OF_BOUNDS
	var cx := cell_x_of(x)
	var cy := cell_y_of(y)
	if not cell_in_bounds(cx, cy):
		return BUILD_OUT_OF_BOUNDS
	if not cell_is_buildable(cx, cy):
		return BUILD_ON_PATH
	if not cell_is_unlocked(cx, cy):
		# Legal ground, just not owned yet - the player can buy it.
		return BUILD_LOCKED
	var spacing_sq := _build_min_spacing * _build_min_spacing
	for i in t_count:
		var dx := t_x[i] - x
		var dy := t_y[i] - y
		if dx * dx + dy * dy < spacing_sq:
			return BUILD_OVERLAPS
	if t_count >= _platform_limit:
		return BUILD_AT_LIMIT
	if _capital < _tier_cost[_bp_tier_offset[blueprint_index]]:
		return BUILD_NO_CAPITAL
	return BUILD_OK

## Build a platform. Returns a BUILD_* reason; anything but BUILD_OK changes
## nothing. A rejected command is counted, never silently treated as success -
## the count is part of the state hash, so a desync in *what got built* is caught.
func _try_place(x: float, y: float, blueprint_index: int) -> int:
	var verdict := can_build_at(x, y, blueprint_index)
	if verdict != BUILD_OK:
		return verdict
	var slot := _bp_tier_offset[blueprint_index]
	var index := t_count
	t_count += 1
	t_used[index] = 1
	t_blueprint[index] = blueprint_index
	t_tier[index] = 0
	t_tier_slot[index] = slot
	t_cooldown[index] = 0
	t_x[index] = x
	t_y[index] = y
	# Face along the corridor until it has something to shoot at.
	t_aim_x[index] = 1.0
	t_aim_y[index] = 0.0
	# A recycled slot must not inherit the last occupant's orders.
	t_priority[index] = TARGET_FIRST
	t_disabled[index] = 0
	t_rate_mult[index] = 1.0
	t_damage_mult[index] = 1.0
	t_range_mult[index] = 1.0
	_mark_links_dirty(index)
	_capital -= _tier_cost[slot]
	return BUILD_OK

## What selling a turret pays back. A fraction of everything spent on it,
## including upgrades - selling a tier-4 refunds a share of all four tiers, not
## of the last one.
func sell_value(platform_index: int) -> int:
	if platform_index < 0 or platform_index >= t_count:
		return 0
	var blueprint := t_blueprint[platform_index]
	var spent := 0
	for tier in t_tier[platform_index] + 1:
		spent += _tier_cost[_bp_tier_offset[blueprint] + tier]
	return int(floor(float(spent) * _sell_refund))

## Take a turret off the board and refund part of what it cost.
##
## Free placement without an undo is punishing in a way nothing in the design
## intends: a misread of the road costs you the turret AND the ground, and the
## deployment limit means you cannot simply build another. The refund is partial
## so relocating stays a real cost rather than a free retry.
##
## The pool is compacted by moving the last turret into the freed slot, which is
## why platform indices are only meaningful within a tick. Commands are
## tick-addressed and applied in order, so a replay sees the identical sequence.
func _try_sell(platform_index: int) -> bool:
	if platform_index < 0 or platform_index >= t_count:
		return false
	_capital += sell_value(platform_index)
	var last := t_count - 1
	if platform_index != last:
		t_used[platform_index] = t_used[last]
		t_blueprint[platform_index] = t_blueprint[last]
		t_tier[platform_index] = t_tier[last]
		t_tier_slot[platform_index] = t_tier_slot[last]
		t_cooldown[platform_index] = t_cooldown[last]
		t_x[platform_index] = t_x[last]
		t_y[platform_index] = t_y[last]
		t_aim_x[platform_index] = t_aim_x[last]
		t_aim_y[platform_index] = t_aim_y[last]
		t_priority[platform_index] = t_priority[last]
		t_disabled[platform_index] = t_disabled[last]
	t_used[last] = 0
	t_count -= 1
	_sold += 1
	# Selling compacts the pool, so indices move and a partial recompute cannot
	# know whose neighbourhood changed. Full, and rare.
	_mark_links_dirty(-1)
	return true

## Start the next wave now instead of waiting out the gap between waves.
##
## Pays a bounty for the time given up, which is what makes it a decision rather
## than a convenience: the gap is when Capital accumulates and turrets get built,
## so calling a wave early trades preparation for money.
func _try_send_wave() -> bool:
	if _phase != PHASE_WAITING or _phase_timer <= 0:
		return false
	# Scaled by how much of the gap is being skipped, so calling a wave with one
	# tick left does not pay the same as calling it immediately.
	_capital += int(floor(float(_early_wave_bonus) * float(_phase_timer)
		/ float(maxi(_inter_wave_delay, 1))))
	_early_calls += 1
	_phase_timer = 0
	return true

## Cost to take a platform to its next tier, or -1 if it is already at the top.
func upgrade_cost(platform_index: int) -> int:
	if platform_index < 0 or platform_index >= t_count:
		return -1
	var blueprint := t_blueprint[platform_index]
	var next_tier := t_tier[platform_index] + 1
	if next_tier >= _bp_tier_count[blueprint]:
		return -1
	return maxi(1, int(round(float(_tier_cost[_bp_tier_offset[blueprint] + next_tier])
		* (1.0 - _upgrade_discount))))

func can_upgrade(platform_index: int) -> bool:
	var cost := upgrade_cost(platform_index)
	return cost >= 0 and _capital >= cost

## One tier up, paid for out of Capital. Cooldown is deliberately not reset: an
## upgrade should not double as a free instant shot, or upgrading mid-wave would
## be strictly better than upgrading between waves for reasons nobody intended.
func _try_upgrade(platform_index: int) -> bool:
	if not can_upgrade(platform_index):
		return false
	var cost := upgrade_cost(platform_index)
	t_tier[platform_index] += 1
	t_tier_slot[platform_index] = _bp_tier_offset[t_blueprint[platform_index]] + t_tier[platform_index]
	# A tier changes what this turret PROJECTS, not just what it does.
	_mark_links_dirty(platform_index)
	_capital -= cost
	return true

## Re-task one turret. Rejected rather than clamped for an unknown mode or a
## platform that is not there: a command the sim quietly reinterprets is a command
## that means something different on replay.
func _try_set_priority(platform_index: int, mode: int) -> bool:
	if platform_index < 0 or platform_index >= t_count:
		return false
	if mode < 0 or mode >= target_mode_count():
		return false
	if t_priority[platform_index] == mode:
		return false
	t_priority[platform_index] = mode
	return true

## Index of the platform within `radius` of a point, nearest first, or -1.
## Used for click-to-upgrade.
func platform_at(x: float, y: float, radius: float) -> int:
	var best := -1
	var best_distance := radius * radius
	for i in t_count:
		var dx := t_x[i] - x
		var dy := t_y[i] - y
		var distance := dx * dx + dy * dy
		if distance < best_distance:
			best_distance = distance
			best = i
	return best

# --- the tick ----------------------------------------------------------------

func step() -> void:
	if _result != RESULT_RUNNING:
		return
	_apply_commands()
	_refresh_links()
	_advance_enemies()
	_hash.rebuild(e_alive, e_x, e_y, _max_enemies)
	_update_support_drones()
	_update_platforms()
	_advance_projectiles()
	_resolve_splits()
	_update_wave_director()
	_resolve_result()
	_tick += 1

func _advance_enemies() -> void:
	for i in _max_enemies:
		if e_alive[i] == 0:
			continue
		e_prev_prog[i] = e_prog[i]
		var step_distance := e_speed[i]
		if e_slow_ticks[i] > 0:
			step_distance *= e_slow_factor[i]
			e_slow_ticks[i] -= 1
			if e_slow_ticks[i] == 0:
				e_slow_factor[i] = 1.0
		var prog := e_prog[i] + step_distance
		if prog >= _route_length[e_route[i]]:
			# Leak. Integrity is the run's real health bar; this is the only
			# place it ever decreases.
			_integrity -= e_leak[i]
			_leaks += 1
			_despawn_enemy(i)
			continue
		e_prog[i] = prog
		_sample_path(prog, e_offset[i], e_route[i])
		e_x[i] = _out_x
		e_y[i] = _out_y

## Position of a point `prog` units along the path, pushed `offset` units
## perpendicular to the current segment. Writes to _out_x/_out_y instead of
## returning, to keep the hot path allocation-free.
func _sample_path(prog: float, offset: float, route: int = 0) -> void:
	var first := _route_first_seg[route]
	var count := _route_seg_count[route]
	var clamped := prog
	if clamped < 0.0:
		clamped = 0.0
	elif clamped > _route_length[route]:
		clamped = _route_length[route]
	# Binary search the cumulative table. The path has a handful of segments, so
	# this is a few compares and costs less than maintaining a per-enemy cursor.
	var lo := first
	var hi := first + count - 1
	while lo < hi:
		var mid := (lo + hi + 1) >> 1
		if _seg_cum[mid] <= clamped:
			lo = mid
		else:
			hi = mid - 1
	var t := clamped - _seg_cum[lo]
	var dx := _seg_dx[lo]
	var dy := _seg_dy[lo]
	# Perpendicular of a unit vector is (-dy, dx) - no trigonometry needed.
	_out_x = _seg_ax[lo] + dx * t - dy * offset
	_out_y = _seg_ay[lo] + dy * t + dx * offset

## Recompute what every turret is getting from its neighbours.
##
## O(turrets squared) and guarded by a dirty flag, because it can only change when
## something is placed, upgraded or sold. At 144 turrets that is twenty thousand
## compares - nothing once in a while, and far too much thirty times a second.
##
## Order-independent by construction: every contribution is summed and the total
## clamped, so the answer does not depend on which turret the loop happens to
## reach first. That is what makes it safe to leave out of the tick's ordering
## rules entirely.
func _mark_links_dirty(index: int) -> void:
	_links_touched = index if not _links_dirty else -1
	_links_dirty = true

func _refresh_links() -> void:
	if not _links_dirty:
		return
	_links_dirty = false
	if _links_touched < 0:
		for i in t_count:
			_recompute_link(i)
	else:
		# Only the turret that changed and whatever it can reach. Placing or
		# upgrading turret k changes what k projects and what k receives, and
		# nothing else in the board moved - so recomputing all of them is O(n^2)
		# work to produce n-minus-a-handful identical answers.
		#
		# Measured, and not a micro-optimisation: at 208 emplacements the full
		# recompute is 43,264 compares, the scripted policy changes the board a few
		# thousand times an act, and the suite went from under eight minutes to over
		# ten on that alone.
		_recompute_link(_links_touched)
		var reach := _max_support_radius
		for i in t_count:
			if i == _links_touched:
				continue
			var dx := t_x[i] - t_x[_links_touched]
			var dy := t_y[i] - t_y[_links_touched]
			if dx * dx + dy * dy <= reach * reach:
				_recompute_link(i)
	_links_touched = -1
	for i in range(t_count, _max_platforms):
		t_rate_mult[i] = 1.0 + _module_rate
		t_damage_mult[i] = 1.0 + _module_damage
		t_range_mult[i] = 1.0 + _module_range

## What one turret is getting from its neighbours.
##
## Order-independent by construction: every contribution is summed and the total
## clamped, so the answer does not depend on which turret the loop happens to
## reach first. That is what makes it safe to leave out of the tick's ordering
## rules entirely, and what makes the partial recompute above sound.
func _recompute_link(i: int) -> void:
	var rate := 0.0
	var damage := 0.0
	var reach := 0.0
	var mine := t_blueprint[i]
	for j in t_count:
		if j == i:
			continue
		var theirs := t_blueprint[j]
		# The rule the whole mechanic rests on: a family projects onto other
		# families only, so a clustered line of one weapon gets nothing.
		if theirs == mine:
			continue
		var radius_sq := _bp_support_radius_sq[theirs]
		if radius_sq <= 0.0:
			continue
		var dx := t_x[j] - t_x[i]
		var dy := t_y[j] - t_y[i]
		if dx * dx + dy * dy > radius_sq:
			continue
		# Nothing at tier 1, and that is the load-bearing part.
		#
		# It reads as flavour - the coupling hardware arrives with the first refit -
		# and it is really a balance rule. A board carried into the next act arrives
		# refitted to tier 1, so an inheritance projects nothing at all until it is
		# re-invested in. Measured with links live at tier 1, twenty-four inherited
		# turrets cleared the whole of Highway act II with no input: the act became
		# a cutscene. It also gives the player the first reason in the game to
		# upgrade a turret that is not their best one.
		var scale := _bp_support_tier_scale[theirs] * float(t_tier[j])
		if scale <= 0.0:
			continue
		rate += _bp_support_rate[theirs] * scale
		damage += _bp_support_damage[theirs] * scale
		reach += _bp_support_range[theirs] * scale
	# Links are capped; modules are not part of that cap, because they are paid for
	# with a draft rather than with placement, and capping them together would make
	# a module worthless on exactly the well-built board that earned it.
	t_rate_mult[i] = 1.0 + minf(rate, _support_cap_rate) + _module_rate
	t_damage_mult[i] = 1.0 + minf(damage, _support_cap_damage) + _module_damage
	t_range_mult[i] = 1.0 + minf(reach, _support_cap_range) + _module_range

## The pairs the renderer draws, rebuilt on demand rather than kept in step.
##
## Deliberately not maintained by _refresh_links: the drawing is wanted a handful
## of times a second when the board visibly changes, and the multipliers are wanted
## thirty times a second whether anything is on screen or not. Tying the two put an
## O(turrets squared) list build in the simulation's hot path for the benefit of a
## renderer that may not exist.
## `limit` is the caller's drawing budget: past a certain density another line
## communicates nothing, and the number belongs to whoever is doing the drawing
## rather than to the simulation, which computes every multiplier in full whether
## anything is on screen or not.
func rebuild_link_pairs(limit: int) -> void:
	_link_pairs.clear()
	for i in t_count:
		var mine := t_blueprint[i]
		for j in t_count:
			if j == i or t_blueprint[j] == mine:
				continue
			var radius_sq := _bp_support_radius_sq[t_blueprint[j]]
			if radius_sq <= 0.0 or _bp_support_tier_scale[t_blueprint[j]] * float(t_tier[j]) <= 0.0:
				continue
			var dx := t_x[j] - t_x[i]
			var dy := t_y[j] - t_y[i]
			if dx * dx + dy * dy > radius_sq:
				continue
			if _link_pairs.size() >= limit * 2:
				return
			_link_pairs.append(i)
			_link_pairs.append(j)

## Menders and jammers, both of which pulse on a fixed cadence rather than every
## tick.
##
## The cadence is taken from the tick counter rather than from per-drone timers,
## so two menders that spawned four hundred ticks apart still pulse together. That
## is one fewer piece of per-entity state to recycle correctly, and a wave of
## menders pulsing in unison reads better than a continuous mush anyway.
func _update_support_drones() -> void:
	if not _has_support_drones:
		return
	for i in _max_enemies:
		if e_alive[i] == 0:
			continue
		var type_index := e_type[i]
		if _type_repair_now[type_index] > 0 and _tick % _type_repair_interval[type_index] == 0:
			_repair_around(i, type_index)
		if _type_jam_ticks[type_index] > 0 and _tick % _type_jam_interval[type_index] == 0:
			_jam_around(i, type_index)

## Put health back into everything nearby except the mender itself.
##
## Never itself: a drone that outheals your line while also being the toughest
## thing in it is not a puzzle, it is a wall. Walks the hash in cell-then-slot
## order like every other area effect, so the order health is restored in is the
## same on every machine.
func _repair_around(index: int, type_index: int) -> void:
	var radius_sq := _type_repair_radius_sq[type_index]
	if radius_sq <= 0.0:
		return
	var amount := _type_repair_now[type_index]
	var radius := sqrt(radius_sq)
	var min_cx := _hash.cell_x(e_x[index] - radius)
	var max_cx := _hash.cell_x(e_x[index] + radius)
	var min_cy := _hash.cell_y(e_y[index] - radius)
	var max_cy := _hash.cell_y(e_y[index] + radius)
	for cy in range(min_cy, max_cy + 1):
		for cx in range(min_cx, max_cx + 1):
			var begin := _hash.bucket_begin(cx, cy)
			var end := _hash.bucket_end(cx, cy)
			for k in range(begin, end):
				var other := _hash.item_at(k)
				if other == index or e_alive[other] == 0:
					continue
				if e_hp[other] >= e_hp_max[other]:
					continue
				var dx := e_x[other] - e_x[index]
				var dy := e_y[other] - e_y[index]
				if dx * dx + dy * dy > radius_sq:
					continue
				e_hp[other] = mini(e_hp[other] + amount, e_hp_max[other])

## Silence every turret within reach for a while.
##
## Turrets are not in the spatial hash - nothing has ever needed to query them by
## position - and at a deployment limit of 144 a linear scan is cheaper than
## maintaining a second broadphase for one drone class.
func _jam_around(index: int, type_index: int) -> void:
	var radius_sq := _type_jam_radius_sq[type_index]
	if radius_sq <= 0.0:
		return
	var ticks := _type_jam_ticks[type_index]
	for i in t_count:
		var dx := t_x[i] - e_x[index]
		var dy := t_y[i] - e_y[index]
		if dx * dx + dy * dy > radius_sq:
			continue
		# Refreshed, not stacked, exactly like suppression on a drone.
		t_disabled[i] = maxi(t_disabled[i], int(round(float(ticks) * (1.0 - _jam_resist))))

func _update_platforms() -> void:
	for i in t_count:
		if t_used[i] == 0:
			continue
		if t_disabled[i] > 0:
			# The cooldown deliberately does NOT advance while jammed, so a second
			# of silence costs a second of fire rather than being partly absorbed by
			# a cooldown that was going to tick down anyway.
			t_disabled[i] -= 1
			continue
		if t_cooldown[i] > 0:
			t_cooldown[i] -= 1
			continue
		var target := _acquire_target(t_x[i], t_y[i], platform_range_sq(i), t_priority[i])
		if target < 0:
			continue
		_fire(i, target)
		# At least one tick: the sim cannot fire twice in a tick, and a rate bonus
		# that rounded an interval to zero would be a turret that never cools down.
		t_cooldown[i] = maxi(1, int(round(
			float(_tier_interval[t_tier_slot[i]]) / t_rate_mult[i])))

## Pick what this turret shoots. Ties break toward whatever the spatial hash walks
## first (strict >), and that walk is cell-then-slot order, which is stable - so
## ties resolve identically in a replay.
func _acquire_target(px: float, py: float, range_sq: float, mode: int) -> int:
	if mode == TARGET_FIRST:
		return _acquire_first(px, py, range_sq)
	var reach := sqrt(range_sq)
	var min_cx := _hash.cell_x(px - reach)
	var max_cx := _hash.cell_x(px + reach)
	var min_cy := _hash.cell_y(py - reach)
	var max_cy := _hash.cell_y(py + reach)
	var best := -1
	var best_score := -INF
	for cy in range(min_cy, max_cy + 1):
		for cx in range(min_cx, max_cx + 1):
			var begin := _hash.bucket_begin(cx, cy)
			var end := _hash.bucket_end(cx, cy)
			for k in range(begin, end):
				var e := _hash.item_at(k)
				var dx := e_x[e] - px
				var dy := e_y[e] - py
				var distance_sq := dx * dx + dy * dy
				if distance_sq > range_sq:
					continue
				# Every mode is expressed as "biggest score wins" so there is one
				# comparison and one tie-break rule rather than five of each.
				var score := 0.0
				if mode == TARGET_LAST:
					score = -e_prog[e]
				elif mode == TARGET_NEAREST:
					score = -distance_sq
				elif mode == TARGET_TOUGHEST:
					score = float(e_hp[e])
				elif mode == TARGET_WEAKEST:
					score = -float(e_hp[e])
				else:
					score = e_prog[e]
				if score <= best_score:
					continue
				best = e
				best_score = score
	return best

## The default, kept as its own loop because it is the hot one.
##
## Every turret in the campaign's measured balance uses it, and it can reject a
## candidate on a single float compare before touching its position - which the
## general path cannot, since NEAREST needs the distance it would be skipping.
## At 144 turrets against 2,048 drones that early-out is the difference between
## the profile that was measured and a slower one.
func _acquire_first(px: float, py: float, range_sq: float) -> int:
	var reach := sqrt(range_sq)
	var min_cx := _hash.cell_x(px - reach)
	var max_cx := _hash.cell_x(px + reach)
	var min_cy := _hash.cell_y(py - reach)
	var max_cy := _hash.cell_y(py + reach)
	var best := -1
	var best_prog := -1.0
	for cy in range(min_cy, max_cy + 1):
		for cx in range(min_cx, max_cx + 1):
			var begin := _hash.bucket_begin(cx, cy)
			var end := _hash.bucket_end(cx, cy)
			for k in range(begin, end):
				var e := _hash.item_at(k)
				if e_prog[e] <= best_prog:
					continue
				var dx := e_x[e] - px
				var dy := e_y[e] - py
				if dx * dx + dy * dy > range_sq:
					continue
				best = e
				best_prog = e_prog[e]
	return best

## Point the turret at what it is shooting. Called before the pool check so the
## barrel still swings even on the tick a shot is dropped for want of a slot.
func _aim_at(platform: int, target: int) -> void:
	var dx := e_x[target] - t_x[platform]
	var dy := e_y[target] - t_y[platform]
	var distance := sqrt(dx * dx + dy * dy)
	if distance <= 0.0:
		return
	t_aim_x[platform] = dx / distance
	t_aim_y[platform] = dy / distance

func _fire(platform: int, target: int) -> void:
	_aim_at(platform, target)
	if _p_free_top == 0:
		# The shot is lost because the pool is full. Count it rather than
		# dropping it silently: a platform that appears to fire but deals no
		# damage is exactly the kind of quietly-wrong number the honesty rules
		# exist to prevent, and it would read as a balance mystery.
		_projectile_overflow += 1
		return
	_p_free_top -= 1
	var i := _p_free[_p_free_top]
	var slot := t_tier_slot[platform]
	p_alive[i] = 1
	p_x[i] = t_x[platform]
	p_y[i] = t_y[platform]
	p_prev_x[i] = p_x[i]
	p_prev_y[i] = p_y[i]
	p_target[i] = target
	p_target_gen[i] = e_gen[target]
	p_damage[i] = maxi(1, int(round(float(_tier_damage[slot]) * t_damage_mult[platform])))
	p_speed[i] = _tier_proj_speed[slot]
	p_hit_radius[i] = _tier_hit_radius[slot]
	p_life[i] = _tier_proj_life[slot]
	p_splash[i] = _tier_splash_radius[slot]
	p_splash_min[i] = _tier_splash_min[slot]
	p_slow_factor[i] = _tier_slow_factor[slot]
	p_slow_ticks[i] = _tier_slow_ticks[slot]
	p_pierce[i] = _tier_pierce[slot]
	p_from_x[i] = t_x[platform]
	p_from_y[i] = t_y[platform]
	p_family[i] = t_blueprint[platform]
	p_live_count += 1

func _advance_projectiles() -> void:
	for i in _max_projectiles:
		if p_alive[i] == 0:
			continue
		p_prev_x[i] = p_x[i]
		p_prev_y[i] = p_y[i]
		var target := p_target[i]
		# The generation stamp is what stops a shot in flight from landing on a
		# different enemy that inherited the dead one's pool slot. Shots aimed at
		# something that died are simply wasted - that overkill is real, and the
		# balance sim needs to see it.
		if e_alive[target] == 0 or e_gen[target] != p_target_gen[i]:
			_despawn_projectile(i)
			continue
		var dx := e_x[target] - p_x[i]
		var dy := e_y[target] - p_y[i]
		var dist := sqrt(dx * dx + dy * dy)
		var reach := p_speed[i]
		if p_hit_radius[i] > reach:
			reach = p_hit_radius[i]
		if dist <= reach:
			if p_pierce[i] > 0.0:
				_lance_through(p_from_x[i], p_from_y[i], e_x[target], e_y[target],
					p_pierce[i], p_damage[i], p_family[i])
			elif p_splash[i] > 0.0:
				# Suppression lands on everything the blast reaches, which is what
				# makes an area suppressor worth its cost against a wave rather
				# than against one drone.
				_detonate(e_x[target], e_y[target], p_splash[i], p_damage[i],
					p_splash_min[i], p_family[i], p_slow_factor[i], p_slow_ticks[i])
			else:
				_suppress(target, p_slow_factor[i], p_slow_ticks[i])
				_damage_enemy(target, p_damage[i], p_family[i])
			_despawn_projectile(i)
			continue
		var step_scale := p_speed[i] / dist
		p_x[i] += dx * step_scale
		p_y[i] += dy * step_scale
		# Lifetime is spent after moving, so a projectile with N ticks of life
		# actually travels N times. Decrementing first cost it its last tick.
		p_life[i] -= 1
		if p_life[i] <= 0:
			_despawn_projectile(i)

## Area damage centred on a point. Everything inside `radius` is hit, at full
## damage in the middle falling linearly to `min_fraction` at the edge.
##
## Iterates the spatial hash in cell order and, within a cell, in slot order, so
## the sequence of kills - and therefore the order bounties are paid and slots
## are recycled - is identical on every machine. An area attack that resolved in
## an arbitrary order would be a determinism hole that only shows up once
## something explodes near a pool boundary.
func _detonate(x: float, y: float, radius: float, damage: int, min_fraction: float,
		family: int, slow_factor: float = 1.0, slow_ticks: int = 0) -> void:
	var min_cx := _hash.cell_x(x - radius)
	var max_cx := _hash.cell_x(x + radius)
	var min_cy := _hash.cell_y(y - radius)
	var max_cy := _hash.cell_y(y + radius)
	var radius_sq := radius * radius
	for cy in range(min_cy, max_cy + 1):
		for cx in range(min_cx, max_cx + 1):
			var begin := _hash.bucket_begin(cx, cy)
			var end := _hash.bucket_end(cx, cy)
			for k in range(begin, end):
				var e := _hash.item_at(k)
				if e_alive[e] == 0:
					continue
				var dx := e_x[e] - x
				var dy := e_y[e] - y
				var distance_sq := dx * dx + dy * dy
				if distance_sq > radius_sq:
					continue
				var falloff := 1.0 - (sqrt(distance_sq) / radius) * (1.0 - min_fraction)
				# Suppression before damage: applying it first keeps the order
				# identical whether or not this blast kills the drone.
				_suppress(e, slow_factor, slow_ticks)
				# Always at least 1, so a shell that reaches something never does
				# literally nothing - a zero-damage hit reads as a bug.
				_damage_enemy(e, maxi(1, int(round(float(damage) * falloff))), family)

## Drag a drone's speed down for a while.
##
## Refresh, never stack: the strongest slow currently on the drone wins and
## re-arms its timer. Stacking multiplicatively would let a cluster of
## suppressors pin a wave in place indefinitely, which turns one turret into the
## answer to every question and is exactly the degenerate state the deployment
## limit exists to prevent.
func _suppress(index: int, factor: float, ticks: int) -> void:
	if ticks <= 0 or factor >= 1.0:
		return
	# Resistance pulls the multiplier back toward 1.0 - toward "not slowed" -
	# rather than shortening the duration, so a resistant drone is visibly still
	# moving instead of stopping and starting.
	var resist := _type_slow_resist[e_type[index]]
	if resist > 0.0:
		factor = factor + (1.0 - factor) * resist
		if factor >= 1.0:
			return
	if e_slow_ticks[index] > 0 and e_slow_factor[index] < factor:
		# Already under a stronger slow; just re-arm its timer.
		e_slow_ticks[index] = maxi(e_slow_ticks[index], ticks)
		return
	e_slow_factor[index] = factor
	e_slow_ticks[index] = maxi(e_slow_ticks[index], ticks)

## Damage everything within `half_width` of the line the shot travelled.
##
## Full damage the whole way down the lane - a piercing round does not fall off,
## which is what makes it worth its very slow fire rate against a column on a
## straight. Walks the spatial hash in cell order then slot order for the same
## reason `_detonate` does: an attack that resolved against several drones in an
## arbitrary sequence would pay bounties and recycle pool slots in that sequence.
func _lance_through(from_x: float, from_y: float, to_x: float, to_y: float,
		half_width: float, damage: int, family: int) -> void:
	var dx := to_x - from_x
	var dy := to_y - from_y
	var length_sq := dx * dx + dy * dy
	if length_sq <= 0.0:
		return
	var min_cx := _hash.cell_x(minf(from_x, to_x) - half_width)
	var max_cx := _hash.cell_x(maxf(from_x, to_x) + half_width)
	var min_cy := _hash.cell_y(minf(from_y, to_y) - half_width)
	var max_cy := _hash.cell_y(maxf(from_y, to_y) + half_width)
	var width_sq := half_width * half_width
	for cy in range(min_cy, max_cy + 1):
		for cx in range(min_cx, max_cx + 1):
			var begin := _hash.bucket_begin(cx, cy)
			var end := _hash.bucket_end(cx, cy)
			for k in range(begin, end):
				var e := _hash.item_at(k)
				if e_alive[e] == 0:
					continue
				# Distance from the drone to the segment, clamped to its ends so a
				# shot does not reach past where it actually went off.
				var t := ((e_x[e] - from_x) * dx + (e_y[e] - from_y) * dy) / length_sq
				t = clampf(t, 0.0, 1.0)
				var ox := e_x[e] - (from_x + dx * t)
				var oy := e_y[e] - (from_y + dy * t)
				if ox * ox + oy * oy > width_sq:
					continue
				_damage_enemy(e, damage, family)

## Land a hit, crediting the weapon family that fired it.
##
## Armour comes off the hit and not off the drone's health, which is the whole
## point of it: it scales with how many hits you need rather than with how much
## damage you deal, so it punishes a wall of cheap fast guns and barely troubles
## one big one. Never below 1 - see _type_armour.
func _damage_enemy(index: int, amount: int, family: int) -> void:
	var armour := _type_armour_now[e_type[index]]
	if armour > 0:
		# Never below the cap, and never below 1.
		var floor_damage := maxi(1, int(ceil(float(amount) * (1.0 - _armour_max_bite))))
		amount = maxi(amount - armour, floor_damage)
	# Overkill is real and the balance sim needs to see it, but it is not damage
	# the debrief should claim was dealt.
	var landed: int = amount if amount < e_hp[index] else e_hp[index]
	e_hp[index] -= amount
	if family >= 0:
		_family_damage[family] += landed
	if e_hp[index] > 0:
		return
	if family >= 0:
		_family_kills[family] += 1
	_capital += e_bounty[index]
	_kills += 1
	# Queued before the despawn, which frees the slot this reads from.
	if _type_split_into[e_type[index]] >= 0 and _split_pending < _split_type.size():
		_split_type[_split_pending] = e_type[index]
		_split_prog[_split_pending] = e_prog[index]
		_split_route[_split_pending] = e_route[index]
		_split_pending += 1
	_despawn_enemy(index)

## Hatch everything that died owing children.
##
## A child starts where its parent fell, so a brood broken open at the far end of
## the road is a problem at the far end of the road. It inherits nothing else -
## not health, not suppression - because it is a different drone.
func _resolve_splits() -> void:
	for i in _split_pending:
		var parent := _split_type[i]
		for _n in _type_split_count[parent]:
			_spawn_at(_type_split_into[parent], _split_prog[i], _split_route[i])
	_split_pending = 0

# --- wave director -----------------------------------------------------------

func _update_wave_director() -> void:
	match _phase:
		PHASE_WAITING:
			_phase_timer -= 1
			if _phase_timer <= 0:
				_begin_wave(_wave_index + 1)
		PHASE_SPAWNING:
			var pending := false
			for g in _g_count:
				if _g_remaining[g] <= 0:
					continue
				pending = true
				if _tick >= _g_next_tick[g]:
					_spawn(_g_enemy_type[g])
					_g_remaining[g] -= 1
					_g_next_tick[g] = _tick + _g_interval[g]
			if not pending:
				_phase = PHASE_CLEARING
		PHASE_CLEARING:
			# A wave is over when the board is clear, whether that happened by
			# killing everything or by letting it through.
			if e_live_count == 0:
				if _wave_index + 1 >= _wave_count:
					_phase = PHASE_DONE
				else:
					_phase = PHASE_WAITING
					_phase_timer = _inter_wave_delay
		PHASE_DONE:
			pass

func _begin_wave(index: int) -> void:
	# Paid before the wave, on what survived the last one, so the choice it creates
	# is made during the gap - which is when every other build decision is made too.
	# Not on the opening wave: that would just be a bigger starting purse.
	if index > 0 and _interest_rate > 0.0:
		var earned := mini(int(floor(float(_capital) * _interest_rate)), _interest_cap)
		_capital += earned
		_interest_paid += earned
	_wave_index = index
	_phase = PHASE_SPAWNING
	# Exponential scaling, computed by repeated multiplication rather than pow().
	# pow() is not bit-reproducible across libm implementations, and a one-ULP
	# difference in enemy HP is a desync.
	var hp_mult := _act_hp_mult
	var bounty_mult := _act_bounty_mult
	for _i in index:
		hp_mult *= _hp_growth
		bounty_mult *= _bounty_growth
	for t in _type_ids.size():
		_type_hp_now[t] = int(round(float(_type_base_hp[t]) * hp_mult))
		_type_bounty_now[t] = int(round(float(_type_base_bounty[t]) * bounty_mult))
		_type_armour_now[t] = int(round(float(_type_armour[t]) * hp_mult))
		# Healing tracks health, or a mender stops mattering the moment the drones
		# around it are worth more than it can put back.
		_type_repair_now[t] = int(round(float(_type_repair[t]) * hp_mult))
	var groups: Array = ((_db.engagement["waves"] as Array)[index] as Dictionary)["groups"]
	_g_count = groups.size()
	for g in _g_count:
		var group: Dictionary = groups[g]
		var type_index := _type_index(str(group["enemy"]))
		_g_enemy_type[g] = maxi(type_index, 0)
		# Database rejects unknown enemy ids at load, so this is unreachable with
		# validated data. If it ever happens, spawn nothing and count it -
		# quietly substituting enemy type 0 would silently change the wave.
		_g_remaining[g] = 0 if type_index < 0 else int(group["count"])
		if type_index < 0:
			_unknown_enemy_groups += 1
		_g_interval[g] = int(group["spawn_interval_ticks"])
		_g_next_tick[g] = _tick + int(group["start_delay_ticks"])

## -1 when the id is unknown. Never falls back to a real type.
func _type_index(id: String) -> int:
	for i in _type_ids.size():
		if _type_ids[i] == id:
			return i
	return -1

## Send a drone out of the gate, down whichever road is next in the rotation.
##
## The rotation is a flat table of route indices expanded from the map's weights,
## walked by a counter, so it is deterministic and needs no arithmetic at spawn
## time. Deliberately not random: a fork whose traffic split wandered run to run
## would make "how much do I put on the left road" unanswerable.
func _spawn(type_index: int) -> void:
	var route := _route_pick[_spawn_cursor % _route_pick.size()]
	_spawn_cursor += 1
	_spawn_at(type_index, 0.0, route)

## Put a drone on a road at a given distance along it. The wave director always
## passes 0; a brood hatching passes wherever its parent died, on its parent's
## road - the wreck is on the road it was travelling.
func _spawn_at(type_index: int, prog: float, route: int = 0) -> void:
	if _e_free_top == 0:
		# Cannot happen with validated data (Database rejects a wave wider than
		# max_enemies), but if it ever does, count it rather than pretending the
		# enemy existed.
		_spawn_overflow += 1
		return
	_e_free_top -= 1
	var i := _e_free[_e_free_top]
	e_alive[i] = 1
	e_hp[i] = _type_hp_now[type_index]
	e_hp_max[i] = _type_hp_now[type_index]
	e_prog[i] = prog
	e_prev_prog[i] = prog
	# The only use of randomness in P0: a lateral scatter so a column of walkers
	# reads as a crowd instead of one sprite. It goes through the seeded service
	# like everything else, so it is part of what the determinism test proves.
	e_offset[i] = _rng.next_symmetric(_type_jitter[type_index])
	e_speed[i] = _type_speed[type_index]
	e_type[i] = type_index
	e_bounty[i] = _type_bounty_now[type_index]
	e_leak[i] = _type_leak[type_index]
	# A recycled slot must not inherit the last occupant's suppression.
	e_slow_ticks[i] = 0
	e_slow_factor[i] = 1.0
	e_route[i] = clampi(route, 0, _route_count - 1)
	_sample_path(prog, e_offset[i], e_route[i])
	e_x[i] = _out_x
	e_y[i] = _out_y
	e_live_count += 1

func _despawn_enemy(index: int) -> void:
	# Guard, not decoration: despawning an already-dead slot used to push a
	# duplicate onto the free list and write one past its end, corrupting the
	# pool. Reachable from any double-resolve (a leak and a kill on the same
	# tick, or a future retarget path).
	if e_alive[index] == 0:
		return
	e_alive[index] = 0
	# Bump the generation so projectiles already aimed at this slot cannot hit
	# whoever is spawned into it next.
	e_gen[index] += 1
	_e_free[_e_free_top] = index
	_e_free_top += 1
	e_live_count -= 1

func _despawn_projectile(index: int) -> void:
	if p_alive[index] == 0:
		return
	p_alive[index] = 0
	_p_free[_p_free_top] = index
	_p_free_top += 1
	p_live_count -= 1

func _resolve_result() -> void:
	# Loss is checked first: if the last enemy of the last wave leaks and that
	# takes integrity to zero, that is a loss, not a win.
	if _integrity <= 0:
		_integrity = 0
		_result = RESULT_LOSS
		_clear_projectiles()
		return
	if _phase == PHASE_DONE and e_live_count == 0:
		_result = RESULT_WIN
		_clear_projectiles()

## Shots still in the air when the engagement ends have nothing left to hit, and
## step() stops after this, so nothing would ever resolve them - they would sit
## in the pool forever and the "every shot fired was resolved" invariant would be
## quietly false.
##
## It only started mattering when the deployment limit went past a hundred: with
## a couple of dozen turrets, the odds of a shot being mid-flight on the exact
## tick the result resolves are low, and the test passed by luck rather than by
## the invariant holding. With 118 turrets firing it happens nearly every time.
func _clear_projectiles() -> void:
	for i in _max_projectiles:
		if p_alive[i] == 1:
			_despawn_projectile(i)

# --- inspection --------------------------------------------------------------

func tick() -> int: return _tick
func result() -> int: return _result
func is_over() -> bool: return _result != RESULT_RUNNING
func capital() -> int: return _capital
func integrity() -> int: return _integrity
func integrity_max() -> int: return _integrity_max
func kills() -> int: return _kills
func leaks() -> int: return _leaks
func spawn_overflow() -> int: return _spawn_overflow
func projectile_overflow() -> int: return _projectile_overflow
func unknown_enemy_groups() -> int: return _unknown_enemy_groups
func rejected_commands() -> int: return _rejected_commands
func wave_number() -> int: return _wave_index + 1
func wave_count() -> int: return _wave_count
func phase() -> int: return _phase
func tick_rate() -> int: return _tick_rate
func path_length() -> float: return _path_length
func build_min_distance() -> float: return _build_min_dist
func build_max_distance() -> float: return _build_max_dist
func build_min_spacing() -> float: return _build_min_spacing
func platform_limit() -> int: return _platform_limit
func bounds_width() -> float: return _bounds_width
func bounds_height() -> float: return _bounds_height
func platform_tier(i: int) -> int: return t_tier[i]
func platform_blueprint(i: int) -> int: return t_blueprint[i]
func platform_max_tier(blueprint: int) -> int: return _bp_tier_count[blueprint]
## Reach including whatever the neighbours are lending it. Everything that asks
## "how far does this turret shoot" - targeting, the HUD, the range ring under the
## cursor - goes through these, or the ring would promise coverage the turret does
## not have.
func platform_range_sq(i: int) -> float:
	return _tier_range_sq[t_tier_slot[i]] * t_range_mult[i] * t_range_mult[i]
func platform_range(i: int) -> float: return sqrt(platform_range_sq(i))
func platform_rate_bonus(i: int) -> float: return t_rate_mult[i] - 1.0
func platform_damage_bonus(i: int) -> float: return t_damage_mult[i] - 1.0
func platform_range_bonus(i: int) -> float: return t_range_mult[i] - 1.0
## How many other turrets are lending this one anything.
##
## Counted directly rather than read off the renderer's pair list, because that
## list is built on demand and is empty in any run with no renderer attached -
## which is every headless test and every balance probe.
func platform_link_count(i: int) -> int:
	var count := 0
	var mine := t_blueprint[i]
	for j in t_count:
		if j == i or t_blueprint[j] == mine:
			continue
		var radius_sq := _bp_support_radius_sq[t_blueprint[j]]
		if radius_sq <= 0.0:
			continue
		if _bp_support_tier_scale[t_blueprint[j]] * float(t_tier[j]) <= 0.0:
			continue
		var dx := t_x[j] - t_x[i]
		var dy := t_y[j] - t_y[i]
		if dx * dx + dy * dy <= radius_sq:
			count += 1
	return count
func link_count() -> int: return _link_pairs.size() / 2
func link_from(i: int) -> int: return _link_pairs[i * 2]
func link_to(i: int) -> int: return _link_pairs[i * 2 + 1]
func blueprint_support_radius(b: int) -> float: return sqrt(_bp_support_radius_sq[b])
func platform_damage(i: int) -> int:
	return maxi(1, int(round(float(_tier_damage[t_tier_slot[i]]) * t_damage_mult[i])))
## Damage per second at this platform's current tier, for the inspect panel.
## Damage per second as this turret actually fires it, links included. Shown on
## hover, so it has to be the real number and not the catalogue one.
func platform_dps(i: int) -> float:
	var interval := maxi(1, int(round(float(_tier_interval[t_tier_slot[i]]) / t_rate_mult[i])))
	return float(platform_damage(i)) * float(_tick_rate) / float(interval)
func tier_name(blueprint: int, tier: int) -> String:
	var tiers: Array = (_db.blueprints[_bp_ids[blueprint]] as Dictionary)["tiers"]
	return str((tiers[tier] as Dictionary)["name"])
## The board's roads. Route 0 is the one the board was authored around; a board
## with no forks has exactly this one and nothing else changes.
func route_count() -> int: return _route_count
func route_waypoint_count(r: int) -> int: return _route_wp_count[r]
func route_waypoint_x(r: int, i: int) -> float: return _wp_x[_route_first_wp[r] + i]
func route_waypoint_y(r: int, i: int) -> float: return _wp_y[_route_first_wp[r] + i]
func route_length(r: int) -> float: return _route_length[r]
func enemy_route(i: int) -> int: return e_route[i]

func waypoint_count() -> int: return _route_wp_count[0]
## How much of the map's full route this engagement uses.
func revealed_waypoints() -> int: return _route_wp_count[0]
func full_waypoints() -> int: return (_db.map["path"] as Array).size()
## Distance along the path at which waypoint `i` sits.
## Distance along route 0 at which waypoint i sits. The last waypoint has no
## segment of its own, so it answers with the route's whole length - which is what
## "the distance to the exit" means and what every caller wants.
func segment_start_distance(i: int) -> float:
	if i >= _route_seg_count[0]:
		return _route_length[0]
	return _seg_cum[_route_first_seg[0] + i]
func waypoint_x(i: int) -> float: return _wp_x[i]
func waypoint_y(i: int) -> float: return _wp_y[i]
func blueprint_index(id: String) -> int:
	for i in _bp_ids.size():
		if _bp_ids[i] == id:
			return i
	return -1
func blueprint_name(i: int) -> String: return _bp_ids[i]
func blueprint_cost(i: int) -> int: return _tier_cost[_bp_tier_offset[i]]
func blueprint_range(i: int) -> float: return sqrt(_tier_range_sq[_bp_tier_offset[i]])
func blueprint_count() -> int: return _bp_ids.size()
## How many tiers a weapon family has. Exposed so callers reason about the top
## of the ladder without hardcoding four.
func blueprint_tier_count(i: int) -> int: return _bp_tier_count[i]
func blueprint_splash(i: int) -> float: return _tier_splash_radius[_bp_tier_offset[i]]
func blueprint_slow_factor(i: int) -> float: return _tier_slow_factor[_bp_tier_offset[i]]
func blueprint_pierce(i: int) -> float: return _tier_pierce[_bp_tier_offset[i]]
func platform_pierce(i: int) -> float: return _tier_pierce[t_tier_slot[i]]
func platform_slow_factor(i: int) -> float: return _tier_slow_factor[t_tier_slot[i]]
func platform_slow_ticks(i: int) -> int: return _tier_slow_ticks[t_tier_slot[i]]
func platform_splash(i: int) -> float: return _tier_splash_radius[t_tier_slot[i]]
## Public so tests and tools can name an enemy instead of guessing its index -
## the index is alphabetical and shifts whenever a new enemy is added.
func enemy_index(id: String) -> int: return _type_index(id)
func enemy_type_count() -> int: return _type_ids.size()
func enemy_id(i: int) -> String: return _type_ids[i]
func blueprint_display_name(i: int) -> String:
	return str((_db.blueprints[_bp_ids[i]] as Dictionary).get("display_name", _bp_ids[i]))
func enemy_radius(type_index: int) -> float: return _type_radius[type_index]
func enemy_speed(type_index: int) -> float: return _type_speed[type_index]
func enemy_base_hp(type_index: int) -> int: return _type_base_hp[type_index]
func enemy_base_bounty(type_index: int) -> int: return _type_base_bounty[type_index]
func enemy_leak_value(type_index: int) -> int: return _type_leak[type_index]
## Every drone class, in the order their indices run.
func enemy_ids() -> PackedStringArray: return _type_ids
func enemy_armour(type_index: int) -> int: return _type_armour[type_index]
func enemy_splits_into(type_index: int) -> int: return _type_split_into[type_index]
func enemy_split_count(type_index: int) -> int: return _type_split_count[type_index]
func enemy_display_name(type_index: int) -> String: return _type_display[type_index]

func platform_priority(i: int) -> int: return t_priority[i]
func platform_jammed(i: int) -> bool: return t_disabled[i] > 0
func enemy_repair(type_index: int) -> int: return _type_repair_now[type_index]
func enemy_jam_ticks(type_index: int) -> int: return _type_jam_ticks[type_index]
func priority_name(mode: int) -> String:
	return TARGET_MODE_NAMES[clampi(mode, 0, target_mode_count() - 1)]

## Damage landed and drones killed by one weapon family this engagement.
func family_damage(blueprint: int) -> int: return _family_damage[blueprint]
func family_kills(blueprint: int) -> int: return _family_kills[blueprint]
func rng_draws() -> int: return _rng.draws()

## Sample a path position for rendering. Public because the renderer interpolates
## `prog` between ticks and then asks where that lands.
func sample_for_render(prog: float, offset: float, route: int = 0) -> void:
	_sample_path(prog, offset, clampi(route, 0, _route_count - 1))
func out_x() -> float: return _out_x
func out_y() -> float: return _out_y

## Bit-exact fingerprint of the whole simulation.
##
## Covers the pools in full, including recycled slots: stale bytes in a dead slot
## are still a deterministic function of history, so hashing them makes the test
## strictly stricter. Also covers the RNG draw count, so a desync in how many
## random values were consumed is caught even when the values happen to agree.
func state_hash() -> int:
	var h := StateHash.begin()
	h = StateHash.mix_int(h, _tick)
	h = StateHash.mix_int(h, _result)
	h = StateHash.mix_int(h, _capital)
	h = StateHash.mix_int(h, _integrity)
	h = StateHash.mix_int(h, _kills)
	h = StateHash.mix_int(h, _leaks)
	h = StateHash.mix_int(h, _spawn_overflow)
	h = StateHash.mix_int(h, _projectile_overflow)
	h = StateHash.mix_int(h, _unknown_enemy_groups)
	h = StateHash.mix_int(h, _cmd_cursor)
	# The queued-but-unapplied tail of the command log is part of the state: two
	# runs with identical boards and different pending input are not in the same
	# place, and a hash that said they were would let a desync through.
	h = StateHash.mix_bytes(h, _cmd_tick.to_byte_array())
	h = StateHash.mix_bytes(h, _cmd_type.to_byte_array())
	h = StateHash.mix_bytes(h, _cmd_a.to_byte_array())
	h = StateHash.mix_bytes(h, _cmd_b.to_byte_array())
	h = StateHash.mix_bytes(h, e_slow_ticks.to_byte_array())
	h = StateHash.mix_bytes(h, e_slow_factor.to_byte_array())
	h = StateHash.mix_bytes(h, _cmd_c.to_byte_array())
	h = StateHash.mix_int(h, _rejected_commands)
	h = StateHash.mix_int(h, _sold)
	h = StateHash.mix_int(h, _early_calls)
	h = StateHash.mix_int(h, _cells_bought)
	h = StateHash.mix_int(h, _carry_dropped)
	h = StateHash.mix_int(h, _carry_stood_down)
	h = StateHash.mix_int(h, _salvage_granted)
	h = StateHash.mix_int(h, _interest_paid)
	h = StateHash.mix_int(h, 1 if _integrity_inherited else 0)
	h = StateHash.mix_int(h, _module_ids.size())
	for id in _module_ids:
		h = StateHash.mix_bytes(h, id.to_utf8_buffer())
	h = StateHash.mix_bytes(h, _cell_unlocked)
	h = StateHash.mix_int(h, _phase)
	h = StateHash.mix_int(h, _wave_index)
	h = StateHash.mix_int(h, _phase_timer)
	h = StateHash.mix_int(h, e_live_count)
	h = StateHash.mix_int(h, p_live_count)
	h = StateHash.mix_int(h, t_count)
	h = StateHash.mix_int(h, _rng.draws())
	h = StateHash.mix_int(h, _rng.state())
	h = StateHash.mix_bytes(h, e_alive)
	h = StateHash.mix_bytes(h, e_gen.to_byte_array())
	h = StateHash.mix_bytes(h, e_hp.to_byte_array())
	h = StateHash.mix_bytes(h, e_prog.to_byte_array())
	h = StateHash.mix_bytes(h, e_offset.to_byte_array())
	h = StateHash.mix_bytes(h, e_x.to_byte_array())
	h = StateHash.mix_bytes(h, e_y.to_byte_array())
	h = StateHash.mix_bytes(h, e_type.to_byte_array())
	h = StateHash.mix_bytes(h, e_route.to_byte_array())
	h = StateHash.mix_int(h, _spawn_cursor)
	h = StateHash.mix_bytes(h, p_alive)
	h = StateHash.mix_bytes(h, p_x.to_byte_array())
	h = StateHash.mix_bytes(h, p_y.to_byte_array())
	h = StateHash.mix_bytes(h, p_target.to_byte_array())
	h = StateHash.mix_bytes(h, p_target_gen.to_byte_array())
	h = StateHash.mix_bytes(h, p_life.to_byte_array())
	h = StateHash.mix_bytes(h, p_splash.to_byte_array())
	h = StateHash.mix_bytes(h, t_used)
	h = StateHash.mix_bytes(h, t_blueprint.to_byte_array())
	h = StateHash.mix_bytes(h, t_tier.to_byte_array())
	h = StateHash.mix_bytes(h, t_tier_slot.to_byte_array())
	h = StateHash.mix_bytes(h, t_cooldown.to_byte_array())
	h = StateHash.mix_bytes(h, t_x.to_byte_array())
	h = StateHash.mix_bytes(h, t_y.to_byte_array())
	h = StateHash.mix_bytes(h, t_aim_x.to_byte_array())
	h = StateHash.mix_bytes(h, t_aim_y.to_byte_array())
	h = StateHash.mix_bytes(h, t_priority.to_byte_array())
	h = StateHash.mix_bytes(h, t_disabled.to_byte_array())
	h = StateHash.mix_bytes(h, t_rate_mult.to_byte_array())
	h = StateHash.mix_bytes(h, t_damage_mult.to_byte_array())
	h = StateHash.mix_bytes(h, t_range_mult.to_byte_array())
	# Splits owed but not yet hatched are state: two runs that agree on every drone
	# on the board and disagree on what is about to appear are not in the same place.
	h = StateHash.mix_int(h, _split_pending)
	h = StateHash.mix_bytes(h, _split_type.to_byte_array())
	h = StateHash.mix_bytes(h, _split_prog.to_byte_array())
	h = StateHash.mix_bytes(h, _family_damage.to_byte_array())
	h = StateHash.mix_bytes(h, _family_kills.to_byte_array())
	return h
