# -*- coding: utf-8 -*-
"""GEMINI (Google AI Studio) — bộ AI thứ hai bên cạnh DeepSeek (07-10-2026).

Gọi qua TẦNG TƯƠNG THÍCH OPENAI của Gemini (``…/v1beta/openai/chat/completions``, Bearer = API key
AI Studio): cùng dạng request / response với DeepSeek nên mọi prompt và bộ đọc kết quả hiện có
(``_parse_id_lines``, ``_parse_json_array``…) dùng lại nguyên. Chỉ ``deepseek_service._chat`` rẽ
nhánh sang đây theo ``ai_engines.tach_model``. Đây là tệp DUY NHẤT biết Gemini khác DeepSeek ở đâu:

- KHÔNG gửi trường ``thinking`` (riêng của DeepSeek — Gemini trả 400 "Unknown name thinking").
- ``reasoning_effort``: Gemini "suy nghĩ" MẶC ĐỊNH (đo 07-10-2026: lô 6 câu 977 token tổng trong khi
  prompt + trả lời ~260; ``"none"`` → 249 token, 1,6 s thay vì 3,9 s). Token suy nghĩ tính tiền như token
  đầu ra. Tác vụ dịch / định dạng dùng ``"none"``, viết kịch bản dùng ``"low"``; model nào từ chối mức
  đó (400) thì tự lùi về ``"low"`` và nhớ trong phiên.
- Lỗi: khoá sai trả **400 "Invalid Auth key"** (không phải 401 như DeepSeek); gói miễn phí 5 lượt/phút
  trả 429 kèm "retry in Ns" → CHỜ đúng chừng đó rồi gọi lại (trần ``TRAN_CHO_429``), không rơi về
  Google Dịch ngay; 503 "high demand" rất hay gặp → lùi theo cấp số; model đã đóng với người dùng mới
  trả 404 kèm tên model thay thế → nói đúng tên đó.
- Sự cố (khoá sai / hết hạn mức / không có quyền) ghi chung tệp với DeepSeek
  (``deepseek_service.ghi_su_co``, có trường ``provider``) để popup toàn cục hiện đúng bộ AI.

Dùng ``urllib`` stdlib như DeepSeek (không thêm dependency vào bản PyInstaller).
"""
from __future__ import annotations

from utils.i18n import L
import json
import logging
import re
import time
import urllib.error
import urllib.request

from services.ai_engines import GEMINI

logger = logging.getLogger("GeminiService")

