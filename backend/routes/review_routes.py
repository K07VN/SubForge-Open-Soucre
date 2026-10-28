from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, File, UploadFile, Body, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import List, Optional
from services.review_service import ReviewService
from services.review_engine import ReviewEngine
from services.config_service import get_config_service
import os
import time
import json
import logging
import asyncio
from services import work_queue as WQ


from services.video_service import VideoService
from services.compositor_service import CompositorService
from services.compositor_utils import (
    load_compositor_config,
    record_compositor_export,
    save_compositor_config,
    get_fonts_dir,
    review_b_chunks_to_segments,
)
import os

from utils.compositor_export_stream import compositor_export_sse_response
from utils.logo_utils import (
    process_and_save_frame_bg_image,
    process_and_save_logo_image,
    remove_logo_background_file,
)
from routes.auth_routes import verify_license

router = APIRouter(prefix="/api/review", tags=["review"], dependencies=[Depends(verify_license)])
config_service = get_config_service()
review_service = ReviewService()
review_engine = ReviewEngine()
video_service = VideoService()

# Guard chống CHẠY CHỒNG theo project: analyze / render / generate-voice loại trừ
# lẫn nhau. Tránh 2 tiến trình nền cùng ghi DB/cache -> "database is locked" hoặc kết
# quả hỏng khi bấm lại/hủy chưa dừng hẳn (giống guard bên pipeline lồng tiếng).
import threading as _threading
from utils.i18n import L

_REVIEW_BUSY: set = set()
_REVIEW_BUSY_LOCK = _threading.Lock()


def _acquire_review(project_id: str) -> bool:
    with _REVIEW_BUSY_LOCK:
        if project_id in _REVIEW_BUSY:
            return False
        _REVIEW_BUSY.add(project_id)
        return True


def _release_review(project_id: str) -> None:
    with _REVIEW_BUSY_LOCK:
        _REVIEW_BUSY.discard(project_id)


def _is_review_busy(project_id: str) -> bool:
    """Có job nền (analyze/render/voice) đang GIỮ khóa cho project này không.

    Sau khi bấm Hủy, khóa chỉ được nhả khi job nền THỰC SỰ dừng (STT kiểm cancel
    giữa các khối nên có thể mất vài giây). FE dựa vào cờ này để đợi dừng hẳn trước
    khi cho chạy lại — tránh 409 'đang chạy tác vụ khác'.
    """
    with _REVIEW_BUSY_LOCK:
        return project_id in _REVIEW_BUSY


def _chan_neu_may_ban() -> None:
    """CHẶN THẲNG khi máy đang chạy một việc nặng khác.

    Chốt với chủ dự án (2026-09-17): bấm việc nặng lúc máy đang bận thì BÁO và
    thôi, chứ KHÔNG xếp hàng. Xếp hàng nghe thì hay nhưng người dùng bấm xong
    không thấy gì nhúc nhích, rồi lại phải dựng thêm cả một giao diện quản lý hàng
    đợi (đang đợi những gì, huỷ cái nào, đổi thứ tự) — phức tạp hơn hẳn cái lợi.

    Hàng đợi ``work_queue`` vẫn giữ nguyên làm CHỐT CHẶN CUỐI cho việc nền (bộ chạy
    lô vẫn đợi tới lượt chứ không bỏ việc); chỗ này chỉ chặn ở cửa mà người dùng bấm.
    """
    x = WQ.viec_dang_chay()
    if x:
        raise HTTPException(
            status_code=409,
            detail=(
                L(f"Máy đang chạy việc nặng: {x['nhan']}. Mỗi lúc chỉ chạy được một "
                  "việc nặng — đợi nó xong (hoặc bấm Huỷ ở chỗ đang chạy) rồi bấm lại.",
                  f"The machine is running a heavy task: {x['nhan']}. Only one heavy task can run at a time "
                  "— wait for it to finish (or click Cancel where it is running) and then try again.")
            ),
        )


def _acquire_review_or_409(project_id: str) -> None:
    _chan_neu_may_ban()
    if not _acquire_review(project_id):
        raise HTTPException(
            status_code=409,
            detail=L("Dự án review này đang chạy một tác vụ khác (phân tích / dựng video / tạo giọng). Vui lòng đợi hoàn tất, hoặc bấm Hủy và chờ dừng hẳn trước khi chạy lại.",
                     "This review project is running another task (analysis / video rendering / voice generation). Please wait for it to finish, or click Cancel and wait for it to fully stop before running again."),
        )

@router.get("/probe-duration")


async def probe_duration(path: str):
    print(f"[REVIEW-API] Probing duration for: {path}")
    path = path.strip().strip('"')
    if not os.path.exists(path):
        print(f"[REVIEW-API] Error: File not found at {path}")
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    try:
        duration_sec = video_service.get_video_duration(path)
        print(f"[REVIEW-API] Probe SUCCESS: {duration_sec} sec")
        return {
            "duration_sec": duration_sec,
            "duration_min": duration_sec / 60.0
        }

    except Exception as e:
        print(f"[REVIEW-API] Probe FAILED: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to probe file: {str(e)}")


class ProjectCreate(BaseModel):
    #  RỖNG = DỰ ÁN TRỐNG, chưa có phim gốc.
    #
    #  Chỉ có nghĩa với chế độ CHI TIẾT: ở đó timeline là một mạch phim tự dựng, người
    #  dùng thêm ảnh/clip/nhạc vào sau (vd ghép ảnh tĩnh rồi lồng lời bình cho từng ảnh)
    #  nên không có "phim gốc" nào để bắt đầu. Chế độ cũ vẫn cần video vì cả luồng của nó
    #  chạy trên một video có sẵn — FE của chế độ cũ không gửi chuỗi rỗng bao giờ.
    video_path: str = ""
    video_duration: float = 0.0
    # Tên dự án (tên người dùng đặt lúc tải). Trống -> backend lấy tên file.
    title: Optional[str] = ""
    # Ngôn ngữ + giọng giờ chọn Ở BƯỚC PHÂN TÍCH nên KHÔNG bắt buộc lúc tạo dự án.
    source_language: Optional[str] = "auto"
    target_language: Optional[str] = "vi"
    target_voice: Optional[str] = "vi-VN-NamMinhNeural"
    #  Dự án CHI TIẾT mới -> áp "mặc định cho dự án mới" (services/mac_dinh_du_an.py).
    #  Mặc định TẮT: bên gọi cũ (chế độ Review cũ) giữ nguyên hành vi.
    ap_mac_dinh: bool = False


class BChunk(BaseModel):
    b_id: int
    b_text: Optional[str] = None
    text: Optional[str] = None
    target_text: Optional[str] = None
    source_a_ids: List[int]

    def get_text(self) -> str:
        return (self.b_text or self.text or self.target_text or "").strip()
    # Tùy chọn (LLM không bắt buộc xuất):
    #  - duration_seconds: overlay suy từ a_chunks nếu thiếu
    #  - voice/target_language/rate: None -> dùng giọng + ngôn ngữ project
    duration_seconds: Optional[float] = None
    rate: Optional[str] = None
    voice: Optional[str] = None
    target_language: Optional[str] = None
    # Người dùng chỉnh time B thủ công ở màn Review (giây trên timeline video gốc).
    # None = dùng time suy từ source_a_ids. Có giá trị = ĐÈ vị trí đặt voice/SRT và
    # buộc sinh lại voice cho khớp (xem resolve_b_time / compute_tts_fingerprint).
    start_override: Optional[float] = None
    end_override: Optional[float] = None
    # Giữ lại cờ trim khi import lại (không mất trạng thái cảnh báo).
    is_trimmed: Optional[bool] = None


@router.post("/projects")
async def create_project(data: ProjectCreate):
    print(f"[REVIEW-API] Creating project: {data.video_path} ({data.source_language} -> {data.target_language})")
    data.video_path = data.video_path.strip().strip('"')
    #  Đường dẫn RỖNG là hợp lệ (dự án trống — xem ProjectCreate.video_path). Chỉ chặn
    #  khi người dùng CÓ đưa đường dẫn mà tệp không tồn tại: đó mới là gõ nhầm/ổ chưa cắm.
    if data.video_path and not os.path.exists(data.video_path):
        print(f"[REVIEW-API] Error: Video file not found")
        raise HTTPException(status_code=400, detail="Video file not found")

    project_id = review_service.create_project(
        data.video_path,
        data.video_duration,
        data.source_language,
        data.target_language,
        data.target_voice,
        title=data.title,
        ap_mac_dinh=data.ap_mac_dinh,
    )
    print(f"[REVIEW-API] Project created: {project_id}")
    return {"project_id": project_id}


#  "ReviewCompositor" / "CompositorService" = BUOC 2/2 (dot phu de / logo). Thieu hai ten
#  nay thi ca dong bat dau lan dong LOI cua buoc 2/2 khong vao synthesis.log: nhat ky dung
#  mai o "Tiep buoc 2/2..." nen nguoi dung tuong may van dang chay, du ffmpeg da chet.
_PROJECT_LOGGERS = (
    "VideoService", "ReviewService", "ReviewEngine", "STTService", "OCRService",
    "TTSService", "SeparationService", "DeepSeekService",
    "ReviewCompositor", "CompositorService",
    #  Logger của CHI TIẾT (review_advanced_routes / review_advanced_ai). Thiếu tên này thì
    #  mọi dòng "Tiến độ tạo kịch bản x/y", "BAT DAU TAO KICH BAN"… chỉ ra console, KHÔNG
    #  vào synthesis.log — tab Nhật ký lẫn nhật ký chạy hàng loạt im lặng suốt lượt dịch
    #  DeepSeek (người dùng báo 29-09-2026).
    "ReviewAdvanced",
    #  Dòng "Gộp mẩu phụ đề ngắn: X → Y câu" (logger theo tên mô-đun).
    "services.gop_manh_ngan",
    #  Dòng "Tách câu gốc / Tách bản dịch: X → Y câu" (services/tach_cau.py).
    "services.tach_cau",
)


