# Home Assistant — config as code

A config-as-code Home Assistant repository built from the Claude Code × Home
Assistant prompt pack. The git repo is the source of truth; the UI is for
pairing radios and reading logs.

**Start here:** `CLAUDE.md` for the conventions, `docs/decisions.md` for why
things are the way they are, `docs/runbook.md` when something is broken.

## Status: what is built, and what needs the house

This repo was built **without access to a live Home Assistant instance**. That
shaped everything, and it is the first thing to understand before using it.

Prompt 00 (wire Claude Code to the instance) is the pack's stated prerequisite
for everything else, and only one of its three legs could be satisfied here:

| Leg | State |
|---|---|
| Filesystem access to `/config` | ✗ no instance exists in this environment |
| Live introspection (token, `hass-cli`/MCP) | ✗ same |
| **Validation** (`check_config`) | ✓ **built and passing** — HA 2026.2.3 |

| Prompt | State | Where |
|---|---|---|
| 00 connect | ◐ validation leg only | `scripts/validate.sh`, `docs/prompts/00-connect.md` |
| 01 bootstrap + CLAUDE.md | ✓ built | `CLAUDE.md`, `configuration.yaml` |
| 02 taxonomy | ◐ tooling built, plan needs the registry | `scripts/apply-taxonomy.py` |
| 03 global scaffolding | ✓ built | `packages/global.yaml` |
| 04 add a device | ○ reusable prompt | `docs/prompts/04-add-device.md` |
| 05 lighting | ✓ built | `packages/lighting.yaml` + blueprint |
| 06 climate | ✓ built | `packages/climate.yaml` |
| 07 presence | ✓ built | `packages/presence.yaml` |
| 08 security | ✓ built (cameras → `optional/`) | `packages/security.yaml` |
| 09 energy | ✓ built (dashboard itself is UI-only) | `packages/energy.yaml` |
| 10 dashboards | ✓ built | `dashboards/` |
| 11 voice | ○ needs hardware | `docs/prompts/11-voice.md` |
| 12 ESPHome | ✓ built | `esphome/common/base.yaml` |
| 13 backups + runbook | ✓ built | `scripts/backup-verify.sh`, `docs/runbook.md` |
| 14 audit | ○ monthly prompt | `docs/prompts/14-audit.md` |
| 15 debug | ○ keep handy | `docs/prompts/15-debug.md` |
| 16 upgrades | ○ per upgrade | `docs/prompts/16-upgrade.md` |
| 17 hardening | ○ needs the instance | `docs/prompts/17-hardening.md` |
| 18 radio planning | ○ **run before pairing anything** | `docs/prompts/18-radio.md` |
| 19 people + phones | ○ per person | `docs/prompts/19-people.md` |
| 20 CI + safe deploys | ✓ built | `.github/workflows/`, `scripts/deploy.sh` |
| 21 wall tablets | ◐ theme + charge automation built | `optional/tablets.yaml` |

✓ built · ◐ partly built · ○ prompt template only

## The design decision everything rests on

`CLAUDE.md` forbids inventing entity_ids, and there was no registry to check
against. So **every automation targets a label or an area**, and every template
resolves through `label_entities()` / `area_entities()` at runtime.

That is the pack's own "labels over hardcoding" rule, and it is also the only
way to write correct automations for a house you have not seen. **There is no
fabricated entity_id anywhere in `packages/`.**

The trade-off is real and you must know it: **the packages do nothing until the
labels exist.** `sensor.perimeter_open_count` reads 0 on an unlabelled house —
not because nothing is open, but because nothing is labelled. The label table is
in `docs/prompts/02-taxonomy.md`.

## Use it

```bash
cp secrets.yaml.example secrets.yaml     # then fill it in
./scripts/install-hooks.sh               # pre-commit lint + secrets scan

# validate against the pinned HA version
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python "homeassistant==$(cat .ha-version)"
./scripts/validate.sh
```

Then, in order: `docs/prompts/18-radio.md` (**before pairing anything**),
`00-connect.md`, `02-taxonomy.md`, and `04-add-device.md` per device.

## Validation

`scripts/validate.sh` runs four checks — yamllint, a secrets scan, a
placeholder scan, and `check_config` against the version pinned in
`.ha-version`. CI runs the same script, building `secrets.yaml` from the
committed example (which proves the example stays complete).

**What that does not prove**, spelled out in `docs/decisions.md`: `check_config`
validates schema, not logic, and it does **not** verify that any entity_id
exists. No automation in this repo has ever run. Treat everything here as
needing one pass on a live instance — particularly the energy baseline, which
needs about a week of real data before its alerts mean anything.
