"""CHẠY HÀNG LOẠT — xếp hàng các dự án Chi tiết rồi chạy từng bước cho cả lô.

KHÔNG viết lại nghiệp vụ nào: mỗi bước gọi ĐÚNG hàm mà nút bấm trong dự án vẫn
gọi (``run_analysis_wrapper``, ``_ai_script_wrapper``, ``ADV.generate_voices``,
``ADV.auto_arrange``, ``_render_wrapper``). Nhờ vậy sửa nghiệp vụ ở một chỗ là cả
hai đường cùng đổi — nhân bản logic ở đây là sớm muộn "chạy lẻ" và "chạy lô" cho
ra hai kết quả khác nhau mà không ai biết bên nào đúng.

Luật đã chốt (2026-09-16):
  · chạy LẦN LƯỢT từng dự án (STT + sinh giọng đều ăn GPU),
  · một dự án lỗi thì bỏ qua, chạy tiếp, cuối lô liệt kê,
  · bước DỒN tự chạy được thì tự chạy; còn câu bị cắt thì để "cần người quyết"
    kèm sẵn các phương án cho modal, KHÔNG tự đổi tốc độ cả dự án.
"""

import asyncio
import logging
import os
import threading
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from services import batch_lock as BL
from services import export_ranges as ER
from services import cat_tu_ban_xuat as CUT
from services import batch_store as BS
from services import review_advanced_engine as ADV
from services import review_advanced_prompts as PROMPTS
from services import review_timeline as TL
from services import tach_cau as TC
from services import work_queue as WQ
from services.review_service import ReviewService
from routes.auth_routes import quyen_hien_tai, verify_license
from routes.review_routes import (
    _acquire_review,
    _chan_neu_may_ban,
    _release_review,
    run_analysis_wrapper,
)
from routes.review_advanced_routes import (
    _ai_script_wrapper,
    _render_wrapper,
    _separate_wrapper,
    enable_advanced,
)
from utils.i18n import L

logger = logging.getLogger(__name__)


def _chan_neu_khong_co_quyen() -> None:
    """CHẶN cả cụm chạy hàng loạt nếu gói của key không cho dùng.

    Vì sao chặn ở BACKEND chứ không chỉ ẩn nút: ẩn ở giao diện chỉ ngăn người
    dùng thường: ai mở công cụ nhà phát triển ra gọi thẳng endpoint là chạy được
    hết. Tính năng có thu tiền thì cửa phải khoá ở chỗ làm việc thật.

    ``quyen_hien_tai`` đọc từ cache xác thực nên không thêm lượt gọi mạng nào.
    """
    q = quyen_hien_tai()
    if q.get("allow_batch", True):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            q.get("quota_message")
            or L("Gói hiện tại của bạn không có chế độ Chạy hàng loạt. "
                 "Liên hệ Fanpage để nâng cấp.",
                 "Your current plan does not include Batch mode. "
                 "Contact the Fanpage to upgrade.")
        ),
    )


router = APIRouter(prefix="/api/batch", tags=["batch"],
                   dependencies=[Depends(verify_license),
                                 Depends(_chan_neu_khong_co_quyen)])
review_service = ReviewService()

#: Lô đang chạy trong tiến trình này: {batch_id: asyncio.Task}. Chỉ để chặn bấm
#: chạy hai lần; trạng thái THẬT nằm dưới đĩa (batch_store) nên tắt app mở lại
#: vẫn thấy lô ở đâu.
_running: dict = {}
#  Bước ĐANG CHẠY THẬT theo lô (batch_id -> step), đặt/gỡ ngay trong ``_chay_lo``.
#  Khác với ``run.step`` trong TỆP lô: tệp có thể trống trong khe hở giữa hai bước của
#  chuỗi hay bị một lượt ghi khác chạm vào, còn sổ này chỉ nói đúng một điều — tác vụ
#  của lô còn sống hay không. Giao diện dựa vào đây để không bao giờ "tưởng rảnh".
_BUOC_DANG_CHAY: dict = {}


#  Dọn lượt chạy dang dở NGAY KHI backend khởi động: tệp lô có thể còn ghi "đang
#  chạy" từ lần app bị đóng đột ngột — không dọn thì lô đó khoá cứng, bấm gì cũng
#  báo "đang chạy" mà chẳng có gì chạy.
try:
    _n_don = BS.reset_stale_runs()
    if _n_don:
        logger.info(f"[LÔ] Đã dọn {_n_don} lô còn kẹt 'đang chạy' từ lần trước.")
except Exception as _e:  # noqa: BLE001 — không được để việc dọn dẹp chặn khởi động
    logger.warning(f"[LÔ] Không dọn được lô kẹt: {_e}")


def _get(batch_id: str) -> dict:
    b = BS.load(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail=L("Không tìm thấy lô", "Batch not found"))
    return b


def _thong_tin_du_an(project_id: str) -> dict:
    """Tên + thời lượng + dung lượng của một dự án, đọc từ metadata và đĩa.

    Gọi lúc THÊM VÀO LÔ (và lúc bấm Làm mới), không gọi theo nhịp hỏi trạng thái:
    lô vài trăm video mà mỗi 2 giây lại đi ``stat`` từng tệp là đĩa quay không kịp.
    """
    ra = {"title": project_id, "giay": 0.0, "mb": 0.0}
    try:
        meta = review_service.get_metadata(project_id) or {}
    except Exception:  # noqa: BLE001 — dự án hỏng thì vẫn hiện được cái tên
        return ra
    ra["title"] = meta.get("title") or meta.get("video_name") or project_id
    try:
        ra["giay"] = float(meta.get("video_duration") or 0)
    except (TypeError, ValueError):
        pass
    duong = (meta.get("video_path") or "").strip()
    try:
        if duong and os.path.exists(duong):
            ra["mb"] = round(os.path.getsize(duong) / (1024 * 1024), 1)
    except OSError:
        pass
    return ra


#: Giây giữa hai lần soát "video gốc còn trên đĩa không" của MỘT dự án. Không soát
#: theo từng nhịp hỏi trạng thái (2 giây): lô vài trăm video là vài trăm lượt đọc
#: tệp mỗi hai giây, ổ mạng thì đứng hình. 20 giây đủ nhanh để người dùng vừa xoá
#: tệp xong quay ra là thấy cảnh báo.
NHIP_SOAT_VIDEO = 20.0
#: project_id -> (mốc soát, mất hay không, đường dẫn video gốc)
_SO_MAT_VIDEO: dict = {}


def _mat_video(project_id: str) -> tuple:
    """Video GỐC của dự án còn trên đĩa không. Trả ``(mất, đường_dẫn)``.

    VÌ SAO Ở ĐÂY: trước đây mất tệp gốc chỉ lộ ra khi mở dự án Chi tiết hoặc khi
    bước chạy tới nơi rồi hỏng. Người dùng bày cả lô 20 video, bấm chạy, đi pha
    cà phê, quay ra mới biết 3 video hỏng từ đầu.

    Đọc THẲNG metadata.json chứ không qua ``review_service.get_metadata``: hàm kia
    còn tính dung lượng thư mục và tự vá trạng thái — quá nặng cho một việc chỉ
    cần biết đường dẫn.
    """
    import json
    import time as _t

    moc, mat, duong = _SO_MAT_VIDEO.get(project_id) or (0.0, False, "")
    if _t.time() - moc < NHIP_SOAT_VIDEO:
        return mat, duong
    try:
        p = os.path.join(review_service.projects_root, project_id, "metadata.json")
        with open(p, "r", encoding="utf-8") as f:
            duong = str((json.load(f) or {}).get("video_path") or "").strip()
        mat = not duong or not os.path.exists(duong)
    except (OSError, ValueError):
        #  Không đọc được metadata (dự án hỏng/đang ghi dở) -> KHÔNG kết luận là
        #  mất video. Báo nhầm còn tệ hơn không báo: người dùng sẽ mất công đi tìm
        #  một tệp vẫn còn nguyên, rồi lần sau không tin cảnh báo nữa.
        mat = False
    _SO_MAT_VIDEO[project_id] = (_t.time(), mat, duong)
    return mat, duong


def _suy_trang_thai(project_id: str) -> dict:
    """ĐỌC dự án xem nó ĐÃ LÀM TỚI ĐÂU rồi — trả {bước: "done"} cho phần đã xong.

    VÌ SAO CẦN: người dùng thêm vào lô một dự án họ đã tự lấy phụ đề, dịch và sinh
    giọng từ trước. Bảng để nguyên "Chưa chạy" thì bấm "Lấy phụ đề" cho cả lô là
    CHẠY LẠI Whisper trên dự án đã có sẵn phụ đề — mất hàng chục phút và ghi đè
    bản dịch đang có. Ở đây chỉ đọc tệp trên đĩa, không sửa gì.

    Chỉ suy những bước có DẤU VẾT chắc chắn. Bước "Dồn" cố tình KHÔNG suy: dấu vết
    của nó (``start_override``) cũng do kéo tay mà có, đoán bừa thành ra bỏ qua
    việc cần làm.
    """
    import os

    from services import review_timeline as TL

    out: dict = {}
    try:
        return _suy_trang_thai_raw(project_id)
    except Exception as e:  # noqa: BLE001
        #  PHẢI nuốt MỌI lỗi ở đây. Hàm này chạy NGAY TRONG vòng lặp của lô; để lọt
        #  một ngoại lệ (dự án hỏng, thiếu khoá trong metadata, tệp bị xoá giữa
        #  chừng) là chết cả lượt chạy và 19 dự án còn lại không ai đụng tới —
        #  đúng lỗi test bắt được. Đoán không ra thì coi như "chưa biết gì".
        logger.warning(f"[LÔ] Không đọc được hiện trạng {project_id}: {e}")
        return out


def _suy_trang_thai_raw(project_id: str) -> dict:
    """Phần đọc thật. Luôn gọi qua ``_suy_trang_thai`` (nơi bắt lỗi)."""
    import os

    from services import review_timeline as TL

    out: dict = {}
    p = ADV.load_project(project_id, migrate=False)
    pdir, meta = p["dir"], p["meta"]
    a_list = p.get("a_lookup") or {}
    b_list = p.get("b_chunks") or []

    if a_list:
        out["sub"] = "done"
    if b_list:
        out["translate"] = "done"

    sep = os.path.join(pdir, "separated")
    if all(os.path.exists(os.path.join(sep, f"{k}.wav")) for k in ("vocals", "no_vocals")):
        out["separate"] = "done"

    #  GIỌNG: chỉ tính là xong khi MỌI câu đều có tệp giọng. Thiếu vài câu mà báo
    #  xong thì lượt chạy hàng loạt sẽ bỏ qua đúng những câu còn thiếu.
    if b_list:
        co_giong = sum(
            1 for b in b_list if os.path.exists(TL.clip_audio_path(pdir, b))
        )
        if co_giong == len(b_list):
            out["voice"] = "done"

    #  Đã soạn bài đăng rồi thì đừng bắt AI viết lại (tốn một lượt gọi DeepSeek
    #  cho mỗi video, mà bản cũ vẫn dùng được).
    if (meta.get("youtube_meta") or {}).get("tieu_de"):
        out["meta"] = "done"
    if (meta.get("state") or {}).get("render_complete"):
        out["export"] = "done"

    #  MỐC CỦA DẤU VẾT — lúc tệp chứng minh bước đó được ghi. Dùng để vá bước đang "lỗi"
    #  mà người dùng ĐÃ TỰ LÀM LẠI sau đó (xem ``_dong_bo_trang_thai``). Chỉ những bước có
    #  một tệp riêng của chính nó; các bước khác không đủ chắc để đè lên một lỗi thật.
    def _moc(duong):
        try:
            return os.path.getmtime(duong)
        except OSError:
            return 0.0

    out["_moc"] = {
        "sub": _moc(os.path.join(pdir, "a_chunks.json")),
        "translate": _moc(TL.b_chunks_path(pdir)),
        "separate": _moc(os.path.join(sep, "vocals.wav")),
    }
    return out


async def _dong_bo_trang_thai(batch: dict, bo_qua=None,
                              ghi_chu: str = "") -> dict:
    """Vá trạng thái cho những bước CÒN "chưa chạy" theo hiện trạng dự án.

    ``bo_qua``: các dự án KHÔNG được đụng (lô đang cầm chúng — kết quả sắp do lô ghi).
    Ghi vào BẢN MỚI NHẤT trên đĩa chứ không ghi ``batch`` truyền vào: lô đang chạy có
    thể vừa ghi trạng thái mới, ghi đè bằng bản cũ trong tay là mất kết quả của lô.

    Không đụng vào bước đã có KẾT QUẢ THẬT của lô (xong / lỗi / cần quyết): đè lên
    bằng phỏng đoán là mất dấu vết lỗi.

    NHƯNG "CHỜ BẠN DÁN" THÌ CÓ VÁ. Đây là chỗ từng sai: chọn "Tự dịch" / "Tự soạn"
    thì lô đánh dấu bước ấy là ``skipped`` — nghĩa thật của nó là "đang chờ CHÍNH BẠN
    làm", và bảng cũng ghi đúng chữ ấy. Người dùng dán bản dịch xong, hoặc soạn bài
    đăng trong trang Chi tiết rồi quay lại, mà ô vẫn đứng nguyên "Chờ bạn dán" mãi
    mãi — vì hàm này chỉ vá những bước còn "chưa chạy".

    Vá chỗ ấy là AN TOÀN vì ``_suy_trang_thai`` chỉ trả "done" khi có DẤU VẾT THẬT
    trên đĩa (có bản dịch, có bài đã soạn). Chưa làm thì vẫn nằm yên ở "Chờ bạn dán".

    CHỈ hai bước ``translate``/``meta`` mới được vá từ "bỏ qua". Các kiểu bỏ qua khác
    là Ý ĐỊNH của người dùng — bấm nút "bỏ qua" ở một bước, hay tắt tách giọng trong
    cấu hình lô — đè lên là xoá mất ý ấy khỏi bảng.
    """
    #: "Bỏ qua" chỉ được vá ở hai bước này — chỉ chúng mới mang nghĩa "chờ bạn dán".
    VA_BO_QUA = ("translate", "meta")
    #: "LỖI" (``failed``) được vá ở những bước này — KHI VÀ CHỈ KHI dấu vết trên đĩa MỚI HƠN lúc báo
    #: lỗi (``_suy_trang_thai`` trả ``_moc``). Người dùng báo (28-09-2026): bước Dịch của
    #: lô lỗi, họ dán bản dịch tay vào là dự án đã có kịch bản, mà ô vẫn đỏ "Lỗi" mãi —
    #: vì trước đây "lỗi" không bao giờ được vá. So mốc để một bản dịch CŨ có từ trước
    #: lỗi không che mất lỗi thật của lượt vừa chạy.
    VA_LOI = ("sub", "translate", "separate")

    def _va_duoc(st: str, tt: str) -> bool:
        return (
            tt == "pending"
            or (tt == "skipped" and st in VA_BO_QUA)
            or (tt == "failed" and st in VA_LOI)
        )

    def _loi_da_sua(st: str, o: dict, moc: dict) -> bool:
        """Bước "lỗi" này đã được làm lại SAU lúc lỗi chưa (theo mốc tệp)."""
        return float((moc or {}).get(st) or 0.0) > float(o.get("at") or 0.0) + 1.0

    bo_qua = set(bo_qua or ())
    da_xong: dict = {}          # project_id -> [buoc suy ra la done]
    for item in batch.get("items") or []:
        pid = item["project_id"]
        if pid in bo_qua:
            continue
        con_trong = [
            st for st in BS.STEPS
            if _va_duoc(st, (item["steps"].get(st) or {}).get("status", "pending"))
        ]
        if not con_trong:
            continue
        suy = await run_in_threadpool(_suy_trang_thai, pid)
        moc = suy.get("_moc") or {}
        xong = [
            st for st in con_trong
            if suy.get(st) == "done" and (
                (item["steps"].get(st) or {}).get("status") != "failed"
                or _loi_da_sua(st, item["steps"].get(st) or {}, moc)
            )
        ]
        if xong:
            da_xong[pid] = (xong, moc)
    if not da_xong:
        return batch
    #  Ghi vào bản mới nhất trên đĩa, chỉ chạm những ô còn "chưa chạy"/"bỏ qua" ở đó.
    with BS.KHOA_GHI:
        moi = BS.load(batch["id"]) or batch
        doi = False
        for pid, (cac_buoc, moc) in da_xong.items():
            item = BS.find_item(moi, pid)
            if not item:
                continue
            for st in cac_buoc:
                o = item["steps"].get(st) or {}
                tt = o.get("status", "pending")
                #  Kiểm lại trên BẢN MỚI NHẤT: lô có thể vừa chạy lại bước này và báo
                #  lỗi MỚI sau lần đọc mốc ở trên — lỗi mới đó phải được giữ.
                if not _va_duoc(st, tt) or (tt == "failed" and not _loi_da_sua(st, o, moc)):
                    continue
                #  Ghi chú phải đúng chuyện đã xảy ra: "chờ bạn dán" / "lỗi" mà nay xong
                #  là do CHÍNH HỌ vừa làm, không phải "có sẵn từ trước".
                item["steps"][st] = {
                    "status": "done",
                    "error": (
                        L("bạn đã tự làm", "you already did this yourself") if tt == "skipped"
                        else L("bạn đã tự làm lại sau lỗi", "you redid this yourself after the error") if tt == "failed"
                        else (ghi_chu or L("đã có sẵn trước khi vào lô", "already available before joining the batch"))
                    ),
                    "at": o.get("at", 0.0),
                }
                doi = True
        return BS.save(moi) if doi else moi


# ============================================================ TỪNG BƯỚC
#
# Mỗi hàm chạy MỘT dự án và raise khi hỏng. Trả về chuỗi ghi chú (rỗng = không có
# gì đáng nói) hoặc raise ``_CanNguoiQuyet`` khi chạy được nhưng cần người chọn.


