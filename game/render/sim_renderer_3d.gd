class_name SimRenderer3D
extends Node3D

## Draws the simulation in 3D. Owns no game state and never writes to the Sim.
##
## The simulation has never changed to support anything in this file. Its world is
## a flat plane in (x, y) with no notion of height, and the renderer maps it to
## (x, 0, y); the third dimension is presentation only. That is the payoff of
## keeping core/ free of engine types - the entire presentation layer has now been
## replaced twice without touching a line of game logic.
##
## Everything that scales with entity count goes through MultiMeshInstance3D:
## enemies (one layer per class, so each class can have its own silhouette),
## health bars, projectiles, buildable cells, and the three parts of every turret.
## Static scenery - ground and corridor - is built into single ArrayMeshes rather
## than one node per segment. That is both a large draw-call saving and the reason
## the corridor can have continuous, correctly-lit surfaces instead of a row of
## boxes with visible seams.
##
## Lighting is a key light with shadows plus a cool fill, over a screen-space
## ambient-occlusion and glow pass. The previous version drew most surfaces
## unshaded, which is why it read as a diagram rather than a scene.

## Simulation (x, y) becomes world (x, 0, y). Kept as a named function rather
## than inlined so there is exactly one place this mapping is defined.
static func to_world(sim_x: float, sim_y: float, height: float) -> Vector3:
	return Vector3(sim_x, height, sim_y)

var _sim: Sim
var _theme: Dictionary = {}
var _world: Dictionary = {}

var camera: Camera3D
var _enemy_layers: Array[MultiMeshInstance3D] = []
var _hp_bars: MultiMeshInstance3D
var _projectiles: MultiMeshInstance3D
var _cells: MultiMeshInstance3D
var _turret_bases: MultiMeshInstance3D
var _turret_bodies: MultiMeshInstance3D
var _turret_barrels: MultiMeshInstance3D
var _scenery: Node3D
## Held so the shadow range can be refitted whenever the framing changes. Boards
## differ by 50% in length and an act reveals a third of one, so a fixed range
## either wastes depth resolution on a small act or cuts shadows off halfway
## across a big one.
var _sun: DirectionalLight3D

## Camera zoom and pan, on top of whatever framing _fit_camera worked out.
##
## Kept as a multiplier and an offset rather than as an absolute camera position,
## so the auto-fit still owns the base framing: the window can be resized, or a
## chain can extend the corridor, and the view the player chose survives it.
var _zoom: float = 1.0
var _pan := Vector3.ZERO
## Base framing, recomputed by _fit_camera and reused when only zoom or pan moved.
var _view_centre := Vector3.ZERO
var _view_extent: float = 1.0
var _cursor: Node3D
var _cursor_disc: MeshInstance3D
var _cursor_ghost: MeshInstance3D

# Resolved once at setup; parsing colours from strings inside the frame loop is a
# String allocation per colour per frame in the one place we promise not to.
var _enemy_color := Color.WHITE
var _enemy_hurt_color := Color.WHITE
var _bar_color := Color.WHITE
var _bar_back_color := Color.WHITE

# Smoothed turret facing, so barrels swing rather than snap. Purely visual: the
# simulation's aim is instant, and nothing here feeds back into it.
var _barrel_angle: PackedFloat64Array = PackedFloat64Array()
var _reference_radius: float = 1.0

# Reused every frame; Transform3D and Basis are value types.
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
	_barrel_angle.resize(sim.t_used.size())
	# Height scales relative to the baseline drone, so classes stay in proportion
	# to each other whatever their radii happen to be.
	_reference_radius = maxf(sim.enemy_radius(maxi(sim.enemy_index("walker"), 0)), 0.001)

	_build_environment()
	_build_scenery()
	_build_cell_layer()
	_build_turret_layers()
	_build_entity_layers()
	refresh_board()

# --- environment ---------------------------------------------------------------

