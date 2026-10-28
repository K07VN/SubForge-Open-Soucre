# -*- coding: utf-8 -*-
"""ĐĂNG VIDEO LÊN YOUTUBE — cấu hình Google, kết nối kênh, hàng đợi bài đăng.

KHÔNG có tiến trình nền ở đây, và đó là điểm mấu chốt của thiết kế: video được
đẩy lên NGAY khi người dùng bấm chạy, kèm ``publishAt`` — chính YouTube mở công
khai đúng giờ. Máy tắt, app đóng, mất mạng đều không ảnh hưởng tới giờ lên sóng.

Bí mật (client secret, refresh token) đi qua ``services/secret_box.py`` (DPAPI)
và KHÔNG BAO GIỜ được trả ra API — mọi chỗ hiển thị đều qua ``che()``.
"""
import logging
import os
import re
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

import services.youtube_service as YT
from models.database import get_db
from services import do_dai_video as DDV
from routes.auth_routes import verify_license
from models.models import Video, YoutubeChannel, YoutubeConfig, YoutubePost
from services.secret_box import che, giai_ma, ma_hoa
from utils.i18n import L

logger = logging.getLogger("YoutubeRoutes")
#  Tính năng có bản quyền như mọi chế độ khác — chặn ngay ở tầng router thay
#  vì kiểm trong từng hàm (14 endpoint, quên một chỗ là thủng).
router = APIRouter(prefix="/api/youtube", tags=["youtube"],
                   dependencies=[Depends(verify_license)])

#: config_id -> đường dẫn đăng nhập Google của lượt kết nối ĐANG CHỜ. Chỉ sống
#: trong lúc người dùng đang đăng nhập; xoá ngay khi xong hoặc khi bắt đầu lượt mới.
_LINK_DANG_NHAP: dict = {}
#: config_id đang bị người dùng bấm HUỶ. Lượt chờ đăng nhập soi sổ này mỗi 0,2 giây.
_HUY_DANG_NHAP: set = set()


# --------------------------------------------------------------------------- #
#  CẤU HÌNH GOOGLE (mỗi khách một dự án Google của riêng họ)
# --------------------------------------------------------------------------- #
#: Dạng Client ID của Google: <số dự án>-<32 ký tự thường/số>.apps.googleusercontent.com
_DANG_CLIENT_ID = re.compile(r"^\d{6,}-[a-z0-9]{10,}\.apps\.googleusercontent\.com$")


class ConfigRequest(BaseModel):
    #: Có id = SỬA cấu hình đó. Bỏ trống = THÊM MỚI (trùng Client ID sẽ bị chặn).
    id: str | None = None
    label: str = ""
    client_id: str = ""
    client_secret: str = ""


#: Client secret của Google dài khoảng 35 ký tự ("GOCSPX-" + 28). Ngắn hơn nhiều
#: thế thì chắc chắn là gõ thiếu hoặc dán nhầm.
DAI_SECRET_TOI_THIEU = 20


def _config_ra(cfg, db) -> dict:
    """Bản hiện ra màn hình — secret ĐÃ CHE, không bao giờ trả nguyên văn."""
    so_kenh = db.query(YoutubeChannel).filter(YoutubeChannel.config_id == cfg.id).count()
    secret = giai_ma(cfg.client_secret_enc or "")
    return {
        "id": cfg.id,
        "label": cfg.label or "Google của tôi",
        "client_id": cfg.client_id or "",
        "client_secret_che": che(secret),
        "co_secret": bool(cfg.client_secret_enc),
        #  Secret ngắn bất thường = chắc chắn hỏng. Cấu hình lưu từ TRƯỚC khi app
        #  biết kiểm khoá vẫn có thể đang mang một chuỗi bậy; không chỉ ra thì
        #  người dùng chỉ thấy "Cần kết nối lại" rồi đi đăng nhập lại vô ích.
        "secret_dang_ngo": bool(cfg.client_secret_enc) and len(secret) < DAI_SECRET_TOI_THIEU,
        "so_kenh": so_kenh,
        "han_muc": YT.con_lai(cfg),
    }


@router.get("/configs")
def danh_sach_config(db: Session = Depends(get_db)):
    cfgs = db.query(YoutubeConfig).order_by(YoutubeConfig.created_at).all()
    #  ``con_lai`` có thể xoá bộ đếm khi sang ngày mới -> lưu lại.
    ra = [_config_ra(c, db) for c in cfgs]
    db.commit()
    return {"configs": ra}


@router.post("/configs")
def luu_config(data: ConfigRequest = Body(...), db: Session = Depends(get_db)):
    cid = (data.client_id or "").strip()
    csec = (data.client_secret or "").strip()
    if not cid or not csec:
        raise HTTPException(status_code=400, detail=L("Cần cả Client ID và Client Secret.", "Both Client ID and Client Secret are required."))
    #  DẠNG THẬT của Client ID Google: "<số dự án>-<32 ký tự>.apps.googleusercontent.com".
    #  Chỉ kiểm đuôi thì gõ bừa "abc.apps.googleusercontent.com" vẫn lọt, rồi tới lúc
    #  bấm kết nối mới báo lỗi bằng tiếng Anh của Google — lúc đó người dùng không
    #  biết sai ở khoá hay ở bước nào trong sáu bước vừa làm.
    if not _DANG_CLIENT_ID.match(cid):
        raise HTTPException(
            status_code=400,
            detail=L("Client ID không đúng dạng của Google. Nó phải trông như "
                     "“123456789012-abc…xyz.apps.googleusercontent.com” — có thể bạn "
                     "đang dán nhầm API key, mã dự án, hoặc thiếu một phần khi chép.",
                     "The Client ID is not in Google's format. It should look like "
                     "“123456789012-abc…xyz.apps.googleusercontent.com” — you may have "
                     "pasted an API key or a project ID by mistake, or part of it is missing."),
        )

    trung = db.query(YoutubeConfig).filter(YoutubeConfig.client_id == cid).first()
    if data.id:
        #  SỬA một cấu hình đã có.
        cfg = db.query(YoutubeConfig).filter(YoutubeConfig.id == data.id).first()
        if not cfg:
            raise HTTPException(status_code=404, detail=L("Không tìm thấy cấu hình cần sửa.", "The configuration to edit was not found."))
        if trung and trung.id != cfg.id:
            raise HTTPException(
                status_code=400,
                detail=f"Client ID này đã dùng cho cấu hình “{trung.label}”.",
            )
    else:
        #  THÊM MỚI. Trùng Client ID thì BÁO LỖI chứ không lặng lẽ ghi đè bản cũ:
        #  người dùng tưởng mình vừa thêm dự án thứ hai, thực ra vừa đổi tên và đổi
        #  secret của dự án đầu — các kênh đang gắn vào đó im lặng đổi chủ.
        if trung:
            raise HTTPException(
                status_code=400,
                detail=L(f"Client ID này đã được khai ở cấu hình “{trung.label}”. "
                         "Muốn đổi khoá thì bấm Sửa ở cấu hình đó; muốn tách hạn mức "
                         "thì phải tạo một dự án Google KHÁC (Client ID khác).",
                         f"This Client ID is already registered in the configuration “{trung.label}”. "
                         "To change the key, click Edit on that configuration; to separate the quota "
                         "you must create a DIFFERENT Google project (a different Client ID)."),
            )
        cfg = YoutubeConfig(id=str(uuid.uuid4()), created_at=time.time())
        db.add(cfg)
    #  HỎI GOOGLE NGAY, đừng để tới lúc kết nối kênh mới biết gõ nhầm: lúc đó
    #  người dùng đã đi qua sáu bước Console và không biết sai ở đâu.
    try:
        YT.kiem_bi_mat(cid, csec)
    except YT.ClientSaiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cfg.label = (data.label or "").strip() or "Google của tôi"
    cfg.client_id = cid
    cfg.client_secret_enc = ma_hoa(csec)
    db.commit()
    return _config_ra(cfg, db)


