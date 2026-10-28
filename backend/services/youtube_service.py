# -*- coding: utf-8 -*-
"""ĐĂNG VIDEO LÊN YOUTUBE — OAuth + upload nhiều mảnh, viết thẳng bằng HTTP.

VÌ SAO KHÔNG DÙNG google-api-python-client: ba thư viện (google-auth,
google-auth-oauthlib, google-api-python-client) kéo theo hàng chục phụ thuộc và
cần hook riêng cho PyInstaller, trong khi thứ ta cần chỉ là bốn lời gọi HTTP có
tài liệu rõ ràng. Bản cài đã 427 MB.

LUỒNG OAUTH: kiểu "ứng dụng máy tính" — mở trình duyệt hệ thống, Google trả mã
về ``http://127.0.0.1:<cổng ngẫu nhiên>``. Không dùng cổng cố định: máy người
dùng có thể đang chiếm cổng đó, và cổng cố định thì hai cửa sổ app cùng mở là
đánh nhau.

Client secret của ứng dụng máy tính KHÔNG phải bí mật thật (Google nói rõ điều
đó — nó nằm trong mọi bản cài). Ta vẫn cất nó qua két vì nó nằm cạnh refresh
token, và refresh token thì là bí mật thật.
"""
from utils.i18n import L
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from services.secret_box import che, giai_ma, ma_hoa

logger = logging.getLogger("YouTube")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

#  Xin ĐÚNG hai quyền cần dùng:
#    · youtube.upload  — đăng video (bắt buộc)
#    · youtube.readonly — đọc tên/ảnh kênh để hiện trong danh sách
#  KHÔNG xin ``youtube`` (toàn quyền): xin rộng thì màn hình cấp quyền của Google
#  hiện danh sách đáng sợ hơn, mà ta cũng không dùng tới.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
#: TẢI PHỤ ĐỀ (captions.insert) cần quyền này — Google không có quyền hẹp hơn cho việc
#: đó. Chỉ xin khi người dùng tick "Cho phép tải phụ đề" lúc kết nối kênh.
SCOPE_PHU_DE = "https://www.googleapis.com/auth/youtube.force-ssl"

#  Giá mỗi lời gọi, theo bảng hạn mức của Google (đã tra 2026-09-22).
#  ``videos.insert`` nằm ở TÚI RIÊNG 100 lượt/ngày; các hàm khác trừ vào túi
#  chung 10.000 đơn vị/ngày.
GIA_UPLOAD = 1          # túi riêng
GIA_THUMBNAIL = 50      # thumbnails.set
GIA_CAPTION = 400       # captions.insert
GIA_PLAYLIST = 50       # playlistItems.insert
#: videos.list — 1 đơn vị cho TỚI 50 video một lượt, nên soát trạng thái cả trăm
#: bài vẫn rẻ hơn một lần đặt ảnh đại diện.
GIA_DOC_VIDEO = 1
#: channels.list — gọi mỗi lần kết nối kênh và mỗi lần soát lại danh sách kênh.
GIA_DOC_KENH = 1
TRAN_UPLOAD_NGAY = 100
TRAN_DON_VI_NGAY = 10000
#: Ngưỡng bắt đầu nhắc người dùng về túi 10.000. Đặt ở 80% chứ không đợi cạn:
#: cạn rồi mới báo thì lượt đang chạy đã hỏng, mà thứ hỏng lại là ảnh bìa của
#: video VỪA ĐĂNG XONG — sửa lại phải vào Studio làm tay.
NGUONG_NHAC_DON_VI = int(TRAN_DON_VI_NGAY * 0.8)

#  Đẩy theo mảnh 8 MB. Nhỏ quá thì nhiều lượt HTTP; to quá thì rớt mạng mất
#  nhiều công. Google yêu cầu mảnh là bội số của 256 KB (trừ mảnh cuối).
KICH_THUOC_MANH = 8 * 1024 * 1024


class YoutubeError(RuntimeError):
    """Lỗi có thể đọc được, để hiện thẳng cho người dùng.

    ``ly_do`` là mã lý do MÁY ĐỌC ĐƯỢC của Google ("quotaExceeded",
    "uploadLimitExceeded"…). Câu tiếng Việt là để người dùng đọc; nhận ra "hết
    hạn mức" bằng cách dò chữ trong câu tiếng Việt là kiểu chắp vá, hỏng ngay
    lần đầu có người sửa lại câu chữ cho hay hơn.
    """

    def __init__(self, thong_diep: str, ly_do: str = ""):
        super().__init__(thong_diep)
        self.ly_do = ly_do


class ClientSaiError(YoutubeError):
    """Sai KHOÁ CỦA DỰ ÁN (Client ID/Secret), không phải sai phiên của kênh.

    Tách riêng vì hai thứ này dẫn tới hai việc phải làm khác hẳn nhau: sai khoá
    thì đi sửa dự án, sai phiên thì đăng nhập lại kênh. Gộp làm một là người dùng
    đăng nhập lại chục lần mà không đỡ."""


def _goi(ham, *a, **kw):
    """Bọc mọi lời gọi mạng: lỗi của ``requests`` thành YoutubeError tiếng Việt.

    VÌ SAO CẦN: ``requests`` ném ConnectionError/Timeout THÔ khi rớt mạng. Hàng
    đợi chỉ bắt YoutubeError, nên lỗi thô lọt lên thành 500 "Internal Server
    Error" — người dùng không biết là mất mạng, và bài không được đánh dấu để
    đẩy tiếp. Đo được trong phép thử: đứt giữa chừng ném ConnectionError.
    """
    try:
        return ham(*a, **kw)
    except requests.exceptions.Timeout:
        raise YoutubeError(L("Google không phản hồi kịp — mạng chậm hoặc tệp quá lớn. Sẽ thử lại.", "Google did not respond in time — slow network or file too large. Will retry."))
    except requests.exceptions.ConnectionError:
        raise YoutubeError(L("Mất kết nối tới Google giữa chừng — phần đã đẩy vẫn giữ, sẽ đẩy tiếp.", "Lost the connection to Google midway — the part already pushed is kept and will be continued."))
    except requests.exceptions.RequestException as e:
        raise YoutubeError(L(f"Lỗi mạng khi gọi Google: {type(e).__name__}", f"Network error calling Google: {type(e).__name__}"))


# --------------------------------------------------------------------------- #
#  HẠN MỨC — đếm theo NGÀY GIỜ THÁI BÌNH DƯƠNG
# --------------------------------------------------------------------------- #
def ngay_pacific() -> str:
    """Ngày hiện tại theo giờ Thái Bình Dương, dạng YYYY-MM-DD.

    Google reset hạn mức lúc nửa đêm PT. Đếm theo ngày Việt Nam thì lệch 14-15
    tiếng: sáng ra người dùng thấy "đã dùng 100/100" trong khi Google đã reset từ
    lúc 2 giờ chiều hôm trước — hoặc ngược lại, tưởng còn hạn mức rồi ăn lỗi.

    KHÔNG dùng thư viện múi giờ: PT là UTC-8, mùa hè UTC-7. Lấy UTC-8 làm mốc thì
    sai tối đa 1 giờ trong mùa hè, và sai về phía AN TOÀN (reset muộn hơn thực tế
    một chút, tức ta dè dặt hơn Google).
    """
    return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


