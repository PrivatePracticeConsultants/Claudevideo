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
import json, os, pathlib, re, sys, urllib.request, urllib.error
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "alarm-system.md"
# Hand-maintained, NEVER written by this script. The insurer's document needs a
# live-test date, but it is regenerated on every run — so the date is typed
# there and reprinted here, rather than hand-edited into generated output.
TEST_LOG = ROOT / "docs" / "alarm-test-log.md"

# Sensor device_classes that count as a protected opening or space.
PROTECTIVE = {"door": "Entry point", "window": "Window", "opening": "Opening",
              "motion": "Motion", "occupancy": "Motion", "garage_door": "Garage",
              "smoke": "SMOKE", "gas": "GAS", "carbon_monoxide": "CARBON MONOXIDE",
              "moisture": "Water leak", "heat": "Heat"}
LIFE_SAFETY = {"smoke", "gas", "carbon_monoxide", "heat"}

# Device files that could not be parsed. Collected rather than raised so one
# typo cannot destroy the document, and printed into the document itself so
# an incomplete count is never read as a complete one.
UNREADABLE: list[tuple[str, str]] = []


def from_live() -> list[dict] | None:
    """Protected points from the live registry — REAL Ring devices only.

    Scoped to entities carrying the `ring` label. Without that scope this
    listed Home Assistant's own fusion/aggregate template sensors
    (human_motion_detected, corroborated_motion, any_window_open) and the
    testlab simulations as protected points — which is exactly what it did on
    its first run, reporting "6 protected points" for a house with no alarm
    hardware at all. On an insurer's document that is not a cosmetic bug.
    """
    url, token = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
    if not (url and token):
        return None
    def _get(path):
        req = urllib.request.Request(url.rstrip("/") + path)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    try:
        states = _get("/api/states")
        # `ring`-labelled entities, resolved through the template API so the
        # scope is the registry's, not a name-guess.
        req = urllib.request.Request(url.rstrip("/") + "/api/template",
                                     data=json.dumps(
                                         {"template": "{{ label_entities('ring') | list }}"}
                                     ).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as r:
            labelled = set(eval(r.read().decode(), {"__builtins__": {}}, {}))
    except (urllib.error.URLError, OSError, SyntaxError, ValueError) as e:
        print(f"  live registry unreachable ({e}); falling back to the register",
              file=sys.stderr)
        return None

    if not labelled:
        print("  live registry reachable but NO entities carry the `ring` "
              "label — falling back to the committed register", file=sys.stderr)
        return None

    rows = []
    for s in states:
        a = s.get("attributes", {})
        dc = a.get("device_class")
        if (s["entity_id"] in labelled
                and s["entity_id"].startswith("binary_sensor.")
                and dc in PROTECTIVE):
            rows.append({"entity_id": s["entity_id"],
                         "name": a.get("friendly_name", s["entity_id"]),
                         "kind": PROTECTIVE[dc], "device_class": dc,
                         "location": a.get("ha_area", ""), "source": "live registry"})
    # #14: an empty result must NOT masquerade as an authoritative answer.
    return rows or None


def from_register() -> list[dict]:
    """Protected points from devices/ring-*.yaml.

    Reads the schema devices/TEMPLATE.yaml actually defines. The first version
    invented `entity_id`/`kind`/`device_class` keys the template does not have,
    so a fully-registered contact sensor still produced "0 protected openings"
    — and a registered smoke listener would still have printed "do not claim
    fire monitoring". Silent under-reporting on an insurance document.
    """
    rows = []
    for f in sorted((ROOT / "devices").glob("ring-*.yaml")):
        # PER-FILE isolation. devices/*.yaml is hand-maintained, so a typo in
        # one file must not take out the document — the repo's fault-isolation
        # rule. But a SILENT skip is worse than a crash here: this document
        # states how many points an alarm protects, and quietly dropping a
        # registered sensor under-reports coverage to an insurer. So: skip the
        # file, keep going, and record it loudly enough that the number cannot
        # be mistaken for complete (see UNREADABLE, printed into the doc).
        try:
            d = yaml.safe_load(f.read_text()) or {}
        except (yaml.YAMLError, OSError) as e:
            first = str(e).strip().splitlines()[0]
            UNREADABLE.append((f.name, first))
            continue
        if not isinstance(d, dict):
            UNREADABLE.append((f.name, "file does not contain a YAML mapping"))
            continue
        dev = d.get("device", {}) or {}
        if not isinstance(dev, dict):
            UNREADABLE.append((f.name, "`device:` is not a mapping"))
            continue
        # device_class is what classifies a point; accept it at the top level or
        # infer from the documented `protects` field.
        dc = (dev.get("device_class") or dev.get("protects") or "").strip()
        entity = dev.get("entity_id") or dev.get("primary_entity") or "—"
        rows.append({"entity_id": entity,
                     "name": dev.get("slug", f.stem),
                     "kind": PROTECTIVE.get(dc, dc or "—"),
                     "device_class": dc,
                     "location": (dev.get("location") or {}).get("physical", ""),
                     "source": f.name})
    return rows


def last_live_test() -> str | None:
    """Most recent dated row of docs/alarm-test-log.md, or None.

    Deliberately forgiving: this is a human-typed table, and a malformed row
    must degrade to "no test recorded" rather than break the insurer document.
    """
    if not TEST_LOG.exists():
        return None
    # Strip HTML comments FIRST. A commented-out example row is still a line
    # beginning with "|", and letting one through would print an invented test
    # date into a document that goes to an insurer. Caught in review; do not
    # simplify this back to a per-line startswith("<!--") check.
    text = re.sub(r"<!--.*?-->", "", TEST_LOG.read_text(), flags=re.DOTALL)
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        try:
            datetime.strptime(cells[0], "%Y-%m-%d")
        except ValueError:
            continue          # header, separator, or the "_none yet_" placeholder
        rows.append(cells)
    if not rows:
        return None
    d = sorted(rows, key=lambda c: c[0])[-1]
    return f"| {d[0]} | {d[1]} | {d[2]} |"


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

    if UNREADABLE:
        w("> ## ⚠️ THIS DOCUMENT IS INCOMPLETE\n>\n"
          "> " + str(len(UNREADABLE)) + " device file(s) in `devices/` could not be read, so any\n"
          "> device they describe is MISSING from the counts below. Fix the file\n"
          "> and regenerate before sending this to anyone.\n>\n"
          + "\n".join(f"> - `{n}` — {e}" for n, e in UNREADABLE) + "\n")

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

    other = [r for r in rows if r not in life and r not in perimeter and r not in interior]

    w(f"## Interior — {len(interior)} motion detector(s)\n")
    if interior:
        w("| Entity | Type | Location | Pet-exposed |")
        w("|---|---|---|---|")
        for r in sorted(interior, key=lambda x: x["entity_id"]):
            w(f"| `{r['entity_id']}` | {r['kind']} | {r['location'] or '—'} | _see register_ |")
    else:
        w("_None recorded yet._")
    w("")

    if other:
        w(f"## Other monitored points — {len(other)}\n")
        w("| Entity | Type | Location |")
        w("|---|---|---|")
        for r in sorted(other, key=lambda x: x["entity_id"]):
            w(f"| `{r['entity_id']}` | {r['kind']} | {r['location'] or '—'} |")
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
      "the notification path is degraded. Immediate alert on transition, and a "
      "daily digest while it persists.\n")
    w("`sensor.alarm_system_healthy_7d` reports the degraded-free percentage "
      "**of observed time**, not of the calendar week. Hours when Home "
      "Assistant was not running are excluded rather than counted as healthy, "
      "and the sensor reports nothing at all below half a window of coverage. "
      "A figure here is therefore a measurement; a blank is an honest absence "
      "of one. Neither is a statement about whether the house was armed — Ring "
      "monitors independently of all of this.\n")
    w("**It reports Home Assistant's visibility, not the alarm's protection.** "
      "Ring arms, sirens and dispatches without the bridge; a degraded reading "
      "means HA has gone blind, not that the house is unprotected.\n")

    w("## Last live test\n")
    tested = last_live_test()
    if tested:
        w("| Date | Test | Result |")
        w("|---|---|---|")
        w(tested)
        w("")
        w("Full history: `docs/alarm-test-log.md`. Procedure: "
          "`docs/alarm-testing.md`.")
    else:
        w("> **No live end-to-end test has been recorded.** Arming, siren and "
          "the dispatch call have not been demonstrated together. Run "
          "`docs/alarm-testing.md` section E — with Ring monitoring in **test "
          "mode** — and log the date in `docs/alarm-test-log.md`.\n>\n"
          "> Do not present this document to an insurer as evidence of a "
          "tested system until that row exists.")
    w("")

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
    if UNREADABLE:
        for n, e in UNREADABLE:
            print(f"  ⚠️  UNREADABLE {n}: {e}")
        print("  The document was still written, and says on its face that it "
              "is incomplete. Exit code 1 so this is not missed in a script.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
