"""LÔ CHẠY HÀNG LOẠT — dữ liệu và máy trạng thái (KHÔNG chạy việc gì).

Một "lô" là một nhóm dự án Chi tiết được xử lý theo TỪNG BƯỚC:

    phụ đề  ->  dịch  ->  giọng  ->  dồn  ->  xuất

Người dùng bấm chạy TỪNG BƯỚC cho cả lô, không phải một nút "làm hết": giữa các
bước họ còn vào từng dự án sửa tay (đó chính là lý do có chế độ Chi tiết). Mỗi
bước nhớ trạng thái riêng cho từng dự án nên chạy lại chỉ đụng vào cái chưa xong.

Tách riêng khỏi phần CHẠY (routes/batch_routes.py) để test được toàn bộ luật
chuyển trạng thái mà không cần ffmpeg, Whisper hay API key.

Luật đã chốt với chủ dự án (2026-09-16):
  · chạy LẦN LƯỢT từng dự án — STT và sinh giọng đều ăn GPU, chạy song song chỉ
    làm chậm tất cả và dễ hết VRAM;
  · một dự án lỗi thì BỎ QUA, chạy tiếp, cuối lô liệt kê ra — bỏ máy chạy qua
    đêm mà video thứ hai chặn cả lô là mất trắng một đêm.
"""

from utils.i18n import L
import json
import logging
import os
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger("BatchStore")

#: Khoá cho những lúc HAI LUỒNG cùng sửa một lô.
#
#  Có đúng một trường hợp: luồng bám nhật ký của dự án đang chạy (xem
#  ``routes/batch_routes.py``) rót từng dòng vào nhật ký lô, trong khi bộ chạy
#  (luồng chính) cập nhật trạng thái các bước. Cả hai đều đọc cả tệp -> sửa -> ghi
#  đè; không có khoá thì bên ghi sau xoá sạch thay đổi của bên kia — mất nguyên
#  một bước "đã xong" mà không có lỗi nào hiện ra.
KHOA_GHI = threading.RLock()

#: Các bước theo ĐÚNG thứ tự phải chạy. Đổi thứ tự ở đây là đổi cả luồng.
#
#  "separate" (tách giọng khỏi nhạc nền) đứng NGAY TRƯỚC bước xuất — chốt với chủ
#  dự án 2026-09-16. Nó chỉ phục vụ lúc TRỘN tiếng (4 mức âm lượng ở bản xuất),
#  không phải thứ dịch hay sinh giọng cần tới. Đặt nó trước sinh giọng như bản đầu
#  thì cả lô phải chờ xong một lượt Demucs (rất nặng) mới bắt đầu có giọng để nghe
#  — tức là chờ đúng cái mình chưa cần.
#
#  Bước này KHÔNG ép buộc: bỏ qua thì bản xuất vẫn ra, chỉ là không tách bạch được
#  nhạc nền với giọng gốc.
#  "meta" (soạn bài đăng) đứng NGAY SAU bước dịch: nó cần bản dịch làm ngữ cảnh,
#  và chạy sớm thì tới lúc mở tab đăng YouTube đã có sẵn tiêu đề / mô tả / thẻ /
#  hashtag / câu hook — không phải ngồi chờ AI ở đúng lúc muốn đăng.
#  "clips" (xuất các đoạn đã chọn) đứng SAU "export": dùng chung danh sách đoạn với
#  nút "Xuất đoạn" của trình dựng (services/export_ranges.py), mỗi đoạn một video riêng.
STEPS = ("sub", "translate", "meta", "voice", "fit", "separate", "export", "clips")

