"""Tạo kịch bản B TỰ ĐỘNG bằng DeepSeek cho CHẾ ĐỘ EDIT CHI TIẾT (nâng cao).

Trước đây chế độ chi tiết chỉ có đường THỦ CÔNG: copy prompt (``GET /prompt``) sang
ChatGPT/Claude rồi dán JSON về (``POST /import``). File này làm nốt phần còn lại —
gọi DeepSeek ngay trong app — cho CẢ HAI loại kịch bản:

  * ``dubbing`` — dịch sát nghĩa, MỘT câu A thành MỘT câu B.
  * ``review``  — viết lại thành lời bình MC, gộp nhiều câu A thành một câu B.

NGUYÊN TẮC: file này CHỈ THÊM MỚI.
  * Dùng lại ``deepseek_service`` ở mức NGUYÊN LIỆU (``_chat``, ``_parse_json_array``,
    ``DeepSeekError``, ``_google_fallback``) và ``review_advanced_prompts`` để dựng
    prompt — không sửa một dòng nào của hai đường lồng tiếng / review cơ bản đang
    chạy ổn. Đổi ở đây không thể làm hỏng bên đó.
  * KHÔNG tự nhập kịch bản: hàm ở đây chỉ TRẢ VỀ danh sách câu B. Việc nhập vào dự án
    vẫn đi đúng cửa cũ (``ADV.import_script`` / ``SEG.append_script``) nên mọi bảo
    đảm về uid / giọng đã sinh / vị trí đã kéo giữ nguyên.

BA LƯỢT CHẠY — vì sao không phải một lượt:
  1. Lượt CHÍNH: chia lô nhỏ, chạy song song (trần ``DEEPSEEK_CONCURRENCY``). Lô nào
     lỗi (API 5xx/429, JSON cụt vì chạm trần token) thì ĐỂ ĐÓ, không dừng cả tiến
     trình — các lô khác vẫn dùng được.
  2. Lượt VÉT: gom đúng những câu A còn thiếu, chia lô NHỎ HƠN (một nửa) rồi chạy lại
     một lượt. Lô nhỏ hơn là cách chữa gọn nhất cho nguyên nhân phổ biến nhất
     (output dài chạm ``max_tokens`` -> JSON đứt giữa -> mất cả lô).
  3. Lượt CỨU (CHỈ ``dubbing``): dịch số còn lại bằng Google. Với ``review`` thì
     KHÔNG có bước này — Google dịch được câu chứ không VIẾT LẠI được lời bình, cứu
     kiểu đó là chèn phụ đề dịch máy vào giữa kịch bản MC mà người dùng không hay.

Câu nào cả ba lượt đều không ra thì trả về trong ``missing_a_ids`` để giao diện nói
thẳng "còn N câu chưa có kịch bản, bấm chạy lại" — không im lặng bỏ sót.
"""
from utils.i18n import L
import concurrent.futures as cf
import hashlib
import json
import logging
import os
import time

from services import review_advanced_prompts as PROMPTS
from services.deepseek_service import (
    DEEPSEEK_CONCURRENCY,
    DeepSeekError,
    _chat,
    _parse_json_array,
)

logger = logging.getLogger("ReviewAdvanced")

#  SỐ CÂU MỖI LÔ: người dùng chọn ở Cài đặt → DeepSeek (``ai_batch_size``, mặc định
#  80; trước 25-09-2026 cố định 40 lồng tiếng / 50 review). Output ở đây là JSON Array
#  (kèm b_id / source_a_ids / speaker cho từng câu) — tốn token gấp mấy lần dòng
#  "[ID] text" của đường cơ bản — nên trần token và thời gian chờ NỚI THEO cỡ lô
#  (``_max_tokens`` / ``_timeout``), không thì lô to chạm trần giữa mảng -> JSON đứt.
FALLBACK_BATCH_SIZE = 80

#  Lượt vét chia lô nhỏ hơn bấy nhiêu lần, nhưng không nhỏ hơn ``MIN_BATCH`` (lô 2-3
#  câu thì số request tăng vọt mà chẳng lợi gì).
RETRY_SHRINK = 3
MIN_BATCH = 8

#  Trần token output cho mỗi lô: lô 40-50 câu ra ~3-5K token. Tối thiểu 8192, cộng
#  ~130 token mỗi câu cho lô to; trần 64K (model cho tới 384K, nhưng một lô viết tràn
#  tới mức đó là hỏng rồi, chờ thêm chỉ phí tiền).
MAX_TOKENS = 8192
TOKENS_PER_LINE = 130
MAX_TOKENS_CAP = 65536


