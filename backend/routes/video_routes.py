from fastapi import APIRouter, Depends, HTTPException, Body, Query, File, UploadFile, Request
from typing import List, Optional
from pydantic import BaseModel
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from models.database import get_db, engine
from models.models import Video, SubtitleSegment, SystemSetting
from services.video_service import VideoService
from services.compositor_service import CompositorService
from services.compositor_utils import (
    DEFAULT_COMPOSITOR_CONFIG,
    DUBBING_SUBTITLE_DEFAULTS,
    load_compositor_config,
    record_compositor_export,
    save_compositor_config,
    get_fonts_dir,
)
from services.stt_service import get_stt_service
from services.ocr_service import (
    get_ocr_service,
    compute_whisper_gaps as _compute_whisper_gaps,
    overlaps_any as _overlaps_any,
)
from services.translation_service import TranslationService
from services.gop_manh_ngan import gop_manh_ngan
from services.tach_cau import MAC_DINH_GOC, tach_cau_goc
from services.tts_service import TTSService
from services.tts_text import lam_sach_cau_doc
from services.config_service import get_config_service
import uuid
import json
import os
import threading
import time
import asyncio
import shutil
import logging
from utils.compositor_export_stream import compositor_export_sse_response
from utils.logo_utils import (
    process_and_save_frame_bg_image,
    process_and_save_logo_image,
    remove_logo_background_file,
)
from routes.auth_routes import verify_license
from utils.i18n import L

router = APIRouter(prefix="/videos", tags=["videos"], dependencies=[Depends(verify_license)])
logger = logging.getLogger(__name__)

def setup_project_logging(video_id, db):
    dirs = get_project_dirs(video_id, db)
    log_file = os.path.join(dirs["root"], "synthesis.log")

    # Giới hạn kích thước log MỖI DỰ ÁN: log là append-only nên qua nhiều lần chạy lại
    # sẽ phình to. Nếu > ~1MB thì giữ lại ~1000 dòng cuối. Best-effort: lỗi (vd file
    # đang được đọc/ghi) thì bỏ qua, KHÔNG chặn pipeline. Tailer tự nhận diện file bị
    # cắt (xem stream_video_logs) nên vẫn stream đúng sau khi trim.
    try:
        if os.path.exists(log_file) and os.path.getsize(log_file) > 1_000_000:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as _rf:
                _tail = _rf.readlines()[-1000:]
            with open(log_file, "w", encoding="utf-8") as _wf:
                _wf.writelines(_tail)
    except Exception:
        pass

    # Configure handlers
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    file_handler.setLevel(logging.INFO)
    
    # Add handler to all relevant loggers
    for logger_name in ["VideoService", "services.video_service", "STTService", "OCRService", "TranslationService", "DeepSeekService", "TTSService", "SeparationService", "routes.video_routes", "backend.routes.video_routes", "services.gop_manh_ngan", "services.tach_cau"]:
        l = logging.getLogger(logger_name)
        # Prevent duplicate handlers
        has_file_handler = any(isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(log_file) for h in l.handlers)
        if not has_file_handler:
            l.addHandler(file_handler)

    return file_handler

def remove_project_logging(file_handler):
    for logger_name in ["VideoService", "services.video_service", "STTService", "OCRService", "TranslationService", "DeepSeekService", "TTSService", "SeparationService", "routes.video_routes", "backend.routes.video_routes", "services.gop_manh_ngan", "services.tach_cau"]:
        l = logging.getLogger(logger_name)
        if file_handler in l.handlers:
            l.removeHandler(file_handler)
    file_handler.close()

# Services
stt_service = get_stt_service()
ocr_service = get_ocr_service()
translation_service = TranslationService()
tts_service = TTSService()

# --- KHOA CHONG CHAY CHONG (per video) ---
# Truoc day co the bam TTS/Merge nhieu lan lien tiep -> nhieu luot chay de len
# nhau: ghi de lan lon file seg_*.mp3, ngon het CPU/RAM (da tung gay
# WinError 1455 "paging file too small"), va lam buoc dang chay cham hon nhieu.
# Guard nay tu choi (409) khi video do dang co buoc nang (tts/merge) chay.
import threading as _threading

_PROCESSING_GUARD = {}
_PROCESSING_GUARD_LOCK = _threading.Lock()


def _acquire_step(video_id: str, step: str):
    """Tra ve True neu gianh duoc quyen chay; False neu dang co buoc khac chay."""
    with _PROCESSING_GUARD_LOCK:
        if video_id in _PROCESSING_GUARD:
            return False
        _PROCESSING_GUARD[video_id] = step
        return True


def _release_step(video_id: str):
    with _PROCESSING_GUARD_LOCK:
        _PROCESSING_GUARD.pop(video_id, None)


# --- HỦY tiến trình pipeline (giống cancel-render bên review) ---
# Cờ hủy theo video_id (in-memory, thread-safe). Bước đang chạy kiểm cờ tại các
# checkpoint (per-segment TTS, giữa các lô dịch, trước demucs/ffmpeg) và dừng sớm.
_CANCEL_REQUESTED: set = set()
_CANCEL_LOCK = _threading.Lock()

# Số câu tạo giọng mỗi LÔ (Edge-TTS). Video vài nghìn câu mà bắn hết một lượt sẽ
# bị Edge-TTS rate-limit cả cụm -> hàng loạt câu thất bại -> "chạy thiếu".
# 200 đủ lớn để vẫn bão hoà Semaphore(35) bên tts_service (không mất tốc độ),
# đủ nhỏ để không kích rate-limit.
_VOICE_BATCH_SIZE = 200

# Số vòng sinh bù cho các câu còn thiếu sau lượt chính (rate-limit thoáng qua,
# mạng chập chờn). Chỉ sinh lại ĐÚNG câu chưa có file nên rất nhanh.
_VOICE_REPAIR_ROUNDS = 2


# TẬN DỤNG KHOẢNG LẶNG SAU MỖI CÂU (chế độ Cơ bản) — người dùng yêu cầu 24-09-2026.
#
# Trước đây khung của giọng = đúng [start, end] của câu sub. Giọng dài hơn khung một chút
# là bị TĂNG TỐC (hoặc cắt) dù ngay sau câu còn cả khoảng im lặng tới câu kế. Nay khung
# được nới tới ĐIỂM BẮT ĐẦU CỦA CÂU KẾ (câu cuối: tới hết video) TRƯỚC, rồi mới tăng tốc
# nếu vẫn không vừa — nên chỉ những câu thật sự chật mới phải đọc nhanh.
#
# BẤT BIẾN: KHÔNG BAO GIỜ đổi điểm BẮT ĐẦU — giọng phải khởi đúng lúc nhân vật mở miệng,
# lệch là lệch cả khung hình. Chỉ ĐIỂM KẾT THÚC được kéo dài, và chỉ dài đúng bằng giọng
# thật (sub hiện hết lúc giọng đọc; bước ghép dùng [start, end] để hạ nhạc nền / giọng
# gốc nên end phải theo giọng, không thì đoạn kéo dài bị nhạc đè). Sát câu kế là được —
# người dùng chốt không cần chừa khoảng nghỉ.
#
# Không đụng tới HÌNH: bước ghép vẫn ``-c:v copy`` (Cơ bản phải giữ nhẹ).
def khung_toi_da_cua_cau(ds: list, do_dai_video: float) -> dict:
    """``ds`` = [(id, start, end)] của MỌI câu trong video. Trả {id: mốc kết thúc TỐI ĐA}
    = điểm bắt đầu của câu kế (bỏ qua câu trùng điểm bắt đầu), câu cuối = hết video
    (chưa biết độ dài video thì giữ end cũ). Không bao giờ nhỏ hơn end gốc — câu đang
    chồng lên câu sau thì giữ nguyên khung."""
    xep = sorted(ds, key=lambda x: (float(x[1]), x[0]))
    ra = {}
    for i, (sid, st, en) in enumerate(xep):
        ke = next((float(t[1]) for t in xep[i + 1:] if float(t[1]) > float(st)), None)
        tran = ke if ke is not None else (float(do_dai_video) if do_dai_video and do_dai_video > 0 else float(en))
        ra[sid] = max(float(en), tran)
    return ra


def diem_ket_thuc_moi(start: float, end: float, moc_toi_da: float, do_dai_giong: float) -> float:
    """Kéo ``end`` ra cho vừa giọng thật, không quá ``moc_toi_da``. Giọng ngắn hơn khung
    gốc thì GIỮ end gốc (không co sub lại)."""
    if not do_dai_giong or do_dai_giong <= 0:
        return end
    return max(float(end), min(float(start) + float(do_dai_giong), float(moc_toi_da)))


def _tran_cua_video(video) -> float:
    """Số giây giọng được TRÀN qua khung của câu (Cơ bản) — đọc hai cột ``cho_tran_giong`` / ``tran_giong`` của bảng
    videos qua đúng một luật với Chi tiết (``TL.tran_giong_cho_phep``). Cột NULL (DB cũ) = mặc định bật 1,5 s."""
    from services import review_timeline as TL

    m = {}
    if getattr(video, "cho_tran_giong", None) is not None:
        m[TL.TRAN_BAT_FIELD] = bool(video.cho_tran_giong)
    if getattr(video, "tran_giong", None) is not None:
        m[TL.TRAN_GIAY_FIELD] = video.tran_giong
    return TL.tran_giong_cho_phep(m)


#: Âm lượng giọng đọc AI mặc định ở Cơ bản (150%) — bằng con số ``volume=1.5`` gắn cứng trước đây trong merge, và bằng mặc định
#: ``tts_volume`` của Chi tiết; kẹp 0–2 (thang 0–200% dùng chung).
TTS_VOLUME_MAC_DINH = 1.5


def _tts_volume_cua_video(video) -> float:
    v = getattr(video, "tts_volume", None)
    if v is None:
        return TTS_VOLUME_MAC_DINH
    try:
        return max(0.0, min(2.0, float(v)))
    except (TypeError, ValueError):
        return TTS_VOLUME_MAC_DINH


def bo_noi_khung(seg) -> None:
    """Giọng của câu bị XOÁ (dịch lại / sửa chữ / đổi giọng / đổi tốc độ) -> trả end về
    mốc gốc. Để nguyên thì khung còn nới mà không có giọng: nhạc nền vẫn bị hạ suốt đoạn
    nới, huy hiệu vẫn đếm câu đó; sinh giọng lỗi thì kẹt luôn (rà soát 29-09-2026)."""
    if seg.goc_end_time is not None:
        seg.end_time = seg.goc_end_time
        seg.goc_end_time = None


def cap_nhat_end_sau_giong(seg, moc_toi_da: float, do_dai_giong: float) -> float:
    """Cập nhật ``end_time`` của MỘT câu sau khi sinh giọng, tính từ mốc kết thúc GỐC
    (``goc_end_time`` nếu đã từng nới, không thì ``end_time``):
      · giọng dài hơn khung gốc -> nới end (không quá ``moc_toi_da``), nhớ mốc gốc MỘT lần;
      · giọng vừa khung gốc mà câu đang bị nới -> co end về mốc gốc, bỏ dấu nới.
    Không bao giờ đụng ``start_time``. Trả về số giây đã nới thêm so với mốc gốc (0 nếu
    không nới); -1 nếu vừa co về mốc gốc."""
    goc = seg.goc_end_time if seg.goc_end_time is not None else seg.end_time
    if getattr(seg, "giu_khung_goc", None):
        moc_toi_da = goc            # người dùng đã chọn giữ khung gốc -> không nới
    moi = diem_ket_thuc_moi(seg.start_time, goc, moc_toi_da, do_dai_giong)
    if moi > goc + 0.001:
        if seg.goc_end_time is None:
            seg.goc_end_time = seg.end_time
        seg.end_time = round(moi, 3)
        return moi - goc
    if seg.goc_end_time is not None:
        seg.end_time = seg.goc_end_time
        seg.goc_end_time = None
        return -1.0
    return 0.0


class _PipelineCancelled(Exception):
    """Raise khi người dùng bấm Hủy — bước đang chạy dừng sớm, KHÔNG coi là lỗi."""


#  Lượt xuất video dựng hình đang chạy theo video: video_id -> threading.Event (cờ hủy).
_XUAT_DANG_CHAY: dict = {}
_XUAT_KHOA = threading.Lock()


def _request_cancel(video_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_REQUESTED.add(video_id)


def _clear_cancel(video_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_REQUESTED.discard(video_id)


def _is_cancel_requested(video_id: str) -> bool:
    with _CANCEL_LOCK:
        return video_id in _CANCEL_REQUESTED


def _check_cancel(video_id: str) -> None:
    if _is_cancel_requested(video_id):
        raise _PipelineCancelled()


# Guard chống CHẠY CHỒNG theo video: mỗi video chỉ 1 bước pipeline nặng chạy 1 lúc.
# Dùng CHUNG cơ chế _acquire_step/_release_step (đã có sẵn cho tts/merge) để mọi bước
# (extract/stt/translate/tts/merge) loại trừ lẫn nhau -> tránh 2 tiến trình cùng ghi
# SQLite gây "database is locked" (vd bấm Hủy STT chưa dừng hẳn rồi chạy lại).
def _acquire_or_409(video_id: str, step: str) -> None:
    """Chiếm slot xử lý cho video (bước `step`); nếu đang bận -> raise 409 rõ ràng."""
    if not _acquire_step(video_id, step):
        raise HTTPException(
            status_code=409,
            detail=L("Video này đang chạy một tác vụ khác. Vui lòng đợi hoàn tất, hoặc bấm Hủy và chờ dừng hẳn trước khi chạy lại.", "This video is running another task. Please wait for it to finish, or click Cancel and wait for it to fully stop before running again."),
        )


# Security Config (Disabled by user request)
config_service = get_config_service()


def get_data_root(db: Session = None):
    # We now primarily use config.json for roots
    path = config_service.get_dubbing_root()
    try:
        if path and not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        return path
    except Exception as e:
        logger.warning(f"[SYSTEM] Không thể truy cập thư mục lưu trữ cấu hình '{path}': {e}. Đang tự động chuyển sang thư mục ổ C mặc định.")
        fallback_path = "C:/SubForge_Workstation_Data"
        os.makedirs(fallback_path, exist_ok=True)
        return fallback_path


def scan_data_root(db: Session, root: str):
    """Scan root for subfolders containing project_state.json and import them to DB"""
    import json

    for item in os.listdir(root):
        item_path = os.path.join(root, item)
        if not os.path.isdir(item_path):
            continue
        state_file = os.path.join(item_path, "project_state.json")
        if os.path.exists(state_file):
            # Check if already in DB
            existing = db.query(Video).filter(Video.id == item).first()
            if not existing:
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    new_v = Video(
                        id=data["id"],
                        title=data["title"],
                        original_path=data["original_path"],
                        status=data["status"],
                        p1_status=data.get("p1_status"),
                        p2_status=data.get("p2_status"),
                        p3_status=data.get("p3_status"),
                        p4_status=data.get("p4_status"),
                        p5_status=data.get("p5_status"),
                        source_lang=data.get("source_lang", "zh-cn"),
                        target_lang=data.get("target_lang", "vi"),
                        voice_talent=data.get("voice_talent", "vi-VN-NamMinhNeural"),
                        created_at=data["created_at"],
                    )
                    db.add(new_v)
                    db.commit()

                    for s in data["segments"]:
                        new_s = SubtitleSegment(
                            video_id=new_v.id,
                            index=s["index"],
                            start_time=s["start_time"],
                            end_time=s["end_time"],
                            source_text=s["source_text"],
                            target_text=s["target_text"],
                            audio_path=s.get("audio_path"),
                            goc_end_time=s.get("goc_end_time"),
                            giu_khung_goc=s.get("giu_khung_goc"),
                        )
                        db.add(new_s)
                    db.commit()
                except Exception:
                    continue


def get_project_dirs(video_id: str, db: Session):
    """Derive project directory from video ID in the configured data root"""
    root = get_data_root(db)
    project_root = os.path.join(root, video_id)

    dirs = {
        "root": project_root,
        "audio": os.path.join(project_root, "audio"),
        "segments": os.path.join(project_root, "audio", "segments"),
        "output": os.path.join(project_root, "output"),
    }

    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    return dirs


def sync_project_to_disk(video_id: str, db: Session):
    """Serialize project metadata and segments to a local JSON file for portability"""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        return

    segments = (
        db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).all()
    )
    dirs = get_project_dirs(video_id, db)

    state = {
        "id": video.id,
        "title": video.title,
        "original_path": video.original_path,
        "status": video.status,
        "p1_status": video.p1_status,
        "p2_status": video.p2_status,
        "p3_status": video.p3_status,
        "p4_status": video.p4_status,
        "p5_status": video.p5_status,
        "source_lang": video.source_lang,
        "target_lang": video.target_lang,
        "voice_talent": video.voice_talent,
        "created_at": video.created_at,
        "segments": [
            {
                "id": s.id,
                "index": s.index,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "source_text": s.source_text,
                "target_text": s.target_text,
                "audio_path": s.audio_path,
                "goc_end_time": s.goc_end_time,
                "giu_khung_goc": s.giu_khung_goc,
            }
            for s in segments
        ],
    }

    with open(
        os.path.join(dirs["root"], "project_state.json"), "w", encoding="utf-8"
    ) as f:
        import json

        json.dump(state, f, ensure_ascii=False, indent=4)


def get_dir_size(path):
    """Calculate total size of a directory in bytes."""
    total_size = 0
    if not path or not os.path.exists(path):
        return 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
    except:
        pass
    return total_size


@router.get("/settings/all-roots")
def get_all_roots():
    config = config_service.load_config()
    config["dubbing_size"] = get_dir_size(config.get("dubbing_root"))
    config["review_size"] = get_dir_size(config.get("review_root"))
    return config


# (Đã gỡ /update-dubbing-root & /update-review-root — vị trí lưu chọn 1 lần
# lúc cài, mọi dữ liệu nằm chung trong data root; không hỗ trợ đổi chỗ nữa.)


# ---------------------------------------------------------------- BỘ AI (DeepSeek / Gemini) — cài đặt chung
@router.get("/settings/ai")
async def get_ai_settings():
    """Cấu hình MỌI bộ AI cho FE (không trả khoá thô; ``key_hint`` = khoá che bớt để hiện như phần Bản quyền)."""
    return config_service.get_ai_config()


@router.post("/update-ai")
async def update_ai_settings(data: dict):
    """Lưu khoá / model mặc định của một bộ AI (``provider``), bộ AI mặc định (``default_provider``), tuỳ chọn kịch bản AI (``ai_script``)."""
    from services.ai_engines import PROVIDERS, DEEPSEEK, GEMINI

    provider = str(data.get("provider") or "").strip()
    api_key = data.get("api_key")
    default_model = data.get("default_model")
    if provider and provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Bộ AI không hợp lệ: {provider}")
    if provider and (api_key is not None or default_model is not None):
        if api_key:
            import services.deepseek_service as _DS

            _DS.xoa_su_co(provider=provider)                                   # đổi key = người dùng đang xử lý sự cố của bộ đó
        if provider == GEMINI:
            config_service.update_gemini_config(api_key=api_key, default_model=default_model)
        elif provider == DEEPSEEK:
            config_service.update_deepseek_config(api_key=api_key, default_model=default_model)
    dp = str(data.get("default_provider") or "").strip()
    if dp:
        try:
            config_service.update_ai_provider(dp)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    ai = data.get("ai_script")
    if isinstance(ai, dict):
        config_service.update_ai_script_options(
            context_enabled=ai.get("context_enabled"), context_model=ai.get("context_model"),
            batch_size=ai.get("batch_size"), summary_level=ai.get("summary_level"),
        )
    return {"status": "success", **config_service.get_ai_config()}


@router.get("/settings/ai/models")
async def get_ai_live_models(provider: str = "gemini"):
    """Model CHỮ mà KHOÁ ĐANG LƯU của bộ AI thấy (Gemini: ``/models`` — Google ra model mới liên tục, không ghi cứng hết).
    Lỗi mạng / khoá → ``ok=False`` kèm lý do; FE vẫn còn danh sách khai sẵn."""
    from fastapi.concurrency import run_in_threadpool
    from services.ai_engines import GEMINI

    if provider != GEMINI:
        cfg = config_service.get_provider_config(provider)
        return {"ok": True, "models": cfg.get("models", []), "live": False}
    cfg = config_service.get_gemini_config()
    if not cfg.get("api_key"):
        return {"ok": False, "models": cfg.get("models", []), "live": False, "message": L("Chưa có API key Gemini.", "No Gemini API key yet.")}

    def _lay():
        import urllib.error

        from services import gemini_service as GS

        try:
            return {"ok": True, "live": True, "models": GS.model_chu_cua_khoa(cfg["api_key"], cfg.get("base_url") or "")}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:500]
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "live": False, "models": cfg.get("models", []), "message": GS.phan_loai_loi(e.code, body)[0]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "live": False, "models": cfg.get("models", []), "message": f"Không lấy được danh sách model: {e}"}

    return await run_in_threadpool(_lay)


