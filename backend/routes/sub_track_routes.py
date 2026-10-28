"""ENDPOINT PHỤ ĐỀ THÊM THỨ TIẾNG — dùng chung cho chế độ Chi tiết và Cơ bản.

Một bộ route, hai chỗ gắn (xem cuối tệp):
  · Chi tiết: ``/api/review-advanced/projects/{id}/sub-tracks…`` — câu = câu B (``uid``);
  · Cơ bản:   ``/videos/{id}/sub-tracks…`` — câu = dòng ``SubtitleSegment`` (``s<id>``).
Khác nhau ĐÚNG ở hàm ``nap`` (đọc dự án ra dạng chung); mọi thứ còn lại — lưu, dịch,
prompt, job — là ``services/review_sub_tracks.py``.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from services import review_sub_tracks as ST
from services.config_service import get_config_service
from utils.i18n import L

logger = logging.getLogger("ReviewAdvanced")


class SubTrackLangRequest(BaseModel):
    lang: str


class SubTrackLinesRequest(BaseModel):
    #: ``{uid câu: câu dịch}`` — câu rỗng = xoá bản dịch của câu đó.
    lines: dict


class SubTrackTranslateRequest(BaseModel):
    #: "deepseek" | "google"
    engine: str = "deepseek"
    #: "missing" (chưa có + cần dịch lại) | "all"
    scope: str = "missing"
    model: str = ""


def tao_router(*, prefix: str, duong: str, nap, dependencies=(), mo_log=None, dong_log=None,
               tag: str = "sub-tracks", sau_them=None) -> APIRouter:
    """``nap(project_id) -> {"dir", "main_lang", "b_chunks", "a_lookup"}`` (404 nếu không có).
    ``mo_log`` / ``dong_log``: ghi nhật ký job dịch vào log của dự án (tab Nhật ký).
    ``sau_them(p, lang)``: gọi SAU khi một thứ tiếng được thêm (nút Thêm hoặc lúc bắt đầu dịch) — Sáng tạo dùng để điền sẵn bản dịch AI đã viết cho thứ tiếng mẹ đẻ."""
    r = APIRouter(prefix=prefix, tags=[tag], dependencies=list(dependencies))

    def _lang(p, lang: str) -> str:
        if not ST.ma_hop_le(lang, p["main_lang"]):
            raise HTTPException(status_code=400,
                                detail=L("Thứ tiếng không hợp lệ (hoặc trùng thứ tiếng chính).", "Invalid language (or the same as the main language)."))
        return ST.chuan_ma(lang)

    @r.get(duong)
    async def get_sub_tracks(project_id: str):
        p = await run_in_threadpool(nap, project_id)
        d = await run_in_threadpool(ST.doc, p["dir"])
        return {"main_lang": ST.chuan_ma(p["main_lang"]),
                "tracks": ST.tinh_trang(d, p["b_chunks"]),
                "job": ST.trang_thai_job(project_id)}

    @r.post(duong)
    async def add_sub_track(project_id: str, data: SubTrackLangRequest):
        p = await run_in_threadpool(nap, project_id)
        lang = _lang(p, data.lang)
        await run_in_threadpool(ST.them_thu_tieng, p["dir"], lang)
        if sau_them:
            await run_in_threadpool(sau_them, p, lang)
        return {"ok": True, "lang": lang}

    @r.post(duong + "/cancel")
    async def cancel_sub_track_translate(project_id: str):
        ST.huy_job(project_id)
        return {"ok": True}

    @r.delete(duong + "/{lang}")
    async def delete_sub_track(project_id: str, lang: str):
        p = await run_in_threadpool(nap, project_id)
        j = ST.trang_thai_job(project_id)
        if j and j.get("running") and j.get("lang") == ST.chuan_ma(lang):
            raise HTTPException(status_code=409, detail=L("Đang dịch thứ tiếng này — hãy Hủy trước.", "This language is being translated — cancel it first."))
        return {"ok": await run_in_threadpool(ST.xoa_thu_tieng, p["dir"], lang)}

    @r.get(duong + "/{lang}")
    async def get_sub_track_lines(project_id: str, lang: str):
        p = await run_in_threadpool(nap, project_id)
        lang = _lang(p, lang)
        d = await run_in_threadpool(ST.doc, p["dir"])
        if lang not in (d.get("tracks") or {}):
            raise HTTPException(status_code=404, detail=L("Chưa thêm thứ tiếng này.", "This language has not been added yet."))
        return {"lang": lang, "lines": ST.danh_sach_cau(d, lang, p["b_chunks"], p["a_lookup"])}

    @r.patch(duong + "/{lang}/lines")
    async def patch_sub_track_lines(project_id: str, lang: str, data: SubTrackLinesRequest):
        p = await run_in_threadpool(nap, project_id)
        lang = _lang(p, lang)
        #  Job đang dịch CÙNG thứ tiếng sẽ ghi kết quả lô SAU lượt lưu tay này -> câu vừa sửa
        #  bị đè mất. Chặn lại, báo rõ.
        j = ST.trang_thai_job(project_id) or {}
        if j.get("running") and ST.chuan_ma(j.get("lang") or "") == ST.chuan_ma(lang):
            raise HTTPException(
                status_code=409,
                detail=L("Đang dịch thứ tiếng này — đợi dịch xong (hoặc bấm dừng) rồi lưu, "
                         "không thì câu vừa sửa bị bản dịch mới ghi đè.",
                         "This language is being translated — wait for it to finish (or stop it) and then save, "
                         "otherwise the sentence you just edited will be overwritten by the new translation."),
            )
        n = await run_in_threadpool(ST.ghi_cau, p["dir"], lang, data.lines, p["b_chunks"])
        return {"ok": True, "updated": n}

    @r.get(duong + "/{lang}/prompt")
    async def get_sub_track_prompt(project_id: str, lang: str, scope: str = "missing",
                                   limit: int = 0):
        """Prompt dịch để tự dán vào ChatGPT / Gemini (không cần API key) — TOÀN BỘ câu cần
        dịch (``limit`` > 0 thì cắt bớt); dán kết quả qua PATCH ``/lines``."""
        from services import review_advanced_ai as AI

        p = await run_in_threadpool(nap, project_id)
        lang = _lang(p, lang)
        d = await run_in_threadpool(ST.doc, p["dir"])
        ctx = await run_in_threadpool(AI.read_film_context, p["dir"]) or {}
        return ST.tao_prompt(
            d, lang, {"target_language": p["main_lang"]}, p["b_chunks"], p["a_lookup"],
            brief=ctx.get("text") or "", pham_vi="all" if scope == "all" else "missing",
            toi_da=limit,
        )

    def _chay_dich(project_id: str, lang: str, engine: str, scope: str, model: str) -> None:
        from services import review_advanced_ai as AI

        #  Mở log hỏng (tệp bị khoá…) KHÔNG được làm job kẹt "đang chạy" mãi — mọi lượt
        #  dịch sau trả 409 và không xoá được thứ tiếng cho tới khi mở lại app.
        try:
            handler = mo_log(project_id) if mo_log else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Không mở được nhật ký dự án khi dịch phụ đề: {e}")
            handler = None
        try:
            p = nap(project_id)
            ctx = AI.read_film_context(p["dir"]) or {}
            opts = get_config_service().get_ai_script_options()
            logger.info(f"=== DỊCH PHỤ ĐỀ SANG '{lang}' ({engine}, {scope}) ===")
            kq = ST.dich(
                project_id, p["dir"], {"target_language": p["main_lang"]}, lang,
                p["b_chunks"], p["a_lookup"], engine=engine, pham_vi=scope, model=model,
                batch_size=int(opts.get("batch_size") or 80), brief=ctx.get("text") or "",
            )
            logger.info(f"=== DỊCH PHỤ ĐỀ XONG: {kq} ===")
            loi = ""
            if kq.get("thieu") and not kq.get("huy"):
                loi = L(f"Còn {kq['thieu']} câu chưa dịch được — bấm Dịch lại để thử tiếp.", f"{kq['thieu']} sentences could not be translated — click Translate again to retry.")
                if engine == "google" and not kq.get("dich"):
                    #  Không ra câu nào qua Google = gần như chắc bị giới hạn tần suất (429).
                    loi = L(
                        f"Google Dịch không trả câu nào ({kq['thieu']} câu) — thường do Google "
                        "tạm chặn vì gửi quá nhiều (lỗi 429). Chờ vài phút rồi Dịch lại, hoặc "
                        "dùng DeepSeek / Copy prompt.",
                        f"Google Translate returned no sentences ({kq['thieu']} sentences) — usually because Google "
                        "temporarily blocked you for sending too many requests (error 429). Wait a few minutes and Translate again, or "
                        "use DeepSeek / Copy prompt.",
                    )
            ST._cap_nhat_job(project_id, running=False, error=loi, result=kq)
        except Exception as e:  # noqa: BLE001
            friendly = getattr(e, "user_message", None) or str(e)
            logger.error(f"Dịch phụ đề thất bại: {friendly}")
            ST._cap_nhat_job(project_id, running=False, error=friendly[:500])
        finally:
            if dong_log and handler is not None:
                dong_log(project_id, handler)

    @r.post(duong + "/{lang}/translate")
    async def translate_sub_track(project_id: str, lang: str, background_tasks: BackgroundTasks,
                                  data: SubTrackTranslateRequest = Body(default=SubTrackTranslateRequest())):
        p = await run_in_threadpool(nap, project_id)
        lang = _lang(p, lang)
        from services.ai_engines import la_ai, ma_model, provider_of, tach_model, ten_bo_ai

        #  "google" | "deepseek" | "gemini" (giá trị lạ -> bộ AI mặc định ở Cài đặt); model gửi đi là ``provider:model``.
        engine = "google" if data.engine == "google" else (provider_of(data.engine) or get_config_service().ai_provider_mac_dinh())
        scope = "all" if data.scope == "all" else "missing"
        model = (data.model or "").strip()
        if la_ai(engine):
            import services.deepseek_service as deepseek_service

            if not deepseek_service.is_configured(engine):
                raise HTTPException(status_code=400, detail=L(f"Chưa cấu hình API key {ten_bo_ai(engine)}. Vào Cài đặt để nhập API key, hoặc chọn Google Dịch.", f"The {ten_bo_ai(engine)} API key is not configured. Go to Settings to enter the API key, or choose Google Translate."))
            model = ma_model(engine, tach_model(model)[1]) if tach_model(model)[1] else get_config_service().ai_model_mac_dinh_cua(engine)
        if sau_them and lang not in ((await run_in_threadpool(ST.doc, p["dir"])).get("tracks") or {}):
            await run_in_threadpool(ST.them_thu_tieng, p["dir"], lang)           # dịch thẳng thứ tiếng chưa thêm: thêm + điền sẵn TRƯỚC khi đếm câu cần dịch
            await run_in_threadpool(sau_them, p, lang)
        d = await run_in_threadpool(ST.doc, p["dir"])
        viec = ST.can_dich(d, lang, p["b_chunks"], scope)
        if not viec:
            return {"status": "nothing"}
        await run_in_threadpool(ST.them_thu_tieng, p["dir"], lang)
        if not ST.bat_dau_job(project_id, lang, len(viec)):
            raise HTTPException(status_code=409, detail=L("Đang dịch phụ đề — đợi lượt trước xong hoặc bấm Hủy.", "Subtitles are being translated — wait for the previous run to finish or click Cancel."))
        background_tasks.add_task(_chay_dich, project_id, lang, engine, scope, model)
        return {"status": "started", "total": len(viec)}

    return r


# --------------------------------------------------------------------------- Chi tiết
def _nap_chi_tiet(project_id: str) -> dict:
    from services import review_advanced_engine as ADV
    from services import review_timeline as TL

    try:
        p = ADV.load_project(project_id, migrate=False)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án", "Project not found"))
    if not TL.is_advanced(p["meta"]):
        raise HTTPException(status_code=400, detail=L("Dự án này không ở chế độ Review nâng cao.", "This project is not in Advanced Review mode."))
    return {"dir": p["dir"], "main_lang": p["meta"].get("target_language") or "vi",
            "b_chunks": p["b_chunks"], "a_lookup": p["a_lookup"]}


def _mo_log_chi_tiet(project_id: str):
    from routes.review_routes import setup_project_logging

    return setup_project_logging(project_id, get_config_service().get_review_root())


def _dong_log_chi_tiet(project_id: str, handler) -> None:
    from routes.review_routes import remove_project_logging

    remove_project_logging(project_id, handler)


# --------------------------------------------------------------------------- Cơ bản
def cau_co_ban(db_segments) -> tuple:
    """Dòng ``SubtitleSegment`` -> (câu dạng câu B, a_lookup) cho ``review_sub_tracks``.

    ``uid`` = ``s<id>`` (id DB bền qua sửa chữ); câu chính = ``target_text`` (bản dịch),
    video chưa dịch thì là ``source_text``; câu gốc tham khảo = ``source_text``."""
    b, a = [], {}
    for i, s in enumerate(db_segments, 1):
        chinh = (s.target_text or "").strip() or (s.source_text or "").strip()
        b.append({"uid": f"s{s.id}", "b_id": i, "b_text": chinh,
                  "source_a_ids": [s.id] if (s.target_text or "").strip() else []})
        a[str(s.id)] = {"text": s.source_text or ""}
    return b, a


def thu_tieng_chinh_co_ban(video, segs) -> str:
    """Thứ tiếng của CÂU CHÍNH ở chế độ Cơ bản. Đã dịch (có ``target_text``) = thứ tiếng
    đích; CHƯA dịch thì câu chính là câu gốc (``cau_co_ban``) -> thứ tiếng NGUỒN — không
    thì prompt ghi "dịch từ Tiếng Việt" cho câu tiếng Trung, .srt dán nhãn sai (rà trong
    app thật 29-09-2026)."""
    if any((s.target_text or "").strip() for s in segs):
        return video.target_lang or "vi"
    return video.source_lang or video.target_lang or "vi"


def _nap_co_ban(project_id: str) -> dict:
    from models.database import SessionLocal
    from models.models import SubtitleSegment, Video
    from routes.video_routes import get_project_dirs

    db = SessionLocal()
    try:
        v = db.query(Video).filter(Video.id == project_id).first()
        if not v:
            raise HTTPException(status_code=404, detail=L("Không tìm thấy video", "Video not found"))
        segs = (db.query(SubtitleSegment).filter(SubtitleSegment.video_id == project_id)
                .order_by(SubtitleSegment.index).all())
        b, a = cau_co_ban(segs)
        return {"dir": get_project_dirs(project_id, db)["root"],
                "main_lang": thu_tieng_chinh_co_ban(v, segs), "b_chunks": b, "a_lookup": a}
    finally:
        db.close()


# --------------------------------------------------------------------------- Sáng tạo
def _nap_sang_tao(project_id: str) -> dict:
    """Dự án Sáng tạo: câu chính = các câu TIẾNG ĐÍCH của phụ đề CC (``phu_de.json`` ghi cùng bản dựng); chưa dựng video thì chưa có câu nào."""
    from services import sangtao_du_an as SDA

    try:
        return SDA.nap_phu_de(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án", "Project not found"))


def _sau_them_sang_tao(p: dict, lang: str) -> None:
    """Thêm thứ tiếng mẹ đẻ của dự án → điền sẵn các câu AI đã dịch lúc viết kịch bản (xem ``sangtao_du_an.gieo_ban_dich_me``)."""
    from services import sangtao_du_an as SDA

    SDA.gieo_ban_dich_me(p["id"], lang)


def _mo_log_co_ban(project_id: str):
    from models.database import SessionLocal
    from routes.video_routes import setup_project_logging

    db = SessionLocal()
    try:
        return setup_project_logging(project_id, db)
    except Exception:  # noqa: BLE001 — không ghi được log dự án thì thôi, vẫn dịch
        return None
    finally:
        db.close()


def _dong_log_co_ban(project_id: str, handler) -> None:
    from routes.video_routes import remove_project_logging

    try:
        remove_project_logging(handler)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- YouTube
def srt_cho_du_an(du_an_nguon: str) -> dict:
    """``{thứ tiếng: nội dung .srt}`` để tải lên YouTube: thứ tiếng CHÍNH + mọi thứ tiếng
    thêm đã dịch ĐỦ (còn câu thiếu thì bỏ thứ tiếng đó — phụ đề khuyết trên YouTube là sai
    trước mặt người xem). ``du_an_nguon`` = "chi_tiet:<id>" | "long_tieng:<id>" | "sang_tao:<id>"."""
    nguon, _, pid = (du_an_nguon or "").partition(":")
    if not pid:
        return {}
    if nguon == "sang_tao":
        #  Dự án Sáng tạo: phụ đề CC ghi cùng lúc với video (``phu_de.json``): tiếng đích + tiếng mẹ đẻ (câu có bản dịch). Chưa có = video dựng trước khi có tính năng này.
        from services import sangtao_du_an as SDA

        try:
            return SDA.phu_de_srt(pid)
        except KeyError:
            return {}
    if nguon == "chi_tiet":
        from routes.review_routes import _review_compositor_segments
        from services import review_advanced_engine as ADV

        try:
            prj = ADV.load_project(pid, migrate=False)
        except FileNotFoundError:
            return {}
        segs = [
            {**x, "start_time": max(0.0, x["start_time"]), "end_time": max(0.0, x["end_time"])}
            for x in _review_compositor_segments(prj["dir"], prj["meta"], prj["b_chunks"])
        ]
        p = {"dir": prj["dir"], "main_lang": prj["meta"].get("target_language") or "vi",
             "b_chunks": prj["b_chunks"]}
    elif nguon == "long_tieng":
        from models.database import SessionLocal
        from models.models import SubtitleSegment

        try:
            p = _nap_co_ban(pid)
        except HTTPException:
            return {}
        db = SessionLocal()
        try:
            segs = [
                {"start_time": max(0.0, s.start_time), "end_time": max(0.0, s.end_time),
                 "source_text": s.source_text,
                 "target_text": (s.target_text or "").strip() or s.source_text,
                 "uid": f"s{s.id}"}
                for s in db.query(SubtitleSegment).filter(SubtitleSegment.video_id == pid)
                .order_by(SubtitleSegment.index).all()
            ]
        finally:
            db.close()
    else:
        return {}
    d = ST.doc(p["dir"])
    main = ST.chuan_ma(p["main_lang"])
    du = [t["lang"] for t in ST.tinh_trang(d, p["b_chunks"]) if not t["missing"]]
    out = {}
    for lg, sg in ST.theo_tieng(ST.chon_xuat({"srt_langs": [main, *du]}, main), segs, d).items():
        chu, n = ST.srt_chu(sg)
        if n:
            out[lg] = chu
    return out


def routers() -> list:
    from routes.auth_routes import verify_license
    from routes.review_advanced_routes import _chan_sua_khi_lo_giu

    return [
        tao_router(prefix="/api/review-advanced", duong="/projects/{project_id}/sub-tracks",
                   nap=_nap_chi_tiet,
                   dependencies=[Depends(verify_license), Depends(_chan_sua_khi_lo_giu)],
                   mo_log=_mo_log_chi_tiet, dong_log=_dong_log_chi_tiet),
        tao_router(prefix="/videos", duong="/{project_id}/sub-tracks", nap=_nap_co_ban,
                   dependencies=[Depends(verify_license)],
                   mo_log=_mo_log_co_ban, dong_log=_dong_log_co_ban),
        #  Sáng tạo: thêm thứ tiếng cho PHỤ ĐỀ CC (người xem nói en / zh / ko… khi video học tiếng Nhật…) — không có nhật ký dự án.
        tao_router(prefix="/api/sangtao", duong="/projects/{project_id}/sub-tracks", nap=_nap_sang_tao,
                   dependencies=[Depends(verify_license)], sau_them=_sau_them_sang_tao),
    ]

