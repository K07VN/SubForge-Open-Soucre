"""LAM VIEC THEO TUNG DOAN — lay sub / lay prompt / nhap kich ban cho MOT clip.

Vi sao co module rieng: hai che do cu lam theo CA DU AN mot luot (``run_transcription`` roi
``generate_a_chunks`` GHI DE ``a_chunks.json`` va danh so lai tu 1). O che do Edit nang cao,
nguoi dung ghep phim dan tung doan: them mot clip vao cuoi roi lay sub RIENG cho doan do,
dich rieng doan do, xong lai them doan khac. Di qua duong cu la moi lan lay sub lai xoa
sach cau A cu.

BAT BIEN SONG CON — ID CUA CAU A KHONG DUOC DOI.
Moi cau B tro ve cau A qua ``source_a_ids``. Danh so lai cau A = moi cau B tro sang cau
khac, video xuat ra doc dung tren doan sai ma khong bao gi ca (cung ho lo voi chuyen uid
cua b_chunks). Nen o day CHI CONG THEM voi id moi tu ``max(a_id) + 1``, khong bao gio sap
lai so cua cau da co.

Thoi gian: STT/OCR doc tren TEP NGUON nen tra ve giay cua tep. Vi tri tren timeline la
``clip.start + (t_nguon - clip.in) / clip.speed`` — xem ``to_timeline``. Sai buoc nay thi
cau A moi nam sai cho, keo theo cau B dich cung sai cho.
"""
from utils.i18n import L
import json
import logging
import os

from services import review_video_clips as VID
from services import review_timeline as TL
from services.gop_manh_ngan import gop_manh_ngan

#  KHONG can cat video ra tep rieng de doc sub: OCR nhan ``windows=[(dau, cuoi)]`` ngay
#  tren tep goc, con STT thi da co ``VideoService.extract_audio_slice``. Cat lai bang
#  ``-c copy`` con nguy hiem: no cat theo khung khoa nen mocs thoi gian lech theo tep.

logger = logging.getLogger("ReviewService")

A_CHUNKS_FILE = "a_chunks.json"


# --------------------------------------------------------------------------- #
#  Cau A: doc / cong them (id BEN VUNG)
# --------------------------------------------------------------------------- #
def load_a_chunks(project_dir: str) -> list:
    p = os.path.join(project_dir, A_CHUNKS_FILE)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        logger.warning(L("[nâng cao] a_chunks.json không đọc được — coi như chưa có câu nào.", "[advanced] a_chunks.json could not be read — treating it as having no sentences."))
        return []