@router.post("/settings/ai/test")
async def test_ai_key(data: dict = None):
    """KIỂM TRA khoá của một bộ AI (``provider``) có dùng được thật không — như ``/settings/deepseek/test`` nhưng cho cả Gemini.
    Gemini: (1) ``GET /models`` để xác nhận khoá + xem model đã chọn còn tồn tại với khoá đó; (2) một lượt chat cực ngắn (không suy nghĩ)."""
    from fastapi.concurrency import run_in_threadpool
    from services.ai_engines import GEMINI

    data = data or {}
    provider = str(data.get("provider") or "deepseek").strip()
    if provider != GEMINI:
        return await test_deepseek_key(data)
    key = str(data.get("api_key") or "").strip()
    model = str(data.get("model") or "").strip()
    cfg = config_service.get_gemini_config()
    model = model or cfg.get("default_model")
    if not key:
        key = cfg.get("api_key") or ""
    if not key:
        return {"ok": False, "reason": "chua_co_key", "message": L("Chưa có API key. Nhập key rồi bấm kiểm tra lại.", "No API key yet. Enter the key and click check again.")}
    la_key_dang_luu = key == (cfg.get("api_key") or "")

    def _thu() -> dict:
        import json as _json
        import urllib.error
        import urllib.request

        from services import gemini_service as GS
        import services.deepseek_service as _DS

        def _loi(ma: int, body: str) -> dict:
            noi, loai, _cach = GS.phan_loai_loi(ma, body, model)
            if la_key_dang_luu and loai:
                _DS.ghi_su_co(loai, noi, provider=GEMINI)
            return {"ok": False, "reason": f"http_{ma}", "message": noi}

        try:
            co = GS.model_chu_cua_khoa(key, cfg.get("base_url") or "")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:500]
            except Exception:  # noqa: BLE001
                pass
            return _loi(e.code, body)
        except urllib.error.URLError as e:
            return {"ok": False, "reason": "mang", "message": f"Không nối được tới generativelanguage.googleapis.com ({e.reason}). Kiểm tra mạng hoặc tường lửa."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": "la", "message": L(f"Lỗi lạ: {e}", f"Unexpected error: {e}")}
        canh_bao = "" if (not co or model in co) else f" Lưu ý: khoá này không thấy model '{model}' — hãy chọn model khác trong danh sách."
        body = _json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8, "stream": False,
                            "reasoning_effort": "none"}).encode("utf-8")
        req = urllib.request.Request(f"{(cfg.get('base_url') or GS.BASE_URL_MAC_DINH).rstrip('/')}/chat/completions", data=body,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "SubForge-Desktop-Client"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                res.read(512)
        except urllib.error.HTTPError as e:
            body_l = ""
            try:
                body_l = e.read().decode("utf-8", "ignore")[:500]
            except Exception:  # noqa: BLE001
                pass
            noi, loai, cach = GS.phan_loai_loi(e.code, body_l, model)
            if cach == "ha_suy_nghi":                                              # model không nhận reasoning_effort: khoá vẫn dùng được
                pass
            elif cach == "cho_429":
                return {"ok": True, "reason": "ok_429", "model": model, "models": co,
                        "message": f"API key dùng được (khoá thấy {len(co)} model chữ) nhưng đang bị giới hạn tần suất: {noi}{canh_bao}"}
            else:
                return _loi(e.code, body_l)
        except urllib.error.URLError as e:
            return {"ok": False, "reason": "mang", "message": f"Không nối được tới generativelanguage.googleapis.com ({e.reason})."}
        if la_key_dang_luu:
            _DS.xoa_su_co(provider=GEMINI)
        return {"ok": True, "reason": "ok", "model": model, "models": co,
                "message": f"API key dùng được (model {model}; khoá thấy {len(co)} model chữ).{canh_bao}"}

    return await run_in_threadpool(_thu)


@router.get("/settings/deepseek")
async def get_deepseek_settings():
    """Cấu hình DeepSeek cho FE (KHÔNG trả API key thô, chỉ báo đã có hay chưa)."""
    cfg = config_service.get_deepseek_config()
    return {
        "base_url": cfg.get("base_url"),
        "models": cfg.get("models", []),
        "default_model": cfg.get("default_model"),
        "has_api_key": bool(cfg.get("api_key")),
        # Tuỳ chọn tạo kịch bản AI (Chi tiết + chạy lô): đọc ngữ cảnh + số câu mỗi lô.
        "ai_script": config_service.get_ai_script_options(),
    }


@router.get("/settings/deepseek/su-co")
async def get_deepseek_su_co():
    """Sự cố API key DeepSeek còn hiệu lực (hết tiền / key sai…) cho POPUP toàn cục."""
    import services.deepseek_service as DS

    return {"su_co": DS.doc_su_co()}


@router.post("/settings/deepseek/su-co/da-xem")
async def dismiss_deepseek_su_co():
    """Người dùng bấm "Để sau": 30 phút không bật lại (trừ khi đổi loại sự cố)."""
    import services.deepseek_service as DS

    DS.da_xem_su_co()
    return {"ok": True}


@router.post("/settings/deepseek/su-co/bo-qua")
async def bo_qua_deepseek_su_co():
    """Người dùng bấm "Không nhắc nữa": XOÁ hẳn bản ghi sự cố — đường lui cho người đã thôi dùng bộ AI đó (góp ý 07-10-2026:
    không dùng nữa mà cứ 30 phút lại nhắc là không ổn). Lần tới có LỖI MỚI (họ thật sự gọi lại bộ AI đó) thì popup hiện lại."""
    import services.deepseek_service as DS

    DS.xoa_su_co()
    return {"ok": True}


@router.post("/update-deepseek")
async def update_deepseek_settings(data: dict):
    """Lưu API key / model mặc định do người dùng nhập (vào config.json)."""
    api_key = data.get("api_key")
    if api_key:
        #  Đổi key = người dùng đang xử lý sự cố -> tắt popup (key mới hỏng thì lần gọi
        #  sau tự ghi lại).
        import services.deepseek_service as _DS

        _DS.xoa_su_co()
    default_model = data.get("default_model")
    cfg = config_service.update_deepseek_config(
        api_key=api_key if api_key is not None else None,
        default_model=default_model if default_model is not None else None,
    )
    ai = data.get("ai_script")
    if isinstance(ai, dict):
        config_service.update_ai_script_options(
            context_enabled=ai.get("context_enabled"),
            context_model=ai.get("context_model"),
            batch_size=ai.get("batch_size"),
            summary_level=ai.get("summary_level"),
        )
    return {
        "status": "success",
        "base_url": cfg.get("base_url"),
        "models": cfg.get("models", []),
        "default_model": cfg.get("default_model"),
        "has_api_key": bool(cfg.get("api_key")),
        "ai_script": config_service.get_ai_script_options(),
    }


@router.post("/settings/deepseek/test")
async def test_deepseek_key(data: dict = None):
    """KIỂM TRA API key DeepSeek có dùng được thật không.

    Vì sao cần: từ trước tới nay nhập key xong chỉ có chữ "Đã lưu" — key sai, hết tiền
    hay chặn mạng thì mãi tới lúc bấm Dịch giữa một lô 20 video mới lộ ra. Ở đây gọi
    thử MỘT lượt chat cực ngắn rồi nói thẳng kết quả.

    Nhận ``api_key`` trong body để thử NGAY key vừa gõ mà chưa cần lưu; không gửi thì
    thử key đang lưu.
    """
    from fastapi.concurrency import run_in_threadpool

    data = data or {}
    key = str(data.get("api_key") or "").strip()
    model = str(data.get("model") or "").strip()
    cfg = config_service.get_deepseek_config()
    model = model or cfg.get("default_model")
    if not key:
        key = cfg.get("api_key") or ""
    if not key:
        return {
            "ok": False,
            "reason": "chua_co_key",
            "message": L("Chưa có API key. Nhập key rồi bấm kiểm tra lại.", "No API key yet. Enter the key and click check again."),
        }

    def _thu() -> dict:
        import json as _json
        import urllib.error
        import urllib.request

        body = _json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{cfg.get('base_url')}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "SubForge-Desktop-Client",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                res.read(512)
            #  Thử KEY ĐANG LƯU thành công -> sự cố cũ hết hiệu lực.
            if key == (cfg.get("api_key") or ""):
                import services.deepseek_service as _DS

                _DS.xoa_su_co()
            return {
                "ok": True,
                "reason": "ok",
                "model": model,
                "message": f"API key dùng được (model {model}).",
            }
        except urllib.error.HTTPError as e:
            ma = e.code
            #  Nói ĐÚNG NGUYÊN NHÂN: ba lỗi này người dùng xử lý ba cách hoàn toàn
            #  khác nhau, gộp thành "lỗi API" là bắt họ đoán.
            noi = {
                401: L("API key sai hoặc đã bị thu hồi.", "The API key is wrong or has been revoked."),
                402: L("Tài khoản DeepSeek hết số dư — nạp thêm rồi thử lại.", "The DeepSeek account is out of balance — top up and try again."),
                403: L("Key không có quyền dùng model này.", "The key has no permission to use this model."),
                429: L("Đang bị giới hạn tần suất — đợi một lát rồi thử lại.", "Rate limited — wait a moment and try again."),
            }.get(ma)
            if key == (cfg.get("api_key") or "") and ma in (401, 402, 403):
                import services.deepseek_service as _DS

                _DS.ghi_su_co(ma, noi or "")
            if not noi:
                noi = (
                    L(f"Máy chủ DeepSeek trả lỗi {ma}.", f"The DeepSeek server returned error {ma}.")
                    if ma < 500
                    else L(f"Máy chủ DeepSeek đang trục trặc (lỗi {ma}) — thử lại sau.", f"The DeepSeek server is having trouble (error {ma}) — try again later.")
                )
            return {"ok": False, "reason": f"http_{ma}", "message": noi}
        except urllib.error.URLError as e:
            return {
                "ok": False,
                "reason": "mang",
                "message": L(f"Không nối được tới api.deepseek.com ({e.reason}). "
                             "Kiểm tra mạng hoặc tường lửa.",
                             f"Cannot connect to api.deepseek.com ({e.reason}). "
                             "Check your network or firewall."),
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": "la", "message": L(f"Lỗi lạ: {e}", f"Unexpected error: {e}")}

    return await run_in_threadpool(_thu)


@router.get("/settings/translate-limit")
async def get_translate_limit_setting():
    """Mức trần ký tự bản dịch (cài đặt CHUNG cho cả app)."""
    return {"mode": config_service.get_translate_limit_mode()}


@router.post("/settings/translate-limit")
async def update_translate_limit_setting(data: dict):
    """Đổi mức trần ký tự cho MỌI chế độ (cơ bản, chi tiết, chạy lô).

    Một công tắc chung chứ không phải mỗi nơi một cờ: prompt do backend dựng, mà
    cùng một người dùng thì kỳ vọng dịch ở đâu cũng ra cùng kiểu — hai ba chỗ đặt
    riêng là sớm muộn copy prompt ở dự án một kiểu, lô chạy ra một kiểu khác.
    """
    return {"mode": config_service.update_translate_limit_mode(data.get("mode"))}


@router.get("/settings/subtitle-quality")
async def get_subtitle_quality_setting():
    """Chất lượng lấy phụ đề (OCR + Whisper, mọi chế độ): "nhanh" | "chinh_xac"."""
    return {"value": config_service.get_subtitle_quality()}


@router.post("/settings/subtitle-quality")
async def update_subtitle_quality_setting(data: dict):
    return {"value": config_service.update_subtitle_quality((data or {}).get("value"))}


@router.get("/settings/gop-manh-ngan")
async def get_gop_manh_ngan_setting():
    """Ô tick "Gộp mẩu sub ngắn" (lấy sub gốc, mọi chế độ) — thử nghiệm, mặc định tắt."""
    return {"enabled": config_service.get_gop_manh_ngan()}


@router.post("/settings/gop-manh-ngan")
async def update_gop_manh_ngan_setting(data: dict):
    return {"enabled": config_service.update_gop_manh_ngan(bool((data or {}).get("enabled")))}


@router.get("/settings/hw-decode")
async def get_hw_decode_setting():
    """Công tắc "Tăng tốc bằng GPU tích hợp" (giải mã video bằng GPU khi xuất) + card
    màn hình của máy để giao diện biết có nên hiện công tắc không."""
    from utils.hardware_utils import get_igpu_info, list_display_adapters

    adapters = await run_in_threadpool(list_display_adapters)
    return {"enabled": config_service.get_hw_decode(), "adapters": adapters,
            "igpu": get_igpu_info()}


@router.post("/settings/hw-decode")
async def update_hw_decode_setting(data: dict):
    return {"enabled": config_service.update_hw_decode(bool(data.get("enabled")))}


@router.get("/settings/onboarding")
async def get_onboarding_setting():
    """Popup gợi ý bắt đầu cho người mới: đã xem chưa (+ câu trả lời đã chọn)."""
    return config_service.get_onboarding()


@router.post("/settings/onboarding")
async def update_onboarding_setting(data: dict):
    return config_service.update_onboarding(bool(data.get("done", True)), data.get("profile"))


@router.get("/settings/huong-dan")
async def get_huong_dan_setting():
    """Đã xem qua hướng dẫn từng bước chưa (popup giới thiệu ở Trang chủ)."""
    return config_service.get_huong_dan()


@router.post("/settings/huong-dan")
async def update_huong_dan_setting(data: dict):
    return config_service.update_huong_dan(bool(data.get("da_xem", True)))


@router.get("/settings/favorite-voices")
async def get_favorite_voices_settings():
    """Lấy danh sách ID giọng đọc yêu thích đã lưu trong config.json."""
    return {"voices": config_service.get_favorite_voices()}


@router.post("/settings/favorite-voices")
async def update_favorite_voices_settings(data: dict):
    """Cập nhật danh sách ID giọng đọc yêu thích vào config.json."""
    voices = data.get("voices", [])
    updated = config_service.update_favorite_voices(voices)
    return {"status": "success", "voices": updated}


@router.get("/supported-local-pairs")
async def get_supported_local_pairs():
    """Get list of supported local translation pairs."""
    try:
        pairs = translation_service.get_supported_local_pairs()
        return {"supported_pairs": pairs}
    except Exception as e:
        logger.error(f"Lỗi khi lấy danh sách cặp ngôn ngữ dịch: {e}")
        return {"supported_pairs": []}


from fastapi.concurrency import run_in_threadpool

@router.post("/open-file-dialog")
async def open_file_dialog():
    # Tkinter qua thread chuyên trách (utils.native_dialogs) — tạo Tk() trực tiếp
    # trong threadpool gây "main thread is not in main loop" ở lần gọi sau.
    from utils.native_dialogs import ask_open_filename

    def get_path():
        return ask_open_filename(
            title=L("Chọn Video cho SubForge", "Choose a Video for SubForge"),
            filetypes=[("Video files", "*.mp4 *.avi *.mkv *.mov *.flv *.wmv *.webm"), ("All files", "*.*")]
        )

    try:
        selected_path = await run_in_threadpool(get_path)
        return {"path": selected_path}
    except Exception as e:
        logger.error(f"Lỗi khi mở hộp thoại chọn tệp: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/open-directory-dialog")
async def open_directory_dialog():
    from utils.native_dialogs import ask_directory

    def get_dir():
        return ask_directory(title=L("Chọn Thư Mục Lưu Trữ", "Choose the Storage Folder"))

    try:
        selected_path = await run_in_threadpool(get_dir)
        return {"path": selected_path}
    except Exception as e:
        logger.error(f"Lỗi khi mở hộp thoại chọn thư mục: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/file")
async def get_local_file(path: str):
    path = os.path.normpath(path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Không tìm thấy tệp: {path}")
    return FileResponse(path)


def _goc_duoc_xoa() -> list:
    """Những thư mục mà ``DELETE /videos/file`` được phép xoá tệp bên trong.

    Chỉ là thư mục dữ liệu app người dùng đã chọn (data root) cùng hai gốc
    dubbing/review trong config.json — bình thường hai gốc này nằm trong data
    root, nhưng config cũ có thể còn trỏ chỗ khác. KHÔNG gồm thư mục AppData
    (config.json, DB) và cũng không gồm thư mục xuất người dùng chọn ngoài data
    root: hiện không có màn nào gọi xoá tệp ở đó.
    """
    from utils.storage_paths import get_data_root as _data_root

    goc = []
    for lay in (_data_root, config_service.get_dubbing_root, config_service.get_review_root):
        try:
            p = lay()
        except Exception:
            continue
        if p:
            goc.append(os.path.normcase(os.path.realpath(p)))
    return goc


def _duoc_phep_xoa(path: str) -> bool:
    """Tệp phải nằm HẲN bên trong một gốc được phép, tính trên đường dẫn thật
    (realpath giải ``..``, symlink, junction) — không phải trên chuỗi client gửi.
    Chính thư mục gốc cũng không được xoá."""
    if not path:
        return False
    try:
        dich = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return False
    for goc in _goc_duoc_xoa():
        try:
            if dich != goc and os.path.commonpath([goc, dich]) == goc:
                return True
        except ValueError:  # khác ổ đĩa
            continue
    return False


@router.delete("/file")
async def delete_file(path: str):
    path = os.path.normpath(path)
    if not _duoc_phep_xoa(path):
        logger.warning(f"[SECURITY] Chặn xoá tệp ngoài thư mục dữ liệu app: {path}")
        raise HTTPException(
            status_code=403,
            detail=L("Không được xoá tệp nằm ngoài thư mục dữ liệu của app.", "Files outside the app data folder cannot be deleted."),
        )
    try:
        if os.path.exists(path):
            os.remove(path)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/demo")
def tao_du_an_mau_co_ban(dung_lai: bool = False, db: Session = Depends(get_db)):
    """DỰ ÁN MẪU Cơ bản (services/du_an_mau.py): video ~23 giây có sẵn trong bộ cài, cấu hình
    SẴN để chạy trọn luồng không cần tải model — OCR + Google Dịch / Copy prompt + giọng
    Edge. Trả kèm vùng phụ đề + nguồn phụ đề để giao diện ghi vào bộ nhớ theo video (hai
    thứ đó chỉ sống ở giao diện)."""
    from services import du_an_mau as DM

    if dung_lai:
        cu = (db.query(Video).filter(Video.title == DM.TIEU_DE)
              .order_by(Video.created_at.desc()).first())
        if cu and os.path.isfile(cu.original_path or ""):
            return {"id": cu.id, "title": DM.TIEU_DE, "ocr_region": DM.VUNG_SUB,
                    "submode": "ocr", "dung_lai": True}
    try:
        path = DM.chep_vao(os.path.join(get_data_root(db), "mau"))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    video_id = str(uuid.uuid4())
    goc = get_project_dirs(video_id, db)["root"]
    db_video = Video(
        id=video_id,
        title=DM.TIEU_DE,
        original_path=path,
        duration=VideoService.get_video_duration(path),
        status="uploaded",
        p1_status="pending",
        p2_status="pending",
        p3_status="pending",
        p4_status="pending",
        p5_status="pending",
        created_at=time.time(),
        source_lang=DM.NGON_NGU_NGUON,
        target_lang=DM.NGON_NGU_DICH,
        voice_talent=DM.GIONG_EDGE,
        translation_engine="google",
    )
    db.add(db_video)
    db.commit()
    db.refresh(db_video)
    #  Nguồn phụ đề OCR đi đường "lựa chọn giao diện của dự án mới" có sẵn (VideoDetail
    #  đọc một lần khi video chưa có lựa chọn riêng).
    os.makedirs(goc, exist_ok=True)
    with open(os.path.join(goc, MAC_DINH_GIAO_DIEN_TEP), "w", encoding="utf-8") as f:
        json.dump({"submode": "ocr", "ocr_region": DM.VUNG_SUB}, f, ensure_ascii=False)
    sync_project_to_disk(video_id, db)
    return {"id": video_id, "title": DM.TIEU_DE, "ocr_region": DM.VUNG_SUB, "submode": "ocr"}


@router.post("/import-path")
def import_by_path(data: dict, db: Session = Depends(get_db)):
    path = data.get("path", "").strip().strip('"')
    path = os.path.normpath(path)

    if not path or not os.path.exists(path):
        raise HTTPException(status_code=400, detail=f"Đường dẫn không hợp lệ: {path}")

    video_id = str(uuid.uuid4())
    get_project_dirs(video_id, db)

    filename = os.path.basename(path)
    duration = VideoService.get_video_duration(path)

    db_video = Video(
        id=video_id,
        title=filename,
        original_path=path,
        duration=duration,
        status="uploaded",
        p1_status="pending",
        p2_status="pending",
        p3_status="pending",
        p4_status="pending",
        p5_status="pending",
        created_at=time.time(),
    )
    _ap_mac_dinh_co_ban(db_video, db)
    db.add(db_video)
    db.commit()
    db.refresh(db_video)
    sync_project_to_disk(video_id, db)
    return db_video


@router.get("/douyin-cookie")
def get_douyin_cookie_status():
    """Trạng thái cookie Douyin đã lưu (để UI hiện đã có/chưa)."""
    from services.download_service import has_cookies

    return {"has_cookie": has_cookies()}


@router.post("/douyin-cookie")
def set_douyin_cookie(data: dict):
    """Lưu nội dung cookies.txt người dùng dán vào."""
    from services.download_service import save_cookies

    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail=L("Nội dung cookie trống.", "The cookie content is empty."))
    # Kiểm tra sơ bộ: là file cookies.txt Netscape (có ít nhất 1 dòng dữ liệu dạng
    # "domain <TAB> ... <TAB> name <TAB> value"), KHÔNG giới hạn nền tảng — chấp nhận
    # Douyin/TikTok/Bilibili... Chỉ chặn khi người dùng dán nhầm text linh tinh.
    import re as _re
    looks_like_cookie = any(
        len(_re.split(r"\t+|\s{2,}", ln.strip())) >= 6
        for ln in content.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    )
    if not looks_like_cookie:
        raise HTTPException(
            status_code=400,
            detail=(
                L("Nội dung không giống file cookies.txt (định dạng Netscape). "
                  "Hãy export bằng tiện ích 'Get cookies.txt LOCALLY' rồi dán lại.",
                  "The content does not look like a cookies.txt file (Netscape format). "
                  "Export it with the 'Get cookies.txt LOCALLY' extension and paste it again.")
            ),
        )
    save_cookies(content)
    return {"status": "success"}


@router.delete("/douyin-cookie")
def delete_douyin_cookie():
    from services.download_service import clear_cookies

    clear_cookies()
    return {"status": "success"}


@router.post("/probe-url")
async def probe_url(data: dict):
    """Quét thông tin video từ URL mà KHÔNG tải — để xem trước + xác nhận link hợp lệ.

    Trả 400 (kèm thông báo tiếng Việt) nếu link sai hoặc không đọc được, để client
    hiện lỗi inline + nút Thử lại.
    """
    from services.download_service import probe_video, normalize_public_url

    # Tự thêm https:// cho link thiếu scheme (vd "douyin.com/video/123").
    url = normalize_public_url(data.get("url") or "")
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail=L("Liên kết không hợp lệ.", "Invalid link."))

    try:
        info = await run_in_threadpool(probe_video, url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return info


@router.post("/probe-url-batch")
async def probe_url_batch(data: dict):
    """Quét NHANH metadata cho DANH SÁCH link dán tay (tiêu đề / thời lượng /
    thumbnail / kênh / lượt xem — không tính sizes). Chạy song song có giới hạn
    để không bị nền tảng chặn; link lỗi trả {"url", "error"} để UI vẫn hiển thị
    được các link còn lại."""
    import asyncio

    from services.download_service import probe_video_meta, normalize_public_url

    #  KHÔNG chặn số lượng: một hợp tuyển Douyin có thể trên trăm tập, mà chặn ở 100
    #  thì người dùng phải chia nhiều lượt dán — chính cái phiền mà tính năng này sinh ra
    #  để bỏ đi. Tải nặng vẫn được ghìm bằng ``Semaphore`` bên dưới (mỗi lúc 3 link),
    #  nên danh sách dài chỉ lâu hơn chứ không làm ngập nền tảng hay chết backend.
    urls = [
        normalize_public_url(u)
        for u in (data.get("urls") or [])
        if isinstance(u, str) and u.strip()
    ]
    if not urls:
        raise HTTPException(status_code=400, detail=L("Danh sách link trống.", "The link list is empty."))

    sem = asyncio.Semaphore(3)
    #  DOUYIN PHẢI ĐI MỘT HÀNG VÀ CÓ NGHỈ GIỮA CÁC LƯỢT.
    #  Đo thật với danh sách 103 link: chạy 3 luồng song song như các nền tảng khác thì
    #  8 link đầu lấy đủ thông tin rồi từ link thứ 9 trở đi lỗi liên tiếp — Douyin siết
    #  tần suất chứ không phải hỏng cookie. Mỗi lượt quét Douyin là một lượt tải trang
    #  chia sẻ, dồn dập là bị gắn cờ (nặng hơn thì dựng luôn captcha cho cả máy).
    #  Nền tảng khác giữ nguyên 3 luồng — chúng không bị vấn đề này.
    sem_dy = asyncio.Semaphore(1)
    NGHI_DOUYIN = 1.2

    def _la_douyin(u: str) -> bool:
        return "douyin.com" in (u or "").lower()

    async def probe_one(u: str) -> dict:
        dy = _la_douyin(u)
        async with (sem_dy if dy else sem):
            try:
                return await run_in_threadpool(probe_video_meta, u)
            except Exception as e:  # noqa: BLE001 — 1 link lỗi không hỏng cả lô
                return {"url": u, "error": str(e)[:200]}
            finally:
                #  Nghỉ trong lúc CÒN GIỮ chỗ, và nghỉ cả khi vừa lỗi — lỗi chính là lúc
                #  cần lùi lại nhất. Đặt sau ``async with`` thì lượt kế đã chen vào rồi.
                if dy:
                    await asyncio.sleep(NGHI_DOUYIN)

    results = await asyncio.gather(*[probe_one(u) for u in urls])
    return {"results": list(results)}


@router.post("/translate-titles")
async def translate_titles(data: dict):
    """Dịch danh sách TIÊU ĐỀ video sang tiếng Việt (giữ dấu) — để panel danh
    sách hiển thị tên đã dịch ngay trên giao diện trước khi tải. Tiêu đề lỗi
    dịch trả chuỗi rỗng ở đúng vị trí (client giữ tên gốc)."""
    import asyncio

    from services.download_service import translate_title_to_vi

    titles = [t for t in (data.get("titles") or []) if isinstance(t, str)][:100]
    if not titles:
        raise HTTPException(status_code=400, detail=L("Danh sách tiêu đề trống.", "The title list is empty."))

    sem = asyncio.Semaphore(3)

    async def tr(t: str) -> str:
        async with sem:
            return await run_in_threadpool(translate_title_to_vi, t)

    results = await asyncio.gather(*[tr(t) for t in titles])
    return {"results": list(results)}


@router.post("/import-url")
async def import_by_url(data: dict, db: Session = Depends(get_db)):
    """Tải video từ URL (Douyin/TikTok/YouTube/...).

    Douyin: yt-dlp lấy luồng gốc không watermark; header trình duyệt để vượt chặn bot.

    Tham số ``target`` quyết định đích đến:
      - "dubbing" (mặc định): tải + tạo Video row (như import-path) để lồng tiếng.
      - "review" / "save": chỉ tải file, trả ``{path, title, duration}`` để phía
        client tự tạo project review hoặc lưu máy — KHÔNG tạo Video row.
    """
    from services.download_service import download_video

    url = (data.get("url") or "").strip()
    target = (data.get("target") or "dubbing").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail=L("Liên kết không hợp lệ.", "Invalid link."))

    # Với dubbing, tải vào thư mục project của video mới. Với review (kể cả review
    # nâng cao) tải vào "downloads" trong thư mục REVIEW; save tải vào "downloads"
    # của thư mục dubbing — đều là thư mục con để không tạo project rác. Giữ khớp
    # với nhánh cùng chức năng trong /import-url/stream.
    video_id = str(uuid.uuid4())
    if target == "dubbing":
        dest_dir = get_project_dirs(video_id, db)["root"]
    elif target in ("review", "review-advanced"):
        dest_dir = os.path.join(config_service.get_review_root(), "downloads")
    else:
        dest_dir = os.path.join(get_data_root(db), "downloads")

    try:
        result = await run_in_threadpool(download_video, url, dest_dir)
    except ValueError as e:
        if target == "dubbing":
            try:
                shutil.rmtree(dest_dir, ignore_errors=True)
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))

    path = result["path"]
    # ffprobe trước; nếu ra 0 (BiliBili DASH đôi khi thiếu 'Duration' header) thì
    # fallback về duration từ metadata yt-dlp.
    duration = VideoService.get_video_duration(path) or result.get("duration") or 0

    if target != "dubbing":
        return {"path": path, "title": result["title"], "duration": duration}

    db_video = Video(
        id=video_id,
        title=result["title"],
        original_path=path,
        duration=duration,
        status="uploaded",
        p1_status="pending",
        p2_status="pending",
        p3_status="pending",
        p4_status="pending",
        p5_status="pending",
        created_at=time.time(),
    )
    _ap_mac_dinh_co_ban(db_video, db)
    db.add(db_video)
    db.commit()
    db.refresh(db_video)
    sync_project_to_disk(video_id, db)
    return db_video


