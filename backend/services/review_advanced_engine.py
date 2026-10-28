"""Engine cho CHE DO REVIEW NANG CAO.

Khac biet coi loi so voi engine Review co dien:

  * Giong sinh o TOC DO NGUOI DUNG CHON, giu nguyen do dai that — khong ep cho vua
    canh, khong tu tang toc, khong cat duoi. Day thuan tuy la "kho nguyen lieu".
  * KHONG chay xe dich thong minh luc render. Vi tri tren timeline (start_override /
    end_override) la vi tri cuoi cung, khong ai duoc ghi de.
  * Duong dan audio bam theo ``uid`` chu khong phai ``B{b_id}`` -> chen/xoa/cat cau
    thoai mai ma giong khong nhay lung tung.
  * Tang toc cho khop la MOT NUT BAM, ghi ra file DAN XUAT, khong dung file goc.

NGUYEN TAC: module nay CHI THEM MOI. Duong Review co dien khong bi sua mot dong nao.
Cac ham tro giup nang (chay TTS, tron audio, goi ffmpeg huy duoc, sinh SRT) duoc TAI
SU DUNG tu ReviewEngine thay vi chep lai.
"""
from utils.i18n import L
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import time

import imageio_ffmpeg

from services.review_engine import ReviewEngine, RenderCancelled
from services.review_render_cache import resolve_b_time
from services.config_service import get_config_service
from utils.file_utils import safe_replace, safe_remove
from services import review_timeline as TL
from services import review_audio_clips as AUD
from services import review_video_clips as VID
from services import review_media_library as LIB
from services import review_advanced_prompts as PROMPTS
from services import review_advanced_segment as SEG

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger("ReviewEngine")  # chung logger -> vao synthesis.log

HISTORY_DIRNAME = "timeline_history"
# Ngan xep LAM LAI: moi lan hoan tac, trang thai dang co duoc day vao day.
REDO_DIRNAME = "timeline_redo"
HISTORY_KEEP = 20

_engine = ReviewEngine()


# --------------------------------------------------------------------------- #
#  Nap / ghi du an
# --------------------------------------------------------------------------- #
def project_dir_of(project_id: str) -> str:
    return os.path.join(get_config_service().get_review_root(), str(project_id))


def load_project(project_id: str, migrate: bool = True) -> dict:
    """Nap metadata + a_chunks + b_chunks cua 1 du an nang cao.

    Tu dong gan uid cho cac cau con thieu (di tru du an cu) va ghi lai neu co thay doi.
    """
    pdir = project_dir_of(project_id)
    meta_path = os.path.join(pdir, "metadata.json")
    a_path = os.path.join(pdir, "a_chunks.json")

    if not os.path.exists(meta_path):
        raise FileNotFoundError(L(f"Không tìm thấy dự án {project_id}", f"Project {project_id} not found"))

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    a_lookup = {}
    if os.path.exists(a_path):
        with open(a_path, "r", encoding="utf-8") as f:
            for a in json.load(f):
                a_lookup[str(a.get("a_id"))] = a

    b_chunks = TL.load_b_chunks(pdir)
    if migrate and b_chunks:
        if TL.ensure_uids(b_chunks) > 0:
            TL.save_b_chunks(pdir, b_chunks)

    # Lan video: du an cu chua co thi tao MOT clip phu kin video goc, de tu day tro di
    # moi noi chi phai doc mot mo hinh duy nhat. Chi ghi dia dung lan dau.
    if migrate and TL.is_advanced(meta):
        try:
            from services.separation_service import SeparationService

            #  DU AN TRONG (chua co phim goc): bo qua ca hai buoc di tru. KHONG duoc goi
            #  ``resolve_source_media("")`` — no giai chuoi rong ra THU MUC LAM VIEC roi
            #  nem loi, nen moi lan nap du an lai ghi mot dong canh bao vo nghia
            #  ("Vị trí file video gốc không hợp lệ: .../backend").
            raw_path = (meta.get("video_path") or "").strip()
            vpath = (
                SeparationService.resolve_source_media(raw_path)[0] if raw_path else ""
            )
            changed = VID.ensure_migrated(meta, vpath)
            # VIDEO GOC cung la mot muc trong KHO NGUYEN LIEU: mo du an len la thay no o
            # day (truoc day kho trong tron, tep vua them khong hien o dau ca), va xoa clip
            # khoi timeline roi van them lai duoc. Chay dung mot lan (xem LIB.ensure_source_item).
            try:
                if LIB.ensure_source_item(pdir, meta, vpath):
                    changed = True
            except Exception as e:
                logger.warning(L(f"[nâng cao] Không đưa được video gốc vào kho nguyên liệu: {e}", f"[advanced] Could not add the source video to the material library: {e}"))
            if changed:
                save_meta(pdir, meta)
        except Exception as e:
            logger.warning(L(f"[nâng cao] Không dựng được làn video cho dự án cũ: {e}", f"[advanced] Could not build the video lane for the old project: {e}"))

    return {"dir": pdir, "meta": meta, "a_lookup": a_lookup, "b_chunks": b_chunks}


def save_meta(project_dir: str, meta: dict) -> None:
    path = os.path.join(project_dir, "metadata.json")
    tmp = path + ".adv.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    safe_replace(tmp, path)


def commit_chunks(project_dir: str, b_chunks: list, a_lookup: dict,
                  snapshot_first: bool = True) -> list:
    """Sap theo thoi gian bat dau -> danh so b_id 1..N -> ghi xuong dia.

    Bat buoc di qua day moi lan luu, vi SRT xuat theo THU TU MANG chu khong sap theo
    thoi gian (xem ``_generate_srt_overlay``); khong sap lai thi file SRT se co dong
    nhay lui thoi gian.
    """
    if snapshot_first:
        take_snapshot(project_dir)
    ordered = TL.sort_and_renumber(b_chunks, a_lookup)
    TL.save_b_chunks(project_dir, ordered)
    return ordered


