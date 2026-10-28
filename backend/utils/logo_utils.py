from utils.i18n import L
import io
import os
import time
import numpy as np
from PIL import Image


def remove_logo_background(img: Image.Image) -> Image.Image:
    """Xoá nền cờ caro / nền trắng / xám giả minh hoạ độ trong suốt xung quanh logo từ 4 viền ngoài."""
    img = img.convert("RGBA")
    data = np.array(img)
    h, w, _ = data.shape
    if h < 2 or w < 2:
        return img

    r, g, b, a = data[:, :, 0], data[:, :, 1], data[:, :, 2], data[:, :, 3]

    # Pixel được coi là màu nền nghi vấn (trắng, xám, cờ caro): sáng R,G,B > 160 & ít lệch màu
    is_bg_color = (
        (r > 160) & (g > 160) & (b > 160) &
        (np.abs(r.astype(int) - g.astype(int)) < 30) &
        (np.abs(g.astype(int) - b.astype(int)) < 30) &
        (np.abs(r.astype(int) - b.astype(int)) < 30)
    )

    visited = np.zeros((h, w), dtype=bool)
    queue = []

    # Thêm các pixel viền ngoài nếu là màu nền nghi vấn
    for x in range(w):
        if is_bg_color[0, x]:
            queue.append((0, x))
            visited[0, x] = True
        if is_bg_color[h - 1, x] and not visited[h - 1, x]:
            queue.append((h - 1, x))
            visited[h - 1, x] = True
    for y in range(h):
        if is_bg_color[y, 0] and not visited[y, 0]:
            queue.append((y, 0))
            visited[y, 0] = True
        if is_bg_color[y, w - 1] and not visited[y, w - 1]:
            queue.append((y, w - 1))
            visited[y, w - 1] = True

    # Loang BFS 4 hướng từ các pixel viền ngoài
    head = 0
    while head < len(queue):
        cy, cx = queue[head]
        head += 1
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx]:
                if is_bg_color[ny, nx]:
                    visited[ny, nx] = True
                    queue.append((ny, nx))

    # Set Alpha = 0 cho tất cả pixel nền ngoài đã loang tới
    data[visited, 3] = 0
    return Image.fromarray(data)


def process_and_save_logo_image(content: bytes, save_path: str, remove_bg: bool = False) -> str:
    """Xử lý và lưu file logo. Chuyển đổi mọi định dạng ảnh (PNG, JPG, WEBP) thành PNG."""
    img = Image.open(io.BytesIO(content))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    
    if remove_bg:
        img = remove_logo_background(img)

    img.save(save_path, format="PNG")
    return save_path.replace("\\", "/")


def remove_logo_background_file(logo_path: str) -> str:
    """Xoá nền trực tiếp trên file logo đã tồn tại."""
    if not os.path.exists(logo_path):
        raise FileNotFoundError(L("Chưa có file logo để xoá nền", "No logo file yet to remove the background from"))
    with open(logo_path, "rb") as f:
        content = f.read()
    img = Image.open(io.BytesIO(content))
    cleaned = remove_logo_background(img)
    cleaned.save(logo_path, format="PNG")
    return logo_path.replace("\\", "/")


def process_and_save_frame_bg_image(content: bytes, bg_dir: str) -> str:
    """Lưu ẢNH NỀN KHUNG (chỗ trống quanh video khi khung rộng hơn video).

    Khác logo ở ba điểm, nên không dùng chung hàm:
      · Ảnh nền là ảnh CHỤP, lưu PNG như logo thì một tấm 4K phình lên hàng chục MB
        nằm trong thư mục dự án mãi mãi — nén JPEG khi ảnh không có kênh trong suốt.
      · Hạ xuống tối đa 3840px: nền chỉ để phủ khung xuất (tối đa 2K), giữ ảnh gốc
        to hơn thế chỉ làm ffmpeg đọc chậm chứ không nét thêm.
      · TÊN TỆP MANG DẤU THỜI GIAN, và xoá các bản cũ. Ghi đè cùng một đường dẫn thì
        trình duyệt vẫn hiện ảnh CŨ trong khung xem trước (URL không đổi) — đúng cái
        bẫy đã gặp với logo.
    """
    img = Image.open(io.BytesIO(content))
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    img = img.convert("RGBA" if has_alpha else "RGB")

    max_side = 3840
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize(
            (max(1, round(img.width * ratio)), max(1, round(img.height * ratio))),
            Image.LANCZOS,
        )

    os.makedirs(bg_dir, exist_ok=True)
    for old in os.listdir(bg_dir):
        if old.startswith("frame-bg"):
            try:
                os.remove(os.path.join(bg_dir, old))
            except OSError:
                pass

    stamp = int(time.time())
    if has_alpha:
        out = os.path.join(bg_dir, f"frame-bg-{stamp}.png")
        img.save(out, format="PNG")
    else:
        out = os.path.join(bg_dir, f"frame-bg-{stamp}.jpg")
        img.save(out, format="JPEG", quality=90)
    return out.replace("\\", "/")
