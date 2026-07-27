extends Node3D

## Entry point: owns the Sim, drives it at a fixed rate, and routes input into
## the command log.
##
## The important thing happening here is the separation the whole architecture
## rests on. The simulation advances in whole 30Hz ticks and knows nothing about
## frames, `delta`, the scene tree, or the fact that it is now being drawn in 3D;
## this node accumulates real time, spends it in fixed steps, and hands the
## leftover fraction to the renderer as `alpha`. Everything good downstream -
## replays, 3x speed for free, the headless balance sim, Daily Contracts that
## match across devices - falls out of that split.

const MAP_ID := "highway_01"
const ENGAGEMENT_ID := "highway_01_act1"
const THEME_PATH := "res://data/theme.json"

## Ceiling on catch-up steps per frame. Without it, one long stall (a breakpoint,
## an alt-tab, a phone call, a browser tab going to the background) leaves a
## backlog that takes longer to simulate than it does to accumulate, and the game
## never catches up - the classic spiral of death. Past this we drop the backlog:
## running slow is recoverable, freezing is not.
##
## This matters more on the web build than anywhere else, because a backgrounded
## tab can hand back a delta measured in minutes.
const MAX_STEPS_PER_FRAME := 12
const PAD_CLICK_RADIUS := 34.0

var _sim: Sim
var _theme: Dictionary = {}
var _renderer: SimRenderer3D
var _overlay: DebugOverlay
var _hud: Hud

var _tick_period: float = 1.0
var _accumulator: float = 0.0
var _speed: int = 1
var _paused: bool = false
var _platforms_drawn: int = 0

func _ready() -> void:
	var db := Database.load_engagement(MAP_ID, ENGAGEMENT_ID)
	if not db.is_valid():
		# A typo in a data file must produce a readable message, not a stack
		# trace and a black window.
		_show_fatal(db.error_text())
		return
	_theme = _load_theme()

	# The seed is fixed for P0 so a session is reproducible while the sim is
	# being built. Run seeding arrives with the run layer in P3.
	_sim = Sim.new(db, 20260727)
	_tick_period = 1.0 / float(_sim.tick_rate())

	_renderer = SimRenderer3D.new()
	add_child(_renderer)
	_renderer.setup(_sim, _theme)

	_hud = Hud.new()
	add_child(_hud)
	_hud.setup(_sim, _theme)

	_overlay = DebugOverlay.new()
	add_child(_overlay)
	_overlay.setup(_sim, Color(str(_theme.get("text", "#dfe6f0"))))

func _process(delta: float) -> void:
	if _sim == null:
		return
	var steps := 0
	if not _paused and not _sim.is_over():
		_accumulator += delta * float(_speed)
		while _accumulator >= _tick_period and steps < MAX_STEPS_PER_FRAME:
			_sim.step()
			_accumulator -= _tick_period
			steps += 1
		if steps >= MAX_STEPS_PER_FRAME:
			_accumulator = 0.0
	_overlay.note_steps(steps)

	var alpha := 0.0 if _sim.is_over() else clampf(_accumulator / _tick_period, 0.0, 1.0)
	_renderer.update_visuals(alpha)
	if _sim.t_count != _platforms_drawn:
		_platforms_drawn = _sim.t_count
		_renderer.rebuild_static()
	_hud.refresh(_speed, _paused)

func _unhandled_input(event: InputEvent) -> void:
	if _sim == null:
		return
	if event is InputEventMouseMotion:
		_renderer.set_hover(_pad_under_cursor((event as InputEventMouseMotion).position))
	elif event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_LEFT:
		var pad := _pad_under_cursor((event as InputEventMouseButton).position)
		if pad >= 0:
			# Placement goes through the command log rather than mutating the sim
			# directly, so what the player did is exactly what a replay replays.
			_sim.queue_place(_sim.tick(), pad, 0)
	elif event is InputEventKey and event.pressed and not event.echo:
		match (event as InputEventKey).keycode:
			KEY_1: _speed = 1
			KEY_2: _speed = 2
			KEY_3: _speed = 3
			KEY_SPACE: _paused = not _paused
			KEY_F3: _overlay.visible = not _overlay.visible
			KEY_R: get_tree().reload_current_scene()
			KEY_ESCAPE: get_tree().quit()

## Cast the cursor onto the ground plane and find the nearest free pad to where
## it lands. Picking against the plane rather than against collision shapes means
## no physics bodies and no colliders to keep in sync with the simulation.
func _pad_under_cursor(screen_point: Vector2) -> int:
	var camera := _renderer.camera
	if camera == null:
		return -1
	var ground := Plane(Vector3.UP, 0.0)
	var hit: Variant = ground.intersects_ray(
		camera.project_ray_origin(screen_point),
		camera.project_ray_normal(screen_point))
	if hit == null:
		return -1
	return _pad_at((hit as Vector3).x, (hit as Vector3).z)

func _pad_at(sim_x: float, sim_y: float) -> int:
	var best := -1
	var best_distance := PAD_CLICK_RADIUS * PAD_CLICK_RADIUS
	for i in _sim.pad_count():
		if not _sim.pad_is_free(i):
			continue
		var dx := _sim.pad_x(i) - sim_x
		var dy := _sim.pad_y(i) - sim_y
		var distance := dx * dx + dy * dy
		if distance < best_distance:
			best_distance = distance
			best = i
	return best

func _load_theme() -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(THEME_PATH))
	return parsed if typeof(parsed) == TYPE_DICTIONARY else {}

func _show_fatal(message: String) -> void:
	push_error(message)
	var layer := CanvasLayer.new()
	var label := Label.new()
	label.position = Vector2(40, 40)
	label.add_theme_font_size_override("font_size", 16)
	label.text = "The game data could not be loaded:\n\n%s" % message
	layer.add_child(label)
	add_child(layer)
