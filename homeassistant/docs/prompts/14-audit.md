# 14 — Audit (run this monthly)

> Audit the whole system and report, in priority order:
> - Automations that have never fired, or haven't fired in 90 days
> - Entities that are unavailable, orphaned, or duplicated
> - Automations that hardcode entity_ids instead of targeting labels or areas
> - Anything that will break if a specific cloud service goes away
> - Single points of failure, and which ones have no manual fallback
> - Secrets or tokens that appear in committed files
> - Database growth trend and what's driving it
> - Devices in `devices/` with no recorded firmware check in six months
>
> Give me a table with severity and effort. Don't fix anything yet.

## What this repo can answer without the live instance

Run these first; they need no token:

```bash
./scripts/validate.sh                  # secrets scan + placeholder scan + schema

# automations that hardcode entity_ids instead of labels/areas
grep -rnE 'entity_id: (light|switch|binary_sensor|sensor|lock|fan|climate)\.' packages/ \
  | grep -v '{{' | grep -v '#'

# devices whose firmware has not been checked in six months
grep -rn 'firmware_checked' devices/
```

The first grep is the tech-debt check the pack asks for. It should return
**nothing** from `packages/` — every concrete `entity_id:` there is either a
helper this repo declares or a template. Anything else that appears is drift.

## What needs the live instance

- Automations that have never fired → the trace/logbook store.
- Unavailable and orphaned entities → `sensor.unavailable_entities` already
  tracks the live count; orphans (registry entries whose integration is gone)
  need `config/entity_registry/list`.
- Database growth → `sensor.database_size` trend on the Admin dashboard.
- Cloud dependencies → read each integration's config entry.
