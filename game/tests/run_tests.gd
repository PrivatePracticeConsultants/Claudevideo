extends SceneTree

## Headless test entry point.
##
##   godot --headless --path game --script res://tests/run_tests.gd
##
## Exits non-zero when anything fails, so it drops straight into CI.

const CASE_DIR := "res://tests/cases"

func _initialize() -> void:
	var files := _discover(CASE_DIR)
	if files.is_empty():
		printerr("No test cases found in %s" % CASE_DIR)
		quit(1)
		return

	var total := 0
	var failed := 0
	var assertions := 0
	var started := Time.get_ticks_msec()

	for path in files:
		var script: Script = load(path)
		if script == null:
			printerr("Could not load test script: %s" % path)
			failed += 1
			continue
		var case_name := path.get_file().get_basename()
		for method in script.get_script_method_list():
			var method_name: String = method["name"]
			if not method_name.begins_with("test_"):
				continue
			total += 1
			var instance: TestCase = script.new()
			instance.before_each()
			instance.call(method_name)
			instance.after_each()
			assertions += instance.assertions
			if instance.failures.is_empty():
				print("  ok    %s.%s" % [case_name, method_name])
			else:
				failed += 1
				print("  FAIL  %s.%s" % [case_name, method_name])
				for f in instance.failures:
					print("          %s" % f)

	var elapsed := Time.get_ticks_msec() - started
	print("")
	print("%d tests, %d assertions, %d failed  (%d ms)" % [total, assertions, failed, elapsed])
	quit(1 if failed > 0 else 0)

func _discover(dir_path: String) -> PackedStringArray:
	var out := PackedStringArray()
	var dir := DirAccess.open(dir_path)
	if dir == null:
		return out
	for file in dir.get_files():
		# Exported projects rename .gd to .gd.remap; tolerate both so the suite
		# can also be run against a packed build.
		var name := file.trim_suffix(".remap")
		if name.ends_with(".gd"):
			out.append("%s/%s" % [dir_path, name])
	out.sort()
	return out
