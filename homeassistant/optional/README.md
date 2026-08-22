# optional/

Config that depends on **custom components not installed on this instance**.

`check_config` fails hard on an unknown integration, so if these lived in
`packages/` the validation gate would be red permanently — and a permanently red
gate is one everyone learns to ignore.

**To activate one:** install the component via HACS, replace any `REPLACE_ME`
placeholders with real entity_ids read from the live registry, move the file to
`packages/`, and run `./scripts/validate.sh`.

The placeholder scan in `validate.sh` deliberately skips this directory and
enforces it everywhere else — so moving a file here into `packages/` with
placeholders still in it fails the gate, which is the intended safety net.

| File | Needs | Prompt |
|---|---|---|
| `frigate.yaml` | Frigate NVR + the Frigate HACS integration | 08 |
| `tablets.yaml` | Fully Kiosk integration, kiosk-mode, browser_mod | 21 |
