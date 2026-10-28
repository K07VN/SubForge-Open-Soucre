"""GIỌNG MẪU DỰNG SẴN — bộ giọng clone đóng gói kèm app, dùng ngay không cần tự thu mẫu.

CƠ CHẾ: mỗi giọng là MỘT giọng clone VieNeu bình thường (``vieneu-clone:<tên>``) — cùng đường
sinh giọng, tự khớp thời lượng, xuất video như giọng người dùng tự thêm. Khác ở chỗ:
  · mẫu ``.wav`` + dấu vân giọng đã TÍNH SẴN nằm trong ``assets/voices`` (đóng gói kèm app) ->
    dùng được NGAY, không phải chờ ~3–20 s mã hoá mẫu ở lần đầu mỗi giọng;
  · được "gieo" vào ``registry.json`` của người dùng với cờ ``builtin`` — KHÔNG xoá được, KHÔNG
    bị người dùng đặt trùng tên, và được cập nhật khi app nâng cấp bộ giọng;
  · tách riêng một nhóm trong bộ chọn giọng ("Giọng mẫu của SubForge").
Như 20 giọng có sẵn của VieNeu, chúng chỉ ĐỌC được khi đã tải VieNeu (Quản lý Model AI): tệp
mẫu nhỏ nên đóng gói được, còn mô hình thì không.

BỐ CỤC ``assets/voices/``:
    manifest.json          {"version": 1, "voices": [{"id", "hien_thi", "gioi_tinh", "dac_diem",
                            "file", "emb", "sha256", "luu_y"}]}
    <file>.wav             mẫu đã chuẩn hoá (mono 24 kHz, ≤ 10 s — đúng như ``add_clone``)
    <emb>.json             {"engine": "<phiên bản vieneu>", "speaker_emb": [...], "codes": [...]}
Tạo / cập nhật bằng ``tools/dong_goi_giong_mau.py`` (dev); kiểm bằng tests/test_giong_mau_dung_san.py.

DẤU VÂN GIỌNG TÍNH SẴN chỉ dùng khi ``engine`` khớp phiên bản thư viện vieneu đang chạy; lệch
(thư viện nâng cấp) thì bỏ cache, lần dùng đầu tự mã hoá lại từ tệp .wav như giọng người dùng.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys

logger = logging.getLogger(__name__)

MANIFEST = "manifest.json"
_CACHE: dict = {"sig": None, "data": None}


def _override_dir() -> str:
    return os.environ.get("SUBFORGE_GIONG_MAU_DIR", "").strip()


def assets_dir() -> str:
    """Dev: backend/assets/voices; bản cài: _MEIPASS/assets/voices (build_backend đóng cả assets/)."""
    o = _override_dir()
    if o:
        return o
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "voices")


def _manifest_path() -> str:
    return os.path.join(assets_dir(), MANIFEST)


def _sig() -> tuple | None:
    try:
        st = os.stat(_manifest_path())
        return (assets_dir(), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def danh_sach() -> list[dict]:
    """Các giọng dựng sẵn HỢP LỆ (có tệp mẫu), theo thứ tự trong manifest. Rỗng nếu chưa có bộ giọng."""
    sig = _sig()
    if sig is None:
        return []
    if _CACHE["sig"] == sig:
        return _CACHE["data"]
    out: list[dict] = []
    try:
        with open(_manifest_path(), encoding="utf-8") as f:
            raw = json.load(f).get("voices", [])
    except (OSError, ValueError) as e:
        logger.warning(f"Giọng mẫu dựng sẵn: không đọc được manifest: {e}")
        raw = []
    seen = set()
    for v in raw:
        if not isinstance(v, dict):
            continue
        ten = str(v.get("id") or "").strip()
        file = str(v.get("file") or "").strip()
        if not ten or not file or ten.casefold() in seen:
            continue
        if os.sep in file or "/" in file or ".." in file:      # tệp phải nằm NGAY trong assets/voices
            continue
        if not os.path.isfile(os.path.join(assets_dir(), file)):
            logger.warning(f"Giọng mẫu dựng sẵn '{ten}': thiếu tệp {file} — bỏ qua")
            continue
        seen.add(ten.casefold())
        out.append(v)
    _CACHE.update(sig=sig, data=out)
    return out


def ten_giong() -> list[str]:
    return [v["id"] for v in danh_sach()]


def la_giong_mau(name: str | None) -> bool:
    n = (name or "").strip().casefold()
    return bool(n) and any(t.casefold() == n for t in ten_giong())


def nhan(v: dict) -> str:
    """Nhãn hiện trong bộ chọn, cùng kiểu giọng VieNeu: "Tên — Nữ · đặc điểm · đặc điểm"."""
    gt = "Nam" if str(v.get("gioi_tinh") or "").lower() == "nam" else "Nữ"
    dd = [str(x).strip() for x in (v.get("dac_diem") or []) if str(x).strip()]
    return f"{v.get('hien_thi') or v['id']} — {' · '.join([gt, *dd][:3])}"


def _engine_hien_tai() -> str:
    try:
        import importlib.metadata as md

        return md.version("vieneu")
    except Exception:  # noqa: BLE001
        return ""


def _doc_cache(v: dict) -> dict | None:
    """Dấu vân giọng tính sẵn (None nếu không có / lệch phiên bản thư viện)."""
    emb = str(v.get("emb") or "").strip()
    if not emb or os.sep in emb or "/" in emb or ".." in emb:
        return None
    try:
        with open(os.path.join(assets_dir(), emb), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("speaker_emb") or d.get("codes") is None:
        return None
    if str(d.get("engine") or "") != _engine_hien_tai():
        return None
    return {"speaker_emb": d["speaker_emb"], "codes": d["codes"]}


def gieo(voices_dir: str, reg: dict) -> bool:
    """Đưa bộ giọng dựng sẵn vào ``reg`` (registry của người dùng) + chép tệp mẫu vào ``voices_dir``.
    Trả True nếu ``reg`` có đổi (nơi gọi ghi lại đĩa). KHÔNG đụng giọng của người dùng.

    · Giọng chưa có / manifest đổi (sha256 khác) -> tạo lại mục, kèm dấu vân giọng tính sẵn nếu dùng được.
    · Mục ``builtin`` không còn trong manifest (bộ giọng bị rút bớt ở bản mới) -> gỡ khỏi registry.
    · Người dùng đã tự clone MỘT tên trùng giọng dựng sẵn (trước khi có bộ giọng này) -> giữ của người
      dùng, bỏ qua giọng dựng sẵn đó (không ghi đè dữ liệu của họ)."""
    dung = danh_sach()
    changed = False
    ids = {v["id"] for v in dung}
    for ten in [k for k, e in reg.items() if isinstance(e, dict) and e.get("builtin") and k not in ids]:
        try:
            os.remove(reg[ten].get("file") or "")
        except OSError:
            pass
        del reg[ten]
        changed = True
    if not dung:
        return changed
    os.makedirs(voices_dir, exist_ok=True)
    for v in dung:
        ten = v["id"]
        cur = reg.get(ten)
        if isinstance(cur, dict) and not cur.get("builtin"):
            logger.warning(f"Giọng mẫu dựng sẵn '{ten}' trùng tên giọng người dùng đã tự thêm — giữ giọng của người dùng.")
            continue
        src = os.path.join(assets_dir(), v["file"])
        dst = os.path.join(voices_dir, f"{ten}.wav")
        try:
            if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copyfile(src, dst)
        except OSError as e:
            logger.warning(f"Giọng mẫu dựng sẵn '{ten}': không chép được tệp mẫu: {e}")
            continue
        sha = str(v.get("sha256") or "")
        if isinstance(cur, dict) and cur.get("builtin") and cur.get("builtin_sha") == sha and cur.get("file") == dst:
            continue                                  # đã gieo đúng bản này
        entry = {"file": dst, "builtin": True, "builtin_sha": sha}
        cache = _doc_cache(v)
        if cache:
            entry.update(cache)
        reg[ten] = entry
        changed = True
    return changed
