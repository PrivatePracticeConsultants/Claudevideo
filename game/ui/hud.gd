class_name Hud
extends CanvasLayer

## Heads-up display: the numbers you need to play, what is coming next, the
## end-of-engagement banner, and the debrief under it.
##
## Section 3.6's table-stakes QoL is now all here - sell, send-wave-early, tier
## readouts, a preview of the next wave, and per-turret targeting orders on the
## turret you are hovering. Still deliberately absent: a build menu richer than
## one cycling key.

## How long a flash() notice stays up.
const NOTICE_SECONDS := 6.0

var _sim: Sim
var _theme: Dictionary
var _notice: Label
var _notice_text: String = ""
var _notice_left: float = 0.0
var _stats: Label
var _hint: Label
var _banner: Label
var _debrief: Label
## Dim panel behind both, so the numbers are readable over a lit board.
var _scrim: ColorRect
## Dark strips behind the top and bottom text bands, so the HUD survives whatever
## the terrain happens to be doing underneath it.
var _top_bar: ColorRect
var _bottom_bar: ColorRect
var _level: Label
var _preview: Label
var _level_text: String = ""
var _is_last_level: bool = false
## Whether the level after this one is the next act on the same board. It changes
## what winning means - not "on to somewhere else" but "the road gets longer and
## everything you built stays" - so the banner has to say which.
var _next_extends: bool = false
## Modules held on this board, and the three offered for winning this act. The
## offer is the only piece of UI in the game that asks for an answer rather than
## reporting one, so it goes where the eye already is - directly under the banner.
var _modules: PackedStringArray = PackedStringArray()
var _offer: PackedStringArray = PackedStringArray()

## The most salvage the NEXT act will accept, or -1 where none applies. The board
## you are finishing is worth far more than any one act will take, so the banner
## has to quote the receiving ceiling or it promises money that never arrives.
var _next_salvage_cap: int = -1

func setup(sim: Sim, theme: Dictionary) -> void:
	_sim = sim
	_theme = theme
	layer = 1

	# Dark strips behind the two text bands, added before anything else so they sit
	# underneath it.
	#
	# The HUD's colours were chosen against a board that was a dark sheet in a dark
	# void. The terrain is now lit ground with sunlight on it, and captured without
	# these the level name and the whole control hint line were simply illegible -
	# pale grey text on pale green grass. Text that has to survive an arbitrary
	# background needs a background of its own.
	_top_bar = ColorRect.new()
	_top_bar.color = Color(_color("background"), float(_theme.get("hud_bar_alpha", 0.55)))
	_top_bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_top_bar)
	_bottom_bar = ColorRect.new()
	_bottom_bar.color = Color(_color("background"), float(_theme.get("hud_bar_alpha", 0.55)))
	_bottom_bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_bottom_bar)

	_stats = Label.new()
	_stats.position = Vector2(14, 640)
	_stats.add_theme_font_size_override("font_size", 18)
	_stats.add_theme_color_override("font_color", _color("text"))
	add_child(_stats)

	_hint = Label.new()
	_hint.position = Vector2(14, 686)
	_hint.add_theme_font_size_override("font_size", 12)
	_hint.add_theme_color_override("font_color", _color("text_dim"))
	_hint.text = "left-click: build / upgrade / buy ground  ·  right-click a turret: sell  ·  T retarget  ·  O overcharge  ·  G/H doctrine at T4  ·  scroll: zoom  ·  middle-drag: pan  ·  Z reset view  ·  Q weapon  ·  E call wave early  ·  1-4 speed  ·  space pause  ·  [ ] board  ·  M mute  ·  F3 debug  ·  R restart"
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
	# Sits between the level title and the wave preview, and is hidden unless it
	# has something to say.
	_notice = Label.new()
	_notice.position = Vector2(14, 68)
	_notice.add_theme_font_size_override("font_size", 14)
	_notice.add_theme_color_override("font_color", _color("warn"))
	_notice.visible = false
	add_child(_notice)

	_preview = Label.new()
	_preview.position = Vector2(14, 46)
	_preview.add_theme_font_size_override("font_size", 14)
	_preview.add_theme_color_override("font_color", _color("text_dim"))
	add_child(_preview)

	# Added before the banner and the debrief so it sits behind them. Both are read
	# against a lit 3D board covered in turrets, and captured without it the
	# debrief's numbers ran straight through a row of emplacements and were simply
	# not legible.
	_scrim = ColorRect.new()
	_scrim.color = Color(_color("background"), float(_theme.get("scrim_alpha", 0.82)))
	_scrim.visible = false
	_scrim.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_scrim)

	# What actually happened, once it is over. A tower defence gives you almost no
	# feedback on WHY you won or lost - the board is a blur at 4x and then it is a
	# banner - and "which of my four weapon families was doing the work" is the one
	# question every build decision in the next act depends on.
	_debrief = Label.new()
	_debrief.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_debrief.add_theme_font_size_override("font_size", 17)
	_debrief.add_theme_color_override("font_color", _color("text"))
	_debrief.visible = false
	add_child(_debrief)

	_banner = Label.new()
	_banner.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_banner.add_theme_font_size_override("font_size", 38)
	_banner.visible = false
	add_child(_banner)

	_layout()
	# The window can be any size and the web canvas can change at any moment. The
	# project stretches with aspect "expand", so extra width appears to the RIGHT
	# of the 1280-unit baseline rather than around it - a panel hardcoded to 1280
	# is off-centre on anything wider, which is most screens.
	var viewport := get_viewport()
	if viewport != null:
		viewport.size_changed.connect(_layout)

