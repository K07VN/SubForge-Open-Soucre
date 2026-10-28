"""Lop du lieu cho CHE DO REVIEW NANG CAO (trinh dung timeline kieu CapCut).

GIAI DOAN 1 — chi lo phan MO HINH DU LIEU, chua co route/UI:

  * co ``mode`` trong metadata phan biet du an Review co dien vs nang cao
  * ``uid`` — danh tinh BAT BIEN cua tung cau B, tach khoi ``b_id`` (so thu tu)
  * duong dan audio bam theo ``uid`` thay vi ``B{b_id}``
  * sap xep theo thoi gian + danh so lai ``b_id`` 1..N

VI SAO CAN ``uid``
------------------
``b_id`` dang bat buoc lien tuc 1..N va khong trung (find_b_id_gaps /
find_duplicate_b_ids trong review_service). Trong khi do audio lai nam o
``output/B{b_id}/audio_tts.wav``. Nen chi can CHEN hoac XOA mot cau o giua la
moi thu muc audio phia sau lech mot bac — giong cau 7 nhay sang cau 6.

Tach lam hai khai niem:

  * ``uid``  = DANH TINH. Sinh 1 lan, KHONG BAO GIO tai su dung. Audio + cache bam vao day.
  * ``b_id`` = SO THU TU HIEN THI. Danh lai 1..N moi lan luu, theo thoi gian bat dau.

Du an cu duoc di tru bang cach gan ``uid = "B{b_id}"`` -> tro dung thu muc audio
dang co, khong phai sinh lai giong nao.

NGUYEN TAC: module nay CHI THEM MOI. Duong Review co dien khong bi sua mot dong
nao — de sau nay go bo che do cu chi viec xoa nguyen khoi.
"""
from utils.i18n import L
import concurrent.futures as cf
import json
import logging
import os
import re
import subprocess
import uuid
import wave

import imageio_ffmpeg

from services.review_render_cache import resolve_b_time
from utils.file_utils import safe_replace

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
logger = logging.getLogger("ReviewAdvanced")

MODE_CLASSIC = "classic"
MODE_ADVANCED = "advanced"

# Tien to uid moi. Thu muc di tru cua du an cu la "B1", "B2"… nen tien to khac chu
# "B" de hai he khong bao gio dam nhau.
UID_PREFIX = "c"
_LEGACY_UID_RE = re.compile(r"^B\d+$")
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

CLIP_AUDIO_NAME = "audio_tts.wav"
DERIVED_DIRNAME = "derived"


# --------------------------------------------------------------------------- #
#  Che do du an
# --------------------------------------------------------------------------- #
#  Truong RIENG cho che do nang cao. KHONG dung lai truong "mode" san co trong
#  metadata (review_service.create_project ghi mode="review") — hien khong noi nao doc
#  no, nhung ghi de mot truong cua duong cu la rui ro thua: chi can sau nay co ai do
#  doc mode la vo. Truong rieng cung khien viec go bo che do nay sau nay sach hon.
MODE_FIELD = "review_mode"


def get_mode(meta: dict) -> str:
    """Tra ve che do cua du an. Thieu truong -> du an cu -> co dien."""
    return MODE_ADVANCED if (meta or {}).get(MODE_FIELD) == MODE_ADVANCED else MODE_CLASSIC


def is_advanced(meta: dict) -> bool:
    return get_mode(meta) == MODE_ADVANCED


# --------------------------------------------------------------------------- #
#  uid
# --------------------------------------------------------------------------- #
def new_uid() -> str:
    """Sinh uid moi: 'c' + 10 ky tu hex. An toan cho ten thu muc tren moi he dieu hanh."""
    return UID_PREFIX + uuid.uuid4().hex[:10]


def is_valid_uid(uid) -> bool:
    return isinstance(uid, str) and bool(_UID_RE.match(uid))


def legacy_uid(b_id) -> str:
    """uid di tru cho du an cu: khop dung ten thu muc audio dang ton tai."""
    return f"B{int(b_id)}"


def ensure_uids(b_chunks: list) -> int:
    """Gan uid cho moi cau B con thieu. Tra ve SO cau vua duoc gan.

    - Cau da co uid hop le -> giu nguyen (ham nay idempotent).
    - Cau chua co -> gan ``B{b_id}`` de tro dung thu muc audio cu; neu ten do da bi
      cau khac chiem (b_id trung) thi cap uid moi hoan toan.
    - Khong bao gio cap trung uid trong cung mot du an.
    """
    taken = set()
    for b in b_chunks:
        uid = b.get("uid")
        if is_valid_uid(uid):
            taken.add(uid)

    added = 0
    for b in b_chunks:
        if is_valid_uid(b.get("uid")):
            continue
        uid = None
        try:
            candidate = legacy_uid(b.get("b_id"))
            if candidate not in taken:
                uid = candidate
        except (TypeError, ValueError):
            uid = None
        while uid is None:
            candidate = new_uid()
            if candidate not in taken:
                uid = candidate
        b["uid"] = uid
        taken.add(uid)
        added += 1
    return added


def get_uid(b_data: dict) -> str:
    """uid cua 1 cau B; roi ve ``B{b_id}`` khi du lieu chua duoc di tru."""
    uid = (b_data or {}).get("uid")
    if is_valid_uid(uid):
        return uid
    return legacy_uid(b_data.get("b_id"))


# --------------------------------------------------------------------------- #
#  Duong dan audio
# --------------------------------------------------------------------------- #
def clip_dir(project_dir: str, b_data: dict) -> str:
    return os.path.join(project_dir, "output", get_uid(b_data))


def clip_audio_path(project_dir: str, b_data: dict) -> str:
    """File giong GOC cua cau B — BAT BIEN, khong bao gio bi ghi de boi nut tu dong."""
    return os.path.join(clip_dir(project_dir, b_data), CLIP_AUDIO_NAME)


def source_tag(src_path: str) -> str:
    """Dau van tay cua FILE NGUON (mtime + kich thuoc) — de dat ten tep dan xuat.

    BAT BUOC phai co trong TEN moi tep dan xuat. Ly do (loi that, nguoi dung da bao):
    truoc day tep dan xuat chi dat ten theo CON SO (``x1.350.wav``,
    ``cut3.710_1.000.wav``). Sinh lai giong bang mot giong KHAC thi file goc doi, nhung
    ten tep dan xuat van y het -> moi cho deu thay "da co roi, dung lai" va phat/xuat ra
    GIONG CU. Trieu chung rat kho lan: nhat ky bao tao giong thanh cong, timeline hien
    dung do dai moi, ma tai nghe van la giong cu.

    Dung mtime+size chu khong bam noi dung: bam MD5 ca tep WAV cho MOI clip MOI lan phat
    la tra gia I/O vo ich; hai gia tri nay doi ngay khi tep bi ghi lai.
    """
    try:
        st = os.stat(src_path)
        # NANOSECOND chu khong phai giay: sinh lai giong bang giong khac trong CUNG MOT
        # GIAY ma tep tinh co bang nhau (rat de xay ra voi cau ngan) thi dau van tay
        # theo giay khong doi -> lai dung nham ban dan xuat cu, dung lai loi cu.
        stamp = getattr(st, "st_mtime_ns", None) or int(st.st_mtime * 1e9)
        return f"{stamp}-{st.st_size}"
    except OSError:
        return "0-0"


