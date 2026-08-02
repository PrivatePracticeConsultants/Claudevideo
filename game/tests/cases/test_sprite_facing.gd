extends TestCase

## The one thing about sprite rendering a player notices instantly: whether the
## gun points at what it is shooting.
##
## This has now been wrong twice, and both times it survived a green suite, a
## screenshot review and a release. The first time the whole turret picture was
## rotated, so a turret aiming down-screen drew upside down. That was fixed by
## splitting the art into a pinned base and a turning gun - and the gun still
## pointed the wrong way, because the error was never in the split. It was in
## the rotation itself:
##
##   PlaneMesh(FACE_Y) puts the texture's TOP edge at local -Z.
##   Basis(Vector3.UP, a) sends -Z to (-sin a, 0, -cos a).
##   facing is atan2(aim_x, aim_y), and to_world maps sim (x, y) to world (X, Z).
##
## Put together, rotating by `facing` alone points the art's top edge at
## (-aim_x, 0, -aim_y): exactly backwards. Every sheet's per-family offsets were
## then measured against art that was already drawing backwards, so each family
## absorbed a different share of the same 180 degrees and no two were wrong by
## the same amount - which is why it read as "some turrets look fine".
##
## So the convention is asserted here, from the plane's own vertex data outward,
## rather than trusted to survive the next sheet.

var _tree: SceneTree
var _sim: Sim
var _renderer: SimRenderer3D

## Every id that is rotated to face something, and where its art points in its
## own picture - degrees clockwise from the top of the frame. Deliberately a
## copy of what theme.json claims rather than a read of it: the test's job is to
## fail when the data and the art disagree, and data that checks itself cannot.
const AUTHORED := {
	"ballistic": 0.0, "cannon": 0.0, "railgun": 90.0, "rig": 62.2,
	"suppressor": 90.0, "proj_kinetic": 43.9, "proj_explosive": 42.5,
	"proj_energy": 44.9, "proj_arc": 44.5,
}

func before_each() -> void:
	_tree = Engine.get_main_loop() as SceneTree
	_sim = SimFixture.fresh()
	_renderer = SimRenderer3D.new()
	_tree.root.add_child(_renderer)
	_renderer.setup(_sim, _theme())

func after_each() -> void:
	_tree.root.remove_child(_renderer)
	_renderer.queue_free()

func test_the_renderer_actually_took_the_split_path() -> void:
	# Split art is all-or-nothing: one missing half drops all five families back
	# to rotating the whole picture, base and all. That fallback is deliberate -
	# it keeps a partial art drop running - but it is also silent, and it is the
	# exact thing that made a turret aiming down-screen draw upside down. If it
	# is ever taken on the shipped art, that is a broken build, not a fallback.
	assert_true(_renderer.turret_art_is_split(),
		"the shipped turret art must load as pinned base + turning gun")

func test_the_plane_still_puts_the_textures_top_edge_at_minus_z() -> void:
	# The load-bearing engine fact. If a Godot release ever changes it, every
	# sprite in the game silently turns around, and this is the line that says so.
	var plane := PlaneMesh.new()
	plane.size = Vector2.ONE
	plane.orientation = PlaneMesh.FACE_Y
	var arrays := plane.get_mesh_arrays()
	var verts: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
	var uvs: PackedVector2Array = arrays[Mesh.ARRAY_TEX_UV]
	var top := Vector3.ZERO
	var right := Vector3.ZERO
	var counted := 0
	for i in verts.size():
		if uvs[i].y < 0.01:
			top += verts[i]
			counted += 1
		if uvs[i].x > 0.99:
			right += verts[i]
	assert_eq(counted, 2, "a plane has two vertices along its top edge")
	assert_almost_eq((top / 2.0).normalized().z, -1.0, 0.0001,
		"texture up must be local -Z")
	assert_almost_eq((right / 2.0).normalized().x, 1.0, 0.0001,
		"texture right must be local +X")

func test_art_drawn_pointing_up_aims_at_its_target() -> void:
	# The plainest case, and the one that was broken: art whose barrel points at
	# the top of its own picture must end up pointing at the thing it is aiming
	# at, in world space, for every direction on the compass.
	for degrees in [0, 37, 90, 143, 180, 221, 270, 315]:
		var aim := Vector2(sin(deg_to_rad(float(degrees))), cos(deg_to_rad(float(degrees))))
		var drawn := _aimed("ballistic", aim)
		assert_almost_eq(drawn.x, aim.x, 0.0001,
			"aiming %d degrees points the art's top edge at the target (x)" % degrees)
		assert_almost_eq(drawn.z, aim.y, 0.0001,
			"aiming %d degrees points the art's top edge at the target (z)" % degrees)

