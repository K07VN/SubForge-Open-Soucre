"""Qwen3-TTS — giọng đọc CHẠY TRÊN MÁY cho 10 thứ tiếng (không có tiếng Việt).

Chạy qua ``qwentts.cpp`` (MIT, C++/GGML) ở chế độ ``tts-server``: MỘT tiến trình riêng
nghe HTTP trên 127.0.0.1, nạp mô hình một lần rồi sinh từng câu. Vì là tiến trình
riêng nên KHÔNG cần torch và không đụng torch 2.8 của gói GPU; chạy được CPU / Vulkan
(mọi GPU kể cả iGPU) / CUDA.

Cùng khuôn với VieneuService (classmethod + khoá), gọi từ ``TTSService.generate_voice``
qua ``asyncio.to_thread`` — xem nhánh ``ENGINE_QWEN`` ở tts_service.py.

NHỮNG ĐIỀU ĐÃ ĐO (backend/tools/bench_qwen3_tts.py, 2026-09-23) và dẫn tới thiết kế:
  · Một tiến trình chỉ nạp MỘT talker: bản CustomVoice (9 giọng có sẵn) HOẶC bản Base
    (clone). Đổi loại = khởi động lại (~3–6 s) -> gom việc theo loại là nơi gọi lo.
  · CPU chậm (RTF 2,85 với 8 luồng, ~8 với 2 nhân), iGPU nhanh (RTF 0,78) -> thang
    mức ``get_qwen_tts_tiers``: GPU trước, CPU sau cùng; hỏng mức nào lùi mức sau.
  · Giọng có sẵn đọc CHẬM hơn trần CPS của app tới ~2,8× (tiếng Hàn/Nhật) -> ngưỡng
    "chắc là lặp/ảo giác" đặt 3,5×, thấp hơn là sinh lại oan câu đúng.
  · Server trả WAV 24 kHz; cả đường xử lý của app đọc MP3 (mutagen) -> đổi sang MP3
    bằng đúng hằng số của VieNeu.
  · qwentts gốc chỉ dùng logical/2 luồng -> bản vá SubForge 0001 đọc QWEN_TTS_N_THREADS.
"""
from __future__ import annotations

from utils.i18n import L
import base64
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave

from models.database import get_app_data_dir
from services.tts_engines import QWEN_LANG_LABEL, normalize_lang, parse_qwen_voice
from utils.file_utils import safe_remove, safe_replace

logger = logging.getLogger("TTSService")   # chung logger với TTSService -> vào synthesis.log

#  9 GIỌNG CÓ SẴN của bản CustomVoice và thứ tiếng BẢN XỨ của từng giọng (README
#  Qwen3-TTS). Đo thật: cả 9 giọng đọc đúng cả 10 thứ tiếng (CER ~0) -> chào hết,
#  giọng bản xứ xếp lên đầu. Nhãn tiếng Việt để hiện trên ô chọn giọng.
QWEN_SPEAKERS: dict[str, dict] = {
    "ryan": {"native": "en", "label": "Ryan (Nam, năng động)"},
    "aiden": {"native": "en", "label": "Aiden (Nam, trong sáng)"},
    "ono_anna": {"native": "ja", "label": "Ono Anna (Nữ, tươi vui)"},
    "sohee": {"native": "ko", "label": "Sohee (Nữ, ấm áp)"},
    "vivian": {"native": "zh-cn", "label": "Vivian (Nữ, trẻ trung)"},
    "serena": {"native": "zh-cn", "label": "Serena (Nữ, dịu dàng)"},
    "uncle_fu": {"native": "zh-cn", "label": "Uncle Fu (Nam, trầm ấm)"},
    "dylan": {"native": "zh-cn", "label": "Dylan (Nam, giọng Bắc Kinh)"},
    "eric": {"native": "zh-cn", "label": "Eric (Nam, giọng Tứ Xuyên)"},
}

MODE_PRESET = "customvoice"
MODE_CLONE = "base"

#  Dài hơn mức đọc thoải mái bấy nhiêu lần thì coi là lặp/ảo giác (xem docstring).
LENGTH_GUARD_FACTOR = 3.5

#  Thông số lấy mẫu gửi kèm MỖI câu, tách theo loại giọng ("preset" = 9 giọng có sẵn,
#  "clone"). Rỗng = mặc định của qwentts.cpp (talker 0.9/top_k 50, subtalker 0.9/50,
#  phạt lặp 1.05). Chốt bằng số đo của tools/bench_tts_stability.py (độ đều cao độ /
#  tốc độ / âm lượng giữa các câu + Whisper kiểm đọc đúng chữ), không chỉnh theo cảm giác.
#  ĐỔI GIÁ TRỊ THÌ PHẢI TĂNG ``QWEN_CACHE_REV`` (services/tts_engines.py) — không thì câu
#  đã lưu đệm vẫn giữ giọng sinh bằng thông số cũ.
#    anchor : giọng có sẵn đọc qua mẫu neo + clone CÓ lời mẫu (ICL — cùng cơ chế). Đo
#             (Ryan/Ono Anna, neo): 0.6/20 ở tầng talker, subtalker để mặc định -> cao độ
#             lệch 0,9/0,8 nửa cung, 0/10 câu lạc (mặc định: 1,28/1,11). Hạ CẢ subtalker
#             xuống 0.6 lại sinh câu lạc (Ryan 2/10).
#    preset : giọng có sẵn đọc THẲNG (thứ tiếng không neo) — chưa có số đo cho thấy đổi
#             là tốt hơn -> giữ mặc định.
#    clone  : clone x-vector (khác tiếng với mẫu / không có lời mẫu) — giữ mặc định.
QWEN_SAMPLING: dict[str, dict] = {
    "preset": {},
    "anchor": {"temperature": 0.6, "top_k": 20},
    "clone": {},
}

