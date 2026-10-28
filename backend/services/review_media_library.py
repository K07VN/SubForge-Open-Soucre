"""KHO MEDIA cua du an Review nang cao — nhap tep truoc, dua len timeline sau.

Giong CapCut: nguoi dung bam Nhap, chon MOT ME tep (video/nhac), chung nam trong kho
co anh dai dien + thoi luong; muon dung cai nao thi bam "Thêm vào timeline". Truoc day
chon tep la doi thang len timeline, khong co cho chua — muon so sanh vai clip hoac nhap
san 10 doan nhac roi tinh sau thi khong lam duoc.

    media_library: [
      {
        "id": "med-xxxx",
        "name": "canh-1.mp4",
        "path": "D:/phim/canh-1.mp4",   # video: TRO TAI CHO; nhac: da sao vao du an
        "kind": "video" | "audio",
        "duration": 125.4,
        "width": 1920, "height": 1080,  # chi video
        "fps": 30.0,
        "has_audio": true,
        "thumb": "assets/thumbs/med-xxxx.jpg",   # chi video
        "owned": false                  # true = tep nam trong du an, xoa kho thi xoa luon
      }
    ]

VI SAO video TRO TAI CHO ma nhac thi SAO VAO du an: video hang GB, nhan ban ra la het
dia; con nhac chi vai MB va thuong la tep nguoi dung tai ve roi xoa, giu ban sao thi du
an khong bi hong. Cung quy tac voi ``review_audio_clips``.
"""
from utils.i18n import L
import os
import subprocess
import uuid

import imageio_ffmpeg

from services import review_audio_clips as AUD
from services import review_video_clips as VID

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

FIELD = "media_library"
THUMB_DIRNAME = os.path.join("assets", "thumbs")
THUMB_WIDTH = 240


def new_id() -> str:
    return "med-" + uuid.uuid4().hex[:10]


def list_items(meta: dict) -> list:
    items = (meta or {}).get(FIELD)
    return items if isinstance(items, list) else []


def find(meta: dict, item_id: str):
    return next((i for i in list_items(meta) if i.get("id") == item_id), None)