BASE_URL_MAC_DINH = "https://generativelanguage.googleapis.com/v1beta/openai"
#: Model CHỮ ổn định (trang Models của AI Studio, 07-10-2026) — thứ tự = thứ tự hiện ở giao diện, phần tử đầu = mặc định.
#: HẠN MỨC key gói MIỄN PHÍ / Google AI Pro (bảng aistudio.google.com/rate-limit của người dùng, 07-10-2026 — họ dùng gói Pro, CHƯA bật
#: thanh toán API; không model nào "không giới hạn" ở tầng này): dòng Flash (3.5 / 3.6 / 3.7 / 3.8) chỉ **5 lượt/phút · 20 lượt/NGÀY**;
#: dòng Flash-Lite (3.5 / 3.1) **15 lượt/phút · 500 lượt/ngày**; đều 250K token/phút. Lô chạy qua đêm với 20 lượt/ngày là chết sau
#: 4 đợt (5 lô song song), nên Flash-Lite đứng đầu làm mặc định; Flash để chọn tay cho việc ít lượt mà cần chất lượng (kịch bản Sáng tạo).
#: NGƯỜI MUA API RIÊNG (bật thanh toán, Tier 1+) có hạn mức cao hơn nhiều → mã KHÔNG ĐƯỢC chặn / tự giới hạn gì theo bảng này
#: (người dùng dặn 07-10-2026): bảng chỉ để chọn mặc định + chú thích; mọi 429 đều đọc "retry in" thật của Google.
#: Dòng 2.5 đã đóng với người dùng mới; bản preview giới hạn chặt và có thể ngừng sau 2 tuần báo trước nên không khai sẵn —
#: nhưng ô chọn model vẫn liệt kê THÊM mọi model chữ mà khoá của người dùng thấy (``liet_ke_model`` + ``la_model_chu``).
MODELS_MAC_DINH = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
#: chú thích ngắn hiện cạnh tên model (mirror ``aiEngines.ts`` GOI_Y_MODEL_GEMINI)
GOI_Y_MODEL = {
    "gemini-3.5-flash-lite": "mặc định — gói miễn phí / Pro được nhiều lượt nhất (15/phút · 500/ngày), hợp dịch hàng loạt",
    "gemini-3.1-flash-lite": "rẻ nhất, hạn mức rộng như 3.5 Lite",
    "gemini-3.8-flash": "mạnh nhất — gói miễn phí / Pro chỉ 5/phút · 20/ngày (hợp kịch bản Sáng tạo); mua API riêng thì thoải mái",
    "gemini-3.7-flash": "thế hệ trước, ổn định (miễn phí / Pro: 5/phút · 20/ngày)",
    "gemini-3.6-flash": "thế hệ trước, ổn định (miễn phí / Pro: 5/phút · 20/ngày)",
    "gemini-3.5-flash": "cũ hơn, ổn định (miễn phí / Pro: 5/phút · 20/ngày)",
}
#: model KHÔNG phải model chữ (TTS / Live / ảnh / video / nhạc / chép lời / embedding / robot / agent…) — không hiện ở ô chọn model dịch
_KHONG_PHAI_CHU = ("tts", "live", "image", "banana", "veo", "lyria", "transcribe", "embedding", "robotics", "computer-use",
                   "omni", "deep-research", "antigravity", "imagen")


def la_model_chu(ten: str) -> bool:
    """Model dùng được cho dịch / viết kịch bản (sinh chữ): tên bắt đầu ``gemini-`` / ``gemma-`` và không thuộc nhóm TTS, Live, ảnh, video, nhạc…"""
    t = (ten or "").lower()
    return (t.startswith("gemini-") or t.startswith("gemma-")) and not any(x in t for x in _KHONG_PHAI_CHU)
#: mức "suy nghĩ" khi nơi gọi không nói gì (dịch / định dạng)
SUY_NGHI_MAC_DINH = "none"
SUY_NGHI_HOP_LE = ("none", "minimal", "low", "medium", "high")
#: tổng thời gian tối đa (giây) CHỜ vì 429 trong MỘT lượt gọi — quá thì báo lỗi để nơi gọi fallback
TRAN_CHO_429 = 120.0
#: 503 "high demand": lùi 1,5 s × lần, không quá chừng này lần (ngoài ``max_retries`` chung)
LAN_THU_503 = 4

#: model đã báo không nhận ``reasoning_effort`` mức thấp → lần sau dùng "low" luôn (nhớ trong phiên). Gieo sẵn theo đo thật
#: 07-10-2026: ``gemini-3.5-flash-lite`` trả 400 "Request contains an invalid argument." (KHÔNG nói vì sao) với "none"; "low" / bỏ
#: hẳn thì chạy, cùng 49 token — còn 3.1-flash-lite và dòng Flash nhận "none". Gieo sẵn để khỏi tốn 1 lượt lỗi mỗi phiên.
#: Mức "bo" = KHÔNG gửi ``reasoning_effort`` — Gemma (``gemma-4-31b-it``, log người dùng 07-10-2026) trả 400 "Thinking level is not
#: supported for this model." với MỌI mức, kể cả "low". Thang lùi: mức yêu cầu → "low" → bỏ hẳn; vẫn 400 thì báo lỗi.
_khong_nhan_muc_goc: dict[str, str] = {"gemini-3.5-flash-lite": "low"}
_khong_nhan_muc: dict[str, str] = dict(_khong_nhan_muc_goc)
BO_SUY_NGHI = "bo"


