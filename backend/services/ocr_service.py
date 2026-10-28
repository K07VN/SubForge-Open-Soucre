"""OCR phụ đề cứng (hardcoded sub) — lấy phụ đề ĐÃ CHÁY sẵn trên hình.

Khác hoàn toàn STT (nhận dạng giọng nói): ở đây ta ĐỌC CHỮ trên khung hình.
Mục tiêu: video vài tiếng chạy trong vài phút. Bí quyết KHÔNG phải OCR nhanh từng
frame, mà là OCR CÀNG ÍT frame càng tốt — nhờ 3 tầng lọc:

  1. Chỉ đọc VÙNG phụ đề do người dùng khoanh (toạ độ %), bỏ qua phần còn lại.
  2. Lấy mẫu THƯA (vài fps) + KHỬ TRÙNG LẶP: một câu phụ đề tồn tại vài giây nên
     hầu hết frame giống nhau -> gộp thành 1 "khoảng ổn định" = 1 câu (kèm start/end)
     -> giảm số lần OCR 10-50 lần.
  3. OCR MỘT LẦN cho mỗi khoảng ổn định, chạy song song nhiều luồng.

Engine: RapidOCR (ONNXRuntime). Model mặc định nhận CẢ tiếng Trung lẫn tiếng Anh.
Ưu tiên GPU (CUDA) nếu có, tự lùi CPU khi thiếu/again OOM — cùng tinh thần VieNeu/STT.
"""

from utils.i18n import L
from utils.hardware_utils import get_best_device
import difflib
import logging
import os
import re
import subprocess
import threading

import imageio_ffmpeg
import numpy as np

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger("OCRService")


def _fmt_ts(seconds: float) -> str:
    """m:ss hoặc h:mm:ss cho log dễ đọc (giống STTService)."""
    total = int(round(seconds or 0))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---- Tham số tốc độ/chất lượng (điều chỉnh được) --------------------------------
