"""PHỤ ĐỀ NHIỀU THỨ TIẾNG cho dự án Chi tiết (``<dự án>/sub_tracks.json``).

Người dùng làm phim Trung -> lồng tiếng + phụ đề tiếng Việt xong, muốn thêm phụ đề tiếng
Nhật, Hàn… cho cùng video (28-09-2026). Tiếng Việt vẫn là thứ tiếng CHÍNH (``b_text``,
thứ được lồng tiếng); mỗi thứ tiếng thêm là một "bản phụ đề" riêng:

    {"version": 1, "tracks": {"ja": {"lines": {<uid câu B>: {"text": "...", "src": "<vân>"}},
                                     "updated": <epoch>}}}

· KHOÁ THEO ``uid`` CÂU B, không lưu thời gian: câu dịch hiện đúng lúc câu tiếng Việt của
  nó hiện — đổi tốc độ, dọn chồng lấn, kéo câu, hoàn tác… đều tự theo, không lệch.
· ``src`` = vân tay câu tiếng Việt lúc dịch. Người dùng sửa câu tiếng Việt thì vân lệch ->
  câu đó báo "cần dịch lại" (không tự dịch đè lên chỗ người ta có thể đã sửa tay).
· Tệp riêng, KHÔNG nằm trong ảnh chụp hoàn tác của trình dựng (Ctrl+Z không đụng bản dịch).

Dịch: DeepSeek dịch CÂU TIẾNG VIỆT (bản đã biên tập, đúng thứ người xem đọc) kèm câu gốc
để tham khảo tên riêng / thuật ngữ, cộng ghi chú ngữ cảnh bước 0 nếu có. Câu DeepSeek bỏ
sót -> lượt vét lô nhỏ -> Google (vi -> đích).
Test: tests/test_phu_de_nhieu_thu_tieng.py
"""
from utils.i18n import L
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("ReviewAdvanced")

TEP = "sub_tracks.json"
PHIEN_BAN = 1

_khoa_tep: dict = {}
_khoa_tep_chung = threading.Lock()


def _khoa(project_dir: str) -> threading.Lock:
    with _khoa_tep_chung:
        k = os.path.normcase(os.path.abspath(project_dir))
        if k not in _khoa_tep:
            _khoa_tep[k] = threading.Lock()
        return _khoa_tep[k]


def chuan_ma(lang: str) -> str:
    lang = (lang or "").strip().lower()
    return "zh" if lang in ("zh-cn", "zh-hans") else lang


def van_tay(text: str) -> str:
    return hashlib.sha1(" ".join(str(text or "").split()).encode("utf-8")).hexdigest()[:12]


#: Tên thứ tiếng BẰNG CHÍNH THỨ TIẾNG ĐÓ — hiện trong menu phụ đề của trình phát, nơi
#: người xem nước ngoài chọn (người Nhật tìm "日本語", không tìm "Tiếng Nhật").
TEN_BAN_XU = {
    "vi": "Tiếng Việt", "en": "English", "zh": "中文", "ja": "日本語", "ko": "한국어",
    "fr": "Français", "de": "Deutsch", "es": "Español", "ru": "Русский",
    "pt": "Português", "it": "Italiano", "th": "ไทย", "id": "Bahasa Indonesia",
}


def nhan_thu_tieng(lang: str) -> str:
    return TEN_BAN_XU.get(chuan_ma(lang), chuan_ma(lang))


def ma_hop_le(lang: str, main_lang: str) -> bool:
    from services.deepseek_service import LANG_NAMES

    lang = chuan_ma(lang)
    return lang in LANG_NAMES and lang != chuan_ma(main_lang)


# --------------------------------------------------------------------------- tệp
def doc(project_dir: str) -> dict:
    path = os.path.join(project_dir, TEP)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"version": PHIEN_BAN, "tracks": {}}
    except Exception as e:  # noqa: BLE001
        logger.warning(L(f"[phụ đề nhiều thứ tiếng] Không đọc được {TEP} ({e}).", f"[multi-language subtitles] Could not read {TEP} ({e})."))
        return {"version": PHIEN_BAN, "tracks": {}}
    if not isinstance(d, dict) or not isinstance(d.get("tracks"), dict):
        return {"version": PHIEN_BAN, "tracks": {}}
    return d


def _ghi(project_dir: str, d: dict) -> None:
    path = os.path.join(project_dir, TEP)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)


def sua(project_dir: str, fn):
    """Đọc -> ``fn(d)`` -> ghi, dưới khoá của dự án (job dịch và ô sửa tay cùng ghi)."""
    with _khoa(project_dir):
        d = doc(project_dir)
        kq = fn(d)
        _ghi(project_dir, d)
        return kq


