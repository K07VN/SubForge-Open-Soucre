"""PROMPT cho che do EDIT NANG CAO — hai loai kich ban: LONG TIENG va REVIEW.

Nguoi dung copy prompt ra ChatGPT/Claude, dan ket qua JSON vao trinh dung. Hai loai chi
khac nhau O DAY (cach viet kich ban), con moi buoc phia sau — nhap, keo tha, sinh giong,
xuat video — dung CHUNG mot duong:

  * ``review``  : VIET LAI thanh loi binh cua MC. Gop nhieu cau A lien tiep thanh 1 cau B
                  (``source_a_ids`` nhieu phan tu) -> SRT ra la cac khoi loi binh dai.
  * ``dubbing`` : DICH SAT NGHIA, MOT cau A thanh MOT cau B (``source_a_ids`` dung mot
                  phan tu) -> SRT ra bam DUNG tung dong phu de goc.

NGUYEN TAC: module nay CHI THEM MOI.
Prompt review dung lai ``deepseek_service._build_review_prompt`` (bo prompt DUY NHAT dung
chung voi luong tao kich ban tu dong, nen khong bao gio lech nhau). Prompt long tieng viet
o day chu KHONG them vao ``deepseek_service``: file do la cua hai che do cu, sua vao la rui
ro thua. O day chi IMPORT hang so tu no de hai ben khong lech con so.

PHAN VAI (``with_speaker``) — MAC DINH TAT. Bat len thi ca hai prompt doi them truong
``speaker``: nhan vai ngan gon cho tung cau, nho no ma video nhieu nhan vat gan duoc giong
khac nhau (nguoi dung chi chon giong cho MOI VAI o bang Phan vai, khong phai bam tung cau).
AI KHONG duoc doan ID giong (no khong biet he thong co nhung giong nao) — chi dat nhan vai.
Tat (mac dinh) thi output gon hon, dich nhanh va re hon, moi cau ve vai mac dinh "MC".
"""
import logging
import re

from services.deepseek_service import (
    CPS_BY_LANG,
    LANG_NAMES,
    resolve_style_directive,
    voice_gender_desc,
    _build_review_prompt,
)

logger = logging.getLogger("ReviewAdvanced")

KIND_REVIEW = "review"
KIND_DUBBING = "dubbing"
KINDS = (KIND_REVIEW, KIND_DUBBING)
#  MẶC ĐỊNH = LỒNG TIẾNG (người dùng chốt 07-10-2026): dự án Chi tiết mới và lô chưa chọn loại kịch bản đều dịch sát nghĩa
#  (trước là review — lô chạy AI ra lời bình trong khi người dùng chỉ muốn lồng tiếng). Dự án đã bấm chọn Review thì
#  ``script_kind`` đã ghi trong metadata nên không đổi.
DEFAULT_KIND = KIND_DUBBING

#  Nhan vai MAC DINH khi AI khong noi ro / kich ban cu khong co truong nay.
DEFAULT_SPEAKER = "MC"


def normalize_kind(kind) -> str:
    k = str(kind or "").strip().lower()
    return k if k in KINDS else DEFAULT_KIND


#  Doan mo ta truong speaker — dung CHUNG cho hai prompt de AI tra ve cung mot dang.
def _speaker_rules(kind: str) -> str:
    if kind == KIND_DUBBING:
        who = (
            "- SPEAKER (nhan vai): moi cau la loi CUA MOT NHAN VAT. Dat nhan ngan, on dinh "
            "cho tung nhan vat va DUNG LAI Y NGUYEN nhan do cho moi cau cua ho trong ca kich "
            "ban (vd \"Nam 1\", \"Nu 1\", \"Ong gia\", hoac ten rieng neu doan duoc). Loi dan "
            "chuyen/thuyet minh (khong phai thoai cua ai) thi dat \"Thuyet minh\".\n"
        )
    else:
        who = (
            f"- SPEAKER (nhan vai): loi binh review mac dinh la MOT giong MC duy nhat — dat "
            f"\"{DEFAULT_SPEAKER}\" cho moi cau. CHI dung nhan khac khi ban co y de mot cau "
            f"cho giong khac doc (vd chen mot cau nhai lai nhan vat: \"Nhan vat\").\n"
        )
    return (
        who
        + "- Nhan vai chi la CHU DE DAT TEN, KHONG phai ten giong doc: tuyet doi khong ghi "
        "ma giong / ten model / \"nam-nu\" gi ca. Nguoi dung se tu chon giong cho tung vai.\n"
    )


