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
const MATERIALS_PATH := "res://data/materials.json"

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
## Generated surface maps, held for the session rather than the board.
var _materials: MaterialLibrary
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
## Modules drafted so far on this board, and the three currently on offer.
##
## Held here rather than in the Sim because they outlive any one act: the Sim is
## rebuilt from scratch every level and a draft has to survive that. They reset
## when the campaign moves to a new board - a board is a contract, and a run that
## accumulated every module across forty-eight levels would end as one obvious
## stack rather than as a series of decisions.
var _held_modules: PackedStringArray = PackedStringArray()
var _offer: PackedStringArray = PackedStringArray()

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
			# Damage carries with the emplacements. Without it a sloppy act I costs
			# nothing in act IV, and each act of a chain is a fresh start wearing a
			# chain's clothes. A new board starts whole - see inherit_integrity.
			if bool(db.economy.get("integrity_persists_in_chain", false)):
				_sim.inherit_integrity(int(_incoming_carry.get("integrity", 0)))
		else:
			_sim.grant_salvage(int(_incoming_carry.get("salvage", 0)))
	# Fitted before anything else runs, so a module is part of the state a replay
	# starts from rather than an event partway through it.
	if not _held_modules.is_empty():
		_sim.apply_modules(_held_modules)
	_tick_period = 1.0 / float(_sim.tick_rate())

	_renderer = SimRenderer3D.new()
	add_child(_renderer)
	# The surface maps are generated, and generating them takes about a quarter
	# of a second - so they are built once for the session and handed to each
	# board's renderer, exactly like the sound above. Rebuilt per level they
	# would be a visible freeze on every one of the 48 acts, which is a strange
	# way to spend a graphics upgrade.
	_renderer.setup(_sim, _theme, _material_library())
	# The renderer's tick diff is the only place that knows a shot was fired or a
	# drone died, so sound rides along with it rather than deriving it twice.
	_renderer.attach_audio(_sfx)
	_sounded_result = Sim.RESULT_RUNNING

	_hud = Hud.new()
	add_child(_hud)
	_hud.setup(_sim, _theme)
	_offer = PackedStringArray()
	var handover := _next_handover()
	# Before set_level, which folds the held modules into the level line.
	_hud.set_modules(_held_modules)
	_hud.set_level(str(level["name"]), _level_index, _levels.size(),
		_sim.t_count, bool(handover[0]), _sim.carry_dropped(),
		_sim.carry_stood_down(), _sim.salvage_granted(), int(handover[1]))

	_renderer.set_quality(_saved_quality)
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

## The quality level read from the save, applied to each level's renderer as it
## is built. Held here rather than in the renderer because the renderer is
## rebuilt per level and the preference is not.
var _saved_quality: int = SimRenderer3D.QUALITY_BALANCED

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
	_saved_quality = clampi(int((parsed as Dictionary).get("quality",
		SimRenderer3D.QUALITY_BALANCED)), 0, SimRenderer3D.QUALITY_NAMES.size() - 1)

