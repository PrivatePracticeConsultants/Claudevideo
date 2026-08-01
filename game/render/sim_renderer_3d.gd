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

## Procedural surface maps, shared by every material that wants one. Generated
## once and outlives the board - see material_library.gd.
var _materials: MaterialLibrary

## True on the Forward+ renderer, false on Compatibility (the web build) and in
## the headless tests. Asked once here rather than at each use, because the
## answer cannot change while the process is running and because it is the same
## question in every case: is there a RenderingDevice, and therefore SSAO, real
## HDR bloom and high-quality shadow filtering?
var _hdr: bool = RenderingServer.get_rendering_device() != null

var camera: Camera3D
var _enemy_layers: Array[MultiMeshInstance3D] = []
var _hp_bars: MultiMeshInstance3D
var _projectiles: MultiMeshInstance3D
var _cells: MultiMeshInstance3D
## One set of part layers per weapon family, so a Railgun does not have to be a
## recoloured Ballistic. Still MultiMesh throughout - this trades a fixed handful
## of extra draw calls (three per family, four families) for silhouettes that
## actually differ, and the count stays flat no matter how many turrets are down,
## which is the rule that matters.
var _turret_bases: Array[MultiMeshInstance3D] = []
var _turret_bodies: Array[MultiMeshInstance3D] = []
var _turret_barrels: Array[MultiMeshInstance3D] = []
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
## Where _fit_camera put the camera. Shake reads and restores this rather than
## accumulating on the live position, which would drift.
var _camera_home := Vector3.ZERO
var _cursor: Node3D
var _cursor_disc: MeshInstance3D
var _cursor_ghost: MeshInstance3D

# Resolved once at setup; parsing colours from strings inside the frame loop is a
# String allocation per colour per frame in the one place we promise not to.
var _enemy_color := Color.WHITE
var _enemy_hurt_color := Color.WHITE
## One base colour per drone class, indexed by enemy type. Class was readable
## from silhouette; now it is readable from colour too, and the colours rhyme
## with the armour classes the wave preview groups by.
var _enemy_class_colors: PackedColorArray = PackedColorArray()
## Last tick's pool bounds, so the diff loops cover slots that were alive then
## and are not now. See _note_wrecks.
var _last_e_bound: int = 0
var _last_p_bound: int = 0
var _champion_scale := 1.5
var _champion_tint := Color.WHITE
var _champion_blend := 0.65
var _bar_color := Color.WHITE
var _bar_back_color := Color.WHITE

# Smoothed turret facing, so barrels swing rather than snap. Purely visual: the
# simulation's aim is instant, and nothing here feeds back into it.
var _barrel_angle: PackedFloat64Array = PackedFloat64Array()
var _reference_radius: float = 1.0

# Reused every frame; Transform3D and Basis are value types.
var _xf := Transform3D()
var _basis := Basis()

## The library is normally passed IN, because it belongs to the session rather
## than to the board - see main.gd, which holds it across levels the same way it
## holds the generated sound. Building one here is the fallback for the tests and
## the dev tools, which make a renderer and nothing else.
func setup(sim: Sim, theme: Dictionary, materials: MaterialLibrary = null) -> void:
	_sim = sim
	_theme = theme
	_world = theme.get("world", {})
	if materials != null:
		_materials = materials
		# The session's library may have been built at a different tier - a level
		# loaded after the player pressed F2. Align it before any material asks.
		_materials.set_detail(_material_detail())
	elif _materials == null:
		_materials = MaterialLibrary.new(_world, _load_families(), _material_detail())
	_enemy_color = _color("enemy")
	_enemy_hurt_color = _color("enemy_hurt")
	var class_colors: Dictionary = _world.get("enemy_class_colors", {}) as Dictionary
	_enemy_class_colors.resize(sim.enemy_type_count())
	for t in sim.enemy_type_count():
		_enemy_class_colors[t] = _color_of(class_colors.get(sim.enemy_id(t), ""))\
			if class_colors.has(sim.enemy_id(t)) else _enemy_color
	_champion_scale = maxf(1.0, float(_world.get("champion_scale", 1.5)))
	_champion_tint = _color_of(_world.get("champion_tint", "#fff3d6"))
	_champion_blend = clampf(float(_world.get("champion_blend", 0.65)), 0.0, 1.0)
	_bar_color = _color("hp_bar")
	_bar_back_color = _color("hp_bar_back")
	_barrel_angle.resize(sim.t_used.size())
	# Height scales relative to the baseline drone, so classes stay in proportion
	# to each other whatever their radii happen to be.
	_reference_radius = maxf(sim.enemy_radius(maxi(sim.enemy_index("walker"), 0)), 0.001)

	_build_environment()
	_lean = _sprite_lean()
	_lean_lift = 0.5 * sin(deg_to_rad(float(_world.get("sprite_lean_degrees", 28.0))))
	_build_scenery()
	_build_cell_layer()
	_build_turret_layers()
	_build_entity_layers()
	_build_tracer_layers()
	_build_effect_layer()
	_build_link_layer()
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
	# A sky rather than a background colour. The board used to sit in a flat void
	# with fog fading into it, and the single biggest thing wrong with how this
	# looked was that there was no horizon to fade INTO - the ground just stopped
	# and the same colour continued upward. A gradient sky costs one material and
	# gives the fog something to be.
	var sky_material := ProceduralSkyMaterial.new()
	sky_material.sky_top_color = _color_of(_world.get("sky_top", "#1b2a3d"))
	sky_material.sky_horizon_color = _color_of(_world.get("sky_horizon", "#4c5a63"))
	sky_material.ground_bottom_color = _color_of(_world.get("sky_ground", "#171c1b"))
	sky_material.ground_horizon_color = _color_of(_world.get("sky_horizon", "#4c5a63"))
	sky_material.sky_energy_multiplier = float(_world.get("sky_energy", 1.0))
	sky_material.ground_energy_multiplier = float(_world.get("sky_energy", 1.0))
	sky_material.sun_angle_max = float(_world.get("sky_sun_size", 12.0))
	sky_material.sun_curve = float(_world.get("sky_sun_curve", 0.12))
	var sky := Sky.new()
	sky.sky_material = sky_material
	env.sky = sky
	env.background_mode = Environment.BG_SKY
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	# Tinted toward the horizon rather than toward the old background: everything
	# outdoors is lit as much by the sky as by the sun, and an ambient term the
	# colour of night on a daylit board is why the unlit faces used to go muddy.
	env.ambient_light_color = _color_of(_world.get("sky_horizon", "#4c5a63")).lightened(0.15)
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
	if _hdr:
		env.ssao_enabled = true
		env.ssao_radius = float(_world.get("ssao_radius", 42.0))
		env.ssao_intensity = float(_world.get("ssao_intensity", 2.4))
	else:
		env.ambient_light_energy *= float(_world.get("ambient_lift_without_ssao", 1.35))

	# Glow is the one setting the two renderers flatly disagree about, so it is
	# tuned twice. Compatibility approximates glow; Forward+ runs a real HDR
	# bloom and actually honours glow_bloom, which adds a constant fraction of
	# EVERY pixel - bright or not - back into the image. The 0.85/0.1 that reads
	# as a gentle lift on the web build washed the entire board to white the
	# first time Forward+ ran it: captured, and the road markings, the treeline
	# and the turrets had all disappeared into it.
	#
	# So on Forward+ the constant term goes to zero and the threshold does the
	# work instead - only pixels that are genuinely overbright (muzzle flashes,
	# tracers, the emissive bands on menders and jammers) bloom, which is what
	# the glow was ever for.
	env.glow_enabled = true
	env.glow_intensity = _lit("glow_strength", 1.15)
	env.glow_bloom = _lit("glow_bloom", 0.25)
	env.glow_hdr_threshold = _lit("glow_threshold", 1.0)
	env.glow_blend_mode = Environment.GLOW_BLEND_MODE_ADDITIVE

	# Depth fog pulls the far end of the corridor back and gives the board scale.
	# Fog the colour of the sky it fades into. It used to be near-black against a
	# near-black background, which worked while there was no horizon and turned
	# every distant thing into a silhouette of nothing the moment there was one -
	# captured with hills and a treeline out there, none of it was visible at all.
	env.fog_enabled = true
	env.fog_light_color = _color_of(_world.get("fog_colour", "#7d8b92"))
	env.fog_density = float(_world.get("fog_density", 0.00007))
	env.fog_sky_affect = float(_world.get("fog_sky_affect", 0.35))

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
		viewport.size_changed.connect(_fit_render_scale)
	_fit_render_scale()

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
	for r in _sim.route_count():
		for i in _sim.route_waypoint_count(r):
			min_x = minf(min_x, _sim.route_waypoint_x(r, i))
			max_x = maxf(max_x, _sim.route_waypoint_x(r, i))
			min_z = minf(min_z, _sim.route_waypoint_y(r, i))
			max_z = maxf(max_z, _sim.route_waypoint_y(r, i))
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
		_camera_home = camera.position
		_fit_shadows(needed)
		return
	# Perspective: pull back far enough that the required extent fits the frustum.
	# tan() is fine here - this is presentation, not simulation.
	var half_fov := deg_to_rad(camera.fov) * 0.5
	var distance := maxf((needed * 0.5) / tan(half_fov), float(cfg.get("distance", 4200.0)) * 0.25)
	camera.position = centre + camera.transform.basis.z * distance
	# Where the camera belongs when nothing is shaking it. Shake is applied as an
	# offset from here every frame, so a leak during a pan cannot leave the camera
	# permanently displaced.
	_camera_home = camera.position
	_fit_shadows(distance + needed)

## Keep the 3D pass at a bounded pixel count however big the window is.
##
## The web export resizes its canvas to the window in CSS pixels TIMES the
## display's pixel ratio, so a 1600x900 browser window on a HiDPI screen is a
## 3200x1800 render target - four times the pixels this scene was designed and
## profiled at, every one of them paying for per-pixel fog, a normal-mapped
## ground and a shadow lookup. That is invisible from the command line and reads
## to a player as "it feels a little laggy".
##
## `scaling_3d_scale` renders the 3D at a fraction and upscales it; the HUD is
## canvas_items and stays crisp at full resolution either way. The budget is a
## pixel COUNT rather than a resolution, so it does the right thing for any
## window shape, and it never scales UP - a small window renders at 1:1.
func _fit_render_scale() -> void:
	var viewport := get_viewport()
	if viewport == null:
		return
	var size := viewport.get_visible_rect().size
	var pixels := maxf(size.x * size.y, 1.0)
	# Explicitly typed: indexing a const Array yields a Variant, and `:=` cannot
	# infer from it. Inferring here failed to COMPILE the whole renderer, and the
	# suite reported "0 failed" for sixteen tests that asserted almost nothing.
	var share: float = QUALITY_PIXEL_SHARE[_quality]
	var budget := maxf(float(_world.get("render_pixel_budget", 1280.0 * 720.0)), 1.0) * share
	# The floor follows the tier rather than being one number for all three.
	#
	# A single 0.6 floor meant that on any screen bigger than 720p EVERY tier
	# rendered the 3D pass at 60% and bilinearly upscaled it - including HIGH -
	# and the authored sprites came back through that looking, in a word,
	# pixelated. The tier is already the place where the player says how much
	# they are willing to pay; the resolution they are shown belongs to it too.
	var floors: Array = _world.get("render_scale_floors", [0.9, 0.75, 0.6])
	var floor_scale := 0.6
	if _quality < floors.size():
		floor_scale = float(floors[_quality])
	floor_scale = clampf(floor_scale, 0.1, 1.0)
	viewport.scaling_3d_mode = Viewport.SCALING_3D_MODE_BILINEAR
	viewport.scaling_3d_scale = clampf(sqrt(budget / pixels), floor_scale, 1.0)

## Quality levels, because I cannot profile the machine this is played on.
##
## Everything measurable was measured and fixed; what is left is fill rate, which
## depends entirely on the GPU in front of the player. Rather than guess at a
## setting that suits every device, the expensive things are bundled into three
## steps the player can cycle with F2 - and the step is remembered, because
## nobody wants to set it again every level.
enum { QUALITY_HIGH, QUALITY_BALANCED, QUALITY_FAST }
const QUALITY_NAMES := ["HIGH", "BALANCED", "FAST"]
## Share of the pixel budget each level renders 3D at.
const QUALITY_PIXEL_SHARE := [1.0, 0.65, 0.4]
## What each step gives up, in the order it costs least to lose. Measured in a
## browser on one board: treeline shadows and drone shadows together are worth
## about a fifth of the frame, the whole shadow pass about a quarter, and the
## scenery about a third. HIGH keeps everything; BALANCED keeps the scenery and
## the board's own shadows but not the treeline's; FAST keeps neither.
var _quality: int = QUALITY_BALANCED
var _scenery_layers: Array[MultiMeshInstance3D] = []

## The fraction the 3D pass is currently rendered at, for the debug readout.
func render_scale() -> float:
	var viewport := get_viewport()
	return 1.0 if viewport == null else viewport.scaling_3d_scale

func quality() -> int: return _quality
func set_quality(level: int) -> void:
	_quality = clampi(level, 0, QUALITY_NAMES.size() - 1)
	apply_quality()
func quality_name() -> String: return QUALITY_NAMES[_quality]

## Cycle to the next level and apply it. Everything it touches is a render
## decision - the simulation never sees this, so a replay is unaffected and two
## players on different settings are playing the identical game.
func cycle_quality() -> int:
	_quality = (_quality + 1) % QUALITY_NAMES.size()
	apply_quality()
	return _quality

func apply_quality() -> void:
	_fit_render_scale()
	# Surface maps are baked INTO materials, and a material already handed to a
	# MeshInstance does not re-read its textures - so changing the tier has to
	# rebuild everything that owns one. Done FIRST, because it replaces the very
	# nodes whose shadow and visibility flags are set below; the other order
	# configured the old nodes and then threw them away, which showed up as FAST
	# still drawing scenery for one tier change.
	#
	# Guarded on the tier actually moving. This is a full board and layer
	# rebuild, and running it on every apply_quality() would make a window
	# resize as expensive as a level load.
	if _materials != null and _materials.set_detail(_material_detail()):
		_rebuild_materials()
	if _sun != null:
		# Shadows are the second most expensive thing after raw fill, and the
		# board still reads without them - the corridor and the turrets are
		# distinguishable by silhouette and colour alone.
		# Measured in a browser on one board: the whole shadow pass is worth about a
		# quarter of the frame and the scenery about a third. BALANCED gives up the
		# cheaper-looking of the two - a board without shadows still reads, because
		# the corridor and the turrets are distinguishable by silhouette and colour;
		# a board without scenery reads as a diagram. FAST gives up both.
		_sun.shadow_enabled = _quality == QUALITY_HIGH
	var tree_shadows := _quality == QUALITY_HIGH and bool(_world.get("tree_shadows", true))
	for node: MultiMeshInstance3D in _scenery_layers:
		node.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON \
			if tree_shadows else GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		# Scenery is the cheapest thing to give up entirely: it is decoration by
		# definition and nothing about play depends on a treeline.
		node.visible = _quality != QUALITY_FAST
	for layer in _enemy_layers:
		layer.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON \
			if (_quality == QUALITY_HIGH and bool(_world.get("drone_shadows", true))) \
			else GeometryInstance3D.SHADOW_CASTING_SETTING_OFF

