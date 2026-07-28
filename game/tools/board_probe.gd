extends SceneTree

## Play one board's four acts in a chain and idle-test only the last of them.
##
## The full probe with --idle doubles the work and does not fit a single run at
## this board size. Act 4 is the one that matters for the idle question anyway:
## it inherits three acts of building, so if any act in the campaign can be won by
## an inherited board with no input, it is this one.
##
##   godot --headless --path game --script res://tools/board_probe.gd -- --from 44

func _init() -> void:
	var start := 0
	var argv := OS.get_cmdline_user_args()
	for i in argv.size():
		if argv[i] == "--from" and i + 1 < argv.size():
			start = int(argv[i + 1])
	var levels := Database.load_levels()
	var carry := {}
	var last := mini(start + 4, levels.size()) - 1
	for index in range(start, last + 1):
		var level: Dictionary = levels[index]
		var sim := SimFixture.start_act(level, carry, 20260727 + index)
		var inherited := sim.t_count
		SimFixture.run_greedy(sim)
		var won := sim.integrity() > 0 and sim.is_over()
		print("%-4d%-18s%9s%8d%8d%8d%8d%7d" % [index, level["engagement"],
			"WIN" if won else "LOSS", sim.integrity(), sim.kills(), sim.leaks(),
			sim.t_count, inherited])
		if index == last:
			var idle := SimFixture.idle_run(level, carry, 20260727 + index)
			print("     idle: integrity %d, leaks %d -> %s" % [idle.integrity(),
				idle.leaks(), "SURVIVED (trivial)" if idle.integrity() > 0 else "lost, as it should"])
		if not won:
			break
		carry = sim.board_snapshot()
	quit(0)
