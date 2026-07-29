extends TestCase

## Act affixes: named, announced modifiers on everything an act sends.
##
## They exist so two acts with the same drone list are not the same problem
## twice. A Swift act wants reach; a Hardened act wants damage per shot rather
## than shots per second; a Relentless act takes away the between-wave breathing
## room a plan needed.
##
## Every one of them is a multiplier or a flat bonus on a value the simulation
## already had, applied once when the act loads. Nothing here happens in a tick -
## which is why the system costs nothing at runtime and cannot desync a replay.

const BASE_MAP := "highway_01"
const BASE_ACT := "highway_act1"

## The same act, with an affix list forced onto it. Mutating the loaded database
## rather than authoring a fixture wave file is deliberate: it holds every other
## number in the act still, so a difference is the affix and nothing else.
func _act_with(ids: Array) -> Sim:
	var db := SimFixture.database(BASE_MAP, BASE_ACT)
	db.engagement = db.engagement.duplicate(true)
	db.engagement["affixes"] = ids
	return Sim.new(db, 12345)

func _plain() -> Sim:
	return _act_with([])

# --- the pool has to be honest ------------------------------------------------

func test_every_affix_in_the_pool_is_used_somewhere() -> void:
	# An affix nobody meets is dead content, and one listed on an act that does
	# not exist is a typo the player would experience as nothing happening.
	var db := SimFixture.database(BASE_MAP, BASE_ACT)
	var used := {}
	for level in Database.load_levels():
		var act := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		for id: Variant in (act.engagement.get("affixes", []) as Array):
			used[str(id)] = true
	for id: String in db.affixes.keys():
		if id.begins_with("_"):
			continue
		assert_true(used.has(id), "%s is on an act somewhere in the campaign" % id)

func test_every_affix_is_announced() -> void:
	# A modifier you cannot read is a surprise, not a decision. Both strings are
	# required by the validator; this is the check that they reach the HUD.
	var sim := _plain()
	var db := SimFixture.database(BASE_MAP, BASE_ACT)
	for id: String in db.affixes.keys():
		if id.begins_with("_"):
			continue
		assert_false(sim.affix_name(id).is_empty(), "%s has a name to announce" % id)
		assert_false(sim.affix_blurb(id).is_empty(), "%s says what it does" % id)

func test_an_act_reports_the_affixes_it_was_given() -> void:
	var sim := _act_with(["swift", "hardened"])
	assert_eq(sim.affix_ids().size(), 2, "both are on the act")
	assert_eq(sim.affix_ids()[0], "swift", "in the order the file listed them")
	assert_eq(sim.affix_ids()[1], "hardened", "and the second is the second")

func test_an_unaffixed_act_reports_none() -> void:
	assert_eq(_plain().affix_ids().size(), 0, "no affixes, nothing announced")

func test_the_opening_act_of_the_campaign_is_clean() -> void:
	# The first thing a player meets should be the board, not a modifier on it.
	var levels := Database.load_levels()
	var first := Database.load_engagement(str(levels[0]["map"]), str(levels[0]["engagement"]))
	assert_eq((first.engagement.get("affixes", []) as Array).size(), 0,
		"level 1 teaches the game without a modifier on top")

func test_no_board_opens_an_act_one_with_an_affix() -> void:
	# Same rule per board: act I is where the road, the limit and the drone mix
	# are learned. Every board's own signature starts at act II at the earliest.
	for level in Database.load_levels():
		if not str(level["engagement"]).ends_with("act1"):
			continue
		var db := Database.load_engagement(str(level["map"]), str(level["engagement"]))
		assert_eq((db.engagement.get("affixes", []) as Array).size(), 0,
			"%s opens a board and should carry no affix" % str(level["engagement"]))

# --- and each one has to actually move a number -------------------------------

func test_hardened_puts_armour_on_everything() -> void:
	var plain := _plain()
	var hard := _act_with(["hardened"])
	var walker := plain.enemy_index("walker")
	assert_gt(float(hard.enemy_armour(walker)), float(plain.enemy_armour(walker)),
		"a Sentry Walker wears armour it did not have")

func test_swift_arrives_sooner() -> void:
	var plain := _plain()
	var swift := _act_with(["swift"])
	var walker := plain.enemy_index("walker")
	assert_gt(swift.enemy_speed(walker), plain.enemy_speed(walker), "and moves faster")

func test_resilient_raises_health_without_touching_the_wave_list() -> void:
	var plain := _plain()
	var tough := _act_with(["resilient"])
	plain._begin_wave(0)
	tough._begin_wave(0)
	var walker := plain.enemy_index("walker")
	assert_gt(float(tough.enemy_hp_now(walker)), float(plain.enemy_hp_now(walker)),
		"more health at the same wave")

