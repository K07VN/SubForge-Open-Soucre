"""PHÂN TÍCH CÂU TRẢ LỜI CỦA AI (người dùng dán JSON về) -> ``Storyboard`` hợp lệ, CHỊU LỖI.

Trợ lý AI (ChatGPT, Gemini…) hay trả: JSON bọc trong ```json, có lời dẫn trước/sau, dấu nháy cong, dấu phẩy thừa,
chú thích ``//``, tên khoá tiếng Việt, màu bằng chữ ("đỏ"), nhãn là chuỗi thay vì đối tượng… Mục tiêu: nhận được
càng nhiều càng tốt, và khi KHÔNG nhận được thì báo rõ cảnh nào, trường nào — không sập, không đoán bừa.

Không gọi mạng, không đọc đĩa: hàm thuần, kiểm thử bằng chuỗi.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from services import sangtao_du_lieu_hinh as DL
from services import sangtao_ruby as RB
from services import sangtao_schema as SC
from services import sangtao_youtube as SY

KHOA_DS_CANH = ("scenes", "canh", "cảnh", "storyboard", "phan_canh", "phân_cảnh", "cac_canh", "các_cảnh", "sections", "parts", "lessons", "bai")
KHOA_HINH = ("visual", "hinh", "hình", "lesson", "content")
KHOA_KIEU = ("type", "kieu", "kiểu", "kind")
#: tên gọi khác của hai kiểu cảnh mà AI hay dùng
BI_DANH_KIEU = {"article": "reading", "text": "reading", "passage": "reading", "news": "reading", "story": "reading", "bai_doc": "reading", "bài_đọc": "reading",
                "dialogue": "reading", "conversation": "reading", "shadowing": "reading", "monologue": "reading"}


@dataclass
class KetQua:
    storyboard: Optional[SC.Storyboard] = None
    loi: list[str] = field(default_factory=list)
    canh_bao: list[str] = field(default_factory=list)
    ghi_chu: list[str] = field(default_factory=list)       # việc tự sửa ở bước tách JSON (để hiển thị cho người dùng)

    @property
    def ok(self) -> bool:
        return self.storyboard is not None and not self.loi


# ------------------------------------------------------------------ tách JSON khỏi văn bản
def _bo_chu_thich(t: str) -> str:
    """Gỡ chú thích ``//…`` và ``/*…*/`` NGOÀI chuỗi (không đụng tới "http://" nằm trong chuỗi)."""
    ra, i, n, trong = [], 0, len(t), False
    while i < n:
        c = t[i]
        if trong:
            ra.append(c)
            if c == "\\" and i + 1 < n:
                ra.append(t[i + 1])
                i += 2
                continue
            if c == '"':
                trong = False
            i += 1
        elif c == '"':
            trong = True
            ra.append(c)
            i += 1
        elif c == "/" and t[i:i + 2] == "//":
            while i < n and t[i] != "\n":
                i += 1
        elif c == "/" and t[i:i + 2] == "/*":
            j = t.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            ra.append(c)
            i += 1
    return "".join(ra)


def _lam_sach(t: str) -> str:
    t = t.replace("\ufeff", "").replace("\u200b", "")
    t = t.replace("“", '"').replace("”", '"').replace("„", '"').replace("‘", "'").replace("’", "'")
    t = _bo_chu_thich(t)
    t = re.sub(r",(\s*[}\]])", r"\1", t)                    # dấu phẩy thừa trước } hoặc ]
    return t


def trich_json(van_ban: str) -> tuple[Any, list[str]]:
    """Tìm đối tượng/mảng JSON lớn nhất trong văn bản. Trả (giá trị | None, ghi chú việc đã sửa)."""
    ghi_chu: list[str] = []
    t = (van_ban or "").strip()
    if not t:
        return None, ["Văn bản trống"]
    m = re.search(r"```(?:json|JSON)?\s*(.*?)```", t, flags=re.S)
    if m and m.group(1).strip().startswith(("{", "[")):
        t = m.group(1).strip()
        ghi_chu.append("Đã bỏ hàng rào markdown ```")
    sach = _lam_sach(t)
    bien_the = ((t, None), (sach, "Đã sửa dấu nháy cong / dấu phẩy thừa / chú thích"))
    #  1) đọc CẢ KHỐI (nguyên bản, rồi bản đã làm sạch) — phải thử hết trước khi đi tìm "mảnh JSON", nếu không một chú thích / nháy cong làm hỏng khối lớn thì
    #  bộ tìm mảnh sẽ nhặt nhầm một cảnh con rồi báo "không thấy danh sách cảnh".
    for bt, mo_ta in bien_the:
        try:
            v = json.loads(bt)
            if mo_ta and bt != t:
                ghi_chu.append(mo_ta)
            return v, ghi_chu
        except ValueError:
            pass
    #  2) có lời dẫn trước/sau: tìm khối JSON lớn nhất trong CẢ HAI bản (nguyên bản có thể đúng, bản làm sạch có thể đọc được khối lớn hơn)
    dec = json.JSONDecoder()
    tot, lon, nhan = None, 0, ""
    for bt, mo_ta in bien_the:
        for mt in list(re.finditer(r"[{\[]", bt))[:80]:
            try:
                v, _end = dec.raw_decode(bt, mt.start())
            except ValueError:
                continue
            if isinstance(v, (dict, list)):
                co = len(json.dumps(v, ensure_ascii=False))
                if co > lon:
                    tot, lon, nhan = v, co, (mo_ta if bt != t else "") or ""
    if tot is not None:
        ghi_chu.append("Đã bỏ lời dẫn trước/sau JSON" + (f"; {nhan.lower()}" if nhan else ""))
        return tot, ghi_chu
    return None, ghi_chu + ["Không tìm thấy JSON hợp lệ trong văn bản dán vào"]




