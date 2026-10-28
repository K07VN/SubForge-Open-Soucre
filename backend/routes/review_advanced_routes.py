"""API cho CHE DO REVIEW NANG CAO (trinh dung timeline kieu CapCut).

Tat ca duong dan nam duoi ``/api/review-advanced``. Phan CHUAN BI NGUYEN LIEU (tao du
an, lay sub, phan tich, nhap kich ban, log, tai san, compositor) DUNG LAI nguyen cac
endpoint ``/api/review`` san co — o day chi bo sung nhung gi rieng cua che do nang cao:

  * danh dau du an la "nang cao"
  * timeline (doc / sua / keo tha / cat / chen / xoa)
  * sinh giong hang loat + tung cau (giu nguyen toc do, khong cat)
  * hai nut tu dong: don cho khoi chong & tang toc cho khop (co tran)
  * dung phim

NGUYEN TAC: file nay CHI THEM MOI. Khong sua mot dong nao cua ``review_routes.py``
(chi import lai vai ham khoa/log de hai che do khong chay chong len nhau).
"""
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from services import work_queue as WQ


from fastapi import (
    APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from services import review_advanced_engine as ADV
from services import review_timeline as TL
from services import review_audio_clips as AUD
from services import review_video_clips as VID
from services import review_media_library as LIB
from services import review_advanced_prompts as PROMPTS
from services import review_advanced_segment as SEG
from services import review_advanced_cleanup as CLEAN
from services import review_advanced_fit_ai as FITAI
from services import tach_cau as TC
from services.config_service import get_config_service
from services.review_engine import RenderCancelled

# Dung CHUNG khoa "du an dang ban" voi che do co dien: cung mot du an khong the vua
# phan tich vua dung phim, du la duong nao goi.
from routes.review_routes import (
    _acquire_review_or_409,
    _release_review,
    _is_review_busy,
    setup_project_logging,
    remove_project_logging,
    review_service,
)

# Cùng hàng rào bản quyền với /videos và /api/review — trước đây router này KHÔNG có,
# nên toàn bộ chế độ Chi tiết chạy được khi chưa kích hoạt và không bị chặn khi offline.
from routes.auth_routes import verify_license
from utils.i18n import L

#: Những việc VẪN cho chạy dù lô đang giữ dự án — đều là việc KHÔNG sửa dữ liệu
#: (hoặc chính là đường thoát): huỷ, đọc nhật ký, hỏi trạng thái, đọc sóng âm.
#:
#: ``/waveforms`` là POST nhưng CHỈ ĐỌC: dùng POST vì phải gửi kèm danh sách vài trăm
#: id (nhét vào query string thì tràn độ dài URL), chứ nó không ghi gì cả.
#:
#: ``/export-ranges`` GHI nhưng chỉ ghi tệp RIÊNG ``export_ranges.json`` mà không bước
#: nào khác của lô đọc/ghi (services/export_ranges.py) -> khoanh đoạn lúc lô đang sinh
#: giọng / dồn cho chính dự án đó vẫn an toàn. Bước "Xuất các đoạn đã chọn" đọc danh
#: sách MỘT lần lúc bắt đầu, sửa giữa chừng thì áp cho lượt sau.
_CHO_QUA_KHI_LO_GIU = ("/cancel", "/logs", "/status", "/prompt", "/waveforms", "/export-ranges")


async def _chan_sua_khi_lo_giu(request: Request) -> None:
    """CHẶN MỌI THAO TÁC SỬA lên dự án mà LÔ đang xử lý.

    Vì sao đặt ở cửa router chứ không gắn vào từng endpoint: trình dựng có hơn 40
    đường ghi (kéo clip, sửa chữ, đổi cấu hình, nhập kịch bản…). Gắn tay từng cái
    thì kiểu gì cũng sót một hai cái — mà đúng cái sót đó là chỗ dữ liệu bị lượt
    chạy của lô ghi đè, âm thầm.

    Lô đọc dự án lúc bắt đầu một bước rồi ghi lại lúc xong; sửa xen vào giữa là
    mất trắng công sửa mà không có lỗi nào hiện ra.

    CỐ Ý chỉ chặn khi bận VÌ LÔ (xem services/batch_lock.py): bận vì chính người
    dùng bấm sinh giọng / dựng thử trong trình dựng thì vẫn phải kéo thả sửa chữa
    được — đó là cách làm việc bình thường của chế độ Chi tiết.

    CHẶN ĐÚNG PHẦN DỮ LIỆU ĐANG BỊ ĐỘNG, không chặn cả trình dựng:

      · Bước TÁCH GIỌNG chỉ ghi ra ``separated/*.wav`` — không đụng timeline, kịch
        bản hay cài đặt. Demucs chạy vài phút mỗi video; khoá cứng trình dựng suốt
        chừng đó chỉ để canh một thư mục nó không hề đọc là vô lý.
      · KIỂU PHỤ ĐỀ / LOGO / HÌNH MỜ / KHUNG / VÙNG CHE nằm ở tệp cấu hình dựng
        hình RIÊNG (router ``/api/review``, không đi qua chốt này) nên chỉnh lúc
        nào cũng được — kể cả đang sinh giọng. Đang XUẤT mà chỉnh thì bản đang
        chạy vẫn dùng cấu hình lúc bắt đầu, thay đổi áp cho lần xuất sau.

    Còn lại (timeline, kịch bản, a/b-chunks, cài đặt dự án) thì các bước sub /
    dịch / sinh giọng / dồn / xuất đều đọc-ghi, nên vẫn chặn.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    duong = request.url.path
    if any(duong.endswith(x) for x in _CHO_QUA_KHI_LO_GIU):
        return
    pid = (request.path_params or {}).get("project_id") or ""
    if not pid:
        return

    from services import batch_lock as BL

    x = BL.dang_giu(str(pid))
    if not x:
        return
    if (x.get("step") or "") == "separate":
        return  # chỉ ghi separated/*.wav — xem ghi chú ở đầu hàm
    nhan = {
        "sub": L("lấy phụ đề", "getting subtitles for"), "translate": L("dịch", "translating"),
        "meta": L("soạn bài đăng", "composing the post for"), "voice": L("sinh giọng", "generating voices for"),
        "fit": L("dồn chồng lấn", "rearranging overlaps in"), "separate": L("tách giọng", "separating vocals of"),
        "export": L("xuất video", "exporting video of"),
        "clips": L("xuất các đoạn đã chọn", "exporting selected segments of"),
    }.get(x.get("step") or "", x.get("step") or L("xử lý", "processing"))
    raise HTTPException(
        status_code=409,
        detail=(
            L(f"Lô đang {nhan} dự án này — sửa lúc này sẽ bị lượt chạy ghi đè. "
              "Đợi nó xong, hoặc bấm Huỷ ở trang Chạy hàng loạt rồi sửa tiếp.",
              f"A batch is currently {nhan} this project — editing now would be overwritten by the run. "
              "Wait for it to finish, or click Cancel on the Batch page and then keep editing.")
        ),
    )


router = APIRouter(
    prefix="/api/review-advanced",
    tags=["review-advanced"],
    dependencies=[Depends(verify_license), Depends(_chan_sua_khi_lo_giu)],
)
logger = logging.getLogger("ReviewAdvanced")
config_service = get_config_service()


# --------------------------------------------------------------------------- #
#  Model
# --------------------------------------------------------------------------- #
class ClipPatch(BaseModel):
    b_text: Optional[str] = None
    voice: Optional[str] = None
    rate: Optional[str] = None
    #  Nhan VAI cua cau (video nhieu nhan vat) — giong doc gan theo vai o voice_cast.
    speaker: Optional[str] = None
    start: Optional[float] = None
    end: Optional[float] = None


class ClipCreate(BaseModel):
    b_text: str = ""
    start: float
    end: Optional[float] = None
    voice: Optional[str] = None
    rate: Optional[str] = None
    #  NHAN VAI cua cau moi. Thieu truong nay thi moi cau chen tay / dan bang Ctrl+V deu
    #  roi ve vai mac dinh "MC" — phim nhieu nhan vat dan lai mot cau cua "Nu 1" la no
    #  doc bang giong MC.
    speaker: Optional[str] = None


class ClipMove(BaseModel):
    uid: str
    start: float
    end: float


class TimelineSave(BaseModel):
    moves: List[ClipMove] = []


class VoiceRequest(BaseModel):
    uids: Optional[List[str]] = None
    force: bool = False


class SplitRequest(BaseModel):
    at_time: Optional[float] = None
    at_ratio: float = 0.5


class ScriptImport(BaseModel):
    """Kich ban B dan tho tu ChatGPT/Claude (nhan nguyen dang JSON nguoi dung co)."""
    b_chunks: List[dict] = []


class SpeedRequest(BaseModel):
    ceiling: Optional[float] = None
    #  Chi tang toc may cau nay (nut "Tăng tốc cho khớp" cua mot cau). Bo trong = ca du an.
    uids: Optional[List[str]] = None


class ApplySpeedRequest(BaseModel):
    """TOC DO TONG mong muon cho moi cau (nguoi nghe cam nhan), khong phai he so tho.
    Backend tu tru di phan toc do ma giong DA duoc sinh ra."""
    target: float = 1.0


#  Sua 1 doan audio: nhan nguyen dict thay vi khai bao model, vi truong "in" la tu
#  KHOA cua Python — khong dat ten field nhu vay duoc, ma bay dat ten khac roi alias
#  lai thi de lech. Cac gia tri deu duoc AUD.patch_clip kep lai nen khong so rac.


# --------------------------------------------------------------------------- #
#  Tro giup
# --------------------------------------------------------------------------- #
def _load(project_id: str):
    try:
        return ADV.load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án", "Project not found"))


def _load_meta(project_id: str) -> dict:
    """Chi doc metadata.json — KHONG dong den a_chunks / b_chunks.

    Dung cho cac endpoint bi FE hoi lien tuc (status, stems-status). Truoc day chung
    goi ``_load`` day du: moi lan hoi la doc ca a_chunks (hang tram cau) + b_chunks
    VA con chay ensure_uids co the GHI LAI b_chunks.json — 2 giay mot lan, ghi dia
    lien tuc vo ich.
    """
    meta_path = os.path.join(ADV.project_dir_of(project_id), "metadata.json")
    if not os.path.exists(meta_path):
        raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án", "Project not found"))
    import json as _json

    with open(meta_path, "r", encoding="utf-8") as f:
        return _json.load(f)


def _source_video(meta: dict) -> str:
    """Duong dan video goc — KHONG BAO GIO nem loi.

    ``SeparationService.resolve_source_media`` nem ``SeparationError`` khi tep bi xoa, bi
    di chuyen, hoac nam tren o dia chua cam. Goi tin timeline TUYET DOI khong duoc chet vi
    the: mat tep goc thi nguoi dung phai mo duoc du an ra de con thay viec da lam va doc
    duoc loi — truoc day ca ``GET /timeline`` lan moi endpoint sua deu tra 500, du an coi
    nhu bay mat khong vao noi. Roi ve dung duong da luu trong metadata: nho vay
    ``is_untouched_single`` van so khop dung, co ``composed`` khong bi doi oan.
    """
    raw = ((meta or {}).get("video_path") or "").strip()
    #  DU AN TRONG (chua co phim goc) -> tra RONG ngay. Khong duoc goi
    #  ``resolve_source_media("")``: no giai chuoi rong ra THU MUC LAM VIEC hien tai
    #  (backend/), roi moi chi phia sau tuong day la "phim goc" — log day canh bao
    #  "Vị trí file video gốc không hợp lệ" moi lan nap du an.
    if not raw:
        return ""
    try:
        from services.separation_service import SeparationService

        path, _ = SeparationService.resolve_source_media(raw)
        return path
    except Exception:
        return raw


def _require_advanced(p) -> None:
    if not TL.is_advanced(p["meta"]):
        raise HTTPException(
            status_code=400,
            detail=L("Dự án này không ở chế độ Review nâng cao.", "This project is not in Advanced Review mode."),
        )


def _timeline_payload(p, include_a: bool = False) -> dict:
    """Goi tin timeline. MAC DINH KHONG kem a_chunks.

    Cac endpoint SUA (keo tha, cat, xoa…) tra ve timeline moi sau moi thao tac; kem
    theo ca danh sach canh A moi lan la lang phi vi no khong he doi. Chi lan nap dau
    (GET /timeline) moi gui kem.
    """
    meta = p["meta"]
    data = TL.build_timeline(
        p["dir"], meta, p["a_lookup"], p["b_chunks"], include_a=include_a,
        # Nho ham nay ma timeline biet cau nao "da co giong nhung giong lech chu" —
        # thong tin song sot qua ca lan tai trang, khac voi co tam trong FE truoc day.
        sig_of=ADV.signature_fn(meta),
        # Vai + giong THAT se doc: mot cau khong ghi giong rieng van co the doc bang giong
        # cua VAI no thuoc ve, nen FE phai duoc biet ket qua da resolve.
        voice_of=lambda b: ADV.resolve_voice(meta, b),
        speaker_of=ADV.speaker_of,
    )
    # Bang PHAN VAI: vai nao dang co tren timeline, bao nhieu cau, gan giong gi.
    data["speakers"] = ADV.speakers_in_use(p["b_chunks"], meta)
    data["script_kind"] = PROMPTS.normalize_kind(meta.get("script_kind"))
    #  Thứ tiếng CHÍNH (câu lồng tiếng) — khung xem trước cần để biết dòng phụ đề thứ hai
    #  (``secondSubLang``) có phải chính nó không. "zh-cn" quy về "zh" như sub_tracks.
    _tl = (meta.get("target_language") or "vi").lower()
    data["target_language"] = "zh" if _tl == "zh-cn" else _tl
    # Lan audio + lan VIDEO nam cung goi tin de FE ve mot the.
    src_video = _source_video(p["meta"])
    data["audio_clips"] = AUD.timeline_view(p["meta"])
    data["video_clips"] = VID.timeline_view(p["meta"], src_video, p["dir"])
    data["video_lane_count"] = VID.lane_count(p["meta"])
    data["video_overlap"] = VID.overlap_count(p["meta"])
    data["source_audio_enabled"] = bool(p["meta"].get("source_audio_enabled", True))
    # Nguoi dung da tu chinh 4 muc am luong chua. Khung xem truoc CAN biet: chua cham thi
    # ban xuat di duong tat ``-c copy`` (tieng goc nguyen 100%, bo qua 4 muc) nen xem truoc
    # phai nghe y nhu vay; cham roi thi ca hai ben deu ap dung 4 muc.
    data["source_mix_touched"] = ADV.mix_touched(p["meta"])
    # Ban xuat se DUNG LAI hinh hay chi ``-c:v copy``? Khung xem truoc phai biet vi hai
    # duong tron tieng khac nhau (xem _render_composed). De backend tra loi thay vi FE
    # tu suy — tranh hai ben lech nhau khi doi luat.
    data["composed"] = not VID.is_untouched_single(p["meta"], src_video)
    # Timeline phai dai bang phan tu di xa nhat, khong con bo cung theo video goc —
    # nguoi dung keo clip ra sau diem ket thuc video goc la binh thuong.
    # Phim thi lay MEP CLIP, khong lay do dai tep goc — xem VID.film_end (tang toc x2).
    data["timeline_duration"] = round(max(
        VID.film_end(p["meta"], data.get("video_duration") or 0.0),
        max((c["start"] + c["duration"] for c in data["audio_clips"]), default=0.0),
        max((c["end"] for c in data["clips"]), default=0.0),
    ), 3)
    return data


async def _payload(p, include_a: bool = False) -> dict:
    """``_timeline_payload`` nhung CHAY O LUONG KHAC.

    Ham do do thoi luong tung tep audio bang ffmpeg (lan dau, truoc khi vao cache) va
    doc/ghi dia — chay thang trong ``async def`` la chan ca event loop, moi request khac
    (kem ca SSE nhat ky va nut Huy) treo theo. Moi endpoint sua timeline deu tra ve goi
    tin nay nen cho nay bi goi rat nhieu.
    """
    return await run_in_threadpool(_timeline_payload, p, include_a)


# --------------------------------------------------------------------------- #
#  Che do du an
# --------------------------------------------------------------------------- #
@router.post("/projects/{project_id}/enable")
async def enable_advanced(project_id: str):
    """Danh dau du an la 'nang cao' + gan uid cho toan bo cau B.

    Du an tao qua ``POST /api/review/projects`` roi goi endpoint nay -> vao luong moi.
    Khong dung den giong da sinh: uid di tru trung dung ten thu muc cu.
    """
    p = _load(project_id)
    p["meta"][TL.MODE_FIELD] = TL.MODE_ADVANCED
    # Che do nang cao KHONG dung xe dich tu dong luc render — vi tri tren timeline la
    # vi tri cuoi cung. Tat co nay de engine cu (neu bi goi nham) khong ghi de.
    p["meta"]["smart_srt_shift"] = False
    ADV.save_meta(p["dir"], p["meta"])
    stats = await run_in_threadpool(TL.migrate_project_uids, p["dir"])

    #  PHU DE CHINH CHU -> DO THANG VAO TIMELINE ngay luc tao du an.
    #
    #  Truoc day sidecar chi duoc doc khi nguoi dung bam "Lay phu de" O CHE DO NGHE, nen
    #  tai mot video CO san sub cua tac gia ve thi timeline van trong tron: khong co dau
    #  hieu nao cho biet phim nay da co sub san, va nguoi dung ngoi cho Whisper nghe lai
    #  ca tap de ra mot ban KEM CHINH XAC HON ban dang nam canh do. Do vao day thi mo
    #  trinh dung la thay ngay.
    #
    #  Van thay lai duoc bang moi duong khac (Whisper / OCR / Ket hop / nap tep), va
    #  sidecar duoc GIU LAI (consume=False) nen nut "Dung lai sub goc cua video" bam bao
    #  nhieu lan cung duoc.
    author = 0
    if not SEG.load_a_chunks(p["dir"]):
        author = await run_in_threadpool(_apply_author_subs, p, log=logger)
    return {
        "status": "enabled",
        "mode": TL.MODE_ADVANCED,
        "migration": stats,
        "author_subs": author,
    }


def _apply_author_subs(p: dict, log=None, lang: str = "") -> int:
    """Do phu de CHINH CHU (sidecar canh video) vao a_chunks. Tra ve so cau da nap.

    KHONG chup anh hoan tac o day: chu goi quyet dinh (luc ``/enable`` thi du an con
    trong tron, chua co gi de lui ve). Sidecar duoc GIU LAI — xem ``_load_author_subs``.
    """
    meta = p["meta"]
    #  Du an TRONG (khong co phim goc) thi khong co sidecar nao de doc — thoat som thay
    #  vi di do mot duong dan rong.
    src = _source_video(meta)
    if not src:
        return 0
    subs = review_service._load_author_subs(src, consume=False, lang=lang)
    if not subs or not subs.get("segments"):
        return 0

    #  MOC TRONG SIDECAR LA GIAY CUA TEP PHIM, khong phai giay timeline. Hai truc do chi
    #  trung nhau khi phim goc con nguyen mot dai bat dau tu giay 0 va chua doi toc do —
    #  dung luc ``/enable``, nhung nut "Dung lai sub goc" bam duoc BAT KY LUC NAO, ke ca
    #  sau khi nguoi dung da cat dau phim, chen doan mo dau, hay doi toc do clip. Khong
    #  chieu lai truc thi phu de nhay het cho ma khong bao gi.
    #
    #  Chieu qua CLIP CUA VIDEO GOC (mảnh dau tien, xem ``VID.is_source_clip``) bang dung
    #  ``SEG.to_timeline``, va bo cau nam ngoai khoang clip con giu lai — cat dau phim thi
    #  phu de cua doan da cat khong con cho nao de dat.
    goc = next(
        (c for c in VID.playable_clips(meta) if VID.is_source_clip(c, src)), None,
    )
    lines = []
    ngoai = 0
    for s in subs["segments"]:
        t0 = float(s.get("start") or 0.0)
        t1 = float(s.get("end") or t0)
        if goc is not None:
            #  Loc theo DIEM GIUA — cung luat voi ``a_ids_in_clip``, cau vat ngang mep chi
            #  thuoc ve mot ben.
            if not (
                VID._num(goc.get("in")) - 0.001
                <= (t0 + t1) / 2.0
                < VID._num(goc.get("out")) + 0.001
            ):
                ngoai += 1
                continue
            t0, t1 = SEG.to_timeline(goc, t0), SEG.to_timeline(goc, t1)
        lines.append({"start": t0, "end": t1, "text": s.get("text")})
    if ngoai and log:
        log.info(
            f"[nâng cao] Sub gốc: bỏ {ngoai} câu nằm ngoài phần phim gốc còn trên "
            f"timeline (đã bị cắt)."
        )
    if not lines:
        return 0

    res = SEG.replace_all_a_chunks(p["dir"], p["a_lookup"], p["b_chunks"], lines)
    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    state = meta.setdefault("state", {})
    state["stt_complete"] = True
    state["a_chunks_generated"] = True
    state["analysis_started"] = False
    state.pop("analysis_error", None)
    #  Ngon ngu cua sub goc -> ngon ngu NGUON cua du an, khi nguoi dung chua chon ro.
    #  Bo qua buoc nay thi o ngon ngu van la "Tu nhan dien" va prompt dich sinh ra khong
    #  biet dang dich tu thu tieng nao.
    lang = (subs.get("lang") or "").split("-")[0].lower()
    if lang and str(meta.get("source_language") or "auto") in ("", "auto"):
        meta["source_language"] = "zh" if lang == "zh" else lang
    ADV.save_meta(p["dir"], meta)
    if log:
        log.info(
            f"[nâng cao] Phụ đề CHÍNH CHỦ của video ({subs.get('lang') or '?'}): "
            f"đã nạp {res['added']} câu gốc thẳng vào timeline, không phải chạy nhận dạng."
        )
    return res["added"]


@router.get("/projects/{project_id}/author-subs")
async def author_subs_status(project_id: str):
    """Video nay CO phu de chinh chu khong (de FE hien/an nut "Dung lai sub goc")."""
    meta = _load_meta(project_id)
    subs = await run_in_threadpool(
        review_service._load_author_subs, _source_video(meta), False,
    )
    return {
        "available": bool(subs and subs.get("segments")),
        "count": len(subs["segments"]) if subs and subs.get("segments") else 0,
        "lang": (subs or {}).get("lang") or "",
        #  MOI thu tieng tai duoc — FE hien o chon de doi qua lai ma khong phai tai lai
        #  video. Sidecar cua ban cu chi co mot thu tieng nen list chi co dung no.
        "langs": (subs or {}).get("langs") or [],
    }


class AuthorSubsRequest(BaseModel):
    #  Thu tieng muon dung (trong so cac thu tieng tai ve). Bo trong = thu tieng mac dinh.
    lang: str = ""


@router.post("/projects/{project_id}/author-subs")
async def use_author_subs(project_id: str, data: AuthorSubsRequest = Body(default=AuthorSubsRequest())):
    """DUNG LAI phu de chinh chu cua video — thay toan bo cau goc.

    Bam duoc NHIEU LAN: sidecar khong bi xoa sau khi doc. Duong quay ve sau khi da thu
    Whisper/OCR/nap tep ma thay ban goc van tot hon. ``lang`` cho phep doi sang thu tieng
    khac trong so cac thu tieng da tai ve cung video.
    """
    p = _load(project_id)
    _require_advanced(p)
    snap = ADV.capture_snapshot(p["dir"])
    added = await run_in_threadpool(_apply_author_subs, p, logger, data.lang)
    if not added:
        raise HTTPException(
            status_code=404,
            detail=(
                L("Video này không có phụ đề chính chủ đi kèm. Dùng Nghe (Whisper), "
                  "Đọc sub cứng (OCR), hoặc nạp tệp phụ đề có sẵn.",
                  "This video has no official subtitles attached. Use Listen (Whisper), "
                  "Read hard subs (OCR), or load an existing subtitle file.")
            ),
        )
    ADV.commit_snapshot(p["dir"], snap)
    p2 = _load(project_id)
    return {"added": added, "timeline": await _payload(p2, include_a=True)}


@router.get("/projects")
async def list_advanced_projects():
    """Chi liet ke du an dang o che do chi tiet.

    CO Y chi tra ve mot nua: dashboard goi CA endpoint nay lan ``/api/review/projects``
    roi lay phan bu de biet du an nao con o che do cu — chinh la nhung du an duoc xep
    rieng mot muc kem nut chuyen doi. Tra ve het o day la muc do rong khong, va du an
    cu se nam lan trong danh sach chi tiet voi nut "Mo trinh dung" bam vao thi 400.
    """
    out = []
    for meta in review_service.list_projects():
        if TL.is_advanced(meta):
            out.append(meta)
    return out


# --------------------------------------------------------------------------- #
#  Phat video nguon (CO ho tro Range)
# --------------------------------------------------------------------------- #
def _range_response(path: str, request: Request, media_type: str):
    """Phat 1 file CO ho tro HTTP Range (206) — dung cho ca video nguon lan stem audio.

    VI SAO can: ``/videos/file`` tra ``FileResponse`` thuong, KHONG xu ly Range. Xem
    het mot luot thi khong sao, nhung trinh dung phai TUA lien tuc — thieu Range la
    trinh duyet khong seek duoc.
    """
    file_size = os.path.getsize(path)
    range_header = request.headers.get("range") or request.headers.get("Range")

    def _iter(start: int, end: int, chunk: int = 1024 * 512):
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    if not range_header:
        return StreamingResponse(
            _iter(0, file_size - 1),
            media_type=media_type,
            headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"},
        )

    # "bytes=START-END" — END co the trong (lay den het file).
    try:
        raw = range_header.split("=", 1)[1]
        s_str, _, e_str = raw.partition("-")
        start = int(s_str) if s_str else 0
        end = int(e_str) if e_str else file_size - 1
    except (ValueError, IndexError):
        start, end = 0, file_size - 1
    start = max(0, min(start, file_size - 1))
    end = max(start, min(end, file_size - 1))

    return StreamingResponse(
        _iter(start, end),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )


async def anh_dai_dien_du_an(project_id: str):
    """Ảnh đại diện (JPEG ~320px, dựng MỘT lần rồi dùng lại) của dự án Chi tiết.

    Dùng chung cho Trang chủ / danh sách Edit chi tiết (router này) và bảng lô
    (``/api/batch/thumb``). Router lô bị chặn theo GÓI bản quyền, nên Trang chủ không
    được đi qua nó — máy không có gói Hàng loạt sẽ mất hết ảnh.
    """
    from fastapi.responses import FileResponse

    try:
        p = ADV.load_project(project_id, migrate=False)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án", "Project not found"))
    src = (p["meta"].get("video_path") or "").strip()
    if not src or not os.path.exists(src):
        raise HTTPException(status_code=404, detail=L("Dự án chưa có video gốc", "The project has no source video yet"))
    dst = os.path.join(LIB.thumb_dir(p["dir"]), "project.jpg")
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        ok = await run_in_threadpool(
            LIB._make_thumb, src, dst, float(p["meta"].get("video_duration") or 0) or 60.0
        )
        if not ok:
            raise HTTPException(status_code=404, detail=L("Không dựng được ảnh đại diện", "Could not create the thumbnail"))
    #  Ảnh không đổi -> cho trình duyệt giữ cache, cuộn danh sách không gọi lại.
    return FileResponse(dst, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/projects/{project_id}/thumb")
async def get_project_thumb(project_id: str):
    return await anh_dai_dien_du_an(project_id)


@router.get("/projects/{project_id}/source-video")
async def stream_source_video(project_id: str, request: Request):
    """Video goc cho khung xem truoc (co Range)."""
    p = _load(project_id)
    # Mat tep thi tra 404 GON GHE cho <video> tu hien loi, dung de SeparationError noi
    # thanh 500 (trinh duyet chi thay "lỗi mạng", nhat ky backend thi day vet nem loi).
    path = _source_video(p["meta"])
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=L("Không tìm thấy video nguồn", "Source video not found"))
    return _range_response(path, request, "video/mp4")


@router.get("/projects/{project_id}/stem/{kind}")
async def stream_stem(project_id: str, kind: str, request: Request):
    """Phat 1 stem da tach: ``vocals`` (giong goc) hoac ``no_vocals`` (nhac nen).

    Nho hai stem nay ma khung xem truoc nghe duoc DUNG 4 muc am luong khi bat tach
    giong — thay vi chi uoc luong bang mot duong tieng goc chung.
    """
    if kind not in ("vocals", "no_vocals"):
        raise HTTPException(status_code=400, detail=L("Stem không hợp lệ", "Invalid stem"))
    p = _load(project_id)
    path = os.path.join(p["dir"], "separated", f"{kind}.wav")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=L("Chưa tách nhạc nền cho dự án này", "Background music has not been separated for this project"))
    return _range_response(path, request, "audio/wav")


# --------------------------------------------------------------------------- #
#  Tach nhac nen (Demucs) — chay MOT LAN, ket qua duoc cache
# --------------------------------------------------------------------------- #
def _separate_wrapper(project_id: str):
    """Chay nen: tach stem + ghi log vao synthesis.log cho FE theo doi."""
    try:
        #  XẾP HÀNG: mỗi lúc chỉ một việc nặng chạy trên máy (xem
        #  services/work_queue.py). Chưa tới lượt thì đợi chứ không bị từ chối.
        with WQ.slot(project_id, L("Tách giọng (Demucs)", "Separate vocals (Demucs)"), cancelled=lambda: review_service.is_render_cancelled(project_id)):
            handler = setup_project_logging(project_id, config_service.get_review_root())
            log = logging.getLogger("SeparationService")
            try:
                from services.separation_service import SeparationService

                p = ADV.load_project(project_id, migrate=False)
                path, _ = SeparationService.resolve_source_media(p["meta"].get("video_path", ""))
                cancel_marker = os.path.join(p["dir"], ".cancel_render")
                stem_dir = os.path.join(p["dir"], "separated")
                log.info("=== BẮT ĐẦU TÁCH GIỌNG & NHẠC NỀN (Demucs) ===")
                # Cung ly do nhu ban tach theo clip: chua xong thi khong duoc coi la co stem.
                VID.mark_separating(stem_dir)
                try:
                    SeparationService.ensure_stems(
                        path, stem_dir,
                        check_cancel=lambda: os.path.exists(cancel_marker),
                    )
                except BaseException:
                    VID.discard_stems(stem_dir)
                    raise
                VID.clear_separating(stem_dir)
                log.info("=== TÁCH GIỌNG & NHẠC NỀN HOÀN TẤT ===")
            except Exception as e:
                log.error(f"Tách giọng & nhạc nền thất bại: {e}", exc_info=True)
            finally:
                remove_project_logging(project_id, handler)
                _release_review(project_id)


    except WQ.WorkCancelled:
        #  Bấm Huỷ khi việc CÒN ĐANG XẾP HÀNG: thân hàm chưa chạy nên phần
        #  dọn dẹp trong ``finally`` của nó cũng chưa chạy — phải tự dọn ở
        #  đây, không thì khoá dự án không ai nhả và giao diện treo mãi.
        logger.warning(f"[HÀNG ĐỢI] Đã huỷ khi còn đang xếp hàng: {project_id}")
        _release_review(project_id)
@router.post("/projects/{project_id}/separate")
async def separate_audio(project_id: str, background_tasks: BackgroundTasks):
    """Tach nhac nen cho du an. Da co san (cache) thi tra ve ngay, khong chay lai."""
    p = _load(project_id)
    _require_advanced(p)

    out_dir = os.path.join(p["dir"], "separated")
    # Dang tach do dang thi KHONG tinh la co san (xem VID.mark_separating).
    have = not VID.is_separating(out_dir) and all(
        os.path.exists(os.path.join(out_dir, f"{k}.wav")) for k in ("vocals", "no_vocals")
    )
    if have:
        return {"status": "ready", "cached": True}

    _acquire_review_or_409(project_id)
    try:
        review_service.clear_render_cancel(project_id)
        background_tasks.add_task(_separate_wrapper, project_id)
    except Exception:
        _release_review(project_id)
        raise
    return {"status": "started", "cached": False}


@router.get("/projects/{project_id}/stems-status")
async def stems_status(project_id: str):
    """Da co stem chua — FE dua vao day de biet co nghe thu 4 muc duoc khong."""
    out_dir = os.path.join(ADV.project_dir_of(project_id), "separated")
    ready = not VID.is_separating(out_dir) and all(
        os.path.exists(os.path.join(out_dir, f"{k}.wav")) for k in ("vocals", "no_vocals")
    )
    return {"ready": ready, "busy": _is_review_busy(project_id)}



# --------------------------------------------------------------------------- #
#  NHIEU DOAN AUDIO chen them (nhac nen, hieu ung…) — keo tha nhu clip giong
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/audio-clips")
async def list_audio_clips(project_id: str):
    meta = _load_meta(project_id)
    return {"clips": AUD.timeline_view(meta)}


@router.post("/projects/{project_id}/audio-clips")
async def add_audio_clip(
    project_id: str,
    file: UploadFile = File(...),
    start: float = 0.0,
):
    """Tai 1 tep nhac len va dat vao timeline tai giay ``start``."""
    p = _load(project_id)
    _require_advanced(p)
    fname = file.filename or ""
    if not fname.lower().endswith(AUD.ALLOWED_EXTS):
        raise HTTPException(
            status_code=400,
            detail=L("Chỉ nhận tệp .mp3 / .wav / .m4a / .aac / .ogg / .flac", "Only .mp3 / .wav / .m4a / .aac / .ogg / .flac files are accepted"),
        )

    ext = os.path.splitext(fname)[1].lower()
    dest = os.path.join(AUD.asset_dir(p["dir"]), f"{AUD.new_id()}{ext}")

    # Sao theo TUNG KHUC va o luong khac, khong ``await file.read()``.
    # Ban cu doc TRON tep vao mot bytes: mot ban nhac khong lo lam, nhung tep .wav /
    # .flac vai tram MB thi backend phong len dung do RAM (Starlette da giu san mot ban
    # o dia roi), va ``f.write`` cua ca khoi do chan luon event loop.
    import shutil as _shutil

    def _save_upload() -> int:
        with open(dest, "wb") as out:
            _shutil.copyfileobj(file.file, out, 1024 * 1024)
        return os.path.getsize(dest)

    try:
        written = await run_in_threadpool(_save_upload)
    except OSError as e:
        logger.error(f"[nâng cao] Không ghi được tệp nhạc vừa tải lên: {e}")
        raise HTTPException(status_code=500, detail=L("Không ghi được tệp nhạc lên đĩa", "Could not write the music file to disk"))
    if written <= 0:
        # Tep rong -> don ngay, dung de lai rac trong thu muc du an.
        try:
            os.remove(dest)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=L("Tệp nhạc rỗng", "The music file is empty"))

    dur = await run_in_threadpool(AUD.media_duration, dest)
    clip = AUD.make_clip(dest, fname, start, dur)
    meta = p["meta"]
    ADV.take_snapshot(p["dir"])      # để Ctrl+Z lùi được thao tác thêm nhạc
    meta[AUD.FIELD] = AUD.list_clips(meta) + [clip]
    ADV.save_meta(p["dir"], meta)
    return {"status": "added", "clip": clip, "clips": AUD.timeline_view(meta)}


@router.post("/projects/{project_id}/audio-clips/from-path")
async def add_audio_clip_from_path(project_id: str, data: dict = Body(...)):
    """Them doan nhac tu file DA CO san — dung khi chon PRESET nhac.

    Preset audio ca nhan giu file trong kho AppData (compositor_presets/audio) nen
    dung lai duoc xuyen du an: chon preset la co ngay bai nhac, khong phai tai lai.
    """
    p = _load(project_id)
    _require_advanced(p)

    src = (data.get("path") or "").strip()
    start = float(data.get("start") or 0.0)
    volume = data.get("volume")
    if not src or not os.path.exists(src):
        raise HTTPException(status_code=404, detail=L("Không tìm thấy tệp nhạc của preset", "The preset music file was not found"))

    # Chi cho phep lay tu 2 noi: thu muc du an, hoac kho preset toan cuc.
    from models.database import get_app_data_dir
    from services.compositor_preset_service import PRESET_STORE_DIR

    store_root = os.path.join(get_app_data_dir(), PRESET_STORE_DIR)
    if not (
        AUD.is_inside_project(p["dir"], src) or AUD.is_inside_project(store_root, src)
    ):
        raise HTTPException(status_code=400, detail=L("Tệp nhạc nằm ngoài kho cho phép", "The music file is outside the allowed library"))

    # Sao vao thu muc du an -> xoa preset sau nay khong lam hong du an dang dung.
    # Tep DA nam trong du an (vd stem vua tach) thi dung tai cho: stem cua phim 20
    # phut nang hang tram MB, nhan doi ra khong duoc gi.
    if AUD.is_inside_project(p["dir"], src):
        dest = src
    else:
        ext = os.path.splitext(src)[1].lower()
        dest = os.path.join(AUD.asset_dir(p["dir"]), f"{AUD.new_id()}{ext}")
        import shutil as _shutil

        _shutil.copy2(src, dest)

    dur = await run_in_threadpool(AUD.media_duration, dest)
    clip = AUD.make_clip(
        dest, data.get("name") or os.path.basename(src), start, dur,
        volume if volume is not None else 0.35,
    )

    # NHAN BAN (Ctrl+C/V) phai ra ban GIONG HET tren timeline: giu nguyen doan da cat
    # (in/out) va co lap. Neu bo qua, nguoi dung dan ra ca bai goc dai thong thong —
    # khong phai thu ho vua cat.
    meta = p["meta"]
    for key in ("in", "out", "loop"):
        if data.get(key) is not None:
            clip[key] = data[key]
    if data.get("in") is not None or data.get("out") is not None:
        tin = max(0.0, float(clip.get("in") or 0.0))
        tout = float(clip.get("out") or dur or 0.0)
        tout = min(tout, dur) if dur else tout
        if tout <= tin + 0.05:
            tout = tin + 0.05
        clip["in"], clip["out"] = round(tin, 3), round(tout, 3)
    clip["loop"] = bool(clip.get("loop", False))

    ADV.take_snapshot(p["dir"])
    meta[AUD.FIELD] = AUD.list_clips(meta) + [clip]
    ADV.save_meta(p["dir"], meta)
    return {"status": "added", "clip": clip, "clips": AUD.timeline_view(meta)}


# --------------------------------------------------------------------------- #
#  KHO MEDIA — nhap tep vao kho truoc, dua len timeline sau (kieu CapCut)
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/media-library")
async def list_media_library(project_id: str):
    p = _load(project_id)
    return {"items": LIB.view(p["dir"], p["meta"])}


@router.post("/projects/{project_id}/media-library")
async def add_to_media_library(project_id: str, data: dict = Body(...)):
    """Them tep vao kho. Nhan MOT duong dan (``path``) hoac MOT ME (``paths``).

    Tep loi thi bo qua rieng tep do va bao lai trong ``failed`` — nhap 10 tep ma hong
    mot cai thi 9 cai kia van vao kho, khong bat lam lai tu dau.
    """
    p = _load(project_id)
    _require_advanced(p)

    raw = data.get("paths") or ([data.get("path")] if data.get("path") else [])
    paths = [str(x).strip().strip('"') for x in raw if str(x or "").strip()]
    if not paths:
        raise HTTPException(status_code=400, detail=L("Chưa chọn tệp nào", "No file selected"))

    meta = p["meta"]
    added, failed = [], []
    for src in paths:
        if not os.path.exists(src):
            failed.append({"path": src, "reason": L("không tìm thấy tệp", "file not found")})
            continue
        try:
            item = await run_in_threadpool(LIB.add_from_path, p["dir"], meta, src)
            added.append(item)
        except ValueError as e:
            failed.append({"path": src, "reason": str(e)})
        except Exception as e:
            logger.error(f"[nâng cao] Nhập kho media lỗi ({src}): {e}", exc_info=True)
            failed.append({"path": src, "reason": L("lỗi không mong đợi", "unexpected error")})

    if added:
        meta[LIB.FIELD] = LIB.list_items(meta) + added
        ADV.save_meta(p["dir"], meta)
        logger.info(f"[nâng cao] Kho media: thêm {len(added)} tệp.")
    if not added and failed:
        raise HTTPException(status_code=400, detail=failed[0]["reason"])
    return {
        "status": "added", "added": len(added), "failed": failed,
        "items": LIB.view(p["dir"], meta),
    }


@router.delete("/projects/{project_id}/media-library/{item_id}")
async def remove_from_media_library(project_id: str, item_id: str):
    p = _load(project_id)
    _require_advanced(p)
    if not LIB.remove(p["dir"], p["meta"], item_id):
        raise HTTPException(status_code=404, detail=L("Không tìm thấy mục trong kho", "Item not found in the library"))
    ADV.save_meta(p["dir"], p["meta"])
    return {"status": "removed", "items": LIB.view(p["dir"], p["meta"])}


@router.get("/projects/{project_id}/media-library/{item_id}/thumb")
async def media_library_thumb(project_id: str, item_id: str):
    """Anh dai dien cua mot muc trong kho (chi video co)."""
    pdir = ADV.project_dir_of(project_id)
    meta = _load_meta(project_id)
    item = LIB.find(meta, item_id)
    if not item or not item.get("thumb"):
        raise HTTPException(status_code=404, detail=L("Mục này không có ảnh đại diện", "This item has no thumbnail"))
    path = os.path.join(pdir, item["thumb"])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=L("Ảnh đại diện không còn trên đĩa", "The thumbnail is no longer on disk"))
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})


@router.post("/projects/{project_id}/media-library/{item_id}/save-preset")
async def save_media_preset(project_id: str, item_id: str, data: dict = Body(default={})):
    """Luu MOT MUC TRONG KHO thanh PRESET dung lai xuyen du an.

    Vi sao can duong nay: kho Nguyen lieu la cua RIENG du an nay, con preset song trong
    AppData nen moi du an sau deu lay lai duoc. Truoc day muon tao preset thi phai dua
    clip len timeline roi luu tu bang thuoc tinh — nhap mot doan nhac/anh de danh dung lai
    ma phai chen vao phim truoc la vong ve vo ly. Nhan MOT nut ngay tai kho.

    Ca ba loai deu luu duoc:
      * nhac  -> nhom "audio", dung chung danh sach voi preset nhac hien co;
      * video -> nhom "video" (co tran PRESET_MAX_SECONDS: preset la de dung lai doan
        NGAN, keo ca bo phim vao AppData la lam day dia nguoi dung);
      * anh   -> cung nhom "video" kem co ``is_image`` (xem _PRESET_EXTS), giu tren man
        DEFAULT_IMAGE_SECONDS giay khi chen lai — dung so ma nut "them vao timeline" dung.

    Tep duoc SAO vao kho preset chu khong tro vao tep trong du an: xoa du an la preset
    van con dung duoc.
    """
    p = _load(project_id)
    _require_advanced(p)
    item = LIB.find(p["meta"], item_id)
    if not item:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy mục trong kho", "Item not found in the library"))
    src = item.get("path") or ""
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail=L("Tệp của mục này không còn trên đĩa", "The file of this item is no longer on disk"))

    kind = str(item.get("kind") or "audio")
    name = str(data.get("name") or "").strip() or (item.get("name") or "Nguyên liệu")
    dur = float(item.get("duration") or 0.0)
    if kind == "video" and dur > VID.PRESET_MAX_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Video này dài {dur / 60:.1f} phút — preset là để dùng lại đoạn NGẮN "
                f"(mở đầu, kết thúc, clip chèn). Hãy đưa lên timeline, cắt còn dưới "
                f"{VID.PRESET_MAX_SECONDS // 60} phút rồi lưu đoạn đó thành preset."
            ),
        )

    from services.compositor_preset_service import (
        add_personal_preset, copy_asset_to_preset_store,
    )

    category = "audio" if kind == "audio" else "video"
    try:
        dest = await run_in_threadpool(copy_asset_to_preset_store, src, category)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if category == "audio":
        payload = {"path": dest, "name": name, "volume": 1.0, "duration": round(dur, 3)}
    else:
        is_img = kind == "image"
        payload = {
            "path": dest,
            "name": name,
            # Anh chua co do dai rieng -> lay dung so mac dinh cua nut "them vao timeline".
            "duration": round(VID.DEFAULT_IMAGE_SECONDS if is_img else dur, 3),
            "is_image": is_img,
            "speed": 1.0,
            "volume": 1.0,
            "muted": False,
            "mix": None,
            "width": int(item.get("width") or 0),
            "height": int(item.get("height") or 0),
        }

    try:
        preset, _store = add_personal_preset(category, name, payload)
    except ValueError as e:
        # Ghi preset that bai -> don luon tep vua sao, dung de rac trong AppData.
        try:
            os.remove(dest)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(f"[nâng cao] Đã lưu “{name}” ({kind}) thành preset {category}.")
    return {"status": "saved", "category": category, "preset": preset}


@router.post("/projects/{project_id}/media-library/{item_id}/to-timeline")
async def media_to_timeline(project_id: str, item_id: str, data: dict = Body(default={})):
    """Dua mot muc trong kho len timeline.

    Video / ANH -> ``position``: ``"end"`` noi vao cuoi, ``"start"`` dat len dau (day moi
    thu lui), ``"at"`` CHEN vao giua tai giay ``at`` (cat clip dang phu diem do roi day
    phan sau — ke ca giong doc va nhac — lui lai). Anh giu tren man
    ``seconds`` giay (mac dinh VID.DEFAULT_IMAGE_SECONDS, keo mep doi sau).
    Nhac -> dat tai ``start`` (mac dinh 0).
    Muc trong kho KHONG bi tieu di: them bao nhieu lan cung duoc.
    """
    p = _load(project_id)
    _require_advanced(p)
    meta = p["meta"]
    item = LIB.find(meta, item_id)
    if not item:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy mục trong kho", "Item not found in the library"))
    src = item.get("path") or ""
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail=L("Tệp của mục này không còn trên đĩa", "The file of this item is no longer on disk"))

    ADV.take_snapshot(p["dir"])
    shifted = {"clips": 0, "audio": 0, "subs": 0}
    if item.get("kind") in ("video", "image"):
        position = str(data.get("position") or "end")
        if position not in ("start", "end", "at"):
            position = "end"
        clip = VID.make_clip(
            src, item.get("name") or "", 0.0,
            seconds=data.get("seconds"),      # chi co y nghia voi ANH
        )
        if position == "at":
            # CHEN TAI CON TRO: cat clip dang phu diem do roi day phan sau lui — ke ca
            # GIONG DOC, NHAC va DAI PHU DE GOC, khong thi hinh va tieng lech nhau dung
            # do dai doan chen.
            at = max(0.0, float(data.get("at") or 0.0))
            VID.insert_clip_at(meta, clip, at)
            shifted = ADV.shift_after(
                p["dir"], meta, p["a_lookup"], p["b_chunks"], at,
                VID.visible_duration(clip),
            )
            ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
        else:
            VID.append_clip(meta, clip, position)
            if position == "start":
                shifted = ADV.shift_everything(
                    p["dir"], meta, p["a_lookup"], p["b_chunks"],
                    VID.visible_duration(clip),
                )
                ADV.commit_chunks(
                    p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False
                )
        kind = item.get("kind")
    else:
        start = float(data.get("start") or 0.0)
        dur = float(item.get("duration") or 0.0) or AUD.media_duration(src)
        clip = AUD.make_clip(src, item.get("name") or "", start, dur,
                             role=AUD.ROLE_MUSIC)
        meta[AUD.FIELD] = AUD.list_clips(meta) + [clip]
        kind = "audio"
    ADV.save_meta(p["dir"], meta)
    # Da doi DAI PHU DE GOC thi PHAI gui kem no: goi tin cua endpoint SUA mac dinh bo
    # a_chunks, FE giu lai ban dang co -> khong gui thi backend da dời đúng mà dai phu de
    # goc van ve o cho cu cho toi luc tai lai trang (dung loi nguoi dung vua gap).
    return {"status": "added", "kind": kind, "clip": clip, "shifted": shifted,
            "timeline": await _payload(p, include_a=bool(shifted.get("subs")))}


@router.post("/pick-media")
async def pick_media(data: dict = Body(default={})):
    """Mo hop thoai chon tep NHAC hoac VIDEO tren may.

    ``multiple=true`` cho chon ca me. Chon bang duong dan thay vi tai len qua HTTP:
    tep video hang GB ma day qua multipart thi cho rat lau va ton gap doi dung luong dia.
    """
    from utils.native_dialogs import ask_open_filename, ask_open_filenames

    # Nhan CA BA loai: video, anh, nhac — kho nguyen lieu la MOT cho cho tat ca.
    all_exts = tuple(AUD.MEDIA_EXTS) + tuple(VID.IMAGE_EXTS)
    exts = " ".join(f"*{e}" for e in all_exts)
    multiple = bool((data or {}).get("multiple"))
    types = [
        (L("Video, ảnh & nhạc", "Video, images & music"), exts),
        ("Video", " ".join(f"*{e}" for e in AUD.VIDEO_EXTS)),
        (L("Ảnh", "Images"), " ".join(f"*{e}" for e in VID.IMAGE_EXTS)),
        (L("Nhạc", "Music"), " ".join(f"*{e}" for e in AUD.ALLOWED_EXTS)),
        ("All files", "*.*"),
    ]
    title = L("Chọn tệp video, ảnh hoặc nhạc", "Choose a video, image or music file")

    def _pick():
        if multiple:
            return ask_open_filenames(title=title, filetypes=types)
        return ask_open_filename(title=title, filetypes=types)

    try:
        res = await run_in_threadpool(_pick)
    except Exception as e:
        logger.error(f"[nâng cao] Không mở được hộp thoại chọn tệp: {e}")
        raise HTTPException(status_code=500, detail=L("Không mở được hộp thoại chọn tệp", "Could not open the file chooser dialog"))

    if multiple:
        return {"paths": list(res or []), "path": (res or [""])[0] if res else ""}
    return {"path": res or "", "paths": [res] if res else []}


@router.post("/projects/{project_id}/video-clips/pack")
async def pack_video_clips(project_id: str):
    """DON SAT ca lan video: xep cac clip lien nhau, khong ho, khong chong lan.

    Giai quyet dut diem viec "keo tay khong the sat mep": bam mot cai la ca mach phim
    lien mach. Clip dang o lan ngoai (chong lan) cung duoc nhap vao mach theo dung thu
    tu thoi gian dang thay.
    """
    p = _load(project_id)
    _require_advanced(p)
    meta = p["meta"]
    clips = sorted(
        [c for c in VID.list_clips(meta) if c.get("enabled", True)],
        key=lambda c: (VID._num(c.get("start")), c.get("id") or ""),
    )
    if not clips:
        raise HTTPException(status_code=400, detail=L("Làn video chưa có clip nào", "The video lane has no clips yet"))

    ADV.take_snapshot(p["dir"])
    cursor, moved = 0.0, 0
    for c in clips:
        if abs(VID._num(c.get("start")) - cursor) > 0.001:
            moved += 1
        c["start"] = round(cursor, 3)
        cursor += VID.visible_duration(c)
    ADV.save_meta(p["dir"], meta)
    logger.info(f"[nâng cao] Dồn sát làn video: {moved}/{len(clips)} clip đổi vị trí.")
    return {"status": "packed", "moved": moved, "total": len(clips),
            "timeline": await _payload(p)}


@router.post("/projects/{project_id}/audio-clips/from-media")
async def add_clip_from_media(project_id: str, data: dict = Body(...)):
    """Them mot doan audio tu tep TREN MAY — nhan ca tep NHAC lan tep VIDEO.

    Tep video thi RUT phan tieng ra (.m4a) roi dat len timeline nhu mot doan nhac.
    Phan hinh chua dung o chang nay (xem ke hoach: lan video se lam o chang sau).
    """
    p = _load(project_id)
    _require_advanced(p)

    src = (data.get("path") or "").strip().strip('"')
    start = float(data.get("start") or 0.0)
    if not src or not os.path.exists(src):
        raise HTTPException(status_code=404, detail=L("Không tìm thấy tệp đã chọn", "The selected file was not found"))
    ext = os.path.splitext(src)[1].lower()
    if ext not in AUD.MEDIA_EXTS:
        raise HTTPException(
            status_code=400,
            detail=L("Chỉ nhận tệp nhạc (.mp3/.wav/.m4a/.aac/.ogg/.flac) "
                     "hoặc video (.mp4/.mkv/.mov/.avi/.webm…)",
                     "Only music files (.mp3/.wav/.m4a/.aac/.ogg/.flac) "
                     "or video files (.mp4/.mkv/.mov/.avi/.webm…) are accepted"),
        )

    name = os.path.basename(src)
    if AUD.is_video(src):
        if not await run_in_threadpool(AUD.has_audio_stream, src):
            raise HTTPException(status_code=400, detail=L("Tệp video này không có tiếng", "This video file has no audio"))
        dest = os.path.join(AUD.asset_dir(p["dir"]), f"{AUD.new_id()}.m4a")
        ok = await run_in_threadpool(AUD.extract_audio, src, dest)
        if not ok:
            raise HTTPException(status_code=500, detail=L("Không rút được tiếng từ tệp video", "Could not extract audio from the video file"))
        role = AUD.ROLE_VIDEO_AUDIO
        logger.info(f"[nâng cao] Đã rút tiếng từ video: {name}")
    else:
        import shutil as _shutil

        dest = os.path.join(AUD.asset_dir(p["dir"]), f"{AUD.new_id()}{ext}")
        await run_in_threadpool(_shutil.copy2, src, dest)
        role = AUD.ROLE_MUSIC

    dur = await run_in_threadpool(AUD.media_duration, dest)
    clip = AUD.make_clip(dest, name, start, dur, 0.5 if role == AUD.ROLE_VIDEO_AUDIO else 0.35,
                         role=role)
    meta = p["meta"]
    ADV.take_snapshot(p["dir"])
    meta[AUD.FIELD] = AUD.list_clips(meta) + [clip]
    ADV.save_meta(p["dir"], meta)
    return {"status": "added", "clip": clip, "clips": AUD.timeline_view(meta)}


@router.post("/projects/{project_id}/audio-clips/add-stem")
async def add_stem_clip(project_id: str, data: dict = Body(default={})):
    """Dua stem DA TACH len timeline thanh doan audio chinh duoc.

    ``kind``: ``no_vocals`` (nhac nen) / ``vocals`` (giong goc) / bo trong = ca hai.

    Dua stem len timeline la TAT tieng goc cua video (``source_audio_enabled=False``):
    stem chinh la tieng goc da tach ra, giu ca hai thi nghe doi tieng. Nguoi dung bat
    lai duoc bang cong tac o bang Chuan bi.
    """
    p = _load(project_id)
    _require_advanced(p)

    kind = (data.get("kind") or "").strip()
    kinds = [kind] if kind in ("no_vocals", "vocals") else ["no_vocals", "vocals"]
    start = float(data.get("start") or 0.0)

    labels = {
        "no_vocals": ("Nhạc nền (đã tách)", AUD.ROLE_STEM_MUSIC, 0.5),
        "vocals": ("Giọng gốc (đã tách)", AUD.ROLE_STEM_VOCALS, 0.35),
    }
    # Kiem du DIEU KIEN truoc khi chup anh: chup roi moi bao loi se lam xoa mat ngan
    # xep "lam lai" ma chang thay doi gi.
    paths = {k: os.path.join(p["dir"], "separated", f"{k}.wav") for k in kinds}
    if not all(os.path.exists(v) for v in paths.values()):
        raise HTTPException(
            status_code=400,
            detail=L("Chưa tách nhạc nền cho dự án này — hãy bấm “Tách ngay” trước.", "Background music has not been separated for this project — click “Separate now” first."),
        )

    meta = p["meta"]
    added = []
    ADV.take_snapshot(p["dir"])
    for k in kinds:
        label, role, vol = labels[k]
        # Da co doan stem nay tren timeline thi khong them trung lan nua.
        if any(c.get("role") == role for c in AUD.list_clips(meta)):
            continue
        dur = await run_in_threadpool(AUD.media_duration, paths[k])
        added.append(AUD.make_clip(paths[k], label, start, dur, vol, role=role))

    if added:
        meta[AUD.FIELD] = AUD.list_clips(meta) + added
    meta["source_audio_enabled"] = False
    ADV.save_meta(p["dir"], meta)
    return {
        "status": "added", "added": len(added),
        "clips": AUD.timeline_view(meta),
        "source_audio_enabled": False,
    }


@router.patch("/projects/{project_id}/audio-clips/{clip_id}")
async def patch_audio_clip(project_id: str, clip_id: str, data: dict = Body(...)):
    p = _load(project_id)
    _require_advanced(p)
    # Chup vao BO NHO truoc, chi ghi vao ngan xep khi thao tac da xong: bam nham mot
    # clip khong ton tai thi khong an mot buoc hoan tac rong (xem ADV.capture_snapshot).
    snap = ADV.capture_snapshot(p["dir"])
    try:
        AUD.patch_clip(p["meta"], clip_id, data or {})
    except KeyError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy đoạn nhạc", "Music segment not found"))
    ADV.commit_snapshot(p["dir"], snap)
    ADV.save_meta(p["dir"], p["meta"])
    return {"status": "ok", "clips": AUD.timeline_view(p["meta"])}


@router.delete("/projects/{project_id}/audio-clips/{clip_id}")
async def remove_audio_clip(project_id: str, clip_id: str):
    p = _load(project_id)
    _require_advanced(p)
    snap = ADV.capture_snapshot(p["dir"])
    if not AUD.delete_clip(p["meta"], clip_id):
        raise HTTPException(status_code=404, detail=L("Không tìm thấy đoạn nhạc", "Music segment not found"))
    ADV.commit_snapshot(p["dir"], snap)
    ADV.save_meta(p["dir"], p["meta"])
    return {"status": "deleted", "clips": AUD.timeline_view(p["meta"])}


@router.get("/projects/{project_id}/nhac-kho/media")
async def stream_nhac_kho(project_id: str, request: Request, path: str = ""):
    """Nghe thử MỘT BÀI TRONG KHO NHẠC — bài chưa nằm trên timeline.

    VÌ SAO KHÔNG DÙNG ``/videos/file``: cửa đó trả ``FileResponse`` thường, KHÔNG xử
    lý Range — đúng lý do mà nghe thử nhạc nền trước đây không ra tiếng (xem ghi chú
    ở ``stream_audio_clip``). Trình duyệt đặt ``currentTime`` là tua thất bại và im
    luôn, không báo lỗi gì.

    VÌ SAO KHÔNG NHẬN ĐƯỜNG DẪN BẤT KỲ: tham số ``path`` do giao diện gửi lên. Chỉ
    cho phép ĐÚNG hai nơi mà preset nhạc được phép trỏ tới — thư mục dự án và kho
    preset chung — giống hệt luật của cửa "thêm nhạc từ preset" ngay bên dưới.
    """
    from models.database import get_app_data_dir
    from services.compositor_preset_service import PRESET_STORE_DIR

    p = _load(project_id)
    duong = (path or "").strip()
    if not duong or not os.path.exists(duong):
        raise HTTPException(status_code=404, detail=L("Tệp nhạc không còn trên đĩa", "The music file is no longer on disk"))
    store_root = os.path.join(get_app_data_dir(), PRESET_STORE_DIR)
    if not (
        AUD.is_inside_project(p["dir"], duong)
        or AUD.is_inside_project(store_root, duong)
    ):
        raise HTTPException(status_code=400, detail=L("Tệp nhạc nằm ngoài kho cho phép", "The music file is outside the allowed library"))
    return _range_response(duong, request, AUD.mime_of(duong))


@router.get("/projects/{project_id}/audio-clips/{clip_id}/media")
async def stream_audio_clip(project_id: str, clip_id: str, request: Request):
    """Phat tep nhac cua 1 doan — CO Range de trinh duyet tua duoc.

    Truoc day nghe thu nhac nen khong ra tieng chinh vi phat qua /videos/file (khong
    ho tro Range): trinh dung luon dat currentTime theo con tro nen tua that bai.
    """
    meta = _load_meta(project_id)
    clip = AUD.find_clip(meta, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy đoạn nhạc", "Music segment not found"))
    path = clip.get("path") or ""
    pdir = ADV.project_dir_of(project_id)
    if not AUD.is_inside_project(pdir, path) or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=L("Tệp nhạc không còn trên đĩa", "The music file is no longer on disk"))
    return _range_response(path, request, AUD.mime_of(path))


# --------------------------------------------------------------------------- #
#  LAN VIDEO — video goc cung chi la mot clip, keo/cat/de len duoc
# --------------------------------------------------------------------------- #
@router.post("/projects/{project_id}/video-clips")
async def add_video_clip(project_id: str, data: dict = Body(...)):
    """Them mot clip video tu tep tren may — CHI vao DAU hoac CUOI mach phim.

    ``position``: ``"end"`` (mac dinh) noi ngay sau clip cuoi; ``"start"`` dat len dau
    va day moi clip dang co lui ve sau. Khong chen vao giua — chen giua se lam moi thu
    phia sau chong lan nhau, roi nguoi dung phai don tay tung clip.
    """
    p = _load(project_id)
    _require_advanced(p)

    src = (data.get("path") or "").strip().strip('"')
    if not src or not os.path.exists(src):
        raise HTTPException(status_code=404, detail=L("Không tìm thấy tệp video đã chọn", "The selected video file was not found"))
    # Nhan ca ANH: lan video chua duoc ca video lan anh (anh giu tren man may giay), va
    # preset "mo dau/ket thuc" cung co the la mot tam anh.
    if not AUD.is_video(src) and not VID.is_image(src):
        raise HTTPException(
            status_code=400,
            detail=L("Chỉ nhận tệp video (.mp4/.mkv/.mov/…) hoặc ảnh (.jpg/.png/.webp/…)", "Only video files (.mp4/.mkv/.mov/…) or images (.jpg/.png/.webp/…) are accepted"),
        )

    info = await run_in_threadpool(VID.probe_video, src)
    if not VID.is_image(src) and info["duration"] <= 0:
        raise HTTPException(status_code=400, detail=L("Không đọc được tệp video này", "Cannot read this video file"))

    # ``at`` = CHEN vao giua mach phim tai giay ``data["at"]`` (xem VID.insert_clip_at).
    position = str(data.get("position") or "end")
    if position not in ("start", "end", "at"):
        position = "end"
    meta = p["meta"]
    clip = VID.make_clip(src, str(data.get("name") or "").strip() or os.path.basename(src),
                         0.0, info=info, seconds=data.get("seconds"))
    # Chen tu PRESET thi mang theo dung cac thong so da luu (toc do, am luong, 4 muc) —
    # khong thi doan mo dau chen vao du an moi lai nghe khac han luc luu.
    for key in ("speed", "volume", "muted"):
        if data.get(key) is not None:
            clip[key] = data[key]
    # KEP NGAY TAI DAY, truoc khi noi vao mach phim. ``append_clip(position="start")`` day
    # moi clip cu lui theo ``visible_duration(clip)``, ma do dai do phu thuoc ``speed`` —
    # kep sau (o VID.patch_clip ben duoi) thi preset co toc do ngoai khoang 0.5-2.0 lam
    # lan video va lan giong lech nhau dung phan chenh, khong ai hieu vi sao.
    clip["speed"] = 1.0 if VID.clip_is_image(clip) else round(VID.clip_speed(clip), 3)
    clip["volume"] = max(0.0, min(2.0, VID._num(clip.get("volume"), 1.0)))
    clip["muted"] = bool(clip.get("muted", False))
    if isinstance(data.get("mix"), dict):
        clip[VID.MIX_FIELD] = {
            k: max(0.0, min(1.0, VID._num(v, 0.0)))
            for k, v in data["mix"].items()
            if k in VID.MIX_KEYS and v is not None
        } or None
        if not clip[VID.MIX_FIELD]:
            clip.pop(VID.MIX_FIELD, None)
    ADV.take_snapshot(p["dir"])
    # Du ba khoa ngay tu dau: ben duoi doc ``shifted.get("subs")`` o MOI nhanh, ke ca
    # nhanh "them vao cuoi" (khong doi gi ca).
    shifted = {"clips": 0, "audio": 0, "subs": 0}
    if position == "at":
        # CHEN TAI CON TRO: cat clip dang phu diem do, day phan sau lui — ke ca GIONG DOC
        # va NHAC (xem VID.insert_clip_at + ADV.shift_after).
        at = max(0.0, float(data.get("at") or 0.0))
        VID.insert_clip_at(meta, clip, at)
        VID.patch_clip(meta, clip["id"], {})
        shifted = ADV.shift_after(
            p["dir"], meta, p["a_lookup"], p["b_chunks"], at, VID.visible_duration(clip)
        )
        ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    else:
        VID.append_clip(meta, clip, position)
        # Kep lai cho chac (toc do ngoai khoang, am luong am…) — dung ham kep san co.
        VID.patch_clip(meta, clip["id"], {})
        if position == "start":
            # Chen vao DAU thi phai day CA giong doc va nhac lui theo, khong thi loi doc
            # lech di dung bang do dai doan vua chen.
            shifted = ADV.shift_everything(
                p["dir"], meta, p["a_lookup"], p["b_chunks"], VID.visible_duration(clip)
            )
            ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    ADV.save_meta(p["dir"], meta)
    logger.info(
        f"[nâng cao] Thêm clip video vào "
        f"{'đầu' if position == 'start' else 'cuối' if position == 'end' else 'giữa (tại con trỏ)'}"
        f": {clip['name']} tại {clip['start']:.2f}s"
    )
    # Da doi DAI PHU DE GOC thi phai gui kem no: goi tin cua cac endpoint SUA mac dinh
    # bo a_chunks va FE giu lai ban dang co -> khong gui thi dai phu de goc ve o cho cu
    # cho toi luc tai lai trang.
    return {"status": "added", "clip": clip, "shifted": shifted,
            "timeline": await _payload(p, include_a=bool(shifted.get("subs")))}


@router.post("/projects/{project_id}/video-clips/{clip_id}/duplicate")
async def duplicate_video_clip(project_id: str, clip_id: str, data: dict = Body(default={})):
    """Nhan ban 1 clip video (Ctrl+C/V, Ctrl+D) — giu nguyen doan da cat va toc do.

    Dat tai ``at`` (mac dinh: ngay sau clip goc). Roi vao cho da co clip khac thi ban
    sao se nam o LAN NGOAI voi mau canh bao, keo di la vao lai lan chinh.
    """
    p = _load(project_id)
    _require_advanced(p)
    src = VID.find_clip(p["meta"], clip_id)
    if not src:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))

    copy = dict(src)
    copy["id"] = VID.new_id()
    # Ban sao NONG dung chung dict 4 muc voi clip goc -> tach ra (xem VID.split_clip).
    if isinstance(copy.get(VID.MIX_FIELD), dict):
        copy[VID.MIX_FIELD] = dict(copy[VID.MIX_FIELD])
    at = data.get("at")
    copy["start"] = round(
        float(at) if at is not None else VID.clip_end(src), 3,
    )
    ADV.take_snapshot(p["dir"])
    p["meta"][VID.FIELD] = VID.list_clips(p["meta"]) + [copy]
    ADV.save_meta(p["dir"], p["meta"])
    return {"status": "duplicated", "clip": copy, "timeline": await _payload(p)}


@router.patch("/projects/{project_id}/video-clips/{clip_id}")
async def patch_video_clip(project_id: str, clip_id: str, data: dict = Body(...)):
    p = _load(project_id)
    _require_advanced(p)
    data = data or {}
    snap = ADV.capture_snapshot(p["dir"])

    def _find():
        return next((c for c in VID.list_clips(p["meta"]) if c.get("id") == clip_id), None)

    before = _find()
    old_start = VID._num((before or {}).get("start"), 0.0)
    old_len = VID.visible_duration(before) if before else 0.0

    try:
        VID.patch_clip(p["meta"], clip_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))

    # DOI TOC DO = phim dai/ngan ra -> keo luon cac cau binh luan theo cho khoi lech
    # canh. Chi lam khi that su doi TOC DO: keo vi tri hay cat dau/cuoi cung lam doi do
    # dai, nhung do la thao tac khac han, dong loi binh theo la ngoai y muon.
    # ``reflow: false`` de nguoi dung tat khi chi muon doi rieng toc do hinh.
    # BA KHOA LUC NAO CUNG DAY DU. Truoc do dict nay thieu "subs" o duong mac dinh, ma
    # ben duoi lai doc ``reflow["subs"]`` -> KeyError, tuc MOI thao tac tren clip video
    # khong phai doi toc do (keo tha, chinh am luong, cat) deu 500.
    reflow = {"moved": 0, "unsped": 0, "subs": 0}
    want_reflow = data.get("reflow") is not False
    if "speed" in data and want_reflow:
        new_len = VID.visible_duration(_find())
        reflow = await run_in_threadpool(
            ADV.reflow_for_span_change,
            p["dir"], p["b_chunks"], p["a_lookup"], old_start, old_len, new_len,
        )
        # CAC CLIP PHIA SAU cung phai lui/tien theo. Noi clip ra ma chung dung yen
        # la chong len nhau -> cai sau roi xuong hang ngoai, thoi khong phat va
        # khong vao ban xuat. Loi binh va phu de goc phia sau da duoc
        # ``reflow_for_span_change`` doi bang dung ngan ay roi.
        VID.shift_clips_after(
            p["meta"], old_start + old_len, new_len - old_len, exclude_id=clip_id
        )
    elif "start" in data and want_reflow:
        # DOI CHO clip -> loi binh thuoc ve doan phim do di theo. Chi cham vao phan NAM
        # TRONG doan cu, thu dung truoc/sau deu yen.
        #
        # Keo tha tren timeline CO Y gui ``reflow: false`` (xem onCommitVideo ben FE):
        # keo de sap xep lai lan hinh la thao tac hang ngay, dinh ca tram cau theo moi
        # lan keo la khong ai chiu noi. O bang thuoc tinh thi co o tick han hoi.
        delta = VID._num((_find() or {}).get("start"), 0.0) - old_start
        if abs(delta) > 0.005 and old_len > 0.01:
            r = await run_in_threadpool(
                ADV.shift_window, p["dir"], p["meta"], p["a_lookup"], p["b_chunks"],
                old_start, old_start + old_len, delta,
            )
            reflow = {"moved": r["clips"], "unsped": 0, "subs": r["subs"]}

    ADV.commit_snapshot(p["dir"], snap)
    ADV.save_meta(p["dir"], p["meta"])
    if reflow.get("moved"):
        ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    # Da gian ca DAI PHU DE GOC thi phai gui kem no. Goi tin cua cac endpoint SUA mac
    # dinh bo a_chunks (chung khong doi) va FE giu lai ban dang co — khong gui thi dai
    # phu de goc van ve o cho cu cho toi luc tai lai trang.
    return {"status": "ok", **reflow,
            "timeline": await _payload(p, include_a=bool(reflow.get("subs")))}


@router.get("/projects/{project_id}/video-clips/{clip_id}/subs-inside")
async def count_subs_inside_clip(project_id: str, clip_id: str):
    """Bao truoc: xoa clip nay thi CO BAO NHIEU cau phu de goc nam trong khoang cua no.

    FE hoi truoc khi hien hop xac nhan — de nut tick "xoa luon N cau phu de goc" noi ra
    duoc con so that, thay vi mot loi de doa chung chung.
    """
    p = _load(project_id)
    _require_advanced(p)
    clip = VID.find_clip(p["meta"], clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    lo, hi = SEG.clip_range(clip)
    goc = SEG.a_chunks_in_range(SEG.load_a_chunks(p["dir"]), lo, hi)
    binh = SEG.b_chunks_in_range(p["b_chunks"], p["a_lookup"], lo, hi)
    return {
        "count": len(goc),
        # Loi binh trong doan — de hop xac nhan hoi luon ca cai nay. Bo phim di ma de
        # loi binh o lai thi no roi vao dung cho vua bi keo ve, chong len cau khac.
        "voice_count": len(binh),
        "voice_has_audio": sum(1 for b in binh if b.get("has_audio") or b.get("audio_path")),
        "start": lo,
        "end": hi,
    }


@router.get("/projects/{project_id}/orphan-subs")
async def scan_orphan_subs(project_id: str):
    """Dem cau phu de goc khong con hinh nao nam duoi."""
    p = _load(project_id)
    _require_advanced(p)
    mo_coi = SEG.orphan_a_chunks(SEG.load_a_chunks(p["dir"]), p["meta"])
    return {
        "count": len(mo_coi),
        # Vai cau dau de nguoi dung nhin thay minh sap xoa cai gi.
        "preview": [
            {"a_id": c.get("a_id"), "start": c.get("start"), "text": (c.get("text") or "")[:80]}
            for c in mo_coi[:5]
        ],
    }


@router.post("/projects/{project_id}/orphan-subs/clean")
async def clean_orphan_subs(project_id: str):
    """Xoa cac cau phu de goc khong con hinh nam duoi. Hoan tac duoc bang Ctrl+Z."""
    p = _load(project_id)
    _require_advanced(p)
    mo_coi = SEG.orphan_a_chunks(SEG.load_a_chunks(p["dir"]), p["meta"])
    if not mo_coi:
        return {"status": "ok", "deleted": 0, "timeline": await _payload(p)}
    ADV.take_snapshot(p["dir"])
    r = SEG.delete_a_chunks(p["dir"], p["a_lookup"], p["b_chunks"], mo_coi)
    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    return {"status": "ok", **r, "timeline": await _payload(p, include_a=True)}


@router.delete("/projects/{project_id}/video-clips/{clip_id}")
async def remove_video_clip(
    project_id: str, clip_id: str, reflow: bool = True,
    drop_subs: bool = False, drop_voice: bool = False,
):
    """Xoa 1 clip video. Mac dinh KEO PHAN SAU VE lap cho trong.

    NGHICH DAO CUA VIEC CHEN — thieu no la mot lech cap that su (nguoi dung da bao):
    them mot clip vao dau thi ``shift_everything`` day CA giong doc va nhac lui theo, ma
    xoa clip do di lai khong keo gi ve ca. Ket qua: doan phim bien mat nhung loi binh o
    lai nguyen cho da bi day, treo lo lung o giua mach phim va khong co cach nao dua ve
    ngoai keo tay tung cau.

    Chi keo nhung thu nam TU MEP CUOI clip tro di, va CHI giong doc + nhac — dung bang
    thu ma buoc chen da day. Khong dung vao ``a_chunks``, cung vi le do.

    ``reflow=false`` de xoa ma giu nguyen moi moc (khi chi muon bo mot clip chong lan).
    """
    p = _load(project_id)
    _require_advanced(p)
    snap = ADV.capture_snapshot(p["dir"])

    gone = VID.find_clip(p["meta"], clip_id)
    if gone is None:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    gap_at = VID.clip_end(gone)                 # mép CUỐI của clip sắp xoá
    gap_len = VID.visible_duration(gone)
    # CHI clip dang chiem cho tren mach phim moi de lai khoang trong can lap. Clip tat
    # tam / dang chong lan thi khong phat va khong vao ban xuat — xoa no di mach phim
    # khong ngan lai mot giay nao, keo loi binh ve la day lech het.
    occupies = VID.occupies_timeline(p["meta"], clip_id)

    # XOA LUON PHU DE GOC nam trong doan (nguoi dung tick o hop xac nhan).
    #
    # Phai lam TRUOC ``shift_after``: xoa xong roi moi keo thi khoang can xoa da bi dich
    # di, tim theo moc cu la xoa nham cau cua doan ben canh. Ham ``delete_a_chunks`` tu
    # dong dinh moc cho cau binh luan truoc khi xoa, nen loi binh khong nhay cho.
    dropped = {"deleted": 0}
    voice_gone = 0
    if drop_subs or drop_voice:
        lo, hi = SEG.clip_range(gone)
        # LOI BINH truoc: xoa cau A se dong dinh moc cho cau B, ma cau B sap xoa thi
        # dong dinh lam gi. Voi lai ``b_chunks_in_range`` con phai suy moc QUA cau A —
        # xoa A truoc la khong con tim ra cau B nao thuoc doan nay.
        if drop_voice:
            binh = SEG.b_chunks_in_range(p["b_chunks"], p["a_lookup"], lo, hi)
            for b in binh:
                p["b_chunks"] = TL.delete_chunk(p["b_chunks"], b.get("uid"))
            voice_gone = len(binh)
        if drop_subs:
            ben_trong = SEG.a_chunks_in_range(SEG.load_a_chunks(p["dir"]), lo, hi)
            if ben_trong:
                dropped = SEG.delete_a_chunks(
                    p["dir"], p["a_lookup"], p["b_chunks"], ben_trong,
                )

    VID.delete_clip(p["meta"], clip_id)

    shifted = {"clips": 0, "audio": 0, "subs": 0, "videos": 0}
    if reflow and occupies and gap_len > 0.01:
        shifted = ADV.shift_after(
            p["dir"], p["meta"], p["a_lookup"], p["b_chunks"], gap_at, -gap_len
        )
        # KEO LUON LAN HINH ve — day la nua con thieu. Chen thi ``insert_clip_at``
        # day lan hinh ra, nen xoa phai keo lan hinh ve; khong thi phim con lai treo
        # lo lung sau mot khoang den, con giong doc / phu de vua keo ve lai lech han.
        shifted["videos"] = VID.shift_clips_after(p["meta"], gap_at, -gap_len)
    ADV.commit_snapshot(p["dir"], snap)
    ADV.save_meta(p["dir"], p["meta"])
    if shifted["clips"] or shifted["audio"] or dropped["deleted"] or voice_gone:
        ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    return {"status": "deleted", **shifted, "subs_deleted": dropped["deleted"],
            "voice_deleted": voice_gone,
            "timeline": await _payload(
                p, include_a=bool(shifted.get("subs") or dropped["deleted"]),
            )}


@router.post("/projects/{project_id}/video-clips/{clip_id}/split")
async def split_video_clip(project_id: str, clip_id: str, data: dict = Body(...)):
    """Cat clip video tai giay ``at_time`` tren timeline thanh hai clip doc lap."""
    p = _load(project_id)
    _require_advanced(p)
    # Cho cat khong hop le la ca ValueError — chup roi ghi ngay thi moi lan bam nham
    # (rat hay xay ra vi con tro thuong nam sat mep clip) lai an mot buoc hoan tac rong.
    snap = ADV.capture_snapshot(p["dir"])
    try:
        ids = VID.split_clip(p["meta"], clip_id, float(data.get("at_time") or 0.0))
    except KeyError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ADV.commit_snapshot(p["dir"], snap)
    ADV.save_meta(p["dir"], p["meta"])
    return {"status": "split", "ids": ids, "timeline": await _payload(p)}


@router.post("/projects/{project_id}/video-clips/{clip_id}/save-preset")
async def save_video_clip_preset(project_id: str, clip_id: str, data: dict = Body(...)):
    """Luu DOAN VIDEO dang thay tren timeline thanh PRESET dung lai xuyen du an.

    Dung cho doan mo dau / ket thuc / clip chen hai — thu nao dung lai o moi video.

    CAT DOAN RA MOT TEP RIENG trong kho preset (``-c copy``, khong ma hoa lai) thay vi tro
    vao tep goc: (1) preset chi nang bang doan da cat, (2) xoa du an hay doi cho tep goc thi
    preset van con dung duoc — dung tinh than cua preset nhac. Kem theo ca toc do, am luong
    va 4 muc am thanh, nen chen lai o du an khac la nghe y nhu cu.
    """
    p = _load(project_id)
    _require_advanced(p)
    clip = VID.find_clip(p["meta"], clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    if not os.path.exists(clip.get("path") or ""):
        raise HTTPException(status_code=404, detail=L("Tệp của clip này không còn trên đĩa", "The file of this clip is no longer on disk"))

    name = str(data.get("name") or "").strip() or (clip.get("name") or "Đoạn video")
    seg = VID.visible_duration(clip) * VID.clip_speed(clip)   # do dai THUC cua doan da cat
    if seg > VID.PRESET_MAX_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Đoạn này dài {seg / 60:.1f} phút — preset là để dùng lại đoạn NGẮN "
                f"(mở đầu, kết thúc, clip chèn). Hãy cắt ngắn còn dưới "
                f"{VID.PRESET_MAX_SECONDS // 60} phút rồi lưu."
            ),
        )

    from services.compositor_preset_service import _preset_asset_dir, add_personal_preset
    import time as _time

    is_img = VID.clip_is_image(clip)
    if is_img:
        # ANH: SAO nguyen tep, KHONG cat bang ffmpeg. ``-c copy`` tren mot tep anh chi ra
        # dung MOT khung (khong co -loop 1) nen preset se la mot .mp4 hong, chen lai thi
        # nhay mot cai roi mat. Anh cung chi vai tram KB nen sao ca tep la re.
        import shutil as _shutil

        ext = os.path.splitext(clip["path"])[1].lower() or ".png"
        dest = os.path.join(
            _preset_asset_dir("video"), f"preset_video_{int(_time.time() * 1000)}{ext}"
        )
        try:
            await run_in_threadpool(_shutil.copy2, clip["path"], dest)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Không sao được ảnh: {e}")
    else:
        dest = os.path.join(
            _preset_asset_dir("video"), f"preset_video_{int(_time.time() * 1000)}.mp4"
        )
        ok = await run_in_threadpool(VID.export_segment, clip, dest)
        if not ok:
            raise HTTPException(
                status_code=500, detail=L("Không cắt được đoạn video để lưu preset", "Could not cut the video segment to save the preset")
            )

    try:
        preset, _store = add_personal_preset(
            "video",
            name,
            {
                "path": dest.replace("\\", "/"),
                "name": name,
                "duration": round(seg, 3),
                # Anh: chen lai phai ra clip ANH giu dung so giay nay, khong phai clip video.
                "is_image": is_img,
                # Giu nguyen cac thong so nguoi dung da chinh -> chen lai la nghe/thay y het.
                "speed": VID.clip_speed(clip),
                "volume": VID._num(clip.get("volume"), 1.0),
                "muted": bool(clip.get("muted", False)),
                "mix": VID.clip_mix(p["meta"], clip) if VID.has_own_mix(clip) else None,
                "width": int(VID._num(clip.get("width"), 0)),
                "height": int(VID._num(clip.get("height"), 0)),
            },
        )
    except ValueError as e:
        # Ghi preset that bai -> don luon doan vua cat, dung de rac trong AppData.
        try:
            os.remove(dest)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(f"[nâng cao] Đã lưu preset video “{name}” ({seg:.1f}s).")
    return {"status": "saved", "preset": preset}


# --------------------------------------------------------------------------- #
#  TACH GIONG & NHAC NEN cho TUNG CLIP VIDEO
# --------------------------------------------------------------------------- #
def _separate_clip_wrapper(project_id: str, clip_id: str):
    """Chay nen: tach stem CHO MOT CLIP + ghi log vao synthesis.log cho FE theo doi."""
    try:
        #  XẾP HÀNG cùng mọi việc nặng khác (services/work_queue.py).
        with WQ.slot(project_id, L("Tách giọng đoạn phim", "Separate vocals of the clip"),
                     cancelled=lambda: review_service.is_render_cancelled(project_id)):
            handler = setup_project_logging(project_id, config_service.get_review_root())
            log = logging.getLogger("SeparationService")
            try:
                from services.separation_service import SeparationService

                p = ADV.load_project(project_id, migrate=False)
                clip = VID.find_clip(p["meta"], clip_id)
                if not clip:
                    log.error(f"Không tìm thấy clip {clip_id} để tách.")
                    return
                src = clip.get("path") or ""
                out_dir = VID.clip_stems_dir(p["dir"], clip)
                cancel_marker = os.path.join(p["dir"], ".cancel_render")
                log.info(f"=== BẮT ĐẦU TÁCH GIỌNG & NHẠC NỀN cho clip: {clip.get('name')} ===")
                # Danh dau DANG GHI trong suot luc tach: Demucs ghi hai tep WAV lon dan, khong co
                # nhan nay thi ngay giay dau tien moi cho da tin la "da tach xong" — bang thuoc
                # tinh doi sang 4 muc stem, khung xem truoc nghe tep con do dang, va bam Xuat luc
                # do thi ban xuat mat tieng phan duoi clip ma khong bao loi.
                VID.mark_separating(out_dir)
                try:
                    SeparationService.ensure_stems(
                        src, out_dir, check_cancel=lambda: os.path.exists(cancel_marker),
                    )
                except BaseException:
                    # Loi hoac nguoi dung huy -> xoa han ban do dang, dung de lai cho lan sau tin.
                    VID.discard_stems(out_dir)
                    raise
                VID.clear_separating(out_dir)
                log.info(f"=== TÁCH XONG cho clip: {clip.get('name')} ===")
            except Exception as e:
                log.error(f"Tách giọng & nhạc nền cho clip thất bại: {e}", exc_info=True)
            finally:
                remove_project_logging(project_id, handler)
                _release_review(project_id)


    except WQ.WorkCancelled:
        #  Bấm Huỷ khi việc CÒN ĐANG XẾP HÀNG: thân hàm chưa chạy nên phần
        #  dọn dẹp trong ``finally`` của nó cũng chưa chạy — phải tự dọn ở
        #  đây, không thì khoá dự án không ai nhả và giao diện treo mãi.
        logger.warning(f"[HÀNG ĐỢI] Đã huỷ khi còn đang xếp hàng: {project_id}")
        _release_review(project_id)
@router.post("/projects/{project_id}/video-clips/{clip_id}/separate")
async def separate_video_clip(project_id: str, clip_id: str, background_tasks: BackgroundTasks):
    """Tach giong & nhac nen cho MOT clip video tren timeline.

    Nho vay MOI video them vao deu ha rieng duoc nhac nen / giong goc, khong chi video goc.
    Ket qua nam o ``separated_clips/<bam theo tep nguon>/`` — bam theo TEP chu khong theo id
    clip, nen cat mot clip lam nhieu manh (hoac nhan ban) chi phai tach MOT lan. Manh cua
    video goc thi dung lai luon ban tach o cap du an neu da co.
    """
    p = _load(project_id)
    _require_advanced(p)
    clip = VID.find_clip(p["meta"], clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    if not os.path.exists(clip.get("path") or ""):
        raise HTTPException(status_code=404, detail=L("Tệp của clip này không còn trên đĩa", "The file of this clip is no longer on disk"))

    from services.separation_service import SeparationService

    _vp = _source_video(p["meta"])
    if VID.clip_stems(p["dir"], clip, _vp):
        return {"status": "ready", "cached": True}
    if not await run_in_threadpool(AUD.has_audio_stream, clip["path"]):
        raise HTTPException(status_code=400, detail=L("Clip này không có đường tiếng để tách", "This clip has no audio track to separate"))

    _acquire_review_or_409(project_id)
    try:
        review_service.clear_render_cancel(project_id)
        background_tasks.add_task(_separate_clip_wrapper, project_id, clip_id)
    except Exception:
        _release_review(project_id)
        raise
    return {"status": "started", "cached": False}


@router.get("/projects/{project_id}/video-clips/{clip_id}/stem/{kind}")
async def stream_video_clip_stem(project_id: str, clip_id: str, kind: str, request: Request):
    """Phat 1 stem da tach CUA MOT CLIP — de khung xem truoc nghe dung 4 muc rieng."""
    if kind not in VID.STEM_KINDS:
        raise HTTPException(status_code=400, detail=L("Stem không hợp lệ", "Invalid stem"))
    meta = _load_meta(project_id)
    clip = VID.find_clip(meta, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    pdir = ADV.project_dir_of(project_id)
    stems = VID.clip_stems(pdir, clip, _source_video(meta))
    if not stems:
        raise HTTPException(status_code=404, detail=L("Clip này chưa tách giọng & nhạc nền", "Vocals and background music have not been separated for this clip"))
    return _range_response(stems[kind], request, "audio/wav")


@router.get("/projects/{project_id}/video-clips/{clip_id}/media")
async def stream_video_clip(project_id: str, clip_id: str, request: Request):
    """Phat tep cua 1 clip video (CO Range de trinh duyet tua duoc).

    Cho phep phat tep NGOAI thu muc du an: clip video la tep nguoi dung tu chon tren
    may minh, khong sao vao du an (video hang GB, nhan ban ra la het dia). Bu lai chi
    phat dung nhung duong dan DANG NAM trong timeline cua du an nay.
    """
    meta = _load_meta(project_id)
    clip = VID.find_clip(meta, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    path = clip.get("path") or ""
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=L("Tệp video không còn trên đĩa", "The video file is no longer on disk"))
    # Clip ANH phai tra dung kieu ANH: khung xem truoc ve no bang <img>, ma <img> nhan
    # Content-Type video/mp4 thi khong giai ma duoc -> o trong tron.
    if VID.clip_is_image(clip):
        ext = os.path.splitext(path)[1].lower()
        mime = {
            ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(ext, "image/jpeg")
        return _range_response(path, request, mime)
    return _range_response(path, request, "video/mp4")


class _XinSongAm(BaseModel):
    """Danh sach id can lay song am trong MOT luot."""
    ids: List[str] = []
    points: int = 0


def _nhieu_song_am(cap: list, points: int) -> dict:
    """Doc song am cho NHIEU tep cung luc.

    ``waveform_peaks`` moi lan chua co cache la mot lan goi ffmpeg, nen chay tuan tu
    ca tram cau thi cho rat lau. Vai luong song song la du: co cache roi thi chi la
    doc JSON, con lan dau thi ffmpeg an CPU nen nhieu luong hon cung khong nhanh them.
    """
    ra = {}
    if not cap:
        return ra
    with ThreadPoolExecutor(max_workers=min(6, len(cap))) as bo:
        cho = {bo.submit(ADV.waveform_peaks, duong, points): ma for ma, duong in cap}
        for xong in as_completed(cho):
            try:
                ra[cho[xong]] = xong.result()
            except Exception:
                ra[cho[xong]] = []
    return ra


@router.post("/projects/{project_id}/clips/waveforms")
async def clip_waveforms(project_id: str, body: _XinSongAm):
    """Song am cua NHIEU cau trong MOT luot.

    Vi sao phai co: timeline ve moi cau mot the <Waveform>, moi the tu goi mot request.
    Du an 20 phut = 304 cau la 304 request ban ra cung luc, ma trinh duyet chi mo 6
    ket noi mot luc -> hang tram request xep hang, VA moi request lai ``_load`` ca
    a_chunks/b_chunks cua du an. Gom lai thi con dung MOT request va MOT lan doc.
    """
    p = _load(project_id)
    points = body.points if body.points else 120
    cap = []
    for uid in body.ids[:2000]:
        b = TL.find_chunk(p["b_chunks"], uid)
        if b is None:
            continue
        cap.append((uid, TL.clip_audio_path(p["dir"], b)))
    peaks = await run_in_threadpool(_nhieu_song_am, cap, points)
    return {"peaks": peaks}


@router.post("/projects/{project_id}/audio-clips/waveforms")
async def audio_clip_waveforms(project_id: str, body: _XinSongAm):
    """Nhu tren, cho cac doan nhac tren lan audio."""
    meta = _load_meta(project_id)
    pdir = ADV.project_dir_of(project_id)
    points = body.points if body.points else 200
    cap = []
    for cid in body.ids[:2000]:
        clip = AUD.find_clip(meta, cid)
        if not clip:
            continue
        duong = clip.get("path") or ""
        if not AUD.is_inside_project(pdir, duong):
            continue
        cap.append((cid, duong))
    peaks = await run_in_threadpool(_nhieu_song_am, cap, points)
    return {"peaks": peaks}


@router.get("/projects/{project_id}/clips/{uid}/waveform")
async def clip_waveform(project_id: str, uid: str, points: int = 120):
    """Duong song am cua 1 cau B — de nhin phat biet cho nao co tieng.

    Tra ve mang 0..1 da rut gon (mac dinh 120 diem) va CO CACHE ben canh file audio,
    vi doc lai ca file wav moi lan ve timeline thi rat cham.
    """
    p = _load(project_id)
    b = TL.find_chunk(p["b_chunks"], uid)
    if b is None:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy câu", "Sentence not found"))
    path = TL.clip_audio_path(p["dir"], b)
    peaks = await run_in_threadpool(ADV.waveform_peaks, path, points)
    return {"uid": uid, "peaks": peaks}


@router.get("/projects/{project_id}/audio-clips/{clip_id}/waveform")
async def audio_clip_waveform(project_id: str, clip_id: str, points: int = 200):
    meta = _load_meta(project_id)
    clip = AUD.find_clip(meta, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy đoạn nhạc", "Music segment not found"))
    pdir = ADV.project_dir_of(project_id)
    path = clip.get("path") or ""
    if not AUD.is_inside_project(pdir, path):
        raise HTTPException(status_code=404, detail=L("Tệp nhạc không hợp lệ", "Invalid music file"))
    peaks = await run_in_threadpool(ADV.waveform_peaks, path, points)
    return {"id": clip_id, "peaks": peaks}


# --------------------------------------------------------------------------- #
#  LAM VIEC THEO TUNG DOAN: lay sub cho MOT clip roi dich rieng doan do
#
#  Vi sao khong dung lai `POST /api/review/projects/{id}/analyze` cua duong cu: ham do
#  chay tren CA video goc roi `generate_a_chunks` GHI DE a_chunks.json va danh so lai tu
#  1. Nguoi dung ghep phim dan tung doan (them clip -> lay sub cho clip do -> dich) thi
#  moi lan chay lai la xoa sach cau goc cu VA doi id, keo theo moi cau B tro sang cau A
#  khac — video doc dung tren doan sai ma khong bao gi. Duong nay CHI CONG THEM.
# --------------------------------------------------------------------------- #
def _read_clip_subs_wrapper(project_id: str, clip_id: str, mode: str, region):
    """Chay nen: doc sub CHI TRONG khoang cua mot clip roi cong them cau goc."""
    try:
        #  XẾP HÀNG cùng mọi việc nặng khác (services/work_queue.py).
        with WQ.slot(project_id, L("Lấy phụ đề đoạn phim", "Get subtitles of the clip"),
                     cancelled=lambda: review_service.is_render_cancelled(project_id)):
            handler = setup_project_logging(project_id, config_service.get_review_root())
            log = logging.getLogger("ReviewService")
            try:
                p = ADV.load_project(project_id, migrate=False)
                clip = VID.find_clip(p["meta"], clip_id)
                if not clip:
                    log.error(f"Không tìm thấy clip {clip_id} để lấy phụ đề.")
                    return
                src = clip.get("path") or ""
                t_in = VID._num(clip.get("in"))
                t_out = VID._num(clip.get("out"))
                name = clip.get("name") or clip_id
                log.info(
                    f"=== LẤY PHỤ ĐỀ CHO ĐOẠN “{name}” ({t_in:.1f}s–{t_out:.1f}s của tệp, "
                    f"{mode.upper()}) ==="
                )

                def _check_cancel():
                    if review_service.is_analysis_cancelled(project_id):
                        from services.review_service import AnalysisCancelled

                        raise AnalysisCancelled()

                meta = p["meta"]
                _src_lang = str(meta.get("source_language") or "").lower()
                ocr_lang = (
                    "en" if _src_lang in ("en", "eng", "en-us", "en-gb")
                    else "ja" if _src_lang in ("ja", "ja-jp", "japan")
                    else "vi" if _src_lang in ("vi", "vi-vn")
                    else "ch"
                )

                vung = region if region is not None else meta.get("ocr_region")

                def _ocr(windows):
                    """Doc sub CUNG trong cac cua so (giay CUA TEP NGUON). Tra ve segment tho."""
                    from services.ocr_service import get_ocr_service

                    return get_ocr_service().extract_subtitles(
                        src, region=vung, lang=ocr_lang, check_cancel=_check_cancel,
                        windows=windows,
                    )

                def _stt():
                    """Cat DUNG doan tieng ra roi nghe. Moc tra ve tinh tu 0 cua doan da cat."""
                    from services.video_service import VideoService

                    wav = os.path.join(p["dir"], f"segment_{clip_id}.wav")
                    if not VideoService.extract_audio_slice(
                        src, wav, t_in, max(0.05, t_out - t_in)
                    ):
                        raise RuntimeError(L("Không tách được tiếng của đoạn này để nhận dạng.", "Could not extract the audio of this segment for recognition."))
                    # Don tep tam trong ``finally``: nhan dang loi hoac nguoi dung bam Huy thi
                    # doan .wav vua cat (phim dai co the hang tram MB) nam lai trong thu muc du
                    # an mai mai, khong co gi don ho.
                    try:
                        segs, _lang = review_service.stt_service.transcribe(
                            wav, language=(None if _src_lang in ("", "auto") else _src_lang),
                            check_cancel=_check_cancel,
                        )
                    finally:
                        try:
                            os.remove(wav)
                        except OSError:
                            pass
                    # Doi ve giay CUA TEP NGUON cho cung he voi OCR — che do ket hop phai so
                    # duoc hai ben voi nhau truoc khi doi sang giay timeline.
                    return [
                        {**s, "start": t_in + float(s["start"]), "end": t_in + float(s["end"])}
                        for s in segs
                    ]

                def _ra_timeline(segs):
                    return [
                        {"start": SEG.to_timeline(clip, float(s["start"])),
                         "end": SEG.to_timeline(clip, float(s["end"])),
                         "text": s.get("text") or "",
                         #  Cờ ``ocr`` giữ lại cho bước gộp mẩu ngắn (không trộn OCR
                         #  vá lỗ vào câu Whisper).
                         "ocr": bool(s.get("ocr"))}
                        for s in segs
                    ]

                if mode == "ocr":
                    # OCR nhan ``windows`` -> doc DUNG khoang cua clip tren chinh tep goc, khong
                    # phai cat tep ra truoc.
                    lines = _ra_timeline(_ocr([(t_in, t_out)]))
                elif mode == "hybrid":
                    # KET HOP: nghe truoc, roi CHI OCR nhung cho Whisper bo sot.
                    #
                    # Vi sao dang cong nay chu khong phai chay ca hai roi gop: OCR ca doan la
                    # cham gap nhieu lan Whisper, va cho nao Whisper da nghe duoc thi ban OCR
                    # chi lam sinh ra cau trung. Phim co doan nhac nen / doan khong loi thoai
                    # nhung van co chu tren hinh (bang chu, doc thoai noi tam) thi day dung la
                    # cho OCR bu vao.
                    #
                    # ``compute_whisper_gaps`` tinh tren truc 0..duration nen phai dich ve goc
                    # cua doan roi cong tra lai, khong thi cua so OCR roi ra ngoai clip.
                    from services.ocr_service import compute_whisper_gaps, overlaps_any

                    log.info("[nâng cao] Kết hợp — [1/2] nghe bằng Whisper…")
                    wh = _stt()
                    dai = max(0.05, t_out - t_in)
                    gaps = compute_whisper_gaps(
                        [{"start": s["start"] - t_in, "end": s["end"] - t_in} for s in wh], dai,
                    )
                    if not gaps:
                        log.info("[nâng cao] Kết hợp — [2/2] Whisper đã phủ kín đoạn, BỎ QUA OCR.")
                        bu = []
                    elif not vung:
                        # Khong the OCR ma khong co vung — nhung phan nghe duoc thi van giu.
                        log.warning(
                            L("[nâng cao] Kết hợp — chưa khoanh vùng phụ đề cứng nên BỎ bước OCR "
                              "vá lỗ; chỉ giữ phần nghe được.",
                              "[advanced] Combined — the hard-subtitle region is not selected so the OCR "
                              "gap-filling step is SKIPPED; only the heard part is kept.")
                        )
                        bu = []
                    elif str(p["meta"].get("source_language") or "").lower() not in (
                        "zh", "zh-cn", "en",
                    ):
                        # Bo nhan dang chi co Trung + Anh. Thu tieng khac thi BO buoc va lo chu
                        # KHONG chan ca lenh: Whisper nghe duoc moi thu tieng, phan nghe duoc
                        # van dung nguyen. Chan cung o day nghia la phim Nhat/Han mat luon che
                        # do "Ket hop" du no chay tot.
                        log.warning(
                            L("[nâng cao] Kết hợp — ngôn ngữ nguồn không phải Trung/Anh nên BỎ "
                              "bước OCR vá lỗ; chỉ giữ phần nghe được.",
                              "[advanced] Combined — the source language is not Chinese/English so the "
                              "OCR gap-filling step is SKIPPED; only the heard part is kept.")
                        )
                        bu = []
                    else:
                        log.info(f"[nâng cao] Kết hợp — [2/2] OCR vá {len(gaps)} khoảng bỏ sót…")
                        thay = _ocr([(t_in + a, t_in + b) for a, b in gaps])
                        bu = [dict(o, ocr=True) for o in thay if not overlaps_any(o, wh)]
                    log.info(f"[nâng cao] Kết hợp: nghe {len(wh)} câu + OCR vá {len(bu)} câu.")
                    lines = _ra_timeline(sorted(wh + bu, key=lambda s: s["start"]))
                else:
                    lines = _ra_timeline(_stt())

                # DỌN CÂU CŨ CỦA CHÍNH ĐOẠN NÀY TRƯỚC KHI CỘNG THÊM.
                #
                # ``append_a_chunks`` chỉ biết nối đuôi. Chạy lại "Lấy phụ đề cho đoạn này" —
                # đổi cách lấy, bấm nhầm, hay lần trước bị huỷ giữa chừng — là cùng một đoạn
                # phim có hai, ba bản phụ đề chồng khít lên nhau ở đúng những mốc đó. Người
                # dùng thật đã dính: một clip 8 phút mà đếm ra 1324 câu gốc, trong đó chỉ ~304
                # câu là thật, phần còn lại là hai bản sao. Hậu quả không dừng ở con số: dải
                # phụ đề gốc xếp chồng nhìn không ra, và prompt dịch nhận mỗi câu ba lần.
                #
                # Lấy lại phụ đề CHO MỘT ĐOẠN nghĩa là THAY phụ đề của đoạn đó — vẫn không đụng
                # gì tới các đoạn khác, đúng tinh thần "cộng thêm" ban đầu (xem đầu tệp
                # review_advanced_segment.py). Xoá đi qua ``delete_a_chunks`` nên mốc của lời
                # bình được đóng đinh trước, không câu nào nhảy chỗ.
                lo, hi = SEG.clip_range(clip)
                cu = SEG.a_chunks_in_range(SEG.load_a_chunks(p["dir"]), lo, hi)
                ADV.take_snapshot(p["dir"])
                if cu:
                    SEG.delete_a_chunks(p["dir"], p["a_lookup"], p["b_chunks"], cu)
                    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
                    log.info(
                        f"[nâng cao] Đoạn này đã có {len(cu)} câu gốc từ lần chạy trước — "
                        f"THAY bằng bản vừa lấy (các đoạn khác không đụng tới)."
                    )

                #  Nghe / kết hợp: tách đoạn dài theo dấu câu (kể cả câu OCR vá lỗ) nếu ô "Tách
                #  câu gốc" của dự án bật; OCR thuần thì không.
                res = SEG.append_a_chunks(
                    p["dir"], lines, tach_cau=(mode != "ocr" and TC.bat_goc(meta))
                )
                log.info(
                    f"=== XONG ĐOẠN “{name}”: thay {len(cu)} câu cũ bằng {res['added']} câu gốc, "
                    f"tổng {res['total']} câu (id câu của đoạn khác không đổi) ==="
                )
            except Exception as e:
                log.error(f"Lấy phụ đề cho đoạn thất bại: {e}", exc_info=True)
            finally:
                remove_project_logging(project_id, handler)
                _release_review(project_id)


    except WQ.WorkCancelled:
        #  Bấm Huỷ khi việc CÒN ĐANG XẾP HÀNG: thân hàm chưa chạy nên phần
        #  dọn dẹp trong ``finally`` của nó cũng chưa chạy — phải tự dọn ở
        #  đây, không thì khoá dự án không ai nhả và giao diện treo mãi.
        logger.warning(f"[HÀNG ĐỢI] Đã huỷ khi còn đang xếp hàng: {project_id}")
        _release_review(project_id)
@router.post("/projects/{project_id}/video-clips/{clip_id}/read-subs")
async def read_clip_subs(
    project_id: str, clip_id: str, background_tasks: BackgroundTasks,
    data: dict = Body(None),
):
    """Lay phu de goc CHI CHO doan cua mot clip — cong them, khong xoa cau da co."""
    p = _load(project_id)
    _require_advanced(p)
    clip = VID.find_clip(p["meta"], clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    if VID.clip_is_image(clip):
        raise HTTPException(status_code=400, detail=L("Ảnh không có phụ đề để đọc", "An image has no subtitles to read"))
    if not os.path.exists(clip.get("path") or ""):
        raise HTTPException(status_code=404, detail=L("Tệp của clip này không còn trên đĩa", "The file of this clip is no longer on disk"))

    data = data or {}
    mode = str(data.get("mode") or p["meta"].get("subtitle_mode") or "whisper").lower()
    if mode not in ("whisper", "ocr", "hybrid"):
        mode = "whisper"
    region = data.get("ocr_region")
    # KET HOP cung can vung: buoc OCR va lo khong biet quet o dau neu thieu. Chan ngay
    # tai cua ngo thay vi de no chay nua duong roi lang le bo qua nua sau.
    # CHI "OCR THUAN" moi bat buoc co vung: no khong con duong nao khac de lay chu.
    # KET HOP thi Whisper la chinh, thieu vung chi lam mat buoc va lo — xem nhanh
    # ``mode == "hybrid"`` o tren, no da tu bo qua OCR va giu phan nghe duoc.
    if mode == "ocr" and region is None and not p["meta"].get("ocr_region"):
        raise HTTPException(
            status_code=400,
            detail=L("Chưa khoanh vùng phụ đề cứng — hãy khoanh vùng trước khi dùng OCR.", "The hard-subtitle region is not selected — select the region before using OCR."),
        )
    # NGON NGU NGUON phai ro la Trung hay Anh — nhung CHI VOI "OCR THUAN".
    #
    # Vi sao khong chan "Ket hop": o do Whisper lam chinh va Whisper nghe duoc MOI thu
    # tieng, OCR chi va vao nhung lo Whisper bo sot. Chan cung ca hai che do nghia la
    # phim tieng Nhat/Han khong dung duoc "Ket hop" du phan nghe hoan toan on — nguoi
    # dung mat mot che do vi mot buoc phu. Thu tieng ngoai Trung/Anh thi bo qua buoc
    # OCR (canh bao trong log) chu khong chan ca lenh, cung kieu "suy giam mem" nhu
    # nhanh thieu vung.
    if mode == "ocr":
        _lang = str(p["meta"].get("source_language") or "").lower()
        if _lang not in ("zh", "zh-cn", "en"):
            raise HTTPException(
                status_code=400,
                detail=(
                    L("OCR chỉ có một bộ nhận dạng Trung + Anh. Hãy chọn rõ ngôn ngữ nguồn "
                      "là Tiếng Trung hoặc Tiếng Anh; thứ tiếng khác thì dùng Nghe (Whisper).",
                      "OCR only has one Chinese + English recognizer. Please set the source language "
                      "clearly to Chinese or English; for other languages use Listen (Whisper).")
                ),
            )

    _acquire_review_or_409(project_id)
    try:
        review_service.clear_analysis_cancel(project_id)
        background_tasks.add_task(
            _read_clip_subs_wrapper, project_id, clip_id, mode, region,
        )
    except Exception:
        _release_review(project_id)
        raise
    return {"status": "started", "mode": mode}


# --------------------------------------------------------------------------- #
#  NAP PHU DE TU TEP CO SAN (.srt/.vtt/.ass) — khong chay STT/OCR
# --------------------------------------------------------------------------- #
#  Vi sao co duong nay: nguoi dung tai video ve kem san tep phu de (yt-dlp ``--write-sub``,
#  tep .srt di kem ban phim, hay ban tu go). Bat ho ngoi cho Whisper nghe lai ca tap phim
#  de ra mot ban KEM CHINH XAC HON ban da co san la vo ly.
#
#  KHAC voi sidecar ``<video>.subforge_sub.json`` (review_service._load_author_subs): cai do
#  chi ton tai khi CHINH SubForge tai video ve va tu dong, dung mot lan roi bi xoa. Duong
#  nay la nguoi dung CHU DONG nap tep bat ky, luc nao cung duoc.
#  Bang ma tep phu de THUC TE hay gap.
#
#  VI SAO KHONG DUNG ``errors="replace"``: tep GBK (SRT tieng Trung) giai bang UTF-8 kem
#  replace ra chuoi TOAN U+FFFD — van "doc duoc", van parse ra DUNG SO CAU, nen endpoint
#  tra 200 va bao thanh cong, trong khi a_chunks that da bi THAY bang mot mo dau hoi.
#  Nguoi dung mat sach phu de goc ma khong co canh bao nao. (Do that: tep GBK 2 cau ra
#  19 ky tu rac.)
#
#  VI SAO KHONG THU TUAN TU roi lay ban dau tien khong nem loi: ``gb18030`` nhan GAN MOI
#  day byte, nen tep Shift-JIS di qua no ra chu Han vo nghia ("巹偼擔杮岅" thay vi
#  "私は日本語"). Phai CHAM DIEM tung ban giai roi lay ban hop ly nhat — xem ``_score``.
_SUB_ENCODINGS = (
    "utf-8-sig", "utf-16", "gb18030", "big5", "shift_jis", "euc-kr", "cp1258", "cp1252",
)


def _decode_sub_bytes(data: bytes) -> str:
    """Giai ma noi dung tep phu de. Nem 400 neu khong doc noi.

    TUYET DOI khong tra ve chuoi day U+FFFD (xem ``_SUB_ENCODINGS``).
    """
    #  UTF-8 / UTF-16 co cau truc RANG BUOC, giai duoc la chac chan dung — thu truoc
    #  (va phai truoc, vi tep UTF-8 hop le cung "giai duoc" bang gb18030 ra chu rac).
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    #  Con lai la cac bang ma 1-2 byte khong the phan biet bang cau truc: gb18030 nhan
    #  gan moi day byte, cp1252/cp1258 thi nhan TAT CA. Nen phai doan bang THONG KE
    #  ngon ngu — dung ``charset_normalizer`` (di kem ``requests``, da co san trong moi
    #  truong nay). Tu viet ham cham diem la sai: ban dau tay cua toi doc tep Shift-JIS
    #  thanh chu Han vo nghia, va sua diem cho Shift-JIS thi lai lam tep GBK doc thanh
    #  chu Latin co dau.
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception as e:  # noqa: BLE001 — thu vien loi thi con duong du phong duoi
        logger.warning(f"[nâng cao] Đoán bảng mã tệp phụ đề thất bại ({e}) — thử lần lượt.")

    #  Du phong: thu lan luot. Kem hon doan thong ke nhung con hon tra 400.
    for enc in _SUB_ENCODINGS[2:]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise HTTPException(
        status_code=400,
        detail=(
            L("Không đọc được bảng mã của tệp phụ đề. Hãy mở tệp bằng Notepad rồi "
              "“Save as” với bảng mã UTF-8, sau đó nạp lại.",
              "Cannot read the encoding of the subtitle file. Open the file in Notepad and "
              "“Save as” with UTF-8 encoding, then load it again.")
        ),
    )


def _apply_guessed_lang(meta: dict, lines: list, filename: str) -> str:
    """Doan ngon ngu goc tu noi dung phu de roi ghi vao metadata. Tra ve ma da ghi ("" =
    khong ghi gi).

    CHI ghi khi o ngon ngu dang la "Tu nhan dien": doan may khong duoc de len lua chon
    nguoi dung da co y dat. Vi sao phai ghi: prompt dich in ten ngon ngu NGUON ra
    (``LANG_NAMES`` trong review_advanced_prompts), de "auto" thi no chi noi duoc "ngon
    ngu gốc" chung chung — AI phai tu suy tu noi dung, dich kem han han.
    """
    if str(meta.get("source_language") or "auto") not in ("", "auto"):
        return ""
    ma = SEG.guess_language(lines, filename)
    if not ma:
        return ""
    meta["source_language"] = ma
    return ma


async def _sub_lines(file: Optional[UploadFile], content: str) -> tuple:
    """Doc ND phu de tu tep tai len HOAC chu dan thang.

    Tra ve ``(lines, ten_tep)`` — ten tep di kem de con doan ngon ngu khi noi dung khong
    quyet dinh duoc (xem ``SEG.guess_language``).
    """
    raw = ""
    ten = ""
    if file is not None and file.filename:
        ten = file.filename
        if not ten.lower().endswith(SEG.SUB_EXTS):
            raise HTTPException(
                status_code=400,
                detail=L("Tệp phụ đề phải là .srt, .vtt, .ass hoặc .ssa", "The subtitle file must be .srt, .vtt, .ass or .ssa"),
            )
        data = await file.read()
        raw = _decode_sub_bytes(data)
    elif content and content.strip():
        raw = content
    if not raw.strip():
        raise HTTPException(status_code=400, detail=L("Chưa chọn tệp phụ đề hoặc dán nội dung", "No subtitle file selected or content pasted"))

    lines = await run_in_threadpool(SEG.parse_subtitle_text, raw, ten)
    if not lines:
        raise HTTPException(
            status_code=400,
            detail=(
                L("Không đọc được câu nào từ tệp phụ đề. Kiểm tra lại định dạng "
                  "(.srt/.vtt/.ass) và mốc thời gian bên trong tệp.",
                  "Could not read any sentence from the subtitle file. Check the format "
                  "(.srt/.vtt/.ass) and the timestamps inside the file.")
            ),
        )
    return lines, ten


@router.post("/projects/{project_id}/subs-from-file")
async def load_subs_from_file(
    project_id: str,
    file: Optional[UploadFile] = File(None),
    content: str = Form(""),
):
    """THAY TOAN BO cau goc bang phu de trong tep — cho CA PHIM.

    Cung ngu nghia voi nut "Lay lai phu de" nen cung mot canh bao: cau B chua duoc keo
    tay se duoc DONG DINH moc truoc khi thay (``freeze_b_times``), khong thi chung mat
    moc va bi don xuong cuoi timeline.

    Moc thoi gian trong tep la moc cua PHIM GOC — bang moc timeline khi video goc bat
    dau tu 0 va chua doi toc do (ca thong thuong). Cat/chen clip roi thi nap cho TUNG
    DOAN (``/video-clips/{id}/subs-from-file``) moi dung cho.
    """
    p = _load(project_id)
    _require_advanced(p)
    lines, ten_tep = await _sub_lines(file, content)

    snap = ADV.capture_snapshot(p["dir"])
    res = await run_in_threadpool(
        #  gop=False: tệp phụ đề người dùng tự nạp giữ nguyên từng câu, không gộp mẩu ngắn.
        SEG.replace_all_a_chunks, p["dir"], p["a_lookup"], p["b_chunks"], lines, False,
    )
    #  Cau B vua bi cat ``source_a_ids`` -> phai ghi xuong dia, khong thi lan nap sau
    #  ``a_lookup`` van con id chet.
    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    ADV.commit_snapshot(p["dir"], snap)
    #  Danh dau da co phu de goc: man Dashboard va cac cho khac doc co nay de biet du an
    #  di duoc toi buoc ke tiep. Nap tep xong ma co van tat thi UI bao "chua phan tich".
    meta = p["meta"]
    state = meta.setdefault("state", {})
    state["stt_complete"] = True
    state["a_chunks_generated"] = True
    state["analysis_started"] = False
    state.pop("analysis_error", None)
    #  Tep .srt khong mang ma ngon ngu ben trong -> doan tu chinh chu viet, khong thi o
    #  "Ngon ngu nguon" ket o "Tu nhan dien" va prompt dich khong biet dang dich tu dau.
    lang = _apply_guessed_lang(meta, lines, ten_tep)
    ADV.save_meta(p["dir"], meta)

    p2 = _load(project_id)
    return {**res, "language": lang, "timeline": await _payload(p2, include_a=True)}


@router.post("/projects/{project_id}/video-clips/{clip_id}/subs-from-file")
async def load_clip_subs_from_file(
    project_id: str,
    clip_id: str,
    file: Optional[UploadFile] = File(None),
    content: str = Form(""),
    offset: float = Form(0.0),
):
    """Nap phu de tu tep CHI CHO doan cua mot clip — THAY cau goc cua rieng doan do.

    Moc trong tep tinh theo TEP NGUON cua clip (giay 0 = dau tep phim), nen phai doi
    sang giay timeline qua ``SEG.to_timeline`` — bo qua buoc nay thi clip nam o giay 20
    cua timeline se co phu de dinh o giay 0. Chi giu cau ROI VAO khoang clip dang chiem
    (``clip.in``..``clip.out``): tep phu de la cua CA tap phim, ma clip chi la mot lat
    cat cua no.

    ``offset`` = so giay cong them vao moc cua tep truoc khi doi (tep phu de lech pha so
    voi ban phim dang dung — rat hay gap khi tep sub lay tu ban chieu rap con video la
    ban truyen hinh co doan quang cao dau).
    """
    p = _load(project_id)
    _require_advanced(p)
    clip = VID.find_clip(p["meta"], clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
    if VID.clip_is_image(clip):
        raise HTTPException(status_code=400, detail=L("Ảnh không có phụ đề để nạp", "An image has no subtitles to load"))
    lines, ten_tep = await _sub_lines(file, content)

    t_in = VID._num(clip.get("in"))
    t_out = VID._num(clip.get("out"))
    trong_doan = []
    for ln in lines:
        s = float(ln.get("start") or 0.0) + float(offset or 0.0)
        e = float(ln.get("end") or s) + float(offset or 0.0)
        #  Loc theo DIEM GIUA — dung luat voi ``a_ids_in_clip``/``a_chunks_in_range``, nho
        #  vay cau nam vat ngang mep chi thuoc ve MOT doan, khong dem hai lan.
        if not (t_in - 0.001 <= (s + e) / 2.0 < t_out + 0.001):
            continue
        trong_doan.append({
            "start": SEG.to_timeline(clip, s),
            "end": SEG.to_timeline(clip, e),
            "text": ln.get("text") or "",
        })
    if not trong_doan:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Tệp phụ đề không có câu nào rơi vào đoạn này "
                f"({t_in:.1f}s–{t_out:.1f}s của tệp phim). Kiểm tra lại tệp, hoặc chỉnh "
                f"“lệch giờ” nếu phụ đề lệch pha so với bản phim đang dùng."
            ),
        )

    #  THAY phu de cua rieng doan nay — cung luat voi "Lay sub cho doan nay" chay lai
    #  (xem ``_read_clip_subs_wrapper``): khong don cau cu thi hai ban phu de chong khit
    #  len nhau, prompt dich nhan moi cau hai lan.
    lo, hi = SEG.clip_range(clip)
    cu = SEG.a_chunks_in_range(SEG.load_a_chunks(p["dir"]), lo, hi)
    snap = ADV.capture_snapshot(p["dir"])
    if cu:
        SEG.delete_a_chunks(p["dir"], p["a_lookup"], p["b_chunks"], cu)
        ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    #  gop=False: tệp người dùng tự nạp giữ nguyên từng câu.
    res = await run_in_threadpool(SEG.append_a_chunks, p["dir"], trong_doan, False)
    ADV.commit_snapshot(p["dir"], snap)

    meta = p["meta"]
    state = meta.setdefault("state", {})
    state["stt_complete"] = True
    state["a_chunks_generated"] = True
    #  DON CO VIEC DANG CHAY, giong hai duong kia: lan "Lay sub cho doan nay" truoc do
    #  that bai giua chung se de lai ``analysis_started = True`` + ``analysis_error``, ma
    #  nap tep xong la du an CO phu de that roi — khong don thi trinh dung cu bao "đang
    #  phân tích…" mai, khong co gi go duoc.
    state["analysis_started"] = False
    state.pop("analysis_error", None)
    #  Doan tren PHAN ROI VAO DOAN, khong phai ca tep: doan phim chen them hay khac
    #  thu tieng voi phan con lai, va do moi la thu nguoi dung sap dich.
    lang = _apply_guessed_lang(meta, trong_doan, ten_tep)
    ADV.save_meta(p["dir"], meta)

    p2 = _load(project_id)
    return {
        **res,
        "replaced": len(cu),
        "skipped": len(lines) - len(trong_doan),
        "language": lang,
        "timeline": await _payload(p2, include_a=True),
    }


@router.post("/projects/{project_id}/import-segment")
async def import_script_segment(project_id: str, data: ScriptImport = Body(...)):
    """CONG THEM cau B cho mot doan — khong dung den cau B da co.

    Khac ``POST /import`` (thay CA danh sach): dich rieng tung doan moi ghep vao thi doan
    da lam xong phai giu nguyen giong da sinh va vi tri da keo.
    """
    p = _load(project_id)
    _require_advanced(p)
    try:
        res = await run_in_threadpool(
            SEG.append_script, p["dir"], p["meta"], p["a_lookup"], data.b_chunks,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ADV.save_meta(p["dir"], p["meta"])
    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2)}


# --------------------------------------------------------------------------- #
#  Prompt tao kich ban (dung CHUNG bo prompt cua backend)
# --------------------------------------------------------------------------- #
def _assign_speakers(meta: dict) -> bool:
    """Du an nay co bat AI PHAN VAI nhan vat khong.

    MAC DINH TAT: phan lon phim chi can mot giong doc, ma bat len la AI phai nghi them
    nhan vai cho TUNG cau va tra ve them mot truong cho moi cau — cham hon, ton token
    hon, va them mot cho de model tra sai dinh dang. Ai can nhieu giong thi bat o man
    Chuan bi. Tat thi moi cau ve vai mac dinh (``ADV.speaker_of`` -> "MC") va bang Phan
    vai van dung binh thuong voi mot vai duy nhat.
    """
    return bool((meta or {}).get("assign_speakers", False))


@router.get("/projects/{project_id}/prompt")
async def get_script_prompt(project_id: str, kind: str = "", clip_id: str = ""):
    """Tra ve NGUYEN VAN prompt de nguoi dung copy sang ChatGPT/Claude.

    HAI LOAI KICH BAN (``kind``), khac nhau DUNG O DAY con moi buoc sau dung chung:
      * ``review``  — viet lai thanh loi binh MC, gop nhieu cau goc thanh mot doan.
      * ``dubbing`` — dich sat nghia, MOT cau goc thanh MOT cau, nen phu de bam dung
        tung dong cua ban goc.

    Bo trong ``kind`` -> lay loai da chon cua du an (``script_kind``, mac dinh LONG TIENG — ``PROMPTS.DEFAULT_KIND``).
    Ca hai prompt deu doi AI ghi them NHAN VAI (``speaker``) cho tung cau de video nhieu
    nhan vat gan duoc giong khac nhau. Xem ``review_advanced_prompts``.

    ``clip_id``: chi lay cau goc NAM TRONG doan cua clip do — de dich rieng tung doan moi
    ghep vao, khong phai dich lai ca phim. Cau B nhap ve se CONG THEM qua
    ``POST /import-segment``.
    """
    p = _load(project_id)
    _require_advanced(p)

    a_list = sorted(p["a_lookup"].values(), key=lambda a: float(a.get("start") or 0))
    scope = "toàn dự án"
    if clip_id:
        clip = VID.find_clip(p["meta"], clip_id)
        if not clip:
            raise HTTPException(status_code=404, detail=L("Không tìm thấy clip video", "Video clip not found"))
        a_list = SEG.a_ids_in_clip(a_list, clip)
        lo, hi = SEG.clip_range(clip)
        scope = f"đoạn {clip.get('name') or clip_id} ({lo:.1f}s–{hi:.1f}s)"
        if not a_list:
            raise HTTPException(
                status_code=400,
                detail=(
                    L("Đoạn này chưa có phụ đề gốc — bấm “Lấy phụ đề cho đoạn này” trước "
                      "rồi mới copy prompt.",
                      "This segment has no source subtitles yet — click “Get subtitles for this segment” first "
                      "and then copy the prompt.")
                ),
            )
    if not a_list:
        raise HTTPException(
            status_code=400,
            detail=L("Chưa có phụ đề gốc — hãy chạy 'Lấy phụ đề' trước.", "There are no source subtitles yet — run 'Get subtitles' first."),
        )

    meta = p["meta"]
    chosen = PROMPTS.normalize_kind(kind or meta.get("script_kind"))
    prompt = PROMPTS.build_prompt(
        chosen,
        a_list,
        source_lang=meta.get("source_language") or "auto",
        target_lang=meta.get("target_language") or "vi",
        default_rate=meta.get("default_rate") or "auto",
        style=meta.get("speech_style"),
        brand=meta.get("brand_name"),
        voice=meta.get("target_voice"),
        with_speaker=_assign_speakers(meta),
    )
    # Ghi nho loai vua chon de lan sau mo lai dung no (va de goi tin timeline noi dung).
    if chosen != PROMPTS.normalize_kind(meta.get("script_kind")):
        meta["script_kind"] = chosen
        ADV.save_meta(p["dir"], meta)
    return {
        "prompt": prompt, "a_count": len(a_list), "kind": chosen, "scope": scope,
        "assign_speakers": _assign_speakers(meta),
    }


# --------------------------------------------------------------------------- #
#  TAO KICH BAN BANG AI (DeepSeek) — duong TU DONG cua che do chi tiet
# --------------------------------------------------------------------------- #
class AiScriptRequest(BaseModel):
    """Yeu cau tao kich ban tu dong.

    ``kind``            : "dubbing" / "review". Bo trong -> loai dang chon cua du an.
    ``model``           : model DeepSeek. Bo trong -> model mac dinh trong Cai dat.
    ``clip_id``         : chi lam cho doan cua clip do (cong them, khong dung cau da co).
    ``assign_speakers`` : co bat AI phan vai nhan vat khong. Bo trong -> theo cai dat
                          du an (mac dinh TAT — xem ``_assign_speakers``).
    """
    kind: Optional[str] = None
    model: Optional[str] = None
    clip_id: Optional[str] = None
    assign_speakers: Optional[bool] = None


def _ai_script_wrapper(project_id: str, kind: str, model: str, clip_id: str,
                       assign_speakers: bool):
    """Chay nen: goi DeepSeek roi NHAP thang ket qua vao du an.

    Log ghi vao synthesis.log de FE theo doi qua SSE san co (LogConsole) — dung y het
    ``run_ai_generate_wrapper`` cua che do co dien, nen khong phai them duong theo doi
    tien do nao moi.

    Nhap ket qua di DUNG CUA CU: ``SEG.append_script`` (co clip_id) / ``ADV.import_script``
    (ca du an). Nho vay moi bao dam cua duong dan tay — cau khong doi chu thi giu nguyen
    uid + giong da sinh + vi tri da keo, chup anh de Ctrl+Z lui duoc — van con nguyen.
    """
    handler = setup_project_logging(project_id, config_service.get_review_root())
    #  LUOI AN TOAN. Ham nay chay NEN nen model rong khong hong ngay ma hong o TUNG LO,
    #  hang chuc lan, bang tieng Anh. Duong chay theo lo tung truyen thang "" vao day va
    #  khong ai thay trong nhieu ban phat hanh. Chan o chinh cho nhan tham so thi moi
    #  duong goi — ca duong sau nay — deu duoc che.
    model = str(model or "").strip() or config_service.ai_model_mac_dinh()
    #  Chuẩn hoá loại kịch bản TRƯỚC khi in banner: lô truyền "" (theo mặc định = lồng tiếng) mà banner so thô với
    #  KIND_DUBBING nên in "REVIEW" dù bên dưới làm lồng tiếng — người dùng tưởng chạy sai (08-10-2026).
    kind = PROMPTS.normalize_kind(kind)
    try:
        from services import review_advanced_ai as AI

        logger.info(
            f"=== BAT DAU TAO KICH BAN BANG AI (chi tiet — "
            f"{'LONG TIENG' if kind == PROMPTS.KIND_DUBBING else 'REVIEW'}, model={model}) ==="
        )
        p = ADV.load_project(project_id, migrate=False)
        meta = p["meta"]
        a_list = sorted(
            p["a_lookup"].values(), key=lambda a: float(a.get("start") or 0)
        )
        #  Bước 0 đọc ngữ cảnh CẢ PHIM kể cả khi chỉ làm cho một clip — xưng hô / tên
        #  riêng của đoạn đó phải khớp phần còn lại.
        film_a_list = a_list
        if clip_id:
            clip = VID.find_clip(meta, clip_id)
            if not clip:
                raise RuntimeError("Khong tim thay clip video (da bi xoa?).")
            a_list = SEG.a_ids_in_clip(a_list, clip)
        if not a_list:
            raise RuntimeError("Khong co cau goc nao trong pham vi da chon.")

        def _should_stop():
            return review_service.is_analysis_cancelled(project_id)

        def _tien_do(done, total, phase="script"):
            _ghi_tien_do_ai(p["dir"], phase=phase, done=done, total=total)

        res = AI.generate_bchunks(
            a_list, meta, kind, model,
            assign_speakers=assign_speakers,
            should_stop=_should_stop,
            on_progress=_tien_do,
            film_a_list=film_a_list,
            project_dir=p["dir"],
        )
        if _should_stop():
            # Huy giua chung: KHONG nhap phan lam duoc. Nhap nua voi la thay ca kich ban
            # cu bang mot ban thieu dau thieu duoi ma nguoi dung khong he yeu cau.
            logger.warning("=== DA HUY TAO KICH BAN THEO YEU CAU ===")
            _save_ai_state(project_id, error="Da huy tao kich ban theo yeu cau.")
            review_service.clear_analysis_cancel(project_id)
            return

        chunks = res["chunks"]
        if clip_id:
            out = SEG.append_script(p["dir"], meta, p["a_lookup"], chunks)
            # ``append_script`` chi DAT co trong meta trong bo nho, khong tu ghi (duong
            # nhap tay ghi ho o route). Khong ghi o day thi ``_save_ai_state`` ben duoi
            # doc lai meta TU DIA -> co ``b_imported`` vua dat bi mat trang.
            ADV.save_meta(p["dir"], meta)
        else:
            out = ADV.import_script(p["dir"], meta, p["a_lookup"], chunks)

        missing = res.get("missing_a_ids") or []
        google = res.get("google_a_ids") or []
        if missing:
            logger.warning(
                f"CON {len(missing)} cau goc chua co kich ban (AI khong tra ve duoc). "
                f"Bam 'Tao bang AI' lan nua de chay tiep phan thieu."
            )
        if google:
            logger.warning(
                f"Co {len(google)} cau phai dich bang Google (DeepSeek loi) — nen doc lai "
                f"nhung cau nay."
            )
        _save_ai_state(project_id, result={
            "kind": kind,
            "total": out.get("total") or len(chunks),
            "added": out.get("added") or out.get("fresh") or 0,
            "missing": len(missing),
            "google": len(google),
            "scope": clip_id or "",
            "speakers": [r["speaker"] for r in (out.get("speakers") or [])],
            "unassigned_speakers": out.get("unassigned_speakers") or [],
        })
        logger.info(f"=== TAO KICH BAN BANG AI HOAN TAT: {len(chunks)} cau ===")
    except Exception as e:
        friendly = getattr(e, "user_message", None) or str(e)
        logger.error(f"Tao kich ban bang AI that bai: {friendly}", exc_info=True)
        _save_ai_state(project_id, error=friendly[:500])
    finally:
        _xoa_tien_do_ai(ADV.project_dir_of(project_id))
        remove_project_logging(project_id, handler)
        _release_review(project_id)


#  TIẾN ĐỘ THẬT của job tạo kịch bản — tệp RIÊNG (như render_progress.json), không ghi
#  vào metadata: job nền còn ghi metadata lúc nhập kết quả, ghi chồng mỗi lô là đua nhau
#  đè mất khoá. FE đọc qua /status (mỗi 3 giây) để hiện "Đang tạo 63/163 câu".
_TEP_TIEN_DO_AI = "ai_script_progress.json"


def _ghi_tien_do_ai(pdir: str, *, phase: str, done: int, total: int) -> None:
    import json
    import time
    try:
        tam = os.path.join(pdir, _TEP_TIEN_DO_AI + ".tmp")
        with open(tam, "w", encoding="utf-8") as f:
            json.dump({"phase": phase, "done": int(done), "total": int(total),
                       "at": time.time()}, f)
        os.replace(tam, os.path.join(pdir, _TEP_TIEN_DO_AI))
    except Exception as e:  # noqa: BLE001 — tiến độ hỏng không được chặn việc dịch
        logger.debug(f"[chi tiet] Khong ghi duoc tien do AI: {e}")


def _doc_tien_do_ai(pdir: str):
    import json
    try:
        with open(os.path.join(pdir, _TEP_TIEN_DO_AI), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _xoa_tien_do_ai(pdir: str) -> None:
    try:
        os.remove(os.path.join(pdir, _TEP_TIEN_DO_AI))
    except OSError:
        pass


def _save_ai_state(project_id: str, *, result: dict = None, error: str = None,
                   started: bool = False) -> None:
    """Ghi ket qua / loi cua job AI vao metadata (khoa RIENG cua che do chi tiet).

    Vi sao KHONG dung ``review_service.mark_analysis_*``: ``mark_analysis_started`` ha ca
    ``stt_complete`` + ``a_chunks_generated`` (no danh cho viec phan tich lai video) —
    dung nham la giao dien tuong du an mat sach phu de goc. O day chi can 3 khoa rieng,
    khong cham vao trang thai phan tich.
    """
    try:
        p = ADV.load_project(project_id, migrate=False)
        state = p["meta"].setdefault("state", {})
        state["ai_script_started"] = bool(started)
        if started:
            state.pop("ai_script_error", None)
            state.pop("ai_script_result", None)
        if result is not None:
            state["ai_script_result"] = result
        if error is not None:
            state["ai_script_error"] = error
        ADV.save_meta(p["dir"], p["meta"])
    except Exception as e:
        logger.warning(f"[chi tiet] Khong ghi duoc trang thai job AI: {e}")


@router.post("/projects/{project_id}/ai-script")
async def ai_script(
    project_id: str, background_tasks: BackgroundTasks,
    data: AiScriptRequest = Body(default=AiScriptRequest()),
):
    """Tao kich ban B TU DONG bang DeepSeek (thay cho copy prompt -> dan tay).

    Chan tai cua ngo moi thu co the bao truoc (chua co API key, chua co phu de goc, clip
    khong ton tai) de nguoi dung biet ngay, thay vi job nen chay roi that bai am tham.
    """
    p = _load(project_id)
    _require_advanced(p)

    import services.deepseek_service as deepseek_service
    if not deepseek_service.is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                L("Chưa cấu hình API key của bộ AI đang chọn (DeepSeek / Gemini). Vào Cài đặt để nhập API key trước khi "
                  "dùng nút này (hoặc copy prompt làm tay như cũ).",
                  "The API key of the selected AI (DeepSeek / Gemini) is not configured. Go to Settings to enter the API key before "
                  "using this button (or copy the prompt and do it by hand as before).")
            ),
        )

    meta = p["meta"]
    kind = PROMPTS.normalize_kind(data.kind or meta.get("script_kind"))
    a_list = sorted(p["a_lookup"].values(), key=lambda a: float(a.get("start") or 0))
    if data.clip_id:
        clip = VID.find_clip(meta, data.clip_id)
        if not clip:
            raise HTTPException(status_code=404, detail="Khong tim thay clip video")
        a_list = SEG.a_ids_in_clip(a_list, clip)
        if not a_list:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Doan nay chua co phu de goc — bam 'Lay phu de cho doan nay' truoc."
                ),
            )
    if not a_list:
        raise HTTPException(
            status_code=400,
            detail="Chua co phu de goc — hay chay 'Lay phu de' truoc.",
        )

    # NGUON TRUNG DICH: chan NGAY tai cua ngo. Prompt se doc thanh "câu thoại gốc
    # (Vietnamese)... dịch sang Vietnamese" -> model tra lai nguyen van cau goc, va
    # Google cung the -> nguoi dung thay "dich xong" ma chu y het nhu cu. Bao ngay o
    # day thi ho sua duoc trong 5 giay, khong phai cho het job roi doi log.
    _src = (meta.get("source_language") or "auto").lower()
    _dst = (meta.get("target_language") or "vi").lower()
    if _src != "auto" and _src == _dst:
        raise HTTPException(
            status_code=400,
            detail=(
                L(f"Ngôn ngữ nguồn và ngôn ngữ đích đang GIỐNG NHAU ({_dst}) nên không có gì "
                  "để dịch — AI sẽ trả lại nguyên văn câu gốc. Vào tab Chuẩn bị sửa 'Ngôn ngữ "
                  "nguồn' (tiếng của phim) hoặc 'Dịch sang' (tiếng muốn đọc) cho khác nhau.",
                  f"The source and target languages are the SAME ({_dst}) so there is nothing "
                  "to translate — the AI would return the original text. Go to the Prepare tab and change 'Source "
                  "language' (the film's language) or 'Translate to' (the language to read) so they differ.")
            ),
        )

    assign = (
        bool(data.assign_speakers)
        if data.assign_speakers is not None
        else _assign_speakers(meta)
    )
    model = data.model or config_service.ai_model_mac_dinh()

    # Ghi nho lua chon: prompt copy tay va lan chay sau phai ra DUNG cai vua chon, khong
    # thi nguoi dung doi o tren roi bam copy lai duoc prompt cua gia tri cu.
    meta["script_kind"] = kind
    meta["assign_speakers"] = assign
    ADV.save_meta(p["dir"], meta)

    _acquire_review_or_409(project_id)
    try:
        review_service.clear_analysis_cancel(project_id)
        _save_ai_state(project_id, started=True)
        background_tasks.add_task(
            _ai_script_wrapper, project_id, kind, model, data.clip_id or "", assign,
        )
    except Exception:
        _release_review(project_id)
        raise
    return {
        "status": "started",
        "kind": kind,
        "model": model,
        "a_count": len(a_list),
        "assign_speakers": assign,
    }


# --------------------------------------------------------------------------- #
#  TOM TAT VIDEO (buoc 0 cua tao kich ban AI) — xem lai / tao rieng
# --------------------------------------------------------------------------- #
#  Buoc 0 viet TOM TAT cho nguoi xem + GHI CHU DICH vao ``<du an>/ai_context.json`` moi lan
#  tao kich ban bang AI (neu bat o Cai dat). Hai endpoint duoi day cho trinh dung / chay
#  lo XEM lai no, va TAO RIENG khi du an chua co (du an cu, hoac da tat buoc 0) — tom tat
#  con dung de noi vao mo ta YouTube.
class AiContextRequest(BaseModel):
    #: Goi lai DeepSeek du da co ban luu khop phu de (nguoi dung bam "Tao lai").
    force: bool = False


def _save_ai_context_state(project_id: str, *, started: bool = False,
                           error: str = None) -> None:
    try:
        p = ADV.load_project(project_id, migrate=False)
        state = p["meta"].setdefault("state", {})
        state["ai_context_started"] = bool(started)
        if started:
            state.pop("ai_context_error", None)
        if error is not None:
            state["ai_context_error"] = error
        ADV.save_meta(p["dir"], p["meta"])
    except Exception as e:
        logger.warning(f"[chi tiet] Khong ghi duoc trang thai job tom tat: {e}")


@router.get("/projects/{project_id}/ai-context")
async def get_ai_context(project_id: str):
    """Tom tat + ghi chu dich DA LUU cua du an (``context`` = None neu chua co)."""
    from services import review_advanced_ai as AI

    p = _load(project_id)
    state = p["meta"].get("state") or {}
    opts = config_service.get_ai_script_options()
    return {
        "context": await run_in_threadpool(AI.read_film_context, p["dir"]),
        #  Chi tinh "dang chay" khi khoa du an (trong bo nho) con giu: app bi tat giua
        #  job thi co ``ai_context_started`` ket lai trong metadata mai mai.
        "running": bool(state.get("ai_context_started")) and _is_review_busy(project_id),
        "script_running": bool(state.get("ai_script_started")) and _is_review_busy(project_id),
        "error": state.get("ai_context_error") or "",
        # De FE bao "ban tom tat viet cho ngon ngu cu" khi da doi 'Dich sang'.
        "target_language": (p["meta"].get("target_language") or "vi").lower(),
        "has_subs": bool(p["a_lookup"]),
        "summary_level": opts.get("summary_level"),
        "context_model": opts.get("context_model"),
    }


def _ai_context_wrapper(project_id: str, force: bool) -> None:
    """Chay nen: buoc 0 RIENG (khong dich). Loi -> ghi ``ai_context_error`` de FE bao."""
    from services import review_advanced_ai as AI

    handler = setup_project_logging(project_id, config_service.get_review_root())
    try:
        p = ADV.load_project(project_id, migrate=False)
        a_list = sorted(p["a_lookup"].values(), key=lambda a: float(a.get("start") or 0))
        opts = config_service.get_ai_script_options()
        model = str(opts.get("context_model") or "").strip() or config_service.ai_model_mac_dinh()
        logger.info(f"=== TOM TAT VIDEO BANG AI (model={model}) ===")
        AI.build_film_context(
            a_list, p["meta"], model, project_dir=p["dir"],
            summary_level=str(opts.get("summary_level") or "co-ban"),
            force=force, strict=True,
            should_stop=lambda: review_service.is_analysis_cancelled(project_id),
        )
        _save_ai_context_state(project_id, started=False)
        logger.info("=== TOM TAT VIDEO XONG ===")
    except Exception as e:
        friendly = getattr(e, "user_message", None) or str(e)
        logger.error(f"Tom tat video that bai: {friendly}")
        _save_ai_context_state(project_id, started=False, error=friendly[:500])
    finally:
        remove_project_logging(project_id, handler)
        _release_review(project_id)


@router.post("/projects/{project_id}/ai-context")
async def make_ai_context(
    project_id: str, background_tasks: BackgroundTasks,
    data: AiContextRequest = Body(default=AiContextRequest()),
):
    """Tao (lai) tom tat video bang AI — CHI buoc 0, khong dung toi kich ban."""
    p = _load(project_id)
    _require_advanced(p)
    import services.deepseek_service as deepseek_service
    if not deepseek_service.is_configured():
        raise HTTPException(
            status_code=400,
            detail=L("Chưa cấu hình API key của bộ AI đang chọn (DeepSeek / Gemini). Vào Cài đặt để nhập API key trước.", "The API key of the selected AI (DeepSeek / Gemini) is not configured. Go to Settings to enter the API key first."),
        )
    if not p["a_lookup"]:
        raise HTTPException(
            status_code=400, detail=L("Chưa có phụ đề gốc — hãy chạy 'Lấy phụ đề' trước.", "There are no source subtitles yet — run 'Get subtitles' first."),
        )
    _acquire_review_or_409(project_id)
    try:
        review_service.clear_analysis_cancel(project_id)
        _save_ai_context_state(project_id, started=True)
        background_tasks.add_task(_ai_context_wrapper, project_id, bool(data.force))
    except Exception:
        _release_review(project_id)
        raise
    return {"status": "started"}


#  Phụ đề thêm thứ tiếng: routes/sub_track_routes.py (dùng chung với chế độ Cơ bản).


# --------------------------------------------------------------------------- #
#  BANG PHAN VAI — video nhieu nhan vat
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/voice-cast")
async def get_voice_cast(project_id: str):
    """Cac VAI dang co tren timeline + giong da gan cho tung vai."""
    p = _load(project_id)
    _require_advanced(p)
    return {
        "speakers": ADV.speakers_in_use(p["b_chunks"], p["meta"]),
        "cast": p["meta"].get(ADV.VOICE_CAST_FIELD) or {},
        "default_voice": p["meta"].get("target_voice") or "",
    }


@router.patch("/projects/{project_id}/voice-cast")
async def patch_voice_cast(project_id: str, data: dict = Body(...)):
    """Gan giong cho mot / nhieu VAI.

    Nhan ``{"cast": {"Nam 1": "vi-VN-NamMinhNeural", "Nữ 1": ""}}`` — gia tri rong = bo
    gan, vai do quay ve giong mac dinh cua du an. KHONG cham vao b_chunks: giong cua tung
    cau duoc suy ra luc sinh giong / dung phim (xem ADV.resolve_voice), nen doi mot vai la
    moi cau cua vai do tu lech dau van tay va nut "Sinh giọng" nhan ra ngay.
    """
    p = _load(project_id)
    _require_advanced(p)
    raw = data.get("cast")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail=L("Thiếu bảng phân vai (cast)", "The cast table is missing"))

    meta = p["meta"]
    # Chup anh truoc: doi giong mot vai la doi giong CA TRAM cau, bam nham phai lui duoc.
    snap = ADV.capture_snapshot(p["dir"])
    # Dong dau van tay theo cau hinh CU truoc khi doi — neu khong, cau nao chua co dau
    # van tay (du an cu) se bi "Sinh giong" bo qua am tham va van doc giong cu.
    if ADV.backfill_voice_sigs(p["dir"], meta, p["b_chunks"]):
        TL.save_b_chunks(p["dir"], p["b_chunks"])
    cast = dict(meta.get(ADV.VOICE_CAST_FIELD) or {})
    for speaker, voice in raw.items():
        name = str(speaker or "").strip()
        if not name:
            continue
        vid = str(voice or "").strip()
        if vid:
            cast[name] = vid
        else:
            cast.pop(name, None)
    meta[ADV.VOICE_CAST_FIELD] = cast
    ADV.commit_snapshot(p["dir"], snap)
    ADV.save_meta(p["dir"], meta)
    logger.info(f"[nâng cao] Phân vai: {len(cast)} vai đã gán giọng.")
    return {
        "status": "ok",
        "cast": cast,
        "speakers": ADV.speakers_in_use(p["b_chunks"], meta),
        "timeline": await _payload(p),
    }


# --------------------------------------------------------------------------- #
#  Cai dat du an (giong mac dinh, am luong, tach nhac nen)
# --------------------------------------------------------------------------- #
class AdvancedSettings(BaseModel):
    target_voice: Optional[str] = None
    source_language: Optional[str] = None
    subtitle_mode: Optional[str] = None
    # Vùng phụ đề cứng để OCR quét — {x,y,w,h} chuẩn hoá 0..1. Khoanh đúng vùng thì
    # OCR chỉ xử lý một dải nhỏ thay vì cả khung: nhanh hơn nhiều và đỡ nhận nhầm
    # chữ trong cảnh (tiết kiệm cả CPU lẫn GPU).
    ocr_region: Optional[dict] = None
    ocr_lang: Optional[str] = None
    default_rate: Optional[str] = None
    separate_audio: Optional[bool] = None
    # Tieng goc cua video co duoc dua vao ban xuat khong. Tat = chi xuat nhung gi dang
    # nam tren timeline (dung khi da dua stem len timeline de chinh tung doan).
    source_audio_enabled: Optional[bool] = None
    bg_volume: Optional[float] = None
    voice_bg_volume: Optional[float] = None
    vocals_volume: Optional[float] = None
    voice_vocals_volume: Optional[float] = None
    #  Am luong LAN GIONG DOC AI (1.0 = 100%, mac dinh 1.5). Xem ADV.tts_volume.
    tts_volume: Optional[float] = None
    speech_style: Optional[str] = None
    brand_name: Optional[str] = None
    #  LOAI KICH BAN: "review" (viet lai thanh loi binh) hoac "dubbing" (dich sat nghia).
    #  Chi anh huong PROMPT sinh kich ban -> tuc anh huong SRT ra sao; moi buoc sau giong nhau.
    script_kind: Optional[str] = None
    target_language: Optional[str] = None
    #  Co bat AI PHAN VAI nhan vat khong (mac dinh TAT — xem ``_assign_speakers``).
    assign_speakers: Optional[bool] = None
    #  BUOC DON con cau doc khong kip thi lam gi: "hoi" (mo modal cho nguoi dung chon
    #  phuong an doi toc do) hay "ai" (nho DeepSeek viet lai ngan hon roi sinh giong
    #  lai). Cung ten khoa, cung hai gia tri voi cau hinh cua LO — mot khai niem thi
    #  chi duoc co mot ten.
    fit_mode: Optional[str] = None
    #  CHO GIONG TRAN SANG CAU SAU (xem ``TL.tran_giong_cho_phep``): bat/tat + so giay toi da (0–3,0; mac dinh 1,5).
    cho_tran_giong: Optional[bool] = None
    tran_giong: Optional[float] = None
    #  TACH CAU DAI theo dau cau + do dai (services/tach_cau.py): cau goc Whisper luc lay sub
    #  (mac dinh BAT) / ban dich luc nhap kich ban (mac dinh TAT). Cai dat CUA DU AN — "Lam
    #  mac dinh cho du an moi" luu ca hai (``mac_dinh_du_an.KHOA_CHI_TIET``).
    tach_cau_goc: Optional[bool] = None
    tach_cau_dich: Optional[bool] = None


@router.post("/demo")
async def tao_du_an_mau_chi_tiet(dung_lai: bool = False):
    """DỰ ÁN MẪU Chi tiết (services/du_an_mau.py): OCR + vùng phụ đề + giọng Edge đặt sẵn.
    Tạo qua đúng hai bước của Trang chủ (``create_project`` rồi ``enable_advanced``).
    ``dung_lai``: đã có dự án mẫu thì mở lại nó (hướng dẫn bấm từ trang danh sách)."""
    from services import du_an_mau as DM
    from services.video_service import VideoService as _VS

    if dung_lai:
        cu = await run_in_threadpool(DM.tim_chi_tiet, config_service.get_review_root())
        if cu:
            return {"project_id": cu, "title": DM.TIEU_DE, "dung_lai": True}

    try:
        path = await run_in_threadpool(
            DM.chep_vao, os.path.join(config_service.get_review_root(), "mau")
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    dur = await run_in_threadpool(_VS.get_video_duration, path)
    pid = await run_in_threadpool(
        review_service.create_project, path, float(dur or 0),
        DM.NGON_NGU_NGUON, DM.NGON_NGU_DICH, DM.GIONG_EDGE, DM.TIEU_DE,
    )
    await enable_advanced(pid)
    p = _load(pid)
    p["meta"].update(DM.META_CHI_TIET)
    ADV.save_meta(p["dir"], p["meta"])
    return {"project_id": pid, "title": DM.TIEU_DE}


class MacDinhBody(BaseModel):
    #  Lưu phần CÀI ĐẶT (ngôn ngữ, giọng, âm lượng, kiểu nói…) không.
    cai_dat: bool = True
    #  Các phần DỰNG HÌNH được lưu (khoá của ``mau_cau_hinh.PHAN_MAU``); rỗng = không lưu.
    phan: List[str] = []


@router.post("/projects/{project_id}/mac-dinh")
async def luu_mac_dinh_chi_tiet(project_id: str, body: MacDinhBody):
    """"Đặt cấu hình dự án này làm mặc định cho dự án mới" — chế độ Chi tiết."""
    from routes.review_routes import _review_compositor_config_path
    from services import mac_dinh_du_an as MD
    from services.compositor_utils import load_compositor_config

    p = _load(project_id)
    meta = p["meta"]
    cai = {k: meta[k] for k in MD.KHOA_CHI_TIET if k in meta} if body.cai_dat else {}
    if body.cai_dat:
        #  Hai ô "Tách câu" lưu GIÁ TRỊ ĐANG HIỆN kể cả khi dự án chưa từng bấm (chưa có khoá
        #  = mặc định) — lưu cấu hình là chụp đúng thứ người dùng đang nhìn thấy.
        cai[TC.KHOA_GOC] = TC.bat_goc(meta)
        cai[TC.KHOA_DICH] = TC.bat_dich(meta)
    comp = None
    if body.phan:
        comp = await run_in_threadpool(
            load_compositor_config, _review_compositor_config_path(project_id)
        )
    await run_in_threadpool(MD.luu, "chi_tiet", cai, comp, body.phan, meta.get("title") or "")
    return {"ok": True, "trang_thai": MD.trang_thai()}


class ExportRangesBody(BaseModel):
    ranges: List[dict] = []


@router.get("/projects/{project_id}/export-ranges")
async def get_export_ranges(project_id: str):
    """Các đoạn đã khoanh để "Xuất đoạn" — dùng chung trình dựng ↔ Chạy hàng loạt."""
    from services import export_ranges as ER

    _load_meta(project_id)  # 404 nếu không có dự án
    return {"ranges": ER.doc(ADV.project_dir_of(project_id))}


@router.put("/projects/{project_id}/export-ranges")
async def put_export_ranges(project_id: str, data: ExportRangesBody = Body(...)):
    from services import export_ranges as ER

    _load_meta(project_id)
    return {"ranges": ER.ghi(ADV.project_dir_of(project_id), data.ranges)}


@router.patch("/projects/{project_id}/settings")
async def patch_settings(project_id: str, data: AdvancedSettings = Body(...)):
    """Cap nhat cai dat du an nang cao (giong, toc do, tach nhac nen, 4 muc am luong)."""
    p = _load(project_id)
    _require_advanced(p)
    # ``exclude_unset`` chu khong phai ``exclude_none``: khoa nao KHONG gui thi khong dung
    # toi (dung tinh than PATCH), con khoa co gui gia tri null thi phai xoa duoc that. Chi
    # ``ocr_region`` duoc phep ve None — bo khoanh vung phu de cung la mot lua chon, ma
    # truoc day gui null bi loc mat nen khoanh nham roi khong co duong go tu giao dien.
    # Cac khoa khac gui null la vo nghia (ghi rong de len gia tri dang dung) -> bo qua.
    changed = {
        k: v
        for k, v in data.model_dump(exclude_unset=True).items()
        if v is not None or k == "ocr_region"
    }
    if changed.get("subtitle_mode") not in (None, "whisper", "ocr", "hybrid"):
        raise HTTPException(status_code=400, detail=L("Nguồn phụ đề không hợp lệ", "Invalid subtitle source"))
    #  "auto" (Tu chon ho phuong an nhe) tung bi tu choi o day -> giao dien doi ma khong luu duoc (ra soat 07-10-2026).
    if changed.get("fit_mode") not in (None, "hoi", "auto", "ai"):
        raise HTTPException(status_code=400, detail=L("Cách xử lý câu không vừa không hợp lệ", "Invalid way of handling sentences that do not fit"))
    if "tran_giong" in changed:
        changed["tran_giong"] = round(TL.tran_giong_giay(changed["tran_giong"]), 2)
    if "script_kind" in changed:
        changed["script_kind"] = PROMPTS.normalize_kind(changed["script_kind"])
    # KEP MOI MUC AM LUONG vao 0..2 ngay tai cua ngo. Truoc day duong nay ghi thang so
    # nhan duoc xuong metadata, khong kep gi — mot con so hong (hay mot ban giao dien cu
    # gui thang 200 thay vi 2.0) la thanh he so x200 trong lenh ffmpeg, xuat ra vo ca loa.
    for k in (*ADV.PROJECT_MIX_KEYS, ADV.TTS_VOLUME_FIELD):
        if k in changed:
            changed[k] = TL.clamp_volume(changed[k], 0.0)
    # Nguoi dung vua cham vao 4 muc am luong -> danh dau, de ``render()`` khong con di
    # duong tat ``-c copy`` (duong do bo qua het 4 muc). Nho co nay ma dat 0% CO Y cung
    # duoc ton trong, khong bi lan voi du an vua tao (cung dang 0%). Xem ADV.mix_touched.
    if any(k in changed for k in ADV.PROJECT_MIX_KEYS):
        changed[ADV.MIX_TOUCHED_FIELD] = True
    # BAT "Tach giong & nhac nen" -> DAT LAI 4 muc ve mac dinh, neu nguoi dung chua tu
    # chinh muc nao.
    #
    # Vi sao: truoc khi tach thi chi hai muc dau co nghia (nhac + giong goc con dinh lien
    # mot duong tieng). Vua tach xong, hai thanh "Giong goc" moi hien ra — de chung o
    # muc nao do cua du an cu la nghe CHONG HAI GIONG (giong AI doc de len giong nguoi
    # noi that). Mac dinh dung la: nhac nen 100% / ha con 25% luc AI noi, giong goc im
    # han ca hai chieu.
    #
    # CHI lam khi chua ai cham toi (``mix_touched`` con False): da tu keo roi thi con so
    # do la y muon cua nguoi dung, ghi de len la xoa mat cong ho vua chinh.
    if changed.get("separate_audio") and not p["meta"].get("separate_audio"):
        if not ADV.mix_touched(p["meta"]) and not any(
            k in changed for k in ADV.PROJECT_MIX_KEYS
        ):
            changed.update(ADV.MIX_DEFAULTS)
    # Doi GIONG / TOC DO mac dinh la doi giong that su cua moi cau dang theo mac dinh.
    # Dong dau van tay theo cau hinh CU truoc khi ap thay doi, de tu day co che phat
    # hien lech nhan ra chung (xem ADV.backfill_voice_sigs).
    if ("target_voice" in changed or "default_rate" in changed) and ADV.backfill_voice_sigs(
        p["dir"], p["meta"], p["b_chunks"]
    ):
        TL.save_b_chunks(p["dir"], p["b_chunks"])
    p["meta"].update(changed)
    ADV.save_meta(p["dir"], p["meta"])
    return {"status": "ok", "settings": changed}


@router.get("/projects/{project_id}/settings")
async def get_settings(project_id: str):
    p = _load(project_id)
    _require_advanced(p)
    m = p["meta"]
    return {
        "target_voice": m.get("target_voice") or "vi-VN-NamMinhNeural",
        "default_rate": m.get("default_rate") or "auto",
        "separate_audio": bool(m.get("separate_audio", False)),
        "fit_mode": m.get("fit_mode") or "hoi",
        "cho_tran_giong": bool(m.get(TL.TRAN_BAT_FIELD, True)),
        "tran_giong": TL.tran_giong_giay(m.get(TL.TRAN_GIAY_FIELD, TL.TRAN_GIONG_MAC_DINH)),
        "source_audio_enabled": bool(m.get("source_audio_enabled", True)),
        "bg_volume": float(m.get("bg_volume", 0.0) or 0.0),
        "voice_bg_volume": float(m.get("voice_bg_volume", 0.0) or 0.0),
        "vocals_volume": float(m.get("vocals_volume", 0.0) or 0.0),
        "voice_vocals_volume": float(m.get("voice_vocals_volume", 0.0) or 0.0),
        # Chua dat bao gio -> tra ve mac dinh 1.5, dung con so ma ``render()`` se dung.
        "tts_volume": ADV.tts_volume(m),
        "speech_style": m.get("speech_style") or "auto",
        "brand_name": m.get("brand_name") or "",
        "source_language": m.get("source_language") or "auto",
        "target_language": m.get("target_language") or "vi",
        "script_kind": PROMPTS.normalize_kind(m.get("script_kind")),
        "assign_speakers": _assign_speakers(m),
        "voice_cast": m.get(ADV.VOICE_CAST_FIELD) or {},
        "subtitle_mode": m.get("subtitle_mode") or "whisper",
        "tach_cau_goc": TC.bat_goc(m),
        "tach_cau_dich": TC.bat_dich(m),
        "ocr_region": m.get("ocr_region"),
        "ocr_lang": m.get("ocr_lang") or "ch",
        "title": m.get("title") or "",
        "video_path": m.get("video_path") or "",
        "state": m.get("state") or {},
    }


# --------------------------------------------------------------------------- #
#  Timeline
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/timeline")
async def get_timeline(project_id: str, include_a: bool = True):
    p = _load(project_id)
    _require_advanced(p)
    data = await _payload(p, include_a)
    data["busy"] = _is_review_busy(project_id)
    data["render_complete"] = bool(p["meta"].get("state", {}).get("render_complete"))
    return data


@router.put("/projects/{project_id}/timeline")
async def save_timeline(project_id: str, data: TimelineSave = Body(...)):
    """Luu ca me sau khi keo tha nhieu clip.

    Luon sap theo thoi gian bat dau roi danh so lai b_id 1..N (xem commit_chunks).
    """
    p = _load(project_id)
    _require_advanced(p)
    if not data.moves:
        return await _payload(p)

    by_uid = {m.uid: m for m in data.moves}
    missing, applied = [], 0
    for uid, m in by_uid.items():
        b = TL.find_chunk(p["b_chunks"], uid)
        if b is None:
            missing.append(uid)
            continue
        TL.patch_chunk(p["b_chunks"], uid, {
            "start_override": round(float(m.start), 3),
            "end_override": round(float(max(m.end, m.start + 0.05)), 3),
        })
        applied += 1

    # Cau da bi xoa/thay uid o mot tab khac thi BO QUA rieng cau do, dung nem ca me.
    # Truoc day bao 404 cho ca lo: nguoi dung keo mot loat clip roi mat trang, khong
    # hieu tai sao — trong khi phan lon cac clip van dat vi tri hoan toan hop le.
    if missing:
        logger.warning(
            f"[nâng cao] Bỏ qua {len(missing)} câu không còn trong dự án khi lưu "
            f"timeline: {missing[:5]}"
        )
    if not applied:
        raise HTTPException(
            status_code=404,
            detail=L("Không còn câu nào trong danh sách vừa kéo — hãy tải lại timeline.", "No sentences left in the dragged list — reload the timeline."),
        )

    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"])
    data_out = await _payload(p)
    if missing:
        data_out["skipped_uids"] = missing
    return data_out


@router.post("/projects/{project_id}/import")
async def import_script(project_id: str, data: ScriptImport = Body(...)):
    """Nhap kich ban B — duong RIENG cua che do nang cao.

    KHONG dung ``POST /api/review/projects/{id}/import`` cua duong co dien: ham do ghi
    de b_chunks.json bang danh sach khong co ``uid``, khien cau moi thua huong thu muc
    audio cua kich ban cu (giong cu dan vao chu moi) va xoa sach cong dung tren
    timeline. Xem ``ADV.import_script`` de biet chi tiet.
    """
    p = _load(project_id)
    _require_advanced(p)
    try:
        res = await run_in_threadpool(
            ADV.import_script, p["dir"], p["meta"], p["a_lookup"], data.b_chunks,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2)}


@router.patch("/projects/{project_id}/clips/{uid}")
async def patch_clip(project_id: str, uid: str, data: ClipPatch = Body(...)):
    """Sua 1 cau: chu / giong / toc do / thoi gian.

    Doi CHU thi giong cu khong con dung nua -> tra ve co ``needs_voice`` de UI nhac
    bam Sinh lai giong (khong tu dong sinh, tranh gọi TTS ngoai y muon).
    """
    p = _load(project_id)
    _require_advanced(p)
    b = TL.find_chunk(p["b_chunks"], uid)
    if b is None:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy câu", "Sentence not found"))

    changes = {}
    if data.b_text is not None:
        changes["b_text"] = data.b_text.strip()
    if data.voice is not None:
        changes["voice"] = data.voice or None
    if data.rate is not None:
        changes["rate"] = data.rate or None
    if data.speaker is not None:
        changes["speaker"] = data.speaker.strip() or None
    if data.start is not None:
        changes["start_override"] = round(float(data.start), 3)
    if data.end is not None:
        changes["end_override"] = round(float(data.end), 3)

    text_changed = "b_text" in changes and changes["b_text"] != (b.get("b_text") or "").strip()
    voice_changed = "voice" in changes and changes["voice"] != b.get("voice")
    rate_changed = "rate" in changes and changes["rate"] != b.get("rate")
    # Doi VAI cung la doi giong: vai khac co the dang gan giong khac trong bang phan vai.
    speaker_changed = "speaker" in changes and changes["speaker"] != b.get("speaker")

    TL.patch_chunk(p["b_chunks"], uid, changes)
    if text_changed or voice_changed or rate_changed or speaker_changed:
        b["speed_factor"] = None   # so lieu tang toc cu khong con dung

    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"])
    return {
        "status": "ok",
        "needs_voice": bool(
            text_changed or voice_changed or rate_changed or speaker_changed
        ),
        "timeline": await _payload(p),
    }


@router.post("/projects/{project_id}/clips")
async def create_clip(project_id: str, data: ClipCreate = Body(...)):
    """Chen mot cau B moi ngay tren timeline (khong can co A-chunk tuong ung)."""
    p = _load(project_id)
    _require_advanced(p)

    start = float(data.start)
    end = float(data.end) if data.end is not None else start + 2.0
    if end <= start:
        end = start + 2.0

    chunk = TL.make_chunk(
        data.b_text, start, end,
        voice=data.voice, rate=data.rate, speaker=data.speaker,
    )
    p["b_chunks"].append(chunk)
    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"])
    return {"status": "created", "uid": chunk["uid"], "timeline": await _payload(p)}


@router.delete("/projects/{project_id}/clips/{uid}")
async def remove_clip(project_id: str, uid: str):
    p = _load(project_id)
    _require_advanced(p)
    if TL.find_chunk(p["b_chunks"], uid) is None:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy câu", "Sentence not found"))

    remaining = TL.delete_chunk(p["b_chunks"], uid)
    ADV.commit_chunks(p["dir"], remaining, p["a_lookup"])
    p["b_chunks"] = remaining
    return {"status": "deleted", "timeline": await _payload(p)}


@router.post("/projects/{project_id}/clips/bulk-delete")
async def remove_clips_bulk(project_id: str, data: dict = Body(...)):
    """Xoa NHIEU cau binh luan trong MOT lan (chon nhieu tren timeline roi bam Delete).

    Vi sao khong de FE goi ``DELETE /clips/{uid}`` nhieu lan: moi lan goi la mot buoc
    hoan tac rieng, nen xoa 30 cau roi Ctrl+Z chi lay lai duoc MOT cau — phai bam 30
    lan. Chua ke 30 luot ghi b_chunks.json xuong dia. O day chup MOT anh, ghi MOT lan.
    """
    p = _load(project_id)
    _require_advanced(p)
    uids = [str(u) for u in (data.get("uids") or []) if u]
    co = [u for u in uids if TL.find_chunk(p["b_chunks"], u) is not None]
    if not co:
        return {"status": "ok", "deleted": 0, "timeline": await _payload(p)}
    ADV.take_snapshot(p["dir"])
    for u in co:
        p["b_chunks"] = TL.delete_chunk(p["b_chunks"], u)
    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    logger.info(f"[nâng cao] Xoá {len(co)} câu bình luận (chọn nhiều trên timeline).")
    return {"status": "ok", "deleted": len(co), "timeline": await _payload(p)}


@router.post("/projects/{project_id}/a-chunks/bulk-delete")
async def remove_a_chunks_bulk(project_id: str, data: dict = Body(...)):
    """Xoa NHIEU cau phu de goc trong MOT lan. Cung mot buoc hoan tac."""
    p = _load(project_id)
    _require_advanced(p)
    want = {int(i) for i in (data.get("a_ids") or []) if str(i).lstrip("-").isdigit()}
    doomed = [
        c for c in SEG.load_a_chunks(p["dir"])
        if isinstance(c, dict) and c.get("a_id") is not None and int(c["a_id"]) in want
    ]
    if not doomed:
        return {"status": "ok", "deleted": 0, "timeline": await _payload(p)}
    ADV.take_snapshot(p["dir"])
    r = SEG.delete_a_chunks(p["dir"], p["a_lookup"], p["b_chunks"], doomed)
    ADV.commit_chunks(p["dir"], p["b_chunks"], p["a_lookup"], snapshot_first=False)
    return {"status": "ok", **r, "timeline": await _payload(p, include_a=True)}


@router.post("/projects/{project_id}/clips/{uid}/split")
async def split_clip(project_id: str, uid: str, data: SplitRequest = Body(default=SplitRequest())):
    """Cat 1 cau thanh 2 cau doc lap; ca hai deu can sinh lai giong."""
    p = _load(project_id)
    _require_advanced(p)
    try:
        res = TL.split_chunk(p["b_chunks"], uid, at_ratio=data.at_ratio,
                             at_time=data.at_time, a_lookup=p["a_lookup"])
    except KeyError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy câu", "Sentence not found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ADV.commit_chunks(p["dir"], res["b_chunks"], p["a_lookup"])
    p["b_chunks"] = res["b_chunks"]
    return {"status": "split", "uids": res["uids"], "needs_voice": res["uids"],
            "timeline": await _payload(p)}


@router.post("/projects/{project_id}/clips/{uid}/split-sentences")
async def split_clip_sentences(project_id: str, uid: str):
    """Tach 1 cau thanh NHIEU cau theo DAU CHAM — mỗi câu một clip riêng.

    Kich ban AI tra ve tung KHOI loi binh 3-4 cau; muon moi cau nam mot cho thi cho cat
    mong muon luon la dau cham, khong phai vi tri con tro. Khung thoi gian cu duoc chia
    theo do dai chu, tong cong giu nguyen. Moi manh can sinh lai giong (uid moi).
    """
    p = _load(project_id)
    _require_advanced(p)
    # Cho tach khong hop le (chi co MOT cau, chua co thoi gian) la ca ValueError — chup vao
    # bo nho truoc, chi ghi vao ngan xep hoan tac khi da tach that.
    snap = ADV.capture_snapshot(p["dir"])
    try:
        res = TL.split_chunk_sentences(p["b_chunks"], uid, a_lookup=p["a_lookup"])
    except KeyError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy câu", "Sentence not found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ADV.commit_snapshot(p["dir"], snap)
    ADV.commit_chunks(p["dir"], res["b_chunks"], p["a_lookup"], snapshot_first=False)
    p["b_chunks"] = res["b_chunks"]
    return {
        "status": "split", "uids": res["uids"], "needs_voice": res["uids"],
        "timeline": await _payload(p),
    }


@router.post("/projects/{project_id}/clips/split-sentences-all")
async def split_all_clips_sentences(project_id: str):
    """Tach TOAN BO cau B theo DAU CHAM trong MOT luot — moi cau mot clip.

    Duong dan khong co ``{uid}`` nen khong dam voi ``/clips/{uid}/split-sentences``.

    HOAN TAC: chi MOT anh chup cho ca luot, nen mot lan Ctrl+Z la ve nguyen trang toan bo
    du an — dung tinh than "mot thao tac cua nguoi dung = mot buoc lui".
    """
    p = _load(project_id)
    _require_advanced(p)
    # Chup vao bo nho truoc, chi ghi vao ngan xep khi da tach that: khong co cau nao tach
    # duoc ma van ghi thi nguoi dung an mot buoc hoan tac RONG va mat sach ngan xep lam
    # lai (cung ly le voi ban tach mot cau).
    snap = ADV.capture_snapshot(p["dir"])
    res = TL.split_all_chunks_sentences(p["b_chunks"], a_lookup=p["a_lookup"])
    if not res["uids"]:
        raise HTTPException(
            status_code=400,
            detail=L("Không có câu nào tách được — mọi câu đều chỉ có một câu, "
                     "hoặc chưa xác định được thời gian.",
                     "No sentence can be split — every sentence is a single sentence, "
                     "or the timing is not determined yet."),
        )

    ADV.commit_snapshot(p["dir"], snap)
    ADV.commit_chunks(p["dir"], res["b_chunks"], p["a_lookup"], snapshot_first=False)
    p["b_chunks"] = res["b_chunks"]
    return {
        "status": "split",
        # so cau GOC bi tach / tong so cau sau khi tach
        "split": res["split"],
        "total": res["total"],
        "uids": res["uids"],
        "needs_voice": res["uids"],
        "timeline": await _payload(p),
    }


@router.get("/projects/{project_id}/cleanup")
async def scan_cleanup(project_id: str):
    """Liet ke DU LIEU RAC cua du an. CHI DOC, khong dung gi vao dia.

    Tach doi quet/xoa de giao dien hien duoc "se bo nhung gi, nang bao nhieu" TRUOC khi
    nguoi dung dong y — xoa hang GB ma khong noi truoc thi khong ai dam bam.
    """
    p = _load(project_id)
    _require_advanced(p)
    return await run_in_threadpool(CLEAN.scan, p["dir"], p["meta"], p["b_chunks"])


@router.post("/projects/{project_id}/cleanup")
async def run_cleanup(project_id: str, data: dict = Body(default={})):
    """Xoa dung nhung gi ban quet vua liet ke. ``keys`` = chi lam may nhom do.

    QUET LAI ngay tai day thay vi tin danh sach do FE gui len: giua luc xem bao cao va
    luc bam dong y, nguoi dung co the da them clip / sinh giong — lam theo danh sach cu
    la xoa nham thu vua tao ra.
    """
    p = _load(project_id)
    _require_advanced(p)
    keys = data.get("keys") if isinstance(data, dict) else None

    # KHOA VIEC NEN. Don rac xoa thu muc theo danh sach "khong con ai dung", ma job nen
    # (sinh giong, tach stem, dung phim) thi VUA CHAY VUA TAO ra dung nhung thu muc do.
    # Chay chen vao giua la xoa mat tep dang duoc ghi dở — hong ca job lan du lieu.
    _acquire_review_or_409(project_id)
    try:
        return await _do_cleanup(p, keys)
    finally:
        _release_review(project_id)


async def _do_cleanup(p, keys):
    def _work():
        report = CLEAN.scan(p["dir"], p["meta"], p["b_chunks"])
        return report, CLEAN.apply(p["dir"], p["meta"], report, keys)

    # Go muc khoi kho la sua metadata -> phai lui duoc. Cac TEP da xoa thi khong,
    # va noi ro dieu do o giao dien: rac tren dia xoa la mat han.
    snap = ADV.capture_snapshot(p["dir"])
    report, res = await run_in_threadpool(_work)
    if res["meta_changed"]:
        ADV.commit_snapshot(p["dir"], snap)
        ADV.save_meta(p["dir"], p["meta"])
    return {"status": "ok", **res, "scanned": report["total"],
            "timeline": await _payload(p)}


@router.post("/projects/{project_id}/undo")
async def undo(project_id: str):
    p = _load(project_id)
    _require_advanced(p)
    ok = await run_in_threadpool(ADV.undo_last, p["dir"])
    if not ok:
        raise HTTPException(status_code=400, detail=L("Không còn thao tác nào để hoàn tác.", "Nothing left to undo."))
    p2 = _load(project_id)
    return {
        "status": "undone",
        **ADV.history_state(p["dir"]),
        #  KÈM a_chunks: ảnh chụp hoàn tác có cả phụ đề gốc (xoá câu gốc, giãn theo tốc
        #  độ video…). Thiếu nó thì FE giữ danh sách ĐANG CÓ — tức danh sách đã xoá — nên
        #  Ctrl+Z xoá phụ đề gốc trông như không hoàn tác được (người dùng báo 28-09-2026),
        #  dù trên đĩa đã khôi phục đúng.
        "timeline": await _payload(p2, include_a=True),
    }


@router.post("/projects/{project_id}/redo")
async def redo(project_id: str):
    """Lam lai buoc vua hoan tac (Ctrl+Shift+Z / Ctrl+Y)."""
    p = _load(project_id)
    _require_advanced(p)
    ok = await run_in_threadpool(ADV.redo_last, p["dir"])
    if not ok:
        raise HTTPException(status_code=400, detail=L("Không còn thao tác nào để làm lại.", "Nothing left to redo."))
    p2 = _load(project_id)
    return {
        "status": "redone",
        **ADV.history_state(p["dir"]),
        #  KÈM a_chunks: ảnh chụp hoàn tác có cả phụ đề gốc (xoá câu gốc, giãn theo tốc
        #  độ video…). Thiếu nó thì FE giữ danh sách ĐANG CÓ — tức danh sách đã xoá — nên
        #  Ctrl+Z xoá phụ đề gốc trông như không hoàn tác được (người dùng báo 28-09-2026),
        #  dù trên đĩa đã khôi phục đúng.
        "timeline": await _payload(p2, include_a=True),
    }


# --------------------------------------------------------------------------- #
#  Sinh giong
# --------------------------------------------------------------------------- #
@router.post("/projects/{project_id}/voices")
async def generate_voices(project_id: str, data: VoiceRequest = Body(default=VoiceRequest())):
    """Sinh giong: bo trong ``uids`` = hang loat; truyen uid = tung cau.

    Giong giu NGUYEN toc do da chon, khong cat, khong tu tang toc.
    """
    p = _load(project_id)
    _require_advanced(p)

    _acquire_review_or_409(project_id)
    handler = setup_project_logging(project_id, config_service.get_review_root())
    review_service.clear_render_cancel(project_id)   # xoá cờ huỷ còn sót của lần trước
    try:
        #  XẾP HÀNG cùng mọi việc nặng khác (xem services/work_queue.py). Xin chỗ
        #  trong luồng phụ để không chặn vòng lặp sự kiện của server.
        async with await WQ.slot_async(
            project_id, L("Sinh giọng", "Generate voices"),
            cancelled=lambda: review_service.is_render_cancelled(project_id),
        ):
            res = await ADV.generate_voices(project_id, uids=data.uids, force=data.force)
    except RenderCancelled:
        # Người dùng bấm Huỷ giữa chừng — KHÔNG phải lỗi, trả về trạng thái bình thường.
        logger.warning("[nâng cao] Đã huỷ tạo giọng theo yêu cầu.")
        p2 = _load(project_id)
        return {
            "total": 0, "ok": 0, "failed": 0, "failed_uids": [],
            "cancelled": True, "timeline": await _payload(p2),
        }
    except Exception as e:
        logger.error(f"[nâng cao] Sinh giọng lỗi: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Tạo giọng thất bại: {e}")
    finally:
        remove_project_logging(project_id, handler)
        _release_review(project_id)

    #  Việc đếm lượt giọng nay nằm TRONG ``ADV.generate_voices`` — mọi đường gọi (nút
    #  này, chạy hàng loạt, Dồn-nhờ-DeepSeek) đều được đếm. Đếm thêm ở đây là đếm đôi.

    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2)}


@router.get("/projects/{project_id}/clips/{uid}/audio")
async def get_clip_audio(project_id: str, uid: str):
    """Tra file giong cua 1 cau — dung cho nut nghe thu va phat lai trong editor."""
    p = _load(project_id)
    b = TL.find_chunk(p["b_chunks"], uid)
    if b is None:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy câu", "Sentence not found"))

    start, end = None, None
    from services.review_render_cache import resolve_b_time
    start, end = resolve_b_time(b, p["a_lookup"])
    window = max(0.0, float(end) - float(start)) if start is not None else 0.0
    #  Cung muc tran voi ban xuat (``render``) — nghe thu phai giong het thu se nghe trong video.
    path, _dur, _trim = await run_in_threadpool(ADV.resolve_playback_audio, p["dir"], b, window,
                                                TL.tran_giong_cho_phep(p["meta"]))
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=L("Câu này chưa có giọng đọc", "This sentence has no voice yet"))
    return FileResponse(path, media_type="audio/wav",
                        headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- #
#  Hai nut tu dong
# --------------------------------------------------------------------------- #
@router.post("/projects/{project_id}/auto-arrange")
async def auto_arrange(project_id: str):
    p = _load(project_id)
    _require_advanced(p)
    res = await run_in_threadpool(ADV.auto_arrange, project_id)
    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2)}


@router.get("/projects/{project_id}/arrange-fit-plan")
async def arrange_fit_plan(project_id: str):
    """Cac PHUONG AN doi toc do cho vua phim — chi tinh, KHONG sua gi.

    FE goi sau khi ``auto-arrange`` bao con cau bi cat, roi hien modal cho nguoi dung
    chon. Tach lam hai buoc co y: doi toc do ca du an la viec lon, khong duoc tu dong.
    """
    p = _load(project_id)
    _require_advanced(p)
    return await run_in_threadpool(ADV.plan_arrange_fit, project_id)


class FitPlanRequest(BaseModel):
    voice_speed: float = 1.0
    video_speed: float = 1.0
    #  "deu" = tang toc dong loat + giam toc phim; "cuc_bo" = chi tang toc cac cau bi dai.
    kind: str = "deu"


# --------------------------------------------------------------------------- #
#  NHO DEEPSEEK RUT GON cac cau con thieu cho (thay vi bat nguoi dung sua tay)
# --------------------------------------------------------------------------- #
def _luu_trang_thai_rut_gon(project_id: str, *, result: dict = None,
                            error: str = None, started: bool = False) -> None:
    """Ghi trang thai job rut gon vao metadata (3 khoa RIENG, nhu job AI kich ban)."""
    try:
        p = ADV.load_project(project_id, migrate=False)
        state = p["meta"].setdefault("state", {})
        state["fit_ai_started"] = bool(started)
        if started:
            state.pop("fit_ai_error", None)
            state.pop("fit_ai_result", None)
        if result is not None:
            state["fit_ai_result"] = result
        if error is not None:
            state["fit_ai_error"] = error
        ADV.save_meta(p["dir"], p["meta"])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chi tiet] Khong ghi duoc trang thai job rut gon: {e}")


async def _chay_rut_gon(project_id: str, model: str, so_vong: int) -> None:
    """Chay job rut gon o NEN, co gan nhat ky du an de nguoi dung theo doi duoc."""
    from routes.review_routes import remove_project_logging, setup_project_logging

    handler = await run_in_threadpool(
        setup_project_logging, project_id, config_service.get_review_root()
    )
    try:
        logger.info("=== NHO DEEPSEEK RUT GON CAC CAU CON THIEU CHO ===")
        res = await FITAI.rut_gon_cho_vua(
            project_id, model=model, so_vong=so_vong, ghi_log=logger.info,
        )
        logger.info(
            f"=== XONG: rut gon {res.get('changed', 0)} cau, "
            f"con {res.get('cut', 0)} cau thieu cho ==="
        )
        _luu_trang_thai_rut_gon(project_id, result=res, started=False)
    except FITAI.RutGonError as e:
        logger.error(f"[chi tiet] Rut gon that bai: {e}")
        _luu_trang_thai_rut_gon(project_id, error=e.user_message[:500], started=False)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[chi tiet] Rut gon that bai: {e}", exc_info=True)
        _luu_trang_thai_rut_gon(project_id, error=str(e)[:500], started=False)
    finally:
        await run_in_threadpool(remove_project_logging, project_id, handler)
        _release_review(project_id)


class RutGonRequest(BaseModel):
    #: So vong thu lai. Vong 2 vet nhung cau model viet van con dai.
    rounds: int = 0
    model: Optional[str] = None


@router.post("/projects/{project_id}/arrange/ai-shorten")
async def ai_shorten(project_id: str, background_tasks: BackgroundTasks,
                     data: RutGonRequest = Body(default=RutGonRequest())):
    """Nho DeepSeek VIET LAI NGAN HON cac cau con thieu cho, roi sinh giong lai.

    Loi ra thay cho "vao sua tay tung cau": bam nut nay thi cac cau doc khong kip duoc
    viet lai gon hon dung bang chỗ con lai, sinh giong lai, roi don lai — tu dong.

    Chay o NEN (DeepSeek + TTS co the vai phut) va ghi vao nhat ky du an nhu moi viec
    nang khac. FE hoi ``/arrange/ai-status`` de biet xong chua.
    """
    p = _load(project_id)
    _require_advanced(p)

    import services.deepseek_service as deepseek_service
    if not deepseek_service.is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                L("Chưa cấu hình API key của bộ AI đang chọn (DeepSeek / Gemini) — vào Cài đặt để nhập key trước, "
                  "hoặc chọn cách chỉnh tay như cũ.",
                  "The API key of the selected AI (DeepSeek / Gemini) is not configured — go to Settings to enter the key first, "
                  "or choose to adjust by hand as before.")
            ),
        )

    do = await run_in_threadpool(FITAI.cau_chua_vua, project_id)
    if not do["items"]:
        return {"status": "skipped", "message": L("Không còn câu nào thiếu chỗ.", "No sentences are short of room anymore.")}

    model = data.model or config_service.ai_model_mac_dinh()
    so_vong = data.rounds or FITAI.SO_VONG_MAC_DINH

    _acquire_review_or_409(project_id)
    try:
        _luu_trang_thai_rut_gon(project_id, started=True)
        background_tasks.add_task(_chay_rut_gon, project_id, model, so_vong)
    except Exception:
        _release_review(project_id)
        raise
    return {"status": "started", "count": len(do["items"]), "model": model}


@router.get("/projects/{project_id}/arrange/ai-status")
async def ai_shorten_status(project_id: str):
    """Job rut gon xong chua + ket qua / loi."""
    p = _load(project_id)
    state = (p["meta"].get("state") or {})
    return {
        "started": bool(state.get("fit_ai_started")),
        "result": state.get("fit_ai_result"),
        "error": state.get("fit_ai_error") or "",
    }


@router.post("/projects/{project_id}/apply-fit-plan")
async def apply_fit_plan(project_id: str, data: FitPlanRequest = Body(...)):
    """Ap dung phuong an nguoi dung da chon trong modal.

    Ca ba viec (giam toc video + tang toc giong dong loat + don lai) nam trong MOT anh
    chup, nen bam Hoan tac MOT lan la ve nguyen trang.
    """
    p = _load(project_id)
    _require_advanced(p)
    res = await run_in_threadpool(
        ADV.apply_fit_plan, project_id, data.voice_speed, data.video_speed, data.kind
    )
    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2, include_a=True)}


@router.post("/projects/{project_id}/arrange-fit-auto")
async def arrange_fit_auto(project_id: str):
    """"Tu chon ho": may tu chon mot phuong an NHE ma xong han roi ap dung luon.

    Khong co phuong an nao vua nhe vua xong han -> KHONG sua gi, tra ``applied=False``
    kem ``plan`` de FE mo modal cho nguoi dung quyet — dung y "cai nao khong chon duoc
    thi van la toi quyet".
    """
    p = _load(project_id)
    _require_advanced(p)
    res = await run_in_threadpool(ADV.tu_chon_va_ap_dung, project_id)
    if not res.get("applied"):
        return res
    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2, include_a=True)}


@router.post("/projects/{project_id}/auto-speed")
async def auto_speed(project_id: str, data: SpeedRequest = Body(default=SpeedRequest())):
    p = _load(project_id)
    _require_advanced(p)
    res = await run_in_threadpool(ADV.auto_speed, project_id, data.ceiling, data.uids)
    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2)}


@router.post("/projects/{project_id}/apply-speed")
async def apply_speed(project_id: str, data: ApplySpeedRequest = Body(...)):
    """Ap 1 he so toc do cho toan bo cau, tra ve timeline moi de nghe thu ngay."""
    p = _load(project_id)
    _require_advanced(p)
    res = await run_in_threadpool(ADV.apply_uniform_speed, project_id, data.target)
    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2)}


@router.post("/projects/{project_id}/reset-speed")
async def reset_speed(project_id: str):
    p = _load(project_id)
    _require_advanced(p)
    res = await run_in_threadpool(ADV.reset_speed, project_id)
    p2 = _load(project_id)
    return {**res, "timeline": await _payload(p2)}


# --------------------------------------------------------------------------- #
#  Huy tac vu dang chay
# --------------------------------------------------------------------------- #
@router.post("/projects/{project_id}/cancel")
async def cancel_running(project_id: str):
    """Huy MOI viec dai dang chay cua du an: tao giong, dung phim, lay phu de.

    Dat CA HAI co huy (render + analysis) vi o che do nang cao nguoi dung chi thay
    mot nut "Huy" duy nhat — khong bat ho phai biet dang chay buoc nao.
    """
    p = _load(project_id)
    _require_advanced(p)
    stopped = []
    try:
        if review_service.cancel_render(project_id):
            stopped.append("render")
    except Exception as e:
        logger.warning(f"[nâng cao] Huỷ dựng lỗi (bỏ qua): {e}")
    try:
        if review_service.cancel_analysis(project_id):
            stopped.append("analysis")
    except Exception as e:
        logger.warning(f"[nâng cao] Huỷ phân tích lỗi (bỏ qua): {e}")
    logger.warning(f"[nâng cao] Người dùng yêu cầu huỷ ({', '.join(stopped) or 'không có việc nào'})")
    return {"status": "cancelling", "stopped": stopped, "busy": _is_review_busy(project_id)}


@router.get("/projects/{project_id}/status")
async def get_status(project_id: str):
    """Cho FE biet co viec nen nao dang chay (de bat/tat nut Huy).

    Endpoint nay bi hoi lien tuc nen chi doc metadata (xem _load_meta).
    """
    meta = _load_meta(project_id)
    state = meta.get("state") or {}
    return {
        "busy": _is_review_busy(project_id),
        "render_started": bool(state.get("render_started")),
        "analysis_started": bool(state.get("analysis_started")),
        "render_complete": bool(state.get("render_complete")),
        # FE cho theo doi nut Xuat: bao loi ngay thay vi cho het gio.
        "render_error": state.get("render_error") or "",
        # TIEN DO THAT cua buoc 1/2 (ghep tieng). Truoc day buoc nay chi co "dang chay /
        # xong" nen thanh tien do phai chay vo dinh suot hang chuc phut voi phim dai —
        # nguoi dung khong biet con bao lau, tuong ung dung treo. Doc tu tep rieng nen
        # khong lam nang endpoint bi hoi 3 giay mot lan nay.
        "render_progress": ADV.read_progress(ADV.project_dir_of(project_id)) or None,
        # Job TAO KICH BAN BANG AI — de FE biet dang chay / vua xong / loi gi.
        "ai_script_started": bool(state.get("ai_script_started")),
        "ai_script_error": state.get("ai_script_error") or "",
        "ai_script_result": state.get("ai_script_result") or None,
        # {phase: "context" | "script", done, total} khi job tạo kịch bản đang chạy.
        "ai_script_progress": (
            _doc_tien_do_ai(ADV.project_dir_of(project_id))
            if state.get("ai_script_started") else None
        ),
        # Job TOM TAT VIDEO rieng (nut "Tao tom tat").
        "ai_context_started": bool(state.get("ai_context_started")),
        "ai_context_error": state.get("ai_context_error") or "",
    }


# --------------------------------------------------------------------------- #
#  Dung phim
# --------------------------------------------------------------------------- #
def _render_wrapper(project_id: str, ranges=None):
    """Chay nen: ghi log vao synthesis.log de FE theo doi qua SSE san co.

    ``ranges`` = cac doan nguoi dung khoanh tren timeline cho "Xuat doan da chon". Truyen
    thang xuong ``ADV.render`` de no CAT NGUON TRUOC khi ghep — xem chu thich o do.
    """
    try:
        #  XẾP HÀNG: mỗi lúc chỉ một việc nặng chạy trên máy (xem
        #  services/work_queue.py). Chưa tới lượt thì đợi chứ không bị từ chối.
        with WQ.slot(project_id, L("Dựng video (chi tiết)", "Render video (Advanced)"), cancelled=lambda: review_service.is_render_cancelled(project_id)):
            handler = setup_project_logging(project_id, config_service.get_review_root())
            try:
                logger.info("=== BẮT ĐẦU DỰNG VIDEO (CHẾ ĐỘ NÂNG CAO) ===")
                asyncio.run(ADV.render(project_id, ranges=ranges))
                p = ADV.load_project(project_id, migrate=False)
                state = p["meta"].setdefault("state", {})
                state["render_complete"] = True
                state["render_started"] = False
                state.pop("render_error", None)
                state["render_run_id"] = int(state.get("render_run_id", 0)) + 1
                ADV.save_meta(p["dir"], p["meta"])
                # Ghi nhận 1 dự án CHI TIẾT hoàn thành (dựng lại cũng tính +1), gộp luôn số
                # voice đã sinh trong dự án. Chỉ ghi local + gửi nền → không chặn/không lỗi.
                try:
                    from services.usage_reporter import record_completion
                    record_completion(str(project_id), "review")
                except Exception as _e:
                    logger.warning(f"Không ghi được usage (hoàn thành nâng cao): {_e}")
                logger.info("=== DỰNG VIDEO HOÀN TẤT ===")
            except RenderCancelled:
                logger.warning("=== ĐÃ HỦY DỰNG VIDEO THEO YÊU CẦU ===")
                review_service.mark_render_failed(project_id, L("Đã hủy theo yêu cầu người dùng.", "Cancelled at the user's request."))
            except Exception as e:
                logger.error(f"Dựng video thất bại: {e}", exc_info=True)
                review_service.mark_render_failed(project_id, str(e))
            finally:
                # Buoc 1/2 da ket thuc (xong / loi / huy) -> bo tien do de UI khong con hien
                # con so cu khi nguoi dung mo lai du an.
                try:
                    ADV.clear_progress(ADV.project_dir_of(project_id))
                except Exception:
                    pass
                remove_project_logging(project_id, handler)
                _release_review(project_id)


    except WQ.WorkCancelled:
        #  Bấm Huỷ khi việc CÒN ĐANG XẾP HÀNG: thân hàm chưa chạy nên phần
        #  dọn dẹp trong ``finally`` của nó cũng chưa chạy — phải tự dọn ở
        #  đây, không thì khoá dự án không ai nhả và giao diện treo mãi.
        logger.warning(f"[HÀNG ĐỢI] Đã huỷ khi còn đang xếp hàng: {project_id}")
        review_service.mark_render_failed(
            project_id, L("Đã hủy theo yêu cầu người dùng.", "Cancelled at the user's request.")
        )
        _release_review(project_id)
@router.post("/projects/{project_id}/render")
async def render_project(project_id: str, background_tasks: BackgroundTasks,
                         payload: dict = Body(default=None)):
    #  Than yeu cau la TUY CHON: cac lan goi cu (va nut "Dung phim" thong thuong) khong
    #  gui gi ca. Chi "Xuat doan da chon" moi kem ``ranges``.
    p = _load(project_id)
    _require_advanced(p)
    ranges = (payload or {}).get("ranges") or None

    _acquire_review_or_409(project_id)
    try:
        review_service.clear_render_cancel(project_id)
        review_service.mark_render_started(project_id)
        background_tasks.add_task(_render_wrapper, project_id, ranges)
    except Exception:
        _release_review(project_id)
        raise
    return {"status": "render_started"}
