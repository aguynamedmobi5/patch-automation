"""
orchestrator.py
---------------
Patch Automation — Main Entry Point

Presents an interactive menu at startup:
  1. Pre Check  — post-patch capture (run after patching, before stop/start)
  2. Post Check — post-reboot capture + diff comparison (run after stop/start)

All 8 files must be in the same directory:
  config.py  s3_utils.py  ec2_utils.py  ssm_utils.py
  capture.py compare.py   notify.py     orchestrator.py

Author : Patch Automation — TTN MSP Team
"""

import sys
import traceback
from typing import Optional, List
from datetime import datetime, timezone

from config    import AWS_REGION, TAG_KEY, PATCH_PHASES, S3_BUCKET
from s3_utils  import get_s3_client
from ec2_utils import discover_instances, get_ec2_client
from ssm_utils import get_ssm_client
from capture   import run_capture, utc_now_str, utc_now_date
from compare   import run_comparison
from notify    import get_sns_client, send_capture_complete, \
                       send_diff_complete, send_error_alert


# ── Helpers ───────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")

def divider(title: str = ""):
    line = "=" * 56
    print(f"\n{line}")
    if title:
        print(f" {title}")
        print(line)


# ── Interactive menu ──────────────────────────────────────────

def show_menu() -> str:
    """
    Displays the startup menu and returns the selected phase:
      '1' → 'post'         (Pre Check)
      '2' → 'post-reboot'  (Post Check)
    Loops until a valid choice is made.
    """
    divider("PATCH AUTOMATION — TTN MSP Team")
    print(f"""
  Region     : {AWS_REGION}
  Tag Key    : {TAG_KEY} (phase selected next)
  S3 Bucket  : {S3_BUCKET}
""")
    print("  Please select an option:\n")
    print("    1.  Pre Check")
    print("        Run after patching is complete.")
    print("        Captures network ports, services, disk, processes.")
    print("        Sends notification email with capture report link.")
    print()
    print("    2.  Post Check")
    print("        Run after stop/start of instances is complete.")
    print("        Captures post-reboot state and compares with Pre Check.")
    print("        Sends diff report email highlighting any changes.")
    print()

    while True:
        try:
            choice = input("  Enter your choice [1 or 2]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n  Exiting. No changes made.")
            sys.exit(0)

        if choice == "1":
            print("\n  Selected: Pre Check (post-patch capture)\n")
            return "post"
        elif choice == "2":
            print("\n  Selected: Post Check (post-reboot capture + diff comparison)\n")
            return "post-reboot"
        else:
            print("  Invalid choice. Please enter 1 or 2.\n")


def ask_patch_phase() -> str:
    """
    Prompts for which patch wave (TAG_KEY=<phase>) to target this run.
    Loops until a valid choice is made.
    """
    print(f"  Instances are tagged '{TAG_KEY}=<phase>'. Which wave are you running?\n")
    for i, phase in enumerate(PATCH_PHASES, start=1):
        print(f"    {i}.  {TAG_KEY}={phase}")
    print()

    while True:
        try:
            choice = input(f"  Enter your choice [1-{len(PATCH_PHASES)}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n  Exiting. No changes made.")
            sys.exit(0)

        if choice.isdigit() and 1 <= int(choice) <= len(PATCH_PHASES):
            phase_value = PATCH_PHASES[int(choice) - 1]
            print(f"\n  Selected: {TAG_KEY}={phase_value}\n")
            return phase_value
        else:
            print(f"  Invalid choice. Please enter 1-{len(PATCH_PHASES)}.\n")


# ── Phase runners ─────────────────────────────────────────────

def phase_pre_check(instances, ssm_client, s3_client, sns_client, timestamp):
    """
    Pre Check — post-patch capture.
    Run after patching is done, before stop/start.
    """
    divider("PRE CHECK: Post-Patch Capture")
    log("Running post-patch capture on all instances...")

    results, summary_key = run_capture(
        ssm_client, s3_client, instances, "post", timestamp
    )

    total  = sum(len(r["commands"]) for r in results)
    ok     = sum(1 for r in results
                 for c in r["commands"] if c["status"] == "Success")
    failed = total - ok

    log(f"Pre Check complete — {len(results)} instance(s), "
        f"{ok}/{total} commands OK"
        + (f", {failed} FAILED" if failed else ""))

    send_capture_complete(
        sns_client, "post", utc_now_date(),
        instances, summary_key, results
    )

    divider("PRE CHECK COMPLETE")
    print("""
  Post-patch capture uploaded to S3.
  A notification email has been sent to the team.

  NEXT STEPS:
    1. Review the capture report from the email link
    2. Perform stop/start on the instances
    3. Once instances are back up and SSM is online,
       run this script again and select option 2 (Post Check)
    """)

    return results, summary_key


