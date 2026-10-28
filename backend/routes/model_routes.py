import os
import shutil
import logging
import threading
import time
import json
import glob
import asyncio
from tqdm.auto import tqdm as _base_tqdm
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from services.config_service import get_config_service
from utils.model_paths import get_models_dir
from routes.auth_routes import verify_license
from utils.i18n import L

logger = logging.getLogger("ModelRoutes")

# Tải model AI về máy cũng là thứ chỉ máy CÓ QUYỀN mới được làm.
router = APIRouter(
    prefix="/api/models", tags=["models"], dependencies=[Depends(verify_license)]
)

download_lock = threading.Lock()

# In-memory download state
download_state = {
    "status": "idle",  # "idle", "downloading", "completed", "error"
    "progress": 0,
    "current_model": "",
    "error_message": "",
    "downloaded_mb": 0.0,
    "target_mb": 0.0,
    "speed_mbps": 0.0,
    #  HÀNG ĐỢI (người dùng 09-10-2026): bấm Tải nhiều model một lượt, app tự tải lần lượt
    #  thay vì bắt đợi xong một cái mới bấm cái sau. Là TUPLE (bất biến) có chủ ý: SSE so
    #  ``dict(download_state) != last`` theo giá trị — để list rồi append thì bản chụp cũ và
    #  mới cùng trỏ một list, so sánh luôn bằng nhau và FE không bao giờ thấy hàng đợi đổi.
    "queue": (),
}

download_cancel_requested = False
download_start_time = 0.0
MAX_DOWNLOAD_DURATION = 1800  # 30 minutes
# Timeout CHỈ kích hoạt khi tải "đứng im" (không có byte mới) quá lâu — mạng chậm
# nhưng vẫn chạy thì KHÔNG giết. Cờ riêng để phân biệt timeout với người dùng Hủy.
STALL_TIMEOUT = 300  # 5 phút không có byte mới -> coi là mạng treo
download_timeout_stalled = False

# Handle của monitor thread hiện hành (đưa lên module-level để download_model_job
# LUÔN dừng được nó — kể cả khi _do_download raise/hủy/retry. Trước đây stop nằm
# ở CUỐI mỗi nhánh nên chỉ chạy khi thành công → thread rò, ~30 phút sau chạm timeout
# rồi set download_cancel_requested=True toàn cục → kẹt mọi HTTP của app.
_monitor_stop: "threading.Event | None" = None
_monitor_thread: "threading.Thread | None" = None

# Monkeypatch requests to support cancellation
try:
    import requests

    _original_send = requests.Session.send

    def _patched_send(self, request, **kwargs):
        global download_cancel_requested
        if download_cancel_requested:
            raise RuntimeError("CANCELLATION_REQUESTED")
        return _original_send(self, request, **kwargs)

    requests.Session.send = _patched_send
except Exception as e:
    logger.error(f"Failed to patch requests for cancel: {e}")

# LƯU Ý: KHÔNG patch urllib.request.urlopen nữa. Patch toàn cục đó từng giết
# MỌI HTTP của process (kể cả tải GPU pack ở hardware_routes) khi cờ hủy model
# đang bật. HF tải qua requests + _CancellableTqdm là đủ để hủy giữa chừng
# (chậm nhất là tới tick progress kế tiếp).

# Tổng byte đã tải (do _CancellableTqdm cộng dồn) — nguồn tiến trình CHÍNH xác,
# realtime hơn đo dung lượng thư mục. Reset về 0 trước mỗi lần tải.
_dl_bytes_lock = threading.Lock()
_dl_bytes_downloaded = 0


# --- tqdm hủy-được + đo byte thật ---
# huggingface_hub gọi tqdm.update(len(chunk)) theo TỪNG chunk trong http_get khi tải.
# Ghi đè update() để:
#   1) raise NGAY khi có yêu cầu hủy → dừng giữa file lớn (trước đây monkeypatch
#      requests/urllib chỉ kiểm cờ ở ĐẦU request nên file GB phải tải xong mới hủy);
#   2) cộng dồn byte đã tải → progress realtime chính xác (thay vì ước lượng theo
#      dung lượng thư mục — vốn sai khi HF xet tải vào cache ở nơi khác).
# LƯU Ý: chỉ hiệu lực khi tải qua http_get (tức phải TẮT xet — xem download_model_job).
# Giữ disable=True để không spam tiến trình ra stderr; check cờ + cộng byte chạy
# TRƯỚC super().update nên vẫn hoạt động dù bar bị tắt hiển thị.
class _CancellableTqdm(_base_tqdm):
    def __init__(self, *args, **kwargs):
        # Bắt unit TRƯỚC super(): khi disable=True, tqdm không set self.unit nên
        # phải tự lưu để lọc đúng bar byte ("B") khỏi bar 'Fetching N files'.
        self._sf_unit = kwargs.get("unit", "")
        kwargs.setdefault("disable", True)
        super().__init__(*args, **kwargs)

    def update(self, n=1):
        if download_cancel_requested:
            raise RuntimeError("CANCELLATION_REQUESTED")
        # Chỉ cộng byte cho bar đơn vị "B" (bỏ qua bar 'Fetching N files').
        if n and self._sf_unit == "B":
            global _dl_bytes_downloaded
            with _dl_bytes_lock:
                _dl_bytes_downloaded += int(n)
        return super().update(n)


# Tổng dung lượng TẢI THẬT (MB) cho từng model — dùng làm mẫu số "x MB / y MB" và %.
# Phải khớp byte mạng thật (_dl_bytes_downloaded đếm từ bar unit "B" của http_get)
# để "tải bao nhiêu hiện bấy nhiêu", KHÔNG cộng cache/copy trung gian.
# VieNeu: xác minh qua HF API (sum siblings.size, blobs=true) cho cả 3 repo:
#   VieNeu-TTS-v3-Turbo 1600.1 + MOSS-ONNX 86.4 + MOSS-PyTorch 84.1 = 1770.5 MB
#   (khớp đúng dung lượng thư mục sau cài). Con số cũ 2600 là ước lượng thời còn
#   đo bằng dung lượng thư mục cache (blobs + snapshots bị đếm trùng trên Windows).
# Demucs: htdemucs + hdemucs_mmi ~240MB (ước lượng cũ 150 làm % nhảy sớm lên 99
# rồi đứng yên rất lâu trong khi vẫn còn ~90MB phải tải).
# Whisper: theo số đo sau cài 141 / 1459.7 / 2947.7 MB.
MODEL_SIZES = {
    "whisper_base": 142.0,
    "whisper_medium": 1465.0,
    "whisper_large_v3": 2955.0,
    "vieneu": 1771.0,
    "demucs": 240.0,
}

#  Qwen3-TTS: tải thẳng từ Hugging Face như VieNeu (services/qwen_models.py — ghim
#  revision + SHA-256). Kích thước theo GÓI hợp với máy: Q8_0 ~2148 MB, Q4_K_M ~1420 MB
#  (RAM ít). Tính một lần lúc nạp module — RAM máy không đổi giữa chừng.
try:
    from services.qwen_models import choose_pack as _qwen_choose_pack, pack_mb as _qwen_pack_mb

    MODEL_SIZES["qwen3_tts"] = _qwen_pack_mb(_qwen_choose_pack())
except Exception as _e_qwen:  # noqa: BLE001 — thiếu Qwen không được làm hỏng các model khác
    logger.warning(f"Không tính được gói Qwen3-TTS: {_e_qwen}")

