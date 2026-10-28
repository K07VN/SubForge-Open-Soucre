from sqlalchemy import Column, String, Float, Integer, ForeignKey, Text, Boolean
from .database import Base


def _default_separate_audio() -> bool:
    """Mặc định TẮT tách giọng / nhạc nền cho MỌI máy (người dùng chốt 07-10-2026 — kể cả máy có GPU; bật tay khi cần).
    Bản trước: bật khi có GPU (Demucs trên CPU ~22 phút cho video 24 phút). Giữ hàm (cột gọi theo tên) để không đổi schema."""
    return False


def _default_separate_audio_cu() -> bool:
    """(Luật cũ, giữ để tham khảo) BẬT khi có GPU."""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001 — thiếu torch / lỗi driver -> coi như không GPU
        return False


class Video(Base):
    __tablename__ = "videos"
    id = Column(String, primary_key=True, index=True)
    title = Column(String)
    original_path = Column(String)
    audio_path = Column(String, nullable=True)
    final_video_path = Column(String, nullable=True)
    status = Column(String) # uploaded, audio_extracted, stt_completed, translated, voice_generated, completed
    
    # Granular phase status: pending, processing, completed, error
    p1_status = Column(String, default="pending")
    p2_status = Column(String, default="pending")
    p3_status = Column(String, default="pending")
    p4_status = Column(String, default="pending")
    p5_status = Column(String, default="pending")

    source_lang = Column(String, default="auto")
    target_lang = Column(String, default="vi")
    voice_talent = Column(String, default="vi-VN-NamMinhNeural")
    translation_engine = Column(String, default="google")
    speed_mode = Column(String, default="auto_fit")
    #  CHO GIONG TRAN SANG CAU SAU (07-10-2026, xem ``review_timeline.tran_giong_cho_phep``): giong duoc dai hon khung
    #  toi da ``tran_giong`` giay (de len dau cau sau) truoc khi ep toc do / cat. NULL (DB cu, cot them bang ALTER TABLE)
    #  = mac dinh BAT 0,85 s — moi noi doc phai qua ``_tran_cua_video`` (video_routes), khong doc thang cot.
    cho_tran_giong = Column(Boolean, default=True, nullable=True)
    tran_giong = Column(Float, default=0.85, nullable=True)
    #  AM LUONG GIONG DOC AI (Co ban, 07-10-2026): truoc day gan cung ``volume=1.5`` trong merge_audio_video; nay la cot
    #  (mac dinh 1,5 = 150%, kep 0–2 nhu Chi tiet ``tts_volume``). NULL (DB cu) = 1,5 — doc qua ``_tts_volume_cua_video``.
    tts_volume = Column(Float, default=2.0, nullable=True)     # du an MOI: 200% (nguoi dung chot 07-10-2026); NULL (du an cu) van 1,5

    # Mac dinh: nhac nen 100% (giu tron nhac), giong goc 0% (thay hoan toan bang
    # giong AI). Khi tat tach nguon, bg_volume/voice_bg_volume ap len TOAN BO
    # audio goc (luu y: 1.0 => nghe ro ca giong goc, nen chinh lai neu tat tach).
    bg_volume = Column(Float, default=0.5)          # mac dinh moi 07-10-2026: nhac nen 50% ca luc khong / co giong AI
    voice_bg_volume = Column(Float, default=0.5)

    # Tach giong goc / nhac nen (Demucs htdemucs). Mac dinh BAT (luon tach);
    # co the TAT bang toggle khi may yeu hoac model loi.
    # Khi BAT: bg_volume/voice_bg_volume chi ap len kenh NHAC NEN (no_vocals),
    # con 2 field duoi day ap len kenh GIONG GOC (vocals).
    separate_audio = Column(Boolean, default=_default_separate_audio)
    vocals_volume = Column(Float, default=0.0)         # giong goc NGOAI doan thoai AI
    voice_vocals_volume = Column(Float, default=0.0)   # giong goc TRONG doan thoai AI

    duration = Column(Float, default=0.0)
    error_message = Column(Text, nullable=True)
    created_at = Column(Float)

