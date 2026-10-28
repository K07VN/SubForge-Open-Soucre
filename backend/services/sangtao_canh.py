"""DỰNG MỘT CẢNH của chế độ "Sáng tạo" (video học ngoại ngữ): bố cục (16:9 / 9:16) → lớp hình theo thời gian → bàn tay + bút → khung hình → clip mp4
(không tiếng). Mỗi cảnh độc lập nên :mod:`sangtao_dung` dựng nhiều cảnh song song. Không ngẫu nhiên: cùng đầu vào → cùng khung hình.

Hai kiểu cảnh: ``reading`` (bài đọc) và ``title`` (thẻ mở đầu / kết thúc, chỉ nội bộ). Hình vẽ ở độ phân giải xuất (không zoom).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import cv2
import imageio_ffmpeg
import numpy as np
from PIL import Image

from services import sangtao_bai as B
from services import sangtao_hoc as HOC
from services import sangtao_phien_am as PA
from services import sangtao_reveal as RV
from services import sangtao_ruby as RB

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
GIAY = np.array([250, 246, 236], np.float32)            # màu giấy
GIAY_U8 = np.full((1, 1, 3), (250, 246, 236), np.uint8)
DUOI = 0.5                                              # đệm im lặng cuối cảnh sau đoạn tiếng cuối (giây)


class Huy(Exception):
    """Người dùng hủy giữa chừng."""


class LoiDung(Exception):
    """Lỗi dựng có thể báo cho người dùng bằng tiếng Việt."""


# ------------------------------------------------------------------ bố cục
@dataclass
class BoCuc:
    ow: int
    oh: int
    tay: float                       # hệ số cỡ bàn tay
    doc: bool = False


def bo_cuc(aspect: str) -> BoCuc:
    if aspect == "16:9":
        return BoCuc(1280, 720, 1.0)
    if aspect == "9:16":
        return BoCuc(720, 1280, 0.8, True)
    raise LoiDung(f"Tỷ lệ '{aspect}' chưa được hỗ trợ")


# ------------------------------------------------------------------ bàn tay
@dataclass
class BanTay:
    rgb: np.ndarray
    alpha: np.ndarray
    tip: tuple[int, int]
    tam: bool = False
    pre: Optional[np.ndarray] = None      # rgb*alpha (uint8) — nhân sẵn để trộn nhanh
    inv: Optional[np.ndarray] = None      # 255-alpha (uint8, 3 kênh)


def thu_muc_tai_nguyen() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "sangtao")


def nap_ban_tay(he_so: float = 1.0, ow: int = 1280) -> BanTay:
    """Bàn tay + bút. Mặc định: ``assets/sangtao/hand/hand.json``; ghi đè bằng biến môi trường ``SUBFORGE_SANGTAO_HAND``
    (đường dẫn tới một hand.json khác — dùng cho demo với ảnh tay thật chưa rõ giấy phép, KHÔNG đưa vào repo)."""
    hand_dir = os.path.join(thu_muc_tai_nguyen(), "hand")
    cfg_path = os.environ.get("SUBFORGE_SANGTAO_HAND") or ""
    if not cfg_path:   # mặc định: ảnh tay iStock người dùng chọn (cục bộ, không commit); thiếu thì dùng clipart công khai
        ist = os.path.join(hand_dir, "hand_istock.json")
        cfg_path = ist if os.path.exists(ist) else os.path.join(hand_dir, "hand.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        im = Image.open(os.path.join(os.path.dirname(cfg_path), cfg["file"])).convert("RGBA")
    except (OSError, KeyError, ValueError) as e:
        raise LoiDung(f"Không nạp được ảnh bàn tay ({cfg_path}): {e}")
    k = float(cfg.get("scale", 1.0)) * he_so * (ow / 1280.0 if ow > 1280 else 1.0)
    im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
    a = np.asarray(im)
    tx, ty = cfg.get("tip", [0, 0])
    rgb = a[..., :3].astype(np.float32)
    al = a[..., 3].astype(np.float32) / 255.0
    a3 = cv2.cvtColor(a[..., 3].copy(), cv2.COLOR_GRAY2RGB)
    pre = cv2.multiply(np.ascontiguousarray(a[..., :3]), a3, scale=1 / 255.0)
    return BanTay(rgb, al, (int(tx * k), int(ty * k)), bool(cfg.get("tam")), pre, cv2.bitwise_not(a3))


# ------------------------------------------------------------------ nền giấy
_nen_giay_cache: dict = {}
_khoa_nen = threading.Lock()


def nen_giay(ww: int, wh: int) -> np.ndarray:
    """Giấy ấm có vân + tối nhẹ ở góc (dựng một lần, dùng chung giữa các luồng; chỉ đọc)."""
    with _khoa_nen:
        if (ww, wh) not in _nen_giay_cache:
            rng = np.random.default_rng(7)
            noise = cv2.GaussianBlur(rng.normal(0, 2.4, (wh, ww)).astype(np.float32), (0, 0), 1.1)
            yy, xx = np.mgrid[0:wh, 0:ww].astype(np.float32)
            d = np.sqrt(((xx - ww / 2) / (ww / 2)) ** 2 + ((yy - wh / 2) / (wh / 2)) ** 2)
            vig = 1.0 - 0.055 * np.clip(d - 0.55, 0, 1) ** 1.3
            bg = np.clip((GIAY[None, None, :] + noise[..., None]) * vig[..., None], 0, 255).astype(np.uint8)
            bg.setflags(write=False)
            _nen_giay_cache[(ww, wh)] = bg
        return _nen_giay_cache[(ww, wh)]


# ------------------------------------------------------------------ một cảnh đã lắp
@dataclass
class CanhDung:
    so: int
    thoi_luong: float
    obj: Any = None                                # lớp hình (HOC.BaiDoc | HOC.TieuDe): ve(world, t) + tip
    bc: Optional[BoCuc] = None
    canh_bao: list = field(default_factory=list)
    khoa: str = ""
    anh_t: float = 1.0                             # lúc chụp ảnh đại diện (giây)

    @property
    def so_khung(self) -> int:
        return int(round(self.thoi_luong * 30))


def _dien_phien_am(dl: dict, ngon: str) -> dict:
    """AI không ghi phiên âm → tự điền khi chắc chắn (xem sangtao_phien_am): pinyin (Trung), La-tinh hoá (Hàn). Tiếng Nhật không tự điền (cần cách đọc từ AI)."""
    if ngon in ("zh-cn", "ko"):
        return {**dl, "lines": [{**ln, "phonetic": ln["phonetic"] or PA.tu_dong(ngon, ln["text"]) or ""} for ln in dl["lines"]]}
    return dl


def xay_canh(canh: dict, so: int, tuy: dict) -> CanhDung:
    """Lắp một cảnh. ``canh``: dict của ``Canh`` đã có ``duration_s`` (và ``visual.data._moc`` do bộ dựng điền); ``tuy``: aspect, language, audience, ngon_me, furi, bai."""
    bc = bo_cuc(tuy.get("aspect", "16:9"))
    ngon = tuy.get("language", "vi")
    v = canh["visual"]
    dur = float(canh["duration_s"])
    kieu = v["type"]
    cd = CanhDung(so=so, thoi_luong=dur, bc=bc)
    if kieu == "title":
        d = v["data"]
        noi = float(d.get("noi") or 0.0)
        cd.obj = HOC.TieuDe(d["text"], d.get("color", "blue"), bc, ngon, tuy, d.get("sub", ""), noi)
        cd.anh_t = min(dur - 0.2, cd.obj.t0 + cd.obj.t_viet + 1.1)
        return cd
    if kieu == "script":                                                          # TOÀN VĂN cuối video (có / không có cách đọc), không tiếng
        cd.obj = HOC.ToanVan(v["data"]["cau"], bc, ngon, tuy)
        cd.anh_t = min(max(0.5, dur - 0.2), 1.2)
        return cd
    if kieu != "reading":
        raise LoiDung(f"Cảnh {so}: kiểu '{kieu}' không còn được hỗ trợ")
    opts = tuy.get("bai") or {}
    dl = _dien_phien_am(v["data"], ngon)
    ke = B.ke_hoach(dl, opts)
    moc = dl.get("_moc")
    if not (isinstance(moc, list) and len(moc) == len(ke)):
        moc = B.moc_uoc(ke, ngon, tuy.get("ngon_me") or "vi", opts, tuy.get("audience") == "kids")
    cd.obj = HOC.BaiDoc(dl, ke, moc, bc, ngon, tuy)
    cd.anh_t = max(0.0, dur - 0.7)
    return cd


# ------------------------------------------------------------------ dựng khung + mã hoá
def _mem(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def khung_hinh(cd: CanhDung, fi: int, ban_tay: BanTay, tt: dict) -> np.ndarray:
    """Khung xuất thứ ``fi`` (uint8 oh×ow×3). ``tt``: trạng thái giữa các khung của cùng cảnh (camera, bàn tay).

    Thứ tự: nội dung vẽ lên "thế giới" → CAMERA (zoom + lia, làm mượt giữa các khung) → phủ lại nền giấy ở dải trên cùng (nội dung trôi vào đó thì mờ hẳn) → lớp TĨNH (tiêu đề,
    nhãn lượt, ghi nguồn) → bàn tay (kích thước cố định, ngòi bút bám theo chữ đang viết, kể cả khi đã zoom) → chuyển cảnh mờ dần."""
    bc = cd.bc
    W, H = bc.ow, bc.oh
    t = fi / 30.0
    ob = cd.obj
    nen = nen_giay(W, H)
    world = nen.copy()
    ob.ve(world, t)
    z_m, fx_m, fy_m = ob.cam_muc if ob.cam_muc else (1.0, W / 2.0, H / 2.0)
    cz, cx, cy = tt.get("cz", 1.0), tt.get("cx", W / 2.0), tt.get("cy", H / 2.0)
    cz += (z_m - cz) * 0.09
    cx += (fx_m - cx) * 0.09
    cy += (fy_m - cy) * 0.09
    tt["cz"], tt["cx"], tt["cy"] = cz, cx, cy
    ox = oy = 0.0
    z = 1.0
    if cz > 1.004:
        z = cz
        hw, hh = W / (2 * z), H / (2 * z)
        ox = float(np.clip(cx, hw, W - hw)) - hw
        oy = float(np.clip(cy, hh, H - hh)) - hh
        M = np.float32([[z, 0, -z * ox], [0, z, -z * oy]])
        khung = cv2.warpAffine(world, M, (W, H), flags=cv2.INTER_CUBIC)
    else:
        khung = world
    h_tren, mem_tren = float(getattr(ob, "dai_tren", 0) or 0), float(getattr(ob, "dai_mem", 34))
    dz = float(getattr(ob, "dai_zoom", 0) or 0)
    if dz > h_tren and z > 1.0:                                                  # đang zoom: chuyển dần sang dải phủ dày hơn theo mức zoom
        w = min(1.0, (z - 1.0) / 0.05)
        h_tren, mem_tren = h_tren + w * (dz - h_tren), mem_tren + w * (float(getattr(ob, "dai_zoom_mem", 34)) - mem_tren)
    h_tren = int(round(h_tren))
    if h_tren > 0:
        c = np.clip((h_tren - np.arange(h_tren, dtype=np.float32)) / max(8.0, mem_tren), 0.0, 1.0)[:, None, None]
        khung[:h_tren] = (nen[:h_tren] * c + khung[:h_tren] * (1.0 - c)).astype(np.uint8)
    ob.ve_tinh(khung, t)
    tip = ((ob.tip[0] - ox) * z, (ob.tip[1] - oy) * z) if ob.tip else ob.tip_tinh
    # bàn tay đi theo ngòi bút khi đang viết; viết xong thì nán lại rồi rút ra khỏi khung
    tay = tt.get("tay")
    if tip:
        if tay is None:
            tay = (tip[0] + 420, tip[1] + 320)                              # từ góc dưới bên phải đưa vào
        d_tay = math.hypot(tip[0] - tay[0], tip[1] - tay[1])
        #  Đang bám ngòi bút (khung trước vẫn viết): càng lệch xa càng đuổi nhanh — xuống dòng ngòi nhảy ~900 px, bàn tay không được lê theo sau cả chục khung. Mới đưa vào / vừa rút ra thì giữ nhịp cũ (0,28).
        g_tay = 0.28 + (min(0.5, d_tay / 400.0) if t - tt.get("tay_t", -9.0) < 0.2 else 0.0)
        tay = (tay[0] + (tip[0] - tay[0]) * g_tay, tay[1] + (tip[1] - tay[1]) * g_tay)
        tt["tay_t"] = t
        tt["tip_cuoi"] = tip
    elif tay is not None and t - tt.get("tay_t", -9.0) > 0.7:
        kk = min(1.0, (t - tt["tay_t"] - 0.7) / 0.6)
        tay = (tay[0] + 16 + 60 * kk, tay[1] + 12 + 45 * kk)
        if tay[0] > W + 320 or tay[1] > H + 320:
            tay = None
    elif tay is not None and tt.get("tip_cuoi") and t - tt.get("tay_t", -9.0) > 0.05:
        # vừa viết xong: NHẤC tay lệch khỏi nét cuối (ngòi bút nằm ngay trên dấu 。 cuối câu nên che mất nó, trước đây phải đợi 0,7 s tay rút đi mới thấy) rồi mới nán lại / rút ra
        tc = tt["tip_cuoi"]
        tay = (tay[0] + (tc[0] + 72 - tay[0]) * 0.2, tay[1] + (tc[1] + 52 - tay[1]) * 0.2)
    tt["tay"] = tay
    if tay is not None:
        RV.tron_san(khung, ban_tay.pre, ban_tay.inv, int(tay[0] - ban_tay.tip[0]), int(tay[1] - ban_tay.tip[1]))
    ob.ve_tren(khung, t)                                                       # ghi nguồn + phụ đề lời nói vẽ SAU bàn tay → bàn tay luôn nằm dưới
    f = min(_mem(t / 0.4), _mem((cd.thoi_luong - t) / 0.4))                  # chuyển cảnh mờ dần
    if f < 1:
        giay_k = np.empty_like(khung)
        giay_k[:] = GIAY_U8
        khung = cv2.addWeighted(khung, float(f), giay_k, float(1 - f), 0)
    return khung


MIN_KHUNG_DOAN = 240        # đoạn song song ngắn hơn 8 s thì chi phí dựng lại đối tượng cảnh + khởi động ffmpeg không đáng
KHUNG_KHOI_DONG = 90        # khung chạy "nháp" trước đầu mỗi đoạn để camera (0,09 / khung) và bàn tay (0,28 / khung) đạt đúng trạng thái như khi dựng liền mạch


class _HuyChung:
    """``is_set()`` = người dùng hủy HOẶC một đoạn khác đã lỗi (các đoạn còn lại dừng sớm)."""

    def __init__(self, huy: Optional[threading.Event], loi: threading.Event):
        self.huy, self.loi = huy, loi

    def is_set(self) -> bool:
        return (self.huy is not None and self.huy.is_set()) or self.loi.is_set()


def _viet_doan(cd: CanhDung, s: int, e: int, out: str, ban_tay: BanTay, huy, tien_do: Optional[Callable[[int], None]], khoi_dong: int = 0) -> None:
    """Dựng các khung [s, e) của ``cd`` ra ``out`` (mp4 không tiếng). ``khoi_dong`` khung trước ``s`` được vẽ nháp (không ghi) để trạng thái camera / bàn tay khớp bản dựng liền mạch.
    Khung GIỐNG HỆT khung trước (cảnh có ``khoa_khung``, vd toàn văn) được dùng lại, không vẽ lại. Lỗi / hủy → xoá tệp dở."""
    bc = cd.bc
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen([FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{bc.ow}x{bc.oh}", "-r", "30",
                             "-i", "-", "-an", "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "19", "-tune", "animation", "-pix_fmt", "yuv420p", out],
                            stdin=subprocess.PIPE, stderr=log)
    tt: dict = {}
    try:
        for fi in range(max(0, s - khoi_dong), s):
            if huy is not None and huy.is_set():
                raise Huy()
            khung_hinh(cd, fi, ban_tay, tt)
        khoa_ham = getattr(cd.obj, "khoa_khung", None)
        khoa_cu, du_lieu = None, b""
        for fi in range(s, e):
            if huy is not None and huy.is_set():
                raise Huy()
            t = fi / 30.0
            khoa = khoa_ham(t) if khoa_ham is not None else None
            if khoa is not None and khoa == khoa_cu and 0.5 < t < cd.thoi_luong - 0.5:
                pass                                                                  # khung không đổi: ghi lại bản trước
            else:
                du_lieu = khung_hinh(cd, fi, ban_tay, tt).tobytes()
                khoa_cu = khoa
            proc.stdin.write(du_lieu)
            if tien_do:
                tien_do(1)
        proc.stdin.close()
        if proc.wait() != 0:
            log.seek(0)
            raise LoiDung("ffmpeg lỗi khi mã hoá cảnh %d: %s" % (cd.so, log.read().decode("utf-8", "ignore")[-400:]))
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        raise
    finally:
        log.close()


def dung_clip(cd: CanhDung, out_mp4: str, ban_tay: BanTay, huy: Optional[threading.Event] = None,
              tien_do: Optional[Callable[[int], None]] = None, pool: Any = None, so_doan: int = 1, tao_cd: Optional[Callable[[], CanhDung]] = None) -> int:
    """Dựng cảnh ra clip mp4 (không tiếng, 30 khung/giây, libx264). Hủy → xoá tệp dở và ném :class:`Huy`.

    Có ``pool`` (ThreadPoolExecutor) + ``tao_cd`` (hàm lắp một bản SAO RIÊNG của cảnh — các đối tượng chữ bị sửa vị trí ngay trong lúc vẽ nên KHÔNG dùng chung giữa các luồng) và cảnh đủ dài: chia
    thành tối đa ``so_doan`` đoạn liên tiếp vẽ SONG SONG (mỗi đoạn một ffmpeg riêng), rồi nối bằng ffmpeg (sao chép luồng, không mã hoá lại). Trước đây một cảnh là MỘT luồng nên bài chỉ có một cảnh dài
    (hầu hết bài) chỉ dùng ~1/8 CPU."""
    os.makedirs(os.path.dirname(os.path.abspath(out_mp4)), exist_ok=True)
    tam = out_mp4 + ".dang_dung.mp4"
    n = cd.so_khung
    k = 1 if (pool is None or tao_cd is None) else max(1, min(int(so_doan), n // MIN_KHUNG_DOAN))
    if k == 1:
        _viet_doan(cd, 0, n, tam, ban_tay, huy, tien_do, 0)
        os.replace(tam, out_mp4)
        return n
    bien = [round(i * n / k) for i in range(k + 1)]
    doan = [f"{out_mp4}.doan{i}.mp4" for i in range(k)]
    loi = threading.Event()
    huy2 = _HuyChung(huy, loi)

    def lam(i: int) -> None:
        try:
            _viet_doan(cd if i == 0 else tao_cd(), bien[i], bien[i + 1], doan[i], ban_tay, huy2, tien_do, 0 if i == 0 else KHUNG_KHOI_DONG)
        except BaseException:
            loi.set()
            raise

    futs = [pool.submit(lam, i) for i in range(k)]
    loi_dau: Optional[BaseException] = None
    for f in futs:
        try:
            f.result()
        except BaseException as ex:  # noqa: BLE001 — chờ HẾT các đoạn rồi mới dọn tệp; ưu tiên báo lỗi gốc (không phải Huy do đoạn khác lỗi)
            if loi_dau is None or (isinstance(loi_dau, Huy) and not isinstance(ex, Huy)):
                loi_dau = ex
    ds = out_mp4 + ".doan.txt"
    try:
        if loi_dau is not None:
            raise loi_dau
        with open(ds, "w", encoding="utf-8", newline="\n") as fh:
            for p_ in doan:
                fh.write("file '%s'\n" % p_.replace("\\", "/").replace("'", "'\\''"))
        r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", ds, "-c", "copy", tam], capture_output=True)
        if r.returncode != 0:
            raise LoiDung("ffmpeg lỗi khi nối các đoạn của cảnh %d: %s" % (cd.so, r.stderr.decode("utf-8", "ignore")[-300:]))
        os.replace(tam, out_mp4)
    except BaseException:
        if os.path.exists(tam):
            try:
                os.remove(tam)
            except OSError:
                pass
        raise
    finally:
        for p_ in doan + [ds]:
            if os.path.exists(p_):
                try:
                    os.remove(p_)
                except OSError:
                    pass
    return n
