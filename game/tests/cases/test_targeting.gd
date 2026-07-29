extends TestCase

## Per-turret targeting priority.
##
## Before this, every turret shot whatever was furthest along the road. That is
## the right default and it is still the default - nothing in the measured
## campaign changes unless the player asks it to - but it made a line of turrets
## one decision repeated N times. A priority is the cheapest real choice a tower
## defence has: the same turret in the same spot does a different job.
##
## It is simulation state and not a display preference, so it goes through the
## command log like everything else and is proved to replay.

## Put a turret somewhere legal and hand it a full magazine.
func _armed(sim: Sim) -> int:
	var spot := SimFixture.a_site(sim)
	sim.queue_place(0, spot[0], spot[1], 0)
	sim.step()
	assert_eq(sim.t_count, 1, "fixture sanity: the turret went down")
	return 0

## Drop drones on the road inside the turret's reach, at chosen distances along
## it. Returns their pool slots in the order asked for.
func _line_up(sim: Sim, turret: int, type_id: String, progs: Array) -> PackedInt32Array:
	var slots := PackedInt32Array()
	var type_index := sim.enemy_index(type_id)
	sim._begin_wave(0)
	for prog: float in progs:
		sim._spawn_at(type_index, prog)
		var slot := sim.e_live_count - 1
		# Spawn jitter is the sim's one use of randomness. A unit test about which
		# drone gets picked should not also be a test of where the dice landed.
		sim.e_offset[slot] = 0.0
		sim.sample_for_render(prog, 0.0)
		sim.e_x[slot] = sim.out_x()
		sim.e_y[slot] = sim.out_y()
		slots.append(slot)
	# The turret has to be able to see them, or the test is measuring nothing.
	sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.e_alive.size())
	var range_sq := sim.platform_range(turret) * sim.platform_range(turret)
	var reachable := 0
	for s in slots:
		var dx := sim.e_x[s] - sim.t_x[turret]
		var dy := sim.e_y[s] - sim.t_y[turret]
		if dx * dx + dy * dy <= range_sq:
			reachable += 1
	assert_eq(reachable, slots.size(), "fixture sanity: every drone is in range")
	return slots

## Progress values on either side of the turret, all inside its range.
func _progs_near(sim: Sim, turret: int) -> Array:
	# The turret was placed beside the road, so the point on the road nearest it
	# is roughly its own distance along. Walk out from there in small steps.
	var reach := sim.platform_range(turret)
	var centre := 0.0
	var best := INF
	var prog := 0.0
	while prog <= sim.path_length():
		sim.sample_for_render(prog, 0.0)
		var dx := sim.out_x() - sim.t_x[turret]
		var dy := sim.out_y() - sim.t_y[turret]
		var d := dx * dx + dy * dy
		if d < best:
			best = d
			centre = prog
		prog += 20.0
	var step := reach * 0.15
	return [maxf(centre - step, 0.0), centre, centre + step]

func test_the_default_is_what_every_turret_did_before() -> void:
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	assert_eq(sim.platform_priority(turret), Sim.TARGET_FIRST,
		"a new turret shoots whatever is furthest along, as it always did")

func test_first_takes_the_one_furthest_along() -> void:
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	var progs := _progs_near(sim, turret)
	var slots := _line_up(sim, turret, "walker", progs)
	var picked := sim._acquire_target(sim.t_x[turret], sim.t_y[turret],
		sim.platform_range(turret) * sim.platform_range(turret), Sim.TARGET_FIRST)
	assert_eq(picked, slots[2], "the closest to the exit")

func test_last_takes_the_one_furthest_back() -> void:
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	var progs := _progs_near(sim, turret)
	var slots := _line_up(sim, turret, "walker", progs)
	var picked := sim._acquire_target(sim.t_x[turret], sim.t_y[turret],
		sim.platform_range(turret) * sim.platform_range(turret), Sim.TARGET_LAST)
	assert_eq(picked, slots[0], "the one with the most road left to cover")

func test_nearest_takes_the_closest_to_the_gun() -> void:
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	var progs := _progs_near(sim, turret)
	var slots := _line_up(sim, turret, "walker", progs)
	var range_sq := sim.platform_range(turret) * sim.platform_range(turret)
	var picked := sim._acquire_target(sim.t_x[turret], sim.t_y[turret], range_sq,
		Sim.TARGET_NEAREST)
	var nearest := -1
	var best := INF
	for s in slots:
		var dx := sim.e_x[s] - sim.t_x[turret]
		var dy := sim.e_y[s] - sim.t_y[turret]
		if dx * dx + dy * dy < best:
			best = dx * dx + dy * dy
			nearest = s
	assert_eq(picked, nearest, "distance decides, not progress")

func test_toughest_and_weakest_split_by_health() -> void:
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	var progs := _progs_near(sim, turret)
	var slots := _line_up(sim, turret, "walker", progs)
	# Same class, different remaining health, so only health can be deciding.
	sim.e_hp[slots[0]] = 900
	sim.e_hp[slots[1]] = 50
	sim.e_hp[slots[2]] = 400
	var range_sq := sim.platform_range(turret) * sim.platform_range(turret)
	assert_eq(sim._acquire_target(sim.t_x[turret], sim.t_y[turret], range_sq,
		Sim.TARGET_TOUGHEST), slots[0], "toughest picks the 900")
	assert_eq(sim._acquire_target(sim.t_x[turret], sim.t_y[turret], range_sq,
		Sim.TARGET_WEAKEST), slots[1], "weakest picks the 50")

