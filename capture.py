from typing import Optional, List, Dict, Tuple
"""
capture.py
----------
Runs post-patch and post-reboot command captures on EC2 instances
via SSM Run Command, uploads raw JSON + HTML reports to S3.
"""

from datetime import datetime, timezone

from config import (
    STANDARD_COMMANDS,
    SERVICE_CHECK_COMMAND_TEMPLATE,
    SERVICE_NAME_MAP,
    S3_PREFIX,
)
from ssm_utils import run_commands_on_instance
from s3_utils  import upload_json, upload_html


# ── Helpers ───────────────────────────────────────────────────

def utc_now_str(fmt: str = "%Y-%m-%dT%H-%M-%SZ") -> str:
    return datetime.now(timezone.utc).strftime(fmt)

def utc_now_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def resolve_service_name(app_service_tag: str) -> Optional[str]:
    """Maps AppService tag value to the systemctl service name."""
    if not app_service_tag:
        return None
    return SERVICE_NAME_MAP.get(app_service_tag, app_service_tag)


def build_commands(instance_info: dict) -> List[tuple]:
    """
    Returns the full (label, command) list for an instance.
    Appends a service-specific check if AppService tag is set.
    """
    commands     = list(STANDARD_COMMANDS)
    service_name = resolve_service_name(instance_info["app_service"])

    if service_name:
        cmd = SERVICE_CHECK_COMMAND_TEMPLATE % service_name
        commands.append((f"service_status_{service_name}", cmd))
        print(f"    [SERVICE] Will check: {service_name}")
    else:
        print(f"    [SERVICE] No AppService tag — skipping service check")

    return commands


# ── Core capture ─────────────────────────────────────────────

def capture_instance(ssm_client, instance_info: dict,
                     phase: str, timestamp: str) -> dict:
    """
    Runs all commands on a single instance and returns structured results.
    """
    instance_id = instance_info["instance_id"]
    name        = instance_info["name"]

    print(f"\n  [CAPTURE] {instance_id} ({name}) — phase={phase}")
    commands = build_commands(instance_info)
    cmd_results = run_commands_on_instance(ssm_client, instance_id, commands)

    return {
        "instance_id"       : instance_id,
        "instance_name"     : name,
        "private_ip"        : instance_info["private_ip"],
        "app_service"       : instance_info["app_service"],
        "phase"             : phase,
        "capture_timestamp" : timestamp,
        "commands"          : cmd_results,
    }


def run_capture(ssm_client, s3_client, instances: List[dict],
                phase: str, timestamp: str) -> Tuple[List[dict], str]:
    """
    Runs capture for all instances, uploads JSON + HTML to S3.
    Returns (all_results, summary_s3_key).
    """
    all_results = []
    date_str    = utc_now_date()

    for instance_info in instances:
        result = capture_instance(ssm_client, instance_info, phase, timestamp)
        all_results.append(result)

        # Upload raw JSON
        upload_json(s3_client, result, instance_info["instance_id"],
                    phase, timestamp)

        # Upload per-instance HTML
        instance_html = generate_instance_html(result)
        html_key      = (f"{S3_PREFIX}/{date_str}/{instance_info['instance_id']}/"
                         f"{phase}_{timestamp}_report.html")
        upload_html(s3_client, instance_html, html_key)

    # Upload summary HTML
    summary_html = generate_summary_html(all_results, phase, timestamp)
    summary_key  = f"{S3_PREFIX}/{date_str}/summary_{phase}_{timestamp}.html"
    upload_html(s3_client, summary_html, summary_key)

    print(f"\n[CAPTURE] {phase} complete — "
          f"{len(all_results)} instance(s), "
          f"summary: s3://new-relic-file-upload/{summary_key}")

    return all_results, summary_key


# ── HTML: per-instance report ─────────────────────────────────

