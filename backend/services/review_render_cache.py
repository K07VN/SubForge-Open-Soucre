"""Cache fingerprint per B-clip — skip TTS khi re-render nếu nội dung không đổi.

Review chỉ còn overlay 100%: cache theo audio_tts (text, giọng, rate, source_a_ids).
"""
import copy
import hashlib
import json
import os
import threading

# v4: đổi ngữ nghĩa tốc độ đọc — "auto" = tự khớp thời lượng; "+X%" = cố định
# tuyệt đối (không auto tăng tốc/cắt). Bump để dự án cũ sinh lại voice đúng chế độ.
RENDER_CACHE_VERSION = 4

# Nhiều worker TTS (ThreadPoolExecutor) cùng remember_clip + save_render_cache trên
# CÙNG một dict cache -> phải khóa để tránh "dictionary changed size during iteration"
# (json.dump đang duyệt clips thì luồng khác thêm key). 1 khóa module-level là đủ vì
# mỗi lần render chỉ thao tác 1 cache.
_cache_lock = threading.RLock()


def _sha(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _source_ids(b_data: dict) -> list:
    try:
        return sorted(int(x) for x in b_data.get("source_a_ids", []))
    except (TypeError, ValueError):
        return []


def _num_or_none(v):
    """float(v) nếu là số hữu hạn > =0, ngược lại None (dùng cho override time)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f if f >= 0 else None


def resolve_b_time(b_data: dict, a_lookup: dict) -> tuple:
    """Tính (start, end) THẬT của 1 đoạn B trên timeline video gốc.

    Ưu tiên override thủ công (start_override / end_override) do người dùng chỉnh ở
    màn Review; nếu không có thì suy từ các A-chunk trong source_a_ids
    (start = min A.start, end = max A.end).

    Trả về ``(start, end)`` — hoặc ``(None, None)`` khi không đủ dữ liệu.
    Đây là NGUỒN SỰ THẬT DUY NHẤT về thời điểm đặt voice / SRT / compositor để mọi
    nơi nhất quán (voice không còn render ở vị trí gốc sau khi sửa time).
    """
    # Ưu tiên CAO NHẤT: thời gian đã "xê dịch thông minh" (smart SRT shift) — review_engine
    # tính lại lúc render để giọng đọc 1x không bị chồng. Có thì mọi nơi (voice/SRT/compositor)
    # dùng chung 1 nguồn -> nhất quán. KHÔNG có thì rơi về override thủ công / suy từ A.
    ss = _num_or_none(b_data.get("shift_start"))
    se = _num_or_none(b_data.get("shift_end"))
    if ss is not None and se is not None:
        return (ss, se if se > ss else ss + 0.1)

    so = _num_or_none(b_data.get("start_override"))
    eo = _num_or_none(b_data.get("end_override"))

    src = _source_ids(b_data)
    a_first = a_lookup.get(str(min(src))) if src else None
    a_last = a_lookup.get(str(max(src))) if src else None

    start = so
    if start is None and a_first is not None:
        start = _num_or_none(a_first.get("start"))
    end = eo
    if end is None and a_last is not None:
        end = _num_or_none(a_last.get("end"))

    if start is None or end is None:
        return (None, None)
    if end <= start:
        # Override lỗi / A trùng mốc -> cho tối thiểu 0.1s để không chia 0 khi ép TTS.
        end = start + 0.1
    return (start, end)


def compute_tts_fingerprint(meta: dict, b_data: dict) -> str:
    """Hash chỉ những gì ảnh hưởng audio TTS (không gồm volume mix).

    KHÔNG gồm override start/end (và duration suy ra): sửa time B chỉ DỜI vị trí voice
    (qua resolve_b_time), KHÔNG sinh lại giọng. Bỏ cơ chế tự sinh lại theo time để mọi
    giọng giữ nguyên tốc độ đã sinh đều — không bị ép/co lại khi rút ngắn khoảng thời gian.
    """
    smart_shift = bool(meta.get("smart_srt_shift", True))
    d = {
        "v": RENDER_CACHE_VERSION,
        "kind": "tts",
        "mode": "overlay_100",
        "b_text": (b_data.get("b_text") or b_data.get("text") or b_data.get("target_text") or b_data.get("content") or "").strip(),
        "source_a_ids": _source_ids(b_data),
        # Time override KHÔNG vào fingerprint ở cả 2 chế độ: đổi time không làm cache miss
        # -> voice không sinh lại, chỉ được đặt lại vị trí. Giữ giọng đồng đều như lúc sinh.
        "start_override": None,
        "end_override": None,
        "rate": b_data.get("rate"),
        "default_rate": meta.get("default_rate"),
        # Xê dịch SRT thông minh: sinh giọng ở tốc độ CỐ ĐỊNH, KHÔNG cắt/ép -> audio khác
        # hẳn chế độ thường -> phải vào fingerprint để cache không dùng nhầm file cũ.
        "smart_srt_shift": smart_shift,
        # Không đưa duration vào fingerprint: tránh sinh lại khi khoảng thời gian đổi.
        "duration_seconds": None,
        "voice": b_data.get("voice"),
        "target_language": b_data.get("target_language"),
        "target_voice": meta.get("target_voice"),
    }
    #  Giọng Qwen3-TTS: thêm phiên bản cách sinh + vân tay mẫu clone. CHỈ khi là Qwen
    #  (khoá mới trong dict) — Edge/VieNeu giữ nguyên fingerprint cũ từng byte.
    from services.tts_engines import engine_cache_tag

    tag = engine_cache_tag(meta.get("target_voice"))
    if tag:
        d["engine"] = tag
    return _sha(d)


def compute_mix_fingerprint(meta: dict, b_data: dict) -> str:
    """Hash cho mix/volume — gồm TTS + volume / tách stems."""
    return _sha({
        "v": RENDER_CACHE_VERSION,
        "kind": "mix",
        "tts": compute_tts_fingerprint(meta, b_data),
        "bg_volume": meta.get("bg_volume", 0.0),
        "voice_bg_volume": meta.get("voice_bg_volume", 0.0),
        "separate_audio": bool(meta.get("separate_audio", False)),
        "vocals_volume": meta.get("vocals_volume", 0.0),
        "voice_vocals_volume": meta.get("voice_vocals_volume", 0.0),
    })


def compute_clip_fingerprint(meta: dict, b_data: dict) -> str:
    """Alias tương thích — fingerprint lớp clip/mix đầy đủ."""
    return compute_mix_fingerprint(meta, b_data)


def _cache_path(project_dir: str) -> str:
    return os.path.join(project_dir, "output", "render_cache.json")


def load_render_cache(project_dir: str) -> dict:
    path = _cache_path(project_dir)
    if not os.path.exists(path):
        return {"version": RENDER_CACHE_VERSION, "clips": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("clips"), dict):
            return {"version": RENDER_CACHE_VERSION, "clips": {}}
        data["version"] = RENDER_CACHE_VERSION
        return data
    except Exception:
        return {"version": RENDER_CACHE_VERSION, "clips": {}}


# Mốc lần ghi gần nhất theo project — dùng cho throttle (ghi mỗi câu xong trên
# OneDrive rất chậm: mỗi lần là deepcopy + full JSON write + sync).
_last_save_at: dict = {}


def save_render_cache(project_dir: str, cache: dict, throttle_s: float = 0.0) -> None:
    """Ghi cache ra đĩa. throttle_s > 0: bỏ qua nếu vừa ghi trong throttle_s giây
    gần đây (dùng cho lưu-tăng-dần mỗi câu; lần lưu CHỐT cuối luôn gọi không throttle)."""
    if throttle_s > 0:
        import time
        now = time.monotonic()
        with _cache_lock:
            if now - _last_save_at.get(project_dir, 0.0) < throttle_s:
                return
            _last_save_at[project_dir] = now
    os.makedirs(os.path.join(project_dir, "output"), exist_ok=True)
    # Dump từ BẢN SAO chụp dưới khóa: json.dump có thể mất vài ms, nếu duyệt trực
    # tiếp cache trong khi worker khác remember_clip() sẽ vỡ "dict changed size".
    with _cache_lock:
        cache["version"] = RENDER_CACHE_VERSION
        snapshot = copy.deepcopy(cache)
    with open(_cache_path(project_dir), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


def clear_render_cache(project_dir: str) -> None:
    path = _cache_path(project_dir)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _clip_entry(cache: dict, b_id: int) -> dict:
    """Chuẩn hoá entry: v1 lưu string → coi như chỉ có lớp clip cũ."""
    raw = (cache.get("clips") or {}).get(str(b_id))
    if isinstance(raw, dict):
        return {"tts": raw.get("tts"), "clip": raw.get("clip")}
    if isinstance(raw, str):
        return {"tts": None, "clip": raw}
    return {"tts": None, "clip": None}


def try_reuse_clip(
    b_id: int,
    fingerprint: str,
    output_dir: str,
    cache: dict,
    artifact: str = "final_clip.mp4",
    layer: str = "clip",
) -> tuple[bool, str | None]:
    """Trả về (hit, path) nếu artifact còn hợp lệ và fingerprint lớp ``layer`` khớp.

    layer: \"tts\" (audio_tts.wav) | \"clip\" (final_clip.mp4)
    """
    entry = _clip_entry(cache, b_id)
    if entry.get(layer) != fingerprint:
        return False, None
    clip_path = os.path.join(output_dir, f"B{b_id}", artifact)
    if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
        return True, clip_path
    return False, None


def remember_clip(
    cache: dict,
    b_id: int,
    fingerprint: str,
    layer: str = "clip",
) -> None:
    # Khóa vì nhiều worker TTS ghi cùng lúc (đọc-sửa-ghi cache["clips"]).
    with _cache_lock:
        if "clips" not in cache:
            cache["clips"] = {}
        entry = _clip_entry(cache, b_id)
        entry[layer] = fingerprint
        cache["clips"][str(b_id)] = entry
