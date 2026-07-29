extends SceneTree

## Dev utility: drive the game to a chosen state and save a PNG of it.
##
##   xvfb-run -a godot --path game --rendering-driver opengl3 \
##       --script res://tools/capture_screenshot.gd -- --wave 6 --out /tmp/shot.png
##
## Exists so the render layer can be verified without a human at a monitor - on a
## build server, in a headless container, or by an agent. It pauses the fixed-tick
## loop and advances the simulation directly, so reaching wave 6 takes a moment
## rather than four real minutes, and the captured frame is reproducible.
##
## From P6 this is also how the marketing clips get captured; the act-three
## spectacle pass needs to be filmable on demand.

const MAIN_SCENE := "res://main.tscn"
const MAX_TICKS := 40000

func _initialize() -> void:
	var target_wave := 6
	var min_enemies := 8
	var out_path := "user://capture.png"
	var show_overlay := false
	# Zooming in is how you check the things that are a few pixels wide when a
	# whole 5,000-unit act is framed at once - which is most of what the feedback
	# layer draws.
	var zoom_steps := 0
	# Which campaign level to capture. -1 keeps whatever the game opens on.
	var level := -1
	var args := OS.get_cmdline_user_args()
	for i in args.size():
		if args[i] == "--wave" and i + 1 < args.size():
			target_wave = int(args[i + 1])
		elif args[i] == "--enemies" and i + 1 < args.size():
			min_enemies = int(args[i + 1])
		elif args[i] == "--out" and i + 1 < args.size():
			out_path = args[i + 1]
		elif args[i] == "--zoom" and i + 1 < args.size():
			zoom_steps = int(args[i + 1])
		elif args[i] == "--level" and i + 1 < args.size():
			level = int(args[i + 1])
		elif args[i] == "--overlay":
			show_overlay = true
	_run(target_wave, min_enemies, out_path, show_overlay, zoom_steps, level)

func _run(target_wave: int, min_enemies: int, out_path: String, show_overlay: bool,
		zoom_steps: int = 0, level: int = -1) -> void:
	await process_frame
	var main: Node = load(MAIN_SCENE).instantiate()
	root.add_child(main)
	await process_frame

	if level >= 0:
		main._start_level(level, {})
		await process_frame
	if main.get("_sim") == null:
		printerr("The game failed to start; data did not load.")
		quit(1)
		return

	var sim: Sim = main._sim
	main._paused = true
	main._overlay.visible = show_overlay

	# Drive with the *same* policy the acceptance tests use, rather than a copy.
	# An earlier inlined copy of it had a bug the real one does not - it stalled
	# retrying a site that had become permanently blocked, and produced
	# screenshots of a losing board with two turrets on it. Duplicated policy is
	# duplicated bugs.
	SimFixture.run_greedy(sim, true, target_wave, min_enemies)

	# Let the renderer rebuild its instance buffers from the new state.
	for _i in 4:
		await process_frame

	# run_greedy drives the sim directly, behind main's back, so the renderer has
	# seen none of those ticks and its feedback layer has nothing to show. A few
	# ticks fed through the normal path give it a before and an after to diff, so
	# the capture includes muzzle flashes and wrecks rather than a board where
	# nothing appears to be happening.
	#
	# Done last, and with main's own _process switched off, because effects age by
	# real elapsed time: a frame in a software-rendered container can take a third
	# of a second, which is longer than a muzzle flash exists for. Ageing them by
	# zero freezes the instant rather than capturing whatever survived the stall.
	# Zoom about whatever the board is busiest around, which is where anything
	# worth looking at closely is happening.
	if zoom_steps > 0:
		var focus_x := 0.0
		var focus_y := 0.0
		var counted := 0
		for i in sim.e_alive.size():
			if sim.e_alive[i] == 1:
				focus_x += sim.e_x[i]
				focus_y += sim.e_y[i]
				counted += 1
		if counted > 0:
			main._renderer.zoom_by(zoom_steps, SimRenderer3D.to_world(
				focus_x / float(counted), focus_y / float(counted), 0.0))
		await process_frame

	main.set_process(false)
	main._renderer.note_tick()
	for _i in 3:
		sim.step()
		main._renderer.note_tick()
	main._renderer.update_visuals(0.0, 0.0)
	await process_frame

	var image := root.get_texture().get_image()
	var error := image.save_png(out_path)
	if error != OK:
		printerr("Could not write %s (error %d)" % [out_path, error])
		quit(1)
		return
	print("captured %s  tick=%d wave=%d/%d enemies=%d projectiles=%d platforms=%d integrity=%d capital=%d effects=%d" % [
		out_path, sim.tick(), sim.wave_number(), sim.wave_count(),
		sim.e_live_count, sim.p_live_count, sim.t_count, sim.integrity(), sim.capital(),
		main._renderer.drawn_effect_count()])
	quit(0)
