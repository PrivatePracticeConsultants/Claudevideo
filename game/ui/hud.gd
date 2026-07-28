class_name Hud
extends CanvasLayer

## Heads-up display: the numbers you need to play, what is coming next, the
## end-of-engagement banner, and the debrief under it.
##
## Section 3.6's table-stakes QoL is now all here - sell, send-wave-early, tier
## readouts, a preview of the next wave, and per-turret targeting orders on the
## turret you are hovering. Still deliberately absent: a build menu richer than
## one cycling key.

var _sim: Sim
var _theme: Dictionary
var _stats: Label
var _hint: Label
var _banner: Label
var _debrief: Label
var _level: Label
var _preview: Label
var _level_text: String = ""
var _is_last_level: bool = false
## Whether the level after this one is the next act on the same board. It changes
## what winning means - not "on to somewhere else" but "the road gets longer and
## everything you built stays" - so the banner has to say which.
var _next_extends: bool = false
## The most salvage the NEXT act will accept, or -1 where none applies. The board
## you are finishing is worth far more than any one act will take, so the banner
## has to quote the receiving ceiling or it promises money that never arrives.
var _next_salvage_cap: int = -1

func setup(sim: Sim, theme: Dictionary) -> void:
	_sim = sim
	_theme = theme
	layer = 1

	_stats = Label.new()
	_stats.position = Vector2(14, 640)
	_stats.add_theme_font_size_override("font_size", 18)
	_stats.add_theme_color_override("font_color", _color("text"))
	add_child(_stats)

	_hint = Label.new()
	_hint.position = Vector2(14, 686)
	_hint.add_theme_font_size_override("font_size", 12)
	_hint.add_theme_color_override("font_color", _color("text_dim"))
	_hint.text = "left-click: build / upgrade / buy ground  ·  right-click a turret: sell  ·  T retarget  ·  scroll: zoom  ·  middle-drag: pan  ·  Z reset view  ·  Q weapon  ·  E call wave early  ·  1-4 speed  ·  space pause  ·  [ ] board  ·  M mute  ·  F3 debug  ·  R restart"
	add_child(_hint)

	_level = Label.new()
	_level.position = Vector2(14, 14)
	_level.add_theme_font_size_override("font_size", 20)
	_level.add_theme_color_override("font_color", _color("text_dim"))
	add_child(_level)

	# What is coming next. With five drone classes on the board, "what is in the
	# next wave" is the difference between planning a board and guessing at one -
	# and it is information the wave file already has, so withholding it is not
	# difficulty, it is just opacity.
	_preview = Label.new()
	_preview.position = Vector2(14, 46)
	_preview.add_theme_font_size_override("font_size", 14)
	_preview.add_theme_color_override("font_color", _color("text_dim"))
	add_child(_preview)

	# What actually happened, once it is over. A tower defence gives you almost no
	# feedback on WHY you won or lost - the board is a blur at 4x and then it is a
	# banner - and "which of my four weapon families was doing the work" is the one
	# question every build decision in the next act depends on.
	_debrief = Label.new()
	_debrief.position = Vector2(0, 372)
	_debrief.size = Vector2(1280, 260)
	_debrief.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_debrief.add_theme_font_size_override("font_size", 17)
	_debrief.add_theme_color_override("font_color", _color("text"))
	_debrief.visible = false
	add_child(_debrief)

	_banner = Label.new()
	_banner.position = Vector2(0, 300)
	_banner.size = Vector2(1280, 120)
	_banner.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_banner.add_theme_font_size_override("font_size", 46)
	_banner.visible = false
	add_child(_banner)

## Rebuilt only when one of the displayed values actually changes. Formatting a
## string every frame for a label that changes a few times a second is a per-frame
## allocation the project can trivially avoid.
var _last_signature: int = -1

