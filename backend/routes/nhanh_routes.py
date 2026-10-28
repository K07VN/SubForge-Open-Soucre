"""LÀM NHANH — dán link (Douyin / YouTube…) hoặc chọn một video trong máy, app tự ra video lồng tiếng.

Người dùng (09-10-2026): người mới vào app phải THẤY ĐƯỢC KẾT QUẢ ngay, chưa cần hiểu cấu hình.
Bản chất: một LÔ ẩn tên "Làm nhanh" (``batch["nhanh"] = True``) dùng chung cho mọi lần dán, mỗi
lần dán thêm một DỰ ÁN CHI TIẾT với bộ cấu hình chuẩn (``CAU_HINH_LO_NHANH`` + ``DUNG_HINH_NHANH``,
chép từ dự án mẫu của chủ app ngày 08-10-2026) rồi chạy chuỗi: Lấy phụ đề → Dịch → Sinh giọng →
Dồn → Tách giọng → Xuất video. Mọi thứ đi qua đúng bộ chạy của Chạy hàng loạt (``batch_routes``),
không có đường xử lý thứ hai; người dùng mở dự án trong Chi tiết để sửa tiếp như thường.

Điều kiện: Whisper (cỡ nào cũng được), VieNeu (giọng Ngọc Huyền) và Demucs (tách giọng) đã tải.
Khoá AI KHÔNG bắt buộc: chưa có thì bước Dịch dừng lại cho người dùng dán bản dịch (copy prompt
đem ra ChatGPT / Gemini miễn phí) hoặc nhập khoá rồi chạy tiếp. Thời lượng: xem ``GIOI_HAN_GIAY`` /
``KHUYEN_NGHI_GIAY`` — đây là bản trải nghiệm nhanh, phim dài đi đường Chi tiết / Lô.

Bước Dồn ở chế độ này KHÔNG BAO GIỜ chặn (``fit_mode = "auto"`` + ``fit_tu_tiep``): chọn được
phương án nhẹ thì áp, không thì ghi chú rồi đi tiếp — không nhờ AI rút gọn, không hỏi người dùng.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from routes import batch_routes as BR
from routes.auth_routes import verify_license
from services import batch_store as BS
from utils.i18n import L

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nhanh", tags=["nhanh"], dependencies=[Depends(verify_license)])

#: Thời lượng (người dùng chốt 09-10-2026): KHUYẾN NGHỊ dưới 30 phút; TỆP TRONG MÁY dài hơn 1 giờ thì từ chối; video TẢI TỪ LINK
#: (``tu_link``) dài hơn 1 giờ vẫn nhận — độ dài quét từ link có thể sai/thiếu, chấp nhận.
GIOI_HAN_GIAY = 3600
KHUYEN_NGHI_GIAY = 1800
#: Giọng đọc của chế độ nhanh — người dùng chốt Ngọc Huyền (bản clone đóng gói).
GIONG_NHANH = "vieneu-clone:Ngọc Huyền Pro Vbee"
#: Các bước chạy (theo thứ tự ``BS.STEPS``) — KHÔNG soạn bài đăng, KHÔNG xuất đoạn.
CHUOI_NHANH = ["sub", "translate", "voice", "fit", "separate", "export"]
#: Tên lô ẩn.
TEN_LO = "Làm nhanh"

#: CẤU HÌNH LÔ CHUẨN — chép từ hai dự án Douyin của chủ app (08-10-2026). Khoá nào không ghi
#: thì lấy ``BS.DEFAULT_CONFIG``.
CAU_HINH_LO_NHANH = {
    "source_language": "auto",          # Whisper tự nhận ngôn ngữ — dán link gì cũng chạy
    "target_language": "vi",
    "subtitle_mode": "whisper",
    "voice": GIONG_NHANH,
    "voice_mode": "chung",
    "fit_mode": "auto",                 # tự chọn phương án nhẹ…
    "fit_tu_tiep": True,                # …không chọn được thì vẫn đi tiếp, không hỏi
    "cho_tran_giong": True,
    "tran_giong": 0.85,
    "tach_cau_goc": True,
    "tach_cau_dich": True,
    "script_kind": "dubbing",
    "translate_engine": "ai",           # đổi sang "manual" lúc chạy nếu chưa có khoá AI
    "speech_style": "auto",
    "audio_source": "lo",
    "separate_audio": True,
    "bg_volume": 1.0,
    "voice_bg_volume": 1.0,
    "vocals_volume": 0.0,
    "voice_vocals_volume": 0.0,
    "tts_volume": 2.0,
    "export_dir": "",
    "template_project": "",
}

#: DỰNG HÌNH CHUẨN (compositor_config của dự án): phụ đề preset "box-vang-den" chữ Mali 90 nền vàng,
#: chế độ cụm 15 chữ, khung theo nguồn, nền mờ, 1080p — y hệt dự án mẫu. Preset chỉ tồn tại ở
#: frontend (constants/subtitlePresets.ts) nên backend ghi thẳng bộ giá trị.
DUNG_HINH_NHANH = {
    "aspectRatio": "source",
    "customAspectW": 9,
    "customAspectH": 16,
    "cropFrame": {"x": 0, "y": 0, "w": 1, "h": 1},
    "frameBackground": {"type": "blur", "color": "#000000", "blur": 28, "image": ""},
    "resolution": "1080p",
    "mirror": False,
    "showSourceSubs": False,
    "subtitleStyle": {
        "mode": "chunked", "fontFamily": "Mali", "fontSize": 90, "bold": True, "italic": False,
        "uppercase": False, "outlineWidth": 0, "shadowDepth": 0,
        "primaryColor": "#111111", "secondaryColor": "#111111",
        "karaokeStyle": "active-only", "karaokeWindowSize": 3, "chunkWordCount": 15,
        "karaokeTickSound": True, "alignment": 2, "marginV": 100,
        "backBox": True, "backBoxColor": "#FFD400", "backBoxOpacity": 1,
        "backBoxPadX": 58, "backBoxPadY": 39, "backBoxRadius": 18, "fontFile": "",
        "posX": 0.022, "posY": 0.768, "boxW": 0.95, "boxH": 0.086,
        "enabled": True, "presetId": "box-vang-den",
    },
}


class BatDauRequest(BaseModel):
    #: Đường dẫn video trong máy (link thì FE tải trước bằng ``/videos/import-url/stream``).
    path: str
    title: str = ""
    #: Video vừa tải từ link (FE tải xong rồi gửi đường dẫn) -> không chặn theo độ dài.
    tu_link: bool = False


# --------------------------------------------------------------------------- #
#  Điều kiện
# --------------------------------------------------------------------------- #
def _kiem_model() -> dict:
    """Ba model bắt buộc có trên máy chưa."""
    from routes import model_routes as MR

    whisper = any(MR.check_whisper_downloaded(s) for s in ("base", "medium", "large-v3"))
    return {
        "whisper": whisper,
        "vieneu": MR.check_vieneu_downloaded(),
        "demucs": MR.check_demucs_downloaded(),
    }


def _co_khoa_ai() -> tuple:
    """(có khoá?, tên bộ AI mặc định)."""
    import services.deepseek_service as deepseek_service
    from services.ai_engines import ten_bo_ai
    from services.config_service import get_config_service

    bo = get_config_service().ai_provider_mac_dinh()
    try:
        return bool(deepseek_service.is_configured(bo)), ten_bo_ai(bo)
    except Exception:  # noqa: BLE001
        return False, ten_bo_ai(bo)


@router.get("/san-sang")
async def san_sang():
    """Máy đã đủ điều kiện làm nhanh chưa — FE hiện danh sách còn thiếu + nút tải hàng loạt."""
    models = await run_in_threadpool(_kiem_model)
    co_khoa, ten_ai = await run_in_threadpool(_co_khoa_ai)
    thieu = [k for k, v in models.items() if not v]
    return {
        "models": models,
        "thieu": thieu,
        "du": not thieu,
        #  Cỡ Whisper gợi ý tải cho người mới: base (nhỏ nhất, 140 MB) — đủ để trải nghiệm.
        "tai": [x for x in ("whisper_base", "vieneu", "demucs") if (x.split("_")[0] in thieu)],
        "ai_key": co_khoa,
        "dich": _cach_dich(_lo_nhanh()),
        "cau_hinh": _cau_hinh_nhanh(_lo_nhanh()),
        "ten_ai": ten_ai,
        "gioi_han_giay": GIOI_HAN_GIAY,
        "khuyen_nghi_giay": KHUYEN_NGHI_GIAY,
        "giong": GIONG_NHANH,
    }


# --------------------------------------------------------------------------- #
#  Lô ẩn
# --------------------------------------------------------------------------- #
#: Mã lô Làm nhanh đã tìm thấy — nhớ lại để mỗi nhịp hỏi trạng thái (2 s) không phải đọc LẠI MỌI tệp lô
#: (``BS.list_all`` nạp cả nhật ký 5000 dòng của từng lô; máy có vài chục lô là backend bận đều đều).
_LO_ID: str = ""


def _lo_nhanh() -> dict:
    """Lô "Làm nhanh" dùng chung — tạo lần đầu với cấu hình chuẩn; lần sau giữ cấu hình người
    dùng đã chỉnh ở trang Chạy hàng loạt (họ chỉnh được, chỉ không bắt buộc phải chỉnh)."""
    global _LO_ID
    if _LO_ID:
        b = BS.load(_LO_ID)
        if b and b.get("nhanh"):
            return b
        _LO_ID = ""                       # lô bị xoá -> tìm / tạo lại
    for b in BS.list_all():
        if b.get("nhanh"):
            _LO_ID = b["id"]
            return b
    b = BS.create(TEN_LO)
    b["nhanh"] = True
    b["config"] = {**b.get("config", {}), **CAU_HINH_LO_NHANH}
    b = BS.save(b)
    _LO_ID = b["id"]
    return b


#: Cách dịch người dùng chọn trong khung Làm nhanh (``b["nhanh_dich"]``): "auto" = có khoá AI thì AI, không thì dán prompt;
#: "ai" = ưu tiên AI (chưa có khoá thì vẫn dừng cho nhập khoá); "prompt" = LUÔN dừng cho copy prompt / dán bản dịch (miễn phí).
CACH_DICH = ("auto", "ai", "prompt")


def _cach_dich(b: dict) -> str:
    v = str((b or {}).get("nhanh_dich") or "auto")
    return v if v in CACH_DICH else "auto"


#: CẤU HÌNH của Làm nhanh (người dùng 09-10-2026): "chuan" = bộ cấu hình gốc của Làm nhanh (``CAU_HINH_LO_NHANH`` + ``DUNG_HINH_NHANH``);
#: "chi_tiet" = theo "Làm mặc định cho dự án mới" của Edit chi tiết (ngôn ngữ, giọng, âm lượng, phụ đề, khung, logo…).
CACH_CAU_HINH = ("chuan", "chi_tiet")

#: Khoá lô mà hai chế độ cùng đặt lại MỖI lượt (để chế độ này không để sót giá trị của chế độ kia trong cấu hình lô dùng chung).
_KHOA_THEO_CHE_DO = (
    "source_language", "target_language", "subtitle_mode", "script_kind", "speech_style", "brand_name",
    "cho_tran_giong", "tran_giong", "tach_cau_goc", "tach_cau_dich", "separate_audio", "bg_volume",
    "voice_bg_volume", "vocals_volume", "voice_vocals_volume", "tts_volume", "audio_source", "voice", "voice_mode",
)


def _cau_hinh_nhanh(b: dict) -> str:
    v = str((b or {}).get("nhanh_cau_hinh") or "chuan")
    return v if v in CACH_CAU_HINH else "chuan"


def _cfg_chuan() -> dict:
    """Cấu hình lô cho chế độ CHUẨN: về mặc định gốc rồi đè bộ chuẩn Làm nhanh."""
    return {**{k: BS.DEFAULT_CONFIG[k] for k in _KHOA_THEO_CHE_DO if k in BS.DEFAULT_CONFIG}, **CAU_HINH_LO_NHANH}


def _cfg_tu_meta(meta: dict) -> dict:
    """Cấu hình lô cho chế độ THEO MẶC ĐỊNH CHI TIẾT: đọc từ metadata dự án vừa tạo (đã áp "mặc định cho dự án mới").
    Bước Dồn vẫn KHÔNG chặn; âm thanh giữ theo từng dự án (``audio_source = "du_an"``)."""
    cfg = {k: BS.DEFAULT_CONFIG[k] for k in _KHOA_THEO_CHE_DO if k in BS.DEFAULT_CONFIG}
    for k in _KHOA_THEO_CHE_DO:
        if k in BS.DEFAULT_CONFIG and (meta or {}).get(k) is not None:
            cfg[k] = meta[k]
    cfg["voice"] = (meta or {}).get("target_voice") or GIONG_NHANH
    cfg["voice_mode"] = "chung"
    cfg["audio_source"] = "du_an"
    cfg["fit_mode"] = "auto"
    cfg["fit_tu_tiep"] = True
    return cfg


def _chuoi_cho(b: dict) -> list:
    """Chuỗi bước của lô lúc này: bỏ Tách giọng nếu cấu hình hiện tại không bật tách giọng (mặc định Chi tiết của người dùng)."""
    tach = bool((b.get("config") or {}).get("separate_audio"))
    return [s for s in CHUOI_NHANH if tach or s != "separate"]


def _dat_nguon_dich(b: dict) -> dict:
    """Trước mỗi lượt chạy: đặt nguồn dịch theo lựa chọn của người dùng + khoá AI hiện có."""
    co_khoa, _ten = _co_khoa_ai()
    muon = "manual" if (_cach_dich(b) == "prompt" or not co_khoa) else "ai"
    if (b.get("config") or {}).get("translate_engine") != muon:
        b = BS.set_config(b, {"translate_engine": muon})
    return b


async def _ghi_dung_hinh(pid: str) -> None:
    """Ghi bộ dựng hình chuẩn vào compositor_config của dự án (đè lên phần tương ứng)."""
    from routes.review_routes import _review_compositor_config_path
    from services.compositor_utils import save_compositor_config
    from services.mau_cau_hinh import doc_cau_hinh_tho

    duong = _review_compositor_config_path(pid)
    cu = await run_in_threadpool(doc_cau_hinh_tho, duong)
    await run_in_threadpool(save_compositor_config, duong, {**(cu or {}), **DUNG_HINH_NHANH})


def _buoc_con_lai(item: dict, chuoi: list | None = None) -> list:
    """Các bước của chuỗi nhanh chưa xong cho dự án này (theo bảng trạng thái của lô)."""
    steps = item.get("steps") or {}
    return [s for s in (chuoi or CHUOI_NHANH) if (steps.get(s) or {}).get("status") not in ("done", "skipped")]


def _cho_dan(pid: str, item: dict) -> bool:
    """Đang chờ người dùng dán bản dịch: bước Dịch bị bỏ qua (tự dịch) mà dự án chưa có câu nào."""
    st = ((item.get("steps") or {}).get("translate") or {}).get("status")
    if st != "skipped":
        return False
    return BR._suy_trang_thai(pid).get("translate") != "done"


async def _chay(b: dict, pid: str) -> dict:
    """Chạy phần còn lại của chuỗi cho một dự án. Trả ``{"started": bool, "cho_dan": bool}``."""
    item = BS.find_item(b, pid) or {}
    if _cho_dan(pid, item):
        return {"started": False, "cho_dan": True}
    b = _dat_nguon_dich(b)
    #  Bước Dịch "bỏ qua" (tự dịch) mà nay đã có câu -> cho chạy lại để lô ghi "done" rồi mới sang
    #  Sinh giọng (chuỗi coi skipped là đủ điều kiện, nhưng Sinh giọng cần câu thật).
    con = _buoc_con_lai(item, _chuoi_cho(b))
    if "translate" not in con and (
        ((item.get("steps") or {}).get("translate") or {}).get("status") == "skipped"
    ):
        con = ["translate", *con]
    #  CHƯA CÓ BẢN DỊCH (nguồn dịch = dán tay) thì DỪNG chuỗi ở bước Dịch: chuỗi coi "bỏ qua" là đủ điều kiện nên nếu để nguyên,
    #  Sinh giọng chạy ngay và báo lỗi "chưa có lời bình" (người dùng 09-10-2026: chưa dịch thì chạy sinh giọng vội làm gì).
    if (b.get("config") or {}).get("translate_engine") == "manual" and BR._suy_trang_thai(pid).get("translate") != "done":
        con = [s for s in con if s in ("sub", "translate")]
    if not con:
        return {"started": False, "cho_dan": False}
    try:
        await BR.run_chain(b["id"], BR.RunChainRequest(steps=con, force=False, project_ids=[pid]))
    except HTTPException as e:
        if e.status_code == 400:            # không còn gì để chạy
            return {"started": False, "cho_dan": False}
        raise
    return {"started": True, "cho_dan": False}


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@router.post("/bat-dau")
async def bat_dau(data: BatDauRequest = Body(...)):
    """Tạo dự án Chi tiết với cấu hình chuẩn rồi chạy chuỗi. Trả ``{batch_id, project_id}``."""
    from services.video_service import VideoService

    path = (data.path or "").strip().strip('"')
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=400, detail=L("Không tìm thấy tệp video.", "Video file not found."))
    models = await run_in_threadpool(_kiem_model)
    thieu = [k for k, v in models.items() if not v]
    if thieu:
        raise HTTPException(
            status_code=409,
            detail=L("Máy chưa đủ model để làm nhanh: ", "This machine does not have enough models for Quick Mode: ") + ", ".join(
                {"whisper": "Whisper", "vieneu": "VieNeu", "demucs": "Demucs"}[k] for k in thieu
            ) + L(". Bấm “Tải tất cả” rồi thử lại.", ". Click “Download all” and try again."),
        )
    try:
        giay = float(await run_in_threadpool(VideoService.get_video_duration, path) or 0)
    except Exception:  # noqa: BLE001
        giay = 0.0
    if giay > GIOI_HAN_GIAY + 0.5 and not data.tu_link:
        raise HTTPException(
            status_code=400,
            detail=L(
                f"Video dài {int(giay // 60)} phút {int(giay % 60)} giây — Làm nhanh chỉ nhận tệp dưới "
                f"{GIOI_HAN_GIAY // 60} phút (nên dưới {KHUYEN_NGHI_GIAY // 60} phút). Video dài hãy dùng Edit chi tiết hoặc Chạy hàng loạt.",
                f"The video is {int(giay // 60)} min {int(giay % 60)} sec long — Quick Mode only accepts files under "
                f"{GIOI_HAN_GIAY // 60} minutes (ideally under {KHUYEN_NGHI_GIAY // 60} minutes). For long videos use Advanced Editor or Batch.",
            ),
        )

    b = _lo_nhanh()
    if b["id"] in BR._running or BS.is_running(b):
        raise HTTPException(status_code=409, detail=L("Đang làm một video khác — chờ xong rồi dán tiếp nhé.", "Another video is being processed — wait until it finishes, then paste the next one."))
    BR._chan_neu_may_ban()

    pid = await run_in_threadpool(
        BR.review_service.create_project,
        path, giay, "auto", "vi", GIONG_NHANH, (data.title or "").strip(), True,
    )
    await BR.enable_advanced(pid)
    b = BS.add_items(b, [{"project_id": pid, **BR._thong_tin_du_an(pid)}])
    #  CẤU HÌNH theo lựa chọn của người dùng: chuẩn Làm nhanh (đặt lại MỖI lượt — vừa là "reset về cấu hình gốc") hoặc theo mặc định
    #  dự án Chi tiết (dự án vừa tạo đã mang các giá trị đó từ ``ap_mac_dinh``; lô chỉ đọc lại từ metadata).
    che_do = _cau_hinh_nhanh(b)
    if che_do == "chi_tiet":
        meta_moi = BR.review_service.get_metadata(pid) or {}
        b = BS.set_config(b, _cfg_tu_meta(meta_moi))
    else:
        b = BS.set_config(b, _cfg_chuan())
    b = _dat_nguon_dich(b)
    await BR._ghi_cau_hinh_lo_xuong_du_an(b, pids=[pid], dung_hinh=False)
    if che_do == "chuan":
        await _ghi_dung_hinh(pid)
    logger.info(f"[NHANH] Tạo dự án {pid} từ '{os.path.basename(path)}' ({giay:.0f}s) — bắt đầu chuỗi.")
    kq = await _chay(BS.load(b["id"]) or b, pid)
    return {"batch_id": b["id"], "project_id": pid, **kq}


@router.post("/{project_id}/tiep")
async def tiep(project_id: str):
    """Chạy tiếp phần còn lại (sau khi dán bản dịch / nhập khoá AI / mở lại trang)."""
    b = _lo_nhanh()
    if not BS.find_item(b, project_id):
        raise HTTPException(status_code=404, detail=L("Dự án không thuộc lô Làm nhanh", "This project does not belong to the Quick Mode batch"))
    if b["id"] in BR._running or BS.is_running(b):
        return {"started": False, "cho_dan": False, "dang_chay": True}
    if b.get("nhanh_dung") == project_id:
        b.pop("nhanh_dung", None)
        b = BS.save(b)
    return await _chay(b, project_id)


class HuyRequest(BaseModel):
    #: Dự án Làm nhanh cần dừng; trống = dự án đang chạy của lô.
    project_id: str = ""


@router.post("/huy")
async def huy(data: Optional[HuyRequest] = Body(None)):
    """DỪNG video Làm nhanh đang làm — KHÔNG xoá gì (người dùng 09-10-2026: "người dùng không muốn xoá, huỷ tẹo chạy tiếp").

    Dừng dự án đang chạy (cùng đường với nút Huỷ của lô), chờ lô thật sự rảnh (~20 s) rồi đánh dấu dự án "đã dừng" để khung
    KHÔNG tự chạy tiếp ngay (vòng tự chạy tiếp thấy "rảnh + còn bước" sẽ chạy lại liền). Dự án, tệp tải về, các bước đã xong đều
    giữ nguyên; ``/{pid}/tiep`` chạy tiếp từ bước dang dở và gỡ dấu "đã dừng".
    """
    import asyncio

    data = data or HuyRequest()
    b = _lo_nhanh()
    pid = (data.project_id or "").strip() or str(((b.get("run") or {}).get("project_id")) or "")
    kq = {"status": "idle"}
    if b["id"] in BR._running or BS.is_running(b):
        kq = await BR.cancel_batch(b["id"])
        for _ in range(40):
            await asyncio.sleep(0.5)
            b = BS.load(b["id"]) or b
            if b["id"] not in BR._running and not BS.is_running(b):
                break
    ranh = b["id"] not in BR._running and not BS.is_running(b)
    if ranh and pid and BS.find_item(b, pid):
        b["nhanh_dung"] = pid
        BS.save(b)
    logger.info(f"[NHANH] Dừng {pid or '(không rõ)'}: ranh={ranh}")
    return {"status": kq.get("status", "idle"), "ranh": ranh, "project_id": pid}


@router.get("/trang-thai/{project_id}")
async def trang_thai(project_id: str):
    """Tiến độ của một dự án Làm nhanh — FE hỏi theo nhịp."""
    b = _lo_nhanh()
    item = BS.find_item(b, project_id)
    if not item:
        raise HTTPException(status_code=404, detail=L("Dự án không thuộc lô Làm nhanh", "This project does not belong to the Quick Mode batch"))
    dang_chay = b["id"] in BR._running or BS.is_running(b)
    buoc = (b.get("run") or {}).get("step") or BR._BUOC_DANG_CHAY.get(b["id"], "")
    cua_toi = ((b.get("run") or {}).get("project_id") == project_id)
    export = (item.get("data") or {}).get("export") or {}
    xuat = None
    if export.get("path") and os.path.exists(export["path"]):
        xuat = {"path": export["path"], "mb": export.get("mb", 0)}
    else:
        #  Bước Xuất bị đánh dấu "đã có sẵn" (xuất xong từ lượt trước / trong dự án) thì lô KHÔNG lưu đường dẫn -> dự phòng: bản xuất gần
        #  nhất ghi trong compositor_config (như ``batch_routes.export_info``). Thiếu dự phòng này trang quay mãi "Đang chờ tới lượt…"
        #  dù video đã có (ảnh người dùng 09-10-2026).
        try:
            from routes.review_routes import _review_compositor_config_path
            from services.compositor_utils import full_export_path

            duong = await run_in_threadpool(full_export_path, _review_compositor_config_path(project_id))
            if duong:
                xuat = {"path": duong, "mb": round(os.path.getsize(duong) / (1024 * 1024), 1)}
        except Exception:  # noqa: BLE001
            xuat = None
    co_khoa, ten_ai = _co_khoa_ai()
    #  TRỌN nhật ký của dự án này (người dùng 09-10-2026: không cắt 12 dòng) — nhật ký lô đã có trần 5000 dòng.
    log = [x for x in (b.get("log") or []) if x.get("project_id") == project_id]
    rong, cao = await run_in_threadpool(_kich_thuoc, project_id)
    return {
        "batch_id": b["id"],
        "project_id": project_id,
        "title": item.get("title") or "",
        "giay": item.get("giay") or 0,
        "rong": rong,
        "cao": cao,
        "steps": {s: (item.get("steps") or {}).get(s) or {} for s in _chuoi_cho(b)},
        "chuoi": _chuoi_cho(b),
        "step_labels": BS.STEP_LABEL,
        "dang_chay": dang_chay and cua_toi,
        "buoc_dang_chay": buoc if (dang_chay and cua_toi) else "",
        "cho_dan": _cho_dan(project_id, item),
        "da_dung": b.get("nhanh_dung") == project_id,
        "con_lai": _buoc_con_lai(item, _chuoi_cho(b)),
        "ai_key": co_khoa,
        "ten_ai": ten_ai,
        "xuat": xuat,
        "log": log,
    }


def _kich_thuoc(pid: str) -> tuple:
    """(rộng, cao) của video nguồn — bản xuất giữ tỷ lệ nguồn (``aspectRatio: source``) nên FE
    dựa vào đây để xếp ô DỌC (Douyin 9:16) hay NGANG (YouTube 16:9) cho hợp. (0, 0) = không biết."""
    try:
        meta = BR.review_service.get_metadata(pid) or {}
        ml = meta.get("media_library") or []
        m = next((x for x in ml if x.get("source")), ml[0] if ml else {})
        return int(m.get("width") or 0), int(m.get("height") or 0)
    except Exception:  # noqa: BLE001
        return 0, 0


@router.get("/gan-day")
async def gan_day(limit: int = 12):
    """Các video đã làm nhanh gần đây (để mở lại / xem bản xuất)."""
    b = _lo_nhanh()
    ra = []
    for it in (b.get("items") or [])[: max(1, min(50, int(limit)))]:
        export = (it.get("data") or {}).get("export") or {}
        pid = it.get("project_id") or ""
        rong, cao = await run_in_threadpool(_kich_thuoc, pid)
        ra.append({
            "project_id": pid,
            "title": it.get("title") or "",
            "giay": it.get("giay") or 0,
            "xong": bool(export.get("path")) and os.path.exists(export.get("path") or ""),
            "path": export.get("path") or "",
            "rong": rong,
            "cao": cao,
        })
    return {"batch_id": b["id"], "items": ra}


class CaiDatRequest(BaseModel):
    dich: str | None = None
    #: "chuan" | "chi_tiet" — áp cho video TIẾP THEO (video đang làm giữ cấu hình đã áp).
    cau_hinh: str | None = None


@router.post("/cai-dat")
async def cai_dat(data: CaiDatRequest = Body(...)):
    """Chọn cách dịch của Làm nhanh: ``auto`` / ``ai`` / ``prompt``. Đổi giữa lúc đang chờ dán bản dịch sang AI (đã có khoá) thì
    mở lại bước Dịch để lượt chạy tiếp dịch bằng AI."""
    if data.dich is not None and data.dich not in CACH_DICH:
        raise HTTPException(status_code=400, detail=L("Cách dịch không hợp lệ", "Invalid translation method"))
    if data.cau_hinh is not None and data.cau_hinh not in CACH_CAU_HINH:
        raise HTTPException(status_code=400, detail=L("Cấu hình không hợp lệ", "Invalid configuration"))
    b = _lo_nhanh()
    if data.cau_hinh is not None:
        b["nhanh_cau_hinh"] = data.cau_hinh
        b = BS.save(b)
        if data.dich is None:
            return {"cau_hinh": data.cau_hinh}
    if data.dich is None:
        return {"dich": _cach_dich(b)}
    b["nhanh_dich"] = data.dich
    b = BS.save(b)
    b = _dat_nguon_dich(b)
    if (b.get("config") or {}).get("translate_engine") == "ai":
        for it in b.get("items") or []:
            pid = it.get("project_id")
            st = ((it.get("steps") or {}).get("translate") or {}).get("status")
            if st == "skipped" and BR._suy_trang_thai(pid).get("translate") != "done":
                BS.set_step(b, pid, "translate", "pending", "")
    return {"dich": data.dich}
