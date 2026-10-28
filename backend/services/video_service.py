from utils.i18n import L
import os
import subprocess
import concurrent.futures
import logging
import imageio_ffmpeg
from utils.hardware_utils import get_best_device

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger("VideoService")


class VideoServiceError(RuntimeError):
    pass


def _ffmpeg_err_tail(stderr: str, max_chars: int = 1200) -> str:
    """Lấy PHẦN CUỐI stderr của ffmpeg — nguyên nhân lỗi thật nằm ở các dòng cuối.

    Log 2.2.0 gửi nguyên văn từ đầu (banner version + configuration dài cả trang)
    nên bị cắt trần TRƯỚC khi tới dòng lỗi → không chẩn đoán được. Cắt lấy đuôi
    để error_log gửi lên server luôn chứa dòng lỗi thật.
    """
    s = (stderr or "").strip()
    if len(s) <= max_chars:
        return s
    return L("…(đã lược phần đầu)…\n", "…(beginning omitted)…") + s[-max_chars:]


class VideoMergeCancelled(Exception):
    """Người dùng bấm Hủy giữa lúc ghép — không coi là lỗi."""
    pass


class VideoService:
    @staticmethod
    def _build_render_command(
        input_path: str, output_path: str, v_filters: list, use_gpu: bool
    ):
        cmd = [FFMPEG_EXE, "-y"]

        if use_gpu:
            cmd.extend(["-hwaccel", "cuda"])

        cmd.extend(["-i", input_path])

        if v_filters:
            cmd.extend(["-vf", ",".join(v_filters)])

        crf = "25" if v_filters else "23"

        if use_gpu:
            cmd.extend(
                [
                    "-c:v",
                    "h264_nvenc",
                    "-preset",
                    "fast",
                    "-rc",
                    "vbr",
                    "-cq",
                    crf,
                    "-c:a",
                    "copy",
                    output_path,
                ]
            )
        else:
            cmd.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    crf,
                    "-c:a",
                    "copy",
                    output_path,
                ]
            )

        return cmd

    @staticmethod
    def extract_audio(video_path: str, output_audio_path: str):
        """Extract audio from video using FFmpeg directly via subprocess."""
        video_path = os.path.abspath(video_path)
        output_audio_path = os.path.abspath(output_audio_path)

        if os.path.exists(output_audio_path):
            try:
                os.remove(output_audio_path)
            except:
                pass

        try:
            # Simple direct command: ffmpeg -i input -acodec pcm_s16le -ac 1 -ar 16000 output.wav
            cmd = [
                FFMPEG_EXE,
                "-y",  # overwrite
                "-i",
                video_path,
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "16000",
                output_audio_path,
            ]

            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                close_fds=True,
            )

            if result.returncode == 0:
                logger.info(L(f"Đã tách âm thanh xong: {output_audio_path}", f"Audio extraction done: {output_audio_path}"))
                return True
            else:
                logger.error(L(f"Tách âm thanh thất bại: {_ffmpeg_err_tail(result.stderr)}", f"Audio extraction failed: {_ffmpeg_err_tail(result.stderr)}"))
                return False

        except Exception as e:
            logger.exception(L(f"Lỗi tách âm thanh không mong muốn cho {video_path}", f"Unexpected audio extraction error for {video_path}"))
            return False

    @staticmethod
    def extract_audio_slice(
        video_path: str, output_audio_path: str, start_time: float, duration: float
    ):
        """Extract a specific slice of audio from video."""
        video_path = os.path.abspath(video_path)
        output_audio_path = os.path.abspath(output_audio_path)

        try:
            cmd = [
                FFMPEG_EXE,
                "-y",
                "-ss",
                str(start_time),
                "-t",
                str(duration),
                "-i",
                video_path,
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "16000",
                output_audio_path,
            ]
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                close_fds=True,
            )
            if result.returncode != 0:
                logger.error(L(f"Tách đoạn âm thanh thất bại: {_ffmpeg_err_tail(result.stderr)}", f"Audio segment extraction failed: {_ffmpeg_err_tail(result.stderr)}"))
            return result.returncode == 0
        except Exception as e:
            logger.exception(L(f"Lỗi tách đoạn âm thanh không mong muốn: {e}", f"Unexpected audio segment extraction error: {e}"))
            return False

    @staticmethod
    def _run_ffmpeg(cmd, check_cancel=None, cleanup_path=None):
        """Chạy ffmpeg; nếu có check_cancel thì poll 0.5s để HỦY được giữa chừng.
        Hủy: kill tiến trình + dọn file output dở dang, raise VideoMergeCancelled."""
        if check_cancel is None:
            return subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                close_fds=True,
            )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if check_cancel():
                    proc.kill()
                    try:
                        proc.communicate(timeout=5)
                    except Exception:
                        pass
                    if cleanup_path and os.path.exists(cleanup_path):
                        try:
                            os.remove(cleanup_path)
                        except OSError:
                            pass
                    raise VideoMergeCancelled()
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            (out or b"").decode("utf-8", errors="replace"),
            (err or b"").decode("utf-8", errors="replace"),
        )

    @staticmethod
    def merge_audio_video(
        video_path: str,
        segments_audio_dir: str,
        segment_data: list,
        output_video_path: str,
        bg_volume: float = 1.0,
        voice_bg_volume: float = 1.0,
        separate_audio: bool = False,
        stems: dict = None,
        vocals_volume: float = 0.0,
        voice_vocals_volume: float = 0.0,
        check_cancel=None,
        tts_volume: float = 1.5,
    ):
        """
        Merge segment-based audio files into video using a Chunked Merging strategy.
        ``tts_volume``: he so khuech dai GIONG DOC AI (mac dinh 1,5 — con so gan cung tu truoc; nay la cot videos.tts_volume).
        Now supports parallel batch processing for better performance.

        separate_audio=True: audio goc da duoc tach thanh 2 stem (xem
        SeparationService), `stems` = {'vocals': path, 'no_vocals': path}.
        Khi do bg_volume/voice_bg_volume chi ap len kenh NHAC NEN (no_vocals),
        con vocals_volume/voice_vocals_volume ap len kenh GIONG GOC (vocals).
        """
        video_path = os.path.normpath(os.path.abspath(video_path))
        output_video_path = os.path.normpath(os.path.abspath(output_video_path))
        output_dir = os.path.dirname(output_video_path)
        project_root = os.path.dirname(output_dir)
        temp_dir = os.path.join(project_root, "temp")

        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(temp_dir, exist_ok=True)

        valid_segments = [
            s
            for s in segment_data
            if s.get("audio_path")
            and os.path.exists(s["audio_path"])
            and os.path.getsize(s["audio_path"]) > 0
        ]
        if not valid_segments:
            logger.warning(
                L(f"Không tìm thấy đoạn âm thanh hợp lệ nào để ghép vào: {output_video_path}", f"No valid audio segment found to merge into: {output_video_path}")
            )
            return False

        temp_files = []
        try:
            video_duration = VideoService.get_video_duration(video_path)
            if not video_duration or video_duration <= 0:
                raise VideoServiceError(
                    L("Không đọc được thời lượng video nguồn (duration = 0). "
                    "File video có thể bị hỏng hoặc thiếu thông tin thời lượng — vui lòng kiểm tra lại video gốc.", "Could not read the source video duration (duration = 0). The video file may be corrupted or missing duration info — please check the original video.")
                )

            # STAGE 1: PARALLEL BATCH PROCESSING
            chunk_size = 50
            chunks = [
                valid_segments[i : i + chunk_size]
                for i in range(0, len(valid_segments), chunk_size)
            ]

            # Mỗi lô chỉ render trong KHOẢNG THỜI GIAN của chính nó [batch_start,
            # batch_end] (thay vì cả video) — tránh video dài (10h) sinh mỗi lô
            # một WAV khổng lồ (giới hạn WAV 4GB + tốn hàng trăm GB temp).
            # Khi trộn vào master sẽ adelay bù lại đúng offset batch_start.
            chunk_starts = []
            chunk_spans = []
            for chunk in chunks:
                c_start = min(s["start_time"] for s in chunk)
                c_end = max(s["end_time"] for s in chunk)
                chunk_starts.append(max(0.0, c_start))
                chunk_spans.append(max(1.0, c_end - max(0.0, c_start)))

            device, _ = get_best_device()
            max_workers = 4 if device == "cuda" else 2

            logger.info(
                L(f"Bước 1/3: Trộn giọng đọc theo dòng thời gian (Phân chia: {len(chunks)} lô cho {len(valid_segments)} câu lồng tiếng)...", f"Step 1/3: Mixing the voice along the timeline (split: {len(chunks)} batches for {len(valid_segments)} dubbed sentences)...")
            )

            batch_files = []
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers
            ) as executor:
                future_to_batch = {
                    executor.submit(
                        VideoService._process_audio_batch,
                        chunk,
                        os.path.join(temp_dir, f"voice_batch_{i}.wav"),
                        chunk_spans[i],
                        i,
                        chunk_starts[i],
                        check_cancel,
                    ): i
                    for i, chunk in enumerate(chunks)
                }

                completed_batches = 0
                for future in concurrent.futures.as_completed(future_to_batch):
                    b_idx = future_to_batch[future]
                    success = future.result()
                    if not success:
                        raise VideoServiceError(L(f"Xử lý lô {b_idx} thất bại", f"Processing batch {b_idx} failed"))
                    completed_batches += 1
                    logger.info(L(f"  └─ Tiến trình ghép: Đã xử lý xong lô {completed_batches}/{len(chunks)}", f"  └─ Merge progress: finished batch {completed_batches}/{len(chunks)}"))

            # Giữ đúng thứ tự lô để khớp offset batch_start khi trộn master.
            batch_files = [
                os.path.join(temp_dir, f"voice_batch_{i}.wav")
                for i in range(len(chunks))
            ]
            temp_files.extend(batch_files)

            # STAGE 2: MERGE BATCHES INTO VOICE MASTER
            voice_master_path = os.path.join(output_dir, "dubbed_voice.wav")

            # Input 0 = nền im lặng dài bằng cả video; mỗi lô adelay bù offset
            # batch_start của chính nó (lô chỉ chứa khoảng thời gian của lô).
            inputs_master = [
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=44100:cl=stereo:d={video_duration}",
            ]
            for bf in batch_files:
                inputs_master.extend(["-i", bf])

            delay_parts = []
            for i, off in enumerate(chunk_starts):
                off_ms = int(off * 1000)
                delay_parts.append(f"[{i + 1}:a]adelay={off_ms}|{off_ms}[b{i}]")
            mix_master = "[0:a]" + "".join([f"[b{i}]" for i in range(len(batch_files))])
            # Không dynaudnorm ở đây: normalize sớm trên track thưa (nhiều silence)
            # làm méo động rồi final alimiter kéo cả mix xuống nhỏ hơn bản gốc.
            filter_master = (
                f"{';'.join(delay_parts)};"
                f"{mix_master}amix=inputs={len(batch_files) + 1}:dropout_transition=0:normalize=0,"
                f"alimiter=limit=1:level_in=1:level_out=1[out]"
            )

            cmd_master = (
                [FFMPEG_EXE]
                + inputs_master
                + [
                    "-filter_complex",
                    filter_master,
                    "-map",
                    "[out]",
                    "-acodec",
                    "pcm_s16le",
                    # RF64: cho phép WAV > 4GB (video rất dài) mà vẫn là WAV thường khi nhỏ.
                    "-rf64",
                    "auto",
                    voice_master_path,
                ]
            )

            logger.info(L("Bước 2/3: Tổng hợp các lô âm thanh thành tệp giọng đọc tổng hợp (voice master)...", "Step 2/3: Combining the audio batches into the voice master file..."))
            res_m = VideoService._run_ffmpeg(
                cmd_master, check_cancel=check_cancel, cleanup_path=voice_master_path
            )
            if res_m.returncode != 0:
                raise VideoServiceError(L(f"Tạo bản giọng tổng thất bại: {_ffmpeg_err_tail(res_m.stderr)}", f"Creating the voice master failed: {_ffmpeg_err_tail(res_m.stderr)}"))
            logger.info(L("  └─ Tổng hợp tệp giọng đọc hoàn tất.", "  └─ Voice master file complete."))

            logger.info(L("Bước 3/3: Trộn nhạc nền, giọng gốc và giọng lồng tiếng (áp dụng hiệu ứng giảm âm lượng tự động - Ducking)...", "Step 3/3: Mixing the background music, original voice and dubbed voice (applying automatic volume ducking)..."))
            ducking_script_path = os.path.join(temp_dir, "ducking.txt")
            temp_files.append(ducking_script_path)

            if separate_audio and stems:
                # Nhanh TACH NGUON: 2 kenh rieng (no_vocals = nhac nen, vocals =
                # giong goc), moi kenh 1 bieu thuc volume theo thoi gian.
                # Inputs: 0=video (chi lay hinh), 1=voice_master, 2=no_vocals, 3=vocals.
                with open(ducking_script_path, "w", encoding="utf-8") as f:
                    music_expr = f"{bg_volume}"
                    vocals_expr = f"{vocals_volume}"
                    v_diff = voice_vocals_volume - vocals_volume
                    for seg in valid_segments:
                        # per-segment override chi ap dung cho kenh nhac nen (nhu cu)
                        # Dùng `is not None` — `or` sẽ nuốt 0.0 (tắt nền) thành voice_bg_volume.
                        seg_bg = seg.get("bg_volume")
                        m_target = voice_bg_volume if seg_bg is None else float(seg_bg)
                        m_diff = m_target - bg_volume
                        if abs(m_diff) > 0.001:
                            music_expr += f"+({m_diff}*between(t,{seg['start_time']},{seg['end_time']}))"
                        if abs(v_diff) > 0.001:
                            vocals_expr += f"+({v_diff}*between(t,{seg['start_time']},{seg['end_time']}))"

                    f.write(
                        f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume='{music_expr}':eval=frame[music_low];\n"
                    )
                    f.write(
                        f"[3:a]aformat=sample_rates=44100:channel_layouts=stereo,volume='{vocals_expr}':eval=frame[voc_low];\n"
                    )
                    # dynaudnorm SAU amix để bù loudness sau khi cộng nhiều track
                    # (tránh alimiter level_out=0.95 kéo cả mix nhỏ hơn bản gốc).
                    f.write(
                        f"[1:a]volume={float(tts_volume):.3f}[voc_boost];\n"
                        "[music_low][voc_low][voc_boost]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,"
                        "dynaudnorm=f=500:g=15:p=0.95:m=10,"
                        "alimiter=limit=1:level_in=1.25:level_out=1[aout]"
                    )
            else:
                # Nhanh CU (giu nguyen tung byte): duck toan bo audio goc.
                with open(ducking_script_path, "w", encoding="utf-8") as f:
                    vol_expr = f"{bg_volume}"
                    for seg in valid_segments:
                        seg_bg = seg.get("bg_volume")
                        target_vol = voice_bg_volume if seg_bg is None else float(seg_bg)
                        diff = target_vol - bg_volume
                        if abs(diff) > 0.001:
                            vol_expr += f"+({diff}*between(t,{seg['start_time']},{seg['end_time']}))"

                    f.write(f"[0:a]volume='{vol_expr}':eval=frame[bg_low];\n")
                    f.write(
                        f"[1:a]volume={float(tts_volume):.3f}[voc_boost];\n"
                        "[bg_low][voc_boost]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                        "dynaudnorm=f=500:g=15:p=0.95:m=10,"
                        "alimiter=limit=1:level_in=1.25:level_out=1[aout]"
                    )

            cmd_final = [FFMPEG_EXE, "-y"]
            if device == "cuda":
                cmd_final.extend(["-hwaccel", "cuda"])

            cmd_final.extend(["-i", video_path, "-i", voice_master_path])
            if separate_audio and stems:
                cmd_final.extend(["-i", stems["no_vocals"], "-i", stems["vocals"]])
            cmd_final.extend(
                [
                    "-filter_complex_script",
                    ducking_script_path,
                    "-map",
                    "0:v",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    output_video_path,
                ]
            )

            import time
            t_merge_start = time.time()
            logger.info(L("Đang chạy lệnh FFmpeg trộn âm thanh & ghép vào video gốc (sử dụng sao chép trực tiếp luồng hình ảnh - c:v copy, sẽ mất khoảng 1-2 phút tùy độ dài video)...", "Running the FFmpeg command to mix the audio & merge into the original video (copying the video stream directly - c:v copy, takes about 1-2 minutes depending on video length)..."))
            res_f = VideoService._run_ffmpeg(
                cmd_final, check_cancel=check_cancel, cleanup_path=output_video_path
            )
            if res_f.returncode == 0:
                logger.info(L(f"=== GHÉP VIDEO THÀNH CÔNG: {output_video_path} (Thời gian xử lý: {time.time() - t_merge_start:.1f}s) ===", f"=== VIDEO MERGE SUCCESSFUL: {output_video_path} (Processing time: {time.time() - t_merge_start:.1f}s) ==="))
                return True
            else:
                raise VideoServiceError(L(f"Ghép hoàn tất thất bại: {_ffmpeg_err_tail(res_f.stderr)}", f"Final merge failed: {_ffmpeg_err_tail(res_f.stderr)}"))

        except VideoMergeCancelled:
            logger.info(L(f"Ghép video ĐÃ HỦY theo yêu cầu: {output_video_path}", f"Video merge CANCELLED at the user's request: {output_video_path}"))
            raise
        except Exception as e:
            logger.error(L(f"Ghép video thất bại cho {output_video_path}: {e}", f"Video merge failed for {output_video_path}: {e}"))
            raise e
        finally:
            for tf in temp_files:
                if os.path.exists(tf):
                    try:
                        os.remove(tf)
                    except:
                        pass

    @staticmethod
    def _process_audio_batch(
        chunk: list,
        batch_out: str,
        span_duration: float,
        b_idx: int,
        batch_start: float = 0.0,
        check_cancel=None,
    ) -> bool:
        """Process a single batch of audio segments.

        Lô chỉ render trong khoảng thời gian của chính nó (span_duration tính từ
        batch_start) — adelay TƯƠNG ĐỐI so với batch_start. Offset tuyệt đối được
        bù lại khi trộn master (tránh mỗi lô 1 WAV dài bằng cả video)."""
        try:
            inputs = [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=44100:cl=stereo:d={span_duration}",
            ]
            filter_parts = []

            input_counter = 1
            for seg in chunk:
                # Absolute path and verify existence one last time
                abs_path = os.path.normpath(os.path.abspath(seg["audio_path"]))
                if not os.path.exists(abs_path):
                    logger.warning(L(f"Thiếu tệp âm thanh khi xử lý lô: {abs_path}. Bỏ qua.", f"Audio file missing while processing the batch: {abs_path}. Skipping."))
                    continue

                inputs.extend(["-i", abs_path])
                delay = max(0, int((seg["start_time"] - batch_start) * 1000))
                filter_parts.append(
                    f"[{input_counter}:a]adelay={delay}|{delay},aformat=sample_rates=44100:channel_layouts=stereo[a{input_counter}]"
                )
                input_counter += 1

            if not filter_parts:
                # No valid audio in this batch, generate silence
                cmd_batch = [FFMPEG_EXE, "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={span_duration}", "-t", str(span_duration), "-rf64", "auto", batch_out]
            else:
                mix_inputs = "".join([f"[a{i+1}]" for i in range(len(filter_parts))])
                # Chỉ ghép TTS + silence timeline — không dynaudnorm sớm (gây nhỏ tiếng ở final).
                complex_filter = (
                    f"{';'.join(filter_parts)};"
                    f"[0:a]{mix_inputs}amix=inputs={len(filter_parts) + 1}:dropout_transition=0:normalize=0,"
                    f"alimiter=limit=1:level_in=1:level_out=1[out]"
                )

                cmd_batch = (
                    [FFMPEG_EXE, "-y"]
                    + inputs
                    + [
                        "-filter_complex",
                        complex_filter,
                        "-map",
                        "[out]",
                        "-acodec",
                        "pcm_s16le",
                        # RF64: cho phép WAV > 4GB nếu lô rất dài, vẫn WAV thường khi nhỏ.
                        "-rf64",
                        "auto",
                        batch_out,
                    ]
                )

            res_b = VideoService._run_ffmpeg(
                cmd_batch, check_cancel=check_cancel, cleanup_path=batch_out
            )
            if res_b.returncode != 0:
                logger.error(L(f"Lỗi FFmpeg ở lô {b_idx}: {_ffmpeg_err_tail(res_b.stderr)}", f"FFmpeg error in batch {b_idx}: {_ffmpeg_err_tail(res_b.stderr)}"))
            return res_b.returncode == 0
        except VideoMergeCancelled:
            raise
        except Exception:
            return False

    @staticmethod
    def render_final_export(
        input_path: str,
        output_path: str,
        aspect_ratio: str = "monitor",
        mirror: bool = False,
    ):
        """Render a master with visual FX: Aspect Ratio and Mirroring."""
        input_path = os.path.normpath(os.path.abspath(input_path))
        output_path = os.path.normpath(os.path.abspath(output_path))
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        v_filters = []
        if mirror:
            v_filters.append("hflip")

        if aspect_ratio == "phone":  # 9:16
            v_filters.append(
                "crop=w='min(iw,ih*9/16)':h=ih,scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2"
            )
        elif aspect_ratio == "tablet":  # 4:3
            v_filters.append(
                "crop=w='min(iw,ih*4/3)':h=ih,scale=1024:768:force_original_aspect_ratio=decrease,pad=1024:768:(ow-iw)/2:(oh-ih)/2"
            )
        elif aspect_ratio == "laptop":  # 16:10
            v_filters.append(
                "crop=w='min(iw,ih*16/10)':h=ih,scale=1280:800:force_original_aspect_ratio=decrease,pad=1280:800:(ow-iw)/2:(oh-ih)/2"
            )

        try:
            device, _ = get_best_device()
            use_gpu = device == "cuda"
            attempts = [use_gpu] if use_gpu is False else [True, False]
            last_error = ""

            for attempt_gpu in attempts:
                cmd = VideoService._build_render_command(
                    input_path, output_path, v_filters, attempt_gpu
                )
                logger.info(
                    L(f"Đang render xuất ({'GPU' if attempt_gpu else 'CPU'}) cho: {os.path.basename(output_path)}", f"Rendering the export ({'GPU' if attempt_gpu else 'CPU'}) for: {os.path.basename(output_path)}")
                )
                result = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    close_fds=True,
                )
                if (
                    result.returncode == 0
                    and os.path.exists(output_path)
                    and os.path.getsize(output_path) > 0
                ):
                    logger.info(L(f"XUẤT HOÀN TẤT: {os.path.basename(output_path)}", f"EXPORT COMPLETE: {os.path.basename(output_path)}"))
                    return True

                last_error = result.stderr.strip() or "Unknown FFmpeg error"
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except:
                        pass
                if attempt_gpu:
                    logger.warning(
                        L(f"Xuất bằng GPU thất bại, chuyển sang dùng CPU: {last_error}", f"GPU export failed, switching to CPU: {last_error}")
                    )

            logger.error(L(f"Render xuất thất bại: {last_error}", f"Export render failed: {last_error}"))
            return False
        except Exception as e:
            logger.exception(L(f"Lỗi render xuất không mong muốn: {e}", f"Unexpected export render error: {e}"))
            return False

    @staticmethod
    def get_video_duration(video_path: str):
        """Get video duration in seconds using ffmpeg -i (more reliable as ffprobe might be missing)."""
        import re

        try:
            cmd = [FFMPEG_EXE, "-i", video_path]
            # ffmpeg outputs info to stderr
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                close_fds=True,
            )

            # Look for "Duration: 00:00:00.00"
            match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", result.stderr)
            if match:
                h, m, s = match.groups()
                return int(h) * 3600 + int(m) * 60 + float(s)
            return 0
        except Exception:
            return 0
