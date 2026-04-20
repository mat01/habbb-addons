# habbb backup sync

Triggers Home Assistant Supervisor backups on a schedule and uploads the resulting `.tar` files to the customer's prefix in the habbb S3 bucket.

This add-on is **installed and configured automatically** by `scripts/finalize.ts` during hub provisioning — no manual interaction is expected. The credentials it receives are scoped to a single customer's prefix (one IAM user per hub, minted by the `habbb-provision-hub` Lambda in the [habbb-cdk](https://github.com/mat01/habbb-cdk) stack) and cannot read or write to any other customer's data.

## What it does

1. Every `backup_freq_hours` (default 24), calls `POST http://supervisor/backups/new/full` to trigger a full Supervisor backup. Supervisor pauses recorder DB writes during the snapshot, so the resulting `.tar` is point-in-time consistent.
2. Every `poll_interval_seconds` (default 60), scans `/backup/` for new `.tar` files and uploads any that are (a) not zero bytes, (b) at least 60 s old (so we don't pick up partially-written files), and (c) not already in our `/data/uploaded.json` state file.
3. Retention is **not handled here** — the habbb bucket has a CDK-managed lifecycle policy that tiers objects to Glacier IR at 30 days and expires noncurrent versions at 90 days. Local backup retention is still managed by Home Assistant's own defaults.

## Options

| option | default | notes |
|---|---|---|
| `bucket` | `""` | S3 bucket — set by finalize.ts to `habbb-hub-backups` |
| `prefix` | `""` | Object prefix — set to `<customer_id>/` by finalize.ts |
| `region` | `eu-west-1` | |
| `aws_access_key_id` | `""` | Per-hub IAM user access key, scoped to the prefix |
| `aws_secret_access_key` | `""` | (password type) |
| `poll_interval_seconds` | `60` | Scan interval (int 30–3600) |
| `backup_freq_hours` | `24` | Backup cadence (int 1–168) |

When any of the four credential fields is empty, the addon enters a wait loop rather than crash — this lets finalize.ts install the addon first and set the options a moment later.

## State

Persistent state is kept at `/data/uploaded.json`:

```json
{
  "last_backup_trigger": 1713593634.2,
  "uploaded": {
    "habbb-20260420-213015.tar": {
      "size": 200123456,
      "mtime": 1713593634.5,
      "uploaded_at": "2026-04-20T21:31:00Z",
      "key": "<customer-id>/habbb-20260420-213015.tar"
    }
  }
}
```

On restart, anything already in `uploaded` is skipped. If a file changes on disk (new size or mtime), it gets re-uploaded — this covers the edge case where Supervisor overwrites a same-named backup.

## Security

- IAM user's policy restricts `s3:PutObject`/`GetObject`/`ListBucket` to the customer's prefix only. If the credentials leak, the blast radius is one customer's backups.
- Backups are encrypted at rest with SSE-S3 (AWS-managed keys). No customer-level passphrase today; add restic in a future iteration for zero-knowledge backups.
- The addon has `hassio_role: backup`, which is the narrowest Supervisor role that can trigger backups — it does not grant `manager` or `admin` permissions.
