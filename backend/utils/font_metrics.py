"""Quy đổi cỡ chữ CSS (em) -> ASS Fontsize theo metrics của font.

libass theo chuẩn VSFilter/GDI: Style ``Fontsize`` là CHIỀU CAO Ô CHỮ
(OS/2 usWinAscent + usWinDescent), còn CSS ``font-size`` của preview là CỠ EM.
Cùng ghi "36" thì libass vẽ glyph tương đương CSS ``36 × unitsPerEm / (winAscent
+ winDescent)`` px — với Segoe UI (2048/2724) chữ xuất ra NHỎ HƠN preview ~25%.
Đo thật 2026-08-27: cùng câu cùng "36px", Chrome vẽ dòng rộng 795px còn libass
chỉ 610px (tỷ lệ 0.767 ≈ 2048/2724).

Cách bù: nhân Fontsize với ``(winAscent + winDescent) / unitsPerEm`` của đúng
file font sẽ dùng. Hệ số này THAY ĐỔI THEO FONT (Arial ~1.12, Segoe UI ~1.33)
nên không hardcode được một con số chung.

Chuỗi suy ra từ libass ``ass_face_set_size`` (ass_font.c):
    mscale   = (hheaAsc - hheaDesc) / (winAsc + winDesc)   # khi có OS/2
    REAL_DIM: ppem = size × mscale × upem / (hheaAsc - hheaDesc)
            => em px = size × upem / (winAsc + winDesc)
Không có OS/2 thì mscale = 1 => em px = size × upem / (hheaAsc - hheaDesc).
"""
import os
import struct
from functools import lru_cache


def _sfnt_tables(data: bytes, offset: int) -> dict[str, int]:
    num_tables = struct.unpack_from(">H", data, offset + 4)[0]
    tables: dict[str, int] = {}
    for i in range(num_tables):
        rec = offset + 12 + 16 * i
        tag = data[rec : rec + 4].decode("latin-1")
        tables[tag] = struct.unpack_from(">I", data, rec + 8)[0]
    return tables


@lru_cache(maxsize=64)
def ass_fontsize_scale(font_path: str | None) -> float:
    """Hệ số nhân vào ASS Fontsize để glyph xuất ra đúng cỡ CSS px của preview.

    Trả 1.0 (giữ nguyên hành vi cũ) khi không có/không đọc được font — thà chữ
    hơi nhỏ như trước còn hơn văng lỗi giữa lúc xuất video.
    """
    if not font_path or not os.path.exists(font_path):
        return 1.0
    try:
        with open(font_path, "rb") as f:

            def read_at(pos: int, size: int) -> bytes:
                f.seek(pos)
                chunk = f.read(size)
                if len(chunk) < size:
                    raise struct.error("font file cụt")
                return chunk

            head = read_at(0, 16)
            # TTC: bảng font đầu tiên nằm tại offset ghi ở byte 12.
            offset = struct.unpack_from(">I", head, 12)[0] if head[:4] == b"ttcf" else 0
            num_tables = struct.unpack_from(">H", read_at(offset + 4, 2), 0)[0]
            directory = read_at(offset, 12 + 16 * num_tables)
            tables = _sfnt_tables(directory, 0)
            upem = struct.unpack_from(">H", read_at(tables["head"] + 18, 2), 0)[0]
            if "OS/2" in tables:
                # libass ép (short) vì có font ghi giá trị âm dù spec là unsigned.
                win_asc, win_desc = struct.unpack_from(
                    ">hh", read_at(tables["OS/2"] + 74, 4), 0
                )
                cell = win_asc + win_desc
            elif "hhea" in tables:
                asc, desc = struct.unpack_from(">hh", read_at(tables["hhea"] + 4, 4), 0)
                cell = asc - desc  # descender âm
            else:
                return 1.0
        if upem <= 0 or cell <= 0:
            return 1.0
        # Kẹp phòng font ghi metrics rác: hệ số thật của font phổ biến ~1.0–1.5.
        return max(0.5, min(2.0, cell / upem))
    except (OSError, KeyError, struct.error):
        return 1.0
