# 18 — Radio and mesh health (run BEFORE pairing your first batch)

**This is the one to run first.** Changing Zigbee channel after 40 devices are
paired means re-pairing 40 devices.

> Plan the 2.4GHz spectrum before the mesh exists:
> - Pick a Zigbee channel (15, 20, or 25) that avoids my Wi-Fi channels, tell me
>   how to lock the AP's 2.4GHz channels so they can't auto-hop onto the Zigbee
>   channel later, and set the coordinator accordingly.
> - Confirm the coordinator is on a USB extension away from USB3 ports and
>   metal, or on Ethernet placed centrally if it's a network coordinator.
>
> Then, once devices exist, on request:
> - Pull the mesh topology, report LQI per link, identify weak links and battery
>   devices parented to distant routers, and recommend where to add router
>   devices (smart plugs).
> - Same exercise for Z-Wave if present: neighbor tables, dead nodes, heal
>   candidates.

## Why 15, 20, 25

Zigbee and 2.4GHz Wi-Fi share the band. Wi-Fi channels 1, 6 and 11 are the
non-overlapping set almost everyone uses, and each is ~22MHz wide. Zigbee 15,
20 and 25 sit in the gaps between them.

- Wi-Fi on 1 and 6 → Zigbee **20** or **25**
- Wi-Fi on 6 and 11 → Zigbee **15**
- Wi-Fi on 1 and 11 → Zigbee **20**

**Locking the AP matters as much as the channel choice.** An AP left on "auto"
will eventually hop onto the channel you carefully avoided, and the symptom is
devices dropping months later with no configuration change to blame.

## Physical placement

- USB coordinator on a **USB 2.0** port via a **1m extension cable**. USB 3
  ports emit broadband noise centred right on 2.4GHz; a stick plugged directly
  into one next to an SSD is the classic "Zigbee is unreliable" cause.
- Away from metal enclosures, the router, and the SSD.

## When you do run the mesh report

Re-enable the link-quality sensors first — `packages/_global.yaml` excludes
`*_linkquality` and `*_rssi` from the recorder, so there is no history to
analyse until you do. Exclude them again afterwards; see `docs/decisions.md`.

Record the chosen channel and the locked Wi-Fi channels in `docs/decisions.md`.