func _build_environment() -> void:
	var cfg: Dictionary = _theme.get("camera", {})
	camera = Camera3D.new()
	if str(cfg.get("projection", "perspective")) == "orthogonal":
		camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	else:
		camera.projection = Camera3D.PROJECTION_PERSPECTIVE
		camera.fov = float(cfg.get("fov_degrees", 26.0))
	camera.near = 10.0
	camera.far = 20000.0
	camera.rotation_degrees = Vector3(
		float(cfg.get("pitch_degrees", -38.0)),
		float(cfg.get("yaw_degrees", -22.0)), 0.0)
	camera.current = true
	add_child(camera)

	_sun = DirectionalLight3D.new()
	_sun.rotation_degrees = Vector3(
		float(_world.get("sun_pitch_degrees", -46.0)),
		float(_world.get("sun_yaw_degrees", 35.0)), 0.0)
	_sun.light_energy = float(_world.get("sun_energy", 2.1))
	_sun.light_color = _color_of(_world.get("sun_colour", "#fff2dc"))
	_sun.shadow_enabled = true
	_sun.directional_shadow_mode = DirectionalLight3D.SHADOW_ORTHOGONAL
	_sun.shadow_bias = 0.06
	add_child(_sun)

	# A cool, shadowless fill from the opposite side. Without it the unlit faces
	# of everything go to flat ambient and the geometry loses its edges.
	var fill := DirectionalLight3D.new()
	fill.rotation_degrees = Vector3(
		float(_world.get("fill_pitch_degrees", -22.0)),
		float(_world.get("fill_yaw_degrees", -145.0)), 0.0)
	fill.light_energy = float(_world.get("fill_energy", 0.55))
	fill.light_color = _color_of(_world.get("fill_colour", "#5f7fb8"))
	fill.shadow_enabled = false
	add_child(fill)

	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = _color("background")
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = _color("background").lightened(0.45)
	env.ambient_light_energy = float(_world.get("ambient_energy", 0.35))

	# Contact shadows where geometry meets geometry - the single biggest
	# contributor to a scene reading as solid rather than as flat shapes.
	#
	# It only exists on the Forward+ renderer. The web build runs Compatibility
	# (WebGL 2), so on the way most people will actually play this, SSAO is off
	# and Godot says so in a warning nobody reads. Asking for it there produced a
	# console warning per level load and no pixels, so it is asked for only where
	# it is real, and the ambient term is lifted a little where it is not - not a
	# substitute, but it stops unlit faces crushing to flat colour without the
	# occlusion pass to give them shape.
	if RenderingServer.get_rendering_device() != null:
		env.ssao_enabled = true
		env.ssao_radius = float(_world.get("ssao_radius", 42.0))
		env.ssao_intensity = float(_world.get("ssao_intensity", 2.4))
	else:
		env.ambient_light_energy *= float(_world.get("ambient_lift_without_ssao", 1.35))

	env.glow_enabled = true
	env.glow_intensity = float(_world.get("glow_strength", 1.15))
	env.glow_bloom = float(_world.get("glow_bloom", 0.25))
	env.glow_blend_mode = Environment.GLOW_BLEND_MODE_ADDITIVE

	# Depth fog pulls the far end of the corridor back and gives the board scale.
	env.fog_enabled = true
	env.fog_light_color = _color_of(_world.get("fog_colour", "#0d1219"))
	env.fog_density = float(_world.get("fog_density", 0.00022))

	env.tonemap_mode = Environment.TONE_MAPPER_FILMIC
	env.tonemap_exposure = float(_world.get("tonemap_exposure", 1.05))
	env.tonemap_white = float(_world.get("tonemap_white", 3.0))

	var world_env := WorldEnvironment.new()
	world_env.environment = env
	add_child(world_env)

	_fit_camera()
	# The browser canvas can be any size and can be resized at any moment.
	# Guarded because the renderer is also built off-tree by the headless tests.
	var viewport := get_viewport()
	if viewport != null:
		viewport.size_changed.connect(_fit_camera)

## Frame the corridor plus the band beside it that can be built on, with a
## margin. Computed in the camera's own space so it holds at any angle, aspect
## ratio or projection.
func _fit_camera() -> void:
	var cfg: Dictionary = _theme.get("camera", {})
	var margin := float(cfg.get("margin", 1.18))
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
	var reach := _sim.build_max_distance()
	min_x -= reach
	max_x += reach
	min_z -= reach
	max_z += reach

	var centre := Vector3((min_x + max_x) * 0.5, 0.0, (min_z + max_z) * 0.5)
	# Pan is clamped to the framed area, so the board can never be driven off
	# screen entirely - getting lost in empty space is not a camera control.
	_view_centre = centre
	_view_extent = maxf(max_x - min_x, max_z - min_z) * 0.5
	var half_width := 0.0
	var half_height := 0.0
	for corner in [Vector3(min_x, 0.0, min_z), Vector3(max_x, 0.0, min_z),
			Vector3(min_x, 0.0, max_z), Vector3(max_x, 0.0, max_z)]:
		var local: Vector3 = basis_inverse * ((corner as Vector3) - centre)
		half_width = maxf(half_width, absf(local.x))
		half_height = maxf(half_height, absf(local.y))

	var viewport := get_viewport()
	var aspect := float(cfg.get("fallback_aspect", 16.0 / 9.0))
	if viewport != null:
		var size := viewport.get_visible_rect().size
		if size.y > 0.0:
			aspect = size.x / size.y
	var needed := maxf(half_height * 2.0, half_width * 2.0 / maxf(aspect, 0.0001)) * margin * _zoom
	centre += _pan

	if camera.projection == Camera3D.PROJECTION_ORTHOGONAL:
		camera.size = needed
		camera.position = centre + camera.transform.basis.z * float(cfg.get("distance", 4200.0))
		_fit_shadows(needed)
		return
	# Perspective: pull back far enough that the required extent fits the frustum.
	# tan() is fine here - this is presentation, not simulation.
	var half_fov := deg_to_rad(camera.fov) * 0.5
	var distance := maxf((needed * 0.5) / tan(half_fov), float(cfg.get("distance", 4200.0)) * 0.25)
	camera.position = centre + camera.transform.basis.z * distance
	_fit_shadows(distance + needed)

