import torch
import logging
import subprocess
import os

logger = logging.getLogger("HardwareUtils")


def get_best_device():
    """
    Detects the best available device for AI processing.
    Prioritizes CUDA (NVIDIA GPU) over CPU.
    """
    if torch.cuda.is_available():
        # Check environment override for compute type
        env_ct = os.environ.get("SUBFORGE_WHISPER_COMPUTE_TYPE", "").strip()
        if env_ct:
            return "cuda", env_ct

        # Automatic low VRAM check (e.g. RTX 3050 Laptop with 4GB VRAM)
        try:
            _, total_b = torch.cuda.mem_get_info()
            total_gb = total_b / (1024 ** 3)
            # If total VRAM is less than 5.0 GB, default to int8_float16 to avoid OOM
            if total_gb < 5.0:
                logger.info(f"Hardware STT: CUDA GPU VRAM detected as {total_gb:.1f}GB (< 5GB). Defaulting to compute_type='int8_float16' to prevent OOM.")
                return "cuda", "int8_float16"
        except Exception as e:
            logger.warning(f"Hardware STT: Failed to read CUDA memory info ({e}). Defaulting to float16.")

        return "cuda", "float16"

    return "cpu", "int8"


def get_cuda_vram_free_gb() -> float | None:
    if not torch.cuda.is_available():
        return None
    try:
        free_b, _ = torch.cuda.mem_get_info()
        return free_b / (1024 ** 3)
    except Exception:
        return None


def get_cuda_tts_batch_size() -> int:
    """Batch infer VieNeu tren GPU — tan dung VRAM trong (3050 4GB thuong 6–10).

    Override: SUBFORGE_TTS_BATCH_SIZE
    """
    override = os.environ.get("SUBFORGE_TTS_BATCH_SIZE", "").strip()
    if override.isdigit():
        return max(1, min(16, int(override)))
    if not torch.cuda.is_available():
        return 6
    free_gb = get_cuda_vram_free_gb()
    if free_gb is None:
        return 6
    if free_gb >= 2.8:
        return 10
    if free_gb >= 2.0:
        return 8
    if free_gb >= 1.2:
        return 6
    return 4


def get_cuda_stt_transcribe_batch_size() -> int:
    """faster-whisper batch_size tren GPU — dung them VRAM de giam thoi gian encode.

    Override: SUBFORGE_STT_BATCH_SIZE
    """
    override = os.environ.get("SUBFORGE_STT_BATCH_SIZE", "").strip()
    if override.isdigit():
        return max(1, min(64, int(override)))
    free_gb = get_cuda_vram_free_gb()
    if free_gb is None:
        return 16
    if free_gb >= 2.5:
        return 32
    if free_gb >= 1.8:
        return 24
    return 16


def get_cuda_stt_beam_size() -> int:
    """GPU: beam nho hon = nhanh hon, van du chat luong dubbing."""
    override = os.environ.get("SUBFORGE_STT_BEAM_SIZE", "").strip()
    if override.isdigit():
        return max(1, min(5, int(override)))
    return 3 if torch.cuda.is_available() else 5

def get_ffmpeg_video_encoder():
    """
    Returns the best hardware-accelerated H.264 encoder for the current system.
    Returns 'libx264' as a safe software fallback.
    """
    # 1. Check for NVIDIA NVENC
    if torch.cuda.is_available():
        return "h264_nvenc"
    
    # 2. Check for AMD AMF (using a quick ffmpeg probe)
    # This is useful if NVIDIA is not present but the user has Ryzen iGPU or AMD dGPU
    # For now, since we confirmed NVIDIA, we return NVENC.
    
    return "libx264"


# --------------------------------------------------------------------------- #
#  QWEN3-TTS: MỨC CHẠY THEO CẤU HÌNH MÁY — cùng tinh thần VIENEU_GPU_MIN_FREE_GB +
#  get_cuda_tts_batch_size: đọc VRAM/RAM/nhân thật rồi chọn, có biến môi trường ghi đè.
#
#  SỐ ĐO (2026-09-23, 0.6B Q8_0, xem backend/tools/bench_qwen3_tts.py):
#    CPU 8 luồng RTF 2,85 · CPU 2 nhân RTF ~8 · iGPU Arc 140V (Vulkan) RTF 0,78
#    RAM đỉnh 4,2–5,2 GB. Nút thắt trên CPU là codec (F32) ~7–10 s/câu -> GPU/iGPU
#    mới là đường chính; CPU chỉ ổn khi đủ nhân.
#  CHƯA ĐO: CUDA, iGPU đời cũ, bản Q4_K_M, chạy nhiều tiến trình song song -> các
#  ngưỡng dưới đây là PHỎNG ĐOÁN có căn cứ, sẽ chốt lại khi có số đo (kế hoạch 2.15.0).
# --------------------------------------------------------------------------- #
QWEN_CUDA_MIN_FREE_GB_Q8 = 2.5    # talker 0.6B Q8 ~1,0 GB + codec + KV 0,9 GB + đệm
QWEN_CUDA_MIN_FREE_GB_Q4 = 1.6
QWEN_CUDA_MIN_FREE_GB_17B = 6.0
QWEN_RAM_Q8_MIN_GB = 12.0         # RAM tổng dưới mức này -> dùng Q4_K_M cho đỡ RAM
QWEN_CPU_OK_PHYS_CORES = 6        # CPU ít nhân hơn -> vẫn chạy, nhưng gắn cờ "chậm"


