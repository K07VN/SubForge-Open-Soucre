"""NHIEU DOAN AUDIO CHEN THEM tren timeline (nhac nen, hieu ung, tieng dem…).

Khac voi ``audioTrack`` cua compositor cu (chi MOT bai, phat tu dau va lap tui bui),
o day moi doan la mot clip doc lap giong CapCut:

    {
      "id":     "aud-xxxx",       # danh tinh, khong tai su dung
      "path":   "…/assets/audio/xxx.mp3",
      "name":   "nhac-mo-dau.mp3",
      "start":  12.5,             # dat o giay thu may tren timeline
      "in":     0.0,              # cat bo bao nhieu giay o DAU file nguon
      "out":    30.0,             # cat den giay thu may cua file nguon
      "volume": 0.35,
      "speed":  1.0,              # toc do phat (atempo, giu nguyen cao do)
      "fade_in": 0.0,             # vuot em dau doan (giay, tinh tren TIMELINE)
      "fade_out": 0.0,
      "role":   "music",          # music | stem_music | stem_vocals | video_audio
      "enabled": true
    }

QUY UOC THOI GIAN (giong CapCut): ``in``/``out`` do bang giay CUA TEP NGUON, con do
dai chiem tren timeline la ``(out - in) / speed``. Doi tocp do KHONG lam thay doi
in/out, chi lam khoi tren timeline ngan/dai ra. ``fade_in``/``fade_out`` do bang giay
tren TIMELINE (nguoi dung nghi "vuot 1 giay" chu khong nghi theo tep nguon).


Nho vay nguoi dung chen duoc nhieu bai, keo doi cho, cat ngan/keo dai tung doan —
va CHINH CAC SO NAY duoc dung lai khi dung phim (mix bang ffmpeg), nen nghe thu the
nao thi xuat ra the ay.

Du lieu nam trong ``metadata.json`` cua du an (khoa ``audio_clips``) — khong dung vao
CompositorConfig de duong xuat compositor cu khong bi anh huong.
"""
import os
import subprocess
import uuid

import imageio_ffmpeg

#  Tran am luong dung CHUNG voi lan video / 4 muc tron / giong doc — mot cho khai bao.
from services.review_timeline import clamp_volume

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

FIELD = "audio_clips"
ASSET_DIRNAME = os.path.join("assets", "audio")
ALLOWED_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
#  Tep VIDEO cung them duoc: he thong rut phan TIENG ra thanh mot doan tren timeline.
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts")
MEDIA_EXTS = ALLOWED_EXTS + VIDEO_EXTS

#  atempo cua ffmpeg chi chay tot trong khoang nay.
SPEED_MIN, SPEED_MAX = 0.5, 2.0

#  Vai cua doan audio — chi de hien thi cho de nhan (ten, biểu tuong).
ROLE_MUSIC = "music"
ROLE_STEM_MUSIC = "stem_music"
ROLE_STEM_VOCALS = "stem_vocals"
ROLE_VIDEO_AUDIO = "video_audio"

#  Kieu MIME theo duoi tep — tra sai kieu thi Chrome co the tu choi phat (truoc day
#  moi tep deu duoc tra la audio/mpeg, ke ca .wav/.flac/.m4a).
MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


def mime_of(path: str) -> str:
    return MIME_BY_EXT.get(os.path.splitext(path or "")[1].lower(), "audio/mpeg")