class _NhanBuoc(dict):
    """Nhãn bước theo ngôn ngữ giao diện hiện tại (tiếng Việt: đúng như cũ)."""

    _EN = {
        "sub": "Get subtitles",
        "translate": "Translate",
        "meta": "Draft post",
        "separate": "Separate voice & music",
        "voice": "Generate voice",
        "fit": "Fit",
        "export": "Export video",
        "clips": "Export clips",
    }

    def __getitem__(self, k):
        vi = dict.__getitem__(self, k)
        return L(vi, self._EN.get(k, vi))

    def get(self, k, default=None):
        return self[k] if k in self else default

    def items(self):
        return [(k, self[k]) for k in self.keys()]

    def values(self):
        return [self[k] for k in self.keys()]


STEP_LABEL = _NhanBuoc({
    "sub": "Lấy phụ đề",
    "translate": "Dịch",
    "meta": "Soạn bài đăng",
    "separate": "Tách giọng & nhạc nền",
    "voice": "Sinh giọng",
    "fit": "Dồn",
    "export": "Xuất video",
    "clips": "Xuất đoạn",
})

#: Trạng thái một bước của MỘT dự án.
#:   pending  — chưa chạy
#:   running  — đang chạy
#:   done     — xong
#:   failed   — lỗi (đã bỏ qua, chạy tiếp dự án khác)
#:   needs_me — chạy được nhưng CẦN NGƯỜI QUYẾT (bước dồn không tự xử được)
#:   busy     — dự án đang bận việc khác (người dùng tự mở ra chạy) -> lô ĐỂ YÊN,
#:              KHÔNG phải lỗi; lần chạy sau vẫn nhặt lại
#:   skipped  — người dùng chủ động bỏ qua
STATUSES = ("pending", "running", "done", "failed", "needs_me", "busy", "skipped")


def _batches_dir() -> str:
    from models.database import get_app_data_dir

    d = os.path.join(get_app_data_dir(), "batches")
    os.makedirs(d, exist_ok=True)
    return d


def _path(batch_id: str) -> str:
    return os.path.join(_batches_dir(), f"{batch_id}.json")


def _now() -> float:
    return time.time()


def _blank_steps() -> dict:
    return {s: {"status": "pending", "error": "", "at": 0.0} for s in STEPS}


#: Nhật ký giữ tối đa ngần này dòng cho mỗi lô.
#:
#: Chỉ là CHỮ nên rộng tay: 5000 dòng ≈ 500 KB, chứa thoải mái vài chục lượt chạy
#: của một lô 200 video. Vẫn phải có trần vì cả tệp lô được GHI LẠI mỗi lần đổi
#: trạng thái một bước — để vô hạn thì tới lúc nào đó mỗi bước xong lại ghi vài MB
#: xuống đĩa, chạy lô lớn sẽ ì ra mà không ai hiểu vì sao.
LOG_TOI_DA = 5000


def _cau_hinh_lo_moi() -> dict:
    """Cấu hình của LÔ MỚI: mặc định gốc, đè bằng "mặc định cho lô mới" người dùng đã lưu
    (services/mac_dinh_du_an.py). CHỈ ở đây — ``load()`` ghép DEFAULT_CONFIG cho lô cũ,
    ghép cả mặc định người dùng ở đó là lô cũ lặng lẽ đổi cấu hình."""
    import copy

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        from services import mac_dinh_du_an as MD

        cfg.update(copy.deepcopy(MD.cai_dat("lo", [k for k in DEFAULT_CONFIG if k not in MD.KHOA_LO_BO])))
    except Exception as e:  # noqa: BLE001 — hỏng phần mặc định thì lô vẫn tạo được
        logger.warning(f"Không đọc được mặc định cho lô mới: {e}")
    return cfg


def create(title: str = "") -> dict:
    """Lô mới, rỗng."""
    bid = uuid.uuid4().hex[:12]
    batch = {
        "id": bid,
        "title": (title or "").strip() or time.strftime("Lô %d/%m %H:%M"),
        "created_at": _now(),
        "items": [],
        #  Cấu hình DÙNG CHUNG cho cả lô. Một giọng cho tất cả video là yêu cầu
        #  rõ ràng của chủ dự án — ai muốn khác thì vào từng dự án đổi.
        "config": _cau_hinh_lo_moi(),
        #  Lượt chạy hiện tại (rỗng = đang rảnh).
        "run": {"step": "", "project_id": "", "started_at": 0.0, "cancel": False},
    }
    save(batch)
    return batch


