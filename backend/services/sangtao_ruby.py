"""FURIGANA (chữ nhỏ ghi cách đọc phía trên chữ Hán) cho video học tiếng Nhật.

AI ghi cách đọc NGAY SAU từng cụm chữ Hán trong ngoặc vuông: ``学生[がくせい]の個性[こせい]`` (chuẩn Anki), hoặc ``{学生|がくせい}``, hoặc
``学生（がくせい）``. Module này chỉ PHÂN TÍCH chuỗi đó thành các đoạn ``(chữ, cách_đọc | None)`` — việc vẽ ở :class:`sangtao_hoc.ChuRuby`.
Giọng đọc và phụ đề KHÔNG được thấy phần ngoặc: dùng :func:`bo_ruby` để lấy chữ trơn.
"""
from __future__ import annotations

import re

_HAN = "一-鿿㐀-䶿々〆ヵヶ"
_KANA = "ぁ-ゖァ-ヺー・"
_RE = re.compile(rf"\{{([^|{{}}\[\]]+)\|([^{{}}\[\]]+)\}}|([{_HAN}]+)\[([^\]]+)\]|([{_HAN}]+)（([{_KANA}]+)）")
_NGOAC_LE = re.compile(r"\[[^\]]*\]")


def phan_tich(van_ban: str) -> list[tuple[str, str | None]]:
    """Chuỗi → [(chữ, cách đọc hoặc None)]. Ngoặc cách đọc không đi kèm cụm chữ Hán thì bị bỏ (không hiện rác)."""
    ra: list[tuple[str, str | None]] = []
    cuoi = 0
    for m in _RE.finditer(van_ban or ""):
        if m.start() > cuoi:
            ra.append((_NGOAC_LE.sub("", van_ban[cuoi:m.start()]), None))
        if m.group(1):
            ra.append((m.group(1).strip(), m.group(2).strip() or None))
        elif m.group(3):
            ra.append((m.group(3), m.group(4).strip() or None))
        else:
            ra.append((m.group(5), m.group(6)))
        cuoi = m.end()
    if cuoi < len(van_ban or ""):
        ra.append((_NGOAC_LE.sub("", van_ban[cuoi:]), None))
    return [(b, r) for b, r in ra if b]


def bo_ruby(van_ban: str) -> str:
    """Chữ TRƠN (không có cách đọc): dùng cho giọng đọc, phụ đề, đo độ dài."""
    return "".join(b for b, _ in phan_tich(van_ban))


def co_ruby(van_ban: str) -> bool:
    return any(r for _, r in phan_tich(van_ban))


def tach_thuong(doan: str) -> list[str]:
    """Đoạn chữ thường → các mảnh có thể ngắt dòng: mỗi chữ CJK một mảnh, chữ Latin theo từ (kèm dấu cách theo sau)."""
    ra: list[str] = []
    cur = ""
    for c in doan:
        if "　" <= c <= "鿿" or "＀" <= c <= "￯" or "가" <= c <= "힣":
            if cur:
                ra.append(cur)
                cur = ""
            ra.append(c)
        elif c == " ":
            cur += c
            ra.append(cur)
            cur = ""
        else:
            cur += c
    if cur:
        ra.append(cur)
    return ra