@router.delete("/configs/{config_id}")
def xoa_config(config_id: str, db: Session = Depends(get_db)):
    cfg = db.query(YoutubeConfig).filter(YoutubeConfig.id == config_id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy cấu hình.", "Configuration not found."))
    so_kenh = db.query(YoutubeChannel).filter(YoutubeChannel.config_id == config_id).count()
    if so_kenh:
        raise HTTPException(
            status_code=400,
            detail=f"Còn {so_kenh} kênh đang dùng cấu hình này — gỡ kênh trước đã.",
        )
    db.delete(cfg)
    db.commit()
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  KÊNH
# --------------------------------------------------------------------------- #
def _kenh_ra(k) -> dict:
    return {
        "id": k.id,
        "config_id": k.config_id,
        "channel_id": k.channel_id,
        "title": k.title,
        "thumbnail": k.thumbnail_url,
        "status": k.status,
        "last_error": k.last_error,
        "co_quyen_phu_de": bool(getattr(k, "co_quyen_phu_de", 0)),
    }


@router.get("/channels")
def danh_sach_kenh(db: Session = Depends(get_db)):
    ks = db.query(YoutubeChannel).order_by(YoutubeChannel.created_at).all()
    return {"channels": [_kenh_ra(k) for k in ks]}


class KetNoiRequest(BaseModel):
    #: Xin thêm quyền TẢI PHỤ ĐỀ (``youtube.force-ssl``) — xem ``YT.SCOPE_PHU_DE``.
    phu_de: bool = False


@router.post("/configs/{config_id}/connect")
async def ket_noi_kenh(config_id: str, db: Session = Depends(get_db),
                       data: KetNoiRequest = Body(default=KetNoiRequest())):
    """Mở trình duyệt cho người dùng đăng nhập Google và thêm MỘT kênh.

    Bấm nhiều lần để thêm nhiều kênh — mỗi lần Google hỏi chọn kênh nào.
    """
    cfg = db.query(YoutubeConfig).filter(YoutubeConfig.id == config_id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail=L("Chưa có cấu hình Google.", "No Google configuration yet."))
    cid, csec = YT.bo_bi_mat(cfg)
    if not csec:
        raise HTTPException(
            status_code=400,
            detail=L("Không đọc được Client Secret (thường do đổi tài khoản Windows "
                     "hoặc cài lại máy) — hãy dán lại.",
                     "Cannot read the Client Secret (usually because the Windows account changed "
                     "or the machine was reinstalled) — please paste it again."),
        )
    try:
        #  Chặn tới 5 phút trong luồng riêng: người dùng còn phải đăng nhập,
        #  chọn kênh, bấm qua màn cảnh báo "app chưa xác minh".
        _LINK_DANG_NHAP.pop(config_id, None)
        _HUY_DANG_NHAP.discard(config_id)
        #  HỎI KÊNH NGAY TRONG LƯỢT ĐĂNG NHẬP. Hỏi ở đây (chứ không phải sau khi
        #  ``bat_dau_dang_nhap`` trả về) để TRANG WEB đang mở biết kết quả thật:
        #  tài khoản không có kênh YouTube nào thì trang báo lỗi luôn, thay vì báo
        #  "Đã kết nối xong" rồi người dùng quay lại app mới thấy chữ đỏ.
        hop: dict = {}

        def _kiem(access_token: str):
            hop["kenh"] = YT.lay_kenh(access_token)

        token = await run_in_threadpool(
            YT.bat_dau_dang_nhap, cid, csec, 300,
            lambda u: _LINK_DANG_NHAP.__setitem__(config_id, u),
            lambda: config_id in _HUY_DANG_NHAP,
            _kiem,
            bool(data.phu_de),
        )
        access = token["access_token"]
        kenh = hop["kenh"]
        #  ``channels.list`` cũng lấy đơn vị từ túi chung 10.000/ngày. Một đơn vị
        #  thì bé, nhưng bỏ qua vì bé là bắt đầu của bộ đếm sai: chỉ những chỗ
        #  "đắt" được trừ, rồi con số app hiện ra không còn khớp với Google nữa.
        YT.tru_don_vi(cfg, YT.GIA_DOC_KENH)
        db.commit()
    except YT.YoutubeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cu = (
        db.query(YoutubeChannel)
        .filter(YoutubeChannel.config_id == config_id,
                YoutubeChannel.channel_id == kenh["channel_id"])
        .first()
    )
    k = cu or YoutubeChannel(id=str(uuid.uuid4()), config_id=config_id,
                             created_at=time.time())
    k.channel_id = kenh["channel_id"]
    k.title = kenh["title"]
    k.thumbnail_url = kenh["thumbnail"]
    k.refresh_token_enc = ma_hoa(token["refresh_token"])
    #  Đọc từ THỨ GOOGLE CẤP (``scope`` của token), không từ ô tick: người dùng có thể bỏ
    #  tick quyền đó ngay trên màn cấp quyền của Google.
    k.co_quyen_phu_de = 1 if YT.co_quyen_phu_de(token) else 0
    k.status = "ok"
    k.last_error = None
    if not cu:
        db.add(k)
    db.commit()
    _LINK_DANG_NHAP.pop(config_id, None)
    _HUY_DANG_NHAP.discard(config_id)
    logger.info(f"[youtube] Đã kết nối kênh “{k.title}” ({k.channel_id}).")
    return {"channel": _kenh_ra(k), "them_moi": cu is None}


@router.post("/configs/{config_id}/connect/huy")
def huy_dang_nhap(config_id: str):
    """Bỏ lượt chờ đăng nhập đang treo.

    Lượt kết nối chặn tới 5 phút trong luồng riêng. Không có cửa này thì bấm nhầm
    hoặc đổi ý là phải ngồi nhìn vòng quay cho hết 5 phút, giao diện khoá cứng.
    """
    _HUY_DANG_NHAP.add(config_id)
    _LINK_DANG_NHAP.pop(config_id, None)
    return {"status": "ok"}


@router.get("/configs/{config_id}/connect/link")
def link_dang_nhap(config_id: str):
    """Đường dẫn đăng nhập Google của lượt kết nối ĐANG CHỜ (rỗng nếu không có).

    VÌ SAO TỒN TẠI: app mở trình duyệt MẶC ĐỊNH của Windows (thường là Edge),
    trong khi tài khoản Google của người dùng có thể đang đăng nhập ở Chrome hoặc
    ở một hồ sơ khác. Đưa đường dẫn ra để họ chép sang đúng trình duyệt — không
    thì hoặc phải đăng nhập lại từ đầu, hoặc cấp quyền nhầm tài khoản rồi mới phát
    hiện khi thấy tên kênh lạ hoắc.
    """
    return {"link": _LINK_DANG_NHAP.get(config_id, "")}


@router.delete("/channels/{channel_row_id}")
def go_kenh(channel_row_id: str, db: Session = Depends(get_db)):
    k = db.query(YoutubeChannel).filter(YoutubeChannel.id == channel_row_id).first()
    if not k:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy kênh.", "Channel not found."))
    cho = (
        db.query(YoutubePost)
        .filter(YoutubePost.channel_row_id == channel_row_id,
                YoutubePost.status.in_(["cho_doi", "dang_day"]))
        .count()
    )
    db.delete(k)
    db.commit()
    return {"status": "ok", "bai_dang_cho_bi_bo": cho}


# --------------------------------------------------------------------------- #
#  HÀNG ĐỢI BÀI ĐĂNG
# --------------------------------------------------------------------------- #
class PostRequest(BaseModel):
    channel_row_id: str
    file_path: str = ""
    video_id: str | None = None
    title: str = ""
    description: str = ""
    tags: str = ""
    category_id: str = "22"
    default_language: str = "vi"
    audio_language: str = ""
    notify_subscribers: bool = False
    made_for_kids: bool = False
    synthetic_media: bool = False
    dang: str = ""
    privacy: str = "private"
    publish_at: str | None = None
    thumbnail_path: str | None = None
    #  Ảnh trên CÓ được gửi lên YouTube không (xem ghi chú ở model).
    dung_thumbnail: bool = False
    #  Tải kèm phụ đề (xem ``_tai_phu_de``) + dự án nguồn "chi_tiet:<id>" / "long_tieng:<id>".
    tai_phu_de: bool = False
    du_an_nguon: str | None = None


def _gio_hen_da_qua(publish_at) -> bool:
    """Mốc hẹn công khai này đã trôi qua chưa?"""
    if not publish_at:
        return False
    try:
        from datetime import datetime, timezone

        t = datetime.fromisoformat(str(publish_at).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _post_ra(p, db, ngan_sach=None) -> dict:
    """``ngan_sach`` = ``[n]``: số tệp CHƯA TỪNG ĐO được phép đo độ dài trong lượt này (xem ``services.do_dai_video``); ``None`` = chỉ đọc số đã nhớ."""
    k = db.query(YoutubeChannel).filter(YoutubeChannel.id == p.channel_row_id).first()
    return {
        "id": p.id,
        "channel_row_id": p.channel_row_id,
        "channel_title": k.title if k else L("(kênh đã gỡ)", "(channel removed)"),
        "video_id": p.video_id,
        "file_path": p.file_path,
        "ten_tep": os.path.basename(p.file_path or ""),
        #  Ảnh bìa để bảng hàng đợi nhìn ra ngay bài nào là bài nào — đọc tiêu đề
        #  tiếng Trung/tiếng Nhật giữa hai chục dòng thì rất khó nhận.
        "thumbnail_path": p.thumbnail_path or "",
        "dung_thumbnail": bool(p.dung_thumbnail),
        "title": p.title,
        "description": p.description,
        "tags": p.tags,
        "privacy": p.privacy,
        "default_language": p.default_language or "",
        "audio_language": p.audio_language or "",
        "notify_subscribers": bool(p.notify_subscribers),
        "made_for_kids": bool(p.made_for_kids),
        "synthetic_media": bool(p.synthetic_media),
        "dang": p.dang or "",
        "publish_at": p.publish_at,
        #  GIỜ HẸN ĐÃ QUA. YouTube coi ``publishAt`` trong quá khứ là "công khai
        #  NGAY" — nên một loạt bài nằm lại qua đêm vì hết hạn mức sẽ công khai
        #  ĐỒNG LOẠT vào hôm sau, mất sạch lịch rải. Phải nói ra trước khi đẩy.
        "gio_hen_da_qua": _gio_hen_da_qua(p.publish_at),
        "status": p.status,
        "attempts": p.attempts,
        "last_error": p.last_error,
        "canh_bao": p.canh_bao or "",
        "tai_phu_de": bool(getattr(p, "tai_phu_de", 0)),
        "du_an_nguon": getattr(p, "du_an_nguon", None) or "",
        "youtube_video_id": p.youtube_video_id,
        "link": f"https://youtu.be/{p.youtube_video_id}" if p.youtube_video_id else "",
        #  Tin hỏi lại từ YouTube. ``None`` = chưa biết (khác 0) — xem models.py.
        "yt": {
            "privacy": p.yt_privacy or "",
            "upload_status": p.yt_upload_status or "",
            "rejection": p.yt_rejection or "",
            "processing": p.yt_processing or "",
            "published_at": p.yt_published_at or "",
            "views": p.yt_views,
            "likes": p.yt_likes,
            "comments": p.yt_comments,
            "checked_at": float(p.yt_checked_at or 0),
            "da_xoa": bool(p.yt_da_xoa),
            "tre_em": bool(p.yt_tre_em),
            "chan_het": bool(p.yt_chan_het),
            "so_nuoc_chan": int(p.yt_so_nuoc_chan or 0),
            "nuoc_chan": [x for x in (p.yt_nuoc_chan or "").split(",") if x],
            "co_ban_quyen": bool(p.yt_co_ban_quyen),
            "loi_xu_ly": [x for x in (p.yt_loi_xu_ly or "").split(",") if x],
        },
        "bytes_sent": float(p.bytes_sent or 0),
        "kich_thuoc": (
            os.path.getsize(p.file_path)
            if p.file_path and os.path.exists(p.file_path)
            else 0
        ),
        #  Độ dài video (giây), 0 = chưa biết. Hàng đợi không lưu độ dài: đo từ tệp nguồn MỘT lần rồi nhớ (``services.do_dai_video``),
        #  bài cũ được đo dần qua vài lượt mở danh sách. Chỉ để XEM (Trang chủ), không ảnh hưởng việc đăng.
        "thoi_luong": round(DDV.giay_cua(p.file_path or "", ngan_sach) or 0, 1),
    }


#: Số bài chưa từng đo độ dài được đo thêm trong MỘT lượt ``GET /posts``.
_NGAN_SACH_DO_DAI = 6

#: Nhóm trạng thái cho bộ lọc của bảng hàng đợi.
_NHOM_LOC = {
    "cho": ["cho_doi", "loi", "dang_day"],
    "da_len": ["da_dang", "da_len_lich"],
    "loi": ["loi"],
}


def _dieu_kien_loc(ten: str):
    """Điều kiện SQL cho một nhóm lọc.

    KHÔNG CÒN LỌC THUẦN THEO ``status``. Video bị YouTube CHẶN vẫn mang trạng thái
    "đã lên lịch" — theo đúng nghĩa của Google thì nó đã tải lên xong. Nhưng với
    người dùng thì nó HỎNG: Studio gạch bỏ lịch hẹn, video sẽ không lên sóng. Xếp
    nó vào nhóm "Đã lên YouTube" là khoe một thành công không có thật, còn tab
    "Lỗi" thì trống trơn trong khi có 4 bài chết.

    Vẫn KHÔNG đổi ``status`` trong cơ sở dữ liệu: "Đẩy hết" lấy bài theo
    ``status in (cho_doi, loi)``, đổi thành "loi" là bài đó được đẩy LẠI và kênh có
    hai video trùng nhau — mà đã lên sóng thì không rút lại được.
    """
    from sqlalchemy import and_, func, or_

    chan = or_(
        YoutubePost.yt_chan_het.is_(True),
        func.coalesce(YoutubePost.yt_so_nuoc_chan, 0) > 0,
    )
    khong_chan = and_(
        YoutubePost.yt_chan_het.isnot(True),
        func.coalesce(YoutubePost.yt_so_nuoc_chan, 0) == 0,
    )
    if ten == "loi":
        return or_(YoutubePost.status == "loi", chan)
    if ten == "da_len":
        return and_(YoutubePost.status.in_(_NHOM_LOC["da_len"]), khong_chan)
    if ten in _NHOM_LOC:
        return YoutubePost.status.in_(_NHOM_LOC[ten])
    return None


def _dem_theo_nhom(db) -> dict:
    """Đếm bài theo từng nhóm — để hiện số trên tab lọc mà không phải tải cả bảng."""
    ra = {"tat_ca": db.query(YoutubePost).count()}
    for ten in _NHOM_LOC:
        ra[ten] = db.query(YoutubePost).filter(_dieu_kien_loc(ten)).count()
    return ra


@router.get("/posts/dem")
def dem_bai(db: Session = Depends(get_db)):
    """CHỈ các con số — cho huy hiệu trên tab, gọi mỗi lần mở màn hình.

    Tách khỏi ``GET /posts`` vì cửa kia còn ``os.path.getsize`` từng tệp; chỉ để
    lấy một con số mà bắt ổ đĩa stat cả trăm tệp thì phí.
    """
    return {"dem": _dem_theo_nhom(db)}


#: Đã dọn cảnh báo cũ trong lượt chạy này chưa (một lần cho mỗi lần mở app).
_DA_DON_CANH_BAO = {"xong": False}

#: Những câu cảnh báo bản CŨ ghi vào TỪNG BÀI, nay đã chuyển thành một dải ở đầu
#: trang. Nhận ra bằng phần đầu câu — đủ đặc trưng, và không đụng vào cảnh báo thật
#: (hết hạn mức, đặt ảnh bìa hỏng) mà người dùng vẫn cần đọc.
_CANH_BAO_CU = ("Đây là Shorts nên app KHÔNG gửi ảnh bìa lên",)


def _don_canh_bao_cu(db) -> None:
    """Xoá cảnh báo bản cũ đã nhồi vào từng bài.

    VÌ SAO PHẢI DỌN CHỨ KHÔNG CHỈ THÔI GHI: câu đó đã nằm sẵn trong cơ sở dữ liệu
    của người dùng. Ngừng ghi thì bài MỚI sạch, còn mấy chục bài cũ vẫn in nguyên
    đoạn văn ấy lên bảng — đúng thứ vừa bị kêu là phiền. Chạy một lần mỗi lần mở
    app, một lượt UPDATE, không có gì để chạy lại.
    """
    if _DA_DON_CANH_BAO["xong"]:
        return
    _DA_DON_CANH_BAO["xong"] = True
    try:
        n = 0
        for dau in _CANH_BAO_CU:
            n += (
                db.query(YoutubePost)
                .filter(YoutubePost.canh_bao.like(f"{dau}%"))
                .update({YoutubePost.canh_bao: None}, synchronize_session=False)
            )
        if n:
            db.commit()
            logger.info(f"[youtube] Đã dọn {n} cảnh báo cũ khỏi bảng hàng đợi.")
    except Exception as e:  # noqa: BLE001 — dọn hộ thôi, hỏng thì bảng vẫn chạy
        logger.warning(f"[youtube] Không dọn được cảnh báo cũ: {e}")


@router.get("/posts")
def danh_sach_bai(
    limit: int = 30,
    offset: int = 0,
    loc: str = "tat_ca",
    db: Session = Depends(get_db),
):
    """Một TRANG của bảng hàng đợi, mới nhất trước.

    VÌ SAO PHẢI PHÂN TRANG: bảng này chỉ dài thêm chứ không bao giờ ngắn đi —
    người dùng đăng mỗi ngày vài chục bài và muốn xem lại lịch sử. Trả hết một
    lượt thì sau vài tháng mỗi lần mở tab là dựng hàng nghìn dòng, kèm một lượt
    ``stat`` cho từng tệp video.
    """
    _don_canh_bao_cu(db)
    q = db.query(YoutubePost)
    if loc in _NHOM_LOC:
        q = q.filter(_dieu_kien_loc(loc))
    tong = q.count()
    #  Chặn trần để một tham số lạ không kéo cả bảng về.
    limit = max(1, min(200, int(limit or 30)))
    ps = (
        q.order_by(YoutubePost.created_at.desc())
        .offset(max(0, int(offset or 0)))
        .limit(limit)
        .all()
    )
    #  Mỗi lượt chỉ đo thêm tối đa vài tệp chưa từng đo (mỗi tệp một ``ffmpeg -i``) — danh sách 200 bài cũ mà đo hết một lượt là treo trang.
    ngan_sach = [_NGAN_SACH_DO_DAI]
    return {
        "posts": [_post_ra(p, db, ngan_sach) for p in ps],
        "tong": tong,
        "dem": _dem_theo_nhom(db),
    }


@router.get("/posts/tien-do")
def tien_do_day(db: Session = Depends(get_db)):
    """Chỉ những con số cần cho THANH TIẾN ĐỘ, hỏi mỗi giây rưỡi lúc đang đẩy.

    Tách khỏi ``GET /posts`` vì cửa kia còn ``os.path.getsize`` từng tệp và dựng
    cả mô tả — hỏi liên tục trong lúc đẩy video 2GB là bắt ổ đĩa làm việc thừa
    đúng lúc nó đang bận nhất.
    """
    ps = (
        db.query(YoutubePost.id, YoutubePost.status, YoutubePost.bytes_sent)
        .filter(YoutubePost.status == "dang_day")
        .all()
    )
    return {
        "dang_day": [
            {"id": p.id, "status": p.status, "bytes_sent": float(p.bytes_sent or 0)}
            for p in ps
        ]
    }


@router.post("/posts")
def them_bai(data: PostRequest = Body(...), db: Session = Depends(get_db)):
    k = db.query(YoutubeChannel).filter(YoutubeChannel.id == data.channel_row_id).first()
    if not k:
        raise HTTPException(status_code=404, detail=L("Kênh không tồn tại.", "The channel does not exist."))

    duong_dan = (data.file_path or "").strip()
    if not duong_dan and data.video_id:
        #  Đăng thẳng từ dự án trong app: lấy bản đã dựng xong.
        v = db.query(Video).filter(Video.id == data.video_id).first()
        if v:
            from routes.youtube_meta_routes import ban_xuat_co_ban

            #  Bản xuất thật (đã đốt phụ đề / logo) trước, bản ghép giọng sau — khớp danh sách nguồn.
            duong_dan = ban_xuat_co_ban(v.id)
            if not duong_dan and v.final_video_path and os.path.exists(v.final_video_path):
                duong_dan = v.final_video_path
    if not duong_dan or not os.path.exists(duong_dan):
        raise HTTPException(
            status_code=400,
            detail=L("Chưa có tệp video để đăng — dự án phải dựng xong đã.", "There is no video file to publish yet — finish building the project first."),
        )

    loi = YT.kiem_metadata(data.title, data.description, data.tags)
    if loi:
        raise HTTPException(status_code=400, detail=" ".join(loi))

    #  GIỜ HẸN TRONG QUÁ KHỨ = CÔNG KHAI NGAY LẬP TỨC (tài liệu Google nói rõ).
    #  Chặn ở đây chứ không chỉ ở giao diện: lỡ tay là cả loạt bung cùng lúc.
    if data.publish_at:
        try:
            from datetime import datetime, timezone

            moc = datetime.fromisoformat(data.publish_at.replace("Z", "+00:00"))
            if moc <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=400,
                    detail=L("Giờ hẹn nằm trong quá khứ — YouTube sẽ công khai NGAY "
                             "lập tức. Chọn thời điểm trong tương lai.",
                             "The scheduled time is in the past — YouTube would make it public "
                             "IMMEDIATELY. Choose a time in the future."),
                )
        except ValueError:
            raise HTTPException(status_code=400, detail=L("Giờ hẹn không hợp lệ.", "Invalid scheduled time."))

    p = YoutubePost(
        id=str(uuid.uuid4()),
        channel_row_id=data.channel_row_id,
        video_id=data.video_id,
        file_path=duong_dan,
        title=data.title.strip(),
        description=data.description,
        tags=data.tags,
        category_id=data.category_id or "22",
        default_language=data.default_language or "vi",
        audio_language=(data.audio_language or "").strip(),
        notify_subscribers=1 if data.notify_subscribers else 0,
        made_for_kids=1 if data.made_for_kids else 0,
        synthetic_media=1 if data.synthetic_media else 0,
        dang=(data.dang or "").strip(),
        privacy=data.privacy or "private",
        publish_at=data.publish_at,
        thumbnail_path=data.thumbnail_path,
        dung_thumbnail=1 if data.dung_thumbnail else 0,
        tai_phu_de=1 if data.tai_phu_de else 0,
        du_an_nguon=(data.du_an_nguon or "").strip() or None,
        status="cho_doi",
        created_at=time.time(),
        updated_at=time.time(),
    )
    db.add(p)
    db.commit()
    return _post_ra(p, db)


class RaiLaiRequest(BaseModel):
    """Rải lại lịch cho các bài còn chờ."""

    bat_dau: str
    cach_nhau_phut: int = 15
    #  Chỉ những bài có giờ hẹn ĐÃ QUA, hay mọi bài đang chờ?
    chi_bai_qua_gio: bool = True


@router.post("/posts/rai-lai")
def rai_lai_lich(d: RaiLaiRequest = Body(...), db: Session = Depends(get_db)):
    """Đặt lại giờ công khai cho các bài còn nằm trong hàng đợi.

    VÌ SAO CẦN: hết hạn mức giữa chừng thì hàng chục bài nằm lại qua đêm, mang
    theo giờ hẹn của HÔM QUA. Đẩy lên vào hôm sau là YouTube thấy ``publishAt``
    trong quá khứ và công khai NGAY — cả loạt bung ra cùng một lúc, mất sạch công
    rải lịch. Sửa tay từng bài thì với 44 bài là 44 lần.
    """
    try:
        from datetime import datetime, timedelta, timezone

        goc = datetime.fromisoformat(d.bat_dau.replace("Z", "+00:00"))
        if goc.tzinfo is None:
            goc = goc.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=L("Giờ bắt đầu không hợp lệ.", "Invalid start time."))

    cach = max(1, int(d.cach_nhau_phut or 15))
    cac = (
        db.query(YoutubePost)
        .filter(YoutubePost.status.in_(["cho_doi", "loi"]))
        .order_by(YoutubePost.created_at)
        .all()
    )
    if d.chi_bai_qua_gio:
        cac = [p for p in cac if _gio_hen_da_qua(p.publish_at)]
    for i, p in enumerate(cac):
        p.publish_at = (goc + timedelta(minutes=i * cach)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p.updated_at = time.time()
    db.commit()
    return {"da_doi": len(cac)}


@router.post("/posts/bu-anh")
async def bu_anh_con_thieu(db: Session = Depends(get_db)):
    """Dựng lại ảnh xem trước cho các bài đang chờ mà chưa có ảnh.

    VÌ SAO CẦN: một bản trước đã bỏ trống ``thumbnail_path`` cho những bài người
    dùng không tự chọn ảnh — ý định là khỏi tốn hạn mức, nhưng nó xoá luôn cả ảnh
    dùng để NHÌN, nên hàng đợi chỉ còn một cột ô xám. Bài đã lưu như thế rồi thì
    sửa mã không cứu được, phải dựng lại ảnh từ chính tệp video.

    Ảnh dựng ở đây KHÔNG gửi lên YouTube (``dung_thumbnail`` giữ nguyên 0) — chỉ
    để nhìn, nên không tiêu một đơn vị hạn mức nào.
    """
    import services.youtube_meta as YM

    cho = (
        db.query(YoutubePost)
        .filter(YoutubePost.status.in_(["cho_doi", "loi"]))
        .all()
    )
    can = [p for p in cho
           if not (p.thumbnail_path or "").strip()
           and p.file_path and os.path.exists(p.file_path)]
    if not can:
        return {"da_bu": 0, "khong_con_tep": 0}

    def _mot(duong: str):
        ten = os.path.join(
            YM.thu_muc_anh(),
            f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}_goc.jpg",
        )
        return YM.khung_mac_dinh(duong, ten, YM.doan_khung(*YM.kich_thuoc_video(duong)))

    da = 0
    for p in can:
        try:
            kq = await run_in_threadpool(_mot, p.file_path)
            p.thumbnail_path = kq.get("path") if isinstance(kq, dict) else str(kq)
            p.updated_at = time.time()
            da += 1
        except Exception as e:  # noqa: BLE001
            #  Một tệp hỏng không được làm chết cả lượt bù — các bài khác vẫn có ảnh.
            logger.warning(f"[youtube] không bù được ảnh cho bài {p.id}: {e}")
    db.commit()
    thieu = sum(1 for p in cho
                if not (p.thumbnail_path or "").strip()
                and (not p.file_path or not os.path.exists(p.file_path)))
    return {"da_bu": da, "khong_con_tep": thieu}


