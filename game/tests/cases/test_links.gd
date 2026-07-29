extends TestCase

## Support links, and interest on Capital.
##
## Both exist to make a decision out of something that previously had only one
## right answer. Interest makes holding money an alternative to spending it;
## support links make WHICH weapon sits next to WHICH matter, on a board where
## placement already decided coverage and nothing else.
##
## The rule the link mechanic rests on is that a family reaches other families
## only. If that ever stops being true, a clustered line of one weapon becomes
## the answer again and the whole thing collapses into a flat damage bonus.

func _families(sim: Sim) -> Array:
	return [sim.blueprint_index("ballistic"), sim.blueprint_index("cannon"),
		sim.blueprint_index("suppressor"), sim.blueprint_index("railgun")]

## Two turrets close enough to link, of the families asked for, both taken to the
## given tiers. Returns their indices.
func _pair(sim: Sim, family_a: int, family_b: int, tier_b: int) -> PackedInt32Array:
	var sites := SimFixture.candidate_sites(sim)
	# Adjacent sites along the road are well inside every family's support radius.
	sim.queue_place(0, sites[0], sites[1], family_a)
	sim.step()
	var placed := PackedInt32Array()
	placed.append(0)
	for k in range(2, sites.size(), 2):
		var dx := float(sites[k]) - sim.t_x[0]
		var dy := float(sites[k + 1]) - sim.t_y[0]
		if dx * dx + dy * dy > 140.0 * 140.0:
			continue
		sim.queue_place(sim.tick(), sites[k], sites[k + 1], family_b)
		sim.step()
		if sim.t_count == 2:
			placed.append(1)
			break
	assert_eq(placed.size(), 2, "fixture sanity: two turrets went down close together")
	for _t in tier_b:
		# Free upgrades: this is a test about links, not about affording them.
		sim._capital = 1 << 24
		sim.queue_upgrade(sim.tick(), 1)
		sim.step()
	assert_eq(sim.platform_tier(1), tier_b, "fixture sanity: the granting turret is at tier")
	return placed

func test_a_turret_on_its_own_gets_nothing() -> void:
	var sim := SimFixture.fresh()
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	assert_eq(sim.platform_rate_bonus(0), 0.0, "no rate")
	assert_eq(sim.platform_damage_bonus(0), 0.0, "no damage")
	assert_eq(sim.platform_range_bonus(0), 0.0, "no range")
	assert_eq(sim.platform_link_count(0), 0, "and nothing linked to it")

func test_a_family_does_not_reach_its_own_kind() -> void:
	# The rule the whole mechanic rests on. Four Autocannons in a row must get
	# nothing from each other, or clustering one weapon becomes the answer again.
	var sim := SimFixture.fresh()
	var families := _families(sim)
	_pair(sim, families[0], families[0], 1)
	assert_eq(sim.platform_link_count(0), 0, "same family, no link")
	assert_eq(sim.platform_rate_bonus(0), 0.0, "and no bonus")

func test_a_different_family_next_door_is_worth_something() -> void:
	var sim := SimFixture.fresh()
	var families := _families(sim)
	_pair(sim, families[0], families[2], 1)
	assert_gt(sim.platform_rate_bonus(0), 0.0,
		"a tier-2 Suppressor lends the Autocannon beside it rate of fire")
	assert_eq(sim.platform_link_count(0), 1, "one source")

func test_nothing_is_projected_at_tier_one() -> void:
	# Flavour says the coupling arrives with the first refit. Mechanically it
	# keeps a fresh board's opening spree from linking itself into a lattice
	# before anything has been upgraded - measured with links live at tier 1,
	# twenty-four tier-1 turrets cleared the whole of Highway act II unattended.
	var sim := SimFixture.fresh()
	var families := _families(sim)
	_pair(sim, families[0], families[2], 0)
	assert_eq(sim.platform_tier(1), 0, "fixture sanity: the neighbour is at tier 1")
	assert_eq(sim.platform_rate_bonus(0), 0.0, "so it lends nothing")

func test_upgrading_the_neighbour_helps_the_turret_beside_it() -> void:
	# The first reason in the game to upgrade something that is not your best gun.
	var sim := SimFixture.fresh()
	var families := _families(sim)
	_pair(sim, families[0], families[2], 1)
	var early := sim.platform_rate_bonus(0)
	sim._capital = 1 << 24
	sim.queue_upgrade(sim.tick(), 1)
	sim.step()
	assert_gt(sim.platform_rate_bonus(0), early,
		"a tier-3 neighbour is worth more than a tier-2 one")

func test_a_link_actually_changes_what_the_turret_does() -> void:
	# A multiplier nothing reads is not a mechanic.
	var alone := SimFixture.fresh()
	var spot := SimFixture.a_site(alone)
	alone.queue_place(0, spot[0], spot[1], 0)
	alone.step()
	var sim := SimFixture.fresh()
	var families := _families(sim)
	_pair(sim, families[0], families[2], 1)
	assert_gt(sim.platform_dps(0), alone.platform_dps(0),
		"the linked Autocannon fires harder than the lonely one")