def save_a_chunks(project_dir: str, a_chunks: list) -> None:
    p = os.path.join(project_dir, A_CHUNKS_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(a_chunks, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)


def next_a_id(a_chunks: list) -> int:
    """Id ke tiep. Lay MAX + 1 chu khong phai len(list) + 1: xoa mot cau giua roi cong
    them theo so luong se tao ra id TRUNG, hai cau A khac nhau cung mot id."""
    ids = [int(c.get("a_id") or 0) for c in a_chunks if isinstance(c, dict)]
    return (max(ids) + 1) if ids else 1


def append_a_chunks(project_dir: str, lines: list, gop: bool = True,
                    tach_cau: bool = False) -> dict:
    """Cong them cau A (``lines`` = [{start, end, text}] tinh bang giay TREN TIMELINE).

    Khong sap lai thu tu va khong doi id cua cau cu — xem phan dau tep.

    ``tach_cau``: ``lines`` den tu WHISPER (nghe / ket hop) VA o "Tach cau goc" cua du an
    dang bat -> tach doan dai theo dau cau + do dai (services/tach_cau.py). OCR thuan / sub
    chinh chu / tep tu nap KHONG tach — chung da theo tung dong hien tren hinh.
    """
    existing = load_a_chunks(project_dir)
    nid = next_a_id(existing)
    added = []
    #  Gộp mẩu ngắn vào câu kề bên ngay khi lấy sub (xem services/gop_manh_ngan.py).
    #  ``gop=False``: tệp phụ đề NGƯỜI DÙNG TỰ NẠP — giữ nguyên từng câu (người dùng chốt
    #  29-09-2026), chỉ gộp phụ đề app tự lấy (STT / OCR / sub chính chủ).
    if gop:
        lines = gop_manh_ngan(lines, L("(đoạn)", "(segment)"))
    if tach_cau:
        from services.tach_cau import tach_cau_goc
        lines = tach_cau_goc(lines, L("(đoạn)", "(segment)"), bat=True)
    for ln in lines:
        text = str((ln or {}).get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, float(ln.get("start") or 0.0))
        end = max(start + 0.05, float(ln.get("end") or start))
        added.append({
            "a_id": nid,
            # Doan them vao khong den tu raw_stt.json cua du an nen khong co segment nao
            # de tro ve. De rong thay vi bia so — cac cho khac chi dung de tra nguoc.
            "segment_ids": [],
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
        })
        nid += 1
    if added:
        save_a_chunks(project_dir, existing + added)
    logger.info(
        L(f"[nâng cao] Cộng thêm {len(added)} câu gốc cho đoạn (id "
        f"{added[0]['a_id'] if added else '-'}–{added[-1]['a_id'] if added else '-'}); "
        f"tổng {len(existing) + len(added)} câu, id câu cũ giữ nguyên.", f"[advanced] Added {len(added)} original sentences for the segment (ids {added[0]['a_id'] if added else '-'}–{added[-1]['a_id'] if added else '-'}); total {len(existing) + len(added)} sentences, old sentence ids kept.")
    )
    return {"added": len(added), "a_ids": [c["a_id"] for c in added], "total": len(existing) + len(added)}


# --------------------------------------------------------------------------- #
#  NAP PHU DE TU TEP CO SAN (.srt/.vtt/.ass) — khong chay STT/OCR
# --------------------------------------------------------------------------- #
#  Vi sao dung LAI ``download_service.parse_subtitle_file``: no da doc du .srt/.vtt/.ass,
#  da bo cue lap, da don the ``<i>``/``{\an8}`` va TRA VE DUNG dang segment ma buoc STT
#  sinh ra. Viet parser thu hai o day la chac chan co ngay hai ben hieu khac nhau mot
#  dinh dang (vd milli-giay dung dau phay hay dau cham).
SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa")


def parse_subtitle_text(content: str, filename: str = "") -> list:
    """Noi dung tep phu de -> [{start, end, text}] (giay CUA TEP PHU DE).

    ``filename`` chi de doan dinh dang (ASS khong dung "-->"). Ghi ra tep tam vi
    ``parse_subtitle_file`` nhan duong dan; doi lai duoc dung CHUNG mot parser voi
    duong tai video.
    """
    import tempfile

    from services.download_service import parse_subtitle_file

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in SUB_EXTS:
        ext = ".ass" if "[events]" in (content or "").lower() else ".srt"
    #  CHUẨN HOÁ XUỐNG DÒNG + ghi ``newline=""``: tệp .srt của Windows (và chữ dán từ ô
    #  nhập — trình duyệt gửi form multipart luôn đổi xuống dòng thành CRLF) mang "\r\n";
    #  ghi chế độ văn bản trên Windows biến nó thành "\r\r\n", đọc lại thành HAI lần
    #  xuống dòng -> mỗi khối SRT vỡ vụn, parser trả 0 câu ("Không đọc được câu nào").
    content = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return parse_subtitle_file(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def guess_language(lines: list, filename: str = "") -> str:
    """Doan NGON NGU GOC tu chinh noi dung phu de. Tra ve ma ngon ngu hoac "".

    VI SAO CAN: tep .srt khong mang thong tin ngon ngu ben trong. Khong doan thi o "Ngon
    ngu nguon" ket o "Tu nhan dien", va prompt dich in ra "ngon ngu gốc" chung chung thay
    vi "English"/"Chinese" — AI phai tu suy tu noi dung, dich kem han.

    Doan bang BANG CHU (dai ky tu Unicode) chu khong phai tu vung: chu Han, kana, hangul,
    chu Viet co dau la nhung dau hieu KHONG THE nham lan, trong khi doan tieng Anh/Phap/
    Duc bang tu thi can tu dien va van hay sai. Thu tu kiem tra co chu y:
      1. kana (Nhat)  — phai truoc chu Han, vi van ban Nhat CO chu Han (kanji) xen ke;
         xet chu Han truoc thi moi phim Nhat deu bi doan thanh tieng Trung.
      2. hangul (Han) — khong lan voi gi.
      3. chu Han      — den day thi khong con kana/hangul nua, chac chan la tieng Trung.
      4. chu Viet co dau — phan biet voi tieng Anh (deu dung chu Latin).
      5. con lai: chu Latin thuan -> "en" la phong doan hop ly nhat.

    ``filename`` chi dung de PHA HOA khi noi dung khong quyet dinh duoc: ten kieu
    "E1.en.srt" / "phim.vi.srt" mang san ma ngon ngu. KHONG uu tien hon noi dung — ten
    tep hay bi dat bua, con chu viet thi khong noi doi duoc.
    """
    import unicodedata

    mau = " ".join(str((ln or {}).get("text") or "") for ln in lines[:200])
    if not mau.strip():
        return _lang_from_filename(filename)

    dem = {"kana": 0, "hangul": 0, "han": 0, "viet": 0, "latin": 0}
    for ch in mau:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            dem["kana"] += 1
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            dem["hangul"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            dem["han"] += 1
        elif ch.isalpha() and o < 0x250:
            dem["latin"] += 1
            #  Chu Viet co dau: tach dau ra roi xem co dau nao khong. Bat duoc ca "ê"
            #  (mot ky tu) lan "ê" (e + dau roi), thu ma so sanh chuoi hay bo sot.
            if o > 0x7F and len(unicodedata.normalize("NFD", ch)) > 1:
                dem["viet"] += 1

    tong = sum(dem.values())
    if tong < 10:                      # qua it chu de ket luan
        return _lang_from_filename(filename)

    #  Nguong 2%: phu de hay xen vai chu nuoc ngoai (ten rieng, bang hieu). Doi hoi mot
    #  ty le toi thieu de vai chu lac khong lat nguoc ket luan.
    if dem["kana"] / tong > 0.02:
        return "ja"
    if dem["hangul"] / tong > 0.02:
        return "ko"
    if dem["han"] / tong > 0.02:
        return "zh"
    if dem["latin"]:
        return "vi" if dem["viet"] / dem["latin"] > 0.02 else "en"
    return _lang_from_filename(filename)


#  Ma ngon ngu HOP LE — dung chung bo voi o chon "Ngon ngu nguon" cua trinh dung.
#  Doan ra thu tieng ngoai bo nay thi thoi khong ghi con hon ghi mot ma khong ai hieu.
KNOWN_LANGS = ("vi", "en", "zh", "ja", "ko")


def _lang_from_filename(filename: str) -> str:
    """Ma ngon ngu trong ten tep kieu "E1.en.srt" / "phim.vi.srt". Rong neu khong ro."""
    parts = os.path.basename(filename or "").lower().split(".")
    for part in reversed(parts[:-1]):           # bo phan duoi (.srt)
        ma = part.split("-")[0]                  # "en-us" -> "en"
        if ma == "zh-cn" or ma == "zh":
            return "zh"
        if ma in KNOWN_LANGS:
            return ma
    return ""


def replace_all_a_chunks(project_dir: str, a_lookup: dict, b_chunks: list, lines: list,
                         gop: bool = True) -> dict:
    """THAY TOAN BO cau goc bang ``lines`` (giay TREN TIMELINE) — dung cho ca phim.

    Cung ngu nghia voi nut "Lay lai phu de" (chay STT/OCR lai tu dau), nen phai giu
    dung mot luat an toan: DONG DINH moc cua cau B truoc khi xoa cau A, khong thi cau
    binh luan chua tung keo tay mat moc va bi don xuong cuoi timeline (xem
    ``freeze_b_times``). Id cau A danh lai tu 1 — chap nhan duoc o day vi ta xoa SACH
    va da cat ``source_a_ids`` cua moi cau B, khong con id chet nao tro nham.

    Chu goi phai tu ``take_snapshot`` truoc.
    """
    cu = load_a_chunks(project_dir)
    frozen = freeze_b_times(b_chunks, a_lookup)
    for b in b_chunks:
        if isinstance(b, dict) and b.get("source_a_ids"):
            b["source_a_ids"] = []
    a_lookup.clear()

    moi = []
    nid = 1
    if gop:                      # tệp người dùng tự nạp: gop=False, giữ nguyên (xem trên)
        lines = gop_manh_ngan(lines, L("(cả phim)", "(whole film)"))
    for ln in lines:
        text = str((ln or {}).get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, float(ln.get("start") or 0.0))
        end = max(start + 0.05, float(ln.get("end") or start))
        moi.append({
            "a_id": nid,
            "segment_ids": [],
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
        })
        a_lookup[str(nid)] = moi[-1]
        nid += 1
    save_a_chunks(project_dir, moi)
    logger.info(
        L(f"[nâng cao] Nạp phụ đề từ tệp: thay {len(cu)} câu gốc cũ bằng {len(moi)} câu; "
        f"đóng đinh mốc cho {frozen} câu bình luận.", f"[advanced] Loaded subtitles from file: replaced {len(cu)} old original sentences with {len(moi)} sentences; pinned the timing of {frozen} commentary sentences.")
    )
    return {"added": len(moi), "removed": len(cu), "frozen": frozen, "total": len(moi)}


# --------------------------------------------------------------------------- #
#  Anh xa thoi gian & loc cau A theo doan
# --------------------------------------------------------------------------- #
def to_timeline(clip: dict, t_src: float) -> float:
    """Giay cua TEP NGUON -> giay TREN TIMELINE cho clip nay."""
    speed = VID.clip_speed(clip)
    t_in = VID._num(clip.get("in"))
    start = VID._num(clip.get("start"))
    return max(0.0, start + (float(t_src) - t_in) / (speed if speed > 0 else 1.0))


def clip_range(clip: dict) -> tuple:
    """Khoang [dau, cuoi] tren timeline ma clip nay chiem."""
    start = VID._num(clip.get("start"))
    return (start, start + max(0.05, VID.visible_duration(clip)))


def a_ids_in_clip(a_chunks: list, clip: dict) -> list:
    """Cau A nam trong khoang cua clip — de lam prompt RIENG cho doan do.

    Tinh theo DIEM GIUA cau: cau nam vat ngang mep clip thi thuoc ve ben chua phan lon
    noi dung, khong bi dem hai lan cho hai doan lien nhau.
    """
    lo, hi = clip_range(clip)
    out = []
    for c in a_chunks:
        if not isinstance(c, dict):
            continue
        s = float(c.get("start") or 0.0)
        e = float(c.get("end") or s)
        mid = (s + e) / 2.0
        if lo - 0.001 <= mid < hi + 0.001:
            out.append(c)
    return out


# --------------------------------------------------------------------------- #
#  XOA CAU GOC — khi doan phim khong con nua
# --------------------------------------------------------------------------- #
def _mid(c: dict) -> float:
    s = float(c.get("start") or 0.0)
    return (s + float(c.get("end") or s)) / 2.0


def a_chunks_in_range(a_chunks: list, lo: float, hi: float) -> list:
    """Cau goc nam trong khoang [lo, hi) — tinh theo DIEM GIUA nhu ``a_ids_in_clip``."""
    return [
        c for c in a_chunks
        if isinstance(c, dict) and lo - 0.001 <= _mid(c) < hi + 0.001
    ]


def orphan_a_chunks(a_chunks: list, meta: dict) -> list:
    """Cau goc KHONG CON HINH nam duoi.

    Xoa het clip video ma dai phu de goc van con nguyen thi nhung cau do treo lo lung:
    khong con khung hinh nao de doi chieu, khong nghe lai duoc, ma van chiem cho tren
    timeline va van chui vao prompt dich. Day la ham tim chung.

    CHI tinh clip DANG CHIEM CHO tren mach phim (``occupies_timeline``): clip tat tam
    hay dang chong lan thi khong phat va khong vao ban xuat — coi nhu khong co.
    """
    khoang = []
    for c in VID.list_clips(meta):
        if not VID.occupies_timeline(meta, c.get("id")):
            continue
        khoang.append(clip_range(c))
    if not khoang:
        # Khong con clip nao -> MOI cau goc deu mo coi.
        return [c for c in a_chunks if isinstance(c, dict)]
    return [
        c for c in a_chunks
        if isinstance(c, dict)
        and not any(lo - 0.001 <= _mid(c) < hi + 0.001 for lo, hi in khoang)
    ]


def b_chunks_in_range(b_chunks: list, a_lookup: dict, lo: float, hi: float) -> list:
    """Cau BINH LUAN nam trong khoang [lo, hi) — tinh theo DIEM GIUA nhu cau goc."""
    from services.review_render_cache import resolve_b_time

    out = []
    for b in b_chunks:
        if not isinstance(b, dict):
            continue
        s, e = resolve_b_time(b, a_lookup or {})
        if s is None:
            continue
        mid = (float(s) + float(e)) / 2.0
        if lo - 0.001 <= mid < hi + 0.001:
            out.append(b)
    return out


def freeze_b_times(b_chunks: list, a_lookup: dict) -> int:
    """Dong dinh moc thoi gian cua cau B dang phai SUY RA tu cau A. Tra ve so cau da dong.

    BAT BUOC goi truoc khi xoa cau A. ``resolve_b_time`` uu tien ``start_override`` roi
    moi roi ve ``a_lookup[source_a_ids]``; cau B chua tung duoc keo tay thi khong co
    override nao, moc cua no HOAN TOAN muon tu cau A. Xoa cau A ma khong dong dinh
    truoc la cau B do mat moc -> ``place_orphan_chunks`` xep no noi duoi cung timeline.
    Tuc la xoa phu de goc lai lam loi binh nhay lung tung, khong mot dong canh bao.
    """
    from services.review_render_cache import resolve_b_time

    n = 0
    for b in b_chunks:
        if not isinstance(b, dict):
            continue
        #  Mép phải do bước sinh giọng TỰ DÃN mà "mép cũ" là ``None`` (= suy từ câu gốc):
        #  sắp cắt liên kết về câu gốc thì phải ghi mép gốc ra SỐ, không thì "Đặt lại" gỡ
        #  ``end_override`` xong câu chỉ còn điểm đầu -> mất mốc, rơi khỏi bản xuất. Làm cho
        #  CẢ câu đã đủ hai override (câu chỉ có start_override rồi được dãn mép phải) — bỏ
        #  qua câu đó trước bước này là sót (rà soát 29-09-2026).
        if TL.END_AUTO_FIELD in b and b.get(TL.END_AUTO_FIELD) is None:
            tam = {k: v for k, v in b.items() if k != "end_override"}
            _s0, goc = resolve_b_time(tam, a_lookup or {})
            if goc is None:
                b.pop(TL.END_AUTO_FIELD, None)
            else:
                b[TL.END_AUTO_FIELD] = round(float(goc), 3)
        if b.get("start_override") is not None and b.get("end_override") is not None:
            continue
        start, end = resolve_b_time(b, a_lookup or {})
        if start is None:
            continue
        b["start_override"] = round(float(start), 3)
        b["end_override"] = round(float(end), 3)
        n += 1
    return n


def delete_a_chunks(project_dir: str, a_lookup: dict, b_chunks: list, doomed: list) -> dict:
    """Xoa han cac cau goc trong ``doomed`` khoi a_chunks.json.

    Dong dinh moc cho cau B TRUOC (xem ``freeze_b_times``) roi moi xoa, va cat luon
    ``source_a_ids`` tro toi cau da mat — de lai id chet thi lan sau "Lay lai phu de"
    danh so lai la cau B tu dung sang mot cau goc HOAN TOAN khac.

    Chu goi phai tu ``take_snapshot`` truoc: ham nay khong tu chup hoan tac.
    """
    ids = {int(c.get("a_id")) for c in doomed if isinstance(c, dict) and c.get("a_id") is not None}
    if not ids:
        return {"deleted": 0, "frozen": 0, "unlinked": 0}

    frozen = freeze_b_times(b_chunks, a_lookup)

    unlinked = 0
    for b in b_chunks:
        if not isinstance(b, dict):
            continue
        src = list(b.get("source_a_ids") or [])
        con = [i for i in src if int(i) not in ids]
        if len(con) != len(src):
            b["source_a_ids"] = con
            unlinked += 1

    con_lai = [
        c for c in load_a_chunks(project_dir)
        if not (isinstance(c, dict) and c.get("a_id") is not None and int(c["a_id"]) in ids)
    ]
    save_a_chunks(project_dir, con_lai)
    for i in ids:
        a_lookup.pop(str(i), None)

    logger.info(
        L(f"[nâng cao] Xoá {len(ids)} câu gốc; đóng đinh mốc cho {frozen} câu bình luận, "
        f"gỡ liên kết ở {unlinked} câu. Còn lại {len(con_lai)} câu gốc.", f"[advanced] Deleted {len(ids)} original sentences; pinned the timing of {frozen} commentary sentences, unlinked {unlinked} sentences. {len(con_lai)} original sentences remain.")
    )
    return {"deleted": len(ids), "frozen": frozen, "unlinked": unlinked}


# --------------------------------------------------------------------------- #
#  Kich ban B: CONG THEM (khong dung tay vao cau dang co)
# --------------------------------------------------------------------------- #
def append_script(project_dir: str, meta: dict, a_lookup: dict, raw_chunks: list,
                  tach_cau: bool = None) -> dict:
    """Cong them cau B cho MOT doan, giu nguyen moi cau B dang co.

    Khac ``ADV.import_script`` (no THAY CA danh sach): o day nguoi dung dich rieng tung
    doan nen cau moi phai cong vao, khong duoc dung den doan da lam xong — giong da sinh
    va vi tri da keo cua chung phai con nguyen.

    Moi cau moi nhan ``uid`` MOI: uid la chia khoa cua thu muc audio (``output/{uid}``),
    dung lai uid cu la cau moi thua huong giong cu.

    ``tach_cau``: tach khoi ban dich dai theo dau cau + do dai — nhu ``ADV.import_script``.
    None = theo o "Tach ban dich" cua du an (metadata, mac dinh TAT).
    """
    from services.review_advanced_engine import (
        commit_chunks, place_orphan_chunks, speakers_in_use, take_snapshot,
        VOICE_CAST_FIELD,
    )
    from services.tach_cau import bat_dich, tach_kich_ban

    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise ValueError(L("Kịch bản trống hoặc không phải danh sách câu.", "The script is empty or is not a list of sentences."))
    raw_chunks, so_tach = tach_kich_ban(
        raw_chunks, a_lookup, bat=bat_dich(meta) if tach_cau is None else tach_cau
    )

    existing = TL.load_b_chunks(project_dir)
    have = {(b.get("uid") or "") for b in existing}
    added = []
    for item in raw_chunks:
        if not isinstance(item, dict):
            continue
        text = str(
            item.get("b_text") or item.get("text") or item.get("target_text")
            or item.get("content") or ""
        ).strip()
        if not text:
            continue
        speaker = item.get("speaker") or item.get("character") or item.get("role")
        uid = TL.new_uid()
        while uid in have:              # gan nhu khong bao gio, nhung tuyet doi khong trung
            uid = TL.new_uid()
        have.add(uid)
        chunk = {
            "uid": uid,
            "b_text": text,
            "source_a_ids": list(item.get("source_a_ids") or []),
            "voice": item.get("voice"),
            "rate": item.get("rate"),
            "is_trimmed": None,
        }
        if speaker:
            chunk["speaker"] = str(speaker).strip()
        if "start_override" in item:           # mảnh vừa tách: khung chia theo độ dài chữ
            chunk["start_override"] = item["start_override"]
            chunk["end_override"] = item["end_override"]
        added.append(chunk)

    if not added:
        raise ValueError(L("Không đọc được câu nào trong kịch bản của đoạn này.", "Could not read any sentence in this segment's script."))

    merged = existing + added
    # Cau cua doan nay khong tro duoc ve cau A nao (AI quen ``source_a_ids``, hoac id da
    # doi) thi phai co cho THAT — de nguyen la no nam o giay 0 tren timeline ma bi bo hoan
    # toan khoi ban xuat, khong bao gi ca. Xem ADV.place_orphan_chunks.
    place_orphan_chunks(merged, a_lookup)

    take_snapshot(project_dir)
    ordered = commit_chunks(project_dir, merged, a_lookup, snapshot_first=False)

    state = meta.setdefault("state", {})
    state["b_imported"] = True
    state["render_complete"] = False

    cast = meta.get(VOICE_CAST_FIELD) or {}
    roles = speakers_in_use(ordered, meta)
    unassigned = [r["speaker"] for r in roles if not r["voice"]]
    logger.info(
        L(f"[nâng cao] Cộng thêm {len(added)} câu cho đoạn — giữ nguyên {len(existing)} câu "
        f"đã có. Tổng {len(ordered)} câu.", f"[advanced] Added {len(added)} sentences for the segment — keeping the {len(existing)} existing sentences. Total {len(ordered)} sentences.")
        + (L(f" Đã tách {so_tach} khối dài theo câu.", f" Split {so_tach} long blocks by sentence.") if so_tach else "")
        + (L(f" Chưa chọn giọng cho: {', '.join(unassigned)}", f" No voice chosen for: {', '.join(unassigned)}") if unassigned else "")
    )
    return {
        "added": len(added),
        "split": so_tach,
        "kept": len(existing),
        "total": len(ordered),
        "speakers": roles,
        "unassigned_speakers": unassigned,
        "cast_size": len(cast) if isinstance(cast, dict) else 0,
    }
