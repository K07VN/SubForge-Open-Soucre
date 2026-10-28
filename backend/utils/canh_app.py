"""Canh app (tiến trình cha = Electron) còn sống để backend không thành tiến trình mồ côi.

Logic thuần, không đụng FastAPI / DB để test được riêng (``tests/test_canh_app.py``); ``main.check_heartbeat`` chỉ gọi
``CanhApp.mot_vong`` mỗi 5 s rồi tự tắt khi được bảo.

Bằng chứng xếp theo độ tin cậy (08-10-2026, sau 4 lần backend tự tắt OAN thấy trong subforge_desktop.log):
  1. TIẾN TRÌNH CHA còn sống -> app còn sống -> KHÔNG BAO GIỜ tự tắt, dù heartbeat im bao lâu. Cửa sổ bị ẩn, máy
     ngủ, renderer treo… đều không phải lý do để giết backend.
  2. Tiến trình cha đã mất -> app đã thoát hoặc sập -> tắt; đang có việc nặng thì chạy nốt rồi mới tắt.
  3. Không xác định được cha (chạy tay ngoài Electron, thiếu psutil): rơi về heartbeat như cũ, NHƯNG vòng canh mà thấy
     đồng hồ tường nhảy quá ``NGUONG_MAY_NGU_S`` thì coi là máy vừa ngủ / khoá sâu (Modern Standby: mọi tiến trình đứng
     im, đồng hồ vẫn chạy) -> đặt lại mốc, cho giao diện 90 s để ping lại. Trước đây đúng lúc tỉnh dậy luồng canh dậy
     TRƯỚC renderer, thấy mốc cũ hơn 90 s là tự tắt ngay — người dùng mở khoá màn hình là thấy app chết, phải tắt bật lại.
     (Log 05-10, 06-10 ×2, 08-10: heartbeat đều đặn 5 s rồi im bặt 10–30 phút KHÔNG có dòng log nào của bất kỳ ai, rồi
     dòng "Không nhận được heartbeat quá 90s" — đúng dấu vết máy ngủ.)
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Optional

logger = logging.getLogger("SubForge")

NGUONG_MAT_HEARTBEAT_S = 90.0
#  Khoảng cách giữa hai vòng canh (ngủ 5 s) mà nhảy quá mức này = máy vừa ngủ / ngủ đông / khoá sâu.
NGUONG_MAY_NGU_S = 30.0


def tien_trinh_cha():
    """Tiến trình đã mở backend (Electron main) — ``None`` nếu không xác định được.

    Electron truyền ``SUBFORGE_PARENT_PID`` cho chắc; không có thì lấy ``os.getppid()`` (bản đóng gói onedir: exe là chính
    tiến trình, cha = Electron). ``psutil.Process`` nhớ ``create_time`` nên ``is_running()`` không bị PID tái dùng đánh lừa.
    """
    try:
        import psutil  # noqa: WPS433

        pid = int(os.environ.get("SUBFORGE_PARENT_PID") or 0) or os.getppid()
        if pid <= 1:
            return None
        return psutil.Process(pid)
    except Exception:  # noqa: BLE001
        return None


def la_desktop() -> bool:
    return os.environ.get("SUBFORGE_DESKTOP") == "true" or bool(getattr(sys, "frozen", False))


class CanhApp:
    """Trạng thái giữa các vòng canh. ``mot_vong(now, last_heartbeat)`` trả ``(quyet_dinh, last_heartbeat_moi)``;
    ``quyet_dinh`` là ``"tat"`` khi backend phải tự tắt, còn lại ``None``."""

    def __init__(self, dang_co_viec_nang: Callable[[], bool], cha=None, desktop: Optional[bool] = None):
        self.dang_co_viec_nang = dang_co_viec_nang
        self.desktop = la_desktop() if desktop is None else desktop
        self.cha = cha if cha is not None else (tien_trinh_cha() if self.desktop else None)
        self.vong_truoc: Optional[float] = None
        self._da_bao_im = False
        self._da_bao_cho_viec = False
        if self.cha is not None:
            logger.info(f"[SERVER] Canh tiến trình app (pid {getattr(self.cha, 'pid', '?')}) — app còn chạy thì backend không tự tắt.")

    def _cha_con_song(self) -> Optional[bool]:
        if self.cha is None:
            return None
        try:
            return bool(self.cha.is_running())
        except Exception:  # noqa: BLE001
            return None

    def mot_vong(self, now: float, last_heartbeat: float):
        #  Máy vừa ngủ / khoá sâu: đồng hồ nhảy một khoảng lớn giữa hai vòng -> đặt lại mốc, không ai bị oan.
        if self.vong_truoc is not None and now - self.vong_truoc > NGUONG_MAY_NGU_S:
            logger.info(
                f"[SERVER] Máy vừa ngủ / khoá khoảng {now - self.vong_truoc:.0f} s — đặt lại mốc heartbeat, không tự tắt."
            )
            last_heartbeat = now
        self.vong_truoc = now
        if not self.desktop:
            return None, last_heartbeat

        cha_song = self._cha_con_song()
        if cha_song is True:
            im = now - last_heartbeat > NGUONG_MAT_HEARTBEAT_S
            if im and not self._da_bao_im:
                logger.warning("[SERVER] Giao diện im heartbeat quá 90 s nhưng app vẫn chạy — giữ backend, không tự tắt.")
                self._da_bao_im = True
            elif not im:
                self._da_bao_im = False
            return None, last_heartbeat
        if cha_song is False:
            if self.dang_co_viec_nang():
                if not self._da_bao_cho_viec:
                    logger.warning("[SERVER] App đã thoát nhưng đang có việc nặng (xuất/dựng/lô) — chạy nốt rồi mới tắt.")
                    self._da_bao_cho_viec = True
                return None, last_heartbeat
            logger.warning("[SERVER] App (tiến trình cha) đã thoát — tắt backend để không thành tiến trình mồ côi.")
            return "tat", last_heartbeat

        #  Không biết cha: heartbeat là bằng chứng duy nhất (như trước 08-10-2026).
        if now - last_heartbeat > NGUONG_MAT_HEARTBEAT_S:
            if self.dang_co_viec_nang():
                logger.warning(
                    "[SERVER] Mất heartbeat >90s nhưng đang có việc nặng (xuất/dựng/lô) — giữ backend, không tự tắt."
                )
                return None, now
            logger.warning("[SERVER] Không nhận được heartbeat từ Frontend quá 90s. Đang tự động tắt Backend...")
            return "tat", last_heartbeat
        return None, last_heartbeat
