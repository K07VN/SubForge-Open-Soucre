from utils.i18n import L
import edge_tts
from edge_tts import communicate as _edge_communicate
from edge_tts import constants as _edge_constants

#  EDGE TTS ĐỔI MÁY CHỦ (từ ~10-10-2026): Microsoft trả 403 ở bước bắt tay WebSocket trên máy chủ cũ
#  ``speech.platform.bing.com`` cho mọi bản edge-tts (đã thử 7.2.8 = bản mới nhất, đổi số phiên bản Edge cũng không ăn).
#  Trình duyệt Edge thật nay gọi ``api.msedgeservices.com`` với cùng khoá, đặt ở tham số ``Ocp-Apim-Subscription-Key``
#  (rany2/edge-tts issue #490, PR #491). Đo trên máy dev 13-10-2026: máy chủ cũ 403, máy chủ mới trả audio bình thường.
#  Chỉ vá khi thư viện CÒN trỏ máy chủ cũ — bản edge-tts sau này tự sửa thì để nguyên của nó.
#  Danh sách giọng (HTTP) vẫn chạy ở máy chủ cũ nên không đụng tới.
if "speech.platform.bing.com" in str(getattr(_edge_communicate, "WSS_URL", "")):
    _edge_communicate.WSS_URL = (
        "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
        f"?Ocp-Apim-Subscription-Key={_edge_constants.TRUSTED_CLIENT_TOKEN}"
    )
import asyncio
import os
import hashlib
import random
import shutil
from mutagen.mp3 import MP3
import logging

from models.database import get_app_data_dir
from utils.model_paths import ModelNotDownloadedError
from utils.file_utils import safe_replace, safe_remove
# VieNeu-TTS offline (module nhe; phan nang duoc import lazy ben trong service)
from services.vieneu_service import VieneuService, is_vieneu_voice
#  Một điểm phân luồng bộ đọc giọng (Edge / VieNeu / Qwen) — xem tts_engines.py.
from services.tts_text import lam_sach_cau_doc
from services.tts_engines import (
    ENGINE_EDGE,
    ENGINE_QWEN,
    assert_edge_voice,
    engine_cache_tag,
    engine_of,
)

logger = logging.getLogger("TTSService")

# Chỉ CẢNH BÁO "cắt cụt" khi phần đuôi bị cắt ĐÁNG KỂ (vượt ngưỡng này). Audio VẪN
# luôn được trim về đúng khung thời gian (tránh đè segment kế tiếp), nhưng nếu chỉ
# dư dưới ngưỡng — thường là khoảng lặng ở đuôi — thì KHÔNG gắn cờ is_trimmed, để
# banner "N đoạn bị cắt cụt" không báo oan khi giọng đọc thực chất vẫn đủ chữ.
# 0.2s: cảnh báo nhạy hơn để bắt các đoạn giọng dài bị cắt cụt phần đuôi.
TRIM_WARN_THRESHOLD_SEC = 0.2

# Số lần thử tối đa cho 1 câu qua Edge-TTS (mạng). 5 lần thử = 4 lần chờ
# (2s, 4s, 8s, 16s ±25% jitter) -> tổng chờ ~22-38s, đủ vượt cửa sổ rate-limit
# của Edge-TTS khi chạy video vài nghìn câu. Ba lần cũ (tổng 4.5s) quá ngắn:
# hết lượt là câu hỏng vĩnh viễn trong lượt chạy đó.
_TTS_MAX_ATTEMPTS = 5
_TTS_RETRY_BASE_SEC = 2.0


def _tts_retry_delay(attempt: int) -> float:
    """Backoff mũ + jitter cho lần thử thứ `attempt` (1-based): 2s, 4s, 8s, 16s.

    Jitter ±25% để 35 luồng cùng bị rate-limit KHÔNG thử lại cùng một thời điểm
    (thundering herd) — nếu không, cả cụm lại đập vào server một lúc và bị chặn
    tiếp, backoff mũ mất tác dụng."""
    delay = _TTS_RETRY_BASE_SEC * (2 ** (attempt - 1))
    return delay * random.uniform(0.75, 1.25)


