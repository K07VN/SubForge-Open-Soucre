"""KHO CHỨNG CHỈ cho mọi lượt tải HTTPS bằng ``urllib`` của backend.

VÌ SAO CẦN (khách báo 28-09-2026, cài GPU Pack):
    <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
     unable to get local issuer certificate>

Tệp tải trên GitHub Release chuyển hướng sang ``release-assets.githubusercontent.com``,
chuỗi chứng chỉ: Let's Encrypt ``YR1`` -> ``Root YR`` -> ``ISRG Root X1``. Python
(``ssl.create_default_context``) chỉ đọc kho gốc CÓ SẴN của Windows — mà Windows nạp gốc
theo kiểu "cần mới tải về", và Chrome dùng kho riêng nên duyệt web không nạp hộ. Máy
chưa từng có ``ISRG Root X1`` trong kho là hỏng đúng câu trên, trình duyệt vẫn tải được.

Cách gỡ: kho Windows (giữ CA của phần mềm diệt virus / proxy công ty cài vào Windows)
CỘNG bộ gốc Mozilla của ``certifi`` (đã đóng gói sẵn trong bản build). Vẫn kiểm chứng
chỉ đầy đủ — chỉ thêm nguồn gốc tin cậy, không tắt kiểm tra.
"""
from utils.i18n import L
import logging
import ssl
import urllib.request
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def ngu_canh_ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception as e:  # noqa: BLE001
        logger.warning("SSL: không nạp được bộ gốc certifi (%s) — chỉ dùng kho Windows.", e)
    return ctx


def cai_cho_urllib() -> None:
    """Gắn ngữ cảnh trên làm mặc định cho ``urllib.request.urlopen``/``urlretrieve``
    toàn tiến trình (gọi một lần lúc khởi động)."""
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ngu_canh_ssl()))
    urllib.request.install_opener(opener)


def la_loi_chung_chi(e: BaseException) -> bool:
    s = f"{e!r} {e}".upper()
    return "CERTIFICATE_VERIFY_FAILED" in s or "SSLCERTVERIFICATIONERROR" in s


def trang_release(url: str) -> str:
    """``…/releases/download/<tag>/<tệp>`` -> ``…/releases/tag/<tag>`` (trang để tải tay)."""
    if "/releases/download/" in url:
        dau, sau = url.split("/releases/download/", 1)
        return f"{dau}/releases/tag/{sau.split('/', 1)[0]}"
    return url


def huong_dan_loi_chung_chi(url: str) -> str:
    """Hướng dẫn gỡ lỗi SSL khi tải GPU Pack (câu cho người dùng, không phải log)."""
    return (
        L("Máy chưa xác thực được chứng chỉ bảo mật của GitHub (lỗi SSL). Thử lần lượt: "
        "(1) Chỉnh đúng ngày giờ Windows (Cài đặt → Thời gian & ngôn ngữ → bật \"Đặt giờ tự "
        "động\"); (2) Tắt tạm tính năng quét HTTPS/SSL của phần mềm diệt virus (Kaspersky, "
        "Avast, ESET, Bitdefender…), tắt VPN/proxy rồi Thử lại; (3) Chạy Windows Update; "
        f"(4) Hoặc TẢI TAY: mở {trang_release(url)} bằng trình duyệt, tải tệp .manifest.json "
        "và TẤT CẢ tệp .partNN.zip vào CÙNG một thư mục, rồi bấm \"Chọn file zip máy\" và "
        "chọn tệp .manifest.json.", f"This machine could not verify GitHub's security certificate (SSL error). Try in order: (1) Set the Windows date and time correctly (Settings → Time & language → turn on \"Set time automatically\"); (2) Temporarily turn off the HTTPS/SSL scanning of your antivirus (Kaspersky, Avast, ESET, Bitdefender…), turn off VPN/proxy, then Retry; (3) Run Windows Update; (4) Or DOWNLOAD MANUALLY: open {trang_release(url)} in a browser, download the .manifest.json file and ALL the .partNN.zip files into the SAME folder, then click \"Choose zip file from computer\" and select the .manifest.json file.")
    )