@router.post("/import-url/stream")
async def import_by_url_stream(data: dict, request: Request):
    """Như /import-url nhưng STREAM tiến độ tải (SSE): %, dung lượng, tốc độ.

    Hỗ trợ HỦY: nếu client ngắt kết nối, backend set cancel_event -> yt-dlp dừng tải.
    Tham số ``quality``: best | 1080 | 720 | 480 | 360.

    Sự kiện: {type:"progress", percent, downloaded, total, speed}
             {type:"done", id?/path/title/duration/target}
             {type:"error", message} | {type:"cancelled"}
    """
    import asyncio
    import json
    import threading
    from queue import Queue, Empty
    from fastapi.responses import StreamingResponse
    from services.download_service import download_video, DownloadCancelled
    from models.database import SessionLocal

    url = (data.get("url") or "").strip()
    target = (data.get("target") or "dubbing").strip()
    quality = (data.get("quality") or "best").strip()
    mode = (data.get("mode") or "video").strip()  # "video" | "audio"
    # Lồng tiếng / Review cần VIDEO -> ép mode video (audio chỉ dùng khi lưu máy).
    if target in ("dubbing", "review", "review-advanced"):
        mode = "video"
    # Tổng dung lượng ước lượng (MB) để gộp tiến độ video+audio thành 1 thanh mượt.
    try:
        expected_bytes = int(float(data.get("expected_mb") or 0) * 1024 * 1024)
    except (TypeError, ValueError):
        expected_bytes = 0
    # Thư mục + tên file tùy chọn (dùng khi target="save", người dùng chọn qua Save As).
    custom_dir = (data.get("dest_dir") or "").strip()
    custom_name = (data.get("filename") or "").strip()
    # Dịch tiêu đề sang tiếng Việt (giữ dấu) — áp cho tên file (lưu máy) LẪN tên
    # dự án (lồng tiếng/review). title_override = tên FE đã dịch sẵn (ưu tiên
    # dùng, khỏi dịch lại); translate_title = yêu cầu backend tự dịch.
    translate_title = bool(data.get("translate_title"))
    title_override = (data.get("title_override") or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail=L("Liên kết không hợp lệ.", "Invalid link."))

    q: Queue = Queue()
    cancel_event = threading.Event()

    def worker():
        db = SessionLocal()
        dest_dir = None
        worker_t0 = time.time()

        def _cleanup_partial_files():
            """Dọn file tải dở (.part/.ytdl/.fNNN.*) CỦA ĐÚNG lượt tải này — lọc
            theo stem tên file người dùng đặt. Không biết stem (review/dán link
            không đặt tên) thì BỎ QUA: xoá theo mtime có thể trúng file .part của
            lượt tải song song khác đang ghi vào cùng thư mục."""
            if not dest_dir or not os.path.isdir(dest_dir):
                return
            stem = os.path.splitext(custom_name)[0].strip() if custom_name else ""
            if not stem:
                return
            import re as _re
            tmp_re = _re.compile(r"(\.part|\.ytdl|\.f\d+\.[a-z0-9]+)$", _re.IGNORECASE)
            try:
                for name in os.listdir(dest_dir):
                    if not name.startswith(stem) or not tmp_re.search(name):
                        continue
                    try:
                        os.remove(os.path.join(dest_dir, name))
                    except OSError:
                        pass
            except OSError:
                pass

        try:
            video_id = str(uuid.uuid4())
            if target == "dubbing":
                dest_dir = get_project_dirs(video_id, db)["root"]
            elif custom_dir and os.path.isdir(custom_dir):
                dest_dir = custom_dir  # người dùng tự chọn nơi lưu
            elif target in ("review", "review-advanced"):
                # Review + Review nâng cao (Edit chi tiết): lưu vào thư mục REVIEW
                # (không phải dubbing) — cả hai đều tạo dự án review từ đường dẫn
                # này (xem review_service.create_project: chỉ THAM CHIẾU video_path,
                # không copy) nên file phải nằm cạnh dữ liệu review.
                dest_dir = os.path.join(config_service.get_review_root(), "downloads")
            else:
                dest_dir = os.path.join(get_data_root(db), "downloads")

            def on_progress(p):
                q.put({"type": "progress", **p})

            try:
                result = download_video(url, dest_dir, on_progress=on_progress,
                                        cancel_event=cancel_event, quality=quality,
                                        filename=(custom_name if target != "dubbing" else ""),
                                        total_hint_bytes=expected_bytes, mode=mode,
                                        want_subtitles=(mode != "audio"),
                                        subtitle_to_file=(target == "save"))
            except DownloadCancelled:
                if target == "dubbing" and dest_dir:
                    shutil.rmtree(dest_dir, ignore_errors=True)
                else:
                    _cleanup_partial_files()  # save/review: không bỏ rác .part trong thư mục người dùng
                q.put({"type": "cancelled"})
                return
            except ValueError as e:
                if target == "dubbing" and dest_dir:
                    shutil.rmtree(dest_dir, ignore_errors=True)
                else:
                    _cleanup_partial_files()
                q.put({"type": "error", "message": str(e)})
                return

            # Hủy tới ĐÚNG LÚC tải xong (trong pha merge hook không chạy nữa):
            # tôn trọng lệnh hủy — không tạo dự án/DB row từ lượt tải đã bị hủy.
            if cancel_event.is_set():
                if target == "dubbing" and dest_dir:
                    shutil.rmtree(dest_dir, ignore_errors=True)
                else:
                    _cleanup_partial_files()
                q.put({"type": "cancelled"})
                return

            path = result["path"]

            from services.download_service import (
                _sanitize_filename, translate_title_to_vi,
            )
            # TÊN CUỐI dùng cho cả tên dự án (lồng tiếng/review) LẪN tên file
            # (lưu máy): ưu tiên title_override (FE đã dịch) -> dịch nếu bật
            # translate_title -> giữ tiêu đề gốc. Cập nhật result["title"] để
            # bản ghi Video / toast dùng đúng tên đã dịch.
            eff_title = ""
            if title_override:
                eff_title = title_override
            elif translate_title and result.get("title"):
                eff_title = translate_title_to_vi(result.get("title"))
            if eff_title:
                result["title"] = eff_title

            # "Lưu về máy": ĐẶT LẠI TÊN FILE — theo tên người dùng chọn ở hộp
            # thoại (custom_name) hoặc theo tiêu đề cuối (link dán / đã dịch).
            if target == "save":
                new_stem = os.path.splitext(custom_name)[0] if custom_name \
                    else (result.get("title") or "")
                if new_stem:
                    new_stem = _sanitize_filename(new_stem)
                    ext = os.path.splitext(path)[1]
                    base_dir = os.path.dirname(path)
                    new_path = os.path.join(base_dir, new_stem + ext)
                    # Không đè file khác trùng tên: thêm hậu tố (2), (3)...
                    n = 1
                    while (os.path.exists(new_path)
                           and os.path.normcase(new_path) != os.path.normcase(path)):
                        n += 1
                        new_path = os.path.join(base_dir, f"{new_stem} ({n}){ext}")
                    if os.path.normcase(new_path) != os.path.normcase(path):
                        try:
                            os.replace(path, new_path)
                            path = new_path
                            result["title"] = new_stem
                        except OSError as rename_err:
                            logger.warning(
                                f"Đổi tên file tải về thất bại (giữ tên cũ): {rename_err}"
                            )

                # Phụ đề chính chủ (lưu về máy): đổi tên file .srt/.ass khớp với video
                # cuối cùng -> "<tên video>.<lang>.<ext>" cạnh file video.
                # Đổi tên MỌI thứ tiếng tải được, không riêng thứ tiếng đã chọn làm
                # transcript: video kèm chục thứ tiếng mà chỉ giữ một thì người dùng
                # muốn bản khác lại phải tải lại cả video.
                sub_paths = dict(result.get("subtitle_paths") or {})
                sub_src = result.get("subtitle_path")
                if sub_src and not sub_paths:
                    sub_paths = {result.get("subtitle_lang") or "": sub_src}
                if sub_paths:
                    vid_stem = os.path.splitext(os.path.basename(path))[0]
                    renamed = {}
                    for lang, src_fp in sub_paths.items():
                        if not src_fp or not os.path.exists(src_fp):
                            continue
                        sub_ext = os.path.splitext(src_fp)[1]  # .srt/.vtt/.ass
                        sub_stem = f"{vid_stem}.{lang}" if lang else vid_stem
                        sub_dst = os.path.join(os.path.dirname(path), sub_stem + sub_ext)
                        try:
                            if os.path.normcase(sub_dst) != os.path.normcase(src_fp):
                                os.replace(src_fp, sub_dst)
                            renamed[lang] = sub_dst
                        except OSError as e:
                            logger.warning(f"Đổi tên file phụ đề ({lang}) thất bại: {e}")
                    if renamed:
                        result["subtitle_paths"] = renamed
                        if sub_src:
                            result["subtitle_path"] = renamed.get(
                                result.get("subtitle_lang") or "", sub_src,
                            )
                        logger.info(
                            f"[DOWNLOAD] Đã lưu {len(renamed)} tệp phụ đề cạnh video "
                            f"({', '.join(sorted(renamed))})."
                        )

            # ffprobe file đã tải trước; nếu ra 0 (BiliBili DASH đôi khi thiếu
            # 'Duration' trong header) thì dùng duration từ metadata yt-dlp.
            duration = VideoService.get_video_duration(path) or result.get("duration") or 0

            if target != "dubbing":
                # Review: nếu tải được sub CHÍNH CHỦ, ghi file sidecar cạnh video để
                # bước STT của review đọc lại làm transcript (project review tạo qua
                # request khác, chỉ mang theo video_path). Xem review_service._load_author_subs.
                author_subs = result.get("subtitles") or []
                # Review nâng cao cũng đọc sidecar này để khỏi phải chạy STT.
                wants_sidecar = target in ("review", "review-advanced")
                if wants_sidecar:
                    # Xoá sidecar CŨ trước (path theo id video là cố định): lần tải lại
                    # không có sub chính chủ mà sidecar cũ còn -> project mới dùng
                    # transcript cũ sai.
                    try:
                        os.remove(path + ".subforge_sub.json")
                    except OSError:
                        pass
                if wants_sidecar and author_subs:
                    try:
                        with open(path + ".subforge_sub.json", "w", encoding="utf-8") as f:
                            json.dump(
                                # ``lang``/``segments``: thứ tiếng mặc định — giữ nguyên
                                # khoá cũ để sidecar của các bản trước vẫn đọc được.
                                # ``all``: MỌI thứ tiếng tải được, để trình dựng cho
                                # người dùng đổi qua lại mà không phải tải lại video.
                                {"lang": result.get("subtitle_lang") or "",
                                 "segments": author_subs,
                                 "all": result.get("subtitles_by_lang") or {}},
                                f, ensure_ascii=False, separators=(",", ":"),
                            )
                    except OSError as _e:
                        logger.warning(f"Không ghi được sidecar phụ đề tác giả: {_e}")
                q.put({"type": "done", "target": target, "path": path,
                       "title": result["title"], "duration": duration,
                       "subtitles_from_author": len(author_subs),
                       "subtitle_langs": sorted(result.get("subtitles_by_lang") or {})})
                return

            db_video = Video(
                id=video_id, title=result["title"], original_path=path,
                duration=duration, status="uploaded",
                p1_status="pending", p2_status="pending", p3_status="pending",
                p4_status="pending", p5_status="pending", created_at=time.time(),
            )
            _ap_mac_dinh_co_ban(db_video, db)
            db.add(db_video)
            db.commit()
            db.refresh(db_video)

            # Phụ đề CHÍNH CHỦ của tác giả (nếu có) -> đổ thẳng làm transcript, coi
            # như bước STT (p2) ĐÃ XONG. Người dùng vẫn bấm STT chạy lại đè được (route
            # /stt xoá hết segment cũ trước khi transcribe).
            #  Gộp mẩu 1–3 chữ / câu nói quá nhanh vào câu kề bên (services/gop_manh_ngan.py).
            author_subs = gop_manh_ngan(result.get("subtitles") or [], "(sub chính chủ)")
            if author_subs:
                for i, seg in enumerate(author_subs):
                    db.add(SubtitleSegment(
                        video_id=video_id,
                        index=i,
                        start_time=seg["start"],
                        end_time=seg["end"],
                        source_text=seg["text"],
                    ))
                db_video.source_lang = result.get("subtitle_lang") or db_video.source_lang
                db_video.p2_status = "completed"
                db_video.status = "stt_completed"
                db.commit()
                logger.info(
                    f"[DOWNLOAD] Video {video_id}: dùng phụ đề tác giả "
                    f"({db_video.source_lang or '?'}) làm transcript ({len(author_subs)} câu), "
                    f"bỏ qua STT."
                )

            sync_project_to_disk(video_id, db)
            q.put({"type": "done", "target": "dubbing", "id": db_video.id,
                   "title": db_video.title,
                   "subtitles_from_author": len(author_subs)})
        except Exception as e:  # noqa: BLE001
            # Lỗi hậu xử lý (DB/ffprobe/đổi tên...) — log chi tiết, gửi UI thông
            # báo tiếng Việt thay vì chuỗi exception thô.
            logger.error(f"[DOWNLOAD] Lỗi xử lý sau tải: {e}", exc_info=True)
            q.put({
                "type": "error",
                "message": (
                    L("Tải xong nhưng bước xử lý sau tải gặp lỗi "
                      f"({type(e).__name__}). Xem log để biết chi tiết.",
                      "The download finished but the post-download processing step hit an error "
                      f"({type(e).__name__}). See the log for details.")
                ),
            })
        finally:
            db.close()
            q.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    async def generator():
        try:
            while True:
                # Client ngắt kết nối -> báo worker hủy tải.
                if await request.is_disconnected():
                    cancel_event.set()
                    break
                try:
                    item = q.get_nowait()
                except Empty:
                    await asyncio.sleep(0.15)
                    if not thread.is_alive() and q.empty():
                        break
                    continue
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            cancel_event.set()  # đảm bảo worker dừng nếu generator thoát sớm

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.get("/{video_id}/running-status")
def get_running_status(video_id: str):
    """Cho biết video này CÓ đang chạy bước pipeline nào không (nguồn: registry
    in-memory _PROCESSING_GUARD). Dùng để khôi phục trạng thái + nút Hủy khi user
    rời màn rồi quay lại — vì trạng thái 'đang chạy' không nằm hết trong React state.
    """
    with _PROCESSING_GUARD_LOCK:
        step = _PROCESSING_GUARD.get(video_id)
    return {
        "running": step is not None,
        "step": step,
        "cancel_requested": _is_cancel_requested(video_id),
    }


@router.post("/{video_id}/extract-audio")
def extract_audio(video_id: str, db: Session = Depends(get_db)):
    _acquire_or_409(video_id, "extract-audio")
    handler = setup_project_logging(video_id, db)
    video = None
    try:
        logger.info(f"=== BẮT ĐẦU TÁCH ÂM THANH CHO VIDEO {video_id} ===")
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))
        video.p1_status = "processing"
        db.commit()
        dirs = get_project_dirs(video_id, db)
        audio_path = os.path.join(dirs["audio"], "source_audio.wav").replace("\\", "/")
        if VideoService.extract_audio(video.original_path, audio_path):
            video.audio_path = audio_path
            video.p1_status = "completed"
            video.status = "audio_extracted"
            db.commit()
            sync_project_to_disk(video_id, db)
            logger.info("=== TÁCH ÂM THANH HOÀN TẤT ===")
            return {"status": "success"}
        video.p1_status = "error"
        db.commit()
        logger.error("=== TÁCH ÂM THANH THẤT BẠI ===")
        return JSONResponse(status_code=400, content={"detail": L("Thất bại", "Failed")})
    except HTTPException:
        raise
    except Exception as e:
        # Nếu extract_audio RAISE (thay vì trả False) mà không bắt, p1_status kẹt
        # "processing" mãi → UI treo spinner. Đưa về "error" để chạy lại được.
        logger.error(f"Tách âm thanh gặp sự cố: {e}")
        try:
            if video is not None:
                video.p1_status = "error"
                db.commit()
        except Exception:
            pass
        return JSONResponse(status_code=400, content={"detail": str(e)})
    finally:
        remove_project_logging(handler)
        _release_step(video_id)


@router.get("/{video_id}/duration")
def get_video_duration(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))
    duration = VideoService.get_video_duration(video.original_path)
    return {"duration": duration}


def _bo_cau_dich_phu(project_root: str) -> None:
    """Câu phụ đề bị xoá sạch để tạo lại -> bỏ bản dịch "phụ đề thêm thứ tiếng" (khoá theo
    id câu, SQLite dùng lại id — xem ``review_sub_tracks.xoa_moi_cau``)."""
    try:
        from services import review_sub_tracks as ST
        ST.xoa_moi_cau(project_root)
    except Exception as e:  # noqa: BLE001 — không được chặn bước nhận dạng
        logger.warning(f"Không dọn được phụ đề thêm thứ tiếng: {e}")


def _gop_manh_ngan_db(db: Session, video_id: str, tu_index: int) -> int:
    """Gộp mẩu phụ đề ngắn trên các câu VỪA nhận dạng (``index > tu_index``) rồi ghi lại
    với số thứ tự liền mạch. Trả về số câu sau khi gộp. Xem services/gop_manh_ngan.py."""
    db.flush()
    rows = (
        db.query(SubtitleSegment)
        .filter(SubtitleSegment.video_id == video_id, SubtitleSegment.index > tu_index)
        .order_by(SubtitleSegment.index)
        .all()
    )
    segs = [{"start": r.start_time, "end": r.end_time, "text": r.source_text or "",
             "ocr": bool(r.ocr_filled)} for r in rows]
    moi = gop_manh_ngan(segs, "(Cơ bản)")
    if len(moi) == len(rows):
        return len(rows)
    for r in rows:
        db.delete(r)
    db.flush()
    for i, s in enumerate(moi):
        db.add(SubtitleSegment(
            video_id=video_id,
            index=tu_index + 1 + i,
            start_time=s["start"],
            end_time=s["end"],
            source_text=s["text"],
            ocr_filled=bool(s.get("ocr")),
        ))
    return len(moi)


def _tach_cau_goc_db(db: Session, video_id: str, tu_index: int, bat: bool) -> int:
    """Tách đoạn Whisper dài thành từng câu (dấu câu + độ dài) trên các câu VỪA nhận dạng
    (``index > tu_index``) rồi ghi lại với số thứ tự liền mạch. Trả về số câu sau khi tách.
    CHỈ gọi ở nhánh Whisper — xem services/tach_cau.py. Chạy SAU ``_gop_manh_ngan_db``.
    ``bat``: ô "Tách câu gốc" của video (tham số ``tach_cau`` của bước Lấy phụ đề)."""
    db.flush()
    rows = (
        db.query(SubtitleSegment)
        .filter(SubtitleSegment.video_id == video_id, SubtitleSegment.index > tu_index)
        .order_by(SubtitleSegment.index)
        .all()
    )
    segs = [{"start": r.start_time, "end": r.end_time, "text": r.source_text or "",
             "ocr": bool(r.ocr_filled)} for r in rows]
    moi = tach_cau_goc(segs, "(Cơ bản)", bat=bat)
    if len(moi) == len(rows):
        return len(rows)
    for r in rows:
        db.delete(r)
    db.flush()
    for i, s in enumerate(moi):
        db.add(SubtitleSegment(
            video_id=video_id,
            index=tu_index + 1 + i,
            start_time=s["start"],
            end_time=s["end"],
            source_text=s["text"],
            ocr_filled=bool(s.get("ocr")),
        ))
    return len(moi)


