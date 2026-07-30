extends TestCase

## The broadphase must never lose an entity. A turret that cannot see something
## standing in front of it is one of the worst bug classes in a TD, because it
## reads as "the game is cheating".

const CELL := 50.0
const CAPACITY := 64

func _grid() -> SpatialHash:
	return SpatialHash.new(0.0, 0.0, 200.0, 200.0, CELL, CAPACITY)

## Populated by _fill(). Held as fields rather than returned in an Array because
## packed arrays are copy-on-write: mutating one through an Array element edits a
## temporary, and the edit is silently lost.
var alive := PackedByteArray()
var px := PackedFloat64Array()
var py := PackedFloat64Array()

func before_each() -> void:
	alive = PackedByteArray()
	px = PackedFloat64Array()
	py = PackedFloat64Array()
	alive.resize(CAPACITY)
	px.resize(CAPACITY)
	py.resize(CAPACITY)

func _fill(count: int, xs: Array, ys: Array) -> void:
	for i in count:
		alive[i] = 1
		px[i] = xs[i]
		py[i] = ys[i]

func test_every_live_entity_is_indexed_exactly_once() -> void:
	var grid := _grid()
	_fill(5, [10.0, 60.0, 110.0, 160.0, 10.0], [10.0, 60.0, 110.0, 160.0, 190.0])
	grid.rebuild(alive, px, py, CAPACITY)
	assert_eq(grid.item_count(), 5, "all five are indexed")
	var seen := {}
	for cy in grid.rows():
		for cx in grid.cols():
			for k in range(grid.bucket_begin(cx, cy), grid.bucket_end(cx, cy)):
				var e := grid.item_at(k)
				assert_false(seen.has(e), "entity %d appeared in two cells" % e)
				seen[e] = true
	assert_eq(seen.size(), 5, "every entity was found exactly once")

func test_dead_slots_are_skipped() -> void:
	var grid := _grid()
	_fill(4, [10.0, 60.0, 110.0, 160.0], [10.0, 60.0, 110.0, 160.0])
	alive[2] = 0
	grid.rebuild(alive, px, py, CAPACITY)
	assert_eq(grid.item_count(), 3, "the dead slot is not indexed")

func test_entities_outside_the_grid_clamp_instead_of_vanishing() -> void:
	# The map path deliberately begins and ends off-screen, so this is the normal
	# case, not an edge case.
	var grid := _grid()
	_fill(2, [-500.0, 9000.0], [-500.0, 9000.0])
	grid.rebuild(alive, px, py, CAPACITY)
	assert_eq(grid.item_count(), 2, "off-grid entities are still findable")
	assert_eq(grid.cell_x(-500.0), 0, "clamps to the low edge")
	assert_eq(grid.cell_x(9000.0), grid.cols() - 1, "clamps to the high edge")

func test_rebuild_is_repeatable() -> void:
	var grid := _grid()
	_fill(6, [10.0, 60.0, 110.0, 160.0, 30.0, 80.0], [10.0, 60.0, 110.0, 160.0, 120.0, 40.0])
	grid.rebuild(alive, px, py, CAPACITY)
	var first := grid.item_count()
	for _i in 5:
		grid.rebuild(alive, px, py, CAPACITY)
	assert_eq(grid.item_count(), first, "repeated rebuilds must not accumulate")

func test_an_empty_grid_reports_nothing() -> void:
	var grid := _grid()
	_fill(0, [], [])
	grid.rebuild(alive, px, py, CAPACITY)
	assert_eq(grid.item_count(), 0, "no entities, no items")
	assert_eq(grid.bucket_begin(0, 0), grid.bucket_end(0, 0), "every bucket is empty")

func test_the_rebuild_reports_the_box_everything_is_inside() -> void:
	# Turret targeting uses this box as a cheap "is anything even near me" reject,
	# which took the per-tick platform update from 4.3ms to 0.4ms on a 208-turret
	# board. A box that were ever too SMALL would make turrets stop firing at
	# drones they can reach, so it is pinned here rather than trusted.
	var hash := SpatialHash.new(0.0, 0.0, 4000.0, 3000.0, 96.0, 64)
	var alive := PackedByteArray([1, 0, 1, 1])
	var xs := PackedFloat64Array([120.0, 9999.0, 480.0, 300.0])
	var ys := PackedFloat64Array([200.0, 9999.0, 640.0, 100.0])
	hash.rebuild(alive, xs, ys, 4)
	assert_almost_eq(hash.min_x(), 120.0, 0.0001, "leftmost live x")
	assert_almost_eq(hash.max_x(), 480.0, 0.0001, "rightmost live x")
	assert_almost_eq(hash.min_y(), 100.0, 0.0001, "topmost live y")
	assert_almost_eq(hash.max_y(), 640.0, 0.0001, "bottommost live y")

func test_an_empty_rebuild_reports_a_box_nothing_can_reach() -> void:
	# Degenerate rather than zero-sized: a box at the origin would make turrets
	# near the origin scan every tick for nothing.
	var hash := SpatialHash.new(0.0, 0.0, 4000.0, 3000.0, 96.0, 64)
	hash.rebuild(PackedByteArray([0, 0]), PackedFloat64Array([1.0, 2.0]),
		PackedFloat64Array([1.0, 2.0]), 2)
	assert_gt(hash.min_x(), hash.max_x(), "min past max, so every reach test fails")
