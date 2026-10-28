# -*- coding: utf-8 -*-
"""THƯ MỤC THEO DÕI — quét xem thư mục có video nào CHƯA đăng.

Cách dùng thật: người dùng để một thư mục "đã dựng xong", thỉnh thoảng bấm quét
là app chỉ ra những video MỚI, rồi đưa thẳng sang màn soạn bài để họ đặt tiêu đề,
chọn kênh, làm ảnh bìa và tự bấm thêm vào hàng đợi.

VÌ SAO KHÔNG TỰ XẾP LỊCH NỮA: bản trước quét xong là tạo bài luôn, tiêu đề lấy
nguyên tên tệp. Với thư mục video tải về thì tên tệp là một dòng tiếng Trung kèm
hashtag — cả loạt 41 bài vào hàng đợi mang tiêu đề như vậy, sửa thì phải sửa từng
bài, mà kênh đăng lên cũng chỉ chọn được MỘT. Nay thư mục theo dõi chỉ làm đúng
việc nó làm tốt: TÌM ra video mới. Phần soạn để màn soạn bài lo, nơi đã có AI đặt
tiêu đề, chọn nhiều kênh, ảnh bìa hàng loạt và rải lịch.

"MỚI" LÀ GÌ: tệp chưa có bài nào trong ``youtube_posts`` (dù đang chờ, đã đăng
hay lỗi). CỐ Ý không ghi sổ riêng "đã xử lý những tệp nào": thêm sổ thứ hai là
sớm muộn hai sổ lệch nhau, rồi một video bị đăng hai lần lên kênh — lỗi không ai
sửa lại được sau khi đã lên sóng.
"""
import logging
import os
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from models.database import get_db
from models.models import YoutubeChannel, YoutubeFolder, YoutubePost
from routes.auth_routes import verify_license
from utils.i18n import L

logger = logging.getLogger("YoutubeFolder")
router = APIRouter(prefix="/api/youtube", tags=["youtube"],
                   dependencies=[Depends(verify_license)])

DUOI_VIDEO = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
#: Trần số video đưa sang màn soạn bài trong MỘT lượt. Mỗi bài còn phải đo thời
#: lượng và cắt một khung ảnh bìa, nên đổ 500 tệp vào một lúc là màn soạn bài
#: nặng ì. Quá trần thì phần còn lại vẫn là "mới", bấm lần nữa là tiếp.
TRAN_MOT_LUOT = 50


class FolderRequest(BaseModel):
    """Thư mục cần theo dõi.

    CHỈ CÒN ĐƯỜNG DẪN VÀ CỜ ĐỆ QUY là thứ người dùng khai. Kênh đăng, tiêu đề,
    mô tả, thẻ, giờ rải… nay do màn SOẠN BÀI quyết định — khai hai nơi thì người
    dùng phải nhớ mình đã đặt gì ở đâu, mà cuối cùng vẫn là màn soạn bài thắng.
    Các trường cũ giữ lại để bản nâng cấp không mất dữ liệu đã lưu.
    """

    id: str | None = None
    path: str
    channel_row_id: str = ""
    de_quy: bool = False
    category_id: str = "22"
    privacy: str = "private"
    default_language: str = "vi"
    mo_ta_mac_dinh: str = ""
    the_mac_dinh: str = ""
    dung_ai: bool = False
    gio_dau: str = "08:00"
    cach_nhau_gio: float = 6.0


def _ra(f, db) -> dict:
    k = (
        db.query(YoutubeChannel).filter(YoutubeChannel.id == f.channel_row_id).first()
        if f.channel_row_id
        else None
    )
    return {
        "id": f.id,
        "path": f.path,
        "de_quy": bool(f.de_quy),
        "channel_row_id": f.channel_row_id,
        "channel_title": k.title if k else "",
        "category_id": f.category_id,
        "privacy": f.privacy,
        "default_language": f.default_language,
        "mo_ta_mac_dinh": f.mo_ta_mac_dinh or "",
        "the_mac_dinh": f.the_mac_dinh or "",
        "dung_ai": bool(f.dung_ai),
        "gio_dau": f.gio_dau or "08:00",
        "cach_nhau_gio": float(f.cach_nhau_gio or 6),
        "last_scan_at": float(f.last_scan_at or 0),
        "con_tren_dia": os.path.isdir(f.path or ""),
    }


@router.get("/folders")
def danh_sach(db: Session = Depends(get_db)):
    fs = db.query(YoutubeFolder).order_by(YoutubeFolder.created_at).all()
    return {"folders": [_ra(f, db) for f in fs]}


@router.post("/chon-thu-muc")
async def chon_thu_muc():
    from utils.native_dialogs import ask_directory

    p = await run_in_threadpool(ask_directory, L("Chọn thư mục chứa video cần đăng", "Choose the folder containing the videos to publish"))
    return {"path": p or ""}


