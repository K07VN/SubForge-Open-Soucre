# -*- coding: utf-8 -*-
"""TÁCH CÂU DÀI theo DẤU CÂU + ĐỘ DÀI — tự động, ở hai thời điểm (08-10-2026).

1. TÁCH CÂU GỐC (``tach_cau_goc``, MẶC ĐỊNH BẬT, mọi chế độ) — ngay lúc lấy sub bằng
   WHISPER. Whisper nghe theo cửa sổ 30s nên hay trả một đoạn 10–20s gồm ba, bốn câu; dịch
   nguyên đoạn ra một khối giọng dài thì phải ép tốc độ hoặc bị cắt. Tách từ câu GỐC thì
   bản dịch đi một câu gốc – một câu dịch, mốc bám sát lời nói hơn.
   CHỈ chế độ Whisper / Whisper + OCR vá lỗ — kể cả câu OCR vá (cờ ``ocr``): chế độ kết
   hợp vẫn lấy Whisper làm chính, OCR chỉ lấp chỗ không nghe được, nên người dùng muốn
   tách như nhau (08-10-2026); mảnh tách GIỮ cờ ``ocr`` để các luật riêng của câu vá (Cơ
   bản: không sinh giọng hàng loạt, hiện banner) vẫn chạy. OCR THUẦN / sub chính chủ / tệp
   tự nạp thì không tách — chúng đã theo đúng từng dòng hiện trên hình.
2. TÁCH BẢN DỊCH (``tach_cau_dich``, MẶC ĐỊNH TẮT, chỉ Chi tiết + chạy lô) — lúc NHẬP KỊCH
   BẢN (dán tay, AI, Google dịch). Hợp với kịch bản review AI viết theo khối 3–4 câu.

LUẬT TÁCH (người dùng dặn: phải cân cả độ dài lẫn dấu câu):
  · chỉ cắt ở DẤU KẾT CÂU (。！？.!?…) — dùng chung ``split_text_sentences`` với nút
    "Tách theo câu", nên "3.5 triệu", "1.000.000", 「本当か。」 vẫn nguyên;
  · câu vẫn dài hơn ``TOI_DA_GIAY`` thì cắt tiếp ở DẤU NGẮT PHỤ (，；： , ; :) rồi gom các
    vế thành ít phần nhất, CÂN NHAU, mỗi phần ~ không quá trần. Không có dấu nào thì ĐỂ
    NGUYÊN — không bao giờ cắt giữa cụm từ. Dấu liệt kê 、 cố ý không dùng ("苹果、香蕉、橘子"
    là một ý);
  · dấu NỬA KHỔ dính liền chữ Hán/kana/hangul (Whisper tiếng Trung hay viết ",。" thành
    "," và không có dấu chấm nào) cũng là chỗ ngắt — "1,500" / "3.5" thì không;
  · mảnh ngắn hơn ``TOI_THIEU_GIAY`` gộp vào mảnh kề bên (bên ngắn hơn) — câu gốc 1 giây
    dịch sang tiếng Việt không cách nào đọc lọt khung (cùng lý do với gop_manh_ngan.py);
  · khung giờ chia theo độ dài chữ bằng ĐÚNG công thức của nút tách tay
    (``TL.chia_khung_theo_chu``) — tổng thời lượng không đổi.

Hàm thuần, không đọc/ghi gì. Hai ô tick là cài đặt THEO DỰ ÁN / LÔ (xem ``bat_goc`` /
``bat_dich``) — chỗ gọi đọc từ metadata / cấu hình lô / tham số của bước rồi truyền ``bat``.
"""
from utils.i18n import L
import logging
import math
import re

from services.review_render_cache import resolve_b_time
from services.review_timeline import chia_khung_theo_chu, split_text_sentences

logger = logging.getLogger(__name__)

#: Mảnh ngắn hơn chừng này (ước theo độ dài chữ) thì gộp vào mảnh bên cạnh.
TOI_THIEU_GIAY = 2.0
#: Câu dài hơn chừng này thì cắt tiếp ở dấu ngắt phụ. Trùng trần 10s của gop_manh_ngan:
#: bước gộp không bao giờ dựng câu dài hơn, bước tách cắt những câu vượt quá.
TOI_DA_GIAY = 10.0

#  Dấu ngắt phụ. Latin phải có khoảng trắng phía sau ("1,5 triệu" không bị cắt); CJK cắt
#  ngay sau dấu như ``_CJK_SENTENCE_SPLIT_RE`` của review_timeline.
_NGAT_PHU_LATIN = re.compile(r"(?<=[,;:])\s+")
_NGAT_PHU_CJK = re.compile(r"(?<=[，；：])(?![」』）】〉》\s])")

