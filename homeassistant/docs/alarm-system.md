# Alarm system — protected points

> **Generated** 2026-08-24 16:47 UTC from devices/ring-*.yaml (offline register) by `scripts/generate-alarm-doc.py`.
>
> Regenerate after any change to the alarm hardware. Do not hand-edit:
> edits are lost on the next run, and a drifted document is worse than
> none because it is believed.

## System

| Field | Value |
|---|---|
| Panel | Ring Alarm base station |
| Monitoring | Ring Protect Pro — professional monitoring |
| Dispatch path | Ring base station → Ring monitoring centre → local dispatch |
| Home Assistant role | **Observability and local response only.** Not in the dispatch chain. |
| Bridge | ring-mqtt (Docker), read/write over MQTT |
| Cellular backup | _confirm on the Ring plan_ |
| Battery backup | Ring base station internal — _confirm runtime_ |
| Property address | **deliberately not recorded in this repo** — held in the Ring account |

## Life-safety coverage — READ THIS FIRST

> ## ⚠️ NO SMOKE / CO / HEAT LISTENER IS PRESENT
>
> This system currently protects against **intrusion only**.
>
> **This directly affects the insurance credit.** Fire and CO
> monitoring is normally the larger of the two credits, and it can
> only appear on the monitoring certificate if a monitored
> life-safety device exists.
>
> Two ways to obtain it with Ring:
> 1. **Ring Alarm Smoke & CO Listener** — sits beside existing
>    smoke/CO alarms and listens for their T3/T4 temporal patterns.
>    Cheapest path; requires working alarms that actually sound.
> 2. **First Alert Z-Wave smoke/CO detectors** paired to the base
>    station — monitored devices in their own right.
>
> **Until one is installed and listed on the certificate, do not
> claim fire monitoring to the insurer.**

## Perimeter — 0 protected opening(s)

_None recorded yet — no Ring contact sensors have been paired._

## Interior — 0 motion detector(s)

_None recorded yet._

## Arming logic

| Mode | Coverage | Set by |
|---|---|---|
| **Disarmed** | Life-safety only (if present) | Human, at the keypad or in the Ring app |
| **Home** | Perimeter armed, interior motion bypassed | Nightly automation, or human |
| **Away** | Perimeter and interior armed | Human. **Auto-arm is written but DISABLED** pending a tracker soak. |

- **Nothing in Home Assistant ever disarms this alarm.** Disarm is a deliberate human act. No presence, arrival, geofence or schedule trigger may disarm a monitored panel.
- Ring keypad codes are not visible over the bridge, so Home Assistant **cannot identify who disarmed**. Attribution lives in the Ring app's event history.
- Entry and exit delays are configured **in the Ring app**, not here.

## Health monitoring

`binary_sensor.alarm_system_degraded` turns on when the bridge, broker, base station, any sensor (>1h), any battery (<20%), any tamper flag, or the notification path is degraded. Immediate alert on transition, daily digest while it persists, and `sensor.alarm_system_healthy_7d` reports 7-day healthy uptime.

**It reports Home Assistant's visibility, not the alarm's protection.** Ring arms, sirens and dispatches without the bridge; a degraded reading means HA has gone blind, not that the house is unprotected.

## Not recorded here, by policy

The property address, the keypad codes, and the monitoring verbal passcode are **deliberately absent from this repository**. The passcode exists and is set in the Ring account; both adults in the household know it. See `docs/prompts/22-ring.md`.