func test_massed_sends_more_and_pays_less() -> void:
	var plain := _plain()
	var massed := _act_with(["massed"])
	plain._begin_wave(0)
	massed._begin_wave(0)
	var walker := plain.enemy_index("walker")
	assert_lt(float(massed.enemy_bounty_now(walker)), float(plain.enemy_bounty_now(walker)),
		"a kill is worth less")
	var before := 0
	for entry in plain.next_wave_preview():
		before += int((entry as Array)[1])
	var after := 0
	for entry in massed.next_wave_preview():
		after += int((entry as Array)[1])
	assert_gt(float(after), float(before), "and there are more of them")

func test_relentless_shortens_the_gap_between_waves() -> void:
	assert_lt(float(_act_with(["relentless"]).inter_wave_delay()),
		float(_plain().inter_wave_delay()), "less time to spend between waves")

func test_screened_blunts_a_suppressor() -> void:
	var plain := _plain()
	var screened := _act_with(["screened"])
	var walker := plain.enemy_index("walker")
	assert_gt(screened.enemy_slow_resistance(walker), plain.enemy_slow_resistance(walker),
		"slowing fields bite less")

func test_austere_thins_the_bounties() -> void:
	var plain := _plain()
	var austere := _act_with(["austere"])
	plain._begin_wave(0)
	austere._begin_wave(0)
	var walker := plain.enemy_index("walker")
	assert_lt(float(austere.enemy_bounty_now(walker)), float(plain.enemy_bounty_now(walker)),
		"wrecks are worth less")

# --- and the composed behaviour has to hold ----------------------------------

func test_two_affixes_both_apply() -> void:
	var plain := _plain()
	var both := _act_with(["swift", "hardened"])
	var walker := plain.enemy_index("walker")
	assert_gt(both.enemy_speed(walker), plain.enemy_speed(walker), "swift applied")
	assert_gt(float(both.enemy_armour(walker)), float(plain.enemy_armour(walker)),
		"and hardened did too")

func test_the_preview_quotes_what_will_actually_spawn() -> void:
	# The honesty rule, in the one place an affix could break it: a preview that
	# quoted the wave FILE while the director spawned a multiplied count would be
	# a number on screen that the next thirty seconds contradicts.
	var sim := _act_with(["massed"])
	sim._begin_wave(0)
	var promised := 0
	for entry in sim.next_wave_preview():
		promised += int((entry as Array)[1])
	var queued := 0
	for g in sim.group_count():
		queued += sim.group_remaining(g)
	assert_eq(queued, promised, "the preview and the director agree")

func test_an_unknown_affix_is_ignored_rather_than_fatal() -> void:
	# The validator rejects one at load; this is the belt to that pair of braces.
	# Substituting a real affix for a typo'd one would silently change the act.
	var sim := _act_with(["not_an_affix"])
	assert_eq(sim.affix_ids().size(), 0, "nothing was applied")
	assert_eq(sim.enemy_armour(sim.enemy_index("walker")),
		_plain().enemy_armour(_plain().enemy_index("walker")), "and nothing moved")

func test_an_affixed_act_simulates_differently() -> void:
	# Deliberately NOT asserted at tick zero. The state hash covers the state, not
	# the configuration - two acts differing only in an affix start from the same
	# empty board and the same hash, and that is correct. What has to be true is
	# that they come apart the moment anything moves, which is what a desync from
	# a mismatched affix would actually look like.
	var plain := _plain()
	var swift := _act_with(["swift"])
	assert_eq(swift.state_hash(), plain.state_hash(),
		"an empty board is an empty board whatever is coming at it")
	for _i in 200:
		plain.step()
		swift.step()
	assert_ne(swift.state_hash(), plain.state_hash(),
		"and the two run apart as soon as the drones walk")

func test_an_affixed_act_replays_identically() -> void:
	# The whole act, not a prefix: replay() runs to the end of the log, so a
	# bounded record against an unbounded replay compares two different moments -
	# which reads as a desync and is a fixture bug.
	var db := SimFixture.database(BASE_MAP, BASE_ACT)
	db.engagement = db.engagement.duplicate(true)
	db.engagement["affixes"] = ["swift", "hardened"]
	var recorded := Sim.new(db, 4242)
	var log := SimFixture.run_greedy(recorded)
	var replayed := Sim.new(db, 4242)
	SimFixture.replay(replayed, log)
	assert_eq(replayed.state_hash(), recorded.state_hash(), "bit-exact under affixes")
