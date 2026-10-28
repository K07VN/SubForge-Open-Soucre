"""NHỜ DEEPSEEK RÚT GỌN những câu không còn chỗ, thay vì bắt người dùng sửa tay.

Bước "Dồn tự động" chạy xong vẫn có thể còn N câu thiếu chỗ (lời bình dài hơn phim).
Từ trước tới nay chỉ có hai lối thoát: đổi tốc độ giọng/hình (modal ``FitPlanDialog``),
hoặc tự vào sửa từng câu cho ngắn lại. File này làm lối thứ ba — đưa đúng những câu đó
cho DeepSeek viết lại ngắn hơn rồi sinh giọng lại, tự động.

NGUYÊN TẮC: CHỈ THÊM MỚI. Dùng lại ``deepseek_service`` ở mức nguyên liệu (``_chat``,
``_parse_json_array``) và các hàm sẵn có của engine (``auto_arrange``,
``generate_voices``, ``apply_uniform_speed``) — không sửa một dòng nào của đường dồn
đang chạy ổn.

BA THỨ PHẢI ĐÚNG, mỗi thứ đều là một cách nói dối người dùng nếu làm sai:

1. **Đo "thiếu chỗ" bằng ĐÚNG phép xếp của ``auto_arrange``.** Hai bên tính khác nhau
   thì con số báo ra một đằng, làm xong một nẻo — đúng cái bẫy đã ghi trong
   ``auto_arrange`` (modal dự báo 0 câu, làm xong còn 32).

2. **Trần ký tự tính theo TỶ LỆ CHỖ CÒN LẠI, không phải một con số cố định.** Cùng một
   giọng thì thời lượng gần như tỷ lệ thuận với số ký tự, nên câu chỉ có 70% chỗ thì
   phải rút xuống ~70% số ký tự. Chừa thêm một nhịp an toàn vì TTS không tuyến tính
   tuyệt đối.

3. **Trả lại TỐC ĐỘ GIỌNG như trước.** Đổi chữ là ``speed_factor`` bị xoá (giọng cũ
   không còn đúng nữa). Dự án đang chạy đồng loạt ×1,3 mà vài câu rơi về ×1,0 thì nghe
   ra ngay là máy móc — nên sinh giọng xong phải áp lại đúng hệ số cũ.
"""
from utils.i18n import L
import asyncio
import json
import logging

from services import review_advanced_engine as ADV
from services import review_timeline as TL
from services.deepseek_service import _chat, _parse_json_array

logger = logging.getLogger("ReviewAdvanced")

#: Mỗi lượt gọi bao nhiêu câu. Nhỏ hơn lô dịch (40–50) vì ở đây input mang cả câu gốc
#: lẫn trần ký tự, mà output phải ngắn — lô nhỏ thì model bám trần sát hơn.
LO = 20
#: Trần token output cho mỗi lô.
MAX_TOKENS = 4096
#: Chạy tối đa mấy vòng. Rút gọn một lần thường là đủ; vòng hai vét những câu model
#: viết vẫn còn dài. Vòng ba trở đi gần như không cải thiện mà chỉ tốn tiền API và làm
#: câu trở nên cụt lủn.
SO_VONG_MAC_DINH = 2
#: Chừa an toàn: xin ngắn hơn chỗ trống một chút. TTS không tuyến tính tuyệt đối (dấu
#: câu, số, tên riêng đọc lâu hơn), xin vừa khít thì phần lớn câu vẫn tràn nhẹ.
AN_TOAN = 0.92
#: Không bao giờ ép một câu xuống dưới ngần này ký tự — dưới mức đó thì không còn là
#: rút gọn nữa mà là xoá mất ý.
TOI_THIEU = 12
#: Sàn của tỷ lệ nén: không bao giờ đòi rút quá một nửa trong MỘT vòng. Nén sâu hơn thì
#: câu ra cụt lủn, mất ý — thà để vòng sau rút tiếp (hoặc để người dùng đổi tốc độ).
TY_LE_SAN = 0.5


class RutGonError(Exception):
    """Không rút gọn được câu nào — kèm lời nhắn hiện thẳng lên giao diện."""

    def __init__(self, loi_nhan: str):
        super().__init__(loi_nhan)
        self.user_message = loi_nhan