## Throw away every node that owns a generated material and build it again.
##
## Only the detail tier calls this. The layers are rebuilt rather than patched
## because a MultiMesh's material lives on its mesh, and the meshes are welded
## per family at build time - reaching in to swap a texture on each would mean
## keeping a second index of every surface, which is exactly the kind of cached
## thing that goes stale silently.
##
## The old nodes have to be removed, not just forgotten: _build_turret_layers
## and _build_entity_layers clear their arrays and add_child() fresh layers, so
## dropping the references alone would leave the previous set on the tree,
## drawing stale instances forever.
func _rebuild_materials() -> void:
	for group in [_turret_bases, _turret_bodies, _turret_barrels, _enemy_layers]:
		for layer: MultiMeshInstance3D in group:
			remove_child(layer)
			layer.queue_free()
	# _build_scenery() frees and replaces the whole _scenery subtree itself.
	_build_scenery()
	_build_turret_layers()
	_build_entity_layers()
	refresh_board()

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
	_tall_scenery.clear()

	# Far larger than the board: at a shallow camera angle the horizon is a long
	# way out, and a visible ground edge reads as a rendering bug.
	var ground := MeshInstance3D.new()
	ground.mesh = _ground_mesh()
	# Vertex colours carry the broad mottling; the base colour has to be white or it
	# would multiply the variation away.
	#
	# ...and the generated "ground" family carries the fine detail. Vertex colour
	# on a 128-cell grid varies every ~195 units, which at this camera distance is
	# cloud rather than surface. The texture and its normal map are what make the
	# ground read as something with a texture instead of as a tinted polygon.
	#
	# The ground's UVs are world-space and already scaled by ground_tile, so this
	# is the one family whose uv_scale must stay 1.0 - the mesh has done the
	# tiling. Everything else tiles through the material.
	var ground_material := _surface_material(Color.WHITE,
		float(_world.get("ground_metallic", 0.0)), float(_world.get("ground_roughness", 0.95)),
		"ground")
	ground_material.vertex_color_use_as_albedo = true
	ground.material_override = ground_material
	_scenery.add_child(ground)

	# Graded earth either side of the road, wider than the walls. Without it the
	# corridor sits on the terrain like a sticker rather than being cut into it.
	var verge := MeshInstance3D.new()
	verge.mesh = _verge_mesh()
	verge.material_override = _surface_material(_color_of(_world.get("verge", "#2a2f26")),
		0.0, float(_world.get("verge_roughness", 0.98)), "verge")
	_scenery.add_child(verge)

	var road := MeshInstance3D.new()
	road.mesh = _corridor_mesh()
	# The road family's noise is stretched along X against Y, so the aggregate
	# reads as dragged in the direction of travel rather than as even gravel.
	road.material_override = _surface_material(_color("path"),
		float(_world.get("path_metallic", 0.15)), float(_world.get("path_roughness", 0.8)),
		"road")
	_scenery.add_child(road)

	# Lane markings down the middle. Cheap, and it is most of what makes a grey
	# strip read as a road rather than as a wall lying down.
	var markings := MeshInstance3D.new()
	markings.mesh = _marking_mesh()
	var marking_material := _surface_material(_color_of(_world.get("road_line", "#b9bcae")),
		0.0, 0.7)
	markings.material_override = marking_material
	_scenery.add_child(markings)

	var walls := MeshInstance3D.new()
	walls.mesh = _wall_mesh()
	# Panel seams, which is the whole reason walls do not read as cliffs: the
	# noise alone would make them stone whatever colour they were given.
	walls.material_override = _surface_material(_color_of(_world.get("wall", "#39424f")),
		float(_world.get("wall_metallic", 0.55)), float(_world.get("wall_roughness", 0.42)),
		"wall")
	_scenery.add_child(walls)

	# The station, on outpost boards. It is the thing being defended, so it is the
	# one piece of scenery that is not decoration: without it the eight lanes
	# converge on a patch of empty ground and there is nothing on screen to
	# explain what losing hull means.
	if _sim.is_outpost():
		var station := MeshInstance3D.new()
		station.mesh = _station_mesh()
		station.position = to_world(_sim.base_x(), _sim.base_y(), 0.0)
		# Not through _shadowed(), which is typed for the instanced layers; this
		# is the one plain MeshInstance3D on the board.
		station.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON
		_scenery.add_child(station)

	var props := _prop_layer()
	if props != null:
		_scenery.add_child(props)

	# Everything from here out is landscape rather than board: it is beyond the
	# band anything can be built in, it never moves, and none of it is a game
	# object. It exists because a corridor with nothing around it reads as a
	# diagram of a corridor.
	#
	# There is no distant ridge line, and there was one for an afternoon. The
	# camera sits at -38 degrees with a 26 degree field of view, so the TOP of the
	# frame still points 25 degrees below horizontal: the horizon is not merely
	# off-screen at this framing, it is geometrically unreachable, and a ring of
	# mountains out there was four hundred vertices no player will ever see.
	# Showing it would mean pitching the camera to about -13, which is nearly
	# side-on and not a tower defence board any more. Everything below is therefore
	# on the ground plane, which is the only thing in frame.
	# Collected as they are added so the quality switch can reach them: these are
	# the layers it is cheapest to give up, being decoration by definition.
	_scenery_layers.clear()
	for layer in _tree_layers():
		_scenery.add_child(layer)
		_scenery_layers.append(layer)
	var rocks := _rock_layer()
	if rocks != null:
		_scenery.add_child(rocks)
		_scenery_layers.append(rocks)
	apply_quality()

## Ground as a mottled grid rather than one flat quad.
##
## A single plane under a single directional light is a slab of constant colour,
## and no amount of tonemapping makes that read as terrain. This lays down a
## coarse grid and varies each vertex's colour from a hash of its position, which
## costs one mesh and gives the eye something to attach scale to.
##
## Hashed rather than random on purpose: the renderer has no business touching
## the simulation's RNG, and a board that looked different every time you
## restarted it would be its own kind of wrong.
func _ground_mesh() -> ArrayMesh:
	var width := _sim.bounds_width()
	var depth := _sim.bounds_height()
	# Enough overshoot that the horizon never shows an edge, but not so much that
	# the whole grid is spent on ground nobody looks at: at 3x the board the
	# mottling cells were ~900 units across and simply invisible.
	var margin := maxf(width, depth) * float(_world.get("ground_overshoot", 1.6))
	var cells := int(_world.get("ground_cells", 96))
	var variation := float(_world.get("ground_variation", 0.16))
	# Three bands, blended by where a vertex is rather than painted on: bare
	# ground beside the road where everything has been driven over, grass beyond
	# it, and rock on anything that has climbed. One flat colour with hashed shade
	# is what the ground used to be, and it read as a sheet of paper however well
	# it was lit - a surface tells you what it is by CHANGING, and this is the
	# cheapest honest change available.
	var verge_soil := _color_of(_world.get("ground_verge", "#4a4436"))
	var grass := _color_of(_world.get("ground", "#3d4739"))
	var rock := _color_of(_world.get("ground_rock", "#585b55"))
	var soil_reach := _sim.build_max_distance() + float(_world.get("ground_soil_reach", 200.0))
	var rock_from := float(_world.get("ground_rock_height", 55.0))

	var vertices := PackedVector3Array()
	var colours := PackedColorArray()
	var uvs := PackedVector2Array()
	var indices := PackedInt32Array()
	var tile := maxf(float(_world.get("ground_tile", 900.0)), 1.0)
	var step_x := (width + margin * 2.0) / float(cells)
	var step_z := (depth + margin * 2.0) / float(cells)
	for row in cells + 1:
		for col in cells + 1:
			var x := -margin + float(col) * step_x
			var z := -margin + float(row) * step_z
			var y := _ground_height(x, z)
			vertices.append(Vector3(x, y, z))
			# World-space UVs, so the surface detail is the same size everywhere and
			# does not stretch with the board.
			uvs.append(Vector2(x / tile, z / tile))
			var to_road := _road_distance(x, z, soil_reach)
			var tint := verge_soil.lerp(grass, clampf(to_road / maxf(soil_reach, 1.0), 0.0, 1.0))
			# Height, not slope: slope needs neighbours and this is one pass. The
			# relief only rises away from the road anyway, so height is standing in
			# for exactly the thing slope would have told us.
			tint = tint.lerp(rock, clampf(absf(y) / maxf(rock_from, 1.0), 0.0, 1.0) * 0.75)
			var shade := 1.0 + (_hash_unit(col, row) - 0.5) * 2.0 * variation
			colours.append(Color(tint.r * shade, tint.g * shade, tint.b * shade))
	for row in cells:
		for col in cells:
			var top_left := row * (cells + 1) + col
			var top_right := top_left + 1
			var bottom_left := top_left + cells + 1
			var bottom_right := bottom_left + 1
			indices.append_array([top_left, bottom_left, top_right,
				top_right, bottom_left, bottom_right])

	var arrays := []
	arrays.resize(Mesh.ARRAY_MAX)
	arrays[Mesh.ARRAY_VERTEX] = vertices
	arrays[Mesh.ARRAY_COLOR] = colours
	arrays[Mesh.ARRAY_TEX_UV] = uvs
	arrays[Mesh.ARRAY_INDEX] = indices
	var mesh := ArrayMesh.new()
	mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
	# Normals, which this mesh shipped without.
	#
	# Not a refinement - a bug. Without an ARRAY_NORMAL the ground was handed to
	# the lighting with no surface direction at all, so every hill on it was shaded
	# identically to the flat ground beside it. The relief had been there for
	# months and could not be seen, and no amount of colour work was ever going to
	# fix that. Tangents come with them, because the detail normal map below is
	# meaningless without a tangent basis to apply it in.
	var tool := SurfaceTool.new()
	tool.create_from(mesh, 0)
	tool.generate_normals()
	tool.generate_tangents()
	mesh.clear_surfaces()
	tool.commit(mesh)
	return mesh

## How high the terrain sits at a point.
##
## Flat everywhere a turret could ever stand, and only then allowed to roll. The
## simulation is 2D and every placement rule is a distance in the ground plane,
## so relief under the playable band would put turrets on slopes the rules know
## nothing about - floating on the high side, sunk on the low. Beyond that band
## it costs nothing and it is the difference between terrain and a tabletop.
func _ground_height(x: float, z: float) -> float:
	var flat := _sim.build_max_distance() + float(_world.get("relief_clearance", 120.0))
	# Most of this mesh is horizon, well outside the board. distance_to_path walks
	# every segment, and calling it for all ~9,400 vertices is a visible hitch on
	# a level load and worse on a phone - so anything outside the board's bounds
	# by more than the flat band is answered without asking.
	var beyond := 0.0
	if x < -flat or z < -flat or x > _sim.bounds_width() + flat \
			or z > _sim.bounds_height() + flat:
		beyond = flat
	else:
		beyond = _sim.distance_to_path(x, z) - flat
	if beyond <= 0.0:
		return 0.0
	var ramp := minf(beyond / float(_world.get("relief_ramp", 700.0)), 1.0)
	var amplitude := float(_world.get("relief_height", 90.0))
	# Two octaves at different scales, so it reads as landform rather than as a
	# regular ripple.
	var coarse := _hash_unit(int(x / 620.0), int(z / 620.0)) - 0.5
	var fine := _hash_unit(int(x / 210.0) + 91, int(z / 210.0) + 47) - 0.5
	return (coarse * 0.75 + fine * 0.25) * 2.0 * amplitude * ramp

## How far the ground mesh reaches past the board on every side. Trees, boulders
## and the far ridge are all placed against this, so they meet the ground rather
## than floating past its edge.
func _ground_margin() -> float:
	return maxf(_sim.bounds_width(), _sim.bounds_height()) \
		* float(_world.get("ground_overshoot", 1.6))

## Distance to the nearest road, with the same early-out _ground_height uses.
##
## distance_to_path walks every segment of every route, and the landscape asks
## this question tens of thousands of times at level load. Anything comfortably
## outside the board is answered without asking, because the only thing the
## answer is used for out there is "is this clear of the road", and it always is.
func _road_distance(x: float, z: float, far_enough: float) -> float:
	if x < -far_enough or z < -far_enough or x > _sim.bounds_width() + far_enough \
			or z > _sim.bounds_height() + far_enough:
		return far_enough
	return _sim.distance_to_path(x, z)

## The camera's view direction flattened onto the ground, as a unit 2D vector.
## Landscape uses it to answer "is this behind the board", which is the only
## question that decides whether a two-hundred-unit tree is scenery or an
## obstruction.
func _view_axis() -> Vector2:
	var forward := -camera.transform.basis.z
	var flat := Vector2(forward.x, forward.z)
	if flat.length() < 0.0001:
		return Vector2(0.0, 1.0)
	return flat.normalized()

## How far along the view axis anything tall is allowed to start.
##
## Measured from the board's CENTRE and not its nearest corner. The corner was the
## obvious answer and it is wrong: on a board four thousand units across, the
## nearest corner is most of the frame away from the far one, so "not in front of
## the corner" still left room for a hundred-and-ninety-unit conifer in the
## bottom-left of the screen with the board visible through the gaps. Behind the
## centre line clears the whole foreground, and the foreground is the play area
## anyway.
func _board_near_limit() -> float:
	var view := _view_axis()
	var centre := Vector2(_sim.bounds_width() * 0.5, _sim.bounds_height() * 0.5)
	return centre.x * view.x + centre.y * view.y \
		+ maxf(_sim.bounds_width(), _sim.bounds_height()) \
		* float(_world.get("scenery_setback", 0.15))

## Trunks and canopies, as two instanced layers.
##
## Clustered rather than scattered: a coarse lattice decides which patches of
## ground are wooded at all, and trees are only placed inside those. Uniform
## scatter reads as an orchard, and an orchard is not what anything looks like.
##
## Nothing grows inside the band a turret can be built in, or within a margin of
## it. A tree standing where you are trying to read whether a cell is buildable is
## not atmosphere, it is an obstruction - and the simulation has no idea it is
## there, so it must never be able to hide anything the rules care about.
## Where the tall scenery ended up.
##
## Kept because a MultiMesh's instance buffer lives in the RenderingServer and
## cannot be read back in a headless run - `get_instance_transform` answers with
## zeroes there. Without this the rule that nothing tall stands in front of the
## board is untestable anywhere it could be run automatically, which for a rule
## about occlusion is exactly backwards.
var _tall_scenery: PackedVector3Array = PackedVector3Array()

func tall_scenery() -> PackedVector3Array: return _tall_scenery

