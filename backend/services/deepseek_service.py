"""Dịch (lồng tiếng) & Tạo kịch bản (review) bằng model DeepSeek.

- API DeepSeek tương thích OpenAI (`{base_url}/chat/completions`). Dùng `urllib`
  stdlib (KHÔNG thêm dependency, giữ bản đóng gói PyInstaller gọn) — theo đúng
  cách usage_reporter.py / auth_routes.py đang gọi HTTP.
- Cấu hình đọc từ ConfigService.get_deepseek_config() (base_url + models khai ở
  deploy.config.js; api_key do người dùng nhập trong Cài đặt).
- LỒNG TIẾNG: gửi 100 đoạn/lần với prompt dịch lồng tiếng (mirror prompt ở
  frontend SubtitleManager.copyAIPrompt). Lô lỗi -> fallback Google cho lô đó.
- REVIEW: gửi 100 đoạn A/lần với prompt "Storyteller" (mirror PromptManager.copyPrompt),
  đánh số b_id liên tục qua các lô. Không fallback (Google không tạo được kịch bản).
"""

from utils.i18n import L
import os
import re
import json
import time
import logging
import urllib.request
import urllib.error

from services.ai_engines import AI_CONCURRENCY, DEEPSEEK, GEMINI, tach_model

logger = logging.getLogger("DeepSeekService")

# Số đoạn gửi lên mỗi request.
DUBBING_BATCH_SIZE = 100
# Review: lô nhỏ hơn dubbing — output là JSON kịch bản dài (tiếng Việt viết lại),
# lô 100 dễ chạm trần max_tokens -> JSON cụt -> parse hỏng cả lô. Lô 50 nhanh hơn
# từng request, ổn định hơn, và tận dụng tốt hơn DEEPSEEK_CONCURRENCY.
REVIEW_BATCH_SIZE = 50
# Số request chạy SONG SONG cùng lúc (giống semaphore của Google Translate). Nhiều
# lô 100-đoạn được gửi đồng thời rồi gộp kết quả, thay vì chạy tuần tự từng lô.
# Dùng CHUNG cho mọi bộ AI (``ai_engines.AI_CONCURRENCY``, 6 → 5 ngày 07-10-2026); tên cũ giữ làm bí danh.
DEEPSEEK_CONCURRENCY = AI_CONCURRENCY

# Tốc độ đọc TTS (ký tự/giây) theo ngôn ngữ đích — ĐỒNG BỘ với CPS_BY_LANG ở
# frontend SubtitleManager.tsx để giới hạn ký tự nhất quán 2 nơi.
#
# Ba bảng dưới đây phủ HẾT thứ tiếng có giọng Edge (``tts_routes.py``), kể cả thứ
# tiếng chưa được chào ở ô "Dịch sang": thiếu mã nào thì prompt in ra mã thô và trần
# ký tự rơi về mặc định 14. ``test_parity_crop_mirror.py`` gác chỗ này.
CPS_BY_LANG = {
    "vi": 15, "en": 15, "fr": 15, "de": 14, "es": 15, "id": 15,
    "pt": 15, "it": 15,
    "ru": 13, "th": 11,
    "ja": 7, "ko": 8, "zh-cn": 7, "zh": 7,
}
# CPS cho kịch bản review tiếng Việt (mirror CPS_VI ở PromptManager.tsx).
CPS_VI_REVIEW = 15
# Tốc độ đọc ước lượng của giọng review ở 1.0x (từ/phút). Dùng để quy đổi thời
# lượng cảnh -> số từ mục tiêu. ~150 wpm sát giọng VieNeu tự nhiên hơn 135 cũ, giúp
# ước lượng "lấp kín" chính xác hơn ở chế độ xê dịch (giọng KHÔNG kéo giãn).
WPM_VI_REVIEW = 150

LANG_NAMES = {
    "vi": "Vietnamese", "en": "English", "zh-cn": "Chinese (Simplified)",
    "zh": "Chinese (Simplified)", "ja": "Japanese", "ko": "Korean",
    "fr": "French", "de": "German", "es": "Spanish",
    "pt": "Portuguese (Brazil)", "it": "Italian",
    "ru": "Russian", "th": "Thai", "id": "Indonesian",
}

# Tên ngôn ngữ VIẾT BẰNG TIẾNG VIỆT — dùng cho prompt REVIEW, vốn soạn toàn bộ bằng
# tiếng Việt ("biên tập lời bình Tiếng Việt súc tích…"). Nhét "Vietnamese"/"English"
# vào giữa câu tiếng Việt thì đọc lổn nhổn, nên giữ riêng bảng này thay vì dùng
# LANG_NAMES (bảng đó dành cho prompt dịch, viết theo lối quốc tế).
TARGET_LANG_VI = {
    "vi": "Tiếng Việt", "en": "Tiếng Anh", "zh": "Tiếng Trung",
    "zh-cn": "Tiếng Trung", "ja": "Tiếng Nhật", "ko": "Tiếng Hàn",
    "fr": "Tiếng Pháp", "de": "Tiếng Đức", "es": "Tiếng Tây Ban Nha",
    "pt": "Tiếng Bồ Đào Nha", "it": "Tiếng Ý",
    "ru": "Tiếng Nga", "th": "Tiếng Thái", "id": "Tiếng Indonesia",
}

# ------------------------------------------------------ phong cách / giọng đọc
# Preset "kiểu nói" người dùng chọn ở UI (dropdown) — id → chỉ dẫn văn phong chèn
# vào prompt. MIRROR với SPEECH_STYLE_PRESETS ở frontend (constants/speechStyles.ts)
# cho luồng copy-prompt thủ công. Giá trị KHÔNG có trong map = văn bản tự nhập,
# chèn nguyên văn. "auto"/"" = không chèn gì (AI tự xử lý — hành vi cũ).
SPEECH_STYLES = {
    "review": (
        "văn phong REVIEW/KỂ CHUYỆN PHIM — sắc sảo, cuốn hút, nhịp kể nhanh, "
        "từ ngữ đời thường có điểm nhấn"
    ),
    "co-trang": (
        "văn phong CỔ TRANG/KIẾM HIỆP — xưng hô ta/ngươi/hắn/nàng/huynh/muội, "
        "ưu tiên từ Hán-Việt trang trọng (tỷ như: bổn cung, tại hạ, cáo từ), "
        "tránh từ ngữ hiện đại"
    ),
    "tong-tai": (
        "văn phong TỔNG TÀI/NGÔN TÌNH hiện đại — lãng mạn, kịch tính, xưng hô "
        "anh/em/cô ấy, giọng điệu quyền lực nhưng tình cảm"
    ),
    "hoat-hinh": (
        "văn phong HOẠT HÌNH/THIẾU NHI — vui tươi, trong sáng, câu ngắn dễ hiểu, "
        "tránh từ ngữ nặng nề/bạo lực"
    ),
    "lich-su": (
        "văn phong LỊCH SỬ/TÀI LIỆU — trang trọng, chuẩn xác, khách quan, "
        "dùng danh xưng và thuật ngữ đúng bối cảnh thời đại"
    ),
    "hai-huoc": (
        "văn phong HÀI HƯỚC — dí dỏm, chơi chữ, tiết tấu nhanh, có thể cường điệu "
        "nhẹ để gây cười nhưng không đổi ý nghĩa gốc"
    ),
}

DEFAULT_REVIEW_BRAND = "Bubi review phim"