@router.post("/{video_id}/stt")
def stt(
    video_id: str,
    source_lang: str = Query(None),
    #  Ô "Tách câu gốc" của video (lựa chọn ở giao diện như "Nguồn phụ đề"); thiếu = mặc định BẬT.
    tach_cau: Optional[bool] = Query(None),
    data: dict = Body(None),
    db: Session = Depends(get_db),
):
    # Chặn chạy chồng: nếu video đang có bước khác chạy (hoặc STT cũ chưa dừng hẳn sau
    # khi bấm Hủy) -> 409 để tránh 2 tiến trình cùng ghi DB gây "database is locked".
    _acquire_or_409(video_id, "stt")
    handler = setup_project_logging(video_id, db)
    _clear_cancel(video_id)  # xoá cờ hủy còn sót từ lần trước (tránh tự hủy oan)
    try:
        logger.info(f"=== BẮT ĐẦU NHẬN DẠNG LỜI THOẠI CHO VIDEO {video_id} (Ngôn ngữ: {source_lang}) ===")
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            logger.error("Không tìm thấy video")
            raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

        # Map SubForge language codes to Whisper codes
        whisper_lang = source_lang
        if source_lang == "zh-cn":
            whisper_lang = "zh"
        elif source_lang == "auto":
            whisper_lang = None

        start_time = data.get("start_time", 0) if data else 0
        duration = data.get("duration") if data else None
        is_chunk = duration is not None

        dirs = get_project_dirs(video_id, db)

        if not is_chunk:
            video.p2_status = "processing"
            video.source_lang = source_lang or video.source_lang  # Persist selected lang
            db.commit()
            db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).delete()
            _bo_cau_dich_phu(dirs["root"])
            audio_to_transcribe = video.audio_path
            if not audio_to_transcribe or not os.path.exists(audio_to_transcribe):
                # Force extract if missing
                audio_path = os.path.join(dirs["audio"], "source_audio.wav").replace(
                    "\\", "/"
                )
                if VideoService.extract_audio(video.original_path, audio_path):
                    video.audio_path = audio_path
                    db.commit()
                    audio_to_transcribe = audio_path
        else:
            # Extract a small slice for this chunk
            temp_wav = os.path.join(dirs["audio"], f"temp_chunk_{start_time}.wav").replace(
                "\\", "/"
            )
            if VideoService.extract_audio_slice(
                video.original_path, temp_wav, start_time, duration
            ):
                audio_to_transcribe = temp_wav
            else:
                logger.error("Không thể tách đoạn âm thanh")
                raise HTTPException(status_code=500, detail=L("Không thể tách đoạn âm thanh", "Could not split the audio segment"))

        try:
            # Global index offset (tính TRƯỚC transcribe: non-chunk đã xoá segment cũ
            # ở trên nên max = -1; chunk giữ lại segment cũ).
            #  ``is None`` chứ không ``or -1``: max = 0 (đang có đúng một câu) mà ``or`` thì
            #  thành -1 -> câu mới trùng số 0, và bước gộp mẩu ngắn kéo cả câu cũ vào.
            max_idx = (
                db.query(func.max(SubtitleSegment.index))
                .filter(SubtitleSegment.video_id == video_id)
                .scalar()
            )
            max_idx = -1 if max_idx is None else int(max_idx)

            # Chèn + commit DẦN từng segment ngay khi transcribe xong (đường sequential)
            # → FE poll (4s) thấy phụ đề hiện dần lên UI trong lúc còn đang nhận dạng.
            _inserted = {"n": 0}

            def _on_seg(sd):
                db.add(SubtitleSegment(
                    video_id=video_id,
                    index=max_idx + 1 + _inserted["n"],
                    start_time=sd["start"] + start_time,
                    end_time=sd["end"] + start_time,
                    source_text=sd["text"],
                ))
                _inserted["n"] += 1
                if _inserted["n"] % 5 == 0:  # commit theo lô 5 để giảm ghi đĩa
                    # Commit best-effort: nếu vướng khoá (WAL + busy_timeout đã giảm
                    # rủi ro) cũng KHÔNG được làm chết cả bước STT — lô sau commit tiếp.
                    try:
                        db.commit()
                    except Exception as _e:
                        logger.warning(f"Bỏ qua lần lưu tạm phụ đề nhận dạng: {_e}")
                        db.rollback()

            _check_cancel(video_id)
            # check_cancel gọi theo TỪNG segment → hủy được giữa chừng STT.
            segments, detected_lang = stt_service.transcribe(
                audio_to_transcribe,
                language=whisper_lang,
                check_cancel=lambda: _check_cancel(video_id),
                on_segment=_on_seg,
            )

            # Save detected language to DB
            if detected_lang and (not video.source_lang or video.source_lang == "auto"):
                video.source_lang = detected_lang
                logger.info(f"STT: Đã tự động nhận diện ngôn ngữ gốc: {detected_lang}")

            # Fallback: nếu on_segment KHÔNG chạy (đường parallel cho video dài) →
            # chèn từ list như cũ.
            if _inserted["n"] == 0:
                for i, seg in enumerate(segments):
                    db.add(SubtitleSegment(
                        video_id=video_id,
                        index=max_idx + 1 + i,
                        start_time=seg["start"] + start_time,
                        end_time=seg["end"] + start_time,
                        source_text=seg["text"],
                    ))
            #  Câu đã hiện dần lên giao diện trong lúc nhận dạng; xong rồi mới gộp mẩu
            #  ngắn (cần thấy cả câu trước lẫn câu sau mới biết gộp về phía nào).
            _so_cau = _gop_manh_ngan_db(db, video_id, max_idx)
            #  Rồi tách đoạn Whisper dài nhiều câu theo dấu câu + độ dài (services/tach_cau.py).
            _so_cau = _tach_cau_goc_db(
                db, video_id, max_idx, MAC_DINH_GOC if tach_cau is None else tach_cau
            )

            if not is_chunk:
                video.p2_status = "completed"
                video.status = "stt_completed"

            db.commit()

            # Cleanup temp file
            if is_chunk and os.path.exists(audio_to_transcribe):
                os.remove(audio_to_transcribe)

            sync_project_to_disk(video_id, db)
            logger.info(f"=== NHẬN DẠNG LỜI THOẠI HOÀN TẤT: {_so_cau} câu ===")
            return {"status": "success", "segment_count": _so_cau}
        except _PipelineCancelled:
            if not is_chunk:
                video.p2_status = "pending"
                db.commit()
            logger.info("=== ĐÃ HỦY NHẬN DẠNG LỜI THOẠI THEO YÊU CẦU ===")
            return {"status": "cancelled"}
        except Exception as e:
            if not is_chunk:
                video.p2_status = "error"
                db.commit()
            logger.error(f"Nhận dạng lời thoại thất bại: {e}")
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        _clear_cancel(video_id)
        remove_project_logging(handler)
        _release_step(video_id)


def _ocr_lang_from_source(src: str) -> str:
    """Map ngôn ngữ nguồn -> mã ngôn ngữ OCR (dùng cho lọc rác theo script)."""
    s = (src or "").lower()
    if s in ("en", "eng", "en-us", "en-gb"):
        return "en"
    if s in ("ja", "ja-jp", "japan"):
        return "ja"
    if s in ("vi", "vi-vn"):
        return "vi"
    return "ch"  # zh-cn / zh / auto / mặc định


@router.post("/{video_id}/ocr")
def ocr_subtitles(
    video_id: str,
    source_lang: str = Query(None),
    data: dict = Body(None),
    db: Session = Depends(get_db),
):
    """CHẾ ĐỘ CHỈ OCR: lấy phụ đề CỨNG (đã cháy trên hình) bằng OCR, thay cho STT.

    Dùng cho video đã có sẵn sub cứng — tốc độ vượt trội (video vài tiếng trong vài
    phút). Body: { "region": {x,y,w,h} } toạ độ vùng phụ đề chuẩn hoá 0..1 do người
    dùng khoanh trên FE. Kết quả lưu vào subtitle_segments giống hệt STT.
    """
    _acquire_or_409(video_id, "ocr")
    handler = setup_project_logging(video_id, db)
    _clear_cancel(video_id)
    try:
        logger.info(f"=== BẮT ĐẦU LẤY PHỤ ĐỀ BẰNG OCR CHO VIDEO {video_id} (Ngôn ngữ: {source_lang}) ===")
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            logger.error("Không tìm thấy video")
            raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

        region = (data or {}).get("region")
        # Truyền đúng ngôn ngữ để lọc rác theo script (Anh giữ 'No/Hi', Trung bỏ Latin ngắn).
        ocr_lang = _ocr_lang_from_source(source_lang)

        video.p2_status = "processing"
        video.source_lang = source_lang or video.source_lang
        db.commit()
        # Chế độ A = thay toàn bộ phụ đề cũ.
        db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).delete()
        db.commit()
        _bo_cau_dich_phu(get_project_dirs(video_id, db)["root"])

        try:
            segments = ocr_service.extract_subtitles(
                video.original_path,
                region=region,
                lang=ocr_lang,
                check_cancel=lambda: _check_cancel(video_id),
            )
            segments = gop_manh_ngan(segments, "(OCR)")

            for i, seg in enumerate(segments):
                db.add(SubtitleSegment(
                    video_id=video_id,
                    index=i,
                    start_time=seg["start"],
                    end_time=seg["end"],
                    source_text=seg["text"],
                ))

            video.p2_status = "completed"
            video.status = "stt_completed"
            db.commit()
            sync_project_to_disk(video_id, db)
            logger.info(f"=== LẤY PHỤ ĐỀ BẰNG OCR HOÀN TẤT: {len(segments)} câu ===")
            return {"status": "success", "segment_count": len(segments)}
        except _PipelineCancelled:
            video.p2_status = "pending"
            db.commit()
            logger.info("=== ĐÃ HỦY LẤY PHỤ ĐỀ BẰNG OCR THEO YÊU CẦU ===")
            return {"status": "cancelled"}
        except Exception as e:
            video.p2_status = "error"
            db.commit()
            logger.error(f"Lấy phụ đề bằng OCR thất bại: {e}")
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        _clear_cancel(video_id)
        remove_project_logging(handler)
        _release_step(video_id)


@router.post("/{video_id}/stt-ocr")
def stt_with_ocr_fallback(
    video_id: str,
    source_lang: str = Query(None),
    tach_cau: Optional[bool] = Query(None),     # ô "Tách câu gốc" — như route /stt
    data: dict = Body(None),
    db: Session = Depends(get_db),
):
    """CHẾ ĐỘ B: Whisper lấy giọng, OCR VÁ các đoạn Whisper không nghe được.

    Chạy Whisper như bình thường, rồi OCR TOÀN video (đọc sub cứng) và CHỈ giữ các
    đoạn OCR rơi vào "lỗ hổng" của Whisper (khoảng không có câu Whisper — thường là
    tiếng nước ngoài Whisper điếc). Các đoạn OCR này gắn cờ ocr_filled=True: KHÔNG
    sinh voice hàng loạt, có banner cảnh báo để người dùng xác nhận.

    Body: { "region": {x,y,w,h} } vùng phụ đề cứng (bắt buộc, cho phần OCR).
    """
    _acquire_or_409(video_id, "stt-ocr")
    handler = setup_project_logging(video_id, db)
    _clear_cancel(video_id)
    try:
        logger.info(f"=== BẮT ĐẦU WHISPER + OCR VÁ LỖ CHO VIDEO {video_id} (Ngôn ngữ: {source_lang}) ===")
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

        region = (data or {}).get("region")
        whisper_lang = source_lang
        if source_lang == "zh-cn":
            whisper_lang = "zh"
        elif source_lang == "auto":
            whisper_lang = None

        dirs = get_project_dirs(video_id, db)
        video.p2_status = "processing"
        video.source_lang = source_lang or video.source_lang
        db.commit()
        db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).delete()
        db.commit()
        _bo_cau_dich_phu(dirs["root"])

        # 1) Đảm bảo có audio cho Whisper.
        audio_to_transcribe = video.audio_path
        if not audio_to_transcribe or not os.path.exists(audio_to_transcribe):
            audio_path = os.path.join(dirs["audio"], "source_audio.wav").replace("\\", "/")
            if VideoService.extract_audio(video.original_path, audio_path):
                video.audio_path = audio_path
                db.commit()
                audio_to_transcribe = audio_path

        try:
            # 2) Whisper (nghe giọng).
            logger.info("Whisper + OCR: [1/2] nhận dạng giọng nói bằng Whisper...")
            _check_cancel(video_id)
            wh_segments, detected_lang = stt_service.transcribe(
                audio_to_transcribe,
                language=whisper_lang,
                check_cancel=lambda: _check_cancel(video_id),
            )
            if detected_lang and (not video.source_lang or video.source_lang == "auto"):
                video.source_lang = detected_lang

            # 3) Tính "lỗ hổng" Whisper (khoảng không có câu) rồi CHỈ OCR các khoảng đó
            #    -> nhanh hơn nhiều so với OCR cả video (ffmpeg input-seek chỉ giải mã
            #    đúng cửa sổ). Không có câu Whisper nào -> OCR cả video.
            vid_dur = float(VideoService.get_video_duration(video.original_path) or 0.0)
            gaps = _compute_whisper_gaps(wh_segments, vid_dur)
            # gaps RỖNG = Whisper đã phủ kín toàn video -> KHÔNG cần OCR (tránh quét cả
            # video vô ích). Whisper trống thật thì compute_whisper_gaps trả [(0,dur)]
            # (không rỗng) nên vẫn OCR cả video đúng như mong muốn.
            if not gaps:
                logger.info(
                    "Whisper + OCR: [2/2] Whisper đã phủ kín toàn bộ lời thoại — "
                    "BỎ QUA OCR (không có khoảng trống cần vá)."
                )
                ocr_segments = []
            else:
                logger.info(
                    f"Whisper + OCR: [2/2] OCR sub cứng vá {len(gaps)} khoảng Whisper bỏ sót..."
                )
                _check_cancel(video_id)
                ocr_segments = ocr_service.extract_subtitles(
                    video.original_path, region=region,
                    lang=_ocr_lang_from_source(source_lang),
                    check_cancel=lambda: _check_cancel(video_id),
                    windows=gaps,
                )

            # 4) Giữ đoạn OCR KHÔNG chồng câu Whisper (phòng input-seek lấn sang vùng
            #    Whisper đã có) -> đúng phần Whisper không nghe được.
            gap_ocr = [
                o for o in ocr_segments
                if not _overlaps_any(o, wh_segments)
            ]
            logger.info(
                f"Whisper + OCR: Whisper {len(wh_segments)} câu, OCR vá thêm "
                f"{len(gap_ocr)} câu vào chỗ trống (OCR quét được {len(ocr_segments)})."
            )

            # 5) Lưu: Whisper (bình thường) + OCR vá lỗ (ocr_filled=True), sắp theo thời gian.
            merged = [
                {"start": s["start"], "end": s["end"], "text": s["text"], "ocr": False}
                for s in wh_segments
            ] + [
                {"start": s["start"], "end": s["end"], "text": s["text"], "ocr": True}
                for s in gap_ocr
            ]
            merged.sort(key=lambda s: s["start"])
            #  Câu OCR vá lỗ chỉ gộp với câu OCR (khoá "ocr"), không trộn vào câu Whisper.
            merged = gop_manh_ngan(merged, "(Whisper + OCR)")
            #  Tách đoạn dài theo dấu câu + độ dài — cả câu OCR vá lỗ (mảnh giữ cờ "ocr" ->
            #  vẫn ``ocr_filled``, vẫn không sinh giọng hàng loạt).
            merged = tach_cau_goc(
                merged, "(Whisper + OCR)", bat=MAC_DINH_GOC if tach_cau is None else tach_cau
            )
            for i, s in enumerate(merged):
                db.add(SubtitleSegment(
                    video_id=video_id,
                    index=i,
                    start_time=s["start"],
                    end_time=s["end"],
                    source_text=s["text"],
                    ocr_filled=s["ocr"],
                ))

            video.p2_status = "completed"
            video.status = "stt_completed"
            db.commit()
            sync_project_to_disk(video_id, db)
            logger.info(
                f"=== WHISPER + OCR HOÀN TẤT: {len(merged)} câu "
                f"({len(gap_ocr)} câu vá bằng OCR) ==="
            )
            return {
                "status": "success",
                "segment_count": len(merged),
                "ocr_filled_count": len(gap_ocr),
            }
        except _PipelineCancelled:
            video.p2_status = "pending"
            db.commit()
            logger.info("=== ĐÃ HỦY WHISPER + OCR THEO YÊU CẦU ===")
            return {"status": "cancelled"}
        except Exception as e:
            video.p2_status = "error"
            db.commit()
            logger.error(f"Whisper + OCR thất bại: {e}")
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        _clear_cancel(video_id)
        remove_project_logging(handler)
        _release_step(video_id)


@router.post("/{video_id}/translate")
async def translate(
    video_id: str,
    source_lang: Optional[str] = Query(None),
    target_lang: Optional[str] = Query(None),
    engine: str = Query("google"),
    force_reset: bool = Query(False),
    # Kiểu nói/thể loại (preset id hoặc text tự nhập) — chỉ có tác dụng với DeepSeek.
    style: Optional[str] = Query(None),
    data: dict = Body(None),
    db: Session = Depends(get_db),
):
    _acquire_or_409(video_id, "translate")
    handler = setup_project_logging(video_id, db)
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

        # Validate languages
        #  Mirror ô ngôn ngữ của tab lồng tiếng (``LANG_LABELS`` trong VideoDetail.tsx) —
        #  FE chào mà BE không có ở đây là bấm Dịch báo "không được hỗ trợ".
        allowed_sources = ["vi", "zh", "en", "ja", "ko", "zh-cn", "auto"]
        allowed_targets = ["vi", "zh", "en", "ja", "ko", "zh-cn",
                           "fr", "de", "es", "ru", "pt", "it"]
        src_check = (source_lang or video.source_lang or "zh-cn").lower()
        tgt_check = (target_lang or video.target_lang or "vi").lower()

        if src_check not in allowed_sources or tgt_check not in allowed_targets:
            raise HTTPException(
                status_code=400,
                detail=f"Ngôn ngữ không được hỗ trợ. Các ngôn ngữ hỗ trợ: {', '.join(allowed_targets)}",
            )

        # Engine dịch: "google" (Google Translate) hoặc "deepseek:<model>".
        # "local" là giá trị CŨ (FE bản trước hardcode) — quy về google để video
        # cũ không bị kẹt selector trống / stale giả trên giao diện mới.
        if not engine or engine == "local":
            engine = "google"
        # Bộ AI (DeepSeek / Gemini) cần API key (người dùng nhập trong Cài đặt). Chặn sớm nếu chưa có
        # để không đưa video vào trạng thái "đang dịch" rồi mới lỗi.
        from services.ai_engines import provider_of, ten_bo_ai

        bo_ai = provider_of(engine)
        if bo_ai:
            import services.deepseek_service as deepseek_service
            if not deepseek_service.is_configured(bo_ai):
                raise HTTPException(
                    status_code=400,
                    detail=f"Chưa cấu hình API key {ten_bo_ai(bo_ai)}. Vào Cài đặt để nhập API key trước khi dịch bằng {ten_bo_ai(bo_ai)}.",
                )
        video.translation_engine = engine

        # Extract data safely
        segment_ids = data.get("segment_ids") if data else None

        # Update model values
        if source_lang and source_lang != "auto":
            video.source_lang = source_lang
        if target_lang:
            video.target_lang = target_lang
        video.translation_engine = engine

        db.commit()

        _clear_cancel(video_id)  # xoá cờ hủy còn sót từ lần trước
        if bo_ai:
            from services.ai_engines import tach_model as _tach

            _m = _tach(engine)[1]
            engine_label = (f"{ten_bo_ai(bo_ai)} ({_m})" if _m
                            else f"{ten_bo_ai(bo_ai)} ({config_service.ai_model_mac_dinh_cua(bo_ai) or '?'} — theo Cài đặt)")
        else:
            engine_label = "Google Dịch"
        logger.info(
            f"=== BẮT ĐẦU DỊCH CHO VIDEO {video_id} ({src_check} -> {tgt_check}) "
            f"| Công cụ: {engine_label} ==="
        )

        # Only reset if explicitly requested
        should_reset = force_reset or (data.get("force_reset", False) if data else False)

        if should_reset:
            logger.info(f"Dịch lại toàn bộ cho video {video_id}")

            # Clear existing translations for the requested segments or all
            if segment_ids:
                db.query(SubtitleSegment).filter(
                    SubtitleSegment.video_id == video_id,
                    SubtitleSegment.id.in_(segment_ids),
                ).update(
                    {SubtitleSegment.target_text: "", SubtitleSegment.translation_fallback: False},
                    synchronize_session=False,
                )
            else:
                db.query(SubtitleSegment).filter(
                    SubtitleSegment.video_id == video_id
                ).update(
                    {SubtitleSegment.target_text: "", SubtitleSegment.translation_fallback: False},
                    synchronize_session=False,
                )
            db.commit()

        query = db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id)

        if segment_ids:
            query = query.filter(SubtitleSegment.id.in_(segment_ids))
        elif not should_reset:
            # Only translate missing or errored segments
            if src_check != tgt_check:
                query = query.filter(
                    (SubtitleSegment.target_text == None)
                    | (SubtitleSegment.target_text == "")
                    | (SubtitleSegment.target_text.like("[Error:%"))
                    | (SubtitleSegment.target_text == SubtitleSegment.source_text)
                )
            else:
                query = query.filter(
                    (SubtitleSegment.target_text == None)
                    | (SubtitleSegment.target_text == "")
                    | (SubtitleSegment.target_text.like("[Error:%"))
                )

        query = query.order_by(SubtitleSegment.index)

        segments = query.all()

        # VỚT THÊM các đoạn "dịch hụt" đã lưu từ trước: target vẫn còn CHỮ HÁN
        # (Google auto trả gần-nguyên-văn / dịch nửa chừng — không bắt được bằng
        # so sánh == source trong SQL). Chỉ áp khi đích không dùng chữ Hán (vi/en).
        if not segment_ids and not should_reset:
            from services.translation_service import has_han, _dest_expects_no_han
            if _dest_expects_no_han(tgt_check):
                seen_ids = {s.id for s in segments}
                stuck = [
                    s
                    for s in db.query(SubtitleSegment)
                    .filter(SubtitleSegment.video_id == video_id)
                    .all()
                    if s.id not in seen_ids and s.target_text and has_han(s.target_text)
                ]
                if stuck:
                    logger.info(
                        f"Phát hiện {len(stuck)} đoạn bản dịch cũ vẫn còn chữ Hán — sẽ dịch lại các đoạn này."
                    )
                    segments = sorted(segments + stuck, key=lambda s: s.index)

        if not segments:
            logger.info("Không có câu nào cần dịch.")
            return {
                "status": "success",
                "message": L("Không có câu nào cần xử lý", "No sentences need processing"),
                "processed_count": 0,
            }

        # Only mark as processing if it's a full batch
        if not segment_ids:
            video.p3_status = "processing"
            db.commit()

        from services.translation_service import TranslationRateLimitError

        try:
            source_texts = [seg.source_text for seg in segments]
            is_partial = False
            fallback_id_set: set = set()
            deepseek_error_ids: list = []

            if provider_of(engine):
                # Bộ AI (DeepSeek / Gemini): gửi 100 đoạn/lần với prompt dịch lồng tiếng; lô lỗi tự
                # fallback về Google (đánh dấu để cảnh báo trên UI).
                import services.deepseek_service as deepseek_service
                from services.ai_engines import ma_model, tach_model

                #  "gemini" / "deepseek" không kèm model = model mặc định của bộ đó ở Cài đặt; mã gửi đi là ``provider:model``.
                _bo, _m = tach_model(engine)
                deepseek_model = ma_model(_bo, _m) if _m else config_service.ai_model_mac_dinh_cua(_bo)

                seg_payload = [
                    {
                        "id": s.id,
                        "text": s.source_text,
                        "start": s.start_time,
                        "end": s.end_time,
                    }
                    for s in segments
                ]
                results_map, fb_ids, err_ids = await deepseek_service.translate_dubbing_segments(
                    seg_payload,
                    source_lang=video.source_lang or "zh-cn",
                    target_lang=video.target_lang or "vi",
                    model=deepseek_model,
                    should_stop=lambda: _is_cancel_requested(video_id),
                    style=style,
                    voice=video.voice_talent,
                    #  "Số câu gửi mỗi lô" ở Cài đặt → DeepSeek (dùng chung với Chi tiết /
                    #  chạy lô) — trước đây Cơ bản gắn cứng 100 (người dùng báo 29-09-2026).
                    batch_size=config_service.get_ai_script_options().get("batch_size"),
                )
                fallback_id_set = set(fb_ids)
                deepseek_error_ids = err_ids
                target_texts = [results_map.get(s.id, "") for s in segments]
            else:
                try:
                    target_texts = await translation_service.batch_translate(
                        source_texts,
                        src=video.source_lang or "zh-cn",
                        dest=video.target_lang or "vi",
                        engine=engine,
                        should_stop=lambda: _is_cancel_requested(video_id),
                    )
                except TranslationRateLimitError as e:
                    logger.warning(
                        f"Dịch bị giới hạn tần suất. Đang lưu phần đã dịch ({len(e.partial_results)} câu)."
                    )
                    target_texts = e.partial_results
                    is_partial = True

            dirs = get_project_dirs(video_id, db)
            any_text_changed = False

            for i, text in enumerate(target_texts):
                # Only save if it's not a special [RateLimit:], [Stopped:] or [Error:] tag
                # unless it was already an error.
                if text and not any(tag in text for tag in ["[RateLimit:", "[Stopped:", "[Error:"]):
                    new_fallback = segments[i].id in fallback_id_set
                    if segments[i].target_text != text:
                        segments[i].target_text = text
                        any_text_changed = True
                        seg_path = os.path.join(
                            dirs["segments"], f"seg_{segments[i].id}.mp3"
                        )
                        if os.path.exists(seg_path):
                            try:
                                os.remove(seg_path)
                            except:
                                pass
                        segments[i].audio_path = None  # Clear DB path as file is gone
                        bo_noi_khung(segments[i])
                    # Cập nhật cờ fallback (dịch lại bằng Google/DeepSeek OK -> False).
                    if bool(segments[i].translation_fallback) != new_fallback:
                        segments[i].translation_fallback = new_fallback

            if any_text_changed:
                video.p4_status = "pending"
                video.p5_status = "pending"

            # HỦY giữa chừng: các câu ĐÃ dịch xong đã được LƯU ở vòng trên. Đưa p3 về
            # pending (chưa đủ) và trả cancelled — re-run chỉ dịch các câu còn thiếu.
            if _is_cancel_requested(video_id):
                if not segment_ids:
                    video.p3_status = "pending"
                db.commit()
                sync_project_to_disk(video_id, db)
                logger.info("=== ĐÃ HỦY DỊCH — đã lưu phần đã dịch ===")
                return {"status": "cancelled"}

            if is_partial:
                video.p3_status = "error"
                video.status = "translation_interrupted"
                db.commit()
                sync_project_to_disk(video_id, db)
                raise HTTPException(
                    status_code=429,
                    detail=L("Google Translate đã chặn do gửi quá nhiều (lỗi 429). Phần đã dịch đã được lưu. Vui lòng đợi vài phút rồi thử lại.", "Google Translate blocked you for sending too many requests (error 429). The translated part has been saved. Please wait a few minutes and try again."),
                )

            # BỘ AI: cảnh báo/tổng kết fallback + đoạn lỗi trong log để người dùng theo dõi.
            if provider_of(engine):
                if fallback_id_set:
                    logger.warning(
                        f"⚠️ {len(fallback_id_set)} đoạn {ten_bo_ai(engine)} lỗi đã tự chuyển sang "
                        f"Google Dịch (xem cảnh báo trên giao diện để kiểm tra lại)."
                    )
                if deepseek_error_ids:
                    logger.error(
                        f"{len(deepseek_error_ids)} đoạn KHÔNG dịch được (cả {ten_bo_ai(engine)} lẫn Google)."
                    )

            # THẤT BẠI TOÀN BỘ: không có đoạn nào dịch được -> KHÔNG đánh dấu "translated"
            # (trước đây vẫn set completed dù rỗng, gây hiển thị 'translated' sai khi lỗi).
            # CHỈ đếm trên các đoạn có source KHÔNG rỗng — đoạn source rỗng (Whisper trả
            # whitespace) hợp lệ trả về rỗng, không được tính là "thất bại" (tránh video
            # đã dịch xong bị flip sang error chỉ vì re-run chọn đúng 1 đoạn rỗng).
            nonempty_total = sum(
                1 for s in segments if (s.source_text or "").strip()
            )
            translated_now = sum(
                1
                for s, t in zip(segments, target_texts)
                if (s.source_text or "").strip()
                and t
                and not any(tag in t for tag in ["[RateLimit:", "[Stopped:", "[Error:"])
            )
            if translated_now == 0 and nonempty_total > 0:
                if not segment_ids:
                    video.p3_status = "error"
                    video.status = "translation_failed"
                db.commit()
                sync_project_to_disk(video_id, db)
                logger.error("=== DỊCH THẤT BẠI: không dịch được đoạn nào ===")
                raise HTTPException(
                    status_code=502,
                    detail=L("Dịch thất bại — không đoạn nào được dịch (kiểm tra API key DeepSeek / kết nối mạng). Xem nhật ký để biết chi tiết.", "Translation failed — no segment was translated (check the DeepSeek API key / network connection). See the log for details."),
                )

            # THẤT BẠI MỘT PHẦN (DeepSeek + Google đều lỗi vài đoạn): giữ phần đã dịch
            # nhưng KHÔNG đánh dấu hoàn tất — để người dùng bấm Dịch lại phần thiếu,
            # tránh mang đoạn rỗng sang bước TTS mà không hay biết (đồng bộ với hành vi
            # 429 của nhánh Google: partial -> error + HTTPException).
            if deepseek_error_ids:
                if not segment_ids:
                    video.p3_status = "error"
                    video.status = "translation_interrupted"
                db.commit()
                sync_project_to_disk(video_id, db)
                raise HTTPException(
                    status_code=502,
                    detail=(
                        L(f"{len(deepseek_error_ids)} đoạn không dịch được (DeepSeek lẫn Google đều lỗi). "
                          "Phần đã dịch đã được lưu — bấm Dịch để thử lại các đoạn còn thiếu.",
                          f"{len(deepseek_error_ids)} segments could not be translated (both DeepSeek and Google failed). "
                          "The translated part has been saved — click Translate to retry the missing segments.")
                    ),
                )

            if not segment_ids:
                video.p3_status = "completed"
                video.status = "translated"

            db.commit()
            sync_project_to_disk(video_id, db)
            logger.info("=== DỊCH HOÀN TẤT ===")
            return {
                "status": "success",
                "processed_count": len(segments),
                "first_index": segments[0].index if segments else 0,
            }
        except HTTPException:
            raise
        except Exception as e:
            if not segment_ids:
                video.p3_status = "error"
                db.commit()
            logger.error(f"Dịch thất bại: {e}")
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        _clear_cancel(video_id)
        remove_project_logging(handler)
        _release_step(video_id)


