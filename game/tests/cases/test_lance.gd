extends TestCase

## The Vanguard Lance: the top of the drone ladder.
##
## Every other class trades speed against health - Skitters are fast and
## fragile, Bulwarks are slow and tough. The Lance is the fastest thing on the
## board AND the toughest, and that is the whole design: a turret gets less time
## on a target that needs more damage, so a thin line of fire that held
## everything else lets Lances through.
##
## Most of this file guards the pacing rather than the numbers, because the
## numbers are tuned by measurement and will move again. Where it does assert a
## number, it asserts a *relationship* between numbers in the data file.

func _lance(sim: Sim) -> int:
	return sim.enemy_index("lance")

func test_the_lance_exists_and_breaks_the_speed_health_trade() -> void:
	var sim := SimFixture.fresh()
	var lance := _lance(sim)
	assert_gte(float(lance), 0.0, "the Lance is defined")
	for id in sim.enemy_ids():
		if str(id) == "lance":
			continue
		var other := sim.enemy_index(str(id))
		assert_gt(sim.enemy_speed(lance), sim.enemy_speed(other),
			"the Lance outruns the %s" % id)
		assert_gt(float(sim.enemy_base_hp(lance)), float(sim.enemy_base_hp(other)),
			"and outlasts the %s" % id)

func test_a_leak_is_priced_below_a_bulwark_on_purpose() -> void:
	# This looks backwards and is not. A Bulwark's leak value is priced for
	# something you can reliably stop; the Lance is priced for something you often
	# cannot, so its expected cost per drone spawned is already the highest in the
	# game. Measured at a higher price than the Bulwark, it was the only thing
	# that ever decided a level - the final act killed 5,578 of 5,590 drones and
	# still lost, on eight Lance leaks.
	var db := SimFixture.database()
	var lance := int((db.enemies["lance"] as Dictionary)["leak_value"])
	var heavy := int((db.enemies["heavy"] as Dictionary)["leak_value"])
	assert_lt(float(lance), float(heavy), "a Lance leak costs less than a Bulwark leak")
	assert_gt(float(lance), float((db.enemies["walker"] as Dictionary)["leak_value"]),
		"but far more than a Walker leak")

func test_it_pays_out_more_than_anything_else() -> void:
	var sim := SimFixture.fresh()
	var lance := _lance(sim)
	for id in sim.enemy_ids():
		if str(id) == "lance":
			continue
		assert_gt(float(sim.enemy_base_bounty(lance)),
			float(sim.enemy_base_bounty(sim.enemy_index(str(id)))),
			"a Lance pays better than a %s" % id)

func test_the_opening_campaign_never_sees_one() -> void:
	# A class this punishing arriving before the player has a board is not
	# difficulty, it is a wall. It is held back until the campaign is well under
	# way, and the levels before that must be clean of it.
	var levels := Database.load_levels()
	var first_with := -1
	for index in levels.size():
		var db := Database.load_engagement(str(levels[index]["map"]),
			str(levels[index]["engagement"]))
		if _lance_count(db) > 0:
			first_with = index
			break
	assert_gt(float(first_with), 6.0,
		"the Lance should not appear until the campaign is well under way")

func test_it_never_opens_a_wave() -> void:
	# Lances arrive behind the drones already on the board, never as the thing
	# that greets you. Same reason Bulwarks do not open one.
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		var waves: Array = db.engagement["waves"]
		for index in waves.size():
			for group in ((waves[index] as Dictionary)["groups"] as Array):
				var g: Dictionary = group
				if str(g["enemy"]) != "lance":
					continue
				assert_gt(float(int(g["start_delay_ticks"])), 0.0,
					"%s wave %d: a Lance group must be staged behind something"
						% [str(level["engagement"]), index + 1])
				assert_gte(float(index), 2.0,
					"%s: Lances must not arrive in the opening waves"
						% str(level["engagement"]))

func test_it_stays_a_small_share_of_head_count() -> void:
	# At 4.5% of head-count the Lance was contributing a third of the closing
	# wave's health and every level from the fourteenth on lost to exactly six
	# leaks. It is an elite, not a population.
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		var total := 0
		for wave in (db.engagement["waves"] as Array):
			for group in ((wave as Dictionary)["groups"] as Array):
				total += int((group as Dictionary)["count"])
		var lances := _lance_count(db)
		assert_lte(float(lances), float(total) * 0.03,
			"%s: Lances are %d of %d drones, which is past elite and into population"
				% [str(level["engagement"]), lances, total])

func test_a_run_that_meets_lances_still_replays_identically() -> void:
	# A new class means new spawn-time RNG draws and new pool traffic. Both are
	# exactly the sort of thing that desyncs a replay.
	# The *first* level that fields Lances, not the last. Both prove the same
	# thing, and the last is a 118-turret engagement that pushed the whole suite
	# past its wall-clock budget when played twice.
	var map_id := ""
	var engagement_id := ""
	for level in Database.load_levels():
		var candidate := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		if _lance_count(candidate) > 0:
			map_id = str(level["map"])
			engagement_id = str(level["engagement"])
			break
	assert_false(engagement_id.is_empty(), "fixture sanity: some act fields Lances")
	var a := SimFixture.for_level(map_id, engagement_id, 77)
	var b := SimFixture.for_level(map_id, engagement_id, 77)
	var log := SimFixture.run_greedy(a)
	SimFixture.replay(b, log)
	assert_eq(b.state_hash(), a.state_hash(), "a run containing Lances replays bit-exactly")

func _lance_count(db: Database) -> int:
	var total := 0
	for wave in (db.engagement["waves"] as Array):
		for group in ((wave as Dictionary)["groups"] as Array):
			if str((group as Dictionary)["enemy"]) == "lance":
				total += int((group as Dictionary)["count"])
	return total
