class_name SimRenderer3D
extends Node3D

## Draws the simulation in 3D. Owns no game state and never writes to the Sim.
##
## The simulation did not change by one line to support this. Its world is a flat
## plane in (x, y) with no notion of height, so it maps directly onto the ground
## plane as (x, 0, y) - the third dimension is presentation only. That is the
## payoff of keeping core/ free of engine types: the renderer is replaceable.
##
## Enemies, health bars and projectiles all go through MultiMeshInstance3D with
## preallocated instance buffers, for the same reason the 2D version did: one
## node per entity does not survive 250 enemies plus 700 projectiles on a phone.
## Static geometry - ground, corridor, pads, platforms - is a handful of mesh
## instances built once and rebuilt only when a platform is placed.
##
## Camera is orthographic at a 3/4 angle. Perspective would make identical towers
## at the near and far edge of the board look like different sizes, which makes
## coverage and range genuinely harder to read.

## Simulation (x, y) becomes world (x, 0, y). Kept as a named function rather
## than inlined so there is exactly one place this mapping is defined.
static func to_world(sim_x: float, sim_y: float, height: float) -> Vector3:
	return Vector3(sim_x, height, sim_y)

var _sim: Sim
var _theme: Dictionary = {}
var _world: Dictionary = {}

var camera: Camera3D
var _enemies: MultiMeshInstance3D
var _hp_bars: MultiMeshInstance3D
var _projectiles: MultiMeshInstance3D
var _static_root: Node3D
var _hover_ring: MeshInstance3D
var _hover_pad: int = -1

# Colours are resolved once at setup. Parsing them from strings inside the frame
# loop - which the first version did - is a String allocation per colour per
# frame in the one place the project promises not to allocate.
var _enemy_color := Color.WHITE
var _enemy_hurt_color := Color.WHITE
var _bar_color := Color.WHITE
var _bar_back_color := Color.WHITE

# Reused every frame; Transform3D and Vector3 are value types, so writing
# through these touches no heap.
var _xf := Transform3D()
var _basis := Basis()

func setup(sim: Sim, theme: Dictionary) -> void:
	_sim = sim
	_theme = theme
	_world = theme.get("world", {})
	_enemy_color = _color("enemy")
	_enemy_hurt_color = _color("enemy_hurt")
	_bar_color = _color("hp_bar")
	_bar_back_color = _color("hp_bar_back")

	_build_environment()
	_build_static_geometry()

	_enemies = _make_layer(sim.e_alive.size(), _lit_material())
	_hp_bars = _make_layer(sim.e_alive.size(), _billboard_material())
	_projectiles = _make_layer(sim.p_alive.size(), _glow_material(_color("projectile")))
	add_child(_enemies)
	add_child(_hp_bars)
	add_child(_projectiles)

func _build_environment() -> void:
	var cam_cfg: Dictionary = _theme.get("camera", {})
	camera = Camera3D.new()
	camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	camera.size = float(cam_cfg.get("size", 980.0))
	camera.near = 1.0
	camera.far = 8000.0
	camera.rotation_degrees = Vector3(
		float(cam_cfg.get("pitch_degrees", -52.0)),
		float(cam_cfg.get("yaw_degrees", -24.0)), 0.0)
	camera.current = true
	add_child(camera)
	_fit_camera()
	# The browser canvas can be any size and can be resized at any moment, so
	# framing is recomputed rather than assumed. This is also what stops a
	# future map with different proportions from needing hand-tuned camera
	# numbers.
	get_viewport().size_changed.connect(_fit_camera)