def _he_so_tran() -> float:
    """Hệ số trần ký tự — cài đặt CHUNG của app (chặt 1.0 / vừa 1.2 / thoải mái 1.4).

    Đọc mỗi lần dựng prompt (không nhớ sẵn): người dùng đổi mức xong bấm copy prompt
    ngay, nhớ sẵn thì lần copy đó vẫn ra prompt của giá trị cũ.
    """
    try:
        from services.config_service import get_config_service

        return get_config_service().get_translate_limit_factor()
    except Exception:  # noqa: BLE001 — không đọc được cấu hình thì giữ nếp cũ
        return 1.0


def build_dubbing_prompt(
    a_list,
    source_lang: str,
    target_lang: str = "vi",
    *,
    style=None,
    voice=None,
    with_speaker: bool = False,
    #: HỆ SỐ trần ký tự. ``None`` = theo cài đặt CHUNG của app (chặt 1.0 / vừa 1.2 /
    #: thoải mái 1.4). Truyền số chỉ khi nơi gọi thật sự muốn ép — hiện chưa có chỗ nào cần.
    char_limit=None,
) -> str:
    """Prompt DICH LONG TIENG cho trinh dung nang cao — MOT cau A thanh MOT cau B.

    Khac ``deepseek_service._build_dubbing_prompt`` (duong long tieng cu) o dinh dang tra
    ve: ben do tra tung dong ``[ID] text`` cho pipeline tu dong doc, con o day phai tra
    JSON Array GIONG HET prompt review, vi trinh dung chi co MOT duong nhap kich ban. Nho
    vay them loai kich ban moi khong phai sua gi o FE lan o ``ADV.import_script``.

    Tran ky tu tinh theo THOI LUONG THAT cua tung cau A (giay x ky tu/giay cua ngon ngu
    dich) — vuot tran la giong doc bi ep nhanh hoac cat cut.

    ``with_speaker`` MAC DINH TAT: phim mot nguoi dan / phim khong can phan vai thi doi AI
    dat nhan cho tung cau la bat no suy nghi them va tra ve them mot truong cho MOI cau —
    cham hon, ton token hon, va them mot cho de no tra sai dinh dang. Tat di thi output gon
    hon han, dich nhanh hon; moi cau ve vai mac dinh ("MC") va bang Phan vai van dung duoc
    voi mot vai duy nhat. Ai can nhieu giong thi BAT o man Chuan bi.
    """
    source_name = LANG_NAMES.get(source_lang, source_lang or "ngôn ngữ gốc")
    target_name = LANG_NAMES.get(target_lang, target_lang)
    cps = CPS_BY_LANG.get(target_lang, 14)
    style_directive = resolve_style_directive(style)
    voice_desc = voice_gender_desc(voice)

    extra = ""
    if style_directive:
        extra += (
            f"- KIỂU NÓI (người dùng chọn): {style_directive} — áp dụng cho từ ngữ và giọng "
            "điệu của mọi câu, nhưng vẫn tuân thủ trần ký tự.\n"
        )
    if voice_desc:
        extra += (
            f"- GIỌNG ĐỌC MẶC ĐỊNH: {voice_desc}. Chọn xưng hô phù hợp cho những câu do vai "
            "mặc định đọc.\n"
        )

    if with_speaker:
        speaker_block = _speaker_rules(KIND_DUBBING)
        field_count, field_list = 4, "b_id, b_text, source_a_ids, speaker"
        example_fields = (
            '"b_id": 1, "b_text": "Tôi không đi đâu cả.", "source_a_ids": [1], '
            '"speaker": "Nữ 1"'
        )
    else:
        #  Noi RO la khong can truong speaker. Bo im lang thi model van tu them vao (prompt
        #  review/long tieng ngoai kia deu co) — mat dung cai loi ma ta muon: output gon.
        speaker_block = (
            "- KHÔNG phân vai: cả kịch bản do MỘT giọng đọc. Tuyệt đối KHÔNG thêm trường "
            "\"speaker\" hay bất cứ trường nào khác ngoài 3 trường bên dưới.\n"
        )
        field_count, field_list = 3, "b_id, b_text, source_a_ids"
        example_fields = '"b_id": 1, "b_text": "Tôi không đi đâu cả.", "source_a_ids": [1]'

    #  Công tắc CHUNG toàn app (Cài đặt) — không truyền thì đọc từ cấu hình, để ba
    #  đường (cơ bản / chi tiết / chạy lô) không thể lệch nhau.
    #
    #  LUÔN CÓ TRẦN, chỉ khác chặt hay rộng: bỏ hẳn trần thì AI viết dài không kiểm
    #  soát — câu dịch tràn sang câu sau, dồn kiểu gì cũng không vừa, mà chỉ lộ ra
    #  sau khi đã sinh giọng cả video.
    he_so = float(char_limit) if char_limit else _he_so_tran()
    tran_block = (
        """- TRẦN KÝ TỰ TỪNG CÂU (quan trọng nhất): mỗi dòng đã ghi rõ số ký tự tối đa (tính cả dấu
  cách). Vượt trần là giọng đọc bị ép nhanh nghe hụt hơi, hoặc bị cắt cụt. Nếu bản dịch sát
  nghĩa dài quá, hãy VIẾT LẠI gọn hơn theo văn NÓI tự nhiên (bỏ từ đệm, bỏ chủ ngữ lặp) mà
  vẫn giữ đúng ý. Không bao giờ để một câu chưa dịch hoặc đứt giữa câu."""
        if he_so <= 1.01
        else """- TRẦN KÝ TỰ TỪNG CÂU (BẮT BUỘC): mỗi dòng ghi rõ số ký tự tối đa (tính cả dấu cách).
  Trần này đã nới rộng sẵn để bạn dịch ĐỦ Ý — hãy dùng hết chỗ đó thay vì cắt bớt nội dung,
  nhưng TUYỆT ĐỐI không vượt quá. Vượt trần là giọng đọc bị ép nhanh nghe hụt hơi hoặc bị
  cắt cụt. Không bao giờ để một câu chưa dịch hoặc đứt giữa câu."""
    )

    lines = []
    total = 0.0
    for a in a_list:
        # Doc phong thu: mot a-chunk thieu truong khong duoc lam sap ca prompt.
        try:
            dur = max(0.3, float(a.get("end") or 0) - float(a.get("start") or 0))
        except (TypeError, ValueError):
            dur = 0.3
        total += dur
        text = " ".join(str(a.get("text") or a.get("a_text") or "").split())
        if not text:
            continue
        lines.append(
            f"[A{a.get('a_id')}] ({dur:.1f}s | "
            f"tối đa {max(8, round(dur * cps * he_so))} ký tự): {text}"
        )
    a_list_text = "\n".join(lines)

    return f"""Bạn là biên dịch viên LỒNG TIẾNG chuyên nghiệp (không phải làm phụ đề).
Dưới đây là các câu thoại gốc ({source_name}) lấy từ video, mỗi dòng một câu kèm thời lượng.
Nhiệm vụ: dịch sang {target_name} để AI đọc lồng tiếng.
{extra}
QUY TẮC (BẮT BUỘC):
- MỘT CÂU A → MỘT CÂU B. Không gộp, không tách, không bỏ câu nào. ``source_a_ids`` của mỗi
  B chứa ĐÚNG MỘT a_id — chính câu A mà nó dịch. Giữ nguyên thứ tự.
{tran_block}
- Văn NÓI, không phải văn viết: ưu tiên cách nói ngắn, tự nhiên, đúng cảm xúc câu gốc.
- Dịch từng câu theo NGỮ CẢNH RIÊNG của nó. Không sao chép bản dịch của câu bên cạnh, kể cả
  khi câu gốc ngắn hoặc khó hiểu.
- Xác định ĐÚNG giới tính / vai vế nhân vật qua xưng hô và ngữ cảnh để chọn đại từ.
- Tên riêng nước ngoài: phiên âm cho AI đọc mượt ("Greenland" → "Grin-len"), không chú thích
  tên gốc trong ngoặc.
{speaker_block}
ĐỊNH DẠNG OUTPUT: CHỈ một JSON Array, mỗi phần tử gồm đúng {field_count} trường {field_list}. Không giải thích, không code fence.
Ví dụ: [{{{example_fields}}}]

TỔNG: {len(lines)} câu, {total:.1f}s.

DỮ LIỆU GỐC (LIST A):
{a_list_text}"""


