from typing import Optional, List, Dict, Tuple
"""
compare.py
----------
Compares post-patch vs post-reboot command outputs.
Detects meaningful differences and generates colour-coded diff reports.
"""

from datetime import datetime, timezone
from config   import (
    DISK_WARN_THRESHOLD_PCT,
    PROCESS_NOISE_FILTERS,
    S3_PREFIX,
)
from s3_utils import (
    list_instance_folders,
    find_latest_json,
    download_json,
    upload_html,
)

# ── Colours ───────────────────────────────────────────────────
SEV_COLOUR = {"OK": "#4caf50", "WARN": "#ff9800", "FAIL": "#f44336", "PASS": "#4caf50"}
SEV_BG     = {"OK": "#0d2b1a", "WARN": "#2b1f0a", "FAIL": "#2b0d0d", "PASS": "#0d2b1a"}


# ── Parsers ───────────────────────────────────────────────────

def _get_output(phase_data: dict, label_substr: str) -> str:
    for cmd in phase_data.get("commands", []):
        if label_substr in cmd["label"] and cmd["status"] == "Success":
            return cmd["stdout"]
    return ""


def _parse_ports(output: str) -> set:
    ports = set()
    for line in output.splitlines():
        if "LISTEN" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[0].lower().replace("6","").replace("4","")
        local = parts[3].replace("::ffff:", "")
        ports.add(f"{proto}:{local}")
    return ports


def _parse_services(output: str) -> set:
    services = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("UNIT"):
            continue
        parts = line.split()
        if parts and parts[0].endswith(".service"):
            services.add(parts[0])
    return services


def _parse_disk(output: str) -> dict:
    disks = {}
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            use_pct = parts[5].replace("%", "")
            disks[parts[6]] = {
                "filesystem": parts[0], "fstype": parts[1],
                "size": parts[2], "used": parts[3], "avail": parts[4],
                "use_pct": int(use_pct) if use_pct.isdigit() else 0,
            }
        except (IndexError, ValueError):
            continue
    return disks


def _parse_fstab(output: str) -> list:
    return [l.strip() for l in output.splitlines()
            if l.strip() and not l.strip().startswith("#")]


def _parse_processes(output: str) -> list:
    procs = []
    for line in output.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        cmd = parts[10].strip()[:60]
        if not any(n in cmd for n in PROCESS_NOISE_FILTERS):
            procs.append(cmd)
    return procs


# ── Comparators ───────────────────────────────────────────────

def _compare_ports(post: dict, reboot: dict) -> dict:
    r = {"status": "OK", "findings": []}
    post_p   = _parse_ports(_get_output(post,  "network_ports"))
    reboot_p = _parse_ports(_get_output(reboot,"network_ports"))

    for p in sorted(reboot_p - post_p):
        r["status"] = "WARN"
        r["findings"].append({"severity":"WARN","message":f"New port listening post-reboot: {p}"})
    for p in sorted(post_p - reboot_p):
        r["status"] = "WARN"
        r["findings"].append({"severity":"WARN","message":f"Port no longer listening post-reboot: {p}"})
    if not r["findings"]:
        r["findings"].append({"severity":"OK","message":"No port changes between post-patch and post-reboot"})

    r["post_ports"]   = sorted(post_p)
    r["reboot_ports"] = sorted(reboot_p)
    return r


def _compare_services(post: dict, reboot: dict) -> dict:
    r = {"status": "OK", "findings": []}
    post_s   = _parse_services(_get_output(post,  "running_services"))
    reboot_s = _parse_services(_get_output(reboot,"running_services"))

    for s in sorted(post_s - reboot_s):
        r["status"] = "FAIL"
        r["findings"].append({"severity":"FAIL","message":f"Service running post-patch but STOPPED post-reboot: {s}"})
    for s in sorted(reboot_s - post_s):
        if r["status"] != "FAIL": r["status"] = "WARN"
        r["findings"].append({"severity":"WARN","message":f"New service appeared post-reboot: {s}"})
    if not r["findings"]:
        r["findings"].append({"severity":"OK","message":"All services consistent between post-patch and post-reboot"})

    r["post_services"]   = sorted(post_s)
    r["reboot_services"] = sorted(reboot_s)
    return r


