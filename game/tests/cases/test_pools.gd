extends TestCase

## Pooling correctness. Recycling slots is what keeps the per-frame path free of
## allocations, but it introduces one specific hazard - a stale index pointing at
## a slot that now holds somebody else - so that is what most of this file is
## about.

func test_enemy_slots_are_recycled_rather_than_grown() -> void:
	var db := SimFixture.database()
	var capacity := int(db.sim["max_enemies"])
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	for _cycle in capacity * 3:
		sim._spawn(0)
		assert_lte(float(sim.e_live_count), float(capacity), "live count never exceeds the pool")
		sim._despawn_enemy(_first_live(sim))
	assert_eq(sim.e_live_count, 0, "everything was returned to the pool")
	assert_eq(sim.spawn_overflow(), 0, "recycling meant the pool never ran dry")
	assert_eq(sim.e_alive.size(), capacity, "the backing array never grew")

func test_pool_exhaustion_is_counted_not_hidden() -> void:
	var db := SimFixture.database()
	db.sim["max_enemies"] = 4.0
	var sim := Sim.new(db, 1)
	sim._begin_wave(0)
	for _i in 10:
		sim._spawn(0)
	assert_eq(sim.e_live_count, 4, "the pool filled to its ceiling")
	assert_eq(sim.spawn_overflow(), 6, "and the six it could not take were counted")

func test_a_projectile_cannot_hit_the_enemy_that_inherited_its_target_slot() -> void:
	# The ABA problem, concretely. Without the generation stamp, this test would
	# show a brand-new full-health enemy taking damage from a shot fired at
	# something that died before it landed.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(0)
	var victim := _first_live(sim)
	sim._try_place(120.0, 430.0, 0)
	sim._fire(0, victim)
	assert_eq(sim.p_live_count, 1, "a shot is in flight")

	var generation_before := sim.e_gen[victim]
	sim._despawn_enemy(victim)
	sim._spawn(0)
	var newcomer := _first_live(sim)
	assert_eq(newcomer, victim, "fixture sanity: the slot really was reused")
	assert_ne(sim.e_gen[newcomer], generation_before, "the generation stamp advanced")

	var hp_before := sim.e_hp[newcomer]
	sim._advance_projectiles()
	assert_eq(sim.e_hp[newcomer], hp_before, "the newcomer took no damage")
	assert_eq(sim.p_live_count, 0, "the orphaned shot was discarded")
	assert_eq(sim.kills(), 0, "and no phantom kill was credited")

func test_projectiles_expire_rather_than_leaking_slots() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(0)
	sim._try_place(120.0, 430.0, 0)
	var target := _first_live(sim)
	sim._fire(0, target)
	# Kill the target so the shot is orphaned, then let it resolve.
	sim._despawn_enemy(target)
	for _i in 200:
		sim._advance_projectiles()
	assert_eq(sim.p_live_count, 0, "no projectile is stuck alive")

func test_the_projectile_pool_survives_a_full_engagement() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim)
	assert_eq(sim.p_live_count, 0, "every shot fired was resolved by the end")
	assert_eq(sim.e_live_count, 0, "and every enemy was resolved")

func test_platforms_cannot_be_stacked_on_the_same_spot() -> void:
	# Free placement replaced fixed pads, so min_platform_spacing is now the only
	# thing stopping an unlimited tower of turrets on one square.
	var sim := SimFixture.fresh()
	assert_eq(sim._try_place(120.0, 430.0, 0), Sim.BUILD_OK, "the first one builds")
	assert_eq(sim._try_place(120.0, 430.0, 0), Sim.BUILD_OVERLAPS, "the second is refused")
	# Just outside the spacing radius is fine again.
	var clear := 120.0 + sim.build_min_spacing() + 1.0
	assert_eq(sim._try_place(clear, 430.0, 0), Sim.BUILD_OK, "far enough away is legal")

func _first_live(sim: Sim) -> int:
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			return i
	return -1