#  Chữ Hán / kana / hangul (KHÔNG gồm dấu câu) — để nhận dấu NỬA KHỔ đứng sát chữ CJK.
_CHU_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
#  WHISPER TIẾNG TRUNG hay viết dấu NỬA KHỔ dính liền chữ Hán, không khoảng trắng, và thường
#  không có dấu chấm nào: "…只能过牛马的路,你把它叫镜,你放牛…" (người dùng gửi ảnh 08-10-2026 —
#  đoạn 22,6s không tách được vì hai luật trên chỉ nhận "，" toàn khổ hoặc "," + khoảng trắng).
#  Dấu nửa khổ có chữ CJK ở MỘT bên là chỗ ngắt; "1,500" / "3.5" hai bên là chữ số nên vẫn nguyên.
_NGAT_PHU_ASCII_CJK = re.compile(rf"(?<=[{_CHU_CJK}][,;:])|(?<=[,;:])(?=[{_CHU_CJK}])")
_KET_CAU_ASCII_CJK = re.compile(rf"(?<=[{_CHU_CJK}][.!?])|(?<=[.!?])(?=[{_CHU_CJK}])")
#  Mảnh không còn chữ / số nào (vd ".." còn lại của "好...我们") -> nối vào mảnh trước.
_CO_CHU = re.compile(r"\w")


#  Chữ Hán/kana/hangul + dấu câu CJK (。，、「」… U+3000–303F, dạng toàn khổ U+FF00–FFEF).
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯　-〿＀-￯]")


def _noi(a: str, b: str) -> str:
    """Nối lại hai phần VỐN LIỀN NHAU trong câu gốc. Giáp ranh là chữ/dấu CJK thì nối
    liền — tiếng Trung/Nhật không có dấu cách, chèn vào là "过去后， 全镇" lệch hẳn bản gốc."""
    if not a or not b:
        return a or b
    if _CJK.match(a[-1]) or _CJK.match(b[0]):
        return a + b
    return f"{a} {b}"


def _cat(text: str, *bieu_thuc) -> list:
    """Cắt lần lượt theo từng biểu thức; bỏ mảnh rỗng, mảnh chỉ còn dấu thì nối vào trước."""
    phan = [text]
    for bt in bieu_thuc:
        phan = [x for p in phan for x in bt.split(p)]
    out = []
    for p in (x.strip() for x in phan):
        if not p:
            continue
        if out and not _CO_CHU.search(p):
            out[-1] = out[-1] + p
        else:
            out.append(p)
    return out


def _cau(text: str) -> list:
    """Danh sách CÂU: luật chung của nút "Tách theo câu" + dấu kết câu nửa khổ sát chữ CJK."""
    return [c for s in split_text_sentences(text) for c in _cat(s, _KET_CAU_ASCII_CJK)]


def _ngat_phu(text: str) -> list:
    return _cat(text, _NGAT_PHU_LATIN, _NGAT_PHU_CJK, _NGAT_PHU_ASCII_CJK)


def _chia_deu(menh: list, giay_moi_chu: float, toi_da: float) -> list:
    """Gom các vế liền nhau thành ÍT phần nhất mà mỗi phần ~ không quá ``toi_da`` giây, và
    các phần CÂN NHAU (22,6s -> 3 phần ~7,5s, không phải 10 + 10 + 2,6). Chọn chỗ ngắt là
    ranh giới vế gần nhất với các mốc chia đều. Vế đơn lẻ dài hơn trần thì đành đứng riêng."""
    dai = [len(m) * giay_moi_chu for m in menh]
    tong = sum(dai)
    k = min(len(menh), max(1, math.ceil(tong / toi_da - 1e-9)))
    if k < 2:
        return [_noi_het(menh)]
    moc, s = [], 0.0
    for d in dai[:-1]:
        s += d
        moc.append(s)
    chon, tu = [], 0
    for j in range(1, k):
        hi = len(moc) - (k - j)               # chừa đủ ranh giới cho các phần còn lại
        tot = min(range(tu, hi + 1), key=lambda i: abs(moc[i] - tong * j / k))
        chon.append(tot)
        tu = tot + 1
    nhom, dau = [], 0
    for b in chon:
        nhom.append(_noi_het(menh[dau:b + 1]))
        dau = b + 1
    nhom.append(_noi_het(menh[dau:]))
    return nhom


