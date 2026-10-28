"""API "Vị trí lưu trữ" — phiên bản ĐƠN GIẢN HÓA.

Người dùng chọn MỘT thư mục gốc (data root) DUY NHẤT một lần khi mới cài app
(first-run). Mọi dữ liệu nằm gọn trong đó cho dễ quản lý:
    <data_root>/app        — db, config, presets, voices, cookies…
    <data_root>/ai_models  — model AI
    <data_root>/gpu_pack   — gói tăng tốc GPU
    <data_root>/dubbing    — dự án Lồng tiếng
    <data_root>/review     — dự án Review

KHÔNG còn tính năng di chuyển dữ liệu (đã gỡ theo yêu cầu — từng gây hỏng
đường dẫn dự án cũ). Muốn đổi chỗ: xóa dữ liệu rồi chọn lại từ đầu, hoặc tự
move thư mục + sửa storage.json khi app tắt.
"""
import logging
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.config_service import get_config_service
from utils.model_paths import get_models_dir
from utils.storage_paths import (
    get_data_root,
    get_default_data_root,
    is_configured,
    is_default_data_root,
    set_data_root,
)

from routes.auth_routes import verify_license
from utils.i18n import L

logger = logging.getLogger("StorageRoutes")

# Chỉ gọi từ màn Cài đặt (sau khi đã kích hoạt) — gắn hàng rào cho đồng bộ với
# các router chức năng khác. Màn kích hoạt KHÔNG đụng tới nhóm này.
router = APIRouter(
    prefix="/api/storage", tags=["storage"], dependencies=[Depends(verify_license)]
)


def is_migrating() -> bool:
    """Tương thích cũ cho các route render — tính năng di chuyển đã gỡ."""
    return False


def _dir_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _categories() -> dict:
    """Đường dẫn hiện tại của từng loại dữ liệu."""
    cfg = get_config_service()
    root = get_data_root()
    return {
        "ai_models": get_models_dir(),
        "gpu_pack": os.path.join(root, "gpu_pack"),
        "dubbing": cfg.get_dubbing_root(),
        "review": cfg.get_review_root(),
    }


@router.get("/info")
async def storage_info():
    root = get_data_root()
    cats = {}
    for name, path in _categories().items():
        cats[name] = {"path": path, "size_bytes": _dir_size(path)}
    try:
        free = shutil.disk_usage(os.path.splitdrive(root)[0] + os.sep).free
    except Exception:
        free = 0
    return {
        "data_root": root,
        "default_root": os.path.abspath(get_default_data_root()),
        "is_default": is_default_data_root(),
        # false = máy mới chưa chọn vị trí lưu -> FE hiện màn chọn thư mục lần đầu.
        "configured": is_configured(),
        "free_bytes": free,
        "categories": cats,
    }


class RootBody(BaseModel):
    new_root: str


@router.post("/set-root")
async def storage_set_root(body: RootBody):
    """Chọn vị trí lưu dữ liệu LẦN ĐẦU (máy mới, chưa có dữ liệu).

    Chỉ cho phép khi CHƯA cấu hình — đã chọn rồi thì không đổi được nữa
    (tính năng di chuyển đã gỡ). Sau khi chọn, FE khởi động lại backend để
    mọi thành phần (db/mounts/models) dùng vị trí mới ngay từ đầu."""
    if is_configured():
        raise HTTPException(409, L("Vị trí lưu dữ liệu đã được chọn từ trước — không đổi được nữa.", "The data storage location was already chosen earlier — it can no longer be changed."))

    new_root = (body.new_root or "").strip().strip('"')
    if not new_root or not os.path.isabs(new_root):
        raise HTTPException(400, L("Đường dẫn phải là đường dẫn tuyệt đối (vd D:\\SubForge_Data).", "The path must be an absolute path (e.g. D:\\SubForge_Data)."))
    new_root = os.path.abspath(new_root)
    try:
        os.makedirs(new_root, exist_ok=True)
        probe = os.path.join(new_root, ".subforge_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise HTTPException(400, L(f"Không ghi được vào vị trí đã chọn: {e}", f"Cannot write to the selected location: {e}"))

    set_data_root(new_root)
    # Trỏ luôn dự án Lồng tiếng/Review vào trong data root (1 thư mục duy nhất).
    get_config_service().update_roots(
        dubbing_root=os.path.join(new_root, "dubbing"),
        review_root=os.path.join(new_root, "review"),
    )
    logger.info("Đã chọn vị trí lưu dữ liệu: %s", new_root)
    return {"ok": True, "data_root": new_root, "restart_required": True}