def is_video(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in VIDEO_EXTS


def new_id() -> str:
    return "aud-" + uuid.uuid4().hex[:10]


def asset_dir(project_dir: str) -> str:
    d = os.path.join(project_dir, ASSET_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def list_clips(meta: dict) -> list:
    clips = (meta or {}).get(FIELD)
    return clips if isinstance(clips, list) else []


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def media_duration(path: str) -> float:
    """Do thoi luong file audio bang ffmpeg (chi goi khi vua tai len)."""
    if not path or not os.path.exists(path):
        return 0.0
    res = subprocess.run(
        [FFMPEG_EXE, "-i", path],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="ignore", close_fds=True,
    )
    import re

    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", res.stderr or "")
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def make_clip(path: str, name: str, start: float, duration: float,
              volume: float = 0.35, role: str = ROLE_MUSIC) -> dict:
    return {
        "id": new_id(),
        "path": path.replace("\\", "/"),
        "name": name,
        "start": round(max(0.0, _num(start)), 3),
        "in": 0.0,
        "out": round(max(0.1, _num(duration, 30.0)), 3),
        "volume": clamp_volume(volume, 0.35),
        "speed": 1.0,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "role": role,
        "enabled": True,
        # Lap lai doan nay cho toi het video (nhac nen thuong dung).
        "loop": False,
        "source_duration": round(max(0.1, _num(duration, 30.0)), 3),
    }


PATCHABLE = ("start", "in", "out", "volume", "enabled", "name", "loop",
             "speed", "fade_in", "fade_out")


def clip_speed(clip: dict) -> float:
    s = _num((clip or {}).get("speed"), 1.0)
    if s <= 0:
        s = 1.0
    return max(SPEED_MIN, min(SPEED_MAX, s))


def visible_duration(clip: dict) -> float:
    """Do dai doan CHIEM TREN TIMELINE = phan da cat chia cho toc do.

    Moi noi (timeline FE, xep lan, xem truoc, ban xuat) phai dung ham nay chu khong
    lay ``out - in``, neu khong doi toc do se lam khoi tren timeline lech voi tieng
    thuc su nghe duoc.
    """
    seg = max(0.0, _num((clip or {}).get("out")) - _num((clip or {}).get("in")))
    return seg / clip_speed(clip) if seg else 0.0


def patch_clip(meta: dict, clip_id: str, changes: dict) -> dict:
    """Sua 1 doan audio. Kep gia tri cho hop le: in < out, khong vuot do dai nguon."""
    clips = list_clips(meta)
    target = next((c for c in clips if c.get("id") == clip_id), None)
    if target is None:
        raise KeyError(clip_id)

    for k in PATCHABLE:
        if k in changes and changes[k] is not None:
            target[k] = changes[k]

    src_dur = _num(target.get("source_duration"), 0.0)
    tin = max(0.0, _num(target.get("in"), 0.0))
    tout = _num(target.get("out"), src_dur or 30.0)
    if src_dur > 0:
        tin = min(tin, max(0.0, src_dur - 0.1))
        tout = min(tout, src_dur)
    if tout <= tin + 0.05:
        tout = tin + 0.05
    target["in"] = round(tin, 3)
    target["out"] = round(tout, 3)
    target["start"] = round(max(0.0, _num(target.get("start"), 0.0)), 3)
    target["volume"] = clamp_volume(target.get("volume"), 0.35)
    target["enabled"] = bool(target.get("enabled", True))
    target["loop"] = bool(target.get("loop", False))
    target["speed"] = round(clip_speed(target), 3)

    # Fade do bang giay TREN TIMELINE. Hai dau cong lai khong duoc vuot do dai khoi,
    # neu khong afade cua ffmpeg se de len nhau va doan giua bi tut am vo ly.
    vis = visible_duration(target)
    fi = max(0.0, _num(target.get("fade_in"), 0.0))
    fo = max(0.0, _num(target.get("fade_out"), 0.0))
    if vis > 0:
        fi = min(fi, vis)
        fo = min(fo, vis)
        if fi + fo > vis:                     # chia doi phan con lai theo ti le
            scale = vis / (fi + fo)
            fi, fo = fi * scale, fo * scale
    target["fade_in"] = round(fi, 3)
    target["fade_out"] = round(fo, 3)
    if not target.get("role"):
        target["role"] = ROLE_MUSIC
    meta[FIELD] = clips
    return target


def delete_clip(meta: dict, clip_id: str) -> bool:
    clips = list_clips(meta)
    remain = [c for c in clips if c.get("id") != clip_id]
    meta[FIELD] = remain
    return len(remain) != len(clips)


def find_clip(meta: dict, clip_id: str):
    return next((c for c in list_clips(meta) if c.get("id") == clip_id), None)



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

def timeline_view(meta: dict) -> list:
    """Danh sach cho FE ve tren lan audio: kem do dai hien thi (out - in)."""
    out = []
    for c in list_clips(meta):
        tin, tout = _num(c.get("in")), _num(c.get("out"))
        out.append({
            "id": c.get("id"),
            "name": c.get("name") or os.path.basename(c.get("path") or ""),
            # Duong dan de FE goi API sao sang kho preset (app desktop, may cua chinh
            # nguoi dung) — khong dung de FE tu doc file.
            "path": c.get("path") or "",
            "start": _num(c.get("start")),
            "in": tin,
            "out": tout,
            # Do dai CHIEM TREN TIMELINE (da chia toc do) — FE ve khoi theo so nay.
            "duration": visible_duration(c),
            "source_duration": _num(c.get("source_duration")),
            "volume": _num(c.get("volume"), 0.35),
            "speed": clip_speed(c),
            "fade_in": _num(c.get("fade_in"), 0.0),
            "fade_out": _num(c.get("fade_out"), 0.0),
            "role": c.get("role") or ROLE_MUSIC,
            "enabled": bool(c.get("enabled", True)),
            "loop": bool(c.get("loop", False)),
            # TEP NGUON CON TREN DIA KHONG. Nguoi dung xoa / doi cho tep nhac sau khi da
            # chen vao timeline la chuyen thuong gap (nhat la nhac tai ve thu muc
            # Downloads roi don di). Khong bao ra thi khoi van nam do nhu binh thuong,
            # nghe thu im ru va ban xuat thieu mat mot duong tieng ma khong canh bao gi.
            "missing": not _clip_file_ok(c),
        })
    out.sort(key=lambda x: x["start"])
    return out


def has_audio_stream(path: str) -> bool:
    """Tep nay co duong tieng khong. Dung truoc khi tro ffmpeg vao ``[0:a]``."""
    if not path or not os.path.exists(path):
        return False
    res = subprocess.run(
        [FFMPEG_EXE, "-i", path],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="ignore", close_fds=True,
    )
    return "Audio:" in (res.stderr or "")


def extract_audio(src: str, dst: str) -> bool:
    """Rut duong TIENG cua mot tep video ra tep audio rieng.

    Ghi ra AAC .m4a thay vi WAV: phim 1 tieng ra WAV la ~600MB trong thu muc du an,
    con AAC 192k chi ~85MB ma tai khong phan biet duoc trong ban tron.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp.m4a"
    res = subprocess.run(
        [FFMPEG_EXE, "-y", "-i", src, "-vn", "-ac", "2", "-ar", "44100",
         "-c:a", "aac", "-b:a", "192k", tmp],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True,
    )
    if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, dst)
        return True
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return False


def is_inside_project(project_dir: str, path: str) -> bool:
    """Chan doc file ngoai thu muc du an (path do client gui len)."""
    try:
        root = os.path.realpath(project_dir)
        target = os.path.realpath(path)
        return os.path.commonpath([root, target]) == root
    except (ValueError, OSError):
        return False