@router.post("/{video_id}/reset-translations")
async def reset_translations(video_id: str, db: Session = Depends(get_db)):
    db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).update(
        {SubtitleSegment.target_text: ""}, synchronize_session=False
    )
    video = db.query(Video).filter(Video.id == video_id).first()
    if video:
        video.p3_status = "pending"
        video.status = "stt_completed"  # Back to previous major step
    db.commit()
    sync_project_to_disk(video_id, db)
    return {"status": "success"}


@router.post("/{video_id}/tts")
async def generate_voice(
    video_id: str,
    voice_talent: Optional[str] = Query(None),
    speed_mode: Optional[str] = Query(None),
    bg_volume: Optional[float] = Query(None),
    voice_bg_volume: Optional[float] = Query(None),
    separate_audio: Optional[bool] = Query(None),
    vocals_volume: Optional[float] = Query(None),
    voice_vocals_volume: Optional[float] = Query(None),
    force_reset: bool = Query(False),
    data: dict = Body(None),
    db: Session = Depends(get_db),
):
    # Không tạo giọng khi dữ liệu đang được DI CHUYỂN sang vị trí lưu mới.
    from routes.storage_routes import is_migrating
    if is_migrating():
        raise HTTPException(status_code=409, detail=L("Đang di chuyển dữ liệu sang vị trí lưu mới — chờ xong rồi hãy chạy.", "Data is being moved to the new storage location — wait for it to finish before running."))
    # Chan chay chong: 1 video chi 1 buoc nang (tts/merge) tai 1 thoi diem.
    _acquire_or_409(video_id, "tts")
    handler = setup_project_logging(video_id, db)
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

        # Đổi giọng / tốc độ → đánh dấu cần SINH LẠI toàn bộ, NHƯNG KHÔNG xóa file cũ
        # ngay. should_overwrite=True (bên dưới) khiến mọi đoạn được ghi đè; generate_voice
        # ghi vào file tạm rồi os.replace atomic — nên từng giọng cũ chỉ bị thay khi ĐÃ CÓ
        # bản mới. Người dùng bấm đổi rồi hủy giữa chừng vẫn giữ được các giọng chưa regen.
        # (Chỉ reset trạng thái DB để không dùng lại giọng cũ trong bản render kế tiếp.)
        def _wipe_all_segment_audio(reason: str):
            for seg in (
                db.query(SubtitleSegment)
                .filter(SubtitleSegment.video_id == video_id)
                .all()
            ):
                seg.is_trimmed = False
            video.p4_status = "pending"
            if video.p5_status == "completed":
                video.p5_status = "pending"
            logger.info(
                f"Đổi cấu hình lồng tiếng ({reason}): sẽ sinh lại toàn bộ giọng "
                f"(ghi đè atomic, giữ giọng cũ tới khi có bản mới)"
            )

        voice_changed = bool(
            voice_talent and voice_talent != (video.voice_talent or "")
        )
        speed_changed = bool(
            speed_mode and speed_mode != (video.speed_mode or "")
        )
        if voice_changed or speed_changed:
            reasons = []
            if voice_changed:
                reasons.append(
                    f"doi giong {video.voice_talent!r} -> {voice_talent!r}"
                )
            if speed_changed:
                reasons.append(
                    f"doi toc do {video.speed_mode!r} -> {speed_mode!r}"
                )
            _wipe_all_segment_audio(", ".join(reasons))

        # Update model values
        if voice_talent:
            video.voice_talent = voice_talent
        if speed_mode:
            video.speed_mode = speed_mode
        if bg_volume is not None:
            video.bg_volume = bg_volume
        if voice_bg_volume is not None:
            video.voice_bg_volume = voice_bg_volume
        if separate_audio is not None:
            video.separate_audio = separate_audio
        if vocals_volume is not None:
            video.vocals_volume = vocals_volume
        if voice_vocals_volume is not None:
            video.voice_vocals_volume = voice_vocals_volume

        if any(
            [
                voice_talent,
                speed_mode,
                bg_volume is not None,
                voice_bg_volume is not None,
                separate_audio is not None,
                vocals_volume is not None,
                voice_vocals_volume is not None,
                voice_changed,
                speed_changed,
            ]
        ):
            db.commit()

        # Only overwrite if explicitly requested OR vừa đổi giọng/tốc độ (đã wipe)
        should_overwrite = force_reset or (
            data.get("force_reset", False) if data else False
        ) or voice_changed or speed_changed

        _clear_cancel(video_id)  # xoá cờ hủy còn sót từ lần trước
        logger.info(f"=== BẮT ĐẦU LỒNG TIẾNG CHO VIDEO {video_id} (Giọng: {voice_talent or video.voice_talent}) ===")
        if should_overwrite:
            logger.info("Tạo lại toàn bộ giọng (ghi đè / sau khi đổi giọng hoặc tốc độ)")
        else:
            logger.info("Lồng tiếng bổ sung: chỉ tạo lại những câu còn thiếu tệp âm thanh")

        # Extract data safely
        segment_ids = data.get("segment_ids") if data else None

        if segment_ids:
            segments = (
                db.query(SubtitleSegment)
                .filter(
                    SubtitleSegment.video_id == video_id,
                    SubtitleSegment.id.in_(segment_ids),
                )
                .all()
            )
        else:
            segments = (
                db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).all()
            )

        if not segments:
            logger.info("Không có câu nào cần lồng tiếng.")
            return {"status": "success", "message": L("Không có câu nào cần xử lý", "No sentences need processing")}

        #  Mốc kết thúc GỐC (trước khi app tự nới): có ``goc_end_time`` thì dùng nó, không
        #  thì end hiện tại. Tính khung từ mốc gốc để sinh lại giọng ngắn hơn thì end CO VỀ
        #  theo giọng — không kẹt ở mốc đã nới lần trước.
        def _cuoi_goc(seg) -> float:
            return seg.goc_end_time if seg.goc_end_time is not None else seg.end_time

        #  Nới khung giọng tới điểm bắt đầu câu kế (xem ``khung_toi_da_cua_cau``).
        _moc_toi_da = khung_toi_da_cua_cau(
            [
                (s.id, s.start_time, _cuoi_goc(s))
                for s in db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).all()
            ],
            float(video.duration or 0),
        )

        #  CHO GIONG TRAN SANG CAU SAU (người dùng chốt 07-10-2026): khung đích của giọng = khung + ``tran`` giây,
        #  tức "Tự động khớp" ép tốc độ ít hơn và chỉ cắt khi vượt cả phần tràn; bước ghép đặt nguyên tệp tại ``start``
        #  nên phần tràn tự đè lên đầu câu sau (cộng dồn + limiter). ``end_time`` vẫn không bao giờ vượt đầu câu sau.
        tran_co_ban = _tran_cua_video(video)
        if tran_co_ban > 0:
            logger.info(f"Cho giọng tràn sang câu sau tối đa {tran_co_ban:.1f}s trước khi ép tốc độ / cắt.")

        def _khung(seg) -> float:
            goc = _cuoi_goc(seg) - seg.start_time
            if getattr(seg, "giu_khung_goc", None):
                return goc + tran_co_ban          # "Khôi phục khung gốc": giọng vừa khung gốc (+ phần được tràn)
            return max(goc, _moc_toi_da.get(seg.id, _cuoi_goc(seg)) - seg.start_time) + tran_co_ban

        def _keo_dai_diem_ket_thuc(ds) -> None:
            """Sau khi sinh giọng: kéo end của câu cho vừa giọng thật (không quá câu kế,
            KHÔNG đổi start). Ghi log tổng số giây khoảng lặng đã tận dụng."""
            from mutagen.mp3 import MP3

            so_cau, tong, so_co = 0, 0.0, 0
            for seg in ds:
                sp = os.path.join(dirs["segments"], f"seg_{seg.id}.mp3")
                if not (os.path.exists(sp) and os.path.getsize(sp) > 0):
                    continue
                try:
                    dai = MP3(sp).info.length
                except Exception:
                    continue
                them = cap_nhat_end_sau_giong(seg, _moc_toi_da.get(seg.id, _cuoi_goc(seg)), dai)
                if them > 0:
                    tong += them
                    so_cau += 1
                elif them < 0:
                    so_co += 1
            if so_co:
                logger.info(f"Giọng mới vừa khung gốc: {so_co} câu co điểm kết thúc về như cũ.")
            if so_cau:
                logger.info(
                    f"Tận dụng khoảng lặng: kéo dài điểm kết thúc {so_cau} câu (tổng {tong:.1f}s) "
                    "để giọng không phải tăng tốc — điểm bắt đầu giữ nguyên."
                )

        if segment_ids:
            logger.info(
                f"Lồng tiếng: tạo lại riêng {len(segment_ids)} câu theo yêu cầu người dùng"
            )
            if video.p5_status == "completed":
                video.p5_status = "pending"
                db.commit()

        if not segment_ids:
            video.p4_status = "processing"
            db.commit()

        dirs = get_project_dirs(video_id, db)
        effective_speed = video.speed_mode or "normal"

        # Map speed mode -> rate (%). ĐỒNG BỘ HOÀN TOÀN với che do Review (13 muc:
        # auto + 12 muc toc do). Tran that = 1.8x (VieneuService.speedup_mp3 cap 1.8).
        # Dung key dang "sNNN" theo boi so (s090=0.9x ... s180=1.8x) cho de doc.
        speed_map = {
            "s090": "-10%",        # 0.9x
            "normal": "+0%",       # 1.0x
            "s110": "+10%",        # 1.1x
            "s120": "+20%",        # 1.2x
            "s130": "+30%",        # 1.3x
            "s140": "+40%",        # 1.4x
            "s150": "+50%",        # 1.5x
            "s160": "+60%",        # 1.6x
            "s165": "+65%",        # 1.65x
            "s170": "+70%",        # 1.7x
            "s175": "+75%",        # 1.75x
            "s180": "+80%",        # 1.8x
        }

        from services.vieneu_service import is_vieneu_voice, VieneuService
        from utils.hardware_utils import get_best_device

        _tts_device, _ = get_best_device()

        # Whisper + VieNeu GPU cung tranh VRAM (3050 4GB de treo sau upgrade clone).
        if _tts_device == "cuda" and is_vieneu_voice(video.voice_talent):
            await asyncio.to_thread(stt_service.release_gpu)

        def _seg_path(seg):
            return os.path.join(dirs["segments"], f"seg_{seg.id}.mp3").replace("\\", "/")

        # Batch CHI loi tren GPU (chay song song that). Tren CPU, infer_batch phai
        # pad ve cau dai nhat + CPU da dung het loi -> khong nhanh hon, con phi.
        # Nen CPU dung duong tuan tu (co progress tung cau) o nhanh else ben duoi.
        if is_vieneu_voice(video.voice_talent) and _tts_device == "cuda":
            # === VieNeu + GPU: sinh theo BATCH (infer_batch) roi khop thoi luong ===
            gen_items = []   # (text, path) can sinh moi
            fit_specs = []   # (path, target_duration, seg_speed)
            skipped = 0
            for idx, seg in enumerate(segments, start=1):
                if not seg.target_text:
                    continue
                # Đoạn OCR vá lỗ (Whisper không nghe được): KHÔNG sinh voice hàng loạt.
                # Chỉ sinh khi người dùng yêu cầu ĐÍCH DANH (segment_ids) cho câu đó.
                if getattr(seg, "ocr_filled", False) and not segment_ids:
                    continue
                seg_path = _seg_path(seg)
                seg_speed = seg.speed_mode or effective_speed
                target_duration = _khung(seg)
                # Pre-generation duration validation
                text_len = len(seg.target_text or "")
                if target_duration > 0 and text_len > target_duration * 15:
                    logger.warning(
                        f"Câu [{idx}/{len(segments)}]: nội dung ({text_len} ký tự) hơi dài so với "
                        f"thời lượng câu ({target_duration:.2f}s), giọng đọc sẽ được tăng tốc và cắt bớt cho vừa"
                    )
                need = should_overwrite or not (
                    os.path.exists(seg_path) and os.path.getsize(seg_path) > 0
                )
                if not need:
                    skipped += 1
                    logger.info(
                        f"Câu [{idx}/{len(segments)}]: đã có giọng đọc sẵn, bỏ qua"
                    )
                    continue
                gen_items.append((lam_sach_cau_doc(seg.target_text), seg_path))     # lô VieNeu-GPU không qua tts_service.generate_voice
                fit_specs.append((seg.id, seg_path, target_duration, seg_speed))

            if gen_items:
                # Batch VieNeu-GPU không chia nhỏ để hủy giữa chừng → kiểm hủy TRƯỚC
                # khi bắt đầu batch và trước khi khớp thời lượng (mốc coarse; batch
                # đang chạy vẫn phải xong nốt).
                _check_cancel(video_id)
                logger.info(f"Đang tạo giọng đọc cho {len(gen_items)} câu (bỏ qua {skipped} câu đã có sẵn)...")

                # Fit tốc độ + LƯU audio_path TỪNG CÂU ngay khi encode MP3 xong
                # (callback on_clip_done chạy trên luồng encode-pool của
                # synthesize_batch) — KHÔNG đợi hết batch. FE poll (4s) thấy
                # audio_path -> icon nghe thử tự bật trong lúc GPU còn sinh các
                # mini-batch sau (giống đường Edge/CPU vốn đã lưu ngay từng câu).
                # _fit_all phía dưới chỉ QUÉT VÉT câu callback bỏ sót (idempotent
                # nhờ fitted_ids + lock — fit 2 lần sẽ tăng tốc audio 2 lần).
                from models.database import SessionLocal
                specs_by_path = {
                    os.path.normcase(os.path.abspath(spec[1])): spec for spec in fit_specs
                }
                fitted_lock = _threading.Lock()
                fitted_ids: set = set()
                trim_results: dict = {}

                def _fit_one(spec):
                    seg_id, path, target_dur, seg_speed = spec
                    with fitted_lock:
                        if seg_id in fitted_ids:
                            return seg_id, trim_results.get(seg_id, False)
                        fitted_ids.add(seg_id)
                    is_trimmed = False
                    if not (os.path.exists(path) and os.path.getsize(path) > 0):
                        # Chưa/không có file -> nhả lại để _fit_all thử vét sau.
                        with fitted_lock:
                            fitted_ids.discard(seg_id)
                        return seg_id, False
                    if seg_speed == "auto_fit":
                        is_trimmed = VieneuService.fit_to_duration(path, target_dur)
                    else:
                        # TỐC ĐỘ CỐ ĐỊNH: áp đúng hệ số % đã chọn (KHÔNG tự tăng
                        # tốc để khớp), nhưng VẪN cắt nếu dài hơn khung + gắn cờ
                        # is_trimmed để người dùng biết đoạn đã bị cắt cho vừa.
                        pct = speed_map.get(seg_speed, "+0%")
                        try:
                            factor = 1.0 + int(pct.strip("+%")) / 100.0
                        except Exception:
                            factor = 1.0
                        # abs(): cho phép CẢ tốc độ chậm (s090 = 0.9x) — trước
                        # đây `> 1.01` làm slowdown bị bỏ qua âm thầm.
                        if abs(factor - 1.0) > 0.01:
                            VieneuService.speedup_mp3(path, factor)
                        try:
                            from mutagen.mp3 import MP3
                            from services.tts_service import TRIM_WARN_THRESHOLD_SEC
                            pre_trim_dur = MP3(path).info.length
                            if pre_trim_dur > target_dur + 0.05:
                                VieneuService._trim_mp3(path, target_dur)
                                is_trimmed = (pre_trim_dur - target_dur) > TRIM_WARN_THRESHOLD_SEC
                        except Exception:
                            pass
                    with fitted_lock:
                        trim_results[seg_id] = is_trimmed
                    return seg_id, is_trimmed

                def _on_clip_done(out_path):
                    spec = specs_by_path.get(os.path.normcase(os.path.abspath(out_path)))
                    if spec is None:
                        return
                    seg_id, sp = spec[0], spec[1]
                    _fit_one(spec)
                    if not (os.path.exists(sp) and os.path.getsize(sp) > 0):
                        return
                    # Session RIÊNG cho luồng encode-pool: `db` của request KHÔNG
                    # thread-safe. Chỉ ghi audio_path + is_trimmed cho đúng 1 row.
                    try:
                        s = SessionLocal()
                        try:
                            row = s.query(SubtitleSegment).filter(SubtitleSegment.id == seg_id).first()
                            if row is not None:
                                row.audio_path = sp.replace("\\", "/")
                                row.is_trimmed = trim_results.get(seg_id, False)
                                s.commit()
                        finally:
                            s.close()
                    except Exception as e:
                        logger.warning(f"Không lưu sớm được audio_path cho câu {seg_id} (sẽ lưu lại cuối batch): {e}")

                # Truyền check_cancel để synthesize_batch kiểm cờ hủy in-memory
                # GIỮA các mini-batch (pipeline lồng tiếng không có marker file
                # .cancel_render nên project_dir không giúp hủy được).
                await asyncio.to_thread(
                    VieneuService.synthesize_batch,
                    gen_items,
                    video.voice_talent,
                    check_cancel=lambda: _is_cancel_requested(video_id),
                    on_clip_done=_on_clip_done,
                )
                _check_cancel(video_id)

                def _fit_all():
                    import concurrent.futures as cf
                    workers = min(8, max(2, (os.cpu_count() or 4)))
                    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                        return list(pool.map(_fit_one, fit_specs))

                fit_results = await asyncio.to_thread(_fit_all)
                # Update DB with is_trimmed
                trim_map = dict(fit_results)
                for seg in segments:
                    if seg.id in trim_map:
                        seg.is_trimmed = trim_map[seg.id]

                logger.info("Đã tạo xong giọng đọc và khớp thời lượng cho từng câu.")
            else:
                logger.info(f"Mọi câu đều đã có giọng đọc sẵn, bỏ qua ({skipped} câu).")
        else:
            # === Edge-TTS / VieNeu CPU: sinh song song tung segment (bo qua file da co) ===
            tasks = []
            task_meta = []  # (idx, seg_id, preview)
            total_segs = len(segments)
            skipped = 0
            for idx, seg in enumerate(segments, start=1):
                if not seg.target_text:
                    continue
                # Đoạn OCR vá lỗ (Whisper không nghe được): KHÔNG sinh voice hàng loạt.
                # Chỉ sinh khi người dùng yêu cầu ĐÍCH DANH (segment_ids) cho câu đó.
                if getattr(seg, "ocr_filled", False) and not segment_ids:
                    continue
                seg_path = _seg_path(seg)
                seg_speed = seg.speed_mode or effective_speed
                target_duration = _khung(seg)
                # Pre-generation duration validation
                text_len = len(seg.target_text or "")
                if target_duration > 0 and text_len > target_duration * 15:
                    logger.warning(
                        f"Câu [{idx}/{total_segs}]: nội dung ({text_len} ký tự) hơi dài so với "
                        f"thời lượng câu ({target_duration:.2f}s), giọng đọc sẽ được tăng tốc và cắt bớt cho vừa"
                    )
                need = should_overwrite or not (
                    os.path.exists(seg_path) and os.path.getsize(seg_path) > 0
                )
                if not need:
                    skipped += 1
                    logger.info(
                        f"Câu [{idx}/{total_segs}]: đã có giọng đọc sẵn, bỏ qua"
                    )
                    continue
                preview = (seg.target_text or "").replace("\n", " ")[:60]
                if seg_speed == "auto_fit":
                    tasks.append(
                        tts_service.generate_voice_auto_fit(
                            seg.target_text, seg_path, target_duration,
                            voice=video.voice_talent, overwrite=should_overwrite,
                            base_rate="auto",
                        )
                    )
                else:
                    # TỐC ĐỘ CỐ ĐỊNH: gen đúng rate đã chọn (KHÔNG tự tăng tốc để
                    # khớp), nhưng VẪN truyền target_duration -> generate_voice sẽ cắt
                    # nếu dài hơn khung + gắn cờ is_trimmed để người dùng biết.
                    rate = speed_map.get(seg_speed, "+0%")
                    tasks.append(
                        tts_service.generate_voice(
                            seg.target_text, seg_path,
                            voice=video.voice_talent, rate=rate,
                            overwrite=should_overwrite, target_duration=target_duration,
                        )
                    )
                task_meta.append((idx, seg, preview))

            if tasks:
                completed = 0
                total = len(tasks)
                logger.info(
                    f"Lồng tiếng: bắt đầu tạo {total} câu (bỏ qua {skipped} câu đã có sẵn)"
                )

                async def track_progress(task_item, meta):
                    nonlocal completed
                    # Checkpoint hủy TRƯỚC khi bắt đầu đoạn này (đoạn chưa chạy sẽ bỏ).
                    # Phải ĐÓNG coroutine của chính đoạn này trước khi raise: nó đã
                    # được tạo nhưng chưa await, bỏ luôn sẽ sinh cảnh báo
                    # "coroutine was never awaited" (mỗi lần hủy vài trăm dòng rác).
                    try:
                        _check_cancel(video_id)
                    except _PipelineCancelled:
                        task_item.close()
                        raise
                    res = await task_item

                    # Handle tuple return from generate_voice_auto_fit
                    success = res
                    is_trimmed = False
                    if isinstance(res, tuple):
                        success, is_trimmed = res

                    completed += 1
                    idx, seg, preview = meta
                    seg.is_trimmed = is_trimmed

                    # LƯU audio_path NGAY khi đoạn xong + commit → FE poll (4s) thấy file
                    # để NGHE THỬ ngay trong lúc các đoạn khác còn đang sinh.
                    if success:
                        sp = _seg_path(seg)
                        if os.path.exists(sp) and os.path.getsize(sp) > 0:
                            seg.audio_path = sp.replace("\\", "/")
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()

                    # 1 dòng log gọn cho mỗi đoạn HOÀN TẤT (thay 2 dòng cũ; thứ tự theo
                    # lúc xong thật vì chạy song song).
                    if success:
                        logger.info(
                            f"Đã tạo xong giọng đọc [{completed}/{total}]: \"{preview}\""
                        )
                    else:
                        logger.info(
                            f"Tạo giọng đọc THẤT BẠI [{completed}/{total}]: \"{preview}\""
                        )
                    # Checkpoint hủy SAU mỗi đoạn xong → dừng ngay, đoạn đã xong vẫn giữ file.
                    _check_cancel(video_id)
                    return success

                # CHIA LO — SUA LOI "video vai nghin cau chay THIEU".
                #
                # Truoc day: tao HET coroutine cho ca 2000+ cau roi gather mot lan.
                # Semaphore(35) trong tts_service chi chan ben TRONG, nen ca 2000
                # request van cung xep hang dap vao Edge-TTS -> Microsoft rate-limit
                # ca cum -> hang loat cau het luot thu lai -> thieu file.
                #
                # Nay chay theo lo _VOICE_BATCH_SIZE cau, xong lo nay moi sang lo ke.
                # Cham hon mot chut nhung CHAY DU. Cung giam manh bo nho (2000
                # coroutine + task song song la khong nho).
                #
                # LUU Y khi sua: coroutine trong `tasks` da duoc TAO san o vong lap
                # tren. Coroutine chua await ma bi bo se ro ri "never awaited" —
                # nen nhanh huy PHAI dong (close) cac coroutine con lai o duoi.
                cancelled_exc = None
                for start in range(0, len(tasks), _VOICE_BATCH_SIZE):
                    chunk_tasks = tasks[start:start + _VOICE_BATCH_SIZE]
                    chunk_meta = task_meta[start:start + _VOICE_BATCH_SIZE]
                    tracked_tasks = [
                        asyncio.ensure_future(track_progress(t, m))
                        for t, m in zip(chunk_tasks, chunk_meta)
                    ]
                    try:
                        await asyncio.gather(*tracked_tasks)
                    except _PipelineCancelled as e:
                        # Hủy: dừng các đoạn còn lại ngay (đoạn đang chạy/đã xong không bị mất file).
                        for f in tracked_tasks:
                            f.cancel()
                        await asyncio.gather(*tracked_tasks, return_exceptions=True)
                        # Đóng coroutine các lô CHƯA chạy -> tránh cảnh báo
                        # "coroutine was never awaited" khi hủy giữa chừng.
                        for c in tasks[start + _VOICE_BATCH_SIZE:]:
                            c.close()
                        cancelled_exc = e
                        break
                if cancelled_exc is not None:
                    raise cancelled_exc
            elif skipped:
                logger.info(f"Lồng tiếng: tất cả {skipped} câu đã có âm thanh, không cần tạo lại.")

        # === SINH BÙ các câu còn thiếu (rate-limit thoáng qua / mạng chập chờn) ===
        # Lượt chính có thể sót vài câu dù đã chia lô + backoff. Số câu sót thường
        # rất ít nên sinh lại rất nhanh — làm luôn ở đây thay vì bắt người dùng tự
        # bấm "Lồng tiếng" lần nữa (trước đây chỉ cảnh báo rồi thôi).
        #
        # Chỉ sinh ĐÚNG câu chưa có file. KHÔNG dùng overwrite -> câu đã xong không
        # bị đụng tới. Vòng nào không cứu thêm được câu nào thì dừng sớm.
        # Chỉ áp dụng cho nhánh Edge-TTS/VieNeu-CPU (nhánh sinh song song qua mạng).
        # Nhánh VieNeu + GPU đi đường batch riêng, không dính rate-limit mạng.
        if not (is_vieneu_voice(video.voice_talent) and _tts_device == "cuda"):
            for _round in range(1, _VOICE_REPAIR_ROUNDS + 1):
                _check_cancel(video_id)
                _missing = [
                    seg for seg in segments
                    if seg.target_text
                    and not (getattr(seg, "ocr_filled", False) and not segment_ids)
                    and not (
                        os.path.exists(_seg_path(seg))
                        and os.path.getsize(_seg_path(seg)) > 0
                    )
                ]
                if not _missing:
                    break
                logger.info(
                    f"Lồng tiếng: sinh bù {len(_missing)} câu còn thiếu "
                    f"(vòng {_round}/{_VOICE_REPAIR_ROUNDS})..."
                )
                for _start in range(0, len(_missing), _VOICE_BATCH_SIZE):
                    _check_cancel(video_id)
                    _chunk = _missing[_start:_start + _VOICE_BATCH_SIZE]
                    _repair = []
                    for _seg in _chunk:
                        _sp = _seg_path(_seg)
                        _spd = _seg.speed_mode or effective_speed
                        _dur = _khung(_seg)
                        if _spd == "auto_fit":
                            _repair.append(tts_service.generate_voice_auto_fit(
                                _seg.target_text, _sp, _dur,
                                voice=video.voice_talent, overwrite=False,
                                base_rate="auto",
                            ))
                        else:
                            _repair.append(tts_service.generate_voice(
                                _seg.target_text, _sp,
                                voice=video.voice_talent,
                                rate=speed_map.get(_spd, "+0%"),
                                overwrite=False, target_duration=_dur,
                            ))
                    await asyncio.gather(*_repair, return_exceptions=True)
                _after = sum(
                    1 for seg in _missing
                    if os.path.exists(_seg_path(seg)) and os.path.getsize(_seg_path(seg)) > 0
                )
                logger.info(f"Lồng tiếng: sinh bù cứu được {_after}/{len(_missing)} câu.")
                if _after == 0:
                    # Không cứu được câu nào -> lỗi không phải do rate-limit tạm
                    # thời (mất mạng / sai giọng). Thử thêm vòng nữa cũng vô ích.
                    break

        # Chỉ gán audio_path cho đoạn CÓ file thật (không gán bừa cho đoạn chưa sinh
        # / text rỗng / lỗi) → tránh đếm "đã lồng tiếng" sai + p4 báo completed nhưng
        # thiếu file. Đoạn không có file → audio_path = None.
        _voiced = 0
        for seg in segments:
            _sp = os.path.join(dirs["segments"], f"seg_{seg.id}.mp3")
            if os.path.exists(_sp) and os.path.getsize(_sp) > 0:
                seg.audio_path = _sp.replace("\\", "/")
                _voiced += 1
            else:
                seg.audio_path = None
        _keo_dai_diem_ket_thuc(segments)

        # ĐẾM SỐ CÂU THẤT BẠI (câu lẽ ra phải có giọng nhưng không có file) — không
        # gắn "completed" mù quáng khi nhiều/tất cả câu lỗi.
        _eligible = [
            seg for seg in segments
            if seg.target_text and not (getattr(seg, "ocr_filled", False) and not segment_ids)
        ]
        _failed = sum(1 for seg in _eligible if not seg.audio_path)
        if _failed:
            logger.warning(
                f"CẢNH BÁO: {_failed}/{len(_eligible)} câu tạo giọng THẤT BẠI (không có file âm thanh). "
                f"Hãy chạy lại Lồng tiếng để sinh bù các câu còn thiếu."
            )

        if not segment_ids:
            if _eligible and _failed == len(_eligible):
                # TẤT CẢ câu đều thất bại -> báo lỗi rõ ràng thay vì "hoàn tất" ảo.
                video.p4_status = "error"
                db.commit()
                sync_project_to_disk(video_id, db)
                _msg = (
                    f"Tạo giọng đọc THẤT BẠI cho toàn bộ {len(_eligible)} câu. "
                    f"Vui lòng kiểm tra kết nối mạng / model giọng đọc rồi thử lại."
                )
                logger.error(_msg)
                raise HTTPException(status_code=400, detail=_msg)
            video.p4_status = "completed"
            video.status = "voice_generated"

        db.commit()
        sync_project_to_disk(video_id, db)
        # Đếm TẠM số voice sinh trong lượt này theo dự án; sẽ gộp vào thống kê khi dự
        # án HOÀN THÀNH (record_completion). Chỉ ghi local, không gửi ngay.
        try:
            from services.usage_reporter import bump_voice
            if _voiced:
                bump_voice(str(video_id), _voiced)
        except Exception as _e:
            logger.warning(f"Không ghi được usage voice (dubbed): {_e}")
        if _failed:
            logger.info(f"=== ĐÃ TẠO GIỌNG ĐỌC: {_voiced} câu (CẢNH BÁO: {_failed} câu thất bại) ===")
        else:
            logger.info(f"=== ĐÃ TẠO XONG TOÀN BỘ GIỌNG ĐỌC: {_voiced} câu ===")
        return {"status": "success", "processed_count": len(segments), "failed_count": _failed}
    except _PipelineCancelled:
        # HỦY: LƯU cờ những đoạn ĐÃ sinh xong (file tồn tại) để lần chạy sau chỉ
        # sinh phần còn thiếu (incremental). Đoạn chưa có file → audio_path = None.
        saved = 0
        for seg in segments:
            sp = os.path.join(dirs["segments"], f"seg_{seg.id}.mp3")
            if os.path.exists(sp) and os.path.getsize(sp) > 0:
                seg.audio_path = sp.replace("\\", "/")
                saved += 1
            else:
                seg.audio_path = None
        _keo_dai_diem_ket_thuc(segments)
        if not segment_ids:
            video.p4_status = "pending"
        db.commit()
        sync_project_to_disk(video_id, db)
        logger.info(f"=== ĐÃ HỦY LỒNG TIẾNG — đã lưu {saved} đoạn hoàn tất ===")
        return {"status": "cancelled", "processed_count": saved}
    except HTTPException:
        # 404 (video not found) và các HTTPException khác phải nổi lên NGUYÊN VẸN —
        # nếu để rơi vào except Exception bên dưới sẽ thành NameError/500 che lỗi thật.
        raise
    except Exception as e:
        # CẬP NHẬT TRẠNG THÁI LỖI TRƯỚC — ghi crash log chỉ là best-effort (CWD có
        # thể là Program Files không ghi được -> PermissionError sẽ nuốt mất lỗi thật
        # và bỏ qua p4_status="error" nếu để ghi log trước).
        if not segment_ids:
            try:
                video.p4_status = "error"
                db.commit()
            except Exception:
                db.rollback()
        try:
            import traceback
            from models.database import get_app_data_dir
            _crash_log = os.path.join(get_app_data_dir(), "backend_crash.log")
            with open(_crash_log, "a", encoding="utf-8") as f:
                f.write("\n" + "="*50 + "\n")
                f.write("CRASH IN TTS ROUTE:\n")
                f.write(traceback.format_exc())
                f.write("="*50 + "\n")
        except Exception:
            pass
        logger.error(f"Lồng tiếng thất bại: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _clear_cancel(video_id)
        _release_step(video_id)
        remove_project_logging(handler)


@router.get("/{video_id}/output")
async def get_video_output(video_id: str, download: bool = False, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not video.final_video_path:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video hoàn thiện", "Finished video not found"))

    if not os.path.exists(video.final_video_path):
        raise HTTPException(status_code=404, detail=L("Tệp không còn trên ổ đĩa", "The file is no longer on disk"))
        
    return FileResponse(
        video.final_video_path,
        media_type="video/mp4",
        filename=f"master_{video_id}.mp4" if download else None,
        content_disposition_type="attachment" if download else "inline"
    )


@router.post("/{video_id}/merge")
def merge(
    video_id: str,
    bg_volume: Optional[float] = Query(None),
    voice_bg_volume: Optional[float] = Query(None),
    separate_audio: Optional[bool] = Query(None),
    vocals_volume: Optional[float] = Query(None),
    voice_vocals_volume: Optional[float] = Query(None),
    db: Session = Depends(get_db),
):
    # Không ghép khi dữ liệu đang được DI CHUYỂN sang vị trí lưu mới.
    from routes.storage_routes import is_migrating
    if is_migrating():
        raise HTTPException(status_code=409, detail=L("Đang di chuyển dữ liệu sang vị trí lưu mới — chờ xong rồi hãy chạy.", "Data is being moved to the new storage location — wait for it to finish before running."))
    # Chan chay chong: bam Merge lan 2 khi lan 1 (tach am + merge co the vai phut)
    # con chay se lam 2 luot ghi de nhau -> ket qua kho luong + cham gap doi.
    _acquire_or_409(video_id, "merge")
    handler = setup_project_logging(video_id, db)
    try:
        _clear_cancel(video_id)  # xoá cờ hủy còn sót từ lần trước
        logger.info(f"=== BẮT ĐẦU GHÉP ÂM THANH VÀO VIDEO {video_id} ===")
        video = db.query(Video).filter(Video.id == video_id).first()
        if bg_volume is not None:
            video.bg_volume = bg_volume
        if voice_bg_volume is not None:
            video.voice_bg_volume = voice_bg_volume
        if separate_audio is not None:
            video.separate_audio = separate_audio
        if vocals_volume is not None:
            video.vocals_volume = vocals_volume
        if voice_vocals_volume is not None:
            video.voice_vocals_volume = voice_vocals_volume

        if any(
            v is not None
            for v in [bg_volume, voice_bg_volume, separate_audio, vocals_volume, voice_vocals_volume]
        ):
            db.commit()
        dirs = get_project_dirs(video_id, db)
        # Kiểm dung lượng TRƯỚC khi ghép — video lớn hết đĩa giữa chừng ra file hỏng.
        from utils.storage_paths import check_render_disk_space
        try:
            check_render_disk_space(dirs["output"], (video.original_path,))
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Bật tách nhạc nền mà CHƯA tải Demucs -> chặn NGAY (log 2.2.0: ghép chạy
        # một lúc rồi mới chết giữa chừng, mất thời gian người dùng).
        effective_separate = (
            separate_audio if separate_audio is not None else bool(video.separate_audio)
        )
        if effective_separate:
            from utils.model_paths import get_demucs_repo_path
            if get_demucs_repo_path() is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        L("Đang bật 'Tách nhạc nền' nhưng chưa tải model Demucs. "
                          "Vào 'Quản lý Model AI' để tải, hoặc tắt tách nhạc nền rồi ghép lại.",
                          "'Separate background music' is on but the Demucs model has not been downloaded. "
                          "Go to 'AI Model Management' to download it, or turn off music separation and merge again.")
                    ),
                )

        video.p5_status = "processing"
        db.commit()

        segments = (
            db.query(SubtitleSegment)
            .filter(SubtitleSegment.video_id == video_id)
            .order_by(SubtitleSegment.index)
            .all()
        )
        output_path = os.path.join(dirs["output"], "final_master.mp4").replace(
            "\\", "/"
        )
        seg_data = [
            {
                "start_time": s.start_time,
                "end_time": s.end_time,
                "audio_path": s.audio_path,
                "bg_volume": s.bg_volume,
            }
            for s in segments
        ]

        # Tach giong goc / nhac nen (LAZY trong P5, co cache theo fingerprint
        # video nguon). SeparationError roi vao except chung -> p5_status=error.
        stems = None
        if video.separate_audio:
            from services.separation_service import (
                SeparationService, SeparationCancelled,
            )

            _check_cancel(video_id)
            logger.info(L("=== TÁCH GIỌNG GỐC VÀ NHẠC NỀN ===", "=== SEPARATING ORIGINAL VOICE AND BACKGROUND MUSIC ==="))
            try:
                stems = SeparationService.ensure_stems(
                    video.original_path, os.path.join(dirs["root"], "separated"),
                    check_cancel=lambda: _is_cancel_requested(video_id),
                )
            except SeparationCancelled:
                raise _PipelineCancelled()

        _check_cancel(video_id)
        from services.video_service import VideoMergeCancelled
        try:
            _merge_ok = VideoService.merge_audio_video(
                video.original_path,
                "",
                seg_data,
                output_path,
                bg_volume=video.bg_volume,
                voice_bg_volume=video.voice_bg_volume,
                separate_audio=bool(video.separate_audio),
                stems=stems,
                vocals_volume=video.vocals_volume if video.vocals_volume is not None else 0.0,
                voice_vocals_volume=video.voice_vocals_volume if video.voice_vocals_volume is not None else 0.0,
                # Cho phép HỦY giữa lúc ffmpeg đang chạy (poll 0.5s, kill + dọn file dở).
                check_cancel=lambda: _is_cancel_requested(video_id),
                tts_volume=_tts_volume_cua_video(video),
            )
        except VideoMergeCancelled:
            raise _PipelineCancelled()
        if _merge_ok:
            video.final_video_path = output_path
            video.p5_status = "completed"
            video.status = "completed"
            db.commit()
            sync_project_to_disk(video_id, db)
            # Ghi nhận 1 video lồng tiếng HOÀN THÀNH (sinh lại cũng tính). Gộp voice
            # đã sinh trong dự án này. Chỉ ghi local + gửi nền → không chặn/không lỗi.
            try:
                from services.usage_reporter import record_completion
                record_completion(str(video_id), "dubbed")
            except Exception as _e:
                logger.warning(f"Không ghi được usage (dubbed): {_e}")
            logger.info("=== GHÉP ÂM THANH VÀO VIDEO HOÀN TẤT ===")
            return {"status": "success"}
        raise Exception(L("Ghép âm thanh vào video thất bại", "Merging the audio into the video failed"))
    except _PipelineCancelled:
        # Merge tạo file master ở bước cuối → hủy giữa chừng KHÔNG có sản phẩm dở
        # để giữ; chỉ đưa trạng thái về pending để chạy lại.
        try:
            video.p5_status = "pending"
            db.commit()
        except Exception:
            pass
        logger.info("=== ĐÃ HỦY GHÉP ÂM THANH VÀO VIDEO THEO YÊU CẦU ===")
        return {"status": "cancelled"}
    except Exception as e:
        # db.commit() co the tu no nem loi (vd DB lock) -> van phai log duoc goc
        # loi va tra JSON, khong de chet im lang.
        logger.error(f"Ghép âm thanh vào video thất bại: {e}")
        try:
            video.p5_status = "error"
            db.commit()
        except Exception as e2:
            logger.error(f"Ghép âm thanh vào video thất bại (không cập nhật được trạng thái): {e2}")
        return JSONResponse(status_code=400, content={"detail": str(e)})
    finally:
        _clear_cancel(video_id)
        _release_step(video_id)
        remove_project_logging(handler)


@router.post("/{video_id}/export")
def export_custom_video(
    video_id: str, data: dict = Body(...), db: Session = Depends(get_db)
):
    handler = setup_project_logging(video_id, db)
    try:
        logger.info(f"=== BẮT ĐẦU XUẤT VIDEO {video_id} ===")
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            logger.error("Không tìm thấy video")
            raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

        aspect_ratio = data.get("aspect_ratio", "monitor")
        mirror = data.get("mirror", False)
        source_type = data.get("source_type", "master")  # "raw" or "master"

        input_video = (
            video.original_path if source_type == "raw" else video.final_video_path
        )
        if not input_video or not os.path.exists(input_video):
            logger.error("Video nguồn chưa sẵn sàng. Vui lòng chạy 'Ghép hoàn thiện' trước.")
            raise HTTPException(
                status_code=400,
                detail=L("Video nguồn chưa sẵn sàng. Vui lòng chạy 'Ghép hoàn thiện' trước.", "The source video is not ready. Please run 'Finalize' first."),
            )

        dirs = get_project_dirs(video_id, db)
        # Check if user provided a specific save path from the UI
        custom_path = data.get("output_path") or data.get("save_path")

        if custom_path:
            output_path = os.path.normpath(custom_path)
            export_filename = os.path.basename(output_path)
        else:
            export_filename = (
                data.get("filename")
                or f"export_{aspect_ratio}_{'mirrored' if mirror else 'original'}.mp4"
            )
            output_path = os.path.normpath(os.path.join(dirs["output"], export_filename))

        logger.info(f"Đường dẫn xuất tệp: {output_path}")

        # Kiểm dung lượng TRƯỚC khi xuất — tránh hết đĩa giữa chừng ra file hỏng.
        from utils.storage_paths import check_render_disk_space
        try:
            check_render_disk_space(os.path.dirname(output_path), (input_video,))
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if VideoService.render_final_export(
            input_video, output_path, aspect_ratio=aspect_ratio, mirror=mirror
        ):
            logger.info("=== XUẤT VIDEO HOÀN TẤT ===")
            return {
                "success": True,
                "filename": export_filename,
                "file_path": output_path.replace("\\", "/"),
            }

        logger.error("Xuất video thất bại")
        raise HTTPException(status_code=500, detail=L("Xuất video thất bại", "Video export failed"))
    finally:
        remove_project_logging(handler)


def _ap_mac_dinh_co_ban(db_video, db: Session) -> None:
    """Dự án Cơ bản MỚI -> áp "mặc định cho dự án mới" (services/mac_dinh_du_an.py): cột
    cài đặt của bảng ``videos`` + compositor_config.json. Gọi đúng lúc tạo, TRƯỚC khi ghi
    DB; lỗi thì bỏ qua — tạo dự án không được hỏng vì phần mặc định."""
    from services import mac_dinh_du_an as MD

    try:
        for k, v in MD.cai_dat("co_ban", MD.KHOA_CO_BAN).items():
            setattr(db_video, k, v)
        goc = get_project_dirs(db_video.id, db)["root"]
        MD.ap_dung_hinh("co_ban", goc)
        #  Nguồn phụ đề / kiểu nói chỉ sống ở giao diện (localStorage theo video) -> ghi
        #  vào THƯ MỤC DỰ ÁN MỚI để giao diện đọc một lần. Đọc thẳng từ kho mặc định mỗi
        #  khi video "chưa có lựa chọn" thì cả video CŨ mở lên cũng bị đổi theo.
        gd = MD.cai_dat("co_ban", MD.KHOA_CO_BAN_GIAO_DIEN)
        if gd:
            os.makedirs(goc, exist_ok=True)
            with open(os.path.join(goc, MAC_DINH_GIAO_DIEN_TEP), "w", encoding="utf-8") as f:
                json.dump(gd, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Không áp được mặc định cho dự án Cơ bản mới {db_video.id}: {e}")


MAC_DINH_GIAO_DIEN_TEP = "mac_dinh_giao_dien.json"


@router.get("/{video_id}/mac-dinh-giao-dien")
def mac_dinh_giao_dien(video_id: str, db: Session = Depends(get_db)):
    """Nguồn phụ đề / kiểu nói mặc định ghi lúc TẠO dự án này (rỗng = dự án không tạo
    kèm mặc định, vd dự án cũ)."""
    p = os.path.join(get_project_dirs(video_id, db)["root"], MAC_DINH_GIAO_DIEN_TEP)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d = d if isinstance(d, dict) else {}
        #  Chuỗi (nguồn phụ đề, kiểu nói) + bật/tắt (ô "Tách câu gốc").
        ra = {k: v for k, v in d.items() if isinstance(v, (str, bool))}
        #  Vùng OCR chỉ có ở DỰ ÁN MẪU (mặc định cho dự án mới không lưu vùng — vùng là của
        #  một video).
        if isinstance(d.get("ocr_region"), dict):
            ra["ocr_region"] = d["ocr_region"]
        return ra
    except (OSError, ValueError):
        return {}


class MacDinhCoBanBody(BaseModel):
    #  Lưu phần CÀI ĐẶT (ngôn ngữ, giọng, công cụ dịch, âm lượng…) không.
    cai_dat: bool = True
    #  Các phần DỰNG HÌNH được lưu (khoá của ``mau_cau_hinh.PHAN_MAU``); rỗng = không lưu.
    phan: List[str] = []
    #  Lựa chọn chỉ nằm ở giao diện: {"submode": "whisper"|"ocr"|"hybrid", "style": "...",
    #  "tach_cau_goc": bool}.
    giao_dien: dict = {}
    #  Giá trị ĐANG HIỆN trên thanh cấu hình (URL / bản nháp). DB chỉ ghi lúc chạy bước, nên
    #  đọc DB là lưu cấu hình của LẦN CHẠY TRƯỚC chứ không phải thứ người dùng đang thấy.
    hien_tai: dict = {}


_KIEU_CO_BAN = {
    "separate_audio": "bool", "bg_volume": "so", "voice_bg_volume": "so",
    "vocals_volume": "so", "voice_vocals_volume": "so",
}


def _chuan_gia_tri_co_ban(k: str, v):
    """Giá trị từ giao diện (chuỗi) -> đúng kiểu cột; hỏng thì None (bỏ qua)."""
    kieu = _KIEU_CO_BAN.get(k)
    try:
        if kieu == "bool":
            return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1")
        if kieu == "so":
            x = float(v)
            return x if 0.0 <= x <= 2.0 else None
        return str(v) if isinstance(v, (str, int, float)) and str(v).strip() else None
    except (TypeError, ValueError):
        return None


@router.post("/{video_id}/mac-dinh")
def luu_mac_dinh_co_ban(video_id: str, body: MacDinhCoBanBody, db: Session = Depends(get_db)):
    """"Đặt cấu hình dự án này làm mặc định cho dự án mới" — chế độ Cơ bản."""
    from services import mac_dinh_du_an as MD

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))
    cai = {}
    if body.cai_dat:
        cai = {k: getattr(video, k) for k in MD.KHOA_CO_BAN if getattr(video, k, None) is not None}
        for k, v in (body.hien_tai or {}).items():
            if k in MD.KHOA_CO_BAN:
                x = _chuan_gia_tri_co_ban(k, v)
                if x is not None:
                    cai[k] = x
        cai.update({k: v for k, v in (body.giao_dien or {}).items()
                    if k in MD.KHOA_CO_BAN_GIAO_DIEN and isinstance(v, (str, bool))})
    comp = None
    if body.phan:
        comp = load_compositor_config(
            _compositor_config_path(video_id, db), subtitle_defaults=DUBBING_SUBTITLE_DEFAULTS
        )
    MD.luu("co_ban", cai, comp, body.phan, video.title or "")
    return {"ok": True, "trang_thai": MD.trang_thai()}