def them_thu_tieng(project_dir: str, lang: str) -> None:
    lang = chuan_ma(lang)

    def _fn(d):
        d["tracks"].setdefault(lang, {"lines": {}, "updated": time.time()})
    sua(project_dir, _fn)


def xoa_thu_tieng(project_dir: str, lang: str) -> bool:
    lang = chuan_ma(lang)
    return sua(project_dir, lambda d: d["tracks"].pop(lang, None) is not None)


def xoa_moi_cau(project_dir: str) -> int:
    """Câu chính bị XOÁ SẠCH rồi tạo lại (Cơ bản: chạy lại nhận dạng / OCR) -> bỏ mọi câu
    đã dịch, GIỮ danh sách thứ tiếng. Trả về số câu đã bỏ.

    Vì sao: câu Cơ bản khoá theo ``s<id>`` mà SQLite DÙNG LẠI id đã xoá — không bỏ ở đây
    thì bản dịch cũ gắn vào câu MỚI chẳng liên quan, bị đốt vào video, xuất .srt, tải
    lên YouTube (chỉ báo "cần dịch lại", vẫn được dùng). Chi tiết thì câu mới mang uid
    mới nên tự thành "chưa dịch" — ở đây làm cho Cơ bản giống vậy (rà soát 29-09-2026)."""
    if not os.path.exists(os.path.join(project_dir, TEP)):
        return 0

    def _fn(d):
        n = 0
        for t in d["tracks"].values():
            if isinstance(t, dict):
                n += len(t.get("lines") or {})
                t["lines"] = {}
                t["updated"] = time.time()
        return n
    n = sua(project_dir, _fn)
    if n:
        logger.info(f"[phụ đề nhiều thứ tiếng] Phụ đề gốc tạo lại -> bỏ {n} câu đã dịch (cần dịch lại).")
    return n


def ghi_cau(project_dir: str, lang: str, texts: dict, b_chunks: list) -> int:
    """Sửa tay ``{uid: câu}``. Vân = câu tiếng Việt HIỆN TẠI (người dùng sửa theo bản đang
    thấy). Câu rỗng = xoá bản dịch của câu đó."""
    lang = chuan_ma(lang)
    vi = {str(b.get("uid")): b.get("b_text") or "" for b in b_chunks if b.get("uid")}

    def _fn(d):
        tr = d["tracks"].setdefault(lang, {"lines": {}})
        lines = tr.setdefault("lines", {})
        n = 0
        for uid, text in (texts or {}).items():
            uid = str(uid)
            if uid not in vi:
                continue
            text = str(text or "").strip()
            if text:
                lines[uid] = {"text": text, "src": van_tay(vi[uid])}
            else:
                lines.pop(uid, None)
            n += 1
        tr["updated"] = time.time()
        return n
    return sua(project_dir, _fn)


# --------------------------------------------------------------------------- xem
def _cau_co_chu(b_chunks: list) -> list:
    return [b for b in b_chunks if b.get("uid") and str(b.get("b_text") or "").strip()]


def tinh_trang(d: dict, b_chunks: list) -> list:
    """``[{lang, total, done, stale, missing}]`` cho mọi thứ tiếng đã thêm."""
    cau = _cau_co_chu(b_chunks)
    out = []
    for lang, tr in sorted((d.get("tracks") or {}).items()):
        lines = tr.get("lines") or {}
        done = stale = 0
        for b in cau:
            ln = lines.get(str(b["uid"]))
            if not ln or not str(ln.get("text") or "").strip():
                continue
            if ln.get("src") == van_tay(b.get("b_text")):
                done += 1
            else:
                stale += 1
        out.append({"lang": lang, "total": len(cau), "done": done, "stale": stale,
                    "missing": len(cau) - done - stale})
    return out


def danh_sach_cau(d: dict, lang: str, b_chunks: list, a_lookup: dict) -> list:
    lines = ((d.get("tracks") or {}).get(chuan_ma(lang)) or {}).get("lines") or {}
    out = []
    for b in _cau_co_chu(b_chunks):
        ln = lines.get(str(b["uid"])) or {}
        text = str(ln.get("text") or "")
        out.append({
            "uid": b["uid"], "b_id": b.get("b_id"),
            "vi": b.get("b_text") or "", "src": _cau_goc(b, a_lookup),
            "text": text,
            "stale": bool(text) and ln.get("src") != van_tay(b.get("b_text")),
        })
    return out


