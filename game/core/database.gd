class_name Database
extends RefCounted

## Loads and validates every balance value in the game.
##
## The rule this class exists to enforce: there are no balance constants in code.
## If a number changes how the game plays, it is read from JSON here, validated
## here, and reaches the simulation only through this object.
##
## Validation is strict but never fatal. A malformed file collects a
## plain-language error and load() returns a Database with is_valid() == false;
## it does not crash and it does not silently substitute a default, because a
## silently-defaulted balance value is a number nobody can explain later.

const DEFAULT_ROOT := "res://data"

var root: String = DEFAULT_ROOT
var sim: Dictionary = {}
var economy: Dictionary = {}
var scaling: Dictionary = {}
var enemies: Dictionary = {}
var blueprints: Dictionary = {}
var map: Dictionary = {}
var engagement: Dictionary = {}
var building: Dictionary = {}

var errors: PackedStringArray = PackedStringArray()

## Every key beginning with "_" is documentation for humans and is skipped by
## the loader, so JSON files can carry the reasoning behind their numbers.
const _DOC_PREFIX := "_"

static func load_engagement(map_id: String, engagement_id: String, data_root: String = DEFAULT_ROOT) -> Database:
	var db := Database.new()
	db.root = data_root
	db._load_all(map_id, engagement_id)
	return db

func is_valid() -> bool:
	return errors.is_empty()

## One newline-joined block, safe to show a player or print in CI.
func error_text() -> String:
	return "\n".join(errors)

func _load_all(map_id: String, engagement_id: String) -> void:
	sim = _read_object("%s/sim.json" % root)
	economy = _read_object("%s/economy.json" % root)
	scaling = _read_object("%s/scaling.json" % root)
	enemies = _read_object("%s/enemies/enemies.json" % root)
	blueprints = _read_object("%s/blueprints/blueprints.json" % root)
	building = _read_object("%s/building.json" % root)
	map = _read_object("%s/maps/%s.json" % [root, map_id])
	engagement = _read_object("%s/waves/%s.json" % [root, engagement_id])
	if not errors.is_empty():
		return
	_validate()

func _read_object(path: String) -> Dictionary:
	if not FileAccess.file_exists(path):
		errors.append("Missing data file: %s" % path)
		return {}
	var text := FileAccess.get_file_as_string(path)
	if text.is_empty():
		errors.append("Data file is empty: %s" % path)
		return {}
	var parser := JSON.new()
	var status := parser.parse(text)
	if status != OK:
		errors.append("%s is not valid JSON (line %d: %s)" % [path, parser.get_error_line(), parser.get_error_message()])
		return {}
	if typeof(parser.data) != TYPE_DICTIONARY:
		errors.append("%s must contain a JSON object at the top level." % path)
		return {}
	return parser.data

# --- validation -------------------------------------------------------------

func _validate() -> void:
	_req_int(sim, "tick_rate_hz", "sim.json", 1)
	_req_int(sim, "max_enemies", "sim.json", 1)
	_req_int(sim, "max_projectiles", "sim.json", 1)
	_req_int(sim, "max_platforms", "sim.json", 1)
	_req_num(sim, "spatial_hash_cell_size", "sim.json", 1.0)
	_req_int(sim, "spatial_hash_margin_cells", "sim.json", 0)

	_req_num(building, "min_distance_from_path", "building.json", 0.0)
	_req_num(building, "max_distance_from_path", "building.json", 0.0)
	_req_num(building, "min_platform_spacing", "building.json", 0.0)
	if float(building.get("max_distance_from_path", 0.0)) <= float(building.get("min_distance_from_path", 0.0)):
		errors.append("building.json: max_distance_from_path must exceed min_distance_from_path, or nowhere is buildable.")

	_req_int(economy, "starting_capital", "economy.json", 0)
	_req_int(economy, "starting_integrity", "economy.json", 1)

	_req_num(scaling, "hp_growth_per_wave", "scaling.json", 0.0001)
	_req_num(scaling, "bounty_growth_per_wave", "scaling.json", 0.0001)

	_validate_enemies()
	_validate_blueprints()
	_validate_map()
	_validate_engagement()

