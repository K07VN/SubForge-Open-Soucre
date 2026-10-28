# -*- coding: utf-8 -*-
"""PHỤ TRỢ CHO MÀN SOẠN BÀI YOUTUBE — chọn video, nhờ AI viết chữ, làm ảnh bìa.

Tách khỏi ``youtube_routes.py`` vì hai nhóm việc khác hẳn nhau: bên kia là hàng
đợi đăng bài (có hạn mức, có trạng thái, có bí mật), bên này chỉ là mấy việc
giúp soạn nhanh — hỏng thì bài vẫn đăng được bằng tay.
"""
import logging
import os
import re
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

import services.youtube_meta as YM
from services.compositor_utils import full_export_path
from models.database import get_db
from models.models import SubtitleSegment, Video
from routes.auth_routes import quyen_hien_tai, verify_license
from services.review_service import ReviewService
from utils.i18n import L

logger = logging.getLogger("YoutubeMeta")
router = APIRouter(prefix="/api/youtube", tags=["youtube"],
                   dependencies=[Depends(verify_license)])
review_service = ReviewService()

DUOI_VIDEO = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")


def ban_xuat_co_ban(video_id: str) -> str:
    """Bản XUẤT CẢ PHIM gần nhất còn trên đĩa của một dự án Cơ bản / Lồng tiếng (do tab ghép hình ghi nhớ trong ``compositor_config.json``), hoặc "".

    Chỉ ĐỌC (không tạo thư mục như ``get_project_dirs``): liệt kê hàng trăm dự án mà mỗi cái tạo thêm thư mục rỗng là việc không ai nhờ.
    Lỗi cấu hình / đọc tệp thì coi như chưa có — danh sách nguồn không được chết vì một dự án hỏng."""
    try:
        from services.compositor_utils import full_export_path
        from services.config_service import get_config_service

        goc = get_config_service().get_dubbing_root()
        if not goc or not video_id:
            return ""
        cfg = os.path.join(goc, video_id, "compositor_config.json")
        return full_export_path(cfg) if os.path.isfile(cfg) else ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[youtube] Không đọc được bản xuất của dự án {video_id}: {e}")
        return ""


# --------------------------------------------------------------------------- #
#  NGUỒN VIDEO ĐỂ ĐĂNG
# --------------------------------------------------------------------------- #
@router.get("/nguon")
def nguon_video(db: Session = Depends(get_db)):
    """Mọi video ĐÃ DỰNG XONG trong máy, gom từ cả hai chế độ.

    LUÔN LÀ BẢN XUẤT MỚI NHẤT, không cần đoán: cả hai chế độ đều ghi bản dựng vào
    MỘT đường dẫn cố định (``output/final_master.mp4`` cho Cơ bản,
    ``final_review.mp4`` cho Chi tiết), nên xuất lại là đè lên chính tệp đó. Không
    có chuyện tồn đọng nhiều bản để phải chọn.

    NGOẠI LỆ ĐÁNG NHỚ: "Xuất đoạn đã chọn" cố ý ghi ra tên tệp KHÁC để không đè
    lên bản dựng đầy đủ — nên thứ hiện ở đây luôn là bản ĐẦY ĐỦ, không phải bản
    cắt đoạn.

    Trả kèm ``xuat_luc`` (thời điểm sửa tệp) để người dùng nhìn ra ngay bản này
    cũ hay mới — dự án sửa hôm nay mà bản xuất từ tuần trước là chuyện phải thấy
    TRƯỚC khi đăng, không phải sau.

    Dự án CHƯA XUẤT vẫn được trả về với ``chua_xuat: True`` thay vì giấu đi: giấu
    thì người dùng tưởng app làm mất dự án của họ và đi tìm khắp nơi.
    """
    ra = []

    def _luc(duong_dan: str) -> float:
        try:
            return os.path.getmtime(duong_dan)
        except OSError:
            return 0.0

    for v in db.query(Video).order_by(Video.created_at.desc()).all():
        #  BẢN XUẤT THẬT TRƯỚC, bản ghép giọng sau. ``final_video_path`` chỉ được ghi ở bước
        #  "Ghép hoàn thiện" (lồng tiếng) — người dùng Cơ bản chỉ DỊCH rồi XUẤT từ tab ghép
        #  hình (nguồn "Video gốc", giữ âm thanh gốc) thì nó rỗng, và dự án bị báo "chưa xuất
        #  video" dù tệp xuất nằm sờ sờ trong ``compositor_config.json`` (``lastFullExportPath``).
        #  Có cả hai thì bản xuất thắng: nó đã đốt phụ đề / logo, còn bản ghép giọng thì chưa
        #  (cùng luật với chế độ Chi tiết ở dưới).
        xuat = ban_xuat_co_ban(v.id)
        master = v.final_video_path or ""
        co_master = bool(master and os.path.exists(master))
        p = xuat or (master if co_master else "")
        if p:
            ra.append({
                "nguon": "long_tieng",
                "id": v.id,
                "ten": v.title or os.path.basename(p),
                "path": p,
                "kich_thuoc": os.path.getsize(p),
                "thoi_luong": float(v.duration or 0),
                "ngon_ngu": v.target_lang or "vi",
                "kenh": "",
                "phong_cach": "",
                "xuat_luc": _luc(p),
                "chua_xuat": False,
                #  Chỉ có bản ghép giọng (chưa xuất từ tab ghép hình): chưa có phụ đề / logo.
                "chua_dot_phu_de": not xuat,
            })
        elif v.title:
            #  Có dự án nhưng chưa có bản dựng (chưa xuất, hoặc đã xoá tệp cho nhẹ máy).
            ra.append({
                "nguon": "long_tieng", "id": v.id, "ten": v.title, "path": "",
                "kich_thuoc": 0, "thoi_luong": float(v.duration or 0),
                "ngon_ngu": v.target_lang or "vi", "kenh": "", "phong_cach": "",
                "xuat_luc": 0.0, "chua_xuat": True,
            })

    root = review_service.projects_root
    if os.path.isdir(root):
        for meta in review_service.list_projects():
            pid = meta.get("id")
            if not pid:
                continue
            #  BẢN XUẤT THẬT (bước 2/2 — đã đốt phụ đề, logo, watermark) chứ không phải
            #  ``final_review.mp4``: tệp đó chỉ là bản trung gian của bước 1/2 (ghép giọng
            #  + nhạc), đăng nó lên là video KHÔNG có phụ đề (người dùng báo 25-09-2026).
            #  Chưa từng xuất cả phim thì mới rơi về bản trung gian, và báo ra.
            p = full_export_path(os.path.join(root, pid, "compositor_config.json"))
            chua_dot = not p
            if not p:
                p = os.path.join(root, pid, "final_review.mp4")
            co = os.path.exists(p)
            ra.append({
                "nguon": "chi_tiet",
                "id": pid,
                "ten": meta.get("title") or pid,
                "path": p if co else "",
                "kich_thuoc": os.path.getsize(p) if co else 0,
                "thoi_luong": float(meta.get("video_duration") or 0),
                "ngon_ngu": meta.get("target_language") or "vi",
                #  TÊN KÊNH + VĂN PHONG người dùng ĐÃ nhập ở bảng Chuẩn bị của dự
                #  án. Trả kèm để hộp nhờ AI điền sẵn — bắt họ gõ lại đúng những
                #  thứ vừa gõ ở màn bên cạnh là thừa, và gõ lại thì hai nơi lệch.
                "kenh": meta.get("brand_name") or "",
                "phong_cach": meta.get("speech_style") or "",
                "xuat_luc": _luc(p) if co else 0.0,
                "chua_xuat": not co,
                #  Chỉ có bản trung gian: có giọng nhưng CHƯA có phụ đề / logo.
                "chua_dot_phu_de": co and chua_dot,
                #  BÀI ĐÃ SOẠN SẴN ở bước "Soạn bài đăng" của lô chạy hàng loạt.
                #  Có sẵn thì màn soạn bài điền luôn, khỏi gọi AI lần nữa và khỏi
                #  bắt người dùng ngồi chờ đúng lúc muốn đăng.
                "da_soan": _bai_da_soan(meta),
                #  TÓM TẮT VIDEO do bước 0 của "tạo kịch bản bằng AI" viết (ngôn ngữ
                #  đích, cho người xem) — màn soạn bài có tuỳ chọn nối vào sau mô tả.
                "tom_tat": _tom_tat_du_an(os.path.join(root, pid)),
            })

    ra += _nguon_sang_tao()

    #  MỚI XUẤT NHẤT LÊN ĐẦU. Trước đây trộn hai chế độ theo hai thứ tự khác nhau
    #  (Cơ bản theo ngày tạo dự án, Chi tiết theo thứ tự thư mục) nên bản vừa xuất
    #  xong có thể nằm lọt thỏm giữa danh sách.
    ra.sort(key=lambda x: (not x["chua_xuat"], x["xuat_luc"]), reverse=True)
    return {"videos": ra}


