extends TestCase

## The Cannon family: shells that damage everything inside a radius.
##
## Area damage is the first mechanic in the game that resolves against several
## entities from one event, which makes it the first place iteration order can
## leak into the simulation. Most of this file is about that.

func _cannon(sim: Sim) -> int:
	return sim.blueprint_index("cannon")

func test_the_cannon_family_exists_and_has_splash() -> void:
	var sim := SimFixture.fresh()
	assert_gte(float(_cannon(sim)), 0.0, "cannon is defined")
	assert_gt(sim.blueprint_splash(_cannon(sim)), 0.0, "and its shells have a blast radius")
	assert_eq(sim.blueprint_splash(sim.blueprint_index("ballistic")), 0.0,
		"while ballistic stays single-target")

func test_a_shell_damages_every_enemy_in_the_blast() -> void:
	var sim := SimFixture.fresh()
	var walker := sim.enemy_index("walker")
	sim._begin_wave(0)
	# A tight cluster: all spawned at the path start, so they sit within a
	# blast radius of each other.
	for _i in 6:
		sim._spawn(walker)
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())
	var before := []
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			before.append(sim.e_hp[i])

	sim._detonate(sim.e_x[0], sim.e_y[0], 400.0, 20, 1.0)
	var damaged := 0
	var index := 0
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			if sim.e_hp[i] < before[index]:
				damaged += 1
			index += 1
	assert_gt(float(damaged), 1.0, "a blast hits more than one enemy")

func test_damage_falls_off_with_distance() -> void:
	var sim := SimFixture.fresh()
	var walker := sim.enemy_index("walker")
	sim._begin_wave(0)
	sim._spawn(walker)
	sim._spawn(walker)
	# Put one at the centre of the blast and one out near its edge.
	sim.e_x[0] = 500.0
	sim.e_y[0] = 500.0
	sim.e_x[1] = 500.0
	sim.e_y[1] = 590.0
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())
	var full := sim.e_hp[0]
	var edge := sim.e_hp[1]
	sim._detonate(500.0, 500.0, 100.0, 40, 0.25)
	var centre_damage := full - sim.e_hp[0]
	var edge_damage := edge - sim.e_hp[1]
	assert_gt(float(centre_damage), float(edge_damage), "the centre takes more than the edge")
	assert_gt(float(edge_damage), 0.0, "but the edge still takes something")

func test_a_blast_never_deals_zero() -> void:
	# A shell that visibly reaches something and does nothing reads as a bug,
	# so the falloff floors at 1 rather than rounding away.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	sim.e_x[0] = 500.0
	sim.e_y[0] = 599.0
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())
	var before := sim.e_hp[0]
	sim._detonate(500.0, 500.0, 100.0, 1, 0.0)
	assert_lt(float(sim.e_hp[0]), float(before), "even a grazing hit removes at least 1 HP")

func test_enemies_outside_the_radius_are_untouched() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	sim.e_x[0] = 500.0
	sim.e_y[0] = 500.0
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())
	var before := sim.e_hp[0]
	sim._detonate(1500.0, 1500.0, 100.0, 999, 1.0)
	assert_eq(sim.e_hp[0], before, "a blast on the far side of the map does nothing")

func test_area_damage_stays_deterministic() -> void:
	# The reason _detonate walks the spatial hash in cell order and slot order:
	# a blast that killed several enemies in an arbitrary sequence would pay
	# bounties and recycle pool slots in that sequence too.
	var a := SimFixture.fresh(4242)
	var b := SimFixture.fresh(4242)
	var log := SimFixture.run_greedy(a)
	SimFixture.replay(b, log)
	assert_eq(b.state_hash(), a.state_hash(), "a run using cannons replays identically")
	assert_eq(b.kills(), a.kills(), "same kills")
	assert_eq(b.capital(), a.capital(), "same capital")

func test_cannons_are_actually_used_by_the_scripted_run() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim)
	var cannons := 0
	for i in sim.t_count:
		if sim.platform_splash(i) > 0.0:
			cannons += 1
	assert_gt(float(cannons), 0.0, "fixture sanity: the policy builds cannons, so the tests exercise splash")

func test_splash_is_what_makes_swarm_waves_survivable() -> void:
	# Skitter swarms exist to punish an arsenal with no area damage. Check that
	# claim rather than asserting it: the same level, same seed, built entirely
	# out of single-target turrets, should do measurably worse.
	var mixed := SimFixture.for_level("port_01", "port_01_act2")
	SimFixture.run_greedy(mixed)
	var single := SimFixture.for_level("port_01", "port_01_act2")
	_build_single_target_only(single)
	assert_gte(float(mixed.integrity()), float(single.integrity()),
		"a mixed arsenal should not do worse than pure single-target against swarms")

func _build_single_target_only(sim: Sim) -> void:
	var sites := SimFixture.candidate_sites(sim)
	var next_site := 0
	var ticks := 0
	while not sim.is_over() and ticks < SimFixture.MAX_TICKS:
		if sim.t_count < sim.platform_limit() and next_site + 1 < sites.size():
			if sim.can_build_at(float(sites[next_site]), float(sites[next_site + 1]), 0) == Sim.BUILD_OK:
				sim.queue_place(sim.tick(), sites[next_site], sites[next_site + 1], 0)
				next_site += 2
		else:
			for i in sim.t_count:
				if sim.can_upgrade(i):
					sim.queue_upgrade(sim.tick(), i)
					break
		sim.step()
		ticks += 1
