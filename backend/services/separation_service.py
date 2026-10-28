"""Tach audio goc thanh 2 stem: vocals (giong goc) + no_vocals (nhac nen).

Dung Demucs (2-stem mode), chon model THICH UNG theo thiet bi:
  - GPU (CUDA): htdemucs    (transformer, chat luong cao, GPU chay rat nhanh)
  - CPU       : hdemucs_mmi (conv Demucs v3, nhanh hon ~40% tren CPU)

Model duoc dong goi offline trong ai_models/demucs (xem download_models.py); neu
thieu se thu tai online, that bai thi bao loi ro rang de nguoi dung TAT tuy chon
tach hoac cai lai ban day du.

Duoc goi LAZY trong P5 merge (dubbing) va render (review) khi video/project bat
`separate_audio`. Ket qua cache theo fingerprint file nguon (gom ca ten model) ->
chi tach lai khi nguon hoac model thay doi.
"""
from utils.i18n import L
import json
import logging
import math
import os
import subprocess
import threading
import time

import imageio_ffmpeg

from utils.model_paths import ModelNotDownloadedError

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger("SeparationService")

# Model theo thiet bi. Doi gia tri o day neu muon uu tien khac.
MODEL_FOR_GPU = "htdemucs"
MODEL_FOR_CPU = "hdemucs_mmi"
# overlap thap hon = nhanh hon (it chong lan), chat luong gan nhu khong doi.
APPLY_OVERLAP = 0.1


def _la_het_bo_nho(e: BaseException) -> bool:
    """Loi nay co phai "het bo nho" khong (RAM may, khong phai VRAM card)?

    torch/numpy bao het RAM bang vai cau khac nhau tuy noi nem ra, nen nhan theo chu
    chu khong theo loai ngoai le — ``MemoryError`` chi la mot trong so do.
    """
    if isinstance(e, MemoryError):
        return True
    t = str(e).lower()
    return (
        "out of memory" in t
        or "tried to allocate" in t
        or "unable to allocate" in t
        or "cannot allocate memory" in t
        or "bad_alloc" in t
    )


class SeparationError(RuntimeError):
    """Loi tach nguon co message tieng Viet, hien thang len UI."""
    pass


class SeparationCancelled(Exception):
    """Nguoi dung huy giua chung khi dang tach nhac nen (khong phai loi)."""
    pass


