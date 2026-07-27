class_name SimFixture
extends RefCounted

## Shared setup for simulation tests, plus the two scripted policies the P0
## acceptance gate is measured against.
##
## These policies are the ancestor of tools/balance_sim.py (phase P3): the whole
## reason the simulation is a headless RefCounted with no engine time in it is so
## that a policy can drive thousands of runs unattended.

const MAP := "highway_01"
const ENGAGEMENT := "highway_01_act1"

## Generous ceiling. The shipped engagement finishes in ~7,500 ticks; this only
## exists so a bug that stalls the wave director fails as a test rather than
## hanging the suite forever.
const MAX_TICKS := 40000

static func database() -> Database:
	return Database.load_engagement(MAP, ENGAGEMENT)

static func fresh(seed_value: int = 12345) -> Sim:
	return Sim.new(database(), seed_value)

## "Fill every pad as soon as it is affordable, in pad order." Deliberately
## naive - it is the floor of competent play, not good play, so a win here means
## the engagement is winnable and a loss here means it is not.
##
## Returns the command log it produced, so a replay can be fed the exact same
## input and checked for a bit-identical outcome.
static func run_greedy(sim: Sim) -> Dictionary:
	var log_tick := PackedInt32Array()
	var log_pad := PackedInt32Array()
	var log_bp := PackedInt32Array()
	var bp := sim.blueprint_index("ballistic")
	var next_pad := 0
	var ticks := 0
	while not sim.is_over() and ticks < MAX_TICKS:
		if next_pad < sim.pad_count() and sim.capital() >= sim.blueprint_cost(bp):
			sim.queue_place(sim.tick(), next_pad, bp)
			log_tick.append(sim.tick())
			log_pad.append(next_pad)
			log_bp.append(bp)
			next_pad += 1
		sim.step()
		ticks += 1
	return {"tick": log_tick, "pad": log_pad, "bp": log_bp, "ticks": ticks}

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
	var log_pad: PackedInt32Array = command_log["pad"]
	var log_bp: PackedInt32Array = command_log["bp"]
	for i in log_tick.size():
		sim.queue_place(log_tick[i], log_pad[i], log_bp[i])
	var ticks := 0
	while not sim.is_over() and ticks < MAX_TICKS:
		sim.step()
		ticks += 1
	return ticks
