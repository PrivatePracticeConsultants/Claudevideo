# 19 — People and phones

> Onboard a person: `<name>`, devices: `<phone model(s)>`.
>
> - Companion app setup for their phone; create the `person` entity and fuse
>   their trackers per the presence package.
> - Add them as a target in the notification router with their preferences: what
>   priority reaches them, quiet hours, whether critical bypasses DND.
> - Configure actionable notifications relevant to them (e.g., "garage left open
>   — [Close] [Ignore tonight]") with the automation handlers for each action.
> - Be explicit with me about which phone sensors the companion app will report
>   (location, battery, activity) — enable the minimum, and record the choice
>   and its privacy tradeoff in `docs/decisions.md`.
> - Send a test at each priority level to their device and confirm delivery
>   before calling it done.

## The exact edit

`script.notify_person` in `packages/_global.yaml` has a `routes:` table that
**ships empty on purpose** — a guessed notify service silently swallows every
alert given to it. One entry per person:

```yaml
      routes:
        alex:
          push: notify.mobile_app_alex_pixel
          quiet_start: "22:00"
          quiet_end: "07:00"
          critical_bypasses_dnd: true
```

Read the real service name out of **Developer Tools → Actions** after the
companion app is paired. Do not guess it.

## What already handles them

- `binary_sensor.anyone_home` / `everyone_away` iterate **every** `person`
  entity, so a new person is fused with no edit to `packages/presence.yaml`.
- Until a route exists, any `warning`/`critical` addressed to that person
  raises an **"Undeliverable"** persistent notification naming the missing
  route. That is deliberate: a notification nobody can receive is a bug, not a
  silent no-op.

## Test all three before calling it done

```yaml
action: script.notify_person
data: {target: alex, priority: info,     title: Test, message: info level}
# then priority: warning   (should reach the phone, unless in quiet hours)
# then priority: critical  (must bypass DND — verify with the phone silenced)
```

The critical test is the one that matters and the one people skip. A critical
alert that does not actually bypass do-not-disturb is worse than none, because
it is trusted.
