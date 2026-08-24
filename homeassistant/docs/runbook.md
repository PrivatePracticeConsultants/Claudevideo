# Runbook

**This is written for whoever is standing in the house when something breaks,
not for the person who built it.** You do not need to understand the system.
Follow the numbered steps for the thing that is wrong.

If you are in a hurry: **the house still works without Home Assistant.** Every
light has a switch, every lock has a key or a keypad, the heat has a
thermostat. Nothing in this system can lock you in, lock you out, or leave you
in the dark. Jump to §1 if that is all you needed to know.

---

## 1. Manual control — HA is down and you just need the house to work

| You want to | Do this |
|---|---|
| Turn a light on | Use the wall switch. All of them are ordinary switches. |
| Unlock a door | Use the physical key, or the keypad code on the lock itself. |
| Change the temperature | Use the thermostat / the mini-split remote directly. |
| Silence the alarm | See §2. |
| Open the garage | Pull the red emergency release cord, then lift by hand. |

**The one rule that makes this true:** no smart bulb is ever installed behind a
switch that can cut its power. If someone ever "upgrades" a fixture and breaks
that rule, this table stops being true — see `CLAUDE.md`, physical-first rule.

> **Fill in when the hardware exists:** the exact location of the keypad, the
> spare key, and the garage release. This table is generic until then, and a
> generic runbook is only half a runbook.

---

## 2. The alarm is going off and you cannot get to a dashboard

1. Enter the alarm code on any wall keypad if one is installed.
2. If not, open Home Assistant on a phone on the local network:
   `http://homeassistant.local:8123` → **Alarm** → **Disarm** → enter the code.
3. No network at all? The siren is on a smart switch. Find its breaker or
   unplug it. Note which one — write it here once you have.
4. Once it is quiet, check **why**: HA → History →
   `binary_sensor.trustworthy_intrusion_signal`. Its `reason` attribute says
   which sensor fired.
5. If it was the dog, that is a bug, not a false alarm — see §8.

---

## 3. Home Assistant will not boot

Symptoms: dashboard does not load, phone app says cannot connect, but the
machine is powered on.

1. Wait **five minutes**. After an update, first boot genuinely takes that long.
   Do not power-cycle during this.
2. Get to the machine's console or SSH in.
3. Read the log:
   ```
   ha core logs           # HA OS / supervised
   docker logs homeassistant --tail 100    # container
   ```
4. Look for the **first** line containing `ERROR` — not the last. The first
   error is the cause; everything after it is fallout.
5. If it names a file in this repo, the last deploy is bad. Go to §4.
6. If it names an integration you did not touch, that integration is broken by
   an update. Disable it to get booting:
   ```
   # edit configuration.yaml, comment out the integration, then:
   ha core restart
   ```
7. Still dead → restore the most recent backup:
   ```
   ha backups list
   ha backups restore <slug>
   ```
8. Nothing works → §7, full rebuild.

---

## 4. A bad YAML deploy broke everything

1. `scripts/deploy.sh` rolls back automatically when its config check fails, so
   this should be rare. If it happened anyway, roll back by hand:
   ```
   cd /config
   git log --oneline -10          # find the last commit that worked
   git tag | grep deployed | tail -5   # every good deploy is tagged
   git reset --hard <last-good-tag>
   ```
2. Restart HA: `ha core restart`.
3. Confirm what is now running: the **Admin** dashboard shows the deployed
   commit, or `git -C /config rev-parse --short HEAD`.
4. Do not try to fix the broken commit on the box. Fix it in the repo, let CI
   check it, and deploy again.

---

## 5. The Zigbee/Z-Wave coordinator died

Symptom: every battery device unavailable at once; mains devices too.
`binary_sensor.critical_devices_online` is `off`.

1. Check the stick is physically still in the port. They work loose.
2. Restart HA before anything drastic — a USB stick that stopped responding
   usually comes back.
3. Confirm the OS still sees it:
   ```
   ls -l /dev/serial/by-id/
   ```
   Nothing there → the stick or the port is dead.
