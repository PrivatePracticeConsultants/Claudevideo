extends TestCase

## Boards that grow across a chain of acts, keeping what you built.
##
## Two mechanics working together: an act reveals only part of its map's route,
## and a later act in the same chain inherits the earlier act's turrets and
## ground. The inheritance is only sound because the revealed prefix is
## identical between acts - if the corridor moved, a carried turret would end up
## somewhere that made no sense.

func _chain() -> Array:
	# Three acts of the opening board.
	return ["highway_act1", "highway_act2", "highway_act3"]

func test_later_acts_reveal_more_of_the_same_route() -> void:
	var previous := 0.0
	var previous_waypoints := 0
	for eid in _chain():
		var sim := SimFixture.for_level("highway_01", eid)
		assert_gt(sim.path_length(), previous, "%s extends the corridor" % eid)
		assert_gt(float(sim.revealed_waypoints()), float(previous_waypoints),
			"%s reveals more waypoints" % eid)
		previous = sim.path_length()
		previous_waypoints = sim.revealed_waypoints()

func test_the_revealed_prefix_never_moves() -> void:
	# The load-bearing property. Everything built in act 1 stays useful in act 3
	# only because the road it covers is in exactly the same place.
	var first := SimFixture.for_level("highway_01", "highway_act1")
	var last := SimFixture.for_level("highway_01", "highway_act3")
	for i in first.waypoint_count():
		assert_almost_eq(last.waypoint_x(i), first.waypoint_x(i), 0.0001,
			"waypoint %d moved between acts" % i)
		assert_almost_eq(last.waypoint_y(i), first.waypoint_y(i), 0.0001,
			"waypoint %d moved between acts" % i)

func test_the_full_route_is_longer_than_any_single_act() -> void:
	var last := SimFixture.for_level("highway_01", "highway_act3")
	assert_eq(last.revealed_waypoints(), last.full_waypoints(),
		"the final act opens the whole route")

func test_a_carried_board_arrives_intact() -> void:
	var first := SimFixture.for_level("highway_01", "highway_act1")
	SimFixture.run_greedy(first)
	assert_gt(float(first.t_count), 0.0, "fixture sanity: act 1 built something")
	var snapshot := first.board_snapshot()

	var second := SimFixture.for_level("highway_01", "highway_act2")
	second.adopt(snapshot["platforms"], snapshot["cells"])
	assert_eq(second.t_count, mini(first.t_count, second.carry_ceiling()),
		"every turret that fits carried forward")
	assert_eq(second.carry_dropped(), 0, "none were dropped by the corridor")
	var stepped_down := 0
	# Only as far as the inheritance ceiling: past that the turrets were stood
	# down, which is a different thing from being carried badly.
	for i in second.t_count:
		assert_almost_eq(second.t_x[i], first.t_x[i], 0.0001, "turret %d kept its place" % i)
		assert_eq(second.platform_blueprint(i), first.platform_blueprint(i),
			"turret %d kept its weapon" % i)
		assert_eq(second.platform_tier(i),
			clampi(mini(first.platform_tier(i) - 1, Sim.CARRY_TIER_CAP), 0, 99),
			"turret %d arrives refitted" % i)
		if first.platform_tier(i) > 0:
			stepped_down += 1
	assert_gt(float(stepped_down), 0.0,
		"fixture sanity: act 1 upgraded something, so the tier step is exercised")

func test_a_finished_board_cannot_be_inherited_finished() -> void:
	# The property the whole chain rests on being a game: however comfortably an
	# act was won, what the next one inherits is bounded. Without this an act that
	# ended tier-4 across the board wins the following act with no input at all -
	# measured on every carrying act in the campaign before the cap existed.
	var sim := SimFixture.for_level("highway_01", "highway_act2")
	var top := sim.blueprint_tier_count(sim.blueprint_index("ballistic")) - 1
	assert_gt(float(top), float(Sim.CARRY_TIER_CAP),
		"fixture sanity: there are tiers above the carry cap")
	var spot := SimFixture.a_site(sim)
	sim.adopt([{"x": float(spot[0]), "y": float(spot[1]),
		"blueprint": sim.blueprint_index("ballistic"), "tier": top}],
		PackedInt32Array())
	assert_eq(sim.t_count, 1, "the turret carried")
	assert_eq(sim.platform_tier(0), Sim.CARRY_TIER_CAP,
		"a top-tier turret arrives capped, not intact")

func test_carrying_a_board_costs_nothing() -> void:
	# Those turrets were paid for in the act that built them. Charging again
	# would make continuing a chain strictly worse than starting one.
	var first := SimFixture.for_level("highway_01", "highway_act1")
	SimFixture.run_greedy(first)
	var second := SimFixture.for_level("highway_01", "highway_act2")
	var before := second.capital()
	second.adopt(first.board_snapshot()["platforms"], PackedInt32Array())
	assert_eq(second.capital(), before, "inherited turrets are free")

