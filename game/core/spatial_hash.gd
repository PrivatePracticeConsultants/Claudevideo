class_name SpatialHash
extends RefCounted

## Uniform grid broadphase for targeting queries.
##
## Exists because the obvious implementation - Area2D plus
## get_overlapping_bodies() - allocates an Array per query per platform per tick
## and routes through the physics server. At the stated budget (250 enemies, 64
## platforms, 30 ticks/second) that is thousands of allocations a second in the
## hot path, which is the single easiest way to miss the 60fps target.
##
## Implemented as a counting sort into three arrays that are allocated once at
## construction and only ever refilled. rebuild() and query cost no allocations.

var _cell_size: float = 1.0
var _inv_cell: float = 1.0
var _cols: int = 1
var _rows: int = 1
var _origin_x: float = 0.0
var _origin_y: float = 0.0

# cell_start[c] .. cell_start[c + 1] indexes into _items for cell c.
var _cell_start: PackedInt32Array = PackedInt32Array()
var _cursor: PackedInt32Array = PackedInt32Array()
var _items: PackedInt32Array = PackedInt32Array()
var _item_count: int = 0

func _init(origin_x: float, origin_y: float, width: float, height: float, cell_size: float, capacity: int) -> void:
	_cell_size = cell_size
	_inv_cell = 1.0 / cell_size
	_origin_x = origin_x
	_origin_y = origin_y
	_cols = maxi(1, int(ceil(width * _inv_cell)))
	_rows = maxi(1, int(ceil(height * _inv_cell)))
	_cell_start.resize(_cols * _rows + 1)
	_cursor.resize(_cols * _rows + 1)
	_items.resize(capacity)

func cols() -> int:
	return _cols

func rows() -> int:
	return _rows

## Entities outside the grid clamp into the edge cell rather than being dropped.
## The map path deliberately starts and ends off-screen, so "outside the grid"
## is a normal state, not an error - and an enemy that exists but cannot be
## found by any turret would be a silent gameplay bug.
func cell_x(x: float) -> int:
	return clampi(int(floor((x - _origin_x) * _inv_cell)), 0, _cols - 1)

func cell_y(y: float) -> int:
	return clampi(int(floor((y - _origin_y) * _inv_cell)), 0, _rows - 1)

## Refill from struct-of-arrays entity storage. `alive` is a byte per slot.
## The box every item in the hash is inside. Degenerate (min > max) when empty,
## so every reach test against it correctly fails.
var _min_x: float = INF
var _max_x: float = -INF
var _min_y: float = INF
var _max_y: float = -INF

func min_x() -> float: return _min_x
func max_x() -> float: return _max_x
func min_y() -> float: return _min_y
func max_y() -> float: return _max_y

func rebuild(alive: PackedByteArray, xs: PackedFloat64Array, ys: PackedFloat64Array, slot_count: int) -> void:
	var cells := _cols * _rows
	_cell_start.fill(0)
	_item_count = 0
	# The bounding box of everything in the hash, measured in the pass that is
	# already reading every position. It belongs HERE rather than beside the call:
	# turret targeting uses it as a cheap "is anything even near me" reject, and a
	# box that could go stale independently of the hash is a silent wrong answer.
	# Owned by the hash, it is exactly as fresh as the hash is, always.
	_min_x = INF
	_max_x = -INF
	_min_y = INF
	_max_y = -INF

	# Pass 1: count per cell (stored shifted by one, so the prefix sum below
	# lands directly on the start offsets).
	for i in slot_count:
		if alive[i] == 1:
			_min_x = minf(_min_x, xs[i])
			_max_x = maxf(_max_x, xs[i])
			_min_y = minf(_min_y, ys[i])
			_max_y = maxf(_max_y, ys[i])
		if alive[i] == 0:
			continue
		var c := cell_y(ys[i]) * _cols + cell_x(xs[i])
		_cell_start[c + 1] += 1
		_item_count += 1

	# Pass 2: prefix sum.
	for c in cells:
		_cell_start[c + 1] += _cell_start[c]
	for c in cells + 1:
		_cursor[c] = _cell_start[c]

	# Pass 3: scatter.
	for i in slot_count:
		if alive[i] == 0:
			continue
		var c := cell_y(ys[i]) * _cols + cell_x(xs[i])
		_items[_cursor[c]] = i
		_cursor[c] += 1

func item_count() -> int:
	return _item_count

func bucket_begin(cx: int, cy: int) -> int:
	return _cell_start[cy * _cols + cx]

func bucket_end(cx: int, cy: int) -> int:
	return _cell_start[cy * _cols + cx + 1]

func item_at(index: int) -> int:
	return _items[index]
