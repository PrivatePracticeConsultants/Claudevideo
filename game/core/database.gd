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
var modules: Dictionary = {}
var damage: Dictionary = {}
var affixes: Dictionary = {}

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
	modules = _read_object("%s/modules/modules.json" % root)
	damage = _read_object("%s/damage.json" % root)
	affixes = _read_object("%s/affixes/affixes.json" % root)
	map = _read_object("%s/maps/%s.json" % [root, map_id])
	engagement = _read_object("%s/waves/%s.json" % [root, engagement_id])
	if not errors.is_empty():
		return
	# Before validation, so the expanded waves are validated like authored ones -
	# a generator that produced a malformed wave would otherwise sail through.
	_expand_endless()
	_validate()

## Turn an "endless" block into a concrete list of waves.
##
## An endless siege is a wave LIST like any other; the only difference is that
## nobody typed it. Expanding it here, once, at load, means the tick knows
## nothing about endless mode - _begin_wave still reads engagement["waves"][i],
## wave_count() still answers, the debrief still works, and a replay of an
## endless run is a replay of a fixed wave list. A generator running inside the
## tick would have been a second source of truth about what wave 40 contains.
##
## Deterministic by construction: the shape of wave N is a function of N and the
## data, with no randomness anywhere. Two players on wave 40 face the same wave.
func _expand_endless() -> void:
	var block: Variant = engagement.get("endless")
	if typeof(block) != TYPE_DICTIONARY:
		return
	var spec: Dictionary = block
	var archetypes: Variant = spec.get("archetypes", [])
	if typeof(archetypes) != TYPE_ARRAY or (archetypes as Array).is_empty():
		errors.append("waves/%s: endless needs at least one archetype."
			% str(engagement.get("engagement_id", "?")))
		return
	var total := maxi(int(spec.get("waves", 0)), 1)
	var boss_every := maxi(int(spec.get("boss_every", 0)), 0)
	var bosses: Array = spec.get("bosses", [])
	var growth := float(spec.get("count_growth", 1.0))
	var interval_floor := maxi(int(spec.get("spawn_interval_floor", 1)), 1)
	var interval_decay := float(spec.get("spawn_interval_decay", 1.0))

	var waves: Array = []
	for index in total:
		# Archetypes cycle rather than being drawn: "wave 12 is always a Rush" is
		# something a player can learn and plan against, where a shuffled order is
		# only ever survived.
		var archetype: Dictionary = (archetypes as Array)[index % (archetypes as Array).size()]
		var groups: Array = []
		# Counts and spawn intervals ramp by repeated multiplication rather than
		# pow(), matching the simulation's own scaling for the same reason: pow()
		# is not bit-reproducible across libm and this feeds enemy counts.
		var count_mult := 1.0
		var interval_mult := 1.0
		for _i in index:
			count_mult *= growth
			interval_mult *= interval_decay
		for entry: Variant in (archetype.get("groups", []) as Array):
			var group: Dictionary = entry
			groups.append({
				"enemy": str(group.get("enemy", "")),
				"count": maxi(1, int(round(float(group.get("count", 1)) * count_mult))),
				"spawn_interval_ticks": maxi(interval_floor,
					int(round(float(group.get("spawn_interval_ticks", 30)) * interval_mult))),
				"start_delay_ticks": int(group.get("start_delay_ticks", 0)),
			})
		# A boss ON TOP of the wave, not instead of it. A boss alone is a damage
		# check with nothing to protect it; a boss walking in behind its own wave
		# is the thing that makes the wave hard.
		var boss_index := -1
		if boss_every > 0 and not bosses.is_empty() and (index + 1) % boss_every == 0:
			boss_index = ((index + 1) / boss_every - 1) % bosses.size()
			var boss: Dictionary = bosses[boss_index]
			groups.append({
				"enemy": str(boss.get("enemy", "")),
				"count": maxi(1, int(boss.get("count", 1))),
				"spawn_interval_ticks": maxi(1, int(boss.get("spawn_interval_ticks", 60))),
				"start_delay_ticks": int(boss.get("start_delay_ticks", 0)),
			})
		waves.append({
			"groups": groups,
			"name": str(archetype.get("name", "ASSAULT")),
			"boss": boss_index >= 0,
		})
	engagement["waves"] = waves

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
	# Required rather than defaulted, because the alternative is a balance value
	# living as a literal in sim.gd, which the purity linter rightly refuses.
	_req_num(scaling, "armour_max_bite", "scaling.json", 0.0)
	_req_int(scaling, "champion_every", "scaling.json", 2)
	_req_num(scaling, "champion_hp_mult", "scaling.json", 1.0)
	_req_num(scaling, "champion_bounty_mult", "scaling.json", 1.0)
	_req_int(economy, "overcharge_cost", "economy.json", 0)
	_req_num(economy, "overcharge_duration_seconds", "economy.json", 0.0001)
	_req_num(economy, "overcharge_cooldown_seconds", "economy.json", 0.0001)
	_req_num(economy, "overcharge_rate_mult", "economy.json", 1.0)
	_req_num(economy, "overcharge_damage_mult", "economy.json", 1.0)
	_req_num(economy, "interest_per_wave", "economy.json", 0.0)
	_req_int(economy, "interest_cap", "economy.json", 0)
	_req_num(economy, "support_cap_fire_rate", "economy.json", 0.0)
	_req_num(economy, "support_cap_damage", "economy.json", 0.0)
	_req_num(economy, "support_cap_range", "economy.json", 0.0)

	if scaling.has("veteran_kills"):
		var ranks: Variant = scaling["veteran_kills"]
		if typeof(ranks) != TYPE_ARRAY or (ranks as Array).is_empty():
			errors.append("scaling.json: veteran_kills must be a non-empty ascending list of kill counts.")
		else:
			var last := 0
			for r: Variant in (ranks as Array):
				if int(r) <= last:
					errors.append("scaling.json: veteran_kills must strictly ascend.")
					break
				last = int(r)
		_req_num(scaling, "veteran_damage_per_rank", "scaling.json", 0.0001)
	_validate_doctrines()
	_validate_premium()
	_validate_modules()
	_validate_damage()
	_validate_affixes()
	_validate_enemies()
	_validate_blueprints()
	_validate_map()
	_validate_routes()
	_validate_engagement()

