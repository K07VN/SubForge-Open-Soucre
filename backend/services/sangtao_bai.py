"""KẾ HOẠCH ĐOẠN TIẾNG của một cảnh BÀI ĐỌC (đọc báo / đọc văn bản, có thể phân vai) và mốc thời gian của từng đoạn.

Một cảnh được phát qua tối đa NĂM LƯỢT (mỗi lượt đọc lại cả cảnh; người dùng bật / tắt từng lượt, thứ tự cố định):
  1. ``doc``     đọc — danh sách (trang sách), chỉ ngôn ngữ đang học;
  2. ``dich``    đọc có BẢN DỊCH — như lượt 1, thêm bản dịch dưới mỗi câu;
  3. ``hoi``     CÂU HỎI kiểm tra nghe hiểu — từng câu hỏi (tiếng đích) → thời gian suy nghĩ → ĐÁP ÁN; chỉ có khi cảnh có ``quiz``;
  4. ``giang``   BÀI GIẢNG — slide cho từng câu: câu gốc → bản dịch → từ vựng bóc tách → mẫu ngữ pháp; mỗi phần hiện (và được đọc: từ / mẫu câu bằng giọng ngôn
                 ngữ đang học, nghĩa / giải thích bằng giọng tiếng mẹ đẻ) đúng lúc;
  5. ``shadow``  người học tự NHẠI THEO (shadowing) — như lượt 1, KHÔNG có bản dịch (tập trung vào âm thanh, nhịp, ngữ điệu), sau mỗi câu chừa khoảng lặng
                 bằng ``khe_nhac`` × thời gian đọc câu để nhại lại.

MỌI LỜI NÓI trong video đều bằng TIẾNG ĐÍCH (ngôn ngữ đang học), do giọng NGƯỜI DẪN (``speakers[0]``) đọc; chữ tiếng mẹ đẻ (bản dịch, nghĩa của từ, giải thích mẫu) chỉ HIỆN trên màn hình.

LỜI DẪN (tắt được bằng ``loi_dan``): lời CHÀO + lộ trình các lượt ở đầu video (chỉ cảnh đầu), lời CHUYỂN trước mỗi lượt sau lượt đầu, lời KẾT ở cuối video (chỉ cảnh cuối).
Trong bài giảng (bật ``doc_giai``) có thêm câu DẪN DẮT (``lead``) trước phần từ vựng / ngữ pháp của mỗi câu và lời GIẢNG (``say``) của từng từ / mẫu ngữ pháp. Chữ do AI viết
(``intro``, ``bridges``, ``outro``, ``lead``, ``say``); thiếu thì chào / chuyển / kết dùng câu mẫu có sẵn (:data:`LOI`, chỉ có cho vài ngôn ngữ — ngôn ngữ khác thì không có lời
dẫn mẫu), còn ``lead`` / ``say`` thiếu thì không có (không bịa; nghĩa của từ chỉ hiện chữ). Bài giảng chỉ giảng các câu CÓ từ vựng / ngữ pháp (nếu có ít nhất một câu như vậy) —
không giảng lại từng câu.

Hàm thuần, không đụng mạng/đĩa — dùng chung cho ba nơi để KHÔNG BAO GIỜ lệch nhau:
  * bộ tạo giọng (``sangtao_du_an``): biết cần đọc những đoạn nào, bằng giọng nào, nối lại thế nào;
  * bộ dựng khung (``sangtao_hoc``): biết lúc nào đoạn nào đang đọc để viết chữ / làm nổi bật đúng lúc;
  * bộ ước lượng khi chưa có tiếng thật (không bật giọng đọc / chưa tạo tiếng).

Đoạn (``dict``): ``i`` số thứ tự câu trong cảnh (-1 = lời dẫn của cả lượt) · ``luot`` số thứ tự lượt phát · ``pk`` loại lượt (doc | dich | giang | shadow) · ``kieu`` "list" |
"slide" | "quiz" (câu hỏi) | "loi" (thẻ lời dẫn: chào / chuyển lượt / kết) · ``ld`` lượt này
CÓ hiện bản dịch · ``shadow`` lượt nhại theo (có khoảng lặng) · ``el`` phần tử được đọc/hiện: "t" câu gốc, "l" câu dẫn dắt (chỉ nói, không có chỗ trên slide), "v<n>" từ vựng (nếu có lời giảng thì đoạn này LÀ lời giảng), "g<n>" mẫu ngữ pháp (như vậy). Bản dịch, phiên âm, nghĩa của từ và giải
thích mẫu KHÔNG có đoạn riêng: chúng hiện CÙNG LÚC với câu gốc / từ / mẫu (bộ dựng hình lo), "n" lời dẫn đầu lượt, "k" lời kết, "q" / "a" câu hỏi / đáp án (``i`` = số thứ tự câu hỏi) · ``giu`` (tuỳ chọn) giữ thêm giây SAU đoạn cho kịp đọc chữ hiện cùng lúc · ``phu`` (tuỳ chọn) chữ (còn cách đọc [kana]) làm PHỤ ĐỀ nhỏ ở đáy video lúc đoạn này nói — chỉ có ở đoạn CHỈ NÓI mà hình không có chữ tương ứng (câu dẫn dắt "l", lời giảng ``say`` của từ vựng / ngữ pháp) · ``van`` chữ thuần để đọc · ``nguoi`` người đọc (số thứ tự trong ``speakers``; -1 = giọng tiếng mẹ đẻ) · ``ngon`` "tgt" (ngôn ngữ đang học) |
"nat" (tiếng mẹ đẻ) · ``am`` có tiếng (False = chỉ hiện chữ, giữ một khoảng thời gian đủ để đọc).
"""
from __future__ import annotations

