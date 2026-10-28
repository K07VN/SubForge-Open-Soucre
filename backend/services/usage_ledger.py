"""Sổ cái thống kê sử dụng cục bộ — đếm TÍCH LŨY (monotonic).

Vấn đề: get_client_stats() đếm trực tiếp số bản ghi/thư mục HIỆN CÓ trên máy. Khi
user xóa dự án cục bộ, con số tụt xuống → báo cáo lên admin sai (và nếu admin cộng
dồn snapshot nhiều ngày thì càng lệch).

Giải pháp NHẸ: giữ 1 file JSON duy nhất. Mỗi hạng mục lưu:
  - count: tổng số ĐÃ TỪNG thấy (chỉ tăng, không bao giờ giảm — tính cả cái đã xóa).
  - known: tập ID HIỆN CÒN trên máy (để nhận ra ID nào là MỚI ở lần quét sau).

Mỗi lần quét: new = current - known → count += len(new); known := current.
=> known chỉ to bằng dữ liệu đang có trên máy (không phình theo thời gian), còn
count vẫn tích lũy đúng kể cả sau khi user xóa.
"""

import os
import json
import logging
import threading

from models.database import get_app_data_dir

logger = logging.getLogger("UsageLedger")

_LOCK = threading.Lock()


def _ledger_path() -> str:
    return os.path.join(get_app_data_dir(), "usage_ledger.json")


def _load() -> dict:
    try:
        with open(_ledger_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Không đọc được sổ cái thống kê, khởi tạo lại: {e}")
        return {}


def _save(data: dict) -> None:
    try:
        path = _ledger_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)  # ghi nguyên tử, tránh hỏng file khi tắt đột ngột
    except Exception as e:
        logger.warning(f"Không lưu được sổ cái thống kê: {e}")


def _bucket_update(data: dict, key: str, current_ids) -> int:
    """Cập nhật 1 hạng mục và trả về tổng tích lũy mới."""
    bucket = data.get(key) or {}
    count = int(bucket.get("count", 0))
    known = set(bucket.get("known", []))
    current = set(str(i) for i in current_ids)

    new_ids = current - known
    count += len(new_ids)  # chỉ cộng cái mới; đã xóa không bị trừ

    data[key] = {"count": count, "known": sorted(current)}
    return count


def update_and_get(
    dubbed_ids,
    dubbing_voice_ids,
    review_ids,
    review_voice_ids,
) -> dict:
    """Nạp sổ cái, cập nhật theo dữ liệu hiện tại, lưu lại và trả về tổng tích lũy.

    Trả về đúng shape mà admin đang chờ: dubbed_videos / review_videos /
    generated_voices (gộp voice của cả lồng tiếng lẫn review).
    """
    with _LOCK:
        data = _load()
        dubbed = _bucket_update(data, "dubbed_videos", dubbed_ids)
        dubbing_voices = _bucket_update(data, "dubbing_voices", dubbing_voice_ids)
        review = _bucket_update(data, "review_videos", review_ids)
        review_voices = _bucket_update(data, "review_voices", review_voice_ids)
        _save(data)

    return {
        "dubbed_videos": dubbed,
        "review_videos": review,
        "generated_voices": dubbing_voices + review_voices,
    }
