"""LAN VIDEO cua trinh dung nang cao — video goc cung chi la MOT clip tren timeline.

Truoc day lan video la "nen tham chieu bat dong" (quyet dinh so 5 trong ke hoach ban
dau). Nguoi dung da THU HOI quyet dinh do: muon lam han mot trinh dung kieu CapCut.
Nen bay gio:

    video_clips: [
      {
        "id": "vid-xxxx",
        "path": "D:/phim/canh-1.mp4",
        "name": "canh-1.mp4",
        "start": 0.0,        # dat o giay thu may tren TIMELINE
        "in": 0.0,           # cat bo bao nhieu giay o DAU tep nguon
        "out": 125.0,        # lay den giay thu may cua tep nguon
        "speed": 1.0,        # atempo cho tieng + setpts cho hinh
        "volume": 1.0,       # tieng CUA CHINH clip nay
        "muted": false,
        "enabled": true,
        "source_duration": 300.0, "width": 1920, "height": 1080, "fps": 30.0
      }
    ]

QUY UOC (giong lan audio, xem review_audio_clips):
  * ``in``/``out`` do bang giay CUA TEP NGUON; do dai chiem tren timeline la
    ``(out - in) / speed``.
  * Cho nao khong co clip nao thi ra MAN DEN.

CHONG LAN — cung luat voi lan giong doc:
  Moi thoi diem chi MOT clip duoc phat. Clip nao chong lan clip dung truoc no thi bi
  day xuong LAN NGOAI (lane >= 1), to mau canh bao, va KHONG duoc phat cung KHONG vao
  ban xuat. Lan duoc tinh tham lam theo thoi gian bat dau: clip bat dau som hon giu
  cho, nen "cai goc" chinh la clip dau tien. Nguoi dung keo cho khac la no tu quay ve
  lan chinh — khong can hop thoai, nhin mau la hieu.

Du lieu nam trong ``metadata.json`` (khoa ``video_clips``). Du an cu chua co khoa nay
thi ``ensure_migrated`` tu tao MOT clip phu kin video goc — mo len khong khac gi truoc.
"""
from utils.i18n import L
import os
import re
import subprocess
import uuid

import imageio_ffmpeg

#  Tran am luong dung CHUNG voi lan nhac / 4 muc tron / giong doc — mot cho khai bao.
from services.review_timeline import clamp_volume

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

FIELD = "video_clips"
#  ẢNH cũng là một clip trên làn video: giữ trên màn hình mấy giây rồi chuyển tiếp. Dùng
#  cho ảnh bìa, ảnh minh hoạ, ảnh chèn giữa phim.
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
#  Thời lượng mặc định khi vừa thêm một ảnh (giây). Kéo mép để giữ lâu hơn/ngắn hơn —
#  ảnh KHÔNG có trần độ dài (``source_duration = 0``) nên giữ bao lâu cũng được.
DEFAULT_IMAGE_SECONDS = 4.0