def _nvidia_smi_free_gb() -> float | None:
    """VRAM trống của card NVIDIA qua nvidia-smi — dùng khi KHÔNG có gói GPU (torch
    bản CPU không đọc được CUDA) nhưng máy vẫn có card NVIDIA."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0:
            return None
        vals = [float(x) for x in out.stdout.split() if x.strip().replace(".", "").isdigit()]
        return max(vals) / 1024.0 if vals else None
    except Exception:
        return None


def _machine_ram_cores() -> tuple[float, int]:
    try:
        import psutil

        return (psutil.virtual_memory().total / 1024 ** 3,
                psutil.cpu_count(logical=False) or os.cpu_count() or 4)
    except Exception:
        return 8.0, os.cpu_count() or 4


def get_qwen_tts_tiers(have_cuda_backend: bool, have_vulkan_backend: bool,
                       prefer: str = "auto") -> list[dict]:
    """Các MỨC chạy Qwen3-TTS, từ cao xuống thấp — dịch vụ thử lần lượt, mức nào không
    nạp được / hết VRAM thì lùi mức sau (giống VieNeu: chia đôi batch -> lùi CPU).

    Mỗi mức: ``{"name", "backend", "size", "quant", "procs", "threads", "slow"}``.
      · backend: giá trị GGML_BACKEND (``CUDA0`` / ``Vulkan0`` / ``CPU``);
      · slow: máy chỉ chạy được bằng CPU ít nhân -> UI nên gợi ý dùng Edge.
    ``have_*_backend``: bản cài có DLL backend đó không (ggml-cuda / ggml-vulkan).
    ``prefer``: "auto" | "cpu" | "gpu" (mục "Thiết bị cho giọng Qwen" trong Cài đặt).

    Ghi đè (theo kiểu SUBFORGE_TTS_BATCH_SIZE): SUBFORGE_QWEN_BACKEND (CPU/Vulkan0/
    CUDA0), SUBFORGE_QWEN_MODEL (vd "0.6b-Q4_K_M"), SUBFORGE_QWEN_PROCS, SUBFORGE_QWEN_THREADS.
    """
    ram_gb, phys = _machine_ram_cores()
    threads_env = os.environ.get("SUBFORGE_QWEN_THREADS", "").strip()
    threads = int(threads_env) if threads_env.isdigit() else int(phys)
    procs_env = os.environ.get("SUBFORGE_QWEN_PROCS", "").strip()
    procs = max(1, min(4, int(procs_env))) if procs_env.isdigit() else 1
    q_ram = "Q8_0" if ram_gb >= QWEN_RAM_Q8_MIN_GB else "Q4_K_M"

    def tier(name, backend, size, quant, slow=False):
        return {"name": name, "backend": backend, "size": size, "quant": quant,
                "procs": procs, "threads": threads, "slow": slow}

    ra: list[dict] = []
    if prefer != "cpu":
        if have_cuda_backend:
            free = get_cuda_vram_free_gb()
            if free is None:
                free = _nvidia_smi_free_gb()
            if free is not None:
                if free >= QWEN_CUDA_MIN_FREE_GB_17B:
                    ra.append(tier("cuda-1.7b", "CUDA0", "1.7b", "Q8_0"))
                if free >= QWEN_CUDA_MIN_FREE_GB_Q8:
                    ra.append(tier("cuda-q8", "CUDA0", "0.6b", "Q8_0"))
                if free >= QWEN_CUDA_MIN_FREE_GB_Q4:
                    ra.append(tier("cuda-q4", "CUDA0", "0.6b", "Q4_K_M"))
        if have_vulkan_backend:
            #  Vulkan: card AMD/Intel rời lẫn iGPU (bộ nhớ dùng chung RAM -> xét RAM).
            #  Không có thiết bị Vulkan thì server tự báo lỗi lúc nạp -> lùi mức sau.
            ra.append(tier("vulkan", "Vulkan0", "0.6b", q_ram))
            if q_ram != "Q4_K_M":
                ra.append(tier("vulkan-q4", "Vulkan0", "0.6b", "Q4_K_M"))
    if prefer != "gpu" or not ra:
        slow = phys < QWEN_CPU_OK_PHYS_CORES
        ra.append(tier("cpu", "CPU", "0.6b", q_ram, slow=slow))
        if q_ram != "Q4_K_M":
            ra.append(tier("cpu-q4", "CPU", "0.6b", "Q4_K_M", slow=slow))

    env_be = os.environ.get("SUBFORGE_QWEN_BACKEND", "").strip()
    env_md = os.environ.get("SUBFORGE_QWEN_MODEL", "").strip()
    if env_be or env_md:
        size, _, quant = (env_md or "0.6b-Q8_0").partition("-")
        ghi_de = tier("env", env_be or "CPU", size or "0.6b", quant or "Q8_0")
        ra = [ghi_de] + [t for t in ra if (t["backend"], t["size"], t["quant"])
                         != (ghi_de["backend"], ghi_de["size"], ghi_de["quant"])]
    return ra


# --------------------------------------------------------------------------- #
#  GPU TÍCH HỢP (iGPU) — Giai đoạn 6 kế hoạch 2.15.0: tăng tốc việc nặng trên máy không
#  có card rời. CHỈ dò, chưa bật gì: từng việc (VieNeu/OCR qua DirectML, giải mã QSV…)
#  chỉ được dùng iGPU sau khi backend/tools/bench_igpu.py đo nhanh hơn MÀ kết quả khớp CPU.
#
#  AdapterRAM của WMI là uint32 (trần 4 GB) và iGPU dùng chung RAM -> không dùng để xét
#  bộ nhớ; xét RAM máy như get_qwen_tts_tiers.
# --------------------------------------------------------------------------- #
_ADAPTERS_CACHE: list[dict] | None = None


def _vendor_of(name: str, company: str) -> str:
    s = f"{name} {company}".lower()
    for key, vendor in (("nvidia", "nvidia"), ("intel", "intel"), ("amd", "amd"),
                        ("radeon", "amd"), ("advanced micro", "amd"), ("qualcomm", "qualcomm")):
        if key in s:
            return vendor
    return "other"


def _is_integrated(name: str, vendor: str) -> bool:
    """Tên -> iGPU hay card rời. Intel Arc A/B (A770, B580…) là card rời, còn Arc 140V
    /130V (Lunar Lake), Iris Xe, UHD là tích hợp. AMD: RX / Pro W / FirePro là card rời,
    "Radeon Graphics" / 780M / Vega Graphics là tích hợp."""
    import re

    n = name.lower()
    if vendor == "intel":
        return not re.search(r"arc(\(tm\))?\s+(pro\s+)?[ab]\d{2,3}", n)
    if vendor == "amd":
        return not re.search(r"\brx\b|radeon pro|firepro|instinct", n)
    return False


def list_display_adapters() -> list[dict]:
    """Card màn hình của máy (Windows: WMI Win32_VideoController), đọc MỘT lần rồi nhớ.
    Bỏ "Microsoft Basic Display Adapter" / màn hình ảo. Lỗi -> []."""
    global _ADAPTERS_CACHE
    if _ADAPTERS_CACHE is not None:
        return _ADAPTERS_CACHE
    ra: list[dict] = []
    if os.name == "nt":
        try:
            import json

            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_VideoController | Select-Object Name,"
                 "AdapterCompatibility,DriverVersion | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            data = json.loads(out.stdout or "[]") if out.returncode == 0 else []
            for g in data if isinstance(data, list) else [data]:
                name = str(g.get("Name") or "").strip()
                company = str(g.get("AdapterCompatibility") or "").strip()
                if not name or "basic display" in name.lower() or "microsoft" in company.lower():
                    continue
                vendor = _vendor_of(name, company)
                ra.append({"name": name, "vendor": vendor, "driver": str(g.get("DriverVersion") or ""),
                           "integrated": _is_integrated(name, vendor)})
        except Exception as e:
            logger.info(f"Không đọc được danh sách card màn hình: {e}")
    _ADAPTERS_CACHE = ra
    return ra


def get_igpu_info() -> dict | None:
    """iGPU đầu tiên của máy + các đường tăng tốc về lý thuyết có thể dùng:
    ``qsv`` (Intel Quick Sync), ``amf`` (AMD), ``directml`` (mọi GPU DX12 trên Windows 10+).
    Không có iGPU -> None. "Có thể dùng" ≠ "đã bật": xem ghi chú đầu mục."""
    for g in list_display_adapters():
        if g["integrated"]:
            return {**g, "qsv": g["vendor"] == "intel", "amf": g["vendor"] == "amd",
                    "directml": os.name == "nt"}
    return None