def phase_post_check(instances, ssm_client, s3_client,
                     sns_client, timestamp, date_str):
    """
    Post Check — post-reboot capture + diff comparison.
    Run after stop/start is complete and SSM is online.
    """
    # Step 1: Post-reboot capture
    divider("POST CHECK — Step 1/2: Post-Reboot Capture")
    log("Running post-reboot capture on all instances...")

    results, summary_key = run_capture(
        ssm_client, s3_client, instances, "post-reboot", timestamp
    )

    total  = sum(len(r["commands"]) for r in results)
    ok     = sum(1 for r in results
                 for c in r["commands"] if c["status"] == "Success")
    failed = total - ok

    log(f"Post-reboot capture complete — {len(results)} instance(s), "
        f"{ok}/{total} commands OK"
        + (f", {failed} FAILED" if failed else ""))

    send_capture_complete(
        sns_client, "post-reboot", date_str,
        instances, summary_key, results
    )

    # Step 2: Diff comparison
    divider("POST CHECK — Step 2/2: Diff Comparison")
    log("Comparing Pre Check vs Post Check across all instances...")

    all_results, diff_summary_key = run_comparison(s3_client, date_str)

    if not all_results:
        log("No comparison data found. Ensure Pre Check was run first.", "WARN")
        log("If Pre Check was run on a different date, the data may not match.", "WARN")
        return [], None

    send_diff_complete(sns_client, date_str, all_results, diff_summary_key)

    return all_results, diff_summary_key


# ── Main ──────────────────────────────────────────────────────

def main():
    # Show interactive menu — no CLI args needed
    phase       = show_menu()
    patch_phase = ask_patch_phase()
    date_str    = utc_now_date()
    timestamp   = utc_now_str()

    print(f"  Date      : {date_str}")
    print(f"  Timestamp : {timestamp}\n")

    # Init AWS clients
    ec2_client = get_ec2_client()
    ssm_client = get_ssm_client()
    s3_client  = get_s3_client(S3_BUCKET)
    sns_client = get_sns_client()

    try:
        # Discover instances for the selected patch wave
        instances = discover_instances(ec2_client, TAG_KEY, patch_phase)
        if not instances:
            log("No instances found matching the tag filter. Exiting.", "ERROR")
            sys.exit(0)

        if phase == "post":
            # ── Pre Check ────────────────────────────────────
            phase_pre_check(
                instances, ssm_client, s3_client, sns_client, timestamp
            )

        elif phase == "post-reboot":
            # ── Post Check ───────────────────────────────────
            all_results, diff_summary_key = phase_post_check(
                instances, ssm_client, s3_client,
                sns_client, timestamp, date_str
            )

            divider("POST CHECK COMPLETE")
            if all_results:
                for r in all_results:
                    status_icon = {
                        "PASS": "✔",
                        "WARN": "⚠",
                        "FAIL": "✘",
                    }.get(r["overall_status"], "?")
                    print(f"  {status_icon}  {r['instance_name']:<30} "
                          f"{r['overall_status']}")
                if diff_summary_key:
                    print(f"\n  Diff summary : s3://{S3_BUCKET}/{diff_summary_key}")
                print("\n  Diff report email sent to the team.\n")
                print("=" * 56)

                if any(r["overall_status"] == "FAIL" for r in all_results):
                    log("One or more instances have FAIL status — "
                        "review the diff report.", "WARN")
                    sys.exit(1)

    except KeyboardInterrupt:
        print("\n\n  Interrupted. No instances were modified.")
        sys.exit(0)

    except Exception as e:
        log(f"Unexpected error: {e}", "ERROR")
        traceback.print_exc()
        try:
            send_error_alert(sns_client, date_str, traceback.format_exc())
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