# Kích thước TỐI THIỂU của model.bin theo size Whisper (MB) — nhỏ hơn = tải dở/hỏng,
# coi như CHƯA cài để UI không báo installed nhầm.
from utils.model_paths import WHISPER_MIN_BIN_MB  # noqa: E402 — một nguồn, dùng chung với STT
# VieNeu: tổng dung lượng thư mục hf tối thiểu (MB) — thật ~1770MB.
VIENEU_MIN_DIR_MB = 1500.0


class UserFacingError(RuntimeError):
    """Lỗi đã có thông điệp tiếng Việt sẵn — hiển thị thẳng cho người dùng."""


def _viet_error(e: Exception) -> str:
    """Đổi lỗi kỹ thuật (tiếng Anh) thành thông điệp tiếng Việt thân thiện.
    Chi tiết gốc CHỈ ghi log (caller đã logger.error), không hiển thị cho người dùng."""
    if isinstance(e, UserFacingError):
        return str(e)
    s = str(e).lower()
    if "no space left" in s or "errno 28" in s or "errno 112" in s or "not enough space" in s:
        return L(
            "Ổ đĩa không đủ dung lượng trống để tải model. "
            "Hãy giải phóng bớt dung lượng, hoặc đổi \"Vị trí lưu dữ liệu\" sang ổ khác "
            "(Cài đặt → Cấu hình lưu trữ) rồi thử lại.",
            "Not enough free disk space to download the model. "
            "Free up some space, or change the \"Data storage location\" to another drive "
            "(Settings → Storage configuration) and try again.",
        )
    if "404" in s or "not found" in s:
        return L("Không tìm thấy model trên máy chủ (404). Vui lòng thử lại sau hoặc cập nhật ứng dụng.", "Model not found on the server (404). Please try again later or update the app.")
    if "403" in s or "401" in s or "forbidden" in s or "unauthorized" in s:
        return L("Máy chủ từ chối truy cập model. Vui lòng thử lại sau.", "The server denied access to the model. Please try again later.")
    if (
        "timeout" in s or "timed out" in s or "connection" in s or "getaddrinfo" in s
        or "ssl" in s or "proxy" in s or "network" in s or "unreachable" in s
    ):
        return L("Lỗi kết nối mạng khi tải model. Kiểm tra Internet/tường lửa rồi thử lại.", "Network error while downloading the model. Check your Internet/firewall and try again.")
    return L("Không tải được model. Vui lòng kiểm tra kết nối mạng và thử lại.", "Could not download the model. Please check your network connection and try again.")


def _check_disk_space(target_dir: str, required_mb: float) -> None:
    """Kiểm dung lượng trống TRƯỚC khi tải: cần ~1.5x dung lượng model (HF còn giữ
    bản trung gian trong .cache). Thiếu -> raise lỗi tiếng Việt, không bắt đầu tải."""
    try:
        free_bytes = shutil.disk_usage(target_dir).free
    except Exception:
        return  # không đo được (ổ lạ) thì bỏ qua, không chặn tải
    required_bytes = int(required_mb * 1.5 * 1024 * 1024)
    if free_bytes < required_bytes:
        raise UserFacingError(L(
            "Ổ đĩa không đủ dung lượng trống: cần ~"
            f"{required_bytes / (1024 ** 3):.1f}GB, còn {free_bytes / (1024 ** 3):.1f}GB. "
            "Hãy giải phóng bớt dung lượng, hoặc đổi \"Vị trí lưu dữ liệu\" sang ổ khác "
            "(Cài đặt → Cấu hình lưu trữ) rồi thử lại.",
            "Not enough free disk space: need ~"
            f"{required_bytes / (1024 ** 3):.1f}GB, only {free_bytes / (1024 ** 3):.1f}GB left. "
            "Free up some space, or change the \"Data storage location\" to another drive "
            "(Settings → Storage configuration) and try again.",
        ))


def _set_hf_offline_const(value):
    """Vá hằng số HF_HUB_OFFLINE của huggingface_hub (đã CHỐT lúc import từ env).

    App bật offline ngay lúc khởi động khi có bundle VieNeu (apply_vieneu_hf_env),
    nên chỉ pop env trước khi tải là KHÔNG đủ — snapshot_download vẫn bị chặn
    ("offline mode is enabled"). Trả về giá trị cũ để khôi phục sau khi tải."""
    try:
        import huggingface_hub.constants as _c
        old = getattr(_c, "HF_HUB_OFFLINE", None)
        _c.HF_HUB_OFFLINE = value
        return old
    except Exception:
        return None


class ModelStatus(BaseModel):
    model_name: str
    downloaded: bool
    size_mb: float
    status: str  # "installed", "missing", "downloading"


class AllModelsStatus(BaseModel):
    whisper_base: ModelStatus
    whisper_medium: ModelStatus
    whisper_large_v3: ModelStatus
    vieneu: ModelStatus
    demucs: ModelStatus
    qwen3_tts: ModelStatus
    active_whisper_size: str
    download_state: dict


# Helpers
def get_dir_size_mb(path: str) -> float:
    if not os.path.exists(path):
        return 0.0
    total_bytes = 0
    try:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.exists(fp):
                        total_bytes += os.path.getsize(fp)
        elif os.path.isfile(path):
            total_bytes = os.path.getsize(path)
    except Exception:
        pass
    return round(total_bytes / (1024 * 1024), 1)


def _dedup_whisper_dir(out_dir: str) -> None:
    """Xóa thư mục .cache trùng lặp mà HuggingFace để lại trong out_dir.

    Trên Windows (không tạo được symlink), faster-whisper/HuggingFace tải blob vào
    ``out_dir/.cache/huggingface`` RỒI COPY sang out_dir -> model chiếm GẤP ĐÔI
    (vd base ~141MB nhưng chiếm ~265MB). File model thật đã nằm ở out_dir nên xóa
    .cache là an toàn. CHỈ xóa khi model.bin là file thật (không phải symlink trỏ
    vào .cache — tránh làm hỏng model trên Linux).
    """
    model_bin = os.path.join(out_dir, "model.bin")
    if not os.path.isfile(model_bin) or os.path.islink(model_bin):
        return
    if os.path.getsize(model_bin) < 10 * 1024 * 1024:
        return  # quá nhỏ -> có thể là pointer, không đụng vào .cache
    cache_dir = os.path.join(out_dir, ".cache")
    if os.path.isdir(cache_dir):
        before = get_dir_size_mb(out_dir)
        shutil.rmtree(cache_dir, ignore_errors=True)
        after = get_dir_size_mb(out_dir)
        logger.info(
            f"[MODELS] Dọn .cache trùng trong {out_dir}: {before:.0f}MB -> {after:.0f}MB"
        )


def check_whisper_downloaded(size: str) -> bool:
    size_std = size.replace("_", "-")
    path = os.path.join(get_models_dir(), "whisper", size_std)

    # Auto-migration: rename old large_v3 folder to large-v3 if it exists
    old_path = os.path.join(get_models_dir(), "whisper", size.replace("-", "_"))
    if old_path != path and os.path.isdir(old_path) and not os.path.exists(path):
        try:
            os.rename(old_path, path)
        except Exception:
            pass

    model_bin = os.path.join(path, "model.bin")
    if not os.path.isfile(model_bin):
        return False
    # File quá nhỏ = tải dở/hỏng -> coi như CHƯA cài (tránh dùng model hỏng).
    min_mb = WHISPER_MIN_BIN_MB.get(size_std, 50.0)
    try:
        return os.path.getsize(model_bin) >= min_mb * 1024 * 1024
    except OSError:
        return False


# Cache kết quả đo size thư mục VieNeu (~1.8GB, os.walk đắt) — endpoint status
# bị FE poll liên tục; TTL 60s là đủ tươi (size chỉ đổi khi tải/xoá model).
_vieneu_size_cache: dict = {"t": 0.0, "ok": False}