def muc_suy_nghi_nho(model: str) -> str | None:
    """Mức đã nhớ cho model (``"low"`` / ``"bo"``) — Gemma luôn "bo" (không có chế độ suy nghĩ)."""
    if model in _khong_nhan_muc:
        return _khong_nhan_muc[model]
    if str(model).lower().startswith("gemma-"):
        return BO_SUY_NGHI
    return None
_RE_RETRY = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.I)
_RE_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', re.I)
_RE_MODEL_THAY = re.compile(r"use models/([A-Za-z0-9._-]+)")


def _cfg() -> dict:
    from services.config_service import get_config_service

    return get_config_service().get_gemini_config()


def is_configured() -> bool:
    try:
        return bool(_cfg().get("api_key"))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ lỗi
def _doc_loi(body: str) -> tuple[str, str]:
    """(message, status) từ body lỗi của Gemini — có lúc bọc trong MẢNG ``[{"error": …}]``."""
    try:
        j = json.loads(body)
        if isinstance(j, list) and j:
            j = j[0]
        e = (j or {}).get("error") if isinstance(j, dict) else None
        if isinstance(e, dict):
            return str(e.get("message") or ""), str(e.get("status") or e.get("type") or "")
        if isinstance(e, str):
            return e, ""
    except (ValueError, AttributeError):
        pass
    return (body or "").strip()[:300], ""


def giay_cho_lai(msg: str, body: str = "") -> float | None:
    """Số giây Gemini bảo đợi ("retry in 59s" / ``retryDelay: "59s"``) — ``None`` nếu không nói."""
    for s in (msg or "", body or ""):
        m = _RE_RETRY.search(s) or _RE_RETRY_DELAY.search(s)
        if m:
            return float(m.group(1))
    return None