from services import sangtao_ruby as RB

#: các lượt phát, theo thứ tự cố định
LUOT = ("doc", "dich", "hoi", "giang", "shadow")
DAU_CANH = 0.4          # lặng mở đầu cảnh: chỉ đủ cho chuyển cảnh mờ vào (tiêu đề hiện NGAY, không còn chờ bút viết — trước đây 1,4 s im lặng đầu mỗi cảnh)
KHE_CAU = 0.5           # lặng giữa hai câu (danh sách)
KHE_SLIDE = 0.7         # lặng giữa hai slide
KHE_DOAN = 0.25         # lặng giữa hai phần trong một slide
KHE_LUOT = 1.5          # lặng giữa hai LƯỢT phát (kịp đổi nhãn "Lần 2")
KHE_NGHI = 4.0          # thời gian suy nghĩ TỐI THIỂU sau mỗi câu hỏi (thực tế = max(KHE_NGHI, 1,2 × thời gian đọc câu hỏi + 2))
KHE_LOI = 0.9           # lặng sau một lời dẫn (chào / chuyển lượt) trước khi vào nội dung
CUOI_CANH = 0.8
KHE_NHAC_TOI_DA = 3.0
#: bản cũ dùng ``translation_mode`` — đổi sang danh sách lượt
_CU = {"khong": ["doc"], "cung": ["dich"], "sau": ["doc", "dich"], "truoc": ["dich", "doc"], "giang": ["doc", "giang"]}

#: ký tự (hoặc âm tiết) đọc được mỗi giây theo ngôn ngữ — chỉ để ƯỚC LƯỢNG khi chưa có tệp tiếng thật
CPS = {"vi": 14.0, "en": 15.0, "ja": 7.0, "zh-cn": 5.0, "ko": 9.0, "fr": 15.0, "de": 14.0, "es": 15.0, "pt": 15.0, "it": 15.0, "ru": 13.0}


def tuy_chon(st: dict | None) -> dict:
    """Chuẩn hoá các tuỳ chọn bài đọc từ cài đặt dự án (giá trị lạ → mặc định an toàn). ``passes`` = các lượt bật (thứ tự luôn là doc, dich, giang, shadow)."""
    st = st or {}
    ds = st.get("passes")
    if not isinstance(ds, (list, tuple)):
        ds = st.get("luot")                                                       # đầu ra của chính hàm này (chuẩn hoá lại phải cho đúng kết quả cũ)
    if not isinstance(ds, (list, tuple)):
        ds = _CU.get(st.get("translation_mode") or st.get("dich") or "khong", ["doc"])
    luot = [x for x in LUOT if x in ds] or ["doc"]
    try:
        khe = float(st.get("shadow_gap") if st.get("shadow_gap") is not None else st.get("khe_nhac") if st.get("khe_nhac") is not None else 1.0)
    except (TypeError, ValueError):
        khe = 1.0
    doc = st.get("read_explanation", st.get("doc_giai", True))
    loi = st.get("narration", st.get("loi_dan", True))
    nm = str(st.get("native_language") or st.get("ngon_me") or "vi")
    nd = str(st.get("ngon_dich") or st.get("language") or "")
    return {"luot": luot, "khe_nhac": min(KHE_NHAC_TOI_DA, max(0.0, khe)), "doc_giai": bool(True if doc is None else doc),
            "loi_dan": bool(True if loi is None else loi), "ngon_me": nm, "ngon_dich": nd, "ten_nguon": str(st.get("ten_nguon") or "").strip()[:80]}


def cac_luot(opts: dict) -> list[dict]:
    """Các lượt phát của một cảnh: ``[{"pk", "ld": có hiện bản dịch, "kieu": "list" | "slide", "shadow": bool}]``."""
    cau_hinh = {"doc": (False, "list", False), "dich": (True, "list", False), "hoi": (False, "quiz", False), "giang": (True, "slide", False), "shadow": (False, "list", True)}
    return [{"pk": p, "ld": cau_hinh[p][0], "kieu": cau_hinh[p][1], "shadow": cau_hinh[p][2]} for p in (opts.get("luot") or ["doc"]) if p in cau_hinh]