def build_prompt(
    kind: str,
    a_list,
    *,
    source_lang: str = "auto",
    target_lang: str = "vi",
    default_rate: str = "auto",
    style=None,
    brand=None,
    voice=None,
    with_speaker: bool = False,
    char_limit=None,
) -> str:
    """Prompt cho MOT loai kich ban. Bo trong / khong hop le -> loai review.

    ``with_speaker`` MAC DINH TAT (xem ``build_dubbing_prompt``) — de mac dinh cua HAM
    trung voi mac dinh cua CAI DAT du an (``_assign_speakers`` -> False). Neu de True o
    day thi mot cho goi quen truyen co la lang le sinh ra prompt co phan vai, nguoc han y
    nguoi dung ma khong co gi bao.
    """
    kind = normalize_kind(kind)
    if kind == KIND_DUBBING:
        return build_dubbing_prompt(
            a_list, source_lang, target_lang, style=style, voice=voice,
            with_speaker=with_speaker, char_limit=char_limit,
        )

    is_chinese = str(source_lang or "").lower().startswith("zh")
    base = _build_review_prompt(
        a_list,
        is_chinese,
        # Che do nang cao: nguoi dung tu dat vi tri tren timeline, khong xe dich tu dong.
        smart_shift=False,
        default_rate=default_rate,
        style=style,
        brand=brand,
        voice=voice,
        # Khong truyen khoa nay thi prompt review ep cung "Tieng Viet": chon ngon ngu
        # dich khac o man Chuan bi van ra kich ban tieng Viet, khong bao gi.
        target_lang=target_lang,
    )
    # Khong phan vai -> prompt goc da la 3 truong (b_id, b_text, source_a_ids), khong phai
    # chen gi. Moi cau se ve vai mac dinh "MC" qua ``ADV.speaker_of``.
    if not with_speaker:
        return base

    # Chen yeu cau ``speaker`` vao prompt review. Vi sao khong sua ham goc: no dung CHUNG
    # voi luong tao kich ban tu dong (o do khong can nhan vai).
    #
    # CO TU KIEM: ba phep va duoi day khop vao CHUOI CHU cua module khac, doi mot chu ben
    # do la khong khop nua — ma khong khop thi im lang, prompt ra nua noi nua ("quy tac doi
    # speaker" nhung "dinh dang chi 3 truong") va AI tra ve thieu truong. Nen: kiem tung
    # phep, thieu phep nao thi DAN MOT KHOI BO SUNG o cuoi prompt (chi dan cuoi cung bao
    # gio cung thang) va ghi canh bao de con biet ma sua cho tu te.
    add = _speaker_rules(KIND_REVIEW)
    patches = (
        (
            "ĐỊNH DẠNG OUTPUT:",
            f"{add}\nĐỊNH DẠNG OUTPUT:",
        ),
        (
            "CHỈ một JSON Array, mỗi phần tử gồm đúng 3 trường b_id, b_text, source_a_ids.",
            "CHỈ một JSON Array, mỗi phần tử gồm đúng 4 trường b_id, b_text, "
            "source_a_ids, speaker.",
        ),
        (
            'Ví dụ: [{"b_id": 1, "b_text": "Lời bình...", "source_a_ids": [1, 2, 3]}]',
            'Ví dụ: [{"b_id": 1, "b_text": "Lời bình...", "source_a_ids": [1, 2, 3], '
            f'"speaker": "{DEFAULT_SPEAKER}"}}]',
        ),
    )
    out = base
    missed = []
    for needle, repl in patches:
        if needle in out:
            out = out.replace(needle, repl, 1)
        else:
            missed.append(needle[:40])

    if missed:
        logger.warning(
            "[prompt] Không chèn được %d chỗ vào prompt review (_build_review_prompt đã "
            "đổi chữ?): %s — dán khối bổ sung ở cuối thay thế.",
            len(missed), "; ".join(missed),
        )
        out += (
            "\n\nBỔ SUNG (GHI ĐÈ phần định dạng ở trên nếu khác):\n"
            + add
            + "- Mỗi phần tử JSON gồm ĐÚNG 4 trường: b_id, b_text, source_a_ids, speaker.\n"
            + 'Ví dụ: [{"b_id": 1, "b_text": "Lời bình...", "source_a_ids": [1, 2, 3], '
            + f'"speaker": "{DEFAULT_SPEAKER}"}}]'
        )
    return out


