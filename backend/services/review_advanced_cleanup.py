"""DON DU LIEU RAC cua mot du an nang cao.

Vi sao can mot cho rieng: du an chinh sua lau ngay tich lai kha nhieu thu khong con ai
dung, ma khong cho nao nhin thay duoc:

  * thu muc giong cua nhung cau B DA BI XOA (moi lan "Tách hết" la mot loat uid moi,
    uid cu o lai nguyen ca thu muc audio);
  * ban tach stem cua clip video khong con tren timeline (moi ban ~200MB/stem);
  * anh thumbnail cua muc kho media da go;
  * muc kho media / clip tro vao tep KHONG CON TREN DIA (nguoi dung xoa hoac doi cho tep
    nguon) — thu nay khong chiem dia nhung lam ban xuat hong giua chung.

NGUYEN TAC: ham ``scan`` chi ĐỌC, khong dung gi. ``apply`` moi xoa, va CHI xoa nhung gi
``scan`` da liet ke. Nho tach doi nhu vay ma giao dien hien duoc "se xoa nhung gi, nang
bao nhieu" TRUOC khi nguoi dung dong y.

CAN THAN — dung nham thu: nhung tep NAM NGOAI thu muc du an (video goc, nhac nguoi dung
tu chon tren may) TUYET DOI khong dung toi. O day chi don trong thu muc du an, cong voi
viec go cac MUC tro vao tep da mat khoi metadata.
"""
from utils.i18n import L
import logging
import os
import shutil

from services import review_timeline as TL
from services import review_audio_clips as AUD
from services import review_video_clips as VID
from services import review_media_library as LIB

logger = logging.getLogger("ReviewService")


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _safe_rmtree(path: str) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except OSError as e:
        logger.warning(L(f"[nâng cao] Không xoá được {path}: {e}", f"[advanced] Could not delete {path}: {e}"))
        return False


def scan(project_dir: str, meta: dict, b_chunks: list) -> dict:
    """Liet ke rac. KHONG dung gi vao dia.

    Tra ve ``{"groups": [...], "bytes": <tong>, "total": <so muc>}``; moi nhom co
    ``key``, ``label``, ``count``, ``bytes`` va ``paths`` (duong dan tuyet doi se xoa)
    hoac ``ids`` (muc trong metadata se go).
    """
    groups = []

    # ---- 1. Thu muc giong cua cau B khong con ton tai ----
    live_uids = {TL.get_uid(b) for b in (b_chunks or [])}
    out_root = os.path.join(project_dir, "output")
    orphan_dirs, orphan_bytes = [], 0
    if os.path.isdir(out_root):
        for name in os.listdir(out_root):
            d = os.path.join(out_root, name)
            if not os.path.isdir(d) or name in live_uids:
                continue
            orphan_dirs.append(d)
            orphan_bytes += _dir_size(d)
    if orphan_dirs:
        groups.append({
            "key": "voice_orphan",
            "label": L("Thư mục giọng của câu đã xoá", "Voice folders of deleted sentences"),
            "hint": L("Mỗi lần tách câu hoặc xoá câu là uid cũ ở lại nguyên thư mục audio.", "Every time a sentence is split or deleted, the old uid stays in the audio folder."),
            "count": len(orphan_dirs),
            "bytes": orphan_bytes,
            "paths": orphan_dirs,
        })

    # ---- 2. Ban tach stem cua clip video khong con tren timeline ----
    live_keys = {
        VID.stems_key(c.get("path") or "") for c in VID.list_clips(meta)
    }
    stem_root = os.path.join(project_dir, VID.CLIP_STEMS_DIRNAME)
    stem_dirs, stem_bytes = [], 0
    if os.path.isdir(stem_root):
        for name in os.listdir(stem_root):
            d = os.path.join(stem_root, name)
            if not os.path.isdir(d) or name in live_keys:
                continue
            stem_dirs.append(d)
            stem_bytes += _dir_size(d)
    if stem_dirs:
        groups.append({
            "key": "stem_orphan",
            "label": L("Bản tách nhạc/giọng của clip đã bỏ", "Music/voice separations of removed clips"),
            "hint": L("Nặng nhất trong đám này — mỗi bản tách là hai tệp WAV của cả phim.", "The heaviest of this bunch — each separation is two WAV files of the whole film."),
            "count": len(stem_dirs),
            "bytes": stem_bytes,
            "paths": stem_dirs,
        })

    # ---- 3. Anh thumbnail khong con muc nao dung ----
    live_thumbs = {
        os.path.normcase(os.path.basename(i.get("thumb") or ""))
        for i in LIB.list_items(meta)
        if i.get("thumb")
    }
    tdir = os.path.join(project_dir, LIB.THUMB_DIRNAME)
    thumbs, thumb_bytes = [], 0
    if os.path.isdir(tdir):
        for name in os.listdir(tdir):
            if os.path.normcase(name) in live_thumbs:
                continue
            p = os.path.join(tdir, name)
            if not os.path.isfile(p):
                continue
            thumbs.append(p)
            try:
                thumb_bytes += os.path.getsize(p)
            except OSError:
                pass
    if thumbs:
        groups.append({
            "key": "thumb_orphan",
            "label": L("Ảnh thu nhỏ không còn dùng", "Thumbnails no longer used"),
            "hint": L("Của những mục đã gỡ khỏi kho nguyên liệu.", "Belonging to items removed from the material library."),
            "count": len(thumbs),
            "bytes": thumb_bytes,
            "paths": thumbs,
        })

    # ---- 4. MUC TRO VAO TEP DA MAT ----
    # Khong chiem dia, nhung nguy hiem hon ca: ban xuat gap clip mat tep la hong giua
    # chung. Liet ke rieng, va o day chi go MUC trong metadata chu khong xoa tep nao.
    missing_media = [
        i["id"] for i in LIB.view(project_dir, meta) if i.get("missing") and not i.get("is_source")
    ]
    if missing_media:
        groups.append({
            "key": "media_missing",
            "label": L("Mục trong kho trỏ vào tệp đã mất", "Library items pointing to missing files"),
            "hint": L("Tệp gốc bị xoá hoặc đổi chỗ. Gỡ mục khỏi kho, KHÔNG xoá tệp nào.", "The original file was deleted or moved. Remove the item from the library; NO file is deleted."),
            "count": len(missing_media),
            "bytes": 0,
            "ids": missing_media,
        })

    return {
        "groups": groups,
        "total": sum(g["count"] for g in groups),
        "bytes": sum(g["bytes"] for g in groups),
    }


