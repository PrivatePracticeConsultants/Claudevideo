#!/usr/bin/env python3
"""Generate docs/alarm-system.md — the protected-points document.

This goes to an insurer, so it is GENERATED, never hand-maintained: a document
that drifts from the real system is worse than none, because it is believed.

Sources, in order of preference:
  1. the live registry, if HA_URL + HA_TOKEN are set (authoritative)
  2. devices/ring-*.yaml, the committed device register (offline fallback)

    HA_URL=http://homeassistant.local:8123 HA_TOKEN=... \\
      python3 scripts/generate-alarm-doc.py

Never emits an address, a keypad code, or the monitoring passcode. It records
that those exist and where they are set — never their values.
"""
from __future__ import annotations
import json, os, pathlib, sys, urllib.request, urllib.error
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "alarm-system.md"

# Sensor device_classes that count as a protected opening or space.
PROTECTIVE = {"door": "Entry point", "window": "Window", "opening": "Opening",
              "motion": "Motion", "occupancy": "Motion", "garage_door": "Garage",
              "smoke": "SMOKE", "gas": "GAS", "carbon_monoxide": "CARBON MONOXIDE",
              "moisture": "Water leak", "heat": "Heat"}
LIFE_SAFETY = {"smoke", "gas", "carbon_monoxide", "heat"}


def from_live() -> list[dict] | None:
    url, token = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
    if not (url and token):
        return None
    req = urllib.request.Request(url.rstrip("/") + "/api/states")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            states = json.loads(r.read())
    except (urllib.error.URLError, OSError) as e:
        print(f"  live registry unreachable ({e}); falling back to the register",
              file=sys.stderr)
        return None
    rows = []
    for s in states:
        a = s.get("attributes", {})
        dc = a.get("device_class")
        if s["entity_id"].startswith("binary_sensor.") and dc in PROTECTIVE:
            rows.append({"entity_id": s["entity_id"],
                         "name": a.get("friendly_name", s["entity_id"]),
                         "kind": PROTECTIVE[dc], "device_class": dc,
                         "location": "", "source": "live registry"})
    return rows


def from_register() -> list[dict]:
    rows = []
    for f in sorted((ROOT / "devices").glob("ring-*.yaml")):
        d = yaml.safe_load(f.read_text()) or {}
        dev = d.get("device", {})
        rows.append({"entity_id": dev.get("entity_id", "—"),
                     "name": dev.get("slug", f.stem),
                     "kind": dev.get("kind", "—"),
                     "device_class": dev.get("device_class", ""),
                     "location": (dev.get("location") or {}).get("physical", ""),
                     "source": f.name})
    return rows