@router.delete("/posts")
def xoa_nhieu_bai(loc: str = "cho", db: Session = Depends(get_db)):
    """Bỏ MỌI bài trong một nhóm lọc khỏi hàng đợi.

    VÌ SAO CẦN: quét một thư mục hoặc chọn nhầm cả trăm video là cả trăm dòng
    trong bảng, mà xoá từng dòng thì phải bấm cả trăm lần.

    KHÔNG ĐỤNG BÀI ĐANG ĐẨY: tệp của nó đang được gửi lên Google, xoá bản ghi
    giữa chừng là mất luôn ``resume_url`` — lượt sau không đẩy tiếp được mà phải
    tải lại từ đầu. Trả về số bài đã bỏ qua để giao diện nói cho người dùng biết.

    Bài ĐÃ LÊN YouTube bị bỏ khỏi bảng vẫn còn nguyên trên kênh; đây chỉ là bảng
    theo dõi của app.
    """
    q = db.query(YoutubePost)
    if loc in _NHOM_LOC:
        q = q.filter(_dieu_kien_loc(loc))
    cac = q.all()
    dang_day = [p for p in cac if p.status == "dang_day"]
    xoa = [p for p in cac if p.status != "dang_day"]
    for p in xoa:
        db.delete(p)
    db.commit()
    return {"da_xoa": len(xoa), "bo_qua_dang_day": len(dang_day),
            "dem": _dem_theo_nhom(db)}


