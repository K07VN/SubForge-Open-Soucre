# -*- coding: utf-8 -*-
"""LÀM SẠCH CÂU TRƯỚC KHI SINH GIỌNG (người dùng dặn 07-10-2026).

Chỉ áp cho CHUỖI GỬI SANG BỘ ĐỌC — chữ trong timeline, phụ đề, kịch bản, bản dịch giữ nguyên từng ký tự.
Vì sao: AI / người dán hay để lại rác ở mép câu — dấu phẩy cuối câu, gạch đầu dòng, dấu sao, ký tự vô hình —
Edge / VieNeu / Qwen đọc thành tiếng lạ, ngắt quãng, hoặc sinh lỗi.

Luật (ít mà chắc — không "sửa văn"):
  · bỏ ký tự vô hình (BOM, zero-width, điều khiển), gộp khoảng trắng;
  · bỏ ĐẦU câu: gạch đầu dòng / dấu sao / chấm tròn (``- • * · ▪ ► –``) và khoảng trắng;
  · bỏ CUỐI câu: dấu ngăn cách lạc lõng (``, ; : 、 ， ； ： - – — / \\ | * # ~ · •``) lặp lại cho tới khi hết — dấu KẾT câu
    (``. ! ? … 。 ！ ？``) và ngoặc / nháy đóng thì giữ;
  · cặp nháy / ngoặc MỞ mà không có ĐÓNG ở cuối câu thì bỏ dấu mở lạc lõng ở cuối;
  · rỗng sau khi làm sạch -> trả về chuỗi gốc đã strip (để bộ đọc tự xử, không "biến mất" câu).
"""
from __future__ import annotations

import re

_VO_HINH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DAU_DAU_CAU = re.compile(r"^[\s\-\u2013\u2014\u2022\*\u00b7\u25aa\u25ba\u25cf]+")
#  dấu ngăn cách / rác KHÔNG kết câu — bỏ khi đứng cuối (sau khi đã bỏ khoảng trắng và ngoặc/nháy đóng tạm)
_RAC_CUOI = "\\,;:\u3001\uff0c\uff1b\uff1a\\-\u2013\u2014/\\\\|\\*#~\u00b7\u2022"
_DAU_CUOI_CAU = re.compile(rf"[\s{_RAC_CUOI}]+$")
#  nháy / ngoặc MỞ đứng cuối câu (không có gì theo sau) = lạc lõng
_MO_CUOI = re.compile(r"[\s\(\[\{\u201c\u2018\"'\u300c\u300e\uff08]+$")
_KHOANG_TRANG = re.compile(r"[ \t\u3000]+")
_XUONG_DONG = re.compile(r"\s*\n\s*")


def lam_sach_cau_doc(text: str | None) -> str:
    """Chuỗi sạch để gửi sang bộ đọc. Luôn trả về str (rỗng nếu đầu vào rỗng)."""
    goc = (text or "")
    s = _VO_HINH.sub("", goc)
    s = _XUONG_DONG.sub(" ", s.strip())
    s = _KHOANG_TRANG.sub(" ", s)
    s = _DAU_DAU_CAU.sub("", s)
    truoc = None
    while truoc != s:
        truoc = s
        s = _DAU_CUOI_CAU.sub("", s)
        s = _MO_CUOI.sub("", s)
    s = s.strip()
    return s if s else goc.strip()
