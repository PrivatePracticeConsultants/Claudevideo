extends TestCase

## Structural guard on the rendering constraint.
##
## `tools/render_stress.gd` measures the real thing - draw calls stay flat from
## 25 to 250 entities - but it needs a GPU context, so it cannot live in the
## headless suite. These tests check the structure that produces that result, so
## a regression to one-node-per-entity fails here first and cheaply.
##
## The renderer is 3D; the simulation is not, and none of these tests reach into
## it. Sim (x, y) maps to world (x, 0, y) and the third dimension is presentation
## only.

var _tree: SceneTree
var _sim: Sim
var _renderer: SimRenderer3D

func before_each() -> void:
	_tree = Engine.get_main_loop() as SceneTree
	_sim = SimFixture.fresh()
	_renderer = SimRenderer3D.new()
	_tree.root.add_child(_renderer)
	_renderer.setup(_sim, _theme())

func after_each() -> void:
	_tree.root.remove_child(_renderer)
	_renderer.queue_free()

func test_entities_render_through_instanced_layers_only() -> void:
	# One body layer per enemy class (so class is readable from silhouette, not
	# only colour), plus health bars and projectiles.
	assert_eq(_renderer.entity_layers().size(), _sim.enemy_type_count() + 2,
		"a body layer per class, plus health bars and projectiles")
	# The buildable-cell grid is a MultiMesh too, but a static one: its size is
	# fixed by the map, not by how much is happening.
	assert_ne(_renderer.cell_layer(), null, "the cell grid is also instanced, not per-cell nodes")
	for child in _renderer.get_children():
		if child is MultiMeshInstance3D:
			continue
		assert_false(child is MeshInstance3D,
			"loose per-entity meshes must not hang off the renderer root")

func test_instance_buffers_are_preallocated_to_the_pool_ceiling() -> void:
	# Allocated once at setup. If instance_count were resized as entities spawn,
	# the renderer would be reallocating GPU buffers mid-wave.
	for i in _renderer.enemy_layer_count():
		assert_eq(_renderer.enemy_layer(i).multimesh.instance_count, _sim.e_alive.size(),
			"enemy layer %d" % i)
	assert_eq(_renderer.hp_bar_layer().multimesh.instance_count, _sim.e_alive.size(), "health bar layer")
	assert_eq(_renderer.projectile_layer().multimesh.instance_count, _sim.p_alive.size(), "projectile layer")
	assert_eq(_renderer.cell_layer().multimesh.instance_count,
		_sim.grid_cols() * _sim.grid_rows(), "cell grid layer")

func test_the_simulation_maps_onto_the_ground_plane() -> void:
	# The whole reason the 2D renderer could be swapped for a 3D one without
	# touching core/: the sim is flat, and height is presentation only.
	var world := SimRenderer3D.to_world(120.0, 340.0, 9.0)
	assert_almost_eq(world.x, 120.0, 0.0001, "sim x is world x")
	assert_almost_eq(world.z, 340.0, 0.0001, "sim y is world z")
	assert_almost_eq(world.y, 9.0, 0.0001, "world y is height, which the sim has no concept of")

func test_visible_instance_count_tracks_live_entities() -> void:
	# This is the mechanism that keeps draw cost proportional to what is on
	# screen without touching allocation.
	_renderer.update_visuals(0.0)
	assert_eq(_renderer.drawn_enemy_count(), 0, "nothing spawned yet")

	_sim._begin_wave(0)
	for _i in 40:
		_sim._spawn(_sim.enemy_index("walker"))
	_renderer.update_visuals(0.0)
	assert_eq(_renderer.drawn_enemy_count(), 40, "40 bodies drawn")
	assert_eq(_renderer.hp_bar_layer().multimesh.visible_instance_count, 40, "40 health bars drawn")

	for i in 15:
		_sim._despawn_enemy(i)
	_renderer.update_visuals(0.0)
	assert_eq(_renderer.drawn_enemy_count(), 25, "count follows despawns")

func test_the_node_count_does_not_grow_with_entities() -> void:
	# The actual regression this file exists to catch.
	var before := _tree.root.get_child_count() + _renderer.get_child_count()
	_sim._begin_wave(0)
	for _i in 200:
		_sim._spawn(_sim.enemy_index("walker"))
	_renderer.update_visuals(0.5)
	var after := _tree.root.get_child_count() + _renderer.get_child_count()
	assert_eq(after, before, "200 enemies must not create 200 nodes")

