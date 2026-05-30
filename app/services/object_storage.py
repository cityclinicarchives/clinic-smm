"""Object storage adapter for v41 generated artifacts.

Cloudflare R2 is S3-compatible, so boto3 is used only when STORAGE_BACKEND=r2.
When R2 is not configured, all functions safely fall back to local-only mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.config import settings


class ObjectStorageError(RuntimeError):
    pass


def storage_enabled() -> bool:
    return (settings.storage_backend or "local").lower() == "r2"


def r2_configured() -> bool:
    return bool(
        storage_enabled()
        and settings.r2_account_id
        and settings.r2_access_key_id
        and settings.r2_secret_access_key
        and settings.r2_bucket
    )


def _endpoint_url() -> str:
    account_id = settings.r2_account_id.strip()
    return f"https://{account_id}.r2.cloudflarestorage.com"


def _client():
    if not r2_configured():
        raise ObjectStorageError("R2 не настроен: проверьте STORAGE_BACKEND, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.")
    try:
        import boto3  # type: ignore
    except Exception as exc:
        raise ObjectStorageError("Для R2 нужен пакет boto3. Добавьте boto3 в requirements.txt.") from exc
    return boto3.client(
        "s3",
        endpoint_url=_endpoint_url(),
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def object_url(storage_key: str) -> Optional[str]:
    base = (settings.r2_public_base_url or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/{storage_key.lstrip('/')}"


def upload_file(local_path: str | Path, storage_key: str, content_type: str = "application/octet-stream") -> bool:
    """Upload local file to R2. Returns False when R2 is disabled/unconfigured."""
    path = Path(local_path)
    if not path.exists() or not path.is_file():
        raise ObjectStorageError(f"Файл для загрузки не найден: {path}")
    if not r2_configured():
        return False
    client = _client()
    client.upload_file(
        str(path),
        settings.r2_bucket,
        storage_key,
        ExtraArgs={"ContentType": content_type},
    )
    return True


def download_file(storage_key: str, local_path: str | Path) -> bool:
    """Download object from R2. Returns False when R2 is disabled/unconfigured or object is missing."""
    if not r2_configured():
        return False
    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    client = _client()
    try:
        client.download_file(settings.r2_bucket, storage_key, str(path))
        return True
    except Exception:
        return False


def object_exists(storage_key: str) -> bool:
    if not r2_configured():
        return False
    client = _client()
    try:
        client.head_object(Bucket=settings.r2_bucket, Key=storage_key)
        return True
    except Exception:
        return False
