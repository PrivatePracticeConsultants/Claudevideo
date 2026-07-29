extends TestCase

## What a weapon is good against.
##
## Four turret families existed for a long time before this and were only ever
## "better" or "worse" - the deployment limit went on whichever had the best
## numbers, and composition was not a decision anybody made. A damage type and an
## armour class turn "what should I build" into a question with a different answer
## on different waves, which is the whole point.
##
## The other axis is flat armour, which comes off each HIT rather than scaling the
## shot. That is what separates Ballistic from Railgun without needing a fourth
## damage type: both are kinetic, and armour punishes the one that lands many
## small rounds.

func _sim() -> Sim:
	return SimFixture.fresh()

# --- the data has to be complete ---------------------------------------------

func test_every_gun_states_what_it_fires() -> void:
	var sim := _sim()
	assert_gt(float(sim.damage_type_ids().size()), 1.0, "there is more than one type")
	for b in sim.blueprint_count():
		var t := sim.blueprint_damage_type(b)
		assert_gte(float(t), 0.0, "%s has a damage type" % sim.blueprint_display_name(b))
		assert_lt(float(t), float(sim.damage_type_ids().size()), "and it is a real one")

func test_every_drone_states_what_it_wears() -> void:
	var sim := _sim()
	assert_gt(float(sim.armour_class_ids().size()), 1.0, "there is more than one class")
	for e in sim.enemy_ids().size():
		var c := sim.enemy_armour_class(e)
		assert_gte(float(c), 0.0, "%s has an armour class" % sim.enemy_display_name(e))
		assert_lt(float(c), float(sim.armour_class_ids().size()), "and it is a real one")

func test_the_matrix_is_stated_for_every_pair() -> void:
	# A missing pair would read as neutral, and "neutral" is a design decision
	# nobody would make by leaving something out.
	var sim := _sim()
	for t in sim.damage_type_ids().size():
		for c in sim.armour_class_ids().size():
			assert_gt(sim.matchup(t, c), 0.0,
				"%s into %s must be worth something" % [sim.damage_type_name(t),
					sim.armour_class_name(c)])

func test_no_damage_type_is_neutral_everywhere() -> void:
	# A type with no favoured class and no unfavoured one is a type that does not
	# exist as far as play is concerned - it is dead content wearing a name.
	var sim := _sim()
	for t in sim.damage_type_ids().size():
		var best := 0.0
		var worst := 999.0
		for c in sim.armour_class_ids().size():
			best = maxf(best, sim.matchup(t, c))
			worst = minf(worst, sim.matchup(t, c))
		assert_gt(best, 1.0, "%s is favoured against something" % sim.damage_type_name(t))
		assert_lt(worst, 1.0, "and unfavoured against something else")

func test_no_armour_class_is_a_free_ride() -> void:
	# Symmetrically: a class every gun is neutral into gives the player nothing to
	# answer, and a class every gun is bad into cannot be answered at all.
	var sim := _sim()
	for c in sim.armour_class_ids().size():
		var best := 0.0
		for t in sim.damage_type_ids().size():
			best = maxf(best, sim.matchup(t, c))
		assert_gt(best, 1.0, "something answers %s" % sim.armour_class_name(c))

func test_every_family_has_something_it_is_the_answer_to() -> void:
	# The rule that stops a family becoming a strictly-worse copy of another one.
	var sim := _sim()
	for b in sim.blueprint_count():
		var best := 0.0
		for c in sim.armour_class_ids().size():
			best = maxf(best, sim.matchup(sim.blueprint_damage_type(b), c))
		assert_gt(best, 1.0, "%s is the right answer to something"
			% sim.blueprint_display_name(b))

func test_every_armour_class_actually_walks_the_campaign() -> void:
	# A class nothing in 48 acts wears is a rule the player never meets.
	var sim := _sim()
	var seen := {}
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		for wave: Dictionary in (db.engagement["waves"] as Array):
			for group: Dictionary in (wave["groups"] as Array):
				seen[str((db.enemies[str(group["enemy"])] as Dictionary)["armour_class"])] = true
	for c in sim.armour_class_ids():
		assert_true(seen.has(c), "%s is sent at somebody somewhere in the campaign" % c)

# --- and it has to actually change the hit -----------------------------------

func test_a_favourable_matchup_lands_more_than_an_unfavourable_one() -> void:
	# The measurement the whole system rests on. Same shot, same drone health,
	# different gun.
	var sim := _sim()
	var kinetic := sim.blueprint_index("ballistic")
	var explosive := sim.blueprint_index("cannon")
	var plated := sim.enemy_index("heavy")
	assert_gt(sim.matchup(sim.blueprint_damage_type(explosive), sim.enemy_armour_class(plated)),
		sim.matchup(sim.blueprint_damage_type(kinetic), sim.enemy_armour_class(plated)),
		"fixture sanity: explosive is the better answer to plate")

	sim._begin_wave(0)
	sim._spawn(plated)
	var start: int = sim.e_hp[0]
	sim._damage_enemy(0, 100, kinetic)
	var by_kinetic: int = start - sim.e_hp[0]
	sim.e_hp[0] = start
	sim._damage_enemy(0, 100, explosive)
	var by_explosive: int = start - sim.e_hp[0]
	assert_gt(float(by_explosive), float(by_kinetic),
		"the same hundred-point round does more when it is the right kind")

