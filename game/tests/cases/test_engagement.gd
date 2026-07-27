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
	var bp := sim.blueprint_index("ballistic")
	var next_pad := 0
	var integrity_entering_wave_8 := -1
	while not sim.is_over() and sim.tick() < SimFixture.MAX_TICKS:
		if next_pad < sim.pad_count() and sim.capital() >= sim.blueprint_cost(bp):
			sim.queue_place(sim.tick(), next_pad, bp)
			next_pad += 1
		sim.step()
		if sim.wave_number() == 8 and integrity_entering_wave_8 < 0:
			integrity_entering_wave_8 = sim.integrity()
	assert_eq(integrity_entering_wave_8, 100, "waves 1-7 should cost a competent player nothing")
	assert_lt(float(sim.integrity()), 100.0, "but the last three waves must actually bite")
	assert_eq(sim.result(), Sim.RESULT_WIN, "...without being unwinnable")

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
