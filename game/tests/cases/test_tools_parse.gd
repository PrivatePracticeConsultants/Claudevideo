extends TestCase

## Every script in the project must at least compile.
##
## The dev tools live outside both the test suite and the shipped web export, so
## nothing loads them in normal work - which means a signature change elsewhere
## rots them silently. That is exactly what happened to tools/render_stress.gd
## when placement gained a blueprint argument: it sat broken until someone tried
## to run it.
##
## This does not test behaviour. It tests that the file still parses, which is
## the failure that was actually occurring.

const DIRECTORIES := ["res://tools", "res://core", "res://render", "res://ui", "res://tests"]

func test_every_script_in_the_project_compiles() -> void:
	var checked := 0
	for directory in DIRECTORIES:
		for path in _scripts_in(directory):
			var script: Script = load(path)
			assert_ne(script, null, "%s does not compile" % path)
			checked += 1
	assert_gt(float(checked), 0.0, "fixture sanity: some scripts were found")

func test_the_entry_point_and_scene_load() -> void:
	assert_ne(load("res://main.gd"), null, "main.gd compiles")
	assert_ne(load("res://main.tscn"), null, "the main scene loads")

func _scripts_in(directory: String) -> PackedStringArray:
	var out := PackedStringArray()
	var dir := DirAccess.open(directory)
	if dir == null:
		return out
	for file in dir.get_files():
		var name := file.trim_suffix(".remap")
		if name.ends_with(".gd"):
			out.append("%s/%s" % [directory, name])
	for sub in dir.get_directories():
		out.append_array(_scripts_in("%s/%s" % [directory, sub]))
	out.sort()
	return out