## Every module must actually do something, and only things the simulation knows
## how to apply. A module with a typo'd effect key would load, offer, be picked,
## and change nothing - the exact silent-nothing failure this file exists for.
const MODULE_EFFECTS := ["fire_rate", "damage", "range", "support_radius",
	"capital", "interest_cap", "sell_refund", "integrity", "jam_resist",
	"upgrade_discount"]

func _validate_modules() -> void:
	var found := false
	for id in modules.keys():
		if id.begins_with(_DOC_PREFIX):
			continue
		found = true
		var where := "modules.json -> %s" % id
		var m: Variant = modules[id]
		if typeof(m) != TYPE_DICTIONARY:
			errors.append("%s must be an object." % where)
			continue
		if str((m as Dictionary).get("display_name", "")).is_empty():
			errors.append("%s needs a display_name - it is offered by name." % where)
		if str((m as Dictionary).get("description", "")).is_empty():
			errors.append("%s needs a description - a choice you cannot read is not a choice." % where)
		var effects := 0
		for key in (m as Dictionary).keys():
			if key == "display_name" or key == "description" or key.begins_with(_DOC_PREFIX):
				continue
			if not MODULE_EFFECTS.has(key):
				errors.append("%s has unknown effect \"%s\". Known: %s"
					% [where, key, ", ".join(MODULE_EFFECTS)])
				continue
			_req_num(m, key, where, 0.0001)
			effects += 1
		if effects == 0:
			errors.append("%s does nothing." % where)
	if not found:
		errors.append("modules.json defines no modules.")

## Every affix must move a number the simulation actually reads, and only those.
## An affix with a typo'd key would be listed on an act, announced in the HUD,
## and change nothing - a lie on screen, which is worse than a crash.
const AFFIX_EFFECTS := ["armour_bonus", "speed_multiplier", "hp_multiplier",
	"bounty_multiplier", "wave_delay_multiplier", "slow_resistance_bonus",
	"count_multiplier"]

