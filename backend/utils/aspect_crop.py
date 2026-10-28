"""Tỷ lệ xuất + khung cắt video cho compositor."""
import os
import subprocess
from typing import Tuple

import imageio_ffmpeg

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

PRESET_ASPECT = {
    "phone": (9, 16),
    "tablet": (4, 3),
    "laptop": (16, 10),
    "monitor": (16, 9),
}

RESOLUTION_MAP = {
    "720p": {"phone": (720, 1280), "tablet": (960, 720), "laptop": (1280, 800), "monitor": (1280, 720)},
    "1080p": {"phone": (1080, 1920), "tablet": (1440, 1080), "laptop": (1920, 1200), "monitor": (1920, 1080)},
    "2k": {"phone": (1440, 2560), "tablet": (2048, 1536), "laptop": (2560, 1600), "monitor": (2560, 1440)},
}


def get_aspect_tuple(config: dict) -> Tuple[int, int]:
    key = config.get("aspectRatio", "source")
    if key == "source":
        return 16, 9
    if key == "custom":
        aw = max(1, int(config.get("customAspectW") or 9))
        ah = max(1, int(config.get("customAspectH") or 16))
        return aw, ah
    return PRESET_ASPECT.get(key, (16, 9))


def probe_video_dimensions(path: str) -> Tuple[int, int]:
    """Lấy width×height video gốc (dùng khi aspectRatio === source)."""
    path = os.path.normpath(path)
    ffprobe = FFMPEG_EXE.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
    if os.path.exists(ffprobe):
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "csv=p=0:s=x",
                    path,
                ],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
                check=False,
            )
            line = (result.stdout or "").strip().splitlines()[0] if result.stdout else ""
            if "x" in line:
                w_s, h_s = line.split("x", 1)
                w, h = int(w_s), int(h_s)
                if w > 0 and h > 0:
                    return w, h
        except (OSError, ValueError, IndexError):
            pass

    try:
        result = subprocess.run(
            [FFMPEG_EXE, "-i", path],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=30,
            check=False,
        )
        wh = _parse_ffmpeg_video_size(result.stderr or "")
        if wh:
            return wh
    except OSError:
        pass
    #  Không đọc được thì đoán 1920x1080 — ghi lại, vì đoán sai là hỏng âm thầm: OCR
    #  khoanh vùng tràn khung (ffmpeg -22, "0 câu"), bản xuất sai tỷ lệ.
    import logging

    logging.getLogger(__name__).warning(
        "Không đọc được kích thước video %s — tạm coi là 1920x1080.", path
    )
    return 1920, 1080


def _parse_ffmpeg_video_size(stderr: str) -> Tuple[int, int] | None:
    """Kích thước HIỂN THỊ từ ``ffmpeg -i``: dòng luồng ``Video:`` đầu tiên không phải
    ảnh bìa, đổi chiều khi có cờ xoay ±90° (ffmpeg tự xoay khung khi giải mã).

    Trước đây lấy cặp NxN ĐẦU TIÊN trong cả đoạn stderr — tên tệp, metadata hay ảnh bìa
    có "1920x1080" là ra sai. Và stderr từng bị giải mã bằng bảng mã máy (cp932/cp1258):
    tên tệp tiếng Trung / tiếng Việt làm hỏng lượt đọc, hàm lặng lẽ trả 1920x1080 cho
    video dọc 720x1280 -> OCR báo "0 câu" ở mọi chế độ (24-09-2026)."""
    import re

    lines = stderr.splitlines()
    for i, line in enumerate(lines):
        if "Video:" not in line or "attached pic" in line:
            continue
        m = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line.split("Video:", 1)[1])
        if not m:
            continue
        w, h = int(m.group(1)), int(m.group(2))
        if w <= 0 or h <= 0:
            continue
        #  Cờ xoay nằm ở các dòng Side data / Metadata ngay dưới luồng này.
        for nxt in lines[i + 1:i + 12]:
            if "Stream #" in nxt:
                break
            r = re.search(r"rotat(?:e|ion)\D{0,12}(-?\d+(?:\.\d+)?)", nxt)
            if r and round(abs(float(r.group(1)))) % 180 == 90:
                w, h = h, w
                break
        return w, h
    return None