def _nguon_sang_tao() -> list:
    """Dự án "Sáng tạo" (đọc báo học ngoại ngữ) — nguồn thứ ba của tab YouTube.

    BẢN DỰNG LÀ ``out.mp4`` trong thư mục dự án (dựng lại là đè lên chính tệp đó, giống hai chế độ kia).
    ``da_soan`` điền sẵn TIÊU ĐỀ (phương án AI soạn lúc nhập JSON ở bước 2) + MÔ TẢ (app dựng: bản quyền,
    link gốc, hashtag) + THẺ + chữ ảnh bìa — nên thêm video vào tab YouTube là có đủ, không phải soạn lại.
    Dự án chưa có cảnh nào thì bỏ qua (chưa có gì để đăng); có cảnh mà chưa dựng thì vẫn hiện ra,
    mờ, ``chua_xuat`` (cùng lý do với hai chế độ kia: giấu đi là người dùng tưởng app làm mất dự án).
    """
    from services import sangtao_du_an as SDA

    ra = []
    if quyen_hien_tai().get("allow_sang_tao") is not True:       # chế độ ẨN MẶC ĐỊNH: key chưa được cấp thì không liệt kê dự án Sáng tạo nào
        return ra
    try:
        ds = SDA.liet_ke()
    except OSError:
        return ra
    for row in ds:
        try:
            d = SDA.doc(row["id"])
            sb = d.get("storyboard") or {}
            if not (sb.get("scenes") or []):
                continue
            p = os.path.join(SDA.thu_muc(row["id"]), "out.mp4")
            co = os.path.isfile(p)
            ra.append({
                "nguon": "sang_tao",
                "id": row["id"],
                "ten": SDA.tieu_de_video(d) or d.get("title") or row["id"],
                "path": p if co else "",
                "kich_thuoc": os.path.getsize(p) if co else 0,
                "thoi_luong": YM.thoi_luong(p) if co else 0.0,
                #  Ngôn ngữ NÓI của video = ngôn ngữ đích (người học nghe tiếng đích).
                "ngon_ngu": sb.get("language") or (d.get("settings") or {}).get("language") or "en",
                "kenh": "",
                "phong_cach": "",
                "xuat_luc": os.path.getmtime(p) if co else 0.0,
                "chua_xuat": not co,
                "da_soan": SDA.bai_dang_youtube(d),
                #  Phụ đề CC (tiếng đích + tiếng mẹ đẻ) đã ghi cùng bản dựng: mã thứ tiếng sẽ được tải kèm. Rỗng = video dựng trước khi có tính năng này → dựng lại một lần.
                "phu_de": list(SDA.phu_de_srt(row["id"])) if co else [],
            })
        except (KeyError, ValueError, OSError) as e:  # noqa: PERF203 — một dự án hỏng không được làm hỏng cả danh sách
            logger.warning(f"[youtube] Bỏ qua dự án Sáng tạo {row.get('id')}: {e}")
    return ra


