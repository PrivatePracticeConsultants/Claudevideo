# Decisions

Why things are the way they are. Read this before "fixing" something that looks
wrong — several things here look wrong and are deliberate.

---

## Targeting: labels and areas, never entity lists

Every automation targets a **label** or an **area**, and every template resolves
through `label_entities()` / `area_entities()` against the live registry at
runtime.

Two reasons. The stated one: adding a device should require no automation edits.
The one that mattered while this was written: **this repo was built without
access to a live instance**, and `CLAUDE.md` forbids inventing entity_ids. A
guessed `sensor.kitchen_temperature` does not error — it renders `unknown`
forever and the automation silently never fires. Registry-driven targeting is
the only way to write correct automations for a house you have not seen, and it
happens to be the better architecture anyway.

**Consequence you must know about:** the packages do nothing until the labels
exist. `sensor.perimeter_open_count` reads 0 on a house with no
`security_perimeter` label — not because nothing is open, but because nothing is
labelled. The canonical label list is in `CLAUDE.md`; applying them is
`docs/prompts/02-taxonomy.md`.

---

## Recorder exclusions — the history being given up

Each of these is a deliberate trade. Reverse any of them if you need it.

| Excluded | What you lose | Why it goes |
|---|---|---|
| `automation` domain | The on/off state of automations over time | Fires are already in the logbook and in traces; the state itself is near-constant and writes a row per reload |
| `update` domain | History of which versions were available when | One row per integration per poll, forever, and it is never once useful |
| `device_tracker` | Raw GPS/router presence history | The highest-churn data in most databases. Presence *decisions* are kept — `binary_sensor.anyone_home` and the `person` entities are still recorded |
| `*_linkquality`, `*_rssi`, `*_wifi_signal` | Radio-strength trends | Moves constantly. Genuinely useful for mesh debugging — **re-enable temporarily** when doing `docs/prompts/18-radio.md`, then exclude again |
| `*_uptime`, `*_last_seen` | Per-device uptime history | Changes every single poll by definition |
| `sensor.time_*`, `sensor.date_*` | Nothing | These change by design every minute |
| `call_service` events | Which service calls were made | Usually the single largest table in an HA database. Traces cover the automation-triggered ones, which are the ones anyone ever wants |

`purge_keep_days: 14` with long-term statistics left on: numeric sensors with a
`state_class` keep hourly min/mean/max **forever** regardless of this setting.
So a year of energy and temperature history survives; a year of every
individual state change does not. That is the intended shape.

---

## Climate: sensors that are missing

The climate package degrades rather than breaks without these. What each one
costs:

| Missing | Consequence |
|---|---|
| Per-zone humidity | Comfort index falls back toward dry-bulb; dehumidification cannot target a zone |
| Window/door contacts labelled `security_perimeter` | The open-window pause cannot work at all — it has no input |
| Ceiling fans exposed as `fan.*` and labelled `comfort_fan` | Fan-before-compressor is skipped entirely; the compressor runs for deltas a fan would fix |
| Outdoor temperature | No "it is cooler outside, open a window" logic |
| Per-zone occupancy | Falls back to `house_mode` only, so unoccupied rooms are conditioned |

**Short-cycle protection is a hard requirement, not a tunable.** A compressor
restarted against unequalised head pressure is a compressor being destroyed. The
5-minute minimum off-time is the industry floor. `hvac_min_offtime` can be
raised, never lowered below 3, and the gate itself
(`binary_sensor.hvac_may_start`) must not be bypassed.

---

## Pet immunity

A dog triggers PIR and mmWave identically to a person. Three layers:

1. **Label** — every sensor within the dog's reach gets `pet_exposed`.
2. **Corroboration** — a `pet_exposed` sensor alone never establishes
   occupancy. It needs a second sensor, a door contact, or device activity.
3. **Intrusion isolation** — `binary_sensor.trustworthy_intrusion_signal` (what
   the alarm consults) ignores pet-exposed motion entirely while the dog is
   home. A perimeter opening is always trusted: a dog does not open a door.

`input_boolean.pet_at_home` relaxes all of this when the dog is away, so the
system is not permanently degraded by a dog that is not there.

Placement guidance is in `docs/runbook.md` §8. The short version: mount PIRs
above 1.2m aimed slightly up, fit the pet-immune lens mask, and turn mmWave
sensitivity down — mmWave sees a dog extremely well.

---

## Manual override detection is partial, and here is exactly how

