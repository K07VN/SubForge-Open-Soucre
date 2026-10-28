from utils.i18n import L
import os


import sys

def get_models_dir() -> str:
    """Thu muc chua model AI.

    Uu tien bien moi truong SUBFORGE_MODELS_DIR. Neu khong co:
    - Trong dev mode, neu backend/ai_models co san model thi dung de test.
    - Trong moi truong prod / electron hoac dev khong co model san: dung C:/SubForge_Workstation_Data/ai_models
    """
    env_dir = os.environ.get("SUBFORGE_MODELS_DIR")
    if env_dir:
        return env_dir

    dev_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_models"
    )
    if os.path.isdir(dev_path) and not getattr(sys, 'frozen', False):
        try:
            if len(os.listdir(dev_path)) > 0:
                return dev_path
        except Exception:
            pass

    # Data root có thể do người dùng cấu hình (storage.json / SUBFORGE_DATA_ROOT).
    from utils.storage_paths import get_data_root
    path = os.path.join(get_data_root(), "ai_models")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def get_whisper_model_path(size: str = "base"):
    """Path model faster-whisper da bundle neu co, nguoc lai None (tai online)."""
    size_std = size.replace("_", "-")
    path = os.path.join(get_models_dir(), "whisper", size_std)

    # Auto-migration: rename old large_v3 folder to large-v3 if it exists
    old_path = os.path.join(get_models_dir(), "whisper", size.replace("-", "_"))
    if old_path != path and os.path.isdir(old_path) and not os.path.exists(path):
        try:
            os.rename(old_path, path)
        except Exception:
            pass

    # faster-whisper can it nhat model.bin trong thu muc model
    if os.path.isfile(os.path.join(path, "model.bin")):
        return path
    return None


WHISPER_SIZES = ("base", "medium", "large-v3")
#  model.bin nhỏ hơn mức này = đang tải dở / hỏng (xem check_whisper_downloaded).
WHISPER_MIN_BIN_MB = {
    "base": 100.0,
    "small": 400.0,
    "medium": 1200.0,
    "large-v3": 2500.0,
}


def _whisper_du_dung_luong(size: str) -> bool:
    path = get_whisper_model_path(size)
    if not path:
        return False
    try:
        mb = os.path.getsize(os.path.join(path, "model.bin")) / (1024 * 1024)
    except OSError:
        return False
    return mb >= WHISPER_MIN_BIN_MB.get(size, 50.0)


def whisper_size_hieu_luc(chosen: str) -> str:
    """Cỡ Whisper THẬT được dùng. Cỡ đang chọn đã tải -> dùng nó. Chưa tải mà máy có cỡ
    khác -> dùng cỡ đã có (nhẹ trước), vì người dùng tải Medium mà cấu hình vẫn ở mặc
    định "base" thì trước đây STT báo thiếu Base và trang chủ đòi tải Base — dù trên máy
    đã có model dùng được. Không có cỡ nào -> trả cỡ đang chọn để báo lỗi đúng tên."""
    chosen = (chosen or "base").replace("_", "-")
    if get_whisper_model_path(chosen):
        return chosen
    for alt in WHISPER_SIZES:
        #  Cỡ THAY THẾ phải tải xong hẳn — đang tải dở thì model.bin đã có mà còn thiếu.
        if alt != chosen and _whisper_du_dung_luong(alt):
            return alt
    return chosen


def vieneu_has_pytorch_weights(hf_dir: str) -> bool:
    """VieNeu co san model PyTorch (cho GPU/CUDA) trong thu muc HF khong.

    Ban v2 (.vieneu_ready_v2) bundle them file *.safetensors / pytorch_model*.bin de
    chay GPU; ban cu v1 (.vieneu_ready) CHI co ONNX -> thieu file nay -> clone tren GPU
    se tu lui ONNX/CPU (cham nhung van chay). Dung de phan biet v1 vs v2 thuc te."""
    try:
        for root, _dirs, files in os.walk(hf_dir):
            for f in files:
                fl = f.lower()
                if fl.endswith(".safetensors") or (
                    fl.startswith("pytorch_model") and fl.endswith(".bin")
                ):
                    return True
    except Exception:
        pass
    return False