def is_image(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in IMAGE_EXTS


def clip_is_image(clip: dict) -> bool:
    """Clip nay la ANH khong. Doc co da luu; du an cu thi suy tu duoi tep."""
    c = clip or {}
    if "is_image" in c:
        return bool(c["is_image"])
    return is_image(c.get("path") or "")


#  Da dung lan video cho du an nay chua. Phan biet "du an cu, chua co lan video" voi
#  "nguoi dung da XOA het clip" — hai truong hop nay trong metadata giong nhau y het
#  (khong co khoa / khoa rong) nhung y muon nguoc nhau.
MIGRATED_FLAG = "video_lane_ready"
SPEED_MIN, SPEED_MAX = 0.5, 2.0
DEFAULT_FPS = 30.0


#  4 MUC AM LUONG RIENG cua tung clip. Thieu khoa nao -> lay muc chung cua du an, nen
#  clip chua ai cham vao van nghe y nhu truoc.
MIX_FIELD = "mix"
MIX_KEYS = ("bg", "bg_duck", "vocals", "vocals_duck")
#  Thu muc chua stem da tach cua CAC CLIP (khac ``separated/`` la stem cua video goc).
CLIP_STEMS_DIRNAME = "separated_clips"
STEM_KINDS = ("no_vocals", "vocals")


def new_id() -> str:
    return "vid-" + uuid.uuid4().hex[:10]


# --------------------------------------------------------------------------- #
#  TACH GIONG & NHAC NEN cho TUNG clip video
# --------------------------------------------------------------------------- #
def stems_key(path: str) -> str:
    """Khoa thu muc stem — bam theo TEP NGUON, khong theo id clip.

    Vi sao theo tep: cat mot clip lam hai (hoac nhan ban) ra hai id khac nhau nhung VAN
    LA MOT TEP. Bam theo tep thi tach mot lan dung cho tat ca cac manh — Demucs chay hang
    phut, tach lai cho tung manh la khong the chap nhan. Stem la cua CA TEP nen doan
    ``in``/``out`` cua tung manh cu cat tren stem la khop.
    """
    import hashlib

    try:
        norm = os.path.realpath(path or "")
    except OSError:
        norm = path or ""
    return hashlib.md5(os.path.normcase(norm).encode("utf-8", "ignore")).hexdigest()[:12]


def clip_stems_dir(project_dir: str, clip: dict) -> str:
    return os.path.join(
        project_dir, CLIP_STEMS_DIRNAME, stems_key((clip or {}).get("path") or "")
    )


#  Nhan "DANG TACH DO DANG". Demucs ghi hai tep WAV LON DAN (header truoc, du lieu sau),
#  nen "co tep va size > 0" la dung ngay giay dau tien — do la luc tep con do dang. Hau
#  qua: bang thuoc tinh clip bao "da tach" khi chua xong, khung xem truoc chuyen sang
#  nghe 2 stem bi cat cut, va bam Xuat ngay luc do thi ffmpeg tron vao mot tep WAV thieu
#  duoi (cuoi clip mat tieng) ma khong bao loi gi.
#
#  Co nhan nay thi coi nhu CHUA CO. Ban tach cu (truoc thay doi nay) khong co nhan nen
#  van tinh la xong — khong ai phai tach lai.
SEPARATING_FLAG = ".separating"


def mark_separating(stems_dir: str) -> None:
    """Danh dau thu muc stem la DANG GHI. Goi TRUOC khi chay Demucs."""
    os.makedirs(stems_dir, exist_ok=True)
    with open(os.path.join(stems_dir, SEPARATING_FLAG), "w", encoding="utf-8") as f:
        f.write("dang tach")


def clear_separating(stems_dir: str) -> None:
    """Bo danh dau — goi khi tach XONG HAN."""
    try:
        os.remove(os.path.join(stems_dir, SEPARATING_FLAG))
    except OSError:
        pass


def is_separating(stems_dir: str) -> bool:
    return os.path.exists(os.path.join(stems_dir, SEPARATING_FLAG))


def discard_stems(stems_dir: str) -> None:
    """Xoa ban tach DO DANG (loi hoac nguoi dung huy) roi bo danh dau.

    Phai xoa han: de lai tep do dang ma bo nhan thi lan sau moi cho deu tin la ban tach
    hoan chinh.
    """
    for k in STEM_KINDS:
        try:
            os.remove(os.path.join(stems_dir, f"{k}.wav"))
        except OSError:
            pass
    clear_separating(stems_dir)


def clip_stems(project_dir: str, clip: dict, video_path: str = "") -> dict:
    """Duong dan 2 stem CUA CLIP nay neu da tach xong, nguoc lai ``{}``.

    Clip la manh cua VIDEO GOC thi uu tien ``separated/`` — do la ban tach cua chinh tep
    do o cap du an (nut "Tach ngay" o bang Chuan bi). Khong lam vay thi cung mot tep bi
    tach hai lan, ton hang phut va gap doi dung luong dia.

    Thu muc dang mang nhan ``.separating`` bi BO QUA: tep o do con do dang.
    """
    dirs = []
    if video_path and is_source_clip(clip, video_path):
        dirs.append(os.path.join(project_dir, "separated"))
    dirs.append(clip_stems_dir(project_dir, clip))
    for d in dirs:
        if is_separating(d):
            continue
        paths = {k: os.path.join(d, f"{k}.wav") for k in STEM_KINDS}
        if all(os.path.exists(v) and os.path.getsize(v) > 0 for v in paths.values()):
            return paths
    return {}


#  Tran do dai cho mot doan luu thanh PRESET. Preset la de dung lai doan NGAN (mo dau,
#  ket thuc, clip chen hai); luu ca bo phim vao kho AppData la lam day o dia nguoi dung.
PRESET_MAX_SECONDS = 10 * 60


def export_segment(clip: dict, dest: str) -> bool:
    """Cat DUNG DOAN dang thay tren timeline ra mot tep rieng, KHONG ma hoa lai.

    Dung cho "luu doan nay thanh preset": tep trong kho preset chi bang doan da cat (mot
    doan mo dau 10 giay ra ~1MB thay vi keo ca tep goc 2GB vao AppData), va mo du an khac
    ra chen vao la duoc ngay.

    ``-c copy`` nen rat nhanh nhung diem cat bam theo KHUNG KHOA cua tep goc — dau doan co
    the lech vai chuc mili giay. Voi doan mo dau/ket thuc thi khong ai nhan ra, con doi lai
    la khong phai ma hoa lai (giu nguyen chat luong, nhanh gap hang chuc lan).
    """
    src = (clip or {}).get("path") or ""
    if not src or not os.path.exists(src):
        return False
    t_in = max(0.0, _num(clip.get("in")))
    seg = max(0.05, _num(clip.get("out")) - t_in)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp" + os.path.splitext(dest)[1]
    cmd = [
        FFMPEG_EXE, "-y", "-ss", f"{t_in:.3f}", "-i", src, "-t", f"{seg:.3f}",
        "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", tmp,
    ]
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, close_fds=True)
    ok = res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
    if ok:
        os.replace(tmp, dest)
        return True
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return False