def resolve_style_directive(style: str | None) -> str:
    """id preset → chỉ dẫn văn phong; chuỗi tự nhập → giữ nguyên; auto/"" → ""."""
    s = (style or "").strip()
    if not s or s.lower() == "auto":
        return ""
    return SPEECH_STYLES.get(s, s)


def voice_gender_desc(voice_id: str | None) -> str:
    """Mô tả giọng đọc người dùng đã chọn (giới tính + tên) để AI chọn xưng hô/
    ngôi kể phù hợp. Best-effort: không nhận diện được giới tính thì trả ""
    (không chèn gì, tránh đoán sai)."""
    vid = (voice_id or "").strip()
    if not vid:
        return ""
    low = vid.lower()
    # Edge-TTS tiếng Việt (2 giọng cố định)
    if "hoaimy" in low:
        return "giọng NỮ (Hoài My)"
    if "namminh" in low:
        return "giọng NAM (Nam Minh)"
    # VieNeu preset: label dạng "Tên — Nữ · Bắc · ..." (token đầu sau "—" là giới
    # tính; đừng tìm "Nam" toàn label vì miền Nam cũng là "Nam").
    try:
        from services.vieneu_service import PRESET_VOICES
        for p in PRESET_VOICES:
            if p["name"].lower() in low:
                gender = p["label"].split("—", 1)[1].split("·", 1)[0].strip()
                if gender in ("Nam", "Nữ"):
                    return f"giọng {gender.upper()} ({p['name']})"
                break
    except Exception:
        pass

    # Giọng Edge của MỌI THỨ TIẾNG KHÁC. Hai nhánh trên chỉ biết giọng tiếng Việt,
    # nên dịch sang tiếng Nhật/Anh/Trung là câu "GIỌNG ĐỌC MẶC ĐỊNH" biến mất khỏi
    # prompt và AI tự chọn xưng hô — video giọng nam mà lời thoại xưng "chị" là
    # kiểu lỗi không ai soi ra cho tới khi nghe xong cả phim.
    # Giới tính đọc từ nhãn của chính bảng giọng ("Nanami (Nữ)", "Sonia (Nữ - UK)").
    try:
        from routes.tts_routes import EDGE_VOICES_BY_LANG
        for cac_giong in EDGE_VOICES_BY_LANG.values():
            for v in cac_giong:
                if str(v.get("id", "")).lower() != low:
                    continue
                nhan = str(v.get("label") or "")
                ten = nhan.split("(", 1)[0].strip()
                trong = nhan[nhan.find("(") + 1:nhan.rfind(")")] if "(" in nhan else ""
                #  "Nữ - UK" -> "Nữ": phần sau gạch là vùng giọng, không phải giới tính.
                gt = trong.split("-", 1)[0].strip()
                if gt in ("Nam", "Nữ"):
                    return f"giọng {gt.upper()} ({ten})"
                return ""
    except Exception:
        pass
    return ""


_ID_LINE_RE = re.compile(r"^\s*\[(\d+)\]\s*(.*)$")
_PAREN_PREFIX_RE = re.compile(r"^\([^)]*\)\s*")


# ------------------------------------------------------------------ cấu hình
def _cfg():
    from services.config_service import get_config_service
    return get_config_service().get_deepseek_config()


def is_configured(provider: str | None = None) -> bool:
    """Bộ AI ``provider`` đã có API key chưa. Không truyền = bộ AI ĐANG CHỌN trong Cài đặt (``ai_provider``) — nên mọi cổng
    "chưa có key AI" của các route tự theo lựa chọn người dùng (DeepSeek hay Gemini) mà không phải sửa từng chỗ."""
    try:
        if provider is None:
            from services.config_service import get_config_service

            provider = get_config_service().ai_provider_mac_dinh()
        return bool(_api_key_cua(provider))
    except Exception:
        return False


def _api_key_cua(provider: str) -> str:
    if provider == GEMINI:
        from services import gemini_service

        return str(gemini_service._cfg().get("api_key") or "")
    return str(_cfg().get("api_key") or "")


class DeepSeekError(Exception):
    """Lỗi gọi API DeepSeek kèm THÔNG BÁO THÂN THIỆN (user_message) để hiện lên UI.
    status = mã HTTP (nếu có) để phía trên phân loại thêm nếu cần."""

    def __init__(self, user_message: str, *, status: int | None = None, detail: str = ""):
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.status = status
        self.detail = detail


def _ten_model_con_dung(msg: str) -> list[str]:
    """Boc danh sach ten model hop le ra khoi cau loi cua DeepSeek.

    Cau cua ho co dang: "The supported API model names are a, b, but you passed c."
    """
    m = re.search(r"model names are\s+(.+?)(?:,\s*but you passed|\.|$)", msg or "", re.I)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def _friendly_api_error(status: int, body: str) -> str:
    """Dịch lỗi HTTP + body JSON của DeepSeek sang thông báo tiếng Việt dễ hiểu."""
    msg = ""
    try:
        msg = (json.loads(body).get("error") or {}).get("message", "") or ""
    except Exception:
        msg = body or ""
    low = f"{msg} {body}".lower()

    if status == 402 or "insufficient balance" in low:
        return (
            L("Tài khoản DeepSeek đã HẾT SỐ DƯ (Insufficient Balance). "
            "Vui lòng nạp tiền tại platform.deepseek.com rồi thử lại.", "The DeepSeek account has RUN OUT OF BALANCE (Insufficient Balance). Please top up at platform.deepseek.com and try again.")
        )
    if status == 401 or "authentication" in low or "invalid api key" in low or "unauthorized" in low:
        return (
            L("API key DeepSeek KHÔNG HỢP LỆ hoặc đã bị thu hồi. "
            "Kiểm tra lại API key trong mục Cài đặt.", "The DeepSeek API key is INVALID or has been revoked. Check the API key in Settings.")
        )
    if status == 429 or "rate limit" in low or "too many requests" in low:
        return (
            L("DeepSeek đang QUÁ TẢI hoặc vượt giới hạn tần suất (429). "
            "Vui lòng đợi ít phút rồi thử lại.", "DeepSeek is OVERLOADED or rate-limited (429). Please wait a few minutes and try again.")
        )
    #  Sai/thieu ten model: DeepSeek tra 400 kem DUNG danh sach ten con dung duoc. Bat
    #  lay danh sach do ma doc cho nguoi dung, thay vi nem nguyen cau tieng Anh — truoc
    #  day cau nay roi xuong nhanh "DeepSeek bao loi: <nguyen van>" nen khach chi thay
    #  tieng Anh lap lai hang chuc lan ma khong biet phai lam gi.
    if "supported api model names" in low or "you passed" in low:
        ds = ", ".join(_ten_model_con_dung(msg or body)) or L("xem thông báo của DeepSeek", "see the DeepSeek notice")
        return (
            L("Model DeepSeek không hợp lệ (app gửi lên tên model rỗng hoặc đã ngừng). "
            f"Model hợp lệ hiện tại: {ds}. "
            "Vào Cài đặt → DeepSeek chọn lại model rồi lưu.", f"Invalid DeepSeek model (the app sent an empty or discontinued model name). Currently valid models: {ds}. Go to Settings → DeepSeek, pick the model again and save.")
        )
    if status == 404 or "model" in low and ("not found" in low or "does not exist" in low or "deprecated" in low):
        return (
            L("Model DeepSeek không tồn tại/đã ngừng hỗ trợ. "
            "Hãy chọn model khác trong Cài đặt (vd deepseek-v4-flash).", "The DeepSeek model does not exist / is no longer supported. Pick another model in Settings (e.g. deepseek-v4-flash).")
        )
    if status and 500 <= status < 600:
        return L("Máy chủ DeepSeek đang gặp sự cố (lỗi 5xx). Vui lòng thử lại sau.", "The DeepSeek server is having problems (5xx error). Please try again later.")
    if msg:
        return L(f"DeepSeek báo lỗi: {msg}", f"DeepSeek reported an error: {msg}")
    return L(f"DeepSeek trả lỗi HTTP {status}.", f"DeepSeek returned HTTP error {status}.")