func _tree_layers() -> Array:
	var spacing := float(_world.get("tree_spacing", 230.0))
	var clearance := _sim.build_max_distance() + float(_world.get("tree_clearance", 130.0))
	var forest_cell := float(_world.get("forest_cell", 1100.0))
	var forest_share := float(_world.get("forest_share", 0.42))
	var density := float(_world.get("tree_density", 0.62))
	var limit := int(_world.get("tree_limit", 4200))
	var margin := _ground_margin()

	# Nothing in front of the board, ever.
	#
	# "Behind" is measured along the camera's own view axis rather than by a
	# compass direction, and the near limit is the nearest of the board's four
	# corners - so a tree can stand level with the closest thing the player is
	# looking at, and never between them and it. Captured without this, a hundred
	# and ninety unit conifers stood in the foreground with the board visible
	# through the gaps: correct placement, and unplayable.
	var near_limit := _board_near_limit()
	var view := _view_axis()

	var spots := PackedVector3Array()
	var cols := int((_sim.bounds_width() + margin * 2.0) / spacing)
	var rows := int((_sim.bounds_height() + margin * 2.0) / spacing)
	for row in rows:
		for col in cols:
			if spots.size() >= limit:
				break
			var x := -margin + (float(col) + 0.5) * spacing \
				+ (_hash_unit(col, row) - 0.5) * spacing * 0.85
			var z := -margin + (float(row) + 0.5) * spacing \
				+ (_hash_unit(row + 613, col + 149) - 0.5) * spacing * 0.85
			if x * view.x + z * view.y < near_limit:
				continue
			# Is this patch of ground wooded at all?
			if _hash_unit(int(floor(x / forest_cell)) + 401,
					int(floor(z / forest_cell)) + 809) > forest_share:
				continue
			if _hash_unit(col + 29, row + 71) > density:
				continue
			if _road_distance(x, z, clearance) < clearance:
				continue
			spots.append(Vector3(x, _ground_height(x, z), z))
	if spots.is_empty():
		return []
	_tall_scenery.append_array(spots)

	var trunk_mesh := CylinderMesh.new()
	trunk_mesh.top_radius = 0.34
	trunk_mesh.bottom_radius = 0.5
	trunk_mesh.height = 1.0
	trunk_mesh.radial_segments = 5
	trunk_mesh.rings = 0
	# Bark: the same generator as everything else, with the lattice counts
	# swapped so the noise stretches UP the trunk instead of around it. That one
	# asymmetry is the entire difference between bark and gravel.
	var trunk_material := _surface_material(Color.WHITE, 0.0, 1.0, "bark")
	trunk_material.vertex_color_use_as_albedo = true
	trunk_mesh.material = trunk_material

	# A cone. Conifers read at distance in a way a rounded canopy does not, and at
	# this camera pitch distance is where almost all of them are.
	var canopy_mesh := CylinderMesh.new()
	canopy_mesh.top_radius = 0.0
	canopy_mesh.bottom_radius = 0.5
	canopy_mesh.height = 1.0
	canopy_mesh.radial_segments = 6
	canopy_mesh.rings = 1
	var canopy_material := _surface_material(Color.WHITE, 0.0,
		float(_world.get("canopy_roughness", 0.98)), "canopy")
	canopy_material.vertex_color_use_as_albedo = true
	canopy_mesh.material = canopy_material

	# ...and a rounded one for the broadleaves. A forest of one silhouette reads as
	# a texture rather than as trees; two is enough for the eye to stop counting.
	var broad_mesh := SphereMesh.new()
	broad_mesh.radius = 0.5
	broad_mesh.height = 1.0
	broad_mesh.radial_segments = 7
	broad_mesh.rings = 4
	broad_mesh.material = canopy_material

	var trunks := _instanced(trunk_mesh, spots.size())
	var canopies := _instanced(canopy_mesh, spots.size())
	var broadleaves := _instanced(broad_mesh, spots.size())
	var shadows := GeometryInstance3D.SHADOW_CASTING_SETTING_ON \
		if bool(_world.get("tree_shadows", true)) \
		else GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	trunks.cast_shadow = shadows
	canopies.cast_shadow = shadows
	broadleaves.cast_shadow = shadows
	var broad_share := float(_world.get("broadleaf_share", 0.35))
	var conifer_count := 0
	var broad_count := 0

	var height := float(_world.get("tree_height", 190.0))
	var girth := float(_world.get("tree_girth", 26.0))
	var bark := _color_of(_world.get("tree_bark", "#3a2f26"))
	var leaf := _color_of(_world.get("tree_leaf", "#2f4a2c"))
	var leaf_far := _color_of(_world.get("tree_leaf_far", "#3c5740"))
	for i in spots.size():
		var spot := spots[i]
		var seed_a := _hash_unit(int(spot.x) + 13, int(spot.z) + 91)
		var seed_b := _hash_unit(int(spot.z) + 57, int(spot.x) + 23)
		var tall := height * (0.6 + seed_a * 0.85)
		var wide := girth * (0.7 + seed_b * 0.7)
		var spin := seed_b * TAU
		var trunk_height := tall * 0.34
		trunks.multimesh.set_instance_transform(i, Transform3D(
			Basis(Vector3.UP, spin).scaled(Vector3(wide * 0.5, trunk_height, wide * 0.5)),
			Vector3(spot.x, spot.y + trunk_height * 0.5, spot.z)))
		trunks.multimesh.set_instance_color(i,
			bark.lerp(Color.WHITE, seed_a * 0.12))
		# Two greens mixed by hash, so a hillside is not one flat colour.
		var tint := leaf.lerp(leaf_far, seed_b)
		if _hash_unit(int(spot.x) + 401, int(spot.z) + 137) < broad_share:
			# Broadleaf: a squatter, wider crown sitting lower on its trunk.
			broadleaves.multimesh.set_instance_transform(broad_count, Transform3D(
				Basis(Vector3.UP, spin).scaled(
					Vector3(wide * 1.55, tall * 0.58, wide * 1.55)),
				Vector3(spot.x, spot.y + trunk_height + tall * 0.26, spot.z)))
			broadleaves.multimesh.set_instance_color(broad_count, tint)
			broad_count += 1
		else:
			canopies.multimesh.set_instance_transform(conifer_count, Transform3D(
				Basis(Vector3.UP, spin).scaled(Vector3(wide, tall * 0.82, wide)),
				Vector3(spot.x, spot.y + trunk_height + tall * 0.41, spot.z)))
			canopies.multimesh.set_instance_color(conifer_count, tint)
			conifer_count += 1
	trunks.multimesh.visible_instance_count = spots.size()
	canopies.multimesh.visible_instance_count = conifer_count
	broadleaves.multimesh.visible_instance_count = broad_count
	return [trunks, canopies, broadleaves]

## Boulders, further out than the debris beside the road and much larger.
##
## Between the props and the trees they are what stops the middle distance being
## an empty green sheet between a road and a treeline.
func _rock_layer() -> MultiMeshInstance3D:
	var spacing := float(_world.get("rock_spacing", 520.0))
	var clearance := _sim.build_max_distance() + float(_world.get("rock_clearance", 90.0))
	var density := float(_world.get("rock_density", 0.3))
	var limit := int(_world.get("rock_limit", 900))
	var margin := _ground_margin() * 0.6
	var size := float(_world.get("rock_size", 62.0))
	var tint := _color_of(_world.get("rock_colour", "#4a4d47"))

	var near_limit := _board_near_limit()
	var view := _view_axis()
	var spots := PackedVector3Array()
	var cols := int((_sim.bounds_width() + margin * 2.0) / spacing)
	var rows := int((_sim.bounds_height() + margin * 2.0) / spacing)
	for row in rows:
		for col in cols:
			if spots.size() >= limit:
				break
			if _hash_unit(col + 211, row + 307) > density:
				continue
			var x := -margin + (float(col) + 0.5) * spacing \
				+ (_hash_unit(col + 5, row + 9) - 0.5) * spacing * 0.7
			var z := -margin + (float(row) + 0.5) * spacing \
				+ (_hash_unit(row + 83, col + 41) - 0.5) * spacing * 0.7
			# Squat enough to be harmless in the foreground, but they still read
			# better ranged behind the board than scattered around the viewer.
			if x * view.x + z * view.y < near_limit:
				continue
			if _road_distance(x, z, clearance) < clearance:
				continue
			spots.append(Vector3(x, _ground_height(x, z), z))
	if spots.is_empty():
		return null
	_tall_scenery.append_array(spots)

	# Few enough segments that it is faceted, which is what makes it read as rock
	# rather than as a ball.
	var mesh := SphereMesh.new()
	mesh.radius = 0.5
	mesh.height = 1.0
	mesh.radial_segments = 6
	mesh.rings = 3
	var material := _surface_material(Color.WHITE, 0.0,
		float(_world.get("rock_roughness", 0.95)), "rock")
	material.vertex_color_use_as_albedo = true
	mesh.material = material
	var layer := _instanced(mesh, spots.size())
	for i in spots.size():
		var spot := spots[i]
		var a := _hash_unit(int(spot.x) + 71, int(spot.z) + 13)
		var b := _hash_unit(int(spot.z) + 37, int(spot.x) + 61)
		var scale := size * (0.5 + a * 1.1)
		# Squashed and sunk, so they sit in the ground instead of resting on it.
		var flat := scale * (0.42 + b * 0.45)
		layer.multimesh.set_instance_transform(i, Transform3D(
			Basis(Vector3.UP, b * TAU).scaled(Vector3(scale, flat, scale * (0.7 + a * 0.5))),
			Vector3(spot.x, spot.y + flat * 0.22, spot.z)))
		layer.multimesh.set_instance_color(i, tint.lerp(Color.WHITE, a * 0.28))
	layer.multimesh.visible_instance_count = spots.size()
	return layer

## Deterministic 0..1 noise from two integers. Not the simulation's Rng - this is
## presentation, and pulling on the seeded stream from here would make what the
## board looks like a function of what the board does.
func _hash_unit(a: int, b: int) -> float:
	var h := (a * 73856093) ^ (b * 19349663)
	h = (h ^ (h >> 13)) * 1274126177
	return float((h ^ (h >> 16)) & 0xFFFF) / 65535.0

## A flat apron either side of the corridor, sitting just above the ground so it
## reads as graded rather than as a decal.
func _verge_mesh() -> ArrayMesh:
	var half := float(_world.get("corridor_width", 76.0)) * 0.5
	var reach := half + float(_world.get("wall_width", 16.0)) + float(_world.get("verge_width", 54.0))
	var lift := float(_world.get("verge_height", 1.5))
	var builder := _StripBuilder.new(_sim)
	for r in _sim.route_count():
		builder.use_route(r)
		builder.strip(-reach, lift, reach, lift)
	return builder.commit()

## The centre line, as a thin raised strip down the middle of the road.
func _marking_mesh() -> ArrayMesh:
	var width := float(_world.get("road_line_width", 4.0))
	var surface := float(_world.get("corridor_height", 9.0)) + 0.4
	var builder := _StripBuilder.new(_sim)
	for r in _sim.route_count():
		builder.use_route(r)
		builder.strip(-width, surface, width, surface)
	return builder.commit()

## Scattered debris off the road: one instanced layer, one draw call, placed from
## the same positional hash the ground uses. It exists to give the terrain a
## sense of scale - a board with nothing on it reads as a diagram however well it
## is lit.
## The outpost: a tiered drum with a ring of buttresses around it.
##
## Built from the same welded primitives as everything else, and deliberately
## LOW and WIDE rather than tall. Anything tall in the middle of the board would
## occlude the lanes converging on it from the camera's side, and the one thing
## this mesh must never do is hide the drones walking towards it.
func _station_mesh() -> ArrayMesh:
	var radius := maxf(_sim.outpost_core_radius(), 1.0)
	var height := radius * float(_world.get("station_height_share", 0.42))
	var parts := []
	var drum := CylinderMesh.new()
	drum.top_radius = radius * 0.78
	drum.bottom_radius = radius
	drum.height = height
	drum.radial_segments = 16
	parts.append([drum, _at(0.0, height * 0.5, 0.0)])
	var cap := CylinderMesh.new()
	cap.top_radius = radius * 0.34
	cap.bottom_radius = radius * 0.62
	cap.height = height * 0.8
	cap.radial_segments = 12
	parts.append([cap, _at(0.0, height * 1.3, 0.0)])
	# Four buttresses on the axes. Not eight-to-match-the-gates on purpose: a
	# silhouette that mirrors the lane layout makes the two read as one shape, and
	# the lanes are the thing that has to stay legible.
	var arm := BoxMesh.new()
	arm.size = Vector3(radius * 0.3, height * 0.7, radius * 1.5)
	for i in 4:
		var offset := radius * 0.62
		var turn := float(i) * PI * 0.5
		parts.append([arm, _at(0.0, height * 0.35, 0.0, Vector3.ONE, Vector3(0.0, turn, 0.0))
			* Transform3D(Basis(), Vector3(0.0, 0.0, offset))])
	var mesh := _merged(parts)
	var material := _surface_material(_color_of(_world.get("station", "#8d97a8")),
		float(_world.get("station_metallic", 0.6)), float(_world.get("station_roughness", 0.4)),
		"wall")
	_skin(mesh, material)
	return mesh

func _prop_layer() -> MultiMeshInstance3D:
	var spacing := float(_world.get("prop_spacing", 340.0))
	var clearance := _sim.build_min_distance() + float(_world.get("prop_clearance", 30.0))
	var size := float(_world.get("prop_size", 26.0))
	var tint := _color_of(_world.get("prop_colour", "#333a30"))
	var cols := int(_sim.bounds_width() / spacing)
	var rows := int(_sim.bounds_height() / spacing)
	if cols <= 0 or rows <= 0:
		return null

	var placed := PackedVector3Array()
	for row in rows:
		for col in cols:
			# Jittered off the lattice, or it reads as a grid of crates.
			var jx := (_hash_unit(col, row) - 0.5) * spacing * 0.8
			var jz := (_hash_unit(row + 977, col + 331) - 0.5) * spacing * 0.8
			var x := (float(col) + 0.5) * spacing + jx
			var z := (float(row) + 0.5) * spacing + jz
			var to_road := _sim.distance_to_path(x, z)
			if to_road < clearance:
				continue  # nothing standing where a turret or the road belongs
			# ...and nothing way out in the dark either. Props are a scale cue for
			# the ground you are playing on; scattered to the horizon they just
			# read as debris floating in a void.
			if to_road > float(_world.get("prop_reach", 620.0)):
				continue
			if _hash_unit(col + 17, row + 53) > float(_world.get("prop_density", 0.45)):
				continue
			placed.append(Vector3(x, 0.0, z))
	if placed.is_empty():
		return null

	var block := BoxMesh.new()
	block.size = Vector3.ONE
	var material := _surface_material(Color.WHITE, 0.0,
		float(_world.get("prop_roughness", 0.95)), "prop")
	material.vertex_color_use_as_albedo = true
	block.material = material
	var layer := _instanced(block, placed.size())
	var mm := layer.multimesh
	for i in placed.size():
		var spot := placed[i]
		var scale := size * (0.55 + _hash_unit(int(spot.x), int(spot.z)))
		var tall := scale * (0.4 + _hash_unit(int(spot.z), int(spot.x)) * 0.9)
		var spin := _hash_unit(int(spot.z) + 7, int(spot.x) + 11) * PI
		mm.set_instance_transform(i, Transform3D(
			Basis(Vector3.UP, spin).scaled(Vector3(scale, tall, scale)),
			Vector3(spot.x, tall * 0.5, spot.z)))
		var shade := 0.82 + _hash_unit(int(spot.x) + 3, int(spot.z) + 5) * 0.36
		mm.set_instance_color(i, Color(tint.r * shade, tint.g * shade, tint.b * shade))
	mm.visible_instance_count = placed.size()
	return layer

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
	for r in _sim.route_count():
		builder.use_route(r)
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
	for r in _sim.route_count():
		builder.use_route(r)
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

	## Which of the board's roads this ribbon follows. A board with one road builds
	## exactly what it always did; a board with a fork builds one ribbon per route
	## into the same mesh, so two roads still cost one draw call.
	var _route: int = 0

	func _init(sim: Sim, route: int = 0) -> void:
		_sim = sim
		use_route(route)

	## Point the builder at another of the board's roads, keeping everything
	## accumulated so far. One builder walked across every route produces one mesh,
	## so a board with a fork still draws its corridor in a single call.
	func use_route(route: int) -> void:
		var sim := _sim
		_route = route
		var count := sim.route_waypoint_count(route)
		_nx.resize(count)
		_nz.resize(count)
		for i in count:
			var dx := 0.0
			var dz := 0.0
			if i > 0:
				dx += sim.route_waypoint_x(route, i) - sim.route_waypoint_x(route, i - 1)
				dz += sim.route_waypoint_y(route, i) - sim.route_waypoint_y(route, i - 1)
			if i < count - 1:
				dx += sim.route_waypoint_x(route, i + 1) - sim.route_waypoint_x(route, i)
				dz += sim.route_waypoint_y(route, i + 1) - sim.route_waypoint_y(route, i)
			var length := sqrt(dx * dx + dz * dz)
			if length <= 0.0:
				length = 1.0
			_nx[i] = dz / length
			_nz[i] = -dx / length

	func strip(offset_a: float, height_a: float, offset_b: float, height_b: float,
			flip: bool = false) -> void:
		var count := _sim.route_waypoint_count(_route)
		var base := _vertices.size()
		for i in count:
			var px := _sim.route_waypoint_x(_route, i)
			var pz := _sim.route_waypoint_y(_route, i)
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