## The damage matrix has to be COMPLETE. A missing pair would silently read as
## neutral, and "this gun is neutral against that drone" is a design decision
## nobody would have made on purpose.
func _validate_damage() -> void:
	var classes := _ids_of(damage.get("classes", {}))
	var types := _ids_of(damage.get("types", {}))
	if classes.is_empty():
		errors.append("damage.json defines no armour classes.")
	if types.is_empty():
		errors.append("damage.json defines no damage types.")
	for c in classes:
		var body: Variant = (damage["classes"] as Dictionary)[c]
		if typeof(body) != TYPE_DICTIONARY or str((body as Dictionary).get("display_name", "")).is_empty():
			errors.append("damage.json -> classes -> %s needs a display_name - the HUD names it." % c)
	for t in types:
		var where := "damage.json -> types -> %s" % t
		var row: Variant = (damage["types"] as Dictionary)[t]
		if typeof(row) != TYPE_DICTIONARY:
			errors.append("%s must be an object." % where)
			continue
		if str((row as Dictionary).get("display_name", "")).is_empty():
			errors.append("%s needs a display_name - the HUD names it." % where)
		for c in classes:
			if not (row as Dictionary).has(c):
				errors.append("%s has no multiplier against \"%s\". Every pair must be stated." % [where, c])
				continue
			_req_num(row as Dictionary, c, where, 0.0)
		for key in (row as Dictionary).keys():
			if key == "display_name" or key.begins_with(_DOC_PREFIX):
				continue
			if not classes.has(key):
				errors.append("%s names armour class \"%s\", which does not exist." % [where, key])

func _validate_affixes() -> void:
	for id in affixes.keys():
		if id.begins_with(_DOC_PREFIX):
			continue
		var where := "affixes.json -> %s" % id
		var a: Variant = affixes[id]
		if typeof(a) != TYPE_DICTIONARY:
			errors.append("%s must be an object." % where)
			continue
		if str((a as Dictionary).get("display_name", "")).is_empty():
			errors.append("%s needs a display_name - it is announced by name." % where)
		if str((a as Dictionary).get("blurb", "")).is_empty():
			errors.append("%s needs a blurb - an announced modifier nobody can read is a surprise." % where)
		var effects := 0
		for key in (a as Dictionary).keys():
			if key == "display_name" or key == "blurb" or key.begins_with(_DOC_PREFIX):
				continue
			if not AFFIX_EFFECTS.has(key):
				errors.append("%s has unknown effect \"%s\". Known: %s"
					% [where, key, ", ".join(AFFIX_EFFECTS)])
				continue
			_req_num(a as Dictionary, key, where, 0.0)
			effects += 1
		if effects == 0:
			errors.append("%s does nothing." % where)

func _ids_of(source: Variant) -> PackedStringArray:
	var out := PackedStringArray()
	if typeof(source) != TYPE_DICTIONARY:
		return out
	for id in (source as Dictionary).keys():
		if not id.begins_with(_DOC_PREFIX):
			out.append(id)
	return out

## A doctrine is a permanent either/or at the top tier, so both halves must exist,
## be named, and do something the simulation knows how to apply.
const DOCTRINE_EFFECTS := ["fire_rate", "damage", "range", "splash_radius",
	"slow_power", "armour_ignore"]

