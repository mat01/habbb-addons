# habbb-addons

Private Home Assistant add-on repository for [habbb](https://habbb.com) — the managed HA service for UK homeowners.

Addons in this repo are installed by `scripts/finalize.ts` during hub provisioning via the Supervisor API (`store/repositories/add` → `store/addons/<slug>/install`). They are not intended for general public consumption.

## Current contents

| slug | purpose |
|---|---|
| `habbb_backup_sync` | Triggers scheduled full HA backups via Supervisor API and uploads the resulting tars to the customer-scoped S3 prefix (`s3://habbb-hub-backups/<customer-id>/`). AWS credentials are minted per-hub by the `habbb-provision-hub` Lambda (in the [habbb-cdk](https://github.com/mat01/habbb-cdk) stack) and set as addon options during finalize. |

## Adding this repo to a hub

Not for end users. The `finalize.ts` script calls:

```
POST http://supervisor/store/repositories { "repository": "https://github.com/mat01/habbb-addons" }
POST http://supervisor/store/reload
POST http://supervisor/store/addons/habbb_backup_sync/install
POST http://supervisor/addons/habbb_backup_sync/options  { "options": { ...creds... } }
POST http://supervisor/addons/habbb_backup_sync/start
```

## Developing an addon locally

1. Edit addon files in the appropriate subdirectory
2. Bump `version` in `config.yaml`
3. Commit + push — HA Supervisor pulls from git via the published repository URL and rebuilds on install/update
4. On a dev hub: `ha store reload` then `ha apps update habbb_backup_sync`