## Frame the whole playable area: every path waypoint and every pad, with a
## margin. Computed in the camera's own space, so it holds at any view angle and
## any aspect ratio.
func _fit_camera() -> void:
	var cam_cfg: Dictionary = _theme.get("camera", {})
	var margin := float(cam_cfg.get("margin", 1.12))
	var basis_inverse := camera.transform.basis.inverse()

	var min_x := INF
	var max_x := -INF
	var min_z := INF
	var max_z := -INF
	for i in _sim.waypoint_count():
		min_x = minf(min_x, _sim.waypoint_x(i))
		max_x = maxf(max_x, _sim.waypoint_x(i))
		min_z = minf(min_z, _sim.waypoint_y(i))
		max_z = maxf(max_z, _sim.waypoint_y(i))
	for i in _sim.pad_count():
		min_x = minf(min_x, _sim.pad_x(i))
		max_x = maxf(max_x, _sim.pad_x(i))
		min_z = minf(min_z, _sim.pad_y(i))
		max_z = maxf(max_z, _sim.pad_y(i))

	var centre := Vector3((min_x + max_x) * 0.5, 0.0, (min_z + max_z) * 0.5)
	var local_half_width := 0.0
	var local_half_height := 0.0
	for corner in [Vector3(min_x, 0.0, min_z), Vector3(max_x, 0.0, min_z),
			Vector3(min_x, 0.0, max_z), Vector3(max_x, 0.0, max_z)]:
		var local: Vector3 = basis_inverse * ((corner as Vector3) - centre)
		local_half_width = maxf(local_half_width, absf(local.x))
		local_half_height = maxf(local_half_height, absf(local.y))

	# Camera3D.size is the *vertical* extent under orthographic projection, so
	# the horizontal requirement has to be converted through the aspect ratio.
	var viewport := get_viewport().get_visible_rect().size
	var aspect := 1.0 if viewport.y <= 0.0 else viewport.x / viewport.y
	camera.size = maxf(local_half_height * 2.0, local_half_width * 2.0 / maxf(aspect, 0.0001)) * margin
	# Under orthographic projection distance does not change framing, only what
	# the near plane clips; push the camera well clear of the board.
	camera.position = centre + camera.transform.basis.z * float(cam_cfg.get("distance", 2400.0))

	var sun := DirectionalLight3D.new()
	sun.rotation_degrees = Vector3(
		float(_world.get("sun_pitch_degrees", -58.0)),
		float(_world.get("sun_yaw_degrees", 40.0)), 0.0)
	sun.light_energy = float(_world.get("sun_energy", 1.15))
	sun.shadow_enabled = true
	add_child(sun)

	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = _color("background")
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = _color("background").lightened(0.35)
	env.ambient_light_energy = float(_world.get("ambient_energy", 0.55))
	var world_env := WorldEnvironment.new()
	world_env.environment = env
	add_child(world_env)

## Ground, corridor and pads. Rebuilt only when a platform is placed, which is
## why this is not in the frame path.
func _build_static_geometry() -> void:
	if _static_root != null:
		_static_root.queue_free()
	_static_root = Node3D.new()
	add_child(_static_root)

	var bounds: Dictionary = _sim._db.map["bounds"]
	var ground := MeshInstance3D.new()
	var ground_mesh := PlaneMesh.new()
	# Generous overshoot: the corridor runs off both edges of the board by design.
	ground_mesh.size = Vector2(float(bounds["width"]) * 3.0, float(bounds["height"]) * 3.0)
	ground.mesh = ground_mesh
	ground.material_override = _flat_material(_color_of(_world.get("ground", "#0b0e13")))
	ground.position = Vector3(float(bounds["width"]) * 0.5, 0.0, float(bounds["height"]) * 0.5)
	_static_root.add_child(ground)

	_build_corridor()
	_build_pads()

## The corridor is one box per path segment plus one at each interior corner to
## fill the notch two boxes leave between them. Simple, and it means the walls
## follow whatever path a map file defines without any authoring step.
func _build_corridor() -> void:
	var height := float(_world.get("corridor_height", 6.0))
	var width := float(_world.get("corridor_width", 46.0))
	var wall_height := float(_world.get("wall_height", 22.0))
	var wall_width := float(_world.get("wall_width", 10.0))
	var floor_material := _flat_material(_color("path"))
	var wall_material := _lit_flat_material(_color_of(_world.get("wall", "#2b3442")))

	for i in _sim.waypoint_count() - 1:
		var ax := _sim.waypoint_x(i)
		var az := _sim.waypoint_y(i)
		var bx := _sim.waypoint_x(i + 1)
		var bz := _sim.waypoint_y(i + 1)
		var dx := bx - ax
		var dz := bz - az
		var length := sqrt(dx * dx + dz * dz)
		var yaw := atan2(dx, dz)  # render-side only; the sim never uses trigonometry
		var mid := Vector3((ax + bx) * 0.5, 0.0, (az + bz) * 0.5)

		_add_box(_static_root, mid + Vector3(0.0, height * 0.5, 0.0),
			Vector3(width, height, length), yaw, floor_material)
		# A wall down each side of the segment, offset perpendicular to it.
		var nx := dz / length
		var nz := -dx / length
		var offset := (width + wall_width) * 0.5
		for side in [-1.0, 1.0]:
			_add_box(_static_root,
				mid + Vector3(nx * offset * side, wall_height * 0.5, nz * offset * side),
				Vector3(wall_width, wall_height, length), yaw, wall_material)

	for i in range(1, _sim.waypoint_count() - 1):
		_add_box(_static_root, Vector3(_sim.waypoint_x(i), height * 0.5, _sim.waypoint_y(i)),
			Vector3(width, height, width), 0.0, floor_material)

