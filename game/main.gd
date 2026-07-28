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

## Campaign progress. Not simulation state and deliberately nowhere near it: the
## sim stays a pure function of data, seed and command log, and what the player
## has unlocked is none of its business.
##
## Progress is per BOARD, not per level, because a board is a chain - its later
## acts are entered carrying the earlier ones and are not winnable from a
## standing start. Dropping a player into act 3 of a board they have never played
## would hand them a level authored against turrets they do not have.
const PROGRESS_PATH := "user://progress.json"

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
## Synthesised at startup and kept across level changes - it holds no per-level
## state, and regenerating its waveforms on every restart would be pure waste.
var _sfx: Sfx
## Result the debrief chord was last played for, so winning is announced once and
## not on every frame of the banner.
var _sounded_result: int = Sim.RESULT_RUNNING

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
## How many boards the player has unlocked, at least one.
var _boards_unlocked: int = 1
## First level index of each board, in campaign order.
var _board_starts: PackedInt32Array = PackedInt32Array()
## Middle-drag camera panning.
var _dragging: bool = false
var _drag_from := Vector2.ZERO

func _ready() -> void:
	_theme = _load_theme()
	_levels = Database.load_levels()
	if _levels.is_empty():
		_show_fatal("data/levels.json lists no levels.")
		return
	_index_boards()
	_sfx = Sfx.new()
	add_child(_sfx)
	_sfx.setup(_theme)
	_load_progress()
	# Resume where they left off rather than at the start of a 36-level campaign.
	_start_level(_board_starts[_boards_unlocked - 1], {})

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
		# Everything else is rebuilt per level; the sound is not, because its
		# waveforms are generated once and belong to the session, not the act.
		if child == _sfx:
			continue
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
	# A chain carries turrets and ground; a board boundary carries what they were
	# worth, because a coordinate on one board means nothing on another.
	# Held either way so restarting the act restores the same opening position.
	_incoming_carry = offered_carry
	if not _incoming_carry.is_empty():
		if bool(db.engagement.get("carries_forward", false)):
			_sim.adopt(_incoming_carry.get("platforms", []),
				_incoming_carry.get("cells", PackedInt32Array()))
		else:
			_sim.grant_salvage(int(_incoming_carry.get("salvage", 0)))
	_tick_period = 1.0 / float(_sim.tick_rate())

	_renderer = SimRenderer3D.new()
	add_child(_renderer)
	_renderer.setup(_sim, _theme)
	# The renderer's tick diff is the only place that knows a shot was fired or a
	# drone died, so sound rides along with it rather than deriving it twice.
	_renderer.attach_audio(_sfx)
	_sounded_result = Sim.RESULT_RUNNING

	_hud = Hud.new()
	add_child(_hud)
	_hud.setup(_sim, _theme)
	var handover := _next_handover()
	_hud.set_level(str(level["name"]), _level_index, _levels.size(),
		_sim.t_count, bool(handover[0]), _sim.carry_dropped(),
		_sim.carry_stood_down(), _sim.salvage_granted(), int(handover[1]))

	_overlay = DebugOverlay.new()
	add_child(_overlay)
	_overlay.setup(_sim, Color(str(_theme.get("text", "#dfe6f0"))))

## What beating this level hands to the next one, read from the next level's own
## data so the HUD cannot claim a hand-over the sim would then refuse.
##
## Returns [extends_board, salvage_ceiling]. The ceiling is -1 when it does not
## apply (no next level, or the next level extends this board and so takes the
## turrets themselves). When it does apply it is the receiving act's, because
## that is the number the player will actually be paid - the outgoing board's
## raw worth is 17x larger at the first boundary and quoting it would be a lie.
func _next_handover() -> Array:
	if _level_index + 1 >= _levels.size():
		return [false, -1]
	var next: Dictionary = _levels[_level_index + 1]
	var db := Database.load_engagement(str(next["map"]), str(next["engagement"]))
	if not db.is_valid():
		return [false, -1]
	var same_board := str(next["map"]) == str((_levels[_level_index] as Dictionary)["map"])
	if same_board and bool(db.engagement.get("carries_forward", false)):
		return [true, -1]
	return [false, Sim.salvage_ceiling_of(db)]

## Where each board begins. Levels are grouped by map in campaign order, so a
## board boundary is simply where the map id changes.
func _index_boards() -> void:
	_board_starts = PackedInt32Array()
	var seen := ""
	for index in _levels.size():
		var map_id := str((_levels[index] as Dictionary)["map"])
		if map_id != seen:
			_board_starts.append(index)
			seen = map_id
	if _board_starts.is_empty():
		_board_starts.append(0)

func _board_of(level_index: int) -> int:
	var board := 0
	for i in _board_starts.size():
		if _board_starts[i] <= level_index:
			board = i
	return board

func _is_last_act_of_board(level_index: int) -> bool:
	var board := _board_of(level_index)
	var next_start := _levels.size() if board + 1 >= _board_starts.size() \
		else _board_starts[board + 1]
	return level_index + 1 >= next_start

func _load_progress() -> void:
	# A missing or corrupt file means a new campaign, never a crash. Progress is a
	# convenience; losing it must not cost anyone the game.
	_boards_unlocked = 1
	if not FileAccess.file_exists(PROGRESS_PATH):
		return
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(PROGRESS_PATH))
	if typeof(parsed) != TYPE_DICTIONARY:
		return
	_boards_unlocked = clampi(int((parsed as Dictionary).get("boards_unlocked", 1)),
		1, _board_starts.size())

func _save_progress() -> void:
	var file := FileAccess.open(PROGRESS_PATH, FileAccess.WRITE)
	if file == null:
		return  # read-only or sandboxed storage; play on regardless
	file.store_string(JSON.stringify({"boards_unlocked": _boards_unlocked}))
	file.close()

