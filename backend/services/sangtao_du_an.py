"""Dự án "Sáng tạo" (video đọc báo học ngoại ngữ): lưu trên đĩa (<data_root>/sangtao_du_an/<id>/) + trình quản lý việc dựng nền (một việc/dự án).

  project.json   tiêu đề, cài đặt, kịch bản, storyboard
  voice/         tiếng từng đoạn + tiếng ghép của từng cảnh (``baidoc_<khoá>.mp3`` + ``.moc.json``)
  scenes/        clip + ảnh đại diện từng cảnh đã dựng
  out.mp4        bản dựng gần nhất
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Optional

from services import do_dai_video as DDV
from services import sangtao_bai as B
from services import sangtao_dung as D
from services.sangtao_ruby import bo_ruby as RB_bo_ruby
from services import sangtao_schema as SC
from services.sangtao_canh import Huy
from utils import storage_paths

logger = logging.getLogger("Sangtao")
_ID = re.compile(r"^[0-9a-f]{12}$")
_KHOA = threading.Lock()
VIEC: dict[str, dict] = {}                       # id -> {state, done, total, message, error, huy, thread, ...}

#: cách xử lý nội dung bài: chon_loc = chọn ý / sự kiện chính rồi TỰ VIẾT câu mới (có thể kèm tối đa 2 câu trích ngắn trong ngoặc kép) · viet_lai (MẶC ĐỊNH) = viết lại ĐẦY ĐỦ mọi ý của bài bằng câu chữ mới ·
#: giu_nguyen = giữ nguyên câu chữ (CHỈ nên dùng cho nội dung của chính bạn / giấy phép mở)
NOI_DUNG = ("chon_loc", "viet_lai", "giu_nguyen")
MAC_DINH = {"language": "en", "aspect": "16:9", "audience": "general", "voice": None, "speed": 0.0,
            "intro": True, "intro_text": "", "outro": True, "outro_text": "",
            "source": {}, "content_mode": "viet_lai", "level": "", "native_language": "vi", "furigana": True, "phonetic": True,      # phonetic: hiện dòng phiên âm (romaji / pinyin) dưới câu — tắt thì bớt chữ, câu chính to hơn
            "passes": ["doc", "dich", "hoi", "giang", "shadow"], "read_explanation": True, "shadow_gap": 1.0, "camera": False,
            "narration": True, "layout": "page", "script_full": True,
            #  Phụ đề CC thêm thứ tiếng (cho người xem nói tiếng khác): chọn TRƯỚC khi dựng, MẶC ĐỊNH KHÔNG CHỌN thứ nào. Dựng xong video thì app tự thêm + dịch các thứ tiếng này (``bat_dau_dich_cc``).
            #  cc_engine: "ai" (bộ AI theo Cài đặt) | "deepseek" | "gemini" | "google" | "manual" (manual = chỉ thêm, người dùng tự dán
            #  prompt ChatGPT / Gemini ở hộp phụ đề CC).
            "cc_langs": [], "cc_engine": "ai",
            "phan_canh": []}                      # voice: None = tự chọn giọng đầu của ngôn ngữ, "" = không tiếng; speed: 0 = mặc định
#: khoá cài đặt của các bản cũ (kể chuyện bằng hình / thẻ từ / hội thoại tự viết): bỏ khi đọc dự án
_KHOA_CU = ("family", "camera_mode", "hook", "char_style", "nguoi_mode", "pace", "topics", "cast", "lesson", "input_mode", "duration_s", "read_translation", "source_url", "translation_mode")


def goc() -> str:
    p = os.path.join(storage_paths.get_data_root(), "sangtao_du_an")
    os.makedirs(p, exist_ok=True)
    return p


def thu_muc(pid: str) -> str:
    if not _ID.match(pid or ""):
        raise KeyError("Mã dự án không hợp lệ")
    return os.path.join(goc(), pid)


def _tep(pid: str) -> str:
    return os.path.join(thu_muc(pid), "project.json")


def _mac_dinh_nguoi_dung() -> dict:
    """Cài đặt người dùng đã "đặt làm mặc định cho dự án mới" (services/mac_dinh_du_an.py, chế độ ``sang_tao``); chưa đặt → {}. Lỗi đọc thì bỏ qua (tạo dự án không được hỏng vì phần này)."""
    try:
        from services import mac_dinh_du_an as MD
        return MD.cai_dat("sang_tao", MD.KHOA_SANG_TAO)
    except Exception as e:  # noqa: BLE001
        logger.warning("Sáng tạo: không đọc được mặc định dự án mới: %s", e)
        return {}


def tao(tieu_de: str = "") -> dict:
    pid = uuid.uuid4().hex[:12]
    os.makedirs(thu_muc(pid), exist_ok=True)
    now = time.time()
    du_an = {"id": pid, "title": (tieu_de or "Dự án mới").strip()[:120], "created": now, "updated": now,
             "settings": _cai_dat_day_du({**copy.deepcopy(MAC_DINH), **copy.deepcopy(_mac_dinh_nguoi_dung())}), "script": "", "storyboard": None}
    _ghi(du_an)
    return du_an


def luu_mac_dinh(pid: str) -> dict:
    """"Đặt cấu hình dự án này làm mặc định cho dự án mới": lưu các cài đặt dùng lại được của dự án ``pid`` (không lưu nguồn bài, chữ thẻ mở đầu / kết thúc). Chỉ áp cho dự án TẠO SAU."""
    from services import mac_dinh_du_an as MD
    d = doc(pid)
    st = d["settings"]
    MD.luu("sang_tao", {k: copy.deepcopy(st[k]) for k in MD.KHOA_SANG_TAO if k in st}, None, [], d.get("title", ""))
    return MD.trang_thai()


def xoa_tat_ca() -> dict:
    """Xoá MỌI dự án (trừ dự án đang dựng — phải hủy trước). Trả {"da_xoa": n, "bo_qua": [tiêu đề các dự án đang dựng]}."""
    da_xoa, bo_qua = 0, []
    for ten in os.listdir(goc()):
        if not _ID.match(ten):
            continue
        try:
            tieu_de = doc(ten).get("title", ten)
        except (KeyError, ValueError, OSError):
            tieu_de = ten                                                  # dự án hỏng vẫn xoá được
        try:
            xoa(ten)
            da_xoa += 1
        except RuntimeError:
            bo_qua.append(tieu_de)
    return {"da_xoa": da_xoa, "bo_qua": bo_qua}


def _ghi(du_an: dict) -> None:
    p = _tep(du_an["id"])
    tam = p + ".tmp"
    with open(tam, "w", encoding="utf-8") as f:
        json.dump(du_an, f, ensure_ascii=False, indent=1)
    os.replace(tam, p)


def _cc_langs_hop_le(x: Any) -> list:
    """Danh sách thứ tiếng phụ đề CC từ cài đặt: chỉ mã chữ hợp lệ, chuẩn hoá (zh-cn -> zh), bỏ trùng, giữ thứ tự. Rác (không phải danh sách, mã lạ) thì bỏ — ghi / đọc không bao giờ ném lỗi."""
    if not isinstance(x, (list, tuple)):
        return []
    from services import review_sub_tracks as ST

    ra: list = []
    for v in x:
        if not isinstance(v, str):
            continue
        m = ST.chuan_ma(v)
        if re.fullmatch(r"[a-z]{2,3}", m) and m not in ra:
            ra.append(m)
    return ra


def _chuan_cc(st: dict) -> None:
    """Làm sạch ngay TẠI CHỖ hai khoá phụ đề CC của một bộ cài đặt (cả lúc ĐỌC lẫn lúc GHI — phản hồi của ``luu`` phải giống bản đã lưu)."""
    st["cc_langs"] = _cc_langs_hop_le(st.get("cc_langs"))
    from services.ai_engines import la_ai

    if not (la_ai(st.get("cc_engine")) or st.get("cc_engine") in ("google", "manual")):     # bộ AI (deepseek / gemini) · google · manual
        st["cc_engine"] = MAC_DINH["cc_engine"]


def _cai_dat_day_du(st: Optional[dict]) -> dict:
    """Cài đặt đầy đủ khoá: bổ sung mặc định cho khoá còn thiếu, bỏ khoá không còn dùng."""
    st = dict(st or {})
    cu_dich = st.get("translation_mode")
    if st.get("source_url") and not st.get("source"):
        st["source"] = {"url": st["source_url"]}
    for k in _KHOA_CU:
        st.pop(k, None)
    ra = {**copy.deepcopy(MAC_DINH), **{k: v for k, v in st.items() if k in MAC_DINH}}
    if ra["content_mode"] not in NOI_DUNG:
        ra["content_mode"] = MAC_DINH["content_mode"]
    if ra["layout"] not in ("page", "scroll"):                                  # page = lượt dạng danh sách hiện theo trang sách · scroll = cuộn dần
        ra["layout"] = MAC_DINH["layout"]
    ra["narration"] = bool(ra["narration"])
    _chuan_cc(ra)
    if "passes" not in st and cu_dich:                                          # bản cũ: chế độ dịch → danh sách lượt
        ra["passes"] = B.tuy_chon({"translation_mode": cu_dich})["luot"]
    ra["passes"] = B.tuy_chon({"passes": ra["passes"]})["luot"]
    return ra


def doc(pid: str) -> dict:
    try:
        with open(_tep(pid), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        raise KeyError("Không tìm thấy dự án")
    d["settings"] = _cai_dat_day_du(d.get("settings"))
    return d


def luu(pid: str, thay_doi: dict) -> tuple[dict, list[str]]:
    """Cập nhật title/settings/script/storyboard. Storyboard sai → giữ bản cũ, trả danh sách lỗi."""
    with _KHOA:
        du_an = doc(pid)
        loi: list[str] = []
        if "title" in thay_doi:
            du_an["title"] = str(thay_doi["title"]).strip()[:120] or du_an["title"]
        if "script" in thay_doi:
            du_an["script"] = str(thay_doi["script"])[:200000]
        if isinstance(thay_doi.get("settings"), dict):
            for k in MAC_DINH:
                if k in thay_doi["settings"]:
                    du_an["settings"][k] = thay_doi["settings"][k]
            _chuan_cc(du_an["settings"])
        if "storyboard" in thay_doi:
            if thay_doi["storyboard"] is None:
                du_an["storyboard"] = None
            else:
                sb, loi = SC.kiem_tra(thay_doi["storyboard"])
                if sb is not None:
                    du_an["storyboard"] = sb.model_dump()
        if str(du_an.get("title") or "").strip() in ("", TEN_MAC_DINH) and "title" not in thay_doi:
            #  Dự án còn tên mặc định ("Dự án mới" ×N trong danh sách, mô tả video mở đầu bằng "Dự án mới"): đặt theo bài. Người dùng đã đổi tên thì không đụng tới.
            ten = tieu_de_video(du_an)
            if ten:
                du_an["title"] = ten[:120]
        du_an["updated"] = time.time()
        _ghi(du_an)
        return du_an, loi


def lan_giong(pid: str) -> dict:
    """{id_cảnh: số lần "làm lại" cảnh} (``project.json["giong_lan"]``). Thiếu / hỏng → {}. Cảnh chưa làm lại lần nào không có mặt (= 0)."""
    try:
        return {int(k): max(0, int(x)) for k, x in (doc(pid).get("giong_lan") or {}).items()}
    except (KeyError, ValueError, TypeError, AttributeError, OSError):
        return {}


def _tang_lan_giong(pid: str, ids: list) -> None:
    """Tăng số lần làm lại của các cảnh ``ids`` (+1). KHÔNG tự lấy khoá: gọi bên trong ``with _KHOA``. Lần sinh đổi thì tên tệp giọng + khoá cảnh đổi theo, nên giọng được sinh MỚI (Qwen: seed khác)
    và hình dựng lại; giọng cũ vẫn nằm trong ``voice/`` (không xoá). ``luu()`` chép nguyên khoá này khi người dùng sửa dự án nên không bị mất."""
    du_an = doc(pid)
    lan = {str(k): x for k, x in lan_giong(pid).items()}
    for i in ids:
        lan[str(int(i))] = lan.get(str(int(i)), 0) + 1
    du_an["giong_lan"] = lan
    _ghi(du_an)


#: Số video chưa từng đo được phép đo trong MỘT lượt liệt kê (mỗi lần đo là một ``ffmpeg -i``). Dự án dựng mới đã được đo sẵn lúc dựng xong;
#: các dự án cũ được đo dần qua vài lượt mở danh sách thay vì treo một lượt.
_NGAN_SACH_DO_DAI = 6


def liet_ke() -> list[dict]:
    kq = []
    ngan_sach = [_NGAN_SACH_DO_DAI]
    for ten in os.listdir(goc()):
        try:
            d = doc(ten)
        except (KeyError, ValueError, OSError):
            continue
        out = os.path.join(thu_muc(ten), "out.mp4")
        co_video = os.path.exists(out)
        kq.append({"id": d["id"], "title": d["title"], "updated": d["updated"], "so_canh": len((d.get("storyboard") or {}).get("scenes") or []),
                   "co_video": co_video,
                   #  Độ dài video đã dựng (giây); 0 = chưa có video hoặc chưa đo được (Trang chủ hiện độ dài ở các chế độ).
                   "thoi_luong": round(DDV.giay_cua(out, ngan_sach) or 0, 1) if co_video else 0})
    return sorted(kq, key=lambda x: -x["updated"])


def xoa(pid: str) -> None:
    v = VIEC.get(pid)
    if v and v["state"] == "running":
        raise RuntimeError("Dự án đang dựng — hãy hủy trước khi xoá")
    shutil.rmtree(thu_muc(pid), ignore_errors=True)
    VIEC.pop(pid, None)


#: câu ghi nguồn hiện trong video theo NGÔN NGỮ ĐÍCH ({site} = tên báo; mọi chữ trong video là tiếng đích, chỉ bản dịch / nghĩa / giải thích là tiếng mẹ đẻ). Chỉ để người xem biết đây là TIN TỨC mới của báo nào
#: (kèm ngày đăng); mục đích giáo dục nói ở lời dẫn + mô tả. Mô tả video dùng cả bảng này theo ngôn ngữ mẹ đẻ.
NHAN_NGUON = {"vi": "Nguồn tin: {site}",
              "en": "News source: {site}",
              "ja": "ニュース出典：{site}",
              "zh-cn": "新闻来源：{site}",
              "ko": "뉴스 출처: {site}",
              "fr": "Source de l'info : {site}",
              "de": "Nachrichtenquelle: {site}",
              "ru": "Источник новости: {site}",
              "es": "Fuente de la noticia: {site}",
              "pt": "Fonte da notícia: {site}",
              "it": "Fonte della notizia: {site}"}


#: "đăng ngày" theo ngôn ngữ đích (dòng nguồn hiện trong video)
NHAN_NGAY = {"vi": "đăng ngày", "en": "published", "ja": "掲載日", "zh-cn": "发布日期", "ko": "게재일",
             "fr": "publié le", "de": "veröffentlicht am", "ru": "опубликовано", "es": "publicado el", "pt": "publicado em", "it": "pubblicato il"}


def _ten_nguon(src: Optional[dict]) -> str:
    """Tên nguồn để ghi: tên báo người dùng đặt / lấy từ trang; không có thì tên miền của link."""
    from urllib.parse import urlparse
    src = src or {}
    ten = str(src.get("site") or "").strip()
    if ten:
        return ten[:80]
    host = (urlparse(str(src.get("url") or "").strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def nguon_bai(src: Optional[dict], ngon: str = "vi", day_du: bool = False) -> str:
    """Dòng ghi nguồn bằng ngôn ngữ ``ngon``: "Nguồn tin: <tên báo>". ``day_du`` thêm link gốc. Không có nguồn → rỗng."""
    ten = _ten_nguon(src)
    if not ten:
        return ""
    s = NHAN_NGUON.get(ngon, NHAN_NGUON["en"]).format(site=ten)
    url = str((src or {}).get("url") or "").strip()
    return f"{s} — {url[:100]}" if (day_du and url) else s


def nguon_hien(src: Optional[dict], ngon: str = "vi") -> str:
    """Nguồn hiện TRONG video (chân trang mọi cảnh, thẻ mở đầu, thẻ kết thúc): MỘT dòng "Nguồn tin: <tên báo> · đăng ngày <ngày>" bằng ngôn ngữ ``ngon`` — video gọi với NGÔN NGỮ ĐÍCH
    (không có ngày thì bỏ phần ngày). KHÔNG ghi link vào video — link bài gốc nằm trong phần mô tả video (:func:`mo_ta_video`). Không có nguồn → rỗng."""
    ten = _ten_nguon(src)
    if not ten:
        return ""
    ngay = str((src or {}).get("date") or "").strip()[:40]
    return NHAN_NGUON.get(ngon, NHAN_NGUON["en"]).format(site=ten) + (f" · {NHAN_NGAY.get(ngon, NHAN_NGAY['en'])} {ngay}" if ngay else "")


#: ---- MÔ TẢ VIDEO (xem :func:`mo_ta_video`): chỉ còn khối bản quyền tiếng Anh + hashtag; không còn khối tiếng Việt / tiếng đích.
TEN_MAC_DINH = "Dự án mới"
#: tên một ngôn ngữ (hàng) khi viết bằng một ngôn ngữ khác (cột) — chỉ để câu mở đầu nêu được "luyện nghe <ngôn ngữ>"
_TEN_NGON = {
    "vi": {"vi": "tiếng Việt", "ja": "tiếng Nhật", "en": "tiếng Anh", "zh-cn": "tiếng Trung", "ko": "tiếng Hàn", "fr": "tiếng Pháp", "de": "tiếng Đức", "ru": "tiếng Nga", "es": "tiếng Tây Ban Nha", "pt": "tiếng Bồ Đào Nha", "it": "tiếng Ý"},
    "ja": {"vi": "ベトナム語", "ja": "日本語", "en": "英語", "zh-cn": "中国語", "ko": "韓国語", "fr": "フランス語", "de": "ドイツ語", "ru": "ロシア語", "es": "スペイン語", "pt": "ポルトガル語", "it": "イタリア語"},
    "en": {"vi": "Vietnamese", "ja": "Japanese", "en": "English", "zh-cn": "Chinese", "ko": "Korean", "fr": "French", "de": "German", "ru": "Russian", "es": "Spanish", "pt": "Portuguese", "it": "Italian"},
    "zh-cn": {"vi": "越南语", "ja": "日语", "en": "英语", "zh-cn": "中文", "ko": "韩语", "fr": "法语", "de": "德语", "ru": "俄语", "es": "西班牙语", "pt": "葡萄牙语", "it": "意大利语"},
    "ko": {"vi": "베트남어", "ja": "일본어", "en": "영어", "zh-cn": "중국어", "ko": "한국어", "fr": "프랑스어", "de": "독일어", "ru": "러시아어", "es": "스페인어", "pt": "포르투갈어", "it": "이탈리아어"},
}


#: THÔNG BÁO NGUỒN GỐC NỘI DUNG / BẢN QUYỀN — CHỈ bằng TIẾNG ANH (người dùng chọn: không ghi bằng tiếng mẹ đẻ của người xem hay ngôn ngữ đích), một khối riêng ở cuối mô tả.
#: Nói đúng PHƯƠNG PHÁP đã làm (chỉ dùng sự kiện thời sự, diễn đạt lại bằng câu chữ riêng, có hay không có câu trích) + MỘT căn cứ chính xác (Công ước Berne, Điều 2 khoản 8: tin tức trong ngày / sự kiện
#: thuần tuý không thuộc đối tượng bảo hộ) + ghi nhận bản quyền bài gốc thuộc nguồn. KHÔNG kết luận "hoàn toàn hợp pháp" / "chắc chắn được kiếm tiền": đó là ý kiến pháp lý (fair use xét theo từng
#: trường hợp), không phải việc của một mẫu mô tả. Giữ nguyên câu chữ (giu_nguyen) thì KHÔNG được nói là đã diễn đạt lại.
LUAT_EN = {
    "nhan": "Copyright notice",
    "viet": "This video only uses current-affairs facts (news of the day, which falls outside copyright protection under the Berne Convention, Art. 2(8)); the narration is re-expressed in original wording. {trich}",
    "khong": "It does not copy or quote the original article verbatim.",
    "co": "Only {n} short quotation(s) are used, in quotation marks and with attribution.",
    "giu": "The reading text keeps the wording of the source material.",
    "bq": "Copyright of the original article belongs to {nguon}; this video does not replace it.",
    "link": "Original article: {url}",
}


def _khoi_luat(st: dict, src: dict, n_trich: int) -> list[str]:
    """Khối pháp lý bằng TIẾNG ANH (xem :data:`LUAT_EN`)."""
    L = LUAT_EN
    ten = _ten_nguon(src)
    dong = [f"▼ {L['nhan']}"]
    if st.get("content_mode") in ("viet_lai", "chon_loc"):
        dong.append(L["viet"].format(trich=(L["co"].format(n=n_trich) if n_trich else L["khong"])))
    else:
        dong.append(L["giu"])
    dong.append(L["bq"].format(nguon=ten or "the original publisher"))
    url = str(src.get("url") or "").strip()
    if url:                                                                         # link bài gốc: nơi DUY NHẤT trong mô tả (video không ghi link) — một dòng, link ở cuối, không dấu câu theo sau
        dong.append(L["link"].format(url=url))
    return dong


# ---- HASHTAG + TỪ KHOÁ cho mô tả (SEO). YouTube bỏ qua TOÀN BỘ hashtag nếu video có hơn 15 hashtag → tối đa 15; ba hashtag đầu hiện phía trên tiêu đề nên xếp đúng thứ tự ưu tiên.
#: KHÔNG dùng tên báo làm hashtag (tránh gợi ý hợp tác / liên kết với nguồn — nguồn chỉ được NÊU TÊN trong phần nguồn tin).
_MAX_HASHTAG = 15
_TAG = {   # ngôn ngữ đích → (hashtag viết bằng chính ngôn ngữ đó, hashtag tiếng Anh, hashtag tiếng Việt, từ khoá bằng chính ngôn ngữ đó, tên thang trình độ)
    "ja": (["#日本語", "#日本語学習", "#日本語リスニング", "#ニュースで学ぶ"], ["#LearnJapanese", "#JapaneseListening"], ["#HocTiengNhat", "#LuyenNgheTiengNhat"], ["日本語 リスニング", "ニュースで学ぶ日本語", "シャドーイング", "日本語学習"], "JLPT"),
    "zh-cn": (["#学中文", "#中文听力", "#新闻中文"], ["#LearnChinese", "#ChineseListening"], ["#HocTiengTrung", "#LuyenNgheTiengTrung"], ["中文听力", "新闻中文", "跟读练习", "学中文"], "HSK"),
    "ko": (["#한국어공부", "#한국어듣기", "#뉴스한국어"], ["#LearnKorean", "#KoreanListening"], ["#HocTiengHan", "#LuyenNgheTiengHan"], ["한국어 듣기", "뉴스로 배우는 한국어", "쉐도잉", "한국어 공부"], "TOPIK"),
    "en": ([], ["#LearnEnglish", "#EnglishListening", "#NewsEnglish"], ["#HocTiengAnh", "#LuyenNgheTiengAnh"], ["English listening", "news English", "shadowing"], "CEFR"),
}
_TEN_VI_ASCII = {"fr": "TiengPhap", "de": "TiengDuc", "ru": "TiengNga", "es": "TiengTayBanNha", "pt": "TiengBoDaoNha", "it": "TiengY", "vi": "TiengViet"}


def hashtag_tu_khoa(du_an: dict) -> tuple[list[str], list[str]]:
    """(hashtag, từ khoá) cho mô tả theo ngôn ngữ đích + ngôn ngữ mẹ đẻ + trình độ đã chọn. Hashtag tiếng Việt chỉ khi người xem dùng tiếng Việt."""
    st = du_an.get("settings") or {}
    dich = str((du_an.get("storyboard") or {}).get("language") or st.get("language") or "en")
    me = str(st.get("native_language") or "vi")
    ten_en = _TEN_NGON["en"].get(dich, dich)
    nat, en, vi, kw_nat, thang = _TAG.get(dich) or ([], [f"#Learn{ten_en.replace(' ', '')}", f"#{ten_en.replace(' ', '')}Listening"], [f"#Hoc{_TEN_VI_ASCII.get(dich, ten_en.replace(' ', ''))}", f"#LuyenNghe{_TEN_VI_ASCII.get(dich, ten_en.replace(' ', ''))}"], [], "")
    muc = "".join(ch for ch in str(st.get("level") or "") if ch.isalnum())[:12]
    muc_tag = [f"#{thang}", f"#{muc if muc.upper().startswith(thang) else thang + muc}"] if (muc and thang) else []
    chung = ["#Shadowing", "#ListeningPractice", "#LearnThroughNews"] + (["#LuyenNghe", "#HocNgoaiNgu"] if me == "vi" else [])
    ai = [str(h) for h in ((((du_an.get("storyboard") or {}).get("youtube")) or {}).get("hashtag") or [])]      # hashtag do AI soạn (xem sangtao_youtube) đứng ĐẦU: ba cái đầu hiện phía trên tiêu đề
    xep = ai + (vi if me == "vi" else []) + nat + en + muc_tag + chung
    tags, thay = [], set()
    for h in xep:                                                                                  # bỏ trùng không phân biệt hoa thường (#LearnJapanese = #learnjapanese)
        if h.lower() not in thay:
            thay.add(h.lower())
            tags.append(h)
    tags = tags[:_MAX_HASHTAG]
    ten_vi = _TEN_NGON["vi"].get(dich, dich)
    kw = []
    if me == "vi":
        kw += [f"luyện nghe {ten_vi}", f"học {ten_vi} qua tin tức", f"shadowing {ten_vi}", f"nghe tin tức {ten_vi}"]
    kw += [f"{ten_en} listening practice", f"learn {ten_en} with the news", f"{ten_en} shadowing"]
    kw += kw_nat
    if muc and thang:
        kw.append(f"{thang} {muc[len(thang):] if muc.upper().startswith(thang) else muc}".strip())
    return tags, list(dict.fromkeys(kw))


def tieu_de_video(du_an: dict) -> str:
    """Tên cho video: tiêu đề tiếng đích của bài (bỏ cách đọc trong ngoặc) → tên dự án (nếu đã đặt) → tiêu đề bài gốc. Rỗng nếu không có gì có nghĩa."""
    sb = du_an.get("storyboard") or {}
    t = RB_bo_ruby(str(sb.get("title") or "")).strip()
    ten = str(du_an.get("title") or "").strip()
    src_t = RB_bo_ruby(str(((du_an.get("settings") or {}).get("source") or {}).get("title") or "")).strip()
    return t or (ten if ten and ten != TEN_MAC_DINH else "") or src_t


def mo_ta_video(du_an: dict, kem_tieu_de: bool = True) -> str:
    """Mô tả video gợi ý (để dán vào phần mô tả YouTube): tiêu đề (tuỳ chọn) + khối BẢN QUYỀN / NGUỒN GỐC chỉ bằng TIẾNG ANH (kèm link bài gốc) + hashtag / từ khoá.
    KHÔNG còn khối tiếng Việt / tiếng đích (lộ trình, nguồn, mục đích giáo dục lặp hai thứ tiếng — người dùng 06-10-2026: "không cần thiết tí nào"); nguồn tin và mục đích giáo dục đã nằm trong lời dẫn
    và chân trang của video. KHÔNG ghi chuyện giọng tổng hợp hay AI vào mô tả (người dùng không muốn — bất lợi cho video)."""
    st = du_an.get("settings") or {}
    src = st.get("source") or {}
    n_trich = sum(1 for c_ in ((du_an.get("storyboard") or {}).get("scenes") or []) for ln in (((c_.get("visual") or {}).get("data") or {}).get("lines") or []) if ln.get("quote"))   # số câu trích nguyên văn (AI đánh dấu quote)
    ra: list[str] = []
    tieu_de = tieu_de_video(du_an) if kem_tieu_de else ""                                        # tab YouTube đã có ô tiêu đề riêng nên không lặp tiêu đề ở đầu mô tả
    if tieu_de:
        ra += [tieu_de, ""]
    ra += _khoi_luat(st, src, n_trich)
    tags, kw = hashtag_tu_khoa(du_an)
    ra += ["", "▼ Hashtags", " ".join(tags), "Keywords: " + ", ".join(kw)]
    return "\n".join(ra).strip()


def _tep_phu_de(pid: str) -> str:
    return os.path.join(thu_muc(pid), "phu_de.json")


def _ghi_phu_de(pid: str, cac: list, ngon_dich: str, ngon_me: str) -> None:
    """Ghi phụ đề CC của bản dựng ``out.mp4`` (``phu_de.json``: các dòng {bd, kt, dich, me} + hai ngôn ngữ). Lỗi ghi không làm hỏng video đã dựng xong."""
    p = _tep_phu_de(pid)
    try:
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"version": 1, "ngon_dich": ngon_dich, "ngon_me": ngon_me, "cac": cac}, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    except OSError as e:
        logger.warning("Sáng tạo: không ghi được phụ đề CC của %s: %s", pid, e)


def _doc_phu_de(pid: str) -> Optional[dict]:
    try:
        with open(_tep_phu_de(pid), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _uid_cc(van: str) -> str:
    """Khoá BỀN của một câu phụ đề CC = băm chữ tiếng đích: cùng một câu nói ở nhiều lượt dùng chung một bản dịch, và dựng lại video mà câu không đổi thì bản dịch cũ vẫn khớp."""
    return "c" + hashlib.md5(" ".join(str(van).split()).encode("utf-8")).hexdigest()[:12]


def cau_cc(pid: str) -> tuple[list, dict]:
    """(``b_chunks``, ``a_lookup``) của phụ đề CC cho cơ chế "phụ đề thêm thứ tiếng" (``review_sub_tracks``): mỗi CÂU tiếng đích khác nhau của bản dựng là một câu (``uid`` = :func:`_uid_cc`,
    theo thứ tự phát lần đầu); câu tham khảo = bản dịch tiếng mẹ đẻ nếu có (AI dùng làm gợi ý khi dịch sang thứ tiếng khác). Chưa dựng video → ([], {})."""
    d = _doc_phu_de(pid)
    if not d:
        return [], {}
    me: dict = {}
    thu_tu: list = []
    for c in d.get("cac") or []:
        t = str(c.get("dich") or "").strip()
        if not t:
            continue
        u = _uid_cc(t)
        if u not in me:
            me[u] = ""
            thu_tu.append((u, t))
        if not me[u] and str(c.get("me") or "").strip():
            me[u] = str(c["me"]).strip()
    b, a = [], {}
    for i, (u, t) in enumerate(thu_tu, 1):
        b.append({"uid": u, "b_id": i, "b_text": t, "source_a_ids": [i] if me[u] else []})
        if me[u]:
            a[str(i)] = {"text": me[u]}
    return b, a


def nap_phu_de(pid: str) -> dict:
    """Dạng chung ``{dir, main_lang, b_chunks, a_lookup}`` của dự án Sáng tạo cho ``sub_track_routes.tao_router`` — thứ tiếng CHÍNH = ngôn ngữ đích (mọi câu nói), nên thêm được en / zh / ko… (các thứ tiếng người xem)."""
    du_an = doc(pid)                                                                  # KeyError nếu không có dự án
    d = _doc_phu_de(pid) or {}
    dich = str(d.get("ngon_dich") or (du_an.get("storyboard") or {}).get("language") or (du_an.get("settings") or {}).get("language") or "en")
    b, a = cau_cc(pid)
    return {"id": pid, "dir": thu_muc(pid), "main_lang": dich, "b_chunks": b, "a_lookup": a}


def gieo_ban_dich_me(pid: str, lang: str) -> int:
    """Thêm thứ tiếng = TIẾNG MẸ ĐẺ của dự án thì ĐIỀN SẴN các câu mà AI đã dịch lúc viết kịch bản (bản dịch của câu trong bài, câu hỏi / đáp án, tiêu đề) — khỏi dịch lại những câu đó; chỉ còn các câu
    chỉ-nói (lời chào, lời chuyển, lời giảng, lời kết) là phải dịch. Chỉ điền câu CHƯA có bản dịch (không đè bản đã sửa tay). Thứ tiếng khác thì không làm gì. Trả số câu đã điền."""
    from services import review_sub_tracks as ST

    d = _doc_phu_de(pid)
    if not d or ST.chuan_ma(lang) != ST.chuan_ma(d.get("ngon_me") or ""):
        return 0
    b, a = cau_cc(pid)
    da_co = ST.ban_dich(ST.doc(thu_muc(pid)), lang)
    moi = {x["uid"]: a[str(x["source_a_ids"][0])]["text"] for x in b if x["source_a_ids"] and x["uid"] not in da_co}
    return ST.ghi_cau(thu_muc(pid), lang, moi, b) if moi else 0


def bat_dau_dich_cc(pid: str) -> bool:
    """Sau khi dựng xong video: nếu người dùng đã chọn thứ tiếng phụ đề CC (``settings.cc_langs``) thì THÊM + DỊCH chúng trong một luồng nền riêng (không giữ trạng thái "đang dựng"). True = đã bắt đầu luồng.

    Tiến độ hiện ở hộp phụ đề CC (``SubTracksBox``) vì dùng đúng cơ chế job của ``review_sub_tracks``. Không bao giờ ném lỗi — dựng xong rồi, phần dịch hỏng thì chỉ ghi vào job."""
    try:
        if not (doc(pid).get("settings") or {}).get("cc_langs"):
            return False
        threading.Thread(target=_dich_phu_de_cc, args=(pid,), name=f"sangtao-cc-{pid}", daemon=True).start()
        return True
    except (KeyError, ValueError, OSError):
        return False                                                                 # dự án không còn / tệp hỏng: không có gì để dịch
    except Exception:  # noqa: BLE001 — gọi từ luồng dựng, vừa dựng xong: tuyệt đối không được biến "xong" thành "lỗi"
        logger.exception("Sáng tạo: không bắt đầu được tự dịch phụ đề CC")
        return False


def _dich_phu_de_cc(pid: str) -> None:
    from services import review_advanced_ai as AI
    from services import review_sub_tracks as ST
    from services.config_service import get_config_service

    try:
        st = doc(pid).get("settings") or {}
        p = nap_phu_de(pid)
        if not p["b_chunks"]:
            return                                                                   # chưa có câu nào để dịch (video dựng không ra phụ đề CC)
        main = ST.chuan_ma(p["main_lang"])
        langs = [lg for lg in _cc_langs_hop_le(st.get("cc_langs")) if ST.ma_hop_le(lg, main)]
        if not langs:
            return
        from services.ai_engines import la_ai, provider_of, ten_bo_ai

        engine = str(st.get("cc_engine") or "ai")
        model = ""
        if la_ai(engine):
            import services.deepseek_service as deepseek_service

            bo_ai = provider_of(engine) or get_config_service().ai_provider_mac_dinh()         # "ai" = theo Cài đặt
            if deepseek_service.is_configured(bo_ai):
                model = get_config_service().ai_model_mac_dinh_cua(bo_ai)
            else:
                logger.warning("Sáng tạo: chưa có API key %s -> dịch phụ đề CC của %s bằng Google.", ten_bo_ai(bo_ai), pid)
                engine = "google"                                                    # chưa có API key -> Google (như lô)
        ctx = AI.read_film_context(p["dir"]) or {}
        batch_size = int(get_config_service().get_ai_script_options().get("batch_size") or 80)
        for lg in langs:
            ST.them_thu_tieng(p["dir"], lg)
            gieo_ban_dich_me(pid, lg)                                                # tiếng mẹ đẻ: điền sẵn các câu AI đã dịch lúc viết kịch bản
            if engine == "manual":
                continue                                                             # chỉ thêm; người dùng tự dán prompt ở hộp phụ đề CC
            viec = ST.can_dich(ST.doc(p["dir"]), lg, p["b_chunks"], "missing")
            if not viec:
                continue
            if not ST.bat_dau_job(pid, lg, len(viec)):
                logger.warning("Sáng tạo: dự án %s đang có lượt dịch phụ đề khác — bỏ qua tự dịch '%s'.", pid, lg)
                continue
            try:
                kq = ST.dich(pid, p["dir"], {"target_language": p["main_lang"]}, lg, p["b_chunks"], p["a_lookup"], engine=engine, pham_vi="missing", model=model,
                             batch_size=batch_size, brief=ctx.get("text") or "")
                loi = f"Còn {kq['thieu']} câu chưa dịch được — bấm Dịch lại để thử tiếp." if kq.get("thieu") and not kq.get("huy") else ""
                ST._cap_nhat_job(pid, running=False, error=loi, result=kq)
                if kq.get("huy"):
                    break                                                            # người dùng bấm Hủy: dừng, không sang thứ tiếng kế (tốn tiền)
            except Exception as e:  # noqa: BLE001
                ST._cap_nhat_job(pid, running=False, error=(getattr(e, "user_message", None) or str(e))[:500])
                logger.error("Sáng tạo: dịch phụ đề CC '%s' của %s lỗi: %s", lg, pid, e)
    except Exception:  # noqa: BLE001 — luồng nền: không được chết lặng thành ngoại lệ không ai thấy
        logger.exception("Sáng tạo: tự dịch phụ đề CC lỗi")


def phu_de_srt(pid: str) -> dict:
    """``{mã_thứ_tiếng: nội dung .srt}`` của bản dựng ``out.mp4`` — để tải lên YouTube (``sub_track_routes.srt_cho_du_an``) hoặc chép. Chỉ làm đúng thứ trang TOÀN VĂN cuối video có: **SCRIPT** (mọi câu người dẫn nói):
    · TIẾNG ĐÍCH — script nguyên bản, đủ mọi câu;
    · mỗi thứ tiếng NGƯỜI DÙNG THÊM (en, zh, ko, kể cả tiếng mẹ đẻ… — cơ chế "phụ đề thêm thứ tiếng": dịch bằng DeepSeek / Google / dán prompt) là BẢN DỊCH CỦA SCRIPT, CHỈ khi đã dịch ĐỦ mọi câu
      (phụ đề khuyết trên YouTube là sai trước mặt người xem).
    Mốc thời gian là lúc người dẫn nói từng câu trong video. Chưa có (video dựng trước khi có tính năng này / chưa dựng) → {}."""
    d = _doc_phu_de(pid)
    if not d:
        return {}
    from services import review_sub_tracks as ST

    cac = [c for c in (d.get("cac") or []) if isinstance(c, dict)]
    dich = ST.chuan_ma(d.get("ngon_dich") or "")
    try:
        cac_tieng = ST.doc(thu_muc(pid))
    except (OSError, ValueError, KeyError):
        cac_tieng = {}
    uids = {_uid_cc(c.get("dich") or "") for c in cac if str(c.get("dich") or "").strip()}

    def _srt(van_cua) -> str:
        chu, n = ST.srt_chu([{"start_time": c.get("bd", 0), "end_time": c.get("kt", 0), "target_text": van_cua(c)} for c in cac])
        return chu if n else ""

    ra: dict = {}
    if dich:
        s = _srt(lambda c: c.get("dich") or "")
        if s:
            ra[dich] = s
    for lg in sorted((cac_tieng.get("tracks") or {})):
        if lg in ra or not lg:
            continue
        ban = ST.ban_dich(cac_tieng, lg)
        if not uids or not uids <= set(ban):                                          # chưa dịch đủ → không tải (khuyết là sai trước mặt người xem)
            continue
        s = _srt(lambda c: ban.get(_uid_cc(c.get("dich") or ""), ""))
        if s:
            ra[lg] = s
    return ra


def phu_de_tom_tat(pid: str) -> list[dict]:
    """[{lang, ten, so_cau}] các phụ đề CC có của bản dựng (rỗng nếu chưa có)."""
    from services import review_sub_tracks as ST

    return [{"lang": lg, "ten": ST.nhan_thu_tieng(lg), "so_cau": len([x for x in chu.split("\r\n\r\n") if x.strip()])} for lg, chu in phu_de_srt(pid).items()]


def bai_dang_youtube(du_an: dict) -> Optional[dict]:
    """BÀI ĐĂNG YOUTUBE điền sẵn cho tab YouTube (cùng hình dạng ``da_soan`` của dự án Chi tiết): tiêu đề (phương án AI soạn ở bước 2, cuối cùng là tiêu đề tiếng đích của bài) ·
    MÔ TẢ dựng sẵn (:func:`mo_ta_video` — bản quyền + link gốc + hashtag; không lặp tiêu đề) · thẻ (AI soạn + từ khoá của app, cắt theo cách YouTube đếm) · chữ ảnh bìa.
    ``hashtag`` để TRỐNG vì mô tả đã có khối hashtag (tab YouTube sẽ nối dòng hashtag vào cuối mô tả nên điền lại là bị lặp). Dự án chưa có cảnh → ``None``."""
    sb = du_an.get("storyboard") or {}
    if not (sb.get("scenes") or []):
        return None
    from services.youtube_service import cat_the
    yt = sb.get("youtube") or {}
    tieu_de = [str(t) for t in (yt.get("tieu_de") or [])]
    ten = tieu_de_video(du_an)[:100]
    if ten and ten not in tieu_de:
        tieu_de.append(ten)                                                          # tiêu đề tiếng đích của bài làm phương án cuối (đủ để không bao giờ trống tiêu đề)
    _, kw = hashtag_tu_khoa(du_an)
    the, thay = [], set()
    for t in [str(x) for x in (yt.get("the") or [])] + kw:
        if t and t.lower() not in thay:
            thay.add(t.lower())
            the.append(t)
    return {"tieu_de": tieu_de, "mo_ta": mo_ta_video(du_an, kem_tieu_de=False), "the": cat_the(the, 450), "hashtag": [],
            "hook_anh": [str(h) for h in (yt.get("hook_anh") or [])], "thumbnail_path": "", "thumbnail_goc": ""}


# ------------------------------------------------------------------ giọng đọc
def giong_mac_dinh(language: str) -> str:
    from routes.tts_routes import EDGE_VOICES_BY_LANG
    ds = EDGE_VOICES_BY_LANG.get(language) or EDGE_VOICES_BY_LANG.get("vi") or []
    return ds[0]["id"] if ds else ""


_ID_EDGE = re.compile(r"^([a-z]{2,3})-[A-Za-z]{2,4}-")


def giong_hop_ngon_ngu(giong: str, language: str) -> bool:
    """Giọng ``giong`` có ĐỌC ĐƯỢC ngôn ngữ ``language`` không. Giọng Edge mang mã ngôn ngữ ở đầu ("ja-JP-NanamiNeural" → ja; "zh-cn" khớp "zh-CN-…"); giọng VieNeu chỉ đọc tiếng Việt;
    mã khác (giọng nhân bản, Qwen…) không biết thì coi là hợp lệ. Giọng KHÁC ngôn ngữ mà vẫn dùng thì tiếng Nhật bị đọc bằng giọng Việt / Anh → nghe "như tiếng gì đó khác"."""
    if not giong:
        return True
    m = _ID_EDGE.match(giong)
    if m:
        return m.group(1) == (language or "").split("-")[0].lower()
    try:
        from services.tts_engines import ENGINE_VIENEU, engine_of                  # MỘT điểm phân luồng bộ đọc (test_voice_dispatch_ratchet cấm tự kiểm tiền tố)
        if engine_of(giong) == ENGINE_VIENEU:
            return (language or "").split("-")[0].lower() == "vi"
    except Exception:  # noqa: BLE001 — thiếu module giọng thì không kiểm được, đừng làm hỏng việc dựng
        pass
    return True


def giong_theo_nguoi(language: str, giong: str, nguoi: list) -> list:
    """Giọng cho từng người nói của bài hội thoại, theo giới (nam / nữ) từ danh sách giọng Edge của ngôn ngữ; hai người cùng giới có hai giọng khác nhau.
    Giọng người dùng chọn là giọng Edge → dùng cho người đầu tiên cùng giới; giọng khác (VieNeu / Qwen) thì dùng cho TẤT CẢ (không ghép được giọng nam/nữ
    khác engine)."""
    from routes.tts_routes import EDGE_VOICES_BY_LANG
    ds = EDGE_VOICES_BY_LANG.get(language) or []
    if giong and not giong_hop_ngon_ngu(giong, language):
        #  Giọng đã chọn thuộc ngôn ngữ KHÁC (vd cấu hình mặc định mang theo giọng tiếng Việt sang bài tiếng Nhật): trước đây nhánh "giọng không phải Edge của ngôn ngữ" dùng nó cho MỌI người đọc,
        #  tiếng Nhật bị giọng khác ngôn ngữ đọc. Bỏ nó, dùng giọng mặc định đúng ngôn ngữ.
        logger.warning("Sáng tạo: giọng '%s' không đọc được ngôn ngữ '%s' — dùng giọng mặc định của ngôn ngữ", giong, language)
        giong = ""

    def gioi(x: dict) -> Optional[str]:
        lb = x.get("label", "")
        return "male" if "(Nam" in lb else "female" if "(Nữ" in lb else None
    nam = [x["id"] for x in ds if gioi(x) == "male"]
    nu = [x["id"] for x in ds if gioi(x) == "female"]
    la_edge = any(x["id"] == giong for x in ds)
    if giong and not la_edge:
        return [giong] * len(nguoi)
    chon = {}
    if la_edge:
        chon[next(gioi(x) for x in ds if x["id"] == giong)] = giong
    dem = {"male": 0, "female": 0}
    ra = []
    for sp in nguoi:
        g = sp.get("gender") or "female"
        ds_g = (nam if g == "male" else nu) or [x["id"] for x in ds] or [giong]
        ra.append(chon[g] if (dem[g] == 0 and g in chon) else ds_g[min(dem[g], len(ds_g) - 1)])
        dem[g] += 1
    return ra


def _khoa_giong_bai(ke: list, voices: list, toc: float, opts: dict, lan: int = 0) -> str:
    """Khoá tệp tiếng GHÉP của một cảnh: chữ + giọng từng đoạn, tốc độ, tuỳ chọn bài, và ``giu`` (giây giữ khung hình sau đoạn — đổi thì khoảng lặng giữa các đoạn đổi nên tệp ghép PHẢI dựng lại)."""
    return hashlib.md5(json.dumps([[s["i"], s["luot"], s["el"], s["van"], vc, s.get("giu", 0.0)] for s, vc in zip(ke, voices)] + [toc, opts, "v10"] + ([f"r{lan}"] if lan else []), ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


async def _tao_giong_bai(tts: Any, c: dict, language: str, giong: str, ngon_me: str, toc: float, opts: dict, thu_muc_voice: str, cham: bool = False, tien: Any = None, lan: int = 0) -> str:
    """Tiếng cho MỘT cảnh bài đọc: đọc từng đoạn (``sangtao_bai.ke_hoach``) bằng giọng đúng người đọc (nam/nữ) hoặc giọng tiếng mẹ đẻ (giải thích bài giảng), nối
    lại có khoảng lặng; ghi mốc từng đoạn cạnh tệp (``.moc.json``). Tiếng từng đoạn lưu theo băm (chữ|giọng|tốc độ) nên lượt đọc lại và dựng lại không đọc lại.
    ``lan`` > 0 = cảnh đã được "làm lại" ``lan`` lần (tab Cảnh): băm tệp giọng + khoá tiếng ghép thêm đuôi ``|r<lan>`` (tệp mới, không dùng lại bản cũ) và giọng Qwen sinh với seed lệch đi; ``lan`` = 0 giữ NGUYÊN tên tệp cũ.
    Đoạn không có tiếng (``am`` = False) được thay bằng im lặng đủ thời gian để đọc chữ trên màn hình."""
    dl = c["visual"]["data"]
    ke = B.ke_hoach(dl, opts)
    ds_giong = giong_theo_nguoi(language, giong, dl["speakers"])
    giong_me = giong_mac_dinh(ngon_me)                                             # giải thích đọc bằng giọng mặc định của ngôn ngữ mẹ đẻ
    voices = [None if not s["am"] else giong_me if s["ngon"] == "nat" else ds_giong[s["nguoi"] % len(ds_giong)] for s in ke]
    khoa = _khoa_giong_bai(ke, voices, toc, opts, lan)
    ra = os.path.join(thu_muc_voice, f"baidoc_{khoa}.mp3")
    if os.path.exists(ra) and os.path.getsize(ra) > 0 and os.path.exists(ra + ".moc.json"):
        if tien:
            tien(1, 1)
        return ra
    tep_cua = {}
    hau = f"|r{lan}" if lan else ""
    #  "Làm lại cảnh" (lan > 0): Qwen đổi seed qua ``lan_sinh``; Edge / VieNeu không có seed nên PHẢI ``overwrite=True`` — không
    #  thì khoá đệm cùng chữ trả đúng bản cũ và nút "Làm lại" không làm gì (người dùng dặn 07-10-2026: bấm sinh lại là sinh lại).
    kw_lan = {"lan_sinh": lan, "overwrite": True} if lan else {}
    for s, vc in zip(ke, voices):
        if vc:
            tep_cua.setdefault((s["van"], vc), os.path.join(thu_muc_voice, hashlib.md5(f"{s['van']}|{vc}|{toc}{hau}".encode()).hexdigest()[:20] + ".mp3"))
    cho = asyncio.Semaphore(4)
    tong_doan, da_doan = len(tep_cua), [0]

    def da_xong() -> None:
        """Báo tiến độ THEO ĐOẠN: một cảnh bài đọc có hàng chục đoạn tiếng (cảnh duy nhất cũng vậy), nếu chỉ báo theo cảnh thì thanh tiến độ đứng im rồi nhảy thẳng tới hết."""
        da_doan[0] += 1
        if tien:
            tien(da_doan[0], tong_doan)

    async def mot(van: str, vc: str, tep: str) -> None:
        if os.path.exists(tep) and os.path.getsize(tep) > 0:
            da_xong()
            return
        async with cho:
            thanh_cong, _ = await tts.generate_voice(van, tep, voice=vc, speed=toc, **kw_lan)
        if not (thanh_cong and os.path.exists(tep) and os.path.getsize(tep) > 0):
            raise RuntimeError(f"không tạo được giọng cho '{van[:20]}…'")
        da_xong()

    if tien:
        tien(0, max(1, tong_doan))
    await asyncio.gather(*[mot(van, vc, tep) for (van, vc), tep in tep_cua.items()])
    doan = []
    for s, vc in zip(ke, voices):
        if vc:
            tep = tep_cua[(s["van"], vc)]
            doan.append((tep, D.do_thoi_luong_am_thanh(tep)))
        else:
            doan.append((None, B.uoc_do_dai(s, language, ngon_me, cham)))
    moc, khe = B.thoi_gian(ke, [d for _, d in doan], opts)
    D.ghep_doan(doan, khe, ra)
    lang = await asyncio.gather(*[asyncio.to_thread(D.do_lang_dau_cuoi, tep) if tep else asyncio.sleep(0, (0.0, 0.0)) for tep, _ in doan])
    tieng = [[round(a + ld, 3), round(max(a + ld + 0.2, b - lc), 3)] if tep else [a, b] for (tep, _), (a, b), (ld, lc) in zip(doan, moc, lang)]     # khoảng THỰC SỰ có tiếng: phụ đề chia câu trong khoảng này
    ngat = await asyncio.gather(*[asyncio.to_thread(D.do_nghi_giua, tep) if tep else asyncio.sleep(0, []) for (tep, _) in doan])           # đã có sẵn trong bộ nhớ đệm của do_lang_dau_cuoi (cùng một lần chạy ffmpeg)
    nghi = [[[round(a + x, 3), round(a + y, 3)] for x, y in ds] for ds, (a, _) in zip(ngat, moc)]            # chỗ giọng ngắt GIỮA lời nói của từng đoạn (thời gian TRONG cảnh): ranh giới câu thật cho phụ đề đáy + phụ đề CC
    with open(ra + ".moc.json", "w", encoding="utf-8") as f:
        json.dump({"moc": moc, "tieng": tieng, "nghi": nghi}, f)
    return ra


def _toc_doc(st: dict, cao_tuoi: bool) -> float:
    """Tốc độ đọc: cài đặt ``speed`` (0,6–1,3); không đặt (0) thì dùng tốc độ dành cho người học: 0,9 (trẻ em 0,8).
    Đo thực tế giọng Edge tiếng Nhật: 1,0× ≈ 6,7–7,1 âm tiết/giây (tốc độ đọc tin của người bản xứ — người học nghe thấy nhanh), 0,9× ≈ 6,0, 0,8× ≈ 5,4."""
    try:
        x = float(st.get("speed") or 0.0)
    except (TypeError, ValueError):
        x = 0.0
    return round(min(1.3, max(0.6, x)), 2) if x else (0.8 if cao_tuoi else 0.9)


def tao_giong(pid: str, sb: dict, giong: str, toc: float, huy: threading.Event, bao: Any, bai: Optional[dict] = None, ngon_me: str = "vi", ten_nguon: str = "", the_phu: Optional[dict] = None,
              tien: Any = None, lan_canh: Optional[dict] = None) -> tuple[dict, list[str]]:
    """Sinh giọng cho từng cảnh (tốc độ ``toc``) → ({id_cảnh: mp3}, cảnh_báo). ``lan_canh`` = {id_cảnh: số lần "làm lại"} (xem :func:`lan_giong`). Cảnh lỗi để im lặng và báo; lỗi hết mọi cảnh (thường do mất mạng) thì ném lỗi rõ ràng."""
    from services.tts_service import TTSService
    d = os.path.join(thu_muc(pid), "voice")
    os.makedirs(d, exist_ok=True)
    opts = B.tuy_chon(bai)
    opts["ngon_me"] = ngon_me
    opts["ngon_dich"] = sb.get("language", "en")                              # lời dẫn nói bằng tiếng đích
    opts["ten_nguon"] = ten_nguon or opts.get("ten_nguon", "")                 # lời chào / lời kết nhắc nguồn tin
    ket: dict = {}
    ket_phu: dict = {}                                                            # {0: thẻ mở đầu, -1: thẻ kết thúc} → mp3
    bao_loi: list[str] = []
    canhs = sb["scenes"]
    n_canh = len(canhs)
    tong_dv = 1000 * (n_canh + (1 if the_phu else 0))                             # đơn vị tiến độ: mỗi cảnh 1000, thẻ mở đầu / kết thúc cộng lại 1000

    async def chay() -> None:
        tts = TTSService()
        for i, c in enumerate(canhs):
            if huy.is_set():
                raise Huy()
            bao(f"Đang tạo giọng đọc {i + 1}/{n_canh}")

            def tien_canh(da: int, tong: int, i: int = i) -> None:
                if tien:
                    tien(i * 1000 + (1000 * da) // max(1, tong), tong_dv, f"Tạo giọng đọc — cảnh {i + 1}/{n_canh} · đoạn {da}/{tong}")

            try:
                ket[c["id"]] = await _tao_giong_bai(tts, c, sb.get("language", "en"), giong, ngon_me, toc, opts, d, sb.get("audience") == "kids", tien_canh,
                                                 int((lan_canh or {}).get(c["id"], 0)))
            except Exception as e:  # noqa: BLE001
                logger.warning("Sáng tạo: giọng cảnh %s lỗi: %s", c["id"], e)
                bao_loi.append(f"Cảnh {c['id']}: không tạo được giọng — để im lặng")

    async def chay_the() -> None:
        """Người dẫn ĐỌC tiêu đề ở thẻ mở đầu và lời cảm ơn ở thẻ kết thúc (tiếng đích), để lúc bút viết chữ không bị im lặng. Lỗi thì thẻ để im lặng, không làm hỏng video."""
        if not the_phu or not canhs:
            return
        tts = TTSService()
        vc = giong_theo_nguoi(sb.get("language", "en"), giong, canhs[0]["visual"]["data"]["speakers"])[0]
        if tien:
            tien(n_canh * 1000, tong_dv, "Tạo giọng thẻ mở đầu / kết thúc")
        for id_, van in the_phu.items():
            van = RB_bo_ruby(str(van or "")).strip()
            if not van:
                continue
            try:
                tep = os.path.join(d, "thephu_" + hashlib.md5(f"{van}|{vc}|{toc}|v1".encode()).hexdigest()[:20] + ".mp3")
                if not (os.path.exists(tep) and os.path.getsize(tep) > 0):
                    tho = tep + ".tho.mp3"
                    thanh_cong, _ = await tts.generate_voice(van, tho, voice=vc, speed=toc)
                    if not (thanh_cong and os.path.exists(tho) and os.path.getsize(tho) > 0):
                        raise RuntimeError("không tạo được giọng")
                    D.ghep_doan([(tho, D.do_thoi_luong_am_thanh(tho))], [0.0], tep, dau=D.LANG_DAU_THE)
                    os.remove(tho)
                ket_phu[id_] = tep
            except Exception as e:  # noqa: BLE001
                logger.warning("Sáng tạo: giọng thẻ %s lỗi: %s", id_, e)
                bao_loi.append("Thẻ mở đầu / kết thúc: không tạo được giọng — để im lặng")
        if tien:
            tien(tong_dv, tong_dv, "Tạo giọng thẻ mở đầu / kết thúc")

    asyncio.run(chay())
    if not ket:
        raise RuntimeError("Không tạo được giọng đọc cho cảnh nào (kiểm tra kết nối mạng — giọng Edge cần Internet).")
    if the_phu and not huy.is_set():
        asyncio.run(chay_the())
        ket.update(ket_phu)
    return ket, bao_loi


# ------------------------------------------------------------------ dựng nền
def nd_canh(c: dict) -> str:
    """Dấu NỘI DUNG của một cảnh (dữ liệu bài, thời lượng) — ghi cạnh clip lúc dựng, so lại khi xem để biết cảnh đã bị sửa
    sau lần dựng (ảnh đại diện còn nhưng đã cũ)."""
    d = {k: c.get(k) for k in ("visual", "duration_s")}
    return hashlib.md5(json.dumps(d, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]


def canh_cu(pid: str, canh_dia: dict) -> list:
    """Danh sách số cảnh đã dựng nhưng NỘI DUNG đã đổi kể từ đó."""
    try:
        scenes = (doc(pid).get("storyboard") or {}).get("scenes") or []
    except KeyError:
        return []
    ra = []
    d = os.path.join(thu_muc(pid), "scenes")
    for so, khoa in canh_dia.items():
        i = int(so) - 1
        tep = os.path.join(d, f"canh_{int(so):04d}_{khoa}.nd")
        if i < 0 or i in (len(scenes), len(scenes) + 1):
            continue                                              # 0 = mở đầu, N+1 = toàn văn, N+2 = kết thúc: không thuộc kịch bản nên không "cũ"
        if i > len(scenes) + 1:
            ra.append(int(so))                                    # cảnh đã bị xoá khỏi kịch bản
            continue
        try:
            with open(tep, encoding="utf-8") as f:
                if f.read().strip() != nd_canh(scenes[i]):
                    ra.append(int(so))
        except OSError:
            pass                                                  # clip dựng từ bản cũ chưa có dấu: coi như còn mới
    return ra


def tep_video(pid: str) -> Optional[str]:
    """Đường dẫn tuyệt đối của video đã dựng (hoặc None) — để giao diện mở thư mục chứa nó / chép đường dẫn."""
    p = os.path.join(thu_muc(pid), "out.mp4")
    return os.path.abspath(p) if os.path.exists(p) else None


def mo_thu_muc(pid: str) -> str:
    """Mở File Explorer tại video (chọn sẵn tệp) hoặc thư mục dự án nếu chưa có video. Trả đường dẫn đã mở."""
    import subprocess
    import sys
    p = tep_video(pid) or os.path.abspath(thu_muc(pid))
    if os.name == "nt":
        if os.path.isfile(p):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        else:
            os.startfile(p)                                      # noqa: S606 — đường dẫn do app tự dựng, không phải đầu vào người dùng
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", p] if os.path.isfile(p) else ["open", p])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(p) if os.path.isfile(p) else p])
    return p


def canh_tren_dia(pid: str) -> dict:
    """{số_cảnh: khoá} của các cảnh đã dựng và còn trên đĩa (bản mới nhất) — để màn hình còn thấy sau khi mở lại app."""
    d = os.path.join(thu_muc(pid), "scenes")
    ra: dict = {}
    if not os.path.isdir(d):
        return ra
    moi: dict = {}
    for t in os.listdir(d):
        if t.startswith("canh_") and t.endswith(".jpg"):
            try:
                so = int(t[5:9])
                k = t[10:-4]
            except ValueError:
                continue
            mt = os.path.getmtime(os.path.join(d, t))
            if so not in moi or mt > moi[so]:
                moi[so], ra[str(so)] = mt, k
    return ra


def trang_thai(pid: str) -> dict:
    v = VIEC.get(pid)
    if not v:
        cd = canh_tren_dia(pid)
        return {"state": "idle", "done": 0, "total": 0, "message": "", "error": None, "canh": cd, "canh_cu": canh_cu(pid, cd), "dang": [],
                "co_video": os.path.exists(os.path.join(thu_muc(pid), "out.mp4")), "tep": tep_video(pid)}
    ra = {k: v.get(k) for k in ("state", "done", "total", "message", "error", "canh_bao", "tong_giay", "giay_dung", "xem_truoc", "pha", "buoc", "so_buoc")}
    ra["giay_pha"] = round(time.time() - v["bat_dau_pha"], 1) if v.get("bat_dau_pha") else 0       # giây đã chạy ở BƯỚC hiện tại (ước thời gian còn lại tính theo bước, không lẫn bước sinh giọng)
    ra["canh"] = {**canh_tren_dia(pid), **{str(k): x for k, x in (v.get("canh") or {}).items()}}   # {số_cảnh: khoá}; ảnh đại diện lấy theo khoá
    ra["canh_cu"] = canh_cu(pid, ra["canh"])
    ra["dang"] = sorted(v.get("dang") or [])                      # cảnh đang được dựng lúc này
    ra["giay_troi_qua"] = round(time.time() - v["bat_dau"], 1) if v.get("bat_dau") else 0
    ra["co_video"] = os.path.exists(os.path.join(thu_muc(pid), "out.mp4"))
    ra["tep"] = tep_video(pid)
    return ra


def bat_dau_dung(pid: str, chi_cac_canh: Optional[list] = None, lam_lai: bool = False) -> None:
    """Dựng video (cả video, hoặc ``chi_cac_canh`` = xem thử vài cảnh). ``lam_lai`` (chỉ có nghĩa khi có ``chi_cac_canh``): LÀM LẠI các cảnh đó từ đầu — tăng số lần làm lại (:func:`_tang_lan_giong`) để giọng sinh mới
    và hình dựng lại, thay vì dùng lại bộ nhớ đệm. Người dùng sau đó dựng cả video một lần nữa: cảnh đã làm lại được dùng lại, chỉ nối."""
    du_an = doc(pid)
    sb_dict = du_an.get("storyboard")
    if not sb_dict:
        raise ValueError("Dự án chưa có storyboard")
    sb, loi = SC.kiem_tra(sb_dict)
    if sb is None:
        raise ValueError("Storyboard lỗi: " + "; ".join(loi[:3]))
    st = du_an["settings"]
    bai = B.tuy_chon(st)
    ngon_me = st.get("native_language") or "vi"
    with _KHOA:
        v = VIEC.get(pid)
        if v and v["state"] == "running":
            raise RuntimeError("Dự án đang dựng")
        if lam_lai and chi_cac_canh:                                         # TRƯỚC khi đặt trạng thái "đang dựng": ghi lỗi thì không để dự án kẹt ở "running"
            _tang_lan_giong(pid, chi_cac_canh)
        huy = threading.Event()
        v = {"state": "running", "done": 0, "total": 0, "message": "Chuẩn bị", "error": None, "huy": huy, "canh_bao": [],
             "canh": {}, "dang": set(), "bat_dau": time.time(), "xem_truoc": bool(chi_cac_canh), "pha": "chuan_bi", "buoc": 0, "so_buoc": 3, "bat_dau_pha": time.time()}
        VIEC[pid] = v

    def chay() -> None:
        out = os.path.join(thu_muc(pid), "out.mp4")
        tam_out = os.path.join(thu_muc(pid), "dang_dung.mp4")

        def dat_pha(pha: str, d: int, t: int, m: str) -> None:
            """Ba BƯỚC rõ ràng: 1 tạo giọng · 2 dựng hình · 3 nối các cảnh (video không tiếng thì chỉ còn 2 bước). ``done / total`` tính RIÊNG trong từng bước."""
            if v.get("pha") != pha:
                v["bat_dau_pha"] = time.time()
            n = int(v.get("so_buoc") or 3)
            v.update(pha=pha, buoc={"giong": 1, "hinh": n - 1, "noi": n}.get(pha, 0), done=d, total=t, message=m)

        def td(d: int, t: int, m: str) -> None:
            dat_pha("noi" if "nối" in m.lower() else "hinh", d, t, m)

        try:
            storage_paths.check_render_disk_space(thu_muc(pid), min_free_mb=max(1024.0, 3.0 * len(sb.scenes)))   # ~3 MB/cảnh
            am_thanh = None
            canh_bao_giong: list[str] = []
            giong = st.get("voice")
            if giong is None:
                giong = giong_mac_dinh(sb.language)
            v["so_buoc"] = 3 if giong else 2
            mo_dau = ket_thuc = None
            nguon = nguon_hien(st.get("source"), sb.language)                  # dòng nguồn trong video bằng TIẾNG ĐÍCH (chỉ bản dịch mới là tiếng mẹ đẻ)
            if st.get("intro", True):
                #  "tr" = bản dịch tiêu đề (chỉ cho phụ đề CC của thẻ mở đầu; không vào hình): lấy từ cảnh đầu khi thẻ đọc đúng tiêu đề bài, tự gõ chữ khác thì không có bản dịch.
                du_lieu_dau = ((sb_dict.get("scenes") or [{}])[0].get("visual") or {}).get("data") or {}
                tr_dau = "" if st.get("intro_text") else str(du_lieu_dau.get("title_tr") or "").strip()
                mo_dau = {"text": (st.get("intro_text") or sb.title or du_an["title"]).strip(), "color": "blue", "sub": nguon, "tr": tr_dau}
            if st.get("outro", True):
                ket_thuc = {"text": (st.get("outro_text") or D.LOI_CAM_ON.get(sb.language, D.LOI_CAM_ON["en"])).strip(), "color": "green", "sub": nguon}
            if giong:
                sd = sb.model_dump()
                B.danh_dau_vi_tri(sd["scenes"])                    # cảnh đầu / cuối của CẢ video (lời chào, lời kết), tính trước khi lọc
                if chi_cac_canh:                                   # xem thử vài cảnh: chỉ đọc đúng các cảnh đó
                    sd["scenes"] = [c for c in sd["scenes"] if c["id"] in chi_cac_canh]
                am_thanh, canh_bao_giong = tao_giong(pid, sd, giong, _toc_doc(st, sb.audience in SC.NGUOI_XEM_CHAM), huy, lambda m: v.update(message=m), bai, ngon_me, _ten_nguon(st.get("source")),
                                                       None if chi_cac_canh else {k_: c_["text"] for k_, c_ in ((0, mo_dau), (-1, ket_thuc)) if c_},
                                                       tien=lambda d_, t_, m_: dat_pha("giong", d_, t_, m_), lan_canh=lan_giong(pid))

            def xong(so: int, khoa: str, tt: str) -> None:
                v["canh"][so] = khoa
                v["dang"].discard(so)
                if 1 <= so <= len(sb_dict["scenes"]):                  # mở đầu/kết thúc không có dấu nội dung
                    try:
                        with open(os.path.join(thu_muc(pid), "scenes", f"canh_{so:04d}_{khoa}.nd"), "w", encoding="utf-8") as f:
                            f.write(nd_canh(sb_dict["scenes"][so - 1]))
                    except OSError:
                        pass

            def bat_dau(so: int) -> None:
                v["dang"].add(so)

            kq = D.dung_video(sb, tam_out, am_thanh=am_thanh, mo_dau=mo_dau, ket_thuc=ket_thuc, thu_muc_canh=os.path.join(thu_muc(pid), "scenes"), canh_xong=xong,
                              canh_bat_dau=bat_dau, bo_noi=bool(chi_cac_canh), huy=huy, tien_do=td, chi_cac_canh=chi_cac_canh,
                              ngon_me=ngon_me, furigana=st.get("furigana", True), phien_am=st.get("phonetic", True) is not False, bai=bai, nguon=nguon, camera=st.get("camera") is True, ten_nguon=_ten_nguon(st.get("source")),
                              bo_cuc="cuon" if st.get("layout") == "scroll" else "trang",
                              toan_van=None if (chi_cac_canh or st.get("script_full") is False) else True)
            if not chi_cac_canh:
                os.replace(tam_out, out)
                DDV.giay_cua(out, [1])                                                # đo sẵn độ dài bản dựng mới (Trang chủ hiện) — lần mở danh sách sau khỏi đo
                _ghi_phu_de(pid, kq.phu_de, sb.language, ngon_me)                     # phụ đề CC khớp ĐÚNG bản dựng vừa ghi (cùng lần dựng, cùng mốc)
            v.update(state="done", message="Xong", canh_bao=canh_bao_giong + kq.canh_bao, tong_giay=round(kq.tong_giay, 1), giay_dung=round(kq.thoi_gian_dung, 1))
            if not chi_cac_canh:
                bat_dau_dich_cc(pid)                                                  # thứ tiếng phụ đề CC đã chọn trước khi dựng: tự thêm + dịch (luồng nền riêng)
        except Huy:
            v.update(state="cancelled", message="Đã hủy")
        except Exception as e:  # noqa: BLE001
            logger.exception("Dựng Sáng tạo lỗi")
            v.update(state="error", error=str(e) or e.__class__.__name__, message="Lỗi")
        finally:
            if os.path.exists(tam_out):
                try:
                    os.remove(tam_out)
                except OSError:
                    pass

    t = threading.Thread(target=chay, name=f"sangtao-{pid}", daemon=True)
    v["thread"] = t
    t.start()


def tep_canh(pid: str, so: int, duoi: str) -> Optional[str]:
    """Tệp mới nhất của cảnh ``so`` đã dựng (``.mp4`` | ``.jpg``), hoặc None."""
    d = os.path.join(thu_muc(pid), "scenes")
    if not os.path.isdir(d):
        return None
    ung = [os.path.join(d, t) for t in os.listdir(d) if t.startswith(f"canh_{int(so):04d}_") and t.endswith(duoi) and ".dang_dung" not in t]
    return max(ung, key=os.path.getmtime) if ung else None


def huy_dung(pid: str) -> bool:
    v = VIEC.get(pid)
    if v and v["state"] == "running":
        v["huy"].set()
        return True
    return False