def phan_loai_loi(status: int, body: str, model: str = "") -> tuple[str, str | None, str]:
    """Lỗi HTTP của Gemini → (thông báo tiếng Việt, loại sự cố cho popup hoặc None, cách xử lý).
    Cách xử lý: ``"dung"`` (báo ngay) · ``"cho_429"`` (đợi rồi gọi lại) · ``"thu_lai"`` (lùi rồi gọi lại) ·
    ``"ha_suy_nghi"`` (model không nhận mức reasoning_effort → gọi lại với "low")."""
    msg, st = _doc_loi(body)
    low = f"{msg} {st}".lower()
    if status == 400 and ("reasoning_effort" in low or "thinking" in low) and ("not supported" in low or "invalid" in low or "unknown" in low):
        return msg, None, "ha_suy_nghi"
    if status == 400 and "invalid argument" in low and "auth" not in low and "api key" not in low:
        #  400 chung chung "Request contains an invalid argument." — 3.5-flash-lite trả thế này khi reasoning_effort="none" (đo
        #  07-10-2026). Nơi gọi thử lại với "low" MỘT lần (chỉ khi mức hiện tại chưa phải "low"); vẫn 400 thì báo lỗi như thường.
        return L(f"Gemini báo lỗi: {msg}", f"Gemini reported an error: {msg}"), None, "ha_suy_nghi"
    if status in (400, 401) and ("invalid auth key" in low or "api key not valid" in low or "api_key_invalid" in low or "unauthenticated" in low or status == 401):
        return (L("API key Gemini KHÔNG HỢP LỆ hoặc đã bị thu hồi. Kiểm tra lại key trong mục Cài đặt "
                "(aistudio.google.com/api-keys).", "The Gemini API key is INVALID or has been revoked. Check the key in Settings (aistudio.google.com/api-keys).")), "sai_key", "dung"
    if status == 403 or "permission_denied" in low:
        return (L("Key Gemini không có quyền dùng model này / API chưa được bật cho dự án Google của bạn "
                f"({msg[:160] or 'PERMISSION_DENIED'}).", f"The Gemini key is not allowed to use this model / the API is not enabled for your Google project ({msg[:160] or 'PERMISSION_DENIED'}).")), "khong_quyen", "dung"
    if status == 429:
        #  Chỉ coi là HẾT HẠN MỨC khi Google nói rõ (theo ngày / quota + billing); mọi 429 khác là tạm thời → đợi theo
        #  "retry in" (không có thì 10 s) rồi gọi lại — người mua API riêng (hạn mức cao) không bị popup oan.
        #  Trường hợp "retry in" dài hàng giờ (hạn mức ngày cạn mà câu lỗi không ghi "per day") xử lý ở ``goi``.
        het_ngay = "per day" in low or "daily" in low or ("quota" in low and "billing" in low)
        if not het_ngay:
            return (L(f"Gemini đang bị giới hạn tần suất (429 — vượt số lượt/phút của tài khoản bạn) — "
                    f"{msg[:160] or 'đợi rồi thử lại'}", f"Gemini is rate-limited (429 — over your account's per-minute quota) — {msg[:160] or 'wait and try again'}")), None, "cho_429"
        return (L("Gemini đã HẾT HẠN MỨC (429 RESOURCE_EXHAUSTED) — hết lượt trong ngày của gói miễn phí / Pro, hoặc dự án "
                "chưa bật thanh toán API. Bật thanh toán tại aistudio.google.com (Set up billing) để hết giới hạn, đổi model Lite, "
                f"hoặc đợi hạn mức được đặt lại. ({msg[:160]})", f"Gemini QUOTA EXHAUSTED (429 RESOURCE_EXHAUSTED) — the daily quota of the free / Pro plan is used up, or the project has no API billing enabled. Enable billing at aistudio.google.com (Set up billing) to lift the limit, switch to a Lite model, or wait for the quota to reset. ({msg[:160]})")), "het_han_muc", "dung"
    if status == 404 or "not_found" in low or "no longer available" in low:
        m = _RE_MODEL_THAY.search(msg or body or "")
        goi_y = L(f" Google khuyên dùng: {m.group(1)}.", f" Google recommends: {m.group(1)}.") if m else ""
        return (L(f"Model Gemini '{model}' không tồn tại hoặc không còn mở cho tài khoản này.{goi_y} "
                "Vào Cài đặt → Gemini chọn lại model.", f"The Gemini model '{model}' does not exist or is no longer open to this account.{goi_y} Go to Settings → Gemini and pick the model again.")), None, "dung"
    if status == 503 or "unavailable" in low or "high demand" in low:
        return L(f"Máy chủ Gemini đang quá tải (503) — tự thử lại… ({msg[:120]})", f"The Gemini server is overloaded (503) — retrying automatically… ({msg[:120]})"), None, "thu_lai"
    if status and 500 <= status < 600:
        return L(f"Máy chủ Gemini đang gặp sự cố (lỗi {status}). Vui lòng thử lại sau.", f"The Gemini server is having problems (error {status}). Please try again later."), None, "thu_lai"
    return (L(f"Gemini báo lỗi: {msg}", f"Gemini reported an error: {msg}") if msg else L(f"Gemini trả lỗi HTTP {status}.", f"Gemini returned HTTP error {status}.")), None, "dung"


# ------------------------------------------------------------------ gọi
def _body(messages, model: str, max_tokens: int, temperature: float, suy_nghi: str, json_mode: bool = False) -> dict:
    b = {
        "model": model,
        "messages": messages,
        "stream": False,
        #  Gemini nhận temperature 0..2 (DeepSeek khuyên 1.3 cho dịch — vẫn trong khoảng).
        "temperature": max(0.0, min(2.0, float(temperature))),
        "max_tokens": int(max_tokens),
    }
    if suy_nghi != BO_SUY_NGHI:
        b["reasoning_effort"] = suy_nghi
    if json_mode:
        #  Ép JSON hợp lệ về cú pháp (đo 07-10-2026: 3.5-flash-lite nhận, 1,3 s; có khi bọc object trong mảng — nơi đọc tự bóc).
        b["response_format"] = {"type": "json_object"}
    return b