#  NEO GIỌNG CÓ SẴN BẰNG MẪU CỐ ĐỊNH (người dùng báo 24-09-2026: giữ nguyên giọng mà mỗi
#  câu một âm điệu). Bản CustomVoice chỉ có mã người đọc, KHÔNG có mẫu âm thanh neo ngữ
#  điệu -> mỗi câu tự "bốc" một kiểu đọc. Đo (tools/bench_tts_stability.py, Ryan, 10
#  câu thuyết minh liền mạch): đọc thẳng lệch cao độ 2,91 nửa cung / tốc độ 12,6% / âm
#  lượng 2,09 dB giữa các câu, 4/10 câu "lạc"; chỉnh temperature/top_k chỉ hạ được còn
#  2/10. Neo: giọng có sẵn đọc MỘT câu mẫu (lần đầu dùng cặp giọng+tiếng, lưu lại), rồi
#  mọi câu đọc bằng bản Base bám mẫu đó (clone có lời mẫu — ICL), đúng cách VieNeu làm
#  với giọng có sẵn của nó -> 1,28 / 7,9% / 0,79 dB. Tắt = về cách đọc thẳng cũ.
QWEN_PRESET_ANCHOR = True
#  Thứ tiếng được neo. Đo thêm: Sohee/ko đọc thẳng 2/10 câu lạc, cao độ 1,64 -> neo 0/10,
#  0,50; Vivian/zh đọc thẳng đã đều, neo ngang ngửa (khác biệt Whisper chỉ là chữ phồn/
#  giản thể) -> vẫn neo để đỡ các giọng Trung khác chưa đo. Tiếng Nhật KHÔNG neo: đo thấy bản Base (đường neo) đọc sai cách
#  đọc chữ Hán nhiều hơn (日の出前 -> "hinodezume", 海辺 -> "umine"; sai chữ 0,4% -> 2,8%)
#  trong khi Ono Anna đọc thẳng đã đều (0/10 câu lạc).
QWEN_ANCHOR_LANGS = frozenset({"en", "ko", "zh-cn", "de", "fr", "ru", "pt", "es", "it"})
#  Câu mẫu mỗi thứ tiếng — ĐÚNG câu đã dùng khi đo (bench_qwen3_tts.REF_TEXT).
ANCHOR_TEXT = {
    "en": "Welcome back to the channel. Today we are going to look at something really interesting, so stay with me until the end.",
    "ja": "チャンネルへようこそ。今日はとても面白い話を紹介しますので、最後まで見てください。",
    "ko": "채널에 오신 것을 환영합니다. 오늘은 정말 흥미로운 이야기를 소개할 테니 끝까지 봐 주세요.",
    "zh-cn": "欢迎回到我的频道。今天我们要聊一个非常有趣的话题，请一定看到最后。",
    "de": "Willkommen zurück auf dem Kanal. Heute schauen wir uns etwas wirklich Spannendes an, also bleibt bis zum Ende dran.",
    "fr": "Bienvenue sur la chaîne. Aujourd'hui, nous allons découvrir quelque chose de vraiment passionnant, alors restez jusqu'à la fin.",
    "ru": "Добро пожаловать на канал. Сегодня мы посмотрим на кое-что действительно интересное, так что оставайтесь до конца.",
    "pt": "Bem-vindos de volta ao canal. Hoje vamos ver algo muito interessante, então fiquem comigo até o final.",
    "es": "Bienvenidos de nuevo al canal. Hoy vamos a ver algo realmente interesante, así que quédense hasta el final.",
    "it": "Bentornati sul canale. Oggi vedremo qualcosa di davvero interessante, quindi restate con me fino alla fine.",
}
#  Thông số khi SINH MẪU (đã đo: 0.6/20 cho mẫu gọn, ít lạc).
ANCHOR_SAMPLING = {"temperature": 0.6, "top_k": 20, "subtalker_temperature": 0.6, "subtalker_top_k": 20}
#  Mẫu phải có tốc độ đọc "bình thường": dài/ngắn quá so với mức đọc thoải mái của thứ
#  tiếng đó thì mẫu đó lạc -> thử seed khác (mẫu lạc là MỌI câu sau lạc theo).
ANCHOR_RATE_OK = (0.6, 1.8)
ANCHOR_TRIES = 4
IDLE_STOP_SEC = 300          # rảnh 5 phút -> tắt server, trả RAM/VRAM
HEALTH_TIMEOUT_SEC = 180     # nạp mô hình trên máy yếu có thể lâu

#  Hằng số MP3 dùng CHUNG với VieNeu — cả đường xử lý sau đó (atempo, trộn) coi mọi
#  câu như nhau.
MP3_BITRATE = "192k"
MP3_SAMPLE_RATE = "48000"

_OOM_HINTS = ("out of memory", "outofdevicememory", "erroroutofdevicememory",
              "cudamalloc", "failed to allocate", "vk_error_out_of", "not enough memory")


class QwenError(RuntimeError):
    """Lỗi Qwen3-TTS với lời nhắn tiếng Việt, hiện thẳng lên UI (như VieneuError)."""


# ---------------------------------------------------------------- đường dẫn
#  Tổ chức GIỐNG VieNeu: PHẦN CHẠY đi kèm bộ cài, MÔ HÌNH tải từ Hugging Face về
#  ``ai_models`` (Quản lý Model AI — xem services/qwen_models.py).
#    · bin/    tts-server (đổi tên subforge_qwen_tts.exe) + DLL ggml CPU/Vulkan —
#              build_backend.py chép vào ``<thư mục backend>/qwen_bin``.
#    · models/ GGUF — ``<ai_models>/qwen3_tts/models``.
#    · logs/   nhật ký máy chủ — cạnh models (thư mục cài có thể không ghi được).
#  Dev chưa đóng gói: dùng bản build/đo ở backend/tools/_qwentts.
#  Ghi đè cả ba: SUBFORGE_QWEN_DIR (test dùng) -> <dir>/bin, <dir>/models, <dir>/logs.
_DEV_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "tools", "_qwentts")


def _override_dir() -> str:
    return os.environ.get("SUBFORGE_QWEN_DIR", "").strip()


def _data_root() -> str:
    from utils.model_paths import get_models_dir

    return os.path.join(get_models_dir(), "qwen3_tts")