func _validate_enemies() -> void:
	var found := false
	for id in enemies.keys():
		if id.begins_with(_DOC_PREFIX):
			continue
		found = true
		var where := "enemies.json -> %s" % id
		var e: Variant = enemies[id]
		if typeof(e) != TYPE_DICTIONARY:
			errors.append("%s must be an object." % where)
			continue
		_req_int(e, "base_hp", where, 1)
		_req_num(e, "speed_units_per_second", where, 0.0001)
		_req_int(e, "leak_value", where, 0)
		_req_int(e, "base_bounty", where, 0)
		_req_num(e, "radius", where, 0.0001)
		_req_num(e, "spawn_jitter_units", where, 0.0)
	if not found:
		errors.append("enemies.json defines no enemies.")

func _validate_blueprints() -> void:
	var found := false
	for id in blueprints.keys():
		if id.begins_with(_DOC_PREFIX):
			continue
		found = true
		var where := "blueprints.json -> %s" % id
		var b: Variant = blueprints[id]
		if typeof(b) != TYPE_DICTIONARY:
			errors.append("%s must be an object." % where)
			continue
		var tiers: Variant = b.get("tiers")
		if typeof(tiers) != TYPE_ARRAY or (tiers as Array).is_empty():
			errors.append("%s needs a non-empty \"tiers\" array." % where)
			continue
		for i in (tiers as Array).size():
			var t: Variant = (tiers as Array)[i]
			var twhere := "%s tier %d" % [where, i + 1]
			if typeof(t) != TYPE_DICTIONARY:
				errors.append("%s must be an object." % twhere)
				continue
			_req_int(t, "cost", twhere, 0)
			_req_int(t, "damage", twhere, 1)
			_req_num(t, "range_units", twhere, 0.0001)
			_req_num(t, "fire_interval_seconds", twhere, 0.0001)
			_req_num(t, "projectile_speed_units_per_second", twhere, 0.0001)
			_req_num(t, "projectile_hit_radius_units", twhere, 0.0001)
			_req_num(t, "projectile_lifetime_seconds", twhere, 0.0001)
			# Optional: absent means a single-target weapon.
			if (t as Dictionary).has("splash_radius_units"):
				_req_num(t, "splash_radius_units", twhere, 0.0)
				var fraction := _req_num(t, "splash_min_fraction", twhere, 0.0)
				if fraction > 1.0:
					errors.append("%s: splash_min_fraction is a share of full damage and cannot exceed 1." % twhere)
			# Optional: absent means the weapon does not suppress. Both fields are
			# required together, because either one alone is a weapon that either
			# slows for no time or for a while by nothing - silently doing nothing
			# is the failure mode these files exist to prevent.
			if (t as Dictionary).has("slow_factor") or (t as Dictionary).has("slow_duration_seconds"):
				var factor := _req_num(t, "slow_factor", twhere, 0.0)
				_req_num(t, "slow_duration_seconds", twhere, 0.0)
				if factor >= 1.0:
					errors.append("%s: slow_factor multiplies drone speed, so it must be below 1 to slow anything." % twhere)
	if not found:
		errors.append("blueprints.json defines no blueprints.")

func _validate_map() -> void:
	var path: Variant = map.get("path")
	if typeof(path) != TYPE_ARRAY or (path as Array).size() < 2:
		errors.append("Map %s needs a \"path\" of at least 2 waypoints." % str(map.get("id", "?")))
	else:
		var before := errors.size()
		for i in (path as Array).size():
			var p: Variant = (path as Array)[i]
			if typeof(p) != TYPE_DICTIONARY or not (p as Dictionary).has("x") or not (p as Dictionary).has("y"):
				errors.append("Map waypoint %d must be an object with x and y." % i)
				continue
			if i == 0:
				continue
			# Two waypoints in the same place make a zero-length segment, and the
			# sim normalises each segment by its own length. That divides by zero
			# and poisons the direction table with NaN, which then spreads into
			# enemy positions and targeting. Catch it here, where it is one clear
			# message, instead of there, where it is a map full of enemies at
			# coordinates that are not numbers.
			var prev: Dictionary = (path as Array)[i - 1]
			if is_equal_approx(float(prev["x"]), float((p as Dictionary)["x"])) \
					and is_equal_approx(float(prev["y"]), float((p as Dictionary)["y"])):
				errors.append("Map waypoints %d and %d are in the same place; every path segment must have length." % [i - 1, i])
		# Only when every waypoint is a well-formed pair - otherwise this would
		# read fields that are not there and turn a clear message into a crash.
		if errors.size() == before:
			_check_route_crowding(path as Array)
	var bounds: Variant = map.get("bounds")
	if typeof(bounds) != TYPE_DICTIONARY:
		errors.append("Map %s needs \"bounds\" with width and height." % str(map.get("id", "?")))
	else:
		_req_num(bounds, "width", "map bounds", 1.0)
		_req_num(bounds, "height", "map bounds", 1.0)