@router.get("/settings/mac-dinh")
def xem_mac_dinh_du_an():
    """Chế độ nào đang có "mặc định cho dự án mới" + lựa chọn giao diện của Cơ bản."""
    from services import mac_dinh_du_an as MD

    return {
        "trang_thai": MD.trang_thai(),
        "co_ban_giao_dien": MD.cai_dat("co_ban", MD.KHOA_CO_BAN_GIAO_DIEN),
    }


@router.delete("/settings/mac-dinh/{che_do}")
def xoa_mac_dinh_du_an(che_do: str):
    """"Về mặc định gốc": dự án mới của chế độ này lại dùng giá trị gốc của app."""
    from services import mac_dinh_du_an as MD

    if che_do not in MD.CHE_DO:
        raise HTTPException(status_code=400, detail=L("Chế độ không hợp lệ", "Invalid mode"))
    MD.xoa(che_do)
    return {"ok": True, "trang_thai": MD.trang_thai()}


def _compositor_config_path(video_id: str, db: Session) -> str:
    dirs = get_project_dirs(video_id, db)
    return os.path.join(dirs["root"], "compositor_config.json")


@router.get("/{video_id}/compositor")
def get_compositor_config_route(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))
    # Lồng tiếng: mặc định sub hiện FULL câu (review giữ mặc định cụm 8 chữ).
    #  Đích Nhật / Trung / Hàn: font mặc định là font CJK đóng gói (xem font_mac_dinh_theo_tieng).
    from utils.font_resolver import font_mac_dinh_theo_tieng

    return load_compositor_config(
        _compositor_config_path(video_id, db),
        subtitle_defaults={**DUBBING_SUBTITLE_DEFAULTS, **font_mac_dinh_theo_tieng(getattr(video, "target_lang", "") or "")},
    )