# ------------------------------------------------------------------ HTTP call
# --------------------------------------------------------------------------- #
#  SỰ CỐ API KEY — để giao diện hiện POPUP TOÀN CỤC thay vì một toast thoáng qua.
#
#  Người dùng góp ý (29-09-2026): hết số dư / key chết chỉ báo bằng toast, lúc họ không
#  nhìn là mất; app lặng lẽ rơi về Google Dịch -> 429, bản dịch kém mà không ai biết vì
#  sao. Ghi sự cố NGHIÊM TRỌNG (401 key sai/thu hồi, 402 hết tiền, 403 không có quyền)
#  xuống tệp; gọi thành công lần sau / đổi key thì tự xoá. Frontend hỏi
#  ``GET /videos/settings/deepseek/su-co`` (components/DeepSeekAlertDialog.tsx).
# --------------------------------------------------------------------------- #
#  Tệp DÙNG CHUNG cho mọi bộ AI từ 07-10-2026 (trường ``provider``); tên cũ deepseek_su_co.json không còn đọc.
_SU_CO_TEP = "ai_su_co.json"
#: Bấm "Để sau" thì chừng này giây không hiện lại (trừ khi đổi LOẠI sự cố).
SU_CO_NHAC_LAI_SAU = 30 * 60
#: Sự cố của bộ AI KHÔNG phải bộ mặc định chỉ giữ chừng này giây (đủ để thấy một lần) rồi tự xoá — xem ``doc_su_co``.
SU_CO_BO_KHAC_GIU = 10 * 60


def _bo_mac_dinh() -> str:
    """Bộ AI mặc định ở Cài đặt (test có thể thay)."""
    try:
        from services.config_service import get_config_service

        return get_config_service().ai_provider_mac_dinh()
    except Exception:  # noqa: BLE001
        return DEEPSEEK
_SU_CO_LOAI = {401: "sai_key", 402: "het_tien", 403: "khong_quyen"}
#: các loại sự cố hợp lệ (Gemini thêm ``het_han_muc``: gói miễn phí hết lượt trong ngày / chưa bật thanh toán)
LOAI_SU_CO = ("sai_key", "het_tien", "khong_quyen", "het_han_muc")


def _su_co_path() -> str:
    from models.database import get_app_data_dir

    return os.path.join(get_app_data_dir(), _SU_CO_TEP)


def _van_key(key: str) -> str:
    import hashlib

    return hashlib.sha1((key or "").encode("utf-8")).hexdigest()[:10]


#  Dịch Cơ bản chạy tới 6 lô SONG SONG: hết số dư là cả 6 lô cùng lỗi 402 trong vài mili
#  giây và cùng đọc-sửa-ghi tệp này. Không khoá + ghi thẳng = JSON rách -> đọc ra None,
#  popup không bao giờ hiện, mà tệp chỉ bị xoá khi gọi API thành công — đúng lúc hết số dư
#  thì không bao giờ có (rà soát 29-09-2026). Khoá luồng + ghi tệp tạm rồi os.replace.
import threading as _threading  # noqa: E402

_SU_CO_KHOA = _threading.Lock()


def _doc_tep_su_co(p: str) -> dict:
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}          # chưa có / rách -> coi như rỗng, ghi đè bằng bản đúng


def _ghi_tep_su_co(p: str, d: dict) -> None:
    tam = p + ".tmp"
    with open(tam, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tam, p)


def ghi_su_co(status, thong_bao: str, provider: str = DEEPSEEK) -> None:
    """``status``: mã HTTP (DeepSeek: 401/402/403) hoặc thẳng tên loại (Gemini phân loại theo nội dung lỗi)."""
    loai = status if isinstance(status, str) and status in LOAI_SU_CO else _SU_CO_LOAI.get(int(status or 0) if not isinstance(status, str) else 0)
    if not loai:
        return
    try:
        p = _su_co_path()
        with _SU_CO_KHOA:
            cu = _doc_tep_su_co(p)
            cung = cu.get("loai") == loai and cu.get("provider", DEEPSEEK) == provider
            d = {
                **cu,
                "provider": provider,
                "loai": loai, "thong_bao": thong_bao, "luc": time.time(),
                "so_lan": int(cu.get("so_lan") or 0) + 1 if cung else 1,
                "van_key": _van_key(_api_key_cua(provider)),
            }
            _ghi_tep_su_co(p, d)
    except Exception as e:  # noqa: BLE001 — ghi hỏng thì thôi, không chặn lỗi gốc
        logger.warning(L(f"Không ghi được sự cố {provider}: {e}", f"Could not record the {provider} incident: {e}"))


#: Dấu "Để sau" giữ lại khi sự cố tự hết (``giu_da_xem``) — phần còn lại của tệp.
_DA_XEM_KHOA = ("da_xem_luc", "da_xem_loai", "van_key")


def xoa_su_co(giu_da_xem: bool = False, provider: str | None = None) -> None:
    """Xoá sự cố. ``giu_da_xem=True`` (gọi API thành công): GIỮ dấu "Để sau" để lỗi
    xen kẽ thành công — vd chạy lô có model bị 403 còn model khác gọi được — không làm
    popup bật lại mỗi 45 giây sau khi người dùng đã bấm "Để sau" (rà soát 30-09-2026:
    trước đây xoá cả tệp nên dấu "Để sau" mất theo). Đổi / kiểm key thì xoá sạch.
    ``provider``: chỉ xoá nếu sự cố đang ghi là của bộ AI đó (Gemini gọi được không xoá sự cố của DeepSeek); None = xoá bất kể."""
    try:
        p = _su_co_path()
        if not os.path.exists(p):
            return
        with _SU_CO_KHOA:
            if provider is not None:
                d0 = _doc_tep_su_co(p)
                if d0.get("loai") and d0.get("provider", DEEPSEEK) != provider:
                    return
            if giu_da_xem:
                d = _doc_tep_su_co(p)
                if d.get("da_xem_luc"):
                    if "loai" in d:  # chỉ còn dấu thì thôi, khỏi ghi lại mỗi lần gọi
                        _ghi_tep_su_co(p, {k: d[k] for k in _DA_XEM_KHOA if k in d})
                    return
            os.remove(p)
    except Exception:  # noqa: BLE001
        pass


