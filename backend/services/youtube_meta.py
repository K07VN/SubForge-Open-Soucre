# -*- coding: utf-8 -*-
"""TIÊU ĐỀ / MÔ TẢ / THẺ do AI soạn + ẢNH ĐẠI DIỆN cho bài đăng YouTube.

HAI VIỆC TÁCH BẠCH, cùng một tệp vì chúng luôn đi cùng nhau trên màn soạn bài:

1. NHỜ AI SOẠN. Một lần gọi DeepSeek trả về ĐỦ BỐN THỨ: nhiều phương án tiêu đề,
   một mô tả, danh sách thẻ, và mấy dòng chữ ngắn để đắp lên ảnh đại diện. Gọi
   một lần chứ không bốn lần vì bốn thứ đó phải ĂN KHỚP nhau — tiêu đề nói một
   đằng, hook trên ảnh nói một nẻo thì người xem bấm vào rồi thoát ngay.

2. ẢNH ĐẠI DIỆN. Mặc định lấy khung đầu phim, nhưng BỎ QUA đoạn đen ở đầu (rất
   nhiều video mở màn bằng fade-in) — lấy đúng khung 0 giây thì thumbnail là một
   ô đen tuyền. Người dùng đổi được sang khung khác, và đắp chữ lên.

VÌ SAO PROMPT NẰM Ở ĐÂY MÀ KHÔNG CHÉP SANG FRONTEND: chỗ khác trong repo có cặp
prompt mirror FE↔BE và chúng đã lệch nhau vài lần. Ở đây màn soạn bài GỌI XUỐNG
lấy prompt (``/youtube/ai/prompt``) rồi mới hiện ra cho người dùng chép tay, nên
chỉ có MỘT bản prompt duy nhất — không có gì để lệch.
"""
from utils.i18n import L
import json
import logging
import os
import re
import shutil
import subprocess
import time

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("YoutubeMeta")

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

#  YouTube khuyến nghị 1280×720 và CHẶN tệp trên 2MB.
#  HAI CỠ KHUNG. YouTube gợi ý 1280×720 cho video ngang, nhưng app này chủ yếu
#  làm Shorts — video DỌC. Ép ảnh bìa của một video dọc vào khung ngang nghĩa là
#  hai bên phải lấp nền mờ, và chữ người dùng đặt nằm trên vùng nền mờ đó chứ
#  không nằm trên hình. Tài liệu ``thumbnails.set`` không hề ràng buộc tỷ lệ —
#  chỉ giới hạn dung lượng và định dạng — nên khung dọc gửi lên được.
KHUNG = {
    "ngang": (1280, 720),
    "doc": (720, 1280),
}
RONG, CAO = KHUNG["ngang"]
TEN_KHUNG = {"ngang": "Ngang 16:9", "doc": "Dọc 9:16 (Shorts)"}


def co_khung(khung: str = "ngang") -> tuple:
    """(rộng, cao) của một khung. Tên lạ thì về khung ngang."""
    return KHUNG.get((khung or "").strip().lower(), KHUNG["ngang"])


def doan_khung(rong: int, cao: int) -> str:
    """Video này nên dùng khung nào? Cao hơn rộng = dọc."""
    return "doc" if (cao or 0) > (rong or 0) else "ngang"
TRAN_BYTE = 2 * 1024 * 1024


