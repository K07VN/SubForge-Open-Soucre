"""New, explicit migration from old remote voice selections to local engines."""
from services.tts_engines import engine_of, ENGINE_EDGE

QWEN_SPEAKERS = {"en": "ryan", "ja": "ono_anna", "ko": "sohee", "zh": "vivian",
                 "de": "ryan", "fr": "ryan", "ru": "ryan", "pt": "ryan", "es": "ryan", "it": "ryan"}


def resolve_local_voice(voice):
    if engine_of(voice) != ENGINE_EDGE:
        return voice
    lang = str(voice or "vi").split("-", 1)[0].lower()
    if lang == "vi":
        from services.vieneu_service import PRESET_VOICES, PRESET_PREFIX
        if not PRESET_VOICES:
            raise RuntimeError("No local Vietnamese voice preset is available")
        return PRESET_PREFIX + PRESET_VOICES[0]["name"]
    if lang in QWEN_SPEAKERS:
        return f"qwen:{'zh-cn' if lang == 'zh' else lang}:{QWEN_SPEAKERS[lang]}"
    raise RuntimeError(f"No installed offline voice engine supports language {lang!r}. Choose VieNeu (Vietnamese) or a supported Qwen language.")