## What each family is built out of. Read from behaviour where possible, but the
## shapes themselves are authored: a mount, a housing and a muzzle, proportioned
## so the four read apart at a glance and from directly above.
##
##   Ballistic  hexagonal mount, blocky housing, one long slim barrel
##   Cannon     wide mount, tapered housing, short fat bore angled up
##   Suppressor squat mount, drum housing, a coil ring instead of a barrel
##   Railgun    low sled, narrow housing, a very long thin rail
func _build_turret_layers() -> void:
	var limit := _sim.t_used.size()
	var radius := float(_world.get("platform_radius", 18.0))
	var pad := float(_world.get("pad_height", 12.0))
	var height := float(_world.get("platform_height", 48.0))
	_turret_bases.clear()
	_turret_bodies.clear()
	_turret_barrels.clear()

	# Sprites only if EVERY family has one. A board that mixed authored turrets
	# with generated ones would read as two games; and the three-layer mount /
	# housing / barrel split only makes sense for the generated meshes, so this
	# is all-or-nothing by construction rather than by preference.
	_sprite_turrets = true
	for family in _sim.blueprint_count():
		if _sprite(SPRITE_TURRET_DIR, _sim.blueprint_name(family)) == null:
			_sprite_turrets = false
			break
	# Split art - a pinned base and a head that turns - is preferred over the
	# whole-sprite rotation, and it is all-or-nothing for the same reason the
	# sprites themselves are. Rotating the WHOLE picture was the bug a player
	# actually reported: a turret aiming down-screen drew its entire art upside
	# down, base in the air, so every shot read as leaving the back of the mount
	# whatever the tracer did. Only the gun should ever turn.
	_sprite_turret_split = _sprite_turrets
	if _sprite_turrets:
		for family in _sim.blueprint_count():
			var id := _sim.blueprint_name(family)
			if _sprite(SPRITE_TURRET_DIR, id + "_base") == null \
					or _sprite(SPRITE_TURRET_DIR, id + "_head") == null:
				_sprite_turret_split = false
				break

	_turret_sprite_factor.resize(_sim.blueprint_count())
	for family in _sim.blueprint_count():
		var id := _sim.blueprint_name(family)
		_turret_sprite_factor[family] = 1.0
		if _sprite_turret_split:
			# The split halves live on a larger canvas, re-centred so the head's
			# pivot is the canvas centre. Drawing them at the single art's span
			# would shrink the turret by that ratio, so the ratio rides along -
			# read off the textures rather than stored anywhere it could go stale.
			var whole := _sprite(SPRITE_TURRET_DIR, id)
			var head := _sprite(SPRITE_TURRET_DIR, id + "_head")
			_turret_sprite_factor[family] = float(head.get_width()) / float(whole.get_width())
			var base_plane := _sprite_plane()
			base_plane.material = _sprite_material(_sprite(SPRITE_TURRET_DIR, id + "_base"))
			_turret_bases.append(_instanced(base_plane, limit))
			add_child(_turret_bases[_turret_bases.size() - 1])
			var head_plane := _sprite_plane()
			head_plane.material = _sprite_material(head)
			_turret_bodies.append(_instanced(head_plane, limit))
			add_child(_turret_bodies[_turret_bodies.size() - 1])
			continue
		if _sprite_turrets:
			# One layer instead of three. The sprite is the whole emplacement, so
			# the mount and the muzzle are inside the picture and the thing that
			# turns to track a target is the entire assembly.
			var plane := _sprite_plane()
			plane.material = _sprite_material(_sprite(SPRITE_TURRET_DIR, id))
			_turret_bodies.append(_instanced(plane, limit))
			add_child(_turret_bodies[_turret_bodies.size() - 1])
			continue
		# The board's own furniture: these three earn a shadow, because a turret
		# without one looks pasted onto the ground rather than standing on it.
		_turret_bases.append(_shadowed(_add_layer(_mount_mesh(id, radius, pad), limit)))
		_turret_bodies.append(_shadowed(_add_layer(_housing_mesh(id, radius, height), limit)))
		_turret_barrels.append(_shadowed(_add_layer(_muzzle_mesh(id), limit)))

## Assign a material to either kind of mesh. A PrimitiveMesh takes it as a
## property; an ArrayMesh - which everything welded by _merged() is - only takes
## it per surface, and assigning the property it does not have fails at runtime.
## Found by screenshot: fifty-one freshly assembled turrets rendering as nothing
## at all, because every mesh assignment in this file predated _merged().
func _skin(mesh: Mesh, material: Material) -> void:
	if mesh is PrimitiveMesh:
		(mesh as PrimitiveMesh).material = material
	else:
		for surface in mesh.get_surface_count():
			(mesh as ArrayMesh).surface_set_material(surface, material)

## Opt a layer back in to casting shadows. See _instanced for why the default is
## the other way round.
func _shadowed(node: MultiMeshInstance3D) -> MultiMeshInstance3D:
	node.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON
	return node

func _add_layer(mesh: Mesh, limit: int) -> MultiMeshInstance3D:
	# Turrets get their own surface, not the drones': machined metal, so the sun
	# actually picks out the assemblies the meshes now have. Values live in
	# theme.json like every other look decision.
	var material := StandardMaterial3D.new()
	material.vertex_color_use_as_albedo = true
	material.metallic = float(_world.get("turret_metallic", 0.55))
	material.roughness = float(_world.get("turret_roughness", 0.38))
	# Brushed metal and panel seams. The turret family's noise is stretched 8:3,
	# which is what makes the housings read as machined rather than cast - and
	# the roughness map is what makes the sun catch the worn edges instead of
	# sliding evenly across the whole assembly.
	if _materials != null:
		_materials.apply(material, "turret")
	_skin(mesh, material)
	var layer := _instanced(mesh, limit)
	add_child(layer)
	return layer

## Several primitives welded into one ArrayMesh, so a silhouette can have greebles
## without costing a second draw call. The parts keep their own normals; nothing
## here needs a shared smooth surface.
##
## This is the whole graphics-quality strategy in one function: the budget is
## draw calls, not triangles, so detail is bought by making each instanced mesh
## richer rather than by adding instances.
func _merged(parts: Array) -> ArrayMesh:
	var tool := SurfaceTool.new()
	tool.begin(Mesh.PRIMITIVE_TRIANGLES)
	for part: Array in parts:
		tool.append_from(part[0] as Mesh, 0, part[1] as Transform3D)
	return tool.commit()

static func _at(x: float, y: float, z: float, scale: Vector3 = Vector3.ONE,
		rot: Vector3 = Vector3.ZERO) -> Transform3D:
	var basis := Basis.from_euler(rot).scaled(scale)
	return Transform3D(basis, Vector3(x, y, z))

## A cylinder whose axis runs along Z instead of Y, because barrels point
## forward and Godot's cylinders point up.
static func _zcyl(top: float, bottom: float, length: float, segments: int = 10) -> Mesh:
	var cyl := CylinderMesh.new()
	cyl.top_radius = top
	cyl.bottom_radius = bottom
	cyl.height = length
	cyl.radial_segments = segments
	return cyl

func _mount_mesh(id: String, radius: float, pad: float) -> Mesh:
	var mount := CylinderMesh.new()
	mount.height = pad
	match id:
		"railgun":
			# A low sled rather than a turntable: the thing on top barely turns.
			mount.top_radius = radius * 1.25
			mount.bottom_radius = radius * 1.9
			mount.radial_segments = 4
		"cannon":
			mount.top_radius = radius * 1.7
			mount.bottom_radius = radius * 1.85
			mount.radial_segments = 8
		"suppressor":
			mount.top_radius = radius * 1.3
			mount.bottom_radius = radius * 1.5
			mount.radial_segments = 12
		_:
			mount.top_radius = radius * 1.45
			mount.bottom_radius = radius * 1.6
			mount.radial_segments = 6
	# Every mount stands on a wider foundation slab with a collar where the
	# housing seats - the cheap two-primitive difference between "a shape on the
	# grass" and "a thing that was installed".
	var slab := CylinderMesh.new()
	slab.top_radius = mount.bottom_radius * 1.18
	slab.bottom_radius = mount.bottom_radius * 1.28
	slab.height = pad * 0.35
	slab.radial_segments = mount.radial_segments
	var collar := TorusMesh.new()
	collar.inner_radius = mount.top_radius * 0.8
	collar.outer_radius = mount.top_radius * 1.02
	collar.rings = 12
	collar.ring_segments = 5
	return _merged([
		[mount, _at(0.0, 0.0, 0.0)],
		[slab, _at(0.0, -pad * 0.36, 0.0)],
		[collar, _at(0.0, pad * 0.5, 0.0)],
	])

## Each housing is a small assembly now, not one primitive. Same silhouette
## logic as before - the four families must read apart from directly above -
## just with the detail a machine would actually have.
func _housing_mesh(id: String, radius: float, height: float) -> Mesh:
	var r := radius
	var h := height
	match id:
		"rig":
			# A derrick: A-frame legs and a crossbeam. It has to read as NOT A
			# WEAPON from across the board - money standing where a gun could be.
			var leg := BoxMesh.new()
			leg.size = Vector3(r * 0.28, h * 1.1, r * 0.28)
			var beam := BoxMesh.new()
			beam.size = Vector3(r * 1.7, h * 0.16, r * 0.34)
			var tank := CylinderMesh.new()
			tank.top_radius = r * 0.55
			tank.bottom_radius = r * 0.55
			tank.height = h * 0.42
			tank.radial_segments = 10
			return _merged([
				[leg, _at(-r * 0.62, 0.0, 0.0, Vector3.ONE, Vector3(0.0, 0.0, 0.22))],
				[leg, _at(r * 0.62, 0.0, 0.0, Vector3.ONE, Vector3(0.0, 0.0, -0.22))],
				[beam, _at(0.0, h * 0.5, 0.0)],
				[tank, _at(0.0, -h * 0.28, 0.0)],
			])
		"ballistic":
			# An autocannon receiver: boxy, with an ammunition drum slung on the
			# left and an optics block on top. Asymmetry is deliberate - real guns
			# are fed from somewhere.
			var receiver := BoxMesh.new()
			receiver.size = Vector3(r * 1.4, h, r * 1.9)
			var optics := BoxMesh.new()
			optics.size = Vector3(r * 0.5, h * 0.28, r * 0.6)
			var drum := CylinderMesh.new()
			drum.top_radius = r * 0.38
			drum.bottom_radius = r * 0.38
			drum.height = r * 0.5
			drum.radial_segments = 10
			return _merged([
				[receiver, _at(0.0, 0.0, 0.0)],
				[optics, _at(r * 0.3, h * 0.6, -r * 0.35)],
				[drum, _at(-r * 0.85, -h * 0.1, 0.0,
					Vector3.ONE, Vector3(0.0, 0.0, PI * 0.5))],
			])
		"cannon":
			# The recoil-soaking taper, with a collar where the tube meets it and
			# two haunches bracing the back. Reads as artillery, not a bollard.
			var taper := CylinderMesh.new()
			taper.top_radius = r * 0.55
			taper.bottom_radius = r * 1.25
			taper.height = h * 0.8
			taper.radial_segments = 10
			var collar := TorusMesh.new()
			collar.inner_radius = r * 0.5
			collar.outer_radius = r * 0.75
			collar.rings = 12
			collar.ring_segments = 6
			var haunch := BoxMesh.new()
			haunch.size = Vector3(r * 0.4, h * 0.5, r * 0.7)
			return _merged([
				[taper, _at(0.0, 0.0, 0.0)],
				[collar, _at(0.0, h * 0.34, 0.0)],
				[haunch, _at(-r * 0.8, -h * 0.14, -r * 0.5)],
				[haunch, _at(r * 0.8, -h * 0.14, -r * 0.5)],
			])
		"suppressor":
			# The drum, wound: three coil rings around it and a cap on top. It is
			# an electrical machine and now looks wound rather than extruded.
			var drum := CylinderMesh.new()
			drum.top_radius = r * 0.8
			drum.bottom_radius = r * 0.8
			drum.height = h * 0.9
			drum.radial_segments = 16
			var winding := TorusMesh.new()
			winding.inner_radius = r * 0.76
			winding.outer_radius = r * 0.94
			winding.rings = 16
			winding.ring_segments = 5
			var cap := CylinderMesh.new()
			cap.top_radius = r * 0.34
			cap.bottom_radius = r * 0.55
			cap.height = h * 0.2
			cap.radial_segments = 10
			var parts := [[drum, _at(0.0, 0.0, 0.0)], [cap, _at(0.0, h * 0.55, 0.0)]]
			for level: float in [-0.28, 0.0, 0.28]:
				parts.append([winding, _at(0.0, h * level, 0.0)])
			return _merged(parts)
		_:
			# Railgun: the sled, twin capacitor banks running its whole length, and
			# a heat-sink stack at the back. Everything about it says "stored
			# charge", which is what the two-second fire interval is.
			var sled := BoxMesh.new()
			sled.size = Vector3(r * 0.95, h * 0.6, r * 2.3)
			var bank := _zcyl(r * 0.26, r * 0.26, r * 2.0, 10)
			var fin := BoxMesh.new()
			fin.size = Vector3(r * 0.8, h * 0.36, r * 0.1)
			var parts := [
				[sled, _at(0.0, -h * 0.06, 0.0)],
				[bank, _at(-r * 0.3, h * 0.32, 0.0)],
				[bank, _at(r * 0.3, h * 0.32, 0.0)],
			]
			for step in 3:
				parts.append([fin, _at(0.0, h * 0.1, -r * (0.85 + 0.14 * float(step)))])
			return _merged(parts)

