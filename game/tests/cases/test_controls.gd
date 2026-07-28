extends TestCase

## Sell, call-wave-early, and the two mechanics the newest classes introduced.
##
## All four are player-facing conveniences, and all four are simulation state -
## which means every one of them is a chance to break replays. That is most of
## what this file checks.

func test_selling_refunds_part_of_what_was_spent() -> void:
	var sim := SimFixture.fresh()
	var spot := SimFixture.a_site(sim)
	var cost := sim.blueprint_cost(0)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	var after_build := sim.capital()
	var refund := sim.sell_value(0)
	assert_gt(float(refund), 0.0, "selling pays something back")
	assert_lt(float(refund), float(cost),
		"but less than it cost, or relocating is free and placement stops mattering")
	sim.queue_sell(sim.tick(), 0)
	sim.step()
	assert_eq(sim.t_count, 0, "the turret is gone")
	assert_eq(sim.capital(), after_build + refund, "and the refund landed exactly")

func test_a_sold_turret_does_not_take_its_neighbours_with_it() -> void:
	# The pool is compacted by moving the last turret into the freed slot. If that
	# went wrong it would not crash - it would quietly delete or duplicate a
	# turret, which is the kind of thing you only notice two levels later.
	var sim := SimFixture.fresh()
	var kept := []
	for n in 3:
		var spot := SimFixture.a_site(sim, n * 4)
		sim.queue_place(0, spot[0], spot[1], 0)
		kept.append([float(spot[0]), float(spot[1])])
	sim.step()
	assert_eq(sim.t_count, 3, "fixture sanity: three turrets went down")
	var doomed_x := sim.t_x[0]
	sim.queue_sell(sim.tick(), 0)
	sim.step()
	assert_eq(sim.t_count, 2, "one fewer")
	for i in sim.t_count:
		assert_ne(sim.t_x[i], doomed_x, "the sold one is not still on the board")
		assert_eq(sim.t_used[i], 1, "and every remaining slot is live")

func test_selling_nothing_is_rejected_rather_than_crashing() -> void:
	var sim := SimFixture.fresh()
	sim.queue_sell(0, 0)      # nothing built yet
	sim.queue_sell(0, 99999)  # nowhere near the pool
	sim.step()
	assert_eq(sim.t_count, 0, "nothing happened")
	assert_eq(sim.rejected_commands(), 2, "and both were counted, not swallowed")

func test_calling_a_wave_early_pays_for_the_time_given_up() -> void:
	var sim := SimFixture.fresh()
	# Run to the gap between wave one and wave two.
	var guard := 0
	while not sim.can_send_wave() and guard < SimFixture.MAX_TICKS:
		sim.step()
		guard += 1
	assert_true(sim.can_send_wave(), "fixture sanity: reached a gap between waves")
	var bonus := sim.send_wave_bonus()
	var before := sim.capital()
	assert_gt(float(bonus), 0.0, "calling in early is worth something")
	sim.queue_send_wave(sim.tick())
	sim.step()
	assert_eq(sim.capital(), before + bonus, "and it paid exactly that")
	assert_false(sim.can_send_wave(), "the gap is spent")

func test_a_wave_cannot_be_called_while_one_is_running() -> void:
	# An engagement opens in the gap before wave one, so calling in at tick zero
	# is legal and skips that opening pause. What must be refused is calling one
	# in while drones are already arriving.
	var sim := SimFixture.fresh()
	var guard := 0
	while sim.can_send_wave() and guard < SimFixture.MAX_TICKS:
		sim.step()
		guard += 1
	assert_false(sim.can_send_wave(), "fixture sanity: a wave is now running")
	var before := sim.capital()
	sim.queue_send_wave(sim.tick())
	sim.step()
	assert_eq(sim.capital(), before,
		"calling a wave that is already running pays nothing")
	assert_gt(float(sim.rejected_commands()), 0.0, "and is counted as refused")

func test_sells_and_early_calls_replay_bit_exactly() -> void:
	# The whole reason these went through the command log rather than mutating the
	# sim directly.
	var a := SimFixture.fresh(8080)
	var b := SimFixture.fresh(8080)
	var log_tick := PackedInt32Array()
	var log_kind := PackedInt32Array()
	var log_a := PackedInt32Array()
	var log_b := PackedInt32Array()
	var log_c := PackedInt32Array()
	var sites := SimFixture.candidate_sites(a)
	for n in 6:
		log_tick.append(n * 3); log_kind.append(Sim.CMD_PLACE)
		log_a.append(sites[n * 8]); log_b.append(sites[n * 8 + 1]); log_c.append(0)
	# Sell two of them, upgrade another, and call a wave in.
	for entry in [[40, Sim.CMD_SELL, 1], [55, Sim.CMD_UPGRADE, 0], [70, Sim.CMD_SELL, 2]]:
		log_tick.append(int(entry[0])); log_kind.append(int(entry[1]))
		log_a.append(int(entry[2])); log_b.append(0); log_c.append(0)
	log_tick.append(120); log_kind.append(Sim.CMD_SEND_WAVE)
	log_a.append(0); log_b.append(0); log_c.append(0)
	var log := {"tick": log_tick, "kind": log_kind, "a": log_a, "b": log_b, "c": log_c}

	SimFixture.replay(a, log)
	SimFixture.replay(b, log)
	assert_gt(float(a.sold()), 0.0, "fixture sanity: turrets were actually sold")
	assert_eq(b.state_hash(), a.state_hash(), "a log containing sells replays identically")
	assert_eq(b.capital(), a.capital(), "same Capital")

func test_the_breaker_shrugs_off_suppression() -> void:
	# The reason the class exists: once slowing everything was possible, slowing
	# everything was the answer to everything.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("breaker"))
	sim._spawn(sim.enemy_index("walker"))
	sim._suppress(0, 0.4, 60)
	sim._suppress(1, 0.4, 60)
	assert_gt(sim.e_slow_factor[0], sim.e_slow_factor[1],
		"the same hit slows a Breaker far less than a Walker")
	assert_gt(sim.e_slow_factor[0], 0.8,
		"it keeps most of its speed through a strong suppression")

func test_a_railgun_round_damages_a_whole_line() -> void:
	# Pierce is the Railgun's entire reason to exist: single-target damage at that
	# fire rate would just be a worse Ballistic.
	var sim := SimFixture.fresh()
	var rail := sim.blueprint_index("railgun")
	assert_gte(float(rail), 0.0, "the Railgun is defined")
	assert_gt(sim.blueprint_pierce(rail), 0.0, "and its rounds pierce")
	sim._begin_wave(0)
	var walker := sim.enemy_index("walker")
	for n in 4:
		sim._spawn(walker)
		sim.e_x[n] = 500.0 + float(n) * 60.0
		sim.e_y[n] = 500.0
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())
	var before := []
	for n in 4:
		before.append(sim.e_hp[n])
	# Fire straight down the line they are standing in.
	sim._lance_through(400.0, 500.0, 740.0, 500.0, 26.0, 5)
	var hit := 0
	for n in 4:
		if sim.e_hp[n] < before[n]:
			hit += 1
	assert_eq(hit, 4, "every drone in the lane took the hit")

func test_pierce_spares_anything_off_the_line() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	sim.e_x[0] = 600.0
	sim.e_y[0] = 900.0
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())
	var before := sim.e_hp[0]
	sim._lance_through(400.0, 500.0, 800.0, 500.0, 26.0, 50)
	assert_eq(sim.e_hp[0], before, "a drone 400 units off the lane is untouched")
