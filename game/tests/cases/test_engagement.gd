extends TestCase

## The P0 acceptance gate's first half: a ten-wave engagement can be played from
## start to finish, and can be both won and lost.

func test_a_competent_run_wins_the_full_ten_waves() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim)
	assert_eq(sim.result(), Sim.RESULT_WIN, "filling every pad should clear the engagement")
	assert_eq(sim.wave_number(), sim.wave_count(), "all ten waves were reached")
	assert_eq(sim.wave_count(), 10, "the P0 engagement is ten waves")
	assert_gt(float(sim.integrity()), 0.0, "a win means integrity never hit zero")
	assert_eq(sim.e_live_count, 0, "a win means the board is clear")

func test_building_nothing_loses() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_idle(sim)
	assert_eq(sim.result(), Sim.RESULT_LOSS, "an undefended corridor must fail")
	assert_eq(sim.integrity(), 0, "integrity floors at zero rather than going negative")
	assert_eq(sim.kills(), 0, "nothing was built, so nothing died")
	assert_lt(float(sim.wave_number()), 10.0, "the run ends before the last wave")

func test_the_difficulty_spike_lands_in_the_last_three_waves() -> void:
	# Section 4.2: losses must cluster late because the waves are authored that
	# way, not because of any hidden difficulty adjustment. Concretely, a
	# competent run should take no damage at all until the back third.
	var sim := SimFixture.fresh()
	var log := SimFixture.run_greedy(sim)
	var integrity_at_wave: PackedInt32Array = log["integrity_at_wave"]
	assert_eq(sim.result(), Sim.RESULT_WIN, "the level is winnable by a competent run")
	assert_gte(float(integrity_at_wave.size()), 8.0, "the run reached at least wave 8")
	assert_eq(integrity_at_wave[7], 100, "waves 1-7 should cost a competent player nothing")
	assert_lt(float(sim.integrity()), 100.0, "but the last three waves must actually bite")

func test_upgrading_is_what_carries_the_later_levels() -> void:
	# The deployment limit exists so the tier ladder matters. If a level could be
	# cleared by tier-1 spam alone, upgrades would be dead content - so check the
	# competent run actually reaches higher tiers.
	var sim := SimFixture.for_level("port_01", "port_01_act2")
	SimFixture.run_greedy(sim)
	assert_eq(sim.result(), Sim.RESULT_WIN, "act II is winnable")
	var highest := 0
	for i in sim.t_count:
		highest = maxi(highest, sim.platform_tier(i))
	assert_gt(float(highest), 0.0, "a winning act II run upgrades past tier 1")
	assert_lte(float(sim.t_count), float(sim.platform_limit()), "and respects the deployment limit")

func test_every_campaign_level_is_winnable_and_losable() -> void:
	for level in Database.load_levels():
		var name := str(level["name"])
		var won := SimFixture.for_level(str(level["map"]), str(level["engagement"]))
		SimFixture.run_greedy(won)
		assert_eq(won.result(), Sim.RESULT_WIN, "%s must be winnable by a competent run" % name)
		var lost := SimFixture.for_level(str(level["map"]), str(level["engagement"]))
		SimFixture.run_idle(lost)
		assert_eq(lost.result(), Sim.RESULT_LOSS, "%s must be losable by an idle one" % name)

func test_every_spawned_enemy_is_accounted_for() -> void:
	# The honesty rule applied to the sim: an enemy either dies or leaks. If
	# these ever fail to add up, something is being silently dropped.
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim)
	var expected := 0
	for wave in (SimFixture.database().engagement["waves"] as Array):
		for group in ((wave as Dictionary)["groups"] as Array):
			expected += int((group as Dictionary)["count"])
	assert_eq(sim.kills() + sim.leaks(), expected, "kills + leaks must equal everything spawned")
	assert_eq(sim.spawn_overflow(), 0, "no spawn was dropped for lack of pool space")

func test_a_loss_stops_the_simulation() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_idle(sim)
	var frozen := sim.state_hash()
	var ended_on := sim.tick()
	for _i in 100:
		sim.step()
	assert_eq(sim.tick(), ended_on, "stepping a finished engagement must do nothing")
	assert_eq(sim.state_hash(), frozen, "and must not mutate state")
