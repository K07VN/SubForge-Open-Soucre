"""Tiện ích trộn hình lên khung cho chế độ "Sáng tạo": trộn alpha có cắt biên (``tron``) và trộn hình đã nhân sẵn alpha (``tron_san``, dùng cho bàn tay)."""
from __future__ import annotations

import threading
from collections import OrderedDict

import cv2
import numpy as np


_DEM: "OrderedDict[tuple, tuple]" = OrderedDict()      # khoá → (alpha, rgb, 255-a, rgb*a): phần tính sẵn của các lớp trộn LẶP LẠI giữa các khung (thẻ giấy, bóng đổ, avatar…)
_VISTA: "OrderedDict[tuple, int]" = OrderedDict()    # khoá đã gặp một lần: chỉ LƯU ĐỆM từ lần gặp thứ hai (lớp đổi từng khung — đang hiện dần, đang mờ — không làm đầy bộ nhớ)
_KHOA = threading.Lock()
_NGAN_SACH = 320 * 1024 * 1024
_DUNG = [0]


def tron(world: np.ndarray, rgb, alpha: np.ndarray, x0: int, y0: int, f: float = 1.0) -> None:
    """world[y0:, x0:] = world*(1-alpha*f) + rgb*alpha*f, có cắt theo biên. ``rgb``: (3,) hoặc HxWx3. ``f`` = độ mờ chung (0..1).
    Tính bằng số nguyên 8-bit của OpenCV (SIMD), sai lệch tối đa 1 mức xám. Mặt nạ + màu là MẢNG DÙNG LẠI giữa các khung (thẻ, bóng, avatar) thì phần tính sẵn (a8, 255-a, rgb·a) được
    LƯU ĐỆM theo (mảng, f, vùng cắt): khung sau chỉ còn hai phép nhân / cộng — trước đây mỗi khung tính lại cả mặt nạ float32 (chiếm ~70% thời gian vẽ khung)."""
    h, w = alpha.shape
    wh, ww = world.shape[:2]
    xa, ya = max(0, x0), max(0, y0)
    xb, yb = min(ww, x0 + w), min(wh, y0 + h)
    if xb <= xa or yb <= ya:
        return
    sx, sy = xa - x0, ya - y0
    la_mang = getattr(rgb, "ndim", 1) != 1
    khoa = (id(alpha), id(rgb) if la_mang else tuple(float(x) for x in rgb), round(float(f), 3), sx, sy, xb - xa, yb - ya)
    with _KHOA:
        e = _DEM.get(khoa)
        if e is not None and e[0] is alpha and (not la_mang or e[1] is rgb):
            _DEM.move_to_end(khoa)
        else:
            e = None
    if e is None:
        a = alpha[sy:sy + yb - ya, sx:sx + xb - xa]
        a8 = np.clip(a * (255.0 * f) + 0.5, 0, 255).astype(np.uint8)
        if not a8.any():
            return
        a3 = cv2.cvtColor(a8, cv2.COLOR_GRAY2RGB)
        if la_mang:
            c = rgb[sy:sy + yb - ya, sx:sx + xb - xa]
            c8 = c if c.dtype == np.uint8 else np.clip(c + 0.5, 0, 255).astype(np.uint8)
        else:
            c8 = np.empty((yb - ya, xb - xa, 3), np.uint8)
            c8[:] = np.asarray(rgb, np.float32).astype(np.uint8)
        inv = cv2.bitwise_not(a3)
        pre = cv2.multiply(c8, a3, scale=1 / 255.0)
        with _KHOA:
            if khoa in _VISTA:                                                         # lần gặp thứ hai → lưu đệm
                _DEM[khoa] = (alpha, rgb if la_mang else None, inv, pre)
                _DUNG[0] += inv.nbytes + pre.nbytes
                while _DUNG[0] > _NGAN_SACH and _DEM:
                    _, cu = _DEM.popitem(last=False)
                    _DUNG[0] -= cu[2].nbytes + cu[3].nbytes
            else:
                _VISTA[khoa] = 1
                while len(_VISTA) > 6000:
                    _VISTA.popitem(last=False)
    else:
        inv, pre = e[2], e[3]
    reg = world[ya:yb, xa:xb]
    cv2.multiply(reg, inv, dst=reg, scale=1 / 255.0)
    cv2.add(reg, pre, dst=reg)


def tron_san(world: np.ndarray, pre: np.ndarray, inv: np.ndarray, x0: int, y0: int) -> None:
    """Như :func:`tron` nhưng với hình ĐÃ NHÂN SẴN alpha (``pre`` = rgb*a, ``inv`` = 255-a, cả hai uint8 HxWx3) — dùng cho bàn tay
    và phụ đề, nơi alpha không đổi giữa các khung nên không phải tính lại mỗi khung."""
    h, w = pre.shape[:2]
    wh, ww = world.shape[:2]
    xa, ya = max(0, x0), max(0, y0)
    xb, yb = min(ww, x0 + w), min(wh, y0 + h)
    if xb <= xa or yb <= ya:
        return
    sx, sy = xa - x0, ya - y0
    reg = world[ya:yb, xa:xb]
    cv2.multiply(reg, inv[sy:sy + yb - ya, sx:sx + xb - xa], dst=reg, scale=1 / 255.0)
    cv2.add(reg, pre[sy:sy + yb - ya, sx:sx + xb - xa], dst=reg)
