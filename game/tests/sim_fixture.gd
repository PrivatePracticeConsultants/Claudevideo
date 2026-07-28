class_name SimFixture
extends RefCounted

## Shared setup for simulation tests, plus the scripted policies the acceptance
## gates are measured against.
##
## These policies are the ancestor of tools/balance_sim.py (phase P3): the whole
## reason the simulation is a headless RefCounted with no engine time in it is so
## that a policy can drive thousands of runs unattended.

const MAP := "highway_01"
const ENGAGEMENT := "highway_act1"

## The scripted arsenal mix, as "every Nth turret". Not optimal play - just a mix
## that exercises every family, so a level clearable only with one of them shows
## up as a loss here, and a family the policy never builds cannot quietly become
## dead content no test would notice.
const CANNON_SHARE := 3
const SUPPRESSOR_SHARE := 5
const RAILGUN_SHARE := 7

## Generous ceiling. It only exists so a bug that stalls the wave director fails
## as a test rather than hanging the suite forever.
const MAX_TICKS := 90000

## Candidate build sites are sampled along the corridor at this spacing, on both
## sides. Wider than min_platform_spacing so neighbouring sites do not reject
## each other.
const SITE_STRIDE := 46.0

## Which campaign chains the suite plays end to end.
##
## Levels are grouped into chains of four acts on one board, and a later act is
## entered carrying the previous act's turrets - so an act cannot be balance
## checked on its own. The unit of testing is therefore a whole chain.
##
## The default is a cheap tripwire, not full coverage. Measured: the chain test
## alone is ~480s of a ~590s budget with a mid-late board in the sample, and a
## suite that cannot finish is a suite nobody runs. These two are the teaching
## board and one far enough in to field Bulwarks and matter.
##
## Everything past here is covered by LASTLINE_FULL_CAMPAIGN=1, the pre-release
## run, and by tools/balance_probe.gd, which is played across all 48 acts before
## a release.
const SAMPLED_CHAINS := ["highway_01", "capital_01"]

## Every campaign level, grouped into chains, in campaign order.
static func campaign_chains() -> Array:
	var full := full_campaign_requested()
	var chains := []
	var index := {}
	for level in Database.load_levels():
		var map_id := str(level["map"])
		if not full and not SAMPLED_CHAINS.has(map_id):
			continue
		if not index.has(map_id):
			index[map_id] = chains.size()
			chains.append([])
		(chains[int(index[map_id])] as Array).append(level)
	return chains

static func full_campaign_requested() -> bool:
	return OS.get_environment("LASTLINE_FULL_CAMPAIGN") != ""

## Play a chain the way the game hands it to a player: act by act, each one
## inheriting what the last act left standing.
##
## Stops at the first act that is not won, because there is nothing to carry
## forward from a loss. Returns one entry per act played:
##   {level, sim, won, inherited, incoming}
## where `incoming` is the board this act was handed - which is what makes the
## idle side of the balance gate answerable (see `idle_run`).
static func play_chain(levels: Array) -> Array:
	var results := []
	var carry := {}
	for level in levels:
		var incoming := carry
		var sim := start_act(level, incoming, 20260727 + results.size())
		var inherited := sim.t_count
		run_greedy(sim)
		var won := sim.result() == Sim.RESULT_WIN
		results.append({"level": level, "sim": sim, "won": won,
			"inherited": inherited, "incoming": incoming})
		if not won:
			break
		carry = sim.board_snapshot()
	return results

## One act, opened with the board offered to it. An act that starts a chain
## refuses the offer, exactly as main.gd does.
static func start_act(level: Dictionary, offered: Dictionary,
		seed_value: int = 12345) -> Sim:
	var db := database(str(level["map"]), str(level["engagement"]))
	var sim := Sim.new(db, seed_value)
	if offered.is_empty():
		return sim
	if bool(db.engagement.get("carries_forward", false)):
		sim.adopt(offered.get("platforms", []), offered.get("cells", PackedInt32Array()))
	else:
		# A new board. The turrets cannot come, so what they were worth does.
		sim.grant_salvage(int(offered.get("salvage", 0)))
	return sim

## Build nothing for a whole act, starting from the board it was handed. An act
## that an inherited board clears unattended is not an act, it is a cutscene.
static func idle_run(level: Dictionary, incoming: Dictionary, seed_value: int = 12345) -> Sim:
	var sim := start_act(level, incoming, seed_value)
	run_idle(sim)
	return sim

static func database(map_id: String = MAP, engagement_id: String = ENGAGEMENT) -> Database:
	return Database.load_engagement(map_id, engagement_id)

static func fresh(seed_value: int = 12345) -> Sim:
	return Sim.new(database(), seed_value)

static func for_level(map_id: String, engagement_id: String, seed_value: int = 12345) -> Sim:
	return Sim.new(database(map_id, engagement_id), seed_value)

## Every legal-looking spot alongside the road, as flat [x, y, x, y, ...] in
## whole units. Ordered along the path so a policy consuming them in order builds
## from the entrance outward, which is roughly what a person does.
## Offsets from the road to try at each step, as a fraction of the way from
## min_distance_from_path to max_distance_from_path.
##
## Several rather than one, because the band of ground an act STARTS owning
## narrows as the campaign goes on. A single mid-band offset stops landing on
## owned ground somewhere around level 30, and the policy then quietly finds
## half the sites it should - measured as the 34th level fielding 63 turrets
## against a limit of 144, and losing. Trying nearer offsets first keeps coverage
## tight to the road, which is also what a person does.
const SITE_OFFSETS := [0.35, 0.5, 0.7, 0.9]

