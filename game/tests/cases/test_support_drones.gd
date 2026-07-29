extends TestCase

## The Field Mender and the Static Jammer.
##
## Every drone before these two was answered by pointing more guns at the road.
## The Mender has to be picked OUT of a crowd — it is small, quick and cheap, so
## First and Toughest walk straight past it — which is the first thing in the game
## that makes the targeting orders worth having. The Jammer is the only drone that
## attacks the board rather than walking past it.

func _mender(sim: Sim) -> int:
	return sim.enemy_index("mender")

func _jammer(sim: Sim) -> int:
	return sim.enemy_index("jammer")

func test_both_classes_exist_and_are_the_only_ones_with_these_jobs() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	assert_gt(float(sim.enemy_repair(_mender(sim))), 0.0, "the Mender repairs")
	assert_gt(float(sim.enemy_jam_ticks(_jammer(sim))), 0.0, "the Jammer jams")
	for t in sim.enemy_type_count():
		if t != _mender(sim):
			assert_eq(sim.enemy_repair(t), 0, "%s does not repair" % sim.enemy_id(t))
		if t != _jammer(sim):
			assert_eq(sim.enemy_jam_ticks(t), 0, "%s does not jam" % sim.enemy_id(t))

## Put a drone on the road with the hash rebuilt around it.
func _place(sim: Sim, type_id: String, prog: float) -> int:
	sim._spawn_at(sim.enemy_index(type_id), prog)
	var slot := sim.e_live_count - 1
	sim.e_offset[slot] = 0.0
	sim.sample_for_render(prog, 0.0)
	sim.e_x[slot] = sim.out_x()
	sim.e_y[slot] = sim.out_y()
	return slot

func _rebuild(sim: Sim) -> void:
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())

func test_a_mender_puts_health_back_into_the_drone_beside_it() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	var mender := _place(sim, "mender", 400.0)
	var hurt := _place(sim, "walker", 420.0)
	sim.e_hp[hurt] = 1
	_rebuild(sim)
	sim._repair_around(mender, sim.e_type[mender])
	assert_gt(float(sim.e_hp[hurt]), 1.0, "the walker was mended")

func test_a_mender_never_mends_itself() -> void:
	# A drone that outheals your line while also being the toughest thing in it is
	# not a puzzle, it is a wall.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	var mender := _place(sim, "mender", 400.0)
	sim.e_hp[mender] = 1
	_rebuild(sim)
	sim._repair_around(mender, sim.e_type[mender])
	assert_eq(sim.e_hp[mender], 1, "it is still on one health")

func test_repair_never_exceeds_full_health() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	var mender := _place(sim, "mender", 400.0)
	var other := _place(sim, "walker", 420.0)
	_rebuild(sim)
	for _pulse in 20:
		sim._repair_around(mender, sim.e_type[mender])
	assert_eq(sim.e_hp[other], sim.e_hp_max[other], "topped up, never overfilled")

func test_repair_does_not_reach_across_the_board() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	var mender := _place(sim, "mender", 200.0)
	var far := _place(sim, "walker", sim.path_length() - 200.0)
	sim.e_hp[far] = 1
	_rebuild(sim)
	sim._repair_around(mender, sim.e_type[mender])
	assert_eq(sim.e_hp[far], 1, "out of reach is out of reach")

func test_repair_keeps_up_with_the_wave() -> void:
	# Healing that does not scale stops mattering the moment the drones around it
	# are worth more than it can put back.
	var sim := SimFixture.fresh()
	var mender := _mender(sim)
	sim._begin_wave(0)
	var early := sim.enemy_repair(mender)
	sim._begin_wave(9)
	assert_gt(float(sim.enemy_repair(mender)), float(early),
		"a later wave's mender puts back more")

func test_a_jammer_silences_the_turrets_it_passes() -> void:
	var sim := SimFixture.fresh()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	sim._begin_wave(0)
	# Drop the jammer right beside the emplacement.
	sim._spawn_at(_jammer(sim), 0.0)
	sim.e_x[0] = sim.t_x[0]
	sim.e_y[0] = sim.t_y[0]
	assert_false(sim.platform_jammed(0), "quiet to begin with")
	sim._jam_around(0, sim.e_type[0])
	assert_true(sim.platform_jammed(0), "and silenced once the jammer is on it")