# --------------------------------------------------------------------------- #
#  Anh chup / hoan tac
# --------------------------------------------------------------------------- #
def _history_dir(project_dir: str) -> str:
    d = os.path.join(project_dir, HISTORY_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _redo_dir(project_dir: str) -> str:
    d = os.path.join(project_dir, REDO_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _capture_state(project_dir: str) -> dict:
    """Chup TOAN BO thu nguoi dung dung tren timeline.

    Gom CA BA LAN: cau B (b_chunks.json), cac doan nhac va cac clip video (nam trong
    metadata.json), CONG THEM cong tac "tieng goc" — no bi thao tac "dua stem len
    timeline" tu dong tat, nen phai lui lai duoc cung mot nhip.

    LICH SU LOI O DAY — DUNG LAP LAI: ban dau chi chup b_chunks nen Ctrl+Z khong dung
    toi lan nhac; sau do them audio_clips nhung lai quen lan VIDEO khi lam chang 2, nen
    cat/keo/xoa clip video xong bam hoan tac cung khong co gi xay ra; lan thu ba la
    ``source_audio_enabled`` — hoan tac buoc them stem thi stem mat ma tieng goc VAN
    tat, xuat ra video im lang ma khong co canh bao nao; lan thu tu la ``a_chunks`` —
    tu khi doi toc do clip keo luon dai phu de goc theo (xem ``reflow_for_span_change``)
    thi no khong con la du lieu bat bien nua. Them thu gi nguoi dung sua duoc tren
    timeline thi PHAI them vao day.
    """
    state = {
        "b_chunks": TL.load_b_chunks(project_dir),
        "a_chunks": SEG.load_a_chunks(project_dir),
        "audio_clips": [],
        "video_clips": [],
    }
    meta_path = os.path.join(project_dir, "metadata.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            state["audio_clips"] = AUD.list_clips(meta)
            state["video_clips"] = VID.list_clips(meta)
            state["source_audio_enabled"] = bool(meta.get("source_audio_enabled", True))
            # Bang PHAN VAI: doi giong mot vai la doi giong CA TRAM cau — phai lui lai duoc.
            state[VOICE_CAST_FIELD] = dict(meta.get(VOICE_CAST_FIELD) or {})
        except Exception:
            pass
    return state


def _restore_state(project_dir: str, state) -> None:
    """Ghi lai trang thai da chup. Ban chup CU (chi la mang b_chunks) van doc duoc."""
    if isinstance(state, list):            # dinh dang cu
        TL.save_b_chunks(project_dir, state)
        return
    # Du an chua he co kich ban (dang ghep phim truoc): anh chup rong ma van ghi thi
    # sinh ra mot ``b_chunks.json`` rong o cho truoc do KHONG co tep nao — cac cho khac
    # doc "tep co ton tai" de biet du an da co kich ban chua se hieu sai. Khong co gi de
    # khoi phuc thi khong dung den tep.
    _b = state.get("b_chunks") or []
    if _b or os.path.exists(TL.b_chunks_path(project_dir)):
        TL.save_b_chunks(project_dir, _b)
    # Ban chup cu khong co khoa nay -> GIU NGUYEN phu de goc dang co, dung ghi thanh
    # rong (cung bay da mac voi ``video_clips``: hoan tac mot phat mat sach du lieu).
    if "a_chunks" in state:
        SEG.save_a_chunks(project_dir, state.get("a_chunks") or [])
    meta_path = os.path.join(project_dir, "metadata.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta[AUD.FIELD] = state.get("audio_clips") or []
            # Ban chup cu (truoc chang 2) khong co khoa nay -> GIU NGUYEN lan video dang
            # co, dung ghi thanh rong (se lam mat het clip video khi hoan tac).
            if "video_clips" in state:
                meta[VID.FIELD] = state.get("video_clips") or []
            if "source_audio_enabled" in state:
                meta["source_audio_enabled"] = bool(state["source_audio_enabled"])
            if VOICE_CAST_FIELD in state:
                meta[VOICE_CAST_FIELD] = dict(state[VOICE_CAST_FIELD] or {})
            save_meta(project_dir, meta)
        except Exception as e:
            logger.error(L(f"[nâng cao] Không khôi phục được làn nhạc / làn video: {e}", f"[advanced] Could not restore the music / video lane: {e}"))


def _write_snapshot(dir_path: str, state: dict) -> str:
    """Ghi MOT anh chup (da co san) vao ngan xep va don bot ban qua cu."""
    dst = os.path.join(dir_path, f"{int(time.time() * 1000000)}.json")
    try:
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(L(f"[nâng cao] Không chụp được ảnh timeline: {e}", f"[advanced] Could not capture the timeline snapshot: {e}"))
        return ""
    snaps = sorted(os.listdir(dir_path))
    for old in snaps[:-HISTORY_KEEP]:
        safe_remove(os.path.join(dir_path, old))
    return dst


def _push(dir_path: str, project_dir: str) -> str:
    """Cat trang thai hien tai vao mot ngan xep (hoan tac hoac lam lai)."""
    if not os.path.isdir(project_dir):
        return ""
    return _write_snapshot(dir_path, _capture_state(project_dir))


def _pop(dir_path: str, project_dir: str) -> bool:
    """Lay ban moi nhat trong ngan xep ra va khoi phuc."""
    snaps = sorted(os.listdir(dir_path)) if os.path.isdir(dir_path) else []
    if not snaps:
        return False
    last = os.path.join(dir_path, snaps[-1])
    try:
        with open(last, "r", encoding="utf-8") as f:
            state = json.load(f)
        _restore_state(project_dir, state)
        safe_remove(last)
        return True
    except Exception as e:
        logger.error(L(f"[nâng cao] Khôi phục ảnh timeline thất bại: {e}", f"[advanced] Failed to restore the timeline snapshot: {e}"))
        return False


def take_snapshot(project_dir: str) -> str:
    """Chup lai b_chunks.json truoc khi doi — de bam Hoan tac la ve nguyen trang.

    Mot thao tac MOI se xoa ngan xep LAM LAI: sau khi hoan tac roi sua tiep, nhanh
    tuong lai cu khong con y nghia (dung quy uoc chung cua moi trinh soan thao).
    """
    return commit_snapshot(project_dir, capture_snapshot(project_dir))


def capture_snapshot(project_dir: str):
    """Chup trang thai vao BO NHO, chua ghi dia.

    Dung khi thao tac CO THE THAT BAI (khong tim thay clip, cho cat khong hop le…):
    chup truoc roi ghi ngay thi mot lan bam nham cung an mot buoc hoan tac rong VA xoa
    sach ngan xep lam lai — nguoi dung phai bam Ctrl+Z hai lan moi lui that duoc.
    Goi ``commit_snapshot`` sau khi thao tac da thanh cong.

    CHAN THEO THU MUC DU AN, KHONG PHAI THEO ``b_chunks.json``.
    LICH SU LOI — DUNG LAP LAI: cai chan cu (``b_chunks.json`` phai ton tai) la di san tu
    hoi lich su chi luu moi b_chunks. Tu khi anh chup gom ca lan video / lan nhac /
    a_chunks thi no thanh loi that: du an MOI chua co kich ban (0 cau) thi tep do chua
    ton tai, nen MOI thao tac tren lan video — cat clip, xoa, keo, nhan ban — deu chup
    hut. Nguoi dung cat clip xong bam Ctrl+Z chi nhan duoc "Khong con thao tac nao de
    hoan tac", trong khi viec da lam that. Dung giai doan hay dung nhat: ghep phim truoc,
    viet kich ban sau.
    """
    if not os.path.isdir(project_dir):
        return None
    return _capture_state(project_dir)


def commit_snapshot(project_dir: str, state) -> str:
    """Ghi anh chup da lay bang ``capture_snapshot`` vao ngan xep hoan tac."""
    if state is None:
        return ""
    path = _write_snapshot(_history_dir(project_dir), state)
    redo = _redo_dir(project_dir)
    for f in os.listdir(redo):
        safe_remove(os.path.join(redo, f))
    return path


def undo_last(project_dir: str) -> bool:
    """Lui mot buoc. Trang thai dang co duoc day sang ngan xep LAM LAI."""
    hist = _history_dir(project_dir)
    if not (os.path.isdir(hist) and os.listdir(hist)):
        return False
    _push(_redo_dir(project_dir), project_dir)
    return _pop(hist, project_dir)


def redo_last(project_dir: str) -> bool:
    """Tien lai mot buoc vua hoan tac. Trang thai hien tai quay ve ngan xep hoan tac."""
    redo = _redo_dir(project_dir)
    if not (os.path.isdir(redo) and os.listdir(redo)):
        return False
    _push(_history_dir(project_dir), project_dir)
    return _pop(redo, project_dir)


def history_state(project_dir: str) -> dict:
    """Con gi de hoan tac / lam lai khong — FE dua vao day de bat tat nut."""
    hist = _history_dir(project_dir)
    redo = _redo_dir(project_dir)
    return {
        "can_undo": bool(os.path.isdir(hist) and os.listdir(hist)),
        "can_redo": bool(os.path.isdir(redo) and os.listdir(redo)),
    }


# --------------------------------------------------------------------------- #
#  Sinh giong (kho nguyen lieu)
# --------------------------------------------------------------------------- #
def _resolve_rate(meta: dict, b_data: dict) -> str:
    """Toc do doc cua 1 cau: uu tien cai dat rieng cua cau, sau do mac dinh du an.

    'auto' o che do nang cao = 'khong ep gi ca' -> +0%. Viec khop thoi gian do nguoi
    dung quyet dinh tren timeline (keo tha) hoac bang nut Tang toc cho khop.
    """
    rate = (b_data.get("rate") or meta.get("default_rate") or "auto")
    return "+0%" if rate == "auto" else rate


# --------------------------------------------------------------------------- #
#  GIONG DOC CUA TUNG CAU — video nhieu nhan vat
# --------------------------------------------------------------------------- #
#  Bang PHAN VAI cua du an: {nhan vai -> ma giong}. Nam trong metadata.json.
VOICE_CAST_FIELD = "voice_cast"

#  4 MUC AM LUONG cua du an + co "nguoi dung DA CHINH".
PROJECT_MIX_KEYS = ("bg_volume", "voice_bg_volume", "vocals_volume", "voice_vocals_volume")
MIX_TOUCHED_FIELD = "source_mix_touched"

#: MUC MAC DINH cua 4 nut am luong — dung cho du an moi VA cho luc bat "Tach giong".
#:
#: Nhac/am goc nghe nguyen 100% luc AI khong noi va ha con 25% luc AI noi; giong goc
#: (chi co khi da tach) IM HAN vi da co giong AI doc thay. Dung bang chung voi
#: ``batch_store.DEFAULT_CONFIG`` — hai duong lech nhau thi chay le mot kieu, chay lo
#: mot kieu.
MIX_DEFAULTS = {
    "bg_volume": 1.0,
    "voice_bg_volume": 0.25,
    "vocals_volume": 0.0,
    "voice_vocals_volume": 0.0,
}
#: Bo muc cua du an CU (truoc 09/2026): ca bon deu 0. Van phai nhan ra la "chua chinh",
#: khong thi ban xuat cua ho bo duong ``-c copy`` roi tron voi bg=0 -> video IM LANG.
_MIX_DEFAULTS_CU = {k: 0.0 for k in PROJECT_MIX_KEYS}

#  AM LUONG LAN GIONG DOC AI (metadata.json). Truoc day la hang so 1.5 nam trong
#  ``render()``, khong ai chinh duoc — va te hon: khung xem truoc KHONG ap he so do (the
#  <audio> de nguyen 1.0) nen nghe thu bao gio cung nho hon thanh pham. Gio la mot muc
#  chinh duoc, va CA HAI ben cung doc no.
TTS_VOLUME_FIELD = "tts_volume"
DEFAULT_TTS_VOLUME = 1.5


def tts_volume(meta: dict) -> float:
    """Muc am luong giong doc cua du an (1.0 = 100%). Mac dinh giu dung 1.5 nhu truoc."""
    return TL.clamp_volume((meta or {}).get(TTS_VOLUME_FIELD), DEFAULT_TTS_VOLUME)


def mix_touched(meta: dict) -> bool:
    """Nguoi dung DA tu chinh 4 muc am luong cua du an chua.

    VI SAO KHONG XET "khoa co trong metadata": ``review_service.create_project`` GHI SAN ca
    bon khoa cho moi du an, nen su co mat cua chung khong noi len dieu gi.

    Hai dau hieu, du mot la coi nhu da chinh:
      * co ``source_mix_touched`` — ``PATCH /settings`` dat khi nguoi dung gui bat ky muc nao
        (nho no ma dat dung muc mac dinh CO Y cung duoc ton trong);
      * bo 4 muc KHAC ca hai bo mac dinh (bo moi ``MIX_DEFAULTS`` va bo cu toan 0).

    VI SAO PHAI NHO CA BO CU: truoc 09/2026 du an moi tao co ca bon muc = 0. Neu chi so
    voi bo moi thi moi du an cu bong thanh "da chinh" -> ban xuat bo duong ``-c copy``
    roi di tron voi bg=0, tuc video IM LANG. Do la loi im lang, khong ai bao gio thay
    thong bao nao ca.

    Dung o dau: ``render()`` chi duoc di duong tat ``-c copy`` (bo qua het 4 muc) khi 4 muc
    con NGUYEN MAC DINH. Da chinh roi ma van sao y video goc thi keo slider xong xuat ra
    khong doi gi — dung cai loi nguoi dung bao "sua tieng o day khong nghe duoc".
    """
    meta = meta or {}
    if meta.get(MIX_TOUCHED_FIELD):
        return True

    def _bang(mau: dict) -> bool:
        for k in PROJECT_MIX_KEYS:
            try:
                if abs(float(meta.get(k) or 0.0) - mau[k]) > 0.001:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    #  Khop DUNG mot trong hai bo mac dinh -> coi nhu chua ai dung toi.
    return not (_bang(MIX_DEFAULTS) or _bang(_MIX_DEFAULTS_CU))

#  Tuong thich nguoc: che do co dien ghi GIOI TINH vao ``b["voice"]`` ("nam"/"nữ"), khong
#  phai ma giong. Du an cu chuyen sang nang cao van phai doc dung.
_GENDER_ALIASES = {"nam": "nam", "male": "nam", "nữ": "nu", "nu": "nu", "female": "nu"}
_GENDER_VOICES = {
    "vi": {"nam": "vi-VN-NamMinhNeural", "nu": "vi-VN-HoaiMyNeural"},
    "en": {"nam": "en-US-GuyNeural", "nu": "en-US-AriaNeural"},
    "zh": {"nam": "zh-CN-YunxiNeural", "nu": "zh-CN-XiaoxiaoNeural"},
    "ja": {"nam": "ja-JP-KeitaNeural", "nu": "ja-JP-NanamiNeural"},
    "ko": {"nam": "ko-KR-InJoonNeural", "nu": "ko-KR-SunHiNeural"},
    #  Thêm ở 2.15.0 — thiếu thì dự án tiếng Đức/Pháp… rơi về giọng vi-VN-NamMinhNeural.
    "fr": {"nam": "fr-FR-HenriNeural", "nu": "fr-FR-DeniseNeural"},
    "de": {"nam": "de-DE-ConradNeural", "nu": "de-DE-KatjaNeural"},
    "es": {"nam": "es-ES-AlvaroNeural", "nu": "es-ES-ElviraNeural"},
    "ru": {"nam": "ru-RU-DmitryNeural", "nu": "ru-RU-SvetlanaNeural"},
    "pt": {"nam": "pt-BR-AntonioNeural", "nu": "pt-BR-FranciscaNeural"},
    "it": {"nam": "it-IT-DiegoNeural", "nu": "it-IT-ElsaNeural"},
}
FALLBACK_VOICE = "vi-VN-NamMinhNeural"


def speaker_of(b_data: dict) -> str:
    """Nhan VAI cua mot cau. Cau khong ghi vai -> vai mac dinh (``MC``).

    Nho vay du an cu (chua co truong nay) van hien MOT vai duy nhat trong bang Phan vai,
    gan giong cho no la ap cho toan bo — khong phai sua tung cau.
    """
    return (str((b_data or {}).get("speaker") or "").strip()) or PROMPTS.DEFAULT_SPEAKER


def resolve_voice(meta: dict, b_data: dict) -> str:
    """GIONG THAT SE DOC cau nay. Hàm RIENG cua che do nang cao — dung nham
    ``ReviewEngine._get_voice`` la sai.

    VI SAO PHAI CO HAM NAY: ``_get_voice`` cua che do co dien coi ``b["voice"]`` la GIOI
    TINH ("nam"/"nữ") roi tra bang; con o trinh dung, bang chon giong ghi vao day MA GIONG
    THAT (``vi-VN-HoaiMyNeural``, ``vieneu:...``) — khong khop bang nen no roi ve giong cua
    ca du an. Nang hon: ``voice_signature`` cung goi ham do, nen doi giong mot cau con
    KHONG bi danh dau lech -> bam sinh lai van ra giong cu, khong mot canh bao nao. Ham do
    cung tra thang giong du an neu du an dung mot giong VieNeu, tuc bo qua het.

    Thu tu uu tien:
      1. ``b["voice"]`` — nguoi dung chon tay cho DUNG cau nay (uu tien tuyet doi).
      2. ``voice_cast[speaker]`` — giong cua VAI ma cau nay thuoc ve.
      3. Giong mac dinh cua du an.
    """
    raw = str((b_data or {}).get("voice") or "").strip()
    if raw:
        gender = _GENDER_ALIASES.get(raw.lower())
        if gender:
            lang = str(
                (b_data or {}).get("target_language")
                or (meta or {}).get("target_language")
                or "vi"
            ).lower()[:2]
            mapped = _GENDER_VOICES.get(lang, {}).get(gender)
            if mapped:
                return mapped
        else:
            return raw               # da la ma giong that
    cast = (meta or {}).get(VOICE_CAST_FIELD) or {}
    if isinstance(cast, dict):
        chosen = str(cast.get(speaker_of(b_data)) or "").strip()
        if chosen:
            return chosen
    return (meta or {}).get("target_voice") or FALLBACK_VOICE


def speakers_in_use(b_chunks: list, meta: dict = None) -> list:
    """Danh sach VAI dang co tren timeline, kem so cau va giong da gan.

    Sap theo SO CAU giam dan: vai noi nhieu nhat len dau — do la vai nguoi dung muon chon
    giong truoc tien.
    """
    cast = ((meta or {}).get(VOICE_CAST_FIELD) or {}) if meta else {}
    counts = {}
    for b in b_chunks or []:
        name = speaker_of(b)
        counts[name] = counts.get(name, 0) + 1
    out = [
        {
            "speaker": name,
            "count": n,
            "voice": str((cast or {}).get(name) or "").strip(),
        }
        for name, n in counts.items()
    ]
    out.sort(key=lambda x: (-x["count"], x["speaker"]))
    return out


def group_jobs_by_voice(jobs: list) -> list:
    """Sap job TTS sao cho cac cau CUNG MOT GIONG nam lien nhau.

    VI SAO QUAN TRONG: doi giong khong he mien phi — VieNeu phai thao/nap lai LoRA adapter
    cho tung giong (``unload_lora_adapter`` + ``PeftModel.from_pretrained``). Xen ke A-B-A-B
    la nap lai nhieu lan vo ich; chay het vai A roi moi sang vai B thi mỗi vai chi nap MOT
    lan. ``ReviewEngine._run_tts_jobs`` cung tu gom theo giong, nhung gom san tu day de
    (a) khong phu thuoc vao chi tiet cai dat cua lop duoi, (b) nhat ky doc ra dung thu tu
    thuc te dang chay.

    Giu nguyen thu tu xuat hien cua tung giong va thu tu cau trong moi giong (on dinh, de
    doi chieu nhat ky).
    """
    order, buckets = [], {}
    for j in jobs:
        v = j.get("voice") or ""
        if v not in buckets:
            buckets[v] = []
            order.append(v)
        buckets[v].append(j)
    return [j for v in order for j in buckets[v]]


def voice_signature(meta: dict, b_data: dict) -> str:
    """Dau van tay cua giong SE sinh ra cho 1 cau: chu + giong + toc do.

    Bam dung HAI gia tri se truyen cho TTS (``_get_voice`` va ``_resolve_rate``) nen
    khong the lech voi thu thuc su duoc doc len. So sanh no voi dau van tay da luu
    (``TL.VOICE_SIG_FIELD``) la biet ngay audio tren dia con dung chu hay khong.

    KHONG gom thoi diem start/end: keo tha chi doi VI TRI, khong lam giong sai — cung
    tinh than voi ``compute_tts_fingerprint`` cua che do co dien.

    Dung ``resolve_voice`` (ham rieng cua nang cao) chu KHONG dung ``_engine._get_voice``:
    ham do khong hieu ma giong tung cau nen dau van tay khong doi khi nguoi dung doi giong
    -> cau khong bi danh dau lech -> sinh lai van ra giong cu.
    """
    text = (b_data.get("b_text") or b_data.get("text") or "").strip()
    voice = resolve_voice(meta, b_data) or ""
    rate = _resolve_rate(meta, b_data)
    raw = f"{text}\x1f{voice}\x1f{rate}"
    #  Giọng Qwen3-TTS: thêm phiên bản cách sinh + vân tay mẫu clone. CHỈ khi là Qwen —
    #  Edge/VieNeu giữ nguyên vân tay cũ, không thì mọi dự án cũ bị đánh dấu lệch.
    from services.tts_engines import engine_cache_tag

    tag = engine_cache_tag(voice)
    if tag:
        raw += f"\x1f{tag}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def signature_fn(meta: dict):
    """Ham tinh dau van tay cho 1 cau — truyen cho ``TL.build_timeline``."""
    return lambda b: voice_signature(meta, b)


def backfill_voice_sigs(project_dir: str, meta: dict, b_chunks: list) -> int:
    """Dong dau van tay cho nhung cau DA CO GIONG ma chua co ``voice_sig``.

    Goi NGAY TRUOC khi doi giong mac dinh / toc do mac dinh / bang phan vai, va phai
    truyen ``meta`` CU (chua ap thay doi).

    VI SAO CAN — lo hong da do duoc: ``build_tts_jobs`` co y bo qua cau khong co dau van
    tay ("du an sinh truoc ban nay -> tin la con dung") de khong bat ca du an cu sinh lai
    hang loat vo co. Mat trai: doi giong mac dinh xong bam "Sinh giong" thi dung nhung
    cau do bi bo qua AM THAM — van doc bang giong cu, khong badge "cần sinh lại", khong
    mot canh bao nao. Dong dau van tay theo cau hinh CU ngay truoc khi doi thi tu do co
    che phat hien lech chay dung nhu voi du an moi.

    Chi cham vao cau THUC SU da co file giong: cau chua sinh thi khong co gi de mo ta.
    """
    n = 0
    for b in b_chunks or []:
        if b.get(TL.VOICE_SIG_FIELD):
            continue
        p = TL.clip_audio_path(project_dir, b)
        if not (os.path.exists(p) and os.path.getsize(p) > 0):
            continue
        b[TL.VOICE_SIG_FIELD] = voice_signature(meta, b)
        n += 1
    return n


def build_tts_jobs(project_dir: str, meta: dict, b_chunks: list,
                   uids=None, force: bool = False) -> list:
    """Danh sach viec sinh giong.

    Bo qua cau da co file audio VA dau van tay con khop. Cau nao doi chu / doi giong /
    doi toc do sau khi sinh thi VAN sinh lai du khong bat ``force`` — nho vay nut
    "Sinh giong" mot phat lo duoc het, khong bat nguoi dung bam tung cau.

    Danh sach tra ve DA GOM THEO GIONG (xem ``group_jobs_by_voice``): chay het vai A roi
    moi sang vai B, de model giong chi phai nap mot lan cho moi vai.
    """
    wanted = set(uids) if uids else None
    jobs = []
    for b in b_chunks:
        uid = TL.get_uid(b)
        if wanted is not None and uid not in wanted:
            continue
        text = (b.get("b_text") or b.get("text") or "").strip()
        if not text:
            continue
        sig = voice_signature(meta, b)
        path = TL.clip_audio_path(project_dir, b)
        if not force and os.path.exists(path) and os.path.getsize(path) > 0:
            stored = b.get(TL.VOICE_SIG_FIELD)
            # Chua co dau van tay = du an sinh truoc ban nay -> tin la con dung, khong
            # tu y sinh lai ca du an cu.
            if not stored or stored == sig:
                continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        jobs.append({
            "b_id": b.get("b_id"),
            "uid": uid,
            "text": text,
            "path": path,
            # 1e9 = "khong ep vao khung nao ca" — nhanh sinh 1 lan, giu do dai that.
            "target_duration": 1e9,
            "voice": resolve_voice(meta, b),
            "speaker": speaker_of(b),
            "base_rate": _resolve_rate(meta, b),
            "sig": sig,
        })
    return group_jobs_by_voice(jobs)


async def generate_voices(project_id: str, uids=None, force: bool = False) -> dict:
    """Sinh giong hang loat (hoac cho mot so cau chi dinh).

    Giong luon sinh o tocp do da chon, KHONG cat, KHONG tu tang toc.
    """
    p = load_project(project_id)
    jobs = build_tts_jobs(p["dir"], p["meta"], p["b_chunks"], uids=uids, force=force)
    if not jobs:
        return {"total": 0, "ok": 0, "failed": 0, "failed_uids": []}

    # Nhat ky noi ro THU TU se chay: het vai nay moi sang vai khac. Doi giong la phai
    # thao/nap lai model (LoRA adapter cua VieNeu), nen gom lai la tiet kiem that.
    by_voice = {}
    for j in jobs:
        by_voice.setdefault(j["voice"], []).append(j)
    plan = " → ".join(
        L(f"{(j[0].get('speaker') or '?')}/{v.split(':')[-1]} ({len(j)} câu)", f"{(j[0].get('speaker') or '?')}/{v.split(':')[-1]} ({len(j)} sentences)")
        for v, j in by_voice.items()
    )
    logger.info(
        L(f"[nâng cao] Bắt đầu tạo giọng cho {len(jobs)} câu bằng {len(by_voice)} giọng "
        f"(giữ nguyên tốc độ, không cắt). Chạy lần lượt từng giọng: {plan}", f"[advanced] Starting voice generation for {len(jobs)} sentences with {len(by_voice)} voices (speed kept as is, no trimming). Running one voice at a time: {plan}")
    )
    await _engine._run_tts_jobs(jobs, project_id=project_id, project_dir=p["dir"])

    ok, failed, done = 0, [], {}
    for j in jobs:
        if not (os.path.exists(j["path"]) and os.path.getsize(j["path"]) > 0):
            failed.append(j["uid"])
            continue
        ok += 1
        done[j["uid"]] = j["sig"]

    if done:
        # DOC LAI tu dia truoc khi ghi. Sinh giong hang loat chay hang phut, trong luc
        # do nguoi dung van keo tha duoc (PUT /timeline ghi thang xuong dia, khong qua
        # khoa nao). Ghi de bang mang dang nam trong bo nho tu dau lan sinh se xoa
        # sach nhung lan keo do. Nen chi cham vao DUNG hai truong cua rieng buoc nay,
        # tren dung nhung cau vua sinh.
        fresh = TL.load_b_chunks(p["dir"])
        changed = False
        for b in fresh:
            sig = done.get(TL.get_uid(b))
            if sig is None:
                continue
            # Dau van tay: tu gio ai sua chu ma khong sinh lai la timeline biet ngay.
            if b.get(TL.VOICE_SIG_FIELD) != sig:
                b[TL.VOICE_SIG_FIELD] = sig
                changed = True
            # Giong vua sinh lai = he so tang toc cu khong con dung nua.
            if b.get("speed_factor") not in (None, 1.0):
                b["speed_factor"] = None
                changed = True
        #  GIONG MOI = DO DAI MOI -> khop lai khung cua DUNG nhung cau vua sinh (giu diem
        #  bat dau, mep phai theo giong moi, khong qua cau sau). Khong co buoc nay thi
        #  khung van la do dai giong CU: sinh lai xong ca loat cau bao "bi cat" hoac thua
        #  mot khoang lang dai (cung loi voi "Toc do moi cau", 25-09-2026).
        #
        #  Them nua (nguoi dung yeu cau 29-09-2026): cau CHUA tung duoc xep (mep con suy tu
        #  phu de goc) ma giong dai hon khung -> DAN MEP PHAI ra khoang trong phia sau, sat
        #  toi dau cau sau. KHONG day diem bat dau cua cau nao. Mep dan co dau
        #  ``end_auto`` -> "Dat lai" tra ve dung mep cu. Bot hang loat cau bao chong lan /
        #  bi cat ngay sau lan sinh dau tien.
        if khop_mep_theo_giong(p["dir"], fresh, p["a_lookup"], p["meta"], uids=set(done),
                               dan_khung_goc=True):
            changed = True
        if changed:
            TL.save_b_chunks(p["dir"], fresh)

    logger.info(L(f"[nâng cao] Tạo giọng xong: {ok}/{len(jobs)} câu thành công.", f"[advanced] Voice generation done: {ok}/{len(jobs)} sentences succeeded."))

    #  ĐẾM LƯỢT GIỌNG NGAY Ở ĐÂY, không để chỗ gọi tự nhớ.
    #
    #  Trước đây việc đếm nằm trong route ``/generate-voices``, nên CHỈ đường bấm nút
    #  trong dự án mới được đếm. Hai đường còn lại cùng gọi hàm này thì không:
    #    · chạy hàng loạt (``batch_routes``) — người dùng thấy "Video 12/1.500" nhảy
    #      đều mà "Lượt tạo giọng" đứng im ở 0, không hiểu vì sao;
    #    · bước Dồn nhờ DeepSeek rút gọn rồi sinh lại giọng (``review_advanced_fit_ai``).
    #  Cả hai đều là lượt giọng THẬT, có tốn của người dùng.
    #
    #  Đặt ở đây thì mọi đường gọi đều được đếm, và đường thứ tư mai này thêm vào cũng
    #  không phải nhớ gì. Đếm hỏng thì KHÔNG được làm hỏng lượt sinh giọng.
    if ok:
        try:
            from services.usage_reporter import bump_voice

            bump_voice(str(project_id), ok)
        except Exception as e:  # noqa: BLE001
            logger.warning(L(f"[nâng cao] Không ghi được thống kê lượt giọng: {e}", f"[advanced] Could not record voice usage stats: {e}"))

    return {"total": len(jobs), "ok": ok, "failed": len(failed), "failed_uids": failed}


# --------------------------------------------------------------------------- #
#  Nhap kich ban B (duong RIENG cua che do nang cao)
# --------------------------------------------------------------------------- #
def _norm_text(t) -> str:
    """Chuan hoa chu de so khop: bo khoang trang thua, khong phan biet chu hoa."""
    return " ".join(str(t or "").split()).lower()


#  Toc do doc UOC LUONG khi phai tu dat khung thoi gian cho mot cau (ky tu / giay). Co y
#  lay thap hon toc do that (~14) de khung rong rai: khung chat hon giong la clip bi to do
#  "se bi cat cut" ngay khi vua nhap kich ban, nhin nhu loi.
FALLBACK_CPS = 11.0
FALLBACK_MIN_WINDOW = 1.5


def place_orphan_chunks(b_chunks: list, a_lookup: dict) -> int:
    """Cap vi tri CU THE cho nhung cau khong the suy ra thoi gian. Tra ve so cau da dat.

    ``resolve_b_time`` tra ``(None, None)`` khi cau vua khong co ``start_override`` vua
    khong tro ve duoc cau A nao — kich ban dan tay thieu ``source_a_ids``, hoac ``a_id``
    da bi danh so lai sau mot lan "Lay lai phu de".

    HAU QUA CU (loi that, im lang hoan toan): ``build_timeline`` ve nhung cau do o giay 0
    va khung xem truoc doc chung binh thuong, con ``render()`` thi ``if start is None:
    continue`` — BO QUA hoan toan. Nguoi dung sinh giong, nghe thu thay du, xuat ra video
    thi khong he co cau ay, khong mot canh bao nao. Nay cap cho no mot cho THAT, xep lan
    luot sau phan da co: thay dung cho no se nam va keo di duoc.

    CHI cham vao cau THAT SU vo chu: cau CO ``source_a_ids`` ma ``a_lookup`` dang rong thi
    DE NGUYEN — do la dau hieu a_chunks.json chua doc duoc (o mang chua ket noi, tep hong),
    ghi override luc do la pha vi tri cua ca du an.
    """
    orphans, cursor = [], 0.0
    for b in b_chunks:
        start, end = resolve_b_time(b, a_lookup or {})
        if start is not None:
            cursor = max(cursor, float(end))
            continue
        if b.get("source_a_ids") and not a_lookup:
            continue                      # a_chunks chua doc duoc -> khong doan bua
        orphans.append(b)

    for b in orphans:
        text = (b.get("b_text") or b.get("text") or "").strip()
        window = max(FALLBACK_MIN_WINDOW, len(text) / FALLBACK_CPS)
        b["start_override"] = round(cursor, 3)
        b["end_override"] = round(cursor + window, 3)
        b.pop(TL.END_AUTO_FIELD, None)
        cursor += window + TL.DEFAULT_GAP

    if orphans:
        logger.warning(
            L(f"[nâng cao] {len(orphans)} câu không suy được thời gian (thiếu source_a_ids) "
            f"— đã đặt tạm nối tiếp nhau từ giây "
            f"{orphans[0].get('start_override', 0):.1f}. Kéo trên timeline để đặt lại.", f"[advanced] {len(orphans)} sentences have no derivable timing (missing source_a_ids) — temporarily placed back to back from second {orphans[0].get('start_override', 0):.1f}. Drag on the timeline to reposition.")
        )
    return len(orphans)


def import_script(project_dir: str, meta: dict, a_lookup: dict, raw_chunks: list,
                  tach_cau: bool = None) -> dict:
    """Nhap kich ban B cho du an nang cao.

    VI SAO KHONG dung ``review_service.import_b_chunks`` cua duong co dien: ham do ghi
    de b_chunks.json bang danh sach KHONG CO ``uid``. Lan mo sau ``ensure_uids`` gan
    lai ``B{b_id}`` theo so thu tu, nen cau thu 7 cua kich ban MOI tro dung thu muc
    ``output/B7`` cua kich ban CU — clip hien "da co giong", nut sinh giong bo qua, va
    video xuat ra doc noi dung cu ma khong ai hay. Dung ca cong dung tren timeline
    (start/end_override, giong rieng, he so toc do) cung mat sach.

    O day:
      * cau nao GIU NGUYEN chu -> dung lai chinh cau cu (uid, vi tri, giong, toc do),
        giong da sinh van dung, khong phai lam lai;
      * cau moi hoac doi chu -> ``uid`` MOI hoan toan, chua co audio, hien "chua co
        giong" -> khong the thua huong giong cua cau nao khac;
      * chup anh truoc khi ghi -> Ctrl+Z lui lai duoc.

    ``tach_cau``: tach khoi ban dich dai theo dau cau + do dai (services/tach_cau.py).
    None = theo o "Tach ban dich" cua du an (metadata ``tach_cau_dich``, mac dinh TAT).
    """
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise ValueError(L("Kịch bản trống hoặc không phải danh sách câu.", "The script is empty or is not a list of sentences."))

    #  TÁCH BẢN DỊCH — phải TRƯỚC bước so chữ bên dưới (xem ``tach_kich_ban``): nhập lại
    #  đúng bản dịch cũ thì từng mảnh khớp mảnh đã tách lần trước, giọng đã sinh còn nguyên.
    from services.tach_cau import bat_dich, tach_kich_ban
    raw_chunks, so_tach = tach_kich_ban(
        raw_chunks, a_lookup, bat=bat_dich(meta) if tach_cau is None else tach_cau
    )

    old = TL.load_b_chunks(project_dir)
    # Nhom cau cu theo chu. Moi cau cu chi duoc dung lai MOT lan: kich ban co hai dong
    # chu y het nhau thi dong thu hai la cau moi, khong phai ban sao cua dong dau.
    pool = {}
    for b in old:
        pool.setdefault(_norm_text(b.get("b_text") or b.get("text")), []).append(b)

    out, reused = [], 0
    dung_lai: set = set()
    for item in raw_chunks:
        if not isinstance(item, dict):
            continue
        text = str(
            item.get("b_text") or item.get("text") or item.get("target_text")
            or item.get("content") or ""
        ).strip()
        a_ids = list(item.get("source_a_ids") or [])
        voice, rate = item.get("voice"), item.get("rate")
        # NHAN VAI do AI dat (video nhieu nhan vat). Nhan "character"/"role" cung duoc —
        # model hay tu doi ten truong, chap nhan het cho khoi bat nguoi dung sua tay.
        speaker = item.get("speaker") or item.get("character") or item.get("role")
        speaker = str(speaker).strip() if speaker else ""

        bucket = pool.get(_norm_text(text)) if text else None
        if bucket:
            chunk = bucket.pop(0)
            #  MẢNH VỪA TÁCH mang khung riêng (``start_override``). Câu cũ cùng chữ mà chưa
            #  có chỗ riêng, hoặc trỏ câu gốc KHÁC (vd "Vâng." ở đoạn khác của phim), thì
            #  đặt theo khung mới — giữ chỗ cũ là mảnh nằm đè lên mảnh bên cạnh. Câu cũ cùng
            #  câu gốc và đã có chỗ (mảnh tách lần trước, có thể đã kéo tay) thì giữ nguyên.
            if "start_override" in item and (
                chunk.get("start_override") is None
                or {str(x) for x in a_ids} != {str(x) for x in (chunk.get("source_a_ids") or [])}
            ):
                chunk["start_override"] = item["start_override"]
                chunk["end_override"] = item["end_override"]
                chunk.pop(TL.END_AUTO_FIELD, None)
            chunk["b_text"] = text
            chunk["source_a_ids"] = a_ids or list(chunk.get("source_a_ids") or [])
            # Kich ban moi noi ro giong / toc do / vai thi theo no; khong thi giu cai
            # nguoi dung da chinh tay tren timeline.
            if voice is not None:
                chunk["voice"] = voice
            if rate is not None:
                chunk["rate"] = rate
            if speaker:
                chunk["speaker"] = speaker
            # Xe dich thong minh con sot tu che do co dien co uu tien CAO NHAT trong
            # resolve_b_time -> phai bo, neu khong no de len vi tri nguoi dung da dat.
            for k in ("shift_start", "shift_end", "shift_audio"):
                chunk.pop(k, None)
            #  Mép TỰ DÃN lúc sinh giọng thuộc về câu gốc CŨ; câu có thể trỏ sang câu gốc
            #  khác -> trả mép về như trước khi dãn (không giữ lại thành mép "kéo tay").
            go_mep_tu_dan(chunk)
            dung_lai.add(TL.get_uid(chunk))
            reused += 1
        else:
            chunk = {
                "uid": TL.new_uid(),      # KHONG bao gio la "B{b_id}"
                "b_id": 0,                # commit_chunks danh so lai
                "b_text": text,
                "source_a_ids": a_ids,
                "voice": voice,
                "rate": rate,
                "speaker": speaker or None,
                "speed_factor": None,
                "is_trimmed": None,
            }
            if "start_override" in item:       # mảnh vừa tách: khung chia theo độ dài chữ
                chunk["start_override"] = item["start_override"]
                chunk["end_override"] = item["end_override"]
        out.append(chunk)

    if not out:
        raise ValueError(L("Không đọc được câu nào trong kịch bản.", "Could not read any sentence in the script."))

    # Câu nào không tro được về câu A thì đặt cho nó một chỗ cụ thể — nếu không, nó nằm ở
    # giây 0 trên timeline nhưng bị bỏ hẳn khỏi bản xuất (xem place_orphan_chunks).
    place_orphan_chunks(out, a_lookup)

    #  Câu GIỮ giọng cũ không vào lượt sinh giọng (vân giọng không đổi) -> không ai dãn lại
    #  mép cho nó: nhập lại kịch bản là mọi câu đang dãn co về khung gốc, báo "bị cắt".
    #  Khớp lại ngay theo khung của câu gốc MỚI (rà soát 29-09-2026).
    if dung_lai:
        try:
            khop_mep_theo_giong(project_dir, out, a_lookup, meta, uids=dung_lai, dan_khung_goc=True)
        except Exception as e:  # noqa: BLE001 — khớp mép hỏng không được chặn việc nhập
            logger.warning(L(f"[nâng cao] Không khớp lại được mép câu giữ giọng: {e}", f"[advanced] Could not re-match the edges of voice-preserved sentences: {e}"))

    take_snapshot(project_dir)
    ordered = commit_chunks(project_dir, out, a_lookup, snapshot_first=False)

    state = meta.setdefault("state", {})
    state["b_imported"] = True
    # Kich ban doi -> ban da dung khong con dung nua, dung de UI moi de len no.
    state["render_complete"] = False
    save_meta(project_dir, meta)

    fresh = len(ordered) - reused
    # VAI chua duoc gan giong: FE nhac nguoi dung mo bang Phan vai. Khong tu gan giong
    # cho vai moi — chon giong la viec cua nguoi dung, doan bừa la sinh ca tram cau sai.
    cast = meta.get(VOICE_CAST_FIELD) or {}
    roles = speakers_in_use(ordered, meta)
    unassigned = [r["speaker"] for r in roles if not r["voice"]]
    logger.info(
        L(f"[nâng cao] Nhập kịch bản: {len(ordered)} câu — {reused} câu giữ nguyên giọng, "
        f"{fresh} câu cần sinh giọng", f"[advanced] Script import: {len(ordered)} sentences — {reused} keep their voice, {fresh} need voice generation")
        + (L(f", {so_tach} khối dài đã tách theo câu", f", {so_tach} long blocks were split by sentence") if so_tach else "")
        + L(f". Vai: {', '.join(r['speaker'] for r in roles) or '(không có)'}", f". Roles: {', '.join(r['speaker'] for r in roles) or '(none)'}")
        + (L(f" — chưa chọn giọng cho: {', '.join(unassigned)}", f" — no voice chosen for: {', '.join(unassigned)}") if unassigned else "")
    )
    return {
        "total": len(ordered),
        "reused": reused,
        "fresh": fresh,
        "split": so_tach,
        "speakers": roles,
        "unassigned_speakers": unassigned,
        "cast_size": len(cast) if isinstance(cast, dict) else 0,
    }


# --------------------------------------------------------------------------- #
#  Nut tu dong 1: don cho khoi chong
# --------------------------------------------------------------------------- #
def _remap_times(project_dir: str, a_lookup: dict, b_chunks: list, remap) -> tuple:
    """Ap MOT phep anh xa thoi gian len ca CAU B lan DAI PHU DE GOC.

    ``remap(start, end) -> (start, end)``. Tra ve ``(so cau B, so cau phu de goc)``.

    VI SAO GOM VAO MOT CHO — hai le, ca hai deu tung sai:

    1. THU TU BAT BUOC. Cau B chua co ``start_override`` lay moc SUY TU cau A. Dung vao
       a_chunks truoc roi moi doc B thi B da mang so MOI, anh xa them lan nua thanh sai
       gap doi. Nen o day CHOT MOC MOI CAU B TRUOC, xong moi sua a_chunks.

    2. DAI PHU DE GOC PHAI DI THEO. Truoc day no bi coi la "du lieu goc, khong sua" nen
       chen mot doan vao dau la hinh va loi binh lui ra sau, con dai phu de goc dung
       nguyen tai cho 0 — lech han voi hinh. Doi toc do clip thi lai co keo no theo, tuc
       hai duong xu ly hai kieu.

    Ai goi ham nay cung PHAI nho: a_chunks gio la du lieu SUA DUOC, nen no da nam trong
    anh chup hoan tac (xem ``_capture_state``).
    """
    # 1. Chot moc hien tai cua moi cau B — TRUOC khi dung vao a_chunks.
    before = []
    for b in b_chunks:
        s0, e0 = resolve_b_time(b, a_lookup)
        before.append(None if s0 is None else (float(s0), float(e0)))

    # 2. Dai phu de goc.
    subs = 0
    a_chunks = SEG.load_a_chunks(project_dir)
    for a in a_chunks:
        try:
            s0, e0 = float(a.get("start")), float(a.get("end"))
        except (TypeError, ValueError):
            continue
        s1, e1 = remap(s0, e0)
        if abs(s1 - s0) < 0.005 and abs(e1 - e0) < 0.005:
            continue
        a["start"] = round(max(0.0, s1), 3)
        a["end"] = round(max(a["start"] + 0.05, e1), 3)
        subs += 1
    if subs:
        SEG.save_a_chunks(project_dir, a_chunks)
        # ``a_lookup`` tro thang vao chinh cac dict nay (xem ``load_project``) — dong bo
        # lai de moi phep doc sau day thay so moi.
        for a in a_chunks:
            key = str(a.get("a_id"))
            if key in a_lookup:
                a_lookup[key].update({"start": a["start"], "end": a["end"]})

    # 3. Cau B — ghi override cu the, dung moc da chot o buoc 1.
    moved = 0
    for b, snap in zip(b_chunks, before):
        if snap is None:
            continue
        s0, e0 = snap
        s1, e1 = remap(s0, e0)
        if abs(s1 - s0) < 0.005 and abs(e1 - e0) < 0.005:
            continue
        b["start_override"] = round(max(0.0, s1), 3)
        b["end_override"] = round(max(b["start_override"] + 0.05, e1), 3)
        # Xe dich cua che do co dien co uu tien cao hon override -> khong xoa la anh xa
        # vua tinh bi de len, cau khong nhuc nhich.
        for k in ("shift_start", "shift_end", "shift_audio", TL.END_AUTO_FIELD):
            b.pop(k, None)
        moved += 1
    return moved, subs


def shift_everything(project_dir: str, meta: dict, a_lookup: dict, b_chunks: list,
                     delta: float) -> dict:
    """Day MOI THU tren timeline lui ve sau ``delta`` giay: cau B, doan nhac, clip video.

    Dung khi CHEN VAO DAU mach phim (doan mo dau, anh bia). ``VID.append_clip`` chi day
    lan VIDEO — neu chi co the thi giong doc va nhac dung nguyen cho, tuc toan bo loi doc
    lech di dung bang do dai doan vua chen. Voi phim ca tram cau thi khong ai keo tay lai
    duoc; day het la viec bat buoc.

    DAI PHU DE GOC cung lui theo: no mo ta canh nao o giay thu may TREN TIMELINE, chen
    mot doan vao dau ma de no dung yen thi lech han voi hinh. Xem ``_remap_times``.
    """
    if abs(delta) < 0.001:
        return {"clips": 0, "audio": 0, "subs": 0}

    moved, subs = _remap_times(
        project_dir, a_lookup, b_chunks, lambda s, e: (s + delta, e + delta)
    )

    n_audio = 0
    for c in AUD.list_clips(meta):
        c["start"] = round(max(0.0, AUD._num(c.get("start")) + delta), 3)
        n_audio += 1

    logger.info(
        L(f"[nâng cao] Chèn vào đầu: đẩy {moved} câu, {subs} câu phụ đề gốc và "
        f"{n_audio} đoạn nhạc lùi {delta:.2f}s cho khớp.", f"[advanced] Insert at start: pushed {moved} sentences, {subs} original subtitle sentences and {n_audio} music clips back by {delta:.2f}s to fit.")
    )
    return {"clips": moved, "audio": n_audio, "subs": subs}


def shift_after(project_dir: str, meta: dict, a_lookup: dict, b_chunks: list,
                at: float, delta: float) -> dict:
    """Day GIONG DOC va NHAC tu giay ``at`` tro di lui thêm ``delta`` giay.

    Dung khi CHEN mot doan video vao GIUA mach phim (xem ``VID.insert_clip_at``): chi day
    lan hinh thi tu diem chen tro di, hinh va tieng lech nhau dung do dai doan vua chen —
    xuat ra moi phat hien, khong co canh bao nao.

    Khac ``shift_everything`` o chỗ: chi cham vao thu NAM TU ``at`` TRO DI, phan truoc diem
    chen phai dung nguyen tai cho.

    DAI PHU DE GOC tu ``at`` tro di cung di theo — cung ly le voi ``shift_everything``.
    Quyet dinh dua tren MOC BAT DAU: cau bat dau truoc ``at`` thi dung nguyen ca hai dau,
    khong bi keo gian ra.
    """
    if abs(delta) < 0.001:
        return {"clips": 0, "audio": 0, "subs": 0}

    def _map(s, e):
        return (s + delta, e + delta) if s >= at - 1e-6 else (s, e)

    moved, subs = _remap_times(project_dir, a_lookup, b_chunks, _map)

    n_audio = 0
    for c in AUD.list_clips(meta):
        if AUD._num(c.get("start")) >= at - 1e-6:
            c["start"] = round(max(0.0, AUD._num(c.get("start")) + delta), 3)
            n_audio += 1

    logger.info(
        L(f"[nâng cao] Dời từ giây {at:.2f}: {moved} câu, {subs} câu phụ đề gốc và "
        f"{n_audio} đoạn nhạc dời {delta:+.2f}s cho khớp.", f"[advanced] Shift from second {at:.2f}: {moved} sentences, {subs} original subtitle sentences and {n_audio} music clips shifted by {delta:+.2f}s to fit.")
    )
    return {"clips": moved, "audio": n_audio, "subs": subs}


def shift_window(project_dir: str, meta: dict, a_lookup: dict, b_chunks: list,
                 frm: float, to: float, delta: float) -> dict:
    """Doi nhung thu NAM TRONG khoang ``[frm, to)`` di ``delta`` giay.

    Dung khi nguoi dung DOI CHO mot clip video: moi thu THUOC VE doan phim do phai di
    theo no — loi binh, nhac, va CA DAI PHU DE GOC. Khac ``shift_after`` (day tat ca tu
    mot diem tro di) — o day chi dung vao phan nam trong doan, thu dung truoc va sau
    deu yen.
    """
    if abs(delta) < 0.001:
        return {"clips": 0, "audio": 0, "subs": 0}

    def _map(s, e):
        return (s + delta, e + delta) if frm - 1e-6 <= s < to - 1e-6 else (s, e)

    moved, subs = _remap_times(project_dir, a_lookup, b_chunks, _map)

    n_audio = 0
    for c in AUD.list_clips(meta):
        s = AUD._num(c.get("start"))
        if frm - 1e-6 <= s < to - 1e-6:
            c["start"] = round(max(0.0, s + delta), 3)
            n_audio += 1

    logger.info(
        L(f"[nâng cao] Dời clip {delta:+.2f}s: kéo theo {moved} câu, {subs} câu phụ đề gốc "
        f"và {n_audio} đoạn nhạc nằm trong đoạn {frm:.2f}–{to:.2f}s.", f"[advanced] Shift clip by {delta:+.2f}s: dragging along {moved} sentences, {subs} original subtitle sentences and {n_audio} music clips within {frm:.2f}–{to:.2f}s.")
    )
    return {"clips": moved, "audio": n_audio, "subs": subs}


def reflow_for_span_change(project_dir: str, b_chunks: list, a_lookup: dict,
                           anchor: float, old_len: float, new_len: float) -> dict:
    """Keo cac cau B theo khi mot clip video bi NOI RA / BOP LAI.

    VI SAO CAN: doi toc do clip lam PHIM dai/ngan ra, nhung moc cua tung cau binh luan
    van nam nguyen cho cu — noi phim ra thi loi binh doa het ve nua dau, khong con an
    khop voi canh nao. Nut "Dồn" khong chua duoc: no chi xep cho khoi chong nhau, khong
    biet gi ve chuyen phim vua dai ra.

    Anh xa (lien tuc, khong dao thu tu):
      · truoc mep dau clip          -> giu nguyen
      · trong khoang CU cua clip    -> gian/co theo dung ty le, neo o mep dau clip
      · sau khoang cu               -> doi cung mot doan ``new_len - old_len``

    CA HAI dau moc cua cau deu di qua anh xa nay, nen KHUNG cua cau cung gian ra theo —
    day moi la thu nguoi dung can: noi phim ra de cau co them cho, giong khoi phai doc
    nhanh cho vua.

    Cau nao nho vay ma khung DA DU CHO giong that thi bo luon he so tang toc — dung muc
    dich cua ca thao tac ("noi ra cho giong doc cham lai"). Dung cung mot phep so sanh
    voi ``auto_speed`` de hai duong khong noi hai kieu.
    """
    if old_len <= 0.05 or abs(new_len - old_len) <= 0.001:
        return {"moved": 0, "unsped": 0, "subs": 0}
    ratio = new_len / old_len
    tail = new_len - old_len
    end_old = anchor + old_len

    def remap(t: float) -> float:
        if t <= anchor:
            return t
        if t <= end_old:
            return anchor + (t - anchor) * ratio
        return t + tail

    # Ca ba lop di qua CUNG mot phep anh xa, va ``_remap_times`` lo dung thu tu (chot
    # moc cau B truoc, roi moi dung vao phu de goc) — bay "nhan hai lan" nam gon o day.
    moved, subs = _remap_times(
        project_dir, a_lookup, b_chunks, lambda s, e: (remap(s), remap(e))
    )

    # Do thoi luong song song truoc (nap cache) — vong duoi chi doc tu cache.
    TL.probe_durations([TL.clip_audio_path(project_dir, b) for b in b_chunks], project_dir)

    # Cau nao nho gian ma DA DU CHO giong that thi bo luon he so tang toc.
    unsped = 0
    for b in b_chunks:
        raw = TL.audio_duration(TL.clip_audio_path(project_dir, b))
        if raw <= 0 or b.get("speed_factor") in (None, 1.0):
            continue
        start, end = resolve_b_time(b, a_lookup)
        if start is None:
            continue
        if raw <= (float(end) - float(start)) + 0.05:
            b["speed_factor"] = None
            unsped += 1

    logger.info(
        L(f"[nâng cao] Nới clip {old_len:.2f}s -> {new_len:.2f}s (×{ratio:.3f}): "
        f"kéo theo {moved} câu bình luận, {subs} câu phụ đề gốc, "
        f"bỏ tăng tốc cho {unsped} câu.", f"[advanced] Stretch clip {old_len:.2f}s -> {new_len:.2f}s (×{ratio:.3f}): dragging along {moved} commentary sentences, {subs} original subtitle sentences, removing the speed-up for {unsped} sentences.")
    )
    return {"moved": moved, "unsped": unsped, "subs": subs}


def _natural_start(b: dict, a_lookup: dict) -> float:
    """MOC GOC cua cau — suy tu phu de goc, KHONG doc ``start_override``.

    Phai bo qua override co y: ham nay chi duoc goi SAU khi ``auto_arrange`` da ghi
    override, nen neu doc override thi "moc tu nhien" hoa ra la moc DA DON — bo quet se
    tinh tren mot the gioi da bi day het sang phai va ket luan "khong phuong an nao co
    ich" (loi da gap: baseline 38 cau ma danh sach phuong an tra ve rong).
    """
    src = [int(x) for x in (b.get("source_a_ids") or []) if str(x).lstrip("-").isdigit()]
    if src:
        a = a_lookup.get(str(min(src)))
        if a is not None:
            v = TL._num(a.get("start"), None)
            if v is not None:
                return float(v)
    # Khong suy duoc tu phu de goc (cau dan tay) -> danh phai dung moc hien tai.
    start, _end = resolve_b_time(b, a_lookup)
    return float(start) if start is not None else 0.0


def moc_tu_nhien_cac_cau(b_chunks: list, a_lookup: dict) -> dict:
    """``{uid: mốc gốc}`` cho MỌI câu — bản theo lô của ``_natural_start``. Ba nơi xếp
    câu (``auto_arrange``, ``_fit_items``, ``fit_ai.cau_chua_vua``) PHẢI lấy mốc từ đây.

    LỖI NGƯỜI DÙNG GẶP (09-10-2026): tách một câu thành 3 (nút "Tách theo câu" / "Cắt
    đôi" / ô "Tách bản dịch") thì cả 3 mảnh GIỮ NGUYÊN ``source_a_ids`` của câu cha.
    ``_natural_start`` tra mốc theo câu A đầu nên trả về CÙNG một mốc cho cả 3 mảnh;
    ``khung_giu_moc`` xếp 3 câu trùng mốc -> hai mảnh đầu 0 giây, rồi được "nới bằng
    giọng" nên cả 3 đè lên nhau tại một điểm trên timeline sau khi bấm "Dọn chồng lấn".

    Luật: các câu B cùng trỏ về ĐÚNG một bộ câu A là ANH EM của một câu đã tách -> chia
    khung phụ đề gốc [A đầu .. A cuối] cho chúng theo ĐỘ DÀI CHỮ bằng đúng công thức lúc
    tách (``TL.chia_khung_theo_chu``), thứ tự theo vị trí đang có trên timeline (chỉ
    dùng override để XẾP THỨ TỰ anh em, KHÔNG lấy làm mốc — xem ghi chú ``_natural_start``).
    Câu đứng một mình thì y như ``_natural_start``.
    """
    ra: dict = {}
    nhom: dict = {}          # bộ câu A -> [(mốc hiện tại, thứ tự trong danh sách, câu B)]
    for idx, b in enumerate(b_chunks):
        src = tuple(sorted(
            int(x) for x in (b.get("source_a_ids") or []) if str(x).lstrip("-").isdigit()
        ))
        a_dau = a_lookup.get(str(src[0])) if src else None
        if a_dau is None or TL._num(a_dau.get("start"), None) is None:
            ra[TL.get_uid(b)] = _natural_start(b, a_lookup)
            continue
        st, _en = resolve_b_time(b, a_lookup)
        nhom.setdefault(src, []).append((float(st) if st is not None else 0.0, idx, b))

    for src, anh_em in nhom.items():
        a_dau = a_lookup[str(src[0])]
        goc = float(TL._num(a_dau.get("start"), 0.0))
        if len(anh_em) == 1:
            ra[TL.get_uid(anh_em[0][2])] = goc
            continue
        a_cuoi = a_lookup.get(str(src[-1])) or a_dau
        cuoi = TL._num(a_cuoi.get("end"), None)
        if cuoi is None or float(cuoi) <= goc:
            cuoi = goc + 0.1
        anh_em.sort(key=lambda h: (h[0], h[1]))
        chu = [(b.get("b_text") or b.get("text") or "").strip() or "x" for _s, _i, b in anh_em]
        for (_s, _i, b), (s, _e) in zip(anh_em, TL.chia_khung_theo_chu(goc, float(cuoi), chu)):
            ra[TL.get_uid(b)] = float(s)
    return ra


#  LUẬT XẾP CÂU — DÙNG CHUNG cho ``auto_arrange``, ``_fit_stats`` (dự báo của hộp chọn
#  phương án) và ``review_advanced_fit_ai.cau_chua_vua`` (chọn câu nhờ AI rút gọn). Ba
#  nơi PHẢI nói cùng một con số, nên chỉ có MỘT bản ở đây.
#
#  LUẬT CỦA NGƯỜI DÙNG (25-09-2026): KHÔNG BAO GIỜ đẩy điểm BẮT ĐẦU của một câu về sau.
#  Mỗi câu bắt đầu ĐÚNG mốc gốc của nó (``_natural_start`` — theo phụ đề gốc); chỉ được
#  nới ĐIỂM KẾT THÚC tới sát mốc bắt đầu của câu SAU (và không quá cuối phim). Giọng dài
#  hơn chỗ đó thì câu đó THIẾU CHỖ — báo ra để tăng tốc giọng / rút gọn / sửa tay, chứ
#  không đẩy câu sau đi.
#
#  Bản trước (tới 2.15.0) đặt ``start = max(mốc gốc, cuối câu trước)``: một câu dài là
#  câu sau bị đẩy lùi, độ lệch dồn dây chuyền — lời bình trôi dần khỏi hình (người dùng
#  thấy câu #25 bắt đầu muộn hơn câu gốc cả giây sau khi bấm "Dọn chồng lấn").
def khung_giu_moc(naturals: list, durs: list, limit: float) -> list:
    """``[(start, end)]`` cho từng câu (đã sắp theo mốc gốc). ``end - start < dur`` là
    câu thiếu chỗ. Thuần tính toán — không đọc đĩa, không sửa gì."""
    n = len(naturals)
    ra = []
    for i in range(n):
        start = float(naturals[i])
        tran = start + float(durs[i])
        if i + 1 < n:
            tran = min(tran, float(naturals[i + 1]))
        if limit > 0:
            tran = min(tran, float(limit))
        ra.append((start, max(start, tran)))
    return ra


#  DON CHONG LAN — ba buoc, va chi doi toc do khi da het cho that
#
#  Ban DAU TIEN cua nut nay chi lam mot viec: day tung cau sang phai cho khoi chong
#  nhau (``cursor`` chi tien, khong bao gio lui). Voi du an ma giong doc DAI HON khung
#  thoi gian goc, phep day do sinh ra hieu ung DOMINO — do lech cong don tu cau nay
#  sang cau sau. Loi that nguoi dung bao: 110 cau, giong 3,18s/cau ma khung goc chi
#  ~2,1s/cau -> cau #11 lech +12s, cau #61 lech +71s, cau cuoi lech +129s; ban xuat
#  thanh 6:00 trong khi phim chi dai 3:51. Nut van bao "Da xep lai" — thanh cong gia.
#
#  GOC RE khong nam o phep day ma o CHO KHONG DU: tong giong dai hon phim gan 1,5 lan.
#  Khong co cach xep nao nhoi vua ma khong chong hoac cat cut. Nen nut nay lam ba buoc,
#  theo thu tu RE TIEN TRUOC:
#
#    BUOC 1+2 — (tu 25-09-2026, luat cua nguoi dung) moi cau GIU NGUYEN moc bat dau goc,
#             chi NOI MEP PHAI toi sat moc bat dau cua cau sau — ``khung_giu_moc``.
#             KHONG con "xep sat nhau" bang cach day cau sau lui lai: ban do lam loi
#             binh troi dan khoi hinh (lech don day chuyen).
#    BUOC 3 — (khong lam o day) Van con cau bi cat -> ``plan_fit_options`` tinh cac
#             phuong an doi toc do de NGUOI DUNG chon; ``apply_fit_plan`` moi ap dung.
#
#  KHONG BAO GIO day cau vuot qua cuoi phim. Cau khong con cho thi de nguyen tai mep
#  cuoi va DEM vao ``cut`` de bao ra — thà báo "còn N câu không đủ chỗ" còn hơn âm thầm
#  tạo bản xuất dài 6:00 mà người dùng chỉ phát hiện lúc xem lại.
def auto_arrange(project_id: str, gap: float = 0.0, snapshot: bool = True,
                 luon_ghi: bool = False) -> dict:
    """Don chong lan: giu moc bat dau goc, chi noi mep phai toi sat cau sau.

    Ghi ra ``start_override`` / ``end_override`` CU THE — nguoi dung van keo chinh tiep
    duoc. Tra ve ca so lieu de FE biet co can mo modal doi toc do hay khong.

    ``snapshot=False`` khi duoc goi TU BEN TRONG mot thao tac lon da chup anh roi
    (``apply_fit_plan``): chup hai lan thi Ctrl+Z phai bam hai lan moi lui that duoc.

    ``luon_ghi=True`` khi ben goi VUA DOI DO DAI GIONG (``apply_fit_plan``,
    ``rut_gon_cho_vua``). BAT BUOC, va day la LOI DA GAP — dung bo:

      Duong "khong du cho -> khong ghi gi ca" ben duoi sinh ra cho NUT DON dung mot
      minh. Nhung khi goi tu ``apply_fit_plan`` thi giong VUA bi doi toc do, nen o
      cua moi cau dang la o cua toc do CU. Khong ghi = giu nguyen mo o cu do: do
      that tren du an 44 cau, chon phuong an "xong han - moi cau du cho" (giong
      x1.06) xong thi 40/44 cau bao "bi cat" moi cau 0,1s, vi o van la o cua x1.10.
      Modal hua 0 cau, timeline do quach 40 cau — dung cai bay "modal du bao mot
      dang, lam xong mot neo" ma ca tep nay canh chung.

    O che do luon ghi, cau khong du cho KHONG bi kep ve moc cuoi phim: con tro van
    tien du do dai giong, nen khong co chuyen ca chuc cau dong cuc tai mot diem
    (chinh la ly do sinh ra duong khong-ghi kia).
    """
    p = load_project(project_id)
    pdir, meta, a_lookup, b_chunks = p["dir"], p["meta"], p["a_lookup"], p["b_chunks"]

    # Do song song truoc (nap cache) — vong duoi chi doc tu cache.
    TL.probe_durations([TL.clip_audio_path(pdir, b) for b in b_chunks], pdir)

    #  MOC TU NHIEN phai lay tu PHU DE GOC, khong phai tu ``start_override``.
    #
    #  LOI DA GAP — dung lap lai: doc ``start_override`` thi moc "tu nhien" hoa ra la moc
    #  CUA LAN DON TRUOC. Lan dau don voi giong 3,18s/cau thi cac moc bi day ra cach nhau
    #  3,18s; sau do tang toc giong xuong 2,19s roi don lai, cac cau VAN o cach nhau
    #  3,18s vi do dang la "moc tu nhien" cua chung — khong bao gio co lai, nen phan
    #  tang toc thanh vo ich va con so cau thieu cho khong giam (do 32/110 thay vi 0).
    #
    #  Lay moc goc thi moi lan don deu tinh lai tu dau, va ``apply_fit_plan`` moi thu
    #  duoc ket qua dung nhu ``plan_arrange_fit`` da du bao (hai ben dung cung mot moc).
    #  Theo LO (``moc_tu_nhien_cac_cau``): cac manh cua mot cau da tach cung tro ve mot cau
    #  A, tinh tung cau mot thi ca ba manh cung mot moc -> chong dong tai mot diem.
    moc = moc_tu_nhien_cac_cau(b_chunks, a_lookup)
    items = []
    for b in b_chunks:
        raw = TL.audio_duration(TL.clip_audio_path(pdir, b))
        dur = TL.effective_audio_duration(b, raw)
        if dur <= 0:
            continue
        items.append({"b": b, "natural": moc[TL.get_uid(b)], "dur": dur})

    if not items:
        return {"moved": 0, "total": 0, "cut": 0, "cut_total": 0.0, "widened": 0}

    items.sort(key=lambda x: x["natural"])

    # Cuoi phim la GIOI HAN CUNG. Du an khong co phim goc (tu ghep anh/clip) thi lay
    # mep phai cua lan video; khong co gi ca thi khong chan (de nguoi dung tu do dung).
    limit = _timeline_limit(meta)
    #  Giong duoc TRAN qua mep khung chung nay giay (``cho_tran_giong``) -> cau chi "thieu cho" khi dai hon khung + tran.
    tran = TL.tran_giong_cho_phep(meta)

    #  DA VUA THI KHONG DON (nguoi dung 09-10-2026): moi cau tren timeline HIEN TAI deu du
    #  cho cho giong (tinh ca phan duoc tran) va khong khung nao de len khung sau -> khong
    #  co gi de don. Truoc day van ghi lai moi cau tu moc goc: cau nguoi dung vua keo tay
    #  bi tra ve cho cu, khung dang rong bi co lai bang giong — bam "Don" ma khong co gi
    #  can don lai thay timeline doi. Chi ap cho NUT DON dung mot minh; ``luon_ghi`` (vua
    #  doi do dai giong) van phai khop lai khung nhu cu.
    if not luon_ghi:
        da_vua, hien = True, []
        for it in items:
            st, en = resolve_b_time(it["b"], a_lookup)
            if st is None:
                continue
            hien.append((float(st), float(en), it["dur"]))
        hien.sort(key=lambda h: h[0])
        for i, (st, en, dur) in enumerate(hien):
            if en - st < dur - tran - 0.05:
                da_vua = False                  # giong dai hon khung + phan tran
                break
            if i + 1 < len(hien) and en > hien[i + 1][0] + 0.05:
                da_vua = False                  # khung de len khung sau
                break
        if da_vua:
            logger.info(
                L(f"[nâng cao] Dọn chồng lấn: {len(items)} câu đều đã đủ chỗ "
                f"(cho tràn {tran:.2f}s) — không cần dồn, giữ nguyên timeline.", f"[advanced] Resolve overlaps: all {len(items)} sentences already have enough room (overflow allowed {tran:.2f}s) — no packing needed, timeline kept as is.")
            )
            return {
                "moved": 0, "total": len(items), "widened": 0,
                "cut": 0, "cut_total": 0.0,
                "applied": False, "da_vua": True,
                "limit": round(limit, 3), "tran": round(tran, 3),
            }

    #  CHUP VAO BO NHO truoc, chi GHI vao ngan xep hoan tac khi that su co sua (xem
    #  ``capture_snapshot``): duong "khong du cho" ben duoi khong sua gi ca, ma chup
    #  ngay thi nguoi dung an mot buoc Ctrl+Z rong VA mat sach ngan xep lam lai.
    snap = capture_snapshot(pdir) if snapshot else None

    # ---- XEP: moi cau GIU MOC BAT DAU goc, chi noi mep phai toi sat cau sau ----
    #  Xem ``khung_giu_moc`` (luat cua nguoi dung). XEP THU TRUOC, chua ghi gi ca — de
    #  con biet co du cho hay khong. ``gap`` khong con dung: khe giua hai cau la khe
    #  cua phu de goc, khong phai do phep xep chen vao.
    for it, (start, end) in zip(
        items, khung_giu_moc([x["natural"] for x in items], [x["dur"] for x in items], limit)
    ):
        it["start"], it["end"], it["cho"] = start, end, end - start

    #  KHONG DU CHO -> KHONG GHI GI CA, chi bao ra so lieu.
    #
    #  LOI NGUOI DUNG DA GAP: khi tong loi binh dai hon phim, phep xep tren day kep moi
    #  cau het cho ve dung moc ``limit``, nen 37 cau cung nam tai giay 231 — chong dong
    #  lai mot diem, timeline phai xep chung thanh 11 lan "Giong (tran)" rong tuech.
    #  Nguoi dung bam mot nut "don chong lan" ma nhan ve mot timeline nhin nhu du lieu
    #  hong, VA chua kip lam gi de sua.
    #
    #  Da thu duong khac (de cau het cho nam lai moc goc) thi con te hon: 62 cau chong
    #  nhau va con so ``cut`` tut xuong 1 — bao ra khong dung su that.
    #
    #  Nen: het cho thi KHONG SUA GI. Tra ve so lieu cho FE mo hop thoai doi toc do;
    #  nguoi dung chon xong, ``apply_fit_plan`` moi xep that (luc do da du cho nen phep
    #  xep tren khong con kep ai vao mep). Du an giu nguyen trang -> khong co gi phai
    #  hoan tac, va khong bao gio thay 11 lan tran.
    dry_cut = sum(1 for it in items if (it["end"] - it["start"]) < it["dur"] - tran - 0.05)
    if dry_cut and not luon_ghi:
        logger.warning(
            L(f"[nâng cao] Dọn chồng lấn: KHÔNG đủ chỗ cho {dry_cut}/{len(items)} câu "
            f"(phim {limit:.0f}s, cho tràn {tran:.1f}s) — chưa xếp lại gì cả, chờ người dùng chọn cách xử lý.", f"[advanced] Resolve overlaps: NOT enough room for {dry_cut}/{len(items)} sentences (film {limit:.0f}s, overflow allowed {tran:.1f}s) — nothing was rearranged, waiting for the user to choose how to proceed.")
        )
        return {
            "moved": 0, "total": len(items), "widened": 0,
            "cut": dry_cut,
            "cut_total": round(
                sum(max(0.0, it["dur"] - tran - (it["end"] - it["start"])) for it in items), 2
            ),
            "applied": False,          # FE: chua sua gi, dung bao "da xep lai"
            "limit": round(limit, 3),
            "tran": round(tran, 3),
        }

    #  CAU KHONG CON CHUT CHO NAO (trung moc voi cau sau, hoac nam sau cuoi phim — che do
    #  luon ghi): khung 0 giay la giong bien mat khoi ban xuat ma khong ai hay. De no dai
    #  bang giong (de len cau sau / thoi ra) cho NHIN THAY duoc tren timeline; van dem la
    #  thieu cho.
    for it in items:
        if it["end"] - it["start"] < 0.05:
            it["end"] = it["start"] + it["dur"]

    moved, widened = 0, 0
    for it in items:
        if abs(TL._num(it["b"].get("start_override"), -1) - it["start"]) > 0.01:
            moved += 1
        elif abs(TL._num(it["b"].get("end_override"), -1) - it["end"]) > 0.01:
            widened += 1

    # ---- Ghi ket qua + dem lai thiet hai con lai ----
    cut, cut_total = 0, 0.0
    for it in items:
        #  Dem theo KHUNG CUA LUAT (``it["cho"]``), khong theo mep vua duoc noi cho cau
        #  khong con chut cho nao — cau do van la cau thieu cho.
        if it["cho"] < it["dur"] - tran - 0.05:
            cut += 1
            cut_total += it["dur"] - tran - it["cho"]
        it["b"]["start_override"] = round(it["start"], 3)
        it["b"]["end_override"] = round(it["end"], 3)
        for k in ("shift_start", "shift_end", "shift_audio", TL.END_AUTO_FIELD):
            it["b"].pop(k, None)

    #  Gio moi ghi anh chup vao ngan xep hoan tac: den day la CHAC CHAN co sua.
    if snap is not None and (moved or widened):
        commit_snapshot(pdir, snap)
    commit_chunks(pdir, b_chunks, a_lookup, snapshot_first=False)
    logger.info(
        L(f"[nâng cao] Dọn chồng lấn: xếp lại {moved}/{len(items)} câu, "
        f"nới thêm cho {widened} câu. Còn {cut} câu chưa đủ chỗ "
        f"(thiếu tổng {cut_total:.1f}s).", f"[advanced] Resolve overlaps: rearranged {moved}/{len(items)} sentences, widened {widened} more. {cut} sentences still do not fit (short by {cut_total:.1f}s in total).")
    )
    return {
        "moved": moved, "total": len(items), "widened": widened,
        "cut": cut, "cut_total": round(cut_total, 2),
        "applied": True,
        "end_time": round(max((it["end"] for it in items), default=0.0), 3),
        "limit": round(limit, 3),
        "tran": round(tran, 3),
    }


def _timeline_limit(meta: dict) -> float:
    """Moc CUOI PHIM tren TIMELINE — khong cau nao duoc day vuot qua day.

    Lay MEP PHAI CUA LAN VIDEO, va chi roi ve ``video_duration`` khi lan video trong.
    THU TU NAY QUAN TRONG — dung dao lai:

    ``video_duration`` la do dai TEP GOC, khong phai do dai tren timeline. Giam toc clip
    xuong 0,95x thi phim CHIEM 243s tren timeline trong khi tep goc van 231s. Neu lay
    ``video_duration`` truoc thi gioi han khong gian theo, va toan bo phan noi ra bi
    chan mat — dung loi da gap: ap dung phuong an "video x0,95" xong ma so cau thieu cho
    khong giam mot cau nao, vi cac cau van bi cat o moc 231s cu.
    """
    clips = VID.playable_clips(meta)
    lane_end = max((VID.clip_end(c) for c in clips), default=0.0)
    if lane_end > 0.05:
        return lane_end
    return TL._num(meta.get("video_duration"), 0.0)


# ---------------------------------------------------------------------------- #
#  BUOC 3 — CAN BANG TOC DO: tinh phuong an de nguoi dung chon
# ---------------------------------------------------------------------------- #
def _fit_stats(items: list, video_len: float, voice_speed: float,
               video_speed: float, gap: float = 0.0,
               scale: float | None = None, tran: float = 0.0) -> dict:
    """Xep thu voi mot cap (toc giong, toc video) roi DEM lai thiet hai.

    ``items`` = [{"natural": mốc tự nhiên, "dur": độ dài giọng gốc}] da sap theo thoi
    gian. Ham nay THUAN TINH TOAN, khong doc dia, khong sua gi — nho vay quet duoc vai
    tram cap trong tich tac.

    Cach mo hinh hoa BUOC 3: giam toc video xuong ``video_speed`` lam phim dai ra
    ``1/video_speed`` lan, va MOI MOC tu nhien cung gian ra dung ty le do (giong
    ``reflow_for_span_change``: noi phim ra thi khung cua tung cau gian theo). Tang toc
    giong chia do dai giong cho ``voice_speed``.
    """
    #  HE SO GIAN CUA PHIM. Mac dinh 1/video_speed, NHUNG cho goi tu ``plan_arrange_fit``
    #  truyen vao con so tinh tu chinh cac clip dang co.
    #
    #  VI SAO PHAI TRUYEN: ``video_speed`` la muc TUYET DOI ("phim chay o x0,96"), khong
    #  phai "cham them 4%". Clip DANG o 0,96 ma chon lai 0,96 thi ``apply_fit_plan``
    #  khong doi gi (co han cau ``if abs(new_speed - cur) < 0.001: continue``), trong khi
    #  1/0,96 van bao phim dai them 4%. Do that: modal hua "0 cau phai chinh tay", ap
    #  xong van con 1 cau bi cat — dung cai bay "du bao mot dang, lam xong mot neo".
    scale = scale if scale is not None else 1.0 / max(0.01, video_speed)
    limit = video_len * scale                      # cuoi phim sau khi gian
    durs = [it["dur"] / max(0.01, voice_speed) for it in items]   # giong ngan lai khi tang toc
    #  CUNG LUAT VOI ``auto_arrange`` (``khung_giu_moc``): moc bat dau giu nguyen (gian
    #  theo phim), chi noi mep phai toi sat cau sau.
    khung = khung_giu_moc([it["natural"] * scale for it in items], durs, limit)
    cut = 0                                        # so cau bi cat cut (thieu cho)
    cut_total = 0.0                                # tong so giay bi cat
    #  ``tran``: giong duoc tran qua mep khung chung nay giay (cho_tran_giong) -> chi thieu cho khi dai hon khung + tran.
    for (start, end), dur in zip(khung, durs):
        if end - start < dur - tran - 0.05:
            cut += 1
            cut_total += dur - tran - (end - start)
    return {
        "cut": cut,
        "cut_total": round(cut_total, 2),
        "moved": 0,                                # luat moi: khong cau nao bi day
        "end": round(max((e for _s, e in khung), default=0.0), 2),
        "limit": round(limit, 2),
    }


#  GIA cua moi can — con so nay dinh nghia "can bang" nghia la gi, nen de ro o day.
#
#  NGUYEN TAC (nguoi dung dat ra): PHAI CHINH TAY VAI CAU LA RE, GIAM TOC VIDEO SAU LA
#  DAT. Vi du chuan de doi chieu: "giong 1,3 + video giu nguyen + 10 cau chinh tay" phai
#  duoc coi la TOT HON "giong 1,3 + video 0,5 + 0 cau chinh tay". Ly do: 10 cau chinh tay
#  la viec lam mot lan, con phim phat o 0,5x thi NGUOI XEM nao cung thay ngay.
#
#  Nen giam toc video bi phat LUY TIEN (binh phuong): lech 0,05 gan nhu mien phi, nhung
#  cang sau cang dat rat nhanh — 0,95 hau nhu khong sao, 0,5 thi khong bao giờ được chọn
#  neu con duong khac.
_W_VOICE = 10.0        # moi 0,1 tang toc giong  (tuyen tinh)
_W_VIDEO = 12.0        # moi 0,1 giam toc video  (nhan luy tien, xem cong thuc)
_W_CUT = 3.0           # moi cau con phai chinh tay


def _cost(voice_speed: float, video_speed: float, cut: int) -> float:
    """Gia cua mot phuong an. Thap hon = tot hon.

    Giam toc video vao theo BINH PHUONG so bac lech: hinh cham mot chut thi tha duoc,
    cham nhieu thi khong. Nho vay bo quet khong bao gio de xuat "bop phim xuong 0,5x cho
    het cau phai chinh tay".
    """
    d_voice = max(0.0, voice_speed - 1.0) * 10        # so bac 0,1
    d_video = max(0.0, 1.0 - video_speed) * 10        # so bac 0,1
    return (d_voice * _W_VOICE
            + (d_video ** 2) * _W_VIDEO
            + cut * _W_CUT)


#  PHUONG AN "CUC BO" — chi tang toc DUNG NHUNG CAU bi dai, moi cau mot he so vua du,
#  khong dung toi cau khac va khong doi toc do phim.
#
#  NGUOI DUNG (09-10-2026): tang toc mot vai cau la NHE HON tang toc ca video — ca phim
#  doc nhanh len thi nguoi xem nao cung thay, con vai cau nhanh hon mot chut thi gan nhu
#  khong ai de y. Nen:
#    · gia moi bac 0,1 cua MOT cau chi bang 1/20 gia cua ca du an (``_W_VOICE_CUC_BO``);
#    · khi cung "xong han", phuong an nay dung TREN moi phuong an dong loat (xem khoa sap
#      xep trong ``plan_fit_options``), va "Tu chon ho" cung lay no truoc.
#  Tran he so la ``speed_ceiling`` cua du an (mac dinh 1,4): cau can hon tran thi van
#  con "phai chinh tay" — khong ep giong the the.
_W_VOICE_CUC_BO = 0.5


def phuong_an_cuc_bo(items: list, video_len: float, tran: float, ceiling: float,
                     scale: float | None = None) -> dict:
    """Tinh phuong an chi tang toc cac cau bi dai (toc do phim giu nguyen).

    ``items`` nhu ``_fit_stats``. Tra ``{"kind": "cuc_bo", "cut", "so_cau", "max_factor",
    "cost", ...}``; ``he_so`` = ``{uid: he so}`` cho tung cau se tang toc (ben ap dung
    dung lai dung bo so nay — mot cong thuc, hai noi).
    """
    scale = scale if scale is not None else 1.0
    limit = video_len * scale
    khung = khung_giu_moc([it["natural"] * scale for it in items],
                          [it["dur"] for it in items], limit)
    he_so, cut, cut_total = {}, 0, 0.0
    for it, (s, e) in zip(items, khung):
        cho = (e - s) + tran
        if it["dur"] <= cho + 0.05:
            continue
        can = it["dur"] / max(0.05, cho)
        #  Lam tron LEN 0,01 de he so ghi ra chac chan du cho (lam tron xuong la thieu
        #  vai phan tram giay, cau lai bao "bi cat").
        can = math.ceil(can * 100 - 1e-9) / 100.0
        if can > ceiling + 1e-9:
            cut += 1
            cut_total += it["dur"] / ceiling - cho
            continue
        he_so[it.get("uid")] = round(can, 3)
    cost = (sum((f - 1.0) * 10 for f in he_so.values()) * _W_VOICE_CUC_BO
            + cut * _W_CUT)
    return {
        "kind": "cuc_bo",
        "voice_speed": 1.0, "video_speed": 1.0,
        "cut": cut, "cut_total": round(cut_total, 2),
        "so_cau": len(he_so),
        "max_factor": round(max(he_so.values()), 3) if he_so else 1.0,
        "ceiling": round(float(ceiling), 3),
        "cost": round(cost, 2),
        "he_so": he_so,
    }


def plan_fit_options(items: list, video_len: float,
                     voice_max: float = 1.7, video_min: float = 0.80,
                     gap: float = 0.0, max_options: int = 8,
                     scale_of=None, tran: float = 0.0,
                     ceiling: float | None = None) -> dict:
    """Quet cac cap (toc giong, toc video) va tra ve vai phuong an de NGUOI DUNG chon.

    ``voice_max`` / ``video_min`` la GIOI HAN CUNG — quet khong bao gio de xuat vuot qua:
    giong nhanh hon 1,6x thi bat dau khong ro chu, phim cham hon 0,85x thi nhin ro la bi
    ruot. (Nguoi dung van tu keo tay vuot duoc neu muon; day chi la vung ma nut tu dong
    dam de xuat.)

    Tra ve:
      · ``baseline``  — thiet hai khi KHONG doi gi (buoc 1+2 thoi)
      · ``options``   — vai phuong an dang tien, moi phuong an la mot buoc danh doi khac
      · ``best_zero`` — phuong an RE NHAT ma het han cau phai chinh tay (neu co)
    """
    def stats(vo: float, vi: float) -> dict:
        return _fit_stats(items, video_len, vo, vi, gap,
                          scale=scale_of(vi) if scale_of else None, tran=tran)

    base = stats(1.0, 1.0)
    if base["cut"] == 0:
        # Buoc 1+2 da du: khong can de xuat gi ca.
        return {"baseline": base, "options": [], "best_zero": None, "grid": []}

    # Quet luoi 0,05 — du min de tim diem vua du, du tho de khong sinh ra hang nghin
    # phuong an gan giong nhau.
    #  Luoi 0,01 — bang dung buoc cua hai thanh truot "Tu can bang" ben FE.
    #
    #  HAI CON SO NAY PHAI KHOP NHAU: khung xem truoc tra bang nay de biet cap dang keo
    #  con bao nhieu cau phai chinh tay. Luoi tho hon thanh truot thi nguoi dung keo mot
    #  nhip la roi vao khe giua hai o va xem truoc bao "ngoai vung da tinh" — vo dung.
    #
    #  Gia phai tra o buoc 0,01: 71x21 = 1491 o, quet mat 65ms va goi tin 47KB (buoc
    #  0,025 truoc day: 261 o, 8ms, 8KB). Doi lay thanh truot keo muot thi re.
    STEP = 0.01
    voice_grid = [round(1.0 + i * STEP, 3)
                  for i in range(int(round((voice_max - 1.0) / STEP)) + 1)]
    video_grid = [round(1.0 - i * STEP, 3)
                  for i in range(int(round((1.0 - video_min) / STEP)) + 1)]

    scored = []
    for vo in voice_grid:
        for vi in video_grid:
            st = stats(vo, vi)
            cost = _cost(vo, vi, st["cut"])
            scored.append({
                "voice_speed": vo, "video_speed": vi,
                "cut": st["cut"], "cut_total": st["cut_total"],
                "cost": round(cost, 2),
            })

    # Phuong an RE NHAT ma khong con cau nao phai chinh tay.
    zero = [s for s in scored if s["cut"] == 0]
    best_zero = min(zero, key=lambda s: s["cost"]) if zero else None

    #  CHON PHUONG AN DE HIEN — phai la mot DUONG DANH DOI that su, moi dong nhuong mot
    #  thu de duoc mot thu khac. Neu chi lay "re nhat theo tung muc cut" thi danh sach ra
    #  vo nghia: phuong an 0 cau chinh tay lai re hon phuong an 2 cau, nen moi dong sau
    #  no deu vua dat hon VUA te hon — khong ai chon.
    #
    #  Nen chon theo PARETO: chi giu phuong an ma khong co phuong an nao khac VUA it cau
    #  chinh tay hon VUA re hon. Roi trong so do lay vai muc cach nhau du xa de doc.
    pareto = []
    for s in sorted(scored, key=lambda x: (x["cost"], x["cut"])):
        if any(o["cut"] <= s["cut"] and o["cost"] <= s["cost"] for o in pareto):
            continue
        pareto.append(s)

    #  Them mot vai MOC TRON theo so cau phai chinh tay (0, ~1/10, ~1/4, ~1/2 so ban
    #  dau): nho vay danh sach luon co ca phuong an "xong han" lan phuong an "nhe tay
    #  hon, chap nhan sua vai cau" du duong Pareto co dang the nao.
    marks = []
    for frac in (0.0, 0.1, 0.25, 0.5):
        target = int(base["cut"] * frac)
        cand = [x for x in scored if x["cut"] <= target]
        if cand:
            marks.append(min(cand, key=lambda x: x["cost"]))

    #  Va mot vai phuong an theo HUONG DANH DOI khac nhau, de danh sach khong chi toan
    #  nhung cap gan giong nhau. Moi huong tra ve cap RE NHAT trong vung cua no:
    #    · giu nguyen hinh hoan toan          (nguoi khong muon phim bi ruot)
    #    · hinh cham toi da 5%                (danh doi rat nhe)
    #    · giong toi da 1,2x / 1,35x          (nguoi uu tien giong tu nhien)
    #  Nguoi dung noi ro: dung fix cung vai phuong an, hay dua nhieu lua chon.
    lanes = [
        [x for x in scored if x["video_speed"] >= 0.999],
        [x for x in scored if x["video_speed"] >= 0.95],
        [x for x in scored if x["voice_speed"] <= 1.2],
        [x for x in scored if x["voice_speed"] <= 1.35],
        [x for x in scored if x["voice_speed"] <= 1.5],
    ]
    for lane in lanes:
        if lane:
            best = min(lane, key=lambda x: (x["cut"], x["cost"]))
            marks.append(best)

    #  Them phuong an "NHE GIONG": nhieu nguoi tha hinh cham hon la giong gap. Lay
    #  phuong an it cau chinh tay nhat trong so nhung phuong an giong <= 1,25x.
    gentle = [s for s in scored if s["voice_speed"] <= 1.25]
    gentle_best = min(gentle, key=lambda s: (s["cut"], s["cost"])) if gentle else None

    #  Them phuong an "GIU NGUYEN HINH": khong giam toc video mot chut nao (vd nguoi
    #  dung khong muon phim bi ruot), chi tang toc giong.
    keep_video = [s for s in scored if s["video_speed"] >= 0.999]
    keep_video_best = min(keep_video, key=lambda s: (s["cut"], s["cost"])) if keep_video else None

    picked, seen = [], set()
    #  PHUONG AN CUC BO (chi tang toc cac cau bi dai) — luon duoc xet, va khi cung "xong
    #  han" thi dung TREN cac phuong an dong loat (nguoi dung: nhe hon tang toc ca video).
    cuc_bo = None
    if ceiling is not None and ceiling > 1.001:
        cuc_bo = phuong_an_cuc_bo(items, video_len, tran, ceiling,
                                  scale=scale_of(1.0) if scale_of else None)
        if cuc_bo["so_cau"] == 0 or cuc_bo["cut"] >= base["cut"]:
            cuc_bo = None                     # khong cai thien gi -> khong bay ra
        else:
            picked.append({k: v for k, v in cuc_bo.items() if k != "he_so"})
    cheapest = min(scored, key=lambda x: x["cost"])
    for s in [cheapest, keep_video_best, gentle_best, best_zero] + marks + pareto:
        if not s:
            continue
        key = (s["voice_speed"], s["video_speed"])
        #  Chi bo phuong an KHONG CAI THIEN GI. Con lai de nguoi dung tu can: co nguoi
        #  chiu chinh tay 15 cau de giu giong that tu nhien, co nguoi muon xong han.
        #  Truoc day o day con loc bo phuong an "cai thien duoi 1/4" — bo cai loc do,
        #  vi nguoi dung da noi ro: dung fix cung, hay dua ra nhieu lua chon.
        if key in seen or s["cut"] >= base["cut"]:
            continue
        seen.add(key)
        picked.append(s)
    #  THU TU HIEN RA: nhom "xong han" (cut == 0) len TRUOC, trong tung nhom moi xep
    #  theo gia.
    #
    #  Vi sao khong xep thuan theo gia: danh sach ra nhin nguoc logic — dong dau la "con
    #  1 cau phai chinh tay" trong khi ngay duoi no la "xong han, moi cau du cho" voi
    #  gia chi nhich hon. Nguoi doc thay mot phuong an TE HON nam tren mot phuong an TOT
    #  HON thi mat tin vao ca danh sach.
    #
    #  Van giu ``cost`` lam khoa thu hai, nen trong nhom "xong han" thi phuong an danh
    #  doi it nhat vẫn dung dau — dung nguyen tac nguoi dung dat ra (giam toc video sau
    #  la dat, chinh tay vai cau la re).
    #  Khoa thu hai: phuong an cuc bo truoc phuong an dong loat (cung nhom cut) — NGUOI
    #  DUNG xep "tang toc vai cau" nhe hon "tang toc ca video", bat ke diem gia.
    options = sorted(
        picked, key=lambda s: (s["cut"] > 0, s.get("kind") != "cuc_bo", s["cost"])
    )[:max_options]

    #  BANG TRA cho khung "tu can bang" ben FE: MOI cap da tinh, dang gon nhat.
    #
    #  Vi sao gui ca bang thay vi de FE goi lai moi lan keo thanh truot: keo truot phat
    #  ra hang chuc gia tri mot giay: goi mang tung lan thi vua tre vua dap backend, ma
    #  bo tinh o FE thi thanh HAI ban thuat toan — kieu gi cung co ngay lech nhau (bai
    #  hoc tu cac cap mirror khac trong du an nay). Ca bang chi khoang 25x9 = 225 so,
    #  nen gui mot lan la re hon moi duong khac.
    grid = [
        {"v": s["voice_speed"], "p": s["video_speed"], "c": s["cut"]}
        for s in scored
    ]
    return {"baseline": base, "options": options, "best_zero": best_zero, "grid": grid}


def _fit_items(pdir: str, b_chunks: list, a_lookup: dict) -> list:
    """Doc du an ra dang ``[{"natural", "dur"}]`` cho bo quet phuong an.

    ``dur`` la do dai giong GOC — CHUA ap he so tang toc. Bo quet tu chia cho he so no
    dang thu, nen dua vao con so goc moi dung.

    NHUNG du an CO THE DA duoc tang toc tu lan truoc (nguoi dung bam nut nay hai lan,
    hoac da dung "Tốc độ mọi câu"). Luc do ``speed_factor`` dang la 1,45 chang han, va
    con so nguoi dung chon trong modal la he so TUYET DOI ("giong x1,45"), khong phai
    nhan them vao cai dang co. Nen o day phai lay do dai GOC that (raw), va
    ``apply_fit_plan`` cung DAT ``speed_factor`` = con so da chon chu khong nhan don.
    Nho vay bam nut lan thu hai voi cung mot lua chon la khong doi gi ca — thay vi tang
    toc chong len nhau thanh giong the the.
    """
    TL.probe_durations([TL.clip_audio_path(pdir, b) for b in b_chunks], pdir)
    moc = moc_tu_nhien_cac_cau(b_chunks, a_lookup)   # cung moc voi auto_arrange (anh em cau tach)
    items = []
    for b in b_chunks:
        raw = TL.audio_duration(TL.clip_audio_path(pdir, b))
        if raw <= 0:
            continue
        items.append({"natural": moc[TL.get_uid(b)], "dur": raw,
                      "uid": TL.get_uid(b)})
    items.sort(key=lambda x: x["natural"])
    return items


def du_bao_do_dai_phim(meta: dict, video_speed: float) -> float:
    """Do dai lan video SAU KHI dat moi clip ve ``video_speed`` — tinh y nhu
    ``apply_fit_plan`` lam that.

    ``apply_fit_plan`` dat toc do TUYET DOI cho tung clip roi day cac clip sau sang
    phai dung bang phan dai ra. Nen do dai moi = do dai hien tai + tong phan chenh
    lech cua tung clip. Clip dang dung toc do do roi thi chenh lech = 0 — va do
    chinh la truong hop ma cach cu (1/video_speed) tinh sai.
    """
    clips = VID.playable_clips(meta)
    hien_tai = max((VID.clip_end(c) for c in clips), default=0.0)
    if not clips or hien_tai <= 0:
        return hien_tai
    them = 0.0
    for c in clips:
        cu = VID.visible_duration(c)                  # do dai dang chiem tren timeline
        goc = cu * max(0.01, VID.clip_speed(c))       # do dai that cua doan phim
        moi = goc / max(0.01, video_speed)
        them += moi - cu
    return max(0.0, hien_tai + them)


def plan_arrange_fit(project_id: str) -> dict:
    """Tinh cac PHUONG AN doi toc do cho vua phim — KHONG sua gi ca.

    FE goi ham nay sau khi ``auto_arrange`` bao con cau bi cat, roi hien modal cho nguoi
    dung chon. Ap dung la viec cua ``apply_fit_plan``.
    """
    p = load_project(project_id)
    pdir, meta, a_lookup, b_chunks = p["dir"], p["meta"], p["a_lookup"], p["b_chunks"]
    items = _fit_items(pdir, b_chunks, a_lookup)
    video_len = _timeline_limit(meta)
    if not items or video_len <= 0.05:
        return {"baseline": {"cut": 0, "cut_total": 0.0}, "options": [], "best_zero": None,
                "grid": [], "video_len": round(video_len, 3), "total": len(items)}
    #  KHONG CO CLIP VIDEO thi khong the giam toc hinh -> chi quet phuong an tang toc
    #  GIONG. Truoc day van quet ca truc video: modal hua "phim cham lai 4%, moi cau
    #  du cho", ap xong thi phan giam toc bi bo qua (chi ghi mot dong canh bao trong
    #  log) va van con cau bi cat — dung cai bay "du bao mot dang, lam xong mot neo".
    co_hinh = bool(VID.playable_clips(meta))
    res = plan_fit_options(
        items, video_len,
        video_min=1.0 if not co_hinh else 0.80,
        #  Do dai phim moi tinh TU CHINH CAC CLIP dang co, khong suy tu 1/video_speed.
        #  KHONG co clip -> ``du_bao_do_dai_phim`` tra 0 -> he so gian 0 lam moi moc dồn
        #  ve giay 0 va ca bang phuong an thanh rac; luc do phim khong gian duoc, he so = 1.
        scale_of=(lambda vi: du_bao_do_dai_phim(meta, vi) / max(0.01, video_len))
        if co_hinh else (lambda vi: 1.0),
        tran=TL.tran_giong_cho_phep(meta),         # cung muc tran voi auto_arrange — modal du bao dung nhu lam that
        ceiling=_tran_toc_do(meta),                # phuong an cuc bo: tang toc tung cau toi da bang tran cua du an
    )
    res["co_the_cham_phim"] = co_hinh
    res["tran"] = round(TL.tran_giong_cho_phep(meta), 3)
    res["video_len"] = round(video_len, 3)
    res["total"] = len(items)
    #  Do dai phim SAU KHI gian, cho tung phuong an — de modal noi ro "phim se dai
    #  3:51 -> 4:03" thay vi bat nguoi dung tu nhan.
    for o in res.get("options") or []:
        o["new_video_len"] = round(du_bao_do_dai_phim(meta, o["video_speed"]), 2)
    return res


#  "Tu chon ho": may chi tu quyet trong VUNG NHE — giong nhanh hon toi da 35%, phim
#  cham lai toi da 10%. Vuot ra ngoai la danh doi nghe/nhin thay ro, viec do phai la
#  nguoi dung quyet. So do khop voi nhan "nhe/vua" ma modal dang to cho tung phuong an.
TU_CHON_GIONG_TOI_DA = 1.35
TU_CHON_PHIM_TOI_THIEU = 0.90


def _tran_toc_do(meta: dict) -> float:
    """Tran he so tang toc tung cau cua du an (``speed_ceiling``), da kep vao [MIN, MAX]."""
    c = TL._num((meta or {}).get("speed_ceiling"), TL.DEFAULT_SPEED_CEILING)
    return max(TL.SPEED_CEILING_MIN, min(TL.SPEED_CEILING_MAX, float(c)))


def chon_phuong_an_hop_ly(plan: dict) -> dict | None:
    """Chon HO mot phuong an tu ``plan_arrange_fit`` — hoac None neu khong dam chon.

    Chi lay phuong an XONG HAN (``cut == 0``, khong con cau phai chinh tay) va nam trong
    vung nhe; nhieu cai thoa thi lay cai RE NHAT theo ``cost`` (dung thuoc do san co
    cua bo quet). Khong co cai nao thoa -> None, tuc "van la nguoi dung quyet".

    Phuong an CUC BO (chi tang toc cac cau bi dai) xong han ma he so lon nhat con trong
    vung nhe thi LAY TRUOC moi phuong an dong loat — nguoi dung xep no nhe hon.
    """
    options = (plan or {}).get("options") or []
    cuc_bo = [
        o for o in options
        if o.get("kind") == "cuc_bo" and int(o.get("cut", 0) or 0) == 0
        and float(o.get("max_factor", 9)) <= TU_CHON_GIONG_TOI_DA
    ]
    if cuc_bo:
        return cuc_bo[0]
    ung_vien = [
        o for o in options
        if o.get("kind") != "cuc_bo"
        and int(o.get("cut", 0) or 0) == 0
        and float(o.get("voice_speed", 9)) <= TU_CHON_GIONG_TOI_DA
        and float(o.get("video_speed", 0)) >= TU_CHON_PHIM_TOI_THIEU
    ]
    bz = (plan or {}).get("best_zero")
    if bz and int(bz.get("cut", 0) or 0) == 0             and float(bz.get("voice_speed", 9)) <= TU_CHON_GIONG_TOI_DA             and float(bz.get("video_speed", 0)) >= TU_CHON_PHIM_TOI_THIEU:
        ung_vien.append(bz)
    if not ung_vien:
        return None
    return min(ung_vien, key=lambda o: (float(o.get("cost", 1e9)),
                                        float(o.get("voice_speed", 9)),
                                        -float(o.get("video_speed", 0))))


def tu_chon_va_ap_dung(project_id: str) -> dict:
    """Bo tron: tinh phuong an -> tu chon -> ap dung. Tra ``{"applied": bool, ...}``.

    Dung cho che do Chi tiet (nut Don voi cau hinh "Tu chon ho") va Chay hang loat.
    Khong chon duoc thi KHONG sua gi va tra ``applied=False`` kem ``plan`` de noi goi
    hoi nguoi dung — dung mot chu "chua ap dung" ro rang, khong bao "da xep" hut.
    """
    plan = plan_arrange_fit(project_id)
    chon = chon_phuong_an_hop_ly(plan)
    if not chon:
        return {"applied": False, "plan": plan, "option": None}
    res = apply_fit_plan(project_id, float(chon["voice_speed"]), float(chon["video_speed"]),
                         kind=str(chon.get("kind") or "deu"))
    return {"applied": True, "option": chon, **res}


def apply_fit_cuc_bo(project_id: str) -> dict:
    """Ap phuong an CUC BO: tang toc VUA DU cho dung nhung cau bi dai, roi don lai.

    Cung mot phep tinh voi ``phuong_an_cuc_bo`` (modal du bao bao nhieu cau thi lam dung
    bay nhieu). He so ghi TUYET DOI theo giong goc (``raw``), nhu ``apply_fit_plan``:
    cau da vua o toc do goc thi ve ``speed_factor = None`` — bam lai cung phuong an la
    khong doi gi. Giong goc giu nguyen, ban tang toc ghi ra ``derived/``.
    """
    p = load_project(project_id)
    pdir, meta, a_lookup, b_chunks = p["dir"], p["meta"], p["a_lookup"], p["b_chunks"]
    take_snapshot(pdir)

    items = _fit_items(pdir, b_chunks, a_lookup)
    video_len = _timeline_limit(meta)
    tran = TL.tran_giong_cho_phep(meta)
    ceiling = _tran_toc_do(meta)
    pa = phuong_an_cuc_bo(items, video_len, tran, ceiling) if items else {"he_so": {}, "cut": 0}
    he_so = pa.get("he_so") or {}

    sped, reused, reset = 0, 0, 0
    for b in b_chunks:
        uid = TL.get_uid(b)
        f = he_so.get(uid)
        if f is None:
            if b.get("speed_factor") not in (None, 1.0):
                b["speed_factor"] = None       # vua o toc do goc -> khong can tang
                reset += 1
            continue
        src = TL.clip_audio_path(pdir, b)
        derived = TL.derived_audio_path(pdir, b, f, src_path=src)
        if os.path.exists(derived) and os.path.getsize(derived) > 0:
            reused += 1
        elif not _atempo_wav(src, derived, f):
            continue
        else:
            sped += 1
        b["speed_factor"] = round(float(f), 3)
    commit_chunks(pdir, b_chunks, a_lookup, snapshot_first=False)

    arranged = auto_arrange(project_id, gap=0.0, snapshot=False, luon_ghi=True)
    logger.info(
        L(f"[nâng cao] Áp dụng phương án cục bộ: tăng tốc riêng {sped + reused} câu "
        f"(tối đa ×{pa.get('max_factor', 1.0)}, trần ×{ceiling:.2f}), "
        f"{reset} câu về tốc độ gốc; còn {arranged.get('cut', 0)} câu chưa đủ chỗ.", f"[advanced] Applied the local plan: sped up {sped + reused} sentences individually (max ×{pa.get('max_factor', 1.0)}, ceiling ×{ceiling:.2f}), {reset} sentences back to original speed; {arranged.get('cut', 0)} sentences still do not fit.")
    )
    return {
        "kind": "cuc_bo",
        "voice_speed": 1.0, "video_speed": 1.0,
        "clips_changed": 0,
        "sped": sped + reused, "reset": reset, "no_audio": 0,
        "max_factor": pa.get("max_factor", 1.0),
        **{f"arrange_{k}": v for k, v in arranged.items()},
        "cut": arranged.get("cut", 0),
    }


def apply_fit_plan(project_id: str, voice_speed: float, video_speed: float,
                   kind: str = "deu") -> dict:
    """Ap dung mot phuong an: tang toc giong DONG LOAT + giam toc video, roi don lai.

    ``kind="cuc_bo"`` -> ``apply_fit_cuc_bo`` (chi tang toc cac cau bi dai, phim giu
    nguyen); hai tham so toc do bi bo qua.

    Ba viec, dung thu tu nay (doi thu tu la sai so lieu):

      1. GIAM TOC VIDEO truoc. Phim dai ra thi moc tu nhien cua tung cau cung gian theo
         (``reflow_for_span_change``) — phai lam truoc de buoc 3 xep tren khung DA GIAN.
      2. TANG TOC GIONG cho MOI CAU cung mot he so. Khac ``auto_speed`` (chi tang cau
         nao dang tran, moi cau mot he so khac nhau): o day nguoi dung chon mot con so
         cho ca du an, nen giong phai deu — cao thap lo^~n xo^.n giua cac cau nghe ro la
         may moc.
      3. Don chong lan lai tren khung moi.

    Moi thay doi nam trong MOT anh chup, nen Ctrl+Z mot lan la ve nguyen trang ca ba.
    """
    if str(kind or "deu") == "cuc_bo":
        return apply_fit_cuc_bo(project_id)
    p = load_project(project_id)
    pdir, meta, a_lookup, b_chunks = p["dir"], p["meta"], p["a_lookup"], p["b_chunks"]

    voice_speed = max(1.0, min(TL.SPEED_CEILING_MAX, float(voice_speed)))
    video_speed = max(VID.SPEED_MIN, min(1.0, float(video_speed)))

    #  MOT anh chup cho ca ba viec (chup truoc khi doi bat cu thu gi).
    take_snapshot(pdir)

    # ---- 1. Giam toc video (neu co) ----
    clips_changed = 0
    reflowed = {"moved": 0, "unsped": 0, "subs": 0}
    if video_speed < 0.999:
        want_clips = VID.playable_clips(meta)
        if not want_clips:
            #  Khong co clip nao thi khong the giam toc hinh. Phai NOI RA: im lang o day
            #  la bao "da ap dung giong x1.3 / video x0.95" trong khi ve hinh khong doi
            #  gi ca, va con so cau con thieu cho se khong khop voi du bao.
            logger.warning(
                L("[nâng cao] Không có clip video nào để giảm tốc — phần giảm tốc hình bị "
                "bỏ qua, chỉ tăng tốc giọng. Số câu còn thiếu chỗ sẽ nhiều hơn dự báo.", "[advanced] No video clip available to slow down — the picture slow-down is skipped, only the voice is sped up. More sentences will lack room than predicted.")
            )
        #  DUYET TU CUOI VE DAU, va DICH CAC CLIP SAU theo phan dai ra.
        #
        #  LOI NGUOI DUNG DA GAP: truoc day vong nay chi doi ``speed`` cua tung clip roi
        #  thoi. Clip dau dai ra (0,95x -> chiem nhieu thoi gian hon) nhung clip thu hai
        #  VAN o moc cu, nen hai clip DE LEN NHAU: clip sau bi day xuong lan "Video
        #  (chong) — khong phat", tuc MAT HAN doan hinh do khoi ban xuat. Tren timeline
        #  hien dung mot dai bao "1 video chong lan", va so cau bi cat con tang len
        #  (51/110) vi phan hinh mat di keo theo cho cua cac cau.
        #
        #  Du an mot clip khong bao gio lo ra loi nay — do la ly do bo test cu khong bat
        #  duoc. Tu cuoi ve dau thi dich clip sau khong lam sai moc cua clip dang xu ly.
        want_clips = sorted(want_clips, key=lambda c: VID._num(c.get("start")))
        for idx in range(len(want_clips) - 1, -1, -1):
            c = want_clips[idx]
            old_span = VID.visible_duration(c)
            cur = VID.clip_speed(c)
            #  DAT TUYET DOI, khong nhan don vao he so dang co.
            #
            #  LOI DA GAP: truoc day o day la ``cur * video_speed``, nen bam nut hai lan
            #  voi CUNG mot phuong an la phim gian chong hai lan (243s -> 256s -> ...),
            #  moi lan mot it, khong bao gio dung. Con so trong modal la muc TUYET DOI
            #  ("phim chay o x0,95"), giong nhu ``speed_factor`` cua giong — nen ap dung
            #  lai cung mot lua chon phai KHONG doi gi ca.
            new_speed = max(VID.SPEED_MIN, video_speed)
            if abs(new_speed - cur) < 0.001:
                continue
            anchor = VID._num(c.get("start"))
            updated = VID.patch_clip(meta, c["id"], {"speed": round(new_speed, 4)})
            new_span = VID.visible_duration(updated)
            clips_changed += 1
            #  DICH MOI CLIP DUNG SAU sang phai dung bang phan clip nay dai ra — khong
            #  thi chung de len nhau.
            shift = new_span - old_span
            if abs(shift) > 0.001:
                for later in want_clips[idx + 1:]:
                    VID.patch_clip(
                        meta, later["id"],
                        {"start": round(VID._num(later.get("start")) + shift, 3)},
                    )
            #  Keo cac cau B + phu de goc theo dung ty le clip vua noi ra.
            r = reflow_for_span_change(pdir, b_chunks, a_lookup, anchor, old_span, new_span)
            for k in reflowed:
                reflowed[k] += r.get(k, 0)
        save_meta(pdir, meta)
        #  Nap lai a_lookup: reflow da doi moc phu de goc tren dia.
        p = load_project(project_id, migrate=False)
        pdir, meta, a_lookup, b_chunks = p["dir"], p["meta"], p["a_lookup"], p["b_chunks"]

    # ---- 2. Tang toc giong DONG LOAT ----
    sped, no_audio = 0, 0
    if voice_speed > 1.001:
        for b in b_chunks:
            src = TL.clip_audio_path(pdir, b)
            raw = TL.audio_duration(src)
            if raw <= 0:
                no_audio += 1
                continue
            derived = TL.derived_audio_path(pdir, b, voice_speed, src_path=src)
            if not (os.path.exists(derived) and os.path.getsize(derived) > 0):
                _atempo_wav(src, derived, voice_speed)
            if os.path.exists(derived) and os.path.getsize(derived) > 0:
                b["speed_factor"] = round(voice_speed, 3)
                sped += 1
        commit_chunks(pdir, b_chunks, a_lookup, snapshot_first=False)

    # ---- 3. Don chong lan lai tren khung moi ----
    #  luon_ghi=True: giong vua doi toc do o buoc 2, o cu khong con dung nua. Xem
    #  ghi chu dai trong ``auto_arrange`` — day la cho tung lam 40/44 cau bao "bi
    #  cat" ngay sau khi modal hua "moi cau du cho".
    arranged = auto_arrange(project_id, gap=0.0, snapshot=False, luon_ghi=True)

    logger.info(
        L(f"[nâng cao] Áp dụng phương án khớp: giọng ×{voice_speed:.2f}, "
        f"video ×{video_speed:.2f} ({clips_changed} clip). Tăng tốc {sped} câu; "
        f"còn {arranged.get('cut', 0)} câu chưa đủ chỗ.", f"[advanced] Applied the fit plan: voice ×{voice_speed:.2f}, video ×{video_speed:.2f} ({clips_changed} clips). Sped up {sped} sentences; {arranged.get('cut', 0)} sentences still do not fit.")
    )
    return {
        "voice_speed": round(voice_speed, 3),
        "video_speed": round(video_speed, 3),
        "clips_changed": clips_changed,
        "sped": sped,
        "no_audio": no_audio,
        **{f"arrange_{k}": v for k, v in arranged.items()},
        "cut": arranged.get("cut", 0),
    }


# --------------------------------------------------------------------------- #
#  Nut tu dong 2: tang toc cho khop (co tran)
# --------------------------------------------------------------------------- #
def _atempo_wav(src: str, dst: str, factor: float) -> bool:
    """Tang toc file WAV bang atempo, GIU NGUYEN cao do. Ghi ra file rieng."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    factor = max(0.5, min(2.0, float(factor)))
    tmp = dst + ".tmp.wav"
    cmd = [FFMPEG_EXE, "-y", "-i", src, "-filter:a", f"atempo={factor:.4f}",
           "-c:a", "pcm_s16le", tmp]
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, close_fds=True)
    if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        safe_replace(tmp, dst)
        return True
    safe_remove(tmp)
    logger.warning(L(f"[nâng cao] atempo thất bại cho {os.path.basename(src)}: "
                   f"{res.stderr.decode('utf-8', errors='ignore')[-300:]}", f"[advanced] atempo failed for {os.path.basename(src)}: {res.stderr.decode('utf-8', errors='ignore')[-300:]}"))
    return False


def auto_speed(project_id: str, ceiling: float = None, uids: list = None) -> dict:
    """Tang toc VUA DU cho cac cau dang tran, khong bao gio vuot tran.

    Cau nao tang het tran ma van tran thi DE NGUYEN va giu canh bao — khong bao gio
    cat bot chu.

    ``uids`` = chi lam cho may cau do (nut "Tăng tốc cho khớp" trong bang thuoc tinh);
    bo trong = toan bo du an.

    Ban tang toc ghi ra ``output/{uid}/derived/x1.350.wav``; file giong goc GIU
    NGUYEN. Nho vay bam di bam lai / doi tran deu khong phai goi TTS lai.

    TANG TOC LAM TREN FILE GIONG DA SINH, khong phai tren chu. Cau chua sinh giong thi
    khong co gi de tang — nhung phai DEM lai va bao ra (``no_audio``), neu khong nguoi
    dung bam nut, thay bao "đã tăng tốc" ma tren timeline khong co gi doi (loi da bao).
    """
    p = load_project(project_id)
    pdir, meta, a_lookup, b_chunks = p["dir"], p["meta"], p["a_lookup"], p["b_chunks"]

    if ceiling is None:
        ceiling = TL._num(meta.get("speed_ceiling"), TL.DEFAULT_SPEED_CEILING)
    ceiling = max(TL.SPEED_CEILING_MIN, min(TL.SPEED_CEILING_MAX, float(ceiling)))

    only = set(uids or [])
    #  Giong duoc tran qua mep khung chung nay giay -> chi tang toc cho vua ``khung + tran``, khong ep sat khung.
    tran = TL.tran_giong_cho_phep(meta)
    # Chup vao bo nho truoc: khong co gi doi thi KHONG ghi vao ngan xep hoan tac (mot cu
    # bam vo thuong vo phat khong duoc an mot buoc Ctrl+Z va xoa sach ngan xep lam lai).
    snap = capture_snapshot(pdir)
    sped, still_over, reused, no_audio, considered = 0, 0, 0, 0, 0
    changed = False

    for b in b_chunks:
        if only and TL.get_uid(b) not in only:
            continue
        considered += 1
        start, end = resolve_b_time(b, a_lookup)
        src = TL.clip_audio_path(pdir, b)
        raw = TL.audio_duration(src)
        if start is None:
            continue
        if raw <= 0:
            no_audio += 1
            continue
        window = max(0.0, float(end) - float(start))
        if window <= 0.05:
            continue
        cho = window + tran                # khung + phan duoc tran
        if raw <= cho + 0.05:
            if b.get("speed_factor") not in (None, 1.0):
                changed = True
            b["speed_factor"] = None       # vua roi, khong can tang toc
            continue

        factor = min(ceiling, raw / cho)
        if factor <= 1.001:
            continue
        # Ten tep dan xuat MANG dau van tay cua ban giong dang co, nen "dung lai" chi
        # xay ra khi that su cung mot ban giong. Truoc day ten chi theo he so: sinh lai
        # giong xong bam Tang toc la lay lai ban tang toc cua GIONG CU.
        dst = TL.derived_audio_path(pdir, b, factor, src_path=src)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            reused += 1                     # da co ban dan xuat cung he so -> dung lai
        elif not _atempo_wav(src, dst, factor):
            continue
        else:
            sped += 1
        if b.get("speed_factor") != round(factor, 3):
            changed = True
        b["speed_factor"] = round(factor, 3)
        if raw / factor > cho + 0.05:
            still_over += 1

    meta["speed_ceiling"] = ceiling
    save_meta(pdir, meta)
    if changed:
        commit_snapshot(pdir, snap)
        commit_chunks(pdir, b_chunks, a_lookup, snapshot_first=False)
    logger.info(L(f"[nâng cao] Tăng tốc: {sped} câu mới, {reused} câu dùng lại bản cũ, "
                f"{still_over} câu chạm trần vẫn tràn, {no_audio} câu chưa có giọng.", f"[advanced] Speed-up: {sped} new sentences, {reused} reused the old version, {still_over} hit the ceiling and still overflow, {no_audio} have no voice yet."))
    return {
        "sped_up": sped, "reused": reused, "still_over": still_over,
        "no_audio": no_audio, "considered": considered, "ceiling": ceiling,
    }


def _khung_goc(b: dict, a_lookup: dict):
    """Khung (bat dau, ket thuc) suy tu phu de goc — bo qua moi override."""
    tam = {k: v for k, v in b.items() if k not in ("start_override", "end_override")}
    st, en = resolve_b_time(tam, a_lookup)
    return (None, None) if st is None or en is None else (float(st), float(en))


def _mep_truoc_dan(b: dict, a_lookup: dict):
    """Mep phai TRUOC khi buoc sinh giong dan ra (gia tri luu trong dau ``end_auto``;
    ``None`` = truoc do khong co override -> mep suy tu phu de goc)."""
    v = b.get(TL.END_AUTO_FIELD)
    if v is not None:
        return float(v)
    tam = {k: x for k, x in b.items() if k != "end_override"}
    _st, en = resolve_b_time(tam, a_lookup)
    return None if en is None else float(en)


def go_mep_tu_dan(b: dict) -> bool:
    """Tra cau ve mep phai truoc khi tu dan (neu co dau). Tra ve True neu co doi."""
    if TL.END_AUTO_FIELD not in b:
        return False
    v = b.pop(TL.END_AUTO_FIELD)
    if v is None:
        b.pop("end_override", None)
    else:
        b["end_override"] = v
    return True


def _chua_xep(b: dict, a_lookup: dict) -> bool:
    """Cau con nam DUNG khung phu de goc (chua xep / chua keo): khong override, hoac
    override trung khit khung goc (mot so thao tac ghi ra so cu the ma khong doi gi)."""
    if b.get("end_override") is None:
        return True
    gs, ge = _khung_goc(b, a_lookup)
    if gs is None:
        return False
    st, en = resolve_b_time(b, a_lookup)
    return (st is not None and abs(float(st) - gs) <= 0.01
            and abs(float(en) - ge) <= 0.01)


def khop_mep_theo_giong(pdir: str, b_chunks: list, a_lookup: dict, meta: dict,
                        uids: set = None, dan_khung_goc: bool = False) -> int:
    """Doi DO DAI GIONG xong (toc do moi cau, dat lai toc do) -> KHOP LAI mep phai.

    LOI NGUOI DUNG GAP (25-09-2026): "Don chong lan" ghi ``end_override`` = bat dau +
    do dai giong O TOC DO LUC BAM. Doi "Toc do moi cau" sau do chi doi ``speed_factor``,
    khung GIU NGUYEN do dai cu: don o x1.5 roi ve x1.3 la ca 57/57 cau bao "bi cat";
    lap vai lan thi khung cu co dan lai. Khung phai theo giong, khong theo lan bam truoc.

    CUNG LUAT voi ``khung_giu_moc``: GIU NGUYEN diem bat dau (khong bao gio day cau di),
    mep phai = bat dau + giong moi, nhung khong qua moc bat dau cua cau SAU va cuoi phim.
    Chi dong vao cau DA co ``end_override`` (khung do phep xep / keo tay dat ra); cau
    chua co thi khung van suy tu phu de goc nhu cu. ``uids``: chi khop nhung cau nay
    (sinh lai giong mot phan) — cau khac van tinh la "cau sau" de chan mep, nhung khong
    bi sua. Tra ve so cau da doi mep.

    ``dan_khung_goc`` (chi buoc sinh giong bat): cau CHUA co ``end_override`` ma giong dai
    hon khung phu de goc -> dan mep phai ra (toi da sat dau cau sau / cuoi phim), danh dau
    ``end_auto`` = mep cu (``None`` neu truoc do khong co override). "Chua xep" gom ca cau
    co override TRUNG KHIT khung phu de goc. Cau DA co dau (du co bat hay khong) thi khong bao gio co lai ngan hon
    mep goc: giong ngan di thi go dau, tra ve mep goc — mep tu dan chi duoc NOI, khong
    duoc AN vao khung phu de goc.
    """
    TL.probe_durations([TL.clip_audio_path(pdir, b) for b in b_chunks], pdir)
    hang = []
    for b in b_chunks:
        st, _en = resolve_b_time(b, a_lookup)
        if st is not None:
            hang.append((float(st), b))
    hang.sort(key=lambda h: h[0])
    limit = _timeline_limit(meta)
    doi = 0
    for i, (st, b) in enumerate(hang):
        if b.get("shift_start") is not None:
            continue
        if uids is not None and TL.get_uid(b) not in uids:
            continue
        tu_dong = TL.END_AUTO_FIELD in b
        if not tu_dong:
            if b.get("end_override") is not None and not (dan_khung_goc and _chua_xep(b, a_lookup)):
                pass                      # mep do phep xep / keo tay -> luat cu ben duoi
            elif dan_khung_goc:
                tu_dong = True
            else:
                continue
        dur = TL.effective_audio_duration(b, TL.audio_duration(TL.clip_audio_path(pdir, b)))
        if dur <= 0:
            continue
        tran = st + dur
        if i + 1 < len(hang):
            tran = min(tran, hang[i + 1][0])
        if limit > 0:
            tran = min(tran, limit)
        if tu_dong:
            goc = _mep_truoc_dan(b, a_lookup)
            if goc is None:
                continue
            if tran <= goc + 0.01:
                #  Khong con gi de dan (giong vua khung / cau sau sat ngay) -> ve mep cu.
                if go_mep_tu_dan(b):
                    doi += 1
                continue
            if TL.END_AUTO_FIELD not in b:
                b[TL.END_AUTO_FIELD] = b.get("end_override")   # nho mep cu de "Dat lai"
                doi += 1
            elif abs(TL._num(b.get("end_override"), -1) - tran) > 0.01:
                doi += 1
            b["end_override"] = round(tran, 3)
            continue
        if tran - st < 0.05:
            continue                  # khong con cho — de nguyen, van hien la thieu cho
        if abs(TL._num(b.get("end_override"), -1) - tran) > 0.01:
            b["end_override"] = round(tran, 3)
            doi += 1
    return doi


def apply_uniform_speed(project_id: str, target: float) -> dict:
    """Dua MOI cau ve dung TOC DO TONG ``target`` de nghe thu ngay.

    ĐIỂM QUAN TRỌNG: ``target`` la toc do NGUOI NGHE CAM NHAN, khong phai he so tho.
    Neu giong duoc sinh o +20% (da nhanh 1.2x) ma nguoi dung chon x1.2 thi KHONG
    duoc tang them gi ca; chon x1.4 thi chi tang them 1.4/1.2 = 1.17 lan. Moi cau co
    the co rate rieng nen he so duoc tinh RIENG cho tung cau.

    Van ghi ra file DAN XUAT, file giong goc giu nguyen.
    """
    p = load_project(project_id)
    pdir, meta, b_chunks, a_lookup = p["dir"], p["meta"], p["b_chunks"], p["a_lookup"]

    target = max(0.5, min(TL.SPEED_CEILING_MAX, float(target)))
    snap = capture_snapshot(pdir)

    applied, reused, cleared, skipped, no_audio = 0, 0, 0, 0, 0
    changed = False
    for b in b_chunks:
        src = TL.clip_audio_path(pdir, b)
        if not os.path.exists(src) or os.path.getsize(src) == 0:
            # Chua sinh giong -> khong co file de doi toc do. Dem lai de bao ro thay vi
            # bao thanh cong suong (cung ly le voi ``auto_speed``).
            no_audio += 1
            continue

        base = TL.clip_base_rate(meta, b)          # toc do da sinh ra
        factor = target / max(0.1, base)           # phan phai tang/giam THEM
        # atempo chi chay tot trong 0.5..2.0; ngoai khoang do thi bo qua cau nay.
        if factor < 0.5 or factor > 2.0:
            skipped += 1
            continue

        if abs(factor - 1.0) <= 0.01:
            if b.get("speed_factor") not in (None, 1.0):
                cleared += 1
                changed = True
            b["speed_factor"] = None               # dung bang giong goc -> khong can file dan xuat
            continue

        factor = round(factor, 3)
        # Nhu tren: khoa theo ban giong dang co, khong chi theo he so.
        dst = TL.derived_audio_path(pdir, b, factor, src_path=src)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            reused += 1
        elif not _atempo_wav(src, dst, factor):
            continue
        else:
            applied += 1
        if b.get("speed_factor") != factor:
            changed = True
        b["speed_factor"] = factor

    #  Giong doi do dai -> khung phai doi theo (xem ``khop_mep_theo_giong``).
    khop = khop_mep_theo_giong(pdir, b_chunks, a_lookup, meta) if changed else 0
    if changed:
        commit_snapshot(pdir, snap)
        commit_chunks(pdir, b_chunks, a_lookup, snapshot_first=False)
    if khop:
        logger.info(L(f"[nâng cao] Khớp lại mép kết thúc {khop} câu theo độ dài giọng mới.", f"[advanced] Re-fitted the end edge of {khop} sentences to the new voice length."))
    logger.info(
        L(f"[nâng cao] Đưa mọi câu về tốc độ x{target:.2f}: {applied} câu tạo mới, "
        f"{reused} dùng lại", f"[advanced] Set every sentence to speed x{target:.2f}: {applied} newly created, {reused} reused")
        + (L(f", {cleared} câu vốn đã đúng tốc độ", f", {cleared} sentences were already at the right speed") if cleared else "")
        + (L(f", {skipped} câu ngoài tầm điều chỉnh", f", {skipped} sentences outside the adjustable range") if skipped else "")
        + (L(f", {no_audio} câu chưa có giọng", f", {no_audio} sentences have no voice yet") if no_audio else "")
    )
    return {
        "target": target, "applied": applied, "reused": reused,
        "cleared": cleared, "skipped": skipped, "no_audio": no_audio,
        "refit": khop,
    }


def reset_speed(project_id: str) -> dict:
    """Bo toan bo he so tang toc -> quay ve giong goc.

    Kem theo: go moi mep phai do buoc sinh giong TU DAN (``end_auto``) -> cau ve lai dung
    khung suy tu phu de goc, nhu truoc khi sinh giong."""
    p = load_project(project_id)
    take_snapshot(p["dir"])
    n, ve_goc = 0, 0
    da_tra = set()
    for b in p["b_chunks"]:
        if b.get("speed_factor") not in (None, 1.0):
            b["speed_factor"] = None
            n += 1
        if go_mep_tu_dan(b):
            ve_goc += 1
            da_tra.add(TL.get_uid(b))
    #  Câu VỪA trả mép về KHÔNG được khớp lại: trả về xong câu mang override (mép cũ là
    #  số) trông như câu "đã xếp", luật cũ lại đặt mép = bắt đầu + giọng -> dãn y như cũ,
    #  mà mất luôn dấu -> bấm Đặt lại lần nữa cũng không về được (rà soát 29-09-2026).
    con_lai = {TL.get_uid(b) for b in p["b_chunks"]} - da_tra
    khop = (
        khop_mep_theo_giong(p["dir"], p["b_chunks"], p["a_lookup"], p["meta"], uids=con_lai)
        if n and con_lai else 0
    )
    commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    return {"reset": n, "refit": khop, "restored_ends": ve_goc}


# --------------------------------------------------------------------------- #
#  Chon file audio de dung phim
# --------------------------------------------------------------------------- #
def _trim_wav(src: str, dst: str, duration: float) -> bool:
    """Cat file WAV con dung ``duration`` giay (nguoi dung keo mep ngan hon giong)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp.wav"
    cmd = [FFMPEG_EXE, "-y", "-i", src, "-t", f"{duration:.3f}",
           "-c:a", "pcm_s16le", tmp]
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, close_fds=True)
    if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        safe_replace(tmp, dst)
        return True
    safe_remove(tmp)
    return False


def resolve_playback_audio(project_dir: str, b_data: dict, window: float, tran: float = 0.0) -> tuple:
    """Chon file audio se dung khi dung phim, tra ``(path, duration, is_trimmed)``.

    Thu tu:
      1. Ban tang toc dan xuat (neu cau co ``speed_factor``).
      2. File giong goc.
    Sau do neu khung thoi gian NGAN HON giong -> cat cho vua (dung y nguoi dung khi
    keo mep ngan lai) va bao ``is_trimmed`` de UI to mau khac.

    ``tran`` (``TL.tran_giong_cho_phep(meta)``): giong duoc phat qua mep khung them chung nay giay
    (de len dau cau sau) truoc khi bi cat — nguoi dung chot 07-10-2026: chong mot chut van nghe ro,
    con hon cat cut chu. Noi goi PHAI truyen cung mot ``tran`` o ca xuat lan nghe thu (route
    ``/clips/{uid}/audio``) de hai ben nghe giong nhau.

    LUU Y: he so CHAM LAI (< 1) cung phai dung ban dan xuat. Truoc day cho nay chi xet
    ``factor > 1.001`` nen moi he so nho hon 1 bi bo qua AM THAM: ``apply_uniform_speed``
    van tao tep ``derived/x0.833.wav``, van ghi ``speed_factor``, timeline van ve khoi
    dai ra theo ``effective_audio_duration`` — nhung nghe thu va ban xuat deu doc tep
    GOC. Ma he so < 1 khong he hiem: chon x0.9, hoac du an sinh giong o ``+20%`` roi
    chon x1.0 / x1.1 la roi vao ngay. Hau qua: bam xong bao thanh cong ma khong co gi
    doi, con co ``trimmed``/``overlap`` thi tinh tren mot do dai khong co that.
    """
    src = TL.clip_audio_path(project_dir, b_data)
    if not os.path.exists(src) or os.path.getsize(src) == 0:
        return None, 0.0, False

    # DAU VAN TAY CUA BAN GIONG DANG CO. Moi tep dan xuat deu mang no trong ten, nen sinh
    # lai giong (giong khac, chu khac) la sinh ra bo ten khac -> khong bao gio dung nham
    # ban cu. Xem ``TL.source_tag`` de biet loi da gap.
    tag = TL.source_tag(src)
    TL.prune_derived(project_dir, b_data, tag)

    factor = TL._num(b_data.get("speed_factor"), 1.0) or 1.0
    path = src
    if abs(factor - 1.0) > 0.001:
        derived = TL.derived_audio_path(project_dir, b_data, factor, src_path=src)
        if not (os.path.exists(derived) and os.path.getsize(derived) > 0):
            _atempo_wav(src, derived, factor)
        if os.path.exists(derived) and os.path.getsize(derived) > 0:
            path = derived

    dur = TL.audio_duration(path)
    cap = window + max(0.0, float(tran or 0.0))       # khung + phan duoc tran
    if window > 0.05 and dur > cap + 0.05:
        #  Ten tep mang ``cap`` (khung + tran): doi muc tran o Cai dat la ra ten khac, khong vo phai ban cat cu.
        cut = os.path.join(TL.clip_dir(project_dir, b_data), TL.DERIVED_DIRNAME,
                           f"cut{cap:.3f}_{factor:.3f}_{tag}.wav")
        if not (os.path.exists(cut) and os.path.getsize(cut) > 0):
            _trim_wav(path, cut, cap)
        if os.path.exists(cut) and os.path.getsize(cut) > 0:
            return cut, TL.audio_duration(cut), True
    return path, dur, False



# --------------------------------------------------------------------------- #
#  Duong song am (waveform)
# --------------------------------------------------------------------------- #
def _waveform_cache_path(src: str, points: int) -> str:
    return f"{src}.peaks{points}.json"


def waveform_peaks(path: str, points: int = 120) -> list:
    """Rut gon file audio thanh ``points`` diem bien do 0..1 de ve tren timeline.

    Doc qua ffmpeg (mono 8kHz PCM) roi lay bien do lon nhat trong tung o. Ket qua
    duoc CACHE canh file goc va het hieu luc khi file doi (so theo mtime+size), vi
    timeline ve lai lien tuc ma doc ca file moi lan thi rat cham.
    """
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    points = max(20, min(600, int(points)))
    cache = _waveform_cache_path(path, points)
    stamp = f"{os.path.getmtime(path):.3f}:{os.path.getsize(path)}"
    if os.path.exists(cache):
        try:
            with open(cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("stamp") == stamp:
                return data.get("peaks") or []
        except Exception:
            pass

    cmd = [FFMPEG_EXE, "-v", "quiet", "-i", path,
           "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
    try:
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                             close_fds=True, timeout=120)
    except subprocess.TimeoutExpired:
        logger.warning(L(f"[nâng cao] Đọc sóng âm quá lâu, bỏ qua: {os.path.basename(path)}", f"[advanced] Reading the waveform took too long, skipped: {os.path.basename(path)}"))
        return []
    raw = res.stdout or b""
    if len(raw) < 4:
        return []

    import array

    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    n = len(samples)
    if n == 0:
        return []
    step = max(1, n // points)
    peaks = []
    for i in range(0, n, step):
        chunk = samples[i : i + step]
        if not chunk:
            break
        peaks.append(round(min(1.0, max(abs(min(chunk)), abs(max(chunk))) / 32768.0), 3))
    peaks = peaks[:points]

    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"stamp": stamp, "peaks": peaks}, f)
    except Exception:
        pass
    return peaks

# --------------------------------------------------------------------------- #
#  Dung phim — dung chung cho lan nhac va tieng cua clip video
# --------------------------------------------------------------------------- #
def _audio_chain(idx: int, pad: str, *, start: float, t_in: float, t_out: float,
                 speed: float = 1.0, volume: float = 1.0,
                 fade_in: float = 0.0, fade_out: float = 0.0,
                 loop: bool = False, video_len: float = 0.0,
                 duck_ratio: float = None, duck_windows=None) -> str:
    """Mot day bo loc audio cho MOT doan tren timeline.

    Dung chung cho lan nhac chen va tieng cua clip video, de hai duong khong bao gio
    lech nhau. THU TU BAT BUOC:

        atrim (giay tep nguon) -> atempo -> aloop -> volume -> afade -> adelay

    ``atrim`` truoc ``atempo`` vi in/out do theo tep nguon; ``afade`` sau ``atempo`` vi
    fade do theo giay TREN TIMELINE (dat truoc thi vuot 1 giay bi co lai con 0.5 khi
    tang toc gap doi).

    ``duck_ratio`` + ``duck_windows``: ha tieng xuong con ``ratio`` lan trong nhung
    khoang giong AI dang noi — dung cho tieng cua clip video, giu duoc net "tu dong ha
    tieng goc" ma van cho chinh am luong rieng tung clip.
    """
    seg_src = max(0.05, t_out - t_in) if t_out > t_in else 0.0
    trim = f"atrim=start={t_in:.3f}" + (f":end={t_out:.3f}" if seg_src else "")
    chain = (
        f"[{idx}:a]{trim},asetpts=PTS-STARTPTS,"
        f"aformat=sample_rates=44100:channel_layouts=stereo"
    )
    if abs(speed - 1.0) > 0.001:
        chain += f",atempo={speed:.4f}"

    seg = seg_src / speed if seg_src else 0.0
    length = seg
    if loop and seg > 0.05 and video_len > 0:
        need = max(0.0, video_len - start)
        repeats = max(0, int(need / seg))
        if repeats > 0:
            chain += f",aloop=loop={repeats}:size={int(seg * 44100)},atrim=0:{need:.3f}"
            length = need

    if duck_ratio is not None and duck_windows and abs(duck_ratio - 1.0) > 0.001:
        # Bao am luong: binh thuong = volume, trong khoang AI noi = volume * ratio.
        # Cac khoang da duoc gop san nen moi thoi diem chi tru dung mot lan.
        d = duck_ratio - 1.0
        expr = f"{volume:.3f}"
        for (s, e) in duck_windows:
            # adelay dat clip vao cho SAU day, nen moc thoi gian o buoc nay phai la
            # thoi gian TRONG clip (tru di start), khong phai thoi gian tren timeline.
            ls, le = s - start, e - start
            if le <= 0 or (length > 0 and ls >= length):
                continue
            expr += f"+({volume * d:.4f}*between(t,{max(0.0, ls):.3f},{le:.3f}))"
        chain += f",volume='{expr}':eval=frame"
    else:
        chain += f",volume={volume:.3f}"

    fi = max(0.0, min(fade_in, length))
    fo = max(0.0, min(fade_out, length))
    if fi > 0.01:
        chain += f",afade=t=in:st=0:d={fi:.3f}"
    if fo > 0.01:
        chain += f",afade=t=out:st={max(0.0, length - fo):.3f}:d={fo:.3f}"

    delay = int(max(0.0, start) * 1000)
    chain += f",adelay={delay}|{delay}{pad}"
    return chain


def _video_chain(idx: int, pad: str, clip: dict, cw: int, ch: int, fps: float,
                 place: bool = True) -> str:
    """Mot day bo loc HINH cho mot clip video (hoac ANH), da dat dung cho tren timeline.

    ``place=False`` thi KHONG chen khung den o dau (``tpad``): duong ``concat`` noi cac
    clip lien tiep nhau nen vi tri do thu tu quyet dinh, khong phai do do dich.
    """
    t_in, t_out = VID._num(clip.get("in")), VID._num(clip.get("out"))
    speed = VID.clip_speed(clip)
    start = VID._num(clip.get("start"))
    if VID.clip_is_image(clip):
        # ANH: dau vao da duoc mo bang ``-loop 1 -t <do dai>`` nen luong hinh dai dung bang
        # thoi luong nguoi dung dat. KHONG ``trim``/``setpts`` (khong co moc thoi gian goc
        # de cat) va KHONG doi toc do (anh tinh, tang toc vo nghia).
        chain = f"[{idx}:v]fps={fps:g}"
    else:
        chain = f"[{idx}:v]trim=start={t_in:.3f}:end={t_out:.3f},setpts=PTS-STARTPTS"
        if abs(speed - 1.0) > 0.001:
            chain += f",setpts=PTS/{speed:.4f}"
        chain += f",fps={fps:g}"
    # Co vua khung, thua thi vien den — khong bao gio bop meo hinh. Clip nho hon hoac
    # ty le khac (dan them tu may) van vao duoc mach phim ma khong bi keo dan.
    chain += (
        f",scale={cw}:{ch}:force_original_aspect_ratio=decrease"
        f",pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    chain += ",setsar=1,format=yuv420p"
    if place and start > 0.001:
        # tpad chen san khung den o dau -> luc overlay, dau vao LUC NAO CUNG co khung,
        # khong bi treo cho. Phan den do bi 'enable' chan nen khong de len anh nen.
        chain += f",tpad=start_duration={start:.3f}:start_mode=add:color=black"
    chain += pad
    return chain


# --------------------------------------------------------------------------- #
#  Tien do BUOC 1/2 (ghep tieng) — de FE khong phai cho trong vo vong
# --------------------------------------------------------------------------- #
#  Buoc 1 truoc day chi bao "dang chay / da xong", nen thanh tien do phai chay vo dinh
#  (indeterminate) suot ca chang. Voi phim 1-2 gio chang nay dai hang chuc phut: nguoi
#  dung khong biet con bao lau, tuong ung dung treo roi tat di.
#
#  Ghi tien do ra MOT TEP RIENG (progress.json) chu khong vao metadata.json: metadata bi
#  doc/ghi o rat nhieu cho, dap vao no vai giay mot lan la mo duong cho ghi de mat du
#  lieu. Tep rieng thi chi mot noi ghi, endpoint /status chi doc.
def _fmt_hms(sec: float) -> str:
    """Giay -> "1:23:45" / "5:07" — de nguoi dung doc duoc con bao lau."""
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s2 = divmod(r, 60)
    return f"{h}:{m:02d}:{s2:02d}" if h else f"{m}:{s2:02d}"


PROGRESS_NAME = "render_progress.json"


def progress_path(pdir: str) -> str:
    return os.path.join(pdir, PROGRESS_NAME)


def set_progress(pdir: str, percent: float, message: str) -> None:
    """Ghi tien do buoc 1/2. KHONG BAO GIO nem loi — tien do hong khong duoc lam chet
    lua xuat video."""
    try:
        tmp = progress_path(pdir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"percent": max(0.0, min(100.0, float(percent))),
                 "message": str(message or ""),
                 "at": time.time()},
                f,
            )
        os.replace(tmp, progress_path(pdir))
    except Exception:
        pass


def read_progress(pdir: str) -> dict:
    """Doc tien do buoc 1/2, tra {} neu chua co / khong doc duoc."""
    try:
        with open(progress_path(pdir), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def clear_progress(pdir: str) -> None:
    try:
        os.remove(progress_path(pdir))
    except OSError:
        pass


def _intermediate_v_opts(pdir: str, total: float, cw: int, ch: int, fps: float) -> list:
    """Thông số nén HÌNH cho bản dựng lại ở BƯỚC 1/2.

    Bản này KHÔNG phải bản người dùng nhận: bước 2/2 còn đốt phụ đề / logo / watermark
    lên nó rồi nén lại lần nữa. Nén kỹ ở bước 1 là trả giá hai lần mà không ai thấy —
    chất lượng cuối do lượt nén Ở BƯỚC 2 quyết định.

    Nên bước 1 đi ``ultrafast`` với ``crf`` THẤP HƠN (18 thay vì 20): nhanh hơn nhiều mà
    vẫn giữ nhiều chi tiết hơn cho lượt nén cuối. Đo trên video 1080p30:

        bước 1 veryfast crf20 -> bước 2  :  28,1s   PSNR cuối 35,77 dB  SSIM 0,9919
        bước 1 ultrafast crf18 -> bước 2 :  17,7s   PSNR cuối 36,04 dB  SSIM 0,9935

    tức nhanh hơn 37% và bản cuối còn NÉT HƠN một chút. Với phim 2 giờ, phần tiết kiệm
    này tính bằng chục phút.

    ĐÁNH ĐỔI DUY NHẤT là tệp trung gian to gấp ~3 (45MB -> 133MB cho 60 giây 1080p).
    Nên chỉ đi đường nhanh khi ổ đĩa CÒN ĐỦ CHỖ; thiếu chỗ thì quay về ``veryfast crf20``
    như trước — chậm hơn nhưng nhẹ đĩa, không bao giờ để người dùng hết đĩa giữa chừng.
    """
    # Ước lượng tệp trung gian. Đo thực tế trên 1080p30 với ``testsrc2`` — hình nhiễu,
    # tức trường hợp NÉN KHÓ NHẤT nên con số này là trần trên, video thật nhẹ hơn:
    #     bản trung gian (ultrafast crf18) : 2,22 MB/s
    #     bản cuối       (veryfast  crf20) : 0,74 MB/s
    # Quy đổi theo số điểm ảnh và fps để phim 4K / 60fps cũng ước đúng.
    px_ratio = max(0.1, (cw * ch) / (1920 * 1080)) * max(0.5, fps / 30.0)

    def _can(mb_giay: float) -> int:
        """Số byte ổ đĩa cần cho MỘT lựa chọn nén: bản trung gian + bản cuối + đệm."""
        mid = total * mb_giay * 1024 ** 2 * px_ratio
        final = total * 0.74 * 1024 ** 2 * px_ratio
        # Hai tệp cùng tồn tại trong lúc bước 2 chạy, cộng 2GB đệm cho tệp tạm khác.
        return int(mid + final) + 2 * 1024 ** 3

    try:
        free = shutil.disk_usage(pdir).free
    except OSError:
        free = 0          # không đo được ổ -> coi như không đủ, đi đường an toàn

    #  ƯU TIÊN ENCODER GPU nếu máy có.
    #
    #  Trước đây bước này LUÔN dùng libx264 dù máy có card — trong khi bước 2/2 thì đã
    #  biết dùng GPU từ lâu. Người dùng nhìn Task Manager thấy đúng điều đó: ffmpeg ăn
    #  gần 50% CPU suốt chặng dài nhất, còn GPU thì ngồi chơi.
    #
    #  Lợi thấy rõ nhất ở phim dài: bản trung gian của phim 3 tiếng ước ~32,5GB theo
    #  đường libx264, xuống còn ~14GB theo GPU — mà bước 2/2 còn phải đọc lại nguyên
    #  tệp đó. Chi tiết số đo: xem ``CompositorService.intermediate_encode_args``.
    try:
        from services.compositor_service import CompositorService

        gpu_opts, gpu_mb, gpu_nhan = CompositorService.intermediate_encode_args()
    except Exception as e:  # noqa: BLE001 — dò GPU hỏng thì cứ đi đường CPU
        logger.info(L(f"[nâng cao] Không dò được encoder GPU ({e}) — dùng CPU.", f"[advanced] Could not detect a GPU encoder ({e}) — using CPU."))
        gpu_opts, gpu_mb, gpu_nhan = [], 0.0, ""

    if gpu_opts and free >= _can(gpu_mb):
        logger.info(
            L(f"[nâng cao] Bước 1/2 nén bằng GPU ({gpu_nhan}) — bản này chỉ là bản trung "
            f"gian cho bước 2/2. Ước cần {_can(gpu_mb) / 1024 ** 3:.1f}GB, còn "
            f"{free / 1024 ** 3:.1f}GB.", f"[advanced] Step 1/2 compresses on GPU ({gpu_nhan}) — this file is only an intermediate for step 2/2. Needs about {_can(gpu_mb) / 1024 ** 3:.1f}GB, {free / 1024 ** 3:.1f}GB left.")
        )
        return list(gpu_opts)

    #  Vào tới đây là ĐI BẰNG CPU. Nói rõ VÌ SAO trong nhật ký: người dùng báo "có GPU
    #  mà ffmpeg vẫn ăn CPU" thì nhìn đúng một dòng này là biết máy không dò ra card hay
    #  chỉ là thiếu chỗ trên ổ.
    vi_sao_cpu = (L("máy không dò ra encoder GPU", "no GPU encoder detected on this machine") if not gpu_opts
                  else L(f"có {gpu_nhan} nhưng ổ đĩa không đủ chỗ cho nó", f"{gpu_nhan} is present but the disk does not have enough room for it"))
    need = _can(2.22)
    if free >= need:
        logger.info(
            L(f"[nâng cao] Bước 1/2 nén nhanh bằng CPU (ultrafast crf18) — {vi_sao_cpu}. "
            f"Bản này chỉ là bản trung gian cho bước 2/2. Ước cần "
            f"{need / 1024 ** 3:.1f}GB, còn {free / 1024 ** 3:.1f}GB.", f"[advanced] Step 1/2 fast compression on CPU (ultrafast crf18) — {vi_sao_cpu}. This file is only an intermediate for step 2/2. Needs about {need / 1024 ** 3:.1f}GB, {free / 1024 ** 3:.1f}GB left.")
        )
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                "-pix_fmt", "yuv420p"]
    logger.warning(
        L(f"[nâng cao] Ổ đĩa còn {free / 1024 ** 3:.1f}GB, không đủ cho bản trung gian nén "
        f"nhanh (~{need / 1024 ** 3:.1f}GB) — dùng nén gọn hơn (veryfast crf20). Bước này "
        "sẽ lâu hơn; giải phóng dung lượng để xuất nhanh hơn.", f"[advanced] The disk has {free / 1024 ** 3:.1f}GB left, not enough for the fast-compressed intermediate (~{need / 1024 ** 3:.1f}GB) — using tighter compression (veryfast crf20). This step will take longer; free up space to export faster.")
    )
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p"]


async def _render_composed(project_id: str, pdir: str, meta: dict, video_path: str,
                           master_tts: str, tts_gain: float,
                           duck_windows: list, out_path: str,
                           on_progress=None) -> None:
    """DUNG LAI hinh tu nhieu clip video + tron toan bo tieng, trong MOT lenh ffmpeg.

    Chi chay khi lan video da bi chinh (cat, keo, them clip, de PiP). Truong hop pho
    bien nhat — mot video goc nguyen ven — van di duong ``-c:v copy`` cu cho nhanh.

    Cach dung hinh: mot khung DEN dai bang timeline, roi lan luot ``overlay`` tung clip
    vao dung khoang thoi gian cua no. Cho nao khong co clip nao thi con lai mau den.
    Lam bang overlay (khong phai concat) de khong phai tinh tay cac khoang trong.

    CHI lay clip o LAN 0. Clip dang chong lan bi bo qua — moi thoi diem chi chieu duoc
    mot hinh, va nguoi dung da thay no nam o lan ngoai voi mau canh bao.
    """
    clips = [c for c in VID.playable_clips(meta) if os.path.exists(c.get("path") or "")]
    if not clips:
        #  Hai duong dan toi day: du an TRONG chua ai them gi, hoac tep cua moi clip da
        #  bien mat. Noi ro phai LAM GI thay vi mo ta trang thai — nguoi dung du an trong
        #  doc "lan video khong con clip nao" thi khong biet minh thieu buoc nao.
        raise RuntimeError(
            L("Chưa có hình nào để xuất. Hãy thêm ít nhất một ảnh hoặc clip video vào "
            "timeline (tab Nguyên liệu) trước khi xuất.", "There is no picture to export yet. Add at least one image or video clip to the timeline (Materials tab) before exporting.")
        )
    skipped = VID.overlap_count(meta)
    if skipped:
        logger.warning(
            L(f"[nâng cao] BỎ QUA {skipped} clip video đang chồng lấn — chúng nằm ở làn "
            f"ngoài và không vào bản xuất. Kéo chúng vào chỗ trống nếu muốn dùng.", f"[advanced] SKIPPING {skipped} overlapping video clips — they sit in an outer lane and are not part of the export. Drag them into an empty spot if you want to use them.")
        )

    cw, ch = VID.canvas_size(meta)
    fps = VID.canvas_fps(meta)
    audio_clips = [c for c in AUD.list_clips(meta) if c.get("enabled", True)
                   and os.path.exists(c.get("path") or "")]

    total = max(
        max((VID.clip_end(c) for c in clips), default=0.0),
        max((AUD._num(c.get("start")) + AUD.visible_duration(c) for c in audio_clips),
            default=0.0),
        max((w[1] for w in duck_windows), default=0.0),
    )
    total = max(0.5, round(total, 3))
    logger.info(
        L(f"[nâng cao] Dựng lại hình từ {len(clips)} clip video — khung {cw}x{ch} "
        f"@{fps:g}fps, dài {total:.1f}s.", f"[advanced] Rebuilding the picture from {len(clips)} video clips — frame {cw}x{ch} @{fps:g}fps, {total:.1f}s long.")
    )

    # STEM cua tung clip (neu da tach): tron 2 duong rieng voi 4 muc cua clip do thay vi
    # lay duong tieng chung cua tep. Nho vay MOI video them vao deu ha duoc nhac nen /
    # giong goc rieng, khong chi co video goc.
    use_source_flag = bool(meta.get("source_audio_enabled", True))
    stems_by_clip = {}
    for c in clips:
        if c.get("muted") or not bool(c.get("use_stems", True)):
            continue
        if VID.is_source_clip(c, video_path) and not use_source_flag:
            continue
        st = VID.clip_stems(pdir, c, video_path)
        if st:
            stems_by_clip[c["id"]] = st

    cmd = [FFMPEG_EXE, "-y"]
    for c in clips:
        if VID.clip_is_image(c):
            # ANH thi phai noi ro DAI BAO NHIEU (``-loop 1 -t``), khong thi luong hinh chi
            # co MOT khung va overlay khong co gi de chieu suot khoang thoi gian cua clip.
            cmd += [
                "-loop", "1",
                "-t", f"{max(0.05, VID.visible_duration(c)):.3f}",
                "-framerate", f"{fps:g}",
                "-i", os.path.abspath(c["path"]),
            ]
        else:
            cmd += ["-i", os.path.abspath(c["path"])]
    tts_idx = len(clips)
    cmd += ["-i", master_tts]
    audio_idx0 = tts_idx + 1
    for c in audio_clips:
        cmd += ["-i", os.path.abspath(c["path"])]
    # Stem duoc dua vao lam input RIENG (2 tep moi clip da tach).
    stem_idx = {}
    next_idx = audio_idx0 + len(audio_clips)
    for cid, st in stems_by_clip.items():
        stem_idx[cid] = {}
        for kind in ("no_vocals", "vocals"):
            cmd += ["-i", os.path.abspath(st[kind])]
            stem_idx[cid][kind] = next_idx
            next_idx += 1
    # Dai im lang lam MOC THOI LUONG cho ban tron (phai co atrim, khong thi vo han).
    anchor_idx = next_idx
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]

    lines = []
    # ---- HINH: hai duong dung, chon theo hinh dang cua lan video ----
    #
    # ``overlay`` (duong tong quat) xep tung clip len mot khung den dai bang timeline.
    # Nhuoc diem: MOI clip phai giai ma + scale suot ca video roi moi bi ``enable`` chan,
    # nen cang nhieu clip cang cham gan nhu tuyen tinh. Do tren 1080p30, cung 60 giay:
    #      3 clip  ->  9,9s     10 clip -> 16,1s     30 clip -> 32,4s
    #
    # ``concat`` xu ly cac clip LAN LUOT nen gan nhu khong phu thuoc so clip:
    #      3 clip  ->  7,6s     10 clip ->  8,2s     30 clip -> 10,1s   (nhanh 3,2 lan)
    #
    # Nhung concat CHI DUNG khi cac clip noi tiep khong ho: no khong biet chen khoang
    # den. Truong hop hay gap nhat cua tab Chi tiet — cat bo doan giua roi cac clip don
    # lai sat nhau — chinh la truong hop nay. Da doi chieu hai duong tren cung du lieu:
    # PSNR = inf (giong het tung diem anh), do dai trung khop.
    gap = 0.0
    contiguous = True
    for i, c in enumerate(clips):
        st = VID._num(c.get("start"))
        if abs(st - gap) > 0.001:      # ho hoac chong -> khong concat duoc
            contiguous = False
            break
        gap = VID.clip_end(c)
    # Con tieng/nhac keo dai qua het clip cuoi thi ban xuat phai dai bang ``total``;
    # concat chi cho ra dung tong do dai cac clip nen truong hop do van phai overlay.
    if contiguous and abs(gap - total) > 0.05:
        contiguous = False

    if contiguous and len(clips) > 1:
        logger.info(
            L(f"[nâng cao] {len(clips)} clip nối tiếp nhau — ghép bằng concat "
            "(nhanh hơn nhiều so với xếp lớp, hình ra giống hệt).", f"[advanced] {len(clips)} clips run back to back — joined with concat (much faster than layering, identical picture).")
        )
        pads = []
        for i, c in enumerate(clips):
            lines.append(_video_chain(i, f"[vc{i}]", c, cw, ch, fps, place=False))
            pads.append(f"[vc{i}]")
        lines.append(f"{''.join(pads)}concat=n={len(clips)}:v=1:a=0[vcat]")
        lines.append("[vcat]format=yuv420p[vout]")
    else:
        lines.append(
            f"color=c=black:s={cw}x{ch}:r={fps:g}:d={total:.3f},format=yuv420p[base]"
        )
        acc = "[base]"
        for i, c in enumerate(clips):
            lines.append(_video_chain(i, f"[vc{i}]", c, cw, ch, fps))
            start = VID._num(c.get("start"))
            end = VID.clip_end(c)
            nxt = f"[vo{i}]"
            lines.append(
                f"{acc}[vc{i}]overlay=x=0:y=0:eof_action=pass:repeatlast=0:"
                f"enable='between(t,{start:.3f},{end:.3f})'{nxt}"
            )
            acc = nxt
        lines.append(f"{acc}format=yuv420p[vout]")

    # ---- Tieng ----
    lines.append(
        f"[{anchor_idx}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
        f"aformat=sample_rates=44100:channel_layouts=stereo[abase]"
    )
    lines.append(
        f"[{tts_idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
        f"volume={tts_gain:.3f}[tts]"
    )
    apads = ["[abase]", "[tts]"]

    # Tieng CUA CHINH cac clip video. Manh cua video goc van giu net "tu dong ha tieng
    # khi AI noi" bang he so ducking suy ra tu 2 muc am luong nguoi dung da chon.
    use_source = bool(meta.get("source_audio_enabled", True))
    ambient = float(meta.get("bg_volume", 0.0) or 0.0)
    duck = float(meta.get("voice_bg_volume", 0.0) or 0.0)
    # duck/ambient — KHONG kep tran 1.0: duong copy nhanh (_duck_expr) cho phep muc "khi
    # AI noi" to hon muc nen, kep o day thi hai duong lech nhau o dung cai cau hinh do.
    ratio = max(0.0, duck / ambient) if ambient > 0.001 else 0.0
    if stems_by_clip:
        logger.info(
            L(f"[nâng cao] {len(stems_by_clip)}/{len(clips)} clip video dùng 2 stem đã tách "
            f"(nhạc nền & giọng gốc tách mức riêng theo từng clip).", f"[advanced] {len(stems_by_clip)}/{len(clips)} video clips use the 2 separated stems (background music & original voice split per clip).")
        )
    no_stems = [c for c in clips if c["id"] not in stems_by_clip and not c.get("muted")]
    if meta.get("separate_audio") and use_source and no_stems:
        # Stem la CUA TUNG TEP: clip nao chua tach thi van chi co duong tieng chung, khong
        # tach rieng nhac nen / giong goc duoc. Noi ro tung clip de nguoi dung biet bam
        # "Tách giọng & nhạc nền" cho clip nao.
        logger.warning(
            L("[nâng cao] Chưa tách stem cho: ", "[advanced] Stems not separated for:")
            + ", ".join((c.get("name") or c["id"]) for c in no_stems[:5])
            + L(" — những clip này dùng đường tiếng chung. Mở bảng thuộc tính clip rồi "
            "bấm “Tách giọng & nhạc nền” nếu muốn hạ riêng nhạc nền / giọng gốc.", ' — these clips use the shared audio track. Open the clip properties panel and click "Separate voice & music" if you want to lower the background music / original voice on their own.')
        )
    for i, c in enumerate(clips):
        if c.get("muted"):
            continue
        # ---- Clip DA TACH STEM: tron 2 duong rieng voi 4 muc CUA CHINH CLIP DO ----
        # Duong nay khong dung ``[i:a]`` cua tep nua. Nho vay moi video them vao deu ha
        # duoc nhac nen / giong goc rieng, dung 4 nut giong nhu cap du an.
        if c["id"] in stem_idx:
            vol = VID._num(c.get("volume"), 1.0)
            start = VID._num(c.get("start"))
            t_in, t_out = VID._num(c.get("in")), VID._num(c.get("out"))
            spd = VID.clip_speed(c)
            # CUNG MOT LUAT voi nhanh khong-stem ben duoi (xem VID.is_source_clip). Truoc
            # day nhanh nay lay ``clip_mix`` cho MOI clip: nguoi dung them mot clip hai
            # roi bam "Tach giong & nhac nen" de ha rieng nhac nen — muc chung cua du an
            # long tieng la 0% nen ca hai stem ve 0 va clip IM RU trong ban xuat, dung
            # cai viec tach ra lai lam mat tieng.
            own = VID.is_source_clip(c, video_path) or VID.has_own_mix(c)
            mix = VID.clip_mix(meta, c) if own else None
            for kind, base_k, duck_k in (
                ("no_vocals", "bg", "bg_duck"),
                ("vocals", "vocals", "vocals_duck"),
            ):
                # Chua cham 4 muc: hai stem deu de nguyen am luong cua clip — cong lai
                # dung bang duong tieng goc, tuc TACH XONG NGHE Y NHU TRUOC KHI TACH.
                base = mix[base_k] * vol if mix else vol
                ducked = mix[duck_k] * vol if mix else vol
                if base <= 0.001 and ducked <= 0.001:
                    continue
                pad = f"[vs{i}{kind[0]}]"
                lines.append(_audio_chain(
                    stem_idx[c["id"]][kind], pad,
                    start=start, t_in=t_in, t_out=t_out, speed=spd,
                    volume=base if base > 0.001 else 0.0001,
                    # ratio = muc "khi AI noi" / muc nen -> _audio_chain tu dung
                    # between() ha xuong dung con so nguoi dung chon.
                    duck_ratio=(ducked / base) if base > 0.001 else 0.0,
                    duck_windows=duck_windows,
                ))
                apads.append(pad)
            continue

        if not c.get("has_audio", True):
            continue
        # HAI LOAI TIENG, HAI LUAT KHAC NHAU (xem VID.is_source_clip):
        #
        #  * Manh cua VIDEO GOC = "tieng goc" cua du an long tieng -> nut rieng cua clip
        #    NHAN muc chung (bg_volume) va bi ha khi giong AI noi. Truoc day chi lay nut
        #    rieng (mac dinh 1.0) nen hai duong xuat lech mot troi mot vuc: du an long
        #    tieng de "tieng goc = 0%", dang xuat im ru, cat MOT clip la nhay sang duong
        #    dung lai hinh va tieng Trung quay ve DU 100%.
        #  * Clip nguoi dung THEM VAO -> nguyen lieu rieng, nghe dung nut am luong cua no,
        #    KHONG nhan muc chung va KHONG bi ha. Nhan voi bg_volume (mac dinh 0.0) thi
        #    thanh ra them clip vao la mat tieng, ma nut am luong bam vo tac dung.
        #    Cung luat voi moi doan tren lan nhac — chung cung khong bi ha bao gio.
        is_src = VID.is_source_clip(c, video_path)
        if is_src and not use_source:
            continue                      # da tat tieng goc thi manh nao cua no cung tat
        vol = VID._num(c.get("volume"), 1.0)
        if is_src or VID.has_own_mix(c):
            # Manh video goc -> luon qua bo tron 2 muc (mac dinh la muc cua du an).
            # Clip tu them ma nguoi dung DA cham 4 muc -> ton trong con so ho dat.
            mix = VID.clip_mix(meta, c)
            base, ducked = mix["bg"] * vol, mix["bg_duck"] * vol
        else:
            # Clip tu them, chua cham 4 muc: nghe nguyen am luong cua no va KHONG bi ha —
            # nhan voi muc chung (du an long tieng de 0%) la mat tieng oan.
            base = ducked = vol
        if base <= 0.001 and ducked <= 0.001:
            continue
        lines.append(_audio_chain(
            i, f"[va{i}]",
            start=VID._num(c.get("start")),
            t_in=VID._num(c.get("in")), t_out=VID._num(c.get("out")),
            speed=VID.clip_speed(c),
            volume=base if base > 0.001 else 0.0001,
            # base == ducked -> _audio_chain tu bo qua between(), tuc khong ha tieng.
            duck_ratio=(ducked / base) if base > 0.001 else 0.0,
            duck_windows=duck_windows,
        ))
        apads.append(f"[va{i}]")

    for j, c in enumerate(audio_clips):
        lines.append(_audio_chain(
            audio_idx0 + j, f"[ma{j}]",
            start=AUD._num(c.get("start")),
            t_in=AUD._num(c.get("in")), t_out=AUD._num(c.get("out")),
            speed=AUD.clip_speed(c),
            volume=AUD._num(c.get("volume"), 0.35),
            fade_in=AUD._num(c.get("fade_in"), 0.0),
            fade_out=AUD._num(c.get("fade_out"), 0.0),
            loop=bool(c.get("loop", False)), video_len=total,
        ))
        apads.append(f"[ma{j}]")

    lines.append(
        f"{''.join(apads)}amix=inputs={len(apads)}:duration=first:dropout_transition=0:"
        "normalize=0,"
        "dynaudnorm=f=500:g=15:p=0.95:m=10,"
        "alimiter=limit=1:level_in=1.25:level_out=1[aout]"
    )

    script = os.path.join(pdir, "filter_compose.txt")
    with open(script, "w", encoding="utf-8") as f:
        f.write(";\n".join(lines))
    cmd += [
        "-filter_complex_script", script,
        "-map", "[vout]", "-map", "[aout]",
    ]
    v_opts = _intermediate_v_opts(pdir, total, cw, ch, fps)
    cmd += v_opts
    #  KHÔNG đặt ``-pix_fmt`` ở đây: mỗi encoder đòi một khuôn điểm ảnh khác nhau
    #  (libx264/NVENC → yuv420p, QSV → nv12), và tham số đặt SAU sẽ đè lên tham số của
    #  encoder. ``_intermediate_v_opts`` trả về kèm ``-pix_fmt`` đúng của nó.
    cmd += [
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", out_path,
    ]
    def _chay(lenh: list) -> tuple:
        rc, err = _engine._run_ffmpeg_cancellable(
            lenh, project_id=project_id,
            on_progress=on_progress, duration_sec=(total if on_progress else 0.0),
        )
        xong = rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
        return xong, err

    try:
        xong, err = _chay(cmd)

        #  ENCODER GPU HỎNG GIỮA CHỪNG -> LUI VỀ CPU, đừng làm hỏng cả lượt xuất.
        #
        #  Bộ dò encoder chỉ thử encode MỘT khung hình; có card/driver qua được phép
        #  thử đó nhưng chết khi gặp video thật (độ phân giải lẻ, quá dài, hết bộ nhớ
        #  GPU). Không có lưới này thì việc bật GPU biến một lượt xuất vốn chạy được
        #  thành lỗi hẳn — tức là làm hại đúng người mình định giúp.
        #
        #  ``RenderCancelled`` KHÔNG rơi vào đây (nó là exception, không phải rc != 0),
        #  nên bấm Huỷ vẫn dừng thật chứ không bị thử lại bằng CPU.
        if not xong and "libx264" not in v_opts:
            logger.warning(
                L(f"[nâng cao] Encoder GPU thất bại ở bước 1/2, chạy lại bằng CPU "
                f"(libx264). Lỗi GPU: {err[-600:]}", f"[advanced] The GPU encoder failed at step 1/2, rerunning on CPU (libx264). GPU error: {err[-600:]}")
            )
            cpu_opts = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                        "-pix_fmt", "yuv420p"]
            lenh_cpu = [x for x in cmd]
            i = lenh_cpu.index(v_opts[0])
            lenh_cpu[i:i + len(v_opts)] = cpu_opts
            xong, err = _chay(lenh_cpu)

        if not xong:
            logger.error(L(f"[nâng cao] Dựng lại hình thất bại: {err[-2000:]}", f"[advanced] Rebuilding the picture failed: {err[-2000:]}"))
            raise RuntimeError(L("Dựng lại hình thất bại — xem log để biết chi tiết.", "Rebuilding the picture failed — see the log for details."))
    finally:
        safe_remove(script)


# --------------------------------------------------------------------------- #
#  Dung phim
# --------------------------------------------------------------------------- #
def _luu_ket_qua(pdir: str, b_chunks: list, a_lookup: dict, cat_doan: bool,
                 ranges, khoang, co_cat_cut: bool = True) -> None:
    """Chốt sổ sau khi dựng xong — hai kiểu tuỳ lượt xuất là ĐẦY ĐỦ hay ĐOẠN ĐÃ CHỌN.

    Lượt xuất ĐOẠN chỉ là một bản phụ, KHÔNG được chạm vào dữ liệu dự án: ``b_chunks``
    lúc này chỉ còn vài câu và mốc đã dời sang dòng thời gian mới, ghi đè lên tệp .srt
    hay cờ cắt cụt là làm hỏng dự án thật mà không ai thấy. Đổi lại nó để lại BIÊN NHẬN
    cho bước 2/2 biết bản cắt đã sẵn, khỏi cắt thêm lần nữa.
    """
    if cat_doan:
        from utils import timeline_slice as TS

        TS.ghi_manifest(pdir, ranges, khoang)
        return
    _write_srt(pdir, b_chunks, a_lookup)
    if co_cat_cut:
        _persist_trim_flags(pdir, b_chunks)


async def render(project_id: str, ranges=None) -> str:
    """Ghep moi thu tren timeline thanh video: hinh, giong doc, nhac, tieng goc.

    KHONG xe dich, KHONG tu tang toc — vi tri tren timeline la vi tri cuoi cung.

    HAI DUONG:
      * Lan video con la MOT clip nguyen ven cua video goc (truong hop long tieng thong
        thuong) -> giu ``-c:v copy``: nhanh hon nhieu lan va khong mat chat luong hinh.
      * Lan video da bi chinh (cat, keo, them clip, de PiP) -> dung lai hinh bang
        ``_render_composed``, buoc nay phai ma hoa lai.

    ``ranges`` = cac khoang [giay, giay] nguoi dung khoanh tren timeline de "xuat doan
    da chon". Co thi CAT MOI NGUON DAU VAO truoc roi moi ghep, tuc chi ghep giong /
    tieng goc / nhac cua dung nhung doan do — khong dung ca phim roi cat. Xem khoi
    "XUAT DOAN DA CHON" ben duoi.
    """
    p = load_project(project_id)
    pdir, meta, a_lookup, b_chunks = p["dir"], p["meta"], p["a_lookup"], p["b_chunks"]

    # Xoa tien do cua lan dung TRUOC ngay tu dau: khong thi vai giay dau cua lan nay
    # UI hien lai con so cu (vd 97%) roi moi tut ve 2% — nhin nhu chay lui.
    clear_progress(pdir)
    set_progress(pdir, 1.0, L("Đang chuẩn bị ghép tiếng…", "Preparing voice merge…"))

    from services.separation_service import SeparationService

    #  DU AN TRONG (khong co phim goc) la HOP LE o che do chi tiet: nguoi dung tu ghep
    #  anh/clip/nhac tren timeline, vd chieu tung tam anh roi long loi binh cho moi tam.
    #  Phan biet hai ca, dung gop lam mot:
    #    * ``video_path`` RONG  -> du an von khong co phim goc. Xuat binh thuong bang
    #      duong ``_render_composed`` (no dung khung den theo canvas roi overlay tung
    #      clip — khong dong gi den phim goc).
    #    * ``video_path`` CO ma tep khong con  -> tep bi xoa / o chua cam. Van phai bao
    #      loi: nguoi dung dang cho mot ban xuat CO hinh phim goc trong do.
    raw_video = (meta.get("video_path") or "").strip()
    if raw_video:
        video_path, _ = SeparationService.resolve_source_media(raw_video)
        if not video_path or not os.path.exists(video_path):
            raise FileNotFoundError(L("Không tìm thấy video nguồn của dự án.", "Project source video not found."))
    else:
        video_path = ""

    # THOI LUONG VIDEO phai co that. Luc tao du an, neu doc thoi luong that bai (duong
    # dan go tay, o mang cham) thi metadata giu 0 — ma con so do la moc cho ca:
    #   * "lap nhac toi het video"  -> video_len = 0 nen KHONG lap lan nao,
    #   * dai im lang lam moc thoi luong khi tat tieng goc,
    #   * canh bao tieng vuot qua cuoi phim.
    # Do lai mot lan roi ghi vao metadata, khong de ba cho tu doan mo ho.
    if video_path and TL._num(meta.get("video_duration"), 0.0) <= 0:
        probed = TL._probe_duration_ffmpeg(video_path)
        if probed > 0:
            meta["video_duration"] = round(probed, 3)
            save_meta(pdir, meta)
            logger.info(L(f"[nâng cao] Đã đo lại thời lượng video gốc: {probed:.2f}s.", f"[advanced] Re-measured the source video duration: {probed:.2f}s."))

    if VID.ensure_migrated(meta, video_path):
        save_meta(pdir, meta)
    #  Khong co phim goc thi KHONG the di duong xuat nhanh ``-c:v copy`` — duong do lay
    #  chinh tep goc lam dau vao ffmpeg. ``is_untouched_single`` da tra False khi
    #  ``video_path`` rong, nhung ep ro o day de y dinh khong phu thuoc vao chi tiet do.
    composed = not video_path or not VID.is_untouched_single(meta, video_path)

    _engine._raise_if_cancelled(project_id)

    final_output = os.path.join(pdir, "final_review.mp4")
    tmp_output = os.path.join(pdir, "final_review.tmp.mp4")
    master_tts = os.path.join(pdir, "master_tts.wav")

    #  ==================== XUẤT ĐOẠN ĐÃ CHỌN ====================
    #  Luồng y hệt xuất cả video — chỉ khác: TIMELINE bị thu về đúng các đoạn đã khoanh
    #  TRƯỚC khi ghép. Nhờ vậy bước này chỉ gánh mấy chục giây thay vì cả phim.
    #
    #  VÌ SAO ĐÁNG LÀM: đo thật trên video 19m42s / 181MB, bước 1/2 mất 107,5 giây ngay
    #  cả khi đi đường ``-c:v copy`` và bản trộn giọng đã nằm sẵn trong cache — vì nó
    #  vẫn phải trộn lại đường tiếng dài 20 phút rồi ghi lại toàn bộ tệp. Phim 3 tiếng
    #  là ~16 phút. Trả cái giá đó để rồi cắt lấy 20 giây là vô lý.
    #
    #  CÁCH LÀM: KHÔNG cắt tệp nào cả — chỉ VIẾT LẠI DANH SÁCH CLIP (xem
    #  ``VID.cat_clip_hinh``). Một clip bị đoạn chọn cắt làm đôi thì thành hai bản ghi,
    #  mỗi bản trỏ vào một quãng khác của cùng tệp nguồn. Hình, tiếng của clip và 2 stem
    #  đã tách đều chạy theo cùng bộ ``in``/``out``/``speed``/``start`` ấy, nên sửa một
    #  chỗ là cả ba theo đúng. Bản trước cắt sẵn tệp nguồn + stem thành ``.dachon`` —
    #  cách đó chỉ đúng với dự án MỘT clip nguyên vẹn, mà dự án thật hay bị cắt/ghép nên
    #  gần như luôn rơi xuống đường lui; nay bỏ hẳn, đỡ luôn hai lượt ffmpeg.
    #
    #  LUI VỀ ĐƯỜNG CŨ (dựng cả phim rồi để bước 2/2 cắt) khi dự án CÓ NHẠC CHÈN: mỗi
    #  đoạn nhạc mang mốc/đoạn cắt/vòng lặp riêng, mà nhạc đang lặp thì không quy được
    #  về một cặp in/out — làm ẩu là lệch nhạc cả bản xuất.
    hau_to_cat = ""
    cat_doan = False
    khoang_cat = []
    if ranges:
        from utils import timeline_slice as TS

        co_nhac = any(c.get("enabled", True) and c.get("path")
                      for c in (AUD.list_clips(meta) or []))
        khoang = TS.gom_khoang(ranges, tong_dai=TL._num(meta.get("video_duration"), 0.0))
        clip_cat = [] if (co_nhac or not khoang) else VID.cat_clip_hinh(meta, khoang)
        if not clip_cat:
            #  Đường lui dựng CẢ PHIM rồi mới cắt. Mỗi đoạn là một lượt xuất riêng, nên
            #  khoanh 3 đoạn ở dự án có nhạc chèn là dựng cả phim 3 lần. Chấp nhận: xuất
            #  từ timeline là việc người dùng cố ý làm, và bộ nhớ đệm "bản dựng còn mới"
            #  đổi lấy rủi ro đưa ra một bản CŨ mà không có dấu hiệu gì.
            logger.info(
                L("[nâng cao] Xuất đoạn đã chọn: %s — dựng cả phim rồi cắt ở bước 2/2.", "[advanced] Export selected ranges: %s — building the whole film then cutting at step 2/2."),
                L("dự án có nhạc chèn", "the project has inserted music") if co_nhac else
                (L("không có đoạn nào hợp lệ", "no valid range") if not khoang
                 else L("các đoạn đã chọn không rơi vào clip hình nào", "the selected ranges do not fall in any video clip")),
            )
        else:
            cat_doan = True
            hau_to_cat = ".dachon"
            khoang_cat = khoang
            dai_moi = TS.tong_thoi_luong(khoang)
            logger.info(
                L(f"[nâng cao] Xuất ĐOẠN ĐÃ CHỌN: {len(khoang)} đoạn, tổng {dai_moi:.1f}s "
                f"(cả phim {TL._num(meta.get('video_duration'), 0.0):.1f}s) — dựng "
                f"{len(clip_cat)} mảnh hình, CHỈ ghép giọng của những đoạn này.", f"[advanced] Exporting SELECTED RANGES: {len(khoang)} ranges, {dai_moi:.1f}s total (whole film {TL._num(meta.get('video_duration'), 0.0):.1f}s) — building {len(clip_cat)} picture pieces, mixing the voice of ONLY these ranges.")
            )
            #  Dọn tàn dư của lượt xuất đoạn TRƯỚC: biên nhận cũ còn đó là bước 2/2
            #  tưởng đã có sẵn bản cắt hợp lệ.
            TS.don_ban_cat(pdir)

            #  Câu nào giao với đoạn đã chọn thì GIỮ và dời mốc về dòng thời gian mới.
            #  Ghi vào ``shift_start``/``shift_end`` vì ``resolve_b_time`` ưu tiên cặp
            #  đó cao nhất — nhờ vậy mọi nơi đọc mốc (chỗ chèn giọng, khoảng hạ tiếng
            #  gốc, phụ đề) đều dùng chung một con số, không nơi nào tự tính lại.
            #
            #  CÂU VẮT QUA MÉP ĐOẠN được đọc TỪ ĐẦU ngay tại mép, chứ không bị cắt giữa
            #  chừng. Đo được: câu 999,44–1004,67s với mép đoạn ở 999,88s thì bản đoạn
            #  sớm hơn bản đầy đủ 0,44 giây ở chỗ đó. Cố ý như vậy: nghe trọn câu vẫn
            #  hơn nghe nửa từ. Đây cũng là chỗ bản "xuất đoạn" khác bản đầy đủ — đường
            #  lui (cắt phim đã dựng) thì cắt đúng giữa từ.
            b_moi = []
            for b in b_chunks:
                s0, e0 = resolve_b_time(b, a_lookup)
                if s0 is None:
                    continue
                moc = TS._kep_vao_khoang(s0, e0, khoang)
                if moc is None:
                    continue
                b_moi.append({**b, "shift_start": moc[0], "shift_end": moc[1]})
            logger.info(L(f"[nâng cao] Giữ {len(b_moi)}/{len(b_chunks)} câu nằm trong đoạn đã chọn.", f"[advanced] Keeping {len(b_moi)}/{len(b_chunks)} sentences inside the selected ranges."))
            b_chunks = b_moi
            #  Bản sao meta — KHÔNG ghi xuống đĩa. Đây là timeline của riêng lượt xuất
            #  đoạn; ghi đè lên dự án là người dùng mở lại thấy phim chỉ còn mấy mảnh.
            meta = {**meta, "video_duration": dai_moi, "video_clips": clip_cat}
            #  ÉP dựng lại hình. Danh sách clip lúc này là những mảnh trỏ vào giữa tệp
            #  nguồn, không đời nào đi được đường ``-c:v copy`` — mà đường đó lấy thẳng
            #  tệp gốc làm đầu vào, nên lỡ đi vào là xuất ra NGUYÊN CẢ PHIM mà không báo
            #  gì. Ép ở đây để chuyện đó không phụ thuộc vào chi tiết của hàm khác.
            composed = True
            final_output = os.path.join(pdir, TS.TEP_BAN_CAT)
            tmp_output = os.path.join(pdir, "final_review.dachon.tmp.mp4")
            #  Bản trộn giọng của đoạn PHẢI để riêng: đè lên ``master_tts.wav`` thì lượt
            #  xuất ĐẦY ĐỦ sau đó vớ phải bản trộn dài mấy chục giây mà không báo gì.
            master_tts = os.path.join(pdir, f"master_tts{hau_to_cat}.wav")

    # 1. Gom cac clip giong + khoang can ha tieng goc (ducking)
    tts_to_mix, duck_windows = [], []
    n_trimmed = 0
    skipped_no_time = []
    tran = TL.tran_giong_cho_phep(meta)              # giong duoc tran qua mep khung (de len dau cau sau) truoc khi cat
    for b in b_chunks:
        start, end = resolve_b_time(b, a_lookup)
        if start is None:
            # Du an CU co the con cau vo chu (kich ban dan tay thieu ``source_a_ids``).
            # Duong nhap moi da tu dat cho cho chung (place_orphan_chunks), nhung du lieu
            # cu thi phai NOI RA — truoc day cau nay bi bo im lang, xuat xong moi phat
            # hien video thieu han mot doan loi doc.
            if (b.get("b_text") or "").strip():
                skipped_no_time.append(b.get("b_id"))
            continue
        window = max(0.0, float(end) - float(start))
        path, dur, trimmed = resolve_playback_audio(pdir, b, window, tran)
        if not path or dur <= 0:
            continue
        if trimmed:
            n_trimmed += 1
        b["is_trimmed"] = bool(trimmed)
        tts_to_mix.append({"path": os.path.abspath(path),
                           "delay_ms": int(float(start) * 1000),
                           "b_id": b.get("b_id")})
        duck_windows.append((float(start), float(start) + dur))

    if n_trimmed:
        logger.warning(L(f"[nâng cao] {n_trimmed} câu bị cắt cụt do khung thời gian ngắn hơn giọng đọc", f"[advanced] {n_trimmed} sentences were cut short because the time slot is shorter than the voice")
                       + (L(f" (đã cho tràn {tran:.1f}s).", f" (overflow allowed {tran:.1f}s).") if tran > 0 else "."))
    if skipped_no_time:
        logger.warning(
            L(f"[nâng cao] BỎ QUA {len(skipped_no_time)} câu chưa xác định được thời gian "
            f"(câu #{', #'.join(str(x) for x in skipped_no_time[:10])}) — chúng KHÔNG vào "
            f"bản xuất. Kéo chúng trên timeline một lần để đặt vị trí, rồi xuất lại.", f"[advanced] SKIPPING {len(skipped_no_time)} sentences whose timing is not determined (sentence #{', #'.join(str(x) for x in skipped_no_time[:10])}) — they are NOT in the export. Drag them once on the timeline to set a position, then export again.")
        )

    _engine._raise_if_cancelled(project_id)

    # Cac doan NHAC CHEN THEM cung duoc dua vao ban tron, dung vi tri / doan da cat /
    # am luong nguoi dung chinh tren timeline -> nghe thu the nao thi xuat ra the ay.
    audio_clips = AUD.list_clips(meta)
    extra_inputs = []
    for c in audio_clips:
        if not c.get("enabled", True):
            continue
        src = c.get("path") or ""
        if not os.path.exists(src):
            logger.warning(L(f"[nâng cao] Bỏ qua đoạn nhạc không còn trên đĩa: {src}", f"[advanced] Skipping a music clip that is no longer on disk: {src}"))
            continue
        extra_inputs.append({
            "path": os.path.abspath(src),
            "start": float(c.get("start") or 0.0),
            "in": float(c.get("in") or 0.0),
            "out": float(c.get("out") or 0.0),
            "volume": float(c.get("volume") if c.get("volume") is not None else 0.35),
            "loop": bool(c.get("loop", False)),
            "speed": AUD.clip_speed(c),
            "fade_in": float(c.get("fade_in") or 0.0),
            "fade_out": float(c.get("fade_out") or 0.0),
            "name": c.get("name") or "",
        })
    if extra_inputs:
        logger.info(L(f"[nâng cao] Ghép thêm {len(extra_inputs)} đoạn nhạc chèn.", f"[advanced] Mixing in {len(extra_inputs)} inserted music clips."))

    # Chua co tieng gi ma lan video cung con nguyen -> chi viec sao y video goc.
    # Lan video DA bi chinh thi van phai dung lai hinh, du khong co giong doc nao.
    #
    # NGOAI TRU hai truong hop, ca hai deu la "nguoi dung da noi ro y minh":
    #   * da TAT tieng goc — sao y ca duong tieng cua ban goc thi cong tac do thanh vo
    #     nghia (bam tat ma xuat ra van nghe nguyen tieng goc);
    #   * da CHINH 4 muc am luong (xem ``mix_touched``) — sao y thi keo slider xong xuat ra
    #     khong doi gi, va khung xem truoc cung phai noi doi theo cho khop.
    # Ca hai di duong tron ben duoi (van ``-c:v copy``, chi ma hoa lai tieng).
    #
    # Vi sao KHONG bo han duong tat: du an vua tao co ca 4 muc = 0, di duong tron luc do
    # cho ra video IM LANG — to hon nhieu cai duoc sua.
    if not tts_to_mix and not extra_inputs and not composed \
            and bool(meta.get("source_audio_enabled", True)) \
            and not mix_touched(meta):
        logger.warning(L("[nâng cao] Chưa có giọng đọc nào — giữ nguyên video gốc.", "[advanced] No voice yet — keeping the original video."))
        cmd = [FFMPEG_EXE, "-y", "-i", video_path, "-c", "copy", final_output]
        _engine._run_ffmpeg_cancellable(cmd, project_id=project_id)
        _luu_ket_qua(pdir, b_chunks, a_lookup, cat_doan, ranges, khoang_cat,
                     co_cat_cut=False)
        return final_output

    # 2. Tron tat ca giong thanh mot dai master (co cache theo dau van tay)
    fp = hashlib.md5(json.dumps(
        [{"p": x["path"], "d": x["delay_ms"],
          "m": os.path.getmtime(x["path"]), "s": os.path.getsize(x["path"])}
         for x in tts_to_mix], sort_keys=True).encode("utf-8")).hexdigest()
    fp_file = os.path.join(pdir, f"master_tts_fingerprint{hau_to_cat}.json")

    reuse = False
    if os.path.exists(master_tts) and os.path.exists(fp_file):
        try:
            with open(fp_file, "r", encoding="utf-8") as f:
                reuse = f.read().strip() == fp
        except Exception:
            reuse = False

    if reuse:
        logger.info(L("[nâng cao] Dùng lại bản trộn giọng đọc từ lần dựng trước.", "[advanced] Reusing the voice mix from the previous build."))
        set_progress(pdir, 8.0, L("Dùng lại bản trộn giọng của lần dựng trước", "Reusing the voice mix from the previous build"))
    else:
        logger.info(L(f"[nâng cao] Đang trộn giọng đọc của {len(tts_to_mix)} câu…", f"[advanced] Mixing the voice of {len(tts_to_mix)} sentences…"))
        set_progress(pdir, 2.0, L(f"Đang trộn giọng đọc của {len(tts_to_mix)} câu…", f"Mixing the voice of {len(tts_to_mix)} sentences…"))
        if not _engine._mix_tts_audios_batched(tts_to_mix, master_tts, project_id=project_id):
            raise RuntimeError(L("Trộn giọng đọc thất bại.", "Voice mixing failed."))
        set_progress(pdir, 8.0, L(f"Đã trộn xong {len(tts_to_mix)} câu giọng đọc", f"Finished mixing the voice of {len(tts_to_mix)} sentences"))
        try:
            with open(fp_file, "w", encoding="utf-8") as f:
                f.write(fp)
        except Exception:
            pass

    _engine._raise_if_cancelled(project_id)

    # 2b. Tron cac doan nhac chen them vao master (cat theo in/out, dat dung start).
    #
    # AM LUONG GIONG DOC — mac dinh 1.5 (nhu che do co dien), gio chinh duoc trong tab
    # Chuan bi. Cho no vao NGAY buoc nay thay vi buoc cuoi: neu de buoc cuoi thi cac doan
    # nhac — da nam trong master — cung an them he so nay, tuc nghe thu mot dang ma xuat
    # ra mot kieu.
    #
    # BAN SAO GUONG: khung xem truoc ap dung con so nay cho lan giong (``voiceVolume``
    # cua useTimelinePlayback). Doi mac dinh o mot ben thi PHAI doi ben kia.
    tts_gain = tts_volume(meta)

    # ---- DUONG DUNG LAI HINH: lam tat ca trong MOT lenh, khong qua buoc 2b/3 ----
    # (nhac chen duoc dua thang vao lenh do, nen o day khong tron truoc nua)
    if composed:
        merged_duck = []
        for w in sorted(duck_windows):
            if merged_duck and w[0] <= merged_duck[-1][1]:
                merged_duck[-1][1] = max(merged_duck[-1][1], w[1])
            else:
                merged_duck.append([w[0], w[1]])
        # Tien do THAT cua chang dot hinh: ffmpeg bao da lam den giay thu bao nhieu,
        # quy ve 10..99% (0..10% da danh cho chang tron giong o tren).
        def _vid_progress(done: float, total_sec: float) -> None:
            if total_sec <= 0:
                return
            pct = 10.0 + min(1.0, done / total_sec) * 89.0
            set_progress(
                pdir, pct,
                L(f"Đang ghép tiếng vào hình — {_fmt_hms(done)}/{_fmt_hms(total_sec)}", f"Merging voice into the picture — {_fmt_hms(done)}/{_fmt_hms(total_sec)}"),
            )

        try:
            await _render_composed(
                project_id, pdir, meta, video_path, master_tts, tts_gain,
                [tuple(w) for w in merged_duck], tmp_output,
                on_progress=_vid_progress,
            )
            safe_replace(tmp_output, final_output)
        finally:
            safe_remove(tmp_output)
        _luu_ket_qua(pdir, b_chunks, a_lookup, cat_doan, ranges, khoang_cat)
        # KHONG goi day la "xuat video hoan tat": moi xong BUOC 1/2 (ghep tieng vao
        # phim). Buoc 2 — dot phu de/logo/watermark/chu len hinh — con chay tiep vai
        # phut nua. Cau chu cu khien nguoi dung doc nhat ky thay "HOAN TAT" trong khi
        # thanh tien do van dang chay "Dot hinh 72%", tuong ung dung treo.
        logger.info(L(f"[nâng cao] === BƯỚC 1/2: GHÉP TIẾNG XONG: {final_output} ===", f"[advanced] === STEP 1/2: VOICE MERGE DONE: {final_output} ==="))
        logger.info(L("[nâng cao] Tiếp bước 2/2 — đốt phụ đề & lớp phủ lên hình…", "[advanced] Continuing to step 2/2 — burning subtitles & overlays onto the picture…"))
        return final_output

    if extra_inputs:
        master_with_music = os.path.join(pdir, "master_mix.wav")
        cmd = [FFMPEG_EXE, "-y", "-i", master_tts]
        filters = [
            f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"volume={tts_gain:.3f}[m0]"
        ]
        pads = ["[m0]"]
        video_len = float(meta.get("video_duration") or 0.0)
        for i, x in enumerate(extra_inputs, start=1):
            cmd += ["-i", x["path"]]
            seg_src = max(0.05, x["out"] - x["in"]) if x["out"] > x["in"] else 0.0
            trim = f"atrim=start={x['in']:.3f}" + (f":end={x['out']:.3f}" if seg_src else "")
            delay = int(max(0.0, x["start"]) * 1000)
            chain = (
                f"[{i}:a]{trim},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates=44100:channel_layouts=stereo"
            )
            # THU TU CAC BO LOC QUAN TRONG:
            #   atrim (theo giay cua tep nguon) -> atempo (doi do dai) -> aloop ->
            #   volume -> afade (theo giay tren TIMELINE) -> adelay (dat vao cho).
            # Dat afade truoc atempo la sai: vuot 1 giay se thanh 0.5 giay khi tang toc
            # gap doi, khong dung con so nguoi dung nhap.
            speed = float(x.get("speed") or 1.0)
            if abs(speed - 1.0) > 0.001:
                chain += f",atempo={speed:.4f}"
            # Do dai MOT LUOT tren timeline sau khi da doi toc do.
            seg = seg_src / speed if seg_src else 0.0
            length = seg
            # LAP TOI HET VIDEO: nhan ban doan da cat du so lan roi cat dung diem cuoi.
            # aloop dem bang MAU nen phai quy doi theo 44100Hz (da aformat o tren).
            if x["loop"] and seg > 0.05 and video_len > 0:
                need = max(0.0, video_len - x["start"])
                repeats = max(0, int(need / seg))
                if repeats > 0:
                    chain += (
                        f",aloop=loop={repeats}:size={int(seg * 44100)}"
                        f",atrim=0:{need:.3f}"
                    )
                    length = need
            chain += f",volume={x['volume']:.3f}"
            fade_in = max(0.0, min(float(x.get("fade_in") or 0.0), length))
            fade_out = max(0.0, min(float(x.get("fade_out") or 0.0), length))
            if fade_in > 0.01:
                chain += f",afade=t=in:st=0:d={fade_in:.3f}"
            if fade_out > 0.01:
                chain += f",afade=t=out:st={max(0.0, length - fade_out):.3f}:d={fade_out:.3f}"
            chain += f",adelay={delay}|{delay}[mx{i}]"
            filters.append(chain)
            pads.append(f"[mx{i}]")
        filters.append(
            f"{''.join(pads)}amix=inputs={len(pads)}:duration=longest:"
            f"dropout_transition=0:normalize=0[aout]"
        )
        script = os.path.join(pdir, "filter_music.txt")
        with open(script, "w", encoding="utf-8") as f:
            f.write(";\n".join(filters))
        cmd += ["-filter_complex_script", script, "-map", "[aout]",
                "-c:a", "pcm_s16le", master_with_music]
        #  TIẾN ĐỘ THẬT (8 -> 10%): trước đây lượt này không báo gì, thanh đứng im ở
        #  "Đã trộn xong N câu giọng đọc · 8%" suốt cả lượt — người dùng tưởng treo.
        _tong_nhac = TL._num(meta.get("video_duration"), 0.0)
        set_progress(pdir, 8.5, L("Đang trộn nhạc chèn vào giọng đọc…", "Mixing the inserted music into the voice…"))

        def _nhac_progress(done: float, total_sec: float) -> None:
            if total_sec > 0:
                set_progress(
                    pdir, 8.5 + min(1.0, done / total_sec) * 1.5,
                    L(f"Đang trộn nhạc chèn — {_fmt_hms(done)}/{_fmt_hms(total_sec)}", f"Mixing inserted music — {_fmt_hms(done)}/{_fmt_hms(total_sec)}"),
                )

        rc, err = _engine._run_ffmpeg_cancellable(
            cmd, project_id=project_id,
            on_progress=_nhac_progress if _tong_nhac > 0 else None,
            duration_sec=_tong_nhac,
        )
        safe_remove(script)
        if rc == 0 and os.path.exists(master_with_music) and os.path.getsize(master_with_music) > 0:
            master_tts = master_with_music
            tts_gain = 1.0      # da khuech dai o tren, buoc cuoi khong nhan lan nua
        else:
            logger.error(L(f"[nâng cao] Ghép nhạc chèn thất bại, bỏ qua nhạc: {err[-500:]}", f"[advanced] Merging the inserted music failed, music skipped: {err[-500:]}"))

    # 3. Ghep vao video: hinh giu nguyen (-c:v copy), tieng goc bi ha trong luc AI noi.
    #
    # Bat "tach giong & nhac nen" thi tron HAI STEM voi HAI muc am luong rieng — dung
    # ca 4 muc y nhu khung xem truoc dang phat. Truoc day buoc nay chi tron tieng goc
    # chung ([0:a]) va bo qua vocals_volume / voice_vocals_volume, nen ha "Giong goc"
    # xuong 0 thi nghe thu sach bong ma xuat ra van con nguyen giong goc.
    ambient = float(meta.get("bg_volume", 0.0) or 0.0)
    duck = float(meta.get("voice_bg_volume", 0.0) or 0.0)

    # Tieng goc cua video la MOT PHAN TU tat/bat duoc. Tat di thi ban xuat khong lay gi
    # tu video ca — dung khi nguoi dung da dua 2 stem len timeline va tu chinh am luong
    # tung doan (neu van lay tieng goc thi nghe doi, vi stem = tieng goc tach ra).
    use_source_audio = bool(meta.get("source_audio_enabled", True))

    stems = None
    if use_source_audio and meta.get("separate_audio", False):
        cand = {
            #  Stem NGUYÊN của cả tệp. Lượt "xuất đoạn đã chọn" không đi qua đây (nó bị
            #  ép sang đường dựng lại hình, nơi stem được cắt theo in/out của từng mảnh
            #  clip) nên không cần bản stem riêng.
            k: os.path.join(pdir, "separated", f"{k}.wav")
            for k in ("no_vocals", "vocals")
        }
        if all(os.path.exists(v) and os.path.getsize(v) > 0 for v in cand.values()):
            stems = cand
        else:
            logger.warning(
                L("[nâng cao] Dự án bật tách nhạc nền nhưng chưa có stem trên đĩa — "
                "lần này dùng tiếng gốc chung. Bấm “Lưu cấu hình” để tách trước.", '[advanced] The project has background music separation on but no stems on disk yet — this time the shared original audio is used. Click "Save settings" to separate first.')
            )

    merged = []
    for w in sorted(duck_windows):
        if merged and w[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], w[1])
        else:
            merged.append([w[0], w[1]])

    def _duck_expr(base: float, ducked: float) -> str:
        """Bieu thuc volume dong: nen ``base``, trong moi khoang AI noi thi ve ``ducked``.

        Cac khoang da duoc gop o tren nen moi thoi diem chi bi tru DUNG MOT lan —
        khong thi cac between() cong don co the day he so xuong am (dao pha).
        """
        d = ducked - base
        expr = f"{base}"
        if abs(d) > 0.001:
            for (s, e) in merged:
                expr += f"+({d}*between(t,{s:.3f},{e:.3f}))"
        return expr

    # Duong xuat nhanh giu nguyen hinh (``-c:v copy``) nen ban xuat DAI DUNG BANG video
    # goc: thu gi nam sau diem ket thuc do se bi cat mat. Noi ro ra thay vi de nguoi dung
    # tu phat hien khi mo file ra xem — muon giu thi phai keo no vao trong, hoac them mot
    # clip video o cuoi (luc do di duong dung lai hinh, timeline dai bao nhieu lay het).
    video_len_ref = float(meta.get("video_duration") or 0.0)
    if video_len_ref > 0:
        far = max(
            # Do dai chiem tren timeline = phan da cat CHIA toc do (dung AUD.visible_duration).
            [
                x["start"] + (x["out"] - x["in"]) / max(0.1, x["speed"])
                for x in extra_inputs
                if x["out"] > x["in"] and not x["loop"]
            ]
            + [w[1] for w in duck_windows]
            + [0.0]
        )
        if far > video_len_ref + 0.5:
            logger.warning(
                L(f"[nâng cao] Có tiếng nằm tới giây {far:.1f} nhưng video gốc chỉ dài "
                f"{video_len_ref:.1f}s — phần vượt ra sẽ BỊ CẮT vì bản xuất giữ nguyên "
                f"hình của video gốc. Muốn giữ thì nối thêm clip video ở cuối.", f"[advanced] Audio runs until second {far:.1f} but the source video is only {video_len_ref:.1f}s long — the excess will be CUT because the export keeps the source video's picture. To keep it, append more video clips at the end.")
            )

    # Duong [bg] con la MOC THOI LUONG cua ban tron (amix duration=first) — no phai dai
    # bang video, neu khong tieng se bi cat giua phim. Video khong co duong tieng (hoac
    # nguoi dung tat tieng goc) thi lay mot dai IM LANG dai bang video lam moc.
    silent_anchor = not (use_source_audio and AUD.has_audio_stream(video_path))
    anchor_len = float(meta.get("video_duration") or 0.0)
    if silent_anchor and anchor_len <= 0:
        anchor_len = TL._probe_duration_ffmpeg(video_path)

    inputs = ["-i", video_path]
    if stems:
        # 0 = video (chi lay hinh), 1 = nhac nen, 2 = giong goc, 3 = giong AI
        inputs += ["-i", stems["no_vocals"], "-i", stems["vocals"]]
    elif silent_anchor:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    inputs += ["-i", master_tts]
    tts_idx = 3 if stems else (2 if silent_anchor else 1)

    filter_lines = [
        f"[{tts_idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
        f"volume={tts_gain:.3f}[tts]"
    ]
    if stems:
        vocals_out = float(meta.get("vocals_volume", 0.0) or 0.0)
        vocals_duck = float(meta.get("voice_vocals_volume", 0.0) or 0.0)
        filter_lines.append(
            f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"volume='{_duck_expr(ambient, duck)}':eval=frame[bg]"
        )
        filter_lines.append(
            f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"volume='{_duck_expr(vocals_out, vocals_duck)}':eval=frame[voc]"
        )
        pads = ["[bg]", "[voc]", "[tts]"]
    elif silent_anchor:
        # anullsrc phat vo han -> PHAI atrim, khong thi amix duration=first khong bao
        # gio ket thuc va ffmpeg treo.
        filter_lines.append(
            f"[1:a]atrim=0:{max(0.1, anchor_len):.3f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates=44100:channel_layouts=stereo[bg]"
        )
        pads = ["[bg]", "[tts]"]
        if not use_source_audio:
            logger.info(L("[nâng cao] Tắt tiếng gốc của video — chỉ xuất những gì trên timeline.", "[advanced] Original video audio muted — exporting only what is on the timeline."))
        else:
            logger.warning(L("[nâng cao] Video nguồn không có đường tiếng — chỉ ghép giọng đọc.", "[advanced] The source video has no audio track — merging the voice only."))
    else:
        filter_lines.append(
            f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"volume='{_duck_expr(ambient, duck)}':eval=frame[bg]"
        )
        pads = ["[bg]", "[tts]"]

    filter_lines.append(
        f"{''.join(pads)}amix=inputs={len(pads)}:duration=first:dropout_transition=0:"
        "normalize=0,"
        "dynaudnorm=f=500:g=15:p=0.95:m=10,"
        "alimiter=limit=1:level_in=1.25:level_out=1[aout]"
    )
    filter_path = os.path.join(pdir, "filter_advanced.txt")
    with open(filter_path, "w", encoding="utf-8") as f:
        f.write(";\n".join(filter_lines))

    if stems:
        logger.info(L("[nâng cao] Trộn theo 2 stem đã tách (nhạc nền + giọng gốc riêng mức).", "[advanced] Mixing with the 2 separated stems (background music + original voice at separate levels)."))

    cmd = [FFMPEG_EXE, "-y"] + inputs + [
        "-filter_complex_script", filter_path,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", tmp_output,
    ]

    logger.info(L("[nâng cao] Đang ghép giọng đọc vào video…", "[advanced] Merging the voice into the video…"))
    #  TIẾN ĐỘ THẬT (10 -> 99%) cho đường GIỮ NGUYÊN HÌNH (-c:v copy). Đường dựng lại hình
    #  (_render_composed) đã báo từ lâu, còn đường này thì không: thanh kẹt ở 8% "Đã trộn
    #  xong…" tới khi xong hẳn (người dùng chụp lại 24-09-2026).
    _tong_ghep = TL._num(meta.get("video_duration"), 0.0)
    set_progress(pdir, 10.0, L("Đang ghép giọng đọc vào video…", "Merging the voice into the video…"))

    def _ghep_progress(done: float, total_sec: float) -> None:
        if total_sec > 0:
            set_progress(
                pdir, 10.0 + min(1.0, done / total_sec) * 89.0,
                L(f"Đang ghép tiếng vào hình — {_fmt_hms(done)}/{_fmt_hms(total_sec)}", f"Merging voice into the picture — {_fmt_hms(done)}/{_fmt_hms(total_sec)}"),
            )

    try:
        rc, err = _engine._run_ffmpeg_cancellable(
            cmd, project_id=project_id,
            on_progress=_ghep_progress if _tong_ghep > 0 else None,
            duration_sec=_tong_ghep,
        )
        if rc == 0 and os.path.exists(tmp_output) and os.path.getsize(tmp_output) > 0:
            safe_replace(tmp_output, final_output)
        else:
            logger.error(L(f"[nâng cao] Ghép video thất bại: {err[-2000:]}", f"[advanced] Video merge failed: {err[-2000:]}"))
            raise RuntimeError(L("Ghép video thất bại — xem log để biết chi tiết.", "Video merge failed — see the log for details."))
    finally:
        safe_remove(tmp_output)
        safe_remove(filter_path)

    # 4. Phu de + luu lai co is_trimmed (chi rieng co do — xem _persist_trim_flags)
    _luu_ket_qua(pdir, b_chunks, a_lookup, cat_doan, ranges, khoang_cat)

    # Xem chu thich o nhanh tren: day moi la BUOC 1/2.
    logger.info(L(f"[nâng cao] === BƯỚC 1/2: GHÉP TIẾNG XONG: {final_output} ===", f"[advanced] === STEP 1/2: VOICE MERGE DONE: {final_output} ==="))
    logger.info(L("[nâng cao] Tiếp bước 2/2 — đốt phụ đề & lớp phủ lên hình…", "[advanced] Continuing to step 2/2 — burning subtitles & overlays onto the picture…"))
    return final_output


