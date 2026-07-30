extends TestCase

## The procedural surface families.
##
## Textures are cosmetic, so none of this asserts that anything LOOKS right -
## that is what tools/capture_screenshot.gd is for. What it does assert is the
## three things that would silently cost a player something: that the maps are
## generated once rather than per level, that a broken data file degrades to a
## flat surface instead of taking the renderer down, and that the FAST tier
## actually stops paying for them.

const MATERIALS_PATH := "res://data/materials.json"

func _families() -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(MATERIALS_PATH))
	if typeof(parsed) != TYPE_DICTIONARY:
		return {}
	return (parsed as Dictionary).get("families", {})

func test_the_shipped_file_parses_and_lists_families() -> void:
	var families := _families()
	assert_gt(float(families.size()), 0.0, "materials.json lists families")

func test_every_family_the_renderer_asks_for_exists() -> void:
	# The renderer names families as string literals. A rename in the data file
	# that missed one would silently drop that surface back to flat, and nothing
	# else in the suite would notice.
	var families := _families()
	for name in ["ground", "verge", "road", "wall", "turret", "enemy",
			"rock", "bark", "canopy", "prop"]:
		assert_true(families.has(name), "materials.json defines '%s'" % name)

func test_applying_a_family_sets_all_three_maps() -> void:
	var library := MaterialLibrary.new({}, _families())
	var material := StandardMaterial3D.new()
	assert_true(library.apply(material, "turret"), "the family applied")
	assert_true(material.albedo_texture != null, "albedo map")
	assert_true(material.normal_texture != null, "normal map")
	assert_true(material.roughness_texture != null, "roughness map")
	assert_true(material.normal_enabled, "and the normal map is switched on")

func test_the_roughness_scalar_is_neutral_when_a_map_carries_it() -> void:
	# Godot multiplies the scalar by the map. A family whose scalar stayed at its
	# themed value would have its roughness applied twice and come out glassy.
	var library := MaterialLibrary.new({}, _families())
	var material := StandardMaterial3D.new()
	material.roughness = 0.3
	library.apply(material, "wall")
	assert_almost_eq(material.roughness, 1.0, 0.0001,
		"the map is the roughness, so the scalar must not scale it")

func test_a_family_is_generated_once_however_many_materials_ask() -> void:
	# This is the whole reason the library outlives the board. Generating the ten
	# families measures about a quarter of a second, and a level load that paid it
	# again would be a visible freeze on each of the 48 acts.
	var library := MaterialLibrary.new({}, _families())
	for _i in 5:
		library.apply(StandardMaterial3D.new(), "enemy")
	assert_eq(library.generated_count(), 1, "one family generated, not five")

func test_the_maps_are_shared_rather_than_copied() -> void:
	var library := MaterialLibrary.new({}, _families())
	var first := StandardMaterial3D.new()
	var second := StandardMaterial3D.new()
	library.apply(first, "rock")
	library.apply(second, "rock")
	assert_true(first.albedo_texture == second.albedo_texture,
		"both materials point at the same texture")

func test_the_fast_tier_applies_nothing() -> void:
	var library := MaterialLibrary.new({}, _families(), MaterialLibrary.DETAIL_PLAIN)
	var material := StandardMaterial3D.new()
	assert_false(library.apply(material, "ground"), "nothing applied")
	assert_true(material.albedo_texture == null, "and no map was built")
	assert_eq(library.generated_count(), 0, "so nothing was generated either")

func test_changing_tier_reports_whether_it_moved() -> void:
	# The renderer rebuilds every material-owning node on a true, and that is an
	# expensive thing to do on a window resize.
	var library := MaterialLibrary.new({}, _families())
	assert_false(library.set_detail(MaterialLibrary.DETAIL_FULL), "already there")
	assert_true(library.set_detail(MaterialLibrary.DETAIL_PLAIN), "moved")
	assert_false(library.set_detail(MaterialLibrary.DETAIL_PLAIN), "and stays there")

func test_the_middle_tier_keeps_the_surface_and_drops_the_normal_map() -> void:
	# The point of the middle rung. The normal map is the expensive map both to
	# generate and to sample; the albedo and roughness variation is most of what
	# stops a surface reading as plastic, and it survives here.
	var library := MaterialLibrary.new({}, _families(), MaterialLibrary.DETAIL_SIMPLE)
	var material := StandardMaterial3D.new()
	assert_true(library.apply(material, "wall"), "the family still applies")
	assert_true(material.albedo_texture != null, "albedo kept")
	assert_true(material.roughness_texture != null, "roughness kept")
	assert_true(material.normal_texture == null, "normal map dropped")
	assert_false(material.normal_enabled, "and not left switched on with no map")