@router.delete("/posts/{post_id}")
def xoa_bai(post_id: str, db: Session = Depends(get_db)):
    p = db.query(YoutubePost).filter(YoutubePost.id == post_id).first()
    if not p:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy bài.", "Post not found."))
    if p.status == "dang_day":
        raise HTTPException(status_code=409, detail=L("Bài đang đẩy — chờ xong đã.", "The post is being uploaded — wait until it finishes."))
    db.delete(p)
    db.commit()
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  CHẠY: đẩy video lên YouTube
# --------------------------------------------------------------------------- #
def _access_token(db, k) -> str:
    """Access token còn hạn cho kênh này, hoặc ném lỗi có câu tiếng Việt.

    Access token sống 1 giờ và KHÔNG được lưu xuống đĩa — lấy mới mỗi lần dùng.
    Refresh token hỏng (bị thu hồi / dự án còn ở Testing nên chết sau 7 ngày) thì
    ĐÁNH DẤU KÊNH thay vì chỉ ném lỗi, để danh sách kênh hiện đỏ và người dùng
    biết phải kết nối lại — không im lặng thất bại từng bài một.
    """
    cfg = db.query(YoutubeConfig).filter(YoutubeConfig.id == k.config_id).first()
    if not cfg:
        raise YT.YoutubeError(L("Kênh này mất cấu hình Google — hãy kết nối lại.", "This channel lost its Google configuration — please reconnect."))
    cid, csec = YT.bo_bi_mat(cfg)
    rt = giai_ma(k.refresh_token_enc or "")
    if not rt:
        k.status = "can_ket_noi_lai"
        k.last_error = L("Không giải mã được phiên đăng nhập (đổi tài khoản Windows?)", "Cannot decrypt the sign-in session (did the Windows account change?)")
        db.commit()
        raise YT.YoutubeError(k.last_error)
    try:
        token = YT.lay_access_token(cid, csec, rt)
    except YT.ClientSaiError:
        #  Sai KHOÁ CỦA DỰ ÁN thì mọi kênh của dự án đó đều gọi không được, nhưng
        #  phiên của từng kênh vẫn còn nguyên — KHÔNG đánh dấu kênh, không thì
        #  người dùng đi đăng nhập lại từng cái mà chẳng đỡ gì.
        raise
    except YT.YoutubeError as e:
        k.status = "can_ket_noi_lai"
        k.last_error = str(e)
        db.commit()
        raise

    #  GỌI ĐƯỢC RỒI thì gỡ cờ đỏ. Không tự lành thì kênh từng bị đánh dấu oan (vd
    #  lúc khoá dự án đang sai) sẽ đỏ mãi tới khi người dùng đăng nhập lại — trong
    #  khi nó đã dùng được bình thường.
    if k.status != "ok":
        k.status = "ok"
        k.last_error = None
        db.commit()
    return token


