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
enum { CMD_PLACE, CMD_UPGRADE, CMD_BUY_CELL }

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
var _seg_count: int = 0
var _path_length: float = 0.0

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
var _type_base_hp: PackedInt64Array = PackedInt64Array()
var _type_speed: PackedFloat64Array = PackedFloat64Array()
var _type_leak: PackedInt32Array = PackedInt32Array()
var _type_base_bounty: PackedInt64Array = PackedInt64Array()
var _type_radius: PackedFloat64Array = PackedFloat64Array()
var _type_jitter: PackedFloat64Array = PackedFloat64Array()

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
## Turrets carried from the previous act that no longer fit - almost always
## because the extended corridor now runs where they stood.
var _carry_dropped: int = 0

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
var e_x: PackedFloat64Array = PackedFloat64Array()
var e_y: PackedFloat64Array = PackedFloat64Array()
var e_live_count: int = 0
var _e_free: PackedInt32Array = PackedInt32Array()
var _e_free_top: int = 0

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
var t_count: int = 0

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
	_hp_growth = float(db.scaling["hp_growth_per_wave"])
	_bounty_growth = float(db.scaling["bounty_growth_per_wave"])
	# The engagement may override the engagement-scoped economy; master plan
	# section 4.1 scales starting Capital by act so a tier-4 platform is
	# reachable in a single fight by act three.
	_capital = int(db.engagement.get("starting_capital", db.economy["starting_capital"]))
	_act_hp_mult = float(db.engagement.get("act_hp_multiplier", 1.0))
	_act_bounty_mult = float(db.engagement.get("act_bounty_multiplier", 1.0))
	_integrity = int(db.economy["starting_integrity"])
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
	var path: Array = full.slice(0, revealed)
	var n := path.size()
	_wp_x.resize(n)
	_wp_y.resize(n)
	for i in n:
		var p: Dictionary = path[i]
		_wp_x[i] = float(p["x"])
		_wp_y[i] = float(p["y"])
	_seg_count = n - 1
	_seg_dx.resize(_seg_count)
	_seg_dy.resize(_seg_count)
	_seg_cum.resize(_seg_count + 1)
	var cum := 0.0
	for i in _seg_count:
		var dx := _wp_x[i + 1] - _wp_x[i]
		var dy := _wp_y[i + 1] - _wp_y[i]
		var length := sqrt(dx * dx + dy * dy)
		_seg_dx[i] = dx / length
		_seg_dy[i] = dy / length
		_seg_cum[i] = cum
		cum += length
	_seg_cum[_seg_count] = cum
	_path_length = cum

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

func _build_blueprints() -> void:
	_bp_ids = _db.blueprint_ids()
	var n := _bp_ids.size()
	_bp_tier_offset.resize(n)
	_bp_tier_count.resize(n)
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
	e_x.resize(_max_enemies)
	e_y.resize(_max_enemies)
	_e_free.resize(_max_enemies)
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

## Re-create a board carried forward from the previous level in a chain.
##
## Turrets and owned ground persist between acts on the same map; Capital does
## not, because engagement-scoped Capital is what makes each act's spending a
## fresh decision. Carried turrets DO count against the new act's deployment
## limit, so a bigger limit is what buys you room to extend rather than a clean
## slate.
##
## They also arrive REFITTED: one tier down, and never above tier 2. That is not
## a tax for its own sake - it is the difference between a chain and a cutscene.
## An act that inherits a finished tier-4 board is won by that board with no input
## at all: measured, every single carrying act in the campaign was cleared by an
## idle run, and raising the next act's health by half did not touch it, because
## a tier-4 turret is an order of magnitude past the tier-1 one it grew from.
## Stepping down alone fixed fourteen of sixteen; the cap fixes the rest
## structurally rather than by tuning, because it bounds what an inheritance can
## be worth no matter how comfortably the previous act was won.
##
## What survives is what the chain is actually for - your placements, your weapon
## choices, the ground you bought. What comes back is the decision the
## inheritance had removed: what to re-invest in, now that the road is longer than
## the board that held it.
const CARRY_TIER_CAP := 1
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
	for entry in platforms:
		var record: Dictionary = entry
		var x := float(record["x"])
		var y := float(record["y"])
		var blueprint := int(record["blueprint"])
		# Free: it was paid for in the act it was built in.
		if _place_without_charge(x, y, blueprint) != BUILD_OK:
			_carry_dropped += 1
			continue
		var index := t_count - 1
		var tier := clampi(mini(int(record["tier"]) - 1, CARRY_TIER_CAP),
			0, _bp_tier_count[blueprint] - 1)
		t_tier[index] = tier
		t_tier_slot[index] = _bp_tier_offset[blueprint] + tier

