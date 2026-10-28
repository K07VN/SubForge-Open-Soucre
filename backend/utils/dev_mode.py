import os
import sys


def _is_packaged() -> bool:
    """Bản đóng gói PyInstaller (phát hành cho người dùng cuối)."""
    return bool(getattr(sys, "frozen", False))


def is_dev_mode() -> bool:
    """Chế độ dev local — bỏ qua gọi Admin Portal để xác thực bản quyền.

    BẢO MẬT (quan trọng): trong BẢN ĐÓNG GÓI, hàm này LUÔN trả False và KHÔNG
    đọc bất kỳ biến môi trường nào. Trước đây bản phát hành vẫn tôn trọng
    SUBFORGE_SKIP_LICENSE_VERIFY / SUBFORGE_DEV, mà Electron spawn backend với
    `env: {...process.env}` nên biến do NGƯỜI DÙNG đặt đi thẳng xuống đây —
    `set SUBFORGE_SKIP_LICENSE_VERIFY=1` là mở khoá toàn bộ app, không cần sửa
    một byte nào trong bản build. Tên biến lại nằm nguyên văn trong bytecode của
    subforge_backend.exe (PyInstaller không biên dịch sang mã máy) nên `strings`
    là đọc ra được — tức lỗ hổng này tự quảng cáo chính nó.
    Dev vẫn dùng được các biến dưới đây vì khi chạy từ source thì không frozen.
    """
    if _is_packaged():
        return False

    if os.environ.get("SUBFORGE_FORCE_LICENSE_VERIFY", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("SUBFORGE_SKIP_LICENSE_VERIFY", "").lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("SUBFORGE_DEV", "").lower() in ("1", "true", "yes"):
        return True
    # Backend chạy trực tiếp (uvicorn reload / py main.py), không phải bản PyInstaller
    return True