#: câu mẫu của lời dẫn theo ngôn ngữ ĐÍCH (ngôn ngữ đang học — người dẫn nói bằng thứ tiếng này) — dùng khi AI không viết lời dẫn riêng. Tiếng Nhật có furigana [kana] cho người học.
#: ``c_*`` = lời chuyển trước lượt; ``chao`` / ``chao_td`` = lời chào (không / có tên bài, ``{t}``); ``vao`` = câu chốt phần chào; ``ket`` = lời kết.
LOI = {
    "vi": {"cn": ("Đầu tiên, ", "Tiếp theo, ", "Cuối cùng, ", "Sau đó, "), "chao": "Xin chào các bạn! Hôm nay chúng ta cùng học qua một bài báo.",
           "chao_td": "Xin chào các bạn! Hôm nay chúng ta cùng học qua một bài báo có tựa đề “{t}”.",
           "p_doc": "bạn sẽ nghe đọc toàn bộ bài.", "p_dich": "chúng ta đọc lại, lần này có kèm bản dịch.",
           "p_hoi": "mình sẽ hỏi bạn vài câu để kiểm tra xem bạn đã nghe hiểu chưa.", "dap_an": "Đáp án là: ",
           "c_hoi": "Bây giờ là phần câu hỏi. Mình sẽ hỏi vài câu về bài vừa nghe — bạn hãy thử trả lời trước, rồi mình sẽ cho đáp án nhé.",
           "p_giang": "mình sẽ cùng phân tích những từ vựng và mẫu ngữ pháp đáng chú ý.", "p_shadow": "là phần luyện shadowing: bạn nhắc lại theo từng câu thật to nhé.",
           "vao": "Nào, chúng ta bắt đầu thôi!",
           "nguon_mo": "Bài hôm nay dựa trên tin tức của {n}. Mình chỉ dùng tin tức để giáo dục: giúp bạn luyện nghe và trả lời câu hỏi.",
           "nguon_ket": "Bạn có thể xem tin tức mới nhất tại {n} nhé. Mình chỉ dùng tin tức làm nền để giáo dục — luyện nghe và trả lời câu hỏi.",
           "c_doc": "Chúng ta tiếp tục với phần tiếp theo của bài.",
           "c_dich": "Bây giờ chúng ta đọc lại bài một lần nữa, lần này có kèm bản dịch để bạn đối chiếu.",
           "c_giang": "Bây giờ là phần bài giảng. Mình sẽ chọn ra những từ vựng và mẫu ngữ pháp đáng chú ý trong bài để cùng phân tích.",
           "c_shadow": "Cuối cùng là phần luyện shadowing. Sau mỗi câu sẽ có một khoảng lặng — bạn hãy nhắc lại thật to, bắt chước cả nhịp điệu và ngữ điệu nhé.",
           "ket": "Vậy là chúng ta đã đi hết bài báo hôm nay.", "ket2": "Hãy nghe lại vài lần để quen tai, và hẹn gặp lại bạn ở bài tiếp theo!"},
    "en": {"cn": ("First, ", "Next, ", "Finally, ", "Then, "), "chao": "Hello, everyone! Today we're learning from a news article.",
           "chao_td": "Hello, everyone! Today we're learning from a news article titled “{t}”.",
           "p_doc": "you'll listen to the whole article read aloud.", "p_dich": "we'll read it again, this time with the translation.",
           "p_hoi": "I'll ask you a few questions to check what you understood.", "dap_an": "The answer is: ",
           "c_hoi": "Now it's quiz time. I'll ask a few questions about the article — try to answer before I give you the answer.",
           "p_giang": "we'll break down some useful vocabulary and grammar points.", "p_shadow": "it's shadowing practice: repeat each sentence out loud after me.",
           "vao": "Let's get started!",
           "nguon_mo": "Today's story is based on news from {n}. We use news only for education — to help you practise listening and answering questions.",
           "nguon_ket": "For the latest news, you can visit {n}. We use news only as a base for education: listening and question practice.",
           "c_doc": "Let's move on to the next part of the article.",
           "c_dich": "Now let's read it once more, this time with the translation so you can compare.",
           "c_giang": "Now for the lesson. Let's pick out the key words and grammar points in this article and look at them closely.",
           "c_shadow": "Finally, shadowing practice. After each sentence there's a short pause — repeat it out loud, copying the rhythm and intonation.",
           "ket": "That's the whole article for today.", "ket2": "Listen again a few times to train your ear, and see you in the next lesson!"},
    "ja": {"cn": ("まず、", "次[つぎ]に、", "最後[さいご]に、", "そのあと、"), "khong_cach": True, "chao": "みなさん、こんにちは！今日[きょう]はニュース記事[きじ]で一緒[いっしょ]に勉強[べんきょう]しましょう。",
           "chao_td": "みなさん、こんにちは！今日[きょう]は「{t}」という記事[きじ]で一緒[いっしょ]に勉強[べんきょう]しましょう。",
           "p_doc": "記事[きじ]全体[ぜんたい]を通[とお]して聞[き]きます。", "p_dich": "もう一度[いちど]、今度[こんど]は翻訳[ほんやく]つきで読[よ]みます。",
           "p_hoi": "内容[ないよう]について、いくつか質問[しつもん]します。", "dap_an": "答[こた]えは、",
           "c_hoi": "では、ここで質問[しつもん]です。今[いま]の記事[きじ]についてたずねますので、先[さき]に答[こた]えを考[かんが]えてみてください。そのあとで、答[こた]えをお伝[つた]えします。",
           "p_giang": "注目[ちゅうもく]したい語彙[ごい]と文法[ぶんぽう]を一緒[いっしょ]に解説[かいせつ]します。",
           "p_shadow": "シャドーイングの練習[れんしゅう]です。一文[いちぶん]ずつ、声[こえ]に出[だ]して真似[まね]してみましょう。",
           "vao": "それでは、始[はじ]めましょう！",
           "nguon_mo": "今日[きょう]のニュースは、{n}の報道[ほうどう]をもとにしています。聞[き]き取[と]りと、質問[しつもん]に答[こた]える練習[れんしゅう]のために使[つか]っています。",
           "nguon_ket": "最新[さいしん]のニュースは、{n}でご覧[らん]くださいね。ニュースは、聞[き]き取[と]りと質問[しつもん]に答[こた]える練習[れんしゅう]のための、教育[きょういく]目的[もくてき]で使[つか]っています。",
           "c_doc": "記事[きじ]の続[つづ]きに進[すす]みましょう。",
           "c_dich": "では、もう一度[いちど]読[よ]みます。今度[こんど]は翻訳[ほんやく]も見[み]ながら確認[かくにん]しましょう。",
           "c_giang": "ここからは解説[かいせつ]パートです。記事[きじ]の中[なか]から大切[たいせつ]な語彙[ごい]と文法[ぶんぽう]を取[と]り上[あ]げて、一緒[いっしょ]に見[み]ていきましょう。",
           "c_shadow": "最後[さいご]はシャドーイングの練習[れんしゅう]です。一文[いちぶん]ごとに間[ま]があるので、リズムやイントネーションもまねして、声[こえ]に出[だ]して繰[く]り返[かえ]してください。",
           "ket": "以上[いじょう]で今日[きょう]の記事[きじ]は終[お]わりです。", "ket2": "何度[なんど]か聞[き]き直[なお]して耳[みみ]を慣[な]らしてくださいね。それでは、また次回[じかい]お会[あ]いしましょう！"},
    "zh-cn": {"cn": ("首先，", "接着，", "最后，", "然后，"), "khong_cach": True, "chao": "大家好！今天我们通过一篇新闻文章来学习。",
              "chao_td": "大家好！今天我们通过一篇题为《{t}》的新闻文章来学习。",
              "p_doc": "我们完整地听一遍整篇文章。", "p_dich": "再读一遍，这次附上翻译。",
              "p_hoi": "我会提几个问题，看看你听懂了多少。", "dap_an": "答案是：",
              "c_hoi": "现在进入提问环节。我会问几个关于刚才文章的问题，请你先想一想，再听我公布答案。",
              "p_giang": "我们一起分析值得注意的词汇和语法。", "p_shadow": "是影子跟读练习：请你跟着每一句大声重复。",
              "vao": "那么，我们开始吧！",
              "nguon_mo": "今天的内容参考了{n}的新闻，我们只把新闻用于教育：帮助你练习听力和回答问题。",
              "nguon_ket": "想看最新的新闻，可以访问{n}哦。我们只是以新闻为素材做教育：练习听力和回答问题。",
              "c_doc": "我们继续往下读文章的下一部分。",
              "c_dich": "现在我们再读一遍，这次附上翻译，方便你对照理解。",
              "c_giang": "接下来是讲解部分。我们从文章中挑出重点词汇和语法，一起仔细分析。",
              "c_shadow": "最后是影子跟读练习。每句话之后会有一小段停顿，请你大声重复，并模仿节奏和语调。",
              "ket": "今天的文章就学到这里。", "ket2": "多听几遍，让耳朵熟悉起来，我们下期再见！"},
    "ko": {"cn": ("먼저, ", "다음으로, ", "마지막으로, ", "그다음에는 "), "chao": "안녕하세요, 여러분! 오늘은 뉴스 기사로 함께 공부해 봅시다.",
           "chao_td": "안녕하세요, 여러분! 오늘은 “{t}”라는 제목의 뉴스 기사로 함께 공부해 봅시다.",
           "p_doc": "기사 전체를 한 번 들어 봅니다.", "p_dich": "이번에는 번역과 함께 다시 읽어 봅니다.",
           "p_hoi": "이해했는지 확인하는 질문을 몇 가지 드립니다.", "dap_an": "정답은 ",
           "c_hoi": "이제 질문 시간입니다. 방금 들은 기사에 대해 몇 가지 묻겠습니다. 먼저 답을 생각해 본 다음, 정답을 확인해 보세요.",
           "p_giang": "눈여겨볼 어휘와 문법을 함께 분석합니다.", "p_shadow": "쉐도잉 연습입니다. 문장마다 소리 내어 따라 말해 보세요.",
           "vao": "그럼 시작해 볼까요?",
           "nguon_mo": "오늘 내용은 {n}의 뉴스를 바탕으로 했습니다. 듣기와 질문에 답하는 연습을 위한 교육 목적으로만 사용합니다.",
           "nguon_ket": "최신 뉴스는 {n}에서 확인해 보세요. 뉴스는 듣기와 질문 답하기 연습을 위한 교육용 자료로만 활용합니다.",
           "c_doc": "기사의 다음 부분으로 넘어가 봅시다.",
           "c_dich": "이제 한 번 더 읽어 볼게요. 이번에는 번역을 함께 보며 확인해 보세요.",
           "c_giang": "이제 해설 시간입니다. 기사에서 중요한 어휘와 문법을 골라 함께 살펴보겠습니다.",
           "c_shadow": "마지막은 쉐도잉 연습입니다. 문장마다 잠시 멈추니, 리듬과 억양까지 따라 하며 소리 내어 말해 보세요.",
           "ket": "오늘 기사는 여기까지입니다.", "ket2": "몇 번 더 들으며 귀를 익혀 보세요. 다음 시간에 또 만나요!"},
}
#: lời dẫn dài tối đa (ký tự) khi AI viết — cắt ở bộ chuẩn hoá (:mod:`sangtao_du_lieu_hinh`); ở đây chỉ để nhắc cùng một nơi
KIEU_LUOT_LOI = LUOT


