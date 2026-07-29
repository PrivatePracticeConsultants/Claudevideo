extends TestCase

## Boards with more than one road.
##
## Modelled as N COMPLETE routes from the same gate to the same exit rather than
## as a graph with junction nodes, and that is the whole reason it was affordable:
## a drone's position stays one scalar distance along one polyline, the placement
## rules stay "distance to the nearest segment", and nothing in the tick learned
## what a junction is.
##
## The point of it is that coverage has to be DIVIDED. One road rewards one long
## line; two roads mean every turret chooses which one it watches, and the
## deployment limit means it cannot watch both.

const FORKED_MAP := "terminus_02"
const FORKED_ACT := "terminus_act3"
const FORKED_ACTS := ["terminus_act1", "terminus_act2", "terminus_act3", "terminus_act4"]

func test_a_board_without_a_fork_has_exactly_one_road() -> void:
	# Every board written before forks existed must be untouched by them.
	var sim := SimFixture.fresh()
	assert_eq(sim.route_count(), 1, "one road")
	assert_eq(sim.route_length(0), sim.path_length(), "and it is the whole path")

func test_the_forked_board_has_two() -> void:
	var sim := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	assert_eq(sim.route_count(), 2, "two roads")
	assert_gt(sim.route_length(0), 0.0, "the main road has length")
	assert_gt(sim.route_length(1), 0.0, "and so does the fork")

func test_a_fork_belongs_to_the_board_not_to_an_act() -> void:
	# It used to open partway through the chain, on the act where the corridor
	# finished revealing. That was measured as unplayable once every act ran the
	# whole road: the act inheriting a full board was already at its deployment
	# limit, so the second road arrived with no turrets left to answer it - 68
	# leaks, integrity 0, on an act that had been winnable. A fork is a fact about
	# the board now, so a player learns it once and it never moves.
	for eid: String in FORKED_ACTS:
		var sim := SimFixture.for_level(FORKED_MAP, eid)
		assert_eq(sim.route_count(), 2, "%s should run both roads" % eid)

func test_both_roads_leave_the_gate_and_reach_the_exit() -> void:
	var sim := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	var tolerance := 1.5
	for r in range(1, sim.route_count()):
		assert_almost_eq(sim.route_waypoint_x(r, 0), sim.route_waypoint_x(0, 0), tolerance,
			"route %d starts at the gate" % r)
		assert_almost_eq(sim.route_waypoint_y(r, 0), sim.route_waypoint_y(0, 0), tolerance,
			"route %d starts at the gate" % r)
		var last := sim.route_waypoint_count(r) - 1
		var main_last := sim.route_waypoint_count(0) - 1
		assert_almost_eq(sim.route_waypoint_x(r, last), sim.route_waypoint_x(0, main_last),
			tolerance, "route %d ends at the exit" % r)
		assert_almost_eq(sim.route_waypoint_y(r, last), sim.route_waypoint_y(0, main_last),
			tolerance, "route %d ends at the exit" % r)

func test_drones_are_sent_down_both_roads() -> void:
	# A second road nothing walks is scenery.
	var sim := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	sim._begin_wave(0)
	var seen := {}
	for _i in 40:
		sim._spawn(sim.enemy_index("walker"))
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			seen[sim.enemy_route(i)] = true
	assert_true(seen.has(0), "some went down the main road")
	assert_true(seen.has(1), "and some down the fork")

func test_the_traffic_split_is_the_one_the_map_asked_for() -> void:
	# Deliberately not random: a fork whose split wandered run to run would make
	# "how much do I put on the left road" unanswerable.
	var sim := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	sim._begin_wave(0)
	var counts := PackedInt32Array()
	counts.resize(sim.route_count())
	for _i in 120:
		sim._spawn(sim.enemy_index("swarm"))
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			counts[sim.enemy_route(i)] += 1
	assert_gt(float(counts[0]), float(counts[1]),
		"the long road carries more of it, as route_weights says")
	assert_gt(float(counts[1]), 0.0, "and the fork carries some")

func test_the_same_split_happens_every_run() -> void:
	var a := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	var b := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	for sim in [a, b]:
		sim._begin_wave(0)
		for _i in 30:
			sim._spawn(sim.enemy_index("walker"))
	assert_eq(a.state_hash(), b.state_hash(), "identical boards, identical traffic")

func test_a_drone_walks_its_own_road_and_leaks_at_its_own_end() -> void:
	var sim := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	sim._begin_wave(0)
	sim._spawn_at(sim.enemy_index("walker"), 0.0, 1)
	var slot := 0
	assert_eq(sim.enemy_route(slot), 1, "fixture sanity: it is on the fork")
	# Placed just short of the FORK's end, which is shorter than the main road's.
	assert_lt(sim.route_length(1), sim.route_length(0),
		"fixture sanity: the fork is the shorter road")
	sim.e_prog[slot] = sim.route_length(1) - 1.0
	var leaks := sim.leaks()
	sim._advance_enemies()
	assert_eq(sim.leaks(), leaks + 1, "it left at the end of ITS road")

