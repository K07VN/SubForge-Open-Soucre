"""PHIÊN ÂM tự động cho video học ngoại ngữ (Nhật / Trung / Hàn): dùng khi AI KHÔNG ghi phiên âm, để lượt đọc vẫn có dòng phiên âm.

- Nhật: chuyển KANA → romaji Hepburn (tự viết, không phụ thuộc thư viện). Chữ Hán (kanji) cần từ điển nên KHÔNG đoán — để trống thay vì sai.
- Trung: pinyin có dấu thanh qua ``pypinyin`` nếu máy có cài (từ điển chuẩn, xử lý chữ đa âm theo cụm); không có thì để trống.
- Hàn: La-tinh hoá chuẩn (Revised Romanization) TỰ VIẾT, có các quy tắc phát âm chính (nối âm, mũi hoá, bật hơi…) — không dùng thư viện
  (``korean-romanizer`` là GPLv3 nên không đóng gói).
- Anh và các tiếng khác: không tự sinh (cách đọc IPA phải do người/AI cung cấp và kiểm).

Mọi hàm trả ``None`` khi không chắc — không bao giờ bịa phiên âm. Romaji tự động chỉ dùng cho TỪ ĐƠN LẺ: câu tiếng Nhật cần tách từ và đọc đúng trợ từ
(は → wa), việc đó phải do AI / người ghi.
"""
from __future__ import annotations

import re
from typing import Optional

_HIRA = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o", "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko", "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to", "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no", "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo", "や": "ya", "ゆ": "yu", "よ": "yo", "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro", "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go", "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo", "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo", "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po", "ゔ": "vu",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
}
_NHO = {"ゃ": "ya", "ゅ": "yu", "ょ": "yo"}                       # chữ nhỏ ghép sau âm i: きゃ → kya
_GHEP_DAC_BIET = {"し": "sh", "ち": "ch", "じ": "j", "ぢ": "j"}   # しゃ → sha, ちゅ → chu, じょ → jo
_NGUYEN_AM = "aiueo"
_KANJI = re.compile(r"[一-鿿㐀-䶿々〆ヵヶ]")


def _kata_sang_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def kana_sang_romaji(van_ban: str) -> Optional[str]:
    """Hiragana/katakana → romaji Hepburn (です → desu, ラーメン → raamen, きょう → kyou). Có chữ Hán → ``None`` (không đoán)."""
    if not van_ban or _KANJI.search(van_ban):
        return None
    s = _kata_sang_hira(van_ban)
    s = re.sub(r"(こんにち|こんばん)は", lambda m: m.group(1) + "わ", s)       # lời chào: は đọc "wa". Câu có trợ từ は/へ khác phải do AI ghi (cần tách từ)
    ra: list[str] = []
    gap_doi = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "っ":
            gap_doi = True
            i += 1
            continue
        if c == "ー":
            if ra and ra[-1] and ra[-1][-1] in _NGUYEN_AM:
                ra[-1] += ra[-1][-1]
            i += 1
            continue
        if c == "ん":
            nxt = _HIRA.get(s[i + 1], "") if i + 1 < len(s) else ""
            ra.append("n'" if nxt[:1] in ("a", "i", "u", "e", "o", "y") and nxt else "n")
            i += 1
            continue
        if c in _HIRA:
            r = _HIRA[c]
            if i + 1 < len(s) and s[i + 1] in _NHO:                    # âm ghép: き + ゃ → kya
                nho = _NHO[s[i + 1]]
                r = (_GHEP_DAC_BIET[c] + nho[1:]) if c in _GHEP_DAC_BIET else r[:-1] + nho
                i += 1
            if gap_doi:                                                # っ: gấp đôi phụ âm đầu (chi → tchi)
                r = ("t" + r) if r.startswith("ch") else (r[0] + r)
                gap_doi = False
            ra.append(r)
            i += 1
            continue
        if c in "、，":
            ra.append(",")
        elif c in "。．":
            ra.append(".")
        elif c in "！":
            ra.append("!")
        elif c in "？":
            ra.append("?")
        elif c in " 　":
            ra.append(" ")
        elif c.isascii():
            ra.append(c)
        else:
            return None                                                # ký tự lạ: không chắc → bỏ
        i += 1
    kq = re.sub(r"\s+", " ", "".join(ra)).strip()
    kq = re.sub(r" ([,.!?])", r"\1", kq)
    return kq or None


