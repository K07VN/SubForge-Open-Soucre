"""DỰNG PROMPT cho chế độ "Sáng tạo" — ĐỌC BÁO học ngoại ngữ (người dùng sao chép prompt, dán vào ChatGPT/Gemini…, rồi dán JSON về).

Đầu vào là NỘI DUNG BÀI BÁO (app đã tải từ link — xem :mod:`sangtao_bai_bao` — hoặc người dùng dán chữ). Mặc định AI chỉ lấy SỰ KIỆN rồi TỰ VIẾT câu mới (kèm tối đa 2 câu trích ngắn,
đánh dấu ``quote``), chia cảnh, dịch, phiên âm và (bài giảng) phân tích từ vựng / ngữ pháp; KHÔNG được thêm sự kiện. Mọi giá trị cho phép lấy từ :mod:`sangtao_schema` để prompt và bộ kiểm không lệch nhau.
"""
from __future__ import annotations

import re
from datetime import date

from services import sangtao_schema as SC

TEN_NGON_NGU = {"vi": "tiếng Việt", "ja": "tiếng Nhật", "en": "tiếng Anh", "zh-cn": "tiếng Trung", "ko": "tiếng Hàn",
                "fr": "tiếng Pháp", "de": "tiếng Đức", "es": "tiếng Tây Ban Nha", "ru": "tiếng Nga", "pt": "tiếng Bồ Đào Nha",
                "it": "tiếng Ý"}
GIOI_HAN_KICH_BAN = 6000          # ký tự mỗi lần dán vào trợ lý AI (an toàn với các giới hạn phổ biến)
NOI_DUNG = ("chon_loc", "viet_lai", "giu_nguyen")
#: độ dài tối đa của MỘT câu đọc (gợi ý cho AI): (số, đơn vị)
DO_DAI_CAU = {"ja": (45, "ký tự"), "zh-cn": (45, "ký tự"), "ko": (55, "ký tự")}
#: thang trình độ theo ngôn ngữ — (mã, mô tả đưa vào prompt). Ngôn ngữ khác dùng thang CEFR.
CEFR = [("A1", "CEFR A1 (sơ cấp)"), ("A2", "CEFR A2 (sơ cấp cao)"), ("B1", "CEFR B1 (trung cấp)"), ("B2", "CEFR B2 (trung cấp cao)"),
        ("C1", "CEFR C1 (cao cấp)"), ("C2", "CEFR C2 (thành thạo)")]
TRINH_DO = {
    "en": CEFR,
    "ja": [("N5", "JLPT N5 (sơ cấp)"), ("N4", "JLPT N4"), ("N3", "JLPT N3 (trung cấp)"), ("N2", "JLPT N2"), ("N1", "JLPT N1 (cao cấp)")],
    "zh-cn": [(f"HSK{i}", f"HSK {i}") for i in range(1, 7)],
    "ko": [(f"TOPIK{i}", f"TOPIK {i}") for i in range(1, 7)],
}


def trinh_do_hop_le(language: str, ma: str | None) -> str:
    """Mã trình độ hợp lệ của ``language`` (hoặc rỗng nếu không chọn / không thuộc thang)."""
    return ma if ma and any(ma == m for m, _ in TRINH_DO.get(language, CEFR)) else ""


def mo_ta_trinh_do(language: str, ma: str) -> str:
    return next((t for m, t in TRINH_DO.get(language, CEFR) if m == ma), "")


def chia_kich_ban(kich_ban: str, toi_da: int = GIOI_HAN_KICH_BAN) -> list[str]:
    """Chia văn bản dài thành các phần <= ``toi_da`` ký tự, ưu tiên ngắt ở ranh giới đoạn rồi câu."""
    t = (kich_ban or "").replace("\r\n", "\n").strip()
    if not t:
        return []
    if len(t) <= toi_da:
        return [t]
    phan, cur = [], ""

    def dong():
        nonlocal cur
        if cur.strip():
            phan.append(cur.strip())
        cur = ""

    for doan in re.split(r"\n{2,}", t):
        if len(doan) > toi_da:                                  # đoạn dài: tách theo câu
            cau = re.split(r"(?<=[.!?。！？])\s*", doan)
            for c in cau:
                while len(c) > toi_da:                           # câu không dấu ngắt: cắt cứng
                    dong() if len(cur) + len(c[:toi_da]) > toi_da else None
                    cur += c[:toi_da]
                    c = c[toi_da:]
                    dong()
                if len(cur) + len(c) + 1 > toi_da:
                    dong()
                cur += (" " if cur else "") + c
            continue
        if len(cur) + len(doan) + 2 > toi_da:
            dong()
        cur += ("\n\n" if cur else "") + doan
    dong()
    return phan


#: mô tả từng lượt cho AI (lộ trình đưa vào lời chào)
MO_TA_LUOT = {"doc": "nghe kịch bản kể lại bài từ đầu đến cuối", "dich": "nghe lại, lần này có bản dịch", "hoi": "trả lời vài câu hỏi kiểm tra nghe hiểu (có đáp án)",
              "giang": "đi qua vài từ vựng và mẫu ngữ pháp đáng chú ý", "shadow": "luyện shadowing (nhắc lại theo từng câu)"}