def _max_tokens(n: int) -> int:
    return int(min(MAX_TOKENS_CAP, max(MAX_TOKENS, n * TOKENS_PER_LINE)))


def _timeout(n: int) -> int:
    """Chờ tối đa theo cỡ lô: lô 200 câu viết ~20K token, 180 s cố định là cắt oan."""
    return int(max(180, n * 3))


def _options() -> dict:
    """Tuỳ chọn ở Cài đặt → DeepSeek. Không đọc được -> hành vi an toàn (bật ngữ cảnh)."""
    try:
        from services.config_service import get_config_service

        return get_config_service().get_ai_script_options()
    except Exception as e:  # noqa: BLE001
        logger.warning(L(f"[chi tiết] Không đọc được tuỳ chọn AI ({e}) — dùng mặc định.", f"[advanced] Could not read the AI options ({e}) — using defaults."))
        return {"context_enabled": False, "context_model": "", "batch_size": FALLBACK_BATCH_SIZE}


class AdvancedAiError(Exception):
    """Không sinh được câu B nào — kèm thông báo thân thiện để hiện thẳng lên UI."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


# --------------------------------------------------------------------------- #
#  Bước 0 — đọc ngữ cảnh cả phim
# --------------------------------------------------------------------------- #
#: Đổi cách dựng / nội dung prompt bước 0 thì TĂNG số này để bản ghi chú cũ đã lưu
#: không bị dùng lại.
CONTEXT_REV = 1
CONTEXT_FILE = "ai_context.json"
CONTEXT_MAX_TOKENS = 4096
#: Phân tích chứ không sáng tác: DeepSeek khuyến nghị 1.0 cho phân tích dữ liệu.
CONTEXT_TEMPERATURE = 1.0


def _context_sig(film_a_list, src: str, dst: str, model: str, level: str) -> str:
    h = hashlib.sha256()
    h.update(f"{CONTEXT_REV}|{src}|{dst}|{model}|{level}".encode("utf-8"))
    for a in film_a_list:
        h.update(b"\x1f")
        h.update(" ".join(str(a.get("text") or a.get("a_text") or "").split()).encode("utf-8"))
    return h.hexdigest()


def read_film_context(project_dir: str) -> dict | None:
    """Ghi chú bước 0 ĐÃ LƯU của dự án (để xem lại / nối vào mô tả YouTube), hoặc None.

    Trả ``{summary, notes, text, model, level, target, lines, created}``. Không kiểm vân
    tay: xem lại thì bản cũ vẫn có ích — nơi cần bản khớp phụ đề hiện tại là bước dịch
    (``build_film_context`` tự so)."""
    if not project_dir:
        return None
    path = os.path.join(project_dir, CONTEXT_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(L(f"[chi tiết] Không đọc được {CONTEXT_FILE} ({e}).", f"[advanced] Could not read {CONTEXT_FILE} ({e})."))
        return None
    if not isinstance(d, dict) or not str(d.get("text") or "").strip():
        return None
    summary, notes = d.get("summary"), d.get("notes")
    if summary is None or notes is None:
        summary, notes = PROMPTS.split_context(d.get("text") or "")
    return {
        "summary": summary or "", "notes": notes or "", "text": d.get("text") or "",
        "model": d.get("model") or "", "level": d.get("level") or "",
        "target": d.get("target") or "", "lines": d.get("lines") or 0,
        "created": d.get("created") or 0,
    }


def build_film_context(film_a_list, meta, model: str, project_dir: str = None,
                       should_stop=None, summary_level: str = "co-ban",
                       force: bool = False, strict: bool = False) -> str:
    """BƯỚC 0: một model đọc TOÀN BỘ phụ đề gốc, viết (1) TÓM TẮT cho người xem bằng
    ngôn ngữ đích và (2) GHI CHÚ DỊCH (nhân vật, xưng hô, tên riêng, thuật ngữ, giọng
    văn). Cả hai gửi kèm MỌI lô. Trả toàn văn, hoặc "" nếu không làm được.

    ``strict=False`` (đường dịch): KHÔNG raise — thiếu ghi chú thì vẫn dịch được như
    trước, chỉ kém thống nhất hơn, không đáng làm hỏng cả lượt tạo kịch bản.
    ``strict=True`` (nút "Tạo tóm tắt"): raise ``DeepSeekError`` / ``AdvancedAiError``
    để giao diện nói đúng lý do. ``force``: bỏ qua bản đã lưu, gọi lại.

    Lưu vào ``<dự án>/ai_context.json`` kèm vân tay (chữ phụ đề + ngôn ngữ + model +
    mức tóm tắt): tạo lại kịch bản / chạy lại lô cùng phụ đề thì dùng lại, không tốn
    thêm lượt gọi.
    """
    src = (meta.get("source_language") or "auto").lower()
    dst = (meta.get("target_language") or "vi").lower()
    level = summary_level if summary_level in PROMPTS.SUMMARY_LEVEL_TEXT else "co-ban"
    sig = _context_sig(film_a_list, src, dst, model, level)
    path = os.path.join(project_dir, CONTEXT_FILE) if project_dir else None
    if path and not force and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cu = json.load(f)
            if cu.get("sig") == sig and str(cu.get("text") or "").strip():
                logger.info(
                    L(f"[chi tiết] Bước 0: dùng lại tóm tắt + ghi chú đã có (model "
                    f"'{cu.get('model')}', {len(cu['text'])} ký tự) — phụ đề gốc không đổi.", f"[advanced] Step 0: reusing the existing summary + notes (model '{cu.get('model')}', {len(cu['text'])} characters) — the original subtitles are unchanged.")
                )
                return cu["text"]
        except Exception as e:  # noqa: BLE001
            logger.warning(L(f"[chi tiết] Bước 0: không đọc được bản cũ ({e}) — tạo lại.", f"[advanced] Step 0: could not read the old copy ({e}) — regenerating."))

    if should_stop and should_stop():
        return ""
    prompt, so_cau, bi_cat = PROMPTS.build_context_prompt(film_a_list, src, dst, level)
    if not so_cau:
        if strict:
            raise AdvancedAiError(L("Chưa có phụ đề gốc — hãy chạy 'Lấy phụ đề' trước.", "No original subtitles yet — run 'Get subtitles' first."))
        return ""
    logger.info(
        L(f"[chi tiết] Bước 0: model '{model}' đọc cả phim ({so_cau} câu gốc, tóm tắt "
        f"{'chi tiết' if level == 'chi-tiet' else 'cơ bản'})", f"[advanced] Step 0: model '{model}' is reading the whole film ({so_cau} original sentences, {'detailed' if level == 'chi-tiet' else 'basic'} summary)")
        + (L(" — phim quá dài, chỉ đọc phần đầu", " — film too long, reading only the beginning") if bi_cat else "") + "…"
    )
    t0 = time.time()
    try:
        text = _chat(
            [{"role": "user", "content": prompt}], model,
            max_tokens=CONTEXT_MAX_TOKENS, temperature=CONTEXT_TEMPERATURE,
            timeout=max(240, so_cau // 10),
        ).strip()
    except DeepSeekError as e:
        if strict:
            raise
        logger.warning(
            L(f"[chi tiết] Bước 0 lỗi ({e.user_message}) — dịch tiếp KHÔNG có ghi chú ngữ cảnh.", f"[advanced] Step 0 failed ({e.user_message}) — continuing the translation WITHOUT context notes.")
        )
        return ""
    except Exception as e:  # noqa: BLE001
        if strict:
            raise AdvancedAiError(L(f"Không đọc được ngữ cảnh video: {e}", f"Could not read the video context: {e}"))
        logger.warning(L(f"[chi tiết] Bước 0 lỗi ({e}) — dịch tiếp KHÔNG có ghi chú ngữ cảnh.", f"[advanced] Step 0 failed ({e}) — continuing the translation WITHOUT context notes."))
        return ""
    if not text:
        if strict:
            raise AdvancedAiError(L("AI trả về rỗng — thử lại hoặc đổi model ở Cài đặt.", "The AI returned an empty result — try again or change the model in Settings."))
        logger.warning(L("[chi tiết] Bước 0: model trả rỗng — dịch tiếp KHÔNG có ghi chú.", "[advanced] Step 0: the model returned an empty result — continuing the translation WITHOUT notes."))
        return ""
    summary, notes = PROMPTS.split_context(text)
    logger.info(
        L(f"[chi tiết] Bước 0 xong sau {time.time() - t0:.0f}s — tóm tắt {len(summary)} ký tự, "
        f"ghi chú {len(notes)} ký tự.", f"[advanced] Step 0 finished in {time.time() - t0:.0f}s — summary {len(summary)} characters, notes {len(notes)} characters.")
        + ("" if summary else L(" (model không tách phần TÓM TẮT — chỉ dùng làm ngữ cảnh)", " (the model did not separate the SUMMARY part — used as context only)"))
    )
    if path:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sig": sig, "model": model, "source": src, "target": dst,
                           "level": level, "lines": so_cau, "created": time.time(),
                           "summary": summary, "notes": notes, "text": text},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.warning(L(f"[chi tiết] Bước 0: không lưu được ghi chú ({e}).", f"[advanced] Step 0: could not save the notes ({e})."))
    return text


# --------------------------------------------------------------------------- #
#  Đọc phản hồi của model
# --------------------------------------------------------------------------- #
def _to_a_id(raw) -> int | None:
    """``12`` / ``"12"`` / ``"A12"`` -> 12. Không đọc được -> None.

    Prompt in câu gốc dưới dạng ``[A12] ...`` nên model rất hay trả lại nguyên chữ
    "A12" thay vì số. Bỏ những câu đó đi là mất câu oan, nên chấp nhận cả hai dạng.
    """
    try:
        return int(str(raw).strip().lstrip("Aa[").rstrip("]"))
    except (TypeError, ValueError):
        return None


def _clean_items(items, allowed: set, *, one_to_one: bool, claimed: set) -> list:
    """Lọc phản hồi của MỘT lô thành danh sách câu B dùng được.

    Bỏ mọi thứ không dùng được thay vì tin bừa:
      * câu rỗng / không phải dict;
      * ``source_a_ids`` trỏ ra NGOÀI lô này (model bịa số) — nếu nhận, câu B sẽ nhảy
        sang nằm ở đoạn phim khác;
      * a_id đã có câu B khác nhận (``claimed``) — lô chạy lại có thể dịch trùng, giữ
        cả hai là một câu gốc bị đọc hai lần.

    ``one_to_one`` (lồng tiếng): mỗi câu B chỉ được giữ MỘT a_id. Model đôi khi gộp
    hai câu thoại vào một dòng dịch; ở chế độ lồng tiếng thì phụ đề phải bám đúng
    từng dòng gốc nên giữ id đầu, phần còn lại để lượt vét dịch lại cho đủ.
    """
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = str(
            it.get("b_text") or it.get("text") or it.get("target_text") or ""
        ).strip()
        if not text:
            continue

        raw_ids = it.get("source_a_ids") or it.get("a_ids") or it.get("source_a_id")
        if isinstance(raw_ids, (int, str)):
            raw_ids = [raw_ids]
        ids = []
        for x in raw_ids or []:
            v = _to_a_id(x)
            if v is not None and v in allowed and v not in claimed and v not in ids:
                ids.append(v)
        if not ids:
            continue
        if one_to_one:
            ids = ids[:1]

        speaker = it.get("speaker") or it.get("character") or it.get("role")
        speaker = str(speaker).strip() if speaker else ""
        claimed.update(ids)
        out.append({
            "b_text": text,
            "source_a_ids": sorted(ids),
            "speaker": speaker or None,
        })
    return out


# --------------------------------------------------------------------------- #
#  Một lô
# --------------------------------------------------------------------------- #
def _run_batch(batch, *, kind, model, meta, assign_speakers, lot, api_errors,
               brief: str = "", before=(), after=()) -> list:
    """Gọi DeepSeek cho một lô câu A. Lô lỗi -> trả [] (KHÔNG raise).

    Vì sao không raise: một lô lỗi (429 / 5xx / JSON cụt) không được phép làm sập cả
    tiến trình. Câu của lô đó nằm lại trong ``missing`` và được lượt vét lo.
    """
    prompt = PROMPTS.build_prompt(
        kind,
        batch,
        source_lang=meta.get("source_language") or "auto",
        target_lang=meta.get("target_language") or "vi",
        default_rate=meta.get("default_rate") or "auto",
        style=meta.get("speech_style"),
        brand=meta.get("brand_name"),
        voice=meta.get("target_voice"),
        with_speaker=assign_speakers,
    )
    #  Ghi chú ngữ cảnh ở tin nhắn SYSTEM — GIỐNG HỆT nhau giữa mọi lô nên DeepSeek tính
    #  giá "cache hit" cho phần này. Câu liền kề nối vào CUỐI (mỗi lô một khác).
    messages = []
    if brief:
        messages.append({"role": "system", "content": PROMPTS.context_system_message(brief)})
    messages.append({"role": "user", "content": prompt + PROMPTS.neighbor_block(before, after)})
    try:
        content = _chat(
            messages, model, max_tokens=_max_tokens(len(batch)), timeout=_timeout(len(batch)),
        )
        return _parse_json_array(content)
    except DeepSeekError as e:
        # Giữ lại lỗi API để nếu KHÔNG ra câu nào thì báo đúng nguyên nhân (hết số dư
        # / sai key / quá tải) chứ không phải "model trả sai định dạng".
        api_errors.append(e)
        logger.error(L(f"[chi tiết] Lô {lot} lỗi API: {e.user_message}", f"[advanced] Batch {lot} API error: {e.user_message}"))
        return []
    except Exception as e:
        logger.error(L(f"[chi tiết] Lô {lot} không đọc được kết quả (bỏ qua lô): {e}", f"[advanced] Batch {lot} result could not be read (batch skipped): {e}"))
        return []


def _run_pass(a_list, *, kind, model, meta, assign_speakers, batch_size,
              claimed: set, should_stop, on_batch, label: str,
              api_errors: list, brief: str = "", film=None) -> list:
    """Chạy MỘT lượt: chia lô, gửi song song, gộp câu B hợp lệ theo thứ tự lô.

    ``film``: toàn bộ câu gốc của phim theo thời gian — để lấy câu LIỀN TRƯỚC / LIỀN
    SAU mỗi lô (kể cả ở lượt vét, nơi lô gồm những câu rời rạc)."""
    film = film or a_list
    vi_tri = {}
    for i, a in enumerate(film):
        v = _to_a_id(a.get("a_id"))
        if v is not None:
            vi_tri.setdefault(v, i)
    k = PROMPTS.NEIGHBOR_LINES

    def _lien_ke(batch):
        i0 = vi_tri.get(_to_a_id(batch[0].get("a_id")))
        i1 = vi_tri.get(_to_a_id(batch[-1].get("a_id")))
        if i0 is None or i1 is None:
            return (), ()
        return film[max(0, i0 - k):i0], film[i1 + 1:i1 + 1 + k]

    batches = [
        a_list[i:i + batch_size] for i in range(0, len(a_list), batch_size)
    ]
    logger.info(
        L(f"[chi tiết] {label}: {len(a_list)} câu gốc / {len(batches)} lô x{batch_size}, "
        f"chạy song song tối đa {DEEPSEEK_CONCURRENCY} lô (model '{model}').", f"[advanced] {label}: {len(a_list)} original sentences / {len(batches)} batches x{batch_size}, running up to {DEEPSEEK_CONCURRENCY} batches in parallel (model '{model}').")
    )
    one_to_one = PROMPTS.normalize_kind(kind) == PROMPTS.KIND_DUBBING
    raw_per_batch = [None] * len(batches)

    def _job(idx, batch):
        if should_stop and should_stop():
            return []
        before, after = _lien_ke(batch)
        return _run_batch(
            batch, kind=kind, model=model, meta=meta,
            assign_speakers=assign_speakers, lot=f"{label} #{idx + 1}",
            api_errors=api_errors, brief=brief, before=before, after=after,
        )

    with cf.ThreadPoolExecutor(max_workers=DEEPSEEK_CONCURRENCY) as pool:
        futs = {pool.submit(_job, i, b): i for i, b in enumerate(batches)}
        for fut in cf.as_completed(futs):
            idx = futs[fut]
            raw_per_batch[idx] = fut.result()
            if on_batch:
                on_batch(len(batches[idx]))

    # Lọc THEO THỨ TỰ LÔ (không theo thứ tự chạy xong) để ``claimed`` được nhận theo
    # đúng trình tự phim: hai lô cùng nhận một a_id thì lô ĐẦU giữ, khỏi phụ thuộc
    # vào lô nào về trước.
    out = []
    for idx, raw in enumerate(raw_per_batch):
        allowed = {
            v for v in (_to_a_id(a.get("a_id")) for a in batches[idx]) if v is not None
        }
        out.extend(
            _clean_items(raw, allowed, one_to_one=one_to_one, claimed=claimed)
        )
    return out


# --------------------------------------------------------------------------- #
#  Cửa chính
# --------------------------------------------------------------------------- #
def generate_bchunks(
    a_list, meta, kind, model, *,
    assign_speakers: bool = True,
    should_stop=None,
    on_progress=None,
    google_fallback: bool = True,
    film_a_list=None,
    project_dir: str = None,
) -> dict:
    """Sinh câu B cho danh sách câu A bằng DeepSeek.

    a_list: list ``[{a_id, start, end, text}]`` — ĐÃ sắp theo thời gian.
    film_a_list: TOÀN BỘ câu gốc của phim (khi ``a_list`` chỉ là đoạn một clip) — bước 0
      đọc ngữ cảnh cả phim và câu liền kề lấy từ đây. Bỏ trống = ``a_list``.
    project_dir: thư mục dự án — lưu ghi chú ngữ cảnh để lần sau dùng lại.
    Trả về dict:
      * ``chunks``        : list ``[{b_id, b_text, source_a_ids, speaker}]`` (dạng
        ``ADV.import_script`` / ``SEG.append_script`` nhận trực tiếp).
      * ``missing_a_ids`` : câu gốc không ra được kịch bản (cả 3 lượt).
      * ``google_a_ids``  : câu phải nhờ Google dịch (chỉ ``dubbing``).
      * ``retried``       : số câu phải chạy lượt vét.
      * ``cancelled``     : bị người dùng huỷ giữa chừng.

    Raise ``AdvancedAiError``/``DeepSeekError`` khi KHÔNG ra câu nào — để phía trên
    báo đúng nguyên nhân thay vì import một kịch bản rỗng.
    """
    kind = PROMPTS.normalize_kind(kind)
    is_dubbing = kind == PROMPTS.KIND_DUBBING
    meta = meta or {}
    a_list = [a for a in (a_list or []) if isinstance(a, dict)]
    if not a_list:
        raise AdvancedAiError(L("Chưa có phụ đề gốc — hãy chạy 'Lấy phụ đề' trước.", "No original subtitles yet — run 'Get subtitles' first."))

    by_id = {}
    for a in a_list:
        v = _to_a_id(a.get("a_id"))
        if v is not None:
            by_id[v] = a
    if not by_id:
        raise AdvancedAiError(L("Phụ đề gốc thiếu số thứ tự câu (a_id) — không dựng được prompt.", "The original subtitles are missing sentence numbers (a_id) — the prompt cannot be built."))

    total = len(by_id)
    opts = _options()
    base_batch = int(opts.get("batch_size") or FALLBACK_BATCH_SIZE)
    film = [a for a in (film_a_list or a_list) if isinstance(a, dict)]
    claimed: set = set()
    api_errors: list = []
    done = 0

    def _bao(phase):
        if on_progress:
            try:
                on_progress(min(done, total), total, phase)
            except Exception:
                pass

    def _tick(n):
        nonlocal done
        done += n
        _bao("script")
        logger.info(L(f"[chi tiết] Tiến độ tạo kịch bản: {min(done, total)}/{total} câu gốc.", f"[advanced] Script progress: {min(done, total)}/{total} original sentences."))

    src_lang = (meta.get("source_language") or "auto").lower()
    dst_lang = (meta.get("target_language") or "vi").lower()
    logger.info(
        L(f"[chi tiết] Tạo kịch bản {'LỒNG TIẾNG' if is_dubbing else 'REVIEW'} bằng AI cho "
        f"{total} câu gốc — ngôn ngữ: {src_lang} -> {dst_lang}, "
        f"phân vai nhân vật: {'CÓ' if assign_speakers else 'KHÔNG'}.", f"[advanced] Generating the {'DUBBING' if is_dubbing else 'REVIEW'} script with AI for {total} original sentences — language: {src_lang} -> {dst_lang}, character role assignment: {'YES' if assign_speakers else 'NO'}.")
    )
    # NGUON TRUNG DICH = KHONG CO GI DE DICH. Prompt se doc thanh "câu thoại gốc
    # (Vietnamese)... dịch sang Vietnamese" -> model tra lai NGUYEN VAN cau goc, va
    # Google cung the (``batch_translate`` thoat som khi src == dest). Nguoi dung thay
    # "dich xong" ma chu y het nhu cu, khong mot loi canh bao nao — da xay ra that o
    # may khach. Chan tai day, noi thang phai sua o dau.
    if src_lang != "auto" and src_lang == dst_lang:
        raise AdvancedAiError(
            L(f"Ngôn ngữ nguồn và ngôn ngữ đích đang GIỐNG NHAU ({dst_lang}) nên không có "
            "gì để dịch — AI sẽ trả lại nguyên văn câu gốc. Vào tab Chuẩn bị sửa "
            "'Ngôn ngữ nguồn' (tiếng của phim) hoặc 'Dịch sang' (tiếng muốn đọc) cho khác nhau.", f"The source language and the target language are the SAME ({dst_lang}) so there is nothing to translate — the AI would return the original sentences verbatim. Go to the Prepare tab and change \"Source language\" (the language of the film) or \"Translate to\" (the language to read aloud) so they differ.")
        )

    # --- Bước 0: đọc ngữ cảnh cả phim (tuỳ chọn) ----------------------------
    brief = ""
    if opts.get("context_enabled", False):
        _bao("context")
        brief = build_film_context(
            film, meta, str(opts.get("context_model") or model),
            project_dir=project_dir, should_stop=should_stop,
            summary_level=str(opts.get("summary_level") or "co-ban"),
        )
    else:
        logger.info(L("[chi tiết] Bước 0 (đọc ngữ cảnh cả phim): TẮT trong Cài đặt.", "[advanced] Step 0 (reading the context of the whole film): OFF in Settings."))

    # --- Lượt 1: chính ------------------------------------------------------
    _bao("script")
    chunks = _run_pass(
        a_list, kind=kind, model=model, meta=meta, assign_speakers=assign_speakers,
        batch_size=base_batch, claimed=claimed, should_stop=should_stop,
        on_batch=_tick, label=L("lượt chính", "main pass"), api_errors=api_errors,
        brief=brief, film=film,
    )
    cancelled = bool(should_stop and should_stop())

    # --- Lượt 2: vét những câu còn thiếu, lô nhỏ hơn ------------------------
    missing_ids = [i for i in sorted(by_id) if i not in claimed]
    retried = 0
    if missing_ids and not cancelled:
        retried = len(missing_ids)
        logger.warning(
            L(f"[chi tiết] Còn {retried}/{total} câu gốc chưa có kịch bản (lô lỗi hoặc "
            f"model trả thiếu) — chạy lại một lượt với lô nhỏ hơn.", f"[advanced] {retried}/{total} original sentences still have no script (failed batches or the model returned too few) — running one more pass with smaller batches.")
        )
        chunks += _run_pass(
            [by_id[i] for i in missing_ids],
            kind=kind, model=model, meta=meta, assign_speakers=assign_speakers,
            batch_size=max(MIN_BATCH, base_batch // RETRY_SHRINK),
            claimed=claimed, should_stop=should_stop, on_batch=None,
            label=L("lượt vét", "cleanup pass"), api_errors=api_errors, brief=brief, film=film,
        )
        cancelled = bool(should_stop and should_stop())

    # --- Lượt 3: Google cứu phần còn lại (CHỈ lồng tiếng) -------------------
    #  Review KHÔNG có bước này: Google dịch được câu, chứ không viết lại được lời
    #  bình MC. Cứu kiểu đó là nhét phụ đề dịch máy vào giữa kịch bản.
    google_ids: list = []
    missing_ids = [i for i in sorted(by_id) if i not in claimed]
    if missing_ids and is_dubbing and google_fallback and not cancelled:
        from services.ai_engines import ten_bo_ai as _ten_bo_ai

        logger.warning(
            L(f"[chi tiết] Dịch {len(missing_ids)} câu còn lại bằng Google Dịch "
            f"({_ten_bo_ai(model)} không trả về được).", f"[advanced] Translating the remaining {len(missing_ids)} sentences with Google Translate ({_ten_bo_ai(model)} could not return them).")
        )
        got = _google_translate(missing_ids, by_id, meta, should_stop)
        for a_id, text in got.items():
            claimed.add(a_id)
            google_ids.append(a_id)
            chunks.append({
                "b_text": text,
                "source_a_ids": [a_id],
                # Google không biết vai — để trống, ``speaker_of`` sẽ trả vai mặc định.
                "speaker": None,
            })

    if not chunks:
        if api_errors:
            raise api_errors[0]
        if cancelled:
            raise AdvancedAiError(L("Đã huỷ tạo kịch bản theo yêu cầu.", "Script generation cancelled at the user's request."))
        raise AdvancedAiError(
            L("AI không trả về câu kịch bản hợp lệ nào (sai định dạng). "
            "Thử lại hoặc đổi model trong Cài đặt.", "The AI did not return any valid script sentence (wrong format). Try again or change the model in Settings.")
        )

    # Sắp theo THỜI GIAN câu gốc đầu tiên rồi đánh số lại: lô chạy song song và lượt
    # vét làm thứ tự trả về lộn xộn, mà SRT/timeline đọc theo thứ tự này.
    def _key(c):
        first = c["source_a_ids"][0]
        a = by_id.get(first) or {}
        try:
            return (float(a.get("start") or 0.0), first)
        except (TypeError, ValueError):
            return (0.0, first)

    chunks.sort(key=_key)
    for i, c in enumerate(chunks, start=1):
        c["b_id"] = i

    missing_ids = [i for i in sorted(by_id) if i not in claimed]
    logger.info(
        L(f"[chi tiết] Xong: {len(chunks)} câu kịch bản / {total} câu gốc", f"[advanced] Done: {len(chunks)} script sentences / {total} original sentences")
        + (L(f" — {len(google_ids)} câu dịch bằng Google", f" — {len(google_ids)} sentences translated by Google") if google_ids else "")
        + (L(f" — CÒN THIẾU {len(missing_ids)} câu", f" — {len(missing_ids)} sentences STILL MISSING") if missing_ids else "")
    )
    return {
        "chunks": chunks,
        "missing_a_ids": missing_ids,
        "google_a_ids": sorted(google_ids),
        "retried": retried,
        "cancelled": cancelled,
        "kind": kind,
    }


def _google_translate(missing_ids, by_id, meta, should_stop) -> dict:
    """Dịch các câu còn lại bằng Google — trả ``{a_id: text}`` (rỗng nếu cũng lỗi).

    Dùng lại ``deepseek_service._google_fallback`` để đi CHUNG trần 10 request đồng
    thời + circuit-breaker 429 của ``TranslationService``: gọi Google bằng đường
    riêng là hai chỗ cùng đập vào một dịch vụ mà không ai biết trần của ai.
    """
    import asyncio

    from services.deepseek_service import _google_fallback

    segments = [
        {
            "id": i,
            "text": str(
                (by_id.get(i) or {}).get("text")
                or (by_id.get(i) or {}).get("a_text") or ""
            ),
        }
        for i in missing_ids
    ]
    segments = [s for s in segments if s["text"].strip()]
    if not segments:
        return {}
    src = (meta.get("source_language") or "auto").lower()
    dst = (meta.get("target_language") or "vi").lower()
    if src != "auto" and src == dst:
        # ``TranslationService.batch_translate`` tra NGUYEN danh sach dau vao khi
        # src == dest. Nhan ket qua do la ta tu tay chep cau goc vao kich ban.
        logger.error(
            L(f"[chi tiết] Bỏ qua Google Dịch: ngôn ngữ nguồn trùng ngôn ngữ đích ({dst}).", f"[advanced] Skipping Google Translate: the source language equals the target language ({dst}).")
        )
        return {}
    try:
        got = asyncio.run(_google_fallback(segments, src, dst, should_stop))
    except Exception as e:
        logger.error(L(f"[chi tiết] Google Dịch cũng lỗi: {e}", f"[advanced] Google Translate also failed: {e}"))
        return {}

    # BO nhung cau Google tra lai Y HET cau goc. Google im lang tra nguyen van trong
    # vai truong hop (doan nham ngon ngu, cau chi co ten rieng/so). Nhan bua thi kich
    # ban co nhung dong tieng nuoc ngoai xen giua ma khong ai duoc bao; tinh la THIEU
    # thi nguoi dung thay con so "còn thiếu N câu" va bam chay lai duoc.
    goc = {s["id"]: s["text"].strip() for s in segments}
    out = {i: t for i, t in got.items() if (t or "").strip() != goc.get(i)}
    bo = len(got) - len(out)
    if bo:
        logger.warning(
            L(f"[chi tiết] {bo} câu Google trả lại nguyên văn câu gốc (không dịch được) "
            f"— tính là còn thiếu thay vì đưa nguyên câu gốc vào kịch bản.", f"[advanced] {bo} sentences came back from Google unchanged (could not be translated) — counted as missing instead of putting the original sentence into the script.")
        )
    return out