def qwen_home() -> str:
    """Thư mục DỮ LIỆU của Qwen (mô hình + nhật ký)."""
    return _override_dir() or _data_root()


def _bin_dir() -> str:
    o = _override_dir()
    if o:
        return os.path.join(o, "bin")
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "qwen_bin")
    return os.path.join(_DEV_ROOT, "bin")


def _models_dir() -> str:
    o = _override_dir()
    if o:
        return os.path.join(o, "models")
    chinh = os.path.join(_data_root(), "models")
    if getattr(sys, "frozen", False):
        return chinh
    #  Dev: chưa tải qua Quản lý Model AI thì dùng bản đã tải cho bench.
    try:
        from services.qwen_models import installed_pack

        if installed_pack(chinh) is None and installed_pack(os.path.join(_DEV_ROOT, "models")):
            return os.path.join(_DEV_ROOT, "models")
    except Exception:
        pass
    return chinh


def _logs_dir() -> str:
    return os.path.join(qwen_home(), "logs")


def _server_exe() -> str:
    """Bản đóng gói đổi tên thành ``subforge_qwen_tts.exe`` (để trình cài / Electron
    giết đúng tên); bản build dev giữ tên gốc ``tts-server.exe``."""
    for ten in ("subforge_qwen_tts.exe", "tts-server.exe", "tts-server"):
        p = os.path.join(_bin_dir(), ten)
        if os.path.isfile(p):
            return p
    return os.path.join(_bin_dir(), "subforge_qwen_tts.exe")


def model_files(size: str, quant: str, mode: str) -> tuple[str, str]:
    """(talker, codec) — tên tệp đúng như bản GGUF của qwentts.cpp."""
    return (os.path.join(_models_dir(), f"qwen-talker-{size}-{mode}-{quant}.gguf"),
            os.path.join(_models_dir(), f"qwen-tokenizer-12hz-{quant}.gguf"))


def _codec_for(size: str, quant: str, mode: str) -> tuple[str, str] | None:
    """Talker + codec có sẵn trên đĩa. Codec cùng họ lượng tử, thiếu thì mượn Q8_0
    (codec dùng chung mọi talker, bản Q8_0 là bản tải mặc định)."""
    talker, codec = model_files(size, quant, mode)
    if not os.path.isfile(talker):
        return None
    if not os.path.isfile(codec):
        codec = model_files(size, "Q8_0", mode)[1]
    return (talker, codec) if os.path.isfile(codec) else None


def _gpu_pack_torch_lib() -> str | None:
    """``torch/lib`` của gói GPU — chứa cudart64_12 / cublas64_12 mà ggml-cuda cần.
    Dùng lại thay vì bắt tải thêm ~900 MB DLL CUDA."""
    try:
        from utils.storage_paths import get_workstation_dir

        p = os.path.join(get_workstation_dir(), "gpu_pack", "subforge_backend_gpu",
                         "_internal", "torch", "lib")
        return p if os.path.isdir(p) else None
    except Exception:
        return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wav_seconds(data: bytes) -> float:
    import io

    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


#  CẮT LẶNG — đo thật (tools/e2e_qwen_app.py, 24-09-2026): có câu Qwen sinh ra 3,74s IM LẶNG
#  Ở ĐẦU + 2,4s lặng giữa câu (12,8s mà chỉ 5,2s có tiếng). Lồng tiếng đặt giọng đúng lúc
#  nhân vật mở miệng, nên lặng đầu = giọng vào TRỄ, lệch khung hình; lặng thừa còn bắt bước
#  tự khớp tăng tốc oan. Giữ một chút đệm đầu/cuối cho tự nhiên, khoảng lặng giữa câu quá
#  dài thì thu lại.
LANG_NGUONG_DB = -40.0        # dưới mức này (so với khung to nhất) coi là lặng
LANG_DEM_DAU = 0.05           # giây giữ lại trước tiếng đầu tiên
LANG_DEM_CUOI = 0.15          # giây giữ lại sau tiếng cuối
LANG_GIUA_TOI_DA = 0.7        # khoảng lặng giữa câu dài hơn thế thì ...
LANG_GIUA_CON = 0.35          # ... thu lại còn chừng này
QWEN_KHUNG_MOI_GIAY = 12.5    # mã hoá âm 12Hz: 58 khung = 4,64s (đo log máy chủ)