def goi(messages, model: str, timeout: int = 180, max_retries: int = 3, max_tokens: int = 8192,
        temperature: float = 1.3, suy_nghi: str | None = None, json_mode: bool = False) -> str:
    """Một lượt chat completion Gemini (đồng bộ, chặn) — cùng chữ ký với ``deepseek_service._chat``.
    Lỗi cấu hình / khoá / hạn mức → ``DeepSeekError`` (lớp lỗi chung của các bộ AI) kèm thông báo tiếng Việt."""
    from services.deepseek_service import DeepSeekError, ghi_su_co, xoa_su_co

    cfg = _cfg()
    api_key = cfg.get("api_key")
    if not api_key:
        raise DeepSeekError(L("Chưa cấu hình API key Gemini (vào Cài đặt → Gemini để nhập).", "The Gemini API key is not configured (go to Settings → Gemini to enter it)."), status=None)
    model = str(model or "").strip() or str(cfg.get("default_model") or "").strip()
    if not model:
        raise DeepSeekError(L("Chưa chọn model Gemini. Vào Cài đặt → Gemini chọn model rồi lưu lại.", "No Gemini model chosen. Go to Settings → Gemini, pick a model and save."))
    muc = str(suy_nghi or SUY_NGHI_MAC_DINH).strip().lower()
    if muc not in SUY_NGHI_HOP_LE:
        muc = SUY_NGHI_MAC_DINH
    nho = muc_suy_nghi_nho(model)
    if nho == BO_SUY_NGHI:
        muc = BO_SUY_NGHI
    elif nho == "low" and muc in ("none", "minimal"):
        muc = "low"
    url = f"{str(cfg.get('base_url') or BASE_URL_MAC_DINH).rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "SubForge-Desktop-Client"}

    da_cho_429 = 0.0
    lan_503 = 0
    last_err, last_user_msg, last_status = None, None, None
    attempt = 0
    while True:
        attempt += 1
        payload = json.dumps(_body(messages, model, max_tokens, temperature, muc, json_mode)).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            xoa_su_co(giu_da_xem=True, provider=GEMINI)
            try:
                return data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                raise DeepSeekError(L("Gemini trả về phản hồi không có nội dung.", "Gemini returned a response with no content."), detail=json.dumps(data)[:300])
        except DeepSeekError:
            raise
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "ignore")[:800]
            except Exception:  # noqa: BLE001
                pass
            last_status = e.code
            last_user_msg, loai, cach = phan_loai_loi(e.code, err_body, model)
            last_err = f"HTTP {e.code} {e.reason}: {err_body[:300]}"
            if cach == "ha_suy_nghi" and muc != BO_SUY_NGHI:
                #  Thang lùi: mức yêu cầu → "low" → bỏ hẳn trường (Gemma không có chế độ suy nghĩ). Nhớ cho model trong phiên.
                muc_moi = "low" if muc != "low" else BO_SUY_NGHI
                logger.info(L(f"Gemini: model '{model}' không nhận reasoning_effort='{muc}' — {'dùng low' if muc_moi == 'low' else 'bỏ reasoning_effort'} từ giờ.", f"Gemini: model '{model}' does not accept reasoning_effort='{muc}' — {'using low' if muc_moi == 'low' else 'dropping reasoning_effort'} from now on."))
                _khong_nhan_muc[model] = muc_moi
                muc = muc_moi
                continue
            if cach == "cho_429":
                cho_goc = giay_cho_lai(last_user_msg, err_body)
                if cho_goc is not None and cho_goc > TRAN_CHO_429:
                    #  Bảo đợi hàng giờ (hạn mức NGÀY cạn nhưng câu lỗi không ghi "per day") — ngủ 2 phút rồi báo là vô ích:
                    #  báo hết hạn mức ngay để popup hiện và nơi gọi tự xử (fallback / dừng lô).
                    last_user_msg = (L(f"Gemini đã HẾT HẠN MỨC của model '{model}' trong tài khoản bạn (Google bảo đợi {cho_goc / 60:.0f} phút). "
                                     "Gói miễn phí / Pro: Flash 20 lượt/ngày, Flash-Lite 500 lượt/ngày — đổi sang model Lite trong Cài đặt, "
                                     "đợi hạn mức đặt lại, hoặc bật thanh toán API (Set up billing) để hết giới hạn.", f"Gemini QUOTA EXHAUSTED for model '{model}' on your account (Google says wait {cho_goc / 60:.0f} minutes). Free / Pro plan: Flash 20 requests/day, Flash-Lite 500 requests/day — switch to a Lite model in Settings, wait for the quota to reset, or enable API billing (Set up billing) to lift the limit."))
                    ghi_su_co("het_han_muc", last_user_msg, provider=GEMINI)
                    raise DeepSeekError(last_user_msg, status=429, detail=last_err)
                cho = cho_goc or 10.0
                cho = min(cho + 1.0, TRAN_CHO_429 - da_cho_429)
                if cho <= 0:
                    raise DeepSeekError(L(f"{last_user_msg} (đã đợi quá {int(TRAN_CHO_429)} s)", f"{last_user_msg} (waited over {int(TRAN_CHO_429)} s)"), status=429, detail=last_err)
                logger.warning(L(f"Gemini 429 — đợi {cho:.0f} s rồi gọi lại (đã đợi {da_cho_429:.0f} s trong lượt này).", f"Gemini 429 — waiting {cho:.0f} s then calling again (already waited {da_cho_429:.0f} s in this run)."))
                time.sleep(cho)
                da_cho_429 += cho
                continue
            if cach == "thu_lai":
                lan_503 += 1
                if lan_503 <= LAN_THU_503:
                    time.sleep(1.5 * lan_503)
                    continue
                raise DeepSeekError(last_user_msg, status=e.code, detail=last_err)
            if loai:
                ghi_su_co(loai, last_user_msg, provider=GEMINI)
            raise DeepSeekError(last_user_msg, status=e.code, detail=last_err)
        except Exception as e:  # noqa: BLE001 — mạng / timeout
            last_err = str(e)
            last_user_msg = L(f"Không gọi được Gemini (mạng/timeout): {e}", f"Could not call Gemini (network/timeout): {e}")
            #  Ghi rõ để biết lượt chờ dài (vd kịch bản Sáng tạo 658 s = 600 s treo + gọi lại 56 s?) là do treo hay do model chậm.
            logger.warning(L(f"Gemini: lỗi mạng / timeout lần {attempt}/{max_retries} với model '{model}' (timeout {timeout} s): {e}", f"Gemini: network error / timeout attempt {attempt}/{max_retries} with model '{model}' (timeout {timeout} s): {e}"))
        if attempt >= max_retries:
            break
        time.sleep(1.5 * attempt)
    raise DeepSeekError(last_user_msg or L(f"Gemini API lỗi: {last_err}", f"Gemini API error: {last_err}"), status=last_status, detail=last_err or "")


