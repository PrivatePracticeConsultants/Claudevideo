extends SceneTree

## Verifies the P0 rendering constraint: enemies and projectiles go through
## MultiMeshInstance2D, so draw calls stay flat as entity count climbs.
##
##   xvfb-run -a godot --path game --rendering-driver opengl3 \
##       --script res://tools/render_stress.gd
##
## The check that matters is the draw-call count, because it is hardware
## independent - if it tracks entity count, somebody has quietly gone back to one
## node per entity and the mobile budget is gone. Frame times are printed too,
## but read them with care: on a headless box this is llvmpipe software
## rasterisation, which is far slower than any real GPU. The 60fps-on-an-iPhone-11
## target in the plan has to be measured on the device, and that is P1's gate.

const MAIN_SCENE := "res://main.tscn"
const FRAMES_PER_SAMPLE := 30

func _initialize() -> void:
	_run()

func _run() -> void:
	await process_frame
	var main: Node = load(MAIN_SCENE).instantiate()
	root.add_child(main)
	await process_frame
	var sim: Sim = main._sim
	main._paused = true

	print("enemies  projectiles  draw_calls  ms/frame")
	for target in [0, 25, 100, 250]:
		await _populate(sim, target)
		# Warm up so shader compilation and buffer growth do not land in the
		# sample.
		for _i in 5:
			await process_frame
		var started := Time.get_ticks_usec()
		for _i in FRAMES_PER_SAMPLE:
			await process_frame
		var elapsed := Time.get_ticks_usec() - started
		var draw_calls := int(Performance.get_monitor(Performance.RENDER_TOTAL_DRAW_CALLS_IN_FRAME))
		print("%7d  %11d  %10d  %8.2f" % [
			sim.e_live_count, sim.p_live_count, draw_calls,
			float(elapsed) / float(FRAMES_PER_SAMPLE) / 1000.0])
	quit(0)

## Force the board to a given enemy count, with a shot in flight for each, by
## driving the pools directly. This is a rendering measurement, not a gameplay
## one, so it bypasses the wave director on purpose.
func _populate(sim: Sim, target: int) -> void:
	while sim.e_live_count > 0:
		for i in sim.e_alive.size():
			if sim.e_alive[i] == 1:
				sim._despawn_enemy(i)
	while sim.p_live_count > 0:
		for i in sim.p_alive.size():
			if sim.p_alive[i] == 1:
				sim._despawn_projectile(i)
	if target == 0:
		await process_frame
		return
	sim._begin_wave(0)
	if sim.t_count == 0:
		sim._try_place(0, 0)
	for i in target:
		sim._spawn(0)
	# Spread them along the corridor so they are not all in one cell, and give
	# each one an inbound projectile.
	var spread := sim.path_length() / float(target)
	var placed := 0
	for i in sim.e_alive.size():
		if sim.e_alive[i] == 0:
			continue
		sim.e_prog[i] = float(placed) * spread
		sim.e_prev_prog[i] = sim.e_prog[i]
		sim.sample_for_render(sim.e_prog[i], sim.e_offset[i])
		sim.e_x[i] = sim.out_x()
		sim.e_y[i] = sim.out_y()
		sim._fire(0, i)
		placed += 1
	await process_frame
