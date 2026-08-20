"""
config.py
---------
Central configuration for Patch Automation.
All environment-specific values live here.
No other module should hardcode these values.
"""

# ── AWS ───────────────────────────────────────────────────────
AWS_REGION = "ap-southeast-1"

# ── EC2 Instance Discovery ────────────────────────────────────
# Tag used to discover instances for patching.
# Astro instances carry patch=phase1..phase4 — TAG_VALUE is not fixed here,
# it's selected interactively at startup (see orchestrator.ask_patch_phase()).
TAG_KEY    = "patch"
TAG_VALUE  = "phase1"          # fallback default; overridden by the phase prompt
PATCH_PHASES = ["phase1", "phase2", "phase3", "phase4"]

# Tag used to detect which service to check (mongod, cassandra, etc.).
# Astro instances don't carry this tag — service is instead inferred by
# matching a SERVICE_NAME_MAP key against the instance's Name tag
# (see ec2_utils.resolve_service_from_name()). Kept here as a fallback:
# if an instance DOES have this tag set, it takes priority over name-matching.
SERVICE_TAG_KEY = "AppService"

# ── S3 ────────────────────────────────────────────────────────
S3_BUCKET = "sin-last-irx-vr-msp-patching"
S3_PREFIX = "astro/patch-automation"   # namespaced — bucket is shared across MSP clients

# Explicit bucket region — avoids needing s3:GetBucketLocation permission.
# Set this to the region where your S3 bucket was created.
S3_BUCKET_REGION = "ap-southeast-1"

# ── SNS ───────────────────────────────────────────────────────
SNS_TOPIC_ARN = "arn:aws:sns:ap-southeast-1:058264107033:astro-patch-automation"

# ── SSM ───────────────────────────────────────────────────────
# Max seconds to wait for a single SSM command to complete
SSM_TIMEOUT_SECONDS    = 120
# How often to poll SSM for command status
SSM_POLL_INTERVAL      = 5

# ── Disk comparison ───────────────────────────────────────────
# Minimum disk usage change (percentage points) to raise a WARN
DISK_WARN_THRESHOLD_PCT = 10

# ── Process noise filter ──────────────────────────────────────
# Processes matching these substrings are excluded from top-10
# comparison to avoid false positives from transient system processes
PROCESS_NOISE_FILTERS = [
    "ssm-document-worker",
    "ssm-session-worker",
    "amazon-ssm-agent",
    "ssm-agent-worker",
    "snap/amazon-ssm",
    "kworker/",
    "kthread",
    "[kworker",
    "migration/",
    "ksoftirqd/",
    "rcu_",
]

# ── Commands to run on every instance ────────────────────────
# Each entry: (label, shell_command)
# label   = section header in HTML report and S3 key
# command = shell command executed via SSM Run Command
STANDARD_COMMANDS = [
    (
        "network_ports",
        "netstat -tulnp 2>/dev/null || ss -tulnp",
    ),
    (
        "top_memory_processes",
        "ps aux --sort=-%mem | head -10",
    ),
    (
        "top_cpu_processes",
        "ps aux --sort=-%cpu | head -10",
    ),
    (
        "disk_usage",
        "df -Th",
    ),
    (
        "fstab",
        "cat /etc/fstab",
    ),
    (
        "running_services",
        "systemctl list-units --type=service --state=running --no-pager",
    ),
]

# Service status check — %s replaced with actual service name at runtime
SERVICE_CHECK_COMMAND_TEMPLATE = "systemctl status %s --no-pager"

# ── Service name mapping ──────────────────────────────────────
# Maps AppService tag value → systemctl service name
# Add new entries here as more project types are onboarded
SERVICE_NAME_MAP = {
    "mongod"          : "mongod",
    "mongodb"         : "mongod",
    "cassandra"       : "cassandra",
    "rabbitmq"        : "rabbitmq-server",
    "rabbitmq-server" : "rabbitmq-server",
    "mysql"           : "mysqld",
    "mysqld"          : "mysqld",
    "nginx"           : "nginx",
    "apache"          : "httpd",
    "httpd"           : "httpd",
    "elasticsearch"   : "elasticsearch",
    "redis"           : "redis",
    "postgresql"      : "postgresql",
    "jenkins"         : "jenkins",
}