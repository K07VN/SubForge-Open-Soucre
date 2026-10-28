"""Resolve system font files for ASS / FFmpeg drawtext on Windows."""
import os
import sys

# FONT ĐÓNG GÓI KÈM APP (backend/assets/fonts) — xem NGUON.md ở đó.
#
# VÌ SAO CÓ: khung xem trước chạy trong Chrome nên tải được font Google qua
# CDN, còn libass lúc xuất chỉ quét C:/Windows/Fonts -> chọn Montserrat thì
# preview đúng mà video ra Arial, im lặng. Năm mặt chữ này đi kèm bản cài nên
# HAI BÊN DÙNG ĐÚNG MỘT FILE.
#
# Khoá là TÊN HỌ ghi trong chính file font (name ID 1) — libass tra theo tên
# đó, đặt khác đi là nó không tìm ra. Họ font tiêu đề chỉ gói MỘT mặt (đậm nhất);
# riêng họ chữ thường gói đủ bốn mặt thường/đậm/nghiêng/đậm-nghiêng — xem
# BUNDLED_FONT_FACES. Bảng này ghi tệp "đại diện" (họ bốn mặt: mặt thường).
# Mirror phía FE: BUNDLED_EXPORT_FONTS (frontend/src/constants/exportFonts.ts)
# và @font-face trong frontend/src/index.css.
BUNDLED_FONT_FILES: dict[str, str] = {
    "Alfa Slab One": "AlfaSlabOne-Regular.ttf",
    "Anton": "Anton-Regular.ttf",
    "Athiti": "Athiti-Bold.ttf",
    "Bai Jamjuree": "BaiJamjuree-Bold.ttf",
    "Barlow Condensed ExtraBold": "BarlowCondensed-ExtraBold.ttf",
    "Be Vietnam Pro": "BeVietnamPro-Regular.ttf",
    "Be Vietnam Pro Black": "BeVietnamPro-Black.ttf",
    "Bevan": "Bevan-Regular.ttf",
    "Bungee": "Bungee.ttf",
    "Bungee Inline": "BungeeInline-Regular.ttf",
    "Bungee Shade": "BungeeShade-Regular.ttf",
    "Chakra Petch": "ChakraPetch-Bold.ttf",
    "Charm": "Charm-Bold.ttf",
    "Charmonman": "Charmonman-Bold.ttf",
    "Chonburi": "Chonburi-Regular.ttf",
    "Coiny": "Coiny-Regular.ttf",
    "Fahkwang": "Fahkwang-Bold.ttf",
    "Fjalla One": "FjallaOne-Regular.ttf",
    "Inter": "Inter-Regular.ttf",
    "Itim": "Itim-Regular.ttf",
    "K2D": "K2D-Bold.ttf",
    "Kanit Black": "Kanit-Black.ttf",
    "KoHo": "KoHo-Bold.ttf",
    "Kodchasan": "Kodchasan-Bold.ttf",
    "Krub": "Krub-Bold.ttf",
    "Lobster": "Lobster-Regular.ttf",
    "Mali": "Mali-Bold.ttf",
    "Mitr": "Mitr-Bold.ttf",
    "Montserrat": "Montserrat-Regular.ttf",
    "Montserrat Black": "Montserrat-Black.ttf",
    "Niramit": "Niramit-Bold.ttf",
    "Noto Sans JP": "NotoSansJP-Regular.otf",
    "Noto Sans KR": "NotoSansKR-Regular.otf",
    "Noto Sans SC": "NotoSansSC-Regular.otf",
    "Oswald": "Oswald-Bold.ttf",
    "Pacifico": "Pacifico-Regular.ttf",
    "Pattaya": "Pattaya-Regular.ttf",
    "Paytone One": "PaytoneOne-Regular.ttf",
    "Philosopher": "Philosopher-Bold.ttf",
    "Playfair Display": "PlayfairDisplay-Regular.ttf",
    "Pridi": "Pridi-Bold.ttf",
    "Prompt Black": "Prompt-Black.ttf",
    "Rowdies": "Rowdies-Bold.ttf",
    "Saira Condensed ExtraBold": "SairaCondensed-ExtraBold.ttf",
    "Sarabun ExtraBold": "Sarabun-ExtraBold.ttf",
    "Sedgwick Ave": "SedgwickAve-Regular.ttf",
    "Sriracha": "Sriracha-Regular.ttf",
    "Srisakdi": "Srisakdi-Bold.ttf",
    "Taviraj": "Taviraj-Bold.ttf",
    "Trirong": "Trirong-Bold.ttf",
    "Zen Maru Gothic": "ZenMaruGothic-Bold.ttf",
}


