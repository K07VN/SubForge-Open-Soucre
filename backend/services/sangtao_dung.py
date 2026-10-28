"""Điều phối dựng video "Sáng tạo" (học ngoại ngữ): storyboard → mốc đoạn tiếng từng cảnh → dựng clip song song → ghép tiếng (hoặc im lặng) → nối thành một video mp4.

Không đụng mạng, không gọi AI. Hủy bằng ``threading.Event``; tiến độ báo qua callback ``(đã_dựng_khung, tổng_khung, thông_điệp)``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from services import sangtao_bai as B
from services import sangtao_canh as CA
from services import sangtao_hoc as HOC
from services.sangtao_canh import FFMPEG, Huy, LoiDung
from services.sangtao_ruby import bo_ruby as _bo_ruby

CPS = B.CPS


@dataclass
class KetQuaDung:
    duong_dan: str
    tong_giay: float
    so_canh: int
    canh_bao: list = field(default_factory=list)
    thoi_gian_dung: float = 0.0
    khung_moi_giay: float = 0.0
    #: phụ đề CC của CẢ video (giây TRONG video cuối): [{bd, kt, dich, me}] — xem :func:`phu_de_video`. Rỗng khi chỉ xem thử một cảnh.
    phu_de: list = field(default_factory=list)


def _giay(m: "re.Match") -> float:
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _thoi_luong_that(stderr: str) -> Optional[float]:
    """Thời lượng THẬT (giây) đã GIẢI MÃ, đọc từ dòng tiến độ ``time=HH:MM:SS.xx`` cuối cùng của ffmpeg; không có thì None."""
    ds = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", stderr or "")
    return int(ds[-1][0]) * 3600 + int(ds[-1][1]) * 60 + float(ds[-1][2]) if ds else None


def do_thoi_luong_am_thanh(duong_dan: str) -> float:
    """Thời lượng tệp âm thanh (giây) — GIẢI MÃ thật, không tin dòng ``Duration`` của phần đầu tệp.

    Với mp3 do ffmpeg / LAME tạo (cách VieNeu / Qwen xuất giọng; có đầu Xing) dòng ``Duration`` dài hơn dữ liệu thật khoảng 0,04 s mỗi tệp (đo: 2,04 s so với 2,00 s giải mã được).
    Mốc của cả video được CỘNG từ các thời lượng này còn tiếng ghép (``ghep_doan``) cộng theo dữ liệu thật, nên 70 đoạn là hình trễ dần ~3 s so với giọng — chữ, phụ đề và phụ đề CC đều
    trễ. (mp3 của Edge không có đầu Xing nên trước đây khớp.) Không giải mã được (không có luồng âm thanh) thì dùng ``Duration``."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", duong_dan, "-vn", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
    that = _thoi_luong_that(r.stderr)
    if that is not None:
        return that
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        raise LoiDung(f"Không đọc được thời lượng âm thanh: {duong_dan}")
    return _giay(m)


_LANG_NHO: dict = {}
NGHI_TOI_THIEU = 0.25                   # khoảng lặng GIỮA lời nói ngắn hơn mức này (phụ âm tắc, ngắt hơi giữa câu) không tính là ranh giới câu


def _do_lang(duong_dan: str) -> tuple[float, float, list]:
    """(lặng đầu, lặng cuối, [(bắt đầu, kết thúc) các khoảng lặng ≥ ``NGHI_TOI_THIEU`` NẰM GIỮA lời nói]) của một tệp tiếng, một lần chạy ffmpeg (-42 dB), nhớ theo (tệp, cỡ, giờ sửa)."""
    try:
        st = os.stat(duong_dan)
    except OSError:
        return 0.0, 0.0, []
    khoa = (duong_dan, st.st_size, int(st.st_mtime))
    if khoa in _LANG_NHO:
        return _LANG_NHO[khoa]
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", duong_dan, "-af", "silencedetect=noise=-42dB:d=0.04", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    ra: tuple = (0.0, 0.0, [])
    dur = _thoi_luong_that(r.stderr)                                                   # thời lượng giải mã thật (``Duration`` của mp3 có đầu Xing dài hơn ~0,04 s — xem do_thoi_luong_am_thanh)
    if dur is None and m:
        dur = _giay(m)
    if dur is not None:
        bd =[float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr)]
        kt = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
        dau = kt[0] if (bd and kt and bd[0] <= 0.03) else 0.0
        cuoi = (dur - bd[-1]) if (bd and bd[-1] > dau + 0.05 and (len(bd) > len(kt) or kt[-1] >= dur - 0.12)) else 0.0      # 0,12: "Duration" của mp3 dài hơn dữ liệu thật ~0,05 s (đệm bộ mã hoá)
        if dau < dur - 0.1 and dau + cuoi < dur - 0.1:                             # cả tệp im lặng thì không có "khoảng nói" để tính
            het = dur - cuoi
            giua = [(round(a, 3), round(b, 3)) for a, b in zip(bd, kt) if a > dau + 0.02 and b < het - 0.02 and b - a >= NGHI_TOI_THIEU]
            ra = (round(min(1.5, max(0.0, dau)), 3), round(min(3.0, max(0.0, cuoi)), 3), giua)
    _LANG_NHO[khoa] = ra
    return ra