def _persist_trim_flags(project_dir: str, b_chunks: list) -> None:
    """Ghi lai DUNG co ``is_trimmed`` cua tung cau, khong dung den bat ky thu gi khac.

    VI SAO KHONG ``save_b_chunks(pdir, b_chunks)``: dung phim chay hang phut, va trong
    luc do nguoi dung VAN keo tha duoc (``PUT /timeline`` ghi thang xuong dia, khong qua
    khoa nao — man hinh khong khoa timeline khi dang xuat). Ghi de bang mang dang nam
    trong bo nho tu dau lan render se xoa sach moi thao tac lam trong luc cho, khong mot
    canh bao nao. Day dung lop loi da xu ly o ``generate_voices``: doc lai tu dia roi chi
    cham vao truong cua rieng buoc nay, tren dung nhung cau con ton tai.
    """
    flags = {TL.get_uid(b): bool(b.get("is_trimmed")) for b in b_chunks}
    fresh = TL.load_b_chunks(project_dir)
    changed = False
    for b in fresh:
        want = flags.get(TL.get_uid(b))
        if want is None:                     # cau vua bi xoa / vua doi uid -> bo qua
            continue
        if bool(b.get("is_trimmed")) != want:
            b["is_trimmed"] = want
            changed = True
    if changed:
        TL.save_b_chunks(project_dir, fresh)


