extends TestCase

## Capital is engagement-scoped and integer-valued. Section 4.1's structural
## squeeze - income growing at 1.05 against HP at 1.10 - only means anything if
## the numbers actually come from the data files, so these tests read the
## expected values out of JSON rather than hardcoding them.

## A spot beside the opening straight of highway_01, and a second one clear of it.
const SITE_X := 120.0
const SITE_Y := 430.0

func test_starting_capital_and_integrity_come_from_data() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	assert_eq(sim.capital(), int(db.engagement["starting_capital"]), "capital is loaded, not hardcoded")
	assert_eq(sim.integrity(), int(db.economy["starting_integrity"]), "integrity is loaded, not hardcoded")

func test_an_engagement_can_override_starting_capital() -> void:
	# Act II and III start richer, per section 4.1, so a tier-4 platform is
	# reachable in a single fight by act three.
	var act1 := SimFixture.for_level("highway_01", "highway_01_act1")
	var act3 := SimFixture.for_level("capital_01", "capital_01_act3")
	assert_gt(float(act3.capital()), float(act1.capital()), "later acts start with more Capital")

func test_placing_a_platform_costs_its_listed_price() -> void:
	var sim := SimFixture.fresh()
	var before := sim.capital()
	var cost := sim.blueprint_cost(0)
	sim.queue_place(0, int(SITE_X), int(SITE_Y), 0)
	sim.step()
	assert_eq(sim.capital(), before - cost, "capital drops by exactly the cost")
	assert_eq(sim.t_count, 1, "and a platform exists")

func test_a_placement_that_cannot_be_afforded_is_rejected_cleanly() -> void:
	var sim := SimFixture.fresh()
	var sites := SimFixture.candidate_sites(sim)
	var affordable := int(sim.capital() / sim.blueprint_cost(0))
	var issued := 0
	var i := 0
	while issued < affordable + 3 and i + 1 < sites.size():
		sim.queue_place(0, sites[i], sites[i + 1], 0)
		issued += 1
		i += 2
	sim.step()
	assert_eq(sim.t_count, affordable, "only what could be paid for was built")
	assert_eq(sim.rejected_commands(), 3, "the rest were rejected, not silently dropped")
	assert_gte(float(sim.capital()), 0.0, "capital never goes negative")

func test_building_on_the_road_is_refused() -> void:
	var sim := SimFixture.fresh()
	# Straight onto the first waypoint - squarely in the middle of the corridor.
	var verdict := sim.can_build_at(sim.waypoint_x(1), sim.waypoint_y(1), 0)
	assert_eq(verdict, Sim.BUILD_ON_PATH, "the road itself is not buildable")

func test_building_far_from_the_road_is_refused() -> void:
	var sim := SimFixture.fresh()
	var far := sim.build_max_distance() + 40.0
	sim.sample_for_render(sim.path_length() * 0.5, far)
	assert_eq(sim.can_build_at(sim.out_x(), sim.out_y(), 0), Sim.BUILD_TOO_FAR,
		"platforms have to be adjacent to the corridor, not anywhere on the map")

func test_out_of_bounds_placements_are_rejected() -> void:
	var sim := SimFixture.fresh()
	sim.queue_place(0, -5000, -5000, 0)
	sim.queue_place(0, 99999, 99999, 0)
	sim.queue_place(0, int(SITE_X), int(SITE_Y), 9999)
	sim.step()
	assert_eq(sim.t_count, 0, "nothing was built from nonsense input")
	assert_eq(sim.rejected_commands(), 3, "each bad command was counted")

func test_a_kill_pays_its_bounty() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(0)
	var before := sim.capital()
	var bounty := sim.e_bounty[0]
	assert_gt(float(bounty), 0.0, "wave 1 bounty is set")
	sim._damage_enemy(0, sim.e_hp[0])
	assert_eq(sim.capital(), before + bounty, "the kill paid exactly its bounty")
	assert_eq(sim.kills(), 1, "and counted as a kill")

func test_partial_damage_pays_nothing() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(0)
	var before := sim.capital()
	sim._damage_enemy(0, 1)
	assert_eq(sim.capital(), before, "wounding is not killing")
	assert_eq(sim.e_live_count, 1, "the enemy is still alive")

func test_hp_and_bounty_both_scale_with_the_wave() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(0)
	var hp_wave_1 := sim.e_hp[0]
	var bounty_wave_1 := sim.e_bounty[0]
	sim._despawn_enemy(0)
	sim._begin_wave(9)
	sim._spawn(0)
	assert_gt(float(sim.e_hp[0]), float(hp_wave_1), "wave 10 enemies are tougher")
	assert_gt(float(sim.e_bounty[0]), float(bounty_wave_1), "and worth more")

func test_hp_outgrows_bounty() -> void:
	# The squeeze from section 4.1: if income ever kept pace with HP, late waves
	# would stop forcing decisions and the engagement would flatten out.
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(0)
	var hp_ratio := float(sim.e_hp[0])
	var bounty_ratio := float(sim.e_bounty[0])
	sim._despawn_enemy(0)
	sim._begin_wave(9)
	sim._spawn(0)
	hp_ratio = float(sim.e_hp[0]) / hp_ratio
	bounty_ratio = float(sim.e_bounty[0]) / bounty_ratio
	assert_gt(hp_ratio, bounty_ratio, "HP must outrun income across the engagement")