def check_vieneu_downloaded() -> bool:
    hf_dir = os.path.join(get_models_dir(), "hf")
    has_marker = os.path.isfile(os.path.join(hf_dir, ".vieneu_ready_v2")) or os.path.isfile(
        os.path.join(hf_dir, ".vieneu_ready")
    )
    if not has_marker:
        _vieneu_size_cache["t"] = 0.0  # marker mất -> lần có lại phải đo tươi
        return False
    now = time.monotonic()
    if now - _vieneu_size_cache["t"] < 60.0:
        return _vieneu_size_cache["ok"]
    # Marker có nhưng dữ liệu quá nhỏ = tải dở/bị xóa tay -> coi như chưa cài.
    ok = get_dir_size_mb(hf_dir) >= VIENEU_MIN_DIR_MB
    _vieneu_size_cache.update(t=now, ok=ok)
    return ok


def _qwen_models_dir() -> str:
    """Nơi tải mô hình Qwen — LUÔN là thư mục dữ liệu thật (ai_models/qwen3_tts/models),
    không phải bản dev ở tools/_qwentts mà dịch vụ mượn khi chạy từ source."""
    from services.qwen_tts_service import _data_root, _override_dir

    o = _override_dir()
    return os.path.join(o, "models") if o else os.path.join(_data_root(), "models")


def check_qwen3_tts_downloaded() -> bool:
    """Đủ MỘT gói (Q8_0 hoặc Q4_K_M): đủ byte + đã kiểm SHA-256 lúc tải."""
    try:
        from services.qwen_models import installed_pack

        return installed_pack(_qwen_models_dir()) is not None
    except Exception:
        return False


def check_demucs_downloaded() -> bool:
    """Đủ CẢ 2 model (htdemucs + hdemucs_mmi): yaml có mặt VÀ mọi checkpoint .th
    mà yaml tham chiếu (theo signature) đều có file. Check lỏng kiểu cũ (có yaml +
    1 file .th bất kỳ) khiến bản cài THIẾU MỘT PHẦN (di sản của bug tải trước đây)
    bị coi là "đã cài" -> không cho tải lại -> đến lúc tách nhạc mới lỗi."""
    path = os.path.join(get_models_dir(), "demucs")
    if not os.path.isdir(path):
        return False
    try:
        files = os.listdir(path)
    except Exception:
        return False

    import yaml as _yaml

    for name in ("htdemucs", "hdemucs_mmi"):
        yaml_path = os.path.join(path, f"{name}.yaml")
        if not os.path.isfile(yaml_path):
            return False
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                sigs = (_yaml.safe_load(f) or {}).get("models", []) or []
        except Exception:
            return False  # yaml hỏng/cụt -> coi như chưa cài để tải lại
        for sig in sigs:
            if not any(f.startswith(str(sig)) and f.endswith(".th") for f in files):
                return False
    return True


@router.get("/status", response_model=AllModelsStatus)
def get_models_status():
    cfg = get_config_service().load_config()
    active_whisper = (
        cfg.get("whisper_size")
        or os.environ.get("SUBFORGE_WHISPER_SIZE", "base").strip()
        or "base"
    )
    #  Báo cỡ STT THẬT sẽ dùng — không thì tải Medium xong vẫn bị báo "thiếu Whisper Base"
    #  (cấu hình còn ở mặc định) và nút "Đang dùng" chỉ nhầm sang Base.
    from utils.model_paths import whisper_size_hieu_luc
    active_whisper = whisper_size_hieu_luc(active_whisper)

    models_dir = get_models_dir()
    whisper_base_path = os.path.join(models_dir, "whisper", "base")
    whisper_medium_path = os.path.join(models_dir, "whisper", "medium")
    whisper_large_path = os.path.join(models_dir, "whisper", "large-v3")
    vieneu_path = os.path.join(models_dir, "hf")
    demucs_path = os.path.join(models_dir, "demucs")

    wb_dl = check_whisper_downloaded("base")
    wm_dl = check_whisper_downloaded("medium")
    wl_dl = check_whisper_downloaded("large-v3")
    vn_dl = check_vieneu_downloaded()
    dm_dl = check_demucs_downloaded()
    qw_dl = check_qwen3_tts_downloaded()

    with download_lock:
        current_model = download_state["current_model"]
        dl_status = download_state["status"]
        hang_doi = set(download_state.get("queue") or ())

    # Auto-heal: dọn .cache trùng cho các bản Whisper đã cài (từ bản cũ tải trước khi
    # có dedup) để chúng về đúng dung lượng thật. Bỏ qua model đang tải dở (tránh race).
    _downloading = current_model if dl_status == "downloading" else None
    for _sz, _path, _dl in (
        ("base", whisper_base_path, wb_dl),
        ("medium", whisper_medium_path, wm_dl),
        ("large-v3", whisper_large_path, wl_dl),
    ):
        if _dl and _downloading != f"whisper_{_sz.replace('-', '_')}":
            try:
                _dedup_whisper_dir(_path)
            except Exception:
                pass

    def _trang_thai(key: str, da_cai: bool) -> str:
        if current_model == key and dl_status == "downloading":
            return "downloading"
        if key in hang_doi:
            return "queued"               # đang chờ tới lượt trong hàng đợi
        return "installed" if da_cai else "missing"

    status_map = {
        "whisper_base": _trang_thai("whisper_base", wb_dl),
        "whisper_medium": _trang_thai("whisper_medium", wm_dl),
        "whisper_large_v3": _trang_thai("whisper_large_v3", wl_dl),
        "vieneu": _trang_thai("vieneu", vn_dl),
        "demucs": _trang_thai("demucs", dm_dl),
        "qwen3_tts": _trang_thai("qwen3_tts", qw_dl),
    }

    return AllModelsStatus(
        whisper_base=ModelStatus(
            model_name="Whisper Base",
            downloaded=wb_dl,
            size_mb=get_dir_size_mb(whisper_base_path),
            status=status_map["whisper_base"],
        ),
        whisper_medium=ModelStatus(
            model_name="Whisper Medium",
            downloaded=wm_dl,
            size_mb=get_dir_size_mb(whisper_medium_path),
            status=status_map["whisper_medium"],
        ),
        whisper_large_v3=ModelStatus(
            model_name="Whisper Large-V3",
            downloaded=wl_dl,
            size_mb=get_dir_size_mb(whisper_large_path),
            status=status_map["whisper_large_v3"],
        ),
        vieneu=ModelStatus(
            model_name="VieNeu TTS",
            downloaded=vn_dl,
            size_mb=get_dir_size_mb(vieneu_path),
            status=status_map["vieneu"],
        ),
        demucs=ModelStatus(
            model_name="Demucs Separator",
            downloaded=dm_dl,
            size_mb=get_dir_size_mb(demucs_path),
            status=status_map["demucs"],
        ),
        qwen3_tts=ModelStatus(
            model_name="Qwen3-TTS",
            downloaded=qw_dl,
            size_mb=get_dir_size_mb(_qwen_models_dir()),
            status=status_map["qwen3_tts"],
        ),
        active_whisper_size=active_whisper,
        download_state=_download_state_snapshot(),
    )


