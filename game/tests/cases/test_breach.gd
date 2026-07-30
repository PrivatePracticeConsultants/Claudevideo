extends TestCase

## The Breach Borer, and the road it opens.
##
## Every other drone costs Integrity. This one costs the MAP: it walks two thirds
## of its road, goes under, and opens a dormant breach route that carries traffic
## for the rest of the act. Killing it before it reaches the tunnel point is the
## entire counterplay - the first drone in the game where killing something fast
## enough matters rather than just killing it.
##
## The breach road exists from the first tick, dormant and drawn, so the buildable
## band includes it. That is the decision: spend slots covering a road that may
## never open, or trust yourself to stop the Borer.

const BREACH_MAP := "terminus_02"
const BREACH_ACT := "terminus_act4"

func _armed() -> Sim:
	return SimFixture.for_level(BREACH_MAP, BREACH_ACT)

func test_an_armed_act_starts_with_a_road_nothing_walks() -> void:
	var sim := _armed()
	assert_gt(float(sim.closed_routes()), 0.0, "a breach road is present")
	assert_eq(sim.breaches_opened(), 0, "and closed")

func test_a_board_without_breaches_has_none() -> void:
	# Every board written before the Borer must be untouched by it.
	var sim := SimFixture.fresh()
	assert_eq(sim.closed_routes(), 0, "no dormant roads")

func test_nothing_spawns_onto_a_closed_road() -> void:
	var sim := _armed()
	var closed := -1
	for r in sim.route_count():
		if not sim.route_is_open(r):
			closed = r
			break
	assert_gte(float(closed), 0.0, "fixture sanity: a closed road exists")
	sim._begin_wave(0)
	for _t in 900:
		sim.step()
	var seen := 0
	for i in sim.enemy_slot_bound():
		if sim.e_alive[i] == 1 and sim.enemy_route(i) == closed:
			seen += 1
	assert_eq(seen, 0, "a closed road carries nothing")

func test_a_borer_that_reaches_its_point_opens_the_road() -> void:
	var sim := _armed()
	var borer := sim.enemy_index("borer")
	assert_gte(float(borer), 0.0, "the Borer is in the roster")
	sim._begin_wave(0)
	sim._spawn_at(borer, 0.0)
	var before := sim.closed_routes()
	# Walk it past its tunnel point by hand rather than waiting out the road.
	for i in sim.enemy_slot_bound():
		if sim.e_alive[i] == 1 and sim.e_type[i] == borer:
			sim.e_prog[i] = sim.route_length(sim.enemy_route(i)) \
				* sim.enemy_tunnels_at(borer) + 1.0
	sim._open_breaches()
	assert_eq(sim.closed_routes(), before - 1, "one road opened")
	assert_eq(sim.breaches_opened(), 1, "and it was counted")

func test_a_borer_that_tunnels_costs_no_integrity() -> void:
	# It never reaches the exit. What it takes is the map, not the wall.
	var sim := _armed()
	var borer := sim.enemy_index("borer")
	sim._begin_wave(0)
	sim._spawn_at(borer, 0.0)
	var integrity := sim.integrity()
	var leaks := sim.leaks()
	for i in sim.enemy_slot_bound():
		if sim.e_alive[i] == 1 and sim.e_type[i] == borer:
			sim.e_prog[i] = sim.route_length(sim.enemy_route(i)) \
				* sim.enemy_tunnels_at(borer) + 1.0
	sim._open_breaches()
	assert_eq(sim.integrity(), integrity, "the wall is untouched")
	assert_eq(sim.leaks(), leaks, "and it did not leak")

func test_killing_the_borer_in_time_saves_the_road() -> void:
	# The counterplay, measured: the same act, the same Borer, killed one step
	# short of its tunnel point - and the road stays shut.
	var sim := _armed()
	var borer := sim.enemy_index("borer")
	sim._begin_wave(0)
	sim._spawn_at(borer, 0.0)
	var before := sim.closed_routes()
	for i in sim.enemy_slot_bound():
		if sim.e_alive[i] == 1 and sim.e_type[i] == borer:
			sim.e_prog[i] = sim.route_length(sim.enemy_route(i)) \
				* sim.enemy_tunnels_at(borer) - 1.0
			sim._damage_enemy(i, 1 << 28, -1)
	sim._open_breaches()
	assert_eq(sim.closed_routes(), before, "no road opened")
	assert_eq(sim.breaches_opened(), 0, "the breach never happened")

func test_an_opened_road_then_carries_traffic() -> void:
	var sim := _armed()
	var borer := sim.enemy_index("borer")
	sim._begin_wave(0)
	sim._spawn_at(borer, 0.0)
	var opened := -1
	for r in sim.route_count():
		if not sim.route_is_open(r):
			opened = r
			break
	for i in sim.enemy_slot_bound():
		if sim.e_alive[i] == 1 and sim.e_type[i] == borer:
			sim.e_prog[i] = sim.route_length(sim.enemy_route(i)) \
				* sim.enemy_tunnels_at(borer) + 1.0
	sim._open_breaches()
	assert_true(sim.route_is_open(opened), "the road is open")
	var seen := false
	for _s in 200:
		sim._spawn(sim.enemy_index("walker"))
	for i in sim.enemy_slot_bound():
		if sim.e_alive[i] == 1 and sim.enemy_route(i) == opened:
			seen = true
			break
	assert_true(seen, "and drones now walk it")

func test_the_breach_road_is_buildable_before_it_opens() -> void:
	# The load-bearing property. If the band did not include a dormant road you
	# could not prepare for it, and an unpreparable threat is not a decision.
	var sim := _armed()
	var closed := -1
	for r in sim.route_count():
		if not sim.route_is_open(r):
			closed = r
			break
	# A point beside the middle of the closed road.
	var mid := sim.route_length(closed) * 0.5
	sim.sample_for_render(mid, sim.build_min_distance() + 12.0, closed)
	assert_lt(sim.distance_to_path(sim.out_x(), sim.out_y()),
		sim.build_max_distance(),
		"ground beside a dormant road is inside the buildable band")

func test_a_breach_is_part_of_the_state_hash_and_replays() -> void:
	var opened := _armed()
	var plain := _armed()
	var borer := opened.enemy_index("borer")
	opened._begin_wave(0)
	plain._begin_wave(0)
	opened._spawn_at(borer, 0.0)
	plain._spawn_at(borer, 0.0)
	for i in opened.enemy_slot_bound():
		if opened.e_alive[i] == 1 and opened.e_type[i] == borer:
			opened.e_prog[i] = opened.route_length(opened.enemy_route(i)) \
				* opened.enemy_tunnels_at(borer) + 1.0
	opened._open_breaches()
	assert_ne(opened.state_hash(), plain.state_hash(),
		"a board with a road open is a different state")

func test_the_borer_is_answerable_by_a_gun() -> void:
	# It is Plated and armoured, so the answer is explosive and prepared rather
	# than a wall of small rounds fired in a hurry.
	var sim := _armed()
	var borer := sim.enemy_index("borer")
	var armour_class := sim.enemy_armour_class(borer)
	var best := 0.0
	for t in sim.damage_type_ids().size():
		best = maxf(best, sim.matchup(t, armour_class))
	assert_gt(best, 1.0, "something is favoured into it")
	assert_gt(float(sim.enemy_base_hp(borer)), 500.0, "and it is not killed by accident")