def generate_instance_html(instance_data: dict) -> str:
    instance_id   = instance_data["instance_id"]
    instance_name = instance_data["instance_name"]
    private_ip    = instance_data["private_ip"]
    app_service   = instance_data["app_service"] or "N/A"
    phase         = instance_data["phase"]
    timestamp     = instance_data["capture_timestamp"]

    command_sections = ""
    for cmd in instance_data["commands"]:
        status_class = "success" if cmd["status"] == "Success" else "failure"
        stdout_html  = cmd["stdout"].replace("<","&lt;").replace(">","&gt;") or "(no output)"
        stderr_html  = cmd["stderr"].replace("<","&lt;").replace(">","&gt;")

        stderr_block = ""
        if stderr_html:
            stderr_block = f"""
            <div class="stderr-block">
              <strong>STDERR:</strong><pre>{stderr_html}</pre>
            </div>"""

        command_sections += f"""
        <div class="command-block">
          <div class="command-header {status_class}">
            <span class="cmd-label">{cmd['label']}</span>
            <span class="cmd-status">{cmd['status']} (exit: {cmd['exit_code']})</span>
          </div>
          <div class="command-meta"><code>$ {cmd['command']}</code></div>
          <div class="command-output"><pre>{stdout_html}</pre></div>
          {stderr_block}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Patch Capture | {instance_name} | {phase}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;padding:24px;line-height:1.5}}
    .header{{background:linear-gradient(135deg,#1a1f2e,#252b3b);border:1px solid #F26522;border-radius:8px;padding:20px 24px;margin-bottom:24px}}
    .header h1{{color:#F26522;font-size:22px;margin-bottom:12px}}
    .meta-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:12px}}
    .meta-item{{background:#1a1f2e;border-radius:6px;padding:10px 14px;border-left:3px solid #F26522}}
    .meta-item .label{{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px}}
    .meta-item .value{{font-size:14px;color:#fff;font-weight:600;margin-top:2px}}
    .phase-badge{{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:700;text-transform:uppercase}}
    .phase-post{{background:#1a3a2a;color:#66bb6a}}
    .phase-post-reboot{{background:#3a1a3a;color:#ce93d8}}
    .command-block{{background:#1a1f2e;border-radius:8px;margin-bottom:16px;overflow:hidden;border:1px solid #2a2f3e}}
    .command-header{{display:flex;justify-content:space-between;align-items:center;padding:10px 16px}}
    .command-header.success{{background:#0d2b1a;border-left:4px solid #4caf50}}
    .command-header.failure{{background:#2b0d0d;border-left:4px solid #f44336}}
    .cmd-label{{font-weight:700;color:#fff;font-size:14px}}
    .cmd-status{{font-size:12px;color:#aaa}}
    .command-meta{{padding:8px 16px;background:#111520;border-bottom:1px solid #2a2f3e}}
    .command-meta code{{font-family:'Courier New',monospace;font-size:12px;color:#F26522}}
    .command-output{{padding:12px 16px}}
    .command-output pre{{font-family:'Courier New',monospace;font-size:12px;white-space:pre-wrap;word-break:break-all;color:#c8d0e0;line-height:1.6}}
    .stderr-block{{padding:8px 16px;background:#2b1515;border-top:1px solid #5a1a1a;font-size:12px;color:#f48}}
    .stderr-block pre{{font-family:'Courier New',monospace;color:#f48}}
    .footer{{text-align:center;margin-top:32px;color:#444;font-size:12px}}
  </style>
</head>
<body>
  <div class="header">
    <h1>Patch Validation Capture Report</h1>
    <div class="meta-grid">
      <div class="meta-item"><div class="label">Instance Name</div><div class="value">{instance_name}</div></div>
      <div class="meta-item"><div class="label">Instance ID</div><div class="value">{instance_id}</div></div>
      <div class="meta-item"><div class="label">Private IP</div><div class="value">{private_ip}</div></div>
      <div class="meta-item"><div class="label">App Service</div><div class="value">{app_service}</div></div>
      <div class="meta-item"><div class="label">Phase</div><div class="value"><span class="phase-badge phase-{phase.replace('-','').replace('reboot','reboot')}">{phase}</span></div></div>
      <div class="meta-item"><div class="label">Captured (UTC)</div><div class="value">{timestamp}</div></div>
    </div>
  </div>
  <div class="commands">{command_sections}</div>
  <div class="footer">Generated by Patch Automation | TTN MSP Team | {timestamp}</div>
</body>
</html>"""


# ── HTML: summary across all instances ───────────────────────

def generate_summary_html(all_results: List[dict],
                           phase: str, timestamp: str) -> str:
    rows = ""
    for r in all_results:
        total   = len(r["commands"])
        success = sum(1 for c in r["commands"] if c["status"] == "Success")
        failed  = total - success
        overall = "ALL OK" if failed == 0 else f"{failed} FAILED"
        cls     = "ok" if failed == 0 else "fail"
        rows += f"""
        <tr class="{cls}">
          <td>{r['instance_name']}</td>
          <td>{r['instance_id']}</td>
          <td>{r['private_ip']}</td>
          <td>{r['app_service'] or 'N/A'}</td>
          <td>{success}/{total}</td>
          <td class="status-cell">{overall}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Patch Summary | {phase} | {timestamp}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;padding:24px}}
    h1{{color:#F26522;margin-bottom:8px}}
    .sub{{color:#888;margin-bottom:24px;font-size:13px}}
    table{{width:100%;border-collapse:collapse;background:#1a1f2e;border-radius:8px;overflow:hidden}}
    th{{background:#F26522;color:#fff;padding:12px 16px;text-align:left;font-size:13px}}
    td{{padding:10px 16px;border-bottom:1px solid #2a2f3e;font-size:13px}}
    tr.ok td{{border-left:3px solid #4caf50}}
    tr.fail td{{border-left:3px solid #f44336}}
    .status-cell{{font-weight:700}}
    tr.ok .status-cell{{color:#4caf50}}
    tr.fail .status-cell{{color:#f44336}}
    .footer{{margin-top:24px;text-align:center;color:#444;font-size:12px}}
  </style>
</head>
<body>
  <h1>Patch Automation — Capture Summary</h1>
  <p class="sub">Phase: <strong>{phase}</strong> &nbsp;|&nbsp;
     Captured: {timestamp} UTC &nbsp;|&nbsp; Instances: {len(all_results)}</p>
  <table>
    <thead><tr>
      <th>Instance Name</th><th>Instance ID</th><th>Private IP</th>
      <th>App Service</th><th>Commands OK</th><th>Overall</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="footer">Generated by Patch Automation | TTN MSP Team | {timestamp}</div>
</body>
</html>"""
