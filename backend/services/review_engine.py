from utils.i18n import L
import os
import json
import logging
import subprocess
import asyncio
from services.tts_service import TTSService
from services.tts_text import lam_sach_cau_doc
from utils.file_utils import safe_replace, safe_remove
from services.review_render_cache import (
    compute_tts_fingerprint,
    load_render_cache,
    save_render_cache,
    try_reuse_clip,
    remember_clip,
    resolve_b_time,
)
import imageio_ffmpeg

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger("ReviewEngine")

# Số câu TTS chạy ĐỒNG THỜI ở đường Edge/VieNeu-CPU. Giữ vừa phải: đủ song song
# cho nhanh nhưng không dội hàng trăm request cùng lúc (Edge-TTS hay 503) và để log
# "Đang tạo câu X" chảy theo tiến độ thay vì in cả 100+ dòng một lúc.
TTS_MAX_CONCURRENCY = 8


class RenderCancelled(Exception):
    """Người dùng hủy render giữa chừng."""
    pass


class ReviewEngine:
    def __init__(self, projects_root=None):
        from services.config_service import get_config_service

        self.config_service = get_config_service()
        self._projects_root = projects_root
        self.tts_service = TTSService()

    @property
    def projects_root(self):
        if self._projects_root:
            return self._projects_root
        return self.config_service.get_review_root()

    def _get_voice(self, meta, b_data):
        """Map language + gender to a real voice ID."""
        # Neu du an chon 1 giong VieNeu cu the (preset/clone) -> uu tien tuyet doi,
        # bo qua heuristic gender (VieNeu la giong dinh danh, khong theo nam/nu edge).
        #  Cũng vậy với giọng Qwen3-TTS (định danh + đã mang sẵn ngôn ngữ trong mã giọng):
        #  để bảng nam/nữ bên dưới ghi đè là dự án tiếng Anh chọn Ryan ra giọng Edge.
        from services.tts_engines import ENGINE_EDGE, engine_of

        tv = meta.get("target_voice") or ""
        if engine_of(tv) != ENGINE_EDGE:
            return tv

        lang = (
            b_data.get("target_language") or meta.get("target_language") or "vi"
        ).lower()
        gender = (b_data.get("voice") or "").lower()

        # Mapping for common languages (Gender: Nam/Nữ)
        voice_map = {
            "vi": {"nam": "vi-VN-NamMinhNeural", "nữ": "vi-VN-HoaiMyNeural"},
            "en": {"nam": "en-US-GuyNeural", "nữ": "en-US-AriaNeural"},
            "zh": {"nam": "zh-CN-YunxiNeural", "nữ": "zh-CN-XiaoxiaoNeural"},
            "ja": {"nam": "ja-JP-KeitaNeural", "nữ": "ja-JP-NanamiNeural"},
            "ko": {"nam": "ko-KR-InJoonNeural", "nữ": "ko-KR-SunHiNeural"},
            "fr": {"nam": "fr-FR-HenriNeural", "nữ": "fr-FR-DeniseNeural"},
            "de": {"nam": "de-DE-ConradNeural", "nữ": "de-DE-KatjaNeural"},
            "es": {"nam": "es-ES-AlvaroNeural", "nữ": "es-ES-ElviraNeural"},
            "ru": {"nam": "ru-RU-DmitryNeural", "nữ": "ru-RU-SvetlanaNeural"},
            "pt": {"nam": "pt-BR-AntonioNeural", "nữ": "pt-BR-FranciscaNeural"},
            "it": {"nam": "it-IT-DiegoNeural", "nữ": "it-IT-ElsaNeural"},
        }

        # 1. Check if specific mapping exists
        if lang in voice_map and gender in voice_map[lang]:
            return voice_map[lang][gender]

        # 2. Fallback to project default voice ("or": key co the ton tai nhung RONG
        # -> .get(key, default) van tra "" lam TTS loi voice rong kho hieu)
        return meta.get("target_voice") or "vi-VN-NamMinhNeural"

    async def _run_tts_jobs(self, jobs, project_id=None, render_cache=None, project_dir=None):
        """Sinh TTS cho nhiều clip review.

        - VieNeu + CUDA: ``infer_batch`` giống lồng tiếng (nhiều câu/lần trên GPU),
          rồi ``fit_to_duration`` song song trên CPU.
        - Edge-TTS (và VieNeu CPU): ``asyncio.gather`` song song như lồng tiếng
          (giới hạn bởi semaphore trong TTSService) — không còn chạy lần lượt.
        jobs: {b_id, text, path, target_duration, voice, base_rate?}
        """
        from services.vieneu_service import VieneuService, is_vieneu_voice
        from utils.hardware_utils import get_best_device
        import concurrent.futures as cf
        from services.review_render_cache import remember_clip, save_render_cache

        if not jobs:
            return

        device, _ = get_best_device()
        by_voice: dict[str, list] = {}
        for j in jobs:
            by_voice.setdefault(j["voice"], []).append(j)

        # Tiến độ tính trên TỔNG tất cả jobs (mọi giọng) — trước đây % tính theo từng
        # nhóm voice nên nhiều giọng làm % nhảy về 0 chạy lại, FE ước lượng sai.
        grand_total = len(jobs)
        grand_done = {"n": 0}

        for voice, vjobs in by_voice.items():
            if project_id:
                self._raise_if_cancelled(project_id)

            use_batch = is_vieneu_voice(voice) and device == "cuda"
            if use_batch:
                logger.info(
                    L(f"Đang tạo giọng đọc cho {len(vjobs)} câu (bằng GPU)...", f"Generating voice for {len(vjobs)} sentences (on GPU)...")
                )
                gen_items = [(j["text"], j["path"]) for j in vjobs]

                # Fit tốc độ + báo UI TỪNG CÂU ngay khi encode MP3 xong (callback từ
                # encode-pool của synthesize_batch) — không đợi hết batch. Nhờ vậy
                # nhóm 8 câu đầu nghe thử được trong lúc GPU còn sinh nhóm sau.
                # _fit_all phía dưới chỉ QUÉT VÉT các câu callback bỏ sót (idempotent
                # nhờ fitted_ids + lock).
                import threading as _threading
                jobs_by_path = {
                    os.path.normcase(os.path.abspath(j["path"])): j for j in vjobs
                }
                fitted_ids: set = set()
                fitted_lock = _threading.Lock()

                def _fit_one(j):
                    with fitted_lock:
                        if j["b_id"] in fitted_ids:
                            return
                        fitted_ids.add(j["b_id"])
                    if project_id and os.path.exists(os.path.join(self.projects_root, project_id, ".cancel_render")):
                        return
                    p = j["path"]
                    if not (os.path.exists(p) and os.path.getsize(p) > 0):
                        # Chưa/không có file (encode lỗi) -> nhả lại để _fit_all thử vét.
                        with fitted_lock:
                            fitted_ids.discard(j["b_id"])
                        return
                    is_trimmed = VieneuService.fit_to_duration(
                        p, j["target_duration"], base_rate=j.get("base_rate") or "auto"
                    )
                    j["is_trimmed"] = is_trimmed

                    # Nhớ cache ngay cho câu đã xong; ghi đĩa có throttle (tránh
                    # full-write JSON mỗi câu trên OneDrive). Lần chốt cuối do
                    # render_overlay_project save không throttle.
                    if render_cache is not None and project_dir:
                        remember_clip(render_cache, j["b_id"], j["fp_tts"], layer="tts")
                        save_render_cache(project_dir, render_cache, throttle_s=2.0)

                    # Marker máy-đọc cho FE (qua SSE log stream): câu này đã có
                    # giọng HOÀN CHỈNH (đã fit tốc độ) -> UI bật nút nghe thử ngay
                    # trong lúc chờ các bước render còn lại. FE ẩn dòng này khỏi console.
                    logger.info(f"[VOICE_READY] B{j['b_id']}")

                def _on_clip_done(out_path):
                    j = jobs_by_path.get(os.path.normcase(os.path.abspath(out_path)))
                    if j is not None:
                        _fit_one(j)

                #  CỜ HUỶ THEO MÃ DỰ ÁN, không phụ thuộc ``project_dir``: trình dựng Chi tiết gọi hàm này KHÔNG truyền
                #  project_dir, nên synthesize_batch không thấy .cancel_render và sinh hết mọi câu trên GPU dù người dùng
                #  đã bấm Huỷ (người dùng báo 13-10-2026; máy không GPU đi nhánh song song bên dưới nên không bị).
                _cancel_path = (
                    os.path.join(self.projects_root, project_id, ".cancel_render") if project_id else None
                )
                await asyncio.to_thread(
                    VieneuService.synthesize_batch, gen_items, voice,
                    project_dir=project_dir, on_clip_done=_on_clip_done,
                    check_cancel=(lambda: os.path.exists(_cancel_path)) if _cancel_path else None,
                )

                if project_id:
                    self._raise_if_cancelled(project_id)

                def _fit_all(specs=vjobs):
                    workers = min(8, max(2, (os.cpu_count() or 4)))
                    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                        list(pool.map(_fit_one, specs))

                await asyncio.to_thread(_fit_all)

                if project_id:
                    self._raise_if_cancelled(project_id)

                ok_n = 0
                for j in vjobs:
                    p = j["path"]
                    if os.path.exists(p) and os.path.getsize(p) > 0:
                        ok_n += 1
                        logger.info(
                            L(f"  Đã tạo xong giọng đọc câu {j['b_id']} "
                            f"(dài {self._get_duration(p):.2f}s)", f"  Finished voice for sentence {j['b_id']} (length {self._get_duration(p):.2f}s)")
                        )
                    else:
                        logger.warning(
                            L(f"  Tạo giọng đọc câu {j['b_id']} THẤT BẠI (thiếu tệp âm thanh)", f"  Voice for sentence {j['b_id']} FAILED (audio file missing)")
                        )
                grand_done["n"] += len(vjobs)
                pct = int(grand_done["n"] / grand_total * 100) if grand_total else 100
                logger.info(
                    L(f"Đã tạo xong giọng đọc: {ok_n}/{len(vjobs)} câu. "
                      f"Tiến độ tạo giọng: {grand_done['n']}/{grand_total} đoạn ({pct}%)",
                      f"Finished voice generation: {ok_n}/{len(vjobs)} sentences. "
                      f"Voice generation progress: {grand_done['n']}/{grand_total} segments ({pct}%)")
                )
                continue

            # Edge-TTS / VieNeu CPU: chạy SONG SONG (giới hạn TTS_MAX_CONCURRENCY câu
            # cùng lúc để tránh dội Edge-TTS/503). KHÔNG log dòng "Đang tạo câu X" nữa:
            # vì chạy song song, câu bắt đầu/kết thúc không theo thứ tự -> log kiểu
            # "Xong câu 69" rồi "Đang tạo câu 70" gây hiểu nhầm là tuần tự. Giờ CHỈ in
            # 1 dòng TIẾN ĐỘ mỗi khi 1 câu xong: số N/tổng lên trước (thứ tự hoàn tất),
            # mã câu #b_id để tham chiếu (không phải thứ tự).
            total = len(vjobs)
            logger.info(
                L(f"Bắt đầu tạo giọng đọc cho {total} câu "
                f"(chạy song song, tối đa {TTS_MAX_CONCURRENCY} câu cùng lúc)…", f"Starting voice generation for {total} sentences (running in parallel, up to {TTS_MAX_CONCURRENCY} at a time)…")
            )
            gate = asyncio.Semaphore(TTS_MAX_CONCURRENCY)

            async def _one(j):
                if project_id:
                    self._raise_if_cancelled(project_id)
                async with gate:
                    if project_id:
                        self._raise_if_cancelled(project_id)
                    ok, is_trimmed = await self._tts_auto_fit_retry(
                        j["text"],
                        j["path"],
                        target_duration=j["target_duration"],
                        voice=j["voice"],
                        base_rate=j.get("base_rate") or "auto",
                        project_id=project_id,
                    )
                if project_id:
                    self._raise_if_cancelled(project_id)
                j["is_trimmed"] = is_trimmed
                grand_done["n"] += 1
                pct = int(grand_done["n"] / grand_total * 100) if grand_total else 100
                p = j["path"]
                if ok and os.path.exists(p) and os.path.getsize(p) > 0:
                    # Nhớ cache ngay; ghi đĩa có throttle (lần chốt cuối không throttle).
                    if render_cache is not None and project_dir:
                        remember_clip(render_cache, j["b_id"], j["fp_tts"], layer="tts")
                        save_render_cache(project_dir, render_cache, throttle_s=2.0)

                    # Marker cho FE bật nút nghe thử ngay (xem chú thích nhánh GPU).
                    logger.info(f"[VOICE_READY] B{j['b_id']}")
                    logger.info(
                        L(f"Đã tạo {grand_done['n']}/{grand_total} câu ({pct}%) "
                          f"— vừa xong câu #{j['b_id']} ({self._get_duration(p):.1f}s)",
                          f"Generated {grand_done['n']}/{grand_total} sentences ({pct}%) "
                          f"— just finished sentence #{j['b_id']} ({self._get_duration(p):.1f}s)")
                    )
                else:
                    logger.warning(
                        L(f"Đã tạo {grand_done['n']}/{grand_total} câu ({pct}%) "
                          f"— câu #{j['b_id']} LỖI (thiếu tệp âm thanh)",
                          f"Generated {grand_done['n']}/{grand_total} sentences ({pct}%) "
                          f"— sentence #{j['b_id']} ERROR (audio file missing)")
                    )
                return ok

            tasks = [asyncio.create_task(_one(j)) for j in vjobs]
            try:
                await asyncio.gather(*tasks)
            except Exception as e:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                raise e

            logger.info(
                L(f"Đã tạo xong giọng đọc {total} câu.", f"Finished voice generation for {total} sentences.")
            )

    async def _tts_auto_fit_retry(
        self, text, audio_path, target_duration, voice, base_rate, project_id=None, max_attempts=3
    ):
        """
        Wrapper quanh generate_voice_auto_fit có TỰ ĐỘNG THỬ LẠI khi TTS lỗi
        (ví dụ Edge-TTS trả 503/timeout làm file rỗng 0 byte). Mỗi lần thất bại
        sẽ chờ backoff tăng dần rồi sinh lại; trả True khi có file audio hợp lệ.
        """
        for attempt in range(1, max_attempts + 1):
            if project_id:
                self._raise_if_cancelled(project_id)
            res = await self.tts_service.generate_voice_auto_fit(
                text,
                audio_path,
                target_duration=target_duration,
                voice=voice,
                overwrite=True,
                base_rate=base_rate,
            )
            ok = res[0] if isinstance(res, tuple) else res
            is_trimmed = res[1] if isinstance(res, tuple) else False
            
            if ok and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                return True, is_trimmed
            if attempt < max_attempts:
                backoff = attempt * 1.5
                logger.warning(
                    L(f"Tạo giọng đọc lỗi (lần {attempt}/{max_attempts}) cho '{text[:25]}...'; "
                    f"thử lại sau {backoff:.1f}s", f"Voice generation error (attempt {attempt}/{max_attempts}) for '{text[:25]}...'; retrying in {backoff:.1f}s")
                )
                await asyncio.sleep(backoff)
        logger.error(L(f"Tạo giọng đọc thất bại sau {max_attempts} lần thử cho '{text[:25]}...'", f"Voice generation failed after {max_attempts} attempts for '{text[:25]}...'"))
        return False, False

    def _raise_if_cancelled(self, project_id: str):
        cancel_path = os.path.join(self.projects_root, project_id, ".cancel_render")
        if os.path.exists(cancel_path):
            raise RenderCancelled(L("Người dùng đã hủy render", "User cancelled the render"))

    def _run_ffmpeg_cancellable(self, cmd, project_id=None, cancel_path=None,
                                on_progress=None, duration_sec=0.0):
        """Chạy ffmpeg qua Popen + poll 0.5s để bấm Hủy có tác dụng NGAY cả khi encode
        đang chạy (video dài encode hàng chục phút — subprocess.run blocking thì Hủy
        vô tác dụng đến hết). Trả (returncode, stderr). Hủy -> kill + raise RenderCancelled.

        ``on_progress(giây_đã_xong, tổng_giây)`` + ``duration_sec`` > 0 thì thêm
        ``-progress pipe:1`` để đọc tiến độ THẬT của ffmpeg. Phải đọc từ STDOUT theo
        khuôn ``key=value``: dòng thống kê mặc định của ffmpeg kết thúc bằng ``\\r``
        (không phải ``\\n``) nên ``readline`` trên stderr sẽ không bao giờ nhả dòng —
        đúng lỗi "kẹt 8%" mà đường xuất compositor đã gặp và sửa theo cách này.
        """
        import time as _time
        if cancel_path is None and project_id:
            cancel_path = os.path.join(self.projects_root, project_id, ".cancel_render")

        want_progress = bool(on_progress) and duration_sec > 0
        run_cmd = list(cmd)
        if want_progress:
            # Chèn TRƯỚC tham số cuối (đường dẫn tệp ra) — ffmpeg đòi tệp ra ở cuối.
            run_cmd[-1:-1] = ["-progress", "pipe:1", "-nostats"]

        proc = subprocess.Popen(
            run_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if want_progress else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        stderr_chunks = []
        import threading as _threading

        def _drain():
            # Đọc stderr liên tục để ffmpeg không bị block khi buffer đầy.
            try:
                for line in proc.stderr:
                    if len(stderr_chunks) < 400:
                        stderr_chunks.append(line)
            except Exception:
                pass

        t = _threading.Thread(target=_drain, daemon=True)
        t.start()

        # Bấm Hủy phải có tác dụng NGAY cả khi đang đọc tiến độ, nên việc canh cờ hủy
        # nằm ở một luồng riêng thay vì trộn vào vòng đọc stdout (vòng đó bị chặn ở
        # readline nên tự nó không canh được).
        cancelled = {"flag": False}

        def _watch_cancel():
            while proc.poll() is None:
                if cancel_path and os.path.exists(cancel_path):
                    cancelled["flag"] = True
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
                _time.sleep(0.5)

        t_cancel = None
        if want_progress:
            t_cancel = _threading.Thread(target=_watch_cancel, daemon=True)
            t_cancel.start()
            if proc.stdout:
                for raw in iter(proc.stdout.readline, b""):
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    # out_time_us=1234567 (micro giây); bản ffmpeg cũ ghi out_time_ms
                    # nhưng giá trị thực tế vẫn là micro giây (quirk lịch sử).
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        try:
                            done = int(line.split("=", 1)[1]) / 1_000_000
                        except ValueError:
                            continue
                        try:
                            on_progress(done, duration_sec)
                        except Exception:
                            pass      # tiến độ hỏng không được làm chết lượt xuất
            proc.wait()
            t_cancel.join(timeout=2)
        else:
            while proc.poll() is None:
                if cancel_path and os.path.exists(cancel_path):
                    proc.kill()
                    proc.wait(timeout=10)
                    raise RenderCancelled(L("Người dùng đã hủy render (dừng ffmpeg giữa chừng)", "User cancelled the render (ffmpeg stopped midway)"))
                _time.sleep(0.5)

        t.join(timeout=2)
        if cancelled["flag"]:
            raise RenderCancelled(L("Người dùng đã hủy render (dừng ffmpeg giữa chừng)", "User cancelled the render (ffmpeg stopped midway)"))
        stderr_text = b"".join(stderr_chunks).decode("utf-8", "ignore")
        return proc.returncode, stderr_text

    async def render_project(self, project_id, force_reset: bool = False):
        """Review chỉ còn overlay 100%: giữ video gốc, TTS theo chunk B."""
        project_dir = os.path.join(self.projects_root, project_id)
        metadata_path = os.path.join(project_dir, "metadata.json")
        a_chunks_path = os.path.join(project_dir, "a_chunks.json")
        b_chunks_path = os.path.join(project_dir, "b_chunks.json")

        if not all(
            os.path.exists(p) for p in [metadata_path, a_chunks_path, b_chunks_path]
        ):
            logger.error(L(f"Thiếu dữ liệu dự án {project_id} (chưa phân tích/tạo kịch bản?)", f"Missing data for project {project_id} (not analyzed / script not generated yet?)"))
            return False

        self._raise_if_cancelled(project_id)

        with open(metadata_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        with open(a_chunks_path, "r", encoding="utf-8") as f:
            a_chunks_raw = json.load(f)
            a_chunks_list = sorted(a_chunks_raw, key=lambda x: x["a_id"])
            a_chunks_lookup = {str(a["a_id"]): a for a in a_chunks_list}

        with open(b_chunks_path, "r", encoding="utf-8") as f:
            b_chunks = json.load(f)

        output_dir = os.path.join(project_dir, "output")
        os.makedirs(output_dir, exist_ok=True)

        return await self.render_overlay_project(
            project_id, meta, a_chunks_lookup, b_chunks, output_dir, force_reset=force_reset
        )


    def _get_duration(self, path):
        # WAV (audio_tts.wav là PCM): đọc thẳng header — ~0.1ms, không spawn ffmpeg.
        # Với hàng trăm câu, đường ffmpeg tốn 100-300ms/process là bottleneck chính.
        if path.lower().endswith(".wav"):
            try:
                import wave
                with wave.open(path, "rb") as w:
                    fr = w.getframerate()
                    if fr > 0 and w.getnframes() > 0:
                        return w.getnframes() / fr
            except Exception:
                pass  # header lạ/không phải PCM chuẩn -> fallback ffmpeg bên dưới
        cmd = [FFMPEG_EXE, "-i", path]
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            close_fds=True,
        )
        import re

        match = re.search(r"Duration:\s*(\d{2}):(\d{2}):([\d\.]+)", res.stderr)
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
        return 0

    # ------------------------------------------------------------------ tron giong
    #  TRON GIONG BANG PHEP CONG PCM (numpy), khong qua ffmpeg.
    #
    #  VI SAO PHAI DOI: bản cũ (``_mix_tts_audios_ffmpeg``, giờ chỉ còn là đường lùi)
    #  chia câu thành lô 150 rồi ``amix`` lô mới CHỒNG LÊN master của lô trước. Mỗi lô
    #  phải ĐỌC LẠI + GHI LẠI toàn bộ master. Master của phim 2 giờ là WAV 44.1kHz
    #  stereo 16-bit ≈ 1,2 GB, nên 2000 câu = 14 lô = khoảng 17 GB I/O — đo thực tế
    #  trên máy 8 nhân: HƠN 20 PHÚT chỉ để trộn tiếng, trong khi phép cộng PCM cùng
    #  bộ dữ liệu mất 6 GIÂY.
    #
    #  Trộn giọng đọc bản chất chỉ là "cộng các dải số vào đúng vị trí" — đúng những gì
    #  ``amix ... normalize=0`` làm, mà numpy làm trong một lượt qua bộ nhớ. Đã đối chiếu
    #  hai bản trên cùng dữ liệu: RMS trùng nhau (3325.158 vs 3325.163), chênh lệch còn
    #  lại ≤1 mẫu (0,02 ms) do CHÍNH ``adelay`` của ffmpeg làm tròn ms→mẫu không nhất
    #  quán (đo được: cùng một công thức mà lệch 0 hoặc −1 mẫu tuỳ giá trị). Tức bản
    #  mới đặt câu ĐÚNG HƠN bản cũ, không phải hồi quy.
    #
    #  CỘNG BẰNG int32 rồi mới kẹp về int16: cộng thẳng trên int16 sẽ TRÀN ÂM (hai câu
    #  chồng nhau to thành ra tiếng rè xé tai). Kẹp ở cuối cho ra đúng hành vi
    #  ``normalize=0`` của amix.
    _MIX_SR = 44100          # master luôn 44.1kHz stereo — mọi bước sau đã giả định vậy
    _MIX_MAX_BYTES = 6 * 1024 ** 3   # trần bộ nhớ cho mảng master (~6 GB ≈ phim 9 tiếng)

    def _read_wav_pcm16(self, path):
        """Đọc WAV PCM 16-bit thành mảng (n, 2) int16 @44.1kHz, hoặc None nếu không đọc nổi.

        Trả None cho MỌI tệp không đúng khuôn (nén, 8/24/32-bit, sample rate khác) thay
        vì tự đoán — chỗ gọi sẽ nhờ ffmpeg quy đổi tệp đó. Tự nội suy sample rate ở đây
        là mở đường cho lỗi lệch cao độ rất khó lần.
        """
        import wave
        import contextlib
        import numpy as np
        try:
            with contextlib.closing(wave.open(path, "rb")) as w:
                if w.getsampwidth() != 2:
                    return None
                if w.getframerate() != self._MIX_SR:
                    return None
                ch = w.getnchannels()
                if ch not in (1, 2):
                    return None
                raw = w.readframes(w.getnframes())
        except Exception:
            return None
        a = np.frombuffer(raw, dtype="<i2")
        if ch == 2:
            if a.size % 2:
                a = a[: a.size - 1]
            return a.reshape(-1, 2)
        # MONO -> nhân đôi thành stereo, đúng như ``aformat=channel_layouts=stereo``.
        return np.repeat(a.reshape(-1, 1), 2, axis=1)

    def _to_mix_wav(self, path):
        """Quy đổi một tệp tiếng LẠ KHUÔN về WAV 44.1kHz stereo 16-bit cạnh tệp gốc.

        Chỉ chạy cho tệp mà ``_read_wav_pcm16`` từ chối (giọng của TTS lạ, tệp mp3 lọt
        vào, WAV 24-bit…). Bản quy đổi được GIỮ LẠI và mang dấu vân tay (mtime+size) của
        tệp gốc trong tên: sinh lại giọng thì vân tay đổi nên không bao giờ dùng nhầm
        bản cũ — cùng luật với ``TL.source_tag``.
        """
        try:
            st = os.stat(path)
            tag = f"{st.st_mtime_ns}_{st.st_size}"
        except OSError:
            return None
        base, _ = os.path.splitext(path)
        dst = f"{base}.mix44k_{tag}.wav"
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst
        tmp = dst + ".tmp.wav"
        cmd = [
            FFMPEG_EXE, "-y", "-i", path,
            "-ac", "2", "-ar", str(self._MIX_SR), "-c:a", "pcm_s16le", tmp,
        ]
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, close_fds=True)
        if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            safe_replace(tmp, dst)
            return dst
        safe_remove(tmp)
        return None

    def _mix_tts_audios_batched(self, tts_items, output_master_wav, batch_size=150,
                                project_id=None):
        """Trộn các câu giọng đọc (đã có ``delay_ms``) thành MỘT master WAV.

        Đi đường cộng PCM bằng numpy; gặp bất cứ trở ngại nào (thiếu numpy, master lớn
        quá trần bộ nhớ, tệp không quy đổi được) thì LÙI về đường ffmpeg cũ để không bao
        giờ chặn người dùng xuất video. ``batch_size`` chỉ còn ý nghĩa cho đường lùi.
        """
        if not tts_items:
            # Master rỗng: một giây im lặng, giữ nguyên hành vi cũ.
            cmd = [
                FFMPEG_EXE, "-y",
                "-f", "lavfi", "-i", f"anullsrc=r={self._MIX_SR}:cl=stereo",
                "-t", "1.0",
                output_master_wav,
            ]
            subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, close_fds=True)
            return True

        try:
            ok = self._mix_tts_audios_pcm(tts_items, output_master_wav, project_id=project_id)
        except RenderCancelled:
            raise
        except MemoryError:
            logger.warning(L("[trộn giọng] Không đủ bộ nhớ cho phép cộng PCM — lùi về ffmpeg.", "[voice mix] Not enough memory for PCM summing — falling back to ffmpeg."))
            ok = False
        except Exception as e:
            logger.warning(L(f"[trộn giọng] Phép cộng PCM lỗi ({e}) — lùi về ffmpeg.", f"[voice mix] PCM summing failed ({e}) — falling back to ffmpeg."))
            ok = False
        if ok:
            return True
        return self._mix_tts_audios_ffmpeg(
            tts_items, output_master_wav, batch_size=batch_size, project_id=project_id
        )

    #  Cua truot khi cong PCM: 2 phut mot lan. 2 phut stereo int32 = 42MB — du nho de
    #  may nao cung chay, du to de moi cau (vai giay) hau het nam gon trong MOT cua nen
    #  khong phai doc lai tep.
    _MIX_CUA_GIAY = 120

    def _mix_tts_audios_pcm(self, tts_items, output_master_wav, project_id=None):
        """Cong cac cau vao master roi ghi ra WAV, THEO TUNG CUA 2 PHUT. Tra False de lui.

        VI SAO PHAI CAT CUA — ban dau ham nay cap phat NGUYEN mot mang int32 dai bang ca
        phim. Do that tren phim 3 tieng (44.1kHz stereo):

            mang master int32          : 3,55 GB
            hai mang bool cua phep dem mau bi kep + phep ``|``  : ~1,9 GB
            DINH RSS do duoc cua tien trinh                     : 6,24 GB

        Tran cu (``_MIX_MAX_BYTES`` 6GB) chi chan o quang 9 tieng nen KHONG BAO GIO cuu
        duoc ai: phim 3 tieng loc qua duoc roi ngoi an 6GB. May nguoi dung con dang mo
        Electron + model tach giong, nen day la luc "bam xuat duoc mot luc la treo" —
        may bat dau trao doi trang, backend nghen, ma ffmpeg thi van chay tiep.

        Cat cua xong thi bo nho khong con phu thuoc DO DAI PHIM nua: 3 tieng hay 10
        tieng deu chi giu mot cua 42MB.
        """
        import time as _time
        import wave
        import contextlib
        try:
            import numpy as np
        except ImportError:
            logger.warning(L("[trộn giọng] Không có numpy — lùi về đường ffmpeg.", "[voice mix] numpy is not available — falling back to the ffmpeg path."))
            return False

        sr = self._MIX_SR
        t0 = _time.time()

        # 1. Đọc kích thước từng câu TRƯỚC để biết master dài bao nhiêu. Chỉ đọc phần
        #    header nên rất nhanh, và biết trước độ dài thì cấp phát đúng một lần.
        plan = []          # (path_da_quy_doi, offset_mau, so_mau, path_goc)
        for it in tts_items:
            if project_id:
                self._raise_if_cancelled(project_id)
            path = goc = it["path"]
            n = self._wav_frames(path, sr)
            if n is None:
                conv = self._to_mix_wav(path)
                if not conv:
                    logger.warning(
                        L(f"[trộn giọng] Không đọc/quy đổi được tệp giọng, lùi về ffmpeg: {path}", f"[voice mix] Could not read/convert the voice file, falling back to ffmpeg: {path}")
                    )
                    return False
                path = conv
                n = self._wav_frames(path, sr)
                if n is None:
                    return False
            if n <= 0:
                continue
            off = int(round(float(it["delay_ms"]) / 1000.0 * sr))
            plan.append((path, max(0, off), n, goc))

        if not plan:
            logger.warning(L("[trộn giọng] Không câu nào đọc được — lùi về ffmpeg.", "[voice mix] No sentence could be read — falling back to ffmpeg."))
            return False

        total = max(off + n for _, off, n, _g in plan)
        #  Tran GIO khong con la bo nho (da cat cua) ma la KHUON TEP WAV: truong kich
        #  thuoc cua RIFF chi 32-bit, tren ~4GB la tep hong ma khong bao loi. 16-bit
        #  stereo 44.1kHz -> 4 byte/mau, tuc gioi han quanh 6 tieng 45 phut.
        if total * 4 + 1024 > 0xFFFFFFF0:
            logger.warning(
                L(f"[trộn giọng] Master {total / sr / 3600:.1f} tiếng vượt giới hạn 4GB của "
                "khuôn WAV — lùi về đường ffmpeg.", f"[voice mix] The {total / sr / 3600:.1f}-hour master exceeds the 4GB limit of the WAV container — falling back to the ffmpeg path.")
            )
            return False

        # 2. Cộng theo TỪNG CỬA rồi ghi thẳng ra tệp, không giữ cả phim trong bộ nhớ.
        #    Sắp theo mốc để mỗi cửa chỉ phải ngó tới các câu nằm trong nó.
        plan.sort(key=lambda x: x[1])
        cua = max(1, int(self._MIX_CUA_GIAY * sr))
        clipped = 0
        i_tiep = 0          # câu đầu tiên CHƯA vào cửa nào
        con_treo = []       # câu dài vắt sang cửa sau: (path, off, n, goc)

        tmp = output_master_wav + ".tmp.wav"
        try:
            with contextlib.closing(wave.open(tmp, "wb")) as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(sr)
                dem = 0
                for s0 in range(0, total, cua):
                    if project_id:
                        self._raise_if_cancelled(project_id)
                    s1 = min(s0 + cua, total)
                    buf = np.zeros((s1 - s0, 2), dtype=np.int32)

                    # Câu vắt từ cửa trước + câu mới bắt đầu trong cửa này.
                    trong_cua = [x for x in con_treo if x[1] + x[2] > s0]
                    con_treo = []
                    while i_tiep < len(plan) and plan[i_tiep][1] < s1:
                        trong_cua.append(plan[i_tiep])
                        i_tiep += 1

                    for path, off, n, goc in trong_cua:
                        if off + n > s1:
                            con_treo.append((path, off, n, goc))
                        a = self._read_wav_pcm16(path)
                        if a is None and goc != path:
                            #  BẢN QUY ĐỔI BIẾN MẤT GIỮA CHỪNG (bị dọn trong lúc xuất) —
                            #  quy đổi lại từ tệp gốc thay vì bỏ cả đường nhanh: đường
                            #  ffmpeg chậm gấp trăm lần và không có tiến độ.
                            conv = self._to_mix_wav(goc)
                            a = self._read_wav_pcm16(conv) if conv else None
                            if a is not None:
                                logger.info(L(f"[trộn giọng] Quy đổi lại tệp bị mất: {goc}", f"[voice mix] Re-converting a lost file: {goc}"))
                        if a is None:
                            logger.warning(L(f"[trộn giọng] Đọc lại thất bại, lùi về ffmpeg: {path}", f"[voice mix] Re-read failed, falling back to ffmpeg: {path}"))
                            return False
                        # Phần của câu này rơi vào cửa hiện tại.
                        d = max(s0, off) - off          # bỏ qua bao nhiêu mẫu đầu câu
                        lay = min(a.shape[0], s1 - off) - d
                        if lay > 0:
                            b0 = max(0, off - s0)
                            buf[b0:b0 + lay] += a[d:d + lay]
                        dem += 1

                    clipped += int(np.count_nonzero((buf > 32767) | (buf < -32768)))
                    np.clip(buf, -32768, 32767, out=buf)
                    w.writeframes(buf.astype("<i2").tobytes())
        except RenderCancelled:
            safe_remove(tmp)
            raise
        except Exception as e:
            safe_remove(tmp)
            logger.warning(L(f"[trộn giọng] Ghi master thất bại ({e}) — lùi về ffmpeg.", f"[voice mix] Writing the master failed ({e}) — falling back to ffmpeg."))
            return False

        if clipped:
            logger.info(
                L(f"[trộn giọng] {clipped} mẫu bị kẹp biên do các câu chồng nhau "
                "(giống hệt amix normalize=0 của bản cũ).", f"[voice mix] {clipped} samples were clipped because sentences overlap (same as amix normalize=0 in the old version).")
            )
        safe_replace(tmp, output_master_wav)
        logger.info(
            L(f"[trộn giọng] Đã trộn {len(plan)} câu thành master "
            f"{total / sr / 60:.1f} phút trong {_time.time() - t0:.1f}s "
            f"(cửa {self._MIX_CUA_GIAY}s, đỉnh bộ nhớ ~{cua * 8 / 1024 ** 2:.0f}MB).", f"[voice mix] Mixed {len(plan)} sentences into a {total / sr / 60:.1f}-minute master in {_time.time() - t0:.1f}s (window {self._MIX_CUA_GIAY}s, memory peak ~{cua * 8 / 1024 ** 2:.0f}MB).")
        )
        return True

    def _wav_frames(self, path, sr):
        """Số mẫu của WAV PCM 16-bit @``sr`` (đọc header thôi), None nếu lạ khuôn."""
        import wave
        import contextlib
        try:
            with contextlib.closing(wave.open(path, "rb")) as w:
                if w.getsampwidth() != 2 or w.getframerate() != sr:
                    return None
                if w.getnchannels() not in (1, 2):
                    return None
                return w.getnframes()
        except Exception:
            return None

    def _mix_tts_audios_ffmpeg(self, tts_items, output_master_wav, batch_size=150, project_id=None):
        # batch_size 150 (trước 40): filter đã ghi ra file script nên command line chỉ
        # còn các "-i path" (~150×100 ký tự ≈ 15K < trần 32K của Windows). Batch lớn
        # hơn = ít lần re-encode master chồng lên nhau hơn (300 câu: 8 lần -> 2 lần).
        """
        Mixes multiple delayed TTS WAV files into a single master WAV file in batches
        to avoid the Windows command line length limit.
        
        tts_items: list of dicts with {"path": str, "delay_ms": int}
        """
        if not tts_items:
            # Create a silent master wav of 1 second if empty
            cmd = [
                FFMPEG_EXE, "-y",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "1.0",
                output_master_wav
            ]
            subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, close_fds=True)
            return True

        current_master = None
        
        # Divide into batches
        for i in range(0, len(tts_items), batch_size):
            # Hủy giữa các lô trộn (video dài nhiều lô, mỗi lô re-encode master).
            if project_id:
                self._raise_if_cancelled(project_id)
            batch = tts_items[i : i + batch_size]
            temp_output = output_master_wav + f".tmp_{i}.wav"
            
            inputs = []
            filter_complex = []
            mix_pads = []
            
            if current_master:
                inputs.extend(["-i", current_master])
                filter_complex.append("[0:a]aformat=sample_rates=44100:channel_layouts=stereo[master_ac];")
                mix_pads.append("[master_ac]")
                start_input_idx = 1
            else:
                start_input_idx = 0

            for idx, item in enumerate(batch):
                inputs.extend(["-i", item["path"]])
                input_id = start_input_idx + idx
                delay = item["delay_ms"]
                filter_complex.append(
                    f"[{input_id}:a]adelay={delay}|{delay},"
                    f"aformat=sample_rates=44100:channel_layouts=stereo[a_del_{i}_{idx}];"
                )
                mix_pads.append(f"[a_del_{i}_{idx}]")

            # amix filter
            pads_str = "".join(mix_pads)
            filter_complex.append(
                f"{pads_str}amix=inputs={len(mix_pads)}:duration=longest:dropout_transition=0:normalize=0[out]"
            )
            
            # Write filter complex script
            filter_script_path = output_master_wav + f".filter_tmp_{i}.txt"
            with open(filter_script_path, "w", encoding="utf-8") as f:
                f.write("".join(filter_complex))
                
            cmd = (
                [FFMPEG_EXE, "-y"]
                + inputs
                + [
                    "-filter_complex_script", filter_script_path,
                    "-map", "[out]",
                    "-acodec", "pcm_s16le",
                    temp_output
                ]
            )
            
            try:
                # project_id=None -> helper chạy như run() thường (không kiểm cancel).
                rc, stderr_text = self._run_ffmpeg_cancellable(cmd, project_id=project_id)
            finally:
                if os.path.exists(filter_script_path):
                    try: os.remove(filter_script_path)
                    except: pass

            if rc != 0:
                logger.error(L(f"Trộn giọng đọc lỗi (lô {i}): {stderr_text[-1000:]}", f"Voice mix error (batch {i}): {stderr_text[-1000:]}"))
                # Clean up and return False
                if current_master and current_master != output_master_wav:
                    try: os.remove(current_master)
                    except: pass
                return False
                
            # Clean up old master
            if current_master:
                try: os.remove(current_master)
                except: pass
                
            current_master = temp_output

        # Rename the final current_master to output_master_wav
        if current_master:
            if os.path.exists(output_master_wav):
                try: os.remove(output_master_wav)
                except: pass
            try:
                os.rename(current_master, output_master_wav)
            except Exception as e:
                logger.error(f"Failed to rename master file {current_master} to {output_master_wav}: {e}")
                # Fallback to copy/remove
                import shutil
                try:
                    shutil.copy(current_master, output_master_wav)
                    os.remove(current_master)
                except Exception as e2:
                    logger.error(f"Fallback copy failed: {e2}")
                    return False
            
        return True

    async def render_overlay_project(self, project_id, meta, a_chunks_lookup, b_chunks, output_dir, force_reset: bool = False):
        project_dir = os.path.join(self.projects_root, project_id)
        metadata_path = os.path.join(project_dir, "metadata.json")
        b_chunks_path = os.path.join(project_dir, "b_chunks.json")

        # Chặn b_id TRÙNG trong b_chunks.json (từ import thủ công). b_id trùng ->
        # 2 câu ghi đè cùng B{id}/audio_tts.wav + SRT chồng, log hiện "câu 2,3,4"
        # lặp. KHÔNG tự sửa: dừng render với thông báo rõ để người dùng sửa kịch bản.
        from services.review_service import find_duplicate_b_ids
        dup_ids = find_duplicate_b_ids(b_chunks)
        if dup_ids:
            # Liệt kê NỘI DUNG từng dòng trùng để người dùng tìm & sửa được ngay
            # (chỉ nêu số b_id thì không biết 2 dòng nào). VD trùng câu 51 với 2 nội
            # dung khác nhau -> 1 dòng khớp cache (dùng lại), 1 dòng khác (tạo mới),
            # cả hai ghi đè cùng B51/audio_tts.wav.
            def _snippet(b):
                t = (b.get("b_text") or b.get("text") or b.get("target_text") or "").strip()
                return (t[:40] + "…") if len(t) > 40 else (t or L("(trống)", "(empty)"))
            details = []
            for d in dup_ids:
                variants = [_snippet(b) for b in b_chunks if str(b.get("b_id")) == str(d)]
                details.append(L(f"  • Câu {d} (x{len(variants)}): ", f"  • Sentence {d} (x{len(variants)}):") + " | ".join(variants))
            msg = (
                L("Kịch bản có b_id bị TRÙNG (mỗi câu phải có b_id riêng): ", "The script has DUPLICATE b_id values (every sentence needs its own b_id):")
                + ", ".join(str(d) for d in dup_ids)
                + ".\n"
                + "\n".join(details)
                + L("\nHãy sửa để mỗi câu một b_id rồi dựng lại.", "\nFix it so each sentence has its own b_id, then build again.")
            )
            logger.error(msg)
            raise ValueError(msg)

        logger.info(L(f"Tổng {len(b_chunks)} câu cần dựng (force_reset={force_reset}). Đang kiểm tra cache giọng đọc…", f"{len(b_chunks)} sentences to build in total (force_reset={force_reset}). Checking the voice cache…"))
        self._raise_if_cancelled(project_id)

        from services.separation_service import SeparationService

        video_path, _ = SeparationService.resolve_source_media(meta.get("video_path", ""))
        meta["video_path"] = video_path
        
        if force_reset:
            clear_render_cache(project_dir)
            render_cache = {"version": RENDER_CACHE_VERSION, "clips": {}}
            logger.info(L("=== ĐÃ XÓA BỘ NHỚ TẠM (FORCE RESET): TẠO MỚI TOÀN BỘ GIỌNG ĐỌC ===", "=== TEMP CACHE CLEARED (FORCE RESET): REGENERATING ALL VOICES ==="))
        else:
            render_cache = load_render_cache(project_dir)
        cache_skipped = 0
        cache_regen = 0

        # Xê dịch SRT thông minh: sinh giọng ở tốc độ CỐ ĐỊNH (default_rate, "auto"->1x),
        # KHÔNG ép/cắt cho vừa khung; sau đó dồn thời điểm để giọng 1x không chồng nhau.
        smart_shift = bool(meta.get("smart_srt_shift", True))
        smart_fixed_rate = self._smart_fixed_rate(meta.get("default_rate"))
        # Xoá thời gian xê dịch của lần render trước để tính lại từ mốc NATURAL (và để
        # khi TẮT smart thì SRT/voice quay về khung gốc).
        for _b in b_chunks:
            _b.pop("shift_start", None)
            _b.pop("shift_end", None)
            _b.pop("shift_audio", None)

        # 1. Collect TTS jobs (skip cache) — VieNeu+GPU se batch giong long tieng
        tts_jobs = []
        for b_data in b_chunks:
            self._raise_if_cancelled(project_id)
            b_id = b_data["b_id"]
            b_text = (b_data.get("b_text") or b_data.get("text") or b_data.get("target_text") or b_data.get("content") or "").strip()
            source_a_ids = [int(aid) for aid in b_data["source_a_ids"]]

            if not source_a_ids:
                continue

            # Thời gian đặt voice: ưu tiên override do người dùng sửa; nếu không thì
            # suy từ A. resolve_b_time trả None khi source_a_ids trỏ A không tồn tại.
            b_start_time, b_end_time = resolve_b_time(b_data, a_chunks_lookup)
            if b_start_time is None:
                logger.warning(
                    L(f"Câu {b_id}: không xác định được thời gian hiển thị, bỏ qua câu này.", f"Sentence {b_id}: display time cannot be determined, skipping this sentence.")
                )
                continue
            target_clip_duration = b_end_time - b_start_time

            b_clip_dir = os.path.join(output_dir, f"B{b_id}")
            os.makedirs(b_clip_dir, exist_ok=True)

            audio_path = os.path.join(b_clip_dir, "audio_tts.wav")
            fp_tts = compute_tts_fingerprint(meta, b_data)
            hit, _ = try_reuse_clip(
                b_id, fp_tts, output_dir, render_cache, "audio_tts.wav", layer="tts"
            )
            if hit:
                # KHÔNG log từng câu (trước đây spam hàng chục dòng "dùng lại") — dòng
                # tổng "Kế hoạch tạo giọng đọc: X câu dùng lại…" bên dưới đã đủ.
                cache_skipped += 1
                continue

            if not (b_text or "").strip():
                continue

            cache_regen += 1
            tts_jobs.append({
                "b_id": b_id,
                "text": lam_sach_cau_doc(b_text),          # lô VieNeu-GPU không qua tts_service.generate_voice -> làm sạch ở đây

                # Smart shift: KHÔNG ép/cắt -> target lớn để giữ trọn giọng ở tốc độ cố định.
                "target_duration": (1e9 if smart_shift else target_clip_duration),
                "path": audio_path,
                "voice": self._get_voice(meta, b_data),
                # Ưu tiên rate riêng của B; nếu không có -> tốc độ mặc định của dự án.
                # Smart shift: dùng tốc độ CỐ ĐỊNH (không "auto" tự khớp).
                "base_rate": (
                    smart_fixed_rate if smart_shift
                    else (b_data.get("rate") or meta.get("default_rate") or "auto")
                ),
                "fp_tts": fp_tts,
            })

        # Log TRƯỚC khi sinh TTS — FE ước lượng thời gian theo số clip thực sự regen (không tính như lần đầu).
        logger.info(
            L(f"Kế hoạch tạo giọng đọc: {cache_skipped} câu dùng lại, "
            f"{cache_regen} câu tạo mới (tổng {len(b_chunks)})", f"Voice plan: {cache_skipped} sentences reused, {cache_regen} newly generated (total {len(b_chunks)})")
        )

        self._raise_if_cancelled(project_id)
        await self._run_tts_jobs(tts_jobs, project_id=project_id, render_cache=render_cache, project_dir=project_dir)
        self._raise_if_cancelled(project_id)
        for j in tts_jobs:
            p = j["path"]
            if os.path.exists(p) and os.path.getsize(p) > 0:
                remember_clip(render_cache, j["b_id"], j["fp_tts"], layer="tts")

        logger.info(
            L(f"Đã tạo giọng đọc: {cache_skipped} câu dùng lại, {cache_regen} câu tạo mới", f"Voice generated: {cache_skipped} sentences reused, {cache_regen} newly generated")
        )
        save_render_cache(project_dir, render_cache)

        # 1b. AUTO-RETRY: sau khi chạy xong CẢ LOẠT TTS, tự động sinh lại những
        # chunk bị lỗi (file thiếu / 0 byte do Edge-TTS trả 503/timeout). Thử lại
        # theo từng vòng với backoff tăng dần để vượt qua lỗi tạm thời của dịch vụ.
        def _failed_overlay_chunks():
            bad = []
            for b_item in b_chunks:
                src = [int(aid) for aid in b_item.get("source_a_ids", [])]
                if not src:
                    continue
                # Bỏ qua các chunk KHÔNG THỂ sinh TTS (để không retry vô ích cả 3 vòng):
                #  - thiếu/blank b_text  (generate_voice trả False ngay, không tạo file)
                #  - tham chiếu A-segment không tồn tại
                if not (b_item.get("b_text") or "").strip():
                    continue
                if str(min(src)) not in a_chunks_lookup or str(max(src)) not in a_chunks_lookup:
                    continue
                p = os.path.join(output_dir, f"B{b_item['b_id']}", "audio_tts.wav")
                if (not os.path.exists(p)) or os.path.getsize(p) == 0:
                    bad.append(b_item)
            return bad

        failed_chunks = _failed_overlay_chunks()
        max_retry_rounds = 3
        retry_round = 0
        while failed_chunks and retry_round < max_retry_rounds:
            self._raise_if_cancelled(project_id)
            retry_round += 1
            failed_ids = [b_item["b_id"] for b_item in failed_chunks]
            logger.info(
                L(f"Tạo lại giọng đọc (lần {retry_round}/{max_retry_rounds}): "
                f"{len(failed_chunks)} câu bị lỗi {failed_ids}. Đang thử lại...", f"Regenerating voice (round {retry_round}/{max_retry_rounds}): {len(failed_chunks)} sentences failed {failed_ids}. Retrying...")
            )
            await asyncio.sleep(2.0 * retry_round)
            retry_jobs = []
            for b_item in failed_chunks:
                b_start_time, b_end_time = resolve_b_time(b_item, a_chunks_lookup)
                if b_start_time is None:
                    continue
                target_clip_duration = b_end_time - b_start_time
                audio_path = os.path.join(
                    output_dir, f"B{b_item['b_id']}", "audio_tts.wav"
                )
                retry_jobs.append({
                    "b_id": b_item["b_id"],
                    "text": lam_sach_cau_doc(b_item["b_text"]),
                    "path": audio_path,
                    "target_duration": (1e9 if smart_shift else target_clip_duration),
                    "voice": self._get_voice(meta, b_item),
                    "base_rate": (
                        smart_fixed_rate if smart_shift
                        else (b_item.get("rate") or meta.get("default_rate") or "auto")
                    ),
                    "fp_tts": compute_tts_fingerprint(meta, b_item),
                })
            await self._run_tts_jobs(retry_jobs, project_id=project_id, render_cache=render_cache, project_dir=project_dir)
            for j in retry_jobs:
                p = j["path"]
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    remember_clip(render_cache, j["b_id"], j["fp_tts"], layer="tts")
            save_render_cache(project_dir, render_cache)
            failed_chunks = _failed_overlay_chunks()

        if failed_chunks:
            logger.error(
                L(f"Vẫn không tạo được giọng đọc sau {max_retry_rounds} lần thử cho các câu "
                f"{[b_item['b_id'] for b_item in failed_chunks]}; "
                f"những câu này sẽ bị im lặng trong video xuất ra.", f"Still could not generate voice after {max_retry_rounds} attempts for sentences {[b_item['b_id'] for b_item in failed_chunks]}; these sentences will be silent in the exported video.")
            )

        # 1c. XÊ DỊCH SRT THÔNG MINH: giọng đã sinh ở tốc độ cố định (1x) -> dồn thời
        # điểm để không chồng nhau; nếu tổng dài hơn video thì tăng tốc TOÀN BỘ chút
        # cho vừa. Ghi shift_start/shift_end vào b_chunks -> voice/SRT/compositor dùng chung.
        if smart_shift:
            self._raise_if_cancelled(project_id)
            # Tốc độ đọc GỐC bạn chọn (auto -> 1.0x). Dùng để giới hạn tăng tốc theo
            # TỔNG tốc độ, tránh base cao (vd +50%) rồi còn nhân thêm -> quá nhanh.
            base_speed = self._rate_multiplier(self._smart_fixed_rate(meta.get("default_rate")))
            self._apply_smart_shift(
                b_chunks, a_chunks_lookup, output_dir,
                float(meta.get("video_duration") or 0.0),
                base_speed=base_speed,
            )

        # 2. Build FFmpeg command to overlay all generated audios on the original video
        final_output = os.path.join(project_dir, "final_review.mp4")
        tmp_output = os.path.join(project_dir, "final_review.tmp.mp4")

        # Tách giọng gốc / nhạc nền (Demucs) nếu dự án bật separate_audio.
        # Stems tách từ video nguồn, cache tại <project_dir>/separated.
        # SeparationError propagate lên run_render_wrapper -> log "Synthesis failed".
        stems = None
        # Mặc định TẮT (False) theo chế độ review mới.
        if meta.get("separate_audio", False):
            self._raise_if_cancelled(project_id)
            # Trên GPU nhỏ (4GB): nhả VieNeu khỏi VRAM TRƯỚC khi chạy Demucs để 2 model
            # không tranh VRAM cùng lúc (TTS đã xong ở bước trên, không cần VieNeu nữa
            # trong lần render này). CPU thì bỏ qua (tránh cold-load vô ích).
            try:
                from utils.hardware_utils import get_best_device
                if get_best_device()[0] == "cuda":
                    from services.vieneu_service import VieneuService
                    VieneuService.release_gpu()
                    logger.info(L("Đã nhả VieNeu khỏi VRAM để nhường GPU cho tách nhạc nền.", "Released VieNeu from VRAM to give the GPU to background-music separation."))
            except Exception as _e:
                logger.warning(L(f"Nhả VieNeu trước Demucs lỗi (bỏ qua): {_e}", f"Releasing VieNeu before Demucs failed (ignored): {_e}"))
            logger.info(L("=== TÁCH GIỌNG GỐC VÀ NHẠC NỀN ===", "=== SEPARATING ORIGINAL VOICE AND BACKGROUND MUSIC ==="))
            from services.separation_service import SeparationCancelled
            cancel_marker = os.path.join(project_dir, ".cancel_render")
            try:
                stems = SeparationService.ensure_stems(
                    meta["video_path"], os.path.join(project_dir, "separated"),
                    check_cancel=lambda: os.path.exists(cancel_marker),
                )
            except SeparationCancelled:
                raise RenderCancelled(L("Người dùng đã hủy render", "User cancelled the render"))
            self._raise_if_cancelled(project_id)

        # Collect TTS files to mix and their ducking windows
        tts_to_mix = []
        duck_windows = []  # (start, end) các khoảng AI đang nói -> hạ tiếng gốc

        for b_data in b_chunks:
            b_id = b_data["b_id"]
            b_clip_dir = os.path.join(output_dir, f"B{b_id}")
            # Smart shift có thể tạo bản giọng đã tăng tốc (audio_shift.wav); dùng bản đó.
            audio_path = os.path.abspath(
                b_data.get("shift_audio")
                or os.path.join(b_clip_dir, "audio_tts.wav")
            )
            if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
                continue
                
            b_start_time, _b_end_time = resolve_b_time(b_data, a_chunks_lookup)
            if b_start_time is None:
                logger.warning(
                    L(f"Câu {b_id}: không xác định được thời gian; bỏ qua khi ghép.", f"Sentence {b_id}: timing cannot be determined; skipped when merging.")
                )
                continue
            delay_ms = int(b_start_time * 1000)

            # Thời lượng audio TTS thực tế -> xác định khoảng cần "ducking" tiếng gốc.
            # File là .wav nên _get_duration đọc header trực tiếp (không spawn ffmpeg).
            seg_dur = self._get_duration(audio_path)

            tts_to_mix.append({
                "path": audio_path,
                "delay_ms": delay_ms,
                "b_id": b_id
            })
            if seg_dur and seg_dur > 0.05:
                duck_windows.append((b_start_time, b_start_time + seg_dur))

        if not tts_to_mix:
            logger.warning(L("Không tạo được giọng đọc nào, giữ nguyên video gốc.", "No voice could be generated, keeping the original video."))
            cmd = [FFMPEG_EXE, "-y", "-i", meta["video_path"], "-c", "copy", final_output]
            self._run_ffmpeg_cancellable(cmd, project_id=project_id)
        else:
            master_tts_path = os.path.join(project_dir, "master_tts.wav")
            
            # Generate a fingerprint of tts_to_mix to cache merging
            import json
            import hashlib
            
            mix_fingerprint = []
            for x in tts_to_mix:
                mtime = os.path.getmtime(x["path"]) if os.path.exists(x["path"]) else 0
                size = os.path.getsize(x["path"]) if os.path.exists(x["path"]) else 0
                mix_fingerprint.append({
                    "path": x["path"],
                    "delay_ms": x["delay_ms"],
                    "mtime": mtime,
                    "size": size
                })
            
            fingerprint_str = json.dumps(mix_fingerprint, sort_keys=True)
            fingerprint_hash = hashlib.md5(fingerprint_str.encode("utf-8")).hexdigest()
            fingerprint_file = os.path.join(project_dir, "master_tts_fingerprint.json")
            
            skip_mix = False
            if os.path.exists(master_tts_path) and os.path.exists(fingerprint_file):
                try:
                    with open(fingerprint_file, "r", encoding="utf-8") as f:
                        cached_hash = f.read().strip()
                    if cached_hash == fingerprint_hash:
                        skip_mix = True
                        logger.info(L("Dùng lại bản trộn giọng đọc master_tts.wav từ cache (không đổi kịch bản/vị trí)", "Reusing the voice mix master_tts.wav from cache (script/positions unchanged)"))
                except Exception:
                    pass

            if skip_mix:
                mix_ok = True
            else:
                logger.info(L(f"Đang trộn giọng đọc của {len(tts_to_mix)} câu thành một dải âm thanh...", f"Mixing the voice of {len(tts_to_mix)} sentences into one audio track..."))
                self._raise_if_cancelled(project_id)
                mix_ok = self._mix_tts_audios_batched(tts_to_mix, master_tts_path, project_id=project_id)
                if mix_ok:
                    try:
                        with open(fingerprint_file, "w", encoding="utf-8") as f:
                            f.write(fingerprint_hash)
                    except Exception:
                        pass

            if not mix_ok:
                logger.error(L("Trộn giọng đọc thất bại.", "Voice mixing failed."))
                return False

            inputs = ["-i", meta["video_path"]]
            if stems:
                # inputs: 0=video (chỉ lấy hình), 1=no_vocals (nhạc nền), 2=vocals (giọng gốc)
                inputs.extend(["-i", stems["no_vocals"], "-i", stems["vocals"]])
            inputs.extend(["-i", master_tts_path])
            tts_input_idx = 3 if stems else 1

            filter_complex = []
            
            # Format master audio to 44.1kHz stereo and boost voice volume
            filter_complex.append(
                f"[{tts_input_idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=1.5[tts_formatted]"
            )

            # Hai mức âm lượng tiếng GỐC độc lập (giống chế độ lồng tiếng):
            #  - ambient_vol : âm gốc KHI KHÔNG có thoại AI  (meta["bg_volume"], mặc định 0.50)
            #  - duck_vol    : âm gốc KHI có thoại AI (hạ nền) (meta["voice_bg_volume"], mặc định 0.12)
            # Dùng biểu thức volume động: nền = ambient_vol, trong mỗi khoảng AI nói
            # thì cộng (duck_vol - ambient_vol) để hạ về mức duck_vol.
            ambient_vol = float(meta.get("bg_volume", 0.0))
            duck_vol = float(meta.get("voice_bg_volume", 0.0))
            diff = duck_vol - ambient_vol

            # Gộp các khoảng duck chồng lấn/kề nhau: mỗi thời điểm chỉ bị trừ ĐÚNG
            # MỘT lần. Nếu để chồng lấn, các between() cộng dồn (ambient + 2*diff)
            # có thể đẩy hệ số volume xuống ÂM -> đảo pha/méo tiếng, không phải im.
            merged_windows = []
            for w in sorted(duck_windows):
                if merged_windows and w[0] <= merged_windows[-1][1]:
                    merged_windows[-1][1] = max(merged_windows[-1][1], w[1])
                else:
                    merged_windows.append([w[0], w[1]])

            if stems:
                # Chế độ TÁCH: 2 kênh riêng, mỗi kênh 1 biểu thức volume động.
                #  - nhạc nền (no_vocals, input 1): bg_volume -> voice_bg_volume
                #  - giọng gốc (vocals, input 2)  : vocals_volume -> voice_vocals_volume
                vocals_out = float(meta.get("vocals_volume", 0.0))
                vocals_duck = float(meta.get("voice_vocals_volume", 0.0))
                v_diff = vocals_duck - vocals_out

                music_expr = f"{ambient_vol}"
                vocals_expr = f"{vocals_out}"
                for (w_start, w_end) in merged_windows:
                    if abs(diff) > 0.001:
                        music_expr += f"+({diff}*between(t,{w_start:.3f},{w_end:.3f}))"
                    if abs(v_diff) > 0.001:
                        vocals_expr += f"+({v_diff}*between(t,{w_start:.3f},{w_end:.3f}))"

                filter_complex.append(
                    f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                    f"volume='{music_expr}':eval=frame[bg_music]"
                )
                filter_complex.append(
                    f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                    f"volume='{vocals_expr}':eval=frame[bg_voc]"
                )
                all_inputs = ["[bg_music]", "[bg_voc]", "[tts_formatted]"]
            else:
                vol_expr = f"{ambient_vol}"
                if abs(diff) > 0.001:
                    for (w_start, w_end) in merged_windows:
                        vol_expr += f"+({diff}*between(t,{w_start:.3f},{w_end:.3f}))"
                # Chuẩn hoá tiếng gốc về 44.1kHz/stereo trước khi ducking + amix.
                filter_complex.append(
                    f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                    f"volume='{vol_expr}':eval=frame[bg]"
                )

                all_inputs = ["[bg]", "[tts_formatted]"]
            filter_complex.append(
                f"{''.join(all_inputs)}amix=inputs={len(all_inputs)}:duration=first:dropout_transition=0:normalize=0,"
                f"dynaudnorm=f=500:g=15:p=0.95:m=10,"
                f"alimiter=limit=1:level_in=1.25:level_out=1[aout]"
            )

            filter_script_path = os.path.join(project_dir, "filter_complex_overlay.txt")
            with open(filter_script_path, "w", encoding="utf-8") as f:
                f.write(";\n".join(filter_complex))

            cmd = (
                [FFMPEG_EXE, "-y"]
                + inputs
                + [
                    "-filter_complex_script", filter_script_path,
                    "-map", "0:v",
                    "-map", "[aout]",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    tmp_output
                ]
            )

            logger.info(L("Đang ghép giọng đọc và phụ đề vào video...", "Merging voice and subtitles into the video..."))
            self._raise_if_cancelled(project_id)
            # Popen + poll: bấm Hủy dừng ffmpeg NGAY (video dài encode rất lâu).
            try:
                returncode, stderr_text = self._run_ffmpeg_cancellable(cmd, project_id=project_id)
                if returncode == 0 and os.path.exists(tmp_output) and os.path.getsize(tmp_output) > 0:
                    safe_replace(tmp_output, final_output)
            finally:
                if os.path.exists(tmp_output):
                    safe_remove(tmp_output)
                if os.path.exists(filter_script_path):
                    safe_remove(filter_script_path)
                if os.path.exists(master_tts_path):
                    safe_remove(master_tts_path)

            if returncode != 0:
                logger.error(L(f"Ghép video review thất bại: {stderr_text[-2000:]}", f"Review video merge failed: {stderr_text[-2000:]}"))
                return False

        # 3. Generate SRT Subtitles
        try:
            srt_path = os.path.join(project_dir, "review_subtitles.srt")
            self._generate_srt_overlay(b_chunks, a_chunks_lookup, srt_path)
            logger.info(L("Đã tạo xong tệp phụ đề (.srt).", "Subtitle file (.srt) created."))
        except Exception as e:
            logger.error(L(f"Tạo phụ đề thất bại: {e}", f"Subtitle creation failed: {e}"))

        logger.info(L(f"=== XUẤT VIDEO REVIEW HOÀN TẤT: {final_output} ===", f"=== REVIEW VIDEO EXPORT COMPLETE: {final_output} ==="))

        # Save updated b_chunks (with is_trimmed flags)
        b_chunks_path = os.path.join(project_dir, "b_chunks.json")
        with open(b_chunks_path, "w", encoding="utf-8") as f:
            json.dump(b_chunks, f, indent=2, ensure_ascii=False)

        # Nếu user đã hủy trong lúc ghép — không ghi đè rollback status.
        self._raise_if_cancelled(project_id)

        # Update metadata — render xong thì idle (không giữ render_started=True)
        meta["state"]["render_complete"] = True
        meta["state"]["render_started"] = False
        meta["state"].pop("render_error", None)
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        return final_output

    @staticmethod
    def _rate_multiplier(rate_str) -> float:
        """Đổi chuỗi rate ('+20%', '-10%', '+0%', 'auto'/None) -> hệ số nhân tốc độ."""
        r = (str(rate_str) if rate_str is not None else "").strip().lower()
        if not r or r == "auto":
            return 1.0
        r = r.replace("%", "").strip()
        try:
            if r.startswith("+"):
                return round(1.0 + float(r[1:]) / 100.0, 4)
            if r.startswith("-"):
                return round(1.0 - float(r[1:]) / 100.0, 4)
            return round(1.0 + float(r) / 100.0, 4) if r else 1.0
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _smart_fixed_rate(default_rate) -> str:
        """Tốc độ CỐ ĐỊNH dùng cho smart shift. 'auto'/trống -> '+0%' (1x); còn lại giữ
        nguyên chuỗi rate người dùng chọn (vd '+10%')."""
        r = (str(default_rate) if default_rate is not None else "").strip()
        if not r or r == "auto":
            return "+0%"
        return r

    def _atempo_file(self, src_path: str, out_path: str, factor: float, max_attempts: int = 2) -> bool:
        """Tăng/giảm tốc audio bằng ffmpeg atempo (giữ cao độ). factor kẹp 0.5..2.0.
        Tự retry khi lỗi (file bị khóa/tạm thời) — thất bại hẳn thì trả False để caller
        fallback dùng file gốc 1x thay vì im lặng mất câu."""
        factor = max(0.5, min(2.0, float(factor)))
        cmd = [
            FFMPEG_EXE, "-y", "-i", src_path,
            "-filter:a", f"atempo={factor:.4f}", out_path,
        ]
        for attempt in range(1, max_attempts + 1):
            res = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", errors="ignore", close_fds=True,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return True
            if attempt < max_attempts:
                logger.warning(
                    L(f"atempo lỗi (lần {attempt}/{max_attempts}) cho {os.path.basename(src_path)}, thử lại...", f"atempo error (attempt {attempt}/{max_attempts}) for {os.path.basename(src_path)}, retrying...")
                )
                import time as _time
                _time.sleep(0.5 * attempt)
            else:
                logger.error(
                    L(f"atempo THẤT BẠI sau {max_attempts} lần cho {src_path}: "
                    f"{(res.stderr or '')[-300:]}", f"atempo FAILED after {max_attempts} attempts for {src_path}: {(res.stderr or '')[-300:]}")
                )
        return False

    def _apply_smart_shift(self, b_chunks, a_lookup, output_dir, video_duration, gap=0.1, base_speed=1.0):
        """Dồn thời điểm các câu B để giọng đọc (tốc độ cố định) KHÔNG chồng nhau, ghi
        shift_start/shift_end vào từng b_data. Nếu tổng dài hơn video -> tăng tốc TOÀN BỘ
        giọng (atempo đồng đều) vừa đủ khít video. GAP = khoảng nghỉ nhỏ giữa 2 câu.

        shift_end sau khi xếp được GAP-FILL: kéo dài tới sát shift_start câu kế tiếp
        để SRT/compositor không bị khoảng trống phụ đề giữa 2 câu (voice/duck vẫn theo
        thời lượng audio thật, không bị ảnh hưởng)."""
        import concurrent.futures as cf

        candidates = []
        for b in b_chunks:
            ns, _ne = resolve_b_time(b, a_lookup)  # shift đã bị xoá -> mốc natural
            if ns is None:
                continue
            p = os.path.join(output_dir, f"B{b['b_id']}", "audio_tts.wav")
            if not os.path.exists(p) or os.path.getsize(p) == 0:
                continue
            candidates.append({"b": b, "ns": float(ns), "path": p})
        if not candidates:
            return

        # Probe duration SONG SONG — mỗi lần probe là 1 lần spawn ffmpeg; chạy tuần tự
        # với hàng trăm câu rất chậm (nhất là trên đĩa OneDrive).
        probe_workers = min(16, max(2, (os.cpu_count() or 4)))
        with cf.ThreadPoolExecutor(max_workers=probe_workers) as pool:
            durs = list(pool.map(lambda it: self._get_duration(it["path"]), candidates))

        items = []
        for it, dur in zip(candidates, durs):
            if not dur or dur <= 0:
                logger.warning(
                    L(f"Câu {it['b']['b_id']}: không đọc được thời lượng audio, bỏ qua khi xê dịch.", f"Sentence {it['b']['b_id']}: could not read the audio duration, skipped when shifting.")
                )
                continue
            it["dur"] = float(dur)
            items.append(it)
        if not items:
            return
        items.sort(key=lambda x: x["ns"])

        # Tăng tốc CỤC BỘ có giới hạn để chống TRÔI TÍCH LŨY ("cảnh qua lâu mới đọc
        # tới"): khi một câu đọc dài hơn khoảng đến cảnh kế, nó đẩy các câu sau trễ
        # dần. Ta cho phép rút ngắn (atempo) TỪNG câu — chỉ khi độ trễ vượt MAX_LAG.
        #
        # GIỚI HẠN THEO TỔNG TỐC ĐỘ (không phải hệ số chồng): atempo ở đây áp THÊM lên
        # audio đã sinh ở base_speed (tốc độ bạn chọn). Nếu chỉ chặn hệ số chồng ở 1.4
        # thì base cao (vd +50% = 1.5x) sẽ thành 1.5×1.4 = 2.1x (quá nhanh, méo giọng).
        # Vậy trần TỔNG = max(base_speed, COMFORT_TOTAL): giọng auto (1.0x) được tăng
        # tối đa tới 1.4x; nếu bạn đã chọn nhanh sẵn (1.2/1.5x) thì trần tổng đúng bằng
        # lựa chọn đó và hệ số chồng thu hẹp lại (thậm chí ~1.0 khi base ≥ 1.4).
        COMFORT_TOTAL = 1.4                       # tổng tốc độ "thoải mái" cho giọng auto
        total_ceiling = max(base_speed, COMFORT_TOTAL)
        MAX_LOCAL_SPEED = max(1.0, total_ceiling / max(0.1, base_speed))  # trần hệ số CHỒNG
        MAX_LAG = 3.0          # độ trễ tối đa so với mốc cảnh trước khi ép tăng tốc
        n = len(items)
        next_ns_list = [items[i + 1]["ns"] if i + 1 < n else None for i in range(n)]

        def _local_desired(start, dur1, next_ns):
            """Hệ số tăng tốc cục bộ CẦN (CHƯA kẹp trần) để câu này không đẩy câu sau
            trễ quá MAX_LAG. >1 nghĩa là câu đang dài hơn khoảng cảnh của nó."""
            if next_ns is None:
                return 1.0
            lag_next = (start + dur1 + gap) - next_ns
            if lag_next <= MAX_LAG:
                return 1.0
            desired = (next_ns + MAX_LAG - gap) - start
            if desired <= 0.2:
                return 99.0  # cực ngắn -> cần nén rất mạnh (chắc chắn vượt trần)
            return dur1 / desired

        def place(global_extra):
            """Xếp tuần tự; factor mỗi câu = max(cục bộ chống-trôi kẹp trần, global).
            Ghi kèm 'raw' = hệ số cục bộ CẦN chưa kẹp (để biết câu nào vượt trần)."""
            cursor = 0.0
            out = []
            for i, it in enumerate(items):
                start = max(it["ns"], cursor)
                raw = _local_desired(start, it["dur"], next_ns_list[i])
                lf = min(MAX_LOCAL_SPEED, raw)
                f = max(lf, global_extra)
                end = start + it["dur"] / f
                cursor = end + gap
                out.append((start, end, f, raw))
            return out

        placed3 = place(1.0)
        natural_end = placed3[-1][1]
        # Nếu SAU khi chống-trôi cục bộ vẫn dài hơn video -> thêm tăng tốc TOÀN CỤC
        # (nhị phân) vừa đủ khít video, chồng lên factor cục bộ.
        if video_duration and video_duration > 0 and natural_end > video_duration:
            if place(2.0)[-1][1] <= video_duration:
                lo, hi = 1.0, 2.0
                for _ in range(30):
                    mid = (lo + hi) / 2
                    if place(mid)[-1][1] <= video_duration:
                        hi = mid
                    else:
                        lo = mid
                placed3 = place(hi)
            else:
                placed3 = place(2.0)
            logger.info(
                L(f"Xê dịch SRT: tổng giọng ({natural_end:.1f}s) > video "
                f"({video_duration:.1f}s) -> tăng tốc toàn cục cho vừa.", f"SRT shift: total voice ({natural_end:.1f}s) > video ({video_duration:.1f}s) -> speeding up globally to fit.")
            )

        placed = [(s, e) for (s, e, _f, _r) in placed3]
        # Câu VƯỢT TRẦN: hệ số cục bộ CẦN > MAX_LOCAL_SPEED (nén tối đa vẫn không đủ,
        # vẫn lệch cảnh) HOẶC factor cuối > trần (bị nén toàn cục mạnh). Đây là các câu
        # QUÁ DÀI so với cảnh -> đánh dấu is_trimmed để banner nhắc người dùng RÚT NGẮN.
        overspeed_bids = []
        for it, (_s, _e, f, raw) in zip(items, placed3):
            it["factor"] = round(f, 4)
            over = (raw > MAX_LOCAL_SPEED + 0.02) or (f > MAX_LOCAL_SPEED + 0.02)
            if over:
                it["b"]["is_trimmed"] = True
                overspeed_bids.append(it["b"]["b_id"])

        n_sped = sum(1 for it in items if it["factor"] > 1.001)
        base_lbl = f"{base_speed:.2f}x" if base_speed > 1.001 else "1x"
        if n_sped:
            # Hiển thị theo TỔNG tốc độ (base × hệ số chồng) cho dễ hình dung.
            total_max = base_speed * max(it["factor"] for it in items)
            logger.info(
                L(f"Xê dịch SRT (tốc độ gốc {base_lbl}): {n_sped}/{n} câu được tăng tốc "
                f"để khớp cảnh (tổng tối đa x{total_max:.2f}); còn lại giữ nguyên {base_lbl}.", f"SRT shift (base speed {base_lbl}): {n_sped}/{n} sentences sped up to match the scene (total max x{total_max:.2f}); the rest stay at {base_lbl}.")
            )
        else:
            logger.info(L(f"Xê dịch SRT: mọi câu giữ nguyên tốc độ gốc {base_lbl}, khớp cảnh tốt.", f"SRT shift: every sentence keeps the base speed {base_lbl}, matching the scene well."))
        if overspeed_bids:
            logger.warning(
                L(f"Xê dịch SRT: {len(overspeed_bids)} câu QUÁ DÀI so với cảnh — kể cả khi "
                f"tăng tới tốc độ tối đa cho phép (x{total_ceiling:.2f} tổng) vẫn không khớp, "
                f"dễ tua nhanh/lệch hình. Hãy RÚT NGẮN các câu: "
                f"{', '.join(f'B{b}' for b in overspeed_bids)}.", f"SRT shift: {len(overspeed_bids)} sentences are TOO LONG for the scene — even at the maximum allowed speed (x{total_ceiling:.2f} total) they still do not fit, risking fast-forward/misalignment. SHORTEN these sentences: {', '.join(f'B{b}' for b in overspeed_bids)}.")
            )

        if n_sped:
            cancel_marker = os.path.join(os.path.dirname(output_dir), ".cancel_render")

            def _speedup_one(it):
                f_i = it.get("factor", 1.0)
                b = it["b"]
                if f_i <= 1.001:
                    b.pop("shift_audio", None)  # câu này giữ giọng gốc 1x
                    return
                if os.path.exists(cancel_marker):
                    return
                out = os.path.join(output_dir, f"B{b['b_id']}", "audio_shift.wav")
                factor_file = os.path.join(output_dir, f"B{b['b_id']}", "shift_factor.txt")

                # Check cache validity (theo factor RIÊNG của câu này).
                skip = False
                if os.path.exists(out) and os.path.getsize(out) > 0 and os.path.exists(factor_file):
                    try:
                        with open(factor_file, "r") as ff:
                            cached_f = float(ff.read().strip())
                        if abs(cached_f - f_i) < 0.0001 and os.path.getmtime(out) >= os.path.getmtime(it["path"]):
                            skip = True
                    except Exception:
                        pass

                if skip:
                    b["shift_audio"] = out
                    return

                if self._atempo_file(it["path"], out, f_i):
                    b["shift_audio"] = out
                    try:
                        with open(factor_file, "w") as ff:
                            ff.write(f"{f_i:.6f}")
                    except Exception:
                        pass
                else:
                    # Fallback: atempo hỏng -> dùng file gốc 1x (chiếm chỗ dài hơn
                    # dự kiến chút nhưng KHÔNG mất tiếng).
                    b.pop("shift_audio", None)
                    logger.warning(
                        L(f"Câu {b['b_id']}: tăng tốc thất bại, dùng giọng gốc 1x.", f"Sentence {b['b_id']}: speed-up failed, using the original voice at 1x.")
                    )

            workers = min(16, max(2, (os.cpu_count() or 4)))
            with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(_speedup_one, items))

        # Ghi mốc audio thật (dùng cho voice/duck) rồi GAP-FILL phần hiển thị:
        # shift_end kéo dài tới sát shift_start câu sau (trừ 0.05s) để phụ đề không
        # bị "hụt" — SRT/compositor đọc qua resolve_b_time nên tự khớp theo.
        for it, (start, end) in zip(items, placed):
            it["audio_end"] = end
            b = it["b"]
            b["shift_start"] = round(start, 3)
            b["shift_end"] = round(end, 3)

        for i, it in enumerate(items):
            b = it["b"]
            if i + 1 < len(items):
                next_start = items[i + 1]["b"]["shift_start"]
                display_end = max(it["audio_end"], next_start - 0.05)
            else:
                # Câu cuối: giữ tới hết audio (không có câu sau để lấp).
                display_end = it["audio_end"]
            if display_end > b["shift_end"]:
                b["shift_end"] = round(display_end, 3)

        # Validate: cảnh báo nếu vẫn còn chồng lấn (không được xảy ra theo cascade).
        for i in range(1, len(items)):
            prev_b, cur_b = items[i - 1]["b"], items[i]["b"]
            if prev_b["shift_end"] > cur_b["shift_start"] + 0.001:
                logger.warning(
                    L(f"Xê dịch SRT: câu {prev_b['b_id']} (end {prev_b['shift_end']}) chồng lên "
                    f"câu {cur_b['b_id']} (start {cur_b['shift_start']}) — tự cắt về sát câu sau.", f"SRT shift: sentence {prev_b['b_id']} (end {prev_b['shift_end']}) overlaps sentence {cur_b['b_id']} (start {cur_b['shift_start']}) — automatically cut to just before the next sentence.")
                )
                prev_b["shift_end"] = round(max(prev_b["shift_start"] + 0.1, cur_b["shift_start"] - 0.05), 3)

    def _generate_srt_overlay(self, b_chunks, a_chunks_lookup, output_path):
        def format_time(seconds):
            td_msec = int((seconds % 1) * 1000)
            td_sec = int(seconds)
            td_min, td_sec = divmod(td_sec, 60)
            td_hr, td_min = divmod(td_min, 60)
            return f"{td_hr:02d}:{td_min:02d}:{td_sec:02d},{td_msec:03d}"

        srt_lines = []
        for i, b_data in enumerate(b_chunks):
            start_time, end_time = resolve_b_time(b_data, a_chunks_lookup)
            if start_time is None:
                continue

            start_str = format_time(start_time)
            end_str = format_time(end_time)
            
            srt_lines.append(f"{i + 1}")
            srt_lines.append(f"{start_str} --> {end_str}")
            srt_lines.append(f"{b_data['b_text']}\n")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(srt_lines))
