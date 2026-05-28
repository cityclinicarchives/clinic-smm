from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from app.schemas.component_generation import ComponentRecord

COMPONENTS_DIR = Path("storage/components")


def slugify(value: str) -> str:
    value = (value or "component").lower().strip()
    value = re.sub(r"[^a-zа-яё0-9]+", "-", value, flags=re.IGNORECASE).strip("-")
    return value[:80] or "component"


def ensure_components_dir(project_state_id: int) -> Path:
    path = COMPONENTS_DIR / f"state-{project_state_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def component_png_path(project_state_id: int, task_id: str, component_id: Optional[str] = None) -> Path:
    base = ensure_components_dir(project_state_id)
    # Normal generation stores the canonical component path. Repair attempts get
    # unique files so the repair loop can choose the best attempt after up to
    # 3 retries instead of overwriting previous candidates.
    base_name = slugify(component_id or task_id)
    if "_repair_" in (task_id or ""):
        return base / f"{base_name}--{slugify(task_id)}.png"
    return base / f"{base_name}.png"


def component_json_path(project_state_id: int) -> Path:
    return ensure_components_dir(project_state_id) / "components.json"


def save_component_bytes(project_state_id: int, task_id: str, image_bytes: bytes, component_id: Optional[str] = None) -> str:
    path = component_png_path(project_state_id, task_id, component_id)
    path.write_bytes(image_bytes)
    return str(path)


def load_component_manifest(project_state_id: int) -> Dict[str, Any]:
    path = component_json_path(project_state_id)
    if not path.exists():
        return {"components": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"components": {}}


def save_component_manifest(project_state_id: int, manifest: Dict[str, Any]) -> None:
    path = component_json_path(project_state_id)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_component_record(project_state_id: int, record: ComponentRecord) -> None:
    manifest = load_component_manifest(project_state_id)
    manifest.setdefault("components", {})[record.component_id] = record.model_dump()
    save_component_manifest(project_state_id, manifest)