class _BamNhatKy:
    """Bám ``synthesis.log`` của dự án đang chạy rồi rót vào NHẬT KÝ LÔ.

    Vì sao cần: nhật ký lô trước đây chỉ có mốc ("bắt đầu Dịch", "xong 2 dự án").
    Một video OCR 20 phút thì suốt 20 phút đó nhìn vào không biết nó đang làm gì,
    cũng không biết nó có treo hay không — mà đó là câu hỏi duy nhất người ta có
    khi bỏ máy chạy hàng loạt. Dòng chi tiết thì mỗi dự án vốn đã ghi sẵn ra
    ``synthesis.log`` (đúng thứ nhật ký trong dự án vẫn hiện), nên ở đây chỉ việc
    bám đuôi tệp đó chứ không dựng đường log thứ hai.

    Chỉ đọc PHẦN MỚI (nhớ vị trí cuối tệp lúc bắt đầu): không thì mỗi bước lại
    chép nguyên nhật ký cũ của dự án vào lô.
    """

    #: Trần dòng cho MỘT bước của MỘT dự án — ffmpeg/Whisper có thể phun hàng vạn
    #: dòng tiến độ; quá trần thì thôi, phần đầu mới là phần nói lên vấn đề.
    TRAN_DONG = 400
    NHIP = 1.5  # giây giữa hai lượt rót

    def __init__(self, batch_id: str, project_id: str, step: str):
        self.batch_id, self.project_id, self.step = batch_id, project_id, step
        self._dung = threading.Event()
        self._luong = None
        self._da = 0

    def _duong_log(self) -> str:
        try:
            #  ``get_config_service()`` chứ không phải module: chỗ lưu dự án do người
            #  dùng đổi được, hằng số chép tay là có ngày trỏ sai thư mục.
            from services.config_service import get_config_service

            goc = get_config_service().get_review_root()
        except Exception:  # noqa: BLE001
            return ""
        return os.path.join(goc, self.project_id, "synthesis.log")

    @staticmethod
    def _gon(dong: str) -> str:
        """Bỏ phần đầu máy móc, giữ lại đúng câu người đọc cần.

        ``2026-09-16 22:47 [INFO] ReviewService: Đang tách...`` -> ``Đang tách...``
        Giờ đã có sẵn ở cột bên trái của nhật ký lô, tên logger thì lặp lại ở mọi
        dòng — để nguyên chỉ tổ đẩy phần chữ thật ra ngoài màn hình.
        """
        d = dong.strip()
        if "] " in d:
            dau, _, sau = d.partition("] ")
            muc = dau.rsplit("[", 1)[-1] if "[" in dau else ""
            sau = sau.split(": ", 1)[-1] if ": " in sau[:40] else sau
            return (f"[{muc}] " if muc in ("ERROR", "WARNING", "CRITICAL") else "") + sau
        return d

    def _chay(self):
        duong = self._duong_log()
        #  DEBUG chứ không INFO: một dòng cho MỖI dự án MỖI bước, lô 200 video là
        #  1200 dòng rác trong nhật ký ứng dụng.
        logger.debug(f"[LÔ] Bám nhật ký: {duong or '(không tìm được đường dẫn)'}")
        if not duong:
            return
        #  Chờ tệp xuất hiện (dự án chạy lần đầu chưa có), nhưng không chờ mãi.
        cho = 0
        while not os.path.exists(duong) and not self._dung.is_set() and cho < 30:
            self._dung.wait(0.5)
            cho += 1
        if not os.path.exists(duong):
            return
        try:
            f = open(duong, "r", encoding="utf-8", errors="ignore")
        except OSError:
            return
        with f:
            f.seek(0, os.SEEK_END)  # CHỈ phần mới
            truoc = ""
            while True:
                #  TỆP BỊ XOÁ TRẮNG GIỮA CHỪNG: mỗi lượt phân tích mở lại
                #  ``synthesis.log`` từ đầu (ghi đè, không nối tiếp). Vị trí đọc cũ
                #  lúc đó nằm QUÁ đuôi tệp mới -> đọc mãi không ra dòng nào, bám
                #  nhật ký thành ra im lặng hoàn toàn. Thấy tệp ngắn đi thì quay về
                #  đầu và đọc lại từ đó.
                try:
                    if os.path.getsize(duong) < f.tell():
                        f.seek(0)
                except OSError:
                    pass

                dong = []
                for raw in f.readlines():
                    raw = raw.rstrip("\n")
                    if not raw.strip():
                        continue
                    g = self._gon(raw)
                    #  Bỏ dòng TRÙNG LIỀN NHAU: thanh tiến độ in lại một câu hàng
                    #  trăm lần, nhét hết vào nhật ký là chôn mất dòng có ích.
                    if g == truoc:
                        continue
                    truoc = g
                    dong.append("    " + g)
                    self._da += 1
                    if self._da >= self.TRAN_DONG:
                        dong.append(L("    … (quá nhiều dòng — xem nhật ký trong dự án)", "    … (too many lines — see the log inside the project)"))
                        break
                if dong:
                    try:
                        BS.them_log(self.batch_id, dong, self.project_id, self.step)
                    except Exception as e:  # noqa: BLE001 — nhật ký hỏng không chặn việc
                        logger.warning(f"[LÔ] Không rót được nhật ký chi tiết: {e}")
                if self._da >= self.TRAN_DONG or self._dung.is_set():
                    #  Lượt cuối đã đọc nốt phần đuôi ở trên rồi -> thoát.
                    return
                self._dung.wait(self.NHIP)

    def __enter__(self):
        self._luong = threading.Thread(target=self._chay, daemon=True)
        self._luong.start()
        return self

    def __exit__(self, *a):
        self._dung.set()
        if self._luong:
            #  Đợi có hạn: luồng chỉ đọc tệp nên không bao giờ treo lâu, nhưng
            #  cũng không để nó giữ chân bộ chạy.
            self._luong.join(timeout=3)
        return False


class _CanNguoiQuyet(Exception):
    """Bước chạy xong phần tự động nhưng còn thứ phải người quyết (bước Dồn).

    ``data`` mang theo thứ giao diện cần để hỏi người dùng (vd các phương án dồn).
    """

    def __init__(self, message: str, data: Optional[dict] = None):
        super().__init__(message)
        self.data = data or {}


async def _buoc_sub(project_id: str, cfg: dict, ctx: dict) -> str:
    meta = review_service.get_metadata(project_id)
    if not meta:
        raise RuntimeError(L("Dự án không còn tồn tại", "The project no longer exists"))
    #  Ghi cấu hình CHUNG của lô xuống từng dự án trước khi chạy — đúng những khoá
    #  mà bảng "Chuẩn bị" trong dự án ghi, để hai đường không lệch nhau.
    meta["source_language"] = cfg.get("source_language") or "auto"
    meta["target_language"] = cfg.get("target_language") or "vi"
    mode = cfg.get("subtitle_mode") or "whisper"
    if mode in ("whisper", "ocr", "hybrid"):
        meta["subtitle_mode"] = mode
    #  Ô "Tách câu gốc" của lô -> dự án (``run_transcription`` đọc từ metadata).
    meta[TC.KHOA_GOC] = TC.bat_goc(cfg)

    #  ĐỌC CHỮ TRÊN HÌNH: bắt buộc phải có VÙNG PHỤ ĐỀ của TỪNG video.
    #
    #  Vì sao chặn hẳn chứ không chạy đại: không khoanh vùng thì OCR quét cả khung
    #  hình — chậm hơn nhiều lần và nhặt luôn chữ của logo, tên kênh, biển hiệu
    #  trong phim vào phụ đề. Chạy xong mới phát hiện là mất cả lượt. Vùng lại
    #  KHÔNG dùng chung được: mỗi video một khung hình, một chỗ đặt phụ đề.
    if mode in ("ocr", "hybrid") and not meta.get("ocr_region"):
        raise _CanNguoiQuyet(
            L("chưa khoanh vùng phụ đề — bấm vào ô này để khoanh cho video này",
              "subtitle region not selected — click this cell to select it for this video")
        )

    review_service._save_metadata(project_id, meta)

    review_service.clear_analysis_cancel(project_id)
    review_service.mark_analysis_started(project_id)
    await run_in_threadpool(run_analysis_wrapper, project_id)

    p = ADV.load_project(project_id, migrate=False)
    n = len(p.get("a_lookup") or {})
    if not n:
        raise RuntimeError(L("Chạy xong nhưng không lấy được câu phụ đề nào", "Finished running but no subtitle sentences were obtained"))
    return L(f"{n} câu gốc", f"{n} source {'sentence' if n == 1 else 'sentences'}")


class _TuDich(Exception):
    """Người dùng chọn TỰ DỊCH — lô không đụng vào bước này."""


async def _dich_bang_google(project_id: str, cfg: dict) -> str:
    """Dịch THẲNG từng câu gốc sang ngôn ngữ đích rồi nạp làm lời bình.

    Khác đường DeepSeek (viết lại thành kịch bản): ở đây một câu gốc thành đúng
    một câu lời bình, giữ nguyên mốc thời gian — hợp khi chỉ cần hiểu đúng nghĩa,
    nhanh và không tốn API key. CỐ Ý không đụng tới kịch bản review hay lồng tiếng
    nhiều nhân vật: đó là việc của DeepSeek, gộp vào đây chỉ làm rối.
    """
    from services.translation_service import TranslationService

    p = ADV.load_project(project_id, migrate=False)
    meta = p["meta"]
    a_list = sorted(
        (p.get("a_lookup") or {}).values(),
        key=lambda a: float(a.get("start") or 0),
    )
    cau = [(a.get("a_text") or a.get("text") or "").strip() for a in a_list]
    if not any(cau):
        raise RuntimeError(L("Phụ đề gốc rỗng — không có gì để dịch.", "The source subtitles are empty — nothing to translate."))

    src = cfg.get("source_language") or meta.get("source_language") or "auto"
    dest = cfg.get("target_language") or meta.get("target_language") or "vi"
    ra = await TranslationService().batch_translate(cau, src=src, dest=dest,
                                                    engine="google")

    chunks, hong = [], 0
    for a, goc, dich in zip(a_list, cau, ra):
        van = (dich or "").strip()
        #  Đoạn lỗi/bị chặn thì GIỮ NGUYÊN CÂU GỐC thay vì nhét chuỗi "[Error: …]"
        #  vào lời bình — nhét vào là giọng đọc sẽ đọc luôn cái chuỗi đó.
        if not van or van.startswith(("[Error:", "[Stopped:", "[RateLimit:")):
            van = goc
            hong += 1
        chunks.append({"b_text": van, "source_a_ids": [a.get("a_id")]})

    #  Ô "Tách bản dịch" của lô (đường Google không qua ``_ap_cau_hinh_dich``) — ghi vào meta để
    #  dự án nhớ cùng giá trị; ``import_script`` lưu meta lúc xong.
    meta[TC.KHOA_DICH] = TC.bat_dich(cfg)
    out = await run_in_threadpool(
        ADV.import_script, p["dir"], meta, p["a_lookup"], chunks
    )
    n = len(chunks)
    return (f"{n} câu (Google Dịch)"
            + (f", {hong} câu không dịch được — giữ nguyên câu gốc" if hong else "")
            + (f", giữ lại {out.get('reused', 0)} câu cũ" if out.get("reused") else ""))


def _ap_cau_hinh_dich(p: dict, cfg: dict, item: dict) -> bool:
    """Áp CẤU HÌNH LÔ xuống metadata dự án trước khi dựng prompt. Trả True nếu đổi.

    Prompt dịch do backend dựng TỪ METADATA DỰ ÁN (tên ngôn ngữ, trần ký tự theo
    CPS, văn phong, tên kênh, giới tính giọng). Cấu hình lô không ghi xuống đó thì
    prompt nói một đằng còn ô chọn trên trang lô nói một nẻo — chọn "Dịch sang
    tiếng Nhật" mà prompt vẫn bảo AI dịch sang tiếng Việt, không có lỗi nào báo.

    DÙNG CHUNG cho cả hai đường: lô tự gọi DeepSeek, và nút "Copy prompt" ở hộp
    dịch tay. Hai đường mà dựng prompt theo hai lối là sớm muộn chúng lệch nhau —
    đúng thứ vừa xảy ra với nút Copy prompt.
    """
    meta = p["meta"]
    doi = False

    #  NGÔN NGỮ DỊCH phải ghi ở đây chứ không chỉ ở bước Lấy phụ đề: lô nào bỏ qua
    #  bước đó (video đã có sub sẵn, hoặc chạy lại riêng bước Dịch) thì vẫn dùng
    #  "vi" cũ.
    dich_sang = (cfg.get("target_language") or "").strip()
    if dich_sang and meta.get("target_language") != dich_sang:
        meta["target_language"] = dich_sang
        doi = True
    if cfg.get("speech_style") and meta.get("speech_style") != cfg["speech_style"]:
        meta["speech_style"] = str(cfg["speech_style"])
        doi = True
    ten_kenh = (cfg.get("brand_name") or "").strip()
    if ten_kenh and meta.get("brand_name") != ten_kenh:
        meta["brand_name"] = ten_kenh
        doi = True

    #  GIỌNG ĐỌC cũng vào prompt — nó quyết định AI xưng hô kiểu nam hay nữ. Trước
    #  đây giọng của lô chỉ được ghi ở BƯỚC SINH GIỌNG, tức là SAU khi dịch xong:
    #  lô chọn giọng nam mà dự án đang để giọng nữ thì cả bản dịch xưng hô giọng nữ,
    #  rồi mới đọc bằng giọng nam.
    giong = (
        (item.get("voice") or "").strip()
        if (cfg.get("voice_mode") or "chung") == "rieng"
        else (cfg.get("voice") or "").strip()
    )
    if giong and meta.get("target_voice") != giong:
        meta["target_voice"] = giong
        doi = True
    #  CHO GIONG TRAN SANG CAU SAU: buoc Don / Xuat cua du an doc tu metadata (``TL.tran_giong_cho_phep``) — lo khong chep
    #  xuong thi cau hinh lo noi mot dang, du an lam mot neo (cung bay voi ngon ngu dich o tren).
    for k in (TL.TRAN_BAT_FIELD, TL.TRAN_GIAY_FIELD):
        if k in cfg and meta.get(k) != cfg[k]:
            meta[k] = cfg[k]
            doi = True
    #  Ô "Tách bản dịch" của lô -> dự án: ``ADV.import_script`` đọc từ metadata ở MỌI đường nhập
    #  (AI của lô, dán bản dịch ở trang lô, mở dự án ra dán).
    tach = TC.bat_dich(cfg)
    if TC.bat_dich(meta) != tach or TC.KHOA_DICH not in meta:
        meta[TC.KHOA_DICH] = tach
        doi = True
    return doi


async def _buoc_translate(project_id: str, cfg: dict, ctx: dict) -> str:
    nguon = (cfg.get("translate_engine") or "ai").strip()

    p = ADV.load_project(project_id, migrate=False)
    if not (p.get("a_lookup") or {}):
        raise RuntimeError(L("Chưa có phụ đề gốc — chạy bước Lấy phụ đề trước.", "There are no source subtitles yet — run the Get subtitles step first."))

    if nguon == "manual":
        #  Tự dịch: KHÔNG chạy gì, cũng không tính là lỗi.
        #  Nhưng NẾU người dùng đã dán kịch bản rồi (dán ngay ở trang lô, hoặc mở dự
        #  án ra dán) thì tính là XONG — không thì bảng mãi mãi báo "bỏ qua" và nhìn
        #  như còn việc chưa làm, dù thực ra đã có đủ lời bình để sinh giọng.
        n = len(p.get("b_chunks") or [])
        if n:
            return L(f"{n} câu (dịch tay)", f"{n} {'sentence' if n == 1 else 'sentences'} (manual translation)")
        raise _TuDich(L("bạn chọn tự dịch — bấm nút dịch tay ở cột này để dán bản dịch", "you chose manual translation — click the manual translate button in this column to paste the translation"))

    if nguon == "google":
        return await _dich_bang_google(project_id, cfg)

    import services.deepseek_service as deepseek_service
    from services.ai_engines import provider_of, ten_bo_ai
    from services.config_service import get_config_service

    #  "deepseek" / "gemini" chọn thẳng bộ AI; "ai" (mặc định) hoặc giá trị lạ (lô cũ) -> bộ AI mặc định ở Cài đặt.
    bo_ai = provider_of(nguon) or get_config_service().ai_provider_mac_dinh()
    if not deepseek_service.is_configured(bo_ai):
        raise RuntimeError(L(
            f"Chưa cấu hình API key {ten_bo_ai(bo_ai)} — vào Cài đặt nhập key, hoặc đổi nguồn "
            "dịch sang Google Dịch / Tự dịch ở cấu hình lô.",
            f"The {ten_bo_ai(bo_ai)} API key is not configured — go to Settings to enter the key, or change the translation "
            "source to Google Translate / Manual in the batch configuration."
        ))

    if await run_in_threadpool(_ap_cau_hinh_dich, p, cfg, ctx.get("item") or {}):
        await run_in_threadpool(ADV.save_meta, p["dir"], p["meta"])

    kind = cfg.get("script_kind") or (p["meta"].get("script_kind") or "")

    #  MODEL PHAI TRUYEN THAT. Truoc day cho nay dat "" (dau tham so ``model``), vi
    #  duong HTTP tuong ung tu no da thay "" bang model mac dinh — nhung day KHONG di
    #  qua duong do, no goi thang ham nen. Ket qua: chay theo lo GUI MODEL RONG len
    #  DeepSeek. Lau nay DeepSeek van doan lay model mac dinh nen khong ai thay; tu khi
    #  ho siet kiem tra thi moi lo tra 400 "but you passed ." — ca lo hong sach.
    model = get_config_service().ai_model_mac_dinh_cua(bo_ai)
    await run_in_threadpool(_ai_script_wrapper, project_id, kind, model, "", False)

    p2 = ADV.load_project(project_id, migrate=False)
    n = len(p2.get("b_chunks") or [])
    #  Nhãn theo BỘ AI THẬT và LOẠI KỊCH BẢN THẬT — trước ghi cứng "lời bình (DeepSeek)" dù đang lồng tiếng bằng Gemini.
    ten_ai = ten_bo_ai(bo_ai)
    loai = "lời bình" if PROMPTS.normalize_kind(kind) == PROMPTS.KIND_REVIEW else "câu lồng tiếng"
    if not n:
        raise RuntimeError(L(f"{ten_ai} không trả về câu nào", f"{ten_ai} returned no sentences"))
    if loai == "lời bình":
        return L(f"{n} {loai} ({ten_ai})", f"{n} commentary {'line' if n == 1 else 'lines'} ({ten_ai})")
    return L(f"{n} câu lồng tiếng ({ten_ai})", f"{n} dubbing {'sentence' if n == 1 else 'sentences'} ({ten_ai})")


