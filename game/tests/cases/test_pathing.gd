extends TestCase

## Movement and leaks. Enemy position is derived from a single scalar (distance
## travelled), so these tests are really about that derivation being right at the
## ends and around corners.

const TOLERANCE := 0.000001

func test_path_start_and_end_land_on_the_first_and_last_waypoint() -> void:
	var sim := SimFixture.fresh()
	sim.sample_for_render(0.0, 0.0)
	assert_almost_eq(sim.out_x(), sim.waypoint_x(0), TOLERANCE, "start x")
	assert_almost_eq(sim.out_y(), sim.waypoint_y(0), TOLERANCE, "start y")
	var last := sim.waypoint_count() - 1
	sim.sample_for_render(sim.path_length(), 0.0)
	assert_almost_eq(sim.out_x(), sim.waypoint_x(last), TOLERANCE, "end x")
	assert_almost_eq(sim.out_y(), sim.waypoint_y(last), TOLERANCE, "end y")

func test_sampling_past_the_ends_clamps() -> void:
	var sim := SimFixture.fresh()
	sim.sample_for_render(-9999.0, 0.0)
	assert_almost_eq(sim.out_x(), sim.waypoint_x(0), TOLERANCE, "clamps to the start")
	sim.sample_for_render(sim.path_length() + 9999.0, 0.0)
	assert_almost_eq(sim.out_x(), sim.waypoint_x(sim.waypoint_count() - 1), TOLERANCE, "clamps to the end")

func test_the_path_is_continuous() -> void:
	# Walking the path in small steps must never jump. A discontinuity here would
	# mean the cumulative-distance table and the waypoint table disagree, which
	# would teleport enemies at corners.
	#
	# A step is not always exactly `stride` long: one that straddles a corner
	# measures the chord rather than the two arms, so it comes up short. This map
	# turns at right angles, where the worst case is a step split evenly across
	# the corner - chord = stride / sqrt(2) ~= 0.707. Anything shorter than that,
	# or anything longer than a stride at all, is a real defect.
	var sim := SimFixture.fresh()
	var steps := 2000
	var stride := sim.path_length() / float(steps)
	var worst_corner := stride * 0.7071
	sim.sample_for_render(0.0, 0.0)
	var prev_x := sim.out_x()
	var prev_y := sim.out_y()
	var shortest := stride
	for i in range(1, steps + 1):
		sim.sample_for_render(float(i) * stride, 0.0)
		var dx := sim.out_x() - prev_x
		var dy := sim.out_y() - prev_y
		var moved := sqrt(dx * dx + dy * dy)
		assert_lte(moved, stride * 1.000001, "jumped forward at step %d" % i)
		assert_gte(moved, worst_corner - stride * 0.000001, "jumped or stalled at step %d" % i)
		shortest = minf(shortest, moved)
		prev_x = sim.out_x()
		prev_y = sim.out_y()
	# Sanity on the test itself: this map has corners, so some step must have
	# been shortened. If none was, the loop was not actually exercising them.
	assert_lt(shortest, stride * 0.999, "fixture sanity: the path should turn corners")

func test_every_waypoint_is_reachable_at_its_own_cumulative_distance() -> void:
	# Directly checks that the cumulative-distance table agrees with the waypoint
	# table - the failure that would show up in play as enemies cutting corners.
	var sim := SimFixture.fresh()
	for i in sim.waypoint_count():
		sim.sample_for_render(sim.segment_start_distance(i), 0.0)
		assert_almost_eq(sim.out_x(), sim.waypoint_x(i), TOLERANCE, "waypoint %d x" % i)
		assert_almost_eq(sim.out_y(), sim.waypoint_y(i), TOLERANCE, "waypoint %d y" % i)

func test_lateral_offset_is_perpendicular_to_travel() -> void:
	var sim := SimFixture.fresh()
	var probe := sim.path_length() * 0.5
	sim.sample_for_render(probe, 0.0)
	var cx := sim.out_x()
	var cy := sim.out_y()
	sim.sample_for_render(probe, 20.0)
	var ox := sim.out_x()
	var oy := sim.out_y()
	var dx := ox - cx
	var dy := oy - cy
	assert_almost_eq(sqrt(dx * dx + dy * dy), 20.0, 0.0001, "offset moves exactly its own magnitude")

func test_an_enemy_takes_the_expected_number_of_ticks_to_cross() -> void:
	var db := SimFixture.database()
	var sim := Sim.new(db, 4)
	sim._begin_wave(0)
	sim._spawn(0)
	var speed_per_tick := float((db.enemies["walker"] as Dictionary)["speed_units_per_second"]) / float(sim.tick_rate())
	var expected := int(ceil(sim.path_length() / speed_per_tick))
	var ticks := 0
	while sim.e_live_count > 0 and ticks < 10000:
		sim._advance_enemies()
		ticks += 1
	assert_almost_eq(float(ticks), float(expected), 1.0, "crossing time follows from speed and path length")

func test_a_leak_costs_exactly_its_leak_value() -> void:
	var db := SimFixture.database()
	var leak_value := int((db.enemies["walker"] as Dictionary)["leak_value"])
	var starting := int(db.economy["starting_integrity"])
	var sim := Sim.new(db, 4)
	sim._begin_wave(0)
	sim._spawn(0)
	while sim.e_live_count > 0 and sim.tick() < 10000:
		sim._advance_enemies()
		sim._tick += 1
	assert_eq(sim.integrity(), starting - leak_value, "one leak costs one leak value")
	assert_eq(sim.leaks(), 1, "and is counted as one leak")
	assert_eq(sim.kills(), 0, "a leak is not a kill")

func test_integrity_never_goes_negative_in_the_reported_result() -> void:
	var sim := SimFixture.fresh()
	SimFixture.run_idle(sim)
	assert_eq(sim.integrity(), 0, "a failed run reports zero, not a negative number")