# --------------------------------------------------------------------------- #
#  1. NHỜ AI SOẠN
# --------------------------------------------------------------------------- #
def dung_prompt(nc: dict) -> str:
    """Soạn prompt từ ngữ cảnh một video. Đây là BẢN DUY NHẤT của prompt này.

    ``nc`` gồm: ``chu_de`` (tên dự án/tệp), ``noi_dung`` (vài câu đầu của phụ đề
    — thứ duy nhất nói lên video THỰC SỰ nói gì), ``kenh``, ``phong_cach``,
    ``ngon_ngu``, ``tu_khoa``.
    """
    #  DẠNG VIDEO: "ngan" (Shorts) · "dai" (video thường) · "" = để AI tự liệu.
    #  KHÔNG mặc định là Shorts. Cùng một nội dung nhưng tiêu đề cho Shorts (giật,
    #  cụt, hợp lướt dọc) và cho video dài (rõ chủ đề, hợp tìm kiếm) khác hẳn nhau;
    #  đóng đinh một dạng là nửa số người dùng nhận tiêu đề sai kiểu.
    dang = (nc.get("dang") or "").strip()
    if not dang:
        try:
            giay = float(nc.get("thoi_luong") or 0)
        except (TypeError, ValueError):
            giay = 0.0
        #  YouTube tính là Shorts khi video DƯỚI 3 PHÚT (và khung dọc). Dưới ngưỡng
        #  đó thì đoán là ngắn, còn lại để trống cho AI trung tính.
        dang = "ngan" if 0 < giay <= 180 else ("dai" if giay > 180 else "")

    chu_de = (nc.get("chu_de") or "").strip()
    noi_dung = (nc.get("noi_dung") or "").strip()
    kenh = (nc.get("kenh") or "").strip()
    phong_cach = (nc.get("phong_cach") or "").strip()
    ngon_ngu = (nc.get("ngon_ngu") or "vi").strip()
    tu_khoa = (nc.get("tu_khoa") or "").strip()

    ten_ngon_ngu = {
        "vi": "tiếng Việt", "en": "tiếng Anh", "th": "tiếng Thái",
        "id": "tiếng Indonesia", "zh-cn": "tiếng Trung", "ja": "tiếng Nhật",
        "ko": "tiếng Hàn", "es": "tiếng Tây Ban Nha", "fr": "tiếng Pháp",
        "zh": "tiếng Trung", "de": "tiếng Đức", "ru": "tiếng Nga",
        "pt": "tiếng Bồ Đào Nha", "it": "tiếng Ý",
    }.get(ngon_ngu, ngon_ngu)

    loi_dang = {
        "ngan": ("Đây là VIDEO NGẮN (Shorts, khung dọc, người xem lướt qua trong "
                 "một giây): tiêu đề phải bắt ngay ở vài chữ đầu, ngắn và gọn."),
        "dai": ("Đây là VIDEO DÀI (xem trên trang chủ / tìm kiếm): tiêu đề phải "
                "nói rõ video có gì, đủ từ khoá để tìm ra, không giật gân rỗng."),
    }.get(dang, "Chưa rõ video ngắn hay dài — hãy viết tiêu đề dùng được cho cả hai.")

    dong = [
        "Bạn là người viết tiêu đề cho kênh YouTube.",
        f"Hãy soạn phần chữ cho MỘT video, viết bằng {ten_ngon_ngu}.",
        loi_dang,
        "",
        "## Video",
    ]
    #  TÊN DỰ ÁN CHỈ ĐỂ THAM KHẢO. Rất nhiều dự án được đặt tên tắt ("video 3",
    #  "bản sửa cuối"), hoặc giữ nguyên tên tệp tải về bằng tiếng Trung, hoặc đã
    #  bị đổi tên cho dễ tìm. Không nói rõ điều này thì AI bám vào tên dự án và
    #  đẻ ra tiêu đề chẳng liên quan gì tới nội dung.
    if chu_de:
        dong += [
            f"- Tên dự án trong máy: {chu_de}",
            "  (CHỈ ĐỂ THAM KHẢO — tên này có thể đã bị đổi, viết tắt, hoặc là tên",
            "  tệp gốc. Nội dung bên dưới mới là căn cứ. Thấy tên lệch với nội dung",
            "  thì BỎ QUA tên.)",
        ]
    else:
        dong.append("- Chủ đề: (không rõ — hãy suy từ nội dung bên dưới)")
    if kenh:
        dong.append(f"- Kênh: {kenh}")
    if phong_cach:
        dong.append(f"- Phong cách kênh: {phong_cach}")
    if tu_khoa:
        dong.append(f"- Từ khoá muốn nhắm: {tu_khoa}")
    if noi_dung:
        dong += ["- Nội dung (trích phần đầu):", '"""' + noi_dung[:1800] + '"""']
    else:
        #  Không có lời thoại (đăng thẳng một tệp trong máy): lúc này tên dự án là
        #  thứ DUY NHẤT ta có, nên phải nói lại cho đúng — không thì AI làm theo
        #  câu "bỏ qua tên" ở trên và ngồi bịa.
        dong += [
            "- Nội dung: KHÔNG có lời thoại kèm theo. Chỉ được dựa vào tên và từ",
            "  khoá ở trên; chỗ nào không chắc thì viết chung chung, TUYỆT ĐỐI",
            "  không bịa chi tiết (con số, tên người, địa danh) không có căn cứ.",
        ]

    dong += [
        "",
        "## Yêu cầu",
        "1. `tieu_de`: 5 phương án KHÁC HẲN NHAU về góc tiếp cận (tò mò, con số,",
        "   xung đột, lời hứa, câu hỏi). Mỗi phương án TỐI ĐA 70 ký tự để không bị",
        "   cắt trên điện thoại. KHÔNG dùng dấu < hoặc >. Không viết hoa toàn bộ.",
        "2. `mo_ta`: 3-5 câu, câu ĐẦU đã phải nói rõ video có gì (YouTube chỉ hiện",
        "   khoảng 150 ký tự đầu). Không nhồi từ khoá, không kêu gọi sáo rỗng.",
        "3. `the`: 10-15 thẻ, chữ thường, không dấu #, xếp từ sát nghĩa nhất.",
        "   Tổng độ dài kể cả dấu phẩy phải dưới 450 ký tự.",
        "4. `hashtag`: 3 hashtag để gắn cuối mô tả, có dấu #."
        + (" Video ngắn thì thêm #Shorts vào cuối." if dang == "ngan" else ""),
        "5. `hook_anh`: 6 dòng chữ RẤT NGẮN (tối đa 4 từ) để đắp lên ảnh đại diện.",
        "   Đọc lướt phải hiểu ngay, không trùng nguyên văn tiêu đề.",
        "",
        "## Định dạng trả lời",
        "CHỈ trả về JSON, không giải thích gì thêm:",
        '{"tieu_de":["…"],"mo_ta":"…","the":["…"],"hashtag":["#…"],"hook_anh":["…"]}',
    ]
    return "\n".join(dong)


