"""
Cloudflare R2 Storage Service

S3-compatible storage for the Oracle Data Lake.
Handles archiving of raw transaction data.
"""

import boto3
from botocore.config import Config

from app.core.config import get_settings


class R2StorageService:
    """Manager for Cloudflare R2 operations."""

    def __init__(self):
        settings = get_settings()
        self.bucket_name = settings.r2_bucket_name
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=Config(signature_version="s3v4"),
        )
