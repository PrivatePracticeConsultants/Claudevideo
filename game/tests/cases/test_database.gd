extends TestCase

## The loader is the only thing standing between a typo in a JSON file and a
## silently wrong balance number, so its failure modes are tested as carefully
## as its success path.

const MAP := "highway_01"
const ENGAGEMENT := "highway_act1"

func test_ships_data_loads_clean() -> void:
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	assert_true(db.is_valid(), "shipped data must load without errors: %s" % db.error_text())
	assert_eq(db.sim["tick_rate_hz"], 30.0, "tick rate comes from sim.json")
	assert_eq((db.engagement["waves"] as Array).size(), 10, "P0 engagement is ten waves")

func test_documentation_keys_are_not_content() -> void:
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	# Every data file carries a "_comment" explaining its numbers. Those must
	# never be mistaken for an enemy or a blueprint.
	for id in db.enemy_ids():
		assert_false(id.begins_with("_"), "enemy_ids leaked a doc key: %s" % id)
	for id in db.blueprint_ids():
		assert_false(id.begins_with("_"), "blueprint_ids leaked a doc key: %s" % id)
	assert_true(db.enemies.has("_comment"), "fixture sanity: the doc key really is in the file")

func test_missing_file_is_reported_not_crashed() -> void:
	var db := Database.load_engagement("no_such_map", ENGAGEMENT)
	assert_false(db.is_valid(), "a missing map must fail validation")
	assert_true(db.error_text().contains("no_such_map"), "the error names the file: %s" % db.error_text())

func test_unknown_enemy_in_wave_is_rejected() -> void:
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	db.errors = PackedStringArray()
	((db.engagement["waves"] as Array)[0] as Dictionary)["groups"] = [
		{"enemy": "ghost", "count": 1, "spawn_interval_ticks": 1, "start_delay_ticks": 0}
	]
	db._validate_engagement()
	assert_false(db.is_valid(), "a wave spawning an undefined enemy must fail")
	assert_true(db.error_text().contains("ghost"), "the error names the enemy: %s" % db.error_text())

func test_wave_wider_than_the_enemy_pool_is_rejected() -> void:
	# This is the failure that would otherwise show up as "enemies quietly stop
	# spawning at wave 9", which is exactly the kind of silently-wrong-numbers
	# bug the honesty rules exist to prevent.
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	db.errors = PackedStringArray()
	db.sim["max_enemies"] = 4.0
	db._validate_engagement()
	assert_false(db.is_valid(), "a wave larger than max_enemies must fail to load")
	assert_true(db.error_text().contains("max_enemies"), "the error explains the fix: %s" % db.error_text())

func test_non_integer_where_integer_required_is_rejected() -> void:
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	db.errors = PackedStringArray()
	db.economy["starting_capital"] = 400.5
	db._req_int(db.economy, "starting_capital", "economy.json", 0)
	assert_false(db.is_valid(), "fractional capital must be rejected, not truncated")

func test_building_rules_must_leave_somewhere_to_build() -> void:
	# max <= min means the buildable band has zero or negative width, so the
	# whole map is unbuildable. That is a silent, total failure at runtime.
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	db.errors = PackedStringArray()
	db.building["max_distance_from_path"] = 10.0
	db.building["min_distance_from_path"] = 40.0
	db._validate()
	assert_false(db.is_valid(), "an empty buildable band must fail validation")
	assert_true(db.error_text().contains("buildable"), "the error says what is wrong: %s" % db.error_text())

func test_every_campaign_level_loads() -> void:
	var levels := Database.load_levels()
	assert_gt(float(levels.size()), 0.0, "levels.json lists levels")
	for level in levels:
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		assert_true(db.is_valid(), "level %s failed to load: %s" % [level["name"], db.error_text()])
