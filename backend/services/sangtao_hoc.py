"""HÌNH ẢNH của video đọc báo học ngoại ngữ: BÀI ĐỌC (danh sách tự cuộn, có phân vai bằng avatar) và THẺ TIÊU ĐỀ (mở đầu / kết thúc).

Mỗi lớp tự vẽ từng khung theo THỜI GIAN TUYỆT ĐỐI ``t`` (giây từ đầu cảnh) và báo vị trí ngòi bút cho bàn tay đi theo; chữ được "viết" dần
(quét từ trái sang phải) đúng lúc đoạn tiếng tương ứng được đọc. Mốc đọc lấy từ :mod:`sangtao_bai` (đoạn tiếng + mốc [bắt đầu, kết thúc]).

Cảnh có thể phát nhiều LƯỢT (đọc · có bản dịch · bài giảng · nhại theo): mỗi lượt viết lại từ đầu. Camera: lớp nội dung báo mục tiêu zoom (``cam_muc``), lớp tĩnh (tiêu đề, nhãn
lượt, ghi nguồn) vẽ SAU camera nên không bị cắt khi zoom.
"""
from __future__ import annotations

import itertools
import math
import re
import threading
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw

from services import sangtao_avatar as AV
from services import sangtao_bai as B
from services import sangtao_chu as CH
from services import sangtao_reveal as RV
from services import sangtao_ruby as RB

MUC = (46, 46, 58)
XAM = (150, 154, 168)
SLATE = (84, 94, 124)             # bản dịch
PHO = (116, 128, 164)             # phiên âm
DAM = [(36, 92, 190), (206, 66, 60), (30, 132, 86), (128, 80, 190)]        # màu từ vựng / mẫu ngữ pháp nổi bật
MAU_TIEU_DE = {"blue": (28, 50, 110), "green": (28, 100, 62), "red": (150, 35, 35), "black": (30, 30, 34)}

#: nhãn trên màn hình theo NGÔN NGỮ ĐÍCH (ngôn ngữ đang học): mọi chữ trong video đều bằng tiếng đích, chỉ BẢN DỊCH / nghĩa / giải thích mới bằng tiếng mẹ đẻ
NHAN = {
    "vi": {"trich": "Trích dẫn từ {site}", "lan": "Lần {n}", "doc": "Đọc", "dich": "Có bản dịch", "giang": "Từ vựng & ngữ pháp", "shadow": "Shadowing", "nhac": "Nhắc lại theo", "vocab": "Từ vựng", "gram": "Ngữ pháp", "loi_mo": "Giới thiệu", "loi_ket": "Lời kết", "hoi": "Câu hỏi", "dap_an": "Đáp án", "nghi": "Hãy suy nghĩ…", "toan": "Toàn văn", "toan_furi": "Toàn văn · có cách đọc", "toan_tron": "Toàn văn · không có cách đọc", "tam_dung": "Bấm tạm dừng video để đọc lại"},
    "en": {"trich": "Quoted from {site}", "lan": "Round {n}", "doc": "Listen", "dich": "With translation", "giang": "Vocab & grammar", "shadow": "Shadowing", "nhac": "Repeat after me", "vocab": "Vocabulary", "gram": "Grammar", "loi_mo": "Welcome", "loi_ket": "Wrap-up", "hoi": "Quiz", "dap_an": "Answer", "nghi": "Think…", "toan": "Full text", "toan_furi": "Full text · with readings", "toan_tron": "Full text · without readings", "tam_dung": "Pause the video to re-read"},
    "ja": {"trich": "引用元：{site}", "lan": "{n}回目", "doc": "聞く", "dich": "翻訳あり", "giang": "語彙と文法", "shadow": "シャドーイング", "nhac": "続けて言おう", "vocab": "語彙", "gram": "文法", "loi_mo": "はじめに", "loi_ket": "おわりに", "hoi": "クイズ", "dap_an": "答え", "nghi": "考えてみよう", "toan": "全文", "toan_furi": "全文・ふりがなあり", "toan_tron": "全文・ふりがななし", "tam_dung": "一時停止して読み返せます"},
    "zh-cn": {"trich": "引自{site}", "lan": "第{n}遍", "doc": "听读", "dich": "带翻译", "giang": "词汇语法", "shadow": "跟读", "nhac": "跟读", "vocab": "词汇", "gram": "语法", "loi_mo": "开场", "loi_ket": "结语", "hoi": "提问", "dap_an": "答案", "nghi": "想一想", "toan": "全文", "toan_furi": "全文·带注音", "toan_tron": "全文·无注音", "tam_dung": "可暂停视频重读"},
    "ko": {"trich": "출처: {site}", "lan": "{n}회차", "doc": "듣기", "dich": "번역 포함", "giang": "어휘·문법", "shadow": "쉐도잉", "nhac": "따라 말하기", "vocab": "어휘", "gram": "문법", "loi_mo": "시작하며", "loi_ket": "마무리", "hoi": "퀴즈", "dap_an": "정답", "nghi": "생각해 보세요", "toan": "전문", "toan_furi": "전문·읽는 법 포함", "toan_tron": "전문·읽는 법 없음", "tam_dung": "영상을 멈추고 다시 읽어 보세요"},
    "fr": {"trich": "Cité depuis {site}", "lan": "Écoute {n}", "doc": "Écouter", "dich": "Avec traduction", "giang": "Vocabulaire et grammaire", "shadow": "Shadowing", "nhac": "Répétez après moi", "vocab": "Vocabulaire", "gram": "Grammaire", "loi_mo": "Introduction", "loi_ket": "Conclusion", "hoi": "Question", "dap_an": "Réponse", "nghi": "Réfléchissez…", "toan": "Texte intégral", "toan_furi": "Texte intégral · avec lectures", "toan_tron": "Texte intégral · sans lectures", "tam_dung": "Mettez la vidéo en pause pour relire"},
    "de": {"trich": "Zitat aus {site}", "lan": "Durchgang {n}", "doc": "Hören", "dich": "Mit Übersetzung", "giang": "Wortschatz & Grammatik", "shadow": "Shadowing", "nhac": "Sprich nach", "vocab": "Wortschatz", "gram": "Grammatik", "loi_mo": "Einführung", "loi_ket": "Abschluss", "hoi": "Frage", "dap_an": "Antwort", "nghi": "Denk nach…", "toan": "Volltext", "toan_furi": "Volltext · mit Lesehilfe", "toan_tron": "Volltext · ohne Lesehilfe", "tam_dung": "Video pausieren, um nachzulesen"},
    "es": {"trich": "Cita de {site}", "lan": "Vuelta {n}", "doc": "Escuchar", "dich": "Con traducción", "giang": "Vocabulario y gramática", "shadow": "Shadowing", "nhac": "Repite conmigo", "vocab": "Vocabulario", "gram": "Gramática", "loi_mo": "Introducción", "loi_ket": "Conclusión", "hoi": "Pregunta", "dap_an": "Respuesta", "nghi": "Piensa…", "toan": "Texto completo", "toan_furi": "Texto completo · con lecturas", "toan_tron": "Texto completo · sin lecturas", "tam_dung": "Pausa el vídeo para releer"},
    "pt": {"trich": "Citação de {site}", "lan": "Rodada {n}", "doc": "Ouvir", "dich": "Com tradução", "giang": "Vocabulário e gramática", "shadow": "Shadowing", "nhac": "Repita comigo", "vocab": "Vocabulário", "gram": "Gramática", "loi_mo": "Introdução", "loi_ket": "Conclusão", "hoi": "Pergunta", "dap_an": "Resposta", "nghi": "Pense…", "toan": "Texto completo", "toan_furi": "Texto completo · com leituras", "toan_tron": "Texto completo · sem leituras", "tam_dung": "Pause o vídeo para reler"},
    "it": {"trich": "Citazione da {site}", "lan": "Ascolto {n}", "doc": "Ascolta", "dich": "Con traduzione", "giang": "Vocabolario e grammatica", "shadow": "Shadowing", "nhac": "Ripeti con me", "vocab": "Vocabolario", "gram": "Grammatica", "loi_mo": "Introduzione", "loi_ket": "Conclusione", "hoi": "Domanda", "dap_an": "Risposta", "nghi": "Pensaci…", "toan": "Testo completo", "toan_furi": "Testo completo · con letture", "toan_tron": "Testo completo · senza letture", "tam_dung": "Metti in pausa il video per rileggere"},
    "ru": {"trich": "Цитата из {site}", "lan": "Прослушивание {n}", "doc": "Слушать", "dich": "С переводом", "giang": "Лексика и грамматика", "shadow": "Шэдоуинг", "nhac": "Повторяйте за мной", "vocab": "Лексика", "gram": "Грамматика", "loi_mo": "Введение", "loi_ket": "Заключение", "hoi": "Вопрос", "dap_an": "Ответ", "nghi": "Подумайте…", "toan": "Полный текст", "toan_furi": "Полный текст · с чтением", "toan_tron": "Полный текст · без чтения", "tam_dung": "Поставьте видео на паузу, чтобы перечитать"},
}