def _doan(i: int, k: int, lt: dict, el: str, van: str, nguoi: int, ngon: str, am: bool, kieu: str | None = None, hien: str | None = None, giu: float = 0.0, phu: str | None = None) -> dict:
    d = {"i": i, "luot": k, "pk": lt["pk"], "kieu": kieu or lt["kieu"], "ld": lt["ld"], "shadow": lt["shadow"], "el": el, "van": van, "nguoi": nguoi, "ngon": ngon, "am": am}
    if giu > 0:                                                                       # giữ thêm ``giu`` giây SAU đoạn này để người xem kịp đọc chữ hiện cùng lúc (bản dịch, nghĩa)
        d["giu"] = float(giu)
    if hien is not None:                                                            # chữ hiện trên thẻ lời dẫn (giữ cách đọc [kana]); ``van`` là chữ trơn để đọc
        d["hien"] = hien
    if phu:                                                                           # đoạn CHỈ NÓI, hình không có chữ tương ứng: bộ dựng hình vẽ PHỤ ĐỀ nhỏ ở đáy video (giữ cách đọc [kana])
        d["phu"] = phu
    return d


#: Câu RẤT dài (ước tính nói ≥ 15 s): bản dịch của nó chỉ có chút thời gian ở cuối lời đọc (giọng hết là sang câu kế) nên GIỮ khung hình thêm vài giây SAU câu — "đứng hình" — để người xem kịp
#: bấm tạm dừng và đọc (người dùng đề nghị 07-10-2026). Chỉ ở lượt có bản dịch / slide; câu có bản dịch hiện cùng lúc đang đọc cũng được hưởng (ít khi thừa quá vài giây).
GIU_CAU_DAI_NGUONG = 15.0
GIU_CAU_DAI_TOI_DA = 4.0


