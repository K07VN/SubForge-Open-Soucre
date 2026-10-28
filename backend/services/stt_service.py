from utils.i18n import L
from utils.hardware_utils import (
    get_best_device,
    get_cuda_stt_beam_size,
)
import gc
import logging
import os
import subprocess
import time
import imageio_ffmpeg
import re
import tempfile
import threading

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger("STTService")


def _fmt_ts(seconds: float) -> str:
    """Định dạng mốc thời gian dễ đọc: có giờ hiện h:mm:ss, không thì m:ss.
    VD 190.3 -> '3:10', 3725.0 -> '1:02:05'."""
    total = int(round(seconds or 0))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class OutOfMemoryError(RuntimeError):
    """Nạp/chạy model thất bại vì HẾT BỘ NHỚ (RAM hệ thống hoặc VRAM GPU).

    Tách riêng để route hiển thị thông báo tiếng Việt dễ hiểu thay vì lỗi kỹ thuật
    kiểu 'mkl_malloc: failed to allocate memory' / 'CUDA out of memory'.
    """


# Dấu hiệu lỗi hết bộ nhớ trong message (cả CPU-MKL lẫn CUDA lẫn cấp phát chung).
_OOM_MARKERS = (
    "mkl_malloc",
    "failed to allocate",
    "out of memory",
    "cuda oom",
    "cublas_status_alloc_failed",
    "bad_alloc",
    "cannot allocate memory",
)


def _is_oom_error(err: Exception) -> bool:
    return any(m in str(err).lower() for m in _OOM_MARKERS)


def _oom_friendly_msg() -> str:
    return L(
        "Máy không đủ bộ nhớ để chạy nhận diện giọng nói (STT). "
        "Hãy đóng bớt ứng dụng đang mở để giải phóng RAM, hoặc chọn model Whisper nhỏ "
        "hơn (Base) trong Quản lý Model AI, rồi thử lại.",
        "The machine does not have enough memory to run speech recognition (STT). "
        "Close some open applications to free up RAM, or pick a smaller Whisper model "
        "(Base) in AI Model Manager, then try again.",
    )


# Singleton — dubbing (video_routes) va review (review_service) dung CUNG engine/logic.
_shared_stt_service = None
_shared_stt_lock = threading.Lock()


def get_stt_service() -> "STTService":
    global _shared_stt_service
    if _shared_stt_service is None:
        with _shared_stt_lock:
            if _shared_stt_service is None:
                _shared_stt_service = STTService()
    return _shared_stt_service


def _resolve_whisper_size() -> str:
    """Doc size Whisper nguoi dung dang chon tu config (fallback env -> 'base')."""
    size = None
    try:
        from services.config_service import get_config_service
        size = get_config_service().load_config().get("whisper_size")
    except Exception:
        size = None
    if not size:
        size = os.environ.get("SUBFORGE_WHISPER_SIZE", "base").strip() or "base"
    #  Cỡ đang chọn chưa tải mà máy có cỡ khác -> dùng cỡ đã có (xem whisper_size_hieu_luc).
    from utils.model_paths import whisper_size_hieu_luc
    return whisper_size_hieu_luc(size)