@router.put("/{video_id}/compositor")
def save_compositor_config_route(
    video_id: str, data: dict = Body(...), db: Session = Depends(get_db)
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))
    save_compositor_config(_compositor_config_path(video_id, db), data)
    return {"success": True}


@router.post("/{video_id}/compositor/logo")
async def upload_compositor_logo(
    video_id: str,
    file: UploadFile = File(...),
    auto_remove_bg: bool = Query(False),
    db: Session = Depends(get_db),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail=L("Tệp logo rỗng", "The logo file is empty"))

    dirs = get_project_dirs(video_id, db)
    logo_dir = os.path.join(dirs["root"], "assets")
    os.makedirs(logo_dir, exist_ok=True)
    logo_path = os.path.join(logo_dir, "logo.png")

    try:
        saved_path = process_and_save_logo_image(content, logo_path, remove_bg=auto_remove_bg)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Tệp tải lên không phải ảnh hợp lệ (hỗ trợ PNG, JPG, WEBP): {e}",
        )

    return {"success": True, "path": saved_path}

@router.post("/{video_id}/compositor/frame-bg")
async def upload_compositor_frame_bg(
    video_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Tai ANH NEN cua khung (cho trong quanh video khi khung rong hon video)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail=L("Tệp ảnh nền rỗng", "The background image file is empty"))

    dirs = get_project_dirs(video_id, db)
    bg_dir = os.path.join(dirs["root"], "assets")
    os.makedirs(bg_dir, exist_ok=True)
    try:
        saved_path = process_and_save_frame_bg_image(content, bg_dir)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Tệp tải lên không phải ảnh hợp lệ (hỗ trợ PNG, JPG, WEBP): {e}",
        )

    return {"success": True, "path": saved_path}

