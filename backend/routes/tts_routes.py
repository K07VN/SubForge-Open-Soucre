"""API cho phan chon giong doc: liet ke giong, nghe thu, va nhan ban giong (clone)."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from models.database import get_app_data_dir
from services.tts_service import TTSService
from services.vieneu_service import (
    VieneuService,
    VieneuError,
    is_vieneu_voice,
    _safe_name,
)
from services.qwen_tts_service import QwenError
from routes.auth_routes import verify_license
from utils.i18n import L

# Nghe thử và NHÂN BẢN GIỌNG là tác vụ AI thật — trước đây router này không có hàng
# rào bản quyền nên máy chưa kích hoạt vẫn dùng được.
router = APIRouter(
    prefix="/api/tts", tags=["tts"], dependencies=[Depends(verify_license)]
)
logger = logging.getLogger("TTSService")

tts_service = TTSService()

# Cau nghe thu NGAN (VieNeu chay CPU ~1s/tu; cau dai truoc day lam preview cham
# gap doi). Van du chu co dau/thanh dieu de danh gia chat giong.
PREVIEW_TEXT = "Xin chào, đây là giọng đọc thử của tôi."

# Giong edge-tts theo TUNG NGON NGU.
#
# BAN SOI GUONG cua ``frontend/src/constants/edgeVoices.ts`` — hai ben phai liet ke y
# het nhau. Tab long tieng doc file TS kia, con trinh dung nang cao goi endpoint nay;
# lech nhau la cung mot du an ma hai cho chon giong lai ra hai danh sach khac nhau.
# Test ``backend/tests/test_parity_edge_voices.py`` canh cap nay.
#
# Truoc day o day CHI co tieng Viet, nen trinh dung nang cao khong the chon giong
# Anh/Trung/Nhat du tab long tieng van chon duoc.
EDGE_VOICES_BY_LANG: dict[str, list[dict]] = {
    "vi": [
        {"id": "vi-VN-HoaiMyNeural", "label": "Hoài Mỹ (Nữ)"},
        {"id": "vi-VN-NamMinhNeural", "label": "Nam Minh (Nam)"},
    ],
    "en": [
        {"id": "en-US-AvaNeural", "label": "Ava (Nữ)"},
        {"id": "en-US-AndrewNeural", "label": "Andrew (Nam)"},
        {"id": "en-GB-SoniaNeural", "label": "Sonia (Nữ - UK)"},
    ],
    "zh-cn": [
        {"id": "zh-CN-XiaoxiaoNeural", "label": "Xiaoxiao (Nữ)"},
        {"id": "zh-CN-YunxiNeural", "label": "Yunxi (Nam)"},
    ],
    "ja": [
        {"id": "ja-JP-NanamiNeural", "label": "Nanami (Nữ)"},
        {"id": "ja-JP-KeitaNeural", "label": "Keita (Nam)"},
    ],
    "ko": [
        {"id": "ko-KR-SunHiNeural", "label": "Sun-Hi (Nữ)"},
        {"id": "ko-KR-InJoonNeural", "label": "InJoon (Nam)"},
    ],
    "fr": [
        {"id": "fr-FR-DeniseNeural", "label": "Denise (Nữ)"},
        {"id": "fr-FR-HenriNeural", "label": "Henri (Nam)"},
    ],
    "de": [
        {"id": "de-DE-KatjaNeural", "label": "Katja (Nữ)"},
        {"id": "de-DE-ConradNeural", "label": "Conrad (Nam)"},
    ],
    "es": [
        {"id": "es-ES-ElviraNeural", "label": "Elvira (Nữ)"},
        {"id": "es-ES-AlvaroNeural", "label": "Alvaro (Nam)"},
    ],
    "ru": [
        {"id": "ru-RU-SvetlanaNeural", "label": "Svetlana (Nữ)"},
        {"id": "ru-RU-DmitryNeural", "label": "Dmitry (Nam)"},
    ],
    #  Bồ Đào Nha: chọn pt-BR (Brazil) — khán giả YouTube tiếng Bồ lớn nhất.
    "pt": [
        {"id": "pt-BR-FranciscaNeural", "label": "Francisca (Nữ)"},
        {"id": "pt-BR-AntonioNeural", "label": "Antonio (Nam)"},
    ],
    "it": [
        {"id": "it-IT-ElsaNeural", "label": "Elsa (Nữ)"},
        {"id": "it-IT-DiegoNeural", "label": "Diego (Nam)"},
    ],
    "th": [
        {"id": "th-TH-PremwadeeNeural", "label": "Premwadee (Nữ)"},
        {"id": "th-TH-NiwatNeural", "label": "Niwat (Nam)"},
    ],
    "id": [
        {"id": "id-ID-GadisNeural", "label": "Gadis (Nữ)"},
        {"id": "id-ID-ArdiNeural", "label": "Ardi (Nam)"},
    ],
}

EDGE_LANG_LABEL = {
    "vi": "Tiếng Việt",
    "en": "English",
    "zh-cn": "中文 (Tiếng Trung)",
    "ja": "日本語 (Tiếng Nhật)",
    "ko": "한국어 (Tiếng Hàn)",
    "fr": "Français (Tiếng Pháp)",
    "de": "Deutsch (Tiếng Đức)",
    "es": "Español (Tây Ban Nha)",
    "ru": "Русский (Tiếng Nga)",
    "pt": "Português (Bồ Đào Nha)",
    "it": "Italiano (Tiếng Ý)",
    "th": "ไทย (Tiếng Thái)",
    "id": "Bahasa Indonesia",
}

# Cau nghe thu theo tung ngon ngu — de mot cau tieng Viet cho giong Nhat doc thi khong
# danh gia duoc gi ve chat giong.
PREVIEW_TEXT_BY_LANG = {
    "vi": PREVIEW_TEXT,
    "en": "Hello, this is a sample of my voice.",
    "zh-cn": "你好，这是我的声音示例。",
    "ja": "こんにちは、これは私の声のサンプルです。",
    "ko": "안녕하세요, 제 목소리 샘플입니다.",
    "fr": "Bonjour, voici un extrait de ma voix.",
    "de": "Hallo, das ist eine Hörprobe meiner Stimme.",
    "es": "Hola, esta es una muestra de mi voz.",
    "ru": "Здравствуйте, это образец моего голоса.",
    "pt": "Olá, esta é uma amostra da minha voz.",
    "it": "Ciao, questo è un campione della mia voce.",
    "th": "สวัสดีครับ นี่คือตัวอย่างเสียงของฉัน",
    "id": "Halo, ini adalah contoh suara saya.",
}


def _lang_of_voice(voice: str) -> str:
    """Doan ngon ngu tu ma giong Edge (``en-US-AvaNeural`` -> ``en``).

    Giong Qwen3-TTS mang san ngon ngu trong ma (``qwen:de:ryan`` -> ``de``): khong doc
    o day thi cau nghe thu roi ve tieng Viet cho mot giong doc tieng Duc."""
    from services.tts_engines import qwen_voice_lang

    qlang = qwen_voice_lang(voice)
    if qlang:
        return qlang
    for lang, items in EDGE_VOICES_BY_LANG.items():
        if any(v["id"] == voice for v in items):
            return lang
    return "vi"

PREVIEW_DIR = os.path.join(get_app_data_dir(), "ai_models", "tts_preview")
os.makedirs(PREVIEW_DIR, exist_ok=True)


@router.get("/voices")
async def list_voices():
    """Danh sach giong doc: Edge theo tung ngon ngu, VieNeu preset, giong clone.

    Moi ngon ngu la MOT NHOM rieng thay vi gop chung mot cuc "Edge": o chon hien tieu
    de nhom nen tim giong theo ngon ngu bang mat, khong phai doc ma vung cua tung dong.
    """
    # Nap model VieNeu o luong nen ngay khi mo picker -> luc bam 'nghe thu' khong
    # con phai cho ~10s cold-load.
    VieneuService.warmup_async()
    vieneu = VieneuService.list_voices()
    edge_groups = [
        {
            "key": f"edge-{lang}",
            "label": f"Edge — {EDGE_LANG_LABEL.get(lang, lang)}",
            "voices": voices,
        }
        for lang, voices in EDGE_VOICES_BY_LANG.items()
    ]
    return {
        "groups": [
            *edge_groups,
            {"key": "vieneu", "label": L("VieNeu (chất lượng cao, offline)", "VieNeu (high quality, offline)"), "voices": vieneu["presets"],
             **_khoa("vieneu")},
            #  Bộ giọng mẫu DỰNG SẴN của SubForge (clone đóng gói kèm app, dùng ngay, không xoá được).
            #  Chỉ có nhóm khi app đi kèm bộ giọng (assets/voices/manifest.json).
            *([{"key": "builtin_clones", "label": L("Giọng mẫu của SubForge", "SubForge sample voices"), "voices": vieneu["builtin"],
                **_khoa("vieneu")}]
              if vieneu.get("builtin") else []),
            {"key": "clone", "label": L("Giọng của tôi (nhân bản)", "My voices (cloned)"), "voices": vieneu["clones"]},
            *_qwen_groups(),
        ]
    }


def _khoa(model: str) -> dict:
    """``{"locked": True, "needs_model": <khoá>}`` khi mô hình chưa tải — giao diện vẫn LIỆT KÊ giọng
    (để người dùng biết có gì) nhưng làm mờ, nghe thử bằng mẫu dựng sẵn, chọn dùng thì nhắc tải model.
    Lỗi khi kiểm -> không khoá (giữ hành vi cũ, không chặn nhầm)."""
    try:
        from routes import model_routes as MR

        co = MR.check_vieneu_downloaded() if model == "vieneu" else MR.check_qwen3_tts_downloaded()
        return {} if co else {"locked": True, "needs_model": model}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Không kiểm được trạng thái mô hình '{model}': {e}")
        return {}


def _qwen_groups() -> list[dict]:
    """Nhóm giọng Qwen3-TTS — MỘT nhóm cho mỗi thứ tiếng (``qwen-<lang>``), cùng kiểu
    nhóm ``edge-<lang>``: mã giọng Qwen mang sẵn ngôn ngữ nên cùng một người đọc là một
    mã khác nhau ở mỗi thứ tiếng. Mỗi nhóm: 9 giọng có sẵn (bản xứ lên đầu) + clone.

    Chưa tải mô hình (Quản lý Model AI, 1,4–2,1 GB) vẫn LIỆT KÊ 9 giọng có sẵn nhưng gắn
    ``locked`` — giao diện làm mờ + nhắc tải; giọng clone chỉ có khi đã cài."""
    try:
        from services.qwen_tts_service import QwenTTSService
        from services.tts_engines import QWEN_LANG_LABEL

        cai = QwenTTSService.is_installed()
        ra = []
        for lang in QWEN_LANG_LABEL:
            v = QwenTTSService.list_voices(lang)
            voices = [{"id": x["id"], "label": x["label"], "native": x["native"]} for x in v["presets"]]
            if cai:
                voices += [{"id": x["id"], "label": x["label"], "clone": True, "builtin": x["builtin"]}
                           for x in v["clones"]]
            ra.append({"key": f"qwen-{lang}",
                       "label": f"Qwen3 — {EDGE_LANG_LABEL.get(lang, lang)}", "voices": voices,
                       **({} if cai else {"locked": True, "needs_model": "qwen3_tts"})})
        return ra
    except Exception as e:  # noqa: BLE001 — lỗi Qwen không được làm mất danh sách giọng khác
        logger.warning(f"Không liệt kê được giọng Qwen3-TTS: {e}")
        return []


@router.post("/preview")
async def preview_voice(voice: str = Form(...), text: str = Form("")):
    """Sinh (hoac tra tu cache) 1 doan mp3 nghe thu cho giong da chon.

    Khong truyen ``text`` thi lay cau mau DUNG NGON NGU cua giong do.
    """
    #  Không gõ câu riêng -> ưu tiên mẫu DỰNG SẴN (phát ngay, khỏi nạp mô hình). Thiếu/hỏng thì
    #  rơi xuống đường sinh trực tiếp bên dưới như cũ.
    if not (text or "").strip():
        from services import nghe_thu_dung_san
        san = nghe_thu_dung_san.tim(voice)
        if san:
            return FileResponse(san, media_type="audio/mpeg", filename="preview.mp3")
    mau = PREVIEW_TEXT_BY_LANG.get(_lang_of_voice(voice), PREVIEW_TEXT)
    text = (text or mau).strip()[:200]
    voice_safe = "".join([c if c.isalnum() or c in "-_" else "_" for c in voice])
    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    out = os.path.join(PREVIEW_DIR, f"{voice_safe}_{text_hash}.mp3")
    try:
        if not (os.path.exists(out) and os.path.getsize(out) > 0):
            ok, _ = await tts_service.generate_voice(text, out, voice=voice, overwrite=True, raise_on_error=True)
            if not ok or not os.path.exists(out) or os.path.getsize(out) == 0:
                raise HTTPException(status_code=500, detail=L("Không tạo được audio nghe thử", "Could not create the preview audio"))
        return FileResponse(out, media_type="audio/mpeg", filename="preview.mp3")
    except (VieneuError, QwenError) as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS preview error: {e}")
        return JSONResponse(status_code=500, content={"detail": f"Lỗi nghe thử: {e}"})


@router.post("/generate")
async def generate_custom_tts(
    text: str = Form(...),
    voice: str = Form(...),
    speed: float = Form(1.0),
    format: str = Form("mp3"),
    style: str = Form("tu_nhien"),
):
    """Sinh đoạn audio TTS tự chọn từ văn bản nhập vào (hỗ trợ VieNeu, Edge TTS, Giọng Clone)."""
    import re
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=L("Vui lòng nhập văn bản cần tạo giọng", "Please enter the text to generate speech for"))

    # Loại bỏ các thẻ cảm xúc [cười], [thở dài]... nếu KHÔNG phải giọng VieNeu (ví dụ Edge TTS)
    if not is_vieneu_voice(voice):
        text = re.sub(r'\[.*?\]', '', text).strip()
        text = re.sub(r'\s+', ' ', text)

    if not text:
        raise HTTPException(status_code=400, detail=L("Vui lòng nhập văn bản có nội dung hợp lệ", "Please enter text with valid content"))

    gen_dir = os.path.join(get_app_data_dir(), "generated_tts")
    os.makedirs(gen_dir, exist_ok=True)

    file_id = f"tts_{hashlib.md5(f'{text}_{voice}_{speed}_{format}_{style}_{asyncio.get_event_loop().time()}'.encode()).hexdigest()[:12]}"
    ext = "wav" if str(format).lower() == "wav" else "mp3"
    out_path = os.path.join(gen_dir, f"{file_id}.{ext}")

    try:
        ok, _ = await tts_service.generate_voice(
            text,
            out_path,
            voice=voice,
            speed=speed,
            overwrite=True,
            raise_on_error=True,
        )
        if not ok or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise HTTPException(status_code=500, detail=L("Không thể tạo audio từ văn bản này", "Could not create audio from this text"))

        media_type = "audio/wav" if ext == "wav" else "audio/mpeg"
        fn = f"{file_id}.{ext}"
        headers = {
            "X-Filename": fn,
            "Access-Control-Expose-Headers": "Content-Disposition, X-Filename",
        }
        return FileResponse(out_path, media_type=media_type, filename=fn, headers=headers)
    except (VieneuError, QwenError) as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS custom generation error: {e}")
        return JSONResponse(status_code=500, content={"detail": f"Lỗi sinh audio: {e}"})

@router.post("/clone-voice")
async def clone_voice(name: str = Form(...), audio: UploadFile = File(...)):
    """Nhan ban giong tu 1 file audio mau (3-8s). Tra ve voice id de chon ngay."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail=L("Vui lòng nhập tên giọng", "Please enter a voice name"))

    name_safe = _safe_name(name)
    reg = VieneuService._load_registry()
    if name_safe in reg and not VieneuService.clone_usable(name_safe, reg):
        pass

    try:
        suffix = os.path.splitext(audio.filename or "")[1] or ".wav"
        temp_dir = os.path.join(get_app_data_dir(), "temp_uploads")
        os.makedirs(temp_dir, exist_ok=True)
        temp_file = os.path.join(temp_dir, f"clone_{int(time.time())}{suffix}")
        with open(temp_file, "wb") as f:
            content = await audio.read()
            f.write(content)

        #  add_clone nạp model + mã hoá mẫu (~20 s trên CPU). Gọi thẳng trong hàm async là
        #  CHẶN vòng sự kiện: cả backend đứng hình (thanh tiến độ, nhật ký, mọi request
        #  khác) cho tới khi xong — docstring của add_clone vốn đã ghi phải chạy qua
        #  to_thread mà chỗ gọi quên.
        voice_id = await asyncio.to_thread(VieneuService.add_clone, name_safe, temp_file)
        try:
            os.remove(temp_file)
        except Exception:
            pass

        return {
            "status": "ok",
            "voice_id": voice_id,
            "name": name_safe,
            "label": f"🎤 {name_safe} (Giọng nhân bản)",
        }
    except (VieneuError, QwenError) as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        logger.error(f"Clone voice error: {e}")
        return JSONResponse(status_code=500, content={"detail": f"Lỗi nhân bản giọng: {e}"})