def get_vieneu_hf_dir():
    """Thu muc HF cache da bundle cho VieNeu (ai_models/hf) neu co, nguoc lai None.

    Runtime set HF_HOME tro vao day + HF_HUB_OFFLINE=1 de VieNeu chay hoan toan
    offline (khong goi mang). Nhan biet qua marker .vieneu_ready do download_models tao.

    Chap nhan CA marker v1 (.vieneu_ready) lan v2 (.vieneu_ready_v2): ban v1 chi co
    ONNX van chay tot tren CPU nen KHONG ep tai lai (giu cai da cai). Neu la marker v1
    NHUNG thu muc thuc te da co PyTorch weight -> tu nang marker len v2 cho dong bo voi
    build-time (khong tai lai gi).
    """
    hf_dir = os.path.join(get_models_dir(), "hf")
    marker_v2 = os.path.join(hf_dir, ".vieneu_ready_v2")
    marker_v1 = os.path.join(hf_dir, ".vieneu_ready")

    if os.path.isfile(marker_v2):
        return hf_dir

    if os.path.isfile(marker_v1):
        # Tu nang v1 -> v2 neu da co san PyTorch weight (v2 thuc te, chi thieu marker).
        if not os.path.isfile(marker_v2) and vieneu_has_pytorch_weights(hf_dir):
            try:
                with open(marker_v2, "w", encoding="utf-8") as f:
                    f.write("ok")
            except Exception:
                pass
        return hf_dir

    return None


def get_vieneu_onnx_precision() -> str:
    """Chon do chinh xac cho backbone ONNX/CPU cua VieNeu: "int8" hay "fp32".

    vieneu >= 3.3.0 mac dinh doc thu muc `onnx_int8/` (nhanh ~3x, nho 4x). Bundle
    tai bang ban Model Manager CU co the CHUA co thu muc do (repo HF them sau) —
    ma runtime lai khoa HF_HUB_OFFLINE=1 -> engine chet han vi khong tim thay file.
    Ham nay do bundle thuc te: thieu int8 nhung co onnx_update thi lui ve "fp32"
    (cham hon nhung chay duoc, KHONG bat nguoi dung tai lai ~1.7GB).

    Khong co bundle (chay online) -> "int8": engine tu tai dung thu muc can.
    """
    hf_dir = get_vieneu_hf_dir()
    if not hf_dir:
        return "int8"
    snapshots = os.path.join(
        hf_dir, "hub", "models--pnnbao-ump--VieNeu-TTS-v3-Turbo", "snapshots"
    )
    has_int8 = has_fp32 = False
    try:
        for rev in os.listdir(snapshots):
            rev_dir = os.path.join(snapshots, rev)
            if os.path.isfile(os.path.join(rev_dir, "onnx_int8", "vieneu_prefill.onnx")):
                has_int8 = True
            if os.path.isfile(os.path.join(rev_dir, "onnx_update", "vieneu_prefill.onnx")):
                has_fp32 = True
    except Exception:
        return "int8"

    if has_int8 or not has_fp32:
        return "int8"
    import logging as _logging
    _logging.getLogger("ModelPaths").warning(
        "Bundle VieNeu chưa có đồ thị ONNX int8 (bản tải cũ) — dùng fp32 để chạy "
        "được offline. Tải lại model VieNeu trong 'Quản lý Model AI' để nhanh hơn ~3x."
    )
    return "fp32"


def apply_vieneu_hf_env():
    """Trỏ HF cache về bundle VieNeu (ai_models/hf) + bật offline, cho engine chạy
    KHÔNG cần mạng. Trả về hf_dir nếu đã có bundle, ngược lại None.

    QUAN TRỌNG: huggingface_hub CHỐT HF_HUB_CACHE / HF_HUB_OFFLINE thành hằng số NGAY
    LÚC IMPORT (từ env lúc đó). Nếu chỉ đặt os.environ MUỘN (sau khi HF đã được import
    bởi transformers/faster-whisper…) thì KHÔNG đổi được nơi lưu → offline tìm trong
    cache mặc định (~/.cache/huggingface) → không thấy model bundle → lỗi "cannot find
    files in local cache". Vì vậy ngoài set env, ta VÁ THẲNG hằng số của huggingface_hub
    (và import sớm để chốt đúng nếu gọi trước mọi import HF). Idempotent, gọi nhiều lần OK.
    """
    hf_dir = get_vieneu_hf_dir()
    if not hf_dir:
        return None
    hub_cache = os.path.join(hf_dir, "hub")

    # Bundle "ĐỦ BỘ" = có cả codec MOSS Audio Tokenizer (repo riêng của OpenMOSS).
    # Bundle tải bằng Model Manager BẢN CŨ chỉ có repo chính -> nếu khoá offline sẽ
    # làm VieNeu chết hẳn sau nâng cấp app (mà UI vẫn báo "Đã cài"). Trường hợp đó:
    # vẫn trỏ cache về bundle nhưng ĐỂ ONLINE — engine tự tải phần thiếu VÀO bundle
    # (HF_HUB_CACHE đã trỏ đúng chỗ) -> lần khởi động sau đủ bộ thì tự bật offline.
    moss_ok = os.path.isdir(
        os.path.join(
            hub_cache, "models--OpenMOSS-Team--MOSS-Audio-Tokenizer-Nano-ONNX", "snapshots"
        )
    )

    # Local reconstruction: downloads belong only to provision_models.py.
    if os.environ.get("SUBFORGE_OFFLINE_ONLY") == "1" and not moss_ok:
        raise ModelNotDownloadedError("Thiếu codec MOSS trong bộ model VieNeu local. Chạy provision_models.py vieneu rồi thử lại; không tải ngầm khi chạy offline.")

    os.environ["HF_HOME"] = hf_dir
    if moss_ok:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        import logging as _logging
        _logging.getLogger("ModelPaths").warning(
            "Bundle VieNeu thiếu codec MOSS (bản tải cũ) — giữ chế độ ONLINE để tự tải "
            "bổ sung vào bundle ở lần dùng giọng tới (cần mạng 1 lần, ~90MB)."
        )
    try:
        import huggingface_hub.constants as c
        c.HF_HOME = hf_dir
        c.HF_HUB_CACHE = hub_cache
        if hasattr(c, "HUGGINGFACE_HUB_CACHE"):
            c.HUGGINGFACE_HUB_CACHE = hub_cache
        c.HF_HUB_OFFLINE = bool(moss_ok)
    except Exception:
        pass
    return hf_dir