def do_lang_dau_cuoi(duong_dan: str) -> tuple[float, float]:
    """(lặng đầu, lặng cuối) của một tệp tiếng (giây): đoạn đầu/cuối dưới -42 dB. Giọng Edge tiếng Nhật đo được ≈ 0,20 s đầu và ≈ 0,97 s cuối; nếu coi cả tệp là
    "đang nói" thì phụ đề chia theo tỷ lệ chữ bị lệch cả nửa giây ở các câu sau. Không đọc được / cả tệp im lặng → (0, 0)."""
    ld, lc, _ = _do_lang(duong_dan)
    return ld, lc


def do_nghi_giua(duong_dan: str) -> list:
    """Các khoảng lặng ≥ 0,25 s NẰM GIỮA lời nói của tệp, [(bắt đầu, kết thúc)] tính từ đầu tệp (giây) — chỗ giọng ngắt giữa hai câu. Phụ đề đáy dùng chúng để đổi câu đúng lúc giọng sang câu mới."""
    return list(_do_lang(duong_dan)[2])


def gan_moc(c: dict, tep_tieng: Optional[str], language: str, ngon_me: str, opts: dict, cham: bool = False) -> None:
    """Điền ``visual.data._moc`` (mốc [bắt đầu, kết thúc] từng đoạn tiếng của cảnh) từ tệp ghi cạnh tiếng thật; không có thì ước lượng (và co giãn cho khớp
    độ dài tiếng nếu có tiếng nhưng thiếu tệp mốc). Không có tiếng → thời lượng cảnh theo mốc ước lượng."""
    v = c.get("visual") or {}
    dl = v.get("data")
    if v.get("type") != "reading" or not isinstance(dl, dict):
        return
    ke = B.ke_hoach(dl, opts)
    moc = None
    tieng = nghi = None
    if tep_tieng:
        try:
            with open(tep_tieng + ".moc.json", encoding="utf-8") as f:
                j = json.load(f)
            m, tg, ng = j.get("moc"), j.get("tieng"), j.get("nghi")
            if isinstance(m, list) and len(m) == len(ke):
                moc = m
                if isinstance(tg, list) and len(tg) == len(ke):                    # khoảng THỰC SỰ có tiếng của từng đoạn (bỏ lặng đầu/cuối của tệp) để phụ đề khớp giọng
                    tieng = tg
                if isinstance(ng, list) and len(ng) == len(ke):                    # các chỗ giọng ngắt giữa hai câu của đoạn có phụ đề → ranh giới câu THẬT
                    nghi = ng
        except (OSError, ValueError, AttributeError):
            moc = None
    if moc is None:
        moc = B.moc_uoc(ke, language, ngon_me, opts, cham)
        dur = c.get("duration_s")
        if tep_tieng and dur and moc[-1][1] > B.DAU_CANH:                      # có tiếng, thiếu mốc: co giãn mốc ước lượng cho vừa độ dài tiếng
            he = max(0.3, (float(dur) - B.DAU_CANH - B.CUOI_CANH) / (moc[-1][1] - B.DAU_CANH))
            moc = [[round(B.DAU_CANH + (a - B.DAU_CANH) * he, 3), round(B.DAU_CANH + (b - B.DAU_CANH) * he, 3)] for a, b in moc]
        elif not tep_tieng:
            c["duration_s"] = round(B.tong_giay(moc) + 0.2, 2)
    dl["_moc"] = moc
    for khoa_, gt in (("_tieng", tieng), ("_nghi", nghi)):
        if gt is not None:
            dl[khoa_] = gt
        else:
            dl.pop(khoa_, None)