def pinyin(van_ban: str, tu_don: bool = False) -> Optional[str]:
    """Pinyin có dấu thanh. Cần gói ``pypinyin`` (MIT); không có → ``None``. Từ đơn lẻ viết LIỀN (苹果 → píngguǒ); câu thì cách từng âm tiết (pypinyin không tách từ)."""
    if not van_ban or not re.search(r"[一-鿿]", van_ban):
        return None
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return None
    tu = lazy_pinyin(van_ban, style=Style.TONE)
    kq = ("" if tu_don else " ").join(t for t in tu if t.strip())
    kq = re.sub(r" ([，。！？,.!?、])", r"\1", kq).translate({ord("，"): ",", ord("、"): ",", ord("。"): ".", ord("！"): "!", ord("？"): "?"})
    return re.sub(r"([,.!?])(?=[^\s,.!?])", r"\1 ", kq).strip() or None


# ---------------------------------------------------------------- La-tinh hoá chữ Hàn (Revised Romanization) — TỰ VIẾT, không dùng thư viện
_DAU = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_GIUA = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_CUOI = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
_RR_DAU = dict(zip(_DAU, ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]))
_RR_GIUA = dict(zip(_GIUA, ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]))
_RR_CUOI = {"ㄱ": "k", "ㄴ": "n", "ㄷ": "t", "ㄹ": "l", "ㅁ": "m", "ㅂ": "p", "ㅇ": "ng"}
#: phụ âm cuối đứng ĐỘC LẬP (trước phụ âm / hết từ): rút gọn về một trong 7 âm
_CUOI_DON = {"ㄱ": "ㄱ", "ㄲ": "ㄱ", "ㄳ": "ㄱ", "ㄴ": "ㄴ", "ㄵ": "ㄴ", "ㄶ": "ㄴ", "ㄷ": "ㄷ", "ㄹ": "ㄹ", "ㄺ": "ㄱ", "ㄻ": "ㅁ", "ㄼ": "ㄹ", "ㄽ": "ㄹ", "ㄾ": "ㄹ", "ㄿ": "ㅂ",
             "ㅀ": "ㄹ", "ㅁ": "ㅁ", "ㅂ": "ㅂ", "ㅄ": "ㅂ", "ㅅ": "ㄷ", "ㅆ": "ㄷ", "ㅇ": "ㅇ", "ㅈ": "ㄷ", "ㅊ": "ㄷ", "ㅋ": "ㄱ", "ㅌ": "ㄷ", "ㅍ": "ㅂ", "ㅎ": "ㄷ"}
#: phụ âm cuối KÉP khi vần sau bắt đầu bằng nguyên âm: (phần ở lại làm phụ âm cuối, phần chuyển sang làm phụ âm đầu; "" = câm)
_TACH_KEP = {"ㄳ": ("ㄱ", "ㅅ"), "ㄵ": ("ㄴ", "ㅈ"), "ㄶ": ("ㄴ", ""), "ㄺ": ("ㄹ", "ㄱ"), "ㄻ": ("ㄹ", "ㅁ"), "ㄼ": ("ㄹ", "ㅂ"), "ㄽ": ("ㄹ", "ㅅ"), "ㄾ": ("ㄹ", "ㅌ"),
             "ㄿ": ("ㄹ", "ㅍ"), "ㅀ": ("ㄹ", ""), "ㅄ": ("ㅂ", "ㅅ")}
_MUI = {"ㄱ": "ㅇ", "ㄷ": "ㄴ", "ㅂ": "ㅁ"}                                       # mũi hoá: ㄱ/ㄷ/ㅂ + ㄴ/ㅁ → ㅇ/ㄴ/ㅁ
_BAT_HOI = {"ㄱ": "ㅋ", "ㄷ": "ㅌ", "ㅂ": "ㅍ", "ㅈ": "ㅊ"}                       # bật hơi với ㅎ