# fps lấy mẫu: phụ đề thường tồn tại >=1s nên 3fps là đủ bắt, ít frame để quét.
DEFAULT_SAMPLE_FPS = 3.0
# Chế độ "Chính xác" (Cài đặt → chất lượng lấy phụ đề): lấy mẫu dày hơn và ĐỌC MỌI khung có
# chữ thay vì đoán lúc đổi câu (``_scan_window_day``).
ACCURATE_SAMPLE_FPS = 6.0
# Hai mẫu liên tiếp có vân tay (trên ô có chữ) lệch dưới chừng này thì DÙNG LẠI chữ đã đọc —
# phụ đề đứng yên nên phần lớn mẫu giống hệt nhau. Đổi câu thật lệch 0,056–0,157 (đo).
ACCURATE_REUSE_DIFF = 0.01
# fps DÒ BIÊN: ffmpeg đưa ra khung ở nhịp này; chỉ 1/(REFINE_FPS/SAMPLE_FPS) khung được
# phân tích đầy đủ, số còn lại nằm trong BỘ ĐỆM VÒNG — thấy đổi câu thì dò ngược trong đó
# tìm ĐÚNG khung đầu tiên đổi. Biên chính xác tới 1/30 s thay vì đoán ở giữa hai mẫu
# (lệch sớm có hệ thống ~0,17 s — đo 24-09-2026). Giải mã tốn như cũ (ffmpeg vốn giải mã
# mọi khung), chỉ thêm băng thông ống dẫn cho vùng chữ đã cắt.
REFINE_FPS = 30.0
# Nhận diện "mực chữ": phụ đề thường SÁNG (trắng/vàng) tương phản cao. Lấy pixel
# sáng >= ngưỡng này làm mặt nạ chữ -> so sánh & phát hiện trống trên MẶT NẠ (bất
# biến với chuyển động/đổi màu của NỀN video dưới phụ đề — thủ phạm gây tách câu giả).
TEXT_BRIGHT_THRESH = 200
# Ngưỡng pixel TỐI (viền chữ). Chữ phụ đề = sáng CÓ VIỀN TỐI kề bên -> phân biệt với
# nền sáng TRƠN (trời/tuyết/đèn) vốn không có viền tối -> tránh câu "dính" khi nền sáng.
DARK_OUTLINE_THRESH = 90
# Tỉ lệ pixel "mực" tối thiểu để coi là CÓ phụ đề. Dưới ngưỡng -> khung TRỐNG,
# BỎ QUA (không OCR) cho nhanh. Để THẤP (0.4%) để KHÔNG bỏ sót chữ mảnh/nhỏ/mờ
# (vd câu đầu ~0:00, hoặc vùng khoanh rộng làm loãng tỉ lệ). Khung nền-không-chữ mà
# lọt qua ngưỡng vẫn được OCR trả rỗng -> tự loại, chỉ tốn thêm chút thời gian.
BLANK_INK_MIN = 0.0015
# Ngưỡng "có chữ" thứ hai — mật độ mực ở DẢI 16 HÀNG đậm nhất (``_la_trong``). Tỉ lệ trên cả
# vùng (BLANK_INK_MIN) bị PHA LOÃNG theo bề cao vùng khoanh: chữ mảnh/mờ trên vùng cao
# 120–160px chỉ đạt 0,0011–0,0013 -> bị coi TRỐNG, cả câu biến mất (rà soát 29-09-2026:
# video-thu-2 mất 10/14 khung có chữ). Dải 16 hàng đo đúng chỗ có dòng chữ nên không phụ
# thuộc bề cao vùng. Đo trên 8 video thật: 0,003 cứu 17/18 khung có chữ bị mất.
BAND_INK_MIN = 0.003
BAND_ROWS = 16
# Ngưỡng "đổi câu": mean-abs-diff của vân tay mặt nạ (thang 0..1). Thấp -> NHẠY hơn
# (bắt được đổi câu giữa 2 dòng thoại DÀI GẦN BẰNG NHAU cùng vị trí — nếu cao sẽ
# nuốt mất dòng sau, gây câu "dính" 20s). 0.02: bắt đủ thoại Anh; Chinese gần như
# không đổi (câu trùng do tách dư sẽ được gộp lại theo nội dung).
DEFAULT_CHANGE_THRESH = 0.02
# Ngưỡng "đổi câu" thứ hai — CHỈ tính trên các ô CÓ CHỮ của vân tay (``_doi_cau``). Mean
# trên cả lưới bị PHA LOÃNG theo bề cao vùng khoanh: vùng cao 398px mà dòng chữ ~25px
# (vùng mặc định của chạy lô cho video dọc) thì hai câu Trung khác nhau chỉ lệch
# 0,008–0,016 < 0,02 -> không bao giờ thấy đổi câu, cả 45 giây thành MỘT câu (rà soát
# 29-09-2026). Đo trên video đó: đổi câu 0,056–0,157, cùng câu tối đa 0,028.
ACTIVE_CHANGE_THRESH = 0.045
# Bỏ các khoảng quá ngắn (nhấp nháy 1 khung) — không phải phụ đề thật. Để thấp để
# không rớt mảnh đầu câu/fade-in; mảnh cùng nội dung sẽ được _merge_similar gộp lại.
MIN_INTERVAL_SEC = 0.15
# Gộp 2 câu liền kề nếu CÙNG/tương đồng text và cách nhau < ngưỡng này (dọn tách dư).
MERGE_GAP_SEC = 1.0
# Thời lượng tối thiểu để 1 câu ĐỌC KỊP: câu quá ngắn (vd flash 1 khung ~0.17s) sẽ
# được KÉO DÀI end tới mức này — NHƯNG không bao giờ đè lên câu kế tiếp.
MIN_DISPLAY_SEC = 0.6
# Giới hạn bề ngang crop đưa vào OCR (giữ nét chữ mà không phình băng thông).
MAX_OCR_WIDTH = 1600
# Điểm tin cậy tối thiểu của một ô chữ để giữ lại.
REC_SCORE_THRESH = 0.5
#  DÒ CHỮ (model det) làm đường DỰ PHÒNG khi mặt nạ chữ SUY BIẾN (08-10-2026): nền sáng lốm đốm (cát, tuyết, lá) có
#  điểm sáng cạnh điểm tối ở khắp nơi -> mặt nạ phủ kín cả dải, "dòng chữ" tách ra = cả khung, rec-only trên dải to
#  trả rỗng -> câu bị mất tới khi nền đổi (đo thật video Douyin "坎儿井": câu đầu hiện 0,3–2,0 s mà OCR chỉ thấy
#  1,87–2,03 s -> giọng đọc bị ép 0,17 s). Det của RapidOCR tìm đúng hộp chữ trên nền đó; đặt ``limit_side_len`` nhỏ
#  theo cạnh dài để chỉ mất ~60 ms/khung (mặc định 736/min mất ~1,2 s). Chỉ chạy khi hộp dòng suy biến (cao hơn nửa
#  khung VÀ rộng gần hết) hoặc rec-only không ra chữ dù có mặt nạ, nên khung bình thường không chậm đi.
DET_LIMIT_SIDE = 480
DET_SUY_BIEN_CAO = 0.5
DET_SUY_BIEN_RONG = 0.85
DET_KHUNG_TOI_THIEU = 80          # khung thấp hơn (người dùng khoanh sát một dòng) thì một dòng chiếm nửa khung là chuyện thường
# Khung ứng viên đầu đã đọc với độ tin cậy từ mức này -> khỏi đọc khung thứ hai.
OCR_DU_TIN = 0.92
# Nhịp log tiến độ OCR (mỗi N câu xong log 1 dòng).
OCR_BATCH = 32
# Số luồng OCR chạy song song (overlap với giải mã video). Sweet spot ~60% số nhân:
# quá ít -> không tận dụng hết CPU; quá nhiều (>= số nhân) -> tranh luồng với
# onnxruntime (mỗi call đã dùng nhiều nhân) làm CHẬM lại. Đo thực: 8 nhân -> 5 tối ưu.
def _default_ocr_workers(device: str = None) -> int:
    # Ép qua env để test: SUBFORGE_OCR_WORKERS=N.
    env = os.environ.get("SUBFORGE_OCR_WORKERS", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    cores = os.cpu_count() or 4
    if device and str(device).startswith("cuda"):
        # GPU: OCR chạy trên GPU nên ít worker CPU (2-4) là đủ; nhiều quá dễ OOM GPU.
        return max(2, min(4, cores))
    # CPU: mỗi engine giới hạn 1 luồng nội bộ -> 1 worker/nhân là tối ưu (cap 12).
    return max(2, min(12, cores))

_shared_ocr_service = None
_shared_ocr_lock = threading.Lock()


def get_ocr_service() -> "OCRService":
    global _shared_ocr_service
    if _shared_ocr_service is None:
        with _shared_ocr_lock:
            if _shared_ocr_service is None:
                _shared_ocr_service = OCRService()
    return _shared_ocr_service


class OCRService:
    def __init__(self):
        self._engine = None
        self._engine_det = None
        self.device = None  # 'cuda' | 'cpu'
        self._engine_lock = threading.Lock()

    # ---- Khởi tạo engine (GPU best-effort, tự lùi CPU) --------------------------
    def _detect_gpu_backend(self):
        """Chọn backend GPU khả dụng cho RapidOCR:
          - 'torch'      : torch-CUDA (tái dùng torch của GPU Pack — KHÔNG cần
                           onnxruntime-gpu). Ưu tiên vì tránh xung đột onnxruntime.
          - 'onnxruntime': onnxruntime-gpu (có CUDAExecutionProvider).
          - None         : không có GPU -> CPU.

        Biến môi trường để TEST/ép cấu hình (không cần app bật chế độ GPU):
          - SUBFORGE_OCR_ENGINE = torch | onnxruntime | cpu   (ép backend)
          - SUBFORGE_OCR_GPU    = 1                            (ép thử GPU)
        Mặc định: chỉ bật GPU khi get_best_device()=='cuda' (app ở chế độ GPU).
        """
        override = (os.environ.get("SUBFORGE_OCR_ENGINE") or "").strip().lower()
        if override in ("cpu", "off"):
            logger.info(L("OCR: ép CPU theo SUBFORGE_OCR_ENGINE.", "OCR: forcing CPU per SUBFORGE_OCR_ENGINE."))
            return None

        forced = override in ("torch", "onnxruntime") or (
            os.environ.get("SUBFORGE_OCR_GPU", "").strip().lower() in ("1", "true", "yes")
        )
        if not forced:
            try:
                if get_best_device()[0] != "cuda":
                    return None
            except Exception:
                return None

        # Dò thực tế backend nào sẵn sàng (để log rõ + quyết định).
        torch_cuda = False
        try:
            import torch
            torch_cuda = bool(torch.cuda.is_available())
        except Exception as e:
            logger.info(L(f"OCR: torch không dùng được ({e}).", f"OCR: torch is not usable ({e})."))
        ort_cuda = False
        try:
            import onnxruntime as ort
            ort_cuda = "CUDAExecutionProvider" in ort.get_available_providers()
        except Exception:
            pass
        logger.info(
            f"OCR GPU detect: torch.cuda={torch_cuda}, onnxruntime.cuda={ort_cuda}, "
            f"override='{override or '-'}'"
        )

        if override == "torch":
            return "torch"
        if override == "onnxruntime":
            return "onnxruntime"
        if torch_cuda:
            return "torch"
        if ort_cuda:
            return "onnxruntime"
        logger.warning(L("OCR: ép thử GPU nhưng KHÔNG có torch.cuda/onnxruntime-gpu -> CPU.", "OCR: GPU was forced but there is NO torch.cuda/onnxruntime-gpu -> CPU."))
        return None

    def _build_engine(self, gpu_backend):
        """Dựng RapidOCR (package 'rapidocr' 3.x). gpu_backend: 'torch'|'onnxruntime'|None.
        Hỏng ở nhánh GPU thì tự lùi CPU (không làm chết luồng)."""
        from rapidocr import RapidOCR

        if gpu_backend == "torch":
            # Dùng torch-CUDA cho cả 3 model (det/cls/rec đều qua torch).
            # rapidocr >= 3.x yêu cầu engine_type là Enum, không nhận chuỗi.
            try:
                from rapidocr import EngineType
                torch_engine = EngineType.TORCH
            except ImportError:
                torch_engine = "torch"
            params = {
                "Det.engine_type": torch_engine,
                "Cls.engine_type": torch_engine,
                "Rec.engine_type": torch_engine,
                "EngineConfig.torch.use_cuda": True,
            }
            try:
                eng = RapidOCR(params=params)
                self.device = "cuda (torch)"
                return eng
            except Exception as e:
                logger.warning(L(f"OCR: bật GPU (torch) thất bại ({e}), thử onnxruntime.", f"OCR: enabling GPU (torch) failed ({e}), trying onnxruntime."))
                gpu_backend = "onnxruntime"

        if gpu_backend == "onnxruntime":
            # CHỈ dùng khi onnxruntime THỰC SỰ có CUDAExecutionProvider (tức đã cài
            # onnxruntime-gpu). Nếu không, use_cuda=True KHÔNG báo lỗi mà âm thầm chạy
            # CPU + gắn nhãn 'cuda' sai -> phải chặn để không chọn nhầm hwaccel cuda.
            ort_ok = False
            try:
                import onnxruntime as ort
                ort_ok = "CUDAExecutionProvider" in ort.get_available_providers()
            except Exception:
                ort_ok = False
            if ort_ok:
                try:
                    eng = RapidOCR(params={"EngineConfig.onnxruntime.use_cuda": True})
                    self.device = "cuda (onnxruntime)"
                    return eng
                except Exception as e:
                    logger.warning(L(f"OCR: bật GPU (onnxruntime) thất bại ({e}), dùng CPU.", f"OCR: enabling GPU (onnxruntime) failed ({e}), using CPU."))
            else:
                logger.warning(
                    L("OCR: onnxruntime KHÔNG có CUDAExecutionProvider (chưa cài "
                    "onnxruntime-gpu) -> dùng CPU.", "OCR: onnxruntime has NO CUDAExecutionProvider (onnxruntime-gpu not installed) -> using CPU.")
                )

        # CPU: BẮT BUỘC giới hạn 1 luồng nội bộ/onnxruntime. Vì ta chạy NHIỀU worker
        # OCR song song (mỗi worker 1 call), nếu mỗi call lại dùng hết nhân -> tranh
        # luồng thảm hại (đo được: chậm ~4.5x). intra=1 + workers=số nhân -> nhanh nhất.
        self.device = "cpu"
        try:
            return RapidOCR(params={"EngineConfig.onnxruntime.intra_op_num_threads": 1})
        except Exception:
            return RapidOCR()

    def _ensure_engine_det(self):
        """Engine RIÊNG cho bước dò chữ dự phòng (xem DET_LIMIT_SIDE) — CPU là đủ (~60 ms/khung)."""
        if getattr(self, "_engine_det", None) is not None:
            return
        with self._engine_lock:
            if getattr(self, "_engine_det", None) is not None:
                return
            from rapidocr import RapidOCR

            try:
                self._engine_det = RapidOCR(params={
                    "Det.limit_side_len": DET_LIMIT_SIDE,
                    "Det.limit_type": "max",
                    "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                })
            except Exception as e:  # noqa: BLE001
                logger.warning(L(f"OCR: không dựng được engine dò chữ dự phòng ({e}) — bỏ đường dự phòng.", f"OCR: could not build the fallback text detection engine ({e}) — dropping the fallback path."))
                self._engine_det = False

    def _det_rec(self, gray: np.ndarray, with_score: bool = False):
        """Dò hộp chữ bằng model det rồi rec-only từng hộp ở độ phân giải gốc. Trả như ``_ocr_frame``."""
        try:
            self._ensure_engine_det()
            if not self._engine_det:
                return ("", 0.0) if with_score else ""
            img = np.stack([gray, gray, gray], axis=-1)
            res = self._engine_det(img, use_det=True, use_rec=False, use_cls=False)
            boxes = getattr(res, "boxes", None)
            if boxes is None or len(boxes) == 0:
                return ("", 0.0) if with_score else ""
            H, W = gray.shape
            hop = []
            for bx in boxes:
                arr = np.asarray(bx, dtype=np.float32).reshape(-1, 2)
                x0, y0 = int(arr[:, 0].min()) - 6, int(arr[:, 1].min()) - 6
                x1, y1 = int(arr[:, 0].max()) + 6, int(arr[:, 1].max()) + 6
                x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
                if y1 - y0 < 8 or x1 - x0 < 8:
                    continue
                hop.append((y0, y1, x0, x1))
            hop.sort(key=lambda h: (h[0], h[2]))
            parts, diem = [], []
            for (y0, y1, x0, x1) in hop:
                t, d = self._rec_line(gray[y0:y1, x0:x1], with_score=True)
                if t:
                    parts.append(t)
                    diem.append(d)
            txt = " ".join(parts).strip()
            if with_score:
                return txt, (sum(diem) / len(diem) if diem else 0.0)
            return txt
        except Exception as e:  # noqa: BLE001
            logger.debug(L(f"OCR: dò chữ dự phòng lỗi ({type(e).__name__}: {e})", f"OCR: fallback text detection error ({type(e).__name__}: {e})"))
            return ("", 0.0) if with_score else ""

    def _ensure_engine(self):
        if self._engine is not None:
            return
        with self._engine_lock:
            if self._engine is not None:
                return
            # RapidOCR log WARNING mỗi khung không có chữ -> rất ồn. Chỉ giữ ERROR.
            logging.getLogger("RapidOCR").setLevel(logging.ERROR)
            gpu_backend = None
            try:
                gpu_backend = self._detect_gpu_backend()
            except Exception:
                gpu_backend = None
            self._engine = self._build_engine(gpu_backend)
            if self.device and self.device.startswith("cuda"):
                logger.info(L(f"OCR: dùng GPU để nhận dạng chữ [{self.device}].", f"OCR: using GPU for text recognition [{self.device}]."))
            else:
                logger.warning(
                    L("OCR: chạy bằng CPU (không có GPU/onnxruntime-gpu) — vẫn nhanh "
                    "nhờ khử trùng lặp, nhưng có GPU sẽ nhanh hơn.", "OCR: running on CPU (no GPU/onnxruntime-gpu) — still fast thanks to de-duplication, but a GPU would be faster.")
                )

    # ---- Tiện ích FFmpeg -------------------------------------------------------
    _hwaccels_cache = None

    @staticmethod
    def _list_hwaccels():
        if OCRService._hwaccels_cache is not None:
            return OCRService._hwaccels_cache
        accels = set()
        try:
            res = subprocess.run(
                [FFMPEG_EXE, "-hide_banner", "-hwaccels"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=15, close_fds=True,
            )
            for line in (res.stdout or "").splitlines():
                s = line.strip().lower()
                if s and s != "hardware" and " " not in s:
                    accels.add(s)
        except Exception:
            pass
        OCRService._hwaccels_cache = accels
        return accels

    def _pick_hwaccel(self) -> str:
        """'cuda' khi đang ở chế độ GPU VÀ ffmpeg có NVDEC; ngược lại 'auto' (an toàn,
        không bao giờ lỗi, tự dùng HW nếu ffmpeg hỗ trợ). GPU decode thật cần ffmpeg
        build kèm CUDA — thường phải ship trong GPU Pack.
        Ép để test: SUBFORGE_OCR_HWACCEL = cuda | auto."""
        override = (os.environ.get("SUBFORGE_OCR_HWACCEL") or "").strip().lower()
        if override in ("cuda", "auto"):
            return override
        try:
            if self.device and self.device.startswith("cuda"):
                if "cuda" in self._list_hwaccels():
                    return "cuda"
                logger.info(
                    L("OCR: đang GPU nhưng ffmpeg KHÔNG có NVDEC -> giải mã CPU ('auto'). "
                    "Muốn giải mã GPU cần ffmpeg build kèm CUDA (ship trong GPU Pack).", "OCR: on GPU but ffmpeg has NO NVDEC -> decoding on CPU ('auto'). GPU decoding needs an ffmpeg build with CUDA (shipped in the GPU Pack).")
                )
        except Exception:
            pass
        return "auto"

    def _probe(self, video_path: str):
        """Trả (width, height, duration_sec). Dùng helper sẵn có của app (đều có
        fallback parse ffmpeg -i, không phụ thuộc ffprobe — imageio_ffmpeg không
        kèm ffprobe)."""
        from utils.aspect_crop import probe_video_dimensions
        from services.video_service import VideoService

        w, h = probe_video_dimensions(video_path)
        dur = float(VideoService.get_video_duration(video_path) or 0.0)
        if not w or not h:
            raise RuntimeError(
                L(f"Không đọc được kích thước video: {video_path}", f"Could not read the video size: {video_path}")
            )
        return w, h, dur

    @staticmethod
    def _even(n: int) -> int:
        n = int(round(n))
        return n - (n % 2)

    def _resolve_crop(self, w: int, h: int, region: dict):
        """region = {x,y,w,h} chuẩn hoá 0..1 -> (cx, cy, cw, ch) pixel, kẹp trong khung.
        Mặc định (không khoanh vùng) = dải đáy ~20%."""
        if not region:
            region = {"x": 0.05, "y": 0.78, "w": 0.90, "h": 0.20}
        cx = self._even(max(0.0, min(1.0, region.get("x", 0.0))) * w)
        cy = self._even(max(0.0, min(1.0, region.get("y", 0.0))) * h)
        cw = self._even(max(0.02, min(1.0, region.get("w", 1.0))) * w)
        ch = self._even(max(0.02, min(1.0, region.get("h", 1.0))) * h)
        cw = min(cw, self._even(w - cx))
        ch = min(ch, self._even(h - cy))
        return cx, cy, max(cw, 2), max(ch, 2)

    # ---- Vân tay khung + so sánh đổi khung -------------------------------------
    @staticmethod
    def _fingerprint(gray: np.ndarray, gh: int = 18, gw: int = 64) -> np.ndarray:
        """Thu khung xám về lưới GH×GW (mean-pooling) làm 'vân tay' để so nhanh."""
        h, w = gray.shape
        gh = min(gh, h)
        gw = min(gw, w)
        hh = (h // gh) * gh
        ww = (w // gw) * gw
        block = gray[:hh, :ww].reshape(gh, hh // gh, gw, ww // gw)
        return block.mean(axis=(1, 3)).astype(np.float32)

    # ---- OCR một khung -> chuỗi text -------------------------------------------
    def _rec_line(self, gray_line: np.ndarray, with_score: bool = False):
        """Nhận dạng MỘT dòng chữ đã crop sát (rec-only, bỏ detection -> nhanh).
        ``with_score``: trả (text, điểm tin cậy trung bình) thay vì chỉ text."""
        img = np.stack([gray_line, gray_line, gray_line], axis=-1)
        try:
            try:
                res = self._engine(img, use_det=False, use_rec=True)
            except TypeError:
                res = self._engine(img)
        except Exception as e:  # noqa: BLE001
            #  OpenCV trong RapidOCR có lúc ném "Unknown C++ exception" với một dải chữ lạ
            #  (đo 30-09-2026). Để lọt ra là CHẾT luồng OCR -> hàng đợi (maxsize 256) đầy ->
            #  luồng giải mã đứng mãi, OCR treo. Dòng lỗi coi như không đọc được chữ.
            logger.debug(L(f"OCR: bỏ một dòng đọc lỗi ({type(e).__name__}: {e})", f"OCR: dropped a line that failed to read ({type(e).__name__}: {e})"))
            res = None
        if res is None:
            return ("", 0.0) if with_score else ""
        txts = getattr(res, "txts", None)
        if not txts:
            return ("", 0.0) if with_score else ""
        scores = getattr(res, "scores", None) or ()
        parts = []
        diem = []
        for i, text in enumerate(txts):
            score = scores[i] if i < len(scores) else 1.0
            if score is not None and score < REC_SCORE_THRESH:
                continue
            t = (text or "").strip()
            if t:
                parts.append(t)
                diem.append(float(score if score is not None else 1.0))
        txt = " ".join(parts).strip()
        if with_score:
            return txt, (sum(diem) / len(diem) if diem else 0.0)
        return txt

    def _ocr_frame(self, gray: np.ndarray, with_score: bool = False):
        """OCR 1 khung xám. Trả text (nối các dòng bằng khoảng trắng) hoặc ''.

        QUAN TRỌNG: KHÔNG rec cả dải rộng (chữ nằm giữa + nhiều khoảng trống bị bóp
        méo -> đọc sai, nhất là phim sub tiếng Anh). Thay vào đó dùng MẶT NẠ CHỮ để:
          1) tách các DÒNG chữ (sub 1 dòng hoặc 2 dòng),
          2) crop SÁT từng dòng (bỏ khoảng trống trái/phải),
          3) rec-only từng dòng (nhanh, không cần detection model).
        Vừa nhanh (rec-only) vừa chính xác (như có detection nhờ crop bằng mặt nạ).
        """
        boxes = _line_boxes(gray)
        if not boxes:
            return ("", 0.0) if with_score else ""
        Hc, Wc = gray.shape
        #  Mặt nạ SUY BIẾN (nền lốm đốm sáng): "dòng" = gần cả khung -> dò chữ bằng model det (xem DET_LIMIT_SIDE).
        if Hc >= DET_KHUNG_TOI_THIEU and all(
            (y1 - y0) > DET_SUY_BIEN_CAO * Hc and (x1 - x0) > DET_SUY_BIEN_RONG * Wc for (y0, y1, x0, x1) in boxes
        ):
            return self._det_rec(gray, with_score)
        parts = []
        diem = []
        for (y0, y1, x0, x1) in boxes:
            t, d = self._rec_line(gray[y0:y1, x0:x1], with_score=True)
            if t:
                parts.append(t)
                diem.append(d)
        txt = " ".join(parts).strip()
        if not txt:
            #  Có mặt nạ mà rec-only không ra chữ: hộp dòng cắt sai (nền rối) -> thử dò chữ.
            return self._det_rec(gray, with_score)
        if with_score:
            return txt, (sum(diem) / len(diem) if diem else 0.0)
        return txt

    # ---- Luồng chính ----------------------------------------------------------
    def extract_subtitles(
        self,
        video_path: str,
        region: dict = None,
        lang: str = "ch",
        sample_fps: float = DEFAULT_SAMPLE_FPS,
        change_thresh: float = DEFAULT_CHANGE_THRESH,
        check_cancel=None,
        windows: list = None,
        muc: str = None,
    ) -> list:
        """Trích phụ đề cứng -> list segment {index,start,end,text} (giống STT).

        region: {x,y,w,h} chuẩn hoá 0..1 (người dùng khoanh trên FE).
        lang: 'ch' (Trung+Anh) — hiện engine mặc định phủ cả hai.
        windows: None -> quét CẢ video. Hoặc list (ss, to) giây -> CHỈ quét các cửa
            sổ đó (dùng cho chế độ Whisper+OCR vá lỗ: chỉ OCR chỗ Whisper bỏ sót).
            ffmpeg input-seek (-ss trước -i) chỉ giải mã đúng cửa sổ -> nhanh hơn
            NHIỀU khi tổng cửa sổ nhỏ so với toàn video. Mốc thời gian là TUYỆT ĐỐI.
        muc: chất lượng lấy phụ đề — None -> theo Cài đặt. "nhanh": như trước (đoán đổi câu).
            "can_bang" / "chinh_xac": ĐỌC MỌI khung có chữ (3 / 6 khung/giây) rồi gom theo nội
            dung — chậm hơn, không phụ thuộc bước đoán đổi câu (nơi mọi lỗi sót câu từng nằm).
            Mọi mức chỉ đọc trong VÙNG khoanh (``region``).
        """
        if muc is None:
            try:
                from services.config_service import get_config_service
                muc = get_config_service().get_subtitle_quality()
            except Exception:  # noqa: BLE001
                muc = "can_bang"
        chinh_xac = muc in ("can_bang", "chinh_xac")
        if muc == "chinh_xac":
            sample_fps = max(float(sample_fps), ACCURATE_SAMPLE_FPS)
        video_path = os.path.abspath(video_path)
        if not os.path.exists(video_path):
            raise FileNotFoundError(L(f"Không tìm thấy video: {video_path}", f"Video not found: {video_path}"))

        self._ensure_engine()
        w, h, duration = self._probe(video_path)
        cx, cy, cw, ch = self._resolve_crop(w, h, region)

        # Scale xuống nếu crop quá rộng (giữ nét chữ, giảm băng thông pipe).
        if cw > MAX_OCR_WIDTH:
            out_w = self._even(MAX_OCR_WIDTH)
            out_h = self._even(ch * MAX_OCR_WIDTH / cw)
            scale_vf = f",scale={out_w}:{out_h}"
        else:
            out_w, out_h = cw, ch
            scale_vf = ""

        #  Khung ra ở REFINE_FPS; cứ ``buoc`` khung mới phân tích một lần (= sample_fps).
        buoc = max(1, int(round(REFINE_FPS / max(0.5, sample_fps))))
        nhip = sample_fps * buoc                 # nhịp khung ra THẬT (vd 3 × 10 = 30)
        vf = f"fps={nhip:g},crop={cw}:{ch}:{cx}:{cy}{scale_vf},format=gray"
        frame_bytes = out_w * out_h
        hwaccel = self._pick_hwaccel()

        # Chuẩn hoá danh sách cửa sổ: None -> cả video.
        full_scan = not windows
        win_list = [(0.0, duration)] if full_scan else [
            (max(0.0, float(s)), float(e)) for s, e in windows if float(e) > float(s)
        ]
        total_win_dur = sum(e - s for s, e in win_list) or 0.1

        logger.info(
            L(f"OCR: quét phụ đề vùng {cw}x{ch} tại ({cx},{cy}) trên video {w}x{h} "
            f"({_fmt_ts(duration)}) — {'cả video' if full_scan else f'{len(win_list)} cửa sổ, tổng {_fmt_ts(total_win_dur)}'}, "
            f"lấy mẫu {sample_fps:g} fps, giải mã '{hwaccel}'", f"OCR: scanning subtitles in region {cw}x{ch} at ({cx},{cy}) on video {w}x{h} ({_fmt_ts(duration)}) — {'whole video' if full_scan else f'{len(win_list)} windows, total {_fmt_ts(total_win_dur)}'}, sampling {sample_fps:g} fps, decoding '{hwaccel}'")
            + (L(f" — mức '{muc}' (đọc mọi khung có chữ).", f" — level '{muc}' (reading every frame that has text).") if chinh_xac else ".")
        )

        segments: list = []
        ket_day: dict = {}                    # chế độ chính xác: khoá mẫu -> (chữ, điểm)
        mau_day: list = []                    # [(cửa sổ, [mẫu...])]
        import queue as _queue
        ocr_q: "_queue.Queue" = _queue.Queue(maxsize=256)
        done_count = {"n": 0}

        def _ocr_worker():
            while True:
                item = ocr_q.get()
                try:
                    if item is None:
                        return
                    if item[0] == "day":
                        #  Chế độ chính xác: đọc MỘT mẫu, ghi theo khoá (gom câu sau).
                        _k, khoa, g = item
                        ket_day[khoa] = self._ocr_frame(g, with_score=True)
                        done_count["n"] += 1
                        continue
                    s, e, ung_vien = item
                    #  Tối đa 2 khung (1/3 và 2/3 câu): một khung có thể dính chuyển cảnh /
                    #  nền trắng loá — đọc khung thứ hai khi khung đầu kém tin cậy.
                    text, diem = "", 0.0
                    for g in ung_vien:
                        t2, d2 = self._ocr_frame(g, with_score=True)
                        if t2 and (not text or d2 > diem):
                            text, diem = t2, d2
                        if text and diem >= OCR_DU_TIN:
                            break
                    if text:
                        segments.append({"start": s, "end": e, "text": text, "score": diem})
                    done_count["n"] += 1
                    if done_count["n"] % OCR_BATCH == 0:
                        logger.info(
                            L(f"OCR: đã xử lý {done_count['n']} câu (mốc {_fmt_ts(e)}).", f"OCR: processed {done_count['n']} sentences (up to {_fmt_ts(e)}).")
                        )
                finally:
                    ocr_q.task_done()

        n_workers = _default_ocr_workers(self.device)
        workers = [
            threading.Thread(target=_ocr_worker, daemon=True)
            for _ in range(n_workers)
        ]
        for _w in workers:
            _w.start()
        if str(self.device or "").startswith("cuda"):
            logger.info(
                L(f"OCR: nhận dạng trên GPU (CUDA), {n_workers} luồng đẩy khung hình.", f"OCR: recognizing on GPU (CUDA), {n_workers} threads feeding frames.")
            )
        else:
            logger.info(
                L(f"OCR: nhận dạng trên CPU — {n_workers} luồng song song "
                f"({os.cpu_count()} nhân CPU).", f"OCR: recognizing on CPU — {n_workers} parallel threads ({os.cpu_count()} CPU cores).")
            )

        def _scan_window(ss: float, to: float, done_before: float):
            """Quét 1 cửa sổ [ss, to] (giây, tuyệt đối). done_before = tổng thời lượng
            các cửa sổ TRƯỚC (để tính % tiến độ chung). Đẩy khung đại diện vào ocr_q."""
            win_dur = to - ss
            cmd = [FFMPEG_EXE, "-nostdin", "-v", "error", "-hwaccel", hwaccel]
            if ss > 0.05:
                cmd += ["-ss", f"{ss:.3f}"]          # input-seek nhanh (tới keyframe)
            cmd += ["-i", video_path]
            if not full_scan:
                cmd += ["-t", f"{win_dur:.3f}"]
            cmd += ["-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
            #  stderr vào TỆP TẠM, không DEVNULL: trước đây ffmpeg hỏng (vd vùng cắt tràn
            #  khung, -22) thì OCR vẫn "xong, 0 câu" trong 1 giây — người dùng không biết
            #  vì sao (24-09-2026). Không dùng PIPE: không ai đọc thì đầy bộ đệm, treo.
            import tempfile

            err_f = tempfile.TemporaryFile()
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=err_f,
                close_fds=True, bufsize=frame_bytes * 4,
            )

            #  TRẠNG THÁI câu đang hiện: vân tay lúc bắt đầu, mốc bắt đầu, và mọi MẪU của câu
            #  (lúc đóng chọn khung 1/3 & 2/3 để OCR — không lấy khung đầu: khung đầu hay là
            #  khung chuyển cảnh / chữ đang hiện dần, OCR đọc rác rồi cả câu mất theo).
            cur_fp = None
            cur_start = 0.0
            cur_repr = None                            # các MẪU (3 fps) của câu đang hiện
            next_log_pct = 10
            dem = []                                   # [(t, gray)] giữa hai mẫu
            #  Khoảng đệm TRƯỚC + mẫu trước đó: câu mới dài gần bằng câu cũ thì ở mẫu đầu
            #  tiên sau khi đổi, khoảng cách vân tay có thể CHƯA vượt ngưỡng — tới mẫu sau
            #  mới vượt. Chỉ dò trong khoảng đệm cuối là trễ đúng một mẫu (0,33 s — đo được).
            dem_truoc = []
            mau_truoc = None

            def _close(end_t):
                nonlocal cur_start, cur_repr
                if cur_repr and end_t - cur_start >= MIN_INTERVAL_SEC:
                    #  Khung ĐẠI DIỆN: mẫu ở 1/3 và 2/3 câu — tránh khung đầu (chữ đang hiện
                    #  dần / chuyển cảnh) và khung cuối (đang tắt). Câu ngắn chỉ 1 mẫu.
                    n = len(cur_repr)
                    chon = sorted({n // 3, (2 * n) // 3}) if n >= 3 else [n // 2]
                    ocr_q.put((cur_start, end_t, [cur_repr[i] for i in chon]))

            def _trong(g) -> bool:
                return _la_trong(_text_mask(g))

            def _do_bien(t_mau, la_moc):
                """Mốc của khung ĐẦU TIÊN trong bộ đệm thoả ``la_moc``; không có thì
                chính mẫu hiện tại (đổi xảy ra đúng giữa khung đệm cuối và mẫu này)."""
                for tt, gg in dem:
                    if la_moc(gg):
                        return tt
                return t_mau

            idx = 0
            try:
                last_t = ss
                while True:
                    buf = _read_exact(proc.stdout, frame_bytes)
                    if buf is None:
                        break
                    t = ss + idx / nhip                 # mốc TUYỆT ĐỐI của khung này
                    last_t = t
                    la_mau = idx % buoc == 0
                    idx += 1
                    gray = np.frombuffer(buf, dtype=np.uint8).reshape(out_h, out_w)
                    if not la_mau:
                        dem.append((t, gray))
                        continue
                    if check_cancel is not None and idx % (60 * buoc) == 1:
                        check_cancel()

                    overall = (done_before + (t - ss)) / total_win_dur * 100
                    if overall >= next_log_pct:
                        logger.info(
                            L(f"OCR: đang quét {min(99, int(overall))}% "
                            f"(mốc {_fmt_ts(t)}) — {done_count['n']} câu xong", f"OCR: scanning {min(99, int(overall))}% (at {_fmt_ts(t)}) — {done_count['n']} sentences done")
                        )
                        next_log_pct = int(overall) + 10

                    # Phát hiện TRỐNG bằng mặt nạ TƯƠNG PHẢN (chữ = sáng+viền tối) ->
                    # nền sáng trơn không tính là chữ -> biết đúng lúc sub biến mất.
                    mask = _text_mask(gray)
                    if _la_trong(mask):
                        if cur_repr is not None:
                            _close(_do_bien(t, _trong))     # đúng khung sub BIẾN MẤT
                            cur_fp = cur_repr = None
                        dem_truoc, dem, mau_truoc = [], [], None
                        continue

                    # Phát hiện ĐỔI CÂU bằng mặt nạ SÁNG (nhạy hơn -> bắt đủ lần đổi). ĐO
                    # 24-09-2026: vân tay trên mặt nạ chữ (sáng+viền tối) kém nhạy, nuốt
                    # 13/40 câu nối liền nhau trên video thật — nên KHÔNG dùng ở đây.
                    fp = _vantay(gray)
                    if cur_repr is None:
                        # sub XUẤT HIỆN: dò đúng khung đầu tiên có chữ (khung đầu cửa sổ
                        # thì giữ nguyên ss).
                        bat_dau = ss if idx == 1 else _do_bien(t, lambda g: not _trong(g))
                        cur_fp, cur_start, cur_repr = fp, bat_dau, [gray.copy()]
                        dem_truoc, dem, mau_truoc = [], [], (t, gray)
                        continue
                    if _doi_cau(fp, cur_fp, change_thresh):
                        # ĐỔI câu. Dò trong khoảng đệm trước + mẫu trước + khoảng đệm cuối:
                        # khung đầu tiên GIỐNG CÂU MỚI HƠN CÂU CŨ (hoặc trống — khe hở ngắn
                        # giữa hai câu) là lúc câu cũ tắt; khung đầu tiên CÓ CHỮ từ đó là lúc
                        # câu mới hiện. So "gần bên nào hơn" thay vì "khác câu cũ quá ngưỡng":
                        # hai câu dài gần bằng nhau thì khung đã đổi vẫn có thể dưới ngưỡng.
                        ung = list(dem_truoc)
                        if mau_truoc is not None and mau_truoc[0] > cur_start:
                            ung.append(mau_truoc)
                        ung += dem
                        het = None
                        moi_bd = None
                        for tt, gg in ung:
                            if tt <= cur_start:
                                continue
                            if het is None:
                                if _trong(gg):
                                    het = tt
                                    continue
                                v = _vantay(gg)
                                if float(np.mean(np.abs(v - fp))) < float(np.mean(np.abs(v - cur_fp))):
                                    het = moi_bd = tt
                            elif moi_bd is None and not _trong(gg):
                                moi_bd = tt
                        het = t if het is None else het
                        moi_bd = t if moi_bd is None else moi_bd
                        #  Mẫu trước đã bị tính vào câu cũ mà thật ra thuộc câu mới -> bỏ khỏi
                        #  khung ứng viên OCR của câu cũ.
                        if mau_truoc is not None and het <= mau_truoc[0] and len(cur_repr) > 1:
                            cur_repr.pop()
                        _close(het)
                        cur_fp, cur_start, cur_repr = fp, moi_bd, [gray.copy()]
                    else:
                        cur_repr.append(gray.copy())
                    dem_truoc = dem
                    dem = []
                    mau_truoc = (t, gray)

                # Hết cửa sổ khi câu vẫn đang hiện: kết thúc ở khung cuối + 1 khung.
                _close(min(to, last_t + 1.0 / nhip))
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                try:
                    err_f.seek(0)
                    err_txt = err_f.read().decode("utf-8", "replace")
                    err_f.close()
                except Exception:
                    err_txt = ""
            #  ffmpeg thoát lỗi mà KHÔNG ra khung nào -> báo lỗi thật (kèm đuôi stderr),
            #  không trả "0 câu" như thể video không có phụ đề. Có khung rồi mới lỗi (tệp
            #  hỏng đoạn cuối) thì giữ phần đã đọc, chỉ ghi cảnh báo.
            if proc.returncode not in (0, None):
                from services.compositor_utils import tom_loi_ffmpeg

                ly_do = tom_loi_ffmpeg(err_txt, 400) or L(f"mã {proc.returncode}", f"code {proc.returncode}")
                if idx == 0:
                    raise RuntimeError(L(f"OCR: không đọc được khung hình video — ffmpeg báo: {ly_do}", f"OCR: could not read the video frames — ffmpeg reported: {ly_do}"))
                logger.warning(L(f"OCR: ffmpeg dừng giữa chừng sau {idx} khung — {ly_do}", f"OCR: ffmpeg stopped midway after {idx} frames — {ly_do}"))

        def _scan_window_day(ss: float, to: float, done_before: float, win_idx: int):
            """CHẾ ĐỘ CHÍNH XÁC: mỗi mẫu có chữ đều được đọc (trừ mẫu giống hệt mẫu trước).
            Mốc chuyển giữa hai mẫu liền nhau dò trong bộ đệm khung ở REFINE_FPS: khung đầu
            tiên giống mẫu sau hơn mẫu trước (hoặc bắt đầu / hết chữ) — sát tới ~1 khung."""
            win_dur = to - ss
            cmd = [FFMPEG_EXE, "-nostdin", "-v", "error", "-hwaccel", hwaccel]
            if ss > 0.05:
                cmd += ["-ss", f"{ss:.3f}"]
            cmd += ["-i", video_path]
            if not full_scan:
                cmd += ["-t", f"{win_dur:.3f}"]
            cmd += ["-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
            import tempfile

            err_f = tempfile.TemporaryFile()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f,
                                    close_fds=True, bufsize=frame_bytes * 4)
            mau: list = []
            mau_day.append((win_idx, mau))
            dem = []
            truoc = None                      # (trong, fp) của mẫu trước
            fp_doc = None                     # vân tay của mẫu gần nhất ĐÃ ĐỌC
            next_log_pct = 10
            idx = 0
            last_t = ss
            try:
                while True:
                    buf = _read_exact(proc.stdout, frame_bytes)
                    if buf is None:
                        break
                    t = ss + idx / nhip
                    last_t = t
                    la_mau = idx % buoc == 0
                    idx += 1
                    gray = np.frombuffer(buf, dtype=np.uint8).reshape(out_h, out_w)
                    if not la_mau:
                        dem.append((t, gray))
                        continue
                    if check_cancel is not None and idx % (60 * buoc) == 1:
                        check_cancel()
                    overall = (done_before + (t - ss)) / total_win_dur * 100
                    if overall >= next_log_pct:
                        logger.info(L(f"OCR (chính xác): đang quét {min(99, int(overall))}% "
                                    f"(mốc {_fmt_ts(t)}) — đã đọc {done_count['n']} khung", f"OCR (accurate): scanning {min(99, int(overall))}% (at {_fmt_ts(t)}) — {done_count['n']} frames read"))
                        next_log_pct = int(overall) + 10
                    trong = _la_trong(_text_mask(gray))
                    fp = _vantay(gray)
                    #  Mốc chuyển từ mẫu trước sang mẫu này.
                    moc = t
                    if truoc is not None:
                        tr_truoc, fp_truoc = truoc
                        for tt, gg in dem:
                            tg = _la_trong(_text_mask(gg))
                            if tr_truoc and not trong:
                                hit = not tg
                            elif trong and not tr_truoc:
                                hit = tg
                            elif not trong and not tr_truoc:
                                v = _vantay(gg)
                                hit = float(np.mean(np.abs(v - fp))) < float(np.mean(np.abs(v - fp_truoc)))
                            else:
                                hit = False
                            if hit:
                                moc = tt
                                break
                    else:
                        moc = ss if idx == 1 else t
                    reuse = False
                    if not trong and fp_doc is not None:
                        d = np.abs(fp - fp_doc)
                        co = (fp > 0) | (fp_doc > 0)
                        reuse = bool(co.any()) and float(d[co].mean()) < ACCURATE_REUSE_DIFF
                    k = (win_idx, len(mau))
                    mau.append({"t": t, "moc": moc, "trong": trong, "reuse": reuse, "k": k})
                    if not trong and not reuse:
                        fp_doc = fp
                        ocr_q.put(("day", k, gray.copy()))
                    elif trong:
                        fp_doc = None
                    truoc = (trong, fp)
                    dem = []
                mau.append({"t": last_t, "moc": min(to, last_t + 1.0 / nhip), "trong": True,
                            "reuse": False, "k": None})     # mốc kết thúc cửa sổ
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                try:
                    err_f.seek(0)
                    err_txt = err_f.read().decode("utf-8", "replace")
                    err_f.close()
                except Exception:
                    err_txt = ""
            if proc.returncode not in (0, None):
                from services.compositor_utils import tom_loi_ffmpeg

                ly_do = tom_loi_ffmpeg(err_txt, 400) or L(f"mã {proc.returncode}", f"code {proc.returncode}")
                if idx == 0:
                    raise RuntimeError(L(f"OCR: không đọc được khung hình video — ffmpeg báo: {ly_do}", f"OCR: could not read the video frames — ffmpeg reported: {ly_do}"))
                logger.warning(L(f"OCR: ffmpeg dừng giữa chừng sau {idx} khung — {ly_do}", f"OCR: ffmpeg stopped midway after {idx} frames — {ly_do}"))

        def _gom_day() -> None:
            """Chế độ chính xác: gom các mẫu liên tiếp CÙNG NỘI DUNG thành câu."""
            for _win, mau in mau_day:
                #  Chữ của từng mẫu (mẫu "dùng lại" lấy chữ của mẫu đã đọc gần nhất).
                ds = []
                chu_truoc = ("", 0.0)
                for m in mau:
                    if m["trong"]:
                        ds.append(("", 0.0))
                        continue
                    if not m["reuse"]:
                        chu_truoc = ket_day.get(m["k"], ("", 0.0))
                    ds.append(chu_truoc)

                def _giong(nhom_, chu_):
                    return bool(chu_) and any(_text_similar(x, chu_, 0.7) for x, _d in nhom_["texts"][-3:])

                nhom = None               # {"start", "texts": [(chu, diem)]}
                for i, m in enumerate(mau):
                    chu, diem = ds[i]
                    if nhom is not None and _giong(nhom, chu):
                        nhom["texts"].append((chu, diem))
                        continue
                    #  KHUNG ĐỌC LỆCH giữa câu (chữ đang chuyển cảnh / loá): 1–2 mẫu kế tiếp
                    #  quay lại đúng câu đang gom thì bỏ qua mẫu này, KHÔNG cắt câu — không
                    #  thì một câu vỡ thành 3 mảnh (đo 30-09-2026: "…重力坝 40" / "…规大的生力现"
                    #  / "…重力坝").
                    if nhom is not None and not m["trong"] and chu and any(
                            _giong(nhom, ds[j][0]) for j in range(i + 1, min(len(mau), i + 3))):
                        continue
                    if nhom is not None:
                        _dong_nhom(nhom, m["moc"])
                        nhom = None
                    if chu:
                        nhom = {"start": m["moc"], "texts": [(chu, diem)]}
                if nhom is not None:
                    _dong_nhom(nhom, mau[-1]["moc"] if mau else nhom["start"])

        def _dong_nhom(nhom: dict, end_t: float) -> None:
            if end_t - nhom["start"] < MIN_INTERVAL_SEC:
                return
            #  Đọc dày là đọc cả khung nhiễu: "câu" chỉ MỘT mẫu và chỉ MỘT chữ ("十", "书")
            #  gần như luôn là vân nền bị đọc thành chữ — bỏ.
            if len(nhom["texts"]) == 1 and len(_norm_text(nhom["texts"][0][0])) <= 1:
                return
            from collections import Counter as _C

            phieu = _C()
            mau_chu = {}
            for chu, diem in nhom["texts"]:
                k = _norm_text(chu)
                phieu[k] += max(0.05, float(diem or 0.0))
                if k not in mau_chu or len(chu) > len(mau_chu[k]):
                    mau_chu[k] = chu
            best = max(phieu, key=lambda k: (phieu[k], len(k)))
            diem_tb = sum(float(d or 0.0) for _c, d in nhom["texts"]) / len(nhom["texts"])
            segments.append({"start": nhom["start"], "end": end_t, "text": mau_chu[best], "score": diem_tb})

        try:
            done_before = 0.0
            for wi, (ss, to) in enumerate(win_list):
                if check_cancel is not None:
                    check_cancel()
                if chinh_xac:
                    _scan_window_day(ss, to, done_before, wi)
                else:
                    _scan_window(ss, to, done_before)
                done_before += to - ss
        finally:
            for _w in workers:
                ocr_q.put(None)
            for _w in workers:
                _w.join()
        if chinh_xac:
            _gom_day()

        segments.sort(key=lambda s: s["start"])
        if not ((lang or "").lower() in ("en", "eng", "vi", "latin")):
            for s_ in segments:
                s_["text"] = _got_mep_cjk(s_["text"])
        # Bỏ đoạn RÁC (nhiễu chuyển cảnh) TRƯỚC khi gộp -> 2 câu thật 2 bên chập đúng.
        # Phim sub tiếng Anh/Latin: giữ từ ngắn thật ('No.','Hi!'); phim Trung: bỏ Latin ngắn.
        latin_lang = (lang or "").lower() in ("en", "eng", "vi", "latin")
        segments = [
            s for s in segments
            if not _looks_garbage(s["text"], s["end"] - s["start"], latin_lang)
        ]
        segments = _merge_similar(segments)
        segments = _enforce_min_duration(segments)
        for i, s in enumerate(segments):
            s["index"] = i
        logger.info(
            f"OCR-STATS ocr_calls={done_count['n']} -> segments={len(segments)}"
        )
        logger.info(L(f"=== OCR PHỤ ĐỀ HOÀN TẤT: {len(segments)} câu ===", f"=== SUBTITLE OCR COMPLETE: {len(segments)} sentences ==="))
        return segments


def compute_whisper_gaps(
    wh_segments: list,
    duration: float,
    min_gap: float = 1.0,
    pad: float = 0.5,
    merge_within: float = 1.5,
) -> list:
    """Từ các câu Whisper -> danh sách khoảng (ss, to) KHÔNG được Whisper phủ (>= min_gap
    giây) để OCR vá. Nới nhẹ 2 đầu (pad) cho khỏi hụt chữ; gộp các khoảng gần nhau
    (< merge_within) để giảm số lần gọi ffmpeg. duration<=0 -> []; không có câu -> cả video."""
    if duration and duration > 0 and not wh_segments:
        return [(0.0, duration)]
    if not duration or duration <= 0:
        return []
    covered = sorted(
        (max(0.0, s["start"]), min(duration, s["end"])) for s in wh_segments
    )
    gaps = []
    cursor = 0.0
    for cs, ce in covered:
        if cs - cursor >= min_gap:
            gaps.append((cursor, cs))
        cursor = max(cursor, ce)
    if duration - cursor >= min_gap:
        gaps.append((cursor, duration))
    if not gaps:
        return []
    padded = [(max(0.0, s - pad), min(duration, e + pad)) for s, e in gaps]
    merged = [list(padded[0])]
    for s, e in padded[1:]:
        if s - merged[-1][1] <= merge_within:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def overlaps_any(seg: dict, others: list, min_overlap: float = 0.3) -> bool:
    """True nếu điểm giữa seg nằm trong 1 câu khác, hoặc chồng lấn >= min_overlap giây.
    Dùng để loại đoạn OCR trùng vùng Whisper (phòng input-seek lấn keyframe)."""
    mid = (seg["start"] + seg["end"]) / 2.0
    for o in others:
        if o["start"] <= mid <= o["end"]:
            return True
        ov = min(seg["end"], o["end"]) - max(seg["start"], o["start"])
        if ov >= min_overlap:
            return True
    return False


def _doi_cau(fp: np.ndarray, cur_fp: np.ndarray, change_thresh: float) -> bool:
    """Hai vân tay có phải HAI CÂU khác nhau không. Giữ nguyên điều kiện cũ (trung bình cả
    lưới) — video vốn chạy tốt không đổi hành vi — cộng thêm điều kiện tính trên ô có chữ
    để vùng khoanh rộng không làm loãng tín hiệu (xem ``ACTIVE_CHANGE_THRESH``)."""
    d = np.abs(fp - cur_fp)
    if float(d.mean()) > change_thresh:
        return True
    co_chu = (fp > 0) | (cur_fp > 0)
    return bool(co_chu.any()) and float(d[co_chu].mean()) > ACTIVE_CHANGE_THRESH


def _la_trong(mask: np.ndarray) -> bool:
    """Khung KHÔNG có phụ đề. Giữ điều kiện cũ (tỉ lệ mực trên cả vùng) và chỉ coi là trống
    khi CẢ dải 16 hàng đậm nhất cũng thưa (xem ``BAND_INK_MIN``)."""
    if float(mask.mean()) >= BLANK_INK_MIN:
        return False
    H, W = mask.shape
    k = min(BAND_ROWS, H)
    if k <= 0 or W <= 0:
        return True
    rs = mask.sum(1).astype(np.float64)
    dai = float(np.convolve(rs, np.ones(k), "valid").max()) / (k * W)
    return dai < BAND_INK_MIN


def _vantay(gray: np.ndarray) -> np.ndarray:
    """Vân tay (lưới 18×64, mean-pooling) của mặt nạ SÁNG — so đổi câu."""
    return OCRService._fingerprint((gray >= TEXT_BRIGHT_THRESH).astype(np.float32))


def _text_mask(gray: np.ndarray) -> np.ndarray:
    """Mặt nạ 'mực chữ' = pixel SÁNG có VIỀN TỐI kề bên (trong bán kính nhỏ).
    Chữ phụ đề luôn có viền/bóng tối quanh nét sáng; nền sáng TRƠN (trời/tuyết/đèn)
    thì không -> bị loại. Nhờ vậy phát hiện 'trống' đáng tin cả khi nền sáng, tránh
    câu bị 'dính' (end quá muộn) và tách câu giả do nền."""
    bright = gray >= TEXT_BRIGHT_THRESH
    dark = gray <= DARK_OUTLINE_THRESH
    r = 2
    near = dark.copy()
    near[:, r:] |= dark[:, :-r]
    near[:, :-r] |= dark[:, r:]
    near[r:, :] |= dark[:-r, :]
    near[:-r, :] |= dark[r:, :]
    return bright & near


def _line_boxes(gray: np.ndarray, thresh: int = TEXT_BRIGHT_THRESH,
                pad: int = 6, min_line_h: int = 8, gap_merge: int = 8) -> list:
    """Từ mặt nạ chữ, tách các DÒNG chữ và crop SÁT từng dòng (loại khoảng trống
    trái/phải/trên/dưới). Trả list (y0,y1,x0,x1). Xử lý cả sub 1 dòng lẫn 2 dòng."""
    m = _text_mask(gray)
    H, W = m.shape
    rowsum = m.sum(1)
    if int(rowsum.max()) == 0:
        return []
    row_thr = max(2, rowsum.max() * 0.06)
    rows = np.where(rowsum > row_thr)[0]
    if len(rows) == 0:
        return []
    # Gom các dòng chữ: các hàng-có-chữ cách nhau <= gap_merge thì cùng 1 dòng.
    groups = []
    start = prev = rows[0]
    for r in rows[1:]:
        if r - prev <= gap_merge:
            prev = r
        else:
            groups.append((start, prev + 1))
            start = prev = r
    groups.append((start, prev + 1))

    boxes = []
    for y0, y1 in groups:
        if y1 - y0 < min_line_h:
            continue
        sub = m[y0:y1]
        colsum = sub.sum(0)
        col_thr = max(2, colsum.max() * 0.06)
        cols = np.where(colsum > col_thr)[0]
        if len(cols) == 0:
            continue
        lo, hi = int(cols[0]), int(cols[-1])
        #  NỚI MÉP theo nét THƯA. Mặt nạ chỉ là viền nét sáng, nên chữ một nét ngang như
        #  "一" chỉ có 2-4 điểm mỗi cột — dưới ngưỡng 6% của cột dày nhất, bị cắt khỏi khung
        #  và mất hẳn khỏi phụ đề ("一烧的时候…" ra "烧的时候…", người dùng báo 25-09-2026;
        #  video mờ còn mất thêm chữ kế bên). Đi tiếp ra hai bên qua mọi cột còn mực, miễn
        #  khe trống không rộng quá một chữ (~ cao dòng). "Còn mực" xét theo CỘT: có điểm
        #  sáng VÀ điểm viền tối trong dải dòng — video mờ đẩy viền cách nét sáng quá 2px
        #  nên mặt nạ theo điểm ảnh ra 0 hẳn, dù cột vẫn rõ là chữ (255 cạnh 24).
        lh = int(y1 - y0)
        band = gray[y0:y1]
        muc = np.where((colsum >= 2) | (
            (band >= TEXT_BRIGHT_THRESH).any(0) & (band <= DARK_OUTLINE_THRESH).any(0)))[0]
        for c in muc[muc < lo][::-1]:
            if lo - int(c) > lh:
                break
            lo = int(c)
        for c in muc[muc > hi]:
            if int(c) - hi > lh:
                break
            hi = int(c)
        x0 = max(0, lo - pad)
        x1 = min(W, hi + pad + 1)
        yy0 = max(0, int(y0) - pad)
        yy1 = min(H, int(y1) + pad)
        boxes.append((yy0, yy1, x0, x1))
    return boxes


def _norm_text(s: str) -> str:
    """Chuẩn hoá để so khớp OCR jitter: bỏ khoảng trắng + dấu câu."""
    return re.sub(r"[\s、。，,.!?！？…·:：;；\-—_\"'“”‘’()（）\[\]【】]", "", s or "")


def _text_similar(a: str, b: str, thresh: float = 0.82) -> bool:
    """2 text có phải CÙNG một câu phụ đề không (chịu được OCR sai lệch nhẹ / thiếu
    vài ký tự / một bên là con của bên kia)."""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return na == nb
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= thresh


def _looks_garbage(text: str, dur: float, latin_lang: bool = False) -> bool:
    """Nhận diện đoạn RÁC do OCR đọc nhầm ở khung chuyển cảnh (giữa 2 câu thật).

    latin_lang=True (phim sub tiếng ANH/Latin): chữ Latin ngắn LÀ nội dung thật
    ('No.', 'Hi!', 'OK', 'Go!') -> CHỈ bỏ khi rỗng, hoặc 1 ký tự + flash rất ngắn.
    latin_lang=False (phim CJK — Trung/Nhật): chuỗi Latin ngắn hầu như là nhiễu
    (vd 'AD','AK','NA'); flash ngắn + text ngắn cũng là nhiễu chuyển cảnh."""
    n = _norm_text(text)
    if not n:
        return True
    if latin_lang:
        # Chỉ loại nhiễu 1 ký tự chớp nhoáng; giữ mọi từ >=2 ký tự.
        if len(n) <= 1 and dur < 0.5:
            return True
        return False
    # CJK: chữ Hán 1-2-3 ký tự LÀ câu hợp lệ (vd '根生','走娘') -> KHÔNG bỏ theo độ dài.
    # Chỉ bỏ chuỗi thuần Latin/số ngắn (AD/AK/NA — nhiễu lạc vào video Trung).
    if len(n) <= 3 and re.fullmatch(r"[A-Za-z0-9]+", n):
        return True
    #  Chuỗi thuần Latin DÀI HƠN chút nhưng chớp nhoáng (vd "NNNMW" — vân nền bị đọc thành
    #  chữ ở cuối video) cũng là nhiễu ở phim Trung/Nhật.
    if len(n) <= 8 and dur < 1.0 and re.fullmatch(r"[A-Za-z0-9]+", n):
        #  8 chứ không 6: "N XHEEKL" (chữ in trên nền video, 0,6 s) từng lọt qua (rà soát
        #  29-09-2026). Tên riêng Latin thật trong phim Trung hiện lâu hơn 1 s.
        return True
    return False


#  Ký hiệu / dấu câu LẠC ở mép câu (vân nền, mép khung bị đọc thành "，", "—", "|").
#  KHÔNG gồm dấu ngoặc kép: "我们就称之为“一壮”" từng bị gọt mất ” (người dùng báo 25-09-2026).
_DAU_MEP = r"\-—–_·•,，.。、:：;；~～|/\\"
_MEP_DAU = re.compile(rf"^[\s{_DAU_MEP}]+|[\s{_DAU_MEP}]+$")
#  MỘT chữ cái đứng TÁCH RỜI bằng khoảng trắng ở mép ("S 下一期再见").
_MEP_CHU_LE = re.compile(r"^[A-Za-z]\s+(?=\S)|(?<=\S)\s+[A-Za-z]$")


def _got_mep_cjk(text: str) -> str:
    """Phim Trung/Nhật: gọt rác ở ĐẦU/CUỐI câu — CHỈ dấu câu / ký hiệu lạc ("，", "—", "|")
    và MỘT chữ cái đứng tách rời bằng khoảng trắng ("S 下一期再见").

    KHÔNG BAO GIỜ gọt chữ cái dính liền: sub Trung hay kèm chữ Latin thật ở mép câu —
    "实际上我们叫JIU", "OK了我们开始". Bản đầu (24-09-2026) gọt tới 4 chữ Latin mỗi đầu,
    xoá mất "JIU" (người dùng báo 25-09-2026). Thà sót một chữ rác dính liền ("M下一期再见")
    còn hơn xoá chữ thật. Chỉ gọt khi phần còn lại vẫn có >= 2 chữ CJK."""
    t = (text or "").strip()
    for _ in range(3):
        moi = _MEP_CHU_LE.sub("", _MEP_DAU.sub("", t)).strip()
        if moi == t or len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", moi)) < 2:
            break
        t = moi
    return t


def _enforce_min_duration(segments: list) -> list:
    """Câu quá ngắn (vd flash 1 khung ~0.17s) khó đọc/khó lồng tiếng -> KÉO DÀI end
    tới MIN_DISPLAY_SEC, NHƯNG không bao giờ đè lên câu kế (giữ khoảng hở nhỏ)."""
    for i, s in enumerate(segments):
        if s["end"] - s["start"] < MIN_DISPLAY_SEC:
            desired = s["start"] + MIN_DISPLAY_SEC
            nxt = segments[i + 1]["start"] if i + 1 < len(segments) else desired
            s["end"] = max(s["end"], min(desired, nxt - 0.01 if nxt > s["end"] else s["end"]))
    return segments


#  Mảnh chữ NẰM TRỌN trong câu bên cạnh (câu đang hiện dần / tắt dần: "记之中明确记载主" rồi
#  1,5 s sau "礼记之中明确记载主人就东阶迎客") là cùng một câu — gộp dù cách xa hơn
#  MERGE_GAP_SEC. Chỉ khi mảnh có >= 3 chữ (mảnh 1–2 chữ trùng ngẫu nhiên quá dễ).
CONTAIN_GAP_SEC = 2.0


def _nam_trong(a: str, b: str) -> bool:
    na, nb = _norm_text(a), _norm_text(b)
    ngan, dai = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(ngan) >= 3 and ngan != dai and ngan in dai


def _merge_similar(segments: list) -> list:
    """Gộp các câu LIỀN KỀ CÙNG NỘI DUNG (text trùng HOẶC tương đồng cao — dọn tách
    câu dư do nền sáng lọt vào mặt nạ + OCR lệch vài ký tự giữa các khung của cùng 1
    câu). Union thời gian (start sớm nhất → end muộn nhất) cho timeline đúng; text
    lấy theo BỎ PHIẾU (bản xuất hiện nhiều nhất — chống OCR sai lệch ngẫu nhiên),
    hoà phiếu thì lấy bản dài nhất."""
    from collections import Counter

    if not segments:
        return segments
    # 1) Gom các câu liền kề cùng nội dung thành nhóm.
    groups = [[dict(segments[0])]]
    for s in segments[1:]:
        nhom = groups[-1]
        khe = s["start"] - max(x["end"] for x in nhom)
        #  So với MỌI mảnh trong nhóm (không chỉ mảnh cuối): nền loá cắt một câu thành
        #  nhiều mảnh chữ lệch nhau ("M下一期再见", "下期再见", "期南见") — mảnh sau có thể
        #  giống mảnh đầu hơn mảnh ngay trước. Mảnh NỐI LIỀN (khe < 0,1 s) thì nới ngưỡng:
        #  hai câu thoại khác nhau hiếm khi giống nhau tới 70%.
        nguong = 0.70 if khe < 0.1 else 0.82
        if (khe <= MERGE_GAP_SEC and any(_text_similar(x["text"], s["text"], nguong) for x in nhom)) \
                or (khe <= CONTAIN_GAP_SEC and any(_nam_trong(x["text"], s["text"]) for x in nhom)):
            nhom.append(dict(s))
        else:
            groups.append([dict(s)])
    # 2) Mỗi nhóm -> 1 câu: thời gian union, text theo phiếu CÓ TRỌNG SỐ = thời lượng ×
    #    độ tin cậy OCR (mảnh dài đọc rõ thắng mảnh vụn đọc lệch); hoà thì bản dài hơn.
    out = []
    for g in groups:
        cnt: Counter = Counter()
        mau: dict = {}
        for x in g:
            k = _norm_text(x["text"])
            cnt[k] += max(0.05, x["end"] - x["start"]) * float(x.get("score", 1.0) or 0.0)
            if k not in mau or len(x["text"]) > len(mau[k]):
                mau[k] = x["text"]
        best_norm = max(cnt, key=lambda k: (cnt[k], len(k)))
        out.append({
            "start": g[0]["start"],
            "end": max(x["end"] for x in g),
            "text": mau[best_norm],
        })
    return out


def _read_exact(stream, n: int):
    """Đọc ĐỦ n byte từ pipe (ffmpeg có thể trả từng phần). None khi hết luồng."""
    chunks = []
    got = 0
    while got < n:
        part = stream.read(n - got)
        if not part:
            return None  # hết luồng (frame cuối dở dang -> bỏ)
        chunks.append(part)
        got += len(part)
    return b"".join(chunks)