def _day_mot_bai(db, p) -> dict:
    """Đẩy MỘT bài. Trả về bản ghi đã cập nhật.

    Trừ hạn mức NGAY KHI Google nhận video, không phải lúc bắt đầu: bắt đầu mà
    hỏng thì Google không tính, trừ sớm là app tưởng hết hạn mức trong khi vẫn
    còn.
    """
    k = db.query(YoutubeChannel).filter(YoutubeChannel.id == p.channel_row_id).first()
    if not k:
        raise YT.YoutubeError(L("Kênh của bài này đã bị gỡ.", "The channel of this post has been removed."))
    cfg = db.query(YoutubeConfig).filter(YoutubeConfig.id == k.config_id).first()

    YT.dam_bao_ngay(cfg)
    if int(cfg.upload_used or 0) >= YT.TRAN_UPLOAD_NGAY:
        raise YT.YoutubeError(
            L(f"Dự án Google này đã dùng hết {YT.TRAN_UPLOAD_NGAY} lượt đăng hôm nay. "
              "Hạn mức reset lúc nửa đêm giờ Thái Bình Dương (khoảng 14-15 giờ VN).",
              f"This Google project has used up all {YT.TRAN_UPLOAD_NGAY} uploads for today. "
              "The quota resets at midnight Pacific Time (around 2-3 pm Vietnam time).")
        )
    if not p.file_path or not os.path.exists(p.file_path):
        raise YT.YoutubeError(L(f"Không còn tệp video: {p.file_path}", f"The video file is gone: {p.file_path}"))

    token = _access_token(db, k)
    kich_thuoc = os.path.getsize(p.file_path)

    p.status = "dang_day"
    p.attempts = int(p.attempts or 0) + 1
    p.canh_bao = None
    p.updated_at = time.time()
    db.commit()

    #  Có URL dở từ lượt trước thì ĐẨY TIẾP, không mở lượt mới: mở lại là tải lên
    #  từ đầu, video 500MB đứt ở 90% thì mất trắng công.
    if not p.resume_url:
        try:
            p.resume_url = YT.mo_phien_day(token, p, kich_thuoc)
        except YT.YoutubeError as e:
            if YT.dong_bo_theo_loi(cfg, getattr(e, "ly_do", "")):
                db.commit()
            raise
        p.bytes_sent = 0
        db.commit()

    def ghi_tien_do(da_gui, tong):
        p.bytes_sent = float(da_gui)
        db.commit()

    try:
        kq = YT.day_tep(p.resume_url, p.file_path, int(p.bytes_sent or 0),
                        tien_do=ghi_tien_do, huy=lambda: _HUY_DAY_HET["bat"])
    except YT.YoutubeError as e:
        #  PHIÊN ĐẨY HẾT HẠN thì XOÁ URL DỞ đi. Tài liệu bảo gặp 404 phải mở lượt
        #  mới; giữ lại cái URL chết thì lần bấm sau lại nhắm vào đúng nó và hỏng
        #  y hệt — bài đó không bao giờ lên được, mà câu lỗi chẳng nói vì sao.
        if getattr(e, "ly_do", "") == "resumeHetHan":
            p.resume_url = None
            p.bytes_sent = 0
            db.commit()
        #  Đồng bộ TRƯỚC khi ném tiếp: câu lỗi đi lên trên, còn con số thì phải
        #  đúng ngay lúc giao diện tải lại — không thì bảng vẫn khoe "còn 97 lượt"
        #  ngay bên cạnh dòng lỗi "hết hạn mức".
        if YT.dong_bo_theo_loi(cfg, getattr(e, "ly_do", "")):
            db.commit()
        raise

    p.youtube_video_id = kq.get("id") or ""
    p.status = "da_len_lich" if p.publish_at else "da_dang"
    p.last_error = None
    p.resume_url = None
    p.bytes_sent = kich_thuoc
    p.updated_at = time.time()
    cfg.upload_used = int(cfg.upload_used or 0) + YT.GIA_UPLOAD
    db.commit()

    #  Ảnh đại diện: hỏng thì KHÔNG làm hỏng cả bài — video đã lên rồi, chỉ ghi
    #  CẢNH BÁO (không phải ``last_error``, không thì bài hiện thành "Lỗi" và
    #  người dùng đi đăng lại thành hai video trùng nhau).
    #  SHORTS THÌ ĐỪNG GỬI — YOUTUBE KHÔNG NHẬN QUA API.
    #
    #  Đo thật 22-09-2026: năm Shorts gửi kèm ảnh bìa, ``thumbnails.set`` trả về 200
    #  (nên app không thấy lỗi gì, không ghi cảnh báo gì), mà vào Studio xem thì ảnh
    #  bìa vẫn là khung tự động. Trang trợ giúp của YouTube nói thẳng: ảnh bìa cho
    #  Shorts "chỉ thêm được trong YouTube Studio trên máy tính".
    #
    #  Cứ gửi thì mỗi Short ĐỐT 50 đơn vị để đổi lấy không gì cả — loạt 91 Short là
    #  4.550 đơn vị trong túi 10.000/ngày, tức gần nửa ngày hạn mức. Nên bỏ hẳn lượt
    #  gọi và NÓI RA, thay vì im lặng làm việc vô ích.
    if (p.dang or "") == "ngan" and p.dung_thumbnail and p.thumbnail_path:
        #  KHÔNG ghi cảnh báo vào TỪNG BÀI. Đây là chuyện chung của mọi Shorts, mà
        #  loạt thường toàn Shorts — in nguyên đoạn giải thích lên từng dòng thì
        #  bảng hàng đợi dài gấp ba và người dùng phải đọc lại cùng một câu hàng
        #  chục lần. Giao diện nói MỘT LẦN ở đầu trang, kèm nút xuất ảnh bìa ra
        #  thư mục để tự thay tay.
        #  Phụ đề thì Shorts VẪN nhận qua API — không được lọt mất ở nhánh ra sớm này.
        _tai_phu_de(db, p, k, cfg, token)
        return _post_ra(p, db)

    #  Chỉ gửi khi người dùng ĐÃ TỰ CHỌN ảnh. Ảnh app tự dựng vẫn nằm đó cho hàng
    #  đợi nhìn, nhưng không đem đi tiêu 50 đơn vị hạn mức của ai cả.
    if p.dung_thumbnail and p.thumbnail_path and os.path.exists(p.thumbnail_path):
        if not YT.du_don_vi(cfg, YT.GIA_THUMBNAIL):
            #  CHẶN TRƯỚC KHI GỌI. ``thumbnails.set`` lấy 50 đơn vị từ túi chung
            #  10.000/ngày — túi này KHÁC túi 100 lượt đăng. Đăng 90 bài kèm ảnh
            #  bìa là 4.500 đơn vị, hai loạt trong ngày là gần cạn; lúc đó Google
            #  trả lỗi tiếng Anh ngay sau khi video vừa lên, khó hiểu vô cùng.
            p.canh_bao = L(
                "Video đã lên YouTube nhưng CHƯA đặt được ảnh đại diện: dự án "
                f"Google này đã dùng hết {YT.TRAN_DON_VI_NGAY} đơn vị hôm nay "
                "(ảnh bìa tốn 50 đơn vị/lượt, khác với túi 100 lượt đăng). "
                "Hạn mức reset lúc nửa đêm giờ Thái Bình Dương — sau đó mở bài "
                "này bấm đặt lại ảnh, hoặc đặt tay trong YouTube Studio.",
                "The video is on YouTube but the thumbnail could NOT be set: this Google project "
                f"has used up its {YT.TRAN_DON_VI_NGAY} units today "
                "(a thumbnail costs 50 units each, separate from the 100-upload bucket). "
                "The quota resets at midnight Pacific Time — afterwards open this post "
                "and click set thumbnail again, or set it manually in YouTube Studio."
            )
            db.commit()
            logger.warning(f"[youtube] {p.canh_bao}")
        else:
            try:
                YT.dat_thumbnail(token, p.youtube_video_id, p.thumbnail_path)
                YT.tru_don_vi(cfg, YT.GIA_THUMBNAIL)
                db.commit()
            except YT.YoutubeError as e:
                #  Lượt gọi hỏng VẪN bị Google tính — trừ đơn vị kể cả khi lỗi,
                #  không thì bộ đếm của app thấp hơn thực tế.
                YT.tru_don_vi(cfg, YT.GIA_THUMBNAIL)
                #  Google vừa nói thẳng là hết hạn mức: tin họ, không tin bộ đếm
                #  của mình. Bộ đếm này chỉ biết việc app làm — cùng một dự án
                #  dùng ở máy khác là nó thấp hơn thật.
                YT.dong_bo_theo_loi(cfg, getattr(e, "ly_do", ""))
                p.canh_bao = f"Video đã lên nhưng chưa đặt được ảnh đại diện: {e}"
                db.commit()
                logger.warning(f"[youtube] {p.canh_bao}")

    _tai_phu_de(db, p, k, cfg, token)

    logger.info(
        f"[youtube] Đã đăng “{p.title}” lên kênh {k.title} — "
        + (f"hẹn công khai {p.publish_at}" if p.publish_at else "công khai theo cài đặt")
    )
    return _post_ra(p, db)


