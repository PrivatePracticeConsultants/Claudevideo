extends TestCase

## Outpost boards: defended from the middle, lanes generated from gates.
##
## The design claim under test is that this mode is a different way of PRODUCING
## routes, not a different simulation - so most of these assert that outpost
## routes behave exactly like authored ones, and the rest pin the two things
## that are genuinely new: the ring-shaped buildable ground, and the endless
## block that expands into a concrete wave list at load.

const MAP := "outpost_01"
const ENGAGEMENT := "outpost_siege"

func test_a_corridor_board_is_not_an_outpost() -> void:
	# The flag must come from the map's shape, not from anything ambient.
	var sim := SimFixture.fresh()
	assert_false(sim.is_outpost(), "highway is a corridor")

func test_the_outpost_board_loads_and_knows_what_it_is() -> void:
	var sim := SimFixture.for_level(MAP, ENGAGEMENT)
	assert_true(sim.is_outpost(), "outpost_01 is an outpost")
	assert_eq(sim.route_count(), 8, "one lane per gate")
	assert_eq(sim.closed_routes(), 0, "and every lane is open from the first tick")

func test_every_lane_ends_at_the_base() -> void:
	# The leak rule is "walked off the end of its route". On this board the end
	# of every route must be the station, or leaking would mean something
	# different per lane.
	var sim := SimFixture.for_level(MAP, ENGAGEMENT)
	var tolerance := 1.5
	for r in sim.route_count():
		var last := sim.route_waypoint_count(r) - 1
		assert_almost_eq(sim.route_waypoint_x(r, last), sim.base_x(), tolerance,
			"lane %d ends at the base" % r)
		assert_almost_eq(sim.route_waypoint_y(r, last), sim.base_y(), tolerance,
			"lane %d ends at the base" % r)

func test_drones_are_sent_down_every_lane() -> void:
	# Eight lanes that all carry traffic is the whole mode; a lane nothing walks
	# is scenery.
	var sim := SimFixture.for_level(MAP, ENGAGEMENT)
	sim._begin_wave(0)
	var seen := {}
	for _i in 160:
		sim._spawn(sim.enemy_index("walker"))
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			seen[sim.enemy_route(i)] = true
	assert_eq(seen.size(), sim.route_count(), "all %d lanes saw traffic" % sim.route_count())

func test_a_drone_leaks_when_it_reaches_the_station() -> void:
	var sim := SimFixture.for_level(MAP, ENGAGEMENT)
	sim._begin_wave(0)
	sim._spawn_at(sim.enemy_index("walker"), sim.route_length(3) - 1.0, 3)
	var integrity := sim.integrity()
	var leaks := sim.leaks()
	sim._advance_enemies()
	assert_eq(sim.leaks(), leaks + 1, "it reached the station")
	assert_lt(float(sim.integrity()), float(integrity), "and the hull paid for it")

func test_the_buildable_ground_is_a_ring() -> void:
	var sim := SimFixture.for_level(MAP, ENGAGEMENT)
	var core := sim.outpost_core_radius()
	var outer := sim.outpost_build_radius()
	var inside := 0
	var beyond := 0
	var held := 0
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			var dx := sim.cell_centre_x(cx) - sim.base_x()
			var dy := sim.cell_centre_y(cy) - sim.base_y()
			var to_base := sqrt(dx * dx + dy * dy)
			if not sim.cell_is_buildable(cx, cy):
				continue
			held += 1
			if to_base < core:
				inside += 1
			if to_base > outer:
				beyond += 1
	assert_gt(float(held), 0.0, "there is ground to build on")
	assert_eq(inside, 0, "none of it is on the station")
	assert_eq(beyond, 0, "and none of it is outside the build radius")

func test_the_hull_comes_from_the_engagement() -> void:
	# A corridor's integrity is a leak counter; a station's is a health bar with
	# eight lanes pointed at it. The override existing - and the corridor NOT
	# using it - is load-bearing for both modes.
	var outpost := SimFixture.for_level(MAP, ENGAGEMENT)
	assert_gt(float(outpost.integrity()), 1000.0, "the station has a hull, not a leak counter")
	var corridor := SimFixture.fresh()
	assert_eq(corridor.integrity(), 100, "and the corridor still has its counter")

func test_the_endless_block_expanded_into_real_waves() -> void:
	var sim := SimFixture.for_level(MAP, ENGAGEMENT)
	assert_eq(sim.wave_count(), 14, "the declared length")

func test_the_expansion_is_deterministic() -> void:
	# Two loads must produce identical boards - the expansion feeds enemy counts,
	# and a wave list that differed between machines would desync a replay.
	var a := SimFixture.for_level(MAP, ENGAGEMENT)
	var b := SimFixture.for_level(MAP, ENGAGEMENT)
	for sim in [a, b]:
		sim._begin_wave(4)
	assert_eq(a.state_hash(), b.state_hash(), "wave 5 is the same wave both times")