def _rate_str_to_factor(rate: str) -> float:
    """Chuyển chuỗi rate Edge-style ("+45%", "-10%", "+0%") -> hệ số tốc độ.
    "+45%" -> 1.45, "-10%" -> 0.9, "+0%" -> 1.0. Trả 1.0 nếu không parse được
    (bao gồm cả "auto"). Kẹp trong [0.5, 1.8] để an toàn cho atempo."""
    try:
        s = (rate or "").strip()
        if s.endswith("%"):
            val = float(s[:-1])  # đã gồm dấu +/-
            factor = 1.0 + val / 100.0
            return max(0.5, min(1.8, factor))
    except Exception:
        pass
    return 1.0


#  HÀNG ĐỢI QWEN PHÍA ASYNCIO. Máy chủ Qwen xử lý TỪNG câu một (khoá trong
#  QwenTTSService). Không xếp hàng ở đây thì semaphore 35 của TTSService đẩy 35 câu vào
#  ``asyncio.to_thread`` cùng lúc: cả bộ luồng mặc định (~cpu+4 luồng) bị chiếm chỉ để ĐỨNG
#  CHỜ khoá, suốt cả lượt lồng tiếng phim dài — mọi việc khác của backend cần
#  ``to_thread`` phải xếp sau. Xếp hàng bằng khoá asyncio thì lúc nào cũng chỉ MỘT luồng.
#  Khoá theo từng event loop (test gọi asyncio.run nhiều lần; khoá asyncio gắn với loop).
_QWEN_KHOA: dict = {}


def _khoa_qwen() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    k = _QWEN_KHOA.get(id(loop))
    if k is None or _QWEN_KHOA.get(("loop", id(loop))) is not loop:
        k = asyncio.Lock()
        _QWEN_KHOA[id(loop)] = k
        _QWEN_KHOA[("loop", id(loop))] = loop
    return k