def _compare_disk(post: dict, reboot: dict) -> dict:
    r = {"status": "OK", "findings": []}
    post_d   = _parse_disk(_get_output(post,  "disk_usage"))
    reboot_d = _parse_disk(_get_output(reboot,"disk_usage"))

    for mount, info in post_d.items():
        if mount not in reboot_d:
            continue
        pre_pct  = info["use_pct"]
        post_pct = reboot_d[mount]["use_pct"]
        delta    = post_pct - pre_pct

        if abs(delta) >= DISK_WARN_THRESHOLD_PCT:
            sev = "FAIL" if post_pct >= 90 else "WARN"
            if r["status"] != "FAIL": r["status"] = sev
            direction = "increased" if delta > 0 else "decreased"
            r["findings"].append({"severity":sev,
                "message":f"Disk {direction} on {mount}: {pre_pct}% → {post_pct}% (Δ{delta:+d}%)"})
        if post_pct >= 90:
            r["status"] = "FAIL"
            r["findings"].append({"severity":"FAIL",
                "message":f"CRITICAL: {mount} at {post_pct}% usage post-reboot"})

    if not r["findings"]:
        r["findings"].append({"severity":"OK",
            "message":f"No significant disk changes (threshold >{DISK_WARN_THRESHOLD_PCT}%)"})

    r["post_disk"]   = post_d
    r["reboot_disk"] = reboot_d
    return r


def _compare_fstab(post: dict, reboot: dict) -> dict:
    r = {"status": "OK", "findings": []}
    post_f   = _parse_fstab(_get_output(post,  "fstab"))
    reboot_f = _parse_fstab(_get_output(reboot,"fstab"))
    post_set   = set(post_f)
    reboot_set = set(reboot_f)

    for line in sorted(reboot_set - post_set):
        r["status"] = "FAIL"
        r["findings"].append({"severity":"FAIL","message":f"fstab ENTRY ADDED post-reboot: {line}"})
    for line in sorted(post_set - reboot_set):
        r["status"] = "FAIL"
        r["findings"].append({"severity":"FAIL","message":f"fstab ENTRY REMOVED post-reboot: {line}"})
    if not r["findings"]:
        r["findings"].append({"severity":"OK","message":"fstab unchanged between post-patch and post-reboot"})

    r["post_fstab"]   = post_f
    r["reboot_fstab"] = reboot_f
    return r


def _compare_processes(post: dict, reboot: dict, label: str) -> dict:
    r = {"status": "OK", "findings": []}
    post_p   = set(_parse_processes(_get_output(post,  label)))
    reboot_p = set(_parse_processes(_get_output(reboot,label)))

    for p in sorted(reboot_p - post_p):
        if r["status"] != "FAIL": r["status"] = "WARN"
        r["findings"].append({"severity":"WARN","message":f"New process in top-10 post-reboot: {p[:80]}"})
    for p in sorted(post_p - reboot_p):
        if r["status"] != "FAIL": r["status"] = "WARN"
        r["findings"].append({"severity":"WARN","message":f"Process no longer in top-10 post-reboot: {p[:80]}"})
    if not r["findings"]:
        r["findings"].append({"severity":"OK","message":"Top process list consistent"})

    return r


def _compare_service_status(post: dict, reboot: dict) -> dict:
    r = {"status": "OK", "findings": []}
    post_out   = _get_output(post,  "service_status")
    reboot_out = _get_output(reboot,"service_status")

    if not post_out and not reboot_out:
        r["findings"].append({"severity":"OK","message":"No AppService tag — service check not applicable"})
        return r

    post_up   = "active (running)" in post_out.lower()
    reboot_up = "active (running)" in reboot_out.lower()

    if post_up and reboot_up:
        r["findings"].append({"severity":"OK","message":"App service running in both post-patch and post-reboot"})
    elif post_up and not reboot_up:
        r["status"] = "FAIL"
        r["findings"].append({"severity":"FAIL","message":"CRITICAL: App service running post-patch but NOT running post-reboot"})
    elif not post_up and reboot_up:
        r["status"] = "WARN"
        r["findings"].append({"severity":"WARN","message":"App service not running post-patch but running post-reboot"})
    else:
        r["findings"].append({"severity":"OK","message":"App service not running in either phase (consistent)"})

    return r


# ── Main comparison entry point ───────────────────────────────

def compare_phases(post_data: dict, reboot_data: dict) -> dict:
    """
    Compares post-patch vs post-reboot across all categories.
    Returns structured findings dict keyed by category.
    """
    return {
        "network_ports"  : _compare_ports(post_data, reboot_data),
        "services"       : _compare_services(post_data, reboot_data),
        "disk"           : _compare_disk(post_data, reboot_data),
        "fstab"          : _compare_fstab(post_data, reboot_data),
        "top_memory"     : _compare_processes(post_data, reboot_data, "top_memory"),
        "top_cpu"        : _compare_processes(post_data, reboot_data, "top_cpu"),
        "service_status" : _compare_service_status(post_data, reboot_data),
    }