def setup_project_logging(project_id, projects_root):
    project_dir = os.path.join(projects_root, project_id)
    os.makedirs(project_dir, exist_ok=True)
    log_file = os.path.join(project_dir, "synthesis.log")
    
    # Configure handlers
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    file_handler.setLevel(logging.INFO)
    
    # Add handler to all relevant loggers
    for logger_name in _PROJECT_LOGGERS:
        l = logging.getLogger(logger_name)
        # Prevent duplicate handlers
        has_file_handler = any(isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(log_file) for h in l.handlers)
        if not has_file_handler:
            l.addHandler(file_handler)

    return file_handler

def remove_project_logging(project_id, file_handler):
    # Remove handler from all relevant loggers
    for logger_name in _PROJECT_LOGGERS:
        l = logging.getLogger(logger_name)
        if file_handler in l.handlers:
            l.removeHandler(file_handler)
    file_handler.close()

def run_analysis_wrapper(project_id):
    try:
        #  XẾP HÀNG: mỗi lúc chỉ một việc nặng chạy trên máy (xem
        #  services/work_queue.py). Chưa tới lượt thì đợi chứ không bị từ chối.
        with WQ.slot(project_id, L("Lấy phụ đề (nhận dạng)", "Get subtitles (recognition)"), cancelled=lambda: review_service.is_analysis_cancelled(project_id)):
            from services.review_service import AnalysisCancelled

            projects_root = config_service.get_review_root()
            handler = setup_project_logging(project_id, projects_root)
            logger = logging.getLogger("ReviewService")
            try:
                logger.info(f"=== BẮT ĐẦU PHÂN TÍCH VIDEO — dự án {project_id} ===")
                if not review_service.run_transcription(project_id):
                    raise RuntimeError(L("Nhận dạng lời thoại thất bại (không có dữ liệu project).", "Speech recognition failed (no project data)."))
                # Người dùng có thể bấm hủy sau STT (trước khi phân đoạn) — kiểm tra lần nữa.
                if review_service.is_analysis_cancelled(project_id):
                    raise AnalysisCancelled()
                if not review_service.generate_a_chunks(project_id):
                    raise RuntimeError(L(
                        "Chuẩn bị các câu phân tích thất bại (không có câu nhận dạng nào — "
                        "video có thể không có lời thoại, hoặc nhận dạng bị lỗi).",
                        "Preparing the analysis sentences failed (no recognized sentences — "
                        "the video may have no speech, or recognition failed)."
                    ))
                logger.info(f"=== PHÂN TÍCH VIDEO HOÀN TẤT ===")
                review_service.clear_analysis_cancel(project_id)
            except AnalysisCancelled:
                logger.warning("=== ĐÃ HỦY PHÂN TÍCH THEO YÊU CẦU ===")
                review_service.mark_analysis_failed(
                    project_id, L("Đã hủy phân tích theo yêu cầu người dùng.", "Analysis cancelled at the user's request.")
                )
            except Exception as e:
                # Bị hủy có thể được bọc trong lỗi khác (vd lỗi STT do dừng giữa chừng).
                if review_service.is_analysis_cancelled(project_id):
                    logger.warning(f"Đã hủy phân tích: {e}")
                    review_service.mark_analysis_failed(
                        project_id, L("Đã hủy phân tích theo yêu cầu người dùng.", "Analysis cancelled at the user's request.")
                    )
                else:
                    logger.error(f"Phân tích video thất bại: {e}", exc_info=True)
                    # rollback analysis_started — tránh UI treo "Đang phân tích…" mãi
                    review_service.mark_analysis_failed(project_id, _loi_doc_duoc(e))
            finally:
                remove_project_logging(project_id, handler)
                _release_review(project_id)

    except WQ.WorkCancelled:
        #  Bấm Huỷ khi việc CÒN ĐANG XẾP HÀNG: thân hàm chưa chạy nên phần
        #  dọn dẹp trong ``finally`` của nó cũng chưa chạy — phải tự dọn ở
        #  đây, không thì khoá dự án không ai nhả và giao diện treo mãi.
        #  KHÔNG dùng ``logger``: trong hàm này nó là biến CỤC BỘ gán ở thân
        #  hàm (phần chưa chạy) — chạm vào là UnboundLocalError, và thế là mất
        #  luôn phần dọn dẹp bên dưới.
        logging.getLogger("ReviewService").warning(
            f"[HÀNG ĐỢI] Đã huỷ khi còn đang xếp hàng: {project_id}"
        )
        review_service.mark_analysis_failed(
            project_id, L("Đã hủy phân tích theo yêu cầu người dùng.", "Analysis cancelled at the user's request.")
        )
        _release_review(project_id)
def _loi_doc_duoc(e: BaseException) -> str:
    """Doi cac loi may moc hay gap sang cau nguoi dung lam duoc gi voi no.

    Ca that: may khach het RAM giua luc phan tich, va tat ca nhung gi hien len la
    "Unable to allocate 195. MiB for an array with shape (...) and data type
    complex128". Dung ve ky thuat, nhung nguoi dung khong rut ra duoc hanh dong nao.
    """
    from services.separation_service import _la_het_bo_nho

    if _la_het_bo_nho(e):
        return (
            L("Máy hết bộ nhớ (RAM) khi phân tích video. Hãy đóng bớt ứng dụng đang mở "
              "(nhất là trình duyệt), hoặc chia video thành các phần ngắn hơn rồi chạy "
              f"lại. Chi tiết kỹ thuật: {str(e)[:200]}",
              "The machine ran out of memory (RAM) while analyzing the video. Close some open applications "
              "(especially the browser), or split the video into shorter parts and run "
              f"again. Technical details: {str(e)[:200]}")
        )
    return str(e)


def run_render_wrapper(project_id, force_reset=False):
    try:
        #  XẾP HÀNG: mỗi lúc chỉ một việc nặng chạy trên máy (xem
        #  services/work_queue.py). Chưa tới lượt thì đợi chứ không bị từ chối.
        with WQ.slot(project_id, L("Dựng video (chế độ cơ bản)", "Render video (Basic mode)"), cancelled=lambda: review_service.is_render_cancelled(project_id)):
            projects_root = config_service.get_review_root()
            handler = setup_project_logging(project_id, projects_root)
            try:
                logger = logging.getLogger("ReviewEngine")
                logger.info(f"=== BẮT ĐẦU DỰNG VIDEO REVIEW — dự án {project_id} ===")
                meta = review_service.get_metadata(project_id) or {}
                from services.vieneu_service import is_vieneu_voice
                from utils.hardware_utils import get_best_device
                if is_vieneu_voice(meta.get("target_voice", "")) and get_best_device()[0] == "cuda":
                    review_service.stt_service.release_gpu()
                import asyncio
                from services.review_engine import RenderCancelled

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(review_engine.render_project(project_id, force_reset=force_reset))
                finally:
                    loop.close()

                # Kiểm result TRƯỚC cờ cancel: nếu render đã chạy xong trọn vẹn (file ra hợp lệ)
                # mà user bấm Hủy đúng tích tắc cuối -> vẫn tính THÀNH CÔNG, không báo "đã hủy" sai.
                if result:
                    logger.info("=== TẠO VIDEO REVIEW HOÀN TẤT ===")
                    review_service.clear_render_cancel(project_id)
                    try:
                        from services.usage_reporter import record_completion
                        record_completion(str(project_id), "review")
                    except Exception as _e:
                        logger.warning(f"Không ghi được usage (review): {_e}")
                elif review_service.is_render_cancelled(project_id):
                    logger.warning("=== ĐÃ HỦY TẠO VIDEO REVIEW THEO YÊU CẦU ===")
                    review_service.mark_render_failed(
                        project_id, L("Đã hủy render theo yêu cầu người dùng.", "Render cancelled at the user's request.")
                    )
                else:
                    logger.error("=== TẠO VIDEO REVIEW THẤT BẠI ===")
                    review_service.mark_render_failed(
                        project_id, L("Render thất bại — xem synthesis.log để biết chi tiết.", "Render failed — see synthesis.log for details.")
                    )
            except Exception as e:
                from services.review_engine import RenderCancelled

                if isinstance(e, RenderCancelled) or review_service.is_render_cancelled(project_id):
                    logging.getLogger("ReviewEngine").warning(f"Synthesis cancelled: {e}")
                    review_service.mark_render_failed(
                        project_id, L("Đã hủy render theo yêu cầu người dùng.", "Render cancelled at the user's request.")
                    )
                else:
                    logging.getLogger("ReviewEngine").error(f"Synthesis failed: {e}", exc_info=True)
                    # Quan trọng: rollback render_started — tránh UI treo "Đang dựng…" mãi
                    try:
                        review_service.mark_render_failed(project_id, str(e))
                    except Exception:
                        pass
            finally:
                remove_project_logging(project_id, handler)
                _release_review(project_id)

    except WQ.WorkCancelled:
        #  Bấm Huỷ khi việc CÒN ĐANG XẾP HÀNG: thân hàm chưa chạy nên phần
        #  dọn dẹp trong ``finally`` của nó cũng chưa chạy — phải tự dọn ở
        #  đây, không thì khoá dự án không ai nhả và giao diện treo mãi.
        #  KHÔNG dùng ``logger``: trong hàm này nó là biến CỤC BỘ gán ở thân
        #  hàm (phần chưa chạy) — chạm vào là UnboundLocalError, và thế là mất
        #  luôn phần dọn dẹp bên dưới.
        logging.getLogger("ReviewService").warning(
            f"[HÀNG ĐỢI] Đã huỷ khi còn đang xếp hàng: {project_id}"
        )
        review_service.mark_render_failed(
            project_id, L("Đã hủy theo yêu cầu người dùng.", "Cancelled at the user's request.")
        )
        _release_review(project_id)
