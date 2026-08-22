# 04 — ADD A DEVICE (the reusable one)

Paste this every time hardware arrives.

---

> **Add a device to the system.**
>
> Device: `<make / model>`
> Protocol: `<Zigbee | Z-Wave | Matter/Thread | Wi-Fi | ESPHome | IR/RF | cloud>`
> Location: `<area>`
> What it's for: `<one sentence — the actual job, not the feature list>`
> Already paired: `<yes / no>`
>
> Do the following:
>
> 1. If not yet paired, tell me the pairing steps for this specific model and
>    this specific stack, including any quirks (binding, firmware update first,
>    quirk file needed for ZHA, Z2M external converter, exclusion before
>    inclusion, etc.).
> 2. Once paired, read the live registry and list every entity the device
>    exposed, including the diagnostic and config ones most people ignore. Tell
>    me which are actually useful and which to disable to keep the registry clean.
> 3. Rename entities per CLAUDE.md conventions and assign area + the appropriate
>    labels **from the table in `docs/prompts/02-taxonomy.md`** — the packages
>    are inert on this device until it is labelled.
> 4. Create `devices/<slug>.yaml` from `devices/TEMPLATE.yaml`, documenting:
>    model, protocol, firmware version, date added, where it physically is, what
>    powers it, battery type, what depends on it, and its failure mode — what
>    breaks and what happens manually if this device dies.
> 5. Append it to `docs/inventory.md`, including whether it is a Zigbee router
>    or an end device.
> 6. Check whether existing automations should now pick it up. Because
>    automations target labels, most should be automatic — confirm which ones
>    will now include it, and flag any that hardcode entity lists as tech debt.
> 7. Propose (don't write yet) any new automations this device makes possible.
>    A numbered list, one line each, and I'll pick.
> 8. Run `./scripts/validate.sh` and tell me whether reload or restart is needed.
>
> If this device duplicates a capability I already have, say so and ask whether
> I want to replace or keep both.

---

## Repo-specific reminders

- **Labelling is the integration step.** Nothing in `packages/` targets entity
  ids, so an unlabelled device is invisible to every automation in the system.
- **A motion or mmWave sensor the dog can reach gets `pet_exposed`.** Not
  optional — see `docs/decisions.md`, pet immunity.
- **A wall remote or smart switch**: wire its event entity into the owning
  package (`packages/lighting.yaml`) so manual-override detection actually sees
  it. Record in `devices/<slug>.yaml` that you did, and where.
- **A lock**: its code-slot event entity is what lets
  `security_unlock_notify` report *which* code was used. Until it is wired, that
  automation reports the lock only.
- **Update `firmware_checked`** with today's date. The monthly audit flags
  anything over six months.
