extends TestCase

## Champion drones and the overcharge that answers them.
##
## A champion is every Nth spawn of a wave group, arriving with several times the
## health and bounty. It puts SPIKES inside a stream: a steady stream is answered
## by a steady line, a spike is answered by a targeting order and by the one
## active ability in the game - overcharge, which pushes a single turret hard for
## a few seconds and then takes it off the table for a long cooldown.

func _sim() -> Sim:
	return SimFixture.fresh()

func _spawn_group_until_champion(sim: Sim) -> int:
	# Jump to the act's last wave - the first waves' groups are smaller than
	# champion_every on purpose, so the opening minutes stay clean - and step the
	# director until the group's Nth spawn walks out. Integrity is pinned high
	# because nothing is defending, and a leaked-out loss would stop the clock.
	sim._integrity = 1 << 20
	sim._begin_wave(sim.wave_count() - 1)
	for _t in 6000:
		sim.step()
		for i in sim.e_alive.size():
			if sim.e_alive[i] == 1 and sim.enemy_is_champion(i):
				return i
	return -1

func test_the_nth_spawn_is_a_champion() -> void:
	var sim := _sim()
	var champion := _spawn_group_until_champion(sim)
	assert_gte(float(champion), 0.0, "a big enough group produces a champion")

func test_a_champion_is_worth_the_name() -> void:
	var sim := _sim()
	var champion := _spawn_group_until_champion(sim)
	assert_gte(float(champion), 0.0, "fixture sanity")
	var type_index: int = sim.e_type[champion]
	assert_gt(float(sim.e_hp_max[champion]),
		float(sim.enemy_hp_now(type_index)) * 2.0,
		"several times the health of its class")
	assert_gt(float(sim.e_bounty[champion]),
		float(sim.enemy_bounty_now(type_index)) * 2.0,
		"and pays several times the bounty")

func test_a_champion_spawns_deterministically() -> void:
	# Same seed, same act - the Nth spawn is the Nth spawn. If this ever fails,
	# champions have picked up a nondeterministic input and replays are dead.
	var a := SimFixture.fresh(777)
	var b := SimFixture.fresh(777)
	for _t in 2000:
		a.step()
		b.step()
	assert_eq(a.state_hash(), b.state_hash(), "identical runs stay identical")
	var count_a := 0
	for i in a.e_alive.size():
		if a.e_alive[i] == 1 and a.enemy_is_champion(i):
			count_a += 1
	var count_b := 0
	for i in b.e_alive.size():
		if b.e_alive[i] == 1 and b.enemy_is_champion(i):
			count_b += 1
	assert_eq(count_a, count_b, "and so do their champions")

func test_split_children_are_never_champions() -> void:
	# A champion Brood would hatch champion Skitters if the flag leaked through
	# _spawn_at's default. It must not: the spike is the parent, not a litter.
	var sim := _sim()
	var brood := sim.enemy_index("brood")
	sim._begin_wave(0)
	sim._spawn_at(brood, 100.0, 0, true)
	var parent := -1
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1 and sim.e_type[i] == brood:
			parent = i
			break
	assert_gte(float(parent), 0.0, "fixture sanity: the carrier spawned")
	assert_true(sim.enemy_is_champion(parent), "and is a champion")
	sim._damage_enemy(parent, 1 << 24, -1)
	sim._resolve_splits()
	var children := 0
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1 and sim.e_type[i] == sim.enemy_index("swarm"):
			children += 1
			assert_false(sim.enemy_is_champion(i), "a hatchling is not a champion")
	assert_gt(float(children), 0.0, "fixture sanity: it hatched")

# --- overcharge ---------------------------------------------------------------

func _with_turret() -> Sim:
	var sim := _sim()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	return sim

func test_overcharge_costs_capital_and_takes_effect() -> void:
	var sim := _with_turret()
	var before := sim.capital()
	sim.queue_overcharge(sim.tick(), 0)
	sim.step()
	assert_eq(sim.capital(), before - sim.overcharge_cost(), "the surge was paid for")
	assert_gt(float(sim.platform_overcharge_ticks(0)), 0.0, "and is running")

