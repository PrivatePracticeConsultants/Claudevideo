# 17 — Remote access and hardening (run once, re-audit via prompt 14)

> Lay out my remote access options — Nabu Casa, Tailscale/WireGuard, reverse
> proxy with TLS — with the tradeoffs, and implement my choice.
>
> Then harden the instance:
> - 2FA required on every user account; separate non-admin accounts for other
>   people in the house and for wall tablets.
> - Inventory every long-lived token and API credential in use (including the
>   one Claude Code uses), record its purpose and creation date in
>   `docs/decisions.md`, and set a rotation reminder automation.
> - IP ban / login-attempt threshold enabled; alert on failed logins through the
>   notification router.
> - Review what the MCP server and any exposed APIs can reach; the AI
>   integration gets the minimum entity exposure that still works.
> - Confirm cameras and streams are not reachable from outside except through
>   the chosen access path.
>
> Document the whole access model in the runbook, including how to revoke
> everything fast if a phone is lost.

## Already in place

- `secrets.yaml` gitignored; `secrets.yaml.example` documents every key, and CI
  proves it is complete by building a working `secrets.yaml` from it.
- Secrets scan in `scripts/validate.sh`, in the pre-commit hook
  (`scripts/install-hooks.sh`), and again in CI.
- Admin dashboard is `require_admin: true`.

## Not in place — needs the instance

- IP ban: add to `configuration.yaml` and restart.
  ```yaml
  http:
    ip_ban_enabled: true
    login_attempts_threshold: 5
  ```
  Deliberately not committed yet: enabling it without a working trusted-network
  or VPN path is a good way to ban yourself out of your own house. Turn it on
  once remote access is settled.
- 2FA per account, non-admin accounts for household and tablets.
- The token inventory. `scripts/deploy.sh` and `scripts/backup-verify.sh` both
  read `HA_TOKEN`; whatever token those get is one that must be in the register.