# --------------------------------------------------------------------------- #
#  ĐO: câu nào còn thiếu chỗ, thiếu bao nhiêu
# --------------------------------------------------------------------------- #
def cau_chua_vua(project_id: str) -> dict:
    """Xếp thử y như ``auto_arrange`` bước 1 rồi trả về các câu KHÔNG đủ chỗ.

    KHÔNG ghi gì cả. Mỗi câu kèm ``cho`` (số giây thật sự có) và ``dur`` (độ dài giọng
    hiện tại), để bên gọi tính được phải rút còn bao nhiêu phần.

    Phép xếp ở đây phải KHỚP TỪNG DÒNG với ``auto_arrange``: mốc tự nhiên lấy từ
    ``a_chunks`` (không đọc ``start_override`` — xem ghi chú dài trong ``auto_arrange``),
    con trỏ chỉ tiến, và cuối phim là giới hạn cứng.
    """
    p = ADV.load_project(project_id)
    pdir, meta, a_lookup, b_chunks = p["dir"], p["meta"], p["a_lookup"], p["b_chunks"]

    TL.probe_durations([TL.clip_audio_path(pdir, b) for b in b_chunks], pdir)
    moc = ADV.moc_tu_nhien_cac_cau(b_chunks, a_lookup)   # cung moc voi auto_arrange (anh em cau tach)
    items = []
    for b in b_chunks:
        raw = TL.audio_duration(TL.clip_audio_path(pdir, b))
        dur = TL.effective_audio_duration(b, raw)
        if dur <= 0:
            continue
        items.append({"b": b, "natural": moc[TL.get_uid(b)], "dur": dur})
    if not items:
        return {"items": [], "total": 0, "limit": 0.0}

    items.sort(key=lambda x: x["natural"])
    limit = ADV._timeline_limit(meta)
    #  Giong duoc tran qua mep khung chung nay giay (cho_tran_giong) -> ``cho`` cua cau = khung + tran, cung luat voi auto_arrange.
    tran = TL.tran_giong_cho_phep(meta)

    thieu = []
    #  CUNG LUAT VOI ``auto_arrange``: ``ADV.khung_giu_moc`` — giu moc bat dau, chi noi
    #  mep phai toi sat cau sau. ``cho`` la khoang CUA RIENG cau do (+ phan duoc tran).
    khung = ADV.khung_giu_moc([x["natural"] for x in items], [x["dur"] for x in items], limit)
    for it, (start, end) in zip(items, khung):
        cho = end - start + tran
        if cho < it["dur"] - 0.05:
            b = it["b"]
            thieu.append(
                {
                    "uid": TL.get_uid(b),
                    "b_id": b.get("b_id"),
                    "text": (b.get("b_text") or b.get("text") or "").strip(),
                    "dur": round(it["dur"], 3),
                    "cho": round(max(0.0, cho), 3),
                    "start": round(start, 3),
                    "speed_factor": b.get("speed_factor"),
                }
            )

    return {"items": thieu, "total": len(items), "limit": round(limit, 3)}


def ty_le_cau(muc: dict) -> float:
    """Câu này phải rút còn bao nhiêu phần: ``cho / dur``, có sàn ``TY_LE_SAN``.

    TÍNH RIÊNG TỪNG CÂU — đúng từ khi phép xếp GIỮ MỐC BẮT ĐẦU (``ADV.khung_giu_moc``,
    25-09-2026): ``cho`` là khoảng của CHÍNH câu đó, tới sát câu sau. Bản trước tính
    theo CẢ CỤM vì phép xếp cũ đẩy con trỏ về sau, một câu tràn là mọi câu sau có
    ``cho = 0`` — chuyện đó không còn xảy ra nữa. Sàn: rút quá nửa trong một vòng là câu
    cụt lủn, thà để vòng sau rút tiếp.
    """
    dur = float(muc.get("dur") or 0)
    if dur <= 0:
        return 1.0
    return max(TY_LE_SAN, min(1.0, float(muc.get("cho") or 0) / dur))


def _tran_ky_tu(muc: dict, ty_le_chung: float = 0.0) -> int:
    """Câu này được phép dài bao nhiêu ký tự sau khi rút.

    ``ty_le_chung`` = tỷ lệ nén (xem ``ty_le_cau``). Bỏ trống thì rơi về ``cho / dur``
    không có sàn — chỉ dùng khi gọi lẻ để soi số liệu.
    """
    text = muc.get("text") or ""
    dur = float(muc.get("dur") or 0)
    cho = float(muc.get("cho") or 0)
    if dur <= 0 or not text:
        return TOI_THIEU
    ty_le = ty_le_chung if ty_le_chung > 0 else max(0.0, min(1.0, cho / dur))
    return max(TOI_THIEU, int(len(text) * min(1.0, ty_le) * AN_TOAN))