func test_a_lent_range_bonus_actually_extends_the_reach() -> void:
	# Range is the one bonus that changes what the turret can SEE, so it has to
	# reach the targeting query and the range ring, not just a readout.
	var alone := SimFixture.fresh()
	var families := _families(alone)
	var spot := SimFixture.a_site(alone)
	alone.queue_place(0, spot[0], spot[1], families[1])
	alone.step()
	var lent := SimFixture.fresh()
	# A Mortar receiving from an Autocannon, which is the family that lends reach.
	_pair(lent, families[1], families[0], 1)
	assert_gt(lent.platform_range_bonus(0), 0.0, "the bonus is there")
	assert_gt(lent.platform_range(0), alone.platform_range(0),
		"and the turret genuinely reaches further than the same turret alone")

func test_the_total_is_capped() -> void:
	# Capped by TOTAL and not by number of sources, so the answer never depends on
	# which turret the loop happened to reach first.
	# Stopped partway: the ceiling is a property of the multipliers, and a board
	# with a hundred turrets on it proves it exactly as well as a finished act does
	# for a fraction of the suite's budget.
	var sim := SimFixture.for_level("reactor_01", "reactor_act3")
	SimFixture.run_greedy(sim, true, 5, 1)
	var cap := float(SimFixture.database().economy["support_cap_fire_rate"])
	for i in sim.t_count:
		assert_lte(sim.platform_rate_bonus(i), cap + 0.0001,
			"turret %d is within the fire-rate ceiling" % i)

func test_links_are_part_of_the_state_hash() -> void:
	var sim := SimFixture.fresh()
	var families := _families(sim)
	_pair(sim, families[0], families[2], 1)
	var before := sim.state_hash()
	sim.t_rate_mult[0] = 9.0
	assert_ne(sim.state_hash(), before, "a multiplier is state, and is hashed")

func test_selling_the_neighbour_takes_the_bonus_with_it() -> void:
	var sim := SimFixture.fresh()
	var families := _families(sim)
	_pair(sim, families[0], families[2], 1)
	assert_gt(sim.platform_rate_bonus(0), 0.0, "fixture sanity: linked")
	sim.queue_sell(sim.tick(), 1)
	sim.step()
	assert_eq(sim.t_count, 1, "the neighbour is gone")
	assert_eq(sim.platform_rate_bonus(0), 0.0, "and so is what it was lending")

func test_a_carried_board_arrives_projecting_what_it_projected() -> void:
	# Tiers carry between acts now, so the lattice carries with them - a support
	# web the player invested in is part of "what you built", and what you built
	# is what you keep. The acts are authored against that.
	var levels := Database.load_levels()
	var pair := []
	for i in levels.size() - 1:
		if str(levels[i]["map"]) == str(levels[i + 1]["map"]):
			pair = [levels[i], levels[i + 1]]
			break
	var first := SimFixture.for_level(str(pair[0]["map"]), str(pair[0]["engagement"]))
	SimFixture.run_greedy(first)
	var second := SimFixture.start_act(pair[1], first.board_snapshot())
	second.step()
	assert_gt(float(second.t_count), 0.0, "fixture sanity: a board carried")
	var receiving := 0
	for i in second.t_count:
		if second.platform_rate_bonus(i) > 0.0 or second.platform_damage_bonus(i) > 0.0 \
				or second.platform_range_bonus(i) > 0.0:
			receiving += 1
	assert_gt(float(receiving), 0.0,
		"an inherited board with upgraded neighbours still projects its links")

# --- interest -----------------------------------------------------------------

func test_capital_in_hand_earns_at_the_start_of_a_wave() -> void:
	var sim := SimFixture.fresh()
	var rate := sim.interest_rate()
	assert_gt(rate, 0.0, "fixture sanity: interest is switched on")
	var held := sim.capital()
	sim._begin_wave(1)
	var expected := mini(int(floor(float(held) * rate)), sim.interest_cap())
	assert_eq(sim.capital(), held + expected, "paid on what was in hand")
	assert_eq(sim.interest_paid(), expected, "and recorded")

func test_the_opening_wave_pays_nothing() -> void:
	# Otherwise it is not interest, it is a bigger starting purse.
	var sim := SimFixture.fresh()
	var held := sim.capital()
	sim._begin_wave(0)
	assert_eq(sim.capital(), held, "wave one pays nothing")
	assert_eq(sim.interest_paid(), 0, "and nothing is recorded")

func test_interest_is_capped() -> void:
	# A percentage of an unbounded pile is an unbounded pile, and the right play
	# late in a long act would become building nothing and banking.
	var sim := SimFixture.fresh()
	sim._capital = 1 << 20
	sim._begin_wave(1)
	assert_eq(sim.interest_paid(), sim.interest_cap(),
		"an enormous balance earns exactly the ceiling")

func test_spending_everything_earns_nothing() -> void:
	var sim := SimFixture.fresh()
	sim._capital = 0
	sim._begin_wave(1)
	assert_eq(sim.interest_paid(), 0, "no float, no interest")

func test_interest_is_part_of_the_state_hash() -> void:
	var rich := SimFixture.fresh()
	var poor := SimFixture.fresh()
	rich._begin_wave(1)
	poor._capital = 0
	poor._begin_wave(1)
	assert_ne(rich.state_hash(), poor.state_hash(),
		"two boards that banked different amounts are not in the same state")
