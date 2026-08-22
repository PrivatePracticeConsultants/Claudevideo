# 02 — Establish the entity taxonomy

**Tooling for this is built:** `scripts/apply-taxonomy.py` (dry-run, rollback
file, and a repo grep report). Its safety behaviour is tested; its websocket
half needs a live instance.

```
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=...

./scripts/apply-taxonomy.py dump > plan.yaml     # read the live registries
$EDITOR plan.yaml                                # decide the renames
./scripts/apply-taxonomy.py apply plan.yaml --dry-run
./scripts/apply-taxonomy.py apply plan.yaml      # writes rollback-<ts>.yaml
```

It **refuses** to rename an entity that is referenced in committed YAML until
the repo is updated (override with `--force` once it is). That refusal is the
whole point: renames break references silently.

## The prompt

> Pull the full entity registry, device registry, and area registry from the
> live instance. Produce a table of every entity: current entity_id, friendly
> name, area, device, integration, and whether it's disabled or hidden.
>
> Then propose a normalized naming scheme following CLAUDE.md and give me:
> 1. A rename plan as a table (old → new), grouped by area, with anything
>    ambiguous flagged for me to decide.
> 2. The set of areas, floors, and labels I should create.
> 3. The plan.yaml to feed `scripts/apply-taxonomy.py`.
>
> Do not execute anything. Renames break references — include the grep report of
> every place each old entity_id appears in the repo.

## Labels this repo already depends on

These are not suggestions — packages already query them, and are inert until
they exist:

| Label | Used by |
|---|---|
| `security_perimeter` | security (perimeter count, alarm), climate (window-open pause) |
| `exterior_light` | security (alarm response) |
| `entry_light` | presence (arrival) |
| `adaptive` | lighting (adaptive refresh) |
| `comfort_fan` | climate (fan-before-compressor) |
| `dehumidifier` | climate (RH band) |
| `monitored_circuit` | energy (draw, baseline, vampire load) |
| `battery_powered` | health (low-battery digest) |
| `critical_uptime` | network (offline alert) |
| `pet_exposed` | presence (**every** pet-immunity rule) |
| `auto_lock` | security (auto-lock) |
| `announce` | notification router (critical TTS) |
| `sim_morning` / `sim_evening` / `sim_night` | security (vacation simulation) |

Plus the axes from the pack not yet consumed by a package, worth creating now:
`sleep_sensitive`, `high_draw`, `guest_visible`.