func test_dropping_to_plain_releases_the_generated_maps() -> void:
	var library := MaterialLibrary.new({}, _families())
	library.apply(StandardMaterial3D.new(), "road")
	assert_eq(library.generated_count(), 1, "generated")
	library.set_detail(MaterialLibrary.DETAIL_PLAIN)
	assert_eq(library.generated_count(), 0, "and released on the way down")

func test_an_unknown_family_is_refused_rather_than_crashing() -> void:
	var library := MaterialLibrary.new({}, _families())
	var material := StandardMaterial3D.new()
	assert_false(library.apply(material, "no_such_surface"), "refused")
	assert_true(material.albedo_texture == null, "and left alone")

func test_an_empty_data_set_degrades_to_flat_surfaces() -> void:
	# The failure mode that matters: materials.json missing or malformed must cost
	# textures, not the game. Every other data file in this project is allowed to
	# stop the program; this one is not.
	var library := MaterialLibrary.new({}, {})
	assert_false(library.has_family("ground"), "no families")
	assert_false(library.apply(StandardMaterial3D.new(), "ground"), "and none applied")

func test_a_family_missing_every_optional_key_still_generates() -> void:
	# The spec is almost all optional, and a hand-written family that lists only a
	# size must not divide by zero or read past the end of a shorter array.
	var library := MaterialLibrary.new({}, {"bare": {"size": 16}})
	var material := StandardMaterial3D.new()
	assert_true(library.apply(material, "bare"), "the defaults carried it")
	assert_true(material.albedo_texture != null, "and it produced a map")

func test_a_zero_lattice_does_not_divide_by_zero() -> void:
	var library := MaterialLibrary.new({},
		{"broken": {"size": 8, "lattice_x": [0, 0], "lattice_y": [0], "weights": [1.0, 1.0]}})
	assert_true(library.apply(StandardMaterial3D.new(), "broken"), "clamped, not crashed")

func test_more_weights_than_lattices_is_survivable() -> void:
	var library := MaterialLibrary.new({},
		{"ragged": {"size": 8, "lattice_x": [4], "weights": [0.5, 0.3, 0.2]}})
	assert_true(library.apply(StandardMaterial3D.new(), "ragged"), "the shorter list wins")

func test_the_maps_are_the_same_every_run() -> void:
	# Determinism is cosmetic here and still worth having: every screenshot
	# comparison in the dev tools assumes two runs of the same board produce the
	# same pixels, and a wandering texture would quietly break all of them.
	var a := MaterialLibrary.new({}, _families())
	var b := MaterialLibrary.new({}, _families())
	var first := StandardMaterial3D.new()
	var second := StandardMaterial3D.new()
	a.apply(first, "bark")
	b.apply(second, "bark")
	var left := (first.albedo_texture as ImageTexture).get_image()
	var right := (second.albedo_texture as ImageTexture).get_image()
	assert_true(left.get_data() == right.get_data(), "identical bytes")

func test_two_families_with_different_seeds_are_different_surfaces() -> void:
	var spec := {"size": 32, "lattice_x": [4, 9], "weights": [0.6, 0.4]}
	var one := spec.duplicate()
	one["seed"] = 1
	var two := spec.duplicate()
	two["seed"] = 900
	var library := MaterialLibrary.new({}, {"one": one, "two": two})
	var a := StandardMaterial3D.new()
	var b := StandardMaterial3D.new()
	library.apply(a, "one")
	library.apply(b, "two")
	var left := (a.albedo_texture as ImageTexture).get_image()
	var right := (b.albedo_texture as ImageTexture).get_image()
	assert_true(left.get_data() != right.get_data(), "the seed actually shifts the field")

func test_the_maps_carry_mipmaps() -> void:
	# Without them the far half of the board crawls with sampling noise whenever
	# the camera moves, which reads as a frame-rate problem rather than a texture
	# one - so it is worth pinning rather than trusting.
	var library := MaterialLibrary.new({}, _families())
	var material := StandardMaterial3D.new()
	library.apply(material, "ground")
	assert_true((material.albedo_texture as ImageTexture).get_image().has_mipmaps(),
		"albedo is mipmapped")

func test_panel_grooves_only_appear_where_the_family_asks_for_them() -> void:
	# Grooves are what separates a manufactured surface from a natural one, and a
	# grid appearing on rock would be worse than no texture at all.
	var families := _families()
	for natural in ["ground", "rock", "bark", "canopy"]:
		var spec: Dictionary = families[natural]
		assert_eq(int(spec.get("panel_depth", 0)), 0,
			"%s is a natural surface and must have no panel grooves" % natural)

func test_the_ground_family_does_not_retile_a_mesh_that_already_tiled() -> void:
	# The ground's UVs are world-space and already divided by ground_tile. A
	# uv_scale other than 1.0 would tile an already-tiled surface and turn the
	# detail into aliasing noise.
	var spec: Dictionary = _families()["ground"]
	assert_almost_eq(float(spec.get("uv_scale", 1.0)), 1.0, 0.0001,
		"the ground mesh does its own tiling")
