class_name SimRenderer
extends Node2D

## Draws the simulation. Owns no game state and never writes to the Sim.
##
## Enemies, their health bars and projectiles all go through MultiMeshInstance2D
## with preallocated instance buffers. The default a code generator reaches for -
## one Node2D or Sprite2D per entity - costs a scene-tree node, a canvas item and
## a draw call each, and falls over well before the stated budget of 250 enemies
## plus 700 projectiles at 60fps on an iPhone 11. Here it is three draw calls
## regardless of entity count.
##
## This is also the layer where interpolation happens: the sim runs at a fixed
## 30Hz, the display runs at whatever the panel does, and `alpha` carries the
## fractional position between the last two ticks. Enemies interpolate along
## their path distance rather than between two screen positions, so they round
## corners instead of cutting across them.

const PATH_WIDTH := 34.0
const PATH_EDGE_WIDTH := 40.0
const PAD_RADIUS := 15.0
const PLATFORM_RADIUS := 12.0
const HP_BAR_WIDTH := 22.0
const HP_BAR_HEIGHT := 3.0
const HP_BAR_LIFT := 15.0
const PROJECTILE_LENGTH := 9.0
const PROJECTILE_WIDTH := 2.5

var _sim: Sim
var _theme: Dictionary = {}
var _enemies: MultiMeshInstance2D
var _hp_bars: MultiMeshInstance2D
var _projectiles: MultiMeshInstance2D
var _hover_pad: int = -1
var _preview_range: float = 0.0

# Reused every frame so the render path allocates nothing either. Transform2D and
# Vector2 are value types, so assigning through these does not touch the heap.
var _xf := Transform2D()
var _basis_x := Vector2.ZERO
var _basis_y := Vector2.ZERO
var _origin := Vector2.ZERO

func setup(sim: Sim, theme: Dictionary) -> void:
	_sim = sim
	_theme = theme
	_enemies = _make_layer(sim.e_alive.size(), true)
	_hp_bars = _make_layer(sim.e_alive.size(), true)
	_projectiles = _make_layer(sim.p_alive.size(), false)
	# Health bars sit above bodies, projectiles above both.
	add_child(_enemies)
	add_child(_hp_bars)
	add_child(_projectiles)
	queue_redraw()

func _make_layer(capacity: int, colored: bool) -> MultiMeshInstance2D:
	var quad := QuadMesh.new()
	quad.size = Vector2.ONE
	var mm := MultiMesh.new()
	mm.transform_format = MultiMesh.TRANSFORM_2D
	mm.use_colors = colored
	mm.mesh = quad
	# Allocated once at the pool ceiling. instance_count is never changed at
	# runtime; visible_instance_count is what varies, and it costs nothing.
	mm.instance_count = capacity
	mm.visible_instance_count = 0
	var node := MultiMeshInstance2D.new()
	node.multimesh = mm
	return node

func set_hover(pad_index: int, preview_range: float) -> void:
	if pad_index == _hover_pad and is_equal_approx(preview_range, _preview_range):
		return
	_hover_pad = pad_index
	_preview_range = preview_range
	queue_redraw()

## Rebuild the instance buffers for this frame. `alpha` is how far the display is
## between the previous tick and the current one, in [0, 1).
func update_visuals(alpha: float) -> void:
	_update_enemies(alpha)
	_update_projectiles(alpha)

