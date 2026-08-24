# CLAUDE.md — Home Assistant configuration repository

Conventions for any session (human or AI) that edits this repo. Read before
changing anything. `docs/decisions.md` records *why*; this file records *how*.

## The one rule that shapes everything else

**The git repo is the source of truth, not the HA UI.** Claude edits YAML here,
validates it, and it gets deployed by `scripts/deploy.sh`. The UI is for pairing
radios and reading logs. If you change something in the UI, it is not real until
it is in this repo.

## Registry-driven targeting — never invent an entity_id

This is the hard rule and the most common way an AI breaks this repo.

- **Never write an entity_id you have not confirmed exists** in the live entity
  registry. A plausible-looking `sensor.kitchen_temperature` that does not exist
  fails silently: templates render `unknown`, automations never trigger, and
  nothing errors.
- **Automations target labels and areas, not entity lists.** Use
  `target: {label_id: exterior_light}` or `target: {area_id: kitchen}`.
- **Templates resolve through the registry at runtime** using `label_entities()`,
  `area_entities()`, `device_entities()`, and `expand()`. These are evaluated
  against the live registry when the template runs, so they are correct without
  any entity_id being hardcoded here.
- Consequence: **adding a device requires zero automation edits.** You label the
  device; the automations already query that label. If you find yourself editing
  an automation because hardware arrived, the automation is wrong — fix it to
  target a label instead. That is tech debt, and `docs/prompts/14-audit.md`
  hunts for it.
- Site-specific *scalars* (tariff rates, thresholds, delays) are `input_number` /
  `input_select` helpers with sane defaults, tuned in the UI — not magic numbers
  buried in templates.

## Naming

- Entity IDs: `<domain>.<area>_<device>_<function>` —
  `light.kitchen_ceiling_main`, `binary_sensor.garage_door_contact`.
- Areas match **physical rooms**, named as the household says them out loud.
- Friendly names are human-readable and **never encode the area** — HA prepends
  it. `"Ceiling"` in area Kitchen displays as "Kitchen Ceiling". Writing
  `"Kitchen Ceiling"` gets you "Kitchen Kitchen Ceiling".
- Labels are the query axis. The canonical set:
  `security_perimeter`, `exterior_light`, `sleep_sensitive`, `high_draw`,
  `battery_powered`, `critical_uptime`, `guest_visible`, `pet_exposed`.

## Every automation gets

- A stable `id` that never changes (traces and the UI key off it). Renaming an
  `id` orphans its history.
- A `description` explaining **intent** — why this exists, not what it does.
  "Turn on lights" is useless; "so the hall is lit before the stairs are reached"
  is the reason it is written the way it is.
- `mode:` set **deliberately** — `single` (default, drops overlapping triggers),
  `restart` (last trigger wins — right for anything with a delay), `queued`, or
  `parallel`. Never leave it to default by accident; state it.
- The global kill switch in its conditions (see below).
- A comment on any non-obvious logic explaining what a trace would show.

## Script variables: compute in an action step, never at script level

Two Home Assistant behaviors, both found by executing scripts on a live
instance after every static check had passed:

- Script-level `variables:` render as *defaults* whose templates **cannot see
  sibling keys** — a computed variable referencing another dies with
  `UndefinedError` on every run.
- A script-level variable holding an **empty mapping is silently dropped when
  the script declares `fields:`** (`routes: {}` vanished while its sibling
  string survived, killing every notification in the house).

So: static AND computed variables both live in a `- variables:` action step at
the top of `sequence:`, which renders sequentially and has neither problem.

## Trigger-based template entities: two runtime rules

Both learned by watching entities fail on a live instance, both invisible to
every static check:

- **`this` is undefined inside the `actions:` block.** It is available in the
  `state:` and attribute templates, but a variables step that reads
  `this.attributes` raises `UndefinedError` on every event. Read the entity's
  own prior value with `state_attr('sensor.x', 'attr')` instead.
- **A condition that aborts the action sequence still renders the entity.**
  The variables the actions would have set are then *undefined*, so any
  accumulator that writes them straight into an attribute silently WIPES
  itself — once per event, which on a `state_changed` trigger is hundreds of
  times a minute. Every state/attribute template must fall back:
  `{{ x if x is defined else (this.attributes.get('x') or default) }}`.

## No orphan helpers

Every `input_boolean`, `input_number`, `timer`, etc. is declared **in YAML, in
the package that uses it**. A helper created in the UI is invisible to this repo
and will be silently destroyed by a restore. If a helper is used by two
packages, it belongs in `global.yaml`.