def _co_stem(pdir: str) -> bool:
    """Dự án này đã có sẵn hai stem (giọng gốc / nhạc nền) trên đĩa chưa."""
    return all(
        os.path.exists(os.path.join(pdir, "separated", f"{k}.wav"))
        for k in ("vocals", "no_vocals")
    )


def _bat_co_tach(project_id: str) -> str:
    """Bật ``separate_audio`` trong metadata dự án SAU KHI đã có stem trên đĩa.

    VÌ SAO BẮT BUỘC: có stem trên đĩa mà cờ vẫn tắt là trạng thái NỬA VỜI, và nó
    hỏng âm thầm theo đúng ba kiểu người dùng đã gặp:
      · trình dựng thấy stem nên KHOÁ nút "Tách giọng" — tưởng đã tách xong rồi;
      · lúc trộn thì cờ tắt nên hai mức "Giọng gốc" bị BỎ QUA — kéo về 0 mà nghe
        thử vẫn còn nguyên giọng gốc;
      · ai lỡ bấm "đưa stem lên timeline" (thao tác đó tắt ``source_audio_enabled``)
        thì bản xuất MẤT SẠCH tiếng gốc: stem không được dùng vì cờ tắt, mà tiếng
        gốc cũng bị bỏ vì công tắc kia đã tắt.

    Trả về chuỗi ghi chú thêm vào dòng nhật ký (rỗng nếu vốn đã bật).
    """
    p = ADV.load_project(project_id, migrate=False)
    meta = p["meta"]
    if meta.get("separate_audio"):
        return ""
    meta["separate_audio"] = True
    #  Cùng cờ mà bảng Chuẩn bị đặt khi người dùng kéo bất kỳ mức nào: thiếu nó thì
    #  lúc dựng engine đi đường tắt ``-c copy`` và bỏ qua cả 4 mức.
    meta[ADV.MIX_TOUCHED_FIELD] = True
    ADV.save_meta(p["dir"], meta)
    return L(" — đã bật “tách giọng” cho dự án", " — “vocal separation” has been turned on for the project")


async def _buoc_separate(project_id: str, cfg: dict, ctx: dict) -> str:
    """Tách giọng khỏi nhạc nền (Demucs) — nặng, nhưng là thứ mở khoá 4 mức âm lượng.

    Đã có stem sẵn thì BỎ QUA, không chạy lại: Demucs mất vài phút mỗi video, chạy
    lại cả lô chỉ vì bấm nhầm nút là mất cả buổi.
    """
    import os

    #  Âm thanh lấy THEO LÔ mà cấu hình lô không bật tách giọng -> bước này vô nghĩa
    #  (stem tách ra không được dùng khi trộn). Bỏ qua thay vì đốt vài phút Demucs
    #  mỗi video. Lấy theo TỪNG DỰ ÁN thì vẫn tách — mỗi dự án tự quyết.
    if (cfg.get("audio_source") or "lo") == "lo" and not cfg.get("separate_audio"):
        raise _TuDich("cấu hình lô không bật tách giọng — bỏ qua" if L("vi", "en") == "vi"
                       else "the batch configuration does not enable vocal separation — skipped")

    p = ADV.load_project(project_id, migrate=False)
    out_dir = os.path.join(p["dir"], "separated")
    from services import review_video_clips as VID

    da_co = not VID.is_separating(out_dir) and all(
        os.path.exists(os.path.join(out_dir, f"{k}.wav"))
        for k in ("vocals", "no_vocals")
    )
    if da_co:
        return L("đã có sẵn", "already available") + _bat_co_tach(project_id)

    review_service.clear_render_cancel(project_id)
    await run_in_threadpool(_separate_wrapper, project_id)

    xong = all(
        os.path.exists(os.path.join(out_dir, f"{k}.wav"))
        for k in ("vocals", "no_vocals")
    )
    if not xong:
        raise RuntimeError(L("Tách xong nhưng không thấy tệp giọng/nhạc nền", "Separation finished but the vocal/background music files were not found"))
    return L("đã tách", "separated") + _bat_co_tach(project_id)


async def _buoc_voice(project_id: str, cfg: dict, ctx: dict) -> str:
    p = ADV.load_project(project_id, migrate=False)
    if not (p.get("b_chunks") or []):
        raise RuntimeError(L("Chưa có lời bình — chạy bước Dịch (hoặc nhập tay) trước.", "There is no commentary yet — run the Translate step (or enter it manually) first."))
    #  GIỌNG: ưu tiên giọng RIÊNG của video này (người dùng chọn ở bảng), không có
    #  thì lấy giọng chung của lô. Ghi xuống cài đặt dự án trước khi sinh, đúng khoá
    #  mà ô chọn giọng trong dự án dùng. Rỗng cả hai = tôn trọng giọng dự án đã đặt.
    if (cfg.get("voice_mode") or "chung") == "rieng":
        voice = ((ctx.get("item") or {}).get("voice") or "").strip()
    else:
        voice = (cfg.get("voice") or "").strip()
    if voice:
        meta = p["meta"]
        meta["target_voice"] = voice
        ADV.save_meta(p["dir"], meta)

    review_service.clear_render_cancel(project_id)
    #  Bước này gọi THẲNG vào engine (không qua endpoint) nên phải TỰ làm hai việc
    #  mà các wrapper kia làm hộ:
    #    · XẾP HÀNG việc nặng,
    #    · GẮN NHẬT KÝ dự án (synthesis.log). Thiếu cái thứ hai thì suốt lúc sinh
    #      giọng — bước lâu nhất của cả lô — nhật ký lô im như tờ, nhìn vào không
    #      biết nó đang đọc tới câu nào hay đã treo.
    from routes.review_routes import remove_project_logging, setup_project_logging
    from services.config_service import get_config_service

    handler = await run_in_threadpool(
        setup_project_logging, project_id, get_config_service().get_review_root()
    )
    try:
        async with await WQ.slot_async(
            project_id, "Sinh giọng (lô)",
            cancelled=lambda: review_service.is_render_cancelled(project_id),
        ):
            res = await ADV.generate_voices(project_id, uids=None, force=False)
    finally:
        await run_in_threadpool(remove_project_logging, project_id, handler)
    if res.get("cancelled"):
        raise RuntimeError(L("Đã huỷ giữa chừng", "Cancelled midway"))
    failed = int(res.get("failed", 0) or 0)
    ok = int(res.get("ok", 0) or 0)
    if failed and not ok:
        raise RuntimeError(f"Không sinh được giọng nào ({failed} câu lỗi)")
    return f"{ok} câu" + (f", {failed} câu lỗi" if failed else "")


async def _buoc_fit(project_id: str, cfg: dict, ctx: dict) -> str:
    res = await run_in_threadpool(ADV.auto_arrange, project_id)
    cut = int(res.get("cut", 0) or 0)
    if not cut:
        if res.get("da_vua"):
            return L("mọi câu đã đủ chỗ, không cần dồn", "all sentences already fit, no rearranging needed")
        return L(f"dồn {res.get('moved', 0)} câu", f"rearranged {res.get('moved', 0)} {'sentence' if res.get('moved', 0) == 1 else 'sentences'}")

    #  CÒN CÂU THIẾU CHỖ. Hai lối, do cấu hình lô chọn:
    #
    #    "ai"  — nhờ DeepSeek viết lại NGẮN HƠN đúng những câu đọc không kịp rồi sinh
    #            giọng lại. Lô chạy qua đêm cần lối này: dừng ở đây là sáng ra thấy cả
    #            lô đứng im chờ một cái bấm.
    #    "hoi" — dừng và đánh dấu "cần bạn quyết" (đường cũ). Đổi tốc độ cả dự án là
    #            việc lớn, không được tự động thay người dùng.
    them = ""
    mode = cfg.get("fit_mode") or "hoi"
    if mode == "auto":
        #  "Tự chọn hộ": máy chọn một phương án NHẸ mà xong hẳn (giọng ≤1,35×, phim
        #  ≥0,90×) rồi áp luôn. Không có phương án nào như vậy -> rơi xuống hỏi người
        #  dùng như chế độ "hoi" — không bao giờ tự chọn phương án nặng.
        auto = await run_in_threadpool(ADV.tu_chon_va_ap_dung, project_id)
        if auto.get("applied"):
            o = auto.get("option") or {}
            if o.get("kind") == "cuc_bo":
                return L(f"dồn {res.get('moved', 0)} câu; tự chọn: tăng tốc riêng "
                         f"{o.get('so_cau', 0)} câu dài (tối đa ×{o.get('max_factor')})",
                         f"rearranged {res.get('moved', 0)} sentences; auto-picked: sped up "
                         f"{o.get('so_cau', 0)} long sentences individually (max ×{o.get('max_factor')})")
            return L(f"dồn {res.get('moved', 0)} câu; tự chọn: giọng ×{o.get('voice_speed')}, "
                     f"phim ×{o.get('video_speed')}",
                     f"rearranged {res.get('moved', 0)} sentences; auto-picked: voice ×{o.get('voice_speed')}, "
                     f"video ×{o.get('video_speed')}")
        them = L("; không có phương án nhẹ nào xong hẳn", "; no light option fits everything")
        #  LÀM NHANH (người dùng 09-10-2026): không chọn được thì CỨ ĐI TIẾP — bước dồn chỉ là
        #  "tăng tốc vài chỗ / giãn phim một chút cho khớp được thì tốt", không bắt buộc chuẩn
        #  chỉnh toàn bộ. Không hỏi, không nhờ AI rút gọn.
        if cfg.get("fit_tu_tiep"):
            return (L(f"còn {cut} câu chưa vừa chỗ — vẫn tiếp tục (có thể chỉnh sau trong Chi tiết)",
                      f"{cut} sentences still do not fit — continuing anyway (you can adjust later in Advanced)")
                    + them)
    if mode == "ai":
        import services.deepseek_service as deepseek_service
        from services import review_advanced_fit_ai as FITAI

        if not deepseek_service.is_configured():
            raise _CanNguoiQuyet(
                L(f"Còn {cut} câu thiếu chỗ nhưng CHƯA có API key của bộ AI đang chọn (DeepSeek / Gemini) — "
                  "vào Cài đặt nhập key, hoặc chọn phương án dồn",
                  f"{cut} sentences do not fit and there is NO API key for the selected AI (DeepSeek / Gemini) — "
                  "go to Settings to enter the key, or choose a rearrange option"),
                {"arrange": res},
            )
        #  Nhật ký: rót thẳng vào nhật ký lô để nhìn là biết nó đang làm gì.
        bid = str(ctx.get("batch_id") or "")

        def _log(dong: str) -> None:
            if bid:
                BS.them_log(bid, [dong], project_id, "fit")

        rg = await FITAI.rut_gon_cho_vua(project_id, ghi_log=_log)
        cut = int(rg.get("cut", 0) or 0)
        them = L(f"; DeepSeek rút gọn {rg.get('changed', 0)} câu", f"; DeepSeek shortened {rg.get('changed', 0)} sentences")
        if not cut:
            return f"dồn {res.get('moved', 0)} câu{them}"
        #  Rút gọn rồi VẪN không đủ chỗ -> lúc này mới thật sự phải người quyết.
        them += L(f"; vẫn còn {cut} câu", f"; {cut} still remaining")

    #  Tính sẵn các phương án để modal gợi ý mở ra là có ngay.
    plan = await run_in_threadpool(ADV.plan_arrange_fit, project_id)
    raise _CanNguoiQuyet(
        f"Còn {cut} câu thiếu chỗ ({res.get('cut_total', 0)}s) — chọn phương án dồn{them}"
        if L("vi", "en") == "vi" else
        f"{cut} sentences do not fit ({res.get('cut_total', 0)}s) — choose a rearrange option{them}",
        {"plan": plan, "arrange": res},
    )


def _chep_ban_xuat(src: str, thu_muc: str, ten_du_an: str) -> str:
    """Chép bản xuất về THƯ MỤC CHUNG của lô, trả đường dẫn mới (hoặc "" nếu bỏ qua).

    CHÉP chứ không di chuyển: tệp trong thư mục dự án là thứ trình dựng dùng để xem
    lại/ xuất lại; dời đi thì mở dự án lên thấy "chưa dựng bao giờ".

    Trùng tên thì thêm hậu tố — hai dự án cùng tên (rất hay gặp: "E1", "Video 1")
    mà ghi đè nhau thì người dùng mất bản xuất mà không hề biết.
    """
    import os
    import shutil

    if not thu_muc:
        return ""
    os.makedirs(thu_muc, exist_ok=True)
    #  Giới hạn rộng hơn 80: tên dự án đã cắt ở 80 (``_ten_du_an_sach``) rồi mới ghép
    #  tiền tố "[XUẤT] " / "[ĐOẠN n] ". Cắt lại ở 80 thì bản xuất và các đoạn bị cụt ở
    #  hai chỗ khác nhau, đặt cạnh nhau nhìn như hai dự án.
    goc = _ten_tep_an_toan(ten_du_an, 100) or "video"
    dich = os.path.join(thu_muc, goc + ".mp4")
    i = 2
    while os.path.exists(dich):
        dich = os.path.join(thu_muc, f"{goc} ({i}).mp4")
        i += 1
    shutil.copy2(src, dich)
    return dich


def _ten_tep_an_toan(ten: str, gioi_han: int = 80) -> str:
    import re

    ten = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (ten or "").strip())
    return ten[:gioi_han].strip(" .")


#: TIỀN TỐ đầu tên tệp xuất — người dùng góp ý (24-09-2026): tên dài, phần phân biệt ở
#: cuối không nhìn kịp, dễ nhầm bản xuất với video gốc hoặc với các đoạn nhỏ.
#: MIRROR với ``TIEN_TO_XUAT`` / ``tienToDoan`` ở frontend/src/utils/compositorExport.ts.
TIEN_TO_XUAT = "[XUẤT]"


def tien_to_doan(i: int) -> str:
    return f"[ĐOẠN {i}]"


def _ten_du_an_sach(meta: dict, project_id: str) -> str:
    """Tên dự án dùng trong tên tệp: bỏ ký tự Windows cấm + bỏ ĐUÔI VIDEO dính theo tên
    (dự án tạo từ tệp "…#传统手工艺.mp4" thì tên xuất thành "….mp4.mp4")."""
    import re

    ten = _ten_tep_an_toan(meta.get("title") or "")
    ten = re.sub(r"\.(mp4|mkv|mov|webm|avi|flv|m4v|ts|wmv)\s*$", "", ten, flags=re.I).strip(" .")
    return ten or project_id


def _dot_hinh(project_id: str, meta: dict, ranges: Optional[list] = None,
              tien_to: str = TIEN_TO_XUAT, phu_de: Optional[dict] = None) -> str:
    """Đốt phụ đề & lớp phủ lên ``final_review.mp4`` — ĐÚNG bước 2/2 của trình dựng.

    Trả về đường dẫn tệp đã đốt. Cấu hình lấy từ tệp cấu hình dựng hình của chính
    dự án (thứ mà trình dựng ghi xuống khi người dùng chỉnh), nên kiểu phụ đề, vùng
    che sub, logo, watermark, khung đều y như lúc xem trước trong dự án.
    """
    from routes.review_routes import (
        _review_compositor_config_path,
        _run_review_compositor_export,
    )
    from services.compositor_utils import load_compositor_config

    cau_hinh = load_compositor_config(_review_compositor_config_path(project_id))
    ten = _ten_du_an_sach(meta, project_id)
    #  ``ranges`` = MỘT đoạn đã khoanh (bước "Xuất các đoạn đã chọn") — cùng thân gửi
    #  như nút "Xuất đoạn" của trình dựng: bước 1/2 đã dựng riêng đoạn đó (tệp
    #  ``.dachon``), bước này khớp biên nhận rồi đốt đúng đoạn ấy.
    than = {
        "config": cau_hinh,
        #  Cùng luật với trình dựng: tắt hẳn kiểu phụ đề thì không đốt chữ.
        "burn_subtitles": (cau_hinh.get("subtitleStyle") or {}).get("enabled")
        is not False,
        "filename": f"{tien_to} {ten}.mp4",
    }
    if ranges:
        than["ranges"] = ranges
    #  Phụ đề nhiều thứ tiếng: ``{"sub_lang": .., "srt_langs": [..]}`` (xem ``_phu_de_them``).
    if phu_de:
        than.update({k: v for k, v in phu_de.items() if v})

    #  XẾP HÀNG: đốt hình là việc NẶNG (ffmpeg mã hoá lại toàn bộ khung hình) — phải
    #  đi chung hàng đợi với mọi việc nặng khác, không thì lô đang đốt mà người dùng
    #  bấm sinh giọng trong dự án khác là hai bên tranh nhau CPU.
    with WQ.slot(project_id, "Đốt hình (lô)",
                 cancelled=lambda: review_service.is_render_cancelled(project_id)):
        ra = _run_review_compositor_export(project_id, than)
    duong = (ra or {}).get("file_path") or ""
    if not duong or not os.path.exists(duong):
        raise RuntimeError(L("Đốt hình xong nhưng không thấy tệp video", "Burning finished but the video file was not found"))
    return duong