def ban_dich(d: dict, lang: str) -> dict:
    """``{uid: câu dịch}`` (bỏ câu rỗng) — dùng khi xuất. Câu "cần dịch lại" VẪN dùng:
    bản dịch cũ còn gần nghĩa hơn là để trống; giao diện đã cảnh báo trước khi xuất."""
    lines = ((d.get("tracks") or {}).get(chuan_ma(lang)) or {}).get("lines") or {}
    return {uid: str(v.get("text")).strip() for uid, v in lines.items()
            if str((v or {}).get("text") or "").strip()}


def _cau_goc(b: dict, a_lookup: dict) -> str:
    parts = []
    for aid in b.get("source_a_ids") or []:
        a = (a_lookup or {}).get(str(aid)) or {}
        t = " ".join(str(a.get("text") or "").split())
        if t:
            parts.append(t)
    return " ".join(parts)


# --------------------------------------------------------------------------- dịch
#: Trạng thái job dịch — TRONG BỘ NHỚ (không ghi metadata.json: job chạy lâu, ghi đè
#: metadata từ bản đọc lúc đầu là nuốt mất thay đổi người dùng làm trong lúc chờ).
_JOB: dict = {}
_JOB_LOCK = threading.Lock()


def trang_thai_job(project_id: str) -> dict | None:
    with _JOB_LOCK:
        j = _JOB.get(project_id)
        return dict(j) if j else None


def bat_dau_job(project_id: str, lang: str, total: int) -> bool:
    with _JOB_LOCK:
        j = _JOB.get(project_id)
        if j and j.get("running"):
            return False
        _JOB[project_id] = {"lang": chuan_ma(lang), "total": total, "done": 0,
                            "running": True, "error": "", "cancel": False,
                            "started": time.time()}
        return True


def huy_job(project_id: str) -> None:
    with _JOB_LOCK:
        if project_id in _JOB:
            _JOB[project_id]["cancel"] = True


def _cap_nhat_job(project_id: str, **kw) -> None:
    with _JOB_LOCK:
        if project_id in _JOB:
            _JOB[project_id].update(kw)


def _bi_huy(project_id: str) -> bool:
    with _JOB_LOCK:
        return bool((_JOB.get(project_id) or {}).get("cancel"))


def can_dich(d: dict, lang: str, b_chunks: list, pham_vi: str) -> list:
    """Câu cần gửi đi: ``missing`` = chưa có + cần dịch lại; ``all`` = mọi câu."""
    lines = ((d.get("tracks") or {}).get(chuan_ma(lang)) or {}).get("lines") or {}
    out = []
    for b in _cau_co_chu(b_chunks):
        ln = lines.get(str(b["uid"]))
        if pham_vi == "all" or not ln or ln.get("src") != van_tay(b.get("b_text")):
            out.append(b)
    return out


def _prompt_he_thong(lang: str, main_lang: str, brief: str) -> str:
    from services.deepseek_service import LANG_NAMES

    dich = LANG_NAMES.get(lang, lang)
    chinh = LANG_NAMES.get(chuan_ma(main_lang), "the video's original language")
    s = (
        f"You are a professional subtitle translator. Translate video SUBTITLES from "
        f"{chinh} into {dich}.\n"
        f"Each input item has: \"i\" (line number), \"t\" — the {chinh} subtitle line shown "
        f"on screen (THIS is what you translate), and optionally \"src\" — the original-"
        f"language line it came from (reference only: use it to get names, terms and "
        f"nuance right; do not translate parts of it that \"t\" left out).\n"
        f"Rules:\n"
        f"- Natural, concise {dich} subtitle style that a native viewer reads quickly.\n"
        f"- Exactly one output per input \"i\"; never merge, split, skip or reorder lines.\n"
        f"- Keep names, forms of address and terminology consistent across all lines.\n"
        f"- No notes, no romanization, no quotes around the line.\n"
        f"- Output ONLY a JSON array: [{{\"i\": <number>, \"t\": \"<{dich} line>\"}}, ...]"
    )
    if brief.strip():
        s += (
            "\n\nVIDEO CONTEXT (written by an editor after reading all subtitles; reference "
            "only, never translate or output it):\n" + brief.strip()
        )
    return s