def giu_cau_dai(ln: dict, ngon: str) -> float:
    """Số giây giữ thêm sau một câu rất dài CÓ bản dịch (0 nếu câu không dài hoặc không có bản dịch): từ 2 s, tăng theo độ dài bản dịch, tối đa ``GIU_CAU_DAI_TOI_DA``.
    "Rất dài" = thời gian nói ước tính (số chữ ÷ ``CPS`` của ngôn ngữ đích) ≥ ``GIU_CAU_DAI_NGUONG``; tiếng Nhật ≈ 105 chữ, tiếng Anh ≈ 225 chữ."""
    tr = "".join(str(ln.get("translation") or "").split())
    if not tr:
        return 0.0
    n = len("".join(RB.bo_ruby(str(ln.get("text") or "")).split()))
    if n / CPS.get(ngon, 12.0) < GIU_CAU_DAI_NGUONG:
        return 0.0
    return min(GIU_CAU_DAI_TOI_DA, max(2.0, 1.5 + 0.01 * len(tr)))


def danh_dau_vi_tri(canhs: list) -> None:
    """Đánh dấu cảnh bài đọc ĐẦU và CUỐI của video (``_dau`` / ``_cuoi`` trong ``visual.data``): lời chào mở đầu chỉ ở cảnh đầu, lời kết chỉ ở cảnh cuối. Gọi trên danh sách ĐẦY ĐỦ
    các cảnh (trước khi lọc ra vài cảnh xem thử) để cảnh giữa không bị nhận nhầm là cảnh đầu."""
    ds = [c for c in canhs if (c.get("visual") or {}).get("type") == "reading" and isinstance((c.get("visual") or {}).get("data"), dict)]
    for n, c in enumerate(ds):
        d = c["visual"]["data"]
        d["_dau"], d["_cuoi"] = n == 0, n == len(ds) - 1


def _noi(mau: dict, cau: list[str]) -> str:
    """Nối các câu (tiếng Nhật / Trung viết liền, còn lại cách nhau một dấu cách)."""
    return ("" if mau.get("khong_cach") else " ").join(x for x in cau if x)


def _loi_chao(du_lieu: dict, mau: dict, pks: list[str], ten_nguon: str = "") -> str:
    """Lời chào + (nguồn tin và mục đích giáo dục) + lộ trình các lượt (đúng những lượt đang bật) + câu chốt — dựng từ câu mẫu."""
    td = str(du_lieu.get("title") or "").strip()
    cn = mau["cn"]
    cau = [mau["chao_td"].replace("{t}", td) if td else mau["chao"]]
    if ten_nguon:
        cau.append(mau["nguon_mo"].replace("{n}", ten_nguon))
    for n, pk in enumerate(pks):
        tu = "" if len(pks) == 1 else cn[0] if n == 0 else cn[2] if n == len(pks) - 1 else cn[1] if n % 2 == 1 else cn[3]      # các lượt giữa luân phiên hai từ nối cho đỡ lặp
        cau.append(tu + mau["p_" + pk])
    cau.append(mau["vao"])
    return _noi(mau, cau)


def _loi_dan(du_lieu: dict, opts: dict, pks: list[str]) -> dict:
    """Lời dẫn của cảnh, bằng TIẾNG ĐÍCH: ``{"mo": lời chào, "cau": {lượt: lời chuyển}, "ket": lời kết}`` — chữ của AI (nếu có) thắng câu mẫu; ngôn ngữ đích không có câu mẫu thì
    chỉ dùng chữ của AI (thiếu thì chuỗi rỗng = không có lời dẫn đó)."""
    mau = LOI.get(str(opts.get("ngon_dich") or ""))
    ten = str(opts.get("ten_nguon") or "").strip()
    cau_ai = du_lieu.get("bridges") if isinstance(du_lieu.get("bridges"), dict) else {}
    intro, outro = str(du_lieu.get("intro") or "").strip(), str(du_lieu.get("outro") or "").strip()
    if mau and ten:                                                              # lời của AI mà không nhắc tới nguồn thì app tự thêm câu nguồn + mục đích giáo dục
        if intro and ten.lower() not in intro.lower():
            intro = _noi(mau, [intro, mau["nguon_mo"].replace("{n}", ten)])
        if outro and ten.lower() not in outro.lower():
            outro = _noi(mau, [outro, mau["nguon_ket"].replace("{n}", ten)])
    return {"mo": intro or (_loi_chao(du_lieu, mau, pks, ten) if mau else ""),
            "cau": {pk: str(cau_ai.get(pk) or "").strip() or (mau["c_" + pk] if mau else "") for pk in KIEU_LUOT_LOI},
            "ket": outro or (_noi(mau, [mau["ket"], mau["nguon_ket"].replace("{n}", ten) if ten else "", mau["ket2"]]) if mau else "")}


