"""CHÉP CẤU HÌNH DỰNG HÌNH THEO TỪNG PHẦN — dùng chung cho:
  · Chạy hàng loạt "Chép từ dự án mẫu" (routes/batch_routes.apply_settings);
  · "Đặt làm mặc định cho dự án mới" (services/mac_dinh_du_an.py) — lúc LƯU (dự án ->
    kho mặc định) và lúc ÁP (kho -> dự án mới).

Chép theo phần thay cho chép nguyên khối (rà soát 30-09-2026). Chép nguyên khối từng:
  · chép cả ``lastExport*`` -> dự án đích trỏ vào BẢN XUẤT của dự án mẫu;
  · ghi logo / nhạc nền đã xoá đường dẫn + tắt -> logo / nhạc nền SẴN CÓ của đích tắt mất;
  · chép mốc ``start``/``end`` của vùng che — đó là giây trên video MẪU.
Tệp đi kèm (logo, ảnh nền khung, font tự tải) được CHÉP sang thư mục đích: mỗi dự án giữ
bản riêng — trỏ vào thư mục dự án mẫu thì xoá dự án mẫu là mất theo mà không báo gì.
Ngôn ngữ phụ đề xuất (subtitleLang, srtLangs…) không chép: tuỳ bản dịch từng dự án có.
"""
from __future__ import annotations

import copy
import os
import shutil

PHAN_MAU: dict[str, tuple[str, ...]] = {
    "sub": ("subtitleStyle", "secondSubStyle", "showSourceSubs"),
    "watermark": ("watermark",),
    "logo": ("logo",),
    "khung": ("aspectRatio", "customAspectW", "customAspectH", "cropFrame",
              "frameBackground", "resolution", "mirror"),
    "mask": ("maskZones",),
    "text": ("textOverlays",),
}
#: Mặc định của lô (như trước khi có ô tick): mọi thứ trừ chữ chèn (chữ riêng từng video).
MAC_DINH_PHAN_MAU = ("sub", "watermark", "logo", "khung", "mask")


def chep_tep_sang(nguon: str, thu_muc_goc: str, ten: str = "", con: str = "assets") -> str:
    """Chép ``nguon`` vào ``<thu_muc_goc>/<con>/`` và trả đường dẫn mới; tệp không còn
    thì trả ""."""
    if not nguon or not os.path.isfile(nguon):
        return ""
    thu_muc = os.path.join(thu_muc_goc, con)
    os.makedirs(thu_muc, exist_ok=True)
    dich = os.path.join(thu_muc, ten or os.path.basename(nguon))
    if os.path.abspath(dich) != os.path.abspath(nguon):
        shutil.copy2(nguon, dich)
    return dich


def ghep_phan_mau(dich: dict, mau: dict, phan, thu_muc_dich: str) -> dict:
    """Cấu hình dựng hình của ``dich`` sau khi chép các ``phan`` từ ``mau``; tệp đi kèm
    chép vào ``thu_muc_dich`` (thư mục dự án đích, hoặc kho mặc định). Không sửa tại chỗ."""
    ra = copy.deepcopy(dich or {})
    phan = [p for p in (phan or ()) if p in PHAN_MAU]
    for p in phan:
        for khoa in PHAN_MAU[p]:
            if khoa in mau:
                ra[khoa] = copy.deepcopy(mau[khoa])
    if "logo" in phan and isinstance(mau.get("logo"), dict):
        logo = dict(ra["logo"])
        moi = chep_tep_sang(logo.get("path") or "", thu_muc_dich, "logo.png")
        if moi:
            logo["path"] = moi
        else:
            #  Mẫu không có tệp logo -> chỉ lấy vị trí / cỡ, giữ tệp + công tắc của đích.
            cu = (dich or {}).get("logo") if isinstance((dich or {}).get("logo"), dict) else {}
            logo["path"], logo["enabled"] = cu.get("path", ""), bool(cu.get("enabled"))
        ra["logo"] = logo
    if "khung" in phan and isinstance(ra.get("frameBackground"), dict):
        fb = dict(ra["frameBackground"])
        if fb.get("type") == "image":
            moi = chep_tep_sang(fb.get("image") or "", thu_muc_dich)
            if moi:
                fb["image"] = moi
            else:
                fb["type"], fb["image"] = "blur", ""
        ra["frameBackground"] = fb

    #  FONT TỰ TẢI LÊN (``fontFile``) cũng nằm trong thư mục dự án mẫu -> chép sang đích;
    #  mất tệp thì bỏ khoá (engine tự rơi về font cùng tên đóng gói / của Windows).
    def _font(kieu):
        if isinstance(kieu, dict) and kieu.get("fontFile"):
            kieu = dict(kieu)
            moi = chep_tep_sang(kieu["fontFile"], thu_muc_dich, con=os.path.join("assets", "fonts"))
            if moi:
                kieu["fontFile"] = moi
            else:
                kieu.pop("fontFile", None)
        return kieu

    if "sub" in phan:
        for khoa in ("subtitleStyle", "secondSubStyle"):
            if khoa in mau:
                ra[khoa] = _font(ra.get(khoa))
    if "text" in phan and isinstance(ra.get("textOverlays"), list):
        ra["textOverlays"] = [_font(t) for t in ra["textOverlays"]]
    if "mask" in phan and isinstance(ra.get("maskZones"), list):
        ra["maskZones"] = [
            {k: v for k, v in z.items() if k not in ("start", "end")}
            for z in ra["maskZones"] if isinstance(z, dict)
        ]
    return ra


def doc_cau_hinh_tho(duong: str) -> dict:
    """Cấu hình dựng hình ĐÚNG NHƯ TRÊN ĐĨA (chưa ghép mặc định). Ghi ngược bản đã ghép
    ``load_compositor_config`` là đóng cứng mặc định chung vào mọi khoá dự án chưa tự
    lưu — lệch mặc định riêng của từng chế độ, và phần KHÔNG tick cũng bị đổi."""
    import json

    from services.compositor_utils import unwrap_compositor_payload

    try:
        with open(duong, encoding="utf-8") as f:
            d = json.load(f)
        d = unwrap_compositor_payload(d) if isinstance(d, dict) else {}
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}
