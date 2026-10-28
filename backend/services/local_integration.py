"""New runtime wiring for local-only AI mode."""
from __future__ import annotations

import asyncio
import functools
import logging
import os


def install_local_services() -> None:
    # Pascal has no fast FP16 instructions. Let CTranslate2 report which
    # compute formats its actual CUDA build supports before choosing Whisper.
    from recovery_runtime import selected_cuda_runtime
    if selected_cuda_runtime() is not None:
        import torch
        try:
            capability = torch.cuda.get_device_capability(0) if torch.cuda.is_available() else None
        except (AssertionError, RuntimeError, IndexError):
            # CUDA_VISIBLE_DEVICES="" can make some torch builds report
            # is_available() before exposing device 0; treat that as CPU mode.
            capability = None
        if capability is not None and capability[0] < 7:
            import ctranslate2
            supported = ctranslate2.get_supported_compute_types("cuda")
            preferred = "int8_float32" if "int8_float32" in supported else "float32"
            os.environ.setdefault("SUBFORGE_WHISPER_COMPUTE_TYPE", preferred)

    from services import deepseek_service, gemini_service, translation_service, tts_service
    from services.local_ai_service import local_chat, local_translate, _cfg
    from services.config_service import ConfigService

    def local_config(self):
        base, model = _cfg()
        return {"api_key": "local-no-cloud-key", "base_url": base,
                "models": [model], "default_model": model}

    ConfigService.get_deepseek_config = local_config
    ConfigService.get_gemini_config = local_config
    ConfigService.ai_provider_mac_dinh = lambda self: "deepseek"
    # Legacy selectors remain accepted by the built UI, while every request
    # and displayed model comes from the local configuration above.
    def chat(*args, **kwargs):
        try:
            return local_chat(*args, **kwargs)
        except Exception as exc:
            raise deepseek_service.DeepSeekError(str(exc), detail=str(exc)) from exc

    # All LLM calls use one local adapter; cloud keys and cloud base URLs are ignored.
    deepseek_service._chat = chat
    gemini_service.goi = chat
    gemini_service.BASE_URL_MAC_DINH = _cfg()[0]

    async def local_fallback(segments, source_lang, target_lang, should_stop=None):
        results = {}
        for segment in segments:
            if should_stop is not None and should_stop():
                break
            results[segment["id"]] = await asyncio.to_thread(
                local_translate, segment.get("text", ""), source_lang, target_lang
            )
        return results

    # The shipped DeepSeek path falls back to Google on errors. In local mode
    # fallback stays local too; no cloud endpoint is ever attempted.
    deepseek_service._google_fallback = local_fallback

    async def local_dubbing(segments, source_lang, target_lang, model,
                            should_stop=None, on_progress=None, style=None,
                            voice=None, batch_size=None):
        """Translate each subtitle independently so a small local model cannot
        shift numbered lines or return a mixed-language batch."""
        results, errors = {}, []
        for index, segment in enumerate(segments, 1):
            if should_stop is not None and should_stop():
                errors.extend(s["id"] for s in segments[index - 1:])
                break
            try:
                text = await asyncio.to_thread(local_translate, segment.get("text", ""), source_lang, target_lang)
                results[segment["id"]] = text
            except Exception:
                logging.getLogger("SubForgeLocal").exception("Local translation failed for segment %s", segment["id"])
                errors.append(segment["id"])
            if on_progress:
                try:
                    on_progress(index, len(segments))
                except Exception:
                    logging.getLogger("SubForgeLocal").exception("Translation progress callback failed")
        return results, [], errors

    deepseek_service.translate_dubbing_segments = local_dubbing

    async def translate_item(self, src, dest, text, stop_event=None):
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("Translation was cancelled")
        if not str(text or "").strip():
            return text
        if src == dest:
            return text
        async with self.semaphore:
            return await asyncio.to_thread(local_translate, text, src, dest)

    async def batch_translate(self, texts, src="en", dest="vi", engine="google", should_stop=None):
        # Keep one position per input, as the original route saves completed
        # translations before reporting cancellation.
        results = [None] * len(texts)
        for index, text in enumerate(texts):
            if should_stop is not None and should_stop():
                break
            results[index] = await translate_item(self, src, dest, text)
        return results

    translation_service.TranslationService._translate_item = translate_item
    translation_service.TranslationService.batch_translate = batch_translate
    translation_service._gtx_translate = local_translate
    translation_service._gtx_translate_lines = lambda texts, sl="auto", tl="vi": [local_translate(t, sl, tl) for t in texts]

    original_generate_voice = tts_service.TTSService.generate_voice

    @functools.wraps(original_generate_voice)
    async def generate_voice(self, text, output_path, voice=None, *args, **kwargs):
        selected = voice or self.voice or ""
        from services.tts_engines import engine_of, ENGINE_EDGE
        if engine_of(selected) == ENGINE_EDGE:
            # Map the *language* of old Edge selections to a real local voice.
            # This intentionally does not claim to reproduce Microsoft's voice.
            from services.local_voices import resolve_local_voice
            voice = resolve_local_voice(selected)
        return await original_generate_voice(self, text, output_path, voice, *args, **kwargs)

    tts_service.TTSService.generate_voice = generate_voice

    # Disable all accidental online fallbacks in local model loaders.
    os.environ.setdefault("SUBFORGE_OFFLINE_ONLY", "1")
    from services.qwen_tts_service import QwenTTSService
    from recovery_runtime import RUNTIME
    from services import qwen_tts_service
    qwen_tts_service._bin_dir = lambda: str(RUNTIME / "qwen_bin")
    # Prevent any attempt to fetch telemetry, models or LoRA during runtime.
    from services import usage_reporter
    usage_reporter.start_worker = lambda: None
    usage_reporter.flush = lambda: False
