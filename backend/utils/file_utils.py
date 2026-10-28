import os
import shutil
import time
import logging

logger = logging.getLogger("FileUtils")


def safe_replace(src: str, dst: str, max_retries: int = 8, delay: float = 0.15) -> bool:
    """Thay thế tệp an toàn trên Windows khi tệp đích có thể đang bị khóa mở bởi
    process khác (vd: browser audio element, Electron preview player, hoặc thread khác).
    """
    if not os.path.exists(src):
        return False
    dst_dir = os.path.dirname(dst)
    if dst_dir:
        os.makedirs(dst_dir, exist_ok=True)

    for attempt in range(max_retries):
        try:
            os.replace(src, dst)
            return True
        except OSError as e:
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                # Nếu os.replace bị cản do WinError 32 (file lock), copy trực tiếp nội dung đè lên dst
                try:
                    with open(src, "rb") as sf, open(dst, "wb") as df:
                        shutil.copyfileobj(sf, df)
                    try:
                        os.remove(src)
                    except OSError:
                        pass
                    return True
                except OSError as err:
                    logger.error(f"safe_replace thất bại từ '{src}' -> '{dst}': {err}")
                    raise err
    return False


def safe_remove(path: str, max_retries: int = 4, delay: float = 0.1) -> bool:
    """Xóa tệp an toàn trên Windows, thử lại nếu đang bị khóa thoáng qua."""
    if not os.path.exists(path):
        return True
    for attempt in range(max_retries):
        try:
            os.remove(path)
            return True
        except OSError:
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
    return False