## Muzzles are authored inside a unit cube, Z forward - the per-frame transform
## scales X/Y by girth and Z by reach, so everything here is proportion.
func _muzzle_mesh(id: String) -> Mesh:
	if id == "rig":
		# The pump wheel where a barrel would be. It spins with the aim code and
		# that is fine - a rig "aims" at nothing and the wheel just turns.
		var wheel := TorusMesh.new()
		wheel.inner_radius = 0.2
		wheel.outer_radius = 0.42
		wheel.rings = 10
		wheel.ring_segments = 5
		return wheel
	if id == "suppressor":
		# No barrel at all - a coil ring with a focus hub floating in it. A weapon
		# that does almost no damage should not be pointing a gun at anything.
		var ring := TorusMesh.new()
		ring.inner_radius = 0.34
		ring.outer_radius = 0.5
		ring.rings = 12
		ring.ring_segments = 8
		var hub := SphereMesh.new()
		hub.radius = 0.16
		hub.height = 0.32
		hub.radial_segments = 8
		hub.rings = 4
		return _merged([[ring, _at(0.0, 0.0, 0.0)], [hub, _at(0.0, 0.0, 0.0)]])
	if id == "cannon":
		# A proper bore pointing forward, with a muzzle ring at the mouth and a
		# breech block behind it, instead of the bare tapered tube.
		var bore := _zcyl(0.4, 0.46, 0.9, 10)
		var mouth := TorusMesh.new()
		mouth.inner_radius = 0.36
		mouth.outer_radius = 0.5
		mouth.rings = 10
		mouth.ring_segments = 5
		var breech := BoxMesh.new()
		breech.size = Vector3(0.66, 0.66, 0.3)
		return _merged([
			[bore, _at(0.0, 0.0, 0.05)],
			[mouth, _at(0.0, 0.0, 0.48, Vector3.ONE, Vector3(PI * 0.5, 0.0, 0.0))],
			[breech, _at(0.0, 0.0, -0.4)],
		])
	if id == "railgun":
		# Twin rails with spacers and an emitter head: the projectile rides the
		# gap, so the gap is the thing the silhouette is built around.
		var rail := BoxMesh.new()
		rail.size = Vector3(0.16, 0.3, 1.0)
		var spacer := BoxMesh.new()
		spacer.size = Vector3(0.56, 0.2, 0.08)
		var head := BoxMesh.new()
		head.size = Vector3(0.6, 0.44, 0.12)
		var parts := [
			[rail, _at(-0.2, 0.0, 0.0)],
			[rail, _at(0.2, 0.0, 0.0)],
			[head, _at(0.0, 0.0, 0.44)],
		]
		for z: float in [-0.32, 0.0, 0.32]:
			parts.append([spacer, _at(0.0, 0.0, z)])
		return _merged(parts)
	# Ballistic: twin barrels and a muzzle brake, which is what an autocannon
	# with a 0.5s fire interval would actually need.
	var barrel := _zcyl(0.14, 0.17, 0.94, 8)
	var brake := BoxMesh.new()
	brake.size = Vector3(0.72, 0.34, 0.16)
	return _merged([
		[barrel, _at(-0.19, 0.0, 0.0)],
		[barrel, _at(0.19, 0.0, 0.0)],
		[brake, _at(0.0, 0.0, 0.42)],
	])

## Muzzle proportions per family, as (length, girth, tilt-up in radians).
## Authored next to the meshes they scale so the two cannot drift apart.
func _muzzle_shape(id: String) -> Vector3:
	match id:
		"cannon":
			return Vector3(0.62, 1.5, 0.62)   # short, fat, and lobbing upward
		"railgun":
			return Vector3(2.45, 0.52, 0.0)   # very long, very thin, dead flat
		"suppressor":
			return Vector3(0.5, 1.25, 0.0)    # a ring, not a barrel
		_:
			return Vector3(1.0, 1.0, 0.0)

func _build_entity_layers() -> void:
	# One layer per enemy class, so each can have its own silhouette. Class is
	# readable from shape as well as colour, which section 6.1 of the plan makes
	# a correctness requirement rather than a nicety.
	_enemy_layers.clear()
	# All classes or none, decided before any layer is built. Reading it off the
	# first class alone was wrong twice over: it made the answer depend on which
	# drone happens to be listed first, and a board that drew some classes as
	# sprites and the rest as meshes would read as two games at once.
	_sprite_drones = true
	for type_index in _sim.enemy_type_count():
		if _sprite(SPRITE_DRONE_DIR, _sim.enemy_id(type_index)) == null:
			_sprite_drones = false
			break

	for type_index in _sim.enemy_type_count():
		var id := _sim.enemy_id(type_index)
		var art := _sprite(SPRITE_DRONE_DIR, id)
		var layer: MultiMeshInstance3D
		if art != null:
			# One authored sprite on a flat quad. Two triangles instead of the
			# welded assembly this class used to be, which is most of why the
			# art pass came out cheaper than the meshes it replaced.
			var plane := _sprite_plane()
			plane.material = _sprite_material(art)
			layer = _instanced(plane, _sim.e_alive.size())
		else:
			var mesh := _enemy_mesh(id)
			_skin(mesh, _instanced_material(id))
			layer = _instanced(mesh, _sim.e_alive.size())
		# Drones too - a shadow is most of what makes one read as a solid object
		# moving over ground rather than a sprite sliding across it. Switchable,
		# because on a slow device this is the next thing to give up after trees.
		if bool(_world.get("drone_shadows", true)):
			_shadowed(layer)
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
			# A pointed body with a swept tail fin: something built to be fast and
			# nothing else. The old silhouette was the bare prism.
			var body := PrismMesh.new()
			body.size = Vector3(0.8, 0.7, 1.0)
			var fin := PrismMesh.new()
			fin.size = Vector3(0.1, 0.55, 0.5)
			return _merged([
				[body, _at(0.0, 0.0, 0.0)],
				[fin, _at(0.0, 0.35, -0.35)],
			])
		"walker":
			# A hull with a sensor head and shoulder plates either side - the
			# baseline drone, and the one every other silhouette is read against.
			var hull := BoxMesh.new()
			hull.size = Vector3(0.8, 0.7, 1.0)
			var head := BoxMesh.new()
			head.size = Vector3(0.4, 0.3, 0.34)
			var plate := BoxMesh.new()
			plate.size = Vector3(0.16, 0.5, 0.8)
			return _merged([
				[hull, _at(0.0, -0.1, 0.0)],
				[head, _at(0.0, 0.34, 0.28)],
				[plate, _at(-0.5, 0.0, -0.05)],
				[plate, _at(0.5, 0.0, -0.05)],
			])
		"heavy":
			# The slab, now visibly ARMOURED: a sloped glacis at the front and
			# layered top plates. It wears Plated in the damage matrix, so it
			# should look like the thing the matrix says it is.
			var slab := BoxMesh.new()
			slab.size = Vector3(1.15, 0.8, 1.25)
			var glacis := PrismMesh.new()
			glacis.size = Vector3(1.15, 0.55, 0.5)
			var plate := BoxMesh.new()
			plate.size = Vector3(0.95, 0.16, 0.9)
			return _merged([
				[slab, _at(0.0, -0.1, -0.1)],
				[glacis, _at(0.0, -0.06, 0.62, Vector3.ONE, Vector3(PI * 0.5, 0.0, 0.0))],
				[plate, _at(0.0, 0.38, -0.15)],
				[plate, _at(0.0, 0.52, -0.25, Vector3(0.8, 1.0, 0.8))],
			])
		"lance":
			# The dart, with swept side blades. Still the longest, narrowest thing
			# on the board - the blades widen the glow without blunting the point.
			var dart := PrismMesh.new()
			dart.size = Vector3(0.55, 0.6, 2.05)
			var blade := PrismMesh.new()
			blade.size = Vector3(0.12, 0.4, 1.1)
			return _merged([
				[dart, _at(0.0, 0.0, 0.0)],
				[blade, _at(-0.34, 0.0, -0.4)],
				[blade, _at(0.34, 0.0, -0.4)],
			])
		"mender":
			# The pod, orbited by its tool ring. It heals the drones around it, and
			# a floating halo is the shape of "projects something outward".
			var pod := SphereMesh.new()
			pod.radius = 0.42
			pod.height = 0.84
			pod.radial_segments = 12
			pod.rings = 6
			var halo := TorusMesh.new()
			halo.inner_radius = 0.5
			halo.outer_radius = 0.6
			halo.rings = 14
			halo.ring_segments = 5
			return _merged([
				[pod, _at(0.0, 0.0, 0.0)],
				[halo, _at(0.0, 0.12, 0.0)],
			])
		"jammer":
			# The mast, now carrying the dish it jams with and a tip emitter. The
			# only drone that attacks the board still reads as the only antenna.
			var mast := PrismMesh.new()
			mast.size = Vector3(0.6, 1.45, 0.6)
			var dish := TorusMesh.new()
			dish.inner_radius = 0.18
			dish.outer_radius = 0.42
			dish.rings = 10
			dish.ring_segments = 5
			var tip := SphereMesh.new()
			tip.radius = 0.14
			tip.height = 0.28
			tip.radial_segments = 8
			tip.rings = 4
			return _merged([
				[mast, _at(0.0, 0.0, 0.0)],
				[dish, _at(0.0, 0.35, 0.2, Vector3.ONE, Vector3(PI * 0.42, 0.0, 0.0))],
				[tip, _at(0.0, 0.78, 0.0)],
			])
		"brood":
			# Eight-sided and bulging, and the bulge is now a visible belly: it has
			# to read as FULL at a glance, because whether you pop it now or later
			# is a decision you only get while you can still see which one it is.
			var carrier := CylinderMesh.new()
			carrier.top_radius = 0.34
			carrier.bottom_radius = 0.58
			carrier.height = 0.9
			carrier.radial_segments = 8
			var belly := SphereMesh.new()
			belly.radius = 0.42
			belly.height = 0.6
			belly.radial_segments = 10
			belly.rings = 5
			return _merged([
				[carrier, _at(0.0, 0.05, 0.0)],
				[belly, _at(0.0, -0.28, 0.0)],
			])
		"breaker":
			# Six-sided and squat, with a ram jutting forward and a spine plate on
			# top. It is the thing built to walk through fire, and now looks it.
			var bunker := CylinderMesh.new()
			bunker.top_radius = 0.44
			bunker.bottom_radius = 0.58
			bunker.height = 0.85
			bunker.radial_segments = 6
			var ram := PrismMesh.new()
			ram.size = Vector3(0.7, 0.45, 0.5)
			var spine := BoxMesh.new()
			spine.size = Vector3(0.2, 0.24, 0.9)
			return _merged([
				[bunker, _at(0.0, 0.0, 0.0)],
				[ram, _at(0.0, -0.16, 0.55, Vector3.ONE, Vector3(PI * 0.5, 0.0, 0.0))],
				[spine, _at(0.0, 0.5, -0.1)],
			])
		_:
			var box := BoxMesh.new()
			box.size = Vector3.ONE
			return box

## Which classes glow. Reserved for the top of the ladder - if everything is lit
## up, nothing is.
const EMISSIVE_CLASSES := ["lance", "mender", "jammer"]

func _instanced_material(enemy_id: String = "") -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.vertex_color_use_as_albedo = true
	material.metallic = float(_world.get("enemy_metallic", 0.12))
	material.roughness = float(_world.get("enemy_roughness", 0.62))
	# Plated carapace. Every drone class shares one family and stays its own
	# colour, because the per-instance damage tint multiplies through the albedo
	# map - the shell gains a surface without any class losing the colour the
	# player identifies it by.
	if _materials != null:
		_materials.apply(material, "enemy")
	if EMISSIVE_CLASSES.has(enemy_id):
		# Emission is a flat add, so the per-instance damage tint still reads
		# through it - a hurt Lance dims like everything else, it just never stops
		# being the bright thing on the board. The two Shielded support drones get
		# their own cold glows, because they are the ones the player is told to
		# pick out of a crowd and a glow is what makes that possible at 4x speed.
		material.emission_enabled = true
		if enemy_id == "mender":
			material.emission = _color_of(_world.get("mender_glow", "#2ef0a8"))
			material.emission_energy_multiplier = float(_world.get("support_glow_energy", 1.1))
		elif enemy_id == "jammer":
			material.emission = _color_of(_world.get("jammer_glow", "#c76bff"))
			material.emission_energy_multiplier = float(_world.get("support_glow_energy", 1.1))
		else:
			material.emission = _color_of(_world.get("elite_glow", "#ff7a3c"))
			material.emission_energy_multiplier = float(_world.get("elite_glow_energy", 1.6))
	return material

# --- combat feedback -----------------------------------------------------------
#
# Everything in this section is decoration and knows it. It reads the simulation
# and never writes to it, it holds no state the simulation needs, and if it were
# deleted the game would play exactly the same and feel considerably worse.
#
# It works by DIFFING the simulation between ticks rather than by having the
# simulation report events. A shot fired is a cooldown that went up; an impact is
# a projectile slot that was alive and is not; a wreck is a drone slot that was
# alive and is not. That keeps the sim free of a render-facing event channel it
# would then have to hash, and it means an effect can never desync anything
# because there is nothing for it to desync.

## One pooled MultiMesh, like everything else on screen - draw calls stay flat
## whether one turret is firing or a hundred and forty-four are.
## Past a certain density another link line communicates nothing, so the drawing
## is bounded even though every multiplier behind it is computed in full.
const MAX_DRAWN_LINKS := 900

const FX_CAPACITY := 1024
enum { FX_FLASH, FX_SPARK, FX_BLAST, FX_WRECK }

var _fx: MultiMeshInstance3D
## Support-link lines. Static between board changes, like the turrets themselves.
var _links: MultiMeshInstance3D
var _fx_pos: PackedVector3Array = PackedVector3Array()
var _fx_age: PackedFloat32Array = PackedFloat32Array()
var _fx_life: PackedFloat32Array = PackedFloat32Array()
var _fx_size: PackedFloat32Array = PackedFloat32Array()
var _fx_grow: PackedFloat32Array = PackedFloat32Array()
var _fx_tint: PackedColorArray = PackedColorArray()
## Which layer each live slot draws into. See FX_GLOW / FX_IMPACT.
var _fx_kind: PackedInt32Array = PackedInt32Array()
enum { FX_GLOW, FX_IMPACT }
var _fx_impact: MultiMeshInstance3D
var _fx_head: int = 0

