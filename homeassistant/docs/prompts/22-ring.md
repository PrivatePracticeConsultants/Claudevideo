# 22 — Ring Alarm install runbook

The decisions behind this (iOS, containers, panel topology **(a)**) are settled
and recorded in `docs/decisions.md`. Do not re-litigate them here.

**What this integration is:** observability and convenience over an alarm that
works without it. Ring's base station arms, sirens and dispatches to the
central station on its own path. **Home Assistant is never in the dispatch
chain.** If every container below is stopped, the house is still armed and
still monitored.

---

## Prerequisites

- [ ] **Phase 0 passed** — the critical notification path is proven audible in
      Do Not Disturb (`docs/runbook.md` §12). Nothing here matters if alerts
      cannot reach a phone.
- [ ] A Docker host that stays up, reachable by Home Assistant.
- [ ] Ring account credentials + the 2FA device.

---

## Part A — the code steps

### 1. Broker credentials

Pick a long random password. It goes in **three** places that must match:

```bash
cd <repo>/homeassistant

# 1. the compose .env
cp .env.example .env && $EDITOR .env

# 2. the Mosquitto password file (hashed, gitignored, never committed)
docker run --rm -v "$PWD/mosquitto/config:/mosquitto/config" \
  eclipse-mosquitto:2.0.22 \
  mosquitto_passwd -c -b /mosquitto/config/passwd hass '<the-password>'

# 3. secrets.yaml (HA's side)
$EDITOR secrets.yaml   # mqtt_username / mqtt_password
```

### 2. Acquire the Ring refresh token

Interactive, one time. The token is written into the state volume — **it never
enters this repo**:

```bash
docker run -it --rm -v ring_mqtt_data:/data \
  --entrypoint /app/ring-mqtt/init-ring-mqtt.js \
  tsightler/ring-mqtt:5.9.3
```

Prompts for account, password and 2FA code, then saves `ring-state.json` to
`/data`. Rotation and expiry behaviour: `docs/decisions.md`, credential
register.

`ring_mqtt_data` is the same volume the bridge mounts: `docker-compose.yml`
pins it with an explicit `name:` so Compose does **not** prefix it with the
project directory. Confirm before moving on — if this shows nothing, the
bootstrap wrote to a volume the bridge will never read:

```bash
docker volume inspect ring_mqtt_data --format '{{.Mountpoint}}'
docker run --rm -v ring_mqtt_data:/data alpine ls -l /data/ring-state.json
```

### 3. Start the substrate

```bash
docker compose up -d
docker compose ps            # both healthy?
docker compose logs -f ring-mqtt
```

> **Configure ring-mqtt's MQTT connection on this first run.** The environment
> variable names for supplying broker host/user/password non-interactively were
> **not documented** on the upstream pages consulted when this was written, so
> `docker-compose.yml` deliberately does not guess them — a wrong variable name
> is accepted silently and the bridge simply never connects. Follow the current
> upstream wiki, and if you confirm the names, add them to the compose file and
> delete the warning comment there.

### 4. Add the MQTT integration in Home Assistant

Config flow — **this lands in `.storage`, not in this repo**:

**Settings → Devices & Services → Add Integration → MQTT**

| Field | Value |
|---|---|
| Broker | the Docker host's address |
| Port | `1883` |
| Username | `mqtt_username` from `secrets.yaml` |
| Password | `mqtt_password` from `secrets.yaml` |
| Discovery prefix | `homeassistant` (default — leave it) |

Record the actual values in `docs/decisions.md`, and note this as a **manual
step in the bare-metal rebuild path** (`docs/runbook.md` §9): a restore from
config backup does **not** bring the MQTT integration back.

### 5. Inventory the devices

Once discovery populates, produce the entity table (entity_id, device, physical
location, mapping), rename anything Ring auto-named badly per `CLAUDE.md`, and
write `devices/ring-<device>.yaml` per physical device — each recording its
**failure mode when Ring's cloud is unreachable**.

### 6. Label everything

**An unlabelled sensor is invisible to every automation in this repo.**

| Device type | Labels |
|---|---|
| Door/window contact | `security_perimeter` |
| Motion detector | `pet_exposed` **if the dog can reach it** |
| Any battery device | `battery_powered` |
| Base station / keypad | `critical_uptime` |