class SeparationService:
    """Tach audio goc thanh 2 stem (vocals / no_vocals) bang Demucs."""

    _models = {}  # cache theo ten model: {name: model}
    _lock = threading.Lock()

    @classmethod
    def release_gpu(cls):
        """Nhả toàn bộ model demucs khỏi cache + dọn VRAM. Gọi khi backend tắt/reload
        để không giữ VRAM (htdemucs GPU nằm trong _models tới khi tắt process)."""
        with cls._lock:
            had = bool(cls._models)
            cls._models.clear()
        if had:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    @classmethod
    def resolve_source_media(cls, source_media_path: str) -> tuple[str, os.stat_result]:
        """Chuan hoa duong dan va kiem tra file nguon doc duoc (fail som, message ro)."""
        path = os.path.normpath((source_media_path or "").strip().strip('"'))
        display_path = os.path.abspath(path).replace("\\", "/") if path else ""
        if not path:
            raise SeparationError(L("Không có vị trí file video gốc trong dự án.", "The project has no source video file location."))

        drive = os.path.splitdrive(path)[0]
        if drive:
            root = drive + os.sep
            try:
                os.listdir(root)
            except OSError as e:
                if getattr(e, "winerror", None) == 1:
                    raise SeparationError(
                        L(f"Không truy cập được file video gốc tại: {display_path}. "
                        f"Ổ đĩa {drive} chưa được kết nối — hãy cắm lại ổ cứng/USB "
                        f"hoặc kết nối lại ổ mạng, rồi chạy lại.", f"Cannot access the source video file at: {display_path}. Drive {drive} is not connected — plug the hard drive/USB back in or reconnect the network drive, then run again.")
                    )
                raise SeparationError(
                    L(f"Không truy cập được file video gốc tại: {display_path}. "
                    f"Lỗi ổ đĩa {drive}: {e}", f"Cannot access the source video file at: {display_path}. Drive {drive} error: {e}")
                )

        try:
            src_stat = os.stat(path)
        except FileNotFoundError:
            raise SeparationError(
                L(f"Không tìm thấy file video gốc tại: {display_path}. "
                "File có thể đã bị xóa hoặc di chuyển sang vị trí khác.", f"Source video file not found at: {display_path}. The file may have been deleted or moved elsewhere.")
            )
        except OSError as e:
            if getattr(e, "winerror", None) == 1:
                raise SeparationError(
                    L(f"Không đọc được file video gốc tại: {display_path}. "
                    "Ổ đĩa có thể chưa được kết nối hoặc file không còn tồn tại.", f"Cannot read the source video file at: {display_path}. The drive may not be connected or the file no longer exists.")
                )
            raise SeparationError(
                L(f"Không đọc được file video gốc tại: {display_path}. Chi tiết: {e}", f"Cannot read the source video file at: {display_path}. Details: {e}")
            )

        if not os.path.isfile(path):
            raise SeparationError(
                L(f"Vị trí file video gốc không hợp lệ (không phải file): {display_path}", f"The source video file location is invalid (not a file): {display_path}")
            )

        return path, src_stat

    @classmethod
    def _pick_model_name(cls, device: str) -> str:
        return MODEL_FOR_GPU if device == "cuda" else MODEL_FOR_CPU

    @classmethod
    def _load_model(cls, model_name: str):
        """Nap model theo ten (cache) — dung chung cho dubbing + review."""
        cached = cls._models.get(model_name)
        if cached is not None:
            return cached
        #  Qwen3-TTS (tiến trình riêng, 1–5 GB RAM/VRAM) đang rảnh thì nhường bộ nhớ.
        try:
            from services.qwen_tts_service import QwenTTSService
            QwenTTSService.release_for_other_model(L("tách nhạc nền (Demucs)", "background-music separation (Demucs)"))
        except Exception:
            pass
        with cls._lock:
            cached = cls._models.get(model_name)
            if cached is not None:
                return cached
            try:
                import functools
                from pathlib import Path

                import torch
                from demucs.pretrained import get_model
                from utils.model_paths import require_demucs_repo

                # BAT BUOC model da tai. Neu chua -> ModelNotDownloadedError (bao ro
                # "vao Quan ly Model AI", KHONG am tham tai online). Passthrough ben duoi.
                repo = require_demucs_repo(model_name)
                # torch >= 2.6 mac dinh torch.load(weights_only=True) -> chan
                # checkpoint demucs (chua pickled class model). File bundle la
                # checkpoint chinh chu cua demucs nen tin cay duoc; tam thoi ep
                # weights_only=False trong luc load.
                _orig_load = torch.load
                torch.load = functools.partial(_orig_load, weights_only=False)
                try:
                    logger.info(
                        L(f"Tách nhạc nền: đang nạp mô hình xử lý âm thanh (offline)", f"Music separation: loading the audio processing model (offline)")
                    )
                    model = get_model(model_name, repo=Path(repo))
                finally:
                    torch.load = _orig_load
            except ModelNotDownloadedError:
                # Giu nguyen thong bao "chua tai" ro rang (khong boc thanh SeparationError).
                raise
            except Exception as e:
                raise SeparationError(
                    L(f"Không tải được model tách giọng (Demucs {model_name}). "
                    "Vui lòng cài lại bản SubForge đầy đủ hoặc TẮT tuỳ chọn "
                    f"'Tách giọng gốc & nhạc nền' rồi chạy lại. Chi tiết: {e}", f"Could not load the voice separation model (Demucs {model_name}). Please reinstall the full SubForge, or TURN OFF the option 'Separate original voice & background music' and run again. Details: {e}")
                )
            model.eval()
            cls._models[model_name] = model
            return model

    @classmethod
    def ensure_stems(cls, source_media_path: str, out_dir: str,
                     check_cancel=None) -> dict:
        """Tra ve {'vocals': path, 'no_vocals': path}.

        Co cache: chi tach lai khi stems chua ton tai hoac file nguon da thay
        doi (fingerprint = path + mtime + size khac voi marker da luu).
        ``check_cancel`` (tuy chon): ham khong tham so tra True khi nguoi dung yeu cau
        huy — kiem tra TRUOC moi khoi Demucs de dung ngay, raise SeparationCancelled.
        """
        os.makedirs(out_dir, exist_ok=True)
        vocals_path = os.path.join(out_dir, "vocals.wav")
        no_vocals_path = os.path.join(out_dir, "no_vocals.wav")
        marker_path = os.path.join(out_dir, "separation_info.json")

        # Chon thiet bi + model TRUOC de dua ten model vao fingerprint (doi model
        # -> cache cu vo hieu, tach lai bang model moi).
        from utils.hardware_utils import get_best_device

        device, _ = get_best_device()
        model_name = cls._pick_model_name(device)

        source_media_path, src_stat = cls.resolve_source_media(source_media_path)
        fingerprint = {
            "source": os.path.abspath(source_media_path),
            "mtime": src_stat.st_mtime,
            "size": src_stat.st_size,
            "model": model_name,
        }
        if (
            os.path.exists(vocals_path)
            and os.path.exists(no_vocals_path)
            and os.path.exists(marker_path)
        ):
            try:
                with open(marker_path, "r", encoding="utf-8") as f:
                    if json.load(f) == fingerprint:
                        logger.info(L("Tách nhạc nền: dùng lại kết quả đã tách trước đó (cache hit).", "Music separation: reusing the previous separation result (cache hit)."))
                        return {"vocals": vocals_path, "no_vocals": no_vocals_path}
            except Exception:
                pass  # marker hong -> tach lai

        # 1) Decode nguon -> WAV 44.1kHz stereo. KHONG dung source_audio.wav
        #    16kHz mono cua Whisper (mat chat luong + demucs can stereo 44.1k).
        #    `-rf64 auto`: video rat dai (>4GB WAV, ~10h) -> ffmpeg tu chuyen sang
        #    RF64 (header 64-bit) thay vi ghi RIFF hong; soundfile doc RF64 binh thuong.
        hq_wav = os.path.join(out_dir, "source_hq.wav")
        cmd = [
            FFMPEG_EXE, "-y", "-i", source_media_path, "-vn",
            "-acodec", "pcm_s16le", "-ac", "2", "-ar", "44100",
            "-rf64", "auto", hq_wav,
        ]
        # Chay ffmpeg dang Popen + poll de co the HUY giua chung (video 10h decode
        # mat vai phut — khong duoc block khong the huy).
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, close_fds=True,
        )
        # PHẢI drain stderr LIÊN TỤC trong lúc chờ: ffmpeg ghi dòng stats tiến độ
        # liên tục; không đọc thì buffer pipe (~64KB) đầy sau vài phút decode ->
        # ffmpeg block khi ghi -> poll() không bao giờ thoát -> treo vĩnh viễn.
        _err_chunks: list = []

        def _drain_stderr():
            try:
                for line in proc.stderr:
                    if len(_err_chunks) < 400:
                        _err_chunks.append(line)
            except Exception:
                pass

        _drain_t = threading.Thread(target=_drain_stderr, daemon=True)
        _drain_t.start()
        try:
            while proc.poll() is None:
                if check_cancel is not None and check_cancel():
                    proc.kill()
                    proc.wait()
                    try:
                        os.remove(hq_wav)  # xoa file WAV ghi do dang
                    except Exception:
                        pass
                    raise SeparationCancelled()
                time.sleep(0.5)
        finally:
            if proc.poll() is None:
                proc.kill()
            _drain_t.join(timeout=2)
        if proc.returncode != 0:
            err_txt = b"".join(_err_chunks).decode("utf-8", errors="replace")
            raise SeparationError(
                L(f"Không giải mã được audio nguồn để tách: {err_txt[-500:]}", f"Could not decode the source audio for separation: {err_txt[-500:]}")
            )

        try:
            # 2) Chay demucs THEO KHOI (chunk). apply_model tra ve tensor ket qua
            #    cho TOAN BO input cung luc -> voi video dai (5-10 phut) tensor
            #    4-stem stereo float32 co the > 1GB va OOM RAM CPU (kec ca khi da
            #    chia segment nho de vua VRAM). Giai phap: cat audio thanh cac khoi
            #    ~45s (co overlap de crossfade), tach tung khoi roi GHI THANG (append)
            #    ra 2 file WAV. RAM dinh KHONG tang theo do dai video.
            import numpy as np
            import soundfile as sf
            import torch
            from demucs.apply import apply_model

            model = cls._load_model(model_name)
            v_idx = model.sources.index("vocals")  # htdemucs: [drums,bass,other,vocals]

            # DOC STREAMING bang soundfile (khong nap ca file vao RAM — video 10h
            # WAV ~6GB se OOM neu doc mot phat; soundfile cung doc duoc RF64 >4GB).
            src_f = sf.SoundFile(hq_wav, "r")
            sr = src_f.samplerate
            total = len(src_f)  # tong so frame
            audio_sec = total / sr

            # Tinh mean/std de chuan hoa tu MAU DAI DIEN (khong doc het file):
            # lay toi da ~40 block 5s rai deu tren toan bo file, cong don tong/binh phuong
            # tren kenh mono-mix roi suy ra mean/std toan cuc gan dung.
            _stat_block = int(5 * sr)
            _n_stat = 40
            _stat_step = max(_stat_block, total // _n_stat) if total else _stat_block
            _s = _s2 = 0.0
            _n = 0
            _p = 0
            while _p < total:
                src_f.seek(_p)
                blk = src_f.read(min(_stat_block, total - _p),
                                 dtype="float32", always_2d=True)  # (L, 2)
                mono = blk.mean(axis=1)
                _s += float(mono.sum())
                _s2 += float((mono ** 2).sum())
                _n += mono.shape[0]
                _p += _stat_step
            if _n > 0:
                mean = _s / _n
                std = math.sqrt(max(_s2 / _n - mean * mean, 0.0)) + 1e-8
            else:
                mean, std = 0.0, 1e-8

            if device == "cpu":
                logger.warning(
                    L(f"Tách nhạc nền: máy không có GPU nên chạy bằng CPU, sẽ chậm hơn. "
                    f"Video dài có thể mất vài phút (khoảng ~{audio_sec/60*0.9:.0f} phút cho {audio_sec/60:.0f} phút video).", f"Music separation: no GPU on this machine so running on CPU, it will be slower. A long video may take several minutes (about ~{audio_sec/60*0.9:.0f} minutes for {audio_sec/60:.0f} minutes of video).")
                )
            logger.info(
                L(f"Tách nhạc nền: đang tách giọng gốc và nhạc nền ({audio_sec:.0f} giây âm thanh)...", f"Music separation: separating the original voice and background music ({audio_sec:.0f} seconds of audio)...")
            )

            # Heartbeat: log moi 10s de UI biet dang chay (khong tuong treo), kem % da
            # xu ly (uoc luong tu vi tri con tro `pos` tren tong so mau audio).
            done = threading.Event()
            progress = {"done_sec": 0.0}  # so giay audio da tach xong (cap nhat sau moi chunk)

            def _heartbeat():
                t0 = time.time()
                while not done.wait(10):
                    pct = min(99.0, progress["done_sec"] / audio_sec * 100.0) if audio_sec else 0.0
                    logger.info(
                        L(f"Tách nhạc nền: đang xử lý {pct:.0f}% "
                        f"({progress['done_sec']:.0f}s/{audio_sec:.0f}s) — đã chạy {time.time()-t0:.0f}s", f"Music separation: processing {pct:.0f}% ({progress['done_sec']:.0f}s/{audio_sec:.0f}s) — ran for {time.time()-t0:.0f}s")
                    )

            hb = threading.Thread(target=_heartbeat, daemon=True)
            hb.start()

            # VRAM thap: chia SEGMENT NHO ben trong apply_model de vua VRAM.
            seg = None
            if device == "cuda":
                try:
                    free_b, _ = torch.cuda.mem_get_info()
                    free_gb = free_b / (1024 ** 3)
                    if free_gb < 4.5:
                        seg = 2.0
                        logger.info(
                            L(f"Tách nhạc nền: VRAM trống ~{free_gb:.1f}GB rất thấp -> chia đoạn {seg}s để tránh OOM.", f"Music separation: free VRAM ~{free_gb:.1f}GB is very low -> splitting into {seg}s segments to avoid OOM.")
                        )
                    elif free_gb < 6.5:
                        seg = 4.0
                        logger.info(
                            L(f"Tách nhạc nền: VRAM trống ~{free_gb:.1f}GB -> chia đoạn {seg}s để tránh OOM.", f"Music separation: free VRAM ~{free_gb:.1f}GB -> splitting into {seg}s segments to avoid OOM.")
                        )
                except Exception:
                    seg = 5.0

            # Khoi ngoai (chunk) gioi han RAM cho tensor KET QUA. 45s/khoi + overlap
            # 3s (crossfade tuyen tinh de khong nghe ro moi noi giua cac khoi).
            CHUNK_SEC = 45.0
            OVERLAP_SEC = 3.0
            chunk_len = int(CHUNK_SEC * sr)
            ov = int(OVERLAP_SEC * sr)

            def _apply(dev, segment, chunk):
                kw = dict(
                    device=dev, split=True, overlap=APPLY_OVERLAP,
                    shifts=0, progress=False,
                )
                if segment:
                    kw["segment"] = segment
                with torch.no_grad():
                    # chunk: (2, L) -> (stems, 2, L)
                    return apply_model(model, chunk[None], **kw)[0]

            def _sep_chunk(chunk):
                """Tra ve (vocals (2,L), no_vocals (2,L)) da chuan hoa nguoc, tren CPU."""
                nonlocal device, model, model_name
                c_norm = (chunk - mean) / std
                try:
                    src = _apply(device, seg, c_norm)
                except (torch.cuda.OutOfMemoryError, RuntimeError) as e:
                    if device == "cuda" and (
                        "out of memory" in str(e).lower()
                        or isinstance(e, torch.cuda.OutOfMemoryError)
                    ):
                        logger.warning(
                            L(f"Tách nhạc nền: GPU hết VRAM. Dọn cache và lùi về model {MODEL_FOR_CPU} trên CPU.", f"Music separation: GPU out of VRAM. Clearing the cache and falling back to model {MODEL_FOR_CPU} on CPU.")
                        )
                        device = "cpu"
                        if model_name != MODEL_FOR_CPU:
                            # GỠ model GPU cũ khỏi cache + chuyển nó off-GPU rồi mới
                            # empty_cache, nếu không htdemucs GPU vẫn nằm trong _models
                            # giữ VRAM tới khi tắt process (rò VRAM trong phiên).
                            with cls._lock:
                                cls._models.pop(model_name, None)
                            try:
                                model.to("cpu")
                            except Exception:
                                pass
                            model = None
                            try:
                                torch.cuda.empty_cache()
                            except Exception:
                                pass
                            model = cls._load_model(MODEL_FOR_CPU)
                            model_name = MODEL_FOR_CPU
                        else:
                            model = model.to("cpu")
                            try:
                                torch.cuda.empty_cache()
                            except Exception:
                                pass
                        src = _apply("cpu", seg or 5.0, c_norm)
                    elif _la_het_bo_nho(e):
                        #  HET RAM MAY (khong phai VRAM) — duong lui ve CPU o tren khong
                        #  cuu duoc, vi CPU moi la cho vua het cho. De nguyen thi khach
                        #  nhan mot vet loi torch tieng Anh kem con so byte tho
                        #  ("Tried to allocate 1870328176 bytes") — khong hieu, khong
                        #  biet lam gi. Noi thang ra va chi cho ho ba duong thoat.
                        raise SeparationError(
                            L("Máy hết bộ nhớ (RAM) khi tách giọng khỏi nhạc nền. "
                            "Hãy thử: đóng bớt ứng dụng đang mở (nhất là trình duyệt), "
                            "hoặc TẮT tuỳ chọn 'Tách giọng gốc & nhạc nền' trong Chuẩn bị, "
                            "hoặc cắt video thành các phần ngắn hơn rồi chạy lại. "
                            f"Chi tiết kỹ thuật: {str(e)[:200]}", f"The machine ran out of memory (RAM) while separating the voice from the background music. Try: closing some open applications (especially the browser), or TURN OFF the option 'Separate original voice & background music' in Prepare, or cut the video into shorter parts and run again. Technical details: {str(e)[:200]}")
                        ) from e
                    else:
                        raise
                src = src.cpu()
                src.mul_(std).add_(mean)  # chuan hoa nguoc, in-place
                vocals = src[v_idx].clone()
                no_vocals = None
                for i in range(src.shape[0]):
                    if i == v_idx:
                        continue
                    if no_vocals is None:
                        no_vocals = src[i].clone()
                    else:
                        no_vocals.add_(src[i])
                del src
                return vocals, no_vocals

            def _open_wav(path):
                # Ghi bang soundfile format RF64 (tuong thich WAV, header 64-bit)
                # -> stems co the vuot 4GB (video 10h). ffmpeg doc RF64 binh thuong.
                return sf.SoundFile(
                    path, "w", samplerate=sr, channels=2,
                    subtype="PCM_16", format="RF64",
                )

            def _write(w, tensor):
                # tensor (2, L) float -> (L, 2) float32; soundfile tu doi sang PCM_16.
                # .contiguous() vi tensor co the la slice/ket qua crossfade (non-contiguous).
                arr = tensor.clamp(-1, 1).contiguous().numpy().T
                w.write(np.ascontiguousarray(arr, dtype=np.float32))

            v_out = _open_wav(vocals_path)
            n_out = _open_wav(no_vocals_path)
            # Duoi cua khoi truoc (vung overlap) de crossfade voi dau khoi sau.
            prev_v_tail = prev_n_tail = None
            fade = torch.linspace(0.0, 1.0, ov) if ov > 0 else None  # (ov,)

            # Uoc luong so khoi (chunk) de log "khoi k/N". Buoc nhay thuc te la
            # (chunk_len - ov) do co overlap, nen chia theo do.
            step = max(1, chunk_len - ov)
            total_chunks = max(1, math.ceil(total / step))

            def _fmt(sec):
                m, s = divmod(int(sec), 60)
                return f"{m:02d}:{s:02d}"

            try:
                pos = 0
                chunk_idx = 0
                while pos < total:
                    # Huy giua chung: kiem TRUOC moi khoi -> dung ngay thay vi chay het
                    # ~22 phut. Raise SeparationCancelled (di qua except sach o duoi).
                    if check_cancel is not None and check_cancel():
                        raise SeparationCancelled()
                    end = min(pos + chunk_len, total)
                    chunk_idx += 1
                    seg_start = pos / sr
                    seg_end = end / sr
                    logger.info(
                        L(f"Tách nhạc nền: khối {chunk_idx}/{total_chunks} "
                        f"({_fmt(seg_start)}–{_fmt(seg_end)}) — bắt đầu tách...", f"Music separation: chunk {chunk_idx}/{total_chunks} ({_fmt(seg_start)}–{_fmt(seg_end)}) — starting separation...")
                    )
                    t_chunk = time.time()
                    # Doc dung khoi can xu ly tu file (streaming, khong giu ca file trong RAM).
                    src_f.seek(pos)
                    chunk_np = src_f.read(end - pos, dtype="float32", always_2d=True)  # (L, 2)
                    chunk = torch.from_numpy(np.ascontiguousarray(chunk_np.T))  # (2, L)
                    del chunk_np
                    v, n = _sep_chunk(chunk)  # (2, L) moi cai
                    del chunk
                    # Cap nhat tien do cho heartbeat + log xong khoi kem %.
                    progress["done_sec"] = seg_end
                    pct = min(99.0, seg_end / audio_sec * 100.0) if audio_sec else 0.0
                    logger.info(
                        L(f"Tách nhạc nền: khối {chunk_idx}/{total_chunks} xong "
                        f"({time.time()-t_chunk:.1f}s) — tiến độ {pct:.0f}% "
                        f"({_fmt(seg_end)}/{_fmt(audio_sec)})", f"Music separation: chunk {chunk_idx}/{total_chunks} done ({time.time()-t_chunk:.1f}s) — progress {pct:.0f}% ({_fmt(seg_end)}/{_fmt(audio_sec)})")
                    )

                    if prev_v_tail is not None and ov > 0 and v.shape[1] >= ov:
                        # crossfade: dau khoi hien tai (0..ov) tron voi duoi khoi truoc
                        f = fade[: v.shape[1]] if v.shape[1] < ov else fade
                        nf = f.shape[0]
                        v[:, :nf] = prev_v_tail[:, :nf] * (1 - f) + v[:, :nf] * f
                        n[:, :nf] = prev_n_tail[:, :nf] * (1 - f) + n[:, :nf] * f

                    is_last = end >= total
                    if is_last:
                        # khoi cuoi: ghi het
                        _write(v_out, v)
                        _write(n_out, n)
                    else:
                        # giu lai `ov` mau cuoi de crossfade voi khoi ke; ghi phan truoc do
                        keep = ov if v.shape[1] > ov else 0
                        w_end = v.shape[1] - keep
                        _write(v_out, v[:, :w_end])
                        _write(n_out, n[:, :w_end])
                        prev_v_tail = v[:, w_end:].clone()
                        prev_n_tail = n[:, w_end:].clone()
                        # buoc nhay: tien chunk_len nhung lui `ov` de dap overlap
                        pos = end - keep
                        del v, n
                        continue
                    del v, n
                    pos = end
            finally:
                done.set()
                src_f.close()
                v_out.close()
                n_out.close()
        except (SeparationError, SeparationCancelled):
            raise  # huy / loi da co ngu nghia rieng -> khong boc lai
        except Exception as e:
            raise SeparationError(L(f"Tách giọng gốc / nhạc nền thất bại: {e}", f"Original voice / background music separation failed: {e}"))
        finally:
            try:
                os.remove(hq_wav)
            except Exception:
                pass

        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump(fingerprint, f)
        logger.info(L("Tách nhạc nền: đã tách xong giọng gốc và nhạc nền.", "Music separation: finished separating the original voice and background music."))
        return {"vocals": vocals_path, "no_vocals": no_vocals_path}
