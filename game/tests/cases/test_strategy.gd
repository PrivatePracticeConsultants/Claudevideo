extends TestCase

## The strategy layer: Salvage Rigs, tier-4 doctrines, premium ground, and
## veterancy. Four systems with one shared purpose - making "carpet the road and
## upgrade everything" stop being the answer. A rig asks WHETHER to build a gun,
## a doctrine asks WHICH gun it becomes, premium ground asks WHERE it stands,
## and veterancy asks which guns are worth protecting.

func _sim() -> Sim:
	return SimFixture.fresh()

func _rig_index(sim: Sim) -> int:
	return sim.blueprint_index("rig")

# --- salvage rig --------------------------------------------------------------

func test_the_rig_exists_and_is_buildable() -> void:
	var sim := _sim()
	assert_gte(float(_rig_index(sim)), 0.0, "the rig is in the arsenal")
	assert_gt(float(sim.blueprint_income(_rig_index(sim))), 0.0, "and it earns")
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], _rig_index(sim))
	sim.step()
	assert_eq(sim.t_count, 1, "it stands where a gun could have")

func test_a_rig_pays_at_the_top_of_each_wave_but_not_the_first() -> void:
	var sim := _sim()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], _rig_index(sim))
	sim.step()
	var held := sim.capital()
	sim._begin_wave(0)
	assert_eq(sim.capital(), held, "the opening wave pays nothing - a rig must be risked before it earns")
	# Interest is also paid at the top of a wave and would blur the reading, so
	# the purse is emptied first: whatever arrives now is the rig's alone.
	sim._capital = 0
	sim._begin_wave(1)
	assert_eq(sim.capital(), sim.platform_income(0), "wave two pays the income")
	assert_eq(sim.rig_income_paid(), sim.platform_income(0), "and it is accounted")

func test_a_rig_never_fires() -> void:
	var sim := _sim()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], _rig_index(sim))
	sim.step()
	sim._begin_wave(0)
	sim._spawn_at(sim.enemy_index("walker"), 10.0)
	for _t in 120:
		sim.step()
	assert_eq(sim.p_live_count, 0, "money does not shoot")

func test_a_jammed_rig_earns_nothing() -> void:
	# The Jammer's silence costs uptime; a rig's uptime IS its income, so jamming
	# one has to cost the pay-out or the Jammer is no threat to an economy board.
	var sim := _sim()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], _rig_index(sim))
	sim.step()
	sim.t_disabled[0] = 1000
	sim._capital = 0
	sim._begin_wave(1)
	assert_eq(sim.capital(), 0, "a silenced rig pays nothing")

# --- doctrines ----------------------------------------------------------------

func _turret_at_top_tier(sim: Sim, family: String = "ballistic") -> int:
	sim._capital = 1 << 24
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], sim.blueprint_index(family))
	sim.step()
	for _u in sim.blueprint_tier_count(sim.blueprint_index(family)) - 1:
		sim.queue_upgrade(sim.tick(), 0)
		sim.step()
	return 0

func test_a_doctrine_needs_the_top_tier() -> void:
	var sim := _sim()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	sim.queue_doctrine(sim.tick(), 0, 0)
	sim.step()
	assert_eq(sim.platform_doctrine(0), -1, "a tier-1 turret has no doctrine to choose")

func test_a_doctrine_is_chosen_once_and_kept() -> void:
	var sim := _sim()
	var i := _turret_at_top_tier(sim)
	sim.queue_doctrine(sim.tick(), i, 0)
	sim.step()
	assert_eq(sim.platform_doctrine(i), 0, "chosen")
	sim.queue_doctrine(sim.tick(), i, 1)
	sim.step()
	assert_eq(sim.platform_doctrine(i), 0, "an either/or you can undo is a menu - refused")

func test_every_gun_family_offers_exactly_two() -> void:
	var sim := _sim()
	for b in sim.blueprint_count():
		if sim.blueprint_income(b) > 0:
			assert_false(sim.blueprint_has_doctrines(b), "money has no doctrine")
			continue
		assert_true(sim.blueprint_has_doctrines(b),
			"%s offers a specialization" % sim.blueprint_display_name(b))
		assert_false(sim.doctrine_name(b, 0).is_empty(), "the first is named")
		assert_false(sim.doctrine_name(b, 1).is_empty(), "and the second")

func test_a_rate_doctrine_shortens_the_effective_interval() -> void:
	var sim := _sim()
	var i := _turret_at_top_tier(sim)
	var before := sim._effective_rate(i)
	sim.queue_doctrine(sim.tick(), i, 0)
	sim.step()
	assert_gt(sim._effective_rate(i), before, "Shredder fires faster")

func test_the_ap_doctrine_ignores_armour() -> void:
	# The one way a wall of small rounds answers plate, bought by giving up the
	# rate doctrine. Measured through a real projectile against a Brood.
	var sim := _sim()
	var i := _turret_at_top_tier(sim)
	sim.queue_doctrine(sim.tick(), i, 1)
	sim.step()
	assert_eq(sim.platform_doctrine(i), 1, "AP Core chosen")
	sim._begin_wave(0)
	var brood := sim.enemy_index("brood")
	assert_gt(float(sim.enemy_armour(brood)), 0.0, "fixture sanity: a Brood wears armour")
	sim._spawn_at(brood, 20.0)
	var target := -1
	for e in sim.e_alive.size():
		if sim.e_alive[e] == 1:
			target = e
			break
	var start: int = sim.e_hp[target]
	for _t in 400:
		sim.step()
		if sim.e_alive[target] == 0 or sim.e_hp[target] < start:
			break
	var landed: int = start - sim.e_hp[target]
	# What the round would land through the matrix WITHOUT armour: damage times
	# matchup, veterancy 0, no links. Computed from the sim's own numbers.
	var expected := maxi(1, int(round(float(sim.platform_damage(i))
		* sim.matchup(sim.blueprint_damage_type(sim.platform_blueprint(i)),
			sim.enemy_armour_class(brood)))))
	assert_eq(landed, expected, "armour took nothing off the AP round")