@router.post("/folders")
def luu(data: FolderRequest = Body(...), db: Session = Depends(get_db)):
    duong = (data.path or "").strip()
    if not duong or not os.path.isdir(duong):
        raise HTTPException(status_code=400, detail=L("Thư mục không tồn tại.", "The folder does not exist."))
    #  KHÔNG đòi kênh nữa: thư mục chỉ để TÌM video mới, còn đăng lên kênh nào
    #  thì tick ở màn soạn bài, nơi chọn được NHIỀU kênh cùng lúc.

    f = (
        db.query(YoutubeFolder).filter(YoutubeFolder.id == data.id).first()
        if data.id
        else None
    )
    if not f:
        #  Cùng một thư mục thì SỬA bản cũ chứ không thêm bản mới — hai dòng y hệt
        #  nhau trong danh sách chẳng để làm gì ngoài gây rối.
        f = db.query(YoutubeFolder).filter(YoutubeFolder.path == duong).first()
    if not f:
        f = YoutubeFolder(id=str(uuid.uuid4()), created_at=time.time())
        db.add(f)

    f.path = duong
    f.de_quy = bool(data.de_quy)
    f.channel_row_id = data.channel_row_id
    f.category_id = data.category_id or "22"
    f.privacy = data.privacy or "private"
    f.default_language = data.default_language or "vi"
    f.mo_ta_mac_dinh = data.mo_ta_mac_dinh or ""
    f.the_mac_dinh = data.the_mac_dinh or ""
    f.dung_ai = bool(data.dung_ai)
    f.gio_dau = data.gio_dau or "08:00"
    #  Sàn 1 PHÚT. Trước là 0,25 giờ (15 phút) — chặn mất những nhịp rải
    #  dày mà giao diện nay cho đặt.
    f.cach_nhau_gio = max(1 / 60, float(data.cach_nhau_gio or 6))
    db.commit()
    return _ra(f, db)


@router.delete("/folders/{folder_id}")
def xoa(folder_id: str, db: Session = Depends(get_db)):
    f = db.query(YoutubeFolder).filter(YoutubeFolder.id == folder_id).first()
    if not f:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy thư mục.", "Folder not found."))
    db.delete(f)
    db.commit()
    #  Bài đã tạo GIỮ NGUYÊN: bỏ theo dõi thư mục không có nghĩa là huỷ lịch đăng.
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  QUÉT
# --------------------------------------------------------------------------- #
def _liet_ke(duong: str, de_quy: bool) -> list:
    ra = []
    try:
        if de_quy:
            for goc, _thu_muc, tep in os.walk(duong):
                for t in tep:
                    if os.path.splitext(t)[1].lower() in DUOI_VIDEO:
                        ra.append(os.path.join(goc, t))
        else:
            for t in os.listdir(duong):
                d = os.path.join(duong, t)
                if os.path.isfile(d) and os.path.splitext(t)[1].lower() in DUOI_VIDEO:
                    ra.append(d)
    except OSError as e:
        raise HTTPException(status_code=400, detail=L(f"Không đọc được thư mục: {e}", f"Cannot read the folder: {e}"))
    #  Xếp theo NGÀY SỬA, cũ trước: video quay trước thì đăng trước. Xếp theo tên
    #  thì "tap10" đứng trước "tap2", sai hẳn thứ tự người xem cần.
    ra.sort(key=lambda p: os.path.getmtime(p))
    return ra


def _video_moi(f, db) -> list:
    """Tệp trong thư mục CHƯA có bài nào trong hàng đợi."""
    da_co = {
        (p.file_path or "").lower()
        for p in db.query(YoutubePost.file_path).all()
    }
    return [p for p in _liet_ke(f.path, bool(f.de_quy))
            if p.lower() not in da_co]


@router.post("/folders/{folder_id}/quet")
async def quet(folder_id: str, db: Session = Depends(get_db)):
    f = db.query(YoutubeFolder).filter(YoutubeFolder.id == folder_id).first()
    if not f:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy thư mục.", "Folder not found."))
    if not os.path.isdir(f.path or ""):
        raise HTTPException(
            status_code=400,
            detail=L(f"Thư mục không còn: {f.path}. Ổ ngoài chưa cắm, hoặc đã đổi tên.", f"The folder is gone: {f.path}. The external drive may be unplugged, or the folder was renamed."),
        )
    moi = await run_in_threadpool(_video_moi, f, db)
    f.last_scan_at = time.time()
    db.commit()
    return {
        "moi": [
            {"path": p, "ten": os.path.basename(p),
             "kich_thuoc": os.path.getsize(p) if os.path.exists(p) else 0}
            for p in moi[:200]
        ],
        "so_moi": len(moi),
        "tran_mot_luot": TRAN_MOT_LUOT,
    }