## Shadows are cast within a distance of the camera, so the range has to follow
## the framing. Too short and the far half of a long act renders unshadowed -
## which reads as two different scenes joined down the middle.
func _fit_shadows(reach: float) -> void:
	if _sun == null:
		return
	_sun.directional_shadow_max_distance = maxf(reach, 1.0) * float(
		(_world.get("shadow_range_margin", 1.25)))

# --- static scenery --------------------------------------------------------------

## Ground and corridor, each a single mesh.
##
## The previous version emitted one BoxMesh node per path segment per wall plus a
## disc at every corner - around 140 draw calls before a single entity existed,
## and visible seams wherever two boxes met. Building the whole corridor as one
## ArrayMesh with shared vertices removes both problems at once.
func _build_scenery() -> void:
	if _scenery != null:
		remove_child(_scenery)
		_scenery.queue_free()
	_scenery = Node3D.new()
	add_child(_scenery)

	var ground := MeshInstance3D.new()
	var plane := PlaneMesh.new()
	# Far larger than the board: at a shallow camera angle the horizon is a long
	# way out, and a visible ground edge reads as a rendering bug.
	plane.size = Vector2(_sim.bounds_width() * 14.0, _sim.bounds_height() * 14.0)
	ground.mesh = plane
	ground.material_override = _surface_material(_color_of(_world.get("ground", "#12161d")),
		float(_world.get("ground_metallic", 0.0)), float(_world.get("ground_roughness", 0.95)))
	ground.position = Vector3(_sim.bounds_width() * 0.5, 0.0, _sim.bounds_height() * 0.5)
	_scenery.add_child(ground)

	var road := MeshInstance3D.new()
	road.mesh = _corridor_mesh()
	road.material_override = _surface_material(_color("path"),
		float(_world.get("path_metallic", 0.15)), float(_world.get("path_roughness", 0.8)))
	_scenery.add_child(road)

	var walls := MeshInstance3D.new()
	walls.mesh = _wall_mesh()
	walls.material_override = _surface_material(_color_of(_world.get("wall", "#39424f")),
		float(_world.get("wall_metallic", 0.55)), float(_world.get("wall_roughness", 0.42)))
	_scenery.add_child(walls)

## The corridor, as one mesh built from a handful of quad strips.
##
## A strip runs the length of the path between two "rails", where a rail is a
## lateral offset from the centre line at a fixed height. Everything the corridor
## needs is expressible that way: the road surface is a strip between its two
## edges, a wall is a vertical strip plus a horizontal cap. Building it this way
## rather than as one box per segment means corners mitre correctly and the whole
## thing is a single draw call.
func _corridor_mesh() -> ArrayMesh:
	var half := float(_world.get("corridor_width", 76.0)) * 0.5
	var surface := float(_world.get("corridor_height", 9.0))
	var builder := _StripBuilder.new(_sim)
	# Road surface, and a lip down each edge so it reads as a raised slab.
	builder.strip(-half, surface, half, surface)
	builder.strip(-half, 0.0, -half, surface)
	builder.strip(half, surface, half, 0.0)
	return builder.commit()

func _wall_mesh() -> ArrayMesh:
	var half := float(_world.get("corridor_width", 76.0)) * 0.5
	var thickness := float(_world.get("wall_width", 16.0))
	var height := float(_world.get("wall_height", 34.0))
	var builder := _StripBuilder.new(_sim)
	for side: float in [-1.0, 1.0]:
		var inner := half * side
		var outer := (half + thickness) * side
		# Inner face (toward the road), top cap, outer face.
		builder.strip(inner, 0.0, inner, height, side < 0.0)
		builder.strip(inner, height, outer, height, side < 0.0)
		builder.strip(outer, height, outer, 0.0, side < 0.0)
	return builder.commit()