# ------------------------------------------------------------------ chuẩn hoá từng phần
def _lay(d: dict, khoa: tuple[str, ...]) -> Any:
    for k in khoa:
        if k in d and d[k] not in (None, ""):
            return d[k]
    low = {str(k).lower(): v for k, v in d.items()}
    for k in khoa:
        if k.lower() in low and low[k.lower()] not in (None, ""):
            return low[k.lower()]
    return None


def _loi_doc_tu_du_lieu(du_lieu: dict) -> str:
    """Lời đọc xem nhanh của cảnh, ghép từ các câu (AI không phải viết lại)."""
    return " ".join(ln["text"] for ln in du_lieu["lines"])


def _doan_kieu(h: dict, c: dict) -> str:
    """Kiểu cảnh: theo khoá ``type`` (kể cả tên gọi khác), không có thì đoán theo dữ liệu (có ``lines`` → bài đọc)."""
    k = str(_lay(h, KHOA_KIEU) or _lay(c, KHOA_KIEU) or "").strip().lower().replace("-", "_").replace(" ", "_")
    k = BI_DANH_KIEU.get(k, k)
    if k in SC.KIEU_HINH:
        return k
    for nguon in (h.get("data"), h, c.get("data"), c):
        if isinstance(nguon, dict) and any(isinstance(nguon.get(x), list) for x in ("lines", "sentences", "dialogue", "turns")):
            return "reading"
    return k


def _chuan_hoa_canh(c: Any, so: int, loi: list[str], cb: list[str]) -> Optional[dict]:
    if not isinstance(c, dict):
        loi.append(f"Cảnh {so}: không phải một đối tượng JSON")
        return None
    hv = _lay(c, KHOA_HINH)
    h = dict(hv) if isinstance(hv, dict) else {}
    kieu = _doan_kieu(h, c)
    if kieu not in SC.KIEU_HINH:
        loi.append(f"Cảnh {so}: không nhận ra kiểu cảnh '{kieu or '?'}' — chỉ còn bài đọc (reading) với danh sách câu \"lines\"")
        return None
    raw = h.get("data") if h.get("data") not in (None, "", [], {}) else (c.get("data") if c.get("data") not in (None, "", [], {}) else h.get(kieu))
    goc_ = dict(raw) if isinstance(raw, dict) else {}
    #  AI hay để phẳng: các trường nằm ngay ở mức visual hoặc mức cảnh
    cac_truong = ("title", "title_tr", "situation", "situation_tr", "speakers", "lines", "sentences", "dialogue", "turns", "intro", "outro", "bridges", "transitions", "quiz", "questions")
    for nguon in (h, c):
        for f_ in cac_truong:
            if f_ not in goc_ and nguon.get(f_) is not None:
                goc_[f_] = nguon[f_]
    du_lieu, cb_dl = DL.chuan_hoa(kieu, goc_ or None)
    cb += [f"Cảnh {so}: {x}" for x in cb_dl]
    if du_lieu is None:
        loi.append(f"Cảnh {so}: thiếu hoặc sai dữ liệu của bài đọc ({'; '.join(cb_dl) or 'không có dữ liệu'})")
        return None
    kq = {"id": so, "voiceover": _loi_doc_tu_du_lieu(du_lieu)[:SC.MAX_LOI_DOC], "visual": {"type": kieu, "data": du_lieu}}
    tl = _lay(c, ("duration_s", "duration", "thoi_luong", "thời_lượng"))
    try:
        if tl is not None and float(tl) > 0:
            kq["duration_s"] = float(tl)
    except (TypeError, ValueError):
        pass
    return kq


# ------------------------------------------------------------------ chữ Hán chưa có cách đọc
_CUM_HAN = re.compile("[一-鿿々]+")


def _cho_thieu_cach_doc(van: Any) -> int:
    """Số cụm chữ Hán KHÔNG có cách đọc ``[kana]`` đi kèm trong một chuỗi tiếng Nhật."""
    return sum(len(_CUM_HAN.findall(b)) for b, r in RB.phan_tich(str(van or "")) if not r)