func _validate_doctrines() -> void:
	for id in blueprints.keys():
		if id.begins_with(_DOC_PREFIX):
			continue
		var b: Variant = blueprints[id]
		if typeof(b) != TYPE_DICTIONARY or not (b as Dictionary).has("doctrines"):
			continue
		var where := "blueprints.json -> %s doctrines" % id
		var pair: Variant = (b as Dictionary)["doctrines"]
		if typeof(pair) != TYPE_ARRAY or (pair as Array).size() != 2:
			errors.append("%s must be exactly two options - an either/or, not a menu." % where)
			continue
		for option: Variant in (pair as Array):
			if typeof(option) != TYPE_DICTIONARY:
				errors.append("%s options must be objects." % where)
				continue
			var o := option as Dictionary
			if str(o.get("display_name", "")).is_empty():
				errors.append("%s option needs a display_name - it is chosen by name." % where)
			var effects := 0
			for key in o.keys():
				if key == "id" or key == "display_name" or key.begins_with(_DOC_PREFIX):
					continue
				if not DOCTRINE_EFFECTS.has(key):
					errors.append("%s has unknown effect \"%s\". Known: %s"
						% [where, key, ", ".join(DOCTRINE_EFFECTS)])
					continue
				_req_num(o, key, where, 0.0001)
				effects += 1
			if effects == 0:
				errors.append("%s option does nothing." % where)

