"""CHUẨN HOÁ dữ liệu của kiểu cảnh duy nhất: BÀI ĐỌC (``reading``) — các lượt đọc của 1–4 người (người dẫn + người được trích lời).

AI chỉ điền dữ liệu thô; ở đây dọn cho gọn và chặt (kiểu, độ dài, số mục) để bộ dựng không bao giờ gặp dữ liệu hỏng.
Thuần Python (không cv2) để schema, trình phân tích và test dùng chung. Nguyên tắc: dữ liệu sai/thiếu → trả ``None`` kèm cảnh báo tiếng Việt,
KHÔNG tự bịa nội dung để lấp chỗ trống. Chuẩn hoá phải IDEMPOTENT (đưa kết quả vào lại cho ra đúng kết quả đó) vì schema chuẩn hoá mỗi lần nạp.
"""
from __future__ import annotations

import re
from typing import Any, Optional

KIEU_DO_HOA = ("reading",)
SO_NGUOI_TOI_DA = 4
SO_TU_TOI_DA = 6                     # từ vựng bóc tách tối đa mỗi câu (slide bài giảng)
SO_NP_TOI_DA = 3                     # mẫu ngữ pháp tối đa mỗi câu
SO_LUOT_TOI_DA = 40                  # số câu tối đa trong MỘT cảnh (bài dài chia nhiều cảnh)
LOI_NOI_TOI_DA = 320                 # độ dài tối đa (ký tự) của một lời dẫn / lời giảng nói thành tiếng; lời chào mở đầu cho phép dài hơn (xem LOI_CHAO_TOI_DA)
LOI_CHAO_TOI_DA = 700
# Giới hạn độ dài MỘT câu bài đọc (và phiên âm / bản dịch của nó). Chế độ "Giữ nguyên văn" phải giữ trọn câu gốc (câu trích của báo NHK dài ~230 chữ,
# romaji của nó ~420 ký tự) — cắt ở đây là mất chữ âm thầm. Bộ dựng tự co cỡ chữ / cuộn để chứa hết, nên giới hạn chỉ là chốt chặn chống dữ liệu rác.
CAU_TOI_DA = 600
PHIEN_AM_TOI_DA = 1000
# Tiêu đề cảnh (+ bản dịch): giới hạn cũ 48 / 60 ký tự CẮT THẦM giữa chữ ngay lúc nhập JSON (tiêu đề báo Nhật dài hơn thế). Nay chỉ là chốt chặn; bộ dựng tự co chữ và thêm "…" khi quá bề ngang.
TIEU_DE_TOI_DA = 80
TIEU_DE_DICH_TOI_DA = 100
DICH_TOI_DA = 700
SO_CAU_HOI_TOI_DA = 6                # câu hỏi nghe hiểu tối đa mỗi cảnh (lượt "hoi")
MAC_GIOI = ("female", "male", "female", "male")      # người dẫn mặc định là nữ, rồi xen kẽ


def _chuoi(v: Any, toi_da: int) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()[:toi_da]