def _mau_cau(p: str) -> str:
    """Mẫu ngữ pháp bỏ ký hiệu dẫn đầu (〜 ~ …) để giọng đọc không đọc thành tên ký hiệu."""
    return RB.bo_ruby(p).lstrip("〜~～…・.· ").strip() or RB.bo_ruby(p)


def co_giai_thich(ln: dict) -> bool:
    """Câu có từ vựng / ngữ pháp cần giảng không."""
    return bool(ln.get("vocab") or ln.get("grammar"))


def ke_hoach(du_lieu: dict, opts: dict) -> list[dict]:
    """Danh sách đoạn tiếng theo thứ tự phát (mọi lượt). Lượt "list": mỗi câu một đoạn. Lượt "slide": mỗi câu CÓ GIẢI THÍCH một dãy đoạn (câu gốc, bản dịch [chỉ hiện chữ], câu dẫn dắt,
    từ vựng, ngữ pháp); câu không có từ vựng / ngữ pháp thì bỏ qua (trừ khi cả cảnh không có câu nào như vậy). Thêm các đoạn LỜI DẪN (``kieu`` = "loi") khi ``opts["loi_dan"]``.
    Mọi đoạn CÓ TIẾNG đều bằng tiếng đích (``ngon`` = "tgt"); chữ tiếng mẹ đẻ chỉ hiện (``am`` = False)."""
    ra: list[dict] = []
    doc_giai = bool(opts.get("doc_giai", True))
    loi = bool(opts.get("loi_dan", True))
    quiz = du_lieu.get("quiz") or []
    luot = [lt for lt in cac_luot(opts) if lt["pk"] != "hoi" or quiz]            # cảnh không có câu hỏi thì không có lượt câu hỏi (các lượt sau đánh số lại)
    lines = du_lieu["lines"]
    nd = _loi_dan(du_lieu, opts, [lt["pk"] for lt in luot]) if loi else None
    co_giai = any(co_giai_thich(ln) for ln in lines)

    def loi_seg(k: int, lt: dict, el: str, raw: str) -> None:
        ra.append(_doan(-1, k, lt, el, RB.bo_ruby(raw), 0, "tgt", True, "loi", raw))                 # người dẫn (speakers[0]) nói

    for k, lt in enumerate(luot):
        if nd:
            if k == 0:
                van = nd["mo"] if du_lieu.get("_dau") else nd["cau"]["doc"]
            else:
                van = nd["cau"][lt["pk"]]
            if van:
                loi_seg(k, lt, "n", van)
        if lt["kieu"] == "quiz":
            dap = (LOI.get(str(opts.get("ngon_dich") or "")) or {}).get("dap_an", "")         # "Đáp án là: " bằng tiếng đích (không có mẫu thì đọc thẳng đáp án)
            for n, qz in enumerate(quiz):
                ra.append(_doan(n, k, lt, "q", RB.bo_ruby(qz["q"]), 0, "tgt", True))
                ra.append(_doan(n, k, lt, "a", RB.bo_ruby(dap + qz["a"]), 0, "tgt", True))
            continue
        for i, ln in enumerate(lines):
            if lt["kieu"] == "slide" and co_giai and not co_giai_thich(ln):
                continue
            s = int(ln.get("s", 0))
            slide = lt["kieu"] == "slide"
            # trên slide bản dịch (và phiên âm) HIỆN CÙNG LÚC với câu gốc, không có đoạn riêng; câu ngắn mà dịch dài thì giữ thêm một chút cho kịp đọc
            giu = 0.5 if (slide and ln.get("translation")) else 0.0
            if slide or lt["ld"]:                                                       # lượt / slide có bản dịch: câu rất dài được giữ khung hình thêm (xem giu_cau_dai)
                giu = max(giu, giu_cau_dai(ln, str(opts.get("ngon_dich") or "")))
            ra.append(_doan(i, k, lt, "t", RB.bo_ruby(ln["text"]), s, "tgt", True, giu=giu))
            if not slide:
                continue
            if doc_giai and co_giai_thich(ln) and ln.get("lead"):
                ra.append(_doan(i, k, lt, "l", RB.bo_ruby(ln["lead"]), 0, "tgt", True, phu=ln["lead"]))               # câu dẫn dắt: chỉ nói → có phụ đề
            # từ vựng / ngữ pháp: nghĩa / giải thích tiếng mẹ đẻ hiện cùng lúc với từ / mẫu. Có lời giảng (say) thì lời giảng ĐÃ nói từ / mẫu đó nên KHÔNG đọc riêng từ trước (tránh đọc lặp);
            # không có thì đọc từ / mẫu một lần rồi giữ 1 s cho kịp đọc nghĩa
            for n, v in enumerate(ln.get("vocab") or []):
                say = v.get("say") if doc_giai else ""
                if say:
                    ra.append(_doan(i, k, lt, f"v{n}", RB.bo_ruby(say), 0, "tgt", True, phu=say))                    # lời giảng: chỉ nói → có phụ đề
                else:
                    ra.append(_doan(i, k, lt, f"v{n}", RB.bo_ruby(v["w"]), 0, "tgt", doc_giai, giu=1.0 if doc_giai else 0.0))
            for n, g in enumerate(ln.get("grammar") or []):
                say = g.get("say") if doc_giai else ""
                if say:
                    ra.append(_doan(i, k, lt, f"g{n}", RB.bo_ruby(say), 0, "tgt", True, phu=say))
                else:
                    ra.append(_doan(i, k, lt, f"g{n}", _mau_cau(g["p"]), 0, "tgt", doc_giai, giu=1.0 if doc_giai else 0.0))
    if nd and luot and du_lieu.get("_cuoi") and nd["ket"]:
        loi_seg(len(luot) - 1, luot[-1], "k", nd["ket"])
    return ra


