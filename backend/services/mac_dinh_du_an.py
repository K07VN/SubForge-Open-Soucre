"""MẶC ĐỊNH CHO DỰ ÁN MỚI — "Đặt cấu hình dự án này làm mặc định" (người dùng đề xuất
30-09-2026), riêng cho từng chế độ:

  · ``co_ban``   — dự án Lồng tiếng Cơ bản (bảng ``videos`` + compositor_config.json);
  · ``chi_tiet`` — dự án Chi tiết (metadata.json + compositor_config.json) — gồm cả dự án
    do Chạy hàng loạt tạo ra (cùng đường ``review_service.create_project``);
  · ``lo``       — cấu hình một LÔ mới (``batch_store.create``);
  · ``sang_tao`` — dự án Sáng tạo (đọc báo học ngoại ngữ, ``sangtao_du_an.tao``): chỉ cài đặt, không có dựng hình.

Trước đây không chế độ nào có mặc định: dự án mới luôn ra giá trị viết cứng, người dùng
làm kênh tiếng Nhật giọng X, nền phụ đề vàng… phải chỉnh lại y hệt cho từng dự án.

CHỈ LƯU THỨ DÙNG LẠI ĐƯỢC. KHÔNG lưu thứ gắn với MỘT video: vùng khoanh OCR, phân vai,
mốc thời gian vùng che, bản đã xuất, câu phụ đề… (xem ``KHOA_*`` và
``services/mau_cau_hinh.py``). Tệp đi kèm (logo, ảnh nền khung, font) được CHÉP vào kho
riêng ``<app data>/mac_dinh_du_an/<chế độ>/`` lúc lưu, rồi chép tiếp vào thư mục dự án
mới lúc áp — không trỏ vào thư mục dự án nguồn (xoá dự án đó là mất theo).

Áp đúng MỘT LẦN, lúc TẠO: bật/tắt mặc định về sau không đụng dự án đã có.
"""
from __future__ import annotations

from utils.i18n import L
import json
import logging
import os
import shutil
import threading
import time

from services import mau_cau_hinh as MAU

logger = logging.getLogger("MacDinhDuAn")

CHE_DO = ("co_ban", "chi_tiet", "lo", "sang_tao")

#: Chi tiết — khoá metadata.json dùng lại được (khớp ``AdvancedSettings``, trừ
#: ``ocr_region``: vùng khoanh là của một video).
KHOA_CHI_TIET = (
    "source_language", "target_language", "target_voice", "default_rate",
    "subtitle_mode", "ocr_lang", "separate_audio", "source_audio_enabled",
    "bg_volume", "voice_bg_volume", "vocals_volume", "voice_vocals_volume", "tts_volume",
    "speech_style", "brand_name", "script_kind", "assign_speakers", "fit_mode",
    "cho_tran_giong", "tran_giong",
    #  Hai ô "Tách câu gốc" / "Tách bản dịch" (services/tach_cau.py) — người dùng muốn
    #  "Lưu cấu hình dự án" nhớ cả hai (08-10-2026).
    "tach_cau_goc", "tach_cau_dich",
)
#: Cơ bản — cột bảng ``videos``.
KHOA_CO_BAN = (
    "source_lang", "target_lang", "voice_talent", "translation_engine", "speed_mode",
    "separate_audio", "bg_volume", "voice_bg_volume", "vocals_volume", "voice_vocals_volume",
    "cho_tran_giong", "tran_giong", "tts_volume",
)
#: Cơ bản — lựa chọn chỉ nằm ở giao diện (localStorage theo từng video): nguồn phụ đề,
#: kiểu nói của prompt, ô "Tách câu gốc" (bool). Frontend tự đọc khi mở một video chưa có
#: lựa chọn riêng.
KHOA_CO_BAN_GIAO_DIEN = ("submode", "style", "tach_cau_goc")
#: Lô — mọi khoá cấu hình trừ dự án mẫu (mã một dự án của lô cũ, lô mới không có).
KHOA_LO_BO = ("template_project",)

#: Sáng tạo — cài đặt dùng lại được. KHÔNG lưu thứ gắn với MỘT bài: nguồn bài (``source``), chữ thẻ mở đầu / kết thúc, các phần JSON đã nhận.
KHOA_SANG_TAO = (
    "language", "aspect", "audience", "voice", "speed", "intro", "outro", "content_mode", "level", "native_language",
    #  "phonetic" = ô "Hiện phiên âm" (romaji / pinyin) — từng thiếu nên "Lưu cấu hình dự án" không nhớ (người dùng báo 07-10-2026).
    "furigana", "phonetic", "passes", "read_explanation", "shadow_gap", "camera", "narration", "layout", "script_full",
    "cc_langs", "cc_engine",
)

