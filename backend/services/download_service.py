"""Tải video từ URL (Douyin, TikTok, YouTube, ...) bằng yt-dlp.

Ghi chú Douyin/TikTok:
- yt-dlp lấy trực tiếp luồng "play address" gốc của Douyin/TikTok nên KHÔNG dính
  watermark (logo) — khác với việc tải file preview ngoài trang.
- Douyin có cơ chế chặn bot: cần User-Agent trình duyệt + Referer đúng domain thì
  mới lấy được metadata/luồng. Ta set sẵn HTTP headers cho việc này.
"""

from utils.i18n import L
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Header giả lập trình duyệt để vượt cơ chế chặn bot của Douyin.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _cookie_file_path() -> str:
    """Đường dẫn file cookie Douyin/TikTok người dùng dán (lưu trong AppData)."""
    from models.database import get_app_data_dir

    return os.path.join(get_app_data_dir(), "douyin_cookies.txt")


def _default_cookie_file_path() -> str:
    """Cookie Douyin MẶC ĐỊNH bundle sẵn trong app — dùng khi người dùng chưa dán
    cookie riêng. Xử lý cả khi chạy dev lẫn khi đóng gói PyInstaller (_MEIPASS).
    """
    import sys

    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "assets", "douyin_cookies_default.txt")


def _registrable_domain(host: str) -> str:
    """Rút gọn host cookie về 'domain gốc' 2 nhãn cuối để gộp theo nền tảng:
    '.www.douyin.com' -> 'douyin.com', '.bilibili.tv' -> 'bilibili.tv'.
    (Đủ dùng để phân biệt Douyin/TikTok/Bilibili; không cần Public Suffix List.)"""
    parts = host.lstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lstrip(".")


def _parse_cookie_domains(content: str) -> dict:
    """Gom các dòng cookie Netscape theo domain gốc -> {domain: [dòng, ...]}.
    Bỏ qua dòng comment/trống. Dòng hỏng (thiếu cột) gom vào khóa '' để không mất."""
    by_domain: dict = {}
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        cols = s.split("\t")
        # Netscape: cột 0 = domain. Có bản dùng nhiều khoảng trắng -> thử tách lại.
        if len(cols) < 7:
            cols = _re.split(r"\s+", s)
        host = cols[0] if cols else ""
        key = _registrable_domain(host) if host else ""
        by_domain.setdefault(key, []).append(line.rstrip("\r\n"))
    return by_domain


def save_cookies(content: str) -> None:
    """Lưu cookies.txt (Netscape) người dùng dán, GỘP THEO DOMAIN: domain nào có
    trong nội dung mới thì thay thế domain đó, các domain cũ khác GIỮ NGUYÊN.
    Nhờ vậy dán cookie Bilibili không xóa cookie Douyin đã dán trước (và ngược lại)."""
    path = _cookie_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    merged: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                merged = _parse_cookie_domains(f.read())
        except OSError:
            merged = {}

    incoming = _parse_cookie_domains(content)
    # Domain nào xuất hiện trong bản dán mới -> ghi đè hẳn domain đó.
    for domain, lines in incoming.items():
        merged[domain] = lines

    out_lines = ["# Netscape HTTP Cookie File"]
    for domain, lines in merged.items():
        out_lines.extend(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines).strip() + "\n")
    logger.info(
        f"[DOWNLOAD] Đã lưu cookie (gộp theo domain: {', '.join(d for d in merged if d) or '?'}) -> {path}"
    )


def clear_cookies() -> None:
    path = _cookie_file_path()
    if os.path.exists(path):
        os.remove(path)
        logger.info("[DOWNLOAD] Đã xóa cookie đã lưu")


def has_cookies() -> bool:
    p = _cookie_file_path()
    return os.path.exists(p) and os.path.getsize(p) > 0


def _installed_browsers() -> list:
    """Danh sách trình duyệt (theo tên yt-dlp) thực sự có profile trên máy Windows.

    Trả về theo thứ tự ưu tiên. Chỉ kiểm tra tồn tại thư mục user-data để tránh dò
    những trình duyệt không cài (gây log lỗi ồn + chậm). Nếu không rơi vào Windows
    thì trả danh sách phổ biến để yt-dlp tự dò.
    """
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    if not local:
        return ["chrome", "edge", "firefox", "brave"]

    candidates = [
        ("chrome", os.path.join(local, "Google", "Chrome", "User Data")),
        ("edge", os.path.join(local, "Microsoft", "Edge", "User Data")),
        ("brave", os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data")),
        ("chromium", os.path.join(local, "Chromium", "User Data")),
        ("firefox", os.path.join(roaming, "Mozilla", "Firefox")),
        ("opera", os.path.join(roaming, "Opera Software", "Opera Stable")),
    ]
    return [name for name, path in candidates if path and os.path.isdir(path)]


def _resolve_ffmpeg_exe() -> Optional[str]:
    """Trả về ĐƯỜNG DẪN FILE ffmpeg để yt-dlp merge/convert khi cần.

    Lưu ý: imageio-ffmpeg đặt tên binary dạng ``ffmpeg-win-x86_64-*.exe`` chứ không
    phải ``ffmpeg.exe``, nên yt-dlp không tự dò ra nếu chỉ đưa thư mục. Vì vậy phải
    truyền thẳng đường dẫn file thực thi.
    """
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[DOWNLOAD] Không resolve được ffmpeg từ imageio-ffmpeg: {e}")
        return None


def _convert_audio_to_mp3(src_path: str) -> str:
    """Convert file audio thô (webm/m4a/opus…) sang MP3 bằng ffmpeg của app (KHÔNG cần
    ffprobe). Trả về đường dẫn .mp3; xóa file nguồn. Lỗi -> ValueError."""
    import subprocess

    if src_path.lower().endswith(".mp3"):
        return src_path
    ffmpeg = _resolve_ffmpeg_exe()
    if not ffmpeg:
        raise ValueError(L("Không tìm thấy ffmpeg để chuyển sang MP3.", "ffmpeg not found for converting to MP3."))
    dst_path = os.path.splitext(src_path)[0] + ".mp3"
    cmd = [
        ffmpeg, "-y", "-i", src_path,
        "-vn", "-acodec", "libmp3lame", "-b:a", "192k",
        dst_path,
    ]
    proc = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="ignore",
    )
    if proc.returncode != 0 or not (os.path.exists(dst_path) and os.path.getsize(dst_path) > 0):
        stderr = _strip_ansi(proc.stderr or "")
        logger.error(
            f"[DOWNLOAD] ffmpeg convert MP3 lỗi. src={src_path!r} "
            f"size={os.path.getsize(src_path) if os.path.exists(src_path) else 'N/A'} "
            f"stderr={stderr[-800:]!r}"
        )
        # Dọn file .mp3 rỗng ffmpeg để lại (nếu có) — tránh lượt sau tưởng đã có file.
        try:
            if os.path.exists(dst_path) and os.path.getsize(dst_path) == 0:
                os.remove(dst_path)
        except OSError:
            pass
        if "does not contain any stream" in stderr:
            # Input không có luồng âm thanh (file video-only hoặc file dở dang/rác
            # trùng tên còn sót từ lượt tải trước) -> báo rõ để người dùng xử lý.
            raise ValueError(
                L("File tải về không có luồng âm thanh để chuyển sang MP3 "
                f"(file: {os.path.basename(src_path)}). Hãy xoá các file trùng tên "
                "trong thư mục đích rồi tải lại.", f"The downloaded file has no audio stream to convert to MP3 (file: {os.path.basename(src_path)}). Delete duplicate files in the destination folder and download again.")
            )
        raise ValueError(
            L("Chuyển âm thanh sang MP3 thất bại (ffmpeg). ", "Audio conversion to MP3 failed (ffmpeg). ") + stderr[-300:]
        )
    # Xóa file nguồn thô, chỉ giữ .mp3.
    if os.path.normpath(dst_path) != os.path.normpath(src_path):
        try:
            os.remove(src_path)
        except OSError:
            pass
    return dst_path


import re as _re

_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    """Bỏ mã màu ANSI (vd \\x1b[0;31m) mà yt-dlp chèn vào thông báo lỗi."""
    return _ANSI_RE.sub("", s)


def _import_yt_dlp():
    try:
        import yt_dlp

        return yt_dlp
    except ImportError as e:  # pragma: no cover
        raise ValueError(
            L("Thiếu thư viện yt-dlp trên backend. Vui lòng cài: pip install yt-dlp", "The yt-dlp library is missing on the backend. Please install it: pip install yt-dlp")
        ) from e


# Map chất lượng -> yt-dlp format selector. "best" = cao nhất; còn lại giới hạn
# chiều cao video để tải nhẹ/nhanh hơn.
_QUALITY_FORMATS = {
    "best": "bestvideo*+bestaudio/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
}


def _is_youtube(url: str) -> bool:
    """Link có thuộc YouTube không (kể cả youtu.be / m. / music. / nhúng)."""
    host = _re.sub(r"^https?://", "", (url or "").strip(), flags=_re.I)
    host = host.split("/")[0].split("?")[0].lower().lstrip(".")
    return host == "youtu.be" or host == "youtube.com" or host.endswith(
        (".youtube.com", ".youtu.be")
    )


def _yt_js_runtime_opts() -> dict:
    """Tuỳ chọn giúp yt-dlp GIẢI CHỮ KÝ của YouTube.

    Từ 2026 YouTube bắt buộc chạy một đoạn JS để ký URL luồng; yt-dlp cần
    (1) một JS runtime và (2) "challenge solver script" tải từ GitHub. Thiếu
    thì phần lớn format bị ẩn và link lấy được sẽ 403.

    Thứ tự ưu tiên:
      1. ``qjs.exe`` (QuickJS ~2 MB) ĐÓNG GÓI SẴN trong assets/jsruntime — đây là
         nguồn chính, đảm bảo bản cài của người dùng luôn có runtime. Electron 30
         chỉ mang Node 20 mà yt-dlp đòi >= 22 nên KHÔNG dùng được.
      2. Node >= 22 nếu máy dev có sẵn (tiện khi chạy từ source chưa fetch qjs).

    Không có runtime nào thì KHÔNG khai báo gì — yt-dlp chạy chế độ hạn chế thay
    vì lỗi cứng: YouTube rơi về vé vớt android (360p), nền tảng khác không ảnh hưởng.
    """
    opts: dict = {"remote_components": ["ejs:github"]}
    qjs = _bundled_qjs_path()
    if qjs:
        opts["js_runtimes"] = {"quickjs": {"path": qjs}}
        return opts
    exe = _find_node_ge22()
    if exe:
        opts["js_runtimes"] = {"node": {"path": exe}}
    return opts


def _bundled_qjs_path() -> Optional[str]:
    """Đường dẫn qjs.exe đóng gói kèm app (assets/jsruntime/qjs.exe), hoặc None.

    Xử lý cả khi chạy dev lẫn khi đóng gói PyInstaller (_MEIPASS) — giống cách
    _default_cookie_file_path() lấy cookie Douyin mặc định.
    Tải file này về bằng: ``py backend/tools/fetch_jsruntime.py``.
    """
    import sys

    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    p = os.path.join(base, "assets", "jsruntime", "qjs.exe")
    return p if os.path.isfile(p) else None


