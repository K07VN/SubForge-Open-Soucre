"""Biểu thức FFmpeg drawtext cho watermark chạy động.

QUAN TRỌNG: các phương trình ở đây phải KHỚP 1:1 với preview
(frontend/src/utils/watermarkMotion.ts — getWatermarkNormPosition):
  - Toạ độ CHUẨN HOÁ theo khung NGUỒN: margin=0.03, hộp chữ boxW=0.22, boxH=0.06.
  - Cùng chu kỳ/biên độ/pha cho float/bounce/diagonal/corners.
  - diagonal: y = mod( mod(t/loop,1) * 1.35, 1 )  (lấy phần dư CỦA p*1.35 —
    bản cũ dùng mod(t*1.35/loop,1) lệch pha qua mỗi vòng lặp so với preview).
Sau đó quy đổi từ source-norm sang OUTPUT px qua crop frame:
  X_px = w * ((x_src - crop.x) / crop.w)   (tương tự cho Y)
— bản cũ bỏ qua crop nên watermark là overlay duy nhất lệch vị trí khi có crop.

HAI HỆ TOẠ ĐỘ (24-09-2026):
  · "static" (Cố định): vị trí người dùng ĐẶT TAY trên video -> hệ NGUỒN, qua crop như trên.
  · Mọi kiểu CHUYỂN ĐỘNG: quỹ đạo tính thẳng trong hệ KHUNG XUẤT (0..1 của cả khung, kể cả
    phần nền mờ hai bên khi video dọc nằm trong khung ngang) -> X_px = w * x. Bản cũ tính
    trong hệ nguồn nên watermark chỉ nảy trong vùng video, không chạy khắp khung.
    Preview quy ngược: x_src = crop.x + x_out * crop.w (watermarkMotion.ts).
    BIÊN theo CỠ CHỮ THẬT (kiểu logo DVD chạm mép): x = lề + (1 - rộng_chữ - 2·lề)·g(t),
    ở đây rộng_chữ = ``tw``/``th`` của drawtext; preview đo phần tử chữ rồi quy về tỷ lệ
    khung xuất. Hộp cố định BOX_W/BOX_H chỉ còn dùng cho "static".
"""

from typing import Tuple

FLOAT_PERIOD = 32.0
BOUNCE_PERIOD_X = 18.0
BOUNCE_PERIOD_Y = 14.0
DIAGONAL_LOOP = 28.0
CORNER_INTERVAL = 8.0
MIN_SPEED = 0.1
FLOAT_AMP = 0.36

BOX_W = 0.22
BOX_H = 0.06
MARGIN = 0.03


def _crop_params(crop: dict | None) -> Tuple[float, float, float, float]:
    crop = crop or {}
    cx = float(crop.get("x", 0.0) or 0.0)
    cy = float(crop.get("y", 0.0) or 0.0)
    cw = float(crop.get("w", 1.0) or 1.0)
    ch = float(crop.get("h", 1.0) or 1.0)
    cw = max(cw, 1e-6)
    ch = max(ch, 1e-6)
    return cx, cy, cw, ch


def _khung_xuat(gx: str, gy: str) -> Tuple[str, str]:
    """Tham số quỹ đạo g ∈ [0,1] -> px trong KHUNG XUẤT, biên theo cỡ chữ thật (tw/th).

    x = w·lề + (w - tw - 2·w·lề)·gx — g=0 chạm lề trái, g=1 mép phải chữ chạm lề phải.
    Mirror: getWatermarkNormPosition với boxW/boxH = cỡ chữ đo được / cỡ khung xuất."""
    return (
        f"w*{MARGIN}+(w-tw-{2 * MARGIN}*w)*({gx})",
        f"h*{MARGIN}+(h-th-{2 * MARGIN}*h)*({gy})",
    )


def _to_output(x_src_expr: str, y_src_expr: str, crop: dict | None) -> Tuple[str, str]:
    """Đổi biểu thức source-norm -> output px (w/h là kích thước OUTPUT trong drawtext)."""
    cx, cy, cw, ch = _crop_params(crop)
    x = f"w*((({x_src_expr})-{cx:.6f})/{cw:.6f})"
    y = f"h*((({y_src_expr})-{cy:.6f})/{ch:.6f})"
    return x, y


def watermark_drawtext_xy(
    animation: str,
    speed: float,
    base_x_norm: float,
    base_y_norm: float,
    crop: dict | None = None,
) -> Tuple[str, str]:
    """Trả về (x_expr, y_expr) cho drawtext — KHỚP preview từng hằng số."""
    sp = max(MIN_SPEED, float(speed or 0.4))
    max_x = 1.0 - BOX_W - MARGIN
    max_y = 1.0 - BOX_H - MARGIN

    if animation == "static":
        bx = max(MARGIN, min(max_x, float(base_x_norm)))
        by = max(MARGIN, min(max_y, float(base_y_norm)))
        return _to_output(f"{bx:.6f}", f"{by:.6f}", crop)

    if animation == "float":
        period = FLOAT_PERIOD / sp
        phase = f"2*PI*t/{period:.6f}-PI/2"
        return _khung_xuat(f"0.5+{FLOAT_AMP}*cos({phase})", f"0.5+{FLOAT_AMP}*sin({phase})")

    if animation == "bounce":
        px = BOUNCE_PERIOD_X / sp
        py = BOUNCE_PERIOD_Y / sp
        # Song tam giac 0->1->0. KHONG nhan 2 o ngoai: abs(2*frac-1) da cho dung bien
        # [0,1]; nhan 2 thanh [0,2] khien watermark bay RA NGOAI khung suot nua moi chu
        # ky. Khop watermarkMotion.ts (tri).
        tri_x = f"abs(2*mod(t/{px:.6f},1)-1)"
        tri_y = f"abs(2*mod(t/{py:.6f}+0.25,1)-1)"
        return _khung_xuat(tri_x, tri_y)

    if animation == "diagonal":
        loop = DIAGONAL_LOOP / sp
        p = f"mod(t/{loop:.6f},1)"
        return _khung_xuat(p, f"mod(({p})*1.35,1)")

    # corners: TL -> TR -> BR -> BL, doi goc moi CORNER_INTERVAL/sp giay
    interval = CORNER_INTERVAL / sp
    k = f"mod(floor(t/{interval:.6f}),4)"
    #  g = 0/1 theo góc: TL(0,0) TR(1,0) BR(1,1) BL(0,1)
    return _khung_xuat(
        f"if(eq({k},1)+eq({k},2),1,0)",
        f"if(eq({k},2)+eq({k},3),1,0)",
    )