def mem(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def doan(p: float, a: float, b: float) -> float:
    """Tiến độ cục bộ của đoạn [a, b] trong p."""
    return min(1.0, max(0.0, (p - a) / (b - a))) if b > a else (1.0 if p >= a else 0.0)


#: ký tự KHÔNG đọc ra tiếng (dấu câu, ngoặc) — cùng danh sách với ``BaiDoc._giay_noi``
_KHONG_DOC = set("。、，,.!?！？…・「」『』（）()“”\"'‘’《》：:；;—–-～~[]{}|")
_KANA_NHO = set("ゃゅょぁぃぅぇぉャュョァィゥェォ")


def _nhip_ky_tu(ch: str, ja: bool) -> float:
    """Nhịp (mora) của một ký tự khi được ĐỌC, cùng luật ước lượng với ``BaiDoc._giay_noi``: dấu câu / khoảng trắng = 0; tiếng Nhật: kana 1, kana nhỏ 0, chữ Hán / chữ số trơ trọi ≈ 2;
    ngôn ngữ khác: 1 / ký tự."""
    if ch.isspace() or ch in _KHONG_DOC or (ja and ch in _KANA_NHO):
        return 0.0
    return 2.0 if (ja and ("一" <= ch <= "鿿" or ch == "々" or ch.isdigit())) else 1.0


def _la_cho_ngat(ch: str, tiep: str) -> bool:
    """Ký tự là CHỖ NGẮT của lời nói (giọng nghỉ sau nó)? ``、。，！？；…：`` luôn là; ``, . ; :`` ASCII chỉ khi đứng cuối từ (sau nó là khoảng trắng / hết dòng / ngoặc đóng) —
    không tính số thập phân "3.5", "1,000"."""
    if ch in "、。，！？!?；…：":
        return True
    return ch in ",.;:" and (tiep == "" or tiep.isspace() or tiep in "」』”’\"')）")


def _co_so(ft) -> int:
    """Khoảng cách từ đỉnh dòng tới ĐƯỜNG CƠ SỞ của font (ascent) — để căn hai chữ cỡ khác nhau theo cùng một đường cơ sở."""
    try:
        return int(ft.getmetrics()[0])
    except Exception:  # noqa: BLE001
        return int(getattr(ft, "size", 20) * 0.8)


def _cat_dau(van: str, n: int) -> str:
    """Lấy ~``n`` chữ ĐẦU của ``van`` (còn cách đọc ``[kana]``), không làm đứt cụm chữ Hán + cách đọc, thêm "…" — dòng tóm tắt câu gốc ở các trang từ vựng / ngữ pháp khi câu quá cao."""
    ra, dem = [], 0
    for base, ru in RB.phan_tich(van):
        if dem + len(base) > n:
            if not ru:
                ra.append(base[: max(0, n - dem)])
            break
        ra.append(f"{base}[{ru}]" if ru else base)
        dem += len(base)
    return "".join(ra).rstrip("、。，, 「") + "…"


#: Số dòng TỐI ĐA cho chữ HIỂN THỊ ĐỦ (bản dịch, phiên âm, giải thích…) — chỉ là lưới an toàn chống chữ dài vô hạn, KHÔNG phải cách vừa khung (vừa khung = co chữ / tách trang / cuộn).
DONG_DAY_DU = 40


class Chu:
    """Một khối chữ đã dựng sẵn (canh giữa / trái / phải), hiện mờ dần hoặc "viết" quét từ trái sang phải."""

    def __init__(self, text: str, font_path: str, co: float, mau: tuple, cx: float, cy: float, rong: float, can: str = "giua", dong_toi_da: int = 2):
        ft = CH.font(font_path, max(8, int(co)))
        self._ft = ft                                                              # để đo vị trí từng ký tự khi bút đi theo giọng đọc (don_vi_but)
        dong = CH.ngat_dong(text, ft, rong)[:dong_toi_da] or [text]
        cao_dong = int(co * 1.3)
        wmax = int(max(CH.do_rong(ft, d) for d in dong)) + 8
        im = Image.new("L", (wmax, cao_dong * len(dong) + 6), 0)
        dr = ImageDraw.Draw(im)
        for i, d in enumerate(dong):
            w = CH.do_rong(ft, d)
            x = (wmax - w) / 2 if can == "giua" else 4 if can == "trai" else wmax - w - 4
            dr.text((x, i * cao_dong), d, font=ft, fill=255)
        self.al = np.asarray(im, np.float32) / 255.0
        self.h, self.w = self.al.shape
        ax = cx - self.w / 2 if can == "giua" else cx if can == "trai" else cx - self.w
        self.x0, self.y0 = int(ax), int(cy - self.h / 2)
        self.mau = mau
        self.dong = dong
        self.base = _co_so(ft)
        self._dat_hd([(i * cao_dong, (wmax - CH.do_rong(ft, d)) / 2 if can == "giua" else 4 if can == "trai" else wmax - CH.do_rong(ft, d) - 4, CH.do_rong(ft, d)) for i, d in enumerate(dong)])

    def _dat_hd(self, hd: list) -> None:
        """Hình học từng dòng để VIẾT TỪNG DÒNG MỘT. ``hd``: [(y đầu dòng, x đầu chữ, bề rộng chữ)] theo toạ độ ảnh chữ; dòng ``i`` chiếm các hàng từ y đầu của nó tới y đầu của dòng kế
        (dòng cuối: tới hết ảnh). Lưu thành [(y0, y1, x0, rộng)]."""
        self.hd = [(int(y), int(hd[i + 1][0]) if i + 1 < len(hd) else self.h, int(x), max(1.0, float(w))) for i, (y, x, w) in enumerate(hd)]

    def _vet(self) -> None:
        """Dựng (một lần, khi cần) bản đồ THỜI ĐIỂM HIỆN của từng điểm ảnh để chữ được "viết" như bằng bút thật chứ không quét ngang: chữ viết lần lượt từng nét liền (từng chữ cái / bộ
        thủ) theo thứ tự dòng rồi từ trái sang phải; trong mỗi nét liền, mực chảy dần từ điểm cao-trái nhất dọc theo hình nét (khoảng cách đi trong nét, không phải khoảng cách thẳng).
        Thời gian của mỗi nét tỷ lệ với cỡ nét nên tốc độ bút gần như đều. Cũng lưu quỹ đạo ngòi bút (``_tip_t`` / ``_tip_xy``) để bàn tay đi theo nét đang viết."""
        if getattr(self, "_T", None) is not None:
            return
        al = self.al
        T = np.full(al.shape, 2.0, np.float32)
        ink = (al > 0.12).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
        if n <= 1:
            self._ket_key = self._ket_q = None
            self._T = T
            self._tip_t = np.array([0.0, 1.0])
            self._tip_xy = np.array([[self.w / 2.0, self.h / 2.0]] * 2)
            return
        ys, xs = np.nonzero(ink)
        ln = lab[ys, xs]
        thu_tu = np.lexsort((xs, ys, ln))                                              # mỗi nét liền: điểm cao nhất (rồi trái nhất) làm nơi bắt đầu
        _, dau = np.unique(ln[thu_tu], return_index=True)
        sy, sx = ys[thu_tu][dau], xs[thu_tu][dau]
        dist = np.full(al.shape, -1, np.int16)
        mat = np.zeros_like(ink)
        mat[sy, sx] = 1
        dist[sy, sx] = 0
        da_toi = mat.copy()
        kern = np.ones((3, 3), np.uint8)
        for it in range(1, 400):                                                      # mực chảy theo nét: mỗi vòng lan thêm 1 điểm ảnh, chỉ trong phần có mực
            moi = cv2.dilate(mat, kern) & ink & (1 - da_toi)
            if not moi.any():
                break
            dist[moi > 0] = it
            da_toi |= moi
            mat = moi
        con = ink > 0
        lab_c = lab[con]
        dmax = np.zeros(n, np.float32)
        np.maximum.at(dmax, lab_c, dist[con].astype(np.float32))
        local = dist[con].astype(np.float32) / np.maximum(1.0, dmax[lab_c])           # 0..1 trong từng nét
        # thứ tự + thời lượng từng nét: theo dòng, rồi từ trái sang phải; nét lớn mất nhiều thời gian hơn
        cx = st[:, cv2.CC_STAT_LEFT] + st[:, cv2.CC_STAT_WIDTH] / 2.0
        cy = st[:, cv2.CC_STAT_TOP] + st[:, cv2.CC_STAT_HEIGHT] / 2.0
        dong_cua = np.zeros(n, np.int32)
        for i, (y0, y1, _x, _w) in enumerate(self.hd):
            dong_cua[(cy >= y0) & (cy < y1)] = i
        dong_cua[cy >= self.hd[-1][1]] = len(self.hd) - 1
        thu = [l_ for l_ in np.lexsort((cx, dong_cua)) if l_ != 0]
        nang = (st[:, cv2.CC_STAT_WIDTH] + st[:, cv2.CC_STAT_HEIGHT]) / 2.0 + 6.0
        tong = float(sum(nang[l_] for l_ in thu))
        bat_dau = np.zeros(n, np.float32)
        keo_dai = np.zeros(n, np.float32)
        acc = 0.0
        for l_ in thu:
            bat_dau[l_] = acc / tong
            keo_dai[l_] = nang[l_] / tong
            acc += nang[l_]
        self._ket_key = np.array([dong_cua[l_] * 1.0e6 + cx[l_] for l_ in thu], np.float64)             # (dòng, x giữa nét) theo THỨ TỰ VIẾT — để biết bút xong tới đâu khi tới một vị trí chữ
        self._ket_q = np.array([bat_dau[l_] + keo_dai[l_] for l_ in thu], np.float64)                  # tiến độ viết (quet) lúc xong từng nét
        T[con] = bat_dau[lab_c] + keo_dai[lab_c] * local
        # viền mờ (khử răng cưa) lấy thời điểm của điểm có mực gần nhất
        tm = T.copy()
        for _ in range(2):
            tm = cv2.erode(tm, kern)
        vien = (al > 0) & ~con
        T[vien] = tm[vien]
        self._T = T
        # quỹ đạo ngòi bút: K điểm trên mỗi nét (vị trí trung bình của mực mới chảy tới)
        K = 8
        ngan = np.minimum((local * K).astype(np.int32), K - 1)
        yy, xx = np.nonzero(con)
        tong_x = np.zeros((n, K), np.float64)
        tong_y = np.zeros((n, K), np.float64)
        dem = np.zeros((n, K), np.float64)
        np.add.at(tong_x, (lab_c, ngan), xx)
        np.add.at(tong_y, (lab_c, ngan), yy)
        np.add.at(dem, (lab_c, ngan), 1.0)
        tt, pts = [], []
        for l_ in thu:
            for k in range(K):
                if dem[l_, k] > 0:
                    tt.append(bat_dau[l_] + keo_dai[l_] * (k + 0.5) / K)
                    pts.append((tong_x[l_, k] / dem[l_, k], tong_y[l_, k] / dem[l_, k]))
        self._tip_t = np.array([0.0] + tt + [1.0])
        self._tip_xy = np.array([pts[0]] + pts + [pts[-1]])

    def _mat_na(self, quet: float) -> np.ndarray:
        """Mặt nạ phần đã viết khi tiến độ là ``quet`` (0..1) — viết theo NÉT."""
        self._vet()
        return (self._T <= quet).astype(np.float32)

    def _mat_ngang(self, quet: float) -> np.ndarray:
        """Mặt nạ kiểu CHẠY DẦN: quét từ trái sang phải, xong dòng trên mới xuống dòng dưới, mép đầu mờ nhẹ (~24 điểm ảnh). Dùng cho phần phụ (phiên âm, bản dịch, nghĩa) chạy theo lời đọc."""
        con = max(0.0, quet) * sum(h[3] for h in self.hd)
        m = np.zeros((self.h, self.w), np.float32)
        for y0, y1, x0, w in self.hd:
            f = min(1.0, con / w)
            if f > 0:
                xe = x0 + max(1.0, f * w)
                dau = max(0, int(xe) - 24)
                m[y0:y1, :dau] = 1.0
                if f >= 1.0:
                    m[y0:y1, dau:] = 1.0
                else:
                    xs = np.arange(dau, min(self.w, int(xe) + 1), dtype=np.float32)
                    m[y0:y1, dau:dau + len(xs)] = np.clip((xe - xs) / 24.0, 0.0, 1.0)[None, :]
            if f < 1.0:
                break
            con -= w
        return m

    def ve(self, world: np.ndarray, a: float = 1.0, quet: Optional[float] = None, ngang: bool = False) -> None:
        """Vẽ khối chữ. ``quet`` (0..1) = tiến độ hiện dần: mặc định viết theo NÉT (chữ chính); ``ngang=True`` = chạy dần từ trái sang phải (phần phụ: phiên âm, bản dịch…)."""
        if a <= 0 or (quet is not None and quet <= 0):
            return
        if quet is not None and quet < 1.0:                                           # đang hiện dần: mặt nạ đổi từng khung
            al = self.al * (self._mat_ngang(quet) if ngang else self._mat_na(quet))
            RV.tron(world, self.mau, al, self.x0, self.y0, min(1.0, a))
        else:                                                                         # đã hiện đủ (hoặc chỉ mờ dần): giữ mảng gốc → lớp trộn được lưu đệm
            RV.tron(world, self.mau, self.al, self.x0, self.y0, min(1.0, a))

    def dau_but(self, quet: float) -> tuple[float, float]:
        """Vị trí ngòi bút khi đã viết ``quet`` (0..1) của cả khối: ở đầu nét đang được viết (đi theo hình nét, nhảy sang chữ kế khi xong một nét)."""
        self._vet()
        q = max(0.0, min(1.0, quet))
        return self.x0 + float(np.interp(q, self._tip_t, self._tip_xy[:, 0])), self.y0 + float(np.interp(q, self._tip_t, self._tip_xy[:, 1]))

    def quet_den(self, dong: int, x_phai: float) -> float:
        """Tiến độ viết (``quet``, 0..1) lúc bút VIẾT XONG mọi nét nằm bên trái ``x_phai`` (toạ độ trong khối chữ) ở dòng ``dong`` và các dòng trên nó (nét viết theo dòng rồi từ trái sang phải — xem :meth:`_vet`)."""
        self._vet()
        key = getattr(self, "_ket_key", None)
        if key is None or len(key) == 0:
            return 1.0
        n = int(np.searchsorted(key, dong * 1.0e6 + x_phai, side="right"))
        return 0.0 if n == 0 else float(self._ket_q[n - 1])

    def don_vi_but(self, ja: bool) -> Optional[list]:
        """Các ĐƠN VỊ chữ theo thứ tự viết, để bút đi cùng GIỌNG ĐỌC: [(nhịp, là chỗ ngắt, dòng, x phải của đơn vị)]. ``None`` nếu khối chữ không biết hình học từng ký tự (ChuHon)."""
        ft = getattr(self, "_ft", None)
        if ft is None:
            return None
        ra = []
        for i, d in enumerate(self.dong):
            x0 = self.hd[i][2]
            for k, ch in enumerate(d):
                ra.append((_nhip_ky_tu(ch, ja), _la_cho_ngat(ch, d[k + 1] if k + 1 < len(d) else ""), i, x0 + CH.do_rong(ft, d[: k + 1])))
        return ra


class ChuRuby(Chu):
    """Khối chữ có FURIGANA: mỗi cụm chữ Hán có cách đọc kana nhỏ ghi phía trên (canh giữa theo cụm), ngắt dòng theo từng cụm. Cùng giao diện với :class:`Chu`."""

    def __init__(self, text: str, font_path: str, co: float, mau: tuple, cx: float, cy: float, rong: float, can: str = "giua", dong_toi_da: int = 2):
        ft = CH.font(font_path, max(8, int(co)))
        co_r = max(8, int(co * 0.42))
        fr = CH.font(font_path, co_r)
        tok = []                                                                  # (chữ, cách đọc, rộng chữ, rộng cả cụm)
        for base, ru in RB.phan_tich(text):
            if ru:
                wb, wr = CH.do_rong(ft, base), CH.do_rong(fr, ru)
                tok.append((base, ru, wb, max(wb, wr + 2)))
            else:
                for m in RB.tach_thuong(base):
                    w = CH.do_rong(ft, m)
                    tok.append((m, None, w, w))
        dong, cur, w_cur = [], [], 0.0
        for t in tok:
            if cur and w_cur + t[3] > rong and len(dong) < dong_toi_da - 1:
                if t[0] not in CH.KHONG_DAU_DONG:
                    dong.append(cur)
                    cur, w_cur = [], 0.0
                elif t[0] not in CH.KHONG_DAU_DONG_CHUOI and len(cur) > 1:
                    # ký tự không được đứng đầu dòng và KHÔNG "treo" được (ー, ゃ, っ…): đẩy cả ký tự đứng trước xuống dòng mới cùng nó (oidashi). Trước đây giữ nó ở lại dòng cũ nên dòng
                    # dài hơn bề rộng cho phép và chữ cuối bị cắt ở mép phải khung (toàn văn: 「…このニュー」). Dấu câu 。、」 vẫn treo ra lề.
                    truoc = cur.pop()
                    dong.append(cur)
                    cur, w_cur = [truoc], truoc[3]
            cur.append(t)
            w_cur += t[3]
        if cur:
            dong.append(cur)
        self._tok_dong = dong                                                     # để bút đi theo giọng đọc (don_vi_but)
        h_r = int(co_r * 1.2)
        cao_dong = int(co * 1.3) + h_r
        tong_w = [sum(x[3] for x in d_) for d_ in dong]
        wmax = int(max(tong_w or [1])) + 8
        im = Image.new("L", (wmax, cao_dong * len(dong) + 6), 0)
        dr = ImageDraw.Draw(im)
        for i, d_ in enumerate(dong):
            x = (wmax - tong_w[i]) / 2 if can == "giua" else 4 if can == "trai" else wmax - tong_w[i] - 4
            y0 = i * cao_dong
            for base, ru, wb, wt in d_:
                if ru:
                    wr = CH.do_rong(fr, ru)
                    dr.text((x + (wt - wr) / 2, y0), ru, font=fr, fill=255)
                dr.text((x + (wt - wb) / 2, y0 + h_r), base, font=ft, fill=255)
                x += wt
        self.al = np.asarray(im, np.float32) / 255.0
        self.h, self.w = self.al.shape
        ax = cx - self.w / 2 if can == "giua" else cx if can == "trai" else cx - self.w
        self.x0, self.y0 = int(ax), int(cy - self.h / 2)
        self.mau = mau
        self.dong = ["".join(x[0] for x in d_) for d_ in dong]
        self.base = h_r + _co_so(ft)                                                  # cách viết furigana nằm TRÊN dòng chữ: đường cơ sở nằm dưới hàng furigana
        self._dat_hd([(i * cao_dong, (wmax - tong_w[i]) / 2 if can == "giua" else 4 if can == "trai" else wmax - tong_w[i] - 4, tong_w[i]) for i in range(len(dong))])

    def don_vi_but(self, ja: bool) -> Optional[list]:
        """Như :meth:`Chu.don_vi_but`; một cụm chữ Hán có cách đọc là MỘT đơn vị, nhịp = số kana (không tính kana nhỏ) của cách đọc."""
        ra = []
        for i, d_ in enumerate(self._tok_dong):
            x = float(self.hd[i][2])
            for base, ru, _wb, wt in d_:
                if ru and ja:
                    ra.append((float(sum(1 for ch in ru if not ch.isspace() and ch not in _KANA_NHO)), False, i, x + wt))
                else:
                    n = len(base)
                    for k, ch in enumerate(base):
                        ra.append((_nhip_ky_tu(ch, ja), _la_cho_ngat(ch, base[k + 1] if k + 1 < n else ""), i, x + wt * (k + 1) / n))
                x += wt
        return ra


class ChuHon(Chu):
    """Khối chữ PHA HAI THỨ TIẾNG trong một dòng — vd lời giải thích tiếng Việt có chèn ví dụ tiếng Nhật: font chính (tiếng Việt) thiếu chữ Hán nên các ký tự thiếu được vẽ bằng
    font phụ. Ngắt dòng theo từ (chữ Latin) / theo ký tự (CJK). Cùng giao diện với :class:`Chu`."""

    def __init__(self, text: str, f1: str, f2: str, co: float, mau: tuple, cx: float, cy: float, rong: float, can: str = "giua", dong_toi_da: int = 2):
        ft1, ft2 = CH.font(f1, max(8, int(co))), CH.font(f2, max(8, int(co)))
        thieu1 = set(CH.thieu_glyph(f1, text))

        def fnt(c: str):
            return ft2 if c in thieu1 else ft1

        def rong_ky_tu(c: str) -> float:
            return CH.do_rong(fnt(c), c)

        tok: list[str] = []                                                       # từ Latin (kèm khoảng trắng đứng sau) hoặc một ký tự CJK
        cur = ""
        for c in text:
            if CH.la_cjk(c):
                if cur:
                    tok.append(cur)
                    cur = ""
                tok.append(c)
            else:
                cur += c
                if c == " ":
                    tok.append(cur)
                    cur = ""
        if cur:
            tok.append(cur)
        dong: list[str] = []
        l_cur, w_cur = "", 0.0
        for t_ in tok:
            wt = sum(rong_ky_tu(c) for c in t_)
            if l_cur and w_cur + wt > rong and t_ not in CH.KHONG_DAU_DONG and len(dong) < dong_toi_da - 1:
                dong.append(l_cur.rstrip())
                l_cur, w_cur = "", 0.0
            l_cur += t_
            w_cur += wt
        if l_cur:
            dong.append(l_cur.rstrip())
        dong = dong or [text]
        cao_dong = int(co * 1.3)
        rongs = [sum(rong_ky_tu(c) for c in d_) for d_ in dong]
        wmax = int(max(rongs)) + 8
        im = Image.new("L", (wmax, cao_dong * len(dong) + 6), 0)
        dr = ImageDraw.Draw(im)
        for i, d_ in enumerate(dong):
            x = (wmax - rongs[i]) / 2 if can == "giua" else 4 if can == "trai" else wmax - rongs[i] - 4
            run, f_run = "", None
            for c in d_ + "\0":
                f_c = fnt(c) if c != "\0" else None
                if run and f_c is not f_run:
                    dr.text((x, i * cao_dong), run, font=f_run, fill=255)
                    x += CH.do_rong(f_run, run)
                    run = ""
                run += c if c != "\0" else ""
                f_run = f_c
        self.al = np.asarray(im, np.float32) / 255.0
        self.h, self.w = self.al.shape
        ax = cx - self.w / 2 if can == "giua" else cx if can == "trai" else cx - self.w
        self.x0, self.y0 = int(ax), int(cy - self.h / 2)
        self.mau = mau
        self.dong = dong
        self.base = _co_so(ft1)
        self._dat_hd([(i * cao_dong, (wmax - rongs[i]) / 2 if can == "giua" else 4 if can == "trai" else wmax - rongs[i] - 4, rongs[i]) for i in range(len(dong))])


def _hop_bo_goc(w: int, h: int, r: int, le: int = 20) -> np.ndarray:
    """Mặt nạ (float 0..1) hình chữ nhật bo góc, có lề ``le`` px mỗi phía."""
    m = np.zeros((h + 2 * le, w + 2 * le), np.uint8)
    r = max(2, min(r, h // 2, w // 2))
    cv2.rectangle(m, (le + r, le), (le + w - r, le + h), 255, -1)
    cv2.rectangle(m, (le, le + r), (le + w, le + h - r), 255, -1)
    for cx_, cy_ in ((le + r, le + r), (le + w - r, le + r), (le + r, le + h - r), (le + w - r, le + h - r)):
        cv2.circle(m, (cx_, cy_), r, 255, -1, cv2.LINE_AA)
    return m.astype(np.float32) / 255.0


class _Xep:
    """Các khối chữ xếp DỌC (mỗi dòng văn bản một :class:`Chu` riêng) — cho khối ghi nguồn nhiều dòng (tên + ngày đăng / link). Cùng giao diện ``h`` + ``ve`` như một khối chữ.
    Không dùng ký tự xuống dòng trong MỘT khối: khối có font phụ (:class:`ChuHon`, vd tên nguồn "NHKニュース" lẫn trong câu tiếng Việt) không đo được chữ nhiều dòng."""

    def __init__(self, chus: list, gian: int = 4):
        self.chus = chus
        self.gian = gian
        self.h = int(sum(c.h for c in chus) + gian * max(0, len(chus) - 1))

    def dat_top(self, y: float) -> None:
        for c in self.chus:
            c.y0 = int(y)
            y += c.h + self.gian

    def ve(self, world: np.ndarray, a: float = 1.0, quet: Optional[float] = None) -> None:
        for c in self.chus:
            c.ve(world, a, quet)


class _Hoc:
    """Nền chung: kích thước, ngôn ngữ, dựng chữ theo font đúng ngôn ngữ, nhãn giao diện."""

    def __init__(self, bc, ngon: str, tuy: dict):
        self.W, self.H = int(bc.ow), int(bc.oh)
        self.doc = bool(bc.doc)
        self.ngon = ngon
        self.me = tuy.get("ngon_me") or "vi"
        self.furi = bool(tuy.get("furi", True))
        self.co = 1.12 if tuy.get("audience") == "kids" else 1.0                  # trẻ em: chữ to hơn
        self.tip: Optional[tuple] = None                                         # ngòi bút (toạ độ "thế giới", trước camera)
        self.tip_tinh: Optional[tuple] = None                                    # ngòi bút của lớp tĩnh (toạ độ màn hình, sau camera)
        self.cam_muc: Optional[tuple] = None                                     # camera mong muốn (zoom, tâm x, tâm y) — bộ dựng làm mượt rồi áp
        self.cam_on = tuy.get("camera", True) is not False
        self.dai_tren = 0                                                         # chiều cao dải trên cùng được phủ lại nền giấy (nội dung trôi vào đó thì mờ hẳn)
        self.dai_mem = 34                                                         # độ dài vùng chuyển mờ của dải đó
        self.dai_zoom = 0                                                         # dải phủ DÀY HƠN khi camera đang zoom (nội dung bị đẩy lên dưới tiêu đề): chiều cao + vùng mờ
        self.dai_zoom_mem = 34
        self.opts = tuy.get("bai") or {}
        self.ten_nguon = str(tuy.get("ten_nguon") or "")

    def chu(self, text, co, mau, cx, cy, rong, can="giua", dong=2, font=None) -> Chu:
        f = font or self._font_cho(self.ngon, RB.bo_ruby(text), "subtitle")
        if RB.co_ruby(text):
            if self.furi:                                                        # chữ Hán + cách đọc kana nhỏ ở trên
                return ChuRuby(text, f, co * self.co, mau, cx, cy, rong, can, dong)
            text = RB.bo_ruby(text)                                              # tắt furigana: chỉ còn chữ trơn
        elif "[" in text:
            text = RB.bo_ruby(text)
        thieu = CH.thieu_glyph(f, text)
        if thieu:                                                                  # font chính thiếu chữ (vd chữ Hán trong lời giải thích tiếng Việt) → font phụ cho riêng các chữ đó
            for lg in (self.ngon, "ja", "zh-cn", "ko", "en"):
                f2, con_thieu = CH.chon_font("subtitle", lg, "".join(thieu))
                if f2 and not con_thieu and f2 != f:
                    return ChuHon(text, f, f2, co * self.co, mau, cx, cy, rong, can, dong)
        return Chu(text, f, co * self.co, mau, cx, cy, rong, can, dong)

    def _chu_vua(self, van: str, co: float, mau: tuple, rong: float, font, dong_ok: int, co_san: float, dong_toi_da: int) -> Chu:
        """Khối chữ phụ (phiên âm / bản dịch) HIỆN ĐỦ: đo số dòng thật; nhiều hơn ``dong_ok`` thì thu nhỏ chữ dần (không nhỏ hơn ``co_san``). Chỉ khi vẫn quá ``dong_toi_da`` dòng (rất hiếm)
        mới cắt — trước đây cắt cứng ở 2 dòng nên câu dài bị chặt giữa chữ (vd phiên âm "…Toukyou shijou d")."""
        c = self.chu(van, co, mau, 0, 0, rong, "trai", 40, font)
        while len(c.dong) > dong_ok and co > co_san:
            co = max(co_san, co * 0.92)
            c = self.chu(van, co, mau, 0, 0, rong, "trai", 40, font)
        if len(c.dong) > dong_toi_da:
            c = self.chu(van, co, mau, 0, 0, rong, "trai", dong_toi_da, font)
        return c

    @staticmethod
    def _vua_mot_dong(van: str, font: str, co: float, rong: float, san: float) -> tuple[str, float]:
        """Cho ``van`` VỪA MỘT dòng rộng ``rong``: thu nhỏ cỡ chữ dần tới ``san``; vẫn dài thì CẮT cho vừa và thêm "…" (chữ Latin lùi tới ranh giới từ). Trả (chữ, cỡ)."""
        while co > san and CH.do_rong(CH.font(font, int(co)), van) > rong:
            co -= 2
        ft = CH.font(font, int(co))
        if CH.do_rong(ft, van) <= rong:
            return van, co
        cat = van
        while len(cat) > 1 and CH.do_rong(ft, cat.rstrip() + "…") > rong:
            cat = cat[:-1]
        if " " in cat and van[len(cat):len(cat) + 1] not in ("", " ") and cat.rfind(" ") >= 0.6 * len(cat):
            cat = cat[:cat.rfind(" ")]                                              # chữ có dấu cách: không để đứt giữa từ
        return cat.rstrip(" ,.;:、。・-–—") + "…", co

    def chu_nhieu_dong(self, text: str, co: float, mau: tuple, cx: float, y_top: float, rong: float, can: str = "giua", font=None, gian: int = 4) -> "_Xep":
        """Mỗi dòng của ``text`` (tách bằng ``\n``) là một khối chữ riêng (đúng font, có font phụ nếu thiếu glyph), xếp dọc từ ``y_top``."""
        chus = [self.chu(d, co, mau, cx, 0, rong, can, 2, font) for d in str(text).split("\n") if d.strip()]
        x = _Xep(chus, gian)
        x.dat_top(y_top)
        return x

    def _font_cho(self, ngon: str, text: str, vai_tro: str = "subtitle", du_phong: tuple = ()) -> str:
        """Font cho MỘT đoạn chữ theo đúng ngôn ngữ của đoạn đó (font CJK thường thiếu dấu Việt → ô vuông). Chọn font đầu tiên đủ glyph,
        không có thì font thiếu ít nhất."""
        tot, it_thieu = None, 10 ** 9
        for lg, vt in ((ngon, vai_tro), *du_phong):
            p, thieu = CH.chon_font(vt, lg, text)
            if p and not thieu:
                return p
            if p and len(thieu) < it_thieu:
                tot, it_thieu = p, len(thieu)
        if not tot:
            raise RuntimeError("Không tìm thấy font nào cho chữ trong cảnh")
        return tot

    def _font_me(self, text: str) -> str:
        """Font cho chữ TIẾNG MẸ ĐẺ (bản dịch, nghĩa, giải thích)."""
        return self._font_cho(self.me, text or "a", "subtitle", du_phong=(("vi", "subtitle"), ("en", "subtitle")))

    def _font_nhan(self, text: str, vai_tro: str = "label") -> str:
        """Font cho NHÃN trên màn hình — bằng tiếng ĐÍCH (xem :data:`NHAN`); thiếu glyph thì font tiếng Việt / Anh."""
        return self._font_cho(self.ngon, text or "a", vai_tro, du_phong=(("vi", vai_tro), ("en", vai_tro)))

    def nhan(self, khoa: str, **kw) -> str:
        """Nhãn trên màn hình bằng tiếng ĐÍCH (mọi chữ trong video là tiếng đích; chỉ bản dịch / nghĩa / giải thích là tiếng mẹ đẻ)."""
        return NHAN.get(self.ngon, NHAN["en"])[khoa].format(**kw)

    def ngoac(self, text: str) -> str:
        """Đặt câu TRÍCH DẪN trong ngoặc kép đúng kiểu từng ngôn ngữ (「」 cho tiếng Nhật)."""
        mo, dong = ("「", "」") if self.ngon == "ja" else ("“", "”")
        return mo + text.strip().strip("\"'“”„‟«»「」『』").strip() + dong

    def nhan_trich(self) -> str:
        """Nhãn nhỏ dưới câu trích dẫn: "Trích dẫn từ <tên báo>" (không có tên báo thì chỉ "Trích dẫn")."""
        ten = self.ten_nguon
        return self.nhan("trich", site=ten) if ten else self.nhan("trich", site="").replace("  ", " ").strip(" :：")

    # --- cửa sổ viết chữ theo mốc đoạn tiếng
    def cua_so(self, moc: list, j: int) -> tuple[float, float]:
        """(bắt đầu, độ dài) cửa sổ VIẾT CHỮ của đoạn ``j``. Có mốc tiếng THẬT (``tieng``: khoảng giọng thực sự nói, bỏ lặng đầu / cuối tệp) thì cửa sổ = đúng khoảng đó: bút bắt đầu khi giọng bắt
        đầu và xong khi giọng hết, KHÔNG còn trần thời gian (trước: 0,92 × đoạn, tối đa 16 s → câu đọc 22 s bị viết xong sớm hơn giọng 6 s). Chưa có thì ước lượng theo mốc: gần bằng thời gian
        đọc, không lấn sang đoạn kế."""
        a, b = moc[j]
        co = self._cua_tieng(j, moc)
        if co is not None:
            return co[0], co[1] - co[0]
        #  Bút viết KHỚP NHỊP giọng (như thẻ lời dẫn "はじめに": ngòi bút luôn nằm trên chữ đang đọc). Trước đây trần 7 s: câu đọc 10–20 s bị viết xong trong 7 s (~14–30 chữ/giây), bàn tay không
        #  theo kịp (trễ 60–200 px, có lúc nằm xa hẳn chữ đang viết) rồi rút đi trong khi giọng còn đọc tiếp (người dùng báo 05-10-2026).
        wd = min(16.0, max(0.9, 0.92 * (b - a)))
        nxt = moc[j + 1][0] if j + 1 < len(moc) else a + wd + 1.0
        return a, min(wd, max(0.5, nxt - a - 0.1))

    def _cua_tieng(self, j: int, moc: Optional[list] = None) -> Optional[tuple]:
        """(bắt đầu, kết thúc) khoảng giọng THỰC SỰ nói của đoạn ``j`` (``tieng``, không lấn sang đoạn kế); ``None`` nếu đoạn không có tiếng hoặc chưa có mốc tiếng thật."""
        tg, ke = getattr(self, "tieng", None), getattr(self, "ke", None)
        if tg is None or ke is None or not ke[j].get("am"):
            return None
        moc = moc if moc is not None else self.moc
        a1, b1 = float(tg[j][0]), float(tg[j][1])
        if j + 1 < len(moc):
            b1 = min(b1, float(moc[j + 1][0]) - 0.05)
        return (a1, b1) if b1 - a1 >= 0.3 else None

    def _luot_hien_tai(self, t: float) -> int:
        k = 0
        for i, b in enumerate(self.bd):
            if t >= b - 1e-6:
                k = i
        return k

    def ve_tinh(self, khung: np.ndarray, t: float) -> None:
        """Lớp TĨNH vẽ sau camera (tiêu đề, nhãn lượt). Mặc định không có."""
        self.tip_tinh = None

    def ve_tren(self, khung: np.ndarray, t: float) -> None:
        """Lớp vẽ SAU CẢ BÀN TAY — nằm TRÊN bàn tay: dòng ghi nguồn + phụ đề lời nói (bàn tay luôn ở DƯỚI phụ đề, người dùng đề nghị 05-10-2026; ghi nguồn cũng không được để tay che). Mặc định không có."""

    def cam_rect(self, rect: tuple, z_max: float) -> tuple:
        """Camera nhắm vào hình chữ nhật ``rect`` (x0, y0, x1, y1): zoom tối đa ``z_max`` nhưng KHÔNG quá mức làm khối bị cắt mép (khối rộng gần hết khung thì gần như không zoom)."""
        x0, y0, x1, y1 = rect
        z = min(z_max, self.W / (x1 - x0 + 70), self.H / (y1 - y0 + 90))
        return (max(1.0, z), (x0 + x1) / 2, (y0 + y1) / 2)

    def _pill_luot(self, khung: np.ndarray, k: int, x1: float, y: float, hau: str = "") -> None:
        """Nhãn "Lần 2 · Có bản dịch" ở góc phải — chỉ khi cảnh có nhiều lượt; màu theo loại lượt. ``hau``: đuôi thêm (số trang " · 2/3")."""
        lt = self.luot[k]
        txt = self.nhan("lan", n=k + 1) + " · " + self.nhan(lt["pk"]) + hau
        c = self._pill_chu.get(txt)
        if c is None:
            c = Chu(txt, self._font_nhan(txt), 26 if not self.doc else 24, (255, 255, 255), 0, 0, 600, "giua", 1)
            self._pill_chu[txt] = c
        ph, pw = c.h + 12, c.w + 40
        x0, y0 = int(x1 - pw), int(y)
        mau = PK_MAU.get(lt["pk"], (36, 92, 190))
        cv2.rectangle(khung, (x0 + ph // 2, y0), (x0 + pw - ph // 2, y0 + ph), mau, -1, cv2.LINE_AA)
        cv2.circle(khung, (x0 + ph // 2, y0 + ph // 2), ph // 2, mau, -1, cv2.LINE_AA)
        cv2.circle(khung, (x0 + pw - ph // 2, y0 + ph // 2), ph // 2, mau, -1, cv2.LINE_AA)
        c.x0, c.y0 = x0 + (pw - c.w) // 2, y0 + (ph - c.h) // 2
        c.ve(khung, 1.0)


# ====================================================================== BÀI ĐỌC
#: màu nhãn từng loại lượt
PK_MAU = {"doc": (36, 92, 190), "dich": (30, 132, 86), "hoi": (122, 64, 176), "giang": (200, 104, 24), "shadow": (176, 48, 108)}


class BaiDoc(_Hoc):
    """Bài đọc phát qua nhiều LƯỢT (đọc · có bản dịch · bài giảng · nhại theo — xem :mod:`sangtao_bai`).

    Lượt dạng DANH SÁCH: câu đang đọc được VIẾT ở giữa trang trong một bong bóng bên cạnh avatar của người đọc (nam/nữ theo từng nước), trượt vào từ phía của người nói; các
    câu trước trôi dần lên rồi mờ đi; từ hai người đọc trở lên thì bong bóng xen kẽ trái/phải. Lượt NHẠI THEO: sau mỗi câu có thanh đếm ngược.
    Lượt BÀI GIẢNG: slide cho từng câu (thẻ câu gốc + thẻ từ vựng + thẻ ngữ pháp); nhiều từ / mẫu ngữ pháp thì chia cột hoặc TÁCH TRANG; camera zoom vào thẻ đang đọc.
    Tiêu đề, nhãn lượt và dòng ghi nguồn là lớp TĨNH vẽ sau camera (:meth:`ve_tinh`) nên không bị cắt khi zoom."""

    def __init__(self, du_lieu: dict, ke: list, moc: list, bc, ngon: str, tuy: dict):
        super().__init__(bc, ngon, tuy)
        if tuy.get("phien_am") is False:                                           # người dùng tắt phiên âm (romaji / pinyin): bỏ dòng phiên âm dưới câu + cách đọc của từ vựng (không sửa dữ liệu gốc của cảnh)
            du_lieu = {**du_lieu, "lines": [{**ln, "phonetic": "", "vocab": [{**v, "r": ""} for v in (ln.get("vocab") or [])]} for ln in du_lieu["lines"]]}
        d = self.d = du_lieu
        self.ke, self.moc = ke, moc
        tg = d.get("_tieng")                                                       # khoảng có tiếng thật của từng đoạn (gan_moc); không có → phụ đề chia theo cả đoạn
        self.tieng = tg if (isinstance(tg, list) and len(tg) == len(ke)) else None
        ng = d.get("_nghi")                                                        # chỗ giọng ngắt giữa lời nói (đoạn có phụ đề): ranh giới câu thật
        self.nghi = ng if (isinstance(ng, list) and len(ng) == len(ke)) else None
        W, H, doc = self.W, self.H, self.doc
        #  BỐ CỤC GỌN (05-10-2026, "nhân vật chiếm diện tích không cần thiết", "cỡ chữ chuẩn với mắt người"): avatar chỉ còn là chấm nhận diện nhỏ ở lề, nhường bề ngang cho chữ.
        #  MỘT người đọc → không tên, cột chỉ rộng bằng avatar; từ HAI người đọc (bong bóng xen kẽ trái / phải) → có tên dưới avatar nên cột rộng hơn.
        self.n_nguoi = len(du_lieu.get("speakers") or [])
        self.ten_hien = self.n_nguoi >= 2
        self.MAR = 26 if doc else 38
        self.AVD = 50 if doc else 58
        self.ZON = (88 if doc else 104) if self.ten_hien else self.AVD                # bề rộng vùng avatar (+ tên)
        self.KHE = 16 if doc else 18                                               # khe giữa vùng avatar và bong bóng (đuôi bong bóng nằm trong khe này)
        self.COT = self.ZON + self.KHE
        self.PAD = 20 if doc else 24
        self.GAP = 22 if doc else 30
        self.dau = 150 if doc else 96                                              # chiều cao dải tiêu đề
        #  Khung DỌC còn chỗ theo chiều dọc: bản dịch tiêu đề dài hơn một dòng ở sàn cỡ chữ (20 px, ~55 chữ) thì xuống HAI dòng và dải tiêu đề cao thêm đúng một dòng (mọi bố cục bên dưới
        #  tính từ ``self.dau`` nên tự dời theo). Khung ngang chỉ một dòng (cạnh đó là nhãn lượt).
        self.dong_td = 1
        if doc and d["title_tr"] and CH.do_rong(CH.font(self._font_me(d["title_tr"]), 20), d["title_tr"]) > W - 2 * self.MAR:
            self.dong_td = 2
            self.dau += int(22 * self.co * 1.3)
        self.dai_tren = self.dau + 34
        n_luot = (max(s["luot"] for s in ke) + 1) if ke else 1
        self.n_luot = n_luot
        self.luot = []
        for k in range(n_luot):
            s0 = next(x for x in ke if x["luot"] == k and x["kieu"] != "loi")           # đoạn LỜI DẪN thuộc lượt nhưng không quyết định kiểu hiển thị của lượt
            self.luot.append({"pk": s0["pk"], "ld": s0["ld"], "kieu": s0["kieu"], "shadow": s0["shadow"]})
        self.luot_ld = [x["ld"] for x in self.luot]
        self.luot_kieu = [x["kieu"] for x in self.luot]
        self.luot_ds = [[j for j, s in enumerate(ke) if s["luot"] == k and s["kieu"] != "loi"] for k in range(n_luot)]
        self.loi_ds = [j for j, s in enumerate(ke) if s["kieu"] == "loi"]               # các thẻ lời dẫn (chào / chuyển lượt / kết), theo thứ tự phát
        self.phu_ds = [j for j, s in enumerate(ke) if s.get("phu")]                      # đoạn CHỈ NÓI (hình không có chữ): vẽ phụ đề nhỏ ở đáy video
        self.co_phu = bool(self.phu_ds)
        self._phu_chu: dict = {}
        self.trang_sach = str(tuy.get("bo_cuc") or "trang") != "cuon"                    # danh sách hiện theo TRANG (như trang sách) thay vì cuộn
        self._pg: dict = {}
        self._mask_thu: dict = {}
        self._chu_thu: dict = {}
        self._qz: dict = {}
        self._but_nho: dict = {}
        self._loi_chu: dict = {}
        self._mat_pill: dict = {}
        self.bd = B.bat_dau_luot(ke, moc)
        self.nhac = any(x["shadow"] for x in self.luot)
        co_ld = any(x["ld"] for x in self.luot)
        lines, nguoi = d["lines"], d["speakers"]
        self._pill_chu: dict = {}
        tw = W - 2 * self.MAR - self.COT - 2 * self.PAD - (12 if doc else 14)
        f_t =self._font_cho(ngon, RB.bo_ruby(" ".join(ln["text"] for ln in lines)), "subtitle")
        f_p = self._font_cho(ngon, " ".join(ln["phonetic"] for ln in lines) or "a", "subtitle", du_phong=((self.me, "subtitle"), ("vi", "subtitle")))
        f_r = self._font_me(" ".join(ln["translation"] for ln in lines) + d["title_tr"])
        self._f_t, self._f_p, self._f_r = f_t, f_p, f_r
        self._slide: dict = {}
        self._hl: dict = {}
        self.nguon_c = None
        if tuy.get("nguon"):                                                           # nguồn ở chân MỌI cảnh bài đọc: dòng 1 tên báo + ngày đăng, dòng 2 link (xem sangtao_du_an.nguon_hien)
            self.nguon_c = self.chu_nhieu_dong(tuy["nguon"], 18 if doc else 19, (138, 142, 158), W / 2, 0, W - 2 * self.MAR, "giua",
                                               self._font_nhan(tuy["nguon"], "subtitle"), 2)
            self.nguon_c.dat_top(H - 6 - self.nguon_c.h)                              # đáy khối cách đáy khung 6 px
        self.chan = int(self.nguon_c.h + 20) if self.nguon_c is not None else 28        # chiều cao dải chân trang (ghi nguồn) mà chữ chính không được lấn vào
        # avatar + tên từng người nói (màu riêng từng vai)
        self.avt = [AV.avatar(sp["gender"], ngon, i, self.AVD, vai=i) for i, sp in enumerate(nguoi)]
        self.ten = []
        for i, sp in enumerate(nguoi):
            f_n = self._font_cho(ngon if sp["name"] and not sp["name"].isascii() else "en", sp["name"], "label", du_phong=(("vi", "label"),))
            self.ten.append(Chu(sp["name"], f_n, 20 if not doc else 18, AV.VAI[i % 4][0], 0, 0, self.ZON, "giua", 1))
        # từng câu: chữ + phiên âm + dịch, bong bóng, vị trí ngang cố định (độc lập lượt)
        self.dong = []
        #  CỠ CHỮ CHUẨN cho mắt người (khung 720 px cạnh ngắn, xem trên điện thoại lẫn máy tính): chữ chính ≥ 36 / 32 px, phiên âm ≥ 24 / 22 px, bản dịch ≥ 26 / 24 px (≥ 3,3 % cạnh ngắn).
        #  Trước đây phiên âm ~25 px và có lúc tụt xuống 14 px (câu dài), chữ chính tụt tới 17–22 px nên không đọc nổi trên điện thoại. Câu quá dài KHÔNG thu nhỏ quá mức sàn nữa:
        #  khối chữ cao hơn khung nhìn sẽ CUỘN THEO NGÒI BÚT (``_doan_cao`` / ``_tam_cuon``).
        co_goc = (44 if doc else 50) if self.trang_sach else (46 if doc else 62)     # trang sách: vừa phải để một trang chứa được vài câu; cuộn dần: to hơn vì chỉ một câu ở giữa
        co_san_t, co_san_p, co_san_r = (32, 22, 24) if doc else (36, 24, 26)
        vt0, vb0 = self._khung_nhin(False)
        ngan_t = 0.5 * (vb0 - vt0)                                                      # chữ chính chiếm quá nửa khung nhìn thì thu nhỏ dần (tới sàn)
        ngan_tong = 0.86 * (vb0 - vt0)                                                  # cả khối (chữ + phiên âm + dịch + nhãn) nên lọt khung nhìn (dưới ngưỡng "câu cao" 0,92)
        for j, ln in enumerate(lines):
            co_t = co_goc                                                               # chưa nhân ``self.co`` (trẻ em ×1,12): ``self.chu`` tự nhân
            van_hien = self.ngoac(ln["text"]) if ln.get("quote") else ln["text"]
            tg = None
            if ln.get("quote"):                                                      # câu TRÍCH DẪN: ngoặc kép + nhãn nguồn (phần tự soạn mới là phần chính của video)
                nt = self.nhan_trich()
                tg = Chu(nt, self._font_nhan(nt, "subtitle"), 24 * self.co, (160, 100, 40), 0, 0, tw, "trai", 1)
            while True:
                tt = self.chu(van_hien, co_t, MUC, 0, 0, tw, "trai", 60, f_t)
                #  Phiên âm / bản dịch: HIỆN ĐỦ (không cắt giữa chữ), cỡ theo chữ chính nhưng không dưới sàn; quá ba dòng thì thu nhỏ dần tới sàn.
                pp = self._chu_vua(ln["phonetic"], max(co_san_p, co_t * 0.6), PHO, tw, f_p, 3, co_san_p, 40) if ln["phonetic"] else None
                rr = self._chu_vua(ln["translation"], max(co_san_r, co_t * 0.68), SLATE, tw, f_r, 3, co_san_r, 40) if ln["translation"] else None
                tong = 2 * self.PAD + tt.h + (8 + pp.h if pp else 0) + (6 + rr.h if (co_ld and rr) else 0) + (6 + tg.h if tg else 0)
                if (tt.h <= ngan_t and tong <= ngan_tong) or co_t <= co_san_t:
                    break
                co_t = max(co_san_t, co_t * 0.92)
            phan = [x for x in (tt, pp, rr if co_ld else None, tg) if x]
            #  MỘT người đọc: bong bóng RỘNG HẾT bề ngang (mép đều, dùng hết diện tích). Từ hai người: bề rộng theo chữ để thấy rõ bong bóng xen kẽ trái / phải.
            bw = int(max(x.w for x in phan) + 2 * self.PAD) if self.ten_hien else int(tw + 2 * self.PAD)
            ben = ln["s"] % 2 if self.ten_hien else 0                             # 0 = trái, 1 = phải
            if ben == 0:
                bx0 = self.MAR + self.COT
                ax = self.MAR + self.ZON / 2
            else:
                bx0 = W - self.MAR - self.COT - bw
                ax = W - self.MAR - self.ZON / 2
            h = {}
            for ld in (False, True):
                ht = 2 * self.PAD + tt.h + (8 + pp.h if pp else 0) + (6 + rr.h if (ld and rr) else 0) + (6 + tg.h if tg else 0)
                h[ld] = int(max(ht, 62, self.AVD + 14 + (self.ten[ln["s"]].h + 6 if self.ten_hien else 0)))
            self.dong.append({"t": tt, "p": pp, "r": rr, "g": tg, "bw": bw, "bx0": int(bx0), "ax": ax, "ben": ben, "h": h, "mask": {}})
        # tiêu đề + bản dịch tiêu đề (lớp tĩnh)
        self.tieu_de = self.tieu_de_dich = None
        if d["title"]:
            #  Tiêu đề cảnh ở dải trên cùng: CHỮ TRƠN (trước đây vẽ nguyên "事件[じけん]の概要[がいよう]…" kèm dấu ngoặc và bị cắt giữa chừng), thu nhỏ cho vừa MỘT dòng
            #  Quá dài mà đã tới SÀN cỡ chữ thì thêm "…" ở chỗ cắt (``_vua_mot_dong``): trước đây ``Chu`` cắt thầm giữa chữ — tiêu đề "米兵逮捕の強盗殺人事件 現場周辺で…" hiện cụt thành "…現場周" (người dùng báo 05-10-2026).
            tron_td = RB.bo_ruby(d["title"])
            f_td = self._font_cho(ngon, tron_td, "label")
            rong_td = W - 2 * self.MAR - (0 if doc else 380)
            tron_td, co_td = self._vua_mot_dong(tron_td, f_td, (40 if not doc else 38) * self.co, rong_td, 30 if not doc else 28)
            self.tieu_de = Chu(tron_td, f_td, co_td, (60, 90, 170), self.MAR if not doc else W / 2, 38 if not doc else 100, rong_td, "trai" if not doc else "giua", 1)
        if d["title_tr"]:
            #  Bản dịch tiêu đề: một dòng, thu nhỏ tới sàn 18 px (20 px ở khung dọc) rồi mới cắt "…" (trước: sàn 22 px + chừa cứng 380 px cho nhãn lượt nên tiêu đề dài như "… công bằng, thỏa…" bị cụt — người dùng báo 07-10-2026).
            #  Khung ngang: nhãn lượt nằm ở góc phải ngay trên dòng này nên chỉ chừa đúng bề rộng nhãn dài nhất (+ khe), không chừa thừa.
            f_tr = self._font_me(d["title_tr"])
            rong_tr = W - 2 * self.MAR - (0 if doc else self._rong_nhan_luot() + 24)
            if self.dong_td == 2:                                                   # khung dọc, dài: hai dòng cỡ 22 px; quá hai dòng thì dòng 2 cắt "…"
                co_tr = 22 * self.co
                dd = CH.ngat_dong(d["title_tr"], CH.font(f_tr, int(co_tr)), rong_tr)
                sep = " " if " " in d["title_tr"] else ""                           # Chu tự ngắt dòng lại theo bề rộng: nối bằng dấu cách (chữ Latin) hoặc liền (CJK)
                if len(dd) > 2:
                    dd = [dd[0], self._vua_mot_dong(sep.join(dd[1:]), f_tr, co_tr, rong_tr, co_tr)[0]]
                self.tieu_de_dich = Chu(sep.join(dd), f_tr, co_tr, SLATE, W / 2, 0, rong_tr, "giua", 2)
                self.tieu_de_dich.y0 = 124                                          # ngay dưới tiêu đề (cao 28 px, tâm y = 100)
            else:
                van_tr, co_tr = self._vua_mot_dong(d["title_tr"], f_tr, (28 if not doc else 26) * self.co, rong_tr, 18 if not doc else 20)
                self.tieu_de_dich = Chu(van_tr, f_tr, co_tr, SLATE, self.MAR if not doc else W / 2, 82 if not doc else 142, rong_tr, "trai" if not doc else "giua", 1)

    def _rong_nhan_luot(self) -> int:
        """Bề rộng (px) của nhãn lượt dài nhất ở góc phải ("Lần 2 · Có bản dịch · 9/9", gồm cả đuôi số trang) — cùng cách dựng với :meth:`_pill_luot`."""
        w = 0
        for k in range(self.n_luot):
            txt = self.nhan("lan", n=k + 1) + " · " + self.nhan(self.luot[k]["pk"]) + " · 9/9"
            c = Chu(txt, self._font_nhan(txt), 26 if not self.doc else 24, (255, 255, 255), 0, 0, 600, "giua", 1)
            w = max(w, int(c.w) + 40)
        return w

    # ================================================================ lớp tĩnh (sau camera)
    def ve_tinh(self, khung: np.ndarray, t: float) -> None:
        """Tiêu đề hiện ngay từ đầu cảnh (+ bản dịch), nhãn lượt ở góc phải, dòng ghi nguồn ở chân trang — vẽ lên khung ĐÃ qua camera."""
        self.tip_tinh = None
        k = self._luot_hien_tai(t)
        ld = self.luot_ld[k]
        #  Tiêu đề (+ bản dịch) HIỆN NGAY từ khung đầu, không còn bút viết (yêu cầu 05-10-2026). Trước đây đầu MỖI cảnh im lặng ``sangtao_bai.DAU_CANH`` = 1,4 s chỉ để chờ bút viết
        #  xong tiêu đề rồi mới đọc — video nhiều cảnh mất cả chục giây không có tiếng, người xem ngồi chờ; nay giọng đọc vào gần như ngay. Bút vẫn viết từng câu của bài LÚC đọc (có tiếng).
        if self.tieu_de is not None:
            self.tieu_de.ve(khung)
        if self.tieu_de_dich is not None and ld:
            self.tieu_de_dich.ve(khung, a=mem(doan(t, self.bd[k] + 0.1, self.bd[k] + 0.6)) if k > 0 else 1.0)
        tr = None if self._loi_dang_hien(t) is not None else self._trang_dang_xem(t, k)   # đang đọc dở một trang: nhãn kèm "2/3"
        if self.n_luot > 1 or (tr is not None and tr[1] > 1):
            self._pill_luot(khung, k, self.W - self.MAR, 22 if not self.doc else 24, f" · {tr[0]}/{tr[1]}" if (tr is not None and tr[1] > 1) else "")

    def ve_tren(self, khung: np.ndarray, t: float) -> None:
        """Dòng ghi nguồn ở chân trang + phụ đề lời nói — vẽ SAU bàn tay nên bàn tay (kể cả khi đưa xuống thấp) luôn nằm dưới chúng."""
        if self.nguon_c is not None:
            self.nguon_c.ve(khung, 0.9)
        if self.phu_ds:
            self._ve_phu_de(khung, t)

    @staticmethod
    def _chia_phu_de(van: str) -> list[str]:
        """Chia lời nói thành từng CÂU (cắt sau 。！？!? và sau dấu chấm + dấu cách) như phụ đề thật: mỗi lúc chỉ một câu ngắn, thường vừa MỘT dòng. Mẩu quá ngắn (< 8 chữ) nối vào mẩu trước.
        Dấu cắt không bao giờ nằm trong ngoặc cách đọc ``[…]``."""
        ra: list[str] = []
        for m in re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+", van.strip()):
            if not m:
                continue
            if ra and len(RB.bo_ruby(m)) < 8:
                ra[-1] += m
            else:
                ra.append(m)
        return ra or [van.strip()]

    #: giọng nghỉ khoảng này (giây) giữa hai câu liền nhau trong cùng một đoạn nói (sau 。/./?/!) — đo trên giọng Edge
    NGHI_GIUA_CAU = 0.35
    #: phụ đề hiện TRƯỚC lúc giọng bắt đầu câu đó (giây): người xem đọc kịp theo, không bao giờ thấy chữ đến sau tiếng
    PHU_SOM = 0.15

    @staticmethod
    def _giay_noi(van: str, ngon: str) -> float:
        """Thời gian NÓI ước tính (giây, tốc độ chuẩn) của một câu — chỉ để chia tỷ lệ giữa các câu trong cùng đoạn, nên chỉ cần đúng tương quan, không cần đúng tuyệt đối.
        Tiếng Nhật đếm theo nhịp (mora): kana 1, chữ Hán có furigana = số kana của cách đọc, chữ Hán trơ trọi ≈ 2, kana nhỏ (ゃゅょ…) không tính. Ngôn ngữ khác đếm ký tự chữ.
        Đếm theo SỐ KÝ TỰ (cách cũ) làm câu nhiều chữ Hán / nhiều kana bị chia sai thời điểm tới vài giây."""
        nho = "ゃゅょぁぃぅぇぉャュョァィゥェォ"
        dau = set("。、，,.!?！？…・「」『』（）()“”\"'：:；;—-～~[]{}|")
        ja = ngon == "ja"
        n = 0.0
        for goc, doc in RB.phan_tich(van):
            if doc and ja:
                n += sum(1 for ch in doc if ch not in nho and not ch.isspace())
                continue
            for ch in goc:
                if ch.isspace() or ch in dau or (ja and ch in nho):
                    continue
                n += 2.0 if (ja and ("一" <= ch <= "鿿" or ch == "々" or ch.isdigit())) else 1.0
        return n / B.CPS.get(ngon, 12.0)

    def _phu_de(self, j: int) -> list:
        """Các câu phụ đề của đoạn ``j`` (dựng một lần): [(khối chữ trắng — furigana nếu bật —, nền tối bo góc, x, y, tỷ lệ LÚC GIỌNG BẮT ĐẦU câu đó trong khoảng có tiếng)]. Nằm ngay trên dòng ghi nguồn ở chân trang;
        câu dài thì thu nhỏ chữ cho vừa MỘT dòng (tối thiểu 22 px), vẫn dài thì xuống hai dòng."""
        L = self._phu_chu.get(j)
        if L is None:
            cau = self._chia_phu_de(self.ke[j]["phu"])
            ngon_c = self.ngon if self.ke[j].get("ngon", "tgt") == "tgt" else self.me
            noi = [self._giay_noi(c_, ngon_c) for c_ in cau]
            tong = max(0.01, sum(noi) + self.NGHI_GIUA_CAU * (len(cau) - 1))
            rong = self.W - 2 * self.MAR - 60
            L, cum = [], 0.0
            for c_, g in zip(cau, noi):
                bd = cum / tong
                cum += g + self.NGHI_GIUA_CAU
                co = 24 if self.doc else 28
                chu = self.chu(c_, co, (255, 255, 255), 0, 0, rong, "giua", 2, self._f_t)
                while len(chu.dong) > 1 and co > 22:
                    co -= 2
                    chu = self.chu(c_, co, (255, 255, 255), 0, 0, rong, "giua", 2, self._f_t)
                px, py = 22, 10
                bw, bh = int(chu.w + 2 * px), int(chu.h + 2 * py)
                x0, y0 = int((self.W - bw) / 2), int(self.H - self.PHU_DAY - bh)
                chu.x0, chu.y0 = x0 + (bw - int(chu.w)) // 2, y0 + py
                L.append((chu, _hop_bo_goc(bw, bh, min(22, bh // 2), 2), x0, y0, bd))
            self._phu_chu[j] = L
        return L

    @staticmethod
    def _ranh_gioi_that(du_doan: list, ung_vien: list, rong: float) -> Optional[list]:
        """Chọn ranh giới câu THẬT từ các chỗ giọng ngắt trong tệp tiếng: ``du_doan`` = lúc bắt đầu câu 2, 3… theo ước lượng (giây); ``ung_vien`` = [[bắt đầu, kết thúc]] các khoảng lặng giữa lời nói.
        Mỗi ranh giới = LÚC GIỌNG NÓI LẠI (kết thúc khoảng lặng). Chọn dãy tăng dần ít "tốn" nhất: lệch khỏi ước lượng trừ chiều dài khoảng lặng (khoảng ngắt dài giữa hai câu thường dài hơn
        dấu phẩy). Có một ranh giới lệch quá ``max(1,2 s ; 30% khoảng nói)`` so với ước lượng → bỏ cả bộ (không tin), người gọi dùng ước lượng."""
        n = len(du_doan)
        uv = sorted((list(map(float, x)) for x in ung_vien if len(x) == 2), key=lambda x: x[0])
        if n == 0 or len(uv) < n:
            return None
        uv = sorted(sorted(uv, key=lambda x: x[0] - x[1])[:12], key=lambda x: x[0])         # tối đa 12 khoảng lặng dài nhất (giữ thứ tự thời gian) để dò tổ hợp không bùng nổ
        if len(uv) < n:
            return None
        tot, gv = None, 0.0
        for tap in itertools.combinations(range(len(uv)), n):
            gv = sum(abs(uv[i][1] - p) - (uv[i][1] - uv[i][0]) for i, p in zip(tap, du_doan))
            if tot is None or gv < tot[0]:
                tot = (gv, tap)
        gioi_han = max(1.2, 0.3 * rong)
        ra = [uv[i][1] for i in tot[1]]
        return ra if all(abs(r - p) <= gioi_han for r, p in zip(ra, du_doan)) else None

    def _phu_bat_dau(self, j: int) -> list[float]:
        """Lúc (giây, tuyệt đối) mỗi câu phụ đề của đoạn ``j`` BẮT ĐẦU hiện, trừ ``PHU_SOM``. Câu đầu: lúc giọng bắt đầu. Các câu sau: nếu có chỗ giọng ngắt thật trong tệp tiếng (``_nghi``) và khớp
        với ước lượng thì dùng chúng (khớp giọng bất kể tốc độ / engine); không thì dùng ước lượng theo thời gian nói trong khoảng có tiếng (:meth:`_giay_noi`)."""
        s0, s1 = self.moc[j]
        a0, a1 = (self.tieng[j][0], self.tieng[j][1]) if self.tieng else (s0, s1)
        rong = max(0.2, a1 - a0)
        cau = self._phu_de(j)
        du_doan = [a0 + x[4] * rong for x in cau]
        if len(cau) >= 2 and self.nghi and self.nghi[j]:
            that = self._ranh_gioi_that(du_doan[1:], self.nghi[j], rong)
            if that is not None:
                du_doan = [du_doan[0]] + that
        return [x - self.PHU_SOM for x in du_doan]

    # ---------------------------------------------------------------- BÚT ĐI THEO GIỌNG ĐỌC
    #: chỗ ngắt tương đương ~3 nhịp (≈ 0,4 s): chỉ để ƯỚC LƯỢNG lúc giọng nghỉ ở mỗi dấu câu, trước khi ghép với các khoảng lặng đo được
    NHIP_NGAT = 3.0

    @staticmethod
    def _ghep_nghi(uoc: list, ls: list) -> list:
        """Ghép các khoảng lặng ĐO được trong tệp tiếng (``ls`` = [(bắt đầu, kết thúc)], giây) với các dấu ngắt trong chữ (``uoc`` = lúc ƯỚC LƯỢNG giọng bắt đầu nghỉ ở từng dấu), theo đúng thứ tự,
        mỗi bên tối đa một lần. Số khoảng lặng KHÔNG cần bằng số dấu: giọng nghỉ ngắn (< 0,25 s, đo không ra) ở một số dấu ``、``, hoặc có chỗ ngắt không ứng với dấu nào (:func:`_ranh_gioi_that` của
        phụ đề đòi đủ số khoảng lặng nên với câu dài thường bỏ cuộc). Chọn bộ ghép tổng độ lệch nhỏ nhất (quy hoạch động như căn hai chuỗi): bỏ sót một khoảng lặng "tốn" 1,0 s, bỏ sót một dấu 0,5 s,
        lệch quá 1,6 s thì không ghép. Trả [(chỉ số dấu, chỉ số khoảng lặng)] tăng dần."""
        n, m = len(uoc), len(ls)
        if n == 0 or m == 0:
            return []
        f = [[0.0] * (m + 1) for _ in range(n + 1)]
        tr = [[0] * (m + 1) for _ in range(n + 1)]                                # 0 = bỏ dấu, 1 = bỏ khoảng lặng, 2 = ghép
        for i in range(n + 1):
            for j in range(m + 1):
                if i == 0 and j == 0:
                    continue
                tot, cach = float("inf"), 0
                if i > 0:
                    tot, cach = f[i - 1][j] + 0.5, 0
                if j > 0 and f[i][j - 1] + 1.0 < tot:
                    tot, cach = f[i][j - 1] + 1.0, 1
                if i > 0 and j > 0:
                    d = abs(ls[j - 1][0] - uoc[i - 1])
                    if d <= 1.6 and f[i - 1][j - 1] + d < tot:
                        tot, cach = f[i - 1][j - 1] + d, 2
                f[i][j], tr[i][j] = tot, cach
        ra, i, j = [], n, m
        while i > 0 or j > 0:
            cach = tr[i][j]
            if cach == 2:
                ra.append((i - 1, j - 1))
                i, j = i - 1, j - 1
            elif cach == 1:
                j -= 1
            else:
                i -= 1
        return ra[::-1]

    def _duong_but(self, j: int, chu) -> Optional[tuple]:
        """Đường THỜI GIAN → TIẾN ĐỘ VIẾT của ``chu`` khi đoạn ``j`` được đọc: (mảng giây, mảng quet 0..1), hoặc ``None`` (đoạn không có mốc tiếng thật → dùng cửa sổ ước lượng). Dựng một lần, nhớ lại."""
        khoa = (j, id(chu))
        r = self._but_nho.get(khoa)
        if r is None:
            r = (chu, self._tinh_duong_but(j, chu))                                  # giữ ``chu`` trong bộ nhớ đệm để id của nó không bị cấp lại cho khối khác
            self._but_nho[khoa] = r
        return r[1]

    def _tinh_duong_but(self, j: int, chu) -> Optional[tuple]:
        """Bút viết theo GIỌNG THẬT chứ không đều theo lượng mực (người dùng báo 06-10-2026 "tốc độ viết không khớp tốc độ đọc"): giọng đọc đều theo NHỊP (mora) và nghỉ ở dấu ngắt, còn bút đều theo mực
        nên chữ Hán dày bị viết nhanh hơn giọng, kana mỏng bị viết chậm hơn, và cuối câu bút xong sớm cả giây.
        Cách làm: (1) cửa sổ viết = khoảng có tiếng thật của đoạn (:meth:`cua_so`); (2) mỗi đơn vị chữ (:meth:`Chu.don_vi_but`) có nhịp riêng — kana 1, cụm Hán có furigana = số kana của cách đọc;
        (3) các dấu ngắt ``、。`` được ghép với khoảng lặng ĐO được trong tệp tiếng (:meth:`_ghep_nghi`): bút dừng đúng khoảng giọng nghỉ; (4) giữa hai chỗ nghỉ, thời gian chia theo nhịp, còn vị trí
        của từng đơn vị trên "tiến độ viết" lấy từ ĐÚNG hình nét sẽ được vẽ (:meth:`Chu.quet_den`). Không ghép được khoảng lặng nào thì chia đều theo nhịp trong cả khoảng có tiếng."""
        s = self.ke[j]
        if self.tieng is None or not s.get("am"):
            return None
        ngon_c = self.ngon if s.get("ngon", "tgt") == "tgt" else self.me
        dv = chu.don_vi_but(ngon_c == "ja")
        if not dv:
            return None
        nhip = np.cumsum([x[0] for x in dv])
        tong = float(nhip[-1])
        if tong <= 0.0:
            return None
        wa, wd = self.cua_so(self.moc, j)
        # chỗ ngắt: một CHUỖI ký tự không đọc ra tiếng có ít nhất một dấu ngắt, nằm giữa hai đoạn có chữ đọc; chuỗi kết thúc ở ``v`` (bút viết xong cả chuỗi, vd "。」" rồi mới nghỉ)
        ung, u = [], 0
        while u < len(dv):
            if dv[u][0] > 0:
                u += 1
                continue
            v = u
            while v + 1 < len(dv) and dv[v + 1][0] <= 0:
                v += 1
            if any(dv[x][1] for x in range(u, v + 1)) and 0.0 < nhip[v] < tong:
                ung.append(v)
            u = v + 1
        ls = []
        if self.nghi and self.nghi[j]:
            ls = sorted((float(p[0]), float(p[1])) for p in self.nghi[j] if len(p) == 2 and wa + 0.05 < p[0] and p[1] < wa + wd - 0.05)
        bu = self.NHIP_NGAT
        uoc = [wa + wd * (float(nhip[v]) + bu * k) / (tong + bu * len(ung)) for k, v in enumerate(ung)]
        dung = [(ung[k], ls[l][0], ls[l][1]) for k, l in self._ghep_nghi(uoc, ls)]
        ts, qs = [wa], [0.0]

        def them(t_: float, q_: float) -> None:
            ts.append(max(t_, ts[-1] + 1e-4))                                       # thời gian tăng NGẶT (cho np.interp), tiến độ không bao giờ lùi
            qs.append(max(q_, qs[-1]))

        u0, t0, w0 = 0, wa, 0.0
        for u1, s_, e_ in dung + [(len(dv) - 1, wa + wd, None)]:
            w1 = float(nhip[u1])
            for u in range(u0, u1 + 1):
                t_u = t0 + (s_ - t0) * ((float(nhip[u]) - w0) / (w1 - w0)) if w1 > w0 else s_
                them(t_u, chu.quet_den(dv[u][2], dv[u][3]))
            if e_ is None:
                break
            them(e_, qs[-1])                                                          # đứng yên suốt chỗ giọng nghỉ
            u0, t0, w0 = u1 + 1, e_, w1
        qs[-1] = 1.0
        return np.array(ts), np.array(qs)

    def _q_but(self, j: int, chu, t: float) -> float:
        """Tiến độ viết (0..1) của ``chu`` ở thời điểm ``t`` khi đoạn ``j`` được đọc: bút đi theo nhịp và chỗ nghỉ thật của giọng (:meth:`_duong_but`); đoạn không có mốc tiếng thật thì viết đều trong cửa sổ ước lượng."""
        d = self._duong_but(j, chu)
        if d is None:
            wa, wd = self.cua_so(self.moc, j)
            return doan(t, wa, wa + wd)
        return float(np.interp(t, d[0], d[1]))

    def _ve_phu_de(self, khung: np.ndarray, t: float) -> None:
        """PHỤ ĐỀ nhỏ ở đáy video cho những đoạn chỉ NÓI mà hình không có chữ tương ứng (câu dẫn dắt, lời giảng từ vựng / ngữ pháp), để người xem vừa nghe vừa đọc theo.
        Hiện từng câu theo thời gian NÓI ước tính (nhịp + nghỉ giữa câu) trong KHOẢNG CÓ TIẾNG của đoạn (bỏ lặng đầu/cuối của tệp giọng — ``_tieng`` trong tệp mốc), và hiện sớm hơn
        giọng ``PHU_SOM`` giây. Không có ``_tieng`` (tiếng cũ / chưa có tiếng) thì coi cả đoạn là đang nói."""
        for j in self.phu_ds:
            s0, s1 = self.moc[j]
            if not (s0 - 0.3 <= t < s1 + 0.5):
                continue
            a1 = self.tieng[j][1] if self.tieng else s1
            cau = self._phu_de(j)
            bd = self._phu_bat_dau(j)
            k = max((i for i, b in enumerate(bd) if t >= b - 0.05), default=0)
            a = mem(doan(t, bd[k] - 0.05, bd[k] + 0.1))
            if k == len(cau) - 1:
                a *= 1.0 - mem(doan(t, a1 + 0.2, a1 + 0.4))
            if a <= 0.01:
                continue
            chu, m, x0, y0, _ = cau[k]
            RV.tron(khung, (18, 20, 34), m, x0 - 2, y0 - 2, 0.70 * a)
            chu.ve(khung, a)
            return

    @property
    def PHU_DAY(self) -> int:
        """Khoảng từ đáy khung tới mép dưới phụ đề: chừa trọn khối ghi nguồn (hai dòng: tên + ngày đăng, link) ở chân trang."""
        return int(self.nguon_c.h + 16) if self.nguon_c is not None else 14             # +16: phụ đề KHÔNG được chạm / đè lên khối ghi nguồn + bản quyền

    @property
    def PHU_CAO(self) -> int:
        """Chiều cao slide nhường cho phụ đề khi bài giảng có đoạn chỉ nói (phụ đề một dòng ngang / hai dòng dọc 9:16) cộng phần chân trang ghi nguồn vượt quá mức 52 px đã chừa sẵn."""
        return max(40, self.PHU_DAY + (128 if self.doc else 76) + 6 - 52)

    # ================================================================ nội dung (trước camera)
    def ve(self, world: np.ndarray, t: float) -> None:
        self.tip = None
        self.cam_muc = None
        k = self._luot_hien_tai(t)
        loi = self._loi_dang_hien(t)
        if loi is not None:                                                         # thẻ lời dẫn (chào / chuyển lượt / kết) đang nói
            self.dai_tren, self.dai_mem = self.dau + 34, 34
            self._ve_loi(world, t, *loi)
        elif self.luot_kieu[k] == "quiz":
            self.dai_tren, self.dai_mem = self.dau + 34, 34
            self._ve_quiz(world, t, self.luot_ds[k])
        elif self.luot_kieu[k] == "slide":
            self.dai_tren, self.dai_mem = self.dau, 22                              # không zoom: thẻ nằm ngay dưới dải tiêu đề, chỉ mờ nhẹ
            self.dai_zoom, self.dai_zoom_mem = self.dau + 34, 34                    # đang zoom: thẻ bị đẩy lên dưới tiêu đề → phủ dày hơn, không để lộ mảnh chữ bị cắt
            self._ve_slide(world, t, self.luot_ds[k])
        else:
            if self.trang_sach:
                self.dai_tren, self.dai_mem = self.dau + 4, 10                        # chỉ phủ sạch dải tiêu đề (trang sách không có câu trôi lên)
                self._ve_trang(world, t, k, self.luot_ld[k], self.luot_ds[k])
            else:
                self.dai_tren, self.dai_mem = self.dau + 34, 34
                self._ve_danh_sach(world, t, k, self.luot_ld[k], self.luot_ds[k])

    # ---------------------------------------------------------------- LỜI DẪN (chào mở đầu, chuyển lượt, lời kết)
    def _loi_dang_hien(self, t: float) -> Optional[tuple]:
        """(chỉ số đoạn, lúc hiện, lúc ẩn) của thẻ lời dẫn đang hiện ở thời điểm ``t`` — hiện từ trước lúc nói một chút tới ngay trước đoạn kế tiếp."""
        for j in self.loi_ds:
            a = self.moc[j][0] - 0.15
            b = (self.moc[j + 1][0] - 0.12) if j + 1 < len(self.moc) else self.moc[j][1] + 1.2
            if a <= t < b:
                return j, a, b
        return None

    def _chu_pill(self, txt: str) -> Chu:
        """Khối chữ trắng của một nhãn viên thuốc (dựng một lần cho mỗi chuỗi)."""
        key = ("pill", txt)
        c = self._pill_chu.get(key)
        if c is None:
            c = Chu(txt, self._font_nhan(txt), 26 * self.co if not self.doc else 24, (255, 255, 255), 0, 0, 700, "giua", 1)
            self._pill_chu[key] = c
        return c

    def _ve_pill_o(self, canvas: np.ndarray, txt: str, mau: tuple, x0: float, y0: float, a: float = 1.0) -> tuple[int, int]:
        """Nhãn viên thuốc (chữ trắng trên nền màu) vẽ lên ``canvas`` ở góc trái-trên (x0, y0), có độ trong suốt ``a``. Trả (rộng, cao)."""
        c = self._chu_pill(txt)
        ph, pw = c.h + 12, c.w + 36
        m = self._mat_pill.get((pw, ph))
        if m is None:
            m = self._mat_pill[(pw, ph)] = _hop_bo_goc(pw, ph, ph // 2, 2)
        RV.tron(canvas, mau, m, int(x0) - 2, int(y0) - 2, a)
        c.x0, c.y0 = int(x0) + (pw - c.w) // 2, int(y0) + (ph - c.h) // 2
        c.ve(canvas, a)
        return pw, ph

    def _lo_trinh(self) -> list[tuple]:
        """Các nhãn lộ trình (lượt đang bật) cho thẻ chào: [(chữ, màu)]."""
        return [(self.nhan("lan", n=k + 1) + " · " + self.nhan(lt["pk"]), PK_MAU.get(lt["pk"], (36, 92, 190))) for k, lt in enumerate(self.luot)]

    def _dung_loi(self, j: int) -> dict:
        """Thẻ lời dẫn của đoạn ``j`` (dựng một lần): khối chữ (cỡ tự giảm cho vừa khung), thẻ nền, nhãn và — với lời CHÀO — các nhãn lộ trình."""
        if j in self._loi_chu:
            return self._loi_chu[j]
        s = self.ke[j]
        W, H = self.W, self.H
        mo_dau = s["el"] == "n" and s["luot"] == 0 and bool(self.d.get("_dau"))
        nhan = self.nhan("loi_mo") if mo_dau else self.nhan("loi_ket") if s["el"] == "k" else ""
        rows: list[list[tuple]] = []
        if mo_dau and self.n_luot > 1:                                              # xếp các nhãn lộ trình thành hàng, hàng nào vượt bề ngang thì xuống hàng
            cur, w_cur = [], 0
            for txt, mau in self._lo_trinh():
                pw = self._chu_pill(txt).w + 36
                if cur and w_cur + 14 + pw > W - 2 * self.MAR:
                    rows.append(cur)
                    cur, w_cur = [], 0
                cur.append((txt, mau, pw))
                w_cur += pw + (14 if len(cur) > 1 else 0)
            rows.append(cur)
        ph = 50 if not self.doc else 46
        h_lo = len(rows) * (ph + 12) + 8 if rows else 0
        top = self.dau + 34 + 24 + (ph // 2 if nhan else 0)
        duoi = H - 62 - h_lo
        rong_the = W - 2 * self.MAR - 20
        pad = self.PAD + 14
        chu = None
        co_t = 46 if not self.doc else 40
        for _ in range(10):
            chu = self.chu(s.get("hien") or s["van"], co_t, MUC, 0, 0, rong_the - 2 * pad, "giua", 14)      # chữ tiếng đích (tiếng Nhật kèm furigana)
            if chu.h + 2 * pad <= duoi - top or co_t <= 22:
                break
            co_t *= 0.92
        ch, cw = int(chu.h + 2 * pad), int(max(chu.w + 2 * pad, 420 if nhan else 0))
        cx0 = int((W - cw) / 2)
        cy0 = int(top + max(0, (duoi - top - ch) / 2))
        m = _hop_bo_goc(cw, ch, 28)
        chu.x0, chu.y0 = int(cx0 + (cw - chu.w) / 2), int(cy0 + pad)
        res = {"chu": chu, "rect": (cx0, cy0, cw, ch), "m": m, "bong": cv2.GaussianBlur(np.roll(m, 7, axis=0), (0, 0), 7) * 0.20, "nhan": nhan,
               "rows": rows, "y_lo": int(duoi + 6), "ph": ph, "mo_dau": mo_dau}
        self._loi_chu[j] = res
        return res

    def _ve_loi(self, world: np.ndarray, t: float, j: int, a0: float, b0: float) -> None:
        """Thẻ lời dẫn: thẻ giấy ở giữa trang, lời nói được VIẾT dần suốt lúc đọc; lời chào kèm lộ trình các lượt hiện lần lượt theo lời nói."""
        L = self._dung_loi(j)
        a = mem((t - a0) / 0.3) * mem((b0 - t) / 0.25)
        if a <= 0:
            return
        nen = world.copy() if a < 1.0 else None
        cx0, cy0, cw, ch = L["rect"]
        RV.tron(world, (40, 40, 70), L["bong"], cx0 - 20, cy0 - 20)
        RV.tron(world, self.NEN_TU, L["m"], cx0 - 20, cy0 - 20, 0.96)
        if L["nhan"]:
            self._ve_pill_o(world, L["nhan"], (176, 84, 30), cx0 + 26, cy0 - L["ph"] // 2)
        s0, s1 = self.moc[j]
        duong = self._duong_but(j, L["chu"])                                         # bút đi theo nhịp + chỗ nghỉ THẬT của giọng (như câu của bài); chưa có mốc tiếng thật thì viết đều trong 0,92 × đoạn (như cũ, không trần 16 s)
        q = float(np.interp(t, duong[0], duong[1])) if duong is not None else doan(t, s0, s0 + max(0.8, 0.92 * (s1 - s0)))
        L["chu"].ve(world, 1.0, quet=q)
        if 0 < q < 1:
            self.tip = L["chu"].dau_but(q)
        if L["rows"]:
            y = L["y_lo"]
            n = 0
            for hang in L["rows"]:
                tong = sum(p[2] for p in hang) + 14 * (len(hang) - 1)
                x = (self.W - tong) / 2
                for txt, mau, pw in hang:
                    ap = mem((t - (s0 + (s1 - s0) * (0.3 + 0.55 * n / max(1, self.n_luot)))) / 0.35)
                    if ap > 0:
                        self._ve_pill_o(world, txt, mau, x, y + (1.0 - ap) * 10, ap)
                    x += pw + 14
                    n += 1
                y += L["ph"] + 12
        if nen is not None:
            world[:] = cv2.addWeighted(world, a, nen, 1.0 - a, 0)

    # ---------------------------------------------------------------- DANH SÁCH
    @staticmethod
    def _dung_bong_bong(dg: dict, w: int, h: int, r: float) -> tuple:
        m = _hop_bo_goc(w, h, max(8, int(r)))
        sg = -1 if dg["ben"] == 0 else 1                                          # đuôi bong bóng chỉ về phía avatar
        xe = 20 if dg["ben"] == 0 else 20 + w
        tam = np.array([[xe, 20 + 18], [xe + sg * 12, 20 + 31], [xe, 20 + 44]], np.int32)     # đuôi ngắn, ngang tâm avatar (avatar nhỏ nằm sát lề)
        mu = (m * 255).astype(np.uint8)
        cv2.fillConvexPoly(mu, tam, 255, cv2.LINE_AA)
        m = mu.astype(np.float32) / 255.0
        return m, cv2.GaussianBlur(np.roll(m, 7, axis=0), (0, 0), 7) * 0.20

    def _bong_bong(self, j: int, ld: bool, sc: float = 1.0) -> tuple:
        """(mặt nạ bong bóng, mặt nạ bóng đổ) của câu ``j`` ở lượt có/không có bản dịch — dựng một lần rồi dùng lại. ``sc`` < 1: bản THU NHỎ (câu cao đang xem tổng thể),
        chỉ giữ vài bản gần nhất vì mỗi bản của khối cao rất nặng."""
        dg = self.dong[j]
        if sc >= 0.995:
            if ld not in dg["mask"]:
                dg["mask"][ld] = self._dung_bong_bong(dg, dg["bw"], self._cao_bb(j, ld), 26)
            return dg["mask"][ld]
        khoa = (j, ld, int(round(sc * 100)))
        r = self._mask_thu.get(khoa)
        if r is None:
            if len(self._mask_thu) >= 6:
                self._mask_thu.pop(next(iter(self._mask_thu)))
            r = self._mask_thu[khoa] = self._dung_bong_bong(dg, max(40, int(dg["bw"] * sc)), max(40, int(self._cao_bb(j, ld) * sc)), 26 * sc)
        return r

    def _thu_chu(self, c: Chu, sc: float) -> Chu:
        """Bản THU NHỎ ``sc`` của khối chữ (chỉ để vẽ cả khối, không viết dần): thu nhỏ ảnh chữ bằng INTER_AREA; giữ vài bản gần nhất."""
        khoa = (id(c), int(round(sc * 100)))
        r = self._chu_thu.get(khoa)
        if r is None:
            if len(self._chu_thu) >= 24:
                self._chu_thu.pop(next(iter(self._chu_thu)))
            r = Chu.__new__(Chu)
            r.__dict__.update(c.__dict__)
            r.al = cv2.resize(c.al, (max(1, int(round(c.w * sc))), max(1, int(round(c.h * sc)))), interpolation=cv2.INTER_AREA)
            r.h, r.w = r.al.shape
            self._chu_thu[khoa] = r
        return r

    def _cao_bb(self, j: int, ld: bool) -> int:
        dg = self.dong[j]
        tt, pp, rr, tg = dg["t"], dg["p"], dg["r"], dg["g"]
        return int(max(66, 2 * self.PAD + tt.h + (8 + pp.h if pp else 0) + (6 + rr.h if (ld and rr) else 0) + (6 + tg.h if tg else 0)))

    def _cao_khoi(self, j: int, ld: bool) -> int:
        return self.dong[j]["h"][ld]

    # ---------------------------------------------------------------- CÂU CAO HƠN KHUNG NHÌN (đọc nguyên văn báo: một câu dài hàng trăm chữ)
    def _khung_nhin(self, shadow: bool) -> tuple[float, float]:
        """(mép trên, mép dưới) vùng chữ nhìn thấy được: dưới dải tiêu đề, trên dải ghi nguồn (và thanh «Nhắc lại theo» của lượt nhại theo)."""
        return float(self.dau + 16), float(self.H - self.chan - (46 if shadow else 0))

    def _la_cao(self, j: int, ld: bool, shadow: bool) -> bool:
        """Khối chữ của câu ``j`` (chỉ số câu) cao hơn khung nhìn → viết LẦN LƯỢT từ trên xuống và cuộn theo ngòi bút, KHÔNG thu nhỏ chữ dưới sàn dễ đọc."""
        vt, vb = self._khung_nhin(shadow)
        return self._cao_khoi(j, ld) > 0.92 * (vb - vt)

    def _het_cho(self, j: int) -> float:
        """Lúc người xem CÒN THẤY khối / trang của đoạn ``j`` (trước khi sang câu hay trang kế): ngay trước đoạn kế trong cùng lượt; hết lượt thì chỉ thêm chốc lát (nhãn lượt mới tới sớm),
        hết cảnh thì thêm đệm cuối."""
        b0 = self.moc[j][1]
        if j + 1 < len(self.moc):
            nx = self.ke[j + 1]
            if nx["luot"] == self.ke[j]["luot"] and nx["kieu"] != "loi":
                return max(b0, float(self.moc[j + 1][0]) - 0.12)
            return b0 + 0.3
        return b0 + 0.8

    def _doan_cao(self, j_seg: int, dg: dict, ld: bool) -> list:
        """Câu CAO: [(khối chữ, y đầu trong bong bóng, bắt đầu, kết thúc, chạy-dần?, đoạn tiếng của bút | None)] để ngòi bút đi từ đầu tới cuối khối và màn hình cuộn theo (:meth:`_dinh_cao`).
        CÓ mốc tiếng thật: chữ chính được viết suốt KHOẢNG GIỌNG ĐỌC (bút đi theo nhịp + chỗ nghỉ của giọng, :meth:`_q_but`) — trước đây chữ chính, phiên âm và bản dịch chia chung cửa sổ đọc theo
        chiều cao nên chữ chính bị viết xong sớm (nhanh gần gấp đôi giọng) còn phiên âm / bản dịch chạy vèo (người dùng báo 06-10-2026). Phiên âm + bản dịch hiện SAU khi giọng đọc hết:
        khi lùi ra xem tổng thể thì mờ dần hiện (:meth:`_ve_khoi_thu`); không có tổng thể thì chạy dần ngay sau lời đọc. Chưa có mốc tiếng thật: chữ → phiên âm → dịch chia nhau cả lúc đọc (cách cũ)."""
        a0, b0 = self.moc[j_seg]
        co = self._cua_tieng(j_seg)
        tong_the = self._tong_the(j_seg, ld)
        y = float(self.PAD)
        muc = [(dg["t"], y, False)]
        y += dg["t"].h + 8
        if dg["p"]:
            muc.append((dg["p"], y, True))
            y += dg["p"].h + 6
        if ld and dg["r"]:
            muc.append((dg["r"], y, True))
        if co is not None:
            a1, b1 = co
            ra = [(dg["t"], muc[0][1], a1, b1, False, j_seg)]
            phu = muc[1:]
            if phu and tong_the is not None:                                          # phiên âm + dịch hiện lúc lùi ra xem tổng thể; trước đó chưa vẽ
                ra += [(c, yy, tong_the[2], tong_the[2] + 1e-3, ng, None) for c, yy, ng in phu]
            elif phu:
                t2 = max(b1 + 1.2, self._het_cho(j_seg))
                tp, tg = float(sum(c.h for c, _, _ in phu)), b1
                for c, yy, ng in phu:
                    d = (t2 - b1) * c.h / tp
                    ra.append((c, yy, tg, tg + d, ng, None))
                    tg += d
            return ra
        t0, t1 = a0 + 0.2, max(a0 + 2.0, tong_the[2] if tong_the else b0 - 0.5)       # viết xong sớm hơn lúc hết lời để còn nhìn TỔNG THỂ
        tong = float(sum(c.h for c, _, _ in muc))
        ra, tg = [], t0
        for c, yy, ng in muc:
            d = (t1 - t0) * c.h / tong
            ra.append((c, yy, tg, tg + d, ng, None))
            tg += d
        return ra

    def _tong_the(self, j_seg: int, ld: bool) -> Optional[tuple]:
        """Câu CAO đã VIẾT XONG thì THU NHỎ cả khối cho vừa khung nhìn để người xem thấy TỔNG THỂ (không còn cảnh chỉ thấy bản dịch mà mất phần chữ gốc phía trên) — yêu cầu 05-10-2026.
        Trả (tỷ lệ thu nhỏ, y tâm trên màn hình, lúc bắt đầu thu nhỏ); ``None`` nếu không áp dụng (không đủ giờ để nhìn tổng thể, hoặc khối cao tới mức thu nhỏ chỉ còn <45 % thì chữ vô nghĩa).
        Bút đi theo giọng (có mốc tiếng thật): lùi ra NGAY SAU khi giọng đọc hết câu (lúc đó bút vừa viết xong) và giữ tới khi sang câu kế, cần ≥ 1 s. Chưa có mốc tiếng thật: ``dwell`` trước cuối đoạn.
        Chỉ gọi lúc vẽ (cần ``dai_tren`` của kiểu hiển thị hiện tại)."""
        s = self.ke[j_seg]
        a0, b0 = self.moc[j_seg]
        co = self._cua_tieng(j_seg)
        if co is not None:
            t_ov = co[1] + 0.1
            if self._het_cho(j_seg) - t_ov < 1.0:
                return None
        else:
            dwell = min(4.0, 0.15 * (b0 - a0))
            if dwell < 1.2:
                return None
            t_ov = b0 - dwell
        hb = self._cao_khoi(s["i"], ld)
        vt, vb = self._khung_nhin(bool(s["shadow"]))
        vt = max(vt, float(self.dai_tren) + 8.0)                                        # không chui vào dải tiêu đề đang phủ nền giấy
        se = (vb - vt) / hb
        if not 0.45 <= se <= 0.97:
            return None
        return se, (vt + vb) / 2.0, t_ov

    def _but_cao(self, parts: list, t: float, hb: float) -> float:
        """Vị trí ngòi bút (tính từ đỉnh khối) của câu CAO ở thời điểm ``t``; viết xong thì ở đáy khối ``hb``."""
        for c, y, w0, w1, _, jj in parts:
            q = self._q_but(jj, c, t) if jj is not None else doan(t, w0, w1)
            if q < 1.0:
                return y + q * c.h
        return float(hb)

    @staticmethod
    def _dinh_cao(hb: float, f: float, vt: float, vb: float) -> float:
        """Toạ độ y của ĐỈNH khối CAO sao cho ngòi bút (cách đỉnh ``f``) nằm ở ~42 % chiều cao khung nhìn; kẹp để đỉnh khối không thấp hơn ``vt`` và đáy không cao hơn ``vb``."""
        return min(vt, max(vb - hb, vt + 0.42 * (vb - vt) - f))

    # ---------------------------------------------------------------- CÂU HỎI (lượt "hoi")
    def _dung_quiz(self, n: int) -> dict:
        """Bố cục câu hỏi ``n`` (dựng một lần): thẻ CÂU HỎI (avatar người dẫn + chữ tiếng đích + bản dịch) ở trên, thẻ ĐÁP ÁN ở dưới, giữa hai thẻ chừa chỗ cho thanh «Hãy suy nghĩ»."""
        if n in self._qz:
            return self._qz[n]
        W, H = self.W, self.H
        qz = self.d["quiz"][n]
        tw = W - 2 * self.MAR - self.COT - 2 * self.PAD - (12 if self.doc else 14)
        top = self.dau + 34 + 34                                                      # chừa chỗ cho nhãn "Câu hỏi n/N" đè lên mép thẻ
        duoi = H - self.chan
        gap = 76
        co = 50 if not self.doc else 44
        co_san, co_san_d = (32, 24) if self.doc else (36, 26)                         # sàn cỡ chữ chính / bản dịch (xem BaiDoc.__init__)
        for _ in range(10):
            q = self.chu(qz["q"], co, MUC, 0, 0, tw, "trai", DONG_DAY_DU)
            qt = self.chu(qz["q_tr"], max(co_san_d, co * 0.66), SLATE, 0, 0, tw, "trai", DONG_DAY_DU, self._font_me(qz["q_tr"])) if qz["q_tr"] else None
            a = self.chu(qz["a"], co * 0.94, (24, 104, 66), 0, 0, tw, "trai", DONG_DAY_DU)
            at = self.chu(qz["a_tr"], max(co_san_d, co * 0.66), SLATE, 0, 0, tw, "trai", DONG_DAY_DU, self._font_me(qz["a_tr"])) if qz["a_tr"] else None
            hq = int(max(2 * self.PAD + q.h + (8 + qt.h if qt else 0), self.AVD + 14 + (self.ten[0].h + 6 if self.ten_hien else 0)))
            ha = int(2 * self.PAD + a.h + (8 + at.h if at else 0))
            if hq + gap + ha <= duoi - top or co <= co_san:
                break
            co = max(co_san, co * 0.92)
        bx0 = self.MAR + self.COT
        bw = int(W - self.MAR - bx0 - 10)                                              # thẻ rộng hết bề ngang cho cân đối (chữ đã được ngắt dòng vừa trong thẻ)
        qy = int(top + max(0, (duoi - top - (hq + gap + ha)) / 2))
        ay = qy + hq + gap
        mq = _hop_bo_goc(bw, hq, 26)
        mu = (mq * 255).astype(np.uint8)
        cv2.fillConvexPoly(mu, np.array([[20, 38], [8, 51], [20, 64]], np.int32), 255, cv2.LINE_AA)           # đuôi bong bóng chỉ về avatar
        mq = mu.astype(np.float32) / 255.0
        ma = _hop_bo_goc(bw, ha, 26)
        res = {"q": q, "qt": qt, "a": a, "at": at, "bw": bw, "bx0": bx0, "qy": qy, "ay": ay, "hq": hq, "ha": ha, "mq": mq, "ma": ma,
               "bq": cv2.GaussianBlur(np.roll(mq, 7, axis=0), (0, 0), 7) * 0.20, "ba": cv2.GaussianBlur(np.roll(ma, 7, axis=0), (0, 0), 7) * 0.20}
        x_chu = bx0 + self.PAD
        q.x0, q.y0 = x_chu, qy + self.PAD
        if qt:
            qt.x0, qt.y0 = x_chu, q.y0 + q.h + 8
        a.x0, a.y0 = x_chu, ay + self.PAD
        if at:
            at.x0, at.y0 = x_chu, a.y0 + a.h + 8
        self._qz[n] = res
        return res

    def _ve_quiz(self, world: np.ndarray, t: float, ds: list) -> None:
        """Lượt câu hỏi: câu hỏi được VIẾT dần khi người dẫn hỏi → thanh «Hãy suy nghĩ» đếm ngược → ĐÁP ÁN hiện và được viết khi người dẫn đọc. Sang câu kế thì câu cũ mờ đi."""
        ke, moc = self.ke, self.moc
        m = -1
        for pos, j in enumerate(ds):
            if t >= moc[j][0] - 0.12:
                m = pos
        if m < 0:
            return
        n = ke[ds[m]]["i"]
        jq = next(j for j in ds if ke[j]["i"] == n and ke[j]["el"] == "q")
        u = mem((t - (moc[jq][0] - 0.12)) / 0.35)
        if n > 0 and u < 1.0:
            self._ve_cau_hoi(world, t, ds, n - 1, 1.0 - u, True)
        self._ve_cau_hoi(world, t, ds, n, 1.0, False)

    def _ve_cau_hoi(self, world: np.ndarray, t: float, ds: list, n: int, a: float, cu: bool) -> None:
        """Vẽ nhóm câu hỏi ``n``. ``cu``: câu đã xong đang mờ đi (vẽ đủ cả câu hỏi lẫn đáp án, không viết dần)."""
        ke, moc = self.ke, self.moc
        L = self._dung_quiz(n)
        jq = next(j for j in ds if ke[j]["i"] == n and ke[j]["el"] == "q")
        ja = next(j for j in ds if ke[j]["i"] == n and ke[j]["el"] == "a")
        so = len(self.d["quiz"])
        aq = a if cu else a * mem((t - (moc[jq][0] - 0.12)) / 0.3)
        aa = a if cu else a * mem((t - (moc[ja][0] - 0.12)) / 0.3)
        bx0, qy, ay, bw = L["bx0"], L["qy"], L["ay"], L["bw"]
        dx = 0
        # --- thẻ câu hỏi + avatar người dẫn
        RV.tron(world, (40, 40, 70), L["bq"], bx0 - 20 + dx, qy - 20, aq)
        RV.tron(world, self.NEN_TU, L["mq"], bx0 - 20 + dx, qy - 20, 0.96 * aq)
        dam, nhat = AV.VAI[0]
        rgb, al = self.avt[0]
        noi = not cu and (moc[jq][0] <= t <= moc[jq][1] + 0.05 or moc[ja][0] <= t <= moc[ja][1] + 0.05)
        ax = self.MAR + self.ZON / 2 + dx
        ay0 = qy + 4 + (int(-4 * abs(math.sin(t * 9.0))) if noi else 0)
        RV.tron(world, rgb, al, int(ax - self.AVD / 2), ay0, aq)
        if self.ten_hien:
            tenc = self.ten[0]
            tenc.x0, tenc.y0 = int(ax - tenc.w / 2), qy + 4 + self.AVD + 6
            tenc.ve(world, aq)
        if noi:
            cv2.circle(world, (int(ax), ay0 + self.AVD // 2), int(self.AVD / 2 + 7 + 3 * math.sin(t * 9.0)), dam, 3, cv2.LINE_AA)
        self._ve_pill_o(world, f"{self.nhan('hoi')} {n + 1}/{so}", PK_MAU["hoi"], bx0 + 26 + dx, qy - 24, aq)
        # chữ câu hỏi (tiếng đích) viết tay theo lời hỏi; bản dịch hiện NGAY cùng lúc
        wa, wd = self.cua_so(moc, jq)
        q_ = 1.0 if cu else self._q_but(jq, L["q"], t)
        L["q"].ve(world, aq, quet=q_ if q_ < 1.0 else None)
        if not cu and 0 < q_ < 1:
            self.tip = L["q"].dau_but(q_)
        if L["qt"]:
            qs_ = 1.0 if cu else doan(t, wa, wa + 0.85 * wd)
            L["qt"].ve(world, aq, quet=qs_ if qs_ < 1.0 else None, ngang=True)
        # --- thanh suy nghĩ giữa hai thẻ
        if not cu and moc[jq][1] + 0.05 < t < moc[ja][0] - 0.1:
            self._thanh_nhac(world, bx0 + 6, bx0 + bw - 6, qy + L["hq"] + 38, PK_MAU["hoi"], 1.0 - (t - moc[jq][1]) / max(0.2, moc[ja][0] - moc[jq][1] - 0.1), "nghi")
        # --- thẻ đáp án
        if aa > 0.01:
            RV.tron(world, (40, 40, 70), L["ba"], bx0 - 20, ay - 20, aa)
            RV.tron(world, (226, 246, 234), L["ma"], bx0 - 20, ay - 20, 0.96 * aa)
            self._ve_pill_o(world, self.nhan("dap_an"), (30, 132, 86), bx0 + 26, ay - 24, aa)
            wa, wd = self.cua_so(moc, ja)
            q_ = 1.0 if cu else self._q_but(ja, L["a"], t)
            L["a"].ve(world, aa, quet=q_ if q_ < 1.0 else None)
            if not cu and 0 < q_ < 1:
                self.tip = L["a"].dau_but(q_)
            if L["at"]:
                as_ = 1.0 if cu else doan(t, wa, wa + 0.85 * wd)
                L["at"].ve(world, aa, quet=as_ if as_ < 1.0 else None, ngang=True)

    # ---------------------------------------------------------------- TRANG SÁCH (mặc định cho lượt dạng danh sách)
    def _trang_cua(self, k: int, ld: bool, ds: list) -> tuple:
        """Chia các câu của lượt ``k`` thành TRANG như trang sách (dựng một lần): ([[vị trí trong ds…] mỗi trang], {vị trí: y tâm}, {vị trí: số trang}). Câu xếp nối nhau từ trên xuống;
        hết chỗ thì sang trang mới. Cả trang được canh giữa theo chiều dọc ngay từ đầu (tính theo mọi câu của trang) nên khi câu mới hiện ra, những câu cũ không bị xê dịch."""
        khoa = (k, ld)
        if khoa in self._pg:
            return self._pg[khoa]
        hts = [self._cao_khoi(self.ke[j]["i"], ld) for j in ds]
        top, duoi = self._khung_nhin(bool(self.luot[k]["shadow"]))                  # trang sách không cần dải mờ dài của kiểu cuộn; ``duoi`` đã chừa chỗ ghi nguồn + thanh "Nhắc lại theo"
        them = 0
        trang, cur, c_ = [], [], 0.0
        for pos, h in enumerate(hts):
            can = h + (self.GAP if cur else 0)
            if cur and c_ + can + them > duoi - top:
                trang.append(cur)
                cur, c_, can = [], 0.0, h
            cur.append(pos)
            c_ += can
        if cur:
            trang.append(cur)
        ys: dict = {}
        so_trang: dict = {}
        for n, pg in enumerate(trang):
            tong = sum(hts[p] for p in pg) + self.GAP * (len(pg) - 1) + them
            y = top + max(0.0, (duoi - top - tong) / 2)
            for p in pg:
                ys[p], so_trang[p] = y + hts[p] / 2, n
                y += hts[p] + self.GAP
        self._pg[khoa] = (trang, ys, so_trang)
        return self._pg[khoa]

    def _trang_dang_xem(self, t: float, k: int) -> Optional[tuple[int, int]]:
        """(trang hiện tại, tổng số trang), đếm từ 1, của lượt danh sách ``k`` ở thời điểm ``t``; None nếu chưa có câu nào / không phải kiểu trang."""
        if not self.trang_sach or self.luot_kieu[k] != "list":
            return None
        ds = self.luot_ds[k]
        m = -1
        for pos, j in enumerate(ds):
            if t >= self.moc[j][0] - 0.12:
                m = pos
        if m < 0:
            return None
        trang, _, so_trang = self._trang_cua(k, self.luot_ld[k], ds)
        return so_trang[m] + 1, len(trang)

    def _ve_trang(self, world: np.ndarray, t: float, k: int, ld: bool, ds: list) -> None:
        """Lượt dạng danh sách theo kiểu TRANG SÁCH: câu hiện ra lần lượt từ trên xuống, được viết dần đúng lúc đọc; các câu đã đọc ở lại trên trang (hơi nhạt đi); hết trang thì
        trang cũ lướt sang trái rồi mờ, trang mới hiện. Không zoom, không cuộn."""
        moc = self.moc
        m = -1
        for pos, j in enumerate(ds):
            if t >= moc[j][0] - 0.12:
                m = pos
        if m < 0:
            return
        trang, ys, so_trang = self._trang_cua(k, ld, ds)
        p = so_trang[m]
        sh = bool(self.luot[k]["shadow"])
        vt, vb = self._khung_nhin(sh)

        def y_cua(pos: int, xong: bool) -> float:
            """Tâm dọc của khối ``pos``. Câu CAO hơn trang (luôn đứng một mình một trang) thì cuộn theo ngòi bút; ``xong``: đã viết xong (đáy khối ở đáy khung nhìn)."""
            ji = self.ke[ds[pos]]["i"]
            if not self._la_cao(ji, ld, sh):
                return ys[pos]
            hb = self._cao_khoi(ji, ld)
            f = float(hb) if xong else self._but_cao(self._doan_cao(ds[pos], self.dong[ji], ld), t, hb)
            return self._dinh_cao(hb, f, vt, vb) + hb / 2

        t_trang = moc[ds[trang[p][0]]][0] - 0.12                                     # lúc trang hiện tại bắt đầu
        if p > 0:                                                                     # trang trước còn đang lật đi
            u = mem((t - t_trang) / 0.45)
            if u < 1.0:
                for pos in trang[p - 1]:
                    tg_ = self._tong_the(ds[pos], ld) if self._la_cao(self.ke[ds[pos]]["i"], ld, sh) else None
                    if tg_ is not None:                                               # câu cao đang ở chế độ TỔNG THỂ: lật đi từ đúng cỡ thu nhỏ đó
                        self._ve_khoi_thu(world, ds, pos, tg_[1], 0.7 * (1.0 - u), tg_[0], t, ld, sx=-int(80 * u))
                    else:
                        self._ve_khoi(world, ds, pos, y_cua(pos, True), 0.7 * (1.0 - u), False, t, ld, sx=-int(80 * u))
        for pos in trang[p]:
            if pos > m:
                break
            if pos == m:
                a = mem((t - (moc[ds[pos]][0] - 0.12)) / 0.3)
            else:                                                                     # câu đã đọc: nhạt dần xuống còn 0,7 khi câu kế tiếp bắt đầu
                a = 1.0 - 0.3 * mem((t - (moc[ds[pos + 1]][0] - 0.12)) / 0.4)
            if a > 0.01:
                self._ve_khoi(world, ds, pos, y_cua(pos, pos != m), a, pos == m, t, ld)

    def _ve_danh_sach(self, world: np.ndarray, t: float, k: int, ld: bool, ds: list) -> None:
        moc = self.moc
        m = -1
        for pos, j in enumerate(ds):
            if t >= moc[j][0] - 0.12:
                m = pos
        if m < 0:
            return
        idx = [self.ke[j]["i"] for j in ds]
        hts = [self._cao_khoi(i, ld) for i in idx]
        tam, c_ = [], 0.0
        for h in hts:
            tam.append(c_ + h / 2)
            c_ += h + self.GAP
        sh = bool(self.luot[k]["shadow"])
        vt, vb = self._khung_nhin(sh)
        y_f = (vt + vb) / 2                                                        # câu đang đọc nằm giữa khung nhìn
        cao = [self._la_cao(i, ld, sh) for i in idx]

        def cuon_o(pos: int, f: float) -> float:
            """Toạ độ cuộn khi xem câu ``pos``: ở giữa khung; câu CAO hơn khung nhìn (đọc nguyên văn báo) thì đỉnh / đáy khối bám theo ngòi bút (``f`` = ngòi cách đỉnh khối)."""
            if not cao[pos]:
                return tam[pos]
            return y_f + tam[pos] - (self._dinh_cao(hts[pos], f, vt, vb) + hts[pos] / 2)

        a_m = moc[ds[m]][0] - 0.12
        c_m = cuon_o(m, self._but_cao(self._doan_cao(ds[m], self.dong[idx[m]], ld), t, hts[m]) if cao[m] else 0.0)
        u = mem((t - a_m) / 0.6) if m >= 1 else 1.0
        c_truoc = cuon_o(m - 1, float(hts[m - 1])) if m >= 1 else c_m                  # câu trước đã viết xong (câu cao: đáy khối ở đáy khung nhìn)
        c_cuon = c_truoc + (c_m - c_truoc) * u
        ci = (m - 1) + u if m >= 1 else 0.0                                       # chỉ số liên tục của câu đang ở giữa
        for pos in range(m + 1):
            y = y_f + tam[pos] - c_cuon
            dist = ci - pos
            a = mem((t - (moc[ds[pos]][0] - 0.12)) / 0.3) if pos == m else max(0.0, min(1.0, 1.0 - 0.42 * dist))
            if a <= 0.01:
                continue
            if pos == m - 1 and cao[pos]:                                            # câu cao vừa xong đang ở chế độ TỔNG THỂ (thu nhỏ): trôi lên từ đúng chỗ đó, không "nhảy" về cỡ cũ
                tg_ = self._tong_the(ds[pos], ld)
                if tg_ is not None:
                    y_ov = tg_[1] - (c_cuon - c_truoc)
                    if y_ov + hts[pos] * tg_[0] / 2 >= self.dau - 30:
                        self._ve_khoi_thu(world, ds, pos, y_ov, a, tg_[0], t, ld)
                    continue
            if y + hts[pos] / 2 < self.dau - 30:                                    # đã trôi hẳn lên khỏi vùng nhìn thấy
                continue
            self._ve_khoi(world, ds, pos, y, a, pos == m, t, ld)
            wa_, wd_ = self.cua_so(moc, ds[m])
            if pos == m and self.cam_on and not cao[m] and t < wa_ + wd_ + 0.3:     # camera nhích nhẹ vào bong bóng + avatar đang đọc — chỉ trong lúc VIẾT; xong thì lùi ra xem tổng thể
                dg = self.dong[self.ke[ds[pos]]["i"]]
                hb = self._cao_khoi(self.ke[ds[pos]]["i"], ld)
                self.cam_muc = self.cam_rect((min(dg["bx0"], dg["ax"] - self.COT / 2), y - hb / 2, max(dg["bx0"] + dg["bw"], dg["ax"] + self.COT / 2), y + hb / 2), 1.04)

    def _ve_khoi(self, world: np.ndarray, ds: list, pos: int, y_c: float, a: float, hien: bool, t: float, ld: bool, sx: int = 0) -> None:
        """Vẽ một khối (avatar + bong bóng + chữ) của câu ``ds[pos]`` tâm ở ``y_c``, độ đậm ``a``; ``hien`` = câu đang đọc (chữ được viết dần); ``sx`` = độ lệch ngang (lật trang)."""
        j_seg = ds[pos]
        s = self.ke[j_seg]
        j = s["i"]
        dg = self.dong[j]
        ln = self.d["lines"][j]
        sp = ln["s"]
        h = self._cao_khoi(j, ld)
        cao = self._la_cao(j, ld, bool(s["shadow"]))
        if hien and cao:
            tt_ = self._tong_the(j_seg, ld)
            if tt_ is not None and t >= tt_[2]:                                      # câu cao đã viết xong: lùi ra xem TỔNG THỂ (thu nhỏ cả khối cho vừa khung nhìn)
                u = mem((t - tt_[2]) / 0.9)
                self._ve_khoi_thu(world, ds, pos, y_c + (tt_[1] - y_c) * u, a, 1.0 + (tt_[0] - 1.0) * u, t, ld, sx)
                return
        top = int(y_c - h / 2)
        dam, nhat = AV.VAI[sp % 4]
        m, bong = self._bong_bong(j, ld)
        bh = self._cao_bb(j, ld)
        dx =(int((1.0 - a) * 46) * (-1 if dg["ben"] == 0 else 1) if hien else 0) + sx   # bong bóng trượt vào từ phía người nói
        bx0, by0 = dg["bx0"] + dx, top
        RV.tron(world, (40, 40, 70), bong, bx0 - 20, by0 - 20, a)
        RV.tron(world, nhat, m, bx0 - 20, by0 - 20, 0.96 * a)
        rgb, al = self.avt[sp]
        a0, b0 = self.moc[j_seg]
        noi = hien and a0 <= t <= b0 + 0.05
        nhun = int(-4 * abs(math.sin(t * 9.0))) if noi else 0                         # avatar nhún nhẹ khi đang nói
        ay_goc = top + 4
        if cao:                                                                      # khối cao hơn khung nhìn: avatar bám mép trên khung nhìn để người xem luôn biết ai đang nói
            ay_goc = int(min(max(ay_goc, self._khung_nhin(bool(s["shadow"]))[0] + 4), top + h - self.AVD - 8))
        ax0, ay0 = int(dg["ax"] + dx - self.AVD / 2), ay_goc + nhun
        RV.tron(world, rgb, al, ax0, ay0, a)
        if self.ten_hien:                                                            # tên chỉ hiện khi có từ 2 người đọc (một người: avatar nhỏ là đủ)
            tenc = self.ten[sp]
            tenc.x0, tenc.y0 = int(dg["ax"] + dx - tenc.w / 2), ay_goc + self.AVD + 6
            tenc.ve(world, a)
        if noi:                                                                      # vòng sáng nhấp nháy quanh avatar
            cv2.circle(world, (int(dg["ax"] + dx), ay0 + self.AVD // 2), int(self.AVD / 2 + 7 + 3 * math.sin(t * 9.0)), dam, max(2, int(3 * a)), cv2.LINE_AA)
        # --- chữ trong bong bóng
        tt, pp, rr, tg = dg["t"], dg["p"], (dg["r"] if ld else None), dg["g"]
        x_chu = dg["bx0"] + self.PAD + dx
        y = top + self.PAD
        tt.x0, tt.y0 = x_chu, int(y)
        y += tt.h + 8
        if pp:
            pp.x0, pp.y0 = x_chu, int(y)
            y += pp.h + 6
        if rr:
            rr.x0, rr.y0 = x_chu, int(y)
            y += rr.h + 6
        if tg:
            tg.x0, tg.y0 = x_chu, int(y)
        if tg:
            tg.ve(world, a)                                                            # nhãn "Trích dẫn từ …" hiện ngay cùng bong bóng
        if not hien:
            for c in (tt, pp, rr):
                if c:
                    c.ve(world, a)
            return
        if cao:                                                                        # câu CAO: viết LẦN LƯỢT chữ chính → phiên âm → bản dịch; màn hình cuộn theo ngòi bút (chỉ phần tới lượt mới hiện)
            for c, _, w0, w1, ngang, jj in self._doan_cao(j_seg, dg, ld):
                q = self._q_but(jj, c, t) if jj is not None else doan(t, w0, w1)
                c.ve(world, a, quet=q, ngang=ngang)
                if not ngang and 0 < q < 1:
                    self.tip = c.dau_but(q)
        else:
            wa, wd = self.cua_so(self.moc, j_seg)
            q = self._q_but(j_seg, tt, t)                                              # chữ tiếng đích được viết tay (theo nét), bút đi theo nhịp + chỗ nghỉ của giọng
            tt.ve(world, a, quet=q)
            if 0 < q < 1:
                self.tip = tt.dau_but(q)
            for c in (pp, rr):                                                         # phiên âm + bản dịch CHẠY DẦN theo lời đọc (xong trước khi hết câu), không viết tay
                if c:
                    c.ve(world, a, quet=doan(t, wa, wa + 0.85 * wd), ngang=True)
        # --- lượt nhại theo: khoảng lặng sau câu = thanh đếm ngược "Nhắc lại theo"
        if s["shadow"]:
            nxt = self.moc[j_seg + 1][0] if j_seg + 1 < len(self.moc) else b0 + 1.0
            if b0 + 0.05 < t < nxt and nxt - b0 >= 0.6:
                self._thanh_nhac(world, dg["bx0"] + 6, dg["bx0"] + dg["bw"] - 6, by0 + bh + 34, dam, 1.0 - (t - b0) / max(0.2, nxt - b0))

    def _ve_khoi_thu(self, world: np.ndarray, ds: list, pos: int, y_c: float, a: float, sc: float, t: float, ld: bool, sx: int = 0) -> None:
        """Vẽ khối của câu CAO ``ds[pos]`` đã VIẾT XONG ở tỷ lệ ``sc`` (≤ 1) với tâm dọc ``y_c``: bong bóng + chữ thu nhỏ quanh tâm ngang của bong bóng, để người xem thấy TỔNG THỂ cả khối
        (chữ gốc + phiên âm + bản dịch cùng lúc). Avatar giữ cỡ cũ và bám góc bong bóng."""
        sc = round(sc * 100) / 100.0
        j_seg = ds[pos]
        s = self.ke[j_seg]
        j = s["i"]
        dg = self.dong[j]
        sp = self.d["lines"][j]["s"]
        h = self._cao_khoi(j, ld)
        bh = self._cao_bb(j, ld)
        top = int(y_c - h * sc / 2)
        bx0 = int(dg["bx0"] + sx + dg["bw"] * (1.0 - sc) / 2)                         # thu nhỏ quanh tâm ngang của bong bóng
        dam, nhat = AV.VAI[sp % 4]
        m, bong = self._bong_bong(j, ld, sc)
        RV.tron(world, (40, 40, 70), bong, bx0 - 20, top - 20, a)
        RV.tron(world, nhat, m, bx0 - 20, top - 20, 0.96 * a)
        rgb, al = self.avt[sp]
        a0, b0 = self.moc[j_seg]
        noi = a0 <= t <= b0 + 0.05
        ax = bx0 + (dg["ax"] - dg["bx0"])
        ay0 = top + 4 + (int(-4 * abs(math.sin(t * 9.0))) if noi else 0)
        RV.tron(world, rgb, al, int(ax - self.AVD / 2), ay0, a)
        if self.ten_hien:
            tenc = self.ten[sp]
            tenc.x0, tenc.y0 = int(ax - tenc.w / 2), top + 4 + self.AVD + 6
            tenc.ve(world, a)
        if noi:
            cv2.circle(world, (int(ax), ay0 + self.AVD // 2), int(self.AVD / 2 + 7 + 3 * math.sin(t * 9.0)), dam, max(2, int(3 * a)), cv2.LINE_AA)
        y = top + self.PAD * sc
        a_pr = a                                                                    # phiên âm + dịch của câu CAO có bút đi theo giọng: chỉ mờ dần hiện khi lùi ra xem tổng thể (xem _doan_cao)
        tg_ = self._tong_the(j_seg, ld) if self._cua_tieng(j_seg) is not None else None
        if tg_ is not None:
            a_pr = a * mem(doan(t, tg_[2] + 0.15, tg_[2] + 0.75))
        for c, khe in ((dg["t"], 8), (dg["p"], 6), (dg["r"] if ld else None, 6), (dg["g"], 0)):
            if c is None:
                continue
            ct = self._thu_chu(c, sc) if sc < 0.995 else c
            ct.x0, ct.y0 = int(bx0 + self.PAD * sc), int(y)
            ct.ve(world, a_pr if (c is dg["p"] or c is dg["r"]) else a)
            y += ct.h + khe * sc
        if s["shadow"]:                                                              # lượt nhại theo: thanh đếm ngược dưới khối
            nxt = self.moc[j_seg + 1][0] if j_seg + 1 < len(self.moc) else b0 + 1.0
            if b0 + 0.05 < t < nxt and nxt - b0 >= 0.6:
                self._thanh_nhac(world, bx0 + 6, bx0 + int(dg["bw"] * sc) - 6, top + int(bh * sc) + 34, dam, 1.0 - (t - b0) / max(0.2, nxt - b0))

    def _thanh_nhac(self, world: np.ndarray, x0: int, x1: int, yc: int, mau: tuple, r_: float, khoa: str = "nhac") -> None:
        """Thanh đếm ngược "Nhắc lại theo" (shadowing) / "Hãy suy nghĩ" (câu hỏi): nhãn bên trái, thanh còn lại thu dần về trái."""
        txt = self.nhan(khoa)
        c = self._pill_chu.get(txt)
        if c is None:
            c = Chu(txt, self._font_nhan(txt), 26 * self.co, mau, 0, 0, 600, "trai", 1)
            self._pill_chu[txt] = c
        c.x0, c.y0 = int(x0), int(yc - c.h / 2)
        c.ve(world, 1.0)
        xb = int(x0 + c.w + 14)
        cv2.line(world, (xb, int(yc)), (int(x1), int(yc)), (226, 226, 236), 6, cv2.LINE_AA)
        cv2.line(world, (xb, int(yc)), (int(xb + (x1 - xb) * max(0.0, r_)), int(yc)), mau, 6, cv2.LINE_AA)

    # ---------------------------------------------------------------- SLIDE BÀI GIẢNG
    DT = (176, 84, 30)                                                                # màu bản dịch trên slide
    NEN_TU = (232, 242, 255)
    NEN_NP = (255, 240, 220)

    def _chia_cot(self, muc: list, so_cot: int) -> list:
        per = -(-len(muc) // so_cot)
        return [muc[c * per:(c + 1) * per] for c in range(so_cot) if muc[c * per:(c + 1) * per]]

    def _tom_tat(self, ln: dict, co: float, tx0: int, tw: int) -> Chu:
        """Câu gốc THU GỌN MỘT DÒNG (vài chữ đầu + "…") cho trang từ vựng / ngữ pháp của câu quá cao — chỉ để người xem biết đang giảng câu nào."""
        van = self.ngoac(ln["text"]) if ln.get("quote") else ln["text"]
        c = None
        for n in (26, 20, 15, 10, 6):
            c = self.chu(_cat_dau(van, n), co, MUC, tx0, 0, tw, "trai", 2, self._f_t)
            if len(c.dong) <= 1:
                break
        return c

    def _xep_trang(self, ln: dict, vs: list, gs: list, gon: bool, he_min: float, bat_buoc: bool, tom_tat: bool = False) -> Optional[dict]:
        """Bố cục MỘT trang slide: thẻ câu gốc + (thẻ từ vựng) + (thẻ ngữ pháp). Chữ tự thu nhỏ (không dưới ``he_min``) cho vừa khung; nhiều mục thì chia 2 cột.
        Không vừa mà không ``bat_buoc`` → ``None`` (người gọi sẽ tách thành hai trang)."""
        W, H, doc, M = self.W, self.H, self.doc, self.MAR
        sp = ln["s"]
        top_y = self.dau + 6
        bot_y = H - 52 - (self.PHU_CAO if self.co_phu else 0)                          # có phụ đề ở đáy thì slide co lại một chút để không bị che
        gap = 34 if self.nhac else 18
        card_w = W - 2 * M
        av = int(self.AVD * (0.8 if gon else 1.0))
        av_col = max(av + 26, self.ZON + 8) if self.ten_hien else av + 26             # có tên dưới avatar (từ 2 người đọc): cột đủ rộng để tên không chạm vào chữ
        tx0 = M + 22 + av_col
        tw = card_w - av_col - 44
        f_v = self._font_cho(self.me, " ".join(v["m"] + v["r"] for v in vs) or "a", "subtitle", du_phong=(("vi", "subtitle"), ("en", "subtitle")))
        f_g = self._font_me(" ".join(g["e"] for g in gs) or "a")
        lab_h = 58
        he = 1.0
        #  KHÔNG đặt giới hạn số dòng thấp ở đây: ``Chu`` / ``ChuRuby`` cắt thẳng tay ở ``dong_toi_da`` (bản dịch / phiên âm bị CỤT giữa chừng — người dùng báo 06-10-2026: "…rằng: "Vụ việc này"), còn
        #  ChuRuby thì dồn phần thừa vào dòng cuối nên chữ tràn ra ngoài thẻ. Hết chỗ thì vòng co chữ bên dưới lo (rồi tách trang / cuộn), không bỏ chữ.
        while True:
            co_t = (52 if not doc else 40) * he * (0.8 if gon else 1.0)
            t_ = self._tom_tat(ln, co_t, tx0, tw) if tom_tat else self.chu(self.ngoac(ln["text"]) if ln.get("quote") else ln["text"], co_t, MUC, tx0, 0, tw, "trai", DONG_DAY_DU, self._f_t)
            p_ = self.chu(ln["phonetic"], (28 if not doc else 24) * he, PHO, tx0, 0, tw, "trai", DONG_DAY_DU, self._f_p) if (ln["phonetic"] and not gon and not tom_tat) else None
            r_ = self.chu(ln["translation"], (34 if not doc else 28) * he * (0.85 if gon else 1.0), self.DT, tx0, 0, tw, "trai", DONG_DAY_DU, self._f_r) if (ln["translation"] and not tom_tat) else None
            ph = [x for x in (t_, p_, r_) if x]
            h1 = int(max(36 + sum(x.h for x in ph) + 8 * (len(ph) - 1), av + 40))
            if vs and gs and not doc:
                vw = int((card_w - gap) * 0.56)
                gw = card_w - gap - vw
            else:
                vw = gw = card_w
            # --- từ vựng: 2 cột khi nhiều
            cot_v, hv, colw_v = [], 0, 0
            if vs:
                n_cot = 2 if (len(vs) >= 4 and vw >= 700) else 1
                colw_v = int((vw - 44 - (n_cot - 1) * 26) / n_cot)
                muc = []
                for n, v in enumerate(vs):
                    w_ = self.chu(v["w"], 34 * he, DAM[n % 4], 0, 0, colw_v, "trai", 1, self._f_t)
                    r2 = self.chu((v["r"] + "  ·  " if v["r"] else "") + v["m"], 29 * he, SLATE, 0, 0, colw_v, "trai", DONG_DAY_DU, f_v)
                    inline = w_.w + 18 + r2.w <= colw_v
                    lech = max(0, int(w_.base - r2.base))                                  # nghĩa nằm CÙNG ĐƯỜNG CƠ SỞ với từ (không canh giữa theo chiều cao: furigana làm khối từ cao hơn nên nghĩa bị lệch lên)
                    muc.append((n, w_, r2, inline, max(w_.h, lech + r2.h) if inline else w_.h + 2 + r2.h, lech))
                cot_v = self._chia_cot(muc, n_cot)
                hv = lab_h + 8 + max(sum(it[4] + 12 for it in c) for c in cot_v) + 10
            # --- ngữ pháp: 2 cột khi thẻ rộng và có ≥ 2 mẫu
            cot_g, hg, colw_g = [], 0, 0
            if gs:
                n_cot = 2 if (len(gs) >= 2 and gw >= 900) else 1
                colw_g = int((gw - 44 - (n_cot - 1) * 26) / n_cot)
                muc = []
                for n, g in enumerate(gs):
                    p2 = self.chu(g["p"], 32 * he, DAM[(n + 2) % 4], 0, 0, colw_g, "trai", DONG_DAY_DU, self._f_t)
                    e2 = self.chu(g["e"], 27 * he, SLATE, 0, 0, colw_g, "trai", DONG_DAY_DU, f_g)
                    muc.append((n, p2, e2, p2.h + 2 + e2.h))
                cot_g = self._chia_cot(muc, n_cot)
                hg = lab_h + 8 + max(sum(it[3] + 16 for it in c) for c in cot_g) + 10
            disp = bot_y - (top_y + h1 + gap)
            need = (hv + (gap if hv and hg else 0) + hg) if (doc and vs and gs) else max(hv, hg)
            if (not (vs or gs) and h1 <= bot_y - top_y) or (vs or gs) and need <= disp:
                break
            if he <= he_min:
                if not bat_buoc:
                    return None
                break
            he = max(he_min, he * 0.92)
        sl: dict = {"cards": [], "el": {}, "sp": sp, "nhat": AV.VAI[sp % 4][1], "dam": AV.VAI[sp % 4][0], "av": av, "gon": gon,
              "cao": not (vs or gs) and h1 > bot_y - top_y}                               # cao: thẻ câu MỘT MÌNH vẫn cao hơn khung nhìn → bút viết kèm cuộn (``_cuon_cao``)
        y1 = top_y if (vs or gs or h1 >= bot_y - top_y) else int((top_y + bot_y - h1) / 2)          # thẻ câu đứng một mình mà cao hơn khung nhìn thì bắt đầu từ ĐỈNH (không đẩy đỉnh ra ngoài khung)
        sl["rect"] = (M, y1, M + card_w, y1 + h1)
        sl["av_xy"] = (M + 22, y1 + 18)
        y = y1 + (h1 - (sum(x.h for x in ph) + 8 * (len(ph) - 1))) / 2
        for x in ph:
            x.y0 = int(y)
            y += x.h + 8
        sr = sl["rect"]
        sl["el"]["t"] = {"parts": [(t_, 1.0)], "rect": (tx0 - 14, t_.y0 - 8, tx0 + tw, (p_ or t_).y0 + (p_ or t_).h + 8), "cam": sr}
        if p_:
            sl["el"]["p"] = {"parts": [(p_, 1.0)], "rect": (tx0 - 14, p_.y0 - 4, tx0 + tw, p_.y0 + p_.h + 4), "cam": sr}
        if r_:
            sl["el"]["r"] = {"parts": [(r_, 1.0)], "rect": (tx0 - 14, r_.y0 - 6, tx0 + tw, r_.y0 + r_.h + 6), "cam": sr}
        cy0 = y1 + h1 + gap
        v_xy = g_xy = None
        if vs:
            vx0, vy0 = M, cy0
            card = {"rect": (vx0, vy0, vx0 + vw, vy0 + hv), "nen": self.NEN_TU, "lab": "vocab", "x": vx0 + 22, "y": vy0 + 10, "mau": DAM[0]}
            sl["cards"].append(card)
            for ci, col in enumerate(cot_v):
                yy = vy0 + lab_h + 8
                xx = vx0 + 22 + ci * (colw_v + 26)
                for n, w_, r2, inline, hh, lech in col:
                    w_.x0, w_.y0 = xx, int(yy)
                    r2.x0, r2.y0 = (int(xx + w_.w + 18), int(yy + lech)) if inline else (xx, int(yy + w_.h + 2))
                    rc = (xx - 12, int(yy) - 5, xx + colw_v + 12, int(yy + hh) + 5)
                    sl["el"][f"v{n}"] = {"parts": [(w_, 1.0)], "rect": rc, "cam": card["rect"]}
                    sl["el"][f"m{n}"] = {"parts": [(r2, 1.0)], "rect": rc, "cam": card["rect"]}
                    yy += hh + 12
        if gs:
            if doc and vs:
                gx0, gy0 = M, cy0 + hv + gap
            else:
                gx0, gy0 = (M + vw + gap if vs else M), cy0
            card = {"rect": (gx0, gy0, gx0 + gw, gy0 + hg), "nen": self.NEN_NP, "lab": "gram", "x": gx0 + 22, "y": gy0 + 10, "mau": DAM[2]}
            sl["cards"].append(card)
            for ci, col in enumerate(cot_g):
                yy = gy0 + lab_h + 8
                xx = gx0 + 22 + ci * (colw_g + 26)
                for n, p2, e2, hh in col:
                    p2.x0, p2.y0 = xx, int(yy)
                    e2.x0, e2.y0 = xx, int(yy + p2.h + 2)
                    rc = (xx - 12, int(yy) - 5, xx + colw_g + 12, int(yy + hh) + 5)
                    sl["el"][f"g{n}"] = {"parts": [(p2, 1.0)], "rect": rc, "cam": card["rect"]}
                    sl["el"][f"e{n}"] = {"parts": [(e2, 1.0)], "rect": rc, "cam": card["rect"]}
                    yy += hh + 16
        for c in sl["cards"]:
            x0, y0, x1, y1_ = c["rect"]
            m = _hop_bo_goc(int(x1 - x0), int(y1_ - y0), 24)
            c["m"] = m
            c["bong"] = cv2.GaussianBlur(np.roll(m, 7, axis=0), (0, 0), 7) * 0.20
        m = _hop_bo_goc(int(sr[2] - sr[0]), int(sr[3] - sr[1]), 24)
        sl["ms"] = {"m": m, "bong": cv2.GaussianBlur(np.roll(m, 7, axis=0), (0, 0), 7) * 0.20}
        for d_ in sl["el"].values():                                                  # vị trí gốc — để cộng độ lệch khi trượt vào
            for c, _ in d_["parts"]:
                c._bx, c._by = c.x0, c.y0
        return sl

    def _dung_slide(self, i: int) -> dict:
        """Slide của câu ``i`` (dựng một lần): một trang nếu vừa; nhiều từ / ngữ pháp mà chữ phải nhỏ hơn ~78% thì TÁCH HAI TRANG (trang từ vựng, trang ngữ pháp — trang sau
        giữ lại thẻ câu gốc thu gọn để người xem không mất ngữ cảnh)."""
        if i in self._slide:
            return self._slide[i]
        ln = self.d["lines"][i]
        vs, gs = ln.get("vocab") or [], ln.get("grammar") or []
        p1 = self._xep_trang(ln, vs, gs, False, 0.8, False)                            # chữ slide không nhỏ hơn 80 % (từ 34 px → ≥ 27 px); hết chỗ thì TÁCH TRANG chứ không thu nhỏ thêm
        if p1 is not None:
            res = {"pages": [p1], "page_of": {}}
        else:
            ps = self._xep_trang(ln, [], [], False, 0.7, True)                            # thẻ câu gốc (+ phiên âm + dịch) MỘT MÌNH
            if ps["cao"]:
                #  Câu gốc một mình đã cao hơn khung nhìn (người dùng hỏi 06-10-2026: chữ Hán chiếm hết màn hình thì sao): bản cũ nhét thẻ từ vựng bên dưới → bản dịch tràn ra ngoài đáy khung,
                #  bị dòng ghi nguồn đè, thẻ từ vựng nằm ngoài màn hình. Nay: trang 0 = CHỈ câu (bút viết kèm cuộn, xong cuộn về đáy xem bản dịch); các trang sau = thẻ từ vựng / ngữ pháp với câu
                #  gốc thu gọn MỘT dòng.
                cac, page_of = [ps], {e: 0 for e in ps["el"]}
                for ten_t, vv, gg, gon_t in (("v", vs, [], False), ("g", [], gs, True)):
                    if not (vv or gg):
                        continue
                    pg = self._xep_trang(ln, vv, gg, gon_t, 0.7, True, True)
                    pg["el"]["t"]["statico"] = True
                    cac.append(pg)
                    page_of.update({e: len(cac) - 1 for e in pg["el"] if e != "t"})
                res = {"pages": cac, "page_of": page_of}
            else:
                pa = self._xep_trang(ln, vs, [], False, 0.7, True)
                pb = self._xep_trang(ln, [], gs, True, 0.7, True)
                res = {"pages": [pa, pb], "page_of": {**{e: 0 for e in pa["el"]}, **{e: 1 for e in pb["el"] if e not in ("t", "p", "r")}}}
                for e in ("t", "p", "r"):                                                # trang 2 chỉ nhắc lại câu gốc / dịch, không viết lại
                    if e in pb["el"]:
                        pb["el"][e]["statico"] = True
        self._slide[i] = res
        return res

    def _cuon_muc(self, lay: dict, el: str) -> float:
        """Độ CUỘN LÊN (px) để phần ``el`` của slide hiện TRỌN trên phụ đề / chân trang. Trang slide dài (câu gốc dài + thẻ từ vựng / ngữ pháp bên dưới) thì thẻ đang giảng nằm thấp, bị phụ đề che
        (người dùng báo 05-10-2026): cuộn cả trang lên vừa đủ để thấy hết thẻ chứa phần đang giảng (câu gốc / phiên âm / dịch thì chính khối chữ), nhưng KHÔNG cuộn quá làm phần trên của nó chui vào dải tiêu đề."""
        d = lay["el"].get(el)
        if d is None:
            return 0.0
        r = d["rect"] if el in ("t", "p", "r") else d["cam"]
        vis_top = self.dau + 30                                                    # dưới dải tiêu đề đang dày thêm khi cuộn (xem ``_ve_slide``)
        vis_bot = self.H - 52 - (self.PHU_CAO if self.co_phu else 0)
        can = r[3] + 12 - vis_bot
        return float(max(0.0, min(can, r[1] - vis_top))) if can > 0 else 0.0

    def _cua_so_cao(self, lay: dict, a: float, b: float, jt: Optional[int] = None) -> dict:
        """Cửa sổ viết của trang slide CAO. CÓ mốc tiếng thật của đoạn ``jt``: chữ gốc viết suốt KHOẢNG GIỌNG ĐỌC (bút đi theo giọng — ``ev["_jt"]``), phiên âm rồi bản dịch (theo chiều cao khối) chạy
        dần SAU khi giọng đọc hết, trong lúc màn hình trôi về đáy thẻ. Không có thì như cách cũ: chữ gốc → phiên âm → bản dịch LẦN LƯỢT trong lúc đọc (thời gian chia theo chiều cao khối, như
        :meth:`_doan_cao` cũ) để ngòi bút đi từ đầu tới cuối và màn hình cuộn theo, bản dịch chỉ chạy ra khi màn hình đã cuộn tới nó."""
        muc = [(e, lay["el"][e]["parts"][0][0].h) for e in ("t", "p", "r") if e in lay["el"]]
        co = self._cua_tieng(jt) if jt is not None else None
        if co is not None:
            a1, b1 = co
            ra = {"t": (a1, b1 - a1), "_jt": jt}
            phu = [(e, h) for e, h in muc if e != "t"]
            t2 = max(b1 + 1.0, self._het_cho(jt))
            tp, tg = float(sum(h for _, h in phu)) or 1.0, b1
            for e, h in phu:
                d = (t2 - b1) * h / tp
                ra[e] = (tg, d)
                tg += d
            return ra
        t0, t1 = a + 0.2, max(a + 2.0, b - 0.5)
        tong = float(sum(h for _, h in muc)) or 1.0
        ra, tg = {}, t0
        for e, h in muc:
            d = (t1 - t0) * h / tong
            ra[e] = (tg, d)
            tg += d
        return ra

    def _cuon_cao(self, lay: dict, t: float, ev: dict) -> float:
        """Độ CUỘN LÊN (px) của trang slide CAO tại ``t``: bám ngòi bút khi viết chữ gốc (ngòi bút ~42 % chiều cao vùng nhìn), viết xong thì trôi về ĐÁY thẻ để thấy phiên âm / bản dịch."""
        vt = self.dau + 30.0
        vb = float(self.H - 52 - (self.PHU_CAO if self.co_phu else 0))
        cuon_max = max(0.0, lay["rect"][3] + 12 - vb)
        a_t, d_t = ev["t"]
        ct = lay["el"]["t"]["parts"][0][0]
        jt = ev.get("_jt")                                                          # có mốc tiếng thật: tiến độ viết theo giọng (cùng đường với chữ đang vẽ)
        q = self._q_but(jt, ct, t) if jt is not None else doan(t, a_t, a_t + d_t)
        if q <= 0:
            return 0.0
        #  TUYẾN TÍNH theo tiến độ viết, KHÔNG dùng toạ độ đầu bút thật (``dau_but``): ngòi bút nhảy giữa hàng furigana và hàng chữ chính, giữa các nét — cuộn bám theo nó rung 60–80 px mỗi khung
        #  hình, lên xuống hơn 200 lần trong một câu (người dùng báo "nháy liên tục", 2.15.8). Lượt đọc (``_but_cao``) cũng dùng đường tuyến tính này.
        y_goc = ct._by + q * ct.h
        theo = min(cuon_max, max(0.0, y_goc - (vt + 0.42 * (vb - vt))))
        if q < 1:
            return theo
        return theo + (cuon_max - theo) * mem(doan(t, a_t + d_t, a_t + d_t + 1.0))

    def _ve_slide(self, world: np.ndarray, t: float, ds: list) -> None:
        """Slide của câu đang đọc: mỗi phần được VIẾT đúng lúc đoạn tiếng của nó được đọc, phần đang đọc được tô nền, camera zoom vào thẻ đang đọc, thẻ trượt vào khi đổi
        câu / đổi trang."""
        ke, moc = self.ke, self.moc
        m = -1
        for pos, j in enumerate(ds):
            if t >= moc[j][0] - 0.12:
                m = pos
        if m < 0:
            return
        j_act = ds[m]
        i = ke[j_act]["i"]
        el_act = ke[j_act]["el"]
        sl = self._dung_slide(i)
        pages = sl["pages"]
        p = sl["page_of"].get(el_act, 0) if len(pages) > 1 else 0
        trang = lambda el: (sl["page_of"].get(el, 0) if len(pages) > 1 else 0)  # noqa: E731
        dau_trang = next(j for j in ds if ke[j]["i"] == i and (trang(ke[j]["el"]) == p or (p == 0)))
        if p > 0:
            dau_trang = next(j for j in ds if ke[j]["i"] == i and trang(ke[j]["el"]) == p)
        a_p = mem((t - (moc[dau_trang][0] - 0.1)) / 0.4)
        dx, dy = (int((1 - a_p) * 60) if p > 0 else 0), int((1 - a_p) * 28)
        lay = pages[p]
        ev, jel = {}, {}
        for j in ds:
            if ke[j]["i"] == i:
                ev[ke[j]["el"]] = self.cua_so(moc, j)
                jel[ke[j]["el"]] = j
        # phần PHỤ (phiên âm, bản dịch, nghĩa của từ, giải thích mẫu) HIỆN CÙNG LÚC với phần chính của nó (câu gốc / từ / mẫu) — không có đoạn riêng, không bắt người xem chờ
        for el in lay["el"]:
            if el not in ev:
                chinh = "t" if el in ("p", "r") else ("v" + el[1:]) if el[0] == "m" else ("g" + el[1:]) if el[0] == "e" else None
                if chinh in ev:
                    ev[el] = ev[chinh]
        if lay.get("cao") and "t" in lay["el"]:
            jt = next(j for j in ds if ke[j]["i"] == i and ke[j]["el"] == "t")
            ev.update(self._cua_so_cao(lay, moc[jt][0], moc[jt][1], jt))
        la_phu = lambda el: el in ("p", "r") or (len(el) > 1 and el[0] in "me" and el[1:].isdigit())  # noqa: E731
        #  CUỘN theo phần đang giảng (mượt giữa hai phần liền nhau cùng câu / cùng trang); cộng vào ``dy`` nên mọi thứ trên slide + ngòi bút đi cùng.
        if lay.get("cao"):
            cuon = self._cuon_cao(lay, t, ev)
        else:
            c_moi = self._cuon_muc(lay, el_act)
            j_truoc = ds[m - 1] if m >= 1 and ke[ds[m - 1]]["i"] == i else None
            c_cu = self._cuon_muc(lay, ke[j_truoc]["el"]) if (j_truoc is not None and trang(ke[j_truoc]["el"]) == p) else 0.0
            cuon = c_cu + (c_moi - c_cu) * mem((t - (moc[j_act][0] - 0.1)) / 0.5)
        dy -= int(round(cuon))
        #  Đang cuộn: phần câu gốc trôi lên dưới dải tiêu đề → dải phủ nền giấy dày thêm (tăng dần theo độ cuộn, không giật) để chữ trôi lên không lẫn với tiêu đề / bản dịch tiêu đề.
        self.dai_tren, self.dai_mem = self.dau + int(30 * min(1.0, cuon / 40.0)), 26 if cuon > 0.5 else 22
        # thẻ từ vựng / ngữ pháp + bóng đổ + nhãn
        for c in lay["cards"]:
            x0, y0, _, _ = c["rect"]
            RV.tron(world, (40, 40, 70), c["bong"], x0 - 20 + dx, y0 - 20 + dy, a_p)
            RV.tron(world, c["nen"], c["m"], x0 - 20 + dx, y0 - 20 + dy, 0.96 * a_p)
            txt = self.nhan(c["lab"])
            lc = self._pill_chu.get(txt)
            if lc is None:
                lc = Chu(txt, self._font_nhan(txt), 26 * self.co, (255, 255, 255), 0, 0, 400, "giua", 1)
                self._pill_chu[txt] = lc
            ph_, pw_ = lc.h + 10, lc.w + 34
            px, py = int(c["x"]) + dx, int(c["y"]) + dy
            cv2.rectangle(world, (px + ph_ // 2, py), (px + pw_ - ph_ // 2, py + ph_), c["mau"], -1, cv2.LINE_AA)
            cv2.circle(world, (px + ph_ // 2, py + ph_ // 2), ph_ // 2, c["mau"], -1, cv2.LINE_AA)
            cv2.circle(world, (px + pw_ - ph_ // 2, py + ph_ // 2), ph_ // 2, c["mau"], -1, cv2.LINE_AA)
            lc.x0, lc.y0 = px + (pw_ - lc.w) // 2, py + (ph_ - lc.h) // 2
            lc.ve(world, a_p)
        x0, y0, x1, y1_ = lay["rect"]
        RV.tron(world, (40, 40, 70), lay["ms"]["bong"], x0 - 20, y0 - 20 + dy, a_p)
        RV.tron(world, lay["nhat"], lay["ms"]["m"], x0 - 20, y0 - 20 + dy, 0.96 * a_p)
        # avatar người đọc
        rgb, al = self.avt[lay["sp"]]
        av = lay["av"]
        ax, ay = lay["av_xy"]
        if av != self.AVD:
            rgb = cv2.resize(rgb, (av, av), interpolation=cv2.INTER_AREA)
            al = cv2.resize(al, (av, av), interpolation=cv2.INTER_AREA)
        RV.tron(world, rgb, al, ax, ay + dy, a_p)
        if self.ten_hien:
            tenc = self.ten[lay["sp"]]
            tenc.x0, tenc.y0 = int(ax + av / 2 - tenc.w / 2), ay + av + 4 + dy
            tenc.ve(world, a_p)
        a_el, wd_el = ev.get(el_act, (0.0, 0.0))
        if el_act == "t" and moc[j_act][0] <= t <= moc[j_act][1] + 0.05:
            cv2.circle(world, (int(ax + av / 2), int(ay + av / 2 + dy)), int(av / 2 + 7 + 3 * math.sin(t * 9.0)), lay["dam"], 3, cv2.LINE_AA)
        # tô nền phần đang đọc
        hl = lay["el"].get(el_act)
        if hl is not None:
            rx0, ry0, rx1, ry1 = hl["rect"]
            kh = (int(rx1 - rx0), int(ry1 - ry0))
            if kh not in self._hl:
                self._hl[kh] = _hop_bo_goc(kh[0], kh[1], 16, 10)
            RV.tron(world, (255, 232, 160), self._hl[kh], int(rx0) - 10 + dx, int(ry0) - 10 + dy, 0.6 * a_p * mem((t - a_el) / 0.2))
        # chữ
        for el, d in lay["el"].items():
            if d.get("statico"):
                for c, _ in d["parts"]:
                    c.x0, c.y0 = c._bx + dx, c._by + dy
                    c.ve(world, a_p)
                continue
            if el not in ev:
                continue
            a0, wd = ev[el]
            for chu, _ in d["parts"]:
                chu.x0, chu.y0 = chu._bx + dx, chu._by + dy
                if la_phu(el):                                                         # phần phụ chạy dần (trái → phải) theo lời đọc phần chính, xong trước khi hết lời
                    chu.ve(world, a_p, quet=doan(t, a0, a0 + 0.85 * (min(wd, 1.5) if el[0] in "me" else wd)), ngang=True)
                    continue
                if el == "t" and "t" in jel and (not lay.get("cao") or "_jt" in ev):  # câu gốc: bút đi theo nhịp + chỗ nghỉ thật của giọng (trang CAO chưa có mốc tiếng thật: cửa sổ riêng, xem _cua_so_cao)
                    q = self._q_but(jel["t"], chu, t)
                else:
                    q = doan(t, a0, a0 + (min(wd, 1.5) if el[0] in "vg" else wd))     # từ / mẫu ngữ pháp viết nhanh (≈ 1,5 s) rồi người dẫn còn giảng tiếp
                chu.ve(world, a_p, quet=q)
                if 0 < q < 1:
                    self.tip = chu.dau_but(q)
        # camera: thẻ chi tiết (từ vựng / ngữ pháp) đang đọc → zoom vào thẻ; câu gốc / bản dịch → nhìn toàn cảnh
        #  Zoom vào thẻ CHỈ trong lúc chữ của thẻ đang được viết (~1,5 s); viết xong thì lùi ra xem TỔNG THỂ cả slide (người xem không bị kẹt ở một thẻ trong lúc người dẫn còn giảng — 05-10-2026).
        if self.cam_on and hl is not None and el_act not in ("t", "r") and t <= a_el + min(wd_el, 1.5) + 0.5:
            cx0, cy0, cx1, cy1 = hl["cam"]
            self.cam_muc = self.cam_rect((cx0 + dx, cy0 - cuon, cx1 + dx, cy1 - cuon), 1.08)


# ====================================================================== THẺ TIÊU ĐỀ (mở đầu / kết thúc)
class TieuDe(_Hoc):
    """Một dòng chữ lớn viết tay giữa trang + nét gạch chân, rồi giữ nguyên; camera nhích vào chậm."""

    def __init__(self, text: str, mau: str, bc, ngon: str, tuy: dict, phu: str = "", noi: float = 0.0):
        super().__init__(bc, ngon, tuy)
        #  ``noi`` = giây người dẫn ĐỌC tiêu đề (0 = thẻ im lặng). Tiêu đề được viết suốt lúc đọc (bút đi cùng giọng nói) rồi mới gạch chân + hiện dòng nguồn.
        self.t0 = 0.35 if noi > 0 else 0.3
        self.t_viet = max(1.2, 0.9 * noi) if noi > 0 else 1.6
        co = 104 if not self.doc else 84
        tron = RB.bo_ruby(text)                                                     # đo / chọn font theo CHỮ TRƠN; phần ``[cách đọc]`` do ``self.chu`` vẽ thành furigana (hoặc bỏ nếu tắt)
        f = self._font_cho(ngon, tron, "label")
        rong = self.W - 160
        while co > 40 and len(CH.ngat_dong(tron, CH.font(f, int(co)), rong)) > 2:
            co -= 6
        self.mau = MAU_TIEU_DE.get(mau, MAU_TIEU_DE["blue"])
        self.c = self.chu(text, co / self.co, self.mau, self.W / 2, self.H / 2, rong, "giua", 2, font=f)
        while co > 40 and self.c.w > rong + 16:                                      # furigana làm dòng rộng hơn chữ trơn: thu nhỏ tới khi vừa
            co -= 6
            self.c = self.chu(text, co / self.co, self.mau, self.W / 2, self.H / 2, rong, "giua", 2, font=f)
        self._gach = (self.c.x0, self.c.x0 + self.c.w, self.c.y0 + self.c.h + 6)
        self.phu = None
        if phu:                                                                     # dòng phụ nhỏ bên dưới (ghi nguồn bài viết)
            fp = self._font_cho("en", phu, "subtitle", du_phong=((ngon, "subtitle"), ("vi", "subtitle")))
            #  qua ``self.chu`` (không dựng ``Chu`` thẳng): tên nguồn như "NHKニュース" lẫn trong câu tiếng Việt, font tiếng Việt không có katakana nên chữ đó bị BỎ lặng lẽ ("…: NHK")
            self.phu = self.chu_nhieu_dong(phu, 26 if not self.doc else 22, SLATE, self.W / 2, self._gach[2] + 54 - 20, self.W - 160, "giua", fp, 6)       # nguồn + ngày đăng (không link: link ở phần mô tả video)

    def ve(self, world: np.ndarray, t: float) -> None:
        self.tip = None
        t1 = self.t0 + self.t_viet                                                  # lúc viết xong tiêu đề
        self.cam_muc = (1.0 + 0.03 * mem(doan(t, 0.0, t1 + 1.5)), self.W / 2, self.H / 2) if self.cam_on else None
        if self.phu is not None:
            self.phu.ve(world, a=mem(doan(t, t1 + 0.3, t1 + 1.0)))
        q = doan(t, self.t0, t1)
        self.c.ve(world, quet=q)
        if 0 < q < 1:
            self.tip = self.c.dau_but(q)
        g = doan(t, t1, t1 + 0.5)
        if g > 0:
            x0, x1, y = self._gach
            xe = x0 + (x1 - x0) * mem(g)
            cv2.line(world, (int(x0), int(y)), (int(xe), int(y + 4 * math.sin(g * 6.0))), self.mau, 7, cv2.LINE_AA)
            if g < 1:
                self.tip = (xe, y)


# ====================================================================== TOÀN VĂN (cuối video)
_DEM_TV: dict = {}                                    # bố cục toàn văn đã dựng (dựng tốn vài giây; ``canh_toan_van`` rồi bộ dựng cảnh đều cần nó, và dựng lại video cũng vậy)
_KHOA_TV = threading.Lock()


class ToanVan(_Hoc):
    """TOÀN BỘ KỊCH BẢN NÓI trong video (lời chào, câu của bài, lời chuyển, câu hỏi + đáp án, lời dẫn dắt, lời giảng, lời kết), đặt ở CUỐI video (sau lượt cuối, trước thẻ kết thúc) để người xem tạm dừng đọc lại
    và nhìn TỔNG QUAN. Chữ chảy LIỀN như một đoạn văn (không xuống dòng theo từng câu) và được THU NHỎ cho vừa 1 trang (nếu chữ vẫn ≥ 24 px), không thì 2 trang, rồi nhiều hơn nếu thật sự cần (cỡ nhỏ nhất
    ``CO_NHO``). Lần 1 CÓ cách đọc (furigana phía trên chữ Hán), lần 2 KHÔNG có; ngôn ngữ không có furigana (hoặc tắt furigana) thì một lần. Không có tiếng."""

    GIAY_CJK, GIAY_KHAC, GIAY_LAI = 0.03, 0.025, 0.02                                # giây giữ mỗi chữ (chỉ để tham khảo / tạm dừng đọc nên ngắn): lần đầu CJK · chữ Latin · lần hai không cách đọc
    CO_NHO = 20                                                                      # sàn cỡ chữ (px): dưới mức này không đọc nổi trên điện thoại → thêm trang thay vì thu nhỏ

    def __init__(self, cau: list, bc, ngon: str, tuy: dict):
        super().__init__(bc, ngon, tuy)
        W, H, doc = self.W, self.H, self.doc
        self.mar = mar = 28 if doc else 40
        cau = [str(c).strip() for c in cau if str(c or "").strip()]
        cjk = ngon in ("ja", "zh-cn", "ko")
        sep = "" if cjk else " "
        co_furi = bool(self.furi and any(RB.co_ruby(c) for c in cau))
        self.f_t = self._font_cho(ngon, RB.bo_ruby(sep.join(cau)) or "a", "subtitle")
        self.cac_phan: list = []                                                      # [(Chu nhãn, Chu gợi ý, [trang])]; trang = {"khoi": [Chu], "so": Chu, "dai": giây}
        self.y_gach = 0
        for furi, khoa in ([(True, "toan_furi"), (False, "toan_tron")] if co_furi else [(False, "toan")]):
            tieu, goi = self.nhan(khoa), self.nhan("tam_dung")
            nhan = self.chu(tieu, 32 * (0.85 if doc else 1.0), (60, 90, 170), mar, 0, W - 2 * mar - 150, "trai", 1, self._font_nhan(tieu, "subtitle"))
            nhan.y0 = 14
            goi_y = self.chu(goi, 20 * (0.9 if doc else 1.0), SLATE, mar + nhan.w + 22, 0, W - 2 * mar, "trai", 1, self._font_nhan(goi, "subtitle"))
            if goi_y.x0 + goi_y.w > W - mar - 90:                                    # khung hẹp (9:16): gợi ý xuống dưới nhãn
                goi_y.x0, goi_y.y0 = mar, nhan.y0 + nhan.h
                dau = goi_y.y0 + goi_y.h
            else:
                goi_y.y0 = nhan.y0 + max(0, nhan.h - goi_y.h - 6)
                dau = nhan.y0 + nhan.h
            self.y_gach = dau + 8
            top, duoi = self.y_gach + 12, H - 24
            khoi = self._dung_trang(cau, furi, sep, self.f_t, W - 2 * mar, duoi - top)
            trang = []
            for i, (chu, n_chu) in enumerate(khoi):
                chu.x0, chu.y0 = mar, top
                so = self.chu(f"{i + 1}/{len(khoi)}", 26 * (0.85 if doc else 1.0), SLATE, W - mar, 0, 200, "phai", 1, self._font_nhan(f"{i + 1}/{len(khoi)}", "subtitle"))
                so.y0 = nhan.y0 + 2
                gc = self.GIAY_LAI if (cjk and not furi and co_furi) else self.GIAY_CJK if cjk else self.GIAY_KHAC
                trang.append({"khoi": [chu], "so": so, "dai": min(45.0, max(8.0, 6.0 + n_chu * gc))})
            self.cac_phan.append((nhan, goi_y, trang))
        self.tong = float(sum(t_["dai"] for _, _, tr in self.cac_phan for t_ in tr))

    def _dung_trang(self, cau: list, furi: bool, sep: str, f_t: str, tw: float, cao: float) -> list:
        """[(khối chữ, số chữ)] mỗi trang MỘT khối chảy liền. Ưu tiên 1 trang nếu chữ còn ≥ 24 px, rồi 2 trang (cỡ lớn nhất vừa khung), rồi nhiều hơn; mỗi trang chứa các câu liền nhau, chia cân theo số chữ."""
        tron = [RB.bo_ruby(c) for c in cau]

        def chia(n: int) -> list:
            tong = sum(len(t) for t in tron) or 1
            phan, cur, acc = [], [], 0
            for c_, t in zip(cau, tron):
                cur.append(c_)
                acc += len(t)
                if len(phan) < n - 1 and acc >= tong * (len(phan) + 1) / n:
                    phan.append(cur)
                    cur = []
            if cur:
                phan.append(cur)
            return phan

        def dung(phan: list, co: int, gioi_han: float) -> Optional[list]:
            ra = []
            for p in phan:
                van = sep.join(p) if furi else RB.bo_ruby(sep.join(p))
                chu = self.chu(van, co, MUC, 0, 0, tw, "trai", 400, f_t)
                if chu.h > gioi_han:
                    return None
                ra.append((chu, sum(len(RB.bo_ruby(c_)) for c_ in p)))
            return ra

        def uoc(phan: list) -> int:
            """Ước cỡ chữ lớn nhất vừa khung từ diện tích: số chữ × (cỡ² × hệ số dòng) ≈ rộng × cao. Dựng khối chữ có furigana tốn ~1 s nên KHÔNG dò nhị phân mà dò quanh số ước."""
            n_max = max((sum(len(RB.bo_ruby(c_)) for c_ in p) for p in phan), default=1) or 1
            he = 1.85 if furi else 1.35                                               # chiều cao dòng / cỡ chữ (furigana thêm ~0,5)
            return int(max(self.CO_NHO, min(36, (tw * cao / (he * n_max * 1.12)) ** 0.5)))

        def tot_nhat(phan: list) -> Optional[tuple]:
            co = uoc(phan)
            r = dung(phan, co, cao)
            if r is not None:                                                         # vừa → thử to hơn từng bước tới khi hết vừa
                best = (co, r)
                while best[0] < 36:
                    r2 = dung(phan, best[0] + 1, cao)
                    if r2 is None:
                        break
                    best = (best[0] + 1, r2)
                return best
            while co > self.CO_NHO:                                                   # chưa vừa → nhỏ dần
                co -= 1
                r = dung(phan, co, cao)
                if r is not None:
                    return (co, r)
            return None

        khoa = (furi, sep, round(tw), round(cao), f_t, self.co, hash(tuple(cau)))
        with _KHOA_TV:
            if khoa in _DEM_TV:
                return _DEM_TV[khoa]
        kq_cuoi: Optional[list] = None
        for n in range(1, 9):
            phan = chia(n)
            if n == 1 and uoc(phan) < 18:                                             # ước đã nhỏ hơn 18 px: một trang không đạt, khỏi dựng thử tốn thời gian
                continue
            kq = tot_nhat(phan)
            if kq is not None and (n >= 2 or kq[0] >= 24):
                kq_cuoi = kq[1]
                break
        if kq_cuoi is None:
            kq_cuoi = dung(chia(8), self.CO_NHO, 10 ** 9) or []                       # quá dài mà vẫn phải dựng: cỡ nhỏ nhất, nhiều trang (người gọi đã giới hạn độ dài)
        with _KHOA_TV:
            _DEM_TV[khoa] = kq_cuoi
            while len(_DEM_TV) > 6:
                _DEM_TV.pop(next(iter(_DEM_TV)))
        return kq_cuoi

    def khoa_khung(self, t: float) -> Optional[tuple]:
        """Khoá của khung KHÔNG ĐỔI (trang đã hiện đủ, chưa mờ đi): bộ dựng clip dùng lại khung trước thay vì vẽ lại — mỗi trang giữ vài chục giây ở nguyên một hình."""
        t0 = 0.0
        for pi, (_, _, trang) in enumerate(self.cac_phan):
            for ti, tr in enumerate(trang):
                if t < t0 + tr["dai"]:
                    return (pi, ti) if t0 + 0.5 <= t <= t0 + tr["dai"] - 0.4 else None
                t0 += tr["dai"]
        return None

    def ve(self, world: np.ndarray, t: float) -> None:
        self.tip = None
        self.cam_muc = None
        t0 = 0.0
        W = self.W
        for pi, (nhan, goi_y, trang) in enumerate(self.cac_phan):
            for ti, tr in enumerate(trang):
                d = tr["dai"]
                cuoi = pi == len(self.cac_phan) - 1 and ti == len(trang) - 1
                if t < t0 + d or cuoi:
                    a = mem(doan(t, t0, t0 + 0.45)) * (1.0 - mem(doan(t, t0 + d - 0.35, t0 + d)))
                    if a > 0.01:
                        nhan.ve(world, a)
                        goi_y.ve(world, a)
                        tr["so"].ve(world, a)
                        cv2.line(world, (self.mar, self.y_gach), (W - self.mar, self.y_gach), (210, 212, 224), 2, cv2.LINE_AA)
                        for k in tr["khoi"]:
                            k.ve(world, a)
                    return
                t0 += d
