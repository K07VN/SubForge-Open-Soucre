# -*- coding: utf-8 -*-
"""CẮT TIMELINE THEO ĐOẠN ĐÃ CHỌN — cho "Xuất đoạn đã chọn" ở chế độ Chi tiết.

Người dùng chọn vài câu trên timeline rồi bấm xuất: bản ra chỉ gồm những đoạn đó, nối
liền nhau. Muốn vậy phải làm hai việc ăn khớp nhau:

  1. CẮT HÌNH + TIẾNG của bản dựng bước 1/2 theo các khoảng đã chọn rồi nối lại;
  2. DỊCH LẠI MỌI MỐC THỜI GIAN (phụ đề, vùng che, logo, watermark, khối chữ) sang
     dòng thời gian MỚI — không thì phụ đề của giây 600 vẫn hiện ở giây 600 trong khi
     bản ra chỉ dài 90 giây, tức mất sạch.

VÌ SAO CẮT TRƯỚC BƯỚC 2/2 chứ không sửa đồ thị lọc của bước đó: bộ dựng filter của
``compositor_service`` mang rất nhiều ràng buộc khớp-với-xem-trước (kiểu phụ đề, vùng
che, quỹ đạo watermark, khung cắt). Chèn trim/concat vào giữa đồ thị đó là đụng đúng
chỗ dễ vỡ nhất. Cắt thành MỘT LƯỢT RIÊNG trước đó thì bước 2/2 không đổi một dòng nào —
nó chỉ nhận vào một video ngắn hơn và một bộ mốc đã dịch sẵn.

MỘT ĐIỀU KHÔNG GIỮ ĐƯỢC: watermark chuyển động theo t liên tục, nên trong bản cắt nó
chạy theo dòng thời gian mới chứ không theo dòng thời gian gốc. Bản xuất đầy đủ vẫn
đúng như xem trước; chỉ bản "đoạn đã chọn" là khác — và khác ở đây là đúng, vì video đã
là một video khác.
"""
from __future__ import annotations

#: Đệm thêm hai đầu mỗi đoạn (giây). Câu nói thường có hơi thở trước/sau; cắt sát mép
#: nghe cụt ngủn. Nhỏ thôi — đệm to thì hai đoạn gần nhau dính vào làm một.
DEM_MAC_DINH = 0.12

#: Hai đoạn cách nhau dưới ngần này thì gộp làm một: cắt rồi nối lại đúng chỗ đó cũng
#: chẳng tiết kiệm được gì mà lại thêm một mối nối nhìn thấy được.
KHE_GOP = 0.35


def _so(v, mac_dinh=None):
    try:
        if v is None or v == "":
            return mac_dinh
        return float(v)
    except (TypeError, ValueError):
        return mac_dinh


def gom_khoang(khoang, dem: float = DEM_MAC_DINH, tong_dai: float = 0.0) -> list:
    """Chuẩn hoá danh sách khoảng: bỏ khoảng rỗng, đệm hai đầu, sắp xếp, GỘP chồng lấn.

    ``tong_dai`` > 0 thì kẹp không cho vượt quá độ dài video.
    Trả về ``[(bat_dau, ket_thuc), ...]`` không chồng nhau, theo thứ tự tăng.
    """
    tho = []
    for k in khoang or []:
        if isinstance(k, dict):
            s, e = _so(k.get("start")), _so(k.get("end"))
        else:
            try:
                s, e = _so(k[0]), _so(k[1])
            except (TypeError, IndexError, KeyError):
                continue
        if s is None or e is None or e <= s:
            continue
        s = max(0.0, s - dem)
        e = e + dem
        if tong_dai > 0:
            e = min(e, tong_dai)
            s = min(s, tong_dai)
        if e > s:
            tho.append((round(s, 3), round(e, 3)))
    if not tho:
        return []
    tho.sort()
    ra = [list(tho[0])]
    for s, e in tho[1:]:
        if s - ra[-1][1] <= KHE_GOP:
            ra[-1][1] = max(ra[-1][1], e)
        else:
            ra.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in ra]


