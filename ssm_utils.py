"""
ssm_utils.py
------------
SSM Run Command execution and result collection.
All remote command execution goes through this module.
"""

import boto3
import time
from typing import Optional, List, Dict, Tuple
from botocore.exceptions import ClientError
from config import AWS_REGION, SSM_TIMEOUT_SECONDS, SSM_POLL_INTERVAL


def get_ssm_client():
    return boto3.client("ssm", region_name=AWS_REGION)


def run_command(ssm_client, instance_id: str,
                label: str, command: str) -> dict:
    """
    Executes a single shell command on an EC2 instance via SSM Run Command.
    Polls until completion or timeout.

    Returns:
    {
        label    : str,
        command  : str,
        status   : str,   # Success | Failed | TimedOut | Error
        stdout   : str,
        stderr   : str,
        exit_code: int,
    }
    """
    result = {
        "label"    : label,
        "command"  : command,
        "status"   : "Unknown",
        "stdout"   : "",
        "stderr"   : "",
        "exit_code": -1,
    }

    try:
        response   = ssm_client.send_command(
            InstanceIds  = [instance_id],
            DocumentName = "AWS-RunShellScript",
            Parameters   = {"commands": [command]},
            Comment      = f"patch-auto-{label}"[:100],
            TimeoutSeconds = SSM_TIMEOUT_SECONDS,
        )
        command_id = response["Command"]["CommandId"]

        elapsed = 0
        while elapsed < SSM_TIMEOUT_SECONDS:
            time.sleep(SSM_POLL_INTERVAL)
            elapsed += SSM_POLL_INTERVAL

            inv    = ssm_client.get_command_invocation(
                CommandId  = command_id,
                InstanceId = instance_id,
            )
            status = inv["StatusDetails"]

            if status in ("Success", "Failed", "TimedOut", "Cancelled",
                          "DeliveryTimedOut", "ExecutionTimedOut"):
                result["status"]    = status
                result["stdout"]    = inv.get("StandardOutputContent", "").strip()
                result["stderr"]    = inv.get("StandardErrorContent", "").strip()
                result["exit_code"] = inv.get("ResponseCode", -1)
                return result

        result["status"] = "TimedOut"
        result["stderr"] = f"Polling timed out after {SSM_TIMEOUT_SECONDS}s"

    except ClientError as e:
        result["status"] = "Error"
        result["stderr"] = str(e)

    return result


def run_commands_on_instance(ssm_client, instance_id: str,
                             commands: List[tuple]) -> List[dict]:
    """
    Runs a list of (label, command) tuples on an instance sequentially.
    Returns list of result dicts.
    """
    results = []
    for label, command in commands:
        print(f"    Running: {label:<30}", end="", flush=True)
        result = run_command(ssm_client, instance_id, label, command)
        icon   = "OK" if result["status"] == "Success" else "FAIL"
        print(f"[{icon}] {result['status']}")
        results.append(result)
    return results


def wait_for_ssm_reconnect(ssm_client, instance_ids: List[str],
                           timeout: int = 300, poll: int = 15) -> bool:
    """
    Waits for all instances to appear as Online in SSM after a restart.
    Returns True if all reconnected within timeout, False otherwise.
    """
    from datetime import datetime, timezone, timedelta
    deadline  = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    connected = set()

    print(f"[SSM] Waiting for SSM reconnect on {len(instance_ids)} instance(s)...")

    while datetime.now(timezone.utc) < deadline:
        time.sleep(poll)
        try:
            response = ssm_client.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": instance_ids}]
            )
            for info in response.get("InstanceInformationList", []):
                iid    = info["InstanceId"]
                status = info.get("PingStatus", "")
                if status == "Online" and iid not in connected:
                    connected.add(iid)
                    print(f"  {iid} → SSM Online")
        except ClientError as e:
            print(f"  [WARN] SSM describe error: {e}")

        if len(connected) == len(instance_ids):
            print("  All instances SSM-online.")
            return True

        remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
        print(f"  SSM online: {len(connected)}/{len(instance_ids)} "
              f"| {remaining}s remaining")

    print("[WARN] SSM reconnect timed out. Proceeding anyway.")
    return False