func test_a_drone_on_the_fork_is_positioned_on_the_fork() -> void:
	var sim := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	sim._begin_wave(0)
	var half := sim.route_length(1) * 0.5
	sim._spawn_at(sim.enemy_index("walker"), half, 1)
	sim.e_offset[0] = 0.0
	sim._advance_enemies()
	sim.sample_for_render(sim.e_prog[0], 0.0, 1)
	var on_fork_x := sim.out_x()
	var on_fork_y := sim.out_y()
	sim.sample_for_render(sim.e_prog[0], 0.0, 0)
	var dx := on_fork_x - sim.out_x()
	var dy := on_fork_y - sim.out_y()
	assert_gt(sqrt(dx * dx + dy * dy), 200.0,
		"the same distance along the two roads is nowhere near the same place")

func test_ground_beside_either_road_is_buildable() -> void:
	# distance_to_path answers with the NEAREST road, so both are defendable.
	var sim := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	var reach := sim.build_min_distance() + 20.0
	for r in sim.route_count():
		sim.sample_for_render(sim.route_length(r) * 0.5, reach, r)
		assert_lte(sim.distance_to_path(sim.out_x(), sim.out_y()), reach + 1.0,
			"a point beside road %d is measured against road %d" % [r, r])

func test_a_brood_hatches_onto_its_parents_road() -> void:
	# The wreck is on the road it was travelling.
	var sim := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	sim._begin_wave(0)
	sim._spawn_at(sim.enemy_index("brood"), 900.0, 1)
	sim._damage_enemy(0, 1 << 30, -1)
	sim._resolve_splits()
	var hatched := 0
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			hatched += 1
			assert_eq(sim.enemy_route(i), 1, "the brood is on the fork too")
	assert_gt(float(hatched), 0.0, "fixture sanity: something hatched")

func test_the_route_is_part_of_the_state_hash() -> void:
	var a := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	var b := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	a._begin_wave(0)
	b._begin_wave(0)
	a._spawn_at(a.enemy_index("walker"), 500.0, 0)
	b._spawn_at(b.enemy_index("walker"), 500.0, 1)
	assert_ne(a.state_hash(), b.state_hash(),
		"the same drone on a different road is a different board")

func test_a_forked_act_replays_identically() -> void:
	# Bounded: determinism is a claim about the state at a given tick, and this is
	# the largest act in the campaign - playing all sixteen waves twice over is
	# five minutes of a suite that has to finish in ten.
	var live := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	var log := SimFixture.run_greedy(live, true, 3, 1)
	var again := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	SimFixture.replay(again, log, 2500)
	var third := SimFixture.for_level(FORKED_MAP, FORKED_ACT)
	SimFixture.replay(third, log, 2500)
	assert_eq(again.tick(), third.tick(), "both ran the same distance")
	assert_eq(again.state_hash(), third.state_hash(),
		"same log, same seed, same state at the same tick")

func test_a_fork_that_starts_elsewhere_is_rejected() -> void:
	# A fork that does not leave the same gate is not a fork, it is a second level
	# sharing a map - drones would appear out of thin air.
	var db := Database.load_engagement(FORKED_MAP, FORKED_ACT)
	db.errors = PackedStringArray()
	var route: Array = (db.map["alternate_paths"] as Array)[0]
	(route[0] as Dictionary)["x"] = 99999.0
	db._validate_routes()
	assert_false(db.is_valid(), "it must fail to load")
	assert_true(db.error_text().contains("gate"), "and say why: %s" % db.error_text())

func test_a_fork_that_ends_elsewhere_is_rejected() -> void:
	var db := Database.load_engagement(FORKED_MAP, FORKED_ACT)
	db.errors = PackedStringArray()
	var route: Array = (db.map["alternate_paths"] as Array)[0]
	(route[route.size() - 1] as Dictionary)["y"] = -99999.0
	db._validate_routes()
	assert_false(db.is_valid(), "it must fail to load")
	assert_true(db.error_text().contains("exit"), "and say why: %s" % db.error_text())

func test_mismatched_route_weights_are_rejected() -> void:
	var db := Database.load_engagement(FORKED_MAP, FORKED_ACT)
	db.errors = PackedStringArray()
	db.map["route_weights"] = [1, 2, 3, 4]
	db._validate_routes()
	assert_false(db.is_valid(), "four weights for two roads must fail")
