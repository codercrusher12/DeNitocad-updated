"""
Cloudflare R2 (S3-compatible) storage for generated STEP/STL files.

Adapted directly from Stitchfren's app/storage/r2.py - same reasoning
applies here: writing to local ./output/ works for local dev, but breaks
on Railway, where the filesystem is ephemeral and wiped on every
redeploy/restart. A download link handed to a customer (or to an OKX
agent) needs to survive longer than "until the next git push."

If R2 isn't configured (no env vars set), upload_file() returns None and
the caller falls back to serving the local file directly via
/download/step/{filename} - useful for local testing without R2
credentials, not something to rely on in production.
"""

from __future__ import annotations

import uuid

from config import settings
from exceptions import StorageError
from logging_config import get_logger

logger = get_logger(__name__)

_client = None


def _get_client():
    """Lazily construct the boto3 client so importing this module doesn't
    require boto3/credentials unless R2 is actually configured."""
    global _client
    if _client is not None:
        return _client

    if not settings.r2_configured:
        return None

    account_id = settings.R2_ACCOUNT_ID
    access_key = settings.R2_ACCESS_KEY_ID
    secret_key = settings.R2_SECRET_ACCESS_KEY

    import boto3
    from botocore.config import Config

    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        # addressing_style: "path" is required for R2 - without it,
        # boto3's default addressing style produces requests R2 rejects
        # with "InvalidArgument: Authorization" (a 400, not an auth
        # failure despite the misleading message - this is R2's own
        # generic error for a malformed/mismatched request, confirmed by
        # multiple independent reports on Cloudflare's own community
        # forum, e.g. community.cloudflare.com/t/r2-storage-unable-to-
        # upload-using-pre-signed-url-getting-invalid-argument-
        # authorization/396502, all resolved the same way).
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    return _client


def is_configured() -> bool:
    return _get_client() is not None


def upload_file(local_path: str, key_prefix: str, content_type: str) -> str | None:
    """
    Uploads a local file to R2 under key_prefix/ and returns a URL for it,
    or None if R2 isn't configured. The object key uses a fresh uuid, not
    the local filename, so this is also the fix for the old
    hash(description) % 10000 collision risk - every upload gets a
    globally unique key regardless of what the local file was named.
    """
    client = _get_client()
    if client is None:
        return None

    bucket = settings.R2_BUCKET_NAME
    if not bucket:
        raise StorageError(
            "R2 credentials are set but R2_BUCKET_NAME is missing. "
            "Set R2_BUCKET_NAME in your environment."
        )

    import os as _os
    ext = _os.path.splitext(local_path)[1]
    object_key = f"{key_prefix}/{uuid.uuid4().hex}{ext}"

    try:
        client.upload_file(
            local_path,
            bucket,
            object_key,
            ExtraArgs={"ContentType": content_type},
        )
    except Exception as exc:  # noqa: BLE001 - boto3 raises many exception types
        raise StorageError(f"R2 upload failed: {exc}") from exc

    logger.info("uploaded %s to R2 as %s", local_path, object_key)

    if settings.R2_PUBLIC_URL:
        return f"{settings.R2_PUBLIC_URL.rstrip('/')}/{object_key}"

    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": object_key},
        ExpiresIn=settings.R2_PRESIGNED_EXPIRY_SECONDS,
    )


def upload_step(local_path: str, key_prefix: str = "step") -> str | None:
    return upload_file(local_path, key_prefix, "application/step")


def upload_stl(local_path: str, key_prefix: str = "stl") -> str | None:
    return upload_file(local_path, key_prefix, "model/stl")


# Content types for every exporters.SUPPORTED_FORMATS entry, keyed the
# same way cad_generator.py names them. IGES and DXF don't have
# universally standardized MIME types the way STEP/STL do in practice -
# these are the ones IANA/common CAD tooling actually uses.
_CONTENT_TYPES = {
    "step": "application/step",
    "stl": "model/stl",
    "iges": "model/iges",
    "dxf": "image/vnd.dxf",
    "pdf": "application/pdf",
}


def upload_export(local_path: str, fmt: str) -> str | None:
    """Generic upload for any exporters.SUPPORTED_FORMATS format - added
    alongside upload_step/upload_stl (kept as-is for any existing caller)
    rather than replacing them, so this is purely additive."""
    content_type = _CONTENT_TYPES.get(fmt, "application/octet-stream")
    return upload_file(local_path, fmt, content_type)