def thumb_dir(project_dir: str) -> str:
    d = os.path.join(project_dir, THUMB_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _make_thumb(src: str, dst: str, duration: float) -> bool:
    """Lay MOT khung lam anh dai dien. Chon mot phan tu do vao giua clip cho khoi
    trung khung den/logo o dau phim.

    ``duration <= 0`` = tep ANH (mot khung duy nhat): TUYET DOI khong truyen ``-ss``. Voi
    demuxer anh, ``-ss 0`` lam ffmpeg nhay QUA khung duy nhat do va xuat ra tep rong
    (rc=0 nhung khong co gi) — anh trong kho se khong bao gio co anh dai dien.
    """
    seek = []
    if duration > 0:
        at = max(0.0, min(duration * 0.25, 5.0))
        seek = ["-ss", f"{at:.3f}"]
    res = subprocess.run(
        [FFMPEG_EXE, "-y", *seek, "-i", src, "-frames:v", "1",
         "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", dst],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True,
    )
    return res.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


def add_from_path(project_dir: str, meta: dict, src: str) -> dict:
    """Them mot tep vao kho. Tra ve item vua tao (chua ghi metadata — nguoi goi lo).

    Nhan CA BA loai: video, nhac va ANH. Anh len lan video thanh mot doan giu tren man
    may giay (anh bia, anh minh hoa, anh chen giua phim) — xem VID.make_clip.
    """
    ext = os.path.splitext(src)[1].lower()
    if ext not in AUD.MEDIA_EXTS and ext not in VID.IMAGE_EXTS:
        raise ValueError(L(f"Không nhận tệp {ext or '(không rõ đuôi)'}", f"File type not accepted: {ext or '(unknown extension)'}"))

    name = os.path.basename(src)
    item_id = new_id()

    if VID.is_image(src):
        # ANH: sao vao du an (chi vai tram KB, de du an tu chua duoc) va dung chinh no
        # lam anh dai dien.
        dest = os.path.join(AUD.asset_dir(project_dir), f"{item_id}{ext}")
        import shutil

        shutil.copy2(src, dest)
        info = VID.probe_video(dest)
        item = {
            "id": item_id, "name": name, "path": dest.replace("\\", "/"),
            "kind": "image",
            # Anh khong co thoi luong — do dai tren timeline do nguoi dung dat.
            "duration": 0.0,
            "width": info["width"], "height": info["height"],
            "has_audio": False, "owned": True,
        }
        thumb = os.path.join(thumb_dir(project_dir), f"{item_id}.jpg")
        if _make_thumb(dest, thumb, 0.0):
            item["thumb"] = os.path.relpath(thumb, project_dir).replace("\\", "/")
        return item

    if AUD.is_video(src):
        info = VID.probe_video(src)
        if info["duration"] <= 0:
            raise ValueError(L(f"Không đọc được tệp video: {name}", f"Could not read the video file: {name}"))
        item = {
            "id": item_id, "name": name, "path": src.replace("\\", "/"),
            "kind": "video", "duration": round(info["duration"], 3),
            "width": info["width"], "height": info["height"],
            "fps": info["fps"], "has_audio": info["has_audio"], "owned": False,
        }
        thumb = os.path.join(thumb_dir(project_dir), f"{item_id}.jpg")
        if _make_thumb(src, thumb, info["duration"]):
            item["thumb"] = os.path.relpath(thumb, project_dir).replace("\\", "/")
        return item

    # ---- nhac: sao vao du an de du an tu chua duoc ----
    dest = os.path.join(AUD.asset_dir(project_dir), f"{item_id}{ext}")
    import shutil

    shutil.copy2(src, dest)
    dur = AUD.media_duration(dest)
    if dur <= 0:
        # Doc khong ra thi don ban sao vua tao, khong de lai rac trong du an.
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        raise ValueError(L(f"Không đọc được tệp nhạc: {name}", f"Could not read the music file: {name}"))
    return {
        "id": item_id, "name": name, "path": dest.replace("\\", "/"),
        "kind": "audio", "duration": round(dur, 3), "has_audio": True,
        "owned": True,
    }


#  Da dua VIDEO GOC cua du an vao kho chua. Cot moc nay bat buoc phai co, dung ly le voi
#  ``VID.MIGRATED_FLAG``: khong co no thi nguoi dung go video goc khoi kho xong, lan nap
#  sau nó TU QUAY VE — go bao nhieu lan cung khong sach.
SOURCE_ADDED_FLAG = "media_source_added"


def ensure_source_item(project_dir: str, meta: dict, video_path: str) -> bool:
    """Dua VIDEO GOC cua du an vao kho Nguyen lieu. Tra ve True neu co SUA metadata.

    VI SAO CAN: video goc truoc day chi ton tai duoi dang MOT CLIP tren lan video (do
    ``VID.ensure_migrated`` dung), con kho Nguyen lieu thi trong tron — nen mo du an len
    khong thay tep minh vua them dau ca, va xoa clip do khoi timeline la khong con duong
    nao lay lai (ngoai Ctrl+Z). Coi no la mot muc trong kho thi: thay ngay no o day, them
    lai vao dau/cuoi mach phim bao nhieu lan cung duoc, kem anh dai dien va thoi luong.

    ``owned = False`` (mac dinh cho video, xem ``add_from_path``): kho chi TRO vao tep tren
    may nguoi dung, nen go muc nay khoi kho KHONG bao gio xoa phim goc cua ho.

    Chay dung MOT LAN cho moi du an (xem ``SOURCE_ADDED_FLAG``).
    """
    if (meta or {}).get(SOURCE_ADDED_FLAG):
        return False
    if not video_path or not os.path.exists(video_path):
        return False

    norm = video_path.replace("\\", "/")
    # Da co san trong kho (nguoi dung tu nhap chinh tep do) -> chi danh cot moc.
    for it in list_items(meta):
        if (it.get("path") or "").replace("\\", "/") == norm:
            meta[SOURCE_ADDED_FLAG] = True
            return True

    item = add_from_path(project_dir, meta, video_path)
    #  Danh dau de UI phan biet "phim goc cua du an" voi tep nguoi dung nhap them.
    item["source"] = True
    meta[FIELD] = list_items(meta) + [item]
    meta[SOURCE_ADDED_FLAG] = True
    return True


def remove(project_dir: str, meta: dict, item_id: str) -> bool:
    """Bo mot muc khoi kho. Tep do CHINH du an giu (nhac da sao) thi xoa luon.

    KHONG xoa tep video goc tren may nguoi dung — kho chi tro vao no.
    Cung KHONG kiem clip tren timeline con dung tep do khong: clip giu duong dan rieng
    nen van chay binh thuong; go khoi kho chi la don danh sach.
    """
    items = list_items(meta)
    target = next((i for i in items if i.get("id") == item_id), None)
    if target is None:
        return False

    if target.get("owned"):
        # Chi xoa khi KHONG con clip nao tren timeline dung den tep nay — ke ca clip tren
        # LAN VIDEO (anh sao vao du an cung "owned", xoa mat la clip anh thanh khung den).
        p = target.get("path") or ""
        used = any(
            (c.get("path") or "") == p
            for c in list(AUD.list_clips(meta)) + list(VID.list_clips(meta))
        )
        if p and os.path.exists(p) and not used:
            try:
                os.remove(p)
            except OSError:
                pass
    thumb = target.get("thumb")
    if thumb:
        tp = os.path.join(project_dir, thumb)
        if os.path.exists(tp):
            try:
                os.remove(tp)
            except OSError:
                pass

    meta[FIELD] = [i for i in items if i.get("id") != item_id]
    return True


def view(project_dir: str, meta: dict) -> list:
    """Danh sach cho FE ve kho media."""
    out = []
    for i in list_items(meta):
        path = i.get("path") or ""
        out.append({
            "id": i.get("id"),
            "name": i.get("name") or os.path.basename(path),
            "kind": i.get("kind") or "audio",
            "duration": float(i.get("duration") or 0.0),
            "width": int(i.get("width") or 0),
            "height": int(i.get("height") or 0),
            "has_audio": bool(i.get("has_audio", True)),
            "has_thumb": bool(i.get("thumb")),
            # Phim GOC cua du an (tu vao kho) hay tep nguoi dung nhap them — de UI dat nhan.
            "is_source": bool(i.get("source")),
            # Tep con tren dia khong: mat tep thi FE to mo va khong cho them vao timeline.
            "missing": not (path and os.path.exists(path)),
        })
    return out
