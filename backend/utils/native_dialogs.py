"""Hộp thoại chọn file/thư mục native (tkinter) — chạy trên MỘT thread chuyên trách.

Tcl/Tk gắn chặt với thread đã khởi tạo nó. Pattern cũ (tạo tk.Tk() bên trong
run_in_threadpool của FastAPI) khiến mỗi lần mở dialog có thể rơi vào một worker
thread khác nhau -> lần gọi sau (hoặc lúc GC đụng object Tk cũ) nổ lỗi
"main thread is not in main loop" (đã ghi nhận trên máy khách, 2026-07-26).

Giải pháp: mọi thao tác tkinter đi qua một thread daemon DUY NHẤT, tuần tự hóa
bằng queue. Caller (endpoint FastAPI) vẫn bọc bằng run_in_threadpool để không
chặn event loop trong lúc người dùng đang mở hộp thoại.
"""
import logging
import queue
import threading

logger = logging.getLogger("NativeDialogs")

_req_q: "queue.Queue" = queue.Queue()
_worker: threading.Thread = None
_worker_lock = threading.Lock()

# Người dùng có thể để hộp thoại mở rất lâu — không đặt timeout ngắn; đây chỉ là
# chốt an toàn để request không treo vô hạn nếu thread dialog chết bất thường.
_DIALOG_TIMEOUT_SECS = 15 * 60


def _worker_loop():
    while True:
        func, res_q = _req_q.get()
        try:
            res_q.put((True, func()))
        except BaseException as e:  # noqa: BLE001 — trả lỗi về caller, giữ thread sống
            res_q.put((False, e))


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_worker_loop, daemon=True, name="native-dialog"
            )
            _worker.start()


def _run_on_dialog_thread(func):
    """Chạy func trên thread dialog chuyên trách, chờ và trả kết quả (hoặc raise)."""
    _ensure_worker()
    res_q: "queue.Queue" = queue.Queue()
    _req_q.put((func, res_q))
    ok, val = res_q.get(timeout=_DIALOG_TIMEOUT_SECS)
    if not ok:
        raise val
    return val


def _make_root():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def ask_open_filename(title: str, filetypes) -> str:
    """Mở hộp thoại chọn file, trả đường dẫn ('' nếu người dùng hủy)."""

    def _do():
        from tkinter import filedialog

        root = _make_root()
        try:
            return filedialog.askopenfilename(title=title, filetypes=filetypes)
        finally:
            root.destroy()

    return _run_on_dialog_thread(_do)


def ask_open_filenames(title: str, filetypes) -> list:
    """Mo hop thoai chon NHIEU file, tra danh sach duong dan ([] neu huy).

    Them moi (khong sua ask_open_filename) de kho media cua Review nang cao nhap duoc
    ca me tep trong mot lan — giong CapCut.
    """

    def _do():
        from tkinter import filedialog

        root = _make_root()
        try:
            res = filedialog.askopenfilenames(title=title, filetypes=filetypes)
            # Tkinter tra ve tuple, hoac chuoi kieu "{a} {b}" tren mot so ban Windows.
            if isinstance(res, str):
                return list(root.tk.splitlist(res)) if res else []
            return list(res or [])
        finally:
            root.destroy()

    return _run_on_dialog_thread(_do)


def ask_directory(title: str) -> str:
    """Mở hộp thoại chọn thư mục, trả đường dẫn ('' nếu người dùng hủy)."""

    def _do():
        from tkinter import filedialog

        root = _make_root()
        try:
            return filedialog.askdirectory(title=title)
        finally:
            root.destroy()

    return _run_on_dialog_thread(_do)