async def _ap_am_thanh_cua_lo(project_id: str, cfg: dict) -> bool:
    """Ghi 4 mức âm lượng + mức giọng AI của LÔ xuống dự án, ngay trước khi xuất.

    Chỉ chạy khi lô để ``audio_source = "lo"``. Để "du_an" thì KHÔNG đụng gì — mỗi dự
    án giữ đúng mức người dùng đã chỉnh trong trình dựng.

    Vì sao ghi ở BƯỚC XUẤT chứ không ghi một lần lúc cấu hình: người dùng có thể vào
    sửa một dự án giữa chừng, và thứ quyết định bản xuất là mức TẠI LÚC XUẤT. Ghi ở
    đây thì con số trên màn hình lô đúng là con số sẽ nghe thấy trong tệp ra.
    """
    if (cfg.get("audio_source") or "lo") != "lo":
        return False
    khoa = [k for k in (*ADV.PROJECT_MIX_KEYS, ADV.TTS_VOLUME_FIELD) if k in cfg]
    if not khoa:
        return False
    p = ADV.load_project(project_id, migrate=False)
    meta = p["meta"]
    for k in khoa:
        meta[k] = TL.clamp_volume(cfg[k], 0.0)
    #  Thiếu cờ này thì lúc dựng, engine đi đường tắt ``-c copy`` và BỎ QUA cả 4 mức
    #  vừa đặt (xem ADV.mix_touched) — đặt xong mà bản xuất không đổi gì.
    meta[ADV.MIX_TOUCHED_FIELD] = True
    await run_in_threadpool(ADV.save_meta, p["dir"], meta)
    return True


async def _ghi_cau_hinh_lo_xuong_du_an(b: dict, pids=None, dung_hinh: bool = False) -> int:
    """GHI NGAY cấu hình lô xuống metadata của từng dự án (người dùng 07-10-2026: "đặt cấu hình ở lô
    xong bấm vào từng dự án Chi tiết phải thấy cấu hình đó").

    Trước đây cấu hình lô chỉ chạm tới dự án ĐÚNG LÚC bước tương ứng chạy (xem ``_ap_cau_hinh_dich``,
    ``_buoc_sub``, ``_ap_am_thanh_cua_lo``) — mở dự án ra trước khi chạy thì thấy toàn giá trị cũ. Nay
    gọi thêm ở ``patch_config`` / ``add_items`` / ``add_from_videos``; các bước vẫn ghi lại lúc chạy
    như cũ (nguồn sự thật lúc chạy vẫn là cấu hình lô). Dùng ĐÚNG các hàm / khoá mà các bước dùng,
    không tạo đường ghi thứ hai.

    Áp: ngôn ngữ nguồn / đích, cách lấy phụ đề, văn phong, tên kênh, giọng (chung hoặc riêng), giọng
    tràn, cách xử lý câu không vừa, loại kịch bản (nếu lô đặt), âm thanh (chỉ khi ``audio_source =
    "lo"``, cùng luật giữ cờ tách giọng khi đã có stem). Dựng hình theo dự án mẫu (``template_project``)
    chỉ chép khi ``dung_hinh`` — tức khi vừa đổi mẫu / phần mẫu hoặc vừa thêm dự án — vì chép tệp đi
    kèm nặng và sẽ đè phần người dùng tự chỉnh trong từng dự án. Lô đang chạy thì không đụng (bước
    đang chạy tự ghi). Một dự án hỏng không chặn các dự án khác. Trả số dự án đã ghi.
    """
    if BS.is_running(b):
        return 0
    cfg = b.get("config") or {}
    chon = set(pids) if pids else None
    mau, phan = None, []
    tpl = (cfg.get("template_project") or "").strip() if dung_hinh else ""
    if tpl:
        try:
            from routes.review_routes import _review_compositor_config_path
            from services.compositor_utils import load_compositor_config

            mau = await run_in_threadpool(load_compositor_config, _review_compositor_config_path(tpl))
            phan = [x for x in (cfg.get("template_parts") or MAC_DINH_PHAN_MAU) if x in PHAN_MAU]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LÔ] Không đọc được dựng hình của dự án mẫu {tpl}: {e}")
            mau = None
    xong = 0
    for item in b.get("items") or []:
        pid = item.get("project_id") or ""
        if not pid or (chon is not None and pid not in chon):
            continue
        try:
            p = ADV.load_project(pid, migrate=False)
            meta = p["meta"]
            _ap_cau_hinh_dich(p, cfg, item)
            meta["source_language"] = cfg.get("source_language") or "auto"
            mode = cfg.get("subtitle_mode") or "whisper"
            if mode in ("whisper", "ocr", "hybrid"):
                meta["subtitle_mode"] = mode
            meta[TC.KHOA_GOC] = TC.bat_goc(cfg)          # cùng dòng ở ``_buoc_sub``
            if cfg.get("fit_mode") in ("hoi", "ai"):
                meta["fit_mode"] = cfg["fit_mode"]
            if (cfg.get("script_kind") or "").strip():
                meta["script_kind"] = PROMPTS.normalize_kind(cfg["script_kind"])
            if (cfg.get("audio_source") or "lo") == "lo":
                muon = bool(cfg.get("separate_audio"))
                if muon or not _co_stem(p["dir"]):
                    meta["separate_audio"] = muon
                co_muc = False
                for k in (*ADV.PROJECT_MIX_KEYS, ADV.TTS_VOLUME_FIELD):
                    if k in cfg:
                        meta[k] = TL.clamp_volume(cfg[k], 0.0)
                        co_muc = True
                if co_muc:
                    meta[ADV.MIX_TOUCHED_FIELD] = True
            await run_in_threadpool(ADV.save_meta, p["dir"], meta)
            #  PHỤ ĐỀ THÊM THỨ TIẾNG của lô (đốt / dòng thứ hai / .srt / mềm) -> cùng khoá mà trình dựng Chi tiết
            #  đọc (``config.subtitleLang`` / ``secondSubLang`` / ``srtLangs`` / ``softLangs``) để khung xem trước vẽ
            #  dòng phụ (người dùng 08-10-2026: lô bật dòng Nhật, xuất đúng mà mở dự án không thấy). Bước Xuất của lô
            #  vẫn gửi thẳng lựa chọn của lô (``_phu_de_them``) — đây chỉ để dự án NHÌN THẤY cùng một thứ.
            try:
                from routes.review_routes import _review_compositor_config_path
                from services import review_sub_tracks as ST
                from services.compositor_utils import save_compositor_config

                main = ST.chuan_ma(cfg.get("target_language") or meta.get("target_language") or "vi")
                langs = [ST.chuan_ma(x) for x in (cfg.get("sub_langs") or []) if str(x or "").strip()]
                langs = [x for x in dict.fromkeys(langs) if x and x != main]
                burn = ST.chuan_ma(cfg.get("sub_burn_lang") or "")
                burn = burn if burn in langs else ""
                hai = ST.chuan_ma(cfg.get("sub_second_lang") or "")
                if hai not in (main, *langs) or hai == (burn or main):
                    hai = ""
                tat_ca = [main, *langs]
                duong = _review_compositor_config_path(pid)
                cu = await run_in_threadpool(_doc_cau_hinh_tho, duong)
                moi_cfg = {
                    "subtitleLang": burn, "secondSubLang": hai,
                    "srtLangs": tat_ca if cfg.get("sub_srt") else [],
                    "softLangs": tat_ca if cfg.get("sub_soft") else [],
                }
                if any((cu or {}).get(k) != v for k, v in moi_cfg.items()):
                    await run_in_threadpool(save_compositor_config, duong, {**(cu or {}), **moi_cfg})
            except Exception as e:  # noqa: BLE001 — không chặn phần cấu hình đã ghi
                logger.warning(f"[LÔ] Không ghi được lựa chọn phụ đề thêm thứ tiếng xuống {pid}: {e}")
            if mau is not None and phan and pid != tpl:
                from routes.review_routes import _review_compositor_config_path
                from services.compositor_utils import save_compositor_config

                duong = _review_compositor_config_path(pid)
                cu = await run_in_threadpool(_doc_cau_hinh_tho, duong)
                await run_in_threadpool(save_compositor_config, duong, ghep_phan_mau(cu, mau, phan, pid))
            xong += 1
        except Exception as e:  # noqa: BLE001 — một dự án hỏng không chặn cả lô
            logger.warning(f"[LÔ] Không ghi được cấu hình lô xuống {pid}: {e}")
    return xong


def _video_nguon_cua(p: dict) -> str:
    """Đường dẫn video GỐC của dự án, đã giải qua kho lưu trữ.

    ``metadata.json`` giữ đường dẫn người dùng chọn lúc tạo dự án; tệp có thể đã
    được dời vào kho chung, nên phải giải lại giống lúc nạp dự án chứ không dùng
    thẳng chuỗi đó.
    """
    import os

    from services.separation_service import SeparationService

    tho = ((p.get("meta") or {}).get("video_path") or "").strip()
    if not tho:
        return ""
    try:
        duong = SeparationService.resolve_source_media(tho)[0]
    except Exception:  # noqa: BLE001
        duong = tho
    return duong if duong and os.path.exists(duong) else ""


async def _buoc_meta(project_id: str, cfg: dict, ctx: dict) -> str:
    """SOẠN SẴN BÀI ĐĂNG: tiêu đề, mô tả, thẻ, hashtag, câu hook cho ảnh bìa.

    VÌ SAO Ở ĐÂY MÀ KHÔNG Ở TAB ĐĂNG: bản dịch vừa xong là đã đủ ngữ cảnh cho AI.
    Chạy ngay lúc này thì tới khi mở tab đăng, mọi thứ đã nằm sẵn — không phải bấm
    "AI soạn cả loạt" rồi ngồi chờ đúng lúc muốn đăng. Cùng số lượt gọi DeepSeek,
    chỉ đổi thời điểm.

    Kết quả nằm trong ``metadata.json`` của dự án, khoá ``youtube_meta``.
    """
    import services.youtube_meta as YM

    p = ADV.load_project(project_id, migrate=False)
    pdir, meta = p["dir"], p["meta"]
    if not (p.get("b_chunks") or []):
        raise RuntimeError(L("Chưa có bản dịch — chạy bước Dịch trước.", "There is no translation yet — run the Translate step first."))

    nguon = (cfg.get("meta_engine") or "ai").strip()
    if nguon == "manual":
        #  TỰ SOẠN — giống hệt "Tự dịch" ở bước Dịch: lô KHÔNG gọi AI, cũng không
        #  tính là lỗi. Đã dán kết quả rồi thì tính là XONG, không thì đánh dấu bỏ
        #  qua kèm câu chỉ đường; bảng mãi báo "chưa chạy" thì nhìn như còn việc.
        if ((meta.get("youtube_meta") or {}).get("tieu_de") or []):
            return L("đã dán sẵn (tự soạn)", "already pasted (manual composing)")
        raise _TuDich(
            L("bạn chọn tự soạn — bấm nút soạn tay ở cột này để chép prompt và dán JSON",
              "you chose manual composing — click the manual compose button in this column to copy the prompt and paste the JSON")
        )

    import services.deepseek_service as deepseek_service
    from services.ai_engines import provider_of, ten_bo_ai
    from services.config_service import get_config_service

    bo_ai = provider_of(nguon) or get_config_service().ai_provider_mac_dinh()
    if not deepseek_service.is_configured(bo_ai):
        raise RuntimeError(L(
            f"Chưa cấu hình API key {ten_bo_ai(bo_ai)} — vào Cài đặt nhập key, hoặc đổi nguồn "
            "soạn bài sang Tự soạn ở cấu hình lô.",
            f"The {ten_bo_ai(bo_ai)} API key is not configured — go to Settings to enter the key, or change the post composing "
            "source to Manual in the batch configuration."
        ))

    #  DÙNG CHUNG với nút "chép prompt" của chế độ tự soạn — xem ``_ngu_canh_soan``.
    nc = _ngu_canh_soan(p, cfg)
    #  MODEL PHẢI LẤY TỪ CẤU HÌNH BỘ AI, y như bước Dịch. Lô gọi THẲNG hàm nền
    #  nên không thừa hưởng mặc định của route: bỏ trống là gửi model rỗng lên
    #  DeepSeek, và từ khi họ siết kiểm tra thì cả lô trả 400 "but you passed .".
    model = get_config_service().ai_model_mac_dinh_cua(bo_ai)
    kq = await run_in_threadpool(YM.sinh_metadata, nc, model, "")
    if not (kq.get("tieu_de") or []):
        raise RuntimeError(L("AI không trả về tiêu đề nào — thử lại bước này.", "The AI returned no titles — retry this step."))

    cu_meta = meta.get("youtube_meta") or {}
    import time as _t
    meta["youtube_meta"] = {**cu_meta, **kq, "luc": _t.time()}
    ADV.save_meta(pdir, meta)
    ctx["ket_qua"] = {"tieu_de": (kq.get("tieu_de") or [""])[0]}
    return f"{len(kq.get('tieu_de') or [])} tiêu đề · {len(kq.get('the') or [])} thẻ"


def _phu_de_them(project_id: str, cfg: dict, batch_id: str = "") -> dict:
    """PHỤ ĐỀ THÊM THỨ TIẾNG của lô (``sub_langs``) — chạy ở ĐẦU bước xuất.

    Dịch các câu còn thiếu / cần dịch lại của từng thứ tiếng (DeepSeek hoặc Google; "manual"
    thì không tự dịch), rồi trả ``{"sub_lang", "srt_langs"}`` cho ``_dot_hinh``. Còn thiếu
    câu thì BÁO LỖI bước xuất — xuất phụ đề tiếng Nhật khuyết câu mà không ai biết là sai
    lặng lẽ; người dùng Dán kết quả / Dịch ở hộp xem video của lô rồi chạy lại bước này.
    """
    from services import review_advanced_ai as AI
    from services import review_sub_tracks as ST
    from services.config_service import get_config_service

    langs = [ST.chuan_ma(x) for x in (cfg.get("sub_langs") or []) if str(x or "").strip()]
    if not langs:
        return {}
    p = ADV.load_project(project_id, migrate=False)
    main = ST.chuan_ma(p["meta"].get("target_language") or "vi")
    langs = [lg for lg in dict.fromkeys(langs) if ST.ma_hop_le(lg, main)]
    if not langs:
        return {}
    from services.ai_engines import la_ai, provider_of, ten_bo_ai

    engine = (cfg.get("sub_engine") or "ai").strip()
    ctx = AI.read_film_context(p["dir"]) or {}
    opts = get_config_service().get_ai_script_options()
    model = ""
    if la_ai(engine):
        import services.deepseek_service as deepseek_service
        bo_ai = provider_of(engine) or get_config_service().ai_provider_mac_dinh()     # "ai" = theo Cài đặt
        if deepseek_service.is_configured(bo_ai):
            model = get_config_service().ai_model_mac_dinh_cua(bo_ai)
        else:
            logger.warning(f"[LÔ] Phụ đề thêm thứ tiếng: chưa có API key {ten_bo_ai(bo_ai)} -> dịch bằng Google.")
            engine = "google"
    thieu = []

    def _kiem_huy():
        #  Huỷ lô chỉ dừng được job ĐANG dịch (ST.huy_job); không kiểm ở đây thì vòng lặp
        #  sang thứ tiếng kế, ``bat_dau_job`` đặt lại cờ huỷ -> dịch trọn các thứ tiếng
        #  còn lại (tốn tiền DeepSeek). Ném lỗi lúc lô đang huỷ = vòng lô để "pending".
        if batch_id and BS.cancel_requested(batch_id):
            raise RuntimeError(L("Đã huỷ lô — dừng dịch phụ đề thêm thứ tiếng", "Batch cancelled — stopped translating extra-language subtitles"))

    for lg in langs:
        _kiem_huy()
        ST.them_thu_tieng(p["dir"], lg)
        if la_ai(engine) or engine == "google":
            viec = ST.can_dich(ST.doc(p["dir"]), lg, p["b_chunks"], "missing")
            if viec and not ST.bat_dau_job(project_id, lg, len(viec)):
                raise RuntimeError(L("Dự án đang có lượt dịch phụ đề khác (bấm ở trình dựng) — đợi "
                                     "xong rồi chạy lại bước Xuất.",
                                     "The project already has another subtitle translation running (started in the editor) — wait "
                                     "for it to finish and then run the Export step again."))
            if viec:
                try:
                    kq = ST.dich(project_id, p["dir"], p["meta"], lg, p["b_chunks"], p["a_lookup"],
                                 engine=engine, pham_vi="missing", model=model,
                                 batch_size=int(opts.get("batch_size") or 80),
                                 brief=ctx.get("text") or "")
                    logger.info(f"[LÔ] Phụ đề '{lg}' của {project_id}: {kq}")
                    ST._cap_nhat_job(project_id, running=False, result=kq)
                    _kiem_huy()
                except Exception as e:  # noqa: BLE001
                    ST._cap_nhat_job(project_id, running=False, error=str(e)[:300])
                    raise RuntimeError(L(f"Dịch phụ đề {ST.chuan_ma(lg)} lỗi: {e}", f"Subtitle translation {ST.chuan_ma(lg)} failed: {e}")) from e
        tt = {t["lang"]: t for t in ST.tinh_trang(ST.doc(p["dir"]), p["b_chunks"])}.get(lg) or {}
        if tt.get("missing"):
            thieu.append(L(f"{lg} còn {tt['missing']} câu", f"{lg} has {tt['missing']} missing"))
    if thieu:
        raise RuntimeError(L(
            "Phụ đề thêm thứ tiếng chưa đủ (" + ", ".join(thieu) + "). Bấm xem video của dự án "
            "→ \"Phụ đề thêm thứ tiếng\" để Dịch hoặc Copy prompt / Dán kết quả, rồi chạy lại "
            "bước Xuất.",
            "The extra-language subtitles are incomplete (" + ", ".join(thieu) + "). Open the project's video "
            "→ \"Extra-language subtitles\" to Translate or Copy prompt / Paste result, then run the "
            "Export step again."
        ))
    burn = ST.chuan_ma(cfg.get("sub_burn_lang") or "")
    burn = burn if burn in langs else ""
    hai = ST.chuan_ma(cfg.get("sub_second_lang") or "")
    if hai not in (main, *langs) or hai == (burn or main):
        hai = ""
    tat_ca = [main, *langs]
    return {
        "sub_lang": burn,
        "second_sub_lang": hai,
        "srt_langs": tat_ca if cfg.get("sub_srt") else [],
        "soft_langs": tat_ca if cfg.get("sub_soft") else [],
    }


