class_name TestCase
extends RefCounted

## Minimal xUnit-style base class.
##
## Why not GUT: GUT is the right long-term choice and the assertion names below
## deliberately mirror its API (assert_eq / assert_true / assert_almost_eq) so
## swapping it in is a mechanical change. It could not be vendored in the
## environment this phase was built in - the GitHub archive and API endpoints
## needed to fetch it are blocked - and shipping untested code to avoid a
## hundred lines of harness would have been the worse trade. See DECISIONS.md.

var failures: PackedStringArray = PackedStringArray()
var assertions: int = 0

## Run before every test method. Override for per-test fixtures.
func before_each() -> void:
	pass

func after_each() -> void:
	pass

func _fail(message: String) -> void:
	failures.append(message)

func fail(message: String) -> void:
	_fail(message)

func assert_true(value: bool, message: String = "") -> void:
	assertions += 1
	if not value:
		_fail("expected true, got false. %s" % message)

func assert_false(value: bool, message: String = "") -> void:
	assertions += 1
	if value:
		_fail("expected false, got true. %s" % message)

func assert_eq(actual: Variant, expected: Variant, message: String = "") -> void:
	assertions += 1
	if actual != expected:
		_fail("expected %s, got %s. %s" % [str(expected), str(actual), message])

func assert_ne(actual: Variant, unexpected: Variant, message: String = "") -> void:
	assertions += 1
	if actual == unexpected:
		_fail("expected something other than %s. %s" % [str(unexpected), message])

func assert_gt(actual: float, floor_value: float, message: String = "") -> void:
	assertions += 1
	if actual <= floor_value:
		_fail("expected > %s, got %s. %s" % [str(floor_value), str(actual), message])

func assert_gte(actual: float, floor_value: float, message: String = "") -> void:
	assertions += 1
	if actual < floor_value:
		_fail("expected >= %s, got %s. %s" % [str(floor_value), str(actual), message])

func assert_lt(actual: float, ceil_value: float, message: String = "") -> void:
	assertions += 1
	if actual >= ceil_value:
		_fail("expected < %s, got %s. %s" % [str(ceil_value), str(actual), message])

func assert_lte(actual: float, ceil_value: float, message: String = "") -> void:
	assertions += 1
	if actual > ceil_value:
		_fail("expected <= %s, got %s. %s" % [str(ceil_value), str(actual), message])

func assert_almost_eq(actual: float, expected: float, tolerance: float, message: String = "") -> void:
	assertions += 1
	if absf(actual - expected) > tolerance:
		_fail("expected %s +/- %s, got %s. %s" % [str(expected), str(tolerance), str(actual), message])