# --------------------------------------------------------------------------- #
#  BUOC 0 — DOC NGU CANH CA PHIM (chi duong TU DONG goi DeepSeek dung)
# --------------------------------------------------------------------------- #
#  Vi sao: moi lo chi thay vai chuc cau (1-2 phut phim) -> xung ho lech giua cac lo,
#  ten rieng / thuat ngu moi lo dich mot kieu. Cho mot model doc HET phu de mot lan, viet
#  ghi chu ngu canh, roi gui kem MOI lo. Hai prompt tren (``build_prompt``) GIU NGUYEN
#  tung chu — luong copy-prompt thu cong khong doi.

#: Tran ky tu phu de gui vao buoc 0. Model 1 trieu token ngu canh; 300K ky tu (chu Han
#: ~1 token/ky tu) du cho phim vai tieng ma van du xa tran.
CONTEXT_MAX_CHARS = 300_000


#: Hai phan cua ban tra loi buoc 0 — tieu de NGUYEN VAN de tach ra duoc.
MARK_SUMMARY = "=== TÓM TẮT ==="
MARK_NOTES = "=== GHI CHÚ DỊCH ==="

SUMMARY_LEVEL_TEXT = {
    #  Co ban: vua mot doan mo ta YouTube, doc 10 giay la nam.
    "co-ban": "2-4 câu, nêu video nói về gì và điểm đáng xem nhất",
    #  Chi tiet: di theo trinh tu video, du de nguoi xem biet truoc tung phan.
    "chi-tiet": ("3-6 đoạn ngắn đi theo TRÌNH TỰ video, mỗi đoạn một phần/ý chính "
                 "(ai, làm gì, kết quả ra sao), tổng khoảng 150-350 từ"),
}