def ghep_doan(doan: list, khe: list, ra: str, dau: float = B.DAU_CANH) -> None:
    """Nối tiếng các đoạn thành MỘT mp3: lặng mở đầu ``dau``, rồi mỗi đoạn kèm lặng ``khe[i]`` phía sau (đoạn cuối = đệm cuối cảnh).
    ``doan``: danh sách ``(tệp_mp3 | None, thời_lượng)`` — ``None`` = đoạn không có tiếng (chỉ hiện chữ), thay bằng im lặng đúng ``thời_lượng``."""
    if len(doan) != len(khe):
        raise LoiDung("Số đoạn tiếng và số khoảng lặng không khớp")
    cmd = [FFMPEG, "-y", "-loglevel", "error"]
    parts = [f"anullsrc=r=24000:cl=mono:d={dau}[s0]"]
    seq = ["[s0]"]
    n_in = 0
    for i, (tep, dur) in enumerate(doan):
        if tep:
            cmd += ["-i", tep]
            parts.append(f"[{n_in}:a]aresample=24000,aformat=sample_fmts=fltp:channel_layouts=mono[a{i}]")
            n_in += 1
        else:
            parts.append(f"anullsrc=r=24000:cl=mono:d={max(0.05, float(dur)):.3f}[a{i}]")
        parts.append(f"anullsrc=r=24000:cl=mono:d={khe[i]:.3f}[g{i}]")
        seq += [f"[a{i}]", f"[g{i}]"]
    parts.append("".join(seq) + f"concat=n={len(seq)}:v=0:a=1[out]")
    _chay(cmd + ["-filter_complex", ";".join(parts), "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "96k", ra], "ghép các đoạn tiếng")


def _chay(cmd: list, mo_ta: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0:
        raise LoiDung(f"ffmpeg lỗi khi {mo_ta}: {r.stderr[-400:]}")


def _ghep_tieng(clip: str, am_thanh: Optional[str], thoi_luong: float, out: str) -> None:
    """Gắn tiếng vào clip: tiếng thật (đệm im lặng cho đủ thời lượng) hoặc im lặng; luôn AAC 44.1k stereo để nối không lệch."""
    if am_thanh:
        nguon = ["-i", am_thanh]
        loc = ["-af", "aresample=44100,aformat=channel_layouts=stereo,apad"]
    else:
        nguon = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        loc = []
    _chay([FFMPEG, "-y", "-loglevel", "error", "-i", clip, *nguon, *loc, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "160k", "-t", f"{thoi_luong:.3f}", out], "ghép tiếng")


REV_DUNG = 33        # (28: bản dịch / phiên âm ở slide không còn bị cắt cụt ở 3 / 2 dòng · 29: slide câu CAO tách trang · 30: cuộn của trang câu cao hết RUNG — clip dựng bằng 2.15.8 nháy liên tục phải dựng lại · 31: bút viết đi theo nhịp + chỗ nghỉ THẬT của giọng, cửa sổ viết = khoảng có tiếng · 32: khối / trang CAO cũng viết chữ chính theo giọng, phiên âm + dịch hiện SAU lời đọc · 33: thẻ lời dẫn (はじめに…) viết theo giọng thật + bản dịch tiêu đề không bị cắt) tăng khi đổi cách vẽ/mã hoá làm khung hình khác đi → cảnh đã lưu bị coi là cũ


def _dau_van_tep(p: Optional[str]) -> str:
    try:
        st = os.stat(p)
        return f"{os.path.basename(p)}:{st.st_size}:{int(st.st_mtime)}"
    except (OSError, TypeError):
        return ""


def khoa_canh(canh: dict, tuy: dict, tep_tieng: Optional[str]) -> str:
    """Vân tay mọi thứ ảnh hưởng tới hình ảnh của MỘT cảnh. Trùng → dùng lại clip đã dựng (dựng tiếp sau khi hủy/sập, chỉnh một cảnh chỉ dựng lại cảnh đó)."""
    cfg_tay = os.environ.get("SUBFORGE_SANGTAO_HAND") or ""
    if (canh.get("visual") or {}).get("type") == "title":                    # thẻ mở đầu / kết thúc không phụ thuộc cách hiển thị bản dịch, furigana…
        tuy = {k: tuy.get(k) for k in ("aspect", "language", "audience")}
    dl = {"c": canh, "t": tuy, "a": _dau_van_tep(tep_tieng), "r": REV_DUNG, "h": cfg_tay}
    return hashlib.md5(json.dumps(dl, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]


def anh_xem_truoc(cd, ban_tay, out_jpg: str) -> None:
    """Ảnh đại diện của cảnh: khung gần cuối cảnh (đã viết xong chữ)."""
    import cv2
    fi = min(max(0, cd.so_khung - 10), int(cd.anh_t * 30))
    khung = CA.khung_hinh(cd, fi, ban_tay, {})
    h = 220
    w = int(khung.shape[1] * h / khung.shape[0])
    cv2.imwrite(out_jpg, cv2.cvtColor(cv2.resize(khung, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 82])


THOI_LUONG_MO_DAU = 3.2                 # thẻ im lặng (video không tiếng); thẻ CÓ tiếng dài theo lời đọc (xem :func:`the_phu_co_tieng`)
THOI_LUONG_KET_THUC = 3.2
LANG_DAU_THE = 0.35                     # giọng bắt đầu sau 0,35 s (kịp mờ vào + bút đặt xuống)
#: Lời cảm ơn mặc định của thẻ kết thúc: hai câu (cảm ơn + hẹn gặp lại) — một câu ngắn thì thẻ chỉ có một dòng chữ trơ trọi và kết video quá cụt.
LOI_CAM_ON = {"vi": "Cảm ơn bạn đã xem video. Hẹn gặp lại bạn ở những video tiếp theo!", "en": "Thank you so much for watching. See you in the next video!",
              "ja": "最後までご視聴いただき、ありがとうございました。また次の動画でお会いしましょう。", "zh-cn": "感谢您观看本视频，我们下期再见！",
              "ko": "끝까지 시청해 주셔서 감사합니다. 다음 영상에서 또 만나요!", "fr": "Merci beaucoup d'avoir regardé cette vidéo. À bientôt pour la prochaine !",
              "de": "Vielen Dank fürs Zuschauen. Bis zum nächsten Video!", "es": "Muchas gracias por ver este vídeo. ¡Nos vemos en el próximo!",
              "ru": "Спасибо, что посмотрели это видео. До встречи в следующем!", "pt": "Muito obrigado por assistir. Até o próximo vídeo!",
              "it": "Grazie mille per aver guardato il video. Alla prossima!"}


def _cat_tieu_de(text, toi_da: int = 80) -> str:
    """Giới hạn độ dài thẻ theo CHỮ THẬT (không tính cách đọc trong ngoặc ``[…]``). Trước đây cắt cứng 60 KÝ TỰ THÔ nên tiêu đề tiếng Nhật có furigana bị chặt ngang giữa
    một cụm (``…の逮``) và hiện cả dấu ngoặc. Quá dài thì bỏ furigana, cắt chữ trơn và thêm “…”."""
    text = str(text or "")
    tron = _bo_ruby(text)
    return text if len(tron) <= toi_da else tron[: toi_da - 1].rstrip() + "…"


def canh_phu(cfg: dict, id_: int) -> dict:
    """Cảnh MỞ ĐẦU (id 0) hoặc KẾT THÚC (id -1) dạng dict, cùng khuôn với cảnh thường: không lời đọc, một dòng chữ lớn viết tay + dòng phụ nhỏ (nguồn bài viết).
    ``cfg``: {text, color?, sub?}."""
    d = {"text": _cat_tieu_de(cfg.get("text")), "color": cfg.get("color") or "blue", "sub": str(cfg.get("sub") or "")[:260]}
    return {"id": id_, "voiceover": "", "duration_s": THOI_LUONG_MO_DAU if id_ == 0 else THOI_LUONG_KET_THUC, "visual": {"type": "title", "data": d}}


def the_phu_co_tieng(cfg: dict, id_: int, tep_tieng: Optional[str]) -> dict:
    """Thẻ mở đầu / kết thúc, có tiếng thì: thời lượng = lời đọc + 0,8 s (tối thiểu 2,4 s) và ghi ``noi`` (giây đọc) để tiêu đề được viết suốt lúc đọc. ``tep_tieng`` đã gồm 0,35 s lặng đầu."""
    c = canh_phu(cfg, id_)
    if tep_tieng:
        d = do_thoi_luong_am_thanh(tep_tieng)
        c["duration_s"] = round(max(2.4, d + 0.8), 2)
        c["visual"]["data"]["noi"] = round(max(0.0, d - LANG_DAU_THE), 2)
    return c


#: lời chỉ-nói dài hơn mức này (số chữ) thì tách thành từng CÂU trong phụ đề CC — một dòng 240 chữ treo 38 s trên màn hình thì không đọc nổi. Tiếng CJK đo theo ký tự nên ngưỡng thấp hơn.
CC_TACH_TREN = {"ja": 45, "zh-cn": 45, "ko": 55}
CC_TACH_TREN_MAC_DINH = 110


def _tach_cau_dai(x: dict, ngon: str) -> list[dict]:
    """Tách một dòng CC dài (lời CHỈ NÓI: chào, chuyển lượt, dẫn dắt, giảng, kết — không có bản dịch) thành từng câu, chia thời gian theo ĐÚNG cách phụ đề đáy của hình
    (:meth:`sangtao_hoc.BaiDoc._giay_noi` ước lượng thời gian nói, rồi :meth:`sangtao_hoc.BaiDoc._ranh_gioi_that` lấy ranh giới THẬT từ các chỗ giọng ngắt đo trên tệp tiếng ``x["nghi"]``
    nếu khớp ước lượng). Dòng có bản dịch (câu của bài, câu hỏi) vốn ngắn nên giữ nguyên; chỉ có MỘT câu thì giữ nguyên."""
    if x.get("me") or len(x["dich"]) <= CC_TACH_TREN.get(ngon, CC_TACH_TREN_MAC_DINH):
        return [x]
    cau = HOC.BaiDoc._chia_phu_de(x["dich"])
    if len(cau) < 2:
        return [x]
    noi = [HOC.BaiDoc._giay_noi(c_, ngon) for c_ in cau]
    tong = max(0.01, sum(noi) + HOC.BaiDoc.NGHI_GIUA_CAU * (len(cau) - 1))
    a, b = x["bd"], x["kt"]
    rong = max(0.2, b - a)
    bd, cum = [], 0.0
    for g in noi:
        bd.append(a + cum / tong * rong)
        cum += g + HOC.BaiDoc.NGHI_GIUA_CAU
    that = HOC.BaiDoc._ranh_gioi_that(bd[1:], [[p, q] for p, q in (x.get("nghi") or [])], rong)
    if that is not None:
        bd = [bd[0]] + list(that)
    ra = []
    for k, c_ in enumerate(cau):
        kt = (bd[k + 1] - 0.05) if k + 1 < len(cau) else b
        if kt - bd[k] >= 0.3:
            ra.append({"bd": round(bd[k], 3), "kt": round(kt, 3), "dich": c_, "me": ""})
    return ra or [x]


def phu_de_video(ds: list, opts: dict, mo_dau: Optional[dict] = None) -> list[dict]:
    """PHỤ ĐỀ CC của cả video: ``[{bd, kt, dich, me}]`` (giây TRONG video cuối) — ``dich`` = đúng lời người dẫn nói (tiếng đích, bỏ cách đọc), ``me`` = bản dịch tiếng mẹ đẻ nếu có.
    ``ds``: [(số_cảnh, dict_cảnh, giây_clip)] của các cảnh sẽ nối. Mốc của mỗi cảnh = (tổng độ dài các cảnh đứng trước) + mốc trong cảnh (:func:`sangtao_bai.cau_phu_de`), thứ tự theo SỐ cảnh vì
    video được nối đúng thứ tự đó (mở đầu 0 → cảnh → toàn văn N+1 → kết thúc N+2). Thẻ mở đầu / kết thúc có tiếng: lời đọc bắt đầu sau ``LANG_DAU_THE`` và dài ``noi`` giây; bản dịch của
    thẻ mở đầu lấy từ ``mo_dau["tr"]`` (nếu có). Cảnh toàn văn không có tiếng nên không có phụ đề."""
    ra: list[dict] = []
    off = 0.0
    for so, c, giay in sorted(ds, key=lambda x: x[0]):
        v = c.get("visual") or {}
        d = v.get("data") or {}
        if v.get("type") == "reading":
            for x0 in B.cau_phu_de(d, opts):
                for x in _tach_cau_dai(x0, str(opts.get("ngon_dich") or "")):
                    ra.append({"bd": round(off + x["bd"], 3), "kt": round(min(off + x["kt"], off + giay), 3), "dich": x["dich"], "me": x["me"]})
        elif v.get("type") == "title" and d.get("noi") and str(d.get("text") or "").strip():
            bd = off + LANG_DAU_THE
            ra.append({"bd": round(bd, 3), "kt": round(min(bd + float(d["noi"]) + 0.2, off + giay), 3), "dich": _bo_ruby(str(d["text"])).strip(),
                       "me": _bo_ruby(str((mo_dau or {}).get("tr") or "")).strip() if so == 0 else ""})
        off += giay
    return ra


TOI_DA_TOAN_VAN = 3600                 # quá số chữ này thì bỏ phần toàn văn cuối video: chữ phải thu nhỏ dưới 15 px mới vừa 2 trang, và video dài hàng chục phút sẽ kéo thêm nhiều phút hình tĩnh


def kich_ban_toan_van(canhs: list, opts: dict, mo_dau: Optional[dict] = None, ket_thuc: Optional[dict] = None) -> list[str]:
    """TOÀN BỘ kịch bản nói của video theo thứ tự phát: tiêu đề (thẻ mở đầu) → từng cảnh bài đọc (xem :func:`sangtao_bai.kich_ban_noi`: lời chào, câu của bài, lời chuyển, câu hỏi + đáp án,
    lời dẫn dắt, lời giảng, lời kết) → lời cảm ơn (thẻ kết thúc). ``canhs``: các cảnh ĐÃ đánh dấu đầu / cuối (:func:`sangtao_bai.danh_dau_vi_tri`)."""
    ra: list[str] = []
    if mo_dau and str(mo_dau.get("text") or "").strip():
        ra.append(str(mo_dau["text"]).strip())
    for c in canhs:
        if (c.get("visual") or {}).get("type") == "reading":
            ra += B.kich_ban_noi(c["visual"]["data"], opts)
    if ket_thuc and str(ket_thuc.get("text") or "").strip():
        ra.append(str(ket_thuc["text"]).strip())
    return ra


def canh_toan_van(cau: list, tuy: dict) -> dict:
    """Cảnh TOÀN VĂN cuối video (id -2, kiểu ``script``): dựng bố cục một lần để biết số trang → thời lượng (mỗi trang giữ theo số chữ). Không có tiếng."""
    c = {"id": -2, "voiceover": "", "duration_s": 10.0, "visual": {"type": "script", "data": {"cau": [str(x) for x in cau]}}}
    c["duration_s"] = round(CA.xay_canh(c, 0, tuy).obj.tong, 2)
    return c


def dung_video(storyboard, out_mp4: str, *, am_thanh: Optional[dict] = None, so_luong: Optional[int] = None,
               huy: Optional[threading.Event] = None, tien_do: Optional[Callable[[int, int, str], None]] = None,
               thu_muc_tam: Optional[str] = None, chi_cac_canh: Optional[list] = None, thu_muc_canh: Optional[str] = None,
               canh_xong: Optional[Callable[[int, str, str], None]] = None, bo_noi: bool = False,
               canh_bat_dau: Optional[Callable[[int], None]] = None,
               mo_dau: Optional[dict] = None, ket_thuc: Optional[dict] = None,
               ngon_me: Optional[str] = None, furigana: bool = True, phien_am: bool = True, bai: Optional[dict] = None, nguon: str = "", camera: bool = True, ten_nguon: str = "", bo_cuc: str = "trang",
               toan_van: "Optional[list | bool]" = None) -> KetQuaDung:
    """Dựng cả video. ``storyboard``: ``Storyboard`` (pydantic) hoặc dict.
    ``am_thanh``: {id_cảnh: đường_dẫn_tệp}; cảnh không có thì lấy thời lượng ước lượng và im lặng.
    ``bai``: tuỳ chọn bài đọc (xem :func:`sangtao_bai.tuy_chon`): hiển thị bản dịch / bài giảng, khoảng lặng nhắc theo. ``nguon``: dòng ghi nguồn hiện ở chân mỗi cảnh. ``camera``: bật zoom / lia nhẹ theo nội dung đang đọc. ``bo_cuc``: "trang" (danh sách hiện theo trang sách) | "cuon" (cuộn). ``ten_nguon``: tên báo, ghi dưới các câu trích dẫn.
    ``toan_van``: ``True`` → tự dựng TOÀN BỘ kịch bản nói (:func:`kich_ban_toan_van`: lời chào, bài, lời chuyển, câu hỏi, lời giảng, lời kết); hoặc danh sách câu (còn cách đọc ``[kana]``) → thêm cảnh toàn văn
    (có / không có cách đọc, chảy liền, thu nhỏ cho vừa 1–2 trang) sau cảnh cuối, trước thẻ kết thúc; đánh số N+1, thẻ kết thúc dời thành N+2.
    ``chi_cac_canh``: chỉ dựng các id này (xem thử nhanh).
    ``thu_muc_canh``: nơi LƯU LÂU clip + ảnh đại diện từng cảnh (``canh_<số>_<khoá>.mp4`` / ``.jpg``); có thì cảnh đã dựng
    được dùng lại. ``canh_xong(số_cảnh, khoá, trạng_thái)`` báo mỗi cảnh xong ("moi" | "cu"). ``bo_noi``: chỉ dựng cảnh,
    KHÔNG nối thành video (xem thử một cảnh)."""
    t0 = time.time()
    huy = huy or threading.Event()
    am_thanh = am_thanh or {}
    sb = storyboard.model_dump() if hasattr(storyboard, "model_dump") else dict(storyboard)
    canhs = sb["scenes"]
    B.danh_dau_vi_tri(canhs)                                                    # cảnh đầu / cuối của CẢ video (lời chào, lời kết), tính trước khi lọc cảnh xem thử
    opts = B.tuy_chon(bai)
    if ngon_me:
        opts["ngon_me"] = ngon_me
    opts["ngon_dich"] = sb.get("language", "vi")                                # lời dẫn nói bằng tiếng đích
    opts["ten_nguon"] = ten_nguon or opts.get("ten_nguon", "")                   # lời chào / lời kết nhắc nguồn tin
    tuy = {"aspect": sb.get("aspect", "16:9"), "language": sb.get("language", "vi"), "audience": sb.get("audience", "general"),
           "ngon_me": ngon_me or "vi", "furi": bool(furigana), "phien_am": bool(phien_am), "bai": opts, "nguon": nguon or "", "camera": bool(camera), "ten_nguon": ten_nguon or "",
           "bo_cuc": "cuon" if bo_cuc == "cuon" else "trang"}
    cham = tuy["audience"] == "kids"
    # thời lượng: tiếng thật (+ đệm ngắn) > ước lượng theo mốc đoạn tiếng
    for c in canhs:
        tep = am_thanh.get(c["id"])
        if tep:
            c["duration_s"] = round(do_thoi_luong_am_thanh(tep) + CA.DUOI, 2)
        gan_moc(c, tep, tuy["language"], tuy["ngon_me"], opts, cham)
    chon = [i for i, c in enumerate(canhs) if chi_cac_canh is None or c["id"] in chi_cac_canh]
    if not chon:
        raise LoiDung("Không có cảnh nào để dựng")
    #  Việc cần dựng: (số_cảnh, dict_cảnh). Mở đầu mang số 0, kết thúc mang số N+1 — nên video luôn nối đúng thứ tự và lưới cảnh ở giao diện đánh số khớp.
    #  Xem thử vài cảnh thì KHÔNG dựng mở đầu/kết thúc.
    viec = [(i + 1, canhs[i]) for i in chon]
    if chi_cac_canh is None and not bo_noi:
        if mo_dau:
            viec.insert(0, (0, the_phu_co_tieng(mo_dau, 0, am_thanh.get(0))))
        if ket_thuc:
            viec.append((len(canhs) + 2, the_phu_co_tieng(ket_thuc, -1, am_thanh.get(-1))))                 # N+1 dành cho toàn văn
    tam = thu_muc_tam or tempfile.mkdtemp(prefix="sangtao_")
    os.makedirs(tam, exist_ok=True)
    if thu_muc_canh:
        os.makedirs(thu_muc_canh, exist_ok=True)
    canh_bao: list = []
    try:
        bc = CA.bo_cuc(tuy["aspect"])
        ban_tay = CA.nap_ban_tay(bc.tay, bc.ow)                                  # chỉ đọc nên dùng chung giữa các luồng
        #  KHÔNG dựng sẵn mọi lớp hình của mọi cảnh: video 1 giờ có hàng trăm cảnh, giữ hết trong RAM là hàng chục GB.
        #  Mỗi luồng tự lắp cảnh của mình, dựng xong thì bỏ.
        if toan_van and chi_cac_canh is None and not bo_noi:
            cau_tv = kich_ban_toan_van(canhs, opts, mo_dau, ket_thuc) if toan_van is True else list(toan_van)
            if sum(len(_bo_ruby(x)) for x in cau_tv) > TOI_DA_TOAN_VAN:
                canh_bao.append(f"Bỏ phần toàn văn cuối video: kịch bản quá dài (trên {TOI_DA_TOAN_VAN} chữ).")
            elif cau_tv:
                viec.append((len(canhs) + 1, canh_toan_van(cau_tv, tuy)))
        ke_ds = []
        for so_, c_ in viec:
            khoa_c = khoa_canh(c_, tuy, am_thanh.get(c_["id"]))
            ke_ds.append((so_, c_, khoa_c, int(round(float(c_["duration_s"]) * 30)), float(c_["duration_s"])))
        tong_khung = sum(x[3] for x in ke_ds)
        da = [0]
        khoa = threading.Lock()

        def bao(n: int) -> None:
            with khoa:
                da[0] += n
                d = da[0]
            if tien_do and (d % 15 == 0 or n > 15):
                tien_do(d, tong_khung, "Đang dựng hình")

        def duong_canh(so: int, khoa_c: str, duoi: str) -> str:
            return os.path.join(thu_muc_canh or tam, f"canh_{so:04d}_{khoa_c}{duoi}")

        def mot_canh(so: int, c: dict, khoa_c: str, so_khung: int) -> tuple[int, str]:
            ra = duong_canh(so, khoa_c, ".mp4")
            if thu_muc_canh and os.path.exists(ra) and os.path.getsize(ra) > 0 and os.path.exists(duong_canh(so, khoa_c, ".jpg")):
                bao(so_khung)                                     # đã dựng từ lần trước, dùng lại
                if canh_xong:
                    canh_xong(so, khoa_c, "cu")
                return so, ra
            if huy.is_set():
                raise Huy()
            if canh_bat_dau:
                canh_bat_dau(so)
            cd = CA.xay_canh(c, so, tuy)
            cd.khoa = khoa_c
            canh_bao.extend(cd.canh_bao)
            clip = os.path.join(tam, f"canh_{so:04d}.mp4")
            CA.dung_clip(cd, clip, ban_tay, huy, bao, pool=pool_khung, so_doan=n_khung, tao_cd=lambda: CA.xay_canh(c, so, tuy))
            tmp_ra = ra + ".dang_dung.mp4"
            _ghep_tieng(clip, am_thanh.get(c["id"]), cd.thoi_luong, tmp_ra)
            os.remove(clip)
            try:
                anh_xem_truoc(cd, ban_tay, duong_canh(so, khoa_c, ".jpg"))
            except Exception as e:  # noqa: BLE001 — thiếu ảnh đại diện không đáng làm hỏng cả video
                canh_bao.append(f"Cảnh {so}: không tạo được ảnh đại diện ({e})")
            os.replace(tmp_ra, ra)
            if canh_xong:
                canh_xong(so, khoa_c, "moi")
            return so, ra

        # mỗi luồng giữ 1 khung (~3–6 MB) + 1 tiến trình ffmpeg; 4–6 luồng là đủ no CPU
        #  Hai tầng luồng: tầng CẢNH (ít — mỗi luồng giữ một bản lắp cảnh trong RAM + chờ) và hồ luồng VẼ KHUNG dùng chung (``n_khung`` luồng): một cảnh dài được chia thành nhiều đoạn vẽ song song
        #  (xem sangtao_canh.dung_clip). Chừa 2 nhân cho máy làm việc khác; tối đa 6 luồng vẽ (mỗi luồng giữ thêm một bản cảnh + ffmpeg riêng).
        n_khung = max(1, min((os.cpu_count() or 2) - 2, 6))
        n_luong = so_luong or max(1, min(len(ke_ds), 3))
        kq: dict = {}
        with ThreadPoolExecutor(max_workers=n_khung, thread_name_prefix="khung") as pool_khung, ThreadPoolExecutor(max_workers=n_luong) as ex:
            futs = [ex.submit(mot_canh, so_, c_, k_, n_) for (so_, c_, k_, n_, _) in ke_ds]
            try:
                for f in as_completed(futs):
                    so, ra = f.result()
                    kq[so] = ra
            except BaseException:
                huy.set()
                for f in futs:
                    f.cancel()
                raise
        if huy.is_set():
            raise Huy()
        if thu_muc_canh:                                          # dọn bản cũ của các cảnh vừa dựng (khoá khác)
            giu = {os.path.basename(duong_canh(so_, k_, d)) for (so_, _, k_, _, _) in ke_ds for d in (".mp4", ".jpg", ".nd")}
            tien_to = tuple(f"canh_{so_:04d}_" for (so_, _, _, _, _) in ke_ds)
            for ten in os.listdir(thu_muc_canh):
                if ten.startswith(tien_to) and ten not in giu:
                    try:
                        os.remove(os.path.join(thu_muc_canh, ten))
                    except OSError:
                        pass
        tong_giay = sum(x[4] for x in ke_ds)
        #  PHỤ ĐỀ CC: tính trên mốc ĐÃ DÙNG để dựng (``_moc`` / ``_tieng`` do gan_moc điền vào từng cảnh), độ dài clip theo SỐ KHUNG (video thật) để mốc không lệch dần qua hàng trăm cảnh.
        phu_de = phu_de_video([(so_, c_, n_ / 30.0) for (so_, c_, _k, n_, _g) in ke_ds], opts, mo_dau) if (chi_cac_canh is None and not bo_noi) else []
        if bo_noi:
            giay = time.time() - t0
            return KetQuaDung("", tong_giay, len(ke_ds), canh_bao, giay, tong_khung / giay if giay > 0 else 0.0)
        if tien_do:
            tien_do(tong_khung, tong_khung, "Đang nối các cảnh")
        ds = os.path.join(tam, "danh_sach.txt")
        with open(ds, "w", encoding="utf-8", newline="\n") as f:
            for so in sorted(kq):
                f.write("file '%s'\n" % kq[so].replace("\\", "/").replace("'", "'\\''"))
        os.makedirs(os.path.dirname(os.path.abspath(out_mp4)), exist_ok=True)
        tmp_out = out_mp4 + ".dang_dung.mp4"
        _chay([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", ds, "-c", "copy", "-movflags", "+faststart", tmp_out], "nối cảnh")
        os.replace(tmp_out, out_mp4)
        giay = time.time() - t0
        return KetQuaDung(out_mp4, tong_giay, len(ke_ds), canh_bao, giay, tong_khung / giay if giay > 0 else 0.0, phu_de)
    finally:
        if not thu_muc_tam:
            shutil.rmtree(tam, ignore_errors=True)
