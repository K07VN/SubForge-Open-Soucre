"""TTS tieng Viet offline bang VieNeu-TTS (https://github.com/pnnbao97/VieNeu-TTS).

Bo sung ben canh edge-tts:
  - 14 giong preset v3 Turbo (Bac/Trung/Nam x nam/nu x tu nhien/ke chuyen/tin tuc), 48kHz
  - Voice cloning tu 3-8s audio mau (dang ky 1 lan, dung lai nhieu lan)
  - Chay offline tren CPU (ONNX engine, model 0.3B, ~realtime)

Quy uoc id giong (luu vao Video.voice_talent nhu id edge):
  - Preset : "vieneu:<Ten Preset>"        vd "vieneu:Trúc Ly"
  - Clone  : "vieneu-clone:<ten>"          vd "vieneu-clone:giong-cua-toi"

Model duoc vieneu tu tai tu HuggingFace lan dau su dung (~1GB, co cache).
Import vieneu RAT NANG (keo theo gradio...) -> chi import trong ham, khong o top.
"""
from utils.i18n import L
import json
import logging
import os
import re
import subprocess
import threading
import time
from utils.file_utils import safe_replace, safe_remove

import concurrent.futures
import imageio_ffmpeg

from models.database import get_app_data_dir

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger("TTSService")  # chung logger voi TTSService de vao synthesis.log

# Nguong canh bao "cat cut" — giu dong bo voi tts_service.TRIM_WARN_THRESHOLD_SEC.
# Dat cuc bo o day de tranh circular import (tts_service da import vieneu_service).
TRIM_WARN_THRESHOLD_SEC = 0.2

PRESET_PREFIX = "vieneu:"
CLONE_PREFIX = "vieneu-clone:"
# Thấp hơn mặc định thư viện (0.8/25/0.95/1.2): nhất quán giữa các câu, tránh lệch
# pitch/tốc độ khi ghép nhiều câu thành video liên tục (giọng không bị 'giật').
# ĐO (24-09-2026, tools/bench_tts_stability.py, 10 câu thuyết minh liền mạch, lệch
# giữa các câu — Trúc Ly / Phạm Tuyên):
#   0.6/20/0.92 (cũ): cao độ 1,30/1,23 nửa cung · tốc độ 10,3%/6,7% · sai chữ 4,5%/3,3%
#   0.5/15/0.90     : cao độ 1,01/1,03           · tốc độ 4,9%/7,7%  · sai chữ 1,9%/2,8%
#   0.4/10          : sai chữ tăng lại (3,6%) — ngẫu nhiên quá thấp bắt đầu nuốt chữ.
# Sinh lại theo seed cố định nên đổi số này KHÔNG đổi khoá lưu đệm VieNeu (golden test
# gác): câu đã sinh giữ nguyên, câu mới dùng thông số mới — chênh lệch nhỏ, không đáng
# bắt mọi dự án sinh lại toàn bộ.
#  THEO KHUYẾN NGHỊ CỦA NHÓM VIENEU (người dùng 13-10-2026): đúng mặc định SDK vieneu 3.3.0 ``V3TurboVieNeuTTS.infer`` và
#  thẻ model HF pnnbao-ump/VieNeu-TTS-v3-Turbo ("temperature around 0.8 gives the most stable result for v3 Turbo").
#  Bộ cũ 0.5/15/0.90/1.3 do mình tự đo 24-09-2026 (xem tools/bench_tts_stability.py, cấu hình t05 vs khuyen_nghi).
VIENEU_INFER_TEMPERATURE = 0.8
VIENEU_INFER_REPETITION_PENALTY = 1.2
VIENEU_INFER_TOP_K = 25
VIENEU_INFER_TOP_P = 0.95
VIENEU_INFER_MAX_NEW_FRAMES = 400     # SDK mặc định 300; giữ 400 tránh cụt câu dài (SDK vẫn tự chặn theo số âm vị)
# Bitrate và sample rate MP3 output — 192kbps + 48kHz native (VieNeu sinh 48kHz)
# tránh downsample 48->44.1 kHz gây mất tần số cao làm tiếng rè.
MP3_BITRATE = "192k"
MP3_SAMPLE_RATE = "48000"
# Ngưỡng VRAM tối thiểu để thử GPU (3050 4GB thường đủ; clone ưu tiên GPU hơn).
VIENEU_GPU_MIN_FREE_GB = 1.0
VIENEU_GPU_MIN_FREE_GB_CLONE = 1.8

# Danh sách 20 preset VieNeu v3 Turbo (đối chiếu voices_v3_turbo.json trong package).
# Hardcode để liệt kê KHÔNG cần load model (~10-30s).
#
# 14 giọng đầu có từ vieneu 3.1.0; 6 giọng cuối (đánh dấu SINCE_3_3_0) chỉ có từ
# vieneu 3.3.0 trở lên. Máy đang cài GÓI GPU CŨ nạp `vieneu` từ gói đó (xem
# main.py `_register_cuda_dll_dirs`: gpu_pack/_internal đứng đầu sys.path), nên 6
# giọng mới sẽ KHÔNG tồn tại trong engine -> `_resolve_voice` bắt và báo lỗi tiếng
# Việt mời cập nhật gói GPU, thay vì để ValueError tiếng Anh của thư viện lòi ra UI.
SINCE_3_3_0 = "3.3.0"
PRESET_VOICES = [
    {"name": "Trúc Ly", "label": "Trúc Ly — Nữ · Bắc · Tự nhiên"},
    {"name": "Đoan Trang", "label": "Đoan Trang — Nữ · Bắc · Tự nhiên"},
    {"name": "Ngọc Linh", "label": "Ngọc Linh — Nữ · Bắc · Kể chuyện"},
    {"name": "Mai Anh", "label": "Mai Anh — Nữ · Bắc · Tin tức"},
    {"name": "Phạm Tuyên", "label": "Phạm Tuyên — Nam · Bắc · Tự nhiên"},
    {"name": "Thanh Bình", "label": "Thanh Bình — Nam · Bắc · Kể chuyện"},
    {"name": "Minh Đức", "label": "Minh Đức — Nam · Bắc · Tin tức"},
    {"name": "Ngọc Trân", "label": "Ngọc Trân — Nữ · Trung · Tự nhiên"},
    {"name": "Quang Sơn", "label": "Quang Sơn — Nam · Trung · Tự nhiên"},
    {"name": "Thục Đoan", "label": "Thục Đoan — Nữ · Nam · Kể chuyện"},
    {"name": "Thùy Dung", "label": "Thùy Dung — Nữ · Nam · Tin tức"},
    {"name": "Xuân Vĩnh", "label": "Xuân Vĩnh — Nam · Nam · Tự nhiên"},
    {"name": "Thái Sơn", "label": "Thái Sơn — Nam · Nam · Kể chuyện"},
    {"name": "Minh Triết", "label": "Minh Triết — Nam · Nam · Tin tức"},
    {"name": "Ngọc Huyền", "label": "Ngọc Huyền — Nữ · Bắc · Tự nhiên", "since": SINCE_3_3_0},
    {"name": "Quỳnh Anh", "label": "Quỳnh Anh — Nữ · Bắc · Kể chuyện", "since": SINCE_3_3_0},
    {"name": "Mỹ Duyên", "label": "Mỹ Duyên — Nữ · Nam · Kể chuyện", "since": SINCE_3_3_0},
    {"name": "Kim Thanh", "label": "Kim Thanh — Nữ · Nam · Kể chuyện", "since": SINCE_3_3_0},
    {"name": "Adam", "label": "Adam — Nam · Nam · Tự nhiên", "since": SINCE_3_3_0},
    {"name": "Đức Trí", "label": "Đức Trí — Nam · Nam · Kể chuyện", "since": SINCE_3_3_0},
]


class VieneuError(RuntimeError):
    """Loi TTS VieNeu voi message tieng Viet, hien thang len UI."""
    pass


def is_vieneu_voice(voice: str) -> bool:
    return bool(voice) and (voice.startswith(PRESET_PREFIX) or voice.startswith(CLONE_PREFIX))