def run_ai_generate_wrapper(project_id, model):
    """Tạo kịch bản review (B-chunks) bằng DeepSeek theo lô 100 đoạn A rồi import.
    Chạy nền; log vào synthesis.log để FE (LogConsole) theo dõi tiến độ."""
    projects_root = config_service.get_review_root()
    handler = setup_project_logging(project_id, projects_root)
    logger = logging.getLogger("ReviewService")
    try:
        import services.deepseek_service as deepseek_service

        logger.info(
            f"=== BẮT ĐẦU TẠO KỊCH BẢN (AI DeepSeek, model={model}) — dự án {project_id} ==="
        )
        a_chunks = review_service.load_a_chunks(project_id)
        if not a_chunks:
            raise RuntimeError(L("Chưa có dữ liệu phân tích — hãy phân tích video trước.", "There is no analysis data yet — analyze the video first."))
        meta = review_service.get_metadata(project_id) or {}

        def _should_stop():
            return review_service.is_analysis_cancelled(project_id)

        b_chunks = deepseek_service.generate_review_bchunks(
            a_chunks, meta, model, should_stop=_should_stop
        )
        if _should_stop():
            logger.warning("=== ĐÃ HỦY TẠO KỊCH BẢN THEO YÊU CẦU ===")
            # Ghi trạng thái hủy vào metadata + xoá cờ hủy — không để cờ sót làm
            # lần chạy sau bị hủy oan / FE không biết job đã dừng.
            review_service.mark_analysis_failed(
                project_id, L("Đã hủy tạo kịch bản theo yêu cầu người dùng.", "Script generation cancelled at the user's request.")
            )
            return
        # auto_fix=True: đường TỰ ĐỘNG — sửa cục bộ b_id trùng/thiếu (đánh số lại)
        # thay vì bắt DeepSeek sinh lại toàn bộ vì vài lỗi số/ký tự nhỏ.
        review_service.import_b_chunks(project_id, b_chunks, auto_fix=True)
        review_service.clear_analysis_cancel(project_id)
        logger.info(
            f"=== TẠO KỊCH BẢN HOÀN TẤT: {len(b_chunks)} câu ==="
        )
    except Exception as e:
        # Với lỗi API DeepSeek, dùng THÔNG BÁO THÂN THIỆN (hết số dư / sai key /
        # quá tải...) để người dùng biết rõ nguyên nhân, thay vì "không sinh được
        # đoạn B nào" chung chung.
        from services.deepseek_service import DeepSeekError

        friendly = (
            e.user_message if isinstance(e, DeepSeekError) else str(e)
        )
        # logger.error -> vừa hiện trong LogConsole, vừa được gửi lên server (error_log).
        logger.error(f"Tạo kịch bản DeepSeek thất bại: {friendly}", exc_info=True)
        # Ghi lỗi vào metadata (state.analysis_error) để FE phát hiện job THẤT BẠI —
        # trước đây FE chỉ đoán qua bChunks.length nên báo "thành công" giả khi
        # project còn kịch bản cũ từ lần import trước.
        review_service.mark_analysis_failed(
            project_id, f"DeepSeek: {friendly}"
        )
    finally:
        remove_project_logging(project_id, handler)
        _release_review(project_id)


@router.post("/projects/{project_id}/ai-generate-script")
async def ai_generate_script(
    project_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    """Tạo kịch bản B tự động bằng DeepSeek (thay cho copy-paste thủ công)."""
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")

    import services.deepseek_service as deepseek_service
    if not deepseek_service.is_configured():
        raise HTTPException(
            status_code=400,
            detail=L("Chưa cấu hình API key của bộ AI đang chọn (DeepSeek / Gemini). Vào Cài đặt để nhập API key trước khi dùng.", "The API key of the selected AI (DeepSeek / Gemini) is not configured. Go to Settings to enter the API key before using it."),
        )

    a_chunks = review_service.load_a_chunks(project_id)
    if not a_chunks:
        raise HTTPException(
            status_code=400,
            detail=L("Chưa có A-chunks. Hãy phân tích video trước khi tạo kịch bản.", "There are no A-chunks yet. Analyze the video before generating the script."),
        )

    if not model:
        model = config_service.ai_model_mac_dinh()

    _acquire_review_or_409(project_id)
    try:
        review_service.clear_analysis_cancel(project_id)
        background_tasks.add_task(run_ai_generate_wrapper, project_id, model)
    except Exception:
        _release_review(project_id)
        raise

    return {"status": "generation_started", "model": model}


@router.post("/projects/{project_id}/analyze")
async def analyze_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    data: dict = Body(None),
):
    print(f"[REVIEW-API] Starting analysis for project: {project_id}")
    meta = review_service.get_metadata(project_id)
    if not meta:
        print(f"[REVIEW-API] Error: Project {project_id} not found")
        raise HTTPException(status_code=404, detail="Project not found")

    # Ngôn ngữ + nguồn phụ đề chọn Ở ĐÂY (bước phân tích) thay vì lúc tạo dự án.
    # Ghi vào metadata trước khi chạy nhận dạng.
    data = data or {}
    if data.get("source_language"):
        meta["source_language"] = data["source_language"]
    if data.get("target_language"):
        meta["target_language"] = data["target_language"]
    sub_mode = data.get("subtitle_mode")
    if sub_mode in ("whisper", "ocr", "hybrid"):
        meta["subtitle_mode"] = sub_mode
    if "ocr_region" in data:
        meta["ocr_region"] = data["ocr_region"]
    if data.get("ocr_lang"):
        meta["ocr_lang"] = data["ocr_lang"]
    # Kiểu nói + thương hiệu cho kịch bản AI (cho phép chuỗi rỗng = xoá về mặc định).
    if "speech_style" in data:
        meta["speech_style"] = str(data.get("speech_style") or "auto")
    if "brand_name" in data:
        #  XOA TRANG THI DE TRANG. Truoc day chuoi rong bi thay bang "Bubi review
        #  phim" — nguoi dung xoa o nay di, luu xong mo lai van thay ten do, va
        #  prompt soan bai dang van viet cho kenh ay.
        meta["brand_name"] = str(data.get("brand_name") or "").strip()
    review_service._save_metadata(project_id, meta)

    # Chặn chạy chồng (phân tích/dựng/tạo giọng loại trừ nhau) -> tránh xung đột DB.
    _acquire_review_or_409(project_id)
    try:
        # Xóa cờ hủy còn sót từ lần trước để không hủy oan lần chạy mới.
        review_service.clear_analysis_cancel(project_id)
        review_service.mark_analysis_started(project_id)
        # Start STT and Chunking in background wrapper (wrapper tự _release_review ở finally)
        print(f"[REVIEW-API] Dispatching background tasks: Transcription & Raw A-Segments")
        background_tasks.add_task(run_analysis_wrapper, project_id)
    except Exception:
        _release_review(project_id)  # lên lịch task thất bại -> nhả khóa ngay
        raise

    return {"status": "analysis_started"}

@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Add file size if available
    projects_root = config_service.get_review_root()
    output_path = os.path.join(projects_root, project_id, "final_review.mp4")
    if os.path.exists(output_path):
        meta["final_video_size"] = os.path.getsize(output_path)
        meta["final_video_mtime"] = int(os.path.getmtime(output_path))
        meta["final_video_absolute_path"] = os.path.abspath(output_path).replace("\\", "/")

    # Cờ cho FE biết job nền còn đang giữ khóa (dừng hẳn chưa) sau khi Hủy.
    meta["is_busy"] = _is_review_busy(project_id)

    return meta

@router.get("/projects/{project_id}/chunks")
async def get_a_chunks(project_id: str):
    print(f"[REVIEW-API] Polling/Fetching chunks for: {project_id}")
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    chunks = review_service.load_a_chunks(project_id)
    if chunks is None:
        print(f"[REVIEW-API] Chunks status: STILL PROCESSING")
        return {"status": "processing", "chunks": []}
    print(f"[REVIEW-API] Chunks status: READY ({len(chunks)} items)")
    return {"status": "ready", "chunks": chunks}


