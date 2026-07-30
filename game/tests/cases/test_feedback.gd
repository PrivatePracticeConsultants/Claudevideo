extends TestCase

## The combat feedback layer: muzzle flashes, impacts, wrecks and screen shake.
##
## All of it is decoration, and all of it is derived by DIFFING the simulation
## between ticks rather than by the simulation reporting events. That is what
## these tests are really guarding: the renderer must be able to work out what
## happened without the sim growing a render-facing event channel it would then
## have to hash. If that ever stops being true, it stops here.
##
## The visual result needs a GPU and an eye; what is testable headlessly is that
## the right events produce effects, that the wrong ones do not, and that the
## whole thing still costs exactly one draw call.

var _tree: SceneTree
var _sim: Sim
var _renderer: SimRenderer3D

func before_each() -> void:
	_tree = Engine.get_main_loop() as SceneTree
	_sim = SimFixture.fresh()
	_renderer = SimRenderer3D.new()
	_tree.root.add_child(_renderer)
	_renderer.setup(_sim, _theme())

func after_each() -> void:
	_tree.root.remove_child(_renderer)
	_renderer.queue_free()

func _theme() -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://data/theme.json"))
	return parsed if typeof(parsed) == TYPE_DICTIONARY else {}

## Draw one frame's worth of effects and report how many were visible.
func _drawn(delta: float = 0.0) -> int:
	_renderer.update_visuals(0.0, delta)
	return _renderer.drawn_effect_count()

func test_a_quiet_board_produces_nothing() -> void:
	_renderer.note_tick()
	assert_eq(_drawn(), 0, "nothing happened, so nothing is drawn")

func test_a_kill_leaves_a_wreck() -> void:
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("walker"))
	_renderer.note_tick()
	assert_eq(_drawn(), 0, "a live drone is not an effect")
	_sim._damage_enemy(0, 1 << 30, -1)
	_renderer.note_tick()
	assert_eq(_drawn(), 1, "a drone that died leaves exactly one wreck")

func test_a_shot_landing_leaves_an_impact() -> void:
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("walker"))
	# Reach into the projectile pool directly: what is being tested is that the
	# renderer notices a slot going from alive to dead, whatever emptied it.
	# Claimed off the free list exactly as _fire() does, so despawning it returns
	# a slot the pool actually handed out.
	var slot: int = _sim._claim_projectile_slot()
	_sim.p_alive[slot] = 1
	_sim.p_x[slot] = _sim.e_x[0]
	_sim.p_y[slot] = _sim.e_y[0]
	_sim.p_splash[slot] = 0.0
	_sim.p_live_count += 1
	_renderer.note_tick()
	assert_eq(_drawn(), 0, "a round in flight is not an effect")
	_sim._despawn_projectile(slot)
	_renderer.note_tick()
	assert_eq(_drawn(), 1, "the round landing is")

func test_effects_expire() -> void:
	# Without this they accumulate until the ring wraps, and the board ends up
	# permanently lit by things that happened a minute ago.
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("walker"))
	# One tick observed alive first: the renderer works by comparing consecutive
	# ticks, so something that appears and dies between two observations never
	# happened as far as it is concerned - which is correct, and cannot occur in
	# the real loop, where note_tick() runs on every tick.
	_renderer.note_tick()
	_sim._damage_enemy(0, 1 << 30, -1)
	_renderer.note_tick()
	assert_eq(_drawn(), 1, "there it is")
	assert_eq(_drawn(2.0), 0, "and two seconds later it is gone")

func test_ageing_is_by_elapsed_time_and_not_by_frame_count() -> void:
	# Thirty frames of zero delta must not fade anything. This is what the
	# screenshot tool relies on to capture the instant a shot goes off: a frame in
	# a software-rendered container can take longer than a muzzle flash exists for,
	# so it ages everything by zero and grabs that. Pausing the game is NOT this
	# case - main keeps passing a real delta while paused, and effects in the air
	# fade out, which is correct.
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("walker"))
	_renderer.note_tick()
	_sim._damage_enemy(0, 1 << 30, -1)
	_renderer.note_tick()
	for _frame in 30:
		assert_eq(_drawn(0.0), 1, "no time passed, so nothing faded")