def derived_audio_path(project_dir: str, b_data: dict, factor: float,
                       src_path: str = None) -> str:
    """File dan xuat (vd ban tang toc atempo) — tai su dung duoc.

    Nut 'Tang toc cho khop' ghi vao day chu KHONG dung den file goc, nen bam di bam
    lai / doi tran toc do deu khong phai goi TTS lai.

    Ten gom CA dau van tay file goc (xem ``source_tag``): cung he so nhung giong khac
    thi la hai tep khac nhau.
    """
    src = src_path or clip_audio_path(project_dir, b_data)
    return os.path.join(
        clip_dir(project_dir, b_data),
        DERIVED_DIRNAME,
        f"x{float(factor):.3f}_{source_tag(src)}.wav",
    )


def prune_derived(project_dir: str, b_data: dict, keep_tag: str) -> int:
    """Xoa cac tep dan xuat sinh tu mot BAN GIONG CU (khac ``keep_tag``).

    Khong co buoc nay thi moi lan sinh lai giong lai de lai mot bo tep dan xuat mo coi —
    cach dat ten cu (chi theo con so) vo tinh khong bi phinh vi no ghi de len nhau, con
    cach dat ten moi thi phai tu don.
    """
    d = os.path.join(clip_dir(project_dir, b_data), DERIVED_DIRNAME)
    if not os.path.isdir(d):
        return 0
    n = 0
    for name in os.listdir(d):
        # Tep dan xuat deu co dang "<tien to>_<tag>.wav"; giu lai dung ban cua giong
        # dang co, cai gi khong doc duoc ten thi de nguyen (khong tu tien xoa).
        #
        # GIU CA BAN QUY DOI CHO BO TRON: ``<tien to>_<tag>.mix44k_<...>.wav`` (xem
        # ``ReviewEngine._to_mix_wav``). Truoc 25-09-2026 chi so ``_<tag>.wav`` nen tep nay
        # bi xoa NGAY GIUA LUC XUAT — ham nay chay moi lan nghe thu mot cau (hop xuat co
        # "xem truoc khi cho"), bo tron doc lai khong thay tep, lui ve duong ffmpeg cham
        # ma khong co tien do: phim 30 phut dung im o 2% (nguoi dung bao, giong Edge).
        if not name.endswith(".wav") or f"_{keep_tag}.wav" in name                 or f"_{keep_tag}.mix44k_" in name:
            continue
        if not (name.startswith("x") or name.startswith("cut")):
            continue
        try:
            os.remove(os.path.join(d, name))
            n += 1
        except OSError:
            pass
    return n


# --------------------------------------------------------------------------- #
#  Sap xep + danh so lai
# --------------------------------------------------------------------------- #
def sort_and_renumber(b_chunks: list, a_lookup: dict) -> list:
    """Sap cac cau B theo THOI GIAN BAT DAU roi danh so ``b_id`` 1..N.

    Bat buoc phai chay truoc moi lan ghi b_chunks.json o che do nang cao, vi hai le:

    1. ``_generate_srt_overlay`` xuat phu de theo THU TU MANG (``enumerate``), khong
       sap theo thoi gian. Keo tha tu do ma khong sap lai se ra file SRT co dong nhay
       lui thoi gian — sai chuan.
    2. Validator ``find_b_id_gaps`` / ``find_duplicate_b_ids`` doi ``b_id`` lien tuc.

    Cau khong xac dinh duoc thoi gian bi day xuong cuoi (giu nguyen thu tu tuong doi)
    thay vi bi loai — nguoi dung con thay ma sua.
    """
    def _key(pair):
        idx, b = pair
        start, _end = resolve_b_time(b, a_lookup or {})
        if start is None:
            return (1, float("inf"), idx)
        return (0, float(start), idx)

    ordered = [b for _i, b in sorted(enumerate(b_chunks), key=_key)]
    for i, b in enumerate(ordered, start=1):
        b["b_id"] = i
    return ordered


# --------------------------------------------------------------------------- #
#  Di tru du an
# --------------------------------------------------------------------------- #
def b_chunks_path(project_dir: str) -> str:
    return os.path.join(project_dir, "b_chunks.json")


def load_b_chunks(project_dir: str) -> list:
    path = b_chunks_path(project_dir)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def save_b_chunks(project_dir: str, b_chunks: list) -> None:
    """Ghi b_chunks.json AN TOAN (file tam + thay the) — day la du lieu goc do nguoi
    dung soan, mat dien giua chung khong duoc lam hong."""
    path = b_chunks_path(project_dir)
    tmp = path + ".tl.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(b_chunks, f, indent=2, ensure_ascii=False)
    safe_replace(tmp, path)


def migrate_project_uids(project_dir: str) -> dict:
    """Gan uid cho b_chunks.json cua 1 du an (idempotent, an toan chay lai nhieu lan).

    KHONG doi ten / khong di chuyen thu muc audio: uid di tru trung dung ten thu muc
    cu (``B{b_id}``) nen moi giong dang co van duoc dung lai nguyen ven.

    Tra ve ``{"ok", "added", "total", "changed", "reason"}``.
    """
    path = b_chunks_path(project_dir)
    if not os.path.exists(path):
        return {"ok": False, "added": 0, "total": 0, "changed": False, "reason": "no_b_chunks"}

    try:
        with open(path, "r", encoding="utf-8") as f:
            b_chunks = json.load(f)
    except Exception as e:
        return {"ok": False, "added": 0, "total": 0, "changed": False, "reason": f"read_error: {e}"}

    if not isinstance(b_chunks, list):
        return {"ok": False, "added": 0, "total": 0, "changed": False, "reason": "not_a_list"}

    added = ensure_uids(b_chunks)
    if added == 0:
        return {"ok": True, "added": 0, "total": len(b_chunks), "changed": False, "reason": "already_migrated"}

    # Ghi ra file tam roi thay the (atomic) — mat dien giua chung khong lam hong
    # b_chunks.json, vi day la du lieu goc khong tai tao duoc.
    tmp = path + ".uid.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(b_chunks, f, indent=2, ensure_ascii=False)
    safe_replace(tmp, path)

    return {"ok": True, "added": added, "total": len(b_chunks), "changed": True, "reason": ""}


