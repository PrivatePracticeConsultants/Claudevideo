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

	# Same policy the acceptance tests use: take positions along the road up to
	# the deployment limit, then upgrade. Enough to produce a board with
	# something happening on it.
	var sites := SimFixture.candidate_sites(sim)
	var next_site := 0
	var ticks := 0
	while not sim.is_over() and ticks < MAX_TICKS:
		if sim.t_count < sim.platform_limit() and next_site + 1 < sites.size():
			if sim.can_build_at(float(sites[next_site]), float(sites[next_site + 1]), 0) == Sim.BUILD_OK:
				sim.queue_place(sim.tick(), sites[next_site], sites[next_site + 1], 0)
				next_site += 2
		else:
			var weakest := -1
			var weakest_tier := 1 << 30
			for i in sim.t_count:
				if sim.can_upgrade(i) and sim.platform_tier(i) < weakest_tier:
					weakest_tier = sim.platform_tier(i)
					weakest = i
			if weakest >= 0:
				sim.queue_upgrade(sim.tick(), weakest)
		sim.step()
		ticks += 1
		if sim.wave_number() >= target_wave and sim.e_live_count >= min_enemies:
			break

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
