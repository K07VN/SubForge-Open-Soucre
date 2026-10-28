"""DỰ ÁN MẪU — "Dùng thử ngay, chưa cần tải model" (hướng dẫn từng bước, 2.15.5).

Video mẫu tự dựng (``assets/sample/video_mau.mp4``, xem ``tools/tao_video_mau.py``): ~23 giây, giọng đọc tiếng Trung +
6 câu phụ đề tiếng Trung in cứng ở dải dưới. Dự án mẫu cấu hình SẴN để chạy trọn luồng mà
KHÔNG PHẢI TẢI GÌ:
  · nguồn phụ đề OCR (model OCR đóng gói trong bộ cài) + vùng khoanh đúng dải phụ đề;
  · dịch: Copy prompt -> ChatGPT / Gemini -> dán kết quả (miễn phí; chủ dự án dặn hướng
    người dùng dùng prompt) — hoặc Google Dịch ở Cơ bản;
  · giọng Edge (đọc qua mạng, không tải model).
Video ngắn nên mỗi bước chỉ vài giây.

Mẫu KHÔNG hưởng "mặc định cho dự án mới" (services/mac_dinh_du_an.py): cấu hình của mẫu
cố định để chắc chắn chạy được trên máy chưa tải gì.
"""
from __future__ import annotations

from utils.i18n import L
import os
import shutil
import sys

TEN_TEP = "video_mau.mp4"
TIEU_DE = "Video mẫu — Chuyện thú vị về bầu trời sao"
#: Dải phụ đề của video mẫu (khớp tools/tao_video_mau.py: chữ ở y = 0.80).
VUNG_SUB = {"x": 0.0, "y": 0.74, "w": 1.0, "h": 0.12}
NGON_NGU_NGUON = "zh-cn"
NGON_NGU_DICH = "vi"
#: Giọng Edge — đọc qua mạng, không cần tải model.
GIONG_EDGE = "vi-VN-HoaiMyNeural"


def duong_goc() -> str:
    """Tệp video mẫu đóng gói (dev: backend/assets; bản cài: _MEIPASS/assets)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "sample", TEN_TEP)


def co_video_mau() -> bool:
    return os.path.isfile(duong_goc())


def chep_vao(thu_muc: str) -> str:
    """Chép video mẫu vào ``thu_muc`` của người dùng (dự án trỏ vào bản chép, không trỏ
    vào thư mục cài đặt — gỡ / cập nhật app không làm dự án mẫu mất video). Có rồi thì
    dùng lại."""
    goc = duong_goc()
    if not os.path.isfile(goc):
        raise FileNotFoundError(L("Bản cài này không có video mẫu", "This installation has no sample video"))
    os.makedirs(thu_muc, exist_ok=True)
    dich = os.path.join(thu_muc, TEN_TEP)
    if not os.path.isfile(dich) or os.path.getsize(dich) != os.path.getsize(goc):
        shutil.copy2(goc, dich)
    return dich


def khoi_phuc_video(goc_review: str) -> bool:
    """Chép lại ``<goc_review>/mau/video_mau.mp4`` nếu MẤT mà còn dự án mẫu trỏ vào nó.

    Bản trước dọn nhầm thư mục ``mau`` như thư mục dự án mồ côi mỗi lần khởi động, nên dự án mẫu
    cũ báo "không thấy tệp". Chỉ làm việc khi tệp đang thiếu (quét metadata chỉ khi đó), và chỉ
    khi có dự án thật sự trỏ vào đó — máy không dùng dự án mẫu thì không chép gì."""
    import json

    thu_muc = os.path.join(goc_review, "mau")
    dich = os.path.join(thu_muc, TEN_TEP)
    if os.path.isfile(dich) or not co_video_mau():
        return False
    can = os.path.normcase(os.path.abspath(dich))
    try:
        ds = os.listdir(goc_review)
    except OSError:
        return False
    for pid in ds:
        try:
            with open(os.path.join(goc_review, pid, "metadata.json"), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        vp = m.get("video_path") if isinstance(m, dict) else None
        if vp and os.path.normcase(os.path.abspath(vp)) == can:
            chep_vao(thu_muc)
            return True
    return False


#: Cấu hình metadata của dự án Chi tiết mẫu. ``du_an_mau`` = dấu để DÙNG LẠI (``tim_chi_tiet``).
META_CHI_TIET = {
    "du_an_mau": True,
    "source_language": NGON_NGU_NGUON,
    "target_language": NGON_NGU_DICH,
    "target_voice": GIONG_EDGE,
    "subtitle_mode": "ocr",
    "ocr_region": VUNG_SUB,
    "ocr_lang": "ch",
}

TIEU_DE_LO = "Lô mẫu — bầu trời sao"


def tim_chi_tiet(goc: str) -> str | None:
    """Dự án Chi tiết mẫu MỚI NHẤT còn video (để hướng dẫn mở lại, không đẻ thêm dự án mỗi
    lần xem). Đọc NHẸ metadata.json — không đi qua ``list_projects`` (tính dung lượng thư
    mục từng dự án, máy vài trăm dự án là chậm)."""
    import json

    tot, luc_tot = None, ""
    try:
        ds = os.listdir(goc)
    except OSError:
        return None
    for pid in ds:
        try:
            with open(os.path.join(goc, pid, "metadata.json"), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict) or not m.get("du_an_mau") or m.get("thuoc_lo_mau"):
            continue
        #  Lô mẫu bản 2.15.5 đầu còn để cờ ``du_an_mau`` trên dự án của lô (tên "… (1)", "… (2)").
        if str(m.get("title") or "").startswith(f"{TIEU_DE} ("):
            continue
        if not os.path.isfile(m.get("video_path") or ""):
            continue
        luc = str(m.get("created_at") or "")
        if luc >= luc_tot:
            tot, luc_tot = pid, luc
    return tot


#: Cấu hình LÔ mẫu: dịch + soạn bài đăng bằng prompt tự dán (không cần DeepSeek key).
CAU_HINH_LO = {
    "source_language": NGON_NGU_NGUON,
    "target_language": NGON_NGU_DICH,
    "subtitle_mode": "ocr",
    "translate_engine": "manual",
    "meta_engine": "manual",
    "voice": GIONG_EDGE,
    "voice_mode": "chung",
    "fit_mode": "auto",
}
