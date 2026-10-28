"""Shared compositor helpers for dubbing + review pipelines."""
import os
import re
import json
from datetime import datetime, timezone

DEFAULT_COMPOSITOR_CONFIG = {
    "version": 1,
    "aspectRatio": "source",
    "customAspectW": 9,
    "customAspectH": 16,
    "cropFrame": {"x": 0, "y": 0, "w": 1, "h": 1},
    #  NEN cua khung — chi thay khi nguoi dung THU NHO video cho lot vao khung (luc do
    #  quanh video co cho trong). "blur" = chinh khung hinh do phong to + lam mo (kieu
    #  TikTok/CapCut), "color" = mot mau don, "image" = anh nguoi dung tai len
    #  (khoa ``image`` giu duong dan tren dia).
    "frameBackground": {"type": "blur", "color": "#000000", "blur": 28, "image": ""},
    "resolution": "1080p",
    "mirror": False,
    "maskZones": [],
    #  Dot them PHU DE GOC (cau nhan dang / nhap vao) vao video xuat ra — nut "Sub goc"
    #  o khung xem truoc. Mac dinh TAT: phu de goc von chi de doi chieu khi lam viec.
    "showSourceSubs": False,
    "logo": {
        "enabled": False,
        "path": "",
        "x": 0.8,
        "y": 0.05,
        "w": 0.15,
        "h": 0.1,
        "opacity": 1,
    },
    "watermark": {
        "enabled": False,
        "text": "",
        "x": 0.05,
        "y": 0.03,
        "fontSize": 28,
        "color": "#FFFFFF",
        "opacity": 0.85,
        "animation": "float",
        "speed": 0.4,
        "bold": True,
        "italic": False,
    },
    "audioTrack": {
        "enabled": False,
        "path": "",
        "name": "",
        "volume": 0.5,
        "loop": True,
    },
    # MIRROR của DEFAULT_SUBTITLE_STYLE bên frontend/src/types/compositor.ts —
    # đổi mặc định phải sửa CẢ HAI nơi (GET /compositor trả block này cho dự án
    # chưa lưu config). Mặc định: cụm 8 chữ, IN HOA + nghiêng.
    "subtitleStyle": {
        "mode": "chunked",
        "fontFamily": "Segoe UI",
        "fontSize": 64,
        "bold": True,
        "italic": True,
        "uppercase": True,
        "outlineWidth": 1.5,
        "shadowDepth": 1,
        "primaryColor": "#FFE566",
        "secondaryColor": "#FFE566",
        "karaokeStyle": "active-only",
        "karaokeWindowSize": 3,
        "chunkWordCount": 8,
        "karaokeTickSound": True,
        "alignment": 2,
        "marginV": 100,
        "backBox": False,
        "backBoxColor": "#000000",
        "backBoxOpacity": 0.6,
        #  ĐỆM NGANG / DỌC của nền phụ đề (thang PlayRes, cùng đơn vị với ``outlineWidth``).
        #  ``None`` = theo độ dày viền như trước (một lớp BorderStyle 4). Có số thì
        #  ``ass_generator`` vẽ nền thành LỚP RIÊNG để đệm không kéo dày viền chữ.
        #  MIRROR: ``DEFAULT_SUBTITLE_STYLE`` (frontend/src/types/compositor.ts).
        "backBoxPadX": None,
        "backBoxPadY": None,
        #  BÁN KÍNH BO GÓC nền (thang PlayRes). ``None``/0 = góc vuông như cũ. > 0 thì
        #  ``ass_generator`` vẽ nền bằng hình vector (xem services/ass_nen_bo_goc.py).
        #  MIRROR: ``backBoxRadius`` (frontend/src/types/compositor.ts).
        "backBoxRadius": None,
        "fontFile": "",
        "posX": 0.05,
        "posY": 0.78,
        "boxW": 0.9,
        "boxH": 0.1,
    },
}