func test_carried_turrets_count_against_the_new_limit() -> void:
	# A bigger deployment limit is meant to buy room to extend coverage, not a
	# clean slate on top of everything already standing.
	var first := SimFixture.for_level("highway_01", "highway_act1")
	SimFixture.run_greedy(first)
	var second := SimFixture.for_level("highway_01", "highway_act2")
	second.adopt(first.board_snapshot()["platforms"], PackedInt32Array())
	assert_eq(second.t_count, first.t_count,
		"every turret carries - what an inheritance costs is tiers, not emplacements")
	assert_lt(float(second.t_count), float(second.platform_limit()),
		"and the new act's limit still leaves room to build past what was inherited")
	assert_gt(float(second.platform_limit()), float(first.platform_limit()),
		"which is larger than the previous act's")

func test_owned_ground_carries_forward() -> void:
	var first := SimFixture.for_level("highway_01", "highway_act1")
	var target := _first_offerable(first)
	assert_gte(float(target.x), 0.0, "fixture sanity: ground was purchasable")
	first.queue_buy_cell(0, int(target.x), int(target.y))
	first.step()
	assert_true(first.cell_is_unlocked(int(target.x), int(target.y)), "act 1 bought it")

	var second := SimFixture.for_level("highway_01", "highway_act2")
	assert_false(second.cell_is_unlocked(int(target.x), int(target.y)),
		"fixture sanity: act 2 would not have owned it on its own")
	second.adopt([], first.board_snapshot()["cells"])
	assert_true(second.cell_is_unlocked(int(target.x), int(target.y)),
		"ground bought in an earlier act stays bought")

func test_an_opening_act_inherits_nothing() -> void:
	# Chains must not leak into each other: act 1 of a board is always a fresh
	# start, whatever the previous chain ended with.
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		var continues := bool(db.engagement.get("carries_forward", false))
		if str(level["engagement"]).ends_with("act1"):
			assert_false(continues, "%s opens a chain and must not carry" % level["name"])
		else:
			assert_true(continues, "%s continues a chain and should carry" % level["name"])

func test_a_carried_board_still_replays_identically() -> void:
	# Carry-over is applied before any command runs, so it is part of the state a
	# replay starts from. If it were not, every chained act would desync.
	var source := SimFixture.for_level("highway_01", "highway_act1")
	SimFixture.run_greedy(source)
	var snapshot := source.board_snapshot()

	var recorded := SimFixture.for_level("highway_01", "highway_act2", 999)
	recorded.adopt(snapshot["platforms"], snapshot["cells"])
	var log := SimFixture.run_greedy(recorded)

	var replayed := SimFixture.for_level("highway_01", "highway_act2", 999)
	replayed.adopt(snapshot["platforms"], snapshot["cells"])
	SimFixture.replay(replayed, log)
	assert_eq(replayed.state_hash(), recorded.state_hash(), "a chained act replays bit-exactly")

## What "the extension heads into fresh ground" means, measured rather than
## asserted in a comment.
##
## Carrying a board forward is only worth anything if the new stretch of road
## does not run back through where you already built. Sample the whole band an
## act could legally build in, then check how much of it the *next* act's longer
## corridor invalidates. Some loss is inherent - the two spots either side of the
## old end-of-route are now beside a road that continues - but a route that
## doubled back would wipe out a large slice, and that is what this catches.
const DOUBLE_BACK_TOLERANCE := 0.04

func test_extending_a_route_does_not_run_it_through_your_board() -> void:
	# Pure geometry, no simulation, so this covers every board rather than a
	# sample.
	var acts := Database.load_levels()
	for i in acts.size() - 1:
		if str(acts[i]["map"]) != str(acts[i + 1]["map"]):
			continue
		var here := SimFixture.for_level(str(acts[i]["map"]), str(acts[i]["engagement"]))
		var next := SimFixture.for_level(str(acts[i + 1]["map"]), str(acts[i + 1]["engagement"]))
		var offset := (here.build_min_distance() + here.build_max_distance()) * 0.5
		var legal := 0
		var lost := 0
		var prog := 0.0
		while prog <= here.path_length():
			for side: float in [-1.0, 1.0]:
				here.sample_for_render(prog, offset * side)
				var x := here.out_x()
				var y := here.out_y()
				if here.distance_to_path(x, y) < here.build_min_distance():
					continue  # not buildable in this act either
				legal += 1
				if next.distance_to_path(x, y) < next.build_min_distance():
					lost += 1
			prog += 40.0
		assert_gt(float(legal), 20.0,
			"%s should have a band to build in" % str(acts[i]["engagement"]))
		assert_lte(float(lost), float(legal) * DOUBLE_BACK_TOLERANCE,
			"%s loses %d of %d build spots when the route extends - the extension is doubling back over the board"
				% [str(acts[i]["engagement"]), lost, legal])

func _first_offerable(sim: Sim) -> Vector2i:
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			if sim.can_buy_cell(cx, cy):
				return Vector2i(cx, cy)
	return Vector2i(-1, -1)