## Accumulates quad strips along the path into one indexed mesh.
##
## Offsets are mitred at each waypoint - the lateral direction used is
## perpendicular to the *average* of the incoming and outgoing segment
## directions - so the outside of a corner does not tear open and the inside does
## not overlap itself.
class _StripBuilder:
	var _sim: Sim
	var _vertices := PackedVector3Array()
	var _indices := PackedInt32Array()
	var _nx := PackedFloat64Array()
	var _nz := PackedFloat64Array()

	func _init(sim: Sim) -> void:
		_sim = sim
		var count := sim.waypoint_count()
		_nx.resize(count)
		_nz.resize(count)
		for i in count:
			var dx := 0.0
			var dz := 0.0
			if i > 0:
				dx += sim.waypoint_x(i) - sim.waypoint_x(i - 1)
				dz += sim.waypoint_y(i) - sim.waypoint_y(i - 1)
			if i < count - 1:
				dx += sim.waypoint_x(i + 1) - sim.waypoint_x(i)
				dz += sim.waypoint_y(i + 1) - sim.waypoint_y(i)
			var length := sqrt(dx * dx + dz * dz)
			if length <= 0.0:
				length = 1.0
			_nx[i] = dz / length
			_nz[i] = -dx / length

	func strip(offset_a: float, height_a: float, offset_b: float, height_b: float,
			flip: bool = false) -> void:
		var count := _sim.waypoint_count()
		var base := _vertices.size()
		for i in count:
			var px := _sim.waypoint_x(i)
			var pz := _sim.waypoint_y(i)
			_vertices.append(Vector3(px + _nx[i] * offset_a, height_a, pz + _nz[i] * offset_a))
			_vertices.append(Vector3(px + _nx[i] * offset_b, height_b, pz + _nz[i] * offset_b))
		for i in count - 1:
			var a := base + i * 2
			var b := base + (i + 1) * 2
			if flip:
				_indices.append_array([a, a + 1, b, a + 1, b + 1, b])
			else:
				_indices.append_array([a, b, a + 1, a + 1, b, b + 1])

	func commit() -> ArrayMesh:
		var mesh := ArrayMesh.new()
		if _vertices.is_empty():
			return mesh
		var arrays := []
		arrays.resize(Mesh.ARRAY_MAX)
		arrays[Mesh.ARRAY_VERTEX] = _vertices
		arrays[Mesh.ARRAY_INDEX] = _indices
		var normals := PackedVector3Array()
		normals.resize(_vertices.size())
		normals.fill(Vector3.UP)
		arrays[Mesh.ARRAY_NORMAL] = normals
		mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
		# Real normals from the geometry, so lighting follows the surfaces rather
		# than the placeholder ups above.
		var tool := SurfaceTool.new()
		tool.create_from(mesh, 0)
		tool.generate_normals()
		mesh.clear_surfaces()
		tool.commit(mesh)
		return mesh

# --- instanced layers -------------------------------------------------------------

func _build_cell_layer() -> void:
	var mesh := BoxMesh.new()
	mesh.size = Vector3.ONE
	var material := StandardMaterial3D.new()
	material.vertex_color_use_as_albedo = true
	material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mesh.material = material
	_cells = _instanced(mesh, _sim.grid_cols() * _sim.grid_rows())
	add_child(_cells)

func _build_turret_layers() -> void:
	var limit := _sim.t_used.size()
	var base := CylinderMesh.new()
	base.top_radius = float(_world.get("platform_radius", 18.0)) * 1.45
	base.bottom_radius = float(_world.get("platform_radius", 18.0)) * 1.6
	base.height = float(_world.get("pad_height", 12.0))
	base.material = _instanced_material()
	_turret_bases = _instanced(base, limit)

	var body := CylinderMesh.new()
	body.top_radius = float(_world.get("platform_radius", 18.0)) * 0.7
	body.bottom_radius = float(_world.get("platform_radius", 18.0))
	body.height = float(_world.get("platform_height", 48.0))
	body.material = _instanced_material()
	_turret_bodies = _instanced(body, limit)

	# A barrel makes the turret read as a machine with a front, and makes it
	# obvious at a glance what each one is shooting at.
	var barrel := BoxMesh.new()
	barrel.size = Vector3.ONE
	barrel.material = _instanced_material()
	_turret_barrels = _instanced(barrel, limit)

	add_child(_turret_bases)
	add_child(_turret_bodies)
	add_child(_turret_barrels)

func _build_entity_layers() -> void:
	# One layer per enemy class, so each can have its own silhouette. Class is
	# readable from shape as well as colour, which section 6.1 of the plan makes
	# a correctness requirement rather than a nicety.
	_enemy_layers.clear()
	for type_index in _sim.enemy_type_count():
		var mesh := _enemy_mesh(_sim.enemy_id(type_index))
		mesh.material = _instanced_material(_sim.enemy_id(type_index))
		var layer := _instanced(mesh, _sim.e_alive.size())
		_enemy_layers.append(layer)
		add_child(layer)

	var bar := BoxMesh.new()
	bar.size = Vector3.ONE
	var bar_material := StandardMaterial3D.new()
	bar_material.vertex_color_use_as_albedo = true
	bar_material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	bar_material.disable_receive_shadows = true
	bar.material = bar_material
	_hp_bars = _instanced(bar, _sim.e_alive.size())
	add_child(_hp_bars)

	var tracer := BoxMesh.new()
	tracer.size = Vector3.ONE
	var tracer_material := StandardMaterial3D.new()
	tracer_material.vertex_color_use_as_albedo = true
	tracer_material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	tracer_material.emission_enabled = true
	tracer_material.emission = _color("projectile")
	tracer_material.emission_energy_multiplier = 3.2
	tracer.material = tracer_material
	_projectiles = _instanced(tracer, _sim.p_alive.size())
	add_child(_projectiles)

