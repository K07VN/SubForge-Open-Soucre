"""HÀNG ĐỢI VIỆC NẶNG — mỗi lúc chỉ MỘT việc nặng chạy trên máy.

VÌ SAO CẦN: nhận dạng lời thoại (Whisper), tách giọng (Demucs), sinh giọng và dựng
video đều ăn hết CPU/GPU/RAM. Trước đây mỗi thứ chỉ tự khoá THEO DỰ ÁN, nên chạy
một lô 20 video rồi mở dự án khác ra tách giọng là hai việc nặng cùng lúc: máy
không có GPU thì treo cứng, máy có GPU thì hết VRAM và cả hai cùng hỏng. Người
dùng chỉ thấy "app đơ / app sập" mà không hiểu vì sao.

CÁCH LÀM: một "chỗ chạy" duy nhất. Ai tới trước chạy trước (FIFO), người sau XẾP
HÀNG chứ không bị từ chối — bấm xong cứ để đó, tới lượt nó tự chạy.

Dùng khoá của THREADING (không phải asyncio) vì các việc nặng đều chạy trong luồng
phụ (BackgroundTasks của Starlette, ``run_in_threadpool``). Bên async thì xin chỗ
qua ``run_in_threadpool`` để không chặn vòng lặp sự kiện.

BẪY PHẢI TRÁNH — kẹt cứng (deadlock):
  · Chỉ xin chỗ ở NGOÀI CÙNG một việc. Hàm đã giữ chỗ mà gọi tiếp hàm cũng xin chỗ
    là tự khoá chính mình -> dùng ``_giu_cho`` (biến theo luồng) để lần xin thứ hai
    trong cùng luồng đi thẳng qua.
  · Thứ tự khoá luôn là: CHỖ CHẠY trước, khoá dự án sau. Ngược nhau ở hai chỗ khác
    nhau là kẹt.
  · Chờ có thể HUỶ: người dùng bấm Huỷ lúc đang xếp hàng thì phải thoát ngay, không
    thì họ ngồi chờ một việc mình đã huỷ.
"""

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Số việc nặng chạy song song. 1 = chạy lần lượt. Để >1 chỉ khi máy thật khoẻ —
#: hai lượt Demucs cùng lúc trên máy không GPU là đủ treo.
SUC_CHUA = 1

_lock = threading.Lock()
_cv = threading.Condition(_lock)
#: Đang chạy: {ma: {"project_id", "nhan", "tu"}}
_dang_chay: dict = {}
#: Xếp hàng, THEO THỨ TỰ tới trước chạy trước.
_hang: list = []
#: Luồng này đã giữ chỗ chưa (tránh tự khoá chính mình khi hàm nọ gọi hàm kia).
_giu_cho = threading.local()


class WorkCancelled(Exception):
    """Người dùng huỷ trong lúc còn đang xếp hàng."""


def _vi_tri(ma: str) -> int:
    """Đứng thứ mấy trong hàng (1 = kế tiếp). 0 = không còn trong hàng."""
    for i, x in enumerate(_hang):
        if x["ma"] == ma:
            return i + 1
    return 0