def _van_doan_noi(s: dict, lines: list, quiz: list, dap: str) -> tuple[str, str]:
    """(chữ tiếng đích — còn cách đọc ``[kana]`` —, bản dịch tiếng mẹ đẻ hoặc "") của một đoạn CÓ TIẾNG. Bản dịch chỉ có ở câu của bài (``translation``) và câu hỏi / đáp án (``q_tr`` / ``a_tr``);
    lời chào, lời chuyển, lời kết, câu dẫn dắt, lời giảng là lời nói riêng của người dẫn nên không có bản dịch."""
    el, i = s["el"], s["i"]
    me = ""
    if s["kieu"] == "loi":
        t = s.get("hien") or s["van"]
    elif el == "t":
        t, me = lines[i]["text"], lines[i].get("translation") or ""
    elif el == "l":
        t = lines[i].get("lead") or s["van"]
    elif el == "q":
        t, me = quiz[i]["q"], quiz[i].get("q_tr") or ""
    elif el == "a":
        t, me = dap + quiz[i]["a"], quiz[i].get("a_tr") or ""
    elif el[:1] in ("v", "g") and el[1:].isdigit():
        ds = lines[i].get("vocab" if el[0] == "v" else "grammar") or []
        n = int(el[1:])
        t = s.get("phu") or (ds[n]["w"] if (el[0] == "v" and n < len(ds)) else s["van"])
    else:
        t = s["van"]
    return str(t or s["van"]).strip(), str(me).strip()


def kich_ban_noi(du_lieu: dict, opts: dict) -> list[str]:
    """TOÀN BỘ KỊCH BẢN NÓI của một cảnh theo thứ tự phát (còn cách đọc ``[kana]``): lời chào, câu của bài, lời chuyển lượt, câu hỏi + đáp án, câu dẫn dắt, lời giảng từ vựng / ngữ pháp, lời kết —
    như một bản script. Mỗi lượt đọc lại cùng các câu của bài (lượt dịch, shadowing…) nên chữ giống hệt đã có thì chỉ giữ LẦN ĐẦU. Chỉ gồm đoạn CÓ TIẾNG (``am``)."""
    ke = ke_hoach(du_lieu, opts)
    lines, quiz = du_lieu["lines"], du_lieu.get("quiz") or []
    dap = (LOI.get(str(opts.get("ngon_dich") or "")) or {}).get("dap_an", "")
    ra: list[str] = []
    thay: set = set()
    for s in ke:
        if not s["am"]:
            continue
        t, _ = _van_doan_noi(s, lines, quiz, dap)
        khoa = RB.bo_ruby(t)
        if t and khoa not in thay:
            thay.add(khoa)
            ra.append(t)
    return ra


#: phụ đề không ngắn hơn / dài hơn mức này (giây) — dòng chớp 0,1 s không đọc kịp; kéo dài quá thì dính sang câu sau
PHU_DE_TOI_THIEU = 0.6
PHU_DE_DEM_CUOI = 0.15


def cau_phu_de(du_lieu: dict, opts: dict) -> list[dict]:
    """PHỤ ĐỀ CC của một cảnh: mỗi đoạn CÓ TIẾNG (mọi lượt — nghe lại, shadowing… đều là lúc người dẫn nói thật) thành một dòng ``{bd, kt, dich, me}`` (giây TRONG cảnh) theo thứ tự phát.
    ``dich`` = chữ tiếng đích đã bỏ cách đọc ``[kana]`` (đúng lời nói); ``me`` = bản dịch tiếng mẹ đẻ nếu đoạn có (xem :func:`_van_doan_noi`), không thì "".
    Mốc: khoảng CÓ TIẾNG thật của đoạn (``_tieng`` — bỏ lặng đầu / cuối của tệp giọng, xem ``sangtao_dung.do_lang_dau_cuoi``), chưa có thì cả đoạn (``_moc``); không bao giờ chồng sang đoạn kế."""
    moc = du_lieu.get("_moc")
    if not isinstance(moc, list):
        return []
    tieng = du_lieu.get("_tieng")
    ke = ke_hoach(du_lieu, opts)
    if len(moc) != len(ke):
        return []
    tieng = tieng if (isinstance(tieng, list) and len(tieng) == len(ke)) else None
    nghi = du_lieu.get("_nghi")                                                       # chỗ giọng ngắt giữa lời nói (để người gọi tách câu dài đúng chỗ)
    nghi = nghi if (isinstance(nghi, list) and len(nghi) == len(ke)) else None
    lines, quiz = du_lieu["lines"], du_lieu.get("quiz") or []
    dap = (LOI.get(str(opts.get("ngon_dich") or "")) or {}).get("dap_an", "")
    ra: list[dict] = []
    for j, s in enumerate(ke):
        if not s["am"]:
            continue
        t, me = _van_doan_noi(s, lines, quiz, dap)
        t = RB.bo_ruby(t).strip()
        if not t:
            continue
        a, b = (tieng[j] if tieng else moc[j])
        b = max(b + PHU_DE_DEM_CUOI, a + PHU_DE_TOI_THIEU)
        if j + 1 < len(ke):
            b = min(b, moc[j + 1][0] - 0.02)                                          # không chồng sang đoạn kế
        if b - a < 0.3:
            continue
        cd = {"bd": round(float(a), 3), "kt": round(float(b), 3), "dich": t, "me": RB.bo_ruby(me).strip()}
        if nghi and nghi[j]:
            cd["nghi"] = [list(p) for p in nghi[j]]
        ra.append(cd)
    return ra


