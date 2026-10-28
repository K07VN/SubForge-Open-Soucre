"""API preset compositor cá nhân — lưu trên AppData (%APPDATA%/SubForge)."""
from fastapi import APIRouter, Body, Depends, HTTPException

from services.compositor_preset_service import (
    add_personal_preset,
    copy_asset_to_preset_store,
    delete_personal_preset,
    load_personal_presets_annotated,
    merge_personal_presets,
)
from routes.auth_routes import verify_license

router = APIRouter(
    prefix="/api/compositor",
    tags=["compositor-presets"],
    dependencies=[Depends(verify_license)],
)


@router.get("/personal-presets")
def get_personal_presets():
    """Đọc preset cá nhân (logo / watermark / sub / audio) từ ổ đĩa — dùng cho app
    desktop. Preset audio được gắn cờ `missing` nếu file nhạc đã bị xóa."""
    return load_personal_presets_annotated()


@router.post("/personal-presets/asset-file")
def store_preset_asset_file(body: dict = Body(...)):
    """Sao file asset (audio/logo) dự án vào kho preset toàn cục — gọi trước khi
    lưu preset audio/logo để preset mang theo file dùng lại xuyên dự án."""
    try:
        path = copy_asset_to_preset_store(body.get("path", ""), body.get("kind", ""))
        return {"path": path}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/personal-presets/{category}")
def create_personal_preset(category: str, body: dict = Body(...)):
    name = body.get("name", "")
    data = body.get("data", {})
    try:
        preset, store = add_personal_preset(category, name, data)
        return {"preset": preset, "store": store}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/personal-presets/{category}/{preset_id}")
def remove_personal_preset(category: str, preset_id: str):
    try:
        store = delete_personal_preset(category, preset_id)
        return {"store": store}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/personal-presets/migrate")
def migrate_personal_presets(body: dict = Body(...)):
    """Gộp preset từ localStorage (lần đầu mở app desktop sau khi nâng cấp)."""
    store = merge_personal_presets(body)
    return {"store": store, "migrated": True}