def _srt_time(sec: float) -> str:
    ms = int((sec % 1) * 1000)
    s = int(sec)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(project_dir: str, b_chunks: list, a_lookup: dict) -> None:
    """Ghi tep .srt cua loi binh.

    KHONG dung ``_engine._generate_srt_overlay`` cua che do co dien nua: ham do lay MEP
    KHUNG lam thoi diem tat chu, con o day chu phai tat theo GIONG (xem
    ``TL.subtitle_end``). De hai duong khac nhau thi tep .srt lech voi chinh chu da dot
    vao video — cung mot du an ma hai ban phu de khong khop.
    """
    try:
        srt_path = os.path.join(project_dir, "review_subtitles.srt")
        lines, n = [], 0
        for b in b_chunks:
            start, end = resolve_b_time(b, a_lookup)
            if start is None:
                continue
            n += 1
            stop = TL.subtitle_end(project_dir, b, float(start), float(end))
            lines.append(str(n))
            lines.append(f"{_srt_time(float(start))} --> {_srt_time(stop)}")
            lines.append(f"{(b.get('b_text') or '').strip()}\n")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.info(L(f"[nâng cao] Đã tạo tệp phụ đề (.srt) — {n} câu, tắt theo giọng.", f"[advanced] Subtitle file (.srt) created — {n} sentences, timed to the voice."))
    except Exception as e:
        logger.error(L(f"[nâng cao] Tạo phụ đề thất bại: {e}", f"[advanced] Subtitle creation failed: {e}"))