def _mot_lo(lo, *, he_thong, model, api_errors) -> dict:
    from services.deepseek_service import DeepSeekError, _chat, _parse_json_array

    vao = [{"i": i, "t": " ".join(str(b.get("b_text") or "").split()),
            **({"src": s} if s else {})} for i, b, s in lo]
    so = {i for i, _b, _s in lo}
    try:
        content = _chat(
            [{"role": "system", "content": he_thong},
             {"role": "user", "content": json.dumps(vao, ensure_ascii=False)}],
            model, max_tokens=int(min(65536, max(8192, len(lo) * 120))),
            timeout=int(max(180, len(lo) * 3)), temperature=1.3,
        )
        items = _parse_json_array(content)
    except DeepSeekError as e:
        api_errors.append(e)
        logger.error(L(f"[phụ đề nhiều thứ tiếng] Lô lỗi API: {e.user_message}", f"[multi-language subtitles] Batch API error: {e.user_message}"))
        return {}
    except Exception as e:  # noqa: BLE001
        logger.error(L(f"[phụ đề nhiều thứ tiếng] Lô không đọc được kết quả: {e}", f"[multi-language subtitles] Batch result could not be read: {e}"))
        return {}
    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            i = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        t = " ".join(str(it.get("t") or "").split())
        if i in so and t:
            out[i] = t
    return out


