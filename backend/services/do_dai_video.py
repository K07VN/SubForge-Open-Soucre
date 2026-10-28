"""ĐỘ DÀI VIDEO đo MỘT LẦN rồi nhớ — để Trang chủ hiện độ dài ở mọi chế độ (yêu cầu 05-10-2026).

Vì sao cần một bộ nhớ riêng: một số chế độ không lưu độ dài ở đâu cả (video Sáng tạo ``out.mp4``; bài đăng YouTube — hàng
đợi "không còn giữ thời lượng video"). Đo mỗi lần mở Trang chủ là một lượt ``ffmpeg -i`` cho từng tệp, và bản phát hành
không có ffprobe.

Cách làm:
  · Khoá = đường dẫn tuyệt đối (chuẩn hoá hoa/thường); giá trị = ``{giay, dau}`` với ``dau`` = ``[mtime_ns, size]`` của tệp lúc đo.
  · Tệp còn trên đĩa mà dấu đổi (dựng lại, ghi đè) -> đo lại. Tệp ĐÃ BỊ XOÁ -> vẫn trả số đã nhớ (bài đăng YouTube sống lâu hơn tệp nguồn).
  · KHÔNG đo trong một lượt gọi trừ khi người gọi cấp NGÂN SÁCH (``ngan_sach=[n]``, giảm dần mỗi lần đo): một danh sách vài trăm tệp
    chưa từng đo mà đo hết một lượt là treo cả lượt hỏi. Hết ngân sách thì trả ``None`` (= chưa biết), lần hỏi sau đo tiếp.
  · Đo ra 0 / lỗi KHÔNG được nhớ (có thể là lỗi tạm: tệp đang ghi dở), lần sau thử lại.

Ghi ra ``<dữ liệu app>/do_dai_video.json`` (ghi nguyên tử, lỗi ghi không làm hỏng lượt hỏi).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_KHOA = threading.Lock()
_BO_NHO: Optional[dict] = None
#  Quá ngưỡng này thì dọn bớt mục của tệp không còn trên đĩa. Cái giá: bài YouTube có tệp nguồn đã xoá mất số đã nhớ (hiện trống) —
#  chỉ xảy ra sau hàng nghìn tệp, và số liệu chỉ để xem, không ảnh hưởng việc đăng.
_NGUONG_DON = 4000


def _tep_cache() -> str:
    from models.database import get_app_data_dir

    return os.path.join(get_app_data_dir(), "do_dai_video.json")


def _nap() -> dict:
    global _BO_NHO
    if _BO_NHO is None:
        try:
            with open(_tep_cache(), encoding="utf-8") as f:
                d = json.load(f)
            _BO_NHO = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _BO_NHO = {}
    return _BO_NHO


def _ghi(m: dict) -> None:
    try:
        dich = _tep_cache()
        os.makedirs(os.path.dirname(dich), exist_ok=True)
        tam = dich + ".tmp"
        with open(tam, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        os.replace(tam, dich)
    except OSError as e:
        logger.warning(f"[do_dai_video] không ghi được bộ nhớ độ dài: {e}")


def _khoa(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _dau(path: str) -> Optional[list]:
    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def giay_cua(path: str, ngan_sach: Optional[list] = None) -> Optional[float]:
    """Độ dài (giây) của tệp video, hoặc ``None`` nếu chưa biết.

    ``ngan_sach``: ``[n]`` = còn được đo tối đa n tệp (giảm dần mỗi lần đo thật). ``None`` hoặc hết = CHỈ đọc bộ nhớ, không gọi ffmpeg.
    """
    if not path:
        return None
    k = _khoa(path)
    dau = _dau(path)
    with _KHOA:
        e = _nap().get(k)
    if isinstance(e, dict) and isinstance(e.get("giay"), (int, float)):
        if dau is None or e.get("dau") == dau:
            return float(e["giay"])                              # còn đúng tệp đã đo — hoặc tệp đã xoá (tin số cũ)
    if dau is None:
        return None                                              # chưa từng đo mà tệp cũng mất rồi
    if not ngan_sach or ngan_sach[0] <= 0:
        return None
    ngan_sach[0] -= 1
    try:
        from services.video_service import VideoService

        giay = float(VideoService.get_video_duration(path) or 0)
    except Exception as e2:  # noqa: BLE001 — đo hỏng thì coi như chưa biết (người gọi là luồng dựng video: không được làm hỏng lần dựng)
        logger.warning(f"[do_dai_video] đo lỗi {path}: {e2}")
        return None
    if giay <= 0:
        return None
    with _KHOA:
        m = _nap()
        m[k] = {"giay": round(giay, 2), "dau": dau}
        _don(m)
        _ghi(m)
    return giay


def _don(m: dict) -> None:
    """Bộ nhớ phình quá _NGUONG_DON thì bỏ bớt mục của tệp đã mất (đủ để còn khoảng nửa ngưỡng). Gọi khi đang giữ _KHOA."""
    if len(m) <= _NGUONG_DON:
        return
    for k in [k for k in m if not os.path.exists(k)][: len(m) - _NGUONG_DON // 2]:
        m.pop(k, None)
