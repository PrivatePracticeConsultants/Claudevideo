extends SceneTree

## Play the whole campaign the way a player would - chained, carrying each
## board forward - and print what happened.
##
## The suite's balance test asserts winnable-and-losable on a sample; this prints
## the full table, which is what you actually want while tuning. It is a dev
## tool, not a test: it never fails, it just reports.
##
##   godot --headless --path game --script res://tools/balance_probe.gd
##   ... -- --level 7          # one level only, started clean
##   ... -- --idle             # also measure the idle-loss side

func _init() -> void:
	var only := -1
	var check_idle := false
	var sweep := -1
	var argv := OS.get_cmdline_user_args()
	for i in argv.size():
		if argv[i] == "--level" and i + 1 < argv.size():
			only = int(argv[i + 1])
		elif argv[i] == "--sweep" and i + 1 < argv.size():
			sweep = int(argv[i + 1])
		elif argv[i] == "--idle":
			check_idle = true
	if sweep >= 0:
		_sweep(sweep)
		quit(0)
		return

	var levels := Database.load_levels()
	print("%-4s%-18s%9s%8s%8s%8s%8s%7s%7s" % ["#", "engagement", "result",
		"integ", "kills", "leaks", "built", "tier4", "carry"])
	var carry := {}
	var losses := 0
	for index in levels.size():
		if only >= 0 and index != only:
			continue
		var level: Dictionary = levels[index]
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		if not db.errors.is_empty():
			print("%-4d%-18s  LOAD FAILED: %s" % [index, level["engagement"], db.errors[0]])
			losses += 1
			continue
		# Through SimFixture rather than re-implemented here. A tool that carries
		# its own copy of a policy is a tool that quietly stops matching the one
		# the suite measures - it has happened twice in this project already.
		var sim := SimFixture.start_act(level, carry, 20260727 + index)
		var inherited := sim.t_count
		SimFixture.run_greedy(sim)

		var tier4 := 0
		for i in sim.t_count:
			if sim.platform_tier(i) >= 3:
				tier4 += 1
		var won := sim.integrity() > 0 and sim.is_over()
		if not won:
			losses += 1
		print("%-4d%-18s%9s%8d%8d%8d%8d%7d%7d" % [index, level["engagement"],
			"WIN" if won else "LOSS", sim.integrity(), sim.kills(), sim.leaks(),
			sim.t_count, tier4, inherited])

		if check_idle:
			# From the board it inherits, not from an empty one - that is the
			# question worth asking once boards carry forward.
			var idle := SimFixture.idle_run(level, carry, 20260727 + index)
			if idle.integrity() > 0:
				print("     ^ IDLE SURVIVED - level is trivial")
				losses += 1

		carry = sim.board_snapshot() if won else {}
	print("levels not won: %d" % losses)
	quit(0)

## Replay one level across a range of health multipliers.
##
## The campaign's difficulty band turned out to be a few percent wide - x6.52
## wins without a scratch and x6.76 loses by nine leaks on the same board - which
## makes tuning by argument hopeless and tuning by measurement cheap. One process
## gives the whole curve for a level, so the shape of the cliff is visible rather
## than inferred from two points either side of it.
##
##   godot --headless --path game --script res://tools/balance_probe.gd -- --sweep 0
const SWEEP_SCALES := [0.7, 0.85, 1.0, 1.2, 1.4, 1.7, 2.0, 2.4]

func _sweep(index: int) -> void:
	var levels := Database.load_levels()
	if index < 0 or index >= levels.size():
		print("no level %d" % index)
		return
	var level: Dictionary = levels[index]
	print("sweeping %s (act_hp_multiplier x scale)" % level["engagement"])
	print("%8s%8s%9s%8s%8s%8s" % ["scale", "hp", "result", "integ", "kills", "leaks"])
	for scale in SWEEP_SCALES:
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		if not db.is_valid():
			print("  load failed: %s" % db.error_text())
			return
		var base := float(db.engagement.get("act_hp_multiplier", 1.0))
		# The Database is a plain data holder, so a probe can retune it in memory
		# without writing a file per sample.
		db.engagement["act_hp_multiplier"] = base * float(scale)
		var sim := Sim.new(db, 20260727 + index)
		SimFixture.run_greedy(sim)
		print("%8.2f%8.2f%9s%8d%8d%8d" % [scale, base * float(scale),
			"WIN" if sim.result() == Sim.RESULT_WIN else "LOSS",
			sim.integrity(), sim.kills(), sim.leaks()])