4. **Do NOT re-pair everything.** The network key and device list live in the
   coordinator's backup, not in the devices.
   - ZHA: Settings → Devices → Zigbee → **Download backup** (do this
     *periodically*, not now — now it is too late if you have none).
   - Restore that backup onto a replacement stick and every device rejoins by
     itself.
5. Replacement stick, no backup → you must re-pair everything. Work from
   `docs/inventory.md`, room by room.
6. While it is down: everything still works from the wall. See §1.

---

## 6. Internet is down

Home Assistant does **not** need the internet. Lights, locks, climate, and
automations all keep working. What stops: phone notifications from outside the
house, cloud voice, and remote access.

1. Check which half broke — they look identical from a phone but need different
   fixes. On the **Admin** dashboard:
   - `sensor.internet_latency` unavailable → no route out. It is the ISP or the
     router.
   - `sensor.dns_resolution` says `failed` but latency is fine → **DNS only**.
     The connection works; name lookups do not.
2. DNS-only: restart whatever resolves names (Pi-hole, AdGuard, the router).
   This is the most common of the two and the fastest to fix.
3. No route out: power-cycle the modem, wait two minutes, then the router.
4. Still down after 15 minutes → it is the ISP. Nothing to do here.
5. Note: alerts during an outage land as **persistent notifications on the local
   dashboard**, because a push cannot leave the house. Check there.

---

## 7. Power came back after an outage

1. Give it **ten minutes** before touching anything. The network comes up
   before HA does, HA comes up before the radios settle, and battery devices
   re-announce over several minutes.
2. Then check the **Admin** dashboard:
   - `sensor.unavailable_entities` should fall to 0.
   - `binary_sensor.critical_devices_online` should be `on`.
3. Anything still unavailable after 30 minutes: power-cycle that specific
   device.
4. Check the alarm went back to the right state — it restores its previous
   state, which may not be what you want now.
5. Check `sensor.database_size` did not balloon; a dirty shutdown can corrupt
   the recorder DB. If HA is slow and the log mentions the database, stop HA,
   delete `/config/home-assistant_v2.db`, and start it. **You lose history, not
   configuration.** That trade is almost always right.

---

## 8. The dog is setting things off

1. Find which sensor: History → `binary_sensor.human_motion_detected` →
   `triggered_by`.
2. That sensor is not labelled correctly. In HA: Settings → Devices → find it →
   add the label **`pet_exposed`**.
3. That single label change is the whole fix. A `pet_exposed` sensor can no
   longer, on its own, establish occupancy or trigger the alarm — it needs a
   corroborating signal.
4. If it keeps happening, the sensor needs physical work:
   - Mount PIR sensors **above 4 feet / 1.2m** and aim them slightly upward.
   - Many PIRs have a pet-immune lens mask — fit it.
   - mmWave: turn down sensitivity, and set a minimum target height if the
     firmware supports it. mmWave sees a dog *very* well.
5. When the dog is away (boarding, travelling), turn **off**
   `input_boolean.pet_at_home` — corroboration requirements relax and motion
   becomes trustworthy again.

---

## 9. Full rebuild from bare metal

You have a dead machine and a backup.

1. Install Home Assistant OS on the new hardware. Do not restore yet.
2. Complete onboarding with a throwaway account.
3. Settings → System → Backups → **Upload backup** → restore **full**.
4. Reboot. Wait ten minutes.
5. Re-plug the Zigbee/Z-Wave coordinator into the **same physical port** if you
   can — some integrations pin the device path.
6. Clone the config repo back over `/config` if the backup predates the last
   deploy:
   ```
   cd /config && git fetch origin && git reset --hard origin/main
   ```
7. Recreate `secrets.yaml` from `secrets.yaml.example` — it is deliberately
   **not** in the repo, so you need the values from your password manager.
8. `./scripts/validate.sh`, then `ha core restart`.
9. Work through `docs/inventory.md` and confirm each device is back.
10. **Take a fresh backup and verify it** (`scripts/backup-verify.sh`) before
    calling this done.

---

## 10. Flashing an ESPHome node

1. New node: copy `esphome/TEMPLATE-node.yaml` to `esphome/<name>.yaml`, set
   `node_name` and `node_friendly`, add the sensors.