def ngu_canh_tu_du_an(p: dict, ngon_ngu_mac_dinh: str = "vi") -> dict:
    """Ngữ cảnh dựng prompt soạn bài, lấy từ MỘT dự án Chi tiết đã nạp.

    DÙNG CHUNG cho ba đường: lô chạy hàng loạt tự soạn, nút "chép prompt" của chế
    độ tự soạn, và nút soạn ngay trong bảng Chuẩn bị của dự án. Ba đường mà dựng
    ngữ cảnh theo ba lối thì prompt chép ra khác prompt gửi đi, và không ai phát
    hiện được vì cả ba đều "chạy được".

    ``p`` là kết quả ``review_advanced_engine.load_project``.
    """
    meta = p.get("meta") or {}
    b_list = p.get("b_chunks") or []
    try:
        thoi_luong = float((meta.get("state") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        thoi_luong = 0.0
    return {
        "chu_de": meta.get("title") or "",
        #  Lời thoại ĐÃ DỊCH — thứ duy nhất nói lên video thực sự nói gì.
        "noi_dung": " ".join(
            [str(x.get("b_text") or "").strip() for x in b_list[:80]]
        ),
        #  Thương hiệu và kiểu nói người dùng đã khai ở chính bảng Chuẩn bị.
        "kenh": meta.get("brand_name") or "",
        "phong_cach": meta.get("speech_style") or "",
        "ngon_ngu": meta.get("target_language") or ngon_ngu_mac_dinh or "vi",
        "tu_khoa": "",
        "dang": "",
        "thoi_luong": thoi_luong,
    }


def _boc_json(noi_dung: str) -> dict:
    """Lấy JSON ra khỏi câu trả lời, kể cả khi model bọc trong ```json … ```.
    Chịu được lỗi vặt hay gặp ở model nhỏ (Gemini 3.5-flash-lite trả JSON hỏng 1/2 lần thử, 07-10-2026): dấu phẩy thừa trước ``]`` / ``}``,
    chú thích ``// …``. Vẫn hỏng thì ném ``ValueError`` để nơi gọi thử lại."""
    s = (noi_dung or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    dau, cuoi = s.find("{"), s.rfind("}")
    if dau == -1 or cuoi <= dau:
        raise ValueError(L("Câu trả lời của AI không có JSON.", "The AI response contains no JSON."))
    s = s[dau:cuoi + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        s2 = re.sub(r"^\s*//[^\n]*$", "", s, flags=re.M)         # dòng chú thích
        s2 = re.sub(r",(\s*[\]}])", r"\1", s2)                   # phẩy thừa
        try:
            return json.loads(s2)
        except json.JSONDecodeError:
            raise ValueError(L(f"JSON của AI không đọc được ({e.msg} ở ký tự {e.pos}).", f"The AI's JSON could not be read ({e.msg} at character {e.pos}).")) from e


def _chuoi_list(v, tran: int) -> list:
    """Ép về danh sách chuỗi sạch. Model hay trả chuỗi thay vì mảng."""
    if isinstance(v, str):
        v = list(re.split(r"[\n,]", v))
    if not isinstance(v, list):
        return []
    ra = []
    for x in v:
        t = str(x or "").strip().strip('"').lstrip("#").strip()
        if t and t not in ra:
            ra.append(t)
    return ra[:tran]


def sinh_metadata(nc: dict, model: str = "", prompt_tay: str = "") -> dict:
    """Gọi DeepSeek một lần, trả về đủ 5 phần đã chuẩn hoá.

    ``prompt_tay`` khác rỗng thì dùng NGUYÊN VĂN prompt đó — chế độ người dùng tự
    sửa prompt. Vẫn ép trả JSON vì phần đọc kết quả không đổi.
    """
    from services import deepseek_service as DS

    prompt = (prompt_tay or "").strip() or dung_prompt(nc)
    loi_cuoi: Exception | None = None
    for lan in (1, 2):
        #  ``json_mode``: Gemini ép trả JSON hợp lệ (response_format json_object); DeepSeek bỏ qua (request giữ nguyên — golden).
        #  Model nhỏ thỉnh thoảng trả JSON hỏng → gọi lại MỘT lần (câu trả lời khác nhau mỗi lần, lần hai thường ổn).
        noi_dung = DS._chat(
            [{"role": "user", "content": prompt}],
            model,
            timeout=120,
            max_retries=2,
            max_tokens=2048,
            json_mode=True,
        )
        try:
            return chuan_hoa(_boc_json(noi_dung))
        except ValueError as e:
            loi_cuoi = e
            logger.warning(f"[YouTube meta] lần {lan}: {e} — {'gọi lại' if lan == 1 else 'bỏ cuộc'}.")
    raise ValueError(L(f"AI trả JSON không đọc được sau 2 lần ({loi_cuoi}). Thử lại, hoặc dùng nút chép prompt rồi dán JSON tay.", f"The AI returned unreadable JSON twice ({loi_cuoi}). Try again, or use the copy-prompt button and paste the JSON by hand."))


#: Ký tự "còn nữa" trong MẪU JSON của prompt: ``{"tieu_de":["…"], …}``.
#:
#: AI đôi khi chép nguyên cái mẫu ấy ra thay vì viết thật (hay gặp khi nội dung quá
#: ngắn, hoặc model nhỏ). Không chặn thì app lưu tiêu đề là "…" và màn hình khoe
#: "đã soạn · 1 tiêu đề · 1 thẻ · 1 câu hook" — nhìn như xong, tới lúc đăng mới biết.
_RONG_TUECH = {"…", "...", "..", ".", "-", "—"}


def _co_chu_that(s: str) -> bool:
    """Chuỗi này có chữ THẬT không, hay chỉ là dấu ba chấm của mẫu?"""
    t = (s or "").strip()
    return bool(t) and t not in _RONG_TUECH and any(c.isalnum() for c in t)


def chuan_hoa(data: dict) -> dict:
    """Dọn JSON thô của AI thành đúng 5 phần app dùng.

    TÁCH RA làm hàm riêng vì có HAI đường vào: lô tự gọi DeepSeek, và người dùng tự
    chạy AI bên ngoài rồi dán JSON về. Hai đường mà dọn theo hai lối là sớm muộn
    chúng lệch nhau — bản dán tay lọt qua một trần mà bản tự động chặn.
    """
    #  Nhập TẠI CHỖ: ``youtube_service`` là nơi giữ luật trần của YouTube (nó là
    #  bên gửi lên), còn tệp này lo phần soạn chữ. Nhập ở đầu tệp thì hai module
    #  tham chiếu vòng nhau.
    from services.youtube_service import cat_the

    tieu_de = [t for t in _chuoi_list(data.get("tieu_de"), 8)
               if "<" not in t and ">" not in t and _co_chu_that(t)]
    return {
        "tieu_de": tieu_de,
        "mo_ta": (lambda m: m if _co_chu_that(m) else "")(
            str(data.get("mo_ta") or "").strip()
        ),
        #  Cắt theo ĐÚNG cách YouTube đếm (dấu phẩy tính, thẻ có dấu cách +2) —
        #  thà bỏ mấy thẻ đuôi còn hơn để YouTube từ chối cả bài.
        "the": cat_the([t for t in _chuoi_list(data.get("the"), 20)
                        if _co_chu_that(t)], 450),
        "hashtag": ["#" + h.lstrip("#")
                    for h in _chuoi_list(data.get("hashtag"), 5)
                    if _co_chu_that(h)],
        "hook_anh": [h for h in _chuoi_list(data.get("hook_anh"), 8)
                     if _co_chu_that(h)],
    }


# --------------------------------------------------------------------------- #
#  2. ẢNH ĐẠI DIỆN
# --------------------------------------------------------------------------- #
def thu_muc_anh() -> str:
    from models.database import get_app_data_dir

    d = os.path.join(get_app_data_dir(), "youtube_thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def kich_thuoc_video(video: str) -> tuple:
    """(rộng, cao) của video, (0, 0) nếu không đọc được.

    Đọc từ chính dòng ``Stream ... 1080x1920`` mà ffmpeg in ra — không cần
    ffprobe riêng, và cũng chỉ một lượt gọi như ``thoi_luong``.
    """
    if not video or not os.path.exists(video):
        return (0, 0)
    res = subprocess.run(
        [FFMPEG_EXE, "-i", video],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="ignore", close_fds=True,
    )
    #  Bỏ qua các con số phía sau như "[SAR 1:1 DAR 9:16]" — chỉ lấy cặp đầu tiên
    #  đi ngay sau phần mô tả luồng hình.
    m = re.search(r"Stream #\d+:\d+.*?Video:.*?,\s*(\d{2,5})x(\d{2,5})", res.stderr or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def thoi_luong(video: str) -> float:
    if not video or not os.path.exists(video):
        return 0.0
    res = subprocess.run(
        [FFMPEG_EXE, "-i", video],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="ignore", close_fds=True,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", res.stderr or "")
    if not m:
        return 0.0
    h, p, s = m.groups()
    return int(h) * 3600 + int(p) * 60 + float(s)


#  Khung ảnh bìa LUÔN là 16:9, trong khi phần lớn video ở đây là video DỌC. Lấp
#  chỗ thừa bằng chính khung đó phóng to, làm mờ và tối đi — thay vì hai vệt đen
#  chiếm hơn nửa ảnh. Đây cũng là cách các kênh Shorts vẫn làm ảnh bìa.
def _loc_nen_mo(rong: int, cao: int) -> str:
    """Dựng bộ lọc theo ĐÚNG cỡ khung đang làm.

    Trước đây đây là một hằng số dựng sẵn cho 1280×720 — thêm khung dọc mà quên
    chỗ này là ảnh dọc vẫn bị nhét vào 1280×720 rồi mới đổi cỡ, méo hết.
    """
    return (
        "[0:v]split=2[nen][truoc];"
        f"[nen]scale={rong}:{cao}:force_original_aspect_ratio=increase,"
        f"crop={rong}:{cao},boxblur=luma_radius=40:luma_power=2,"
        "eq=brightness=-0.10:saturation=0.85[nenmo];"
        f"[truoc]scale={rong}:{cao}:force_original_aspect_ratio=decrease[vua];"
        "[nenmo][vua]overlay=(W-w)/2:(H-h)/2"
    )


def lay_khung(video: str, giay: float, dich: str, khung: str = "ngang") -> str:
    """Cắt MỘT khung tại ``giay`` ra tệp JPEG 1280×720 (nền mờ nếu lệch tỷ lệ).

    ``-ss`` đặt TRƯỚC ``-i`` để ffmpeg nhảy thẳng tới mốc thay vì giải mã từ đầu —
    video một tiếng thì đây là khác biệt giữa 0,2 giây và gần một phút.
    """
    if not video or not os.path.exists(video):
        raise ValueError(L("Không còn tệp video để lấy khung hình.", "The video file is gone, so no frame can be taken."))
    os.makedirs(os.path.dirname(dich) or ".", exist_ok=True)
    r_, c_ = co_khung(khung)
    res = subprocess.run(
        [FFMPEG_EXE, "-y", "-ss", f"{max(0.0, giay):.3f}", "-i", video,
         "-frames:v", "1", "-filter_complex", _loc_nen_mo(r_, c_), "-q:v", "2", dich],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True,
    )
    if res.returncode != 0 or not os.path.exists(dich) or os.path.getsize(dich) == 0:
        duoi = (res.stderr or b"").decode("utf-8", "ignore")[-400:]
        raise ValueError(L(f"Không cắt được khung hình. {duoi}", f"Could not cut the frame. {duoi}"))
    return dich


def anh_tu_tep(nguon: str, dich: str, khung: str = "ngang") -> str:
    """Đưa một ẢNH CÓ SẴN về đúng khung 1280×720 của ảnh bìa.

    Dùng CHUNG bộ lọc với ``lay_khung`` nên ảnh dọc người dùng tải lên được lấp
    nền mờ y như khung cắt từ video — không thì cùng một hộp thoại lại cho ra hai
    kiểu ảnh khác nhau tuỳ nguồn.
    """
    if not nguon or not os.path.exists(nguon):
        raise ValueError(L("Không còn tệp ảnh.", "The image file is gone."))
    os.makedirs(os.path.dirname(dich) or ".", exist_ok=True)
    r_, c_ = co_khung(khung)
    res = subprocess.run(
        [FFMPEG_EXE, "-y", "-i", nguon, "-frames:v", "1",
         "-filter_complex", _loc_nen_mo(r_, c_), "-q:v", "2", dich],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True,
    )
    if res.returncode != 0 or not os.path.exists(dich) or os.path.getsize(dich) == 0:
        duoi = (res.stderr or b"").decode("utf-8", "ignore")[-400:]
        raise ValueError(L(f"Không đọc được ảnh. {duoi}", f"Could not read the image. {duoi}"))
    return dich


def _do_sang(anh: str) -> float:
    """Độ sáng trung bình 0-255. Dùng để BỎ QUA đoạn đen ở đầu phim."""
    try:
        with Image.open(anh) as im:
            nho = im.convert("L").resize((32, 18))
            px = list(nho.getdata())
        return sum(px) / len(px)
    except Exception:
        return 255.0


def khung_mac_dinh(video: str, dich: str, khung: str = "") -> dict:
    """Khung đại diện mặc định: khung ĐẦU TIÊN không tối thui.

    Thử lần lượt 0,5s → 1,5s → 3s → 6s → 10s → 1/4 phim. Ngưỡng 24/255 bắt được
    cả fade-in lẫn logo trắng trên nền đen (vốn sáng trung bình rất thấp) mà
    không loại nhầm cảnh đêm thật.
    """
    #  Chưa nói rõ khung thì TỰ ĐOÁN theo hình dạng video: video dọc thì ảnh bìa
    #  dọc. Đây là mặc định đúng cho gần hết các bài trong app này (Shorts).
    rv, cv = kich_thuoc_video(video)
    if not khung:
        khung = doan_khung(rv, cv)
    dai = thoi_luong(video)
    moc = [0.5, 1.5, 3.0, 6.0, 10.0]
    if dai > 0:
        moc = [m for m in moc if m < dai] or [max(0.0, dai * 0.1)]
        moc.append(min(dai * 0.25, 30.0))
    cuoi = moc[0]
    for m in moc:
        lay_khung(video, m, dich, khung)
        sang = _do_sang(dich)
        cuoi = m
        if sang >= 24:
            return {"path": dich, "giay": m, "do_sang": round(sang, 1),
                    "khung": khung, "video_rong": rv, "video_cao": cv}
    #  Toàn phim tối (video đêm) — giữ khung cuối đã cắt, không để tay trắng.
    return {"path": dich, "giay": cuoi, "do_sang": round(_do_sang(dich), 1),
            "khung": khung, "video_rong": rv, "video_cao": cv}


# --------------------------------------------------------------------------- #
#  CHỮ ĐẮP LÊN ẢNH
# --------------------------------------------------------------------------- #
#  Mỗi kiểu: màu chữ, màu viền, dải nền phía sau (None = không có).
KIEU_CHU = {
    "vang_vien_den": {"ten": "Vàng viền đen", "chu": "#FFE000", "vien": "#000000", "nen": None},
    "trang_vien_den": {"ten": "Trắng viền đen", "chu": "#FFFFFF", "vien": "#000000", "nen": None},
    "trang_vien_do": {"ten": "Trắng viền đỏ", "chu": "#FFFFFF", "vien": "#D40000", "nen": None},
    "trang_nen_do": {"ten": "Chữ trắng nền đỏ", "chu": "#FFFFFF", "vien": "#7A0000", "nen": "#D40000"},
    "den_nen_vang": {"ten": "Chữ đen nền vàng", "chu": "#111111", "vien": "#FFFFFF", "nen": "#FFD400"},
    "trang_nen_den": {"ten": "Chữ trắng nền đen", "chu": "#FFFFFF", "vien": "#000000", "nen": "#000000"},
}

#  Font đắp lên ảnh — LẤY TỪ BỘ ĐÓNG GÓI của app, không lấy font hệ thống. Cả 8
#  font này đã kiểm là có ĐỦ DẤU TIẾNG VIỆT (Arial Black / Impact thì không, chữ
#  sẽ mất dấu hoặc rơi về font khác).
FONT_GOI_Y = [
    ("BeVietnamPro-Black.ttf", "Be Vietnam Pro Black"),
    ("Montserrat-Black.ttf", "Montserrat Black"),
    ("Kanit-Black.ttf", "Kanit Black"),
    ("Anton-Regular.ttf", "Anton"),
    ("Oswald-Bold.ttf", "Oswald Bold"),
    ("Prompt-Black.ttf", "Prompt Black"),
    ("Bungee.ttf", "Bungee"),
    ("AlfaSlabOne-Regular.ttf", "Alfa Slab One"),
]


def _thu_muc_font() -> str:
    goc = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(goc, "assets", "fonts")


def _nhan_font(ten_tep: str) -> str:
    """"BeVietnamPro-Black.ttf" -> "Be Vietnam Pro Black"."""
    ten = os.path.splitext(ten_tep)[0].replace("_", "-")
    phan = []
    for khuc in ten.split("-"):
        if not khuc:
            continue
        #  Tách chữ hoa dính nhau: "BeVietnamPro" -> "Be Vietnam Pro".
        phan.append(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", khuc))
    nhan = " ".join(phan)
    #  "Regular" trong tên tệp là mặt mặc định, hiện ra chỉ tổ dài dòng.
    return re.sub(r"\s+Regular$", "", nhan).strip() or ten_tep


#: Font đóng gói cho CHẾ ĐỘ KHÁC (chữ Nhật của "Sáng tạo") nằm chung thư mục ``assets/fonts`` nhưng KHÔNG có dấu tiếng Việt → không được bày ra cho ảnh bìa
#: (chữ ảnh bìa phần lớn là tiếng Việt, thiếu dấu là ra ô vuông). Tên tệp bắt đầu bằng một trong các tiền tố này thì bị loại.
FONT_KHONG_DAU_VIET = ("ZenMaruGothic",)


def fonts_tat_ca() -> list:
    """MỌI font đóng gói kèm app (trừ :data:`FONT_KHONG_DAU_VIET`), tám font quen đứng trước.

    Cả 43 tệp trong ``assets/fonts`` đã được đo là CÓ ĐỦ DẤU TIẾNG VIỆT
    (2026-09-22, dựng từng ký tự ``ếữợẳỹặẵđĐƯƠẮỬỊỄỢ`` rồi so với ô .notdef), nên
    bày hết ra là an toàn — khác hẳn font hệ thống, nơi Arial Black / Impact /
    Trebuchet MS mất dấu.
    """
    thu_muc = _thu_muc_font()
    try:
        co = sorted(
            t for t in os.listdir(thu_muc)
            if t.lower().endswith((".ttf", ".otf")) and not t.startswith(FONT_KHONG_DAU_VIET)
        )
    except OSError:
        co = []
    quen = [t for t, _ in FONT_GOI_Y if t in co]
    con_lai = [t for t in co if t not in quen]
    return (
        [{"tep": t, "ten": dict(FONT_GOI_Y).get(t) or _nhan_font(t), "quen": True}
         for t in quen]
        + [{"tep": t, "ten": _nhan_font(t), "quen": False} for t in con_lai]
    )


def _duong_font(ten_tep: str) -> str:
    """Đường dẫn tệp font. Tên rỗng / không có thật -> lấy font đầu danh sách.

    PHẢI kiểm ``isfile`` chứ không phải ``exists``: tên rỗng thì đường dẫn ghép ra
    chính là THƯ MỤC fonts, mà thư mục thì ``exists`` trả True — PIL nhận vào rồi
    ném "cannot open resource", lỗi chẳng nói lên điều gì.
    """
    goc = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    thu_muc = os.path.join(goc, "assets", "fonts")
    d = os.path.join(thu_muc, ten_tep) if ten_tep else ""
    if d and os.path.isfile(d):
        return d
    return os.path.join(thu_muc, FONT_GOI_Y[0][0])


#  Nhớ sẵn ký tự nào font vẽ được — mỗi lần hỏi là một lượt dựng ảnh nhỏ, mà một
#  loạt 90 ảnh bìa thì hỏi đi hỏi lại đúng những ký tự đó.
_NHO_VE_DUOC: dict = {}


def _ve_duoc(duong_font: str, co: int, ky_tu: str) -> bool:
    """Font này có hình cho ký tự này không?

    VÌ SAO PHẢI HỎI: cả 43 font đóng gói đều chỉ có Latinh + tiếng Việt. Không
    font nào có chữ Hán, Nhật, Hàn, Thái, Nga hay Ả Rập — đo thật 22-09-2026.
    Đắp một tiêu đề tiếng Trung lên ảnh thì 38 trên 53 ký tự ra Ô TRỐNG, còn lại
    trơ mấy dấu ``#`` và chữ "young" — đúng thứ người dùng vừa thấy trên ảnh bìa
    hàng loạt. Không có lỗi nào hiện ra, ảnh vẫn xuất ra bình thường, chỉ là nhìn
    như hỏng.
    """
    khoa = (duong_font, ky_tu)
    if khoa in _NHO_VE_DUOC:
        return _NHO_VE_DUOC[khoa]
    try:
        ft = ImageFont.truetype(duong_font, 40)

        def _ra(c):
            im = Image.new("L", (60, 60), 0)
            ImageDraw.Draw(im).text((6, 6), c, font=ft, fill=255)
            return im.tobytes()

        #  So với ô .notdef: vùng dùng riêng U+E000 chắc chắn không font nào có.
        ra = _ra(ky_tu) != _ra("\ue000")
    except OSError:
        ra = True  # không mở được font thì đừng chặn oan
    _NHO_VE_DUOC[khoa] = ra
    return ra


def loc_ve_duoc(cau: str, font_tep: str = "") -> str:
    """Bỏ khỏi ``cau`` những ký tự font không vẽ được.

    Giữ khoảng trắng và gộp lại cho gọn. Trả về chuỗi rỗng nếu chẳng còn gì đáng
    đọc — phía gọi nên hiểu đó là "đừng đắp chữ lên ảnh này".
    """
    if not cau:
        return ""
    d = _duong_font(font_tep)
    d = _duong_font(font_tep)
    dac = [c for c in cau if not c.isspace()]
    if not dac:
        return ""
    giu = [c if (c.isspace() or _ve_duoc(d, 40, c)) else " " for c in cau]
    con = re.sub(r"\s+", " ", "".join(giu)).strip()

    #  MẤT QUÁ NHIỀU THÌ BỎ HẲN, đừng cố vớt. Tiêu đề tiếng Trung lọc xong còn
    #  mấy dấu ngoặc với chữ "young" — vẫn là rác, chỉ khác là rác đọc được; còn
    #  "用1分40秒带你了解" rút lại thành "1 40", nhìn như app điền nhầm số. Cả hai
    #  đều tệ hơn hẳn một tấm ảnh không chữ.
    mat = sum(1 for c in dac if not _ve_duoc(d, 40, c))
    if mat > len(dac) * 0.3:
        return ""
    #  Còn lại toàn dấu câu cũng là rác.
    if sum(1 for c in con if c.isalnum()) < 2:
        return ""
    return con


def _mau(s: str, mac_dinh=(255, 255, 255)) -> tuple:
    s = (s or "").strip().lstrip("#")
    if len(s) != 6:
        return mac_dinh
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return mac_dinh


def _xuong_dong(chu: str, font, rong_toi_da: int, ve) -> list:
    """Ngắt dòng theo BỀ NGANG THẬT của chữ, không đếm ký tự.

    Đếm ký tự thì "Ừ THÌ" và "MMMMM" ra cùng một độ dài, mà trên ảnh chúng lệch
    nhau gần gấp đôi — dòng bị tràn ra ngoài khung.
    """
    tu = (chu or "").split()
    if not tu:
        return []
    dong, hien = [], tu[0]
    for t in tu[1:]:
        thu = f"{hien} {t}"
        if ve.textlength(thu, font=font) <= rong_toi_da:
            hien = thu
        else:
            dong.append(hien)
            hien = t
    dong.append(hien)
    return dong


def dap_chu(
    anh_nguon: str,
    dich: str,
    cac_dong: list,
    kieu: str = "vang_vien_den",
    font_tep: str = "",
    vi_tri: str = "duoi",
    co_chu: int = 100,
    x: float | None = None,
    y: float | None = None,
    khung: str = "",
) -> str:
    """Đắp chữ lên ảnh rồi lưu ra ``dich`` (JPEG, luôn dưới trần 2MB).

    ``co_chu`` là PHẦN TRĂM so với cỡ tự tính (cỡ tự tính đã lo cho chữ vừa khung),
    nên người dùng chỉ việc chỉnh to/nhỏ tương đối.

    ``x``/``y`` là TÂM khối chữ tính theo phần của khung (0..1) — dùng khi người
    dùng kéo chữ tới chỗ mình muốn. Bỏ trống thì rơi về ba nấc ``vi_tri`` cũ, nên
    ảnh đã lưu từ bản trước dựng lại vẫn y nguyên.
    """
    #  LỌC TRƯỚC KHI VẼ. Chỗ này gác cho MỌI đường vào — hộp ảnh bìa từng bài,
    #  lượt chạy cả loạt, câu do AI viết ở bất cứ thứ tiếng nào.
    cau = [x for x in (loc_ve_duoc(str(d), font_tep) for d in (cac_dong or [])) if x]
    with Image.open(anh_nguon) as im:
        #  GIỮ NGUYÊN cỡ ảnh nền nếu nó đã là một khung hợp lệ. Ép về 1280×720
        #  như bản trước là ảnh bìa dọc bị bóp méo ngay ở bước đắp chữ, dù bước
        #  cắt khung đã làm đúng.
        rong, cao = co_khung(khung) if khung else (
            im.size if tuple(im.size) in KHUNG.values() else (RONG, CAO)
        )
        anh = im.convert("RGB").resize((rong, cao))

    if cau:
        k = KIEU_CHU.get(kieu) or KIEU_CHU["vang_vien_den"]
        ve = ImageDraw.Draw(anh)
        le = int(rong * 0.06)
        rong_chu = rong - le * 2

        #  Cỡ chữ: bắt đầu to rồi thu dần tới khi CẢ khối chữ vừa 2/3 chiều cao.
        #  Tự tính thay vì để người dùng mò — hook 2 từ và hook 6 từ cần cỡ rất
        #  khác nhau, mà họ chỉ muốn "nhìn cho vừa".
        duong_font = _duong_font(font_tep)
        co = int(cao * 0.20 * max(30, min(200, co_chu)) / 100)
        while co > 18:
            font = ImageFont.truetype(duong_font, co)
            dong = []
            for c in cau:
                dong += _xuong_dong(c, font, rong_chu, ve) or [c]
            #  PHẢI CANH CẢ BỀ NGANG, không chỉ chiều cao. _xuong_dong ngắt
            #  theo khoảng trắng nên MỘT TỪ dài hơn khung thì nó bó tay — dòng đó
            #  cứ thế tràn ra ngoài và bị cắt cụt hai bên.
            #
            #  Khung dọc làm lỗi này lộ ra ngay lập tức: cỡ chữ tính từ CHIỀU CAO
            #  (720×0,2 = 144 với khung ngang, nhưng 1280×0,2 = 256 với khung dọc)
            #  trong khi bề ngang lại tụt từ 1280 xuống 720. Chữ "SHORTS" ở cỡ
            #  256 rộng hơn hẳn 634px lòng khung — đo thật, thấy rõ trên ảnh.
            rong_nhat = max((ve.textlength(d, font=font) for d in dong), default=0)
            if int(co * 1.18) * len(dong) <= cao * 0.66 and rong_nhat <= rong_chu:
                break
            co = int(co * 0.9)
        font = ImageFont.truetype(duong_font, co)
        dong = []
        for c in cau:
            dong += _xuong_dong(c, font, rong_chu, ve) or [c]
        cao_dong = int(co * 1.18)
        tong_cao = cao_dong * len(dong)

        if y is not None:
            #  Kẹp trong khung: kéo chữ ra sát mép thì phần thò ra ngoài bị cắt
            #  mất, người dùng tưởng chữ biến đâu mất.
            y_ve = int(float(y) * cao - tong_cao / 2)
            y_ve = max(le // 2, min(cao - tong_cao - le // 2, y_ve))
        elif vi_tri == "tren":
            y_ve = le
        elif vi_tri == "giua":
            y_ve = (cao - tong_cao) // 2
        else:
            y_ve = cao - tong_cao - le

        mau_chu = _mau(k["chu"])
        mau_vien = _mau(k["vien"], (0, 0, 0))
        day_vien = max(3, co // 11)

        if k["nen"]:
            #  Dải nền vẽ trên lớp RGBA riêng để có độ trong; dán đè lên ảnh.
            lop = Image.new("RGBA", anh.size, (0, 0, 0, 0))
            ImageDraw.Draw(lop).rectangle(
                [0, y_ve - int(co * 0.22), rong, y_ve + tong_cao + int(co * 0.18)],
                fill=_mau(k["nen"], (0, 0, 0)) + (225,),
            )
            anh = Image.alpha_composite(anh.convert("RGBA"), lop).convert("RGB")
            ve = ImageDraw.Draw(anh)

        #  Tâm ngang: mặc định giữa khung, hoặc chỗ người dùng kéo tới. Từng dòng
        #  vẫn căn giữa QUANH tâm đó chứ không căn trái — khối chữ hai dòng lệch
        #  nhau nhìn như lỗi.
        tam_x = rong / 2 if x is None else float(x) * rong
        rong_nhat = max((ve.textlength(d, font=font) for d in dong), default=0)
        tam_x = max(le + rong_nhat / 2, min(rong - le - rong_nhat / 2, tam_x))

        for i, d in enumerate(dong):
            rong_d = ve.textlength(d, font=font)
            ve.text(
                (tam_x - rong_d / 2, y_ve + i * cao_dong), d, font=font,
                fill=mau_chu, stroke_width=day_vien, stroke_fill=mau_vien,
            )

    os.makedirs(os.path.dirname(dich) or ".", exist_ok=True)
    #  Hạ chất lượng dần cho tới khi lọt trần 2MB của YouTube. Ảnh 1280×720 hầu
    #  như luôn xong ở vòng đầu; vòng lặp chỉ để không bao giờ xuất tệp bị từ chối.
    for chat_luong in (92, 85, 75, 65, 55):
        anh.save(dich, "JPEG", quality=chat_luong, optimize=True)
        if os.path.getsize(dich) <= TRAN_BYTE:
            break
    return dich


def ten_moi(hau_to: str = "jpg") -> str:
    return f"{int(time.time() * 1000)}.{hau_to}"
