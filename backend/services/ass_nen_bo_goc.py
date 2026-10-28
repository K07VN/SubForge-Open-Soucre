r"""NỀN PHỤ ĐỀ BO GÓC cho bản xuất (``backBoxRadius``, 2.15.4).

libass KHÔNG bo được góc hộp nền: BorderStyle 3/4 luôn vuông (đo thật: ``\bord`` 4/12/24,
cả ``\blur``, ô 12x12 ở góc không hở điểm nào). Nên có bán kính thì lớp nền vẽ bằng HÌNH
VECTOR ASS (``\p1``) — hình chữ nhật bo góc cỡ = khung chữ + lề, neo cùng điểm với chữ.

Cỡ khung chữ phải TỰ ĐO (libass không cho biết). Đo thật với 5 font (Arial, Segoe UI
Black, Montserrat, Be Vietnam Pro, Inter; cỡ 64 @1080p, chữ tiếng Việt):
  · bề ngang: PIL ``getlength`` + ``Spacing`` mỗi ký tự lệch libass -0,4..+7,8px (~1%);
  · bề cao: khung dòng libass = đúng ``Fontsize`` ASS + 4px, cả 5 font.

Dùng từ ``AssGenerator.generate`` (bước hậu kỳ của chế độ "nền hai lớp").
"""
import re
import unicodedata
from functools import lru_cache

#: libass nở hộp BorderStyle 4 thêm chừng này ngoài khung chữ khi đệm = 0 (đo ở trên).
HOP_CONG_NGANG = 4.0
HOP_CONG_DOC = 4.0

_TAG_RE = re.compile(r"(?<!\\)\{[^}]*\}")
_SCALE_RE = re.compile(r"\\(fscx|fscy|fsp)(-?\d+(?:\.\d+)?)")
_NEO_RE = re.compile(r"\{\\an(\d)\\pos\(([-\d.]+),([-\d.]+)\)\}")


