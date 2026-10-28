"""CẮT ĐOẠN TỪ BẢN "XUẤT VIDEO" ĐÃ CÓ — thay vì dựng lại từng đoạn (Chạy hàng loạt).

Người dùng yêu cầu (24-09-2026): lô đã chạy bước Xuất video (bản dài, đã đốt phụ đề /
logo / chữ) rồi sang bước Xuất đoạn thì cắt luôn từ bản dài đó, đừng dựng lại.

Dựng một đoạn = ghép tiếng + đốt hình (hai lượt ffmpeg nặng, kèm trộn giọng). Cắt từ
bản đã đốt = một lượt mã hoá lại ĐÚNG quãng đó. Nội dung như nhau vì bản dài chính là
timeline đã dựng — với ĐIỀU KIỆN nó còn khớp dự án.

VÂN TAY BẢN DỰNG — chốt chặn duy nhất giữa "cắt nhanh" và "ra đoạn SAI mà không ai
biết": người dùng xuất xong vào sửa một câu / đổi kiểu phụ đề rồi mới chạy Xuất đoạn thì
bản dài đã cũ. Vân tay gồm:
  · b_chunks.json (lời bình, mốc, giọng), a_chunks.json (phụ đề gốc),
  · metadata.json trừ ``state`` (cờ dựng/hoàn tất đổi mỗi lượt chạy, không đổi nội dung),
  · compositor_config.json trừ ghi chú "lần xuất gần nhất" (ghi SAU khi xuất),
  · cỡ + mốc sửa của MỌI tệp mà các cấu hình trên trỏ tới (logo, nhạc, clip video, ảnh
    nền…) — ghi đè logo cùng tên thì cấu hình không đổi một chữ nhưng hình thì đổi.
Lệch bất kỳ chỗ nào -> dựng lại như cũ (chậm nhưng đúng). Thà dựng thừa còn hơn cắt sai.
"""
from __future__ import annotations

from utils.i18n import L
import hashlib
import json
import os
import subprocess

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

#: Khoá metadata đổi mỗi lượt chạy mà KHÔNG đổi nội dung video.
BO_KHOA_META = frozenset({"state"})
#: Khoá cấu hình dựng hình do chính lượt xuất ghi lại (``record_compositor_export``).
BO_KHOA_CAU_HINH = frozenset({"lastExportPath", "lastExportSize", "lastExportAt"})


def _duong_dan_trong(obj, ra: set) -> None:
    """Gom mọi chuỗi trông như đường dẫn TUYỆT ĐỐI tới một tệp có thật."""
    if isinstance(obj, dict):
        for v in obj.values():
            _duong_dan_trong(v, ra)
    elif isinstance(obj, list):
        for v in obj:
            _duong_dan_trong(v, ra)
    elif isinstance(obj, str) and len(obj) < 1024 and (
        (len(obj) > 2 and obj[1] == ":") or obj.startswith(("/", "\\\\"))
    ):
        try:
            if os.path.isfile(obj):
                ra.add(os.path.normcase(os.path.normpath(obj)))
        except (OSError, ValueError):
            pass


def van_tay_ban_dung(project_dir: str, cau_hinh_path: str) -> str:
    h = hashlib.sha1()
    tep_tro_toi: set = set()
    for ten, duong, bo in (
        ("b", os.path.join(project_dir, "b_chunks.json"), None),
        ("a", os.path.join(project_dir, "a_chunks.json"), None),
        ("m", os.path.join(project_dir, "metadata.json"), BO_KHOA_META),
        ("c", cau_hinh_path, BO_KHOA_CAU_HINH),
    ):
        h.update(ten.encode())
        try:
            with open(duong, encoding="utf-8") as f:
                du_lieu = json.load(f)
        except FileNotFoundError:
            h.update(b"<khong co>")
            continue
        except (OSError, ValueError):
            #  Đọc hỏng -> vân tay "không bao giờ khớp" (mã ngẫu nhiên theo mốc sửa), để
            #  rơi về đường dựng lại chứ không liều cắt.
            h.update(os.urandom(8))
            continue
        if bo and isinstance(du_lieu, dict):
            du_lieu = {k: v for k, v in du_lieu.items() if k not in bo}
        h.update(json.dumps(du_lieu, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        _duong_dan_trong(du_lieu, tep_tro_toi)
    for p in sorted(tep_tro_toi):
        try:
            st = os.stat(p)
            h.update(f"{p}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8"))
        except OSError:
            h.update(f"{p}|mat".encode("utf-8"))
    return h.hexdigest()


def thoi_luong(duong: str) -> float:
    import re

    r = subprocess.run([FFMPEG, "-hide_banner", "-i", duong], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def cat_doan(src: str, a: float, b: float, out: str) -> None:
    """Cắt [a, b] giây từ ``src`` ra ``out`` — MÃ HOÁ LẠI (không ``-c copy``): copy chỉ
    cắt được ở khung khoá, đoạn ra lệch đầu tới vài giây. Đoạn ngắn nên mã hoá lại nhanh.
    Ghi ra tệp tạm rồi mới đổi tên: hỏng giữa chừng thì không để lại tệp dở mang tên thật."""
    from services.compositor_utils import tom_loi_ffmpeg
    from utils.file_utils import safe_replace

    tam = out + ".dang_cat.mp4"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-v", "error",
        "-ss", f"{a:.3f}", "-i", src, "-t", f"{max(0.05, b - a):.3f}",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tam,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0 or not os.path.exists(tam) or os.path.getsize(tam) == 0:
        try:
            os.remove(tam)
        except OSError:
            pass
        raise RuntimeError(L("Cắt đoạn từ bản đã xuất hỏng: ", "Cutting a segment from the exported file failed:") + (tom_loi_ffmpeg(r.stderr or "", 400) or L(f"mã {r.returncode}", f"code {r.returncode}")))
    safe_replace(tam, out)