`automation.lighting_manual_override_detect` catches a human changing a light
from the HA UI, the companion app, or voice, because those carry a `user_id` in
the event context.

It does **not** catch:

- **A dumb wall switch that cuts power.** Invisible to HA by definition. This is
  also why no smart bulb goes behind one (`CLAUDE.md`, physical-first rule).
- **A smart switch or Zigbee wall remote**, *unless* it exposes an event entity
  or device trigger. Those are model-specific, so they are wired per device in
  `devices/<slug>.yaml` rather than guessed centrally.

Stated plainly because the failure is confusing: the lights fight you, and the
override that was supposed to stop that never armed.

---

## Alarm: built-in `manual` rather than Alarmo

Alarmo (HACS) has a better UI and real per-sensor bypass. The built-in `manual`
platform was chosen because it is built in — no HACS dependency, no upgrade
risk, and it survives a HACS outage. Every automation targets
`alarm_control_panel.house`, so switching to Alarmo later is a change to one
file.

Bypass is handled honestly instead: `script.arm_house` arms anyway but
**reports** what it bypassed. Silently arming around an open patio door is how
people believe a house is secure when it is not.

---

## Why `optional/` exists

`check_config` fails hard on integrations that are not installed. Anything
needing HACS — Frigate, Alarmo, auto-entities, kiosk-mode, browser_mod — lives
in `optional/` and is **not** auto-included. Move a file into `packages/` once
the component is actually installed.

Without this split, the validation gate would be red permanently on any machine
that has not yet installed every custom component, and a permanently red gate is
a gate everyone ignores.

---

## HA version is pinned in `.ha-version`

CI and `scripts/validate.sh` both read it. Validating against a version other
than production's is worse than not validating: it accepts syntax production has
already removed, and rejects syntax production requires.

This bit during the build. Python 3.11 caps pip at Home Assistant **2024.3.3**,
which predates the `triggers:`/`conditions:`/`actions:` key rename and the
`service:` → `action:` change. Everything in this repo uses the modern keys and
would have "failed" against that validator for no reason. Python 3.13 was used
instead, which resolves **2026.2.3**.

---

## Helpers: no `initial:` — restore must win

No `input_number`/`input_select` here sets `initial:`. HA's own source returns
early from restore when a seeded value exists, so `initial:` resets the helper
on EVERY restart: `house_mode` would snap back to `home` while the household is
away, and every slider tuned in the UI would silently revert to defaults after
an update. First boot still defaults sanely (`input_select` falls back to its
first option; templates carry `| int(...)` / `| float(...)` fallbacks).
`scripts/audit.py` does not police this — the rule lives here and in review.

Two adjacent rules found the hard way, both now enforced by tooling:

- **Package filenames must be valid HA slugs.** `_global.yaml` fails
  `cv.slug` (leading underscore) and the package is *silently skipped* — the
  whole foundation never loaded, and check_config reports it only under the
  `Incorrect config` banner, which the old gate did not grep for. Both fixed;
  `scripts/audit.py` now checks slugs and `scripts/validate.sh` greps both
  banners.
- **A dynamically-dispatched filter name — `map('x')`, `select('x')` — is not
  checked at compile time.** `map('extract')` (an Ansible filter that does not
  exist in Jinja or HA) compiled cleanly and would have crashed the
  notification router on its first real push. `scripts/check-templates.py`
  compiles all templates with HA's real environment AND verifies every
  dispatched name against its filter/test registries.

---

## The UI is core cards only, on purpose

No HACS. No mushroom-cards, no button-card, no card-mod, no auto-entities. The
showcase dashboards people post use those, and they do look better than stock.

The trade is upgrade risk against polish. A frontend card is a Lovelace
*resource*, not an integration, so it never fails `check_config` — it fails
silently in the browser after an HA update, usually as a blank card, and
`docs/prompts/16-upgrade.md` exists precisely because that keeps happening. The
non-technical single user this is built for cannot debug a blank dashboard.

Home Assistant's own cards closed most of that gap: `sections` view layout,
`heading` cards, and `tile` cards with real inline features (alarm mode buttons,
select dropdowns, ± setpoints, toggles, trend graphs) are all core now. That is
what `dashboards/` uses, verified card-by-card against the shipped frontend by
`scripts/check-dashboards.py`.

If you want the extra polish later, add the HACS cards to `optional/` first and
accept that each one is a thing to re-check on every upgrade.