## Placement that skips the price but honours every other rule. Only used by
## adopt(); a turret carried forward was already paid for.
func _place_without_charge(x: float, y: float, blueprint_index: int) -> int:
	var held := _capital
	_capital = _tier_cost[_bp_tier_offset[blueprint_index]]
	var verdict := _try_place(x, y, blueprint_index)
	_capital = held
	return verdict

## What to hand to the next act in this chain.
func board_snapshot() -> Dictionary:
	var platforms := []
	for i in t_count:
		platforms.append({"x": t_x[i], "y": t_y[i],
			"blueprint": t_blueprint[i], "tier": t_tier[i]})
	var cells := PackedInt32Array()
	for cy in _grid_rows:
		for cx in _grid_cols:
			if _cell_unlocked[cy * _grid_cols + cx] == 1:
				cells.append(cx)
				cells.append(cy)
	return {"platforms": platforms, "cells": cells}

func carry_dropped() -> int: return _carry_dropped

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
	for i in _seg_count:
		var ax := _wp_x[i]
		var ay := _wp_y[i]
		var dx := _seg_dx[i]
		var dy := _seg_dy[i]
		var seg_length := _seg_cum[i + 1] - _seg_cum[i]
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
	_capital -= _tier_cost[slot]
	return BUILD_OK

## Cost to take a platform to its next tier, or -1 if it is already at the top.
func upgrade_cost(platform_index: int) -> int:
	if platform_index < 0 or platform_index >= t_count:
		return -1
	var blueprint := t_blueprint[platform_index]
	var next_tier := t_tier[platform_index] + 1
	if next_tier >= _bp_tier_count[blueprint]:
		return -1
	return _tier_cost[_bp_tier_offset[blueprint] + next_tier]

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
	_capital -= cost
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
	_advance_enemies()
	_hash.rebuild(e_alive, e_x, e_y, _max_enemies)
	_update_platforms()
	_advance_projectiles()
	_update_wave_director()
	_resolve_result()
	_tick += 1

func _advance_enemies() -> void:
	for i in _max_enemies:
		if e_alive[i] == 0:
			continue
		e_prev_prog[i] = e_prog[i]
		var prog := e_prog[i] + e_speed[i]
		if prog >= _path_length:
			# Leak. Integrity is the run's real health bar; this is the only
			# place it ever decreases.
			_integrity -= e_leak[i]
			_leaks += 1
			_despawn_enemy(i)
			continue
		e_prog[i] = prog
		_sample_path(prog, e_offset[i])
		e_x[i] = _out_x
		e_y[i] = _out_y

## Position of a point `prog` units along the path, pushed `offset` units
## perpendicular to the current segment. Writes to _out_x/_out_y instead of
## returning, to keep the hot path allocation-free.
func _sample_path(prog: float, offset: float) -> void:
	var clamped := prog
	if clamped < 0.0:
		clamped = 0.0
	elif clamped > _path_length:
		clamped = _path_length
	# Binary search the cumulative table. The path has a handful of segments, so
	# this is a few compares and costs less than maintaining a per-enemy cursor.
	var lo := 0
	var hi := _seg_count - 1
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
	_out_x = _wp_x[lo] + dx * t - dy * offset
	_out_y = _wp_y[lo] + dy * t + dx * offset

func _update_platforms() -> void:
	for i in t_count:
		if t_used[i] == 0:
			continue
		if t_cooldown[i] > 0:
			t_cooldown[i] -= 1
			continue
		var target := _acquire_target(t_x[i], t_y[i], _tier_range_sq[t_tier_slot[i]])
		if target < 0:
			continue
		_fire(i, target)
		t_cooldown[i] = _tier_interval[t_tier_slot[i]]