func _save_progress() -> void:
	var file := FileAccess.open(PROGRESS_PATH, FileAccess.WRITE)
	if file == null:
		return  # read-only or sandboxed storage; play on regardless
	# Quality rides along with progress: it is a per-machine preference, and being
	# asked to find it again every session is the same annoyance as being asked to
	# re-issue every targeting order.
	file.store_string(JSON.stringify({"boards_unlocked": _boards_unlocked,
		"quality": _renderer.quality() if _renderer != null else 1}))
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
		# Whether the GROUND changed is passed through, so buying a turret does not
		# pay to redraw an overlay only a ground purchase can alter.
		var ground_changed := _sim.cells_bought() != _cells_drawn
		_platforms_drawn = _sim.t_count
		_tiers_drawn = tier_sum
		_cells_drawn = _sim.cells_bought()
		_renderer.refresh_board(ground_changed)
	_refresh_cursor()
	_hud.refresh(_speed, _paused, _hover_platform, _blueprint, _cursor_can_buy)
	if _sim.result() != _sounded_result:
		_sounded_result = _sim.result()
		if _sounded_result == Sim.RESULT_WIN:
			_offer = _draw_offer()
			_hud.set_offer(_offer)
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
			# 1-3 double as the module draft while an offer is standing, which it
			# only ever is on a won act - so during play they are speed and nothing
			# else.
			KEY_1:
				if _offer.is_empty(): _speed = 1
				else: _take_module(0)
			KEY_2:
				if _offer.is_empty(): _speed = 2
				else: _take_module(1)
			KEY_3:
				if _offer.is_empty(): _speed = 3
				else: _take_module(2)
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
			KEY_O:
				# Overcharge the hovered turret: a few seconds of surge, then a
				# long per-turret cooldown. The sim refuses it cleanly if it is
				# jammed, already surging, cooling down, or unaffordable.
				if _hover_platform >= 0:
					_sim.queue_overcharge(_sim.tick(), _hover_platform)
			KEY_G, KEY_H:
				# Choose the hovered tier-4 turret's doctrine: G the first, H the
				# second. Permanent - the sim refuses seconds thoughts, wrong
				# tiers and families without doctrines, all silently.
				if _hover_platform >= 0:
					_sim.queue_doctrine(_sim.tick(), _hover_platform,
						0 if event.keycode == KEY_G else 1)
			KEY_F2:
				# Graphics quality. Bundled rather than a menu of switches, and
				# remembered, because the answer to "is this smooth on my machine"
				# is one the player has to find and should only have to find once.
				_renderer.cycle_quality()
				_save_progress()
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
					# A new board is a new contract: the draft resets with it.
					if not bool(_next_handover()[0]):
						_held_modules = PackedStringArray()
					_start_level(_level_index + 1, _sim.board_snapshot())
			KEY_ESCAPE: get_tree().quit()

## Three modules to choose between, drawn without replacement.
##
## Empty when this win does not continue a chain: a module fitted on the last act
## of a board would be discarded before it ever fired, which is a choice with no
## consequence and reads as the game losing track.
##
## Seeded by the level rather than by the clock, so the same act always offers the
## same three. That is what makes a draft a decision you can plan a board around
## instead of a slot machine you reload until it gives you the one you wanted.
const OFFER_SIZE := 3

func _draw_offer() -> PackedStringArray:
	var out := PackedStringArray()
	if not bool(_next_handover()[0]):
		return out
	var pool := PackedStringArray()
	for id in Database.load_modules():
		if not _held_modules.has(id):
			pool.append(id)
	var rng := Rng.new(20260729 + _level_index * 7919)
	while out.size() < OFFER_SIZE and pool.size() > 0:
		var pick := rng.next_below(pool.size())
		out.append(pool[pick])
		pool.remove_at(pick)
	return out

## Fit one of the three. Rejected unless there is an offer standing and the act
## was actually won, so the keys do nothing during play.
func _take_module(index: int) -> void:
	if _offer.is_empty() or index < 0 or index >= _offer.size():
		return
	if _sim.result() != Sim.RESULT_WIN:
		return
	_held_modules.append(_offer[index])
	_offer = PackedStringArray()
	_hud.set_offer(_offer)
	_hud.set_modules(_held_modules)
	_sfx.play(Sfx.BUILD)

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

## The session's surface maps, generated on first use and kept thereafter.
##
## A RefCounted rather than a Node, so it is not swept up by the queue_free()
## pass that clears the previous board - the only thing keeping it alive is this
## reference, which is the point.
func _material_library() -> MaterialLibrary:
	if _materials == null:
		var families := {}
		var parsed: Variant = JSON.parse_string(
			FileAccess.get_file_as_string(MATERIALS_PATH))
		if typeof(parsed) == TYPE_DICTIONARY:
			var listed: Variant = (parsed as Dictionary).get("families", {})
			if typeof(listed) == TYPE_DICTIONARY:
				families = listed
		# The tier is applied by the renderer, which is the thing that knows it.
		_materials = MaterialLibrary.new(_theme.get("world", {}), families)
	return _materials

func _show_fatal(message: String) -> void:
	push_error(message)
	var layer := CanvasLayer.new()
	var label := Label.new()
	label.position = Vector2(40, 40)
	label.add_theme_font_size_override("font_size", 16)
	label.text = "The game data could not be loaded:\n\n%s" % message
	layer.add_child(label)
	add_child(layer)
