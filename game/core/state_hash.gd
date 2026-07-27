class_name StateHash
extends RefCounted

## FNV-1a over the raw bytes of simulation state.
##
## Used by the determinism test and (later) by replay validation and the headless
## balance sim. Hashing raw bytes rather than rounded values is deliberate: a
## one-ULP drift in a single enemy's position is a desync, and the whole point of
## this hash is to catch it on the tick it happens instead of ten waves later.
##
## The constants below are the published FNV-1a 64-bit parameters, not balance
## values, which is why this file is exempt from the "no numeric literals"
## rule in tests/test_sim_purity.gd.

const OFFSET_BASIS: int = -3750763034362895579  # 14695981039346656037 as int64
const PRIME: int = 1099511628211

static func begin() -> int:
	return OFFSET_BASIS

static func mix_bytes(h: int, bytes: PackedByteArray) -> int:
	var acc := h
	for b in bytes:
		acc = (acc ^ b) * PRIME  # wrapping int64 multiply
	return acc

static func mix_int(h: int, value: int) -> int:
	var acc := h
	var v := value
	for _i in 8:
		acc = (acc ^ (v & 0xFF)) * PRIME
		v = Rng.ushr(v, 8)
	return acc

static func mix_float(h: int, value: float) -> int:
	# Bit-exact: reinterpret the double, never round or format it.
	var buf := PackedFloat64Array([value])
	return mix_bytes(h, buf.to_byte_array())
