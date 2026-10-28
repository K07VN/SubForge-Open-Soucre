import hashlib
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import asyncio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from routes.auth_routes import verify_license
from utils.ssl_tin_cay import huong_dan_loi_chung_chi, la_loi_chung_chi

logger = logging.getLogger("HardwareRoutes")

router = APIRouter(prefix="/api/hardware", tags=["hardware"])

# BẢO MẬT: các endpoint GHI của router này trước đây KHÔNG kiểm bản quyền (đây là
# router duy nhất thiếu). Backend chỉ bind 127.0.0.1 nhưng local_only_guard cho qua
# mọi request không có header Origin, nên một dòng curl trên chính máy đó là gọi
# được: giải nén zip từ đường dẫn tuỳ ý (/install-gpu-local), xoá sạch gpu_pack
# (/uninstall-gpu), hay tắt GPU toàn app (/set-cpu-mode).
# Các endpoint CHỈ ĐỌC (status, install-progress) cố ý để mở: UI cần đọc trạng thái
# máy/tiến độ tải kể cả trước khi kích hoạt, và chúng không gây tác dụng phụ.
LICENSED = [Depends(verify_license)]

from utils.storage_paths import get_workstation_dir
from utils.i18n import L

SUBFORGE_DEV = os.environ.get("SUBFORGE_DEV") == "true"
# Data root có thể do người dùng cấu hình (storage.json) — không còn hardcode C:.
WORKSTATION_DIR = get_workstation_dir()
GPU_PACK_DIR = os.path.join(WORKSTATION_DIR, "gpu_pack")
GPU_EXE_PATH = os.path.join(
    GPU_PACK_DIR, "subforge_backend_gpu", "subforge_backend_gpu.exe"
)
UNINSTALL_FLAG = os.path.join(WORKSTATION_DIR, "uninstall_gpu.flag")
FORCE_CPU_FLAG = os.path.join(WORKSTATION_DIR, "force_cpu.flag")
MIN_GPU_EXE_BYTES = 5 * 1024 * 1024
MIN_ZIP_BYTES = 1024 * 1024

# SHA-256 kỳ vọng của gói GPU (để trống → thử tải file cạnh "<url>.sha256"). Ưu tiên
# env SUBFORGE_GPU_PACK_SHA256 (Electron truyền từ deploy.config.js).
EXPECTED_GPU_PACK_SHA256 = os.environ.get("SUBFORGE_GPU_PACK_SHA256", "") or ""

# URL gói GPU: env SUBFORGE_GPU_PACK_URL (deploy.config.js) → fallback.
# GIỮ ĐỒNG BỘ version + định dạng với electron/deploy.config.js (GPU_PACK_URL) để khi
# env không được truyền (chạy backend trần) vẫn tải ĐÚNG gói khớp ABI torch/CUDA của
# app hiện tại — tránh cài nhầm gói cũ gây lệch ABI. Download tự nhận .json = multipart.
DEFAULT_GPU_PACK_URL = (
    "https://github.com/nyatto03/SubForge-Releases/releases/download/v2.3.0/"
    "subforge_backend_gpu.manifest.json"
)


def _resolve_gpu_pack_url() -> str:
    """URL gói GPU, ưu tiên: (1) admin quản lý per-version qua /api/public/latest
    (gpu_pack_url) → (2) env SUBFORGE_GPU_PACK_URL (deploy.config) → (3) default.

    Chỉ gọi khi người dùng bấm cài GPU (hiếm) nên 1 request admin ở đây không đáng kể."""
    try:
        from services.config_service import get_config_service

        admin_url = get_config_service().get_admin_url()
        if admin_url:
            _prev = socket.getdefaulttimeout()
            socket.setdefaulttimeout(8)
            try:
                with urllib.request.urlopen(
                    admin_url.rstrip("/") + "/api/public/latest"
                ) as r:
                    data = json.loads(r.read().decode("utf-8", errors="ignore"))
            finally:
                socket.setdefaulttimeout(_prev)
            gpu = (data.get("latest") or {}).get("gpu_pack_url")
            if gpu and str(gpu).strip():
                logger.info("GPU Pack: dùng URL từ admin latest.")
                return str(gpu).strip()
    except Exception as e:
        logger.info("GPU Pack: không lấy được URL admin latest (%s) — dùng env/default.", e)

    return os.environ.get("SUBFORGE_GPU_PACK_URL") or DEFAULT_GPU_PACK_URL


def _fetch_expected_sha256(url: str) -> str:
    """Lấy SHA-256 kỳ vọng: ưu tiên hằng/env; nếu trống thử tải '<url>.sha256'
    (file text chứa hex, có thể kèm tên file). Trả '' nếu không có (bỏ kiểm)."""
    if EXPECTED_GPU_PACK_SHA256:
        return EXPECTED_GPU_PACK_SHA256.strip()
    try:
        prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(15)
        try:
            with urllib.request.urlopen(url + ".sha256") as resp:
                text = resp.read().decode("utf-8", errors="ignore").strip()
        finally:
            socket.setdefaulttimeout(prev)
        # Dạng "abcdef...  subforge_backend_gpu.zip" hoặc chỉ hex.
        token = text.split()[0] if text else ""
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
    except Exception as e:
        logger.info("GPU Pack: không có file .sha256 kèm theo (%s) — bỏ kiểm checksum.", e)
    return ""

# install_state bị ghi từ thread BackgroundTask (download/extract) và đọc từ
# endpoint stream (async) + GET. Lock bảo đảm reader luôn thấy snapshot nhất quán
# (không đọc rách giữa lúc reset nhiều field). Ghi 1 field lẻ (progress) là atomic
# trong CPython nên không cần lock, chỉ lock các khối reset nhiều field + lúc đọc.
install_lock = threading.Lock()

install_state = {
    "status": "idle",
    "progress": 0,
    "error_message": "",
    "cancel_requested": False,
}


class HardwareStatus(BaseModel):
    has_nvidia_gpu: bool
    gpu_name: str
    gpu_pack_installed: bool
    gpu_bundled_in_app: bool = False
    force_cpu_mode: bool = False
    gpu_pack_invalid: bool = False
    # Gói GPU trên đĩa thuộc bản app CŨ (định danh khác bản hiện tại) → nên cập nhật
    # để khớp torch/CUDA của bản mới, tránh lỗi/âm thầm lùi CPU sau khi nâng cấp app.
    gpu_pack_outdated: bool = False
    current_device: str
    gpu_vram_gb: float = 0.0
    total_ram_gb: float = 0.0


