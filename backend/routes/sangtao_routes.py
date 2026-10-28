"""API chế độ "Sáng tạo" — ĐỌC BÁO học ngoại ngữ.

  POST /api/sangtao/article            link bài báo -> tiêu đề + văn bản chính (app tự tải, không để AI đoán từ link)
  POST /api/sangtao/prompt             nội dung bài -> prompt chia cảnh (1 hoặc nhiều phần nếu bài dài)
  POST /api/sangtao/storyboard/import  dán JSON của AI -> storyboard hợp lệ
  /projects…                           dự án, dựng nền, tiến độ, xem video / từng cảnh
Router gắn rào bản quyền như các router AI khác.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from routes.auth_routes import quyen_hien_tai, verify_license
from services import sangtao_bai_bao as BB
from services import sangtao_du_an as DA
from services import sangtao_phan_tich as PT
from services import sangtao_prompt as PR

logger = logging.getLogger("Sangtao")


def _chan_neu_khong_co_quyen_sang_tao() -> None:
    """CHẶN cả cụm ``/api/sangtao`` nếu key chưa được admin cấp quyền dùng chế độ Sáng tạo (ẨN MẶC ĐỊNH, cấp theo từng key ở trang quản lý key).

    Chặn ở BACKEND chứ không chỉ ẩn nút (cùng lý do với ``_chan_neu_khong_co_quyen`` của Chạy hàng loạt): ẩn ở giao diện chỉ ngăn người dùng thường, ai gọi thẳng endpoint là chạy được.
    ``quyen_hien_tai`` đọc từ cache xác thực nên không thêm lượt gọi mạng; chạy từ mã nguồn (dev) thì luôn được."""
    if quyen_hien_tai().get("allow_sang_tao") is True:
        return
    raise HTTPException(status_code=403, detail="Chế độ Sáng tạo chưa được bật cho API Key này. Liên hệ quản trị viên để được cấp quyền.")


router = APIRouter(prefix="/api/sangtao", tags=["sangtao"], dependencies=[Depends(verify_license), Depends(_chan_neu_khong_co_quyen_sang_tao)])


class BaiBody(BaseModel):
    url: str


class HtmlBody(BaseModel):
    url: str
    html: str = ""                     # HTML SAU KHI JavaScript chạy (do cửa sổ trình duyệt của app trả về)
    main_text: str = ""                # chữ đang hiển thị của vùng chính


class PromptBody(BaseModel):
    script: str                        # nội dung bài (văn bản đã lấy từ link hoặc dán tay)
    language: str = "en"               # ngôn ngữ đang học (đọc to)
    aspect: str = "16:9"
    audience: str = "general"
    native_language: str = "vi"        # ngôn ngữ dịch
    level: str = ""                    # trình độ (CEFR / JLPT / HSK / TOPIK) — dùng khi viết lại
    content_mode: str = "viet_lai"     # viet_lai (mặc định: đủ ý, câu mới) | chon_loc | giu_nguyen
    passes: list[str] = ["doc"]        # các lượt phát; có "giang" → nhờ AI chọn 5–10 từ + 1–3 mẫu ngữ pháp, kèm lời dẫn / lời giảng
    narration: bool = True             # nhờ AI viết lời chào / chuyển lượt / kết (nói bằng tiếng đích)
    source_url: str = ""               # link bài gốc (chỉ để ghi nguồn)
    source_site: str = ""              # tên báo (lời dẫn mở đầu nêu nguồn)
    source_date: str = ""              # ngày đăng bài gốc (nếu có) — mốc thời gian AI được dùng trong tiêu đề YouTube


class ImportBody(BaseModel):
    text: str
    language: str = "en"
    aspect: str = "16:9"
    audience: str = "general"
    voice: Optional[str] = None


@router.post("/article")
def lay_bai(b: BaiBody):
    """Tải bài báo từ link và tách văn bản chính. Hàm đồng bộ → FastAPI chạy trong luồng riêng (không chặn các việc khác)."""
    try:
        return BB.lay_bai(b.url)
    except BB.LoiBai as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/article/html")
def tach_bai_tu_trinh_duyet(b: HtmlBody):
    """Tách bài từ trang đã dựng xong trong cửa sổ trình duyệt của app (không tải mạng): dùng khi tải thẳng chỉ ra phần dẫn / trang dựng bằng JavaScript."""
    if len(b.html) > BB.TOI_DA_HTML or len(b.main_text) > BB.TOI_DA_CHU:
        raise HTTPException(status_code=413, detail="Trang quá lớn")
    if not (b.html.strip() or b.main_text.strip()):
        raise HTTPException(status_code=400, detail="Trang trống")
    try:
        return BB.tach_tu_trinh_duyet(b.url, b.html, b.main_text)
    except BB.LoiBai as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/prompt")
async def tao_prompt(b: PromptBody):
    if BB.la_link(b.script):
        raise HTTPException(status_code=400, detail="Đây là đường dẫn — hãy bấm \"Lấy nội dung bài\" trước (hoặc dán chữ của bài vào ô)")
    try:
        ra = PR.cac_phan(**_tuy_chon_prompt(b))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ra:
        raise HTTPException(status_code=400, detail="Nội dung bài trống")
    return {"so_phan": len(ra), "phan": ra}


def _tuy_chon_prompt(b: PromptBody) -> dict:
    """Tham số dựng prompt từ body — dùng chung cho ``/prompt`` và việc nền "Nhờ AI viết kịch bản" (cùng một prompt)."""
    return {"script": b.script, "language": b.language, "aspect": b.aspect, "audience": b.audience, "native_language": b.native_language,
            "level": b.level, "content_mode": b.content_mode, "passes": b.passes, "narration": b.narration, "source_url": b.source_url,
            "source_site": b.source_site, "source_date": b.source_date}


@router.post("/storyboard/import")
async def nhap_storyboard(b: ImportBody):
    kq = PT.phan_tich(b.text, mac_dinh={"language": b.language, "aspect": b.aspect, "audience": b.audience, "voice": b.voice})
    if not kq.ok:
        return {"ok": False, "loi": kq.loi, "canh_bao": kq.canh_bao, "ghi_chu": kq.ghi_chu}
    return {"ok": True, "canh_bao": kq.canh_bao, "ghi_chu": kq.ghi_chu, "storyboard": kq.storyboard.model_dump()}


# ------------------------------------------------------------------ nhờ AI viết kịch bản (thay cho dán prompt)
class AiKichBanBody(PromptBody):
    #: mã model ``provider:model`` (DeepSeek tên trơn); rỗng = model mặc định của bộ AI đang chọn trong Cài đặt
    model: str = ""
    #: chữ ký đầu vào (bài + cài đặt) do giao diện đặt, trả lại nguyên trong tiến độ — giao diện chỉ TỰ NHẬN kết quả khi chữ ký còn khớp bài đang mở
    ky: str = ""


@router.post("/projects/{pid}/ai-kich-ban")
async def ai_viet_kich_ban(pid: str, b: AiKichBanBody):
    """Việc nền: gọi bộ AI với ĐÚNG prompt của nút "Tạo prompt", từng phần tuần tự trong cùng hội thoại; JSON trả về ghi ``ai_kich_ban.json``
    để giao diện đưa vào đường "Dán & nhận cảnh" hiện có (AI chỉ thay bàn tay dán)."""
    from services import sangtao_ai_kich_ban as AK

    try:
        AK.bat_dau(pid, _tuy_chon_prompt(b), b.model, ky=b.ky)
    except KeyError as e:
        raise _khong_thay(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return AK.trang_thai(pid)


@router.get("/projects/{pid}/ai-kich-ban")
async def ai_viet_kich_ban_tien_do(pid: str):
    from services import sangtao_ai_kich_ban as AK

    try:
        return AK.trang_thai(pid)
    except KeyError as e:
        raise _khong_thay(e)


@router.post("/projects/{pid}/ai-kich-ban/cancel")
async def ai_viet_kich_ban_huy(pid: str):
    from services import sangtao_ai_kich_ban as AK

    return {"da_huy": AK.huy(pid)}


# ------------------------------------------------------------------ dự án + dựng
def _khong_thay(e: KeyError):
    return HTTPException(status_code=404, detail=str(e.args[0]) if e.args else "Không tìm thấy")


class TaoBody(BaseModel):
    title: str = ""


class LuuBody(BaseModel):
    title: Optional[str] = None
    script: Optional[str] = None
    settings: Optional[dict] = None
    storyboard: Optional[dict] = None
    xoa_storyboard: bool = False


class DungBody(BaseModel):
    chi_cac_canh: Optional[list[int]] = None
    lam_lai: bool = False                      # chỉ có nghĩa khi có chi_cac_canh: làm lại cảnh từ đầu (giọng sinh mới + dựng lại hình), không dùng bộ nhớ đệm


@router.get("/projects")
async def ds_du_an():
    return {"du_an": DA.liet_ke()}


@router.post("/projects")
async def tao_du_an(b: TaoBody):
    return DA.tao(b.title)


@router.get("/projects/{pid}")
async def lay_du_an(pid: str):
    try:
        return DA.doc(pid)
    except KeyError as e:
        raise _khong_thay(e)


@router.put("/projects/{pid}")
async def luu_du_an(pid: str, b: LuuBody):
    tc = b.model_dump(exclude_none=True)
    tc.pop("xoa_storyboard", None)
    if b.xoa_storyboard:
        tc["storyboard"] = None
    try:
        du_an, loi = DA.luu(pid, tc)
    except KeyError as e:
        raise _khong_thay(e)
    return {"du_an": du_an, "loi": loi}


@router.delete("/projects")
async def xoa_tat_ca_du_an():
    """"Xoá tất cả": mọi dự án trừ dự án đang dựng. Không khôi phục được."""
    return {"ok": True, **DA.xoa_tat_ca()}


class DoKhopBody(BaseModel):
    #: Câu đang hiện trên màn hình (``[{"text", "quote"?}]``) và chữ bài gốc — để số liệu phản ánh cả chỗ người dùng vừa sửa mà CHƯA lưu. Bỏ trống thì dùng bản đã lưu của dự án.
    dong: Optional[list[dict]] = None
    script: Optional[str] = None


@router.post("/projects/{pid}/do-khop")
async def do_khop_du_an(pid: str, b: Optional[DoKhopBody] = None):
    """Đối chiếu THẬT các câu của kịch bản với chữ của bài gốc (``services/sangtao_do_khop``): bao nhiêu câu đúng nguyên văn / không có trong bài / lặp, thứ tự, phần bài được dùng, câu của bài bị bỏ sót.
    Giao diện dùng nó thay cho con số tự khai từ cờ ``quote`` của AI."""
    from services import sangtao_do_khop as DK

    try:
        d = DA.doc(pid)
    except KeyError as e:
        raise _khong_thay(e)
    dong = DK.dong_cua_du_an(d) if (b is None or b.dong is None) else [{"text": str(x.get("text") or ""), "quote": bool(x.get("quote"))} for x in b.dong if isinstance(x, dict)][:2000]
    script = d.get("script") or "" if (b is None or b.script is None) else b.script[:200000]
    kq = DK.do_khop(script, dong)
    kq["che_do"] = (d.get("settings") or {}).get("content_mode") or "viet_lai"
    return kq


@router.post("/projects/{pid}/mac-dinh")
async def luu_mac_dinh_du_an(pid: str):
    """"Đặt cấu hình dự án này làm mặc định cho dự án mới" (services/mac_dinh_du_an.py, chế độ ``sang_tao``). Bỏ mặc định: DELETE /api/videos/settings/mac-dinh/sang_tao."""
    try:
        tt = DA.luu_mac_dinh(pid)
    except KeyError as e:
        raise _khong_thay(e)
    return {"ok": True, "trang_thai": tt}


@router.delete("/projects/{pid}")
async def xoa_du_an(pid: str):
    try:
        DA.xoa(pid)
    except KeyError as e:
        raise _khong_thay(e)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@router.post("/projects/{pid}/render")
async def dung(pid: str, b: DungBody):
    try:
        DA.bat_dau_dung(pid, b.chi_cac_canh, b.lam_lai)
    except KeyError as e:
        raise _khong_thay(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return DA.trang_thai(pid)


@router.get("/projects/{pid}/render")
async def tien_do(pid: str):
    try:
        return DA.trang_thai(pid)
    except KeyError as e:
        raise _khong_thay(e)


@router.post("/projects/{pid}/render/cancel")
async def huy(pid: str):
    return {"da_huy": DA.huy_dung(pid)}


@router.post("/projects/{pid}/open-folder")
async def mo_thu_muc(pid: str):
    """Mở thư mục chứa video đã dựng (dùng khi chạy ngoài Electron; trong app desktop giao diện gọi thẳng showItemInFolder)."""
    try:
        return {"path": DA.mo_thu_muc(pid)}
    except KeyError as e:
        raise _khong_thay(e)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Không mở được thư mục: {e}")


@router.get("/projects/{pid}/video")
async def xem_video(pid: str, request: Request):
    try:
        p = os.path.join(DA.thu_muc(pid), "out.mp4")
    except KeyError as e:
        raise _khong_thay(e)
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="Chưa có video")
    from routes.review_advanced_routes import _range_response
    return _range_response(p, request, "video/mp4")


@router.get("/projects/{pid}/description")
async def mo_ta(pid: str):
    """Mô tả video gợi ý (nguồn văn bản + link gốc + ghi chú) để dán vào phần mô tả khi đăng."""
    try:
        return {"text": DA.mo_ta_video(DA.doc(pid))}
    except KeyError as e:
        raise _khong_thay(e)


@router.get("/projects/{pid}/subtitles")
async def phu_de_cc(pid: str):
    """Phụ đề CC của bản dựng hiện có: [{lang, ten, so_cau}] — tiếng đích (đủ mọi câu nói) + tiếng mẹ đẻ (câu có bản dịch). Rỗng nếu chưa dựng / dựng trước khi có tính năng này."""
    try:
        return {"phu_de": DA.phu_de_tom_tat(pid)}
    except KeyError as e:
        raise _khong_thay(e)


@router.get("/projects/{pid}/subtitles/{lang}")
async def phu_de_cc_srt(pid: str, lang: str):
    """Nội dung .srt của một thứ tiếng (để chép / lưu cạnh video)."""
    try:
        ds = DA.phu_de_srt(pid)
    except KeyError as e:
        raise _khong_thay(e)
    if lang not in ds:
        raise HTTPException(status_code=404, detail="Chưa có phụ đề thứ tiếng này — dựng lại video để tạo")
    return {"lang": lang, "srt": ds[lang]}


@router.get("/voices")
async def giong(language: str = "vi"):
    """Giọng đọc có thể chọn cho một ngôn ngữ (Edge; cần Internet). Trả cả giọng mặc định sẽ dùng khi dự án chưa chọn."""
    from routes.tts_routes import EDGE_VOICES_BY_LANG
    return {"giong": EDGE_VOICES_BY_LANG.get(language, []), "mac_dinh": DA.giong_mac_dinh(language)}


@router.get("/projects/{pid}/scene/{so}/thumb")
async def anh_canh(pid: str, so: int):
    try:
        p = DA.tep_canh(pid, so, ".jpg")
    except KeyError as e:
        raise _khong_thay(e)
    if not p:
        raise HTTPException(status_code=404, detail="Cảnh chưa dựng")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=31536000, immutable"})   # tên tệp mang khoá nên bất biến


@router.get("/projects/{pid}/scene/{so}/clip")
async def clip_canh(pid: str, so: int, request: Request):
    try:
        p = DA.tep_canh(pid, so, ".mp4")
    except KeyError as e:
        raise _khong_thay(e)
    if not p:
        raise HTTPException(status_code=404, detail="Cảnh chưa dựng")
    from routes.review_advanced_routes import _range_response
    return _range_response(p, request, "video/mp4")