## Two stretches of road that pass close to each other leave a strip of ground
## that is inside neither's buildable band - dead space that looks like a
## corridor you should be able to defend and is not. Below twice the
## no-build radius the corridor walls themselves overlap and it renders as one
## fused blob. Caught here because it is invisible in the JSON: a map author sees
## a list of coordinates, not the shape they make.
func _check_route_crowding(path: Array) -> void:
	var clearance := float(building.get("min_distance_from_path", 0.0)) * 2.0
	if clearance <= 0.0:
		return
	for i in path.size() - 1:
		for j in range(i + 2, path.size() - 1):
			var gap := _segment_gap(path[i], path[i + 1], path[j], path[j + 1])
			if gap < clearance:
				errors.append(("Map %s: segments %d and %d pass within %.0f units of "
					+ "each other; the road must keep at least %.0f from itself.")
					% [str(map.get("id", "?")), i, j, gap, clearance])
				return  # one clear message beats a wall of them

func _segment_gap(a: Variant, b: Variant, c: Variant, d: Variant) -> float:
	var ax := float((a as Dictionary)["x"]); var ay := float((a as Dictionary)["y"])
	var bx := float((b as Dictionary)["x"]); var by := float((b as Dictionary)["y"])
	var cx := float((c as Dictionary)["x"]); var cy := float((c as Dictionary)["y"])
	var dx := float((d as Dictionary)["x"]); var dy := float((d as Dictionary)["y"])
	# Endpoint-to-segment is exact for the non-crossing case and a safe
	# over-estimate for the crossing one, which a route should never do anyway.
	return minf(
		minf(_point_gap(ax, ay, cx, cy, dx, dy), _point_gap(bx, by, cx, cy, dx, dy)),
		minf(_point_gap(cx, cy, ax, ay, bx, by), _point_gap(dx, dy, ax, ay, bx, by)))

