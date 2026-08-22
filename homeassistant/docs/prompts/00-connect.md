# 00 — Wire Claude Code to the instance

**Status in this repo: one of three legs is done.**

| Leg | State |
|---|---|
| Filesystem access to `/config` | ✗ not done — this repo was built without one |
| Live introspection (token + hass-cli / MCP) | ✗ not done |
| Validation (`check_config`) | ✓ **done** — `scripts/validate.sh`, pinned by `.ha-version` |

Everything in `packages/` is registry-driven precisely because legs 1 and 2 were
missing. Once they exist, nothing here needs rewriting — but the entity_ids in
any *new* work must be checked against the live registry, per `CLAUDE.md`.

## Do this

1. **Filesystem.** Get `/config` onto the machine running Claude Code — the
   *Advanced SSH & Web Terminal* add-on over SSHFS, the *Samba* add-on, or keep
   this repo local and push with the *Git pull* add-on / `scripts/deploy.sh`.
   Then `git init` the config directory and commit before touching anything.
2. **Live introspection.** Long-lived token (Profile → Security), plus either
   `hass-cli` (`pipx install homeassistant-cli`) or an HA MCP server. The
   official *Model Context Protocol Server* integration is control-oriented and
   follows your Assist exposure settings; the community `ha-mcp` / `hass-mcp`
   servers read config and edit automations more capably.
3. **Validation.** Already working locally. On the box it is
   `ha core check`, or `python -m homeassistant --script check_config -c /config`
   in a container. Point `HA_PYTHON` at a python holding the version in
   `.ha-version`.

## Verify

> Using the HA connection, list every entity whose entity_id starts with
> `sensor.` and print the domain counts. Then run a config check and show me the
> output.

If that round-trips, you are set.

`./scripts/apply-taxonomy.py dump` does the same round-trip and prints domain
counts to stderr — use it as the smoke test once the token is set.

## Recommended

Stand up a disposable HA instance in Docker on a laptop and point this repo at
it first. Twenty minutes tells you whether the plumbing works without risking a
live house, and it becomes the staging box for `16-upgrade.md`.
