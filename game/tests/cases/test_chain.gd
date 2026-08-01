extends TestCase

## Boards that hold across a chain of acts, keeping what you built.
##
## An act inherits the previous act's turrets and ground, and the board itself -
## road, deployment limit, owned ground - is the same board every act. What
## escalates is what walks it.
##
## It did not start that way. Acts used to reveal a longer prefix of the map's
## route and raise the deployment limit as they went, so a later act was harder
## partly because the board had moved out from under the line you had already
## built. That was reported as not feeling like difficulty: holding meant
## stretching rather than reinforcing. The reveal is gone; head-count and health
## carry the escalation on their own.

func _chain() -> Array:
	# Three acts of the opening board.
	return ["highway_act1", "highway_act2", "highway_act3"]

func test_every_act_of_a_board_runs_the_same_road() -> void:
	# The replacement for "each act reveals more". A carried turret is useful in
	# act 3 because act 3's road is act 1's road, not a superset of it.
	var first := SimFixture.for_level("highway_01", _chain()[0])
	for eid in _chain():
		var sim := SimFixture.for_level("highway_01", eid)
		assert_almost_eq(sim.path_length(), first.path_length(), 0.0001,
			"%s runs a different length of road" % eid)
		assert_eq(sim.revealed_waypoints(), first.revealed_waypoints(),
			"%s opens a different number of waypoints" % eid)
		assert_eq(sim.platform_limit(), first.platform_limit(),
			"%s deploys a different number of turrets" % eid)

func test_the_escalation_is_in_the_waves() -> void:
	# What is left to get harder, checked on every chain in the campaign rather
	# than a sample: a later act brings more drones or tougher ones, and never
	# fewer of both. This is the whole mechanic, so it is asserted, not assumed.
	var levels := Database.load_levels()
	for i in levels.size() - 1:
		if str(levels[i]["map"]) != str(levels[i + 1]["map"]):
			continue
		var here := Database.load_engagement(str(levels[i]["map"]), str(levels[i]["engagement"]))
		var next := Database.load_engagement(str(levels[i + 1]["map"]), str(levels[i + 1]["engagement"]))
		# As the acts are actually PLAYED: affixes multiply health and head-count
		# at load, so an act whose raw hp multiplier dipped but whose Resilient
		# affix more than makes it up really is the tougher act. Comparing the raw
		# file numbers here once failed a finale that was measurably the hardest
		# act in the game.
		var grew_count := _drone_count(next) > _drone_count(here)
		var grew_health := _effective_hp(next) > _effective_hp(here)
		assert_true(grew_count or grew_health,
			"%s is no harder than the act before it - %d drones at x%.2f health against %d at x%.2f"
				% [str(levels[i + 1]["engagement"]), _drone_count(next),
					_effective_hp(next), _drone_count(here), _effective_hp(here)])

func _effective_hp(db: Database) -> float:
	var total := float(db.engagement.get("act_hp_multiplier", 1.0))
	for id: Variant in (db.engagement.get("affixes", []) as Array):
		total *= maxf(0.0, float((db.affixes.get(str(id), {}) as Dictionary)
			.get("hp_multiplier", 1.0)))
	return total

func _drone_count(db: Database) -> int:
	var total := 0
	var mult := 1.0
	for id: Variant in (db.engagement.get("affixes", []) as Array):
		mult *= maxf(0.0, float((db.affixes.get(str(id), {}) as Dictionary)
			.get("count_multiplier", 1.0)))
	for wave: Dictionary in (db.engagement["waves"] as Array):
		for group: Dictionary in (wave["groups"] as Array):
			total += maxi(1, int(round(float(group["count"]) * mult)))
	return total

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

func test_every_act_opens_the_whole_route() -> void:
	for eid in _chain():
		var sim := SimFixture.for_level("highway_01", eid)
		assert_eq(sim.revealed_waypoints(), sim.full_waypoints(),
			"%s runs less than the whole route" % eid)

func test_a_carried_board_arrives_intact() -> void:
	var first := SimFixture.for_level("highway_01", "highway_act1")
	SimFixture.run_greedy(first)
	assert_gt(float(first.t_count), 0.0, "fixture sanity: act 1 built something")
	var snapshot := first.board_snapshot()

	var second := SimFixture.for_level("highway_01", "highway_act2")
	second.adopt(snapshot["platforms"], snapshot["cells"])
	assert_eq(second.t_count, first.t_count, "every turret carried forward")
	assert_eq(second.carry_dropped(), 0, "none were dropped by the corridor")
	var upgraded := 0
	for i in second.t_count:
		assert_almost_eq(second.t_x[i], first.t_x[i], 0.0001, "turret %d kept its place" % i)
		assert_eq(second.platform_blueprint(i), first.platform_blueprint(i),
			"turret %d kept its weapon" % i)
		assert_eq(second.platform_tier(i), first.platform_tier(i),
			"turret %d kept its tier" % i)
		if first.platform_tier(i) > 0:
			upgraded += 1
	assert_gt(float(upgraded), 0.0,
		"fixture sanity: act 1 upgraded something, so tier carry is exercised")