def liet_ke_model(api_key: str, base_url: str = "", timeout: int = 20) -> list[str]:
    """Tên các model mà KHOÁ NÀY thấy (``GET /models`` của tầng OpenAI) — để nút Kiểm tra xác nhận model đã chọn còn dùng được.
    Lỗi → ném ``urllib.error.HTTPError`` / ``URLError`` cho nơi gọi phân loại."""
    url = f"{(base_url or BASE_URL_MAC_DINH).rstrip('/')}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}", "User-Agent": "SubForge-Desktop-Client"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8", "ignore"))
    ra = []
    for m in (j.get("data") or j.get("models") or []):
        ten = str((m or {}).get("id") or (m or {}).get("name") or "")
        if ten.startswith("models/"):
            ten = ten[len("models/"):]
        if ten and ten not in ra:
            ra.append(ten)
    return ra


def model_chu_cua_khoa(api_key: str, base_url: str = "", timeout: int = 20) -> list[str]:
    """Model CHỮ mà khoá này thấy, danh sách khai sẵn xếp trước (đúng thứ tự), rồi các model chữ khác theo tên giảm dần (mới trước)."""
    co = liet_ke_model(api_key, base_url, timeout)
    chu = [m for m in co if la_model_chu(m)]
    dau = [m for m in MODELS_MAC_DINH if m in chu]
    them = sorted((m for m in chu if m not in dau), reverse=True)
    return dau + them
