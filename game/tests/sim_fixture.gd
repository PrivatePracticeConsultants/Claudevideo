class_name SimFixture
extends RefCounted

## Shared setup for simulation tests, plus the scripted policies the acceptance
## gates are measured against.
##
## These policies are the ancestor of tools/balance_sim.py (phase P3): the whole
## reason the simulation is a headless RefCounted with no engine time in it is so
## that a policy can drive thousands of runs unattended.

const MAP := "highway_01"
const ENGAGEMENT := "highway_01_act1"

## Generous ceiling. It only exists so a bug that stalls the wave director fails
## as a test rather than hanging the suite forever.
const MAX_TICKS := 90000

## Candidate build sites are sampled along the corridor at this spacing, on both
## sides. Wider than min_platform_spacing so neighbouring sites do not reject
## each other.
const SITE_STRIDE := 46.0

static func database(map_id: String = MAP, engagement_id: String = ENGAGEMENT) -> Database:
	return Database.load_engagement(map_id, engagement_id)

static func fresh(seed_value: int = 12345) -> Sim:
	return Sim.new(database(), seed_value)

static func for_level(map_id: String, engagement_id: String, seed_value: int = 12345) -> Sim:
	return Sim.new(database(map_id, engagement_id), seed_value)

## Every legal-looking spot alongside the road, as flat [x, y, x, y, ...] in
## whole units. Ordered along the path so a policy consuming them in order builds
## from the entrance outward, which is roughly what a person does.
static func candidate_sites(sim: Sim) -> PackedInt32Array:
	var sites := PackedInt32Array()
	var offset := (sim.build_min_distance() + sim.build_max_distance()) * 0.5
	var prog := 0.0
	while prog <= sim.path_length():
		for side in [-1.0, 1.0]:
			sim.sample_for_render(prog, offset * side)
			var x := roundi(sim.out_x())
			var y := roundi(sim.out_y())
			# Only keep spots that fail purely for affordability reasons, so the
			# list is stable regardless of how much Capital happens to be in hand.
			var verdict := sim.can_build_at(float(x), float(y), 0)
			if verdict == Sim.BUILD_OK or verdict == Sim.BUILD_NO_CAPITAL:
				sites.append(x)
				sites.append(y)
		prog += SITE_STRIDE
	return sites

## A competent baseline: build along the road while there is somewhere to build
## and Capital to do it, otherwise pour Capital into upgrading the weakest
## turret. Deliberately not clever - it is the floor of reasonable play, so a win
## here means the level is winnable and a loss means it is not.
##
## Returns the command log, so a replay can be fed identical input and checked
## for a bit-identical outcome.
static func run_greedy(sim: Sim, allow_upgrades: bool = true) -> Dictionary:
	var log_tick := PackedInt32Array()
	var log_kind := PackedInt32Array()
	var log_a := PackedInt32Array()
	var log_b := PackedInt32Array()
	var sites := candidate_sites(sim)
	var next_site := 0
	var ticks := 0
	# Integrity as each wave begins. This is what makes "the damage lands late"
	# a measurable claim rather than an impression, and it is the shape of data
	# tools/balance_sim.py will want per run.
	var integrity_at_wave := PackedInt32Array()

	while not sim.is_over() and ticks < MAX_TICKS:
		while integrity_at_wave.size() < sim.wave_number():
			integrity_at_wave.append(sim.integrity())
		# Coverage first, then depth: take positions until the deployment limit is
		# reached, then spend everything on upgrading what is already down.
		if sim.t_count < sim.platform_limit() and next_site + 1 < sites.size():
			var x := sites[next_site]
			var y := sites[next_site + 1]
			var verdict := sim.can_build_at(float(x), float(y), 0)
			if verdict == Sim.BUILD_OK:
				sim.queue_place(sim.tick(), x, y, 0)
				log_tick.append(sim.tick())
				log_kind.append(Sim.CMD_PLACE)
				log_a.append(x)
				log_b.append(y)
				next_site += 2
			elif verdict != Sim.BUILD_NO_CAPITAL:
				next_site += 2  # permanently unusable now something sits nearby
		elif allow_upgrades:
			var target := _weakest_upgradable(sim)
			if target >= 0:
				sim.queue_upgrade(sim.tick(), target)
				log_tick.append(sim.tick())
				log_kind.append(Sim.CMD_UPGRADE)
				log_a.append(target)
				log_b.append(0)
		sim.step()
		ticks += 1
	return {"tick": log_tick, "kind": log_kind, "a": log_a, "b": log_b, "ticks": ticks,
		"integrity_at_wave": integrity_at_wave}

## Lowest-tier platform that can be afforded right now; ties go to the lowest
## index so the choice is stable and replays stay aligned.
static func _weakest_upgradable(sim: Sim) -> int:
	var best := -1
	var best_tier := 1 << 30
	for i in sim.t_count:
		if not sim.can_upgrade(i):
			continue
		if sim.platform_tier(i) < best_tier:
			best_tier = sim.platform_tier(i)
			best = i
	return best

## Build nothing and watch the corridor fail.
static func run_idle(sim: Sim) -> int:
	var ticks := 0
	while not sim.is_over() and ticks < MAX_TICKS:
		sim.step()
		ticks += 1
	return ticks

## Replay a recorded log into a fresh simulation, queuing everything up front.
static func replay(sim: Sim, command_log: Dictionary) -> int:
	var log_tick: PackedInt32Array = command_log["tick"]
	var log_kind: PackedInt32Array = command_log["kind"]
	var log_a: PackedInt32Array = command_log["a"]
	var log_b: PackedInt32Array = command_log["b"]
	for i in log_tick.size():
		if log_kind[i] == Sim.CMD_PLACE:
			sim.queue_place(log_tick[i], log_a[i], log_b[i], 0)
		else:
			sim.queue_upgrade(log_tick[i], log_a[i])
	var ticks := 0
	while not sim.is_over() and ticks < MAX_TICKS:
		sim.step()
		ticks += 1
	return ticks
