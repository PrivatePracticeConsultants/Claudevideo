# 15 — Debug prompt (keep handy)

> `<Thing>` isn't working: `<what I expected vs what happened, and when>`.
>
> Investigate before proposing a fix. Pull the automation trace, the relevant
> logbook entries, and the entity's state history around the failure. Check
> whether the entity was available, whether conditions evaluated as you'd
> expect, and whether the automation mode caused it to be skipped.
>
> Tell me the root cause first, in one sentence. Then the fix. Then whether
> anything else in the repo has the same flaw.

## Check these four first — they cause most of it in this repo

1. **Is the label applied?** Every package targets labels. An unlabelled device
   is invisible. `{{ label_entities('security_perimeter') }}` in Developer
   Tools → Template is the fastest check there is.
2. **Is the kill switch on?** `input_boolean.automations_paused` is a condition
   in every automation. If it is `on`, nothing runs and nothing errors.
3. **Is an override timer active?** `timer.<area>_light_override` suppresses
   lighting for that area; `timer.hvac_compressor_lockout` and
   `binary_sensor.hvac_may_start` gate all climate starts. Both are *designed*
   to look like "the automation is broken".
4. **Did `mode:` drop the trigger?** `single` silently discards overlapping
   triggers. The trace says "already running" — that is not an error, it is the
   configured behaviour.

## Then

Developer Tools → Template, paste the automation's condition templates, and read
what they actually render. A template referencing a nonexistent entity renders
`unknown` and fails the condition **without erroring** — that is the single most
common failure mode, and the reason `CLAUDE.md` forbids inventing entity_ids.