func test_a_hundred_wrecks_are_still_one_draw_call() -> void:
	# The project's rendering invariant, applied to the newest layer.
	var before := _tree.root.get_child_count() + _renderer.get_child_count()
	_sim._begin_wave(0)
	for _i in 100:
		_sim._spawn(_sim.enemy_index("swarm"))
	_renderer.note_tick()
	for i in 100:
		_sim._damage_enemy(i, 1 << 30, -1)
	_renderer.note_tick()
	assert_eq(_drawn(), 100, "all hundred are drawn")
	assert_eq(_renderer.effect_layer().multimesh.instance_count, SimRenderer3D.FX_CAPACITY,
		"out of one preallocated buffer")
	assert_eq(_tree.root.get_child_count() + _renderer.get_child_count(), before,
		"and not one node was created to do it")

func test_the_ring_never_overruns_its_buffer() -> void:
	# More events in one tick than the pool has slots. The oldest are dropped;
	# nothing is written past the end.
	_sim._begin_wave(0)
	var wanted := SimRenderer3D.FX_CAPACITY + 200
	var spawned := 0
	while spawned < wanted and _sim.e_live_count < _sim.e_alive.size():
		_sim._spawn(_sim.enemy_index("swarm"))
		spawned += 1
	_renderer.note_tick()
	for i in _sim.e_alive.size():
		if _sim.e_alive[i] == 1:
			_sim._damage_enemy(i, 1 << 30, -1)
	_renderer.note_tick()
	assert_lte(float(_drawn()), float(SimRenderer3D.FX_CAPACITY),
		"never more than the pool holds")

func test_only_a_leak_shakes_the_camera() -> void:
	assert_eq(_renderer.shake(), 0.0, "still to begin with")
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("walker"))
	_sim._damage_enemy(0, 1 << 30, -1)
	_renderer.note_tick()
	assert_eq(_renderer.shake(), 0.0, "killing something does not shake the screen")
	# Walk a drone off the end of the road.
	_sim._spawn(_sim.enemy_index("walker"))
	for i in _sim.e_alive.size():
		if _sim.e_alive[i] == 1:
			_sim.e_prog[i] = _sim.path_length() - 1.0
	_sim._advance_enemies()
	assert_eq(_sim.leaks(), 1, "fixture sanity: something got through")
	_renderer.note_tick()
	assert_gt(_renderer.shake(), 0.0, "losing integrity does")

func test_the_shake_decays_and_puts_the_camera_back() -> void:
	# A camera left displaced by a leak would silently break every screen-to-world
	# pick after it, which is how you build and sell in the wrong place.
	var home := _renderer.camera_home()
	_sim._begin_wave(0)
	_sim._spawn(_sim.enemy_index("walker"))
	for i in _sim.e_alive.size():
		if _sim.e_alive[i] == 1:
			_sim.e_prog[i] = _sim.path_length() - 1.0
	_sim._advance_enemies()
	_renderer.note_tick()
	_renderer.update_visuals(0.0, 0.016)
	assert_ne(_renderer.camera.position, home, "it moved")
	for _frame in 120:
		_renderer.update_visuals(0.0, 0.016)
	assert_eq(_renderer.shake(), 0.0, "the shake ran out")
	assert_eq(_renderer.camera.position, home, "and the camera is exactly where it was")

func test_a_sold_turret_does_not_leave_a_ghost_firing() -> void:
	# Selling compacts the platform pool. A stale cooldown left in the vacated slot
	# would make the next turret built into it flash on its first frame.
	var spot := SimFixture.a_site(_sim)
	_sim.queue_place(0, spot[0], spot[1], 0)
	_sim.step()
	_renderer.note_tick()
	_sim.queue_sell(_sim.tick(), 0)
	_sim.step()
	_renderer.note_tick()
	assert_eq(_sim.t_count, 0, "fixture sanity: it is gone")
	assert_eq(_drawn(), 0, "and it did not fire on the way out")