func test_an_upgraded_turret_stays_upgraded() -> void:
	# The reversal of the rule this test used to assert. For most of the project a
	# carried turret arrived refitted to tier 1, because an act that inherits a
	# finished board is won by that board with no input at all. That reasoning was
	# sound and the experience was not: "every time I go to a new level it resets
	# the levels of my weapons" - a tier is the most expensive thing a player
	# buys, and buying it knowing it expires is worse than not buying it. The
	# price moved into the waves: acts II-IV are authored against a board that
	# arrives INTACT, and the idle gate now asks that an act be losable from a
	# clean start rather than from an inheritance.
	var sim := SimFixture.for_level("highway_01", "highway_act2")
	var top := sim.blueprint_tier_count(sim.blueprint_index("ballistic")) - 1
	var spot := SimFixture.a_site(sim)
	sim.adopt([{"x": float(spot[0]), "y": float(spot[1]),
		"blueprint": sim.blueprint_index("ballistic"), "tier": top}],
		PackedInt32Array())
	assert_eq(sim.t_count, 1, "the turret carried")
	assert_eq(sim.platform_tier(0), top, "a top-tier turret arrives at top tier")

func test_carrying_a_board_costs_nothing() -> void:
	# Those turrets were paid for in the act that built them. Charging again
	# would make continuing a chain strictly worse than starting one.
	var first := SimFixture.for_level("highway_01", "highway_act1")
	SimFixture.run_greedy(first)
	var second := SimFixture.for_level("highway_01", "highway_act2")
	var before := second.capital()
	second.adopt(first.board_snapshot()["platforms"], PackedInt32Array())
	assert_eq(second.capital(), before, "inherited turrets are free")

func test_carried_turrets_count_against_the_limit() -> void:
	# The deployment limit is the same every act, so an inheritance spends it. A
	# clean slate on top of everything already standing would make the limit
	# meaningless from act 2 onward.
	var first := SimFixture.for_level("highway_01", "highway_act1")
	SimFixture.run_greedy(first)
	var second := SimFixture.for_level("highway_01", "highway_act2")
	second.adopt(first.board_snapshot()["platforms"], PackedInt32Array())
	assert_eq(second.t_count, first.t_count,
		"every turret carries - what an inheritance costs is tiers, not emplacements")
	assert_lte(float(second.t_count), float(second.platform_limit()),
		"and they are counted against the act's own limit")

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
	# "Opens a chain" means "is the first level on its board", not "is named
	# act1" - the name was a proxy that held until Ares Station arrived as a
	# single-act board whose one engagement is called a siege.
	var seen_maps := {}
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		var continues := bool(db.engagement.get("carries_forward", false))
		var opens: bool = not seen_maps.has(str(level["map"]))
		seen_maps[str(level["map"])] = true
		if opens:
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

## Nothing a later act does may invalidate where you were allowed to build.
##
## This was written when acts revealed a longer route and the risk was a route
## that doubled back over the board you had. The road no longer changes, so the
## tolerance should now be met with room to spare - which is exactly why it is
## worth keeping. It is the check that would fail first if a future act ever
## quietly reshaped a board again, and it covers every board rather than a
## sample because it is pure geometry with no simulation in it.
const DOUBLE_BACK_TOLERANCE := 0.04

func test_a_later_act_does_not_run_the_road_through_your_board() -> void:
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
			"%s loses %d of %d build spots to the next act's road - the board reshaped under the line"
				% [str(acts[i]["engagement"]), lost, legal])

func _first_offerable(sim: Sim) -> Vector2i:
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			if sim.can_buy_cell(cx, cy):
				return Vector2i(cx, cy)
	return Vector2i(-1, -1)

# --- crossing to a new board -------------------------------------------------
#
# A chain carries turrets. A board boundary cannot: a coordinate on one board
# means nothing on another, and there is no honest way to move an emplacement
# between maps. What crosses instead is what the board was worth.
#
# This exists because arriving at a new board with nothing to show for the last
# one reads as the game deleting your progress - which is exactly how it was
# reported.

func _board_boundary() -> Array:
	# The first pair of consecutive levels on different maps.
	var levels := Database.load_levels()
	for i in levels.size() - 1:
		if str(levels[i]["map"]) != str(levels[i + 1]["map"]):
			return [levels[i], levels[i + 1]]
	return []

func test_a_finished_board_is_worth_something() -> void:
	var sim := SimFixture.for_level("highway_01", "highway_act1")
	SimFixture.run_greedy(sim)
	assert_gt(float(sim.t_count), 0.0, "fixture sanity: a board was built")
	assert_gt(float(sim.board_salvage()), 0.0, "and it salvages for something")

