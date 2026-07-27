extends TestCase

## Regression tests for the P0 audit.
##
## Every test here failed before its fix. They are grouped in one file on purpose:
## it is the record of what a deliberate attempt to break P0 actually found, and
## it should be read alongside the "Audit" section of DECISIONS.md.

# --- A. degenerate path geometry ---------------------------------------------

func test_a_zero_length_path_segment_is_rejected_at_load() -> void:
	# Was: accepted. The sim normalises each segment by its own length, so a
	# repeated waypoint divided by zero and wrote NaN into the direction table.
	# Whether that NaN reached an enemy depended on where a binary search landed,
	# which is the worst kind of bug - real, and intermittent.
	var db := SimFixture.database()
	var path: Array = db.map["path"]
	path.insert(1, {"x": path[0]["x"], "y": path[0]["y"]})
	db.errors = PackedStringArray()
	db._validate_map()
	assert_false(db.is_valid(), "a repeated waypoint must fail validation")
	assert_true(db.error_text().contains("same place"), "the error explains the problem: %s" % db.error_text())

func test_the_shipped_map_has_no_degenerate_segments() -> void:
	var db := SimFixture.database()
	assert_true(db.is_valid(), "shipped map still loads: %s" % db.error_text())
	var sim := Sim.new(db, 1)
	assert_false(is_nan(sim.path_length()), "path length is a number")
	assert_gt(sim.path_length(), 0.0, "and a positive one")

# --- B. pool exhaustion is counted, never silent -----------------------------

func test_dropped_shots_are_counted() -> void:
	# Was: _fire() returned silently when the pool was full, so a platform could
	# appear to fire and deal no damage with nothing anywhere recording it.
	var db := SimFixture.database()
	db.sim["max_projectiles"] = 3.0
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var spot := SimFixture.a_site(sim)
	sim._try_place(float(spot[0]), float(spot[1]), 0)
	for _i in 10:
		sim._fire(0, 0)
	assert_eq(sim.p_live_count, 3, "the pool filled to its ceiling")
	assert_eq(sim.projectile_overflow(), 7, "and the seven it could not take were counted")

func test_the_shipped_engagement_never_drops_a_shot() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim)
	assert_eq(sim.projectile_overflow(), 0, "max_projectiles is sized correctly for the shipped data")
	assert_eq(sim.spawn_overflow(), 0, "and so is max_enemies")

# --- C. pool integrity under double-resolve ----------------------------------

func test_despawning_an_already_dead_enemy_is_harmless() -> void:
	# Was: an out-of-bounds write. The free list is exactly max_enemies long, so
	# pushing a duplicate wrote one past the end and corrupted the pool.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	sim._despawn_enemy(0)
	sim._despawn_enemy(0)
	sim._despawn_enemy(0)
	assert_eq(sim.e_live_count, 0, "live count stays at zero, never negative")
	# The pool must still work afterwards.
	for _i in 20:
		sim._spawn(sim.enemy_index("walker"))
	assert_eq(sim.e_live_count, 20, "the pool still hands out slots")
	var seen := {}
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			assert_false(seen.has(i), "slot %d handed out twice" % i)
			seen[i] = true
	assert_eq(seen.size(), 20, "and hands out twenty distinct ones")

func test_despawning_an_already_dead_projectile_is_harmless() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var spot := SimFixture.a_site(sim)
	sim._try_place(float(spot[0]), float(spot[1]), 0)
	sim._fire(0, 0)
	sim._despawn_projectile(0)
	sim._despawn_projectile(0)
	assert_eq(sim.p_live_count, 0, "live count stays at zero")

# --- D. command log ordering --------------------------------------------------

func test_commands_queued_out_of_order_still_apply_at_their_own_tick() -> void:
	# Was: the replay cursor only moves forward, so a command appended after one
	# with a later tick was applied at the wrong time - and differently between a
	# live run and its replay, which is exactly what determinism is meant to rule
	# out.
	var sim := SimFixture.fresh()
	var late := SimFixture.a_site(sim, 0)
	var early := SimFixture.a_site(sim, 8)
	sim.queue_place(100, late[0], late[1], 0)
	sim.queue_place(10, early[0], early[1], 0)
	for _i in 20:
		sim.step()
	assert_eq(sim.t_count, 1, "the tick-10 command landed at tick 10, not after the tick-100 one")