func test_interpolation_lands_between_the_two_ticks() -> void:
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("walker"))
	var slot := 0
	for i in _sim.e_alive.size():
		if _sim.e_alive[i] == 1:
			slot = i
			break
	_sim.e_prev_prog[slot] = 100.0
	_sim.e_prog[slot] = 200.0

	_sim.sample_for_render(100.0, _sim.e_offset[slot])
	var start_x := _sim.out_x()
	_sim.sample_for_render(200.0, _sim.e_offset[slot])
	var end_x := _sim.out_x()
	_sim.sample_for_render(150.0, _sim.e_offset[slot])
	var mid_x := _sim.out_x()

	assert_almost_eq(mid_x, (start_x + end_x) * 0.5, 0.0001,
		"half a tick of interpolation is half the distance along the path")

func _theme() -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://data/theme.json"))
	return parsed if typeof(parsed) == TYPE_DICTIONARY else {}

# --- camera control ----------------------------------------------------------
#
# Boards run to 16,000 units, and a whole act framed at once makes a single
# turret a few pixels wide. Zoom and pan are presentation only - nothing here
# touches the simulation - but they sit on top of the auto-fit, and the auto-fit
# reruns on every window resize and every time a chain extends the corridor. The
# thing worth guarding is that the view the player chose survives all of that.

func test_the_view_starts_fitted() -> void:
	assert_almost_eq(_renderer.zoom(), 1.0, 0.0001,
		"a level opens framed on its whole revealed corridor")

func test_zooming_in_moves_the_camera_closer() -> void:
	var before := _renderer.camera.position
	_renderer.zoom_by(3, Vector3.ZERO)
	assert_lt(_renderer.zoom(), 1.0, "zoom went in")
	assert_lt(_renderer.camera.position.distance_to(Vector3.ZERO),
		before.distance_to(Vector3.ZERO), "and the camera actually moved in")

func test_zoom_is_bounded_at_both_ends() -> void:
	# Unbounded zoom is how you end up inside the ground plane, or looking at a
	# board the size of a pixel with no way to find it again.
	for _i in 60:
		_renderer.zoom_by(1, Vector3.ZERO)
	assert_gte(_renderer.zoom(), SimRenderer3D.ZOOM_MIN, "cannot zoom past the near limit")
	for _i in 120:
		_renderer.zoom_by(-1, Vector3.ZERO)
	assert_lte(_renderer.zoom(), SimRenderer3D.ZOOM_MAX,
		"and cannot zoom out past the fitted framing")

func test_panning_is_clamped_so_the_board_cannot_be_lost() -> void:
	_renderer.zoom_by(4, Vector3.ZERO)
	_renderer.pan_by(Vector3(900000.0, 0.0, 900000.0))
	var far_corner := _renderer.camera.position
	_renderer.pan_by(Vector3(900000.0, 0.0, 900000.0))
	assert_almost_eq(_renderer.camera.position.x, far_corner.x, 0.001,
		"panning past the clamp does nothing more")
	assert_almost_eq(_renderer.camera.position.z, far_corner.z, 0.001,
		"in either axis")

func test_there_is_no_panning_at_full_zoom_out() -> void:
	# Nothing is off screen when the whole board is framed, so drifting the eye
	# around could only lose it.
	var fitted := _renderer.camera.position
	_renderer.pan_by(Vector3(5000.0, 0.0, 5000.0))
	assert_almost_eq(_renderer.camera.position.x, fitted.x, 0.001, "the view holds")
	assert_almost_eq(_renderer.camera.position.z, fitted.z, 0.001, "in both axes")

func test_the_view_survives_a_refit() -> void:
	# _fit_camera reruns on every viewport change; if it reset zoom, every window
	# resize would throw away where the player was looking.
	_renderer.zoom_by(3, Vector3.ZERO)
	var chosen := _renderer.zoom()
	_renderer._fit_camera()
	assert_almost_eq(_renderer.zoom(), chosen, 0.0001, "zoom is preserved across a refit")

func test_resetting_returns_to_the_fitted_view() -> void:
	var fitted := _renderer.camera.position
	_renderer.zoom_by(4, Vector3(1000.0, 0.0, 1000.0))
	_renderer.reset_view()
	assert_almost_eq(_renderer.zoom(), 1.0, 0.0001, "back to fitted")
	assert_almost_eq(_renderer.camera.position.x, fitted.x, 0.001, "and back to where it was")
	assert_almost_eq(_renderer.camera.position.z, fitted.z, 0.001, "in both axes")