def overall_status(findings: dict) -> str:
    statuses = [v["status"] for v in findings.values()]
    if "FAIL" in statuses: return "FAIL"
    if "WARN" in statuses: return "WARN"
    return "PASS"


# ── Run comparison for all instances in S3 ───────────────────

def run_comparison(s3_client, date_str: str) -> Tuple[list[dict], Optional[str]]:
    """
    Loads post and post-reboot JSONs from S3, runs comparison,
    uploads per-instance diff HTML + summary.
    Returns (all_instance_results, summary_s3_key).
    """
    instance_ids = list_instance_folders(s3_client, date_str)
    if not instance_ids:
        print("[COMPARE] No instance folders found in S3.")
        return [], None

    all_results = []

    for instance_id in instance_ids:
        post_key   = find_latest_json(s3_client, date_str, instance_id, "post")
        reboot_key = find_latest_json(s3_client, date_str, instance_id, "post-reboot")

        if not post_key:
            print(f"  [SKIP] {instance_id} — no post-patch JSON found")
            continue
        if not reboot_key:
            print(f"  [SKIP] {instance_id} — no post-reboot JSON found")
            continue

        post_data   = download_json(s3_client, post_key)
        reboot_data = download_json(s3_client, reboot_key)

        if not post_data or not reboot_data:
            continue

        findings = compare_phases(post_data, reboot_data)
        status   = overall_status(findings)

        instance_name = post_data.get("instance_name", instance_id)
        private_ip    = post_data.get("private_ip",    "N/A")
        app_service   = post_data.get("app_service",   "")
        post_ts       = post_data.get("capture_timestamp",   "N/A")
        reboot_ts     = reboot_data.get("capture_timestamp", "N/A")

        print(f"  {instance_name} ({instance_id}) → {status}")
        for cat, result in findings.items():
            for f in result["findings"]:
                if f["severity"] in ("FAIL","WARN"):
                    print(f"    [{f['severity']}] {f['message']}")

        diff_html = generate_diff_html(
            instance_id, instance_name, private_ip, app_service,
            post_ts, reboot_ts, findings, status,
        )
        diff_key = f"{S3_PREFIX}/{date_str}/{instance_id}/diff_report_{date_str}.html"
        upload_html(s3_client, diff_html, diff_key)

        all_results.append({
            "instance_id"    : instance_id,
            "instance_name"  : instance_name,
            "private_ip"     : private_ip,
            "app_service"    : app_service,
            "overall_status" : status,
            "findings"       : findings,
            "diff_key"       : diff_key,
        })

    if not all_results:
        return [], None

    summary_html = generate_summary_diff_html(all_results, date_str)
    summary_key  = f"{S3_PREFIX}/{date_str}/diff_summary_{date_str}.html"
    upload_html(s3_client, summary_html, summary_key)
    return all_results, summary_key


# ── HTML builders ─────────────────────────────────────────────

def _finding_rows(findings_list: list) -> str:
    rows = ""
    for f in findings_list:
        c  = SEV_COLOUR.get(f["severity"], "#aaa")
        bg = SEV_BG.get(f["severity"], "#1a1f2e")
        rows += (f'<div class="finding-row" style="background:{bg};border-left:4px solid {c};">'
                 f'<span class="sev-badge" style="color:{c};">{f["severity"]}</span>'
                 f'<span class="sev-msg">{f["message"]}</span></div>')
    return rows


def _section(title: str, result: dict, extra: str = "") -> str:
    c = SEV_COLOUR.get(result["status"], "#aaa")
    return (f'<div class="section">'
            f'<div class="section-header" style="border-left:5px solid {c};">'
            f'<span class="section-title">{title}</span>'
            f'<span class="section-status" style="color:{c};">{result["status"]}</span></div>'
            f'<div class="section-body">{_finding_rows(result["findings"])}{extra}</div></div>')


