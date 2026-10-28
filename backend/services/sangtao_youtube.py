"""THÔNG TIN ĐĂNG YOUTUBE của một dự án "Sáng tạo", do AI soạn CÙNG LÚC với kịch bản (bước 2 — dán JSON) để khi thêm video vào tab YouTube thì điền sẵn, không phải soạn lại.

Nằm trong JSON của AI dưới khoá ``youtube`` (cùng cấp với ``title`` / ``scenes``) với bốn phần giống màn soạn bài của tab YouTube (``services/youtube_meta.py``):
``tieu_de`` (5 phương án) · ``the`` · ``hashtag`` · ``hook_anh`` (chữ ngắn đắp lên ảnh bìa). MÔ TẢ thì app tự dựng (``sangtao_du_an.mo_ta_video``: bản quyền + link gốc + hashtag) nên AI không viết.
Dùng lại ``youtube_meta.chuan_hoa`` để bản dán tay và bản của tab YouTube bị cùng một luật (không có tiêu đề rỗng "…", thẻ cắt theo cách YouTube đếm).
"""
from __future__ import annotations

import re
from typing import Any, Optional

#: tên gọi khác của khoá ngoài và bốn phần bên trong mà AI hay dùng
KHOA_YT = ("youtube", "youtube_meta", "yt")
_KHOA_TRONG = {
    "tieu_de": ("tieu_de", "titles", "title_options", "title"),
    "the": ("the", "tags", "keywords"),
    "hashtag": ("hashtag", "hashtags"),
    "hook_anh": ("hook_anh", "hooks", "hook", "thumbnail_text", "thumbnail"),
}
TRAN_TIEU_DE = 100                    # YouTube cho tiêu đề tối đa 100 ký tự


def _lay(d: dict, khoa: tuple) -> Any:
    for k in khoa:
        if k in d:
            return d[k]
    return None


_FURIGANA = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf々〆]+)(?:\[[\u3041-\u3096\u30a1-\u30faー・]+\]|（[\u3041-\u3096\u30a1-\u30faー・]+）)")


def _bo_furigana(t: str) -> str:
    """``否認[ひにん]`` → ``否認`` (chỉ ngoặc kana đứng NGAY SAU chữ Hán); ngoặc khác giữ nguyên."""
    return _FURIGANA.sub(r"\1", t or "").strip()


_NGAY_CUOI = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s*\)?\s*$")


def rut_tieu_de(t: str, tran: int = TRAN_TIEU_DE) -> str:
    """Rút tiêu đề quá ``tran`` ký tự cho GỌN chứ không chặt cụt giữa chữ (bản cũ ``[:100]`` ra "…từ vựng v" — người dùng thấy 05-10-2026).
    Thứ tự: (1) tiêu đề có phần sau dấu ``|`` mà cuối là NGÀY → bỏ phần giữa, giữ "<đầu> | <ngày>" (ngày là thứ AI được dặn đặt ở cuối); (2) bỏ dần các đoạn ``|`` từ cuối;
    (3) cắt ở ranh giới TỪ gần nhất rồi gỡ dấu nối còn thừa."""
    t = (t or "").strip()
    if len(t) <= tran:
        return t
    ngay = (_NGAY_CUOI.search(t) or [None, ""])[1]
    phan = [x.strip() for x in t.split("|") if x.strip()]
    if len(phan) >= 2:
        if ngay:
            thu = " | ".join(phan[:-1]) + " | " + ngay
            if len(thu) <= tran:
                return thu
        while len(phan) > 1:
            phan.pop()
            thu = " | ".join(phan)
            if len(thu) <= tran:
                return thu
    cat = t[:tran]
    sp = cat.rfind(" ")
    if sp >= tran * 0.6:
        cat = cat[:sp]
    return cat.rstrip(" |,;:-–—·").strip()


def tieu_de_bi_rut(raw: Any) -> list[tuple[int, str]]:
    """(độ dài gốc, bản đã rút) của các tiêu đề trong phần ``youtube`` thô mà app phải rút vì quá trần — để báo người dùng lúc nhận JSON."""
    if not isinstance(raw, dict):
        return []
    ds = _lay(raw, _KHOA_TRONG["tieu_de"])
    if isinstance(ds, str):
        ds = [ds]
    ra: list[tuple[int, str]] = []
    for t in ds if isinstance(ds, list) else []:
        t = _bo_furigana(str(t or ""))
        if len(t) > TRAN_TIEU_DE:
            ra.append((len(t), rut_tieu_de(t)))
    return ra


def chuan_hoa(raw: Any) -> Optional[dict]:
    """Dọn phần ``youtube`` thô của AI thành ``{tieu_de, the, hashtag, hook_anh}`` (các danh sách chuỗi sạch); không có gì dùng được → ``None`` (không lưu cái vỏ rỗng)."""
    if not isinstance(raw, dict):
        return None
    from services import youtube_meta as YM                       # nhập tại chỗ: tệp đó kéo theo PIL / imageio, không cần cho lúc nạp schema
    kq = YM.chuan_hoa({k: _lay(raw, ks) for k, ks in _KHOA_TRONG.items()})
    #  Tiêu đề / chữ ảnh bìa là chữ THƯỜNG của YouTube: AI hay đưa cả furigana "否認[ひにん]" vào (người dùng thấy ở bản dùng thử 05-10-2026). Chỉ bỏ ngoặc cách đọc đứng ngay sau chữ Hán và chỉ chứa kana —
    #  nhãn như "[JLPT N3]" ở đầu tiêu đề được GIỮ (khác ``sangtao_ruby.bo_ruby``: hàm đó bỏ MỌI ngoặc vuông).
    ra = {"tieu_de": [rut_tieu_de(_bo_furigana(t)) for t in kq["tieu_de"]], "the": kq["the"], "hashtag": kq["hashtag"][:5], "hook_anh": [_bo_furigana(h) for h in kq["hook_anh"]]}
    return ra if (ra["tieu_de"] or ra["the"] or ra["hashtag"] or ra["hook_anh"]) else None
