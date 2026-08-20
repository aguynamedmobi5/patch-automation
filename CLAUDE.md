# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone Python CLI ("Patch Automation") used by an MSP team to validate EC2 instances before/after
OS patching + reboot cycles. It has no build system, no test suite, and no package manifest — it's 8
flat `.py` files meant to be copied together onto a bastion/jump host and run directly with `python orchestrator.py`.

All 8 files must live in the same directory (no packaging, no relative imports beyond flat `from x import y`):
`config.py`, `s3_utils.py`, `ec2_utils.py`, `ssm_utils.py`, `capture.py`, `compare.py`, `notify.py`, `orchestrator.py`.

## Running it

```
python orchestrator.py
```

There are no CLI args — it's fully interactive. On startup it prompts for one of two phases:

1. **Pre Check** (`phase="post"` internally) — run right after patching finishes, before instance
   stop/start. Captures current state to S3, emails a report link.
2. **Post Check** (`phase="post-reboot"` internally) — run after stop/start, once SSM is back online.
   Captures state again, diffs it against the Pre Check capture for the same day, emails a diff report.
   Exits with code 1 if any instance's overall diff status is `FAIL`.

Requires working AWS credentials (boto3 default credential chain) with permissions for EC2 describe,
SSM SendCommand/GetCommandInvocation, S3 PutObject/GetObject/List, and SNS Publish, scoped to the
region/bucket/topic in `config.py`.

There's no local way to dry-run this without live AWS resources — it always calls real EC2/SSM/S3/SNS APIs.

## Architecture

Pipeline, one-directional: `orchestrator.py` (menu + phase dispatch) → `ec2_utils` (discover targets) →
`capture.py` (run commands via `ssm_utils`, write results via `s3_utils`) → on Post Check only,
`compare.py` (re-read both phases' JSON from S3, diff them) → `notify.py` (email via SNS with a
presigned S3 link to the HTML report).

**Instance discovery (`ec2_utils.py`)**: finds *running* EC2 instances tagged `TAG_KEY=TAG_VALUE`
(`config.py`, default `patching=true`). Also reads the `AppService` tag per instance — this drives
which systemd service gets an extra health check later.

**Capture (`capture.py`)**: for each instance, runs the fixed `STANDARD_COMMANDS` list from `config.py`
(netstat/ss, top mem/cpu processes, disk usage, fstab, running services) via SSM Run Command, plus one
extra `systemctl status <service>` command if the instance's `AppService` tag maps to something in
`SERVICE_NAME_MAP`. Results go to S3 as both raw JSON (source of truth for later diffing) and rendered
per-instance + summary HTML (human-facing report, emailed as a presigned link).

**S3 layout** (`S3_PREFIX = patch-automation`, bucket/prefix in `config.py`):
```
patch-automation/<YYYY-MM-DD>/<instance-id>/post_<timestamp>.json
patch-automation/<YYYY-MM-DD>/<instance-id>/post-reboot_<timestamp>.json
patch-automation/<YYYY-MM-DD>/<instance-id>/<phase>_<timestamp>_report.html
patch-automation/<YYYY-MM-DD>/<instance-id>/diff_report_<date>.html
patch-automation/<YYYY-MM-DD>/summary_<phase>_<timestamp>.html
patch-automation/<YYYY-MM-DD>/diff_summary_<date>.html
```
The date (`YYYY-MM-DD`, UTC) is the join key between phases — `compare.py` finds the latest `post_*.json`
and `post-reboot_*.json` per instance folder under the *same day's* prefix. Running Pre Check and Post
Check on different UTC dates means Post Check finds nothing to compare against.

**Comparison (`compare.py`)**: each category (ports, services, disk, fstab, top processes, app service
health) has a dedicated `_parse_*` (turn raw SSM stdout into a set/dict) and `_compare_*` (diff
pre vs. post, assign severity) function. Severity rolls up per-instance via `overall_status()`:
`FAIL` if any category FAILs, else `WARN` if any WARNs, else `PASS`. Notable severity rules living in
`config.py` / `compare.py`, not obvious from output alone:
- A stopped/missing systemd service post-reboot that was running post-patch → `FAIL` (not just WARN).
- fstab additions/removals → always `FAIL` (mount table changes are treated as high-risk).
- Disk usage delta ≥ `DISK_WARN_THRESHOLD_PCT` (10 pts) → `WARN`, or `FAIL` if it crosses 90% used.
- New/changed listening ports and top-10 process churn → `WARN` only.
- Process comparison ignores anything matching `PROCESS_NOISE_FILTERS` (SSM agent workers, kernel
  threads, etc.) to avoid flagging transient system noise as a real change.

**Adding a new comparison category**: add a `_parse_x` + `_compare_x` pair in `compare.py`, wire it into
the dict returned by `compare_phases()`, and add a corresponding `_section(...)` call in
`generate_diff_html()` if it should appear in the HTML report.

**Adding a new captured command**: add a `(label, shell_command)` tuple to `STANDARD_COMMANDS` in
`config.py`. If it needs a diff/comparison, also add parsing logic in `compare.py` — capture alone
won't produce diff findings.

**Adding a new app service to check**: add an entry to `SERVICE_NAME_MAP` in `config.py` mapping the
`AppService` tag value to the actual systemd unit name.

**SSM execution (`ssm_utils.py`)**: `run_command` blocks synchronously, polling
`get_command_invocation` every `SSM_POLL_INTERVAL`s up to `SSM_TIMEOUT_SECONDS` — commands run
sequentially per instance, not in parallel, so total capture time scales with
`instances × commands × poll_interval`. `wait_for_ssm_reconnect` exists but is not currently called
from `orchestrator.py`'s Post Check flow — the operator is expected to manually confirm SSM is back
online before selecting Post Check.

**Notifications (`notify.py`)**: every email is a plain-text SNS publish (not HTML — the HTML report
lives in S3 and is linked via a 24h presigned URL). Errors during the main run trigger
`send_error_alert` from the top-level exception handler in `orchestrator.py`.

## Configuration

All environment-specific values (region, tag filters, S3 bucket/prefix, SNS topic ARN, thresholds,
command list, service name mapping) live in `config.py` — no other module should hardcode these.
`S3_BUCKET_REGION` is set explicitly and used to construct the S3 client so the app never needs
`s3:GetBucketLocation` IAM permission; if the bucket moves regions, update it there.
