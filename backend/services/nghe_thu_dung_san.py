"""MẪU NGHE THỬ DỰNG SẴN — câu đọc thử đã sinh sẵn cho giọng VieNeu (20 giọng + giọng mẫu) và
Qwen3-TTS (9 giọng × 10 thứ tiếng), đóng kèm app ở ``assets/previews``.

Vì sao: bấm "nghe thử" lần đầu phải nạp mô hình (VieNeu ~10 s, Qwen bật tiến trình riêng) rồi mới sinh
~1–8 s. Có sẵn mp3 thì phát NGAY, và nghe thử được cả giọng VieNeu khi chưa tải mô hình.

Quy tắc an toàn (đường cũ luôn là lưới đỡ):
  * Chỉ dùng khi người dùng KHÔNG gõ câu riêng (route /preview truyền text rỗng);
  * Tệp thiếu / sai SHA-256 / manifest hỏng -> trả None, route sinh trực tiếp như trước;
  * Mọi lỗi ở đây bị nuốt (chỉ log) — không bao giờ làm hỏng nghe thử.
Sinh lại bằng ``tools/dong_goi_nghe_thu.py`` khi đổi giọng / mô hình / câu mẫu.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys

logger = logging.getLogger("NgheThuDungSan")

_cache: dict | None = None
_sha_ok: set[str] = set()


def assets_dir() -> str:
    """Thư mục mẫu: ``SUBFORGE_NGHE_THU_DIR`` (test) > cạnh ``assets`` đóng gói > cạnh mã nguồn."""
    env = os.environ.get("SUBFORGE_NGHE_THU_DIR")
    if env:
        return env
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "previews")


def _manifest() -> dict:
    global _cache
    if _cache is not None and _cache.get("_dir") == assets_dir():
        return _cache
    d = assets_dir()
    ra: dict = {"_dir": d, "voices": {}}
    try:
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
            data = json.load(f)
        voices = data.get("voices")
        if isinstance(voices, dict):
            ra["voices"] = voices
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Manifest nghe thử dựng sẵn hỏng, bỏ qua: {e}")
    _cache = ra
    return ra


def reset() -> None:
    """Xoá bộ nhớ đệm (test / sau khi sinh lại)."""
    global _cache
    _cache = None
    _sha_ok.clear()


def tim(voice: str) -> str | None:
    """Đường dẫn mp3 dựng sẵn của ``voice`` nếu CÓ và còn nguyên vẹn; không thì None.

    Câu trong mẫu là câu RIÊNG (đa dạng theo giọng/thứ tiếng), khác câu mặc định của route —
    nên chỉ gọi khi người dùng không gõ câu của mình."""
    try:
        e = _manifest()["voices"].get(voice)
        if not e:
            return None
        d = assets_dir()
        path = os.path.realpath(os.path.join(d, str(e.get("file", ""))))
        if os.path.commonpath([os.path.realpath(d), path]) != os.path.realpath(d):
            return None                                    # đường dẫn thoát khỏi thư mục mẫu
        if not os.path.isfile(path):
            return None
        if path not in _sha_ok:
            with open(path, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() != e.get("sha256"):
                    logger.warning(f"Mẫu nghe thử '{voice}' sai SHA-256, bỏ qua")
                    return None
            _sha_ok.add(path)
        return path
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"Tra mẫu nghe thử '{voice}' lỗi, sinh trực tiếp: {ex}")
        return None