@router.post("/chon-tep")
async def chon_tep():
    """Mở hộp thoại Windows để chọn video có sẵn trong máy (chọn được nhiều)."""
    from utils.native_dialogs import ask_open_filenames

    def _mo():
        return ask_open_filenames(
            title=L("Chọn video để đăng lên YouTube", "Choose videos to publish to YouTube"),
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"),
                       (L("Tất cả", "All files"), "*.*")],
        )

    duong_dan = await run_in_threadpool(_mo)
    hop_le = [
        p for p in (duong_dan or [])
        if os.path.exists(p) and os.path.splitext(p)[1].lower() in DUOI_VIDEO
    ]

    #  PHẢI ĐO THỜI LƯỢNG. Trước đây chỗ này trả thẳng ``"thoi_luong": 0`` cho
    #  nhanh, nhưng ``0`` nghĩa là "không biết": giao diện mất nhãn Shorts, và
    #  prompt gửi cho AI cũng mất câu "đây là video ngắn" — video dọc 40 giây bị
    #  viết tiêu đề như phim dài. Video chọn từ dự án có sẵn thời lượng nên lỗi
    #  này chỉ lộ ra với tệp chọn tay.
    #
    #  Đo SONG SONG: mỗi tệp là một lượt ``ffmpeg -i`` chừng 0,1 giây, nhưng người
    #  dùng chọn cả trăm tệp một lúc thì xếp hàng lần lượt là chờ chục giây.
    return {"videos": await _dung_nguon(hop_le)}


async def _dung_nguon(hop_le: list) -> list:
    """Dựng danh sách nguồn (kèm thời lượng đo thật) từ các đường dẫn đã lọc."""
    def _do_dai(cac: list) -> dict:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as tp:
            return dict(zip(cac, tp.map(YM.thoi_luong, cac)))

    dai = await run_in_threadpool(_do_dai, hop_le) if hop_le else {}
    return [{
        "nguon": "tep_may",
        "id": "",
        "ten": os.path.basename(p),
        "path": p,
        "kich_thuoc": os.path.getsize(p),
        "thoi_luong": round(dai.get(p, 0.0), 2),
        "ngon_ngu": "vi",
        "kenh": "",
        "phong_cach": "",
    } for p in hop_le]


class DuongDanRequest(BaseModel):
    """Danh sách đường dẫn video ĐÃ BIẾT — thư mục theo dõi đưa sang."""

    paths: list[str] = []


@router.post("/nguon/tu-duong-dan")
async def nguon_tu_duong_dan(d: DuongDanRequest = Body(...)):
    """Dựng nguồn từ đường dẫn có sẵn, không mở hộp thoại chọn tệp.

    VÌ SAO TÁCH RA: thư mục theo dõi đã biết chính xác tệp nào mới, người dùng
    không phải đi chọn lại. Và vì mỗi lượt gọi chỉ nhận một NHÚM đường dẫn, màn
    hình đếm được "đang đọc 12/41 video" — thư mục 40-50 video mất cả chục giây
    đo thời lượng, không có số đếm thì trông như treo.
    """
    hop_le = [
        p for p in (d.paths or [])
        if os.path.exists(p) and os.path.splitext(p)[1].lower() in DUOI_VIDEO
    ]
    return {"videos": await _dung_nguon(hop_le)}


# --------------------------------------------------------------------------- #
#  NHỜ AI VIẾT TIÊU ĐỀ / MÔ TẢ / THẺ
# --------------------------------------------------------------------------- #
class AIRequest(BaseModel):
    #  Một trong hai: dự án lồng tiếng hoặc dự án chi tiết. Có thì backend TỰ lấy
    #  lời thoại làm ngữ cảnh — người dùng khỏi phải chép dán.
    video_id: str | None = None
    project_id: str | None = None
    #  Dự án "Sáng tạo": lấy tiêu đề + các câu tiếng đích của bài làm ngữ cảnh.
    sangtao_id: str | None = None
    chu_de: str = ""
    noi_dung: str = ""
    kenh: str = ""
    phong_cach: str = ""
    ngon_ngu: str = "vi"
    tu_khoa: str = ""
    #  "ngan" (Shorts) · "dai" · "" = tự đoán theo ``thoi_luong``. App này dùng cho
    #  CẢ HAI dạng, nên không chốt cứng bên nào.
    dang: str = ""
    thoi_luong: float = 0
    model: str = ""
    #  Khác rỗng = dùng nguyên văn prompt này thay cho prompt dựng sẵn.
    prompt_tay: str = ""


def _tom_tat_du_an(project_dir: str) -> str:
    """Phần TÓM TẮT đã lưu ở ``ai_context.json`` của dự án Chi tiết, hoặc ""."""
    try:
        from services.review_advanced_ai import read_film_context

        return ((read_film_context(project_dir) or {}).get("summary") or "").strip()
    except Exception:  # noqa: BLE001 — thiếu tóm tắt không được làm hỏng danh sách
        return ""