def _safe_name(name: str) -> str:
    """Ten file an toan cho giong clone (giu chu Viet, bo ky tu nguy hiem)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()
    return cleaned[:60] or "giong-moi"


class VieneuService:
    """Singleton quan ly model VieNeu + registry giong clone."""

    _tts = None
    _lock = threading.Lock()          # bao ve viec nap model
    _infer_lock = threading.Lock()    # model khong thread-safe -> sinh tuan tu
    _registered_clones = set()        # clone da add_voice vao model trong process nay
    _warming = False                  # dang warm-up nen (tranh spawn nhieu luong)
    # V3TurboVieNeuTTS khong set .device tren wrapper — chi co .backend + engine.device.
    # Tu theo doi de tranh detect sai -> upgrade GPU lap moi clip.
    _engine_is_onnx_cpu = True
    _clone_gpu_upgrade_attempted = False
    _clone_cpu_warned = False
    _gieo_key = None                  # (thư mục giọng, chữ ký manifest) đã gieo giọng mẫu dựng sẵn

    @classmethod
    def warmup_async(cls):
        """Nap model VieNeu o LUONG NEN (idempotent). Goi khi mo danh sach giong
        de luc bam 'nghe thu' model da san sang -> khong con doi ~10s cold-load."""
        if cls._tts is not None or cls._warming:
            return
        cls._warming = True

        def _run():
            try:
                cls._get_tts()
            except Exception:
                pass
            finally:
                cls._warming = False

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------ paths
    @classmethod
    def voices_dir(cls) -> str:
        d = os.path.join(get_app_data_dir(), "voices")
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def _registry_path(cls) -> str:
        return os.path.join(cls.voices_dir(), "registry.json")

    @classmethod
    def _load_registry(cls) -> dict:
        try:
            with open(cls._registry_path(), "r", encoding="utf-8") as f:
                reg = json.load(f)
        except Exception:
            reg = {}
        return cls._gieo_giong_mau(reg)

    @classmethod
    def _gieo_giong_mau(cls, reg: dict) -> dict:
        """Đưa bộ GIỌNG MẪU DỰNG SẴN (services/giong_mau_dung_san.py) vào registry của người dùng.
        Đi qua đường đọc registry duy nhất nên mọi nơi (danh sách giọng, sinh giọng, xoá…) đều
        thấy chúng, kể cả khi app mới mở mà dự án đã dùng sẵn một giọng mẫu. Lối nhanh: đã gieo
        đúng thư mục + đúng bản manifest và registry còn đủ tên -> không đụng đĩa."""
        try:
            from services import giong_mau_dung_san as GM

            key = (cls.voices_dir(), GM._sig())
            if cls._gieo_key == key and all(t in reg for t in GM.ten_giong()):
                return reg
            if GM.gieo(cls.voices_dir(), reg):
                cls._save_registry(reg)
            cls._gieo_key = key
        except Exception as e:  # noqa: BLE001 — lỗi bộ giọng mẫu không được làm hỏng giọng của người dùng
            logger.warning(L(f"VieNeu: không gieo được giọng mẫu dựng sẵn: {e}", f"VieNeu: could not seed the built-in sample voices: {e}"))
        return reg

    @classmethod
    def _save_registry(cls, reg: dict):
        with open(cls._registry_path(), "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=1)

    # ------------------------------------------------------------------ model
    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    @classmethod
    def _infer_kwargs(cls, voice_name: str, style: str | None = None) -> dict:
        kw = {
            "voice": voice_name,
            "temperature": VIENEU_INFER_TEMPERATURE,
            "top_k": VIENEU_INFER_TOP_K,
            "top_p": VIENEU_INFER_TOP_P,
            "repetition_penalty": VIENEU_INFER_REPETITION_PENALTY,
            "max_new_frames": VIENEU_INFER_MAX_NEW_FRAMES,
            "apply_watermark": False,  # tắt watermark Perth (không ảnh hưởng chất lượng thực)
            # vieneu >= 3.3.0 gộp chunk vào cùng forward trên PyTorch/GPU, mặc định
            # max_batch_size=32 — quá lớn cho card 4GB (3050) -> OOM ngay batch đầu.
            # Chốt theo VRAM trống giống batch của synthesize_batch. Trên ONNX/CPU
            # tham số bị bỏ qua; vieneu 3.1.0 (gói GPU cũ) nuốt qua **kwargs -> vô hại.
            "batch_size": cls._gpu_batch_size(),
        }
        if style:
            kw["style"] = style
        return kw

    @classmethod
    def _gpu_batch_size(cls) -> int:
        """Trần số chunk gộp vào một forward GPU. CPU/ONNX -> 1 (không batch)."""
        if cls._is_onnx_cpu_engine():
            return 1
        try:
            from utils.hardware_utils import get_cuda_tts_batch_size

            return max(1, get_cuda_tts_batch_size())
        except Exception:
            return 4

    @staticmethod
    def _fix_seed(text: str = "", voice_name: str = "") -> None:
        """Cố định seed theo nội dung câu + tên giọng.
        
        Seed = hash(text + voice) -> cùng câu/giọng luôn ra audio giống hệt
        (hỗ trợ render cache), câu khác nhau có seed khác -> tự nhiên hơn.
        """
        import hashlib
        raw = (text + "|§|" + voice_name).encode("utf-8", errors="replace")
        seed = int(hashlib.md5(raw).hexdigest()[:8], 16) % (2**31)
        try:
            import torch
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
        except Exception:
            pass
        try:
            import numpy as np
            np.random.seed(seed % (2**32))
        except Exception:
            pass

    @classmethod
    def _add_clone_voice(cls, tts, name: str, reg_entry: dict, reg_full: dict) -> None:
        """Đăng ký clone — lưu cache embedding để tránh re-encode (mất 20s mỗi lần)."""
        if "speaker_emb" in reg_entry and "codes" in reg_entry:
            import numpy as np
            tts._preset_voices[name] = {
                "description": f"{name} (Clone)",
                "gender": "",
                "style": "tu_nhien",
                "speaker_emb": np.array(reg_entry["speaker_emb"], dtype=np.float32),
                "codes": np.array(reg_entry["codes"], dtype=np.int64) if reg_entry["codes"] is not None else None,
            }
            if not tts._default_voice:
                tts._default_voice = name
            return

        wav_path = reg_entry["file"]
        try:
            tts.add_voice(name, wav_path, denoise=True)
        except TypeError:
            tts.add_voice(name, wav_path)
            
        # Lưu cache lại vào registry để lần sau nạp ngay lập tức
        import numpy as np
        v = tts._preset_voices.get(name)
        if v:
            emb = v.get("speaker_emb")
            codes = v.get("codes")
            reg_entry["speaker_emb"] = [round(float(x), 6) for x in np.asarray(emb).reshape(-1)] if emb is not None else None
            reg_entry["codes"] = np.asarray(codes, dtype=int).tolist() if codes is not None else None
            cls._save_registry(reg_full)

    @classmethod
    def _detect_onnx_cpu(cls, tts) -> bool:
        """True neu engine dang ONNX/CPU. V3Turbo chi set .backend (khong co .device)."""
        if tts is None:
            return True
        try:
            be = str(getattr(tts, "backend", getattr(tts, "_backend", ""))).lower()
            if be == "pytorch":
                return False
            if be == "onnx":
                return True
            eng = getattr(tts, "engine", None)
            if eng is not None:
                d = getattr(eng, "device", None)
                if d is not None:
                    dtype = getattr(d, "type", None)
                    if dtype is not None:
                        return str(dtype).lower() == "cpu"
                    ds = str(d).lower()
                    return "cuda" not in ds and "cpu" in ds
            # Khong dung getattr(..., "cpu"): V3Turbo khong co .device -> luon sai.
            if hasattr(tts, "device"):
                ds = str(tts.device).lower()
                return "cuda" not in ds and "cpu" in ds
        except Exception:
            pass
        return True

    @classmethod
    def _is_onnx_cpu_engine(cls) -> bool:
        if cls._tts is None:
            return True
        # Uu tien flag tu _build_engine (chinh xac); dong bo lai neu lech.
        detected = cls._detect_onnx_cpu(cls._tts)
        cls._engine_is_onnx_cpu = detected
        return detected

    @classmethod
    def _ensure_hf_env(cls) -> None:
        """Dat HF_HOME + offline + VA hang so huggingface_hub truoc khi khoi tao engine.

        Dung helper dung chung: ngoai set env con va thang HF_HUB_CACHE/HF_HUB_OFFLINE
        (huggingface_hub chot cache luc import) — xu ly ca truong hop tai model GIUA
        PHIEN roi dung ngay (luc do env dat muon khong con tac dung)."""
        from utils.model_paths import apply_vieneu_hf_env

        apply_vieneu_hf_env()

    @classmethod
    def release_gpu(cls) -> None:
        """Nhả engine VieNeu khỏi VRAM để nhường GPU cho việc khác (vd Demucs) trên
        card nhỏ (4GB). Lần TTS sau tự nạp lại engine (chấp nhận cold-load)."""
        with cls._lock:
            if cls._tts is not None:
                cls._release_engine(cls._tts)
                cls._tts = None
                # Engine mat -> moi clone da add_voice cung mat theo. KHONG clear
                # se lam _resolve_voice skip dang ky lai o engine moi -> lan TTS
                # sau bao "Voice not found" (vd render review lan 2 sau Demucs).
                cls._registered_clones.clear()

    @classmethod
    def _release_engine(cls, eng) -> None:
        """Giai phong engine cu truoc khi nap engine moi (tranh doi VRAM tren 4GB)."""
        if eng is None:
            return
        try:
            del eng
        except Exception:
            pass
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    @classmethod
    def _try_upgrade_engine_for_clone(cls) -> None:
        """Clone chạy nhanh và ổn định hơn trên PyTorch/GPU — thử nâng cấp một lần/process."""
        if not cls._cuda_available() or not cls._is_onnx_cpu_engine():
            return
        if cls._clone_gpu_upgrade_attempted:
            return
        cls._ensure_hf_env()
        with cls._lock:
            if not cls._is_onnx_cpu_engine() or cls._clone_gpu_upgrade_attempted:
                return
            cls._clone_gpu_upgrade_attempted = True
            from utils.hardware_utils import get_cuda_vram_free_gb

            free_gb = get_cuda_vram_free_gb()
            if free_gb is not None and free_gb < VIENEU_GPU_MIN_FREE_GB_CLONE:
                # Cho phep thu lai sau khi Whisper/process khac giai phong VRAM.
                cls._clone_gpu_upgrade_attempted = False
                logger.warning(
                    f"VieNeu: VRAM trong ~{free_gb:.1f}GB (<{VIENEU_GPU_MIN_FREE_GB_CLONE}GB) — "
                    "giu ONNX/CPU cho clone (tranh treo/OOM). Giai phong Whisper GPU truoc TTS neu can."
                )
                return
            try:
                from vieneu import Vieneu

                logger.warning(
                    L("VieNeu: giọng clone đang dùng ONNX/CPU — đang thử chuyển sang "
                    "PyTorch/GPU (khuyến nghị VieNeu cho voice cloning).", "VieNeu: the clone voice is running on ONNX/CPU — trying to switch to PyTorch/GPU (recommended by VieNeu for voice cloning).")
                )
                old = cls._tts
                cls._tts = None
                cls._release_engine(old)
                new_eng = cls._build_engine(Vieneu, for_clone=True)
                cls._tts = new_eng
                cls._registered_clones.clear()
                if cls._is_onnx_cpu_engine():
                    logger.warning(
                        L("VieNeu: nâng cấp GPU không thành công, giữ ONNX/CPU.", "VieNeu: GPU upgrade failed, keeping ONNX/CPU.")
                    )
                    return
                logger.info(L("VieNeu: đã nâng cấp engine clone lên GPU (PyTorch/CUDA).", "VieNeu: upgraded the clone engine to GPU (PyTorch/CUDA)."))
            except Exception as e:
                logger.warning(
                    L(f"VieNeu: không nâng cấp được GPU cho clone, giữ ONNX/CPU: {e}", f"VieNeu: could not upgrade the clone to GPU, keeping ONNX/CPU: {e}")
                )

    @classmethod
    def _build_engine(cls, Vieneu, *, for_clone: bool = False):
        """Tao engine VieNeu: uu tien PyTorch/CUDA (card roi) neu du dieu kien,
        nguoc lai ONNX/CPU. Loi GPU -> tu dong lui ONNX/CPU."""
        cls._ensure_hf_env()
        # vieneu >= 3.3.0 nhan them precision/max_batch_size; vieneu 3.1.0 (may dang
        # giu GOI GPU CU nap thu vien tu goi do) nuot qua **kwargs nen VO HAI.
        extra = cls._engine_kwargs()
        min_free = VIENEU_GPU_MIN_FREE_GB_CLONE if for_clone else VIENEU_GPU_MIN_FREE_GB
        if cls._cuda_available():
            try:
                import torch
                name = torch.cuda.get_device_name(0)
                try:
                    free_b, _ = torch.cuda.mem_get_info()
                    free_gb = free_b / (1024 ** 3)
                    if free_gb < min_free:
                        logger.warning(
                            f"VieNeu: VRAM trong chi ~{free_gb:.1f}GB (<{min_free}GB) -> "
                            "dung ONNX/CPU cho an toan (tranh OOM)."
                        )
                        raise RuntimeError("VRAM thap, uu tien CPU")
                except RuntimeError:
                    raise
                except Exception:
                    pass
                logger.info(f"VieNeu: thu chay GPU (PyTorch/CUDA) tren card roi: {name}")
                try:
                    import torch
                    torch.backends.cudnn.benchmark = True
                except Exception:
                    pass
                eng = Vieneu(mode="v3turbo", backend="pytorch", device="cuda", **extra)
                cls._engine_is_onnx_cpu = False
                logger.info("VieNeu: dang chay tren GPU (PyTorch/CUDA).")
                return eng
            except Exception as e:
                # Log KÈM traceback đầy đủ: lỗi kiểu "Could not import module
                # 'PreTrainedModel'" là lỗi transformers bọc lại lỗi gốc (peft/torch
                # /version mismatch…). Không có traceback thì không biết nguyên nhân
                # thật để sửa (vì sao GPU/clone không nạp được).
                logger.warning(
                    f"VieNeu: khong chay duoc GPU (thieu model PyTorch/OOM/driver: {e}). "
                    "Lui ve ONNX/CPU.",
                    exc_info=True,
                )
        eng = Vieneu(mode="v3turbo", backend="onnx", device="cpu", **extra)
        cls._engine_is_onnx_cpu = True
        logger.info(
            f"VieNeu: dang chay tren CPU (ONNX {extra.get('precision', 'int8')}, on dinh, offline)."
        )
        return eng

    @classmethod
    def _engine_kwargs(cls) -> dict:
        """Tham so khoi tao THEM cua vieneu >= 3.3.0 (3.1.0 bo qua qua **kwargs)."""
        from utils.model_paths import get_vieneu_onnx_precision

        kw = {"precision": get_vieneu_onnx_precision()}
        # Tran chunk/forward tren GPU — mac dinh 32 cua thu vien qua lon cho card 4GB.
        try:
            from utils.hardware_utils import get_cuda_tts_batch_size

            kw["max_batch_size"] = max(1, get_cuda_tts_batch_size())
        except Exception:
            pass
        return kw

    @classmethod
    def _get_tts(cls):
        if cls._tts is not None:
            return cls._tts
        with cls._lock:
            if cls._tts is not None:
                return cls._tts
            try:
                from utils.model_paths import require_vieneu_dir

                # BAT BUOC model VieNeu da tai. Neu chua -> ModelNotDownloadedError
                # (KHONG am tham tai ~1GB online). Chi anh huong khi THUC SU dung giong
                # VieNeu/clone; STT va Edge-TTS khong goi _get_tts nen khong bi chan.
                # warmup_async nuot loi nay nen khong tai ngam khi thieu model.
                hf_dir = require_vieneu_dir()
                cls._ensure_hf_env()
                logger.info(f"VieNeu: dung model da tai (offline) tai {hf_dir}")

                # KHONG cap OMP_NUM_THREADS: so luong CPU khong quyet dinh RAM
                # (RAM da duoc bao ve boi _infer_lock -> chi 1 infer/lan). Cap luong
                # chi lam CHAM. De onnxruntime dung toan bo loi cho nhanh nhat.
                from vieneu import Vieneu

                # Chon backend AN TOAN 2 chieu:
                #  - May co CUDA (card roi NVIDIA) VA da bundle model PyTorch
                #    -> chay GPU (nhanh hon).
                #  - Nguoc lai (CPU-only, hoac GPU nhung THIEU model PyTorch)
                #    -> ONNX/CPU tu bo model da bundle (on dinh, chay offline).
                # Neu thu GPU that bai (thieu model / OOM / driver) -> TU DONG lui
                # ve ONNX/CPU, KHONG lam chet tinh nang. Card Intel 500MB khong ho
                # tro CUDA nen khong bao gio bi chon nham — CUDA chi thay card roi.
                cls._tts = cls._build_engine(Vieneu)
            except Exception as e:
                raise VieneuError(
                    L("Không khởi động được giọng đọc VieNeu (cần internet ở lần chạy "
                    f"đầu để tải model, hoặc cài lại bản đầy đủ). Chi tiết: {e}", f"Could not start the VieNeu voice (internet is needed on the first run to download the model, or reinstall the full version). Details: {e}")
                )
            return cls._tts

    # ------------------------------------------------------------------ voices
    @classmethod
    def list_voices(cls) -> dict:
        """Danh sach giong (KHONG can load model): preset hardcode + clone tu registry."""
        presets = [
            {"id": f"{PRESET_PREFIX}{v['name']}", "label": v["label"]}
            for v in PRESET_VOICES
        ]
        # ``missing``: giong con trong danh sach nhung KHONG con dung duoc (mat ca tep mau
        # lan cache embedding). Phai noi ra o day, khong thi picker liet ke du ma chon vao
        # la sinh giong that bai hang loat, nguoi dung khong hieu vi sao.
        reg = cls._load_registry()
        from services import giong_mau_dung_san as GM

        mau_ten = {v["id"] for v in GM.danh_sach()}
        clones = [
            {
                "id": f"{CLONE_PREFIX}{name}",
                "label": L(f"{name} — Giọng của tôi", f"{name} — My voice"),
                "missing": not cls.clone_usable(name, reg),
            }
            for name in sorted(reg.keys())
            if name not in mau_ten or not (reg.get(name) or {}).get("builtin")
        ]
        #  Giọng mẫu dựng sẵn của SubForge: nhóm riêng, theo thứ tự trong manifest.
        mau = [
            {
                "id": f"{CLONE_PREFIX}{v['id']}",
                "label": GM.nhan(v),
                "name": v.get("hien_thi") or v["id"],
                "gender": "nam" if str(v.get("gioi_tinh") or "").lower() == "nam" else "nữ",
                "builtin": True,
                "missing": not cls.clone_usable(v["id"], reg),
            }
            for v in GM.danh_sach()
            if (reg.get(v["id"]) or {}).get("builtin")
        ]
        return {"presets": presets, "clones": clones, "builtin": mau}

    @classmethod
    def clone_file(cls, name: str, reg: dict = None) -> str:
        """Duong dan tep mau giong CON DUNG DUOC cua mot clone ('' neu that su mat).

        VI SAO CAN: registry luu duong dan TUYET DOI. Doi "vi tri luu du lieu" (hoac nang
        cap tu ban cu von luu o ``%APPDATA%\\SubForge\\voices``) thi cac tep .wav duoc DOI
        SANG kho moi, con duong dan trong registry van tro cho cu. Hau qua da gap that:
        ``list_voices`` doc theo KHOA nen picker van liet ke du giong, nguoi dung chon
        xong thi ``_resolve_voice`` bao "Giong clone khong ton tai (da bi xoa?)" — sinh
        giong that bai hang loat trong khi tep mau con nguyen ven trong kho moi.

        Tim lai theo TEN TEP trong kho giong hien hanh; tim duoc thi SUA LUON registry de
        lan sau khong phai do nua. Cung cach da lam cho preset compositor
        (``compositor_preset_service.resolve_asset_path``).
        """
        reg = cls._load_registry() if reg is None else reg
        info = reg.get(name)
        if not isinstance(info, dict):
            return ""
        f = info.get("file") or ""
        if f and os.path.exists(f):
            return f
        base = os.path.basename(f) if f else f"{name}.wav"
        candidate = os.path.join(cls.voices_dir(), base)
        if not os.path.exists(candidate):
            # Thu ca ten theo KHOA: co ban cu dat ten tep khac ten giong.
            candidate = os.path.join(cls.voices_dir(), f"{name}.wav")
            if not os.path.exists(candidate):
                return ""
        info["file"] = candidate
        reg[name] = info
        try:
            cls._save_registry(reg)
            logger.info(
                L(f"Đã tìm lại tệp mẫu của giọng nhân bản '{name}' trong kho hiện hành "
                f"(đường dẫn cũ đã lỗi thời sau khi đổi vị trí lưu dữ liệu).", f"Found the sample file of the cloned voice '{name}' again in the current storage (the old path was outdated after the data location changed).")
            )
        except Exception:
            pass
        return candidate

    @classmethod
    def clone_usable(cls, name: str, reg: dict = None) -> bool:
        """Giong clone nay CON DUNG DUOC khong.

        Tep mau (.wav) chi can de TINH embedding. Registry da cache ``speaker_emb`` +
        ``codes`` thi ``_add_clone_voice`` dung thang cache, khong mo tep nua — nen thieu
        tep KHONG phai ly do chan.

        Truoc day cho nay chi xet ``os.path.exists(entry["file"])``, thanh ra sau khi doi
        vi tri luu du lieu thi MOI giong nhan ban deu bao "khong ton tai" du van dung
        duoc: duong dan cu het dung, ma embedding thi da co san trong registry. Loi that
        (2026-08-01): 4/4 giong clone chet, picker van liet ke du, sinh giong that bai
        hang loat.
        """
        reg = cls._load_registry() if reg is None else reg
        entry = reg.get(name)
        if not isinstance(entry, dict):
            return False
        if entry.get("speaker_emb") is not None and "codes" in entry:
            return True                      # co cache -> khong can tep mau
        return bool(cls.clone_file(name, reg))

    @classmethod
    def _clear_preview_cache(cls, name: str) -> None:
        """Xoa cache 'nghe thu' cua 1 giong clone (goi khi xoa HOAC ghi de giong)."""
        try:
            voice_id = f"{CLONE_PREFIX}{name}"
            voice_safe = "".join([c if c.isalnum() or c in "-_" else "_" for c in voice_id])
            preview_dir = os.path.join(get_app_data_dir(), "ai_models", "tts_preview")
            if os.path.exists(preview_dir):
                for f in os.listdir(preview_dir):
                    if f.startswith(voice_safe):
                        try:
                            os.remove(os.path.join(preview_dir, f))
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Failed to clear preview cache for {name}: {e}")

    @classmethod
    def add_clone(cls, name: str, src_audio_path: str) -> str:
        """Luu audio mau + dang ky giong clone. Tra ve voice id."""
        name = _safe_name(name)
        from services import giong_mau_dung_san as GM

        if GM.la_giong_mau(name):
            raise VieneuError(
                L(f"Tên '{name}' trùng với một giọng mẫu của SubForge. Vui lòng đặt tên khác.", f"The name '{name}' conflicts with a SubForge sample voice. Please choose another name.")
            )
        # Chan ten trung preset: add_voice se GHI DE preset trong engine ->
        # nguoi dung chon preset nhung nghe ra giong clone (sai lech kho hieu).
        if any(p["name"].casefold() == name.casefold() for p in PRESET_VOICES):
            raise VieneuError(
                L(f"Tên '{name}' trùng với giọng có sẵn của hệ thống. Vui lòng đặt tên khác.", f"The name '{name}' conflicts with a built-in system voice. Please choose another name.")
            )
        reg = cls._load_registry()
        # Ghi de giong cu cung ten -> xoa cache nghe thu cu (tranh nghe ra giong cu).
        if name in reg:
            cls._clear_preview_cache(name)
        # Chuan hoa audio mau ve wav mono 24kHz, cat toi da 10s (vieneu tu trim 8s + denoise)
        dst = os.path.join(cls.voices_dir(), f"{name}.wav")
        cmd = [FFMPEG_EXE, "-y", "-i", src_audio_path, "-t", "10",
               "-ac", "1", "-ar", "24000", "-acodec", "pcm_s16le", dst]
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", close_fds=True)
        if res.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 1000:
            raise VieneuError(L(f"File audio mẫu không đọc được: {res.stderr[-300:]}", f"The sample audio file cannot be read: {res.stderr[-300:]}"))

        reg[name] = {"file": dst}
        cls._save_registry(reg)
        # ENCODE NGAY luc them giong (nap model neu chua) -> lan bam "nghe thu"
        # sau do chi con INFER (nhanh). Neu KHONG lam o day, encode (~20s) se bi
        # don sang lan preview dau -> nghe rat lau. Dat chi phi vao dung luc
        # nguoi dung cho "them giong". Ham nay duoc goi qua asyncio.to_thread
        # nen khong nghen server.
        try:
            tts = cls._get_tts()
            with cls._infer_lock:
                cls._add_clone_voice(tts, name, reg[name], reg)
            cls._registered_clones.add(name)
            logger.info(L(f"Đã tạo xong giọng nhân bản '{name}' (sẵn sàng nghe ngay).", f"Finished creating the cloned voice '{name}' (ready to listen right away)."))
        except Exception as e:
            logger.warning(
                L(f"VieNeu: chưa encode được giọng clone '{name}' (sẽ encode khi nghe lần đầu): {e}", f"VieNeu: could not encode the clone voice '{name}' yet (it will be encoded on first listen): {e}"),
                exc_info=True
            )
        return f"{CLONE_PREFIX}{name}"

    @classmethod
    def remove_clone(cls, name: str) -> bool:
        name = _safe_name(name)
        from services import giong_mau_dung_san as GM

        if GM.la_giong_mau(name):
            raise VieneuError(L("Giọng mẫu của SubForge là giọng dựng sẵn nên không xoá được.", "SubForge sample voices are built in and cannot be deleted."))
        reg = cls._load_registry()
        if name not in reg:
            return False
        try:
            os.remove(reg[name]["file"])
        except Exception:
            pass
        del reg[name]
        cls._save_registry(reg)
        cls._registered_clones.discard(name)

        # Clear cached preview files for this clone
        cls._clear_preview_cache(name)

        return True

    # ------------------------------------------------------------------ synth
    @classmethod
    def synthesize(cls, text: str, voice_id: str, output_mp3_path: str) -> bool:
        """Sinh giong doc -> file MP3 tai output_mp3_path. Ham SYNC (goi qua to_thread).

        - Preset : infer(voice=<ten>)
        - Clone  : dang ky add_voice 1 lan/process roi infer(voice=<ten>)
        """
        text = (text or "").strip()
        if not text:
            return False

        # Chuẩn hoá thẻ [ngập ngừng] và [tặc lưỡi] thành khoảng dừng ngập ngừng (...) cho VieNeu AI
        text = re.sub(r'\[ngập ngừng\]', '... ', text, flags=re.IGNORECASE)
        text = re.sub(r'\[tặc lưỡi\]', '... ', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+', ' ', text).strip()

        # Upgrade GPU (neu can) chi trong _resolve_voice — tranh goi 2 lan/clip.
        tts, voice_name, style = cls._resolve_voice(voice_id)

        os.makedirs(os.path.dirname(output_mp3_path), exist_ok=True)
        tmp_wav = output_mp3_path + ".vieneu.wav"
        # Encode MP3 ra FILE TẠM rồi os.replace atomic -> hủy/lỗi giữa chừng KHÔNG làm
        # hỏng/mất file giọng cũ đang có ở output_mp3_path.
        tmp_mp3 = output_mp3_path + ".vieneu.mp3"
        try:
            with cls._infer_lock:
                cls._fix_seed(text, voice_name)  # seed theo nội dung -> nhất quán + tự nhiên
                audio = tts.infer(text, **cls._infer_kwargs(voice_name, style))
                tts.save(audio, tmp_wav)

            # Chuyen ve MP3 (pipeline hien tai doc duration bang mutagen MP3
            # va cac seg file deu la .mp3 nhu edge-tts sinh ra)
            cmd = [FFMPEG_EXE, "-y", "-i", tmp_wav,
                   "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE,
                   "-ar", MP3_SAMPLE_RATE, "-q:a", "2",
                   tmp_mp3]
            res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", close_fds=True)
            if res.returncode != 0 or not os.path.exists(tmp_mp3) or os.path.getsize(tmp_mp3) == 0:
                raise VieneuError(L(f"Không mã hoá được audio VieNeu sang MP3: {res.stderr[-300:]}", f"Could not encode VieNeu audio to MP3: {res.stderr[-300:]}"))
            safe_replace(tmp_mp3, output_mp3_path)
            return True
        finally:
            for _tmp in (tmp_wav, tmp_mp3):
                try:
                    os.remove(_tmp)
                except OSError:
                    pass

    @classmethod
    def load_lora_adapter(cls, tts, lora_repo_id: str, hf_token: str = None) -> bool:
        if getattr(tts, "_current_lora_repo", None) == lora_repo_id:
            return True
        try:
            from peft import PeftModel
        except ImportError:
            if os.environ.get("SUBFORGE_OFFLINE_ONLY") == "1":
                raise VieneuError("Thiếu thư viện PEFT trong runtime local; không tự cài qua mạng.")
            import sys
            if getattr(sys, "frozen", False):
                # Bản đóng gói: sys.executable là subforge_backend.exe — chạy
                # "-m pip" sẽ KHỞI ĐỘNG THÊM một backend lạc (chiếm cổng, khóa
                # thư mục cài làm hỏng nâng cấp), không cài được gì. peft phải
                # được bundle sẵn lúc build.
                logger.error(
                    L("Thiếu thư viện peft trong bản đóng gói — bỏ qua LoRA "
                    "(dùng giọng gốc). Cần bundle peft khi build backend.", "The peft library is missing from the packaged build — skipping LoRA (using the base voice). peft must be bundled when building the backend.")
                )
                return False
            logger.info("PEFT library not found. Installing dynamically via pip...")
            import subprocess
            import importlib
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "peft"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            importlib.invalidate_caches()
            from peft import PeftModel
            
        # Cache the original model
        if not hasattr(tts.engine, "_original_base_model"):
            tts.engine._original_base_model = tts.engine.model
            
        # Patch model methods required by PEFT
        model = tts.engine._original_base_model
        if not hasattr(model, "get_input_embeddings"):
            model.get_input_embeddings = lambda: model.text_embeddings
        if not hasattr(model, "set_input_embeddings"):
            model.set_input_embeddings = lambda value: setattr(model, "text_embeddings", value)
        if not hasattr(model, "prepare_inputs_for_generation"):
            model.prepare_inputs_for_generation = lambda *args, **kwargs: None

        # 1. Try loading purely offline first (fast, no internet required)
        try:
            logger.info(f"🎯 Loading LoRA adapter offline from: {lora_repo_id}")
            if getattr(tts, "_lora_loaded", False):
                cls.unload_lora_adapter(tts)
                
            tts.engine.model = PeftModel.from_pretrained(tts.engine._original_base_model, lora_repo_id, local_files_only=True)
            tts._lora_loaded = True
            tts._current_lora_repo = lora_repo_id
            
            # Load voices.json from Hugging Face repo (offline)
            tts._load_voices(lora_repo_id, hf_token, clear_existing=False)
            
            # Convert loaded lists to numpy arrays
            import numpy as np
            for name, v in list(tts._preset_voices.items()):
                emb = v.get("speaker_emb")
                codes = v.get("codes")
                
                # If speaker_emb is missing (typical for LoRA model presets), copy from another valid preset
                if emb is None:
                    fallback_emb = None
                    for pk, pv in tts._preset_voices.items():
                        if pv.get("speaker_emb") is not None and pk != name:
                            fallback_emb = pv["speaker_emb"]
                            break
                    if fallback_emb is not None:
                        v["speaker_emb"] = fallback_emb.copy() if hasattr(fallback_emb, "copy") else np.asarray(fallback_emb, dtype=np.float32)
                    else:
                        v["speaker_emb"] = np.random.normal(0.0, 0.1, (192,)).astype(np.float32)
                elif not isinstance(emb, np.ndarray):
                    v["speaker_emb"] = np.asarray(emb, dtype=np.float32)
                    
                if codes is not None and not isinstance(codes, np.ndarray):
                    v["codes"] = np.asarray(codes, dtype=np.int64)
                    
            logger.info(f"   ✅ LoRA adapter loaded offline successfully: {lora_repo_id}")
            return True
        except Exception as offline_err:
            if os.environ.get("SUBFORGE_OFFLINE_ONLY") == "1":
                raise VieneuError(f"LoRA chưa có trong bộ nhớ cục bộ: {offline_err}") from offline_err
            logger.warning(f"Offline LoRA load failed (trying online fallback): {offline_err}")

        # 2. Online Fallback: temporarily enable online mode to download LoRA from HF
        old_hub_offline = os.environ.get("HF_HUB_OFFLINE")
        old_trans_offline = os.environ.get("TRANSFORMERS_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["TRANSFORMERS_OFFLINE"] = "0"
        
        try:
            logger.info(f"🎯 Loading LoRA adapter online from: {lora_repo_id}")
            if getattr(tts, "_lora_loaded", False):
                cls.unload_lora_adapter(tts)
                
            from huggingface_hub import constants
            constants.HF_HUB_OFFLINE = False
            tts.engine.model = PeftModel.from_pretrained(tts.engine._original_base_model, lora_repo_id, token=hf_token)
            tts._lora_loaded = True
            tts._current_lora_repo = lora_repo_id
            
            # Load voices.json from Hugging Face repo
            tts._load_voices(lora_repo_id, hf_token, clear_existing=False)
            
            # Convert loaded lists to numpy arrays
            import numpy as np
            for name, v in list(tts._preset_voices.items()):
                emb = v.get("speaker_emb")
                codes = v.get("codes")
                
                # If speaker_emb is missing (typical for LoRA model presets), copy from another valid preset
                if emb is None:
                    fallback_emb = None
                    for pk, pv in tts._preset_voices.items():
                        if pv.get("speaker_emb") is not None and pk != name:
                            fallback_emb = pv["speaker_emb"]
                            break
                    if fallback_emb is not None:
                        v["speaker_emb"] = fallback_emb.copy() if hasattr(fallback_emb, "copy") else np.asarray(fallback_emb, dtype=np.float32)
                    else:
                        v["speaker_emb"] = np.random.normal(0.0, 0.1, (192,)).astype(np.float32)
                elif not isinstance(emb, np.ndarray):
                    v["speaker_emb"] = np.asarray(emb, dtype=np.float32)
                    
                if codes is not None and not isinstance(codes, np.ndarray):
                    v["codes"] = np.asarray(codes, dtype=np.int64)
                    
            logger.info(f"   ✅ LoRA adapter loaded online successfully: {lora_repo_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to load LoRA adapter online: {e}")
            return False
        finally:
            # Restore offline flags
            if old_hub_offline is not None:
                os.environ["HF_HUB_OFFLINE"] = old_hub_offline
            else:
                os.environ.pop("HF_HUB_OFFLINE", None)
            if old_trans_offline is not None:
                os.environ["TRANSFORMERS_OFFLINE"] = old_trans_offline
            else:
                os.environ.pop("TRANSFORMERS_OFFLINE", None)

    @classmethod
    def unload_lora_adapter(cls, tts) -> bool:
        if not getattr(tts, "_lora_loaded", False):
            return True
        logger.info(f"   🔄 Unloading LoRA adapter: {tts._current_lora_repo}")
        try:
            # Restore the original base model
            if hasattr(tts.engine, "_original_base_model"):
                tts.engine.model = tts.engine._original_base_model
            tts._lora_loaded = False
            tts._current_lora_repo = None
            logger.info("   ✅ LoRA adapter unloaded, original weights restored")
            return True
        except Exception as e:
            logger.error(f"Failed to unload LoRA adapter: {e}")
            return False

    @classmethod
    def _require_preset_in_engine(cls, tts, voice_name: str) -> None:
        """Chan som giong preset co trong app nhung KHONG co trong engine.

        Xay ra khi may con GOI GPU CU: main.py chen `gpu_pack/.../_internal` len DAU
        sys.path nen `import vieneu` lay thu vien TU GOI DO, trong khi PRESET_VOICES
        lai den tu .exe moi -> 6 giong them o 3.3.0 khong ton tai trong engine. Neu
        khong chan, thu vien nem ValueError tieng Anh ("Voice 'X' not found...") loi
        thang ra UI. Khong co goi GPU thi engine = vieneu cua .exe -> khong bao gio dinh.
        """
        presets = getattr(tts, "_preset_voices", None)
        if not presets or voice_name in presets:
            return
        meta = next((p for p in PRESET_VOICES if p["name"] == voice_name), None)
        if meta is None or not meta.get("since"):
            return  # khong phai giong "moi" -> de loi goc cua thu vien noi cho dung
        raise VieneuError(
            L(f"Giọng '{voice_name}' cần thư viện VieNeu {meta['since']} trở lên, "
            "nhưng gói tăng tốc GPU trên máy vẫn là bản cũ nên chưa có giọng này. "
            "Vào mục Tăng tốc GPU để cập nhật gói, hoặc tạm chọn giọng khác.", f"The voice '{voice_name}' needs VieNeu library {meta['since']} or newer, but the GPU acceleration pack on this machine is still an old version, so this voice is not available yet. Go to GPU Acceleration to update the pack, or pick another voice for now.")
        )

    @classmethod
    def _resolve_voice(cls, voice_id: str):
        """Tra ve (tts, voice_name, style). Dang ky clone neu can."""
        if voice_id.startswith(CLONE_PREFIX):
            cls._try_upgrade_engine_for_clone()
            if cls._is_onnx_cpu_engine() and not cls._clone_cpu_warned:
                cls._clone_cpu_warned = True
                logger.warning(
                    L("Giọng nhân bản đang chạy bằng CPU nên sẽ chậm hơn. "
                    "Cài GPU Pack hoặc dùng máy có card NVIDIA để nhanh hơn.", "The cloned voice is running on CPU so it will be slower. Install the GPU Pack or use a machine with an NVIDIA card to go faster.")
                )

        tts = cls._get_tts()
        
        # Unload LoRA if any voice is selected to restore baseline weights
        cls.unload_lora_adapter(tts)
        if voice_id.startswith(CLONE_PREFIX):
            name = voice_id[len(CLONE_PREFIX):]
            reg = cls._load_registry()
            if not cls.clone_usable(name, reg):
                raise VieneuError(
                    L(f"Giọng clone '{name}' không tồn tại (đã bị xoá?). "
                    "Vui lòng chọn giọng khác hoặc tạo lại.", f"The clone voice '{name}' does not exist (was it deleted?). Please choose another voice or create it again.")
                )
            # Kiem tra CA _preset_voices cua engine (khong chi set in-memory):
            # engine bi thay/release giua chung ma set chua clear -> skip dang ky
            # -> infer bao "Voice not found". Doi chieu truc tiep de tu chua lanh.
            def _engine_missing():
                return name not in getattr(tts, "_preset_voices", {})

            if name not in cls._registered_clones or _engine_missing():
                with cls._infer_lock:
                    if name not in cls._registered_clones or _engine_missing():
                        t0 = time.time()
                        on_gpu = not cls._is_onnx_cpu_engine()
                        logger.info(
                            L(f"Đang chuẩn bị giọng nhân bản '{name}' "
                            f"(bằng {'GPU' if on_gpu else 'CPU'})...", f"Preparing the cloned voice '{name}' (on {'GPU' if on_gpu else 'CPU'})...")
                        )
                        cls._add_clone_voice(tts, name, reg[name], reg)
                        cls._registered_clones.add(name)
                        logger.info(
                            L(f"Đã chuẩn bị xong giọng nhân bản '{name}' ({time.time() - t0:.1f}s)", f"Finished preparing the cloned voice '{name}' ({time.time() - t0:.1f}s)")
                        )
            return tts, name, None
        raw_name = voice_id[len(PRESET_PREFIX):]
        
        # Map tên dạng slug (như truc_ly, doan_trang, adam...) sang tên preset chính xác của VieNeu (như Trúc Ly, Đoan Trang, Adam...)
        voice_name = raw_name
        VOICE_NAME_MAP = {
            "truc_ly": "Trúc Ly",
            "doan_trang": "Đoan Trang",
            "ngoc_linh": "Ngọc Linh",
            "mai_anh": "Mai Anh",
            "pham_tuyen": "Phạm Tuyên",
            "thanh_binh": "Thanh Bình",
            "minh_duc": "Minh Đức",
            "ngoc_tran": "Ngọc Trân",
            "quang_son": "Quang Sơn",
            "thuc_doan": "Thục Đoan",
            "thuy_dung": "Thùy Dung",
            "xuan_vinh": "Xuân Vĩnh",
            "thai_son": "Thái Sơn",
            "minh_triet": "Minh Triết",
            "ngoc_huyen": "Ngọc Huyền",
            "quynh_anh": "Quỳnh Anh",
            "my_duyen": "Mỹ Duyên",
            "kim_thanh": "Kim Thanh",
            "adam": "Adam",
            "duc_tri": "Đức Trí",
        }
        if raw_name.lower() in VOICE_NAME_MAP:
            voice_name = VOICE_NAME_MAP[raw_name.lower()]
        else:
            engine_presets = getattr(tts, "_preset_voices", {})
            for k in engine_presets.keys():
                if k.lower() == raw_name.lower() or k.replace(" ", "_").lower() == raw_name.lower():
                    voice_name = k
                    break

        cls._require_preset_in_engine(tts, voice_name)
        style = None
        try:
            info = tts.get_preset_voice(voice_name)
            if isinstance(info, dict):
                style = info.get("style")
        except Exception:
            pass
        return tts, voice_name, style

    @classmethod
    def _wav_to_mp3(cls, tmp_wav: str, out_mp3_path: str) -> None:
        """Wav -> mp3 giu nguyen 48kHz native cua VieNeu (tranh downsample mat tan so cao).

        Encode ra FILE TAM roi os.replace atomic -> huy/loi giua chung KHONG lam hong
        hay mat file giong cu dang co o out_mp3_path.
        """
        tmp_mp3 = out_mp3_path + ".enc.mp3"
        cmd = [FFMPEG_EXE, "-y", "-i", tmp_wav,
               "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE,
               "-ar", MP3_SAMPLE_RATE,
               "-q:a", "2",       # VBR quality level 2 (xap xi 192kbps, tot hon CBR thuan)
               tmp_mp3]
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", close_fds=True)
        if res.returncode != 0 or not os.path.exists(tmp_mp3) or os.path.getsize(tmp_mp3) == 0:
            try:
                if os.path.exists(tmp_mp3):
                    os.remove(tmp_mp3)
            except OSError:
                pass
            raise VieneuError(L(f"Không mã hoá được audio VieNeu sang MP3: {res.stderr[-300:]}", f"Could not encode VieNeu audio to MP3: {res.stderr[-300:]}"))
        safe_replace(tmp_mp3, out_mp3_path)

    @classmethod
    def _encode_mp3(cls, audio, tts, out_mp3_path: str):
        """Luu numpy audio -> wav tam -> MP3 44.1kHz (khop pipeline seg .mp3)."""
        os.makedirs(os.path.dirname(out_mp3_path), exist_ok=True)
        tmp_wav = out_mp3_path + ".vieneu.wav"
        try:
            tts.save(audio, tmp_wav)
            cls._wav_to_mp3(tmp_wav, out_mp3_path)
        finally:
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    @classmethod
    def _encode_then_notify(cls, tmp_wav: str, out_mp3_path: str, on_clip_done=None) -> bool:
        """Encode MP3 xong -> bao on_clip_done NGAY (tren luong encode-pool).
        Loi trong callback KHONG lam hong ket qua encode."""
        ok = cls._encode_wav_file_job(tmp_wav, out_mp3_path)
        if ok and on_clip_done is not None:
            try:
                on_clip_done(out_mp3_path)
            except Exception as e:
                logger.warning(f"VieNeu batch: on_clip_done loi cho '{out_mp3_path}': {e}")
        return ok

    @classmethod
    def _encode_wav_file_job(cls, tmp_wav: str, out_mp3_path: str) -> bool:
        try:
            cls._wav_to_mp3(tmp_wav, out_mp3_path)
            return True
        except Exception as e:
            logger.error(f"VieNeu batch: loi luu '{out_mp3_path}': {e}")
            return False
        finally:
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    @classmethod
    def _downgrade_engine_to_cpu(cls) -> None:
        """Lui engine hien tai ve ONNX/CPU khi GPU OOM giua chung — giai phong VRAM
        va nap lai engine CPU (on dinh). Xoa registry clone da encode tren engine cu."""
        cls._ensure_hf_env()
        old = cls._tts
        cls._tts = None
        cls._release_engine(old)
        try:
            from vieneu import Vieneu
            eng = Vieneu(mode="v3turbo", backend="onnx", device="cpu")
            cls._engine_is_onnx_cpu = True
            cls._tts = eng
            cls._registered_clones.clear()
            logger.warning(L("VieNeu: đã lùi engine về ONNX/CPU sau khi GPU hết VRAM.", "VieNeu: engine rolled back to ONNX/CPU after the GPU ran out of VRAM."))
        except Exception as e:
            raise VieneuError(L(f"Không lùi được VieNeu về CPU sau khi GPU OOM: {e}", f"Could not roll VieNeu back to CPU after GPU OOM: {e}"))

    @staticmethod
    def _is_oom_error(e: Exception) -> bool:
        try:
            import torch
            if isinstance(e, torch.cuda.OutOfMemoryError):
                return True
        except Exception:
            pass
        s = str(e).lower()
        return "out of memory" in s or "cuda oom" in s or "cublas_status_alloc_failed" in s

    @classmethod
    def _infer_batch_safe(cls, tts, texts: list, kw: dict, device: str):
        """Goi tts.infer_batch co CHONG OOM.

        GPU OOM giua chung -> don cache, GIAM batch (chia doi, thu lai). Neu batch
        da ve 1 ma van OOM -> LUI CA ENGINE ve CPU va lam lai tu dau tren CPU.
        Tra ve (audios, tts_moi, device_moi) — engine/device co the thay doi.
        """
        try:
            import torch
        except Exception:
            torch = None

        def _run(_tts, _texts):
            return _tts.infer_batch(_texts, **kw)

        # Tren CPU: khong OOM VRAM, chay thang.
        if device != "cuda":
            return _run(tts, texts), tts, device

        try:
            return _run(tts, texts), tts, device
        except Exception as e:
            if not cls._is_oom_error(e):
                raise
            logger.warning(
                L(f"VieNeu: GPU hết VRAM khi sinh nhóm {len(texts)} câu. "
                "Dọn cache và chia nhỏ batch để thử lại.", f"VieNeu: GPU ran out of VRAM while generating a group of {len(texts)} sentences. Clear the cache and split into smaller batches to retry.")
            )
            if torch is not None:
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass

            # Chia doi batch de giam dinh VRAM, thu lai tung nua (de quy).
            if len(texts) > 1:
                mid = len(texts) // 2
                a1, tts, device = cls._infer_batch_safe(tts, texts[:mid], kw, device)
                a2, tts, device = cls._infer_batch_safe(tts, texts[mid:], kw, device)
                return list(a1) + list(a2), tts, device

            # Batch = 1 cau van OOM -> GPU khong du cho ca 1 cau. Lui ca engine ve CPU.
            logger.warning(
                L("VieNeu: 1 câu vẫn hết VRAM -> lùi hẳn engine sang CPU (ONNX) và sinh lại trên CPU.", "VieNeu: a single sentence still runs out of VRAM -> rolling the engine back to CPU (ONNX) and regenerating on CPU.")
            )
            cls._downgrade_engine_to_cpu()
            new_tts = cls._get_tts()
            return _run(new_tts, texts), new_tts, "cpu"

    @classmethod
    def synthesize_batch(cls, items, voice_id: str, batch_size: int = None, project_dir: str = None, check_cancel=None, on_clip_done=None) -> list:
        """Sinh NHIEU cau 1 luot bang infer_batch (nhanh hon sinh tuan tu).

        GPU pipeline: infer batch N (GPU) trong khi ffmpeg batch N-1 chay song song (CPU).

        check_cancel: callable không tham số (tuỳ chọn) — trả True để DỪNG giữa các
        mini-batch (dùng cho pipeline lồng tiếng vốn giữ cờ hủy in-memory, không có
        marker file .cancel_render như review).
        on_clip_done: callable(out_path) (tuỳ chọn) — gọi NGAY khi từng file MP3 nén
        xong (chạy trên luồng encode-pool), KHÔNG đợi hết batch. Dùng để fit tốc độ +
        báo UI nghe thử từng câu trong lúc các mini-batch sau còn đang sinh trên GPU.
        """
        items = [(t, p) for t, p in items if (t or "").strip()]
        if not items:
            return []
        tts, voice_name, style = cls._resolve_voice(voice_id)

        from utils.hardware_utils import get_best_device, get_cuda_tts_batch_size
        device, _ = get_best_device()
        if batch_size is None:
            batch_size = get_cuda_tts_batch_size() if device == "cuda" else 6

        encode_workers = min(8, max(2, (os.cpu_count() or 4)))
        results = []
        total = len(items)
        encode_futures: list[concurrent.futures.Future] = []

        cancel_marker = os.path.join(project_dir, ".cancel_render") if project_dir else None

        with concurrent.futures.ThreadPoolExecutor(max_workers=encode_workers) as encode_pool:
            for i in range(0, total, batch_size):
                if (cancel_marker and os.path.exists(cancel_marker)) or (
                    check_cancel is not None and check_cancel()
                ):
                    logger.warning(L("VieNeu batch: Phát hiện yêu cầu hủy render, dừng tiến trình sinh GPU.", "VieNeu batch: Render cancellation detected, stopping the GPU generation process."))
                    break

                chunk = items[i:i + batch_size]
                texts = [t for t, _ in chunk]
                kw = cls._infer_kwargs(voice_name, style)
                logger.info(
                    L(f"VieNeu: Xử lý tuần tự nhóm {len(texts)} câu "
                    f"(tiến độ: {min(i + batch_size, total)}/{total}, {device})...", f"VieNeu: Processing a group of {len(texts)} sentences sequentially (progress: {min(i + batch_size, total)}/{total}, {device})...")
                )
                t0 = time.time()
                prev_device = device
                with cls._infer_lock:
                    # seed theo câu đầu của batch -> nhất quán xuyên suốt batch
                    cls._fix_seed(texts[0] if texts else "", voice_name)
                    audios, tts, device = cls._infer_batch_safe(tts, texts, kw, device)
                if device != prev_device:
                    # Engine da lui CPU giua chung -> voice (nhat la clone) can dang ky
                    # lai tren engine moi cho cac batch KE TIEP. _resolve_voice tu lay
                    # _infer_lock ben trong (clone) nen phai goi NGOAI vung khoa tren.
                    tts, voice_name, style = cls._resolve_voice(voice_id)
                    kw = cls._infer_kwargs(voice_name, style)
                logger.info(
                    L(f"VieNeu: Xong nhóm {len(texts)} câu ({time.time() - t0:.1f}s) - Bắt đầu nén MP3 ngầm...", f"VieNeu: Finished a group of {len(texts)} sentences ({time.time() - t0:.1f}s) - starting background MP3 encoding...")
                )
                for (_t, out), audio in zip(chunk, audios):
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    tmp_wav = out + ".vieneu.wav"
                    try:
                        tts.save(audio, tmp_wav)
                        encode_futures.append(
                            encode_pool.submit(
                                cls._encode_then_notify, tmp_wav, out, on_clip_done
                            )
                        )
                    except Exception as e:
                        logger.error(f"VieNeu batch: loi save wav '{out}': {e}")
                        results.append(False)

            for fut in concurrent.futures.as_completed(encode_futures):
                results.append(fut.result())

        return results

    @classmethod
    def _trim_mp3(cls, path: str, target_duration: float):
        tmp = path + ".trim.mp3"
        cmd = [FFMPEG_EXE, "-y", "-i", path, "-t", f"{target_duration:.3f}",
               "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE,
               "-ar", MP3_SAMPLE_RATE, "-q:a", "2", tmp]
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", close_fds=True)
        if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            safe_replace(tmp, path)
        else:
            try:
                os.remove(tmp)
            except Exception:
                pass

    @classmethod
    def fit_to_duration(cls, path: str, target_duration: float, base_rate: str = "auto") -> bool:
        """Xử lý tốc độ file MP3 (base) theo chế độ base_rate.

        - base_rate == "auto": KHỚP thời lượng đoạn -> atempo (cap 1.8x) để vừa
          target_duration rồi trim đuôi thừa. (Hành vi cũ.)
        - base_rate == "+X%"/"-X%"/"+0%": tốc độ CỐ ĐỊNH -> atempo theo đúng hệ số
          đã chọn (KHÔNG tự tăng tốc để khớp), nhưng VẪN trim nếu dài hơn khung để
          người dùng biết đoạn bị cắt cho vừa.

        Trả True nếu có cắt đuôi đáng kể (dùng để gắn cờ is_trimmed).
        """
        from mutagen.mp3 import MP3

        is_fixed = bool(base_rate) and base_rate != "auto"

        if is_fixed:
            # CỐ ĐỊNH: áp hệ số tốc độ đã chọn.
            factor = cls._rate_str_to_factor(base_rate)
            if factor and abs(factor - 1.0) > 0.01:
                cls.speedup_mp3(path, factor)
        else:
            # AUTO: tăng tốc để khớp target.
            if not target_duration or target_duration <= 0.5:
                return False
            try:
                dur = MP3(path).info.length
            except Exception:
                return False
            if dur > target_duration + 0.05:
                cls.speedup_mp3(path, dur / target_duration)

        # Trim chung cho CẢ hai chế độ: cắt nếu vẫn dài hơn khung + báo is_trimmed.
        if target_duration and target_duration > 0.5:
            try:
                pre_trim_dur = MP3(path).info.length
                if pre_trim_dur > target_duration + 0.05:
                    cls._trim_mp3(path, target_duration)
                    return (pre_trim_dur - target_duration) > TRIM_WARN_THRESHOLD_SEC
            except Exception:
                pass
        return False

    @staticmethod
    def _rate_str_to_factor(rate: str) -> float:
        """"+45%" -> 1.45, "-10%" -> 0.9, "+0%" -> 1.0. Kẹp [0.5, 1.8]."""
        try:
            s = (rate or "").strip()
            if s.endswith("%"):
                return max(0.5, min(1.8, 1.0 + float(s[:-1]) / 100.0))
        except Exception:
            pass
        return 1.0

    @classmethod
    def speedup_mp3(cls, path: str, factor: float) -> bool:
        """Doi toc do file MP3 bang ffmpeg atempo (ho tro CA nhanh lan cham).

        Decode MP3 -> WAV trung gian truoc khi atempo de tranh generation loss
        (moi lan decode->encode MP3 la 1 lop nhieu nen -> lam re tieng).
        atempo nhan 0.5-2.0 -> kep [0.5, 1.8]. Truoc day kep san 1.0 lam toc do
        CHAM ("-10%" -> 0.9x) bi bo qua am tham (log bao x0.90 nhung audio giu nguyen).
        """
        factor = max(0.5, min(1.8, factor))
        if abs(factor - 1.0) <= 0.01:
            return True
        filters = [f"atempo={factor:.4f}"]
        tmp_wav = path + ".spd.wav"
        tmp_mp3 = path + ".spd.mp3"
        # Buoc 1: MP3 -> WAV (giai nen hoan toan, khong mat chat luong)
        decode_cmd = [FFMPEG_EXE, "-y", "-i", path, "-ar", MP3_SAMPLE_RATE, tmp_wav]
        decode_res = subprocess.run(decode_cmd, stdin=subprocess.DEVNULL,
                                    capture_output=True, close_fds=True)
        if decode_res.returncode != 0 or not os.path.exists(tmp_wav):
            # fallback: encode truc tiep tu mp3 neu giai nen loi
            tmp_wav = path
        # Buoc 2: WAV + atempo -> MP3 moi (chi encode 1 lan -> it nhieu nhat)
        cmd = [FFMPEG_EXE, "-y", "-i", tmp_wav, "-filter:a", ",".join(filters),
               "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE,
               "-ar", MP3_SAMPLE_RATE, "-q:a", "2", tmp_mp3]
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", close_fds=True)
        if tmp_wav != path:
            try: os.remove(tmp_wav)
            except Exception: pass
        if res.returncode == 0 and os.path.exists(tmp_mp3) and os.path.getsize(tmp_mp3) > 0:
            safe_replace(tmp_mp3, path)
            return True
        try:
            os.remove(tmp_mp3)
        except Exception:
            pass
        logger.warning(f"VieNeu: speedup atempo that bai: {res.stderr[-200:]}")
        return False
