class_name Sfx
extends Node

## Sound, synthesised in code.
##
## There are no audio files in this repository and there is not going to be one:
## every sample below is generated at startup from noise and swept sine tones, in
## about thirty lines of arithmetic. That buys three things worth more than
## fidelity - the web build stays the size it was, a family's sound can be tuned
## by editing a number in theme.json the same way its colour is, and a silent
## board becomes a board where you can hear the moment the line stops holding.
##
## Two rules it lives by:
##
## 1. IT NEVER TOUCHES THE SIMULATION. It is driven by the same tick-to-tick diff
##    the visual feedback layer uses, and it is optional - the renderer works
##    perfectly with no Sfx attached, which is exactly how the headless tests run.
## 2. IT IS RATE-LIMITED, HARD. A hundred and forty-four turrets firing three
##    times a second is four hundred shots a second; played faithfully that is not
##    a firing line, it is white noise. Each family gets one voice every few tens
##    of milliseconds and the rest are dropped, which is what makes sustained fire
##    read as sustained fire.

enum { SHOT, BLAST, IMPACT, WRECK, LEAK, BUILD, WIN, LOSS }

const MIX_RATE := 22050
## Enough voices that a busy moment layers, few enough that it cannot ever become
## a wall. Exceeding it drops the newest sound rather than cutting off a playing
## one, because a clipped explosion is more noticeable than a missing tick.
const VOICES := 20
## Waveform generation is seeded so a given build always sounds the same. Nothing
## downstream depends on it - this is not simulation randomness - but a game whose
## explosions differ between launches is a game that sounds broken.
const WAVE_SEED := 90210

var _players: Array[AudioStreamPlayer] = []
var _next_voice: int = 0
var _streams: Dictionary = {}
## Last time in seconds each throttle bucket let a sound through.
var _last_played: Dictionary = {}
var _gap: Dictionary = {}
var _muted: bool = false
var _master_db: float = -14.0
var _clock: float = 0.0

func setup(theme: Dictionary) -> void:
	var cfg: Dictionary = theme.get("audio", {})
	_master_db = float(cfg.get("master_db", -14.0))
	_muted = bool(cfg.get("start_muted", false))

	var rng := RandomNumberGenerator.new()
	rng.seed = WAVE_SEED

	# One shot sound per weapon family, keyed by blueprint id so a new family gets
	# a voice by being named here rather than by touching any of the logic.
	_streams["shot:ballistic"] = _mix(
		_noise(0.055, 34.0, 0.55, rng), _sweep(0.055, 320.0, 120.0, 30.0, 0.35))
	_streams["shot:cannon"] = _mix(
		_noise(0.16, 13.0, 0.5, rng), _sweep(0.16, 150.0, 42.0, 11.0, 0.75))
	_streams["shot:suppressor"] = _mix(
		_noise(0.09, 22.0, 0.16, rng), _sweep(0.09, 900.0, 1500.0, 18.0, 0.4))
	_streams["shot:railgun"] = _mix(
		_noise(0.05, 40.0, 0.22, rng), _sweep(0.28, 1800.0, 180.0, 9.0, 0.5))
	_streams["shot"] = _streams["shot:ballistic"]

	_streams["blast"] = _mix(
		_noise(0.34, 8.0, 0.75, rng), _sweep(0.34, 110.0, 30.0, 7.0, 0.85))
	_streams["impact"] = _noise(0.03, 70.0, 0.3, rng)
	_streams["wreck"] = _mix(
		_noise(0.11, 26.0, 0.42, rng), _sweep(0.11, 260.0, 70.0, 20.0, 0.3))
	# The one sound that is meant to be unwelcome: low, slow, and nothing else on
	# the board sounds remotely like it.
	_streams["leak"] = _sweep(0.6, 220.0, 62.0, 4.0, 0.9)
	_streams["build"] = _sweep(0.11, 480.0, 760.0, 14.0, 0.5)
	_streams["win"] = _sweep(0.7, 300.0, 620.0, 2.4, 0.7)
	_streams["loss"] = _sweep(1.1, 260.0, 55.0, 2.0, 0.8)

	# How long each bucket must wait before it may sound again. Shots are throttled
	# hardest because there are two orders of magnitude more of them than anything
	# else; a leak is never throttled at all, because every one of them matters.
	_gap["shot"] = float(cfg.get("shot_gap_seconds", 0.075))
	_gap["blast"] = float(cfg.get("blast_gap_seconds", 0.06))
	_gap["impact"] = float(cfg.get("impact_gap_seconds", 0.05))
	_gap["wreck"] = float(cfg.get("wreck_gap_seconds", 0.045))
	_gap["leak"] = 0.0
	_gap["build"] = 0.0
	_gap["win"] = 0.0
	_gap["loss"] = 0.0

	for _i in VOICES:
		var player := AudioStreamPlayer.new()
		player.bus = "Master"
		add_child(player)
		_players.append(player)

func _process(delta: float) -> void:
	# Its own clock rather than the engine's: throttling has to keep working when
	# the game is paused and the simulation is not advancing at all.
	_clock += delta

func muted() -> bool:
	return _muted

func toggle_mute() -> bool:
	_muted = not _muted
	if _muted:
		for player in _players:
			player.stop()
	return _muted