def doc_su_co() -> dict | None:
    """Sự cố còn hiệu lực, kèm ``hien`` = có nên bật popup không. Key đã đổi so với lúc
    ghi -> coi như đã xử lý (xoá)."""
    try:
        p = _su_co_path()
        if not os.path.exists(p):
            return None
        with _SU_CO_KHOA:
            d = _doc_tep_su_co(p)
        if not d or "loai" not in d:  # rỗng / chỉ còn dấu "Để sau" = không có sự cố
            return None
    except Exception:  # noqa: BLE001
        return None
    d.setdefault("provider", DEEPSEEK)
    if d.get("van_key") != _van_key(_api_key_cua(d["provider"])):
        xoa_su_co()
        return None
    if d["provider"] != _bo_mac_dinh() and time.time() - float(d.get("luc") or 0) > SU_CO_BO_KHAC_GIU:
        #  Sự cố của bộ AI KHÔNG còn là mặc định (người dùng đã chuyển sang bộ khác) mà cứ nhắc lại mỗi 30 phút thì rất phiền
        #  (góp ý 07-10-2026). Chỉ giữ vài phút để lỗi vừa xảy ra khi chọn bộ đó ở một lô vẫn được thấy một lần, rồi tự xoá.
        xoa_su_co()
        return None
    da_xem = float(d.get("da_xem_luc") or 0)
    d["hien"] = (not da_xem or d.get("da_xem_loai") != d.get("loai")
                 or time.time() - da_xem >= SU_CO_NHAC_LAI_SAU)
    return d


def da_xem_su_co() -> None:
    try:
        p = _su_co_path()
        if not os.path.exists(p):
            return
        with _SU_CO_KHOA:
            d = _doc_tep_su_co(p)
            if not d:
                return
            d["da_xem_luc"] = time.time()
            d["da_xem_loai"] = d.get("loai")
            _ghi_tep_su_co(p, d)
    except Exception:  # noqa: BLE001
        pass


def _chat(messages, model, timeout=180, max_retries=3, max_tokens=8192,
          temperature=1.3, suy_nghi: str | None = None, json_mode: bool = False) -> str:
    """Gọi 1 lần chat completion (đồng bộ, chặn). Retry lỗi mạng/5xx/429.

    ``model``: tên trơn (DeepSeek) hoặc ``"<provider>:<model>"`` (xem ``ai_engines.tach_model``) — hàm này là
    cửa DUY NHẤT mọi đường gọi AI đi qua, nên bộ AI thứ hai (Gemini, đợt 1) nối vào đây.
    ``suy_nghi``: mức "suy nghĩ" của model cho tác vụ (``"none"`` dịch / định dạng, ``"low"`` viết kịch bản);
    DeepSeek V4 luôn tắt thinking (bên dưới) nên bỏ qua tham số này.
    ``json_mode``: nơi gọi chỉ đọc JSON (soạn bài YouTube) → Gemini ép ``response_format`` json_object (JSON luôn hợp lệ về cú pháp;
    đo 07-10-2026 flash-lite có khi bọc object trong mảng — nơi đọc tự bóc). DeepSeek bỏ qua: request giữ nguyên từng byte (golden).
    """
    provider, model = tach_model(model)
    if provider == GEMINI:
        from services import gemini_service

        return gemini_service.goi(messages, model, timeout=timeout, max_retries=max_retries, max_tokens=max_tokens,
                                  temperature=temperature, suy_nghi=suy_nghi, json_mode=json_mode)
    if provider != DEEPSEEK:
        raise DeepSeekError(L(f"Bộ AI '{provider}' chưa được hỗ trợ ở bản này — chọn lại model trong Cài đặt.", f"The AI provider '{provider}' is not supported in this version — pick the model again in Settings."))
    cfg = _cfg()
    api_key = cfg.get("api_key")
    if not api_key:
        raise RuntimeError(L("Chưa cấu hình API key DeepSeek (vào Cài đặt để nhập).", "The DeepSeek API key is not configured (go to Settings to enter it)."))

    #  LUOI AN TOAN CUOI CUNG. Model rong thi DeepSeek tra 400 cho TUNG LO, ma mot lan
    #  tao kich ban chay hang chuc lo — nguoi dung nhan hang chuc dong loi tieng Anh
    #  giong het nhau roi moi thay cau "that bai" o cuoi. Duong chay theo lo tung de lot
    #  loi nay qua nhieu ban phat hanh ma khong ai thay. Lay lai dung model mac dinh ma
    #  nguoi dung da chon trong Cai dat (y het duong trinh edit chi tiet).
    model = str(model or "").strip() or str(cfg.get("default_model") or "").strip()
    if not model:
        raise DeepSeekError(
            L("Chưa chọn model DeepSeek. Vào Cài đặt → DeepSeek chọn model rồi lưu lại.", "No DeepSeek model chosen. Go to Settings → DeepSeek, pick a model and save.")
        )

    url = f"{cfg['base_url']}/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        # DeepSeek khuyến nghị temperature 1.3 cho dịch (chỉ áp dụng ở non-thinking);
        # bước đọc ngữ cảnh (phân tích) dùng thấp hơn — xem review_advanced_ai.
        "temperature": temperature,
        # Cap output để tránh cắt cụt giữa chừng (V4 cho tối đa 384K → 100 đoạn thừa sức).
        "max_tokens": max_tokens,
    }
    # Model V4 MẶC ĐỊNH bật "thinking" (chậm + tốn hơn, và BỎ QUA temperature). Với tác
    # vụ dịch/định dạng cứng, TẮT thinking để nhanh + rẻ + temperature có hiệu lực.
    if "v4" in (model or "").lower():
        body["thinking"] = {"type": "disabled"}
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "SubForge-Desktop-Client",
    }

    last_err = None
    last_user_msg = None
    last_status = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            #  Gọi được = key sống, còn tiền -> sự cố cũ (nếu có) của DeepSeek đã được xử lý.
            if os.path.exists(_su_co_path()):
                xoa_su_co(giu_da_xem=True, provider=DEEPSEEK)
            return (data["choices"][0]["message"]["content"] or "")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "ignore")[:500]
            except Exception:
                pass
            last_status = e.code
            last_user_msg = _friendly_api_error(e.code, err_body)
            last_err = f"HTTP {e.code} {e.reason}: {err_body}"
            # 4xx (trừ 429 rate-limit) là lỗi cấu hình/số dư/key -> KHÔNG retry,
            # báo ngay message thân thiện để người dùng biết cách xử lý.
            if e.code != 429 and 400 <= e.code < 500:
                ghi_su_co(e.code, last_user_msg)
                raise DeepSeekError(last_user_msg, status=e.code, detail=last_err)
        except DeepSeekError:
            raise
        except Exception as e:
            last_err = str(e)
            last_user_msg = L(f"Không gọi được DeepSeek (mạng/timeout): {e}", f"Could not call DeepSeek (network/timeout): {e}")
        if attempt < max_retries:
            time.sleep(1.5 * attempt)
    raise DeepSeekError(
        last_user_msg or L(f"DeepSeek API lỗi: {last_err}", f"DeepSeek API error: {last_err}"),
        status=last_status,
        detail=last_err or "",
    )