# --------------------------------------------------------------------------- #
#  GỌI MODEL
# --------------------------------------------------------------------------- #
def _prompt(lo: list) -> str:
    dong = []
    for i, m in enumerate(lo, 1):
        dong.append(f"[{i}] (tối đa {m['tran']} ký tự): {m['text']}")
    ds = "\n".join(dong)
    return f"""Bạn là biên tập viên lời bình video. Các câu dưới đây ĐỌC KHÔNG KỊP trong
khung thời gian của nó, cần viết lại NGẮN HƠN.

QUY TẮC (BẮT BUỘC):
- Giữ ĐÚNG Ý CHÍNH và giọng điệu của câu gốc. Không thêm ý mới, không bịa chi tiết.
- Mỗi câu có trần ký tự riêng ghi ngay trên dòng (tính cả dấu cách). KHÔNG được vượt.
- Cắt từ đệm, chủ ngữ lặp, trạng ngữ thừa trước; chỉ bỏ bớt chi tiết khi vẫn còn dài.
- Văn NÓI tự nhiên, đọc lên nghe thuận miệng. Giữ nguyên tên riêng và con số.
- Giữ nguyên ngôn ngữ của câu gốc. Không dịch sang tiếng khác.
- TUYỆT ĐỐI không trả về câu rỗng và không bỏ sót câu nào.

ĐỊNH DẠNG OUTPUT: CHỈ một JSON Array, mỗi phần tử gồm đúng 2 trường "id" và "text".
Không giải thích, không code fence.
Ví dụ: [{{"id": 1, "text": "Câu đã rút gọn."}}]

DANH SÁCH CÂU ({len(lo)} câu):
{ds}"""


def _doc_ket_qua(raw, lo: list) -> dict:
    """Phản hồi của model -> ``{uid: chữ mới}``. Bỏ qua câu sai định dạng/rỗng/dài hơn."""
    ra = {}
    for phan in raw or []:
        if not isinstance(phan, dict):
            continue
        try:
            i = int(phan.get("id"))
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= len(lo)):
            continue
        moi = " ".join(str(phan.get("text") or "").split())
        cu = lo[i - 1]["text"]
        #  BỎ QUA câu model trả DÀI HƠN bản gốc: nhận vào là làm tình hình tệ đi đúng
        #  cái việc đang cố sửa, mà người dùng thì thấy "đã rút gọn N câu".
        if not moi or len(moi) >= len(cu):
            continue
        ra[lo[i - 1]["uid"]] = moi
    return ra


