# -*- coding: utf-8 -*-
"""SÁNG TẠO — "Nhờ AI viết kịch bản" bằng API (07-10-2026), thay cho dán prompt vào ChatGPT / Gemini.

Nguyên tắc: **AI chỉ thay bàn tay dán.** Việc nền tạo ra đúng *văn bản JSON* mà người dùng lẽ ra phải chép từ AI về; mọi kiểm tra, ghép
cảnh, cảnh báo, đối chiếu nguyên văn vẫn đi qua đường "Dán & nhận cảnh" hiện có ở giao diện (``nhanPhan``). Cụ thể:

- Prompt từng phần lấy từ CÙNG hàm với nút "Tạo prompt" (``sangtao_prompt.cac_phan``) — hai đường ra một prompt.
- Gọi TUẦN TỰ, mỗi phần nối vào LỊCH SỬ hội thoại (prompt phần 2+ ghi "gửi lần lượt trong CÙNG cuộc trò chuyện"; thông tin YouTube
  chỉ ở phần cuối, AI phải thấy các phần trước). Không song song — cũng vừa gói Gemini miễn phí 5 lượt/phút.
- Mỗi phần xong: kiểm bằng ``sangtao_phan_tich.phan_tich`` (y như dán tay); hỏng thì DỪNG, báo lỗi phần đó — prompt vẫn nằm sẵn để dán tay.
- Ghi ``ai_kich_ban.json`` trong thư mục dự án sau từng phần (khởi động lại app không mất; giao diện tự đưa phần đã xong vào ``nhanPhan``).
- Bộ AI: model ``provider:model`` người dùng chọn hoặc model mặc định của bộ đang chọn trong Cài đặt; ``suy_nghi="low"`` (viết kịch bản
  cần suy nghĩ chút; "none" dành cho dịch / định dạng).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("SangtaoAiKichBan")

TEP = "ai_kich_ban.json"
#: mỗi phần: trần token đầu ra + thời gian chờ (kịch bản một phần ≤ 6 000 ký tự bài ra JSON ~10–25 k ký tự)
MAX_TOKENS = 65536
TIMEOUT_GIAY = 600
VIEC: dict = {}
_KHOA = threading.Lock()


def _thu_muc(pid: str) -> str:
    from services import sangtao_du_an as DA

    return DA.thu_muc(pid)


def _tep(pid: str) -> str:
    return os.path.join(_thu_muc(pid), TEP)


#  Đọc / ghi tệp qua MỘT khoá: giao diện hỏi tiến độ (đọc) đúng lúc luồng nền ghi xong một phần (``os.replace``) thì Windows từ chối
#  thay tệp đang mở ("Access is denied") — việc nền chết oan ở phần 1. Ngoài khoá còn thử lại vài lần phòng trình quét virus giữ tệp.
_KHOA_TEP = threading.Lock()


def doc_tep(pid: str) -> dict:
    with _KHOA_TEP:
        try:
            with open(_tep(pid), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}


def _ghi_tep(pid: str, d: dict) -> None:
    p = _tep(pid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tam = p + ".tmp"
    with _KHOA_TEP:
        with open(tam, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        for lan in range(5):
            try:
                os.replace(tam, p)
                return
            except PermissionError:
                if lan == 4:
                    raise
                time.sleep(0.1 * (lan + 1))


def trang_thai(pid: str) -> dict:
    """Tiến độ cho giao diện: ``state`` idle / running / done / error / cancelled · ``tong`` số phần · ``phan`` {i: {raw, luc, so_canh}} các phần
    đã xong · ``dang`` phần đang viết · ``loi`` · ``model`` · ``giay`` đã chạy. Dự án không có → ``KeyError``."""
    from services import sangtao_du_an as DA

    DA.doc(pid)                                                                 # KeyError nếu không có dự án
    d = doc_tep(pid)
    v = VIEC.get(pid)
    ra = {"state": d.get("state") or "idle", "tong": int(d.get("tong") or 0), "phan": d.get("phan") or {}, "dang": None,
          "loi": d.get("loi"), "model": d.get("model") or "", "giay": 0.0, "luc": d.get("luc"), "ky": d.get("ky") or ""}
    if v:
        ra.update(state=v["state"], tong=v["tong"], dang=v.get("dang"), loi=v.get("loi"), model=v["model"],
                  giay=round(time.time() - v["bat_dau"], 1), giay_phan=round(time.time() - (v.get("bat_dau_phan") or v["bat_dau"]), 1))
    elif ra["state"] == "running":
        #  Tệp ghi "running" mà không có luồng nào: app đã tắt / khởi động lại giữa chừng (luồng chết theo tiến trình, tệp còn).
        #  Không sửa thì giao diện hiện "đang viết" + đồng hồ mãi mãi. Báo lỗi rõ; phần đã xong vẫn nằm trong ``phan`` để nhận.
        ra["state"] = "error"
        ra["loi"] = (f"App đã tắt hoặc khởi động lại giữa chừng (đã xong {len(ra['phan'])}/{ra['tong']} phần). "
                     "Phần đã xong vẫn được nhận; bấm “Nhờ AI viết kịch bản” lại ở bước 1 để viết tiếp.")
    return ra


def bat_dau(pid: str, tuy_chon: dict, model: str = "", ky: str = "") -> None:
    """Mở việc nền. ``tuy_chon``: tham số của ``sangtao_prompt.cac_phan`` (từ body ``/prompt``); ``ky``: chữ ký đầu vào do giao diện đặt (trả lại
    nguyên trong tiến độ). ``ValueError`` nếu thiếu khoá / bài trống / tuỳ chọn sai; ``RuntimeError`` nếu đang chạy."""
    import services.deepseek_service as DS
    from services import sangtao_du_an as DA
    from services import sangtao_prompt as PR
    from services.ai_engines import tach_model, ten_bo_ai
    from services.config_service import get_config_service

    DA.doc(pid)
    cs = get_config_service()
    ma = str(model or "").strip() or cs.ai_model_mac_dinh()
    provider, _m = tach_model(ma)
    if not DS.is_configured(provider):
        raise ValueError(f"Chưa có API key {ten_bo_ai(provider)} — vào Cài đặt nhập key, hoặc dùng nút \"Tạo prompt & chép\" rồi dán tay.")
    phan = PR.cac_phan(**tuy_chon)
    if not phan:
        raise ValueError("Nội dung bài trống")
    mac_dinh = {"language": tuy_chon.get("language"), "aspect": tuy_chon.get("aspect"), "audience": tuy_chon.get("audience")}
    with _KHOA:
        v = VIEC.get(pid)
        if v and v["state"] == "running":
            raise RuntimeError("AI đang viết kịch bản cho dự án này")
        huy = threading.Event()
        v = {"state": "running", "tong": len(phan), "dang": 1, "loi": None, "model": ma, "huy": huy, "bat_dau": time.time(),
             "bat_dau_phan": time.time()}
        VIEC[pid] = v
    _ghi_tep(pid, {"state": "running", "tong": len(phan), "phan": {}, "model": ma, "ky": ky, "loi": None, "luc": time.time()})

    def chay() -> None:
        from services import sangtao_phan_tich as PT

        lich_su: list = []
        xong: dict = {}
        try:
            for p in phan:
                i = int(p["i"])
                if huy.is_set():
                    raise _Huy()
                v.update(dang=i, bat_dau_phan=time.time())
                logger.info(f"[Sáng tạo] AI viết kịch bản {pid}: phần {i}/{len(phan)} (model {ma})…")
                tin = lich_su + [{"role": "user", "content": p["prompt"]}]
                raw = DS._chat(tin, ma, timeout=TIMEOUT_GIAY, max_retries=2, max_tokens=MAX_TOKENS, temperature=1.0, suy_nghi="low")
                if huy.is_set():
                    raise _Huy()
                kq = PT.phan_tich(raw, mac_dinh=mac_dinh)
                if not kq.ok:
                    v.update(state="error", loi=f"Phần {i}: AI trả JSON không nhận được — " + "; ".join((kq.loi or [])[:3]), dang=None)
                    _ghi_tep(pid, {"state": "error", "tong": len(phan), "phan": xong, "model": ma, "ky": ky, "loi": v["loi"], "luc": time.time(),
                                   "raw_loi": {str(i): raw[:20000]}})
                    return
                xong[str(i)] = {"raw": raw, "luc": time.time(), "so_canh": len(kq.storyboard.scenes)}
                _ghi_tep(pid, {"state": "running", "tong": len(phan), "phan": xong, "model": ma, "ky": ky, "loi": None, "luc": time.time()})
                lich_su = tin + [{"role": "assistant", "content": raw}]
            v.update(state="done", dang=None)
            _ghi_tep(pid, {"state": "done", "tong": len(phan), "phan": xong, "model": ma, "ky": ky, "loi": None, "luc": time.time()})
            logger.info(f"[Sáng tạo] AI viết kịch bản {pid}: xong {len(phan)} phần.")
        except _Huy:
            v.update(state="cancelled", dang=None)
            _ghi_tep(pid, {"state": "cancelled", "tong": len(phan), "phan": xong, "model": ma, "ky": ky, "loi": None, "luc": time.time()})
        except Exception as e:  # noqa: BLE001
            loi = getattr(e, "user_message", "") or str(e) or e.__class__.__name__
            logger.error(f"[Sáng tạo] AI viết kịch bản {pid} lỗi ở phần {v.get('dang')}: {loi}")
            v.update(state="error", loi=f"Phần {v.get('dang')}: {loi}", dang=None)
            _ghi_tep(pid, {"state": "error", "tong": len(phan), "phan": xong, "model": ma, "ky": ky, "loi": v["loi"], "luc": time.time()})

    t = threading.Thread(target=chay, name=f"sangtao-ai-{pid}", daemon=True)
    v["thread"] = t
    t.start()


class _Huy(Exception):
    pass


def huy(pid: str) -> bool:
    v = VIEC.get(pid)
    if v and v["state"] == "running":
        v["huy"].set()
        return True
    return False


def xoa(pid: str) -> None:
    """Bỏ kết quả AI đã ghi (khi người dùng đổi bài / tạo prompt mới)."""
    VIEC.pop(pid, None)
    try:
        os.remove(_tep(pid))
    except OSError:
        pass


def dang_chay(pid: str) -> Optional[dict]:
    v = VIEC.get(pid)
    return v if v and v["state"] == "running" else None