# ============================================================ LỒNG TIẾNG (dịch)
def _build_dubbing_prompt(segments, source_lang, target_lang, style=None, voice=None):
    source_name = LANG_NAMES.get(source_lang, source_lang)
    target_name = LANG_NAMES.get(target_lang, target_lang)
    cps = CPS_BY_LANG.get(target_lang, 14)
    style_directive = resolve_style_directive(style)
    voice_desc = voice_gender_desc(voice)
    extra_rules = ""
    if style_directive:
        extra_rules += (
            f"9. SPEAKING STYLE/GENRE (chosen by the user): {style_directive}. "
            "Apply this style to word choice, pronouns and tone in every line, "
            "while still obeying the character limits above.\n"
        )
    if voice_desc:
        extra_rules += (
            f"{'10' if style_directive else '9'}. The translated lines will be "
            f"read by one AI voice: {voice_desc}. Pick pronouns and phrasing "
            "consistent with that narrator voice.\n"
        )

    #  TRẦN KÝ TỰ — cài đặt CHUNG của app, cùng một mức với chế độ chi tiết và chạy
    #  lô. Đọc mỗi lần dựng prompt để đổi mức là ăn ngay. LUÔN có trần, chỉ khác chặt
    #  (1.0), vừa (1.2) hay rộng (1.4): bỏ hẳn trần thì AI viết dài không kiểm soát.
    try:
        from services.config_service import get_config_service

        he_so = get_config_service().get_translate_limit_factor()
    except Exception:  # noqa: BLE001 — không đọc được cấu hình thì giữ nếp cũ
        he_so = 1.0
    tran_chat = he_so <= 1.01

    system = (
        "You are a professional DUBBING script translator (not just subtitles).\n"
        f"Task: Translate the following dialogue lines from {source_name} into "
        f"{target_name} for AI voice dubbing.\n\n"
        "Strict Instructions:\n"
        "1. Output ONLY the translated text, one line per input line.\n"
        "2. You MUST preserve the [ID] bracket at the start of each line (e.g., [123]). "
        "Do NOT repeat the (duration/limit) part in your output.\n"
        "3. Do not include timecodes, headers, or any other explanations.\n"
        "4. Maintain the emotional tone and context of the original dialogue.\n"
        + (
            "5. TIME BUDGET — MOST IMPORTANT RULE: each line shows its spoken duration and a "
            "hard character limit, e.g. (3.2s | max 48 chars). Your translation MUST NOT exceed "
            "that character limit (spaces included). The dubbed voice has to fit in that time "
            "window; longer text forces the AI voice to speed up and sound rushed.\n"
            "6. If a faithful translation would be too long, REWRITE it more concisely in natural "
            f"SPOKEN {target_name} (drop filler words, redundant subjects, and repetition) while "
            "keeping the core meaning. Never cut a sentence off mid-way and never leave a line "
            "untranslated.\n"
            if tran_chat
            else
            "5. TIME BUDGET — MANDATORY: each line shows its spoken duration and a hard "
            "character limit, e.g. (3.2s | max 67 chars). This limit is already generous so "
            "you can convey the FULL meaning — use that room instead of cutting content, but "
            "NEVER exceed it. Going over makes the dubbed voice rush or get cut off.\n"
            "6. Prefer natural spoken phrasing; never leave a line untranslated or cut off "
            "mid-sentence.\n"
        )
        + 
        "7. Prefer short, natural spoken phrasing over literal written-style translation.\n"
        "8. Crucial: Translate each line independently based on its own unique source text. Never copy or repeat the translation of one line to another line unless their original source texts are identical. Even if the source text is short, confusing, or contains typos, you must translate it as accurately as possible based on its own context, rather than duplicating the translation of a neighboring line.\n"
        + extra_rules
    )

    lines = ["--- START OF SUBTITLES ---"]
    for seg in segments:
        dur = max(0.5, float(seg.get("end") or 0) - float(seg.get("start") or 0))
        max_chars = max(10, round(dur * cps * he_so))
        text = (seg.get("text") or "").replace("\n", " ").strip()
        lines.append(f"[{seg['id']}] ({dur:.1f}s | max {max_chars} chars) {text}")
    lines.append("--- END OF SUBTITLES ---")
    return system, "\n".join(lines)


#: model lỡ chép lại thẻ "[1] " lần nữa trước câu dịch (Gemini 3.5-flash-lite, đo 07-10-2026: "[1] (1.8s | max 27 chars) [1] Trời mưa…"
#: → câu lồng tiếng dính "[1] " và giọng đọc cả dấu ngoặc). Câu dịch thật không bao giờ bắt đầu bằng "[số] ".
_DUP_ID_RE = re.compile(r"^\[\d+\]\s*")


def _parse_id_lines(content):
    """Parse các dòng `[ID] text` -> {id: text}."""
    out = {}
    for raw in (content or "").splitlines():
        m = _ID_LINE_RE.match(raw)
        if not m:
            continue
        seg_id = int(m.group(1))
        text = m.group(2).strip()
        # Bỏ phần "(Xs | max N chars)" và thẻ "[ID]" nếu model lỡ lặp lại (theo bất kỳ thứ tự nào).
        for _ in range(2):
            text = _PAREN_PREFIX_RE.sub("", text).strip()
            text = _DUP_ID_RE.sub("", text).strip()
        if text:
            out[seg_id] = text
    return out


async def _google_fallback(segments, source_lang, target_lang, should_stop):
    """Dịch danh sách segment bằng Google (deep_translator) — dùng khi DeepSeek lỗi.

    Được gọi MỘT LẦN cho toàn bộ đoạn lỗi SAU khi các lô DeepSeek chạy xong (không
    gọi trong từng lô nữa) — giữ đúng trần 10 request đồng thời + circuit-breaker
    429 của TranslationService. Bị 429 giữa chừng vẫn CỨU phần đã dịch được
    (partial_results). Trả {id: text} cho các đoạn dịch được."""
    from services.translation_service import TranslationService, TranslationRateLimitError

    ts = TranslationService()
    texts = [(s.get("text") or "") for s in segments]
    try:
        translated = await ts.batch_translate(
            texts, src=source_lang, dest=target_lang, should_stop=should_stop
        )
    except TranslationRateLimitError as e:
        logger.warning(
            L("Google fallback bị giới hạn tần suất (429) — giữ phần đã dịch được.", "Google fallback is rate-limited (429) — keeping what was already translated.")
        )
        translated = list(e.partial_results or [])
        translated += [None] * (len(texts) - len(translated))
    except Exception as e:
        logger.error(L(f"Google fallback cũng lỗi: {e}", f"The Google fallback also failed: {e}"))
        return {}
    out = {}
    for s, t in zip(segments, translated):
        if t and not any(tag in t for tag in ["[RateLimit:", "[Stopped:", "[Error:"]):
            out[s["id"]] = t
    return out