# --- premium ground -----------------------------------------------------------

func test_every_map_authors_premium_ground() -> void:
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		assert_gt(float((db.map.get("premium_cells", []) as Array).size()), 0.0,
			"%s has ground worth fighting over" % str(level["map"]))

func test_premium_ground_makes_the_turret_on_it_better() -> void:
	var sim := _sim()
	var premium := Vector2i(-1, -1)
	var kind := -1
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			if sim.cell_premium(cx, cy) >= 0 and sim.cell_is_unlocked(cx, cy):
				premium = Vector2i(cx, cy)
				kind = sim.cell_premium(cx, cy)
				break
		if kind >= 0:
			break
	if kind < 0:
		# This act's starting reach does not own a premium cell; buying ground is
		# player behaviour, so force-own one rather than skipping the test.
		for cy in sim.grid_rows():
			for cx in sim.grid_cols():
				if sim.cell_premium(cx, cy) >= 0 and sim.cell_is_buildable(cx, cy):
					premium = Vector2i(cx, cy)
					kind = sim.cell_premium(cx, cy)
					sim._cell_unlocked[cy * sim.grid_cols() + cx] = 1
					break
			if kind >= 0:
				break
	assert_gte(float(kind), 0.0, "fixture sanity: the map authored a reachable premium cell")
	sim._capital = 1 << 24
	sim.queue_place(0, sim.cell_centre_x(premium.x), sim.cell_centre_y(premium.y), 0)
	sim.step()
	assert_eq(sim.t_count, 1, "the turret stood on it")
	assert_eq(sim.platform_site(0), kind, "and knows what it stands on")
	var plain := _sim()
	var spot := SimFixture.a_site(plain)
	plain.queue_place(0, spot[0], spot[1], 0)
	plain.step()
	var better := sim.platform_range_sq(0) > plain.platform_range_sq(0) \
		or sim._effective_rate(0) > plain._effective_rate(0)
	assert_true(better, "high ground reaches further or a power tap fires faster")

# --- veterancy ----------------------------------------------------------------

func test_kills_are_credited_to_the_turret_that_landed_them() -> void:
	var sim := _sim()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	sim._begin_wave(0)
	sim._spawn_at(sim.enemy_index("swarm"), 30.0)
	for _t in 600:
		sim.step()
		if sim.platform_kills(0) > 0:
			break
	assert_gt(float(sim.platform_kills(0)), 0.0, "the kill went on the turret's record")

func test_rank_raises_damage() -> void:
	var sim := _sim()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	var green := sim._effective_damage(0)
	sim.t_kills[0] = 1 << 20
	assert_gt(sim._effective_damage(0), green, "a veteran hits harder")
	assert_gt(float(sim.platform_rank(0)), 0.0, "and wears a rank")

func test_selling_a_veteran_throws_the_ranks_away() -> void:
	var sim := _sim()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	sim.t_kills[0] = 1 << 20
	sim.queue_sell(sim.tick(), 0)
	sim.step()
	var spot2 := SimFixture.a_site(sim)
	sim.queue_place(sim.tick(), spot2[0], spot2[1], 0)
	sim.step()
	assert_eq(sim.platform_kills(0), 0, "the replacement starts green")

# --- the contract every command shares ----------------------------------------

func test_the_new_commands_replay_bit_exactly() -> void:
	var recorded := SimFixture.fresh(9090)
	recorded._capital = 1 << 24
	var spot := SimFixture.a_site(recorded)
	recorded.queue_place(0, spot[0], spot[1], 0)
	var rig := recorded.blueprint_index("rig")
	var spot2 := [spot[0] + 92, spot[1]]
	recorded.queue_place(0, int(spot2[0]), int(spot2[1]), rig)
	for _u in 4:
		recorded.queue_upgrade(1 + _u, 0)
	recorded.queue_doctrine(8, 0, 1)
	var log := {"tick": PackedInt32Array([0, 0, 1, 2, 3, 4, 8]),
		"kind": PackedInt32Array([Sim.CMD_PLACE, Sim.CMD_PLACE, Sim.CMD_UPGRADE,
			Sim.CMD_UPGRADE, Sim.CMD_UPGRADE, Sim.CMD_UPGRADE, Sim.CMD_DOCTRINE]),
		"a": PackedInt32Array([spot[0], int(spot2[0]), 0, 0, 0, 0, 0]),
		"b": PackedInt32Array([spot[1], int(spot2[1]), 0, 0, 0, 0, 1]),
		"c": PackedInt32Array([0, rig, 0, 0, 0, 0, 0])}
	for _t in 900:
		recorded.step()
	var replayed := SimFixture.fresh(9090)
	replayed._capital = 1 << 24
	SimFixture.replay(replayed, log, 900)
	assert_eq(replayed.state_hash(), recorded.state_hash(),
		"rig placement and doctrine choice land identically in a replay")