def khe_sau(ke: list[dict], dur: list[float], opts: dict) -> list[float]:
    """Lặng SAU từng đoạn (giây). Đoạn cuối cùng = đệm cuối cảnh; đoạn cuối của lượt (không phải lượt chót) = lặng giữa hai lượt.
    Lượt NHẠI THEO: sau mỗi CÂU GỐC chừa khoảng lặng = ``khe_nhac`` × thời gian đọc câu đó (+ 0,3 s) để người học nhại lại."""
    ra: list[float] = []
    n = len(ke)
    nhac = float(opts.get("khe_nhac") or 0.0)
    for j, s in enumerate(ke):
        them = nhac * dur[j] if (nhac > 0 and s["shadow"] and s["el"] == "t") else 0.0
        if j == n - 1:
            ra.append(max(CUOI_CANH, them))
            continue
        nxt = ke[j + 1]
        if nxt["luot"] != s["luot"]:
            ra.append(KHE_LUOT + them)
        elif s["kieu"] == "loi":
            ra.append(KHE_LOI)
        elif s["kieu"] == "quiz":
            ra.append(max(KHE_NGHI, 1.2 * dur[j] + 2.0) if s["el"] == "q" else KHE_SLIDE)      # sau câu hỏi: thời gian suy nghĩ; sau đáp án: sang câu kế
        elif s["kieu"] != "slide":
            ra.append(max(KHE_CAU, them + 0.3) if them else KHE_CAU)
        elif nxt["i"] != s["i"]:
            ra.append(KHE_SLIDE + them)
        else:
            ra.append(KHE_DOAN)
    return [g + float(sg.get("giu") or 0.0) for g, sg in zip(ra, ke)]


def thoi_gian(ke: list[dict], dur: list[float], opts: dict, dau: float = DAU_CANH) -> tuple[list[list[float]], list[float]]:
    """(mốc [bắt đầu, kết thúc] từng đoạn, lặng sau từng đoạn). ``dur``: thời lượng tiếng thật (hoặc ước lượng) của từng đoạn."""
    khe = khe_sau(ke, dur, opts)
    moc, t = [], float(dau)
    for d, g in zip(dur, khe):
        moc.append([round(t, 3), round(t + d, 3)])
        t += d + g
    return moc, khe


def uoc_do_dai(s: dict, ngon: str, ngon_me: str = "vi", cham: bool = False) -> float:
    """Thời lượng ước tính của một đoạn: đoạn có tiếng theo tốc độ đọc của ngôn ngữ; đoạn chỉ hiện chữ theo thời gian cần để ĐỌC trên màn hình."""
    n = len("".join(str(s["van"]).split()))
    if not s.get("am", True):
        return max(1.2, 0.9 + 0.07 * n)
    cps = CPS.get(ngon if s.get("ngon", "tgt") == "tgt" else ngon_me, 12.0) * (0.8 if cham else 1.0)
    return max(0.7, n / cps + 0.35)


def moc_uoc(ke: list[dict], ngon: str, ngon_me: str, opts: dict, cham: bool = False) -> list[list[float]]:
    """Mốc khi CHƯA có tiếng thật: ước thời gian đọc theo độ dài chữ."""
    return thoi_gian(ke, [uoc_do_dai(s, ngon, ngon_me, cham) for s in ke], opts)[0]


def tong_giay(moc: list[list[float]], khe_cuoi: float = CUOI_CANH) -> float:
    return (moc[-1][1] if moc else 0.0) + khe_cuoi


def bat_dau_luot(ke: list[dict], moc: list[list[float]]) -> list[float]:
    """Lúc BẮT ĐẦU mỗi lượt (giây): lượt đầu = 0; các lượt sau = ngay trước đoạn đầu của lượt đó (kịp đổi nhãn "Lần 2", xoá danh sách cũ)."""
    ra: list[float] = []
    for k in range(max((s["luot"] for s in ke), default=-1) + 1):
        idx = [j for j, s in enumerate(ke) if s["luot"] == k]
        if k == 0 or not idx:
            ra.append(0.0)
            continue
        truoc = [j for j, s in enumerate(ke) if s["luot"] == k - 1]
        ra.append(max(moc[truoc[-1]][1] + 0.3, moc[idx[0]][0] - 1.1))
    return ra