def _chep_srt_kem(goc_mp4: str, chep_mp4: str, langs: list) -> None:
    """Chép các tệp ``<video>.<mã>.srt`` theo bản video đã chép về thư mục chung."""
    import os
    import shutil

    for lg in langs or []:
        src = os.path.splitext(goc_mp4)[0] + f".{lg}.srt"
        if os.path.exists(src):
            shutil.copy2(src, os.path.splitext(chep_mp4)[0] + f".{lg}.srt")


async def _buoc_export(project_id: str, cfg: dict, ctx: dict) -> str:
    import os

    lo = str(ctx.get("batch_id") or "")
    phu_de = await run_in_threadpool(_phu_de_them, project_id, cfg, lo)
    await _ap_am_thanh_cua_lo(project_id, cfg)
    #  Bấm Huỷ lô giữa lúc dịch phụ đề -> cờ huỷ dựng do ``cancel_batch`` đặt KHÔNG được
    #  xoá ở dòng dưới, không thì lượt dựng nặng vẫn chạy.
    if lo and BS.cancel_requested(lo):
        raise RuntimeError(L("Đã huỷ lô", "Batch cancelled"))
    review_service.clear_render_cancel(project_id)
    review_service.mark_render_started(project_id)
    await run_in_threadpool(_render_wrapper, project_id)
    p = ADV.load_project(project_id, migrate=False)
    state = (p["meta"].get("state") or {})
    if state.get("render_error"):
        raise RuntimeError(str(state["render_error"])[:300])
    if not state.get("render_complete"):
        raise RuntimeError(L("Dựng xong nhưng dự án không báo hoàn tất", "Rendering finished but the project did not report completion"))

    goc = os.path.join(p["dir"], "final_review.mp4")
    if not os.path.exists(goc):
        raise RuntimeError(L("Dự án báo dựng xong nhưng không thấy tệp video", "The project reported rendering done but the video file was not found"))

    #  BƯỚC 2/2 — ĐỐT HÌNH. Bấm "Xuất video" trong trình dựng là chạy HAI bước:
    #  ghép tiếng (``render``) rồi đốt phụ đề / vùng che sub / logo / watermark /
    #  khung lên hình (``export-compositor``). Lô trước đây dừng ở bước 1 nên bản
    #  xuất của lô KHÁC hẳn bản xuất trong dự án — không có chữ, không có logo —
    #  mà nhìn tên tệp thì không ai biết là thiếu.
    #
    #  Gọi THẲNG hàm mà endpoint kia dùng: cùng một đường, không có bản chép tay
    #  thứ hai để lệch nhau.
    #  ``phu_de`` truyền THEO TÊN và chỉ khi có — giữ nguyên dáng gọi cũ cho lô thường.
    goc = await run_in_threadpool(_dot_hinh, project_id, p["meta"],
                                  **({"phu_de": phu_de} if phu_de else {}))

    duong = goc
    chep = ""
    try:
        chep = await run_in_threadpool(
            _chep_ban_xuat, goc, (cfg.get("export_dir") or "").strip(),
            f"{TIEN_TO_XUAT} {_ten_du_an_sach(p['meta'], project_id)}",
        )
        if chep:
            duong = chep
            await run_in_threadpool(_chep_srt_kem, goc, chep, (phu_de or {}).get("srt_langs"))
    except Exception as e:  # noqa: BLE001 — chép hỏng KHÔNG được tính là xuất hỏng
        logger.warning(f"[LÔ] Không chép được bản xuất của {project_id}: {e}")

    mb = round(os.path.getsize(duong) / (1024 * 1024), 1)
    #  VÂN TAY BẢN DỰNG lúc vừa xuất xong — bước Xuất đoạn so lại để biết có cắt thẳng
    #  từ bản này được không (xem services/cat_tu_ban_xuat.py).
    from routes.review_routes import _review_compositor_config_path

    try:
        van_tay = CUT.van_tay_ban_dung(p["dir"], _review_compositor_config_path(project_id))
    except Exception:  # noqa: BLE001 — không có vân tay thì Xuất đoạn tự dựng lại, không sao
        van_tay = ""
    ctx["ket_qua"] = {"path": duong.replace("\\", "/"), "mb": mb,
                      "trong_du_an": goc.replace("\\", "/"), "van_tay": van_tay}
    return f"{mb} MB" + (L(" — đã chép vào thư mục chung", " — copied to the shared folder") if chep else "")


async def _buoc_clips(project_id: str, cfg: dict, ctx: dict) -> str:
    """XUẤT CÁC ĐOẠN ĐÃ CHỌN — mỗi đoạn một video riêng, y như "Xuất N đoạn" của trình
    dựng: lặp lại lượt xuất MỘT đoạn (dựng tiếng theo đoạn -> đốt hình theo đoạn) cho
    từng đoạn, lần lượt. Không đẻ đường xuất thứ hai: lượt một-đoạn đã được kiểm tới
    từng khung hình (xem export-selected-ranges).

    Danh sách đoạn đọc từ ``export_ranges.json`` — chung với trình dựng, nên khoanh ở
    popup của lô hay trên thước của trình dựng đều ra cùng một kết quả.
    """
    import os

    from services import export_ranges as ER

    p = ADV.load_project(project_id, migrate=False)
    doan = ER.doc(p["dir"])
    if not doan:
        raise _TuDich(L("chưa khoanh đoạn nào — bấm \"Chọn đoạn\" để khoanh rồi chạy lại", "no segment selected — click \"Select segments\" to select them and run again"))

    #  Đoạn đốt CÙNG thứ tiếng phụ đề với bản xuất cả phim (không kèm .srt từng đoạn).
    phu_de = await run_in_threadpool(_phu_de_them, project_id, cfg)
    phu_de_doan = {k: phu_de.get(k) for k in ("sub_lang", "second_sub_lang") if phu_de.get(k)} or None
    await _ap_am_thanh_cua_lo(project_id, cfg)
    ten = _ten_du_an_sach(p["meta"], project_id)
    thu_muc = (cfg.get("export_dir") or "").strip()

    #  CẮT TỪ BẢN "XUẤT VIDEO" nếu nó còn KHỚP dự án (vân tay — xem cat_tu_ban_xuat):
    #  một lượt mã hoá quãng ngắn thay cho dựng tiếng + đốt hình từng đoạn.
    ban_dai, ly_do_dung = _ban_dai_con_moi(project_id, p, ctx)
    tong_dai = CUT.thoi_luong(ban_dai) if ban_dai else 0.0
    if ban_dai and tong_dai <= 0:
        ban_dai, ly_do_dung = "", L("không đọc được độ dài bản Xuất video", "cannot read the length of the exported video")
    BS.them_log(ctx.get("batch_id") or "", [
        L("Xuất đoạn: cắt thẳng từ bản Xuất video (khớp dự án) — không dựng lại",
          "Export segments: cut straight from the exported video (matches the project) — no re-render") if ban_dai
        else L(f"Xuất đoạn: dựng từng đoạn ({ly_do_dung})", f"Export segments: rendering each segment ({ly_do_dung})")
    ], project_id, "clips") if ctx.get("batch_id") else None

    ra: list = []
    for i, r in enumerate(doan, start=1):
        #  Huỷ phải dừng ĐƯỢC giữa các đoạn — 10 đoạn là 10 lượt dựng + đốt.
        if BS.cancel_requested(ctx.get("batch_id") or "") or review_service.is_render_cancelled(project_id):
            raise RuntimeError(f"Đã huỷ sau {i - 1}/{len(doan)} đoạn")
        #  Tiền tố "[ĐOẠN n]" ở ĐẦU tên, như trình dựng.
        tien_to = tien_to_doan(i)
        if ban_dai:
            #  Cùng khoảng đệm hai đầu như đường dựng theo đoạn (TS.gom_khoang).
            from utils import timeline_slice as TS

            (a, b), = TS.gom_khoang([r], tong_dai=tong_dai) or [(r["start"], r["end"])]
            duong = os.path.join(p["dir"], f"{tien_to} {ten}.mp4")
            with WQ.slot(project_id, "Cắt đoạn (lô)",
                         cancelled=lambda: review_service.is_render_cancelled(project_id)):
                await run_in_threadpool(CUT.cat_doan, ban_dai, a, b, duong)
            #  TÍNH LƯỢT: mỗi đoạn là MỘT video xuất ra, như đường dựng lại bên dưới (đi qua
            #  ``_render_wrapper`` -> record_completion). Thiếu dòng này thì cùng 5 đoạn mà
            #  cắt nhanh tính 0, dựng lại tính 5 — và hạn mức lô bị đi cửa sau. Lô đang giữ
            #  dự án nên record_completion tự ghi vào phần "do lô" (videos_lo).
            try:
                from services.usage_reporter import record_completion

                record_completion(str(project_id), "review")
            except Exception as e:  # noqa: BLE001 — ghi thống kê hỏng không được làm hỏng lượt xuất
                logger.warning(f"[LÔ] Không ghi được lượt xuất đoạn {i} của {project_id}: {e}")
        else:
            review_service.clear_render_cancel(project_id)
            review_service.mark_render_started(project_id)
            await run_in_threadpool(_render_wrapper, project_id, [r])
            st = (ADV.load_project(project_id, migrate=False)["meta"].get("state") or {})
            if st.get("render_error"):
                raise RuntimeError(f"Đoạn {i}/{len(doan)}: " + str(st["render_error"])[:300])
            if not st.get("render_complete"):
                raise RuntimeError(f"Đoạn {i}/{len(doan)}: dựng xong nhưng dự án không báo hoàn tất")
            duong = await run_in_threadpool(_dot_hinh, project_id, p["meta"], [r], tien_to,
                                            **({"phu_de": phu_de_doan} if phu_de_doan else {}))
        try:
            chep = await run_in_threadpool(_chep_ban_xuat, duong, thu_muc, f"{tien_to} {ten}")
            if chep:
                duong = chep
        except Exception as e:  # noqa: BLE001 — chép hỏng KHÔNG tính là xuất hỏng
            logger.warning(f"[LÔ] Không chép được đoạn {i} của {project_id}: {e}")
        ra.append({"path": duong.replace("\\", "/"), "start": r["start"], "end": r["end"],
                   "mb": round(os.path.getsize(duong) / (1024 * 1024), 1)})
    tong = round(sum(x["mb"] for x in ra), 1)
    #  ``path`` = tệp đầu tiên — để các chỗ xem lại / mở thư mục dùng chung kiểu dữ liệu
    #  với bước "export".
    noi_luu = thu_muc or p["dir"]
    ctx["ket_qua"] = {"path": ra[0]["path"], "mb": tong, "doan": ra,
                      "thu_muc": noi_luu.replace("\\", "/"), "cat_tu_ban_dai": bool(ban_dai)}
    #  NÓI RÕ TỆP NẰM ĐÂU — người dùng chạy lô xong không biết các đoạn ra chỗ nào (lô
    #  không hỏi nơi lưu như trình dựng). Bấm ô "Xong" để xem danh sách + mở thư mục.
    return (L(f"{len(ra)} đoạn — {tong} MB", f"{len(ra)} {'segment' if len(ra) == 1 else 'segments'} — {tong} MB")
            + (L(" — cắt từ bản Xuất video", " — cut from the exported video") if ban_dai else "")
            + L(f" — lưu ở {noi_luu}", f" — saved at {noi_luu}"))


def _ban_dai_con_moi(project_id: str, p: dict, ctx: dict) -> tuple:
    """(đường dẫn bản Xuất video còn khớp dự án | "", lý do phải dựng lại)."""
    from routes.review_routes import _review_compositor_config_path

    ex = ((ctx.get("item") or {}).get("data") or {}).get("export") or {}
    duong = (ex.get("trong_du_an") or "").replace("/", os.sep)
    if not duong or not ex.get("van_tay"):
        return "", L("chưa có bản Xuất video của lô", "the batch has no exported video yet")
    if not os.path.exists(duong):
        return "", L("bản Xuất video đã bị xoá / di chuyển", "the exported video was deleted / moved")
    if CUT.van_tay_ban_dung(p["dir"], _review_compositor_config_path(project_id)) != ex["van_tay"]:
        return "", L("dự án đã sửa sau lần Xuất video — bản dài đã cũ", "the project was edited after the video export — the long version is outdated")
    return duong, ""


_BUOC = {
    "sub": _buoc_sub,
    "translate": _buoc_translate,
    "meta": _buoc_meta,
    "separate": _buoc_separate,
    "voice": _buoc_voice,
    "fit": _buoc_fit,
    "export": _buoc_export,
    "clips": _buoc_clips,
}


def _gui_so_lieu_ngam() -> None:
    """Đẩy số liệu sử dụng lên máy chủ ở luồng nền, không chờ kết quả.

    ``usage_reporter`` gửi theo nhịp 30 phút. Đủ cho việc thống kê, nhưng KHÔNG đủ
    cho thanh hạn mức: chạy xong một lô 40 video mà mức dùng trên màn hình đứng yên
    nửa tiếng thì người dùng tưởng nó không đếm — rồi bất ngờ bị khoá.
    """
    import threading

    def _chay():
        try:
            from services import usage_reporter

            usage_reporter.flush()
        except Exception as e:  # noqa: BLE001 — gửi hỏng thì nhịp 30 phút vẫn gửi lại
            logger.info(f"[LÔ] Chưa gửi được số liệu ngay sau lượt chạy: {e}")

    threading.Thread(target=_chay, daemon=True).start()