def _do_download(model_key: str) -> None:
    global download_state, download_cancel_requested, download_start_time
    global _monitor_stop, _monitor_thread
    models_dir = get_models_dir()
    os.makedirs(models_dir, exist_ok=True)

    target_size = MODEL_SIZES.get(model_key, 100.0)

    # Kiểm đĩa trống TRƯỚC khi tải (cần ~1.5x size model) — thiếu thì báo lỗi rõ
    # ngay, không tải nửa chừng rồi chết vì hết đĩa.
    _check_disk_space(models_dir, target_size)

    stop_progress_monitor = threading.Event()
    _monitor_stop = stop_progress_monitor
    _monitor_thread = None

    def monitor_progress(target_dir, hf_cache_blobs_dir=None):
        prev_size = 0.0
        prev_time = time.time()
        last_progress_size = 0.0
        last_progress_time = time.time()
        while not stop_progress_monitor.is_set():
            time.sleep(1.0)
            now = time.time()
            # Ưu tiên byte thật từ tqdm (http_get) — chính xác & realtime. Nếu chưa
            # có (vd bar chưa chạy), lùi về đo dung lượng thư mục như cũ.
            with _dl_bytes_lock:
                tqdm_mb = _dl_bytes_downloaded / (1024 * 1024)
            if tqdm_mb > 0:
                current_size = tqdm_mb
            else:
                current_size = get_dir_size_mb(target_dir)
                if hf_cache_blobs_dir and os.path.isdir(hf_cache_blobs_dir):
                    current_size = max(current_size, get_dir_size_mb(hf_cache_blobs_dir))
            percent = int(current_size * 100 / target_size)

            with download_lock:
                download_state["progress"] = min(percent, 99)
                # Cap không vượt estimate: giai đoạn HuggingFace copy từ .cache sang
                # thư mục model cũng chạy bar đơn vị "B" -> byte bị đếm gần gấp đôi
                # (vd "185/145 MB"). Cap để dialog không hiển thị quá 100%.
                download_state["downloaded_mb"] = min(current_size, target_size)
                elapsed = now - prev_time
                if elapsed > 0:
                    delta = current_size - prev_size
                    # Chỉ cập nhật tốc độ khi có byte mới; nếu 1 nhịp khựng (delta<=0)
                    # thì GIỮ tốc độ cũ để hiển thị không bị nhấp nháy (ẩn/hiện).
                    if delta > 0:
                        download_state["speed_mbps"] = round(delta / elapsed, 1)

                # Timeout check: CHỈ dừng khi tải quá lâu VÀ mạng đã "đứng im"
                # (không có byte mới trong STALL_TIMEOUT) — mạng chậm nhưng vẫn
                # chạy thì để tiếp tục. Đặt cờ timeout RIÊNG để download_model_job
                # phân biệt với người dùng bấm Hủy (báo lỗi thay vì im lặng idle).
                if current_size > last_progress_size:
                    last_progress_size = current_size
                    last_progress_time = now
                if (
                    now - download_start_time > MAX_DOWNLOAD_DURATION
                    and now - last_progress_time > STALL_TIMEOUT
                ):
                    global download_cancel_requested, download_timeout_stalled
                    download_timeout_stalled = True
                    download_cancel_requested = True

            prev_size = current_size
            prev_time = now

    if model_key.startswith("whisper_"):
        size = model_key.replace("whisper_", "")
        official_size = size.replace("_", "-")
        repo = f"Systran/faster-whisper-{official_size}"
        out_dir = os.path.join(models_dir, "whisper", official_size)
        os.makedirs(out_dir, exist_ok=True)

        hf_cache_root = os.environ.get(
            "HF_HOME", os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
        )
        hf_cache_blobs = os.path.join(
            hf_cache_root, "hub", f"models--{repo.replace('/', '--')}", "blobs"
        )
        t_mon = threading.Thread(
            target=monitor_progress, args=(out_dir, hf_cache_blobs), daemon=True
        )
        t_mon.start()
        _monitor_thread = t_mon

        logger.info(f"Downloading Whisper {official_size} model from Hugging Face...")
        _saved_hf_offline = os.environ.pop("HF_HUB_OFFLINE", None)
        _saved_tr_offline = os.environ.pop("TRANSFORMERS_OFFLINE", None)
        # Mở online cả ở HẰNG SỐ (bundle VieNeu có sẵn -> app đã chốt offline lúc boot).
        _saved_const_offline = _set_hf_offline_const(False)
        try:
            from faster_whisper import download_model
            import faster_whisper.utils as _fw_utils

            # faster-whisper ép kwargs["tqdm_class"] = disabled_tqdm (không hủy được
            # giữa chừng). Tạm thay bằng tqdm hủy-được để dừng ngay khi bấm Hủy.
            _orig_disabled = _fw_utils.disabled_tqdm
            _fw_utils.disabled_tqdm = _CancellableTqdm
            try:
                download_model(official_size, output_dir=out_dir)
                # Dọn bản copy trùng trong .cache để model chỉ chiếm ~1 bản (đúng
                # dung lượng thật, vd base ~141MB thay vì ~265MB).
                _dedup_whisper_dir(out_dir)
                _chon_whisper_vua_tai_neu_can(official_size)
            finally:
                _fw_utils.disabled_tqdm = _orig_disabled
        finally:
            if _saved_hf_offline is not None:
                os.environ["HF_HUB_OFFLINE"] = _saved_hf_offline
            if _saved_tr_offline is not None:
                os.environ["TRANSFORMERS_OFFLINE"] = _saved_tr_offline
            if _saved_const_offline is not None:
                _set_hf_offline_const(_saved_const_offline)

    elif model_key == "vieneu":
        hf_dir = os.path.join(models_dir, "hf")
        os.makedirs(hf_dir, exist_ok=True)

        hf_cache_root = os.environ.get(
            "HF_HOME", os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
        )
        hf_cache_blobs = os.path.join(
            hf_cache_root, "hub", "models--pnnbao-ump--VieNeu-TTS-v3-Turbo", "blobs"
        )
        t_mon = threading.Thread(
            target=monitor_progress, args=(hf_dir, hf_cache_blobs), daemon=True
        )
        t_mon.start()
        _monitor_thread = t_mon

        logger.info("Downloading VieNeu TTS model from Hugging Face...")
        _saved_hf_home = os.environ.get("HF_HOME")
        os.environ["HF_HOME"] = hf_dir
        _saved_hf_offline = os.environ.pop("HF_HUB_OFFLINE", None)
        _saved_tr_offline = os.environ.pop("TRANSFORMERS_OFFLINE", None)
        # App có thể đã BẬT offline + chốt hằng số huggingface_hub lúc khởi động (khi đã
        # có bundle). Popping env KHÔNG mở lại được → phải VÁ THẲNG hằng số về online,
        # nếu không snapshot_download sẽ bị chặn ("offline mode is enabled").
        _saved_const_offline = _set_hf_offline_const(False)
        try:
            from huggingface_hub import snapshot_download

            # QUAN TRỌNG: truyền cache_dir TƯỜNG MINH = hf_dir/hub. huggingface_hub chốt
            # HF_HUB_CACHE thành hằng số lúc import (từ HF_HOME luc đó), nên đặt
            # os.environ["HF_HOME"]=hf_dir MUỘN (khi HF đã import) KHÔNG đổi được nơi lưu
            # → model rơi vào cache mặc định, models_dir/hf chỉ còn file marker (size=0)
            # và runtime offline không tìm thấy. Đặt cache_dir cố định vào đúng nơi bản
            # bundle (download_models.py) + runtime (HF_HOME=hf_dir → hf_dir/hub) dùng.
            # tqdm_class hủy-được → hủy tải giữa chừng file lớn (denoiser/model chính).
            snapshot_download(
                "pnnbao-ump/VieNeu-TTS-v3-Turbo",
                cache_dir=os.path.join(hf_dir, "hub"),
                tqdm_class=_CancellableTqdm,
            )
            # BẮT BUỘC: VieNeu v3turbo còn cần CODEC MOSS Audio Tokenizer (repo RIÊNG
            # của OpenMOSS). Thiếu nó -> khởi động giọng offline lỗi "cannot find files
            # in local cache" DÙ repo chính đã đủ (bug cũ: chỉ tải repo chính).
            #  - Bản -ONNX: engine ONNX/CPU dùng (bắt buộc với mọi máy).
            snapshot_download(
                "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX",
                cache_dir=os.path.join(hf_dir, "hub"),
                tqdm_class=_CancellableTqdm,
            )
            #  - Bản PyTorch: chỉ engine GPU (GPU pack) dùng — best-effort, thiếu thì
            #    GPU tự lui ONNX/CPU, không chặn cài đặt.
            try:
                snapshot_download(
                    "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano",
                    cache_dir=os.path.join(hf_dir, "hub"),
                    tqdm_class=_CancellableTqdm,
                )
            except Exception as _e_moss_pt:
                if download_cancel_requested:
                    raise
                logger.warning(
                    f"Không tải được MOSS tokenizer bản PyTorch (GPU sẽ tự lui CPU): {_e_moss_pt}"
                )
        finally:
            # Khôi phục HF_HOME: nếu để nguyên trỏ vào ai_models/hf, lần tải Whisper
            # sau + mọi thao tác HF khác trong process sẽ tính sai cache root.
            if _saved_hf_home is not None:
                os.environ["HF_HOME"] = _saved_hf_home
            else:
                os.environ.pop("HF_HOME", None)
            if _saved_hf_offline is not None:
                os.environ["HF_HUB_OFFLINE"] = _saved_hf_offline
            if _saved_tr_offline is not None:
                os.environ["TRANSFORMERS_OFFLINE"] = _saved_tr_offline
            if _saved_const_offline is not None:
                _set_hf_offline_const(_saved_const_offline)

        with open(os.path.join(hf_dir, ".vieneu_ready_v2"), "w", encoding="utf-8") as f:
            f.write("ok")
        # Tải xong: trỏ HF cache về bundle + bật lại offline + vá hằng số, để dùng
        # NGAY trong phiên này (không cần khởi động lại app) mà vẫn chạy offline.
        try:
            from utils.model_paths import apply_vieneu_hf_env
            apply_vieneu_hf_env()
        except Exception:
            pass

        from download_models import _flatten_hf_symlinks

        _flatten_hf_symlinks(hf_dir)

    elif model_key == "qwen3_tts":
        #  Tải TỪNG TỆP của gói hợp với máy, ghim revision, kiểm SHA-256 sau mỗi tệp.
        #  Tệp đã đủ + đã kiểm thì bỏ qua -> hủy/lỗi giữa chừng không phải tải lại
        #  từ đầu cả 2 GB. Máy chủ Qwen đang chạy thì tắt trước (giữ khoá tệp).
        from huggingface_hub import hf_hub_download

        from services import qwen_models as QM
        from services.qwen_tts_service import QwenTTSService

        QwenTTSService.stop()
        out_dir = _qwen_models_dir()
        os.makedirs(out_dir, exist_ok=True)
        pack = QM.choose_pack()
        global _dl_bytes_downloaded
        with _dl_bytes_lock:   # tệp đã có tính luôn vào tiến độ, % không tụt khi tải tiếp
            _dl_bytes_downloaded = sum(QM.QWEN_FILES[f][0] for f in QM.QWEN_PACKS[pack]
                                       if QM.file_ok(out_dir, f))
        t_mon = threading.Thread(target=monitor_progress, args=(out_dir,), daemon=True)
        t_mon.start()
        _monitor_thread = t_mon
        logger.info(f"Downloading Qwen3-TTS ({pack}) from Hugging Face "
                    f"{QM.QWEN_HF_REPO}@{QM.QWEN_HF_REVISION[:7]}...")
        for name in QM.QWEN_PACKS[pack]:
            if QM.file_ok(out_dir, name):
                continue
            hf_hub_download(
                QM.QWEN_HF_REPO, name, revision=QM.QWEN_HF_REVISION,
                local_dir=out_dir, tqdm_class=_CancellableTqdm,
            )
            if not QM.file_ok(out_dir, name, verify=True):
                try:
                    os.remove(os.path.join(out_dir, name))
                except OSError:
                    pass
                raise UserFacingError(L(
                    "Tệp mô hình Qwen3-TTS tải về bị hỏng (sai mã kiểm tra) — đã xoá tệp "
                    "đó. Bấm 'Tải về' để thử lại.",
                    "The downloaded Qwen3-TTS model file is corrupted (checksum mismatch) — the file "
                    "was deleted. Click 'Download' to try again.",
                ))

    elif model_key == "demucs":
        out_dir = os.path.join(models_dir, "demucs")
        os.makedirs(out_dir, exist_ok=True)

        t_mon = threading.Thread(target=monitor_progress, args=(out_dir,), daemon=True)
        t_mon.start()
        _monitor_thread = t_mon

        logger.info("Downloading Demucs Separation models...")
        import torch
        from demucs.pretrained import REMOTE_ROOT, _parse_remote_files

        remote = str(REMOTE_ROOT)

        # demucs >= 4.1 đổi hành vi get_model(): ƯU TIÊN tải qua HuggingFace hub,
        # chỉ khi HF lỗi mới rơi về kho AWS cũ (torch.hub). Khi nhánh HF thành
        # công, checkpoint nằm trong HF CACHE chứ không phải torch.hub/checkpoints
        # -> bước copy kiểu cũ trắng tay (log máy khách 2026-07-26: "thư mục không
        # tồn tại"). Bỏ hẳn get_model(): tự tải TRỰC TIẾP từng checkpoint theo URL
        # trong remote/files.txt vào THẲNG thư mục đích (model_dir=out_dir) —
        # load_state_dict_from_url tải qua torch.hub.download_url_to_file (temp
        # file rồi move, không để lại file cụt) + kiểm sha256 theo tên file
        # (check_hash=True) + vẫn đo tiến độ/hủy được nhờ patch tqdm bên dưới.
        sig_urls = _parse_remote_files(REMOTE_ROOT / "files.txt")

        import torch.hub as _torch_hub

        _orig_hub_tqdm = _torch_hub.tqdm
        _torch_hub.tqdm = _CancellableTqdm
        try:
            import glob as _glob

            for name in ["htdemucs", "hdemucs_mmi"]:
                shutil.copy2(
                    os.path.join(remote, f"{name}.yaml"),
                    os.path.join(out_dir, f"{name}.yaml"),
                )
                with open(os.path.join(remote, f"{name}.yaml"), "r", encoding="utf-8") as f:
                    import yaml

                    spec = yaml.safe_load(f)
                sigs = spec.get("models", []) or []
                for sig in sigs:
                    sig = str(sig)
                    if _glob.glob(os.path.join(out_dir, f"{sig}*.th")):
                        continue  # đã có từ lần tải trước (resume, không tải lại)
                    url = sig_urls.get(sig)
                    if not url:
                        logger.error(
                            f"Demucs '{name}': signature {sig} không có trong "
                            f"remote/files.txt (bản demucs đóng gói lệch yaml?)."
                        )
                        raise UserFacingError(L(
                            "Gói demucs trong app thiếu thông tin tải model. "
                            "Vui lòng cập nhật ứng dụng lên bản mới nhất.",
                            "The demucs package in the app is missing model download info. "
                            "Please update the app to the latest version.",
                        ))
                    logger.info(f"Demucs '{name}': tải checkpoint {sig} ...")
                    torch.hub.load_state_dict_from_url(
                        url, model_dir=out_dir, map_location="cpu", check_hash=True
                    )
                # Nghiệm thu theo TỪNG signature — thiếu là raise, không bao giờ
                # báo "completed" khi trọng số chưa nằm đủ trong out_dir.
                for sig in sigs:
                    if not _glob.glob(os.path.join(out_dir, f"{str(sig)}*.th")):
                        raise UserFacingError(L(
                            "Tải model Demucs chưa hoàn chỉnh (thiếu file trọng số). "
                            "Vui lòng bấm 'Tải về' để thử lại.",
                            "The Demucs model download is incomplete (missing weight files). "
                            "Please click 'Download' to try again.",
                        ))
        finally:
            _torch_hub.tqdm = _orig_hub_tqdm