class WorkspaceFolder(Base):
    __tablename__ = "workspace_folders"
    id = Column(Integer, primary_key=True, index=True)
    path = Column(String, unique=True)
    name = Column(String)

class SystemSetting(Base):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True)
    value = Column(String)

class SubtitleSegment(Base):
    __tablename__ = "subtitle_segments"
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(String, ForeignKey("videos.id"))
    index = Column(Integer)
    start_time = Column(Float)
    end_time = Column(Float)
    source_text = Column(Text)
    target_text = Column(Text, nullable=True)
    speed_mode = Column(String, nullable=True) # auto_fit, normal, or null (inherit)
    bg_volume = Column(Float, nullable=True)   # null (inherit from global)
    audio_path = Column(String, nullable=True)
    is_trimmed = Column(Boolean, default=False)
    # True khi đoạn này dịch bằng DeepSeek bị lỗi và đã TỰ chuyển sang Google Translate.
    # Dùng để cảnh báo trên UI (giống is_trimmed) cho người dùng vào kiểm tra lại.
    translation_fallback = Column(Boolean, default=False)
    # True khi đoạn này do OCR VÁ vào chỗ Whisper không nghe được (chế độ Whisper+OCR).
    # Các đoạn này: KHÔNG sinh voice hàng loạt, hiện banner để người dùng xác nhận;
    # muốn có giọng thì sinh thủ công từng câu. Xem services/ocr_service.py + video_routes.
    ocr_filled = Column(Boolean, default=False)
    # Điểm kết thúc GỐC trước khi bước lồng tiếng tự NỚI khung vào khoảng lặng phía sau
    # (``_keo_dai_diem_ket_thuc`` ở video_routes). NULL = khung chưa bị nới. Nhờ nó mà: sinh
    # lại giọng ngắn hơn thì end co về theo giọng (không kẹt ở mốc đã nới lần trước), và nút
    # "Khôi phục khung gốc" trả đúng về như cũ.
    goc_end_time = Column(Float, nullable=True)
    # Người dùng bấm "Khôi phục khung gốc": câu này GIỮ khung gốc — bước lồng tiếng không
    # tự nới nữa, giọng sinh lại cho vừa khung gốc (xem ``restore_segment_timing``).
    giu_khung_goc = Column(Boolean, nullable=True)


# --------------------------------------------------------------------------- #
#  ĐĂNG VIDEO LÊN YOUTUBE
#
#  MÔ HÌNH: mỗi KHÁCH tự tạo một dự án Google Cloud của riêng họ rồi dán Client
#  ID/Secret vào app (``YoutubeConfig``); nhiều KÊNH của họ cùng trỏ về cấu hình
#  đó (``YoutubeChannel``). Hạn mức 100 lượt đăng/ngày là của DỰ ÁN, tức các
#  kênh dùng chung — không phải mỗi kênh 100 lượt.
#
#  VÌ SAO KHÔNG DÙNG MỘT CLIENT ID CHUNG CHO MỌI KHÁCH: hạn mức sẽ chia cho tất
#  cả (vài chục khách là nghẽn), và dự án chưa qua xác minh Google chỉ cho tối đa
#  100 lượt cấp quyền TRỌN ĐỜI. Mỗi khách một dự án thì không chạm trần nào, và
#  không ai phải xin Google xác minh.
# --------------------------------------------------------------------------- #
class YoutubeConfig(Base):
    __tablename__ = "youtube_configs"
    id = Column(String, primary_key=True, index=True)
    #  Tên gợi nhớ do người dùng đặt ("Google của tôi", "Dự án của khách A").
    label = Column(String, default="")
    client_id = Column(String)
    #  Qua services/secret_box.py (DPAPI). KHÔNG BAO GIỜ trả nguyên văn ra API.
    client_secret_enc = Column(Text)

    #  ĐẾM HẠN MỨC. Hai túi riêng biệt theo tài liệu Google:
    #    · upload_used : videos.insert — túi riêng, 100 lượt/ngày
    #    · unit_used   : mọi thứ khác — 10.000 đơn vị/ngày
    #      (thumbnails.set 50 · captions.insert 400 · playlistItems.insert 50)
    #  ``quota_date`` là NGÀY THEO GIỜ THÁI BÌNH DƯƠNG, vì Google reset lúc nửa
    #  đêm PT chứ không phải nửa đêm giờ Việt Nam — đếm theo giờ ta là sai lệch
    #  đúng 14-15 tiếng, và người dùng sẽ thấy "hết hạn mức" vào lúc vô lý.
    quota_date = Column(String, default="")
    upload_used = Column(Integer, default=0)
    unit_used = Column(Integer, default=0)
    created_at = Column(Float)


