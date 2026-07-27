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
	var args := OS.get_cmdline_user_args()
	for i in args.size():
		if args[i] == "--wave" and i + 1 < args.size():
			target_wave = int(args[i + 1])
		elif args[i] == "--enemies" and i + 1 < args.size():
			min_enemies = int(args[i + 1])
		elif args[i] == "--out" and i + 1 < args.size():
			out_path = args[i + 1]
		elif args[i] == "--overlay":
			show_overlay = true
	_run(target_wave, min_enemies, out_path, show_overlay)

func _run(target_wave: int, min_enemies: int, out_path: String, show_overlay: bool) -> void:
	await process_frame
	var main: Node = load(MAIN_SCENE).instantiate()
	root.add_child(main)
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

	var image := root.get_texture().get_image()
	var error := image.save_png(out_path)
	if error != OK:
		printerr("Could not write %s (error %d)" % [out_path, error])
		quit(1)
		return
	print("captured %s  tick=%d wave=%d/%d enemies=%d projectiles=%d platforms=%d integrity=%d capital=%d" % [
		out_path, sim.tick(), sim.wave_number(), sim.wave_count(),
		sim.e_live_count, sim.p_live_count, sim.t_count, sim.integrity(), sim.capital()])
	quit(0)