def clip_mix(meta: dict, clip: dict) -> dict:
    """4 MUC AM LUONG DA CHOT cua mot clip: muc rieng cua clip, thieu thi lay cua du an.

    Y nghia 4 muc (giong cap du an): ``bg``/``bg_duck`` la nhac nen (hoac tieng goc chung
    khi chua tach) luc KHONG / CO giong AI dang noi; ``vocals``/``vocals_duck`` la giong
    goc, chi dung khi clip da tach stem.
    """
    meta = meta or {}
    base = {
        "bg": _num(meta.get("bg_volume"), 0.0),
        "bg_duck": _num(meta.get("voice_bg_volume"), 0.0),
        "vocals": _num(meta.get("vocals_volume"), 0.0),
        "vocals_duck": _num(meta.get("voice_vocals_volume"), 0.0),
    }
    own = (clip or {}).get(MIX_FIELD)
    if isinstance(own, dict):
        for k in MIX_KEYS:
            if own.get(k) is not None:
                base[k] = max(0.0, min(1.0, _num(own.get(k), base[k])))
    return base


def has_own_mix(clip: dict) -> bool:
    own = (clip or {}).get(MIX_FIELD)
    return isinstance(own, dict) and any(own.get(k) is not None for k in MIX_KEYS)


def list_clips(meta: dict) -> list:
    clips = (meta or {}).get(FIELD)
    return clips if isinstance(clips, list) else []


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
#  Do thong so tep video
# --------------------------------------------------------------------------- #
_DIM_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s+fps")
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")

_PROBE_CACHE = {}
#  Co tran, cung ly do voi ``_DURATION_CACHE`` cua review_timeline: khoa gom mtime nen
#  khong bao gio het han tu nhien. Day tran thi xoa sach roi do lai.
_PROBE_CACHE_MAX = 512


