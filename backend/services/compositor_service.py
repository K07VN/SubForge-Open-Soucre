"""
FFmpeg compositor pipeline: multi-zone masking, logo overlay, ASS subtitles.
Builds a single filter_complex chain for optimal render performance.
"""
from utils.i18n import L
import os
import sys
import re
import subprocess
import logging
import imageio_ffmpeg
from utils.watermark_motion import watermark_drawtext_xy
from utils.font_resolver import (
    BLACK_VARIANTS,
    cjk_fallback_font,
    has_cjk,
    WINDOWS_FONT_FILES,
    cyrillic_fallback_font,
    has_cyrillic,
    bundled_font_path,
    effective_ass_font,
    resolve_windows_font,
)
from utils.aspect_crop import (
    build_frame_filters,
    build_crop_scale_filter,
    frame_bg_image_path,
    compute_target_video_bitrate,
    get_output_dimensions,
    probe_video_dimensions,
    probe_video_fps,
)
from utils.radial_blur_mask import feather_alpha_expr, ffmpeg_boxblur_params
from utils.overlay_coords import source_rect_to_output_norm
from services.ass_generator import AssGenerator
from services.compositor_utils import tom_loi_ffmpeg

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

_hw_encoder: str | None | object = None  # None chua do; "" = khong co; ten = co
_HW_CANDIDATES = ("h264_nvenc", "h264_qsv", "h264_amf")


