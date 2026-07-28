extends TestCase

## Capital is engagement-scoped and integer-valued. Section 4.1's structural
## squeeze - income growing at 1.05 against HP at 1.10 - only means anything if
## the numbers actually come from the data files, so these tests read the
## expected values out of JSON rather than hardcoding them.

## Buildable spots are asked of the fixture rather than hardcoded: a coordinate
## that is valid today stops being valid the moment a map is re-authored.

func test_starting_capital_and_integrity_come_from_data() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	assert_eq(sim.capital(), int(db.engagement["starting_capital"]), "capital is loaded, not hardcoded")
	assert_eq(sim.integrity(), int(db.economy["starting_integrity"]), "integrity is loaded, not hardcoded")

func test_an_engagement_can_override_starting_capital() -> void:
	# Later boards start richer, per section 4.1, so a tier-4 turret is reachable
	# in a single fight by the end of the campaign. Compared board-opening act to
	# board-opening act: within a chain the later acts start *poorer*, because
	# they inherit a board and are buying an extension rather than an army.
	var first := SimFixture.for_level("highway_01", "highway_act1")
	var last := SimFixture.for_level("lastlight_01", "lastlight_act1")
	assert_gt(float(last.capital()), float(first.capital()),
		"later boards start with more Capital")
	var inheriting := SimFixture.for_level("lastlight_01", "lastlight_act3")
	assert_lt(float(inheriting.capital()), float(last.capital()),
		"an act that inherits a board is funded for the extension, not for the board")

func test_placing_a_platform_costs_its_listed_price() -> void:
	var sim := SimFixture.fresh()
	var before := sim.capital()
	var cost := sim.blueprint_cost(0)
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	assert_eq(sim.capital(), before - cost, "capital drops by exactly the cost")
	assert_eq(sim.t_count, 1, "and a platform exists")

func test_a_placement_that_cannot_be_afforded_is_rejected_cleanly() -> void:
	# Sites are sampled along the road closer together than min_platform_spacing,
	# so consecutive ones reject *each other* once the first is built. Thin them
	# out first, or this measures the spacing rule while claiming to measure the
	# affordability one - which is how it started failing the moment the opening
	# level could afford enough turrets to reach its own neighbours.
	var sim := SimFixture.fresh()
	var spaced := _spaced_sites(sim)
	var affordable := int(sim.capital() / sim.blueprint_cost(0))
	assert_gt(float(spaced.size() / 2), float(affordable + 3),
		"fixture sanity: enough legal spots to overspend on")
	for n in affordable + 3:
		sim.queue_place(0, spaced[n * 2], spaced[n * 2 + 1], 0)
	sim.step()
	assert_eq(sim.t_count, affordable, "only what could be paid for was built")
	assert_eq(sim.rejected_commands(), 3, "the rest were rejected, not silently dropped")
	assert_gte(float(sim.capital()), 0.0, "capital never goes negative")

## Candidate sites thinned so no two are within min_platform_spacing of each
## other, i.e. spots that stay legal as they get built out.
func _spaced_sites(sim: Sim) -> PackedInt32Array:
	var all := SimFixture.candidate_sites(sim)
	var out := PackedInt32Array()
	var gap := sim.build_min_spacing()
	for i in range(0, all.size() - 1, 2):
		var x := float(all[i])
		var y := float(all[i + 1])
		var clear := true
		for j in range(0, out.size() - 1, 2):
			var dx := float(out[j]) - x
			var dy := float(out[j + 1]) - y
			if dx * dx + dy * dy < gap * gap:
				clear = false
				break
		if clear:
			out.append(all[i])
			out.append(all[i + 1])
	return out

func test_building_on_the_road_is_refused() -> void:
	var sim := SimFixture.fresh()
	# Straight onto the first waypoint - squarely in the middle of the corridor.
	var verdict := sim.can_build_at(sim.waypoint_x(1), sim.waypoint_y(1), 0)
	assert_eq(verdict, Sim.BUILD_ON_PATH, "the road itself is not buildable")

func test_ground_far_from_the_road_is_locked_rather_than_forbidden() -> void:
	# Distant ground is not illegal, it is unowned - the player can buy their way
	# out to it one cell at a time.
	# "Far" has to mean far from the *whole* route, not far from the segment the
	# sample was taken on. Offsetting perpendicular to one segment can land inside
	# the owned band of another one where the road bends, which is how this test
	# started failing the moment the boards got longer and curvier.
	var sim := SimFixture.fresh()
	var found := false
	var prog := 0.0
	while prog <= sim.path_length() and not found:
		for side: float in [1.0, -1.0]:
			sim.sample_for_render(prog, (sim.build_max_distance() + 120.0) * side)
			var x := sim.out_x()
			var y := sim.out_y()
			if sim.distance_to_path(x, y) <= sim.build_max_distance():
				continue
			# ...and still on the board, or the answer is "out of bounds" and the
			# question about ownership never gets asked.
			if x < 0.0 or y < 0.0 or x > sim.bounds_width() or y > sim.bounds_height():
				continue
			assert_eq(sim.can_build_at(x, y, 0), Sim.BUILD_LOCKED,
				"far ground is locked, not permanently refused")
			found = true
			break
		prog += 200.0
	assert_true(found, "fixture sanity: the board has ground outside the owned band")

func test_out_of_bounds_placements_are_rejected() -> void:
	var sim := SimFixture.fresh()
	sim.queue_place(0, -5000, -5000, 0)
	sim.queue_place(0, 99999, 99999, 0)
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 9999)
	sim.step()
	assert_eq(sim.t_count, 0, "nothing was built from nonsense input")
	assert_eq(sim.rejected_commands(), 3, "each bad command was counted")

func test_a_kill_pays_its_bounty() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var before := sim.capital()
	var bounty := sim.e_bounty[0]
	assert_gt(float(bounty), 0.0, "wave 1 bounty is set")
	sim._damage_enemy(0, sim.e_hp[0])
	assert_eq(sim.capital(), before + bounty, "the kill paid exactly its bounty")
	assert_eq(sim.kills(), 1, "and counted as a kill")

func test_partial_damage_pays_nothing() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var before := sim.capital()
	sim._damage_enemy(0, 1)
	assert_eq(sim.capital(), before, "wounding is not killing")
	assert_eq(sim.e_live_count, 1, "the enemy is still alive")

func test_hp_and_bounty_both_scale_with_the_wave() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var hp_wave_1 := sim.e_hp[0]
	var bounty_wave_1 := sim.e_bounty[0]
	sim._despawn_enemy(0)
	sim._begin_wave(9)
	sim._spawn(sim.enemy_index("walker"))
	assert_gt(float(sim.e_hp[0]), float(hp_wave_1), "wave 10 enemies are tougher")
	assert_gt(float(sim.e_bounty[0]), float(bounty_wave_1), "and worth more")

func test_hp_outgrows_bounty() -> void:
	# The squeeze from section 4.1: if income ever kept pace with HP, late waves
	# would stop forcing decisions and the engagement would flatten out.
	var db := SimFixture.database()
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var hp_ratio := float(sim.e_hp[0])
	var bounty_ratio := float(sim.e_bounty[0])
	sim._despawn_enemy(0)
	sim._begin_wave(9)
	sim._spawn(sim.enemy_index("walker"))
	hp_ratio = float(sim.e_hp[0]) / hp_ratio
	bounty_ratio = float(sim.e_bounty[0]) / bounty_ratio
	assert_gt(hp_ratio, bounty_ratio, "HP must outrun income across the engagement")