def _them_canh_bao(p, cau: str) -> None:
    p.canh_bao = (p.canh_bao + " · " if p.canh_bao else "") + cau


def _tai_phu_de(db, p, k, cfg, token: str) -> None:
    """TẢI KÈM PHỤ ĐỀ ngay sau khi video lên: thứ tiếng chính + mọi thứ tiếng thêm đã dịch
    đủ của dự án nguồn (``sub_track_routes.srt_cho_du_an``). ``captions.insert`` tốn 400
    đơn vị MỖI thứ tiếng từ túi chung 10.000 — hết túi thì dừng, không gọi bừa.

    Hỏng KHÔNG làm hỏng bài: video đã lên rồi — chỉ ghi cảnh báo để người dùng biết."""
    if not getattr(p, "tai_phu_de", 0) or not p.youtube_video_id:
        return
    if not getattr(k, "co_quyen_phu_de", 0):
        _them_canh_bao(p, L("Chưa tải phụ đề: kênh chưa cấp quyền tải phụ đề — vào tab Kênh, "
                            "tick “Cho phép tải phụ đề” rồi Kết nối lại kênh.",
                            "Subtitles were not uploaded: the channel has not granted the subtitle upload permission — go to the Channels tab, "
                            "tick “Allow subtitle upload” and reconnect the channel."))
        db.commit()
        return
    from routes.sub_track_routes import srt_cho_du_an
    from services.review_sub_tracks import nhan_thu_tieng

    #  Token lấy từ LÚC BẮT ĐẦU đẩy video: video dài đẩy quá 1 giờ là token hết hạn, mọi
    #  lượt phụ đề 401 mà vẫn bị trừ 400 đơn vị. Xin token mới (lỗi thì dùng tạm token cũ).
    try:
        token = _access_token(db, k) or token
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[youtube] Không làm mới được token trước khi tải phụ đề: {e}")

    try:
        cac_srt = srt_cho_du_an(getattr(p, "du_an_nguon", "") or "")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[youtube] Không dựng được phụ đề của “{p.title}”: {e}")
        cac_srt = {}
    if not cac_srt:
        _them_canh_bao(p, L("Chưa tải phụ đề: dự án nguồn không có câu phụ đề nào (hoặc bài "
                            "không đăng từ dự án).",
                            "Subtitles were not uploaded: the source project has no subtitle sentences (or the post "
                            "was not published from a project)."))
        db.commit()
        return
    xong, hong = [], []
    for lg, chu in cac_srt.items():
        if not YT.du_don_vi(cfg, YT.GIA_CAPTION):
            hong.append(f"{lg} (hết hạn mức hôm nay — mỗi phụ đề 400 đơn vị)")
            continue
        try:
            YT.dat_phu_de(token, p.youtube_video_id, lg, nhan_thu_tieng(lg), chu)
            YT.tru_don_vi(cfg, YT.GIA_CAPTION)
            xong.append(lg)
        except YT.YoutubeError as e:
            YT.tru_don_vi(cfg, YT.GIA_CAPTION)
            YT.dong_bo_theo_loi(cfg, getattr(e, "ly_do", ""))
            hong.append(f"{lg} ({e})")
            if getattr(e, "ly_do", "") == "insufficientPermissions":
                k.co_quyen_phu_de = 0
                break
        db.commit()
    if xong:
        logger.info(f"[youtube] Đã tải phụ đề {', '.join(xong)} cho “{p.title}”.")
    if hong:
        _them_canh_bao(p, L("Phụ đề chưa tải được: ", "Subtitles could not be uploaded: ") + "; ".join(hong))
        logger.warning(f"[youtube] {p.canh_bao}")
    db.commit()