def download_model_job(model_key: str):
    global download_state, download_cancel_requested, download_start_time
    global _dl_bytes_downloaded, download_timeout_stalled

    # Reset cờ hủy đầu job: phòng trường hợp một monitor rò trước đó (hoặc timeout)
    # đã bật cờ toàn cục → nếu không reset, lần tải này sẽ bị hủy oan ngay lập tức.
    download_cancel_requested = False
    download_timeout_stalled = False

    # TẮT hf_xet trong suốt lần tải: xet (Rust) tải file lớn vào cache riêng ở
    # nơi khác + bỏ qua hook Python (tqdm/requests) → progress đứng 0% và KHÔNG hủy
    # được giữa chừng. Tắt xet → dùng http_get chuẩn: tqdm.update chạy theo chunk
    # (đo byte thật + hủy được), file .incomplete nằm trong thư mục đích (monitor
    # thấy). is_xet_available() đọc cờ này động mỗi lần nên patch runtime là đủ.
    import huggingface_hub.constants as _hf_const

    _prev_xet = _hf_const.HF_HUB_DISABLE_XET
    _prev_xet_env = os.environ.get("HF_HUB_DISABLE_XET")
    _hf_const.HF_HUB_DISABLE_XET = True
    os.environ["HF_HUB_DISABLE_XET"] = "1"

    # BẬT lại mạng cho HuggingFace trong suốt lần tải. VieNeu (vieneu_service.
    # _ensure_hf_env) đặt HF_HUB_OFFLINE=1 TOÀN CỤC để chạy offline; huggingface_hub
    # đọc cờ này thành HẰNG SỐ module constants.HF_HUB_OFFLINE nên chỉ pop env var là
    # KHÔNG đủ (hằng số vẫn True) → tải model báo "outgoing traffic has been disabled".
    # Ép CẢ hằng số LẪN env về online, khôi phục ở finally để VieNeu tiếp tục chạy
    # offline sau đó.
    _prev_offline_const = _hf_const.HF_HUB_OFFLINE
    _prev_offline_env = os.environ.get("HF_HUB_OFFLINE")
    _prev_tr_offline_env = os.environ.get("TRANSFORMERS_OFFLINE")
    _hf_const.HF_HUB_OFFLINE = False
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["TRANSFORMERS_OFFLINE"] = "0"

    try:
        max_retries = 2
        for attempt in range(1, max_retries + 1):
            try:
                download_start_time = time.time()
                with _dl_bytes_lock:
                    _dl_bytes_downloaded = 0
                try:
                    _do_download(model_key)
                finally:
                    # LUÔN dừng monitor thread của lần này (kể cả lỗi/hủy/retry) để
                    # không rò thread → không có ai bật cờ hủy toàn cục về sau.
                    if _monitor_stop is not None:
                        _monitor_stop.set()
                    if _monitor_thread is not None:
                        _monitor_thread.join(timeout=2)

                # NGHIỆM THU trước khi báo thành công: _do_download có thể kết thúc
                # "êm" mà file trên đĩa chưa đủ (checkpoint không copy được, AV xóa,
                # ghi dở...). Không kiểm thì UI toast "Đã tải xong" nhưng model vẫn
                # không dùng được — người dùng chỉ phát hiện khi chạy tính năng.
                # Xóa cache đo size VieNeu để nghiệm thu đo tươi (cache 60s có thể
                # còn giữ kết quả đo TRƯỚC khi tải).
                _vieneu_size_cache["t"] = 0.0
                if not _is_model_installed(model_key):
                    raise UserFacingError(L(
                        "Tải xong nhưng kiểm tra lại thấy model chưa đầy đủ trên đĩa. "
                        "Vui lòng bấm 'Tải về' thử lại; nếu vẫn lỗi, kiểm tra phần mềm "
                        "diệt virus hoặc dung lượng ổ đĩa.",
                        "The download finished but the model looks incomplete on disk. "
                        "Please click 'Download' to try again; if it still fails, check your "
                        "antivirus software or disk space.",
                    ))

                with download_lock:
                    download_state["status"] = "completed"
                    download_state["progress"] = 100
                    download_state["current_model"] = ""
                    download_state["speed_mbps"] = 0.0
                # Xoá cache đo size VieNeu: cache có thể còn giữ ok=False đo TRƯỚC
                # khi tải — không xoá thì /status báo "chưa cài" tới 60s sau khi
                # tải xong (UI vẫn hiện nút Tải về dù đã thành công).
                _vieneu_size_cache["t"] = 0.0
                download_cancel_requested = False
                logger.info(f"Model {model_key} downloaded successfully.")
                return

            except Exception as e:
                # Ưu tiên xét cờ hủy: HF/ThreadPool có thể bọc lại exception nên chuỗi
                # "CANCELLATION_REQUESTED" đôi khi bị che — cờ là nguồn tin cậy nhất.
                is_cancelled = (
                    download_cancel_requested
                    or "CANCELLATION_REQUESTED" in str(e)
                    or (
                        hasattr(e, "__cause__")
                        and e.__cause__
                        and "CANCELLATION_REQUESTED" in str(e.__cause__)
                    )
                )

                if is_cancelled and download_timeout_stalled:
                    # Timeout do mạng treo (KHÔNG phải người dùng hủy): báo LỖI rõ
                    # ràng thay vì lặng lẽ về idle. KHÔNG xóa gì cả — file .incomplete
                    # trong cache HF cho phép resume ở lần tải sau.
                    with download_lock:
                        download_state["status"] = "error"
                        download_state["error_message"] = L(
                            "Tải model quá lâu / mạng quá chậm (không có dữ liệu mới "
                            "trong 5 phút). Kiểm tra kết nối mạng rồi thử lại — phần "
                            "đã tải sẽ được tiếp tục, không phải tải lại từ đầu.",
                            "The model download is taking too long / the network is too slow (no new data "
                            "in 5 minutes). Check your network connection and try again — the part "
                            "already downloaded will resume, no need to start over.",
                        )
                        download_state["progress"] = 0
                        download_state["current_model"] = ""
                        download_state["speed_mbps"] = 0.0
                    download_cancel_requested = False
                    download_timeout_stalled = False
                    logger.error(f"Model {model_key} download stalled (timeout).")
                    return

                if is_cancelled:
                    with download_lock:
                        download_state["status"] = "idle"
                        download_state["progress"] = 0
                        download_state["current_model"] = ""
                        download_state["error_message"] = ""
                        download_state["downloaded_mb"] = 0.0
                        download_state["target_mb"] = 0.0
                        download_state["speed_mbps"] = 0.0
                    download_cancel_requested = False
                    logger.info(f"Model {model_key} download cancelled by user.")

                    try:
                        models_dir = get_models_dir()
                        if model_key.startswith("whisper_"):
                            size = model_key.replace("whisper_", "")
                            # Dùng dạng "large-v3" khớp đúng out_dir lúc tải
                            # (trước đây build "large_v3" nên dọn hụt).
                            path = os.path.join(
                                models_dir, "whisper", size.replace("_", "-")
                            )
                            if os.path.exists(path):
                                shutil.rmtree(path, ignore_errors=True)
                        elif model_key == "vieneu":
                            path = os.path.join(models_dir, "hf")
                            if os.path.exists(path):
                                shutil.rmtree(path, ignore_errors=True)
                        elif model_key == "demucs":
                            path = os.path.join(models_dir, "demucs")
                            if os.path.exists(path):
                                shutil.rmtree(path, ignore_errors=True)
                        elif model_key == "qwen3_tts":
                            #  CHỈ dọn tệp dở / chưa kiểm — tệp đã đủ + đúng SHA giữ
                            #  lại để lần sau tải tiếp (mỗi tệp gần 1 GB).
                            _don_qwen_do()
                    except Exception as cleanup_err:
                        logger.error(f"Failed to clean up cancelled model {cleanup_err}")
                    return

                if attempt < max_retries:
                    logger.warning(
                        f"Download attempt {attempt} failed for {model_key}, retrying... Error: {e}",
                        exc_info=True
                    )
                    with download_lock:
                        download_state["error_message"] = (
                            f"Lần {attempt} thất bại, đang thử lại...: {_viet_error(e)}"
                        )
                    time.sleep(2)
                else:
                    logger.error(
                        f"Failed to download model {model_key} after {max_retries} attempts: {e}",
                        exc_info=True
                    )
                    with download_lock:
                        download_state["status"] = "error"
                        # Thông điệp tiếng Việt cho người dùng; lỗi gốc đã ghi log ở trên.
                        download_state["error_message"] = _viet_error(e)
                        download_state["progress"] = 0
                        download_state["current_model"] = ""
                        download_state["downloaded_mb"] = 0.0
                        download_state["target_mb"] = 0.0
                        download_state["speed_mbps"] = 0.0
                    download_cancel_requested = False
    finally:
        # Khôi phục cờ xet về trạng thái trước đó (không ảnh hưởng phần còn lại của app).
        _hf_const.HF_HUB_DISABLE_XET = _prev_xet
        if _prev_xet_env is None:
            os.environ.pop("HF_HUB_DISABLE_XET", None)
        else:
            os.environ["HF_HUB_DISABLE_XET"] = _prev_xet_env

        # Khôi phục cờ offline về đúng trạng thái trước khi tải (trả VieNeu về offline).
        _hf_const.HF_HUB_OFFLINE = _prev_offline_const
        if _prev_offline_env is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = _prev_offline_env
        if _prev_tr_offline_env is None:
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
        else:
            os.environ["TRANSFORMERS_OFFLINE"] = _prev_tr_offline_env


