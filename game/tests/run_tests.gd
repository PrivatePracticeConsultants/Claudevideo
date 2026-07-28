extends SceneTree

## Headless test entry point.
##
##   godot --headless --path game --script res://tests/run_tests.gd
##   ... -- --case test_engagement       # one file, by name
##
## Exits non-zero when anything fails, so it drops straight into CI.
##
## The filter exists because the full-campaign run (LASTLINE_FULL_CAMPAIGN=1) is
## 24 engagements played end to end and outgrew the wall-clock budget of a single
## invocation. Being able to run the expensive gate on its own is the difference
## between running it before a release and not running it.

const CASE_DIR := "res://tests/cases"

func _initialize() -> void:
	var only := ""
	var argv := OS.get_cmdline_user_args()
	for i in argv.size():
		if argv[i] == "--case" and i + 1 < argv.size():
			only = argv[i + 1]
	var files := _discover(CASE_DIR)
	if not only.is_empty():
		var kept := PackedStringArray()
		for path in files:
			if path.get_file().get_basename() == only:
				kept.append(path)
		if kept.is_empty():
			printerr("No test case named %s in %s" % [only, CASE_DIR])
			quit(1)
			return
		files = kept
	if files.is_empty():
		printerr("No test cases found in %s" % CASE_DIR)
		quit(1)
		return

	var total := 0
	var failed := 0
	var assertions := 0
	var started := Time.get_ticks_msec()

	for path in files:
		# A test file that will not parse must FAIL, loudly. Counting it as
		# "nothing to run" is how a whole file of tests disappears from the suite
		# without anyone noticing the total went down - which is exactly what
		# happened once during this project.
		var script: Script = load(path)
		if script == null:
			print("  FAIL  %s could not be loaded (parse error - see above)" % path.get_file())
			failed += 1
			total += 1
			continue
		var methods := script.get_script_method_list()
		if methods.is_empty():
			print("  FAIL  %s defines no tests" % path.get_file())
			failed += 1
			total += 1
			continue
		var case_name := path.get_file().get_basename()
		for method in methods:
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
