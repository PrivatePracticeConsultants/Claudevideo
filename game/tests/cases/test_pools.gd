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
		sim._spawn(sim.enemy_index("walker"))
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
		sim._spawn(sim.enemy_index("walker"))
	assert_eq(sim.e_live_count, 4, "the pool filled to its ceiling")
	assert_eq(sim.spawn_overflow(), 6, "and the six it could not take were counted")

func test_a_projectile_cannot_hit_the_enemy_that_inherited_its_target_slot() -> void:
	# The ABA problem, concretely. Without the generation stamp, this test would
	# show a brand-new full-health enemy taking damage from a shot fired at
	# something that died before it landed.
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn(sim.enemy_index("walker"))
	var victim := _first_live(sim)
	var spot := SimFixture.a_site(sim)
	sim._try_place(float(spot[0]), float(spot[1]), 0)
	sim._fire(0, victim)
	assert_eq(sim.p_live_count, 1, "a shot is in flight")

	var generation_before := sim.e_gen[victim]
	sim._despawn_enemy(victim)
	sim._spawn(sim.enemy_index("walker"))
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
	sim._spawn(sim.enemy_index("walker"))
	var spot := SimFixture.a_site(sim)
	sim._try_place(float(spot[0]), float(spot[1]), 0)
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
	var spot := SimFixture.a_site(sim)
	assert_eq(sim._try_place(float(spot[0]), float(spot[1]), 0), Sim.BUILD_OK, "the first one builds")
	assert_eq(sim._try_place(float(spot[0]), float(spot[1]), 0), Sim.BUILD_OVERLAPS, "the second is refused")
	# Far enough along the road is fine again.
	var elsewhere := SimFixture.a_site(sim, 8)
	assert_eq(sim._try_place(float(elsewhere[0]), float(elsewhere[1]), 0), Sim.BUILD_OK,
		"a spot clear of the first is legal")

func _first_live(sim: Sim) -> int:
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			return i
	return -1

# --- the live-slot bounds ------------------------------------------------------
#
# The tick and the renderer both sweep to a high-water bound instead of to the
# pool size, which took sim.step from 4.9ms to 0.5ms. A bound that was ever too
# LOW would silently stop simulating something that is alive - the worst class of
# bug this project can ship - so it is pinned from both directions here.

func test_the_enemy_bound_covers_every_live_slot() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim, true, 6, 12)
	var highest := -1
	var live := 0
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			highest = i
			live += 1
	assert_gt(float(live), 0.0, "fixture sanity: drones are on the board")
	assert_gt(float(sim.enemy_slot_bound()), float(highest),
		"the bound is past the highest live enemy slot")
	assert_lte(float(sim.enemy_slot_bound()), float(sim.e_alive.size()),
		"and never past the pool")

func test_the_projectile_bound_covers_every_live_slot() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim, true, 6, 12)
	var highest := -1
	for i in sim.p_alive.size():
		if sim.p_alive[i] == 1:
			highest = i
	if highest < 0:
		return  # nothing in flight on this exact tick; the enemy case covers the rule
	assert_gt(float(sim.projectile_slot_bound()), float(highest),
		"the bound is past the highest live projectile slot")

func test_the_bound_holds_through_a_whole_act() -> void:
	# Every tick, not one sampled moment: the bound is maintained incrementally,
	# so the interesting failures are transient.
	var replay := SimFixture.fresh()
	# Driven by the greedy policy's own commands is unnecessary here - what is
	# under test is pool bookkeeping, and the wave director alone exercises spawn,
	# leak, split and despawn on every tick of a real act.
	for _t in 2000:
		replay.step()
		var bound := replay.enemy_slot_bound()
		for i in range(bound, replay.e_alive.size()):
			if replay.e_alive[i] == 1:
				assert_true(false, "a live enemy sits above the bound at tick %d" % replay.tick())
				return
		var pbound := replay.projectile_slot_bound()
		for i in range(pbound, replay.p_alive.size()):
			if replay.p_alive[i] == 1:
				assert_true(false, "a live projectile sits above the bound at tick %d" % replay.tick())
				return
	assert_true(true, "the bound held every tick of the act")

func test_an_emptied_pool_resets_the_bound() -> void:
	var sim := SimFixture.fresh()
	sim._begin_wave(0)
	sim._spawn_at(sim.enemy_index("walker"), 40.0)
	assert_gt(float(sim.enemy_slot_bound()), 0.0, "a spawn raises the bound")
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 1:
			sim._despawn_enemy(i)
	assert_eq(sim.enemy_slot_bound(), 0, "an empty pool costs nothing to sweep")