## Distinct silhouettes per drone class. Skitters are small and pointed, Walkers
## are boxy, Bulwarks are heavy slabs, Lances are long narrow darts.
##
## The Lance is the one that most needs to be identifiable at a glance: it is the
## fastest thing on the board, so by the time you have read a health bar it has
## covered ground nothing else could. Hence a silhouette nothing else shares and
## an emissive skin on top of it.
func _enemy_mesh(enemy_id: String) -> Mesh:
	match enemy_id:
		"swarm":
			var prism := PrismMesh.new()
			prism.size = Vector3.ONE
			return prism
		"heavy":
			var slab := BoxMesh.new()
			slab.size = Vector3(1.15, 1.0, 1.4)
			return slab
		"lance":
			var dart := PrismMesh.new()
			dart.size = Vector3(0.62, 1.0, 2.05)
			return dart
		"breaker":
			# Six-sided and squat: nothing else on the board is round, so a
			# Breaker is identifiable from its outline alone even at 4x speed.
			var bunker := CylinderMesh.new()
			bunker.top_radius = 0.44
			bunker.bottom_radius = 0.58
			bunker.height = 1.0
			bunker.radial_segments = 6
			return bunker
		_:
			var box := BoxMesh.new()
			box.size = Vector3.ONE
			return box

## Which classes glow. Reserved for the top of the ladder - if everything is lit
## up, nothing is.
const EMISSIVE_CLASSES := ["lance"]

func _instanced_material(enemy_id: String = "") -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.vertex_color_use_as_albedo = true
	material.metallic = float(_world.get("enemy_metallic", 0.12))
	material.roughness = float(_world.get("enemy_roughness", 0.62))
	if EMISSIVE_CLASSES.has(enemy_id):
		# Emission is a flat add, so the per-instance damage tint still reads
		# through it - a hurt Lance dims like everything else, it just never stops
		# being the bright thing on the board.
		material.emission_enabled = true
		material.emission = _color_of(_world.get("elite_glow", "#ff7a3c"))
		material.emission_energy_multiplier = float(_world.get("elite_glow_energy", 1.6))
	return material

func _instanced(mesh: Mesh, capacity: int) -> MultiMeshInstance3D:
	var mm := MultiMesh.new()
	mm.transform_format = MultiMesh.TRANSFORM_3D
	mm.use_colors = true
	mm.mesh = mesh
	mm.instance_count = maxi(capacity, 1)
	mm.visible_instance_count = 0
	var node := MultiMeshInstance3D.new()
	node.multimesh = mm
	node.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON
	# The corridor runs off the edge of the board; without a generous custom AABB
	# Godot culls instances whose transforms it has not measured.
	node.custom_aabb = AABB(Vector3(-6000, -600, -6000), Vector3(14000, 1200, 14000))
	return node

# --- board state (turrets and owned ground) ---------------------------------------

## Rebuilt when the board changes - a turret placed or upgraded, or ground
## bought. Not per frame.
func refresh_board() -> void:
	_refresh_cells()
	_refresh_turrets()

func _refresh_cells() -> void:
	var mm := _cells.multimesh
	var height := float(_world.get("band_height", 2.0))
	var size := _sim.cell_size() - float(_world.get("cell_inset", 5.0))
	var owned := _color_of(_world.get("band", "#1e4034"))
	var offered := _color_of(_world.get("band_offer", "#39506e"))
	var shown := 0
	for cy in _sim.grid_rows():
		for cx in _sim.grid_cols():
			var unlocked := _sim.cell_is_unlocked(cx, cy)
			if not unlocked and not _sim.cell_is_offerable(cx, cy):
				continue
			var tint := owned if unlocked else offered
			tint.a = float(_world.get("band_alpha", 0.55)) if unlocked \
				else float(_world.get("band_offer_alpha", 0.3))
			mm.set_instance_transform(shown, Transform3D(
				Basis().scaled(Vector3(size, height, size)),
				to_world(_sim.cell_centre_x(cx), _sim.cell_centre_y(cy), height * 0.5)))
			mm.set_instance_color(shown, tint)
			shown += 1
	mm.visible_instance_count = shown

