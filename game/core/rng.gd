class_name Rng
extends RefCounted

## The one and only source of randomness inside the simulation.
##
## Nothing in core/ or entities/ may call randf(), randi(), randomize() or touch
## a RandomNumberGenerator. Godot's global RNG is process-global, reseeded by the
## engine, and not guaranteed stable across versions - any of which silently
## destroys replays, seeded Daily Contracts and the headless balance sim.
## tests/test_sim_purity.gd fails the build if that rule is broken.
##
## Algorithm is PCG-XSH-RR 64/32 (O'Neill). Chosen because its whole state is a
## single 64-bit integer updated with wrapping integer ops, so it produces bit
## identical streams on every platform Godot targets. Every constant below is an
## algorithm constant, not a balance value.

const _MULTIPLIER: int = 6364136223846793005
const _DEFAULT_STREAM: int = 1442695040888963407
const _U32_MASK: int = 0xFFFFFFFF

var _state: int = 0
var _stream: int = 0
var _draws: int = 0

func _init(seed_value: int = 0, stream: int = _DEFAULT_STREAM) -> void:
	# `stream | 1` is required by PCG: the increment must be odd for the LCG to
	# reach full period.
	_stream = stream | 1
	_state = 0
	_step()
	_state = _state + seed_value
	_step()
	_draws = 0

## Number of values drawn so far. Part of the simulation state hash, so a
## desync in *how many* draws happened is caught even when the values collide.
func draws() -> int:
	return _draws

func state() -> int:
	return _state

## Logical (zero-filling) right shift. GDScript's >> is an arithmetic shift on
## signed 64-bit ints, which smears the sign bit into the result and would make
## the generator's output depend on whether the state happens to be "negative".
static func ushr(value: int, bits: int) -> int:
	if bits <= 0:
		return value
	if bits >= 64:
		return 0
	return (value >> bits) & ((1 << (64 - bits)) - 1)

func _step() -> void:
	# Wrapping 64-bit multiply-add. GDScript ints are two's-complement int64 and
	# overflow wraps, which is exactly the behaviour PCG expects.
	_state = _state * _MULTIPLIER + _stream

## Uniform 32-bit value in [0, 2^32).
func next_u32() -> int:
	var old := _state
	_step()
	_draws += 1
	var xorshifted := ushr(ushr(old, 18) ^ old, 27) & _U32_MASK
	var rot := ushr(old, 59) & 31
	if rot == 0:
		return xorshifted
	return (ushr(xorshifted, rot) | ((xorshifted << (32 - rot)) & _U32_MASK)) & _U32_MASK

## Uniform integer in [0, bound). Uses rejection sampling rather than a modulo,
## so the distribution has no bias and - more importantly here - the number of
## draws consumed is a pure function of the stream, keeping replays aligned.
func next_below(bound: int) -> int:
	if bound <= 1:
		return 0
	var threshold := (0x100000000 - bound) % bound
	while true:
		var r := next_u32()
		if r >= threshold:
			return r % bound
	return 0

## Uniform double in [0, 1). Built from the 32-bit integer stream by exact
## division by 2^32, so it is bit-identical everywhere; never from a float RNG.
func next_unit() -> float:
	return float(next_u32()) / 4294967296.0

## Uniform double in [-magnitude, +magnitude].
func next_symmetric(magnitude: float) -> float:
	return (next_unit() * 2.0 - 1.0) * magnitude