def _noi_het(phan: list) -> str:
    out = ""
    for p in phan:
        out = _noi(out, p)
    return out


def chia_doan(text: str, start, end, toi_thieu: float = TOI_THIEU_GIAY,
              toi_da: float = TOI_DA_GIAY) -> list:
    """Một đoạn -> ``[(chữ, s, e), ...]``. Không đáng tách thì trả ĐÚNG MỘT phần tử, chữ
    NGUYÊN VĂN (không chuẩn hoá khoảng trắng) — đoạn không tách không bị đụng tới."""
    goc = [(text, start, end)]
    try:
        s0, e0 = float(start), float(end)
    except (TypeError, ValueError):
        return goc
    dai = e0 - s0
    if dai < 2 * toi_thieu:            # không thể ra hai mảnh đủ dài
        return goc
    cau = _cau(text)
    if not cau:
        return goc

    #  1) Câu quá dài -> cắt tiếp ở dấu ngắt phụ, chia thành các phần cân nhau.
    giay_moi_chu = dai / (sum(len(c) for c in cau) or 1)
    phan = []
    for c in cau:
        if len(c) * giay_moi_chu > toi_da:
            menh = _ngat_phu(c)
            if len(menh) >= 2:
                phan.extend(_chia_deu(menh, giay_moi_chu, toi_da))
                continue
        phan.append(c)

    #  2) Gộp mảnh quá ngắn — xử lý mảnh NGẮN NHẤT trước, gộp vào bên kề ngắn hơn cho các
    #     mảnh cân nhau. Ước giây lại theo danh sách hiện tại (cắt phụ bỏ khoảng trắng).
    while len(phan) > 1:
        k = dai / (sum(len(p) for p in phan) or 1)
        i = min(range(len(phan)), key=lambda j: len(phan[j]))
        if len(phan[i]) * k >= toi_thieu:
            break
        if i == 0:
            j = 1
        elif i == len(phan) - 1:
            j = i - 1
        else:
            j = i - 1 if len(phan[i - 1]) <= len(phan[i + 1]) else i + 1
        a, b = min(i, j), max(i, j)
        phan[a:b + 1] = [_noi(phan[a], phan[b])]

    if len(phan) < 2:
        return goc
    return [(p, s, e) for p, (s, e) in zip(phan, chia_khung_theo_chu(s0, e0, phan))]


# --------------------------------------------------------------------------- #
#  Hai ô tick là cài đặt THEO DỰ ÁN / THEO LÔ — "Làm mặc định cho dự án mới" phải nhớ
#  được (người dùng 08-10-2026), nên KHÔNG để ở Cài đặt chung như gop_manh_ngan:
#    · Chi tiết: khoá metadata.json (``mac_dinh_du_an.KHOA_CHI_TIET``);
#    · lô:       khoá cấu hình lô, ghi xuống từng dự án (batch_routes ``_buoc_sub`` /
#                ``_ap_cau_hinh_dich`` / ``_ghi_cau_hinh_lo_xuong_du_an``);
#    · Cơ bản:   tham số ``tach_cau`` của bước Lấy phụ đề — lựa chọn sống ở giao diện như
#                "Nguồn phụ đề" (``KHOA_CO_BAN_GIAO_DIEN``). Cơ bản không có ô bản dịch.
#  Chưa có khoá (dự án / lô cũ) = mặc định bên dưới.
# --------------------------------------------------------------------------- #
KHOA_GOC = "tach_cau_goc"
KHOA_DICH = "tach_cau_dich"
MAC_DINH_GOC = True
MAC_DINH_DICH = False


def bat_goc(cau_hinh) -> bool:
    """Ô "Tách câu gốc" của một metadata dự án / cấu hình lô."""
    v = (cau_hinh or {}).get(KHOA_GOC)
    return MAC_DINH_GOC if v is None else bool(v)


def bat_dich(cau_hinh) -> bool:
    """Ô "Tách bản dịch" của một metadata dự án / cấu hình lô."""
    v = (cau_hinh or {}).get(KHOA_DICH)
    return MAC_DINH_DICH if v is None else bool(v)