def apply(project_dir: str, meta: dict, report: dict, keys=None) -> dict:
    """Xoa dung nhung gi ``scan`` da liet ke. ``keys`` = chi lam may nhom do."""
    wanted = set(keys) if keys else None
    removed, freed, meta_changed = 0, 0, False
    for g in report.get("groups") or []:
        if wanted is not None and g["key"] not in wanted:
            continue
        for p in g.get("paths") or []:
            # Chan mot cu cuoi: tuyet doi khong dung toi thu NGOAI thu muc du an.
            if not os.path.normcase(os.path.abspath(p)).startswith(
                os.path.normcase(os.path.abspath(project_dir)) + os.sep
            ):
                logger.warning(L(f"[nâng cao] Bỏ qua đường dẫn ngoài dự án: {p}", f"[advanced] Skipping a path outside the project: {p}"))
                continue
            ok = _safe_rmtree(p) if os.path.isdir(p) else _remove_file(p)
            if ok:
                removed += 1
                freed += g["bytes"] // max(1, g["count"])
        for item_id in g.get("ids") or []:
            # ``LIB.remove`` chi xoa tep khi muc do "owned" VA tep con tren dia — o day
            # tep da mat nen no chi go muc khoi kho, dung y ta muon.
            if LIB.remove(project_dir, meta, item_id):
                removed += 1
                meta_changed = True

    logger.info(
        L(f"[nâng cao] Dọn rác: bỏ {removed} mục, giải phóng ~{freed / 1024 / 1024:.1f}MB.", f"[advanced] Cleanup: removed {removed} items, freed ~{freed / 1024 / 1024:.1f}MB.")
    )
    return {"removed": removed, "freed": freed, "meta_changed": meta_changed}


def _remove_file(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError as e:
        logger.warning(L(f"[nâng cao] Không xoá được {path}: {e}", f"[advanced] Could not delete {path}: {e}"))
        return False
