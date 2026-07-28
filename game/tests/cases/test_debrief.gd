extends TestCase

## The end-of-act debrief.
##
## A tower defence tells you almost nothing about why you won. The board is a blur
## at 4x, then it is a banner, and the next act's build decisions get made on a
## hunch. The debrief answers the one question those decisions actually turn on -
## which of your weapon families was doing the work - and it is held to the same
## honesty rule as everything else that reports a number: overkill is not damage
## dealt, and a family that never fired is not listed as having done nothing.

var _tree: SceneTree
var _sim: Sim
var _hud: Hud

func before_each() -> void:
	_tree = Engine.get_main_loop() as SceneTree
	_sim = SimFixture.fresh()
	_hud = Hud.new()
	_tree.root.add_child(_hud)
	_hud.setup(_sim, _theme())

func after_each() -> void:
	_tree.root.remove_child(_hud)
	_hud.queue_free()

func _theme() -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://data/theme.json"))
	return parsed if typeof(parsed) == TYPE_DICTIONARY else {}

func test_damage_is_credited_to_the_family_that_did_it() -> void:
	var ballistic := _sim.blueprint_index("ballistic")
	var cannon := _sim.blueprint_index("cannon")
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("heavy"))
	_sim._damage_enemy(0, 30, ballistic)
	_sim._damage_enemy(0, 12, cannon)
	assert_eq(_sim.family_damage(ballistic), 30, "the Autocannon's thirty")
	assert_eq(_sim.family_damage(cannon), 12, "and the Mortar's twelve")

func test_overkill_is_not_counted_as_damage_dealt() -> void:
	# A railgun round doing 640 to something with 12 health left did 12. Claiming
	# 640 would make the family breakdown a fiction the moment anything died.
	var railgun := _sim.blueprint_index("railgun")
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("swarm"))
	var health: int = _sim.e_hp[0]
	_sim._damage_enemy(0, health * 100, railgun)
	assert_eq(_sim.family_damage(railgun), health,
		"only what the drone actually had is credited")

func test_a_kill_is_credited_once_to_whoever_landed_it() -> void:
	var ballistic := _sim.blueprint_index("ballistic")
	var cannon := _sim.blueprint_index("cannon")
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("walker"))
	_sim._damage_enemy(0, 1, ballistic)
	_sim._damage_enemy(0, 1 << 30, cannon)
	assert_eq(_sim.family_kills(ballistic), 0, "softening it up is not a kill")
	assert_eq(_sim.family_kills(cannon), 1, "landing the last round is")

func test_armour_is_taken_off_before_the_credit() -> void:
	# Otherwise the debrief would report damage that never reached the drone.
	var ballistic := _sim.blueprint_index("ballistic")
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("brood"))
	var before: int = _sim.e_hp[0]
	_sim._damage_enemy(0, 40, ballistic)
	assert_eq(_sim.family_damage(ballistic), before - _sim.e_hp[0],
		"credited exactly what came off the drone")
	assert_lt(float(_sim.family_damage(ballistic)), 40.0, "which is less than was fired")

func test_the_debrief_names_the_families_that_fired() -> void:
	var cannon := _sim.blueprint_index("cannon")
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("heavy"))
	_sim._damage_enemy(0, 100, cannon)
	var text := _hud.debrief_text()
	assert_true(text.contains(_sim.blueprint_display_name(cannon).to_upper()),
		"the family that fired is listed: %s" % text)
	assert_false(text.contains(_sim.blueprint_display_name(_sim.blueprint_index("railgun")).to_upper()),
		"a family that never fired is not listed as having done nothing: %s" % text)

func test_a_debrief_with_nothing_to_report_says_so() -> void:
	# Losing having built nothing is a real outcome, and a panel of blanks is worse
	# than a sentence.
	var text := _hud.debrief_text()
	assert_true(text.contains("nothing fired a shot"), "it says what happened: %s" % text)

func test_large_numbers_are_readable() -> void:
	# Damage totals run to seven digits by the last board, and Godot has no
	# thousands separator.
	assert_eq(_hud._thousands(0), "0", "zero")
	assert_eq(_hud._thousands(7), "7", "single digit")
	assert_eq(_hud._thousands(999), "999", "no separator needed")
	assert_eq(_hud._thousands(1000), "1,000", "one comma")
	assert_eq(_hud._thousands(1234567), "1,234,567", "two commas")
	assert_eq(_hud._thousands(-4200), "-4,200", "and negatives keep their sign")

func test_the_debrief_reports_the_engagement_totals() -> void:
	_sim._begin_wave(0)
	for _i in 3:
		_sim._spawn(_sim.enemy_index("swarm"))
	for i in 3:
		_sim._damage_enemy(i, 1 << 30, _sim.blueprint_index("ballistic"))
	var text := _hud.debrief_text()
	assert_true(text.contains("3 DESTROYED"), "kills are reported: %s" % text)
	assert_true(text.contains("0 LEAKED"), "and so are leaks: %s" % text)