func _validate_premium() -> void:
	for kind in (building.get("premium_kinds", {}) as Dictionary).keys():
		if kind.begins_with(_DOC_PREFIX):
			continue
		var where := "building.json -> premium_kinds -> %s" % kind
		var k: Variant = (building["premium_kinds"] as Dictionary)[kind]
		if typeof(k) != TYPE_DICTIONARY:
			errors.append("%s must be an object." % where)
			continue
		if str((k as Dictionary).get("display_name", "")).is_empty():
			errors.append("%s needs a display_name." % where)
		if float((k as Dictionary).get("range_bonus", 0.0)) <= 0.0 \
				and float((k as Dictionary).get("fire_rate_bonus", 0.0)) <= 0.0:
			errors.append("%s grants nothing." % where)
	# A breach route is a complete road like a fork: same gate, same exit.
	for route: Variant in (map.get("breach_paths", []) as Array):
		if typeof(route) != TYPE_ARRAY or (route as Array).size() < 2:
			errors.append("map breach_paths entries must be routes of at least two waypoints.")
	for entry: Variant in (map.get("premium_cells", []) as Array):
		if typeof(entry) != TYPE_ARRAY or (entry as Array).size() != 3:
			errors.append("map premium_cells entries must be [cx, cy, kind].")
			continue
		var kind := str((entry as Array)[2])
		if not (building.get("premium_kinds", {}) as Dictionary).has(kind):
			errors.append("map premium cell names unknown kind \"%s\"." % kind)

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
		if (e as Dictionary).has("armour"):
			_req_int(e, "armour", where, 0)
		# Required, not defaulted. A drone with no armour class would quietly take
		# whatever class index 0 happens to be, and "this one is Light because
		# nobody said otherwise" is exactly the unexplainable balance value this
		# file exists to prevent.
		var armour_class := str((e as Dictionary).get("armour_class", ""))
		if armour_class.is_empty():
			errors.append("%s has no armour_class. Every drone must state what it wears: %s"
				% [where, ", ".join(_ids_of(damage.get("classes", {})))])
		elif not _ids_of(damage.get("classes", {})).has(armour_class):
			errors.append("%s wears unknown armour class \"%s\"." % [where, armour_class])
		# Both halves of each pair are required together: healing with no radius
		# heals nothing, and a radius with no amount is a drone that appears to be
		# a mender and is not. Silently doing nothing is the failure mode this file
		# exists to prevent.
		if (e as Dictionary).has("repair_per_pulse") or (e as Dictionary).has("repair_radius_units"):
			_req_int(e, "repair_per_pulse", where, 1)
			_req_num(e, "repair_radius_units", where, 0.0001)
			_req_num(e, "repair_interval_seconds", where, 0.0001)
		if (e as Dictionary).has("jam_seconds") or (e as Dictionary).has("jam_radius_units"):
			_req_num(e, "jam_seconds", where, 0.0001)
			_req_num(e, "jam_radius_units", where, 0.0001)
			_req_num(e, "jam_interval_seconds", where, 0.0001)
		# A Borer's tunnel point is a share of its road, so it must be inside it.
		# Zero would be a drone that tunnels before it has walked anywhere, and 1.0
		# one that tunnels at the exit, where it may as well have leaked.
		if (e as Dictionary).has("tunnels_at_progress"):
			var at := _req_num(e, "tunnels_at_progress", where, 0.0001)
			if at >= 1.0:
				errors.append("%s tunnels at or past the exit, which is just a leak." % where)
			if int((e as Dictionary).get("leak_value", 0)) != 0:
				errors.append("%s tunnels rather than leaking, so its leak_value must be 0." % where)
		if (e as Dictionary).has("splits_into"):
			var child := str((e as Dictionary).get("splits_into", ""))
			if not enemies.has(child) or child.begins_with(_DOC_PREFIX):
				errors.append("%s splits into unknown enemy \"%s\"." % [where, child])
			elif child == id:
				errors.append("%s splits into itself, which would never stop." % where)
			elif not str((enemies[child] as Dictionary).get("splits_into", "")).is_empty():
				# One hop only. A chain cannot then be a cycle, and the pool ceiling
				# stays something the wave validator can compute in one multiply.
				errors.append("%s splits into \"%s\", which itself splits. Splitting is one generation deep."
					% [where, child])
			_req_int(e, "split_count", where, 1)
		elif (e as Dictionary).has("split_count"):
			errors.append("%s has split_count but no splits_into, so nothing would ever hatch." % where)
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
		# Required for the same reason a drone's armour class is: an untyped gun
		# would silently fire whatever type index 0 is, and be strong and weak
		# against things nobody chose.
		var income := false
		var tiers_peek: Variant = (b as Dictionary).get("tiers")
		if typeof(tiers_peek) == TYPE_ARRAY and not (tiers_peek as Array).is_empty() \
				and typeof((tiers_peek as Array)[0]) == TYPE_DICTIONARY:
			income = ((tiers_peek as Array)[0] as Dictionary).has("income_per_wave")
		var damage_type := str((b as Dictionary).get("damage_type", ""))
		if damage_type.is_empty() and not income:
			errors.append("%s has no damage_type. Every gun must state what it fires: %s"
				% [where, ", ".join(_ids_of(damage.get("types", {})))])
		elif not damage_type.is_empty() and not _ids_of(damage.get("types", {})).has(damage_type):
			errors.append("%s fires unknown damage type \"%s\"." % [where, damage_type])
		if (b as Dictionary).has("support"):
			var support: Variant = (b as Dictionary)["support"]
			var swhere := "%s support" % where
			if typeof(support) != TYPE_DICTIONARY:
				errors.append("%s must be an object." % swhere)
			else:
				# A radius of zero reaches nothing, and a link with no bonus is a
				# link that does nothing - both are almost certainly a typo, and
				# both would be invisible at runtime.
				_req_num(support, "radius_units", swhere, 0.0001)
				var total := float((support as Dictionary).get("fire_rate_bonus", 0.0)) \
					+ float((support as Dictionary).get("damage_bonus", 0.0)) \
					+ float((support as Dictionary).get("range_bonus", 0.0))
				if total <= 0.0:
					errors.append("%s has a radius but grants nothing." % swhere)
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
			# An income tier is a rig: it earns instead of firing, so the whole
			# ballistic block is absent ON PURPOSE. Mixing the two in one tier is
			# refused - a gun that also prints money answers every trade at once.
			if (t as Dictionary).has("income_per_wave"):
				_req_int(t, "income_per_wave", twhere, 1)
				if (t as Dictionary).has("damage"):
					errors.append("%s has both damage and income_per_wave - a tier earns or fires, never both." % twhere)
				continue
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
	# An outpost board has no road. It declares a base and a ring of gates, and
	# the lanes are generated from them, so requiring a "path" here would reject a
	# perfectly valid map for lacking a thing its shape does not have.
	if map.has("base") or map.has("gates"):
		_validate_outpost()
		return
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
## Extra roads on a board. Each is a COMPLETE route from the same gate to the same
## exit, not a branch off the main one - which is what lets a drone's position stay
## a single distance along a single polyline.
##
## Both endpoints are checked because a fork that starts or ends somewhere else is
## not a fork, it is a second level sharing a map: drones would appear out of thin
## air or leak somewhere the player was never told to defend.
const ROUTE_ENDPOINT_TOLERANCE := 1.0