def tong_thoi_luong(khoang) -> float:
    return round(sum(e - s for s, e in khoang), 3)


def doi_moc(t, khoang):
    """Đổi MỘT mốc tuyệt đối sang dòng thời gian sau khi cắt.

    Trả ``None`` nếu mốc rơi ra ngoài mọi đoạn đã chọn (thứ đó không có trong bản ra).
    """
    t = _so(t)
    if t is None:
        return None
    truoc = 0.0
    for s, e in khoang:
        if t < s:
            return None
        if t <= e:
            return round(truoc + (t - s), 3)
        truoc += e - s
    return None


def _kep_vao_khoang(s, e, khoang):
    """Phần giao của [s,e] với các đoạn đã chọn, quy về dòng thời gian mới.

    Trả ``(moi_s, moi_e)`` hoặc ``None`` nếu không giao đoạn nào. Một thứ trải dài qua
    nhiều đoạn (vd vùng che cả video) được kéo từ điểm giao ĐẦU tới điểm giao CUỐI —
    chấp nhận phủ luôn phần nối ở giữa, vì cắt nó thành nhiều mảnh thì một vùng che
    liền mạch bỗng nhấp nháy.
    """
    s, e = _so(s), _so(e)
    if s is None and e is None:
        return None
    lo = s if s is not None else 0.0
    hi = e if e is not None else float("inf")
    if hi <= lo:
        return None
    truoc = 0.0
    dau = cuoi = None
    for ds, de in khoang:
        giao_s, giao_e = max(lo, ds), min(hi, de)
        if giao_e > giao_s:
            a = truoc + (giao_s - ds)
            b = truoc + (giao_e - ds)
            dau = a if dau is None else min(dau, a)
            cuoi = b if cuoi is None else max(cuoi, b)
        truoc += de - ds
    if dau is None:
        return None
    return round(dau, 3), round(cuoi, 3)


def cat_cues(cues, khoang, khoa_s: str = "start_time", khoa_e: str = "end_time") -> list:
    """Dịch danh sách phụ đề sang dòng thời gian mới, bỏ những câu nằm ngoài."""
    ra = []
    for c in cues or []:
        moi = _kep_vao_khoang(c.get(khoa_s), c.get(khoa_e), khoang)
        if moi is None:
            continue
        ra.append({**c, khoa_s: moi[0], khoa_e: moi[1]})
    return ra


def _cat_moc_phan_tu(phan_tu, khoang):
    """Dịch mốc ``start``/``end`` của MỘT phần tử phủ lên hình (vùng che, logo, chữ…).

    Phần tử không đặt mốc nào = hiện suốt video -> giữ nguyên (vẫn hiện suốt bản cắt).
    Đặt mốc mà không giao đoạn nào -> trả ``None`` để nơi gọi bỏ nó đi.
    """
    if not isinstance(phan_tu, dict):
        return phan_tu
    s, e = phan_tu.get("start"), phan_tu.get("end")
    if _so(s) is None and _so(e) is None:
        return phan_tu
    moi = _kep_vao_khoang(s, e, khoang)
    if moi is None:
        return None
    return {**phan_tu, "start": moi[0], "end": moi[1]}


#: Những khoá trong cấu hình ghép hình có mang mốc thời gian. Thêm phần tử phủ mới có
#: ``start``/``end`` thì NHỚ thêm vào đây, không thì bản "đoạn đã chọn" hiện sai giờ.
KHOA_DANH_SACH = ("maskZones", "textOverlays")
KHOA_DON = ("logo", "watermark")