2. First flash must be over USB — a bare board has no Wi-Fi credentials:
   ```
   esphome run esphome/<name>.yaml
   ```
3. Every flash after that is over the air; same command, it finds the node.
4. Node stuck in a crash loop and refusing OTA? Press **Restart into safe
   mode** on its device page in HA. Safe mode boots without sensors and will
   accept an OTA. This is the difference between a fix from the sofa and a
   ladder.
5. Node unreachable entirely: it falls back to its own hotspot,
   `<node_name> fallback`. Join it and reconfigure at `192.168.4.1`.

---

## 12. The critical notification path (iOS) — verify this, don't assume it

**This is the single point of failure for every alert in the house**, including
the alarm. If it is broken, everything downstream is silent and you will not
find out until the night it matters. Verify it on setup, and re-verify it after
every iOS major update and every Companion-app reinstall.

### 12.1 One-time setup

1. Install **Home Assistant** (Companion) from the App Store on the phone.
2. Open it, sign in to your instance, and **accept the notification permission
   prompt**. If you decline it here, nothing below will work.
3. Confirm the device registered: in Home Assistant, **Developer Tools →
   Actions**, search `notify.` — your phone appears as
   `notify.mobile_app_<device_name>`. **Copy that exact name.**
4. Put it in the `routes:` table in `packages/global.yaml` (there is a
   commented block ready for it). Do not guess the name — read it from the list.
5. Reload scripts (or deploy normally). No restart needed.

### 12.2 Grant Critical Alerts — the step everyone misses

The critical payload this repo sends (`interruption-level: critical` plus
`push.sound.critical`) **does nothing on its own**. Critical Alerts is a
separate iOS entitlement the user must grant.

**The authoritative check** — this is what actually decides whether an alarm
wakes you:

> **iOS Settings → Notifications → Home Assistant → Critical Alerts must be ON.**

If that toggle is **not present**, the app has never requested the entitlement:
open the Companion app, go to its notification settings and re-run notification
setup, then re-check. Grant the prompt when iOS shows it.

> The exact in-app menu path varies by Companion version and could not be
> verified from the official docs at the time of writing — the iOS Settings
> path above is authoritative regardless of app version, so check there.

### 12.3 The test that counts

A delivery is **not** a pass. A persistent notification appearing in HA is
**not** a pass. The only pass is: **the phone made noise while in Do Not
Disturb.**

1. Put the phone in **Do Not Disturb / a Focus mode**. Leave the ringer switch
   silenced too — critical alerts are supposed to beat both.
2. Lock the phone and set it down.
3. From **Developer Tools → Actions**, run:

   ```yaml
   action: script.notify_person
   data:
     target: owner
     priority: critical
     title: Critical path test
     message: If this made noise, the alert path works.
   ```

4. **Confirm all three:** it appeared on the lock screen, it played a sound,
   and it did so with DND active.

**If it was silent:** the entitlement is off (§12.2), or the route in
`routes:` names a service that does not exist — a wrong service name is
accepted by HA and discarded silently. Check Developer Tools → Actions again.

**If nothing arrived at all:** check `script.notify_person` in the logbook. If
it raised an *Undeliverable* persistent notification, the `routes:` table has
no push entry for that target — that notice exists precisely so this failure is
visible rather than silent.

### 12.4 Re-verify after an OS update

iOS major updates can reset notification entitlements, and restoring a phone
from backup does not always carry Critical Alerts across.

After **any iOS major update, phone replacement, or Companion reinstall**:

1. Re-check the toggle in §12.2.
2. Re-run the DND test in §12.3. It takes ninety seconds.
3. Note the date in `docs/decisions.md` under the token/credential register.

If the phone is replaced, the `notify.mobile_app_*` service name changes —
update `routes:` in `packages/global.yaml` or every alert silently stops.

## 11. Who to call / where things are

> Fill this in. It is the part of the runbook that cannot be written in
> advance, and the part most likely to be needed by someone who is not you.

- Electrician:
- HVAC:
- ISP + account number:
- Alarm monitoring (if any):
- Breaker panel location:
- Water shutoff:
- Coordinator stick model + spare:
- Password manager (where the secrets live):