# --------------------------------------------------------------------------- #
#  1. Câu gốc Whisper
# --------------------------------------------------------------------------- #
def tach_cau_goc(segs: list, nhan: str = "", *, bat: bool) -> list:
    """Trả DANH SÁCH MỚI (không sửa ``segs``): đoạn Whisper dài nhiều câu thành nhiều đoạn
    ``{start, end, text, ...}``. Mảnh mang lại mọi khoá của đoạn gốc (kể cả cờ ``ocr`` của
    câu OCR vá lỗ); khoá ``index`` (nếu có) đánh lại 0..N-1.

    CHỈ GỌI ở nhánh Whisper (Whisper thuần / Whisper + OCR vá lỗ) — hàm không tự biết
    danh sách đến từ đâu, OCR thuần / sub chính chủ / tệp tự nạp phải do chỗ gọi chặn.
    ``bat`` (bắt buộc): ô "Tách câu gốc" của dự án — xem ``bat_goc``.
    """
    ds = [dict(s) for s in (segs or []) if isinstance(s, dict)]
    nhan = f" {nhan}" if nhan else ""
    if not bat:
        if ds:
            logger.info(L(f"Tách câu gốc{nhan}: TẮT ở dự án này — giữ nguyên {len(ds)} đoạn Whisper.", f"Split original sentences{nhan}: OFF for this project — keeping {len(ds)} Whisper segments as they are."))
        return ds
    out, so_doan = [], 0
    for s in ds:
        manh = chia_doan(s.get("text") or "", s.get("start"), s.get("end"))
        if len(manh) < 2:
            out.append(s)
            continue
        so_doan += 1
        for chu, a, b in manh:
            out.append({**s, "text": chu, "start": a, "end": b})
    if so_doan:
        for k, s in enumerate(out):
            if "index" in s:
                s["index"] = k
        logger.info(
            L(f"Tách câu gốc{nhan}: {so_doan} đoạn Whisper dài được tách theo dấu câu — "
            f"{len(ds)} → {len(out)} câu (mảnh ≥ {TOI_THIEU_GIAY:g}s, câu > {TOI_DA_GIAY:g}s "
            f"cắt tiếp ở dấu phẩy).", f"Split original sentences{nhan}: {so_doan} long Whisper segments were split at punctuation — {len(ds)} → {len(out)} sentences (pieces ≥ {TOI_THIEU_GIAY:g}s, sentences > {TOI_DA_GIAY:g}s are cut further at commas).")
        )
    return out


# --------------------------------------------------------------------------- #
#  2. Bản dịch lúc nhập kịch bản (Chi tiết + chạy lô)
# --------------------------------------------------------------------------- #
def chu_cua(item: dict) -> str:
    """Chữ của một câu trong kịch bản nhập vào — nhận đủ các tên trường mà
    ``ADV.import_script`` nhận (AI hay tự đổi tên trường)."""
    return str(
        item.get("b_text") or item.get("text") or item.get("target_text")
        or item.get("content") or ""
    ).strip()


def tach_kich_ban(raw_chunks: list, a_lookup: dict, *, bat: bool) -> tuple:
    """Tách các khối bản dịch dài trước khi nhập. Trả ``(danh sách mới, số khối đã tách)``.

    Mỗi mảnh giữ ``source_a_ids`` / giọng / vai của khối gốc, mang thêm
    ``start_override``/``end_override`` (khung chia theo độ dài chữ) để người nhập biết
    đặt nó ở đâu.

    PHẢI chạy TRƯỚC bước so chữ với câu cũ của ``import_script``: nhập lại đúng bản dịch cũ
    (sửa một câu rồi dán lại cả JSON) thì từng mảnh khớp chữ với mảnh đã tách lần trước ->
    giữ giọng đã sinh. Tách SAU mới so thì khối nguyên không khớp mảnh nào — mất sạch giọng.
    ``bat`` (bắt buộc): ô "Tách bản dịch" của dự án — xem ``bat_dich``.
    """
    ds = list(raw_chunks or [])
    if not bat:
        return ds, 0
    out, so_khoi = [], 0
    for item in ds:
        text = chu_cua(item) if isinstance(item, dict) else ""
        if not text:
            out.append(item)
            continue
        s, e = resolve_b_time(item, a_lookup or {})
        manh = chia_doan(text, s, e) if s is not None else []
        if len(manh) < 2:
            out.append(item)
            continue
        so_khoi += 1
        goc = {k: v for k, v in item.items() if k not in ("text", "target_text", "content")}
        for chu, a, b in manh:
            out.append({**goc, "b_text": chu, "start_override": a, "end_override": b})
    if so_khoi:
        logger.info(
            L(f"Tách bản dịch: {so_khoi} khối dài được tách theo dấu câu — "
            f"{len(ds)} → {len(out)} câu.", f"Split translation: {so_khoi} long blocks were split at punctuation — {len(ds)} → {len(out)} sentences.")
        )
    return out, so_khoi
