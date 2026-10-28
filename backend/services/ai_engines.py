# -*- coding: utf-8 -*-
"""ĐIỂM PHÂN LUỒNG BỘ AI (DeepSeek / Gemini) — nơi DUY NHẤT hiểu mã engine / mã model.

Vì sao có tệp này (07-10-2026): thêm Gemini làm bộ AI thứ hai. Trước đó app có hàng chục
chỗ tự kiểm ``startswith("deepseek")`` / ``== "deepseek"`` rồi coi phần còn lại là Google;
thêm Gemini thì mỗi chỗ như thế lại hiểu mã ``gemini:…`` theo một kiểu riêng. Cùng cách
làm với bộ đọc giọng: ``services/tts_engines.py`` + test bánh cóc.

Mã dùng xuyên suốt app:
  · engine dịch / nguồn: ``"google"`` · ``"manual"`` · ``"deepseek"`` · ``"gemini"`` ·
    ``"deepseek:<model>"`` · ``"gemini:<model>"``;
  · mã model gửi cho ``deepseek_service._chat``: ``"<provider>:<model>"``; tên TRƠN
    (``deepseek-v4-flash``) vẫn hiểu là DeepSeek — tương thích cấu hình, dự án, lô cũ.

Mirror ở frontend: ``constants/aiEngines.ts`` (đợt 1).
"""
from __future__ import annotations

DEEPSEEK = "deepseek"
GEMINI = "gemini"
#: các bộ AI (gọi model qua API). Thứ tự = thứ tự hiện ở giao diện.
PROVIDERS = (DEEPSEEK, GEMINI)
#: tên hiện với người dùng
TEN = {DEEPSEEK: "DeepSeek", GEMINI: "Gemini"}
#: Mã engine "bộ AI THEO CÀI ĐẶT" (07-10-2026): lô / Sáng tạo mặc định ghi ``"ai"`` thay vì gắn cứng ``"deepseek"`` — đổi bộ mặc định ở
#: Cài đặt là mọi lô mới đi theo. ``provider_of("ai")`` = None (chưa biết bộ nào) → nơi gọi ``or ai_provider_mac_dinh()``; ``la_ai("ai")`` = True.
AI_THEO_CAI_DAT = "ai"


def ten_bo_ai(provider_hoac_ma: str | None) -> str:
    """"DeepSeek" / "Gemini" từ mã provider, mã engine hay mã model; không phải bộ AI (hoặc "ai" theo Cài đặt) → "AI"."""
    s = str(provider_hoac_ma or "").strip()
    if s == AI_THEO_CAI_DAT:
        return "AI"
    p = provider_of(s) or tach_model(s)[0]
    return TEN.get(p, "AI")

#: Số lô gọi AI CÙNG LÚC — cho MỌI bộ AI (người dùng chốt 07-10-2026: hạ từ 6 về 5).
#: Gói Gemini miễn phí chỉ 5 lượt/phút: đợt 5 lô đầu đi ngay, đợt kế bị 429 và CHỜ theo
#: "retry in" rồi mới đi tiếp (xem đợt 1) — không rơi về Google Dịch.
AI_CONCURRENCY = 5


def tach_model(ma: str | None) -> tuple[str, str]:
    """``"gemini:gemini-3.8-flash"`` → ``("gemini", "gemini-3.8-flash")``; ``"deepseek:deepseek-v4-flash"`` → ``("deepseek", "deepseek-v4-flash")``;
    tên trơn ``"deepseek-v4-flash"`` → ``("deepseek", "deepseek-v4-flash")``; rỗng → ``("deepseek", "")`` (model mặc định do nơi gọi điền)."""
    s = str(ma or "").strip()
    for p in PROVIDERS:
        if s == p:
            return p, ""
        if s.startswith(p + ":"):
            return p, s[len(p) + 1:].strip()
    return DEEPSEEK, s


def provider_of(engine: str | None) -> str | None:
    """Bộ AI của một mã engine (``"deepseek"``, ``"gemini:…"``…) — ``None`` nếu không phải bộ AI (``"google"``, ``"manual"``, rỗng)."""
    s = str(engine or "").strip()
    for p in PROVIDERS:
        if s == p or s.startswith(p + ":"):
            return p
    return None


def la_ai(engine: str | None) -> bool:
    """Engine là một bộ AI (DeepSeek / Gemini, hoặc "ai" = bộ theo Cài đặt) — khác Google / manual."""
    return provider_of(engine) is not None or str(engine or "").strip() == AI_THEO_CAI_DAT


def provider_hieu_luc(engine: str | None) -> str:
    """Bộ AI thật sự sẽ gọi: ``"gemini:…"`` → gemini; ``"ai"`` / mã lạ → bộ mặc định ở Cài đặt."""
    p = provider_of(engine)
    if p:
        return p
    from services.config_service import get_config_service

    return get_config_service().ai_provider_mac_dinh()


def ma_model(provider: str, model: str) -> str:
    """Ghép lại mã ``provider:model`` (DeepSeek giữ tên trơn để cấu hình cũ không đổi)."""
    return model if provider == DEEPSEEK else f"{provider}:{model}"