## An outpost board: a base to defend and the gates they come from.
##
## Validated strictly, because every one of these is load-bearing geometry and a
## typo in any of them produces a board that looks fine and cannot be played -
## gates inside the core, a build ring narrower than the core, a base with no way
## in. A readable message now is worth more than a mystery later.
func _validate_outpost() -> void:
	var where := "Map %s" % str(map.get("id", "?"))
	if not map.has("base"):
		errors.append("%s declares gates but no \"base\" for them to converge on." % where)
	else:
		var base: Variant = map.get("base")
		if typeof(base) != TYPE_DICTIONARY:
			errors.append("%s: \"base\" must be an object with x and y." % where)
		else:
			_req_num(base as Dictionary, "x", "%s base" % where, -1000000.0)
			_req_num(base as Dictionary, "y", "%s base" % where, -1000000.0)
	var gates: Variant = map.get("gates")
	if typeof(gates) != TYPE_ARRAY or (gates as Array).is_empty():
		errors.append("%s needs at least one gate - a base nothing can reach is not a level." % where)
	else:
		for i in (gates as Array).size():
			var gate: Variant = (gates as Array)[i]
			if typeof(gate) != TYPE_DICTIONARY:
				errors.append("%s gate %d must be an object with x and y." % [where, i])
				continue
			_req_num(gate as Dictionary, "x", "%s gate %d" % [where, i], -1000000.0)
			_req_num(gate as Dictionary, "y", "%s gate %d" % [where, i], -1000000.0)
	var core := float(map.get("core_radius", 0.0))
	var build := float(map.get("build_radius", 0.0))
	var start := float(map.get("start_radius", 0.0))
	if build <= core:
		errors.append("%s: build_radius (%.0f) must exceed core_radius (%.0f), or there is nowhere to build."
			% [where, build, core])
	if start < core or start > build:
		errors.append("%s: start_radius (%.0f) must sit between core_radius (%.0f) and build_radius (%.0f)."
			% [where, start, core, build])

func _validate_routes() -> void:
	var main: Variant = map.get("path")
	if typeof(main) != TYPE_ARRAY or (main as Array).size() < 2:
		return
	var extras: Variant = map.get("alternate_paths", [])
	if typeof(extras) != TYPE_ARRAY:
		errors.append("map: alternate_paths must be an array of routes.")
		return
	for r in (extras as Array).size():
		var route: Variant = (extras as Array)[r]
		var where := "map: alternate_paths[%d]" % r
		if typeof(route) != TYPE_ARRAY or (route as Array).size() < 2:
			errors.append("%s needs at least two waypoints." % where)
			continue
		for i in (route as Array).size():
			var point: Variant = (route as Array)[i]
			if typeof(point) != TYPE_DICTIONARY:
				errors.append("%s waypoint %d must be an object." % [where, i])
				continue
			_req_num(point, "x", "%s waypoint %d" % [where, i], -1000000.0)
			_req_num(point, "y", "%s waypoint %d" % [where, i], -1000000.0)
		if not _same_point((route as Array)[0], (main as Array)[0]):
			errors.append("%s starts somewhere the main road does not - a fork leaves the same gate." % where)
		if not _same_point((route as Array)[-1], (main as Array)[-1]):
			errors.append("%s ends somewhere the main road does not - a fork rejoins at the same exit." % where)
	var weights: Variant = map.get("route_weights", [])
	if typeof(weights) == TYPE_ARRAY and (weights as Array).size() > 0 \
			and (weights as Array).size() != 1 + (extras as Array).size():
		errors.append("map: route_weights has %d entries for %d routes."
			% [(weights as Array).size(), 1 + (extras as Array).size()])

