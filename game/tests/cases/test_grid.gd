extends TestCase

## Buildable ground is a grid of cells: some owned from the start, the rest
## bought with Capital and only next to ground you already hold.

func test_ground_beside_the_road_starts_owned() -> void:
	var sim := SimFixture.fresh()
	var owned := 0
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			if sim.cell_is_unlocked(cx, cy):
				owned += 1
	assert_gt(float(owned), 0.0, "a new level is immediately playable without buying ground")

func test_the_road_itself_is_never_buildable() -> void:
	var sim := SimFixture.fresh()
	# Walk the corridor centre line; no cell containing it may be buildable.
	var step := sim.path_length() / 60.0
	var prog := 0.0
	while prog <= sim.path_length():
		sim.sample_for_render(prog, 0.0)
		var cx := sim.cell_x_of(sim.out_x())
		var cy := sim.cell_y_of(sim.out_y())
		if sim.cell_in_bounds(cx, cy):
			assert_false(sim.cell_is_buildable(cx, cy),
				"cell (%d,%d) sits on the road but is buildable" % [cx, cy])
		prog += step

func test_distant_ground_is_offered_but_not_owned() -> void:
	var sim := SimFixture.fresh()
	var offerable := 0
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			if sim.cell_is_offerable(cx, cy):
				offerable += 1
				assert_false(sim.cell_is_unlocked(cx, cy), "an offered cell is not already owned")
	assert_gt(float(offerable), 0.0, "there is a frontier to expand into")

func test_only_ground_next_to_owned_ground_can_be_bought() -> void:
	var sim := SimFixture.fresh()
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			if not sim.cell_is_offerable(cx, cy):
				continue
			var touches := sim.cell_is_unlocked(cx - 1, cy) or sim.cell_is_unlocked(cx + 1, cy) \
				or sim.cell_is_unlocked(cx, cy - 1) or sim.cell_is_unlocked(cx, cy + 1)
			assert_true(touches, "cell (%d,%d) is offered without touching owned ground" % [cx, cy])

func test_buying_a_cell_costs_capital_and_unlocks_it() -> void:
	var sim := SimFixture.fresh()
	var target := _first_offerable(sim)
	assert_gte(float(target.x), 0.0, "fixture sanity: something is purchasable")
	var before := sim.capital()
	var price := sim.next_cell_cost()
	sim.queue_buy_cell(0, int(target.x), int(target.y))
	sim.step()
	assert_true(sim.cell_is_unlocked(int(target.x), int(target.y)), "the cell is now owned")
	assert_eq(sim.capital(), before - price, "and cost exactly the quoted price")
	assert_eq(sim.cells_bought(), 1, "the purchase was counted")

func test_each_purchase_is_dearer_than_the_last() -> void:
	# Otherwise expanding is reflexive rather than a trade against turrets.
	var sim := SimFixture.fresh()
	var first := sim.next_cell_cost()
	var target := _first_offerable(sim)
	sim.queue_buy_cell(0, int(target.x), int(target.y))
	sim.step()
	assert_gt(float(sim.next_cell_cost()), float(first), "the price climbs with each cell")

func test_buying_extends_the_frontier() -> void:
	# The point of adjacency: what you buy becomes the springboard for the next
	# purchase, so expansion crawls outward from the road.
	var sim := SimFixture.fresh()
	var target := _first_offerable(sim)
	var before := _offerable_count(sim)
	sim.queue_buy_cell(0, int(target.x), int(target.y))
	sim.step()
	assert_gte(float(_offerable_count(sim)), float(before - 1),
		"buying a cell opens up its neighbours rather than shrinking the frontier")

func test_an_unaffordable_or_unreachable_cell_is_refused() -> void:
	var sim := SimFixture.fresh()
	# Far corner of the map: legal ground, but nowhere near anything owned.
	var far_x := sim.grid_cols() - 1
	var far_y := sim.grid_rows() - 1
	assert_false(sim.can_buy_cell(far_x, far_y), "you cannot claim an unconnected corner")
	assert_false(sim.can_buy_cell(-1, 0), "or a cell off the board")
	sim.queue_buy_cell(0, far_x, far_y)
	sim.step()
	assert_eq(sim.cells_bought(), 0, "and nothing was bought")
	assert_eq(sim.rejected_commands(), 1, "the refusal was counted")