async def translate_dubbing_segments(
    segments, source_lang, target_lang, model, should_stop=None, on_progress=None,
    style=None, voice=None, batch_size=None,
):
    """Dịch lồng tiếng bằng DeepSeek — chia lô (``batch_size``, mặc định
    DUBBING_BATCH_SIZE), gửi NHIỀU LÔ SONG SONG.

    ``batch_size``: số câu mỗi lô người dùng chọn ở Cài đặt → DeepSeek (``ai_batch_size``,
    dùng chung với Chi tiết / chạy lô). Trần token + thời gian chờ nới theo cỡ lô (như
    ``review_sub_tracks``) để lô 200 câu không chạm trần 8192 rồi cụt giữa chừng.

    Giống cơ chế của Google Translate: nhiều request chạy đồng thời (giới hạn bởi
    DEEPSEEK_CONCURRENCY) rồi gộp kết quả, thay vì chờ tuần tự từng lô.

    segments: list [{id, text, start, end}] (giữ thứ tự đầu vào).
    Trả về (results_map, fallback_ids, error_ids):
      - results_map: {id: translated_text}
      - fallback_ids: các id đã phải chuyển sang Google (DeepSeek lỗi/thiếu)
      - error_ids: các id không dịch được (cả DeepSeek lẫn Google)
    """
    import asyncio

    source_lang = (source_lang or "auto").lower()
    target_lang = (target_lang or "vi").lower()
    results, fallback_ids, error_ids = {}, [], []
    total = len(segments)
    done = 0

    try:
        lo = int(batch_size or DUBBING_BATCH_SIZE)
    except (TypeError, ValueError):
        lo = DUBBING_BATCH_SIZE
    lo = max(20, min(200, lo))
    batches = [
        segments[i:i + lo]
        for i in range(0, total, lo)
    ]
    sem = asyncio.Semaphore(DEEPSEEK_CONCURRENCY)
    logger.info(
        L(f"DeepSeek: dịch {total} đoạn qua {len(batches)} lô x{lo}, "
        f"chạy song song tối đa {DEEPSEEK_CONCURRENCY} lô cùng lúc (model '{model}').", f"DeepSeek: translating {total} segments in {len(batches)} batches x{lo}, running up to {DEEPSEEK_CONCURRENCY} batches in parallel (model '{model}').")
    )

    # Các đoạn DeepSeek lỗi/thiếu được GOM LẠI, fallback Google MỘT LẦN sau khi mọi
    # lô chạy xong — không fallback bên trong từng lô nữa. Lý do: (1) mỗi lô tự tạo
    # fallback riêng sẽ bắn tới 6x10=60 request Google đồng thời -> 429 dây chuyền;
    # (2) fallback trong `async with sem` giữ slot DeepSeek suốt thời gian chạy Google.
    fallback_pending: list = []

    async def _run_batch(lot, batch):
        nonlocal done
        # asyncio 1 luồng: các thao tác dict/list dưới đây không có await xen giữa nên
        # an toàn, không cần lock.
        if should_stop and should_stop():
            return
        async with sem:
            if should_stop and should_stop():
                return
            system, user = _build_dubbing_prompt(
                batch, source_lang, target_lang, style=style, voice=voice
            )
            try:
                content = await asyncio.to_thread(
                    _chat,
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    model,
                    timeout=int(max(180, len(batch) * 3)),
                    max_tokens=int(min(65536, max(8192, len(batch) * 120))),
                )
                parsed = _parse_id_lines(content)
                for s in batch:
                    if s["id"] in parsed:
                        results[s["id"]] = parsed[s["id"]]
                missing = [s for s in batch if s["id"] not in parsed]
                if missing:
                    logger.warning(
                        L(f"DeepSeek thiếu {len(missing)}/{len(batch)} đoạn ở lô {lot} "
                        f"-> sẽ fallback Google sau khi các lô chạy xong.", f"DeepSeek is missing {len(missing)}/{len(batch)} segments in batch {lot} -> will fall back to Google after the batches finish.")
                    )
                    fallback_pending.extend(missing)
            except Exception as e:
                reason = e.user_message if isinstance(e, DeepSeekError) else str(e)
                logger.error(
                    L(f"DeepSeek lô {lot} lỗi ({reason}); TOÀN BỘ lô sẽ fallback sang Google.", f"DeepSeek batch {lot} failed ({reason}); the WHOLE batch will fall back to Google.")
                )
                fallback_pending.extend(batch)

            done += len(batch)
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:
                    pass
            logger.info(L(f"DeepSeek tiến độ dịch: {done}/{total} đoạn (lô {lot} xong).", f"DeepSeek translation progress: {done}/{total} segments (batch {lot} done)."))

    await asyncio.gather(
        *[_run_batch(i + 1, b) for i, b in enumerate(batches)]
    )

    # Fallback Google 1 LẦN cho toàn bộ đoạn lỗi (giữ thứ tự theo index gốc).
    if fallback_pending and not (should_stop and should_stop()):
        fallback_pending.sort(key=lambda s: s["id"])
        logger.warning(
            L(f"Đang dịch {len(fallback_pending)} đoạn DeepSeek lỗi bằng Google Dịch...", f"Translating {len(fallback_pending)} segments that failed on DeepSeek with Google Translate...")
        )
        fb = await _google_fallback(
            fallback_pending, source_lang, target_lang, should_stop
        )
        for s in fallback_pending:
            if s["id"] in fb:
                results[s["id"]] = fb[s["id"]]
                fallback_ids.append(s["id"])
            else:
                error_ids.append(s["id"])
    elif fallback_pending:
        # Bị hủy giữa chừng: các đoạn chưa fallback tính là lỗi (re-run sẽ dịch lại).
        error_ids.extend(s["id"] for s in fallback_pending)

    if fallback_ids:
        logger.warning(
            L(f"Có {len(fallback_ids)} đoạn phải dịch bằng Google (DeepSeek lỗi). "
            f"Đã đánh dấu để cảnh báo trên giao diện.", f"{len(fallback_ids)} segments had to be translated by Google (DeepSeek failed). Marked to warn in the interface.")
        )
    return results, fallback_ids, error_ids


# ============================================================ REVIEW (kịch bản)
def _parse_rate_to_multiplier(rate_str: str) -> float:
    rate_str = (rate_str or "").strip().lower()
    if not rate_str or rate_str == "auto":
        return 1.15
    try:
        val = rate_str.replace("%", "").strip()
        if val.startswith("+"):
            pct = float(val[1:])
            return round(1.0 + pct / 100.0, 2)
        elif val.startswith("-"):
            pct = float(val[1:])
            return round(1.0 - pct / 100.0, 2)
        else:
            pct = float(val)
            return round(1.0 + pct / 100.0, 2)
    except Exception:
        return 1.0


