"""MỘT ĐIỂM PHÂN LUỒNG cho mọi bộ đọc giọng: mã giọng này do ai đọc?

VÌ SAO CÓ: trước 2.15.0 app chỉ có hai bộ đọc, và cách phân biệt là rải
``is_vieneu_voice(v)`` ở hơn chục chỗ — hễ KHÔNG phải VieNeu thì coi là Edge. Thêm bộ
đọc thứ ba (Qwen3-TTS) mà giữ kiểu đó thì mã Qwen âm thầm rơi vào nhánh Edge: gửi
``qwen:en:ryan`` lên máy chủ Microsoft, nhận lỗi khó hiểu, hoặc tệ hơn là bảng giới
tính của chế độ Review ghi đè giọng người dùng chọn bằng một giọng Edge.

Nên: mọi chỗ cần biết "bộ đọc nào" gọi ``engine_of``. Test
``test_voice_dispatch_ratchet.py`` đếm các chỗ còn tự kiểm tiền tố để không ai thêm
đường tắt mới.

MÃ GIỌNG QWEN — ngôn ngữ nằm NGAY TRONG mã giọng, giống giọng Edge (``en-US-…``):
    qwen:<lang>:<speaker>       giọng CÓ SẴN của bản CustomVoice (vd ``qwen:en:ryan``)
    qwen-clone:<lang>:<name>    giọng clone (và giọng mẫu dựng sẵn đóng gói kèm app)
Nhờ vậy khoá lưu đệm / vân tay giọng tự phân biệt theo ngôn ngữ, và 7 nơi gọi
``generate_voice`` không phải thêm tham số. Tên clone không chứa ``:`` (``_safe_name``
của VieNeu và của Qwen đều bỏ ký tự này) nên ``split(":", 2)`` là an toàn.

BẢN SOI GƯƠNG: ``frontend/src/constants/ttsEngines.ts``.
"""
from __future__ import annotations

from utils.i18n import L
from services.vieneu_service import is_vieneu_voice

QWEN_PRESET_PREFIX = "qwen:"
QWEN_CLONE_PREFIX = "qwen-clone:"

#  Thứ tiếng Qwen3-TTS đọc được, theo khoá ngôn ngữ đích của app (tiếng Trung là
#  "zh-cn" như bảng giọng Edge) -> nhãn mà API của Qwen nhận. KHÔNG có tiếng Việt:
#  tiếng Việt vẫn là việc của VieNeu / Edge.
QWEN_LANG_LABEL = {
    "en": "English", "ja": "Japanese", "ko": "Korean", "zh-cn": "Chinese",
    "de": "German", "fr": "French", "ru": "Russian", "pt": "Portuguese",
    "es": "Spanish", "it": "Italian",
}

ENGINE_EDGE = "edge"
ENGINE_VIENEU = "vieneu"
ENGINE_QWEN = "qwen"


def normalize_lang(lang: str) -> str:
    """Khoá ngôn ngữ về dạng của bảng giọng: "zh" (dự án Chi tiết lưu vậy) -> "zh-cn"."""
    lg = (lang or "").strip().lower()
    return "zh-cn" if lg in ("zh", "zh-cn", "zh-hans") else lg


def is_qwen_voice(voice: str | None) -> bool:
    v = voice or ""
    return v.startswith(QWEN_PRESET_PREFIX) or v.startswith(QWEN_CLONE_PREFIX)


def engine_of(voice: str | None) -> str:
    """"vieneu" | "qwen" | "edge". Mã rỗng / lạ rơi về "edge" — đúng hành vi cũ."""
    if is_vieneu_voice(voice or ""):
        return ENGINE_VIENEU
    if is_qwen_voice(voice):
        return ENGINE_QWEN
    return ENGINE_EDGE


def parse_qwen_voice(voice: str | None) -> tuple[str, str, str] | None:
    """``("preset"|"clone", lang, name)`` của một mã Qwen, hoặc None nếu sai dạng."""
    v = voice or ""
    if v.startswith(QWEN_CLONE_PREFIX):
        kind, rest = "clone", v[len(QWEN_CLONE_PREFIX):]
    elif v.startswith(QWEN_PRESET_PREFIX):
        kind, rest = "preset", v[len(QWEN_PRESET_PREFIX):]
    else:
        return None
    lang, sep, name = rest.partition(":")
    lang = normalize_lang(lang)
    if not sep or not name or lang not in QWEN_LANG_LABEL:
        return None
    return kind, lang, name


def make_qwen_voice(kind: str, lang: str, name: str) -> str:
    prefix = QWEN_CLONE_PREFIX if kind == "clone" else QWEN_PRESET_PREFIX
    return f"{prefix}{normalize_lang(lang)}:{name}"


def qwen_voice_lang(voice: str | None) -> str | None:
    """Ngôn ngữ ghi trong mã giọng Qwen (khoá bảng giọng), None nếu không phải Qwen."""
    p = parse_qwen_voice(voice)
    return p[1] if p else None


def with_lang(voice: str, lang: str) -> str | None:
    """Đổi NGÔN NGỮ của mã giọng Qwen, giữ nguyên người đọc (đổi ngôn ngữ đích của
    dự án thì không bắt người dùng chọn lại giọng). None = thứ tiếng mới Qwen không
    đọc được -> nơi gọi tự về giọng Edge mặc định."""
    p = parse_qwen_voice(voice)
    lg = normalize_lang(lang)
    if not p or lg not in QWEN_LANG_LABEL:
        return None
    return make_qwen_voice(p[0], lg, p[2])


class UnknownVoiceIdError(ValueError):
    """Mã giọng có dạng ``tiền_tố:...`` lọt tới nhánh Edge — chắc chắn là bug định
    tuyến (mã Edge thật KHÔNG BAO GIỜ chứa ``:``). Báo rõ thay vì gửi lên Microsoft."""


def assert_edge_voice(voice: str | None) -> None:
    if voice and ":" in voice:
        raise UnknownVoiceIdError(
            L(f"Mã giọng '{voice}' không phải giọng Edge nhưng bị gửi sang Edge TTS "
            "(lỗi định tuyến bộ đọc giọng). Hãy chọn lại giọng hoặc báo lỗi này.", f"The voice ID '{voice}' is not an Edge voice but was sent to Edge TTS (voice reader routing error). Please pick the voice again or report this error.")
        )


#  Phiên bản "cách sinh" của Qwen: tăng khi đổi thứ làm audio khác đi với CÙNG câu +
#  CÙNG mã giọng (đổi mô hình, đổi seed, đổi tham số lấy mẫu). Đi vào khoá lưu đệm.
#    2: giọng có sẵn đọc qua MẪU NEO (qwen_tts_service.QWEN_PRESET_ANCHOR).
#    3: cắt lặng đầu/cuối + thu lặng dài giữa câu (qwen_tts_service._gon_lang).
QWEN_CACHE_REV = "3"


def engine_cache_tag(voice: str | None) -> str:
    """Đuôi thêm vào khoá lưu đệm / vân tay giọng — RỖNG với Edge và VieNeu, để khoá
    của hai bộ đọc cũ giữ nguyên từng byte (test_tts_cache_key_golden gác). Giọng clone
    Qwen kèm vân tay MẪU GIỌNG: người dùng thay mẫu thì câu cũ phải sinh lại."""
    p = parse_qwen_voice(voice)
    if not p:
        return ""
    tag = f"|qwen@{QWEN_CACHE_REV}"
    if p[0] == "clone":
        try:
            from services.qwen_tts_service import QwenTTSService

            tag += f"#{QwenTTSService.clone_sha(p[2])}"
        except Exception:
            pass
    return tag