# ============================================================ BỘ CHẠY
async def _chay_lo(batch_id: str, step: str, force: bool, only: Optional[list] = None,
                   trong_chuoi: bool = False) -> bool:
    """Chạy MỘT bước cho cả lô, lần lượt từng dự án.

    ``only``        = chỉ chạy đúng những dự án này (nút chạy lại một dòng).
    ``trong_chuoi`` = đang là một mắt trong chuỗi nhiều bước -> KHÔNG nhả chỗ trong
                      ``_running``: nhả ra giữa chuỗi thì một request "chạy bước
                      khác" lọt vào được, hai lượt cùng ghi lên một lô.

    Trả về True nếu lượt này bị HUỶ — nơi gọi (chuỗi) dựa vào đó để dừng hẳn, vì
    ``end_run`` xoá cờ huỷ ngay sau đó nên đọc lại từ đĩa là không còn thấy gì.
    """
    bi_huy = False
    batch = BS.load(batch_id)
    if not batch:
        return
    todo = BS.pending_for(batch, step, force=force)
    if only:
        todo = [p for p in todo if p in set(only)]
    logger.info(f"[LÔ] Bắt đầu bước '{step}' cho {len(todo)} dự án của lô {batch_id}")
    _BUOC_DANG_CHAY[batch_id] = step
    try:
        for pid in todo:
            #  Đọc lại cờ huỷ TỪ ĐĨA (bản trong bộ nhớ không bao giờ thấy cờ do
            #  request khác đặt). Huỷ chỉ chặn dự án KẾ TIẾP — dự án đang chạy vẫn
            #  dừng bằng nút Huỷ sẵn có trong chính dự án đó.
            if BS.cancel_requested(batch_id):
                logger.info(f"[LÔ] Đã huỷ theo yêu cầu — dừng trước dự án {pid}")
                bi_huy = True
                break

            #  CHẠM TRẦN HẠN MỨC GIỮA CHỪNG -> dừng lô ở đây.
            #
            #  VÌ SAO PHẢI KIỂM Ở ĐÂY: cửa chặn ``_chan_neu_khong_co_quyen`` là một
            #  ràng buộc của ROUTER, nên nó chỉ chạy khi có REQUEST MỚI. Vòng này
            #  chạy trong tác vụ nền, không đi qua router nữa — lô đã bấm chạy sẽ
            #  chạy hết 50 video dù trần chỉ còn 3. Người dùng không hề biết, và
            #  admin thì thấy số liệu vượt trần mà không có gì chặn.
            #
            #  Đọc từ CACHE xác thực (không gọi mạng thêm), nên chạm trần được nhận
            #  ra trong vòng một chu kỳ cache. Chấp nhận trễ đó: gọi máy chủ trước
            #  mỗi video là mỗi lô vài chục lượt gọi, mà số liệu dùng cũng chỉ được
            #  gửi lên mỗi 30 phút nên hỏi dày hơn cũng không chính xác hơn.
            q = quyen_hien_tai()
            #  Lô "Làm nhanh" (routes/nhanh_routes.py) chạy MỘT video mỗi lượt — tương đương người
            #  dùng tự bấm từng bước trong Chi tiết, nên không tính vào hạn mức chạy hàng loạt.
            if not q.get("allow_batch", True) and not batch.get("nhanh"):
                ly_do = q.get("quota_message") or L("Gói hiện tại đã hết hạn mức chạy hàng loạt.", "Plan batch quota used up.")
                logger.warning(f"[LÔ] Dừng lô {batch_id}: {ly_do}")
                BS.them_log(batch_id, [L(f"■ Dừng lô — {ly_do}", f"■ Stopped — {ly_do}")], "", step)
                bi_huy = True
                break

            batch = BS.load(batch_id) or batch

            #  NGƯỜI DÙNG CÓ THỂ ĐÃ TỰ LÀM bước này trong lúc lô đang chạy (mở dự án
            #  chưa tới lượt ra làm tay). Kiểm lại NGAY TRƯỚC KHI chạy: đã có kết quả
            #  thì để yên, không chạy đè lên công của họ. Bỏ qua kiểm khi người dùng
            #  CỐ Ý ép chạy lại (force).
            if not force:
                #  Gọi THẲNG, không qua luồng phụ: đây chỉ là vài lượt đọc tệp nhỏ,
                #  mà đưa vào luồng phụ thì mỗi lượt lại phải nhường quyền điều khiển
                #  — ở môi trường không giữ vòng lặp chạy liên tục (test), task nền
                #  nhường xong là không bao giờ được chạy tiếp, cả lô đứng hình.
                da_co = _suy_trang_thai(pid)
                if da_co.get(step) == "done":
                    BS.set_step(batch, pid, step, "done",
                                L("bạn đã tự làm xong trong lúc lô chạy", "you already finished this yourself while the batch was running"))
                    continue

            BS.mark_current(batch, pid)
            BS.set_step(batch, pid, step, "running")

            #  Dự án đang bận (người dùng mở ra chạy việc khác) -> KHÔNG phải lỗi:
            #  để yên, đánh dấu "bận", lần chạy sau tự nhặt lại. Báo đỏ ở đây thì
            #  người dùng tưởng hỏng, mà thật ra chỉ là hai bên cùng đụng một dự án.
            if not _acquire_review(pid):
                BS.set_step(batch, pid, step, "busy",
                            L("Bạn đang chạy việc khác trong dự án này — lô bỏ qua, "
                              "bấm chạy lại khi xong",
                              "You are running something else in this project — the batch skipped it, "
                              "click run again when it is done"))
                continue
            try:
                #  ``item``: bước cần biết cấu hình RIÊNG của video này (vd giọng
                #  riêng) — không có thì mọi video buộc phải dùng chung một giọng.
                ctx: dict = {
                    "item": BS.find_item(batch, pid) or {},
                    #  Bước nào cần RÓT THẲNG vào nhật ký lô (vd bước Dồn nhờ
                    #  DeepSeek rút gọn) thì phải biết mình đang ở lô nào.
                    "batch_id": batch_id,
                }
                #  GHI SỔ "lô đang giữ dự án này": trình dựng đọc sổ để CHẶN SỬA
                #  trong lúc lô chạy. Không chặn thì người dùng sửa xong, lô ghi
                #  lại kết quả của nó -> mất trắng công sửa mà không có lỗi nào.
                BL.dat(pid, batch_id, step, (ctx["item"] or {}).get("title") or "")
                #  Bám nhật ký chi tiết của dự án trong SUỐT lúc bước này chạy.
                with _BamNhatKy(batch_id, pid, step):
                    ghi_chu = await _BUOC[step](pid, batch.get("config") or {}, ctx)
                batch = BS.load(batch_id) or batch
                BS.set_step(batch, pid, step, "done", ghi_chu or "")
                #  Kết quả kèm theo (vd đường dẫn bản xuất) — giao diện dựa vào đây
                #  để mở xem / mở thư mục, khỏi bắt người dùng tự đi tìm tệp.
                if ctx.get("ket_qua"):
                    item = BS.find_item(batch, pid)
                    if item is not None:
                        item.setdefault("data", {})[step] = ctx["ket_qua"]
                        BS.save(batch)
            except _TuDich as e:
                batch = BS.load(batch_id) or batch
                BS.set_step(batch, pid, step, "skipped", str(e))
            except _CanNguoiQuyet as e:
                batch = BS.load(batch_id) or batch
                BS.set_step(batch, pid, step, "needs_me", str(e))
                item = BS.find_item(batch, pid)
                if item is not None:
                    item.setdefault("data", {})[step] = e.data
                    BS.save(batch)
            except Exception as e:  # noqa: BLE001 — một dự án hỏng không được chặn cả lô
                batch = BS.load(batch_id) or batch
                if BS.cancel_requested(batch_id):
                    #  Không phải dự án hỏng — là người dùng bấm Huỷ giữa lúc nó chạy.
                    #  Để "pending" cho lần chạy sau làm tiếp; tô đỏ "LỖI" ở đây là
                    #  người dùng tưởng video hỏng thật.
                    logger.info(f"[LÔ] {pid} bị huỷ giữa bước {step}")
                    BS.set_step(batch, pid, step, "pending", "bị huỷ giữa chừng — chạy lại được")
                    bi_huy = True
                    break
                logger.warning(f"[LÔ] {pid} lỗi ở bước {step}: {e}", exc_info=True)
                BS.set_step(batch, pid, step, "failed", str(e))
            finally:
                BL.xoa(pid)
                _release_review(pid)
    except Exception as e:  # noqa: BLE001
        #  Bộ chạy là một task nền: ngoại lệ lọt ra đây KHÔNG ai thấy (asyncio nuốt
        #  im lặng) — lô chỉ đơn giản đứng hình, mọi dự án còn "chưa chạy" và người
        #  dùng không biết vì sao. Ghi lại rõ ràng, và đánh dấu dự án đang dở.
        logger.error(f"[LÔ] Lượt chạy '{step}' của lô {batch_id} chết giữa chừng: {e}",
                     exc_info=True)
        hien = BS.load(batch_id)
        dang = ((hien or {}).get("run") or {}).get("project_id") or ""
        if hien and dang:
            BS.set_step(hien, dang, step, "failed", f"Lượt chạy gặp lỗi: {e}")
    finally:
        #  Dọn sạch sổ của lô này: đường thoát bất thường (lỗi, huỷ) có thể bỏ sót
        #  ``BL.xoa``, mà bỏ sót thì dự án bị khoá sửa cho tới lúc khởi động lại.
        BL.don_lo(batch_id)
        batch = BS.load(batch_id)
        if batch:
            bi_huy = bi_huy or bool((batch.get("run") or {}).get("cancel"))
            BS.end_run(batch)
        if not trong_chuoi:
            _running.pop(batch_id, None)
            #  GỬI SỐ LIỆU NGAY sau khi xong một lượt, đừng đợi nhịp 30 phút: thanh
            #  hạn mức trên màn hình mới nói đúng "còn bao nhiêu". Chạy ở luồng riêng
            #  vì nó gọi mạng (tối đa 15s) — không được giữ chân người dùng.
            _gui_so_lieu_ngam()
        #  Trong chuỗi thì bước kế sẽ đặt lại ngay; gỡ ở đây để không báo "đang chạy"
        #  cho một bước đã xong khi chuỗi dừng vì huỷ / hết việc.
        if _BUOC_DANG_CHAY.get(batch_id) == step:
            _BUOC_DANG_CHAY.pop(batch_id, None)
        logger.info(f"[LÔ] Xong bước '{step}' của lô {batch_id}")
    return bi_huy


# ============================================================ ENDPOINT
class BatchCreate(BaseModel):
    title: str = ""


class ItemsRequest(BaseModel):
    project_ids: List[str] = []


class VideosRequest(BaseModel):
    """Đường dẫn video TRÊN MÁY (không tải lên qua HTTP — tệp video hàng GB đẩy
    qua multipart thì chờ rất lâu và tốn gấp đôi dung lượng đĩa)."""

    paths: List[str] = []


def _loc_da_chon(b: dict, project_ids) -> Optional[set]:
    """Bộ id người dùng đã tick, hoặc ``None`` nghĩa là CẢ LÔ.

    Trả ``None`` khi danh sách rỗng — không tick gì thì chạy cả lô như nếp cũ.

    Id không có trong lô bị LOẠI, và nếu loại xong chẳng còn id nào thì BÁO LỖI chứ
    không lặng lẽ trả ``None``: "rỗng" và "chọn toàn thứ không tồn tại" mà gộp làm một
    thì người dùng tick 3 video của lô cũ rồi bấm chạy sẽ thấy CẢ LÔ chạy — đúng cái họ
    không muốn, mà không có dấu hiệu gì.
    """
    if not project_ids:
        return None
    co = {it.get("project_id") for it in (b.get("items") or [])}
    ra = {p for p in project_ids if p in co}
    if not ra:
        raise HTTPException(
            status_code=400,
            detail=L("Những video đã chọn không còn trong lô này — tải lại trang rồi chọn lại.", "The selected videos are no longer in this batch — reload the page and select again."),
        )
    return ra


class RunRequest(BaseModel):
    step: str
    force: bool = False
    #  Chi chay dung nhung du an nay (nguoi dung tick o cot dau bang). RONG = ca lo, giu
    #  nguyen nep cu — dung de ai khong tick gi van bam chay duoc nhu truoc.
    project_ids: List[str] = []


class FitApply(BaseModel):
    voice_speed: float = 1.0
    video_speed: float = 1.0
    #  "deu" = tang toc dong loat + giam toc phim; "cuc_bo" = chi tang toc cac cau bi dai.
    kind: str = "deu"


#: Gửi ra giao diện TỐI ĐA ngần này dòng nhật ký (dòng MỚI NHẤT).
#:
#: Nhật ký giữ tới 5000 dòng trên đĩa (``BS.LOG_TOI_DA``), trong khi trang lô hỏi lại
#: cả gói tin MỖI 2 GIÂY lúc đang chạy. Gửi cả 5000 dòng là ~1,5MB mỗi lượt hỏi, và
#: giao diện vẽ lại từng ấy dòng — đúng lúc máy đang bận chạy lô. Người dùng chỉ đọc
#: phần đuôi (khung tự cuộn xuống đáy), nên gửi đuôi là đủ; muốn xem hết thì mở tệp
#: nhật ký của lô.
LOG_GUI_TOI_DA = 400


def _dang_chay_that(batch_id: str) -> str:
    """Bước lô này ĐANG CHẠY THẬT ("" nếu không): tác vụ asyncio còn sống + sổ bước."""
    t = _running.get(batch_id)
    if t is None or getattr(t, "done", lambda: True)():
        return ""
    return _BUOC_DANG_CHAY.get(batch_id, "") or ""


def _view(batch: dict) -> dict:
    """Bản gửi cho giao diện: kèm số đếm từng bước để vẽ thanh tiến độ."""
    nk = batch.get("log") or []
    #  ``run`` trong tệp có thể trống một khoảnh khắc (khe hở giữa hai bước của chuỗi)
    #  hoặc bị lượt ghi khác chạm vào, trong khi tác vụ của lô vẫn đang chạy. Giao diện
    #  từng đọc đúng khoảnh khắc đó, tắt vòng hỏi và đứng ở "rảnh" suốt buổi dù nhật ký
    #  vẫn nhảy. Nên bù lại từ sổ tác vụ: còn sống thì ``run.step`` không được trống.
    buoc_that = _dang_chay_that(batch.get("id", ""))
    run = dict(batch.get("run") or {})
    if buoc_that and not run.get("step"):
        run["step"] = buoc_that

    #  MẤT VIDEO GỐC — tính vào bản gửi đi, KHÔNG ghi xuống tệp lô: đây là sự thật
    #  của ổ đĩa lúc này, chép vào tệp thì lần sau đọc lên có thể đã sai (người
    #  dùng cắm lại ổ ngoài, khôi phục từ thùng rác). Tính mỗi lượt hỏi, nhưng
    #  ``_mat_video`` có sổ nhớ 20 giây nên không đụng đĩa mỗi 2 giây.
    items = []
    for it in batch.get("items") or []:
        mat, duong = _mat_video(it["project_id"])
        #  Số đoạn đã khoanh cho bước "Xuất các đoạn đã chọn" — nút "Chọn đoạn" hiện
        #  số này. Có sổ nhớ trong ER (cập nhật ngay khi ghi) nên không đọc đĩa mỗi nhịp.
        so_doan = ER.dem(ADV.project_dir_of(it["project_id"]))
        items.append({**it, "mat_video": mat, "video_path": duong, "so_doan": so_doan})

    return {
        **batch,
        "items": items,
        "run": run,
        "dang_chay_that": bool(buoc_that),
        #  CHỈ ĐUÔI — xem ``LOG_GUI_TOI_DA``.
        "log": nk[-LOG_GUI_TOI_DA:],
        "log_total": len(nk),
        "steps": list(BS.STEPS),
        "step_labels": BS.STEP_LABEL,
        "progress": {s: BS.counts(batch, s) for s in BS.STEPS},
    }


@router.post("")
async def create_batch(data: BatchCreate = Body(default=BatchCreate())):
    return _view(BS.create(data.title))


@router.get("")
async def list_batches():
    return {"batches": [
        {"id": b["id"], "title": b["title"], "created_at": b["created_at"],
         "count": len(b["items"]), "running": BS.is_running(b),
         #  Dự án ĐẦU TIÊN của lô — Trang chủ lấy ảnh đại diện của nó làm ảnh bìa lô (khung "Chạy hàng loạt"). Rỗng nếu lô chưa có dự án.
         "thumb_pid": str(b["items"][0].get("project_id") or "") if b["items"] else "",
         #  TỔNG ĐỘ DÀI các video trong lô (giây) — cộng từ số đã nhớ lúc thêm vào lô (``giay``), không đo lại; video chưa có số tính 0.
         "tong_giay": round(sum(float(i.get("giay") or 0) for i in b["items"]), 1),
         "progress": {s: BS.counts(b, s) for s in BS.STEPS}}
        for b in BS.list_all()
    ]}


@router.get("/queue/status")
async def queue_status():
    """Hàng đợi việc nặng toàn máy — giao diện hiện "đang xếp hàng sau N việc".

    Đặt trong nhóm /api/batch cho gọn, nhưng nội dung là TOÀN MÁY chứ không riêng lô.
    Phải khai báo TRƯỚC "/{batch_id}" — không thì "queue" bị hiểu là mã lô.

    Kèm luôn LÔ ĐANG CHẠY để chỉ báo nổi ở góc màn hình chỉ cần MỘT lượt gọi: nó
    hiện trên mọi trang, gọi hai ba endpoint mỗi vài giây là phí.
    """
    out = WQ.trang_thai()
    lo = None
    for b in BS.list_all():
        if BS.is_running(b):
            step = (b.get("run") or {}).get("step") or ""
            dem = BS.counts(b, step) if step else {}
            dang_pid = (b.get("run") or {}).get("project_id") or ""
            muc = BS.find_item(b, dang_pid) if dang_pid else None
            lo = {
                "id": b["id"],
                "title": b["title"],
                "step": step,
                "step_label": BS.STEP_LABEL.get(step, step),
                "xong": int(dem.get("done", 0)) + int(dem.get("skipped", 0)),
                "tong": len(b.get("items") or []),
                "dang_lam": (muc or {}).get("title", ""),
                #  Lô ẩn của chế độ Làm nhanh: bấm thanh tiến độ thì mở LÀM NHANH, không nhảy sang trang Chạy hàng loạt.
                "nhanh": bool(b.get("nhanh")),
            }
            break
    out["lo"] = lo
    #  DỰ ÁN NÀO ĐANG BỊ LÔ GIỮ — trình dựng đọc để hiện dải cảnh báo "đừng sửa"
    #  và khoá các nút nặng, thay vì để người dùng bấm rồi mới ăn lỗi 409.
    out["giu"] = BL.tat_ca()
    return out


@router.get("/{batch_id}")
async def get_batch(batch_id: str):
    return _view(_get(batch_id))


@router.post("/{batch_id}/refresh")
async def refresh_batch(batch_id: str):
    """Đọc lại hiện trạng từng dự án -> vá các bước còn "chưa chạy".

    Dùng khi người dùng vừa vào dự án làm tay xong quay ra: bảng phải biết là việc
    đó đã xong, không thì lượt chạy hàng loạt kế tiếp làm lại.
    """
    b = _get(batch_id)
    #  LÔ ĐANG CHẠY VẪN LÀM MỚI ĐƯỢC. Cách dùng thật: lô đang lấy phụ đề / tách giọng
    #  cho 25 video, người dùng tranh thủ dán bản dịch tay cho những video đã xong phụ
    #  đề. Trước đây trả 409 nên ô "Dịch" của họ vẫn "Chưa chạy", mở lại modal dịch tay
    #  vẫn thấy chưa nhập — dù vào dự án thì bản dịch đã có. Chỉ né đúng dự án lô đang
    #  cầm (kết quả sắp do lô ghi), còn lại đọc hiện trạng như bình thường.
    dang_cam = {pid for pid in (BL.tat_ca() or {}).keys()} if BS.is_running(b) else set()

    #  VÁ TRẠNG THÁI NỬA VỜI: dự án có stem trên đĩa mà cờ ``separate_audio`` vẫn
    #  tắt (do bản cũ của bước Tách giọng để lại) thì hai mức "Giọng gốc" bị bỏ qua
    #  âm thầm — kéo về 0 mà xuất ra vẫn còn nguyên giọng gốc. Vá ngay ở đây vì
    #  "Làm mới" đúng nghĩa là đọc lại hiện trạng cho khớp.
    for it in b.get("items") or []:
        if it["project_id"] in dang_cam:
            continue
        #  Lô cũ chưa có hai con số này (hoặc dự án vừa được thay video) -> đọc lại.
        if not it.get("giay") or not it.get("mb"):
            tt = await run_in_threadpool(_thong_tin_du_an, it["project_id"])
            it["giay"] = it.get("giay") or tt["giay"]
            it["mb"] = it.get("mb") or tt["mb"]
        try:
            pdir = ADV.project_dir_of(it["project_id"])
            if _co_stem(pdir):
                await run_in_threadpool(_bat_co_tach, it["project_id"])
        except Exception:  # noqa: BLE001 — dự án hỏng thì bỏ qua, không chặn làm mới
            pass

    if dang_cam:
        #  Không ghi đè cả tệp khi lô đang chạy — chỉ vá trạng thái (bên dưới ghi vào
        #  bản mới nhất trên đĩa). Số liệu giây/MB đọc thêm ở trên sẽ vào ở lần làm
        #  mới sau khi lô xong.
        return _view(await _dong_bo_trang_thai(
            b, bo_qua=dang_cam, ghi_chu=L("bạn đã tự làm trong lúc lô chạy", "you already did this yourself while the batch was running")))
    BS.save(b)
    return _view(await _dong_bo_trang_thai(b))


@router.delete("/{batch_id}")
async def delete_batch(batch_id: str):
    b = _get(batch_id)
    if BS.is_running(b):
        raise HTTPException(status_code=409, detail=L("Lô đang chạy — hãy Huỷ trước.", "The batch is running — cancel it first."))
    BS.delete(batch_id)
    return {"success": True}