class YoutubeChannel(Base):
    __tablename__ = "youtube_channels"
    id = Column(String, primary_key=True, index=True)
    config_id = Column(String, ForeignKey("youtube_configs.id"))
    #  channel_id của YouTube (UC...) — dùng để chống thêm trùng một kênh.
    channel_id = Column(String, index=True)
    title = Column(String, default="")
    thumbnail_url = Column(String, default="")
    #  Refresh token (đã qua két). Access token KHÔNG lưu: nó sống 1 giờ, lấy
    #  mới khi cần rẻ hơn nhiều so với rủi ro giữ thêm một bí mật trên đĩa.
    refresh_token_enc = Column(Text)
    #  "ok" · "can_ket_noi_lai" (token bị thu hồi / giải mã không ra)
    status = Column(String, default="ok")
    last_error = Column(Text, nullable=True)
    created_at = Column(Float)
    #  Kênh đã cấp quyền ``youtube.force-ssl`` (tải phụ đề — captions.insert) chưa. Xin
    #  RIÊNG khi người dùng tick lúc kết nối: quyền này hiện trên màn cấp quyền là "quản
    #  lý video", đáng sợ hơn hẳn hai quyền mặc định, nên không ép mọi kênh.
    co_quyen_phu_de = Column(Integer, default=0)


class YoutubeFolder(Base):
    """THƯ MỤC THEO DÕI — quét xem có video mới thì tự xếp lịch đăng.

    Cách dùng thật: người dùng để một thư mục "đã dựng xong", cứ thỉnh thoảng bấm
    quét là những video MỚI được xếp vào lịch, nối tiếp sau bài hẹn muộn nhất.

    KHÔNG có sổ riêng ghi "tệp nào đã xử lý": bảng ``youtube_posts`` chính là sổ
    đó — tệp nào đã có bài (dù đang chờ, đã đăng hay lỗi) thì không còn là mới.
    Thêm sổ thứ hai là sớm muộn hai sổ lệch nhau, rồi một video bị đăng hai lần.
    """
    __tablename__ = "youtube_folders"
    id = Column(String, primary_key=True, index=True)
    path = Column(String)
    de_quy = Column(Boolean, default=False)     # quét cả thư mục con
    channel_row_id = Column(String, ForeignKey("youtube_channels.id"))

    #  Khuôn mặc định áp cho mọi video quét được.
    category_id = Column(String, default="22")
    privacy = Column(String, default="private")
    default_language = Column(String, default="vi")
    mo_ta_mac_dinh = Column(Text, default="")
    the_mac_dinh = Column(Text, default="")
    #  Nhờ AI đặt tiêu đề/mô tả cho từng video mới. Tắt thì lấy TÊN TỆP làm tiêu
    #  đề — xấu nhưng không tốn gì và không chờ.
    dung_ai = Column(Boolean, default=False)

    #  Lịch: bài đầu tiên vào khung giờ này, các bài sau cách nhau ngần này tiếng.
    gio_dau = Column(String, default="08:00")
    cach_nhau_gio = Column(Float, default=6.0)
    last_scan_at = Column(Float, nullable=True)
    created_at = Column(Float)