# --------------------------------------------------------------------------- #
#  Do thoi luong audio
# --------------------------------------------------------------------------- #
#  Nho thoi luong da do: khoa theo (duong dan, mtime, kich thuoc) nen file bi sinh
#  lai la tu dong het hieu luc. Timeline duoc doc lai sau MOI thao tac keo tha, ma
#  do bang ffmpeg ton 100-300ms/file -> khong nho thi du an 100 cau treo ca chuc giay.
_DURATION_CACHE = {}
#  CO TRAN: khoa gom ca mtime nen moi lan sinh lai giong la them mot khoa moi, khoa cu
#  khong bao gio duoc doc lai. Backend cua ban desktop chay lien nhieu ngay, mo hang
#  chuc du an -> bo nho cu dinh do lon dan mai.
#
#  TRAN 4096 LA QUA THAP — day la mot loi that, lo ra o dung co du an nguoi dung bao:
#  phim 3 tieng co 4121 cau. Vua do xong gan het thi cham tran, cache BI XOA SACH ngay
#  giua chung, roi moi lan doc timeline sau do lai do lai tu dau vai nghin tep. Nay:
#    · tran 50.000 (moi khoa vai chuc byte, tuc vai MB o muc toi da — chap nhan duoc),
#    · va cham tran thi bo NUA CU chu khong xoa sach, de phan vua do van con.
_DURATION_CACHE_MAX = 50_000
#  Ten tep nho thoi luong TREN DIA, dat trong thu muc du an.
_DURATION_FILE = ".audio_durations.json"


def _probe_duration_ffmpeg(path: str) -> float:
    """Do thoi luong bang ffmpeg (cho file KHONG phai WAV chuan)."""
    try:
        res = subprocess.run(
            [FFMPEG_EXE, "-i", path],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", close_fds=True,
        )
        m = re.search(r"Duration:\s*(\d{2}):(\d{2}):([\d.]+)", res.stderr or "")
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception as e:
        logger.warning(f"[timeline] Không đo được thời lượng {os.path.basename(path)}: {e}")
    return 0.0


