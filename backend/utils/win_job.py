"""Windows Job Object: tiến trình con CHẾT THEO backend, kể cả khi backend bị giết thô.

VÌ SAO CẦN: Qwen3-TTS chạy trong một tiến trình riêng (``tts-server``) giữ ~1–5 GB RAM
/ VRAM. Backend thoát êm thì ``QwenTTSService.stop()`` tắt nó; nhưng backend bị kill
(taskkill /f, crash, Electron đóng mạnh) thì không ai gọi stop -> tiến trình mồ côi giữ
nguyên bộ nhớ tới khi người dùng khởi động lại máy. Gắn con vào một Job Object có cờ
KILL_ON_JOB_CLOSE: handle của job nằm trong backend, backend chết là handle đóng, và
Windows tự giết mọi tiến trình trong job.

Chỉ dùng ctypes (không thêm thư viện). Ngoài Windows / lỗi bất kỳ -> trả False, KHÔNG
ném lỗi: thiếu job chỉ mất lưới an toàn, không được làm hỏng việc sinh giọng.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger("WinJob")

_JOB = None   # giữ handle suốt đời tiến trình backend — đóng là giết cả job


def _job_handle():
    global _JOB
    if _JOB is not None:
        return _JOB
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = JobObjectExtendedLimitInformation
    if not ok:
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    _JOB = job
    return job


def attach_kill_on_close(pid: int) -> bool:
    """Gắn tiến trình ``pid`` vào job chung. True nếu gắn được."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        h = k32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not h:
            raise OSError(ctypes.get_last_error(), "OpenProcess")
        try:
            if not k32.AssignProcessToJobObject(_job_handle(), h):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        finally:
            k32.CloseHandle(h)
        return True
    except Exception as e:  # noqa: BLE001 — mất lưới an toàn chứ không hỏng việc chính
        logger.warning(f"Không gắn được tiến trình {pid} vào Job Object (bỏ qua): {e}")
        return False
