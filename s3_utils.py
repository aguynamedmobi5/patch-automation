"""
s3_utils.py
-----------
S3 client factory and all S3 helper operations.

Bucket region is read from config.S3_BUCKET_REGION — no GetBucketLocation
call needed, avoiding the s3:GetBucketLocation IAM permission requirement.
Update S3_BUCKET_REGION in config.py if the bucket moves.
"""

import json
from typing import Optional, List, Dict, Tuple
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from config import S3_BUCKET, S3_PREFIX, S3_BUCKET_REGION


def get_s3_client(bucket_name: str = S3_BUCKET):
    """
    Returns an S3 client configured for the bucket region defined in config.
    Uses SigV4 to ensure presigned URLs work correctly for all regions.
    """
    return boto3.client(
        "s3",
        region_name=S3_BUCKET_REGION,
        config=Config(signature_version="s3v4"),
    )


# ── Upload helpers ────────────────────────────────────────────

def upload_json(s3_client, data: dict, instance_id: str,
                phase: str, timestamp: str) -> Optional[str]:
    """
    Uploads raw command result JSON to S3.
    Returns the S3 key on success, None on failure.

    S3 path: patch-automation/YYYY-MM-DD/<instance-id>/<phase>_<ts>.json
    """
    from datetime import datetime, timezone
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key      = f"{S3_PREFIX}/{date_str}/{instance_id}/{phase}_{timestamp}.json"

    try:
        s3_client.put_object(
            Bucket      = S3_BUCKET,
            Key         = key,
            Body        = json.dumps(data, indent=2),
            ContentType = "application/json",
        )
        print(f"    [S3] JSON uploaded: s3://{S3_BUCKET}/{key}")
        return key
    except ClientError as e:
        print(f"    [S3][ERROR] JSON upload failed: {e}")
        return None


def upload_html(s3_client, html_content: str, s3_key: str) -> bool:
    """
    Uploads an HTML string to a given S3 key.
    Returns True on success, False on failure.
    """
    try:
        s3_client.put_object(
            Bucket      = S3_BUCKET,
            Key         = s3_key,
            Body        = html_content.encode("utf-8"),
            ContentType = "text/html",
        )
        print(f"    [S3] HTML uploaded: s3://{S3_BUCKET}/{s3_key}")
        return True
    except ClientError as e:
        print(f"    [S3][ERROR] HTML upload failed: {e}")
        return False


def download_json(s3_client, key: str) -> Optional[dict]:
    """
    Downloads and parses a JSON file from S3.
    Returns parsed dict, or None on error.
    """
    try:
        obj  = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        print(f"    [S3][ERROR] JSON download failed for {key}: {e}")
        return None


def key_exists(s3_client, key: str) -> bool:
    """
    Returns True if an S3 key exists, False otherwise.
    """
    try:
        s3_client.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def list_instance_folders(s3_client, date_str: str) -> List[str]:
    """
    Lists instance-id folders under patch-automation/YYYY-MM-DD/
    Returns list of instance IDs found in S3 for that date.
    """
    prefix   = f"{S3_PREFIX}/{date_str}/"
    response = s3_client.list_objects_v2(
        Bucket    = S3_BUCKET,
        Prefix    = prefix,
        Delimiter = "/",
    )
    folders = []
    for cp in response.get("CommonPrefixes", []):
        folder = cp["Prefix"].rstrip("/").split("/")[-1]
        if folder.startswith("i-"):
            folders.append(folder)
    return folders


def find_latest_json(s3_client, date_str: str,
                     instance_id: str, phase: str) -> Optional[str]:
    """
    Finds the most recent JSON file for a given instance + phase.
    Returns the S3 key, or None if not found.
    """
    prefix   = f"{S3_PREFIX}/{date_str}/{instance_id}/{phase}_"
    response = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    keys     = sorted(
        obj["Key"] for obj in response.get("Contents", [])
        if obj["Key"].endswith(".json")
    )
    return keys[-1] if keys else None


def generate_presigned_get_url(s3_client, key: str,
                                expires_in: int = 86400) -> str:
    """
    Generates a pre-signed GET URL for an S3 object.
    Default expiry: 24 hours.
    """
    return s3_client.generate_presigned_url(
        "get_object",
        Params   = {"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn= expires_in,
    )