def _find_node_ge22() -> Optional[str]:
    """Tìm node.exe phiên bản >= 22 (yêu cầu tối thiểu của yt-dlp). Trả None nếu
    không có. Dò ``node`` trong PATH trước, rồi các thư mục nvm/Program Files —
    máy cài nvm thường để bản mới ở ngoài PATH (PATH đang trỏ bản cũ)."""
    import shutil
    import subprocess

    candidates = []
    which = shutil.which("node")
    if which:
        candidates.append(which)
    local = os.environ.get("LOCALAPPDATA", "")
    roots = [os.path.join(local, "nvm")] if local else []
    roots.append(os.path.join(os.environ.get("ProgramFiles", ""), "nodejs"))
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root), reverse=True):  # bản mới trước
            p = os.path.join(root, name, "node.exe")
            if os.path.isfile(p):
                candidates.append(p)
            elif name.lower() == "node.exe":
                candidates.append(os.path.join(root, name))

    for exe in candidates:
        try:
            out = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
            major = int(_re.sub(r"^v", "", out).split(".")[0])
            if major >= 22:
                return exe
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


# YouTube: CHỈ nhận format HLS (m3u8) — xem giải thích ở _base_opts.
# CỐ Ý KHÔNG có vé vớt sang luồng thẳng: video không có HLS sẽ báo "Requested
# format is not available" ngay lập tức, để download_video bắt lỗi đó và tải lại
# bằng _YT_ANDROID_ARGS — thay vì tải nửa chừng rồi mới 403.
_YT_HLS_FORMATS = {
    "best": "bestvideo[protocol^=m3u8]+bestaudio[protocol^=m3u8]/best[protocol^=m3u8]",
    "1080": "best[protocol^=m3u8][height<=1080]",
    "720": "best[protocol^=m3u8][height<=720]",
    "480": "best[protocol^=m3u8][height<=480]",
    "360": "best[protocol^=m3u8][height<=360]",
}

# Vé vớt khi video KHÔNG có HLS (nhiều video dài / video cũ chỉ có luồng thẳng):
# client "android" trả luồng tải TRỌN VẸN, đổi lại chỉ có format 18 (mp4 muxed
# 360p). Thà 360p tải xong còn hơn 1080p đứt giữa chừng vì 403.
_YT_ANDROID_ARGS = {"youtube": {"player_client": ["android"]}}