@router.post("/projects/{project_id}/import")
async def import_b_chunks(project_id: str, b_chunks: List[BChunk]):
    print(f"[REVIEW-API] Importing {len(b_chunks)} B-chunks for: {project_id}")
    raw_chunks = []
    for b in b_chunks:
        d = b.dict()
        d["b_text"] = b.get_text()
        raw_chunks.append(d)
    try:
        success = review_service.import_b_chunks(project_id, raw_chunks)
    except ValueError as e:
        # b_id trùng — báo lỗi rõ ràng để người dùng tự sửa kịch bản.
        print(f"[REVIEW-API] B-chunk import REJECTED: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    if not success:
        print(f"[REVIEW-API] Error: B-chunk import FAILED")
        raise HTTPException(status_code=400, detail="Failed to import B-chunks")
    print(f"[REVIEW-API] B-chunk import SUCCESS")
    return {"status": "success"}


class ReviewSettings(BaseModel):
    bg_volume: Optional[float] = None
    voice_bg_volume: Optional[float] = None
    separate_audio: Optional[bool] = None
    vocals_volume: Optional[float] = None
    voice_vocals_volume: Optional[float] = None
    target_voice: Optional[str] = None
    # Tốc độ đọc mặc định áp cho mọi đoạn B (chuỗi rate kiểu Edge/VieNeu: "-10%",
    # "+0%", "+15%"...). None = không đổi. Đổi tốc độ -> voice sinh lại cho khớp.
    default_rate: Optional[str] = None
    # Xê dịch SRT thông minh: đọc giọng ở tốc độ cố định (default_rate), dồn thời điểm
    # cho không chồng thay vì ép nhanh/cắt. None = không đổi.
    smart_srt_shift: Optional[bool] = None
    speech_style: Optional[str] = None
    brand_name: Optional[str] = None


@router.patch("/projects/{project_id}/settings")
async def update_review_settings(project_id: str, data: ReviewSettings):
    """Cập nhật mức âm lượng tiếng gốc (nền / khi có thoại) cho dự án review."""
    result = review_service.update_settings(
        project_id,
        bg_volume=data.bg_volume,
        voice_bg_volume=data.voice_bg_volume,
        separate_audio=data.separate_audio,
        vocals_volume=data.vocals_volume,
        voice_vocals_volume=data.voice_vocals_volume,
        target_voice=data.target_voice,
        default_rate=data.default_rate,
        smart_srt_shift=data.smart_srt_shift,
        speech_style=data.speech_style,
        brand_name=data.brand_name,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    print(f"[REVIEW-API] Updated volumes for {project_id}: {result}")
    return {"status": "ok", **result}


@router.post("/projects/{project_id}/render")
async def render_project(project_id: str, force_reset: bool = Query(False), background_tasks: BackgroundTasks = BackgroundTasks()):
    print(f"[REVIEW-API] Starting final render for: {project_id} (force_reset={force_reset})")
    meta = review_service.get_metadata(project_id)
    if not meta or not meta["state"]["b_imported"]:
        print(f"[REVIEW-API] Error: Project not ready or B-chunks missing")
        raise HTTPException(status_code=400, detail="B-chunks must be imported first")

    # Không render khi dữ liệu đang được DI CHUYỂN sang vị trí lưu mới.
    from routes.storage_routes import is_migrating
    if is_migrating():
        raise HTTPException(status_code=409, detail=L("Đang di chuyển dữ liệu sang vị trí lưu mới — chờ xong rồi hãy render.", "Data is being moved to the new storage location — wait for it to finish before rendering."))

    # Kiểm dung lượng TRƯỚC khi render — video lớn hết đĩa giữa chừng ra file hỏng.
    from utils.storage_paths import check_render_disk_space
    try:
        check_render_disk_space(
            os.path.join(review_service.projects_root, project_id),
            (meta.get("video_path"),),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Bật tách nhạc nền mà CHƯA tải Demucs -> chặn NGAY, không để render chết giữa chừng.
    if bool(meta.get("separate_audio")):
        from utils.model_paths import get_demucs_repo_path
        if get_demucs_repo_path() is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    L("Đang bật 'Tách nhạc nền' nhưng chưa tải model Demucs. "
                      "Vào 'Quản lý Model AI' để tải, hoặc tắt tách nhạc nền rồi dựng lại.",
                      "'Separate background music' is on but the Demucs model has not been downloaded. "
                      "Go to 'AI Model Management' to download it, or turn off music separation and render again.")
                ),
            )

    # Chặn chạy chồng (phân tích/dựng/tạo giọng loại trừ nhau) -> tránh xung đột DB.
    _acquire_review_or_409(project_id)
    try:
        # Mark Render Started & Run in background wrapper (wrapper tự _release_review)
        print(f"[REVIEW-API] Dispatching background task: Rendering pipeline")
        review_service.mark_render_started(project_id)
        background_tasks.add_task(run_render_wrapper, project_id, force_reset)
    except Exception:
        _release_review(project_id)
        raise

    return {"status": "rendering_started"}


@router.post("/projects/{project_id}/cancel-render")
async def cancel_render(project_id: str):
    """Hủy render đang chạy / rollback status khi UI treo."""
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    if not meta.get("state", {}).get("render_started"):
        # Vẫn cho phép rollback nếu FE nghĩ đang treo
        pass
    ok = review_service.cancel_render(project_id)
    if not ok:
        raise HTTPException(status_code=400, detail=L("Không hủy được render", "Could not cancel the render"))
    logging.getLogger("ReviewEngine").warning(
        f"Cancel render requested for project {project_id}"
    )
    return {"status": "cancelled"}


@router.post("/projects/{project_id}/cancel-analysis")
async def cancel_analysis(project_id: str):
    """Hủy phân tích (STT) đang chạy / rollback status khi UI treo."""
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    ok = review_service.cancel_analysis(project_id)
    if not ok:
        raise HTTPException(status_code=400, detail=L("Không hủy được phân tích", "Could not cancel the analysis"))
    logging.getLogger("ReviewService").warning(
        f"Cancel analysis requested for project {project_id}"
    )
    return {"status": "cancelled"}


@router.get("/projects/{project_id}/logs")
async def stream_project_logs(project_id: str):
    projects_root = config_service.get_review_root()
    log_file = os.path.join(projects_root, project_id, "synthesis.log")
    
    async def log_generator():
        # PERSISTENT (giống chế độ lồng tiếng): giữ kết nối luôn mở, KHÔNG tự đóng khi
        # bước xong → log "live" xuyên suốt analyze → render nhiều lượt, không đứt.
        # Chi phí trên app desktop (1 kết nối localhost, đọc file mỗi 0.5s) không đáng kể.
        # Kết nối chỉ kết thúc khi client (FE) ngắt.

        # NGOẠI LỆ: khi dự án đang bị xoá thì phải nhả file ra ngay — handle đọc đang
        # mở trên synthesis.log chặn xoá thư mục trên Windows (xoá hụt, còn rác đĩa).

        # Chờ file log xuất hiện — chờ mãi, không đóng.
        waited = 0
        while not os.path.exists(log_file):
            if review_service.is_log_stream_stop_requested(project_id):
                return
            if waited % 20 == 0:
                yield L("data: [SYSTEM] Đang chờ tiến trình khởi động...\n\n", "data: [SYSTEM] Waiting for the process to start...\n\n")
            await asyncio.sleep(0.5)
            waited += 1

        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            # Khi MỚI mở: seek về 256KB cuối rồi lấy ~300 dòng — KHÔNG f.read() cả
            # file (log video dài có thể hàng chục MB, mỗi lần FE mở lại SSE là 1 cú
            # đọc lớn + tốn RAM).
            try:
                f.seek(0, os.SEEK_END)
                fsize = f.tell()
                f.seek(max(0, fsize - 256 * 1024))
                if fsize > 256 * 1024:
                    f.readline()  # bỏ dòng cắt dở đầu khối
            except OSError:
                f.seek(0)
            for line in f.read().splitlines()[-300:]:
                yield f"data: {line}\n\n"

            idle = 0
            while True:
                if review_service.is_log_stream_stop_requested(project_id):
                    yield L("data: [SYSTEM] Dự án đang được xoá — đóng cửa sổ log.\n\n", "data: [SYSTEM] The project is being deleted — closing the log window.\n\n")
                    return
                line = f.readline()
                if not line:
                    # File bị cắt bớt (trim) → nhảy về cuối để tail đúng.
                    try:
                        if f.tell() > os.path.getsize(log_file):
                            f.seek(0, os.SEEK_END)
                    except OSError:
                        pass
                    idle += 1
                    if idle % 30 == 0:  # keepalive ~15s
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


@router.get("/projects/{project_id}/output")
async def get_project_output(project_id: str, download: bool = False):
    projects_root = config_service.get_review_root()
    project_dir = os.path.join(projects_root, project_id)
    output_path = os.path.join(project_dir, "final_review.mp4")
    
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Output video not found")

    # no-store: tránh browser/Electron giữ bản mp4 cũ sau khi render lại
    # (cùng URL /output -> player đen / không thấy video mới).
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }
    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=f"review_{project_id}.mp4" if download else None,
        content_disposition_type="attachment" if download else "inline",
        headers=headers,
    )

@router.get("/projects/{project_id}/srt")
async def get_project_srt(project_id: str):
    projects_root = config_service.get_review_root()
    project_dir = os.path.join(projects_root, project_id)
    srt_path = os.path.join(project_dir, "review_subtitles.srt")

    if not os.path.exists(srt_path):
        meta = review_service.get_metadata(project_id)
        if meta and meta.get("state", {}).get("render_complete"):
            try:
                a_chunks_path = os.path.join(project_dir, "a_chunks.json")
                b_chunks_path = os.path.join(project_dir, "b_chunks.json")
                with open(a_chunks_path, "r", encoding="utf-8") as f:
                    a_list = json.load(f)
                a_lookup = {str(a["a_id"]): a for a in a_list}
                with open(b_chunks_path, "r", encoding="utf-8") as f:
                    b_chunks = json.load(f)
                review_engine._generate_srt_overlay(b_chunks, a_lookup, srt_path)
            except Exception as e:
                print(f"[REVIEW-API] Failed on-the-fly SRT generation: {e}")

    if not os.path.exists(srt_path):
        raise HTTPException(status_code=404, detail="SRT subtitles not found. Please render the project first.")

    return FileResponse(
        srt_path,
        media_type="application/x-subrip",
        filename=f"subtitles_{project_id}.srt",
        content_disposition_type="attachment",
    )