func test_nothing_in_range_is_still_nothing() -> void:
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	for mode in Sim.target_mode_count():
		assert_eq(sim._acquire_target(sim.t_x[turret], sim.t_y[turret], 100.0, mode), -1,
			"%s finds nothing on an empty board" % sim.priority_name(mode))

func test_retasking_goes_through_the_command_log() -> void:
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	var before := sim.command_count()
	sim.queue_priority(sim.tick(), turret, Sim.TARGET_NEAREST)
	assert_eq(sim.command_count(), before + 1, "it is a logged command")
	sim.step()
	assert_eq(sim.platform_priority(turret), Sim.TARGET_NEAREST, "and it applied")

func test_an_impossible_order_is_refused_not_clamped() -> void:
	# A command the sim quietly reinterprets means something different on replay.
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	var rejected := sim.rejected_commands()
	sim.queue_priority(sim.tick(), turret, 99)
	sim.queue_priority(sim.tick(), turret, -1)
	sim.queue_priority(sim.tick(), 4000, Sim.TARGET_LAST)
	sim.step()
	assert_eq(sim.rejected_commands(), rejected + 3, "all three refused")
	assert_eq(sim.platform_priority(turret), Sim.TARGET_FIRST, "and nothing changed")

func test_re_issuing_the_same_order_is_not_a_change() -> void:
	var sim := SimFixture.fresh()
	var turret := _armed(sim)
	var rejected := sim.rejected_commands()
	sim.queue_priority(sim.tick(), turret, Sim.TARGET_FIRST)
	sim.step()
	assert_eq(sim.rejected_commands(), rejected + 1,
		"an order that changes nothing is refused, so the HUD cannot spam the log")

func test_priority_is_part_of_the_state_hash() -> void:
	var a := SimFixture.fresh()
	var b := SimFixture.fresh()
	var ta := _armed(a)
	var tb := _armed(b)
	assert_eq(a.state_hash(), b.state_hash(), "fixture sanity: identical so far")
	a.queue_priority(a.tick(), ta, Sim.TARGET_TOUGHEST)
	a.step()
	b.queue_priority(b.tick(), tb, Sim.TARGET_WEAKEST)
	b.step()
	assert_ne(a.state_hash(), b.state_hash(),
		"two boards aiming at different things are not in the same state")

func test_selling_carries_the_order_with_the_compaction() -> void:
	# Selling moves the last turret into the freed slot. If the priority array is
	# not moved with it, the surviving turret silently inherits the dead one's
	# orders - which would look like the game changing its mind.
	var sim := SimFixture.fresh()
	var first := SimFixture.a_site(sim, 0)
	var second := SimFixture.a_site(sim, 6)
	sim.queue_place(0, first[0], first[1], 0)
	sim.step()
	sim.queue_place(sim.tick(), second[0], second[1], 0)
	sim.step()
	assert_eq(sim.t_count, 2, "fixture sanity: two turrets")
	sim.queue_priority(sim.tick(), 1, Sim.TARGET_NEAREST)
	sim.step()
	sim.queue_sell(sim.tick(), 0)
	sim.step()
	assert_eq(sim.t_count, 1, "one left")
	assert_eq(sim.platform_priority(0), Sim.TARGET_NEAREST,
		"and it is the survivor's own order, in its new slot")

func test_orders_carry_to_the_next_act() -> void:
	# Everything about a turret carries between acts - tier and standing order
	# alike. Making the player re-issue thirty orders would be tedium.
	var levels := Database.load_levels()
	var pair := []
	for i in levels.size() - 1:
		if str(levels[i]["map"]) == str(levels[i + 1]["map"]):
			pair = [levels[i], levels[i + 1]]
			break
	assert_eq(pair.size(), 2, "fixture sanity: the campaign has a chain in it")
	var first := SimFixture.start_act(pair[0], {})
	var spot := SimFixture.a_site(first)
	first.queue_place(0, spot[0], spot[1], 0)
	first.step()
	first.queue_priority(first.tick(), 0, Sim.TARGET_TOUGHEST)
	first.step()
	var second := SimFixture.start_act(pair[1], first.board_snapshot())
	assert_gt(float(second.t_count), 0.0, "fixture sanity: the board carried")
	assert_eq(second.platform_priority(0), Sim.TARGET_TOUGHEST,
		"the order came with the emplacement")

func test_a_run_that_retasks_replays_identically() -> void:
	# The whole point of putting this in the command log.
	var sim := SimFixture.fresh()
	var log := SimFixture.run_greedy(sim, true, 3, 1)
	var tick_list: PackedInt32Array = log["tick"]
	var kinds: PackedInt32Array = log["kind"]
	var a_list: PackedInt32Array = log["a"]
	var b_list: PackedInt32Array = log["b"]
	var c_list: PackedInt32Array = log["c"]
	# Retask a few of the turrets that exist by then, at ticks the log reaches.
	for i in mini(4, sim.t_count):
		tick_list.append(sim.tick() + i)
		kinds.append(Sim.CMD_SET_PRIORITY)
		a_list.append(i)
		b_list.append((i + 1) % Sim.target_mode_count())
		c_list.append(0)
	var live := SimFixture.fresh()
	SimFixture.replay(live, log)
	var again := SimFixture.fresh()
	SimFixture.replay(again, log)
	assert_eq(live.state_hash(), again.state_hash(),
		"same log, same seed, same end state")
	assert_gt(float(live.tick()), 0.0, "fixture sanity: the replay actually ran")
