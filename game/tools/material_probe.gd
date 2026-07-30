extends SceneTree

## What the procedural surface maps cost, per family.
##
## The art pass added ten families of three maps each, all generated in GDScript
## at startup. That is a load-time cost paid on every machine and a memory cost
## paid for the whole session, and neither is visible from a screenshot - so it
## gets measured rather than assumed. Reports; it is not a gate.
##
##   godot --headless --path game --script res://tools/material_probe.gd

const MATERIALS_PATH := "res://data/materials.json"

func _initialize() -> void:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(MATERIALS_PATH))
	if typeof(parsed) != TYPE_DICTIONARY:
		print("materials.json did not parse")
		quit()
		return
	var families: Dictionary = (parsed as Dictionary).get("families", {})
	var library := MaterialLibrary.new({}, families)

	print("family        size   maps    ms     KiB")
	var total_ms := 0.0
	var total_kib := 0.0
	for name: String in families.keys():
		var spec: Dictionary = families[name]
		var size := int(spec.get("size", 96))
		var started := Time.get_ticks_usec()
		# apply() is the real entry point and the only one that generates.
		var material := StandardMaterial3D.new()
		var applied := library.apply(material, name)
		var ms := float(Time.get_ticks_usec() - started) / 1000.0
		# Two RGB8 maps and one L8, plus a third again for the mip chain.
		var kib := float(size * size * (3 + 3 + 1)) * 1.33 / 1024.0
		total_ms += ms
		total_kib += kib
		print("%-12s  %4d   %s   %6.1f  %6.1f"
			% [name, size, "yes" if applied else " no", ms, kib])

	print("")
	print("%d families, %.1f ms total, %.0f KiB" % [families.size(), total_ms, total_kib])
	print("cached: %d" % library.generated_count())

	# The cache is the reason a level load is not this expensive. Prove it is one.
	var again := Time.get_ticks_usec()
	for name: String in families.keys():
		library.apply(StandardMaterial3D.new(), name)
	print("second pass over all families: %.2f ms"
		% (float(Time.get_ticks_usec() - again) / 1000.0))
	quit()