def _tach_am_tiet(c: str):
    """Âm tiết Hàn → [đầu, giữa, cuối] (jamo); không phải âm tiết → None."""
    n = ord(c) - 0xAC00
    if not 0 <= n < 11172:
        return None
    return [_DAU[n // 588], _GIUA[(n % 588) // 28], _CUOI[n % 28]]


def _quy_tac_cap(a: list, b: list) -> None:
    """Áp quy tắc phát âm cho MỘT cặp âm tiết liền nhau (sửa trực tiếp a, b)."""
    cuoi, dau = a[2], b[0]
    if not cuoi:
        return
    if dau == "ㅇ":                                                              # NỐI ÂM
        if cuoi == "ㅇ":
            return
        if cuoi in _TACH_KEP:
            a[2], chuyen = _TACH_KEP[cuoi]
            b[0] = chuyen or "ㅇ"
        elif cuoi == "ㅎ":
            a[2] = ""                                                            # ㅎ cuối + nguyên âm: câm
        else:
            a[2], b[0] = "", cuoi
            if b[1] == "ㅣ" and cuoi in ("ㄷ", "ㅌ"):                             # 구개음화: 같이 → gachi
                b[0] = "ㅈ" if cuoi == "ㄷ" else "ㅊ"
        return
    if dau == "ㅎ":                                                              # BẬT HƠI: ㄱ/ㄷ/ㅂ/ㅈ + ㅎ
        if cuoi in ("ㄺ", "ㄼ", "ㄿ"):
            a[2], b[0] = "ㄹ", {"ㄺ": "ㅋ", "ㄼ": "ㅍ", "ㄿ": "ㅍ"}[cuoi]
            return
        nhap = _BAT_HOI.get(cuoi if cuoi == "ㅈ" else _CUOI_DON.get(cuoi, cuoi))
        if nhap and cuoi not in ("ㄴ", "ㄹ", "ㅁ", "ㅇ"):
            a[2], b[0] = "", nhap
            return
    if cuoi in ("ㅎ", "ㄶ", "ㅀ") and dau in ("ㄱ", "ㄷ", "ㅈ"):                  # ㅎ + ㄱ/ㄷ/ㅈ → ㅋ/ㅌ/ㅊ
        a[2] = {"ㅎ": "", "ㄶ": "ㄴ", "ㅀ": "ㄹ"}[cuoi]
        b[0] = {"ㄱ": "ㅋ", "ㄷ": "ㅌ", "ㅈ": "ㅊ"}[dau]
        return
    don = _CUOI_DON.get(cuoi, cuoi)
    if don in _MUI and dau in ("ㄴ", "ㅁ"):                                       # MŨI HOÁ
        don = _MUI[don]
    elif don in ("ㅁ", "ㅇ") and dau == "ㄹ":                                     # ㅁ/ㅇ + ㄹ → ㄴ
        b[0] = "ㄴ"
    elif don in ("ㄱ", "ㅂ") and dau == "ㄹ":
        don, b[0] = _MUI[don], "ㄴ"
    elif don == "ㄴ" and dau == "ㄹ":                                            # ㄴ + ㄹ → ㄹㄹ
        don = "ㄹ"
    elif don == "ㄹ" and dau == "ㄴ":
        b[0] = "ㄹ"
    a[2] = don


def _romaja_tu(tu: str) -> str:
    """Một TỪ (không có dấu cách): áp quy tắc phát âm giữa các âm tiết rồi La-tinh hoá."""
    am = [_tach_am_tiet(c) or c for c in tu]
    for i in range(len(am) - 1):
        if isinstance(am[i], list) and isinstance(am[i + 1], list):
            _quy_tac_cap(am[i], am[i + 1])
    ra: list[str] = []
    for x in am:
        if not isinstance(x, list):
            ra.append(x)
            continue
        dau, giua, cuoi = x
        cuoi_don = _CUOI_DON.get(cuoi, cuoi) if cuoi else ""
        dau_rr = "l" if (dau == "ㄹ" and ra and ra[-1].endswith("l")) else _RR_DAU[dau]     # ㄹㄹ → ll
        ra.append(dau_rr + _RR_GIUA[giua] + _RR_CUOI.get(cuoi_don, ""))
    return "".join(ra)


def han_romaja(van_ban: str) -> Optional[str]:
    """La-tinh hoá chữ Hàn theo Revised Romanization có quy tắc phát âm chính (사과 → sagwa, 감사합니다 → gamsahamnida). Quy tắc áp trong từng TỪ (tách bởi
    dấu cách); không có chữ Hàn → ``None``. Tự viết, không dùng thư viện ngoài (``korean-romanizer`` là GPLv3 nên không đóng gói)."""
    if not van_ban or not re.search(r"[\uac00-\ud7a3]", van_ban):
        return None
    ra = []
    for tu in van_ban.split():
        m = re.match(r"^([^\w\uac00-\ud7a3]*)(.*?)([^\w\uac00-\ud7a3]*)$", tu, re.S)
        ra.append(m.group(1) + _romaja_tu(m.group(2)) + m.group(3))
    return " ".join(ra) or None


def tu_dong(ngon_ngu: str, van_ban: str, doc: Optional[str] = None, tu_don: bool = False) -> Optional[str]:
    """Phiên âm cho ``van_ban`` bằng ``ngon_ngu`` (ja | zh-cn | ko). ``doc``: cách đọc bằng kana do AI ghi (Nhật có chữ Hán) → đổi sang romaji.
    ``tu_don``: đây là MỘT từ (thẻ từ vựng) — pinyin viết liền."""
    if ngon_ngu == "ja":
        return kana_sang_romaji(doc) if doc else kana_sang_romaji(van_ban)
    if ngon_ngu == "zh-cn":
        return pinyin(van_ban, tu_don)
    if ngon_ngu == "ko":
        return han_romaja(van_ban)
    return None