func _refresh_turrets() -> void:
	var pad_height := float(_world.get("pad_height", 12.0))
	var body_height := float(_world.get("platform_height", 48.0))
	var growth := float(_world.get("platform_tier_growth", 0.28))
	var top_colour := _color("platform_max_tier")
	var ballistic := _color("platform")
	var cannon := _color_of(_world.get("platform_cannon", "#c98a5b"))
	var suppressor := _color_of(_world.get("platform_suppressor", "#4fc9d8"))
	var railgun := _color_of(_world.get("platform_railgun", "#d8d24f"))
	var plinth := _color("pad_occupied")

	var bases := _turret_bases.multimesh
	var bodies := _turret_bodies.multimesh
	for i in _sim.t_count:
		var tier := _sim.platform_tier(i)
		var max_tier := maxi(_sim.platform_max_tier(_sim.platform_blueprint(i)) - 1, 1)
		var scale := 1.0 + growth * float(tier)
		var fraction := float(tier) / float(max_tier)
		var family := _family_colour(i, ballistic, cannon, suppressor, railgun)
		var tint := family.lerp(top_colour, fraction)

		bases.set_instance_transform(i, Transform3D(Basis(),
			to_world(_sim.t_x[i], _sim.t_y[i], pad_height * 0.5)))
		bases.set_instance_color(i, plinth)

		bodies.set_instance_transform(i, Transform3D(
			Basis().scaled(Vector3(scale, scale, scale)),
			to_world(_sim.t_x[i], _sim.t_y[i], pad_height + body_height * scale * 0.5)))
		bodies.set_instance_color(i, tint)
	bases.visible_instance_count = _sim.t_count
	bodies.visible_instance_count = _sim.t_count

# --- per frame ---------------------------------------------------------------------

func update_visuals(alpha: float) -> void:
	_update_enemies(alpha)
	_update_projectiles(alpha)
	_update_barrels()

func _update_enemies(alpha: float) -> void:
	var bar_mm := _hp_bars.multimesh
	var enemy_height := float(_world.get("enemy_height", 26.0))
	var bar_lift := float(_world.get("hp_bar_lift", 50.0))
	var bar_width := float(_world.get("hp_bar_width", 38.0))
	var bar_thickness := float(_world.get("hp_bar_height", 6.0))

	var per_layer := PackedInt32Array()
	per_layer.resize(_enemy_layers.size())
	per_layer.fill(0)
	var bars := 0

	for i in _sim.e_alive.size():
		if _sim.e_alive[i] == 0:
			continue
		# Interpolate distance-along-path, then resolve it to a position, so
		# enemies round corners instead of cutting across them.
		var prog: float = _sim.e_prev_prog[i] + (_sim.e_prog[i] - _sim.e_prev_prog[i]) * alpha
		_sim.sample_for_render(prog, _sim.e_offset[i])
		var px := _sim.out_x()
		var pz := _sim.out_y()
		var type_index: int = _sim.e_type[i]
		# Footprint is the enemy's own radius; height scales with it so a Bulwark
		# is visibly a bigger machine than a Skitter, not just a wider one.
		var radius := _sim.enemy_radius(type_index)
		var footprint := radius * 2.0
		var height := enemy_height * (radius / _reference_radius)

		var layer := _enemy_layers[clampi(type_index, 0, _enemy_layers.size() - 1)]
		var mm := layer.multimesh
		var slot := per_layer[type_index]
		mm.set_instance_transform(slot, Transform3D(
			Basis().scaled(Vector3(footprint, height, footprint)),
			Vector3(px, height * 0.5, pz)))

		var hp_max: float = float(_sim.e_hp_max[i])
		var fraction: float = 0.0 if hp_max <= 0.0 else clampf(float(_sim.e_hp[i]) / hp_max, 0.0, 1.0)
		mm.set_instance_color(slot, _enemy_hurt_color.lerp(_enemy_color, fraction))
		per_layer[type_index] = slot + 1

		var filled := bar_width * fraction
		bar_mm.set_instance_transform(bars, Transform3D(
			Basis().scaled(Vector3(maxf(filled, 0.001), bar_thickness, bar_thickness)),
			Vector3(px - (bar_width - filled) * 0.5, bar_lift, pz)))
		bar_mm.set_instance_color(bars, _bar_color if fraction > 0.35 else _bar_back_color.lerp(_bar_color, 0.6))
		bars += 1

	for index in _enemy_layers.size():
		_enemy_layers[index].multimesh.visible_instance_count = per_layer[index]
	bar_mm.visible_instance_count = bars

