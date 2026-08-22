# 21 — Wall tablets (kiosk dashboards)

Needs the live instance: the tablet's exact resolution, a dedicated HA user, and
several HACS components. The battery-management half is a **safety** requirement,
not a nicety.

> Set up tablet `<name>` mounted in `<area>`. It runs
> `<Fully Kiosk Browser on Android | Safari kiosk on iPad>`.
>
> **Account and dashboard:**
> - Create a dedicated non-admin HA user for this tablet (per prompt 17). It can
>   control devices but cannot reach settings, developer tools, or other users'
>   data.
> - Build a YAML dashboard `tablet-<name>` designed for a fixed landscape
>   viewport at this tablet's exact resolution — not a responsive afterthought of
>   the mobile view.
> - Install kiosk-mode (HACS) and strip the header and sidebar for this user only.
> - Views, in order: a **home view** for the room it lives in (lights, climate,
>   media for that area, plus a whole-house status strip: mode, alarm, anything
>   open or wrong); a **rooms view** generated from the area registry; a
>   **security view** with camera feeds and lock controls; a **music/scenes
>   view**. Navigation as a persistent bottom bar with large targets —
>   everything operable with a thumb from two feet away, minimum 48px touch
>   targets, no scrolling on the home view.
> - Consistent custom theme (dark, high contrast), and use browser_mod for
>   confirmation popups on anything destructive (alarm disarm, unlock).
> - Auto-return to the home view after 60 seconds idle (browser_mod).
>
> **The tablet as a device:**
> - Integrate it via the Fully Kiosk integration so HA controls it: screen wakes
>   when the room's presence sensor sees someone approach, dims with sun
>   elevation, sleeps in `night` mode, and shows a photo-frame/clock screensaver
>   when idle during the day.
> - **Battery management is mandatory for a permanently docked tablet**: power it
>   through a smart plug and write an automation holding charge between 20% and
>   80%. A tablet pinned at 100% on a charger for a year swells its battery —
>   this is a safety issue, not a nicety.
> - Report tablet battery, screen state, and charging state to the health
>   package; alert if a tablet goes offline or its battery drains unexpectedly.
> - Auto-relaunch the dashboard if the browser crashes or the page errors.
>
> Repeat for the second tablet with its own user, dashboard, and room context —
> shared theme and layout templates so a design change propagates to both.

## Already in place

- **Shared theme**: `themes/house.yaml` (`house_dark`) — high contrast, chosen
  for reading at two feet. Both tablets use it, so a change propagates to both.
- **The 20–80% charge automation is written**: `optional/tablets.yaml`. It is in
  `optional/` because the Fully Kiosk integration and its entities do not exist
  on this instance yet. Move it to `packages/` once they do, and replace the
  two placeholder entity_ids at the top.
- **Status strip entities** already exist and need no work:
  `sensor.house_status_summary`, `sensor.house_secure_summary`,
  `input_select.house_mode`, `alarm_control_panel.house`.

## HACS components required

`kiosk-mode`, `browser_mod`, and the Fully Kiosk integration (core, but needs
the app's remote-admin API enabled). All three are why this is prompt 21 and not
part of the built dashboards.