static func candidate_sites(sim: Sim) -> PackedInt32Array:
	var sites := PackedInt32Array()
	var near := sim.build_min_distance()
	var span := sim.build_max_distance() - near
	var prog := 0.0
	while prog <= sim.path_length():
		for side: float in [-1.0, 1.0]:
			for fraction: float in SITE_OFFSETS:
				sim.sample_for_render(prog, (near + span * fraction) * side)
				var x := roundi(sim.out_x())
				var y := roundi(sim.out_y())
				# Only keep spots that fail purely for affordability reasons, so
				# the list is stable regardless of how much Capital is in hand.
				var verdict := sim.can_build_at(float(x), float(y), 0)
				if verdict == Sim.BUILD_OK or verdict == Sim.BUILD_NO_CAPITAL \
						or verdict == Sim.BUILD_AT_LIMIT:
					sites.append(x)
					sites.append(y)
					break  # one spot per side per step; the rest are too close
		prog += SITE_STRIDE
	return sites

## A known-good buildable spot on the current map, as [x, y] in whole units.
## `skip` picks a later one, far enough along the road not to collide with the
## first. Tests ask for these rather than hardcoding coordinates, because a
## coordinate that was valid stops being valid the moment a map is re-authored -
## and it then fails as "the command was rejected", which looks like a logic bug.
static func a_site(sim: Sim, skip: int = 0) -> PackedInt32Array:
	var sites := candidate_sites(sim)
	var index := mini(skip * 2, maxi(sites.size() - 2, 0))
	var out := PackedInt32Array()
	out.append(sites[index])
	out.append(sites[index + 1])
	return out

## A competent baseline: build along the road while there is somewhere to build
## and Capital to do it, otherwise pour Capital into upgrading the weakest
## turret. Deliberately not clever - it is the floor of reasonable play, so a win
## here means the level is winnable and a loss means it is not.
##
## Returns the command log, so a replay can be fed identical input and checked
## for a bit-identical outcome.
## `stop_wave`/`stop_enemies` let a caller halt the run at an interesting moment
## (the screenshot tool uses them). Default is to play to the end.
static func run_greedy(sim: Sim, allow_upgrades: bool = true,
		stop_wave: int = -1, stop_enemies: int = 0) -> Dictionary:
	var log_tick := PackedInt32Array()
	var log_kind := PackedInt32Array()
	var log_a := PackedInt32Array()
	var log_b := PackedInt32Array()
	var log_c := PackedInt32Array()
	var ballistic := sim.blueprint_index("ballistic")
	var cannon := maxi(sim.blueprint_index("cannon"), 0)
	var suppressor := maxi(sim.blueprint_index("suppressor"), 0)
	var railgun := maxi(sim.blueprint_index("railgun"), 0)
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
			# Every fifth position is a Suppressor and every third of the rest a
			# Cannon, so swarm waves meet area damage and fast waves meet
			# something that slows them down.
			var blueprint := ballistic
			if (sim.t_count % RAILGUN_SHARE) == RAILGUN_SHARE - 1:
				blueprint = railgun
			elif (sim.t_count % SUPPRESSOR_SHARE) == SUPPRESSOR_SHARE - 1:
				blueprint = suppressor
			elif (sim.t_count % CANNON_SHARE) == CANNON_SHARE - 1:
				blueprint = cannon
			var verdict := sim.can_build_at(float(x), float(y), blueprint)
			if verdict == Sim.BUILD_OK:
				sim.queue_place(sim.tick(), x, y, blueprint)
				log_tick.append(sim.tick())
				log_kind.append(Sim.CMD_PLACE)
				log_a.append(x)
				log_b.append(y)
				log_c.append(blueprint)
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
				log_c.append(0)
		sim.step()
		ticks += 1
		if stop_wave > 0 and sim.wave_number() >= stop_wave and sim.e_live_count >= stop_enemies:
			break
	return {"tick": log_tick, "kind": log_kind, "a": log_a, "b": log_b, "c": log_c,
		"ticks": ticks, "integrity_at_wave": integrity_at_wave}

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
	var log_c: PackedInt32Array = command_log["c"]
	for i in log_tick.size():
		# Every command kind, not just the two the greedy policy happens to emit.
		# An `else: upgrade` fallback silently replays a sell as an upgrade, which
		# is a desync that would look like a simulation bug rather than a fixture
		# one - and the determinism tests would have been proving nothing about
		# any log containing the newer commands.
		match log_kind[i]:
			Sim.CMD_PLACE:
				sim.queue_place(log_tick[i], log_a[i], log_b[i], log_c[i])
			Sim.CMD_UPGRADE:
				sim.queue_upgrade(log_tick[i], log_a[i])
			Sim.CMD_BUY_CELL:
				sim.queue_buy_cell(log_tick[i], log_a[i], log_b[i])
			Sim.CMD_SELL:
				sim.queue_sell(log_tick[i], log_a[i])
			Sim.CMD_SEND_WAVE:
				sim.queue_send_wave(log_tick[i])
			Sim.CMD_SET_PRIORITY:
				sim.queue_priority(log_tick[i], log_a[i], log_b[i])
	var ticks := 0
	while not sim.is_over() and ticks < MAX_TICKS:
		sim.step()
		ticks += 1
	return ticks