func _update_projectiles(alpha: float) -> void:
	var mm := _projectiles.multimesh
	var length := float(_world.get("projectile_length", 24.0))
	var width := float(_world.get("projectile_width", 5.0))
	var lift := float(_world.get("projectile_lift", 20.0))
	var tint := _color("projectile")
	var shown := 0

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
		# A shell is fatter and shorter than a bullet, so the two families read
		# differently in flight.
		var is_shell := _sim.p_splash[i] > 0.0
		var long_axis := length * (0.55 if is_shell else 1.0)
		var girth := width * (2.0 if is_shell else 1.0)
		_basis = Basis(
			Vector3(dz * girth, 0.0, -dx * girth),
			Vector3(0.0, girth, 0.0),
			Vector3(dx * long_axis, 0.0, dz * long_axis))
		mm.set_instance_transform(shown, Transform3D(_basis, Vector3(px, lift, pz)))
		mm.set_instance_color(shown, tint)
		shown += 1
	mm.visible_instance_count = shown

## Swing each barrel toward whatever its turret last fired at. Smoothed here
## rather than in the simulation: the sim's aim is instant and authoritative, and
## this is only how it looks.
func _update_barrels() -> void:
	var mm := _turret_barrels.multimesh
	var pad_height := float(_world.get("pad_height", 12.0))
	var body_height := float(_world.get("platform_height", 48.0))
	var growth := float(_world.get("platform_tier_growth", 0.28))
	var barrel_length := float(_world.get("barrel_length", 34.0))
	var barrel_radius := float(_world.get("barrel_radius", 5.5))
	var turn := float(_world.get("turret_turn_rate", 9.0))
	var top_colour := _color("platform_max_tier")
	var ballistic := _color("platform")
	var cannon := _color_of(_world.get("platform_cannon", "#c98a5b"))
	var suppressor := _color_of(_world.get("platform_suppressor", "#4fc9d8"))
	var railgun := _color_of(_world.get("platform_railgun", "#d8d24f"))

	for i in _sim.t_count:
		var target := atan2(_sim.t_aim_x[i], _sim.t_aim_y[i])
		var current: float = _barrel_angle[i] if i < _barrel_angle.size() else target
		# Shortest way round, so a turret never spins the long way to track a
		# target that crossed behind it.
		var delta := wrapf(target - current, -PI, PI)
		current = current + delta * clampf(turn * 0.0333, 0.0, 1.0)
		if i < _barrel_angle.size():
			_barrel_angle[i] = current

		var scale := 1.0 + growth * float(_sim.platform_tier(i))
		var dx := sin(current)
		var dz := cos(current)
		var lift := pad_height + body_height * scale * 0.78
		var reach := barrel_length * scale
		var girth := barrel_radius * scale
		_basis = Basis(
			Vector3(dz * girth, 0.0, -dx * girth),
			Vector3(0.0, girth, 0.0),
			Vector3(dx * reach, 0.0, dz * reach))
		mm.set_instance_transform(i, Transform3D(_basis,
			Vector3(_sim.t_x[i] + dx * reach * 0.5, lift, _sim.t_y[i] + dz * reach * 0.5)))
		var max_tier := maxi(_sim.platform_max_tier(_sim.platform_blueprint(i)) - 1, 1)
		var family := _family_colour(i, ballistic, cannon, suppressor, railgun)
		mm.set_instance_color(i, family.lerp(top_colour, float(_sim.platform_tier(i)) / float(max_tier)))
	mm.visible_instance_count = _sim.t_count

## Which family a turret belongs to, for tinting.
##
## Asked in the right order on purpose: the Suppressor has a blast radius too, so
## testing "does it splash" first would paint every Suppressor as a Cannon. Read
## from behaviour rather than from the blueprint's name, so a fourth family that
## slows or splashes inherits sensible colours without touching this.
func _family_colour(index: int, ballistic: Color, cannon: Color, suppressor: Color,
		railgun: Color) -> Color:
	if _sim.platform_slow_factor(index) < 1.0:
		return suppressor
	if _sim.platform_pierce(index) > 0.0:
		return railgun
	if _sim.platform_splash(index) > 0.0:
		return cannon
	return ballistic

## --- camera control --------------------------------------------------------
##
## Boards run to 16,000 units and a whole act framed at once makes a single
## turret about four pixels wide. Zoom is a multiplier on the fitted extent -
## below 1 is closer in - and pan slides the framed centre around, clamped so the
## board cannot be driven off screen.
const ZOOM_MIN := 0.18
const ZOOM_MAX := 1.0
const ZOOM_STEP := 0.86

## Zoom about a world point rather than about the screen centre, so the thing
## under the cursor stays roughly under the cursor. `focus` is where the pointer
## is on the ground plane; pass the current centre to zoom about the middle.
func zoom_by(steps: int, focus: Vector3) -> void:
	var before := _zoom
	_zoom = clampf(_zoom * pow(ZOOM_STEP, float(steps)), ZOOM_MIN, ZOOM_MAX)
	if is_equal_approx(before, _zoom):
		return
	# Move the centre toward the focus in proportion to how much closer we got.
	var shift := (focus - (_view_centre + _pan)) * (1.0 - _zoom / before)
	_pan += Vector3(shift.x, 0.0, shift.z)
	_clamp_pan()
	_fit_camera()