def _bai_da_soan(meta: dict) -> dict | None:
    """Phần bài đăng lô đã soạn sẵn, dọn đúng hình dạng màn soạn bài cần."""
    yt = (meta or {}).get("youtube_meta") or {}
    #  BỎ QUA BẢN RỖNG TUẾCH đã lỡ lưu. Có lúc AI chép nguyên cái mẫu JSON trong
    #  prompt ra ("…"), và bản cũ lưu thẳng — màn hình khoe "đã soạn · 1 tiêu đề"
    #  trong khi tiêu đề chỉ là ba dấu chấm. Nay coi như CHƯA soạn để người dùng
    #  bấm soạn lại, thay vì mang cái đó đi đăng.
    tieu_de = [t for t in (yt.get("tieu_de") or []) if YM._co_chu_that(t)]
    if not tieu_de:
        return None
    anh = (yt.get("thumbnail_path") or "").strip()
    return {
        "tieu_de": tieu_de,
        "mo_ta": yt.get("mo_ta") or "",
        "the": yt.get("the") or [],
        "hashtag": yt.get("hashtag") or [],
        "hook_anh": yt.get("hook_anh") or [],
        #  Ảnh chỉ trả khi TỆP CÒN: bản nháp cũ trỏ vào ảnh đã dọn thì màn soạn bài
        #  hiện một ô vỡ, khó hiểu hơn là không có ảnh.
        "thumbnail_path": anh if anh and os.path.exists(anh) else "",
        "thumbnail_goc": (yt.get("thumbnail_goc") or ""),
    }


def _ngu_canh(d: AIRequest, db: Session) -> dict:
    """Gom ngữ cảnh cho prompt; tự lấy lời thoại nếu người dùng chưa dán vào."""
    chu_de, noi_dung = d.chu_de, d.noi_dung
    if not noi_dung and d.sangtao_id:
        from services import sangtao_du_an as SDA
        from services.sangtao_ruby import bo_ruby

        try:
            du_an = SDA.doc(d.sangtao_id)
        except KeyError:
            du_an = {}
        sb = du_an.get("storyboard") or {}
        chu_de = chu_de or SDA.tieu_de_video(du_an)
        #  Các câu TIẾNG ĐÍCH của bài (bỏ cách đọc [kana]) — thứ nói lên video thực sự nói gì.
        cau = [bo_ruby(str(ln.get("text") or "")) for c in (sb.get("scenes") or [])
               for ln in (((c.get("visual") or {}).get("data") or {}).get("lines") or [])]
        noi_dung = " ".join(x for x in cau[:80] if x)
    if not noi_dung and d.video_id:
        v = db.query(Video).filter(Video.id == d.video_id).first()
        if v:
            chu_de = chu_de or (v.title or "")
            cau = (
                db.query(SubtitleSegment)
                .filter(SubtitleSegment.video_id == d.video_id)
                .order_by(SubtitleSegment.index)
                .limit(80)
                .all()
            )
            noi_dung = " ".join([(c.target_text or c.source_text or "").strip() for c in cau])
    if not noi_dung and d.project_id:
        import json

        pdir = os.path.join(review_service.projects_root, d.project_id)
        meta = review_service.get_metadata(d.project_id) or {}
        chu_de = chu_de or (meta.get("title") or "")
        try:
            with open(os.path.join(pdir, "b_chunks.json"), "r", encoding="utf-8") as f:
                b = json.load(f)
            noi_dung = " ".join([str(x.get("b_text") or "").strip() for x in b[:80]])
        except (OSError, ValueError):
            noi_dung = ""
    return {
        "chu_de": chu_de, "noi_dung": noi_dung, "kenh": d.kenh,
        "phong_cach": d.phong_cach, "ngon_ngu": d.ngon_ngu, "tu_khoa": d.tu_khoa,
        "dang": d.dang, "thoi_luong": d.thoi_luong,
    }


class SoanChoDuAnRequest(BaseModel):
    """Soạn bài đăng cho MỘT dự án Chi tiết, ngay trong bảng Chuẩn bị."""

    project_id: str
    #: Nguyên văn câu trả lời của AI ngoài. Có thì KHÔNG gọi DeepSeek, chỉ dọn và lưu.
    noi_dung: str = ""


def _nap_du_an(project_id: str):
    from services import review_advanced_engine as ADV

    try:
        p = ADV.load_project(project_id, migrate=False)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án.", "Project not found."))
    if not (p.get("b_chunks") or []):
        raise HTTPException(
            status_code=400,
            detail=L("Dự án chưa có bản dịch — dịch xong mới soạn được bài đăng.", "The project has no translation yet — translate it first before composing a post."),
        )
    return ADV, p


@router.post("/du-an/prompt-soan")
async def prompt_soan_du_an(d: SoanChoDuAnRequest = Body(...)):
    """Prompt soạn bài của một dự án — để tự chạy AI ngoài rồi dán kết quả về."""
    _ADV, p = _nap_du_an(d.project_id)
    return {"prompt": YM.dung_prompt(YM.ngu_canh_tu_du_an(p))}