#: tên NGẮN của từng lượt — để nhắc kỹ năng thật có trong video khi soạn tiêu đề YouTube (không dùng MO_TA_LUOT: quá dài)
KY_NANG_NGAN = {"doc": "nghe", "dich": "nghe kèm bản dịch", "hoi": "trả lời câu hỏi nghe hiểu", "giang": "từ vựng + ngữ pháp", "shadow": "shadowing"}
#: cách gọi gọn từng lượt để ghép thành cụm "luyện nghe, dịch và shadowing" trong tiêu đề YouTube
KY_NANG_CAU = {"doc": "nghe", "dich": "dịch", "hoi": "trả lời câu hỏi", "giang": "từ vựng", "shadow": "shadowing"}
#: thang trình độ theo ngôn ngữ đích (để nhờ AI tự chọn MỘT mức khi người dùng chưa chọn)
THANG_TRINH_DO = {"ja": "JLPT", "zh-cn": "HSK", "ko": "TOPIK"}
#: ví dụ cách ghi HAI mức liền kề (một chữ tên thang rồi các mức cách nhau dấu "/") khi người dùng chưa chọn trình độ — để tiêu đề phủ cả hai cụm tìm kiếm
VD_HAI_MUC = {"ja": "JLPT N3/N2", "zh-cn": "HSK 3/4", "ko": "TOPIK 2/3"}


def cac_phan(script: str, *, language: str = "en", aspect: str = "16:9", audience: str = "general", native_language: str = "vi", level: str = "",
             content_mode: str = "viet_lai", passes: list[str] | None = None, narration: bool = True, source_url: str = "", source_site: str = "",
             source_date: str = "") -> list[dict]:
    """Chia bài thành các phần ≤ ``GIOI_HAN_KICH_BAN`` ký tự và dựng prompt từng phần: ``[{"i", "ky_tu", "prompt"}]`` (``i`` từ 1).
    MỘT nơi cho cả nút "Tạo prompt" (người dùng dán vào ChatGPT / Gemini) lẫn việc nền "Nhờ AI viết kịch bản" (app tự gọi API) — hai đường
    phải ra CÙNG một prompt (test golden ``test_sangtao_ai_kich_ban``). Bài trống → ``[]``; tuỳ chọn sai → ``ValueError``."""
    phan = chia_kich_ban(script)
    if not phan:
        return []
    passes = list(passes or ["doc"])
    return [{"i": i, "ky_tu": len(p),
             "prompt": dung_prompt(p, language, aspect, audience, phan=(i, len(phan)) if len(phan) > 1 else None, ngon_me=native_language,
                                   trinh_do=level, noi_dung=content_mode, nguon=source_url, giai_thich="giang" in passes, passes=passes,
                                   loi_dan=narration, ten_nguon=source_site, ngay_dang=source_date)}
            for i, p in enumerate(phan, 1)]