def _don_qwen_do() -> None:
    """Xoá mọi thứ trong thư mục mô hình Qwen TRỪ tệp GGUF đã đủ byte + đúng SHA (và
    dấu đã kiểm của nó): tệp .incomplete, thư mục .cache của HF, tệp sai mã băm."""
    from services import qwen_models as QM

    qdir = _qwen_models_dir()
    if not os.path.isdir(qdir):
        return
    mark = ".sha256_ok"
    for f in os.listdir(qdir):
        fp = os.path.join(qdir, f)
        if f in QM.QWEN_FILES and QM.file_ok(qdir, f):
            continue
        if f.endswith(mark) and f[: -len(mark)] in QM.QWEN_FILES and QM.file_ok(qdir, f[: -len(mark)]):
            continue
        if os.path.isdir(fp):
            shutil.rmtree(fp, ignore_errors=True)
        else:
            try:
                os.remove(fp)
            except OSError:
                pass


def _is_model_installed(model_key: str) -> bool:
    """Model da tai xong chua (theo marker/file that), de bo qua tai lai."""
    if model_key.startswith("whisper_"):
        return check_whisper_downloaded(model_key.replace("whisper_", ""))
    if model_key == "vieneu":
        return check_vieneu_downloaded()
    if model_key == "demucs":
        return check_demucs_downloaded()
    if model_key == "qwen3_tts":
        return check_qwen3_tts_downloaded()
    return False