@contextmanager
def slot(project_id: str = "", nhan: str = "việc nặng",
         cancelled: Optional[Callable[[], bool]] = None):
    """Giữ chỗ chạy. Chưa tới lượt thì ĐỢI (không từ chối).

    ``cancelled`` — hàm trả True khi người dùng đã bấm huỷ; đang đợi mà huỷ thì
    thoát bằng ``WorkCancelled``.
    """
    #  Đã giữ chỗ ở tầng ngoài (vd bước của lô gọi lại đúng hàm mà nút bấm gọi) thì
    #  ĐI THẲNG — xin chỗ lần hai trong cùng một luồng là tự khoá chính mình.
    if getattr(_giu_cho, "co", False):
        yield {"reentrant": True}
        return

    ma = uuid.uuid4().hex[:8]
    muc = {"ma": ma, "project_id": project_id, "nhan": nhan, "tu": time.time()}
    with _cv:
        _hang.append(muc)
        da_bao = False
        while len(_dang_chay) >= SUC_CHUA or (_hang and _hang[0]["ma"] != ma):
            if cancelled and cancelled():
                _hang[:] = [x for x in _hang if x["ma"] != ma]
                _cv.notify_all()
                raise WorkCancelled()
            if not da_bao and _dang_chay:
                dang = next(iter(_dang_chay.values()))
                logger.info(
                    f"[HÀNG ĐỢI] '{nhan}' xếp hàng (thứ {_vi_tri(ma)}) — "
                    f"đang chạy: {dang['nhan']}"
                )
                da_bao = True
            #  Đợi có thời hạn để còn kiểm cờ huỷ, không ngủ mãi.
            _cv.wait(0.5)
        _hang[:] = [x for x in _hang if x["ma"] != ma]
        muc["bat_dau"] = time.time()
        _dang_chay[ma] = muc

    _giu_cho.co = True
    try:
        yield muc
    finally:
        _giu_cho.co = False
        with _cv:
            _dang_chay.pop(ma, None)
            _cv.notify_all()


async def slot_async(project_id: str = "", nhan: str = "việc nặng",
                     cancelled: Optional[Callable[[], bool]] = None):
    """Bản cho đường async: xin chỗ trong luồng phụ để KHÔNG chặn vòng lặp sự kiện.

    Dùng như:  ``async with slot_async(...):``
    """
    from fastapi.concurrency import run_in_threadpool

    class _Ctx:
        def __init__(self):
            self._cm = None

        async def __aenter__(self):
            self._cm = slot(project_id, nhan, cancelled)
            return await run_in_threadpool(self._cm.__enter__)

        async def __aexit__(self, *exc):
            return await run_in_threadpool(self._cm.__exit__, *exc)

    return _Ctx()


def trang_thai() -> dict:
    """Ảnh chụp hàng đợi cho giao diện."""
    with _lock:
        return {
            "suc_chua": SUC_CHUA,
            "dang_chay": [
                {"project_id": x["project_id"], "nhan": x["nhan"],
                 "giay": round(time.time() - x.get("bat_dau", x["tu"]), 1)}
                for x in _dang_chay.values()
            ],
            "cho": [
                {"project_id": x["project_id"], "nhan": x["nhan"],
                 "thu": i + 1, "giay": round(time.time() - x["tu"], 1)}
                for i, x in enumerate(_hang)
            ],
        }


def dang_ban() -> bool:
    with _lock:
        return bool(_dang_chay)


def viec_dang_chay(tru_luong_nay: bool = True):
    """Việc nặng ĐANG chạy trên máy — ``None`` nếu rảnh.

    Dùng để CHẶN THẲNG ở cửa endpoint: chốt với chủ dự án (2026-09-17) là bấm việc
    nặng lúc máy đang bận thì báo "có việc nặng khác đang chạy" và thôi, chứ không
    xếp hàng. Xếp hàng thì người dùng bấm xong không thấy gì nhúc nhích, lại phải
    dựng thêm giao diện quản lý hàng đợi (xem cái gì đang đợi, huỷ cái nào) — phức
    tạp hơn hẳn cái lợi.

    ``tru_luong_nay``: bỏ qua chỗ do CHÍNH luồng đang hỏi giữ. Bộ chạy lô gọi vào
    đây giữa các bước của nó, nếu tính cả chỗ của chính mình thì lô tự chặn lô.
    """
    #  Luồng này đang GIỮ CHỖ sẵn -> coi như rảnh với chính nó (hàm nọ gọi hàm kia
    #  trong cùng một việc thì không được tự chặn mình).
    if tru_luong_nay and getattr(_giu_cho, "co", False):
        return None
    with _lock:
        for x in _dang_chay.values():
            return {"project_id": x.get("project_id", ""), "nhan": x.get("nhan", ""),
                    "giay": round(time.time() - x.get("bat_dau", x["tu"]), 1)}
        return None