## What the board looked like at the end of the previous tick.
var _was_alive_e: PackedByteArray = PackedByteArray()
var _was_alive_p: PackedByteArray = PackedByteArray()
var _was_cooldown: PackedInt32Array = PackedInt32Array()
var _was_integrity: int = -1
## Camera shake, in world units, decaying toward zero. Only a leak causes it: if
## everything shakes the screen then nothing does, and a leak is the only event in
## the game that costs something you cannot get back.
var _shake: float = 0.0
var _shake_phase: float = 0.0
## Optional. The tick-to-tick diff below is the only place in the game that knows
## a shot was fired or a drone died, so sound rides along with it rather than
## working the same thing out a second time. Null everywhere it is not wanted -
## every headless test runs with no audio attached and nothing here notices.
var _sfx: Sfx = null

func attach_audio(sfx: Sfx) -> void:
	_sfx = sfx

## Which tracer art each weapon family fires. Named by art rather than damage
## type because the Suppressor and the Railgun are both Energy and look nothing
## alike in flight. A family with no entry (the Rig never fires) or missing art
## falls back to the generated tracer quad.
const TRACER_ART := {
	"ballistic": "proj_kinetic",
	"cannon": "proj_explosive",
	"railgun": "proj_energy",
	"suppressor": "proj_arc",
}

## One sprite layer per weapon family, or null where the family keeps the
## generated tracer. Indexed by blueprint, same as every other per-family array.
var _tracer_layers: Array = []

func _build_tracer_layers() -> void:
	_tracer_layers.clear()
	for family in _sim.blueprint_count():
		var id := _sim.blueprint_name(family)
		var art: Texture2D = null
		if TRACER_ART.has(id):
			art = _sprite(SPRITE_FX_DIR, TRACER_ART[id])
		if art == null:
			_tracer_layers.append(null)
			continue
		var plane := _sprite_plane()
		plane.material = _sprite_material(art)
		var layer := _instanced(plane, _sim.p_alive.size())
		add_child(layer)
		_tracer_layers.append(layer)

func _build_effect_layer() -> void:
	var quad := QuadMesh.new()
	quad.size = Vector2.ONE
	var material := StandardMaterial3D.new()
	material.vertex_color_use_as_albedo = true
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	# Additive, so fading is just fading the colour toward black and there is
	# nothing to depth-sort. Muzzle flashes overlapping each other in a firing line
	# is the correct look anyway.
	material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	material.blend_mode = BaseMaterial3D.BLEND_MODE_ADD
	material.billboard_mode = BaseMaterial3D.BILLBOARD_ENABLED
	material.billboard_keep_scale = true
	material.disable_receive_shadows = true
	material.no_depth_test = false
	# Without this every effect is a flat white square - a sticker on the board
	# rather than light coming off it. Captured and looked at before it was added,
	# which is the only way that particular problem is ever going to be noticed.
	material.albedo_texture = _glow_texture()
	quad.material = material
	_fx = _instanced(quad, FX_CAPACITY)
	_fx.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(_fx)

	# A second layer for hits, wearing the authored spark burst instead of the
	# soft glow. Same pool, same ageing, same additive fade - the slots carry a
	# kind and the draw sweep deals each live slot to its own layer. One extra
	# draw call, and a hit stops being a smear of light and becomes debris.
	_fx_impact = null
	var burst := _sprite(SPRITE_FX_DIR, "proj_impact")
	if burst != null:
		var impact_quad := QuadMesh.new()
		impact_quad.size = Vector2.ONE
		var impact_material := StandardMaterial3D.new()
		impact_material.vertex_color_use_as_albedo = true
		impact_material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
		impact_material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
		impact_material.blend_mode = BaseMaterial3D.BLEND_MODE_ADD
		impact_material.billboard_mode = BaseMaterial3D.BILLBOARD_ENABLED
		impact_material.billboard_keep_scale = true
		impact_material.disable_receive_shadows = true
		impact_material.albedo_texture = burst
		impact_quad.material = impact_material
		_fx_impact = _instanced(impact_quad, FX_CAPACITY)
		_fx_impact.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		add_child(_fx_impact)

	_fx_pos.resize(FX_CAPACITY)
	_fx_kind.resize(FX_CAPACITY)
	_fx_age.resize(FX_CAPACITY)
	_fx_life.resize(FX_CAPACITY)
	_fx_size.resize(FX_CAPACITY)
	_fx_grow.resize(FX_CAPACITY)
	_fx_tint.resize(FX_CAPACITY)
	for i in FX_CAPACITY:
		_fx_age[i] = 1.0
		_fx_life[i] = 0.0

	_was_alive_e.resize(_sim.e_alive.size())
	_was_alive_p.resize(_sim.p_alive.size())
	_last_e_bound = 0
	_last_p_bound = 0
	_was_cooldown.resize(_sim.t_used.size())
	_was_integrity = _sim.integrity()

## A soft round glow, generated rather than shipped.
##
## Same reasoning as the sound: no binary assets in the repository, and the shape
## of the falloff becomes something that can be reasoned about in one line instead
## of opened in an image editor. Squared falloff rather than linear because a
## linear one still has a visible disc edge.
func _glow_texture() -> ImageTexture:
	var size := 64
	var image := Image.create(size, size, false, Image.FORMAT_RGBA8)
	var half := float(size) * 0.5
	for y in size:
		for x in size:
			var dx := (float(x) + 0.5 - half) / half
			var dy := (float(y) + 0.5 - half) / half
			var falloff := clampf(1.0 - sqrt(dx * dx + dy * dy), 0.0, 1.0)
			image.set_pixel(x, y, Color(1.0, 1.0, 1.0, falloff * falloff))
	return ImageTexture.create_from_image(image)

## Call once per simulation tick, from whoever is driving step(). Per tick and not
## per frame: at 4x speed a frame covers several ticks, and a muzzle flash that
## only appears on the tick a frame happens to land on is a firing line that looks
## like it is misfiring.
func note_tick() -> void:
	if _fx == null:
		return
	_note_muzzle_flashes()
	_note_impacts()
	_note_wrecks()
	var integrity := _sim.integrity()
	if _was_integrity >= 0 and integrity < _was_integrity:
		_shake = minf(_shake + float(_world.get("shake_per_leak", 9.0)),
			float(_world.get("shake_max", 34.0)))
		if _sfx != null:
			_sfx.play(Sfx.LEAK)
	_was_integrity = integrity

func _note_muzzle_flashes() -> void:
	var reach := float(_world.get("barrel_length", 34.0))
	var growth := float(_world.get("platform_tier_growth", 0.28))
	var lift := float(_world.get("pad_height", 12.0)) + float(_world.get("platform_height", 48.0)) * 0.78
	var tint := _color_of(_world.get("flash", "#ffd9a0"))
	for i in _sim.t_count:
		var cooldown := _sim.t_cooldown[i]
		# A cooldown only ever counts down, one per tick. The one thing that can
		# raise it is _fire().
		var fired := i < _was_cooldown.size() and cooldown > _was_cooldown[i]
		if i < _was_cooldown.size():
			_was_cooldown[i] = cooldown
		if not fired:
			continue
		var angle: float = _barrel_angle[i] if i < _barrel_angle.size() else 0.0
		var scale := 1.0 + growth * float(_sim.platform_tier(i))
		var shape := _muzzle_shape(_sim.blueprint_name(_sim.platform_blueprint(i)))
		var out := reach * scale * shape.x
		_emit(Vector3(_sim.t_x[i] + sin(angle) * out, lift * scale + sin(shape.z) * out * 0.5,
			_sim.t_y[i] + cos(angle) * out),
			float(_world.get("flash_size", 26.0)) * scale, 0.9,
			float(_world.get("flash_life", 0.07)), tint)
		if _sfx != null:
			_sfx.play(Sfx.SHOT, _sim.blueprint_name(_sim.platform_blueprint(i)))
	# Turrets sold this tick leave a stale cooldown behind; clearing the tail stops
	# the next turret built into that slot flashing on its first frame.
	for i in range(_sim.t_count, _was_cooldown.size()):
		_was_cooldown[i] = 0

func _note_impacts() -> void:
	var lift := float(_world.get("projectile_lift", 20.0))
	var spark := _color_of(_world.get("impact", "#ffe6b0"))
	var blast := _color_of(_world.get("blast", "#ff9a4a"))
	# Same grow-until-drained rule as _note_wrecks: an impact IS a projectile
	# leaving the pool, so the sweep must outlive the pool emptying.
	_last_p_bound = maxi(_last_p_bound, _sim.projectile_slot_bound())
	# Nothing left alive means every death up to the bound is inspected by this
	# sweep, so the bound can start again from zero afterwards.
	var drained := _sim.p_live_count == 0
	for i in _last_p_bound:
		var alive := _sim.p_alive[i]
		var died := _was_alive_p[i] == 1 and alive == 0
		_was_alive_p[i] = alive
		if not died:
			continue
		var splash: float = _sim.p_splash[i]
		if splash > 0.0:
			# Sized to the actual blast radius, so what you see is what it hit.
			_emit(Vector3(_sim.p_x[i], lift, _sim.p_y[i]), splash * 0.5, 2.0,
				float(_world.get("blast_life", 0.3)), blast, FX_IMPACT)
			if _sfx != null:
				_sfx.play(Sfx.BLAST)
		else:
			_emit(Vector3(_sim.p_x[i], lift, _sim.p_y[i]),
				float(_world.get("impact_size", 15.0)), 1.1,
				float(_world.get("impact_life", 0.11)), spark, FX_IMPACT)
			if _sfx != null:
				_sfx.play(Sfx.IMPACT)
	if drained:
		_last_p_bound = 0

func _note_wrecks() -> void:
	var tint := _color_of(_world.get("wreck", "#ff7042"))
	var height := float(_world.get("enemy_height", 26.0))
	# A DIFF loop, so it cannot use the sim's live bound directly: the bound resets
	# to zero on the death that empties the pool, and that death is precisely the
	# one this function exists to draw. So the renderer keeps its OWN bound that
	# only grows, and drops to zero only after a sweep that found nothing left
	# alive - which means every death up to it has just been processed. Deliberately
	# not "this tick's bound or last tick's": that version was correct only if
	# note_tick ran on every single tick, and a test that called it once caught it.
	_last_e_bound = maxi(_last_e_bound, _sim.enemy_slot_bound())
	var drained := _sim.e_live_count == 0
	for i in _last_e_bound:
		var alive := _sim.e_alive[i]
		var died := _was_alive_e[i] == 1 and alive == 0
		_was_alive_e[i] = alive
		if not died:
			continue
		var radius := _sim.enemy_radius(_sim.e_type[i])
		_emit(Vector3(_sim.e_x[i], height * (radius / _reference_radius) * 0.5, _sim.e_y[i]),
			radius * 2.2, 1.8, float(_world.get("wreck_life", 0.26)), tint)
		if _sfx != null:
			_sfx.play(Sfx.WRECK)
	if drained:
		_last_e_bound = 0

## Claim the next slot in the ring. Oldest-first eviction, which at 1024 slots
## means the only thing that can ever be cut short is an effect from a tick where
## more than a thousand things happened at once - and on that tick nobody is
## looking at any one of them.
func _emit(position: Vector3, size: float, grow: float, life: float, tint: Color,
		kind: int = FX_GLOW) -> void:
	var i := _fx_head
	_fx_head = (_fx_head + 1) % FX_CAPACITY
	_fx_pos[i] = position
	_fx_age[i] = 0.0
	_fx_life[i] = life
	_fx_size[i] = size
	_fx_grow[i] = grow
	_fx_tint[i] = tint
	# Falls back to the glow when the authored burst is not on disk, so a
	# checkout with no assets/ still shows every hit.
	_fx_kind[i] = kind if _fx_impact != null else FX_GLOW

## Age and draw.
##
## Ageing is by elapsed time and not by frame count, which is what lets the
## screenshot tool age everything by zero and capture the instant rather than
## whatever survived a software renderer's half-second frame. Pausing the game
## does NOT stop it: main keeps calling this with a real delta while paused, so
## effects already in the air fade out and the board goes quiet, which is the
## right thing for a decoration layer to do when the simulation stops.
func _update_effects(delta: float) -> void:
	var mm := _fx.multimesh
	var impact_mm: MultiMesh = _fx_impact.multimesh if _fx_impact != null else null
	var shown := 0
	var impacts := 0
	for i in FX_CAPACITY:
		if _fx_age[i] >= _fx_life[i]:
			continue
		_fx_age[i] += delta
		if _fx_age[i] >= _fx_life[i]:
			# Expired during this frame. The last frame of the fade is fully faded,
			# so there is nothing to draw but a black quad.
			continue
		var t: float = clampf(_fx_age[i] / maxf(_fx_life[i], 0.0001), 0.0, 1.0)
		var size: float = _fx_size[i] * (1.0 + _fx_grow[i] * t)
		var xf := Transform3D(Basis().scaled(Vector3(size, size, size)), _fx_pos[i])
		# Fade toward black rather than toward transparent: the blend is additive,
		# so black IS invisible and there is no sorting to get wrong.
		var faded: Color = _fx_tint[i] * (1.0 - t)
		if impact_mm != null and _fx_kind[i] == FX_IMPACT:
			impact_mm.set_instance_transform(impacts, xf)
			impact_mm.set_instance_color(impacts, faded)
			impacts += 1
		else:
			mm.set_instance_transform(shown, xf)
			mm.set_instance_color(shown, faded)
			shown += 1
		if shown + impacts >= FX_CAPACITY:
			break
	mm.visible_instance_count = shown
	if impact_mm != null:
		impact_mm.visible_instance_count = impacts

## Nudge the camera when the corridor takes a hit. Decays on its own; the phase
## walk keeps successive leaks from landing on the same offset.
func _update_shake(delta: float) -> void:
	if _shake <= 0.01:
		_shake = 0.0
		camera.position = _camera_home
		return
	_shake_phase += delta * float(_world.get("shake_speed", 47.0))
	camera.position = _camera_home + Vector3(
		sin(_shake_phase) * _shake, cos(_shake_phase * 1.37) * _shake * 0.6, 0.0)
	_shake = maxf(_shake - delta * float(_world.get("shake_decay", 44.0)), 0.0)

func _instanced(mesh: Mesh, capacity: int) -> MultiMeshInstance3D:
	var mm := MultiMesh.new()
	mm.transform_format = MultiMesh.TRANSFORM_3D
	mm.use_colors = true
	mm.mesh = mesh
	mm.instance_count = maxi(capacity, 1)
	mm.visible_instance_count = 0
	var node := MultiMeshInstance3D.new()
	node.multimesh = mm
	# Shadows OFF by default, on for the layers that earn them. It was on for
	# everything, which meant the health bars, the tracers and the transparent
	# ground overlay were all being rendered a second time into the shadow map -
	# for objects that are unshaded decorations and cast nothing a player could
	# ever see. The shadow pass is the most expensive thing in this scene.
	node.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	# The corridor runs off the edge of the board; without a generous custom AABB
	# Godot culls instances whose transforms it has not measured.
	node.custom_aabb = AABB(Vector3(-6000, -600, -6000), Vector3(14000, 1200, 14000))
	return node