def main() -> int:
    rows = from_live()
    src = "the live Home Assistant registry"
    if rows is None:
        rows, src = from_register(), "devices/ring-*.yaml (offline register)"

    life = [r for r in rows if r["device_class"] in LIFE_SAFETY]
    perimeter = [r for r in rows if r["device_class"] in {"door", "window", "opening", "garage_door"}]
    interior = [r for r in rows if r["device_class"] in {"motion", "occupancy"}]

    L = []
    w = L.append
    w("# Alarm system — protected points\n")
    w(f"> **Generated** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
      f"from {src} by `scripts/generate-alarm-doc.py`.\n>\n"
      "> Regenerate after any change to the alarm hardware. Do not hand-edit:\n"
      "> edits are lost on the next run, and a drifted document is worse than\n"
      "> none because it is believed.\n")

    w("## System\n")
    w("| Field | Value |")
    w("|---|---|")
    w("| Panel | Ring Alarm base station |")
    w("| Monitoring | Ring Protect Pro — professional monitoring |")
    w("| Dispatch path | Ring base station → Ring monitoring centre → local dispatch |")
    w("| Home Assistant role | **Observability and local response only.** Not in the dispatch chain. |")
    w("| Bridge | ring-mqtt (Docker), read/write over MQTT |")
    w("| Cellular backup | _confirm on the Ring plan_ |")
    w("| Battery backup | Ring base station internal — _confirm runtime_ |")
    w("| Property address | **deliberately not recorded in this repo** — held in the Ring account |")
    w("")

    # ---- the life-safety flag: the money paragraph for the insurer ----
    w("## Life-safety coverage — READ THIS FIRST\n")
    if life:
        w("The following life-safety devices are present and monitored:\n")
        w("| Hazard | Entity | Location |")
        w("|---|---|---|")
        for r in life:
            w(f"| **{r['kind']}** | `{r['entity_id']}` | {r['location'] or '—'} |")
        w("")
        w("**Confirm each of these appears on the Ring monitoring certificate.** "
          "A device that reports in the app but is not listed on the certificate "
          "is not covered for the insurance credit.")
    else:
        w("> ## ⚠️ NO SMOKE / CO / HEAT LISTENER IS PRESENT\n>\n"
          "> This system currently protects against **intrusion only**.\n>\n"
          "> **This directly affects the insurance credit.** Fire and CO\n"
          "> monitoring is normally the larger of the two credits, and it can\n"
          "> only appear on the monitoring certificate if a monitored\n"
          "> life-safety device exists.\n>\n"
          "> Two ways to obtain it with Ring:\n"
          "> 1. **Ring Alarm Smoke & CO Listener** — sits beside existing\n"
          ">    smoke/CO alarms and listens for their T3/T4 temporal patterns.\n"
          ">    Cheapest path; requires working alarms that actually sound.\n"
          "> 2. **First Alert Z-Wave smoke/CO detectors** paired to the base\n"
          ">    station — monitored devices in their own right.\n>\n"
          "> **Until one is installed and listed on the certificate, do not\n"
          "> claim fire monitoring to the insurer.**")
    w("")

    w(f"## Perimeter — {len(perimeter)} protected opening(s)\n")
    if perimeter:
        w("| Entity | Type | Location |")
        w("|---|---|---|")
        for r in sorted(perimeter, key=lambda x: x["entity_id"]):
            w(f"| `{r['entity_id']}` | {r['kind']} | {r['location'] or '—'} |")
    else:
        w("_None recorded yet — no Ring contact sensors have been paired._")
    w("")

    w(f"## Interior — {len(interior)} motion detector(s)\n")
    if interior:
        w("| Entity | Type | Location | Pet-exposed |")
        w("|---|---|---|---|")
        for r in sorted(interior, key=lambda x: x["entity_id"]):
            w(f"| `{r['entity_id']}` | {r['kind']} | {r['location'] or '—'} | _see register_ |")
    else:
        w("_None recorded yet._")
    w("")

    w("## Arming logic\n")
    w("| Mode | Coverage | Set by |")
    w("|---|---|---|")
    w("| **Disarmed** | Life-safety only (if present) | Human, at the keypad or in the Ring app |")
    w("| **Home** | Perimeter armed, interior motion bypassed | Nightly automation, or human |")
    w("| **Away** | Perimeter and interior armed | Human. **Auto-arm is written but DISABLED** pending a tracker soak. |")
    w("")
    w("- **Nothing in Home Assistant ever disarms this alarm.** Disarm is a "
      "deliberate human act. No presence, arrival, geofence or schedule trigger "
      "may disarm a monitored panel.")
    w("- Ring keypad codes are not visible over the bridge, so Home Assistant "
      "**cannot identify who disarmed**. Attribution lives in the Ring app's "
      "event history.")
    w("- Entry and exit delays are configured **in the Ring app**, not here.")
    w("")

    w("## Health monitoring\n")
    w("`binary_sensor.alarm_system_degraded` turns on when the bridge, broker, "
      "base station, any sensor (>1h), any battery (<20%), any tamper flag, or "
      "the notification path is degraded. Immediate alert on transition, daily "
      "digest while it persists, and `sensor.alarm_system_healthy_7d` reports "
      "7-day healthy uptime.\n")
    w("**It reports Home Assistant's visibility, not the alarm's protection.** "
      "Ring arms, sirens and dispatches without the bridge; a degraded reading "
      "means HA has gone blind, not that the house is unprotected.\n")

    w("## Not recorded here, by policy\n")
    w("The property address, the keypad codes, and the monitoring verbal "
      "passcode are **deliberately absent from this repository**. The passcode "
      "exists and is set in the Ring account; both adults in the household know "
      "it. See `docs/prompts/22-ring.md`.\n")

    OUT.write_text("\n".join(L) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(rows)} protected point(s), "
          f"{len(life)} life-safety, source = {src}")
    if not life:
        print("  ⚠️  NO life-safety device found — the doc carries the "
              "fire-coverage warning prominently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