> ### ⚠️ Pet exposure — read before labelling a motion detector
>
> Under topology (a) a false intrusion is **a siren, a central-station call and
> a Honolulu false-alarm fee**, not a logbook entry.
>
> For every Ring motion detector, record its **pet-immunity weight rating** and
> whether the dog can physically enter its field of view. Ring's PIR motion
> detectors are commonly rated for pets under a stated weight **when mounted at
> the manufacturer's specified height and angle** — confirm the rating on the
> actual model's datasheet, because it is void if the unit is mounted low or
> aimed down a surface the dog can climb.
>
> Any detector the dog can reach gets `pet_exposed`, which removes it as
> standalone intrusion evidence in this repo's fusion logic. **That does not
> affect Ring's own dispatch decision** — Ring arms and dispatches on its own
> sensor logic, so a pet-triggered Ring motion event can still cause a dispatch
> regardless of any label here. If a detector is genuinely dog-exposed, the
> real fix is physical: raise it, re-aim it, or exclude it from the armed-away
> mode **in the Ring app**.

---

## Part B — HUMAN CHECKLIST (not code — these get forgotten)

These are done in the **Ring app** and with the city. None of them are
configuration in this repo, and every one of them is load-bearing.

- [ ] **Add partner as a shared user** in the Ring app, so she can arm and
      disarm independently. Confirm she can actually disarm from her own phone
      before relying on it.

- [ ] **Configure the MONITORING CONTACT LIST.** This is **separate from app
      logins** — a shared user is not automatically a monitoring contact.
      Set explicitly:
      - [ ] Contact 1: ______________________  (call order: 1st)
      - [ ] Contact 2: ______________________  (call order: 2nd)
      - [ ] Contact 3 — a **local, on-island** contact who can physically
            attend. Add once you have one; a mainland-only list means nobody
            can respond in person. ______________________

- [ ] **Set the verbal passcode** used to cancel a dispatch. Both of you must
      know it and be able to recall it under stress.
      > **The passcode value NEVER goes in this repo.** Recorded here only:
      > *that it exists*, and that it is set in the Ring app under monitoring /
      > account settings. Store the value in the household password manager.

- [ ] **Register the alarm permit with the City & County of Honolulu** if
      required for a monitored alarm, and record:
      - [ ] Permit number stored (password manager, not here): ☐
      - [ ] False-alarm fee schedule recorded below, with the number of free
            occurrences before fees begin:

      | Occurrence | Fee |
      |---|---|
      | 1st | |
      | 2nd | |
      | 3rd+ | |

      This is why pet exposure above is treated as a money-and-credibility
      problem, not a nuisance.

- [ ] **Put monitoring into TEST MODE before any live alarm test.** Never fire
      a real alarm without it — see `docs/alarm-testing.md`.

---

## Part C — verify

- [ ] Both containers healthy (`docker compose ps`).
- [ ] MQTT integration connected; Ring devices discovered.
- [ ] Every device labelled per the table above.
- [ ] `binary_sensor.alarm_system_degraded` is **off**.
- [ ] Stop `ring-mqtt` → the degraded sensor turns **on** and notifies; start it
      → it clears. (This proves the health monitoring is real. See
      `docs/alarm-testing.md`.)
- [ ] `./scripts/validate.sh` and `./scripts/audit.py` pass.
- [ ] `optional/ring.yaml` promoted to `packages/` and all placeholders filled
      (the placeholder scan enforces this on promotion).

---

# Phase 2 — migrating to Ring's panel

## The inventory (step 14) — 11 sites in 3 files

| File | Line | What it does | Migration |
|---|---|---|---|
| `packages/security.yaml` | 41 | `alarm_control_panel:` — the `manual` platform definition | **DELETE** (step 17) |
| `packages/security.yaml` | 147 | `script.arm_house` → `alarm_arm_away` | retarget |
| `packages/security.yaml` | 152 | `script.arm_house` → `alarm_arm_home` | retarget |
| `packages/security.yaml` | 157 | `script.arm_house` → `alarm_arm_night` | **remap → `alarm_arm_home`** (see below) |
| `packages/security.yaml` | 318 | intrusion automation: armed-state condition | **RETIRE the automation** |
| `packages/security.yaml` | 326 | intrusion automation: armed_away branch | **RETIRE** |
| `packages/security.yaml` | 336 | intrusion automation: `alarm_trigger` call | **RETIRE — must not exist under (a)** |
| `packages/security.yaml` | 352 | response automation: `to: triggered` | superseded by `ring_alarm_local_response` |
| `dashboards/home.yaml` | 24 | alarm badge | retarget |
| `dashboards/home.yaml` | 184 | alarm tile, `alarm-modes` feature | retarget + drop `armed_night` |
| `dashboards/guest.yaml` | 33 | guest status template | retarget |