@router.post("/{video_id}/compositor/logo/remove-bg")
async def remove_compositor_logo_bg(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

    dirs = get_project_dirs(video_id, db)
    logo_path = os.path.join(dirs["root"], "assets", "logo.png")
    if not os.path.exists(logo_path):
        raise HTTPException(status_code=404, detail=L("Chưa có file logo để xoá nền", "There is no logo file to remove the background from"))

    try:
        saved_path = remove_logo_background_file(logo_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Xoá nền logo thất bại: {e}")

    return {"success": True, "path": saved_path}


_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


@router.post("/{video_id}/compositor/audio")
async def upload_compositor_audio(
    video_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

    if not file.filename or not file.filename.lower().endswith(_AUDIO_EXTS):
        raise HTTPException(
            status_code=400,
            detail=L("Audio phải là .mp3/.wav/.m4a/.aac/.ogg/.flac", "Audio must be .mp3/.wav/.m4a/.aac/.ogg/.flac"),
        )

    dirs = get_project_dirs(video_id, db)
    audio_dir = os.path.join(dirs["root"], "assets")
    os.makedirs(audio_dir, exist_ok=True)
    ext = os.path.splitext(file.filename)[1].lower()
    audio_path = os.path.join(audio_dir, f"compositor_audio{ext}")

    content = await file.read()
    with open(audio_path, "wb") as f:
        f.write(content)

    return {
        "success": True,
        "path": audio_path.replace("\\", "/"),
        "name": file.filename,
    }


@router.post("/{video_id}/compositor/font")
async def upload_compositor_font(
    video_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

    if not file.filename or not file.filename.lower().endswith((".ttf", ".otf")):
        raise HTTPException(status_code=400, detail=L("Font phải là file .ttf hoặc .otf", "The font must be a .ttf or .otf file"))

    dirs = get_project_dirs(video_id, db)
    fonts_dir = get_fonts_dir(dirs["root"])
    ext = os.path.splitext(file.filename)[1].lower()
    font_path = os.path.join(fonts_dir, f"custom{ext}")

    content = await file.read()
    with open(font_path, "wb") as f:
        f.write(content)

    font_family = os.path.splitext(file.filename)[0]
    return {
        "success": True,
        "path": font_path.replace("\\", "/"),
        "fontFamily": font_family,
    }


@router.post("/{video_id}/export-compositor")
def export_compositor_video(
    video_id: str, data: dict = Body(...), db: Session = Depends(get_db)
):
    handler = setup_project_logging(video_id, db)
    try:
        logger.info(f"=== BẮT ĐẦU XUẤT VIDEO DỰNG HÌNH CHO {video_id} ===")
        result = _run_compositor_export(video_id, data, db)
        logger.info("=== XUẤT VIDEO DỰNG HÌNH HOÀN TẤT ===")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Xuất video dựng hình thất bại: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        remove_project_logging(handler)


def _run_compositor_export(video_id: str, data: dict, db: Session, progress_queue=None,
                           should_cancel=None):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

    source_type = data.get("source_type", "master")
    input_video = (
        video.original_path if source_type == "raw" else video.final_video_path
    )
    if not input_video or not os.path.exists(input_video):
        detail = (
            L("Không tìm thấy video gốc của dự án.", "The project's original video was not found.")
            if source_type == "raw"
            else L("Chưa có video lồng tiếng. Vui lòng chạy 'Ghép hoàn thiện' trước, "
                   "hoặc chọn nguồn 'Video gốc' để xuất giữ nguyên âm thanh gốc.",
                   "There is no dubbed video yet. Please run 'Finalize' first, "
                   "or choose the 'Original video' source to export with the original audio.")
        )
        raise HTTPException(status_code=400, detail=detail)

    # Kiểm dung lượng TRƯỚC khi xuất compositor — tránh hết đĩa giữa chừng.
    from utils.storage_paths import check_render_disk_space
    try:
        dirs = get_project_dirs(video_id, db)
        check_render_disk_space(dirs["output"], (input_video,))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    config = data.get("config") or {}
    burn_subtitles = data.get("burn_subtitles", True)

    segments = None
    if burn_subtitles:
        db_segments = (
            db.query(SubtitleSegment)
            .filter(SubtitleSegment.video_id == video_id)
            .order_by(SubtitleSegment.index)
            .all()
        )
        segments = [
            {
                # Dùng ĐÚNG mốc start/end (không đẩy sớm 0.15s) -> sub xuất ra khớp
                # thời gian như preview, không hiện nhanh trước.
                "start_time": max(0.0, s.start_time),
                "end_time": max(0.0, s.end_time),
                "source_text": s.source_text,
                "target_text": s.target_text,
                #  Khoá câu cho phụ đề thêm thứ tiếng (routes/sub_track_routes.cau_co_ban).
                "uid": f"s{s.id}",
            }
            for s in db_segments
        ]

    #  PHỤ ĐỀ NHIỀU THỨ TIẾNG (services/review_sub_tracks.py) — cùng lựa chọn với chế độ
    #  Chi tiết: ``sub_lang`` (đốt), ``second_sub_lang`` (dòng thứ hai), ``srt_langs``,
    #  ``soft_langs``.
    from services import review_sub_tracks as ST

    from routes.sub_track_routes import thu_tieng_chinh_co_ban

    chon = ST.chon_xuat(data, thu_tieng_chinh_co_ban(
        video, db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).all()))
    phu_de_theo_tieng: dict = {}
    if chon["can"]:
        segs_chinh = [
            {
                "start_time": max(0.0, s.start_time),
                "end_time": max(0.0, s.end_time),
                "source_text": s.source_text,
                #  Câu chính = bản dịch; video chưa dịch thì là câu gốc (y như AssGenerator).
                "target_text": (s.target_text or "").strip() or s.source_text,
                "uid": f"s{s.id}",
            }
            for s in db.query(SubtitleSegment)
            .filter(SubtitleSegment.video_id == video_id)
            .order_by(SubtitleSegment.index)
            .all()
        ]
        try:
            phu_de_theo_tieng = ST.theo_tieng(chon, segs_chinh, ST.doc(dirs["root"]))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if chon["sub_lang"] and burn_subtitles:
            segments = phu_de_theo_tieng[chon["sub_lang"]] or None
            logger.info(f"[xuất] Đốt phụ đề thứ tiếng '{chon['sub_lang']}' ({len(segments or [])} câu).")
    source_segments = None
    if chon["second_lang"]:
        source_segments = phu_de_theo_tieng.get(chon["second_lang"]) or None
        config = {**config, "showSourceSubs": bool(source_segments)}

    #  VÙNG CHE "CHỈ KHI CÓ SUB": mốc của chính các câu của video (câu gốc và câu dịch
    #  chung một dòng ở chế độ này). Cần cả khi tắt đốt phụ đề. Bản sao config — khoá
    #  ``_mask_cues`` chỉ sống trong lượt xuất (xem ``_mask_enable_expr``).
    if any((z or {}).get("onlyWithSubs") for z in (config.get("maskZones") or [])):
        #  LUÔN lấy từ DB (mọi câu gốc): ``segments`` có thể đã bị đổi sang một thứ tiếng
        #  khác (``sub_lang``) — tập đó bỏ câu chưa dịch, che theo nó là lộ sub cứng gốc
        #  đúng ở những câu ấy, trong khi khung xem trước vẫn che.
        segments_moc = [
            (max(0.0, s.start_time), max(0.0, s.end_time))
            for s in db.query(SubtitleSegment)
            .filter(SubtitleSegment.video_id == video_id)
            .all()
        ]
        config = {**config, "_mask_cues": segments_moc}

    dirs = get_project_dirs(video_id, db)
    custom_path = data.get("output_path") or data.get("save_path")
    if custom_path:
        output_path = os.path.normpath(custom_path)
        export_filename = os.path.basename(output_path)
    else:
        aspect = config.get("aspectRatio", "source")
        export_filename = data.get("filename") or f"compositor_{aspect}.mp4"
        output_path = os.path.normpath(os.path.join(dirs["output"], export_filename))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def on_progress(percent: int, message: str) -> None:
        if progress_queue is not None:
            progress_queue.put(
                {"type": "progress", "percent": percent, "message": message}
            )

    if progress_queue is not None:
        on_progress(1, L("Đang lưu cấu hình và chuẩn bị xuất...", "Saving the configuration and preparing to export..."))

    if CompositorService.render(
        input_video,
        output_path,
        config,
        segments,
        source_segments=source_segments,
        on_progress=on_progress if progress_queue is not None else None,
        #  Hủy phải tới được ffmpeg. Trước đây không truyền gì: bấm Hủy chỉ ngắt luồng
        #  tiến độ, ffmpeg chạy tiếp tới hết — người dùng bấm Xuất lại là hai, ba, bốn
        #  ffmpeg cùng chạy, lượt nào cũng chậm gấp bấy nhiêu.
        should_cancel=should_cancel,
    ):
        loi_soft = ""
        if chon["soft_langs"]:
            loi_soft = ST.nhung_phu_de_mem(output_path, chon["soft_langs"], phu_de_theo_tieng,
                                           chon["main"])
        record_compositor_export(_compositor_config_path(video_id, db), output_path)
        file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        tep_srt = []
        for lg in chon["srt_langs"]:
            duong = os.path.splitext(output_path)[0] + f".{lg}.srt"
            try:
                tep_srt.append({"lang": lg, "path": duong.replace("\\", "/"),
                                "lines": ST.ghi_srt(duong, phu_de_theo_tieng.get(lg) or [])})
            except OSError as e:
                logger.error(f"[xuất] Không ghi được {duong}: {e}")
        payload = {
            "success": True,
            "filename": export_filename,
            "file_path": output_path.replace("\\", "/"),
            "file_size": file_size,
            "srt_files": tep_srt,
            "soft_langs": [] if loi_soft else chon["soft_langs"],
            "soft_error": loi_soft,
        }
        if progress_queue is not None:
            progress_queue.put({"type": "done", **payload})
            return payload
        return payload

    if should_cancel and should_cancel():
        thong_bao = L("Đã hủy xuất video theo yêu cầu.", "Video export cancelled at your request.")
        if progress_queue is not None:
            progress_queue.put({"type": "error", "message": thong_bao, "cancelled": True})
            return None
        raise HTTPException(status_code=409, detail=thong_bao)

    if progress_queue is not None:
        progress_queue.put(
            {"type": "error", "message": L("Xuất video dựng hình thất bại", "Video export (render) failed")}
        )
        return None
    raise HTTPException(status_code=500, detail=L("Xuất video dựng hình thất bại", "Video export (render) failed"))


@router.post("/{video_id}/export-compositor/stream")
def export_compositor_video_stream(
    video_id: str, data: dict = Body(...), db: Session = Depends(get_db)
):
    handler = setup_project_logging(video_id, db)

    #  MỘT lượt xuất cho mỗi video. Đang có lượt cũ (người dùng bấm Hủy rồi bấm Xuất
    #  lại, hay F5 giữa chừng) thì hạ cờ lượt cũ trước: ý người dùng là "làm lại", không
    #  phải "chạy thêm một cái nữa".
    cancel_event = threading.Event()
    with _XUAT_KHOA:
        cu_ = _XUAT_DANG_CHAY.pop(video_id, None)
        if cu_ is not None and not cu_.is_set():
            cu_.set()
            logger.warning(f"[XUẤT] Video {video_id} đang có lượt xuất cũ — đã hủy lượt cũ để chạy lượt mới.")
        _XUAT_DANG_CHAY[video_id] = cancel_event

    def worker(progress_queue, co_huy):
        try:
            logger.info(f"=== BẮT ĐẦU XUẤT VIDEO DỰNG HÌNH (TRỰC TIẾP) CHO {video_id} ===")
            _run_compositor_export(
                video_id, data, db, progress_queue=progress_queue,
                should_cancel=lambda: co_huy.is_set(),
            )
            if co_huy.is_set():
                logger.info(f"=== XUẤT VIDEO DỰNG HÌNH ĐÃ HỦY (video {video_id}) ===")
        except HTTPException as exc:
            progress_queue.put({"type": "error", "message": str(exc.detail)})
        except Exception as exc:
            logger.exception(f"Xuất video dựng hình (trực tiếp) thất bại: {exc}")
            progress_queue.put({"type": "error", "message": str(exc)})
        finally:
            remove_project_logging(handler)
            with _XUAT_KHOA:
                if _XUAT_DANG_CHAY.get(video_id) is co_huy:
                    _XUAT_DANG_CHAY.pop(video_id, None)

    return compositor_export_sse_response(worker, cancel_event=cancel_event)


@router.post("/{video_id}/cancel")
async def cancel_processing(video_id: str):
    """Yêu cầu HỦY bước pipeline đang chạy (giống cancel-render bên review).

    Bước đang chạy sẽ dừng ở checkpoint gần nhất (per-segment TTS, giữa lô dịch,
    trước demucs/ffmpeg). Các bước gọi 1 phát dài (STT) thì hủy có hiệu lực nếu
    chưa bắt đầu; nếu đã chạy sẽ dừng ngay sau khi xong.
    """
    _request_cancel(video_id)
    #  Đang xuất video dựng hình thì hạ luôn cờ của lượt xuất đó (ffmpeg dừng trong ~1s).
    with _XUAT_KHOA:
        ev = _XUAT_DANG_CHAY.get(video_id)
    if ev is not None:
        ev.set()
    # KHÔNG set translation_service.stop_event (cờ TOÀN CỤC, chia sẻ giữa các video →
    # hủy video này sẽ làm hỏng dịch video khác). Việc dừng dịch giữa chừng dùng
    # should_stop THEO VIDEO truyền vào batch_translate (kiểm _is_cancel_requested).
    logger.info(f"[CANCEL] Đã yêu cầu hủy tiến trình cho video {video_id}")
    return {"status": "cancel_requested"}


@router.get("/{video_id}/logs")
async def stream_video_logs(video_id: str, db: Session = Depends(get_db)):
    dirs = get_project_dirs(video_id, db)
    log_file = os.path.join(dirs["root"], "synthesis.log")
    
    async def log_generator():
        # PERSISTENT: giữ kết nối luôn mở, KHÔNG tự đóng khi bước xong. Nhờ vậy log
        # "live" xuyên suốt nhiều lần chạy mà không phải mở lại kết nối. Chi phí trên
        # app desktop (1 kết nối localhost, đọc file mỗi 0.5s) là không đáng kể.
        # Kết nối chỉ kết thúc khi client (FE) ngắt (đóng tab/rời trang).

        # Chờ file log xuất hiện (video có thể chưa chạy bước nào) — chờ mãi, không
        # đóng; nhắc + giữ nhịp định kỳ.
        waited = 0
        while not os.path.exists(log_file):
            if waited % 20 == 0:
                yield L("data: [SYSTEM] Đang chờ tiến trình khởi động...\n\n", "data: [SYSTEM] Waiting for the process to start...\n\n")
            await asyncio.sleep(0.5)
            waited += 1

        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            # Khi MỚI mở: chỉ gửi ~300 dòng cuối (log tích luỹ qua nhiều lần chạy có
            # thể rất dài → tránh dump cả nghìn dòng một lúc).
            existing_lines = f.read().splitlines()
            for line in existing_lines[-300:]:
                yield f"data: {line}\n\n"

            # Tail vô hạn — chỉ dừng khi client ngắt kết nối.
            idle = 0
            while True:
                line = f.readline()
                if not line:
                    # File bị cắt bớt (trim per-project) → vị trí đọc vượt kích thước
                    # hiện tại → nhảy về cuối để tiếp tục tail đúng (không dump lại).
                    try:
                        if f.tell() > os.path.getsize(log_file):
                            f.seek(0, os.SEEK_END)
                    except OSError:
                        pass
                    idle += 1
                    # Keepalive comment ~mỗi 15s để giữ kết nối khỏe (localhost gần
                    # như không cần nhưng vô hại + không hiển thị ở FE).
                    if idle % 30 == 0:
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)
                    continue
                idle = 0
                yield f"data: {line.strip()}\n\n"

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/")
def list_videos(page: int = 1, limit: int = 20, db: Session = Depends(get_db)):
    root = get_data_root(db)
    
    # 1. Tự động scan root mỗi khi list để cập nhật db nếu có thư mục mới được import/move vào
    if root and os.path.exists(root):
        try:
            scan_data_root(db, root)
        except Exception as e:
            logger.error(f"Lỗi khi quét thư mục dữ liệu {root}: {e}")
            
    # 2. Lọc danh sách video: Chỉ hiển thị những video có thư mục con tương ứng tồn tại thực tế trong root
    existing_folders = []
    if root and os.path.exists(root):
        try:
            existing_folders = [item for item in os.listdir(root) if os.path.isdir(os.path.join(root, item))]
        except Exception as e:
            logger.error(f"Lỗi khi đọc thư mục gốc {root}: {e}")
            
    query = db.query(Video)
    if existing_folders:
        query = query.filter(Video.id.in_(existing_folders))
    else:
        # Nếu thư mục root rỗng hoặc không có thư mục con nào, danh sách video sẽ rỗng
        query = query.filter(Video.id == "non_existent_id_value_to_return_empty")
        
    total = query.count()
    videos = (
        query.order_by(Video.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    #  Dự án cũ chưa lưu độ dài (duration <= 0): Trang chủ hiện độ dài video nên đo bù — như ``get_video`` làm khi MỞ dự án, nhưng ở đây là cả danh sách,
    #  nên mỗi lượt chỉ đo thêm vài tệp chưa từng đo (``services.do_dai_video``); phần còn lại được bù dần qua các lượt mở sau.
    from services import do_dai_video as DDV

    ngan_sach = [6]
    da_sua = False
    for v in videos:
        if (v.duration or 0) <= 0 and v.original_path:
            g = DDV.giay_cua(v.original_path, ngan_sach)
            if g:
                v.duration = g
                da_sua = True
    if da_sua:
        try:
            db.commit()
        except Exception as e:  # noqa: BLE001 — số liệu chỉ để xem, ghi hỏng thì bỏ
            logger.warning(f"[videos] không ghi được độ dài bù: {e}")
            db.rollback()
    return {
        "videos": videos,
        "total": total,
        "page": page,
        "total_pages": (total + limit - 1) // limit if limit > 0 else 1,
    }


@router.get("/{video_id}/thumb")
async def get_video_thumb(video_id: str, db: Session = Depends(get_db)):
    """Ảnh đại diện (JPEG ~320px) của dự án Cơ bản — cho kiểu hiển thị có ảnh ở Trang chủ.

    Dựng MỘT lần (trích một khung ở ~1/4 phim, như ảnh dự án Chi tiết) rồi dùng lại.
    Thời lượng đo lúc dựng: ``_make_thumb`` coi thời lượng <= 0 là TỆP ẢNH, bỏ ``-ss``.
    """
    from services import review_media_library as LIB

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án", "Project not found"))
    src = (video.original_path or "").strip()
    if not src or not os.path.exists(src):
        raise HTTPException(status_code=404, detail=L("Dự án chưa có video gốc", "The project has no source video yet"))
    thu_muc = os.path.join(get_project_dirs(video_id, db)["root"], "thumbs")
    os.makedirs(thu_muc, exist_ok=True)
    dst = os.path.join(thu_muc, "project.jpg")
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        dai = float(await run_in_threadpool(VideoService.get_video_duration, src) or 0) or 60.0
        if not await run_in_threadpool(LIB._make_thumb, src, dst, dai):
            raise HTTPException(status_code=404, detail=L("Không dựng được ảnh đại diện", "Could not create the thumbnail"))
    return FileResponse(dst, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{video_id}")
def get_video(
    video_id: str,
    page: int = 1,
    limit: int = 20,
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

    # Auto-fill duration for legacy projects
    if video.duration <= 0 and video.original_path:
        try:
            video.duration = VideoService.get_video_duration(video.original_path)
            db.commit()
        except:
            pass

    query = db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id)

    if search:
        if search.isdigit():
            # Search by index (1-based from user perspective potentially, but let's match exact if possible)
            idx = int(search)
            query = query.filter(
                (SubtitleSegment.index == idx) | (SubtitleSegment.index == idx - 1)
            )
        elif search.lower() == "translated":
            query = query.filter(
                SubtitleSegment.target_text != None, SubtitleSegment.target_text != ""
            )
        elif search.lower() == "voiced":
            query = query.filter(
                SubtitleSegment.audio_path != None, SubtitleSegment.audio_path != ""
            )
        elif search.lower() == "trimmed":
            query = query.filter(
                SubtitleSegment.is_trimmed == True
            )
        else:
            query = query.filter(
                (SubtitleSegment.source_text.ilike(f"%{search}%"))
                | (SubtitleSegment.target_text.ilike(f"%{search}%"))
            )

    total_segments = query.count()
    segments = (
        query.order_by(SubtitleSegment.index)
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    # Enrich with workspace paths
    dirs = get_project_dirs(video_id, db)

    # Check if segment voice files exist on disk and enrich audio_path dynamically
    for seg in segments:
        if not seg.audio_path:
            possible_path = os.path.join(dirs["segments"], f"seg_{seg.id}.mp3").replace("\\", "/")
            if os.path.exists(possible_path) and os.path.getsize(possible_path) > 0:
                seg.audio_path = possible_path

    # Global stats (ignoring search/pagination)
    global_total = (
        db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).count()
    )
    global_translated = (
        db.query(SubtitleSegment)
        .filter(
            SubtitleSegment.video_id == video_id,
            SubtitleSegment.target_text != None,
            SubtitleSegment.target_text != "",
        )
        .count()
    )
    global_voiced = (
        db.query(SubtitleSegment)
        .filter(
            SubtitleSegment.video_id == video_id,
            SubtitleSegment.audio_path != None,
            SubtitleSegment.audio_path != "",
        )
        .count()
    )

    # Count actually existing voice files in segments directory for stats
    seg_dir = dirs.get("segments")
    actual_voiced_count = 0
    if seg_dir and os.path.isdir(seg_dir):
        try:
            for fname in os.listdir(seg_dir):
                # Bỏ qua file tạm của ghi atomic (seg_xxx.mp3.new.mp3): nó xuất hiện
                # rồi bị rename/xóa ngay -> vừa đếm nhầm, vừa dễ gây WinError 2 khi
                # getsize (file đã biến mất giữa listdir và getsize).
                if ".new." in fname or fname.endswith(".new.mp3") or fname.endswith(".new.wav"):
                    continue
                if fname.startswith("seg_") and fname.endswith((".mp3", ".wav")):
                    fpath = os.path.join(seg_dir, fname)
                    try:
                        if os.path.getsize(fpath) > 0:
                            actual_voiced_count += 1
                    except OSError:
                        # File bị rename/xóa giữa chừng bởi TTS đang chạy song song:
                        # bỏ qua file này thay vì hủy cả phép đếm.
                        continue
        except Exception as e:
            logger.error(f"Lỗi khi quét thư mục các đoạn để thống kê: {e}")
            actual_voiced_count = global_voiced

    # Trimmed segments: all trimmed segments across all pages (for global warning banner)
    trimmed_segments_all = (
        db.query(SubtitleSegment.id, SubtitleSegment.index)
        .filter(
            SubtitleSegment.video_id == video_id,
            SubtitleSegment.is_trimmed == True,
        )
        .order_by(SubtitleSegment.index)
        .all()
    )
    trimmed_segment_ids = [{'id': s.id, 'index': s.index} for s in trimmed_segments_all]

    # Fallback segments: đoạn dịch bằng DeepSeek lỗi -> đã tự chuyển sang Google.
    # Trả về toàn bộ (mọi trang) để hiện banner cảnh báo cho người dùng kiểm tra.
    fallback_segments_all = (
        db.query(SubtitleSegment.id, SubtitleSegment.index)
        .filter(
            SubtitleSegment.video_id == video_id,
            SubtitleSegment.translation_fallback == True,
        )
        .order_by(SubtitleSegment.index)
        .all()
    )
    fallback_segment_ids = [{'id': s.id, 'index': s.index} for s in fallback_segments_all]

    # OCR-filled segments: đoạn OCR vá vào chỗ Whisper không nghe được (chế độ B).
    # Trả về để FE hiện banner cảnh báo + cho người dùng xác nhận / sinh voice thủ công.
    ocr_filled_all = (
        db.query(SubtitleSegment.id, SubtitleSegment.index)
        .filter(
            SubtitleSegment.video_id == video_id,
            SubtitleSegment.ocr_filled == True,
        )
        .order_by(SubtitleSegment.index)
        .all()
    )
    ocr_filled_segment_ids = [{'id': s.id, 'index': s.index} for s in ocr_filled_all]

    response = {
        "video": video,
        "segments": segments,
        "total_segments": total_segments,  # Paginated/Filtered count
        "stats": {
            "total": global_total,
            "translated": global_translated,
            "voiced": max(global_voiced, actual_voiced_count),
        },
        "page": page,
        "total_pages": (total_segments + limit - 1) // limit if limit > 0 else 1,
        "project_root": dirs["root"].replace("\\", "/"),
        "project_state_path": os.path.join(dirs["root"], "project_state.json").replace(
            "\\", "/"
        ),
        "trimmed_segment_ids": trimmed_segment_ids,
        #  Số câu đang có điểm kết thúc do app tự NỚI vào khoảng lặng (mọi trang) — để hiện
        #  nút "Khôi phục khung gốc".
        "extended_count": db.query(SubtitleSegment)
        .filter(SubtitleSegment.video_id == video_id, SubtitleSegment.goc_end_time.isnot(None))
        .count(),
        "fallback_segment_ids": fallback_segment_ids,
        "ocr_filled_segment_ids": ocr_filled_segment_ids,
    }

    if video.final_video_path and os.path.exists(video.final_video_path):
        response["final_video_size"] = os.path.getsize(video.final_video_path)
        response["final_video_absolute_path"] = os.path.abspath(video.final_video_path).replace("\\", "/")

    return response


@router.post("/{video_id}/restore-segment-timing")
async def restore_segment_timing(video_id: str, db: Session = Depends(get_db)):
    """"Khôi phục khung gốc" (Cơ bản): trả điểm kết thúc của MỌI câu đã được bước lồng
    tiếng tự nới (``goc_end_time``) về đúng mốc gốc, và cho các câu đó GIỮ khung gốc.

    Giọng của các câu này dài hơn khung gốc (vì thế mới nới) -> XOÁ giọng để lần Lồng tiếng
    tới sinh lại cho vừa khung gốc. Giữ giọng cũ thì khi ghép đuôi giọng phát đè lên nhạc
    nền không được hạ, còn lần Lồng tiếng sau lại nới y như cũ (rà soát 29-09-2026)."""
    from routes.storage_routes import is_migrating
    if is_migrating():
        raise HTTPException(status_code=409, detail=L("Đang di chuyển dữ liệu sang vị trí lưu mới — chờ xong rồi hãy làm.", "Data is being moved to the new storage location — wait for it to finish before doing this."))
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))
    #  Cùng khoá với các bước nặng: đang sinh giọng (kể cả một câu) thì bước đó ghi end
    #  sau lượt này -> nới lại ngay.
    _acquire_or_409(video_id, "restore-timing")
    try:
        dirs = get_project_dirs(video_id, db)
        rows = (
            db.query(SubtitleSegment)
            .filter(SubtitleSegment.video_id == video_id, SubtitleSegment.goc_end_time.isnot(None))
            .all()
        )
        for r in rows:
            r.end_time = r.goc_end_time
            r.goc_end_time = None
            r.giu_khung_goc = True
            sp = os.path.join(dirs["segments"], f"seg_{r.id}.mp3")
            try:
                if os.path.exists(sp):
                    os.remove(sp)
            except OSError:
                pass
            r.audio_path = None
        if rows:
            #  Thiếu giọng -> bước Lồng tiếng chạy lại (chỉ câu thiếu); bản ghép cũ hết đúng.
            video.p4_status = "pending"
            video.p5_status = "pending"
        db.commit()
        if rows:
            sync_project_to_disk(video_id, db)
            logger.info(
                f"Khôi phục khung gốc: {len(rows)} câu về lại điểm kết thúc ban đầu — "
                "giọng các câu này sẽ sinh lại cho vừa khung gốc ở lần Lồng tiếng tới."
            )
        return {"restored": len(rows)}
    finally:
        _release_step(video_id)


@router.post("/{video_id}/update-segments")
async def update_segments(
    video_id: str, segments_data: list = Body(...), db: Session = Depends(get_db)
):
    dirs = get_project_dirs(video_id, db)
    tts_needs_reset = False
    merge_needs_reset = False

    for seg_data in segments_data:
        seg = (
            db.query(SubtitleSegment)
            .filter(SubtitleSegment.id == seg_data["id"])
            .first()
        )
        if seg:
            text_changed = seg.target_text != seg_data.get("target_text")
            speed_changed = seg.speed_mode != seg_data.get("speed_mode")
            vol_changed = seg.bg_volume != seg_data.get("bg_volume")

            if text_changed or speed_changed:
                seg_path = os.path.join(dirs["segments"], f"seg_{seg.id}.mp3")
                if os.path.exists(seg_path):
                    try:
                        os.remove(seg_path)
                    except:
                        pass
                seg.audio_path = None  # Clear DB path
                bo_noi_khung(seg)

                tts_needs_reset = True
                merge_needs_reset = True

            if vol_changed:
                merge_needs_reset = True

            seg.target_text = seg_data.get("target_text")
            seg.speed_mode = seg_data.get("speed_mode")
            seg.bg_volume = seg_data.get("bg_volume")

    db.commit()

    # Recalculate stats and update video status
    video = db.query(Video).filter(Video.id == video_id).first()
    if video:
        if tts_needs_reset:
            video.p4_status = "pending"
        if merge_needs_reset:
            video.p5_status = "pending"

        # Check if all segments now have target text
        total_segs = db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).count()
        translated_segs = db.query(SubtitleSegment).filter(
            SubtitleSegment.video_id == video_id,
            SubtitleSegment.target_text != None,
            SubtitleSegment.target_text != ""
        ).count()

        if total_segs > 0 and translated_segs == total_segs:
            video.p3_status = "completed"
            # If we were at an earlier step, progress the main status
            if video.status in ["uploaded", "audio_extracted", "stt_completed"]:
                video.status = "translated"
        elif translated_segs > 0:
            # Partial translation
            if video.p3_status != "processing":
                video.p3_status = "pending"
        
        db.commit()

    sync_project_to_disk(video_id, db)
    return {"status": "success"}


@router.patch("/{video_id}/settings")
async def update_project_settings(
    video_id: str, data: dict, db: Session = Depends(get_db)
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))

    if "source_lang" in data:
        video.source_lang = data["source_lang"]
    if "target_lang" in data:
        video.target_lang = data["target_lang"]

    if "voice_talent" in data and video.voice_talent != data["voice_talent"]:
        video.voice_talent = data["voice_talent"]
        video.p4_status = "pending"
        video.p5_status = "pending"
        dirs = get_project_dirs(video_id, db)
        seg_dir = dirs.get("segments")
        if seg_dir and os.path.isdir(seg_dir):
            for fname in os.listdir(seg_dir):
                if fname.startswith("seg_") and fname.endswith(".mp3"):
                    try:
                        os.remove(os.path.join(seg_dir, fname))
                    except OSError:
                        pass
        for seg in db.query(SubtitleSegment).filter(
            SubtitleSegment.video_id == video_id
        ).all():
            seg.audio_path = None
            bo_noi_khung(seg)

    if "speed_mode" in data and video.speed_mode != data["speed_mode"]:
        video.speed_mode = data["speed_mode"]
        video.p4_status = "pending"
        video.p5_status = "pending"
        dirs = get_project_dirs(video_id, db)
        seg_dir = dirs.get("segments")
        if seg_dir and os.path.isdir(seg_dir):
            for fname in os.listdir(seg_dir):
                if fname.startswith("seg_") and fname.endswith(".mp3"):
                    try:
                        os.remove(os.path.join(seg_dir, fname))
                    except OSError:
                        pass
        for seg in db.query(SubtitleSegment).filter(
            SubtitleSegment.video_id == video_id
        ).all():
            seg.audio_path = None
            bo_noi_khung(seg)

    #  CHO GIONG TRAN SANG CAU SAU — chỉ áp cho LẦN SINH GIỌNG TIẾP THEO (không xoá giọng đã sinh như đổi tốc độ:
    #  đây là công tắc, bật/tắt thử không được phép xoá cả loạt mp3).
    if "cho_tran_giong" in data and data["cho_tran_giong"] is not None:
        video.cho_tran_giong = bool(data["cho_tran_giong"])
    if "tran_giong" in data and data["tran_giong"] is not None:
        from services import review_timeline as TL

        video.tran_giong = round(TL.tran_giong_giay(data["tran_giong"]), 2)
    #  ÂM LƯỢNG GIỌNG AI (0–200%) — áp lúc ghép video, không cần sinh lại giọng.
    if "tts_volume" in data and data["tts_volume"] is not None:
        try:
            video.tts_volume = round(max(0.0, min(2.0, float(data["tts_volume"]))), 3)
            video.p5_status = "pending"
        except (TypeError, ValueError):
            pass

    if "bg_volume" in data and video.bg_volume != float(data["bg_volume"]):
        video.bg_volume = float(data["bg_volume"])
        video.p5_status = "pending"

    if "voice_bg_volume" in data and video.voice_bg_volume != float(
        data["voice_bg_volume"]
    ):
        video.voice_bg_volume = float(data["voice_bg_volume"])
        video.p5_status = "pending"

    if "separate_audio" in data and bool(video.separate_audio) != bool(
        data["separate_audio"]
    ):
        video.separate_audio = bool(data["separate_audio"])
        video.p5_status = "pending"

    if "vocals_volume" in data and video.vocals_volume != float(data["vocals_volume"]):
        video.vocals_volume = float(data["vocals_volume"])
        video.p5_status = "pending"

    if "voice_vocals_volume" in data and video.voice_vocals_volume != float(
        data["voice_vocals_volume"]
    ):
        video.voice_vocals_volume = float(data["voice_vocals_volume"])
        video.p5_status = "pending"

    db.commit()
    sync_project_to_disk(video_id, db)
    return {
        "status": "success",
        "settings": {
            "source_lang": video.source_lang,
            "target_lang": video.target_lang,
            "voice_talent": video.voice_talent,
            "speed_mode": video.speed_mode,
            "cho_tran_giong": video.cho_tran_giong if video.cho_tran_giong is not None else True,
            "tran_giong": video.tran_giong if video.tran_giong is not None else 0.85,
            "tts_volume": _tts_volume_cua_video(video),
            "bg_volume": video.bg_volume,
            "voice_bg_volume": video.voice_bg_volume,
            "separate_audio": bool(video.separate_audio),
            "vocals_volume": video.vocals_volume,
            "voice_vocals_volume": video.voice_vocals_volume,
        },
    }


@router.delete("/purge-all")
async def purge_all(db: Session = Depends(get_db)):
    root = get_data_root(db)

    # 0. Chặn hẳn khi còn video đang xử lý: xoá thư mục lúc FFmpeg/STT còn ghi
    # vào đó sẽ để lại dự án cụt. Thà không xoá gì và nói rõ lý do.
    phase_cols = [
        Video.p1_status,
        Video.p2_status,
        Video.p3_status,
        Video.p4_status,
        Video.p5_status,
    ]
    busy = (
        db.query(Video)
        .filter(or_(*[c == "processing" for c in phase_cols]))
        .all()
    )
    if busy:
        names = [v.title or v.id for v in busy]
        raise HTTPException(
            status_code=409,
            detail=(
                L(f"Đang có {len(names)} dự án Cơ bản chạy dở: {', '.join(names[:3])}",
                  f"{len(names)} Basic projects are still running: {', '.join(names[:3])}")
                + ("…" if len(names) > 3 else "")
                + L(". Hãy đợi chạy xong hoặc dừng tiến trình, rồi xoá lại.",
                    ". Wait for them to finish or stop the process, then delete again.")
            ),
        )

    # 1. Clear database records first
    db.query(SubtitleSegment).delete()
    db.query(Video).delete()
    db.commit()

    # 2. Manually destroy identifying files and try to wipe folders
    failed = []
    if os.path.exists(root):
        for item in os.listdir(root):
            item_path = os.path.join(root, item)
            # Identifying file
            state_file = os.path.join(item_path, "project_state.json")
            if os.path.exists(state_file):
                try:
                    os.remove(state_file)
                except:
                    pass

            # Folder/File removal (skip the DB file itself temporarily)
            if item == "subforge.db":
                continue

            # Không nuốt lỗi: trước đây xoá hụt vẫn báo "success", người dùng
            # tưởng đã sạch mà dung lượng không hề giảm.
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)
            except Exception:
                failed.append(item)

    # 3. Attempt to delete the subforge.db file itself
    db_file = os.path.join(root, "subforge.db")
    engine.dispose()  # Try to unlock
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass  # Likely locked by current process

    if failed:
        raise HTTPException(
            status_code=500,
            detail=(
                L(f"Còn {len(failed)} mục không xoá được (tệp đang bị khoá): ",
                  f"{len(failed)} items could not be deleted (files are locked): ")
                + ", ".join(failed[:3])
                + ("…" if len(failed) > 3 else "")
                + L(". Hãy đóng trình phát đang mở file rồi thử lại.",
                    ". Close any player that has the file open and try again.")
            ),
        )
    return {"status": "success"}


@router.delete("/{video_id}")
async def delete_video(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if video:
        dirs = get_project_dirs(video_id, db)

        # 1. Delete identifying file first to prevent re-import if folder removal fails
        state_file = os.path.join(dirs["root"], "project_state.json")
        if os.path.exists(state_file):
            try:
                os.remove(state_file)
            except:
                pass

        # 2. Try to remove folder
        if os.path.exists(dirs["root"]):
            try:
                shutil.rmtree(dirs["root"])
            except:
                pass

        # 3. Wipe DB records
        db.query(SubtitleSegment).filter(SubtitleSegment.video_id == video_id).delete()
        db.delete(video)
        db.commit()

        # Đánh dấu dự án đã xoá để báo trạng thái lên server (không mất số đã thống kê).
        try:
            from services.usage_reporter import mark_deleted
            mark_deleted(str(video_id))
        except Exception as _e:
            logger.warning(f"Không ghi được usage (deleted): {_e}")
    return {"status": "success"}