def _canh_bao_thieu_cach_doc(canh: list) -> list[str]:
    """Cảnh tiếng Nhật còn chữ Hán chưa có cách đọc: video chỉ hiện furigana ở chỗ có ``[kana]`` (bộ dựng hình KHÔNG tự đoán cách đọc — đọc sai còn tệ hơn không có), nên nói rõ trước khi dựng.
    Chia theo nơi thiếu để người dùng biết nhờ AI bổ sung phần nào (câu của bài · câu dẫn dắt · lời giảng — hai thứ sau hiện thành phụ đề ở đáy video)."""
    ra = []
    for c in canh:
        d = (c.get("visual") or {}).get("data") or {}
        dem = {"câu của bài": 0, "câu dẫn dắt": 0, "lời giảng": 0}
        for ln in d.get("lines") or []:
            dem["câu của bài"] += _cho_thieu_cach_doc(ln.get("text"))
            dem["câu dẫn dắt"] += _cho_thieu_cach_doc(ln.get("lead"))
            for it in list(ln.get("vocab") or []) + list(ln.get("grammar") or []):
                dem["lời giảng"] += _cho_thieu_cach_doc(it.get("say"))
        if any(dem.values()):
            ra.append(f"Cảnh {c['id']}: còn chữ Hán chưa có cách đọc [kana] — " + ", ".join(f"{k} {n} chỗ" for k, n in dem.items() if n)
                      + ". Video chỉ hiện furigana ở chỗ có cách đọc; muốn đủ thì nhờ AI bổ sung furigana rồi dán lại.")
    return ra


# ------------------------------------------------------------------ cổng vào
def phan_tich(van_ban: str, mac_dinh: Optional[dict] = None) -> KetQua:
    """Văn bản AI trả về -> :class:`KetQua`. ``mac_dinh`` (ngôn ngữ, tỷ lệ, đối tượng, giọng) LUÔN thắng giá trị AI tự điền: đó là lựa chọn của người dùng ở bước 1."""
    kq = KetQua()
    obj, kq.ghi_chu = trich_json(van_ban)
    if obj is None:
        kq.loi = list(kq.ghi_chu[-1:]) or ["Không đọc được JSON"]
        kq.ghi_chu = kq.ghi_chu[:-1]
        return kq
    goc: dict[str, Any] = {}
    if isinstance(obj, dict):
        goc = obj
        ds = _lay(obj, KHOA_DS_CANH)
        if isinstance(ds, dict):                              # {"1": {...}, "2": {...}}
            ds = [ds[k] for k in sorted(ds, key=lambda x: (len(str(x)), str(x)))]
    elif isinstance(obj, list):
        ds = obj
    else:
        ds = None
    if not isinstance(ds, list) or not ds:
        kq.loi = ["Không thấy danh sách cảnh (khoá 'scenes') trong JSON"]
        return kq
    if len(ds) > SC.MAX_CANH:
        kq.loi = [f"Quá nhiều cảnh ({len(ds)}), tối đa {SC.MAX_CANH}. Hãy chia kịch bản thành nhiều phần"]
        return kq
    canh = []
    for i, c in enumerate(ds, 1):
        x = _chuan_hoa_canh(c, i, kq.loi, kq.canh_bao)
        if x:
            canh.append(x)
    if kq.loi:
        return kq
    md = mac_dinh or {}
    sb = {"version": SC.PHIEN_BAN, "scenes": canh,
          "language": md.get("language") or goc.get("language") or "vi",
          "aspect": md.get("aspect") or goc.get("aspect") or "16:9",
          "audience": md.get("audience") or goc.get("audience") or "general",
          "voice": md.get("voice") or goc.get("voice"),
          "title": goc.get("title") if isinstance(goc.get("title"), str) else None,
          "youtube": _lay(goc, SY.KHOA_YT)}                    # thông tin đăng YouTube AI soạn kèm (chỉ phần cuối của bài); schema tự dọn
    sb_obj, loi = SC.kiem_tra(sb)
    kq.storyboard, kq.loi = sb_obj, loi
    for n, moi in SY.tieu_de_bi_rut(sb["youtube"]):                      # nói thẳng khi app phải rút tiêu đề (không để người dùng tự phát hiện "…từ vựng v")
        kq.canh_bao.append(f"Tiêu đề YouTube dài {n} ký tự (YouTube tối đa {SY.TRAN_TIEU_DE}): app đã rút còn {len(moi)} ký tự — “{moi}”. Muốn giữ nguyên ý thì nhờ AI viết lại ngắn hơn.")
    if kq.storyboard is not None and str(sb["language"]) == "ja":
        kq.canh_bao += _canh_bao_thieu_cach_doc(canh)
    return kq
