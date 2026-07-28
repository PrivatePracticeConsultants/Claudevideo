extends TestCase

## Armour and the Brood Carrier.
##
## The five classes before this were an HP-and-payout ladder: every one of them
## was "the last one but more so". Armour and splitting are the first two enemy
## properties that ask the player to do something different rather than the same
## thing harder - armour makes cheap fast guns the wrong tool, and what a carrier
## leaves behind makes them the right one again ten metres later.

func _brood(sim: Sim) -> int:
	return sim.enemy_index("brood")

func test_the_carrier_exists_and_is_the_only_thing_that_splits() -> void:
	var sim := SimFixture.fresh()
	var brood := _brood(sim)
	assert_gte(float(brood), 0.0, "the Brood Carrier is defined")
	assert_eq(sim.enemy_splits_into(brood), sim.enemy_index("swarm"),
		"and it leaves Skitters")
	assert_gt(float(sim.enemy_split_count(brood)), 0.0, "more than none of them")
	for t in sim.enemy_type_count():
		if t == brood:
			continue
		assert_eq(sim.enemy_splits_into(t), -1,
			"%s simply dies" % sim.enemy_id(t))

func test_killing_a_carrier_hatches_its_brood() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	var brood := _brood(sim)
	sim._spawn_at(brood, 400.0)
	assert_eq(sim.e_live_count, 1, "fixture sanity: one carrier on the road")
	sim._damage_enemy(0, 1 << 30, -1)
	assert_eq(sim.e_live_count, 0, "the carrier is dead")
	# Deferred to the end of the tick on purpose - see _resolve_splits.
	sim._resolve_splits()
	assert_eq(sim.e_live_count, sim.enemy_split_count(brood),
		"and exactly the declared number of Skitters took its place")
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			assert_eq(sim.e_type[i], sim.enemy_index("swarm"), "of the declared class")

func test_the_brood_starts_where_the_carrier_fell() -> void:
	# Not at the entrance. A carrier broken open at the far end of the road is a
	# problem at the far end of the road, and that is the whole decision it poses.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	var where := sim.path_length() * 0.6
	sim._spawn_at(_brood(sim), where)
	sim._damage_enemy(0, 1 << 30, -1)
	sim._resolve_splits()
	var found := 0
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 0:
			continue
		found += 1
		assert_almost_eq(sim.e_prog[i], where, 0.0001, "hatched where it died")
	assert_gt(float(found), 0.0, "fixture sanity: something hatched")

func test_a_carrier_that_leaks_leaves_nothing() -> void:
	# Splitting is on death, not on exit. It is what makes letting one through a
	# real option rather than always the worse one.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn_at(_brood(sim), sim.path_length() - 1.0)
	sim._advance_enemies()
	sim._resolve_splits()
	assert_eq(sim.leaks(), 1, "it got through")
	assert_eq(sim.e_live_count, 0, "and left nothing behind")

func test_the_blast_that_kills_a_carrier_does_not_also_hit_its_brood() -> void:
	# The reason splits are deferred. Area damage walks the spatial hash, which
	# still lists slots whose occupants died earlier in the same tick; hatching
	# immediately can hand a child one of those slots and the same explosion finds
	# it alive. Deterministic, but decided by the free list, which is no way to
	# decide anything.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn_at(_brood(sim), 400.0)
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())
	sim._detonate(sim.e_x[0], sim.e_y[0], 400.0, 1 << 20, 1.0, -1)
	assert_eq(sim.e_live_count, 0, "the carrier went up")
	sim._resolve_splits()
	assert_eq(sim.e_live_count, sim.enemy_split_count(_brood(sim)),
		"and every Skitter came out of it alive")
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			assert_eq(sim.e_hp[i], sim.e_hp_max[i], "at full health")