def _base_opts(quality: str = "best", url: str = "") -> dict:
    is_yt = _is_youtube(url)
    opts = {
        # Chọn theo chất lượng người dùng yêu cầu (mặc định cao nhất).
        "format": (_YT_HLS_FORMATS if is_yt else _QUALITY_FORMATS).get(
            quality, (_YT_HLS_FORMATS if is_yt else _QUALITY_FORMATS)["best"]
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,  # link Douyin thường là 1 video, tránh tải cả trang cá nhân
        "quiet": True,
        "no_warnings": True,
        #  10 lần + NGHỈ tăng dần giữa các lần (gắn ở download_video) — bản cũ 5 lần dồn
        #  dập trong một giây, mạng chập chờn vài giây là đã hết lượt.
        "retries": 10,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    # CHỈ ép header giả lập kiểu Douyin (Referer douyin.com + Accept-Language zh-CN)
    # cho ĐÚNG Douyin/TikTok. Áp cho mọi nền tảng sẽ khiến bilibili.tv, YouTube... trả
    # HTTP 403 Forbidden (server từ chối vì Referer/UA không khớp domain). Các nền tảng
    # khác để yt-dlp dùng header mặc định của nó.
    if any(k in url for k in ("douyin.com", "tiktok.com")):
        opts["http_headers"] = _BROWSER_HEADERS
    # YouTube (2026): luồng tải thẳng (progressive https) BỊ CHẶN giữa chừng —
    # YouTube chỉ cho tải theo dải BỊ CHẶN ĐẦU-CUỐI, nên yt-dlp tải được vài chục MB
    # rồi dính "HTTP Error 403: Forbidden" (đã kiểm chứng: chunk 1-2 xong, chunk 3
    # trả 403; CLI yt-dlp gốc cũng hỏng y hệt -> KHÔNG phải lỗi code của app).
    # Metadata vẫn lấy được bình thường, vì vậy UI hiện đủ tiêu đề/lượt xem/dung
    # lượng rồi mới báo lỗi lúc bấm Tải — đúng như ảnh người dùng gửi.
    # Luồng HLS (m3u8) chia sẵn thành fragment nhỏ, mỗi fragment là một dải có
    # đầu-cuối nên KHÔNG dính chặn -> tải trọn vẹn. Đây là cách sửa gốc.
    # Kèm theo cần JS runtime để giải chữ ký (xem _yt_js_runtime_opts).
    if is_yt:
        opts.update(_yt_js_runtime_opts())
    ffmpeg_exe = _resolve_ffmpeg_exe()
    if ffmpeg_exe:
        opts["ffmpeg_location"] = ffmpeg_exe
    return opts


# Nguồn cookie thành công gần nhất (thử đầu tiên ở lần sau) + các nguồn browser
# tạm hỏng — dùng chung cho mọi lượt quét/tải của process.
_last_good_cookie_source: list = [None]
#: {tên nguồn: thời điểm đánh dấu hỏng}. CHẾT TẠM, KHÔNG CHẾT HẲN.
#
#  Lý do: nguồn hỏng phổ biến nhất là "Chrome đang mở" — Windows khoá tệp Cookies nên
#  yt-dlp không sao chép được ("Could not copy Chrome cookie database"). Đó là trạng
#  thái NHẤT THỜI: người dùng đóng trình duyệt là đọc được ngay. Bản cũ loại nguồn đó
#  hẳn khỏi phiên, nên đóng Chrome rồi app vẫn không thử lại — phải tắt hẳn app (hoặc
#  như người dùng gặp thật: khởi động lại máy) mới lấy được cookie tươi.
_dead_cookie_sources: dict = {}
#: Bao lâu thì thử lại một nguồn đã hỏng (giây).
_DEAD_COOKIE_TTL = 180.0

# Dấu hiệu lỗi LIÊN QUAN COOKIE/QUYỀN TRUY CẬP — chỉ những lỗi này mới đáng thử
# nguồn cookie khác. Lỗi khác (đĩa đầy, ffmpeg merge, file bị khóa, mạng đứt hẳn...)
# thử lại với cookie khác chỉ tốn thời gian tải lại toàn bộ + che mất lỗi thật.
_AUTH_ERR_HINTS = (
    "403", "forbidden", "unauthorized", "fresh cookies", "cookie", "login",
    "sign in", "private", "not subscriptable", "'nonetype'", "empty",
    "không trả về dữ liệu", "returned no video data",
)

# Dấu hiệu lỗi "CỨNG" (môi trường/máy) — dừng ngay, không fallback cookie.
_FATAL_ERR_HINTS = (
    "no space left", "errno 28", "not enough space", "disk full",
    "postprocess", "ffmpeg", "unable to rename", "winerror",
    #  Douyin bắt XÁC MINH (captcha) thì đổi cookie không cứu được — chặn theo máy/IP,
    #  không theo phiên. Thiếu mục này thì mỗi lượt quét lại quay vòng đủ 5 nguồn
    #  cookie rồi mới bỏ cuộc, vừa lâu vừa báo sai bệnh là "cookie hết hạn".
    "captcha",
)


# Lỗi THÔ của lượt tải gần nhất (trước khi bị đổi thành câu tiếng Việt) — dùng
# để quyết định có thử lại bằng client khác không.
_last_download_error: list = [""]

# ====================== ĐỨT GIỮA CHỪNG -> LẤY LINK MỚI, TẢI TIẾP
#
# Khách báo (28-09-2026, Bilibili 2:48:12 ~1,3GB): "0 bytes read, 653814739 more
# expected. Giving up after 5 retries". Đo thật cùng video: link CDN Bilibili HẾT HẠN
# sau 120 phút, và tốc độ theo từng máy chủ CDN chênh hàng trăm lần (126 KB/s … 12 MB/s;
# mỗi lượt lấy link được phân máy chủ khác). yt-dlp chỉ thử lại trên CHÍNH link cũ, nên
# máy chủ chậm/đứt hay link hết hạn là tải hỏng — dù phần đã tải (.part) vẫn còn nguyên.
# Cách gỡ: chạy lại lượt tải — yt-dlp lấy link MỚI rồi tải TIẾP từ tệp .part.
# Test: tests/test_tai_dut_giua_chung.py

#: Trần thời gian nghỉ giữa hai lần thử lại của yt-dlp (1, 2, 4… giây).
_NGU_THU_LAI_TOI_DA = 20.0
#: Tối đa bấy nhiêu lượt "lấy link mới, tải tiếp" cho một lần tải.
_TAI_TIEP_TOI_DA = 8
#: Dừng khi chừng này lượt liền nhau không tải thêm được byte nào (hỏng hẳn).
_TAI_TIEP_KHONG_TIEN = 2

_MANG_DUT_HINTS = (
    "bytes read", "more expected", "incompleteread", "incomplete read",
    "timed out", "timeout", "connection reset", "connection aborted",
    "remote end closed", "winerror 10054", "winerror 10053", "winerror 10060",
    "giving up after",
)


def _la_loi_dut_mang(raw: str, da_tai: int) -> bool:
    """Lỗi có phải kiểu mạng/CDN đứt giữa chừng (đáng lấy link mới tải tiếp) không.
    403 chỉ tính khi ĐÃ tải được một phần — tức link hết hạn giữa chừng, không phải
    video bị chặn ngay từ đầu."""
    low = (raw or "").lower()
    if any(k in low for k in _MANG_DUT_HINTS):
        return True
    return da_tai > 0 and "http error 403" in low


def _no_hls_format_error(raw: str) -> bool:
    """Lỗi có phải kiểu 'không có format khớp selector' không — nghĩa là video
    không có luồng HLS (chứ không phải mạng hỏng / video riêng tư)."""
    low = (raw or "").lower()
    return "requested format is not available" in low or "no video formats found" in low


def _should_try_next_cookie(e: Exception) -> bool:
    """Chỉ thử nguồn cookie kế tiếp khi lỗi có dáng dấp cookie/quyền truy cập."""
    err_low = str(e).lower()
    if isinstance(e, OSError) or isinstance(e.__cause__, OSError):
        return False
    if any(k in err_low for k in _FATAL_ERR_HINTS):
        return False
    return any(k in err_low for k in _AUTH_ERR_HINTS)


def _run_with_cookie_fallback(url: str, base_opts: dict, action: str, runner):
    """Chạy ``runner(opts)`` với cơ chế thử cookie tự động cho Douyin/TikTok.

    Douyin/TikTok siết chặn bot: yt-dlp cần "cookies tươi". Thử lấy cookie tự động
    từ trình duyệt người dùng đã cài (không cần đăng nhập, chỉ cần từng mở trang).
    CHỈ thử những trình duyệt thực sự có profile trên máy (tránh dò vô ích + log ồn),
    cuối cùng fallback chạy không cookie. Raise ``ValueError`` (tiếng Việt) khi hết cách.
    """
    # Thứ tự thử: (1) cookie người dùng DÁN (chắc ăn nhất) -> (2) cookie MẶC ĐỊNH
    # bundle sẵn (để người dùng mới chưa cấu hình vẫn tải được) -> (3) cookie tự đọc
    # từ trình duyệt đã cài -> (4) không cookie.
    attempts: list = []
    if has_cookies():
        attempts.append(("cookie đã dán", "file", _cookie_file_path()))
    is_douyin_tiktok = any(k in url for k in ("douyin.com", "tiktok.com"))
    if is_douyin_tiktok:
        default_cookie = _default_cookie_file_path()
        if os.path.exists(default_cookie):
            attempts.append(("cookie mặc định", "file", default_cookie))
        for b in _installed_browsers():
            attempts.append((b, "browser", b))
    attempts.append(("không cookie", None, None))

    # Tối ưu cho quét/tải HÀNG LOẠT (hàng trăm link): bỏ nguồn cookie đã hỏng
    # hẳn trong phiên (Chrome DB khóa / DPAPI lỗi... — thử lại ở mọi link chỉ tốn
    # 1-2s/nguồn + spam log), và ưu tiên thử NGUỒN THÀNH CÔNG GẦN NHẤT trước.
    import time as _time

    _now = _time.monotonic()
    # Hết hạn "chết tạm" thì cho nguồn đó thử lại (người dùng có thể vừa đóng Chrome).
    for _name, _at in list(_dead_cookie_sources.items()):
        if _now - _at >= _DEAD_COOKIE_TTL:
            _dead_cookie_sources.pop(_name, None)
    attempts = [a for a in attempts if a[0] not in _dead_cookie_sources]
    if _last_good_cookie_source[0]:
        attempts.sort(
            key=lambda a: 0 if a[0] == _last_good_cookie_source[0] else 1
        )

    last_err = None
    #  Có nguồn cookie TRÌNH DUYỆT nào đọc không được không (thường vì trình duyệt đang
    #  mở)? Cần biết để nói đúng cách gỡ ở thông báo cuối.
    browser_locked = False
    for label, kind, value in attempts:
        opts = dict(base_opts)
        if kind == "file":
            opts["cookiefile"] = value
        elif kind == "browser":
            opts["cookiesfrombrowser"] = (value,)
        try:
            result = runner(opts)
            _last_good_cookie_source[0] = label
            return result
        except DownloadCancelled:
            raise  # hủy -> thoát ngay, không thử fallback
        except Exception as e:  # noqa: BLE001
            # yt-dlp có thể bọc DownloadCancelled trong lỗi của nó.
            if isinstance(e.__cause__, DownloadCancelled) or "DownloadCancelled" in repr(e):
                raise DownloadCancelled() from e
            err_low = str(e).lower()
            # Nguồn browser hỏng VĨNH VIỄN trong phiên (không phải lỗi theo link).
            broken_browser = kind == "browser" and (
                "could not copy" in err_low
                or "dpapi" in err_low
                or "cookie database" in err_low
            )
            # Lỗi "đọc cookie trình duyệt hỏng" là chuyện của MÁY, không phải lý do
            # link tải không được. Nếu lấy nó làm lỗi cuối thì người dùng nhận câu
            # kỹ thuật vô nghĩa ("Failed to decrypt with DPAPI") và MẤT hướng dẫn
            # thật sự khả thi từ nền tảng (vd Douyin: "cần dán cookie"). Vì vậy chỉ
            # ghi đè last_err khi CHƯA có lỗi nào có ý nghĩa hơn.
            if not (broken_browser and last_err is not None):
                last_err = e
            if broken_browser:
                _dead_cookie_sources[label] = _now
                browser_locked = True
                logger.info(
                    f"[DOWNLOAD] Tạm bỏ nguồn cookie '{label}' trong "
                    f"{int(_DEAD_COOKIE_TTL)}s (không đọc được — thường do trình duyệt đang mở)."
                )
            logger.warning(f"[DOWNLOAD] {action} ({label}) thất bại: {str(e)[:200]}")
            # Lỗi KHÔNG liên quan cookie (đĩa đầy, ffmpeg, file khóa...) -> dừng
            # ngay để báo đúng nguyên nhân, không tải lại toàn bộ với cookie khác.
            if not _should_try_next_cookie(e):
                break

    raw = _strip_ansi(str(last_err))
    _last_download_error[0] = raw  # giữ lỗi thô cho lớp gọi quyết định thử cách khác
    #  Ghi RÕ đã thử những nguồn cookie nào: log cũ chỉ có câu lỗi cuối nên nhìn vào
    #  không biết cookie người dùng dán có được dùng không, hay nguồn trình duyệt hỏng.
    logger.error(
        f"[DOWNLOAD] Lỗi {action} '{url}': {raw} "
        f"| đã thử cookie: {', '.join(a[0] for a in attempts) or 'không có'}"
        + (" | trình duyệt đang mở nên không đọc được cookie tươi" if browser_locked else "")
    )
    low = raw.lower()
    # Thông báo tiếng Việt gọn gàng theo từng loại lỗi phổ biến (ẩn chi tiết kỹ thuật).
    if "no space left" in low or "errno 28" in low or "not enough space" in low or "disk full" in low:
        friendly = L("Ổ đĩa không đủ dung lượng trống. Hãy dọn bớt rồi tải lại.", "Not enough free disk space. Free up some space and download again.")
    elif "cookies" in low or "fresh cookies" in low:
        #  Douyin nay CHỈ tải được qua trang chia sẻ, và trang đó chỉ trả dữ liệu khi
        #  request có cookie (đo 2026-09-16: có cookie 4/4 lần được, không cookie 0/4).
        #  Nên cách gỡ đúng là DÁN LẠI cookie — không phải "đóng trình duyệt" như bản
        #  trước nói: đường lấy cookie từ trình duyệt chỉ là nguồn dự phòng, và dù mở
        #  được nó cũng không cứu được extractor Douyin của yt-dlp (đã chết hẳn).
        friendly = (
            L("Douyin yêu cầu cookie mới. Bấm \"Dán cookie\" rồi dán lại nội dung "
            "cookies.txt mới lấy từ trình duyệt (không cần đăng nhập Douyin) và "
            "Thử lại.", 'Douyin requires a fresh cookie. Click "Paste cookie" and paste the new cookies.txt content taken from your browser (no Douyin login needed), then Retry.')
        )
        if browser_locked:
            friendly += (
                L(" Nếu muốn app tự lấy cookie, hãy đóng hẳn Chrome/Edge (kể cả cửa "
                "sổ ẩn dưới khay hệ thống) trước khi Thử lại.", " If you want the app to fetch the cookie itself, fully close Chrome/Edge (including the window hidden in the system tray) before retrying.")
            )
    elif "unable to download webpage" in low or "http error 4" in low or "http error 5" in low:
        friendly = L("Không truy cập được liên kết. Kiểm tra mạng hoặc link còn sống không, rồi Thử lại.", "Cannot reach the link. Check your network or whether the link is still alive, then Retry.")
    elif "private" in low or "login" in low:
        friendly = L("Video ở chế độ riêng tư / yêu cầu đăng nhập nên không tải được.", "The video is private / requires login, so it cannot be downloaded.")
    elif "unsupported url" in low or "no video" in low:
        friendly = L("Liên kết này không chứa video hỗ trợ tải.", "This link does not contain a downloadable video.")
    elif "unable to rename" in low or "winerror 32" in low or "being used by another process" in low:
        friendly = (
            L("File tải về đang bị ứng dụng khác giữ (trình phát nhạc/video đang mở file, "
            "hoặc lượt tải trước chưa kết thúc hẳn). Đóng ứng dụng đang mở file / đợi vài giây rồi Thử lại.", "The downloaded file is being held by another application (a music/video player has the file open, or the previous download has not fully finished). Close the app holding the file / wait a few seconds, then Retry.")
        )
    elif "not subscriptable" in low or "'nonetype'" in low:
        # TypeError thô từ TRONG extractor của yt-dlp: nền tảng trả dữ liệu rỗng
        # (Douyin/TikTok chặn bot / cookie hết hạn; hoặc video bị xóa / chặn khu vực).
        if any(k in url for k in ("douyin.com", "tiktok.com")):
            friendly = (
                L("Douyin/TikTok từ chối trả dữ liệu (cookie hết hạn hoặc bị chặn bot). "
                "Bấm \"Quản lý cookie Douyin\" để dán cookie mới rồi Thử lại; "
                "nếu vẫn lỗi hãy thử lại sau ít phút.", 'Douyin/TikTok refused to return data (cookie expired or bot blocked). Click "Manage Douyin cookie" to paste a new cookie, then Retry; if it still fails, try again in a few minutes.')
            )
        elif "bilibili.tv" in url:
            # BiliIntl: tập phim chỉ dành cho tài khoản đăng nhập/khu vực → extractor
            # trả dữ liệu rỗng gây TypeError thô (log 2.2.0 gặp hàng loạt).
            friendly = (
                L("Tập phim này trên Bilibili.TV yêu cầu đăng nhập tài khoản hoặc bị "
                "giới hạn khu vực nên không tải được. Hãy dán cookie tài khoản "
                "Bilibili (cookies.txt) rồi Thử lại, hoặc bỏ qua tập này.", "This Bilibili.TV episode requires an account login or is region-restricted, so it cannot be downloaded. Paste your Bilibili account cookie (cookies.txt) then Retry, or skip this episode.")
            )
        else:
            friendly = (
                L("Nền tảng trả về dữ liệu rỗng — video có thể đã bị xóa, để riêng tư "
                "hoặc chặn khu vực. Kiểm tra link mở được trên trình duyệt rồi Thử lại.", "The platform returned empty data — the video may have been deleted, set to private or region-blocked. Check that the link opens in a browser, then Retry.")
            )
    else:
        friendly = L(f"Không đọc được video từ liên kết. Chi tiết: {raw[:160]}", f"Could not read the video from the link. Details: {raw[:160]}")
    raise ValueError(friendly) from last_err


# ====================== DOUYIN: đi qua TRANG CHIA SẺ, không qua extractor yt-dlp
#
# VÌ SAO PHẢI CÓ ĐƯỜNG RIÊNG (đo ngày 2026-09-16, link thật của khách):
# extractor Douyin của yt-dlp LUÔN hỏng với "Fresh cookies (not necessarily logged
# in) are needed" — API ``/aweme/v1/web/aweme/detail/`` nay đòi chữ ký ``a_bogus``
# mà yt-dlp chưa sinh được (chính họ để TODO ngay chỗ raise lỗi đó). Đã loại trừ
# từng khả năng: yt-dlp 2026.07.04 lẫn 2026.08.19 (mới nhất) đều hỏng; cookie
# người dùng dán, cookie TƯƠI lấy thẳng từ douyin.com (ttwid + __ac_nonce +
# msToken), và không cookie — cả bốn đều hỏng. Tức KHÔNG phải "cookie cũ", mà là
# hết đường qua yt-dlp; nâng cấp yt-dlp cũng không cứu được.
#
# Đường còn sống: trang CHIA SẺ ``iesdouyin.com/share/video/<id>/`` nhúng sẵn dữ
# liệu trong ``window._ROUTER_DATA`` (không cần chữ ký). Đo 4 lượt mỗi kiểu:
#   · UA DI ĐỘNG + có cookie (đã dán / mặc định bundle) -> 4/4 lấy được,
#   · không cookie -> 0/4,  · UA máy tính -> trang không kèm dữ liệu.
# Nên vẫn đi qua đúng chuỗi nguồn cookie cũ (_run_with_cookie_fallback).
_DOUYIN_SHARE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_DOUYIN_ID_RE = _re.compile(r"/(?:video|note|share/video|share/note|slides)/(\d+)")


def _is_douyin(url: str) -> bool:
    return "douyin.com" in (url or "")


def _requests_session_from_opts(opts: dict):
    """Session ``requests`` mang ĐÚNG nguồn cookie mà bộ thử cookie đang xét.

    Nhờ vậy trang chia sẻ dùng chung một chính sách cookie với yt-dlp: thứ tự thử,
    nhớ nguồn tốt gần nhất, tạm loại nguồn hỏng, câu lỗi tiếng Việt — không phải
    viết lại lần hai.
    """
    import http.cookiejar

    import requests

    s = requests.Session()
    s.headers.update({
        "User-Agent": _DOUYIN_SHARE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.douyin.com/",
    })
    path = opts.get("cookiefile")
    if path and os.path.exists(path):
        jar = http.cookiejar.MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        s.cookies = requests.cookies.merge_cookies(s.cookies, jar)
    browser = (opts.get("cookiesfrombrowser") or (None,))[0]
    if browser:
        # Lỗi ở đây mang đúng câu "Could not copy ... cookie database" của yt-dlp,
        # nên _run_with_cookie_fallback nhận ra là nguồn trình duyệt bị khoá.
        from yt_dlp.cookies import extract_cookies_from_browser

        s.cookies = requests.cookies.merge_cookies(
            s.cookies, extract_cookies_from_browser(browser)
        )
    return s


def _douyin_aweme_id(url: str, session) -> str:
    """Mã video. Link rút gọn (v.douyin.com/XXXX) phải đi theo chuyển hướng."""
    m = _DOUYIN_ID_RE.search(url or "")
    if m:
        return m.group(1)
    r = session.get(url, timeout=25, allow_redirects=True)
    m = _DOUYIN_ID_RE.search(r.url or "") or _DOUYIN_ID_RE.search(r.text or "")
    if not m:
        raise ValueError(L("Không đọc được mã video Douyin từ liên kết này.", "Could not read the Douyin video ID from this link."))
    return m.group(1)


def _douyin_share_item(url: str, session) -> dict:
    """Dữ liệu video nhúng trong trang chia sẻ."""
    import json

    vid = _douyin_aweme_id(url, session)
    r = session.get(f"https://www.iesdouyin.com/share/video/{vid}/", timeout=25)
    m = _re.search(
        r"window\._ROUTER_DATA\s*=\s*(\{.*?\});?\s*</script>", r.text or "", _re.S
    )
    #  TRANG XÁC MINH (captcha) — phải tách khỏi lỗi cookie. Douyin gắn cờ máy/IP rồi
    #  trả về trang "验证码中间页" (nạp captcha/index.js) thay cho dữ liệu, với HTTP 200.
    #  Đo được 18/09: cả 5 nguồn cookie đều hỏng, kể cả cookie người dùng vừa lấy mới —
    #  vì đây không phải chuyện cookie. Báo "cookie hết hạn" ở ca này là đẩy người dùng
    #  đi lấy cookie mới mãi mà không bao giờ xong.
    tho = r.text or ""
    if "_ROUTER_DATA" not in tho and ("captcha/index.js" in tho or "验证码" in tho):
        raise ValueError(
            L("Douyin đang bắt XÁC MINH (captcha) với máy này — không phải lỗi cookie. "
            "Mở douyin.com trên trình duyệt, hoàn tất phần xác minh rồi thử lại; "
            "hoặc nghỉ ít phút cho Douyin bỏ cờ (tải dồn dập dễ bị gắn cờ).", "Douyin is demanding VERIFICATION (captcha) on this machine — this is not a cookie error. Open douyin.com in your browser, complete the verification and try again; or wait a few minutes for Douyin to lift the flag (rapid downloads are easily flagged).")
        )
    #  Chữ "cookie" trong câu lỗi là CỐ Ý: _should_try_next_cookie dựa vào đó để
    #  chuyển sang nguồn cookie kế tiếp thay vì bỏ cuộc ngay.
    #
    #  KHÔNG khẳng định chắc nịch là "cookie hết hạn": khi Douyin chặn máy, nó trả về
    #  KHÔNG NHẤT QUÁN — lúc là trang captcha (bắt được ở khối trên), lúc là một trang
    #  rỗng ~2,5KB không dấu hiệu gì. Rơi vào ca thứ hai mà đổ cho cookie là người dùng
    #  đi lấy cookie mới hết lần này tới lần khác trong khi bệnh nằm chỗ khác.
    thieu = ValueError(
        L("Douyin không trả về dữ liệu video — cookie hết hạn, HOẶC Douyin đang chặn máy "
        "này (bắt xác minh). Thử lấy cookie mới; vẫn vậy thì mở douyin.com trên trình "
        "duyệt hoàn tất xác minh, hoặc nghỉ ít phút rồi thử lại.", "Douyin returned no video data — the cookie expired, OR Douyin is blocking this machine (demanding verification). Try getting a new cookie; if that does not help, open douyin.com in your browser and complete the verification, or wait a few minutes and retry.")
    )
    if not m:
        raise thieu
    try:
        data = json.loads(m.group(1))
    except ValueError:
        raise thieu from None
    for v in (data.get("loaderData") or {}).values():
        items = ((v or {}).get("videoInfoRes") or {}).get("item_list") or []
        if items:
            return items[0]
    raise thieu


def _douyin_play_url(item: dict, watermark: bool = False) -> str:
    """Link phát. ``playwm`` là bản CÓ logo; đổi sang ``play`` ra bản không logo
    (đã kiểm: cùng video, 23.7 MB không logo so với 29.5 MB có logo)."""
    urls = ((item.get("video") or {}).get("play_addr") or {}).get("url_list") or []
    if not urls:
        raise ValueError(L("Douyin không trả về đường dẫn phát video.", "Douyin did not return a playback URL for the video."))
    u = urls[0]
    return u if watermark else u.replace("/playwm/", "/play/")


def _douyin_normalize(item: dict, url: str) -> dict:
    v = item.get("video") or {}
    st = item.get("statistics") or {}
    return {
        "id": str(item.get("aweme_id") or ""),
        "title": (item.get("desc") or "").strip() or "Video Douyin",
        # Douyin trả thời lượng bằng MILI giây.
        "duration": round(float(v.get("duration") or 0) / 1000.0, 3),
        "thumbnail": ((v.get("cover") or {}).get("url_list") or [""])[0],
        "uploader": ((item.get("author") or {}).get("nickname") or ""),
        "width": v.get("width") or 0,
        "height": v.get("height") or 0,
        "view_count": st.get("play_count"),
        "like_count": st.get("digg_count"),
        "comment_count": st.get("comment_count"),
        "play_url": _douyin_play_url(item),
        "webpage_url": url,
    }


def _douyin_filesize(play_url: str, session) -> int:
    """Dung lượng thật của luồng (byte), 0 nếu máy chủ không nói."""
    try:
        r = session.get(play_url, timeout=25, stream=True, allow_redirects=True)
        size = int(r.headers.get("Content-Length") or 0)
        r.close()
        return size
    except Exception:  # noqa: BLE001 — chỉ là ước lượng cho UI
        return 0


def douyin_fetch_info(url: str, with_size: bool = False) -> dict:
    """Thông tin video Douyin qua trang chia sẻ, dùng lại bộ thử cookie chung."""

    def _runner(opts: dict):
        s = _requests_session_from_opts(opts)
        info = _douyin_normalize(_douyin_share_item(url, s), url)
        info["filesize"] = _douyin_filesize(info["play_url"], s) if with_size else 0
        return info

    return _run_with_cookie_fallback(url, {}, "quét", _runner)


def _douyin_sizes(filesize: int) -> dict:
    """Douyin chỉ phát MỘT luồng nên mọi mức chất lượng cùng một dung lượng —
    nói thật thay vì bịa ra các con số khác nhau cho từng mức."""
    mb = round(filesize / (1024 * 1024), 1) if filesize else None
    out = {k: mb for k in ("best", "1080", "720", "480", "360")}
    #  Tải tiếng: bóc MP3 192 kbps từ chính luồng đó.
    out["audio"] = None
    return out


def _fmt_size(f: dict, duration: float = 0) -> int:
    """Dung lượng (byte) của 1 format. Luồng HLS của YouTube KHÔNG khai filesize,
    chỉ có ``tbr`` (kbit/s) -> quy ra byte theo thời lượng, không thì UI hiện
    trống dung lượng cho mọi video YouTube."""
    size = int(f.get("filesize") or f.get("filesize_approx") or 0)
    if size:
        return size
    tbr = f.get("tbr")
    if tbr and duration:
        return int(float(tbr) * 1000 / 8 * float(duration))
    return 0


def _estimate_sizes(info: dict, ydl, url: str = "") -> dict:
    """Dung lượng (MB) cho từng mức chất lượng — dùng CHÍNH bộ chọn format của
    yt-dlp (``build_format_selector``) với đúng format string lúc tải, nên số khớp
    với dung lượng tải thật (không phải ước lượng thô).

    Trả về ``{"best", "1080", "720", "480", "360"}`` (MB, hoặc None nếu không xác định).
    """
    formats = info.get("formats") or []
    duration = info.get("duration") or 0
    if not formats:
        # Không có danh sách formats (vd 1 vài nền tảng) -> lấy size tổng của info.
        s = _fmt_size(info, duration)
        mb = round(s / (1024 * 1024), 1) if s else None
        return {k: mb for k in ("best", "1080", "720", "480", "360")}

    ctx = {"formats": formats, "incomplete_formats": False}
    out = {}
    # YouTube tải bằng luồng HLS (xem _base_opts) nên phải ước lượng trên ĐÚNG
    # nhánh HLS — dùng _QUALITY_FORMATS sẽ ra size của luồng thẳng (khác xa thực
    # tế, vd 232 MB so với 80 MB tải thật) khiến UI báo sai.
    is_yt = _is_youtube(url)
    has_hls = any("m3u8" in str(f.get("protocol")) for f in formats)
    # YouTube không có HLS -> lúc tải sẽ rơi về vé vớt android (chỉ format 18,
    # mp4 muxed 360p). Ước lượng theo ĐÚNG nhánh đó, không thì UI hiện size của
    # luồng 1080p mà thực tế tải về là 360p.
    selectors = dict(
        (_YT_HLS_FORMATS if has_hls else {k: "best" for k in _YT_HLS_FORMATS})
        if is_yt else _QUALITY_FORMATS
    )
    # Size khi chỉ tải âm thanh — MIRROR selector audio ở download_video.
    selectors["audio"] = (
        ("bestaudio[protocol^=m3u8]/worstvideo[protocol^=m3u8]/best[protocol^=m3u8]"
         if has_hls else "bestaudio/best")
        if is_yt else
        "bestaudio/bestaudio*[vcodec^=h264]/bestaudio*[vcodec^=avc]"
        "/bestaudio*/best"
    )
    for key, fmt in selectors.items():
        try:
            selected = list(ydl.build_format_selector(fmt)(ctx))
            if not selected:
                out[key] = None
                continue
            sel = selected[0]
            reqs = sel.get("requested_formats") or [sel]
            total = sum(_fmt_size(f, duration) for f in reqs)
            out[key] = round(total / (1024 * 1024), 1) if total else None
        except Exception:  # noqa: BLE001 — bộ chọn có thể đổi giữa các bản yt-dlp
            out[key] = None
    # YouTube: nguồn audio thực tế là luồng CÓ HÌNH (HLS/format 18 đều muxed) nên
    # số ở trên là size video, trong khi file giao cho người dùng là MP3 bóc ra ở
    # 192 kbps. Quy đổi theo thời lượng cho khớp file thật (~70 MB cho 1 giờ).
    if is_yt and duration:
        out["audio"] = round(192 * 1000 / 8 * duration / (1024 * 1024), 1)
    return out


# Số video tối đa liệt kê từ 1 link danh sách — chặn playlist "vô hạn"
# (YouTube Mix, kênh nghìn video) làm treo quét / tràn UI.
PLAYLIST_PROBE_LIMIT = 100


def normalize_public_url(url: str) -> str:
    """Tự thêm https:// cho link THIẾU SCHEME (vd người dùng dán
    "douyin.com/video/123" hoặc "www.youtube.com/...") thay vì từ chối."""
    u = (url or "").strip()
    if (
        u
        and not u.startswith(("http://", "https://"))
        and _re.match(r"^[\w.-]+\.[A-Za-z]{2,}([/?#]|$)", u)
    ):
        return "https://" + u
    return u


def probe_video_meta(url: str) -> dict:
    """Quét metadata 1 link video (tiêu đề/thời lượng/thumbnail/kênh/lượt xem +
    sizes ước lượng theo chất lượng) — dùng điền thông tin cho danh sách LINK
    DÁN TAY. Trả dict kèm "url" gốc để client khớp lại từng dòng."""
    #  DOUYIN đi đường riêng (trang chia sẻ) — extractor của yt-dlp đã chết hẳn,
    #  xem khối "DOUYIN" ở trên.
    if _is_douyin(url):
        info = douyin_fetch_info(url, with_size=True)
        return {
            "url": url,
            "title": info["title"],
            "duration": info["duration"],
            "thumbnail": info["thumbnail"],
            "uploader": info["uploader"],
            "sizes": _douyin_sizes(info.get("filesize") or 0),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
        }

    yt_dlp = _import_yt_dlp()
    base = _base_opts(url=url)
    # QUÉT chỉ đọc metadata, không tải byte nào -> KHÔNG ràng buộc format. Giữ
    # selector HLS-only (dành cho lúc tải) sẽ làm quét CHẾT với video YouTube
    # không có HLS: yt-dlp báo "Requested format is not available" ngay ở bước
    # lấy thông tin, dù video đó vẫn tải được bằng vé vớt android.
    base.pop("format", None)

    def _runner(opts: dict):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError(L("Nền tảng không trả về dữ liệu video.", "The platform returned no video data."))
            if info.get("_type") == "playlist" and info.get("entries"):
                picked = [e for e in info["entries"] if e]
                if picked:
                    info = picked[0]
            # formats đã nằm sẵn trong info -> tính sizes không tốn thêm mạng.
            sizes = _estimate_sizes(info, ydl, url)
        return info, sizes

    info, sizes = _run_with_cookie_fallback(url, base, "quét", _runner)
    return {
        "url": url,
        "title": info.get("title") or "",
        "duration": _extract_duration(info),
        "thumbnail": info.get("thumbnail") or _entry_thumbnail(info),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "sizes": sizes,
        **_video_stats(info),
    }


def _entry_thumbnail(e: dict) -> str:
    """Lấy URL thumbnail từ entry phẳng (flat entry có thể để trong 'thumbnails')."""
    if e.get("thumbnail"):
        return e["thumbnail"]
    thumbs = e.get("thumbnails") or []
    return (thumbs[-1].get("url") or "") if thumbs else ""


def _stat(info: dict, key: str):
    """Số liệu tương tác (view/like/comment/repost) — trả None nếu nền tảng
    không cung cấp, để UI ẩn thay vì hiện 0 sai lệch."""
    v = info.get(key)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _video_stats(info: dict) -> dict:
    """Gom các chỉ số yt-dlp trả về để hiển thị (view/like/comment/repost)."""
    return {
        "view_count": _stat(info, "view_count"),
        "like_count": _stat(info, "like_count"),
        "comment_count": _stat(info, "comment_count"),
        "repost_count": _stat(info, "repost_count"),
    }


def probe_video(url: str) -> dict:
    """Quét thông tin từ URL mà KHÔNG tải file (extract_info download=False).

    Link VIDEO ĐƠN -> ``{"kind": "video", "title", "duration", "thumbnail",
    "uploader", "extractor", "sizes"}`` (``sizes`` = ước tính MB theo chất lượng).

    Link DANH SÁCH (playlist YouTube/BiliBili, trang user TikTok, video nhiều
    phần...) -> ``{"kind": "playlist", "title", "uploader", "extractor",
    "count", "truncated", "entries": [{id, url, title, duration, thumbnail}]}``.
    Quét PHẲNG (extract_flat) nên nhanh — không extract từng video con.
    Lưu ý: giữ noplaylist=True nên link "watch?v=X&list=Y" vẫn ra VIDEO X
    (đúng ý người dùng dán link video); chỉ link danh sách thuần mới ra list.
    """
    # yt-dlp KHÔNG có extractor trang cá nhân Douyin (chỉ douyin.com/video/<id>).
    # Chặn sớm với thông báo rõ thay vì để rơi vào generic extractor báo lỗi khó hiểu.
    if _re.search(r"douyin\.com/user/", url):
        raise ValueError(
            L("Douyin hiện chỉ hỗ trợ tải TỪNG video (chưa hỗ trợ cả trang cá nhân). "
            "Hãy mở video cụ thể rồi sao chép liên kết video đó.", "Douyin currently only supports downloading ONE video at a time (profile pages are not supported yet). Open the specific video and copy its link.")
        )

    if _is_douyin(url):
        info = douyin_fetch_info(url, with_size=True)
        return {
            "kind": "video",
            "title": info["title"],
            "duration": info["duration"],
            "thumbnail": info["thumbnail"],
            "uploader": info["uploader"],
            "extractor": "douyin",
            "sizes": _douyin_sizes(info.get("filesize") or 0),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
        }

    yt_dlp = _import_yt_dlp()
    base = _base_opts(url=url)
    # QUÉT chỉ đọc metadata, không tải byte nào -> KHÔNG ràng buộc format. Giữ
    # selector HLS-only (dành cho lúc tải) sẽ làm quét CHẾT với video YouTube
    # không có HLS: yt-dlp báo "Requested format is not available" ngay ở bước
    # lấy thông tin, dù video đó vẫn tải được bằng vé vớt android.
    base.pop("format", None)
    base["extract_flat"] = "in_playlist"
    base["playlistend"] = PLAYLIST_PROBE_LIMIT

    def _runner(opts: dict):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # Một số extractor trả None thay vì raise khi nền tảng chặn/trả rỗng.
            if not info:
                raise ValueError(L("Nền tảng không trả về dữ liệu video.", "The platform returned no video data."))
            if info.get("_type") == "playlist":
                return info, None
            # Tính size trong ngữ cảnh ydl để dùng build_format_selector chính xác.
            sizes = _estimate_sizes(info, ydl, url)
        return info, sizes

    info, sizes = _run_with_cookie_fallback(url, base, "quét", _runner)

    if info.get("_type") == "playlist":
        # Feed "recommended" (vd "youtube.com/watch?234234" thiếu v= -> tab trang
        # chủ) KHÔNG phải danh sách người dùng chủ đích -> coi như link sai.
        if (info.get("id") or "").lower() in ("recommended", ":ytrec", ":ytsubs"):
            raise ValueError(
                L("Liên kết không trỏ tới một video hay danh sách cụ thể. "
                "Kiểm tra lại link rồi Thử lại.", "The link does not point to a specific video or list. Check the link and Retry.")
            )
        entries = []
        # Dùng NGUYÊN dữ liệu quét phẳng của yt-dlp — KHÔNG quét chi tiết từng
        # video (video ẩn/cần quyền để yt-dlp tự xử lý lúc tải). Nền tảng nào
        # flat scan thiếu tên (vd BiliBili) thì hiện "Video N".
        for e in info.get("entries") or []:
            # Video đã xóa/ẩn trong danh sách có thể là entry rỗng (None).
            if not e:
                continue
            e_url = e.get("url") or e.get("webpage_url") or ""
            if not e_url.startswith("http"):
                continue
            entries.append({
                "id": str(e.get("id") or len(entries)),
                "url": e_url,
                "title": e.get("title") or f"Video {len(entries) + 1}",
                "duration": _extract_duration(e),
                "thumbnail": _entry_thumbnail(e),
                "uploader": e.get("uploader") or e.get("channel") or "",
                **_video_stats(e),
            })
        if not entries:
            raise ValueError(L("Danh sách này không có video nào tải được.", "This list has no downloadable videos."))
        # Danh sách chỉ 1 video -> quét như video đơn cho đủ size/preview.
        if len(entries) == 1:
            return probe_video(entries[0]["url"])
        total = int(info.get("playlist_count") or 0) or len(entries)
        return {
            "kind": "playlist",
            "title": info.get("title") or "Danh sách video",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "extractor": info.get("extractor_key") or info.get("extractor") or "",
            "count": total,
            "truncated": total > len(entries),
            "entries": entries,
        }

    # Link không trỏ tới 1 VIDEO cụ thể (vd link kênh/danh sách rỗng): extract vẫn
    # "thành công" nhưng không có luồng media nào -> phải báo lỗi rõ ràng thay vì
    # hiện panel xem trước rỗng (không thời lượng, không dung lượng).
    has_media = bool(info.get("formats")) or _fmt_size(info) > 0 or _extract_duration(info) > 0
    if not has_media:
        raise ValueError(
            L("Liên kết không trỏ tới một video cụ thể (có thể là trang chủ, kênh "
            "hoặc danh sách phát). Kiểm tra lại link rồi Thử lại.", "The link does not point to a specific video (it may be a home page, channel or playlist). Check the link and Retry.")
        )

    return {
        "kind": "video",
        "title": info.get("title") or "Video",
        "duration": _extract_duration(info),
        "thumbnail": info.get("thumbnail") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "sizes": sizes,
        **_video_stats(info),
    }


def _extract_duration(info: dict) -> float:
    """Lấy thời lượng (giây) từ metadata yt-dlp, chịu được nền tảng thiếu field gốc.

    Một số extractor (vd bilibili.tv / BiliIntl) KHÔNG set 'duration' ở cấp info,
    nhưng thời lượng vẫn nằm trong từng 'format' (field 'duration'). Thử lần lượt:
    info.duration -> duration của bất kỳ format nào -> 0.
    """
    def _num(v) -> float:
        try:
            v = float(v)
            return v if v > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    d = _num(info.get("duration"))
    if d:
        return d
    for f in info.get("formats") or []:
        d = _num(f.get("duration"))
        if d:
            return d
    return 0.0


class DownloadCancelled(Exception):
    """Người dùng hủy tải giữa chừng."""


# Mốc thời gian phụ đề: SRT dùng "00:01:02,500", VTT dùng "00:01:02.500" (dấu . hoặc ,),
# giờ có thể vắng ("01:02.500"). Bắt cả hai để parse chung 1 hàm.
_SUB_TS_RE = _re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)


def _ts_to_sec(h, m, s, ms) -> float:
    return (int(h or 0) * 3600 + int(m) * 60 + int(s)
            + int((ms or "0").ljust(3, "0")[:3]) / 1000.0)


def _clean_sub_text(raw: str) -> str:
    """Bỏ thẻ định dạng VTT (<c>, <00:00:01.000>...), gộp nhiều dòng thành 1 câu."""
    txt = _re.sub(r"<[^>]+>", "", raw)          # thẻ inline vtt
    txt = _re.sub(r"\{\\[^}]*\}", "", txt)      # thẻ ASS/SSA nếu lọt vào
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    return " ".join(lines).strip()


def _ass_ts_to_sec(ts: str) -> float:
    """Mốc thời gian ASS/SSA dạng 'H:MM:SS.cc' (centisecond) -> giây."""
    m = _re.match(r"(\d+):(\d{2}):(\d{2})[.:](\d{1,3})", ts.strip())
    if not m:
        return 0.0
    h, mi, s, cs = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(cs.ljust(2, "0")[:3]) / (
        100.0 if len(cs) <= 2 else 1000.0
    )


def _parse_ass(content: str) -> list:
    """Parse phụ đề ASS/SSA (bilibili.tv hay trả loại này) -> list {start,end,text}.
    Đọc dòng 'Dialogue:' theo thứ tự cột khai báo ở 'Format:' trong [Events]."""
    segments = []
    prev_text = None
    fmt = None  # danh sách tên cột của dòng Dialogue
    for line in content.splitlines():
        low = line.strip().lower()
        if low.startswith("format:") and fmt is None and "start" in low:
            fmt = [c.strip().lower() for c in line.split(":", 1)[1].split(",")]
            continue
        if not low.startswith("dialogue:"):
            continue
        cols = line.split(":", 1)[1].split(",")
        if fmt:
            idx_s = fmt.index("start") if "start" in fmt else 1
            idx_e = fmt.index("end") if "end" in fmt else 2
            idx_t = fmt.index("text") if "text" in fmt else len(fmt) - 1
        else:
            idx_s, idx_e, idx_t = 1, 2, 9
        # Cột Text là cột CUỐI: nối lại phần bị cắt do text chứa dấu ",".
        text_raw = ",".join(cols[idx_t:]).strip()
        start = _ass_ts_to_sec(cols[idx_s]) if len(cols) > idx_s else 0.0
        end = _ass_ts_to_sec(cols[idx_e]) if len(cols) > idx_e else 0.0
        text = _re.sub(r"\{\\[^}]*\}", "", text_raw).replace("\\N", " ").replace("\\n", " ")
        text = _re.sub(r"\s+", " ", text).strip()
        if not text or end <= start or text == prev_text:
            continue
        prev_text = text
        segments.append({"start": start, "end": end, "text": text})
    return segments


def parse_subtitle_file(path: str) -> list:
    """Parse file .srt/.vtt/.ass của tác giả -> list ``{"start", "end", "text"}`` (giây),
    ĐÚNG định dạng segment mà bước STT sinh ra. Trả list rỗng nếu file hỏng/không đọc
    được (caller fallback STT)."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            content = f.read()
    except OSError as e:  # noqa: BLE001
        logger.warning(f"[DOWNLOAD] Không đọc được file phụ đề {path}: {e}")
        return []

    # ASS/SSA (bilibili.tv) có cấu trúc riêng, không dùng "-->".
    if path.lower().endswith((".ass", ".ssa")) or "[events]" in content.lower():
        return _parse_ass(content)

    # Tách theo block trống; mỗi block có 1 dòng chứa "-->" + các dòng text.
    segments = []
    prev_text = None
    for block in _re.split(r"\r?\n\r?\n+", content):
        m = _SUB_TS_RE.search(block)
        if not m:
            continue
        g = m.groups()
        start = _ts_to_sec(g[0], g[1], g[2], g[3])
        end = _ts_to_sec(g[4], g[5], g[6], g[7])
        # Text = phần sau dòng chứa timestamp trong block.
        after = block[m.end():].strip("\r\n")
        text = _clean_sub_text(after)
        if not text or end <= start:
            continue
        # Bỏ cue lặp y hệt câu trước liền kề (VTT cuộn dòng lặp lại nội dung cũ).
        if text == prev_text:
            continue
        prev_text = text
        segments.append({"start": start, "end": end, "text": text})
    return segments


def _collect_author_subtitles(info: dict) -> dict:
    """MỌI phụ đề CHÍNH CHỦ đã tải được -> ``{lang: đường_dẫn_file}``.

    CHỈ đọc ``requested_subtitles`` (yt-dlp chỉ đưa sub thật sự tải được vào đây) và
    loại auto-caption (nằm trong ``automatic_captions`` mà không có trong ``subtitles``).

    Trả về CẢ BỘ chứ không phải một thứ tiếng: video Bilibili/YouTube thường kèm 5-10
    thứ tiếng, trước đây giữ đúng một cái rồi xoá sạch phần còn lại — muốn thứ tiếng
    khác thì phải tải lại cả video.
    """
    requested = info.get("requested_subtitles") or {}
    if not requested:
        return {}
    auto_langs = set((info.get("automatic_captions") or {}).keys())
    cand = {}
    for lang, data in requested.items():
        fp = (data or {}).get("filepath")
        if not fp or not os.path.exists(fp):
            continue
        # requested_subtitles chỉ chứa sub tác giả khi ta bật writesubtitles (không
        # bật writeautomaticsub) — nhưng phòng hờ vẫn loại lang trùng automatic_captions
        # mà KHÔNG có trong subtitles gốc.
        if lang in auto_langs and lang not in (info.get("subtitles") or {}):
            continue
        cand[lang] = fp
    return cand


def _pick_author_subtitle(info: dict, preferred_lang: str = "") -> Optional[str]:
    """Chọn ĐƯỜNG DẪN file phụ đề CHÍNH CHỦ dùng làm transcript, ưu tiên ngôn ngữ gốc
    của video. Trả None nếu không có sub chính chủ."""
    cand = _collect_author_subtitles(info)
    if not cand:
        return None

    # Ưu tiên: lang chỉ định -> ngôn ngữ gốc video (info.language) -> lang đầu tiên.
    order = [preferred_lang, info.get("language") or ""]
    for want in order:
        if not want:
            continue
        if want in cand:
            return cand[want]
        # Khớp phần gốc: "en" khớp "en-US", "zh" khớp "zh-Hans".
        base = want.split("-")[0].lower()
        for lang, fp in cand.items():
            if lang.split("-")[0].lower() == base:
                return fp
    return next(iter(cand.values()))


def _sanitize_filename(name: str) -> str:
    """Bỏ ký tự không hợp lệ trên Windows để dùng làm tên file."""
    name = _re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".")
    return name[:150] or "video"


def _is_translate_error_page(text: str) -> bool:
    """Kết quả "dịch" có thực chất là TRANG LỖI của Google không.

    Khi quá tải, Google trả nguyên trang lỗi (``Error 500 (Server Error)!!1...
    That's an error. There was an error. Please try again later.``) như một chuỗi
    dịch BÌNH THƯỜNG — không raise. Không chặn thì cụm đó thành tên file.

    Bắt theo DẤU HIỆU GHÉP ĐÔI (mã lỗi + câu đặc trưng của Google) thay vì chỉ
    một từ khoá, để tiêu đề thật có chữ "error"/"500" không bị loại oan.

    (Bản song sinh cho phụ đề: translation_service._is_google_error_page —
    sửa một bên nên xem lại bên kia.)
    """
    low = (text or "").lower()
    if not low:
        return False
    has_code = _re.search(r"\berror \d{3}\b", low) or "(server error)" in low
    has_phrase = (
        "that’s an error" in low
        or "that's an error" in low
        or "that’s all we know" in low
        or "that's all we know" in low
        or "please try again later" in low
    )
    return bool(has_code and has_phrase)


def translate_title_to_vi(title: str) -> str:
    """Dịch tiêu đề video sang TIẾNG VIỆT (Google Translate) — dùng đặt tên file
    khi lưu về máy (vd tên phim tiếng Trung -> "Nam Kinh 1937 đã có mặt").
    GIỮ NGUYÊN DẤU tiếng Việt; chỉ bỏ ký tự không hợp lệ với tên file Windows.
    Lỗi dịch (mạng/429...) -> trả chuỗi RỖNG để caller giữ tên gốc, không làm
    hỏng lượt tải."""
    import time

    src = (title or "").strip()
    if not src:
        return ""
    def _clean_result(translated: str) -> str:
        translated = _re.sub(r'[\\/:*?"<>|]', " ", translated)
        translated = _re.sub(r"\s+", " ", translated).strip(" .")
        return translated[:120]

    last_err = None
    attempts = 4
    for attempt in range(attempts):
        # NGHỈ GIÃN CÁCH trước khi thử lại (0s/1s/2s/3s). Khi Google quá tải,
        # thử lại NGAY LẬP TỨC đập vào đúng trạng thái lỗi đó — đo thực tế có
        # lúc trượt cả loạt; nghỉ 1-3s thì lượt sau thường qua.
        if attempt:
            time.sleep(attempt)
        try:
            from deep_translator import GoogleTranslator

            translated = (
                GoogleTranslator(source="auto", target="vi").translate(src[:300]) or ""
            )
            # Google quá tải thường trả NGUYÊN TRANG LỖI HTML dưới dạng văn bản
            # dịch "thành công" (không raise) -> nếu không chặn, cả cụm
            # "Error 500 (Server Error)!!1..." thành TÊN FILE / tên dự án.
            if not _is_translate_error_page(translated):
                return _clean_result(translated)
            last_err = "Google trả trang lỗi (quá tải)"
            logger.warning(
                f"[DOWNLOAD] Dịch tiêu đề: Google trả trang lỗi "
                f"(lần {attempt + 1}/{attempts}), thử lại."
            )
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(
                f"[DOWNLOAD] Dịch tiêu đề lỗi (lần {attempt + 1}/{attempts}): {e}"
            )
        # Đường chính trượt -> thử NGAY endpoint dự phòng gtx (hạn mức khác,
        # thường vẫn sống khi endpoint chính bị Google siết theo IP).
        try:
            from services.translation_service import _gtx_translate

            translated = _gtx_translate(src[:300], "auto", "vi")
            logger.info("[DOWNLOAD] Dịch tiêu đề qua endpoint dự phòng (gtx).")
            return _clean_result(translated)
        except Exception as gtx_err:  # noqa: BLE001
            last_err = gtx_err
            logger.warning(
                f"[DOWNLOAD] Endpoint dự phòng (gtx) cũng lỗi "
                f"(lần {attempt + 1}/{attempts}): {str(gtx_err)[:60]}"
            )
    logger.warning(f"[DOWNLOAD] Dịch tiêu đề thất bại (giữ tên gốc): {last_err}")
    return ""


# Đuôi file TẠM mà yt-dlp có thể sinh ra cho 1 stem: file luồng riêng (.f251.webm),
# file dở dang (.part) và file resume (.ytdl). CHỈ nhận diện file tạm + file trùng
# ĐÚNG đuôi đích — KHÔNG đụng file khác loại của người dùng (vd đã có "phim.mp3"
# thì tải "phim.mp4" không được xóa mp3 đó).
_TEMP_SUFFIX_RE = _re.compile(
    r"^\.(?:f\d+\.)?(?:mp4|mkv|webm|mov|flv|ts|mp3|m4a|opus|ogg|aac|wav|ytdl)\.part$"
    r"|^\.f\d+\.(?:mp4|mkv|webm|mov|flv|ts|mp3|m4a|opus|ogg|aac|wav)$"
    r"|^\.ytdl$",
    _re.IGNORECASE,
)


def _cap_stem_for_path(dest_dir: str, stem: str) -> str:
    """Cắt bớt stem để đường dẫn không vượt MAX_PATH 260 của Windows.

    Chừa ~30 ký tự cho phần đuôi yt-dlp có thể thêm (".f251.mp4.part", hậu tố
    " (1)"...): giữ len(os.path.join(dest_dir, stem)) <= 230.
    """
    max_stem = 230 - len(os.path.join(dest_dir, ""))
    if max_stem < 8:
        max_stem = 8  # thư mục quá sâu: vẫn giữ tối thiểu 8 ký tự tên file
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(" ._")
    return stem or "video"


def _pick_free_stem(dest_dir: str, stem: str, target_ext: str = "") -> str:
    """Chọn tên file (stem, không đuôi) KHÔNG xung đột trong ``dest_dir``.

    Windows: nếu file cùng tên còn bị process khác giữ (ffmpeg của lượt tải trước
    bị hủy chưa thoát hẳn, trình phát nhạc đang mở file...) thì yt-dlp rename
    ``.part`` -> file đích sẽ lỗi "[WinError 32] being used by another process".
    Xử lý: file tạm / file trùng ĐÚNG đuôi ``target_ext`` mà XÓA ĐƯỢC thì xóa
    (người dùng đã đồng ý ghi đè qua hộp thoại lưu); xóa KHÔNG được (đang bị
    khóa) -> tự thêm hậu tố " (1)", " (2)"... như trình duyệt.
    """
    ext_low = (target_ext or "").lstrip(".").lower()

    def _is_conflict_suffix(suffix: str) -> bool:
        if _TEMP_SUFFIX_RE.match(suffix):
            return True
        # File hoàn chỉnh: CHỈ khi trùng đúng đuôi đích (vd ".mp4" khi tải mp4).
        return bool(ext_low) and suffix.lower() == "." + ext_low

    def _conflicts(s: str) -> list:
        try:
            names = os.listdir(dest_dir)
        except OSError:
            return []
        return [
            os.path.join(dest_dir, fn)
            for fn in names
            if fn.startswith(s + ".") and _is_conflict_suffix(fn[len(s):])
        ]

    candidate = stem
    for i in range(1, 51):
        blocked = False
        for p in _conflicts(candidate):
            try:
                os.remove(p)
            except OSError:
                blocked = True
        if not blocked:
            return candidate
        candidate = f"{stem} ({i})"
    return candidate


def download_video(url: str, dest_dir: str, on_progress=None, cancel_event=None,
                   quality: str = "best", filename: str = "",
                   total_hint_bytes: int = 0, mode: str = "video",
                   want_subtitles: bool = False, subtitle_lang: str = "",
                   subtitle_to_file: bool = False) -> dict:
    """Tải 1 video về ``dest_dir``.

    Trả về ``{"path": <đường dẫn file>, "title": <tiêu đề>}``.
    ``want_subtitles`` — cũng tải phụ đề CHÍNH CHỦ (tác giả upload) nếu có; khi tìm
    được sẽ thêm ``"subtitles"`` (list ``{start,end,text}``) và ``"subtitle_lang"``
    vào dict trả về để caller đổ thẳng làm transcript (bỏ qua STT). KHÔNG tải
    auto-caption. ``subtitle_lang`` — ngôn ngữ ưu tiên (trống = dùng ngôn ngữ gốc).
    ``subtitle_to_file`` — GIỮ LẠI file phụ đề đã chọn trên đĩa (chế độ "lưu về máy":
    xuất .srt/.ass cạnh video) và trả ``"subtitle_path"`` thay vì xoá đi.
    ``on_progress(dict)`` (tùy chọn) nhận tiến độ: percent, downloaded, total, speed.
    ``cancel_event`` (threading.Event, tùy chọn) — nếu set giữa chừng thì hủy tải
    ngay (yt-dlp dừng khi hook raise).
    ``quality`` — "best" | "1080" | "720" | "480" | "360".
    ``filename`` — tên file mong muốn (không kèm đuôi). Trống -> dùng id video.
    Raise ``ValueError`` / ``DownloadCancelled`` khi lỗi / bị hủy.
    """
    yt_dlp = _import_yt_dlp()
    os.makedirs(dest_dir, exist_ok=True)

    # Kiểm tra dung lượng đĩa TRƯỚC khi tải (khi có ước lượng size từ probe):
    # cần dư ~30% cho file tạm .part + bước ghép ffmpeg. Báo sớm còn hơn tải
    # nửa chừng rồi lỗi "No space left".
    if total_hint_bytes:
        import shutil

        try:
            free = shutil.disk_usage(dest_dir).free
        except OSError:
            free = None
        if free is not None and free < total_hint_bytes * 1.3:
            need_mb = round(total_hint_bytes * 1.3 / (1024 * 1024))
            free_mb = round(free / (1024 * 1024))
            raise ValueError(
                L(f"Ổ đĩa không đủ dung lượng trống (cần ~{need_mb} MB, còn {free_mb} MB). "
                "Hãy dọn bớt rồi tải lại.", f"Not enough free disk space (need ~{need_mb} MB, {free_mb} MB left). Free up some space and download again.")
            )

    base = _base_opts(quality, url=url)

    #  DOUYIN: lấy link phát từ trang chia sẻ rồi tải THẲNG link đó (extractor
    #  Douyin của yt-dlp đã chết — xem khối "DOUYIN" ở trên). yt-dlp vẫn là thứ
    #  đi tải, nên tiến độ / hủy giữa chừng / đặt tên tệp giữ nguyên như mọi nền
    #  tảng khác. Douyin chỉ phát MỘT luồng nên tham số chất lượng không có tác
    #  dụng — ép "best" thay vì để selector theo chiều cao lọc trượt hết format.
    douyin_meta = None
    fetch_url = url
    if _is_douyin(url):
        douyin_meta = douyin_fetch_info(url)
        fetch_url = douyin_meta["play_url"]
        base["format"] = "best"
        base["http_headers"] = {
            **(base.get("http_headers") or {}),
            "User-Agent": _DOUYIN_SHARE_UA,
            "Referer": "https://www.iesdouyin.com/",
        }

    if mode == "audio":
        # Chỉ tải âm thanh THÔ (bestaudio) rồi TỰ convert sang MP3 bằng ffmpeg của app
        # ở bước sau. KHÔNG dùng postprocessor FFmpegExtractAudio của yt-dlp vì nó gọi
        # ffprobe để dò codec — imageio-ffmpeg CHỈ có ffmpeg, KHÔNG có ffprobe -> lỗi
        # "unable to obtain file audio codec with ffprobe", mp3 ra 0 KB. ffmpeg convert
        # trực tiếp không cần ffprobe.
        # bestaudio  = format audio-only tốt nhất;
        # bestaudio*[vcodec^=h264] = format muxed CÓ audio, ƯU TIÊN H264 — TikTok
        #   khai láo mọi format đều có aac nhưng stream H265 (bytevc1) thực tế
        #   KHÔNG có tiếng (đã kiểm chứng bằng ffmpeg); chỉ bản h264 có tiếng thật.
        #   Sort mặc định của yt-dlp lại xếp h265 trên h264 nên phải ghim rõ;
        # bestaudio* = format tốt nhất có chứa audio (nấc chung mọi nền tảng) —
        #   thiếu nấc này thì "best" đơn thuần có thể chọn nhầm format VIDEO-ONLY
        #   -> ffmpeg convert MP3 lỗi "no stream";
        # best       = vé vớt cuối cùng.
        if _is_youtube(url):
            # YouTube: luồng tải thẳng bị chặn giữa chừng (403) nên phải đi HLS —
            # xem giải thích ở _base_opts. HLS của YouTube KHÔNG có nhánh
            # audio-only, vì vậy lấy fragment muxed nhẹ nhất (ưu tiên độ phân giải
            # thấp cho nhanh) rồi _convert_audio_to_mp3 bóc tiếng ra MP3.
            # KHÔNG có vé vớt sang luồng thẳng (sẽ 403) — hết HLS thì để lỗi
            # "format is not available" nổi lên cho vé vớt android xử lý.
            base["format"] = (
                "bestaudio[protocol^=m3u8]/worstvideo[protocol^=m3u8]"
                "/best[protocol^=m3u8]"
            )
        else:
            base["format"] = (
                "bestaudio/bestaudio*[vcodec^=h264]/bestaudio*[vcodec^=avc]"
                "/bestaudio*/best"
            )
        base.pop("merge_output_format", None)
    if want_subtitles and mode != "audio":
        # CHỈ sub chính chủ (tác giả upload) — KHÔNG bật writeautomaticsub. Tải mọi
        # ngôn ngữ tác giả có rồi tự chọn (ưu tiên ngôn ngữ gốc) ở _pick_author_subtitle.
        base["writesubtitles"] = True
        base["writeautomaticsub"] = False
        base["subtitleslangs"] = ["all"]
        base["subtitlesformat"] = "srt/vtt/best"
    out_ext = "%(ext)s"
    # Đuôi file ĐÍCH sau postprocess (ghép mp4 / xuất mp3) — dùng để chỉ dọn
    # file trùng đúng loại, không đụng file khác đuôi của người dùng.
    target_ext = "mp3" if mode == "audio" else "mp4"
    if filename:
        stem = _sanitize_filename(os.path.splitext(filename)[0])
        # Cắt bớt stem nếu đường dẫn quá dài (MAX_PATH 260 của Windows).
        stem = _cap_stem_for_path(dest_dir, stem)
        # Tránh [WinError 32]: dọn file trùng stem của lượt tải trước; nếu file
        # đang bị khóa thì tự thêm hậu tố " (1)"... để không đụng.
        stem = _pick_free_stem(dest_dir, stem, target_ext)
        base["outtmpl"] = os.path.join(dest_dir, stem + "." + out_ext)
    else:
        # Không có tên chỉ định (tải cho dubbing/review): thêm hậu tố ngắn ngẫu
        # nhiên sau id để 2 lượt tải CÙNG video (song song / tải lại) không đụng
        # nhau ([WinError 32] / ghi đè file đang dùng). Caller dùng path trả về
        # nên tên file có hậu tố không ảnh hưởng.
        import uuid

        #  DOUYIN: ta đưa cho yt-dlp một LINK PHÁT THẲNG, nên "%(id)s" của nó là id
        #  bịa từ URL — kèm nguyên chuỗi truy vấn ("?a=1128&ch=0&"). Windows không
        #  cho các ký tự đó trong tên tệp -> "[Errno 22] Invalid argument" ngay khi
        #  mở tệp để ghi, tải chưa được byte nào. Dùng mã video của Douyin thay thế.
        stem_id = (douyin_meta or {}).get("id") or "%(id)s"
        base["outtmpl"] = os.path.join(
            dest_dir, stem_id + "_" + uuid.uuid4().hex[:8] + "." + out_ext
        )

    # Chất lượng cao: yt-dlp tải VIDEO và AUDIO thành 2 luồng riêng rồi ghép. Nếu báo
    # tiến độ theo TỪNG file, UI sẽ thấy "2 lần tải" (video xong lại chạy audio). Ta
    # GỘP tiến độ mọi luồng thành 1 thanh: cộng dồn byte đã tải + tổng byte của tất cả
    # luồng. Dùng total_hint_bytes (ước lượng từ probe) làm tổng để thanh chạy mượt
    # 0->100% ngay từ đầu (không bị nhảy khi luồng audio bắt đầu).
    _seen: dict = {}  # filename -> (downloaded, total)

    def _hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        status = d.get("status")
        if status not in ("downloading", "finished"):
            return
        key = d.get("filename") or d.get("tmpfilename") or str(id(d))
        cur_total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        cur_done = d.get("downloaded_bytes") or 0
        if status == "finished":
            cur_done = cur_total = (cur_total or cur_done)
        #  Ghi cả khi không ai nghe tiến độ: vòng "tải tiếp" cần biết lượt vừa hỏng có
        #  tải thêm được byte nào không.
        _seen[key] = (cur_done, cur_total)
        if on_progress is None:
            return

        agg_done = sum(dl for dl, _ in _seen.values())
        agg_total_seen = sum(t for _, t in _seen.values())
        grand = max(total_hint_bytes, agg_total_seen) if total_hint_bytes else agg_total_seen
        # Kẹp <=100 (ước lượng tổng có thể THẤP hơn thực tế); khi nền tảng không
        # cho biết tổng (HLS) vẫn phát sự kiện kèm downloaded_mb để UI hiện
        # "Đã tải X MB" thay vì kẹt 0%.
        on_progress({
            "downloaded": agg_done,
            "downloaded_mb": round(agg_done / (1024 * 1024), 1),
            "total": grand,
            "percent": min(100.0, round(agg_done / grand * 100, 1)) if grand else 0,
            "speed": d.get("speed") or 0,  # bytes/s (của luồng hiện tại)
        })
    base["progress_hooks"] = [_hook]

    # Pha POSTPROCESS (ffmpeg merge video+audio — video dài mất nhiều phút):
    # progress_hooks không còn chạy nên bấm Hủy bị bỏ qua. Hook postprocessor
    # được gọi trước/sau mỗi bước PP -> kiểm cờ hủy ở đó để dừng sớm.
    def _pp_hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        # Báo UI biết đang ghép (không có % chi tiết nhưng không "đứng im").
        if on_progress is not None and d.get("status") == "started":
            on_progress({
                "downloaded": 0, "downloaded_mb": 0, "total": 0, "percent": 100.0,
                "speed": 0, "stage": "processing",
            })
    base["postprocessor_hooks"] = [_pp_hook]

    def _runner(opts: dict):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(fetch_url, download=True)
            if info.get("_type") == "playlist" and info.get("entries"):
                info = info["entries"][0]
            file_path = ydl.prepare_filename(info)
            # Postprocessor/merge đổi đuôi (mp3 khi tải audio, mp4 khi ghép video).
            if not os.path.exists(file_path):
                base_name, _ = os.path.splitext(file_path)
                # Audio THÔ chưa convert -> đủ loại container (webm/m4a/opus…).
                exts = (
                    [".m4a", ".webm", ".opus", ".mp3", ".aac", ".ogg", ".mp4"]
                    if mode == "audio"
                    else [".mp4", ".mkv", ".webm"]
                )
                # Chọn file MỚI SỬA GẦN NHẤT trong các ứng viên — nếu chọn theo thứ
                # tự cố định có thể vớ phải file cũ trùng tên còn sót từ lượt tải
                # trước (vd .mp4 video-only) -> convert MP3 lỗi "no stream".
                candidates = [
                    base_name + ext for ext in exts
                    if os.path.exists(base_name + ext)
                ]
                if candidates:
                    file_path = max(candidates, key=os.path.getmtime)
        return info, file_path

    #  Nghỉ tăng dần giữa các lần thử lại của yt-dlp (1, 2, 4… tối đa 20s), CHỜ BẰNG cờ
    #  Hủy để bấm Hủy lúc đang nghỉ vẫn dừng ngay. Trả 0 vì đã tự nghỉ ở đây.
    def _ngu_thu_lai(n):
        cho = min(2.0 ** n, _NGU_THU_LAI_TOI_DA)
        if cancel_event is None:
            return cho
        if cancel_event.wait(cho):
            raise DownloadCancelled()
        return 0
    base["retry_sleep_functions"] = {"http": _ngu_thu_lai}

    def _tai_mot_luot():
        try:
            return _run_with_cookie_fallback(url, base, "tải", _runner)
        except ValueError:
            # YouTube: video KHÔNG có luồng HLS -> selector ở _YT_HLS_FORMATS không
            # khớp format nào. Tải lại bằng client "android" (luồng tải trọn vẹn,
            # tối đa 360p) thay vì để người dùng thấy lỗi. Xem _YT_ANDROID_ARGS.
            if not (_is_youtube(url) and _no_hls_format_error(_last_download_error[0])):
                raise
            logger.info("[DOWNLOAD] YouTube không có luồng HLS -> thử lại bằng client android.")
            base["extractor_args"] = _YT_ANDROID_ARGS
            base["format"] = "best" if mode != "audio" else "bestaudio/best"
            return _run_with_cookie_fallback(url, base, "tải", _runner)

    #  ĐỨT GIỮA CHỪNG -> chạy lại: yt-dlp lấy link MỚI (có khi sang máy chủ CDN nhanh
    #  hơn) rồi tải TIẾP từ tệp .part — xem khối "ĐỨT GIỮA CHỪNG" ở trên. Dừng khi
    #  _TAI_TIEP_KHONG_TIEN lượt liền nhau không thêm được byte nào.
    luot = 0
    khong_tien = 0
    moc = 0
    while True:
        try:
            info, file_path = _tai_mot_luot()
            break
        except ValueError as e:
            raw = _strip_ansi(str(e.__cause__ or e))
            da_tai = sum(dl for dl, _ in _seen.values())
            if not _la_loi_dut_mang(raw, da_tai):
                raise
            khong_tien = khong_tien + 1 if da_tai <= moc else 0
            moc = max(moc, da_tai)
            luot += 1
            if luot > _TAI_TIEP_TOI_DA or khong_tien >= _TAI_TIEP_KHONG_TIEN:
                logger.error(
                    f"[DOWNLOAD] Bỏ cuộc sau {luot} lượt tải tiếp "
                    f"(đã có {moc / 1048576:.0f} MB): {raw[:200]}"
                )
                raise ValueError(
                    L("Mạng hoặc máy chủ video liên tục ngắt giữa chừng nên chưa tải xong "
                    f"(đã tải được {moc / 1048576:.0f} MB). Kiểm tra kết nối mạng rồi "
                    "Thử lại; nếu vẫn lỗi, chọn chất lượng thấp hơn cho tệp nhẹ hơn.", f"The network or video server kept dropping the connection, so the download did not finish (got {moc / 1048576:.0f} MB). Check your network connection and Retry; if it still fails, choose a lower quality for a lighter file.")
                ) from e
            logger.warning(
                f"[DOWNLOAD] Đứt giữa chừng (đã có {da_tai / 1048576:.0f} MB) -> lấy link "
                f"mới, tải tiếp (lượt {luot}/{_TAI_TIEP_TOI_DA}): {raw[:160]}"
            )
            if cancel_event is not None and cancel_event.wait(min(2.0 * luot, 10.0)):
                raise DownloadCancelled() from e
            #  Douyin: ta đưa yt-dlp LINK PHÁT THẲNG nên nó không tự lấy link mới được —
            #  xin link phát mới từ trang chia sẻ (khách gặp thật 27-09-2026: zjcdn.com
            #  "Read timed out" / "27 bytes read, 26993274 more expected"). Xin hỏng thì
            #  cứ tải tiếp bằng link cũ.
            if douyin_meta:
                try:
                    douyin_meta = douyin_fetch_info(url)
                    fetch_url = douyin_meta["play_url"]
                except Exception as e2:  # noqa: BLE001
                    logger.warning(f"[DOWNLOAD] Douyin: không xin được link phát mới: {e2}")

    if not os.path.exists(file_path):
        raise ValueError(L("Tải xong nhưng không tìm thấy file trên đĩa.", "Download finished but the file was not found on disk."))

    # Audio: tự chuyển sang MP3 bằng ffmpeg (không dùng ffprobe). Video dài có thể mất
    # ít giây -> báo UI đang xử lý + kiểm cờ hủy trước khi convert.
    if mode == "audio" and not file_path.lower().endswith(".mp3"):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        if on_progress is not None:
            on_progress({
                "downloaded": 0, "downloaded_mb": 0, "total": 0, "percent": 100.0,
                "speed": 0, "stage": "processing",
            })
        file_path = _convert_audio_to_mp3(file_path)

    title = info.get("title") or os.path.basename(file_path)
    if douyin_meta:
        #  Link phát thẳng không mang tiêu đề/thời lượng — lấy từ trang chia sẻ,
        #  không thì tên dự án thành "play" và thời lượng bằng 0.
        title = douyin_meta["title"]
    # Thời lượng lấy từ metadata yt-dlp (nguồn đáng tin cậy; xử lý cả nền tảng thiếu
    # field 'duration' cấp info như bilibili.tv). File BiliBili sau khi ghép DASH đôi
    # khi không có 'Duration' trong header nên ffprobe đọc ra 0 -> route dùng giá trị
    # này để fallback.
    result = {
        "path": os.path.normpath(file_path),
        "title": title,
        "duration": (douyin_meta or {}).get("duration") or _extract_duration(info),
    }

    if want_subtitles:
        # GIỮ HẾT MỌI THỨ TIẾNG tác giả có, không chỉ một.
        #
        # Bản cũ chọn đúng một thứ tiếng (ưu tiên tiếng gốc) rồi XOÁ sạch phần còn lại,
        # nên video Bilibili kèm chục thứ tiếng vẫn chỉ ra mỗi tiếng Anh — muốn thứ khác
        # thì phải tải lại cả video. Nay parse tất cả: ``subtitles_by_lang`` để caller
        # cho người dùng chọn về sau, ``subtitles``/``subtitle_lang`` vẫn là thứ tiếng
        # ưu tiên như trước (mọi đường gọi cũ không phải sửa gì).
        cand = _collect_author_subtitles(info)
        sub_path = _pick_author_subtitle(info, subtitle_lang)

        def _lang_of(path: str, fallback: str = "") -> str:
            """Thứ tiếng suy từ tên file ``<stem>.<lang>.<ext>`` mà yt-dlp đặt."""
            m = _re.search(r"\.([A-Za-z-]+)\.(?:srt|vtt|ass|ssa)$", os.path.basename(path))
            return (m.group(1) if m else "") or fallback

        by_lang = {}
        paths_by_lang = {}
        for lang, fp in cand.items():
            segs = parse_subtitle_file(fp)
            if not segs:
                continue
            key = lang or _lang_of(fp)
            by_lang[key] = segs
            paths_by_lang[key] = os.path.normpath(fp)
        if by_lang:
            result["subtitles_by_lang"] = by_lang
            logger.info(
                f"[DOWNLOAD] Phụ đề chính chủ: {len(by_lang)} thứ tiếng "
                f"({', '.join(sorted(by_lang))})."
            )

        if sub_path:
            chosen = _lang_of(sub_path, info.get("language") or "")
            segs = by_lang.get(chosen) or parse_subtitle_file(sub_path)
            if segs:
                result["subtitles"] = segs
                result["subtitle_lang"] = chosen
                logger.info(
                    f"[DOWNLOAD] Dùng phụ đề chính chủ ({chosen or '?'}) "
                    f"làm transcript: {len(segs)} câu."
                )
            if subtitle_to_file:
                # Chế độ "lưu về máy": GIỮ file sub đã chọn cạnh video, trả path để
                # caller đổi tên khớp với video sau khi rename.
                result["subtitle_path"] = os.path.normpath(sub_path)

        if subtitle_to_file:
            # Giữ HẾT file trên đĩa để caller đổi tên "<video>.<lang>.<ext>".
            result["subtitle_paths"] = paths_by_lang
        else:
            # Không lưu về máy: đã parse xong thành segment rồi, dọn file cho sạch.
            for fp in paths_by_lang.values():
                try:
                    os.remove(fp)
                except OSError:
                    pass
            # Cả những file parse ra rỗng (hỏng / định dạng lạ) cũng phải dọn.
            for data in (info.get("requested_subtitles") or {}).values():
                fp = (data or {}).get("filepath")
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass

    return result
