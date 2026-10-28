# -*- coding: utf-8 -*-
"""ĐA NGÔN NGỮ phía backend (Việt / Anh) — người dùng 13-10-2026.

App desktop chỉ có MỘT người dùng nên ngôn ngữ giao diện là trạng thái CHUNG của tiến trình: middleware chép header
``x-app-lang`` của mọi request vào đây, và các luồng chạy nền (không có request) vẫn đọc được lần gần nhất.
Chưa có request nào thì mặc định tiếng Việt.

Cách dùng:  ``from utils.i18n import L``  ->  ``raise HTTPException(400, L("Không tìm thấy tệp", "File not found"))``
Chỉ dịch chữ HIỆN CHO NGƯỜI DÙNG (lỗi trả về giao diện, nhật ký tiến độ). Prompt gửi AI, tên tệp, khoá lưu bền KHÔNG dịch.
"""
from __future__ import annotations

_lang = "vi"


def set_lang(v) -> None:
    global _lang
    s = str(v or "").strip().lower()
    if s in ("vi", "en"):
        _lang = s


def get_lang() -> str:
    return _lang


def L(vi: str, en: str) -> str:
    """Chọn chữ theo ngôn ngữ giao diện hiện tại."""
    return en if _lang == "en" else vi
