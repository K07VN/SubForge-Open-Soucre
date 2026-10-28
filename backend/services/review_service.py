from utils.i18n import L
import os
import json
import logging
import uuid
import shutil
import stat
import time
import threading

from datetime import datetime
from services.stt_service import get_stt_service
from services.video_service import VideoService
from services.tts_service import TTSService

logger = logging.getLogger("ReviewService")

# Các project_id đang cần SSE /logs nhả file synthesis.log ra (để xoá được thư mục).
# Endpoint stream log giữ file mở vĩnh viễn; trên Windows handle đó chặn xoá.
_LOG_STREAM_STOP = set()


def find_duplicate_b_ids(b_chunks):
    """Tìm các b_id bị TRÙNG trong danh sách câu.

    b_id là danh tính dùng để đặt tên thư mục clip (B{b_id}/audio_tts.wav),
    khóa cache TTS và đánh dòng SRT. Nếu import thủ công đưa vào 2 câu cùng b_id
    thì cả hai ghi ĐÈ cùng một file audio (đua ghi khi sinh song song) và SRT bị
    chồng — log cũng hiện "câu 2, 3, 4" lặp lại. KHÔNG tự sửa: trả danh sách b_id
    trùng (đã sắp xếp) để báo cho người dùng tự sửa lại kịch bản."""
    seen = set()
    dups = set()
    for c in b_chunks:
        try:
            bid = int(c.get("b_id"))
        except (TypeError, ValueError):
            continue
        if bid in seen:
            dups.add(bid)
        else:
            seen.add(bid)
    return sorted(dups)


def find_b_id_gaps(b_chunks):
    """Tìm các b_id BỊ THIẾU khiến chuỗi không liền mạch (vd nhảy 49 -> 60, thiếu
    50..59). b_id phải liên tục 1,2,3,…,N; thiếu số = thiếu câu -> đoạn đó không có
    lời khi dựng. Trả danh sách b_id thiếu (đã sắp xếp) để báo người dùng bổ sung."""
    ids = set()
    for c in b_chunks:
        try:
            ids.add(int(c.get("b_id")))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    return [i for i in range(1, max(ids) + 1) if i not in ids]


def renumber_b_ids_sequential(b_chunks):
    """Đánh số lại b_id tuần tự 1..N theo THỨ TỰ trong danh sách.

    Dùng cho đường TỰ ĐỘNG (DeepSeek): các lỗi nhỏ như b_id trùng/thiếu/không
    liên tục do model xuất ra được sửa CỤC BỘ tại đây, tránh phải bắt DeepSeek
    sinh lại toàn bộ kịch bản chỉ vì vài lỗi số/ký tự. Trả (chunks, số câu đổi id)."""
    n_fixed = 0
    for i, c in enumerate(b_chunks):
        new_id = i + 1
        try:
            old_id = int(c.get("b_id"))
        except (TypeError, ValueError):
            old_id = None
        if old_id != new_id:
            n_fixed += 1
        c["b_id"] = new_id
    return b_chunks, n_fixed


class AnalysisCancelled(Exception):
    """Người dùng hủy bước phân tích (STT) giữa chừng."""


#: Thư mục con của gốc review KHÔNG phải dự án (không có metadata.json) — dọn "mồ côi" phải bỏ qua.
THU_MUC_RIENG = frozenset({"downloads", "mau"})