def build_context_prompt(film_a_list, source_lang: str = "auto",
                         target_lang: str = "vi", summary_level: str = "co-ban") -> tuple:
    """Prompt buoc 0. Tra ``(prompt, so_cau_da_dua, bi_cat)``.

    Ban tra loi gom HAI phan (tach bang ``split_context``):
      * TOM TAT — cho NGUOI XEM, viet bang ngon ngu DICH: xem lai o trinh dung / chay lo,
        noi vao mo ta YouTube. Do dai theo ``summary_level``.
      * GHI CHU DICH — cho NGUOI DICH: nhan vat, xung ho, ten rieng, thuat ngu, giong van.
    Ca hai phan deu gui kem moi lo (tom tat cung la ngu canh)."""
    source_name = LANG_NAMES.get(source_lang, source_lang or "ngôn ngữ gốc")
    target_name = LANG_NAMES.get(target_lang, target_lang)
    muc_tom_tat = SUMMARY_LEVEL_TEXT.get(summary_level) or SUMMARY_LEVEL_TEXT["co-ban"]
    lines, used = [], 0
    for a in film_a_list:
        text = " ".join(str(a.get("text") or a.get("a_text") or "").split())
        if not text:
            continue
        if used + len(text) > CONTEXT_MAX_CHARS:
            break
        lines.append(f"- {text}")
        used += len(text) + 3
    so_cau = len(lines)
    bi_cat = so_cau < sum(1 for a in film_a_list if str(a.get("text") or a.get("a_text") or "").strip())
    prompt = f"""Bạn là biên tập viên chuẩn bị tài liệu cho nhóm dịch LỒNG TIẾNG một video.
Dưới đây là TOÀN BỘ phụ đề gốc ({source_name}) của video, đúng thứ tự thời gian. Bản dịch sẽ là {target_name}.
Người dịch chỉ thấy vài chục câu một lúc, nên hãy đọc hết rồi viết tài liệu cho họ dùng chung — để cả video giữ ĐÚNG và THỐNG NHẤT xưng hô, tên riêng, thuật ngữ.

Trả lời ĐÚNG 2 phần, mỗi phần mở đầu bằng dòng tiêu đề NGUYÊN VĂN như dưới:

{MARK_SUMMARY}
Tóm tắt nội dung video cho NGƯỜI XEM, viết bằng {target_name}: {muc_tom_tat}. Văn xuôi tự nhiên, không gạch đầu dòng, không markdown, không emoji, không hashtag, không nhắc tới "phụ đề" hay "video gốc".

{MARK_NOTES}
Viết bằng tiếng Việt, gạch đầu dòng, ngắn gọn, đúng 3 mục:
1. NHÂN VẬT / NGƯỜI NÓI: mỗi người một dòng — tên gốc → tên dùng trong bản {target_name}, giới tính, vai trò, quan hệ với người khác, và CÁCH XƯNG HÔ khi họ nói với nhau. Không chắc giới tính thì ghi "chưa rõ", không đoán.
2. TÊN RIÊNG & THUẬT NGỮ: mỗi mục một dòng "gốc → cách dịch cố định" (người, địa danh, tổ chức, chiêu thức, thuật ngữ chuyên ngành, từ lặp lại nhiều lần). Chữ Latin/viết tắt có sẵn trong phụ đề (vd "JIU", "OK") ghi rõ giữ nguyên hay phiên âm. Chỉ ghi mục thực sự xuất hiện.
3. GIỌNG VĂN: văn phong nên dùng (trang trọng / đời thường / hài…), ngôi kể, đối tượng người nghe.

KHÔNG dịch phụ đề. Không lời dẫn, không lời kết. Phần ghi chú tối đa khoảng 600 từ.

PHỤ ĐỀ GỐC ({so_cau} câu):
""" + "\n".join(lines)
    return prompt, so_cau, bi_cat


