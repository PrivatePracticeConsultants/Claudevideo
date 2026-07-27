# post-launch.md

Ideas that came up while building, which are **out of scope for the phase that
raised them**. Nothing here is a commitment. Per the plan's scope rule: this
document is where new ideas go so they stop competing with the current phase.

Each entry notes the earliest phase it could sensibly land, so this stays a
backlog rather than a wish list.

---

## Surfaced during P0

**Ballistic lead prediction** *(earliest: P1)*
P0 projectiles home to their target because straight shots miss a moving walker
at these speeds (see DECISIONS P0-6). Real ballistic lead — solving the intercept
quadratic — is more honest once projectile speeds vary by family, and would make
Missile's homing an actual differentiator instead of a shared default.

**Retarget orphaned projectiles** *(earliest: P2, probably never)*
Shots aimed at an enemy that dies mid-flight are currently wasted. Letting them
retarget would reduce overkill. Deliberately not done: overkill is a real cost
that punishes over-committing to one lane, and the balance sim needs to see it.
Would need to become a *module* if it ever ships, not a global rule.

**Wave overlap as a pacing lever** *(earliest: P3)*
Waves currently clear fully before the next begins. Overlapping is the standard
way to tighten late-engagement pacing and is a wave-director change, not a data
change. Worth trying if act three feels slack once the run layer exists.

**Path-adjacency as a placement consideration** *(earliest: P4)*
The pad layout means some pads cover two corridor segments at once. That is
currently an accident of map authoring. It could become an explicit, readable
property — "junction pads" — that map design and Synergy modules both key off.

**Replay files** *(earliest: P5)*
The command log is already a complete replay: it is tick-addressed, hashable, and
`test_determinism` proves it reproduces bit-exactly. Serialising it to disk is
close to free, and it would make bug reports reproducible ("attach your replay")
and give the short-form-video pipeline in §7.2 a way to re-shoot a run at a
different camera or speed.

**Determinism check in CI across platforms** *(earliest: P4)*
`state_hash()` on a fixed seed and log should be identical on Windows, macOS and
Linux. Asserting that in CI is what would actually prove the float64 discipline
holds, rather than trusting the reasoning behind it. Cheap, and it is the only
thing that would catch a libm difference before players do.

**Pad hover should show DPS contribution, not just range** *(earliest: P2)*
Range preview is table stakes (§3.6). Showing what a platform would actually
contribute given current coverage is the thing that would make placement legible
to a new player, and it is the FTUE-shaped version of the same feature.

**`--profile` mode on the capture tool** *(earliest: P6)*
`tools/capture_screenshot.gd` can already drive the game to an arbitrary state
headlessly. Adding frame-time capture over a scripted run would give a
regression-testable performance number per build, which the monthly low-end
hardware check in §8.5 risk 3 needs anyway.