def _detect_hw_encoder() -> str:
    """Tra ve encoder GPU H.264 chay duoc tren may (cache 1 lan), "" neu khong.

    QUAN TRONG: doc lap voi PyTorch CUDA. GPU encode nhanh hon libx264 CPU
    NHIEU LAN va tuy hang card:
      - NVIDIA -> h264_nvenc
      - Intel (vd Arc/Iris) -> h264_qsv (Quick Sync)
      - AMD -> h264_amf
    Test tung ung vien bang cach encode thu 1 frame — chac chan hon doc list
    encoder (co the liet ke nhung driver/phan cung khong dung duoc). Dung o
    ung vien dau tien thanh cong. Neu tat ca fail -> "" (encode CPU).
    """
    global _hw_encoder
    if _hw_encoder is not None:
        return _hw_encoder  # type: ignore[return-value]
    for enc in _HW_CANDIDATES:
        try:
            probe = subprocess.run(
                [
                    FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=128x128:d=0.1",
                    "-c:v", enc, "-f", "null", "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            if probe.returncode == 0:
                _hw_encoder = enc
                logger.info(f"Encode GPU kha dung: {enc} -> uu tien khi xuat video.")
                return enc
        except Exception:
            continue
    _hw_encoder = ""
    logger.info("Khong co encoder GPU -> encode CPU (libx264).")
    return ""


#  GIAI MA BANG GPU/iGPU — THU NGHIEM, MAC DINH TAT (Giai doan 6 ke hoach 2.15.0).
#
#  Vi sao can: may KHONG card roi (chi iGPU) da nen bang QSV/AMF, nhung GIAI MA + bo loc
#  van an CPU — phim 2–3 tieng xuat mat > 3 tieng. Day giai ma sang iGPU la tra lai CPU
#  cho bo loc.
#
#  Vi sao KHONG dung lai ``-hwaccel auto`` (da go vi HONG MAU, xem ``render``): de ffmpeg
#  tu tai khung ve RAM thi bo loc phia sau nhan khung o dinh dang phan cung tai ve (nv12)
#  va co bo loc doc sai kenh mau. O day tai ve TUONG MINH (``hwdownload``) roi EP
#  ``format=yuv420p`` truoc moi bo loc — phan con lai cua do thi nhan dung khung nhu giai
#  ma CPU. Chi bat khi tools/bench_igpu.py (chuoi loc THAT cua app) do ra nhanh hon MA
#  mau/anh khop ban giai ma CPU.
#
#  Nguon 10-bit (p010) khong tai ve duoi dang nv12 duoc -> ffmpeg bao loi ngay -> bac
#  thang trong ``render`` lui ve giai ma CPU.
_HW_DECODE_ARGS = {
    "d3d11va": ["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"],
    "qsv": ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"],
}


def _hw_decode_mode() -> str:
    """"" (tat) | "d3d11va" (moi GPU DX11: Intel/AMD/NVIDIA) | "qsv" (Intel).

    Nguon: bien moi truong SUBFORGE_HW_DECODE (de do/thu, "off" = ep tat) -> cong tac
    "Tang toc bang GPU tich hop" trong Cai dat (config ``hw_decode``, MAC DINH TAT).
    Bat thi dung d3d11va: do tren Arc 140V nhanh hon qsv o moi cau hinh.

    SO DO (2026-09-24, tools/bench_igpu.py, xuat THAT 60s 1080p, h264_qsv, nguon H.265):
      CPU ghim 2 nhan : 16:9 21,2s -> 14,4s (-32%) · 9:16 nen mo 27,6s -> 20,2s (-27%)
      CPU ghim 4 nhan : -10% / -11% · du 8 nhan: nguon H.264 16:9 -26%, 9:16 ~ngang
      CPU ffmpeg an giam ~1/2 o moi luot · anh khop tung diem (PSNR inf) · mau 3 dai khop.
    CHUA DO: iGPU doi cu (UHD 620), AMD APU -> de tat mac dinh, nguoi dung tu bat."""
    m = (os.environ.get("SUBFORGE_HW_DECODE") or "").strip().lower()
    if m:
        return m if m in _HW_DECODE_ARGS else ""
    try:
        from services.config_service import get_config_service

        return "d3d11va" if get_config_service().get_hw_decode() else ""
    except Exception:  # noqa: BLE001 — doc cau hinh hong thi coi nhu tat
        return ""


def _with_hw_download(filter_complex: str) -> str | None:
    """Doi moi ``[0:v]`` sang khung DA TAI VE RAM o dang yuv420p. None = do thi khong
    lay hinh tu input 0 -> khong co gi de giai ma bang GPU."""
    n = filter_complex.count("[0:v]")
    if n == 0:
        return None
    nhan = [f"[hwdl{i}]" for i in range(n)]
    dau = "[0:v]hwdownload,format=nv12,format=yuv420p"
    dau += f",split={n}{''.join(nhan)}" if n > 1 else nhan[0]
    for x in nhan:
        filter_complex = filter_complex.replace("[0:v]", x, 1)
    return f"{dau};{filter_complex}"


logger = logging.getLogger("CompositorService")
FFMPEG_TIME_RE = re.compile(rb"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _has_audio_stream(path: str) -> bool:
    """Kiem tra video co san track audio khong (de biet co can amix hay khong)."""
    try:
        probe = subprocess.run(
            [FFMPEG_EXE, "-hide_banner", "-i", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        return b"Audio:" in (probe.stderr or b"")
    except Exception:
        return False


def _zone_enable_expr(zone: dict) -> str:
    """``:enable='between(t,s,e)'`` cho MOT PHAN TU PHU LEN HINH (vung che, khoi chu,
    logo), hoac chuoi rong neu hien suot video.

    Dung chung cho ca ba vi ``drawbox``, ``drawtext`` va ``overlay`` deu ho tro timeline —
    chi can noi chuoi nay vao duoi filter la xong.

    Chap nhan chi co mot dau: chi ``start`` = tu do den het, chi ``end`` = tu dau den do.
    """
    def _num(v):
        try:
            return None if v is None or v == "" else float(v)
        except (TypeError, ValueError):
            return None

    s = _num(zone.get("start"))
    e = _num(zone.get("end"))
    if s is None and e is None:
        return ""
    lo = max(0.0, s if s is not None else 0.0)
    if e is None:
        return f":enable='gte(t,{lo:.3f})'"
    hi = float(e)
    if hi <= lo:
        # Khoang khong hop le (nguoi dung go nham) -> coi nhu che ca video, KHONG am tham
        # bo vung che di: bo di thi ho tuong minh khoanh vung sai cho.
        return ""
    return f":enable='between(t,{lo:.3f},{hi:.3f})'"


#: VÙNG CHE "CHỈ KHI CÓ SUB" — nới mỗi câu ra hai đầu bấy nhiêu giây (mốc OCR / chữ hiện
#: dần lệch vài khung: che trễ là lộ chữ gốc một thoáng), rồi GỘP hai câu cách nhau ít
#: hơn ``MASK_CUE_GOP`` giây (che - bỏ che - che liên tục trông nhấp nháy, lại làm biểu
#: thức dài ra). MIRROR: ``MASK_CUE_PAD`` / ``MASK_CUE_GOP`` ở
#: frontend/src/utils/maskZoneTime.ts.
MASK_CUE_PAD = 0.15
MASK_CUE_GOP = 0.4


def mask_cue_intervals(cues) -> list:
    """Các khoảng ``[(a, b)]`` có phụ đề (đã nới + gộp, sắp tăng dần)."""
    ds = []
    for c in cues or []:
        try:
            if isinstance(c, dict):
                a = float(c.get("start_time", c.get("start")))
                b = float(c.get("end_time", c.get("end")))
            else:
                a, b = float(c[0]), float(c[1])
        except (TypeError, ValueError, IndexError):
            continue
        if b > a:
            ds.append((max(0.0, a - MASK_CUE_PAD), b + MASK_CUE_PAD))
    ds.sort()
    gop: list = []
    for a, b in ds:
        if gop and a - gop[-1][1] < MASK_CUE_GOP:
            gop[-1] = (gop[-1][0], max(gop[-1][1], b))
        else:
            gop.append((a, b))
    return gop


def _mask_enable_expr(zone: dict, cues) -> str:
    """Như ``_zone_enable_expr`` nhưng hiểu thêm ``onlyWithSubs`` của vùng che.

    ``cues`` = mốc phụ đề GỐC (câu mà vùng che này giấu đi). ``None`` = bên gọi không có
    dữ liệu phụ đề -> che như thường (không đoán bừa là "không có sub" rồi bỏ trống cả
    video). Danh sách RỖNG = có dữ liệu mà không câu nào -> không che lúc nào cả.
    Vẫn giao với khoảng ``start``/``end`` riêng của vùng.
    """
    if not zone.get("onlyWithSubs") or cues is None:
        return _zone_enable_expr(zone)

    def _num(v):
        try:
            return None if v is None or v == "" else float(v)
        except (TypeError, ValueError):
            return None

    s, e = _num(zone.get("start")), _num(zone.get("end"))
    lo = max(0.0, s) if s is not None else 0.0
    hi = e if e is not None else float("inf")
    if hi <= lo:                      # khoảng hỏng -> coi như cả video (như _zone_enable_expr)
        lo, hi = 0.0, float("inf")
    khoang = []
    for a, b in mask_cue_intervals(cues):
        a2, b2 = max(a, lo), min(b, hi)
        if b2 > a2:
            khoang.append((a2, b2))
    if not khoang:
        return ":enable='0'"
    return ":enable='" + "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in khoang) + "'"


def _side_file_stem(output_path: str) -> str:
    """Gốc tên cho các tệp phụ của một lượt xuất (.ass, .src.ass, .wm.txt, .textN.txt…).

    Các tệp này đi vào CHUỖI BỘ LỌC ffmpeg (bộ lọc phụ đề, drawtext textfile — đường dẫn nằm trong nháy đơn). Trong
    cặp nháy đơn, dấu cách / phẩy / ngoặc vuông / chữ có dấu đều an toàn, riêng dấu
    NHÁY ĐƠN thì không cách thoát nào ổn (đã thử 4 kiểu, 24-09-2026): tên "Don't Look
    Up" là xuất hỏng với "Unable to open …Dont Look Up.ass". Từ 2.15.0 tên tệp xuất tự
    mang tên dự án nên chuyện này dễ gặp hơn hẳn.

    Tên không có ``'`` -> giữ nguyên như trước (tệp .ass nằm cạnh video, cùng tên).
    Có ``'`` -> tên an toàn theo mã băm, cùng thư mục; thư mục cũng có ``'`` thì dùng
    thư mục tạm. Video xuất ra vẫn đúng tên người dùng đặt."""
    stem = output_path.rsplit(".", 1)[0]
    if "'" not in stem:
        return stem
    import hashlib
    import tempfile

    ma = hashlib.md5(output_path.encode("utf-8")).hexdigest()[:12]
    thu_muc = os.path.dirname(output_path)
    if "'" in thu_muc:
        thu_muc = tempfile.gettempdir()
    return os.path.join(thu_muc, f"subforge_export_{ma}")


class CompositorService:
    @staticmethod
    def _escape_drawtext(text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace("%", "\\%")
        )

    @staticmethod
    def _format_drawtext_color(hex_color: str, opacity: float) -> str:
        raw = (hex_color or "#FFFFFF").lstrip("#")
        if len(raw) == 3:
            raw = "".join(c * 2 for c in raw)
        raw = raw[:6].ljust(6, "0")
        alpha = max(0.0, min(1.0, float(opacity)))
        return f"0x{raw}@{alpha:.2f}"

    @staticmethod
    def _cyrillic_safe(style: dict, texts) -> dict:
        """Đổi font khi phải vẽ chữ KIRIN (tiếng Nga) mà font đang chọn không có.

        Chỉ đổi khi CẢ HAI điều đúng: văn bản thật sự có chữ Kirin, và font là font
        đóng gói đã đo là thiếu (``cyrillic_fallback_font``). Bản xuất tiếng Việt/Anh…
        không có chữ Kirin nên KHÔNG BAO GIỜ đi vào nhánh đổi. Font tải lên (fontFile)
        giữ nguyên — không đo được file của người dùng, đổi bừa còn tệ hơn.
        Trả về dict MỚI, không sửa dict gốc (cấu hình còn được lưu lại / dùng tiếp).
        """
        if not style or style.get("fontFile"):
            return style
        fam = style.get("fontFamily", "")
        if not any(has_cyrillic(t) for t in texts):
            return style
        moi = cyrillic_fallback_font(fam)
        if moi == fam:
            return style
        logger.info(L(f"Chữ Kirin: font '{fam}' không có chữ tiếng Nga -> xuất bằng '{moi}'", f"Cyrillic text: font '{fam}' has no Russian letters -> exporting with '{moi}'"))
        return {**style, "fontFamily": moi}

    @staticmethod
    def _cjk_safe(style: dict, texts) -> dict:
        """Đổi sang Noto Sans JP / KR / SC khi phải vẽ chữ Nhật / Hàn / Trung bằng font Latin đóng gói
        (08-10-2026) — cùng luật với ``_cyrillic_safe``: chỉ font đóng gói thiếu chữ, font tải lên và
        font lạ giữ nguyên; văn bản không có chữ CJK thì trả đúng đối tượng cũ."""
        if not style or style.get("fontFile"):
            return style
        fam = style.get("fontFamily", "")
        texts = list(texts)
        if not any(has_cjk(t) for t in texts):
            return style
        moi = cjk_fallback_font(fam, texts)
        if moi == fam:
            return style
        logger.info(L(f"Chữ CJK: font '{fam}' không có chữ Nhật / Hàn / Trung -> xuất bằng '{moi}'", f"CJK text: font '{fam}' has no Japanese / Korean / Chinese letters -> exporting with '{moi}'"))
        return {**style, "fontFamily": moi}

    @staticmethod
    def _font_an_toan(style: dict, texts) -> dict:
        """Kirin rồi CJK — một cửa cho mọi chỗ chọn font xuất (sub chính, dòng phụ, chữ chèn)."""
        texts = list(texts)
        return CompositorService._cjk_safe(CompositorService._cyrillic_safe(style, texts), texts)

    @staticmethod
    def _resolve_font_file(sub_style: dict) -> str:
        font_file = sub_style.get("fontFile", "")
        if font_file and os.path.exists(font_file):
            return font_file
        # Font DONG GOI KEM APP (Anton, Bungee, Oswald, Montserrat, Be Vietnam
        # Pro) — phai tra o day, TRUOC resolve_windows_font: may khach khong cai
        # nhung font nay nen nhanh duoi se roi ve Arial va video xuat ra sai font
        # ma khong bao gi.
        #  Họ bốn mặt (Inter, Montserrat…) -> đúng mặt đậm/nghiêng: tệp này làm vân tay
        #  đo cỡ chữ, lấy nhầm mặt thường cho chữ đậm là đo sai bề ngang.
        bundled = bundled_font_path(
            sub_style.get("fontFamily", ""),
            bool(sub_style.get("bold", True)),
            bool(sub_style.get("italic", False)),
        )
        if bundled:
            return bundled
        # Phai resolve theo MAT HIEU DUNG (bold -> mat Black, mirror AssGenerator):
        # duong dan nay dung lam van tay metrics cho co chu — Arial Black co he so
        # 1.41 khac han Arial 1.12, lay nham mat la chu xuat sai co.
        family, _ = effective_ass_font(
            sub_style.get("fontFamily", ""), bool(sub_style.get("bold", True))
        )
        win_font = resolve_windows_font(family)
        if win_font:
            return win_font
        if sys.platform == "win32":
            arial = "C:/Windows/Fonts/arial.ttf"
            if os.path.exists(arial):
                return arial
        return ""

    #: NỀN CHỮ CHÈN — MIRROR ``buildTextOverlaySpanStyle`` (frontend/src/utils/
    #: textOverlayStyle.ts): ``padding: 0.15em 0.4em``, ``borderRadius: 0.15em``,
    #: ``lineHeight: 1.15``, màu nền × ``opacity`` của cả khối (CSS opacity áp lên
    #: span nên nhân cả vào nền). Đổi một bên là preview lệch bản xuất.
    NEN_CHU_PAD_DOC = 0.15
    NEN_CHU_PAD_NGANG = 0.4
    NEN_CHU_BO_GOC = 0.15
    NEN_CHU_LINE_HEIGHT = 1.15

    @staticmethod
    def _ve_nen_bo_goc(path: str, w: int, h: int, r: int, hex_color: str, alpha: float) -> bool:
        """Ảnh PNG hộp nền BO GÓC (``drawtext:box`` chỉ vẽ được góc vuông). Vẽ ở 4×
        rồi thu nhỏ cho mép bo mịn. Lỗi -> False (nơi gọi lùi về box của drawtext)."""
        try:
            from PIL import Image, ImageDraw

            raw = (hex_color or "#000000").lstrip("#")
            if len(raw) == 3:
                raw = "".join(c * 2 for c in raw)
            raw = (raw + "000000")[:6]
            rgb = tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
            a = max(0, min(255, round(float(alpha) * 255)))
            k = 4
            img = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
            ImageDraw.Draw(img).rounded_rectangle(
                (0, 0, w * k - 1, h * k - 1), radius=max(0, r * k), fill=(*rgb, a)
            )
            img.resize((w, h), Image.LANCZOS).save(path)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(L(f"Không vẽ được nền chữ bo góc ({e}) — dùng nền góc vuông.", f"Could not draw the rounded text background ({e}) — using a square-corner background."))
            return False

    @staticmethod
    def _be_dong_cho_vua(text: str, font_path: str, font_size: int,
                         rong_o: float) -> str:
        """Bẻ dòng cho chữ vừa bề ngang ô — ``drawtext`` KHÔNG tự làm việc này.

        VÌ SAO CẦN: khung xem trước là HTML, chữ tự xuống dòng khi chạm mép ô. Còn
        ``drawtext`` của ffmpeg chỉ ngắt ở ký tự xuống dòng THẬT — nên "Cạo tường
        làm đá?" hiện 2 dòng gọn trong khung xem trước, mà video xuất ra là MỘT
        dòng dài tràn khỏi cả khung hình, mất đầu mất đuôi (đúng ảnh người dùng
        gửi 23-09-2026).

        ĐO BẰNG CHÍNH TỆP FONT SẼ VẼ (PIL đọc cùng tệp .ttf mà ffmpeg dùng), nên bề
        rộng sát với thực tế. Không đọc được font thì ước lượng theo cỡ chữ — thà
        bẻ hơi sớm còn hơn để chữ tràn ra ngoài khung.

        Giữ nguyên những chỗ người dùng đã tự xuống dòng: bẻ TỪNG ĐOẠN một.
        """
        if rong_o <= 0 or not text:
            return text

        do_dai = None
        try:
            from PIL import ImageFont

            if font_path and os.path.exists(font_path):
                _f = ImageFont.truetype(font_path, max(8, int(font_size)))
                do_dai = _f.getlength
        except Exception:  # noqa: BLE001 — thiếu font/PIL thì dùng ước lượng
            do_dai = None
        if do_dai is None:
            #  Ước lượng thô: chữ Latinh trung bình rộng ~0,52 lần cỡ chữ.
            def do_dai(x: str) -> float:  # type: ignore[misc]
                return len(x) * font_size * 0.52

        ra: list[str] = []
        for doan in text.split("\n"):
            tu = doan.split(" ")
            dong = ""
            for t in tu:
                thu = f"{dong} {t}".strip()
                if dong and do_dai(thu) > rong_o:
                    ra.append(dong)
                    dong = t
                else:
                    dong = thu
            ra.append(dong)
        return "\n".join(ra)

    @staticmethod
    def _resolve_overlay_font(ov: dict) -> str:
        """Font cho 1 khối chữ chèn: ưu tiên fontFile, rồi font Windows theo family."""
        font_file = ov.get("fontFile", "")
        if font_file and os.path.exists(font_file):
            return font_file
        family = ov.get("fontFamily", "") or "Arial"
        #  drawtext chỉ nhận MỘT tệp -> họ bốn mặt phải chọn đúng mặt ở đây (preview:
        #  đậm = weight 800 -> CSS lấy mặt 700; nghiêng = mặt italic).
        bundled = bundled_font_path(
            family, bool(ov.get("bold", True)), bool(ov.get("italic", False))
        )
        if bundled:
            return bundled
        #  ĐẬM phải ra ĐÚNG mặt mà preview vẽ. Preview: ``fontWeight: 800`` (textOverlayStyle.ts)
        #  -> luật chọn mặt của CSS lấy mặt nặng gần nhất >= 800: họ có mặt Black (900) thì
        #  ra Black (Segoe UI -> Segoe UI Black, Arial -> Arial Black — cùng cặp
        #  BLACK_VARIANTS phụ đề đã đo), không có thì mặt Bold. Trước đây resolve_windows_font
        #  bỏ qua cờ đậm với họ có trong bảng -> preview đậm mà xuất ra chữ THƯỜNG.
        #  Sửa ở đây (không sửa resolve_windows_font) để không đụng phép đo cỡ phụ đề.
        if bool(ov.get("bold", True)) and not bool(ov.get("italic", False)):
            black = BLACK_VARIANTS.get(family)
            for key in ((black,) if black else ()) + (f"{family} Bold",):
                fn = WINDOWS_FONT_FILES.get(key or "")
                path = os.path.join("C:/Windows/Fonts", fn) if fn else ""
                if path and os.path.exists(path):
                    return path
        return (
            resolve_windows_font(
                family, bool(ov.get("bold", True)), bool(ov.get("italic", False))
            )
            or resolve_windows_font(
                "Arial", bool(ov.get("bold", True)), bool(ov.get("italic", False))
            )
            or ""
        )

    @staticmethod
    def _aspect_crop_filter(config: dict, out_w: int, out_h: int) -> str:
        return build_crop_scale_filter(config, out_w, out_h)

    @staticmethod
    def _norm_to_px(val: float, dim: int) -> int:
        return max(0, int(val * dim))

    @staticmethod
    def build_filter_complex(
        config: dict,
        video_w: int,
        video_h: int,
        has_logo: bool,
        has_ass: bool,
        src_fps: float | None = None,
    ) -> tuple[str, list[str]]:
        filters: list[str] = []
        extra_inputs: list[str] = []
        current = "[0:v]"

        mirror = config.get("mirror", False)

        out_w, out_h = video_w, video_h
        #  LAT GUONG truoc, roi moi dat vao khung: khung duoc nguoi dung can theo hinh DA
        #  lat (do la thu ho nhin thay o khung xem truoc).
        if mirror:
            filters.append(f"{current}hflip[vflip]")
            current = "[vflip]"
        #  DAT VAO KHUNG. Khung rong hon video (nguoi dung thu nho video cho lot vao) thi
        #  buoc nay sinh ra NHIEU doan filter (nen mo/mau + overlay) chu khong con la mot
        #  chuoi tuyen tinh — xem ``build_frame_filters``.
        #  ANH NEN phai nap them mot input cho ffmpeg. Nap TRUOC logo de so thu tu
        #  input khong doi voi du an cu khong dung anh nen (extra_inputs van rong).
        bg_image = frame_bg_image_path(config)
        bg_image_index = None
        if bg_image:
            extra_inputs.append(bg_image)
            bg_image_index = len(extra_inputs)   # 0 = video goc
        filters.extend(build_frame_filters(
            config, out_w, out_h, current, "[vbase]", bg_image_index, src_fps=src_fps,
        ))
        current = "[vbase]"

        mask_zones = config.get("maskZones", [])
        crop = config.get("cropFrame") or {}
        for i, zone in enumerate(mask_zones):
            # KHOANG THOI GIAN cua vung che (giay tren timeline). Thieu ca hai = che CA
            # VIDEO nhu truoc (du an cu khong doi hanh vi). Co thi them ``enable`` — ca
            # ``overlay`` lan ``drawbox`` deu ho tro timeline nen chi can noi vao duoi.
            #
            # VI SAO CAN: sub cung thuong chi hien o vai doan, va ke tu khi chen duoc doan
            # mo dau vao truoc phim thi vung che dat cho sub cua ban goc se che luon ca
            # intro — nhin nhu bug ma khong ai hieu tai sao.
            #  ``_mask_cues``: mốc phụ đề gốc do route đặt vào (chỉ khi có vùng "chỉ khi có sub").
            z_enable = _mask_enable_expr(zone, config.get("_mask_cues"))
            ox, oy, ow, oh = source_rect_to_output_norm(
                float(zone.get("x", 0)),
                float(zone.get("y", 0)),
                float(zone.get("w", 0.1)),
                float(zone.get("h", 0.05)),
                crop,
            )
            #  CAT VUNG THEO KHUNG HINH. Vung ve tren video goc roi doi sang khung khac
            #  ty le (16:9 -> 9:16) thi be ngang ``ow`` vuot 1 — dai che sub keo het ngang
            #  video goc thanh rong gap ~3 lan khung. ``crop`` cua ffmpeg KHONG tu kep kich
            #  thuoc: "Invalid too big ... width" -> ca luot xuat chet voi -22 (Invalid
            #  argument). Khung xem truoc thi chi cat phan thua di, nen o day cung vay.
            x = int(ox * out_w)
            y = int(oy * out_h)
            w = int(ow * out_w)
            h = int(oh * out_h)
            if x < 0:
                w, x = w + x, 0
            if y < 0:
                h, y = h + y, 0
            w = min(w, out_w - x)
            h = min(h, out_h - y)
            if w < 1 or h < 1:
                continue  # vung nam han ngoai khung: khong co gi de che
            mode = zone.get("mode", "blur")

            if mode == "blur":
                # Ep kich thuoc CHAN: crop tren yuv420 tu lam tron xuong chan,
                # con mask scale giu nguyen -> alphamerge loi "sizes do not match"
                # (lech 1px). Ep chan ca x/y de chroma khong bi xo lech.
                w -= w % 2
                h -= h % 2
                x -= x % 2
                y -= y % 2
                w = max(2, w)
                h = max(2, h)
                blur_str = max(5, int(zone.get("blurStrength", 20)))
                # Tham so boxblur tach rieng luma/chroma + kep theo w/h vung:
                # cu phap ngan R:P cu lam filter FAIL tren vung thap (chroma
                # plane 4:2:0 chi bang nua vung) -> export khong mo gi ca.
                bb_params = ffmpeg_boxblur_params(blur_str, w, h)

                # MO DAC KIN CA VUNG — khong con mep mem.
                #
                # Truoc day vung mo duoc cat bang mask PGM (+ alphamerge) de 4 vien
                # nhat dan cho "do lo", nhung nguoi dung muon che kin han: vien nhat
                # lam chu goc van doc duoc o ria. Bo mask di thi chuoi filter chi con
                # crop -> boxblur -> overlay: it hon 3 filter va 1 input anh moi vung,
                # nen vua dung y vua nhanh hon. BAN SOI GUONG cua preview
                # (BlurMaskCanvas + solidifyAlpha) — sua mot ben phai sua ben kia.
                filters.append(
                    f"{current}split[zsrc{i}][zbg{i}]"
                )
                #  MEP THOAI DAN (tuy chon). Mac dinh 0 = giu nguyen kieu dac kin o
                #  tren. Co thoai thi phai gan them kenh alpha cho mieng da mo —
                #  ``geq`` chi ghi plane ALPHA, cac plane mau giu nguyen.
                #
                #  ``geq`` chay tung diem anh nen cham; chi mac vao khi nguoi dung
                #  THAT SU bat thoai mep, de du an cu xuat nhanh y nhu truoc.
                a_expr = feather_alpha_expr(int(zone.get("featherStrength", 0) or 0), w, h)
                feather_chain = (
                    #  ``geq`` BAT BUOC phai co bieu thuc luma ("A luminance or RGB
                    #  expression is mandatory") — thieu la ffmpeg tu choi ca chuoi
                    #  filter. ``p(X,Y)`` = giu nguyen plane do.
                    f",format=yuva420p,"
                    f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='{a_expr}'"
                    if a_expr else ""
                )
                filters.append(
                    f"[zsrc{i}]crop={w}:{h}:{x}:{y},boxblur={bb_params}"
                    f"{feather_chain}[blur{i}]"
                )
                filters.append(
                    f"[zbg{i}][blur{i}]overlay={x}:{y}:format=auto{z_enable}[vm{i}]"
                )
            elif mode == "reflect":
                sx = float(zone.get("x", 0))
                sy = float(zone.get("y", 0))
                sw = float(zone.get("w", 0.1))
                sh = float(zone.get("h", 0.05))
                has_above = sy >= sh * 0.5
                strip_y = max(0.0, sy - sh) if has_above else min(sy + sh, max(0.0, 1.0 - sh))
                src_ox, src_oy, src_ow, src_oh = source_rect_to_output_norm(
                    sx, strip_y, sw, sh, crop,
                )
                src_x = CompositorService._norm_to_px(src_ox, out_w)
                src_y = CompositorService._norm_to_px(src_oy, out_h)
                # KHOP PREVIEW: sau khi lat dai doi xung vao vung, preview con
                # PHA 35% dai DOI DIEN vao 45% duoi cua vung ("xuyen khong").
                blend_sy = (
                    min(sy + sh, max(0.0, 1.0 - sh)) if has_above else max(0.0, sy - sh)
                )
                bl_ox, bl_oy, _bw, _bh = source_rect_to_output_norm(
                    sx, blend_sy, sw, sh, crop,
                )
                bl_x = CompositorService._norm_to_px(bl_ox, out_w)
                bl_y = CompositorService._norm_to_px(bl_oy, out_h)
                bh45 = max(1, int(h * 0.45))
                filters.append(f"{current}split=3[mz{i}a][mz{i}b][mz{i}c]")
                filters.append(f"[mz{i}b]crop={w}:{h}:{src_x}:{src_y},vflip[refl{i}]")
                blend_chain = f"crop={w}:{h}:{bl_x}:{bl_y},scale={w}:{bh45}"
                if not has_above:
                    blend_chain += ",vflip"
                filters.append(
                    f"[mz{i}c]{blend_chain},format=rgba,colorchannelmixer=aa=0.35[rblend{i}]"
                )
                filters.append(f"[mz{i}a][refl{i}]overlay={x}:{y}{z_enable}[vmr{i}]")
                filters.append(
                    f"[vmr{i}][rblend{i}]overlay={x}:{y + h - bh45}:format=auto{z_enable}[vm{i}]"
                )
            else:
                color = zone.get("color", "black")
                opacity = zone.get("opacity", 0.4)
                filters.append(
                    f"{current}drawbox=x={x}:y={y}:w={w}:h={h}:color={color}@{opacity}:t=fill"
                    f"{z_enable}[vm{i}]"
                )
            current = f"[vm{i}]"

        logo = config.get("logo", {})
        if logo.get("enabled") and logo.get("path") and os.path.exists(logo["path"]):
            logo_idx = len(extra_inputs) + 1
            extra_inputs.append(logo["path"])
            crop = config.get("cropFrame") or {}
            ox, oy, ow, oh = source_rect_to_output_norm(
                float(logo.get("x", 0.8)),
                float(logo.get("y", 0.05)),
                float(logo.get("w", 0.15)),
                float(logo.get("h", 0.1)),
                crop,
            )
            lx = CompositorService._norm_to_px(ox, out_w)
            ly = CompositorService._norm_to_px(oy, out_h)
            lw = max(1, CompositorService._norm_to_px(ow, out_w))
            lh = max(1, CompositorService._norm_to_px(oh, out_h))
            logo_opacity = logo.get("opacity", 1.0)
            # KHOP PREVIEW: giu ty le anh (contain, letterbox trong khung) —
            # ban cu scale=w:h keo MEO anh khi khung khac ty le anh goc.
            filters.append(
                f"[{logo_idx}:v]scale={lw}:{lh}:force_original_aspect_ratio=decrease,"
                f"format=rgba,colorchannelmixer=aa={logo_opacity}[logo]"
            )
            # Can giua trong khung nhu object-fit: contain
            filters.append(
                f"{current}[logo]overlay=x='{lx}+({lw}-w)/2':y='{ly}+({lh}-h)/2'"
                f"{_zone_enable_expr(logo)}[vlogo]"
            )
            current = "[vlogo]"

        watermark = config.get("watermark", {})
        wm_text = (watermark.get("text") or "").strip()
        if watermark.get("enabled") and wm_text:
            # KHOP PREVIEW (mismatch 4A/4B/4C cu):
            #  - Toa do la SOURCE-norm va PHAI qua crop transform (truoc day
            #    watermark la overlay duy nhat khong duoc crop-correct).
            #  - Co chu: preview hien thi ~ fontSize*1.1 px o he OUTPUT (khong
            #    theo out_h/1080); crop phong to them 1/ch.
            #  - Font: dung font cua SUBTITLE nhu preview, khong hardcode Arial.
            wm_crop = config.get("cropFrame") or {}
            ch = max(float(wm_crop.get("h", 1.0) or 1.0), 1e-6)
            fs = max(8, round(float(watermark.get("fontSize", 24)) * 1.1 / ch))
            animation = watermark.get("animation", "float")
            speed = float(watermark.get("speed", 0.4))
            x_expr, y_expr = watermark_drawtext_xy(
                animation, speed,
                float(watermark.get("x", 0.05)), float(watermark.get("y", 0.05)),
                wm_crop,
            )
            fontcolor = CompositorService._format_drawtext_color(
                watermark.get("color", "#FFFFFF"),
                watermark.get("opacity", 0.5),
            )
            sub_family = (config.get("subtitleStyle") or {}).get("fontFamily", "")
            #  FONT ĐÓNG GÓI TRƯỚC: preview vẽ watermark bằng font phụ đề; phụ đề chọn font
            #  đóng gói (Kanit Black, Montserrat…) mà ở đây chỉ tra Windows là video ra Arial.
            wm_font = bundled_font_path(
                sub_family,
                bool(watermark.get("bold", True)),
                bool(watermark.get("italic", False)),
            ) or resolve_windows_font(
                sub_family or "Arial",
                bool(watermark.get("bold", True)),
                bool(watermark.get("italic", False)),
            ) or resolve_windows_font(
                "Arial",
                bool(watermark.get("bold", True)),
                bool(watermark.get("italic", False)),
            )
            fontfile_clause = ""
            if wm_font:
                escaped_font = wm_font.replace("\\", "/").replace(":", "\\:")
                fontfile_clause = f":fontfile='{escaped_font}'"
            text_file = config.get("_wm_text_file", "")
            if text_file and os.path.exists(text_file):
                tf = text_file.replace("\\", "/").replace(":", "\\:")
                text_clause = f"textfile='{tf}'"
            else:
                text_clause = f"text='{CompositorService._escape_drawtext(wm_text)}'"
            filters.append(
                f"{current}drawtext={text_clause}:x='{x_expr}':y='{y_expr}':fontsize={fs}"
                f":fontcolor={fontcolor}{fontfile_clause}:borderw=1:bordercolor=0x00000060"
                f"{_zone_enable_expr(watermark)}[vwm]"
            )
            current = "[vwm]"

        # --- Khối chữ chèn tĩnh (textOverlays) — nhiều khối, style riêng ---
        # KHỚP PREVIEW: toạ độ SOURCE-norm qua crop transform; fontSize quy đổi
        # giống watermark (fontSize*1.1/crop.h). Căn ngang theo align trong khung.
        to_crop = config.get("cropFrame") or {}
        to_ch = max(float(to_crop.get("h", 1.0) or 1.0), 1e-6)
        text_files = config.get("_text_overlay_files", {}) or {}
        for idx, ov in enumerate(config.get("textOverlays", []) or []):
            if not ov.get("enabled", True):
                continue
            raw_text = (ov.get("text") or "").strip()
            ov_id = ov.get("id", str(idx))
            tf_path = text_files.get(ov_id)
            if not raw_text and not (tf_path and os.path.exists(tf_path)):
                continue

            ox, oy, ow, oh = source_rect_to_output_norm(
                float(ov.get("x", 0.1)),
                float(ov.get("y", 0.1)),
                float(ov.get("w", 0.8)),
                float(ov.get("h", 0.14)),
                to_crop,
            )
            bx = ox * out_w
            by = oy * out_h
            bw = max(1.0, ow * out_w)
            bh = max(1.0, oh * out_h)
            fs = max(8, round(float(ov.get("fontSize", 40)) * 1.1 / to_ch))

            align = int(ov.get("align", 2) or 2)
            if align == 1:
                x_expr = f"{bx:.1f}"
            elif align == 3:
                x_expr = f"({bx + bw:.1f})-text_w"
            else:
                x_expr = f"({bx + bw / 2:.1f})-text_w/2"
            y_expr = f"({by + bh / 2:.1f})-text_h/2"

            opacity = ov.get("opacity", 1.0)
            fontcolor = CompositorService._format_drawtext_color(
                ov.get("color", "#FFFFFF"), opacity
            )
            ov_font = CompositorService._resolve_overlay_font(ov)
            fontfile_clause = ""
            if ov_font:
                escaped_font = ov_font.replace("\\", "/").replace(":", "\\:")
                fontfile_clause = f":fontfile='{escaped_font}'"

            clauses = ""
            outline_w = round(float(ov.get("outlineWidth", 0) or 0) / to_ch)
            if outline_w > 0:
                border_color = CompositorService._format_drawtext_color(
                    ov.get("outlineColor", "#000000"), opacity
                )
                clauses += f":borderw={outline_w}:bordercolor={border_color}"
            shadow_d = round(float(ov.get("shadowDepth", 0) or 0) / to_ch)
            if shadow_d > 0:
                clauses += (
                    f":shadowx={shadow_d}:shadowy={shadow_d}:shadowcolor=0x000000@0.85"
                )
            co_nen = bool(ov.get("backBox"))
            #  Đệm của nền chữ — ĐÚNG số của CSS preview (0.15em dọc × 0.4em ngang).
            #  Người dùng chỉnh được lề (``backBoxPadX/Y``, em, 0..2) — MIRROR ``lePadNenChu`` ở textOverlayStyle.ts.
            def _le(khoa, md):
                try:
                    v = float(ov.get(khoa))
                except (TypeError, ValueError):
                    return md
                return md if v != v else max(0.0, min(2.0, v))

            le_x = _le("backBoxPadX", CompositorService.NEN_CHU_PAD_NGANG)
            le_y = _le("backBoxPadY", CompositorService.NEN_CHU_PAD_DOC)
            pad_x = round(fs * le_x) if co_nen else 0
            pad_y = round(fs * le_y) if co_nen else 0

            #  BẺ DÒNG CHO VỪA Ô — làm ở ĐÂY vì chỉ tới đây mới biết bề ngang ô
            #  tính bằng pixel của khung xuất (``bw``) và cỡ chữ thật (``fs``).
            #  Trừ phần đệm ngang của nền chữ, không thì chữ chạm sát mép nền.
            rong_chu = max(1.0, bw - pad_x * 2)

            noi_dung = ""
            if tf_path and os.path.exists(tf_path):
                try:
                    with open(tf_path, "r", encoding="utf-8") as _f:
                        noi_dung = _f.read()
                except OSError:
                    noi_dung = ""
            if not noi_dung:
                noi_dung = raw_text.upper() if ov.get("uppercase") else raw_text

            noi_dung = CompositorService._be_dong_cho_vua(
                noi_dung, ov_font, fs, rong_chu
            )

            #  KHOẢNG CÁCH DÒNG = CSS ``lineHeight: 1.15`` của preview. drawtext đặt mỗi dòng
            #  cách nhau (chiều cao dòng của font = FreeType ``size.metrics.height``) +
            #  ``line_spacing`` — đo thật: Segoe UI 59px / Arial 51px ở cỡ 44 (1,16–1,34em),
            #  trước đây cộng thêm 0.12em nên chữ nhiều dòng thưa hơn preview 10–26%. Nay bù
            #  cho đúng 1.15em (có thể âm với font dòng cao). Không đo được font -> số cũ.
            khoang_dong = max(2, round(fs * 0.12))
            if ov_font:
                try:
                    from PIL import ImageFont

                    _cao_dong = ImageFont.truetype(ov_font, fs).font.height
                    khoang_dong = round(fs * CompositorService.NEN_CHU_LINE_HEIGHT) - int(_cao_dong)
                except Exception:  # noqa: BLE001
                    pass

            #  NỀN CHỮ BO GÓC. Trước đây dùng ``drawtext:box`` -> GÓC VUÔNG + đệm đều
            #  0.25em bốn phía, trong khi preview bo góc 0.15em, đệm 0.15em dọc / 0.4em
            #  ngang (người dùng thấy ngay: "preview bo cong mà xuất thì góc vuông").
            #  Nay vẽ hộp thành PNG đúng số của CSS rồi overlay DƯỚI chữ. Kích thước chữ
            #  đo bằng PIL với CÙNG font + cỡ mà drawtext dùng (cùng FreeType); thiếu font
            #  hoặc vẽ hỏng thì lùi về box vuông của drawtext như cũ.
            if co_nen:
                hop = None
                try:
                    if ov_font:
                        from PIL import ImageFont

                        _f = ImageFont.truetype(ov_font, fs)
                        dong = noi_dung.split("\n") or [""]
                        tw = max((_f.getlength(d) for d in dong), default=0.0)
                        cao_css = len(dong) * CompositorService.NEN_CHU_LINE_HEIGHT * fs
                        box_w = max(1, round(tw + 2 * pad_x))
                        #  Cao ĐÚNG theo preview. Dùng được cho cả chữ nhiều dòng vì khoảng
                        #  cách dòng khi xuất đã bù về đúng 1.15em (``khoang_dong`` bên dưới).
                        box_h = max(1, round(cao_css + 2 * pad_y))
                        hop = (box_w, box_h)
                except Exception as e:  # noqa: BLE001
                    logger.warning(L(f"Không đo được chữ chèn để vẽ nền ({e}) — dùng nền góc vuông.", f"Could not measure the inserted text to draw its background ({e}) — using a square-corner background."))
                    hop = None
                png = ""
                if hop:
                    if tf_path:
                        png = f"{tf_path}.box.png"
                    else:
                        import tempfile
                        import uuid as _uuid

                        png = os.path.join(tempfile.gettempdir(), f"subforge_textbox_{_uuid.uuid4().hex}.png")
                    ok_png = CompositorService._ve_nen_bo_goc(
                        png, hop[0], hop[1], round(fs * CompositorService.NEN_CHU_BO_GOC),
                        ov.get("backBoxColor", "#000000"),
                        float(ov.get("backBoxOpacity", 0.5)) * float(opacity if opacity is not None else 1.0),
                    )
                    if not ok_png:
                        png = ""
                if png:
                    box_w, box_h = hop
                    if align == 1:
                        box_x = f"{bx:.1f}"
                        x_expr = f"{bx + pad_x:.1f}"
                    elif align == 3:
                        box_x = f"{bx + bw - box_w:.1f}"
                        x_expr = f"({bx + bw - pad_x:.1f})-text_w"
                    else:
                        box_x = f"{bx + bw / 2 - box_w / 2:.1f}"
                    box_y = f"{by + bh / 2 - box_h / 2:.1f}"
                    box_idx = len(extra_inputs) + 1
                    extra_inputs.append(png)
                    filters.append(
                        f"{current}[{box_idx}:v]overlay=x={box_x}:y={box_y}"
                        f"{_zone_enable_expr(ov)}[vtbox{idx}]"
                    )
                    current = f"[vtbox{idx}]"
                else:
                    box_color = CompositorService._format_drawtext_color(
                        ov.get("backBoxColor", "#000000"),
                        ov.get("backBoxOpacity", 0.5),
                    )
                    clauses += f":box=1:boxcolor={box_color}:boxborderw={max(2, round(fs * 0.25))}"

            #  Ghi ra tệp RIÊNG, không đè tệp gốc: tệp gốc còn dùng cho lượt dựng
            #  khác (xuất đoạn đã chọn chạy lại cùng một cấu hình).
            text_clause = ""
            if tf_path:
                wrap_path = f"{tf_path}.wrap.txt"
                try:
                    #  newline = LF BẮT BUỘC: chế độ văn bản trên Windows đổi mỗi xuống
                    #  dòng thành CR+LF — drawtext hiểu CR cũng là xuống dòng nên mỗi lần
                    #  xuống dòng bị NHÂN ĐÔI, chữ nhiều dòng giãn gấp đôi và tràn ra
                    #  ngoài hộp nền (đo thật: 2,9em/dòng).
                    with open(wrap_path, "w", encoding="utf-8", newline="\n") as _f:
                        _f.write(noi_dung)
                    tf = wrap_path.replace("\\", "/").replace(":", "\\:")
                    text_clause = f"textfile='{tf}'"
                except OSError:
                    text_clause = ""
            if not text_clause:
                text_clause = (
                    f"text='{CompositorService._escape_drawtext(noi_dung)}'"
                )

            filters.append(
                f"{current}drawtext={text_clause}:x='{x_expr}':y='{y_expr}'"
                f":fontsize={fs}:fontcolor={fontcolor}{fontfile_clause}{clauses}"
                f":line_spacing={khoang_dong}"
                # Khoang thoi gian RIENG cua tung khoi chu -> xep so le / chong nhau tuy y.
                f"{_zone_enable_expr(ov)}[vtext{idx}]"
            )
            current = f"[vtext{idx}]"

        #  PHU DE GOC — dot TRUOC loi binh de loi binh nam tren neu hai lop co cham nhau.
        src_ass = config.get("_src_ass_path") or ""
        if src_ass:
            src_escaped = src_ass.replace("\\", "/").replace(":", "\\:")
            src_fonts = config.get("_src_fonts_dir") or ""
            if src_fonts and os.path.isdir(src_fonts):
                src_fonts_e = src_fonts.replace("\\", "/").replace(":", "\\:")
                filters.append(f"{current}subtitles='{src_escaped}':fontsdir='{src_fonts_e}'[vsrcsub]")
            else:
                filters.append(f"{current}subtitles='{src_escaped}'[vsrcsub]")
            current = "[vsrcsub]"

        if has_ass:
            ass_path = config.get("_ass_path", "subtitle.ass")
            fonts_dir = config.get("_fonts_dir", "")
            escaped = ass_path.replace("\\", "/").replace(":", "\\:")
            if fonts_dir and os.path.isdir(fonts_dir):
                fonts_escaped = fonts_dir.replace("\\", "/").replace(":", "\\:")
                filters.append(
                    f"{current}subtitles='{escaped}':fontsdir='{fonts_escaped}'[vout]"
                )
            else:
                filters.append(f"{current}subtitles='{escaped}'[vout]")
            current = "[vout]"
        else:
            filters.append(f"{current}null[vout]")

        return "; ".join(filters), extra_inputs

    @staticmethod
    def _run_ffmpeg(
        cmd: list[str],
        *,
        duration_sec: float = 0,
        on_progress=None,
        should_cancel=None,
    ) -> tuple[bool, str]:
        """Chay ffmpeg, doc tien do, VA dung duoc giua chung.

        ``should_cancel()`` -> True thi giet ffmpeg va tra ``(False, "__CANCELLED__")``.

        VI SAO PHAI CO: day la BUOC 2/2 cua ban xuat (dot phu de / logo / watermark len
        hinh) — voi phim 3 tieng no la chang lau nhat. Truoc day ham nay chi
        ``proc.wait()`` toi khi ffmpeg xong, tuc nut Huy KHONG CO TAC DUNG GI trong suot
        chang do: nguoi dung bam Huy, giao dien bao "dang huy", con may thi cu chay tiep
        hang chuc phut. Buoc 1/2 von da huy duoc (``_run_ffmpeg_cancellable``), chi
        rieng buoc nay bi bo quen.
        """
        last_pct = 0

        def emit(pct: int, message: str) -> None:
            nonlocal last_pct
            pct = max(last_pct, min(99, pct))
            if pct > last_pct and on_progress:
                last_pct = pct
                on_progress(pct, message)

        if on_progress:
            on_progress(8, L("Đang khởi chạy FFmpeg...", "Starting FFmpeg..."))

        # SUA LOI "ket 8%": truoc day parse `time=` tu stderr bang readline —
        # nhung ffmpeg ket thuc dong stats bang \r (khong phai \n) nen readline
        # KHONG BAO GIO nha dong ra -> khong co cap nhat tien do nao ca.
        # Chuyen sang `-progress pipe:1` (key=value, xuong dong \n chuan) doc tu
        # stdout; stderr van duoc gom (bang thread rieng, tranh deadlock) de lay
        # thong bao loi khi fail.
        run_cmd = list(cmd)
        run_cmd[-1:-1] = ["-progress", "pipe:1", "-nostats"]

        proc = subprocess.Popen(
            run_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )

        stderr_chunks: list[bytes] = []

        def _drain_stderr(pipe):
            try:
                for chunk in iter(lambda: pipe.read(4096), b""):
                    stderr_chunks.append(chunk)
            except Exception:
                pass

        import threading
        import time as _time

        t_err = threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True)
        t_err.start()

        #  CANH HUY o LUONG RIENG: vong doc tien do ben duoi bi chan o ``readline``
        #  nen tu no khong canh duoc co huy.
        da_huy = {"co": False}
        t_huy = None
        if should_cancel:
            def _canh_huy():
                while proc.poll() is None:
                    try:
                        if should_cancel():
                            da_huy["co"] = True
                            proc.kill()
                            return
                    except Exception:  # noqa: BLE001 — doc co huy hong thi thoi
                        pass
                    _time.sleep(0.5)

            t_huy = threading.Thread(target=_canh_huy, daemon=True)
            t_huy.start()

        if proc.stdout:
            for raw_line in iter(proc.stdout.readline, b""):
                if not raw_line:
                    break
                if not (on_progress and duration_sec > 0):
                    continue
                line = raw_line.decode("utf-8", errors="replace").strip()
                # out_time_us=1234567 (micro giay); ban ffmpeg cu dung out_time_ms
                # nhung gia tri thuc te van la micro giay (quirk lich su).
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        current = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    pct = int(10 + (current / duration_sec) * 85)
                    emit(pct, L("Đang render video...", "Rendering video..."))

        proc.wait()
        if t_huy:
            t_huy.join(timeout=2)
        t_err.join(timeout=5)
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        if da_huy["co"]:
            #  Dau rieng de ben goi khong thu lai bang encoder khac: bi huy KHONG phai
            #  la "encoder nay hong".
            return False, "__CANCELLED__"

        if proc.returncode == 0:
            if on_progress:
                on_progress(99, L("Hoàn tất render...", "Finishing the render..."))
            return True, ""
        return False, stderr_text.strip() or "Unknown FFmpeg error"

    @staticmethod
    def intermediate_encode_args() -> tuple[list[str], float, str]:
        """Thong so nen HINH cho BAN TRUNG GIAN (buoc 1/2 cua che do Chi tiet).

        Tra ve ``(tham_so_ffmpeg, MB_moi_giay_uoc_tinh, nhan_de_ghi_log)``.
        Khong co encoder GPU -> tra ``([], 0, "")`` de ben goi di duong libx264 cu.

        VI SAO O DAY chu khong o ``review_advanced_engine``: bo do encoder GPU nam san
        trong file nay (``_detect_hw_encoder``), va no cache ket qua. De hai noi tu do
        lay la co ngay hai cau tra loi khac nhau tren cung mot may.

        DO THAT tren may Intel iGPU (QSV), nguon 1080p30 60 giay, encode tu TEP co san:

            libx264 ultrafast crf18 (duong cu) :  7,1s  ·  140,3 MB
            h264_qsv  global_quality 21        : 10,6s  ·   61,4 MB

        Tuc tren iGPU Intel thi GPU CHAM HON 1,5 lan nhung TEP NHO HON 2,3 lan. Van
        chon GPU, vi ba ly do, theo thu tu quan trong:

          1. Ban trung gian cua phim 3 tieng uoc ~32,5GB o duong cu — xuong con ~14GB.
             Buoc 2/2 con phai doc lai nguyen tep do, nen phan tiet kiem nay tinh ca o
             cho ghi lan cho doc.
          2. Giai phong CPU. Day dung la dieu nguoi dung bao: "co GPU ma ffmpeg van an
             CPU". CPU bi chiem thi ca may cham theo, ma app con dang lam viec khac.
          3. Tren card NVIDIA roi (NVENC) thi con NHANH HON HAN, khong chi nho hon —
             day moi la cau hinh pho bien cua nguoi dung nang.
        """
        enc = _detect_hw_encoder()
        if enc == "h264_nvenc":
            #  p4 = can bang; ``-b:v 0`` de ``-cq`` thanh che do chat luong khong doi.
            return (
                ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "21",
                 "-b:v", "0", "-pix_fmt", "yuv420p"],
                1.10,
                "h264_nvenc cq21",
            )
        if enc == "h264_qsv":
            return (
                ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "21",
                 "-pix_fmt", "nv12"],
                1.04,          # do duoc: 62,7MB / 60s tren nguon kho nen nhat
                "h264_qsv gq21",
            )
        if enc == "h264_amf":
            return (
                ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
                 "-qp_i", "21", "-qp_p", "21", "-qp_b", "21", "-pix_fmt", "yuv420p"],
                1.20,
                "h264_amf qp21",
            )
        return ([], 0.0, "")

    @staticmethod
    def _build_video_encode_args(
        input_path: str,
        out_w: int,
        out_h: int,
        config: dict,
        src_w: int | None,
        src_h: int | None,
        encoder: str,
    ) -> list[str]:
        """Tham so encode theo encoder cu the.

        `encoder`: "" (libx264 CPU) | "h264_nvenc" | "h264_qsv" | "h264_amf".
        Moi encoder GPU co cu phap rate-control RIENG — dung sai se fail.
        """
        target_bps = compute_target_video_bitrate(
            input_path, out_w, out_h, config, src_w, src_h
        )
        target_k = max(400, target_bps // 1000) if target_bps else 0
        max_k = max(target_k + 128, int(target_k * 1.22)) if target_k else 0
        buf_k = target_k * 2 if target_k else 0

        def _rate(pix_fmt: str) -> list[str]:
            if not target_k:
                return ["-pix_fmt", pix_fmt]
            return [
                "-pix_fmt", pix_fmt,
                "-b:v", f"{target_k}k",
                "-maxrate", f"{max_k}k",
                "-bufsize", f"{buf_k}k",
            ]

        if encoder == "h264_nvenc":
            # NVIDIA: p3 thien toc do (nhanh hon p4, chat luong gan nhu y het o
            # cung bitrate); rc vbr voi cq neu khong co bitrate.
            base = ["-c:v", "h264_nvenc", "-preset", "p3", "-rc", "vbr"]
            if not target_k:
                base += ["-cq", "27"]
            return base + _rate("yuv420p")

        if encoder == "h264_qsv":
            # Intel Quick Sync (Arc/Iris): nhan frame nv12; global_quality khi
            # khong co bitrate muc tieu. preset `faster` uu tien toc do.
            base = ["-c:v", "h264_qsv", "-preset", "faster"]
            if not target_k:
                base += ["-global_quality", "25"]
            return base + _rate("nv12")

        if encoder == "h264_amf":
            # AMD AMF: quality=speed, rc cqp khi khong co bitrate.
            base = ["-c:v", "h264_amf", "-quality", "speed"]
            if not target_k:
                base += ["-rc", "cqp", "-qp_i", "24", "-qp_p", "24", "-qp_b", "24"]
            else:
                base += ["-rc", "vbr_peak"]
            return base + _rate("yuv420p")

        # CPU (libx264): 'faster' nhanh hon 'medium' ~2x. Da ep bitrate cap nen
        # dung luong file GIU NGUYEN, chi giam chat luong khong dang ke — hop ly
        # cho video review/long tieng.
        # 'veryfast' thay 'faster' (2.13.2): may KHONG GPU la may yeu nhat, va encode la
        # phan nang nhat cua duong xuat o do. Do khung 1080x1920, 30s video, chi scale +
        # encode: faster 24-40s · veryfast 12-22s · superfast 6,6s. Nguoi dung may CPU
        # bao xuat 5 phut mat 17 phut. veryfast la diem can bang x264 khuyen dung: chat
        # luong o cung bitrate gan nhu khong doi (bitrate da kep boi -b:v/-maxrate),
        # superfast nhanh hon nua nhung bo nhieu buoc phan tich — chua dung mac dinh.
        base = ["-c:v", "libx264", "-preset", "veryfast"]
        if target_k:
            base += ["-profile:v", "high"]
            return base + _rate("yuv420p")
        return base + ["-crf", "26", "-pix_fmt", "yuv420p"]

    @staticmethod
    def render(
        input_path: str,
        output_path: str,
        config: dict,
        segments: list[dict] | None = None,
        *,
        source_segments: list[dict] | None = None,
        on_progress=None,
        duration_sec: float | None = None,
        should_cancel=None,
        bao_loi=None,
    ) -> bool:
        """``bao_loi(text)`` — goi kem nguyen nhan khi render hong.

        VI SAO CO: ham nay chi tra True/False, nen ben goi xua nay bao cho nguoi dung
        dung mot cau "Compositor rendering failed" — khong nguyen nhan, khong tieng
        Viet, khong lam gi duoc. Ly do that chi nam trong nhat ky, ma nhat ky lai bi
        cat mat duoi. Day la duong de nguyen nhan di ra toi man hinh.
        """
        input_path = os.path.normpath(os.path.abspath(input_path))
        output_path = os.path.normpath(os.path.abspath(output_path))
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if on_progress:
            on_progress(2, L("Chuẩn bị phụ đề và overlay...", "Preparing subtitles and overlays..."))

        if duration_sec is None:
            from services.video_service import VideoService

            duration_sec = VideoService.get_video_duration(input_path)
        duration_sec = max(float(duration_sec or 0), 0.1)

        src_w, src_h = probe_video_dimensions(input_path)
        out_w, out_h = get_output_dimensions(config, src_w, src_h)

        ass_path = None
        fonts_dir = None
        if segments and config.get("subtitleStyle"):
            ass_path = _side_file_stem(output_path) + ".ass"
            #  Tiếng Nga + font thiếu chữ Kirin -> đổi font (xem _cyrillic_safe). Ghi lại
            #  vào config để watermark (dùng font phụ đề) cũng thấy đúng font.
            sub_style = CompositorService._font_an_toan(
                config["subtitleStyle"],
                (sg.get("target_text") or sg.get("source_text") or "" for sg in segments),
            )
            config = {**config, "subtitleStyle": sub_style}
            font_path = CompositorService._resolve_font_file(sub_style)
            if font_path:
                fonts_dir = os.path.dirname(font_path)
            ass_content = AssGenerator.generate(
                segments,
                sub_style,
                play_res_x=out_w,
                play_res_y=out_h,
                crop_frame=config.get("cropFrame") or {},
                # De AssGenerator quy doi co chu CSS -> ASS Fontsize theo metrics
                # cua dung font se dot vao video (xem utils/font_metrics.py).
                font_path=font_path or None,
            )
            with open(ass_path, "w", encoding="utf-8-sig") as f:
                f.write(ass_content)
            config = {**config, "_ass_path": ass_path}
            if fonts_dir:
                config = {**config, "_fonts_dir": fonts_dir}

        #  PHU DE GOC (cau nhan dang / nhap vao) — DOT THEM mot lop nua khi nguoi dung
        #  bat nut "Sub goc". Day la lop DOI CHIEU: nam sat day, chu nho hon loi binh va
        #  luon la chu tinh (khong karaoke, khong hien tung chu) — chay hai lop chu nhay
        #  cung luc thi khong doc noi lop nao.
        src_ass_path = None
        if source_segments and config.get("showSourceSubs") and config.get("subtitleStyle"):
            #  KIỂU dòng phụ: người dùng tự chỉnh (``secondSubStyle``) hoặc suy từ sub chính
            #  — xem ``compositor_utils.kieu_sub_phu`` (mirror frontend secondSubStyle.ts).
            from services.compositor_utils import kieu_sub_phu

            src_style = dict(CompositorService._font_an_toan(
                kieu_sub_phu(config),
                (sg.get("target_text") or sg.get("source_text") or "" for sg in source_segments),
            ))
            src_font = CompositorService._resolve_font_file(src_style)
            src_ass_path = _side_file_stem(output_path) + ".src.ass"
            with open(src_ass_path, "w", encoding="utf-8-sig") as f:
                f.write(AssGenerator.generate(
                    source_segments,
                    src_style,
                    play_res_x=out_w,
                    play_res_y=out_h,
                    crop_frame=config.get("cropFrame") or {},
                    font_path=src_font or None,
                ))
            config = {**config, "_src_ass_path": src_ass_path}
            #  Font tự tải lên của dòng phụ: libass chỉ thấy khi có ``fontsdir``.
            if src_font:
                config = {**config, "_src_fonts_dir": os.path.dirname(src_font)}

        logo = config.get("logo", {})
        has_logo = bool(
            logo.get("enabled") and logo.get("path") and os.path.exists(logo["path"])
        )
        has_ass = ass_path is not None

        wm_text_file = None
        wm = config.get("watermark", {})
        if wm.get("enabled") and (wm.get("text") or "").strip():
            wm_text_file = _side_file_stem(output_path) + ".wm.txt"
            with open(wm_text_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(wm["text"].strip())
            config = {**config, "_wm_text_file": wm_text_file}

        # File chua noi dung tung khoi chu chen (ho tro xuong dong + ky tu dac
        # biet + uppercase da chuyen san) — drawtext doc qua textfile.
        text_overlay_files: dict[str, str] = {}
        base_no_ext = _side_file_stem(output_path)
        for idx, ov in enumerate(config.get("textOverlays", []) or []):
            if not ov.get("enabled", True):
                continue
            txt = ov.get("text") or ""
            if not txt.strip():
                continue
            if ov.get("uppercase"):
                txt = txt.upper()
            tf_path = f"{base_no_ext}.text{idx}.txt"
            try:
                with open(tf_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(txt)
                text_overlay_files[ov.get("id", str(idx))] = tf_path
            except OSError:
                pass
        if text_overlay_files:
            config = {**config, "_text_overlay_files": text_overlay_files}

        #  Khối chữ chèn có chữ Kirin: đổi font TRƯỚC khi dựng filter, để cả drawtext lẫn
        #  phép đo bẻ dòng (_be_dong_cho_vua) dùng CÙNG một font.
        if config.get("textOverlays"):
            config = {**config, "textOverlays": [
                CompositorService._font_an_toan(ov, [ov.get("text") or ""])
                if isinstance(ov, dict) else ov
                for ov in config["textOverlays"]
            ]}

        #  fps nguon cho cac nen vo han (anh nen ``loop`` / nen mau): xem probe_video_fps.
        src_fps = probe_video_fps(input_path)
        filter_complex, extra_inputs = CompositorService.build_filter_complex(
            config, out_w, out_h, has_logo, has_ass, src_fps=src_fps
        )

        # --- Chen audio (nhac nen): tu lap tay toi het video, KHOP am luong
        # voi preview (audioTrack.volume 0..1). Original audio giu nguyen (1.0),
        # audio chen mix o muc volume nguoi dung chinh o preview. ---
        audio = config.get("audioTrack", {}) or {}
        audio_path = audio.get("path", "")
        has_audio_track = bool(
            audio.get("enabled") and audio_path and os.path.exists(audio_path)
        )
        audio_map = ["-map", "0:a?"]
        audio_codec = ["-c:a", "copy"]
        add_shortest = False
        if has_audio_track:
            audio_index = 1 + len(extra_inputs)  # 0=video, 1..N=extra_inputs
            vol = max(0.0, min(2.0, float(audio.get("volume", 0.5))))
            orig_has_audio = _has_audio_stream(input_path)
            if orig_has_audio:
                # normalize=0 -> giu nguyen muc tung nguon (khong tu ha am luong),
                # duration=first -> cat theo do dai audio goc = do dai video.
                audio_filter = (
                    f"[0:a]volume=1[a0];"
                    f"[{audio_index}:a]volume={vol:.4f}[a1];"
                    f"[a0][a1]amix=inputs=2:duration=first:normalize=0[aout]"
                )
            else:
                # Video khong co audio goc -> chi phat nhac chen (cat theo video).
                audio_filter = f"[{audio_index}:a]volume={vol:.4f}[aout]"
                add_shortest = True
            filter_complex = f"{filter_complex};{audio_filter}"
            audio_map = ["-map", "[aout]"]
            audio_codec = ["-c:a", "aac", "-b:a", "192k"]

        try:
            # Uu tien encode GPU (QSV/NVENC/AMF) khi kha dung — doc lap voi
            # torch. Neu that bai runtime (driver/VRAM) -> tu dong roi ve CPU.
            hw_encoder = _detect_hw_encoder()
            # Bac thang xu ly (decode_args, encoder) — rot dan cho an toan:
            #   1) GPU encode + CPU decode.
            #   2) FULL CPU (libx264).
            #
            # DA BO BAC "-hwaccel auto" (decode tren GPU): no LAM HONG MAU.
            #
            # Do that tren may co QSV: nguon 3 dai DO/XANH LA/XANH DUONG, xuat ra CA BA
            # deu thanh xanh la (0,229,0). Ly do: -hwaccel auto tra khung o dinh dang
            # phan cung, ffmpeg tu chen buoc tai ve RAM nhung filter CPU phia sau
            # (subtitles/blur/logo) dien giai sai kenh mau. Comment cu tuong rang khong
            # dat -hwaccel_output_format thi an toan — thuc te KHONG.
            #
            # Nguy hiem nhat: ffmpeg tra ve exit 0, khong canh bao gi. Bac thang chi roi
            # xuong khi LOI, nen ca video hong mau van duoc coi la xuat thanh cong. Toc do
            # decode GPU khong dang de doi lay rui ro do — phan nang van la filter + encode.
            if hw_encoder:
                attempts = [
                    ([], hw_encoder),
                    ([], ""),
                ]
            else:
                attempts = [([], "")]
            #  Giai ma GPU (thu nghiem, mac dinh tat — xem ``_hw_decode_mode``): them MOT
            #  bac dung dau, hong thi roi xuong dung bac thang cu.
            hw_dec = _hw_decode_mode()
            if hw_dec and _with_hw_download(filter_complex) is not None:
                attempts.insert(0, (_HW_DECODE_ARGS[hw_dec], hw_encoder or ""))
            last_error = ""

            for decode_args, encoder in attempts:
                cmd = [FFMPEG_EXE, "-y"]
                # -hwaccel la INPUT option: dat NGAY TRUOC -i video chinh nen chi
                # ap cho input do (cac input anh mask/logo van decode CPU binh
                # thuong). Filter graph van chay CPU tren frame da tai ve RAM.
                cmd.extend(decode_args)
                cmd.extend(["-i", input_path])
                for extra in extra_inputs:
                    cmd.extend(["-i", extra])
                if has_audio_track:
                    # -stream_loop -1 phai dung TRUOC -i cua chinh audio do.
                    cmd.extend(["-stream_loop", "-1", "-i", audio_path])
                fc = _with_hw_download(filter_complex) if decode_args else filter_complex
                #  BỘ LỌC DÀI (vùng che "chỉ khi có sub" với hàng trăm khoảng) -> ghi ra tệp:
                #  dòng lệnh Windows tối đa ~32.000 ký tự, vượt là ffmpeg không chạy được
                #  mà lỗi trả về chẳng nói gì về lý do.
                if len(fc) > 8000:
                    fc_path = output_path + ".filter.txt"
                    with open(fc_path, "w", encoding="utf-8") as _f:
                        _f.write(fc)
                    cmd.extend(["-filter_complex_script", fc_path, "-map", "[vout]"])
                else:
                    cmd.extend(["-filter_complex", fc, "-map", "[vout]"])
                cmd.extend(audio_map)
                cmd.extend(
                    CompositorService._build_video_encode_args(
                        input_path, out_w, out_h, config, src_w, src_h, encoder
                    )
                )
                cmd.extend(audio_codec)
                if add_shortest:
                    cmd.append("-shortest")
                cmd.append(output_path)

                enc_label = encoder or "libx264 (CPU)"
                dec_label = "GPU-decode" if decode_args else "CPU-decode"
                logger.info(
                    f"Compositor render ({dec_label} + {enc_label}): {filter_complex[:200]}..."
                )
                if on_progress and encoder and decode_args:
                    on_progress(6, L(f"Render GPU toàn phần ({encoder})...", f"Full GPU render ({encoder})..."))
                elif on_progress and encoder:
                    on_progress(6, f"Render GPU ({encoder})...")
                elif on_progress:
                    on_progress(6, "Render CPU (libx264)...")

                ok, stderr = CompositorService._run_ffmpeg(
                    cmd,
                    duration_sec=duration_sec,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
                if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    logger.info(f"COMPOSITOR EXPORT COMPLETED: {output_path}")
                    for tmp in [ass_path, wm_text_file, src_ass_path, *text_overlay_files.values(),
                                *(f"{p}{d}" for p in text_overlay_files.values()
                                  for d in (".box.png", ".wrap.txt"))]:
                        if tmp and os.path.exists(tmp):
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                    if on_progress:
                        on_progress(100, L("Xuất video hoàn tất!", "Video export complete!"))
                    return True

                last_error = stderr or "Unknown FFmpeg error"
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except OSError:
                        pass
                #  BI HUY thi DUNG HAN — dung thu tiep encoder khac. Vong nay von de
                #  vot khi mot encoder GPU hong; huy khong phai la "encoder hong", ma
                #  thu tiep nghia la nguoi dung bam Huy xong may lai chay them mot luot
                #  nua tu dau.
                if last_error == "__CANCELLED__":
                    logger.warning("Compositor render: nguoi dung da huy — dung han.")
                    return False
                if encoder or decode_args:
                    stage = (
                        f"GPU-decode + {encoder}" if decode_args and encoder
                        else (encoder or "GPU-decode")
                    )
                    logger.warning(
                        f"Compositor render ({stage}) that bai, thu buoc ke tiep: "
                        f"{tom_loi_ffmpeg(last_error)}"
                    )

            #  Ghi PHAN DUOI cua stderr. Bao loi gui ve bi cat theo DAU chuoi, ma dau
            #  stderr ffmpeg toan banner + libass liet ke font Windows — de nguyen thi
            #  loi khach gui len khong chan doan duoc gi (da co ca that).
            duoi = tom_loi_ffmpeg(last_error)
            logger.error(f"Compositor render failed: {duoi}")
            if bao_loi:
                bao_loi(duoi)
            return False
        except Exception as e:
            logger.exception(f"Compositor render error: {e}")
            if bao_loi:
                bao_loi(str(e))
            return False
        finally:
            #  ``ass_path`` (phụ đề CHÍNH) và ``wm_text_file`` cũng là tệp tạm: bản trước chỉ dọn lúc xuất thành công và QUÊN ``ass_path``, nên cạnh mỗi video xuất ra còn một tệp
            #  ``[XUẤT] ….ass`` cùng tên — trình phát (Windows Media Player, VLC, MPC) tự nạp phụ đề cùng tên, bật sẵn, nên chữ đốt hiện thêm lần nữa và nó là mục DUY NHẤT trong
            #  menu phụ đề, che mất các luồng phụ đề mềm đã nhúng (người dùng báo 05-10-2026).
            for tmp in [ass_path, wm_text_file, src_ass_path, output_path + ".filter.txt", *text_overlay_files.values(),
                        *(f"{p}{d}" for p in text_overlay_files.values()
                          for d in (".box.png", ".wrap.txt"))]:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