def dam_bao_ngay(cfg) -> None:
    """Sang ngày mới (theo PT) thì xoá bộ đếm. Gọi trước mọi phép cộng/kiểm."""
    if cfg is None:
        #  Cấu hình bị xoá trong lúc bài còn trong hàng đợi. Ném AttributeError ở
        #  đây thì người dùng nhận một cú 500 trống trơn thay vì câu nói rõ kênh
        #  đã mất — câu đó do phía gọi lo, việc ở đây chỉ là đừng nổ.
        return
    hom_nay = ngay_pacific()
    if cfg.quota_date != hom_nay:
        cfg.quota_date = hom_nay
        cfg.upload_used = 0
        cfg.unit_used = 0


def tru_don_vi(cfg, gia: int) -> None:
    """Trừ ``gia`` đơn vị vào túi chung 10.000/ngày.

    MỌI lời gọi API ngoài ``videos.insert`` đều phải đi qua đây. Quên một chỗ thì
    bộ đếm thấp hơn thực tế, người dùng tin vào con số đó rồi đâm thẳng vào tường
    của Google — và cái hỏng thường là ảnh bìa của video VỪA đăng xong.
    """
    if cfg is None:
        return
    dam_bao_ngay(cfg)
    cfg.unit_used = int(cfg.unit_used or 0) + max(0, int(gia))


def du_don_vi(cfg, gia: int) -> bool:
    """Còn đủ chỗ trong túi chung cho một lượt gọi giá ``gia`` không?"""
    if cfg is None:
        return True
    dam_bao_ngay(cfg)
    return int(cfg.unit_used or 0) + max(0, int(gia)) <= TRAN_DON_VI_NGAY


#: Những mã lý do Google trả về khi hạn mức đã cạn — dùng để KÉO BỘ ĐẾM CỤC BỘ
#: về sát sự thật.
LY_DO_HET_HAN_MUC = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
LY_DO_HET_LUOT_DANG = {"uploadLimitExceeded"}


def dong_bo_theo_loi(cfg, ly_do: str) -> bool:
    """Google vừa nói "hết hạn mức" — kéo bộ đếm cục bộ lên kịch trần.

    VÌ SAO CẦN: bộ đếm ở đây chỉ biết những gì CHÍNH APP NÀY làm. Cùng một dự án
    Google mà dùng ở máy thứ hai, hoặc ở một công cụ khác, hoặc trước đây từng có
    lượt gọi hỏng mà app không trừ — là con số của app thấp hơn thật. Người dùng
    tin vào con số đó rồi đâm thẳng vào tường của Google.

    Google KHÔNG có cửa nào trả về "đã dùng bao nhiêu" (``QuotaInfo`` của Cloud
    Quotas API chỉ có trần, không có mức dùng; số liệu dùng thật nằm ở Cloud
    Monitoring, cần thêm quyền và thêm bước bật API). Nhưng chính câu TỪ CHỐI của
    họ là một tin thật và miễn phí: đã bị từ chối vì hết hạn mức thì app biết
    chắc túi đó đã cạn, không cần đoán nữa.

    Trả về True nếu có chỉnh gì.
    """
    if cfg is None or not ly_do:
        return False
    dam_bao_ngay(cfg)
    if ly_do in LY_DO_HET_LUOT_DANG:
        cfg.upload_used = TRAN_UPLOAD_NGAY
        return True
    if ly_do in LY_DO_HET_HAN_MUC:
        cfg.unit_used = TRAN_DON_VI_NGAY
        #  ``quotaExceeded`` ở ngay lượt ``videos.insert`` nghĩa là túi LƯỢT ĐĂNG
        #  cạn chứ không phải túi đơn vị — nhưng ta không phân biệt được từ đây,
        #  nên đánh dấu cả hai. Báo thừa "hết hạn mức" chỉ làm người dùng chờ tới
        #  nửa đêm Thái Bình Dương; báo thiếu thì họ bấm đăng và ăn lỗi lặp lại.
        cfg.upload_used = TRAN_UPLOAD_NGAY
        return True
    return False


def con_lai(cfg) -> dict:
    dam_bao_ngay(cfg)
    return {
        "ngay": cfg.quota_date,
        "upload_da_dung": int(cfg.upload_used or 0),
        "upload_con": max(0, TRAN_UPLOAD_NGAY - int(cfg.upload_used or 0)),
        "don_vi_da_dung": int(cfg.unit_used or 0),
        "don_vi_con": max(0, TRAN_DON_VI_NGAY - int(cfg.unit_used or 0)),
        "tran_upload": TRAN_UPLOAD_NGAY,
        "tran_don_vi": TRAN_DON_VI_NGAY,
        #  Giá một lượt đặt ảnh bìa — giao diện cần nó để nói "còn đủ cho N ảnh".
        "gia_thumbnail": GIA_THUMBNAIL,
        "sap_het_don_vi": int(cfg.unit_used or 0) >= NGUONG_NHAC_DON_VI,
    }


