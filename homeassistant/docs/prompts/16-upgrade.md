# 16 — Upgrades (run before every HA update)

> I'm on Home Assistant `<current version>` and want to upgrade to `<target>`.
>
> 1. Read the release notes and breaking-changes sections for every version
>    between current and target — not just the target.
> 2. Grep this repo for every integration, config key, and template syntax the
>    breaking changes touch, and list exactly what will break, file by file.
> 3. Check every HACS custom component and frontend card for compatibility with
>    the target version. Flag anything unmaintained as replace-or-accept-risk.
> 4. Confirm a verified backup exists from the last 24 hours
>    (`./scripts/backup-verify.sh`).
> 5. Give me a go / no-go with the required repo changes as a branch, so I can
>    merge them atomically with the upgrade.
>
> After I upgrade: scan the logs for deprecation warnings and fix them in the
> repo now, while they're warnings, not next month when they're errors.

## The mechanical part of this repo

Upgrading means changing **one file**, `.ha-version`, and letting CI tell you:

```bash
echo "2026.6.1" > .ha-version
# locally, against the new version:
uv venv --python 3.13 /tmp/ha-next
uv pip install --python /tmp/ha-next/bin/python "homeassistant==$(cat .ha-version)"
HA_PYTHON=/tmp/ha-next/bin/python ./scripts/validate.sh
```

Green means the *schema* survives the upgrade. It does **not** mean behaviour
survived — `check_config` does not run automations. The staging instance from
`00-connect.md` is what catches the rest.

## Known exposure in this repo

Things most likely to be hit by a breaking change, worth grepping first:

- `command_line` sensors (`global.yaml`, `network.yaml`, `deploy.yaml`) —
  this platform has been reworked once already.
- The `manual` alarm panel (`security.yaml`) — a YAML platform in a
  config-flow-first world.
- Trigger-based template entities using `this.attributes` (`energy.yaml`) —
  depends on state restoration semantics.
- `label_entities()` / `area_entities()` — used everywhere; a signature change
  would be systemic.
- `homeassistant.turn_on` / `turn_off` targeting labels (`climate.yaml`).