@router.post("/du-an/soan")
async def soan_cho_du_an(d: SoanChoDuAnRequest = Body(...)):
    """Soạn (hoặc nhận bản dán tay) tiêu đề/mô tả/thẻ/hook rồi LƯU vào dự án.

    VÌ SAO CÓ CỬA NÀY BÊN CẠNH BƯỚC CỦA LÔ: không phải ai cũng chạy qua lô. Người
    dùng mở một dự án, dịch xong, muốn soạn luôn bài đăng ngay tại bảng Chuẩn bị —
    và câu hook soạn ra còn được tab "Chèn chữ" bày ra để đắp vào đầu video.

    Kết quả nằm cùng một chỗ với đường của lô (``metadata.json`` → ``youtube_meta``)
    nên tab đăng YouTube đọc được, không phân biệt soạn từ đâu.
    """
    import time as _t

    ADV, p = _nap_du_an(d.project_id)

    if (d.noi_dung or "").strip():
        #  Dán tay: dọn bằng ĐÚNG hàm mà đường tự động dùng.
        try:
            kq = YM.chuan_hoa(YM._boc_json(d.noi_dung))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        from services import deepseek_service as DS
        from services.config_service import get_config_service

        if not DS.is_configured():
            raise HTTPException(
                status_code=400,
                detail=L("Chưa cấu hình API key của bộ AI đang chọn (DeepSeek / Gemini) — vào Cài đặt nhập key, hoặc "
                         "chép prompt rồi tự chạy AI ngoài và dán kết quả về.",
                         "The API key of the selected AI (DeepSeek / Gemini) is not configured — go to Settings to enter the key, or "
                         "copy the prompt, run it in an external AI and paste the result back."),
            )
        model = get_config_service().ai_model_mac_dinh()
        try:
            kq = await run_in_threadpool(
                YM.sinh_metadata, YM.ngu_canh_tu_du_an(p), model, ""
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400,
                                detail=getattr(e, "user_message", "") or str(e))

    if not (kq.get("tieu_de") or []):
        raise HTTPException(status_code=400,
                            detail=L("Không nhận được tiêu đề nào — thử lại.", "No titles were received — try again."))

    cu_meta = p["meta"].get("youtube_meta") or {}
    p["meta"]["youtube_meta"] = {**cu_meta, **kq, "luc": _t.time()}
    await run_in_threadpool(ADV.save_meta, p["dir"], p["meta"])
    return kq


@router.get("/du-an/{project_id}/bai-soan")
def bai_soan_cua_du_an(project_id: str):
    """Bài đăng đã soạn của một dự án (rỗng nếu chưa soạn)."""
    from services import review_advanced_engine as ADV

    try:
        p = ADV.load_project(project_id, migrate=False)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án.", "Project not found."))
    return {"bai": _bai_da_soan(p.get("meta") or {}),
            "co_ban_dich": bool(p.get("b_chunks"))}


@router.post("/ai/prompt")
def xem_prompt(d: AIRequest = Body(...), db: Session = Depends(get_db)):
    """Trả về ĐÚNG prompt mà app sẽ gửi đi.

    Chế độ "tự chạy AI ngoài": người dùng chép prompt này sang ChatGPT/Gemini rồi
    dán JSON trả về. Lấy từ đây chứ không chép lại ở giao diện — một bản prompt
    duy nhất thì không bao giờ có chuyện hai bên lệch nhau.
    """
    return {"prompt": YM.dung_prompt(_ngu_canh(d, db))}


@router.post("/ai/metadata")
async def ai_metadata(d: AIRequest = Body(...), db: Session = Depends(get_db)):
    from services import deepseek_service as DS

    if not DS.is_configured():
        raise HTTPException(
            status_code=400,
            detail=L("Chưa có API key của bộ AI đang chọn (DeepSeek / Gemini) — vào Cài đặt để nhập, hoặc dùng nút "
                     "“Chép prompt” rồi tự chạy ở AI khác.",
                     "There is no API key for the selected AI (DeepSeek / Gemini) — go to Settings to enter it, or use the "
                     "“Copy prompt” button and run it in another AI."),
        )
    nc = _ngu_canh(d, db)
    try:
        return await run_in_threadpool(YM.sinh_metadata, nc, d.model, d.prompt_tay)
    except DS.DeepSeekError as e:
        #  ``str(e)`` tra ve ``detail`` — nguyen van HTTP + JSON cua DeepSeek
        #  ("HTTP 402 Payment Required: {\"error\":{\"message\":\"Insufficient
        #  Balance\"...}}"). ``DeepSeekError`` da mang san cau tieng Viet trong
        #  ``user_message``; moi noi khac trong app deu doc truong do, rieng cho
        #  nay quen nen nem nguyen khoi JSON len man hinh nguoi dung.
        raise HTTPException(status_code=400, detail=e.user_message or str(e))
    except ValueError as e:
        #  AI trả về thứ không phải JSON — nói thẳng thay vì 500 khó hiểu.
        raise HTTPException(
            status_code=400,
            detail=f"Không đọc được câu trả lời của AI ({e}). Thử lại hoặc đổi model.",
        )


# --------------------------------------------------------------------------- #
#  ẢNH ĐẠI DIỆN
# --------------------------------------------------------------------------- #
class KhungRequest(BaseModel):
    file_path: str
    giay: float = 0.0
    #  "ngang" (1280×720) · "doc" (720×1280, cho Shorts) · "" = tự đoán theo hình
    #  dạng video. App này chủ yếu làm Shorts nên tự đoán là mặc định đúng.
    khung: str = ""


class ChuRequest(BaseModel):
    anh_goc: str
    cac_dong: list[str] = []
    kieu: str = "vang_vien_den"
    font: str = ""
    vi_tri: str = "duoi"
    co_chu: int = 100
    khung: str = ""
    #  Tâm khối chữ theo phần của khung (0..1) khi người dùng KÉO chữ. Bỏ trống thì
    #  dùng ba nấc ``vi_tri`` như cũ.
    x: float | None = None
    y: float | None = None


class AnhMayRequest(BaseModel):
    """Không có trường nào — hộp thoại chọn tệp mở ở phía máy chủ."""


