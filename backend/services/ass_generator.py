"""
ASS (Advanced SubStation Alpha) generator for SubForge compositor pipeline.
Supports 3 subtitle modes: normal, word-by-word, karaoke.
"""
import re
from typing import Any

from services.ass_nen_bo_goc import dong_nen_bo_goc
from utils.font_metrics import ass_fontsize_scale
from utils.font_resolver import effective_ass_font
from utils.overlay_coords import source_rect_to_output_norm

# Font TỰ THÂN ĐÃ NẶNG -> không bật cờ Bold nữa (libass giả-đậm chồng lên mặt
# đã nặng sẽ bệt nét). Năm font đóng gói đều chỉ có MỘT mặt và mặt đó là
# Black/Bold sẵn, nên nằm hết ở đây.
# Mirror: HEAVY_FONTS trong frontend/src/utils/subtitleTextStyle.ts.
_HEAVY_FONTS = frozenset({
    "Arial Black", "Impact", "Segoe UI Black",
    "Alfa Slab One",
    "Anton",
    "Athiti",
    "Bai Jamjuree",
    "Barlow Condensed ExtraBold",
    "Be Vietnam Pro Black",
    "Bevan",
    "Bungee",
    "Bungee Inline",
    "Bungee Shade",
    "Chakra Petch",
    "Charm",
    "Charmonman",
    "Chonburi",
    "Coiny",
    "Fahkwang",
    "Fjalla One",
    "Itim",
    "K2D",
    "Kanit Black",
    "KoHo",
    "Kodchasan",
    "Krub",
    "Lobster",
    "Mali",
    "Mitr",
    "Montserrat Black",
    "Niramit",
    "Oswald",
    "Pacifico",
    "Pattaya",
    "Paytone One",
    "Philosopher",
    "Pridi",
    "Prompt Black",
    "Rowdies",
    "Saira Condensed ExtraBold",
    "Sarabun ExtraBold",
    "Sedgwick Ave",
    "Sriracha",
    "Srisakdi",
    "Taviraj",
    "Trirong",
    "Zen Maru Gothic",
})
_DEFAULT_BOX_H = 0.18