# --- board state (turrets and owned ground) ---------------------------------------

## Rebuilt when the board changes - a turret placed or upgraded, or ground
## bought. Not per frame.
##
## Split by trigger, because the two halves cost very different amounts and are
## caused by different things: the ground overlay sweeps every cell on the board
## (7.5ms at 74x40) and only changes when ground is BOUGHT, while turrets and
## their link lines change on every placement and every upgrade. Rebuilding both
## on either was a 12ms hitch on every click - most of it redrawing an overlay
## that had not changed.
func refresh_board(ground_changed: bool = true) -> void:
	if ground_changed:
		_refresh_cells()
	_refresh_turrets()
	_refresh_link_lines()

## The line between two turrets that are lending each other something.
##
## Drawn because a mechanic you cannot see is not a mechanic. Support links change
## where you put things and what you upgrade, and both of those decisions are made
## by looking at the board - so the board has to say which turrets are actually
## reaching each other rather than leaving it to be inferred from a hover readout
## one turret at a time.
func _build_link_layer() -> void:
	var bar := BoxMesh.new()
	bar.size = Vector3.ONE
	var material := StandardMaterial3D.new()
	material.vertex_color_use_as_albedo = true
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	material.blend_mode = BaseMaterial3D.BLEND_MODE_ADD
	material.disable_receive_shadows = true
	bar.material = material
	_links = _instanced(bar, MAX_DRAWN_LINKS)
	_links.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(_links)

func _refresh_link_lines() -> void:
	# The pair list is built on demand rather than kept in step by the simulation:
	# drawing is wanted a handful of times a second when the board visibly changes,
	# and the multipliers are wanted thirty times a second whether anything is on
	# screen or not.
	_sim.rebuild_link_pairs(MAX_DRAWN_LINKS)
	var mm := _links.multimesh
	var lift := float(_world.get("link_lift", 9.0))
	var width := float(_world.get("link_width", 3.0))
	var alpha := float(_world.get("link_alpha", 0.5))
	var ballistic := _color("platform")
	var cannon := _color_of(_world.get("platform_cannon", "#c98a5b"))
	var suppressor := _color_of(_world.get("platform_suppressor", "#4fc9d8"))
	var railgun := _color_of(_world.get("platform_railgun", "#d8d24f"))
	var shown := 0
	for k in _sim.link_count():
		if shown >= MAX_DRAWN_LINKS:
			break
		var to := _sim.link_from(k)
		var from := _sim.link_to(k)
		var dx := _sim.t_x[to] - _sim.t_x[from]
		var dz := _sim.t_y[to] - _sim.t_y[from]
		var span := sqrt(dx * dx + dz * dz)
		if span < 0.001:
			continue
		dx /= span
		dz /= span
		_basis = Basis(
			Vector3(dz * width, 0.0, -dx * width),
			Vector3(0.0, width, 0.0),
			Vector3(dx * span, 0.0, dz * span))
		mm.set_instance_transform(shown, Transform3D(_basis, Vector3(
			(_sim.t_x[from] + _sim.t_x[to]) * 0.5, lift,
			(_sim.t_y[from] + _sim.t_y[to]) * 0.5)))
		# Tinted by the family DOING the lending, so a glance says what kind of
		# help is flowing and in which direction the upgrade money should go.
		var tint := _family_colour(from, ballistic, cannon, suppressor, railgun)
		# Additive blend, so brightness IS the alpha: scale the colour and leave the
		# alpha channel alone. Multiplying the whole Color by the fade (which is what
		# this did first) dimmed it twice and the lines all but vanished.
		mm.set_instance_color(shown, Color(tint.r * alpha, tint.g * alpha, tint.b * alpha, 1.0))
		shown += 1
	mm.visible_instance_count = shown

func _refresh_cells() -> void:
	var mm := _cells.multimesh
	var height := float(_world.get("band_height", 2.0))
	var size := _sim.cell_size() - float(_world.get("cell_inset", 5.0))
	# Every one of these was being looked up INSIDE the loop below, which runs
	# once per cell on the board - 2,960 of them on the largest maps. Two
	# Dictionary lookups and two float conversions per cell is around six
	# thousand of each per ground purchase, for four values that cannot change
	# while the loop runs. The four possible tints are finished here, alpha and
	# all, so the body only chooses between them.
	var owned_alpha := float(_world.get("band_alpha", 0.55))
	var offer_alpha := float(_world.get("band_offer_alpha", 0.3))
	var owned := _color_of(_world.get("band", "#1e4034"))
	owned.a = owned_alpha
	var offered := _color_of(_world.get("band_offer", "#39506e"))
	offered.a = offer_alpha
	# Premium ground gets its own colour at full strength - scarce tiles whose
	# whole point is being seen and fought over.
	var premium_owned := _color_of(_world.get("band_premium", "#c9a227"))
	premium_owned.a = owned_alpha
	var premium_offered := premium_owned
	premium_offered.a = offer_alpha
	# Every cell is the same size, so the basis is built once instead of 2,960
	# times.
	var cell_basis := Basis().scaled(Vector3(size, height, size))
	var lift := height * 0.5
	var shown := 0
	# Centres are computed arithmetically rather than through cell_centre_x/y: this
	# loop runs over every cell on the board and a GDScript call per axis per cell
	# is measurable. A "skip cells that are not buildable" reject was tried here
	# and made it SLOWER - on these boards most of the band beside the road is
	# buildable, so it added a call per cell and rejected almost nothing.
	var cell_size := _sim.cell_size()
	var half := cell_size * 0.5
	# Read the flags as arrays and reject on them, instead of asking the sim three
	# questions about every cell on the board. That was three cross-object calls
	# per cell - each of which made more of its own - around twenty thousand
	# dispatches per ground purchase, and it measured 8.9ms: half a frame at 60Hz,
	# spent at the exact moment the player clicked.
	#
	# The cheap tests are the array reads. Whether a cell can actually be OFFERED
	# is still asked through cell_is_offerable(), which owns the adjacency rule -
	# and it is now only asked about cells that are buildable ground to begin
	# with, which is a thin band beside the road rather than the whole grid. An
	# overlay that re-derived that rule for itself would be free to disagree with
	# what the player can really buy, which is worse than any frame it would save.
	var unlocked_flags := _sim.cell_unlocked_flags()
	var buildable_flags := _sim.cell_buildable_flags()
	var premium_ids := _sim.cell_premium_ids()
	var cols := _sim.grid_cols()
	for cy in _sim.grid_rows():
		var centre_z := float(cy) * cell_size + half
		var row := cy * cols
		for cx in cols:
			var index := row + cx
			var unlocked := unlocked_flags[index] == 1
			if not unlocked:
				if buildable_flags[index] == 0:
					continue
				if not _sim.cell_is_offerable(cx, cy):
					continue
			var tint := owned if unlocked else offered
			if premium_ids[index] >= 0:
				tint = premium_owned if unlocked else premium_offered
			mm.set_instance_transform(shown, Transform3D(cell_basis,
				to_world(float(cx) * cell_size + half, centre_z, lift)))
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

	# Turrets are grouped by family, so each family's layers only carry its own.
	# Counted off the BODY layers: in sprite mode the mount and muzzle layers do
	# not exist, and the body is the whole emplacement.
	var filled := PackedInt32Array()
	filled.resize(_turret_bodies.size())
	filled.fill(0)
	var sprite_span := float(_world.get("turret_sprite_span", 96.0))
	var sprite_lift := float(_world.get("turret_sprite_lift", 3.0))

	for i in _sim.t_count:
		var blueprint := _sim.platform_blueprint(i)
		if blueprint < 0 or blueprint >= _turret_bodies.size():
			continue
		var slot := filled[blueprint]
		var tier := _sim.platform_tier(i)
		var max_tier := maxi(_sim.platform_max_tier(blueprint) - 1, 1)
		var scale := 1.0 + growth * float(tier)
		var fraction := float(tier) / float(max_tier)
		var tint := _family_colour(i, ballistic, cannon, suppressor, railgun).lerp(
			top_colour, fraction)

		var facing := _barrel_angle[i] if i < _barrel_angle.size() else 0.0
		var bodies := _turret_bodies[blueprint].multimesh
		if _sprite_turrets:
			# Flat on the ground, turned to face its target. Tier still reads as
			# size, and the tint is left WHITE so the authored colours survive -
			# a family tint multiplied over painted art only ever muddies it.
			var span := sprite_span * scale * _turret_sprite_factor[blueprint]
			var forward := _forward_of(_sim.blueprint_name(blueprint))
			bodies.set_instance_transform(slot, Transform3D(
				_lean * Basis(Vector3.UP, facing + forward).scaled(Vector3(span, 1.0, span)),
				to_world(_sim.t_x[i], _sim.t_y[i],
					sprite_lift + (2.0 if _sprite_turret_split else 0.0) + span * _lean_lift)))
			bodies.set_instance_color(slot, Color.WHITE)
			if _sprite_turret_split:
				# The base never turns. That is the entire point of the split.
				var base_mm := _turret_bases[blueprint].multimesh
				base_mm.set_instance_transform(slot, Transform3D(
					_lean * Basis().scaled(Vector3(span, 1.0, span)),
					to_world(_sim.t_x[i], _sim.t_y[i], sprite_lift + span * _lean_lift)))
				base_mm.set_instance_color(slot, Color.WHITE)
		else:
			var bases := _turret_bases[blueprint].multimesh
			bases.set_instance_transform(slot, Transform3D(Basis(),
				to_world(_sim.t_x[i], _sim.t_y[i], pad_height * 0.5)))
			bases.set_instance_color(slot, plinth)
			# Housings are turned to face the same way as the barrel, so a boxy
			# receiver reads as pointing at something rather than as a stray crate.
			bodies.set_instance_transform(slot, Transform3D(
				Basis(Vector3.UP, facing).scaled(Vector3(scale, scale, scale)),
				to_world(_sim.t_x[i], _sim.t_y[i], pad_height + body_height * scale * 0.5)))
			bodies.set_instance_color(slot, tint)
		filled[blueprint] = slot + 1

	for family in _turret_bodies.size():
		if not _turret_bases.is_empty():
			_turret_bases[family].multimesh.visible_instance_count = filled[family]
		_turret_bodies[family].multimesh.visible_instance_count = filled[family]

# --- per frame ---------------------------------------------------------------------

func update_visuals(alpha: float, delta: float = 0.0) -> void:
	_update_enemies(alpha)
	_update_projectiles(alpha)
	_update_barrels()
	_update_effects(delta)
	_update_shake(delta)

func _update_enemies(alpha: float) -> void:
	var bar_mm := _hp_bars.multimesh
	var enemy_height := float(_world.get("enemy_height", 26.0))
	var drone_span := float(_world.get("drone_sprite_scale", 3.1))
	var drone_lift := float(_world.get("drone_sprite_lift", 4.0))
	var drone_rotates := bool(_world.get("sprite_rotate_drones", true))
	var bar_lift := float(_world.get("hp_bar_lift", 50.0))
	var bar_width := float(_world.get("hp_bar_width", 38.0))
	var bar_thickness := float(_world.get("hp_bar_height", 6.0))

	var per_layer := PackedInt32Array()
	per_layer.resize(_enemy_layers.size())
	per_layer.fill(0)
	var bars := 0

	# Bounded by the sim's live high-water mark, not by the pool size: the pool is
	# 4,096 slots and a busy board has a few hundred live.
	for i in _sim.enemy_slot_bound():
		if _sim.e_alive[i] == 0:
			continue
		# Interpolate distance-along-path, then resolve it to a position, so
		# enemies round corners instead of cutting across them.
		var prog: float = _sim.e_prev_prog[i] + (_sim.e_prog[i] - _sim.e_prev_prog[i]) * alpha
		_sim.sample_for_render(prog, _sim.e_offset[i], _sim.enemy_route(i))
		var px := _sim.out_x()
		var pz := _sim.out_y()
		var type_index: int = _sim.e_type[i]
		# Footprint is the enemy's own radius; height scales with it so a Bulwark
		# is visibly a bigger machine than a Skitter, not just a wider one.
		var radius := _sim.enemy_radius(type_index)
		var footprint := radius * 2.0
		var height := enemy_height * (radius / _reference_radius)
		# A champion is half again the machine, and tinted white-hot. The size is
		# render-only - its hitbox is its class's - which is fine because nothing
		# in the sim aims by silhouette.
		var champion := _sim.enemy_is_champion(i)
		if champion:
			footprint *= _champion_scale
			height *= _champion_scale

		var layer := _enemy_layers[clampi(type_index, 0, _enemy_layers.size() - 1)]
		var mm := layer.multimesh
		var slot := per_layer[type_index]
		if _sprite_drones:
			# Flat on the ground and turned to face the way it is walking, so the
			# art's own "forward" - every asset is drawn facing up - points down
			# the lane. Sized off the class radius alone: a sprite has no height
			# to scale, and the picture already carries the proportions.
			var span := footprint * drone_span
			var heading := 0.0
			if drone_rotates:
				heading = atan2(_sim.out_dx(), _sim.out_dy())
			mm.set_instance_transform(slot, Transform3D(
				_lean * Basis(Vector3.UP,
					heading + _forward_of(_sim.enemy_id(type_index))
				).scaled(Vector3(span, 1.0, span)),
				Vector3(px, drone_lift + span * _lean_lift, pz)))
		else:
			mm.set_instance_transform(slot, Transform3D(
				Basis().scaled(Vector3(footprint, height, footprint)),
				Vector3(px, height * 0.5, pz)))

		var hp_max: float = float(_sim.e_hp_max[i])
		var fraction: float = 0.0 if hp_max <= 0.0 else clampf(float(_sim.e_hp[i]) / hp_max, 0.0, 1.0)
		var base := _enemy_class_colors[clampi(type_index, 0, _enemy_class_colors.size() - 1)]
		var shade := _enemy_hurt_color.lerp(base, fraction)
		if champion:
			shade = shade.lerp(_champion_tint, _champion_blend)
		mm.set_instance_color(slot, shade)
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
	var span := float(_world.get("projectile_sprite_span", 58.0))
	# The sim spawns a projectile at the turret's CENTRE, and a quad centred on
	# that position pokes out of the back of the mount on the frame it fires -
	# which is exactly what it looked like. The drawn centre is advanced along
	# the velocity so the art's tail clears the turret's own picture and the shot
	# reads as leaving the barrel. Render-only: the sim's position, and therefore
	# what a shot actually hits, is untouched.
	var advance := float(_world.get("projectile_muzzle_advance", 36.0))
	var tint := _color("projectile")
	var shown := 0
	var per_family := PackedInt32Array()
	per_family.resize(_tracer_layers.size())
	per_family.fill(0)

	for i in _sim.projectile_slot_bound():
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

		var family: int = _sim.p_family[i]
		var art_layer: MultiMeshInstance3D = null
		if family >= 0 and family < _tracer_layers.size():
			art_layer = _tracer_layers[family]
		if art_layer != null:
			# Authored tracer, turned to its heading and leaning with everything
			# else. White, because the art carries its own colour.
			var slot := per_family[family]
			per_family[family] = slot + 1
			var heading := atan2(dx, dz) + _forward_of(TRACER_ART[_sim.blueprint_name(family)])
			art_layer.multimesh.set_instance_transform(slot, Transform3D(
				_lean * Basis(Vector3.UP, heading).scaled(Vector3(span, 1.0, span)),
				Vector3(px + dx * advance, lift, pz + dz * advance)))
			art_layer.multimesh.set_instance_color(slot, Color.WHITE)
			continue

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
	for family in _tracer_layers.size():
		if _tracer_layers[family] != null:
			_tracer_layers[family].multimesh.visible_instance_count = per_family[family]