@router.get("/projects")
async def list_projects(limit: Optional[int] = None):
    """Mọi dự án (trang Edit chi tiết), hoặc ``limit`` dự án mới nhất (Trang chủ — tránh
    lag khi có hàng trăm dự án). Tổng số luôn có ở header ``X-Total-Count``."""
    from fastapi.responses import JSONResponse
    from fastapi.encoders import jsonable_encoder

    if limit and limit > 0:
        ds, tong = await run_in_threadpool(review_service.list_projects_recent, int(limit))
    else:
        ds = await run_in_threadpool(review_service.list_projects)
        tong = len(ds)
    return JSONResponse(content=jsonable_encoder(ds), headers={"X-Total-Count": str(tong),
                                                               "Access-Control-Expose-Headers": "X-Total-Count"})

@router.delete("/projects/purge")
async def purge_all_projects():
    projects_root = config_service.get_review_root()
    review_service.projects_root = projects_root # Temporary sync

    # Chặn hẳn khi còn dự án đang render: xoá giữa chừng vừa hỏng dữ liệu vừa
    # để lại thư mục nửa vời, nên thà không xoá gì và nói rõ lý do.
    busy = await run_in_threadpool(review_service.list_busy_projects)
    if busy:
        raise HTTPException(
            status_code=409,
            detail=(
                L(f"Đang có {len(busy)} dự án Chi tiết chạy dở: {', '.join(busy[:3])}",
                  f"{len(busy)} Advanced projects are still running: {', '.join(busy[:3])}")
                + ("…" if len(busy) > 3 else "")
                + L(". Hãy đợi chạy xong hoặc bấm Hủy render, rồi xoá lại.",
                    ". Wait for them to finish or click Cancel render, then delete again.")
            ),
        )

    deleted, failed = await run_in_threadpool(review_service.purge_all)
    if failed:
        raise HTTPException(
            status_code=500,
            detail=(
                L(f"Đã xoá {deleted} dự án, còn {len(failed)} dự án không xoá được "
                  f"(tệp đang bị khoá): {', '.join(failed[:3])}",
                  f"Deleted {deleted} projects, {len(failed)} projects could not be deleted "
                  f"(files are locked): {', '.join(failed[:3])}")
                + ("…" if len(failed) > 3 else "")
                + L(". Hãy đóng trình phát đang mở file rồi thử lại.",
                    ". Close any player that has the file open and try again.")
            ),
        )
    return {"status": "purged", "deleted": deleted}

@router.get("/assets/{project_id}/{filename:path}")
async def get_review_asset(project_id: str, filename: str):
    projects_root = config_service.get_review_root()
    asset_path = os.path.join(projects_root, project_id, filename)
    if not os.path.exists(asset_path):
        # Check subfolders (like B1/final_clip.mp4)
        # This is simple for now, can be expanded
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(asset_path)


