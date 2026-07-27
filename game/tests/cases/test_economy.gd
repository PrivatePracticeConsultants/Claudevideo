extends TestCase

## Capital is engagement-scoped and integer-valued. Section 4.1's structural
## squeeze - income growing at 1.05 against HP at 1.10 - only means anything if
## the numbers actually come from the data files, so these tests read the
## expected values out of JSON rather than hardcoding them.

func test_starting_capital_and_integrity_come_from_data() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	assert_eq(sim.capital(), int(db.economy["starting_capital"]), "capital is loaded, not hardcoded")
	assert_eq(sim.integrity(), int(db.economy["starting_integrity"]), "integrity is loaded, not hardcoded")

func test_placing_a_platform_costs_its_listed_price() -> void:
	var sim := SimFixture.fresh()
	var bp := sim.blueprint_index("ballistic")
	var before := sim.capital()
	var cost := sim.blueprint_cost(bp)
	sim.queue_place(0, 0, bp)
	sim.step()
	assert_eq(sim.capital(), before - cost, "capital drops by exactly the cost")
	assert_eq(sim.t_count, 1, "and a platform exists")
	assert_false(sim.pad_is_free(0), "the pad is now occupied")

func test_a_placement_that_cannot_be_afforded_is_rejected_cleanly() -> void:
	var sim := SimFixture.fresh()
	var bp := sim.blueprint_index("ballistic")
	var affordable := int(sim.capital() / sim.blueprint_cost(bp))
	for pad in affordable + 3:
		sim.queue_place(0, pad, bp)
	sim.step()
	assert_eq(sim.t_count, affordable, "only what could be paid for was built")
	assert_eq(sim.rejected_commands(), 3, "the rest were rejected, not silently dropped")
	assert_gte(float(sim.capital()), 0.0, "capital never goes negative")

func test_a_pad_cannot_be_double_occupied() -> void:
	var sim := SimFixture.fresh()
	var bp := sim.blueprint_index("ballistic")
	sim.queue_place(0, 5, bp)
	sim.queue_place(1, 5, bp)
	sim.step()
	var after_first := sim.capital()
	sim.step()
	assert_eq(sim.t_count, 1, "the second placement is refused")
	assert_eq(sim.capital(), after_first, "and costs nothing")
	assert_eq(sim.rejected_commands(), 1, "and is counted")

func test_out_of_range_placements_are_rejected() -> void:
	var sim := SimFixture.fresh()
	var bp := sim.blueprint_index("ballistic")
	sim.queue_place(0, -1, bp)
	sim.queue_place(0, 9999, bp)
	sim.queue_place(0, 0, 9999)
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