@router.get("/{batch_id}/items/{project_id}/prompt")
async def prompt_theo_lo(batch_id: str, project_id: str):
    """Prompt dịch tay của MỘT video, dựng theo ĐÚNG cấu hình của lô.

    VÌ SAO KHÔNG GỌI THẲNG ``/api/review-advanced/projects/{id}/prompt``: cửa đó
    dựng prompt từ metadata DỰ ÁN. Trang lô đặt "dịch sang tiếng Nhật, giọng nam,
    văn phong X" ở cấu hình LÔ, mà những thứ đó chỉ được ghi xuống dự án khi bước
    Dịch chạy — nguồn dịch là "Tự dịch" thì bước đó không bao giờ chạy. Kết quả:
    người dùng copy prompt ra thấy nó bảo dịch sang tiếng Việt bằng giọng cũ của
    dự án, không khớp gì với cấu hình họ vừa đặt.

    Ở đây áp cấu hình lô xuống metadata TRƯỚC (đúng hàm mà bước Dịch tự động dùng),
    rồi mới dựng prompt — nên hai đường không thể lệch nhau nữa.
    """
    b = _get(batch_id)
    item = BS.find_item(b, project_id)
    if not item:
        raise HTTPException(status_code=404, detail=L("Video không nằm trong lô này.", "This video is not in this batch."))

    p = await run_in_threadpool(ADV.load_project, project_id, False)
    if await run_in_threadpool(_ap_cau_hinh_dich, p, b.get("config") or {}, item):
        await run_in_threadpool(ADV.save_meta, p["dir"], p["meta"])

    #  Gọi lại ĐÚNG cửa của chế độ Chi tiết: prompt chỉ được dựng ở một chỗ.
    from routes.review_advanced_routes import get_script_prompt

    return await get_script_prompt(project_id, kind="dubbing")


def _ngu_canh_soan(p: dict, cfg: dict) -> dict:
    """Ngữ cảnh dựng prompt soạn bài — gọi thẳng hàm dùng chung ở service.

    Giữ lại lớp vỏ này vì lô còn truyền thêm ngôn ngữ mặc định của lô cho những dự
    án chưa đặt ngôn ngữ riêng.
    """
    import services.youtube_meta as YM

    return YM.ngu_canh_tu_du_an(p, (cfg or {}).get("target_language") or "vi")


@router.get("/{batch_id}/items/{project_id}/prompt-soan")
async def prompt_soan_bai(batch_id: str, project_id: str):
    """Prompt SOẠN BÀI ĐĂNG của một video — để người dùng chép sang AI ngoài.

    Giống hệt nút "Copy prompt" của bước Dịch: một bản prompt duy nhất, dựng ở
    backend, nên bản chép tay không bao giờ lệch bản lô gửi đi.
    """
    import services.youtube_meta as YM

    b = _get(batch_id)
    if not BS.find_item(b, project_id):
        raise HTTPException(status_code=404, detail=L("Video không nằm trong lô này.", "This video is not in this batch."))
    p = await run_in_threadpool(ADV.load_project, project_id, False)
    if not (p.get("b_chunks") or []):
        raise HTTPException(status_code=400, detail=L("Chưa có bản dịch — chạy bước Dịch trước.", "There is no translation yet — run the Translate step first."))
    return {"prompt": YM.dung_prompt(_ngu_canh_soan(p, b.get("config") or {}))}


class DanBaiSoanRequest(BaseModel):
    #: Nguyên văn câu trả lời của AI ngoài (chấp nhận cả khi bọc trong ```json).
    noi_dung: str = ""


@router.post("/{batch_id}/items/{project_id}/dan-bai-soan")
async def dan_bai_soan(batch_id: str, project_id: str,
                       d: DanBaiSoanRequest = Body(...)):
    """Nhận JSON người dùng tự chạy AI ngoài rồi dán về, ghi vào dự án.

    Dọn bằng ĐÚNG hàm mà đường tự động dùng (``YM.chuan_hoa``) — không thì bản dán
    tay lọt qua những trần mà bản tự động chặn (thẻ quá dài, tiêu đề có < >).
    """
    import time as _t

    import services.youtube_meta as YM

    b = _get(batch_id)
    if not BS.find_item(b, project_id):
        raise HTTPException(status_code=404, detail=L("Video không nằm trong lô này.", "This video is not in this batch."))
    try:
        kq = YM.chuan_hoa(YM._boc_json(d.noi_dung))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not (kq.get("tieu_de") or []):
        raise HTTPException(
            status_code=400,
            detail=L("JSON không có phần `tieu_de` — chép lại đúng câu trả lời của AI.", "The JSON has no `tieu_de` part — copy the AI answer again exactly."),
        )

    p = await run_in_threadpool(ADV.load_project, project_id, False)
    cu_meta = p["meta"].get("youtube_meta") or {}
    p["meta"]["youtube_meta"] = {**cu_meta, **kq, "luc": _t.time()}
    await run_in_threadpool(ADV.save_meta, p["dir"], p["meta"])

    b = BS.load(batch_id) or b
    BS.set_step(b, project_id, "meta", "done", "")
    return {"ok": True, "tieu_de": kq["tieu_de"][0]}


@router.post("/{batch_id}/items")
async def add_items(batch_id: str, data: ItemsRequest = Body(...)):
    b = _get(batch_id)
    items = [{"project_id": pid, **_thong_tin_du_an(pid)}
             for pid in data.project_ids if pid]
    b = BS.add_items(b, items)
    #  Dự án mới vào lô nhận ngay cấu hình lô (mở dự án là thấy), kể cả dựng hình theo dự án mẫu.
    await _ghi_cau_hinh_lo_xuong_du_an(b, pids=[it["project_id"] for it in items], dung_hinh=True)
    #  Dự án thêm vào có thể đã làm dở/xong từ trước — đánh dấu ngay, không thì lượt
    #  chạy đầu tiên làm lại từ đầu và ghi đè kết quả cũ.
    return _view(await _dong_bo_trang_thai(b))


@router.post("/{batch_id}/from-videos")
async def add_from_videos(batch_id: str, data: VideosRequest = Body(...)):
    """Chọn thẳng VIDEO -> tự tạo dự án Chi tiết cho từng tệp rồi thả vào lô.

    Đây là đường vào chính của việc chạy hàng loạt: người dùng có sẵn một thư mục
    video, không phải ngồi tạo tay từng dự án rồi mới gom lại.

    Tạo dự án đi qua ĐÚNG hai bước mà trang chủ vẫn dùng — ``create_project`` rồi
    ``enable_advanced`` — nên video có sẵn phụ đề chính chủ vẫn được đổ thẳng vào
    timeline y như tạo tay (bước ``enable`` lo việc đó).

    Một tệp hỏng không được chặn cả mẻ: ghi vào ``failed`` rồi làm tiếp tệp sau.
    """
    import os

    from services.video_service import VideoService

    b = _get(batch_id)
    video_service = VideoService()
    tao_duoc, that_bai = [], []
    for path in data.paths:
        path = (path or "").strip().strip('"')
        ten = os.path.basename(path) or path
        if not path or not os.path.exists(path):
            that_bai.append({"path": path, "error": L("Không tìm thấy tệp", "File not found")})
            continue
        try:
            try:
                thoi_luong = await run_in_threadpool(
                    video_service.get_video_duration, path
                )
            except Exception:  # noqa: BLE001 — không đọc được thì để 0, dự án vẫn mở được
                thoi_luong = 0.0
            pid = await run_in_threadpool(
                review_service.create_project,
                path, float(thoi_luong or 0), "auto", "vi", "vi-VN-NamMinhNeural",
                "", True,
            )
            await enable_advanced(pid)
            tao_duoc.append({"project_id": pid, **_thong_tin_du_an(pid)})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LÔ] Không tạo được dự án từ '{path}': {e}", exc_info=True)
            that_bai.append({"path": path, "name": ten, "error": str(e)[:200]})

    if tao_duoc:
        b = BS.add_items(b, tao_duoc)
        await _ghi_cau_hinh_lo_xuong_du_an(b, pids=[it["project_id"] for it in tao_duoc], dung_hinh=True)
    return {"batch": _view(b), "created": len(tao_duoc), "failed": that_bai}


@router.patch("/{batch_id}/items/{project_id}")
async def set_item(batch_id: str, project_id: str, body: dict = Body(...)):
    """Đổi cấu hình RIÊNG của một video trong lô. Hiện chỉ có giọng đọc."""
    b = _get(batch_id)
    if "voice" not in body:
        raise HTTPException(400, L("Chỉ nhận khoá 'voice'", "Only the 'voice' key is accepted"))
    try:
        b = BS.set_item_voice(b, project_id, str(body.get("voice") or ""))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _view(b)


@router.delete("/{batch_id}/items/{project_id}")
async def remove_item(batch_id: str, project_id: str):
    b = _get(batch_id)
    if BS.is_running(b):
        raise HTTPException(status_code=409, detail=L("Lô đang chạy — hãy Huỷ trước.", "The batch is running — cancel it first."))
    return _view(BS.remove_item(b, project_id))


class RenameRequest(BaseModel):
    title: str


@router.patch("/{batch_id}")
async def rename_batch(batch_id: str, data: RenameRequest = Body(...)):
    """Đổi tên lô. Tên mặc định là ngày giờ tạo — đủ để phân biệt, nhưng đặt tên
    theo việc ("Phim Hoa ngữ tuần này") thì mở lại sau vài hôm mới biết là lô gì."""
    b = _get(batch_id)
    ten = (data.title or "").strip()
    if not ten:
        raise HTTPException(status_code=400, detail=L("Tên lô không được để trống", "The batch name cannot be empty"))
    b["title"] = ten[:80]
    return _view(BS.save(b))


@router.patch("/{batch_id}/config")
async def patch_config(batch_id: str, data: dict = Body(...)):
    b = BS.set_config(_get(batch_id), data or {})
    #  Ghi ngay xuống từng dự án; dựng hình theo mẫu chỉ chép lại khi chính ô mẫu / phần mẫu vừa đổi.
    doi = set((data or {}).keys())
    await _ghi_cau_hinh_lo_xuong_du_an(b, dung_hinh=bool(doi & {"template_project", "template_parts"}))
    return _view(b)


class ApplySettings(BaseModel):
    """Chép cấu hình xuống TỪNG dự án trong lô.

    ``compositor_from`` = mã dự án mẫu: chép nguyên cấu hình dựng hình của nó (kiểu
    phụ đề, watermark, logo, khung, vùng che…) sang mọi dự án còn lại. Cách này thay
    cho việc dựng lại cả trình chỉnh trong trang lô: người dùng canh MỘT dự án cho
    ưng mắt — ngay trong trình dựng quen thuộc, có xem trước thật — rồi nhân ra.
    """

    audio: bool = True
    compositor_from: str = ""
    #  Phần nào của cấu hình mẫu được chép (khoá của ``PHAN_MAU``). None = mặc định.
    parts: Optional[List[str]] = None



#  Chép theo từng phần — luật chung ở services/mau_cau_hinh.py (cũng dùng cho "mặc định
#  cho dự án mới").
from services import mau_cau_hinh as MAU  # noqa: E402

PHAN_MAU = MAU.PHAN_MAU
MAC_DINH_PHAN_MAU = MAU.MAC_DINH_PHAN_MAU
_doc_cau_hinh_tho = MAU.doc_cau_hinh_tho


def ghep_phan_mau(dich: dict, mau: dict, phan, pid_dich: str) -> dict:
    """``MAU.ghep_phan_mau`` với tệp đi kèm chép vào thư mục dự án Chi tiết ``pid_dich``."""
    from routes import review_routes as RR

    return MAU.ghep_phan_mau(dich, mau, phan, RR._review_project_dir(pid_dich))


@router.get("/{batch_id}/ocr-regions")
async def ocr_regions(batch_id: str):
    """VÙNG phụ đề của từng dự án trong lô — trả {project_id: vùng hoặc null}.

    Trả NGUYÊN toạ độ chứ không phải cờ có/không: hộp khoanh vùng phải mở ra đúng
    khung đã lưu để chỉnh lại, mở ra khung mặc định là người dùng vẽ đè lên công
    mình vừa làm mà không hay.

    Chỉ giao diện gọi, và chỉ khi nguồn phụ đề là OCR / Kết hợp. Đọc meta.json từng
    dự án (tệp nhỏ) nên vài trăm dự án vẫn nhẹ, nhưng vẫn đẩy sang luồng phụ để
    không chẹn vòng lặp sự kiện.
    """
    batch = BS.load(batch_id)
    if not batch:
        raise HTTPException(404, L("Không tìm thấy lô", "Batch not found"))

    def _doc() -> dict:
        ra = {}
        for it in batch.get("items") or []:
            pid = it["project_id"]
            try:
                meta = review_service.get_metadata(pid) or {}
                ra[pid] = meta.get("ocr_region") or None
            except Exception:  # noqa: BLE001 — dự án hỏng thì coi như chưa khoanh
                ra[pid] = None
        return ra

    return {"regions": await run_in_threadpool(_doc)}