@router.post("/projects/{project_id}/chunks/{b_id}/generate-voice")
async def generate_chunk_voice(project_id: str, b_id: int):
    # 1. Check project
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # 2. Load b_chunks
    project_dir = os.path.join(review_engine.projects_root, project_id)
    b_chunks_path = os.path.join(project_dir, "b_chunks.json")
    if not os.path.exists(b_chunks_path):
        raise HTTPException(status_code=400, detail="B-chunks not imported yet")
    
    with open(b_chunks_path, "r", encoding="utf-8") as f:
        b_chunks = json.load(f)
        
    # Find specific B-chunk
    b_data = None
    for chunk in b_chunks:
        if chunk.get("b_id") == b_id:
            b_data = chunk
            break
            
    if not b_data:
        raise HTTPException(status_code=404, detail=f"B-chunk with ID {b_id} not found")
        
    # 3. Load a_chunks for timing
    a_chunks_path = os.path.join(project_dir, "a_chunks.json")
    if not os.path.exists(a_chunks_path):
        raise HTTPException(status_code=400, detail="A-chunks not found")
    with open(a_chunks_path, "r", encoding="utf-8") as f:
        a_chunks_raw = json.load(f)
        a_chunks_lookup = {str(a["a_id"]): a for a in a_chunks_raw}
        
    # Thời lượng đích: ưu tiên override time (khi người dùng sửa time B), nếu không
    # thì suy từ A. resolve_b_time xử lý cả 2 trường hợp nhất quán với lúc render.
    from services.review_render_cache import resolve_b_time
    b_start_time, b_end_time = resolve_b_time(b_data, a_chunks_lookup)
    if b_start_time is None:
        raise HTTPException(status_code=400, detail=L("B-chunk thiếu thời gian (source_a_ids/override không hợp lệ)", "The B-chunk is missing timing (invalid source_a_ids/override)"))
    target_clip_duration = b_end_time - b_start_time
    
    # 4. Check cache
    output_dir = os.path.join(project_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    b_clip_dir = os.path.join(output_dir, f"B{b_id}")
    os.makedirs(b_clip_dir, exist_ok=True)
    audio_path = os.path.join(b_clip_dir, "audio_tts.wav")
    
    from services.review_render_cache import load_render_cache, save_render_cache, compute_tts_fingerprint, try_reuse_clip, remember_clip
    
    render_cache = load_render_cache(project_dir)
    fp_tts = compute_tts_fingerprint(meta, b_data)
    
    hit, _ = try_reuse_clip(b_id, fp_tts, output_dir, render_cache, "audio_tts.wav", layer="tts")
    if hit:
        return {
            "status": "cached",
            "audio_url": f"/api/review/assets/{project_id}/output/B{b_id}/audio_tts.wav?t={int(os.path.getmtime(audio_path))}"
        }
        
    # 5. Generate TTS
    b_text = b_data.get("b_text", "")
    if not (b_text or "").strip():
        raise HTTPException(status_code=400, detail="Empty B-chunk text")
        
    smart_shift = bool(meta.get("smart_srt_shift", True))
    smart_fixed_rate = review_engine._smart_fixed_rate(meta.get("default_rate"))

    # Create the single job item
    jobs = [{
        "b_id": b_id,
        "text": b_text,
        "path": audio_path,
        "target_duration": (1e9 if smart_shift else target_clip_duration),
        "voice": review_engine._get_voice(meta, b_data),
        "base_rate": (
            smart_fixed_rate if smart_shift
            else (b_data.get("rate") or meta.get("default_rate") or "auto")
        ),
        "fp_tts": fp_tts,
    }]

    # Chặn chạy chồng: nếu project đang phân tích/dựng/tạo giọng khác -> 409.
    _acquire_review_or_409(project_id)
    # We must ensure to release STT model from GPU if needed
    projects_root = config_service.get_review_root()
    handler = setup_project_logging(project_id, projects_root)
    try:
        from services.vieneu_service import is_vieneu_voice
        from utils.hardware_utils import get_best_device
        if is_vieneu_voice(jobs[0]["voice"]) and get_best_device()[0] == "cuda":
            review_service.stt_service.release_gpu()
            
        await review_engine._run_tts_jobs(jobs, project_id=project_id, render_cache=render_cache, project_dir=project_dir)
    except Exception as e:
        logging.getLogger("ReviewEngine").error(f"[generate_chunk_voice] Exception for B{b_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"TTS error: {str(e)}")
    finally:
        remove_project_logging(project_id, handler)
        _release_review(project_id)
    
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
        remember_clip(render_cache, b_id, fp_tts, layer="tts")
        save_render_cache(project_dir, render_cache)

        # Đếm TẠM 1 voice sinh cho dự án review này; gộp vào thống kê khi dự án HOÀN
        # THÀNH (render xong → record_completion). Chỉ ghi local, không gửi ngay.
        try:
            from services.usage_reporter import bump_voice
            bump_voice(str(project_id), 1)
        except Exception as _e:
            logging.getLogger("ReviewEngine").warning(f"Không ghi được usage voice (review): {_e}")

        # Persist is_trimmed flag back to b_chunks.json
        is_trimmed = jobs[0].get("is_trimmed", False)
        for chunk in b_chunks:
            if chunk.get("b_id") == b_id:
                chunk["is_trimmed"] = is_trimmed
                break
        with open(b_chunks_path, "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(b_chunks, f, indent=2, ensure_ascii=False)
        
        return {
            "status": "generated",
            "is_trimmed": is_trimmed,
            "audio_url": f"/api/review/assets/{project_id}/output/B{b_id}/audio_tts.wav?t={int(os.path.getmtime(audio_path))}"
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to generate audio clip")

@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    projects_root = config_service.get_review_root()
    existed = os.path.isdir(os.path.join(projects_root, project_id))
    # PHẢI chạy ở threadpool: delete_project chờ SSE /logs nhả file synthesis.log;
    # nếu chạy thẳng trên event loop thì generator không bao giờ được chạy để đóng
    # file → xoá luôn thất bại.
    success = await run_in_threadpool(review_service.delete_project, project_id)
    if not success:
        if not existed:
            raise HTTPException(status_code=404, detail="Project not found")
        # Trước đây lỗi này bị nuốt và vẫn trả "deleted" — dữ liệu còn nguyên trên đĩa.
        raise HTTPException(
            status_code=500,
            detail=(
                L("Xoá dự án thất bại: một số tệp đang bị khoá (video/âm thanh đang mở "
                  "hoặc tiến trình dựng chưa dừng). Hãy dừng tiến trình, đóng trình phát "
                  "rồi thử lại.",
                  "Deleting the project failed: some files are locked (video/audio is open "
                  "or the render process has not stopped). Stop the process, close the player "
                  "and try again.")
            ),
        )
    # Đánh dấu dự án đã xoá để báo trạng thái lên server (giữ số đã thống kê).
    try:
        from services.usage_reporter import mark_deleted
        mark_deleted(str(project_id))
    except Exception as _e:
        logging.getLogger("ReviewRoutes").warning(f"Không ghi được usage (deleted): {_e}")
    return {"status": "deleted"}


def _review_compositor_config_path(project_id: str) -> str:
    projects_root = config_service.get_review_root()
    return os.path.join(projects_root, project_id, "compositor_config.json")


def _review_project_dir(project_id: str) -> str:
    return os.path.join(config_service.get_review_root(), project_id)


@router.get("/projects/{project_id}/b-chunks")
async def get_b_chunks(project_id: str):
    """Tải b_chunks đã import (từ b_chunks.json) — dùng cho compositor sau render."""
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    project_dir = _review_project_dir(project_id)
    b_chunks_path = os.path.join(project_dir, "b_chunks.json")
    if not os.path.exists(b_chunks_path):
        return {"chunks": []}
    with open(b_chunks_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)
    
    # Check if audio file exists for each chunk
    output_dir = os.path.join(project_dir, "output")
    for chunk in chunks:
        b_id = chunk.get("b_id")
        audio_path = os.path.join(output_dir, f"B{b_id}", "audio_tts.wav")
        chunk["audio_generated"] = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
        
    return {"chunks": chunks}


def _load_review_a_chunks_lookup(project_dir: str) -> dict:
    a_chunks_path = os.path.join(project_dir, "a_chunks.json")
    if not os.path.exists(a_chunks_path):
        return {}
    with open(a_chunks_path, "r", encoding="utf-8") as f:
        a_chunks = json.load(f)
    if not isinstance(a_chunks, list):
        return {}
    return {str(a["a_id"]): a for a in a_chunks if "a_id" in a}


def _review_source_segments(project_dir: str) -> list:
    """Segments của PHỤ ĐỀ GỐC (a_chunks) để đốt thêm vào video khi người dùng bật
    nút "Sub gốc".

    Mốc trong ``a_chunks`` ĐÃ nằm trên trục timeline (chế độ Chi tiết chiếu sang lúc
    nạp), nên dùng thẳng. Câu rỗng hoặc khoảng thời gian hỏng thì bỏ — libass gặp cue
    ``end <= start`` sẽ bỏ qua im lặng, để lọt vào chỉ tổ khó lần khi đếm thiếu câu.
    """
    a_chunks_path = os.path.join(project_dir, "a_chunks.json")
    if not os.path.exists(a_chunks_path):
        return []
    try:
        with open(a_chunks_path, "r", encoding="utf-8") as f:
            chunks = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(chunks, list):
        return []
    out = []
    for c in chunks:
        text = (c.get("text") or "").strip()
        try:
            start = float(c.get("start") or 0.0)
            end = float(c.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        if not text or end <= start:
            continue
        out.append({
            "start_time": start,
            "end_time": end,
            "source_text": text,
            "target_text": text,
        })
    return out


def _review_compositor_segments(project_dir: str, meta: dict, b_chunks: list) -> list:
    """Build compositor segments trên timeline tuyệt đối (overlay 100%).

    Chế độ NÂNG CAO: chữ tắt theo GIỌNG chứ không theo khung (xem ``TL.subtitle_end``).
    Chế độ cổ điển giữ nguyên hành vi cũ — ở đó khung vốn chính là độ dài giọng.
    """
    from services import review_timeline as _TL

    return review_b_chunks_to_segments(
        project_dir,
        b_chunks,
        a_chunks_lookup=_load_review_a_chunks_lookup(project_dir),
        fit_to_voice=_TL.is_advanced(meta),
    )


@router.get("/projects/{project_id}/compositor-segments")
async def get_review_compositor_segments(project_id: str):
    """Segments sub cho compositor preview — khớp timeline video render."""
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    project_dir = _review_project_dir(project_id)
    b_chunks_path = os.path.join(project_dir, "b_chunks.json")
    if not os.path.exists(b_chunks_path):
        return {"segments": []}
    with open(b_chunks_path, "r", encoding="utf-8") as f:
        b_chunks = json.load(f)
    return {"segments": _review_compositor_segments(project_dir, meta, b_chunks)}


@router.get("/projects/{project_id}/compositor")
async def get_review_compositor_config(project_id: str):
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    #  Dự án MỚI đích Nhật / Trung / Hàn: font mặc định là font CJK đóng gói (xem font_mac_dinh_theo_tieng).
    from utils.font_resolver import font_mac_dinh_theo_tieng

    return load_compositor_config(
        _review_compositor_config_path(project_id),
        subtitle_defaults=font_mac_dinh_theo_tieng(meta.get("target_language") or ""),
    )


@router.put("/projects/{project_id}/compositor")
async def save_review_compositor_config(project_id: str, data: dict = Body(...)):
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    save_compositor_config(_review_compositor_config_path(project_id), data)
    return {"success": True}


@router.post("/projects/{project_id}/compositor/logo")
async def upload_review_compositor_logo(
    project_id: str,
    file: UploadFile = File(...),
    auto_remove_bg: bool = Query(False),
):
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail=L("Tệp logo rỗng", "The logo file is empty"))

    project_dir = _review_project_dir(project_id)
    logo_dir = os.path.join(project_dir, "assets")
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



@router.post("/projects/{project_id}/compositor/frame-bg")
async def upload_review_frame_bg(
    project_id: str,
    file: UploadFile = File(...),
):
    """Tai ANH NEN cua khung (cho trong quanh video khi khung rong hon video)."""
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail=L("Tệp ảnh nền rỗng", "The background image file is empty"))

    bg_dir = os.path.join(_review_project_dir(project_id), "assets")
    os.makedirs(bg_dir, exist_ok=True)
    try:
        saved_path = process_and_save_frame_bg_image(content, bg_dir)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Tệp tải lên không phải ảnh hợp lệ (hỗ trợ PNG, JPG, WEBP): {e}",
        )

    return {"success": True, "path": saved_path}

@router.post("/projects/{project_id}/compositor/logo/remove-bg")
async def remove_review_compositor_logo_bg(project_id: str):
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = _review_project_dir(project_id)
    logo_path = os.path.join(project_dir, "assets", "logo.png")
    if not os.path.exists(logo_path):
        raise HTTPException(status_code=404, detail=L("Chưa có file logo để xoá nền", "There is no logo file to remove the background from"))

    try:
        saved_path = remove_logo_background_file(logo_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Xoá nền logo thất bại: {e}")

    return {"success": True, "path": saved_path}


_COMPOSITOR_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


@router.post("/projects/{project_id}/compositor/audio")
async def upload_review_compositor_audio(project_id: str, file: UploadFile = File(...)):
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    if not file.filename or not file.filename.lower().endswith(_COMPOSITOR_AUDIO_EXTS):
        raise HTTPException(
            status_code=400,
            detail=L("Audio phải là .mp3/.wav/.m4a/.aac/.ogg/.flac", "Audio must be .mp3/.wav/.m4a/.aac/.ogg/.flac"),
        )

    project_dir = _review_project_dir(project_id)
    audio_dir = os.path.join(project_dir, "assets")
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


@router.post("/projects/{project_id}/compositor/font")
async def upload_review_compositor_font(project_id: str, file: UploadFile = File(...)):
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")
    if not file.filename or not file.filename.lower().endswith((".ttf", ".otf")):
        raise HTTPException(status_code=400, detail=L("Font phải là file .ttf hoặc .otf", "The font must be a .ttf or .otf file"))

    project_dir = _review_project_dir(project_id)
    fonts_dir = get_fonts_dir(project_dir)
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


@router.post("/projects/{project_id}/export-compositor")
async def export_review_compositor(project_id: str, data: dict = Body(...)):
    #  ``async def`` mà gọi thẳng hàm CHẶN (ffmpeg chạy tới xong) là đứng nguyên vòng
    #  lặp sự kiện: suốt lúc đó backend không trả lời BẤT CỨ request nào — kể cả Hủy,
    #  kể cả luồng nhật ký. Với phim 3 tiếng là hàng chục phút "mất kết nối tới BE"
    #  trong khi ffmpeg vẫn chạy. Giao diện đi đường ``/stream`` (đã có luồng riêng),
    #  nhưng đường này vẫn mở cho ai gọi thẳng API — đẩy sang threadpool cho an toàn.
    from fastapi.concurrency import run_in_threadpool

    return await run_in_threadpool(_run_review_compositor_export, project_id, data)


#  Số lượt xuất bước 2/2 đang chạy — cho bộ canh heartbeat ở ``main.py`` biết máy đang
#  bận việc nặng (đường xuất này không đi qua ``work_queue``).
_XUAT_DANG_CHAY = 0
_XUAT_KHOA = __import__("threading").Lock()


def dang_xuat_compositor() -> bool:
    return _XUAT_DANG_CHAY > 0


def _run_review_compositor_export(project_id: str, data: dict, progress_queue=None,
                                  cancel_event=None):
    global _XUAT_DANG_CHAY
    with _XUAT_KHOA:
        _XUAT_DANG_CHAY += 1
    try:
        return _run_review_compositor_export_impl(project_id, data, progress_queue, cancel_event)
    finally:
        with _XUAT_KHOA:
            _XUAT_DANG_CHAY -= 1


def _run_review_compositor_export_impl(project_id: str, data: dict, progress_queue=None,
                                       cancel_event=None):
    #  MỐC BẮT ĐẦU lấy ở DÒNG ĐẦU TIÊN — trước mọi việc chuẩn bị. Phần chuẩn bị (dựng
    #  ASS cho hàng trăm câu, đo đĩa, đọc phụ đề gốc) mất ~16s với video 20 phút; lấy
    #  mốc sau đó là lệnh Hủy bấm trong 16s ấy bị coi là "của lượt trước" và bị bỏ qua
    #  (đo được: Hủy ở giây 3, ffmpeg vẫn khởi chạy ở giây 24 rồi chạy tiếp).
    moc_bat_dau = time.time()
    #  Import MOT LAN cho ca ham: dat trong nhanh thi Python coi day la bien cuc
    #  bo, va nhanh khong chay import se vo o cho dung no (UnboundLocalError) du
    #  dau tep da import cung ten.
    from utils import timeline_slice as TS
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = _review_project_dir(project_id)

    #  BƯỚC 1/2 CÓ CẮT SẴN KHÔNG? Khi xuất "đoạn đã chọn", bước 1/2 thường đã cắt mọi
    #  nguồn rồi mới ghép — nó để lại biên nhận nói rõ bản cắt ứng với đúng những khoảng
    #  nào. Khớp thì lấy thẳng bản đó; không khớp (hoặc bước 1/2 phải lui về đường dựng
    #  cả phim) thì rơi xuống nhánh tự cắt bên dưới như cũ.
    khoang_san = None
    if data.get("ranges"):
        khoang_san = TS.doc_manifest(project_dir, data["ranges"])

    if khoang_san:
        input_video = os.path.join(project_dir, TS.TEP_BAN_CAT)
    else:
        input_video = os.path.join(project_dir, "final_review.mp4")
    if not os.path.exists(input_video):
        raise HTTPException(status_code=400, detail=L("Chưa có video render — hãy render trước", "There is no rendered video yet — render first"))

    # Kiểm dung lượng TRƯỚC khi xuất compositor — tránh hết đĩa giữa chừng.
    from utils.storage_paths import check_render_disk_space
    try:
        check_render_disk_space(project_dir, (input_video,))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    config = data.get("config") or {}
    burn_subtitles = data.get("burn_subtitles", True)
    segments = None
    if burn_subtitles:
        b_chunks_path = os.path.join(project_dir, "b_chunks.json")
        if os.path.exists(b_chunks_path):
            with open(b_chunks_path, "r", encoding="utf-8") as f:
                b_chunks = json.load(f)
            raw_segs = _review_compositor_segments(project_dir, meta, b_chunks)
            if raw_segs:
                segments = [
                    {
                        # Dùng ĐÚNG mốc start/end (không đẩy sớm 0.15s) -> khớp preview.
                        "start_time": max(0.0, s["start_time"]),
                        "end_time": max(0.0, s["end_time"]),
                        "source_text": s["source_text"],
                        "target_text": s["target_text"],
                        "uid": s.get("uid") or "",
                    }
                    for s in raw_segs
                ]

    #  PHỤ ĐỀ NHIỀU THỨ TIẾNG — ``sub_lang`` (đốt), ``second_sub_lang`` (dòng thứ hai),
    #  ``srt_langs`` (.srt cạnh video), ``soft_langs`` (phụ đề mềm). Xem
    #  ``review_sub_tracks.chon_xuat``. Mốc giờ đi CHUNG với câu chính, nên cắt "đoạn đã
    #  chọn" ở dưới áp y hệt cho mọi thứ tiếng.
    from services import review_sub_tracks as ST

    chon = ST.chon_xuat(data, meta.get("target_language") or "vi")
    main_lang = chon["main"]
    sub_lang, second_lang = chon["sub_lang"], chon["second_lang"]
    srt_langs, soft_langs = chon["srt_langs"], chon["soft_langs"]
    phu_de_theo_tieng: dict = {}
    if chon["can"]:
        b_chunks_path = os.path.join(project_dir, "b_chunks.json")
        with open(b_chunks_path, "r", encoding="utf-8") as f:
            _b_all = json.load(f)
        segs_chinh = [
            {**s, "start_time": max(0.0, s["start_time"]), "end_time": max(0.0, s["end_time"])}
            for s in _review_compositor_segments(project_dir, meta, _b_all)
        ]
        try:
            phu_de_theo_tieng = ST.theo_tieng(chon, segs_chinh, ST.doc(project_dir))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if sub_lang and burn_subtitles:
            segments = phu_de_theo_tieng[sub_lang] or None
            logging.getLogger("ReviewCompositor").info(
                f"[nâng cao] Đốt phụ đề thứ tiếng '{sub_lang}' ({len(segments or [])} câu)."
            )

    #  ---------------------------------------------------------- XUẤT ĐOẠN ĐÃ CHỌN
    #  ``ranges`` = các khoảng [giây, giây] người dùng khoanh trên timeline. Có thì cắt
    #  bản dựng bước 1/2 theo đúng những đoạn đó rồi nối lại, và DỊCH MỌI MỐC (phụ đề,
    #  vùng che, logo, watermark, khối chữ) sang dòng thời gian mới — xem
    #  ``utils/timeline_slice.py``. Bước 2/2 bên dưới không đổi một dòng nào: nó chỉ
    #  nhận vào một video ngắn hơn và bộ mốc đã dịch sẵn.
    #  Import Ở NGOÀI mọi nhánh. Đặt trong nhánh thì Python coi ``CompositorService`` là
    #  biến cục bộ của cả hàm, và nhánh KHÔNG chạy import sẽ vỡ ở chỗ gọi tận cuối hàm
    #  (UnboundLocalError) — dù ngay đầu tệp đã có import cùng tên.
    from services.compositor_service import CompositorService, FFMPEG_EXE
    from services.compositor_service import _has_audio_stream

    khoang = []
    tep_cat = ""
    if khoang_san:
        #  Bước 1/2 đã cắt: chỉ còn việc dịch mốc cho khớp video ngắn đó.
        khoang = khoang_san
        logging.getLogger("ReviewCompositor").info(
            f"[nâng cao] Xuất ĐOẠN ĐÃ CHỌN: bước 1/2 đã ghép sẵn đúng {len(khoang)} đoạn "
            f"({TS.tong_thoi_luong(khoang):.1f}s) — bước 2/2 khỏi cắt lại."
        )
        segments = TS.cat_cues(segments, khoang) if segments else segments
        phu_de_theo_tieng = {k: TS.cat_cues(v, khoang) for k, v in phu_de_theo_tieng.items()}
        config = TS.cat_config(config, khoang)
    elif data.get("ranges"):
        from services.video_service import VideoService

        tong_dai = 0.0
        try:
            tong_dai = float(VideoService.get_video_duration(input_video) or 0)
        except Exception:  # noqa: BLE001 — đo hụt thì thôi, chỉ mất phép kẹp mép cuối
            pass
        khoang = TS.gom_khoang(data["ranges"], tong_dai=tong_dai)
        if not khoang:
            raise HTTPException(status_code=400,
                                detail=L("Các đoạn đã chọn không hợp lệ (không có đoạn nào dài hơn 0 giây).", "The selected segments are invalid (no segment is longer than 0 seconds)."))
        dai_moi = TS.tong_thoi_luong(khoang)
        #  Ghi vào synthesis.log của dự án (nơi nút "Xem log" đọc) — module này không
        #  giữ logger ở tầm module, mỗi chỗ tự lấy theo tên.
        logging.getLogger("ReviewCompositor").info(
            f"[nâng cao] Xuất ĐOẠN ĐÃ CHỌN: {len(khoang)} đoạn, tổng {dai_moi:.1f}s "
            f"(video đầy đủ {tong_dai:.1f}s)."
        )
        if progress_queue is not None:
            progress_queue.put({"type": "progress", "percent": 3,
                                "message": f"Đang cắt {len(khoang)} đoạn đã chọn…"})
        tep_cat = os.path.join(project_dir, TS.TEP_BAN_CAT)
        v_args, _, nhan = CompositorService.intermediate_encode_args()
        cmd = TS.lenh_cat(FFMPEG_EXE, input_video, tep_cat, khoang,
                          v_args=v_args or None, co_tieng=_has_audio_stream(input_video))
        ok_cat, loi_cat = CompositorService._run_ffmpeg(
            cmd, duration_sec=dai_moi,
            should_cancel=lambda: (
                (cancel_event is not None and cancel_event.is_set())
                or review_service.is_render_cancelled_since(project_id, moc_bat_dau)
            ),
        )
        if not ok_cat and loi_cat != "__CANCELLED__" and v_args:
            #  Encoder GPU hỏng giữa chừng thì lui về CPU — y như bước 1/2 vẫn làm.
            logging.getLogger("ReviewCompositor").warning(
                f"[nâng cao] Cắt đoạn bằng {nhan} hỏng, thử lại bằng CPU.")
            ok_cat, loi_cat = CompositorService._run_ffmpeg(
                TS.lenh_cat(FFMPEG_EXE, input_video, tep_cat, khoang,
                            co_tieng=_has_audio_stream(input_video)),
                duration_sec=dai_moi,
            )
        if not ok_cat:
            if loi_cat == "__CANCELLED__":
                thong_bao = L("Đã hủy xuất video theo yêu cầu.", "Video export cancelled at your request.")
                if progress_queue is not None:
                    progress_queue.put({"type": "error", "message": thong_bao, "cancelled": True})
                    return None
                raise HTTPException(status_code=409, detail=thong_bao)
            raise HTTPException(status_code=500,
                                detail=f"Không cắt được các đoạn đã chọn: {loi_cat[:300]}")
        input_video = tep_cat
        segments = TS.cat_cues(segments, khoang) if segments else segments
        phu_de_theo_tieng = {k: TS.cat_cues(v, khoang) for k, v in phu_de_theo_tieng.items()}
        config = TS.cat_config(config, khoang)

    custom_path = data.get("output_path") or data.get("save_path")
    if custom_path:
        output_path = os.path.normpath(custom_path)
        export_filename = os.path.basename(output_path)
    else:
        aspect = config.get("aspectRatio", "source")
        export_filename = data.get("filename") or f"compositor_review_{aspect}.mp4"
        output_path = os.path.normpath(os.path.join(project_dir, export_filename))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def on_progress(percent: int, message: str) -> None:
        if progress_queue is not None:
            progress_queue.put(
                {"type": "progress", "percent": percent, "message": message}
            )

    if progress_queue is not None:
        on_progress(1, L("Đang chuẩn bị xuất review...", "Preparing to export review..."))

    #  CỜ HỦY ĐỌC THEO MỐC CỦA LƯỢT NÀY (``moc_bat_dau`` lấy ở đầu hàm). Cờ hủy là một
    #  TỆP dùng chung cho cả dự án và chỉ ``/render`` (bước 1/2) mới xoá. Từ khi bước này
    #  biết đọc cờ, một lần bấm Hủy ở bước 2/2 để cờ nằm lại — lần xuất KẾ TIẾP chết ngay
    #  sau 3 giây với "rendering failed" (bắt được khi chạy thật). Xoá cờ lúc bắt đầu thì
    #  lại xoá nhầm cờ của một lượt khác đang dừng dở (cũng bắt được khi chạy thật). Nên:
    #  chỉ nghe lệnh Hủy đặt SAU mốc bắt đầu của chính lượt này.

    #  PHỤ ĐỀ GỐC — chỉ dựng khi người dùng đã bật nút "Sub gốc" trên khung xem trước.
    #  Đọc a_chunks mà không ai bật thì vừa tốn công vừa dễ đốt nhầm vào video.
    source_segments = (
        _review_source_segments(project_dir) if config.get("showSourceSubs") else None
    )
    if khoang and source_segments:
        source_segments = TS.cat_cues(source_segments, khoang)

    #  VÙNG CHE "CHỈ KHI CÓ SUB": cần mốc PHỤ ĐỀ GỐC (câu mà vùng che giấu đi) kể cả khi
    #  không bật "Sub gốc". Đặt vào BẢN SAO config (khoá ``_`` chỉ sống trong lượt xuất,
    #  không bao giờ ghi xuống ``compositor_config.json``). Xem ``_mask_enable_expr``.
    if any((z or {}).get("onlyWithSubs") for z in (config.get("maskZones") or [])):
        cues = source_segments if source_segments is not None else _review_source_segments(project_dir)
        if khoang and source_segments is None:
            cues = TS.cat_cues(cues, khoang)
        config = {**config, "_mask_cues": [(c["start_time"], c["end_time"]) for c in (cues or [])]}

    #  THỨ TIẾNG THỨ HAI dùng lớp "Sub gốc" (thay cho câu gốc). Đặt SAU khối vùng che ở
    #  trên: vùng che "chỉ khi có sub" vẫn bám mốc phụ đề GỐC, không bám thứ tiếng thứ hai.
    if second_lang:
        source_segments = phu_de_theo_tieng.get(second_lang) or None
        config = {**config, "showSourceSubs": bool(source_segments)}
        logging.getLogger("ReviewCompositor").info(
            f"[nâng cao] Hiện thêm thứ tiếng '{second_lang}' ({len(source_segments or [])} câu) "
            f"ở dòng phụ."
        )

    loi_ffmpeg: list[str] = []

    if CompositorService.render(
        input_video,
        output_path,
        config,
        segments,
        source_segments=source_segments,
        on_progress=on_progress if progress_queue is not None else None,
        #  NUT HUY PHAI AN O CA BUOC NAY. Day la buoc 2/2 (dot phu de / logo /
        #  watermark len hinh) — voi phim 3 tieng no la chang lau nhat, ma truoc day
        #  khong he co duong huy: nguoi dung bam Huy, giao dien bao "dang huy", con
        #  may cu chay tiep hang chuc phut. Dung CHUNG mot co voi buoc 1/2 nen chi can
        #  bam mot lan la ca hai chang deu dung.
        #  Cờ thứ hai: khách ngắt luồng SSE (bấm Hủy chỉ abort() request, đóng tab, F5).
        should_cancel=lambda: (
            (cancel_event is not None and cancel_event.is_set())
            or review_service.is_render_cancelled_since(project_id, moc_bat_dau)
        ),
        bao_loi=loi_ffmpeg.append,
    ):
        #  Mọi thứ sinh ra cho lượt xuất đoạn (bản cắt, nguồn cắt, bản trộn giọng riêng,
        #  biên nhận) chỉ sống trong một lượt — để lại thì mỗi lần xuất đoạn lại bỏ quên
        #  vài trăm MB trong thư mục dự án, mà biên nhận cũ còn đó là lượt sau tưởng đã
        #  có sẵn bản cắt hợp lệ.
        if khoang:
            TS.don_ban_cat(project_dir)
        loi_soft = ""
        if soft_langs:
            loi_soft = ST.nhung_phu_de_mem(output_path, soft_langs, phu_de_theo_tieng, main_lang)
        record_compositor_export(
            _review_compositor_config_path(project_id), output_path, full=not khoang,
        )
        file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        #  Tệp .srt từng thứ tiếng cạnh video: "<tên video>.<thứ tiếng>.srt" (quy ước
        #  trình phát VLC / MPC / YouTube nhận ra thứ tiếng).
        tep_srt = []
        for lg in srt_langs:
            segs_lg = phu_de_theo_tieng.get(lg) or []
            duong = os.path.splitext(output_path)[0] + f".{lg}.srt"
            try:
                n_srt = ST.ghi_srt(duong, segs_lg)
                tep_srt.append({"lang": lg, "path": duong.replace("\\", "/"), "lines": n_srt})
            except OSError as e:
                logging.getLogger("ReviewCompositor").error(f"[nâng cao] Không ghi được {duong}: {e}")
        payload = {
            "success": True,
            "filename": export_filename,
            "file_path": output_path.replace("\\", "/"),
            "file_size": file_size,
            "srt_files": tep_srt,
            "soft_langs": [] if loi_soft else soft_langs,
            "soft_error": loi_soft,
        }
        if progress_queue is not None:
            progress_queue.put({"type": "done", **payload})
            return payload
        return payload

    #  Người dùng bấm Hủy thì nói đúng là đã hủy — không phải "rendering failed".
    if (cancel_event is not None and cancel_event.is_set()) or \
            review_service.is_render_cancelled_since(project_id, moc_bat_dau):
        thong_bao = L("Đã hủy xuất video theo yêu cầu.", "Video export cancelled at your request.")
        if progress_queue is not None:
            progress_queue.put({"type": "error", "message": thong_bao, "cancelled": True})
            return None
        raise HTTPException(status_code=409, detail=thong_bao)

    #  KEM NGUYEN NHAN. Truoc day o day chi co dung cau "Compositor rendering failed":
    #  khach bao loi thi ca ho lan ta deu khong biet gi them, phai di doc nhat ky — ma
    #  nhat ky thi bi cat mat dung doan co nguyen nhan.
    thong_bao = L("Xuất video thất bại ở bước đốt phụ đề / logo lên hình.", "Video export failed at the step of burning subtitles / logo onto the picture.")
    if loi_ffmpeg:
        thong_bao += L(f"\nFFmpeg báo: {loi_ffmpeg[-1]}", f"\nFFmpeg reported: {loi_ffmpeg[-1]}")
    if progress_queue is not None:
        progress_queue.put({"type": "error", "message": thong_bao})
        return None
    raise HTTPException(status_code=500, detail=thong_bao)


@router.post("/projects/{project_id}/export-compositor/stream")
async def export_review_compositor_stream(project_id: str, data: dict = Body(...)):
    # Ghi log xuất compositor vào synthesis.log (stage render) để nút "Xem log"
    # trên UI mở lại xem được khi xuất video có sự cố.
    projects_root = config_service.get_review_root()
    handler = setup_project_logging(project_id, projects_root)
    logger = logging.getLogger("ReviewCompositor")

    def worker(progress_queue, co_huy):
        try:
            logger.info(f"=== BẮT ĐẦU XUẤT VIDEO COMPOSITOR — dự án {project_id} ===")
            ket_qua = _run_review_compositor_export(
                project_id, data, progress_queue=progress_queue, cancel_event=co_huy,
            )
            #  Qua luong SSE thi that bai / huy KHONG raise ma tra None (loi da day vao
            #  progress_queue) — ghi "HOAN TAT" vo dieu kien la noi doi trong nhat ky.
            if ket_qua:
                logger.info("=== XUẤT VIDEO COMPOSITOR HOÀN TẤT ===")
            else:
                logger.error("=== XUẤT VIDEO COMPOSITOR THẤT BẠI / ĐÃ HỦY — xem dòng lỗi phía trên ===")
        except HTTPException as exc:
            logger.error(f"Xuất video compositor thất bại: {exc.detail}")
            progress_queue.put({"type": "error", "message": str(exc.detail)})
        except Exception as exc:
            logger.exception(f"Xuất video compositor lỗi: {exc}")
            progress_queue.put({"type": "error", "message": str(exc)})
        finally:
            remove_project_logging(project_id, handler)

    return compositor_export_sse_response(worker)
