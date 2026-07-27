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

func test_entities_render_through_exactly_three_multimesh_layers() -> void:
	assert_eq(_renderer.entity_layers().size(), 3, "bodies, health bars and projectiles")
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
	assert_eq(_renderer.enemy_layer().multimesh.instance_count, _sim.e_alive.size(), "enemy layer")
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
	assert_eq(_renderer.enemy_layer().multimesh.visible_instance_count, 0, "nothing spawned yet")

	_sim._begin_wave(0)
	for _i in 40:
		_sim._spawn(_sim.enemy_index("walker"))
	_renderer.update_visuals(0.0)
	assert_eq(_renderer.enemy_layer().multimesh.visible_instance_count, 40, "40 bodies drawn")
	assert_eq(_renderer.hp_bar_layer().multimesh.visible_instance_count, 40, "40 health bars drawn")

	for i in 15:
		_sim._despawn_enemy(i)
	_renderer.update_visuals(0.0)
	assert_eq(_renderer.enemy_layer().multimesh.visible_instance_count, 25, "count follows despawns")

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