def cat_config(config, khoang):
    """Bản sao cấu hình ghép hình với mọi mốc thời gian đã dịch sang dòng mới."""
    ra = dict(config or {})
    for khoa in KHOA_DANH_SACH:
        ds = ra.get(khoa)
        if isinstance(ds, list):
            ra[khoa] = [x for x in (_cat_moc_phan_tu(p, khoang) for p in ds) if x is not None]
    for khoa in KHOA_DON:
        p = ra.get(khoa)
        if isinstance(p, dict):
            moi = _cat_moc_phan_tu(p, khoang)
            #  Logo/watermark có khoanh giờ mà rơi ngoài đoạn chọn -> TẮT hẳn, không xoá
            #  khoá (xoá thì bộ dựng đọc ra mặc định và hiện suốt video).
            ra[khoa] = moi if moi is not None else {**p, "enabled": False}
    return ra


def lenh_cat(ffmpeg: str, nguon: str, dich: str, khoang, v_args=None, co_tieng=True) -> list:
    """Lệnh ffmpeg cắt ``nguon`` theo các đoạn rồi nối lại thành ``dich``.

    Dùng trim/atrim + concat trong MỘT lượt: chính xác tới từng khung hình. Cắt kiểu
    ``-ss -c copy`` chỉ cắt được ở khung khoá nên lệch tới cả giây — với đoạn vài giây
    thì lệch đó là hỏng hẳn.
    """
    v_args = list(v_args or ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                             "-pix_fmt", "yuv420p"])
    loc, nhan_v, nhan_a = [], [], []
    for i, (s, e) in enumerate(khoang):
        loc.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        nhan_v.append(f"[v{i}]")
        if co_tieng:
            loc.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
            nhan_a.append(f"[a{i}]")
    n = len(khoang)
    if co_tieng:
        xen = "".join(f"{v}{a}" for v, a in zip(nhan_v, nhan_a))
        loc.append(f"{xen}concat=n={n}:v=1:a=1[vout][aout]")
    else:
        loc.append(f"{''.join(nhan_v)}concat=n={n}:v=1:a=0[vout]")
    cmd = [ffmpeg, "-y", "-i", nguon, "-filter_complex", "; ".join(loc), "-map", "[vout]"]
    if co_tieng:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    cmd += v_args + ["-movflags", "+faststart", dich]
    return cmd


#: Biên nhận bước 1/2 để lại khi nó đã tự cắt nguồn trước lúc ghép. Bước 2/2 đọc tệp
#: này để biết KHỎI cắt lại — xem ``doc_manifest``.
TEP_MANIFEST = "final_review.dachon.json"

#: Tên bản dựng của riêng lượt "xuất đoạn đã chọn". Phải khác ``final_review.mp4``, nếu
#: không lượt xuất đầy đủ sau đó sẽ vớ phải bản dài mấy chục giây này.
TEP_BAN_CAT = "final_review.dachon.mp4"


def chuan_raw(khoang) -> list:
    """Đưa danh sách khoảng THÔ về một dạng duy nhất để so sánh hai bên cho khớp.

    Bước 1/2 và bước 2/2 nhận cùng một danh sách từ giao diện nhưng đi qua hai đường
    khác nhau (thân yêu cầu JSON, tệp trên đĩa). So sánh trên bản chuẩn hoá này thì
    chênh lệch kiểu dữ liệu (chuỗi "12" vs số 12, dict vs cặp) không làm lệch kết quả.
    """
    ra = []
    for k in khoang or []:
        if isinstance(k, dict):
            s, e = _so(k.get("start")), _so(k.get("end"))
        else:
            try:
                s, e = _so(k[0]), _so(k[1])
            except (TypeError, IndexError, KeyError):
                continue
        if s is None or e is None:
            continue
        ra.append([round(s, 3), round(e, 3)])
    return ra