def _build_review_prompt(
    a_batch, is_chinese, smart_shift=False, default_rate="auto",
    style=None, brand=None, voice=None, target_lang="vi",
):
    """Mirror PromptManager.copyPrompt (Storyteller) cho 1 lô A.

    ``target_lang`` MẶC ĐỊNH "vi" để luồng tạo kịch bản tự động (vốn chỉ làm tiếng Việt)
    gọi như cũ không phải sửa gì. Trình dựng nâng cao truyền mã người dùng chọn xuống.
    Trước đây prompt ép cứng chữ "Tiếng Việt" nên chọn thứ tiếng khác vẫn ra tiếng Việt —
    sai âm thầm, chỉ lộ khi đã dán prompt sang ChatGPT và nhận về kịch bản sai ngôn ngữ.
    """
    target_name_vi = TARGET_LANG_VI.get(target_lang, LANG_NAMES.get(target_lang, target_lang))
    brand_name = (brand or "").strip() or DEFAULT_REVIEW_BRAND
    style_directive = resolve_style_directive(style)
    voice_desc = voice_gender_desc(voice)
    style_rules = ""
    if style_directive:
        style_rules += (
            f"- KIỂU NÓI (người dùng chọn): {style_directive} — áp dụng cho từ ngữ, "
            "xưng hô và giọng điệu của TOÀN BỘ lời bình, nhưng vẫn tuân thủ trần độ dài.\n"
        )
    if voice_desc:
        style_rules += (
            f"- GIỌNG ĐỌC: lời bình sẽ được đọc bằng {voice_desc} — chọn ngôi kể, "
            "xưng hô của MC phù hợp với giới tính giọng đọc này.\n"
        )
    a_list_text = "\n".join(
        f"[A{a['a_id']}] ({a['start']:.1f}s → {a['end']:.1f}s | "
        f"{max(0.1, a['end'] - a['start']):.1f}s): {a['text']}"
        for a in a_batch
    )
    total_dur = sum(max(0.1, a["end"] - a["start"]) for a in a_batch)
    total_chars = round(total_dur * CPS_VI_REVIEW)
    # Tốc độ dùng để ước lượng số từ PHẢI KHỚP tốc độ render thực tế:
    #  - Smart shift: giọng phát ở tốc độ CỐ ĐỊNH (auto -> 1.0x), KHÔNG kéo giãn ->
    #    dùng đúng tốc độ cố định đó, nếu không sẽ đọc xong sớm/trễ so với cảnh.
    #  - Thường: dùng tốc độ người dùng chọn (auto -> 1.15x tự khớp).
    if smart_shift:
        fixed_rate = "+0%" if (not (default_rate or "").strip() or default_rate == "auto") else default_rate
        target_speed = _parse_rate_to_multiplier(fixed_rate)
    else:
        target_speed = _parse_rate_to_multiplier(default_rate)
    words_per_sec = (WPM_VI_REVIEW * target_speed) / 60
    target_words = round(total_dur * words_per_sec)

    chinese_note = ""
    if is_chinese:
        chinese_note = (
            "LƯU Ý: Ngôn ngữ gốc là Tiếng Trung (câu thường rất ngắn) — biên tập lời bình "
            f"{target_name_vi} súc tích, sát nghĩa; KHÔNG kéo dài thêm so với nội dung gốc.\n"
        )

    if smart_shift:
        mode_rules = (
            "- CHẾ ĐỘ XÊ DỊCH SRT LINH HOẠT (BẬT): Giọng đọc phát ở TỐC ĐỘ TỰ NHIÊN và "
            "KHÔNG bị kéo giãn/ép nhanh; hệ thống chỉ dồn thời điểm cho khớp. Vì vậy lời "
            "bình phải ĐỦ DÀI để LẤP KÍN thời lượng cảnh — viết quá ngắn thì giọng đọc XONG "
            "SỚM, để lại khoảng lặng dài không khớp hình.\n"
            "- GỘP A → B VỪA PHẢI, GIỚI HẠN THEO THỜI LƯỢNG (RẤT QUAN TRỌNG): gộp các A liên "
            "tiếp thành B, nhưng MỖI B chỉ nên trải khoảng 8–20 GIÂY (tổng thời lượng các A "
            "trong B). TUYỆT ĐỐI KHÔNG gộp thành B trải quá dài (>30s): B càng dài thì càng "
            "khó viết đủ lời lấp kín, sẽ để lại khoảng im lặng lớn. Nếu một chuỗi A liền kề "
            "kéo dài, hãy CHIA THÀNH NHIỀU B (mỗi B ~10-20s) thay vì 1 B khổng lồ.\n"
            "- VIẾT ĐỦ LẤP KÍN từng B: số chữ mỗi B ≈ thời lượng B (giây) × ~2.5 từ/giây. B "
            "trải ~15s thì cần ~35-40 từ. Cảnh DÀI mà ÍT thoại gốc (hành động, chuyển cảnh) "
            "-> MC vẫn phải BÌNH LUẬN/phân tích/dẫn dắt để lấp kín, KHÔNG để trôi qua im lặng."
        )
        target_words_rule = (
            f"NHẮM ~{target_words} TỪ (~{total_chars} ký tự) cho CẢ LÔ — hãy viết ĐỦ để LẤP KÍN "
            f"thời lượng, khoảng 95–108% mục tiêu. TUYỆT ĐỐI KHÔNG viết dưới ~90% (sẽ đọc xong "
            f"sớm, hụt so với cảnh); cũng đừng vượt quá ~110% (tránh tràn sang cảnh sau). "
            f"Nếu nội dung gốc ít ý mà thời lượng dài, hãy bình luận/diễn giải sâu thêm cho đủ độ dài."
        )
    else:
        mode_rules = (
            f"- GIỚI HẠN KÝ TỰ TỪNG ĐOẠN B (RÀNG BUỘC CỨNG): max_ký_tự = tổng thời lượng các A "
            f"tham chiếu (giây) × {CPS_VI_REVIEW} (tính cả dấu cách). Vượt trần sẽ làm giọng đọc bị tua "
            f"nhanh/cắt cụt. Nếu bản viết tự nhiên dài hơn, VIẾT LẠI ngắn gọn hơn.\n"
            "- Giữ khoảng lặng tự nhiên: giữa hai cụm thoại có khoảng trống dài thì tách thành hai "
            "đoạn B riêng (đừng gộp xuyên silence lớn)."
        )
        target_words_rule = (
            f"Khoảng {target_words} TỪ (~{total_chars} ký tự, ±5%); ràng buộc quan trọng hơn là "
            f"giới hạn ký tự TỪNG ĐOẠN B ở trên."
        )

    return f"""Bạn là "Storyteller" cho kênh "{brand_name}" — bình luận sắc sảo, hài hước, cuốn hút.
Dưới đây là danh sách đoạn A (raw STT, 1 câu Whisper = 1 A). Nhiệm vụ: VIẾT LẠI thành lời bình review (đoạn B) BẰNG {target_name_vi.upper()}, giữ 100% thời lượng video gốc.
{chinese_note}{style_rules}
QUY TẮC (BẮT BUỘC):
- GHÉP A → B: Mỗi B tham chiếu các đoạn A LIÊN TIẾP qua source_a_ids. ƯU TIÊN GỘP CÁC MỐC TIME LIỀN NHAU THÀNH ĐOẠN B LỚN (vd gộp 10-20+ A liên tiếp như [1..15] thành 1 B). Mọi a_id phải xuất hiện ĐÚNG MỘT LẦN trong toàn bộ source_a_ids — không bỏ sót, không trùng, thứ tự tăng dần.
- KHÔNG BỎ SÓT NỘI DUNG (RẤT QUAN TRỌNG): Lời bình của mỗi B phải phản ánh diễn biến/nội dung của TẤT CẢ các A mà nó gộp — không được chỉ tóm tắt vài A rồi lược bỏ phần còn lại. Nếu B gộp nhiều A nhưng bạn chỉ viết vài câu ngắn, đoạn giữa/cuối của khoảng đó sẽ KHÔNG có lời -> video bị TRỐNG TIẾNG ngay chỗ đang có cảnh. Hãy viết đủ ý và đủ dài để trải kín khoảng thời gian B phủ.
- THOẠI NHÂN VẬT PHẢI ĐƯỢC KỂ LẠI, KHÔNG ĐƯỢC BỎ (hay gặp lỗi này): khi trong các A có câu thoại của nhân vật, TUYỆT ĐỐI không lược bỏ chúng khỏi lời bình. Chuyển thành LỜI KỂ GIÁN TIẾP của MC, giữ đúng diễn biến + cảm xúc (vd "Tôi không đi!" → "Lúc này cô gái nhất quyết từ chối đi"). Cả video là 1 giọng MC duy nhất — đừng để những cảnh có thoại trôi qua mà lời review im lặng.
- VIẾT LẠI, không dịch máy/chép nguyên văn: lời bình sống động, đúng ý nội dung gốc, từ ngữ "đắt", đan xen câu ngắn/dài.
- Xác định ĐÚNG GIỚI TÍNH nhân vật qua xưng hô/tên/ngữ cảnh — không dịch sai đại từ.
- KHÔNG tự thêm câu chào kênh/kêu gọi đăng ký nếu đoạn A gốc không có.
- Tên riêng nước ngoài: phiên âm cho AI đọc mượt ("Greenland" → "Grin-len"), KHÔNG chú thích tên gốc trong ngoặc.
- Mỗi đoạn B là câu hoàn chỉnh, kết thúc bằng DẤU CHẤM — không ngắt lửng câu qua 2 đoạn.
{mode_rules}

ĐỘ DÀI (CHO LÔ NÀY — tổng {total_dur:.1f}s, tốc độ đọc {target_speed:.2f}x):
{target_words_rule}

ĐỊNH DẠNG OUTPUT: CHỈ một JSON Array, mỗi phần tử gồm đúng 3 trường b_id, b_text, source_a_ids. Không giải thích, không code fence.
Ví dụ: [{{"b_id": 1, "b_text": "Lời bình...", "source_a_ids": [1, 2, 3]}}]

DỮ LIỆU GỐC (LIST A):
{a_list_text}"""