def _get_gpus_powershell() -> list:
    for cmd_str in [
        "Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM | ConvertTo-Json -Compress",
        "Get-WmiObject Win32_VideoController | Select-Object Name, AdapterRAM | ConvertTo-Json -Compress"
    ]:
        try:
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd_str]
            output = subprocess.check_output(cmd, creationflags=0x08000000, timeout=8).decode("utf-8", errors="ignore").strip()
            if output:
                data = json.loads(output)
                if isinstance(data, dict):
                    return [data]
                elif isinstance(data, list):
                    return data
        except Exception:
            continue
    return []


def check_nvidia_gpu_physical() -> bool:
    if shutil.which("nvidia-smi") is not None:
        return True
    default_win_path = "C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe"
    if os.path.exists(default_win_path):
        return True
    try:
        output = subprocess.check_output(
            ["wmic", "path", "win32_VideoController", "get", "name"],
            creationflags=0x08000000,
        ).decode("utf-8", errors="ignore")
        if "nvidia" in output.lower():
            return True
    except Exception:
        pass

    # Fallback to PowerShell
    try:
        gpus = _get_gpus_powershell()
        for gpu in gpus:
            name = gpu.get("Name") or gpu.get("name") or ""
            if "nvidia" in name.lower():
                return True
    except Exception:
        pass

    return False


def get_gpu_name() -> str:
    try:
        output = subprocess.check_output(
            ["wmic", "path", "win32_VideoController", "get", "name"],
            creationflags=0x08000000,
        ).decode("utf-8", errors="ignore")
        lines = [line.strip() for line in output.split("\n") if line.strip()]
        for line in lines:
            if "nvidia" in line.lower():
                return line
        for line in lines:
            if line.lower() not in ["name", "microsoft basic display adapter"]:
                return line
    except Exception:
        pass

    # Fallback to PowerShell
    try:
        gpus = _get_gpus_powershell()
        for gpu in gpus:
            name = gpu.get("Name") or gpu.get("name") or ""
            if "nvidia" in name.lower():
                return name
        for gpu in gpus:
            name = gpu.get("Name") or gpu.get("name") or ""
            if name.lower() not in ["name", "microsoft basic display adapter", ""]:
                return name
    except Exception:
        pass

    return ""


