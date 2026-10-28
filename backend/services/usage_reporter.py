"""Báo cáo thống kê sử dụng theo DỰ ÁN + NGÀY, chống mất số liệu.

Mô hình (chốt với chủ dự án 2026-07-18):
- KHÔNG gửi ngay khi tạo xong video. Ghi trạng thái vào 1 file JSON cục bộ, rồi một
  worker nền gửi định kỳ (mỗi 30') + gửi khi mở app. Nhờ vậy tạo xong video mà lúc
  gửi bị lỗi mạng/server thì số liệu KHÔNG mất — lần sau gửi lại.
- Đếm theo TỪNG DỰ ÁN, theo NGÀY hoàn thành:
    * completions (dubbed/review): số lần dự án chuyển sang HOÀN THÀNH (sinh/render
      lại cũng +1).
    * voices: số voice sinh ra — đếm tạm theo project (voice_pending), khi dự án
      hoàn thành mới gộp vào bản ghi ngày đó rồi xoá phần tạm.
- Mỗi thay đổi (hoàn thành mới HOẶC xoá) → đặt sent=False. Xoá cũng gửi lên
  (deleted=True) để server biết trạng thái.
- Gửi bằng UPSERT ghi đè theo (key, project_id, date): cùng dự án + cùng ngày → ghi
  đè; khác ngày → dòng mới. Gửi lại nhiều lần không phình.

File JSON:
{
  "records": { "<project_id>|<YYYY-MM-DD>": {project_id, date, dubbed, review,
               voices, deleted, sent} },
  "voice_pending": { "<project_id>": <int> },
  "voice_pending_lo": { "<project_id>": <int> }   // phần do CHẠY HÀNG LOẠT
}
"""

import os
import json
import time
import logging
import threading
import urllib.request
import urllib.error
from datetime import date as _date

from models.database import get_app_data_dir

logger = logging.getLogger("UsageReporter")

_LOCK = threading.RLock()
_WORKER_STARTED = False
_FLUSH_INTERVAL_SECS = 30 * 60  # 30 phút

# Log lỗi: gộp theo NGÀY (project_id sentinel dưới đây) rồi gửi CÙNG payload với số
# liệu voice/review/lồng-tiếng. Cắt trần + dedupe để không phình/spam server.
_ERRORLOG_PID = "__errorlog__"
_MAX_ERRORLOG_CHARS = 8000
_MAX_MSG_CHARS = 2000
_last_error_msg = None  # dedupe message lặp liên tiếp


def _store_path() -> str:
    return os.path.join(get_app_data_dir(), "usage_reports.json")


def _today() -> str:
    return _date.today().isoformat()  # "YYYY-MM-DD"


def _rec_key(project_id: str, day: str) -> str:
    return f"{project_id}|{day}"


def _app_version() -> str:
    """Phiên bản app lúc số liệu/log phát sinh. Import trễ để tránh vòng import."""
    try:
        from routes.auth_routes import get_app_version
        return get_app_version()
    except Exception:
        return "unknown"


def _load() -> dict:
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except Exception as e:
        logger.warning(f"Không đọc được usage_reports, khởi tạo lại: {e}")
        data = {}
    data.setdefault("records", {})
    data.setdefault("voice_pending", {})
    data.setdefault("voice_pending_lo", {})
    return data


def _save(data: dict) -> None:
    try:
        path = _store_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)  # ghi nguyên tử, không hỏng khi tắt đột ngột
    except Exception as e:
        logger.warning(f"Không lưu được usage_reports: {e}")


# ------------------------------------------------------------------ ghi số liệu

def _do_lo(project_id: str) -> bool:
    """Việc này có phải do CHẾ ĐỘ CHẠY HÀNG LOẠT làm không.

    Hỏi thẳng sổ ``batch_lock`` — sổ đó ghi "lô đang giữ dự án nào" trong suốt lúc
    lô chạy một bước. Nhờ vậy không phải nối thêm tham số qua chục lớp gọi hàm
    (sinh giọng, dựng, xuất đều đi qua những hàm dùng chung cho cả hai đường).

    Vì sao phải phân biệt: gói 399k có trần RIÊNG cho phần chạy qua lô, còn chế độ
    cơ bản và Chi tiết thì không giới hạn. Lấy tổng rồi trừ ra là khách chạy tay
    nhiều sẽ bị khoá lô oan.
    """
    try:
        from services import batch_lock as BL

        return bool(BL.dang_giu(project_id))
    except Exception:  # noqa: BLE001 — không biết thì coi như chạy tay (chặt hơn)
        return False