class TTSService:
    def __init__(self, voice="vi-VN-NamMinhNeural"):
        self.voice = voice
        self._semaphores = {}
        self.audio_cache = {}  # Cache for generated audio paths
        # Lưu cache TTS vào AppData (ghi được), KHÔNG đặt cạnh file thực thi:
        # bản đóng gói per-machine nằm trong Program Files -> standard user không
        # ghi được -> os.makedirs sẽ ném PermissionError và crash backend lúc start.
        self.cache_dir = os.path.join(get_app_data_dir(), "ai_models", "tts_cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    @property
    def semaphore(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.Semaphore(35)

        if not hasattr(self, "_semaphores") or self._semaphores is None:
            self._semaphores = {}
        if loop not in self._semaphores:
            self._semaphores[loop] = asyncio.Semaphore(35)
        return self._semaphores[loop]

    def _trim_mp3(self, path: str, target_duration: float) -> bool:
        """Cat file MP3 xuong dung target_duration bang ffmpeg (best-effort)."""
        import subprocess
        import imageio_ffmpeg
        temp_path = path + ".trimmed.mp3"
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_bin, "-y", "-i", path, "-t", f"{target_duration:.3f}",
               "-c:a", "libmp3lame", "-b:a", "128k", temp_path]
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, close_fds=True)
        if res.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            os.remove(path)
            shutil.move(temp_path, path)
            return True
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return False

    async def generate_voice(self, text: str, output_path: str, voice: str = None, rate: str = "+0%", overwrite: bool = False, target_duration: float = None, raise_on_error: bool = False, speed: float = 1.0, lan_sinh: int = 0) -> tuple[bool, bool]:
        """Generate audio for a single text segment with concurrency control and caching. Returns (success, is_trimmed).
        ``lan_sinh`` > 0 (Sáng tạo: "làm lại cảnh"): CHỈ giọng Qwen — seed lệch đi + khoá lưu đệm khác, để câu đọc ra rác được sinh lại thành bản khác; Edge / VieNeu bỏ qua (khoá giữ nguyên từng byte)."""
        if not text:
            return False, False

        # Nếu có tham số speed (float) thì tự động chuyển thành rate string cho Edge TTS
        if speed is not None and speed > 0 and rate == "+0%":
            percent = int(round((speed - 1.0) * 100))
            rate = f"{percent:+d}%"

        # ...và NGƯỢC LẠI cho VieNeu: model offline KHÔNG có tham số rate, tốc độ chỉ
        # áp được bằng atempo qua `speed`. Lồng tiếng cơ bản trên máy CPU-only gọi
        # thẳng generate_voice(rate="+50%") -> trước đây rate bị bỏ qua ÂM THẦM nên
        # người dùng chọn 1.5x/1.8x mà giọng vẫn đọc y hệt 1.0x. Máy có GPU không lộ
        # lỗi vì đi nhánh batch riêng (tự gọi speedup_mp3), nên chỉ máy khách gặp.
        #  (Qwen3-TTS cũng không có tham số rate -> cùng đường với VieNeu.)
        if engine_of(voice or self.voice) != ENGINE_EDGE and (
            speed is None or abs(speed - 1.0) <= 0.01
        ):
            speed = _rate_str_to_factor(rate)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        # Create cache key
        target_dur_str = f"|{target_duration:.3f}" if target_duration is not None else ""
        #  engine_cache_tag: RỖNG với Edge/VieNeu (khoá cũ giữ nguyên từng byte — xem
        #  test_tts_cache_key_golden); giọng Qwen thêm phiên bản + vân tay mẫu clone.
        lan_tag = f"|lan{int(lan_sinh)}" if lan_sinh and engine_of(voice or self.voice) == ENGINE_QWEN else ""
        cache_key = hashlib.md5(f"{text}|{voice or self.voice}|{rate}|spd_{speed}{target_dur_str}{engine_cache_tag(voice or self.voice)}{lan_tag}".encode()).hexdigest()
        persistent_cache_path = os.path.join(self.cache_dir, f"{cache_key}.mp3")
        #  LÀM SẠCH chuỗi gửi sang bộ đọc (phẩy cuối câu, gạch đầu dòng, ký tự vô hình… — xem services/tts_text). Đặt SAU
        #  khoá đệm để khoá của mọi câu cũ giữ nguyên từng byte (test_tts_cache_key_golden); chữ ở timeline / phụ đề không đổi.
        text = lam_sach_cau_doc(text)
        if cache_key in self.audio_cache and not overwrite:
            cached_path = self.audio_cache[cache_key]
            if os.path.exists(cached_path) and os.path.getsize(cached_path) > 0:
                # Copy cached file to output_path if different
                if cached_path != output_path:
                    shutil.copy2(cached_path, output_path)
                return True, False
        if not overwrite and os.path.exists(persistent_cache_path) and os.path.getsize(persistent_cache_path) > 0:
            if persistent_cache_path != output_path:
                shutil.copy2(persistent_cache_path, output_path)
            self.audio_cache[cache_key] = persistent_cache_path
            return True, False
        
        # Check if already exists to save time (unless overwrite is requested)
        # Fix: If the file exists but is 0 bytes, we MUST re-generate it.
        if not overwrite and os.path.exists(output_path):
            size = os.path.getsize(output_path)
            if size > 0:
                self.audio_cache[cache_key] = output_path
                if output_path != persistent_cache_path and not os.path.exists(persistent_cache_path):
                    shutil.copy2(output_path, persistent_cache_path)
                return True, False
            else:
                logger.warning(L(f"TTS: Phát hiện file hỏng 0 byte cho '{text[:20]}...', đang tạo lại...", f"TTS: Found a corrupted 0-byte file for '{text[:20]}...', regenerating..."))
 
        async with self.semaphore:
            # GHI VÀO FILE TẠM trước, chỉ thay thế file cũ khi bản mới HOÀN CHỈNH
            # (os.replace atomic). Nhờ vậy nếu sinh lại lỗi / bị hủy giữa chừng thì
            # giọng CŨ vẫn còn nguyên — chỉ có "cái mới" xong mới xóa "cái cũ".
            work_path = output_path + ".new.mp3"
            try:
                if os.path.exists(work_path):
                    try: os.remove(work_path)
                    except OSError: pass

                selected_voice = voice or self.voice
                if is_vieneu_voice(selected_voice):
                    # VieNeu offline: khong co tham so rate (auto-fit xu ly bang
                    # atempo o generate_voice_auto_fit). Sinh tuan tu qua to_thread
                    # (model CPU-bound, co lock noi bo).
                    ok = await asyncio.to_thread(
                        VieneuService.synthesize, text, selected_voice, work_path
                    )
                    if not ok:
                        return False, False

                    # Đổi tốc độ đọc nếu khác 1.0x. Dùng CHUNG speedup_mp3 với nhánh
                    # GPU/auto-fit: cùng kẹp [0.5, 1.8], cùng giải nén WAV trước khi
                    # atempo (đỡ một lớp nhiễu nén) -> cùng một tốc độ cho ra cùng
                    # một kết quả dù đi đường nào.
                    if speed is not None and speed > 0 and abs(speed - 1.0) > 0.01 and os.path.exists(work_path):
                        try:
                            await asyncio.to_thread(
                                VieneuService.speedup_mp3, work_path, speed
                            )
                        except Exception as e_spd:
                            logger.warning(L(f"Lỗi chỉnh tốc độ đọc cho VieNeu: {e_spd}", f"Error adjusting the reading speed for VieNeu: {e_spd}"))
                elif engine_of(selected_voice) == ENGINE_QWEN:
                    #  Qwen3-TTS (chạy trên máy, 10 thứ tiếng): như VieNeu — không có
                    #  tham số rate, sinh ở 1.0x rồi atempo. Server xử lý tuần tự: xếp hàng
                    #  ở khoá asyncio (``_khoa_qwen``) để không chiếm hết bộ luồng.
                    from services.qwen_tts_service import QwenTTSService

                    async with _khoa_qwen():
                        ok = await asyncio.to_thread(
                            QwenTTSService.synthesize, text, selected_voice, work_path, **({"lan_sinh": int(lan_sinh)} if lan_tag else {})
                        )
                    if not ok:
                        return False, False
                    if speed is not None and speed > 0 and abs(speed - 1.0) > 0.01 and os.path.exists(work_path):
                        try:
                            await asyncio.to_thread(
                                VieneuService.speedup_mp3, work_path, speed
                            )
                        except Exception as e_spd:
                            logger.warning(L(f"Lỗi chỉnh tốc độ đọc cho Qwen3-TTS: {e_spd}", f"Error adjusting the reading speed for Qwen3-TTS: {e_spd}"))
                else:
                    #  CHỐT CHẶN: mã Edge thật không bao giờ chứa ":" — gặp là bug định
                    #  tuyến, dừng tại đây chứ không gửi lên máy chủ Microsoft.
                    assert_edge_voice(selected_voice)
                    # Edge-TTS qua mạng hay lỗi lặt vặt (timeout/mất kết nối) → thử lại
                    # với backoff MŨ + jitter.
                    #
                    # VI SAO KHONG PHAI BACKOFF TUYEN TINH 3 LAN NUA: video vai nghin
                    # cau lam Edge-TTS rate-limit ca CUM request. Cua so chan keo dai
                    # hon nhieu so voi tong 4.5s cua ba lan thu cu -> cau do hong han
                    # trong luot chay, gay "chay thieu". Backoff mu cho tong ~62s.
                    #
                    # Jitter la BAT BUOC: 35 luong cung bi chan mot luc, neu cung ngu
                    # dung 2s roi cung thu lai thi lai dap vao server cung mot thoi
                    # diem (thundering herd) -> lai bi chan tiep.
                    last_err = None
                    for attempt in range(1, _TTS_MAX_ATTEMPTS + 1):
                        try:
                            communicate = edge_tts.Communicate(text, selected_voice, rate=rate)
                            await communicate.save(work_path)
                            if os.path.exists(work_path) and os.path.getsize(work_path) > 0:
                                last_err = None
                                break
                            last_err = RuntimeError("Edge-TTS trả về file rỗng")
                        except Exception as e:
                            last_err = e
                        if attempt < _TTS_MAX_ATTEMPTS:
                            delay = _tts_retry_delay(attempt)
                            logger.warning(
                                L(f"Edge-TTS lỗi (lần {attempt}/{_TTS_MAX_ATTEMPTS}) cho '{text[:20]}...': "
                                f"{last_err} — thử lại sau {delay:.1f}s", f"Edge-TTS error (attempt {attempt}/{_TTS_MAX_ATTEMPTS}) for '{text[:20]}...': {last_err} — retrying in {delay:.1f}s")
                            )
                            await asyncio.sleep(delay)
                    if last_err is not None:
                        raise last_err

                # Integrity check: Ensure file is not empty
                if not os.path.exists(work_path) or os.path.getsize(work_path) == 0:
                    if os.path.exists(work_path):
                        try: os.remove(work_path)
                        except OSError: pass
                    logger.warning(L(f"TTS Cảnh báo: Tạo ra file rỗng cho văn bản: {text[:30]}...", f"TTS Warning: Produced an empty file for the text: {text[:30]}..."))
                    return False, False

                # Trim logic (thao tác trên file tạm)
                is_trimmed = False
                if target_duration is not None and target_duration > 0:
                    try:
                        audio = MP3(work_path)
                        actual_dur = audio.info.length
                    except Exception:
                        actual_dur = 0

                    if actual_dur > target_duration + 0.02:
                        temp_path = work_path + ".trimmed.mp3"
                        import subprocess
                        import imageio_ffmpeg
                        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
                        cmd = [
                            ffmpeg_bin, "-y",
                            "-i", work_path,
                            "-t", f"{target_duration:.3f}",
                            "-c:a", "libmp3lame",
                            "-b:a", "128k",
                            temp_path
                        ]
                        res = subprocess.run(
                            cmd,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            close_fds=True,
                        )
                        if res.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                            safe_replace(temp_path, work_path)
                            # Chỉ cảnh báo khi cắt đáng kể (đuôi ngắn thường là im lặng).
                            is_trimmed = (actual_dur - target_duration) > TRIM_WARN_THRESHOLD_SEC
                            logger.info(L(f"Cắt bớt giọng đọc từ {actual_dur:.2f}s còn {target_duration:.2f}s cho câu '{text[:20]}...'", f"Trimming the voice from {actual_dur:.2f}s to {target_duration:.2f}s for sentence '{text[:20]}...'"))
                        else:
                            if os.path.exists(temp_path):
                                safe_remove(temp_path)
                            logger.warning(L(f"TTS Cắt Cảnh báo: Gọt âm thanh thất bại: {res.stderr.decode('utf-8', errors='ignore')}", f"TTS Trim Warning: Trimming the audio failed: {res.stderr.decode('utf-8', errors='ignore')}"))

                # Bản mới đã hoàn chỉnh -> thay thế file cũ (atomic) rồi mới cập nhật cache.
                safe_replace(work_path, output_path)
                if output_path != persistent_cache_path:
                    shutil.copy2(output_path, persistent_cache_path)
                    self.audio_cache[cache_key] = persistent_cache_path
                else:
                    self.audio_cache[cache_key] = output_path
                return True, is_trimmed
            except ModelNotDownloadedError:
                # Loi "chua tai model" phai NOI LEN pipeline de hien thi ro cho nguoi
                # dung (huong dan sang Quan ly Model AI), khong nuot thanh that bai chung.
                # KHONG dung tay file cu output_path — chi don file tam.
                if os.path.exists(work_path):
                    try: os.remove(work_path)
                    except OSError: pass
                raise
            except Exception as e:
                logger.error(L(f"TTS Lỗi: {e}", f"TTS Error: {e}"), exc_info=True)
                # Chi don file tam; GIU NGUYEN giong cu o output_path neu co.
                if os.path.exists(work_path):
                    try: os.remove(work_path)
                    except OSError: pass
                if raise_on_error:
                    raise e
                return False, False

    async def generate_voice_auto_fit(self, text: str, output_path: str, target_duration: float, voice: str = None, overwrite: bool = False, base_rate: str = "auto") -> tuple[bool, bool]:
        """Generate audio and adjust speed if it exceeds the original segment duration. Returns (generated, is_trimmed)."""
        if not text:
            return False, False
            
        # Check if already exists to save time
        # Fix: If the file exists but is 0 bytes, we MUST re-generate it.
        if not overwrite and os.path.exists(output_path):
            size = os.path.getsize(output_path)
            if size > 0:
                return True, False
            else:
                logger.warning(L(f"TTS (Auto-fit): Phát hiện file hỏng 0 byte, đang tạo lại...", f"TTS (Auto-fit): Found a corrupted 0-byte file, regenerating..."))

        # Phân biệt 2 chế độ (xem chi tiết ở nhánh Edge-TTS bên dưới):
        #  - "auto"           : tự động khớp thời lượng đoạn.
        #  - "+X%"/"-X%"/"+0%": tốc độ CỐ ĐỊNH tuyệt đối, không khớp, không cắt.
        is_auto_fit = (base_rate == "auto")

        # --- Nhanh VieNeu: khong co tham so rate -> sinh giong binh thuong roi
        #     tang toc bang ffmpeg atempo neu dai hon target (thay vi regenerate). ---
        selected_voice_af = voice or self.voice
        #  Mọi bộ đọc KHÔNG có tham số rate (VieNeu, Qwen3-TTS) đi nhánh này.
        if engine_of(selected_voice_af) != ENGINE_EDGE:
            # Sinh + atempo/trim trên FILE TẠM, chỉ thay file cũ khi xong (atomic) để
            # hủy/lỗi giữa chừng không mất giọng cũ.
            work_path = output_path + ".new.mp3"
            if os.path.exists(work_path):
                try: os.remove(work_path)
                except OSError: pass
            generated, _ = await self.generate_voice(text, work_path, voice=selected_voice_af, overwrite=True)
            if not generated or not os.path.exists(work_path) or os.path.getsize(work_path) == 0:
                if os.path.exists(work_path):
                    try: os.remove(work_path)
                    except OSError: pass
                return False, False
            try:
                duration = MP3(work_path).info.length
            except Exception:
                if os.path.exists(work_path):
                    try: os.remove(work_path)
                    except OSError: pass
                return False, False

            is_trimmed = False
            if not is_auto_fit:
                # CỐ ĐỊNH: VieNeu sinh ở tốc độ tự nhiên (1.0x), nên áp đúng hệ số
                # tốc độ đã chọn bằng atempo (KHÔNG tự tăng tốc để khớp).
                fixed_factor = _rate_str_to_factor(base_rate)
                if fixed_factor and abs(fixed_factor - 1.0) > 0.01:
                    logger.info(
                        L(f"Điều chỉnh tốc độ giọng đọc x{fixed_factor:.2f} (theo tốc độ đã chọn)", f"Adjusting the voice speed x{fixed_factor:.2f} (per the chosen speed)")
                    )
                    await asyncio.to_thread(VieneuService.speedup_mp3, work_path, fixed_factor)
            else:
                # AUTO: tăng tốc để vừa target.
                if target_duration and target_duration > 0.5 and duration > target_duration + 0.05:
                    factor = duration / target_duration
                    logger.info(
                        L(f"Tăng tốc giọng đọc x{min(1.8, factor):.2f} cho vừa {target_duration:.2f}s (giọng gốc {duration:.2f}s)", f"Speeding up the voice x{min(1.8, factor):.2f} to fit {target_duration:.2f}s (original voice {duration:.2f}s)")
                    )
                    await asyncio.to_thread(VieneuService.speedup_mp3, work_path, factor)

            # Trim an toan neu van du dai — áp cho CẢ auto lẫn cố định để người dùng
            # biết đoạn bị cắt (is_trimmed), tránh giọng tràn đè đoạn kế.
            if target_duration and target_duration > 0:
                try:
                    pre_trim_dur = MP3(work_path).info.length
                    if pre_trim_dur > target_duration + 0.05:
                        self._trim_mp3(work_path, target_duration)
                        # Chỉ cảnh báo khi cắt đáng kể (> ngưỡng), không tính đuôi im lặng.
                        is_trimmed = (pre_trim_dur - target_duration) > TRIM_WARN_THRESHOLD_SEC
                except Exception:
                    pass
            if not (os.path.exists(work_path) and os.path.getsize(work_path) > 0):
                return False, False
            safe_replace(work_path, output_path)
            return True, is_trimmed

        # Phân biệt 2 chế độ theo base_rate:
        #  - "auto"  : TỰ ĐỘNG KHỚP. Sinh với sàn +40% rồi tăng tốc thêm (nếu cần)
        #              để giọng vừa khít target_duration, và cắt đuôi thừa nếu tràn.
        #  - "+X%"/"-X%"/"+0%": CỐ ĐỊNH. Giọng đọc đúng tốc độ đã chọn, KHÔNG tự tăng
        #              tốc để khớp; nhưng VẪN cắt nếu dài hơn khung + gắn cờ is_trimmed
        #              để người dùng biết đoạn đã bị cắt cho vừa.
        # Chế độ Xê dịch linh hoạt (target_duration >= 999999): Giọng đọc tự nhiên (1.0x),
        # KHÔNG cần auto-fit hay ép sàn +40% -> Sinh trực tiếp 1 lần duy nhất cực nhanh.
        if target_duration is not None and target_duration >= 999999:
            fit_rate = "+0%" if base_rate == "auto" else base_rate
            return await self.generate_voice(
                text, output_path, voice=voice, rate=fit_rate,
                overwrite=overwrite, target_duration=None,
            )

        is_auto_fit = (base_rate == "auto")

        if not is_auto_fit:
            # Chế độ CỐ ĐỊNH: gen đúng rate, để generate_voice tự trim theo
            # target_duration (đồng thời trả cờ is_trimmed).
            return await self.generate_voice(
                text, output_path, voice=voice, rate=base_rate,
                overwrite=True, target_duration=target_duration,
            )

        # === Từ đây chỉ chạy cho chế độ "auto" (tự động khớp) ===
        effective_base_rate = "+40%"  # sàn +40% cho dễ khớp

        # 1. Generate base voice - use enforced auto rate
        generated, _ = await self.generate_voice(text, output_path, voice=voice, rate=effective_base_rate, overwrite=True)
        if not generated or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False, False

        # 2. Check duration
        try:
            audio = MP3(output_path)
            duration = audio.info.length
        except Exception:
            return False, False

        # Calculate the actual numeric rate used for the first generation
        used_rate_val = 1.0
        try:
            if effective_base_rate.startswith("+"):
                used_rate_val = 1.0 + (float(effective_base_rate.strip("+%")) / 100.0)
            elif effective_base_rate.startswith("-"):
                used_rate_val = 1.0 - (float(effective_base_rate.strip("-%")) / 100.0)
        except: pass

        # Auto-speedup if the generated audio duration exceeds target_duration.
        # This prevents the speech from being cut off (trimmed) at the end.
        if duration > target_duration + 0.05 and target_duration > 0.5:
            multiplier = duration / target_duration
            new_absolute_rate = used_rate_val * multiplier

            new_rate_pct = int((new_absolute_rate - 1.0) * 100)

            # Auto luôn đảm bảo tối thiểu +40%.
            new_rate_pct = max(40, new_rate_pct)

            # Safety cap at +80% to keep voice audible
            new_rate_pct = min(new_rate_pct, 80)

            new_rate_str = f"+{new_rate_pct}%" if new_rate_pct >= 0 else f"{new_rate_pct}%"
            logger.info(L(f"Tăng tốc giọng đọc tự động {new_rate_str} cho vừa {target_duration:.2f}s (trước là {duration:.2f}s)", f"Automatically speeding up the voice {new_rate_str} to fit {target_duration:.2f}s (was {duration:.2f}s)"))

            regenerated, _ = await self.generate_voice(text, output_path, voice=voice, rate=new_rate_str, overwrite=True)
            if not regenerated or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return False, False

        # Final Trim check: Ensure the file duration does not exceed target_duration
        is_trimmed = False
        if target_duration is not None and target_duration > 0:
            try:
                audio = MP3(output_path)
                actual_dur = audio.info.length
            except Exception:
                actual_dur = 0
            
            if actual_dur > target_duration + 0.02:
                temp_path = output_path + ".trimmed.mp3"
                import subprocess
                import imageio_ffmpeg
                ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
                cmd = [
                    ffmpeg_bin, "-y",
                    "-i", output_path,
                    "-t", f"{target_duration:.3f}",
                    "-c:a", "libmp3lame",
                    "-b:a", "128k",
                    temp_path
                ]
                res = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                )
                if res.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    os.remove(output_path)
                    shutil.move(temp_path, output_path)
                    # Chỉ cảnh báo khi cắt đáng kể (đuôi ngắn thường là im lặng).
                    is_trimmed = (actual_dur - target_duration) > TRIM_WARN_THRESHOLD_SEC
                    logger.info(L(f"Cắt bớt giọng đọc từ {actual_dur:.2f}s còn {target_duration:.2f}s cho vừa câu", f"Trimming the voice from {actual_dur:.2f}s to {target_duration:.2f}s to fit the sentence"))
                else:
                    if os.path.exists(temp_path):
                        try: os.remove(temp_path)
                        except: pass

        return os.path.exists(output_path) and os.path.getsize(output_path) > 0, is_trimmed

    async def batch_generate(self, segments: list, folder_path: str, voice: str = None, overwrite: bool = False):
        """
        Generate audio for all segments.
        segments: list of dicts with {'id', 'target_text'}
        """
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
            
        tasks = []
        for seg in segments:
            seg_path = os.path.join(folder_path, f"seg_{seg['id']}.mp3")
            tasks.append(self.generate_voice(seg['target_text'], seg_path, voice=voice, overwrite=overwrite))
            
        return await asyncio.gather(*tasks)
