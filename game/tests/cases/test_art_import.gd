extends TestCase

## A linter over the .import sidecars, expressed as a test.
##
## Every sprite in this game is a texture drawn onto a quad lying almost flat in
## a 3D scene, so it is always being minified - and two of Godot's texture import
## defaults are wrong for that, both of them silently.
##
##   mipmaps/generate=false. The renderer asks for LINEAR_WITH_MIPMAPS. With no
##   mipmap chain to sample, Godot does not warn and does not error; it quietly
##   falls back to plain linear, and the art shimmers and reads as pixelated.
##   Turning mipmaps ON also measured FASTER (84.0 -> 72.5 ms a frame), because
##   a minified texture with no mips thrashes the sampler cache.
##
##   detect_3d/compress_to=1. The first time a texture is used in 3D, Godot
##   REWRITES its own import settings to VRAM compression. On flat cel-shaded art
##   with hard outlines that is visible block noise, and it happens on a machine
##   nobody is watching - the checked-in sidecar changes underneath you.
##
## This is here because both were already fixed once and both came back. The fix
## was applied to the sprites the entity sheet produced; the split turret halves
## and the five tracers were generated later, got fresh default sidecars, and
## shipped for several releases with mipmaps off and VRAM compression armed. A
## setting that has to be re-applied by hand after every slicer run is a setting
## that will be wrong again, so it is asserted instead of remembered.
const ART_DIR := "res://assets/art"

## The authored sheets are kept next to the sprites they were cut from, for
## re-slicing. They are never loaded by the game, so their import settings do
## not matter and are not policed.
const SOURCE_PREFIX := "_source"

## Settings every drawn sprite must carry, and why the default is wrong.
const REQUIRED := {
	"mipmaps/generate=true":
		"sprites are minified in 3D; without mips the renderer's "
		+ "LINEAR_WITH_MIPMAPS filter silently degrades to plain linear",
	"detect_3d/compress_to=0":
		"leaving detect-3D on lets Godot rewrite this sidecar to VRAM "
		+ "compression on first use, which blocks the flat art's outlines",
}

func test_every_drawn_sprite_is_imported_for_3d() -> void:
	var sidecars := _sidecars(ART_DIR)
	assert_true(sidecars.size() >= 20,
		"expected the art tree to hold the turret, drone and fx sprites, found %d"
			% sidecars.size())
	for path in sidecars:
		var text := FileAccess.get_file_as_string(path)
		for setting in REQUIRED:
			assert_true(text.contains(setting),
				"%s must set %s - %s" % [path, setting, REQUIRED[setting]])

func test_the_split_turret_art_is_complete() -> void:
	# The renderer only pins the base and turns the gun when EVERY family has
	# both halves; one missing file drops all five back to rotating the whole
	# picture, which is the bug that put barrels out of the back of the mount.
	for family in ["ballistic", "cannon", "railgun", "rig", "suppressor"]:
		for part in ["", "_base", "_head"]:
			var path := "%s/turrets/%s%s.png" % [ART_DIR, family, part]
			assert_true(ResourceLoader.exists(path), "missing turret art: %s" % path)

func test_base_and_head_share_one_canvas() -> void:
	# The pinned base and the turning gun are drawn at the same span with no
	# per-family correction, which is only right because the slicer writes both
	# onto one square canvas with the mounting point at its centre. If they ever
	# differ, the gun draws at a different scale from the drum it sits on - and
	# it would look like a rendering bug rather than an art-pipeline one.
	var side := 0
	for family in ["ballistic", "cannon", "railgun", "rig", "suppressor"]:
		for part in ["_base", "_head"]:
			var texture: Texture2D = load("%s/turrets/%s%s.png" % [ART_DIR, family, part])
			assert_ne(texture, null, "%s%s failed to load" % [family, part])
			assert_eq(texture.get_width(), texture.get_height(),
				"%s%s is not square; a quad rotating about its centre needs it to be"
					% [family, part])
			if side == 0:
				side = texture.get_width()
			assert_eq(texture.get_width(), side,
				"%s%s is %d px but the sheet's canvas is %d px - every family must "
					% [family, part, texture.get_width(), side]
					+ "share one canvas or they draw at different sizes")

## Every .import sidecar under a directory, recursively, skipping the authored
## source sheets.
func _sidecars(from: String) -> PackedStringArray:
	var found := PackedStringArray()
	var dir := DirAccess.open(from)
	if dir == null:
		fail("art directory is missing: %s" % from)
		return found
	dir.list_dir_begin()
	var name := dir.get_next()
	while name != "":
		if dir.current_is_dir():
			found.append_array(_sidecars("%s/%s" % [from, name]))
		elif name.ends_with(".png.import") and not name.begins_with(SOURCE_PREFIX):
			found.append("%s/%s" % [from, name])
		name = dir.get_next()
	dir.list_dir_end()
	return found
