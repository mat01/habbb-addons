#!/usr/bin/env python3
"""
habbb backup-sync addon.

Two jobs:
  1. Every `backup_freq_hours`, trigger a full Supervisor backup
     (Supervisor pauses DB writes to produce a consistent .tar in /backup/)
  2. Every `poll_interval_seconds`, scan /backup/ for new .tar files and
     upload any unseen (and not-still-being-written) ones to
     s3://<bucket>/<prefix><filename>, using the addon-options AWS creds
     (per-hub IAM user minted by habbb-provision-hub Lambda).

S3 lifecycle on the bucket (managed by habbb-cdk) handles tiering to
Glacier IR at 30 days and non-current version cleanup at 90 days, so this
addon does not prune anything on the hub or in S3.

State is persisted to /data/uploaded.json so restarts don't re-upload
everything. Failures on individual uploads are logged and retried on the
next poll — the state file is only updated on successful upload.

Configuration comes from /data/options.json, which Supervisor populates
from the addon's options when it starts the container.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError


OPTIONS_PATH = Path("/data/options.json")
STATE_PATH = Path("/data/uploaded.json")
BACKUP_DIR = Path("/backup")
SUPERVISOR_BACKUPS_URL = "http://supervisor/backups/new/full"

# A .tar freshly produced by Supervisor is written atomically, but be safe:
# refuse to upload anything whose mtime is younger than MIN_MTIME_AGE_SECONDS.
MIN_MTIME_AGE_SECONDS = 60


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {msg}", flush=True)


def load_options() -> dict:
    if not OPTIONS_PATH.exists():
        log(f"ERROR: {OPTIONS_PATH} missing — Supervisor did not inject options")
        sys.exit(1)
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            log(f"WARN: corrupt state file, starting fresh: {e}")
    return {"uploaded": {}, "last_backup_trigger": 0}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def trigger_backup_if_due(state: dict, freq_hours: int) -> None:
    """Request a full backup via Supervisor API if enough time has passed."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        log("WARN: SUPERVISOR_TOKEN missing — skipping scheduled backup trigger")
        return
    now = time.time()
    due_at = state.get("last_backup_trigger", 0) + (freq_hours * 3600 - 60)
    if now < due_at:
        return
    name = f"habbb-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    log(f"Triggering full backup '{name}' via Supervisor API")
    body = json.dumps({"name": name, "compressed": True}).encode()
    req = urllib.request.Request(
        SUPERVISOR_BACKUPS_URL,
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        # Supervisor returns immediately with the slug; the actual backup
        # creation is async. Give it a reasonable socket timeout anyway.
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            if data.get("result") == "ok":
                slug = data.get("data", {}).get("slug", "<unknown>")
                log(f"Backup triggered, Supervisor slug={slug}")
                state["last_backup_trigger"] = now
                save_state(state)
            else:
                log(f"Backup trigger returned non-ok: {data}")
    except urllib.error.URLError as e:
        log(f"Backup trigger HTTP error: {e}")
    except Exception as e:
        log(f"Backup trigger unexpected error: {e}")


def sync_uploads(state: dict, s3, bucket: str, prefix: str) -> None:
    """Upload any new, stable .tar files in BACKUP_DIR to S3."""
    now = time.time()
    if not BACKUP_DIR.is_dir():
        log(f"WARN: {BACKUP_DIR} is not a directory")
        return
    for tar in sorted(BACKUP_DIR.glob("*.tar")):
        name = tar.name
        try:
            st = tar.stat()
        except FileNotFoundError:
            continue
        if st.st_size == 0:
            continue
        if (now - st.st_mtime) < MIN_MTIME_AGE_SECONDS:
            # Still being written, or raced with a creation — wait a poll.
            continue
        existing = state["uploaded"].get(name)
        if (
            existing
            and existing.get("size") == st.st_size
            and existing.get("mtime") == st.st_mtime
        ):
            continue
        key = f"{prefix}{name}"
        log(f"Uploading {name} ({st.st_size / 1024 / 1024:.1f} MB) → s3://{bucket}/{key}")
        try:
            s3.upload_file(
                str(tar), bucket, key,
                ExtraArgs={
                    "Metadata": {
                        "habbb-prefix": prefix,
                        "ha-mtime": str(int(st.st_mtime)),
                    },
                },
            )
            state["uploaded"][name] = {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "key": key,
            }
            save_state(state)
            log(f"Uploaded {name}")
        except (BotoCoreError, ClientError) as e:
            log(f"Upload failed for {name}: {e}")
        except Exception as e:
            log(f"Upload unexpected error for {name}: {e}")


def main() -> None:
    opts = load_options()
    bucket = opts.get("bucket", "")
    prefix = opts.get("prefix", "")
    region = opts.get("region", "eu-west-1")
    access_key = opts.get("aws_access_key_id", "")
    secret_key = opts.get("aws_secret_access_key", "")
    poll_interval = int(opts.get("poll_interval_seconds", 60))
    freq_hours = int(opts.get("backup_freq_hours", 24))

    if not bucket or not prefix or not access_key or not secret_key:
        log("WARN: addon options incomplete (missing bucket/prefix/credentials). "
            "Waiting — finalize.ts should be setting these shortly.")
        while not (bucket and prefix and access_key and secret_key):
            time.sleep(poll_interval)
            opts = load_options()
            bucket = opts.get("bucket", "")
            prefix = opts.get("prefix", "")
            access_key = opts.get("aws_access_key_id", "")
            secret_key = opts.get("aws_secret_access_key", "")

    log(f"Starting: bucket={bucket} prefix={prefix} region={region} "
        f"poll={poll_interval}s freq={freq_hours}h")

    s3 = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )

    state = load_state()

    while True:
        try:
            trigger_backup_if_due(state, freq_hours)
            sync_uploads(state, s3, bucket, prefix)
        except Exception as e:
            log(f"Main loop unexpected error: {e}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