func test_every_authored_offset_lands_the_barrel_on_the_target() -> void:
	# Same check for the ids whose art was NOT drawn pointing up - the Railgun's
	# rails point right, the Rig's boom up-and-right, every tracer up-and-right.
	# Each one's own barrel direction, not the sprite's top edge, has to arrive
	# at the target.
	for id in AUTHORED:
		var barrel: float = AUTHORED[id]
		for degrees in [0, 64, 128, 199, 285]:
			var aim := Vector2(sin(deg_to_rad(float(degrees))), cos(deg_to_rad(float(degrees))))
			var drawn := _aimed(id, aim, barrel)
			assert_almost_eq(drawn.x, aim.x, 0.0002,
				"%s aiming %d degrees puts its barrel on the target (x)" % [id, degrees])
			assert_almost_eq(drawn.z, aim.y, 0.0002,
				"%s aiming %d degrees puts its barrel on the target (z)" % [id, degrees])

func test_the_data_matches_the_art_the_test_was_written_against() -> void:
	# The offsets are read off the PNGs by eye and by measuring the alpha's
	# principal axis. If someone re-authors a sheet and edits theme.json without
	# re-measuring, this is what notices.
	var table: Dictionary = _theme().get("world", {}).get("sprite_barrel_degrees", {})
	for id in AUTHORED:
		assert_true(table.has(id), "theme.json is missing a barrel angle for %s" % id)
		assert_almost_eq(float(table.get(id, 999.0)), float(AUTHORED[id]), 0.05,
			"%s's authored barrel angle changed; re-measure it against the art" % id)

func test_a_backwards_convention_would_fail_this_test() -> void:
	# The check that the check works. Half a turn is exactly the mistake that
	# shipped, so the test must be able to see it.
	var aim := Vector2(0.0, 1.0)
	var backwards := _point("ballistic", _renderer.sprite_facing("ballistic",
		atan2(aim.x, aim.y)) + PI, 0.0)
	assert_almost_eq(backwards.z, -1.0, 0.0001,
		"half a turn from correct must point away from the target")

## Where the art's barrel ends up in world space, on the ground plane, when a
## sprite of this id is drawn aiming along `aim`.
func _aimed(id: String, aim: Vector2, barrel_degrees: float = 0.0) -> Vector3:
	return _point(id, _renderer.sprite_facing(id, atan2(aim.x, aim.y)), barrel_degrees)

func _point(id: String, rotation: float, barrel_degrees: float) -> Vector3:
	# The barrel in the sprite's own local space: the texture's top edge is -Z,
	# and "clockwise in the picture" turns it toward +X, which is a NEGATIVE
	# rotation about UP.
	var barrel := Basis(Vector3.UP, -deg_to_rad(barrel_degrees)) * Vector3(0.0, 0.0, -1.0)
	var world := Basis(Vector3.UP, rotation) * barrel
	return Vector3(world.x, 0.0, world.z).normalized()

func test_the_lean_does_not_change_where_a_turret_points() -> void:
	# Sprites are leaned toward the camera so they read as standing objects
	# rather than decals. That lean must not steer them: taking it back off an
	# instance's real transform has to leave the barrel on the target.
	var lean := _renderer.sprite_lean()
	var facing := _renderer.sprite_facing("ballistic", atan2(0.6, 0.8))
	var leaned := lean * Basis(Vector3.UP, facing) * Vector3(0.0, 0.0, -1.0)
	var flat := lean.inverse() * leaned
	assert_almost_eq(flat.x, 0.6, 0.0001, "un-leaning recovers the aim (x)")
	assert_almost_eq(flat.z, 0.8, 0.0001, "un-leaning recovers the aim (z)")

func test_a_sprite_nobody_turns_is_drawn_exactly_as_authored() -> void:
	# This sheet's drones are standing characters seen from the front, so they
	# are not rotated at all (sprite_rotate_drones is false). A sprite that is
	# never turned must get no facing correction either - the correction is only
	# meaningful as "turn the art's forward to where it is going". Adding it
	# anyway is half a turn applied to every drone on the board, and it looks
	# exactly like the art was exported upside down.
	assert_false(bool(_theme().get("world", {}).get("sprite_rotate_drones", true)),
		"this test is about the non-rotating case; if the sheet changes, so must it")
	assert_true(_renderer.drone_art_is_sprites(),
		"the drone art failed to load, so this test would prove nothing")
	# NOTE: this asks the renderer what rotation it will apply rather than
	# reading it back off the MultiMesh, and that is not squeamishness. Under
	# --headless the dummy rendering server does not keep the instance buffer:
	# set_instance_transform is accepted and get_instance_transform returns the
	# identity, for every layer, always. A test written the other way passes
	# whatever the renderer does, which is the same shape of nothing-test this
	# suite has been bitten by before.
	for type_index in _sim.enemy_type_count():
		assert_almost_eq(_renderer.drone_facing(type_index, false), 0.0, 0.0001,
			"%s is not rotated, so it must be drawn exactly as authored"
				% _sim.enemy_id(type_index))

func _theme() -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://data/theme.json"))
	return parsed if typeof(parsed) == TYPE_DICTIONARY else {}