@router.get("/clone-voices")
async def list_clone_voices():
    """Danh sach giong clone hien co."""
    return VieneuService.list_voices()


@router.delete("/clone-voice/{name}")
async def delete_clone_voice(name: str):
    """Xoa giong clone.

    Ten ham ben service la ``remove_clone`` — goi nham ``delete_clone`` thi khong
    phai 404 ma la AttributeError -> 500, nen nut "Xoa giong" khong bao gio chay.
    """
    try:
        ok = VieneuService.remove_clone(name)
    except VieneuError as e:                       # giọng mẫu dựng sẵn: không xoá được
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy giọng clone", "Cloned voice not found"))
    return {"status": "ok"}


# ------------------------------------------------------------------ Qwen3-TTS
@router.get("/qwen/status")
async def qwen_status():
    """Trạng thái Qwen3-TTS cho giao diện: đã cài chưa, đang chạy mức nào, RTF đo được
    trên CHÍNH máy này (để ước lượng thời gian), và máy có bị gắn cờ "chậm" không."""
    from services.qwen_tts_service import QwenTTSService

    st = QwenTTSService.status()
    try:
        tiers = QwenTTSService._tiers()
    except Exception:
        tiers = []
    st["first_tier"] = tiers[0] if tiers else None
    st["slow"] = bool(tiers) and all(t.get("slow") for t in tiers)
    return st