## Centre the end-of-engagement panel on whatever the viewport actually is.
func _layout() -> void:
	var width := 1280.0
	var viewport := get_viewport()
	if viewport != null:
		width = maxf(viewport.get_visible_rect().size.x, 320.0)
	_top_bar.position = Vector2.ZERO
	_top_bar.size = Vector2(width, 76.0)
	_bottom_bar.position = Vector2(0.0, 626.0)
	_bottom_bar.size = Vector2(width, 94.0)
	_scrim.position = Vector2(0.0, BANNER_TOP - 22.0)
	_scrim.size = Vector2(width, 268.0)
	_banner.position = Vector2(0.0, BANNER_TOP)
	_banner.size = Vector2(width, 70.0)
	_debrief.position = Vector2(0.0, BANNER_TOP + 74.0)
	_debrief.size = Vector2(width, 170.0)

## Where the end-of-engagement panel starts, in the 720-unit baseline height.
const BANNER_TOP := 292.0

## Rebuilt only when one of the displayed values actually changes. Formatting a
## string every frame for a label that changes a few times a second is a per-frame
## allocation the project can trivially avoid.
var _last_signature: int = -1

func set_level(name: String, index: int, total: int, inherited: int = 0,
		next_extends: bool = false, dropped: int = 0, stood_down: int = 0,
		salvage: int = 0, next_salvage_cap: int = -1) -> void:
	_level_text = "%s      LEVEL %d/%d" % [name, index + 1, total]
	# Named where the level is named, because an affix is part of what this act IS
	# rather than something that happens during it. An unannounced modifier is a
	# surprise; an announced one is a decision about what to build.
	var affixes := _sim.affix_ids()
	if not affixes.is_empty():
		var badges := PackedStringArray()
		for id in affixes:
			badges.append(_sim.affix_name(id).to_upper())
		_level_text += "      « %s »" % "  ·  ".join(badges)
	# A dormant road is a question the player has to be TOLD is a question: cover
	# it now, or kill the Borer and never need to. Warned about only when a Borer
	# is actually coming - the road is present on every act of these boards so
	# nothing ever materialises under a line already built, and a dormant road
	# with nothing to open it is scenery rather than a decision.
	if _sim.breaches_opened() > 0:
		_level_text += "      BREACH OPEN"
	elif _sim.fields_borers():
		_level_text += "      BREACH ARMED"
	if not _modules.is_empty():
		var names := PackedStringArray()
		for id in _modules:
			var text := Database.module_text(id)
			if not text.is_empty():
				names.append(text[0].to_upper())
		_level_text += "      %s" % "  ·  ".join(names)
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
		# A turret standing where the next act's road runs is gone, and the
		# honesty rule says you get told rather than left to notice. Every act of
		# a board now runs the same road, so this cannot fire today; it stays
		# because the day a board does reshape itself, silence would be the bug.
		_level_text += "  ·  %d LOST TO THE NEW ROAD" % dropped
	_is_last_level = index + 1 >= total
	_next_extends = next_extends
	_next_salvage_cap = next_salvage_cap
	_level.text = _level_text

func set_modules(ids: PackedStringArray) -> void:
	_modules = ids
	_last_signature = -1

## A short-lived notice, shown under the level title and then gone.
##
## For things the game did on the player's behalf rather than things they chose.
## Deliberately not the banner: the banner is for the act's result and taking it
## over for a settings message would step on the one thing that must always be
## readable.
func flash(message: String) -> void:
	_notice_text = message
	_notice_left = NOTICE_SECONDS
	if _notice != null:
		_notice.text = message
		_notice.visible = true

func _process(delta: float) -> void:
	if _notice_left <= 0.0:
		return
	_notice_left -= delta
	if _notice_left <= 0.0 and _notice != null:
		_notice.visible = false
		_notice_text = ""

func set_offer(ids: PackedStringArray) -> void:
	_offer = ids
	_last_signature = -1