func test_wave_five_brings_the_titan() -> void:
	# boss_every: 5 - and ON TOP of the wave, not instead of it.
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	var wave: Dictionary = (db.engagement["waves"] as Array)[4]
	var names := []
	for group: Dictionary in (wave["groups"] as Array):
		names.append(str(group["enemy"]))
	assert_true(names.has("titan"), "the Titan is in wave 5: %s" % [names])
	assert_gt(float(names.size()), 1.0, "and it did not come alone")

func test_wave_ten_brings_the_harbinger_instead() -> void:
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	var wave: Dictionary = (db.engagement["waves"] as Array)[9]
	var names := []
	for group: Dictionary in (wave["groups"] as Array):
		names.append(str(group["enemy"]))
	assert_true(names.has("harbinger"), "the bosses alternate: %s" % [names])

func test_the_ramp_actually_ramps() -> void:
	# Wave 11 is the same archetype as wave 1 (five archetypes, cycling), so the
	# only difference between them is the growth - which makes them the clean pair
	# to compare.
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	var early: Dictionary = ((db.engagement["waves"] as Array)[0] as Dictionary)
	var late: Dictionary = ((db.engagement["waves"] as Array)[10] as Dictionary)
	var early_count := 0
	var late_count := 0
	for group: Dictionary in (early["groups"] as Array):
		early_count += int(group["count"])
	for group: Dictionary in (late["groups"] as Array):
		late_count += int(group["count"])
	assert_gt(float(late_count), float(early_count),
		"wave 11 (%d) outweighs wave 1 (%d)" % [late_count, early_count])

func test_an_outpost_run_replays_identically() -> void:
	# The mode's whole claim: same code downstream, so the determinism contract
	# holds without any outpost-specific work.
	var live := SimFixture.for_level(MAP, ENGAGEMENT)
	var log := SimFixture.run_greedy(live, true, 3, 1)
	var again := SimFixture.for_level(MAP, ENGAGEMENT)
	SimFixture.replay(again, log, 2500)
	var third := SimFixture.for_level(MAP, ENGAGEMENT)
	SimFixture.replay(third, log, 2500)
	assert_eq(again.state_hash(), third.state_hash(), "same log, same state")

func test_gates_without_a_base_are_rejected() -> void:
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	db.errors = PackedStringArray()
	db.map.erase("base")
	db._validate_map()
	assert_false(db.is_valid(), "gates with nothing to converge on must fail")
	assert_true(db.error_text().contains("base"), "and say why: %s" % db.error_text())

func test_a_ring_narrower_than_the_core_is_rejected() -> void:
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	db.errors = PackedStringArray()
	db.map["build_radius"] = float(db.map["core_radius"]) - 1.0
	db._validate_map()
	assert_false(db.is_valid(), "nowhere to build must fail to load")

func test_an_endless_block_with_no_archetypes_is_rejected() -> void:
	var db := Database.load_engagement(MAP, ENGAGEMENT)
	db.errors = PackedStringArray()
	db.engagement["endless"] = {"waves": 10, "archetypes": []}
	db._expand_endless()
	assert_false(db.is_valid(), "an endless mode with nothing to send is not a mode")

func test_the_siege_cannot_be_won_by_doing_nothing() -> void:
	# The siege shipped unlosable. It was authored at 40 waves, the balance probe
	# could not win it, it was cut to 14 - and the hull was not rescaled with it.
	# An idle run, not one turret placed, finished with 1196 of 2600 left.
	#
	# Only LASTLINE_FULL_CAMPAIGN=1 exercises the outpost chain, so the guard
	# that existed found this months late, in a pre-release run. This is the same
	# assertion at sampled-suite cost: one level, one idle run, every time.
	var level := {}
	for entry in Database.load_levels():
		if str(entry["engagement"]) == ENGAGEMENT:
			level = entry
	assert_false(level.is_empty(), "the siege level is in levels.json")

	var idle := SimFixture.idle_run(level, {})
	assert_eq(idle.result(), Sim.RESULT_LOSS,
		"a siege nobody defends must fall; %d hull left" % idle.integrity())

func test_the_siege_is_still_winnable_by_defending_it() -> void:
	# The other half, and the reason the hull is not simply set to 1. Measured
	# across five seeds: an idle run leaks exactly 1404 (seed-independent -
	# everything reaches the core, so the total is just what the waves are
	# worth), a competent run leaks 926-991. The hull sits at 1200, mid-window.
	var level := {}
	for entry in Database.load_levels():
		if str(entry["engagement"]) == ENGAGEMENT:
			level = entry
	var sim: Sim = SimFixture.start_act(level, {}, 20260727)
	SimFixture.run_greedy(sim)
	assert_eq(sim.result(), Sim.RESULT_WIN,
		"a defended siege must hold; %d hull left" % sim.integrity())
	assert_gt(float(sim.integrity()), 0.0, "won with hull to spare")
