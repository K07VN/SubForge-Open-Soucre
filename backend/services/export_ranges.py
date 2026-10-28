"""CÁC ĐOẠN ĐÃ KHOANH để "Xuất đoạn" — MỘT nguồn sự thật cho cả trình dựng Chi tiết lẫn
bước "Xuất các đoạn đã chọn" của Chạy hàng loạt.

Trước 2.15.0 danh sách này chỉ nằm trong state giao diện của trình dựng: đóng màn là
mất, và lô không có cách nào biết người dùng muốn cắt những đoạn nào. Người dùng muốn
(24-09-2026) khoanh ở bên nào cũng được, sửa ở bên nào thì bên kia thấy y như vậy.

LƯU Ở TỆP RIÊNG ``export_ranges.json``, KHÔNG trong ``metadata.json``: metadata bị hàng
chục đường đọc-sửa-ghi NGUYÊN TỆP mà không có khoá (dựng, dồn, sinh giọng…). Một lượt
dựng đọc metadata lúc bắt đầu rồi ghi lại lúc xong là xoá mất đoạn người dùng vừa khoanh
giữa chừng — âm thầm. Tệp riêng thì chỉ hai cửa dưới đây đụng tới.

Mốc là giây trên TIMELINE (đúng thang trình dựng khoanh và ``render(ranges=…)`` nhận),
không phải giây của video gốc: sau bước Dồn khớp hình, clip video có thể đổi tốc độ nên
hai thang lệch nhau.
"""
from __future__ import annotations

import json
import os

from utils.file_utils import safe_replace

TEN_TEP = "export_ranges.json"
#  Đoạn ngắn hơn mức này không xuất được (khớp ``tongDoan < 0.2`` ở trình dựng).
DAI_TOI_THIEU = 0.2
TOI_DA = 100


def chuan_hoa(ranges, tong_dai: float | None = None) -> list[dict]:
    """Lọc + sắp xếp: bỏ đoạn hỏng / quá ngắn, kẹp vào [0, tong_dai], sắp theo đầu đoạn.

    KHÔNG gộp các đoạn chồng nhau: mỗi đoạn là MỘT video riêng người dùng muốn có —
    gộp lại là tự ý đổi số video họ nhận được."""
    ra = []
    for r in ranges or []:
        try:
            a, b = float(r.get("start")), float(r.get("end"))
        except (AttributeError, TypeError, ValueError):
            continue
        if a != a or b != b:  # NaN
            continue
        a = max(0.0, a)
        if tong_dai and tong_dai > 0:
            b = min(b, float(tong_dai))
        if b - a < DAI_TOI_THIEU:
            continue
        ra.append({"start": round(a, 3), "end": round(b, 3)})
    ra.sort(key=lambda x: (x["start"], x["end"]))
    return ra[:TOI_DA]


#: project_dir -> số đoạn. Bảng lô hỏi trạng thái 2 giây một lần cho cả trăm dự án;
#: mọi lần ghi đều đi qua ``ghi`` nên giữ số đếm ở đây là đúng, khỏi đọc đĩa mỗi nhịp.
_DEM: dict = {}


def dem(project_dir: str) -> int:
    if project_dir not in _DEM:
        _DEM[project_dir] = len(doc(project_dir))
    return _DEM[project_dir]


def doc(project_dir: str) -> list[dict]:
    try:
        with open(os.path.join(project_dir, TEN_TEP), encoding="utf-8") as f:
            return chuan_hoa((json.load(f) or {}).get("ranges"))
    except (OSError, ValueError, AttributeError):
        return []


def ghi(project_dir: str, ranges) -> list[dict]:
    sach = chuan_hoa(ranges)
    duong = os.path.join(project_dir, TEN_TEP)
    tam = duong + ".tmp"
    with open(tam, "w", encoding="utf-8") as f:
        json.dump({"ranges": sach}, f, ensure_ascii=False)
    safe_replace(tam, duong)
    _DEM[project_dir] = len(sach)
    return sach