class MotBaiChu(BaseModel):
    """Một bài trong lượt đắp chữ hàng loạt."""

    key: str = ""
    #  Ảnh nền của RIÊNG bài này. Chạy hàng loạt cố ý KHÔNG đụng tới ảnh nền —
    #  mỗi video có khung mặc định riêng, ép chung một ảnh là hỏng hết.
    anh_goc: str
    cac_dong: list[str] = []


class ChuHangLoatRequest(BaseModel):
    """Một KIỂU CHỮ, đắp lên nhiều ảnh nền khác nhau."""

    bai: list[MotBaiChu] = []
    kieu: str = "vang_vien_den"
    font: str = ""
    co_chu: int = 100
    x: float | None = None
    y: float | None = None
    khung: str = ""


#  DANH MỤC của YouTube. Danh sách CỐ ĐỊNH chứ không gọi ``videoCategories.list``:
#  gọi API thì tốn hạn mức, cần mạng, và cần chọn sẵn quốc gia — trong khi mã danh
#  mục này giống nhau ở mọi nước và nhiều năm nay không đổi. Đây là những mã CHO
#  PHÉP GÁN KHI ĐĂNG (một số mã như 21 “Videoblogging” chỉ đọc được, gán vào là lỗi).
DANH_MUC = [
    ("22", "Con người & Blog"), ("24", "Giải trí"), ("27", "Giáo dục"),
    ("26", "Hướng dẫn & Phong cách"), ("23", "Hài"), ("1", "Phim & Hoạt hình"),
    ("10", "Âm nhạc"), ("20", "Trò chơi"), ("17", "Thể thao"),
    ("19", "Du lịch & Sự kiện"), ("28", "Khoa học & Công nghệ"),
    ("25", "Tin tức & Chính trị"), ("15", "Thú cưng & Động vật"),
    ("2", "Xe cộ"), ("29", "Phi lợi nhuận & Hoạt động xã hội"),
]


@router.get("/danh-muc")
def danh_muc():
    _en = {
        "22": "People & Blogs", "24": "Entertainment", "27": "Education",
        "26": "Howto & Style", "23": "Comedy", "1": "Film & Animation",
        "10": "Music", "20": "Gaming", "17": "Sports",
        "19": "Travel & Events", "28": "Science & Technology",
        "25": "News & Politics", "15": "Pets & Animals",
        "2": "Autos & Vehicles", "29": "Nonprofits & Activism",
    }
    return {"danh_muc": [{"ma": m, "ten": L(t, _en.get(m, t))} for m, t in DANH_MUC]}


@router.get("/thumbnail/kieu")
def kieu_chu():
    return {
        "kieu": [{"ma": k, "ten": v["ten"], "chu": v["chu"], "vien": v["vien"],
                  "nen": v["nen"]} for k, v in YM.KIEU_CHU.items()],
        #  HẾT font đóng gói chứ không chỉ tám font gợi ý: cả 43 tệp đã đo là đủ
        #  dấu tiếng Việt, giữ lại tám cái cho riêng mình chẳng để làm gì.
        "fonts": YM.fonts_tat_ca(),
        "vi_tri": [{"ma": "tren", "ten": L("Trên", "Top")}, {"ma": "giua", "ten": L("Giữa", "Middle")},
                   {"ma": "duoi", "ten": L("Dưới", "Bottom")}],
        #  Hai cỡ khung. Tài liệu ``thumbnails.set`` KHÔNG ràng buộc tỷ lệ, chỉ
        #  giới hạn dung lượng và định dạng — nên khung dọc gửi lên được, và với
        #  Shorts thì đó mới là khung khớp với thứ người xem nhìn thấy.
        "khung": [{"ma": k, "ten": YM.TEN_KHUNG[k], "rong": v[0], "cao": v[1]}
                  for k, v in YM.KHUNG.items()],
    }


def _anh_moi(hau_to: str) -> str:
    """Tên tệp ảnh MỚI, không bao giờ trùng.

    MỐC MILI GIÂY KHÔNG ĐỦ. Lượt đắp chữ hàng loạt chạy 4 luồng song song, PIL vẽ
    một ảnh chỉ mất vài chục mili giây — đo thật với 15 bài thì ra 13 tên khác
    nhau, tức là HAI CẶP bài dùng chung một tệp. Không có lỗi nào hiện ra: hai
    video chỉ lặng lẽ mang cùng một ảnh bìa, mà ảnh đó lại là của bài kia.

    Thêm 6 ký tự ngẫu nhiên. Vẫn giữ mốc thời gian ở đầu để tên tệp sắp theo thứ
    tự thời gian, tiện khi phải mò trong thư mục.
    """
    dau = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    return os.path.join(YM.thu_muc_anh(), f"{dau}_{hau_to}.jpg")


