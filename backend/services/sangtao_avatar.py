"""AVATAR NGƯỜI NÓI cho bài hội thoại / shadowing: ảnh bán thân phẳng, tròn, vẽ bằng PIL (không dùng tài nguyên bên ngoài).

Mỗi người nói có avatar riêng theo GIỚI TÍNH và theo QUỐC GIA của ngôn ngữ đang học để người xem phân biệt vai ngay:
  Anh → mặt Tây (nhiều tông da, tóc nâu / vàng / đen / đỏ) · Nhật → tóc đen, nữ tóc bob có mái bằng, nam tóc ngắn rẽ ngôi
  Trung → nữ tóc dài thẳng mái lệch, nam đầu cua · Hàn → nữ tóc dài gợn sóng mái thưa, nam tóc mái vuốt sang bên.
Cùng (giới, vùng, thứ tự người) → cùng một avatar (không ngẫu nhiên), nên dựng lại không đổi.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

SS = 4                                                       # siêu lấy mẫu: vẽ gấp 4 rồi thu nhỏ cho mép mịn
#: màu từng VAI (đậm = viền avatar / chữ tên; nhạt = nền bong bóng thoại)
VAI = [((52, 120, 214), (222, 236, 255)), ((226, 96, 84), (255, 228, 224)), ((52, 160, 104), (220, 244, 230)), ((150, 100, 206), (238, 228, 255))]
_DA_A = [(250, 222, 196), (240, 208, 176), (246, 214, 186)]
_DA_TAY = [(255, 224, 202), (236, 196, 156), (200, 150, 108), (146, 100, 70)]
_TOC_A = [(28, 26, 30), (46, 36, 34), (36, 30, 32)]
_TOC_TAY = [(70, 46, 34), (228, 188, 96), (30, 28, 32), (172, 84, 52), (110, 76, 52)]
_AO = [(64, 84, 140), (200, 92, 92), (84, 150, 112), (232, 176, 72), (120, 96, 168), (70, 140, 170), (90, 90, 100), (210, 120, 150)]


def vung_cua_ngon_ngu(ngon: str) -> str:
    """Ngôn ngữ đang học → kiểu avatar: ja | zh | ko | tay (Anh và các tiếng châu Âu)."""
    return {"ja": "ja", "zh-cn": "zh", "ko": "ko"}.get(ngon, "tay")


def _mau_toi(c: tuple, he: float) -> tuple:
    return tuple(max(0, min(255, int(x * he))) for x in c)


def _ve(gioi: str, vung: str, stt: int, mau_nen: tuple, mau_vien: tuple, d: int) -> Image.Image:
    N = d * SS
    nu = gioi == "female"
    a_dong = vung in ("ja", "zh", "ko")
    da = (_DA_A if a_dong else _DA_TAY)[(stt + (1 if nu else 0)) % (3 if a_dong else 4)]
    toc = (_TOC_A if a_dong else _TOC_TAY)[(stt * 2 + (1 if nu else 0)) % (3 if a_dong else 5)]
    ao = _AO[(stt * 3 + (2 if nu else 0)) % len(_AO)]
    da_toi = _mau_toi(da, 0.86)
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    cx = N / 2
    dr.ellipse([0, 0, N - 1, N - 1], fill=mau_nen + (255,))
    # --- tóc phía sau (tóc dài / bob): nằm sau cổ + vai
    kieu_toc = "ngan"
    if nu:
        kieu_toc = {"ja": "bob", "zh": "dai", "ko": "song", "tay": ("dai", "song", "duoi")[stt % 3]}[vung]
    else:
        kieu_toc = {"ja": "ranh", "zh": "cua", "ko": "mai", "tay": ("ranh", "ngan", "xoan")[stt % 3]}[vung]
    if kieu_toc in ("dai", "song"):
        dr.rounded_rectangle([cx - 0.255 * N, 0.20 * N, cx + 0.255 * N, 0.80 * N], radius=int(0.2 * N), fill=toc + (255,))
    elif kieu_toc == "bob":
        dr.rounded_rectangle([cx - 0.250 * N, 0.20 * N, cx + 0.250 * N, 0.73 * N], radius=int(0.17 * N), fill=toc + (255,))
    elif kieu_toc == "duoi":
        dr.ellipse([cx + 0.13 * N, 0.20 * N, cx + 0.31 * N, 0.52 * N], fill=toc + (255,))          # đuôi ngựa lệch bên phải
    # --- thân (vai) + cổ áo
    dr.ellipse([cx - 0.42 * N, 0.70 * N, cx + 0.42 * N, 1.30 * N], fill=ao + (255,))
    dr.polygon([(cx - 0.085 * N, 0.70 * N), (cx + 0.085 * N, 0.70 * N), (cx, 0.80 * N)], fill=_mau_toi(da, 0.97) + (255,))
    # --- cổ
    dr.rounded_rectangle([cx - 0.075 * N, 0.56 * N, cx + 0.075 * N, 0.76 * N], radius=int(0.04 * N), fill=da_toi + (255,))
    # --- tai
    for sg in (-1, 1):
        dr.ellipse([cx + sg * 0.205 * N - 0.034 * N, 0.44 * N, cx + sg * 0.205 * N + 0.034 * N, 0.52 * N], fill=da + (255,))
    # --- đầu
    hy, rx, ry = 0.435 * N, 0.205 * N, 0.235 * N
    dr.ellipse([cx - rx, hy - ry, cx + rx, hy + ry], fill=da + (255,))
    # --- tóc phía trước: một "mũ" tóc phủ nửa trên đầu, rồi vẽ lại khuôn mặt để lộ đường chân tóc
    mu = [cx - rx * 1.06, hy - ry * 1.08, cx + rx * 1.06, hy + ry * 0.55]
    dr.ellipse(mu, fill=toc + (255,))
    mat_y = hy + ry * 0.12
    dr.ellipse([cx - rx * 0.93, mat_y - ry * 0.80, cx + rx * 0.93, hy + ry], fill=da + (255,))        # khuôn mặt đè lên mũ tóc
    # mái tóc theo kiểu
    if kieu_toc in ("bob", "dai") and vung in ("ja", "zh"):
        if kieu_toc == "bob":                                                    # mái bằng
            dr.rounded_rectangle([cx - rx * 0.98, hy - ry * 0.78, cx + rx * 0.98, hy - ry * 0.28], radius=int(0.03 * N), fill=toc + (255,))
        else:                                                                    # mái lệch sang trái
            dr.polygon([(cx - rx * 0.98, hy - ry * 0.80), (cx + rx * 0.98, hy - ry * 0.80), (cx + rx * 0.2, hy - ry * 0.18), (cx - rx * 0.98, hy - ry * 0.05)], fill=toc + (255,))
    elif kieu_toc in ("song", "dai") or kieu_toc == "duoi":                        # mái thưa ngôi giữa
        dr.polygon([(cx, hy - ry * 0.80), (cx - rx * 0.98, hy - ry * 0.80), (cx - rx * 0.98, hy - ry * 0.10), (cx - rx * 0.28, hy - ry * 0.42)], fill=toc + (255,))
        dr.polygon([(cx, hy - ry * 0.80), (cx + rx * 0.98, hy - ry * 0.80), (cx + rx * 0.98, hy - ry * 0.10), (cx + rx * 0.28, hy - ry * 0.42)], fill=toc + (255,))
    elif kieu_toc == "mai":                                                      # nam Hàn: mái vuốt sang bên
        dr.polygon([(cx - rx * 0.98, hy - ry * 0.80), (cx + rx * 0.98, hy - ry * 0.80), (cx + rx * 0.9, hy - ry * 0.34), (cx - rx * 0.1, hy - ry * 0.22), (cx - rx * 0.98, hy - ry * 0.05)], fill=toc + (255,))
    elif kieu_toc in ("ranh", "xoan"):                                           # rẽ ngôi lệch
        dr.polygon([(cx - rx * 0.98, hy - ry * 0.80), (cx + rx * 0.98, hy - ry * 0.80), (cx + rx * 0.95, hy - ry * 0.30), (cx + rx * 0.25, hy - ry * 0.52), (cx - rx * 0.55, hy - ry * 0.22), (cx - rx * 0.98, hy - ry * 0.12)], fill=toc + (255,))
        if kieu_toc == "xoan":                                                   # tóc xoăn: vài búi tròn trên đỉnh
            for ox in (-0.55, -0.15, 0.25, 0.62):
                dr.ellipse([cx + ox * rx - 0.05 * N, hy - ry * 1.12 - 0.02 * N, cx + ox * rx + 0.05 * N, hy - ry * 1.12 + 0.07 * N], fill=toc + (255,))
    # --- mặt: mắt, mày, mũi, miệng, má hồng
    mat_dy = 0.015 * N
    ey = hy + ry * 0.12
    erx, ery = (0.021 * N, 0.020 * N) if a_dong else (0.023 * N, 0.029 * N)
    for sg in (-1, 1):
        ex = cx + sg * rx * 0.46
        dr.ellipse([ex - erx, ey - ery, ex + erx, ey + ery], fill=(38, 32, 40, 255))
        dr.ellipse([ex - erx * 0.35 + erx * 0.2, ey - ery * 0.6, ex + erx * 0.25 + erx * 0.2, ey - ery * 0.1], fill=(255, 255, 255, 255))
        by = ey - ery * (2.0 if nu else 1.7)
        dr.line([ex - erx * 1.3, by + 0.006 * N * sg * -1, ex + erx * 1.3, by - 0.002 * N], fill=_mau_toi(toc, 1.1) + (255,), width=int(0.011 * N * (1 if nu else 1.5)))
        if nu:
            dr.line([ex + sg * erx * 0.9, ey - ery * 0.9, ex + sg * erx * 1.5, ey - ery * 1.5], fill=(38, 32, 40, 255), width=int(0.007 * N))
    dr.arc([cx - 0.05 * N, ey + 0.10 * N + mat_dy, cx + 0.05 * N, ey + 0.17 * N + mat_dy], 15, 165, fill=(190, 84, 84, 255) if nu else (150, 70, 66, 255), width=int(0.011 * N))
    dr.arc([cx - 0.012 * N, ey + 0.045 * N, cx + 0.012 * N, ey + 0.075 * N], 20, 160, fill=_mau_toi(da, 0.74) + (255,), width=int(0.007 * N))
    cheek = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    cd = ImageDraw.Draw(cheek)
    for sg in (-1, 1):
        cd.ellipse([cx + sg * rx * 0.62 - 0.034 * N, ey + 0.05 * N, cx + sg * rx * 0.62 + 0.034 * N, ey + 0.095 * N], fill=(240, 110, 110, 70))
    im = Image.alpha_composite(im, cheek)
    dr = ImageDraw.Draw(im)
    if nu and vung == "ja":                                                      # kẹp tóc đỏ nhỏ (nữ Nhật)
        dr.rounded_rectangle([cx + rx * 0.62, hy - ry * 0.62, cx + rx * 0.98, hy - ry * 0.50], radius=int(0.01 * N), fill=(230, 70, 80, 255))
    # --- chi tiết: kính (một số người), bông tai (nữ)
    if stt % 5 == 3 and not nu:
        for sg in (-1, 1):
            ex = cx + sg * rx * 0.46
            dr.ellipse([ex - 0.062 * N, ey - 0.05 * N, ex + 0.062 * N, ey + 0.05 * N], outline=(60, 60, 70, 255), width=int(0.009 * N))
        dr.line([cx - rx * 0.46 + 0.062 * N, ey, cx + rx * 0.46 - 0.062 * N, ey], fill=(60, 60, 70, 255), width=int(0.009 * N))
    if nu and stt % 2 == 1:
        for sg in (-1, 1):
            dr.ellipse([cx + sg * 0.205 * N - 0.014 * N, 0.52 * N, cx + sg * 0.205 * N + 0.014 * N, 0.55 * N], fill=(250, 214, 80, 255))
    # --- cắt tròn + viền màu của vai
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, N - 1, N - 1], fill=255)
    ra = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ra.paste(im, (0, 0), mask)
    dr = ImageDraw.Draw(ra)
    dr.ellipse([0, 0, N - 1, N - 1], outline=mau_vien + (255,), width=int(0.055 * N))
    return ra.resize((d, d), Image.LANCZOS)


_CACHE: dict = {}


def avatar(gioi: str, ngon: str, stt: int, d: int, vai: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """(rgb uint8 d×d×3, alpha float32 d×d) của người nói thứ ``stt`` — theo giới tính ("male" | "female") và ngôn ngữ đang học."""
    vung = vung_cua_ngon_ngu(ngon)
    gioi = "male" if gioi == "male" else "female"
    khoa = (gioi, vung, stt % 12, d, vai % len(VAI))
    if khoa not in _CACHE:
        dam, nhat = VAI[vai % len(VAI)]
        im = np.asarray(_ve(gioi, vung, stt, nhat, dam, d))
        _CACHE[khoa] = (np.ascontiguousarray(im[..., :3]), im[..., 3].astype(np.float32) / 255.0)
    return _CACHE[khoa]