def _dat_trang_thai_bat_dau(model: str) -> None:
    """Đặt download_state sang "đang tải model này". GỌI DƯỚI ``download_lock``."""
    download_state["status"] = "downloading"
    download_state["progress"] = 0
    download_state["current_model"] = model
    download_state["error_message"] = ""
    download_state["downloaded_mb"] = 0.0
    download_state["target_mb"] = MODEL_SIZES.get(model, 0.0)
    download_state["speed_mbps"] = 0.0


def _go_khoi_danh_sach_don(model: str) -> None:
    """Gỡ thư mục đích khỏi danh sách "dọn khi khởi động lại" (nếu vừa xóa model dở —
    file khóa). Không gỡ thì lần boot sau sẽ XÓA NHẦM bản vừa tải mới."""
    try:
        from utils.storage_paths import remove_pending_cleanup
        models_dir = get_models_dir()
        if model.startswith("whisper_"):
            size_std = model.replace("whisper_", "").replace("_", "-")
            remove_pending_cleanup(os.path.join(models_dir, "whisper", size_std))
        elif model == "vieneu":
            remove_pending_cleanup(os.path.join(models_dir, "hf"))
        elif model == "demucs":
            remove_pending_cleanup(os.path.join(models_dir, "demucs"))
        elif model == "qwen3_tts":
            remove_pending_cleanup(_qwen_models_dir())
    except Exception:
        pass


#  Cho luồng SSE (poll 0,5 s) kịp thấy trạng thái cuối (completed / error / idle) của
#  model vừa xong trước khi sang model kế — không thì FE không bao giờ nhận được "đã tải
#  xong X" vì trạng thái nhảy thẳng sang "đang tải Y".
_NGHI_GIUA_HAI_MODEL = 1.2


def _chay_hang_doi(model_key: str) -> None:
    """Tải ``model_key`` rồi LẦN LƯỢT các model trong hàng đợi (cùng một luồng nền).

    Huỷ (``/cancel``) xoá sạch hàng đợi nên vòng lặp dừng sau model đang tải. Model lỗi
    không chặn model sau: báo lỗi xong vẫn tải tiếp cái kế — người dùng xếp 3 cái rồi đi
    làm việc khác, về thấy 2 cái xong + 1 cái lỗi còn hơn cả 3 cái đứng im.
    """
    while True:
        download_model_job(model_key)
        time.sleep(_NGHI_GIUA_HAI_MODEL)
        model_key = _lay_model_ke_tiep()
        if model_key is None:
            return
        _go_khoi_danh_sach_don(model_key)
        logger.info(f"Hàng đợi: bắt đầu tải {model_key} (còn chờ {len(download_state['queue'])}).")


def _lay_model_ke_tiep():
    """Rút model kế trong hàng đợi và đặt trạng thái "đang tải" cho nó; bỏ qua model đã
    có trên máy. ``None`` = hết hàng."""
    with download_lock:
        q = list(download_state.get("queue") or ())
        while q:
            m = q.pop(0)
            download_state["queue"] = tuple(q)
            if _is_model_installed(m):
                continue                  # đã có trên máy (vd vừa tải xong ở lượt trước)
            _dat_trang_thai_bat_dau(m)
            return m
        download_state["queue"] = ()
        return None