def split_context(text: str) -> tuple:
    """Tach ban tra loi buoc 0 thanh ``(tom_tat, ghi_chu)``.

    Model khong theo dung tieu de -> ``("", toan_bo)``: van dung duoc lam ngu canh cho
    cac lo, chi khong co tom tat de hien / noi vao YouTube (khong doan bua mot doan)."""
    t = (text or "").strip()
    i = t.find(MARK_SUMMARY)
    j = t.find(MARK_NOTES)
    if i < 0 or j < 0 or j < i:
        return "", t
    summary = t[i + len(MARK_SUMMARY):j].strip()
    notes = t[j + len(MARK_NOTES):].strip()
    #  Model hay boc tieu de bang ** hoac # — bo dau thua o hai mep.
    summary = re.sub(r"^[#*\s]+|[#*\s]+$", "", summary)
    notes = re.sub(r"^[#*\s]+|[#*\s]+$", "", notes)
    return summary, notes


def context_system_message(brief: str) -> str:
    """Tin nhan system gui kem MOI lo. Giong het nhau giua cac lo -> DeepSeek tinh gia
    'cache hit' cho phan nay (dat hon 50-100 lan neu tinh cache miss)."""
    return (
        "NGỮ CẢNH CHUNG CỦA CẢ VIDEO — biên tập viên soạn sau khi đọc toàn bộ phụ đề. "
        "Dùng để giữ ĐÚNG và THỐNG NHẤT xưng hô, tên riêng, thuật ngữ, giọng văn giữa "
        "các đoạn. Đây là tài liệu tham khảo: KHÔNG dịch, KHÔNG đưa vào output. Nếu ghi "
        "chú mâu thuẫn với chính câu gốc thì theo câu gốc.\n\n" + brief.strip()
    )


#: So cau lien ke gui kem moi lo (moi phia). Chi de hieu mach — khong co so a_id nen
#: model khong the tra ve chung; ``_clean_items`` cung chan a_id ngoai lo.
NEIGHBOR_LINES = 10


def neighbor_block(before, after) -> str:
    """Khoi 'cau lien ke' noi vao CUOI prompt cua mot lo. Rong -> ''."""
    def _txt(a):
        return " ".join(str(a.get("text") or a.get("a_text") or "").split())

    truoc = [f"- {_txt(a)}" for a in before if _txt(a)]
    sau = [f"- {_txt(a)}" for a in after if _txt(a)]
    if not truoc and not sau:
        return ""
    out = ["", "", "CÂU LIỀN KỀ ĐOẠN NÀY (CHỈ ĐỂ HIỂU MẠCH — KHÔNG dịch, KHÔNG đưa vào output):"]
    if truoc:
        out += ["Ngay TRƯỚC đoạn:"] + truoc
    if sau:
        out += ["Ngay SAU đoạn:"] + sau
    return "\n".join(out)