class YoutubePost(Base):
    __tablename__ = "youtube_posts"
    id = Column(String, primary_key=True, index=True)
    channel_row_id = Column(String, ForeignKey("youtube_channels.id"))
    #  Dự án trong app (nếu đăng từ dự án) — để mở lại được từ bảng lịch.
    video_id = Column(String, nullable=True)
    file_path = Column(String)

    title = Column(String, default="")
    description = Column(Text, default="")
    #  Danh sách thẻ, ngăn nhau bằng dấu phẩy. Trần của YouTube là 500 KÝ TỰ cho
    #  TOÀN BỘ danh sách, kể cả dấu phẩy ngăn cách.
    tags = Column(Text, default="")
    category_id = Column(String, default="22")       # 22 = People & Blogs
    #  Ngôn ngữ để YouTube gợi ý đúng nhóm người xem.
    default_language = Column(String, default="vi")
    #  NGÔN NGỮ ÂM THANH tách riêng khỏi ngôn ngữ tiêu đề/mô tả. Video vietsub giữ
    #  tiếng gốc: tiêu đề tiếng Việt nhưng lời thoại vẫn tiếng Trung — khai gộp
    #  một ngôn ngữ là YouTube hiểu sai, phụ đề tự động và lồng tiếng tự động của
    #  họ chạy sai ngôn ngữ. Rỗng = dùng luôn ``default_language``.
    audio_language = Column(String, default="")
    #  BÁO CHO NGƯỜI ĐĂNG KÝ. YouTube mặc định là CÓ, mà app này đăng theo lô —
    #  đẩy 90 video là 90 lần làm phiền người đăng ký trong một buổi. Nên mặc định
    #  ở đây là KHÔNG, ngược với mặc định của Google.
    notify_subscribers = Column(Integer, default=0)
    #  TỰ KHAI "dành cho trẻ em". YouTube BẮT phải khai, và khai nhầm là thành
    #  video trẻ em: tắt bình luận, mất đề xuất, mất quảng cáo cá nhân hoá.
    made_for_kids = Column(Integer, default=0)
    #  "ngan" (Shorts) · "dai" · "" — ghi lại lúc soạn, vì hàng đợi không còn giữ
    #  thời lượng video và tệp nguồn có thể đã bị xoá khi người dùng xem lại.
    dang = Column(String, default="")
    #  KHAI BÁO NỘI DUNG DO AI TẠO/CHỈNH (``status.containsSyntheticMedia``).
    #  App này lồng tiếng bằng AI, mà YouTube BẮT khai khi nội dung nghe/nhìn như
    #  thật nhưng đã bị tạo hoặc sửa bằng máy — giọng người nói là đúng trường hợp
    #  đó. Không khai thì YouTube có thể tự gắn nhãn hoặc gỡ video.
    synthetic_media = Column(Integer, default=0)
    privacy = Column(String, default="private")      # private · unlisted · public
    #  Mốc NỀN TẢNG tự mở công khai (ISO 8601, UTC). Có giá trị thì privacy phải
    #  là "private" — đó là điều kiện của API. Để trống = giữ nguyên privacy.
    publish_at = Column(String, nullable=True)
    #  Ảnh bìa app tự dựng cho MỌI bài, dùng để nhận ra bài nào là bài nào trong
    #  hàng đợi. Nó KHÔNG đồng nghĩa với việc gửi ảnh lên YouTube.
    thumbnail_path = Column(String, nullable=True)
    #  CÓ GỬI ẢNH LÊN YOUTUBE KHÔNG. Tách riêng khỏi ``thumbnail_path`` vì hai việc
    #  khác hẳn nhau: xem trong app thì miễn phí, còn ``thumbnails.set`` ngốn 50 đơn
    #  vị mỗi lượt trong túi 10.000/ngày. Người dùng không tự chọn ảnh thì giữ ảnh
    #  để xem, nhưng không tiêu hạn mức.
    dung_thumbnail = Column(Integer, default=0)
    #  TẢI KÈM PHỤ ĐỀ (captions.insert, 400 đơn vị/thứ tiếng): thứ tiếng chính + mọi thứ
    #  tiếng thêm đã dịch ĐỦ của dự án nguồn. ``du_an_nguon`` = "chi_tiet:<id>" /
    #  "long_tieng:<id>" — ``video_id`` ở trên chỉ có với video lồng tiếng.
    tai_phu_de = Column(Integer, default=0)
    du_an_nguon = Column(String, nullable=True)

    #  cho_doi · dang_day · da_len_lich · da_dang · loi · huy
    status = Column(String, default="cho_doi")
    attempts = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    #  CẢNH BÁO ≠ LỖI. Video đã lên YouTube nhưng ảnh bìa chưa đặt được là chuyện
    #  phải nói, mà nhét vào ``last_error`` thì bài hiện thành "Lỗi" trong khi nó
    #  đã đăng thành công — người dùng đi đăng lại, thành hai video trùng nhau.
    canh_bao = Column(Text, nullable=True)
    youtube_video_id = Column(String, nullable=True)
    #  URL tiếp tục của lượt đẩy dở (upload nhiều mảnh) — nhờ nó mà rớt mạng ở
    #  90% không phải đẩy lại từ đầu.
    resume_url = Column(Text, nullable=True)
    bytes_sent = Column(Float, default=0)
    created_at = Column(Float)
    updated_at = Column(Float)

    #  ------------------------------------------------------------------ #
    #  TIN TỨC HỎI LẠI TỪ YOUTUBE (videos.list — 1 đơn vị cho 50 video).
    #
    #  Đẩy lên xong app mới chỉ biết "đã gửi". Video còn phải qua khâu xử lý của
    #  YouTube (có thể hỏng, có thể bị từ chối vì bản quyền), rồi tới giờ hẹn mới
    #  công khai. Lưu lại đây để bảng hiện đúng chuyện đang xảy ra mà không phải
    #  hỏi Google mỗi lần vẽ lại màn hình.
    #
    #  Ba con số để trống (NULL) nghĩa là CHƯA BIẾT, khác hẳn 0: kênh tắt hiển
    #  thị lượt thích thì YouTube bỏ hẳn trường đó, hiện 0 là nói sai.
    #  ------------------------------------------------------------------ #
    yt_privacy = Column(String, nullable=True)        # private · unlisted · public
    yt_upload_status = Column(String, nullable=True)  # uploaded · processed · failed · rejected
    yt_rejection = Column(String, nullable=True)      # lý do bị từ chối, nếu có
    yt_processing = Column(String, nullable=True)     # processing · succeeded · failed
    yt_published_at = Column(String, nullable=True)   # mốc THẬT đã công khai
    yt_views = Column(Integer, nullable=True)
    yt_likes = Column(Integer, nullable=True)
    yt_comments = Column(Integer, nullable=True)
    #  Lần cuối hỏi lại Google (epoch giây) — để hiện "số liệu lúc 20:15".
    yt_checked_at = Column(Float, nullable=True)
    #  True khi Google KHÔNG còn trả về video này: đã bị xoá trên YouTube.
    yt_da_xoa = Column(Boolean, default=False)
    #  YouTube tự xếp video là "dành cho trẻ em" (dù app luôn khai là KHÔNG). Bị
    #  xếp vậy thì bình luận bị tắt và video không vào đề xuất — phải nói ra.
    yt_tre_em = Column(Boolean, default=False)
    #  CHẶN THEO KHU VỰC — chỗ YouTube Studio hiện chữ "Blocked".
    #
    #  Video dính khiếu nại bản quyền KHÔNG bị "từ chối": nó vẫn "processed", vẫn
    #  giữ giờ hẹn, nên nhìn ``uploadStatus`` thì thấy bình thường. Chỉ có
    #  ``contentDetails.regionRestriction`` nói ra. Không đọc chỗ này thì app báo
    #  "Đã lên lịch" cho một video sẽ không ai xem được.
    yt_chan_het = Column(Boolean, default=False)      # chặn ở MỌI nước
    yt_so_nuoc_chan = Column(Integer, default=0)      # số nước bị chặn
    yt_nuoc_chan = Column(String, nullable=True)      # vài mã nước, ngăn bằng phẩy
    #  Video đã bị một ĐỐI TÁC NỘI DUNG của YouTube nhận bản quyền. Đây là thứ gần
    #  nhất với "khiếu nại bản quyền" mà Data API cho biết — chi tiết từng khiếu
    #  nại nằm ở Content ID API, chỉ mở cho đối tác.
    yt_co_ban_quyen = Column(Boolean, default=False)
    #  Vì sao YouTube sẽ xử lý hỏng: archiveFile · audioFile · imageFile ·
    #  notAVideoFile · projectFile · docFile. Ngăn bằng phẩy.
    yt_loi_xu_ly = Column(String, nullable=True)
