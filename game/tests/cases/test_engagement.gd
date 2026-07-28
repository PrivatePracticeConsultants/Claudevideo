extends TestCase

## The P0 acceptance gate's first half: a ten-wave engagement can be played from
## start to finish, and can be both won and lost.

func test_a_competent_run_wins_the_full_ten_waves() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim)
	assert_eq(sim.result(), Sim.RESULT_WIN, "filling every pad should clear the engagement")
	assert_eq(sim.wave_number(), sim.wave_count(), "all ten waves were reached")
	assert_eq(sim.wave_count(), 10, "the P0 engagement is ten waves")
	assert_gt(float(sim.integrity()), 0.0, "a win means integrity never hit zero")
	assert_eq(sim.e_live_count, 0, "a win means the board is clear")

func test_building_nothing_loses() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_idle(sim)
	assert_eq(sim.result(), Sim.RESULT_LOSS, "an undefended corridor must fail")
	assert_eq(sim.integrity(), 0, "integrity floors at zero rather than going negative")
	assert_eq(sim.kills(), 0, "nothing was built, so nothing died")
	assert_lt(float(sim.wave_number()), 10.0, "the run ends before the last wave")

func test_the_difficulty_spike_is_authored_into_the_waves() -> void:
	# Section 4.2: losses must cluster late because the waves are authored that
	# way, not because of any hidden difficulty adjustment.
	#
	# This checks the authoring, not one policy's luck. Requiring the scripted
	# run to finish damaged sounds stronger and is not: sweeping level 1 across
	# health multipliers, x0.91 wins untouched, x1.09 wins on 96 integrity and
	# x1.27 collapses to 25 leaks. The whole "won, but it cost something" band is
	# ~16% wide, so an assertion pinned inside it fails on any unrelated tuning
	# change and says nothing about whether the level is back-loaded.
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		var waves: Array = db.engagement["waves"]
		var third := maxi(1, waves.size() / 3)
		var opening := _head_count(waves, 0, third)
		var closing := _head_count(waves, waves.size() - third, waves.size())
		assert_gt(float(closing), float(opening) * 1.6,
			"%s: the closing waves must be substantially heavier than the opening ones"
				% str(level["name"]))

func _head_count(waves: Array, from_index: int, to_index: int) -> int:
	var total := 0
	for i in range(from_index, to_index):
		for group in ((waves[i] as Dictionary)["groups"] as Array):
			total += int((group as Dictionary)["count"])
	return total

func test_whatever_damage_a_competent_run_takes_it_takes_late() -> void:
	# The other half of section 4.2, and the half that is actually about play: a
	# competent run must not be bleeding in the opening waves. Conditional on
	# damage happening at all, because whether it does depends on the policy - but
	# *when* it happens is the authored property.
	var sim := SimFixture.fresh()
	var log := SimFixture.run_greedy(sim)
	var integrity_at_wave: PackedInt32Array = log["integrity_at_wave"]
	assert_eq(sim.result(), Sim.RESULT_WIN, "the level is winnable by a competent run")
	assert_gte(float(integrity_at_wave.size()), 8.0, "the run reached at least wave 8")
	assert_eq(integrity_at_wave[7], sim.integrity_max(),
		"waves 1-7 should cost a competent player nothing")

func test_upgrading_is_what_carries_the_later_levels() -> void:
	# The deployment limit exists so the tier ladder matters. If a level could be
	# cleared by tier-1 spam alone, upgrades would be dead content - so check the
	# competent run actually reaches higher tiers.
	# A board-opening act, so it is judged from a standing start rather than
	# from an inheritance.
	var sim := SimFixture.for_level("port_01", "port_act1")
	SimFixture.run_greedy(sim)
	assert_eq(sim.result(), Sim.RESULT_WIN, "the Port Authority approach is winnable")
	var highest := 0
	for i in sim.t_count:
		highest = maxi(highest, sim.platform_tier(i))
	assert_gt(float(highest), 0.0, "a winning act II run upgrades past tier 1")
	assert_lte(float(sim.t_count), float(sim.platform_limit()), "and respects the deployment limit")

func test_every_campaign_chain_is_winnable_and_losable() -> void:
	# The single most valuable balance guard in the project: a change that makes
	# a level impossible - or trivial - fails here rather than in play.
	#
	# It plays whole chains rather than single levels, because a level in the
	# middle of a chain is entered carrying the previous act's board. Judging
	# act 3 from a standing start would measure a game nobody plays: it is
	# authored against the turrets act 2 leaves behind, and its own Capital is
	# smaller because of them.
	#
	# Runs a sample of the campaign by default and all of it under
	# LASTLINE_FULL_CAMPAIGN=1; see SimFixture.campaign_chains for why.
	var chains := SimFixture.campaign_chains()
	assert_gt(float(chains.size()), 0.0, "there are chains to check")
	for chain in chains:
		var played := SimFixture.play_chain(chain)
		assert_eq(played.size(), (chain as Array).size(),
			"the chain on %s should run to its last act" % str((chain as Array)[0]["map"]))
		for entry in played:
			var name := str((entry["level"] as Dictionary)["name"])
			var sim: Sim = entry["sim"]
			assert_true(bool(entry["won"]), "%s must be winnable by a competent run" % name)
			assert_lte(float(sim.t_count), float(sim.platform_limit()),
				"%s respects its deployment limit" % name)
			assert_eq(sim.carry_dropped(), 0,
				"%s should not lose inherited turrets to the extended corridor" % name)
			# Losable *from the board it inherits*, which is the demanding form of
			# the question once boards carry forward.
			var idle := SimFixture.idle_run(entry["level"], entry["incoming"])
			assert_eq(idle.result(), Sim.RESULT_LOSS,
				"%s must still be losable by an idle run" % name)

func test_every_campaign_level_at_least_loads_and_starts() -> void:
	# Cheap, so it covers all twenty-four even when the expensive test above is
	# sampling. Catches a broken map or wave file immediately.
	for level in Database.load_levels():
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		assert_true(db.is_valid(), "%s: %s" % [level["name"], db.error_text()])
		var sim := Sim.new(db, 1)
		for _t in 300:
			sim.step()
		assert_false(sim.is_over(), "%s should still be running 10 seconds in" % level["name"])
		assert_gt(sim.path_length(), 0.0, "%s has a path" % level["name"])

func test_every_spawned_enemy_is_accounted_for() -> void:
	# The honesty rule applied to the sim: an enemy either dies or leaks. If
	# these ever fail to add up, something is being silently dropped.
	var sim := SimFixture.fresh()
	SimFixture.run_greedy(sim)
	var expected := 0
	for wave in (SimFixture.database().engagement["waves"] as Array):
		for group in ((wave as Dictionary)["groups"] as Array):
			expected += int((group as Dictionary)["count"])
	assert_eq(sim.kills() + sim.leaks(), expected, "kills + leaks must equal everything spawned")
	assert_eq(sim.spawn_overflow(), 0, "no spawn was dropped for lack of pool space")

func test_a_loss_stops_the_simulation() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_idle(sim)
	var frozen := sim.state_hash()
	var ended_on := sim.tick()
	for _i in 100:
		sim.step()
	assert_eq(sim.tick(), ended_on, "stepping a finished engagement must do nothing")
	assert_eq(sim.state_hash(), frozen, "and must not mutate state")