# --------------------------------------------------------------------------- #
#  OAUTH
# --------------------------------------------------------------------------- #
#  Trang hiện trong trình duyệt sau khi người dùng bấm "Cho phép".
#
#  VÌ SAO NÓ PHẢI CHỜ: bản trước ghi "Đã kết nối xong ✓" NGAY khi nhận được mã của
#  Google — tức là trước cả lúc đổi mã lấy token và trước lúc hỏi xem tài khoản đó
#  có kênh YouTube nào không. Kết quả: trang báo xong, người dùng quay lại app thì
#  thấy chữ đỏ "Tài khoản Google này chưa có kênh YouTube nào". Hai màn hình nói
#  hai chuyện khác nhau về cùng một việc.
#
#  Nay trang hiện "Đang hoàn tất…" rồi TỰ HỎI LẠI máy chủ tí hon này cho tới khi
#  biết kết quả thật, và hiện đúng câu lỗi nếu hỏng.
_TRANG = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<title>SubForge</title></head>
<body style="font-family:system-ui;text-align:center;padding:60px;
background:#0f1115;color:#e8ecf5">
<h2 id="t">Đang hoàn tất…</h2>
<p id="p" style="max-width:520px;margin:12px auto;line-height:1.6">
Bạn giữ thẻ này thêm vài giây.</p>
<script>
(function hoi(){
  fetch('/tt').then(r=>r.json()).then(d=>{
    if(!d.xong){ setTimeout(hoi, 600); return; }
    document.getElementById('t').textContent = d.ok
      ? 'Đã kết nối xong \u2713' : 'Kết nối không thành công';
    document.getElementById('t').style.color = d.ok ? '#7ee787' : '#ff7b72';
    document.getElementById('p').textContent = d.ok
      ? 'Bạn đóng thẻ này và quay lại SubForge.'
      : (d.loi || 'Hãy thử lại trong SubForge.');
  }).catch(()=>setTimeout(hoi, 900));
})();
</script></body></html>"""


def _trang_html() -> str:
    """Trang kết nối xong theo ngôn ngữ giao diện (tiếng Việt: giữ nguyên ``_TRANG``)."""
    if L("vi", "en") != "en":
        return _TRANG
    out = _TRANG.replace('lang="vi"', 'lang="en"')
    for vi, en in (
        ("Đang hoàn tất…", "Finishing up…"),
        ("Bạn giữ thẻ này thêm vài giây.", "Keep this tab open for a few more seconds."),
        ("Đã kết nối xong", "Connected"),
        ("Kết nối không thành công", "Connection failed"),
        ("Bạn đóng thẻ này và quay lại SubForge.", "You can close this tab and return to SubForge."),
        ("Hãy thử lại trong SubForge.", "Please try again in SubForge."),
    ):
        out = out.replace(vi, en)
    return out


class _BatMa(BaseHTTPRequestHandler):
    """Máy chủ tí hon hứng mã Google trả về, và báo lại kết quả THẬT cho trang web."""

    ket_qua: dict = {}
    #  ``xong`` chỉ bật khi luồng chính đã đổi token XONG và đã kiểm xong tài khoản.
    ket_thuc: dict = {"xong": False, "ok": False, "loi": ""}

    def do_GET(self):  # noqa: N802 (tên do thư viện chuẩn quy định)
        duong = urllib.parse.urlparse(self.path)
        if duong.path == "/tt":
            b = json.dumps(_BatMa.ket_thuc).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            #  Không cho bộ nhớ đệm giữ: trang hỏi lại liên tục, giữ bản cũ là nó
            #  đứng mãi ở "đang hoàn tất".
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
            return

        q = urllib.parse.parse_qs(duong.query)
        if q:
            _BatMa.ket_qua = {k: v[0] for k, v in q.items()}
        b = _trang_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *_args):
        #  Câm: mặc định nó in mọi request ra stderr, lẫn vào nhật ký app.
        return


def bat_dau_dang_nhap(client_id: str, client_secret: str, cho_toi_da: int = 300,
                      ghi_url=None, huy=None, kiem_tra=None, xin_phu_de: bool = False) -> dict:
    """Mở trình duyệt cho người dùng đăng nhập, trả về bộ token.

    Chặn tối đa ``cho_toi_da`` giây. Người dùng bỏ ngang (đóng trình duyệt) thì
    ném lỗi có câu tiếng Việt chứ không treo mãi.

    ``ghi_url``: hàm nhận đường dẫn đăng nhập ngay khi dựng xong. VÌ SAO CẦN:
    ``webbrowser.open`` mở TRÌNH DUYỆT MẶC ĐỊNH của Windows (thường là Edge), mà
    tài khoản Google của người dùng có khi lại đang đăng nhập ở Chrome. Lúc đó họ
    phải đăng nhập lại từ đầu trong Edge, hoặc tệ hơn là cấp quyền nhầm tài khoản.
    Giao diện lấy đường dẫn này ra cho họ chép sang đúng trình duyệt họ muốn.

    ``huy``: hàm trả True khi người dùng bấm Huỷ. Không có nó thì bấm nhầm là phải
    ngồi chờ hết 5 phút, giao diện khoá cứng chẳng làm được gì.

    ``kiem_tra(access_token)``: chạy NGAY SAU khi đổi được token, trước khi hàm này
    trả về. Ném lỗi thì cả lượt coi như hỏng, và trang web đang mở sẽ hiện đúng câu
    lỗi đó. Đây là chỗ hỏi "tài khoản này có kênh YouTube nào không" — hỏi muộn hơn
    thì trang web đã kịp báo thành công.
    """
    if not client_id or not client_secret:
        raise YoutubeError(L("Chưa có Client ID / Client Secret của Google.", "Google Client ID / Client Secret not set yet."))

    #  Cổng 0 = hệ điều hành tự chọn cổng trống.
    server = HTTPServer(("127.0.0.1", 0), _BatMa)
    cong = server.server_address[1]
    redirect = f"http://127.0.0.1:{cong}"
    _BatMa.ket_qua = {}

    state = secrets.token_urlsafe(24)
    tham_so = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES + ([SCOPE_PHU_DE] if xin_phu_de else [])),
        #  offline + consent: BẮT BUỘC để Google trả REFRESH TOKEN. Thiếu
        #  ``prompt=consent`` thì lần cấp quyền thứ hai trở đi Google chỉ trả
        #  access token 1 giờ — app sẽ chạy ngon hôm nay và chết vào ngày mai.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        #  Cho người dùng chọn kênh khác khi thêm kênh thứ hai.
        "include_granted_scopes": "true",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(tham_so)

    _BatMa.ket_thuc = {"xong": False, "ok": False, "loi": ""}
    #  PHỤC VỤ NHIỀU LƯỢT, không chỉ một: sau lượt mang mã về, trang còn hỏi lại
    #  ``/tt`` vài lần để biết kết quả thật.
    luong = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                             daemon=True)
    luong.start()
    if ghi_url:
        #  Báo URL TRƯỚC khi mở trình duyệt: mở xong mới báo thì giao diện hiện
        #  link chậm hơn cửa sổ trình duyệt, người dùng đã kịp bấm nhầm.
        try:
            ghi_url(url)
        except Exception:  # noqa: BLE001 — báo URL hỏng không được chặn đăng nhập
            pass
    webbrowser.open(url)

    def _dong():
        #  Đóng cổng loopback: cổng được trả lại ngay cho lượt bấm kế tiếp (không
        #  thì mỗi lần huỷ là bỏ phí một cổng).
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        server.server_close()

    def _bao(ok: bool, loi: str = ""):
        """Cho trang web biết kết quả, rồi chừa một nhịp để nó kịp hỏi."""
        _BatMa.ket_thuc = {"xong": True, "ok": ok, "loi": loi}
        time.sleep(1.2)
        _dong()

    het = time.time() + cho_toi_da
    da_huy = False
    while time.time() < het:
        if huy and huy():
            da_huy = True
            break
        if _BatMa.ket_qua:
            break
        time.sleep(0.2)

    if da_huy:
        _bao(False, L("Bạn đã huỷ kết nối trong SubForge.", "You cancelled the connection in SubForge."))
        raise YoutubeError(L("Đã huỷ kết nối theo yêu cầu.", "Cancelled the connection at your request."))

    kq = _BatMa.ket_qua
    if not kq:
        _dong()
        raise YoutubeError(
            L("Hết thời gian chờ đăng nhập Google. Hãy bấm kết nối lại và hoàn tất "
            "trên trình duyệt.", "Google sign-in timed out. Click connect again and finish in the browser.")
        )
    if kq.get("state") != state:
        #  Chống bị trang khác trên máy bắn mã vào cổng loopback của ta.
        _bao(False, L("Phản hồi đăng nhập không khớp phiên — hãy thử lại trong SubForge.", "The sign-in response does not match the session — please try again in SubForge."))
        raise YoutubeError(L("Phản hồi đăng nhập không khớp phiên — hãy thử lại.", "The sign-in response does not match the session — please try again."))
    if "error" in kq:
        _bao(False, L(f"Google từ chối cấp quyền: {kq['error']}", f"Google denied the permission: {kq['error']}"))
        raise YoutubeError(L(f"Google từ chối cấp quyền: {kq['error']}", f"Google denied the permission: {kq['error']}"))

    try:
        token = doi_ma_lay_token(client_id, client_secret, kq["code"], redirect)
        #  KIỂM TRA TRƯỚC KHI BÁO XONG. Đây là chỗ hỏi tài khoản có kênh YouTube
        #  không — hỏi sau khi đã báo xong thì trang web nói một đằng, app nói một nẻo.
        if kiem_tra:
            kiem_tra(token.get("access_token", ""))
    except YoutubeError as e:
        _bao(False, str(e))
        raise

    _bao(True)
    return token


def doi_ma_lay_token(client_id: str, client_secret: str, code: str, redirect: str) -> dict:
    r = _goi(
        requests.post,
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
        timeout=30,
    )
    if r.status_code != 200:
        if "invalid_client" in (r.text or ""):
            #  Người dùng vừa đăng nhập xong, chọn kênh xong, bấm qua màn cảnh báo
            #  — rồi nhận một khối JSON tiếng Anh. Nói thẳng sai ở đâu và sửa ở đâu.
            raise ClientSaiError(
                L("Client Secret của dự án Google không đúng nên Google từ chối ở "
                "bước cuối. Bấm “Sửa” ở dự án này, dán lại Client secret lấy từ mục "
                "Clients trong Google Auth Platform, rồi kết nối lại.", "The Google project's Client Secret is wrong so Google rejected it at the last step. Click \"Edit\" on this project, paste the Client secret again from the Clients section of Google Auth Platform, then reconnect.")
            )
        raise YoutubeError(_loi_google(r, L("Đổi mã đăng nhập thất bại", "Failed to exchange the sign-in code")))
    d = r.json()
    if not d.get("refresh_token"):
        #  Xảy ra khi thiếu prompt=consent, hoặc người dùng đã cấp quyền trước đó
        #  cho chính Client ID này. Nói rõ cách sửa thay vì để họ đoán.
        raise YoutubeError(
            L("Google không trả refresh token. Vào https://myaccount.google.com/permissions "
            "gỡ quyền của ứng dụng rồi kết nối lại.", "Google did not return a refresh token. Go to https://myaccount.google.com/permissions, remove the app's access, then reconnect.")
        )
    return d


def lay_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Đổi refresh token lấy access token mới (sống 1 giờ)."""
    r = _goi(
        requests.post,
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if r.status_code != 200:
        loi = _loi_google(r, L("Làm mới phiên đăng nhập thất bại", "Failed to refresh the sign-in session"))
        if "invalid_grant" in r.text:
            raise YoutubeError(
                L("Phiên đăng nhập đã hết hiệu lực (thu hồi quyền, đổi mật khẩu, "
                "hoặc dự án Google còn ở trạng thái Testing nên token chỉ sống 7 "
                "ngày). Hãy kết nối lại kênh này.", "The sign-in session has expired (access revoked, password changed, or the Google project is still in Testing status so the token only lives 7 days). Please reconnect this channel.")
            )
        if "invalid_client" in r.text:
            #  KHOÁ CỦA DỰ ÁN sai, KHÔNG phải phiên của kênh hỏng. Phân biệt cho
            #  bằng được: gộp chung thì mọi kênh của dự án đó đồng loạt hiện "cần
            #  kết nối lại", người dùng đi đăng nhập lại từng kênh — mà đăng nhập
            #  lại cũng hỏng, vì sai nằm ở Client ID/Secret.
            raise ClientSaiError(
                L("Client ID hoặc Client Secret của dự án Google không đúng. Bấm "
                "Sửa ở dự án này và dán lại khoá — kết nối lại kênh sẽ không giúp gì.", "The Google project's Client ID or Client Secret is wrong. Click Edit on this project and paste the keys again — reconnecting the channel will not help.")
            )
        raise YoutubeError(loi)
    return r.json()["access_token"]


def kiem_bi_mat(client_id: str, client_secret: str) -> None:
    """Hỏi Google xem cặp Client ID/Secret có thật không. Sai thì ném lỗi.

    MẸO: gửi một refresh token CHẮC CHẮN SAI. Google trả về:
      · ``invalid_client`` -> cặp khoá sai (đây là thứ ta cần bắt),
      · ``invalid_grant``  -> khoá ĐÚNG, chỉ token là bậy — tức là đạt.
    Không có cách nào rẻ hơn để kiểm khoá mà không bắt người dùng đi hết vòng
    OAuth rồi mới biết mình gõ nhầm một ký tự.

    MẤT MẠNG THÌ BỎ QUA (không ném): chặn lưu khi offline là vô cớ — người dùng
    có thể đang dán khoá trước rồi mới nối mạng.
    """
    try:
        r = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": "1//khong-phai-token-that",
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
    except requests.exceptions.RequestException:
        return
    if r.status_code == 200:
        return
    if "invalid_client" in (r.text or ""):
        raise ClientSaiError(
            L("Google từ chối cặp Client ID / Client Secret này. Hãy chép lại cả hai "
            "từ mục Clients trong Google Auth Platform — chú ý không lẫn khoảng "
            "trắng ở đầu hoặc cuối.", "Google rejected this Client ID / Client Secret pair. Copy both again from the Clients section of Google Auth Platform — make sure there is no whitespace at the start or end.")
        )


# --------------------------------------------------------------------------- #
#  KÊNH
# --------------------------------------------------------------------------- #
def lay_kenh(access_token: str) -> dict:
    """Thông tin kênh mà token này có quyền — dùng để hiện tên/ảnh trong app."""
    r = _goi(
        requests.get,
        f"{API}/channels",
        params={"part": "snippet,contentDetails", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if r.status_code != 200:
        raise YoutubeError(_loi_google(r, L("Không đọc được thông tin kênh", "Could not read the channel info")))
    items = r.json().get("items") or []
    if not items:
        raise YoutubeError(
            L("Tài khoản Google này chưa có kênh YouTube nào. Hãy tạo kênh trước "
            "rồi kết nối lại.", "This Google account has no YouTube channel yet. Create a channel first, then reconnect.")
        )
    it = items[0]
    sn = it.get("snippet") or {}
    anh = ((sn.get("thumbnails") or {}).get("default") or {}).get("url", "")
    return {"channel_id": it.get("id", ""), "title": sn.get("title", ""), "thumbnail": anh}


# --------------------------------------------------------------------------- #
#  ĐĂNG VIDEO
# --------------------------------------------------------------------------- #
def kiem_metadata(title: str, description: str, tags: str) -> list:
    """Soát trước theo ĐÚNG trần của YouTube. Trả về danh sách lỗi (rỗng = ổn).

    CHÚ Ý CÁI BẪY: trần mô tả là 5000 **BYTE**, không phải 5000 ký tự. Tiếng Việt
    có dấu tốn 2-3 byte mỗi ký tự, nên ~2000 ký tự tiếng Việt đã có thể chạm
    trần. Đếm ký tự như thường lệ là sẽ có ngày API trả lỗi khó hiểu.
    """
    loi = []
    t = (title or "").strip()
    if not t:
        loi.append(L("Chưa có tiêu đề.", "No title yet."))
    if len(t) > 100:
        loi.append(L(f"Tiêu đề dài {len(t)}/100 ký tự.", f"The title is {len(t)}/100 characters long."))
    if "<" in t or ">" in t:
        loi.append(L("Tiêu đề không được chứa ký tự < hoặc >.", "The title must not contain the characters < or >."))
    d = description or ""
    so_byte = len(d.encode("utf-8"))
    if so_byte > 5000:
        loi.append(L(f"Mô tả dài {so_byte}/5000 byte (tiếng Việt tốn 2-3 byte mỗi chữ).", f"The description is {so_byte}/5000 bytes long (Vietnamese takes 2-3 bytes per letter)."))
    if "<" in d or ">" in d:
        loi.append(L("Mô tả không được chứa ký tự < hoặc >.", "The description must not contain the characters < or >."))
    if len(tags or "") > 500:
        loi.append(L(f"Danh sách thẻ dài {len(tags)}/500 ký tự (tính cả dấu phẩy).", f"The tag list is {len(tags)}/500 characters long (commas included)."))
    return loi


#: Ký tự DUY NHẤT tài liệu cấm trong tiêu đề và mô tả. Có một cái là Google trả
#: ``invalidTitle`` / ``invalidDescription`` và CẢ LƯỢT ĐĂNG HỎNG — mà chữ do AI
#: viết thì thỉnh thoảng có ``<`` (ví dụ "<3", "nhiệt độ <100 độ").
_CAM = ("<", ">")


def sach_chu(s: str) -> str:
    """Bỏ hai ký tự YouTube cấm. Xem ``_CAM``."""
    ra = s or ""
    for c in _CAM:
        ra = ra.replace(c, "")
    return ra


def cat_byte(s: str, tran: int) -> str:
    """Cắt cho vừa ``tran`` BYTE UTF-8, không cắt ngang một ký tự.

    Mô tả bị giới hạn 5000 BYTE chứ không phải 5000 ký tự — tiếng Việt có dấu mỗi
    chữ 2-3 byte, nên 5000 ký tự tiếng Việt là hơn 8000 byte và Google từ chối cả
    lượt đăng. Đây là chỗ rất dễ dính vì mô tả do AI viết.
    """
    b = (s or "").encode("utf-8")
    if len(b) <= tran:
        return s or ""
    return b[:tran].decode("utf-8", "ignore")


def dem_the(cac_the: list) -> int:
    """Đếm độ dài danh sách thẻ ĐÚNG CÁCH YOUTUBE ĐẾM.

    Tài liệu nói rõ hai điều mà đếm ký tự thường bỏ sót:
    · dấu phẩy ngăn giữa các thẻ CŨNG tính vào 500 ký tự;
    · thẻ CÓ DẤU CÁCH bị coi như có ngoặc kép bao quanh, và hai dấu ngoặc đó cũng
      tính — nên "van hoa" là 9 ký tự chứ không phải 7.

    Đếm thiếu thì app báo "222/500" trong khi Google thấy 260 và trả ``invalidTags``.
    """
    if not cac_the:
        return 0
    return sum(len(t) + (2 if " " in t else 0) for t in cac_the) + len(cac_the) - 1


def cat_the(cac_the: list, tran: int = 500) -> list:
    """Giữ lại những thẻ đầu còn vừa trần — bỏ thẻ cuối chứ không hỏng cả lượt."""
    giu: list = []
    for t in cac_the:
        if dem_the(giu + [t]) > tran:
            break
        giu.append(t)
    return giu


def _than_bai(post) -> dict:
    tags = cat_the([t.strip() for t in (post.tags or "").split(",") if t.strip()])
    snippet = {
        #  Trần 100 KÝ TỰ (tài liệu), và bỏ ký tự cấm TRƯỚC khi cắt.
        "title": sach_chu((post.title or "").strip())[:100],
        #  Trần 5000 BYTE, không phải 5000 ký tự.
        "description": cat_byte(sach_chu(post.description or ""), 5000),
        "categoryId": post.category_id or "22",
    }
    if tags:
        snippet["tags"] = tags
    if post.default_language:
        snippet["defaultLanguage"] = post.default_language
    #  NGÔN NGỮ ÂM THANH có thể khác ngôn ngữ tiêu đề: bản vietsub giữ tiếng gốc
    #  thì tiêu đề tiếng Việt mà lời thoại tiếng Trung. Trước đây gán bằng nhau
    #  nên YouTube tưởng âm thanh tiếng Việt và chạy sai phụ đề/lồng tiếng tự động.
    #  LƯU Ý: tài liệu ``videos.insert`` KHÔNG liệt kê ``defaultAudioLanguage``
    #  trong danh sách trường đặt được (chỉ có ``defaultLanguage``). Google có thể
    #  bỏ qua nó ở bước đăng. Vẫn gửi vì không hại gì — và bản trước cũng đã gửi —
    #  nhưng ĐỪNG hứa với người dùng là chắc chắn ăn; muốn chắc thì phải gọi thêm
    #  ``videos.update`` (50 đơn vị mỗi video, đắt cho việc đăng theo lô).
    tieng_noi = (getattr(post, "audio_language", "") or "").strip() or post.default_language
    if tieng_noi:
        snippet["defaultAudioLanguage"] = tieng_noi

    status = {
        "privacyStatus": post.privacy or "private",
        #  YouTube BẮT khai; không khai là video bị treo chờ khai báo.
        "selfDeclaredMadeForKids": bool(getattr(post, "made_for_kids", 0)),
    }
    if getattr(post, "synthetic_media", 0):
        #  Chỉ gửi khi CÓ. Google nhận trường này ở ``videos.insert`` (xem danh
        #  sách "You can set values for these properties" trong tài liệu), nhưng
        #  gửi ``false`` cho mọi video thì chẳng khác gì không gửi — mà lại là một
        #  lời khai thừa cho những video không hề đụng tới AI.
        status["containsSyntheticMedia"] = True
    if post.publish_at:
        #  publishAt CHỈ có tác dụng khi privacyStatus = private, và chỉ với video
        #  chưa từng công khai. Ép ở đây thay vì tin vào giao diện: đặt sai thì
        #  YouTube lặng lẽ bỏ qua giờ hẹn và video nằm riêng tư mãi mãi.
        status["privacyStatus"] = "private"
        status["publishAt"] = post.publish_at
    return {"snippet": snippet, "status": status}


#: Những trường "nói thêm" — bỏ đi thì video vẫn lên, chỉ mất phần khai báo.
#:
#: VÌ SAO CẦN DANH SÁCH NÀY: tài liệu ``videos.insert`` liệt kê
#: ``status.containsSyntheticMedia`` là đặt được, nhưng KHÔNG liệt kê
#: ``snippet.defaultAudioLanguage``. Nếu Google từ chối một trong hai (hoặc đổi ý
#: sau này), mà app không có đường lùi, thì MỌI lượt đăng hỏng — người dùng không
#: đăng được video nào cho tới khi có bản vá. Thà mất một dòng khai báo còn hơn
#: mất cả tính năng.
_TRUONG_NOI_THEM = (("snippet", "defaultAudioLanguage"), ("status", "containsSyntheticMedia"))


def _bo_truong_noi_them(than: dict) -> dict:
    gon = {k: dict(v) for k, v in than.items()}
    for nhom, ten in _TRUONG_NOI_THEM:
        gon.get(nhom, {}).pop(ten, None)
    return gon


def mo_phien_day(access_token: str, post, kich_thuoc: int) -> str:
    """Khởi tạo lượt đẩy nhiều mảnh, trả về URL để đẩy tiếp (resume URL).

    Google từ chối vì một trường "nói thêm" thì THỬ LẠI MỘT LẦN không kèm trường
    đó, thay vì để cả lượt đăng chết. Xem ``_TRUONG_NOI_THEM``.
    """
    r = _goi(
        requests.post,
        UPLOAD_URL,
        params={
            "uploadType": "resumable",
            "part": "snippet,status",
            #  GOOGLE MẶC ĐỊNH LÀ "true". App này đăng theo lô nên để nguyên mặc
            #  định của họ là đẩy 90 video thành 90 lần báo cho người đăng ký
            #  trong một buổi — đủ để người ta tắt thông báo hoặc huỷ đăng ký.
            "notifySubscribers": (
                "true" if getattr(post, "notify_subscribers", 0) else "false"
            ),
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(kich_thuoc),
            "X-Upload-Content-Type": "video/*",
        },
        data=json.dumps(_than_bai(post)).encode("utf-8"),
        timeout=60,
    )
    if r.status_code == 400:
        #  Chỉ thử lại khi câu lỗi CÓ NHẮC tới đúng trường mình thêm vào — không
        #  thì tiêu đề sai định dạng cũng bị thử lại lần hai một cách vô nghĩa.
        loi_van = (r.text or "")
        if any(ten in loi_van for _, ten in _TRUONG_NOI_THEM):
            logger.warning(
                "[youtube] Google từ chối một trường khai báo, thử lại không kèm nó: "
                + loi_van[:200]
            )
            r = _goi(
                requests.post,
                UPLOAD_URL,
                params={
                    "uploadType": "resumable",
                    "part": "snippet,status",
                    "notifySubscribers": (
                        "true" if getattr(post, "notify_subscribers", 0) else "false"
                    ),
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(kich_thuoc),
                    "X-Upload-Content-Type": "video/*",
                },
                data=json.dumps(_bo_truong_noi_them(_than_bai(post))).encode("utf-8"),
                timeout=60,
            )
    if r.status_code not in (200, 201):
        raise YoutubeError(_loi_google(r, L("Không mở được lượt đẩy video", "Could not open the video upload session")), ly_do_google(r))
    url = r.headers.get("Location")
    if not url:
        raise YoutubeError(L("Google không trả địa chỉ đẩy video.", "Google did not return a video upload address."))
    return url


def day_tep(resume_url: str, path: str, bat_dau: int = 0, tien_do=None,
            huy=None) -> dict:
    """Đẩy tệp theo từng mảnh. Trả về JSON của video khi xong.

    ``bat_dau`` > 0 = đẩy tiếp lượt dở. ``tien_do(da_gui, tong)`` để hiện %.
    ``huy()`` trả True thì dừng và ném YoutubeError — người dùng bấm Huỷ.
    """
    tong = os.path.getsize(path)
    if tong <= 0:
        raise YoutubeError(L(f"Tệp video rỗng: {path}", f"The video file is empty: {path}"))
    gui = int(bat_dau)

    #  ĐẨY TIẾP THÌ HỎI GOOGLE ĐÃ NHẬN TỚI ĐÂU, đừng tin con số của mình.
    #
    #  Tài liệu "resumable upload protocol" bảo: muốn biết lượt đứt dở đã tới đâu
    #  thì gửi một PUT RỖNG kèm ``Content-Range: bytes */TỔNG``. Cần thật, vì
    #  ``bytes_sent`` của app chỉ là con số app ghi lại — mất điện giữa lúc gửi
    #  mảnh cuối thì hai bên lệch nhau, và PUT sai mốc là Google từ chối.
    if gui > 0:
        r0 = _goi(
            requests.put, resume_url,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{tong}"},
            timeout=120,
        )
        if r0.status_code in (200, 201):
            #  Hoá ra đã xong từ lượt trước — đừng đẩy lại lần nữa.
            if tien_do:
                tien_do(tong, tong)
            return r0.json()
        if r0.status_code == 404:
            raise YoutubeError(
                L("Lượt đẩy dở đã hết hạn bên Google — bài này sẽ được đẩy lại từ đầu.", "The unfinished upload session expired at Google — this post will be uploaded again from the start."),
                "resumeHetHan",
            )
        if r0.status_code == 308:
            rng = r0.headers.get("Range")
            #  Không có ``Range`` nghĩa là Google CHƯA nhận byte nào.
            gui = int(rng.split("-")[1]) + 1 if rng else 0
            if tien_do:
                tien_do(gui, tong)

    #: Số lần liên tiếp Google trả lỗi tạm, để lùi dần (xem dưới).
    lan = [0]
    with open(path, "rb") as f:
        while gui < tong:
            if huy and huy():
                raise YoutubeError(L("Đã huỷ theo yêu cầu.", "Cancelled at your request."))
            f.seek(gui)
            manh = f.read(KICH_THUOC_MANH)
            cuoi = gui + len(manh) - 1
            r = _goi(
                requests.put,
                resume_url,
                headers={
                    "Content-Length": str(len(manh)),
                    "Content-Range": f"bytes {gui}-{cuoi}/{tong}",
                },
                data=manh,
                timeout=300,
            )
            if r.status_code in (200, 201):
                if tien_do:
                    tien_do(tong, tong)
                return r.json()
            if r.status_code == 308:
                #  "Resume Incomplete" — Google nói nó đã nhận tới đâu. TIN THEO
                #  CON SỐ CỦA NÓ, không tự cộng dồn: mạng chập chờn có thể làm
                #  mảnh cuối vào một phần, tự cộng là lệch và hỏng cả tệp.
                rng = r.headers.get("Range")
                gui = int(rng.split("-")[1]) + 1 if rng else gui + len(manh)
                if tien_do:
                    tien_do(gui, tong)
                continue
            if r.status_code == 404:
                #  PHIÊN ĐẨY ĐÃ HẾT HẠN. Tài liệu: gặp 404 thì phải MỞ LƯỢT MỚI và
                #  đẩy lại từ đầu. Không nhận ra mã này thì app cứ nhắm vào cái URL
                #  chết ấy mãi — bấm đẩy lại bao nhiêu lần cũng hỏng y hệt, mà câu
                #  lỗi thì chẳng gợi ý gì.
                raise YoutubeError(
                    L("Lượt đẩy dở đã hết hạn bên Google — bài này sẽ được đẩy lại từ đầu.", "The unfinished upload session expired at Google — this post will be uploaded again from the start."),
                    "resumeHetHan",
                )
            if r.status_code in (500, 502, 503, 504):
                #  LỖI TẠM CỦA GOOGLE — tài liệu bảo lùi dần rồi thử lại. Thử ngay
                #  tại chỗ vài lần trước khi chịu thua: bỏ cuộc ngay thì một cơn
                #  nấc 503 ba giây biến thành một bài "Lỗi" trong hàng đợi, và với
                #  lượt đẩy 90 video thì kiểu gì cũng gặp vài lần.
                lan[0] += 1
                if lan[0] <= 3:
                    time.sleep(2 ** lan[0])       # 2, 4, 8 giây
                    continue
                raise YoutubeError(
                    L(f"Google tạm thời không nhận (mã {r.status_code}) — sẽ thử lại.", f"Google is temporarily not accepting it (code {r.status_code}) — will retry.")
                )
            #  Mảnh này đi lọt thì coi như hết nấc, đếm lại từ đầu.
            lan[0] = 0
            raise YoutubeError(_loi_google(r, L("Đẩy video thất bại", "Video upload failed")), ly_do_google(r))
    raise YoutubeError(L("Đẩy hết tệp mà Google chưa xác nhận xong.", "The whole file was sent but Google has not confirmed it yet."))


def dat_thumbnail(access_token: str, video_id: str, path: str) -> None:
    """Đặt ảnh đại diện. Kênh PHẢI đã xác minh số điện thoại mới dùng được."""
    if not path or not os.path.exists(path):
        return
    with open(path, "rb") as f:
        r = _goi(
            requests.post,
            "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
            params={"videoId": video_id},
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "image/jpeg"},
            data=f.read(),
            timeout=120,
        )
    if r.status_code not in (200, 201):
        raise YoutubeError(_loi_google(r, L("Đặt ảnh đại diện thất bại", "Setting the thumbnail failed")), ly_do_google(r))


def doc_thong_tin_video(access_token: str, video_ids: list) -> dict:
    """Trạng thái + số liệu của những video ĐÃ ĐĂNG. Trả ``{video_id: {...}}``.

    Một lần gọi lo tối đa 50 video và CHỈ TỐN 1 ĐƠN VỊ hạn mức — nên soát lại
    trạng thái cả trăm bài vẫn rẻ hơn một lần đặt ảnh đại diện (50 đơn vị).

    VÌ SAO CẦN: đẩy lên xong app mới chỉ biết "đã gửi". Video còn phải qua khâu
    xử lý của YouTube (có thể HỎNG, có thể bị từ chối vì bản quyền), rồi tới giờ
    hẹn mới công khai. Không hỏi lại thì người dùng phải tự mở YouTube kiểm từng
    cái — mà đó chính là việc họ mua app này để khỏi làm.

    Video đã bị xoá trên YouTube thì đơn giản là KHÔNG có trong kết quả; bên gọi
    nhìn vào đó mà biết.
    """
    ra: dict = {}
    ids = [str(v).strip() for v in (video_ids or []) if str(v or "").strip()]
    for i in range(0, len(ids), 50):
        lo = ids[i:i + 50]
        r = _goi(
            requests.get,
            f"{API}/videos",
            params={
                #  ``processingDetails`` chỉ chủ kênh mới đọc được — đúng thứ ta
                #  cần để biết video đang xử lý hay đã hỏng.
                #
                #  ``contentDetails`` để đọc CHẶN THEO KHU VỰC. Đây mới là chỗ hiện
                #  chữ "Blocked" của YouTube Studio khi video dính khiếu nại bản
                #  quyền — ``rejectionReason`` KHÔNG dùng được cho việc đó, tài liệu
                #  nói rõ trường ấy chỉ có mặt khi ``uploadStatus`` là "rejected",
                #  tức là video bị từ chối hẳn. Video bị chặn thì vẫn "processed",
                #  vẫn có giờ hẹn, nên app cũ báo "Đã lên lịch" như không có gì.
                #
                #  KHÔNG tốn thêm hạn mức: ``videos.list`` là 1 đơn vị cho cả lượt
                #  gọi, thêm bao nhiêu ``part`` cũng vậy.
                #  ``suggestions`` chỉ chủ kênh đọc được, và nó nói thẳng vì sao
                #  YouTube sẽ xử lý hỏng (tệp thật ra là ảnh, là âm thanh, là tệp
                #  nén…). Không xin thì app chỉ biết "failed" mà không biết vì sao.
                "part": ("snippet,status,statistics,processingDetails,"
                         "contentDetails,suggestions"),
                "id": ",".join(lo),
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if r.status_code != 200:
            raise YoutubeError(_loi_google(r, L("Không đọc được trạng thái video", "Could not read the video status")), ly_do_google(r))
        for it in r.json().get("items") or []:
            st = it.get("status") or {}
            sn = it.get("snippet") or {}
            tk = it.get("statistics") or {}
            pd = it.get("processingDetails") or {}
            cd = it.get("contentDetails") or {}
            gy = it.get("suggestions") or {}
            #  Hai cách YouTube diễn đạt chuyện chặn, theo tài liệu:
            #  · ``allowed`` CÓ MẶT và RỖNG  -> chặn ở MỌI nước.
            #  · ``blocked`` liệt kê các nước bị chặn (rỗng = không chặn đâu cả).
            rr = cd.get("regionRestriction") or {}
            cho_phep = rr.get("allowed")
            bi_chan = rr.get("blocked") or []
            chan_het = isinstance(cho_phep, list) and not cho_phep

            def _so(khoa):
                #  YouTube BỎ HẲN trường khi kênh tắt hiển thị (vd ẩn lượt thích).
                #  Trả None chứ không 0: "không biết" và "bằng 0" là hai chuyện,
                #  hiện 0 lượt xem cho video đang chạy tốt là báo sai.
                v = tk.get(khoa)
                try:
                    return int(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            ra[it.get("id") or ""] = {
                "privacy": st.get("privacyStatus") or "",
                "upload_status": st.get("uploadStatus") or "",
                #  Có giá trị khi video bị từ chối (bản quyền, điều khoản…).
                "rejection": st.get("rejectionReason") or "",
                "processing": pd.get("processingStatus") or "",
                "publish_at": st.get("publishAt") or "",
                "published_at": sn.get("publishedAt") or "",
                "title": sn.get("title") or "",
                #  YouTube CÓ THỂ tự xếp video là "dành cho trẻ em" dù ta đã khai
                #  selfDeclaredMadeForKids=false. Bị xếp vậy thì BÌNH LUẬN BỊ TẮT,
                #  không hiện trong đề xuất và mất quảng cáo cá nhân hoá — người
                #  dùng cần biết ngay chứ không phải tự mò trong Studio.
                #  Số nước bị chặn, và cờ "chặn sạch". Người dùng chỉ cần biết
                #  video của mình có xem được hay không, không cần danh sách 200 mã
                #  quốc gia — nhưng giữ vài mã đầu để họ tra lại trong Studio.
                "chan_het": chan_het,
                "so_nuoc_chan": 0 if chan_het else len(bi_chan),
                "vai_nuoc_chan": bi_chan[:5],
                #  Chỉ xem được ở đúng mấy nước này (khi YouTube dùng lối "allowed").
                "chi_xem_duoc_o": (cho_phep or [])[:5] if cho_phep else [],
                #  "Nội dung có bản quyền" — tài liệu: video được tải lên kênh có
                #  liên kết với đối tác nội dung của YouTube RỒI BỊ ĐỐI TÁC ĐÓ NHẬN
                #  BẢN QUYỀN. Đây là thứ gần nhất với "khiếu nại bản quyền" mà API
                #  công khai cho biết — chi tiết từng khiếu nại chỉ có trong
                #  Content ID API, và API đó chỉ mở cho đối tác.
                "co_ban_quyen": bool(cd.get("licensedContent")),
                #  Vì sao YouTube sẽ xử lý hỏng (archiveFile, audioFile, imageFile,
                #  notAVideoFile, projectFile, docFile).
                "loi_xu_ly": list(gy.get("processingErrors") or [])[:5],
                "canh_bao_xu_ly": list(gy.get("processingWarnings") or [])[:5],
                "made_for_kids": bool(st.get("madeForKids")),
                "tu_khai_tre_em": bool(st.get("selfDeclaredMadeForKids")),
                "views": _so("viewCount"),
                "likes": _so("likeCount"),
                "comments": _so("commentCount"),
            }
    return ra


# --------------------------------------------------------------------------- #
#: Mã thứ tiếng của app -> mã YouTube nhận cho phụ đề (BCP-47).
MA_PHU_DE_YT = {"zh": "zh-Hans", "zh-cn": "zh-Hans"}


def co_quyen_phu_de(token: dict) -> bool:
    """Bộ token Google trả về có kèm quyền tải phụ đề không (trường ``scope``)."""
    return SCOPE_PHU_DE in str((token or {}).get("scope") or "").split()


def dat_phu_de(access_token: str, video_id: str, lang: str, ten: str, srt: str) -> str:
    """Tải MỘT phụ đề .srt lên video (captions.insert — 400 đơn vị). Trả id phụ đề."""
    import json as _json
    import uuid as _uuid

    ranh = "sf" + _uuid.uuid4().hex
    than = (
        f"--{ranh}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + _json.dumps({"snippet": {"videoId": video_id,
                                   "language": MA_PHU_DE_YT.get(lang, lang),
                                   "name": ten, "isDraft": False}}, ensure_ascii=False)
        + f"\r\n--{ranh}\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + srt.encode("utf-8") + f"\r\n--{ranh}--\r\n".encode("utf-8")
    r = _goi(
        requests.post,
        "https://www.googleapis.com/upload/youtube/v3/captions",
        params={"part": "snippet", "uploadType": "multipart"},
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": f"multipart/related; boundary={ranh}"},
        data=than,
        timeout=120,
    )
    if r.status_code not in (200, 201):
        ly_do = ly_do_google(r)
        #  CHỈ "insufficientPermissions" (hoặc 403 không lý do) = token thiếu quyền phụ đề.
        #  "forbidden" là chuyện khác (vd video chưa cho phép thêm phụ đề) — không được
        #  xoá cờ quyền của kênh vì nó.
        if r.status_code == 403 and ly_do in ("insufficientPermissions", ""):
            raise YoutubeError(
                L("Kênh này chưa cấp quyền tải phụ đề. Vào tab Kênh, tick “Cho phép tải phụ "
                "đề” rồi bấm Kết nối lại kênh.", "This channel has not granted permission to upload subtitles. Go to the Channels tab, tick \"Allow uploading subtitles\", then click Reconnect channel."), "insufficientPermissions")
        raise YoutubeError(_loi_google(r, L(f"Tải phụ đề '{lang}' thất bại", f"Failed to upload subtitle '{lang}'")), ly_do)
    return (r.json() or {}).get("id") or ""


def ly_do_google(r) -> str:
    """Mã lý do của Google ("quotaExceeded", "forbidden"…), rỗng nếu không đọc được."""
    try:
        for d in ((r.json() or {}).get("error") or {}).get("errors") or []:
            return d.get("reason") or ""
    except Exception:
        pass
    return ""


def _loi_google(r, mo_dau: str) -> str:
    """Dịch lỗi của Google sang câu người dùng hiểu được.

    Phản hồi lỗi của Google là JSON lồng nhiều tầng; in nguyên nó ra màn hình
    thì người dùng không làm gì được với đống chữ đó.
    """
    try:
        e = (r.json() or {}).get("error") or {}
        thong_diep = e.get("message") or r.text[:300]
        ly_do = ""
        for d in e.get("errors") or []:
            ly_do = d.get("reason") or ""
            break
    except Exception:
        thong_diep, ly_do = r.text[:300], ""

    goi_y = {
        #  Quên bật YouTube Data API v3 ở bước 2. Khoá đúng, đăng nhập đúng, mà
        #  mọi lời gọi đều bị chặn — không nói rõ thì người dùng đi soát lại khoá.
        "accessNotConfigured": (
            L("Dự án Google này CHƯA BẬT YouTube Data API v3. Mở "
            "console.cloud.google.com/apis/library/youtube.googleapis.com, chọn "
            "đúng dự án rồi bấm Enable (bước 2 trong hướng dẫn).", "This Google project does NOT HAVE the YouTube Data API v3 enabled. Open console.cloud.google.com/apis/library/youtube.googleapis.com, pick the right project, then click Enable (step 2 in the guide).")
        ),
        "quotaExceeded": L("Đã hết hạn mức hôm nay của dự án Google này. Hạn mức reset lúc nửa đêm giờ Thái Bình Dương (khoảng 14-15 giờ Việt Nam).", "Today's quota for this Google project is used up. The quota resets at midnight Pacific Time (about 14-15 hours Vietnam time)."),
        "uploadLimitExceeded": L("Kênh này đã chạm giới hạn đăng của YouTube — chờ vài giờ rồi thử lại.", "This channel has hit YouTube's upload limit — wait a few hours and try again."),
        "forbidden": L("Kênh chưa được cấp quyền đăng, hoặc chưa xác minh số điện thoại.", "The channel has not been granted upload permission, or its phone number is not verified."),
        "invalidVideoMetadata": L("Tiêu đề/mô tả/thẻ không hợp lệ — soát lại độ dài và ký tự đặc biệt.", "Invalid title/description/tags — check the length and special characters."),
        "youtubeSignupRequired": L("Tài khoản Google này chưa có kênh YouTube.", "This Google account has no YouTube channel."),
        #  Phần dưới: mọi mã lỗi CÒN LẠI mà tài liệu ``videos.insert`` liệt kê.
        #  Không dịch thì người dùng nhận nguyên câu tiếng Anh của Google và không
        #  biết phải sửa ở ô nào.
        "invalidTitle": (
            L("Tiêu đề không hợp lệ: để trống, dài quá 100 ký tự, hoặc có ký tự "
            "< và > (YouTube cấm hai ký tự này).", "Invalid title: empty, longer than 100 characters, or contains the characters < and > (YouTube forbids these two characters).")
        ),
        "invalidDescription": (
            L("Mô tả không hợp lệ: dài quá 5000 BYTE (tiếng Việt có dấu mỗi chữ 2-3 "
            "byte), hoặc có ký tự < và >.", "Invalid description: longer than 5000 BYTES (accented Vietnamese letters take 2-3 bytes each), or contains the characters < and >.")
        ),
        "invalidTags": (
            L("Thẻ không hợp lệ: tổng quá 500 ký tự. YouTube tính cả dấu phẩy ngăn "
            "giữa các thẻ, và thẻ có dấu cách bị tính thêm 2 ký tự ngoặc kép.", "Invalid tags: total over 500 characters. YouTube counts the commas between tags, and a tag with a space is charged 2 extra quote characters.")
        ),
        "invalidCategoryId": (
            L("Danh mục này không dùng được — chọn danh mục khác ở phần cấu hình chung.", "This category cannot be used — pick another category in the general settings.")
        ),
        "invalidPublishAt": (
            L("Giờ hẹn công khai không hợp lệ — phải là mốc trong TƯƠNG LAI và video "
            "phải ở chế độ riêng tư.", "Invalid scheduled publish time — it must be in the FUTURE and the video must be private.")
        ),
        "forbiddenPrivacySetting": (
            L("Kênh này không được đặt chế độ riêng tư đó cho video.", "This channel is not allowed to set that privacy status on the video.")
        ),
        "forbiddenLicenseSetting": L("Kênh này không được đặt giấy phép đó cho video.", "This channel is not allowed to set that license on the video."),
        "defaultLanguageNotSet": (
            L("Thiếu ngôn ngữ mặc định — chọn ngôn ngữ ở phần cấu hình chung.", "Missing default language — choose a language in the general settings.")
        ),
        "mediaBodyRequired": L("Lượt gửi thiếu phần video — thử đẩy lại bài này.", "The request is missing the video part — try uploading this post again."),
        "invalidFilename": L("Tên tệp video không hợp lệ — đổi tên tệp rồi thử lại.", "Invalid video file name — rename the file and try again."),
        "invalidRecordingDetails": L("Thông tin ngày quay không hợp lệ.", "Invalid recording date info."),
        "rateLimitExceeded": (
            L("Gửi quá nhanh, Google chặn tạm. Chờ ít phút rồi bấm đẩy tiếp — "
            "bài đã đẩy dở không mất.", "Sending too fast, Google blocked temporarily. Wait a few minutes then click upload again — the partially uploaded post is not lost.")
        ),
        "dailyLimitExceeded": (
            L("Đã chạm giới hạn trong ngày của dự án Google này. Hạn mức reset lúc "
            "nửa đêm giờ Thái Bình Dương (khoảng 14-15 giờ Việt Nam).", "This Google project has hit its daily limit. The quota resets at midnight Pacific Time (about 14-15 hours Vietnam time).")
        ),
    }.get(ly_do, "")
    phan = f"{mo_dau}: {thong_diep}"
    return f"{phan} — {goi_y}" if goi_y else phan


def bo_bi_mat(cfg) -> tuple:
    """(client_id, client_secret) đã giải mã — dùng ngay rồi bỏ, KHÔNG log."""
    return cfg.client_id or "", giai_ma(cfg.client_secret_enc or "")


__all__ = [
    "YoutubeError", "SCOPES", "SCOPE_PHU_DE", "GIA_THUMBNAIL", "GIA_CAPTION", "GIA_PLAYLIST",
    "TRAN_UPLOAD_NGAY", "TRAN_DON_VI_NGAY", "GIA_DOC_KENH", "NGUONG_NHAC_DON_VI",
    "tru_don_vi", "du_don_vi", "ngay_pacific", "dam_bao_ngay",
    "sach_chu", "cat_byte", "dem_the", "cat_the",
    "ly_do_google", "dong_bo_theo_loi", "LY_DO_HET_HAN_MUC", "LY_DO_HET_LUOT_DANG", "con_lai", "bat_dau_dang_nhap", "lay_access_token", "lay_kenh",
    "kiem_metadata", "mo_phien_day", "day_tep", "dat_thumbnail", "bo_bi_mat",
    "ma_hoa", "che",
]