func _build_pads() -> void:
	var pad_height := float(_world.get("pad_height", 8.0))
	var pad_radius := float(_world.get("pad_radius", 17.0))
	var platform_height := float(_world.get("platform_height", 30.0))
	var platform_radius := float(_world.get("platform_radius", 11.0))
	var free_material := _lit_flat_material(_color("pad_free"))
	var used_material := _lit_flat_material(_color("pad_occupied"))
	var turret_material := _lit_flat_material(_color("platform"))

	for i in _sim.pad_count():
		var free := _sim.pad_is_free(i)
		var base := MeshInstance3D.new()
		var cylinder := CylinderMesh.new()
		cylinder.top_radius = pad_radius
		cylinder.bottom_radius = pad_radius
		cylinder.height = pad_height
		base.mesh = cylinder
		base.material_override = free_material if free else used_material
		base.position = to_world(_sim.pad_x(i), _sim.pad_y(i), pad_height * 0.5)
		_static_root.add_child(base)
		if free:
			continue
		# A built platform: a squat barrel on top of the pad.
		var turret := MeshInstance3D.new()
		var barrel := CylinderMesh.new()
		barrel.top_radius = platform_radius * 0.65
		barrel.bottom_radius = platform_radius
		barrel.height = platform_height
		turret.mesh = barrel
		turret.material_override = turret_material
		turret.position = to_world(_sim.pad_x(i), _sim.pad_y(i), pad_height + platform_height * 0.5)
		_static_root.add_child(turret)

func _add_box(parent: Node3D, centre: Vector3, size: Vector3, yaw: float, material: Material) -> void:
	var node := MeshInstance3D.new()
	var box := BoxMesh.new()
	box.size = size
	node.mesh = box
	node.material_override = material
	node.position = centre
	node.rotation = Vector3(0.0, yaw, 0.0)
	parent.add_child(node)

func _make_layer(capacity: int, material: Material) -> MultiMeshInstance3D:
	var mesh := BoxMesh.new()
	mesh.size = Vector3.ONE
	mesh.material = material
	var mm := MultiMesh.new()
	mm.transform_format = MultiMesh.TRANSFORM_3D
	mm.use_colors = true
	mm.mesh = mesh
	mm.instance_count = capacity
	mm.visible_instance_count = 0
	var node := MultiMeshInstance3D.new()
	node.multimesh = mm
	# The corridor runs off the edge of the board; without a generous custom AABB
	# Godot culls instances whose transforms it has not measured.
	node.custom_aabb = AABB(Vector3(-4000, -400, -4000), Vector3(8000, 800, 8000))
	return node

## Called when a platform is built, so the static geometry picks it up.
func rebuild_static() -> void:
	_build_static_geometry()

func set_hover(pad_index: int) -> void:
	_hover_pad = pad_index

func hovered_pad() -> int:
	return _hover_pad

# --- per-frame -----------------------------------------------------------------

func update_visuals(alpha: float) -> void:
	_update_enemies(alpha)
	_update_projectiles(alpha)