def bump_voice(project_id: str, n: int = 1) -> None:
    """Mỗi lần TTS sinh 1 câu thành công. Chỉ đếm TẠM theo project (chưa gửi)."""
    if not project_id or n <= 0:
        return
    lo = _do_lo(project_id)
    with _LOCK:
        data = _load()
        if lo:
            #  GIỌNG SINH TRONG LÔ: ghi THẲNG vào bản ghi của ngày, không treo ở "tạm".
            #
            #  Phần "tạm" (voice_pending) chỉ được gộp khi dự án HOÀN THÀNH (xuất xong).
            #  Với hạn mức lô thì thế là sai: người dùng tick Sinh giọng cho 20 video
            #  và nhìn thanh "Lượt tạo giọng" đứng im ở 0 suốt buổi — trong khi video
            #  nào chưa xuất thì giọng của nó không bao giờ được tính. Lượt giọng là thứ
            #  ĐÃ TIÊU ngay lúc sinh, nên tính ngay lúc sinh.
            #
            #  Ghi cả ``voices`` lẫn ``voices_lo``: máy chủ kẹp batch_voices <= voices,
            #  ghi một cột thì cột kia kéo về 0. Không đụng voice_pending để không bị
            #  đếm đôi khi dự án hoàn thành.
            day = _today()
            rk = _rec_key(project_id, day)
            rec = data["records"].get(rk) or {
                "project_id": project_id, "date": day, "dubbed": 0, "review": 0,
                "voices": 0, "videos_lo": 0, "voices_lo": 0, "deleted": False, "sent": False,
            }
            rec["voices"] = int(rec.get("voices", 0)) + int(n)
            rec["voices_lo"] = int(rec.get("voices_lo", 0)) + int(n)
            rec["app_version"] = _app_version()
            rec["deleted"] = False
            rec["sent"] = False
            data["records"][rk] = rec
        else:
            vp = data["voice_pending"]
            vp[project_id] = int(vp.get(project_id, 0)) + int(n)
        _save(data)


def record_completion(project_id: str, kind: str) -> None:
    """Dự án chuyển sang HOÀN THÀNH. kind = 'dubbed' | 'review'. Cộng +1 vào loại
    tương ứng của bản ghi (project, hôm nay), gộp voice tạm của project vào ngày đó,
    rồi đặt sent=False để worker gửi lên."""
    if not project_id or kind not in ("dubbed", "review"):
        return
    do_lo = _do_lo(project_id)
    with _LOCK:
        data = _load()
        day = _today()
        rk = _rec_key(project_id, day)
        rec = data["records"].get(rk) or {
            "project_id": project_id,
            "date": day,
            "dubbed": 0,
            "review": 0,
            "voices": 0,
            #  Phần do CHẠY HÀNG LOẠT làm ra (luôn <= số tổng bên trên).
            "videos_lo": 0,
            "voices_lo": 0,
            "deleted": False,
            "sent": False,
        }
        rec[kind] = int(rec.get(kind, 0)) + 1
        rec["app_version"] = _app_version()
        # Gộp voice đếm tạm của project vào NGÀY hoàn thành, rồi xoá phần tạm.
        pending = int(data["voice_pending"].pop(project_id, 0))
        if pending:
            rec["voices"] = int(rec.get("voices", 0)) + pending
        pending_lo = int(data.setdefault("voice_pending_lo", {}).pop(project_id, 0))
        if pending_lo:
            rec["voices_lo"] = int(rec.get("voices_lo", 0)) + pending_lo
        #  Video này do lô xuất hay do người dùng tự bấm — quyết định ngay tại đây
        #  vì đúng lúc này sổ ``batch_lock`` còn đang giữ dự án.
        if do_lo:
            rec["videos_lo"] = int(rec.get("videos_lo", 0)) + 1
        rec["deleted"] = False
        rec["sent"] = False
        data["records"][rk] = rec
        _save(data)
    # KHÔNG gửi ngay: worker nền lo gửi (mỗi 30' + khi mở app). Chỉ cần đã ghi local.