func _update_enemies(alpha: float) -> void:
	var body_mm := _enemies.multimesh
	var bar_mm := _hp_bars.multimesh
	var body_color := _color("enemy")
	var hurt_color := _color("enemy_hurt")
	var bar_color := _color("hp_bar")
	var bar_back := _color("hp_bar_back")
	var visible_count := 0
	for i in _sim.e_alive.size():
		if _sim.e_alive[i] == 0:
			continue
		# Interpolate the scalar, then resolve it to a position. Interpolating
		# the position instead would cut corners.
		var prog: float = _sim.e_prev_prog[i] + (_sim.e_prog[i] - _sim.e_prev_prog[i]) * alpha
		_sim.sample_for_render(prog, _sim.e_offset[i])
		var px := _sim.out_x()
		var py := _sim.out_y()
		var size := _sim.enemy_radius(_sim.e_type[i]) * 2.0

		_basis_x.x = size
		_basis_x.y = 0.0
		_basis_y.x = 0.0
		_basis_y.y = size
		_origin.x = px
		_origin.y = py
		_xf = Transform2D(_basis_x, _basis_y, _origin)
		body_mm.set_instance_transform_2d(visible_count, _xf)

		var hp_max: float = float(_sim.e_hp_max[i])
		var fraction: float = 0.0 if hp_max <= 0.0 else clampf(float(_sim.e_hp[i]) / hp_max, 0.0, 1.0)
		body_mm.set_instance_color(visible_count, hurt_color.lerp(body_color, fraction))

		# Health bar: a single quad whose width tracks the fraction, anchored so
		# it shrinks from the right rather than from the centre.
		var filled := HP_BAR_WIDTH * fraction
		_basis_x.x = filled
		_basis_x.y = 0.0
		_basis_y.x = 0.0
		_basis_y.y = HP_BAR_HEIGHT
		_origin.x = px - (HP_BAR_WIDTH - filled) * 0.5
		_origin.y = py - HP_BAR_LIFT
		_xf = Transform2D(_basis_x, _basis_y, _origin)
		bar_mm.set_instance_transform_2d(visible_count, _xf)
		bar_mm.set_instance_color(visible_count, bar_color if fraction > 0.35 else bar_back.lerp(bar_color, 0.6))

		visible_count += 1
	body_mm.visible_instance_count = visible_count
	bar_mm.visible_instance_count = visible_count

func _update_projectiles(alpha: float) -> void:
	var mm := _projectiles.multimesh
	var visible_count := 0
	for i in _sim.p_alive.size():
		if _sim.p_alive[i] == 0:
			continue
		var px: float = _sim.p_prev_x[i] + (_sim.p_x[i] - _sim.p_prev_x[i]) * alpha
		var py: float = _sim.p_prev_y[i] + (_sim.p_y[i] - _sim.p_prev_y[i]) * alpha
		# Stretch the quad along the direction of travel to read as a tracer.
		# The basis is built straight from the direction vector - no atan2, no
		# rotation matrix construction.
		var dx: float = _sim.p_x[i] - _sim.p_prev_x[i]
		var dy: float = _sim.p_y[i] - _sim.p_prev_y[i]
		var length := sqrt(dx * dx + dy * dy)
		if length < 0.0001:
			dx = 1.0
			dy = 0.0
		else:
			dx /= length
			dy /= length
		_basis_x.x = dx * PROJECTILE_LENGTH
		_basis_x.y = dy * PROJECTILE_LENGTH
		_basis_y.x = -dy * PROJECTILE_WIDTH
		_basis_y.y = dx * PROJECTILE_WIDTH
		_origin.x = px
		_origin.y = py
		_xf = Transform2D(_basis_x, _basis_y, _origin)
		mm.set_instance_transform_2d(visible_count, _xf)
		visible_count += 1
	mm.visible_instance_count = visible_count
	_projectiles.modulate = _color("projectile")

## Static geometry: the corridor, the pads, and whatever is built on them.
## Redrawn only when something changes, not every frame.
func _draw() -> void:
	if _sim == null:
		return
	var points := PackedVector2Array()
	for i in _sim.waypoint_count():
		points.append(Vector2(_sim.waypoint_x(i), _sim.waypoint_y(i)))
	draw_polyline(points, _color("path_edge"), PATH_EDGE_WIDTH)
	draw_polyline(points, _color("path"), PATH_WIDTH)

	if _hover_pad >= 0 and _preview_range > 0.0:
		var preview := _color("range_preview")
		preview.a = 0.13
		draw_circle(Vector2(_sim.pad_x(_hover_pad), _sim.pad_y(_hover_pad)), _preview_range, preview)

	for i in _sim.pad_count():
		var centre := Vector2(_sim.pad_x(i), _sim.pad_y(i))
		var free := _sim.pad_is_free(i)
		var tint: Color = _color("pad_hover") if i == _hover_pad and free else _color("pad_free") if free else _color("pad_occupied")
		draw_circle(centre, PAD_RADIUS, tint)
		if not free:
			draw_circle(centre, PLATFORM_RADIUS, _color("platform"))

func _color(key: String) -> Color:
	return Color(str(_theme.get(key, "#ff00ff")))