func _update_enemies(alpha: float) -> void:
	var body_mm := _enemies.multimesh
	var bar_mm := _hp_bars.multimesh
	var enemy_height := float(_world.get("enemy_height", 18.0))
	var bar_lift := float(_world.get("hp_bar_lift", 34.0))
	var bar_width := float(_world.get("hp_bar_width", 26.0))
	var bar_thickness := float(_world.get("hp_bar_height", 4.0))
	var visible_count := 0

	for i in _sim.e_alive.size():
		if _sim.e_alive[i] == 0:
			continue
		# Interpolate distance-along-path, then resolve it to a position, so
		# enemies round corners instead of cutting across them.
		var prog: float = _sim.e_prev_prog[i] + (_sim.e_prog[i] - _sim.e_prev_prog[i]) * alpha
		_sim.sample_for_render(prog, _sim.e_offset[i])
		var px := _sim.out_x()
		var pz := _sim.out_y()
		var size := _sim.enemy_radius(_sim.e_type[i]) * 2.0

		_basis = Basis().scaled(Vector3(size, enemy_height, size))
		_xf = Transform3D(_basis, Vector3(px, enemy_height * 0.5, pz))
		body_mm.set_instance_transform(visible_count, _xf)

		var hp_max: float = float(_sim.e_hp_max[i])
		var fraction: float = 0.0 if hp_max <= 0.0 else clampf(float(_sim.e_hp[i]) / hp_max, 0.0, 1.0)
		body_mm.set_instance_color(visible_count, _enemy_hurt_color.lerp(_enemy_color, fraction))

		var filled := bar_width * fraction
		_basis = Basis().scaled(Vector3(maxf(filled, 0.001), bar_thickness, bar_thickness))
		_xf = Transform3D(_basis, Vector3(px - (bar_width - filled) * 0.5, bar_lift, pz))
		bar_mm.set_instance_transform(visible_count, _xf)
		bar_mm.set_instance_color(visible_count, _bar_color if fraction > 0.35 else _bar_back_color.lerp(_bar_color, 0.6))

		visible_count += 1
	body_mm.visible_instance_count = visible_count
	bar_mm.visible_instance_count = visible_count

func _update_projectiles(alpha: float) -> void:
	var mm := _projectiles.multimesh
	var length := float(_world.get("projectile_length", 16.0))
	var width := float(_world.get("projectile_width", 3.0))
	var lift := float(_world.get("projectile_lift", 14.0))
	var visible_count := 0

	for i in _sim.p_alive.size():
		if _sim.p_alive[i] == 0:
			continue
		var px: float = _sim.p_prev_x[i] + (_sim.p_x[i] - _sim.p_prev_x[i]) * alpha
		var pz: float = _sim.p_prev_y[i] + (_sim.p_y[i] - _sim.p_prev_y[i]) * alpha
		var dx: float = _sim.p_x[i] - _sim.p_prev_x[i]
		var dz: float = _sim.p_y[i] - _sim.p_prev_y[i]
		var travel := sqrt(dx * dx + dz * dz)
		if travel < 0.0001:
			dx = 0.0
			dz = 1.0
		else:
			dx /= travel
			dz /= travel
		# Build the basis straight from the direction vector - the tracer's long
		# axis is Z, so Z is the direction and X is its perpendicular.
		_basis = Basis(
			Vector3(dz * width, 0.0, -dx * width),
			Vector3(0.0, width, 0.0),
			Vector3(dx * length, 0.0, dz * length))
		_xf = Transform3D(_basis, Vector3(px, lift, pz))
		mm.set_instance_transform(visible_count, _xf)
		mm.set_instance_color(visible_count, Color.WHITE)
		visible_count += 1
	mm.visible_instance_count = visible_count

# --- materials -----------------------------------------------------------------

func _lit_material() -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.vertex_color_use_as_albedo = true
	material.roughness = 0.75
	return material

func _billboard_material() -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.vertex_color_use_as_albedo = true
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	material.disable_receive_shadows = true
	return material

func _glow_material(tint: Color) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = tint
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	material.emission_enabled = true
	material.emission = tint
	material.emission_energy_multiplier = 1.6
	return material

func _flat_material(tint: Color) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = tint
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	return material

func _lit_flat_material(tint: Color) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = tint
	material.roughness = 0.7
	return material

func _color(key: String) -> Color:
	return Color(str(_theme.get(key, "#ff00ff")))

func _color_of(value: Variant) -> Color:
	return Color(str(value))
