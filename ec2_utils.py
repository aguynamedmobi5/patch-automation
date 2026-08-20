from typing import Optional, List, Dict, Tuple
"""
ec2_utils.py
------------
EC2 instance discovery via tag filters.
Returns structured instance metadata used by all other modules.
"""

import boto3
from config import AWS_REGION, TAG_KEY, TAG_VALUE, SERVICE_TAG_KEY, SERVICE_NAME_MAP


def get_ec2_client():
    return boto3.client("ec2", region_name=AWS_REGION)


def resolve_service_from_name(name: str) -> str:
    """
    Infers the AppService value by matching a SERVICE_NAME_MAP key against
    the instance's Name tag, e.g. 'sin-last-irx-vr-prod-cassandra-instance-1'
    -> 'cassandra'. Used when the instance has no explicit AppService tag.
    Returns "" if no known service keyword is found in the name.
    """
    name_lower = name.lower()
    for service_key in SERVICE_NAME_MAP:
        if service_key in name_lower:
            return service_key
    return ""


def discover_instances(ec2_client=None, tag_key: str = TAG_KEY,
                       tag_value: str = TAG_VALUE) -> List[dict]:
    """
    Discovers running EC2 instances matching the given tag filter.

    Returns a list of instance dicts:
    {
        instance_id : str,
        name        : str,       # value of Name tag, or instance_id
        private_ip  : str,
        app_service : str,       # value of AppService tag (lowercased)
        tags        : dict,      # all tags as flat key→value dict
    }
    """
    if ec2_client is None:
        ec2_client = get_ec2_client()

    print(f"\n[DISCOVERY] Searching for instances with tag "
          f"{tag_key}={tag_value} in {AWS_REGION}...")

    paginator = ec2_client.get_paginator("describe_instances")
    pages     = paginator.paginate(
        Filters=[
            {"Name": f"tag:{tag_key}", "Values": [tag_value]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )

    instances = []
    for page in pages:
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                name = tags.get("Name", inst["InstanceId"])

                app_service = tags.get(SERVICE_TAG_KEY, "").lower().strip()
                service_source = "tag"
                if not app_service:
                    app_service = resolve_service_from_name(name)
                    service_source = "name-match"

                info = {
                    "instance_id" : inst["InstanceId"],
                    "name"        : name,
                    "private_ip"  : inst.get("PrivateIpAddress", "N/A"),
                    "app_service" : app_service,
                    "tags"        : tags,
                }
                instances.append(info)
                print(f"  Found: {info['instance_id']} | {info['name']} "
                      f"| IP: {info['private_ip']} "
                      f"| AppService: {info['app_service'] or 'NOT SET'} ({service_source})")

    if not instances:
        print(f"  [WARN] No running instances found with tag {tag_key}={tag_value}")
    else:
        print(f"  Total: {len(instances)} instance(s) found")

    return instances
