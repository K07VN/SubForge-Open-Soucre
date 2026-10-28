# -*- coding: utf-8 -*-
"""GỘP MẨU PHỤ ĐỀ NGẮN vào câu liền kề — chạy ở MỌI bước lấy sub gốc (29-09-2026).

VÌ SAO: STT/OCR hay tách ra những mẩu 1–3 chữ ở đầu/cuối câu ("同时", "对不对",
"我们在") hoặc những câu nói rất nhanh (5 chữ trong 0,7s) — trên timeline chúng thành
những khối giọng MẢNH 0,3–0,8s nằm sát nhau (người dùng chỉ ảnh 29-09-2026). Tiếng gốc đọc vừa, nhưng
dịch sang tiếng Việt thì không cách nào đọc lọt khung đó -> chồng lấn / bị cắt. Ở chế
độ "Dịch sát nghĩa" AI giữ đúng 1 câu gốc = 1 câu bình, nên phải gộp TỪ BƯỚC LẤY SUB.

LUẬT (thận trọng — thà bỏ sót còn hơn gộp nhầm hai câu không liên quan):
  · mẩu ngắn = ≤ ``TOI_DA_CHU`` chữ (Hán/Nhật/Hàn đếm từng chữ, tiếng khác đếm từ)
    HOẶC dài < ``TOI_THIEU_GIAY`` giây;
  · chỉ gộp với câu KỀ BÊN cách ≤ ``KHE_TOI_DA`` giây, cùng loại nguồn (câu OCR vá lỗ
    không trộn với câu Whisper);
  · không gộp qua dấu kết câu (câu trước đã "。/!/?" thì mẩu là đầu câu mới -> gộp tới;
    mẩu tự kết câu thì chỉ gộp lùi);
  · câu sau khi gộp không quá ``TRAN_GIAY`` giây / ``TRAN_CHU_CJK`` chữ / ``TRAN_TU`` từ;
  · cả hai phía đều được thì chọn phía khe NGẮN hơn; hai khe gần bằng nhau thì gộp vào
    câu ngắn hơn.

Hàm thuần, không đọc/ghi gì — mọi chỗ lấy sub gọi ``gop_manh_ngan(segs)``.
"""
from utils.i18n import L
import logging
import re

logger = logging.getLogger(__name__)

TOI_DA_CHU = 3
#: 1,2s chứ không 0,8s: đo trên dữ liệu thật, câu gốc 1,0–1,2s (4–8 chữ Hán) dịch ra
#: tiếng Việt vẫn không lọt; ba mẩu 0,33–0,43s gộp lại mới 1,1s vẫn chật.
TOI_THIEU_GIAY = 1.2
KHE_TOI_DA = 0.5
#: Hai khe lệch nhau không quá chừng này thì coi như bằng nhau.
KHE_NGANG = 0.15
TRAN_GIAY = 10.0
TRAN_CHU_CJK = 40
TRAN_TU = 30

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")
_KET_CAU = tuple("。！？!?…；;")
_BO_DAU = re.compile(r"[\s\W_]+", re.UNICODE)


def _chu(text: str) -> str:
    return (text or "").strip()


def _co_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def do_dai_chu(text: str) -> int:
    """Số "chữ" để so ngưỡng: Hán/Nhật/Hàn đếm từng chữ, tiếng khác đếm từ."""
    t = _chu(text)
    n_cjk = len(_CJK.findall(t))
    if n_cjk:
        return n_cjk
    return len([w for w in t.split() if _BO_DAU.sub("", w)])


def _ket_cau(text: str) -> bool:
    return _chu(text).rstrip("\"'”’」』)）").endswith(_KET_CAU)


def la_manh_ngan(seg: dict) -> bool:
    try:
        dai = float(seg["end"]) - float(seg["start"])
    except (KeyError, TypeError, ValueError):
        return False
    n = do_dai_chu(seg.get("text"))
    return n > 0 and (n <= TOI_DA_CHU or dai < TOI_THIEU_GIAY)


def _noi_chu(a: str, b: str) -> str:
    a, b = _chu(a), _chu(b)
    if not a or not b:
        return a or b
    #  Dấu cách cả với chữ Hán: OCR vốn nối các dòng của một khung bằng dấu cách, và
    #  nối liền ("闭门造车你知道为什么") làm AI dịch không thấy chỗ ngắt ý.
    return f"{a} {b}"


def _vua_tran(a: dict, b: dict) -> bool:
    if float(b["end"]) - float(a["start"]) > TRAN_GIAY:
        return False
    chu = _noi_chu(a.get("text"), b.get("text"))
    return (len(_CJK.findall(chu)) <= TRAN_CHU_CJK) if _co_cjk(chu) else (len(chu.split()) <= TRAN_TU)