func test_building_on_unowned_ground_is_refused_but_not_forbidden() -> void:
	var sim := SimFixture.fresh()
	var target := _first_offerable(sim)
	var x := sim.cell_centre_x(int(target.x))
	var y := sim.cell_centre_y(int(target.y))
	assert_eq(sim.can_build_at(x, y, 0), Sim.BUILD_LOCKED, "unowned ground reports as locked")
	sim.queue_buy_cell(0, int(target.x), int(target.y))
	sim.step()
	assert_eq(sim.can_build_at(x, y, 0), Sim.BUILD_OK, "and becomes buildable once bought")

func test_buying_ground_is_part_of_the_state_hash() -> void:
	var a := SimFixture.fresh(11)
	var b := SimFixture.fresh(11)
	var target := _first_offerable(a)
	a.queue_buy_cell(0, int(target.x), int(target.y))
	a.step()
	b.step()
	assert_ne(b.state_hash(), a.state_hash(), "owned ground is simulation state, not decoration")

func _first_offerable(sim: Sim) -> Vector2i:
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			if sim.can_buy_cell(cx, cy):
				return Vector2i(cx, cy)
	return Vector2i(-1, -1)

func _offerable_count(sim: Sim) -> int:
	var count := 0
	for cy in sim.grid_rows():
		for cx in sim.grid_cols():
			if sim.cell_is_offerable(cx, cy):
				count += 1
	return count

func test_a_run_that_buys_ground_replays_identically() -> void:
	# The scripted policy never needs to buy ground, so without this the whole
	# CMD_BUY_CELL path would be absent from the determinism guarantee - the one
	# place a new command type can silently break replays.
	var recorded := SimFixture.fresh(2468)
	var bought := _buy_and_build(recorded)
	assert_gt(float(bought.size()), 0.0, "fixture sanity: ground really was bought")

	var replayed := SimFixture.fresh(2468)
	for i in range(0, bought.size(), 3):
		if bought[i + 2] == 0:
			replayed.queue_buy_cell(bought[i], bought[i + 1] >> 16, bought[i + 1] & 0xFFFF)
		else:
			replayed.queue_place(bought[i], bought[i + 1] >> 16, bought[i + 1] & 0xFFFF, 0)
	for _t in 2000:
		replayed.step()
	assert_eq(replayed.cells_bought(), recorded.cells_bought(), "same ground bought")
	assert_eq(replayed.t_count, recorded.t_count, "same platforms built")
	assert_eq(replayed.state_hash(), recorded.state_hash(), "same end state")

## Buy several cells, then build on them. Returns a flat log of
## [tick, packed_xy, kind] where kind 0 is a purchase and 1 a placement.
func _buy_and_build(sim: Sim) -> PackedInt32Array:
	var log := PackedInt32Array()
	var bought_cells: Array[Vector2i] = []
	for _n in 5:
		var target := _first_offerable(sim)
		if target.x < 0:
			break
		log.append(sim.tick())
		log.append((int(target.x) << 16) | int(target.y))
		log.append(0)
		sim.queue_buy_cell(sim.tick(), int(target.x), int(target.y))
		bought_cells.append(target)
		for _t in 5:
			sim.step()
	for cell in bought_cells:
		var x := roundi(sim.cell_centre_x(cell.x))
		var y := roundi(sim.cell_centre_y(cell.y))
		if sim.can_build_at(float(x), float(y), 0) != Sim.BUILD_OK:
			continue
		log.append(sim.tick())
		log.append((x << 16) | y)
		log.append(1)
		sim.queue_place(sim.tick(), x, y, 0)
		for _t in 5:
			sim.step()
	while sim.tick() < 2000:
		sim.step()
	return log