def _coerce_list(data):
    """Chấp nhận cả mảng thuần lẫn object bọc mảng (vd {"segments":[...]})."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return None


def _parse_json_array(content):
    """Trích JSON array từ phản hồi (chịu được code fence / object bọc / chữ thừa)."""
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        got = _coerce_list(json.loads(s))
        if got is not None:
            return got
    except Exception:
        pass
    # Vớt JSON giữa chữ giải thích: KHÔNG dùng regex tham lam [.*] (chữ mở đầu có
    # ngoặc vuông kiểu "[Lưu ý] ..." sẽ phá cả lô). Quét từng vị trí '['/'{' và
    # thử parse thật bằng raw_decode — lấy mảng chunk hợp lệ đầu tiên.
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", s):
        try:
            val, _end = decoder.raw_decode(s, m.start())
        except ValueError:
            continue
        got = _coerce_list(val)
        if got is not None and any(isinstance(x, dict) for x in got):
            return got
    raise RuntimeError(L("Không parse được JSON kịch bản từ phản hồi AI.", "Could not parse the script JSON from the AI response."))


def generate_review_bchunks(a_chunks, meta, model, should_stop=None, on_progress=None):
    """Tạo B-chunks (kịch bản review) từ A-chunks bằng DeepSeek, theo lô 100 đoạn A.

    a_chunks: list [{a_id, start, end, text, ...}]. b_id được đánh số LIÊN TỤC qua
    các lô. source_a_ids chỉ trong phạm vi từng lô (không ghép xuyên ranh giới lô).
    Trả về list [{b_id, b_text, source_a_ids}]. Raise nếu không có đoạn B hợp lệ.
    """
    import concurrent.futures as cf

    source_lang = (meta.get("source_language") or "").lower()
    is_chinese = source_lang in ("zh-cn", "zh")
    smart_shift = bool(meta.get("smart_srt_shift", True))
    default_rate = meta.get("default_rate") or "auto"
    speech_style = meta.get("speech_style") or ""
    brand_name = meta.get("brand_name") or ""
    target_voice = meta.get("target_voice") or ""
    total = len(a_chunks)
    batches = [
        a_chunks[i:i + REVIEW_BATCH_SIZE]
        for i in range(0, total, REVIEW_BATCH_SIZE)
    ]
    logger.info(
        L(f"Đang tạo kịch bản từ {total} câu, chia {len(batches)} lô (mỗi lô {REVIEW_BATCH_SIZE} câu), "
        f"chạy song song tối đa {DEEPSEEK_CONCURRENCY} lô (AI DeepSeek, model '{model}').", f"Generating the script from {total} sentences, split into {len(batches)} batches ({REVIEW_BATCH_SIZE} sentences each), running up to {DEEPSEEK_CONCURRENCY} batches in parallel (DeepSeek AI, model '{model}').")
    )

    # Giữ lỗi API gặp phải để nếu KHÔNG sinh được đoạn B nào thì báo ĐÚNG nguyên nhân
    # (hết số dư / sai key / quá tải...) thay vì thông báo chung chung.
    api_errors: list = []

    def _run_batch(lot, batch):
        """Trả về list chunk của lô, hoặc None nếu lô lỗi/parse hỏng (BỎ QUA, không
        làm hỏng cả tiến trình — các lô còn lại vẫn dùng được)."""
        if should_stop and should_stop():
            return None
        try:
            content = _chat(
                [{"role": "user", "content": _build_review_prompt(
                    batch, is_chinese, smart_shift, default_rate,
                    style=speech_style, brand=brand_name, voice=target_voice,
                )}],
                model,
                # Lô 50 đoạn A -> output ~3-5K token; 8192 đủ dư và chặn model viết tràn.
                max_tokens=8192,
            )
            return _parse_json_array(content)
        except DeepSeekError as e:
            api_errors.append(e)
            logger.error(L(f"Tạo kịch bản lô {lot} lỗi: {e.user_message}", f"Script batch {lot} failed: {e.user_message}"))
            return None
        except Exception as e:
            logger.error(
                L(f"Tạo kịch bản lô {lot} lỗi (bỏ qua lô này): {e}", f"Script batch {lot} failed (skipping this batch): {e}")
            )
            return None

    # Chạy song song, nhưng GỘP theo THỨ TỰ lô để b_id + dòng thời gian đúng.
    results_per_batch = [None] * len(batches)
    done = 0
    with cf.ThreadPoolExecutor(max_workers=DEEPSEEK_CONCURRENCY) as pool:
        fut_to_idx = {
            pool.submit(_run_batch, i + 1, b): i for i, b in enumerate(batches)
        }
        for fut in cf.as_completed(fut_to_idx):
            idx = fut_to_idx[fut]
            results_per_batch[idx] = fut.result()
            done += len(batches[idx])
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:
                    pass
            logger.info(L(f"Tiến độ tạo kịch bản: {done}/{total} câu (lô {idx + 1} xong).", f"Script progress: {done}/{total} sentences (batch {idx + 1} done)."))

    all_b = []
    b_offset = 0
    skipped_batches = 0
    for chunks in results_per_batch:
        if not chunks:
            skipped_batches += 1
            continue
        for c in chunks:
            if not isinstance(c, dict):
                continue
            b_text = (c.get("b_text") or "").strip()
            src_ids = c.get("source_a_ids") or []
            if not b_text or not src_ids:
                continue
            try:
                src_ids = [int(x) for x in src_ids]
            except (TypeError, ValueError):
                continue
            b_offset += 1
            all_b.append({
                "b_id": b_offset,
                "b_text": b_text,
                "source_a_ids": src_ids,
            })

    if skipped_batches:
        logger.warning(
            L(f"Có {skipped_batches}/{len(batches)} lô bị bỏ qua do lỗi/parse hỏng "
            f"(kịch bản có thể thiếu 1 phần).", f"{skipped_batches}/{len(batches)} batches were skipped due to errors/parse failures (the script may be missing a part).")
        )
    if not all_b:
        # Ưu tiên báo ĐÚNG lỗi API (hết số dư / sai key / quá tải...) nếu có.
        if api_errors:
            raise api_errors[0]
        raise DeepSeekError(
            L("DeepSeek không trả về đoạn kịch bản hợp lệ nào "
            "(phản hồi sai định dạng). Thử lại hoặc đổi model.", "DeepSeek did not return any valid script segment (malformed response). Try again or change the model.")
        )
    logger.info(L(f"Đã tạo kịch bản: tổng {len(all_b)} câu hợp lệ.", f"Script generated: {len(all_b)} valid sentences in total."))
    return all_b