def probe_video(path: str) -> dict:
    """Do thoi luong / kich thuoc / fps cua 1 tep video.

    Co cache theo (duong dan, mtime, kich thuoc): moi lan ve timeline hoac dung phim
    deu can so nay, ma goi ffmpeg thi ton 100-300ms mot lan.
    """
    empty = {"duration": 0.0, "width": 0, "height": 0, "fps": DEFAULT_FPS,
             "has_audio": False}
    if not path or not os.path.exists(path):
        return dict(empty)
    try:
        key = (path, os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return dict(empty)
    hit = _PROBE_CACHE.get(key)
    if hit is not None:
        return dict(hit)

    res = subprocess.run(
        [FFMPEG_EXE, "-i", path],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="ignore", close_fds=True,
    )
    err = res.stderr or ""
    out = dict(empty)
    m = _DUR_RE.search(err)
    if m:
        h, mi, s = m.groups()
        out["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    m = _DIM_RE.search(err)
    if m:
        out["width"], out["height"] = int(m.group(1)), int(m.group(2))
    # Lay fps cua DONG VIDEO, khong phai con so nao khac trong log.
    vline = next((l for l in err.splitlines() if "Video:" in l), "")
    m = _FPS_RE.search(vline)
    if m:
        f = float(m.group(1))
        if 1.0 <= f <= 240.0:
            out["fps"] = f
    out["has_audio"] = "Audio:" in err

    if out["duration"] > 0:
        if len(_PROBE_CACHE) >= _PROBE_CACHE_MAX:
            _PROBE_CACHE.clear()
        _PROBE_CACHE[key] = dict(out)
    return out


# --------------------------------------------------------------------------- #
#  Tao / sua clip
# --------------------------------------------------------------------------- #
def make_clip(path: str, name: str, start: float, info: dict = None,
              seconds: float = None) -> dict:
    """Mot clip moi tren lan video — tep video HOAC ANH.

    ANH khac video hai diem: (1) thoi luong do NGUOI DUNG quyet dinh (mac dinh
    ``DEFAULT_IMAGE_SECONDS``, keo mep bao lau cung duoc), nen ``source_duration = 0`` de
    ham kep khong chan; (2) khong co tieng.
    """
    info = info or probe_video(path)
    img = is_image(path)
    if img:
        dur = max(0.2, _num(seconds, DEFAULT_IMAGE_SECONDS) or DEFAULT_IMAGE_SECONDS)
    else:
        dur = max(0.1, _num(info.get("duration"), 0.0) or 0.1)
    return {
        "id": new_id(),
        "path": path.replace("\\", "/"),
        "name": name or os.path.basename(path),
        "start": round(max(0.0, _num(start)), 3),
        "in": 0.0,
        "out": round(dur, 3),
        "speed": 1.0,
        "volume": 1.0,
        "muted": False,
        "enabled": True,
        # Anh: 0 = KHONG co tran do dai, giu tren man bao lau cung duoc.
        "source_duration": 0.0 if img else round(dur, 3),
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "fps": _num(info.get("fps"), DEFAULT_FPS) or DEFAULT_FPS,
        "has_audio": False if img else bool(info.get("has_audio")),
        "is_image": img,
    }


def clip_speed(clip: dict) -> float:
    s = _num((clip or {}).get("speed"), 1.0)
    if s <= 0:
        s = 1.0
    return max(SPEED_MIN, min(SPEED_MAX, s))


def visible_duration(clip: dict) -> float:
    """Do dai clip CHIEM TREN TIMELINE = phan da cat chia toc do."""
    seg = max(0.0, _num((clip or {}).get("out")) - _num((clip or {}).get("in")))
    return seg / clip_speed(clip) if seg else 0.0


def clip_end(clip: dict) -> float:
    return _num(clip.get("start")) + visible_duration(clip)


def cat_clip_hinh(meta: dict, khoang) -> list:
    """Danh sach clip hinh sau khi CHI GIU cac doan da chon, quy ve dong thoi gian moi.

    Dung cho "Xuat doan da chon". Khong dong gi den tep tren dia: mot clip bi khoang chon
    cat lam doi thi thanh HAI ban ghi, moi ban tro vao mot quang khac cua cung tep nguon.
    Nho vay ca hinh, tieng cua clip lan 2 stem da tach deu theo dung — chung deu duoc
    dung boi cung bo ``in``/``out``/``speed``/``start`` (xem chuoi loc atrim -> atempo ->
    volume -> adelay). Khong phai cat truoc tep nao het.

    Quy doi: clip chiem tren timeline quang ``[start, start + (out-in)/speed]``; mot moc
    ``t`` trong quang do ung voi giay ``in + (t - start) * speed`` CUA TEP NGUON.

    ``khoang`` la danh sach ``(bat_dau, ket_thuc)`` da gop, theo thu tu tang.
    """
    from utils import timeline_slice as TS

    ra = []
    for c in playable_clips(meta):
        toc = clip_speed(c)
        d0 = _num(c.get("start"))
        d1 = d0 + visible_duration(c)
        if d1 <= d0:
            continue
        for s, e in khoang:
            a, b = max(s, d0), min(e, d1)
            #  Manh ngan hon mot khung hinh thi bo: giu lai chi de ra mot clip 0 giay,
            #  ffmpeg ma hoa no thanh ra loi "khong co khung nao".
            if b - a < 0.04:
                continue
            moc_moi = TS.doi_moc(a, khoang)
            if moc_moi is None:
                continue
            ra.append({
                **c,
                #  Id phai DUY NHAT: no la khoa cua bang stem theo clip (``stem_idx``),
                #  hai manh trung id thi manh sau de mat stem cua manh truoc.
                "id": f"{c.get('id')}#{len(ra)}",
                "start": round(moc_moi, 3),
                "in": round(_num(c.get("in")) + (a - d0) * toc, 3),
                "out": round(_num(c.get("in")) + (b - d0) * toc, 3),
            })
    ra.sort(key=lambda x: _num(x.get("start")))
    return ra


PATCHABLE = ("start", "in", "out", "speed", "volume", "muted", "enabled", "name",
             "use_stems")


def patch_clip(meta: dict, clip_id: str, changes: dict) -> dict:
    """Sua 1 clip video, kep moi gia tri vao khoang hop le."""
    clips = list_clips(meta)
    target = next((c for c in clips if c.get("id") == clip_id), None)
    if target is None:
        raise KeyError(clip_id)

    for k in PATCHABLE:
        if k in changes and changes[k] is not None:
            target[k] = changes[k]

    # 4 MUC AM LUONG RIENG: nhan sua tung phan (``{"mix": {"bg": 0.2}}``). Gia tri None =
    # bo muc rieng do, quay ve muc chung cua du an.
    if isinstance(changes.get(MIX_FIELD), dict):
        own = dict(target.get(MIX_FIELD) or {})
        for k in MIX_KEYS:
            if k in changes[MIX_FIELD]:
                v = changes[MIX_FIELD][k]
                if v is None:
                    own.pop(k, None)
                else:
                    own[k] = round(max(0.0, min(1.0, _num(v, 0.0))), 3)
        if own:
            target[MIX_FIELD] = own
        else:
            target.pop(MIX_FIELD, None)

    src_dur = _num(target.get("source_duration"), 0.0)
    tin = max(0.0, _num(target.get("in"), 0.0))
    tout = _num(target.get("out"), src_dur or 1.0)
    if src_dur > 0:
        tin = min(tin, max(0.0, src_dur - 0.1))
        tout = min(tout, src_dur)
    if tout <= tin + 0.05:
        tout = tin + 0.05
    target["in"], target["out"] = round(tin, 3), round(tout, 3)
    target["start"] = round(max(0.0, _num(target.get("start"), 0.0)), 3)
    # Anh la hinh tinh: toc do vo nghia, ma de khac 1.0 thi ``-t`` cua dau vao lech voi
    # do dai chain -> khoa cung o 1.0.
    target["speed"] = 1.0 if clip_is_image(target) else round(clip_speed(target), 3)
    target["volume"] = clamp_volume(target.get("volume"), 1.0)
    target["muted"] = bool(target.get("muted", False))
    target["enabled"] = bool(target.get("enabled", True))
    meta[FIELD] = clips
    return target


def delete_clip(meta: dict, clip_id: str) -> bool:
    clips = list_clips(meta)
    remain = [c for c in clips if c.get("id") != clip_id]
    meta[FIELD] = remain
    return len(remain) != len(clips)


def find_clip(meta: dict, clip_id: str):
    return next((c for c in list_clips(meta) if c.get("id") == clip_id), None)


def split_clip(meta: dict, clip_id: str, at_time: float) -> list:
    """Cat 1 clip video tai giay ``at_time`` TREN TIMELINE thanh hai clip doc lap."""
    clips = list_clips(meta)
    target = next((c for c in clips if c.get("id") == clip_id), None)
    if target is None:
        raise KeyError(clip_id)

    start = _num(target.get("start"))
    vis = visible_duration(target)
    offset = float(at_time) - start
    if offset <= 0.05 or offset >= vis - 0.05:
        raise ValueError(L("Chỗ cắt phải nằm trong clip (cách hai mép ít nhất 0.05 giây).", "The cut point must lie inside the clip (at least 0.05 seconds from both edges)."))

    speed = clip_speed(target)
    cut_src = _num(target.get("in")) + offset * speed     # doi ve giay cua tep nguon

    second = dict(target)
    second["id"] = new_id()
    # ``dict()`` la ban sao NONG: khong tach ra thi hai clip dung CHUNG mot dict 4 muc,
    # sua muc cua manh nay la manh kia doi theo.
    if isinstance(second.get(MIX_FIELD), dict):
        second[MIX_FIELD] = dict(second[MIX_FIELD])
    second["in"] = round(cut_src, 3)
    second["start"] = round(start + offset, 3)
    target["out"] = round(cut_src, 3)

    out = []
    for c in clips:
        out.append(c)
        if c.get("id") == clip_id:
            out.append(second)
    meta[FIELD] = out
    return [target["id"], second["id"]]


# --------------------------------------------------------------------------- #
#  Chong lan -> day xuong lan ngoai
# --------------------------------------------------------------------------- #
def assign_lanes(meta: dict) -> list:
    """Xep clip video vao cac LAN. Tra ve danh sach ``(clip, lane)`` da sap theo start.

    Cung luat voi lan giong doc (``TL.assign_lanes``): tham lam theo thoi gian bat dau,
    clip nao dung cho la bi day xuong lan duoi. Khac mot diem quan trong: o lan video
    CHI LAN 0 duoc phat va duoc dua vao ban xuat — vi moi thoi diem chi chieu duoc mot
    hinh. Clip o lan >= 1 chi hien de nguoi dung thay ma keo di cho khac.

    "Cai goc" khong phai mot co dac biet nao ca: clip bat dau SOM NHAT giu cho truoc,
    nen no tu nhien la cai goc.
    """
    items = sorted(
        (c for c in list_clips(meta) if c.get("enabled", True)),
        key=lambda c: (_num(c.get("start")), c.get("id") or ""),
    )
    lane_ends: list = []
    out = []
    for c in items:
        start, end = _num(c.get("start")), clip_end(c)
        lane = None
        for i, e in enumerate(lane_ends):
            if start >= e - 1e-6:
                lane, lane_ends[i] = i, end
                break
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(end)
        out.append((c, lane))
    return out


def playable_clips(meta: dict) -> list:
    """Nhung clip THUC SU duoc chieu: lan 0, da sap theo thoi gian."""
    return [c for c, lane in assign_lanes(meta) if lane == 0]


def overlap_count(meta: dict) -> int:
    return sum(1 for _c, lane in assign_lanes(meta) if lane > 0)


# --------------------------------------------------------------------------- #
#  Di tru: video goc thanh clip dau tien
# --------------------------------------------------------------------------- #
def ensure_migrated(meta: dict, video_path: str) -> bool:
    """Du an chua co lan video thi tao MOT clip phu kin video goc.

    Tra ve True neu co SUA metadata (nguoi goi phai luu). Mo du an cu sau khi di tru thi
    nhin y nhu truoc: mot dai video duy nhat, bat dau tu giay 0.

    CHAY DUNG MOT LAN cho moi du an, danh dau bang ``MIGRATED_FLAG``. Truoc day chi xet
    "lan video co rong khong", nen xoa clip cuoi cung xong thi lan nap sau ham nay lai
    dung video goc len — nguoi dung xoa, thay no mat, roi keo mot thao tac bat ky la no
    QUAY VE. Da di tru roi thi lan video rong la Y NGUOI DUNG: de rong, khung xem truoc
    ra man den ("chỗ này chưa có clip nào") va Ctrl+Z lay lai duoc.
    """
    if meta.get(MIGRATED_FLAG):
        return False
    if isinstance(meta.get(FIELD), list) and meta[FIELD]:
        meta[MIGRATED_FLAG] = True      # ghi cot moc, lan sau khong hoi lai nua
        return True
    if not video_path or not os.path.exists(video_path):
        return False
    info = probe_video(video_path)
    if info["duration"] <= 0:
        # Khong doc duoc thoi luong thi lay so da luu trong metadata (co the do luc tao
        # du an) — con hon khong tao clip nao ca.
        info["duration"] = _num(meta.get("video_duration"), 0.0)
        if info["duration"] <= 0:
            return False
    clip = make_clip(video_path, os.path.basename(video_path), 0.0, info=info)
    meta[FIELD] = [clip]
    meta[MIGRATED_FLAG] = True
    return True


def append_clip(meta: dict, clip: dict, position: str = "end") -> dict:
    """Them clip vao DAU hoac CUOI mach phim — khong chen vao giua.

    ``position="start"`` day moi clip dang co lui ve sau dung do dai clip moi, nen
    khong tao ra chong lan; ``"end"`` dat ngay sau clip cuoi cung.
    """
    clips = list_clips(meta)
    if position == "start":
        shift = visible_duration(clip)
        for c in clips:
            c["start"] = round(_num(c.get("start")) + shift, 3)
        clip["start"] = 0.0
        meta[FIELD] = [clip] + clips
    else:
        clip["start"] = round(
            max((clip_end(c) for c in clips if c.get("enabled", True)), default=0.0), 3
        )
        meta[FIELD] = clips + [clip]
    return clip


def insert_clip_at(meta: dict, clip: dict, at: float) -> dict:
    """CHEN clip vao DUNG giay ``at`` roi DAY phan phia sau lui lai.

    Truoc day chi cho them vao DAU hoac CUOI (``append_clip``) — quyet dinh co y de khong
    sinh ra chong lan. Nguoi dung yeu cau chen tai con tro, nen o day lam kieu "ripple":

      1. Clip dang phu diem chen bi CAT tai do (con tro nam han trong no).
      2. Moi clip bat dau TU ``at`` tro di duoc day lui ``+ do dai clip moi``.
      3. Clip moi nam dung tai ``at``.

    Vi sao khong chi dat ``start = at`` roi thoi: diem chen gan nhu luon nam tren mach phim
    dang co, nen clip moi se chong lan -> roi xuong hang ngoai, KHONG phat va KHONG vao ban
    xuat. Nguoi dung bam "them tai thuoc" ma khong thay gi thay doi.

    NGUOI GOI phai day luon GIONG DOC va NHAC (``ADV.shift_after``) — chi day lan hinh thi
    loi doc lech di dung do dai doan vua chen.
    """
    at = max(0.0, float(at))
    length = visible_duration(clip)

    # 1. Cat clip dang phu diem chen. Doi con tro cach hai mep >= 0.05s (giong split_clip):
    #    sat mep thi khong can cat, chi day la du.
    covering = next(
        (
            c for c in playable_clips(meta)
            if _num(c.get("start")) + 0.05 < at < clip_end(c) - 0.05
        ),
        None,
    )
    did_split = False
    if covering is not None:
        try:
            split_clip(meta, covering["id"], at)
            did_split = True
        except (KeyError, ValueError):
            did_split = False

    # 2. Day moi clip bat dau tu diem chen tro di (ke ca nua sau vua cat ra).
    moved = 0
    for c in list_clips(meta):
        if _num(c.get("start")) >= at - 1e-6:
            c["start"] = round(_num(c.get("start")) + length, 3)
            moved += 1

    clip["start"] = round(at, 3)
    meta[FIELD] = list_clips(meta) + [clip]
    return {"split": did_split, "shifted": moved, "length": round(length, 3)}


def is_source_clip(clip: dict, video_path: str) -> bool:
    """Clip nay co phai la MOT MANH CUA VIDEO GOC khong (khac voi clip nguoi dung them).

    Quan trong vi TIENG cua hai loai duoc xu ly khac nhau khi xuat:

      * manh cua video goc  -> do la "tieng goc" cua du an long tieng: chay qua bo tron
        2 muc (``bg_volume`` / ``voice_bg_volume``) va bi ha xuong khi giong AI noi.
      * clip nguoi dung THEM VAO -> la nguyen lieu rieng ho co tinh dat vao, nghe dung
        nut am luong cua clip do, KHONG bi ha — cung luat voi moi doan tren lan nhac.

    Neu tron lam mot thi hoac tieng Trung cua ban goc quay ve du 100% (du an long tieng
    de "tieng goc = 0%"), hoac nut am luong cua clip vua them thanh vo nghia vi bi nhan
    voi 0. Cat video goc thanh nhieu manh thi manh nao cung van la video goc.
    """
    a, b = clip.get("path") or "", video_path or ""
    if not a or not b:
        return False
    try:
        if os.path.realpath(a) == os.path.realpath(b):
            return True
    except OSError:
        pass
    # Roi ve so chuoi da chuan hoa: tep co the khong con tren dia (o chua cam), luc do
    # realpath khong giai duoc nhung hai duong dan van co the la mot.
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def is_untouched_single(meta: dict, video_path: str) -> bool:
    """Lan video co dung la MOT clip nguyen ven cua video goc khong.

    Dung de giu duong dung phim NHANH: truong hop nay khong can dung lai hinh, cu
    ``-c:v copy`` nhu truoc (nhanh hon nhieu lan va khong mat chat luong). Chi khi
    nguoi dung thuc su cat/keo/them clip moi phai dung lai.
    """
    clips = [c for c in list_clips(meta) if c.get("enabled", True)]
    if len(clips) != 1:
        return False
    c = clips[0]
    if abs(_num(c.get("start"))) > 0.01 or abs(_num(c.get("in"))) > 0.01:
        return False
    if abs(clip_speed(c) - 1.0) > 0.001:
        return False
    if os.path.realpath(c.get("path") or "") != os.path.realpath(video_path or ""):
        return False
    src = _num(c.get("source_duration"), 0.0)
    if src > 0 and _num(c.get("out")) < src - 0.05:
        return False
    # Am luong / tat tieng con mac dinh thi moi coi la "nguyen ven".
    return not c.get("muted", False) and abs(_num(c.get("volume"), 1.0) - 1.0) < 0.01


def timeline_end(meta: dict) -> float:
    """Giay cuoi cung co clip video — timeline phai dai it nhat den day."""
    return max((clip_end(c) for c in list_clips(meta)), default=0.0)


def film_end(meta: dict, video_duration: float) -> float:
    """Mep phai cua PHIM tren timeline: mep clip video xa nhat; lan video trong moi roi
    ve do dai tep goc.

    KHONG lay max voi ``video_duration``: do la do dai TEP GOC, khong phai tren timeline.
    Tang toc x2 thi phim 31:40 chi con 15:50, ban xuat cung 15:50 (_render_composed tinh
    theo mep clip) — lay max la trinh phat ghi "/ 31:40", nua sau trong tron (nguoi dung
    hoi 25-09-2026). Cung thu tu voi ``review_advanced_engine._timeline_limit``."""
    lane_end = timeline_end(meta)
    return lane_end if lane_end > 0.05 else float(video_duration or 0.0)


def canvas_size(meta: dict) -> tuple:
    """Kich thuoc khung dung phim: lay theo clip VIDEO duoc phat dau tien.

    UU TIEN VIDEO, chi lay ANH khi ca mach phim khong co video nao. Vi sao: nguoi dung
    hay chen mot anh bia 800x450 lam mo dau cho phim 1920x1080 — neu anh do (dung dau
    mach) duoc quyet dinh khung thi CA BAN XUAT co lai con 800x450, phim bi thu nho ma
    khong ai hieu tai sao.

    Clip bi chong lan khong duoc tinh — no khong vao ban xuat thi cung khong co quyen
    quyet dinh khung hinh.
    """
    playable = playable_clips(meta)
    for only_video in (True, False):
        for c in playable:
            if only_video and clip_is_image(c):
                continue
            w, h = int(_num(c.get("width"), 0)), int(_num(c.get("height"), 0))
            if w > 0 and h > 0:
                return (w - w % 2, h - h % 2)     # x264 doi canh chan
    return (1920, 1080)


def canvas_fps(meta: dict) -> float:
    """Cung ly le voi ``canvas_size``: anh khong co fps that (chi la so mac dinh) nen
    khong duoc de no de len fps cua video."""
    playable = playable_clips(meta)
    for only_video in (True, False):
        for c in playable:
            if only_video and clip_is_image(c):
                continue
            f = _num(c.get("fps"), 0.0)
            if f > 0:
                return f
    return DEFAULT_FPS


def _display_lanes(meta: dict) -> dict:
    """Lan DE VE cho tung clip (khoa theo ``id()`` cua dict).

    Khac ``assign_lanes`` mot diem: clip dang TAT TAM (``enabled=False``) khong duoc xep
    lan vi no khong tham gia phat, nhung van phai VE ra dau do. Truoc day chung roi ve
    mac dinh 0 — tuc dung vao hang "se phat", de len chinh clip dang chieu, nhin tuong
    nhu co hai clip cung mot cho.

    Cach xep: clip dang bat chiem cho truoc (dung ket qua ``assign_lanes``), roi tung clip
    tat tam duoc dat vao HANG THAP NHAT ma cho cua no con trong. Nho vay clip tat tam nam
    DUNG CHO CU cua no, chi mo di — khong nhay hang, khong an them mot dong chieu cao —
    tru khi cho do that su da bi clip khac chiem.
    """
    placed = assign_lanes(meta)
    lanes = {id(c): lane for c, lane in placed}
    # Cac khoang thoi gian da bi chiem tren tung hang.
    rows: list = []
    for c, lane in placed:
        while len(rows) <= lane:
            rows.append([])
        rows[lane].append((_num(c.get("start")), clip_end(c)))

    for c in list_clips(meta):
        if id(c) in lanes:
            continue
        s, e = _num(c.get("start")), clip_end(c)
        row = next(
            (
                i for i, busy in enumerate(rows)
                if all(e <= bs + 1e-6 or s >= be - 1e-6 for bs, be in busy)
            ),
            None,
        )
        if row is None:
            row = len(rows)
            rows.append([])
        rows[row].append((s, e))
        lanes[id(c)] = row
    return lanes



def _clip_file_ok(clip: dict) -> bool:
    """Tep nguon cua clip con doc duoc khong.

    Xet ca KICH THUOC chu khong chi ton tai: tep tren o mang / OneDrive dang "online-only"
    hay bi cat giua chung van con ten nhung rong, ffmpeg mo ra la loi.
    """
    p = (clip or {}).get("path") or ""
    try:
        return bool(p) and os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False

def timeline_view(meta: dict, video_path: str = "", project_dir: str = "") -> list:
    """Danh sach cho FE ve lan video, kem so lan va co chong lan.

    ``video_path`` de danh dau clip nao la manh cua VIDEO GOC (xem ``is_source_clip``):
    khung xem truoc phai tron tieng theo dung luat cua tung loai.
    ``project_dir`` de biet clip nao DA TACH stem — khung xem truoc nghe qua 2 stem voi
    4 muc rieng, y nhu ban xuat.
    """
    lanes = _display_lanes(meta)
    playable = {id(c) for c, lane in assign_lanes(meta) if lane == 0}
    out = []
    for c in list_clips(meta):
        tin, tout = _num(c.get("in")), _num(c.get("out"))
        lane = lanes.get(id(c), 0)
        out.append({
            "id": c.get("id"),
            "name": c.get("name") or os.path.basename(c.get("path") or ""),
            "path": c.get("path") or "",
            "start": _num(c.get("start")),
            "in": tin,
            "out": tout,
            "duration": visible_duration(c),
            "source_duration": _num(c.get("source_duration")),
            "speed": clip_speed(c),
            # lane 0 = hang SE PHAT; >0 = dang chong lan (hoac dang tat tam), chi hien
            # de keo di cho khac.
            "lane": lane,
            # "Chong lan" chi noi ve clip DANG BAT ma bi clip khac giu cho. Clip tat tam
            # cung khong phat, nhung noi no "chong lan" la sai — nguoi dung se di tim
            # cho trong de keo vao trong khi chi can bat lai.
            "overlap": bool(c.get("enabled", True)) and id(c) not in playable,
            "volume": _num(c.get("volume"), 1.0),
            "muted": bool(c.get("muted", False)),
            "enabled": bool(c.get("enabled", True)),
            "width": int(_num(c.get("width"), 0)),
            "height": int(_num(c.get("height"), 0)),
            "has_audio": bool(c.get("has_audio", True)),
            # ANH tren lan video: khung xem truoc ve bang <img>, ban xuat dung -loop 1.
            "is_image": clip_is_image(c),
            # Manh cua video goc (tieng cua no la "tieng goc", chay qua bo tron 2 muc)
            # hay clip nguoi dung tu them (nghe dung nut am luong rieng, khong bi ha).
            "is_source": is_source_clip(c, video_path),
            # Da tach giong & nhac nen cho clip nay chua -> co thi nghe/tron qua 2 stem
            # voi 4 muc rieng ben duoi.
            "has_stems": bool(clip_stems(project_dir, c, video_path)) if project_dir else False,
            "use_stems": bool(c.get("use_stems", True)),
            # 4 muc DA CHOT (muc rieng cua clip, thieu thi cua du an) + co "co muc rieng"
            # de UI hien nut "về mức chung".
            "mix": clip_mix(meta, c),
            "own_mix": has_own_mix(c),
            # MUC RIENG THO cua clip (chi nhung khoa clip THAT SU de rieng; khoa nao
            # thua ke cua du an thi khong co mat).
            #
            # VI SAO FE CAN CAI NAY: ``mix`` o tren la ban DA CHOT tai thoi diem goi tin
            # nay duoc dung. Nguoi dung keo 4 muc CUA DU AN thi goi tin khong duoc dung
            # lai, nen khung xem truoc van tron theo muc cu — phai F5 moi nghe ra thay
            # doi (loi nguoi dung da bao). Co ban tho nay thi FE tu ghep: khoa nao clip
            # de rieng lay cua clip, con lai lay muc SONG cua du an.
            "mix_own": {
                k: max(0.0, min(1.0, _num((c.get(MIX_FIELD) or {}).get(k), 0.0)))
                for k in MIX_KEYS
                if isinstance(c.get(MIX_FIELD), dict)
                and (c.get(MIX_FIELD) or {}).get(k) is not None
            },
            # TEP NGUON CON TREN DIA KHONG — xem chu thich cung ten ben review_audio_clips.
            # Voi lan video con nang hon: mat tep video la ban xuat dung han giua chung.
            "missing": not _clip_file_ok(c),
        })
    out.sort(key=lambda x: (x["lane"], x["start"]))
    return out


def shift_clips_after(meta: dict, at: float, delta: float, exclude_id: str = None) -> int:
    """Doi cac clip video BAT DAU tu giay ``at`` tro di di ``delta`` giay.

    NGHICH DAO cua buoc day trong ``insert_clip_at``. Thieu no la mot lo hong that (nguoi
    dung da bao): xoa mot clip thi giong doc, nhac va phu de goc deu duoc keo ve lap cho
    trong, RIENG lan hinh dung yen — phim con lai treo lo lung o giua, dang truoc no la
    mot khoang den, va moi thu vua keo ve lai lech voi hinh.

    Cung dung khi NOI/BOP mot clip bang toc do: clip dai ra ma clip sau khong lui theo la
    hai cai chong len nhau, cai sau roi xuong hang ngoai va thoi khong phat nua.

    ``exclude_id`` = clip vua bi doi do dai (chinh no thi khong dời).
    """
    if abs(delta) < 0.001:
        return 0
    n = 0
    for c in list_clips(meta):
        if exclude_id and c.get("id") == exclude_id:
            continue
        s = _num(c.get("start"))
        if s < at - 1e-6:
            continue
        c["start"] = round(max(0.0, s + delta), 3)
        n += 1
    return n


def occupies_timeline(meta: dict, clip_id: str) -> bool:
    """Clip nay co THAT SU chiem cho tren mach phim khong (dang bat VA nam o lan 0).

    Clip tat tam, hoac dang CHONG LAN (bi clip khac giu cho, nam o lan >= 1) thi khong
    phat va khong vao ban xuat — no chi hien de nguoi dung keo di cho khac.

    Dung o cho XOA clip: chi clip dang chiem cho moi de lai mot khoang trong can lap.
    Xoa mot clip chong lan ma van keo phan sau ve la day lech het loi binh, trong khi
    tren mach phim chang co gi thay doi ca.
    """
    return any(
        lane == 0 and c.get("id") == clip_id for c, lane in assign_lanes(meta)
    )


def lane_count(meta: dict) -> int:
    """So HANG can ve cho lan video — tinh ca hang chua clip dang tat tam.

    Dung ``_display_lanes`` chu khong phai ``assign_lanes``: thieu hang do thi khoi
    timeline khong du cao va clip tat tam bi cat mat khoi khung.
    """
    return max(_display_lanes(meta).values(), default=0) + 1 if list_clips(meta) else 1
