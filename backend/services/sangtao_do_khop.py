"""ĐỘ KHỚP VỚI BÀI GỐC của kịch bản "Sáng tạo" — đối chiếu THẬT từng câu với chữ của bài, không tin cờ ``quote`` do AI tự gán.

Vì sao có module này (05-10-2026): chế độ "Giữ nguyên văn" từng báo CỨNG "Câu trích nguyên văn: 22/22 (100%)" (giao diện đặt ``so = tổng số câu``) trong khi AI đã viết lại bài thành những câu rời
rạc, có câu lặp, có câu không hề có trong bài. Con số tự khai đó gây hiểu nhầm — nhất là khi nó đi kèm lời khẳng định về bản quyền. Ở đây đo bằng chữ:

  · ``khop``  — câu (đã bỏ furigana, dấu câu, khoảng trắng, chuẩn hoá NFKC) là một đoạn LIỀN của bài gốc → đúng nguyên văn;
  · ``gan``   — không phải đoạn liền nhưng ≥ 60% các cụm chữ liên tiếp của câu có trong bài (đã sửa nhẹ / ghép từ vài chỗ);
  · ``lech``  — còn lại: câu KHÔNG có trong bài gốc (AI tự viết / viết lại / bịa);
  · ``lap``   — trùng một câu đứng trước (trừ khi chính bài gốc có câu đó nhiều lần).
Ngoài ra: thứ tự có đúng bài gốc không, bao nhiêu phần bài gốc được đọc tới, những câu của bài gốc bị bỏ sót, và (cho chế độ viết lại) số câu "tự viết" mà thực ra chép lại bài gốc.
``cat_nho`` — số câu "khop" chỉ là MỘT MẢNH của một câu gốc (AI tách câu ở dấu phẩy / băm nhỏ): ở "Giữ nguyên văn" mỗi câu của bài phải là một dòng nguyên vẹn, kể cả đoạn trích trong 「…」 dài hàng trăm chữ
(yêu cầu 05-10-2026 sau khi thấy câu trích NHK bị băm nhỏ).

Chỉ đối chiếu được khi bài gốc và kịch bản cùng HỆ CHỮ (vd bài tiếng Nhật → video tiếng Nhật). Bài gốc tiếng Anh dịch sang tiếng Nhật thì không so từng chữ được — nói rõ chứ không giả vờ.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from services import sangtao_ruby as RB

_BO_KY_TU = re.compile(r"[\W_]+", re.UNICODE)               # dấu câu, ngoặc, khoảng trắng, ký hiệu
_CAT_CAU = re.compile(r"(?<=[。！？!?])|\n+|(?<=\.)\s+")
NGUONG_GAN = 0.6
DO_DAI_TOI_THIEU = 6                                         # câu chuẩn hoá ngắn hơn mức này thì không xét "lặp" (vd "はい。")


def chuan_hoa(s: Any) -> str:
    """Chữ để SO SÁNH: bỏ furigana ``[…]``, NFKC (７万 → 7万, ｱ → ア), hạ chữ thường, bỏ mọi dấu câu / khoảng trắng."""
    t = RB.bo_ruby(str(s or ""))
    return _BO_KY_TU.sub("", unicodedata.normalize("NFKC", t).casefold())


_MO, _DONG = "「『(（“", "」』)）”"


def _cau_goc(goc: str) -> list[str]:
    """Các CÂU của bài gốc: cắt sau 。！？!? và xuống dòng, nhưng KHÔNG cắt khi đang ở trong ngoặc 「」『』（）“” — đoạn phát biểu dài trong ngoặc là MỘT câu."""
    ra: list[str] = []
    cur: list[str] = []
    sau = 0
    for ch in str(goc or ""):
        cur.append(ch)
        if ch in _MO:
            sau += 1
        elif ch in _DONG:
            sau = max(0, sau - 1)
        if ch == "\n" or (ch in "。！？!?" and sau == 0):
            ra.append("".join(cur))
            cur, sau = [], 0
    if cur:
        ra.append("".join(cur))
    return ra


def he_chu(s: str) -> str:
    """Hệ chữ chiếm đa số: "cjk" (kana + Hán) · "hangul" · "cyrillic" · "latin" · "khac" (quá ít chữ để biết)."""
    dem = {"cjk": 0, "hangul": 0, "cyrillic": 0, "latin": 0}
    for c in s:
        o = ord(c)
        if 0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF:
            dem["cjk"] += 1
        elif 0xAC00 <= o <= 0xD7AF:
            dem["hangul"] += 1
        elif 0x0400 <= o <= 0x04FF:
            dem["cyrillic"] += 1
        elif c.isalpha() and o < 0x250:
            dem["latin"] += 1
    tong = sum(dem.values())
    if tong < 10:
        return "khac"
    return max(dem, key=lambda k: dem[k])


def _rong(ly_do: str, tong: int) -> dict:
    return {"co_doi_chieu": False, "ly_do": ly_do, "tong": tong, "khop": 0, "gan": 0, "lech": 0, "lap": 0, "sai_thu_tu": 0, "phu_bai": 0.0, "thieu": 0, "thieu_mau": [],
            "chep_khong_ghi_trich": 0, "cat_nho": 0, "so_cau_goc": 0, "dong": []}


def do_khop(goc: str, cac_dong: list[dict]) -> dict:
    """Đối chiếu ``cac_dong`` (``[{"text", "quote"?}]`` theo thứ tự phát) với bài gốc ``goc``.

    Trả ``{co_doi_chieu, ly_do, tong, khop, gan, lech, lap, sai_thu_tu, phu_bai (0–1), thieu, thieu_mau, chep_khong_ghi_trich, dong}`` — ``dong`` là nhãn từng câu
    ("khop" | "gan" | "lech" | "lap"), CÙNG thứ tự ``cac_dong``, để giao diện đánh dấu đúng câu nào có vấn đề."""
    tong = len(cac_dong)
    if not tong:
        return _rong("Chưa có câu nào để đối chiếu.", 0)
    g = chuan_hoa(goc)
    if len(g) < 20:
        return _rong("Chưa có nội dung bài gốc (ô nội dung ở tab 1 đang trống) nên chưa đối chiếu được.", tong)
    txt = [chuan_hoa(d.get("text")) for d in cac_dong]
    he_g, he_d = he_chu(g), he_chu("".join(txt))
    if he_g != he_d and "khac" not in (he_g, he_d):
        return _rong("Bài gốc và kịch bản viết bằng hệ chữ khác nhau (kịch bản là bản DỊCH) nên không đối chiếu từng chữ được.", tong)

    khoang: list[tuple[int, int]] = []                         # [đầu, cuối) của từng CÂU gốc trong chuỗi đã chuẩn hoá (để biết dòng nào chỉ là một mảnh của câu)
    cuon_c = 0
    for cau in _cau_goc(goc):
        c = chuan_hoa(cau)
        if len(c) < DO_DAI_TOI_THIEU:
            continue
        v0 = g.find(c, cuon_c)
        if v0 < 0:
            continue
        khoang.append((v0, v0 + len(c)))
        cuon_c = v0 + len(c)
    n = 4 if he_g in ("cjk", "hangul") else 6
    gram_goc = {g[i:i + n] for i in range(len(g) - n + 1)}
    phu = bytearray(len(g))                                    # 1 = ký tự của bài gốc đã được đọc bằng một câu "khop"
    con_tro = 0                                                # vị trí câu "khop" trước trong bài gốc — để biết có đúng thứ tự không
    nhan: list[str] = []
    da_co: set[str] = set()
    ra = _rong("", tong)
    ra["co_doi_chieu"] = True
    for d, t in zip(cac_dong, txt):
        if not t:                                              # câu trống chữ: coi là không có trong bài
            nhan.append("lech")
            ra["lech"] += 1
            continue
        if len(t) >= DO_DAI_TOI_THIEU and t in da_co and g.count(t) < 2:
            nhan.append("lap")
            ra["lap"] += 1
            continue
        da_co.add(t)
        vt = g.find(t, con_tro)
        if vt < 0:
            vt2 = g.find(t)
            if vt2 >= 0:                                       # có trong bài nhưng NGƯỢC thứ tự so với câu trước
                ra["sai_thu_tu"] += 1
                vt = vt2
        if vt >= 0:
            nhan.append("khop")
            ra["khop"] += 1
            for v0, v1 in khoang:
                if v0 <= vt < v1:
                    if len(t) < 0.9 * (v1 - v0) and len(t) >= DO_DAI_TOI_THIEU:     # chỉ một MẢNH của câu gốc (cắt ở dấu phẩy / băm nhỏ)
                        ra["cat_nho"] += 1
                    break
            for i in range(vt, vt + len(t)):
                phu[i] = 1
            if vt >= con_tro:
                con_tro = vt + len(t)
            if not d.get("quote"):
                ra["chep_khong_ghi_trich"] += 1
            continue
        grams = [t[i:i + n] for i in range(len(t) - n + 1)]
        cov = (sum(1 for x in grams if x in gram_goc) / len(grams)) if grams else 0.0
        if cov >= NGUONG_GAN:
            nhan.append("gan")
            ra["gan"] += 1
            if not d.get("quote"):
                ra["chep_khong_ghi_trich"] += 1
        else:
            nhan.append("lech")
            ra["lech"] += 1
    ra["dong"] = nhan
    ra["so_cau_goc"] = len(khoang)
    ra["phu_bai"] = round(sum(phu) / len(g), 3)

    #  Câu của bài gốc không được đọc tới (chỉ để THAM KHẢO: bài gốc có thể chứa tiêu đề, ngày giờ, chú thích… mà người dùng không muốn đọc).
    thieu, mau, cuon = 0, [], 0
    for cau in _CAT_CAU.split(str(goc or "")):
        c = chuan_hoa(cau)
        if len(c) < 8:
            continue
        vt = g.find(c, cuon)
        if vt < 0:
            continue
        cuon = vt + len(c)
        if sum(phu[vt:vt + len(c)]) < 0.5 * len(c):
            thieu += 1
            if len(mau) < 3:
                mau.append(cau.strip()[:50])
    ra["thieu"], ra["thieu_mau"] = thieu, mau
    return ra


def dong_cua_du_an(du_an: dict) -> list[dict]:
    """Các câu ``lines`` của mọi cảnh bài đọc, theo thứ tự phát (cùng thứ tự giao diện ``flatMap(dongCua)``)."""
    ra: list[dict] = []
    for c in ((du_an.get("storyboard") or {}).get("scenes") or []):
        v = c.get("visual") or {}
        if v.get("type") != "reading":
            continue
        for ln in (v.get("data") or {}).get("lines") or []:
            if isinstance(ln, dict):
                ra.append({"text": ln.get("text") or "", "quote": bool(ln.get("quote"))})
    return ra
