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

const THEME_PATH := "res://data/theme.json"

## How close a click has to be to an existing platform to mean "upgrade this"
## rather than "build here". Larger than the platform itself so it is forgiving.
const PLATFORM_CLICK_RADIUS := 30.0

## Ceiling on catch-up steps per frame. Without it, one long stall (a breakpoint,
## an alt-tab, a phone call, a browser tab going to the background) leaves a
## backlog that takes longer to simulate than it does to accumulate, and the game
## never catches up - the classic spiral of death. Past this we drop the backlog:
## running slow is recoverable, freezing is not.
##
## This matters more on the web build than anywhere else, because a backgrounded
## tab can hand back a delta measured in minutes.
const MAX_STEPS_PER_FRAME := 12

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
var _tiers_drawn: int = 0
var _levels: Array = []
var _level_index: int = 0
var _cursor_sim_x: float = 0.0
var _cursor_sim_y: float = 0.0
var _cursor_valid: bool = false
var _hover_platform: int = -1
## Nothing is drawn under the cursor until the pointer has actually been
## somewhere; otherwise a build ghost sits at the world origin on the first frame.
var _cursor_live: bool = false
var _blueprint: int = 0
var _cursor_cell_x: int = 0
var _cursor_cell_y: int = 0
var _cursor_can_buy: bool = false
var _cells_drawn: int = -1
## The board this level started from. Held so restarting an act restores the
## same inheritance rather than handing you a clean slate - retrying act 3 should
## not quietly delete what acts 1 and 2 built.
var _incoming_carry: Dictionary = {}

func _ready() -> void:
	_theme = _load_theme()
	_levels = Database.load_levels()
	if _levels.is_empty():
		_show_fatal("data/levels.json lists no levels.")
		return
	_start_level(0, {})

## Tear down and rebuild for a level. Everything is recreated rather than reset
## because the Sim is immutable once constructed - its tables are built from the
## map and engagement it was handed - and rebuilding is both simpler and harder
## to get subtly wrong than a reset path nobody exercises.
func _start_level(index: int, offered_carry: Dictionary) -> void:
	_level_index = clampi(index, 0, _levels.size() - 1)
	var level: Dictionary = _levels[_level_index]
	var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
	if not db.is_valid():
		# A typo in a data file must produce a readable message, not a stack
		# trace and a black window.
		_show_fatal(db.error_text())
		return

	for child in get_children():
		child.queue_free()
	_platforms_drawn = 0
	_tiers_drawn = 0
	_accumulator = 0.0
	_paused = false
	_hover_platform = -1
	_cursor_live = false
	_cells_drawn = -1
	_blueprint = 0

	# The seed is derived from the level so a given level always plays the same
	# way. Run seeding arrives with the run layer in P3.
	_sim = Sim.new(db, 20260727 + _level_index)
	# An act that continues a chain inherits the previous act's turrets and
	# ground. One that opens a chain never does, whatever it was handed.
	_incoming_carry = offered_carry if bool(db.engagement.get("carries_forward", false)) else {}
	if not _incoming_carry.is_empty():
		_sim.adopt(_incoming_carry.get("platforms", []),
			_incoming_carry.get("cells", PackedInt32Array()))
	_tick_period = 1.0 / float(_sim.tick_rate())

	_renderer = SimRenderer3D.new()
	add_child(_renderer)
	_renderer.setup(_sim, _theme)

	_hud = Hud.new()
	add_child(_hud)
	_hud.setup(_sim, _theme)
	_hud.set_level(str(level["name"]), _level_index, _levels.size(),
		_sim.t_count, _next_level_extends(), _sim.carry_dropped())

	_overlay = DebugOverlay.new()
	add_child(_overlay)
	_overlay.setup(_sim, Color(str(_theme.get("text", "#dfe6f0"))))

## Whether beating this level extends the same board rather than moving to a new
## one. Read from the next level's own data, so the HUD cannot claim a hand-over
## the sim would then refuse.
func _next_level_extends() -> bool:
	if _level_index + 1 >= _levels.size():
		return false
	var next: Dictionary = _levels[_level_index + 1]
	if str(next["map"]) != str((_levels[_level_index] as Dictionary)["map"]):
		return false
	var db := Database.load_engagement(str(next["map"]), str(next["engagement"]))
	return db.is_valid() and bool(db.engagement.get("carries_forward", false))

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
	# Platform geometry is static, so it is rebuilt when the board changes -
	# either a new platform, or an existing one changing tier.
	var tier_sum := 0
	for i in _sim.t_count:
		tier_sum += _sim.platform_tier(i)
	if _sim.t_count != _platforms_drawn or tier_sum != _tiers_drawn \
			or _sim.cells_bought() != _cells_drawn:
		_platforms_drawn = _sim.t_count
		_tiers_drawn = tier_sum
		_cells_drawn = _sim.cells_bought()
		_renderer.refresh_board()
	_refresh_cursor()
	_hud.refresh(_speed, _paused, _hover_platform, _blueprint, _cursor_can_buy)