@router.post("/thumbnail/mac-dinh")
async def thumbnail_mac_dinh(d: KhungRequest = Body(...)):
    """Khung mặc định = khung đầu tiên KHÔNG tối thui (bỏ qua fade-in đầu phim)."""
    if not os.path.exists(d.file_path):
        raise HTTPException(status_code=400, detail=L("Không còn tệp video.", "The video file is gone."))
    try:
        return await run_in_threadpool(
            YM.khung_mac_dinh, d.file_path, _anh_moi("goc"), d.khung
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/thumbnail/khung")
async def thumbnail_khung(d: KhungRequest = Body(...)):
    if not os.path.exists(d.file_path):
        raise HTTPException(status_code=400, detail=L("Không còn tệp video.", "The video file is gone."))
    try:
        #  Chưa nói rõ thì theo hình dạng video — kéo thanh thời gian không được
        #  làm đổi tỷ lệ ảnh bìa đang làm dở.
        khung = d.khung or YM.doan_khung(*YM.kich_thuoc_video(d.file_path))
        p = await run_in_threadpool(
            YM.lay_khung, d.file_path, d.giay, _anh_moi("goc"), khung
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": p, "giay": d.giay, "khung": khung}


@router.post("/thumbnail/chu-hang-loat")
async def thumbnail_chu_hang_loat(d: ChuHangLoatRequest = Body(...)):
    """Đắp CÙNG MỘT KIỂU CHỮ lên ảnh bìa của nhiều bài, mỗi bài một câu riêng.

    VÌ SAO CÓ CỬA NÀY: soạn 90 bài mà mở hộp ảnh bìa từng cái để chọn lại font,
    màu, cỡ là công việc vô nghĩa — kiểu chữ của một kênh vốn giống nhau, chỉ
    câu chữ là khác. Ảnh NỀN vẫn là khung mặc định của từng video, không ép chung.

    Bài nào hỏng thì BÁO RIÊNG bài đó chứ không đổ cả lượt: 90 bài mà một tệp ảnh
    bị xoá giữa chừng, ném nguyên lượt đi là người dùng mất cả 89 bài còn lại.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _mot(b: MotBaiChu) -> dict:
        try:
            if not b.anh_goc or not os.path.exists(b.anh_goc):
                return {"key": b.key, "loi": L("Không còn ảnh nền của bài này.", "The background image of this post is gone.")}
            #  Câu này có còn chữ nào font vẽ được không? Hỏi TRƯỚC để còn báo
            #  lại cho người dùng, chứ ảnh trơn mà không nói gì thì họ tưởng app
            #  bỏ sót bài đó.
            con_chu = any(YM.loc_ve_duoc(str(c), d.font) for c in (b.cac_dong or []))
            p = YM.dap_chu(
                b.anh_goc, _anh_moi("chu"), b.cac_dong,
                d.kieu, d.font, "duoi", d.co_chu, d.x, d.y, d.khung,
            )
            return {"key": b.key, "path": p,
                    "bo_chu": bool(b.cac_dong) and not con_chu}
        except (OSError, ValueError) as e:
            return {"key": b.key, "loi": str(e)}

    def _chay() -> list:
        #  PIL vẽ bằng CPU nên chạy song song có lợi thật; 4 luồng là đủ để không
        #  giành hết máy với lượt đẩy video có thể đang chạy cùng lúc.
        with ThreadPoolExecutor(max_workers=4) as tp:
            return list(tp.map(_mot, d.bai))

    ra = await run_in_threadpool(_chay)
    return {
        "ket_qua": ra,
        "xong": sum(1 for r in ra if r.get("path")),
        #  Bao nhiêu bài ra ảnh TRƠN vì font không có ký tự của thứ tiếng đó.
        "bo_chu": sum(1 for r in ra if r.get("bo_chu")),
    }


#  Đuôi tệp -> kiểu nội dung, để trình duyệt biết đường giải mã.
_KIEU_VIDEO = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
}

@router.get("/video")
async def phat_video(path: str, request: Request):
    """Phát video nguồn để người dùng XEM và tự chọn khung — CÓ hỗ trợ Range.

    VÌ SAO KHÔNG DÙNG ``/videos/file``: cửa đó trả ``FileResponse`` thường, không
    xử lý ``Range``. Xem một mạch từ đầu thì được, nhưng ở đây người dùng phải TUA
    liên tục để tìm khung đẹp — thiếu Range là thanh tua không nhảy được.

    Tệp 500MB cũng không tải hết về: trình duyệt xin đúng đoạn nó cần.

    AI ĐƯỢC PHÉP GỌI CỬA NÀY: ``local_only_guard`` trong ``main.py`` đã gác cho
    TOÀN BỘ app — Host phải là localhost (chặn DNS-rebinding) và Origin, nếu có,
    phải là localhost / null / file:// (chặn trang web ngoài). Cửa này không tự
    dựng thêm hàng rào riêng, có hai lý do:

    · Đã thử và HỎNG. Bản đầu chặn bằng danh sách đường dẫn ("phải nằm trong thư
      mục app, hoặc là tệp người dùng vừa chọn trong phiên này") — nó chặn nhầm
      chính người dùng: chọn 92 tệp hôm nay, soạn dở, hôm sau mở app lên thì bản
      nháp vẫn còn (nằm trong localStorage của giao diện) mà danh sách bên máy chủ
      đã rỗng. Bản sau chặn bằng Origin riêng, chặt hơn middleware — nhưng nó từ
      chối ``Origin: null``, mà đó đúng là thứ renderer ``file://`` của bản đóng
      gói gửi lên, tức là sẽ làm hỏng bản cài.
    · Khác chính sách chung là một cái bẫy. ``/videos/file`` và mọi cửa phục vụ
      tệp khác đều dựa vào đúng middleware đó. Một cửa đi luật riêng thì người sửa
      sau phải đoán xem luật nào đang có hiệu lực.

    Còn lại hai điều kiện rẻ và đáng giữ: phải là TỆP VIDEO (không biến cửa này
    thành cửa đọc mọi loại tệp), và phải còn trên đĩa.
    """
    duoi = os.path.splitext(path or "")[1].lower()
    if duoi not in _KIEU_VIDEO:
        raise HTTPException(status_code=400, detail=L("Không phải tệp video.", "Not a video file."))
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=L("Không còn tệp video.", "The video file is gone."))
    from routes.review_advanced_routes import _range_response

    return _range_response(path, request, _KIEU_VIDEO[duoi])


class XuatAnhBiaRequest(BaseModel):
    """Xuất ảnh bìa của cả loạt ra một thư mục để tự thay tay trong YouTube Studio."""

    class Mot(BaseModel):
        #: Tiêu đề bài — dùng đặt tên tệp cho dễ tìm đúng video trong Studio.
        ten: str = ""
        path: str = ""

    bai: list[Mot] = []
    #: Thư mục đích. Bỏ trống thì mở hộp thoại chọn thư mục.
    thu_muc: str = ""


#: Ký tự Windows cấm trong tên tệp. Tiêu đề video thì đầy dấu ? : / — không lọc là
#: ghi tệp hỏng và cả lượt xuất chết giữa chừng.
_CAM_TEN = r'<>:"/\|?*'


def _ten_an_toan(s: str, tran: int = 60) -> str:
    ra = "".join(" " if c in _CAM_TEN else c for c in (s or "")).strip()
    ra = " ".join(ra.split())
    return ra[:tran].strip() or "anh-bia"


@router.post("/thumbnail/xuat-thu-muc")
async def xuat_anh_bia(d: XuatAnhBiaRequest = Body(...)):
    """Chép ảnh bìa của cả loạt vào MỘT thư mục, đặt tên theo tiêu đề bài.

    VÌ SAO CẦN: YouTube không cho đặt ảnh bìa của Shorts qua API (chỉ làm tay được
    trong Studio trên máy tính). Ảnh app dựng ra vẫn dùng được — chỉ là người dùng
    phải tự tải lên. Bắt họ mở từng bài, bấm chuột phải lưu ảnh, 90 lần thì không
    ai làm. Xuất một lượt ra một thư mục, đặt tên theo số thứ tự + tiêu đề, thì mở
    Studio lên thay lần lượt là xong.
    """
    import shutil

    from utils.native_dialogs import ask_directory

    thu_muc = (d.thu_muc or "").strip()
    if not thu_muc:
        thu_muc = await run_in_threadpool(
            ask_directory, L("Chọn thư mục để lưu ảnh bìa", "Choose the folder to save the thumbnails")
        )
    if not thu_muc:
        return {"thu_muc": "", "da_xuat": 0, "bo_qua": 0}
    if not os.path.isdir(thu_muc):
        raise HTTPException(status_code=400, detail=L("Thư mục không tồn tại.", "The folder does not exist."))

    da, bo = 0, 0
    for i, b in enumerate(d.bai or [], start=1):
        nguon = (b.path or "").strip()
        if not nguon or not os.path.exists(nguon):
            bo += 1
            continue
        duoi = os.path.splitext(nguon)[1] or ".jpg"
        #  SỐ THỨ TỰ ĐỨNG ĐẦU: thư mục xếp theo tên sẽ ra ĐÚNG thứ tự trên màn soạn
        #  bài, nên mở Studio thay lần lượt từ trên xuống là khớp, không phải dò.
        dich = os.path.join(thu_muc, f"{i:02d} - {_ten_an_toan(b.ten)}{duoi}")
        try:
            shutil.copyfile(nguon, dich)
            da += 1
        except OSError as e:  # noqa: PERF203
            logger.warning(f"[youtube] Không chép được ảnh bìa {nguon}: {e}")
            bo += 1
    return {"thu_muc": thu_muc, "da_xuat": da, "bo_qua": bo}


@router.post("/thumbnail/anh-may")
async def thumbnail_anh_may(khung: str = "ngang"):
    """Chọn một ẢNH có sẵn trong máy làm ảnh bìa (thay cho khung cắt từ video)."""
    from utils.native_dialogs import ask_open_filenames

    def _mo():
        return ask_open_filenames(
            title=L("Chọn ảnh làm ảnh đại diện", "Choose an image as the thumbnail"),
            filetypes=[(L("Ảnh", "Images"), "*.jpg *.jpeg *.png *.webp *.bmp"), (L("Tất cả", "All files"), "*.*")],
        )

    duong_dan = await run_in_threadpool(_mo)
    #  Hộp thoại cho chọn nhiều, nhưng ảnh bìa chỉ có một — lấy tệp đầu.
    nguon = (duong_dan or [None])[0]
    if not nguon:
        return {"path": ""}
    try:
        p = await run_in_threadpool(YM.anh_tu_tep, nguon, _anh_moi("goc"), khung)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": p, "ten": os.path.basename(nguon)}


@router.post("/thumbnail/chu")
async def thumbnail_chu(d: ChuRequest = Body(...)):
    """Đắp chữ lên KHUNG GỐC, luôn xuất ra tệp MỚI.

    Không ghi đè tệp cũ: giao diện xem trước ảnh qua URL, ghi đè cùng đường dẫn
    thì trình duyệt giữ bản cũ trong bộ nhớ đệm và người dùng tưởng chỉnh sửa
    không ăn. Tên tệp mang mốc thời gian nên mỗi lần chỉnh là một URL khác.
    """
    if not os.path.exists(d.anh_goc):
        raise HTTPException(status_code=400, detail=L("Không còn ảnh gốc — lấy lại khung hình.", "The original image is gone — grab the frame again."))
    try:
        p = await run_in_threadpool(
            YM.dap_chu, d.anh_goc, _anh_moi("chu"), d.cac_dong,
            d.kieu, d.font, d.vi_tri, d.co_chu, d.x, d.y, d.khung,
        )
    except OSError as e:
        raise HTTPException(status_code=400, detail=L(f"Không tạo được ảnh: {e}", f"Could not create the image: {e}"))
    return {"path": p, "kich_thuoc": os.path.getsize(p)}