def _cung_loai(a: dict, b: dict) -> bool:
    return bool(a.get("ocr")) == bool(b.get("ocr"))


def _gop(a: dict, b: dict) -> dict:
    out = dict(a)
    out["start"] = min(float(a["start"]), float(b["start"]))
    out["end"] = max(float(a["end"]), float(b["end"]))
    out["text"] = _noi_chu(a.get("text"), b.get("text"))
    return out


def _cau_hinh_bat() -> bool:
    """Ô tick "Gộp mẩu sub ngắn" (Cài đặt chung — THỬ NGHIỆM, mặc định TẮT). Đọc hỏng ->
    coi như tắt: giữ nguyên phụ đề như nguồn là cách an toàn."""
    try:
        from services.config_service import get_config_service
        return get_config_service().get_gop_manh_ngan()
    except Exception:  # noqa: BLE001
        return False


def gop_manh_ngan(segs: list, nhan: str = "", bat: bool = None) -> list:
    """Trả DANH SÁCH MỚI (không sửa ``segs``) đã gộp mẩu ngắn. Giữ nguyên thứ tự thời
    gian; khoá ``index`` (nếu có) được đánh lại 0..N-1.

    ``bat``: None = theo ô tick trong Cài đặt. Tắt -> trả NGUYÊN danh sách như nguồn (không
    sắp lại, không bỏ câu nào) — đúng như lúc chưa có tính năng gộp."""
    if not (_cau_hinh_bat() if bat is None else bat):
        if segs:
            logger.info(L(f"Gộp mẩu phụ đề ngắn{(' ' + nhan) if nhan else ''}: TẮT trong Cài đặt — giữ nguyên {len(segs)} câu.", f"Merge short subtitle fragments{(' ' + nhan) if nhan else ''}: OFF in Settings — keeping {len(segs)} sentences as they are."))
        return list(segs or [])
    if not segs:
        return list(segs or [])
    ds = sorted((dict(s) for s in segs if isinstance(s, dict) and _chu(s.get("text"))),
                key=lambda s: float(s.get("start") or 0.0))
    truoc = len(ds)
    i = 0
    while i < len(ds):
        s = ds[i]
        if not la_manh_ngan(s):
            i += 1
            continue
        lui = tien = None
        if i > 0:
            p = ds[i - 1]
            khe = float(s["start"]) - float(p["end"])
            if khe <= KHE_TOI_DA and not _ket_cau(p.get("text")) and _cung_loai(p, s) and _vua_tran(p, s):
                lui = max(0.0, khe)
        if i + 1 < len(ds):
            n = ds[i + 1]
            khe = float(n["start"]) - float(s["end"])
            if khe <= KHE_TOI_DA and not _ket_cau(s.get("text")) and _cung_loai(s, n) and _vua_tran(s, n):
                tien = max(0.0, khe)
        if lui is None and tien is None:
            i += 1
            continue
        if lui is not None and tien is not None and abs(lui - tien) <= KHE_NGANG:
            #  Hai khe gần bằng nhau -> gộp vào câu NGẮN hơn cho các câu cân nhau.
            d_lui = float(ds[i - 1]["end"]) - float(ds[i - 1]["start"])
            d_tien = float(ds[i + 1]["end"]) - float(ds[i + 1]["start"])
            chon_lui = d_lui <= d_tien
        else:
            chon_lui = tien is None or (lui is not None and lui <= tien)
        if chon_lui:
            ds[i - 1:i + 1] = [_gop(ds[i - 1], s)]
            i = max(0, i - 1)          # câu vừa gộp có thể vẫn còn ngắn -> xét lại
        else:
            ds[i:i + 2] = [_gop(s, ds[i + 1])]
    for k, s in enumerate(ds):
        if "index" in s:
            s["index"] = k
    if len(ds) != truoc:
        logger.info(L(f"Gộp mẩu phụ đề ngắn{(' ' + nhan) if nhan else ''}: {truoc} → {len(ds)} câu "
                    f"(gộp {truoc - len(ds)} mẩu ≤{TOI_DA_CHU} chữ hoặc <{TOI_THIEU_GIAY}s vào câu kề bên).", f"Merge short subtitle fragments{(' ' + nhan) if nhan else ''}: {truoc} → {len(ds)} sentences (merged {truoc - len(ds)} fragments ≤{TOI_DA_CHU} words or <{TOI_THIEU_GIAY}s into the neighbouring sentence)."))
    return ds