class STTService:
    def __init__(self, model_size=None):
        if model_size is None:
            model_size = _resolve_whisper_size()
        self.model_size = model_size
        self.model = None
        self.device = None
        self.compute_type = None
        self._vad_ok = None
        self._transcribe_lock = threading.Lock()  # WhisperModel khong thread-safe

    @classmethod
    def _check_vad_available(cls) -> bool:
        """Silero VAD (faster-whisper) can onnxruntime.SessionOptions tren CPU."""
        try:
            import onnxruntime as ort
            return hasattr(ort, "SessionOptions")
        except Exception:
            return False

    def _vad_filter_enabled(self) -> bool:
        if self._vad_ok is None:
            self._vad_ok = self._check_vad_available()
            if not self._vad_ok:
                logger.warning(
                    L("STT: onnxruntime VAD không khả dụng (thiếu SessionOptions) — "
                    "tắt vad_filter. Nếu dùng backend GPU, hãy build lại GPU pack mới.", "STT: onnxruntime VAD is unavailable (missing SessionOptions) — turning off vad_filter. If you use the GPU backend, rebuild the new GPU pack.")
                )
        return self._vad_ok

    def _ensure_model(self):
        # Dong bo size voi lua chon hien tai cua nguoi dung (config). get_stt_service()
        # la singleton -> neu doi Whisper size sau khi da tao service / da nap model,
        # phai NAP LAI dung model moi (truoc day giu nguyen 'base' cu du da chon Medium).
        desired = _resolve_whisper_size()
        if desired != self.model_size:
            logger.info(
                L(f"STT: Whisper size đổi '{self.model_size}' -> '{desired}', sẽ nạp lại model.", f"STT: Whisper size changed '{self.model_size}' -> '{desired}', the model will be reloaded.")
            )
            self.model_size = desired
            if self.model is not None:
                try:
                    self.release_gpu()
                except Exception:
                    pass
                self.model = None

        if self.model is not None:
            return

        # 1. Detect best hardware (GPU if available, otherwise CPU)
        device, compute_type = get_best_device()
        cpu_threads = os.cpu_count() or 4

        # BAT BUOC model da tai san. Neu chua co -> bao loi RO RANG (khong am tham
        # tai online, vi khong co thanh tien trinh). Nguoi dung sang 'Quan ly Model AI'
        # tai truoc. require_whisper_model raise ModelNotDownloadedError khi thieu.
        from utils.model_paths import require_whisper_model
        model_ref = require_whisper_model(self.model_size)
        local_only = True
        logger.info(L(f"STT: Dùng model Whisper đã tải tại {model_ref} (offline)", f"STT: Using the downloaded Whisper model at {model_ref} (offline)"))

        try:
            logger.info(L(f"STT: Khởi tạo WhisperModel '{self.model_size}' trên {device} ({compute_type})", f"STT: Initializing WhisperModel '{self.model_size}' on {device} ({compute_type})"))
            if device == "cpu":
                logger.info(
                    L("STT: Đang dùng CPU. Nếu máy có NVIDIA, vào Dashboard bật 'GPU Pack' "
                    "để cài backend CUDA — sau đó khởi động lại app, log sẽ hiện 'on cuda'.", "STT: Using CPU. If the machine has an NVIDIA card, go to the Dashboard and turn on 'GPU Pack' to install the CUDA backend — then restart the app, the log will show 'on cuda'.")
                )
            self.model = self._load_whisper_model(
                model_ref, device, compute_type, cpu_threads
            )
            self.device = device
            self.compute_type = compute_type
            if device == "cuda":
                try:
                    import torch
                    torch.backends.cudnn.benchmark = True
                except Exception:
                    pass
                from utils.hardware_utils import get_cuda_vram_free_gb
                vram = get_cuda_vram_free_gb()
                vram_s = f"{vram:.1f}GB" if vram is not None else "?"
                logger.info(L(f"STT GPU: bật cudnn benchmark, VRAM trống ~{vram_s}", f"STT GPU: cudnn benchmark enabled, free VRAM ~{vram_s}"))
        except Exception as e:
            # 2. Fallback to CPU if GPU initialization fails (e.g., out of memory or driver issues)
            if device != "cpu":
                logger.warning(L(f"STT: Khởi tạo GPU thất bại ({e}). Chuyển sang chế độ CPU.", f"STT: Initializing GPU failed ({e}). Switching to CPU mode."))
                try:
                    self.model = self._load_whisper_model(
                        model_ref, "cpu", "int8", cpu_threads
                    )
                    self.device = "cpu"
                    self.compute_type = "int8"
                except Exception as cpu_err:
                    # CPU cũng chết: nếu do hết bộ nhớ (mkl_malloc...) thì báo tiếng Việt.
                    if _is_oom_error(cpu_err) or _is_oom_error(e):
                        logger.error(L(f"STT: Hết bộ nhớ cả GPU lẫn CPU: {cpu_err}", f"STT: Out of memory on both GPU and CPU: {cpu_err}"))
                        raise OutOfMemoryError(_oom_friendly_msg()) from cpu_err
                    logger.error(L(f"STT: Lỗi nghiêm trọng khi khởi tạo model trên CPU: {cpu_err}", f"STT: Fatal error initializing the model on CPU: {cpu_err}"))
                    raise cpu_err
            else:
                if _is_oom_error(e):
                    logger.error(L(f"STT: Hết bộ nhớ khi khởi tạo model trên CPU: {e}", f"STT: Out of memory initializing the model on CPU: {e}"))
                    raise OutOfMemoryError(_oom_friendly_msg()) from e
                logger.error(L(f"STT: Lỗi nghiêm trọng khi khởi tạo model trên {device}: {e}", f"STT: Fatal error initializing the model on {device}: {e}"))
                raise e

    def _load_whisper_model(self, model_ref, device, compute_type, cpu_threads):
        """Nap Whisper tu model DA TAI (offline). model_ref luon la path cuc bo do
        _ensure_model da kiem tra bang require_whisper_model truoc do -> khong tai online."""
        from faster_whisper import WhisperModel
        #  Qwen3-TTS đang rảnh thì nhường bộ nhớ (không chờ nếu nó đang bận — xem
        #  QwenTTSService.release_for_other_model).
        try:
            from services.qwen_tts_service import QwenTTSService
            QwenTTSService.release_for_other_model(L("nhận dạng lời thoại (Whisper)", "speech recognition (Whisper)"))
        except Exception:
            pass
        return WhisperModel(
            model_ref,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=2 if device == "cuda" else 4,
            local_files_only=True,
        )

    def release_gpu(self, sync_timeout_sec: float = 8.0):
        """Giai phong VRAM Whisper truoc TTS/VieNeu GPU (quan trong tren card 4GB).

        torch.cuda.synchronize() co the treo hang chuc giay tren Windows/WDDM — dung
        timeout de khong block analysis/UI hoac shutdown backend.
        """
        if self.model is None:
            return
        dev = self.device
        try:
            del self.model
        except Exception:
            pass
        self.model = None
        self.device = None
        self.compute_type = None
        gc.collect()
        if dev == "cuda":
            import torch
            def _sync_and_clear():
                try:
                    torch.cuda.synchronize()
                    torch.cuda.empty_cache()
                except Exception:
                    pass

            t = threading.Thread(target=_sync_and_clear, daemon=True)
            t.start()
            t.join(timeout=sync_timeout_sec)
            if t.is_alive():
                logger.warning(
                    "STT: cuda.synchronize() qua cham (>%.0fs) — bo qua, "
                    "VRAM se giai phong khi process tat.",
                    sync_timeout_sec,
                )
            # Ghi lại VRAM trống sau khi nhả — để xác nhận Whisper đã trả GPU cho
            # VieNeu (quan trọng trên card 4GB: cần >1.8GB trống thì clone mới lên GPU).
            try:
                free_b, _ = torch.cuda.mem_get_info()
                logger.info(
                    "STT: da giai phong VRAM GPU (unload Whisper). VRAM trong ~%.1fGB.",
                    free_b / (1024 ** 3),
                )
                return
            except Exception:
                pass
        logger.info("STT: da giai phong VRAM GPU (unload Whisper).")

    def _reload_on_cpu(self):
        """Nap lai Whisper tren CPU (int8) sau khi GPU OOM giua chung transcribe.

        Giai phong model GPU hien tai roi tao lai tren CPU -> job khong chet, chi
        cham hon. Goi khi bat OOM trong luc nhan dang (khong phai luc khoi tao).
        """
        from utils.model_paths import require_whisper_model

        logger.warning(L("STT: GPU hết bộ nhớ khi nhận dạng -> nạp lại model trên CPU (int8).", "STT: GPU ran out of memory during recognition -> reloading the model on CPU (int8)."))
        try:
            self.release_gpu()
        except Exception:
            pass
        self.model = None
        model_ref = require_whisper_model(self.model_size)
        cpu_threads = os.cpu_count() or 4
        self.model = self._load_whisper_model(model_ref, "cpu", "int8", cpu_threads)
        self.device = "cpu"
        self.compute_type = "int8"
        logger.info(L("STT: đã chuyển sang CPU (int8) sau khi GPU OOM.", "STT: switched to CPU (int8) after the GPU OOM."))

    def transcribe(self, audio_path: str, language: str = None, check_cancel=None, on_segment=None):
        """
        Transcribe audio using faster-whisper.
        If duration > 3 mins, uses parallel chunking.

        check_cancel: hàm không tham số (tuỳ chọn) được gọi định kỳ trong lúc nhận
        dạng; nếu nó raise (vd yêu cầu hủy) thì transcribe dừng và raise lên trên.
        on_segment: callback(seg_dict) gọi NGAY khi một segment được nhận dạng xong
        (chỉ đường sequential — để route lưu dần lên UI). Đường parallel bỏ qua
        (chạy đa luồng, không an toàn để gọi vào DB session của route).
        """
        self._ensure_model()
        language = self._normalize_language(language)
        audio_path = os.path.abspath(audio_path)

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found at: {audio_path}")

        duration = self._get_duration(audio_path)

        # Cung logic cat chunk cho CPU va GPU (audio dai > 3 phut).
        # KHONG dung BatchedInferencePipeline tren GPU — no gop segment qua VAD batch.
        if duration > 180:
            logger.info(
                L(f"STT: {'GPU' if self.device == 'cuda' else 'CPU'} chế độ chia khối song song "
                f"(whisper={self.model_size}, thời lượng={duration:.0f}s)", f"STT: {'GPU' if self.device == 'cuda' else 'CPU'} parallel chunk mode (whisper={self.model_size}, duration={duration:.0f}s)")
            )
            return self._transcribe_parallel(audio_path, duration, language, check_cancel=check_cancel, on_segment=on_segment)
        if self.device == "cuda":
            bm = get_cuda_stt_beam_size()
            logger.info(
                L(f"STT: Nhận dạng tuần tự bằng GPU (whisper={self.model_size}, beam_size={bm}, thời lượng={duration:.0f}s)", f"STT: Sequential recognition on GPU (whisper={self.model_size}, beam_size={bm}, duration={duration:.0f}s)")
            )
        else:
            logger.info(
                L(f"STT: Nhận dạng tuần tự bằng CPU (whisper={self.model_size}, thời lượng={duration:.0f}s)", f"STT: Sequential recognition on CPU (whisper={self.model_size}, duration={duration:.0f}s)")
            )
        return self._transcribe_sequential(audio_path, language, check_cancel=check_cancel, on_segment=on_segment)

    def _get_duration(self, path):
        """Get audio duration using ffprobe or ffmpeg fallback."""
        # Try ffprobe first
        ffprobe = FFMPEG_EXE.replace("ffmpeg", "ffprobe")
        if os.path.exists(ffprobe):
            cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
            try:
                res = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    check=True,
                    close_fds=True,
                )
                return float(res.stdout.strip())
            except:
                pass
        
        # Fallback to ffmpeg
        cmd = [FFMPEG_EXE, "-i", path]
        try:
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                close_fds=True,
            )
            # Look for "Duration: 00:00:00.00" in stderr
            match = re.search(r"Duration:\s*(\d{2}):(\d{2}):([\d\.]+)", res.stderr)
            if match:
                hours, minutes, seconds = match.groups()
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except:
            pass
            
        return 0


    def _transcribe_kw(self) -> dict:
        #  Chất lượng lấy phụ đề (Cài đặt): chỉ "chinh_xac" đổi Whisper — beam 5 cả trên CPU.
        #  Đo 4 video (30-09-2026, so chữ với đáp án OCR): beam 1 F1 0,771 / 36 s, beam 3 0,767 /
        #  54 s (không hơn), beam 5 0,782 / 66 s -> "can_bang" (mặc định) giữ beam như "nhanh".
        try:
            from services.config_service import get_config_service
            muc = get_config_service().get_subtitle_quality()
        except Exception:  # noqa: BLE001
            muc = "nhanh"
        bm = get_cuda_stt_beam_size() if self.device == "cuda" else 1
        if muc == "chinh_xac":
            bm = max(bm, 5)
        return dict(
            # CPU: beam_size=1 (greedy) để giải mã nhanh hơn ~2-3x -> cửa sổ 30s ra
            # kết quả nhanh, cảm giác gần realtime. Độ chính xác giảm không đáng kể
            # với audio rõ. GPU vẫn dùng beam cấu hình được (đủ nhanh).
            beam_size=bm,
            task="transcribe",
            vad_filter=self._vad_filter_enabled(),
            vad_parameters=dict(min_silence_duration_ms=500),
            # Chống "ảo giác" (hallucination) trên nhạc nền / nhạc phim có lời và
            # đoạn im lặng — hay gặp với phim anime Nhật. condition_on_previous_text
            # là thủ phạm chính gây lặp lại câu cũ khi gặp đoạn nhạc; tắt đi để mỗi
            # đoạn nhận dạng độc lập. Các ngưỡng còn lại loại segment "rác" điểm tin
            # cậy thấp / nén bất thường (đặc trưng của text ảo giác lặp).
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
        )

    def _transcribe_sequential(self, audio_path: str, language: str = None, offset: float = 0, check_cancel=None, on_segment=None, chunk_label: str = None):
        """Transcribe bang WhisperModel (CPU/GPU cung API, segment nho nhu CPU).

        faster-whisper tra ve generator LUOI — moi vong lap moi transcribe 1 segment.
        Log tung segment de UI thay tien trinh; goi check_cancel de huy giua chung;
        goi on_segment de route luu dan len UI.

        chunk_label: khi chay song song nhieu khoi, moi dong log co tien to
        "[Khoi k/N]" de nguoi doc phan biet khoi nao — vi cac khoi in xen ke nhau.
        """
        # Tien to nhan khoi cho tung dong log (rong khi chay tuan tu 1 mach).
        tag = f"[{chunk_label}] " if chunk_label else ""

        detected_lang = [None]

        def _run_once():
            """Chay transcribe 1 luot. faster-whisper LUOI (lazy) -> OOM co the xay
            ra khi ITERATE, khong chi luc goi transcribe() -> gom ca vong lap vao day
            de tang cho khoi/goi lai chay lai duoc tu dau chunk."""
            with self._transcribe_lock:
                segments_iter, info = self.model.transcribe(
                    audio_path,
                    language=language,
                    **self._transcribe_kw(),
                )
                if info and hasattr(info, "language"):
                    detected_lang[0] = info.language
                    prob = getattr(info, "language_probability", None)
                    if language:
                        logger.info(
                            L(f"STT {tag}Ngôn ngữ: '{info.language}' (chỉ định sẵn)", f"STT {tag}Language: '{info.language}' (preset)")
                        )
                    elif prob is not None:
                        logger.info(
                            L(f"STT {tag}Nhận diện ngôn ngữ: '{info.language}' (độ tin cậy={prob:.2f})", f"STT {tag}Detected language: '{info.language}' (confidence={prob:.2f})")
                        )
                    else:
                        logger.info(
                            L(f"STT {tag}Nhận diện ngôn ngữ: '{info.language}'", f"STT {tag}Detected language: '{info.language}'")
                        )
                # Iterate NGAY trong lock: model khong thread-safe va generator moi
                # thuc su chay khi iterate (day la noi OOM xay ra).
                out = []
                for i, seg in enumerate(segments_iter):
                    if check_cancel is not None:
                        check_cancel()
                    text = seg.text.strip()
                    seg_dict = {
                        "index": i,
                        "start": seg.start + offset,
                        "end": seg.end + offset,
                        "text": text,
                    }
                    out.append(seg_dict)
                    preview = text[:60]
                    logger.info(
                        f"STT {tag}#{i + 1} @ {_fmt_ts(seg.start + offset)} → \"{preview}\""
                    )
                    if on_segment is not None:
                        on_segment(seg_dict)
                return out

        try:
            segments = _run_once()
        except Exception as e:
            # OOM trong luc nhan dang tren GPU -> lui CPU (int8) va chay lai chunk nay.
            # Chi retry 1 lan; loi khac (vd huy) nem thang.
            if self.device == "cuda" and _is_oom_error(e):
                self._reload_on_cpu()
                segments = _run_once()
            else:
                raise

        if chunk_label:
            logger.info(L(f"STT {tag}✓ xong ({len(segments)} câu)", f"STT {tag}✓ done ({len(segments)} sentences)"))
        return segments, detected_lang[0]

    def _transcribe_parallel(self, audio_path: str, duration: float, language: str, max_workers: int = 4, check_cancel=None, on_segment=None):
        """Chia audio dai (>3 phut) theo diem im lang roi nhan dang TUAN TU tung khoi.

        Truoc day dung ThreadPool(4 worker) nhung WhisperModel KHONG thread-safe nen
        moi transcribe deu qua _transcribe_lock -> that ra van chay tuan tu, chi ton
        them RAM/disk vi tao SAN tat ca WAV chunk cung luc. Nay:
          - Trich + nhan dang + XOA tung chunk MOT -> RAM/disk dinh khong tang theo
            do dai video (chi 1 WAV chunk ton tai moi luc).
          - on_segment goi duoc AN TOAN (tuan tu, khong ghi DB tu nhieu luong).
          - Loi OOM giua chung tu lui CPU (xem _transcribe_sequential).
        Ten ham giu nguyen de khong phai sua caller. max_workers khong con dung.
        """

        # 1. Detect silence points
        cmd = [FFMPEG_EXE, "-i", audio_path, "-af", "silencedetect=noise=-40dB:d=0.3", "-f", "null", "-"]
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
        )
        
        silence_starts = [float(m) for m in re.findall(r"silence_start: ([\d\.]+)", res.stderr)]
        silence_ends = [float(m) for m in re.findall(r"silence_end: ([\d\.]+)", res.stderr)]
        
        # 2. Determine split points at silence, targeting larger chunks to reduce disk I/O.
        split_points = [0]
        ideal_chunk_v = 120
        last_split = 0

        
        for s_start, s_end in zip(silence_starts, silence_ends):
            silence_mid = (s_start + s_end) / 2
            if silence_mid - last_split > ideal_chunk_v:
                split_points.append(silence_mid)
                last_split = silence_mid
        
        if duration - last_split > 60:
            split_points.append(duration)
        else:
            split_points[-1] = duration

        if len(split_points) <= 2:
            logger.info(L("STT: Không tìm được điểm cắt phù hợp. Chuyển sang chế độ tuần tự.", "STT: No suitable split point found. Switching to sequential mode."))
            return self._transcribe_sequential(audio_path, language, check_cancel=check_cancel, on_segment=on_segment)

        n_chunks = len(split_points) - 1
        logger.info(L(f"STT: Chia âm thanh thành {n_chunks} khối để xử lý song song.", f"STT: Splitting audio into {n_chunks} chunks for parallel processing."))
        # Bảng khối để người đọc log biết mỗi "Khối k" ứng với đoạn thời gian nào.
        for i in range(n_chunks):
            s, e = split_points[i], split_points[i + 1]
            logger.info(
                L(f"STT:   • Khối {i + 1}/{n_chunks}: {s:.0f}s → {e:.0f}s "
                f"({e - s:.0f}s)", f"STT:   • Chunk {i + 1}/{n_chunks}: {s:.0f}s → {e:.0f}s ({e - s:.0f}s)")
            )

        # 3. Trich + nhan dang TUNG khoi mot (chi 1 WAV chunk ton tai moi luc).
        all_segments = []
        chunk_errors = []
        detected_lang = None
        temp_dir = tempfile.mkdtemp(prefix="stt_chunks_")
        try:
            for i in range(n_chunks):
                # Huy giua chung: kiem TRUOC moi khoi (con trong khoi -> check_cancel
                # per-segment trong _transcribe_sequential dam nhiem).
                if check_cancel is not None:
                    check_cancel()

                start = split_points[i]
                end = split_points[i + 1]
                label = L(f"Khối {i + 1}/{n_chunks}", f"Chunk {i + 1}/{n_chunks}")
                chunk_path = os.path.join(temp_dir, f"chunk_{i}.wav")

                extract_cmd = [
                    FFMPEG_EXE, "-y", "-ss", str(start), "-to", str(end),
                    "-i", audio_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
                    chunk_path,
                ]
                extract_res = subprocess.run(
                    extract_cmd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    close_fds=True,
                )
                if extract_res.returncode != 0 or not os.path.exists(chunk_path) or os.path.getsize(chunk_path) == 0:
                    raise RuntimeError(f"Failed to extract STT chunk {i}: {extract_res.stderr}")

                try:
                    # offset=start de start/end tra ve theo thoi gian TUYET DOI cua audio goc.
                    # on_segment goi truc tiep (tuan tu, an toan) -> UI thay tien do dan.
                    res_segs, chunk_lang = self._transcribe_sequential(
                        chunk_path, language, offset=start,
                        chunk_label=label, check_cancel=check_cancel,
                        on_segment=on_segment,
                    )
                    if i == 0:
                        detected_lang = chunk_lang
                    all_segments.extend(res_segs)
                finally:
                    try:
                        os.remove(chunk_path)  # xoa NGAY sau khi xong -> disk khong phinh
                    except OSError:
                        pass
        finally:
            try:
                os.rmdir(temp_dir)
            except OSError:
                pass

        if chunk_errors:
            raise RuntimeError("Transcription failed for one or more chunks: " + "; ".join(chunk_errors))

        all_segments.sort(key=lambda x: x["start"])
        for i, seg in enumerate(all_segments):
            seg["index"] = i

        logger.info(
            L(f"STT: ✓ Ghép xong {n_chunks} khối → {len(all_segments)} câu (đã sắp theo thời gian).", f"STT: ✓ Merged {n_chunks} chunks → {len(all_segments)} sentences (sorted by time).")
        )
        return all_segments, detected_lang

    def _normalize_language(self, lang):
        if not lang:
            return None

        lang = lang.lower()
        # "auto" => de Whisper TU NHAN DIEN ngon ngu goc (language=None).
        # Whisper khong co ma "auto"; truyen thang se loi.
        if lang == "auto":
            return None
        mapping = {
            "zh-cn": "zh",
            "zh-tw": "zh",
            "zh-hk": "zh",
            "en-us": "en",
            "en-gb": "en",
            "en-au": "en",
            "fr-fr": "fr",
            "de-de": "de",
            "ja-jp": "ja",
            "ko-kr": "ko",
            "vi-vn": "vi"
        }
        return mapping.get(lang, lang)

