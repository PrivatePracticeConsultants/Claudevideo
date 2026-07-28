extends TestCase

## The Arc Suppressor: the first weapon family that is not primarily about
## damage.
##
## It does a fraction of a Ballistic's damage and instead drags everything in a
## small blast radius down to a fraction of its speed. That is a force
## multiplier - every other turret on the board gets more time on target - which
## makes it the direct answer to the Vanguard Lance, whose whole threat is that
## it crosses a firing arc too fast to be killed in it.

func _suppressor(sim: Sim) -> int:
	return sim.blueprint_index("suppressor")

func test_the_family_exists_and_trades_damage_for_suppression() -> void:
	var sim := SimFixture.fresh()
	var arc := _suppressor(sim)
	assert_gte(float(arc), 0.0, "the Arc Suppressor is defined")
	assert_lt(sim.blueprint_slow_factor(arc), 1.0, "and it actually slows")
	assert_eq(sim.blueprint_slow_factor(sim.blueprint_index("ballistic")), 1.0,
		"while Ballistic does not")
	assert_eq(sim.blueprint_slow_factor(sim.blueprint_index("cannon")), 1.0,
		"and neither does Cannon")

func test_a_suppressed_drone_actually_moves_slower() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	sim._spawn(sim.enemy_index("walker"))
	# One suppressed, one left alone, then step both and compare ground covered.
	sim._suppress(0, 0.5, 30)
	var before_a := sim.e_prog[0]
	var before_b := sim.e_prog[1]
	sim._advance_enemies()
	var moved_slow := sim.e_prog[0] - before_a
	var moved_free := sim.e_prog[1] - before_b
	assert_gt(float(moved_free), 0.0, "the free drone moved")
	assert_almost_eq(moved_slow, moved_free * 0.5, 0.0001,
		"the suppressed one covered exactly half the ground")

func test_suppression_wears_off() -> void:
	# An effect with no expiry is a permanent debuff wearing a duration's clothes.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	sim._suppress(0, 0.5, 3)
	for _i in 3:
		sim._advance_enemies()
	assert_eq(sim.e_slow_ticks[0], 0, "the timer ran out")
	assert_eq(sim.e_slow_factor[0], 1.0, "and the factor was released")
	var before := sim.e_prog[0]
	sim._advance_enemies()
	assert_almost_eq(sim.e_prog[0] - before, sim.e_speed[0], 0.0001,
		"so it is back to full speed")

func test_slows_refresh_rather_than_stack() -> void:
	# Stacking multiplicatively would let a cluster of Suppressors pin a wave in
	# place indefinitely, which turns one turret into the answer to everything.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	for _i in 8:
		sim._suppress(0, 0.6, 30)
	assert_almost_eq(sim.e_slow_factor[0], 0.6, 0.0001,
		"eight hits are worth exactly as much as one")

func test_the_strongest_slow_wins_and_re_arms_the_timer() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	sim._suppress(0, 0.8, 10)
	sim._suppress(0, 0.4, 40)
	assert_almost_eq(sim.e_slow_factor[0], 0.4, 0.0001, "the stronger slow took over")
	assert_eq(sim.e_slow_ticks[0], 40, "with its own duration")
	# A weaker hit afterwards must not dilute the stronger one already running.
	sim._suppress(0, 0.9, 60)
	assert_almost_eq(sim.e_slow_factor[0], 0.4, 0.0001,
		"a weaker hit does not weaken what is already on it")
	assert_eq(sim.e_slow_ticks[0], 60, "but it does re-arm the timer")

func test_a_recycled_pool_slot_does_not_inherit_suppression() -> void:
	# The generation-stamp bug class, applied to status effects: a drone spawning
	# into a slot that died suppressed must start at full speed.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	sim._suppress(0, 0.3, 500)
	sim._despawn_enemy(0)
	sim._spawn(sim.enemy_index("walker"))
	assert_eq(sim.e_slow_ticks[0], 0, "the new occupant is not slowed")
	assert_eq(sim.e_slow_factor[0], 1.0, "and its speed multiplier is clean")

func test_suppression_is_part_of_the_state_hash() -> void:
	# If it were not, a desync in who is slowed would be invisible to the
	# determinism test - which is the only thing standing between this project
	# and replays that quietly diverge.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var before := sim.state_hash()
	sim._suppress(0, 0.5, 30)
	assert_ne(sim.state_hash(), before, "suppressing a drone changes the state hash")

func test_a_run_using_suppressors_replays_identically() -> void:
	var a := SimFixture.fresh(5150)
	var b := SimFixture.fresh(5150)
	var log := SimFixture.run_greedy(a)
	var built := 0
	for i in a.t_count:
		if a.platform_slow_factor(i) < 1.0:
			built += 1
	assert_gt(float(built), 0.0,
		"fixture sanity: the scripted policy builds Suppressors, so this exercises them")
	SimFixture.replay(b, log)
	assert_eq(b.state_hash(), a.state_hash(), "a run using suppression replays bit-exactly")

func test_suppression_is_what_makes_lances_answerable() -> void:
	# The claim the family exists for, checked rather than asserted: the same
	# level, same seed, built with no Suppressors at all should do no better.
	#
	# Gated to the full-campaign pass. It plays a late engagement twice, and the
	# levels that field Lances are 100+ turret boards - enough to push the whole
	# suite past its wall-clock budget on its own. Everything mechanical about
	# suppression is covered by the always-on tests above; this is a balance
	# claim, and balance claims are what the pre-release pass is for.
	if not SimFixture.full_campaign_requested():
		return
	var level := _first_level_with_lances()
	assert_false(level.is_empty(), "fixture sanity: some act fields Lances")
	var mixed := SimFixture.for_level(str(level["map"]), str(level["engagement"]))
	SimFixture.run_greedy(mixed)
	var without := SimFixture.for_level(str(level["map"]), str(level["engagement"]))
	_build_without_suppressors(without)
	assert_gte(float(mixed.integrity()), float(without.integrity()),
		"an arsenal including suppression should not do worse against Lances")

func _first_level_with_lances() -> Dictionary:
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		for wave in (db.engagement["waves"] as Array):
			for group in ((wave as Dictionary)["groups"] as Array):
				if str((group as Dictionary)["enemy"]) == "lance":
					return level
	return {}

func _build_without_suppressors(sim: Sim) -> void:
	var sites := SimFixture.candidate_sites(sim)
	var next_site := 0
	var ticks := 0
	var ballistic := sim.blueprint_index("ballistic")
	while not sim.is_over() and ticks < SimFixture.MAX_TICKS:
		if sim.t_count < sim.platform_limit() and next_site + 1 < sites.size():
			if sim.can_build_at(float(sites[next_site]), float(sites[next_site + 1]), ballistic) == Sim.BUILD_OK:
				sim.queue_place(sim.tick(), sites[next_site], sites[next_site + 1], ballistic)
				next_site += 2
		else:
			for i in sim.t_count:
				if sim.can_upgrade(i):
					sim.queue_upgrade(sim.tick(), i)
					break
		sim.step()
		ticks += 1
