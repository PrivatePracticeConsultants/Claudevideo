extends TestCase

## The wave director reads its whole schedule from JSON. These tests check it
## spawns exactly what the file says, in the order the file says, and that the
## wave state machine cannot skip or stall.

func test_wave_count_comes_from_the_data_file() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	assert_eq(sim.wave_count(), (db.engagement["waves"] as Array).size(), "wave count is not hardcoded")

func test_nothing_spawns_during_the_opening_build_window() -> void:
	var db := SimFixture.database()
	var delay := int(db.engagement["inter_wave_delay_ticks"])
	var sim := Sim.new(db, 1)
	for _i in delay - 1:
		sim.step()
	assert_eq(sim.e_live_count, 0, "the player gets the full build window before wave 1")
	assert_eq(sim.wave_number(), 0, "no wave has started yet")

func test_each_wave_spawns_exactly_what_the_file_lists() -> void:
	var db := SimFixture.database()
	var waves: Array = db.engagement["waves"]
	var sim := Sim.new(db, 1)
	# Run undefended so nothing dies early; every spawn survives to be counted
	# via kills + leaks.
	var seen_per_wave := PackedInt32Array()
	seen_per_wave.resize(waves.size())
	var previous_total := 0
	var guard := 0
	while not sim.is_over() and guard < SimFixture.MAX_TICKS:
		sim.step()
		guard += 1
		var wave := sim.wave_number() - 1
		if wave >= 0:
			var total := sim.kills() + sim.leaks() + sim.e_live_count
			seen_per_wave[wave] += total - previous_total
			previous_total = total
	# The undefended run ends early, so only check the waves it actually reached.
	for w in sim.wave_number() - 1:
		var expected := 0
		for group in ((waves[w] as Dictionary)["groups"] as Array):
			expected += int((group as Dictionary)["count"])
		assert_eq(seen_per_wave[w], expected, "wave %d spawned the wrong number" % (w + 1))

func test_a_wave_does_not_begin_while_the_previous_one_is_still_on_the_board() -> void:
	var sim := SimFixture.fresh()
	var last_wave := 0
	var guard := 0
	while not sim.is_over() and guard < SimFixture.MAX_TICKS:
		sim.step()
		guard += 1
		if sim.wave_number() != last_wave:
			if last_wave > 0:
				assert_eq(sim.e_live_count, 0, "wave %d began with enemies still alive" % sim.wave_number())
			last_wave = sim.wave_number()

func test_waves_advance_one_at_a_time() -> void:
	var sim := SimFixture.fresh()
	var sites := SimFixture.candidate_sites(sim)
	var next_site := 0
	var last_wave := 0
	var guard := 0
	while not sim.is_over() and guard < SimFixture.MAX_TICKS:
		if next_site + 1 < sites.size() \
				and sim.can_build_at(float(sites[next_site]), float(sites[next_site + 1]), 0) == Sim.BUILD_OK:
			sim.queue_place(sim.tick(), sites[next_site], sites[next_site + 1], 0)
			next_site += 2
		sim.step()
		guard += 1
		assert_lte(float(sim.wave_number() - last_wave), 1.0, "waves must not skip")
		assert_gte(float(sim.wave_number()), float(last_wave), "waves must not go backwards")
		last_wave = sim.wave_number()
	assert_eq(last_wave, sim.wave_count(), "a won engagement reaches the final wave")

func test_the_engagement_ends_rather_than_running_forever() -> void:
	var sim := SimFixture.fresh()
	var ticks: int = SimFixture.run_greedy(sim)["ticks"]
	assert_lt(float(ticks), float(SimFixture.MAX_TICKS), "the wave director terminated on its own")
	assert_true(sim.is_over(), "and reached a terminal state")

func test_start_delays_within_a_wave_are_honoured() -> void:
	# Wave 9 and 10 each have a second group on a delay; the delayed group must
	# not appear at the same instant as the first.
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(8)
	var groups: Array = ((db.engagement["waves"] as Array)[8] as Dictionary)["groups"]
	assert_eq(groups.size(), 2, "fixture sanity: wave 9 has a delayed second group")
	var delay := int((groups[1] as Dictionary)["start_delay_ticks"])
	assert_gt(float(delay), 0.0, "fixture sanity: the second group is delayed")
	var first_group_count := int((groups[0] as Dictionary)["count"])
	var spawned_before_delay := 0
	for _i in delay:
		sim._update_wave_director()
		sim._tick += 1
		spawned_before_delay = sim.kills() + sim.leaks() + sim.e_live_count
	assert_lte(float(spawned_before_delay), float(first_group_count),
		"the delayed group must not spawn early")