#  HỌ BỐN MẶT (thường/đậm/nghiêng/đậm-nghiêng) — font chữ thường mà nút Đậm/Nghiêng
#  phải có tác dụng (Inter, Montserrat, Playfair Display, Be Vietnam Pro — thêm
#  24-09-2026). Phụ đề đi libass + ``fontsdir`` nên tự chọn mặt theo cờ; còn CHỮ CHÈN
#  vẽ bằng drawtext với ĐÚNG MỘT tệp -> phải tra bảng này, không thì bật nghiêng mà
#  video vẫn ra chữ đứng. SINH TỰ ĐỘNG bởi scripts/sinh_danh_sach_font.py.
BUNDLED_FONT_FACES: dict[str, dict[str, str]] = {
    "Be Vietnam Pro": {"regular": "BeVietnamPro-Regular.ttf", "bold": "BeVietnamPro-Bold.ttf", "italic": "BeVietnamPro-Italic.ttf", "bold_italic": "BeVietnamPro-BoldItalic.ttf"},
    "Inter": {"regular": "Inter-Regular.ttf", "bold": "Inter-Bold.ttf", "italic": "Inter-Italic.ttf", "bold_italic": "Inter-BoldItalic.ttf"},
    "Montserrat": {"regular": "Montserrat-Regular.ttf", "bold": "Montserrat-Bold.ttf", "italic": "Montserrat-Italic.ttf", "bold_italic": "Montserrat-BoldItalic.ttf"},
    "Noto Sans JP": {"regular": "NotoSansJP-Regular.otf", "bold": "NotoSansJP-Bold.otf", "italic": "NotoSansJP-Regular.otf", "bold_italic": "NotoSansJP-Bold.otf"},
    "Noto Sans KR": {"regular": "NotoSansKR-Regular.otf", "bold": "NotoSansKR-Bold.otf", "italic": "NotoSansKR-Regular.otf", "bold_italic": "NotoSansKR-Bold.otf"},
    "Noto Sans SC": {"regular": "NotoSansSC-Regular.otf", "bold": "NotoSansSC-Bold.otf", "italic": "NotoSansSC-Regular.otf", "bold_italic": "NotoSansSC-Bold.otf"},
    "Playfair Display": {"regular": "PlayfairDisplay-Regular.ttf", "bold": "PlayfairDisplay-Bold.ttf", "italic": "PlayfairDisplay-Italic.ttf", "bold_italic": "PlayfairDisplay-BoldItalic.ttf"},
}