## The three on offer, as the lines the player chooses between.
func offer_text() -> String:
	if _offer.is_empty():
		return ""
	var lines := PackedStringArray()
	lines.append("FIT ONE MODULE FOR THE REST OF THIS BOARD")
	for i in _offer.size():
		var text := Database.module_text(_offer[i])
		if text.size() < 2:
			continue
		lines.append("[%d]  %s  -  %s" % [i + 1, text[0].to_upper(), text[1]])
	return "\n".join(lines)

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
		# The damage type sits next to the price because that is the moment the
		# choice is made: the preview says what is coming, this says what this $200
		# is good against.
		"[Q] %s %s $%d%s" % [_sim.blueprint_display_name(blueprint).to_upper(),
			_sim.damage_type_name(_sim.blueprint_damage_type(blueprint)).to_upper(),
			cost, "" if affordable else "  (short)"],
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
		# What the neighbours are worth to it. Without this the mechanic is a number
		# that changes for reasons the player cannot see, which is worse than not
		# having it: the links are drawn on the board, and this says what they add up
		# to on the turret you are actually looking at.
		var links := _sim.platform_link_count(hovered_platform)
		if links > 0:
			var parts := PackedStringArray()
			if _sim.platform_rate_bonus(hovered_platform) > 0.0:
				parts.append("+%d%% rate" % int(round(_sim.platform_rate_bonus(hovered_platform) * 100.0)))
			if _sim.platform_damage_bonus(hovered_platform) > 0.0:
				parts.append("+%d%% dmg" % int(round(_sim.platform_damage_bonus(hovered_platform) * 100.0)))
			if _sim.platform_range_bonus(hovered_platform) > 0.0:
				parts.append("+%d%% range" % int(round(_sim.platform_range_bonus(hovered_platform) * 100.0)))
			if not parts.is_empty():
				label += "   LINKED x%d  %s" % [links, " ".join(parts)]
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
		_scrim.visible = false
		return
	_banner.visible = true
	_debrief.visible = true
	_scrim.visible = true
	# The offer replaces the debrief while it is standing. Both at once is a wall
	# of text at the one moment the player is being asked to decide something, and
	# the debrief is still there the moment they have chosen.
	_debrief.text = offer_text() if not _offer.is_empty() else debrief_text()
	if _sim.result() == Sim.RESULT_WIN:
		if _is_last_level:
			_banner.text = "CONTRACT COMPLETE   ·   %d integrity   ·   R to replay" % _sim.integrity()
		elif _next_extends:
			# The board is the same board next act - same road, same ground, same
			# turrets. What changes is what walks it, so say that instead of the
			# old promise that the corridor would grow.
			_banner.text = "SECTOR HELD   ·   %d integrity   ·   N holds the same ground, heavier drones" % _sim.integrity()
		else:
			# A new board is a different map, so the turrets cannot come. Say it
			# here rather than letting it look like the game ate them - and say it
			# in the figure that will actually be paid, not the board's raw worth.
			_banner.text = "BOARD CLEARED   ·   %d integrity   ·   N moves to a new board, salvaging $%d" % [
				_sim.integrity(), quoted_salvage()]
		_banner.add_theme_color_override("font_color", _color("good"))
	else:
		# The board's own noun. "Corridor lost" on a board with no corridor reads
		# as a bug in the game rather than as a defeat.
		_banner.text = "%s LOST   ·   wave %d/%d   ·   R to retry" % [
			"STATION" if _sim.is_outpost() else "CORRIDOR",
			_sim.wave_number(), _sim.wave_count()]
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
	# Grouped by armour class, not by group, because the armour class is the
	# question the preview now exists to answer: which of your four families is
	# the next $200 worth spending on. Tagging every group instead was tried and
	# ran off the right-hand edge of a 1280-wide window, which lost the Shielded
	# drones at the end - the ones that most needed reading.
	var by_class := {}
	var order := PackedInt32Array()
	for entry in groups:
		var group: Array = entry
		var armour_class := int(group[2])
		if not by_class.has(armour_class):
			# Plain Arrays, not Packed ones. A PackedStringArray held inside a
			# Dictionary is a VALUE: appending to a local copy of it drops the
			# append on the floor, silently, and the preview reads "LIGHT 371 ()".
			# Found in a screenshot, which is the only place it was visible.
			by_class[armour_class] = [0, []]
			order.append(armour_class)
		var bucket: Array = by_class[armour_class]
		bucket[0] = int(bucket[0]) + int(group[1])
		(bucket[1] as Array).append("%d %s" % [int(group[1]), str(group[0])])
	var parts := PackedStringArray()
	for armour_class in order:
		var bucket: Array = by_class[armour_class]
		parts.append("%s %d (%s)" % [_sim.armour_class_name(armour_class).to_upper(),
			int(bucket[0]), ", ".join(bucket[1] as Array)])
	_preview.text = "INCOMING  wave %d:   %s" % [_sim.next_wave_number(), "    ".join(parts)]

func _color(key: String) -> Color:
	return Color(str(_theme.get(key, "#ff00ff")))