def _prune_missing_assets(config: dict) -> dict:
    """Dọn path file đã bị xóa/mất (logo, audio) — tránh UI hiển thị nhầm là còn
    và tránh export âm thầm bỏ qua. Tắt luôn để buộc người dùng tải lại."""
    logo = config.get("logo")
    if isinstance(logo, dict):
        path = logo.get("path")
        if path and not os.path.exists(path):
            logo["enabled"] = False
            logo["path"] = ""
    frame_bg = config.get("frameBackground")
    if isinstance(frame_bg, dict):
        path = frame_bg.get("image")
        if path and not os.path.exists(path):
            # Anh nen mat thi quay ve nen mo — de nguyen "image" thi khung xem truoc ve
            # o trong con ban xuat lai ra nen mo, hai noi khac nhau ma khong ai bao.
            frame_bg["image"] = ""
            if frame_bg.get("type") == "image":
                frame_bg["type"] = "blur"
    audio = config.get("audioTrack")
    if isinstance(audio, dict):
        path = audio.get("path")
        if path and not os.path.exists(path):
            audio["enabled"] = False
            audio["path"] = ""
            audio["name"] = ""
    return config


# Override mặc định sub cho dự án LỒNG TIẾNG: hiện full câu (mode "normal").
# Review dùng nguyên DEFAULT (cụm 8 chữ) — theo yêu cầu user 2026-07-27.
DUBBING_SUBTITLE_DEFAULTS = {"mode": "normal"}


def _pos_y_sub_phu(crop) -> float:
    """Vị trí đứng MẶC ĐỊNH của dòng phụ: 0,90 nhưng không lọt ra ngoài KHUNG CẮT (toạ độ
    phụ đề tính theo video gốc — cắt 9:16 giữ y 0,22→0,90 mà đặt 0,90 là mất trọn dòng).
    Không cắt khung -> 0,90 y cũ. MIRROR: ``posYSubPhu`` ở frontend/src/utils/secondSubStyle.ts."""
    day = 1.0
    if isinstance(crop, dict):
        try:
            day = min(1.0, float(crop.get("y") or 0) + float(crop.get("h") if crop.get("h") is not None else 1))
        except (TypeError, ValueError):
            day = 1.0
    return max(0.0, min(0.90, day - 0.10))


def kieu_sub_phu(config: dict) -> dict:
    """KIỂU CHỮ của DÒNG PHỤ ĐỀ PHỤ (lớp "Sub gốc" / thứ tiếng thứ hai).

    ``secondSubStyle`` người dùng đã chỉnh -> dùng nó (khoá thiếu lấy theo sub chính).
    Chưa chỉnh (None) -> SUY từ sub chính y như trước 29-09-2026: chữ tĩnh, cỡ 72%, sát đáy
    (posY 0,90 / boxH 0,10), màu phụ — dự án cũ xuất ra không đổi một điểm ảnh.
    MIRROR: ``kieuSubPhu`` ở frontend/src/utils/secondSubStyle.ts."""
    base = dict(config.get("subtitleStyle") or {})
    rieng = config.get("secondSubStyle")
    if isinstance(rieng, dict) and rieng:
        return {**base, **rieng}
    return {
        **base,
        #  "normal" = chu tinh, ca cau hien mot lan. AssGenerator chi biet 4 che do
        #  (normal / word-by-word / chunked / karaoke) — dat ten khac la no khong sinh dong
        #  Dialogue nao ca, file ASS ra rong tuech ma khong bao loi.
        "mode": "normal",
        "karaokePop": False,
        "fontSize": max(8, int(float(base.get("fontSize") or 42) * 0.72 + 0.5)),
        #  Sat day khung, DUOI lop loi binh.
        "posY": _pos_y_sub_phu(config.get("cropFrame")),
        "boxH": 0.10,
        #  Mau phu: phan biet ngay voi loi binh ma khong phai them o chon mau.
        "primaryColor": base.get("secondaryColor") or "#FFFFFF",
    }


