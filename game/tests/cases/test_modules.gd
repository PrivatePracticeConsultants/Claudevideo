extends TestCase

## Modules, and Integrity that persists across a board.
##
## The module draft is the roguelite hook: three offered after a win that
## continues a chain, one kept for the rest of the board. Both properties matter.
## Three, so it is a choice; for the rest of the BOARD and no further, because a
## run that accumulated every module across forty-eight levels would end as one
## obvious stack rather than as a series of decisions.
##
## Every module is applied at construction, like an inherited board and like
## salvage, so it is part of the state a replay starts from rather than an event
## partway through one.

func _first_chain_pair() -> Array:
	var levels := Database.load_levels()
	for i in levels.size() - 1:
		if str(levels[i]["map"]) == str(levels[i + 1]["map"]):
			return [levels[i], levels[i + 1]]
	return []

func _with(module_id: String) -> Sim:
	var sim := SimFixture.fresh()
	var ids := PackedStringArray()
	ids.append(module_id)
	sim.apply_modules(ids)
	return sim

func test_the_data_file_defines_a_pool_worth_drafting() -> void:
	# Three offered without replacement, so fewer than four makes the last draft of
	# a board a formality.
	var ids := Database.load_modules()
	assert_gte(float(ids.size()), 4.0, "there are enough modules to draft from")
	for id in ids:
		var text := Database.module_text(id)
		assert_eq(text.size(), 2, "%s has a name and a description" % id)
		assert_false(text[1].is_empty(), "%s explains itself" % id)

func test_every_module_changes_something_measurable() -> void:
	# A module that loads, is offered, is picked, and changes nothing is the exact
	# silent-nothing failure the validators exist for.
	var bare := SimFixture.fresh()
	for id in Database.load_modules():
		var fitted := _with(id)
		assert_ne(fitted.state_hash(), bare.state_hash(),
			"%s changes the opening state" % id)

func test_a_rate_module_makes_every_turret_fire_faster() -> void:
	var bare := SimFixture.fresh()
	var spot := SimFixture.a_site(bare)
	bare.queue_place(0, spot[0], spot[1], 0)
	bare.step()
	var fitted := _with("coolant_loop")
	fitted.queue_place(0, spot[0], spot[1], 0)
	fitted.step()
	assert_gt(fitted.platform_dps(0), bare.platform_dps(0), "it fires harder")
	assert_gt(fitted.module_rate_bonus(), 0.0, "and says so")

func test_a_range_module_reaches_further() -> void:
	var bare := SimFixture.fresh()
	var spot := SimFixture.a_site(bare)
	bare.queue_place(0, spot[0], spot[1], 0)
	bare.step()
	var fitted := _with("long_optics")
	fitted.queue_place(0, spot[0], spot[1], 0)
	fitted.step()
	assert_gt(fitted.platform_range(0), bare.platform_range(0), "further")

func test_an_economy_module_opens_the_act_richer() -> void:
	var bare := SimFixture.fresh()
	var fitted := _with("forward_depot")
	assert_gt(float(fitted.capital()), float(bare.capital()), "more in hand")

func test_an_integrity_module_raises_the_ceiling_as_well_as_the_bar() -> void:
	# Raising current integrity without the maximum would make the bar read over
	# 100%, and would be silently undone the moment anything clamped it.
	var bare := SimFixture.fresh()
	var fitted := _with("corridor_hardening")
	assert_gt(float(fitted.integrity()), float(bare.integrity()), "more integrity")
	assert_gt(float(fitted.integrity_max()), float(bare.integrity_max()), "and more room for it")

func test_the_discount_module_makes_upgrades_cheaper() -> void:
	var bare := SimFixture.fresh()
	var spot := SimFixture.a_site(bare)
	bare.queue_place(0, spot[0], spot[1], 0)
	bare.step()
	var fitted := _with("field_refit")
	fitted.queue_place(0, spot[0], spot[1], 0)
	fitted.step()
	assert_lt(float(fitted.upgrade_cost(0)), float(bare.upgrade_cost(0)), "cheaper")

func test_the_counter_jam_module_shortens_a_jam() -> void:
	var bare := SimFixture.fresh()
	var fitted := _with("counter_jam")
	for sim in [bare, fitted]:
		var spot := SimFixture.a_site(sim)
		sim.queue_place(0, spot[0], spot[1], 0)
		sim.step()
		sim._begin_wave(0)
		sim._spawn_at(sim.enemy_index("jammer"), 0.0)
		sim.e_x[0] = sim.t_x[0]
		sim.e_y[0] = sim.t_y[0]
		sim._jam_around(0, sim.e_type[0])
	assert_gt(float(bare.t_disabled[0]), 0.0, "fixture sanity: the bare board was jammed")
	assert_lt(float(fitted.t_disabled[0]), float(bare.t_disabled[0]),
		"and the fitted one for less time")

