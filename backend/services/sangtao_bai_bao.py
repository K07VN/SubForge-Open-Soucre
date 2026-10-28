"""LẤY NỘI DUNG BÀI BÁO TỪ LINK cho chế độ "Sáng tạo" (đọc báo học ngoại ngữ).

App tự tải MỘT trang người dùng đưa và tách phần văn bản chính (tiêu đề + các đoạn) — AI không phải "đoán" nội dung từ link. Không đăng nhập, không vượt
tường trả phí, không thu thập nhiều trang: trang cần đăng nhập / nạp bằng JavaScript / chặn truy cập thì báo rõ và người dùng dán chữ vào tay.

An toàn (chống SSRF): chỉ http/https; chỉ nối tới địa chỉ CÔNG CỘNG (chặn localhost, LAN, link-local, CGNAT, IPv4 nhúng trong IPv6…) ở MỌI lần chuyển hướng để một trang web khác không
lợi dụng app tải thứ trong máy/mạng của người dùng; phân giải DNS MỘT LẦN rồi kết nối thẳng tới đúng địa chỉ đã kiểm (chống DNS rebinding); không dùng proxy theo biến môi trường;
giới hạn dung lượng + thời gian; không thực thi mã trang. Thư viện: ``requests`` + ``beautifulsoup4`` (MIT), trình phân tích
``html.parser`` có sẵn của Python.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, UnicodeDammit

TOI_DA_BYTE = 3_000_000
TOI_DA_KY_TU = 60_000                       # văn bản giữ lại tối đa (bài dài hơn bị cắt, có báo)
THOI_GIAN_CHO = 15
TOI_DA_CHUYEN_HUONG = 5
TOI_THIEU_KY_TU = 400                       # dưới mức này coi như không tách được bài
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SubForge-ArticleReader/1.0"
LUU_Y_BAN_QUYEN = ("Bài báo thường có bản quyền, kể cả khi chỉ đọc lại bằng giọng nói. Hãy ưu tiên nguồn cho phép sử dụng (giấy phép mở / miền công cộng / của chính bạn), "
                   "chọn bài NGẮN, dùng \"Viết lại\" bằng câu chữ riêng và chế độ \"Bài giảng\" (kèm dịch, từ vựng, ngữ pháp) thay vì chỉ đọc nguyên văn, "
                   "và giữ phần ghi nguồn (app tự ghi nguồn + link gốc trong video; hãy dán cả vào mô tả khi đăng). Người đăng video chịu trách nhiệm về quyền sử dụng nội dung; "
                   "cần chắc chắn thì hỏi chủ sở hữu hoặc tư vấn pháp lý.")


class LoiBai(Exception):
    """Lỗi lấy bài có thể báo cho người dùng bằng tiếng Việt."""


_RAC = re.compile(r"comment|share|social|related|recommend|promo|advert|\bads?\b|subscribe|newsletter|cookie|banner|sidebar|menu|breadcrumb|footer|popup|modal|"
                  r"paywall|signup|sign-up|login|outbrain|taboola|sponsor|byline-share|toolbar|tags|author-box|bio\b", re.I)
_VUNG_BAI = re.compile(r"article[-_]?(body|content|text)|post[-_]?(content|body)|entry[-_]?content|story[-_]?(body|content)|content[-_]?body|main[-_]?content|"
                       r"body[-_]?text|news[-_]?content|detail[-_]?content|fck_detail|maincontent", re.I)
#: tiêu đề mục "tin liên quan / đọc thêm" — từ dòng này trở xuống KHÔNG còn là nội dung bài (danh sách tiêu đề các bài khác, video, tin mới…)
_MUC_LIEN_QUAN = re.compile(r"^(あわせて読みたい|関連(ニュース|記事|リンク|情報|番組)?|関連する?(記事|ニュース)|おすすめ(記事|ニュース)?|人気(記事|ニュース)|ランキング|深掘りコンテンツ|最新・注目の動画|"
                            r"新着ニュース|各地のニュース|注目ワード|related( articles| stories| news| posts)?|read more|read next|recommended( for you| stories| articles)?|more from.*|"
                            r"trending( now)?|you may also like|most (read|popular)|相关(阅读|新闻|文章|推荐)|推荐阅读|更多新闻|热门(文章|新闻)|관련 ?(기사|뉴스)|추천 ?(기사|뉴스)|많이 본 ?(기사|뉴스))\s*[:：]?$", re.I)
#: dấu hiệu bài bị cắt (chỉ có đoạn dẫn): kết thúc bằng … / ... / [...]
_BI_CAT = re.compile(r"(…|\.{3}|\[…\]|\[\.{3}\]|続きを読む|続きはこちら|continue reading|read more|阅读全文|查看全文|전체 보기)\s*$", re.I)
_DONG_RAC = re.compile(r"^(share|advertisement|advert|read more|related|subscribe|sign up|sign in|log in|follow us|cookie|copyright|©|all rights reserved|"
                       r"quảng cáo|đọc thêm|xem thêm|chia sẻ|theo dõi|đăng ký|広告|関連記事|もっと見る|共有)", re.I)


def _la_ip_cam(ip: str) -> bool:
    """True = KHÔNG được kết nối tới địa chỉ này (không phải địa chỉ công cộng toàn cầu). Xử lý IPv6 có scope (``fe80::1%eth0``), IPv4 NHÚNG trong IPv6
    (``::ffff:127.0.0.1``, 6to4, NAT64) và Teredo; chuỗi không đọc được cũng bị chặn."""
    try:
        a = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return True
    if a.version == 6:
        if a.teredo is not None:
            return True
        nhung = a.ipv4_mapped or a.sixtofour
        if a in ipaddress.ip_network("64:ff9b::/96"):                           # NAT64: IPv4 nằm ở 32 bit cuối
            nhung = ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)
        if nhung is not None:
            return _la_ip_cam(str(nhung))
    return (not a.is_global) or a.is_multicast


def _phan_giai(host: str, port: int) -> str:
    """Phân giải ``host`` MỘT LẦN, kiểm MỌI địa chỉ trả về là công cộng, rồi trả MỘT địa chỉ để kết nối thẳng tới (ưu tiên IPv4). Kết nối bằng đúng địa chỉ đã kiểm —
    không để thư viện HTTP phân giải lại — nên kẻ điều khiển DNS không thể trả địa chỉ công cộng lúc kiểm rồi đổi sang địa chỉ nội bộ lúc kết nối (DNS rebinding)."""
    try:
        ds = [x[4][0] for x in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]
    except (OSError, UnicodeError):
        raise LoiBai("Không tìm thấy trang (kiểm tra đường dẫn và kết nối mạng)")
    if not ds or any(_la_ip_cam(ip) for ip in ds):
        raise LoiBai("Không được lấy trang ở địa chỉ nội bộ / mạng riêng")
    return next((ip for ip in ds if ":" not in ip), ds[0])


def _tach_url(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, đường_dẫn+query) của một URL http/https hợp lệ; sai → :class:`LoiBai`."""
    u = (url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise LoiBai("Chỉ nhận đường dẫn http:// hoặc https://")
    p = urlparse(u)
    if not p.hostname:
        raise LoiBai("Đường dẫn không hợp lệ")
    try:
        cong = p.port
    except ValueError:
        raise LoiBai("Đường dẫn không hợp lệ (cổng sai)")
    scheme = p.scheme.lower()
    return scheme, p.hostname, cong or (443 if scheme == "https" else 80), (p.path or "/") + (("?" + p.query) if p.query else "")


def kiem_url(url: str) -> str:
    """Chuẩn hoá + kiểm an toàn một URL (giao thức, địa chỉ phân giải là công cộng); sai → :class:`LoiBai`. Trả URL sạch."""
    scheme, host, cong, _ = _tach_url(url)
    _phan_giai(host, cong)
    return url.strip()


class _GhimTen(requests.adapters.HTTPAdapter):
    """Kết nối HTTPS tới ĐỊA CHỈ IP đã kiểm nhưng vẫn xác thực chứng chỉ + gửi SNI theo TÊN MIỀN gốc."""

    def __init__(self, ten: str):
        self._ten = ten
        super().__init__(max_retries=0)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        kw["server_hostname"] = self._ten
        kw["assert_hostname"] = self._ten
        return super().init_poolmanager(connections, maxsize, block=block, **kw)


def _tai(url: str) -> tuple[str, str]:
    """(html, url cuối). Tự theo chuyển hướng nhưng phân giải + kiểm an toàn + GHIM địa chỉ ở TỪNG chặng; không dùng proxy theo biến môi trường (proxy sẽ tự phân giải lại)."""
    cur = (url or "").strip()
    for _ in range(TOI_DA_CHUYEN_HUONG + 1):
        scheme, host, cong, duong = _tach_url(cur)
        ip = _phan_giai(host, cong)
        netloc = f"[{ip}]" if ":" in ip else ip
        mac_dinh = 443 if scheme == "https" else 80
        url_ip = f"{scheme}://{netloc}{'' if cong == mac_dinh else ':' + str(cong)}{duong}"
        phien = requests.Session()
        phien.trust_env = False
        if scheme == "https":
            phien.mount("https://", _GhimTen(host))
        try:
            r = phien.get(url_ip, headers={"User-Agent": UA, "Host": host if cong == mac_dinh else f"{host}:{cong}", "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", "Accept-Language": "*"},
                          timeout=THOI_GIAN_CHO, stream=True, allow_redirects=False)
        except requests.RequestException as e:
            raise LoiBai(f"Không tải được trang: {e.__class__.__name__}")
        try:
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                nxt = r.headers.get("Location")
                if not nxt:
                    raise LoiBai("Trang chuyển hướng sai")
                cur = urljoin(cur, nxt)
                continue
            if r.status_code in (401, 402, 403):
                raise LoiBai("Trang yêu cầu đăng nhập / trả phí hoặc chặn truy cập tự động — hãy dán nội dung vào tay")
            if r.status_code >= 400:
                raise LoiBai(f"Trang trả lỗi {r.status_code}")
            kieu = (r.headers.get("Content-Type") or "").lower()
            if kieu and not any(x in kieu for x in ("html", "xml", "text/plain")):
                raise LoiBai("Đường dẫn này không phải trang văn bản (có thể là ảnh / video / PDF)")
            raw = b""
            for chunk in r.iter_content(65536):
                raw += chunk
                if len(raw) > TOI_DA_BYTE:
                    raise LoiBai("Trang quá lớn")
            return UnicodeDammit(raw, is_html=True).unicode_markup or "", cur
        finally:
            r.close()
            phien.close()
    raise LoiBai("Trang chuyển hướng quá nhiều lần")


def _chuan(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _ty_le_cjk(t: str) -> float:
    return sum(1 for c in t if ord(c) >= 0x2E80) / max(1, len(t))


def _la_rac(el) -> bool:
    for a in [el, *el.parents][:6]:
        if getattr(a, "attrs", None) is None:
            continue
        nhan = " ".join([" ".join(a.get("class") or []), a.get("id") or "", a.get("role") or ""])
        if nhan.strip() and _RAC.search(nhan) and a.name not in ("html", "body", "article", "main"):
            return True
    return False


def _diem(el) -> int:
    n = 0
    for p in el.find_all("p"):
        t = _chuan(p.get_text(" "))
        if len(t) >= (14 if _ty_le_cjk(t) > 0.3 else 40) and not _la_rac(p):
            n += len(t)
    return n


def _doan_van(cont) -> list[str]:
    ra: list[str] = []
    thay = set()
    for el in cont.find_all(["h1", "h2", "h3", "p", "blockquote"]):
        if el.name == "blockquote" and el.find("p"):
            continue                                                  # đã lấy qua <p> bên trong
        if _la_rac(el):
            continue
        t = _chuan(el.get_text(" "))
        if _MUC_LIEN_QUAN.match(t):
            if ra:                                                            # đã có nội dung: từ mục "tin liên quan" trở xuống bỏ hết
                break
            continue
        cjk = _ty_le_cjk(t) > 0.3
        if len(t) < (6 if cjk else (3 if el.name in ("h1", "h2", "h3") else 25)) or _DONG_RAC.match(t) or t in thay:
            continue
        if len(t) < 90 and (el.find_parent("a") is not None or sum(len(_chuan(a.get_text(" "))) for a in el.find_all("a")) >= 0.8 * len(t)):
            continue                                                          # dòng ngắn chỉ là liên kết (tiêu đề của một bài khác), không phải đoạn văn của bài
        thay.add(t)
        ra.append(t)
    return ra


def _json_ld(soup) -> list[str]:
    ra: list[str] = []
    for sc in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            d = json.loads(sc.string or sc.get_text() or "")
        except ValueError:
            continue
        pile = [d]
        while pile:
            x = pile.pop()
            if isinstance(x, list):
                pile += x
            elif isinstance(x, dict):
                if isinstance(x.get("articleBody"), str) and len(x["articleBody"]) > 200:
                    ra += [_chuan(p) for p in re.split(r"\n+", x["articleBody"]) if len(_chuan(p)) >= 20]
                pile += list(x.values())
    return ra


def _meta(soup, *khoa: tuple) -> str:
    for ten, gia_tri in khoa:
        m = soup.find("meta", attrs={ten: gia_tri})
        if m and m.get("content") and _chuan(m["content"]):
            return _chuan(m["content"])
    return ""


def _tac_gia_ngay(soup, ld_obj: list) -> tuple[str, str]:
    """(tác giả, ngày đăng) từ thẻ meta / JSON-LD / <time>. Không có thì rỗng — KHÔNG đoán."""
    tg = _meta(soup, ("name", "author"), ("property", "article:author"), ("name", "twitter:creator"), ("name", "dc.creator"))
    ngay = _meta(soup, ("property", "article:published_time"), ("itemprop", "datePublished"), ("name", "date"), ("name", "pubdate"), ("name", "dc.date"), ("name", "publishdate"))
    for x in ld_obj:
        if not tg and isinstance(x.get("author"), (dict, list, str)):
            a = x["author"]
            a = a[0] if isinstance(a, list) and a else a
            tg = _chuan(a.get("name", "") if isinstance(a, dict) else a if isinstance(a, str) else "")
        if not ngay and isinstance(x.get("datePublished"), str):
            ngay = _chuan(x["datePublished"])
    if not ngay:
        tm = soup.find("time", attrs={"datetime": True})
        ngay = _chuan(tm["datetime"]) if tm else ""
    if tg.startswith(("http://", "https://")):                         # article:author hay là link trang tác giả → bỏ
        tg = ""
    return tg[:80], ngay[:10] if re.match(r"\d{4}-\d{2}-\d{2}", ngay) else ngay[:40]


def _json_ld_obj(soup) -> list:
    ra: list = []
    for sc in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            d = json.loads(sc.string or sc.get_text() or "")
        except ValueError:
            continue
        pile = [d]
        while pile:
            x = pile.pop()
            if isinstance(x, list):
                pile += x
            elif isinstance(x, dict):
                if x.get("@type") and any(k in x for k in ("author", "datePublished", "articleBody")):
                    ra.append(x)
                pile += list(x.values())
    return ra


def _tieu_de(soup, ten_bao: str = "") -> str:
    """Tiêu đề bài (og:title → twitter:title → <h1> → <title>), bỏ đuôi " | tên báo"."""
    tieu_de = ""
    for sel in (("meta", {"property": "og:title"}), ("meta", {"name": "twitter:title"})):
        m = soup.find(*sel)
        if m and m.get("content"):
            tieu_de = _chuan(m["content"])
            break
    if not tieu_de:
        h1 = soup.find("h1")
        tieu_de = _chuan(h1.get_text(" ")) if h1 else _chuan(soup.title.get_text(" ")) if soup.title else ""
    for sep in (" | ", " - ", " – ", " — ", " ｜ "):                                  # "Tiêu đề | NHKニュース": bỏ đuôi tên báo
        if sep in tieu_de and ten_bao and ten_bao.lower() in tieu_de.rsplit(sep, 1)[1].lower():
            tieu_de = tieu_de.rsplit(sep, 1)[0].strip()
            break
    return tieu_de


def _canh_bao(thieu: bool) -> list[str]:
    if not thieu:
        return []
    return ["Chỉ lấy được PHẦN DẪN của bài (văn bản kết thúc bằng “…”): trang này chỉ hiện đủ bài sau khi người đọc xử lý hộp thoại của trang (xác nhận điều khoản / đăng nhập) hoặc khi đã đăng nhập "
            "— app không tự bấm đồng ý hay đăng nhập thay bạn và không vượt qua cơ chế đó. Hãy mở bài trong trình duyệt của bạn, chọn phần chữ của bài, sao chép rồi DÁN vào ô nội dung bên dưới (thay cho đoạn vừa lấy)."]


def tach_bai(html: str, url: str = "") -> dict:
    """Tách {title, text, lang, chars, cat, site, author, date} từ HTML (hàm thuần — dễ kiểm thử bằng chuỗi). Không tách đủ nội dung → :class:`LoiBai`."""
    soup = BeautifulSoup(html or "", "html.parser")
    ten_bao = _meta(soup, ("property", "og:site_name"), ("name", "application-name"))
    tac_gia, ngay_dang = _tac_gia_ngay(soup, _json_ld_obj(soup))
    tieu_de = _tieu_de(soup, ten_bao)
    ld = _json_ld(soup)
    lang = (soup.html.get("lang") if soup.html else "") or ""
    for t in soup(["script", "style", "noscript", "nav", "footer", "aside", "form", "iframe", "svg", "button", "template", "figure", "header"]):
        t.decompose()
    ung_vien = [e for e in soup.find_all(["article", "main"])]
    ung_vien += [e for e in soup.find_all(["div", "section"]) if _VUNG_BAI.search(" ".join(e.get("class") or []) + " " + (e.get("id") or "") + " " + (e.get("itemprop") or ""))]
    ung_vien += [e for e in soup.find_all(attrs={"itemprop": "articleBody"})]
    if soup.body:
        ung_vien.append(soup.body)
    tot, d_tot = None, 0
    for e in ung_vien:
        d = _diem(e)
        if tot is None or d > d_tot * 1.4:                          # ưu tiên ứng viên đứng trước (cụ thể hơn) trừ khi ứng viên sau hơn hẳn
            tot, d_tot = e, d
    doan = _doan_van(tot) if tot is not None else []
    if sum(len(x) for x in doan) < TOI_THIEU_KY_TU * (0.4 if _ty_le_cjk(" ".join(doan)) > 0.3 else 1.0) and ld:
        doan = ld
    van = "\n\n".join(doan)
    if tieu_de and doan and _chuan(doan[0]) == tieu_de:
        doan = doan[1:]
        van = "\n\n".join(doan)
    nguong = TOI_THIEU_KY_TU * (0.4 if _ty_le_cjk(van) > 0.3 else 1.0)
    thieu = bool(_BI_CAT.search(van.rstrip()))                                  # chỉ có phần dẫn: nhiều trang chỉ gửi đủ bài cho trình duyệt đã đăng nhập / được cấp quyền
    if len(van) < nguong and not (thieu and len(van) >= 40):
        raise LoiBai("Không tách được nội dung bài (trang cần đăng nhập / trả phí, nạp chữ bằng JavaScript, hoặc không phải bài viết) — hãy dán nội dung vào tay")
    cat = len(van) > TOI_DA_KY_TU
    if cat:
        van = van[:TOI_DA_KY_TU].rsplit("\n\n", 1)[0]
    return {"title": tieu_de[:200], "text": van, "lang": lang[:10], "chars": len(van), "cat": cat, "site": ten_bao[:80], "author": tac_gia, "date": ngay_dang,
            "dai": len(van) > (1500 if _ty_le_cjk(van) > 0.3 else 3500), "thieu": thieu, "canh_bao": _canh_bao(thieu)}


def _hoan_thien(kq: dict, url: str) -> dict:
    host = (urlparse(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    kq.update(url=url, host=host, site=kq.get("site") or host, luu_y=LUU_Y_BAN_QUYEN)
    return kq


def lay_bai(url: str) -> dict:
    """Tải + tách bài từ ``url`` → {url, site, title, text, lang, chars, cat, luu_y}."""
    html, cuoi = _tai(url)
    return _hoan_thien(tach_bai(html, cuoi), cuoi)


#: dòng giao diện (nút chia sẻ, in…) và dòng ngày giờ trong chữ hiển thị của trang
_DONG_GIAO_DIEN = re.compile(r"^(シェアする|共有|印刷|ブックマーク|メニュー|share|print|bookmark|menu|tweet|facebook|line|email|copy link)$", re.I)
_DONG_NGAY = re.compile(r"^[（(]?\d{4}年\d{1,2}月\d{1,2}日.*$|^\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}$|^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}.*$")
TOI_DA_HTML = 4_000_000
TOI_DA_CHU = 500_000


def tach_tu_van_ban(van_ban: str, tieu_de: str = "", ten_bao: str = "", tac_gia: str = "", ngay: str = "", lang: str = "") -> dict:
    """Tách bài từ CHỮ ĐANG HIỂN THỊ của trang (``innerText`` do cửa sổ trình duyệt của app trả về) — dùng khi HTML không có thẻ đoạn văn rõ ràng. Bỏ tiêu đề trùng, nút chia sẻ,
    dòng ngày giờ, dòng quá ngắn (tiêu đề mục, nút) và từ mục "tin liên quan" trở xuống."""
    ra: list[str] = []
    thay = set()
    for t in (_chuan(x) for x in re.split(r"[\r\n]+", van_ban or "")):
        if not t:
            continue
        if _MUC_LIEN_QUAN.match(t):
            if ra:
                break
            continue
        if t == tieu_de or _DONG_GIAO_DIEN.match(t) or _DONG_NGAY.match(t) or t in thay:
            continue
        if len(t) < (12 if _ty_le_cjk(t) > 0.3 else 40):
            continue
        thay.add(t)
        ra.append(t)
    van = "\n\n".join(ra)
    thieu = bool(_BI_CAT.search(van.rstrip()))
    if len(van) < TOI_THIEU_KY_TU * (0.4 if _ty_le_cjk(van) > 0.3 else 1.0) and not (thieu and len(van) >= 40):
        raise LoiBai("Không tách được nội dung bài từ chữ của trang — hãy dán nội dung vào tay")
    cat = len(van) > TOI_DA_KY_TU
    if cat:
        van = van[:TOI_DA_KY_TU].rsplit("\n\n", 1)[0]
    return {"title": tieu_de[:200], "text": van, "lang": lang[:10], "chars": len(van), "cat": cat, "site": ten_bao[:80], "author": tac_gia, "date": ngay,
            "dai": len(van) > (1500 if _ty_le_cjk(van) > 0.3 else 3500), "thieu": thieu, "canh_bao": _canh_bao(thieu)}


def tach_tu_trinh_duyet(url: str, html: str, main_text: str = "") -> dict:
    """Tách bài từ trang ĐÃ DỰNG XONG trong cửa sổ trình duyệt của app (Electron): ``html`` = HTML sau khi JavaScript chạy, ``main_text`` = chữ đang hiển thị của vùng chính.
    Thử tách theo thẻ đoạn văn trước; không được (hoặc chỉ ra phần dẫn) thì tách theo chữ hiển thị; lấy kết quả ĐẦY ĐỦ hơn. KHÔNG tải mạng."""
    html, main_text = (html or "")[:TOI_DA_HTML], (main_text or "")[:TOI_DA_CHU]
    kq, loi = None, None
    try:
        kq = tach_bai(html, url)
    except LoiBai as e:
        loi = e
    if (kq is None or kq["thieu"]) and main_text.strip():
        soup = BeautifulSoup(html, "html.parser")
        ten_bao = _meta(soup, ("property", "og:site_name"), ("name", "application-name"))
        tg, ngay = _tac_gia_ngay(soup, _json_ld_obj(soup))
        tieu_de = kq["title"] if kq else _tieu_de(soup, ten_bao)
        try:
            kq2 = tach_tu_van_ban(main_text, tieu_de, ten_bao, tg, ngay, (soup.html.get("lang") if soup.html else "") or "")
            if kq is None or (not kq2["thieu"] and kq["thieu"]) or kq2["chars"] > kq["chars"] * 1.5:
                kq = kq2
        except LoiBai as e:
            loi = loi or e
    if kq is None:
        raise loi or LoiBai("Không tách được nội dung bài")
    return _hoan_thien(kq, url)


def la_link(van_ban: str) -> Optional[str]:
    """Văn bản người dùng dán là MỘT đường dẫn (và không có gì khác) → trả đường dẫn; ngược lại None."""
    t = (van_ban or "").strip()
    return t if re.fullmatch(r"https?://\S+", t) else None