def _hex_to_ass_color(hex_color: str, alpha: int = 0) -> str:
    """Convert #RRGGBB to ASS &HAABBGGRR format."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def _split_words(text: str) -> list[str]:
    return [w for w in re.split(r"(\s+)", text) if w.strip()]


# Karaoke "lead": đẩy highlight đi trước âm thanh một chút. Mắt người nhận biết
# chữ nhanh hơn tai nghe âm, nên nếu chia đều đúng theo audio thì cảm giác chữ bị
# CHẬM hơn giọng đọc. Dịch mốc chuyển chữ sớm lên ~0.12s cho khớp cảm giác.
# Giữ đồng bộ với KARAOKE_LEAD_SEC bên frontend (karaokeWindow.ts).
KARAOKE_LEAD_SEC = 0.2


def _estimate_word_timings(start: float, end: float, words: list[str]) -> list[tuple[str, float, float]]:
    if not words:
        return []
    duration = max(end - start, 0.1)
    per_word = duration / len(words)
    # Mốc chuyển giữa các chữ, dịch sớm lên lead (trừ mốc đầu = start và mốc cuối
    # = end để phụ đề vẫn phủ trọn khoảng segment).
    result = []
    for i, word in enumerate(words):
        ws = start if i == 0 else max(start, start + i * per_word - KARAOKE_LEAD_SEC)
        we = end if i == len(words) - 1 else max(ws, start + (i + 1) * per_word - KARAOKE_LEAD_SEC)
        result.append((word, ws, we))
    return result


def _clamp_chunk_word_count(n: Any) -> int:
    # Mirror clampChunkWordCount (karaokeWindow.ts): 0/NaN -> 3, ROUND rồi kẹp
    # 1..20 — trước đây int() truncate khiến 2.6 hiện 3 chữ ở preview nhưng
    # xuất 2 chữ. Trần 20 phải KHỚP MAX_CHUNK_WORDS bên kia: lệch là ô nhập cho
    # gõ 12 mà xuất ra vẫn cắt còn 10, không có gì báo.
    try:
        v = float(n)
    except (TypeError, ValueError):
        v = 0.0
    if not v:
        v = 3.0
    return max(1, min(20, int(round(v))))


def _estimate_group_timings(
    start: float, end: float, words: list[str], chunk_size: int
) -> list[tuple[str, float, float]]:
    """Chia words thành các cụm N chữ liên tiếp (8 chữ, N=3 → 3-3-2), thời gian
    mỗi cụm tỷ lệ theo số chữ. Mốc chuyển dịch sớm KARAOKE_LEAD_SEC như
    _estimate_word_timings — giữ đồng bộ với getChunkedSlice (karaokeWindow.ts)."""
    if not words:
        return []
    n = _clamp_chunk_word_count(chunk_size)
    duration = max(end - start, 0.1)
    per_word = duration / len(words)
    groups = [words[i : i + n] for i in range(0, len(words), n)]
    result = []
    consumed = 0
    for gi, group in enumerate(groups):
        gs = start if gi == 0 else max(start, start + consumed * per_word - KARAOKE_LEAD_SEC)
        consumed += len(group)
        ge = end if gi == len(groups) - 1 else max(gs, start + consumed * per_word - KARAOKE_LEAD_SEC)
        result.append((" ".join(group), gs, ge))
    return result


def _effective_font_size(style: dict[str, Any]) -> int:
    base = float(style.get("fontSize", 56))
    box_h = float(style.get("boxH", _DEFAULT_BOX_H))
    return max(12, int(round(base * (box_h / _DEFAULT_BOX_H))))


def _ass_bold_flag(style: dict[str, Any], font: str) -> int:
    if font in _HEAVY_FONTS:
        return 0
    return -1 if style.get("bold", True) else 0


def _pos_prefix(
    style: dict[str, Any],
    crop: dict[str, Any],
    play_res_x: int,
    play_res_y: int,
) -> str:
    """Neo sub KHOP PREVIEW.

    Preview LUON can giua chu theo chieu DOC trong hop sub
    (flex items-center justify-center) va chi dung `alignment` cho can le NGANG
    (trai/giua/phai) qua text-align. Vi vay export cung phai neo o TAM DOC cua
    hop (oy+oh/2) va chon \\an theo COT ngang -> 4/5/6 (mid-left/center/right).

    Bug cu: _anchor_px anh xa hang bi DAO (alignment 2 = day trong ASS nhung
    tinh py=oy = DINH hop) khien chu bi day vot len tren hop -> sub xuat lech
    cao hon preview.
    """
    ox, oy, ow, oh = source_rect_to_output_norm(
        float(style.get("posX", 0.05)),
        float(style.get("posY", 0.78)),
        float(style.get("boxW", 0.9)),
        float(style.get("boxH", 0.1)),
        crop,
    )
    col = (int(style.get("alignment", 2)) - 1) % 3
    if col == 0:
        px, an = ox, 4
    elif col == 1:
        px, an = ox + ow / 2, 5
    else:
        px, an = ox + ow, 6
    py = oy + oh / 2
    ax, ay = int(px * play_res_x), int(py * play_res_y)
    return f"{{\\an{an}\\pos({ax},{ay})}}"


def _display_text(text: str, uppercase: bool) -> str:
    return text.upper() if uppercase else text


#: KHOẢNG CÁCH DÒNG của preview (CSS ``lineHeight: 1.15`` — mirror frontend/src/utils/subtitleTextStyle.ts).
#: libass xếp các dòng cách nhau đúng ``Fontsize`` ASS = cỡ CSS × hệ số metrics của font (1,2–1,6 tuỳ font),
#: nên câu hai dòng xuất ra thưa hơn preview 5–40 % (người dùng thấy 08-10-2026). Chế độ nền hai lớp tách
#: từng dòng thành một Dialogue riêng, neo cách nhau ``LINE_HEIGHT_EM × cỡ CSS`` — y như preview.
LINE_HEIGHT_EM = 1.15
_NEO_DONG_RE = re.compile(r"^\{\\an(\d)\\pos\((-?[\d.]+),(-?[\d.]+)\)\}")


def tach_tung_dong(text: str, line_h: float) -> list[str]:
    r"""``{\anN\pos(x,y)}L1\NL2…`` -> mỗi dòng một Text riêng, neo cùng \an, y rải quanh y gốc
    (khối n dòng vẫn căn giữa dọc tại y như trước). Có thẻ override khác (karaoke…) hoặc một dòng
    -> trả nguyên."""
    m = _NEO_DONG_RE.match(text)
    if not m:
        return [text]
    than = text[m.end():]
    if "\\N" not in than or "{" in than:
        return [text]
    dong = than.split("\\N")
    n = len(dong)
    an, x, y = m.group(1), m.group(2), float(m.group(3))
    y0 = y - (n - 1) * line_h / 2.0
    ra = []
    for i, d in enumerate(dong):
        yi = y0 + i * line_h
        ra.append(f"{{\\an{an}\\pos({x},{yi:.1f})}}{d}")
    return ra


def _clamp_karaoke_window(size: Any) -> int:
    # Mirror clampKaraokeWindow (karaokeWindow.ts): 0/NaN -> 3, ROUND (không
    # truncate) rồi kẹp 2..20 — lệch là preview và video xuất hiện khác số chữ.
    # Trần 20 (trước là 6) khớp MAX_KARAOKE_WINDOW bên kia và MAX_CHUNK_WORDS.
    try:
        n = float(size)
    except (TypeError, ValueError):
        n = 0.0
    if not n:
        n = 3.0
    return max(2, min(20, int(round(n))))


def _karaoke_window_slice(
    words: list[str], active_idx: int, window_size: int
) -> tuple[list[str], int, int]:
    """Cửa sổ karaoke: N chữ đang hiện + vị trí chữ đang đọc trong cửa sổ.

    Cửa sổ nhảy theo CỤM (0-2, 3-5, 6-8…), KHÔNG canh giữa quanh chữ đang đọc.
    Bản cũ đặt ``start = idx - size // 2`` nên chữ đang đọc luôn ở chính giữa:
    vệt sáng đứng yên còn chữ trượt bên dưới, người xem không thấy chuyển động.
    Nhảy theo cụm thì highlight đi hết trái → giữa → phải rồi mới sang cụm sau.

    Cụm cuối có thể thiếu chữ (8 chữ, N=3 → 3-3-2) — cố nhồi cho đủ bằng cách
    lùi ``start`` sẽ lặp chữ của cụm trước và làm highlight giật về bên phải.

    Mirror getKaraokeWindowSlice (frontend/src/utils/karaokeWindow.ts).
    """
    if not words:
        return [], 0, 0
    size = min(_clamp_karaoke_window(window_size), len(words))
    idx = max(0, min(active_idx, len(words) - 1))
    start = (idx // size) * size
    return words[start : start + size], start, idx - start


class AssGenerator:
    @staticmethod
    def generate(
        segments: list[dict[str, Any]],
        style: dict[str, Any],
        play_res_x: int = 1080,
        play_res_y: int = 1920,
        crop_frame: dict[str, Any] | None = None,
        font_path: str | None = None,
    ) -> str:
        mode = style.get("mode", "normal")
        # Bật đậm -> nâng lên mặt BLACK theo tên họ (nếu Windows có cài): preview
        # đặt weight 900 nên trình duyệt chọn mặt Black, còn libass không hiểu
        # Bold=900 — phải gọi thẳng "Segoe UI Black"/"Arial Black" trong Fontname.
        # Xem effective_ass_font (font_resolver.py); mirror FE: BLACK_WEIGHT_FONTS.
        # (cờ bold trả về bỏ qua được: họ Black đã nằm trong _HEAVY_FONTS nên
        # _ass_bold_flag tự trả 0 — không giả-đậm chồng lên mặt đã nặng.)
        font, _ = effective_ass_font(
            style.get("fontFamily", "Arial Black"), bool(style.get("bold", True))
        )
        # ``font_size`` là cỡ NHÌN THẤY (CSS em px, cùng hệ với preview) — mọi phép
        # suy từ cỡ chữ (Spacing, \fsp) dùng con số này. Riêng Style ``Fontsize``
        # phải nhân hệ số metrics của font: libass hiểu Fontsize là CHIỀU CAO Ô CHỮ
        # (winAscent+winDescent) chứ không phải cỡ em như CSS — không nhân thì chữ
        # xuất ra nhỏ hơn preview (~25% với Segoe UI). Xem utils/font_metrics.py.
        font_size = _effective_font_size(style)
        ass_font_size = max(12, int(round(font_size * ass_fontsize_scale(font_path))))
        crop = crop_frame or {}
        bold = _ass_bold_flag(style, font)
        italic = -1 if style.get("italic", False) else 0
        # DAY VIEN = DUNG so nguoi dung dat, khong bot va khong lam tron.
        #
        # Ban cu: ``int(round(outlineWidth * 0.85))``. Hai phep nay cong lai lam chu xuat
        # ra MONG HON HAN preview:
        #   · ``* 0.85``  bot thang 15% do day.
        #   · ``int(round(...))`` ep ve SO NGUYEN — vien 1.5 thanh 1 (mat them 33%),
        #     vien 2.5 thanh 2. ASS/libass nhan duoc so thap phan nen khong co ly do gi
        #     phai lam tron.
        # Vien 1.5 vi the xuat ra chi con 1.0 = 67% do day dat trong preview.
        #
        # Preview (``subtitleTextStyle.ts``) nay dung THANG so nay nhan previewScale —
        # hai ben cung mot con so, khong con he so bi an nao.
        outline_raw = float(style.get("outlineWidth", 3))
        outline = round(max(0.0, outline_raw), 2) if outline_raw > 0 else 0
        shadow_depth = int(style.get("shadowDepth", 2) or 0)
        shadow = max(2, shadow_depth) if shadow_depth > 0 else 2
        primary_hex = style.get("primaryColor", "#FFE600")
        secondary_hex = style.get("secondaryColor", "#FFFFFF")
        primary = _hex_to_ass_color(primary_hex)
        secondary = _hex_to_ass_color(secondary_hex)
        if mode in ("normal", "chunked"):
            # Hai chế độ này vẽ chữ tĩnh bằng màu phụ (giống preview) — hoán màu.
            primary = _hex_to_ass_color(secondary_hex)
            secondary = _hex_to_ass_color(primary_hex)
        uppercase = style.get("uppercase", False)
        # Giãn chữ: preview đặt letterSpacing 0.02em (hoa) / 0.01em (thường) —
        # ASS Spacing tính bằng pixel ở thang PlayRes nên phải quy từ em ra px
        # theo cỡ chữ. Trước đây hardcode 0 -> chữ xuất ra bó sát hơn preview.
        spacing = round(font_size * (0.02 if uppercase else 0.01), 2)
        # DAO DAU: CSS rotate() quay THUAN chieu kim dong ho voi goc duong,
        # con ASS Angle quay NGUOC chieu — khong dao thi sub xuat nghieng
        # nguoc huong so voi preview.
        rotation = -float(style.get("rotation", 0) or 0)
        pos_tag = _pos_prefix(style, crop, play_res_x, play_res_y)
        karaoke_style = style.get("karaokeStyle", "active-only")
        karaoke_window = _clamp_karaoke_window(style.get("karaokeWindowSize", 3))
        # Karaoke "pop": phóng to chữ đang đọc để tạo cảm giác nhảy từng chữ.
        karaoke_pop = bool(style.get("karaokePop", False))
        POP_SCALE = 118  # %
        alignment = style.get("alignment", 2)
        back_box = style.get("backBox", False)
        if back_box:
            # BorderStyle 4 (libass): hộp nền vẽ bằng BackColour ĐÚNG màu + độ
            # mờ người dùng chọn, viền chữ (Outline) vẫn được vẽ — khớp preview.
            # (BorderStyle 3 cũ tô hộp bằng OutlineColour đen đặc: chọn màu nào
            # cũng ra hộp đen, và mất luôn viền chữ.)
            back_color = _hex_to_ass_color(
                style.get("backBoxColor", "#000000"),
                alpha=int((1 - style.get("backBoxOpacity", 0.6)) * 255),
            )
            border_style = 4
        else:
            # Không hộp nền: BackColour là MÀU BÓNG ĐỔ — preview luôn đổ bóng
            # đen mờ ~50% (subtitleTextStyle.ts), không dùng màu hộp ở đây.
            back_color = "&H80000000"
            border_style = 1

        #  ĐỆM NGANG / DỌC RIÊNG cho nền (``backBoxPadX`` / ``backBoxPadY``, 2.15.3).
        #
        #  Với BorderStyle 4 libass nở hộp mỗi bên đúng bằng Outline — và ``\xbord`` /
        #  ``\ybord`` tách được hai trục, NHƯNG cũng kéo dày viền CHỮ theo (đo thật: chữ
        #  "ABC" cỡ 40, \xbord20 -> điểm ảnh viền x6). Nên có đệm riêng thì vẽ HAI LỚP:
        #    · lớp 0 (style ``Nen``): chữ + viền TRONG SUỐT, chỉ còn hộp BackColour, đệm
        #      bằng ``\xbord\ybord``;
        #    · lớp 1 (style ``Default``): chữ như không có nền (BorderStyle 1, viền đúng
        #      ``outlineWidth``, bóng đổ như preview).
        #  Đo thật: đệm x=20 y=4 -> hộp nở 21-22px ngang, 11-12px dọc (dọc có sẵn ~7px
        #  khoảng trống của font, y như khi đệm bằng Outline); viền chữ giữ nguyên.
        #  Không đặt (None) thì GIỮ NGUYÊN đường một lớp cũ — dự án cũ xuất ra y hệt.
        def _so_hoac_none(v):
            try:
                return max(0.0, round(float(v), 2)) if v is not None and v != "" else None
            except (TypeError, ValueError):
                return None

        pad_x = _so_hoac_none(style.get("backBoxPadX"))
        pad_y = _so_hoac_none(style.get("backBoxPadY"))
        #  BO GÓC (``backBoxRadius``, 2.15.4) — libass không bo được hộp, nên > 0 thì lớp
        #  nền là hình vector (xem services/ass_nen_bo_goc.py). Cần tệp font để đo khung
        #  chữ; không có thì rơi về hộp vuông như cũ.
        ban_kinh = _so_hoac_none(style.get("backBoxRadius")) or 0.0
        bo_goc = bool(back_box) and ban_kinh > 0 and bool(font_path)
        hai_lop = bool(back_box) and (pad_x is not None or pad_y is not None or bo_goc)
        if hai_lop:
            pad_x = outline if pad_x is None else pad_x
            pad_y = outline if pad_y is None else pad_y
            box_color = back_color
            back_color = "&H80000000"       # lớp chữ: BackColour lại là màu bóng đổ
            border_style = 1
        style_nen = (
            f"Style: Nen,{font},{ass_font_size},&HFF000000,&HFF000000,&HFF000000,"
            f"{box_color},{bold},{italic},0,0,100,100,{spacing},{rotation:.2f},4,0,0,"
            f"{alignment},20,20,20,1\n"
            if hai_lop and not bo_goc else ""
        )
        if bo_goc:
            #  Hình vector được TÔ bằng PrimaryColour -> màu + độ mờ của nền nằm ở đó.
            style_nen = (
                f"Style: NenBo,{font},{ass_font_size},{box_color},{box_color},{box_color},"
                f"&HFF000000,0,0,0,0,100,100,0,{rotation:.2f},1,0,0,"
                f"{alignment},20,20,20,1\n"
            )

        header = f"""[Script Info]
