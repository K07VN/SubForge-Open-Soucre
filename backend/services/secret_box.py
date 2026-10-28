# -*- coding: utf-8 -*-
"""KÉT BÍ MẬT — mã hoá chuỗi nhạy cảm bằng DPAPI của Windows.

DÙNG CHO: refresh token của YouTube, client secret của Google. Đây là những thứ
CÓ QUYỀN ĐĂNG VIDEO lên kênh người dùng — khác hẳn API key DeepSeek (cùng lắm là
tốn tiền người khác). Chép được nó là đăng được video lên kênh của họ.

VÌ SAO DPAPI CHỨ KHÔNG PHẢI MỘT KHOÁ TRONG MÃ NGUỒN: khoá nhúng trong app thì
nằm trong mọi bản cài, ai cũng moi ra được — mã hoá kiểu đó chỉ làm người ta yên
tâm chứ không bảo vệ gì. DPAPI lấy khoá từ chính TÀI KHOẢN WINDOWS đang đăng
nhập, nên tệp chép sang máy khác (hoặc sang tài khoản Windows khác trên cùng
máy) là giải mã không ra.

VÌ SAO KHÔNG DÙNG THƯ VIỆN: ``ctypes`` gọi thẳng crypt32.dll, không thêm phụ
thuộc nào vào bản đóng gói PyInstaller (installer đã 427 MB).

NGOÀI WINDOWS (chỉ gặp khi chạy dev trên máy khác): KHÔNG âm thầm lưu trần.
``ma_hoa`` ném lỗi — thà chặn còn hơn để token nằm trần mà người dùng tưởng đã
được mã hoá.
"""
from utils.i18n import L
import base64
import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger("SecretBox")

#: Tiền tố đánh dấu chuỗi đã qua DPAPI. Có nó thì biết ngay dữ liệu cũ/mới khi
#: đọc lên, khỏi phải đoán theo độ dài hay thử giải mã rồi bắt lỗi.
TIEN_TO = "dpapi:v1:"


class KetBiMatError(RuntimeError):
    pass


if sys.platform == "win32":

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _blob(data: bytes) -> _BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _doc_blob(blob: _BLOB) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            #  DPAPI cấp bộ nhớ bằng LocalAlloc; không trả lại là rò mỗi lần gọi.
            _kernel32.LocalFree(blob.pbData)


def ma_hoa(chuoi: str) -> str:
    """Mã hoá một chuỗi. Trả về dạng ``dpapi:v1:<base64>``.

    Chuỗi rỗng trả về rỗng — không có gì để giấu thì không tạo rác.
    """
    if not chuoi:
        return ""
    if sys.platform != "win32":
        raise KetBiMatError(
            L("Chỉ mã hoá được trên Windows (DPAPI). Không lưu token ở dạng trần.", "Encryption is only possible on Windows (DPAPI). The token is not stored in plain form.")
        )
    vao = _blob(chuoi.encode("utf-8"))
    ra = _BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(vao), None, None, None, None, 0, ctypes.byref(ra)
    )
    if not ok:
        raise KetBiMatError(f"CryptProtectData thất bại (mã {ctypes.GetLastError()})")
    return TIEN_TO + base64.b64encode(_doc_blob(ra)).decode("ascii")


def giai_ma(chuoi: str) -> str:
    """Giải mã chuỗi do ``ma_hoa`` tạo ra. Trả rỗng nếu không giải được.

    KHÔNG ném lỗi khi giải thất bại: ca hay gặp nhất là người dùng cài lại
    Windows hoặc đổi tài khoản — lúc đó token cũ vĩnh viễn không đọc được, và
    việc đúng phải làm là bảo họ kết nối lại kênh, không phải làm app chết.
    """
    if not chuoi:
        return ""
    if not chuoi.startswith(TIEN_TO):
        #  Dữ liệu chưa qua két (bản cũ hoặc ai đó sửa tay tệp DB).
        return chuoi
    if sys.platform != "win32":
        return ""
    try:
        raw = base64.b64decode(chuoi[len(TIEN_TO):])
    except Exception:
        logger.warning("[két] Chuỗi mã hoá hỏng định dạng base64 — bỏ qua.")
        return ""
    vao = _blob(raw)
    ra = _BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(vao), None, None, None, None, 0, ctypes.byref(ra)
    )
    if not ok:
        logger.warning(
            "[két] Không giải mã được (thường do đổi tài khoản Windows hoặc cài "
            "lại máy) — cần kết nối lại kênh."
        )
        return ""
    return _doc_blob(ra).decode("utf-8", errors="replace")


def che(chuoi: str, giu_dau: int = 4, giu_cuoi: int = 4) -> str:
    """Rút gọn để HIỆN RA MÀN HÌNH / ghi nhật ký: ``1234…abcd``.

    Mọi chỗ log hay trả về API phải đi qua đây. Một lần lỡ tay in nguyên token
    vào nhật ký là nó nằm trong tệp log, rồi theo báo cáo lỗi gửi lên server.
    """
    s = chuoi or ""
    if len(s) <= giu_dau + giu_cuoi:
        return "…" if s else ""
    return f"{s[:giu_dau]}…{s[-giu_cuoi:]}"