def save(batch: dict) -> dict:
    tmp = _path(batch["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(batch, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(batch["id"]))
    return batch


#: Cấu hình mặc định — dùng cả lúc TẠO lô lẫn lúc ĐỌC lô cũ lên (xem ``load``).
DEFAULT_CONFIG = {
    "source_language": "auto",
    "target_language": "vi",
    "subtitle_mode": "whisper",
    "voice": "",
    #  CÁCH CHỌN GIỌNG:
    #    "chung" — một giọng áp cho mọi video (khoá ``voice`` ở trên),
    #    "rieng" — mỗi video một giọng, chọn ở từng dòng trong bảng (khoá
    #              ``voice`` của từng item).
    #  Để riêng một khoá thay vì suy từ "có giọng chung hay không": lô mới tạo thì
    #  cả hai đều rỗng, không có gì để suy, mà giao diện phải biết bày ô nào.
    "voice_mode": "chung",
    #  BUOC DON — lam gi khi don xong VAN con cau doc khong kip:
    #    "hoi" — dung lai, danh dau "cần bạn quyết" de nguoi dung mo modal chon phuong
    #            an doi toc do (nhu tu truoc toi nay).
    #    "ai"  — nho DeepSeek VIET LAI NGAN HON dung nhung cau do roi sinh giong lai,
    #            khong can ai ngoi canh. Dung cho lo chay qua dem.
    "fit_mode": "hoi",
    #  Bước Dồn ở ``fit_mode = "auto"`` mà KHÔNG chọn được phương án nhẹ nào: True = ghi chú rồi
    #  đi tiếp (chế độ Làm nhanh — không hỏi, không nhờ AI rút gọn); False = hỏi người dùng như cũ.
    "fit_tu_tiep": False,
    #  CHO GIONG TRAN SANG CAU SAU (xem ``review_timeline.tran_giong_cho_phep``): bat + so giay toi da. Chep vao
    #  metadata cua tung du an trong lo (``_ap_cau_hinh_dich``) de buoc Don / Xuat tinh cung mot muc.
    "cho_tran_giong": True,
    "tran_giong": 0.85,                 # = review_timeline.TRAN_GIONG_MAC_DINH
    #  TÁCH CÂU DÀI theo dấu câu + độ dài (services/tach_cau.py) — cùng tên khoá với metadata
    #  dự án Chi tiết, lô ghi xuống từng dự án (``_buoc_sub`` / ``_ap_cau_hinh_dich``):
    #    · câu gốc Whisper lúc Lấy phụ đề — mặc định BẬT (= tach_cau.MAC_DINH_GOC);
    #    · bản dịch lúc nhập kịch bản — mặc định TẮT (= tach_cau.MAC_DINH_DICH).
    "tach_cau_goc": True,
    "tach_cau_dich": False,
    #  NGUỒN SOẠN BÀI ĐĂNG — giống hệt cách chọn nguồn dịch:
    #    "deepseek" — AI tự soạn tiêu đề/mô tả/thẻ/hook (mặc định),
    #    "manual"   — TỰ SOẠN: lô bỏ qua bước này, người dùng chép prompt sang
    #                 ChatGPT/Gemini rồi dán JSON trả về.
    "meta_engine": "ai",                 # "ai" = bộ AI theo Cài đặt (ai_engines.AI_THEO_CAI_DAT); "deepseek" / "gemini" chọn thẳng; "manual"
    #  "" = theo dự án; dự án chưa chọn thì lồng tiếng (``review_advanced_prompts.DEFAULT_KIND``) — người dùng muốn lô luôn lồng tiếng.
    "script_kind": "",
    #  NGUỒN DỊCH cho cả lô:
    #    "deepseek" — AI viết lại theo prompt của dự án (mặc định, chất lượng cao
    #                 nhưng tốn API key và chậm hơn),
    #    "google"   — dịch thẳng từng câu, nhanh và miễn phí, hợp khi chỉ cần
    #                 hiểu đúng nghĩa,
    #    "manual"   — TỰ DỊCH: lô bỏ qua bước này, người dùng copy prompt / dán
    #                 bản dịch trong từng dự án như vẫn làm.
    "translate_engine": "ai",            # "ai" = bộ AI theo Cài đặt · "deepseek" / "gemini" · "google" · "manual"
    #  Hai thứ ĐI THẲNG VÀO PROMPT của DeepSeek (chỉ có nghĩa khi nguồn dịch là
    #  DeepSeek): văn phong và tên kênh. Cùng tên khoá với bảng Chuẩn bị trong dự
    #  án — lô chỉ ghi hộ xuống từng dự án, không tạo đường cấu hình thứ hai.
    "speech_style": "auto",
    "brand_name": "",
    #  ÂM THANH lấy từ đâu:
    #    "lo"     — dùng 4 mức + mức giọng AI ghi ngay dưới đây, áp cho mọi dự án ở
    #               BƯỚC XUẤT (tức con số nhìn thấy ở trang lô đúng là con số nghe
    #               thấy trong tệp ra).
    #    "du_an"  — KHÔNG đụng gì: mỗi dự án giữ đúng mức đã chỉnh trong trình dựng.
    #               Dùng khi lô gom các video đã canh tiếng riêng từng cái.
    "audio_source": "lo",
    #  ÂM THANH GỐC — đúng những khoá mà bảng Chuẩn bị trong dự án ghi.
    #  ``separate_audio`` bật thì 4 mức dưới mới tách bạch nhạc nền / giọng gốc;
    #  tắt thì chỉ hai mức đầu có tác dụng (xem PipelineSidebar).
    "separate_audio": False,
    "bg_volume": 1.0,            # nhạc/âm gốc lúc KHÔNG có giọng đọc AI
    "voice_bg_volume": 0.25,     # nhạc/âm gốc lúc AI đang nói
    #  Giọng gốc IM HẲN cả hai chiều: đã có giọng AI đọc thay thì để tiếng người
    #  nói gốc lọt vào là nghe chồng hai giọng. Mirror ``ADV.MIX_DEFAULTS``.
    "vocals_volume": 0.0,        # giọng gốc lúc không có AI (cần tách giọng)
    "voice_vocals_volume": 0.0,  # giọng gốc lúc AI đang nói
    "tts_volume": 1.5,           # âm lượng giọng đọc AI
    #  Thư mục chép bản xuất của CẢ LÔ về một chỗ. Rỗng = để nguyên trong thư mục
    #  từng dự án (đường cũ) — xuất 20 video mà phải vào 20 thư mục dự án mò từng
    #  tệp thì mất hết cái lợi của chạy hàng loạt.
    "export_dir": "",
    #  Dự án MẪU kiểu phụ đề · watermark · logo · khung (nút "Áp cho tất cả"). Nhớ theo
    #  lô: chọn xong rời trang quay lại mà về "Không chép" là người dùng tưởng mất.
    "template_project": "",
    #  CHÉP NHỮNG PHẦN NÀO từ dự án mẫu (``batch_routes.PHAN_MAU``). Mặc định như trước
    #  (phụ đề · watermark · logo · khung · che mờ); chữ chèn là chữ riêng từng video nên
    #  phải tự tick.
    "template_parts": ["sub", "watermark", "logo", "khung", "mask"],
    #  PHỤ ĐỀ THÊM THỨ TIẾNG (services/review_sub_tracks.py). Chạy ở ĐẦU BƯỚC XUẤT —
    #  lúc đó câu thứ tiếng chính đã chốt (kể cả bản dán tay), dịch sớm hơn là dịch câu
    #  còn sửa. ``sub_engine`` "manual" = không tự dịch: người dùng Copy prompt / Dán kết
    #  quả ở từng dự án; thiếu câu thì bước xuất báo lỗi chứ không xuất phụ đề khuyết.
    "sub_langs": [],
    "sub_engine": "ai",                  # "ai" = bộ AI theo Cài đặt · "deepseek" / "gemini" · "google" · "manual"
    #: Thứ tiếng ĐỐT vào video; "" = thứ tiếng chính (câu lồng tiếng) như cũ.
    "sub_burn_lang": "",
    #: Xuất kèm .srt cho thứ tiếng chính + mọi thứ tiếng thêm.
    "sub_srt": False,
    #: Thứ tiếng THỨ HAI hiện cùng lúc (dòng nhỏ sát đáy, thay chỗ "Sub gốc"); "" = không.
    #: Được phép là thứ tiếng chính khi ``sub_burn_lang`` là thứ tiếng khác.
    "sub_second_lang": "",
    #: NHÚNG phụ đề mềm (MP4, người xem tự bật/tắt) cho thứ tiếng chính + mọi thứ tiếng thêm.
    "sub_soft": False,
}


def them_log(batch_id: str, cac_dong: list, project_id: str = "",
             step: str = "") -> None:
    """Rót một CHÙM dòng vào nhật ký lô, an toàn giữa hai luồng.

    Gộp theo chùm chứ không ghi từng dòng: mỗi lượt là một lần đọc + ghi cả tệp
    lô, ghi từng dòng của Whisper/ffmpeg thì đĩa quay không kịp.
    """
    if not cac_dong:
        return
    with KHOA_GHI:
        b = load(batch_id)
        if not b:
            return
        for t in cac_dong:
            ghi_log(b, t, project_id, step)
        save(b)


def load(batch_id: str) -> Optional[dict]:
    try:
        with open(_path(batch_id), "r", encoding="utf-8") as f:
            batch = json.load(f)
    except (OSError, ValueError):
        return None
    #  LÔ CŨ (tạo trước khi thêm khoá mới) phải được VÁ KHI ĐỌC, không thì:
    #    · thiếu khoá âm lượng -> giao diện đọc ra 0 và hiện "0%", người dùng bấm
    #      "Áp cho tất cả" là cả lô câm tiếng gốc mà không ai cố ý;
    #    · thiếu bước mới -> bảng có cột trống, chạy bước đó thì KeyError.
    batch["config"] = {**DEFAULT_CONFIG, **(batch.get("config") or {})}
    batch.setdefault("log", [])
    for item in batch.get("items") or []:
        item.setdefault("voice", "")
        item.setdefault("giay", 0)
        item.setdefault("mb", 0)
        steps = item.setdefault("steps", {})
        for st in STEPS:
            steps.setdefault(st, {"status": "pending", "error": "", "at": 0.0})
    return batch


def list_all() -> list:
    out = []
    for name in os.listdir(_batches_dir()):
        if not name.endswith(".json"):
            continue
        b = load(name[:-5])
        if b:
            out.append(b)
    out.sort(key=lambda b: b.get("created_at") or 0, reverse=True)
    return out


def delete(batch_id: str) -> bool:
    """Xoá LÔ, KHÔNG xoá dự án — lô chỉ là cái danh sách việc."""
    try:
        os.remove(_path(batch_id))
        return True
    except OSError:
        return False


def add_items(batch: dict, projects: list) -> dict:
    """Thêm dự án vào lô. ``projects`` = list ``{"project_id", "title"}``.

    Bỏ qua dự án đã có trong lô: thêm trùng thì bước chạy hai lần trên cùng một
    dự án, lần sau ghi đè kết quả lần trước mà nhìn bảng không thấy gì bất thường.
    """
    co_san = {i["project_id"] for i in batch["items"]}
    #  VIDEO MỚI LÊN ĐẦU DANH SÁCH, không xuống cuối.
    #
    #  Vì sao: lô chạy lâu thì người dùng cứ thêm video vào giữa chừng, mà thứ vừa
    #  thêm là thứ họ đang muốn nhìn (đặt giọng riêng, xem đã nhận đúng video
    #  chưa). Nằm cuối thì lô vài chục video là phải cuộn xuống đáy mỗi lần thêm.
    #  Giữ nguyên thứ tự trong CÙNG một lượt thêm (chọn 5 video thì vẫn đúng thứ
    #  tự đó), chỉ cả cụm mới nằm trên cụm cũ.
    moi = []
    for p in projects:
        pid = str(p.get("project_id") or "").strip()
        if not pid or pid in co_san:
            continue
        co_san.add(pid)
        moi.append({
            "project_id": pid,
            "title": (p.get("title") or "").strip() or pid,
            #  DÀI BAO NHIÊU + NẶNG BAO NHIÊU — đọc MỘT LẦN lúc thêm vào lô rồi nhớ
            #  luôn. Đây là hai con số người ta nhìn để ước "lô này chạy mất bao lâu"
            #  và "ổ còn đủ chỗ không". Đọc lại mỗi nhịp hỏi trạng thái thì lô vài
            #  trăm video là vài trăm lượt đọc tệp mỗi 2 giây.
            "giay": float(p.get("giay") or 0),
            "mb": float(p.get("mb") or 0),
            "added_at": _now(),
            #  GIỌNG RIÊNG của video này. Rỗng = dùng giọng chung của lô (khoá
            #  ``config["voice"]``); rỗng CẢ HAI = tôn trọng giọng dự án đã đặt.
            "voice": "",
            "steps": _blank_steps(),
        })
    batch["items"] = moi + batch["items"]
    return save(batch)


def set_item_voice(batch: dict, project_id: str, voice: str) -> dict:
    """Đặt GIỌNG RIÊNG cho một video trong lô (rỗng = theo giọng chung của lô).

    Vì sao để ở LÔ chứ không ghi thẳng vào dự án: ghi vào dự án thì lần chạy sau
    giọng chung của lô lại đè lên, người dùng đặt riêng bao nhiêu lần cũng mất.
    Ở đây lô nhớ lấy, và lúc chạy nó mới ghi xuống dự án.
    """
    item = find_item(batch, project_id)
    if item is None:
        raise ValueError(L("Dự án không có trong lô", "Project is not in the batch"))
    item["voice"] = (voice or "").strip()
    return save(batch)


def remove_item(batch: dict, project_id: str) -> dict:
    batch["items"] = [i for i in batch["items"] if i["project_id"] != project_id]
    return save(batch)


def find_item(batch: dict, project_id: str) -> Optional[dict]:
    for i in batch["items"]:
        if i["project_id"] == project_id:
            return i
    return None


def ghi_log(batch: dict, text: str, project_id: str = "", step: str = "") -> None:
    """Thêm một dòng nhật ký (KHÔNG tự lưu — nơi gọi lưu một lượt)."""
    nk = batch.setdefault("log", [])
    nk.append({"luc": _now(), "text": text[:300], "project_id": project_id,
               "step": step})
    if len(nk) > LOG_TOI_DA:
        del nk[: len(nk) - LOG_TOI_DA]


def set_step(batch: dict, project_id: str, step: str, status: str,
             error: str = "") -> dict:
    if step not in STEPS:
        raise ValueError(L(f"Bước không hợp lệ: {step}", f"Invalid step: {step}"))
    if status not in STATUSES:
        raise ValueError(L(f"Trạng thái không hợp lệ: {status}", f"Invalid status: {status}"))
    item = find_item(batch, project_id)
    if item:
        cu = item["steps"].get(step) or {}
        moi = {"status": status, "error": error[:500], "at": _now()}
        if status == "running":
            #  MỐC BẮT ĐẦU để tính "chạy hết bao lâu". Thiếu nó thì nhìn bảng chỉ
            #  biết xong/chưa, không biết video nào ngốn thời gian — mà đó chính là
            #  thứ người ta cần khi ước lượng lô 20 video chạy mất bao lâu.
            moi["bat_dau"] = _now()
        else:
            bd = float(cu.get("bat_dau") or 0)
            if bd:
                moi["bat_dau"] = bd
                moi["giay"] = round(_now() - bd, 1)
        item["steps"][step] = moi

        if status != "running":
            ten = item.get("title") or project_id
            nhan = STEP_LABEL.get(step, step)
            mo_ta = {
                "done": L("xong", "done"), "failed": L("LỖI", "ERROR"), "needs_me": L("cần bạn quyết", "needs your decision"),
                "busy": L("bận — để sau", "busy — later"), "skipped": L("bỏ qua", "skipped"),
            }.get(status, status)
            giay = moi.get("giay")
            ghi_log(
                batch,
                f"{nhan} — {ten}: {mo_ta}"
                + (f" ({giay:g}s)" if giay else "")
                + (f" — {error}" if error else ""),
                project_id, step,
            )
    return save(batch)


def pending_for(batch: dict, step: str, force: bool = False) -> list:
    """Những dự án CÒN PHẢI CHẠY ở bước này (theo thứ tự trong lô).

    Mặc định bỏ qua cái đã ``done`` và cái người dùng ``skipped``. ``busy`` (dự án
    lúc đó đang bận việc khác) CÓ nhặt lại — đó mới là lý do nó không phải là lỗi.
    Bấm lại nút
    chạy sau khi vài dự án lỗi thì chỉ chạy lại phần lỗi, không làm lại từ đầu
    (một lô 20 video chạy lại toàn bộ là mất hàng giờ). ``force`` = chạy lại tất.
    """
    out = []
    for i in batch["items"]:
        st = (i["steps"].get(step) or {}).get("status", "pending")
        if force or st not in ("done", "skipped"):
            out.append(i["project_id"])
    return out


def counts(batch: dict, step: str) -> dict:
    """Đếm theo trạng thái cho một bước — dùng cho thanh tiến độ trên trang."""
    out = {s: 0 for s in STATUSES}
    for i in batch["items"]:
        st = (i["steps"].get(step) or {}).get("status", "pending")
        out[st] = out.get(st, 0) + 1
    return out


def failures(batch: dict, step: str) -> list:
    """Danh sách dự án lỗi ở một bước, kèm lý do — bảng tổng kết cuối lô."""
    out = []
    for i in batch["items"]:
        s = i["steps"].get(step) or {}
        if s.get("status") in ("failed", "needs_me", "busy"):
            out.append({
                "project_id": i["project_id"],
                "title": i["title"],
                "status": s.get("status"),
                "error": s.get("error") or "",
            })
    return out


def is_running(batch: dict) -> bool:
    return bool((batch.get("run") or {}).get("step"))


def start_run(batch: dict, step: str, so_du_an: int = 0) -> dict:
    if step not in STEPS:
        raise ValueError(L(f"Bước không hợp lệ: {step}", f"Invalid step: {step}"))
    batch["run"] = {"step": step, "project_id": "", "started_at": _now(),
                    "cancel": False}
    ghi_log(batch, L(f"▶ Bắt đầu {STEP_LABEL.get(step, step)}", f"▶ Starting {STEP_LABEL.get(step, step)}")
                   + (L(f" cho {so_du_an} dự án", f" for {so_du_an} projects") if so_du_an else ""), step=step)
    return save(batch)


def mark_current(batch: dict, project_id: str) -> dict:
    batch.setdefault("run", {})["project_id"] = project_id
    return save(batch)


def end_run(batch: dict) -> dict:
    run = batch.get("run") or {}
    step = run.get("step") or ""
    bd = float(run.get("started_at") or 0)
    if step:
        c = counts(batch, step)
        ghi_log(
            batch,
            L(f"■ Xong lượt {STEP_LABEL.get(step, step)}", f"■ Finished run of {STEP_LABEL.get(step, step)}")
            + (f" sau {round(_now() - bd)}s" if bd else "")
            + f" — {c.get('done', 0)} xong"
            + (L(f", {c.get('failed', 0)} lỗi", f", {c.get('failed', 0)} errors") if c.get("failed") else "")
            + (L(f", {c.get('needs_me', 0)} cần bạn quyết", f", {c.get('needs_me', 0)} need your decision") if c.get("needs_me") else "")
            + (L(f", {c.get('busy', 0)} bận", f", {c.get('busy', 0)} busy") if c.get("busy") else ""),
            step=step,
        )
    batch["run"] = {"step": "", "project_id": "", "started_at": 0.0, "cancel": False}
    return save(batch)


def request_cancel(batch_id: str) -> bool:
    """Đặt cờ huỷ. Đọc lại TỪ ĐĨA rồi ghi ngay: tiến trình chạy đang giữ một bản
    ``batch`` trong bộ nhớ, sửa bản đó thì nó không bao giờ thấy cờ."""
    b = load(batch_id)
    if not b:
        return False
    b.setdefault("run", {})["cancel"] = True
    save(b)
    return True


def cancel_requested(batch_id: str) -> bool:
    b = load(batch_id)
    return bool(b and (b.get("run") or {}).get("cancel"))


def reset_stale_runs() -> int:
    """Dọn LƯỢT CHẠY DANG DỞ còn sót từ lần chạy trước của ứng dụng.

    Trạng thái lô nằm dưới đĩa, còn tiến trình chạy thì nằm trong BỘ NHỚ của
    backend. Đóng app (hoặc máy sập, hoặc backend khởi động lại) giữa chừng thì tệp
    lô vẫn ghi "đang chạy bước X, dự án Y" trong khi chẳng còn ai chạy: mở lại app
    là mọi nút bị khoá vĩnh viễn, bấm Huỷ cũng không ăn vì không có gì để huỷ.

    Gọi MỘT LẦN lúc backend khởi động (xem routes/batch_routes.py). Dự án đang dở
    được đánh dấu LỖI kèm lý do thật, không im lặng đặt về "chưa chạy": việc đó có
    thể đã ghi ra nửa vời (tách giọng dở, video xuất dở) và người dùng cần biết mà
    chạy lại.
    """
    n = 0
    for batch in list_all():
        if not is_running(batch):
            continue
        for item in batch.get("items") or []:
            for st in STEPS:
                if (item["steps"].get(st) or {}).get("status") == "running":
                    item["steps"][st] = {
                        "status": "failed",
                        "error": L("Bị ngắt giữa chừng (ứng dụng đóng hoặc khởi động lại) — hãy chạy lại bước này", "Interrupted midway (app closed or restarted) — run this step again"),
                        "at": _now(),
                    }
        batch["run"] = {"step": "", "project_id": "", "started_at": 0.0,
                        "cancel": False}
        save(batch)
        n += 1
    return n


def set_config(batch: dict, patch: dict) -> dict:
    """Ghi các khoá cấu hình hợp lệ; khoá lạ thì bỏ qua.

    DANH SÁCH KHOÁ HỢP LỆ LẤY TỪ ``DEFAULT_CONFIG``, không chép tay lần hai.
    Trước đây đây là một tuple viết tay, và thêm khoá mới mà quên sửa nó thì ô chọn
    trên màn hình BẤM KHÔNG ĂN GÌ — không lỗi, không cảnh báo, giá trị cũ hiện lại
    như chưa có chuyện gì (đúng chuyện vừa xảy ra với "Nguồn soạn bài đăng" và mấy ô
    ảnh bìa). Một danh sách duy nhất thì không có gì để quên.
    """
    for k, v in (patch or {}).items():
        if k in DEFAULT_CONFIG:
            batch["config"][k] = v
    return save(batch)