Title: SubForge Export
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{ass_font_size},{primary},{secondary},&H00000000,{back_color},{bold},{italic},0,0,100,100,{spacing},{rotation:.2f},{border_style},{outline},{shadow},{alignment},20,20,20,1
{style_nen}
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

        events: list[str] = []

        for seg in segments:
            text = seg.get("target_text") or seg.get("source_text") or ""
            if not text.strip():
                continue
            start = float(seg.get("start_time", 0))
            end = float(seg.get("end_time", start + 1))
            start_ts = AssGenerator._format_timestamp(start)
            end_ts = AssGenerator._format_timestamp(end)

            if mode == "normal":
                display = _display_text(text, uppercase)
                events.append(
                    f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{pos_tag}{_escape_ass_text(display)}"
                )
            elif mode == "word-by-word":
                words = _split_words(text)
                if uppercase:
                    words = [w.upper() for w in words]
                for word, ws, we in _estimate_word_timings(start, end, words):
                    ws_ts = AssGenerator._format_timestamp(ws)
                    we_ts = AssGenerator._format_timestamp(we)
                    events.append(
                        f"Dialogue: 0,{ws_ts},{we_ts},Default,,0,0,0,,{pos_tag}{_escape_ass_text(word)}"
                    )
            elif mode == "chunked":
                words = _split_words(text)
                if uppercase:
                    words = [w.upper() for w in words]
                chunk_size = _clamp_chunk_word_count(style.get("chunkWordCount", 3))
                for group_text, gs, ge in _estimate_group_timings(start, end, words, chunk_size):
                    gs_ts = AssGenerator._format_timestamp(gs)
                    ge_ts = AssGenerator._format_timestamp(ge)
                    events.append(
                        f"Dialogue: 0,{gs_ts},{ge_ts},Default,,0,0,0,,{pos_tag}{_escape_ass_text(group_text)}"
                    )
            elif mode == "karaoke":
                words = _split_words(text)
                if uppercase:
                    words = [w.upper() for w in words]
                word_timings = _estimate_word_timings(start, end, words)
                # GIAN CHU giua cac tu karaoke — phai CO THEO CO CHU, khong duoc cung.
                #
                # ``\fsp`` la LETTER-SPACING: no cong them sau MOI GLYPH, khong phai
                # "mot lan giua hai chu". Ban cu dat fsp_tag mot lan o dau dong nen
                # TUNG KY TU trong tu bi tach roi ("Đ Ổ I  S A N G" — thay ro trong
                # video xuat 2026-08-27), trong khi preview chi gian GIUA CAC TU
                # (margin em moi ben, ky tu trong tu van sat nhau).
                #
                # Cach dung: chi BOC RIENG dau cach giua hai tu — mo override truoc
                # dau cach, tra ve Spacing goc ngay sau. Khe ho = be rong dau cach
                # + word_gap, voi word_gap = spacing goc (dau cach nao cung mang no,
                # ca hai ben) + 2 × margin em cua preview (margin dat ca hai ben tu).
                #
                # He so 0.09/0.05 (giam tu 0.18/0.1 ngay 2026-08-27): khe cu rong
                # ~0.66em nhin "thua" — nguoi dung che. Doi o day thi phai doi
                # margin em cua CompositorPreview + OverlayCanvas cung luc.
                _fsp_em = 0.09 if karaoke_pop else 0.05
                word_gap = round(spacing + font_size * _fsp_em * 2, 2)
                sep = "{\\fsp%s} {\\fsp%s}" % (word_gap, spacing)
                # Mirror preview: mọi giá trị KHÁC "cumulative" đều là active-only
                # (CompositorPreview: karaokeStyle !== "cumulative").
                if karaoke_style != "cumulative" and word_timings:
                    for i, (_, ws, we) in enumerate(word_timings):
                        ws_ts = AssGenerator._format_timestamp(ws)
                        we_ts = AssGenerator._format_timestamp(we)
                        visible, _, local_active = _karaoke_window_slice(
                            words, i, karaoke_window
                        )
                        parts = []
                        for j, w in enumerate(visible):
                            is_active = j == local_active
                            color = primary if is_active else secondary
                            if karaoke_pop:
                                scale = POP_SCALE if is_active else 100
                                parts.append(
                                    f"{{\\1c{color}\\fscx{scale}\\fscy{scale}}}{_escape_ass_text(w)}"
                                )
                            else:
                                parts.append(f"{{\\1c{color}}}{_escape_ass_text(w)}")
                        # MOT dau cach that giua hai tu (preview cung co glyph space),
                        # khoang ho con lai do word_gap trong ``sep`` lo.
                        line = sep.join(parts)
                        events.append(
                            f"Dialogue: 0,{ws_ts},{we_ts},Default,,0,0,0,,{pos_tag}{line}"
                        )
                else:
                    total_cs = max(int((end - start) * 100), 1)
                    if word_timings:
                        for i, (_, ws, we) in enumerate(word_timings):
                            ws_ts = AssGenerator._format_timestamp(ws)
                            we_ts = AssGenerator._format_timestamp(we)
                            visible, _, local_active = _karaoke_window_slice(
                                words, i, karaoke_window
                            )
                            parts = []
                            for j, w in enumerate(visible):
                                color = primary if j <= local_active else secondary
                                # PHONG TO chu dang doc — nhanh nay TRUOC DAY BO QUEN.
                                # Preview phong to theo ``karaokePop`` bat ke kieu karaoke
                                # (OverlayCanvas chi xet ``i === active``), nen chon
                                # "Dồn màu" + bat pop thi preview co chu nhay con video
                                # xuat ra thi khong.
                                scale = POP_SCALE if (karaoke_pop and j == local_active) else 100
                                parts.append(
                                    f"{{\\1c{color}\\fscx{scale}\\fscy{scale}}}"
                                    f"{_escape_ass_text(w)}"
                                )
                            line = sep.join(parts)
                            events.append(
                                f"Dialogue: 0,{ws_ts},{we_ts},Default,,0,0,0,,{pos_tag}{line}"
                            )
                    else:
                        display = _display_text(text, uppercase)
                        karaoke_text = f"{{\\kf{total_cs}}}{_escape_ass_text(display)}"
                        events.append(
                            f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{pos_tag}{karaoke_text}"
                        )

        if hai_lop:
            #  Mỗi dòng chữ thêm một bản sao ở LỚP 0 bằng style ``Nen`` (chỉ hộp). Giữ
            #  nguyên mọi thẻ của dòng (vị trí, \fscx phóng chữ karaoke, \1c) để hộp ôm
            #  đúng chữ; \1c chỉ đổi màu, không đổi độ trong suốt nên chữ vẫn ẩn.
            dau = "Dialogue: 0,"
            the_dem = "{\\xbord%s\\ybord%s}" % (pad_x, pad_y)
            moi: list[str] = []
            for ev in events:
                if not ev.startswith(dau):
                    moi.append(ev)
                    continue
                phan = ev.split(",", 9)          # 10 trường, Text là trường cuối
                nen = phan[:]
                chu = phan[:]
                chu[0] = "Dialogue: 1"
                line_h = round(font_size * LINE_HEIGHT_EM, 2)
                if bo_goc:
                    #  Nền BO GÓC: hình vector cỡ khung chữ + lề. Chữ có thể được ngắt
                    #  dòng lại (câu dài) để hai lớp cùng một cách xuống dòng.
                    chu[9], nen[9] = dong_nen_bo_goc(
                        phan[9], font_path=font_path, em=font_size, spacing=spacing,
                        ass_font_size=ass_font_size, pad_x=pad_x, pad_y=pad_y,
                        radius=ban_kinh, play_res_x=play_res_x, play_res_y=play_res_y,
                        line_h=line_h,
                    )
                    nen[3] = "NenBo"
                    moi.append(",".join(nen))
                    #  Chữ nhiều dòng: TÁCH từng dòng, cách nhau đúng khoảng cách dòng của preview.
                    for t in tach_tung_dong(chu[9], line_h):
                        moi.append(",".join(chu[:9] + [t]))
                else:
                    nen[3] = "Nen"
                    #  Hộp vuông \xbord\ybord: tách cả hai lớp theo cùng một cách — mỗi dòng một hộp
                    #  (các hộp chạm / đè nhau thành một khối liền vì có đệm dọc).
                    for t in tach_tung_dong(phan[9], line_h):
                        moi.append(",".join(nen[:9] + [the_dem + t]))
                    for t in tach_tung_dong(chu[9], line_h):
                        moi.append(",".join(chu[:9] + [t]))
            events = moi
        return header + "\n".join(events) + "\n"

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h}:{m:02d}:{s:05.2f}"