def audio_duration(path: str) -> float:
    """Thoi luong 1 clip giong doc, tinh bang giay.

    LUU Y quan trong: file ten ``audio_tts.wav`` nhung RUOT LA MP3 — Edge-TTS xuat
    mp3, pipeline chi dat ten duoi .wav. Nen phai thu doc header WAV truoc (nhanh,
    ap dung cho ban dan xuat do ffmpeg tao ra la PCM that), roi moi roi ve ffmpeg.
    """
    if not path or not os.path.exists(path):
        return 0.0
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return 0.0
    if size == 0:
        return 0.0

    key = (path, mtime, size)
    hit = _DURATION_CACHE.get(key)
    if hit is not None:
        return hit

    dur = 0.0
    try:
        with wave.open(path, "rb") as w:
            fr, n = w.getframerate(), w.getnframes()
            if fr > 0 and n > 0:
                dur = n / fr
    except Exception:
        dur = 0.0          # khong phai WAV chuan (thuong la MP3) -> do bang ffmpeg
    if dur <= 0:
        dur = _probe_duration_ffmpeg(path)

    if dur > 0:
        if len(_DURATION_CACHE) >= _DURATION_CACHE_MAX:
            #  Bo NUA CU (dict Python giu thu tu chen) thay vi xoa sach — xoa sach
            #  dung giua lan do dau tien la phai do lai tu dau.
            for k in list(_DURATION_CACHE)[: _DURATION_CACHE_MAX // 2]:
                _DURATION_CACHE.pop(k, None)
        _DURATION_CACHE[key] = dur
    return dur


def _doc_nho_tren_dia(pdir: str) -> int:
    """Nap thoi luong da nho TREN DIA vao cache. Tra ve so muc nap duoc."""
    tep = os.path.join(pdir, _DURATION_FILE)
    try:
        with open(tep, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0
    n = 0
    for khoa, dur in (data or {}).items():
        try:
            duong, mtime, size = khoa.rsplit("|", 2)
            k = (duong, float(mtime), int(size))
        except (ValueError, TypeError):
            continue
        if isinstance(dur, (int, float)) and dur > 0:
            _DURATION_CACHE[k] = float(dur)
            n += 1
    return n


def _ghi_nho_tren_dia(pdir: str, paths: list) -> None:
    """Ghi thoi luong cua ``paths`` xuong dia (ghi nguyen tu)."""
    ra = {}
    co = set(paths)
    for (duong, mtime, size), dur in _DURATION_CACHE.items():
        if duong in co and dur > 0:
            ra[f"{duong}|{mtime}|{size}"] = round(float(dur), 4)
    if not ra:
        return
    tep = os.path.join(pdir, _DURATION_FILE)
    tmp = tep + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ra, f)
        os.replace(tmp, tep)
    except OSError as e:
        logger.info(f"[timeline] Khong ghi duoc bo nho thoi luong: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def probe_durations(paths: list, pdir: str = "") -> None:
    """Do truoc nhieu file SONG SONG roi nap vao cache.

    Mo timeline lan dau phai do ca nghin clip; chay tuan tu voi ffmpeg se rat cham.

    ``pdir`` = thu muc du an -> NHO KET QUA XUONG DIA.

    VI SAO CAN NHO XUONG DIA: cache chi nam trong bo nho, nen MOI LAN MO APP la phai do
    lai tu dau. Tep giong do Edge-TTS sinh ra mang ten ``.wav`` nhung ruot la MP3, tuc
    doc header WAV that bai va phai goi ffmpeg cho TUNG TEP — tren Windows moi lan dung
    tien trinh ffmpeg ton hang tram mili giay. Du an 4121 cau (phim 3 tieng, dung ca
    nguoi dung bao) vi vay treo man hinh khung xuong rat lau moi mo duoc, trong khi
    ket qua thi khong bao gio doi (khoa da gom ca mtime + kich thuoc).
    """
    todo = [p for p in paths if p]
    if not todo:
        return
    if pdir:
        _doc_nho_tren_dia(pdir)
    #  Chi do nhung tep CHUA co trong cache.
    thieu = []
    for p in todo:
        try:
            k = (p, os.path.getmtime(p), os.path.getsize(p))
        except OSError:
            continue
        if k not in _DURATION_CACHE:
            thieu.append(p)
    if thieu:
        if len(thieu) < 2:
            audio_duration(thieu[0])
        else:
            workers = min(16, max(2, (os.cpu_count() or 4)))
            with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(audio_duration, thieu))
        if pdir:
            _ghi_nho_tren_dia(pdir, todo)


# --------------------------------------------------------------------------- #
#  Hang so timeline
# --------------------------------------------------------------------------- #
#  Khoang ho toi thieu giua 2 clip lien tiep khi tu dong don (giay).
DEFAULT_GAP = 0.1
#  Tran tang toc mac dinh cho nut "Tang toc cho khop" va che do rate=auto.
DEFAULT_SPEED_CEILING = 1.4
SPEED_CEILING_MIN = 1.0
SPEED_CEILING_MAX = 1.8

STATUS_OK = "ok"            # khung du chua giong
STATUS_OVERLAP = "overlap"  # chong lan clip khac -> xuong lan duoi (mau cam)
STATUS_TRIMMED = "trimmed"  # khung NGAN HON giong qua muc tran cho phep -> cau se bi cat cut (mau do)
STATUS_TRAN = "tran"        # giong dai hon khung nhung TRAN trong muc cho phep -> phat tron, de len dau cau sau (xanh nhat)
STATUS_NO_AUDIO = "no_audio"

#  CHO GIONG TRAN SANG CAU SAU (nguoi dung chot 07-10-2026): thay vi cat giong dung mep khung (hang loat cau "do"), cho
#  phat tiep toi da ``tran_giong`` giay de len dau cau sau — chong mot chut van nghe ro. Khoa metadata (Chi tiet) / cot
#  bang videos (Co ban): ``cho_tran_giong`` (bool, MAC DINH BAT) + ``tran_giong`` (giay, 0–3,0). Tat = 0 giay = hanh vi cu.
#  Moi noi do "thieu cho" / cat giong / to mau deu phai di qua ``tran_giong_cho_phep`` — khong tu doc khoa.
TRAN_BAT_FIELD = "cho_tran_giong"
TRAN_GIAY_FIELD = "tran_giong"
TRAN_GIONG_MAC_DINH = 0.85       # người dùng chỉnh 07-10-2026: 1,5 -> 0,85 s (đủ hết dấu phẩy / âm cuối, không đè sâu vào câu sau)
TRAN_GIONG_MIN = 0.0       # người dùng 13-10-2026: cho chỉnh về 0 (0 = không cho tràn), không kẹp sàn 0,5
TRAN_GIONG_MAX = 3.0


def tran_giong_giay(v) -> float:
    """Kep so giay tran ve [MIN, MAX]; rong / hong -> mac dinh."""
    try:
        g = float(v)
    except (TypeError, ValueError):
        return TRAN_GIONG_MAC_DINH
    if g != g:                      # NaN
        return TRAN_GIONG_MAC_DINH
    return max(TRAN_GIONG_MIN, min(TRAN_GIONG_MAX, g))


def tran_giong_cho_phep(meta: dict | None) -> float:
    """So giay giong duoc TRAN qua mep khung cua cau (0.0 = khong cho tran = cat dung mep nhu cu)."""
    m = meta or {}
    if not m.get(TRAN_BAT_FIELD, True):
        return 0.0
    return tran_giong_giay(m.get(TRAN_GIAY_FIELD, TRAN_GIONG_MAC_DINH))


def _num(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


#  TRAN AM LUONG dung CHUNG cho moi duong tieng: 2.0 = 200%.
#
#  Vi sao khong dung 1.0: nhac tai ve / tieng goc thuong nho hon giong doc rat nhieu. Chi
#  co duong ha xuong thi muon nghe ro nhac phai ha het moi thu khac — ca ban phim be lai.
#  ffmpeg khuech dai thoai mai (``volume=1.8``), con khung xem truoc theo kip nho WebAudio
#  (frontend/src/components/reviewAdvanced/audioGain.ts) vi the <audio> thuan bi CHUAN WEB
#  kep cung o 1.0.
#
#  SUA SO NAY THI PHAI SUA CA ``MAX_GAIN`` ben audioGain.ts — hai ben lech nhau thi thanh
#  keo cho phep mot dang, may nhan mot neo.
MAX_VOLUME = 2.0


def clamp_volume(v, default: float = 1.0) -> float:
    """Kep mot muc am luong vao 0..MAX_VOLUME."""
    return max(0.0, min(MAX_VOLUME, _num(v, default)))


def rate_multiplier(rate: str) -> float:
    """Chuoi rate ("+20%", "-10%", "auto") -> he so toc do (1.2, 0.9, 1.0).

    Day la toc do MA GIONG DA DUOC SINH RA. Quan trong: neu du an sinh giong o +20%
    thi giong hien tai da nhanh 1.2 lan roi — hien "x1.0" tren UI la sai su that.
    """
    r = (rate or "auto").strip()
    if r in ("", "auto"):
        return 1.0
    try:
        if r.endswith("%"):
            return max(0.5, min(2.0, 1.0 + float(r[:-1]) / 100.0))
    except (TypeError, ValueError):
        pass
    return 1.0


def clip_base_rate(meta: dict, b_data: dict) -> float:
    """Toc do goc cua 1 cau: uu tien rate rieng cua cau, roi den mac dinh du an."""
    return rate_multiplier((b_data or {}).get("rate") or (meta or {}).get("default_rate"))


# --------------------------------------------------------------------------- #
#  Dau van tay cua GIONG DA SINH
# --------------------------------------------------------------------------- #
#  Ghi canh moi cau sau khi sinh giong xong: bam vao chu + giong + toc do DA DUNG.
#  Nho no ma sua chu roi bo do (khong bam sinh lai) van bi phat hien — truoc day chi
#  can file audio ton tai la coi nhu xong, nen xuat ra video doc noi dung cu ma khong
#  ai hay. Han che co y: cau chua co truong nay (du an cu) duoc coi la CON DUNG, de
#  khong bat ca du an cu sinh lai giong hang loat.
VOICE_SIG_FIELD = "voice_sig"

#  Dau cua ``end_override`` do BUOC SINH GIONG tu dan ra (giong dai hon khung phu de goc
#  nhung con khoang trong toi cau sau). Co dau nay moi phan biet duoc voi mep nguoi dung
#  keo tay: "Dat lai" chi go dung nhung mep co dau, tra ve mep suy tu phu de goc. Moi
#  cho ghi override bang tay / bang phep xep deu phai go dau (mep luc do la cua ho).
END_AUTO_FIELD = "end_auto"


def subtitle_end(project_dir: str, b_data: dict, start: float, end: float) -> float:
    """Giay mà PHU DE cua cau nay phai TAT — bam theo GIONG DOC, khong bam khung.

    VI SAO: khung thoi gian cua cau la CHO DANH SAN, khong phai do dai giong. Giong ngan
    hon khung (rat hay xay ra: khung suy tu cau goc, hoac cau vua duoc tang toc cho khop)
    thi chu con nam tren man hinh mot luc sau khi da doc xong — nhin rat vo duyen.

    Luat: tat khi giong tat, nhung KHONG BAO GIO qua mep khung — giong dai hon khung thi
    ban xuat cat no tai mep (xem ``resolve_playback_audio``), chu de dai hon la chu con
    tren man ma tieng da het.

    Cau CHUA CO GIONG thi giu nguyen khung: khong thi chu bien mat hoan toan, nguoi dung
    tuong mat kich ban.

    BAN SAO GUONG: khung xem truoc tinh y het (xem ``subtitleEnd`` trong AdvancedEditor).
    Lech nhau la nghe/nhin mot dang, xuat ra mot neo.
    """
    window = max(0.0, float(end) - float(start))
    if window <= 0:
        return float(end)
    raw = audio_duration(clip_audio_path(project_dir, b_data))
    if raw <= 0:
        return float(end)                      # chua sinh giong -> giu khung
    spoken = effective_audio_duration(b_data, raw)
    return float(start) + min(spoken, window)


def effective_audio_duration(b_data: dict, raw_duration: float) -> float:
    """Thoi luong giong SAU khi ap he so tang toc dan xuat (neu co)."""
    factor = _num(b_data.get("speed_factor"), 1.0) or 1.0
    if factor <= 0:
        factor = 1.0
    return raw_duration / factor if raw_duration else 0.0


# --------------------------------------------------------------------------- #
#  Dung timeline
# --------------------------------------------------------------------------- #
#  Duoi nguong nay thi KHONG tinh la chong lan.
#
#  VI SAO KHONG DUNG 1e-6: moc start/end ghi xuong dia deu lam tron 3 chu so, con
#  do dai giong thi khong — nen "giong het o 12,644" trong khi cau sau bat dau
#  12,643 la chuyen binh thuong cua phep lam tron. Voi nguong 1e-6, mot phan nghin
#  giay do du de day cau sau xuong lan ngoai va to do ca bang: do that tren du an
#  44 cau, 11 cau bi bao chong lan trong khi lech lon nhat chi 1ms.
#
#  20ms: nho hon moi thu tai nguoi nghe ra duoc, lon hon moi sai so lam tron.
NGUONG_CHONG = 0.02


def assign_lanes(clips: list, tran: float = 0.0) -> None:
    """Xep clip vao cac LAN sao cho trong cung mot lan khong co hai clip chong nhau.

    Lan 0 la lan chinh; clip nao dung do bi day xuong lan 1, 2… -> nhin phat la thay
    ngay cho nao bi chong. Tham lam theo thoi gian bat dau (clips da sap san), moi
    clip roi vao lan TRONG dau tien.

    ``tran`` = so giay giong duoc tran qua mep khung (``tran_giong_cho_phep``). Clip chiem
    cho tren timeline toi ``start + min(giong, khung + tran)`` — phan giong vuot qua do bi cat
    luc xuat nen khong tinh. Xuong lan duoi vi DUOI TRAN cho phep cua clip truoc thi KHONG
    phai canh bao (``overlap`` chi bat khi hai KHUNG de len nhau — keo tay chong nhau).
    """
    lane_ends = []        # thoi diem ket thuc (ke ca duoi tran) cua clip cuoi cung tren tung lan
    lane_frame_ends = []  # mep KHUNG cua clip cuoi cung tren tung lan
    for c in clips:
        start = c["start"]
        cap = c.get("window", max(0.0, c["end"] - start)) + max(0.0, float(tran or 0.0))
        end = max(c["end"], start + min(c["audio_duration"], cap))
        placed = False
        for i, lane_end in enumerate(lane_ends):
            if start >= lane_end - NGUONG_CHONG:
                c["lane"] = i
                lane_ends[i] = end
                lane_frame_ends[i] = c["end"]
                placed = True
                break
        if not placed:
            c["lane"] = len(lane_ends)
            lane_ends.append(end)
            lane_frame_ends.append(c["end"])
        if c["lane"] > 0 and start < max(lane_frame_ends[:c["lane"]]) - NGUONG_CHONG:
            c["overlap"] = True


def build_timeline(
    project_dir: str,
    meta: dict,
    a_lookup: dict,
    b_chunks: list,
    include_a: bool = True,
    sig_of=None,
    voice_of=None,
    speaker_of=None,
) -> dict:
    """Dung du lieu timeline cho man 2: moi cau B thanh 1 clip day du thong tin UI can,
    gom ca thoi luong audio THAT, co tran/cat va so lan. KHONG sua b_chunks.

    ``sig_of``: ham ``(b_data) -> str`` tra ve dau van tay giong MONG DOI cua 1 cau.
    Truyen vao thay vi tu tinh o day vi module nay la lop du lieu, khong duoc phu
    thuoc ReviewEngine (noi giu ham chon giong). Nho vay chi co MOT cho tinh dau van
    tay (review_advanced_engine) — khong the lech nhau.

    ``voice_of`` / ``speaker_of``: cung ly le — GIONG THAT se doc va NHAN VAI cua tung cau
    do ``review_advanced_engine.resolve_voice`` / ``speaker_of`` quyet dinh, truyen vao day
    de FE hien dung thu se nghe (bang phan vai + nhan tren clip) ma lop du lieu khong phai
    biet gi ve bang phan vai.
    """
    # Do thoi luong TAT CA clip song song truoc (nap cache) — sau do vong lap duoi
    # chi doc tu cache, khong con cho tung file mot.
    probe_durations([clip_audio_path(project_dir, b) for b in b_chunks], project_dir)

    clips = []
    for b in b_chunks:
        uid = get_uid(b)
        start, end = resolve_b_time(b, a_lookup or {})
        audio_path = clip_audio_path(project_dir, b)
        raw = audio_duration(audio_path)
        dur = effective_audio_duration(b, raw)

        if start is None:
            start, end = 0.0, (dur or 1.0)
            unresolved = True
        else:
            unresolved = False

        # Giong da sinh con khop chu/giong/toc do hien tai khong? Thieu dau van tay
        # (du an cu) -> coi nhu con khop, khong lam nguoi dung giat minh vo co.
        stored_sig = b.get(VOICE_SIG_FIELD)
        expect_sig = sig_of(b) if sig_of else None
        stale = bool(raw > 0 and stored_sig and expect_sig and stored_sig != expect_sig)

        window = max(0.0, float(end) - float(start))
        clips.append({
            "uid": uid,
            "b_id": b.get("b_id"),
            "voice_stale": stale,
            "text": (b.get("b_text") or b.get("text") or "").strip(),
            "voice": b.get("voice"),
            # Vai + giong THAT se doc (da qua bang phan vai) — de UI hien dung su that
            # thay vi chi hien cai nguoi dung go tay vao tung cau.
            "speaker": speaker_of(b) if speaker_of else (b.get("speaker") or ""),
            "resolved_voice": voice_of(b) if voice_of else "",
            "rate": b.get("rate"),
            "speed_factor": _num(b.get("speed_factor"), 1.0),
            # Toc do GOC (luc sinh giong) va toc do TONG dang nghe = goc x tang them.
            "base_speed": round(clip_base_rate(meta, b), 3),
            "speed_total": round(
                clip_base_rate(meta, b) * (_num(b.get("speed_factor"), 1.0) or 1.0), 3
            ),
            "source_a_ids": list(b.get("source_a_ids") or []),
            "start": float(start),
            "end": float(end),
            "window": window,
            "audio_duration": dur,
            "raw_audio_duration": raw,
            "has_audio": raw > 0,
            "unresolved_time": unresolved,
            "lane": 0,
            "overlap": False,
        })

    clips.sort(key=lambda c: (c["start"], c["b_id"] if c["b_id"] is not None else 0))
    tran = tran_giong_cho_phep(meta)
    assign_lanes(clips, tran)

    # Trang thai mau: uu tien bao "bi cat" vi do la MAT CHU — nang hon chong lan. Giong dai hon khung
    # nhung con trong muc tran cho phep thi KHONG do: phat tron, chi danh dau "tran" (xanh nhat).
    for c in clips:
        if not c["has_audio"]:
            c["status"] = STATUS_NO_AUDIO
        elif c["window"] + tran + 0.05 < c["audio_duration"]:
            c["status"] = STATUS_TRIMMED
            c["trimmed_by"] = round(c["audio_duration"] - c["window"] - tran, 3)
            if tran > 0:
                c["tran_by"] = round(tran, 3)
        elif c["window"] + 0.05 < c["audio_duration"]:
            c["status"] = STATUS_TRAN
            c["tran_by"] = round(c["audio_duration"] - c["window"], 3)
        elif c["overlap"]:
            c["status"] = STATUS_OVERLAP
        else:
            c["status"] = STATUS_OK

    # Danh sach canh A chi de VE NEN cho lan video — no KHONG doi khi keo tha clip,
    # ma lai la phan nang nhat cua goi tin (video dai co the co ca nghin cau).
    # Vi vay chi gui khi FE thuc su can (lan nap dau), va cat bot chu cho nhe.
    a_clips = []
    if include_a:
        for key, a in (a_lookup or {}).items():
            a_clips.append({
                "a_id": a.get("a_id", key),
                "start": _num(a.get("start"), 0.0),
                "end": _num(a.get("end"), 0.0),
                "text": (a.get("text") or a.get("a_text") or "")[:80],
            })
        a_clips.sort(key=lambda x: x["start"])

    return {
        "video_duration": _num(meta.get("video_duration"), 0.0),
        # FE phat video goc qua /videos/file?path=… nen can duong dan tuyet doi.
        "video_path": meta.get("video_path") or "",
        "title": meta.get("title") or "",
        #  CAU HOOK do AI nghi o buoc "Soan bai dang" cua lo chay hang loat.
        #
        #  De o day de tab "Chen chu" cua trinh dung bay ra cho nguoi dung bam mot
        #  cai la co chu dat vao dau video — dung nhung cau da an khop voi tieu de
        #  va mo ta se dang len YouTube. Khong co thi mang rong, tab do tu an phan
        #  goi y di.
        "hook_anh": list(((meta.get("youtube_meta") or {}).get("hook_anh") or [])[:8]),
        #  Tieu de AI da soan — dung lam mot goi y nua (cau dai hon hook).
        "yt_tieu_de": list(((meta.get("youtube_meta") or {}).get("tieu_de") or [])[:5]),
        "lane_count": (max((c["lane"] for c in clips), default=0) + 1) if clips else 1,
        "speed_ceiling": _num(meta.get("speed_ceiling"), DEFAULT_SPEED_CEILING),
        #  So giay giong duoc tran qua mep khung (0 = tat) — FE ve duoi tran + ghi chu, khong tu doc khoa.
        "tran_cho_phep": round(tran, 3),
        "default_rate": meta.get("default_rate") or "auto",
        "default_voice": meta.get("target_voice") or "",
        "counts": {
            "total": len(clips),
            "no_audio": sum(1 for c in clips if c["status"] == STATUS_NO_AUDIO),
            "overlap": sum(1 for c in clips if c["status"] == STATUS_OVERLAP),
            "trimmed": sum(1 for c in clips if c["status"] == STATUS_TRIMMED),
            "tran": sum(1 for c in clips if c["status"] == STATUS_TRAN),
            # Cau da co giong nhung chu/giong/toc do doi sau do -> can sinh lai.
            "stale": sum(1 for c in clips if c["voice_stale"]),
        },
        "clips": clips,
        "a_chunks": a_clips,
        # FE dua vao co nay de biet co nen giu lai a_chunks dang co hay khong.
        "a_included": include_a,
    }


# --------------------------------------------------------------------------- #
#  Thao tac tren cau B
# --------------------------------------------------------------------------- #
def find_chunk(b_chunks: list, uid: str):
    for b in b_chunks:
        if get_uid(b) == uid:
            return b
    return None


def split_text_at(text: str, ratio: float) -> tuple:
    """Chia chuoi lam doi TAI RANH GIOI TU gan vi tri ratio nhat (0..1).

    Cat giua tu se tao hai manh vo nghia khi doc len, nen luon lui/tien ve khoang
    trang gan nhat.
    """
    text = (text or "").strip()
    if not text:
        return "", ""
    ratio = min(0.95, max(0.05, float(ratio)))
    target = int(len(text) * ratio)

    left_space = text.rfind(" ", 0, target)
    right_space = text.find(" ", target)
    if left_space < 0 and right_space < 0:
        return text, ""
    if left_space < 0:
        cut = right_space
    elif right_space < 0:
        cut = left_space
    else:
        cut = left_space if (target - left_space) <= (right_space - target) else right_space
    return text[:cut].strip(), text[cut:].strip()


def make_chunk(text: str, start: float, end: float, source_a_ids=None,
               voice=None, rate=None, speaker=None) -> dict:
    """Tao mot cau B moi (chen tay tren timeline). Dung start/end_override lam moc nen
    KHONG bat buoc phai co A-chunk tuong ung."""
    return {
        "uid": new_uid(),
        "b_id": 0,  # sap va danh so lai luc luu
        "b_text": (text or "").strip(),
        "source_a_ids": list(source_a_ids or []),
        "start_override": float(start),
        "end_override": float(end),
        "voice": voice,
        "rate": rate,
        # Nhan VAI cua cau (video nhieu nhan vat) — giong doc gan theo vai o
        # ``metadata.voice_cast``. Xem ADV.resolve_voice.
        "speaker": (str(speaker).strip() if speaker else None) or None,
        "speed_factor": None,
        "is_trimmed": None,
    }


def split_chunk(b_chunks: list, uid: str, at_ratio: float = 0.5,
                at_time: float = None, a_lookup: dict = None) -> dict:
    """Cat 1 cau B thanh 2 cau doc lap.

    Hai manh chia doi khung thoi gian cu va deu can SINH LAI GIONG (xoa speed_factor
    + is_trimmed). Manh sau giu source_a_ids de van suy duoc canh goc.

    CA HAI manh nhan uid MOI. Truoc day manh dau giu nguyen uid nen no tro dung thu
    muc audio cua cau DAY DU: clip hien "da co giong", nut Sinh giong hang loat bo
    qua, va manh dau doc tron ca cau goc luc xuat. Uid moi = chua co audio = nhin la
    biet phai sinh lai.
    """
    b = find_chunk(b_chunks, uid)
    if b is None:
        raise KeyError(uid)

    start, end = resolve_b_time(b, a_lookup or {})
    if start is None:
        raise ValueError(L("Câu này chưa xác định được thời gian, không cắt được.", "This sentence has no determined timing, so it cannot be cut."))
    start, end = float(start), float(end)

    if at_time is not None and start < float(at_time) < end:
        cut_time = float(at_time)
        ratio = (cut_time - start) / max(1e-6, end - start)
    else:
        ratio = min(0.95, max(0.05, float(at_ratio)))
        cut_time = start + (end - start) * ratio

    left_text, right_text = split_text_at(b.get("b_text") or "", ratio)

    first = dict(b)
    first["uid"] = new_uid()
    first["b_text"] = left_text
    first["start_override"] = start
    first["end_override"] = cut_time
    first["speed_factor"] = None
    first["is_trimmed"] = None
    for k in ("shift_start", "shift_end", "shift_audio", VOICE_SIG_FIELD, END_AUTO_FIELD):
        first.pop(k, None)

    second = make_chunk(
        right_text, cut_time, end,
        source_a_ids=b.get("source_a_ids"),
        voice=b.get("voice"), rate=b.get("rate"),
        # NHAN VAI phai theo sang manh sau. Thieu no thi manh dau giu vai (no la ban sao
        # cua ca cau) con manh sau rot ve vai mac dinh "MC" -> ``resolve_voice`` tra giong
        # cua MC, tuc cat mot cau cua "Nu 1" la nua sau doi giong doc ma khong bao gi ca.
        speaker=b.get("speaker"),
    )

    out = []
    for item in b_chunks:
        if get_uid(item) == uid:
            out.append(first)
            out.append(second)
        else:
            out.append(item)
    return {"b_chunks": out, "uids": [get_uid(first), second["uid"]]}


# --------------------------------------------------------------------------- #
#  Tach mot cau thanh NHIEU cau theo DAU CHAM
# --------------------------------------------------------------------------- #
#  Cat SAU dau ket cau roi den khoang trang. Doi phai co khoang trang nen "3.5 triệu",
#  "1.000.000" khong bi cat lam doi — do la ca hay gap nhat trong loi binh.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
#  CJK (Nhat / Trung) KHONG dat khoang trang sau dau cham, va dung dau RIENG: 。．！？
#  Nen luat "phai co khoang trang" o tren bo sot sach — ca doan tieng Nhat dai may cau
#  van bi coi la MOT cau, nut "Tach theo cau" chet han.
#
#  Khong doi khoang trang o day duoc: cat NGAY SAU dau. Doi lai phai chua hai ca:
#    · dau DONG NGOAC (」』）… ) phai di theo cau TRUOC — 「本当か。」 la mot cau, khong
#      duoc cat giua 。 va 」;
#    · da co khoang trang thi de nhanh LATIN o tren cat, khong cat hai lan.
#  Dau "." va "?" ASCII CO Y khong nam trong nhom nay: chung con dung cho so thap phan
#  va URL trong van ban Latin, cat khong khoang trang la vo "3.5 trieu".
_CJK_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。．！？…])(?![」』）】〉》\s])")
#  Manh ngan hon the coi la vun (vd "Ừ.", chu viet tat) -> nhap vao manh truoc thay vi
#  tao ra mot cau rieng khong doc thanh tieng duoc.
MIN_SENTENCE_CHARS = 4
#  Nguong RIENG cho CJK: mot chu Han/kana mang luong thong tin bang ca mot tu Latin, nen
#  "はい。" (3 ky tu) la MOT CAU HOAN CHINH chu khong phai manh vun. Do bang nguong 4 la
#  gop oan nhung cau tra loi ngan — dung thu ca doan hoi thoai tieng Nhat dinh lien nhau.
MIN_CJK_SENTENCE_CHARS = 2
#  Co ky tu CJK nao trong manh khong (Han + hiragana + katakana).
_CJK_CHAR_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
#  Khung toi thieu cho mot manh, de manh rat ngan khong bi cho 0 giay.
MIN_PIECE_WINDOW = 0.3