class ReviewService:
    def __init__(self, projects_root=None):
        from services.config_service import get_config_service
        self.config_service = get_config_service()
        self._projects_root = projects_root
        
        self.stt_service = get_stt_service()
        self.tts_service = TTSService()
        
    @property
    def projects_root(self):
        if self._projects_root: return self._projects_root
        return self.config_service.get_review_root()

    @projects_root.setter
    def projects_root(self, value):
        self._projects_root = value
        try:
            os.makedirs(self.projects_root, exist_ok=True)
        except Exception as e:
            logger.warning(f"Failed to create projects_root {self.projects_root}: {e}")

    def create_project(self, video_path, video_duration, source_lang="auto",
                       target_lang="vi", voice="vi-VN-NamMinhNeural", title="",
                       ap_mac_dinh=False):
        """``ap_mac_dinh``: dự án CHI TIẾT mới -> áp "mặc định cho dự án mới" người dùng đã
        lưu (services/mac_dinh_du_an.py). Chỉ bên gọi biết đây là dự án mới của chế độ
        nào, nên phải bật tường minh — ``/enable`` còn gọi cho cả dự án cũ."""
        # Ngôn ngữ nguồn/đích giờ chọn Ở BƯỚC PHÂN TÍCH (không bắt buộc lúc tạo) —
        # để mặc định, analyze endpoint sẽ ghi đè trước khi nhận dạng.
        project_id = str(uuid.uuid4())
        project_dir = os.path.join(self.projects_root, project_id)
        os.makedirs(project_dir, exist_ok=True)

        # Tên dự án: ưu tiên title truyền vào (tên người dùng đặt lúc tải), nếu trống
        # thì lấy tên file (bỏ đuôi) làm mặc định thay vì để trống -> hiện id.
        # DỰ ÁN TRỐNG (không có phim gốc) thì không có tên tệp nào để lấy — đặt tên chung
        # thay vì để rỗng, không thì danh sách dự án hiện nguyên cái uuid.
        proj_title = (
            (title or "").strip()
            or os.path.splitext(os.path.basename(video_path))[0]
            or "Dự án trống"
        )

        # Mặc định BẬT tách giọng/nhạc nền CHỈ khi có GPU. Máy không GPU -> mặc định
        # TẮT (Demucs trên CPU rất chậm); người dùng vẫn bật lại bằng toggle nếu muốn.
        try:
            import torch
            default_separate = bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001
            default_separate = False

        metadata = {
            "id": project_id,
            "mode": "review",
            "title": proj_title,
            "video_path": video_path,
            "video_duration": video_duration,
            "source_language": source_lang or "auto",
            "target_language": target_lang or "vi",
            "target_voice": voice or "vi-VN-NamMinhNeural",
            # Nguồn phụ đề cho bước phân tích: "whisper" | "ocr" | "hybrid".
            "subtitle_mode": "whisper",
            "ocr_region": None,     # {x,y,w,h} chuẩn hoá 0..1 (khi dùng OCR)
            "ocr_lang": "ch",       # model OCR (Trung+Anh)
            # Overlay 100% — giữ video gốc, viết lại lời bằng AI
            "compression_ratio": 1.0,
            "is_already_reviewed": True,
            "created_at": datetime.now().isoformat(),
            # Mặc định theo chế độ review (mirror ``ADV.MIX_DEFAULTS`` và
            # ``batch_store.DEFAULT_CONFIG`` — ba đường phải ra cùng một bộ số):
            # - Nhạc/âm gốc: 100% lúc AI không nói, hạ còn 25% lúc AI đang nói
            # - Giọng gốc (chỉ có khi đã tách): im hẳn, vì đã có giọng AI đọc thay
            # - Tách giọng & nhạc nền: TẮT (False)
            # - Xê dịch SRT khớp giọng: BẬT (True)
            "bg_volume": 1.0,
            "voice_bg_volume": 0.25,
            "separate_audio": False,
            "smart_srt_shift": True,
            # Kiểu nói/thể loại cho kịch bản AI (preset id hoặc text tự nhập;
            # "auto" = AI tự xử lý) + tên thương hiệu chèn vào prompt Storyteller.
            "speech_style": "auto",
            # TEN KENH DE TRONG khi tao du an. Truoc day dat san "Bubi review phim"
            # — ten mot kenh CU THE — nen moi du an moi deu mang no, va prompt SOAN
            # BAI DANG YOUTUBE nhet thang vao ("Kenh: Bubi review phim"): AI viet
            # tieu de cho kenh do du nguoi dung dang lam kenh khac han. De trong thi
            # prompt bo han dong ay, AI viet trung tinh; ai muon gan thuong hieu thi
            # go vao o "Ten kenh" o bang Chuan bi.
            "brand_name": "",
            "vocals_volume": 0.0,
            "voice_vocals_volume": 0.0,
            "state": {
                "stt_complete": False,
                "a_chunks_generated": False,
                "analysis_started": False,
                "b_imported": False,
                "render_started": False,
                "render_complete": False,
                "render_run_id": 0,
            }

        }

        if ap_mac_dinh:
            try:
                from services import mac_dinh_du_an as MD
                from services import review_advanced_engine as ADV

                md = MD.cai_dat("chi_tiet", MD.KHOA_CHI_TIET)
                metadata.update(md)
                #  Đã chọn mức âm lượng làm mặc định = "đã tự chỉnh" (ADV.mix_touched):
                #  thiếu cờ thì lúc dựng engine đi đường tắt ``-c copy``, bỏ qua các mức đó.
                if any(k in md for k in ADV.PROJECT_MIX_KEYS):
                    metadata[ADV.MIX_TOUCHED_FIELD] = True
                MD.ap_dung_hinh("chi_tiet", project_dir)
            except Exception as e:  # noqa: BLE001 — tạo dự án không được hỏng vì mặc định
                logger.warning(L(f"Không áp được mặc định cho dự án mới {project_id}: {e}", f"Could not apply the defaults to the new project {project_id}: {e}"))

        self._save_metadata(project_id, metadata)
        return project_id

    def _save_metadata(self, project_id, metadata):
        path = os.path.join(self.projects_root, project_id, "metadata.json")
        # storage_size_bytes là field động (tính lúc đọc trong get_metadata),
        # không được ghi đè vào file metadata.json.
        metadata.pop("storage_size_bytes", None)
        # Ghi ATOMIC (temp + replace): metadata bị ghi đồng thời từ endpoint hủy và
        # job nền; ghi thẳng mà crash/đụng độ giữa chừng -> JSON hỏng -> project
        # "biến mất" khỏi list (get_metadata lỗi). os.replace là atomic trên Windows.
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        try:
            os.replace(tmp_path, path)
        except OSError:
            # OneDrive đôi khi khóa file thoáng qua — thử lại 1 lần rồi bỏ cuộc.
            time.sleep(0.2)
            os.replace(tmp_path, path)

    def mark_analysis_started(self, project_id):
        meta = self.get_metadata(project_id)
        if meta:
            meta["state"]["analysis_started"] = True
            # Xóa lỗi lần trước để banner cảnh báo cũ không còn hiện khi chạy lại.
            meta["state"].pop("analysis_error", None)
            # Chạy LẠI phân tích: hạ cờ hoàn tất để UI hiện màn "đang phân tích"
            # thay vì storyboard cũ (run_transcription/generate_a_chunks sẽ bật lại).
            meta["state"]["stt_complete"] = False
            meta["state"]["a_chunks_generated"] = False
            self._save_metadata(project_id, meta)
            # Xoá log cũ (giống mark_render_started): phân tích/OCR video dài ghi dồn
            # log rất lớn -> SSE mở lại phải đọc file khổng lồ.
            log_file = os.path.join(self.projects_root, project_id, "synthesis.log")
            try:
                with open(log_file, "w", encoding="utf-8"):
                    pass
            except OSError:
                pass
            return True
        return False

    def recover_stale_states(self):
        """Gọi MỘT LẦN lúc backend khởi động: app crash/tắt giữa chừng để lại
        analysis_started/render_started=True vĩnh viễn trong metadata -> UI treo
        'Đang phân tích/dựng...' mãi. Rollback các cờ này thành lỗi 'bị gián đoạn'."""
        if not os.path.exists(self.projects_root):
            return
        recovered = 0
        for pid in os.listdir(self.projects_root):
            try:
                meta = self.get_metadata(pid)
                if not meta:
                    continue
                state = meta.get("state") or {}
                changed = False
                if state.get("analysis_started"):
                    state["analysis_started"] = False
                    state["analysis_error"] = (
                        L("Phân tích bị gián đoạn do ứng dụng đóng giữa chừng. Hãy chạy lại.", "The analysis was interrupted because the app was closed midway. Please run it again.")
                    )
                    changed = True
                    self._append_interrupt_log(
                        pid, L("PHÂN TÍCH bị gián đoạn do ứng dụng đóng giữa chừng. Hãy bấm chạy lại.", "ANALYSIS was interrupted because the app was closed midway. Please click run again.")
                    )
                if state.get("render_started"):
                    meta = self._restore_render_idle_state(meta, pid)
                    meta["state"]["render_error"] = (
                        L("Render bị gián đoạn do ứng dụng đóng giữa chừng. Hãy render lại.", "The render was interrupted because the app was closed midway. Please render again.")
                    )
                    changed = True
                    self._append_interrupt_log(
                        pid, L("DỰNG VIDEO bị gián đoạn do ứng dụng đóng giữa chừng. Hãy bấm Dựng lại.", "VIDEO BUILD was interrupted because the app was closed midway. Please click Build again.")
                    )
                if changed:
                    self._save_metadata(pid, meta)
                    self.clear_render_cancel(pid)
                    self.clear_analysis_cancel(pid)
                    recovered += 1
            except Exception as e:
                logger.warning(L(f"Recovery project {pid} lỗi (bỏ qua): {e}", f"Recovery of project {pid} failed (ignored): {e}"))
        if recovered:
            logger.info(L(f"Đã khôi phục trạng thái {recovered} dự án review bị gián đoạn.", f"Restored the state of {recovered} interrupted review projects."))

    def _append_interrupt_log(self, project_id, message):
        """Ghi 1 dòng cuối vào synthesis.log báo tác vụ BỊ GIÁN ĐOẠN. Vì log cũ dừng
        giữa chừng (vd '...23/28 câu') nên khi mở lại LogConsole trông như đang chạy;
        dòng này (định dạng [ERROR] -> LogConsole tô đỏ) cho người dùng biết đã dừng."""
        try:
            log_file = os.path.join(self.projects_root, project_id, "synthesis.log")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{ts} [ERROR] ReviewEngine: === {message} ===\n")
        except OSError:
            pass  # không có log cũ / ghi lỗi -> bỏ qua, metadata vẫn đã rollback

    def _get_dir_size(self, path):
        """Calculate total size of a directory in bytes."""
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total_size += os.path.getsize(fp)
        except Exception as e:
            logger.warning(f"Error calculating size for {path}: {e}")
        return total_size

    # Cache size thư mục theo project (TTL 60s): _get_dir_size os.walk cả cây output
    # (hàng nghìn file B*/...), mà get_metadata bị FE poll liên tục + list_projects gọi
    # cho MỌI project -> không cache thì tự đốt IO/CPU khi đang render (OneDrive càng tệ).
    _dir_size_cache: dict = {}
    _DIR_SIZE_TTL = 60.0

    def _get_dir_size_cached(self, project_id):
        now = time.monotonic()
        hit = self._dir_size_cache.get(project_id)
        if hit and now - hit[0] < self._DIR_SIZE_TTL:
            return hit[1]
        size = self._get_dir_size(os.path.join(self.projects_root, project_id))
        self._dir_size_cache[project_id] = (now, size)
        return size

    def get_metadata(self, project_id):
        path = os.path.join(self.projects_root, project_id, "metadata.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            # JSON hỏng (crash giữa lúc ghi bản cũ) -> coi như không đọc được, đừng
            # để 1 project hỏng làm sập cả list_projects/endpoint khác.
            logger.error(L(f"metadata.json hỏng cho project {project_id}: {e}", f"metadata.json is corrupted for project {project_id}: {e}"))
            return None
        # Auto-heal: nếu phân tích đã xong hoàn toàn (stt_complete + a_chunks_generated),
        # chắc chắn analysis_started = False để không bao giờ treo UI.
        state = meta.get("state") or {}
        if state.get("stt_complete") and state.get("a_chunks_generated") and state.get("analysis_started"):
            state["analysis_started"] = False

        # Add dynamic storage info (cached — os.walk mỗi lần poll rất đắt)
        meta["storage_size_bytes"] = self._get_dir_size_cached(project_id)
        return meta

    def run_transcription(self, project_id):
        meta = self.get_metadata(project_id)
        if not meta:
            return False

        logger.info(
            L(f"=== BẮT ĐẦU NHẬN DẠNG LỜI THOẠI (REVIEW) — dự án {project_id} ===", f"=== STARTING SPEECH RECOGNITION (REVIEW) — project {project_id} ===")
        )

        # Cho phép hủy giữa chừng.
        def _check_cancel():
            if self.is_analysis_cancelled(project_id):
                raise AnalysisCancelled()

        sub_mode = meta.get("subtitle_mode", "whisper")
        region = meta.get("ocr_region")
        # Mã ngôn ngữ OCR suy từ ngôn ngữ nguồn (để lọc rác theo script: Anh giữ từ
        # ngắn 'No/Hi', Trung bỏ Latin ngắn). Không tin ocr_lang cứng từ FE.
        _src = (meta.get("source_language") or "").lower()
        ocr_lang = (
            "en" if _src in ("en", "eng", "en-us", "en-gb")
            else "ja" if _src in ("ja", "ja-jp", "japan")
            else "vi" if _src in ("vi", "vi-vn")
            else "ch"
        )
        video_path = meta["video_path"]

        def _normalize_lang(lang):
            if not lang or lang == "auto":
                return None
            return "zh" if lang == "zh-cn" else lang

        detected_lang = None

        # Phụ đề CHÍNH CHỦ tải kèm lúc download (sidecar cạnh video) -> dùng thẳng làm
        # transcript, BỎ QUA Whisper. Chỉ áp cho chế độ whisper mặc định (OCR/hybrid là
        # người dùng CHỦ ĐÍCH chọn nguồn khác). Đọc xong XOÁ sidecar để lần phân tích
        # lại chạy STT thật (đè) đúng như người dùng mong đợi.
        author_subs = self._load_author_subs(video_path) if sub_mode == "whisper" else None
        if author_subs:
            logger.info(
                L(f"Review: dùng phụ đề chính chủ ({author_subs.get('lang') or '?'}) "
                f"làm transcript ({len(author_subs['segments'])} câu), bỏ qua Whisper.", f"Review: using the author's own subtitles ({author_subs.get('lang') or '?'}) as the transcript ({len(author_subs['segments'])} sentences), skipping Whisper.")
            )
            segments = author_subs["segments"]
            for i, s in enumerate(segments):
                s["index"] = i
            detected_lang = author_subs.get("lang") or None
        elif sub_mode == "ocr":
            # === CHỈ OCR (đọc sub cứng) — không chạy Whisper ===
            from services.ocr_service import get_ocr_service
            logger.info(L("Review: nguồn phụ đề = CHỈ OCR (đọc sub cứng trên hình).", "Review: subtitle source = OCR ONLY (reading the hard-coded subtitles on the picture)."))
            segments = get_ocr_service().extract_subtitles(
                video_path, region=region, lang=ocr_lang, check_cancel=_check_cancel,
            )
        elif sub_mode == "hybrid":
            # === Whisper + OCR vá lỗ (chỉ OCR chỗ Whisper bỏ sót) ===
            from services.ocr_service import (
                get_ocr_service, compute_whisper_gaps, overlaps_any,
            )
            logger.info(L("Review: nguồn phụ đề = Whisper + OCR vá lỗ.", "Review: subtitle source = Whisper + OCR gap filling."))
            audio_path = os.path.join(self.projects_root, project_id, "source_audio.wav")
            if not VideoService.extract_audio(video_path, audio_path):
                raise RuntimeError(f"Khong tach duoc audio tu video: {video_path}")
            logger.info(L("Review: [1/2] Whisper nghe giọng...", "Review: [1/2] Whisper is listening to the voice..."))
            wh_segments, detected_lang = self.stt_service.transcribe(
                audio_path, language=_normalize_lang(meta.get("source_language")),
                check_cancel=_check_cancel,
            )
            dur = float(meta.get("video_duration") or 0.0)
            gaps = compute_whisper_gaps(wh_segments, dur)
            # gaps RỖNG = Whisper đã phủ kín toàn video -> KHÔNG cần OCR (tránh quét cả
            # video vô ích). Whisper trống thật thì compute_whisper_gaps trả [(0,dur)]
            # nên vẫn OCR cả video.
            if not gaps:
                logger.info(L("Review: [2/2] Whisper đã phủ kín — BỎ QUA OCR (không có chỗ trống).", "Review: [2/2] Whisper already covers everything — SKIPPING OCR (no gaps)."))
                ocr_segs = []
            else:
                logger.info(L(f"Review: [2/2] OCR vá {len(gaps)} khoảng Whisper bỏ sót...", f"Review: [2/2] OCR is filling {len(gaps)} gaps Whisper missed..."))
                ocr_segs = get_ocr_service().extract_subtitles(
                    video_path, region=region, lang=ocr_lang,
                    check_cancel=_check_cancel, windows=gaps,
                )
            gap_ocr = [o for o in ocr_segs if not overlaps_any(o, wh_segments)]
            #  Cờ ``ocr``: bước gộp mẩu ngắn không trộn câu OCR vá lỗ vào câu Whisper.
            for o in gap_ocr:
                o["ocr"] = True
            segments = sorted(
                wh_segments + gap_ocr, key=lambda s: s["start"]
            )
            for i, s in enumerate(segments):
                s["index"] = i
            logger.info(
                L(f"Review: Whisper {len(wh_segments)} câu + OCR vá {len(gap_ocr)} câu.", f"Review: Whisper {len(wh_segments)} sentences + OCR gap fill {len(gap_ocr)} sentences.")
            )
        else:
            # === Whisper thuần (mặc định) ===
            audio_path = os.path.join(self.projects_root, project_id, "source_audio.wav")
            if not VideoService.extract_audio(video_path, audio_path):
                raise RuntimeError(f"Khong tach duoc audio tu video: {video_path}")
            if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
                raise RuntimeError(f"File audio STT khong hop le: {audio_path}")
            segments, detected_lang = self.stt_service.transcribe(
                audio_path, language=_normalize_lang(meta.get("source_language")),
                check_cancel=_check_cancel,
            )

        logger.info(L(f"=== NHẬN DẠNG LỜI THOẠI HOÀN TẤT: {len(segments)} câu ===", f"=== SPEECH RECOGNITION COMPLETE: {len(segments)} sentences ==="))

        #  Gộp mẩu 1–3 chữ / câu nói quá nhanh vào câu kề bên (mọi nguồn: sub chính chủ,
        #  Whisper, OCR, Whisper+OCR) — dịch ra tiếng Việt mới đọc lọt khung. Xem
        #  services/gop_manh_ngan.py.
        from services.gop_manh_ngan import gop_manh_ngan
        segments = gop_manh_ngan(segments, L("(lấy sub gốc)", "(from original subtitles)"))

        #  Tách đoạn dài nhiều câu theo dấu câu + độ dài (services/tach_cau.py). CHỈ Whisper /
        #  Whisper + OCR vá lỗ (tách cả câu vá, mảnh giữ cờ ``ocr``): sub chính chủ / OCR thuần
        #  vốn đã theo từng dòng hiện trên hình.
        #  Ô "Tách câu gốc" là cài đặt CỦA DỰ ÁN (metadata, mặc định bật) — lô ghi xuống trước.
        if not author_subs and sub_mode != "ocr":
            from services.tach_cau import bat_goc, tach_cau_goc
            segments = tach_cau_goc(segments, L("(lấy sub gốc)", "(from original subtitles)"), bat=bat_goc(meta))

        # Update detected language in metadata
        if detected_lang and (not meta.get("source_language") or meta.get("source_language") == "auto"):
            meta["source_language"] = detected_lang
            logger.info(L(f"Review: đã tự động nhận diện ngôn ngữ gốc: {detected_lang}", f"Review: automatically detected the source language: {detected_lang}"))

        # Compact JSON — indent=2 trên OneDrive rất chậm với hàng trăm segment.
        stt_out = os.path.join(self.projects_root, project_id, "raw_stt.json")
        with open(stt_out, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, separators=(",", ":"))
        logger.info(L(f"Review: đã lưu {len(segments)} câu nhận dạng", f"Review: saved {len(segments)} recognized sentences"))

        meta["state"]["stt_complete"] = True
        meta["state"].pop("analysis_error", None)
        self._save_metadata(project_id, meta)
        return True

    def _load_author_subs(self, video_path, consume=True, lang=""):
        """Đọc file sidecar ``<video>.subforge_sub.json`` (phụ đề chính chủ ghi lúc
        download). Trả ``{"lang", "segments":[{start,end,text}]}`` hoặc None.

        ``consume=True`` (chế độ CŨ): xoá sidecar sau khi đọc, để lần phân tích lại chạy
        Whisper thật — người dùng bấm "Lấy lại phụ đề" là muốn đè kết quả.

        ``consume=False`` (chế độ CHI TIẾT): GIỮ sidecar lại. Ở đó phụ đề chính chủ được
        đổ vào timeline ngay khi mở dự án, và có nút "Dùng lại sub gốc của video" bấm
        được nhiều lần — xoá đi là bấm lần hai không còn gì để lấy, mà người dùng đã
        thấy tận mắt là video CÓ sub gốc nên chỉ thấy nút hỏng.

        ``lang`` — chọn ĐÚNG thứ tiếng đó trong số các thứ tiếng tải về (khoá ``all``
        của sidecar); không truyền / không có thì dùng thứ tiếng mặc định như trước.
        Trả thêm ``langs`` = danh sách thứ tiếng đang có để giao diện cho chọn."""
        path = f"{video_path}.subforge_sub.json"
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            # ``all`` chỉ có ở sidecar ghi từ bản 2.10.1 trở đi; sidecar cũ vẫn đọc được
            # bình thường (chỉ có đúng một thứ tiếng).
            by_lang = data.get("all") if isinstance(data.get("all"), dict) else {}
            segs = data.get("segments")
            cur = data.get("lang") or ""
            want = (lang or "").strip()
            if want and by_lang:
                # Khớp đúng mã trước, rồi khớp phần gốc ("zh" khớp "zh-Hans").
                pick = by_lang.get(want)
                if pick is None:
                    base = want.split("-")[0].lower()
                    for code, val in by_lang.items():
                        if code.split("-")[0].lower() == base:
                            pick, want = val, code
                            break
                if pick:
                    segs, cur = pick, want
            if not segs:
                return None
            return {
                "lang": cur,
                "segments": segs,
                "langs": sorted(by_lang) if by_lang else ([cur] if cur else []),
            }
        except (OSError, ValueError) as e:
            logger.warning(L(f"Review: không đọc được phụ đề tác giả ({path}): {e}", f"Review: could not read the author's subtitles ({path}): {e}"))
            return None
        finally:
            if consume:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def load_a_chunks(self, project_id):
        """Đọc a_chunks.json nếu đã có — không regenerate."""
        path = os.path.join(self.projects_root, project_id, "a_chunks.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            chunks = json.load(f)
        return chunks if isinstance(chunks, list) else None

    def generate_a_chunks(self, project_id):
        """Map 1 STT segment → 1 A-chunk (giống lồng tiếng).

        Không còn semantic embedding / time-merge local — AI (prompt B) sẽ
        ghép các A nguyên bản qua source_a_ids.
        """
        meta = self.get_metadata(project_id)
        stt_path = os.path.join(self.projects_root, project_id, "raw_stt.json")
        if not os.path.exists(stt_path):
            return False

        logger.info(L("Review: đang chuẩn bị các câu để phân tích...", "Review: preparing the sentences for analysis..."))
        with open(stt_path, "r", encoding="utf-8") as f:
            segments = json.load(f)

        if not segments:
            return False

        a_chunks = [
            self._create_chunk_from_segments(i + 1, [s])
            for i, s in enumerate(segments)
        ]

        chunks_out = os.path.join(self.projects_root, project_id, "a_chunks.json")
        with open(chunks_out, "w", encoding="utf-8") as f:
            json.dump(a_chunks, f, ensure_ascii=False, separators=(",", ":"))

        if meta:
            meta["state"]["a_chunks_generated"] = True
            meta["state"]["analysis_started"] = False
            meta["state"].pop("analysis_error", None)
            self._save_metadata(project_id, meta)

        logger.info(
            L(f"=== ĐÃ CHUẨN BỊ {len(a_chunks)} câu để phân tích ===", f"=== PREPARED {len(a_chunks)} SENTENCES FOR ANALYSIS ===")
        )

        # Unload Whisper nền — tránh block analysis nếu CUDA sync chậm.
        def _release_bg():
            try:
                self.stt_service.release_gpu()
            except Exception as e:
                logger.warning(L(f"Review: release_gpu sau A-chunks thất bại (bỏ qua): {e}", f"Review: release_gpu after A-chunks failed (ignored): {e}"))

        threading.Thread(target=_release_bg, daemon=True).start()

        return a_chunks

    def _create_chunk_from_segments(self, chunk_id, segments):
        return {
            "a_id": chunk_id,
            "segment_ids": [s["index"] for s in segments],
            "start": segments[0]["start"],
            "end": segments[-1]["end"],
            "text": " ".join([s["text"] for s in segments])
        }

    def import_b_chunks(self, project_id, b_chunks, auto_fix=False):
        """Lưu kịch bản B.

        auto_fix=False (mặc định — NHẬP TAY): b_id trùng -> raise ValueError để
          người dùng TỰ sửa lại (giữ đúng nội dung từng câu do người dùng soạn).
        auto_fix=True (đường TỰ ĐỘNG — DeepSeek): sửa CỤC BỘ b_id trùng/thiếu bằng
          cách đánh số lại tuần tự, tránh bắt DeepSeek sinh lại toàn bộ chỉ vì lỗi
          số/ký tự nhỏ.
        """
        meta = self.get_metadata(project_id)
        if not meta: return False

        # CHẶN dự án ở chế độ Review NÂNG CAO đi qua đây.
        #
        # Hàm này ghi đè b_chunks.json bằng danh sách KHÔNG CÓ ``uid``. Ở chế độ nâng cao,
        # audio bám theo uid (``output/{uid}/audio_tts.wav``) và vị trí trên timeline nằm
        # trong ``start_override``/``end_override`` — mất cả hai thì lần mở sau ``ensure_uids``
        # gán lại ``B{b_id}`` theo số thứ tự, nên câu thứ 7 của kịch bản MỚI trỏ đúng thư
        # mục audio của kịch bản CŨ: clip hiện "đã có giọng", nút sinh giọng bỏ qua, và
        # video xuất ra đọc nội dung cũ mà không có cảnh báo nào. Công đã dựng trên
        # timeline cũng mất sạch.
        #
        # Đường vào còn mở vì màn Review CŨ vẫn mở được dự án nâng cao (nút "AI tạo kịch
        # bản" / "Nhập kịch bản" ở đó gọi vào đây). Chặn tại lớp service để bịt CẢ HAI
        # đường một lần, không phải sửa route. ValueError -> route trả 400 kèm nguyên văn
        # thông báo, còn job AI thì ghi vào state.analysis_error cho FE hiện lên.
        try:
            from services import review_timeline as TL

            if TL.is_advanced(meta):
                raise ValueError(
                    L("Dự án này đang ở chế độ Review nâng cao — nhập kịch bản ở màn cũ sẽ "
                    "xoá hết vị trí trên timeline và làm giọng đã sinh gán sai câu. "
                    "Hãy mở dự án bằng trình dựng (Review nâng cao) rồi nhập kịch bản ở đó.", "This project is in Advanced Review mode — importing a script on the old screen would erase all positions on the timeline and attach generated voices to the wrong sentences. Open the project in the editor (Advanced Review) and import the script there.")
                )
        except ImportError:
            pass

        # Normalize b_text property for all chunks
        for chunk in b_chunks:
            txt = chunk.get("b_text") or chunk.get("text") or chunk.get("target_text") or chunk.get("content") or ""
            chunk["b_text"] = str(txt).strip()

        # b_id là danh tính đặt tên clip (B{b_id}/audio_tts.wav) + dòng SRT. Trùng
        # -> 2 câu ghi đè cùng file + SRT chồng khi render.
        if auto_fix:
            b_chunks, n_fixed = renumber_b_ids_sequential(b_chunks)
            if n_fixed:
                logger.warning(
                    L(f"Import (tự động): đã đánh số lại b_id cho {n_fixed} câu để "
                    f"liền mạch/không trùng (không cần DeepSeek sinh lại).", f"Import (automatic): renumbered b_id for {n_fixed} sentences to be continuous/unique (no need for DeepSeek to regenerate).")
                )
        else:
            dups = find_duplicate_b_ids(b_chunks)
            if dups:
                raise ValueError(
                    L("Kịch bản có b_id bị trùng: ", "The script has duplicate b_id values:")
                    + ", ".join(str(d) for d in dups)
                    + L(". Mỗi câu phải có b_id riêng — hãy sửa lại rồi nhập lại.", ". Every sentence must have its own b_id — please fix it and import again.")
                )
            gaps = find_b_id_gaps(b_chunks)
            if gaps:
                # Gộp dải liên tiếp cho gọn: 50,51,52 -> "50–52".
                parts, i = [], 0
                while i < len(gaps):
                    j = i
                    while j + 1 < len(gaps) and gaps[j + 1] == gaps[j] + 1:
                        j += 1
                    parts.append(str(gaps[i]) if i == j else f"{gaps[i]}–{gaps[j]}")
                    i = j + 1
                raise ValueError(
                    L("Kịch bản có b_id KHÔNG liền mạch — thiếu: ", "The script's b_id values are NOT continuous — missing:")
                    + ", ".join(parts)
                    + L(". b_id phải liên tục 1,2,3,…,N (thiếu số = thiếu câu, đoạn đó "
                    "sẽ trống tiếng). Hãy bổ sung câu thiếu rồi nhập lại.", ". b_id must run continuously 1,2,3,…,N (a missing number = a missing sentence, that part will be silent). Please add the missing sentences and import again.")
                )

        path = os.path.join(self.projects_root, project_id, "b_chunks.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(b_chunks, f, indent=2, ensure_ascii=False)

        meta["state"]["b_imported"] = True
        # Importing a new B-script invalidates any previous render, so the UI
        # returns to the edit/render step instead of showing the stale result.
        meta["state"]["render_started"] = False
        meta["state"]["render_complete"] = False
        self._save_metadata(project_id, meta)
        return True

    def list_projects_recent(self, limit: int):
        """``limit`` dự án MỚI NHẤT + TỔNG số dự án — cho Trang chủ.

        ``list_projects`` chạy ``get_metadata`` (nặng: tính dung lượng thư mục, tự vá
        trạng thái) cho TỪNG dự án; máy có vài trăm dự án là Trang chủ lag mỗi lần mở.
        Ở đây chỉ đọc NHẸ ``created_at`` trong metadata.json để sắp thứ tự (đúng thứ tự
        của ``list_projects``), rồi mới gọi ``get_metadata`` cho ``limit`` dự án đầu."""
        import json as _json

        if not os.path.exists(self.projects_root):
            return [], 0
        nhe = []
        for pid in os.listdir(self.projects_root):
            mp = os.path.join(self.projects_root, pid, "metadata.json")
            if not os.path.isfile(mp):
                continue
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    tao = str((_json.load(f) or {}).get("created_at", ""))
            except (OSError, ValueError, AttributeError):
                tao = ""
            nhe.append((tao, pid))
        nhe.sort(key=lambda x: x[0], reverse=True)
        ra = []
        for _, pid in nhe:
            if len(ra) >= limit:
                break
            meta = self.get_metadata(pid)
            if meta:
                ra.append(meta)
        return ra, len(nhe)

    def list_projects(self):
        projects = []
        if not os.path.exists(self.projects_root):
            return []
        
        for pid in os.listdir(self.projects_root):
            meta = self.get_metadata(pid)
            if meta:
                projects.append(meta)
        
        # Sort by created_at descending if possible
        try:
            projects.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        except:
            pass
        return projects

    def _make_rm_error_handler(self, failures):
        """Handler cho shutil.rmtree: gỡ read-only rồi xoá lại; path nào vẫn không xoá
        được thì GHI LẠI vào `failures` (trước đây nuốt lỗi im lặng khiến rmtree kết
        thúc "êm" và delete_project báo thành công dù dữ liệu còn nguyên trên đĩa)."""
        import stat

        def _on_rm_error(func, path, exc_info):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception as e:
                failures.append((path, e))

        return _on_rm_error

    def request_log_stream_stop(self, project_id):
        """Báo cho SSE /logs của dự án này tự đóng file + kết thúc.

        Trên Windows, handle đọc đang mở trên synthesis.log CHẶN việc xoá thư mục.
        """
        _LOG_STREAM_STOP.add(str(project_id))

    def is_log_stream_stop_requested(self, project_id):
        return str(project_id) in _LOG_STREAM_STOP

    def clear_log_stream_stop(self, project_id):
        _LOG_STREAM_STOP.discard(str(project_id))

    def _rmtree_verified(self, path):
        """rmtree + KIỂM CHỨNG bằng os.path.exists. Trả về (ok, mô tả lỗi)."""
        failures = []
        try:
            shutil.rmtree(path, onexc=self._make_rm_error_handler(failures))
        except Exception as e:
            failures.append((path, e))
        if os.path.exists(path):
            if not failures:
                failures.append((path, "thư mục vẫn tồn tại sau khi xoá"))
            return False, "; ".join(f"{p}: {e}" for p, e in failures[:5])
        return True, ""

    def delete_project(self, project_id):
        path = os.path.join(self.projects_root, project_id)
        if not os.path.exists(path):
            return False

        print(f"[REVIEW-SERVICE] Attempting to delete: {project_id}")
        # Nhả handle log đang mở (SSE) rồi mới xoá. Chờ một nhịp > chu kỳ đọc 0.5s
        # của log_generator để nó kịp thoát và đóng file.
        self.request_log_stream_stop(project_id)
        time.sleep(0.8)
        try:
            last_err = ""
            for attempt in range(4):
                if attempt:
                    time.sleep(0.5)
                # Xoá NỘI DUNG trước, giữ metadata.json đến cuối cùng: nếu xoá hụt,
                # dự án vẫn hiện trên UI để xoá lại được (không thành thư mục "ma").
                last_err = ""
                for name in os.listdir(path):
                    if name == "metadata.json":
                        continue
                    child = os.path.join(path, name)
                    if os.path.isdir(child) and not os.path.islink(child):
                        ok, err = self._rmtree_verified(child)
                    else:
                        ok, err = True, ""
                        try:
                            os.chmod(child, stat.S_IWRITE)
                        except Exception:
                            pass
                        try:
                            os.remove(child)
                        except Exception as e:
                            ok, err = False, f"{child}: {e}"
                    if not ok:
                        last_err = err
                if last_err:
                    print(f"[REVIEW-SERVICE] Delete failed (attempt {attempt+1}), retrying... {last_err}")
                    continue
                # Nội dung sạch → bỏ metadata rồi xoá thư mục rỗng.
                ok, err = self._rmtree_verified(path)
                if ok:
                    print(f"[REVIEW-SERVICE] Successfully deleted: {project_id}")
                    return True
                last_err = err
                print(f"[REVIEW-SERVICE] Delete failed (attempt {attempt+1}), retrying... {err}")
            print(
                f"[REVIEW-SERVICE] CRITICAL: Failed to delete project {project_id} "
                f"after retries: {last_err}"
            )
            return False
        finally:
            self.clear_log_stream_stop(project_id)

    def cleanup_orphan_projects(self):
        """Dọn thư mục 'ma' — còn trên đĩa nhưng mất metadata.json nên list_projects
        không thấy (di sản của các lần xoá hụt trước bản vá). Chạy lúc khởi động,
        khi chắc chắn không có dự án nào đang được tạo dở."""
        root = self.projects_root
        if not os.path.exists(root):
            return 0
        removed = 0
        for pid in os.listdir(root):
            d = os.path.join(root, pid)
            #  Thư mục RIÊNG của app, không phải dự án nên không có metadata.json: "downloads" và
            #  "mau" (video mẫu của dự án mẫu — xoá nó là mọi dự án mẫu báo "không thấy tệp").
            if not os.path.isdir(d) or pid in THU_MUC_RIENG:
                continue
            if os.path.exists(os.path.join(d, "metadata.json")):
                continue
            ok, err = self._rmtree_verified(d)
            if ok:
                removed += 1
                logger.info(f"[REVIEW-SERVICE] Da don thu muc du an mo coi: {pid}")
            else:
                logger.warning(
                    f"[REVIEW-SERVICE] Khong don duoc thu muc mo coi {pid}: {err}"
                )
        return removed

    def is_project_busy(self, project_id):
        """Dự án đang render dở: đã bấm render và chưa xong, chưa lỗi.

        Xoá thư mục lúc này vừa hỏng dữ liệu (FFmpeg còn ghi vào đó) vừa gần như
        chắc chắn fail vì Windows khoá file. Trạng thái này khớp với cờ mà
        mark_render_started đặt và _restore_render_idle_state gỡ.
        """
        meta = self.get_metadata(project_id)
        if not meta:
            return False
        state = meta.get("state") or {}
        if not state.get("render_started"):
            return False
        return not state.get("render_complete") and not state.get("render_error")

    def list_busy_projects(self):
        """Tên các dự án đang render — để chặn purge và báo rõ cho người dùng."""
        if not os.path.exists(self.projects_root):
            return []
        busy = []
        for pid in os.listdir(self.projects_root):
            if not os.path.isdir(os.path.join(self.projects_root, pid)):
                continue
            if pid == "downloads":
                continue
            if self.is_project_busy(pid):
                meta = self.get_metadata(pid) or {}
                busy.append(meta.get("name") or pid)
        return busy

    def purge_all(self, progress=None):
        """Xoá mọi dự án Chi tiết. Trả (deleted, failed_names).

        progress(done, total, name) được gọi sau mỗi dự án để UI hiện tiến độ —
        purge có thể mất hàng phút với thư mục vài GB.
        """
        if not os.path.exists(self.projects_root):
            return 0, []

        print(f"[REVIEW-SERVICE] Purging all projects...")
        # Xoá cả "downloads" (video nguồn đã tải) như trước, để dung lượng hiển
        # thị trên UI — vốn tính cả thư mục đó — thực sự về 0 sau khi xoá.
        pids = [
            p
            for p in os.listdir(self.projects_root)
            if os.path.isdir(os.path.join(self.projects_root, p))
        ]
        total = len(pids)
        deleted = 0
        failed = []
        # Xoá lần lượt để handle bị khoá của dự án này không kéo cả mẻ chết theo.
        for i, pid in enumerate(pids, 1):
            meta = self.get_metadata(pid)
            name = (meta or {}).get("name") or pid
            ok = self.delete_project(pid)
            if ok:
                # Chỉ đếm dự án thật (có metadata) để toast không nói "2 dự án"
                # khi thực chất là 1 dự án + thư mục downloads.
                if meta:
                    deleted += 1
            else:
                failed.append(name)
            if progress:
                try:
                    progress(i, total, name)
                except Exception:
                    pass

        # Ensure root remains
        os.makedirs(self.projects_root, exist_ok=True)
        # Báo đúng sự thật: trước đây luôn trả True kể cả khi còn dữ liệu chưa xoá.
        return deleted, failed

    def mark_render_started(self, project_id):
        meta = self.get_metadata(project_id)
        if meta:
            meta["state"]["render_started"] = True
            # Starting a (re-)render invalidates the previous completed output
            # so the UI shows live progress instead of the old result.
            meta["state"]["render_complete"] = False
            meta["state"].pop("render_error", None)
            # Tăng mỗi lần render — FE dùng làm key remount EventSource (tránh
            # Disconnected khi sửa & chạy lại vì render_started vẫn True).
            prev_run = int(meta["state"].get("render_run_id") or 0)
            meta["state"]["render_run_id"] = prev_run + 1
            self._save_metadata(project_id, meta)
            # Xóa log cũ để SSE không phát lại "SYNTHESIS COMPLETED" của lần trước
            # (gây đóng stream / progress 100% giả khi render lại).
            log_file = os.path.join(self.projects_root, project_id, "synthesis.log")
            try:
                with open(log_file, "w", encoding="utf-8"):
                    pass
            except OSError:
                pass
            self.clear_render_cancel(project_id)
            return True
        return False

    def _cancel_flag_path(self, project_id):
        return os.path.join(self.projects_root, project_id, ".cancel_render")

    def clear_render_cancel(self, project_id):
        path = self._cancel_flag_path(project_id)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def is_render_cancelled(self, project_id):
        return os.path.exists(self._cancel_flag_path(project_id))

    def is_render_cancelled_since(self, project_id, moc: float) -> bool:
        """Có lệnh Hủy đặt SAU mốc ``moc`` (time.time()) không.

        Cờ hủy là một tệp dùng chung cho cả dự án. Đọc theo mốc thời gian của từng lượt
        thì (1) cờ cũ còn sót từ lần trước không làm chết lượt mới ngay khi bắt đầu,
        và (2) lượt mới KHÔNG phải xoá cờ lúc khởi động — tức không xoá nhầm cờ mà
        người dùng vừa đặt cho một lượt khác đang chạy dở (bắt được khi test: bấm Xuất
        lần hai trong lúc lần một chưa dừng hẳn, lần một chạy tiếp tới xong).
        """
        try:
            return os.path.getmtime(self._cancel_flag_path(project_id)) >= float(moc) - 1.0
        except OSError:
            return False

    def _restore_render_idle_state(self, meta, project_id):
        """Đưa UI về trạng thái không treo: nếu đã có final_review thì giữ output cũ."""
        meta["state"]["render_started"] = False
        output_path = os.path.join(self.projects_root, project_id, "final_review.mp4")
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            meta["state"]["render_complete"] = True
        else:
            meta["state"]["render_complete"] = False
        return meta

    def cancel_render(self, project_id):
        """Người dùng hủy: rollback status ngay + báo job nền dừng sớm."""
        meta = self.get_metadata(project_id)
        if not meta:
            return False
        try:
            os.makedirs(os.path.join(self.projects_root, project_id), exist_ok=True)
            with open(self._cancel_flag_path(project_id), "w", encoding="utf-8") as f:
                f.write("1")
        except OSError as e:
            logger.warning(L(f"Không tạo được cancel flag: {e}", f"Could not create the cancel flag: {e}"))
        meta = self._restore_render_idle_state(meta, project_id)
        meta["state"]["render_error"] = L("Đã hủy render theo yêu cầu người dùng.", "Render cancelled at the user's request.")
        self._save_metadata(project_id, meta)
        return True

    def mark_render_failed(self, project_id, error_message: str):
        """Render lỗi / bị hủy giữa chừng — không để render_started=True treo UI."""
        meta = self.get_metadata(project_id)
        if not meta:
            return False
        meta = self._restore_render_idle_state(meta, project_id)
        meta["state"]["render_error"] = (error_message or L("Render thất bại", "Render failed"))[:500]
        self._save_metadata(project_id, meta)
        self.clear_render_cancel(project_id)
        return True

    # ------------------------------------------------------------- cancel analysis
    # Cơ chế giống cancel_render nhưng dùng flag riêng cho bước PHÂN TÍCH (STT +
    # A-chunks). STT là bước lâu nhất với video dài; transcribe() nhận check_cancel
    # để dừng SỚM giữa chừng thay vì phải chờ chạy xong cả file.
    def _analysis_cancel_flag_path(self, project_id):
        return os.path.join(self.projects_root, project_id, ".cancel_analysis")

    def clear_analysis_cancel(self, project_id):
        path = self._analysis_cancel_flag_path(project_id)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def is_analysis_cancelled(self, project_id):
        return os.path.exists(self._analysis_cancel_flag_path(project_id))

    def cancel_analysis(self, project_id):
        """Người dùng hủy phân tích: rollback status ngay + báo job nền dừng sớm."""
        meta = self.get_metadata(project_id)
        if not meta:
            return False
        try:
            os.makedirs(os.path.join(self.projects_root, project_id), exist_ok=True)
            with open(self._analysis_cancel_flag_path(project_id), "w", encoding="utf-8") as f:
                f.write("1")
        except OSError as e:
            logger.warning(L(f"Không tạo được cancel flag (analysis): {e}", f"Could not create the cancel flag (analysis): {e}"))
        meta["state"]["analysis_started"] = False
        meta["state"]["analysis_error"] = L("Đã hủy phân tích theo yêu cầu người dùng.", "Analysis cancelled at the user's request.")
        self._save_metadata(project_id, meta)
        return True

    def mark_analysis_failed(self, project_id, error_message: str):
        """Phân tích lỗi / bị hủy — không để analysis_started=True treo UI."""
        meta = self.get_metadata(project_id)
        if not meta:
            return False
        meta["state"]["analysis_started"] = False
        meta["state"]["analysis_error"] = (error_message or L("Phân tích thất bại", "Analysis failed"))[:500]
        self._save_metadata(project_id, meta)
        self.clear_analysis_cancel(project_id)
        return True

    def update_settings(
        self,
        project_id,
        bg_volume=None,
        voice_bg_volume=None,
        separate_audio=None,
        vocals_volume=None,
        voice_vocals_volume=None,
        target_voice=None,
        default_rate=None,
        smart_srt_shift=None,
        speech_style=None,
        brand_name=None,
    ):
        """
        Cập nhật mức âm lượng tiếng gốc cho dự án review và lưu vào metadata:
          - target_voice        : giọng đọc (edge id / vieneu: / vieneu-clone:)
          - bg_volume           : âm gốc KHI KHÔNG có thoại AI (nhạc nền)
          - voice_bg_volume     : âm gốc KHI có thoại AI (hạ nền dưới giọng đọc)
          - separate_audio      : bật tách giọng gốc / nhạc nền (Demucs)
          - vocals_volume       : giọng gốc KHI KHÔNG có thoại AI (chỉ khi tách)
          - voice_vocals_volume : giọng gốc KHI có thoại AI (chỉ khi tách)
        Giá trị volume được kẹp trong [0.0, 1.0]. Trả về dict các mức hiện tại,
        hoặc None nếu không tìm thấy dự án. Khi tách BẬT, bg_volume/voice_bg_volume
        chỉ áp lên kênh nhạc nền (no_vocals).
        """
        meta = self.get_metadata(project_id)
        if not meta:
            return None

        audio_changed = False

        def _clamp(v):
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return None

        if bg_volume is not None:
            cv = _clamp(bg_volume)
            if cv is not None and cv != meta.get("bg_volume", 1.0):
                meta["bg_volume"] = cv
                audio_changed = True
        if voice_bg_volume is not None:
            cv = _clamp(voice_bg_volume)
            if cv is not None and cv != meta.get("voice_bg_volume", 1.0):
                meta["voice_bg_volume"] = cv
                audio_changed = True
        if separate_audio is not None and bool(separate_audio) != bool(meta.get("separate_audio", True)):
            meta["separate_audio"] = bool(separate_audio)
            audio_changed = True
        if vocals_volume is not None:
            cv = _clamp(vocals_volume)
            if cv is not None and cv != meta.get("vocals_volume", 0.0):
                meta["vocals_volume"] = cv
                audio_changed = True
        if voice_vocals_volume is not None:
            cv = _clamp(voice_vocals_volume)
            if cv is not None and cv != meta.get("voice_vocals_volume", 0.0):
                meta["voice_vocals_volume"] = cv
                audio_changed = True
        if target_voice and str(target_voice) != str(meta.get("target_voice", "")):
            meta["target_voice"] = str(target_voice)
            audio_changed = True
        # Tốc độ đọc mặc định: đổi -> fingerprint TTS khác -> voice sinh lại khớp.
        if default_rate is not None and str(default_rate) != str(meta.get("default_rate", "")):
            meta["default_rate"] = str(default_rate)
            audio_changed = True
        # Xê dịch SRT thông minh: đổi -> giọng sinh khác (fingerprint đã gồm cờ này) +
        # thời điểm SRT/voice khác -> phải render lại.
        if smart_srt_shift is not None and bool(smart_srt_shift) != bool(meta.get("smart_srt_shift", True)):
            meta["smart_srt_shift"] = bool(smart_srt_shift)
            audio_changed = True
        if speech_style is not None and str(speech_style) != str(meta.get("speech_style", "")):
            meta["speech_style"] = str(speech_style)
            audio_changed = True
        if brand_name is not None and str(brand_name) != str(meta.get("brand_name", "")):
            meta["brand_name"] = str(brand_name)
            audio_changed = True

        if audio_changed:
            # Không xoá cả render_cache: fingerprint TTS/mix tự invalidate đúng lớp.
            # Đổi volume → tái dùng TTS; đổi giọng → chỉ miss lớp TTS.
            meta["state"]["render_complete"] = False

        self._save_metadata(project_id, meta)
        return {
            "bg_volume": meta.get("bg_volume", 0.0),
            "voice_bg_volume": meta.get("voice_bg_volume", 0.0),
            "separate_audio": bool(meta.get("separate_audio", False)),
            "vocals_volume": meta.get("vocals_volume", 0.0),
            "voice_vocals_volume": meta.get("voice_vocals_volume", 0.0),
            "target_voice": meta.get("target_voice", ""),
            "default_rate": meta.get("default_rate", "auto"),
            "smart_srt_shift": bool(meta.get("smart_srt_shift", True)),
            "speech_style": meta.get("speech_style", "auto"),
            # Doc ra ĐÚNG thu dang luu — khong bia ten kenh ho.
            "brand_name": meta.get("brand_name", ""),
        }