func test_a_jammed_turret_does_not_fire() -> void:
	var sim := SimFixture.fresh()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	sim._begin_wave(0)
	var target := _place(sim, "walker", 0.0)
	sim.e_x[target] = sim.t_x[0]
	sim.e_y[target] = sim.t_y[0]
	_rebuild(sim)
	sim.t_disabled[0] = 30
	var before := sim.p_live_count
	sim._update_platforms()
	assert_eq(sim.p_live_count, before, "not a shot while it is jammed")
	sim.t_disabled[0] = 0
	sim._update_platforms()
	assert_gt(float(sim.p_live_count), float(before), "and it fires the moment it is free")

func test_a_jammed_turret_does_not_bank_its_cooldown() -> void:
	# A second of silence has to cost a second of fire. If the cooldown kept
	# advancing, jamming a turret that was reloading anyway would cost nothing.
	var sim := SimFixture.fresh()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	sim.t_cooldown[0] = 10
	sim.t_disabled[0] = 5
	for _tick in 5:
		sim._update_platforms()
	assert_eq(sim.t_cooldown[0], 10, "the cooldown did not move")
	assert_eq(sim.t_disabled[0], 0, "and the jam ran out")

func test_jamming_refreshes_rather_than_stacks() -> void:
	var sim := SimFixture.fresh()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	sim._begin_wave(0)
	sim._spawn_at(_jammer(sim), 0.0)
	sim.e_x[0] = sim.t_x[0]
	sim.e_y[0] = sim.t_y[0]
	for _pulse in 6:
		sim._jam_around(0, sim.e_type[0])
	assert_eq(sim.t_disabled[0], sim.enemy_jam_ticks(sim.e_type[0]),
		"six pulses are worth exactly one")

func test_selling_carries_the_jam_with_the_compaction() -> void:
	# Selling moves the last turret into the freed slot. A stale jam left behind
	# would silence a turret nothing is jamming.
	var sim := SimFixture.fresh()
	var first := SimFixture.a_site(sim, 0)
	var second := SimFixture.a_site(sim, 6)
	sim.queue_place(0, first[0], first[1], 0)
	sim.step()
	sim.queue_place(sim.tick(), second[0], second[1], 0)
	sim.step()
	assert_eq(sim.t_count, 2, "fixture sanity: two turrets")
	sim.t_disabled[1] = 40
	sim.queue_sell(sim.tick(), 0)
	sim.step()
	assert_eq(sim.platform_jammed(0), true, "the survivor kept its own jam")

func test_both_are_part_of_the_state_hash() -> void:
	var a := SimFixture.fresh()
	var b := SimFixture.fresh()
	var spot := SimFixture.a_site(a)
	for sim in [a, b]:
		sim.queue_place(0, spot[0], spot[1], 0)
		sim.step()
	assert_eq(a.state_hash(), b.state_hash(), "fixture sanity: identical so far")
	a.t_disabled[0] = 12
	assert_ne(a.state_hash(), b.state_hash(), "a silenced turret is a different board")

func test_the_campaign_fields_both_of_them() -> void:
	var levels := Database.load_levels()
	var seen := {}
	for level: Dictionary in levels:
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		if not db.is_valid():
			continue
		for wave: Dictionary in (db.engagement["waves"] as Array):
			for g: Dictionary in (wave["groups"] as Array):
				seen[str(g["enemy"])] = true
	assert_true(seen.has("mender"), "some engagement spawns Field Menders")
	assert_true(seen.has("jammer"), "some engagement spawns Static Jammers")

func test_an_engagement_without_them_skips_the_scan_entirely() -> void:
	# The per-tick scan over the whole enemy pool is guarded, because most of the
	# campaign fields neither of these and should not pay for them.
	var early := SimFixture.for_level("highway_01", "highway_act1")
	assert_false(early._has_support_drones,
		"fixture sanity: the opening board has no support drones")

func test_a_run_against_both_replays_identically() -> void:
	var levels := Database.load_levels()
	var chosen := {}
	for level: Dictionary in levels:
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		if not db.is_valid():
			continue
		for wave: Dictionary in (db.engagement["waves"] as Array):
			for g: Dictionary in (wave["groups"] as Array):
				if str(g["enemy"]) == "jammer":
					chosen = level
					break
		if not chosen.is_empty():
			break
	assert_false(chosen.is_empty(), "fixture sanity: found a level with jammers")
	var live := SimFixture.for_level(str(chosen["map"]), str(chosen["engagement"]))
	var log := SimFixture.run_greedy(live, true, 4, 1)
	var again := SimFixture.for_level(str(chosen["map"]), str(chosen["engagement"]))
	SimFixture.replay(again, log)
	var third := SimFixture.for_level(str(chosen["map"]), str(chosen["engagement"]))
	SimFixture.replay(third, log)
	assert_eq(again.state_hash(), third.state_hash(), "same log, same end state")