func set_level(name: String, index: int, total: int, inherited: int = 0,
		next_extends: bool = false, dropped: int = 0, stood_down: int = 0,
		salvage: int = 0, next_salvage_cap: int = -1) -> void:
	_level_text = "%s      LEVEL %d/%d" % [name, index + 1, total]
	if inherited > 0:
		# Without this the inherited turrets read as a bug - a board you did not
		# build, on a level you have not played.
		_level_text += "      %d TURRETS HELD OVER" % inherited
	if salvage > 0:
		# A new board cannot take your turrets, so it takes what they were worth.
		# Saying so is the difference between continuity and apparent deletion.
		_level_text += "      $%d SALVAGED FROM THE LAST BOARD" % salvage
	if stood_down > 0:
		# Not a loss - a cap. Saying so stops it reading as a bug.
		_level_text += "  ·  %d STOOD DOWN" % stood_down
	if dropped > 0:
		# You can build beside road that has not been revealed yet. When it is
		# revealed and runs through your turret, that turret is gone - and the
		# honesty rule says you get told, not left to notice.
		_level_text += "  ·  %d LOST TO THE NEW ROAD" % dropped
	_is_last_level = index + 1 >= total
	_next_extends = next_extends
	_next_salvage_cap = next_salvage_cap
	_level.text = _level_text

func refresh(speed: int, paused: bool, hovered_platform: int = -1,
		blueprint: int = 0, can_buy_ground: bool = false) -> void:
	var signature := hash([_sim.capital(), _sim.integrity(), _sim.wave_number(),
		speed, paused, _sim.result(), hovered_platform, blueprint, can_buy_ground,
		_sim.t_count, _sim.cells_bought(), _sim.can_send_wave(), _sim.next_wave_number(),
		-1 if hovered_platform < 0 else _sim.platform_tier(hovered_platform),
		-1 if hovered_platform < 0 else _sim.platform_priority(hovered_platform)])
	if signature == _last_signature:
		return
	_last_signature = signature
	var cost := _sim.blueprint_cost(blueprint)
	var affordable := _sim.capital() >= cost
	_stats.text = "CAPITAL $%d    INTEGRITY %d    WAVE %d/%d    TURRETS %d/%d    %s    %s" % [
		_sim.capital(),
		_sim.integrity(),
		maxi(_sim.wave_number(), 1), _sim.wave_count(),
		_sim.t_count, _sim.platform_limit(),
		"[Q] %s $%d%s" % [_sim.blueprint_display_name(blueprint).to_upper(), cost,
			"" if affordable else "  (short)"],
		"PAUSED" if paused else "%dx" % speed,
	]
	if can_buy_ground:
		_stats.text += "    BUY GROUND $%d" % _sim.next_cell_cost()
	if _sim.can_send_wave():
		_stats.text += "    [E] CALL WAVE +$%d" % _sim.send_wave_bonus()
	_refresh_preview()
	# Integrity is the run's real health bar, so it is the one number that
	# changes colour as it goes.
	var fraction := float(_sim.integrity()) / float(_sim.integrity_max())
	_stats.add_theme_color_override("font_color",
		_color("text") if fraction > 0.5 else _color("warn") if fraction > 0.2 else _color("bad"))

	# Hovering a turret swaps the blueprint readout for what that turret is and
	# what the next tier would cost - the information you need at the moment you
	# are deciding whether to upgrade it.
	if hovered_platform >= 0:
		var tier := _sim.platform_tier(hovered_platform)
		var family := _sim.platform_blueprint(hovered_platform)
		var upgrade := _sim.upgrade_cost(hovered_platform)
		var label := "%s  T%d  %.0f dps" % [
			_sim.tier_name(family, tier), tier + 1, _sim.platform_dps(hovered_platform)]
		if _sim.platform_splash(hovered_platform) > 0.0:
			label += "  splash %.0f" % _sim.platform_splash(hovered_platform)
		if _sim.platform_slow_factor(hovered_platform) < 1.0:
			label += "  slows to %d%%" % int(round(_sim.platform_slow_factor(hovered_platform) * 100.0))
		if _sim.platform_pierce(hovered_platform) > 0.0:
			label += "  pierces"
		if upgrade < 0:
			label += "   MAX TIER"
		else:
			label += "   upgrade $%d%s" % [upgrade, "" if _sim.capital() >= upgrade else "  (short)"]
		# What it is shooting at, and how to change it. Shown on the hovered turret
		# rather than as a global mode, because it is per-turret state and reading it
		# anywhere else would be reading someone else's orders.
		label += "   [T] targets %s" % _sim.priority_name(
			_sim.platform_priority(hovered_platform)).to_upper()
		label += "   right-click: sell $%d" % _sim.sell_value(hovered_platform)
		_stats.text = "CAPITAL $%d    INTEGRITY %d    WAVE %d/%d    %s" % [
			_sim.capital(), _sim.integrity(),
			maxi(_sim.wave_number(), 1), _sim.wave_count(), label]

	if not _sim.is_over():
		_banner.visible = false
		_debrief.visible = false
		return
	_banner.visible = true
	_debrief.visible = true
	_debrief.text = debrief_text()
	if _sim.result() == Sim.RESULT_WIN:
		if _is_last_level:
			_banner.text = "CONTRACT COMPLETE   ·   %d integrity   ·   R to replay" % _sim.integrity()
		elif _next_extends:
			_banner.text = "SECTOR HELD   ·   %d integrity   ·   N extends the corridor" % _sim.integrity()
		else:
			# A new board is a different map, so the turrets cannot come. Say it
			# here rather than letting it look like the game ate them - and say it
			# in the figure that will actually be paid, not the board's raw worth.
			_banner.text = "BOARD CLEARED   ·   %d integrity   ·   N moves to a new board, salvaging $%d" % [
				_sim.integrity(), quoted_salvage()]
		_banner.add_theme_color_override("font_color", _color("good"))
	else:
		_banner.text = "CORRIDOR LOST   ·   wave %d/%d   ·   R to retry" % [_sim.wave_number(), _sim.wave_count()]
		_banner.add_theme_color_override("font_color", _color("bad"))