func test_the_matchup_scales_the_shot_before_armour_bites() -> void:
	# Order matters and the other order was rejected: armour subtracted from an
	# already-halved round punishes a bad matchup twice, and the two systems ask
	# different questions - the matrix asks what you brought, armour asks how big
	# it is. This pins the order rather than leaving it to a comment.
	var sim := _sim()
	var gun := sim.blueprint_index("ballistic")
	var brood := sim.enemy_index("brood")
	assert_gt(float(sim.enemy_armour(brood)), 0.0, "fixture sanity: a Brood wears armour")
	sim._begin_wave(0)
	sim._spawn(brood)
	var start: int = sim.e_hp[0]
	var scaled := int(round(100.0
		* sim.matchup(sim.blueprint_damage_type(gun), sim.enemy_armour_class(brood))))
	var expected: int = maxi(scaled - sim.enemy_armour_now(brood),
		maxi(1, int(ceil(float(scaled) * (1.0 - sim.armour_max_bite())))))
	sim._damage_enemy(0, 100, gun)
	assert_eq(start - sim.e_hp[0], expected, "matrix first, then armour off the result")

func test_a_bad_matchup_is_a_bad_answer_not_no_answer() -> void:
	# The floor. A round that rounded to zero would make a family unusable rather
	# than unwise, and there is no wave where "this gun does literally nothing"
	# is a fun thing to discover.
	var sim := _sim()
	var worst_type := 0
	var worst_class := 0
	var worst := 999.0
	for t in sim.damage_type_ids().size():
		for c in sim.armour_class_ids().size():
			if sim.matchup(t, c) < worst:
				worst = sim.matchup(t, c)
				worst_type = t
				worst_class = c
	var gun := -1
	for b in sim.blueprint_count():
		if sim.blueprint_damage_type(b) == worst_type:
			gun = b
			break
	var drone := -1
	for e in sim.enemy_ids().size():
		if sim.enemy_armour_class(e) == worst_class:
			drone = e
			break
	assert_gte(float(gun), 0.0, "fixture sanity: something fires the worst type")
	assert_gte(float(drone), 0.0, "fixture sanity: something wears the worst class")
	sim._begin_wave(0)
	sim._spawn(drone)
	var start: int = sim.e_hp[0]
	sim._damage_enemy(0, 1, gun)
	assert_gt(float(start - sim.e_hp[0]), 0.0, "the worst matchup in the game still hurts")

func test_the_support_drones_are_answerable_by_a_gun() -> void:
	# A Mender and a Jammer are the two drones a player is told to kill FIRST, and
	# the targeting orders exist to let them. Putting both behind a class that
	# something is favoured into is what makes "bring the right gun and pick them
	# out" a plan rather than a hope.
	var sim := _sim()
	for id: String in ["mender", "jammer"]:
		var armour_class := sim.enemy_armour_class(sim.enemy_index(id))
		var best := 0.0
		for t in sim.damage_type_ids().size():
			best = maxf(best, sim.matchup(t, armour_class))
		assert_gt(best, 1.0, "%s can be answered by bringing the right gun" % id)

func test_splash_and_pierce_are_typed_too() -> void:
	# The three ways a shot can land - direct, splash, pierce - all go through the
	# same credit path, so all three must be scaled. A splash that ignored the
	# matrix would make the Mortar the universal answer by accident.
	var sim := _sim()
	var cannon := sim.blueprint_index("cannon")
	var light := sim.enemy_index("swarm")
	sim._begin_wave(0)
	sim._spawn(light)
	sim._spawn(light)
	# A blast asks the spatial hash who is nearby, and the hash is rebuilt inside
	# the tick. Detonating before one has run finds an empty board.
	sim.step()
	# Every alive slot, not just the two that were spawned by hand: the tick that
	# built the hash also let the wave director spawn, and a blast wide enough to
	# be worth measuring catches those too.
	var before := _total_health(sim)
	sim._detonate(sim.e_x[0], sim.e_y[0], 400.0, 100, 1.0, cannon)
	var landed: int = before - _total_health(sim)
	assert_gt(float(landed), 0.0, "the splash landed")
	assert_eq(sim.family_damage(cannon), landed,
		"and every point of it was credited through the typed path")

func _total_health(sim: Sim) -> int:
	var total := 0
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			total += sim.e_hp[i]
	return total