func _unhandled_input(event: InputEvent) -> void:
	if _sim == null:
		return
	if event is InputEventMouseMotion:
		_track_cursor((event as InputEventMouseMotion).position)
	elif event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_LEFT:
		_track_cursor((event as InputEventMouseButton).position)
		# Every player action goes through the command log rather than mutating
		# the sim directly, so what the player did is exactly what a replay
		# replays. Clicking an existing platform upgrades it; clicking open
		# ground builds.
		# Click priority, most specific first: an existing turret means upgrade
		# it; owned ground means build on it; unowned frontier ground means buy
		# it. One button, and the meaning is always the most useful thing that
		# could happen at that spot.
		if _hover_platform >= 0:
			_sim.queue_upgrade(_sim.tick(), _hover_platform)
		elif _cursor_can_buy:
			_sim.queue_buy_cell(_sim.tick(), _cursor_cell_x, _cursor_cell_y)
		else:
			_sim.queue_place(_sim.tick(), roundi(_cursor_sim_x), roundi(_cursor_sim_y), _blueprint)
	elif event is InputEventKey and event.pressed and not event.echo:
		match (event as InputEventKey).keycode:
			KEY_1: _speed = 1
			KEY_2: _speed = 2
			KEY_3: _speed = 3
			KEY_SPACE: _paused = not _paused
			KEY_F3: _overlay.visible = not _overlay.visible
			KEY_Q, KEY_TAB:
				# Cycle the weapon to build. One key rather than a number per
				# family, so it still works when there are five of them.
				_blueprint = (_blueprint + 1) % _sim.blueprint_count()
			KEY_R: _start_level(_level_index, _incoming_carry)
			KEY_N:
				# Advance on a win; on a loss this does nothing, so it cannot be
				# used to skip a level you have not beaten.
				if _sim.result() == Sim.RESULT_WIN and _level_index + 1 < _levels.size():
					# Hand the finished board forward; the next act takes it only
					# if it continues this chain.
					_start_level(_level_index + 1, _sim.board_snapshot())
			KEY_ESCAPE: get_tree().quit()

## Cast the cursor onto the ground plane to find the simulation coordinates it
## points at. Picking against the plane rather than against collision shapes
## means no physics bodies and no colliders to keep in sync with the simulation.
##
## Coordinates are rounded to whole units when a command is issued, so the
## command log stays integer-valued and a replay cannot drift by a fraction of a
## pixel of mouse position.
func _track_cursor(screen_point: Vector2) -> void:
	var camera := _renderer.camera
	if camera == null:
		return
	var ground := Plane(Vector3.UP, 0.0)
	var hit: Variant = ground.intersects_ray(
		camera.project_ray_origin(screen_point),
		camera.project_ray_normal(screen_point))
	if hit == null:
		return
	_cursor_live = true
	_cursor_sim_x = (hit as Vector3).x
	_cursor_sim_y = (hit as Vector3).z
	_hover_platform = _sim.platform_at(_cursor_sim_x, _cursor_sim_y, PLATFORM_CLICK_RADIUS)
	_cursor_cell_x = _sim.cell_x_of(_cursor_sim_x)
	_cursor_cell_y = _sim.cell_y_of(_cursor_sim_y)
	_cursor_can_buy = _hover_platform < 0 and _sim.can_buy_cell(_cursor_cell_x, _cursor_cell_y)
	_cursor_valid = _sim.can_build_at(roundi(_cursor_sim_x), roundi(_cursor_sim_y), _blueprint) == Sim.BUILD_OK

func _refresh_cursor() -> void:
	if not _cursor_live:
		_renderer.set_build_cursor(0.0, 0.0, 0.0, false)
		return
	if _hover_platform >= 0:
		# Hovering a platform shows what it already covers, and whether the next
		# tier is affordable.
		_renderer.set_build_cursor(_sim.t_x[_hover_platform], _sim.t_y[_hover_platform],
			_sim.platform_range(_hover_platform), _sim.can_upgrade(_hover_platform))
	elif _cursor_can_buy:
		# Buying ground shows the cell, not a weapon's range.
		_renderer.set_build_cursor(_sim.cell_centre_x(_cursor_cell_x),
			_sim.cell_centre_y(_cursor_cell_y), _sim.cell_size() * 0.5, true)
	else:
		_renderer.set_build_cursor(_cursor_sim_x, _cursor_sim_y,
			_sim.blueprint_range(_blueprint), _cursor_valid)

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