def bundled_fonts_dir() -> str:
    """Thư mục font đóng gói — chạy dev lẫn bản đóng gói (PyInstaller _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "assets", "fonts")


def bundled_font_path(family: str, bold: bool = False, italic: bool = False) -> str:
    """Đường dẫn file font đóng gói của họ này, rỗng nếu không phải font đóng gói.

    Họ bốn mặt (BUNDLED_FONT_FACES) trả đúng mặt theo ``bold``/``italic``; họ một mặt
    bỏ qua hai cờ (mặt đó đã nặng sẵn — xem _HEAVY_FONTS)."""
    fam = (family or "").strip()
    mat = BUNDLED_FONT_FACES.get(fam)
    if mat:
        khoa = ("bold_italic" if italic else "bold") if bold else ("italic" if italic else "regular")
        path = os.path.join(bundled_fonts_dir(), mat.get(khoa, ""))
        if mat.get(khoa) and os.path.exists(path):
            return path
    filename = BUNDLED_FONT_FILES.get(fam)
    if not filename:
        return ""
    path = os.path.join(bundled_fonts_dir(), filename)
    return path if os.path.exists(path) else ""

# Tên file trong C:/Windows/Fonts (một số font Google cần upload .ttf riêng)
WINDOWS_FONT_FILES: dict[str, str] = {
    "Segoe UI": "segoeui.ttf",
    "Segoe UI Black": "seguibl.ttf",
    "Tahoma": "tahoma.ttf",
    "Verdana": "verdana.ttf",
    "Arial": "arial.ttf",
    "Arial Black": "ariblk.ttf",
    "Impact": "impact.ttf",
    "Trebuchet MS": "trebuc.ttf",
    "Georgia": "georgia.ttf",
    "Comic Sans MS": "comic.ttf",
    "Times New Roman": "times.ttf",
    "Calibri": "calibri.ttf",
    "Calibri Bold": "calibrib.ttf",
    "Calibri Italic": "calibrii.ttf",
    "Arial Bold": "arialbd.ttf",
    "Arial Italic": "ariali.ttf",
    "Arial Bold Italic": "arialbi.ttf",
}


# Mặt BLACK (nặng hơn Bold) của các họ font hệ thống — CHỈ liệt kê họ có mặt
# Black cài sẵn trong Windows. Khi người dùng bật "đậm", preview (Chrome qua
# DirectWrite) render weight 900 = mặt Black, còn libass trong ffmpeg KHÔNG hiểu
# ``Bold: 900`` (đo thật 2026-08-27: Bold=900 ra y hệt Bold=-1 → mặt 700) — muốn
# ra mặt Black phải gọi thẳng TÊN HỌ Black trong ASS Fontname. Mirror phía FE:
# ``BLACK_WEIGHT_FONTS`` trong subtitleTextStyle.ts.
BLACK_VARIANTS: dict[str, str] = {
    "Segoe UI": "Segoe UI Black",
    "Arial": "Arial Black",
}


#  CHỮ KIRIN (tiếng Nga) — font đóng gói nào CÓ đủ 66 chữ cái А–я/Ёё.
#
#  ĐO THẬT 2026-09-23 (dựng từng chữ rồi so ảnh với ô .notdef, cùng cách với
#  test_youtube_thumbnail): chỉ 6/43 font đóng gói có; 37 font còn lại THIẾU TOÀN BỘ,
#  (24-09-2026 thêm họ bốn mặt: Inter, Montserrat, Playfair Display CÓ đủ cả bốn mặt;
#  Be Vietnam Pro thì không — cùng đo bằng cách trên)
#  gồm cả mấy font dày hay dùng cho phụ đề (Anton, Kanit Black, Be Vietnam Pro
#  Black, Bungee…). Mọi font Windows trong WINDOWS_FONT_FILES đều có đủ.
#  Thiếu mà cứ đốt thì libass mượn font khác cho TỪNG chữ -> dòng chữ mỗi ký tự một
#  kiểu, không báo gì (cùng bệnh với font thiếu dấu tiếng Việt).
#  Mirror phía FE: CYRILLIC_BUNDLED_FONTS + cyrillicFallbackFont (exportFonts.ts).
CYRILLIC_BUNDLED_FONTS = frozenset({
    "Inter", "Lobster", "Montserrat", "Montserrat Black", "Oswald", "Pacifico", "Pattaya",
    "Philosopher", "Playfair Display",
    #  Font CJK (08-10-2026) cũng có đủ chữ Kirin (đo thật qua test_font_cyrillic).
    "Noto Sans JP", "Noto Sans KR", "Noto Sans SC", "Zen Maru Gothic",
})
#  Font THAY THẾ khi văn bản có chữ Kirin: giữ gần nhất dáng chữ đã chọn — font HẸP
#  cao sang Oswald, font DÀY/đậm sang Montserrat Black, còn lại sang Segoe UI (font
#  mặc định của app, Windows nào cũng có, tự lên mặt Black khi bật đậm).
_CYRILLIC_CONDENSED = frozenset({
    "Anton", "Fjalla One", "Barlow Condensed ExtraBold", "Saira Condensed ExtraBold",
})
_CYRILLIC_HEAVY = frozenset({
    "Alfa Slab One", "Be Vietnam Pro Black", "Bevan", "Bungee", "Bungee Inline",
    "Bungee Shade", "Chonburi", "Coiny", "Kanit Black", "Paytone One", "Prompt Black",
    "Rowdies", "Sarabun ExtraBold",
})


def has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in (text or ""))


def cyrillic_fallback_font(family: str) -> str:
    """Font nên dùng thay ``family`` khi phải vẽ chữ Kirin. Trả nguyên ``family`` nếu
    nó đã có đủ chữ Kirin, hoặc là font LẠ (người dùng tự cài / tải lên) — font lạ
    thì không đo được, đổi bừa còn tệ hơn."""
    f = (family or "").strip()
    if f not in BUNDLED_FONT_FILES or f in CYRILLIC_BUNDLED_FONTS:
        return family
    if f in _CYRILLIC_CONDENSED:
        return "Oswald"
    if f in _CYRILLIC_HEAVY:
        return "Montserrat Black"
    return "Segoe UI"


#  CHỮ NHẬT / TRUNG / HÀN (08-10-2026) — font đóng gói nào CÓ chữ CJK: ba họ Noto Sans (JP / KR / SC,
#  OTF, thường + đậm) thêm cùng ngày, và Zen Maru Gothic (Nhật). Mọi font đóng gói khác là font Latin,
#  gặp chữ CJK thì libass mượn font hệ thống (Yu Gothic…) ở cỡ Fontsize ASS -> mỗi máy một mặt chữ,
#  máy không có font CJK ra ô vuông, preview lại mượn font khác nữa. Nay đổi sang Noto theo CHỮ VIẾT:
#  kana -> JP, Hangul -> KR, chỉ Hán -> SC. Mirror phía FE: CJK_BUNDLED_FONTS + cjkFallbackFont
#  (exportFonts.ts). Test: tests/test_font_cjk.py.
CJK_BUNDLED_FONTS = frozenset({"Noto Sans JP", "Noto Sans KR", "Noto Sans SC", "Zen Maru Gothic"})
#  Tiếng Nhật = Zen Maru Gothic: font chế độ Sáng tạo đang dùng, người dùng khen dễ đọc (08-10-2026) và chốt làm
#  mặc định cho Cơ bản / Chi tiết. Nó thiếu nhiều chữ giản thể (测 试 简 闯 发 关) và không có Hangul -> Trung / Hàn vẫn Noto.
CJK_FONT_JP = "Zen Maru Gothic"
CJK_FONT_KR = "Noto Sans KR"
CJK_FONT_SC = "Noto Sans SC"


def _la_kana(ch: str) -> bool:
    return "\u3040" <= ch <= "\u30ff" or "\u31f0" <= ch <= "\u31ff" or "\uff66" <= ch <= "\uff9f"


def _la_hangul(ch: str) -> bool:
    return "\uac00" <= ch <= "\ud7af" or "\u1100" <= ch <= "\u11ff" or "\u3130" <= ch <= "\u318f"


def _la_han(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or "\uf900" <= ch <= "\ufaff"


def has_cjk(text: str) -> bool:
    return any(_la_kana(c) or _la_hangul(c) or _la_han(c) for c in (text or ""))


def cjk_script(texts) -> str:
    """"ja" / "ko" / "zh" / "" theo chữ viết có trong ``texts``: kana -> ja (ưu tiên, vì tiếng Nhật
    luôn có kana xen Hán), Hangul -> ko, chỉ Hán -> zh, không có CJK -> ""."""
    co_kana = co_hangul = co_han = False
    for t in texts or []:
        for c in t or "":
            if _la_kana(c):
                co_kana = True
            elif _la_hangul(c):
                co_hangul = True
            elif _la_han(c):
                co_han = True
    if co_kana:
        return "ja"
    if co_hangul:
        return "ko"
    return "zh" if co_han else ""


def cjk_fallback_font(family: str, texts) -> str:
    """Font nên dùng thay ``family`` khi văn bản có chữ CJK. Giữ nguyên nếu không có chữ CJK, nếu
    ``family`` đã là font CJK đóng gói, hoặc là font LẠ / font hệ thống (không đo được, người dùng
    có thể cố ý chọn Yu Gothic…) — cùng luật với ``cyrillic_fallback_font``."""
    f = (family or "").strip()
    #  Khác luật Kirin ở một điểm: font HỆ THỐNG (Segoe UI, Arial… — WINDOWS_FONT_FILES) cũng đổi, vì chúng chắc chắn
    #  không có chữ CJK (libass sẽ mượn Yu Gothic mỗi máy một kiểu) và "Segoe UI" là font MẶC ĐỊNH của mọi dự án.
    #  Font lạ (người dùng tự cài, vd Yu Gothic) giữ nguyên.
    if f in CJK_BUNDLED_FONTS or (f not in BUNDLED_FONT_FILES and f not in WINDOWS_FONT_FILES):
        return family
    chu = cjk_script(texts)
    if chu == "ja":
        return CJK_FONT_JP
    if chu == "ko":
        return CJK_FONT_KR
    if chu == "zh":
        return CJK_FONT_SC
    return family


def font_mac_dinh_theo_tieng(lang: str) -> dict:
    """``subtitle_defaults`` cho dự án MỚI theo ngôn ngữ ĐÍCH: Nhật -> Zen Maru Gothic, Trung -> Noto Sans SC, Hàn ->
    Noto Sans KR; thứ tiếng khác -> {} (giữ mặc định chung). Chỉ có tác dụng khi dự án chưa tự lưu kiểu chữ
    (``load_compositor_config``) — dự án cũ không đổi. Người dùng 08-10-2026: "chữ Nhật ở Sáng tạo dễ đọc, đặt làm
    mặc định cho Cơ bản / Chi tiết"."""
    l = (lang or "").strip().lower().replace("_", "-")
    if l.startswith("ja"):
        return {"fontFamily": CJK_FONT_JP}
    if l.startswith("zh"):
        return {"fontFamily": CJK_FONT_SC}
    if l.startswith("ko"):
        return {"fontFamily": CJK_FONT_KR}
    return {}