@router.post("/demo")
async def tao_lo_mau(dung_lai: bool = False):
    """LÔ MẪU (services/du_an_mau.py): lô mới + 2 dự án từ video mẫu, cấu hình sẵn OCR +
    dịch / soạn bài bằng prompt tự dán + giọng Edge — chạy được trên máy chưa tải model.
    Mỗi dự án được khoanh sẵn vùng phụ đề (bước Lấy phụ đề của lô đòi vùng của TỪNG dự án)."""
    from services import du_an_mau as DM
    from services.config_service import get_config_service

    if dung_lai:
        for cu in BS.list_all():
            if cu.get("title") == DM.TIEU_DE_LO and cu.get("items"):
                return {"batch": _view(cu), "created": 0, "dung_lai": True}
    try:
        path = await run_in_threadpool(
            DM.chep_vao, os.path.join(get_config_service().get_review_root(), "mau")
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    b = BS.create(DM.TIEU_DE_LO)
    b = BS.set_config(b, dict(DM.CAU_HINH_LO))
    kq = await add_from_videos(b["id"], VideosRequest(paths=[path, path]))
    b = _get(b["id"])
    for i, it in enumerate(b.get("items") or [], 1):
        p = ADV.load_project(it["project_id"], migrate=False)
        p["meta"].update(DM.META_CHI_TIET)
        p["meta"]["title"] = f"{DM.TIEU_DE} ({i})"
        #  Dự án THUỘC LÔ mẫu không phải "dự án Chi tiết mẫu" để hướng dẫn mở lại từ trang
        #  danh sách (``tim_chi_tiet``) — không thì bấm chương lại mở nhầm dự án của lô.
        p["meta"]["du_an_mau"] = False
        p["meta"]["thuoc_lo_mau"] = True
        await run_in_threadpool(ADV.save_meta, p["dir"], p["meta"])
        #  Bảng lô hiện tên lưu trong LÔ (lấy từ tên tệp lúc thêm) — đổi cả ở đây.
        it["title"] = p["meta"]["title"]
    BS.save(b)
    return {"batch": _view(_get(b["id"])), "created": kq.get("created", 0)}


@router.post("/{batch_id}/mac-dinh")
async def luu_mac_dinh_lo(batch_id: str):
    """"Đặt cấu hình lô này làm mặc định cho lô mới" (services/mac_dinh_du_an.py)."""
    from services import mac_dinh_du_an as MD

    b = _get(batch_id)
    cfg = {k: v for k, v in (b.get("config") or {}).items()
           if k in BS.DEFAULT_CONFIG and k not in MD.KHOA_LO_BO}
    MD.luu("lo", cfg, None, [], tu=b.get("title") or "")
    return {"ok": True, "trang_thai": MD.trang_thai()}


@router.post("/{batch_id}/apply-settings")
async def apply_settings(batch_id: str, data: ApplySettings = Body(...)):
    b = _get(batch_id)
    if BS.is_running(b):
        raise HTTPException(status_code=409, detail=L("Lô đang chạy — hãy đợi hoặc Huỷ.", "The batch is running — wait or cancel."))
    cfg = b.get("config") or {}

    mau = None
    if data.compositor_from:
        from routes.review_routes import _review_compositor_config_path
        from services.compositor_utils import load_compositor_config

        mau = await run_in_threadpool(
            load_compositor_config, _review_compositor_config_path(data.compositor_from)
        )
    phan = [p for p in (data.parts if data.parts is not None else MAC_DINH_PHAN_MAU)
            if p in PHAN_MAU]

    xong, hong = 0, []
    for item in b["items"]:
        pid = item["project_id"]
        if pid == data.compositor_from and not data.audio:
            continue
        try:
            if data.audio:
                p = ADV.load_project(pid, migrate=False)
                meta = p["meta"]
                #  KHÔNG TẮT cờ của dự án ĐÃ CÓ STEM trên đĩa. Tắt đi là rơi
                #  đúng vào trạng thái nửa vời ở trên: trình dựng vẫn khoá nút tách
                #  (vì thấy stem), mà lúc trộn lại bỏ qua hai mức "Giọng gốc".
                #  Muốn bỏ hẳn thì xoá stem trong dự án, không phải tắt cờ ở lô.
                muon = bool(cfg.get("separate_audio"))
                if muon or not _co_stem(p["dir"]):
                    meta["separate_audio"] = muon
                for k in (*ADV.PROJECT_MIX_KEYS, ADV.TTS_VOLUME_FIELD):
                    if k in cfg:
                        meta[k] = TL.clamp_volume(cfg[k], 0.0)
                #  Đánh dấu "đã tự chỉnh" y như khi người dùng kéo trong dự án — thiếu
                #  cờ này thì lúc dựng, engine đi đường tắt ``-c copy`` và BỎ QUA cả 4
                #  mức vừa đặt (xem ADV.mix_touched).
                meta[ADV.MIX_TOUCHED_FIELD] = True
                await run_in_threadpool(ADV.save_meta, p["dir"], meta)
            if mau is not None and phan and pid != data.compositor_from:
                from routes.review_routes import _review_compositor_config_path
                from services.compositor_utils import save_compositor_config

                duong = _review_compositor_config_path(pid)
                cu = await run_in_threadpool(_doc_cau_hinh_tho, duong)
                await run_in_threadpool(
                    save_compositor_config, duong, ghep_phan_mau(cu, mau, phan, pid)
                )
            xong += 1
        except Exception as e:  # noqa: BLE001 — một dự án hỏng không chặn cả lô
            logger.warning(f"[LÔ] Không áp được cấu hình cho {pid}: {e}", exc_info=True)
            hong.append({"project_id": pid, "title": item["title"], "error": str(e)[:200]})
    return {"applied": xong, "failed": hong, "batch": _view(b)}


@router.post("/{batch_id}/run")
async def run_step(batch_id: str, data: RunRequest = Body(...)):
    b = _get(batch_id)
    if data.step not in BS.STEPS:
        raise HTTPException(status_code=400, detail=L("Bước không hợp lệ", "Invalid step"))
    if batch_id in _running or BS.is_running(b):
        raise HTTPException(status_code=409, detail=L("Lô này đang chạy một bước khác.", "This batch is running another step."))
    _chan_neu_may_ban()
    chi = _loc_da_chon(b, data.project_ids)
    todo = BS.pending_for(b, data.step, force=data.force)
    if chi is not None:
        todo = [p for p in todo if p in chi]
    if not todo:
        raise HTTPException(
            status_code=400,
            detail=(
                L("Những video đã chọn không còn gì phải chạy ở bước này (bấm 'Chạy lại' nếu muốn làm lại).",
                  "The selected videos have nothing left to run in this step (click 'Run again' to redo).")
                if chi is not None else
                L("Không còn dự án nào phải chạy ở bước này (bấm 'Chạy lại tất cả' nếu muốn làm lại).",
                  "No projects are left to run in this step (click 'Run all again' to redo).")
            ),
        )
    BS.start_run(b, data.step, len(todo))
    #  Khong tick gi thi KHONG truyen ``only``: de ``_chay_lo`` tu tinh lai danh sach lúc
    #  no thuc su chay, dung y nhu truoc gio.
    _running[batch_id] = asyncio.create_task(
        _chay_lo(batch_id, data.step, data.force, only=(todo if chi is not None else None))
    )
    return {"status": "started", "step": data.step, "count": len(todo)}


class RunChainRequest(BaseModel):
    steps: List[str]
    force: bool = False
    #  Nhu ``RunRequest.project_ids`` — rong la ca lo.
    project_ids: List[str] = []


async def _chay_chuoi(batch_id: str, steps: list, force: bool, only: Optional[list] = None):
    """Chạy LẦN LƯỢT nhiều bước cho cả lô — "bấm rồi đi ngủ".

    Đây mới là cách người ta thật sự dùng trang này: tối bấm cho 20 video chạy
    phụ đề -> dịch -> giọng, sáng ra vào dồn và xuất. Trước đây phải ngồi canh
    từng bước xong rồi bấm bước kế — tức là phải thức.

    Ba điều quan trọng:
      · chạy theo ĐÚNG THỨ TỰ trong ``BS.STEPS`` chứ không theo thứ tự người dùng
        tick — dịch trước khi có phụ đề thì cả lô lỗi;
      · bước không còn gì để làm thì BỎ QUA, không tính là lỗi (rất hay gặp: vài
        video đã dịch tay từ hôm trước);
      · HUỶ là dừng cả chuỗi, không phải chỉ dừng bước đang chạy — bấm Huỷ mà nó
        lại sang bước sau thì không ai hiểu nổi.
    """
    try:
        for step in [x for x in BS.STEPS if x in steps]:
            if BS.cancel_requested(batch_id):
                logger.info(f"[LÔ] Chuỗi của lô {batch_id} dừng theo yêu cầu huỷ.")
                BS.them_log(batch_id, [L("■ Đã huỷ — dừng chuỗi", "■ Cancelled — chain stopped")], "", "")
                return
            b = BS.load(batch_id)
            if not b:
                return
            todo = BS.pending_for(b, step, force=force)
            #  Lọc theo những video người dùng đã tick. Lọc Ở ĐÂY, TRƯỚC phép xét bước
            #  trước đã xong chưa: lọc sau thì video không được chọn vẫn bị ghi trạng
            #  thái "chờ bước trước xong" — người dùng thấy dòng đó nhấp nháy dù họ
            #  không hề chọn nó.
            if only:
                todo = [p for p in todo if p in set(only)]
            if not todo:
                BS.them_log(
                    batch_id,
                    [L(f"⏭ Bỏ qua {BS.STEP_LABEL.get(step, step)} — không còn dự án nào "
                       f"phải chạy",
                       f"⏭ Skipped {BS.STEP_LABEL.get(step, step)} — no projects left "
                       f"to run")],
                    "", step,
                )
                continue
            #  TUẦN TỰ THEO TỪNG DỰ ÁN: một dự án chỉ được sang bước này khi MỌI bước
            #  đứng trước nó trong chuỗi đã xong (hoặc người dùng cố ý bỏ qua). Không
            #  thì dự án lỗi ở Sinh giọng vẫn bị "nhảy cóc" tới Xuất video — ra một
            #  bản không có lời bình mà nhìn tên tệp không ai biết là thiếu.
            truoc = [x for x in BS.STEPS
                     if x in steps and BS.STEPS.index(x) < BS.STEPS.index(step)]
            hop_le, cho = [], []
            for pid in todo:
                it = BS.find_item(b, pid) or {}
                thieu = [x for x in truoc
                         if ((it.get("steps") or {}).get(x) or {}).get("status", "pending")
                         not in ("done", "skipped")]
                (cho if thieu else hop_le).append((pid, thieu))
            for pid, thieu in cho:
                BS.set_step(b, pid, step, "pending",
                            L(f"chờ {BS.STEP_LABEL.get(thieu[0], thieu[0])} xong trước",
                              f"waiting for {BS.STEP_LABEL.get(thieu[0], thieu[0])} to finish first"))
            if cho:
                BS.them_log(
                    batch_id,
                    [L(f"⏭ {BS.STEP_LABEL.get(step, step)}: bỏ qua {len(cho)} dự án vì "
                       f"bước trước chưa xong",
                       f"⏭ {BS.STEP_LABEL.get(step, step)}: skipped {len(cho)} projects because "
                       f"the previous step is not finished")],
                    "", step,
                )
            if not hop_le:
                continue
            todo = [pid for pid, _ in hop_le]
            BS.start_run(b, step, len(todo))
            #  ``trong_chuoi``: mắt giữa chuỗi KHÔNG được nhả chỗ trong ``_running``,
            #  nếu không thì giữa hai bước có một khe cho request khác chen vào.
            if await _chay_lo(batch_id, step, force, only=todo, trong_chuoi=True):
                BS.them_log(batch_id, [L("■ Đã huỷ — dừng chuỗi", "■ Cancelled — chain stopped")], "", "")
                return
        BS.them_log(batch_id, [L("■ Xong chuỗi bước đã chọn", "■ Finished the chain of selected steps")], "", "")
    finally:
        #  Chỗ trong ``_running`` do chuỗi giữ từ đầu tới cuối — chính nó phải nhả.
        _running.pop(batch_id, None)
        #  Chuỗi chạy mọi bước với ``trong_chuoi=True`` nên ``_chay_lo`` KHÔNG gửi số
        #  liệu ở cuối mỗi bước — chuỗi phải tự gửi một lần khi xong, không thì chạy
        #  qua đêm xong thanh hạn mức vẫn đứng ở con số của tối qua.
        _gui_so_lieu_ngam()


class RunOneRequest(BaseModel):
    step: str


@router.post("/{batch_id}/run-chain")
async def run_chain(batch_id: str, data: RunChainRequest = Body(...)):
    """Chạy nhiều bước nối tiếp nhau cho cả lô (bấm rồi để máy chạy qua đêm)."""
    b = _get(batch_id)
    chon = [x for x in BS.STEPS if x in (data.steps or [])]
    if not chon:
        raise HTTPException(status_code=400, detail=L("Chưa chọn bước nào", "No step selected"))
    if batch_id in _running or BS.is_running(b):
        raise HTTPException(status_code=409, detail=L("Lô này đang chạy một bước khác.", "This batch is running another step."))
    _chan_neu_may_ban()
    chi = _loc_da_chon(b, data.project_ids)
    #  Đếm việc để báo ngay cho người dùng biết mình vừa đặt bao nhiêu việc.
    so_viec = 0
    for x in chon:
        ds = BS.pending_for(b, x, force=data.force)
        so_viec += len([p for p in ds if p in chi] if chi is not None else ds)
    if not so_viec:
        raise HTTPException(
            status_code=400,
            detail=(
                L("Những video đã chọn không còn gì để chạy ở các bước này (bấm 'Chạy lại' nếu muốn làm lại).",
                  "The selected videos have nothing left to run in these steps (click 'Run again' to redo).")
                if chi is not None else
                L("Các bước đã chọn đều không còn gì để chạy (tick 'chạy lại tất cả' nếu muốn làm lại).",
                  "None of the selected steps has anything left to run (tick 'run all again' to redo).")
            ),
        )
    BS.them_log(
        batch_id,
        [L("▶ Bắt đầu chuỗi: ", "▶ Starting chain: ")
         + " → ".join(BS.STEP_LABEL.get(x, x) for x in chon)
         + L(f" ({so_viec} lượt việc)", f" ({so_viec} {'task' if so_viec == 1 else 'tasks'})")],
        "", "",
    )
    #  NHỚ bộ bước đã tick ngay trong lô: rời trang rồi quay lại, giao diện đọc lại
    #  từ đây thay vì hiện bộ mặc định — người dùng tick 3 bước, mở lại thấy tick
    #  khác là tưởng máy chạy sai bước.
    #  ĐỌC LẠI TỪ ĐĨA trước khi ghi: ``them_log`` ngay trên vừa ghi dòng "Bắt đầu chuỗi"
    #  xuống đĩa, ghi đè bằng bản ``b`` đọc từ đầu request là xoá mất dòng đó.
    b = BS.load(batch_id) or b
    b["chain"] = chon
    #  XOÁ CỜ HUỶ CŨ. Chuỗi kiểm cờ huỷ NGAY bước đầu, trước cả ``start_run`` (chỗ duy
    #  nhất xoá cờ) — nên cờ sót lại (bấm Huỷ lúc lô rảnh, bấm Huỷ lần hai khi lượt trước
    #  vừa dừng) làm MỌI lần bấm chạy sau đều in "Đã huỷ — dừng chuỗi" rồi thôi, mà lối
    #  thoát đó không gọi ``end_run`` nên cờ không bao giờ tự mất (ca thật 23-09-2026).
    b.setdefault("run", {})["cancel"] = False
    BS.save(b)
    _running[batch_id] = asyncio.create_task(
        _chay_chuoi(batch_id, chon, data.force,
                    only=(sorted(chi) if chi is not None else None))
    )
    return {"status": "started", "steps": chon, "count": so_viec}


@router.post("/{batch_id}/run-one/{project_id}")
async def run_one(batch_id: str, project_id: str, data: RunOneRequest = Body(...)):
    """Chạy MỘT bước cho MỘT dự án — đường chạy lại sau khi một video lỗi.

    Dùng đúng bộ chạy của cả lô (chỉ khác danh sách một phần tử) nên mọi luật vẫn
    giữ: khoá dự án, ghi trạng thái, huỷ được, lỗi vào bảng tổng kết.
    """
    b = _get(batch_id)
    if data.step not in BS.STEPS:
        raise HTTPException(status_code=400, detail=L("Bước không hợp lệ", "Invalid step"))
    if not BS.find_item(b, project_id):
        raise HTTPException(status_code=404, detail=L("Dự án không có trong lô", "The project is not in the batch"))
    if batch_id in _running or BS.is_running(b):
        raise HTTPException(status_code=409, detail=L("Lô này đang chạy — hãy đợi hoặc Huỷ.", "This batch is running — wait or cancel."))
    BS.start_run(b, data.step)
    _running[batch_id] = asyncio.create_task(
        _chay_lo(batch_id, data.step, True, only=[project_id])
    )
    return {"status": "started", "step": data.step, "count": 1}


@router.get("/thumb/{project_id}")
async def project_thumb(project_id: str):
    """Ảnh đại diện của video dự án — dùng CHUNG hàm với Trang chủ (review_advanced_routes).
    Trang lô có hàng trăm dòng: giao diện chỉ tải ảnh của dòng đang nhìn thấy."""
    from routes.review_advanced_routes import anh_dai_dien_du_an

    return await anh_dai_dien_du_an(project_id)


@router.post("/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    """Huỷ lô = dừng CẢ dự án đang chạy, không chỉ chặn dự án kế tiếp.

    Bản đầu chỉ đặt cờ cho vòng lặp và để dự án đang chạy "chạy nốt" — với một dự
    án 4.000 câu, "chạy nốt" là vài tiếng, người dùng bấm Huỷ mà thấy nó vẫn sinh
    giọng đều đều thì đúng là "ấn huỷ không được". Nay đặt luôn hai cờ huỷ của
    chính dự án đó (dựng + phân tích) — đúng việc nút Huỷ trong dự án làm — nên bước
    đang chạy (sinh giọng, tách, dựng, đốt hình) dừng trong vài giây, rồi vòng lặp
    thấy cờ lô mà dừng hẳn.
    """
    b = _get(batch_id)
    #  Lô không chạy gì thì KHÔNG đặt cờ: cờ đặt lúc rảnh không ai tiêu thụ, nằm đó
    #  chờ giết lượt chạy kế tiếp của người dùng.
    if batch_id not in _running and not BS.is_running(b):
        return {"status": "idle", "project_id": "", "stopped": []}
    BS.request_cancel(batch_id)
    dang =((b.get("run") or {}).get("project_id") or "").strip()
    dung = []
    if dang:
        for ten, ham in (("render", review_service.cancel_render),
                         ("analysis", review_service.cancel_analysis)):
            try:
                if ham(dang):
                    dung.append(ten)
            except Exception as e:  # noqa: BLE001 — huỷ hụt một cờ không được chặn cờ kia
                logger.warning(f"[LÔ] Huỷ {ten} của {dang} lỗi (bỏ qua): {e}")
        #  Đang dịch phụ đề thêm thứ tiếng ở đầu bước Xuất -> dừng luôn job đó.
        try:
            from services import review_sub_tracks as ST

            ST.huy_job(dang)
        except Exception:  # noqa: BLE001
            pass
        BS.them_log(batch_id, [L("■ Đã yêu cầu huỷ — dừng dự án đang chạy", "■ Cancel requested — stopping the running project")], dang, "")
    return {"status": "cancelling", "project_id": dang, "stopped": dung}


@router.get("/{batch_id}/export/{project_id}")
async def export_info(batch_id: str, project_id: str):
    """Bản xuất của một dự án nằm ở đâu (để xem lại / mở thư mục).

    Ưu tiên đường dẫn đã ghi lúc chạy lô; KHÔNG có (dự án xuất từ trước khi có tính
    năng này, hoặc xuất bằng tay trong trình dựng) thì TỰ TÌM tệp trong thư mục dự
    án. Thiếu bước tự tìm thì bảng báo "Xong" mà bấm vào chẳng ra gì — đúng cái
    người dùng vừa gặp.
    """
    import os

    b = _get(batch_id)
    item = BS.find_item(b, project_id)
    if not item:
        raise HTTPException(status_code=404, detail=L("Dự án không có trong lô", "The project is not in the batch"))

    duong = ((item.get("data") or {}).get("export") or {}).get("path") or ""
    if not duong or not os.path.exists(duong):
        try:
            p = ADV.load_project(project_id, migrate=False)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=404, detail=L("Không tìm thấy dự án", "Project not found"))
        #  Bản xuất CẢ PHIM đã đốt phụ đề trước; ``final_review.mp4`` (bản trung gian
        #  bước 1/2, chưa có chữ) chỉ là đường cuối.
        from services.compositor_utils import full_export_path

        duong = full_export_path(os.path.join(p["dir"], "compositor_config.json")) or             os.path.join(p["dir"], "final_review.mp4")
    if not os.path.exists(duong):
        raise HTTPException(
            status_code=404,
            detail=L("Chưa thấy tệp video đã xuất của dự án này (có thể đã bị xoá hoặc di chuyển).", "The exported video file of this project was not found (it may have been deleted or moved)."),
        )
    return {
        "path": duong.replace("\\", "/"),
        "mb": round(os.path.getsize(duong) / (1024 * 1024), 1),
        "luc": round(os.path.getmtime(duong)),
    }


@router.get("/{batch_id}/fit-plan/{project_id}")
async def fit_plan(batch_id: str, project_id: str):
    """Phương án dồn đã tính sẵn lúc chạy lô — cho modal gợi ý."""
    b = _get(batch_id)
    item = BS.find_item(b, project_id)
    if not item:
        raise HTTPException(status_code=404, detail=L("Dự án không có trong lô", "The project is not in the batch"))
    data = (item.get("data") or {}).get("fit") or {}
    if not data:
        #  Chưa có (lô cũ / vừa xoá) -> tính lại, vẫn hơn là báo lỗi trống trơn.
        data = {"plan": await run_in_threadpool(ADV.plan_arrange_fit, project_id)}
    return data


@router.post("/{batch_id}/fit-apply/{project_id}")
async def fit_apply(batch_id: str, project_id: str, data: FitApply = Body(...)):
    """Áp dụng phương án người dùng chọn trong modal, rồi đánh dấu bước Dồn xong."""
    b = _get(batch_id)
    if not BS.find_item(b, project_id):
        raise HTTPException(status_code=404, detail=L("Dự án không có trong lô", "The project is not in the batch"))
    if not _acquire_review(project_id):
        raise HTTPException(status_code=409, detail=L("Dự án đang chạy tác vụ khác.", "The project is running another task."))
    try:
        res = await run_in_threadpool(
            ADV.apply_fit_plan, project_id, data.voice_speed, data.video_speed, data.kind
        )
    finally:
        _release_review(project_id)
    b = BS.load(batch_id) or b
    con_cat = int((res or {}).get("cut", 0) or 0)
    BS.set_step(b, project_id, "fit",
                "needs_me" if con_cat else "done",
                L(f"Còn {con_cat} câu thiếu chỗ", f"{con_cat} sentences do not fit") if con_cat
                else L("đã dồn theo phương án đã chọn", "rearranged using the selected option"))
    return {**res, "batch": _view(BS.load(batch_id) or b)}


@router.post("/{batch_id}/skip/{project_id}")
async def skip_step(batch_id: str, project_id: str, data: dict = Body(...)):
    """Bỏ qua một bước cho một dự án (vd đã tự làm tay trong dự án)."""
    step = str((data or {}).get("step") or "")
    if step not in BS.STEPS:
        raise HTTPException(status_code=400, detail=L("Bước không hợp lệ", "Invalid step"))
    b = _get(batch_id)
    return _view(BS.set_step(b, project_id, step, "skipped", L("bỏ qua", "skipped")))
