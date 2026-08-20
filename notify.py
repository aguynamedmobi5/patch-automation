"""
notify.py
---------
SNS email notification helpers.
Sends capture completion and final diff result emails.
"""

import boto3
from typing import List
from datetime import datetime, timezone
from botocore.exceptions import ClientError

from config   import AWS_REGION, SNS_TOPIC_ARN, S3_BUCKET
from s3_utils import generate_presigned_get_url, get_s3_client


def get_sns_client():
    return boto3.client("sns", region_name=AWS_REGION)


def _publish(sns_client, subject: str, message: str):
    """Publishes a message to the SNS topic. Logs errors without raising."""
    try:
        sns_client.publish(
            TopicArn = SNS_TOPIC_ARN,
            Subject  = subject[:100],   # SNS subject limit
            Message  = message,
        )
        print(f"[SNS] Email sent: {subject}")
    except ClientError as e:
        print(f"[SNS][ERROR] Failed to send email: {e}")


def send_capture_complete(sns_client, phase: str, date_str: str,
                           instances: List[dict], summary_key: str,
                           all_results: List[dict]):
    """
    Sends a notification after a capture phase completes.
    Includes summary stats and a pre-signed link to the HTML report.
    """
    s3_client   = get_s3_client()
    summary_url = generate_presigned_get_url(s3_client, summary_key)

    total_cmds = sum(len(r["commands"]) for r in all_results)
    ok_cmds    = sum(
        sum(1 for c in r["commands"] if c["status"] == "Success")
        for r in all_results
    )
    failed_cmds = total_cmds - ok_cmds

    instance_lines = "\n".join(
        f"  • {i['name']} ({i['instance_id']}) | {i['private_ip']} "
        f"| Service: {i['app_service'] or 'N/A'}"
        for i in instances
    )

    phase_label = {
        "post"        : "Post-Patch",
        "post-reboot" : "Post-Reboot",
    }.get(phase, phase.title())

    status_line = "ALL COMMANDS SUCCEEDED" if failed_cmds == 0 \
                  else f"{failed_cmds} COMMAND(S) FAILED — review report"

    subject = (f"[Patch Automation] {phase_label} Capture Complete "
               f"| {date_str} | {status_line}")

    message = f"""
Patch Automation — {phase_label} Capture Complete
{'=' * 52}
Date       : {date_str}
Phase      : {phase_label}
Region     : {AWS_REGION}
Timestamp  : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC

INSTANCES CAPTURED
------------------
{instance_lines}

RESULTS
-------
  Total commands  : {total_cmds}
  Successful      : {ok_cmds}
  Failed          : {failed_cmds}
  Overall         : {status_line}

CAPTURE REPORT (valid 24 hours)
{summary_url}

{'⚠  ACTION REQUIRED: ' + str(failed_cmds) + ' command(s) failed. Review the report above.' if failed_cmds > 0 else '✔  No action required for this phase.'}

---
This is an automated message from the Patch Automation system.
TTN MSP Team | {AWS_REGION}
    """.strip()

    _publish(sns_client, subject, message)


def send_diff_complete(sns_client, date_str: str,
                        all_results: List[dict],
                        diff_summary_key: str):
    """
    Sends the final diff comparison result email after post-reboot capture
    and comparison are both complete.
    """
    s3_client   = get_s3_client()
    summary_url = generate_presigned_get_url(s3_client, diff_summary_key)

    overall_fail = any(r["overall_status"] == "FAIL" for r in all_results)
    overall_warn = any(r["overall_status"] == "WARN" for r in all_results)

    if overall_fail:
        top_status = "FAIL — Immediate Review Required"
    elif overall_warn:
        top_status = "WARN — Minor Changes Detected"
    else:
        top_status = "PASS — All Clear"

    result_lines = ""
    for r in all_results:
        result_lines += (f"  • {r['instance_name']} ({r['instance_id']}) "
                         f"→ {r['overall_status']}\n")
        for cat, finding in r["findings"].items():
            for f in finding["findings"]:
                if f["severity"] in ("FAIL", "WARN"):
                    result_lines += f"      [{f['severity']}] {f['message']}\n"

    subject = (f"[Patch Automation] Diff Report | {top_status} | {date_str}")

    message = f"""
Patch Automation — Post-Patch vs Post-Reboot Diff Complete
{'=' * 58}
Date       : {date_str}
Region     : {AWS_REGION}
Status     : {top_status}
Generated  : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC

INSTANCE RESULTS
----------------
{result_lines}
DIFF SUMMARY REPORT (valid 24 hours)
{summary_url}

---
This is an automated message from the Patch Automation system.
TTN MSP Team | {AWS_REGION}
    """.strip()

    _publish(sns_client, subject, message)


def send_error_alert(sns_client, date_str: str, error_msg: str):
    """Sends an alert email when the orchestrator hits an unexpected error."""
    subject = f"[Patch Automation] ERROR | {date_str}"
    message = f"""
Patch Automation — Unexpected Error
=====================================
Date    : {date_str}
Region  : {AWS_REGION}
Time    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC

ERROR DETAILS
-------------
{error_msg}

Please check the server running the patch_automation orchestrator
and review logs for the full traceback.

---
TTN MSP Team | {AWS_REGION}
    """.strip()
    _publish(sns_client, subject, message)