@router.post("/qwen/transcribe-ref")
async def qwen_transcribe_ref(audio: UploadFile = File(...)):
    """Whisper nghe mẫu giọng -> điền sẵn LỜI MẪU + thứ tiếng (người dùng sửa được).

    Có lời mẫu thì clone giống giọng hơn khi đọc CÙNG thứ tiếng với mẫu (chế độ ICL).
    Chạy trong luồng riêng: Whisper nạp model + nhận dạng mất vài giây, gọi thẳng trong
    hàm async là chặn cả backend (lỗi từng gặp với add_clone của VieNeu)."""
    temp_dir = os.path.join(get_app_data_dir(), "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    suffix = os.path.splitext(audio.filename or "")[1] or ".wav"
    tmp = os.path.join(temp_dir, f"qwen_ref_{int(time.time() * 1000)}{suffix}")
    with open(tmp, "wb") as f:
        f.write(await audio.read())
    try:
        from services.stt_service import get_stt_service

        segs, lang = await asyncio.to_thread(get_stt_service().transcribe, tmp)
        text = " ".join((s.get("text") or "").strip() for s in (segs or [])).strip()
        return {"text": text[:500], "lang": (lang or "").lower()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Nhận lời mẫu giọng lỗi: {e}")
        raise HTTPException(
            status_code=400,
            detail=L("Chưa nhận được lời mẫu (cần model Whisper trong Quản lý Model AI). "
                     "Bạn có thể tự gõ lời trong mẫu, hoặc để trống.",
                     "Could not get the sample transcript (the Whisper model is needed in AI Model Management). "
                     "You can type the transcript of the sample yourself, or leave it empty."),
        )
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@router.post("/qwen/clone")
async def qwen_clone(
    name: str = Form(...),
    audio: UploadFile = File(...),
    ref_text: str = Form(""),
    ref_lang: str = Form(""),
):
    """Nhân bản giọng cho Qwen3-TTS (đọc 10 thứ tiếng). Trả ``name`` đã làm sạch — mã
    giọng do giao diện ghép theo ngôn ngữ đích: ``qwen-clone:<lang>:<name>``."""
    from services.qwen_tts_service import QwenTTSService

    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail=L("Vui lòng nhập tên giọng", "Please enter a voice name"))
    temp_dir = os.path.join(get_app_data_dir(), "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    suffix = os.path.splitext(audio.filename or "")[1] or ".wav"
    tmp = os.path.join(temp_dir, f"qwen_clone_{int(time.time() * 1000)}{suffix}")
    with open(tmp, "wb") as f:
        f.write(await audio.read())
    try:
        ten = await asyncio.to_thread(QwenTTSService.add_clone, name, tmp, ref_text, ref_lang)
        _clear_qwen_clone_previews(ten)
        return {"name": ten}
    except QwenError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@router.delete("/qwen/clone/{name}")
async def qwen_delete_clone(name: str):
    from services.qwen_tts_service import QwenTTSService

    if not QwenTTSService.delete_clone(name):
        raise HTTPException(status_code=404, detail=L("Không tìm thấy giọng (hoặc là giọng dựng sẵn)", "Voice not found (or it is a built-in voice)"))
    _clear_qwen_clone_previews(name)
    return {"ok": True}


def _clear_qwen_clone_previews(name: str) -> None:
    """Xoá tệp nghe thử cũ của giọng clone Qwen (mọi thứ tiếng) — thay mẫu mà tệp nghe
    thử cũ còn thì bấm Nghe thử vẫn ra giọng CŨ (VieNeu dọn y như vậy khi ghi đè clone)."""
    try:
        safe = "".join([c if c.isalnum() or c in "-_" else "_" for c in name])
        #  Khớp TRỌN tên tệp (``qwen-clone_<lang>_<tên>_<md5>.mp3``): chỉ so chuỗi con thì
        #  xoá "toi" cũng xoá luôn tệp nghe thử của "giong_toi".
        mau = re.compile(rf"^qwen-clone_[a-z-]+_{re.escape(safe)}_[0-9a-f]{{32}}\.mp3$")
        for f in os.listdir(PREVIEW_DIR):
            if mau.match(f):
                try:
                    os.remove(os.path.join(PREVIEW_DIR, f))
                except OSError:
                    pass
    except OSError:
        pass


@router.post("/concat-audio")
async def concat_audio_files(
    audio_paths: str = Form(...),
    format: str = Form("mp3"),
):
    """Gộp nhiều đoạn file audio (wav/mp3) đã tạo thành 1 file hoàn chỉnh duy nhất."""
    import json
    import subprocess
    import imageio_ffmpeg
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()

    try:
        paths = json.loads(audio_paths)
    except Exception:
        raise HTTPException(status_code=400, detail=L("Danh sách audio cần gộp không hợp lệ", "The list of audio files to merge is invalid"))

    if not paths or not isinstance(paths, list):
        raise HTTPException(status_code=400, detail=L("Vui lòng cung cấp ít nhất 2 đoạn audio để gộp", "Please provide at least 2 audio clips to merge"))

    gen_dir = os.path.join(get_app_data_dir(), "generated_tts")
    valid_files = []
    for p in paths:
        full_p = p if os.path.isabs(p) else os.path.join(gen_dir, os.path.basename(p))
        if os.path.exists(full_p) and os.path.getsize(full_p) > 0:
            valid_files.append(full_p)

    if not valid_files:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy file audio cần gộp", "The audio file to merge was not found"))

    file_id = f"master_{hashlib.md5(f'{valid_files}_{asyncio.get_event_loop().time()}'.encode()).hexdigest()[:12]}"
    ext = "wav" if str(format).lower() == "wav" else "mp3"
    out_master = os.path.join(gen_dir, f"{file_id}.{ext}")

    list_txt = os.path.join(gen_dir, f"concat_{file_id}.txt")
    with open(list_txt, "w", encoding="utf-8") as f:
        for vf in valid_files:
            safe_p = vf.replace("\\", "/")
            f.write(f"file '{safe_p}'\n")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_txt,
        "-c", "copy" if ext == "mp3" else "pcm_s16le",
        out_master
    ]

    try:
        res = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            os.remove(list_txt)
        except Exception:
            pass

        if res.returncode != 0 or not os.path.exists(out_master) or os.path.getsize(out_master) == 0:
            logger.error(f"Concat audio error: {res.stderr}")
            raise HTTPException(status_code=500, detail=L("Không thể gộp các file audio", "Could not merge the audio files"))

        media_type = "audio/wav" if ext == "wav" else "audio/mpeg"
        fn = f"{file_id}.{ext}"
        headers = {
            "X-Filename": fn,
            "Access-Control-Expose-Headers": "Content-Disposition, X-Filename",
        }
        return FileResponse(out_master, media_type=media_type, filename=fn, headers=headers)
    except Exception as e:
        logger.error(f"Concat exception: {e}")
        return JSONResponse(status_code=500, content={"detail": f"Lỗi gộp audio: {e}"})


# ==============================================================================
# HỆ THỐNG LƯU LỊCH SỬ TTS BỀN VỮNG TRÊN ĐĨA CỨNG (PERSISTENT HISTORY)
# ==============================================================================

# Số mục lịch sử TỐI ĐA trả về / lưu lại. Một bài đọc dài (vài giờ tới vài chục
# giờ) sinh ra HÀNG NGÀN file trong generated_tts. Nếu phục hồi hết thì mỗi lần mở
# trang phải mutagen-probe từng file, ghi ra JSON khổng lồ và đẩy cả nghìn dòng
# xuống UI — treo máy yếu đúng lúc nó đang bận sinh giọng. Giữ N bản mới nhất là đủ
# dùng (file cũ vẫn nằm nguyên trên đĩa, mở thư mục là thấy).
HISTORY_MAX_ITEMS = 300


def get_history_file() -> str:
    return os.path.join(get_app_data_dir(), "generated_tts", "tts_history.json")

def load_tts_history() -> list:
    hfile = get_history_file()
    if os.path.exists(hfile):
        try:
            with open(hfile, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading TTS history from {hfile}: {e}")
    # Fallback check appdata directory if data dir moved
    try:
        old_appdata_file = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "SubForge", "generated_tts", "tts_history.json")
        if old_appdata_file != hfile and os.path.exists(old_appdata_file):
            with open(old_appdata_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_tts_history(items: list):
    """Ghi lịch sử ra đĩa KIỂU NGUYÊN TỬ (file tạm -> os.replace).

    Ghi thẳng bằng open(...,"w") sẽ CẮT file về 0 byte NGAY khi mở; json.dump lỗi
    giữa chừng (hoặc app bị tắt) là mất trắng lịch sử mà không có cách nào biết —
    đúng lỗi đã xảy ra khi module thiếu `import json`. Ghi ra .tmp rồi thay thế thì
    file cũ còn nguyên cho tới lúc file mới đã ghi xong.
    """
    hfile = get_history_file()
    tmp = hfile + ".tmp"
    try:
        os.makedirs(os.path.dirname(hfile), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, hfile)
    except Exception as e:
        logger.error(f"Error saving TTS history: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def probe_audio_duration(path: str) -> float:
    """Thời lượng (giây) của 1 file audio, 0.0 nếu không đọc được.

    Dùng mutagen (đã có trong requirements) — đọc header, KHÔNG giải mã cả file nên
    rẻ. Cần cho các mục lịch sử được tự phục hồi từ đĩa: chúng không đi qua bước
    ghi lịch sử của frontend nên không có `duration`, và UI thì cần hiện thời lượng.
    """
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        length = getattr(getattr(mf, "info", None), "length", 0.0)
        return round(float(length), 2) if length and length > 0 else 0.0
    except Exception:
        return 0.0


def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


def resolve_existing_filename(gen_dir: str, fn: str) -> str | None:
    if not fn:
        return None
    fn_base = os.path.basename(fn)
    if os.path.exists(os.path.join(gen_dir, fn_base)):
        return fn_base
    clean_fn = re.sub(r'^(speech_|master_speech_)', '', fn_base)
    if os.path.exists(os.path.join(gen_dir, clean_fn)):
        return clean_fn
    return None


@router.get("/audio/{filename}")
async def get_tts_audio(filename: str):
    """Serves a generated TTS audio file from app_data/generated_tts directory."""
    gen_dir = os.path.join(get_app_data_dir(), "generated_tts")
    real_fn = resolve_existing_filename(gen_dir, filename) or filename
    file_path = os.path.join(gen_dir, real_fn)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=L("File audio không tồn tại", "The audio file does not exist"))
    ext = os.path.splitext(real_fn)[1].lower()
    media_type = "audio/wav" if ext == ".wav" else "audio/mpeg"
    headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Expose-Headers": "Accept-Ranges, Content-Length, Content-Type",
    }
    return FileResponse(file_path, media_type=media_type, headers=headers)


@router.get("/history")
async def get_history():
    """Lấy danh sách lịch sử các file audio TTS đã tạo bền vững từ đĩa cứng kèm dung lượng & vị trí thư mục."""
    items = load_tts_history()
    gen_dir = os.path.join(get_app_data_dir(), "generated_tts")
    os.makedirs(gen_dir, exist_ok=True)
    
    valid_items = []
    known_filenames = set()
    mtimes: dict = {}
    total_bytes = 0
    
    for item in items:
        fn = item.get("filename")
        real_fn = resolve_existing_filename(gen_dir, fn)
        if real_fn:
            item["filename"] = real_fn
            item["audioUrl"] = f"/api/tts/audio/{real_fn}"
            fp = os.path.join(gen_dir, real_fn)
            sz = os.path.getsize(fp)
            item["size"] = sz
            item["formattedSize"] = format_file_size(sz)
            # Mục cũ chưa có thời lượng -> đo 1 lần rồi ghi lại vào registry.
            if not item.get("duration"):
                item["duration"] = probe_audio_duration(fp)
            total_bytes += sz
            valid_items.append(item)
            mtimes[real_fn] = os.path.getmtime(fp)
            known_filenames.add(real_fn)

    # Tự động phục hồi các file audio có sẵn trên đĩa cứng chưa có trong registry.
    # Xếp theo thời gian TRƯỚC rồi mới lấy phần đầu: probe thời lượng là thao tác
    # đọc file, chạy nó trên vài nghìn file mỗi lần mở trang là không chấp nhận được.
    recovered: list = []
    try:
        candidates = []
        for f in os.listdir(gen_dir):
            if f.endswith((".mp3", ".wav")) and f not in known_filenames and not f.startswith("concat_"):
                fp = os.path.join(gen_dir, f)
                if os.path.isfile(fp):
                    sz = os.path.getsize(fp)
                    if sz > 0:
                        candidates.append((os.path.getmtime(fp), f, fp, sz))
        candidates.sort(reverse=True)
        room = max(0, HISTORY_MAX_ITEMS - len(valid_items))
        if len(candidates) > room:
            logger.info(
                f"TTS history: {len(candidates)} file tren dia, chi hien {room} ban moi nhat."
            )
        for mtime, f, fp, sz in candidates[:room]:
            recovered.append({
                "id": f"rec_{f.split('.')[0]}",
                "text": f"Audio ({f})",
                "voice": "VieNeu AI",
                "filename": f,
                "audioUrl": f"/api/tts/audio/{f}",
                "timestamp": time.strftime("%H:%M", time.localtime(mtime)),
                "format": f.split(".")[-1],
                "size": sz,
                "formattedSize": format_file_size(sz),
                "duration": probe_audio_duration(fp),
            })
            mtimes[f] = mtime
            total_bytes += sz
            known_filenames.add(f)
        valid_items.extend(recovered)
    except Exception as e:
        logger.error(f"Error auto recovering audio files: {e}")

    # Sắp MỚI NHẤT TRƯỚC theo thời điểm file được tạo trên đĩa. Cần cho CẢ mục lấy từ
    # registry: registry được ghi lại từ thứ tự os.listdir (= hash băm) nên nếu không
    # sắp thì UI hiện lộn xộn (18:20, 18:13, 18:19...) dù cột giờ vẫn đúng.
    valid_items.sort(key=lambda it: mtimes.get(it.get("filename"), 0.0), reverse=True)
    valid_items = valid_items[:HISTORY_MAX_ITEMS]

    save_tts_history(valid_items)
        
    return {
        "items": valid_items,
        "folderPath": os.path.abspath(gen_dir),
        "totalSizeBytes": total_bytes,
        "formattedTotalSize": format_file_size(total_bytes),
    }


@router.post("/history")
async def add_history_item(
    id: str = Form(...),
    text: str = Form(...),
    voice: str = Form(...),
    filename: str = Form(...),
    timestamp: str = Form(...),
    format: str = Form("mp3"),
    duration: float = Form(0.0),
):
    """Lưu 1 mục lịch sử mới vào tts_history.json trên đĩa."""
    items = load_tts_history()
    audio_url = f"/api/tts/audio/{filename}"
    gen_dir = os.path.join(get_app_data_dir(), "generated_tts")
    fp = os.path.join(gen_dir, filename)
    sz = os.path.getsize(fp) if os.path.exists(fp) else 0

    new_item = {
        "id": id,
        "text": text,
        "voice": voice,
        "filename": filename,
        "audioUrl": audio_url,
        "timestamp": timestamp,
        "format": format,
        "duration": duration,
        "size": sz,
        "formattedSize": format_file_size(sz),
    }
    items = [new_item] + [i for i in items if i.get("id") != id]
    save_tts_history(items[:50])
    return new_item


@router.delete("/history/{item_id}")
async def delete_history_item(item_id: str):
    """Xoá 1 mục lịch sử và xoá file audio tương ứng trên đĩa."""
    items = load_tts_history()
    target = next((i for i in items if i.get("id") == item_id), None)
    if target:
        fn = target.get("filename")
        if fn:
            gen_dir = os.path.join(get_app_data_dir(), "generated_tts")
            fp = os.path.join(gen_dir, fn)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass
        items = [i for i in items if i.get("id") != item_id]
        save_tts_history(items)
    return {"status": "deleted"}


@router.delete("/history")
async def clear_all_history():
    """Xoá toàn bộ lịch sử và dọn dẹp các file audio trong thư mục generated_tts."""
    items = load_tts_history()
    gen_dir = os.path.join(get_app_data_dir(), "generated_tts")
    for item in items:
        fn = item.get("filename")
        if fn:
            fp = os.path.join(gen_dir, fn)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass
    save_tts_history([])
    return {"status": "cleared"}


@router.post("/open-folder")
async def open_tts_folder():
    """Mở thư mục chứa các file audio TTS bằng File Explorer của hệ điều hành."""
    import sys
    gen_dir = os.path.join(get_app_data_dir(), "generated_tts")
    os.makedirs(gen_dir, exist_ok=True)
    abs_path = os.path.abspath(gen_dir)
    try:
        if os.name == "nt":
            os.startfile(abs_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", abs_path])
        else:
            subprocess.Popen(["xdg-open", abs_path])
        return {"status": "ok", "path": abs_path}
    except Exception as e:
        logger.error(f"Error opening folder {abs_path}: {e}")
        raise HTTPException(status_code=500, detail=L(f"Không thể mở thư mục: {e}", f"Cannot open the folder: {e}"))