func test_the_relay_module_widens_the_support_radius() -> void:
	var bare := SimFixture.fresh()
	var fitted := _with("link_relay")
	var family := bare.blueprint_index("suppressor")
	assert_gt(fitted.blueprint_support_radius(family),
		bare.blueprint_support_radius(family), "links reach further")

func test_an_unknown_module_is_ignored_rather_than_fatal() -> void:
	# The offer is made by the layer above. A module removed from the data file
	# between two runs must not make a saved chain unplayable.
	var sim := SimFixture.fresh()
	var ids := PackedStringArray()
	ids.append("no_such_module")
	sim.apply_modules(ids)
	assert_eq(sim.modules().size(), 0, "it was skipped, not fitted")

func test_modules_are_part_of_the_state_hash() -> void:
	var bare := SimFixture.fresh()
	var fitted := _with("rail_mass")
	assert_ne(fitted.state_hash(), bare.state_hash(), "a fitted board is a different board")

func test_modules_carry_to_the_next_act_of_the_chain() -> void:
	var pair := _first_chain_pair()
	assert_eq(pair.size(), 2, "fixture sanity: the campaign has a chain in it")
	var first := SimFixture.start_act(pair[0], {})
	var ids := PackedStringArray()
	ids.append("rail_mass")
	first.apply_modules(ids)
	var snapshot := first.board_snapshot()
	assert_true(snapshot.has("modules"), "the snapshot carries them")
	var held: PackedStringArray = snapshot["modules"]
	assert_eq(held.size(), 1, "exactly what was fitted")

func test_two_modules_stack() -> void:
	var one := _with("coolant_loop")
	var sim := SimFixture.fresh()
	var ids := PackedStringArray()
	ids.append("coolant_loop")
	ids.append("rail_mass")
	sim.apply_modules(ids)
	assert_eq(sim.modules().size(), 2, "both fitted")
	assert_eq(sim.module_rate_bonus(), one.module_rate_bonus(), "rate from the one that grants it")
	assert_gt(sim.module_damage_bonus(), 0.0, "and damage from the other")

func test_a_run_with_modules_replays_identically() -> void:
	var log := {}
	var hashes := PackedInt64Array()
	for _pass in 2:
		var sim := SimFixture.fresh()
		var ids := PackedStringArray()
		ids.append("coolant_loop")
		ids.append("long_optics")
		sim.apply_modules(ids)
		if log.is_empty():
			log = SimFixture.run_greedy(sim, true, 4, 1)
		else:
			SimFixture.replay(sim, log)
		hashes.append(sim.state_hash())
	assert_eq(hashes[0] != 0, true, "fixture sanity: something ran")
	var third := SimFixture.fresh()
	var ids := PackedStringArray()
	ids.append("coolant_loop")
	ids.append("long_optics")
	third.apply_modules(ids)
	SimFixture.replay(third, log)
	assert_eq(third.state_hash(), hashes[1], "same modules, same log, same end state")

# --- integrity that persists across a board -----------------------------------

func test_damage_carries_between_the_acts_of_a_board() -> void:
	# Without it a sloppy act I costs nothing in act IV, and a chain is four fresh
	# starts wearing a chain's clothes.
	var pair := _first_chain_pair()
	var first := SimFixture.start_act(pair[0], {})
	first._integrity = 61
	var second := SimFixture.start_act(pair[1], first.board_snapshot())
	assert_eq(second.integrity(), 61, "the next act opens on what the last one left")
	assert_true(second.integrity_inherited(), "and knows that it did")

func test_a_new_board_starts_whole() -> void:
	# A board is a contract. A bad run three boards ago following you forever with
	# no way to recover it is a punishment, not a decision.
	var levels := Database.load_levels()
	var boundary := []
	for i in levels.size() - 1:
		if str(levels[i]["map"]) != str(levels[i + 1]["map"]):
			boundary = [levels[i], levels[i + 1]]
			break
	var last := SimFixture.start_act(boundary[0], {})
	last._integrity = 7
	var opening := SimFixture.start_act(boundary[1], last.board_snapshot())
	assert_eq(opening.integrity(), opening.integrity_max(), "the new board is whole")
	assert_false(opening.integrity_inherited(), "and says it inherited nothing")

func test_inherited_integrity_never_exceeds_the_maximum() -> void:
	var pair := _first_chain_pair()
	var sim := SimFixture.start_act(pair[1], {})
	sim.inherit_integrity(1 << 20)
	assert_eq(sim.integrity(), sim.integrity_max(), "clamped, not banked")

func test_inheriting_integrity_is_part_of_the_state_hash() -> void:
	var pair := _first_chain_pair()
	var whole := SimFixture.start_act(pair[1], {})
	var hurt := SimFixture.start_act(pair[1], {})
	hurt.inherit_integrity(40)
	assert_ne(hurt.state_hash(), whole.state_hash(),
		"a board opened on 40 integrity is not the same board as one opened on 100")
