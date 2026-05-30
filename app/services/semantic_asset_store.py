"""PostgreSQL registry for generated v41 files stored locally and/or in R2."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import SemanticAssetFile
from app.services.object_storage import object_url, storage_enabled, upload_file, download_file


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
    """Ensure a DB-registered artifact exists on local disk, downloading from R2 if needed."""
    local = Path(desired_path or row.local_path or "")
    if local and str(local) != "." and local.exists() and local.is_file() and local.stat().st_size > 0:
        return local
    if not row.storage_key:
        return None
    if not local or str(local) == ".":
        local = Path("storage/r2_cache") / row.storage_key
    if download_file(row.storage_key, local):
        return local
    return None
