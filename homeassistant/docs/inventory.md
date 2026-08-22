# Device inventory

The human-readable register. One row per physical device. Appended by
`docs/prompts/04-add-device.md` every time hardware is added.

Empty because no hardware has been paired yet — this repo has never been
connected to a live instance. The columns are the ones that matter at 2am when
something is broken, so keep them filled.

| Device | Model | Protocol | Area | Power | Battery | Added | Firmware checked | Depends on it | If it dies |
|---|---|---|---|---|---|---|---|---|---|
| _(none yet)_ | | | | | | | | | |

## Column meanings

- **Power** — mains / battery / PoE / USB. Determines whether it survives an outage.
- **Battery** — the actual cell type (CR2032, AA, 18650). The thing you need to
  know when you are standing at a shop.
- **Firmware checked** — the date someone last looked. The monthly audit
  (`docs/prompts/14-audit.md`) flags anything over six months.
- **Depends on it** — which automations break. Fill this from the audit, not
  from memory.
- **If it dies** — the manual fallback. Required by the physical-first rule; a
  device with no answer here is a single point of failure and should be flagged.

## Radio devices — routers vs end devices

Track which Zigbee devices are **routers** (mains-powered: plugs, bulbs,
switches) and which are **end devices** (battery: sensors, buttons). Mesh health
depends on router coverage, and `docs/prompts/18-radio.md` needs this list.

| Device | Router or end device | Parented to | LQI |
|---|---|---|---|
| _(none yet)_ | | | |