def _ffprobe_path() -> str:
    return FFMPEG_EXE.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")


def probe_video_bitrate(path: str) -> int | None:
    """Bitrate video (bits/s) — ưu tiên stream, fallback format."""
    path = os.path.normpath(path)
    ffprobe = _ffprobe_path()
    if not os.path.exists(ffprobe):
        return None

    def _read_bitrate(args: list[str]) -> int | None:
        try:
            result = subprocess.run(
                [ffprobe, *args],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
                check=False,
            )
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    value = int(line)
                    if value > 0:
                        return value
        except (OSError, ValueError):
            pass
        return None

    stream_bps = _read_bitrate(
        [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
    )
    if stream_bps:
        return stream_bps

    format_bps = _read_bitrate(
        [
            "-v",
            "error",
            "-show_entries",
            "format=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
    )
    if format_bps:
        return format_bps

    return None


def compute_target_video_bitrate(
    input_path: str,
    out_w: int,
    out_h: int,
    config: dict,
    src_w: int | None = None,
    src_h: int | None = None,
) -> int | None:
    """Mục tiêu bitrate theo nguồn — tránh phình file khi re-encode CRF cố định."""
    src_bps = probe_video_bitrate(input_path)
    if not src_bps:
        return None

    if not src_w or not src_h:
        src_w, src_h = probe_video_dimensions(input_path)

    crop = config.get("cropFrame") or {}
    cw = max(float(crop.get("w", 1)), 0.05)
    ch = max(float(crop.get("h", 1)), 0.05)
    src_pixels = max(src_w * src_h * cw * ch, 1)
    out_pixels = max(out_w * out_h, 1)
    pixel_ratio = out_pixels / src_pixels

    target = int(src_bps * max(pixel_ratio, 0.8) * 1.08)
    cap = int(src_bps * 1.3)
    floor_bps = 450_000
    return max(floor_bps, min(target, cap))


def get_output_dimensions(
    config: dict,
    src_w: int | None = None,
    src_h: int | None = None,
) -> Tuple[int, int]:
    res = config.get("resolution", "1080p")
    ref = RESOLUTION_MAP.get(res, RESOLUTION_MAP["1080p"])
    key = config.get("aspectRatio", "source")

    if key == "source" and src_w and src_h and src_h > 0:
        # "GIU NGUYEN NGUON" = giu nguyen THU NGUOI DUNG CAT RA, khong phai giu ty le
        # cua tep goc.
        #
        # VI SAO PHAI NHAN cropFrame: o tuy chon nay khung cat keo TU DO (OverlayCanvas
        # co y khong khoa ty le). Neu kich thuoc xuat van lay theo ty le tep goc thi
        # ``build_crop_scale_filter`` cat vung do roi SCALE EP ve ty le goc -> anh keo
        # dan. Do that voi ca nguoi dung gap: nguon 16:9, cat 39%x100% -> hinh tron
        # trong ban xuat thanh bau duc 2,57 lan, ma khong co gi bao loi.
        #
        # BAN SOI GUONG: ``getOutputAspectRatio`` trong frontend/src/utils/cropFrame.ts
        # (khung xem truoc phai doan dung kich thuoc nay, lech la xem mot dang xuat mot neo).
        crop = config.get("cropFrame") or {}
        cw = max(0.05, min(1.0, float(crop.get("w", 1) or 1)))
        ch = max(0.05, min(1.0, float(crop.get("h", 1) or 1)))
        aspect = (src_w * cw) / (src_h * ch)
        phone = ref.get("phone", (1080, 1920))
        monitor = ref.get("monitor", (1920, 1080))
        if aspect <= 1:
            out_h = phone[1]
            out_w = int(round(out_h * aspect))
        else:
            out_w = monitor[0]
            out_h = int(round(out_w / aspect))
        out_w = max(2, out_w - out_w % 2)
        out_h = max(2, out_h - out_h % 2)
        return out_w, out_h

    if key != "custom":
        dims = ref.get(key)
        if dims:
            return dims

    aw, ah = get_aspect_tuple(config)
    phone = ref.get("phone", (1080, 1920))
    monitor = ref.get("monitor", (1920, 1080))
    if ah >= aw:
        out_h = phone[1]
        out_w = int(out_h * aw / ah)
    else:
        out_w = monitor[0]
        out_h = int(out_w * ah / aw)
    out_w = max(2, out_w - out_w % 2)
    out_h = max(2, out_h - out_h % 2)
    return out_w, out_h


def compute_center_crop_frame(src_aspect: float, target_aspect: float) -> dict:
    """Vùng cắt chuẩn hóa 0..1 trên video gốc."""
    if src_aspect <= 0 or target_aspect <= 0:
        return {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    if src_aspect > target_aspect:
        h = 1.0
        w = target_aspect / src_aspect
        x = (1.0 - w) / 2.0
        y = 0.0
    else:
        w = 1.0
        h = src_aspect / target_aspect
        x = 0.0
        y = (1.0 - h) / 2.0
    return {
        "x": max(0.0, min(1.0, x)),
        "y": max(0.0, min(1.0, y)),
        "w": max(0.05, min(1.0, w)),
        "h": max(0.05, min(1.0, h)),
    }


def build_crop_scale_filter(config: dict, out_w: int, out_h: int) -> str:
    """FFmpeg: cắt theo cropFrame rồi scale ra kích thước xuất."""
    crop = config.get("cropFrame") or {}
    cx = max(0.0, min(1.0, float(crop.get("x", 0))))
    cy = max(0.0, min(1.0, float(crop.get("y", 0))))
    cw = max(0.05, min(1.0, float(crop.get("w", 1))))
    ch = max(0.05, min(1.0, float(crop.get("h", 1))))

    if cx + cw > 1.001:
        cx = max(0.0, 1.0 - cw)
    if cy + ch > 1.001:
        cy = max(0.0, 1.0 - ch)

    return (
        f"crop=w='trunc(iw*{cw:.6f})':h='trunc(ih*{ch:.6f})':"
        f"x='trunc(iw*{cx:.6f})':y='trunc(ih*{cy:.6f})',"
        f"scale={out_w}:{out_h}"
    )


#: Khung co the RONG HON video (nguoi dung thu nho video cho lot vao khung) —
#: luc do quanh video co cho trong, phai to bang gi do. Hai kieu nen:
#:   "blur"  — chinh khung hinh do phong to cho phu kin roi lam mo (kieu TikTok/CapCut)
#:   "color" — mot mau don
#:   "image" — mot tam anh nguoi dung tai len (``image`` = duong dan tren dia)
#: BAN SOI GUONG: ``FrameBackground`` trong frontend/src/types/compositor.ts.
DEFAULT_FRAME_BG = {"type": "blur", "color": "#000000", "blur": 28, "image": ""}


def probe_video_fps(path: str, default: float = 30.0) -> float:
    """Toc do khung hinh cua video goc (r_frame_rate), ``default`` neu khong doc duoc.

    Dung cho cac NGUON NEN VO HAN (nen mau ``color``, anh nen ``loop``): chung la luong
    CHINH cua ``overlay`` nen quyet dinh fps dau ra. Co dinh 30 nhu truoc la video 25/60fps
    bi nhan/bo khung; theo fps that thi moi khung video giu nguyen mot khung dau ra.
    """
    path = os.path.normpath(path)
    ffprobe = FFMPEG_EXE.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
    if not os.path.exists(ffprobe):
        return default
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        raw = (result.stdout or "").strip().splitlines()[0] if result.stdout else ""
        if "/" in raw:
            a, b = raw.split("/", 1)
            fps = float(a) / float(b) if float(b) else 0.0
        else:
            fps = float(raw) if raw else 0.0
        if 1.0 <= fps <= 240.0:
            return round(fps, 3)
    except (OSError, ValueError, IndexError, ZeroDivisionError):
        pass
    return default


def frame_bg_image_path(config: dict) -> str:
    """Duong dan ANH NEN can nap them vao ffmpeg, hoac "" neu khong dung anh.

    Tra "" luon khi tep khong con ton tai: anh nen nam trong thu muc du an, xoa tay
    hoac chep du an sang may khac la mat. Luc do quay ve nen mo — thay vi de ffmpeg
    chet giua chung voi mot dong loi ma nguoi dung khong doc duoc.
    """
    bg = {**DEFAULT_FRAME_BG, **(config.get("frameBackground") or {})}
    if str(bg.get("type") or "blur") != "image":
        return ""
    path = str(bg.get("image") or "").strip()
    return path if path and os.path.exists(path) else ""


def frame_has_gap(config: dict) -> bool:
    """Khung co CHO TRONG quanh video khong (video khong phu kin khung)?

    ``cropFrame`` la vung cat tren video goc. Vuot ra ngoai [0,1] nghia la khung rong
    hon video — tuc nguoi dung da thu nho video lai cho lot vao khung, va phan thua
    quanh no phai ve nen.
    """
    crop = config.get("cropFrame") or {}
    try:
        x = float(crop.get("x", 0.0))
        y = float(crop.get("y", 0.0))
        w = float(crop.get("w", 1.0))
        h = float(crop.get("h", 1.0))
    except (TypeError, ValueError):
        return False
    eps = 0.0005
    return x < -eps or y < -eps or x + w > 1 + eps or y + h > 1 + eps


def build_frame_filters(config: dict, out_w: int, out_h: int,
                        src: str = "[vsrc]", dst: str = "[vbase]",
                        bg_image_index: int | None = None,
                        src_fps: float | None = None) -> list:
    """Dat video vao KHUNG xuat — tra ve danh sach doan filter cua ffmpeg.

    Hai duong:

    · Video PHU KIN khung (``cropFrame`` nam gon trong [0,1]) — duong cu:
      ``crop`` roi ``scale``. Cat truoc nen it diem anh phai scale, nhanh hon han.

    · Khung RONG HON video (nguoi dung thu nho video lai cho lot vao khung, kieu
      CapCut): khong ``crop`` duoc nua (ffmpeg khong cho cat ra ngoai bien anh), nen
      dung ``scale`` roi ``overlay`` len mot cai nen dung bang khung. Toa do co the AM
      — ``overlay`` tu cat phan tran ra ngoai, dung nhu nguoi dung nhin thay o khung
      xem truoc.

    Toan: mot don vi 0..1 cua video goc trai dai ``out_w/w`` diem anh trong khung, nen
    video duoc scale len ``(out_w/w) x (out_h/h)`` va dat tai ``(-x*W2, -y*H2)``.
    """
    crop = config.get("cropFrame") or {}
    cx = float(crop.get("x", 0.0) or 0.0)
    cy = float(crop.get("y", 0.0) or 0.0)
    cw = max(0.01, float(crop.get("w", 1.0) or 1.0))
    ch = max(0.01, float(crop.get("h", 1.0) or 1.0))

    if not frame_has_gap(config):
        # Duong cu — giu nguyen tung ky tu de ban xuat cua du an cu khong doi.
        return [f"{src}{build_crop_scale_filter(config, out_w, out_h)}{dst}"]

    vw = max(2, int(round(out_w / cw)))
    vh = max(2, int(round(out_h / ch)))
    vw -= vw % 2
    vh -= vh % 2
    ox = int(round(-cx * out_w / cw))
    oy = int(round(-cy * out_h / ch))

    bg = {**DEFAULT_FRAME_BG, **(config.get("frameBackground") or {})}
    bg_type = str(bg.get("type") or "blur")
    #  Chon anh nhung anh da mat (hoac nguoi goi khong nap input) -> quay ve nen mo.
    if bg_type == "image" and bg_image_index is None:
        bg_type = "blur"
    out = []
    fps = float(src_fps) if src_fps and src_fps > 0 else 30.0
    if bg_type == "image":
        #  ANH NEN phu kin khung ("cover": scale trum het roi cat phan thua).
        #
        #  Anh chi co MOT khung hinh nen khong tu lam nen cua ``overlay`` duoc (luong
        #  nen het sau khung dau). Ban dau lay CHINH VIDEO scale len co khung lam tam
        #  nen dung nhip roi dan anh (rgba) de len o MOI khung hinh — hai phep nang
        #  chay 30 lan/giay chi de ve mot cai nen tinh. Do tren video 960x720 -> 1080p:
        #  1,3x thoi gian that chi rieng phan loc, phim 4 phut xuat hon 10 phut.
        #
        #  Nay: scale + cat anh MOT LAN, roi ``loop`` lap lai dung khung do (khong giai
        #  ma lai, khong scale lai), ``fps`` dat ve nhip cua video. Moi khung chi con
        #  MOT phep overlay video len nen — bang nhanh nen mau. ``shortest=1`` vi nen la
        #  nguon vo han. (Anh PNG co vung trong suot: vung do ra mau nen cua anh sau khi
        #  bo alpha — truoc day la video mo ben duoi, mot hieu ung khong ai co y dung.)
        out.append(
            f"[{bg_image_index}:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},format=yuv420p,loop=loop=-1:size=1:start=0,"
            f"fps={fps:g}[bg]"
        )
        out.append(f"{src}scale={vw}:{vh}[fg]")
        out.append(f"[bg][fg]overlay={ox}:{oy}:format=auto:shortest=1{dst}")
        return out
    if bg_type == "blur":
        #  Nen = CHINH khung hinh do, phong to cho PHU KIN khung roi lam mo.
        #  ``increase`` + ``crop`` = phep "cover": scale giu ty le cho trum het khung roi
        #  cat phan thua; thieu buoc nay thi nen bi keo dan theo khung.
        sigma = max(1, int(bg.get("blur") or DEFAULT_FRAME_BG["blur"]))
        out.append(f"{src}split[fgsrc][bgsrc]")
        #  LAM MO O 1/4 DO PHAN GIAI roi phong len. ``gblur`` tren nguyen khung 1080p la
        #  phan nang nhat cua ca do thi (do: 29,7s cho 30s video, tuc CHAM HON thoi gian
        #  that). Mo la mo: lam o 1/4 kich thuoc voi sigma/4 roi scale len cho ra hinh
        #  SSIM 0,983 so voi mo nguyen khung — mat khong phan biet — ma nhanh gap 2,4 lan
        #  (12,6s). Kich thuoc nho phai CHAN de yuv420p khong loi.
        bw = max(2, (out_w // 4) // 2 * 2)
        bh = max(2, (out_h // 4) // 2 * 2)
        sig_nho = max(1.0, sigma / 4.0)
        out.append(
            f"[bgsrc]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh},gblur=sigma={sig_nho:g},scale={out_w}:{out_h}:flags=bilinear[bg]"
        )
        out.append(f"[fgsrc]scale={vw}:{vh}[fg]")
    else:
        color = str(bg.get("color") or "#000000").lstrip("#")
        out.append(f"color=c=0x{color}:s={out_w}x{out_h}:r={fps:g}[bg]")
        out.append(f"{src}scale={vw}:{vh}[fg]")
    #  ``shortest=1``: nen mau la nguon VO HAN, khong chot lai thi ffmpeg chay mai.
    shortest = ":shortest=1" if bg_type != "blur" else ""
    out.append(f"[bg][fg]overlay={ox}:{oy}:format=auto{shortest}{dst}")
    return out
