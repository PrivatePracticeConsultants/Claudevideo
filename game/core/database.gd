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
	if not found:
		errors.append("blueprints.json defines no blueprints.")

func _validate_map() -> void:
	var path: Variant = map.get("path")
	if typeof(path) != TYPE_ARRAY or (path as Array).size() < 2:
		errors.append("Map %s needs a \"path\" of at least 2 waypoints." % str(map.get("id", "?")))
	else:
		for i in (path as Array).size():
			var p: Variant = (path as Array)[i]
			if typeof(p) != TYPE_DICTIONARY or not (p as Dictionary).has("x") or not (p as Dictionary).has("y"):
				errors.append("Map waypoint %d must be an object with x and y." % i)
	var pads: Variant = map.get("pads")
	if typeof(pads) != TYPE_ARRAY or (pads as Array).is_empty():
		errors.append("Map %s needs at least one tower pad." % str(map.get("id", "?")))
	else:
		var seen := {}
		for i in (pads as Array).size():
			var pad: Variant = (pads as Array)[i]
			if typeof(pad) != TYPE_DICTIONARY or not (pad as Dictionary).has("x") or not (pad as Dictionary).has("y"):
				errors.append("Map pad %d must be an object with x and y." % i)
				continue
			var pid := str((pad as Dictionary).get("id", "pad_%d" % i))
			if seen.has(pid):
				errors.append("Map has two pads with id \"%s\"; pad ids must be unique." % pid)
			seen[pid] = true
	var bounds: Variant = map.get("bounds")
	if typeof(bounds) != TYPE_DICTIONARY:
		errors.append("Map %s needs \"bounds\" with width and height." % str(map.get("id", "?")))
	else:
		_req_num(bounds, "width", "map bounds", 1.0)
		_req_num(bounds, "height", "map bounds", 1.0)

func _validate_engagement() -> void:
	_req_int(engagement, "inter_wave_delay_ticks", "wave file", 0)
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

func tier_data(blueprint_id: String, tier_index: int) -> Dictionary:
	var b: Dictionary = blueprints.get(blueprint_id, {})
	var tiers: Array = b.get("tiers", [])
	if tier_index < 0 or tier_index >= tiers.size():
		return {}
	return tiers[tier_index]