def load_compositor_config(
    config_path: str, subtitle_defaults: dict | None = None
) -> dict:
    """Đọc config compositor; subtitle_defaults (nếu có) đè lên default sub
    KHI dự án chưa tự lưu giá trị tương ứng — cho phép mỗi chế độ (lồng tiếng /
    review) có mặc định hiển thị sub riêng."""
    default_sub = {
        **DEFAULT_COMPOSITOR_CONFIG["subtitleStyle"],
        **(subtitle_defaults or {}),
    }
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Du an cu da bi ghi long mot tang (xem unwrap_compositor_payload) — boc ra
        # ngay khi doc, khong thi chung mai mai tra ve gia tri mac dinh.
        data = unwrap_compositor_payload(data)
        merged = {**DEFAULT_COMPOSITOR_CONFIG, **data}
        merged["subtitleStyle"] = {
            **default_sub,
            **(data.get("subtitleStyle") or {}),
        }
        #  Kiểu sub PHỤ: None = suy từ sub chính (``kieu_sub_phu``); có thì ghép lên mặc định
        #  để dự án lưu thiếu khoá vẫn đủ trường.
        if isinstance(data.get("secondSubStyle"), dict) and data["secondSubStyle"]:
            merged["secondSubStyle"] = {**default_sub, **data["secondSubStyle"]}
        if "logo" in data:
            merged["logo"] = {
                **DEFAULT_COMPOSITOR_CONFIG["logo"],
                **data["logo"],
            }
        if "watermark" in data:
            merged["watermark"] = {
                **DEFAULT_COMPOSITOR_CONFIG["watermark"],
                **data["watermark"],
            }
        if "audioTrack" in data:
            merged["audioTrack"] = {
                **DEFAULT_COMPOSITOR_CONFIG["audioTrack"],
                **data["audioTrack"],
            }
        if "cropFrame" in data:
            merged["cropFrame"] = {
                **DEFAULT_COMPOSITOR_CONFIG["cropFrame"],
                **data["cropFrame"],
            }
        if "frameBackground" in data and isinstance(data["frameBackground"], dict):
            merged["frameBackground"] = {
                **DEFAULT_COMPOSITOR_CONFIG["frameBackground"],
                **data["frameBackground"],
            }
        if "maskZones" in data and isinstance(data["maskZones"], list):
            merged["maskZones"] = data["maskZones"]
        return _prune_missing_assets(merged)
    return {**DEFAULT_COMPOSITOR_CONFIG, "subtitleStyle": default_sub}


def unwrap_compositor_payload(data: dict) -> dict:
    """Boc lop ``{"config": {...}}`` neu client goi kem.

    Trinh dung NANG CAO PUT ca goi ``{config: <payload>}``, con endpoint thi ghi
    NGUYEN VAN body xuong dia. Ket qua la file cau hinh co mot khoa ``config`` long
    ben trong, con cac khoa o TANG NGOAI van la gia tri mac dinh — moi thu doc file
    nay o tang ngoai (tab ghep hinh co dien, ``record_compositor_export``, bat ky
    doan backend nao doc ``config["maskZones"]``) deu thay du an TRONG KHONG: khong
    vung che, khong dau mo, ty le "source" du nguoi dung da chon "tablet".

    Boc o day chu khong o tung route: moi duong ghi cau hinh deu di qua
    ``save_compositor_config``, nen sua mot cho la het.
    """
    if not isinstance(data, dict):
        return data
    trong = data.get("config")
    if isinstance(trong, dict) and "subtitleStyle" in trong:
        # Giu lai cac khoa NGOAI ma goi long khong co (lastExportPath...) roi moi
        # de goi trong de len — khong thi ghi cau hinh se xoa mat lan xuat gan nhat.
        ngoai = {k: v for k, v in data.items() if k != "config"}
        return {**ngoai, **trong}
    return data


