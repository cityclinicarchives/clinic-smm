"""PostgreSQL registry for generated v41 files stored locally and/or in R2."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import SemanticAssetFile
from app.services.object_storage import object_url, storage_enabled, upload_file, download_file, object_exists


def storage_key_for(asset_id: int, state_id: int | None, kind: str, file_name: str) -> str:
    state = f"state-{state_id}" if state_id is not None else "latest"
    safe_kind = kind.strip().replace(" ", "_") or "artifact"
    return f"assets/{asset_id}/{state}/{safe_kind}/{file_name}"


def _mime_for(path: Path, kind: str) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".jpg" or suffix == ".jpeg":
        return "image/jpeg"
    if suffix == ".json":
        return "application/json"
    if suffix == ".zip":
        return "application/zip"
    return "application/octet-stream"




def artifact_file_exists(row: SemanticAssetFile) -> bool:
    """Return True only when the registered artifact is physically available.

    For R2-backed rows this checks the remote object with HEAD. This prevents
    PostgreSQL metadata from being treated as a valid cache when a file was
    deleted manually in Cloudflare R2.
    """
    if (row.storage_backend or "").lower() == "r2":
        return bool(row.storage_key and object_exists(row.storage_key))
    if row.local_path:
        try:
            p = Path(row.local_path)
            return p.exists() and p.is_file() and p.stat().st_size > 0
        except Exception:
            return False
    return False


def purge_artifact_row(row: SemanticAssetFile, *, delete_local: bool = True, delete_remote: bool = False) -> None:
    """Remove one artifact registry row and optionally its physical files."""
    row_id = row.id
    storage_key = row.storage_key
    local_path = row.local_path
    db = SessionLocal()
    try:
        fresh = db.query(SemanticAssetFile).filter(SemanticAssetFile.id == row_id).first()
        if fresh is not None:
            db.delete(fresh)
            db.commit()
    finally:
        db.close()
    if delete_local and local_path:
        try:
            p = Path(local_path)
            if p.exists() and p.is_file():
                p.unlink()
        except Exception:
            pass
    if delete_remote and storage_key:
        try:
            from app.services.object_storage import delete_object
            delete_object(storage_key)
        except Exception:
            pass

def upsert_artifact_file(
    db: Session,
    *,
    asset_id: int,
    state_id: int | None,
    kind: str,
    local_path: str | Path,
    upload: bool = True,
) -> SemanticAssetFile:
    path = Path(local_path)
    storage_key = storage_key_for(asset_id, state_id, kind, path.name)
    backend = "local"
    public_url = None
    if upload and path.exists() and storage_enabled():
        uploaded = upload_file(path, storage_key, _mime_for(path, kind))
        if uploaded:
            backend = "r2"
            public_url = object_url(storage_key)

    row = (
        db.query(SemanticAssetFile)
        .filter(
            SemanticAssetFile.asset_id == asset_id,
            SemanticAssetFile.project_state_id == state_id,
            SemanticAssetFile.kind == kind,
            SemanticAssetFile.file_name == path.name,
        )
        .first()
    )
    if row is None:
        row = SemanticAssetFile(
            asset_id=asset_id,
            project_state_id=state_id,
            kind=kind,
            file_name=path.name,
        )
        db.add(row)
    row.local_path = str(path)
    row.storage_backend = backend
    row.storage_key = storage_key if backend == "r2" else row.storage_key
    row.public_url = public_url or row.public_url
    row.mime_type = _mime_for(path, kind)
    row.size_bytes = path.stat().st_size if path.exists() else None
    db.commit()
    db.refresh(row)
    return row


def register_artifact(
    *,
    asset_id: int,
    state_id: int | None,
    kind: str,
    local_path: str | Path,
    upload: bool = True,
) -> SemanticAssetFile:
    db = SessionLocal()
    try:
        return upsert_artifact_file(db, asset_id=asset_id, state_id=state_id, kind=kind, local_path=local_path, upload=upload)
    finally:
        db.close()


def list_artifacts(asset_id: int, state_id: int | None = None, kind: str | None = None) -> list[SemanticAssetFile]:
    db = SessionLocal()
    try:
        q = db.query(SemanticAssetFile).filter(SemanticAssetFile.asset_id == asset_id)
        if state_id is not None:
            q = q.filter(SemanticAssetFile.project_state_id == state_id)
        if kind is not None:
            q = q.filter(SemanticAssetFile.kind == kind)
        return q.order_by(SemanticAssetFile.file_name.asc(), SemanticAssetFile.id.asc()).all()
    finally:
        db.close()


def ensure_artifact_local(row: SemanticAssetFile, desired_path: str | Path | None = None) -> Path | None:
    """Ensure a DB-registered artifact exists on local disk, downloading from R2 if needed.

    Important: for R2-backed rows we first verify the remote object exists.
    If a user manually deleted the file in Cloudflare R2, the PostgreSQL row is
    stale and must not be counted as a reusable artifact.
    """
    local = Path(desired_path or row.local_path or "")
    if (row.storage_backend or "").lower() == "r2":
        if not row.storage_key or not object_exists(row.storage_key):
            return None
        if not local or str(local) == ".":
            local = Path("storage/r2_cache") / row.storage_key
        if local.exists() and local.is_file() and local.stat().st_size > 0:
            return local
        if download_file(row.storage_key, local):
            return local
        return None

    if local and str(local) != "." and local.exists() and local.is_file() and local.stat().st_size > 0:
        return local
    return None


def delete_artifacts(
    *,
    asset_id: int,
    state_id: int | None = None,
    kind: str | None = None,
    file_name_or_prefix: str | None = None,
    delete_remote: bool = True,
    delete_local: bool = True,
) -> dict:
    """Delete artifact registry rows and their local/R2 files.

    file_name_or_prefix can be an exact file name, a stem like png_002, or a prefix.
    Returns counters useful for Telegram diagnostics.
    """
    from app.services.object_storage import delete_object

    target = (file_name_or_prefix or "").strip()
    target_stem = Path(target).stem if target else ""
    stats = {
        "matched": 0,
        "db_deleted": 0,
        "local_deleted": 0,
        "remote_deleted": 0,
        "remote_errors": 0,
        "local_errors": 0,
        "deleted_files": [],
    }
    db = SessionLocal()
    try:
        q = db.query(SemanticAssetFile).filter(SemanticAssetFile.asset_id == asset_id)
        if state_id is not None:
            q = q.filter(SemanticAssetFile.project_state_id == state_id)
        if kind is not None:
            q = q.filter(SemanticAssetFile.kind == kind)
        rows = q.all()
        for row in rows:
            file_name = row.file_name or ""
            stem = Path(file_name).stem
            if target:
                if not (file_name == target or stem == target_stem or file_name.startswith(target) or stem.startswith(target_stem)):
                    continue
            stats["matched"] += 1
            stats["deleted_files"].append(file_name)
            if delete_remote and row.storage_key:
                try:
                    if delete_object(row.storage_key):
                        stats["remote_deleted"] += 1
                except Exception:
                    stats["remote_errors"] += 1
            if delete_local and row.local_path:
                try:
                    p = Path(row.local_path)
                    if p.exists() and p.is_file():
                        p.unlink()
                        stats["local_deleted"] += 1
                except Exception:
                    stats["local_errors"] += 1
            db.delete(row)
            stats["db_deleted"] += 1
        db.commit()
        return stats
    finally:
        db.close()