## State and semantics mapping (step 15)

Home Assistant's canonical states (read from core, not assumed):
`disarmed, armed_home, armed_away, armed_night, armed_vacation,
armed_custom_bypass, pending, arming, disarming, triggered`.

| Concept | `manual` panel (today) | Ring Alarm | Lossy? |
|---|---|---|---|
| Disarmed | `disarmed` | `disarmed` | no |
| Stay/Home | `armed_home` | `armed_home` | no |
| Away | `armed_away` | `armed_away` | no |
| **Night** | `armed_night` — configured here | **Ring has no Night mode.** Ring Alarm is Disarmed / Home / Away. | **YES — see below** |
| Exit delay | `arming_time:` in YAML | set **in the Ring app**, not here | **YES** — no longer version-controlled |
| Entry delay | `delay_time:` in YAML | set **in the Ring app** | **YES** |
| Siren duration | `trigger_time:` in YAML | set in the Ring app | **YES** |
| Trigger from HA | `alarm_trigger` worked | **Do not use** — see below | intentional |
| Who disarmed | n/a | keypad codes not exposed over the bridge | **YES** — HA can never attribute a disarm |
| Bypass at arm | `script.arm_house` reports openings from the `security_perimeter` label | Ring computes its own bypass from its own sensors | **YES** — the two views can disagree |

### The Night-mode loss

`armed_night` has three consumers today: `script.arm_house` (mode `night`), the
dashboard tile's `alarm-modes` feature, and `presence_night_mode`. Ring offers
no equivalent.

**Decision: map Night → `armed_home`.** Ring's Home mode already arms the
perimeter and bypasses interior motion, which is what Night was for here. Keep
the `night` option in `input_select.house_mode` — it still drives lighting and
climate — but it now arms the panel *Home*.

Do **not** synthesise a fake Night by arming Away and bypassing sensors: that
changes what the monitoring centre is told about the house.

### Retiring the intrusion automation — the important one

`security_intrusion_trips_alarm` exists because the `manual` panel is inert: it
does not watch sensors, so something had to call `alarm_trigger`. **Ring's base
station watches its own sensors and triggers itself.**

Under topology (a) that automation must be **deleted, not migrated**:

1. It is redundant — Ring has already tripped by the time HA sees anything.
2. It is slower — HA's path is sensor → bridge → MQTT → HA → back through the
   bridge, versus the base station's local decision.
3. It puts Home Assistant **inside the dispatch chain**, which the architecture
   constraints forbid outright. A bridge glitch must never be able to summon a
   patrol car.

`binary_sensor.trustworthy_intrusion_signal` **stays** — it is still the right
signal for HA-side awareness and for the pet-immunity logic. It simply no
longer arms or triggers anything.

Likewise `security_alarm_triggered` is superseded by
`ring_alarm_local_response` in `optional/ring.yaml`, which does the same local
job (lights + critical notification) against the Ring panel.

### Confirm on the real panel before migrating

Ring's exact entity id and supported modes are not knowable from here. On the
live instance, before touching anything:

```
Developer Tools → Template:
  {{ states.alarm_control_panel | map(attribute='entity_id') | list }}
  {{ state_attr('<the ring panel>', 'supported_features') }}
```

`supported_features` is a bit field: `ARM_HOME=1, ARM_AWAY=2, ARM_NIGHT=4,
TRIGGER=8, ARM_CUSTOM_BYPASS=16, ARM_VACATION=32`. A value of `3` means Home +
Away only and confirms the Night mapping above. **If bit 4 is set, Ring does
expose Night and the remap is unnecessary** — verify rather than assume.

## Order of work (step 16 → 17)

1. Bind `REPLACE_ME_ring_panel` in `optional/ring.yaml` to the real entity.
2. Scenario-test in `testlab.yaml` against the Ring panel: arm Home, arm Away,
   disarm, and the local response on `triggered`.
3. Retarget the 11 sites above; delete the `manual` platform block and the
   intrusion automation.
4. Promote `optional/ring.yaml` → `packages/ring.yaml` (the placeholder scan
   blocks this until every `REPLACE_ME` is gone).
5. `./scripts/validate.sh && ./scripts/audit.py`, then deploy via `deploy.sh`.
6. Record the retirement in `docs/decisions.md`.