def so(v: Any) -> Optional[float]:
    """'29,1%' / '1.234,5' / '1,234.5' / 29 → float; không đọc được → None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^\d,.\-+eE]", "", str(v or "")).strip()
    if not t or t in "+-.,":
        return None
    if "," in t and "." in t:                                   # dấu cuối cùng là dấu thập phân
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        phan = t.split(",")
        if len(phan) > 2 or (len(phan[-1]) == 3 and 1 <= len(phan[0].lstrip("+-")) <= 3):
            t = "".join(phan)
        else:
            t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def chuan_hoa(kieu: str, raw: Any) -> tuple[Optional[dict], list[str]]:
    """(dữ liệu sạch | None, cảnh báo). Không bao giờ ném lỗi."""
    cb: list[str] = []
    try:
        ham = {"reading": _bai_doc}[kieu]
        return ham(raw, cb), cb
    except Exception as e:  # noqa: BLE001 — dữ liệu AI bậy đến đâu cũng không được làm sập việc nhập
        return None, cb + [f"dữ liệu '{kieu}' không đọc được ({e.__class__.__name__})"]


def _phien_am(x: dict) -> str:
    return _chuoi(x.get("phonetic") or x.get("pinyin") or x.get("romaji") or x.get("romanization") or x.get("pronunciation") or x.get("reading"), PHIEN_AM_TOI_DA)


def _tu_vung(raw: Any) -> list[dict]:
    """Từ vựng bóc tách của một câu: [{w: từ (tiếng Nhật kèm furigana 食[た]べる), r: cách đọc (pinyin / romaji / IPA), m: nghĩa}] — tối đa 4. Nhận cả chuỗi "từ = nghĩa"."""
    ra: list[dict] = []
    for x in (raw if isinstance(raw, list) else [])[:SO_TU_TOI_DA + 4]:
        if isinstance(x, str):
            w, m = (re.split(r"\s*[=:：–—]\s*", x, maxsplit=1) + [""])[:2]
            x = {"w": w, "m": m}
        if not isinstance(x, dict):
            continue
        w = _chuoi(x.get("w") or x.get("word") or x.get("term"), 40)
        m = _chuoi(x.get("m") or x.get("meaning") or x.get("translation"), 60)
        if w and m:
            muc = {"w": w, "r": _chuoi(x.get("r") or x.get("reading") or x.get("pinyin") or x.get("romaji") or x.get("phonetic") or x.get("pronunciation"), 50), "m": m}
            say = _chuoi(x.get("say") or x.get("speak") or x.get("spoken") or x.get("explain"), LOI_NOI_TOI_DA)
            if say:                                                           # lời giảng nói thành tiếng (chỉ nghe, không hiện) — thiếu thì đọc đúng nghĩa ngắn "m"
                muc["say"] = say
            ra.append(muc)
    return ra[:SO_TU_TOI_DA]


def _ngu_phap(raw: Any) -> list[dict]:
    """Mẫu ngữ pháp của một câu: [{p: mẫu câu, e: giải thích ngắn}] — tối đa 2."""
    ra: list[dict] = []
    for x in (raw if isinstance(raw, list) else [])[:SO_NP_TOI_DA + 2]:
        if isinstance(x, str):
            x = {"p": x, "e": ""}
        if not isinstance(x, dict):
            continue
        p = _chuoi(x.get("p") or x.get("pattern") or x.get("structure") or x.get("point"), 50)
        e = _chuoi(x.get("e") or x.get("explanation") or x.get("meaning") or x.get("note"), 140)
        if p and e:
            muc = {"p": p, "e": e}
            say = _chuoi(x.get("say") or x.get("speak") or x.get("spoken") or x.get("explain"), LOI_NOI_TOI_DA)
            if say:
                muc["say"] = say
            ra.append(muc)
    return ra[:SO_NP_TOI_DA]


def _cau_hoi(raw: Any) -> list[dict]:
    """``quiz``: [{q: câu hỏi (tiếng đích), a: đáp án (tiếng đích), q_tr / a_tr: bản dịch hiện trên màn hình}] — mục thiếu câu hỏi hoặc đáp án bị bỏ."""
    ra: list[dict] = []
    for x in (raw if isinstance(raw, list) else [])[:SO_CAU_HOI_TOI_DA + 2]:
        if not isinstance(x, dict):
            continue
        q = _chuoi(x.get("q") or x.get("question"), 220)
        a = _chuoi(x.get("a") or x.get("answer"), 260)
        if q and a:
            ra.append({"q": q, "a": a, "q_tr": _chuoi(x.get("q_tr") or x.get("question_tr"), 220), "a_tr": _chuoi(x.get("a_tr") or x.get("answer_tr"), 260)})
    return ra[:SO_CAU_HOI_TOI_DA]


#: tên khoá thay thế AI hay dùng cho lời chuyển của từng lượt
_KHOA_LUOT = {"doc": ("doc", "read", "reading", "1"), "dich": ("dich", "translation", "translate", "2"), "hoi": ("hoi", "quiz", "questions", "3"),
              "giang": ("giang", "lesson", "lecture", "4"), "shadow": ("shadow", "shadowing", "5")}


def _chuyen_luot(raw: Any) -> dict:
    """``bridges``: {lượt: lời chuyển} — chỉ giữ lượt có chữ."""
    ra: dict = {}
    if isinstance(raw, dict):
        for pk, khoa in _KHOA_LUOT.items():
            v = _chuoi(next((raw[k] for k in khoa if raw.get(k) not in (None, "")), ""), LOI_NOI_TOI_DA)
            if v:
                ra[pk] = v
    return ra


def _bai_doc(raw: Any, cb: list[str]) -> Optional[dict]:
    """BÀI ĐỌC: 1–4 người đọc (người dẫn + người được trích lời), 1–40 lượt đọc, mỗi lượt có câu + phiên âm + bản dịch."""
    if not isinstance(raw, dict):
        return None
    ds = raw.get("lines") or raw.get("sentences") or raw.get("turns") or raw.get("dialogue")
    if not isinstance(ds, list):
        cb.append("bài đọc thiếu danh sách câu (lines)")
        return None
    nguoi: list[dict] = []
    for x in (raw.get("speakers") if isinstance(raw.get("speakers"), list) else [])[:SO_NGUOI_TOI_DA]:
        ten = _chuoi(x.get("name") if isinstance(x, dict) else x, 14)
        g = str((x.get("gender") if isinstance(x, dict) else "") or "").strip().lower()
        nguoi.append({"name": ten, "gender": g if g in ("male", "female") else None})
    ten_idx = {n["name"].lower(): i for i, n in enumerate(nguoi) if n["name"]}
    lines: list[dict] = []
    for x in ds[:SO_LUOT_TOI_DA]:
        if not isinstance(x, dict):
            continue
        text = _chuoi(x.get("text") or x.get("line") or x.get("sentence"), CAU_TOI_DA)
        if not text:
            continue
        who = x.get("speaker", x.get("s", x.get("who")))
        f = so(who) if who is not None and not isinstance(who, bool) else None
        if f is not None and 0 <= int(f) < SO_NGUOI_TOI_DA:
            idx = int(f)
        elif isinstance(who, str) and who.strip().lower() in ten_idx:
            idx = ten_idx[who.strip().lower()]
        elif isinstance(who, str) and who.strip().upper() in ("A", "B", "C", "D"):
            idx = "ABCD".index(who.strip().upper())
        else:
            idx = len(lines) % max(len(nguoi), 1)                                                # không ghi người nói: xen kẽ giữa các người đã khai báo
        dong = {"s": min(idx, SO_NGUOI_TOI_DA - 1), "text": text, "phonetic": _phien_am(x),
                "translation": _chuoi(x.get("translation") or x.get("meaning"), DICH_TOI_DA),
                "vocab": _tu_vung(x.get("vocab") or x.get("vocabulary") or x.get("words")), "grammar": _ngu_phap(x.get("grammar") or x.get("patterns")),
                "quote": bool(x.get("quote") or x.get("quoted") or x.get("verbatim"))}
        lead = _chuoi(x.get("lead") or x.get("hook") or x.get("intro"), LOI_NOI_TOI_DA)
        if lead:                                                                    # câu dẫn dắt trước phần từ vựng / ngữ pháp của câu (chỉ nghe)
            dong["lead"] = lead
        lines.append(dong)
    if not lines:
        cb.append("bài đọc cần ít nhất 1 câu có chữ")
        return None
    n = min(SO_NGUOI_TOI_DA, max(len(nguoi), max(ln["s"] for ln in lines) + 1))
    while len(nguoi) < n:
        nguoi.append({"name": "", "gender": None})
    for i, p in enumerate(nguoi):
        p["name"] = p["name"] or "ABCD"[i]
        p["gender"] = p["gender"] or MAC_GIOI[i]
    ra = {"title": _chuoi(raw.get("title") or raw.get("situation"), TIEU_DE_TOI_DA), "title_tr": _chuoi(raw.get("title_tr") or raw.get("situation_tr"), TIEU_DE_DICH_TOI_DA),
          "speakers": nguoi, "lines": lines}
    for khoa, toi_da in (("intro", LOI_CHAO_TOI_DA), ("outro", LOI_NOI_TOI_DA)):    # lời chào mở đầu / lời kết (giọng tiếng mẹ đẻ) — chỉ ghi khi AI có viết
        v = _chuoi(raw.get(khoa), toi_da)
        if v:
            ra[khoa] = v
    br = _chuyen_luot(raw.get("bridges") or raw.get("transitions"))
    if br:
        ra["bridges"] = br
    quiz = _cau_hoi(raw.get("quiz") or raw.get("questions"))
    if quiz:
        ra["quiz"] = quiz
    return ra
