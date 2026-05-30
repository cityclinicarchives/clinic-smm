"""Persistent storage for expensive v41 semantic analysis JSON.

The analysis stage is the costly part of the pipeline. Railway's local
filesystem may be reset on redeploy, so the canonical copy is stored in
PostgreSQL. Files in storage/analysis are treated as convenience exports only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import SemanticAnalysis
from app.schemas.project_state import ProjectStatePayload

SCHEMA_VERSION = "v41.1-compact"


def build_analysis_document(
    *,
    asset_id: int,
    state_id: int,
    payload: ProjectStatePayload | dict[str, Any],
    issues: list[str] | None = None,
) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        payload_data = payload.model_dump()  # type: ignore[attr-defined]
    else:
        payload_data = payload
    return {
        "asset_id": asset_id,
        "project_state_id": state_id,
        "pipeline_stage": "semantic_analysis",
        "schema_version": SCHEMA_VERSION,
        "validation_issues": issues or [],
        "payload": payload_data,
    }


def _cost_fields(document: dict[str, Any]) -> dict[str, Any]:
    payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
    custom = payload.get("custom") if isinstance(payload.get("custom"), dict) else {}
    analysis_state = payload.get("analysis_state") if isinstance(payload.get("analysis_state"), dict) else {}
    cost = custom.get("cost_estimate") or analysis_state.get("cost_estimate") or {}
    if not isinstance(cost, dict):
        cost = {}
    return {
        "estimated_cost_usd": cost.get("estimated_total_usd"),
        "input_tokens": cost.get("input_tokens"),
        "output_tokens": cost.get("output_tokens"),
        "total_tokens": cost.get("total_tokens"),
    }


def save_analysis_to_db(
    db: Session,
    *,
    asset_id: int,
    state_id: int,
    payload: ProjectStatePayload | dict[str, Any],
    issues: list[str] | None = None,
    file_path: str | None = None,
) -> SemanticAnalysis:
    """Create or update the DB copy for a semantic analysis state."""
    document = build_analysis_document(asset_id=asset_id, state_id=state_id, payload=payload, issues=issues)
    compact_json = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    cost = _cost_fields(document)

    row = (
        db.query(SemanticAnalysis)
        .filter(SemanticAnalysis.asset_id == asset_id, SemanticAnalysis.project_state_id == state_id)
        .first()
    )
    if row is None:
        row = SemanticAnalysis(asset_id=asset_id, project_state_id=state_id)
        db.add(row)

    row.schema_version = document.get("schema_version") or SCHEMA_VERSION
    row.analysis_json = compact_json
    row.file_path = file_path
    row.estimated_cost_usd = str(cost.get("estimated_cost_usd")) if cost.get("estimated_cost_usd") is not None else None
    row.input_tokens = int(cost.get("input_tokens") or 0) if cost.get("input_tokens") is not None else None
    row.output_tokens = int(cost.get("output_tokens") or 0) if cost.get("output_tokens") is not None else None
    row.total_tokens = int(cost.get("total_tokens") or 0) if cost.get("total_tokens") is not None else None
    db.commit()
    db.refresh(row)
    return row


def list_analyses_from_db(db: Session, asset_id: int | None = None, limit: int = 20) -> list[SemanticAnalysis]:
    query = db.query(SemanticAnalysis)
    if asset_id is not None:
        query = query.filter(SemanticAnalysis.asset_id == asset_id)
    return query.order_by(SemanticAnalysis.created_at.desc(), SemanticAnalysis.id.desc()).limit(limit).all()


def get_latest_analysis_from_db(db: Session, asset_id: int) -> SemanticAnalysis | None:
    return (
        db.query(SemanticAnalysis)
        .filter(SemanticAnalysis.asset_id == asset_id)
        .order_by(SemanticAnalysis.created_at.desc(), SemanticAnalysis.id.desc())
        .first()
    )


def load_latest_analysis_document(asset_id: int) -> dict[str, Any] | None:
    """Load latest analysis from PostgreSQL. Returns None when missing."""
    db = SessionLocal()
    try:
        row = get_latest_analysis_from_db(db, asset_id)
        if row is None or not row.analysis_json:
            return None
        data = json.loads(row.analysis_json)
        data["_db_analysis_id"] = row.id
        data["_analysis_path"] = row.file_path or f"db://semantic_analyses/{row.id}"
        return data
    finally:
        db.close()


def export_analysis_row_to_file(row: SemanticAnalysis, directory: Path | str = "storage/analysis") -> Path:
    """Write a DB analysis JSON to a local file so Telegram can send it."""
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"asset-{row.asset_id}-state-{row.project_state_id}-semantic-analysis.json"
    path.write_text(row.analysis_json or "{}", encoding="utf-8")
    return path
