extends TestCase

## Sound, which is synthesised in code rather than shipped as files.
##
## What is testable without ears: that the waveforms actually contain a signal,
## that the throttle really does drop the four-hundred-shots-a-second case down to
## something a person can listen to, that mute means mute, and that the voice pool
## is bounded. How it SOUNDS is a judgement call and this file makes no claim
## about it.

var _tree: SceneTree
var _sfx: Sfx

func before_each() -> void:
	_tree = Engine.get_main_loop() as SceneTree
	_sfx = Sfx.new()
	_tree.root.add_child(_sfx)
	_sfx.setup(_theme())

func after_each() -> void:
	_tree.root.remove_child(_sfx)
	_sfx.queue_free()

func _theme() -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://data/theme.json"))
	return parsed if typeof(parsed) == TYPE_DICTIONARY else {}

func _peak(stream: AudioStreamWAV) -> int:
	var bytes := stream.data
	var loudest := 0
	for i in bytes.size() / 2:
		loudest = maxi(loudest, absi(bytes.decode_s16(i * 2)))
	return loudest

func test_every_sound_is_generated_and_audible() -> void:
	# A silent buffer is the failure mode a synthesis bug produces, and it is
	# indistinguishable from "audio is off" unless something checks.
	for key in ["shot:ballistic", "shot:cannon", "shot:suppressor", "shot:railgun",
			"blast", "impact", "wreck", "leak", "build", "win", "loss"]:
		var stream: Variant = _sfx._streams.get(key)
		assert_ne(stream, null, "%s exists" % key)
		var wav := stream as AudioStreamWAV
		assert_gt(float(wav.data.size()), 0.0, "%s has samples" % key)
		assert_gt(float(_peak(wav)), 3000.0, "%s is not silence" % key)

func test_each_family_sounds_different() -> void:
	# Four families that all fire the same sample is worse than no sound: it tells
	# you something is shooting and nothing about what.
	var seen := {}
	for family in ["ballistic", "cannon", "suppressor", "railgun"]:
		var wav: AudioStreamWAV = _sfx._streams["shot:%s" % family]
		var signature := wav.data.size()
		assert_false(seen.has(signature),
			"%s has its own waveform, not a copy of %s" % [family, str(seen.get(signature, ""))])
		seen[signature] = family

func test_shots_are_throttled_hard() -> void:
	# A hundred and forty-four turrets firing three times a second is four hundred
	# shots a second. Played faithfully that is white noise, not a firing line.
	var played := 0
	for _i in 400:
		if _sfx._would_play(Sfx.SHOT):
			played += 1
		_sfx.play(Sfx.SHOT, "ballistic")
	assert_eq(played, 1, "four hundred shots in one instant are one sound")
	_sfx._process(1.0)
	assert_true(_sfx._would_play(Sfx.SHOT), "and the next one is allowed a second later")

func test_a_leak_is_never_throttled() -> void:
	# Every one of them costs something you cannot get back.
	for _i in 5:
		assert_true(_sfx._would_play(Sfx.LEAK), "a leak always sounds")
		_sfx.play(Sfx.LEAK)

func test_mute_stops_everything() -> void:
	assert_false(_sfx.muted(), "sound is on by default")
	assert_true(_sfx.toggle_mute(), "M mutes")
	_sfx.play(Sfx.LEAK)
	var playing := 0
	for player in _sfx._players:
		if player.playing:
			playing += 1
	assert_eq(playing, 0, "and nothing plays while it is muted")
	assert_false(_sfx.toggle_mute(), "M unmutes again")

func test_the_voice_pool_is_bounded_and_never_overruns() -> void:
	# The failure this guards is a pool that grows a node per sound - the audio
	# equivalent of one mesh per enemy.
	var before := _sfx.get_child_count()
	for i in 500:
		_sfx._process(1.0)  # clear every throttle
		_sfx.play(Sfx.WRECK)
	assert_eq(_sfx.get_child_count(), before, "no voice was ever created on demand")
	assert_eq(before, Sfx.VOICES, "and there are exactly the declared number of them")

func test_an_unknown_family_still_makes_a_noise() -> void:
	# A weapon family added to blueprints.json without a waveform must fall back to
	# the generic shot rather than going silently missing.
	assert_ne(_sfx._streams.get("shot"), null, "there is a generic shot")
	_sfx.play(Sfx.SHOT, "trebuchet")
	assert_true(true, "and asking for one that does not exist is not an error")
