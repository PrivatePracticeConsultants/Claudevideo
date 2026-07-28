class_name Hud
extends CanvasLayer

## Minimal P0 heads-up display: the four numbers you need to play, plus the
## end-of-engagement banner.
##
## Deliberately not here yet: targeting priority controls, send-wave-early, sell,
## tier upgrades and full unit inspection are the table-stakes QoL from section
## 3.6 and land with the economy in P2. The build menu is one blueprint because
## there is one blueprint.

var _sim: Sim
var _theme: Dictionary
var _stats: Label
var _hint: Label
var _banner: Label
var _level: Label
var _level_text: String = ""
var _is_last_level: bool = false
## Whether the level after this one is the next act on the same board. It changes
## what winning means - not "on to somewhere else" but "the road gets longer and
## everything you built stays" - so the banner has to say which.
var _next_extends: bool = false

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
	_hint.text = "click owned ground to build  ·  click a turret to upgrade  ·  click dim ground to buy it  ·  Q weapon  ·  1/2/3 speed  ·  space pause  ·  F3 debug  ·  R restart"
	add_child(_hint)

	_level = Label.new()
	_level.position = Vector2(14, 14)
	_level.add_theme_font_size_override("font_size", 20)
	_level.add_theme_color_override("font_color", _color("text_dim"))
	add_child(_level)

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
		next_extends: bool = false, dropped: int = 0) -> void:
	_level_text = "%s      LEVEL %d/%d" % [name, index + 1, total]
	if inherited > 0:
		# Without this the inherited turrets read as a bug - a board you did not
		# build, on a level you have not played.
		_level_text += "      %d TURRETS HELD OVER" % inherited
	if dropped > 0:
		# You can build beside road that has not been revealed yet. When it is
		# revealed and runs through your turret, that turret is gone - and the
		# honesty rule says you get told, not left to notice.
		_level_text += "  ·  %d LOST TO THE NEW ROAD" % dropped
	_is_last_level = index + 1 >= total
	_next_extends = next_extends
	_level.text = _level_text

func refresh(speed: int, paused: bool, hovered_platform: int = -1,
		blueprint: int = 0, can_buy_ground: bool = false) -> void:
	var signature := hash([_sim.capital(), _sim.integrity(), _sim.wave_number(),
		speed, paused, _sim.result(), hovered_platform, blueprint, can_buy_ground,
		_sim.t_count, _sim.cells_bought(),
		-1 if hovered_platform < 0 else _sim.platform_tier(hovered_platform)])
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
		if upgrade < 0:
			label += "   MAX TIER"
		else:
			label += "   upgrade $%d%s" % [upgrade, "" if _sim.capital() >= upgrade else "  (short)"]
		_stats.text = "CAPITAL $%d    INTEGRITY %d    WAVE %d/%d    %s" % [
			_sim.capital(), _sim.integrity(),
			maxi(_sim.wave_number(), 1), _sim.wave_count(), label]

	if not _sim.is_over():
		_banner.visible = false
		return
	_banner.visible = true
	if _sim.result() == Sim.RESULT_WIN:
		if _is_last_level:
			_banner.text = "CONTRACT COMPLETE   ·   %d integrity   ·   R to replay" % _sim.integrity()
		elif _next_extends:
			_banner.text = "SECTOR HELD   ·   %d integrity   ·   N extends the corridor" % _sim.integrity()
		else:
			_banner.text = "CORRIDOR HELD   ·   %d integrity   ·   N for next level" % _sim.integrity()
		_banner.add_theme_color_override("font_color", _color("good"))
	else:
		_banner.text = "CORRIDOR LOST   ·   wave %d/%d   ·   R to retry" % [_sim.wave_number(), _sim.wave_count()]
		_banner.add_theme_color_override("font_color", _color("bad"))

func _color(key: String) -> Color:
	return Color(str(_theme.get(key, "#ff00ff")))