## Physical-first rule

**Every automated function must still work with the network down, HA down, or
the owner on a plane.**

- Never install a smart bulb behind a switch that can cut its power. If the
  switch is a normal switch, the bulb is dumb and the *switch* is smart.
- Every lock, every light, every door has a manual path that does not involve a
  phone. `docs/runbook.md` documents each one.
- When a device is added, `devices/<slug>.yaml` records its **failure mode**:
  what breaks when it dies, and what the human does instead.

## Secrets

- Nothing but `!secret` references in committed files. `secrets.yaml` is
  gitignored; `secrets.yaml.example` documents every key with a dummy value.
- No tokens, no API keys, no coordinates, no external hostnames in committed
  YAML. The pre-commit hook scans for these and blocks the commit.

## Before any change

1. Read the current file — do not write from memory.
2. Check the **live entity registry** to confirm every entity_id referenced
   actually exists. If there is no live instance available in the session, say
   so and use label/area targeting instead of guessing.
3. Confirm which package owns the concern. One concern, one package.

## After any change

1. Run `scripts/validate.sh` (yamllint + `check_config` against the pinned HA
   version) and paste the output.
2. Report the diff.
3. State explicitly whether this needs a **reload** or a **restart**:
   - *Reload* — automations, scripts, scenes, templates, most helpers.
   - *Restart* — `configuration.yaml` itself, `recorder:`, new integrations,
     anything under `homeassistant:`.
4. If the change touches anything in the physical-first list, say what the
   manual fallback is.

## Layout

```
configuration.yaml   includes only — no logic ever lives here
packages/            one file per functional domain, auto-included
devices/             one file per physical device (see docs/prompts/04-add-device.md)
blueprints/          reusable automation logic; per-area instantiation
dashboards/          YAML-mode dashboards, versioned
esphome/             common/ shared package + per-node files
optional/            config requiring HACS/custom components — NOT auto-included
docs/                inventory, runbook, decisions, reusable prompts
scripts/             validate, deploy, backup-verify, taxonomy
```

`optional/` exists because `check_config` fails on integrations that are not
installed. Anything depending on HACS (Frigate, Alarmo, auto-entities,
kiosk-mode, browser_mod) lives there with install instructions, and is moved
into `packages/` by the user once the component is actually installed.

## Session habits

- **One concern per session.** A sprawling diff is unreviewable.
- **Commit before every prompt**, so `git diff` is the review surface.
- **`check_config` is not enough.** It catches schema, not logic, and it does
  **not** verify that an entity_id exists. Reload, trigger manually, read the
  trace.
- **Write the runbook as you go.** A system that cannot be handed to someone
  else during an emergency is a liability, not an amenity.

## Runtime facts that only execution revealed

Each of these cost a real bug. They are not inferable from the schema, and
`check_config` accepts every one of the broken versions.

- **An unset `input_datetime` renders `00:00:00`, not `unknown`.** So a guard
  written as `states(...) not in ['unknown','unavailable','']` passes on a
  helper nobody has ever set. This armed a monitored alarm panel in testing.
  Anything gating on "has the user configured this?" must treat `00:00:00` as
  unset, and accept that exactly-midnight becomes unexpressible.
- **A one-shot bootstrap can only ever seed a fresh install.** Defaults appended
  to a first-run block never reach an instance that already ran it — which is
  every instance being upgraded. Use the migration ladder in
  `packages/bootstrap.yaml`: add a new versioned step, never edit an old one.
- **`history_stats` does not count time it has no records for.** Hours when HA
  was stopped are not "not degraded", they are unmeasured — so dividing by a
  fixed window turns downtime into apparent health. Divide by a measured
  coverage sensor and go unavailable when coverage is thin.
- **A `rejectattr` self-exclusion stops the value oscillating, not the log
  line.** A template iterating all states still subscribes to itself, so HA may
  still log "Template loop detected ... skipping template render". The
  exclusion is still required; just do not read its absence as proof of a loop,
  or its presence as proof of none.
- **A registry label can be non-empty and still resolve to nothing.** Orphaned
  entries keep a label alive after the entity is gone. Guard on "how many
  entities can this actually read", not on `label_entities(x) | count > 0`.
- **`automation.trigger` skips conditions** unless you pass
  `skip_condition: false`. A test without it proves the actions run, not that
  the guards hold.