@router.post("/download")
def download_model(data: dict, background_tasks: BackgroundTasks):
    global download_state
    model = data.get("model")
    if not model or model not in MODEL_SIZES:
        raise HTTPException(status_code=400, detail="Invalid model identifier")

    # Bo qua tai lai neu da co (tranh tai thua ca GB). Truyen force=true de tai lai
    # chu dong (vd nghi model hong). Uu tien "giu cai da cai".
    if not data.get("force") and _is_model_installed(model):
        return {"status": "already_installed"}

    with download_lock:
        if download_state["status"] in ("downloading", "cancelling"):
            #  ĐANG TẢI CÁI KHÁC -> XẾP HÀNG, tự tải khi tới lượt (không bắt người dùng đợi
            #  rồi bấm lại). Cùng model đang tải / đã trong hàng thì không xếp hai lần.
            if download_state["current_model"] == model:
                return {"status": "already_running"}
            q = list(download_state.get("queue") or ())
            if model in q:
                return {"status": "queued", "position": q.index(model) + 1}
            q.append(model)
            download_state["queue"] = tuple(q)
            logger.info(f"Xếp hàng tải {model} (vị trí {len(q)}).")
            return {"status": "queued", "position": len(q)}

        _dat_trang_thai_bat_dau(model)

    _go_khoi_danh_sach_don(model)
    background_tasks.add_task(_chay_hang_doi, model)
    return {"status": "started"}


@router.post("/queue/remove")
def remove_queued_model(data: dict):
    """Bỏ một model khỏi hàng đợi (chưa bắt đầu tải)."""
    model = data.get("model")
    with download_lock:
        q = list(download_state.get("queue") or ())
        if model not in q:
            return {"status": "not_queued"}
        q.remove(model)
        download_state["queue"] = tuple(q)
    return {"status": "removed"}


def _download_state_snapshot() -> dict:
    """Bản sao nhất quán của download_state (đọc dưới lock, tránh torn read khi
    monitor thread đang ghi nhiều field)."""
    with download_lock:
        return dict(download_state)


@router.get("/download-progress")
def get_download_progress():
    return _download_state_snapshot()


@router.get("/download-progress/stream")
async def stream_download_progress():
    async def event_generator():
        last_status = None
        while True:
            with download_lock:
                current = dict(download_state)

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


@router.post("/cancel")
def cancel_download():
    global download_cancel_requested, download_state
    with download_lock:
        #  Huỷ = dừng cả hàng đợi: người dùng bấm Huỷ tải là muốn máy thôi tải, không phải
        #  "bỏ cái này rồi tải tiếp cái sau".
        download_state["queue"] = ()
        if download_state["status"] in ("downloading", "cancelling"):
            download_cancel_requested = True
            # Đổi trạng thái NGAY để FE phản hồi tức thì ("Đang hủy…") thay vì đợi
            # tới khi thread tải thực sự dừng (có thể kẹt ở lần đọc mạng / 504).
            download_state["status"] = "cancelling"
            return {"status": "cancel_pending"}
    return {"status": "not_downloading"}


@router.post("/delete")
def delete_model(data: dict):
    model = data.get("model")
    if not model or model not in MODEL_SIZES:
        raise HTTPException(status_code=400, detail="Invalid model identifier")

    # Chặn xóa đúng model đang tải dở: rmtree sẽ đua với luồng download đang ghi
    # file → hỏng dữ liệu / lỗi khó lần. Bắt hủy tải trước.
    with download_lock:
        if (
            download_state["status"] == "downloading"
            and download_state["current_model"] == model
        ):
            raise HTTPException(
                status_code=409,
                detail=L("Model đang được tải, hãy hủy tải trước khi xóa.", "The model is being downloaded — cancel the download before deleting."),
            )
        #  Đang chờ trong hàng đợi thì rút khỏi hàng — xoá xong mà vẫn tự tải lại thì vô lý.
        q = list(download_state.get("queue") or ())
        if model in q:
            q.remove(model)
            download_state["queue"] = tuple(q)

    models_dir = get_models_dir()

    def _force_delete(path: str) -> bool:
        """Xóa thư mục model; nếu file đang bị KHÓA (model đã nạp trong phiên này)
        thì đăng ký dọn ở lần khởi động sau. Trả True = đã sạch ngay."""
        if not os.path.exists(path):
            return True
        shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):
            from utils.storage_paths import add_pending_cleanup
            add_pending_cleanup(path)
            logger.warning(
                "[MODELS] '%s' đang bị khóa (model đã nạp) — sẽ xóa hẳn khi khởi động lại.",
                path,
            )
            return False
        return True

    leftover = False
    try:
        if model.startswith("whisper_"):
            size = model.replace("whisper_", "")
            size_std = size.replace("_", "-")
            path = os.path.join(models_dir, "whisper", size_std)
            leftover |= not _force_delete(path)

            # Clean up old folder style if it exists
            old_path = os.path.join(models_dir, "whisper", size)
            if old_path != path:
                leftover |= not _force_delete(old_path)

            # Nếu vừa xóa đúng size Whisper đang active → chuyển active sang size
            # khác còn cài (nếu có) để STT không lỗi runtime "thiếu model.bin".
            cfg_service = get_config_service()
            cfg = cfg_service.load_config()
            active_size = cfg.get("whisper_size", "base").replace("_", "-")
            if active_size == size_std:
                fallback = next(
                    (
                        alt
                        for alt in ["base", "medium", "large-v3"]
                        if alt != size_std and check_whisper_downloaded(alt)
                    ),
                    "base",
                )
                cfg["whisper_size"] = fallback
                cfg_service.save_config(cfg)
                logger.info(
                    "Đã xóa Whisper '%s' đang active → chuyển active sang '%s'.",
                    size_std,
                    fallback,
                )
        elif model == "vieneu":
            path = os.path.join(models_dir, "hf")
            leftover |= not _force_delete(path)
            # Xóa cache đo size để /status phản ánh NGAY (không đợi TTL 60s).
            _vieneu_size_cache["t"] = 0.0
        elif model == "demucs":
            path = os.path.join(models_dir, "demucs")
            leftover |= not _force_delete(path)
        elif model == "qwen3_tts":
            #  Máy chủ Qwen đang chạy giữ khoá tệp GGUF -> tắt TRƯỚC khi xoá.
            from services.qwen_tts_service import QwenTTSService

            QwenTTSService.stop()
            leftover |= not _force_delete(_qwen_models_dir())

        if leftover:
            return {
                "status": "pending_restart",
                "message": L(
                    "Một phần file đang được sử dụng (model đã nạp trong phiên này) "
                    "— sẽ được xóa hẳn khi khởi động lại ứng dụng. Sau đó có thể tải bản mới.",
                    "Some files are in use (the model was loaded in this session) "
                    "— they will be fully removed when the app restarts. You can download a new copy afterwards.",
                ),
            }
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _chon_whisper_vua_tai_neu_can(size: str) -> None:
    """Tải xong một cỡ Whisper mà cỡ ĐANG CHỌN chưa có trên máy -> chọn luôn cỡ vừa tải
    (ghi cấu hình), để sau này tải thêm cỡ khác cũng không âm thầm đổi cỡ đang dùng."""
    try:
        from utils.model_paths import get_whisper_model_path
        cfg_service = get_config_service()
        cfg = cfg_service.load_config()
        dang_chon = (cfg.get("whisper_size") or "base").replace("_", "-")
        if dang_chon != size and not get_whisper_model_path(dang_chon):
            cfg["whisper_size"] = size
            cfg_service.save_config(cfg)
            logger.info(f"Whisper '{dang_chon}' chưa tải -> tự chọn '{size}' vừa tải xong.")
    except Exception as e:
        logger.warning(f"Không tự chọn được Whisper vừa tải: {e}")


@router.post("/set-whisper-size")
def set_whisper_size(data: dict):
    size = data.get("size")
    if not size or size not in ["base", "medium", "large-v3"]:
        raise HTTPException(status_code=400, detail="Invalid Whisper size")

    cfg_service = get_config_service()
    config = cfg_service.load_config()
    config["whisper_size"] = size
    cfg_service.save_config(config)

    return {"status": "success", "whisper_size": size}