func test_out_of_order_and_in_order_logs_produce_the_same_run() -> void:
	var ordered := SimFixture.fresh(31337)
	var first := SimFixture.a_site(ordered, 0)
	var second := SimFixture.a_site(ordered, 8)
	ordered.queue_place(10, second[0], second[1], 0)
	ordered.queue_place(100, first[0], first[1], 0)
	var shuffled := SimFixture.fresh(31337)
	shuffled.queue_place(100, first[0], first[1], 0)
	shuffled.queue_place(10, second[0], second[1], 0)
	for _i in 1500:
		ordered.step()
		shuffled.step()
	assert_eq(shuffled.state_hash(), ordered.state_hash(), "queue order must not change the run")

func test_a_command_for_a_past_tick_is_not_silently_dropped() -> void:
	var sim := SimFixture.fresh()
	for _i in 50:
		sim.step()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(5, spot[0], spot[1], 0)  # already long gone
	sim.step()
	assert_eq(sim.t_count, 1, "it lands on the next tick rather than vanishing")

func test_same_tick_commands_keep_their_insertion_order() -> void:
	# Both target the same pad, so only the first can win. Which one wins has to
	# be stable or replays diverge.
	var sim := SimFixture.fresh()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(10, spot[0], spot[1], 0)
	sim.queue_place(10, spot[0], spot[1], 0)
	for _i in 15:
		sim.step()
	assert_eq(sim.t_count, 1, "the second is refused - it overlaps the first")
	assert_eq(sim.rejected_commands(), 1, "and counted")

# --- E. unknown enemy ids ------------------------------------------------------

func test_an_unknown_enemy_id_does_not_silently_become_enemy_zero() -> void:
	# Was: _type_index() returned 0 for anything it did not recognise, so a typo
	# in a wave file would quietly spawn a different enemy than the one written.
	var sim := SimFixture.fresh()
	assert_eq(sim._type_index("does_not_exist"), -1, "unknown ids resolve to -1")
	assert_gte(float(sim._type_index("walker")), 0.0, "known ids still resolve")

func test_a_wave_group_with_an_unknown_enemy_spawns_nothing_and_is_counted() -> void:
	var db := SimFixture.database()
	var groups: Array = ((db.engagement["waves"] as Array)[0] as Dictionary)["groups"]
	(groups[0] as Dictionary)["enemy"] = "ghost"
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	assert_eq(sim.unknown_enemy_groups(), 1, "the bad group was counted at wave start")
	# Spawn cursors for that wave only; running the director on past the wave
	# would begin wave 2, whose groups are fine, and mask the result.
	for _i in 400:
		sim._update_wave_director()
		sim._tick += 1
		if sim.phase() != Sim.PHASE_SPAWNING:
			break
	assert_eq(sim.e_live_count, 0, "nothing was spawned in place of the unknown enemy")

# --- F. projectile lifetime ----------------------------------------------------

func test_a_projectile_travels_for_its_whole_lifetime() -> void:
	# Was: life was spent before the move, so a projectile with N ticks of life
	# only ever travelled N-1 times.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var spot := SimFixture.a_site(sim)
	sim._try_place(float(spot[0]), float(spot[1]), 0)
	sim._fire(0, 0)
	sim.p_life[0] = 1
	var start_x := sim.p_x[0]
	var start_y := sim.p_y[0]
	sim._advance_projectiles()
	var moved := absf(sim.p_x[0] - start_x) + absf(sim.p_y[0] - start_y)
	assert_gt(moved, 0.0, "its last tick of life is a tick of travel")
	assert_eq(sim.p_live_count, 0, "and then it expires")

# --- G. the state hash covers pending input ------------------------------------

func test_the_state_hash_notices_pending_commands() -> void:
	# Two runs with identical boards but different queued input are not in the
	# same state, and a hash that says they are would let a desync through.
	var a := SimFixture.fresh(5)
	var b := SimFixture.fresh(5)
	var spot := SimFixture.a_site(b)
	b.queue_place(9000, spot[0], spot[1], 0)
	assert_ne(b.state_hash(), a.state_hash(), "queued input is part of the state")