## Beating the last act of a board opens the next one.
func _note_win() -> void:
	if not _is_last_act_of_board(_level_index):
		return
	var next_board := _board_of(_level_index) + 1
	if next_board < _board_starts.size() and next_board + 1 > _boards_unlocked:
		_boards_unlocked = next_board + 1
		_save_progress()

## Jump to another unlocked board. Always to its FIRST act, because that is the
## only act in a chain authored to be entered with an empty board.
func _select_board(delta: int) -> void:
	var board := clampi(_board_of(_level_index) + delta, 0, _boards_unlocked - 1)
	_start_level(_board_starts[board], {})

func _process(delta: float) -> void:
	if _sim == null:
		return
	var steps := 0
	if not _paused and not _sim.is_over():
		_accumulator += delta * float(_speed)
		while _accumulator >= _tick_period and steps < MAX_STEPS_PER_FRAME:
			_sim.step()
			# The renderer diffs the board every tick to find what to flash, spark
			# and blow up. Per tick and not per frame: at 4x a frame spans several
			# ticks, and only sampling one of them makes a firing line look like it
			# is misfiring.
			_renderer.note_tick()
			_accumulator -= _tick_period
			steps += 1
		if steps >= MAX_STEPS_PER_FRAME:
			_accumulator = 0.0
	_overlay.note_steps(steps)

	var alpha := 0.0 if _sim.is_over() else clampf(_accumulator / _tick_period, 0.0, 1.0)
	_renderer.update_visuals(alpha, delta)
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
	if _sim.result() != _sounded_result:
		_sounded_result = _sim.result()
		if _sounded_result == Sim.RESULT_WIN:
			_sfx.play(Sfx.WIN)
		elif _sounded_result == Sim.RESULT_LOSS:
			_sfx.play(Sfx.LOSS)

func _unhandled_input(event: InputEvent) -> void:
	if _sim == null:
		return
	if event is InputEventMouseMotion:
		_track_cursor((event as InputEventMouseMotion).position)
	elif event is InputEventMouseButton and (event.button_index == MOUSE_BUTTON_WHEEL_UP \
			or event.button_index == MOUSE_BUTTON_WHEEL_DOWN) and event.pressed:
		# Zoom about whatever the pointer is over, so the thing you are looking at
		# stays where you are looking. Purely presentational - the simulation has
		# no idea a camera exists.
		_track_cursor((event as InputEventMouseButton).position)
		var steps := 1 if event.button_index == MOUSE_BUTTON_WHEEL_UP else -1
		_renderer.zoom_by(steps, SimRenderer3D.to_world(_cursor_sim_x, _cursor_sim_y, 0.0))
	elif event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_MIDDLE:
		_dragging = event.pressed
		_drag_from = (event as InputEventMouseButton).position
	elif event is InputEventMouseMotion and _dragging:
		# Drag the ground, not the camera: convert both pointer positions to world
		# space and move by the difference, so the board tracks the cursor exactly
		# at any zoom.
		var motion := event as InputEventMouseMotion
		var from_world := _ground_at(_drag_from)
		var to_world := _ground_at(motion.position)
		_renderer.pan_by(from_world - to_world)
		_drag_from = motion.position
	elif event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_RIGHT:
		# Right-click sells. Free placement with no undo is punishing in a way
		# nothing in the design intends: a misread of the road costs the turret
		# AND the slot, and the deployment limit means you cannot just build
		# another somewhere better.
		_track_cursor((event as InputEventMouseButton).position)
		if _hover_platform >= 0:
			_sim.queue_sell(_sim.tick(), _hover_platform)
			_hover_platform = -1
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
		# Played on the click, not on the command succeeding: a click that the sim
		# then refuses still deserves an acknowledgement that the game heard it.
		_sfx.play(Sfx.BUILD)
	elif event is InputEventKey and event.pressed and not event.echo:
		match (event as InputEventKey).keycode:
			KEY_1: _speed = 1
			KEY_2: _speed = 2
			KEY_3: _speed = 3
			KEY_4: _speed = 4
			KEY_E:
				# Call the next wave in early for a bounty. Harmlessly rejected
				# if there is no gap left to skip.
				_sim.queue_send_wave(_sim.tick())
			KEY_T:
				# Re-task whatever the pointer is over. On the hovered turret rather
				# than as a global mode: it is per-turret state, and a mode would mean
				# the same keypress does something different depending on a thing you
				# cannot see.
				if _hover_platform >= 0:
					var next := (_sim.platform_priority(_hover_platform) + 1) % Sim.target_mode_count()
					_sim.queue_priority(_sim.tick(), _hover_platform, next)
			KEY_M: _sfx.toggle_mute()
			KEY_Z: _renderer.reset_view()
			KEY_BRACKETLEFT: _select_board(-1)
			KEY_BRACKETRIGHT: _select_board(1)
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
					_note_win()
					# Hand the finished board forward; the next act takes it only
					# if it continues this chain.
					_start_level(_level_index + 1, _sim.board_snapshot())
			KEY_ESCAPE: get_tree().quit()

## Where a screen position lands on the ground plane, in world space. Returns the
## origin if the ray runs parallel to the ground, which only happens if the camera
## is edge-on and nothing sensible could be reported anyway.
func _ground_at(screen_point: Vector2) -> Vector3:
	var camera := _renderer.camera
	if camera == null:
		return Vector3.ZERO
	var hit: Variant = Plane(Vector3.UP, 0.0).intersects_ray(
		camera.project_ray_origin(screen_point), camera.project_ray_normal(screen_point))
	return Vector3.ZERO if hit == null else hit as Vector3

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
