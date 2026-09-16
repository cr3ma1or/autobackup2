# Release Notes

## Stabilization release — 2026-09-16

### Key results

- E2E smoke test confirms encrypted archive processing, SQLite replication, client Dual-Layer consistency and host invariants.
- `xui-standby` supports opt-in path overrides for isolated environments without changing production defaults.
- E2E sandbox is removed automatically unless diagnostic flag `--keep` is supplied.
- Configuration templates now match the supported runtime keys and the allowlist protects `webPort`, `subPort`, `tgBotEnable` and `subURI`.
- Operational documentation covers installation, unattended deployment, status checks, systemd timers, path permissions, SQLite transactions and Reality identity isolation.

### Deployment readiness

The release is ready for deployment after the validation commands documented in `AGENTS.md` and the release checklist have passed in the target Linux environment. Production defaults preserve the existing Standby isolation, explicit SQLite transaction boundaries and Dual-Layer merge contract.