def _chay_mot_lo(lo: list, model: str) -> dict:
    try:
        raw = _chat(
            [{"role": "user", "content": _prompt(lo)}], model, max_tokens=MAX_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — lô hỏng thì bỏ lô đó, không dừng cả lượt
        logger.warning(L(f"[rút gọn] Lô {len(lo)} câu gọi DeepSeek lỗi: {e}", f"[shorten] Batch of {len(lo)} sentences: DeepSeek call failed: {e}"))
        return {}
    try:
        return _doc_ket_qua(_parse_json_array(raw), lo)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(L(f"[rút gọn] Không đọc được phản hồi: {e}", f"[shorten] Could not read the response: {e}"))
        return {}


# --------------------------------------------------------------------------- #
#  VIỆC CHÍNH
# --------------------------------------------------------------------------- #
def _he_so_dang_dung(b_chunks: list) -> float:
    """Hệ số tăng tốc mà CẢ DỰ ÁN đang dùng (0 nếu mỗi câu một kiểu / không dùng).

    Chỉ trả về số khi gần như mọi câu cùng một hệ số — đó là dấu hiệu người dùng đã áp
    một phương án đồng loạt. Mỗi câu một hệ số (do ``auto_speed``) thì không có "hệ số
    của dự án" để khôi phục, đụng vào là làm hỏng thứ người dùng đã chỉnh.
    """
    cac = [b.get("speed_factor") for b in b_chunks]
    co = [float(x) for x in cac if x not in (None, "", 0)]
    if not co or len(co) < max(1, int(len(cac) * 0.8)):
        return 0.0
    dau = co[0]
    if any(abs(x - dau) > 0.005 for x in co):
        return 0.0
    return dau if dau > 1.001 else 0.0


async def rut_gon_cho_vua(
    project_id: str,
    *,
    model: str = "",
    so_vong: int = SO_VONG_MAC_DINH,
    ghi_log=None,
) -> dict:
    """Rút gọn các câu thiếu chỗ bằng DeepSeek, sinh giọng lại, rồi dồn lại.

    Trả về số liệu để bên gọi báo cho người dùng; không ném lỗi khi chỉ rút được một
    phần — báo đúng phần làm được còn hơn bỏ hết công vừa làm.
    """
    from services.config_service import get_config_service

    def noi(s: str) -> None:
        logger.info(L(f"[rút gọn] {s}", f"[shorten] {s}"))
        if ghi_log:
            try:
                ghi_log(s)
            except Exception:  # noqa: BLE001 — ghi nhật ký hỏng không được làm hỏng việc
                pass

    model = model or get_config_service().ai_model_mac_dinh()

    da_sua, da_sinh, vong_chay = 0, 0, 0
    da_chup = False
    con_lai = len((await asyncio.to_thread(cau_chua_vua, project_id))["items"])
    ban_dau = con_lai
    if not con_lai:
        return {"changed": 0, "voiced": 0, "before": 0, "cut": 0, "rounds": 0}

    for vong in range(1, max(1, int(so_vong)) + 1):
        do = await asyncio.to_thread(cau_chua_vua, project_id)
        thieu = do["items"]
        if not thieu:
            break
        vong_chay = vong
        noi(L(f"Vòng {vong}: {len(thieu)} câu chưa đủ chỗ — nhờ DeepSeek rút gọn.", f"Round {vong}: {len(thieu)} sentences still lack room — asking DeepSeek to shorten them."))

        muc = []
        for m in thieu:
            if not m["text"]:
                continue
            m = dict(m)
            m["tran"] = _tran_ky_tu(m, ty_le_cau(m))
            #  Đã ngắn tới mức trần rồi thì rút thêm cũng vô nghĩa — bỏ qua, để người
            #  dùng xử lý bằng cách khác (đổi tốc độ, cắt bớt hình).
            if len(m["text"]) <= m["tran"]:
                continue
            muc.append(m)
        if not muc:
            noi(L("Không câu nào rút gọn thêm được nữa.", "No more sentences can be shortened."))
            break

        cac_lo = [muc[i: i + LO] for i in range(0, len(muc), LO)]
        moi: dict = {}
        for lo in cac_lo:
            moi.update(await asyncio.to_thread(_chay_mot_lo, lo, model))
        if not moi:
            noi(L("DeepSeek không trả về câu nào dùng được.", "DeepSeek did not return any usable sentence."))
            break

        #  CHỤP HOÀN TÁC một lần, ngay TRƯỚC lần ghi đầu tiên.
        #
        #  Bắt buộc: đây là thao tác VIẾT LẠI CHỮ của hàng chục câu rồi sinh giọng đè
        #  lên — người dùng phải lùi lại được bằng Ctrl+Z y như mọi thao tác khác.
        #  Chụp TRƯỚC KHI ghi (không chụp lúc mới vào hàm) để lượt nào DeepSeek không
        #  trả về gì thì không để lại một bước hoàn tác rỗng.
        if not da_chup:
            await asyncio.to_thread(
                lambda: ADV.take_snapshot(ADV.load_project(project_id)["dir"])
            )
            da_chup = True

        #  Ghi chữ mới. ``speed_factor`` phải xoá: bản giọng đang có là của chữ cũ.
        def _ghi(uid_text: dict) -> int:
            p = ADV.load_project(project_id)
            n = 0
            for uid, chu in uid_text.items():
                b = TL.find_chunk(p["b_chunks"], uid)
                if b is None:
                    continue
                TL.patch_chunk(p["b_chunks"], uid, {"b_text": chu})
                b["speed_factor"] = None
                n += 1
            if n:
                ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"])
            return n

        n = await asyncio.to_thread(_ghi, moi)
        da_sua += n
        noi(L(f"Vòng {vong}: đã rút gọn {n} câu, đang sinh lại giọng.", f"Round {vong}: shortened {n} sentences, regenerating the voice."))

        res = await ADV.generate_voices(project_id, uids=list(moi.keys()), force=True)
        da_sinh += int(res.get("ok", 0) or 0)
        if res.get("cancelled"):
            noi(L("Đã huỷ giữa chừng.", "Cancelled midway."))
            break

        #  Trả lại tốc độ giọng như trước khi sửa chữ (xem ghi chú đầu file).
        he_so = await asyncio.to_thread(
            lambda: _he_so_dang_dung(ADV.load_project(project_id)["b_chunks"])
        )
        if he_so:
            await asyncio.to_thread(ADV.apply_uniform_speed, project_id, he_so)
            noi(L(f"Đã áp lại tốc độ giọng ×{he_so:.2f} cho đều cả dự án.", f"Reapplied the voice speed ×{he_so:.2f} evenly across the project."))

        #  luon_ghi=True: vua sinh lai giong (do dai doi) nen o cu khong con dung.
        xep = await asyncio.to_thread(ADV.auto_arrange, project_id, 0.0, True, True)
        con_lai = int(xep.get("cut", 0) or 0)
        noi(L(f"Vòng {vong}: dồn lại — còn {con_lai} câu chưa đủ chỗ.", f"Round {vong}: re-fitted — {con_lai} sentences still lack room."))
        if not con_lai:
            break

    return {
        "changed": da_sua,
        "voiced": da_sinh,
        "before": ban_dau,
        "cut": con_lai,
        "rounds": vong_chay,
    }