## Swing each barrel toward whatever its turret last fired at. Smoothed here
## rather than in the simulation: the sim's aim is instant and authoritative, and
## this is only how it looks.
## Track targets with the sprite layers.
##
## Rewrites only the basis, keeping the translation the board refresh already
## wrote - so this stays a per-frame rotation rather than a second place that
## decides where a turret stands.
func _turn_turret_sprites(turn: float) -> void:
	var growth := float(_world.get("platform_tier_growth", 0.28))
	var span := float(_world.get("turret_sprite_span", 96.0))
	var filled := PackedInt32Array()
	filled.resize(_turret_bodies.size())
	filled.fill(0)
	for i in _sim.t_count:
		var blueprint := _sim.platform_blueprint(i)
		if blueprint < 0 or blueprint >= _turret_bodies.size():
			continue
		var slot := filled[blueprint]
		filled[blueprint] = slot + 1
		var target := atan2(_sim.t_aim_x[i], _sim.t_aim_y[i])
		var current: float = _barrel_angle[i] if i < _barrel_angle.size() else target
		var delta := wrapf(target - current, -PI, PI)
		current = current + delta * clampf(turn * 0.0333, 0.0, 1.0)
		if i < _barrel_angle.size():
			_barrel_angle[i] = current
		var mm := _turret_bodies[blueprint].multimesh
		var reach := span * (1.0 + growth * float(_sim.platform_tier(i))) \
			* _turret_sprite_factor[blueprint]
		var xf := mm.get_instance_transform(slot)
		xf.basis = _lean * Basis(Vector3.UP,
			current + _forward_of(_sim.blueprint_name(blueprint))).scaled(
			Vector3(reach, 1.0, reach))
		mm.set_instance_transform(slot, xf)

func _update_barrels() -> void:
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

	# In sprite mode there is no separate muzzle to turn - the whole emplacement
	# is one picture - so the tracking is applied to the body layer and nothing
	# else in this function runs. The angle is still smoothed the same way, so a
	# sprite turret swings onto a target exactly as a built one did.
	if _sprite_turrets:
		_turn_turret_sprites(turn)
		return

	var filled := PackedInt32Array()
	filled.resize(_turret_barrels.size())
	filled.fill(0)

	for i in _sim.t_count:
		var blueprint := _sim.platform_blueprint(i)
		if blueprint < 0 or blueprint >= _turret_barrels.size():
			continue
		var target := atan2(_sim.t_aim_x[i], _sim.t_aim_y[i])
		var current: float = _barrel_angle[i] if i < _barrel_angle.size() else target
		# Shortest way round, so a turret never spins the long way to track a
		# target that crossed behind it.
		var delta := wrapf(target - current, -PI, PI)
		current = current + delta * clampf(turn * 0.0333, 0.0, 1.0)
		if i < _barrel_angle.size():
			_barrel_angle[i] = current

		var shape := _muzzle_shape(_sim.blueprint_name(blueprint))
		var scale := 1.0 + growth * float(_sim.platform_tier(i))
		var dx := sin(current)
		var dz := cos(current)
		var reach := barrel_length * scale * shape.x
		var girth := barrel_radius * scale * shape.y
		# Elevation is baked into the basis rather than applied as a separate
		# rotation: a mortar that lobs its shells has to look like it does, and a
		# railgun that does not has to look like it does not.
		var rise := sin(shape.z)
		var run := cos(shape.z)
		var lift := pad_height + body_height * scale * 0.78
		_basis = Basis(
			Vector3(dz * girth, 0.0, -dx * girth),
			Vector3(-dx * rise * girth, run * girth, -dz * rise * girth),
			Vector3(dx * run * reach, rise * reach, dz * run * reach))
		var mm := _turret_barrels[blueprint].multimesh
		var slot := filled[blueprint]
		mm.set_instance_transform(slot, Transform3D(_basis, Vector3(
			_sim.t_x[i] + dx * run * reach * 0.5,
			lift + rise * reach * 0.5,
			_sim.t_y[i] + dz * run * reach * 0.5)))
		var max_tier := maxi(_sim.platform_max_tier(blueprint) - 1, 1)
		var family := _family_colour(i, ballistic, cannon, suppressor, railgun)
		var tint := family.lerp(top_colour, float(_sim.platform_tier(i)) / float(max_tier))
		# A jammed turret is not firing and has to look like it is not firing, or
		# the player reads a hole in their line as bad luck. An overcharged one is
		# firing harder than anything else on the board, and glows like it.
		if _sim.platform_jammed(i):
			tint = tint.lerp(_color_of(_world.get("jammed", "#2a3038")),
				float(_world.get("jammed_blend", 0.72)))
		elif _sim.platform_overcharge_ticks(i) > 0:
			tint = tint.lerp(_color_of(_world.get("overcharged", "#ffe27a")),
				float(_world.get("overcharged_blend", 0.6)))
		mm.set_instance_color(slot, tint)
		filled[blueprint] = slot + 1

	for family_index in _turret_barrels.size():
		_turret_barrels[family_index].multimesh.visible_instance_count = filled[family_index]

## Which family a turret belongs to, for tinting.
##
## Asked in the right order on purpose: the Suppressor has a blast radius too, so
## testing "does it splash" first would paint every Suppressor as a Cannon. Read
## from behaviour rather than from the blueprint's name, so a fourth family that
## slows or splashes inherits sensible colours without touching this.
func _family_colour(index: int, ballistic: Color, cannon: Color, suppressor: Color,
		railgun: Color) -> Color:
	if _sim.platform_income(index) > 0:
		return _color_of(_world.get("platform_rig", "#7fb069"))
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
func effect_layer() -> MultiMeshInstance3D: return _fx
func link_layer() -> MultiMeshInstance3D: return _links
func drawn_link_count() -> int: return _links.multimesh.visible_instance_count
## Every effect currently drawn, across BOTH layers. The impact layer split
## made the single-layer count a lie: a landed shot drew on the impact layer,
## this still answered zero, and the feedback test rightly failed - the test
## was correct and the accessor was stale.
func drawn_effect_count() -> int:
	var total := _fx.multimesh.visible_instance_count
	if _fx_impact != null:
		total += _fx_impact.multimesh.visible_instance_count
	return total
func shake() -> float: return _shake
func camera_home() -> Vector3: return _camera_home
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

const MATERIALS_PATH := "res://data/materials.json"
const SPRITE_TURRET_DIR := "res://assets/art/turrets/"
const SPRITE_DRONE_DIR := "res://assets/art/drones/"
const SPRITE_FX_DIR := "res://assets/art/fx/"

## Authored sprites, by the id the game already holds - blueprint id for a
## turret, enemy id for a drone. Empty when the art is not present, and every
## call site falls back to the generated meshes, so the game still runs from a
## checkout with no assets/ directory.
var _sprites: Dictionary = {}

## Whether the turret families are being drawn as sprites. Read in several
## places that have to agree, so it is answered once.
var _sprite_turrets: bool = false
## Whether the turret art is split into pinned base + rotating head.
var _sprite_turret_split: bool = false
## Split-canvas to single-canvas span ratio, per family. 1.0 when unsplit.
var _turret_sprite_factor: PackedFloat32Array = PackedFloat32Array()
## And whether the drone classes are. Answered per class at build time and
## collapsed to one flag, because the transform loop runs per drone per frame
## and must not be branching on a dictionary lookup in there.
var _sprite_drones: bool = false
## The lean, computed once from the camera. See _sprite_lean().
var _lean := Basis()

## Which way each sprite's art actually points, as radians to ADD to its facing.
##
## The game turns sprites so that the picture's "up" points where the entity is
## going or aiming - which is only right when the art was drawn facing the top
## of the frame. Authored sheets do not reliably do that: the second sheet's
## turrets point left and its drones face the viewer. This is the correction,
## read from theme.json ("sprite_forward_degrees", id -> degrees) so a new sheet
## is a data edit rather than a code change. Missing id means zero.
var _forward: Dictionary = {}

func _forward_of(id: String) -> float:
	if _forward.has(id):
		return float(_forward[id])
	var table: Dictionary = _world.get("sprite_forward_degrees", {})
	var radians := deg_to_rad(float(table.get(id, 0.0)))
	_forward[id] = radians
	return radians
## How far a leaning sprite has to be raised, as a share of its own span, to keep
## its lower edge from sinking into the ground it is standing on.
var _lean_lift: float = 0.0

## Load a sprite, or null if the art for this id was never authored.
##
## Cached including the misses: ResourceLoader.exists() is a filesystem question
## and asking it per layer per level load is a hitch nobody would ever find.
func _sprite(dir: String, id: String) -> Texture2D:
	var key := dir + id
	if _sprites.has(key):
		return _sprites[key]
	var path := "%s%s.png" % [dir, id]
	var texture: Texture2D = null
	if ResourceLoader.exists(path):
		texture = ResourceLoader.load(path) as Texture2D
	_sprites[key] = texture
	return texture

## The axis a sprite leans back around, and how far.
##
## Sprites lying flat on the ground are foreshortened by the camera's pitch -
## at -40 degrees a circle becomes 64% as tall as it is wide - and with no
## thickness at all they read as decals painted on the floor. "Pancakes" was the
## word, and it was the right one.
##
## Leaning each sprite back toward the camera fixes most of it for free: at a 28
## degree lean against a 40 degree camera the sprite sits 22 degrees off
## square-on instead of 50, so it keeps its proportions AND reads as something
## standing up. The lean happens about the camera's own right-hand axis, which
## is fixed because this camera never rotates - so it is computed once, here,
## rather than per sprite per frame.
##
## Applied OUTSIDE the facing rotation: a turret still turns in the ground plane
## to track a target, and then the whole thing leans. The other order would swing
## the lean around with the barrel and make the turret wobble as it tracked.
func _sprite_lean() -> Basis:
	var yaw := deg_to_rad(float((_theme.get("camera", {}) as Dictionary).get(
		"yaw_degrees", -22.0)))
	var tilt := deg_to_rad(float(_world.get("sprite_lean_degrees", 28.0)))
	# The camera's right-hand axis in world space, from its yaw alone.
	var axis := Vector3(cos(yaw), 0.0, -sin(yaw)).normalized()
	# Positive, and that sign is the whole thing. The first attempt leaned the
	# sprites AWAY from the camera, taking them from 50 degrees off square-on to
	# 78 and rendering the board as a field of edge-on slivers. Captured, which is
	# the only reason it took one attempt rather than an afternoon of arguing
	# about basis conventions.
	return Basis(axis, tilt)

## A horizontal quad, one unit across, lying on the ground.
##
## PlaneMesh with FACE_Y rather than a QuadMesh rotated into place: the mesh is
## shared by every instance of a layer and baking the orientation in means the
## per-instance basis is free to carry only what actually varies, which is
## facing and scale.
func _sprite_plane() -> PlaneMesh:
	var plane := PlaneMesh.new()
	plane.size = Vector2.ONE
	plane.orientation = PlaneMesh.FACE_Y
	return plane

## The material an authored sprite is drawn with.
##
## Unshaded, and that is a decision rather than a shortcut. The art already has
## its light painted in - a rim from the upper left on every asset - and lighting
## it a second time with the board's sun turned the mid-tones muddy and made the
## drones read as grey blobs at distance. It is also the cheapest shading mode
## there is, which matters on a build that is fill-bound.
##
## ALPHA_SCISSOR rather than alpha blending, for the same reason: blended sprites
## have to be depth-sorted and they pay for every overlapping pixel. A cutout
## costs one discard and never sorts. The threshold is low enough to keep the
## Mender's glow arcs and the Suppressor's lightning, which are the two assets
## that would notice.
func _sprite_material(texture: Texture2D) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_texture = texture
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA_SCISSOR
	material.alpha_scissor_threshold = float(_world.get("sprite_cutout", 0.35))
	# The damage flash and the family tint still ride on the instance colour.
	material.vertex_color_use_as_albedo = true
	material.cull_mode = BaseMaterial3D.CULL_DISABLED
	material.texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR_WITH_MIPMAPS
	return material


## The surface families, or an empty set if the file is missing or malformed.
##
## Deliberately forgiving. Every other data file in this project is load-bearing
## and a typo in one has to stop the program with a plain message, because a
## silently-wrong number is a wrong game. This one is not: a family that fails to
## load costs a texture, and a renderer that refuses to start over a texture is a
## worse outcome than a flat-shaded wall.
func _load_families() -> Dictionary:
	if not FileAccess.file_exists(MATERIALS_PATH):
		return {}
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(MATERIALS_PATH))
	if typeof(parsed) != TYPE_DICTIONARY:
		push_warning("materials.json did not parse; surfaces will be flat")
		return {}
	var families: Variant = (parsed as Dictionary).get("families", {})
	if typeof(families) != TYPE_DICTIONARY:
		return {}
	return families as Dictionary

## Which detail tier the current quality level implies.
##
## One rung each, because the surface maps turned out to be worth roughly what
## the shadows are: measured in a browser, all three maps cost 48% of the frame
## at HIGH and 9% at BALANCED, and FAST was unchanged because it had already
## stopped paying for them. So BALANCED keeps the albedo and roughness variation
## - most of the look - and gives up the normal map, which is the expensive one.
func _material_detail() -> int:
	match _quality:
		QUALITY_FAST:
			return MaterialLibrary.DETAIL_PLAIN
		QUALITY_BALANCED:
			return MaterialLibrary.DETAIL_SIMPLE
		_:
			return MaterialLibrary.DETAIL_FULL

## A material for a static board surface, optionally carrying a generated
## surface family. Passing no family keeps the old flat behaviour exactly.
func _surface_material(tint: Color, metallic: float, roughness: float,
		family: String = "") -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = tint
	material.metallic = metallic
	material.roughness = roughness
	if family != "" and _materials != null:
		_materials.apply(material, family)
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

## A lighting value that may be tuned once per renderer.
##
## Returns "<key>_hdr" from the theme when running on Forward+ and that key
## exists, and plain "<key>" otherwise. The point is that a value can stay a
## single number for as long as both renderers agree about it, and only the ones
## that genuinely differ - so far, glow - pay for a second entry. A theme with no
## _hdr keys at all behaves exactly as it did before this existed.
func _lit(key: String, fallback: float) -> float:
	if _hdr and _world.has(key + "_hdr"):
		return float(_world[key + "_hdr"])
	return float(_world.get(key, fallback))

func _color(key: String) -> Color:
	return Color(str(_theme.get(key, "#ff00ff")))

func _color_of(value: Variant) -> Color:
	return Color(str(value))