## Play one sound. `variant` names a weapon family for SHOT and is ignored
## otherwise. Silently does nothing if the bucket is still cooling down, if every
## voice is busy, or if the game is muted - all three are normal, not errors.
func play(kind: int, variant: String = "") -> void:
	if _muted or _players.is_empty():
		return
	var bucket := _bucket(kind)
	if not _would_play(kind):
		return
	# Armed here, on the decision, rather than after a voice is found. A sound
	# dropped for want of a voice was still this bucket's turn; re-offering it on
	# the very next tick would defeat the throttle exactly when the board is
	# busiest, which is the one time it has to work.
	_last_played[bucket] = _clock
	var stream: Variant = _streams.get(_key(kind, variant), _streams.get(bucket))
	if stream == null:
		return
	var player := _free_voice()
	if player == null:
		return
	# A player outside the tree cannot play, and asking it to is an engine error
	# rather than a no-op. Reachable from a headless harness that builds the node
	# before the tree is up; refusing quietly is the honest answer, since there is
	# nothing to hear either way.
	if not player.is_inside_tree():
		return
	player.stream = stream as AudioStream
	player.volume_db = _master_db
	# A little pitch variation, or repeated shots sound like one sample on loop -
	# which is exactly what they are.
	player.pitch_scale = 1.0 + (float((_next_voice * 37) % 13) - 6.0) * 0.012
	player.play()

## Whether this kind's throttle would currently let a sound through. Exposed so a
## test can measure the throttle without measuring the audio driver, which in a
## headless run is a stub that reports nothing.
func _would_play(kind: int) -> bool:
	var bucket := _bucket(kind)
	var gap: float = _gap.get(bucket, 0.05)
	return gap <= 0.0 or _clock - float(_last_played.get(bucket, -999.0)) >= gap

func _key(kind: int, variant: String) -> String:
	if kind == SHOT and not variant.is_empty():
		return "shot:%s" % variant
	return _bucket(kind)

func _bucket(kind: int) -> String:
	match kind:
		SHOT: return "shot"
		BLAST: return "blast"
		IMPACT: return "impact"
		WRECK: return "wreck"
		LEAK: return "leak"
		BUILD: return "build"
		WIN: return "win"
		_: return "loss"

func _free_voice() -> AudioStreamPlayer:
	# Round-robin from where we left off, so a busy moment spreads across the pool
	# instead of hammering voice 0.
	for i in VOICES:
		var index := (_next_voice + i) % VOICES
		if not _players[index].playing:
			_next_voice = (index + 1) % VOICES
			return _players[index]
	return null

# --- synthesis ----------------------------------------------------------------
#
# Two generators and a mixer. Everything above is built out of these three.

## Filtered white noise with an exponential decay. `decay` is in nepers per
## second: bigger is shorter. The one-pole smoothing is what turns a hiss into
## something with a body to it.
func _noise(seconds: float, decay: float, level: float, rng: RandomNumberGenerator) -> AudioStreamWAV:
	var count := int(seconds * float(MIX_RATE))
	var samples := PackedFloat32Array()
	samples.resize(count)
	var smoothed := 0.0
	for i in count:
		var t := float(i) / float(MIX_RATE)
		smoothed = smoothed * 0.62 + rng.randf_range(-1.0, 1.0) * 0.38
		samples[i] = smoothed * level * exp(-decay * t)
	return _wav(samples)

## A sine swept from one frequency to another, with an exponential decay. Phase is
## integrated rather than computed from t, or the sweep would fold back on itself.
func _sweep(seconds: float, from_hz: float, to_hz: float, decay: float, level: float) -> AudioStreamWAV:
	var count := int(seconds * float(MIX_RATE))
	var samples := PackedFloat32Array()
	samples.resize(count)
	var phase := 0.0
	for i in count:
		var t := float(i) / float(MIX_RATE)
		var progress := t / maxf(seconds, 0.0001)
		var hz: float = from_hz + (to_hz - from_hz) * progress
		phase += TAU * hz / float(MIX_RATE)
		samples[i] = sin(phase) * level * exp(-decay * t)
	return _wav(samples)

## Sum two samples, longest wins, clipped to the representable range.
func _mix(a: AudioStreamWAV, b: AudioStreamWAV) -> AudioStreamWAV:
	var left := _floats(a)
	var right := _floats(b)
	var count := maxi(left.size(), right.size())
	var out := PackedFloat32Array()
	out.resize(count)
	for i in count:
		var value := 0.0
		if i < left.size():
			value += left[i]
		if i < right.size():
			value += right[i]
		out[i] = clampf(value, -1.0, 1.0)
	return _wav(out)

func _wav(samples: PackedFloat32Array) -> AudioStreamWAV:
	var bytes := PackedByteArray()
	bytes.resize(samples.size() * 2)
	for i in samples.size():
		bytes.encode_s16(i * 2, int(clampf(samples[i], -1.0, 1.0) * 32000.0))
	var wav := AudioStreamWAV.new()
	wav.format = AudioStreamWAV.FORMAT_16_BITS
	wav.mix_rate = MIX_RATE
	wav.stereo = false
	wav.loop_mode = AudioStreamWAV.LOOP_DISABLED
	wav.data = bytes
	return wav

func _floats(wav: AudioStreamWAV) -> PackedFloat32Array:
	var bytes := wav.data
	var out := PackedFloat32Array()
	out.resize(bytes.size() / 2)
	for i in out.size():
		out[i] = float(bytes.decode_s16(i * 2)) / 32000.0
	return out
