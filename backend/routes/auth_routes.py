# Decompiled auth functions verified against shipped PYZ; unchanged portions verified from loose source.
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
import urllib.request
import urllib.error
import json
import logging
import platform
import socket
import sys
import time
import os
from services.config_service import get_config_service
from utils.dev_mode import is_dev_mode
from utils.i18n import L

logger = logging.getLogger("AuthRoutes")
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Cache state for license verification.
# TTL 30 phút + DÙNG CHUNG cho get_auth_status LẪN verify_license → cắt mạnh số lần
# gọi Admin Portal (Vercel) + query Supabase. Với hạ tầng FREE + 200-300 user, không
# thể verify online mỗi tác vụ / mỗi 2 phút. Đánh đổi: key bị thu hồi còn dùng được
# tối đa ~30 phút (chấp nhận vì chưa cần real-time). Chỉ cache kết quả HỢP LỆ.
LAST_VERIFIED_TIME = 0.0
CACHE_DURATION_SECS = float(os.environ.get("SUBFORGE_LICENSE_CACHE_SECS", "1800"))  # 30'
_LAST_VERIFY_RESULT = None
_LAST_VERIFY_KEY = None

# PHIÊN BẢN APP — nguồn tin cậy là biến môi trường Electron truyền vào lúc spawn
# backend (SUBFORGE_APP_VERSION = app.getVersion()). Trước đây backend chỉ biết
# phiên bản qua query/header của frontend, mà phần lớn request KHÔNG gửi, nên mọi
# thứ gửi lên server đều mang "1.0.0" — sai và vô dụng khi truy lỗi theo bản phát
# hành. Header của frontend chỉ còn là đường dự phòng cho bản chạy trong trình duyệt.
_APP_VERSION_ENV = (os.environ.get("SUBFORGE_APP_VERSION") or "").strip()
_LAST_CLIENT_VERSION = ""


def remember_client_version(v: str) -> None:
    """Ghi nhớ phiên bản do frontend khai (dự phòng khi không có env)."""
    global _LAST_CLIENT_VERSION
    v = (v or "").strip()
    if v and v != "1.0.0":
        _LAST_CLIENT_VERSION = v


def get_app_version() -> str:
    """Phiên bản app dùng cho MỌI thứ gửi lên server (xác thực + log + thống kê)."""
    return _APP_VERSION_ENV or _LAST_CLIENT_VERSION or "unknown"


def _verify_cached(api_key: str, device_id: str, device_info: str, admin_url: str, client_version: str = "1.0.0") -> dict:
    """Xác thực có CACHE: trả kết quả hợp lệ đã cache nếu còn hạn → tránh gọi Admin
    (Vercel) + Supabase mỗi lần. Chỉ cache kết quả valid; invalid/lỗi mạng luôn gọi lại."""
    global LAST_VERIFIED_TIME, _LAST_VERIFY_RESULT, _LAST_VERIFY_KEY
    now = time.time()
    if (
        _LAST_VERIFY_RESULT is not None
        and _LAST_VERIFY_KEY == api_key
        and (now - LAST_VERIFIED_TIME) < CACHE_DURATION_SECS
    ):
        return _LAST_VERIFY_RESULT
    res = verify_key_on_server(
        api_key, device_id, device_info, admin_url, is_activation=False, client_version=client_version
    )
    if res.get("valid"):
        _LAST_VERIFY_RESULT = res
        _LAST_VERIFY_KEY = api_key
        LAST_VERIFIED_TIME = now
    return res

class ActivateRequest(BaseModel):
    key: str
    admin_url: str = None  # Cho phép cấu hình admin url từ UI kích hoạt
    client_version: str = "1.0.0"