def effective_ass_font(family: str, bold: bool) -> tuple[str, bool]:
    """(Họ font ghi vào ASS, cờ bold còn lại) sau khi nâng lên mặt Black.

    Bật đậm + họ có mặt Black cài thật -> dùng thẳng họ Black và TẮT cờ bold
    (mặt đã nặng sẵn, để Bold=-1 nữa là libass giả-đậm làm bệt nét). Không có
    mặt Black thì giữ nguyên họ + cờ bold (libass tự lấy mặt 700).
    """
    if bold:
        black = BLACK_VARIANTS.get(family)
        if black and _font_path(WINDOWS_FONT_FILES.get(black, "")):
            return black, False
    return family, bold


def resolve_windows_font(family: str, bold: bool = False, italic: bool = False) -> str:
    if sys.platform != "win32":
        return ""
    if family in WINDOWS_FONT_FILES:
        filename = WINDOWS_FONT_FILES[family]
    else:
        base = family
        if bold and italic:
            for key in (f"{base} Bold Italic", f"{base} Italic Bold"):
                if key in WINDOWS_FONT_FILES:
                    return _font_path(WINDOWS_FONT_FILES[key])
            filename = WINDOWS_FONT_FILES.get("Arial Bold Italic", "arialbi.ttf")
        elif bold:
            for key in (f"{base} Bold", "Arial Bold", "Arial Black"):
                if key in WINDOWS_FONT_FILES:
                    return _font_path(WINDOWS_FONT_FILES[key])
            filename = "arialbd.ttf"
        elif italic:
            for key in (f"{base} Italic", "Arial Italic"):
                if key in WINDOWS_FONT_FILES:
                    return _font_path(WINDOWS_FONT_FILES[key])
            filename = "ariali.ttf"
        else:
            filename = WINDOWS_FONT_FILES.get(base, "arial.ttf")
    return _font_path(filename)


def _font_path(filename: str) -> str:
    path = os.path.join("C:/Windows/Fonts", filename)
    return path if os.path.exists(path) else ""


def resolve_windows_font_legacy(family: str) -> str:
    return resolve_windows_font(family, False, False)