func _same_point(a: Variant, b: Variant) -> bool:
	if typeof(a) != TYPE_DICTIONARY or typeof(b) != TYPE_DICTIONARY:
		return false
	return absf(float((a as Dictionary).get("x", 0.0)) - float((b as Dictionary).get("x", 0.0))) <= ROUTE_ENDPOINT_TOLERANCE \
		and absf(float((a as Dictionary).get("y", 0.0)) - float((b as Dictionary).get("y", 0.0))) <= ROUTE_ENDPOINT_TOLERANCE

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
	if engagement.has("affixes"):
		var listed: Variant = engagement["affixes"]
		if typeof(listed) != TYPE_ARRAY:
			errors.append("wave file: affixes must be a list of affix ids.")
		else:
			var seen := {}
			for entry: Variant in (listed as Array):
				var id := str(entry)
				if not affixes.has(id) or id.begins_with(_DOC_PREFIX):
					errors.append("wave file: unknown affix \"%s\". Known: %s"
						% [id, ", ".join(_ids_of(affixes))])
				elif seen.has(id):
					# Twice would apply twice, which is a different act than the one
					# whoever wrote the list was describing.
					errors.append("wave file: affix \"%s\" is listed twice." % id)
				seen[id] = true
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
			# A drone that breaks into others puts more on the board than the wave
			# file lists. Counting only what is written would let a wave overrun the
			# pool at the moment the last brood dies, which is the worst possible
			# moment to start silently dropping drones.
			# ...and an affix that multiplies head-count puts more on the board than
			# the group lists, so the ceiling has to be checked against the act as it
			# will actually be played, not as it was written.
			in_wave += int(ceil(float(count) * _affix_count_multiplier())) \
				* (1 + _split_yield(enemy_id))
		peak = maxi(peak, in_wave)
	# A wave that can out-spawn the enemy pool would silently drop enemies, which
	# is exactly the kind of "the numbers are quietly wrong" failure that must not
	# be possible. Catch it at load, not at tick 4000.
	var cap := int(sim.get("max_enemies", 0))
	if peak > cap:
		errors.append("A wave spawns %d enemies but sim.json max_enemies is %d. Raise max_enemies." % [peak, cap])

## What this act's affixes do to every group's head-count, multiplied together.
## 1.0 when the act has none, which is most of them.
func _affix_count_multiplier() -> float:
	var total := 1.0
	for entry: Variant in (engagement.get("affixes", []) as Array):
		total *= maxf(0.0, float((affixes.get(str(entry), {}) as Dictionary)
			.get("count_multiplier", 1.0)))
	return total

## How many extra drones one of this type eventually puts on the board. 0 for
## everything that simply dies.
func _split_yield(enemy_id: String) -> int:
	if not enemies.has(enemy_id):
		return 0
	var e: Dictionary = enemies[enemy_id]
	if str(e.get("splits_into", "")).is_empty():
		return 0
	return maxi(0, int(e.get("split_count", 0)))

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

## Every module id, in file order with the doc key skipped. Static because the
## draft is made by the layer above the simulation, which has no Database of its
## own and does not need one to ask what exists.
static func load_modules(data_root: String = DEFAULT_ROOT) -> PackedStringArray:
	var out := PackedStringArray()
	var path := "%s/modules/modules.json" % data_root
	if not FileAccess.file_exists(path):
		return out
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if typeof(parsed) != TYPE_DICTIONARY:
		return out
	for id in (parsed as Dictionary).keys():
		if not str(id).begins_with(_DOC_PREFIX):
			out.append(str(id))
	return out

## A module's display name and one-line description, for whoever is offering it.
static func module_text(id: String, data_root: String = DEFAULT_ROOT) -> PackedStringArray:
	var out := PackedStringArray()
	var path := "%s/modules/modules.json" % data_root
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if typeof(parsed) != TYPE_DICTIONARY:
		return out
	var m: Variant = (parsed as Dictionary).get(id)
	if typeof(m) != TYPE_DICTIONARY:
		return out
	out.append(str((m as Dictionary).get("display_name", id)))
	out.append(str((m as Dictionary).get("description", "")))
	return out

func tier_data(blueprint_id: String, tier_index: int) -> Dictionary:
	var b: Dictionary = blueprints.get(blueprint_id, {})
	var tiers: Array = b.get("tiers", [])
	if tier_index < 0 or tier_index >= tiers.size():
		return {}
	return tiers[tier_index]