def dung_prompt(van_ban: str, language: str = "en", aspect: str = "16:9", audience: str = "general", phan: tuple[int, int] | None = None,
                ngon_me: str = "vi", trinh_do: str = "", noi_dung: str = "viet_lai", nguon: str = "", giai_thich: bool = False,
                passes: list[str] | None = None, loi_dan: bool = True, ten_nguon: str = "", thong_tin_youtube: bool = True,
                ngay_dang: str = "", hom_nay: str = "") -> str:
    """Prompt hoàn chỉnh (tiếng Việt) cho một phần nội dung bài. ``phan=(i, tong)`` khi bài được chia nhiều phần gửi lần lượt trong CÙNG cuộc trò chuyện.
    ``passes``: các lượt đang bật (để lời chào giới thiệu đúng lộ trình); ``loi_dan``: yêu cầu AI viết lời chào / chuyển lượt / kết (nói bằng tiếng đích). ``ten_nguon``: tên báo, để lời dẫn mở đầu nêu nguồn.
    ``thong_tin_youtube``: nhờ AI soạn luôn tiêu đề / thẻ / hashtag / chữ ảnh bìa để ĐĂNG YOUTUBE (khoá ``youtube``) — CHỈ ở bài một phần hoặc PHẦN CUỐI của bài dài (lúc đó AI đã đọc hết các phần trước trong cùng cuộc trò chuyện).
    ``ngay_dang``: ngày đăng của bài gốc (như người dùng ghi / app lấy được; có thể rỗng) · ``hom_nay``: ngày làm video (mặc định hôm nay, ``dd/mm/yyyy``) — hai mốc này là THỜI GIAN DUY NHẤT AI được dùng trong tiêu đề YouTube (không tự bịa ngày)."""
    kb = (van_ban or "").strip()
    if not kb:
        raise ValueError("Nội dung bài trống")
    if len(kb) > GIOI_HAN_KICH_BAN * 1.2:
        raise ValueError(f"Nội dung dài {len(kb)} ký tự, hãy dùng chia_kich_ban() để chia thành các phần ≤ {GIOI_HAN_KICH_BAN}")
    if aspect not in SC.TY_LE:
        raise ValueError(f"Tỷ lệ không hợp lệ: {aspect}")
    audience = "general" if audience == "elderly" else audience
    if audience not in SC.DOI_TUONG:
        raise ValueError(f"Đối tượng không hợp lệ: {audience}")
    if noi_dung not in NOI_DUNG:
        raise ValueError(f"Cách xử lý nội dung không hợp lệ: {noi_dung}")
    ngon = TEN_NGON_NGU.get(language, language)
    nm = TEN_NGON_NGU.get(ngon_me, ngon_me)
    trinh_do = trinh_do_hop_le(language, trinh_do)
    td_chu = mo_ta_trinh_do(language, trinh_do)
    n_toi_da, don_vi = DO_DAI_CAU.get(language, (22, "từ"))
    ghi_phan = (f"\n(Đây là phần {phan[0]}/{phan[1]} của một bài dài, gửi lần lượt trong CÙNG cuộc trò chuyện: chỉ chia cảnh cho phần này, "
                + ("cảnh đầu tiên mang TIÊU ĐỀ của cả bài" if phan[0] == 1 else "KHÔNG lặp lại tiêu đề / lời mở đầu, nối tiếp mạch bài") + ".)") if phan else ""
    gioi_han_cau = f"{n_toi_da} {don_vi}"
    pks = [p_ for p_ in MO_TA_LUOT if p_ in (passes or list(MO_TA_LUOT))] or ["doc"]
    ky_nang = ", ".join(KY_NANG_NGAN[p_] for p_ in pks if p_ in KY_NANG_NGAN)
    cac_ky_nang = [KY_NANG_CAU[p_] for p_ in pks if p_ in KY_NANG_CAU]
    ky_nang_cau = "luyện " + (", ".join(cac_ky_nang[:-1]) + " và " + cac_ky_nang[-1] if len(cac_ky_nang) > 1 else cac_ky_nang[0] if cac_ky_nang else "nghe")
    thang_td = THANG_TRINH_DO.get(language, "CEFR")
    nhan_td = td_chu.split(" (")[0] if td_chu else ""                              # "JLPT N3" (bỏ phần mô tả "(trung cấp)")
    nhan_mau_td = nhan_td or f"{thang_td} <mức>"                                    # chỗ trình độ trong công thức tiêu đề
    vd_hai_muc = VD_HAI_MUC.get(language, "CEFR B1/B2")
    ky_nang_ghep = (", ".join(cac_ky_nang[:-1]) + " và " + cac_ky_nang[-1]) if len(cac_ky_nang) > 1 else (cac_ky_nang[0] if cac_ky_nang else "nghe")
    lo_trinh = ", rồi ".join(MO_TA_LUOT[p_] for p_ in pks)                     # lộ trình đúng thứ tự các lượt đang bật
    co_hoi = "hoi" in pks
    hom_nay = (hom_nay or "").strip() or date.today().strftime("%d/%m/%Y")
    ngay_txt = (f"ngày đăng của bài gốc: {ngay_dang.strip()[:40]}; " if (ngay_dang or "").strip() else "app KHÔNG biết ngày đăng của bài gốc; ") + f"hôm nay, ngày làm video: {hom_nay}"
    co_yt = bool(thong_tin_youtube) and (not phan or phan[0] == phan[1])           # chỉ bài một phần / phần cuối: lúc đó AI đã thấy cả bài
    nguon_ten = ten_nguon.strip() or "báo gốc"
    bridges_mau = ", ".join('"' + p_ + '": "…"' for p_ in (pks[1:] or ["dich"]))
    if noi_dung == "giu_nguyen":
        #  GIỮ NGUYÊN VĂN = SAO CHÉP, không phải "dùng đúng ý". Bản cũ chỉ nói "dùng đúng ý và câu chữ, tách thành câu ngắn hơn ở chỗ hợp lý" nên AI vừa tách vừa diễn đạt lại,
        #  gộp / lặp / thêm câu dẫn (05-10-2026: bài NHK ra 22 câu rời rạc, có câu lặp, nhiều câu không có trong bài) — mà app lại báo "100% nguyên văn". Nay: chỉ được CẮT, cấm mọi việc khác;
        #  và app đối chiếu từng câu với bài gốc (``sangtao_do_khop``) để báo câu nào không khớp.
        r_nd = (f"CÁCH XỬ LÝ NỘI DUNG — GIỮ NGUYÊN VĂN (chỉ dùng cho nội dung của chính người dùng hoặc nguồn có giấy phép mở). Đây là việc SAO CHÉP chứ KHÔNG phải viết lại: "
                "mỗi \"text\" phải là một ĐOẠN CHỮ LIỀN NHAU của phần thân bài, chép ĐÚNG TỪNG KÝ TỰ (chữ, số, dấu), theo đúng thứ tự xuất hiện trong bài. "
                "Việc DUY NHẤT được làm với chữ: (1) CẮT phần thân bài thành các câu — CHỈ ở dấu kết câu của bài (。！？ . ! ?) nằm NGOÀI ngoặc kép / ngoặc 「」『』\"…\". MỖI CÂU của bài là MỘT \"text\" NGUYÊN VẸN, kể cả khi rất dài "
                "(báo hay ghi cả đoạn phát biểu trong 「…」 dài hàng trăm chữ: vẫn để NGUYÊN trong một \"text\"). KHÔNG tách câu ở dấu phẩy / ranh giới mệnh đề, KHÔNG băm nhỏ, KHÔNG rút gọn — app tự cuộn màn hình theo câu dài; "
                "không thêm bớt chữ nào; (2) BỎ phần không thuộc thân bài (quảng cáo, liên kết \"đọc thêm\", chú thích ảnh, tên tác giả, ngày giờ đăng, tiêu đề lặp). "
                "TUYỆT ĐỐI KHÔNG: viết lại, tóm tắt, diễn đạt khác, đổi từ, đổi thứ tự, gộp hai ý, thêm câu dẫn / câu giải thích / câu chuyển vào \"text\" (lời dẫn đã có các khoá \"intro\" / \"bridges\" / \"outro\" riêng), "
                "lặp lại một câu, hay bỏ sót câu nào của thân bài. Mỗi câu của bài xuất hiện ĐÚNG MỘT LẦN. Trước khi trả lời hãy tự kiểm tra: nối các \"text\" theo thứ tự (bỏ furigana) phải tái tạo lại đúng phần thân bài. "
                f"Chỉ khi bài KHÔNG viết bằng {ngon} thì DỊCH sát nghĩa từng câu sang {ngon} (vẫn đúng thứ tự, mỗi câu gốc một câu dịch, không tóm tắt, không bỏ ý). "
                "Đặt \"quote\": true cho MỌI câu.")
    elif noi_dung == "viet_lai":
        r_nd = (f"CÁCH XỬ LÝ NỘI DUNG — VIẾT KỊCH BẢN KỂ LẠI: bạn là người đã đọc bài báo rồi CHIA SẺ LẠI cho mọi người cùng nghe để luyện nghe. Hãy TỰ VIẾT toàn bộ lời nói bằng câu chữ MỚI hoàn toàn của bạn, "
                f"kể và dẫn dắt theo mạch bài: mở đầu bằng 1–2 câu dẫn kiểu chia sẻ có NÊU NGUỒN (VD: \"Hôm nay mình đọc được bản tin của {nguon_ten} về …\" nhưng nói bằng {ngon}), rồi đi qua các ý chính theo đúng thứ tự, "
                "ở chỗ hợp lý có thể nhắc \"theo báo …\". Không cần đọc hay đi hết từng câu của bài gốc — được lược chi tiết phụ, miễn giữ đúng mạch và sự kiện. "
                "Độ dài TƯƠNG ĐƯƠNG hoặc NGẮN HƠN bài gốc (không dài ra, không thêm ý). Độ khó từ vựng và ngữ pháp phải TƯƠNG ĐƯƠNG bài gốc"
                + (f" (người dùng chọn trình độ {td_chu}: bám đúng mức này)" if td_chu else " (không đơn giản hoá, cũng không làm khó thêm)")
                + f". KHÔNG sao chép câu hay cụm từ từ 5 từ liên tiếp trở lên của bài gốc; chỉ giữ SỰ KIỆN (số liệu, tên riêng, thời gian, sự việc) vì sự kiện không có bản quyền. Bài gốc không viết bằng {ngon} thì viết thẳng bằng {ngon}. "
                "Bỏ phần rác (quảng cáo, liên kết \"đọc thêm\", chú thích ảnh, tên tác giả). "
                f"ĐƯỢC đưa TỐI ĐA 2 câu trích NGUYÊN VĂN NGẮN của bài gốc (không quá {gioi_han_cau}, đúng ngôn ngữ gốc của bài), PHẢI đánh dấu \"quote\": true; "
                "mọi câu khác là câu do bạn tự viết: \"quote\": false (hoặc bỏ trống). KHÔNG tự thêm dấu ngoặc kép vào \"text\" (app tự đặt câu trích trong ngoặc kép và ghi nguồn).")
    else:
        dau = ("CHỌN LỌC & TỰ VIẾT LẠI: chọn các ý / sự kiện quan trọng và đáng học nhất của bài (không cần dùng hết, mỗi cảnh một ý trọn vẹn), rồi " if noi_dung == "chon_loc"
               else "")
        r_nd = (f"CÁCH XỬ LÝ NỘI DUNG — {dau}TỰ VIẾT thành câu MỚI hoàn toàn của bạn: KHÔNG sao chép câu hay cụm từ từ 5 từ liên tiếp trở lên của bài gốc; chỉ giữ SỰ KIỆN (số liệu, tên riêng, "
                "thời gian, sự việc) vì sự kiện không có bản quyền. Văn phong tự nhiên, câu ngắn rõ"
                + (f", dùng từ vựng và ngữ pháp ĐÚNG TRÌNH ĐỘ {td_chu}" if td_chu else ", dễ hiểu với người học")
                + f". Bài gốc không viết bằng {ngon} thì viết thẳng bằng {ngon}. Bỏ phần rác (quảng cáo, liên kết \"đọc thêm\", chú thích ảnh, tên tác giả). "
                f"ĐƯỢC đưa TỐI ĐA 2 câu trích NGUYÊN VĂN NGẮN của bài gốc (không quá {gioi_han_cau}, đúng ngôn ngữ gốc của bài) làm ví dụ từ vựng / ngữ pháp, và PHẢI đánh dấu \"quote\": true; "
                "mọi câu khác là câu do bạn tự viết: \"quote\": false (hoặc bỏ trống). KHÔNG tự thêm dấu ngoặc kép vào \"text\" (app tự đặt câu trích trong ngoặc kép và ghi nguồn).")
    pa = {"ja": "romaji Hepburn, TÁCH TỪ, trợ từ は/を/へ viết wa/o/e", "zh-cn": "pinyin có dấu thanh, tách theo từ", "ko": "La-tinh hoá chuẩn (Revised Romanization), tách theo từ"}.get(
        language, "để chuỗi rỗng \"\" (CHỈ ghi IPA khi chắc chắn)")
    furigana_ja = (" Tiếng Nhật: MỌI chữ Hán trong \"text\" phải kèm cách đọc kana NGAY SAU cụm chữ Hán, trong ngoặc vuông — VD 学生[がくせい]です。食[た]べます。 "
                   "Quy tắc furigana: (1) trong ngoặc CHỈ có kana (hiragana / katakana) — KHÔNG romaji, KHÔNG chữ Hán, KHÔNG ký tự thừa; "
                   "(2) ngoặc đứng NGAY SAU cụm chữ Hán, không đứng sau kana / katakana / chữ Latin (viết NHKニュース, KHÔNG viết NHKニュース[ニュース]); "
                   "(3) phần okurigana (kana đuôi động từ / tính từ) nằm NGOÀI ngoặc: 認[みと]める, 食[た]べる — KHÔNG viết 認[みとめ]; "
                   "(4) từ ghép chữ Hán ghi cả cụm: 市内[しない]; (5) đọc ĐÚNG theo ngữ cảnh (VD 憤[いきどお]り) — không chắc cách đọc thì chọn từ khác dễ hơn. "
                   "(app hiện furigana nhỏ phía trên chữ Hán để người học nhìn là biết đọc; giọng đọc không đọc phần ngoặc)." if language == "ja" else "")
    quy_tac = [
        f"Người xem là người học {ngon}, tiếng mẹ đẻ là {nm}. Video được ĐỌC TO bằng {ngon} (người học nghe và đọc theo)."
        + (" Người xem là TRẺ EM: từ ngữ đơn giản, câu ngắn, giọng tích cực; bỏ chi tiết đáng sợ / bạo lực / dành cho người lớn." if audience == "kids" else ""),
        r_nd,
        "KỊCH BẢN & CHIA CẢNH: các câu trong \"lines\" chính là KỊCH BẢN NÓI của người dẫn — video nghe nó từ đầu đến cuối ở lần 1, nghe lại có bản dịch ở lần 2, rồi lần lượt qua các phần sau (chữ hiện trên màn hình chỉ để minh hoạ cho lời nói). "
        "Nếu kịch bản của phần này không quá 40 câu thì dùng DUY NHẤT MỘT cảnh; dài hơn thì chia thành vài cảnh liên tiếp, mỗi cảnh tối đa 40 câu và là MỘT phần hoàn chỉnh, "
        "giữ đúng thứ tự. Đánh số \"id\" từ 1 trong mỗi phần. \"visual.type\" luôn là \"reading\".",
        f"\"visual.data\" = {{\"title\": tiêu đề bằng {ngon} (cảnh đầu = tiêu đề bài; các cảnh sau = tên phần ngắn), \"title_tr\": bằng {nm}, "
        f"\"speakers\": [{{\"name\": tên người đọc, \"gender\": \"male\"|\"female\"}}], "
        f"\"lines\": [{{\"speaker\": 0, \"text\": câu bằng {ngon}, \"phonetic\": phiên âm ({pa}), \"translation\": nghĩa bằng {nm}, \"quote\": false"
        + (", \"vocab\": [...], \"grammar\": [...]" if giai_thich else "") + f"}}]}}.{furigana_ja}",
        f"\"title\" NGOÀI CÙNG (cùng cấp với \"scenes\") là TIÊU ĐỀ VIDEO bằng {ngon}: NGẮN (tối đa khoảng {'30 chữ' if language in ('ja', 'zh-cn', 'ko') else '12 từ'}, một dòng); "
        "nó hiện thành thẻ mở đầu của video và được người dẫn đọc to" + (", tiếng Nhật kèm furigana như ở \"text\"" if language == "ja" else "") + ".",
        (f"Mỗi \"text\" là MỘT hoặc HAI câu ngắn đọc liền, tối đa khoảng {n_toi_da} {don_vi}; câu dài thì tách thành nhiều lượt. " if noi_dung != "giu_nguyen" else
         "Mỗi \"text\" là MỘT câu NGUYÊN VẸN của bài, dài bao nhiêu cũng để nguyên (kể cả cả đoạn trích 「…」 hàng trăm chữ) — KHÔNG cắt ở dấu phẩy, KHÔNG rút gọn hay viết lại cho ngắn đi; \"translation\" dịch trọn câu đó. ")
        + "Luôn ghi \"translation\" (dịch tự nhiên, đúng nghĩa) cho MỌI lượt — người dùng có thể bật hoặc tắt hiển thị bản dịch ở ứng dụng.",
        *([(f"CÂU HỎI NGHE HIỂU (lượt câu hỏi, đứng SAU lượt nghe lại có bản dịch và TRƯỚC phần từ vựng / ngữ pháp): thêm \"quiz\" vào \"visual.data\" — 3–5 câu hỏi kiểm tra NGHE HIỂU nội dung kịch bản, "
            f"trả lời được chỉ từ những gì đã nói (ai / cái gì / khi nào / ở đâu / vì sao…), theo thứ tự nội dung; mỗi câu hỏi và đáp án đều bằng {ngon} (câu đầy đủ, ngắn gọn, đúng trình độ), "
            f"kèm bản dịch bằng {nm}: {{\"q\": câu hỏi, \"a\": đáp án, \"q_tr\": dịch câu hỏi, \"a_tr\": dịch đáp án}}. KHÔNG hỏi chi tiết không có trong kịch bản. "
            + ("Tiếng Nhật: kèm furigana trong ngoặc vuông sau chữ Hán như ở \"text\"." if language == "ja" else ""))] if co_hoi else []),
        *([(f"BÀI GIẢNG (slide): CHỈ chọn các điểm đáng học NHẤT của cả cảnh — TỔNG CỘNG 5–10 từ / cụm từ quan trọng và 1–3 mẫu ngữ pháp đáng chú ý — rồi gắn mỗi mục vào ĐÚNG câu chứa nó "
            f"(một câu tối đa 3 từ và 2 mẫu). Mục của câu nằm trong \"vocab\": {{\"w\": từ"
            + (" kèm furigana ngay sau chữ Hán, VD 食[た]べる" if language == "ja" else "") + f", \"r\": cách đọc ({pa}), \"m\": nghĩa ngắn bằng {nm} (HIỆN trên màn hình, không đọc thành tiếng), "
            f"\"say\": 1–2 câu giảng bằng {ngon} đơn giản, đúng trình độ (PHẢI nói chính từ đó ngay đầu lời giảng vì app KHÔNG đọc riêng từ trước; rồi nghĩa, cách dùng, ví dụ rất ngắn — chỉ ĐỌC thành tiếng, không hiện)}}; "
            f"và \"grammar\": {{\"p\": mẫu câu (VD 〜てほしい / have + p.p.), \"e\": giải thích ngắn bằng {nm} (hiện trên màn hình), \"say\": 1–2 câu giảng bằng {ngon}, nói chính mẫu đó ngay đầu (chỉ đọc)}}. "
            f"Câu có mục nào thì thêm \"lead\": MỘT câu dẫn dắt ngắn (tối đa khoảng 25 từ) bằng {ngon}, gợi sự tò mò về điểm đáng chú ý của câu đó (VD: \"Chú ý từ này nhé — nó hay xuất hiện lắm!\" nhưng bằng {ngon}), "
            "không dịch, không đọc lại nguyên câu, mỗi câu một cách nói khác nhau. Câu không có điểm đáng giảng thì để \"vocab\": [] và \"grammar\": [] (video bỏ qua câu đó ở phần bài giảng); "
            "KHÔNG bịa, KHÔNG liệt kê từ quá dễ (a, the, です…)."
            + (" Tiếng Nhật: \"lead\" và \"say\" cũng PHẢI kèm furigana trong ngoặc vuông sau MỌI chữ Hán như ở \"text\" — app hiện chúng thành phụ đề có cách đọc kana nhỏ phía trên chữ Hán; thiếu ngoặc thì phụ đề không có cách đọc." if language == "ja" else ""))] if giai_thich else []),
        *([(f"LỜI DẪN — mọi lời nói trong video đều bằng {ngon} (người dẫn nói, KHÔNG nói bằng {nm}; {nm} chỉ dùng cho chữ hiện trên màn hình như bản dịch), giọng thân thiện, tự nhiên, đúng trình độ người học, KHÔNG phóng đại. Thêm vào \"visual.data\": "
            f"(a) \"intro\" — CHỈ ở CẢNH ĐẦU TIÊN" + (" của bài" if not phan or phan[0] == 1 else " nếu phần này là phần đầu; phần sau KHÔNG ghi") + f": 3–5 câu ngắn: chào người xem, nói bài hôm nay về chủ đề gì "
            f"(1 câu, chỉ dựa vào bài), NÊU NGUỒN tin là {nguon_ten} và nói rõ mục đích: dùng tin tức làm nền để GIÁO DỤC — giúp người học luyện nghe và trả lời câu hỏi, giới thiệu lộ trình đúng thứ tự: {lo_trinh}, rồi mời bắt đầu; "
            f"(b) \"bridges\" — lời chuyển trước các lượt sau lượt đầu: {{{bridges_mau}}}, mỗi lời 1–2 câu dẫn vào phần kế tiếp, mỗi lượt một cách nói khác nhau (không lặp mẫu); khi nhắc tới BẢN DỊCH, nói đúng đó là bản dịch sang {nm} (bản dịch hiện trên màn hình là {nm}, KHÔNG phải {ngon}); "
            f"(c) \"outro\" — CHỈ ở CẢNH CUỐI CÙNG" + (" của bài" if not phan or phan[0] == phan[1] else " nếu phần này là phần cuối; phần khác KHÔNG ghi") + f": 2–3 câu kết: mời người xem đọc tin tức mới nhất tại {nguon_ten} (chỉ nêu tên và mời xem), nhắc lại rằng video dùng tin tức cho mục đích giáo dục (luyện nghe, trả lời câu hỏi), khuyến khích nghe lại, hẹn gặp lại; KHÔNG nói là đối tác / tài trợ / được cấp phép. "
            + ("Tiếng Nhật: kèm furigana trong ngoặc vuông sau chữ Hán như ở \"text\". " if language == "ja" else "") + "Mỗi lời dẫn tối đa 300 ký tự (\"intro\" tối đa 600).")] if loi_dan else []),
        *([("THÔNG TIN ĐĂNG YOUTUBE" + (" (đây là PHẦN CUỐI: hãy dựa vào TOÀN BỘ các phần đã nhận trong cuộc trò chuyện này, không chỉ phần này)" if phan else "")
            + ": thêm vào JSON NGOÀI CÙNG (cùng cấp với \"title\" và \"scenes\") khoá \"youtube\" để người dùng đăng video lên YouTube mà không phải soạn lại. "
            #  TIÊU ĐỀ CHO NGƯỜI HỌC (05-10-2026): bản cũ đòi "5 góc tiếp cận (tò mò, con số, câu hỏi, lời hứa, chủ đề)" + "chèn MỘT cụm {ngon} vào MỌI phương án" nên AI viết tít báo kèm đuôi vô nghĩa
            #  ("Chứng khoán Tokyo phục hồi mốc 70.000 yên và từ vựng 株価 cần nhớ") — người dùng báo không có ý nghĩa với người học. Nay: nói được người học NHẬN GÌ + móc bấm + mốc THỜI GIAN thật (app cấp, AI không bịa).
            f"(1) \"tieu_de\": 5 phương án viết bằng {nm} (người xem tìm kiếm bằng tiếng mẹ đẻ), dành cho NGƯỜI HỌC {ngon} chứ KHÔNG phải tít báo, TỐI ƯU TÌM KIẾM (SEO) trên YouTube. "
            "KHÔNG tóm tắt tin như một tít báo và KHÔNG gắn đuôi vô nghĩa kiểu \"… và từ vựng X cần nhớ\". "
            #  SEO (05-10-2026): từ khoá người học gõ tìm đứng ĐẦU tiêu đề; ngày đứng CUỐI (ngày ở đầu chiếm chỗ đẹp nhất mà không ai tìm bằng ngày).
            f"TỪ KHOÁ Ở ĐẦU: đặt cụm người học hay gõ tìm ở ĐẦU tiêu đề (vd \"Luyện nghe hiểu {ngon} {nhan_mau_td}\"); mốc thời gian đặt ở CUỐI tiêu đề (vd \"| 05/10/2026\" hoặc \"(tin ngày 05/10/2026)\"), KHÔNG đặt ở đầu. "
            "Nhãn trình độ phải được NÊU RÕ trong ÍT NHẤT 3 trong 5 phương án: "
            + (f"người dùng đã chọn trình độ {nhan_td} — ghi đúng nhãn \"{nhan_td}\" (đủ chữ {thang_td}, không chỉ \"{nhan_td.split(' ')[-1]}\"), không đổi mức"
               if td_chu else f"người dùng chưa chọn trình độ — hãy tự chọn mức của thang {thang_td} mà kịch bản bạn viết phù hợp nhất; được ghi HAI mức liền kề để phủ rộng tìm kiếm nhưng PHẢI viết dạng \"{vd_hai_muc}\" (một chữ {thang_td}, các mức cách nhau dấu /)")
            + ", KHÔNG viết kiểu \"N3-N2\" hay \"B1–B2\", và mọi phương án dùng CÙNG một cách ghi trình độ. "
            f"Mỗi phương án phải cho người học thấy MÌNH ĐƯỢC GÌ (chỉ nói kỹ năng có thật trong video: {ky_nang}; nếu hứa số từ vựng thì ghi \"vài\" hoặc \"5–10\", không bịa con số khác) "
            "và có MÓC khiến họ muốn bấm (thử thách, tò mò, lời hứa cụ thể, câu hỏi đúng điều người học thắc mắc). "
            f"TÍNH THỜI SỰ để tiêu đề \"tươi\": ÍT NHẤT 3 trong 5 phương án có MỐC THỜI GIAN (ở CUỐI), và CHỈ dùng thông tin này (không tự bịa ngày): {ngay_txt}. "
            "Cách ghi: ngày đầy đủ (vd 05/10/2026). Chỉ được viết \"mới nhất\" / \"hôm nay\" / \"tuần này\" khi ngày đăng của bài gốc (nếu app cung cấp) trùng hoặc cách ngày làm video không quá 2 ngày; bài cũ hơn thì ghi ĐÚNG ngày đăng (\"tin ngày …\"); "
            "không biết ngày đăng thì chỉ ghi NGÀY LÀM VIDEO dạng ngày đầy đủ. TUYỆT ĐỐI không viết \"hôm nay / mới nhất / vừa xảy ra\" cho tin cũ hơn hoặc không rõ ngày. "
            f"5 phương án theo 5 CÔNG THỨC KHÁC HẲN NHAU, phương án ĐẦU TIÊN (được dùng làm mặc định) theo công thức (a): "
            f"(a) ĐÚNG DẠNG \"Luyện nghe hiểu {ngon} {nhan_mau_td}: <chi tiết RIÊNG của bài, ngắn> | tin tức <mới nhất> <dd/mm/yyyy>\" "
            f"(bỏ chữ \"mới nhất\" khi chưa đủ điều kiện dùng ở luật thời gian trên; chỉ khi tổng độ dài vẫn còn dưới 95 ký tự mới được thêm kỹ năng ({ky_nang_ghep}) trước phần tin tức); "
            "(b) thử thách nghe hiểu có trình độ (kiểu \"Nghe hiểu được bao nhiêu? …\"), ngày ở cuối; "
            f"(c) lời hứa cụ thể về từ vựng / mẫu câu học được từ tin, kèm MỘT cụm {ngon} nổi bật của bài + nghĩa của nó, hoặc đặt cụm đó thành câu hỏi (\"… là gì?\"); "
            "(d) câu hỏi / điều tò mò về chính nội dung tin, viết cho người học; "
            f"(e) trình độ + chi tiết riêng của bài + lộ trình luyện (đúng lộ trình video: {ky_nang_ghep}). Chỉ phương án (c) bắt buộc có cụm {ngon}. "
            "ĐỘ DÀI (bắt buộc): TỰ ĐẾM ký tự từng tiêu đề, tính cả khoảng trắng — phương án (a) TỐI ĐA 95 ký tự, các phương án (b)–(e) TỐI ĐA 70 ký tự; vượt là SAI (YouTube chỉ nhận 100 ký tự, app sẽ phải cắt mất phần cuối) — quá dài thì BỎ bớt chữ chứ đừng viết dài rồi để bị cắt. Mọi phương án không dùng dấu < >, không viết hoa toàn bộ. "
            "PHÂN BIỆT GIỮA CÁC VIDEO: một ngày có thể đăng NHIỀU video cùng trình độ, cùng kỹ năng, cùng ngày — trình độ / kỹ năng / ngày chỉ là KHUNG chung. MỖI phương án PHẢI mang ít nhất MỘT chi tiết RIÊNG của bài này "
            "(tên riêng, địa danh, sự kiện, con số) để người xem phân biệt với video khác; phần <chi tiết RIÊNG> của (a) phải CỤ THỂ kiểu đó, không viết chung chung (\"tin kinh tế\", \"tin xã hội\"). Hai bài khác nhau mà tiêu đề đọc lên giống nhau là SAI. "
            f"Tiêu đề là chữ thường của YouTube: KHÔNG dùng furigana trong ngoặc vuông (viết 否認, KHÔNG viết 否認[ひにん]). PHẢI trung thực với bài: không giật gân sai sự thật, không thêm sự kiện. "
            f"(2) \"the\": 10–15 thẻ tìm kiếm, trộn {nm}, {ngon} và tiếng Anh, chữ thường, không dấu #, xếp từ sát nghĩa nhất — ưu tiên cụm NGƯỜI HỌC hay gõ tìm: luyện nghe {ngon}, nghe hiểu {ngon} {thang_td}, mỗi mức trình độ MỘT thẻ riêng (vd \"{thang_td.lower()} …\"), học {ngon} qua tin tức, shadowing, từ vựng theo chủ đề; rồi tới từ khoá RIÊNG của bài (tên riêng, địa danh, sự kiện); tổng độ dài kể cả dấu phẩy dưới 450 ký tự. "
            f"(3) \"hashtag\": 3 hashtag có dấu # (ít nhất 1 hashtag nêu trình độ + việc nghe hiểu {ngon}, 1 hashtag theo chủ đề của bài). KHÔNG dùng tên báo làm thẻ hoặc hashtag. "
            f"(4) \"hook_anh\": 6 dòng chữ để đắp lên ảnh đại diện, bằng {nm}, mỗi dòng TỐI ĐA 10 từ nhưng là MỘT Ý ĐỦ HIỂU: người xem lướt qua là biết video nói chuyện gì và họ được gì — KHÔNG viết từ khoá cụt kiểu \"Tin mới\" / \"Luyện Shadowing\"; "
            "dạng <chi tiết riêng của bài> + <điều người học làm / nhận được>. Không trùng nguyên văn tiêu đề. Trong 6 dòng chỉ được TỐI ĐA 2 dòng chung (trình độ / kỹ năng / \"Nghe được bao nhiêu?\"); ÍT NHẤT 4 dòng phải nói chi tiết RIÊNG của bài này (tên riêng, địa danh, sự kiện, con số) "
            "để ảnh bìa các video đăng CÙNG NGÀY KHÔNG giống nhau. "
            "KHÔNG viết mô tả video (app tự dựng mô tả đa ngôn ngữ có ghi nguồn và bản quyền).")] if co_yt else []),
        "NGƯỜI ĐỌC: \"speakers[0]\" là NGƯỜI DẪN (tên chung như \"Phát thanh viên\" / \"Reporter\" bằng " + ngon + "). CHỈ khi bài có LỜI TRÍCH TRỰC TIẾP của một người thì thêm người đó vào "
        "\"speakers\" (tối đa 4 người, ghi đúng giới tính nếu bài nói rõ) và gán các câu trích cho họ bằng \"speaker\" = số thứ tự; mọi câu còn lại để \"speaker\": 0.",
        "TRUNG THỰC: KHÔNG thêm sự kiện, số liệu, tên riêng hay lời trích không có trong bài; KHÔNG bình luận, KHÔNG đổi ý nghĩa của sự kiện. Phiên âm / cách đọc CHỈ ghi khi chắc chắn.",
        "Không thêm trường nào khác ngoài các trường đã nêu. Không giải thích. Chỉ trả về MỘT khối JSON.",
    ]
    mau_json = (
        '{\n  "title": "Tiêu đề bài",\n  "scenes": [\n    {\n      "id": 1,\n      "visual": {\n        "type": "reading",\n        "data": {\n'
        '          "title": "…", "title_tr": "…",\n'
        + ('          "intro": "…", "bridges": {' + bridges_mau + '}, "outro": "…",\n' if loi_dan else "")
        + ('          "quiz": [{"q": "…", "a": "…", "q_tr": "…", "a_tr": "…"}],\n' if co_hoi else "")
        + '          "speakers": [{"name": "…", "gender": "female"}, {"name": "…", "gender": "male"}],\n          "lines": [\n'
        '            {"speaker": 0, "text": "…", "phonetic": "…", "translation": "…", "quote": false'
        + (', "lead": "…", "vocab": [{"w": "…", "r": "…", "m": "…", "say": "…"}], "grammar": [{"p": "…", "e": "…", "say": "…"}]' if giai_thich else "")
        + '},\n            {"speaker": 1, "text": "…", "phonetic": "…", "translation": "…"}\n'
        '          ]\n        }\n      }\n    }\n  ]'
        + (',\n  "youtube": {"tieu_de": ["…", "…", "…", "…", "…"], "the": ["…", "…"], "hashtag": ["#…", "#…", "#…"], "hook_anh": ["…", "…"]}' if co_yt else "")
        + '\n}'
    )
    danh_sach = "\n".join(f"{i}. {q}" for i, q in enumerate(quy_tac, 1))
    nguon_txt = f" (nguồn: {nguon})" if nguon else ""
    return (f"Bạn là biên tập viên bài đọc cho người học ngoại ngữ. Hãy biến NỘI DUNG BÀI BÁO bên dưới thành các cảnh đọc để dựng video {aspect}.{ghi_phan}\n\n"
            f"QUY TẮC:\n{danh_sach}\n\nĐỊNH DẠNG TRẢ VỀ (ví dụ cấu trúc, thay phần … bằng nội dung thật):\n{mau_json}\n\n"
            f"NỘI DUNG BÀI BÁO{nguon_txt}:\n\"\"\"\n{kb}\n\"\"\"\n")