@lru_cache(maxsize=64)
def _font(path: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(path, size)


@lru_cache(maxsize=64)
def _co_glyph_cjk(path: str) -> bool:
    """Font có glyph CJK thật không (Noto Sans JP/KR/SC, Zen Maru Gothic… — 08-10-2026): vẽ "中" và so
    với ô .notdef (U+E000, vùng dùng riêng); giống nhau = không có. Có glyph thì bề ngang đo thẳng
    bằng font (đúng), không thì ước lượng theo cỡ font dự phòng (``em_cjk``)."""
    try:
        from PIL import Image, ImageDraw

        ft = _font(path, 40)

        def anh(ch):
            im = Image.new("L", (80, 80), 0)
            ImageDraw.Draw(im).text((8, 8), ch, font=ft, fill=255)
            return im.tobytes()

        return anh("\u4e2d") != anh("\ue000")
    except Exception:  # noqa: BLE001
        return False


def _dai(font, s: str, em: float) -> float:
    """Bề ngang một đoạn chữ. Ký tự TOÀN KHỔ (Hán, kana, Hangul…) tính ``em`` MỖI CHỮ: font
    Latin không có chữ đó thì PIL đo ra ô rỗng .notdef (~0,6–0,75 em) trong khi libass lấy
    font CJK thay vào -> hình nền chỉ bằng nửa dòng chữ tiếng Nhật (rà soát 29-09-2026).

    ``em`` cho chữ toàn khổ PHẢI là cỡ libass đặt cho font dự phòng — tức ``Fontsize`` ASS
    (= cỡ CSS × hệ số metrics của font Latin, thường 1,2–1,35), KHÔNG phải cỡ CSS: libass
    đặt font thay thế theo cùng Fontsize (chiều cao ô chữ) và font CJK có ô chữ ≈ 1 em nên
    chữ Nhật ra to đúng bằng Fontsize. Đưa cỡ CSS vào là nền ngắn hơn chữ 20–35 % (người
    dùng thấy 08-10-2026 với dòng phụ tiếng Nhật). Xem ``do_khung_chu(em_cjk=)``."""
    w, buf = 0.0, []
    #  Font CÓ glyph CJK (Noto…) -> để PIL đo như chữ thường (đúng tới từng glyph).
    co_cjk = _co_glyph_cjk(font.path) if getattr(font, "path", "") else False
    for c in s:
        if not co_cjk and unicodedata.east_asian_width(c) in ("W", "F"):
            if buf:
                w += font.getlength("".join(buf))
                buf = []
            w += em
        else:
            buf.append(c)
    if buf:
        w += font.getlength("".join(buf))
    return w


def do_khung_chu(text: str, font_path: str, em: float, spacing: float, em_cjk: float | None = None) -> tuple:
    r"""``(bề ngang dòng dài nhất, [hệ số cao % của từng dòng])`` của MỘT dòng Dialogue.

    Đọc chính các thẻ trong dòng — ``\fscx``/``\fscy`` (chữ karaoke phóng to), ``\fsp``
    (giãn khoảng giữa hai từ karaoke), ``\N`` (xuống dòng) — nên dùng chung được cho mọi
    chế độ hiển thị (thường / từng chữ / cụm / karaoke)."""
    font = _font(font_path, max(1, int(round(em))))
    #  Chữ toàn khổ (CJK) do font DỰ PHÒNG vẽ ở cỡ Fontsize ASS — xem ``_dai``.
    em_cjk = float(em_cjk) if em_cjk else float(font.size)
    fscx, fscy, fsp = 100.0, 100.0, float(spacing)
    rong = [0.0]
    cao = [100.0]
    pos = 0
    khop = list(_TAG_RE.finditer(text))
    for i in range(len(khop) + 1):
        m = khop[i] if i < len(khop) else None
        doan = text[pos:m.start()] if m else text[pos:]
        doan = doan.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
        for j, dong in enumerate(doan.split("\\N")):
            if j:
                rong.append(0.0)
                cao.append(fscy)
            if dong:
                rong[-1] += (_dai(font, dong, em_cjk) + fsp * len(dong)) * fscx / 100.0
                cao[-1] = max(cao[-1], fscy)
        if not m:
            break
        for ten, gt in _SCALE_RE.findall(m.group(0)):
            if ten == "fscx":
                fscx = float(gt)
            elif ten == "fscy":
                fscy = float(gt)
            else:
                fsp = float(gt)
        pos = m.end()
    return max(rong), cao


def ngat_deu(text: str, font_path: str, em: float, spacing: float, max_w: float,
             em_cjk: float | None = None) -> str:
    r"""Chữ THUẦN dài quá ``max_w`` -> tự chèn ``\N`` chia ĐỀU số dòng (như WrapStyle 0 của
    libass), để lớp chữ và hình nền cùng một cách ngắt. Để libass tự ngắt thì hình nền
    không biết chữ xuống dòng ở đâu. Có thẻ (karaoke…) hoặc đã có ``\N`` thì để nguyên."""
    if _TAG_RE.search(text) or "\\N" in text:
        return text
    rong, _ = do_khung_chu(text, font_path, em, spacing, em_cjk)
    if rong <= max_w:
        return text
    tu = text.split(" ")

    def _rong(t):
        return do_khung_chu(t, font_path, em, spacing, em_cjk)[0]

    def _xep(tran: float) -> list:
        """Xếp tham lam: mỗi dòng dài nhất có thể mà không quá ``tran``."""
        dong, hien = [], []
        for w in tu:
            if hien and _rong(" ".join(hien + [w])) > tran:
                dong.append(" ".join(hien))
                hien = [w]
            else:
                hien.append(w)
        dong.append(" ".join(hien))
        return dong

    #  CHIA CÂN BẰNG như libass: số dòng ÍT NHẤT vừa ``max_w``, rồi tìm (chia đôi) bề
    #  ngang tối đa NHỎ NHẤT vẫn xếp gọn trong chừng đó dòng. Chia kiểu "đều theo tổng"
    #  từng để phần dư dồn xuống dòng cuối (gặp thật: dòng 3 rộng 1068 > 1040) — libass
    #  lại tự ngắt thêm và hình nền không còn khớp chữ.
    so_dong = len(_xep(max_w))
    lo, hi = max(_rong(w) for w in tu), max_w
    for _ in range(24):
        giua = (lo + hi) / 2
        if len(_xep(giua)) <= so_dong:
            hi = giua
        else:
            lo = giua
    return "\\N".join(_xep(hi))


def _so(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def hinh_bo_goc(w: float, h: float, r: float) -> str:
    """Đường vẽ ASS của hình chữ nhật bo góc (Bézier bậc ba, k = 0,5523·r)."""
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0:
        return f"m 0 0 l {_so(w)} 0 l {_so(w)} {_so(h)} l 0 {_so(h)}"
    k = r * 0.5523
    return (
        f"m {_so(r)} 0 l {_so(w - r)} 0 "
        f"b {_so(w - r + k)} 0 {_so(w)} {_so(r - k)} {_so(w)} {_so(r)} "
        f"l {_so(w)} {_so(h - r)} "
        f"b {_so(w)} {_so(h - r + k)} {_so(w - r + k)} {_so(h)} {_so(w - r)} {_so(h)} "
        f"l {_so(r)} {_so(h)} "
        f"b {_so(r - k)} {_so(h)} 0 {_so(h - r + k)} 0 {_so(h - r)} "
        f"l 0 {_so(r)} b 0 {_so(r - k)} {_so(r - k)} 0 {_so(r)} 0"
    )


def dong_nen_bo_goc(text: str, *, font_path: str, em: float, spacing: float,
                    ass_font_size: float, pad_x: float, pad_y: float, radius: float,
                    play_res_x: int, play_res_y: int, line_h: float | None = None) -> tuple:
    r"""Cho Text của MỘT dòng chữ -> ``(text chữ đã ngắt dòng, text lớp nền vector)``.

    Chữ neo ``{\anN\pos(x,y)}`` (``_pos_prefix`` luôn dùng \an4/5/6 — căn giữa dọc). Hình
    nền dùng cùng \an: \an5 cùng tâm; \an4 / \an6 lùi ra ngoài đúng lề + nửa phần libass
    vốn cộng. ``\org`` = điểm neo của CHỮ để hai lớp XOAY quanh cùng một tâm.

    ``line_h`` = khoảng cách giữa hai dòng chữ (px PlayRes) khi lớp chữ được TÁCH TỪNG DÒNG
    (xem ``AssGenerator`` — ``LINE_HEIGHT_EM``); None = để libass tự xếp (mỗi dòng cao đúng
    ``Fontsize``)."""
    m = _NEO_RE.match(text)
    neo = m.group(0) if m else ""
    than = text[m.end():] if m else text
    than = ngat_deu(than, font_path, em, spacing, max(50.0, play_res_x - 40.0), ass_font_size)
    rong, cao = do_khung_chu(than, font_path, em, spacing, ass_font_size)
    w = rong + HOP_CONG_NGANG + 2 * pad_x
    if line_h and len(cao) > 1:
        #  n dòng tách rời cách nhau ``line_h``: hộp = (n-1)·line_h + một ô chữ (dòng cao nhất).
        h = (len(cao) - 1) * line_h + ass_font_size * max(cao) / 100.0 + HOP_CONG_DOC + 2 * pad_y
    else:
        h = sum(ass_font_size * c / 100.0 for c in cao) + HOP_CONG_DOC + 2 * pad_y
    an = int(m.group(1)) if m else 5
    x = float(m.group(2)) if m else play_res_x / 2
    y = float(m.group(3)) if m else play_res_y / 2
    lech = pad_x + HOP_CONG_NGANG / 2
    x2 = x - lech if an in (1, 4, 7) else (x + lech if an in (3, 6, 9) else x)
    nen = (
        f"{{\\an{an}\\pos({_so(x2)},{_so(y)})\\org({_so(x)},{_so(y)})\\p1}}"
        f"{hinh_bo_goc(w, h, radius)}{{\\p0}}"
    )
    return neo + than, nen