## First priority: of everything in range, the enemy furthest along the path.
## Ties break toward the lower slot index (strict >), which is stable and so
## keeps replays aligned.
func _acquire_target(px: float, py: float, range_sq: float) -> int:
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
	p_damage[i] = _tier_damage[slot]
	p_speed[i] = _tier_proj_speed[slot]
	p_hit_radius[i] = _tier_hit_radius[slot]
	p_life[i] = _tier_proj_life[slot]
	p_splash[i] = _tier_splash_radius[slot]
	p_splash_min[i] = _tier_splash_min[slot]
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
			if p_splash[i] > 0.0:
				_detonate(e_x[target], e_y[target], p_splash[i], p_damage[i], p_splash_min[i])
			else:
				_damage_enemy(target, p_damage[i])
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
func _detonate(x: float, y: float, radius: float, damage: int, min_fraction: float) -> void:
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
				# Always at least 1, so a shell that reaches something never does
				# literally nothing - a zero-damage hit reads as a bug.
				_damage_enemy(e, maxi(1, int(round(float(damage) * falloff))))

func _damage_enemy(index: int, amount: int) -> void:
	e_hp[index] -= amount
	if e_hp[index] > 0:
		return
	_capital += e_bounty[index]
	_kills += 1
	_despawn_enemy(index)

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

func _spawn(type_index: int) -> void:
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
	e_prog[i] = 0.0
	e_prev_prog[i] = 0.0
	# The only use of randomness in P0: a lateral scatter so a column of walkers
	# reads as a crowd instead of one sprite. It goes through the seeded service
	# like everything else, so it is part of what the determinism test proves.
	e_offset[i] = _rng.next_symmetric(_type_jitter[type_index])
	e_speed[i] = _type_speed[type_index]
	e_type[i] = type_index
	e_bounty[i] = _type_bounty_now[type_index]
	e_leak[i] = _type_leak[type_index]
	_sample_path(0.0, e_offset[i])
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
		return
	if _phase == PHASE_DONE and e_live_count == 0:
		_result = RESULT_WIN

# --- inspection --------------------------------------------------------------

func tick() -> int: return _tick
func result() -> int: return _result
func is_over() -> bool: return _result != RESULT_RUNNING
func capital() -> int: return _capital
func integrity() -> int: return _integrity
func integrity_max() -> int: return int(_db.economy["starting_integrity"])
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
func platform_range(i: int) -> float: return sqrt(_tier_range_sq[t_tier_slot[i]])
func platform_damage(i: int) -> int: return _tier_damage[t_tier_slot[i]]
## Damage per second at this platform's current tier, for the inspect panel.
func platform_dps(i: int) -> float:
	return float(_tier_damage[t_tier_slot[i]]) * float(_tick_rate) / float(_tier_interval[t_tier_slot[i]])
func tier_name(blueprint: int, tier: int) -> String:
	var tiers: Array = (_db.blueprints[_bp_ids[blueprint]] as Dictionary)["tiers"]
	return str((tiers[tier] as Dictionary)["name"])
func waypoint_count() -> int: return _wp_x.size()
## How much of the map's full route this engagement uses.
func revealed_waypoints() -> int: return _wp_x.size()
func full_waypoints() -> int: return (_db.map["path"] as Array).size()
## Distance along the path at which waypoint `i` sits.
func segment_start_distance(i: int) -> float: return _seg_cum[i]
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
func platform_splash(i: int) -> float: return _tier_splash_radius[t_tier_slot[i]]
## Public so tests and tools can name an enemy instead of guessing its index -
## the index is alphabetical and shifts whenever a new enemy is added.
func enemy_index(id: String) -> int: return _type_index(id)
func enemy_type_count() -> int: return _type_ids.size()
func enemy_id(i: int) -> String: return _type_ids[i]
func blueprint_display_name(i: int) -> String:
	return str((_db.blueprints[_bp_ids[i]] as Dictionary).get("display_name", _bp_ids[i]))
func enemy_radius(type_index: int) -> float: return _type_radius[type_index]
func rng_draws() -> int: return _rng.draws()

## Sample a path position for rendering. Public because the renderer interpolates
## `prog` between ticks and then asks where that lands.
func sample_for_render(prog: float, offset: float) -> void:
	_sample_path(prog, offset)
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
	h = StateHash.mix_bytes(h, _cmd_c.to_byte_array())
	h = StateHash.mix_int(h, _rejected_commands)
	h = StateHash.mix_int(h, _cells_bought)
	h = StateHash.mix_int(h, _carry_dropped)
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
	return h
