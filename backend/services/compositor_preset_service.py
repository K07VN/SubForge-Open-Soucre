"""Preset compositor cá nhân — lưu file trên AppData (desktop Electron)."""
from utils.i18n import L
import json
import os
import shutil
import time
from typing import Any, Dict, List

from models.database import get_app_data_dir

PRESETS_FILE = "compositor_personal_presets.json"
# Thư mục riêng, cố định trong AppData — tách khỏi dự án nên không bị move/xóa
# theo dự án. Mỗi loại asset 1 thư mục con.
PRESET_STORE_DIR = "compositor_presets"
_PRESET_EXTS = {
    "audio": (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"),
    "logo": (".png",),
    # Preset VIDEO (đoạn mở đầu / kết thúc / clip chèn hài): file do backend CẮT RA từ
    # đoạn đang thấy trên timeline rồi đặt vào kho, nên luôn là .mp4.
    #
    # ẢNH cũng nằm ở nhóm "video" này — nó là một clip trên LÀN VIDEO, chỉ khác là giữ
    # trên màn bao lâu do người dùng đặt (data.is_image = True). Không tách nhóm riêng để
    # khỏi phải di trú kho preset của người dùng đang có, và để một danh sách preset duy
    # nhất hiện cả hai loại ở tab Nguyên liệu.
    "video": (".mp4", ".mkv", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".bmp"),
}
# Loại preset có kèm file asset (cần dọn file khi xóa + gắn cờ missing khi mất).
_ASSET_CATEGORIES = ("audio", "logo", "video")

EMPTY_STORE: Dict[str, List[dict]] = {
    "logo": [],
    "watermark": [],
    "subtitle": [],
    "audio": [],
    "text": [],
    # Đoạn VIDEO dùng lại xuyên dự án: mở đầu, kết thúc, clip chèn hài…
    # Kho preset cũ không có khoá này -> _normalize_store tự trả [] nên không cần di trú.
    "video": [],
}

VALID_CATEGORIES = frozenset(EMPTY_STORE.keys())


def _presets_path() -> str:
    app_dir = get_app_data_dir()
    os.makedirs(app_dir, exist_ok=True)
    return os.path.join(app_dir, PRESETS_FILE)


def _normalize_store(data: Any) -> Dict[str, List[dict]]:
    if not isinstance(data, dict):
        return {k: [] for k in EMPTY_STORE}
    out = {k: [] for k in EMPTY_STORE}
    for cat in EMPTY_STORE:
        items = data.get(cat)
        if isinstance(items, list):
            out[cat] = [i for i in items if isinstance(i, dict) and i.get("id")]
    return out


def load_personal_presets() -> Dict[str, List[dict]]:
    path = _presets_path()
    if not os.path.exists(path):
        return {k: [] for k in EMPTY_STORE}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _normalize_store(json.load(f))
    except Exception:
        return {k: [] for k in EMPTY_STORE}


def resolve_asset_path(path: str, kind: str) -> str:
    """Tìm lại file asset của preset khi đường dẫn đã lỗi thời.

    VÌ SAO CẦN: đường dẫn lưu trong preset là TUYỆT ĐỐI. Khi người dùng đổi "vị trí
    lưu dữ liệu" (hoặc nâng cấp từ bản cũ lưu ở %APPDATA%), file được dời sang kho
    mới nhưng đường dẫn trong preset vẫn trỏ chỗ cũ → mọi preset audio/logo hiện
    "mất tệp" dù file còn nguyên.

    Ở đây thử tìm lại theo TÊN FILE trong kho preset hiện hành. Trả về đường dẫn còn
    dùng được, hoặc chuỗi rỗng nếu thực sự không còn.
    """
    if not path:
        return ""
    if os.path.exists(path):
        return path
    try:
        candidate = os.path.join(_preset_asset_dir(kind), os.path.basename(path))
    except Exception:
        return ""
    return candidate.replace("\\", "/") if os.path.exists(candidate) else ""


def load_personal_presets_annotated() -> Dict[str, List[dict]]:
    """Như load_personal_presets nhưng gắn cờ `missing` cho preset audio/logo có
    file kèm theo đã bị xóa.

    Nếu tìm lại được file trong kho hiện hành (xem resolve_asset_path) thì SỬA LUÔN
    đường dẫn và ghi xuống đĩa — chữa một lần cho tất cả các lần sau.
    """
    store = load_personal_presets()
    annotated: Dict[str, List[dict]] = {}
    healed = 0
    for cat, items in store.items():
        if cat in _ASSET_CATEGORIES:
            new_items = []
            for p in items:
                data = p.get("data") or {}
                path = data.get("path")
                fixed = resolve_asset_path(path, cat) if path else ""
                if path and fixed and fixed != path:
                    data["path"] = fixed
                    p["data"] = data
                    healed += 1
                    path = fixed
                new_items.append({**p, "missing": bool(path) and not os.path.exists(path)})
            annotated[cat] = new_items
        else:
            annotated[cat] = items

    if healed:
        # Ghi lại bản đã chữa để lần sau không phải dò nữa.
        try:
            save_personal_presets(store)
        except Exception:
            pass
    return annotated


def save_personal_presets(store: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    normalized = _normalize_store(store)
    path = _presets_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    return normalized


def add_personal_preset(category: str, name: str, data: dict) -> tuple[dict, Dict[str, List[dict]]]:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category: {category}")
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValueError(L("Tên preset không được để trống", "The preset name must not be empty"))

    store = load_personal_presets()
    preset = {
        "id": f"personal-{category}-{int(time.time() * 1000)}",
        "name": trimmed,
        "createdAt": int(time.time() * 1000),
        "data": data if isinstance(data, dict) else {},
    }
    store[category] = [preset, *store[category]]
    save_personal_presets(store)
    return preset, store


def _preset_asset_dir(kind: str) -> str:
    d = os.path.join(get_app_data_dir(), PRESET_STORE_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return d


def copy_asset_to_preset_store(source_path: str, kind: str) -> str:
    """Sao file asset (audio/logo) của dự án vào kho preset toàn cục (AppData) để
    preset dùng lại xuyên dự án. Trả về đường dẫn bền vững; raise nếu không hợp lệ."""
    if kind not in _PRESET_EXTS:
        raise ValueError(L(f"Loại asset không hỗ trợ: {kind}", f"Unsupported asset type: {kind}"))
    if not source_path or not os.path.exists(source_path):
        raise ValueError(L("File nguồn không tồn tại", "The source file does not exist"))
    ext = os.path.splitext(source_path)[1].lower()
    if ext not in _PRESET_EXTS[kind]:
        raise ValueError(L("Định dạng file không hỗ trợ", "Unsupported file format"))
    dest = os.path.join(_preset_asset_dir(kind), f"preset_{kind}_{int(time.time() * 1000)}{ext}")
    shutil.copyfile(source_path, dest)
    return dest.replace("\\", "/")


# Giữ tên cũ cho tương thích lời gọi hiện có.
def copy_audio_to_preset_store(source_path: str) -> str:
    return copy_asset_to_preset_store(source_path, "audio")


def delete_personal_preset(category: str, preset_id: str) -> Dict[str, List[dict]]:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category: {category}")
    store = load_personal_presets()
    # Dọn file asset kèm theo preset (tránh rác trong AppData). Chỉ xóa file nằm
    # trong kho preset — không đụng tới file trong thư mục dự án.
    if category in _ASSET_CATEGORIES:
        for p in store[category]:
            if p.get("id") == preset_id:
                path = (p.get("data") or {}).get("path")
                norm = (path or "").replace("\\", "/")
                if path and PRESET_STORE_DIR in norm and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
    store[category] = [p for p in store[category] if p.get("id") != preset_id]
    return save_personal_presets(store)


def merge_personal_presets(incoming: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    """Gộp preset từ localStorage (migrate desktop cũ) — không trùng id."""
    store = load_personal_presets()
    normalized = _normalize_store(incoming)
    for cat in EMPTY_STORE:
        existing_ids = {p.get("id") for p in store[cat]}
        for item in normalized[cat]:
            if item.get("id") not in existing_ids:
                store[cat].append(item)
                existing_ids.add(item.get("id"))
        store[cat].sort(key=lambda p: p.get("createdAt", 0), reverse=True)
    return save_personal_presets(store)