def get_device_info() -> str:
    """Thu thập thông tin chi tiết hệ điều hành và cấu hình máy gửi lên Server"""
    try:
        info = {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "processor": platform.processor() or "Unknown CPU"
        }
        return json.dumps(info, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Failed to gather device info: {str(e)}"})

# Cache thống kê: get_client_stats() os.walk TOÀN BỘ review root — nếu gọi mỗi lần
# verify online (mỗi tác vụ nặng STT/TTS/merge…) sẽ quét đĩa lặp lại, thêm độ trễ cho
# mỗi bước. Stats chỉ để BÁO CÁO lên admin nên cũ vài chục giây là chấp nhận được.
_STATS_CACHE: dict = {}
_STATS_CACHE_TIME = 0.0
_STATS_CACHE_TTL = 60.0  # giây


def get_client_stats() -> dict:
    """Thu thập thống kê sử dụng cục bộ (có cache TTL): số video lồng tiếng, review
    và số câu voice sinh ra.

    QUAN TRỌNG: con số trả về là TÍCH LŨY qua sổ cái (usage_ledger) — tính cả dự án
    user đã XÓA cục bộ, và KHÔNG tụt xuống khi xóa. Ở đây chỉ thu thập ID HIỆN CÓ
    rồi giao cho sổ cái đối chiếu với lần quét trước (thêm ID mới, giữ nguyên count).
    """
    global _STATS_CACHE, _STATS_CACHE_TIME
    now = time.time()
    if _STATS_CACHE and (now - _STATS_CACHE_TIME) < _STATS_CACHE_TTL:
        return _STATS_CACHE

    from models.database import SessionLocal
    from models.models import Video, SubtitleSegment

    dubbed_ids: list = []
    dubbing_voice_ids: list = []
    review_ids: list = []
    review_voice_ids: list = []

    # ID video lồng tiếng & ID câu lồng tiếng đã sinh voice
    db = SessionLocal()
    try:
        dubbed_ids = [row[0] for row in db.query(Video.id).all()]
        dubbing_voice_ids = [
            f"{vid}:{sid}"
            for vid, sid in db.query(
                SubtitleSegment.video_id, SubtitleSegment.id
            ).filter(SubtitleSegment.audio_path != None).all()
        ]
    except Exception as e:
        logger.warning(f"Error querying local stats: {e}")
    finally:
        db.close()

    # ID video review & ID câu review đã sinh voice (khóa theo đường dẫn tương đối)
    try:
        from services.config_service import get_config_service
        config_service = get_config_service()
        review_root = config_service.get_review_root()
        if review_root and os.path.exists(review_root):
            for pid in os.listdir(review_root):
                pdir = os.path.join(review_root, pid)
                if not os.path.isdir(pdir):
                    continue
                review_ids.append(pid)
                for root, dirs, files in os.walk(pdir):
                    for f in files:
                        if f == "audio_tts.wav":
                            rel = os.path.relpath(os.path.join(root, f), review_root)
                            review_voice_ids.append(rel)
    except Exception as e:
        logger.warning(f"Error walking review root: {e}")

    # Đối chiếu với sổ cái → tổng tích lũy (đã tính cả cái đã xóa).
    try:
        from services.usage_ledger import update_and_get
        result = update_and_get(
            dubbed_ids=dubbed_ids,
            dubbing_voice_ids=dubbing_voice_ids,
            review_ids=review_ids,
            review_voice_ids=review_voice_ids,
        )
    except Exception as e:
        # Sổ cái lỗi → fallback về đếm trực tiếp để vẫn có số (không chặn verify).
        logger.warning(f"Usage ledger lỗi, dùng đếm trực tiếp: {e}")
        result = {
            "dubbed_videos": len(dubbed_ids),
            "review_videos": len(review_ids),
            "generated_voices": len(dubbing_voice_ids) + len(review_voice_ids),
        }

    _STATS_CACHE = result
    _STATS_CACHE_TIME = now
    return result

#: Quyền MẶC ĐỊNH khi không hỏi được server (offline, bản admin cũ chưa biết gói).
#:
#:  Cố ý MỞ chứ không khoá: người đã trả tiền mà mất mạng vài phút thì không được
#:  phép mất tính năng. Cửa chặn thật nằm ở server — chạy offline mãi thì lượt
#:  xác thực sau sẽ chặn.
#:  NGOẠI LỆ ``allow_sang_tao`` (chế độ Sáng tạo): ẨN MẶC ĐỊNH, admin cấp theo từng key —
#:  không biết thì coi là KHÔNG có quyền (xem :func:`_quyen_sang_tao_cuoi` cho trường hợp mất mạng).
_QUYEN_MAC_DINH = {"plan": "", "plan_label": "", "allow_batch": True, "allow_sang_tao": False,
                   "quota": None, "quota_message": ""}


def _phan_quyen(verification: dict) -> dict:
    """Rút phần GÓI & HẠN MỨC từ phản hồi của server, có mặc định an toàn."""
    if not verification or "allow_batch" not in verification:
        return dict(_QUYEN_MAC_DINH)
    return {
        "plan": verification.get("plan") or "",
        "plan_label": verification.get("plan_label") or "",
        "allow_batch": bool(verification.get("allow_batch", True)),
        "allow_sang_tao": verification.get("allow_sang_tao") is True,
        "quota": verification.get("quota"),
        "quota_message": verification.get("quota_message") or "",
    }




def _quyen_sang_tao_cuoi(api_key: str) -> bool:
    """Quyền Sáng tạo theo lần xác thực THÀNH CÔNG gần nhất của CHÍNH key này (hết hạn cache vẫn dùng) — để mất mạng / máy chủ chập chờn vài phút không làm người ĐÃ được cấp
    quyền mất tính năng giữa chừng. Key chưa từng được xác thực trong lần chạy này (vừa mở app lúc offline) thì không có quyền."""
    return _LAST_VERIFY_RESULT is not None and _LAST_VERIFY_KEY == api_key and _LAST_VERIFY_RESULT.get("allow_sang_tao") is True


def quyen_hien_tai() -> dict:
    """Quyền của key ĐANG dùng — cho các route cần chặn tính năng theo gói.

    Đọc từ CACHE xác thực (``_verify_cached``) nên không gọi mạng thêm; cache hết
    hạn thì chính lượt gọi này làm mới nó.
    """
    if is_dev_mode():
        return {**_QUYEN_MAC_DINH, "allow_sang_tao": True}            # chạy từ mã nguồn: không có key, dev dùng đủ mọi chế độ
    cs = get_config_service()
    admin_url = cs.get_admin_url()
    api_key = cs.get_api_key()
    if not admin_url or not api_key:
        return dict(_QUYEN_MAC_DINH)
    try:
        v = _verify_cached(api_key, cs.get_device_id(), get_device_info(), admin_url)
    except Exception:  # noqa: BLE001 — hỏi không được thì cứ cho chạy (xem trên)
        return {**_QUYEN_MAC_DINH, "allow_sang_tao": _quyen_sang_tao_cuoi(api_key)}
    if v.get("network_error"):
        return {**_QUYEN_MAC_DINH, "allow_sang_tao": _quyen_sang_tao_cuoi(api_key)}
    return _phan_quyen(v)


def verify_key_on_server(key: str, device_id: str, device_info: str, admin_url: str, is_activation: bool = False, client_version: str = "1.0.0") -> dict:
    """Gọi HTTP request lên Admin Web Portal để xác thực API Key kèm theo Device ID & Info"""
    if not admin_url:
        return {"valid": False, "message": L("Địa chỉ Admin Server chưa được cấu hình.", "Admin Server address is not configured.")}
    url = f"{admin_url.rstrip('/')}/api/verify-key"
    stats = get_client_stats()
    payload = json.dumps({
        "key": key, "device_id": device_id, "device_info": device_info,
        "is_activation": is_activation, "client_version": client_version, "stats": stats,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "SubForge-Desktop-Client"}
    desktop_secret = os.environ.get("SUBFORGE_DESKTOP_SECRET")
    if desktop_secret:
        headers["X-SubForge-Secret"] = desktop_secret
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode("utf-8")
            data = json.loads(res_body)
            data["status_code"] = response.status
            return data
    except urllib.error.HTTPError as e:
        res_body = e.read().decode("utf-8")
        try:
            data = json.loads(res_body)
            data["status_code"] = e.code
            return data
        except:
            return {"valid": False, "message": L(f"Lỗi HTTP {e.code}", f"HTTP error {e.code}"), "status_code": e.code}
    except Exception as e:
        logger.error(f"Lỗi kết nối tới server verify: {e}")
        return {"valid": False, "message": L(f"Không thể kết nối đến máy chủ xác thực: {str(e)}", f"Cannot connect to the verification server: {str(e)}"), "network_error": True}



def unbind_key_on_server(key: str, device_id: str, admin_url: str) -> dict:
    """Gọi HTTP request lên Admin Web Portal để hủy liên kết thiết bị"""
    if not admin_url:
        return {"success": False, "message": L("Không tìm thấy Admin URL", "Admin URL not found")}
        
    url = f"{admin_url.rstrip('/')}/api/deactivate-key"
    payload = json.dumps({"key": key, "device_id": device_id}).encode("utf-8")
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "SubForge-Desktop-Client",
    }
    desktop_secret = os.environ.get("SUBFORGE_DESKTOP_SECRET")
    if desktop_secret:
        headers["X-SubForge-Secret"] = desktop_secret
    
    req = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode("utf-8")
            return json.loads(res_body)
    except Exception as e:
        logger.error(f"Lỗi khi hủy liên kết thiết bị trên server: {e}")
        return {"success": False, "message": str(e)}

@router.get("/status")
async def get_auth_status(request: Request):
    config_service = get_config_service()
    admin_url = config_service.get_admin_url()
    remember_client_version(request.query_params.get("client_version") or request.headers.get("x-app-version") or "")
    client_version = get_app_version()
    if is_dev_mode():
        return {
            "activated": True,
            "key": config_service.get_api_key() or "dev-mode",
            "description": L("Chế độ phát triển — bỏ qua kiểm tra bản quyền trên server", "Development mode — license check on the server is skipped"),
            "admin_url": admin_url or "http://localhost:3000",
            "dev_mode": True,
            "allow_sang_tao": True,
        }
    api_key = config_service.get_api_key()
    device_id = config_service.get_device_id()
    device_info = get_device_info()
    if not admin_url:
        return {
            "activated": False,
            "message": L("Admin Server chưa được cấu hình ở Backend (Thiếu biến môi trường SUBFORGE_ADMIN_URL).", "Admin Server is not configured in the Backend (missing environment variable SUBFORGE_ADMIN_URL)."),
            "admin_url": "",
        }
    if not api_key:
        return {"activated": False, "message": L("Ứng dụng chưa được kích hoạt.", "The app has not been activated."), "admin_url": admin_url}
    verification = _verify_cached(api_key, device_id, device_info, admin_url, client_version=client_version)
    if verification.get("valid"):
        return {
            "activated": True,
            "key": api_key,
            "description": verification.get("description", ""),
            "owner_name": verification.get("description") or "",
            "email": verification.get("email") or "",
            "phone": verification.get("phone") or "",
            "is_trial": bool(verification.get("is_trial")),
            "admin_url": admin_url,
            "created_at": verification.get("created_at"),
            "expires_at": verification.get("expires_at"),
            **_phan_quyen(verification),
        }
    if verification.get("network_error"):
        return {
            "activated": True,
            "key": api_key,
            "description": L("Chế độ offline tạm thời", "Temporary offline mode"),
            "admin_url": admin_url,
            "allow_sang_tao": _quyen_sang_tao_cuoi(api_key),
        }
    status_code = verification.get("status_code", 400)
    is_invalid = status_code == 401 or "không tồn tại" in verification.get("message", "").lower()
    if is_invalid:
        config_service.update_auth_config(api_key="")
    return {
        "activated": False,
        "key": api_key,
        "message": verification.get("message", L("API Key hết hạn hoặc bị khóa.", "The API Key has expired or is locked.")),
        "admin_url": admin_url,
        "created_at": verification.get("created_at"),
        "expires_at": verification.get("expires_at"),
        "is_active": verification.get("is_active", True),
    }



@router.post("/refresh")
async def refresh_license(request: Request):
    """HỎI LẠI SERVER NGAY về gói & hạn mức, bỏ qua cache 30 phút.

    VÌ SAO PHẢI CÓ: kết quả xác thực được cache ``CACHE_DURATION_SECS`` (30 phút) để
    khỏi gọi Admin mỗi lần. Nhưng người dùng vừa NÂNG GÓI (mua 399k để chạy lô) thì
    app vẫn báo gói cũ suốt tới nửa tiếng — mà nút "Đồng bộ bản quyền" lại bị KHOÁ khi
    ô key đang là chính key đó, nên không còn cách nào bắt app hỏi lại ngoài việc tắt
    app đi mở lại.

    Xoá cache rồi trả về đúng gói tin của ``/status`` — tức mọi nơi đọc quyền
    (``quyen_hien_tai``, cửa chặn chạy lô) đều thấy gói mới ngay.
    """
    global _LAST_VERIFY_RESULT, _LAST_VERIFY_KEY, LAST_VERIFIED_TIME
    _LAST_VERIFY_RESULT = None
    _LAST_VERIFY_KEY = None
    LAST_VERIFIED_TIME = 0.0
    return await get_auth_status(request)


@router.post("/activate")
async def activate_app(req: ActivateRequest):
    config_service = get_config_service()
    device_id = config_service.get_device_id()
    device_info = get_device_info()
    if getattr(sys, "frozen", False):
        admin_url = config_service.get_admin_url()
    else:
        admin_url = req.admin_url or config_service.get_admin_url()
    if not admin_url:
        raise HTTPException(status_code=400, detail=L("Địa chỉ Admin Server chưa được cấu hình ở Backend.", "Admin Server address is not configured in the Backend."))
    if not admin_url.startswith("http"):
        admin_url = "http://" + admin_url
    if is_dev_mode():
        config_service.update_auth_config(api_key=req.key or "dev-mode", admin_url=admin_url)
        return {
            "activated": True,
            "message": L("Dev mode — kích hoạt cục bộ, không gọi server xác thực.", "Dev mode — activated locally, the verification server is not called."),
            "description": L("Chế độ phát triển", "Development mode"),
        }
    remember_client_version(req.client_version)
    verification = verify_key_on_server(req.key, device_id, device_info, admin_url, is_activation=True, client_version=get_app_version())
    if verification.get("valid"):
        config_service.update_auth_config(api_key=req.key, admin_url=admin_url)
        return {
            "activated": True,
            "message": L("Kích hoạt thành công!", "Activation successful!"),
            "description": verification.get("description", ""),
            "created_at": verification.get("created_at"),
            "expires_at": verification.get("expires_at"),
        }
    device_conflict_info = verification.get("device_info")
    if device_conflict_info:
        try:
            info = json.loads(device_conflict_info)
            device_desc = L(f"tên máy '{info.get('hostname', 'Chưa rõ')}' ({info.get('os', 'Hệ điều hành chưa rõ')})", f"machine name '{info.get('hostname', 'Unknown')}' ({info.get('os', 'Unknown OS')})")
        except:
            device_desc = L(f"thông tin '{device_conflict_info}'", f"info '{device_conflict_info}'")
        conflict_msg = L(
            f"Mã API Key đã được sử dụng trên một thiết bị khác ({device_desc}). Để kích hoạt trên thiết bị này, bạn cần vào thiết bị cũ để hủy liên kết khóa (Gỡ bỏ khóa), hoặc liên hệ Quản trị viên (Admin) nếu không thể truy cập thiết bị đó nữa để được hỗ trợ.",
            f"This API Key is already in use on another device ({device_desc}). To activate it on this device, go to the old device and unlink the key (Remove key), or contact the Administrator (Admin) if you can no longer access that device.",
        )
        raise HTTPException(status_code=403, detail=conflict_msg)
    raise HTTPException(status_code=400, detail=verification.get("message", L("API Key không hợp lệ hoặc lỗi kết nối.", "Invalid API Key or connection error.")))



@router.post("/deactivate")
async def deactivate_app():
    config_service = get_config_service()
    api_key = config_service.get_api_key()
    admin_url = config_service.get_admin_url()
    device_id = config_service.get_device_id()

    if not is_dev_mode() and api_key and admin_url:
        unbind_key_on_server(api_key, device_id, admin_url)
        
    config_service.update_auth_config(api_key="")
    return {"activated": False, "message": L("Hủy kích hoạt thành công.", "Deactivated successfully.")}

def verify_license(request: Request):
    if is_dev_mode():
        return
    config_service = get_config_service()
    api_key = config_service.get_api_key()
    if not api_key:
        raise HTTPException(status_code=401, detail=L("Ứng dụng chưa được kích hoạt. Vui lòng nhập API Key để sử dụng chức năng này.", "The app has not been activated. Please enter an API Key to use this feature."))
    admin_url = config_service.get_admin_url()
    device_id = config_service.get_device_id()
    path = request.url.path
    is_expensive_action = any(keyword in path for keyword in (
        '/extract-audio', '/stt', '/translate', '/tts', '/merge', '/export', '/export-compositor', '/render', '/analyze', '/voices', '/separate', '/read-subs',
    ))
    device_info = get_device_info()
    remember_client_version(request.headers.get("x-app-version") or request.query_params.get("client_version") or "")
    verification = _verify_cached(api_key, device_id, device_info, admin_url, client_version=get_app_version())
    if not verification.get("valid"):
        if verification.get("network_error"):
            if is_expensive_action:
                raise HTTPException(status_code=403, detail=L("Không thể kết nối đến máy chủ xác thực để chạy tác vụ xử lý AI này. Vui lòng kiểm tra kết nối mạng.", "Cannot connect to the verification server to run this AI task. Please check your network connection."))
            return
        status_code = verification.get("status_code", 400)
        is_invalid = status_code == 401 or "không tồn tại" in verification.get("message", "").lower()
        if is_invalid:
            config_service.update_auth_config(api_key="")
        raise HTTPException(status_code=401, detail=verification.get("message", L("Khóa bản quyền đã bị thu hồi hoặc đổi thiết bị sử dụng.", "The license key has been revoked or moved to another device.")))


