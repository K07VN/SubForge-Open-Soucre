"""SSE stream helper cho xuất compositor (tiến trình FFmpeg)."""
from utils.i18n import L
import asyncio
import inspect
import json
import threading
from queue import Queue
from typing import Callable, Optional

from fastapi.responses import StreamingResponse


def compositor_export_sse_response(
    worker: Callable[..., None],
    cancel_event: Optional[threading.Event] = None,
) -> StreamingResponse:
    """Chạy worker trong thread; worker đẩy event dict vào queue, kết thúc bằng None.

    ``cancel_event``: cờ hủy của LƯỢT XUẤT NÀY. Khách ngắt kết nối (bấm Hủy trong app —
    nút đó chỉ ``abort()`` request, đóng tab, F5, app tắt) thì cờ được đặt để ffmpeg
    dừng. Không có cờ này, người dùng bấm Hủy rồi bấm Xuất lại là có HAI ffmpeg chạy
    song song, bấm thêm là ba, bốn — máy chậm gấp bấy nhiêu lần mà không ai nhìn ra
    (bắt được từ Task Manager của người dùng: 4 ffmpeg cùng ~11% CPU, ~600MB mỗi cái).
    Worker nhận cờ qua tham số thứ hai nếu nó khai báo hai tham số; worker cũ một tham
    số vẫn chạy như trước.
    """

    progress_queue: Queue = Queue()
    if cancel_event is None:
        cancel_event = threading.Event()

    def run_worker() -> None:
        try:
            if _worker_nhan_co(worker):
                worker(progress_queue, cancel_event)
            else:
                worker(progress_queue)
        except Exception as exc:  # noqa: BLE001
            progress_queue.put({"type": "error", "message": str(exc)})
        finally:
            progress_queue.put(None)

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()

    async def generator():
        try:
            while True:
                await asyncio.sleep(0.15)
                had_item = False
                while not progress_queue.empty():
                    had_item = True
                    item = progress_queue.get_nowait()
                    if item is None:
                        return
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if not thread.is_alive() and progress_queue.empty():
                    if not had_item:
                        yield (
                            "data: "
                            + json.dumps(
                                {"type": "error", "message": L("Xuất video bị gián đoạn", "Video export was interrupted")},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                    return
        finally:
            #  Vào đây khi luồng kết thúc BẤT KỂ lý do. Worker còn sống nghĩa là khách đã
            #  đi trước (Starlette huỷ generator khi mất kết nối) -> hạ cờ để ffmpeg dừng.
            if thread.is_alive():
                cancel_event.set()

    return StreamingResponse(generator(), media_type="text/event-stream")


def _worker_nhan_co(worker: Callable[..., None]) -> bool:
    """Worker có nhận tham số thứ hai (cờ hủy) không — giữ tương thích worker cũ."""
    try:
        return len(inspect.signature(worker).parameters) >= 2
    except (TypeError, ValueError):
        return False
