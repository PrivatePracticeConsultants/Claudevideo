extends TestCase

## The P0 acceptance gate's second half: same seed + same input log => identical
## end state.
##
## This is the load-bearing test of the whole project. Replays, seeded Daily
## Contracts and the headless balance sim all reduce to this property, and every
## awkward constraint in core/sim.gd - no Vector2, no pow(), no engine time, no
## Dictionary iteration - exists to keep it true. If this test goes red, nothing
## downstream of it can be trusted.

func test_same_seed_and_log_reach_an_identical_end_state() -> void:
	var recorded := SimFixture.fresh(12345)
	var command_log := SimFixture.run_greedy(recorded)

	var replayed := SimFixture.fresh(12345)
	SimFixture.replay(replayed, command_log)

	assert_eq(replayed.state_hash(), recorded.state_hash(), "full state hash must match after replay")
	assert_eq(replayed.tick(), recorded.tick(), "tick count")
	assert_eq(replayed.result(), recorded.result(), "result")
	assert_eq(replayed.integrity(), recorded.integrity(), "integrity")
	assert_eq(replayed.kills(), recorded.kills(), "kills")
	assert_eq(replayed.capital(), recorded.capital(), "capital")
	assert_eq(replayed.rng_draws(), recorded.rng_draws(), "RNG draws consumed")

func test_state_matches_at_every_checkpoint_not_just_the_end() -> void:
	# Two runs that diverge and then coincidentally re-converge would pass an
	# end-state-only check. Compare all the way along instead.
	var a := SimFixture.fresh(777)
	var b := SimFixture.fresh(777)
	var bp := a.blueprint_index("ballistic")
	var one := SimFixture.a_site(a, 0)
	var two := SimFixture.a_site(a, 8)
	a.queue_place(30, one[0], one[1], bp)
	b.queue_place(30, one[0], one[1], bp)
	a.queue_place(200, two[0], two[1], bp)
	b.queue_place(200, two[0], two[1], bp)
	for tick in 3000:
		a.step()
		b.step()
		if tick % 250 == 0:
			assert_eq(b.state_hash(), a.state_hash(), "diverged at tick %d" % tick)

func test_a_different_seed_produces_a_different_state() -> void:
	# Guards against the hash being accidentally insensitive - if this passed
	# with identical hashes it would mean the seeded RNG never reaches the sim,
	# and the determinism test above would be proving nothing.
	var a := SimFixture.fresh(1)
	var b := SimFixture.fresh(2)
	for _i in 900:
		a.step()
		b.step()
	assert_ne(b.state_hash(), a.state_hash(), "different seeds must diverge")

func test_replaying_a_log_is_not_confused_by_command_ordering() -> void:
	# The replay queues every command up front while the recording appended them
	# one tick at a time. Both must land on the same board.
	var recorded := SimFixture.fresh(4242)
	var command_log := SimFixture.run_greedy(recorded)
	assert_gt(float((command_log["a"] as PackedInt32Array).size()), 0.0, "fixture sanity: some commands were issued")
	var replayed := SimFixture.fresh(4242)
	SimFixture.replay(replayed, command_log)
	assert_eq(replayed.t_count, recorded.t_count, "same number of platforms built")
	assert_eq(replayed.rejected_commands(), recorded.rejected_commands(), "same commands rejected")
	assert_eq(replayed.state_hash(), recorded.state_hash(), "same end state")

func test_hash_is_sensitive_to_a_single_changed_command() -> void:
	var a := SimFixture.fresh(99)
	var b := SimFixture.fresh(99)
	var bp := a.blueprint_index("ballistic")
	var one := SimFixture.a_site(a, 0)
	a.queue_place(10, one[0], one[1], bp)
	b.queue_place(10, one[0] + 4, one[1], bp)  # four units over
	for _i in 1200:
		a.step()
		b.step()
	assert_ne(b.state_hash(), a.state_hash(), "a different placement must produce a different state")