def _port_table(post_ports: list, reboot_ports: list) -> str:
    if not post_ports and not reboot_ports: return ""
    all_p = sorted(set(post_ports) | set(reboot_ports))
    rows  = ""
    for p in all_p:
        in_post   = "✔" if p in post_ports   else "—"
        in_reboot = "✔" if p in reboot_ports else "—"
        changed   = in_post != in_reboot
        style     = "background:#2b1f0a;" if changed else ""
        cp        = "#4caf50" if in_post   == "✔" else "#888"
        cr        = "#4caf50" if in_reboot == "✔" else "#f44336"
        rows += (f'<tr style="{style}"><td>{p}</td>'
                 f'<td style="text-align:center;color:{cp};">{in_post}</td>'
                 f'<td style="text-align:center;color:{cr};">{in_reboot}</td></tr>')
    return (f'<table class="data-table" style="margin-top:12px;">'
            f'<thead><tr><th>Port</th><th>Post-Patch</th><th>Post-Reboot</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def _service_table(post_svcs: list, reboot_svcs: list) -> str:
    if not post_svcs and not reboot_svcs: return ""
    all_s = sorted(set(post_svcs) | set(reboot_svcs))
    rows  = ""
    for s in all_s:
        in_post   = "✔" if s in post_svcs   else "—"
        in_reboot = "✔" if s in reboot_svcs else "—"
        changed   = in_post != in_reboot
        style     = "background:#2b1f0a;" if changed else ""
        cp        = "#4caf50" if in_post   == "✔" else "#888"
        cr        = "#4caf50" if in_reboot == "✔" else "#f44336"
        rows += (f'<tr style="{style}"><td>{s}</td>'
                 f'<td style="text-align:center;color:{cp};">{in_post}</td>'
                 f'<td style="text-align:center;color:{cr};">{in_reboot}</td></tr>')
    return (f'<table class="data-table" style="margin-top:12px;max-height:300px;overflow-y:auto;display:block;">'
            f'<thead><tr><th>Service</th><th>Post-Patch</th><th>Post-Reboot</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def _disk_table(post_disk: dict, reboot_disk: dict) -> str:
    if not post_disk: return ""
    rows = ""
    for mount, info in post_disk.items():
        ri        = reboot_disk.get(mount, {})
        pre_pct   = info.get("use_pct", "N/A")
        post_pct  = ri.get("use_pct", "N/A")
        if isinstance(pre_pct, int) and isinstance(post_pct, int):
            delta = post_pct - pre_pct
            dstr  = f"{delta:+d}%"
            clr   = "#f44336" if abs(delta) >= DISK_WARN_THRESHOLD_PCT else "#4caf50"
        else:
            dstr, clr = "N/A", "#aaa"
        rows += (f'<tr><td>{mount}</td><td>{info.get("fstype","")}</td>'
                 f'<td>{info.get("size","")}</td>'
                 f'<td>{info.get("used","")} ({pre_pct}%)</td>'
                 f'<td>{ri.get("used","N/A")} ({post_pct}%)</td>'
                 f'<td style="color:{clr};font-weight:700;">{dstr}</td></tr>')
    return (f'<table class="data-table" style="margin-top:12px;">'
            f'<thead><tr><th>Mount</th><th>Type</th><th>Size</th>'
            f'<th>Used (Post-Patch)</th><th>Used (Post-Reboot)</th><th>Delta</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def generate_diff_html(instance_id: str, instance_name: str,
                        private_ip: str, app_service: str,
                        post_ts: str, reboot_ts: str,
                        findings: dict, status: str) -> str:

    oc = SEV_COLOUR.get(status, "#aaa")
    ob = SEV_BG.get(status, "#1a1f2e")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    sections = (
        _section("Network Ports",        findings["network_ports"],
                 _port_table(findings["network_ports"].get("post_ports",[]),
                             findings["network_ports"].get("reboot_ports",[])))
      + _section("Running Services",     findings["services"],
                 _service_table(findings["services"].get("post_services",[]),
                                findings["services"].get("reboot_services",[])))
      + _section("Disk Usage",           findings["disk"],
                 _disk_table(findings["disk"].get("post_disk",{}),
                             findings["disk"].get("reboot_disk",{})))
      + _section("fstab Integrity",      findings["fstab"])
      + _section("Top Memory Processes", findings["top_memory"])
      + _section("Top CPU Processes",    findings["top_cpu"])
      + _section("App Service Health",   findings["service_status"])
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Patch Diff | {instance_name} | {status}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;padding:24px;line-height:1.5}}
    .header{{background:linear-gradient(135deg,#1a1f2e,#252b3b);border:1px solid #F26522;border-radius:8px;padding:20px 24px;margin-bottom:24px}}
    .header h1{{color:#F26522;font-size:22px;margin-bottom:12px}}
    .overall{{display:inline-block;padding:8px 20px;border-radius:6px;font-size:18px;font-weight:800;letter-spacing:2px;background:{ob};color:{oc};border:2px solid {oc};margin-bottom:16px}}
    .meta-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:12px}}
    .meta-item{{background:#111520;border-radius:6px;padding:10px 14px;border-left:3px solid #F26522}}
    .meta-item .label{{font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.5px}}
    .meta-item .value{{font-size:13px;color:#fff;font-weight:600;margin-top:2px}}
    .section{{background:#1a1f2e;border-radius:8px;margin-bottom:16px;overflow:hidden;border:1px solid #2a2f3e}}
    .section-header{{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:#111520}}
    .section-title{{font-weight:700;font-size:14px;color:#fff}}
    .section-status{{font-size:13px;font-weight:700}}
    .section-body{{padding:12px 16px}}
    .finding-row{{display:flex;align-items:flex-start;gap:10px;padding:8px 12px;border-radius:4px;margin-bottom:6px;font-size:13px}}
    .sev-badge{{font-weight:800;font-size:11px;min-width:40px;letter-spacing:.5px}}
    .sev-msg{{color:#c8d0e0}}
    .data-table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}}
    .data-table th{{background:#F26522;color:#fff;padding:7px 10px;text-align:left}}
    .data-table td{{padding:6px 10px;border-bottom:1px solid #2a2f3e;font-family:'Courier New',monospace}}
    .footer{{text-align:center;margin-top:32px;color:#444;font-size:12px}}
  </style>
</head>
<body>
  <div class="header">
    <h1>Patch Validation — Diff Report</h1>
    <div class="overall">OVERALL: {status}</div>
    <div class="meta-grid">
      <div class="meta-item"><div class="label">Instance Name</div><div class="value">{instance_name}</div></div>
      <div class="meta-item"><div class="label">Instance ID</div><div class="value">{instance_id}</div></div>
      <div class="meta-item"><div class="label">Private IP</div><div class="value">{private_ip}</div></div>
      <div class="meta-item"><div class="label">App Service</div><div class="value">{app_service or 'N/A'}</div></div>
      <div class="meta-item"><div class="label">Post-Patch Capture</div><div class="value">{post_ts}</div></div>
      <div class="meta-item"><div class="label">Post-Reboot Capture</div><div class="value">{reboot_ts}</div></div>
      <div class="meta-item"><div class="label">Report Generated</div><div class="value">{ts} UTC</div></div>
    </div>
  </div>
  {sections}
  <div class="footer">Generated by Patch Automation | TTN MSP Team | {ts} UTC</div>
</body>
</html>"""


def generate_summary_diff_html(results: List[dict], date_str: str) -> str:
    rows = ""
    for r in results:
        s  = r["overall_status"]
        c  = SEV_COLOUR.get(s, "#aaa")
        bg = SEV_BG.get(s, "#1a1f2e")

        issues = []
        for cat, finding in r["findings"].items():
            for f in finding["findings"]:
                if f["severity"] in ("FAIL","WARN"):
                    issues.append(f'<span style="color:{SEV_COLOUR[f["severity"]]};">'
                                  f'⚠ {f["message"]}</span>')

        issues_html = "<br>".join(issues[:3]) or '<span style="color:#4caf50;">✔ No issues</span>'
        if len(issues) > 3:
            issues_html += f'<br><span style="color:#888;">...and {len(issues)-3} more</span>'

        rows += (f'<tr><td>{r["instance_name"]}</td><td>{r["instance_id"]}</td>'
                 f'<td>{r["private_ip"]}</td><td>{r["app_service"] or "N/A"}</td>'
                 f'<td style="background:{bg};color:{c};font-weight:800;text-align:center;">{s}</td>'
                 f'<td style="font-size:12px;">{issues_html}</td></tr>')

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Patch Diff Summary | {date_str}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;padding:24px}}
    h1{{color:#F26522;margin-bottom:6px}}
    .sub{{color:#888;margin-bottom:24px;font-size:13px}}
    table{{width:100%;border-collapse:collapse;background:#1a1f2e;border-radius:8px;overflow:hidden}}
    th{{background:#F26522;color:#fff;padding:12px 14px;text-align:left;font-size:13px}}
    td{{padding:10px 14px;border-bottom:1px solid #2a2f3e;font-size:13px}}
    .footer{{margin-top:24px;text-align:center;color:#444;font-size:12px}}
  </style>
</head>
<body>
  <h1>Patch Automation — Diff Summary</h1>
  <p class="sub">Date: <strong>{date_str}</strong> &nbsp;|&nbsp; Instances: {len(results)} &nbsp;|&nbsp; Generated: {ts} UTC</p>
  <table>
    <thead><tr>
      <th>Instance Name</th><th>Instance ID</th><th>Private IP</th>
      <th>App Service</th><th>Overall Status</th><th>Key Findings</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="footer">Generated by Patch Automation | TTN MSP Team | {ts} UTC</div>
</body>
</html>"""