@router.post("/posts/{post_id}/run")
async def chay_mot_bai(post_id: str, db: Session = Depends(get_db)):
    p = db.query(YoutubePost).filter(YoutubePost.id == post_id).first()
    if not p:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy bài.", "Post not found."))
    if p.status in ("da_dang", "da_len_lich"):
        raise HTTPException(status_code=409, detail=L("Bài này đã đăng rồi.", "This post has already been published."))
    try:
        return await run_in_threadpool(_day_mot_bai, db, p)
    except YT.YoutubeError as e:
        p.status = "loi"
        p.last_error = str(e)
        p.updated_at = time.time()
        db.commit()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/posts/refresh-status")
async def soat_trang_thai(db: Session = Depends(get_db)):
    """Hỏi lại YouTube xem những bài ĐÃ ĐĂNG giờ ra sao.

    Một lượt gọi lo 50 video và chỉ tốn 1 đơn vị hạn mức, nên soát cả trăm bài
    vẫn rẻ hơn một lần đặt ảnh đại diện — cứ hỏi thoải mái.

    Gom theo KÊNH vì mỗi kênh một access token. Kênh nào hỏng thì bỏ qua kênh đó
    chứ không làm hỏng cả lượt soát: một kênh hết hạn đăng nhập không được phép
    che mất số liệu của chín kênh còn lại.
    """
    bai = (
        db.query(YoutubePost)
        .filter(YoutubePost.youtube_video_id.isnot(None),
                YoutubePost.youtube_video_id != "")
        .all()
    )
    if not bai:
        return {"da_soat": 0, "loi": []}

    theo_kenh: dict = {}
    for p in bai:
        theo_kenh.setdefault(p.channel_row_id, []).append(p)

    da_soat, loi, don_vi = 0, [], 0
    for kid, ds in theo_kenh.items():
        k = db.query(YoutubeChannel).filter(YoutubeChannel.id == kid).first()
        if not k:
            continue
        try:
            token = await run_in_threadpool(_access_token, db, k)
            tin = await run_in_threadpool(
                YT.doc_thong_tin_video, token, [p.youtube_video_id for p in ds]
            )
        except YT.YoutubeError as e:
            loi.append({"kenh": k.title, "loi": str(e)})
            continue

        cfg = db.query(YoutubeConfig).filter(YoutubeConfig.id == k.config_id).first()
        if cfg:
            YT.dam_bao_ngay(cfg)
            #  Mỗi 50 video là một lượt gọi — tính đúng số lượt đã dùng.
            so_luot = (len(ds) + 49) // 50
            YT.tru_don_vi(cfg, so_luot * YT.GIA_DOC_VIDEO)
            don_vi += so_luot * YT.GIA_DOC_VIDEO

        for p in ds:
            t = tin.get(p.youtube_video_id or "")
            p.yt_checked_at = time.time()
            if not t:
                #  Google không trả về = video không còn trên YouTube (bị xoá,
                #  hoặc bị gỡ). Đánh dấu chứ không xoá bản ghi: người dùng còn
                #  cần biết mình đã từng đăng cái gì.
                p.yt_da_xoa = True
                continue
            p.yt_da_xoa = False
            p.yt_privacy = t["privacy"]
            p.yt_upload_status = t["upload_status"]
            p.yt_rejection = t["rejection"]
            p.yt_processing = t["processing"]
            p.yt_published_at = t["published_at"]
            p.yt_views, p.yt_likes, p.yt_comments = t["views"], t["likes"], t["comments"]
            p.yt_tre_em = t["made_for_kids"]
            p.yt_chan_het = bool(t.get("chan_het"))
            p.yt_so_nuoc_chan = int(t.get("so_nuoc_chan") or 0)
            p.yt_nuoc_chan = ",".join(t.get("vai_nuoc_chan") or [])
            p.yt_co_ban_quyen = bool(t.get("co_ban_quyen"))
            p.yt_loi_xu_ly = ",".join(t.get("loi_xu_ly") or [])

            #  ĐÃ CÔNG KHAI THẬT thì đổi nhãn từ "Đã lên lịch" sang "Đã đăng" —
            #  đây là điều người dùng canh, và chỉ YouTube mới biết chắc.
            if p.status == "da_len_lich" and t["privacy"] in ("public", "unlisted"):
                p.status = "da_dang"
            #  Xử lý hỏng / bị từ chối: video nằm trên kênh nhưng sẽ không bao giờ
            #  lên sóng. Kéo về "lỗi" kèm lý do, đừng để nó xanh mãi.
            if t["upload_status"] in ("failed", "rejected") or t["processing"] == "failed":
                p.status = "loi"
                p.last_error = (
                    t["rejection"] or t["upload_status"] or L("YouTube xử lý video thất bại", "YouTube failed to process the video")
                )
            da_soat += 1

    db.commit()
    return {"da_soat": da_soat, "loi": loi, "don_vi_da_dung": don_vi}