def get_demucs_repo_path(model_name: str = "htdemucs"):
    """Thu muc repo demucs local (chua <model_name>.yaml + checkpoint .th) neu da
    bundle model do, nguoc lai None (demucs se tai online qua torch.hub)."""
    path = os.path.join(get_models_dir(), "demucs")
    
    has_th = False
    if os.path.isdir(path):
        try:
            for f in os.listdir(path):
                if f.endswith(".th"):
                    has_th = True
                    break
        except Exception:
            pass

    if os.path.isfile(os.path.join(path, f"{model_name}.yaml")) and has_th:
        return path
    return None


class ModelNotDownloadedError(RuntimeError):
    """Raise khi model/goi bat buoc chua duoc tai. Thay vi am tham tai online (khong
    co thanh tien trinh), bao loi RO RANG de nguoi dung sang muc 'Quan ly Model AI'
    tai truoc. Ke thua RuntimeError nen cac handler san co van bat duoc binh thuong."""
    pass


# Cau chi dan chung, gan cuoi moi thong bao loi thieu model.
class _HintTai:
    """Câu gợi ý tải model theo ngôn ngữ giao diện (chỉ dùng trong f-string)."""

    def __str__(self) -> str:
        return L("Vào mục 'Quản lý Model AI' (Dashboard) để tải, rồi thử lại.",
                 "Go to 'AI Model Manager' (Dashboard) to download, then try again.")


_GO_DOWNLOAD_HINT = _HintTai()


def require_whisper_model(size: str = "base") -> str:
    """Tra ve path model Whisper da tai; neu chua co -> ModelNotDownloadedError."""
    path = get_whisper_model_path(size)
    if path is None:
        raise ModelNotDownloadedError(
            L(f"Chưa tải model nhận dạng giọng nói (Whisper '{size}'). {_GO_DOWNLOAD_HINT}", f"The speech recognition model (Whisper '{size}') is not downloaded yet. {_GO_DOWNLOAD_HINT}")
        )
    return path


def require_vieneu_dir() -> str:
    """Tra ve thu muc HF cua VieNeu neu da tai; nguoc lai -> ModelNotDownloadedError."""
    path = get_vieneu_hf_dir()
    if path is None:
        raise ModelNotDownloadedError(
            L("Chưa tải model giọng AI (VieNeu TTS). "
            f"{_GO_DOWNLOAD_HINT} Hoặc chọn giọng Edge (cần mạng) để thay thế.", f"The AI voice model (VieNeu TTS) is not downloaded yet. {_GO_DOWNLOAD_HINT} Or pick an Edge voice (needs internet) instead.")
        )
    return path


def require_demucs_repo(model_name: str = "htdemucs") -> str:
    """Tra ve repo demucs local neu da tai; nguoc lai -> ModelNotDownloadedError."""
    path = get_demucs_repo_path(model_name)
    if path is None:
        raise ModelNotDownloadedError(
            L("Chưa tải model tách nhạc nền (Demucs). "
            f"{_GO_DOWNLOAD_HINT} Hoặc tắt tuỳ chọn tách nhạc nền.", f"The background-music separation model (Demucs) is not downloaded yet. {_GO_DOWNLOAD_HINT} Or turn off the music separation option.")
        )
    return path
