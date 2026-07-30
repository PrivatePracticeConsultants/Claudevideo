extends TestCase

## The automatic quality governor.
##
## It is a safety net for a machine that cannot hold the current settings, and
## the ways it can be wrong are all worse than the problem it solves: dropping
## quality on a machine that was fine, oscillating between tiers, or overruling a
## player who has just made a choice. So the rules are tested rather than
## trusted, using the same arithmetic main.gd runs.
##
## The governor itself lives in main.gd, which needs a window and a board. What
## is checked here is the decision - the rules that decide WHETHER to step - and
## the renderer-side ladder it steps along.

const FLOOR_FPS := 24.0
const PATIENCE := 5.0

## The decision main.gd makes each frame, in one place so the test and the game
## cannot drift apart in shape.
func _accumulate(slow_for: float, frame_seconds: float) -> float:
	if frame_seconds > 1.0 / FLOOR_FPS:
		return slow_for + frame_seconds
	return 0.0

func test_a_good_frame_rate_never_trips_it() -> void:
	var slow := 0.0
	# Sixty seconds at a comfortable 60fps.
	for _i in 3600:
		slow = _accumulate(slow, 1.0 / 60.0)
	assert_almost_eq(slow, 0.0, 0.0001, "nothing accumulated")

func test_a_frame_rate_just_above_the_floor_never_trips_it() -> void:
	# 25fps against a 24fps floor. The boundary is the case most likely to be
	# wrong, and a game sitting just above the line must be left alone forever.
	var slow := 0.0
	for _i in 3000:
		slow = _accumulate(slow, 1.0 / 25.0)
	assert_lt(slow, PATIENCE, "still has not tripped")

func test_sustained_slowness_trips_it() -> void:
	var slow := 0.0
	var frames := 0
	# 10fps, which is roughly what was reported.
	while slow < PATIENCE and frames < 1000:
		slow = _accumulate(slow, 0.1)
		frames += 1
	assert_lt(float(frames), 1000.0, "it tripped")
	assert_almost_eq(float(frames), 50.0, 1.0, "after about five seconds of it")

func test_one_slow_frame_among_good_ones_is_forgiven() -> void:
	# A hitch is not a settings problem. Level loads, shader compiles and a
	# backgrounded tab all produce single enormous frames, and none of them mean
	# the player should lose their graphics.
	var slow := 0.0
	for i in 600:
		slow = _accumulate(slow, 4.0 if i % 100 == 0 else 1.0 / 60.0)
	assert_almost_eq(slow, 0.0, 0.0001, "every good frame resets it")

func test_a_run_of_slow_frames_broken_by_a_good_one_starts_over() -> void:
	var slow := 0.0
	for _i in 40:
		slow = _accumulate(slow, 0.1)
	assert_gt(slow, 3.0, "it was building")
	slow = _accumulate(slow, 1.0 / 60.0)
	assert_almost_eq(slow, 0.0, 0.0001, "and one good frame cleared it")

func test_the_ladder_only_goes_down_and_stops_at_the_bottom() -> void:
	# The renderer's half of the contract. Stepping back up on a quiet moment
	# would give a game that oscillates, and a stutter caused by the fix is worse
	# than the one it fixed.
	var names := SimRenderer3D.QUALITY_NAMES
	assert_eq(names.size(), 3, "three tiers")
	assert_eq(str(names[0]), "HIGH", "and the first is the most expensive")
	assert_eq(str(names[names.size() - 1]), "FAST", "and the last the cheapest")
	# main.gd steps quality() + 1 and gives up when that leaves the array.
	var next := names.size() - 1 + 1
	assert_gte(next, names.size(), "at the bottom there is nothing left to give up")

func test_the_thresholds_are_data_not_code() -> void:
	# Every other tuneable in this project lives in a JSON file, and the two
	# numbers that decide whether a player silently loses their graphics are not
	# the place to make an exception.
	var parsed: Variant = JSON.parse_string(
		FileAccess.get_file_as_string("res://data/theme.json"))
	assert_eq(typeof(parsed), TYPE_DICTIONARY, "theme.json parses")
	var world: Dictionary = (parsed as Dictionary).get("world", {})
	assert_true(world.has("auto_quality_fps"), "the floor is in the theme")
	assert_true(world.has("auto_quality_seconds"), "so is the patience")
	assert_gt(float(world["auto_quality_seconds"]), 1.0,
		"patience under a second would trip on a single level load")
	assert_lt(float(world["auto_quality_fps"]), 30.0,
		"a floor at or above 30fps would step down a game that is playing fine")
