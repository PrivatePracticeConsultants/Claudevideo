extends SceneTree

## Where a frame actually goes, measured stage by stage on a real board.
##
##   xvfb-run -a godot --path game --rendering-driver opengl3 \
##       --script res://tools/frame_profile.gd -- --level 46 --wave 10
##
## Exists because "it feels laggy" is not a number and the render stress tool
## only counts draw calls. This times the CPU side: the per-tick diff the
## feedback layer does, the per-frame instance fill, and the board rebuild -
## separately, so the answer is which one rather than how much.

const MAIN_SCENE := "res://main.tscn"

func _initialize() -> void:
	var level := 46
	var wave := 10
	var enemies := 30
	var argv := OS.get_cmdline_user_args()
	for i in argv.size():
		if argv[i] == "--level" and i + 1 < argv.size():
			level = int(argv[i + 1])
		elif argv[i] == "--wave" and i + 1 < argv.size():
			wave = int(argv[i + 1])
		elif argv[i] == "--enemies" and i + 1 < argv.size():
			enemies = int(argv[i + 1])
	_run(level, wave, enemies)

func _run(level: int, wave: int, min_enemies: int) -> void:
	await process_frame
	var main: Node = load(MAIN_SCENE).instantiate()
	root.add_child(main)
	await process_frame
	main._start_level(level, {})
	await process_frame
	var sim: Sim = main._sim
	main._paused = true
	SimFixture.run_greedy(sim, true, wave, min_enemies)
	var renderer = main._renderer
	renderer.refresh_board()
	await process_frame

	print("level %d  wave %d/%d  turrets %d  enemies alive %d  projectiles alive %d"
		% [level, sim.wave_number(), sim.wave_count(), sim.t_count,
			sim.e_live_count, sim.p_live_count])
	print("pool sizes: enemies %d  projectiles %d  platforms %d  grid %d x %d = %d cells"
		% [sim.e_alive.size(), sim.p_alive.size(), sim.t_used.size(),
			sim.grid_cols(), sim.grid_rows(), sim.grid_cols() * sim.grid_rows()])
	print("")
	print("%-26s %10s %10s" % ["stage", "ms/call", "calls/s at 4x"])
	_time("renderer.note_tick (per tick)", 200, func() -> void: renderer.note_tick())
	_time("renderer.update_visuals", 200, func() -> void: renderer.update_visuals(0.5, 0.0166))
	_time("refresh_board (turret click)", 30, func() -> void: renderer.refresh_board(false))
	_time("refresh_board (ground bought)", 30, func() -> void: renderer.refresh_board(true))
	_time("sim.step", 100, func() -> void: sim.step())
	print("")
	print("--- inside refresh_board ---")
	_time("  _refresh_cells", 30, func() -> void: renderer._refresh_cells())
	_time("  _refresh_turrets", 30, func() -> void: renderer._refresh_turrets())
	_time("  _refresh_link_lines", 30, func() -> void: renderer._refresh_link_lines())
	print("")
	print("--- inside sim.step ---")
	_time("  _refresh_links", 100, func() -> void: sim._refresh_links())
	_time("  _advance_enemies", 100, func() -> void: sim._advance_enemies())
	_time("  hash.rebuild", 100, func() -> void:
		sim._hash.rebuild(sim.e_alive, sim.e_x, sim.e_y, sim.enemy_slot_bound()))
	_time("  _update_platforms", 100, func() -> void: sim._update_platforms())
	_time("  _advance_projectiles", 100, func() -> void: sim._advance_projectiles())
	_time("  _update_wave_director", 100, func() -> void: sim._update_wave_director())
	quit(0)

## Median of N runs, not the mean: one scheduling hiccup in a headless container
## should not become the headline number.
func _time(label: String, runs: int, body: Callable) -> void:
	var samples := PackedFloat64Array()
	for _r in runs:
		var began := Time.get_ticks_usec()
		body.call()
		samples.append(float(Time.get_ticks_usec() - began) / 1000.0)
	var sorted := Array(samples)
	sorted.sort()
	var median: float = sorted[sorted.size() / 2]
	print("%-26s %10.3f %10s" % [label, median,
		"240" if label.ends_with("(per tick)") else "60"])