func test_an_overcharged_turret_fires_faster_and_harder() -> void:
	# Measured on the sim's own quoted numbers rather than a copy of the data:
	# the effective interval shortens and the next round leaving the gun is
	# heavier. Both read from the projectile actually launched.
	var sim := _with_turret()
	sim._begin_wave(0)
	sim._spawn_at(sim.enemy_index("heavy"), 40.0)
	var plain_damage := 0
	for _t in 200:
		sim.step()
		if sim.p_live_count > 0:
			for i in sim.p_alive.size():
				if sim.p_alive[i] == 1:
					plain_damage = sim.p_damage[i]
					break
			break
	assert_gt(float(plain_damage), 0.0, "fixture sanity: it fired un-charged")
	sim.queue_overcharge(sim.tick(), 0)
	sim.step()
	var charged_damage := 0
	for _t in 200:
		sim.step()
		if sim.platform_overcharge_ticks(0) <= 0:
			break
		var found := false
		for i in sim.p_alive.size():
			if sim.p_alive[i] == 1 and sim.p_damage[i] > plain_damage:
				charged_damage = sim.p_damage[i]
				found = true
				break
		if found:
			break
	assert_gt(float(charged_damage), float(plain_damage),
		"a surging turret's rounds are heavier")

func test_the_surge_expires_into_a_cooldown() -> void:
	var sim := _with_turret()
	sim.queue_overcharge(sim.tick(), 0)
	sim.step()
	var guard := 0
	while sim.platform_overcharge_ticks(0) > 0 and guard < 100000:
		sim.step()
		guard += 1
	assert_gt(float(sim.platform_overcharge_cooldown(0)), 0.0,
		"spent, the turret starts its cooldown")

func test_a_cooling_turret_refuses_a_second_surge() -> void:
	var sim := _with_turret()
	sim._capital = 1 << 20
	sim.queue_overcharge(sim.tick(), 0)
	sim.step()
	var held := sim.capital()
	sim.queue_overcharge(sim.tick(), 0)
	sim.step()
	assert_eq(sim.capital(), held, "a surge cannot be stacked on a surge")

func test_an_unaffordable_surge_is_refused_cleanly() -> void:
	var sim := _with_turret()
	sim._capital = sim.overcharge_cost() - 1
	sim.queue_overcharge(sim.tick(), 0)
	sim.step()
	assert_eq(sim.capital(), sim.overcharge_cost() - 1, "nothing was taken")
	assert_eq(sim.platform_overcharge_ticks(0), 0, "and nothing started")

func test_a_jammed_turret_cannot_be_overcharged() -> void:
	# The counter to a Jammer is killing the Jammer, not paying to ignore it.
	var sim := _with_turret()
	sim.t_disabled[0] = 100
	var before := sim.capital()
	sim.queue_overcharge(sim.tick(), 0)
	sim.step()
	assert_eq(sim.capital(), before, "refused while jammed")

func test_overcharge_is_part_of_the_state_hash_and_replays() -> void:
	var recorded := SimFixture.fresh(4242)
	var spot := SimFixture.a_site(recorded)
	recorded.queue_place(0, spot[0], spot[1], 0)
	recorded.step()
	recorded.queue_overcharge(recorded.tick(), 0)
	recorded.step()
	var plain := SimFixture.fresh(4242)
	plain.queue_place(0, spot[0], spot[1], 0)
	plain.step()
	plain.step()
	assert_ne(recorded.state_hash(), plain.state_hash(),
		"a surging board is a different state")

	# The recorded sim is already 2 ticks in from its setup steps; the replay
	# queues everything up front and must run the same TOTAL tick count, or the
	# comparison is between two different moments and reads as a desync.
	var replayed := SimFixture.fresh(4242)
	replayed.queue_place(0, spot[0], spot[1], 0)
	replayed.queue_overcharge(1, 0)
	for _t in 400:
		recorded.step()
	for _t in 402:
		replayed.step()
	assert_eq(replayed.state_hash(), recorded.state_hash(),
		"the same commands at the same ticks land in the same place")