def mark_deleted(project_id: str) -> None:
    """Dự án bị xoá ở client. Đánh dấu deleted=True + sent=False cho MỌI bản ghi của
    project (mọi ngày) để gửi trạng thái xoá lên. Cũng bỏ voice tạm còn treo."""
    if not project_id:
        return
    with _LOCK:
        data = _load()
        data["voice_pending"].pop(project_id, None)
        data.setdefault("voice_pending_lo", {}).pop(project_id, None)
        touched = False
        for rk, rec in data["records"].items():
            if rec.get("project_id") == project_id and not rec.get("deleted"):
                rec["deleted"] = True
                rec["sent"] = False
                touched = True
        if touched:
            _save(data)
    # KHÔNG gửi ngay: worker nền lo gửi (mỗi 30' + khi mở app).


def record_error(message: str, project_id: str | None = None) -> None:
    """Ghi 1 dòng log lỗi để gửi lên server (bảng api_key_usage_logs, cột error_log).

    Gộp theo NGÀY (mặc định bucket `__errorlog__`; truyền project_id nếu muốn gắn theo
    dự án). Cắt trần tổng + dedupe message lặp liên tiếp để tránh phình/spam. Đặt
    sent=False để worker gửi kèm số liệu voice/review/lồng-tiếng trong cùng chu kỳ."""
    global _last_error_msg
    msg = (message or "").strip()
    if not msg:
        return
    if len(msg) > _MAX_MSG_CHARS:
        msg = msg[:_MAX_MSG_CHARS] + "…"
    with _LOCK:
        # Bỏ qua message trùng ngay trước đó (tránh spam khi 1 lỗi log lặp nhiều lần).
        if msg == _last_error_msg:
            return
        _last_error_msg = msg
        data = _load()
        day = _today()
        pid = project_id or _ERRORLOG_PID
        rk = _rec_key(pid, day)
        rec = data["records"].get(rk) or {
            "project_id": pid,
            "date": day,
            "dubbed": 0,
            "review": 0,
            "voices": 0,
            "error_log": "",
            "deleted": False,
            "sent": False,
        }
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        prev = rec.get("error_log") or ""
        combined = (prev + "\n" + line) if prev else line
        if len(combined) > _MAX_ERRORLOG_CHARS:
            combined = combined[-_MAX_ERRORLOG_CHARS:]
        rec["error_log"] = combined
        rec["app_version"] = _app_version()
        rec["sent"] = False
        data["records"][rk] = rec
        _save(data)


# --------------------------------------------------------------------- gửi lên

def _build_payload(records: list) -> dict:
    from services.config_service import get_config_service
    from routes.auth_routes import get_device_info, get_app_version

    cfg = get_config_service()
    now_version = get_app_version()
    return {
        "key": cfg.get_api_key(),
        "device_id": cfg.get_device_id(),
        "device_info": get_device_info(),
        # Phiên bản app cho CẢ gói gửi (dùng khi bản ghi cũ chưa kịp đóng dấu).
        "app_version": now_version,
        "items": [
            {
                "project_id": r["project_id"],
                "date": r["date"],
                "dubbed": int(r.get("dubbed", 0)),
                "review": int(r.get("review", 0)),
                "voices": int(r.get("voices", 0)),
                #  Server dùng hai số này cho trần gói 399k. Bản server cũ chưa
                #  biết khoá này thì bỏ qua — không làm hỏng lượt gửi.
                "batch_videos": int(r.get("videos_lo", 0)),
                "batch_voices": int(r.get("voices_lo", 0)),
                "error_log": r.get("error_log", "") or "",
                # Đóng dấu TỪNG bản ghi theo phiên bản lúc số liệu/log phát sinh —
                # người dùng cập nhật app giữa chừng thì log cũ vẫn mang bản cũ.
                "app_version": r.get("app_version") or now_version,
                "deleted": bool(r.get("deleted", False)),
            }
            for r in records
        ],
    }


