extends TestCase

## The RNG is the only sanctioned source of randomness in the sim, so its
## reproducibility is load-bearing for replays and the Daily Contract.

func test_same_seed_produces_the_same_stream() -> void:
	var a := Rng.new(2024)
	var b := Rng.new(2024)
	for i in 500:
		assert_eq(b.next_u32(), a.next_u32(), "stream diverged at draw %d" % i)

func test_different_seeds_produce_different_streams() -> void:
	var a := Rng.new(1)
	var b := Rng.new(2)
	var same := 0
	for _i in 200:
		if a.next_u32() == b.next_u32():
			same += 1
	assert_lt(float(same), 5.0, "two seeds should not track each other")

func test_unsigned_shift_does_not_smear_the_sign_bit() -> void:
	# GDScript's >> is arithmetic. If ushr were implemented with it, every state
	# with the high bit set would produce garbage, and the generator would be
	# subtly biased in a way that is very hard to see downstream.
	assert_eq(Rng.ushr(-1, 60), 15, "-1 >>> 60 is 15, not -1")
	assert_eq(Rng.ushr(-1, 63), 1, "-1 >>> 63 is 1")
	assert_eq(Rng.ushr(1 << 63, 63), 1, "the sign bit shifts down to 1")
	assert_eq(Rng.ushr(256, 4), 16, "positive values behave normally")
	assert_eq(Rng.ushr(5, 0), 5, "a zero shift is identity")

func test_values_stay_in_range() -> void:
	var rng := Rng.new(99)
	for _i in 2000:
		var u := rng.next_u32()
		assert_gte(float(u), 0.0, "u32 is non-negative")
		assert_lte(float(u), 4294967295.0, "u32 fits in 32 bits")
	for _i in 2000:
		var f := rng.next_unit()
		assert_gte(f, 0.0, "unit float floor")
		assert_lt(f, 1.0, "unit float is half-open")
	for _i in 2000:
		var n := rng.next_below(7)
		assert_gte(float(n), 0.0, "below() floor")
		assert_lt(float(n), 7.0, "below() ceiling")

func test_distribution_is_not_obviously_broken() -> void:
	var rng := Rng.new(31337)
	var total := 0.0
	var draws := 20000
	for _i in draws:
		total += rng.next_unit()
	var mean := total / float(draws)
	assert_almost_eq(mean, 0.5, 0.02, "mean of a uniform stream should sit near 0.5")

func test_bucket_counts_are_roughly_even() -> void:
	var rng := Rng.new(5150)
	var buckets := PackedInt32Array()
	buckets.resize(8)
	var draws := 16000
	for _i in draws:
		buckets[rng.next_below(8)] += 1
	var expected := float(draws) / 8.0
	for b in 8:
		assert_almost_eq(float(buckets[b]), expected, expected * 0.15, "bucket %d skewed" % b)

func test_draw_count_is_tracked() -> void:
	var rng := Rng.new(7)
	assert_eq(rng.draws(), 0, "a fresh generator has drawn nothing")
	for _i in 10:
		rng.next_u32()
	assert_eq(rng.draws(), 10, "draws are counted for the state hash")

func test_symmetric_range_is_centred_on_zero() -> void:
	var rng := Rng.new(808)
	for _i in 1000:
		var v := rng.next_symmetric(14.0)
		assert_gte(v, -14.0, "symmetric floor")
		assert_lte(v, 14.0, "symmetric ceiling")

func test_zero_or_one_bound_is_safe() -> void:
	var rng := Rng.new(1)
	assert_eq(rng.next_below(1), 0, "a single-option choice needs no randomness")
	assert_eq(rng.next_below(0), 0, "an empty bound must not hang or crash")