#  CỜ HUỶ cho lượt "Đẩy hết". Một cờ toàn cục là đủ vì app chỉ chạy một lượt đẩy
#  tại một thời điểm (nút bị khoá trong lúc chạy).
_HUY_DAY_HET = {"bat": False}


@router.post("/run-all/huy")
def huy_day_het():
    """Dừng lượt "Đẩy hết" đang chạy.

    Bài ĐANG đẩy dở dừng ngay giữa chừng, nhưng GIỮ LẠI ``resume_url`` nên lần
    sau đẩy tiếp từ chỗ dở chứ không tải lại từ đầu — video 2GB đứt ở 90% mà phải
    làm lại từ đầu thì thà đừng có nút huỷ.
    """
    _HUY_DAY_HET["bat"] = True
    return {"status": "ok"}


@router.post("/run-all")
async def chay_tat_ca(db: Session = Depends(get_db)):
    """Đẩy MỌI bài đang chờ, lần lượt.

    Đẩy hết trong một lượt là đúng tinh thần của thiết kế: giờ lên sóng đã nằm
    trong ``publishAt`` nên không cần chờ tới giờ mới đẩy. Hết hạn mức thì DỪNG
    và báo — phần còn lại vẫn nằm trong hàng đợi cho ngày mai, không đánh dấu lỗi.
    """
    _HUY_DAY_HET["bat"] = False
    cho = (
        db.query(YoutubePost)
        .filter(YoutubePost.status.in_(["cho_doi", "loi"]))
        .order_by(YoutubePost.created_at)
        .all()
    )
    xong, hong, het_han_muc, da_huy = 0, [], False, False
    #  DỰ ÁN NÀO ĐÃ CẠN thì bỏ qua các bài của nó, KHÔNG dừng cả lượt.
    #
    #  Bản trước ``break`` ngay ở bài đầu tiên gặp "hết hạn mức". Với một dự án
    #  thì đúng, nhưng ai đã nối hai dự án Google (cách hợp lệ để có 200 lượt/ngày)
    #  thì bài thứ 101 nằm ở dự án A làm chết luôn 99 bài của dự án B còn nguyên
    #  hạn mức — người dùng thấy "đã đăng 100 bài rồi hết" mà chẳng hiểu vì sao
    #  dự án kia không chạy.
    du_an_can: set = set()

    def _du_an_cua(bai) -> str:
        k = db.query(YoutubeChannel).filter(YoutubeChannel.id == bai.channel_row_id).first()
        return k.config_id if k else ""

    for p in cho:
        #  Soát cờ TRƯỚC mỗi bài. Bài đang đẩy dở tự dừng bên trong ``day_tep``
        #  (xem ``_day_mot_bai``), còn vòng này lo phần chưa bắt đầu.
        if _HUY_DAY_HET["bat"]:
            da_huy = True
            break
        if _du_an_cua(p) in du_an_can:
            #  Bài này thuộc dự án vừa cạn — để nguyên trạng thái chờ cho ngày mai.
            het_han_muc = True
            continue
        try:
            await run_in_threadpool(_day_mot_bai, db, p)
            xong += 1
        except YT.YoutubeError as e:
            loi = str(e)
            p.status = "loi"
            p.last_error = loi
            p.updated_at = time.time()
            db.commit()
            if "Đã huỷ theo yêu cầu" in loi or "Cancelled at your request" in loi:
                #  NGƯỜI DÙNG BẤM HUỶ — KHÔNG PHẢI LỖI. Để nguyên trạng thái "lỗi"
                #  thì bảng hiện một dòng đỏ cho việc chính họ vừa làm, và tệ hơn:
                #  bài đó không còn nằm trong nhóm "đang chờ" để đẩy tiếp.
                #  ``resume_url`` GIỮ NGUYÊN nên lượt sau đẩy tiếp từ chỗ dở.
                p.status = "cho_doi"
                p.last_error = None
                db.commit()
                da_huy = True
                break
            #  NHẬN RA BẰNG MÃ LÝ DO CỦA GOOGLE, không dò chữ tiếng Việt: câu
            #  chữ sẽ có người sửa cho hay hơn, còn mã thì không đổi. Vẫn giữ phép
            #  dò chữ cho câu do CHÍNH APP dựng ở bước chặn trước.
            ma = getattr(e, "ly_do", "")
            if (
                ma in YT.LY_DO_HET_HAN_MUC
                or ma in YT.LY_DO_HET_LUOT_DANG
                or ("hết" in loi and "lượt đăng" in loi)
                or ("used up" in loi and "uploads" in loi)
            ):
                #  Hết hạn mức: KHÔNG coi những bài còn lại là lỗi, chúng chỉ chưa
                #  tới lượt. Trả về trạng thái chờ để bảng hiện đúng sự thật.
                p.status = "cho_doi"
                p.last_error = None
                db.commit()
                het_han_muc = True
                #  Ghi sổ dự án này rồi CHẠY TIẾP — các bài của dự án khác vẫn đẩy
                #  được. Chỉ dừng hẳn khi mọi dự án đều cạn (vòng lặp tự hết).
                du_an_can.add(_du_an_cua(p))
                continue
            hong.append({"id": p.id, "title": p.title, "loi": loi})
    return {
        "da_dang": xong,
        "that_bai": hong,
        "het_han_muc": het_han_muc,
        "da_huy": da_huy,
        "con_cho": db.query(YoutubePost).filter(YoutubePost.status == "cho_doi").count(),
    }
