"""Chuyển tọa độ overlay source (video gốc) ↔ output (sau crop)."""


def source_rect_to_output_norm(
    x: float,
    y: float,
    w: float,
    h: float,
    crop: dict,
) -> tuple[float, float, float, float]:
    cx = float(crop.get("x", 0))
    cy = float(crop.get("y", 0))
    cw = max(float(crop.get("w", 1)), 1e-6)
    ch = max(float(crop.get("h", 1)), 1e-6)
    return (
        (x - cx) / cw,
        (y - cy) / ch,
        w / cw,
        h / ch,
    )