def ghi_manifest(pdir: str, khoang_tho, khoang) -> None:
    """Bước 1/2 đóng dấu: bản cắt này ứng với đúng những khoảng nào."""
    import json as _js
    import os as _os

    try:
        with open(_os.path.join(pdir, TEP_MANIFEST), "w", encoding="utf-8") as f:
            _js.dump({"tho": chuan_raw(khoang_tho),
                      "khoang": [list(x) for x in khoang]}, f)
    except OSError:
        #  Ghi hụt thì bước 2/2 tự cắt lại như đường cũ — chậm hơn, không sai.
        pass


def doc_manifest(pdir: str, khoang_tho):
    """Bản cắt sẵn có khớp với yêu cầu lần này không.

    Trả về danh sách khoảng đã gộp nếu KHỚP và tệp video cắt còn đó, ngược lại ``None``.
    So bằng khoảng THÔ chứ không phải khoảng đã gộp: tham số gộp (đệm mép, khe gộp) mà
    đổi ở một bên thì hai bên vẫn phải nhận ra là cùng một yêu cầu — hoặc là không.

    NGHI NGỜ LÀ TRẢ ``None``: bước 2/2 tự cắt lại, chậm hơn nhưng chắc chắn đúng. Nhầm
    theo hướng kia là người dùng nhận một bản cắt của lần trước.
    """
    import json as _js
    import os as _os

    try:
        tep = _os.path.join(pdir, TEP_MANIFEST)
        phim = _os.path.join(pdir, TEP_BAN_CAT)
        if not _os.path.exists(tep) or not _os.path.exists(phim):
            return None
        if _os.path.getsize(phim) <= 0:
            return None
        with open(tep, "r", encoding="utf-8") as f:
            bn = _js.load(f)
        if bn.get("tho") != chuan_raw(khoang_tho):
            return None
        #  Bản cắt phải MỚI HƠN biên nhận: dựng lại cả phim sẽ ghi đè tệp video mà không
        #  động tới biên nhận, lúc đó biên nhận cũ nói dối.
        if _os.path.getmtime(phim) < _os.path.getmtime(tep) - 1:
            return None
        khoang = [tuple(x) for x in (bn.get("khoang") or [])]
        return khoang or None
    except (OSError, ValueError, TypeError):
        return None


def don_ban_cat(pdir: str) -> None:
    """Xoá mọi thứ sinh ra cho lượt "xuất đoạn đã chọn" — chúng chỉ sống trong một lượt.

    Để lại thì mỗi lần xuất đoạn lại bỏ thêm một bản phim vào thư mục dự án, mà biên
    nhận cũ còn đó lại làm lượt sau tưởng đã có sẵn bản cắt.
    """
    import os as _os

    ten = [TEP_MANIFEST, TEP_BAN_CAT, "final_review.dachon.tmp.mp4",
           "nguon.dachon.mp4", "master_tts.dachon.wav",
           "master_tts_fingerprint.dachon.json",
           _os.path.join("separated", "vocals.dachon.wav"),
           _os.path.join("separated", "no_vocals.dachon.wav")]
    for t in ten:
        try:
            duong = _os.path.join(pdir, t)
            if _os.path.exists(duong):
                _os.remove(duong)
        except OSError:
            pass


def lenh_cat_tieng(ffmpeg: str, nguon: str, dich: str, khoang) -> list:
    """Như ``lenh_cat`` nhưng cho tệp CHỈ CÓ TIẾNG (stem giọng gốc / nhạc nền).

    Giữ nguyên PCM 16-bit: đây là nguyên liệu cho lượt trộn tiếp theo, nén lại ở khâu
    trung gian là mất chất lượng mà chẳng tiết kiệm được gì.
    """
    loc, nhan = [], []
    for i, (s, e) in enumerate(khoang):
        loc.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        nhan.append(f"[a{i}]")
    loc.append(f"{''.join(nhan)}concat=n={len(khoang)}:v=0:a=1[aout]")
    return [ffmpeg, "-y", "-i", nguon, "-filter_complex", "; ".join(loc),
            "-map", "[aout]", "-c:a", "pcm_s16le", dich]