def _is_scrap(piece: str) -> bool:
    """Manh nay co phai VUN khong (ngan qua, khong dang lam mot cau rieng).

    Nguong khac nhau theo loi viet: chu CJK dac thong tin hon nhieu so voi chu cai
    Latin, nen "はい。" (3 ky tu) la cau hoan chinh con "Ok." thi khong.
    """
    nguong = (
        MIN_CJK_SENTENCE_CHARS if _CJK_CHAR_RE.search(piece) else MIN_SENTENCE_CHARS
    )
    return len(piece) < nguong


def split_text_sentences(text: str) -> list:
    """Tach mot doan thanh danh sach CAU theo dau ket cau.

    Ho tro CA HAI loi viet:
      * Latin (Viet/Anh…): ``. ! ? …`` + khoang trang phia sau;
      * CJK (Nhat/Trung):  ``。．！？`` khong can khoang trang.

    Dau cau di theo cau DUNG TRUOC no ("… sống lại thật." chu khong phai "sống lại thật").
    Manh vun (ngan hon ``MIN_SENTENCE_CHARS``, thuong la chu viet tat bi cat oan) duoc
    nhap lai vao manh truoc — tha it cau dung hon la nhieu cau vo nghia.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return []
    # Tach Latin TRUOC roi tach tiep CJK trong tung manh: mot doan co the tron ca hai
    # loi viet (vd ten rieng Latin xen giua cau tieng Nhat).
    raws = []
    for chunk in _SENTENCE_SPLIT_RE.split(text):
        raws.extend(_CJK_SENTENCE_SPLIT_RE.split(chunk))
    parts = []
    for raw in raws:
        piece = raw.strip()
        if not piece:
            continue
        if parts and _is_scrap(piece):
            parts[-1] = f"{parts[-1]} {piece}"
        elif parts and _is_scrap(parts[-1]):
            parts[-1] = f"{parts[-1]} {piece}"
        else:
            parts.append(piece)
    return parts


def _sentence_pieces_for(b: dict, a_lookup: dict) -> list:
    """Chia MOT cau B thanh danh sach chunk moi theo dau cham. ``[]`` = khong tach duoc.

    Tach rieng ra khoi ``split_chunk_sentences`` de ban TUNG CAU va ban CA LOAT dung y
    het mot cong thuc. Hai cong thuc chia khung thoi gian ma lech nhau thi cung mot du an
    tach le va tach hang loat cho ra hai ket qua khac nhau — kieu sai rat kho lan ra.
    """
    pieces = split_text_sentences(b.get("b_text") or b.get("text") or "")
    if len(pieces) < 2:
        return []

    start, end = resolve_b_time(b, a_lookup or {})
    if start is None:
        return []

    out = []
    for piece, (s, e) in zip(pieces, chia_khung_theo_chu(start, end, pieces)):
        new = dict(b)
        new["uid"] = new_uid()
        new["b_text"] = piece
        new["start_override"] = s
        new["end_override"] = e
        new["speed_factor"] = None
        new["is_trimmed"] = None
        for k in ("shift_start", "shift_end", "shift_audio", VOICE_SIG_FIELD, END_AUTO_FIELD):
            new.pop(k, None)
        out.append(new)
    return out


def chia_khung_theo_chu(start: float, end: float, pieces: list) -> list:
    """Chia khung ``[start, end]`` cho các mảnh chữ theo ĐỘ DÀI chữ -> ``[(s, e), ...]``.

    Câu dài chiếm nhiều giây hơn; mảnh cuối lấy đúng phần còn lại nên cộng lại khít khung
    cũ. MỘT công thức duy nhất cho mọi chỗ tách (nút "Tách theo câu", tách tự động câu gốc
    Whisper, tách bản dịch lúc nhập — services/tach_cau.py): hai công thức lệch nhau thì
    cùng một câu tách tay và tách tự động ra hai mốc khác nhau.
    """
    start, end = float(start), float(end)
    window = max(MIN_PIECE_WINDOW * len(pieces), end - start)
    total_chars = sum(len(p) for p in pieces) or 1
    out, cursor = [], start
    for i, piece in enumerate(pieces):
        # Manh cuoi lay dung phan con lai -> tong cong khop tuyet doi voi khung cu.
        span = (
            (start + window) - cursor
            if i == len(pieces) - 1
            else max(MIN_PIECE_WINDOW, window * len(piece) / total_chars)
        )
        out.append((round(cursor, 3), round(cursor + span, 3)))
        cursor += span
    return out


def split_chunk_sentences(b_chunks: list, uid: str, a_lookup: dict = None) -> dict:
    """Tach 1 cau B thanh NHIEU cau doc lap, moi CAU (theo dau cham) mot clip.

    Vi sao can: kich ban AI tra ve tung KHOI loi binh dai 3-4 cau. Muon moi cau nam mot
    cho tren timeline thi truoc day phai bam "Cat doi" nhieu lan, moi lan lai phai dat con
    tro dung cho — trong khi cho cat mong muon luon la dau cham.

    Khung thoi gian cu duoc chia theo DO DAI CHU cua tung manh (cau dai chiem nhieu giay
    hon), cong lai vua dung khoang cu — nen mach phim khong bi dai ra hay ngan di.

    MOI manh nhan ``uid`` MOI: chu cua chung khac cau goc nen giong cu khong dung duoc
    nua; uid moi = chua co audio = nhin la biet phai sinh lai (cung ly le voi
    ``split_chunk``, dung de manh nao thua huong thu muc audio cua cau day du).
    """
    b = find_chunk(b_chunks, uid)
    if b is None:
        raise KeyError(uid)

    if len(split_text_sentences(b.get("b_text") or b.get("text") or "")) < 2:
        raise ValueError(
            L("Câu này chỉ có một câu (không tìm thấy dấu chấm nào ở giữa) — không tách được.", "This sentence is a single sentence (no period found in the middle) — it cannot be split.")
        )
    out = _sentence_pieces_for(b, a_lookup or {})
    if not out:
        raise ValueError(L("Câu này chưa xác định được thời gian, không tách được.", "This sentence has no determined timing, so it cannot be split."))

    merged = []
    for item in b_chunks:
        if get_uid(item) == uid:
            merged.extend(out)
        else:
            merged.append(item)
    return {"b_chunks": merged, "uids": [n["uid"] for n in out]}


def split_all_chunks_sentences(b_chunks: list, a_lookup: dict = None) -> dict:
    """Tach TAT CA cau B co tu 2 cau tro len — moi cau mot clip, trong MOT luot.

    Vi sao can: kich ban AI tra ve hang tram KHOI loi binh 3-4 cau. Tach tung khoi mot thi
    phai bam vao tung cau roi bam nut — vai tram lan bam cho mot viec may lam duoc het
    trong mot nhip.

    Cau khong tach duoc (chi co mot cau, hoac chua xac dinh duoc thoi gian) thi GIU
    NGUYEN, ke ca uid — nho vay giong da sinh cua chung khong bi mat. Chi nhung cau THAT
    SU bi tach moi doi uid va can sinh lai giong.
    """
    merged, uids, touched = [], [], 0
    for item in b_chunks:
        out = _sentence_pieces_for(item, a_lookup or {})
        if out:
            merged.extend(out)
            uids.extend(n["uid"] for n in out)
            touched += 1
        else:
            merged.append(item)
    return {"b_chunks": merged, "uids": uids, "split": touched, "total": len(merged)}


def delete_chunk(b_chunks: list, uid: str) -> list:
    return [b for b in b_chunks if get_uid(b) != uid]


PATCHABLE = ("b_text", "voice", "rate", "speaker",
             "start_override", "end_override", "speed_factor")


def patch_chunk(b_chunks: list, uid: str, changes: dict) -> dict:
    """Sua 1 cau B. Doi THOI GIAN thi khong dung den audio (dung tinh than cua
    compute_tts_fingerprint: override khong nam trong fingerprint)."""
    b = find_chunk(b_chunks, uid)
    if b is None:
        raise KeyError(uid)

    for k in PATCHABLE:
        if k in changes:
            b[k] = changes[k]

    # Xe dich thong minh con sot lai tu che do co dien PHAI bi xoa, neu khong no de
    # len override cua nguoi dung trong resolve_b_time (shift co uu tien cao nhat).
    if "start_override" in changes or "end_override" in changes:
        for k in ("shift_start", "shift_end", "shift_audio", END_AUTO_FIELD):
            b.pop(k, None)
    return b
