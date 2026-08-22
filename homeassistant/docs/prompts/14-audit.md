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
./scripts/validate.sh    # yamllint + secrets + placeholders + schema
./scripts/audit.py       # the structural half of this prompt

# devices whose firmware has not been checked in six months
grep -rn 'firmware_checked' devices/
```

`scripts/audit.py` answers the offline part of this prompt mechanically and
exits non-zero on any finding. It checks:

- **Dangling references** — a helper, script, or alarm panel referenced but
  declared nowhere. This is the failure `check_config` cannot see, because it
  never consults a registry.
- **Hardcoded entity targets** in `packages/` — the tech-debt check the pack
  asks for. Should always be zero; anything appearing is drift away from
  label targeting.
- **Duplicate automation ids and unique_ids** — HA silently keeps one.
- **Helper name collisions between packages** — packages merge, so two files
  declaring the same helper means one silently loses.
- **Kill-switch coverage** — any automation acting on the house without
  checking `automations_paused`, unless it carries a written
  `DELIBERATELY EXEMPT` rationale (the alarm response and the tablet
  battery interlock do).
- **Undocumented `!secret` keys.**

CI runs it on every push, so these stay at zero rather than accumulating.

## What needs the live instance

- Automations that have never fired → the trace/logbook store.
- Unavailable and orphaned entities → `sensor.unavailable_entities` already
  tracks the live count; orphans (registry entries whose integration is gone)
  need `config/entity_registry/list`.
- Database growth → `sensor.database_size` trend on the Admin dashboard.
- Cloud dependencies → read each integration's config entry.