def flush() -> bool:
    """Gửi mọi bản ghi sent=False lên server. 2xx → đánh dấu sent=True. Lỗi/mạng →
    giữ nguyên để lần sau thử lại. Trả True nếu gửi thành công (hoặc không có gì)."""
    from services.config_service import get_config_service

    with _LOCK:
        data = _load()
        unsent = [r for r in data["records"].values() if not r.get("sent")]
        # Chụp "vân tay" nội dung từng bản ghi TẠI THỜI ĐIỂM GỬI. Trong lúc POST
        # (tối đa 15s, ngoài lock) record_error/record_completion có thể ghi thêm
        # vào cùng bucket — nếu đánh dấu sent mù quáng sau đó thì phần ghi thêm
        # KHÔNG BAO GIỜ được gửi. Chỉ mark sent khi nội dung không đổi.
        sent_fingerprints = {
            _rec_key(r["project_id"], r["date"]): json.dumps(
                {k: v for k, v in r.items() if k != "sent"}, sort_keys=True
            )
            for r in unsent
        }
    if not unsent:
        return True

    cfg = get_config_service()
    admin_url = cfg.get_admin_url()
    api_key = cfg.get_api_key()
    if not admin_url or not api_key:
        return False  # chưa kích hoạt / chưa cấu hình → để lần sau

    url = f"{admin_url.rstrip('/')}/api/report-usage"
    payload = json.dumps(_build_payload(unsent)).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "SubForge-Desktop-Client",
    }
    secret = os.environ.get("SUBFORGE_DESKTOP_SECRET")
    if secret:
        headers["X-SubForge-Secret"] = secret

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # 4xx (vd key sai) — không nên retry vô hạn; log rồi coi như thất bại lần này.
        logger.warning(f"report-usage HTTP {e.code}: {e.reason}")
        return False
    except Exception as e:
        logger.info(f"report-usage chưa gửi được (sẽ thử lại): {e}")
        return False

    if not ok:
        return False

    # Thành công → đánh dấu sent=True cho đúng các bản ghi vừa gửi, NHƯNG chỉ khi
    # nội dung không bị ghi thêm trong lúc POST (so vân tay). Bản ghi đã đổi thì
    # giữ sent=False — lần flush sau UPSERT ghi đè cả bản mới, không mất dữ liệu.
    with _LOCK:
        data = _load()
        for rk, fp in sent_fingerprints.items():
            rec = data["records"].get(rk)
            if not rec:
                continue
            cur_fp = json.dumps(
                {k: v for k, v in rec.items() if k != "sent"}, sort_keys=True
            )
            if cur_fp == fp:
                rec["sent"] = True
        # Dọn bớt: bản ghi đã gửi VÀ đã đánh dấu deleted thì không cần giữ nữa.
        # Bucket log lỗi của NGÀY CŨ đã gửi xong cũng xoá (tránh phình file cục bộ).
        today = _today()
        for rk in list(data["records"].keys()):
            r = data["records"][rk]
            if r.get("sent") and r.get("deleted"):
                data["records"].pop(rk, None)
            elif (
                r.get("sent")
                and r.get("project_id") == _ERRORLOG_PID
                and r.get("date") != today
            ):
                data["records"].pop(rk, None)
        _save(data)
    logger.info(f"Đã gửi {len(unsent)} bản ghi thống kê sử dụng.")
    #  VỪA GỬI SỐ LIỆU XONG -> XOÁ CACHE XÁC THỰC, để lượt hỏi kế tiếp lấy được hạn
    #  mức MỚI từ máy chủ.
    #
    #  Vì sao cần: kết quả xác thực (trong đó có ``quota``) được cache 30 phút. Không
    #  xoá thì thanh hạn mức trên màn hình vẫn là con số của trước lúc gửi — người
    #  dùng chạy xong một lô 40 video mà thấy mức dùng đứng yên, tưởng nó không đếm.
    try:
        from routes import auth_routes as _AU

        _AU._LAST_VERIFY_RESULT = None
        _AU._LAST_VERIFY_KEY = None
        _AU.LAST_VERIFIED_TIME = 0.0
    except Exception:  # noqa: BLE001 — không xoá được cache thì cũng đừng hỏng flush
        pass
    return True


def start_worker() -> None:
    """Khởi động worker nền: gửi khi mở app + lặp lại mỗi 30'. Gọi 1 lần lúc app khởi
    động. Idempotent (chỉ start 1 lần)."""
    global _WORKER_STARTED
    with _LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True

    def _loop():
        # Trễ nhẹ lúc mở app để backend/kích hoạt kịp sẵn sàng rồi mới gửi lần đầu.
        time.sleep(20)
        while True:
            try:
                flush()
            except Exception as e:
                logger.warning(f"Worker flush lỗi: {e}")
            time.sleep(_FLUSH_INTERVAL_SECS)

    threading.Thread(target=_loop, daemon=True).start()
    logger.info("UsageReporter worker đã khởi động (gửi mỗi 30').")
