extends SceneTree

## What the support-link mechanic is actually worth in a real game.
##
## A mechanic that only fires in a unit test is not a mechanic. This plays a few
## representative levels with the same policy the suite uses and prints how many
## turrets ended up linked and what they were getting, so "links matter" is a
## measurement rather than a hope.
##
##   godot --headless --path game --script res://tools/link_probe.gd
##
## Read the mid-chain rows with care. Every level here is played from a CLEAN
## start, which is not how acts 2-4 are met in play - they arrive carrying the
## previous act's board, tiers included. From nothing, the greedy policy spends
## on width rather than height, and a board of all-tier-1 turrets reports zero
## links and +0% across the board. That is correct, not a bug: a lender needs
## `tier_scaling * t_tier > 0` and t_tier is zero-based, so links begin at tier
## 2. reactor_act3 is the row that looks alarming and is not - the balance probe
## enters it carrying 144 tier-4 turrets.
const SAMPLE := [0, 8, 16, 26]

func _init() -> void:
	var levels := Database.load_levels()
	print("%-16s%8s%22s%8s%7s%34s%8s" % ["engagement", "turrets", "tiers 1/2/3/4",
		"linked", "links", "best  rate / dmg / range", "mean"])
	for index in SAMPLE:
		var level: Dictionary = levels[index]
		var sim := SimFixture.start_act(level, {}, 20260727 + index)
		SimFixture.run_greedy(sim)
		# The drawn pair list is built on demand; nothing has asked for it here.
		sim.rebuild_link_pairs(SimRenderer3D.MAX_DRAWN_LINKS)
		var tiers := PackedInt32Array()
		tiers.resize(4)
		var linked := 0
		var best_rate := 0.0
		var best_damage := 0.0
		var best_range := 0.0
		var sum_rate := 0.0
		for i in sim.t_count:
			tiers[clampi(sim.platform_tier(i), 0, 3)] += 1
			if sim.platform_link_count(i) > 0:
				linked += 1
			best_rate = maxf(best_rate, sim.platform_rate_bonus(i))
			best_damage = maxf(best_damage, sim.platform_damage_bonus(i))
			best_range = maxf(best_range, sim.platform_range_bonus(i))
			sum_rate += sim.platform_rate_bonus(i)
		print("%-16s%8d%22s%8d%7d%34s%7.1f%%" % [
			str(level["engagement"]), sim.t_count,
			"%d/%d/%d/%d" % [tiers[0], tiers[1], tiers[2], tiers[3]],
			linked, sim.link_count(),
			"+%.0f%% / +%.0f%% / +%.0f%%" % [best_rate * 100.0, best_damage * 100.0, best_range * 100.0],
			(sum_rate / maxf(float(sim.t_count), 1.0)) * 100.0])
	quit()
