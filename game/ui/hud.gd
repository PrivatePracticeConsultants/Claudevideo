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
	_hint.text = "click a pad to build  ·  1/2/3 speed  ·  space pause  ·  F3 debug  ·  R restart"
	add_child(_hint)

	_banner = Label.new()
	_banner.position = Vector2(0, 300)
	_banner.size = Vector2(1280, 120)
	_banner.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_banner.add_theme_font_size_override("font_size", 46)
	_banner.visible = false
	add_child(_banner)

func refresh(speed: int, paused: bool) -> void:
	var cost := _sim.blueprint_cost(0)
	var affordable := _sim.capital() >= cost
	_stats.text = "CAPITAL $%d    INTEGRITY %d    WAVE %d/%d    %s    %s" % [
		_sim.capital(),
		_sim.integrity(),
		maxi(_sim.wave_number(), 1), _sim.wave_count(),
		"%s $%d%s" % [_sim.blueprint_name(0).to_upper(), cost, "" if affordable else "  (short)"],
		"PAUSED" if paused else "%dx" % speed,
	]
	# Integrity is the run's real health bar, so it is the one number that
	# changes colour as it goes.
	var fraction := float(_sim.integrity()) / float(_sim.integrity_max())
	_stats.add_theme_color_override("font_color",
		_color("text") if fraction > 0.5 else _color("warn") if fraction > 0.2 else _color("bad"))

	if not _sim.is_over():
		return
	_banner.visible = true
	if _sim.result() == Sim.RESULT_WIN:
		_banner.text = "CORRIDOR HELD   ·   %d integrity   ·   R to replay" % _sim.integrity()
		_banner.add_theme_color_override("font_color", _color("good"))
	else:
		_banner.text = "CORRIDOR LOST   ·   wave %d/%d   ·   R to retry" % [_sim.wave_number(), _sim.wave_count()]
		_banner.add_theme_color_override("font_color", _color("bad"))

func _color(key: String) -> Color:
	return Color(str(_theme.get(key, "#ff00ff")))