func test_armour_comes_off_the_hit_not_off_the_health() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	var brood := _brood(sim)
	assert_gt(float(sim.enemy_armour(brood)), 0.0, "the carrier has armour")
	sim._spawn_at(brood, 100.0)
	sim._spawn_at(sim.enemy_index("walker"), 100.0)
	var armoured_before := sim.e_hp[0]
	var bare_before := sim.e_hp[1]
	sim._damage_enemy(0, 40, -1)
	sim._damage_enemy(1, 40, -1)
	assert_lt(float(armoured_before - sim.e_hp[0]), float(bare_before - sim.e_hp[1]),
		"the same round took less off the armoured one")

func test_armour_never_takes_a_whole_round() -> void:
	# A weapon that literally cannot scratch something reads as broken, not as a
	# counter. The cap is what keeps the cheap tiers working at every scale.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	var brood := _brood(sim)
	sim._spawn_at(brood, 100.0)
	var before := sim.e_hp[0]
	sim._damage_enemy(0, 1, -1)
	assert_eq(before - sim.e_hp[0], 1, "one damage still does one")
	var bite := float(SimFixture.database().scaling["armour_max_bite"])
	var hp := sim.e_hp[0]
	sim._damage_enemy(0, 100, -1)
	assert_gte(float(hp - sim.e_hp[0]), floor(100.0 * (1.0 - bite)),
		"and armour never absorbs more than its share of a round")

func test_armour_keeps_up_with_the_wave() -> void:
	# Flat armour in a game where health grows exponentially is a texture that
	# exists for four levels and then evaporates.
	var sim := SimFixture.fresh()
	var brood := _brood(sim)
	sim._begin_wave(0)
	sim._spawn_at(brood, 100.0)
	var early := sim.e_hp[0]
	sim._damage_enemy(0, 60, -1)
	var early_taken := early - sim.e_hp[0]
	sim._begin_wave(8)
	sim._spawn_at(brood, 100.0)
	var late := sim.e_hp[1]
	sim._damage_enemy(1, 60, -1)
	var late_taken := late - sim.e_hp[1]
	assert_lt(float(late_taken), float(early_taken),
		"the same round is worth less against a later wave's armour")

func test_splitting_is_part_of_the_state_hash() -> void:
	# Two boards that agree on every drone alive and disagree about what is one
	# tick from existing are not in the same place.
	var a := SimFixture.fresh()
	var b := SimFixture.fresh()
	a._begin_wave(0)
	b._begin_wave(0)
	a._spawn_at(_brood(a), 300.0)
	b._spawn_at(_brood(b), 300.0)
	assert_eq(a.state_hash(), b.state_hash(), "fixture sanity: identical so far")
	a._damage_enemy(0, 1 << 30, -1)
	b._damage_enemy(0, 1 << 30, -1)
	b._resolve_splits()
	assert_ne(a.state_hash(), b.state_hash(),
		"one owes three Skitters and the other has already paid")

func test_a_run_against_carriers_replays_identically() -> void:
	# Splitting spawns drones mid-run, and every spawn draws from the seeded RNG.
	# If the order of those draws ever depended on anything but the tick, this is
	# where it would show.
	var level := _first_level_with_carriers()
	assert_ne(str(level.get("engagement", "")), "", "fixture sanity: carriers are fielded somewhere")
	var live := SimFixture.for_level(str(level["map"]), str(level["engagement"]))
	var log := SimFixture.run_greedy(live, true, 4, 1)
	var again := SimFixture.for_level(str(level["map"]), str(level["engagement"]))
	SimFixture.replay(again, log)
	var third := SimFixture.for_level(str(level["map"]), str(level["engagement"]))
	SimFixture.replay(third, log)
	assert_eq(again.state_hash(), third.state_hash(), "same log, same end state")

func test_the_campaign_actually_fields_them() -> void:
	# A class nothing spawns is a class that does not exist.
	var level := _first_level_with_carriers()
	assert_ne(str(level.get("engagement", "")), "",
		"some engagement in the campaign spawns Brood Carriers")

func _first_level_with_carriers() -> Dictionary:
	var levels := Database.load_levels()
	for level: Dictionary in levels:
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		if not db.is_valid():
			continue
		for wave: Dictionary in (db.engagement["waves"] as Array):
			for group: Dictionary in (wave["groups"] as Array):
				if str(group["enemy"]) == "brood":
					return level
	return {}