def get_nvidia_vram_gb() -> float:
    # 1) Try nvidia-smi
    smi_path = "nvidia-smi"
    if shutil.which("nvidia-smi") is None:
        default_win_path = (
            "C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe"
        )
        if os.path.exists(default_win_path):
            smi_path = default_win_path
        else:
            smi_path = None

    if smi_path:
        try:
            res = subprocess.run(
                [smi_path, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                check=True,
                creationflags=0x08000000,
            )
            val = res.stdout.strip()
            return float(val) / 1024.0
        except Exception:
            pass

    # 2) Try torch
    try:
        import torch

        if torch.cuda.is_available():
            _, total_b = torch.cuda.mem_get_info()
            return total_b / (1024**3)
    except Exception:
        pass

    # 3) Try wmic (Windows standard)
    try:
        output = subprocess.check_output(
            ["wmic", "path", "win32_VideoController", "get", "Name,AdapterRAM"],
            text=True,
            creationflags=0x08000000,
        )
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        if len(lines) > 1:
            for line in lines[1:]:
                if "nvidia" in line.lower():
                    parts = line.split()
                    for part in parts:
                        if part.isdigit():
                            return float(part) / (1024**3)
            for line in lines[1:]:
                parts = line.split()
                for part in parts:
                    if part.isdigit():
                        return float(part) / (1024**3)
    except Exception:
        pass

    # 4) Try PowerShell fallback
    try:
        gpus = _get_gpus_powershell()
        for gpu in gpus:
            name = gpu.get("Name") or gpu.get("name") or ""
            ram = gpu.get("AdapterRAM") or gpu.get("vram_bytes") or 0
            if "nvidia" in name.lower() and ram:
                if ram < 0:
                    ram = ram + 2**32
                return float(ram) / (1024**3)
        for gpu in gpus:
            name = gpu.get("Name") or gpu.get("name") or ""
            ram = gpu.get("AdapterRAM") or gpu.get("vram_bytes") or 0
            if name.lower() not in ["name", "microsoft basic display adapter", ""] and ram:
                if ram < 0:
                    ram = ram + 2**32
                return float(ram) / (1024**3)
    except Exception:
        pass

    return 0.0


def _gpu_exe_size() -> int:
    try:
        return os.path.getsize(GPU_EXE_PATH) if os.path.exists(GPU_EXE_PATH) else 0
    except OSError:
        return 0


def is_gpu_pack_installed() -> bool:
    if os.path.exists(UNINSTALL_FLAG):
        return False
    # Cần CẢ exe đủ lớn LẪN marker installed.txt (chỉ ghi sau khi giải nén +
    # validate xong). Tránh trường hợp crash giữa lúc giải nén để lại dir "có exe"
    # nhưng thiếu DLL → báo đã cài nhầm.
    marker = os.path.join(GPU_PACK_DIR, "installed.txt")
    return _gpu_exe_size() >= MIN_GPU_EXE_BYTES and os.path.exists(marker)


def is_force_cpu_mode() -> bool:
    return os.path.exists(UNINSTALL_FLAG) or os.path.exists(FORCE_CPU_FLAG)


def is_gpu_pack_invalid() -> bool:
    if not os.path.exists(GPU_EXE_PATH):
        return False
    size = _gpu_exe_size()
    return 0 < size < MIN_GPU_EXE_BYTES


_PACK_VERSION_PREFIX = "pack_version="


def _current_gpu_pack_version() -> str:
    """'Phiên bản' GÓI GPU mà bản app hiện tại yêu cầu — TÁCH BIỆT phiên bản app.

    Nguồn: env SUBFORGE_GPU_PACK_VERSION (deploy.config.js → GPU_PACK_VERSION). NHIỀU
    bản app có thể dùng chung 1 gói: team chỉ đổi giá trị này khi phát hành gói GPU MỚI
    KHÔNG tương thích ngược. KHÔNG dùng URL/số phiên bản app để so — vì URL có thể đổi
    theo mỗi release dù gói vẫn chạy tốt (→ tránh báo cũ nhầm, tránh ép tải lại vô ích)."""
    return (os.environ.get("SUBFORGE_GPU_PACK_VERSION") or "").strip()


def _write_installed_marker() -> None:
    """Ghi marker installed.txt KÈM phiên bản gói. Dòng 1 giữ chữ 'Installed' cho tương
    thích cách đọc cũ; dòng 2 'pack_version=<...>' để lần nâng cấp app sau so khớp."""
    ver = _current_gpu_pack_version()
    with open(os.path.join(GPU_PACK_DIR, "installed.txt"), "w", encoding="utf-8") as f:
        f.write("Installed\n")
        if ver:
            f.write(_PACK_VERSION_PREFIX + ver + "\n")


def _read_installed_pack_version() -> str:
    """Đọc phiên bản gói đã lưu trong installed.txt. Marker cũ (chỉ có chữ 'Installed',
    chưa ghi pack_version) → trả '' = không xác định."""
    marker = os.path.join(GPU_PACK_DIR, "installed.txt")
    try:
        with open(marker, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(_PACK_VERSION_PREFIX):
                    return line[len(_PACK_VERSION_PREFIX):].strip()
    except Exception:
        pass
    return ""


def is_gpu_pack_outdated() -> bool:
    """Gói GPU trên đĩa thuộc PHIÊN BẢN GÓI cũ hơn bản app hiện tại yêu cầu.

    CHỈ báo cũ khi CẢ HAI đầu xác định được VÀ KHÁC nhau. Nghĩa là:
      • App không khai GPU_PACK_VERSION (env rỗng) → KHÔNG bao giờ báo cũ (tắt tính năng).
      • Marker cũ chưa ghi pack_version (máy đã cài trước bản này) → KHÔNG báo cũ.
      • Cùng phiên bản gói (nhiều bản app dùng chung 1 gói) → KHÔNG báo cũ.
    Nhờ vậy không ép người dùng tải lại gói ~4-6GB khi gói cũ vẫn chạy tốt
    (ưu tiên giữ cái đã cài)."""
    if not is_gpu_pack_installed():
        return False
    expected = _current_gpu_pack_version()
    stored = _read_installed_pack_version()
    if not expected or not stored:
        return False
    return expected != stored


def _cleanup_gpu_pack_dir() -> None:
    if os.path.exists(GPU_PACK_DIR):
        shutil.rmtree(GPU_PACK_DIR, ignore_errors=True)
    os.makedirs(GPU_PACK_DIR, exist_ok=True)


def _cleanup_gpu_pack_dir_keep_parts() -> None:
    """Dọn thư mục gói GPU nhưng GIỮ các part .zip đã tải (để lần cài sau bỏ qua
    part đã tải hợp lệ — không phải tải lại nhiều GB); chỉ xoá cây đã bung dở."""
    if not os.path.exists(GPU_PACK_DIR):
        os.makedirs(GPU_PACK_DIR, exist_ok=True)
        return
    for name in os.listdir(GPU_PACK_DIR):
        if name.lower().endswith(".zip"):
            continue
        p = os.path.join(GPU_PACK_DIR, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                _force_remove(p)
            except OSError:
                pass


def _expected_gpu_pack_bytes() -> int:
    """Dung lượng gói GPU dự kiến (bytes): env SUBFORGE_GPU_PACK_SIZE_GB
    (deploy.config) nếu có, mặc định 6GB."""
    try:
        gb = float(os.environ.get("SUBFORGE_GPU_PACK_SIZE_GB") or 6.0)
    except ValueError:
        gb = 6.0
    return int(gb * 1024 ** 3)


def _check_disk_space_for_pack(required_bytes: int) -> None:
    """Kiểm đĩa trống TRƯỚC khi tải gói GPU: cần ~1.5x (vừa giữ part vừa bung).
    Thiếu -> raise lỗi tiếng Việt, không tải nửa chừng rồi chết vì hết đĩa."""
    os.makedirs(GPU_PACK_DIR, exist_ok=True)
    try:
        free = shutil.disk_usage(GPU_PACK_DIR).free
    except Exception:
        return  # không đo được thì bỏ qua, không chặn cài
    need = int(required_bytes * 1.5)
    if free < need:
        raise RuntimeError(L(
            f"Ổ đĩa không đủ dung lượng trống: cần ~{need / 1024 ** 3:.1f}GB, "
            f"còn {free / 1024 ** 3:.1f}GB. Hãy giải phóng bớt dung lượng ổ đĩa, "
            "hoặc đổi \"Vị trí lưu dữ liệu\" sang ổ khác (Cài đặt → Cấu hình lưu trữ) "
            "rồi thử lại.",
            f"Not enough free disk space: need ~{need / 1024 ** 3:.1f}GB, "
            f"only {free / 1024 ** 3:.1f}GB left. Please free up some disk space, "
            "or change the \"Data storage location\" to another drive (Settings → Storage configuration) "
            "and try again.",
        ))


def _urlretrieve_with_retry(url: str, dest: str, reporthook, attempts: int = 3):
    """urlretrieve có retry + backoff (mạng chập chờn hay đứt giữa chừng).
    Người dùng Hủy thì dừng NGAY, không retry. Tự đặt/khôi phục socket timeout 60s
    (urlretrieve không nhận timeout trực tiếp, mặc định không có -> treo vô hạn)."""
    last_err = None
    for i in range(1, attempts + 1):
        _prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(60)
        try:
            urllib.request.urlretrieve(url, dest, reporthook=reporthook)
            return
        except Exception as e:
            last_err = e
            if _is_cancellation(e) or install_state.get("cancel_requested"):
                raise RuntimeError("CANCELLATION_REQUESTED") from e
            if i < attempts:
                logger.warning(
                    "GPU Pack: tải thất bại lần %d/%d (%s) — thử lại sau %ds...",
                    i, attempts, e, 2 * i,
                )
                time.sleep(2 * i)
        finally:
            socket.setdefaulttimeout(_prev)
    raise last_err


def _validate_gpu_pack() -> None:
    size = _gpu_exe_size()
    if size < MIN_GPU_EXE_BYTES:
        raise RuntimeError(L(
            "Gói GPU không hợp lệ hoặc chưa được phát hành trên server. "
            f"File backend chỉ {size} bytes (cần ≥ {MIN_GPU_EXE_BYTES // (1024 * 1024)}MB). "
            "Vui lòng thử lại sau khi bản cập nhật có file subforge_backend_gpu.zip.",
            "The GPU pack is invalid or has not been released on the server. "
            f"The backend file is only {size} bytes (need ≥ {MIN_GPU_EXE_BYTES // (1024 * 1024)}MB). "
            "Please try again once the update includes the file subforge_backend_gpu.zip.",
        ))


@router.get("/status", response_model=HardwareStatus)
def get_hardware_status():
    import torch

    bundled = os.environ.get("SUBFORGE_GPU_BUNDLED") == "true"
    forced_cpu = is_force_cpu_mode()

    # torch.cuda.is_available() = CUDA có SẴN SÀNG cho tiến trình này không. Khi bật
    # chế độ CPU, main.py đã đặt CUDA_VISIBLE_DEVICES="" nên giá trị này là False
    # (GPU đã được giải phóng thật). Guard thêm forced_cpu để UI phản hồi TỨC THÌ
    # ngay cả trước khi khởi động lại (lúc process cũ vẫn còn thấy CUDA).
    cuda_now = torch.cuda.is_available()
    current_dev = "cpu" if forced_cpu else ("cuda" if cuda_now else "cpu")

    # gpu_pack_installed = gói GPU có TỒN TẠI để kích hoạt không (tách khỏi việc đang
    # chạy GPU hay CPU). Nhờ vậy khi toggle sang CPU, UI vẫn hiện gói đã cài + nút
    # bật lại/xoá, thay vì hiểu nhầm là chưa cài rồi mời tải lại.
    pack_present = bundled or is_gpu_pack_installed() or cuda_now

    return HardwareStatus(
        has_nvidia_gpu=check_nvidia_gpu_physical(),
        gpu_name=get_gpu_name(),
        gpu_pack_installed=pack_present,
        gpu_bundled_in_app=bundled,
        force_cpu_mode=forced_cpu,
        gpu_pack_invalid=is_gpu_pack_invalid(),
        # Bundled trong app thì luôn khớp bản app → không bao giờ "cũ". Chỉ xét gói rời.
        gpu_pack_outdated=(not bundled) and is_gpu_pack_outdated(),
        current_device=current_dev,
        gpu_vram_gb=round(get_nvidia_vram_gb(), 1),
        total_ram_gb=_get_total_ram_gb(),
    )


def _get_total_ram_gb() -> float:
    """Tổng RAM hệ thống (GB) — dùng để cảnh báo máy RAM thấp chạy model lớn."""
    try:
        import psutil

        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _force_remove(path: str) -> None:
    """Xoá file kể cả khi bị đặt read-only (bỏ cờ read-only rồi mới xoá)."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except Exception:
        pass
    os.remove(path)


def _extract_zip_with_progress(
    zip_path, dest_dir, on_progress=None, cancel_check=None, byte_reporter=None
):
    """Giải nén zip theo TỪNG entry để báo tiến trình realtime + hủy được giữa chừng.

    - Chống zip-slip: kiểm mọi entry nằm trong dest_dir trước khi giải nén.
    - on_progress(pct 0..100): gọi sau mỗi entry theo tổng dung lượng đã bung.
    - byte_reporter(done_bytes): gọi sau mỗi entry với TỔNG byte (chưa nén) đã bung
      của riêng zip này. Dùng cho tiến trình toàn cục khi cài nhiều part.
    - cancel_check(): trả True để dừng ngay (raise CANCELLATION_REQUESTED).
    - Bền với file bị khoá: một số DLL (msvcp140.dll, cudnn…) có thể đang được tiến
      trình hiện tại NẠP (loaded) nên Windows khoá, không ghi đè được (Errno 13).
      Nếu file ĐÃ tồn tại từ lần cài trước (cùng gói) → BỎ QUA, vì bản đang nạp
      chính là bản đúng. Chỉ khi file CÒN THIẾU mà không ghi được mới báo lỗi rõ.
    """
    dest_root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        for m in members:
            target = os.path.realpath(os.path.join(dest_dir, m.filename))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise RuntimeError(f"Entry không an toàn trong zip: {m.filename}")
        total = sum(m.file_size for m in members) or 1
        done = 0
        for m in members:
            if cancel_check and cancel_check():
                raise RuntimeError("CANCELLATION_REQUESTED")

            tpath = os.path.join(dest_dir, m.filename)
            # Dọn bản cũ (kể cả read-only) trước khi ghi đè. Nếu đang bị khoá thì
            # bước extract bên dưới sẽ ném PermissionError và được xử lý ở except.
            if not m.is_dir() and os.path.exists(tpath):
                try:
                    _force_remove(tpath)
                except OSError:
                    pass

            try:
                zf.extract(m, dest_dir)
            except PermissionError:
                if not m.is_dir() and os.path.exists(tpath):
                    logger.warning(
                        "GPU Pack: bỏ qua file đang bị khoá (đã có sẵn): %s",
                        m.filename,
                    )
                else:
                    raise RuntimeError(L(
                        f"Không ghi được '{m.filename}' vì file đang bị một tiến "
                        "trình khác sử dụng. Hãy ĐÓNG hẳn ứng dụng (thoát cả cửa sổ "
                        "nền/backend) rồi cài lại, hoặc khởi động lại máy.",
                        f"Cannot write '{m.filename}' because the file is in use by another "
                        "process. Fully CLOSE the app (including the background/backend "
                        "window) and install again, or restart your computer.",
                    )) from None

            done += m.file_size
            if on_progress:
                on_progress(min(100, int(done * 100 / total)))
            if byte_reporter:
                byte_reporter(done)


def _is_cancellation(e: Exception) -> bool:
    """True nếu exception (hoặc __cause__) là do người dùng bấm Hủy."""
    if "CANCELLATION_REQUESTED" in str(e):
        return True
    cause = getattr(e, "__cause__", None)
    return bool(cause and "CANCELLATION_REQUESTED" in str(cause))


def _is_manifest_url(url: str) -> bool:
    """Gói nhiều part khi URL trỏ tới manifest .json; ngược lại là zip đơn (bản cũ)."""
    return url.split("?", 1)[0].lower().rstrip("/").endswith(".json")


def _http_get_json(url: str, timeout: int = 20) -> dict:
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        with urllib.request.urlopen(url) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))
    finally:
        socket.setdefaulttimeout(prev)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download_and_extract_gpu_pack(url: str):
    """Điều phối: manifest .json → tải nhiều part; còn lại → zip đơn (bản cũ).
    Toàn bộ xử lý hủy/lỗi + dọn thư mục tập trung ở đây cho cả 2 luồng."""
    global install_state
    with install_lock:
        install_state["status"] = "downloading"
        install_state["progress"] = 0
        install_state["error_message"] = ""
        # KHÔNG reset cancel_requested ở đây: nếu người dùng bấm Hủy trong khoảng
        # giữa lúc endpoint trả về và thread này bắt đầu, reset sẽ nuốt mất yêu cầu
        # hủy đó. Việc reset được làm 1 lần ở endpoint (atomic với set status).

    # GIỮ lại part .zip đã tải từ lần trước (resume) — chỉ dọn cây đã bung dở.
    # Part cũ hỏng sẽ bị _download_multipart tự kiểm sha/size và tải lại.
    _cleanup_gpu_pack_dir_keep_parts()

    if os.path.exists(UNINSTALL_FLAG):
        try:
            os.remove(UNINSTALL_FLAG)
        except Exception:
            pass

    try:
        if _is_manifest_url(url):
            _download_multipart(url)  # kiểm đĩa bên trong theo size manifest
        else:
            _check_disk_space_for_pack(_expected_gpu_pack_bytes())
            _download_single_zip(url)

        _validate_gpu_pack()
        _write_installed_marker()
        with install_lock:
            install_state["status"] = "completed"
            install_state["progress"] = 100
        logger.info(
            "GPU Pack: cài đặt thành công (%s MB)",
            round(_gpu_exe_size() / 1024 / 1024, 1),
        )

    except Exception as e:
        if _is_cancellation(e):
            logger.info("GPU Pack: download cancelled by user.")
            with install_lock:
                install_state["status"] = "idle"
                install_state["progress"] = 0
                install_state["error_message"] = ""
                install_state["cancel_requested"] = False
            _cleanup_gpu_pack_dir()
        else:
            logger.error("GPU Pack: lỗi cài đặt — %s", e)
            thong_bao = str(e)
            #  Lỗi chứng chỉ SSL: câu kỹ thuật thô không giúp người dùng tự gỡ được gì —
            #  thay bằng các bước gỡ + đường TẢI TAY. Xem utils/ssl_tin_cay.py.
            if any(la_loi_chung_chi(x) for x in (e, e.__cause__) if x is not None):
                thong_bao = huong_dan_loi_chung_chi(url)
            with install_lock:
                install_state["status"] = "error"
                install_state["error_message"] = thong_bao
                install_state["progress"] = 0
                install_state["cancel_requested"] = False
            # Lỗi (không phải hủy): GIỮ part .zip đã tải hợp lệ để lần cài sau
            # tiếp tục, chỉ dọn phần đã bung dở.
            _cleanup_gpu_pack_dir_keep_parts()


def _download_single_zip(url: str):
    """Bản cũ: tải 1 zip → verify sha → giải nén vào GPU_PACK_DIR."""
    zip_path = os.path.join(GPU_PACK_DIR, "gpu_pack.zip")

    start_time = time.time()
    _no_total_last = [0.0]  # lần cập nhật detail gần nhất khi KHÔNG có Content-Length

    def report_progress(block_num, block_size, total_size):
        if install_state.get("cancel_requested"):
            raise RuntimeError("CANCELLATION_REQUESTED")
        if total_size <= 0:
            # Server không trả Content-Length: vẫn cập nhật MB đã tải ~1s/lần
            # để UI không trông như bị treo.
            now = time.time()
            if now - _no_total_last[0] >= 1.0:
                _no_total_last[0] = now
                got_mb = block_num * block_size / (1024 * 1024)
                install_state["detail"] = f"Đã tải {got_mb:.1f} MB... (chưa rõ tổng dung lượng)"
            return
        if total_size > 0:
            percent = int(block_num * block_size * 100 / total_size)
            install_state["progress"] = min(percent, 99)
            
            got = block_num * block_size
            got = min(got, total_size)
            elapsed = time.time() - start_time
            speed = got / elapsed if elapsed > 0 else 0
            if speed > 0:
                eta = (total_size - got) / speed
                eta_str = f" - Còn {int(eta)}s" if eta < 60 else f" - Còn {int(eta // 60)}p {int(eta % 60)}s"
            else:
                eta_str = ""
            
            got_mb = got / (1024 * 1024)
            size_mb = total_size / (1024 * 1024)
            speed_mb = speed / (1024 * 1024)
            
            install_state["detail"] = f"Đang tải GPU Pack: {got_mb:.1f} MB / {size_mb:.1f} MB ({speed_mb:.1f} MB/s){eta_str}"

    logger.info("GPU Pack: bắt đầu tải từ %s", url)
    # Tải qua helper có retry 3 lần + backoff (helper tự đặt socket timeout 60s).
    try:
        _urlretrieve_with_retry(url, zip_path, report_progress)
    except Exception as e:
        if _is_cancellation(e):
            raise RuntimeError("CANCELLATION_REQUESTED") from e
        raise RuntimeError(L(
            "Không tải được GPU Pack. Kiểm tra mạng hoặc file chưa có trên server "
            f"(subforge_backend_gpu.zip). Chi tiết: {e}",
            "Could not download the GPU Pack. Check your network, or the file is not on the server yet "
            f"(subforge_backend_gpu.zip). Details: {e}",
        )) from e

    if install_state.get("cancel_requested"):
        raise RuntimeError("CANCELLATION_REQUESTED")

    zip_size = os.path.getsize(zip_path)
    if zip_size < MIN_ZIP_BYTES:
        raise RuntimeError(L(
            f"File tải về quá nhỏ ({zip_size} bytes) — URL có thể trả về 404. "
            "Liên hệ quản trị viên để phát hành GPU Pack.",
            f"The downloaded file is too small ({zip_size} bytes) — the URL may have returned 404. "
            "Contact the administrator to release the GPU Pack.",
        ))

    # Kiểm toàn vẹn: SHA từ hằng/env, nếu trống thử file cạnh "<url>.sha256".
    # Có sha → bắt buộc khớp; không có → chỉ dựa vào kiểm kích thước ở trên.
    expected_sha = _fetch_expected_sha256(url)
    if expected_sha:
        actual = _sha256_file(zip_path)
        if actual.lower() != expected_sha.lower():
            raise RuntimeError(f"GPU pack checksum không khớp (nhận {actual[:12]}...).")
        logger.info("GPU Pack: checksum SHA-256 khớp.")

    if install_state.get("cancel_requested"):
        raise RuntimeError("CANCELLATION_REQUESTED")

    with install_lock:
        install_state["status"] = "extracting"
        install_state["progress"] = 0
        install_state["detail"] = L("Đang giải nén gói GPU...", "Extracting GPU pack...")

    def _extract_progress(pct):
        install_state["progress"] = pct
        install_state["detail"] = f"Đang giải nén gói GPU... {pct}%"

    _extract_zip_with_progress(
        zip_path,
        GPU_PACK_DIR,
        on_progress=_extract_progress,
        cancel_check=lambda: install_state.get("cancel_requested"),
    )

    if os.path.exists(zip_path):
        os.remove(zip_path)


def _download_multipart(manifest_url: str):
    """Tải gói GPU chia nhiều part theo manifest .json. Với MỖI part: tải → kiểm
    kích thước + sha256 → giải nén gộp vào GPU_PACK_DIR → XOÁ part (giữ đĩa thấp).
    Tiến trình toàn cục (0..99) tính theo tổng byte tải + byte giải nén để thanh
    progress luôn tăng, không lùi khi chuyển part."""
    try:
        manifest = _http_get_json(manifest_url)
    except Exception as e:
        if _is_cancellation(e):
            raise RuntimeError("CANCELLATION_REQUESTED") from e
        raise RuntimeError(
            f"Không tải được manifest GPU Pack ({manifest_url}). "
            f"Kiểm tra mạng hoặc release chưa có file manifest. Chi tiết: {e}"
        ) from e

    parts = manifest.get("parts") or []
    if not parts:
        raise RuntimeError(L("Manifest GPU Pack rỗng (không có part nào).", "The GPU Pack manifest is empty (no parts)."))

    total_dl = sum(int(p.get("size", 0)) for p in parts)
    total_ex = sum(int(p.get("uncompressed", 0)) for p in parts)
    grand_total = (total_dl + total_ex) or 1

    # Kiểm đĩa theo tổng size THẬT từ manifest (fallback ước lượng env/6GB).
    # TRỪ các part .zip đã nằm sẵn hợp lệ từ lần cài trước (resume): chúng vừa
    # chiếm chỗ trên đĩa (làm free ÍT đi) vừa KHÔNG cần tải lại — không trừ thì
    # máy vừa đủ đĩa sẽ bị chặn oan "không đủ dung lượng" đúng lúc retry.
    already_have = 0
    for p in parts:
        p_name = p.get("name") or ""
        p_size = int(p.get("size", 0))
        p_zip = os.path.join(GPU_PACK_DIR, p_name)
        try:
            if p_name and p_size and os.path.isfile(p_zip) and os.path.getsize(p_zip) == p_size:
                already_have += p_size
        except OSError:
            pass
    if total_dl + total_ex:
        required = max(0, total_dl - already_have) + total_ex
    else:
        required = _expected_gpu_pack_bytes()
    _check_disk_space_for_pack(required)

    processed = 0  # tổng byte đã xử lý (tải + giải nén) trên toàn bộ part

    def set_progress():
        install_state["progress"] = min(99, int(processed * 100 / grand_total))

    logger.info("GPU Pack: manifest có %d part, bắt đầu tải.", len(parts))

    for idx, part in enumerate(parts, 1):
        if install_state.get("cancel_requested"):
            raise RuntimeError("CANCELLATION_REQUESTED")

        name = part.get("name")
        if not name:
            raise RuntimeError(f"Manifest thiếu tên file ở part {idx}.")
        part_url = urllib.parse.urljoin(manifest_url, name)
        part_zip = os.path.join(GPU_PACK_DIR, name)
        size = int(part.get("size", 0))
        uncomp = int(part.get("uncompressed", 0))

        # ---------- tải part ----------
        with install_lock:
            install_state["status"] = "downloading"
        base = processed

        start_time = time.time()

        def report(block_num, block_size, _total, _base=base, _size=size):
            if install_state.get("cancel_requested"):
                raise RuntimeError("CANCELLATION_REQUESTED")
            got = block_num * block_size
            if _size:
                got = min(got, _size)
            nonlocal processed
            processed = _base + got
            set_progress()

            elapsed = time.time() - start_time
            speed = got / elapsed if elapsed > 0 else 0
            if speed > 0:
                eta = (_size - got) / speed
                eta_str = f" - Còn {int(eta)}s" if eta < 60 else f" - Còn {int(eta // 60)}p {int(eta % 60)}s"
            else:
                eta_str = ""

            got_mb = got / (1024 * 1024)
            size_mb = _size / (1024 * 1024)
            speed_mb = speed / (1024 * 1024)

            install_state["detail"] = f"Đang tải phần {idx}/{len(parts)} ({name}): {got_mb:.1f} MB / {size_mb:.1f} MB ({speed_mb:.1f} MB/s){eta_str}"

        expected = (part.get("sha256") or "").strip().lower()

        def _part_ok() -> bool:
            """Part đã có trên đĩa và qua kiểm sha256 (nếu manifest có) hoặc size."""
            if not os.path.isfile(part_zip):
                return False
            try:
                asize = os.path.getsize(part_zip)
            except OSError:
                return False
            if asize < MIN_ZIP_BYTES:
                return False
            if expected:
                return _sha256_file(part_zip).lower() == expected
            return (not size) or asize == size

        if _part_ok():
            # Resume: part còn nguyên từ lần cài trước → bỏ qua tải, chỉ giải nén.
            logger.info(
                "GPU Pack: part %d/%d (%s) đã có sẵn hợp lệ — bỏ qua tải.",
                idx, len(parts), name,
            )
            processed = base + (size or os.path.getsize(part_zip))
            set_progress()
        else:
            # Part cũ hỏng/thiếu → xoá rồi tải lại (retry 3 lần trong helper).
            if os.path.exists(part_zip):
                try:
                    _force_remove(part_zip)
                except OSError:
                    pass
            logger.info("GPU Pack: tải part %d/%d (%s)", idx, len(parts), name)
            try:
                _urlretrieve_with_retry(part_url, part_zip, report)
            except Exception as e:
                if _is_cancellation(e):
                    raise RuntimeError("CANCELLATION_REQUESTED") from e
                raise RuntimeError(
                    f"Không tải được part {idx}/{len(parts)} ({name}). "
                    f"Kiểm tra mạng hoặc file chưa có trên release. Chi tiết: {e}"
                ) from e

            actual_size = os.path.getsize(part_zip)
            processed = base + (size or actual_size)
            set_progress()

            if actual_size < MIN_ZIP_BYTES:
                raise RuntimeError(
                    f"Part {idx} ({name}) tải về quá nhỏ ({actual_size} bytes) — có thể 404."
                )

            if expected:
                actual = _sha256_file(part_zip).lower()
                if actual != expected:
                    # Chỉ dọn part hỏng này (giữ các part khác cho resume).
                    try:
                        _force_remove(part_zip)
                    except OSError:
                        pass
                    raise RuntimeError(
                        f"Checksum part {idx} ({name}) không khớp (nhận {actual[:12]}...)."
                    )

        if install_state.get("cancel_requested"):
            raise RuntimeError("CANCELLATION_REQUESTED")

        # ---------- giải nén part ----------
        with install_lock:
            install_state["status"] = "extracting"
        ex_base = processed

        def on_bytes(done, _base=ex_base):
            nonlocal processed
            processed = _base + done
            set_progress()

            pct = min(100, int(done * 100 / uncomp)) if uncomp else 0
            install_state["detail"] = f"Đang giải nén phần {idx}/{len(parts)} ({name})... {pct}%"

        _extract_zip_with_progress(
            part_zip,
            GPU_PACK_DIR,
            byte_reporter=on_bytes,
            cancel_check=lambda: install_state.get("cancel_requested"),
        )

        processed = ex_base + uncomp
        set_progress()

        # Xoá part ngay sau khi giải nén xong → đỉnh dung lượng đĩa chỉ ~1 part + cây
        # đã bung, thay vì toàn bộ part cộng lại.
        try:
            os.remove(part_zip)
        except OSError:
            pass


def local_extract_job(filepath: str):
    """Cài từ máy local. Chọn manifest .json → cài nhiều part (các part cạnh manifest);
    chọn .zip → cài zip đơn (bản cũ)."""
    global install_state
    try:
        _cleanup_gpu_pack_dir()
        if _is_manifest_url(filepath):
            _local_multipart(filepath)
        else:
            _local_single_zip(filepath)

        _validate_gpu_pack()
        _write_installed_marker()
        with install_lock:
            install_state["status"] = "completed"
            install_state["progress"] = 100
        logger.info("GPU Pack: Cài đặt từ local thành công.")

    except Exception as e:
        if _is_cancellation(e):
            logger.info("GPU Pack: cài đặt từ local bị huỷ.")
            with install_lock:
                install_state["status"] = "idle"
                install_state["progress"] = 0
                install_state["error_message"] = ""
                install_state["cancel_requested"] = False
            _cleanup_gpu_pack_dir()
        else:
            logger.error("GPU Pack: Lỗi cài đặt từ local — %s", e)
            with install_lock:
                install_state["status"] = "error"
                install_state["error_message"] = str(e)
                install_state["progress"] = 0
                install_state["cancel_requested"] = False
            _cleanup_gpu_pack_dir()


def _local_single_zip(filepath: str):
    with install_lock:
        install_state["status"] = "extracting"
        install_state["progress"] = 10
        install_state["error_message"] = ""

    zip_path = os.path.join(GPU_PACK_DIR, "gpu_pack.zip")
    shutil.copy2(filepath, zip_path)  # sao chép vào thư mục cài đặt
    install_state["progress"] = 50

    if install_state.get("cancel_requested"):
        raise RuntimeError("CANCELLATION_REQUESTED")

    if os.path.getsize(zip_path) < MIN_ZIP_BYTES:
        raise RuntimeError(L("File zip không hợp lệ hoặc quá nhỏ.", "The zip file is invalid or too small."))

    install_state["progress"] = 0
    install_state["detail"] = L("Đang chuẩn bị giải nén...", "Preparing to extract...")

    def _extract_progress(pct):
        install_state["progress"] = pct
        install_state["detail"] = f"Đang giải nén gói GPU... {pct}%"

    _extract_zip_with_progress(
        zip_path,
        GPU_PACK_DIR,
        on_progress=_extract_progress,
        cancel_check=lambda: install_state.get("cancel_requested"),
    )

    if os.path.exists(zip_path):
        os.remove(zip_path)


def _local_multipart(manifest_path: str):
    """Cài nhiều part từ manifest local: các part .zip phải nằm CÙNG thư mục manifest.
    Tiến trình toàn cục theo tổng byte giải nén (không có bước tải qua mạng)."""
    with install_lock:
        install_state["status"] = "extracting"
        install_state["progress"] = 0
        install_state["error_message"] = ""

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    parts = manifest.get("parts") or []
    if not parts:
        raise RuntimeError(L("Manifest rỗng (không có part nào).", "The manifest is empty (no parts)."))

    src_dir = os.path.dirname(os.path.abspath(manifest_path))
    total_ex = sum(int(p.get("uncompressed", 0)) for p in parts) or 1
    processed = 0

    for idx, part in enumerate(parts, 1):
        if install_state.get("cancel_requested"):
            raise RuntimeError("CANCELLATION_REQUESTED")

        name = part.get("name")
        if not name:
            raise RuntimeError(f"Manifest thiếu tên file ở part {idx}.")
        part_path = os.path.join(src_dir, name)
        if not os.path.exists(part_path):
            raise RuntimeError(
                f"Thiếu file '{name}' cạnh manifest. Đặt tất cả part + manifest cùng 1 thư mục."
            )

        expected = (part.get("sha256") or "").strip().lower()
        if expected and _sha256_file(part_path).lower() != expected:
            raise RuntimeError(f"Checksum part {idx} ({name}) không khớp.")

        base = processed

        def on_bytes(done, _base=base, _total=total_ex):
            install_state["progress"] = min(99, int((_base + done) * 100 / _total))
            pct = min(100, int(done * 100 / part.get("uncompressed", 1))) if part.get("uncompressed") else 0
            install_state["detail"] = f"Đang giải nén phần {idx}/{len(parts)} ({name})... {pct}%"

        _extract_zip_with_progress(
            part_path,
            GPU_PACK_DIR,
            byte_reporter=on_bytes,
            cancel_check=lambda: install_state.get("cancel_requested"),
        )
        processed = base + int(part.get("uncompressed", 0))


@router.post("/install-gpu", dependencies=LICENSED)
async def start_install_gpu(background_tasks: BackgroundTasks):
    """GPU đã tích hợp sẵn trong installer — endpoint giữ cho bản cũ."""
    if os.environ.get("SUBFORGE_GPU_BUNDLED") == "true":
        return {
            "status": "already_bundled",
            "message": L("GPU CUDA đã có sẵn trong app. Khởi động lại nếu chưa thấy GPU (CUDA) trong Active Engine.", "GPU CUDA is already bundled in the app. Restart if you do not see GPU (CUDA) in Active Engine."),
        }
    global install_state
    # URL gói GPU: lấy từ deploy.config.js (qua env SUBFORGE_GPU_PACK_URL) — có thể là
    # GitHub release HOẶC Google Drive; fallback release cũ nếu chưa cấu hình.
    download_url = _resolve_gpu_pack_url()

    # Guard + set trong cùng 1 lock để tránh 2 lần cài chạy song song (check-then-act).
    with install_lock:
        if install_state["status"] in ["downloading", "extracting"]:
            return {"status": "already_running"}
        install_state["status"] = "downloading"
        install_state["progress"] = 0
        install_state["error_message"] = ""
        install_state["cancel_requested"] = False  # reset 1 lần ở đây (job không reset)

    background_tasks.add_task(download_and_extract_gpu_pack, download_url)
    return {"status": "started"}


class LocalInstallRequest(BaseModel):
    filepath: str


@router.post("/install-gpu-local", dependencies=LICENSED)
async def install_gpu_local(
    req: LocalInstallRequest, background_tasks: BackgroundTasks
):
    global install_state
    filepath = req.filepath
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=400, detail=L("File không tồn tại trên máy.", "The file does not exist on this machine."))

    if not (filepath.endswith(".zip") or filepath.endswith(".json")):
        raise HTTPException(
            status_code=400,
            detail=L("File phải là .zip (gói đơn) hoặc .json (manifest nhiều part).", "The file must be a .zip (single package) or .json (multi-part manifest)."),
        )

    with install_lock:
        if install_state["status"] in ["downloading", "extracting"]:
            return {"status": "already_running"}
        install_state["status"] = "extracting"
        install_state["progress"] = 0
        install_state["error_message"] = ""
        install_state["cancel_requested"] = False

    background_tasks.add_task(local_extract_job, filepath)
    return {"status": "started"}


@router.post("/open-gpu-zip-dialog", dependencies=LICENSED)
async def open_gpu_zip_dialog():
    """Mở hộp thoại chọn file .zip gói GPU (để cài từ máy local) và trả về đường dẫn
    tuyệt đối cho /install-gpu-local. Dùng tkinter phía backend — KHÔNG phụ thuộc
    File.path của Electron (thuộc tính này đã bị gỡ ở các bản Electron mới nên input
    type=file không còn lấy được path). Tkinter chạy qua thread chuyên trách
    (utils.native_dialogs) — tạo Tk() trong threadpool gây "main thread is not in
    main loop" khi lần gọi sau rơi vào worker thread khác."""
    from fastapi.concurrency import run_in_threadpool
    from utils.native_dialogs import ask_open_filename

    def get_path():
        return ask_open_filename(
            title=L("Chọn gói tăng tốc GPU (manifest .json hoặc .zip)", "Choose the GPU acceleration pack (manifest .json or .zip)"),
            filetypes=[
                ("GPU pack (*.json *.zip)", "*.json *.zip"),
                (L("Manifest nhiều part (*.json)", "Multi-part manifest (*.json)"), "*.json"),
                (L("Gói đơn (*.zip)", "Single package (*.zip)"), "*.zip"),
                ("All files", "*.*"),
            ],
        )

    try:
        selected_path = await run_in_threadpool(get_path)
        return {"path": selected_path}
    except Exception as e:
        logger.error("Error opening GPU zip dialog: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/install-progress")
async def get_install_progress():
    with install_lock:
        return dict(install_state)


@router.get("/install-progress/stream")
async def stream_install_progress():
    async def event_generator():
        last_status = None
        while True:
            with install_lock:
                current = dict(install_state)

            if current != last_status:
                yield f"data: {json.dumps(current, ensure_ascii=False)}\n\n"
                last_status = dict(current)

            if current["status"] in ("completed", "error", "idle"):
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cancel-install-gpu", dependencies=LICENSED)
async def cancel_install_gpu():
    global install_state
    with install_lock:
        # Cho phép hủy cả khi đang tải LẪN đang giải nén (vòng lặp extract theo
        # từng entry sẽ kiểm cờ này và dừng ngay).
        if install_state["status"] in ("downloading", "extracting"):
            install_state["cancel_requested"] = True
            return {"status": "cancel_pending"}
        # Đang ở trạng thái LỖI (đã dừng) → dọn về idle để người dùng "tắt" thông báo.
        # Nếu không reset, install_state giữ "error" mãi -> mỗi lần F5 lại tự mở modal
        # báo lỗi cũ (dù tải đã dừng từ lâu).
        if install_state["status"] == "error":
            install_state["status"] = "idle"
            install_state["progress"] = 0
            install_state["error_message"] = ""
            install_state["cancel_requested"] = False
            return {"status": "cleared"}
    return {"status": "not_downloading"}


@router.post("/uninstall-gpu", dependencies=LICENSED)
async def uninstall_gpu():
    try:
        os.makedirs(WORKSTATION_DIR, exist_ok=True)
        with open(UNINSTALL_FLAG, "w", encoding="utf-8") as f:
            f.write("uninstall")
        return {
            "status": "pending_restart",
            "message": L("Gỡ cài đặt thành công. Vui lòng khởi động lại ứng dụng để áp dụng.", "Uninstalled successfully. Please restart the app to apply."),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Lỗi tạo flag gỡ cài đặt: {str(e)}"
        )


@router.post("/set-cpu-mode", dependencies=LICENSED)
async def set_cpu_mode():
    try:
        os.makedirs(WORKSTATION_DIR, exist_ok=True)
        with open(FORCE_CPU_FLAG, "w", encoding="utf-8") as f:
            f.write("force_cpu")
        return {
            "status": "cpu_mode_set",
            "message": L("Đã chuyển sang chế độ CPU. Khởi động lại app để áp dụng.", "Switched to CPU mode. Restart the app to apply."),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=L(f"Lỗi tạo flag CPU mode: {str(e)}", f"Error creating the CPU mode flag: {str(e)}"))


@router.post("/clear-cpu-mode", dependencies=LICENSED)
async def clear_cpu_mode():
    try:
        if os.path.exists(FORCE_CPU_FLAG):
            os.remove(FORCE_CPU_FLAG)
        return {
            "status": "cpu_mode_cleared",
            "message": L("Đã bỏ chế độ CPU. Khởi động lại app để dùng GPU.", "CPU mode removed. Restart the app to use the GPU."),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=L(f"Lỗi xoá flag CPU mode: {str(e)}", f"Error removing the CPU mode flag: {str(e)}"))
