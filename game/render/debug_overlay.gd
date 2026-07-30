class_name DebugOverlay
extends CanvasLayer

## Performance instrumentation, present from the first phase rather than bolted
## on at the end.
##
## The reason it exists this early: the entity budget (250 enemies + 700
## projectiles at 60fps on an iPhone 11) is easy to blow and hard to notice, and
## a regression found in phase P6 is a rewrite while one found in P0 is an
## afternoon. Toggle with F3.
##
## On "allocations": Godot does not expose a per-frame allocation counter, so
## this reports the two honest proxies it does expose - live object count and
## static memory - plus the per-second delta of each. Both should sit flat during
## a wave. A number that climbs while entity counts are steady is the signal that
## something in the tick path is allocating. It is labelled as a proxy rather
## than as an allocation count, because claiming a measurement the engine is not
## making would be worse than not having it.

const SAMPLE_INTERVAL := 1.0

var _label: Label
var _sim: Sim
var _elapsed: float = 0.0
var _frames: int = 0
var _fps: float = 0.0
var _last_objects: int = 0
var _last_memory: int = 0
var _objects_delta: int = 0
var _memory_delta: int = 0
var _steps_this_frame: int = 0
var _peak_enemies: int = 0
var _peak_projectiles: int = 0

## Frame-time SHAPE, not the average.
##
## The average was the only thing reported here, and an average cannot show a
## stutter: a steady 40ms and an alternating 20/60ms have the same one and feel
## nothing alike. What a player calls "missing frames" is the long tail, so the
## worst frame in each sample window is kept, along with a count of frames that
## took more than twice the window's typical frame. Both reset per window, so
## the readout describes the last second rather than the whole session.
var _worst_ms: float = 0.0
var _worst_shown: float = 0.0
var _long_frames: int = 0
var _long_shown: int = 0

func setup(sim: Sim, text_color: Color) -> void:
	_sim = sim
	layer = 2
	_label = Label.new()
	_label.position = Vector2(10, 10)
	_label.add_theme_color_override("font_color", text_color)
	_label.add_theme_font_size_override("font_size", 13)
	add_child(_label)
	_last_objects = int(Performance.get_monitor(Performance.OBJECT_COUNT))
	_last_memory = int(Performance.get_monitor(Performance.MEMORY_STATIC))
	visible = false

func note_steps(steps: int) -> void:
	_steps_this_frame = steps

func _process(delta: float) -> void:
	_peak_enemies = maxi(_peak_enemies, _sim.e_live_count)
	_peak_projectiles = maxi(_peak_projectiles, _sim.p_live_count)
	if not visible:
		return
	_frames += 1
	_elapsed += delta
	var frame_ms := delta * 1000.0
	_worst_ms = maxf(_worst_ms, frame_ms)
	# Measured against the PREVIOUS window's average, because the current one is
	# not known until it ends. A hitch lasting longer than a window would want a
	# real histogram; this is a readout, not a profiler.
	if _fps > 0.0 and frame_ms > 2.0 * (1000.0 / _fps):
		_long_frames += 1
	if _elapsed >= SAMPLE_INTERVAL:
		_fps = float(_frames) / _elapsed
		_worst_shown = _worst_ms
		_long_shown = _long_frames
		_worst_ms = 0.0
		_long_frames = 0
		var objects := int(Performance.get_monitor(Performance.OBJECT_COUNT))
		var memory := int(Performance.get_monitor(Performance.MEMORY_STATIC))
		_objects_delta = objects - _last_objects
		_memory_delta = memory - _last_memory
		_last_objects = objects
		_last_memory = memory
		_frames = 0
		_elapsed = 0.0
	_label.text = "\n".join([
		"F3  debug",
		"fps            %.1f  (frame %.2f ms)" % [_fps, 0.0 if _fps <= 0.0 else 1000.0 / _fps],
		"worst frame    %.1f ms   long frames %d/s" % [_worst_shown, _long_shown],
		"sim steps/frame %d" % _steps_this_frame,
		"tick           %d" % _sim.tick(),
		"enemies        %d  (peak %d)" % [_sim.e_live_count, _peak_enemies],
		"projectiles    %d  (peak %d)" % [_sim.p_live_count, _peak_projectiles],
		"platforms      %d" % _sim.t_count,
		"draw calls     %d" % int(Performance.get_monitor(Performance.RENDER_TOTAL_DRAW_CALLS_IN_FRAME)),
		"objects        %d  (%+d/s)" % [_last_objects, _objects_delta],
		"static mem     %.2f MB  (%+.1f KB/s)" % [float(_last_memory) / 1048576.0, float(_memory_delta) / 1024.0],
		"rng draws      %d" % _sim.rng_draws(),
		"", "objects/mem deltas are the allocation proxy; both should sit flat mid-wave",
	])