func pan_by(delta: Vector3) -> void:
	_pan += Vector3(delta.x, 0.0, delta.z)
	_clamp_pan()
	_fit_camera()

func reset_view() -> void:
	_zoom = 1.0
	_pan = Vector3.ZERO
	_fit_camera()

func zoom() -> float:
	return _zoom

## How far the eye may wander from the framed centre: further the closer you are,
## because at full zoom-out there is nothing off screen worth looking at.
func _clamp_pan() -> void:
	var reach := _view_extent * (1.0 - _zoom)
	_pan.x = clampf(_pan.x, -reach, reach)
	_pan.z = clampf(_pan.z, -reach, reach)
	_pan.y = 0.0

# --- build cursor -------------------------------------------------------------------

func set_build_cursor(x: float, y: float, radius: float, allowed: bool) -> void:
	if _cursor == null:
		_build_cursor_nodes()
	if radius <= 0.0:
		_cursor.visible = false
		return
	_cursor.visible = true
	_cursor.position = to_world(x, y, float(_world.get("band_height", 2.0)) + 0.6)
	var disc := _cursor_disc.mesh as CylinderMesh
	disc.top_radius = radius
	disc.bottom_radius = radius
	var tint: Color = _color("good") if allowed else _color("bad")
	tint.a = float(_world.get("cursor_alpha", 0.16))
	(_cursor_disc.material_override as StandardMaterial3D).albedo_color = tint
	var solid := tint
	solid.a = 0.8
	(_cursor_ghost.material_override as StandardMaterial3D).albedo_color = solid

func _build_cursor_nodes() -> void:
	_cursor = Node3D.new()
	add_child(_cursor)
	_cursor_disc = MeshInstance3D.new()
	var disc := CylinderMesh.new()
	disc.height = 1.0
	_cursor_disc.mesh = disc
	_cursor_disc.material_override = _transparent_material(Color.WHITE, 0.16)
	_cursor_disc.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	_cursor.add_child(_cursor_disc)
	_cursor_ghost = MeshInstance3D.new()
	var ghost := CylinderMesh.new()
	ghost.top_radius = float(_world.get("platform_radius", 18.0)) * 0.7
	ghost.bottom_radius = float(_world.get("platform_radius", 18.0))
	ghost.height = float(_world.get("platform_height", 48.0))
	_cursor_ghost.mesh = ghost
	_cursor_ghost.material_override = _transparent_material(Color.WHITE, 0.8)
	_cursor_ghost.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	_cursor_ghost.position = Vector3(0.0, ghost.height * 0.5, 0.0)
	_cursor.add_child(_cursor_ghost)

# --- accessors used by tests ----------------------------------------------------------

## The layers whose instance counts scale with entities. Named rather than
## positional: the cell grid and the turret parts are MultiMeshes too, and
## anything keying off child order breaks the moment another one is added.
func entity_layers() -> Array:
	var layers: Array = []
	layers.append_array(_enemy_layers)
	layers.append(_hp_bars)
	layers.append(_projectiles)
	return layers

func enemy_layer(type_index: int = 0) -> MultiMeshInstance3D: return _enemy_layers[type_index]
func enemy_layer_count() -> int: return _enemy_layers.size()
func hp_bar_layer() -> MultiMeshInstance3D: return _hp_bars
func projectile_layer() -> MultiMeshInstance3D: return _projectiles
func cell_layer() -> MultiMeshInstance3D: return _cells
func turret_layers() -> Array: return [_turret_bases, _turret_bodies, _turret_barrels]

## Total enemies currently drawn, across every class layer.
func drawn_enemy_count() -> int:
	var total := 0
	for layer in _enemy_layers:
		total += layer.multimesh.visible_instance_count
	return total

## Kept for the call sites that still say "rebuild the static geometry".
func rebuild_static() -> void:
	refresh_board()

# --- materials -------------------------------------------------------------------------

func _surface_material(tint: Color, metallic: float, roughness: float) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = tint
	material.metallic = metallic
	material.roughness = roughness
	# Double-sided. The corridor is a generated strip mesh and getting the
	# winding right on every face of every mitred corner is fiddly and easy to
	# regress; a back-facing wall renders as a black slot, which is exactly what
	# it did. Culling saves nothing here - it is one mesh of a few hundred
	# triangles - so the robust option is the correct one.
	material.cull_mode = BaseMaterial3D.CULL_DISABLED
	return material

func _transparent_material(tint: Color, alpha: float) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	var colour := tint
	colour.a = alpha
	material.albedo_color = colour
	material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	return material

func _color(key: String) -> Color:
	return Color(str(_theme.get(key, "#ff00ff")))

func _color_of(value: Variant) -> Color:
	return Color(str(value))
