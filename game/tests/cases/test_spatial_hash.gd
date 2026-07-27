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