def save_compositor_config(config_path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(unwrap_compositor_payload(data), f, ensure_ascii=False, indent=2)


def record_compositor_export(config_path: str, output_path: str, full: bool = True) -> None:
    """Ghi nhớ lần xuất compositor gần nhất (hiển thị khi mở tab).

    ``full=False`` cho lượt "Xuất đoạn": vẫn ghi ``lastExport*`` (tab hiện lần xuất gần
    nhất), nhưng KHÔNG đụng ``lastFullExport*`` — đó là BẢN XUẤT CẢ PHIM (đã đốt phụ đề,
    logo) mà tab Đăng YouTube dùng. Trước đây chỉ có một khoá nên xuất một đoạn xong là
    mất dấu bản cả phim, còn tab YouTube thì lấy ``final_review.mp4`` — bản trung gian
    của bước 1/2, chưa có phụ đề (người dùng báo 25-09-2026).
    """
    data = load_compositor_config(config_path)
    normalized = os.path.normpath(output_path).replace("\\", "/")
    size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    luc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data["lastExportPath"] = normalized
    data["lastExportSize"] = size
    data["lastExportAt"] = luc
    if full:
        data["lastFullExportPath"] = normalized
        data["lastFullExportSize"] = size
        data["lastFullExportAt"] = luc
    save_compositor_config(config_path, data)


def full_export_path(config_path: str) -> str:
    """Đường dẫn BẢN XUẤT CẢ PHIM gần nhất còn tồn tại trên đĩa, hoặc "".

    Dự án xuất trước khi có ``lastFullExportPath`` chỉ có ``lastExportPath`` — dùng nó
    nếu không phải tệp đoạn (``[ĐOẠN n] …``, xem ``batch_routes.tien_to_doan``)."""
    data = load_compositor_config(config_path)
    for khoa in ("lastFullExportPath", "lastExportPath"):
        p = str(data.get(khoa) or "").strip()
        if not p:
            continue
        if khoa == "lastExportPath" and os.path.basename(p).startswith("[ĐOẠN"):
            continue
        if os.path.isfile(p):
            return p
    return ""


def review_b_chunks_to_segments(
    project_dir: str,
    b_chunks: list,
    *,
    a_chunks_lookup: dict | None = None,
    fit_to_voice: bool = False,
) -> list:
    """Chuyển b_chunks review → segments compositor (timeline tuyệt đối).

    KHÔNG đòi phải có a_chunks. Trước đây thiếu lookup là trả về danh sách RỖNG, tức
    chế độ Review nâng cao có thể xuất ra video KHÔNG CÓ CHỮ NÀO mà không báo gì: ở đó
    câu B tự đứng được bằng ``start_override``/``end_override`` (người dùng dán kịch bản
    rồi tự đặt vị trí trên timeline, không cần chạy "Lấy phụ đề"). Khung xem trước vẫn
    vẽ phụ đề vì nó đọc timeline, còn bước ĐỐT CHỮ lại bỏ trắng — nghe/nhìn một đằng,
    xuất ra một nẻo. Tệp .srt thì vốn đã bỏ qua từng câu không xác định được thời gian
    (xem ``_generate_srt_overlay``); ở đây làm y như vậy cho hai đường khớp nhau.

    Với đường Review cổ điển thì không đổi gì: dự án ở đó luôn có a_chunks.

    ``fit_to_voice`` (chỉ chế độ NÂNG CAO bật): tắt chữ khi GIỌNG tắt thay vì khi hết
    khung. Khung là chỗ dành sẵn chứ không phải độ dài giọng — giọng ngắn hơn khung thì
    chữ còn nằm lại trên màn sau khi đã đọc xong. Xem ``TL.subtitle_end``.
    """
    segments = []
    lookup = a_chunks_lookup or {}

    from services.review_render_cache import resolve_b_time

    for b in b_chunks:
        # Ưu tiên override time (người dùng sửa ở màn Review) rồi mới suy từ A —
        # khớp đúng vị trí voice/SRT lúc render.
        start_time, end_time = resolve_b_time(b, lookup)
        if start_time is None:
            continue
        if end_time <= start_time:
            end_time = start_time + max(float(b.get("duration_seconds") or 0.1), 0.1)
        if fit_to_voice:
            from services import review_timeline as _TL

            end_time = _TL.subtitle_end(project_dir, b, start_time, end_time)
        segments.append({
            "start_time": start_time,
            "end_time": end_time,
            "source_text": b.get("b_text", ""),
            "target_text": b.get("b_text", ""),
            #  Để đổi sang bản phụ đề thứ tiếng khác khi xuất (services/review_sub_tracks).
            "uid": b.get("uid") or "",
        })
    return segments


def get_fonts_dir(project_root: str) -> str:
    fonts_dir = os.path.join(project_root, "assets", "fonts")
    os.makedirs(fonts_dir, exist_ok=True)
    return fonts_dir


# ---------------------------------------------------------------- loi ffmpeg
#: Dong dau stderr cua ffmpeg luon la banner phien ban + cau hinh build + danh sach
#: thu vien, roi tiep den libass quet TOAN BO font he thong. Tren Windows phan do mot
#: minh no da hon 2000 ky tu. Nguyen nhan THAT thi luon nam o DONG CUOI.
_FFMPEG_MO_DAU = (
    "ffmpeg version",
    "built with",
    "configuration:",
    "Copyright (c)",
    "Press [q]",
)
_FFMPEG_TU_NHIEU = (
    "fontselect",
    "Using font",
    "fontconfig",
    "font_provider",
    "Glyph 0x",
)
_RE_THU_VIEN = re.compile(r"^lib[a-z]+\s+\d+\.")
#: Dong ffmpeg in ra LAN LUOT tung tac vu don dep SAU khi nguyen nhan that da xay ra.
#: Mot loi filter sinh ra ca chuc dong nhu the (moi tac vu 2 dong "Task finished" +
#: "Terminating thread") — chung day dong nguyen nhan ra khoi 700 ky tu cuoi. Da co ca
#: that: man hinh loi chi con "-22 (Invalid argument)", mat dong "Parsed_crop ... Invalid
#: too big" o tren, nen khong ai biet la vung che rong hon khung hinh.
_FFMPEG_HE_QUA = (
    "Task finished with error code",
    "Terminating thread with return code",
    "Could not open encoder before EOF",
    "Nothing was written into output file",
    "received no packets",
    "Error reinitializing filters",
    "Failed to configure",
    "Conversion failed",
)
_RE_TIEN_DO = re.compile(r"^frame=\s*\d+")


def tom_loi_ffmpeg(stderr: str, gioi_han: int = 700) -> str:
    """Rut phan CO NGHIA o CUOI stderr ffmpeg, bo banner + rac quet font.

    VI SAO CAN: bao loi gui ve bi cat theo DAU chuoi (``[:500]`` o nhieu cho), ma dau
    stderr ffmpeg toan banner va libass liet ke font Windows — nen loi khach gui len
    chi co rac, khong co nguyen nhan. Da co mot ca that: ba lan "Compositor render
    failed" cua cung mot khach, ca ba deu khong chan doan duoc vi ly do that bi cat mat.

    Cat SAN o day thay vi sua tung cho ``[:500]`` phia sau: chuoi tra ra da du ngan nen
    moi tang cat phia duoi khong con dong toi noi dung nua.
    """
    tho = (stderr or "").strip()
    if not tho:
        return ""
    giu = []
    for dong in tho.splitlines():
        d = dong.strip()
        if not d or d.startswith(_FFMPEG_MO_DAU) or _RE_THU_VIEN.match(d):
            continue
        if any(t in d for t in _FFMPEG_TU_NHIEU):
            continue
        #  "Terminating thread" luon lap lai y dong "Task finished" ngay tren no, con
        #  dong tien do "frame=..." khong noi gi ve loi.
        if "Terminating thread with return code" in d or _RE_TIEN_DO.match(d):
            continue
        giu.append(d)
    if not giu:  # ca la: toan bo deu bi loc -> tha con hon mat trang
        giu = [d.strip() for d in tho.splitlines() if d.strip()]

    #  NGUYEN NHAN GOC = dong NGAY TRUOC dong hau qua dau tien (dau stderr con thong tin
    #  luong "Input #0 / Stream #0:0" nen khong lay dong dau). Luon giu no, ke ca khi
    #  phan duoi da day cho.
    goc = None
    for d in giu:
        if any(t in d for t in _FFMPEG_HE_QUA):
            break
        goc = d
    if goc is not None and len(goc) + 1 >= gioi_han:
        goc = None
    cho_goc = len(goc) + 1 if goc else 0

    ra, do_dai = [], 0
    for d in reversed(giu):
        if ra and do_dai + len(d) + 1 + cho_goc > gioi_han:
            break
        ra.append(d)
        do_dai += len(d) + 1
    ra.reverse()
    if goc and goc not in ra:
        ra.insert(0, goc)
    return "\n".join(ra)[:gioi_han]
