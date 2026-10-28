"""CHỮ cho bộ dựng "Sáng tạo": chọn font theo ngôn ngữ (có KIỂM ĐỦ GLYPH) và ngắt dòng cho cả chữ Latin lẫn chữ Nhật/Hán.

Bài học đã gặp: font viết tay có sẵn của Windows (Segoe Print, Ink Free, Comic Sans) THIẾU dấu tiếng Việt, và Mali/Itim
không có chữ Nhật — chọn font phải kiểm glyph thật của văn bản, không đoán theo tên.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

#: font đóng gói (backend/assets/fonts) theo vai trò + ngôn ngữ, ưu tiên từ trái sang phải
FONT_DONG_GOI = {
    "label": {"ja": ["ZenMaruGothic-Bold.ttf"], "ru": ["Montserrat-Bold.ttf"],
              "default": ["Mali-Bold.ttf", "BeVietnamPro-Bold.ttf", "Inter-Bold.ttf"]},
    "subtitle": {"ja": ["ZenMaruGothic-Bold.ttf"], "ru": ["Montserrat-Bold.ttf"],
                 "default": ["BeVietnamPro-Bold.ttf", "Inter-Bold.ttf", "Montserrat-Bold.ttf"]},
}
#: tiếng Trung/Hàn chưa đóng gói font: thử font hệ thống Windows có sẵn (nếu thiếu thì báo thiếu glyph, không vẽ ô vuông âm thầm)
FONT_HE_THONG = {
    "zh-cn": ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"],
    "ko": ["C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/malgun.ttf"],
    "ja": ["C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc"],
}
NGON_NGU_CJK = {"ja", "zh-cn", "ko"}
KHONG_DAU_DONG = set("、。，．,.！？!?」』）)〕】〜～ー…‥ゃゅょっぁぃぅぇぉャュョッァィゥェォ")   # kinsoku: không đứng đầu dòng
KHONG_DAU_DONG_CHUOI = "、。，．」』）〕】！？!?"                                  # dấu câu cuối dòng được "treo" ra mép (không tính vào bề rộng khi cắt dòng)
KHONG_CUOI_DONG = set("「『（(〔【")


def thu_muc_font() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "fonts")


@lru_cache(maxsize=64)
def _nap(duong_dan: str, co: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(duong_dan, co)


def font(duong_dan: str, co: int) -> ImageFont.FreeTypeFont:
    return _nap(duong_dan, int(co))


@lru_cache(maxsize=64)
def _glyph_thieu(duong_dan: str) -> frozenset:
    """Mặt nạ của ký tự 'không có' trong font (để so sánh)."""
    ft = _nap(duong_dan, 40)
    return frozenset(bytes(ft.getmask(c)) for c in ("\ue000", "\uffff", "\U0010fffd"))


def thieu_glyph(duong_dan: str, van_ban: str) -> list[str]:
    """Các ký tự của ``van_ban`` mà font KHÔNG có (rỗng = đủ)."""
    ft = _nap(duong_dan, 40)
    thieu = _glyph_thieu(duong_dan)
    ra = []
    for c in dict.fromkeys(van_ban):
        if c.isspace() or ord(c) < 32:
            continue
        if bytes(ft.getmask(c)) in thieu and c not in ra:
            ra.append(c)
    return ra


def chon_font(vai_tro: str, language: str, van_ban: str = "") -> tuple[Optional[str], list[str]]:
    """(đường dẫn font, ký tự còn thiếu). Duyệt ứng viên đóng gói rồi font hệ thống; lấy font đầu tiên đủ glyph,
    nếu không có font nào đủ thì trả font ít thiếu nhất kèm danh sách ký tự thiếu để giao diện cảnh báo."""
    bang = FONT_DONG_GOI.get(vai_tro, FONT_DONG_GOI["label"])
    ds = [os.path.join(thu_muc_font(), f) for f in bang.get(language, bang["default"])]
    ds += [p for p in FONT_HE_THONG.get(language, [])]
    if language in NGON_NGU_CJK:                              # CJK: font đóng gói ưu tiên, nhưng Latin cũng phải đủ
        ds += [os.path.join(thu_muc_font(), f) for f in bang["default"]]
    tot, it_thieu = None, None
    for p in ds:
        if not os.path.isfile(p):
            continue
        try:
            t = thieu_glyph(p, van_ban)
        except OSError:
            continue
        if not t:
            return p, []
        if it_thieu is None or len(t) < len(it_thieu):
            tot, it_thieu = p, t
    return tot, (it_thieu or [])


def la_cjk(c: str) -> bool:
    o = ord(c)
    return (0x3000 <= o <= 0x9FFF) or (0xAC00 <= o <= 0xD7AF) or (0xFF00 <= o <= 0xFFEF) or (0x20000 <= o <= 0x2FFFF)


def do_rong(ft: ImageFont.FreeTypeFont, s: str) -> float:
    return ImageDraw.Draw(Image.new("L", (4, 4))).textlength(s, font=ft)


def ngat_dong(van_ban: str, ft: ImageFont.FreeTypeFont, rong: float) -> list[str]:
    """Ngắt dòng theo bề rộng pixel. Chữ Latin ngắt ở khoảng trắng (từ quá dài mới cắt cứng); chữ Nhật/Hán ngắt theo ký tự
    kèm luật kinsoku (không để dấu 。、」ở đầu dòng, không để 「（ở cuối dòng)."""
    van_ban = van_ban.replace("\r", "")
    dong: list[str] = []
    for doan in van_ban.split("\n"):
        cur = ""
        # tách thành 'đơn vị': một ký tự CJK, hoặc một từ Latin kèm khoảng trắng đứng trước
        don_vi: list[str] = []
        tu = ""
        for c in doan:
            if la_cjk(c):
                if tu:
                    don_vi.append(tu)
                    tu = ""
                don_vi.append(c)
            elif c == " ":
                if tu:
                    don_vi.append(tu)
                tu = " "
            else:
                tu += c
        if tu:
            don_vi.append(tu)
        for u in don_vi:
            thu = cur + u
            if do_rong(ft, thu.strip()) <= rong or not cur.strip():
                cur = thu
                continue
            if u and u[0] in KHONG_DAU_DONG and len(u) == 1:       # dấu câu: dính vào dòng trước, không xuống dòng
                cur = thu
                continue
            if cur and cur[-1] in KHONG_CUOI_DONG:                # ký tự mở ngoặc: kéo xuống dòng sau cùng đơn vị này
                dong.append(cur[:-1].strip())
                cur = cur[-1] + u.lstrip()
                continue
            dong.append(cur.strip())
            cur = u.lstrip()
        if cur.strip():
            dong.append(cur.strip())
    # từ Latin dài hơn cả dòng: cắt cứng. Dấu câu cuối dòng (。、」…) được "treo" ra mép — không tính vào bề rộng; trước đây tính nên một dòng vượt đúng một dấu bị cắt lại
    # và dấu 。 rơi xuống đầu dòng sau (phạm luật kinsoku mà chính bước trên đã cố giữ)
    ra = []
    for d in dong:
        while do_rong(ft, d.rstrip(KHONG_DAU_DONG_CHUOI)) > rong and len(d) > 1:
            k = len(d)
            while k > 1 and do_rong(ft, d[:k]) > rong:
                k -= 1
            ra.append(d[:k])
            d = d[k:]
        ra.append(d)
    return ra


def ngat_can_bang(van_ban: str, so_dong: int, language: str) -> list[str]:
    """Chia nhãn ngắn thành ``so_dong`` dòng cân nhau (tránh dòng thứ hai chỉ có một chữ)."""
    if so_dong <= 1 or len(van_ban) <= 1:
        return [van_ban]
    if language in NGON_NGU_CJK or not any(c == " " for c in van_ban):
        k = -(-len(van_ban) // so_dong)
        return [van_ban[i:i + k] for i in range(0, len(van_ban), k)][:so_dong]
    tu = van_ban.split()
    muc = sum(len(t) for t in tu) / so_dong
    dong, cur = [], ""
    for t in tu:
        if cur and len(cur) + 1 + len(t) > muc * 1.25 and len(dong) < so_dong - 1:
            dong.append(cur)
            cur = t
        else:
            cur = (cur + " " + t).strip()
    dong.append(cur)
    return dong
