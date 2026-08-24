# Alarm testing

Two rules before anything in this document.

> ## 1. PUT RING MONITORING IN TEST MODE FIRST
>
> Ring app → **Settings → Alarm → Professional Monitoring → Test Mode**
> (or call the monitoring centre). Choose a window long enough for the whole
> session.
>
> **Never fire a real alarm without it.** A live trigger is a real dispatch: a
> patrol car, a false-alarm fee, and a permit strike against the address.
>
> Confirm test mode is ACTIVE before the first trigger, and confirm it is OFF
> again when you finish. An alarm left in test mode is an unmonitored alarm.

> ## 2. Home Assistant is not the alarm
>
> Everything here tests **observability and local response**. Ring's base
> station arms, sirens and dispatches on its own path. If a Home Assistant test
> fails, the house is still protected — you have found a blind spot in the
> monitoring of the monitor, which is what this document is for.

---

## A. Per-sensor check

Do this at install, then every six months. Ring app → History confirms the base
station saw each event; Home Assistant confirms the bridge did.

| # | Sensor | Action | Ring app shows | HA entity flips | Pass |
|---|---|---|---|---|---|
| 1 | _(each contact)_ | Open, wait 5s, close | Open → Closed | `on` → `off` | ☐ |
| 2 | _(each motion)_ | Walk the field of view | Motion detected | `on` → `off` | ☐ |
| 3 | _(each keypad)_ | Press a key | Keypad active | battery/tamper entity live | ☐ |
| 4 | Base station | — | Online | `binary_sensor.…_base` on | ☐ |

Tamper: open one sensor's case. Expect a tamper flag in the Ring app **and**
`binary_sensor.alarm_system_degraded` → `on` with `TAMPER:` in its `summary`.
Close it; expect both to clear.

## B. Arming automations

| Test | Method | Expect | Pass |
|---|---|---|---|
| Nightly auto-arm | Set `input_datetime.ring_nightly_arm_time` two minutes ahead, panel disarmed | Panel → Home; info notification | ☐ |
| Already armed | Repeat while already Armed Away | **No change** — never downgrades Away to Home | ☐ |
| Kill switch | `input_boolean.automations_paused` on, wait for the arm time | Nothing happens | ☐ |
| **No auto-disarm exists** | `grep -rn "alarm_disarm" packages/ optional/` | **Zero results.** If this ever returns a hit, stop and remove it. | ☐ |
| Auto-arm-away stays disabled | `automation.trigger` on `automation.ring_auto_arm_away_disabled_pending_soak` **with `skip_condition: false`** | Panel does **not** arm. Without that flag `automation.trigger` SKIPS conditions and the test proves nothing. | ☐ |

## C. The health sensor — simulate the failures

The point of this section: prove the alarm-system watchdog actually barks. Each
row should turn `binary_sensor.alarm_system_degraded` **on** within ~60 s (it
recomputes on a one-minute cycle), name the cause in `summary`, and send one
warning notification.

| # | Simulate | Command | Expect in `summary` | Pass |
|---|---|---|---|---|
| 1 | Bridge down | `docker compose stop ring-mqtt` | `ring-mqtt bridge is …` | ☐ |
| 2 | Broker down | `docker compose stop mosquitto` | `MQTT broker connection lost` | ☐ |
| 3 | Base station | Unplug the base station (test mode!) | `base station unreachable` | ☐ |
| 4 | Sensor offline | Pull a sensor battery, wait 1 h | `… alarm sensor(s) unavailable` | ☐ |
| 5 | Low battery | Raise `input_number.ring_battery_threshold` above a real reading | `low battery: …` | ☐ |
| 6 | Tamper | Open a sensor case | `TAMPER: …` | ☐ |
| 7 | Recovery | `docker compose start ring-mqtt mosquitto` | Sensor → `off`, recovery notice | ☐ |

Rows 1, 2 and 7 are the ones to run every time — they are cheap and they cover
the most likely real failure.

## D. The notification path

This is the single point of failure for every alert in the system. Re-verify it
after **every iOS update** and after reinstalling the Companion app.

1. Put the phone in **Do Not Disturb**.
2. Developer Tools → Actions → `script.notify_person`:
   ```yaml
   target: <person key>
   priority: critical
   title: Alarm test
   message: Critical path check
   ```
3. **It must make a sound with the phone silenced.** A silent banner is a
   FAIL — Critical Alerts permission has been lost. Fix per
   `docs/runbook.md` §12, then repeat.
4. Repeat for `warning` (should arrive, need not break DND) and `info`
   (persistent notification only).

## E. Coordinated live test

Do this once at install, then annually. Two people, ~30 minutes.

**Before:**
- ☐ Ring monitoring in **test mode** — confirmed active
- ☐ Both people present, both phones in hand
- ☐ Both know the **verbal passcode** (§F)
- ☐ Partner is a shared user in the Ring app and can disarm

**Run:**

| # | Step | Expect |
|---|---|---|
| 1 | Arm Away from the Ring app | Exit delay sounds; HA panel entity → `arming` → `armed_away` |
| 2 | Wait for the exit delay to finish | HA shows `armed_away` |
| 3 | Open a perimeter door | Entry delay begins; HA contact sensor flips |
| 4 | **Do not disarm.** Let the entry delay run out | Siren; HA panel → `triggered` |
| 5 | Local response fires | Interior and exterior lights to 100%; **critical** notification on both phones |
| 6 | Monitoring calls | Answer. **Give the verbal passcode to cancel.** |
| 7 | Disarm at the keypad | Siren stops; HA panel → `disarmed` |
| 8 | Check HA history | The whole sequence is in the logbook |

**After:**
- ☐ Ring monitoring **test mode OFF** — confirmed
- ☐ Panel armed or disarmed deliberately, as intended
- ☐ Add a dated row to `docs/alarm-test-log.md` (hand-maintained), then
  run `python3 scripts/generate-alarm-doc.py` — it reprints the newest
  row into `docs/alarm-system.md`. Do **not** type the date into
  `alarm-system.md` itself: that file is generated and the edit is lost
  on the next run.

## F. The verbal passcode — rehearse it

Step 6 above is the step people fumble, because it is the only one that
happens under adrenaline with a stranger on the phone.

- Both adults must know the passcode **from memory**.
- It is **not** in this repository, and must never be added to it.
- It is set in the Ring account; `docs/prompts/22-ring.md` records that it
  exists and where, never its value.
- If it is ever spoken to someone who should not have it, change it in the
  Ring app the same day.

Rehearsing the cancellation is the difference between a cancelled dispatch and
a false-alarm fee.