func test_salvage_crosses_a_board_boundary() -> void:
	var pair := _board_boundary()
	assert_eq(pair.size(), 2, "fixture sanity: the campaign changes board somewhere")
	var finished := SimFixture.for_level(str(pair[0]["map"]), str(pair[0]["engagement"]))
	SimFixture.run_greedy(finished)
	var opened := SimFixture.start_act(pair[1], finished.board_snapshot())
	assert_eq(opened.t_count, 0, "turrets cannot cross to another map")
	assert_gt(float(opened.salvage_granted()), 0.0, "but what they were worth does")

func test_salvage_is_bounded_by_the_new_act_s_own_budget() -> void:
	# Measured before this bound existed: a finished board was worth 8,790 Capital
	# arriving at an act budgeted for 1,300, and 22,330 at one budgeted for 2,350.
	# Unbounded continuity is just deleting the economy.
	var pair := _board_boundary()
	var opened := SimFixture.start_act(pair[1], {"salvage": 9999999})
	assert_gt(float(opened.salvage_ceiling()), 0.0, "there is a ceiling")
	assert_eq(opened.salvage_granted(), opened.salvage_ceiling(),
		"an absurd amount is clamped to it, not banked")
	# And the ceiling is proportional, so it stays meaningful late instead of
	# being decisive early and irrelevant by the end.
	var levels := Database.load_levels()
	var late := SimFixture.start_act(levels[levels.size() - 1], {"salvage": 9999999})
	assert_gt(float(late.salvage_ceiling()), float(opened.salvage_ceiling()),
		"a later board salvages into a bigger budget")

func test_the_first_level_salvages_nothing() -> void:
	# There is no previous board. It must not start richer for that reason.
	var levels := Database.load_levels()
	var opened := SimFixture.start_act(levels[0], {})
	assert_eq(opened.salvage_granted(), 0, "the opening level is handed nothing")

func test_a_weaker_finish_salvages_less() -> void:
	# The point of tying it to the board: it is continuity, not a flat bonus.
	var pair := _board_boundary()
	var strong := SimFixture.for_level(str(pair[0]["map"]), str(pair[0]["engagement"]))
	SimFixture.run_greedy(strong)
	var weak := SimFixture.for_level(str(pair[0]["map"]), str(pair[0]["engagement"]))
	var spot := SimFixture.a_site(weak)
	weak.queue_place(0, spot[0], spot[1], 0)
	weak.step()
	assert_lt(float(weak.board_salvage()), float(strong.board_salvage()),
		"one turret is worth less than a full board")

func test_the_quoted_salvage_is_the_amount_actually_paid() -> void:
	# The end-of-board banner quotes a figure. It shipped quoting the outgoing
	# board's raw worth while the next act paid its own capped share - measured at
	# the first boundary, $8,790 promised against $520 delivered. A number on
	# screen that the next screen contradicts is the honesty rule broken, so the
	# quote is now taken from the receiving act's ceiling.
	var pair := _board_boundary()
	var finished := SimFixture.for_level(str(pair[0]["map"]), str(pair[0]["engagement"]))
	SimFixture.run_greedy(finished)
	var next_db := SimFixture.database(str(pair[1]["map"]), str(pair[1]["engagement"]))
	var quoted := mini(finished.board_salvage(), Sim.salvage_ceiling_of(next_db))
	var opened := SimFixture.start_act(pair[1], finished.board_snapshot())
	assert_eq(quoted, opened.salvage_granted(),
		"what the banner promises is what the next act credits")
	# And again where the cap actually binds, which is the case that was wrong.
	# A single act's fixture board is worth less than the ceiling, so the clamp
	# has to be exercised with a board worth more than any act will take.
	var rich := SimFixture.start_act(pair[1], {"salvage": 9999999})
	assert_eq(mini(9999999, Sim.salvage_ceiling_of(next_db)), rich.salvage_granted(),
		"a board worth more than the ceiling is quoted at the ceiling, not its worth")

func test_the_static_and_instance_ceilings_agree() -> void:
	# Two callers, one formula. If they ever diverge the banner starts lying again
	# and nothing else notices.
	var levels := Database.load_levels()
	for level in [levels[0], levels[levels.size() / 2], levels[levels.size() - 1]]:
		var sim := SimFixture.start_act(level, {})
		var db := SimFixture.database(str(level["map"]), str(level["engagement"]))
		assert_eq(Sim.salvage_ceiling_of(db), sim.salvage_ceiling(),
			"%s quotes its own ceiling" % str(level["engagement"]))

func test_salvage_is_part_of_the_state_hash() -> void:
	var pair := _board_boundary()
	var bare := SimFixture.start_act(pair[1], {})
	var funded := SimFixture.start_act(pair[1], {"salvage": 500})
	assert_ne(funded.state_hash(), bare.state_hash(),
		"opening with salvage is a different starting state")
	assert_eq(funded.capital(), bare.capital() + funded.salvage_granted(),
		"and the Capital arrived")