def dich(project_id: str, project_dir: str, meta: dict, lang: str, b_chunks: list,
         a_lookup: dict, *, engine: str = "deepseek", pham_vi: str = "missing",
         model: str = "", batch_size: int = 80, brief: str = "") -> dict:
    """Dịch các câu cần dịch rồi ghi dần vào tệp (mỗi lô xong là lưu — Hủy giữa chừng
    vẫn giữ phần đã dịch). Trả ``{"dich": n, "google": g, "thieu": m}``."""
    import asyncio

    from services.deepseek_service import DEEPSEEK_CONCURRENCY, _google_fallback

    lang = chuan_ma(lang)
    main_lang = meta.get("target_language") or "vi"
    viec = can_dich(doc(project_dir), lang, b_chunks, pham_vi)
    _cap_nhat_job(project_id, total=len(viec), done=0)
    by_i = {i: b for i, b in enumerate(viec, 1)}
    con = dict(by_i)
    so_google = 0

    def _luu(kq: dict):
        vtay = {i: van_tay(by_i[i].get("b_text")) for i in kq}

        def _fn(d):
            tr = d["tracks"].setdefault(lang, {"lines": {}})
            lines = tr.setdefault("lines", {})
            for i, t in kq.items():
                lines[str(by_i[i]["uid"])] = {"text": t, "src": vtay[i]}
            tr["updated"] = time.time()
        sua(project_dir, _fn)
        for i in kq:
            con.pop(i, None)
        _cap_nhat_job(project_id, done=len(by_i) - len(con))

    from services.ai_engines import la_ai, ten_bo_ai

    if la_ai(engine) and con:
        he_thong = _prompt_he_thong(lang, main_lang, brief)
        api_errors: list = []
        for co in (max(8, int(batch_size or 80)), max(8, int(batch_size or 80) // 3)):
            ds = [(i, b, _cau_goc(b, a_lookup)) for i, b in sorted(con.items())]
            lo_list = [ds[k:k + co] for k in range(0, len(ds), co)]
            if not lo_list:
                break
            logger.info(L(f"[phụ đề nhiều thứ tiếng] {ten_bo_ai(model or engine)} dịch {len(ds)} câu sang {lang}, "
                        f"{len(lo_list)} lô × {co} câu (model {model}).", f"[multi-language subtitles] {ten_bo_ai(model or engine)} is translating {len(ds)} sentences into {lang}, {len(lo_list)} batches × {co} sentences (model {model})."))
            ex = ThreadPoolExecutor(max_workers=DEEPSEEK_CONCURRENCY)
            try:
                futs = [ex.submit(_mot_lo, lo, he_thong=he_thong, model=model,
                                  api_errors=api_errors) for lo in lo_list]
                for f in futs:
                    if _bi_huy(project_id):
                        break
                    kq = f.result()
                    if kq:
                        _luu(kq)
            finally:
                #  Hủy thì BỎ các lô còn xếp hàng (cancel_futures), không ngồi chờ chúng
                #  gọi DeepSeek xong — phim dài là vài phút sau mới thật sự dừng.
                ex.shutdown(wait=not _bi_huy(project_id), cancel_futures=True)
            if _bi_huy(project_id):
                return {"dich": len(by_i) - len(con), "google": 0, "thieu": len(con), "huy": True}
        if api_errors and len(con) == len(by_i):
            #  Không ra câu nào + có lỗi API -> báo đúng lỗi (hết số dư / sai key …) chứ
            #  không lặng lẽ đẩy cả phim sang Google.
            raise RuntimeError(getattr(api_errors[-1], "user_message", str(api_errors[-1])))

    if con and not _bi_huy(project_id):
        segs = [{"id": i, "text": " ".join(str(b.get("b_text") or "").split())}
                for i, b in sorted(con.items())]
        try:
            got = asyncio.run(_google_fallback(segs, chuan_ma(main_lang), lang,
                                               lambda: _bi_huy(project_id)))
        except Exception as e:  # noqa: BLE001
            logger.error(L(f"[phụ đề nhiều thứ tiếng] Google Dịch lỗi: {e}", f"[multi-language subtitles] Google Translate error: {e}"))
            got = {}
        goc = {s["id"]: s["text"] for s in segs}
        got = {i: " ".join(str(t or "").split()) for i, t in (got or {}).items()
               if i in con and str(t or "").strip()
               and (str(t).strip() != goc.get(i) or _giu_nguyen_duoc(goc.get(i) or ""))}
        if got:
            so_google = len(got)
            _luu(got)
    return {"dich": len(by_i) - len(con), "google": so_google, "thieu": len(con)}


# --------------------------------------------------------------------------- prompt
def _giu_nguyen_duoc(text: str) -> bool:
    """Google trả NGUYÊN VĂN mà vẫn là bản dịch đúng: câu rất ngắn ("OK", "iPhone 15") hoặc
    không có chữ cái ("100%", "2026"). Câu dài trả nguyên văn thì coi như Google không dịch
    (lỗi / nhận nhầm thứ tiếng) và bỏ — bỏ luôn cả câu ngắn thì những câu đó mãi "chưa
    dịch", chạy lô lần nào cũng báo thiếu (rà soát 29-09-2026)."""
    t = text.strip()
    return bool(t) and (len(t.split()) <= 3 or not any(c.isalpha() for c in t))


def tao_prompt(d: dict, lang: str, meta: dict, b_chunks: list, a_lookup: dict, *,
               brief: str = "", pham_vi: str = "missing", toi_da: int = 0) -> dict:
    """Prompt để người dùng tự dán vào ChatGPT / Gemini. Kết quả trả về theo ĐÚNG định
    dạng ``[{"b_id", "b_text"}]`` của ô dán kịch bản -> frontend đọc bằng ``docKichBanAI``
    (chịu xuống dòng, ngoặc kép trong câu, câu trả lời bị cắt ngang).

    Mặc định chép TOÀN BỘ câu cần dịch trong một prompt — giống prompt kịch bản (người
    dùng yêu cầu 29-09-2026). ``toi_da`` > 0 chỉ để cắt bớt khi cần."""
    from services.deepseek_service import LANG_NAMES, TARGET_LANG_VI

    lang = chuan_ma(lang)
    main_lang = chuan_ma(meta.get("target_language") or "vi")
    viec = can_dich(d, lang, b_chunks, pham_vi)
    phan = viec[:int(toi_da)] if toi_da and int(toi_da) > 0 else viec
    ten = TARGET_LANG_VI.get(lang, LANG_NAMES.get(lang, lang))
    ten_chinh = TARGET_LANG_VI.get(main_lang, LANG_NAMES.get(main_lang, "ngôn ngữ gốc của video"))
    dong = [
        {"b_id": b.get("b_id"), "b_text": " ".join(str(b.get("b_text") or "").split()),
         **({"src": s} if (s := _cau_goc(b, a_lookup)) else {})}
        for b in phan
    ]
    s = (
        f"Bạn là người dịch phụ đề video chuyên nghiệp. Dịch phụ đề từ {ten_chinh} sang "
        f"{ten} ({LANG_NAMES.get(lang, lang)}).\n\n"
        f"Mỗi câu gồm: \"b_id\" (số câu), \"b_text\" — câu phụ đề {ten_chinh} hiện trên màn "
        f"hình (ĐÂY là câu cần dịch), và có thể có \"src\" — câu tiếng gốc của video (chỉ để "
        f"tham khảo tên riêng, thuật ngữ, sắc thái; không dịch phần mà b_text đã bỏ).\n\n"
        f"Yêu cầu:\n"
        f"- Văn phong phụ đề {ten} tự nhiên, ngắn gọn, người bản xứ đọc nhanh được.\n"
        f"- Mỗi b_id đúng MỘT câu dịch; không gộp, không tách, không bỏ, không đổi thứ tự.\n"
        f"- Giữ tên riêng, xưng hô, thuật ngữ thống nhất giữa các câu.\n"
        f"- Không ghi chú, không phiên âm, không xuống dòng trong câu.\n"
        f"- CHỈ trả về một mảng JSON, không thêm chữ nào khác:\n"
        f"  [{{\"b_id\": <số>, \"b_text\": \"<câu {ten}>\"}}, ...]\n"
    )
    if brief.strip():
        s += "\nNGỮ CẢNH CẢ VIDEO (chỉ để tham khảo, không dịch, không đưa vào kết quả):\n" \
             + brief.strip() + "\n"
    s += (f"\nCÁC CÂU CẦN DỊCH ({len(dong)} câu):\n[\n"
          + ",\n".join(json.dumps(x, ensure_ascii=False) for x in dong) + "\n]")
    return {"prompt": s, "count": len(dong), "remaining": len(viec)}


# --------------------------------------------------------------------------- xuất
def doi_chu(segments: list, ban: dict) -> tuple:
    """Thay chữ của từng segment (có ``uid``) bằng bản dịch. Câu chưa dịch BỎ (không hiện)
    — chèn câu tiếng Việt vào giữa phụ đề tiếng Nhật là sai lặng lẽ. Trả (segs, số bỏ)."""
    out, bo = [], 0
    for s in segments or []:
        t = ban.get(str(s.get("uid") or ""))
        if not t:
            bo += 1
            continue
        out.append({**s, "source_text": t, "target_text": t})
    return out, bo


def _gio_srt(t: float) -> str:
    ms = max(0, int(round(float(t) * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_chu(segments: list) -> tuple:
    """``(nội dung .srt, số câu)`` — CRLF, bỏ câu rỗng / mốc hỏng."""
    n = 0
    dong = []
    for s in segments or []:
        #  Bỏ dòng TRỐNG bên trong câu (dán từ AI hay có hai lần xuống dòng liền): trong
        #  .srt dòng trống là hết cue -> phần sau bị đọc thành rác, lệch hết các cue sau (cả
        #  phụ đề mềm lẫn YouTube đọc chung tệp này).
        t = "\r\n".join(d.strip() for d in str(s.get("target_text") or "").splitlines() if d.strip())
        a, b = float(s.get("start_time") or 0), float(s.get("end_time") or 0)
        if not t or b <= a:
            continue
        n += 1
        dong += [str(n), f"{_gio_srt(a)} --> {_gio_srt(b)}", t, ""]
    return "\r\n".join(dong), n


def ghi_srt(path: str, segments: list) -> int:
    chu, n = srt_chu(segments)
    #  CRLF + BOM: định dạng SRT chuẩn; trình phát cũ trên Windows đọc đúng chữ Nhật/Hàn.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(chu)
    return n


# --------------------------------------------------------------------------- chọn khi xuất
def chon_xuat(data: dict, main_lang: str) -> dict:
    """Đọc lựa chọn thứ tiếng của một lượt xuất (chung Chi tiết / Cơ bản / lô):
    ``sub_lang`` (ĐỐT; trùng thứ tiếng chính = như cũ), ``second_sub_lang`` (dòng thứ hai —
    trùng dòng chính thì bỏ), ``srt_langs``, ``soft_langs``. ``can`` = mọi thứ tiếng cần dựng."""
    def _ds(k):
        return list(dict.fromkeys(chuan_ma(x) for x in (data.get(k) or []) if str(x or "").strip()))

    main = chuan_ma(main_lang or "vi")
    sub = chuan_ma(data.get("sub_lang") or "")
    sub = "" if sub == main else sub
    hai = chuan_ma(data.get("second_sub_lang") or "")
    hai = "" if hai == (sub or main) else hai
    srt, soft = _ds("srt_langs"), _ds("soft_langs")
    can = {*srt, *soft, *([sub] if sub else []), *([hai] if hai else [])}
    return {"main": main, "sub_lang": sub, "second_lang": hai, "srt_langs": srt,
            "soft_langs": soft, "can": can}


def theo_tieng(chon: dict, segs_chinh: list, d: dict) -> dict:
    """``{thứ tiếng: segments}`` cho mọi thứ tiếng ``chon["can"]``. Thứ tiếng chính = câu
    chính; thứ tiếng thêm = đổi chữ theo ``uid`` (câu chưa dịch bỏ trống). Thứ tiếng CHƯA
    THÊM -> ``ValueError`` (không xuất bừa)."""
    out = {}
    for lg in chon["can"]:
        if lg == chon["main"]:
            out[lg] = segs_chinh
            continue
        if lg not in (d.get("tracks") or {}):
            raise ValueError(L(f"Dự án chưa có bản phụ đề '{lg}'.", f"The project has no '{lg}' subtitle track yet."))
        doi, bo = doi_chu(segs_chinh, ban_dich(d, lg))
        if bo:
            logger.warning(L(f"[phụ đề nhiều thứ tiếng] '{lg}': {bo} câu chưa dịch — bỏ trống "
                           f"(không chèn câu thứ tiếng chính vào giữa).", f"[multi-language subtitles] '{lg}': {bo} sentences not translated — left blank (the main-language sentence is not inserted in between)."))
        out[lg] = doi
    return out


#: Mã ISO 639-2 cho thẻ ``language`` của luồng phụ đề mềm — trình phát (VLC, MPC, Phim &
#: TV, YouTube) đọc thẻ này để hiện tên thứ tiếng trong menu phụ đề.
MA_639_2 = {
    "vi": "vie", "en": "eng", "zh": "chi", "ja": "jpn", "ko": "kor", "fr": "fre",
    "de": "ger", "es": "spa", "ru": "rus", "pt": "por", "it": "ita", "th": "tha",
    "id": "ind",
}


def dat_co_bat_phu_de(path: str, bat: list) -> bool:
    """Đặt cờ BẬT SẴN (``enabled`` trong hộp ``tkhd``) của từng luồng phụ đề mềm của một MP4, theo thứ tự luồng: ``bat[i]`` = luồng phụ đề thứ i có bật sẵn không.

    VÌ SAO: bộ ghép MP4 của ffmpeg LUÔN bật sẵn luồng phụ đề ĐẦU TIÊN dù đã ``-disposition 0`` (kiểm trên ffmpeg thật: cả hai luồng đều ``-disposition 0`` mà luồng đầu vẫn ra
    ``(default)``). Video đã ĐỐT cứng tiếng chính (vd Việt) rồi nhúng thêm Nhật + Hàn: luồng Nhật bị bật sẵn, trình phát đọc cờ này (VLC, QuickTime, điện thoại) hiện chữ Nhật CHỒNG lên
    chữ Việt đã đốt — trong khi ý định là "chỉ thứ tiếng chính bật sẵn, còn lại để người xem tự chọn". Cờ nằm ngay trong hộp ``tkhd`` nên sửa tại chỗ 3 byte mỗi luồng, không ghép lại.

    An toàn: chỉ đọc cấu trúc hộp (moov → trak → tkhd / mdia → hdlr), SỐ LUỒNG phụ đề tìm được không khớp ``bat`` hoặc cấu trúc lạ thì KHÔNG sửa gì và trả False (video vẫn nguyên vẹn,
    chỉ là luồng đầu bật sẵn như bộ ghép mặc định)."""
    import struct

    try:
        with open(path, "r+b") as f:
            f.seek(0, 2)
            tong = f.tell()

            def hop(pos: int, het: int):
                while pos + 8 <= het:
                    f.seek(pos)
                    s, t = struct.unpack(">I4s", f.read(8))
                    hl = 8
                    if s == 1:
                        s, hl = struct.unpack(">Q", f.read(8))[0], 16
                    elif s == 0:
                        s = het - pos
                    if s < hl or pos + s > het:
                        return
                    yield t, pos, s, hl
                    pos += s

            moov = next(((p, s, hl) for t, p, s, hl in hop(0, tong) if t == b"moov"), None)
            if moov is None:
                return False
            tkhd_sub: list = []
            for t, p, s, hl in hop(moov[0] + moov[2], moov[0] + moov[1]):
                if t != b"trak":
                    continue
                tkhd, loai = None, None
                for t2, p2, s2, hl2 in hop(p + hl, p + s):
                    if t2 == b"tkhd":
                        tkhd = p2 + hl2
                    elif t2 == b"mdia":
                        for t3, p3, _s3, hl3 in hop(p2 + hl2, p2 + s2):
                            if t3 == b"hdlr":
                                f.seek(p3 + hl3 + 8)                                # version+flags (4) · pre_defined (4) · handler_type (4)
                                loai = f.read(4)
                if loai in (b"sbtl", b"text", b"subt") and tkhd is not None:
                    tkhd_sub.append(tkhd)
            if len(tkhd_sub) != len(bat):
                return False
            for pos, b in zip(tkhd_sub, bat):
                f.seek(pos + 1)                                                       # sau byte version: 3 byte cờ, bit 0x1 = enabled
                cu = int.from_bytes(f.read(3), "big")
                moi = (cu | 1) if b else (cu & ~1)
                if moi != cu:
                    f.seek(pos + 1)
                    f.write(moi.to_bytes(3, "big"))
        return True
    except (OSError, struct.error, ValueError) as e:
        logger.warning(L(f"[phụ đề mềm] Không đặt được cờ bật sẵn: {e}", f"[soft subtitles] Could not set the default-on flag: {e}"))
        return False


def nhung_phu_de_mem(output_path: str, langs: list, theo_tieng: dict, main_lang: str) -> str:
    """NHÚNG phụ đề MỀM (``mov_text``) vào MP4 vừa xuất — mỗi thứ tiếng một luồng, người
    xem tự bật/tắt. Chép nguyên hình + tiếng (``-c copy``), chỉ thêm luồng chữ: vài giây
    cho phim dài. Hỏng thì GIỮ bản xuất (đã có phụ đề cứng nếu bật) và trả câu lỗi."""
    import subprocess

    from services.compositor_service import FFMPEG_EXE, _side_file_stem

    goc = _side_file_stem(output_path)
    tam = []
    try:
        vao = []
        for lg in langs:
            duong = f"{goc}.soft.{lg}.srt"
            tam.append(duong)
            if ghi_srt(duong, theo_tieng.get(lg) or []) > 0:
                vao.append((lg, duong))
        if not vao:
            return L("Không có câu phụ đề nào để nhúng.", "There are no subtitle sentences to embed.")
        #  CÙNG thư mục với bản xuất: ``_side_file_stem`` có thể là %TEMP% (tên có dấu nháy)
        #  mà ``os.replace`` không chuyển được qua ổ đĩa khác. Đối số dòng lệnh không vướng
        #  dấu nháy nên tên thật dùng được.
        ra = os.path.splitext(output_path)[0] + ".soft_tmp.mp4"
        tam.append(ra)
        lenh = [FFMPEG_EXE, "-y", "-hide_banner", "-i", output_path]
        for _lg, duong in vao:
            lenh += ["-sub_charenc", "UTF-8", "-i", duong]
        lenh += ["-map", "0:v", "-map", "0:a?"]
        for i in range(len(vao)):
            lenh += ["-map", str(i + 1)]
        lenh += ["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text"]
        for i, (lg, _d) in enumerate(vao):
            lenh += [f"-metadata:s:s:{i}", f"language={MA_639_2.get(lg, lg)}",
                     #  MP4 lưu tên luồng ở ``handler_name`` (``title`` bị bỏ qua).
                     f"-metadata:s:s:{i}", f"title={nhan_thu_tieng(lg)}",
                     f"-metadata:s:s:{i}", f"handler_name={nhan_thu_tieng(lg)}",
                     #  Luồng thứ tiếng CHÍNH bật sẵn; còn lại để người xem tự chọn.
                     f"-disposition:s:{i}", "default" if lg == main_lang else "0"]
        lenh += ["-movflags", "+faststart", ra]
        kq = subprocess.run(lenh, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=1800)
        if kq.returncode != 0 or not os.path.exists(ra) or os.path.getsize(ra) == 0:
            duoi = (kq.stderr or "").strip().splitlines()[-1:] or ["?"]
            logger.error(L(f"[nâng cao] Nhúng phụ đề mềm lỗi: {duoi[0]}", f"[advanced] Embedding soft subtitles failed: {duoi[0]}"))
            return L(f"Nhúng phụ đề mềm lỗi: {duoi[0][:200]}", f"Embedding soft subtitles failed: {duoi[0][:200]}")
        os.replace(ra, output_path)
        tam.remove(ra)
        #  Chỉ luồng của thứ tiếng CHÍNH được bật sẵn (nếu có trong danh sách); ffmpeg tự bật luồng đầu dù không phải tiếng chính. Hỏng thì bỏ qua (video vẫn tốt).
        dat_co_bat_phu_de(output_path, [lg == main_lang for lg, _d in vao])
        logger.info(
            L(f"[nâng cao] Đã nhúng {len(vao)} luồng phụ đề mềm: {', '.join(lg for lg, _ in vao)}.", f"[advanced] Embedded {len(vao)} soft subtitle streams: {', '.join(lg for lg, _ in vao)}.")
        )
        return ""
    except Exception as e:  # noqa: BLE001
        logger.error(L(f"[nâng cao] Nhúng phụ đề mềm lỗi: {e}", f"[advanced] Embedding soft subtitles failed: {e}"))
        return L(f"Nhúng phụ đề mềm lỗi: {e}", f"Embedding soft subtitles failed: {e}")
    finally:
        for t in tam:
            try:
                os.remove(t)
            except OSError:
                pass