## The end-of-act debrief, as text.
##
## Damage is per weapon FAMILY, which is both the honest unit (see Sim's
## _family_damage - an emplacement index moves when you sell) and the useful one:
## the decision it informs is "what should I build more of", not "which of my
## hundred and forty-four turrets had the best afternoon". Families that never
## fired are omitted rather than printed as zeroes; a line of noughts is not
## information.
func debrief_text() -> String:
	var lines := PackedStringArray()
	lines.append("%s DESTROYED   ·   %s LEAKED   ·   %d TURRETS STANDING   ·   $%s IN HAND" % [
		_thousands(_sim.kills()), _thousands(_sim.leaks()), _sim.t_count,
		_thousands(_sim.capital())])
	var total := 0
	for b in _sim.blueprint_count():
		total += _sim.family_damage(b)
	for b in _sim.blueprint_count():
		var damage := _sim.family_damage(b)
		if damage <= 0:
			continue
		lines.append("%-16s %12s damage  (%d%%)   %s kills" % [
			_sim.blueprint_display_name(b).to_upper(), _thousands(damage),
			int(round(float(damage) * 100.0 / float(maxi(total, 1)))),
			_thousands(_sim.family_kills(b))])
	if total <= 0:
		lines.append("nothing fired a shot")
	var tail := PackedStringArray()
	if _sim.sold() > 0:
		tail.append("%d sold" % _sim.sold())
	if _sim.early_calls() > 0:
		tail.append("%d waves called early" % _sim.early_calls())
	if _sim.cells_bought() > 0:
		tail.append("%d ground bought" % _sim.cells_bought())
	if not tail.is_empty():
		lines.append("  ·  ".join(tail))
	return "\n".join(lines)

## 1234567 -> "1,234,567". Godot has no thousands separator, and a seven-digit
## damage figure is unreadable without one.
func _thousands(value: int) -> String:
	var digits := str(absi(value))
	var out := ""
	var count := 0
	for i in range(digits.length() - 1, -1, -1):
		out = digits[i] + out
		count += 1
		if count % 3 == 0 and i > 0:
			out = "," + out
	return ("-" if value < 0 else "") + out

## What the next act will actually credit, which is the board's worth clamped to
## that act's own ceiling. Public so a test can hold it against what the sim then
## grants instead of trusting a string on screen.
func quoted_salvage() -> int:
	var worth := _sim.board_salvage()
	return worth if _next_salvage_cap < 0 else mini(worth, _next_salvage_cap)

func _refresh_preview() -> void:
	if _sim.is_over():
		_preview.text = ""
		return
	var groups := _sim.next_wave_preview()
	if groups.is_empty():
		_preview.text = ""
		return
	var parts := PackedStringArray()
	for entry in groups:
		var pair: Array = entry
		parts.append("%d x %s" % [int(pair[1]), str(pair[0])])
	_preview.text = "INCOMING  wave %d:  %s" % [_sim.next_wave_number(), "  ·  ".join(parts)]

func _color(key: String) -> Color:
	return Color(str(_theme.get(key, "#ff00ff")))