func _point_gap(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
	var vx := bx - ax
	var vy := by - ay
	var length_sq := vx * vx + vy * vy
	var t := 0.0 if length_sq <= 0.0 else clampf(((px - ax) * vx + (py - ay) * vy) / length_sq, 0.0, 1.0)
	var qx := px - (ax + vx * t)
	var qy := py - (ay + vy * t)
	return sqrt(qx * qx + qy * qy)

func _validate_engagement() -> void:
	_req_int(engagement, "inter_wave_delay_ticks", "wave file", 0)
	# Per-engagement overrides. Optional, so an engagement that does not scale
	# reads exactly like one written before acts existed.
	if engagement.has("starting_capital"):
		_req_int(engagement, "starting_capital", "wave file", 0)
	if engagement.has("path_waypoints"):
		var revealed := _req_int(engagement, "path_waypoints", "wave file", 2)
		var available: int = (map.get("path", []) as Array).size()
		if revealed > available:
			errors.append("wave file: path_waypoints is %d but the map only has %d waypoints."
				% [revealed, available])
	if engagement.has("starting_ground_reach"):
		var reach := _req_num(engagement, "starting_ground_reach", "wave file", 0.0)
		if reach <= float(building.get("min_distance_from_path", 0.0)):
			errors.append("wave file: starting_ground_reach must exceed min_distance_from_path, or no ground starts owned.")
	if engagement.has("platform_limit"):
		_req_int(engagement, "platform_limit", "wave file", 1)
	if engagement.has("act_hp_multiplier"):
		_req_num(engagement, "act_hp_multiplier", "wave file", 0.0001)
	if engagement.has("act_bounty_multiplier"):
		_req_num(engagement, "act_bounty_multiplier", "wave file", 0.0001)
	var waves: Variant = engagement.get("waves")
	if typeof(waves) != TYPE_ARRAY or (waves as Array).is_empty():
		errors.append("Wave file %s has no waves." % str(engagement.get("engagement_id", "?")))
		return
	var peak := 0
	for w in (waves as Array).size():
		var wave: Variant = (waves as Array)[w]
		var where := "wave %d" % (w + 1)
		if typeof(wave) != TYPE_DICTIONARY:
			errors.append("%s must be an object." % where)
			continue
		var groups: Variant = (wave as Dictionary).get("groups")
		if typeof(groups) != TYPE_ARRAY or (groups as Array).is_empty():
			errors.append("%s has no groups." % where)
			continue
		var in_wave := 0
		for g in (groups as Array).size():
			var group: Variant = (groups as Array)[g]
			var gwhere := "%s group %d" % [where, g + 1]
			if typeof(group) != TYPE_DICTIONARY:
				errors.append("%s must be an object." % gwhere)
				continue
			var enemy_id := str((group as Dictionary).get("enemy", ""))
			if not enemies.has(enemy_id) or enemy_id.begins_with(_DOC_PREFIX):
				errors.append("%s spawns unknown enemy \"%s\"." % [gwhere, enemy_id])
			var count := _req_int(group, "count", gwhere, 1)
			_req_int(group, "spawn_interval_ticks", gwhere, 1)
			_req_int(group, "start_delay_ticks", gwhere, 0)
			in_wave += count
		peak = maxi(peak, in_wave)
	# A wave that can out-spawn the enemy pool would silently drop enemies, which
	# is exactly the kind of "the numbers are quietly wrong" failure that must not
	# be possible. Catch it at load, not at tick 4000.
	var cap := int(sim.get("max_enemies", 0))
	if peak > cap:
		errors.append("A wave spawns %d enemies but sim.json max_enemies is %d. Raise max_enemies." % [peak, cap])

# --- typed readers ----------------------------------------------------------

func _req_int(d: Variant, key: String, where: String, minimum: int) -> int:
	if typeof(d) != TYPE_DICTIONARY or not (d as Dictionary).has(key):
		errors.append("%s is missing required value \"%s\"." % [where, key])
		return minimum
	var v: Variant = (d as Dictionary)[key]
	if typeof(v) != TYPE_FLOAT and typeof(v) != TYPE_INT:
		errors.append("%s: \"%s\" must be a number." % [where, key])
		return minimum
	var f := float(v)
	if f != floor(f):
		errors.append("%s: \"%s\" must be a whole number, got %s." % [where, key, str(f)])
		return minimum
	var i := int(f)
	if i < minimum:
		errors.append("%s: \"%s\" must be at least %d, got %d." % [where, key, minimum, i])
		return minimum
	return i

func _req_num(d: Variant, key: String, where: String, minimum: float) -> float:
	if typeof(d) != TYPE_DICTIONARY or not (d as Dictionary).has(key):
		errors.append("%s is missing required value \"%s\"." % [where, key])
		return minimum
	var v: Variant = (d as Dictionary)[key]
	if typeof(v) != TYPE_FLOAT and typeof(v) != TYPE_INT:
		errors.append("%s: \"%s\" must be a number." % [where, key])
		return minimum
	var f := float(v)
	if f < minimum:
		errors.append("%s: \"%s\" must be at least %s, got %s." % [where, key, str(minimum), str(f)])
		return minimum
	return f

# --- convenience accessors used by the sim ----------------------------------

func enemy_ids() -> PackedStringArray:
	var out := PackedStringArray()
	for id in enemies.keys():
		if not id.begins_with(_DOC_PREFIX):
			out.append(id)
	out.sort()  # deterministic ordering; Dictionary key order must never leak into the sim
	return out

func blueprint_ids() -> PackedStringArray:
	var out := PackedStringArray()
	for id in blueprints.keys():
		if not id.begins_with(_DOC_PREFIX):
			out.append(id)
	out.sort()
	return out

## The campaign level list. Read separately from an engagement because it names
## which engagements exist, so it cannot be part of loading one.
static func load_levels(data_root: String = DEFAULT_ROOT) -> Array:
	var path := "%s/levels.json" % data_root
	if not FileAccess.file_exists(path):
		return []
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if typeof(parsed) != TYPE_DICTIONARY:
		return []
	var levels: Variant = (parsed as Dictionary).get("levels")
	return levels if typeof(levels) == TYPE_ARRAY else []

func tier_data(blueprint_id: String, tier_index: int) -> Dictionary:
	var b: Dictionary = blueprints.get(blueprint_id, {})
	var tiers: Array = b.get("tiers", [])
	if tier_index < 0 or tier_index >= tiers.size():
		return {}
	return tiers[tier_index]