def _gon_lang(data: bytes) -> bytes:
    """Cắt lặng đầu/cuối + thu khoảng lặng dài giữa câu của một WAV PCM16 mono. Lỗi hoặc
    toàn lặng -> trả nguyên."""
    import io

    try:
        import numpy as np

        with wave.open(io.BytesIO(data), "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2 or ch != 1 or sr <= 0:
            return data
        x = np.frombuffer(raw, dtype=np.int16)
        win = max(1, int(sr * 0.02))
        n = len(x) // win
        if n < 3:
            return data
        rms = np.sqrt(np.mean(x[: n * win].astype(np.float64).reshape(n, win) ** 2, axis=1))
        if rms.max() <= 0:
            return data
        co_tieng = 20 * np.log10(np.maximum(rms, 1e-9) / rms.max()) > LANG_NGUONG_DB
        idx = np.where(co_tieng)[0]
        if idx.size == 0:
            return data
        dau = max(0, idx[0] * win - int(sr * LANG_DEM_DAU))
        cuoi = min(len(x), (idx[-1] + 1) * win + int(sr * LANG_DEM_CUOI))
        #  Thu khoảng lặng giữa câu: giữ các đoạn có tiếng, lặng > LANG_GIUA_TOI_DA -> LANG_GIUA_CON.
        manh, prev_end = [], None
        gioi_han, con = int(LANG_GIUA_TOI_DA / 0.02), int(LANG_GIUA_CON * sr)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and idx[j + 1] - idx[j] <= gioi_han:
                j += 1
            s0 = max(dau, idx[i] * win) if prev_end is not None else dau
            e0 = min(cuoi, (idx[j] + 1) * win) if j + 1 < len(idx) else cuoi
            if prev_end is not None:
                manh.append(np.zeros(con, dtype=np.int16))
            manh.append(x[s0:e0])
            prev_end = e0
            i = j + 1
        y = np.concatenate(manh) if manh else x[dau:cuoi]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(y.astype(np.int16).tobytes())
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — cắt lặng hỏng thì dùng nguyên bản
        return data


def _seed_of(text: str, voice: str, bump: int = 0) -> int:
    """Seed theo NỘI DUNG: cùng câu + cùng giọng -> cùng kết quả (sinh lại không đổi
    giọng một cách ngẫu nhiên), khác câu thì khác seed cho tự nhiên."""
    h = hashlib.md5(f"{text}\x1f{voice}".encode("utf-8")).hexdigest()
    return (int(h[:8], 16) + bump) % 2_147_483_647


def _cps(lang: str) -> float:
    try:
        from services.deepseek_service import CPS_BY_LANG

        return float(CPS_BY_LANG.get(lang) or CPS_BY_LANG.get(lang.split("-")[0]) or 14)
    except Exception:
        return 14.0


# -------------------------------------------------------------- tiến trình
class _Server:
    """Một tiến trình tts-server + cổng + nhật ký riêng."""

    def __init__(self, cmd_prefix: list[str], talker: str, codec: str, tier: dict,
                 log_path: str):
        self.tier = tier
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        self.registered: set[str] = set()
        #  Báo cho luồng canh "rảnh" của RIÊNG server này thoát ngay khi server dừng.
        self.closed = threading.Event()
        env = dict(os.environ)
        env["GGML_BACKEND"] = tier["backend"]
        if tier.get("threads"):
            env["QWEN_TTS_N_THREADS"] = str(tier["threads"])
        if tier["backend"].upper().startswith("CUDA"):
            lib = _gpu_pack_torch_lib()
            if lib:
                env["PATH"] = lib + os.pathsep + env.get("PATH", "")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._log = open(log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [*cmd_prefix, "--model", talker, "--codec", codec, "--port", str(self.port)],
            stdout=self._log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, cwd=_bin_dir() if os.path.isdir(_bin_dir()) else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        from utils.win_job import attach_kill_on_close

        attach_kill_on_close(self.proc.pid)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def log_tail(self, n: int = 1200) -> str:
        try:
            self._log.flush()
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return f.read()[-n:]
        except OSError:
            return ""

    def wait_ready(self, timeout: float = HEALTH_TIMEOUT_SEC) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if not self.alive():
                raise QwenError(L(f"Máy chủ Qwen3-TTS thoát khi đang nạp mô hình:\n{self.log_tail()}", f"The Qwen3-TTS server exited while loading the model:\n{self.log_tail()}"))
            try:
                code, _ = self.call("/health", None, timeout=3)
                if code == 200:
                    return
            except OSError:
                pass
            time.sleep(0.3)
        raise QwenError(L("Máy chủ Qwen3-TTS không sẵn sàng sau "
                        f"{int(timeout)} giây — máy có thể quá yếu cho mức {self.tier['name']}.", f"The Qwen3-TTS server was not ready after {int(timeout)} seconds — the machine may be too weak for the {self.tier['name']} tier."))

    def backend_in_use(self) -> str:
        """Backend THẬT server đang dùng (đọc từ nhật ký) — ép Vulkan0 mà máy không có
        thiết bị thì ggml báo lỗi, nên dòng này là bằng chứng mức đã chọn chạy được."""
        for line in self.log_tail(20000).splitlines():
            if "Talker backend:" in line:
                return line.split("Talker backend:", 1)[1].split("(")[0].strip()
        return ""

    def call(self, path: str, body: dict | None, timeout: float) -> tuple[int, bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data,
                                     method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def stop(self) -> None:
        self.closed.set()
        try:
            if self.alive():
                self.proc.terminate()
                self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self._log.close()
        except Exception:
            pass


# ----------------------------------------------------------------- dịch vụ
class QwenTTSService:
    _lock = threading.RLock()        # server xử lý FIFO; khoá giữ thứ tự + đổi mode an toàn
    _server: _Server | None = None
    _server_mode: str | None = None
    _bad_tiers: set[str] = set()     # mức đã hỏng (hết VRAM / không có thiết bị) trong phiên
    _last_used = 0.0
    _rtf: list[float] = []           # RTF đo được của các câu gần đây (hiện lên UI)

    #  Tiền tố lệnh chạy server — test thay bằng [python, fake_server.py].
    @classmethod
    def _cmd_prefix(cls) -> list[str]:
        return [_server_exe()]

    # ------------------------------------------------------------ trạng thái
    @classmethod
    def backends_present(cls) -> tuple[bool, bool]:
        """(có ggml-cuda, có ggml-vulkan) trong bin/ — quyết định mức nào được thử."""
        b = _bin_dir()
        return (any(os.path.isfile(os.path.join(b, f)) for f in ("ggml-cuda.dll", "libggml-cuda.so")),
                any(os.path.isfile(os.path.join(b, f)) for f in ("ggml-vulkan.dll", "libggml-vulkan.so")))

    @classmethod
    def is_installed(cls, mode: str | None = None) -> bool:
        if not os.path.isfile(_server_exe()):
            return False
        modes = [mode] if mode else [MODE_PRESET, MODE_CLONE]
        return any(_codec_for("0.6b", q, m) for m in modes for q in ("Q8_0", "Q4_K_M"))

    @classmethod
    def status(cls) -> dict:
        with cls._lock:
            s = cls._server
            return {
                "installed": cls.is_installed(),
                "running": bool(s and s.alive()),
                "mode": cls._server_mode,
                "tier": (s.tier if s else None),
                "backend": (s.backend_in_use() if s else ""),
                "rtf": round(sum(cls._rtf) / len(cls._rtf), 2) if cls._rtf else None,
                "bad_tiers": sorted(cls._bad_tiers),
            }

    @classmethod
    def _tiers(cls) -> list[dict]:
        from utils.hardware_utils import get_qwen_tts_tiers

        cuda, vulkan = cls.backends_present()
        prefer = os.environ.get("SUBFORGE_QWEN_DEVICE", "auto").strip().lower() or "auto"
        return [t for t in get_qwen_tts_tiers(cuda, vulkan, prefer)
                if t["name"] not in cls._bad_tiers]

    # ------------------------------------------------------------ vòng đời
    @classmethod
    def stop(cls) -> None:
        """Tắt server, trả RAM/VRAM. Gọi khi rảnh lâu, trước Demucs/STT trên GPU, khi
        backend tắt (``_release_all_gpu_models``)."""
        with cls._lock:
            if cls._server is not None:
                logger.info(L("Qwen3-TTS: tắt máy chủ, trả bộ nhớ.", "Qwen3-TTS: shutting down the server, freeing memory."))
                cls._server.stop()
            cls._server = None
            cls._server_mode = None

    @classmethod
    def release_for_other_model(cls, reason: str = "") -> None:
        """Nhường RAM/VRAM cho mô hình nặng khác (Demucs, Whisper) sắp nạp.

        KHÔNG CHỜ KHOÁ: Qwen đang sinh giọng mà đứng chờ ở đây thì dễ khoá chéo (Qwen
        nhả Whisper, Whisper lúc nạp lại nhả Qwen). Đang bận -> bỏ qua; server vẫn tự
        tắt sau IDLE_STOP_SEC. Chưa bật -> không tốn gì."""
        if cls._server is None:
            return
        if not cls._lock.acquire(blocking=False):
            return
        try:
            if cls._server is not None:
                logger.info(L(f"Qwen3-TTS: nhường bộ nhớ{' cho ' + reason if reason else ''}.", f"Qwen3-TTS: yielding memory{' to ' + reason if reason else ''}."))
                cls.stop()
        finally:
            cls._lock.release()

    @classmethod
    def _idle_watch(cls, srv: "_Server") -> None:
        """Canh MỘT server: rảnh quá IDLE_STOP_SEC thì tắt. Mỗi server một luồng canh
        riêng, thoát ngay khi server đó dừng (``srv.closed``) — luồng cũ đang ngủ không
        bao giờ canh nhầm server mới bật sau nó."""
        while not srv.closed.wait(min(30.0, max(0.2, IDLE_STOP_SEC / 5))):
            with cls._lock:
                if cls._server is not srv:
                    return
                if time.monotonic() - cls._last_used > IDLE_STOP_SEC:
                    cls.stop()
                    return

    @classmethod
    def _release_gpu_neighbours(cls, backend: str) -> None:
        """Chạy trên GPU thì nhả Whisper trước (mẫu của VieNeu ở video_routes) — hai
        mô hình cùng giành VRAM của card 4 GB là OOM giữa chừng."""
        if backend.upper() == "CPU":
            return
        try:
            from services.stt_service import get_stt_service

            get_stt_service().release_gpu(sync_timeout_sec=3.0)
        except Exception as e:
            logger.debug(L(f"Qwen3-TTS: nhả Whisper lỗi (bỏ qua): {e}", f"Qwen3-TTS: releasing Whisper failed (ignored): {e}"))

    @classmethod
    def _ensure_server(cls, mode: str) -> _Server:
        """Server đang chạy đúng ``mode``; chưa có / sai mode / đã chết -> bật mới,
        thử lần lượt các mức, mức nào hỏng thì ghi vào ``_bad_tiers`` và lùi."""
        s = cls._server
        if s is not None and s.alive() and cls._server_mode == mode:
            return s
        cls.stop()
        loi_cuoi = ""
        for tier in cls._tiers():
            files = _codec_for(tier["size"], tier["quant"], mode)
            if not files:
                continue   # chưa tải bản mô hình này -> thử mức sau
            cls._release_gpu_neighbours(tier["backend"])
            log_path = os.path.join(_logs_dir(), f"server-{mode}-{tier['name']}.log")
            logger.info(L(f"Qwen3-TTS: bật máy chủ {mode} ở mức {tier['name']} "
                        f"({tier['backend']}, {tier['size']} {tier['quant']})...", f"Qwen3-TTS: starting the {mode} server at tier {tier['name']} ({tier['backend']}, {tier['size']} {tier['quant']})..."))
            srv = None
            try:
                srv = _Server(cls._cmd_prefix(), files[0], files[1], tier, log_path)
                srv.wait_ready()
                that = srv.backend_in_use()
                if that and tier["backend"].upper() != "CPU" and that.upper() == "CPU":
                    raise QwenError(L(f"Không dùng được {tier['backend']} (ggml lùi về CPU).", f"{tier['backend']} cannot be used (ggml fell back to CPU)."))
            except Exception as e:  # noqa: BLE001 — mức này hỏng, lùi mức sau
                tail = srv.log_tail() if srv else ""
                if srv:
                    srv.stop()
                loi_cuoi = f"{e}"
                #  Chỉ GHI NHỚ mức GPU hỏng (hết VRAM / không có thiết bị — lặp lại y hệt).
                #  Mức CPU hỏng thường là nhất thời (nạp lâu, tệp đang bị khoá): ghi nhớ
                #  là Qwen chết tới lúc mở lại app.
                if tier["backend"].upper() != "CPU":
                    cls._bad_tiers.add(tier["name"])
                la_oom = any(h in (tail + str(e)).lower() for h in _OOM_HINTS)
                logger.warning(L(f"Qwen3-TTS: mức {tier['name']} không chạy được"
                               f"{' (hết bộ nhớ)' if la_oom else ''} — lùi mức sau. {str(e)[:200]}", f"Qwen3-TTS: tier {tier['name']} cannot run{' (out of memory)' if la_oom else ''} — falling back to the next tier. {str(e)[:200]}"))
                continue
            cls._server, cls._server_mode = srv, mode
            cls._last_used = time.monotonic()
            threading.Thread(target=cls._idle_watch, args=(srv,), daemon=True).start()
            logger.info(L(f"Qwen3-TTS: sẵn sàng ({srv.backend_in_use() or tier['backend']}).", f"Qwen3-TTS: ready ({srv.backend_in_use() or tier['backend']})."))
            return srv
        if not cls.is_installed(mode):
            raise QwenError(L("Chưa cài mô hình Qwen3-TTS — mở 'Quản lý Model AI' để tải.", "The Qwen3-TTS model is not installed — open 'AI Model Manager' to download it."))
        raise QwenError(L(f"Không bật được Qwen3-TTS ở mức nào trên máy này. Lỗi cuối: {loi_cuoi}", f"Could not start Qwen3-TTS at any tier on this machine. Last error: {loi_cuoi}"))

    # ------------------------------------------------------------ sinh giọng
    @classmethod
    def synthesize(cls, text: str, voice_id: str, output_mp3_path: str, lan_sinh: int = 0) -> bool:
        """Sinh MỘT câu ra MP3. True nếu xong; lỗi -> QwenError (tiếng Việt). ``lan_sinh`` > 0 = "làm lại giọng": seed lệch đi. Seed vốn theo NỘI DUNG (cùng câu + cùng giọng -> cùng kết quả) nên
        sinh lại KHÔNG sửa được một câu đã đọc ra rác (đo 05-10-2026: giọng aiden đọc tiếng Nhật ra âm vô nghĩa, sinh lại y hệt) — Sáng tạo tăng ``lan_sinh`` khi người dùng làm lại một cảnh."""
        parts = parse_qwen_voice(voice_id)
        if not parts:
            raise QwenError(L(f"Mã giọng Qwen không hợp lệ: '{voice_id}'", f"Invalid Qwen voice ID: '{voice_id}'"))
        kind, lang, name = parts
        text = (text or "").strip()
        if not text:
            return False
        mode = MODE_PRESET if kind == "preset" else MODE_CLONE
        if kind == "preset" and name not in QWEN_SPEAKERS:
            raise QwenError(L(f"Qwen3-TTS không có giọng '{name}'.", f"Qwen3-TTS has no voice '{name}'."))

        with cls._lock:
            anchor = cls._anchor_for(name, lang) if kind == "preset" else None
            if anchor:
                mode = MODE_CLONE             # đọc bằng bản Base, bám mẫu neo
            for lan_thu in range(2):          # server chết giữa chừng -> bật lại, thử 1 lần
                srv = cls._ensure_server(mode)
                try:
                    if anchor:
                        wav = cls._speak_checked(srv, text, voice_id, "anchor", lang, name,
                                                 voice=cls._ensure_anchor_registered(srv, anchor), lan_sinh=lan_sinh)
                    else:
                        wav = cls._speak_checked(srv, text, voice_id, kind, lang, name, lan_sinh=lan_sinh)
                    break
                except (OSError, ConnectionError) as e:
                    #  Kết nối bị cắt thường tới TRƯỚC khi tiến trình kịp thoát hẳn: hỏi
                    #  ``alive()`` ngay lúc đó vẫn ra True -> không bật lại mà báo lỗi luôn
                    #  (test sập-giữa-chừng trượt 2/3 lần). Cho nó chút thời gian để chết hẳn.
                    if srv.alive() and not lan_thu:
                        try:
                            srv.proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
                    if srv.alive() or lan_thu:
                        raise QwenError(L(f"Qwen3-TTS không trả lời: {e}", f"Qwen3-TTS did not respond: {e}")) from e
                    logger.warning(L("Qwen3-TTS: máy chủ đã dừng giữa chừng — bật lại và thử lại.", "Qwen3-TTS: the server stopped midway — start it again and retry."))
                    cls.stop()
            cls._last_used = time.monotonic()
        cls._wav_to_mp3(wav, output_mp3_path)
        return True

    @classmethod
    def _speak_checked(cls, srv: _Server, text: str, voice_id: str, kind: str,
                       lang: str, name: str, voice: str | None = None, lan_sinh: int = 0) -> bytes:
        """``kind`` = preset | clone | anchor (giọng có sẵn đọc qua mẫu neo — ``voice``
        là khoá mẫu đã đăng ký)."""
        if voice is None:
            voice = name if kind == "preset" else cls._ensure_clone(srv, name, lang)
        #  Clone ICL (khoá đuôi "i", xem ``_ensure_clone``) = cùng cơ chế với mẫu neo.
        nhom = "anchor" if kind == "anchor" or (kind == "clone" and voice.endswith("i")) else kind
        expect = len(text) / _cps(lang)
        tran = max(3.0, expect * LENGTH_GUARD_FACTOR)
        for bump in range(2):                 # quá dài -> đổi seed, sinh lại MỘT lần
            body = {"input": text, "voice": voice, "language": QWEN_LANG_LABEL[lang],
                    "response_format": "wav", "seed": _seed_of(text, voice_id, bump + 1000 * max(0, int(lan_sinh))),
                    #  Trần số khung theo trần chống lặp (+10%): câu bị lặp dừng ở ~trần
                    #  thay vì chạy tới 2048 khung mặc định (~164s) — đo thật: một câu lặp
                    #  mất 405s mới sinh lại được.
                    "max_new_tokens": int(tran * QWEN_KHUNG_MOI_GIAY * 1.1) + 8,
                    **QWEN_SAMPLING.get(nhom, {})}
            t0 = time.monotonic()
            code, data = srv.call("/v1/audio/speech", body, timeout=120 + len(text) * 4)
            dt = time.monotonic() - t0
            if code >= 300:
                raise QwenError(L(f"Qwen3-TTS báo lỗi {code}: {data[:300].decode('utf-8', 'replace')}", f"Qwen3-TTS reported error {code}: {data[:300].decode('utf-8', 'replace')}"))
            dur_tho = _wav_seconds(data)
            if dur_tho > 0:
                cls._rtf = (cls._rtf + [dt / dur_tho])[-20:]
            data = _gon_lang(data)
            dur = _wav_seconds(data)
            if 0.2 < dur <= tran:
                return data
            logger.warning(L(f"Qwen3-TTS: câu dài bất thường ({dur:.1f}s, trần {tran:.1f}s) — "
                           f"{'sinh lại với seed khác' if not bump else 'bỏ'}: '{text[:30]}…'", f"Qwen3-TTS: abnormally long sentence ({dur:.1f}s, ceiling {tran:.1f}s) — {'regenerating with another seed' if not bump else 'dropping'}: '{text[:30]}…'"))
        raise QwenError(L("Qwen3-TTS sinh ra audio dài/ngắn bất thường cho câu này (có thể "
                        "bị lặp). Hãy sửa câu hoặc đổi giọng rồi thử lại.", "Qwen3-TTS produced abnormally long/short audio for this sentence (it may be repeating). Edit the sentence or change the voice and try again."))

    @classmethod
    def _wav_to_mp3(cls, wav: bytes, output_mp3_path: str) -> None:
        from services.vieneu_service import FFMPEG_EXE

        os.makedirs(os.path.dirname(output_mp3_path) or ".", exist_ok=True)
        tmp_wav = output_mp3_path + ".qwen.wav"
        tmp_mp3 = output_mp3_path + ".qwen.mp3"
        try:
            with open(tmp_wav, "wb") as f:
                f.write(wav)
            res = subprocess.run(
                [FFMPEG_EXE, "-y", "-i", tmp_wav, "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE,
                 "-ar", MP3_SAMPLE_RATE, "-q:a", "2", tmp_mp3],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode != 0 or not os.path.exists(tmp_mp3) or os.path.getsize(tmp_mp3) == 0:
                raise QwenError(L(f"Không mã hoá được audio Qwen sang MP3: {res.stderr[-300:]}", f"Could not encode Qwen audio to MP3: {res.stderr[-300:]}"))
            safe_replace(tmp_mp3, output_mp3_path)
        finally:
            for p in (tmp_wav, tmp_mp3):
                safe_remove(p)

    # --------------------------------------------------------------- clone
    @classmethod
    def voices_dir(cls) -> str:
        d = os.path.join(get_app_data_dir(), "voices", "qwen")
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def _registry_path(cls) -> str:
        return os.path.join(cls.voices_dir(), "registry.json")

    @classmethod
    def _load_registry(cls) -> dict:
        try:
            with open(cls._registry_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d.get("clones"), dict) else {"clones": {}}
        except (OSError, ValueError):
            return {"clones": {}}

    @classmethod
    def _save_registry(cls, reg: dict) -> None:
        tmp = cls._registry_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
        safe_replace(tmp, cls._registry_path())

    @classmethod
    def list_clones(cls) -> list[dict]:
        reg = cls._load_registry()["clones"]
        return [{"name": k, "ref_lang": v.get("ref_lang"), "has_ref_text": bool(v.get("ref_text")),
                 "builtin": bool(v.get("builtin"))} for k, v in sorted(reg.items())]

    @classmethod
    def add_clone(cls, name: str, src_audio: str, ref_text: str = "", ref_lang: str = "") -> str:
        """Lưu mẫu giọng (chuẩn hoá WAV 24 kHz mono, tối đa 10 s). Trả tên đã làm sạch.

        ``ref_text`` = lời trong mẫu (bật chế độ clone ICL, giống giọng hơn khi đọc CÙNG
        thứ tiếng với mẫu). Không có thì chỉ dùng x-vector (nhận dạng giọng) — đo thật
        thì cách này đọc CHÉO tiếng tốt (CER 0), nên luôn là đường dự phòng."""
        from services.vieneu_service import FFMPEG_EXE, _safe_name

        ten = _safe_name(name)
        dst = os.path.join(cls.voices_dir(), f"{ten}.wav")
        tmp = dst + ".tmp.wav"
        res = subprocess.run(
            [FFMPEG_EXE, "-y", "-i", src_audio, "-t", "10", "-ac", "1", "-ar", "24000",
             "-c:a", "pcm_s16le", tmp],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if res.returncode != 0 or not os.path.exists(tmp):
            safe_remove(tmp)
            raise QwenError(L(f"Không đọc được tệp mẫu giọng: {res.stderr[-300:]}", f"Could not read the voice sample file: {res.stderr[-300:]}"))
        safe_replace(tmp, dst)
        with open(dst, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        with cls._lock:
            reg = cls._load_registry()
            reg["clones"][ten] = {"file": os.path.basename(dst), "sha": sha,
                                  "ref_text": (ref_text or "").strip(),
                                  "ref_lang": normalize_lang(ref_lang) if ref_lang else "",
                                  "builtin": False}
            cls._save_registry(reg)
            if cls._server is not None:   # mẫu đổi -> bản đăng ký cũ trên server hết đúng
                cls._server.registered = {k for k in cls._server.registered
                                          if not k.startswith(cls._reg_prefix(ten))}
        return ten

    @classmethod
    def delete_clone(cls, name: str) -> bool:
        with cls._lock:
            reg = cls._load_registry()
            info = reg["clones"].pop(name, None)
            if info is None or info.get("builtin"):
                return False
            cls._save_registry(reg)
        safe_remove(os.path.join(cls.voices_dir(), info.get("file", "")))
        return True

    @classmethod
    def clone_sha(cls, name: str) -> str:
        """Vân tay mẫu giọng — đi vào khoá lưu đệm: đổi mẫu thì câu cũ phải sinh lại."""
        return (cls._load_registry()["clones"].get(name) or {}).get("sha", "")[:12]

    @staticmethod
    def _reg_prefix(name: str) -> str:
        return "c" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10]

    @classmethod
    def _ensure_clone(cls, srv: _Server, name: str, lang: str) -> str:
        """Đăng ký giọng clone lên server (lười: chỉ khi cần; server khởi động lại thì
        đăng ký lại). Đọc CÙNG tiếng với mẫu + có lời mẫu -> ICL; còn lại -> x-vector."""
        info = cls._load_registry()["clones"].get(name)
        if not info:
            raise QwenError(L(f"Không tìm thấy giọng clone '{name}'.", f"Clone voice '{name}' not found."))
        icl = bool(info.get("ref_text")) and (not info.get("ref_lang") or info["ref_lang"] == lang)
        key = f"{cls._reg_prefix(name)}{info.get('sha', '')[:6]}{'i' if icl else 'x'}"
        if key in srv.registered:
            return key
        with open(os.path.join(cls.voices_dir(), info["file"]), "rb") as f:
            body = {"name": key, "wav_b64": base64.b64encode(f.read()).decode()}
        if icl:
            body["ref_text"] = info["ref_text"]
        code, data = srv.call("/v1/audio/voices", body, timeout=120)
        if code >= 300:
            raise QwenError(L(f"Không nạp được giọng clone '{name}': "
                            f"{data[:200].decode('utf-8', 'replace')}", f"Could not load the clone voice '{name}': {data[:200].decode('utf-8', 'replace')}"))
        srv.registered.add(key)
        return key

    # ------------------------------------------------------- mẫu neo giọng
    @classmethod
    def anchors_dir(cls) -> str:
        d = os.path.join(cls.voices_dir(), "anchors")
        os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def _anchor_for(cls, speaker: str, lang: str) -> dict | None:
        """Mẫu neo của cặp (giọng có sẵn, thứ tiếng): có rồi thì dùng; chưa có thì sinh
        bằng bản CustomVoice rồi LƯU (các lần sau, kể cả mở lại app, dùng lại đúng mẫu
        đó). None = tắt neo / không sinh được mẫu / chưa tải bản Base -> đọc thẳng như
        cũ. Gọi khi đang giữ ``_lock``."""
        if (not QWEN_PRESET_ANCHOR or lang not in QWEN_ANCHOR_LANGS or lang not in ANCHOR_TEXT
                or not cls.is_installed(MODE_CLONE)):
            return None
        wav_path = os.path.join(cls.anchors_dir(), f"{speaker}_{lang}.wav")
        meta_path = wav_path[:-4] + ".json"
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            if os.path.isfile(wav_path) and meta.get("text"):
                return {**meta, "wav": wav_path}
        except (OSError, ValueError):
            pass
        text = ANCHOR_TEXT[lang]
        expect = len(text) / _cps(lang)
        try:
            srv = cls._ensure_server(MODE_PRESET)
            for lan in range(ANCHOR_TRIES):
                seed = _seed_of(f"anchor|{lang}", speaker, lan)
                body = {"input": text, "voice": speaker, "language": QWEN_LANG_LABEL[lang],
                        "response_format": "wav", "seed": seed, **ANCHOR_SAMPLING}
                code, data = srv.call("/v1/audio/speech", body, timeout=120 + len(text) * 4)
                if code >= 300:
                    raise QwenError(f"{code}: {data[:200].decode('utf-8', 'replace')}")
                data = _gon_lang(data)
                dur = _wav_seconds(data)
                if ANCHOR_RATE_OK[0] * expect <= dur <= ANCHOR_RATE_OK[1] * expect:
                    tmp = wav_path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    safe_replace(tmp, wav_path)
                    meta = {"text": text, "lang": lang, "speaker": speaker, "seed": seed,
                            "sha": hashlib.sha256(data).hexdigest()[:12]}
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False)
                    logger.info(L(f"Qwen3-TTS: đã tạo mẫu neo {speaker}/{lang} ({dur:.1f}s, lần {lan + 1}).", f"Qwen3-TTS: created anchor sample {speaker}/{lang} ({dur:.1f}s, attempt {lan + 1})."))
                    return {**meta, "wav": wav_path}
                logger.info(L(f"Qwen3-TTS: mẫu neo {speaker}/{lang} lạc tốc độ ({dur:.1f}s, "
                            f"chuẩn ~{expect:.1f}s) — thử seed khác.", f"Qwen3-TTS: anchor sample {speaker}/{lang} drifted in speed ({dur:.1f}s, expected ~{expect:.1f}s) — trying another seed."))
        except Exception as e:  # noqa: BLE001 — neo hỏng không được làm chết việc đọc
            logger.warning(L(f"Qwen3-TTS: không tạo được mẫu neo {speaker}/{lang} ({e}) — đọc thẳng.", f"Qwen3-TTS: could not create anchor sample {speaker}/{lang} ({e}) — reading directly."))
            return None
        logger.warning(L(f"Qwen3-TTS: {ANCHOR_TRIES} mẫu neo {speaker}/{lang} đều lạc — đọc thẳng.", f"Qwen3-TTS: all {ANCHOR_TRIES} anchor samples {speaker}/{lang} drifted — reading directly."))
        return None

    @classmethod
    def _ensure_anchor_registered(cls, srv: _Server, anchor: dict) -> str:
        key = f"a{anchor['speaker']}_{anchor['lang']}_{anchor['sha'][:6]}"
        if key in srv.registered:
            return key
        with open(anchor["wav"], "rb") as f:
            body = {"name": key, "wav_b64": base64.b64encode(f.read()).decode(),
                    "ref_text": anchor["text"]}
        code, data = srv.call("/v1/audio/voices", body, timeout=120)
        if code >= 300:
            raise QwenError(L(f"Không nạp được mẫu neo {anchor['speaker']}: "
                            f"{data[:200].decode('utf-8', 'replace')}", f"Could not load the anchor sample {anchor['speaker']}: {data[:200].decode('utf-8', 'replace')}"))
        srv.registered.add(key)
        return key

    # ----------------------------------------------------------- danh sách
    @classmethod
    def list_voices(cls, lang: str) -> dict:
        """Giọng Qwen đọc được ``lang``: giọng có sẵn (bản xứ lên đầu) + clone."""
        lg = normalize_lang(lang)
        if lg not in QWEN_LANG_LABEL:
            return {"presets": [], "clones": []}
        sp = sorted(QWEN_SPEAKERS.items(), key=lambda kv: kv[1]["native"] != lg)
        presets = [{"id": f"qwen:{lg}:{k}", "label": v["label"], "native": v["native"] == lg}
                   for k, v in sp]
        clones = [{"id": f"qwen-clone:{lg}:{c['name']}", "label": c["name"],
                   "builtin": c["builtin"]} for c in cls.list_clones()]
        return {"presets": presets, "clones": clones}