#: Phần dựng hình lưu mặc định nếu người dùng không chọn — như lô, trừ che mờ: vùng
#: che đặt cho sub của MỘT video, dự án mới bất kỳ chưa chắc sub nằm đúng chỗ đó.
MAC_DINH_PHAN = ("sub", "watermark", "logo", "khung")

_KHOA = threading.Lock()


def _goc() -> str:
    from models.database import get_app_data_dir

    return get_app_data_dir()


def _tep() -> str:
    return os.path.join(_goc(), "mac_dinh_du_an.json")


def thu_muc_kho(che_do: str) -> str:
    return os.path.join(_goc(), "mac_dinh_du_an", che_do)


def _doc_het() -> dict:
    try:
        with open(_tep(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _ghi_het(d: dict) -> None:
    p = _tep()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tam = p + ".tmp"
    with open(tam, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tam, p)


def doc(che_do: str) -> dict | None:
    if che_do not in CHE_DO:
        return None
    with _KHOA:
        m = _doc_het().get(che_do)
    return m if isinstance(m, dict) else None


def trang_thai() -> dict:
    """Cho giao diện: chế độ nào đang có mặc định, lưu từ dự án nào, lúc nào."""
    with _KHOA:
        d = _doc_het()
    ra = {}
    for c in CHE_DO:
        m = d.get(c)
        ra[c] = (
            {"luc": m.get("luc"), "tu": m.get("tu", ""), "phan": m.get("phan", []),
             "co_dung_hinh": bool(m.get("compositor"))}
            if isinstance(m, dict) else None
        )
    return ra


def luu(che_do: str, cai_dat: dict, compositor: dict | None, phan, tu: str = "") -> dict:
    """Lưu mặc định của ``che_do``. ``compositor`` = cấu hình dựng hình ĐÃ GHÉP của dự án
    nguồn (None = không lưu phần dựng hình); ``phan`` = các phần dựng hình được lưu."""
    if che_do not in CHE_DO:
        raise ValueError(L(f"Chế độ lạ: {che_do}", f"Unknown mode: {che_do}"))
    phan = [p for p in (phan if phan is not None else MAC_DINH_PHAN) if p in MAU.PHAN_MAU]
    kho = thu_muc_kho(che_do)
    with _KHOA:
        #  Làm lại kho từ đầu: logo cũ của lần lưu trước không được sót lại để lần áp sau
        #  lỡ trỏ vào.
        shutil.rmtree(kho, ignore_errors=True)
        comp = None
        if compositor and phan:
            os.makedirs(kho, exist_ok=True)
            comp = MAU.ghep_phan_mau({}, compositor, phan, kho)
        d = _doc_het()
        d[che_do] = {
            "luc": time.time(), "tu": tu or "",
            "cai_dat": dict(cai_dat or {}), "phan": phan if comp else [],
            "compositor": comp,
        }
        _ghi_het(d)
    logger.info(f"Đã lưu mặc định dự án mới ({che_do}) từ '{tu}': "
                f"{len(cai_dat or {})} cài đặt, dựng hình: {phan if comp else 'không'}")
    return d[che_do]


def xoa(che_do: str) -> None:
    with _KHOA:
        d = _doc_het()
        d.pop(che_do, None)
        _ghi_het(d)
        shutil.rmtree(thu_muc_kho(che_do), ignore_errors=True)


def cai_dat(che_do: str, khoa_hop_le=None) -> dict:
    """Phần cài đặt (không phải dựng hình) đã lưu, lọc theo ``khoa_hop_le``."""
    m = doc(che_do) or {}
    c = m.get("cai_dat") if isinstance(m.get("cai_dat"), dict) else {}
    if khoa_hop_le is None:
        return dict(c)
    return {k: v for k, v in c.items() if k in khoa_hop_le}


def ap_dung_hinh(che_do: str, thu_muc_du_an: str) -> bool:
    """Ghi compositor_config.json cho dự án MỚI từ mặc định (chép tệp đi kèm từ kho sang
    thư mục dự án). Không có mặc định dựng hình -> không ghi gì (dự án dùng mặc định gốc).
    Lỗi thì ghi log rồi thôi: tạo dự án không được hỏng vì phần mặc định."""
    m = doc(che_do) or {}
    comp, phan = m.get("compositor"), m.get("phan") or []
    if not isinstance(comp, dict) or not phan:
        return False
    try:
        from services.compositor_utils import save_compositor_config

        cfg = MAU.ghep_phan_mau({}, comp, phan, thu_muc_du_an)
        save_compositor_config(os.path.join(thu_muc_du_an, "compositor_config.json"), cfg)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Không áp được mặc định dựng hình ({che_do}) cho {thu_muc_du_an}: {e}")
        return False
