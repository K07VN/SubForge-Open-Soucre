"""DỰ ÁN NÀO ĐANG BỊ LÔ XỬ LÝ — sổ ghi dùng chung giữa lô và trình dựng.

VÌ SAO CẦN RIÊNG MỘT SỔ, đã có khoá ``_REVIEW_BUSY`` rồi:

``_REVIEW_BUSY`` chỉ nói "dự án này đang bận", không nói AI làm nó bận. Mà hai
trường hợp bận cần cư xử KHÁC HẲN nhau:

  · bận vì CHÍNH NGƯỜI DÙNG bấm trong trình dựng (sinh giọng một câu, dựng thử):
    họ vẫn phải kéo thả, sửa chữ, đổi cấu hình bình thường — chặn là phá cách làm
    việc vốn có;
  · bận vì LÔ đang chạy dự án đó: mọi sửa đổi lúc này đều có thể bị lượt chạy ghi
    đè (lô đọc dự án lúc bắt đầu bước, ghi lại lúc xong) — người dùng sửa xong
    thấy mất trắng mà không có lỗi nào hiện ra.

Sổ này đánh dấu đúng trường hợp thứ hai, để trình dựng chặn SỬA (không chặn xem)
và nói rõ vì sao.

Đặt ở ``services/`` cho cả hai bên cùng nạp được: ``routes/batch_routes`` đã nạp
``routes/review_advanced_routes``, nên chiều ngược lại mà nạp thẳng là vòng tròn.
"""

import threading

_khoa = threading.Lock()
#: project_id -> {"batch_id", "step", "title"}
_dang: dict = {}


def dat(project_id: str, batch_id: str, step: str, title: str = "") -> None:
    """Lô bắt đầu xử lý dự án này."""
    if not project_id:
        return
    with _khoa:
        _dang[project_id] = {"batch_id": batch_id, "step": step, "title": title}


def xoa(project_id: str) -> None:
    """Lô làm xong (hoặc lỗi) — nhả dự án ra."""
    with _khoa:
        _dang.pop(project_id, None)


def dang_giu(project_id: str):
    """Dự án này có đang bị lô xử lý không -> thông tin lô, hoặc ``None``."""
    with _khoa:
        x = _dang.get(project_id)
        return dict(x) if x else None


def tat_ca() -> dict:
    with _khoa:
        return {k: dict(v) for k, v in _dang.items()}


def don_lo(batch_id: str) -> None:
    """Dọn sạch mọi dấu của một lô — gọi khi lượt chạy kết thúc.

    Cần vì đường thoát bất thường (lỗi, huỷ, tắt máy giữa chừng) có thể bỏ sót
    ``xoa``; bỏ sót thì dự án đó bị khoá sửa VĨNH VIỄN cho tới khi khởi động lại,
    mà người dùng không hiểu vì sao.
    """
    with _khoa:
        for pid in [k for k, v in _dang.items() if v.get("batch_id") == batch_id]:
            _dang.pop(pid, None)
