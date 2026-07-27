extends TestCase

## A linter, expressed as a test.
##
## Two of the project's non-negotiables are easy to state and easy to violate by
## accident, especially with a coding model generating most of the volume:
##
##   1. No balance constants in code.
##   2. Nothing non-deterministic inside the simulation.
##
## Both fail silently. A hardcoded `damage * 1.5` still runs; a stray randf()
## still runs, and only shows up months later as a replay that desyncs on one
## machine. So they are enforced mechanically here rather than trusted to review.
## This is the ancestor of tools/module_linter.py in phase P4.

## Every one of these has a correct alternative, named in the message, because a
## linter that only says "no" gets disabled.
const BANNED := {
	"randf": "use the seeded Rng service (Rng.next_unit)",
	"randi": "use the seeded Rng service (Rng.next_below)",
	"randomize": "the sim is seeded explicitly; never reseed globally",
	"RandomNumberGenerator": "use core/rng.gd - Godot's RNG is not replay-stable",
	"Vector2": "Vector2 is float32; the sim uses float64 scalars for determinism",
	"Vector3": "the sim is 2D and float64",
	"Time.": "the tick is the only clock inside the simulation",
	"OS.get_ticks": "the tick is the only clock inside the simulation",
	"Engine.get_frames": "simulation must not depend on frame count",
	"get_overlapping": "use the SpatialHash broadphase, not the physics server",
	"get_process_delta": "the tick is fixed; there is no delta in the sim",
	"sin(": "not bit-reproducible across libm; the path tables avoid trigonometry",
	"cos(": "not bit-reproducible across libm; the path tables avoid trigonometry",
	"atan": "not bit-reproducible across libm; the path tables avoid trigonometry",
	"pow(": "not bit-reproducible across libm; multiply iteratively instead",
	"randfn": "use the seeded Rng service",
}

## Every simulation file must be deterministic.
const SIM_FILES := [
	"res://core/sim.gd",
	"res://core/spatial_hash.gd",
	"res://core/rng.gd",
	"res://core/database.gd",
]

## Gameplay files, where a bare number is almost certainly a balance value that
## belongs in /data.
##
## rng.gd, state_hash.gd and database.gd are exempt by design and not by
## convenience: rng.gd and state_hash.gd are published algorithm constants
## (PCG and FNV-1a), and database.gd's literals are schema floors used to
## validate the data files rather than values the game plays with.
const NUMERIC_FILES := ["res://core/sim.gd", "res://core/spatial_hash.gd"]

## Indices, array steps and the identity element. Anything else is a balance
## value and belongs in JSON.
const ALLOWED_NUMBERS := ["0", "1", "2", "0.0", "1.0", "2.0"]

func test_no_nondeterminism_in_simulation_code() -> void:
	for path in SIM_FILES:
		var source := _strip(_read(path))
		for token in BANNED:
			assert_false(source.contains(token),
				"%s uses `%s` - %s" % [path, token, BANNED[token]])

func test_no_balance_constants_in_gameplay_code() -> void:
	var number := RegEx.new()
	# Lookbehind keeps identifiers out of it: the 32 in PackedInt32Array and the
	# 64 in PackedFloat64Array are type names, not numbers.
	number.compile("(?<![A-Za-z0-9_.])(\\d+\\.\\d+|\\d+)")
	for path in NUMERIC_FILES:
		var lines := _strip(_read(path)).split("\n")
		for line_number in lines.size():
			var line: String = lines[line_number]
			for m in number.search_all(line):
				var literal := m.get_string()
				if ALLOWED_NUMBERS.has(literal):
					continue
				fail("%s:%d has the literal `%s` - balance values belong in /data JSON. Line: %s"
					% [path, line_number + 1, literal, line.strip_edges()])

func test_the_linter_actually_catches_things() -> void:
	# A linter nobody has seen fail is a linter that might be matching nothing.
	var fake := "var x = randf()\nvar v = Vector2(1, 2)\nvar d = 3.75"
	var stripped := _strip(fake)
	assert_true(stripped.contains("randf"), "banned-token scan sees live code")
	assert_true(stripped.contains("Vector2"), "banned-token scan sees live code")
	# ...and that it does not fire on comments or strings, which is what makes
	# the doc comments in sim.gd (which name Vector2 and pow) legal.
	var commented := "# never use randf() here\nvar name = \"pow(\"\nvar ok = 1"
	var clean := _strip(commented)
	assert_false(clean.contains("randf"), "comments are stripped before scanning")
	assert_false(clean.contains("pow("), "string literals are stripped before scanning")

func _read(path: String) -> String:
	assert_true(FileAccess.file_exists(path), "linted file is missing: %s" % path)
	return FileAccess.get_file_as_string(path)

## Remove comments and string literal contents, so documentation that *names* a
## banned construct in order to explain why it is banned does not trip the scan.
func _strip(source: String) -> String:
	var out := ""
	for raw_line in source.split("\n"):
		var line: String = raw_line
		var cleaned := ""
		var quote := ""
		var i := 0
		while i < line.length():
			var c := line[i]
			if quote != "":
				if c == "\\":
					i += 2
					continue
				if c == quote:
					quote = ""
				# characters inside a string are dropped entirely
			elif c == "\"" or c == "'":
				quote = c
			elif c == "#":
				break
			else:
				cleaned += c
			i += 1
		out += cleaned + "\n"
	return out