## Weather binds itself; the integration is added in the UI

Of the 28 weather integrations shipping in 2026.2.3, `template` is the ONLY one
with a YAML platform schema — every other one is config-flow. So the repo cannot
install weather. Add **Met.no** in the UI (free, no API key, uses the lat/long
already in `secrets.yaml`).

`packages/weather.yaml` then binds itself: it resolves the first `weather.*`
entity in the registry at runtime rather than naming one, so it is correct
whichever provider you pick and inert (not broken) before you pick any. Forecast
figures come from the `weather.get_forecasts` **service** via a trigger-based
template entity — forecast attributes were removed in 2024.4 and there is no
longer any way to read them declaratively.

## First-boot defaults: why `initial:` is banned and bootstrap.yaml exists

See `packages/bootstrap.yaml` for the full reasoning. Short version, both halves
verified on a live instance:

- `initial:` **defeats state restore on every restart** — HA returns early from
  `async_added_to_hass` when a value is already seeded. `house_mode` would snap
  back to `home` mid-vacation and every tuned slider would revert after an
  update.
- With no `initial:`, a fresh instance leaves every `input_number` at its `min`
  — observed: `comfort_target` 60°F, every tariff rate $0.000,
  `low_battery_threshold` 5%. Silently, confidently wrong.

So: no `initial:` anywhere, and a one-shot bootstrap automation guarded by a
boolean that itself restores.

## What has NOT been verified

Honesty about the limits of the validation, because "it validates" is easy to
over-read.

`scripts/validate.sh` proves the config is **schema-valid** and free of
committed secrets. It does **not** prove:

- **That any entity_id exists.** `check_config` does not consult a registry.
  This is precisely why targeting is label-driven.
- ~~That the notification router works~~ **Now executed, not just validated**:
  `script.notify_person` has been invoked live at every priority — info creates
  a persistent notification, an unrouted warning/critical surfaces an
  Undeliverable notice, a bad priority is flagged — and the daily security
  check ran the full automation → router → notification chain ("House check:
  Secure"). This execution round found and fixed two script-variable landmines
  (see CLAUDE.md, "Script variables") that every static check had passed.
- ~~That apply-taxonomy.py's websocket half works~~ **Now executed**: `dump`
  ran against the live registry — auth, all three registry queries, ~200
  entities and the instance's areas in the emitted plan.
- **That any automation does the right thing** on real hardware. A live instance
  HAS now been booted (HA 2026.2.3, onboarded, dashboards rendered and
  screenshotted) and reaches **zero configuration errors and zero template
  loops** — but with no paired devices, so no automation has ever fired against
  a real sensor. (What IS now verified beyond schema:
  all 231 templates compile in HA 2026.2.3's real Jinja environment with every
  dynamically-dispatched filter/test name checked against its registries; the
  blueprint instantiates cleanly through check_config; the ESPHome template
  node validates end-to-end with esphome 2026.8.0; the notification-route,
  battery-threshold and wettest-zone templates render correctly on test data;
  the five shell scripts are shellcheck-clean.)
- **`sensor.house_power_baseline`.** The hour-of-week EWMA needs roughly a week
  of real data before its output means anything, and its alerts should not be
  trusted or acted on until then. The mechanism (trigger-based template entity
  restoring attributes across restarts) is sound but unexercised.
- **The vampire-load minimum tracker.** Same: needs a full week, and one reset
  cycle, to be meaningful.
- **`command_line` sensors.** `sensor.database_size`, `sensor.internet_latency`,
  `sensor.dns_resolution` and `sensor.deployed_commit` all shell out. The paths
  (`/config/home-assistant_v2.db`, `git -C /config`) are the standard container
  layout and will need checking on a supervised or core install.

Everything in this list needs one pass on a live instance. Do that before
relying on any number it produces.

---

## To be recorded once the live instance exists

These are required by the prompt pack and cannot be filled in from here:

- **Assist exposure list** and the privacy trade-off of each cloud fallback
  (prompt 11).
- **Every long-lived token**: purpose, creation date, rotation schedule —
  including the one Claude Code uses (prompt 17).
- **Companion-app sensors enabled per phone**, and why each one is on
  (prompt 19).
- **The Energy dashboard's circuit → device-class mapping**, since that
  configuration lives in `.storage` and cannot be versioned (prompt 09).
- **Zigbee channel chosen and the Wi-Fi channels locked around it** (prompt 18).
