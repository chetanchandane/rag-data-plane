"""Presigned S3 upload URLs (SPEC: Architecture, "Upload flow").

The client PUTs the file straight to S3; the API never handles file bytes. The URL signs two
headers, so S3 itself rejects an upload that doesn't match the registration:

  x-amz-checksum-sha256  S3 hashes the received bytes and refuses them if they differ.
  content-length         Refuses any other size, which also enforces MAX_UPLOAD_BYTES.
"""

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config

from api.config import UPLOAD_URL_EXPIRES_SECONDS


@dataclass(frozen=True)
class Upload:
    url: str
    method: str
    headers: dict[str, str]  # the client must send these exactly
    expires_at: datetime


def make_s3_client(region: str, session: boto3.session.Session | None = None):
    # Virtual-hosted, Regional URLs (bucket.s3.us-east-2.amazonaws.com). The global endpoint
    # can redirect requests for a newly created bucket, and a PUT doesn't follow redirects.
    return (session or boto3.session.Session()).client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def checksum_header(sha256_hex: str) -> str:
    """S3 wants the base64 of the raw 32-byte digest, not the hex string we store."""
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode("ascii")


def presign_upload(s3, *, bucket: str, key: str, sha256_hex: str, size_bytes: int) -> Upload:
    checksum = checksum_header(sha256_hex)
    # Signing is local (no AWS call), so this never waits on the network.
    url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ChecksumSHA256": checksum,
            "ContentLength": size_bytes,
        },
        ExpiresIn=UPLOAD_URL_EXPIRES_SECONDS,
    )
    return Upload(
        url=url,
        method="PUT",
        # Content-Length is signed too, but HTTP clients set it from the body themselves
        # (browsers forbid setting it), so only the checksum header is listed.
        headers={"x-amz-checksum-sha256": checksum},
        expires_at=datetime.now(UTC) + timedelta(seconds=UPLOAD_URL_EXPIRES_SECONDS),
    )
